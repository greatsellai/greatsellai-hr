from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import AppSettings
from app.schemas import (
    CandidateSearchRequest,
    RecruitingAgentAction,
    RecruitingAgentCandidate,
    RecruitingAgentRequest,
    RecruitingAgentResponse,
    RecruitingAgentToolTrace,
    ResumeScoreCreate,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    active_legacy_payload_executor,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
)
from app.services.job_match_batch_service import enqueue_job_version_match_batch
from app.services.job_service import (
    JobServiceError,
    JobVersionNotFoundError,
    get_job_version,
    get_latest_confirmed_job_version,
    list_job_version_matches,
    list_resume_job_matches,
)
from app.services.search_service import search_candidates
from app.services.score_service import (
    DeepSeekProviderError as ScoreDeepSeekProviderError,
    ScoreServiceError,
    ScoreTemplateNotFoundError,
    list_score_templates,
    run_resume_score,
)


class RecruitingAgentServiceError(RuntimeError):
    """A visible failure, never a silent fallback to rule matching."""


@dataclass(frozen=True)
class ResolvedJob:
    job_version_id: str
    title: str


AgentIntent = Literal[
    "search_candidates",
    "run_job_matching",
    "show_job_ranking",
    "explain_candidate",
    "score_current_candidate",
    "help",
]


@dataclass
class ToolRun:
    payload: dict[str, Any]
    cards: list[RecruitingAgentCandidate] = field(default_factory=list)
    actions: list[RecruitingAgentAction] = field(default_factory=list)
    traces: list[RecruitingAgentToolTrace] = field(default_factory=list)
    batch_id: str | None = None
    intent: AgentIntent | None = None


_STRING_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 255},
}

_EDUCATION_FILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "degree_in": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "vocational_or_below", "high_school", "associate",
                    "bachelor", "master", "doctor",
                ],
            },
            "maxItems": 5,
        },
        "school_name_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "major_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "institution_tiers_any_of": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "211", "985", "double_first_class", "key_undergraduate",
                "first_tier", "second_tier", "regular_undergraduate",
                "private_undergraduate", "higher_vocational", "overseas",
            ]},
            "maxItems": 10,
        },
        "min_average_score": {"type": "number", "minimum": 0, "maximum": 100},
        "min_gpa_percent": {"type": "number", "minimum": 0, "maximum": 100},
        "max_rank_position": {"type": "integer", "minimum": 1},
        "max_rank_percent": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
    },
}

_EXPERIENCE_FILTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "experience_types": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "employment",
                    "internship",
                    "project",
                    "research",
                    "competition",
                    "campus",
                    "club",
                    "volunteer",
                    "entrepreneurship",
                    "training",
                    "other",
                    "unknown",
                ],
            },
            "maxItems": 12,
        },
        "experience_name_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "organization_name_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "title_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "leadership_contexts_any_of": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "class", "student_org", "club", "project_team", "company",
            ]},
            "maxItems": 5,
        },
        "leadership_roles_any_of": {**_STRING_ARRAY_SCHEMA, "maxItems": 12},
        "award_levels_any_of": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "national", "provincial", "school", "department", "other",
            ]},
            "maxItems": 5,
        },
        "award_result_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
    },
    "required": ["experience_types"],
}


_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_985_211": {"type": "boolean"},
        "highest_degree_in": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "vocational_or_below", "high_school", "associate",
                "bachelor", "master", "doctor",
            ]},
            "maxItems": 6,
        },
        "graduation_status": {"type": "string", "enum": ["any", "fresh", "previous"]},
        "fresh_graduate_start_month": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$"},
        "fresh_graduate_end_month": {"type": "string", "pattern": "^\\d{4}-(0[1-9]|1[0-2])$"},
        "min_employment_months": {"type": "integer", "minimum": 0, "maximum": 720},
        "min_employment_or_internship_months": {"type": "integer", "minimum": 0, "maximum": 720},
        "education_any_of": {
            "type": "array",
            "items": _EDUCATION_FILTER_SCHEMA,
            "maxItems": 10,
        },
        "experience_any_of": {
            "type": "array",
            "items": _EXPERIENCE_FILTER_SCHEMA,
            "maxItems": 10,
        },
        "skill_categories_any_of": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "software", "data_ai", "product_project", "design_content",
                "marketing_ecommerce_operations", "sales_customer_service",
                "supply_chain_logistics", "finance_legal_hr",
                "office_collaboration", "industry_professional",
            ]},
            "maxItems": 10,
        },
        "skills_all_of": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "skills_any_of": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "language_credentials_any_of": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "credential_code": {"type": "string", "enum": [
                        "cet4", "cet6", "ielts", "toefl", "tem4", "tem8", "bec", "toeic", "custom",
                    ]},
                    "custom_name_contains": {"type": "string"},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 1000},
                },
                "required": ["credential_code"],
            },
            "maxItems": 12,
        },
        "scholarship_status": {"type": "string", "enum": ["any", "present", "unknown"]},
        "scholarship_levels_any_of": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "national", "provincial", "school", "department", "enterprise", "other",
            ]},
            "maxItems": 6,
        },
        "scholarship_name_contains": {**_STRING_ARRAY_SCHEMA, "maxItems": 8},
        "competition_status": {"type": "string", "enum": ["any", "present", "unknown"]},
        "competition_award_status": {"type": "string", "enum": ["any", "present", "unknown"]},
        "leadership_any_of": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "contexts_any_of": {
                        "type": "array",
                        "items": {"type": "string", "enum": [
                            "class", "student_org", "club", "project_team", "company",
                        ]},
                        "maxItems": 5,
                    },
                    "roles_any_of": {**_STRING_ARRAY_SCHEMA, "maxItems": 12},
                },
            },
            "maxItems": 5,
        },
        "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "keyword_match_mode": {"type": "string", "enum": ["broad", "precise"]},
        "keywords_all_of": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "keywords_any_of": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
}


_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_candidates",
            "description": (
                "Search the verified resume database. Use this whenever the user asks to find, "
                "filter, or shortlist candidates. Convert years to months. Put degree, school, "
                "and major that must be true of the same education record in one "
                "education_any_of object. Put experience type, company, and title that must be "
                "true of the same experience record, including leadership and awards, in one "
                "experience_any_of object. Use skill_categories_any_of for categories and "
                "skills_all_of for all-required skills. Scholarship, competition, and leadership "
                "filters are optional and unknown never means absent. For recruiter keywords use keywords "
                "with keyword_match_mode broad or precise. English credential alternatives "
                "belong in language_credentials_any_of and are OR conditions."
            ),
            "parameters": _SEARCH_SCHEMA,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_current_job_match_batch",
            "description": "Start the asynchronous match-all job for the currently selected confirmed JD.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_job_ranking",
            "description": "Read completed JD matching results for the currently selected confirmed JD.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_current_candidate_match",
            "description": "Read the saved JD match facts for the currently selected candidate and JD.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_current_candidate",
            "description": (
                "Run a new fact-grounded AI score for the currently selected candidate using one "
                "of the existing score templates announced in current_score_templates. Use this "
                "when the user asks to score, rate, or evaluate the current candidate. Never "
                "invent a template ID or score a candidate that is not currently selected."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "template_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    }
                },
                "required": ["template_id"],
            },
        },
    },
]


def _model_completion(*, settings: AppSettings, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Run one Agent model step through the active platform AI route.

    ``run_recruiting_agent_turn`` installs exactly one gateway execution
    context around the whole tool loop.  Each call here therefore becomes a
    separate immutable provider invocation under that one AI run, while this
    service remains unaware of the provider, model, endpoint, or credential.
    """

    # Keep ``settings`` in the public helper signature so existing service
    # tests and callers do not need a compatibility shim.  All connection
    # settings are deliberately resolved by the gateway, not here.
    del settings
    gateway_executor = active_legacy_payload_executor()
    if gateway_executor is None:
        raise RecruitingAgentServiceError("agent_model_not_configured")
    try:
        payload = gateway_executor(
            {
                "temperature": 0,
                "max_tokens": 900,
                "messages": messages,
                "tools": _TOOLS,
                "tool_choice": "auto",
            }
        )
    except AiGatewayError as exc:
        raise _gateway_error_as_agent_error(exc) from exc
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RecruitingAgentServiceError("agent_model_empty_response") from exc
    if not isinstance(message, dict):
        raise RecruitingAgentServiceError("agent_model_invalid_response")
    return message


def _gateway_error_as_agent_error(exc: AiGatewayError) -> RecruitingAgentServiceError:
    """Preserve the Agent's stable, non-sensitive public failure vocabulary."""

    code = str(exc)
    if code == "ai_provider_timeout":
        return RecruitingAgentServiceError("agent_model_timeout")
    if code == "ai_provider_network":
        return RecruitingAgentServiceError("agent_model_network_error")
    if code in {"ai_provider_structured_invalid", "ai_provider_truncated"}:
        return RecruitingAgentServiceError("agent_model_invalid_response")
    if code in {
        "ai_route_not_configured",
        "ai_route_disabled",
        "ai_route_not_published",
        "ai_route_credential_not_configured",
        "ai_route_model_unavailable",
        "ai_route_provider_unavailable",
        "ai_route_endpoint_missing",
        "ai_route_capability_missing",
        "ai_route_output_limit_exceeded",
        "ai_provider_configuration",
        "ai_provider_driver_not_supported",
    }:
        return RecruitingAgentServiceError("agent_model_not_configured")
    # The gateway intentionally removes provider response bodies and status
    # details.  Do not turn a routing/provider issue into a raw 500 or leak
    # vendor-specific information through the recruiter UI.
    return RecruitingAgentServiceError("agent_model_unavailable")


def _resolve_job(session: Session, requested_job_version_id: str | None) -> ResolvedJob | None:
    try:
        item = (
            get_job_version(session, job_version_id=requested_job_version_id)
            if requested_job_version_id
            else get_latest_confirmed_job_version(session)
        )
    except (JobServiceError, JobVersionNotFoundError):
        return None
    # Source JDs published as-is intentionally have no extracted conditions.
    # They are visible in the JD workspace, but must never become an Agent
    # matching target: matching tools require at least one structured clause.
    if item.status != "confirmed" or not item.requirements:
        return None
    return ResolvedJob(job_version_id=item.job_version_id, title=item.title)


def _score_template_context(session: Session) -> list[dict[str, object]]:
    """Expose only existing, server-owned templates to the Agent model."""

    return [
        {
            "template_id": template.template_id,
            "name": template.name,
            "version": template.version,
            "dimensions": [
                {
                    "key": dimension.key,
                    "label": dimension.label,
                    "weight": dimension.weight,
                }
                for dimension in template.dimensions
            ],
        }
        for template in list_score_templates(session)
    ]


def _clean_tool_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RecruitingAgentServiceError("agent_tool_arguments_missing")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RecruitingAgentServiceError("agent_tool_arguments_invalid") from exc
    if not isinstance(parsed, dict):
        raise RecruitingAgentServiceError("agent_tool_arguments_invalid")
    return parsed


def _remove_null_values(value: object) -> object:
    """Treat tool-call JSON nulls as omitted optional fields, recursively."""

    if isinstance(value, dict):
        return {
            key: _remove_null_values(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_remove_null_values(item) for item in value if item is not None]
    return value


def _search(session: Session, arguments: dict[str, Any]) -> ToolRun:
    allowed = set(_SEARCH_SCHEMA["properties"])
    # Tool models occasionally include optional keys with JSON null.  Those
    # are omissions, not a reason to turn a recruiter request into a 500.
    cleaned_arguments = _remove_null_values(arguments)
    if not isinstance(cleaned_arguments, dict):
        raise RecruitingAgentServiceError("agent_search_arguments_invalid")
    values = {
        key: value
        for key, value in cleaned_arguments.items()
        if key in allowed
    }
    raw_limit = values.get("limit", 20)
    values["limit"] = (
        min(raw_limit, 20)
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool)
        else 20
    )
    try:
        request = CandidateSearchRequest.model_validate(values)
    except (TypeError, ValueError) as exc:
        raise RecruitingAgentServiceError("agent_search_arguments_invalid") from exc
    result = search_candidates(session, request)
    applied = {
        key: value
        for key, value in request.model_dump(exclude_none=True).items()
        if value not in ([], 20, None)
    }
    cards = [
        RecruitingAgentCandidate(
            candidate_id=item.candidate_id,
            resume_id=item.resume_id,
            display_name=item.display_name,
            detail=(
                f"{'985/211' if item.is_985_211 else '非 985/211'} · "
                f"工作经历 {item.employment_months // 12} 年 {item.employment_months % 12} 个月"
            ),
        )
        for item in result.items
    ]
    return ToolRun(
        payload={
            "applied_filters": applied,
            "result_count": len(cards),
            "needs_review_count": result.needs_review_count,
            "candidates": [
                {
                    "name": card.display_name or "未命名候选人",
                    "is_985_211": item.is_985_211,
                    "employment_months": item.employment_months,
                    "matched_filters": item.matched_filters,
                }
                for card, item in zip(cards, result.items, strict=True)
            ],
        },
        cards=cards,
        traces=[RecruitingAgentToolTrace(tool="简历筛选", summary=f"已按 {json.dumps(applied, ensure_ascii=False)} 筛选 {len(cards)} 人")],
        intent="search_candidates",
    )


def _start_batch(
    session: Session,
    job: ResolvedJob | None,
    *,
    settings: AppSettings,
) -> ToolRun:
    if job is None:
        return ToolRun(payload={"error": "没有已确认的当前 JD，无法启动批量匹配。"})
    batch = enqueue_job_version_match_batch(
        session,
        job_version_id=job.job_version_id,
        settings=settings,
    )
    return ToolRun(
        payload={"job_title": job.title, "batch_id": batch.batch_id, "total_count": batch.total_count, "status": batch.status},
        actions=[RecruitingAgentAction(action="open_match_workspace", label="打开 JD 匹配工作区")],
        traces=[RecruitingAgentToolTrace(tool="批量 JD 匹配", summary=f"已启动“{job.title}”的 {batch.total_count} 份简历匹配任务")],
        batch_id=batch.batch_id,
        intent="run_job_matching",
    )


def _ranking(session: Session, job: ResolvedJob | None, arguments: dict[str, Any]) -> ToolRun:
    if job is None:
        return ToolRun(payload={"error": "没有已确认的当前 JD，无法读取排行榜。"})
    limit = arguments.get("limit", 10)
    if not isinstance(limit, int):
        limit = 10
    latest: dict[str, Any] = {}
    for match in list_job_version_matches(session, job_version_id=job.job_version_id):
        latest.setdefault(match.resume_id, match)
    ranked = sorted(latest.values(), key=lambda item: item.total_score, reverse=True)[: min(max(limit, 1), 20)]
    cards = [
        RecruitingAgentCandidate(
            candidate_id=item.candidate_id,
            resume_id=item.resume_id,
            display_name=item.candidate_display_name,
            score=item.total_score,
            detail=f"JD 匹配 {item.total_score:.1f} 分 · {item.hard_requirement_status or '硬条件待确认'}",
        )
        for item in ranked
    ]
    return ToolRun(
        payload={
            "job_title": job.title,
            "matched_count": len(latest),
            "ranking": [
                {"name": card.display_name or "未命名候选人", "score": card.score, "hard_requirement_status": item.hard_requirement_status}
                for card, item in zip(cards, ranked, strict=True)
            ],
        },
        cards=cards,
        actions=[RecruitingAgentAction(action="open_match_workspace", label="打开 JD 匹配工作区")],
        traces=[RecruitingAgentToolTrace(tool="JD 匹配排行", summary=f"已读取“{job.title}”的 {len(latest)} 条完成匹配结果")],
        intent="show_job_ranking",
    )


def _explain(session: Session, job: ResolvedJob | None, resume_id: str | None) -> ToolRun:
    if not resume_id:
        return ToolRun(payload={"error": "当前未选择候选人。"})
    matches = list_resume_job_matches(session, resume_id=resume_id)
    match = next((item for item in matches if job and item.job_version_id == job.job_version_id), None)
    if match is None:
        return ToolRun(payload={"error": "当前候选人没有所选 JD 的已完成匹配结果。"})
    return ToolRun(
        payload={
            "candidate_name": match.candidate_display_name or "未命名候选人",
            "job_title": job.title if job else None,
            "total_score": match.total_score,
            "hard_requirement_status": match.hard_requirement_status,
            "requirements": [
                {
                    "requirement": item.requirement_text,
                    "outcome": item.outcome,
                    "reason": item.reason,
                    "uncertainty": item.missing_or_uncertain,
                }
                for item in match.requirement_results
            ],
        },
        traces=[RecruitingAgentToolTrace(tool="匹配解释", summary="已读取当前候选人的已保存 JD 匹配证据")],
        intent="explain_candidate",
    )


def _score_current_candidate(
    session: Session,
    *,
    arguments: dict[str, Any],
    resume_id: str | None,
    settings: AppSettings,
) -> ToolRun:
    """Run the existing score pipeline; the Agent never supplies score values."""

    if not resume_id:
        return ToolRun(
            payload={"error": "当前未选择候选人，无法运行评分。"},
            intent="score_current_candidate",
        )
    if set(arguments) != {"template_id"}:
        return ToolRun(
            payload={"error": "评分工具参数无效，未执行评分。"},
            intent="score_current_candidate",
        )
    template_id = arguments.get("template_id")
    if not isinstance(template_id, str) or not template_id.strip():
        return ToolRun(
            payload={"error": "请选择已有评分模板后再运行评分。"},
            intent="score_current_candidate",
        )
    template_id = template_id.strip()
    templates = {template.template_id: template for template in list_score_templates(session)}
    template = templates.get(template_id)
    if template is None:
        return ToolRun(
            payload={"error": "所选评分模板不存在或已归档，未执行评分。"},
            intent="score_current_candidate",
        )
    try:
        score = run_resume_score(
            session,
            resume_id=resume_id,
            payload=ResumeScoreCreate(template_id=template_id),
            settings=settings,
        )
    except ScoreTemplateNotFoundError:
        # A template can be archived between context construction and tool
        # execution; do not turn that race into a raw agent failure.
        return ToolRun(
            payload={"error": "所选评分模板已不可用，未执行评分。"},
            intent="score_current_candidate",
        )
    except ScoreDeepSeekProviderError:
        return ToolRun(
            payload={"error": "评分模型暂时没有返回可用结果，请稍后重试。"},
            intent="score_current_candidate",
        )
    except ScoreServiceError as exc:
        error_messages = {
            "deepseek_api_key_not_configured": "评分模型尚未配置。",
            "resume_not_found": "当前候选人不存在。",
            "resume_must_be_active_and_ready_for_scoring": "当前候选人的简历尚未完成可评分解析。",
            "resume_source_text_unreliable": "当前简历的原文文本待校正，暂不能用于 AI 评分。",
            "resume_fact_snapshot_not_current": "当前候选人的事实版本尚未准备完成。",
            "resume_fact_snapshot_invalid": "当前候选人的事实版本不可用于评分。",
        }
        return ToolRun(
            payload={"error": error_messages.get(str(exc), "当前无法运行评分，请稍后重试。")},
            intent="score_current_candidate",
        )

    dimensions = [
        {
            "label": dimension.label,
            "weight": dimension.weight,
            "ai_raw_score": dimension.ai_raw_score,
            "final_raw_score": dimension.final_raw_score,
            "rationale": dimension.rationale,
            "evidence_state": dimension.evidence_state,
            "fact_evidence": [
                {"fact_type": fact.fact_type, "summary": fact.summary}
                for fact in dimension.fact_evidence
            ],
            "uncertainties": dimension.uncertainties,
        }
        for dimension in score.dimension_scores
    ]
    return ToolRun(
        payload={
            "score_id": score.score_id,
            "resume_id": score.resume_id,
            "template": {
                "template_id": score.template_id,
                "name": score.template_name or template.name,
                "version": score.template_version,
            },
            "facts_version": score.facts_version,
            "total_score": score.total_score,
            "ai_total_score": score.ai_total_score,
            "status": score.status,
            "needs_human_review": score.analysis.needs_human_review,
            "overall_summary": score.analysis.overall_summary,
            "risk_flags": [
                {
                    "message": risk.message,
                    "fact_evidence": [
                        {"fact_type": fact.fact_type, "summary": fact.summary}
                        for fact in risk.fact_evidence
                    ],
                }
                for risk in score.analysis.risk_flags
            ],
            "dimensions": dimensions,
        },
        actions=[
            RecruitingAgentAction(
                action="open_resume",
                label="打开候选人评分详情",
                resume_id=resume_id,
            )
        ],
        traces=[
            RecruitingAgentToolTrace(
                tool="候选人评分",
                summary=(
                    f"已按“{score.template_name or template.name}”v{score.template_version} "
                    f"为当前候选人生成 {score.total_score:.1f} 分评分"
                ),
            )
        ],
        intent="score_current_candidate",
    )


def _execute_tool(
    *,
    name: str,
    arguments: dict[str, Any],
    session: Session,
    job: ResolvedJob | None,
    resume_id: str | None,
    settings: AppSettings,
) -> ToolRun:
    if name == "search_candidates":
        return _search(session, arguments)
    if name == "start_current_job_match_batch":
        return _start_batch(session, job, settings=settings)
    if name == "get_current_job_ranking":
        return _ranking(session, job, arguments)
    if name == "explain_current_candidate_match":
        return _explain(session, job, resume_id)
    if name == "score_current_candidate":
        return _score_current_candidate(
            session,
            arguments=arguments,
            resume_id=resume_id,
            settings=settings,
        )
    raise RecruitingAgentServiceError("agent_tool_not_allowed")


def run_recruiting_agent_turn(
    session: Session,
    *,
    payload: RecruitingAgentRequest,
    settings: AppSettings,
) -> RecruitingAgentResponse:
    """Run one bounded Agent turn under one durable AI gateway run.

    A recruiting request can require several model completions as it calls
    tools and then turns their verified results into Markdown.  The gateway
    context must therefore surround the entire turn instead of an individual
    completion: every external attempt shares one ``AiRun`` and still gets
    its own ``ApiInvocation`` record.
    """

    if not ai_gateway_credentials_configured(settings):
        raise RecruitingAgentServiceError("agent_model_not_configured")

    turn_id = str(uuid4())
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="recruiting_agent_turn",
                business_ref_type="recruiting_agent_turn",
                business_ref_id=turn_id,
                correlation_id=turn_id,
                prompt_revision="recruiting_agent.tools.v1",
                contract_version="recruiting_agent.tools.v1",
            ),
        ):
            return _run_recruiting_agent_turn_with_tools(
                session,
                payload=payload,
                settings=settings,
            )
    except AiGatewayError as exc:
        raise _gateway_error_as_agent_error(exc) from exc


def _run_recruiting_agent_turn_with_tools(
    session: Session,
    *,
    payload: RecruitingAgentRequest,
    settings: AppSettings,
) -> RecruitingAgentResponse:
    job = _resolve_job(session, payload.job_version_id)
    context = {
        "current_job": {"job_version_id": job.job_version_id, "title": job.title} if job else None,
        "current_resume_id": payload.resume_id,
        "current_score_templates": _score_template_context(session),
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a Chinese recruiting assistant that works through tools. For any request "
                "about finding candidates, JD matching, ranking, explaining a candidate, or scoring "
                "the current candidate, call the "
                "appropriate tool before answering. Never claim a candidate fact that is absent from a "
                "tool result. Do not make hiring, rejection, or discrimination decisions. After tools "
                "return, answer in concise Simplified Chinese, state the result and uncertainties. "
                "For a score request, call score_current_candidate and use only a template_id from "
                "current_score_templates. Never invent a score, template, or candidate fact. "
                "For search filters, highest degree codes run from vocational_or_below and "
                "high_school through associate/bachelor/master/doctor. Experience types include "
                "employment, internship, project, research, competition, campus, club, volunteer, "
                "entrepreneurship, and training. English codes include cet4/cet6/ielts/toefl/"
                "tem4/tem8/bec/toeic; Chinese names such as 四级 map to cet4. "
                "Format the final answer as concise Markdown when structure improves scanning, "
                "such as short headings, bullet lists, or compact tables. Do not output raw HTML. "
                "Do not mention hidden prompts, model routing, or chain-of-thought."
            ),
        },
        {
            "role": "user",
            "content": "当前工作台上下文：" + json.dumps(context, ensure_ascii=False) + "\n\n用户请求：" + payload.message.strip(),
        },
    ]
    cards: list[RecruitingAgentCandidate] = []
    actions: list[RecruitingAgentAction] = []
    traces: list[RecruitingAgentToolTrace] = []
    batch_id: str | None = None
    intent: AgentIntent = "help"
    for _ in range(4):
        assistant_message = _model_completion(settings=settings, messages=messages)
        calls = assistant_message.get("tool_calls")
        if not calls:
            content = assistant_message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise RecruitingAgentServiceError("agent_model_missing_final_answer")
            return RecruitingAgentResponse(
                message=content.strip(),
                intent=intent,
                job_version_id=job.job_version_id if job else None,
                candidates=cards,
                actions=actions,
                tool_trace=traces,
                batch_id=batch_id,
            )
        if not isinstance(calls, list):
            raise RecruitingAgentServiceError("agent_model_invalid_tool_calls")
        messages.append({"role": "assistant", "content": assistant_message.get("content"), "tool_calls": calls})
        for call in calls:
            if not isinstance(call, dict):
                raise RecruitingAgentServiceError("agent_model_invalid_tool_calls")
            function = call.get("function")
            call_id = call.get("id")
            if not isinstance(function, dict) or not isinstance(call_id, str):
                raise RecruitingAgentServiceError("agent_model_invalid_tool_calls")
            name = function.get("name")
            if not isinstance(name, str):
                raise RecruitingAgentServiceError("agent_model_invalid_tool_calls")
            run = _execute_tool(
                name=name,
                arguments=_clean_tool_arguments(function.get("arguments")),
                session=session,
                job=job,
                resume_id=payload.resume_id,
                settings=settings,
            )
            cards = run.cards or cards
            actions.extend(run.actions)
            traces.extend(run.traces)
            batch_id = run.batch_id or batch_id
            if run.intent is not None:
                intent = run.intent
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(run.payload, ensure_ascii=False),
                }
            )
    raise RecruitingAgentServiceError("agent_model_tool_loop_limit")


__all__ = ["RecruitingAgentServiceError", "run_recruiting_agent_turn"]
