from __future__ import annotations

import json
import re
import unicodedata
from contextvars import ContextVar
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
from app.services.mailbox_background_job_service import (
    enqueue_all_mailbox_sync_jobs,
    enqueue_mailbox_sync_job,
    list_mailbox_background_jobs,
)
from app.services.mailbox_import_service import (
    MailboxImportError,
    list_mailbox_configs,
    list_mailbox_imports,
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
    "show_mailbox_status",
    "show_mailbox_imports",
    "sync_mailbox",
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


_MAILBOX_NAME_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 32,
}

# Mailbox tools are deliberately separate from the core recruiting tool set.
# The active model tool list is selected per request from a ContextVar so two
# concurrent Agent turns cannot accidentally share an organization plan's
# mailbox entitlement.
_MAILBOX_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_mailbox_status",
            "description": (
                "Read safe status for the current workspace's named resume inboxes. "
                "Use this for questions about configured inboxes, whether a channel is "
                "enabled, when it last synchronized, or whether a sync task is running. "
                "It never exposes email addresses, IMAP hosts, passwords, mail bodies, "
                "or attachment filenames. mailbox_name is optional; omit it to list all "
                "current channels."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"mailbox_name": _MAILBOX_NAME_SCHEMA},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_mailbox_imports",
            "description": (
                "Read a privacy-safe aggregate of recent resume attachment ingestion for "
                "one named inbox or all current inboxes. Use this to explain counts of "
                "imported, failed, skipped, or retryable attachments. Never reveal sender "
                "addresses, mail bodies, authorization codes, or attachment filenames."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mailbox_name": _MAILBOX_NAME_SCHEMA,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enqueue_named_mailbox_sync",
            "description": (
                "Queue a background synchronization for exactly one current named inbox. "
                "Use only after the user explicitly asks to sync that named channel. This "
                "does not connect to IMAP inside the chat request; it only queues durable "
                "worker work. Never use a guessed name."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"mailbox_name": _MAILBOX_NAME_SCHEMA},
                "required": ["mailbox_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enqueue_all_mailbox_syncs",
            "description": (
                "Queue an independent background synchronization for every enabled current "
                "resume inbox. Use only when the user explicitly says to sync all or every "
                "inbox. It only creates durable worker jobs and does not read IMAP in the "
                "chat request."
            ),
            "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        },
    },
]

_ACTIVE_TOOL_DEFINITIONS: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "recruiting_agent_active_tool_definitions",
    default=tuple(_TOOLS),
)


def _tool_definitions(*, mailbox_tools_available: bool) -> tuple[dict[str, Any], ...]:
    if mailbox_tools_available:
        return tuple((*_TOOLS, *_MAILBOX_TOOLS))
    return tuple(_TOOLS)


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
                "tools": list(_ACTIVE_TOOL_DEFINITIONS.get()),
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


_SAFE_MAILBOX_ERROR_MESSAGES: dict[str, str] = {
    "mailbox_not_enabled": "通道已暂停，暂不能同步",
    "mailbox_config_archived": "通道已归档，不能再同步",
    "mailbox_source_epoch_changed": "邮箱来源状态已变化，需要重新绑定通道",
    "mailbox_task_source_changed": "通道配置已变化，已取消本次同步",
    "mailbox_credentials_unavailable": "邮箱授权信息不可用，需要在邮箱入库页面更新",
    "mailbox_credentials_key_invalid": "邮箱授权信息不可用，需要在邮箱入库页面更新",
    "mailbox_sync_in_progress": "该通道正在同步",
    "mailbox_connection_failed": "邮箱连接或授权异常",
    "mailbox_login_failed": "邮箱连接或授权异常",
    "imap_connection_failed": "邮箱连接或授权异常",
    "imap_login_failed": "邮箱连接或授权异常",
    "attachment_validation_failed": "附件校验失败",
    "attachment_message_unavailable": "原邮件或附件暂时无法读取",
    "attachment_source_changed": "附件来源已变化，不能安全重试",
    "attachment_source_unavailable": "原邮件附件已不可获取",
    "unsupported_document_type": "附件格式不受支持",
    "document_text_extraction_failed": "附件文本提取异常",
}


def _safe_mailbox_error_summary(value: str | None) -> str | None:
    """Collapse service codes to safe, recruiter-facing categories.

    Mailbox services normally store stable error codes, but tool payloads must
    remain safe even if a provider implementation adds a new exception text.
    The Agent never receives a raw IMAP/provider message.
    """

    if not value:
        return None
    if value in _SAFE_MAILBOX_ERROR_MESSAGES:
        return _SAFE_MAILBOX_ERROR_MESSAGES[value]
    if value.startswith(("mailbox_", "imap_")):
        return "邮箱同步异常，请在邮箱附件入库页面查看处理状态"
    if value.startswith(("attachment_", "document_", "office_", "ocr_")):
        return "附件入库或解析异常，请在邮箱附件入库页面查看处理状态"
    return "同步或入库异常，请在邮箱附件入库页面查看处理状态"


def _normalized_mailbox_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


_SYNC_ACTION_PATTERN = r"(?:同步|拉取|刷新|收取)"
_ENGLISH_SYNC_ACTION_ALTERNATION = r"sync|pull|refresh|fetch|collect"
_ENGLISH_SYNC_ACTION_PATTERN = rf"(?:{_ENGLISH_SYNC_ACTION_ALTERNATION})"
_SYNC_COMMAND_LEADING_PATTERN = (
    r"(?:(?:麻烦你帮我|麻烦帮我|请帮我|请帮忙|劳烦你|麻烦你|帮我|帮忙|"
    r"劳烦|麻烦|请你|请|给我|替我|重新|先|只|继续|再|现在|立即|马上|立刻)\s*)*"
)
_ENGLISH_SYNC_COMMAND_LEADING_PATTERN = (
    r"(?:(?:please|kindly|help\s+me|again|first|continue|now)\s+)*"
)
_ENGLISH_SYNC_POLITE_QUESTION_LEADING_PATTERN = (
    r"(?:(?:could|would|can)\s+you\s+)(?:please\s+)?"
)
_SYNC_COMMAND_END_PATTERN = r"[。.!！]*"
_SYNC_TARGET_FIRST_ACTION_SUFFIX_PATTERN = r"(?:\s*(?:一下|下))?[。.!！]*"
_SYNC_CLAUSE_SEPARATOR = re.compile(
    r"\s*(?:[,，;；。.!！]\s*|\bbut\b|\bhowever\b|但是|不过|但)\s*"
)
_NEGATIVE_SYNC_POLARITY_PATTERN = re.compile(
    rf"(?:不要|不用|无需|不需要|不必|暂不|先不|不再|先别|别再|别|勿|停止|"
    rf"暂停|取消|撤销|终止|不能|无法|未|没|不)\s*"
    rf"(?:(?:继续|再|帮我|给我|替我|马上|立即|现在|对|的)\s*)*{_SYNC_ACTION_PATTERN}"
    rf"|{_SYNC_ACTION_PATTERN}.*(?:停止|暂停|取消|撤销|终止)"
)
_TARGET_NON_COMMAND_STATE_PATTERN = re.compile(
    r"(?:已完成|完成了|完成|需要多久|多久|多长时间|不行|超时|正在进行|进行中|"
    r"正在|卡住|没反应|失败|报错|出错|异常|错误|没成功|未完成|不成功)"
)
_ENGLISH_NEGATIVE_SYNC_POLARITY_PATTERN = re.compile(
    rf"\b(?:do\s+not|don't|dont|not|never|no|stop|cancel|pause)\b.*"
    rf"\b(?:{_ENGLISH_SYNC_ACTION_ALTERNATION})\b"
    rf"|\b(?:{_ENGLISH_SYNC_ACTION_ALTERNATION})\b.*\b(?:cancelled|canceled|stopped|paused)\b"
)
_ENGLISH_TARGET_NON_COMMAND_STATE_PATTERN = re.compile(
    r"\b(?:not|failed|failure|errors?|issues?|completed|complete|done|how\s+long|"
    r"timeouts?|timed\s+out|running|in\s+progress|stuck|not\s+working|no\s+response|"
    r"status|progress|reason|why|how)\b"
)
_TARGET_EXCLUSION_PATTERN = re.compile(
    r"(?:除了|除外|不含|排除)|\b(?:except|unless|excluding?|without|skip|omit)\b"
)
_SAFE_SYNC_FOLLOWUP_PATTERN = re.compile(
    r"^(?:完成后(?:告诉我(?:结果)?|通知我(?:结果)?))$"
)
_SYNC_TARGET_SENTINEL = "\ue000agent_mailbox_target\ue001"
_ALL_MAILBOX_TARGET_PATTERN = (
    r"(?:(?:全部|所有|全量)(?:已启用的|启用的)?"
    r"(?:收件邮箱|邮箱|收件箱|收件通道|邮箱通道|通道)"
    r"|(?:all|every)\s+(?:enabled\s+)?(?:mailboxes?|inboxes?))"
)
_ALL_MAILBOX_EXCLUSION_PATTERN = re.compile(
    r"(?:除了|除外|不含|排除|仅限|只|仅)|\b(?:except|unless|only)\b"
)


def _sync_clauses(normalized_message: str) -> list[str]:
    """Split only at strong boundaries so polarity remains target-local."""

    return [
        clause.strip()
        for clause in _SYNC_CLAUSE_SEPARATOR.split(normalized_message)
        if clause.strip()
    ]


def _clause_negates_sync_target(
    clause: str,
    *,
    target_pattern: re.Pattern[str],
) -> bool:
    if target_pattern.search(clause) is None:
        return False
    clause_without_target = target_pattern.sub("", clause)
    return bool(
        _NEGATIVE_SYNC_POLARITY_PATTERN.search(clause_without_target)
        or _TARGET_NON_COMMAND_STATE_PATTERN.search(clause_without_target)
        or _ENGLISH_NEGATIVE_SYNC_POLARITY_PATTERN.search(clause_without_target)
        or _ENGLISH_TARGET_NON_COMMAND_STATE_PATTERN.search(clause_without_target)
        or _TARGET_EXCLUSION_PATTERN.search(clause_without_target)
    )


def _clause_is_explicit_sync_command(
    clause: str,
    *,
    target_regex: str,
) -> bool:
    """Match a deliberately small positive-command grammar around one target."""

    chinese_action_first = re.compile(
        rf"^{_SYNC_COMMAND_LEADING_PATTERN}{_SYNC_ACTION_PATTERN}\s*"
        rf"{target_regex}{_SYNC_COMMAND_END_PATTERN}$"
    )
    chinese_target_first = re.compile(
        rf"^{_SYNC_COMMAND_LEADING_PATTERN}(?:把|将)\s*{target_regex}\s*"
        rf"(?:(?:重新|继续|再|先)\s*)?{_SYNC_ACTION_PATTERN}"
        rf"{_SYNC_TARGET_FIRST_ACTION_SUFFIX_PATTERN}$"
    )
    english_action_first = re.compile(
        rf"^{_ENGLISH_SYNC_COMMAND_LEADING_PATTERN}{_ENGLISH_SYNC_ACTION_PATTERN}\s+"
        rf"{target_regex}{_SYNC_COMMAND_END_PATTERN}$"
    )
    english_polite_question = re.compile(
        rf"^{_ENGLISH_SYNC_POLITE_QUESTION_LEADING_PATTERN}"
        rf"{_ENGLISH_SYNC_ACTION_PATTERN}\s+{target_regex}"
        rf"\s*[?？]$"
    )
    return any(
        pattern.fullmatch(clause) is not None
        for pattern in (
            chinese_action_first,
            chinese_target_first,
            english_action_first,
            english_polite_question,
        )
    )


def _explicitly_requests_target_sync(message: str, *, target_regex: str) -> bool:
    normalized_message = _normalized_mailbox_name(message)
    clauses = _sync_clauses(normalized_message)
    target_pattern = re.compile(target_regex)
    command_seen = False
    followup_seen = False
    for clause in clauses:
        if target_pattern.search(clause) is not None:
            if command_seen or followup_seen:
                return False
            if _clause_negates_sync_target(clause, target_pattern=target_pattern):
                return False
            if not _clause_is_explicit_sync_command(
                clause,
                target_regex=target_regex,
            ):
                return False
            command_seen = True
            continue
        if not command_seen or not _SAFE_SYNC_FOLLOWUP_PATTERN.fullmatch(clause):
            return False
        followup_seen = True
    return command_seen


def _explicitly_requests_named_mailbox_sync(message: str, mailbox_name: str) -> bool:
    normalized_name = _normalized_mailbox_name(mailbox_name)
    if not normalized_name:
        return False
    return _explicitly_requests_target_sync(
        message,
        target_regex=re.escape(normalized_name),
    )


def _explicitly_requests_disambiguated_named_mailbox_sync(
    message: str,
    mailbox_name: str,
) -> bool:
    """Authorize a literal target after workspace-level name disambiguation."""

    normalized_message = _normalized_mailbox_name(message)
    normalized_name = _normalized_mailbox_name(mailbox_name)
    if (
        not normalized_name
        or normalized_name not in normalized_message
        or _SYNC_TARGET_SENTINEL in normalized_message
    ):
        return False
    protected_message = normalized_message.replace(
        normalized_name,
        _SYNC_TARGET_SENTINEL,
    )
    return _explicitly_requests_target_sync(
        protected_message,
        target_regex=re.escape(_SYNC_TARGET_SENTINEL),
    )


def _explicitly_requests_all_mailbox_sync(message: str) -> bool:
    normalized_message = _normalized_mailbox_name(message)
    if _ALL_MAILBOX_EXCLUSION_PATTERN.search(normalized_message):
        return False
    if any(
        _NEGATIVE_SYNC_POLARITY_PATTERN.search(clause)
        or _ENGLISH_NEGATIVE_SYNC_POLARITY_PATTERN.search(clause)
        for clause in _sync_clauses(normalized_message)
    ):
        return False
    return _explicitly_requests_target_sync(
        normalized_message,
        target_regex=_ALL_MAILBOX_TARGET_PATTERN,
    )


def _agent_mailbox_context(session: Session) -> list[dict[str, object]]:
    """Expose only names and safe lifecycle state to the model."""

    try:
        configs = list_mailbox_configs(session).items
    except MailboxImportError:
        # A status query will return a controlled error if the service remains
        # unavailable. Do not make context construction fail an entire turn.
        return []
    return [
        {
            "mailbox_name": item.display_name,
            "enabled": item.enabled,
            "archived": item.archived_at is not None,
            "last_synced_at": item.last_synced_at.isoformat() if item.last_synced_at else None,
            "last_sync_issue": _safe_mailbox_error_summary(item.last_sync_error),
        }
        for item in configs
        if item.display_name
    ]


def _agent_mailbox_by_name(
    configs: list[Any],
    mailbox_name: object,
) -> Any | None:
    if not isinstance(mailbox_name, str):
        return None
    normalized = _normalized_mailbox_name(mailbox_name)
    if not normalized:
        return None
    return next(
        (
            item
            for item in configs
            if item.display_name
            and _normalized_mailbox_name(item.display_name) == normalized
        ),
        None,
    )


def _mailbox_configs_named_in_message(
    message: str,
    configs: list[Any],
) -> list[Any]:
    """Return workspace configs whose normalized display name occurs literally."""

    normalized_message = _normalized_mailbox_name(message)
    matches: list[tuple[int, Any]] = []
    for config in configs:
        display_name = getattr(config, "display_name", None)
        if not isinstance(display_name, str):
            continue
        normalized_name = _normalized_mailbox_name(display_name)
        if normalized_name and normalized_name in normalized_message:
            matches.append((len(normalized_name), config))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [config for _, config in matches]


def _unique_longest_mailbox_literal_match(
    message: str,
    configs: list[Any],
) -> Any | None:
    """Resolve one unique longest literal config name, or fail closed."""

    matches = _mailbox_configs_named_in_message(message, configs)
    if not matches:
        return None
    longest_length = len(_normalized_mailbox_name(matches[0].display_name))
    longest_matches = [
        config
        for config in matches
        if len(_normalized_mailbox_name(config.display_name)) == longest_length
    ]
    return longest_matches[0] if len(longest_matches) == 1 else None


def _safe_sync_job_payload(job: Any | None) -> dict[str, object] | None:
    if job is None:
        return None
    return {
        "status": job.status,
        "trigger_type": job.trigger_type,
        "imported_count": job.imported_count,
        "duplicate_count": job.duplicate_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "issue": _safe_mailbox_error_summary(job.last_error),
        "requested_at": job.requested_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _mailbox_status_rows(session: Session, configs: list[Any]) -> list[dict[str, object]]:
    """Build a compact, privacy-safe operational projection per channel."""

    jobs = list_mailbox_background_jobs(session, limit=100).items
    imports = list_mailbox_imports(session, limit=100).items
    rows: list[dict[str, object]] = []
    for config in configs:
        if not config.mailbox_id or not config.display_name:
            continue
        sync_jobs = [
            item
            for item in jobs
            if item.mailbox_id == config.mailbox_id and item.job_kind == "sync"
        ]
        active_job = next(
            (item for item in sync_jobs if item.status in {"queued", "running"}),
            None,
        )
        latest_job = active_job or (sync_jobs[0] if sync_jobs else None)
        channel_imports = [
            item for item in imports if item.mailbox_config_id == config.mailbox_id
        ]
        recent_import_counts = {
            status: sum(item.status == status for item in channel_imports)
            for status in ("imported", "failed", "retrying", "skipped")
        }
        rows.append(
            {
                "mailbox_name": config.display_name,
                "enabled": config.enabled,
                "last_synced_at": (
                    config.last_synced_at.isoformat() if config.last_synced_at else None
                ),
                "last_sync_issue": _safe_mailbox_error_summary(config.last_sync_error),
                "active_sync": _safe_sync_job_payload(active_job),
                "latest_sync": _safe_sync_job_payload(latest_job),
                "recent_attachment_counts": recent_import_counts,
                "recent_retryable_failure_count": sum(
                    item.status == "failed" and item.can_retry for item in channel_imports
                ),
            }
        )
    return rows


def _mailbox_tool_error(
    *,
    intent: AgentIntent,
    tool: str,
    message: str,
) -> ToolRun:
    return ToolRun(
        payload={"error": message},
        traces=[RecruitingAgentToolTrace(tool=tool, summary=message)],
        intent=intent,
    )


def _mailbox_tools_unavailable(*, intent: AgentIntent) -> ToolRun:
    return _mailbox_tool_error(
        intent=intent,
        tool="收件邮箱",
        message="当前账号没有管理收件邮箱的权限或套餐未开通该功能，未读取或同步任何邮箱。",
    )


def _get_mailbox_status(session: Session, arguments: dict[str, Any]) -> ToolRun:
    if not set(arguments).issubset({"mailbox_name"}):
        return _mailbox_tool_error(
            intent="show_mailbox_status",
            tool="收件邮箱状态",
            message="邮箱状态查询参数无效，未读取邮箱。",
        )
    try:
        configs = list_mailbox_configs(session).items
    except MailboxImportError:
        return _mailbox_tool_error(
            intent="show_mailbox_status",
            tool="收件邮箱状态",
            message="暂时无法读取收件邮箱状态，请稍后重试。",
        )
    requested_name = arguments.get("mailbox_name")
    if requested_name is not None:
        selected = _agent_mailbox_by_name(configs, requested_name)
        if selected is None:
            return _mailbox_tool_error(
                intent="show_mailbox_status",
                tool="收件邮箱状态",
                message="未找到该收件通道，未读取其他邮箱。",
            )
        configs = [selected]
    try:
        rows = _mailbox_status_rows(session, configs)
    except MailboxImportError:
        return _mailbox_tool_error(
            intent="show_mailbox_status",
            tool="收件邮箱状态",
            message="暂时无法读取收件邮箱状态，请稍后重试。",
        )
    if not rows:
        message = "当前工作区尚未配置可查询的收件邮箱。"
    else:
        message = f"已读取 {len(rows)} 个收件通道的安全状态。"
    return ToolRun(
        payload={"mailboxes": rows, "message": message},
        actions=[
            RecruitingAgentAction(
                action="open_mailbox_workspace",
                label="打开邮箱附件入库",
            )
        ],
        traces=[RecruitingAgentToolTrace(tool="收件邮箱状态", summary=message)],
        intent="show_mailbox_status",
    )


def _get_recent_mailbox_imports(session: Session, arguments: dict[str, Any]) -> ToolRun:
    if not set(arguments).issubset({"mailbox_name", "limit"}):
        return _mailbox_tool_error(
            intent="show_mailbox_imports",
            tool="附件入库状态",
            message="附件入库查询参数无效，未读取邮箱记录。",
        )
    raw_limit = arguments.get("limit", 20)
    limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 20
    limit = min(max(limit, 1), 20)
    try:
        configs = list_mailbox_configs(session).items
    except MailboxImportError:
        return _mailbox_tool_error(
            intent="show_mailbox_imports",
            tool="附件入库状态",
            message="暂时无法读取附件入库状态，请稍后重试。",
        )
    requested_name = arguments.get("mailbox_name")
    selected = None
    if requested_name is not None:
        selected = _agent_mailbox_by_name(configs, requested_name)
        if selected is None:
            return _mailbox_tool_error(
                intent="show_mailbox_imports",
                tool="附件入库状态",
                message="未找到该收件通道，未读取其他邮箱记录。",
            )
    try:
        history = list_mailbox_imports(
            session,
            limit=limit,
            mailbox_config_id=selected.mailbox_id if selected is not None else None,
        )
    except MailboxImportError:
        return _mailbox_tool_error(
            intent="show_mailbox_imports",
            tool="附件入库状态",
            message="暂时无法读取附件入库状态，请稍后重试。",
        )
    names_by_id = {
        item.mailbox_id: item.display_name
        for item in configs
        if item.mailbox_id and item.display_name
    }
    grouped: dict[str, list[Any]] = {}
    for item in history.items:
        # Keep this Agent view focused on currently configured channels. An
        # archived source remains available in the dedicated inbox audit UI,
        # but should not surface as an anonymous historical bucket in chat.
        if item.mailbox_config_id not in names_by_id:
            continue
        grouped.setdefault(item.mailbox_config_id, []).append(item)
    rows: list[dict[str, object]] = []
    for mailbox_id, items in grouped.items():
        status_counts = {
            status: sum(item.status == status for item in items)
            for status in ("imported", "failed", "retrying", "skipped")
        }
        failure_categories: dict[str, int] = {}
        for item in items:
            if item.status != "failed":
                continue
            category = _safe_mailbox_error_summary(item.error) or "附件入库异常"
            failure_categories[category] = failure_categories.get(category, 0) + 1
        rows.append(
            {
                "mailbox_name": names_by_id.get(mailbox_id, "当前收件通道"),
                "recent_record_count": len(items),
                "status_counts": status_counts,
                "retryable_failure_count": sum(
                    item.status == "failed" and item.can_retry for item in items
                ),
                "failure_categories": failure_categories,
            }
        )
    if selected is not None and not rows:
        rows.append(
            {
                "mailbox_name": selected.display_name,
                "recent_record_count": 0,
                "status_counts": {"imported": 0, "failed": 0, "retrying": 0, "skipped": 0},
                "retryable_failure_count": 0,
                "failure_categories": {},
            }
        )
    message = "已读取最近附件入库的汇总状态。" if rows else "当前没有可查询的附件入库记录。"
    return ToolRun(
        payload={
            "mailboxes": rows,
            "history_total": sum(len(items) for items in grouped.values()),
            "message": message,
        },
        actions=[
            RecruitingAgentAction(
                action="open_mailbox_workspace",
                label="打开邮箱附件入库",
            )
        ],
        traces=[RecruitingAgentToolTrace(tool="附件入库状态", summary=message)],
        intent="show_mailbox_imports",
    )


def _enqueue_named_mailbox_sync(
    session: Session,
    *,
    arguments: dict[str, Any],
    settings: AppSettings,
    user_message: str,
) -> ToolRun:
    if set(arguments) != {"mailbox_name"}:
        return _mailbox_tool_error(
            intent="sync_mailbox",
            tool="收件邮箱同步",
            message="指定邮箱同步参数无效，未创建同步任务。",
        )
    try:
        active_configs = list_mailbox_configs(session).items
        authorization_configs = list_mailbox_configs(
            session,
            include_archived=True,
        ).items
        config = _agent_mailbox_by_name(
            active_configs,
            arguments.get("mailbox_name"),
        )
        authorization_config = _agent_mailbox_by_name(
            authorization_configs,
            arguments.get("mailbox_name"),
        )
        if config is None and authorization_config is not None:
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message="该收件通道已归档，未创建同步任务。",
            )
        if config is None or not config.mailbox_id or not config.display_name:
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message="未找到该收件通道，未创建同步任务。",
            )
        literal_matches = _mailbox_configs_named_in_message(
            user_message,
            authorization_configs,
        )
        if not literal_matches:
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message="请明确指定要同步的收件通道，未创建同步任务。",
            )
        literal_target = _unique_longest_mailbox_literal_match(
            user_message,
            authorization_configs,
        )
        if literal_target is None or literal_target.mailbox_id != config.mailbox_id:
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message="请明确复述要同步的唯一收件通道名称，未创建同步任务。",
            )
        if _explicitly_requests_all_mailbox_sync(user_message):
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message=(
                    "检测到指定收件通道名称与全部邮箱指令有歧义，"
                    "请明确复述要同步全部邮箱还是指定邮箱，未创建同步任务。"
                ),
            )
        if not _explicitly_requests_disambiguated_named_mailbox_sync(
            user_message,
            config.display_name,
        ):
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="收件邮箱同步",
                message="请明确指定要同步的收件通道，未创建同步任务。",
            )
        job = enqueue_mailbox_sync_job(
            session,
            settings=settings,
            mailbox_config_id=config.mailbox_id,
        )
    except MailboxImportError as exc:
        return _mailbox_tool_error(
            intent="sync_mailbox",
            tool="收件邮箱同步",
            message=_safe_mailbox_error_summary(str(exc)) or "未能创建同步任务。",
        )
    message = (
        f"“{config.display_name}”已有同步任务，未重复创建。"
        if job.deduplicated
        else f"已为“{config.display_name}”创建后台同步任务。"
    )
    return ToolRun(
        payload={
            "mailbox_name": config.display_name,
            "queued": not job.deduplicated,
            "deduplicated": job.deduplicated,
            "job": _safe_sync_job_payload(job),
            "message": message,
        },
        actions=[
            RecruitingAgentAction(
                action="open_mailbox_workspace",
                label="查看邮箱同步进度",
            )
        ],
        traces=[RecruitingAgentToolTrace(tool="收件邮箱同步", summary=message)],
        intent="sync_mailbox",
    )


def _enqueue_all_mailbox_syncs(
    session: Session,
    *,
    settings: AppSettings,
    user_message: str,
) -> ToolRun:
    try:
        active_configs = list_mailbox_configs(session).items
        authorization_configs = list_mailbox_configs(
            session,
            include_archived=True,
        ).items
        if not _explicitly_requests_all_mailbox_sync(user_message):
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="全部收件邮箱同步",
                message="请明确说明要同步全部收件邮箱，未创建同步任务。",
            )
        if _mailbox_configs_named_in_message(
            user_message,
            authorization_configs,
        ):
            return _mailbox_tool_error(
                intent="sync_mailbox",
                tool="全部收件邮箱同步",
                message=(
                    "检测到收件通道名称与全部邮箱指令有歧义，"
                    "请明确复述要同步全部邮箱还是指定邮箱，未创建同步任务。"
                ),
            )
        enabled_count = sum(item.enabled for item in active_configs)
        jobs = enqueue_all_mailbox_sync_jobs(session, settings=settings)
    except MailboxImportError as exc:
        return _mailbox_tool_error(
            intent="sync_mailbox",
            tool="全部收件邮箱同步",
            message=_safe_mailbox_error_summary(str(exc)) or "未能创建全部邮箱同步任务。",
        )
    if enabled_count == 0:
        message = "当前没有启用的收件邮箱，未创建同步任务。"
    elif jobs.queued_count:
        message = f"已为 {jobs.queued_count} 个收件通道创建后台同步任务。"
    else:
        message = f"{jobs.deduplicated_count} 个收件通道已有同步任务，未重复创建。"
    return ToolRun(
        payload={
            "enabled_mailbox_count": enabled_count,
            "queued_count": jobs.queued_count,
            "deduplicated_count": jobs.deduplicated_count,
            "message": message,
        },
        actions=[
            RecruitingAgentAction(
                action="open_mailbox_workspace",
                label="查看邮箱同步进度",
            )
        ],
        traces=[RecruitingAgentToolTrace(tool="全部收件邮箱同步", summary=message)],
        intent="sync_mailbox",
    )


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


def _recoverable_tool_call_error(exc: RecruitingAgentServiceError) -> ToolRun | None:
    """Turn model formatting mistakes into a no-op tool result.

    A malformed function call is a provider output problem, not a recruiter
    outage.  Keep the bounded tool loop alive so the model can give a useful
    final answer, while ensuring no data read or side effect took place.
    """

    code = str(exc)
    if code in {
        "agent_tool_arguments_missing",
        "agent_tool_arguments_invalid",
        "agent_search_arguments_invalid",
    }:
        message = "工具调用参数无法识别，未执行任何操作。"
    elif code == "agent_tool_not_allowed":
        message = "请求的工具不可用，未执行任何操作。"
    else:
        return None
    return ToolRun(
        payload={"error": message},
        traces=[RecruitingAgentToolTrace(tool="Agent 工具", summary=message)],
    )


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
    mailbox_tools_available: bool,
    user_message: str,
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
    if name == "get_mailbox_status":
        return (
            _get_mailbox_status(session, arguments)
            if mailbox_tools_available
            else _mailbox_tools_unavailable(intent="show_mailbox_status")
        )
    if name == "get_recent_mailbox_imports":
        return (
            _get_recent_mailbox_imports(session, arguments)
            if mailbox_tools_available
            else _mailbox_tools_unavailable(intent="show_mailbox_imports")
        )
    if name == "enqueue_named_mailbox_sync":
        return (
            _enqueue_named_mailbox_sync(
                session,
                arguments=arguments,
                settings=settings,
                user_message=user_message,
            )
            if mailbox_tools_available
            else _mailbox_tools_unavailable(intent="sync_mailbox")
        )
    if name == "enqueue_all_mailbox_syncs":
        return (
            _enqueue_all_mailbox_syncs(
                session,
                settings=settings,
                user_message=user_message,
            )
            if not arguments and mailbox_tools_available
            else (
                _mailbox_tools_unavailable(intent="sync_mailbox")
                if not mailbox_tools_available
                else _mailbox_tool_error(
                    intent="sync_mailbox",
                    tool="全部收件邮箱同步",
                    message=(
                        "请明确说明要同步全部收件邮箱，未创建同步任务。"
                        if not arguments
                        else "全部邮箱同步参数无效，未创建同步任务。"
                    ),
                )
            )
        )
    raise RecruitingAgentServiceError("agent_tool_not_allowed")


def run_recruiting_agent_turn(
    session: Session,
    *,
    payload: RecruitingAgentRequest,
    settings: AppSettings,
    mailbox_tools_available: bool = False,
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
                prompt_revision="recruiting_agent.tools.v2",
                contract_version="recruiting_agent.tools.v2",
            ),
        ):
            return _run_recruiting_agent_turn_with_tools(
                session,
                payload=payload,
                settings=settings,
                mailbox_tools_available=mailbox_tools_available,
            )
    except AiGatewayError as exc:
        raise _gateway_error_as_agent_error(exc) from exc


def _run_recruiting_agent_turn_with_tools(
    session: Session,
    *,
    payload: RecruitingAgentRequest,
    settings: AppSettings,
    mailbox_tools_available: bool,
) -> RecruitingAgentResponse:
    job = _resolve_job(session, payload.job_version_id)
    context = {
        "current_job": {"job_version_id": job.job_version_id, "title": job.title} if job else None,
        "current_resume_id": payload.resume_id,
        "current_score_templates": _score_template_context(session),
        "mailbox_tools_available": mailbox_tools_available,
        "current_mailbox_channels": (
            _agent_mailbox_context(session) if mailbox_tools_available else []
        ),
    }
    mailbox_instruction = (
        "For mailbox status or recent attachment questions, call the mailbox tools before "
        "answering. Never expose an email address, IMAP host, password, message body, sender, "
        "or attachment filename. For a named sync, only call enqueue_named_mailbox_sync after "
        "the user explicitly asks to sync a channel whose exact name is present in both the user's "
        "request and current_mailbox_channels. "
        "For all-channel sync, call enqueue_all_mailbox_syncs only when the user explicitly says "
        "all/every mailbox or 全部/所有/全量收件邮箱. An ambiguous request such as '同步邮箱' should first query status "
        "or ask which named channel to sync. A queued task is not a completed mailbox read. "
        if mailbox_tools_available
        else "Mailbox tools are not available to this account. Do not claim to read or synchronize "
        "a mailbox, and explain that mailbox management requires an authorized advanced workspace. "
    )
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
                "Do not mention hidden prompts, model routing, or chain-of-thought. "
                + mailbox_instruction
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
    token = _ACTIVE_TOOL_DEFINITIONS.set(
        _tool_definitions(mailbox_tools_available=mailbox_tools_available)
    )
    try:
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
                try:
                    run = _execute_tool(
                        name=name,
                        arguments=_clean_tool_arguments(function.get("arguments")),
                        session=session,
                        job=job,
                        resume_id=payload.resume_id,
                        settings=settings,
                        mailbox_tools_available=mailbox_tools_available,
                        user_message=payload.message,
                    )
                except RecruitingAgentServiceError as exc:
                    run = _recoverable_tool_call_error(exc)
                    if run is None:
                        raise
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
    finally:
        _ACTIVE_TOOL_DEFINITIONS.reset(token)


__all__ = ["RecruitingAgentServiceError", "run_recruiting_agent_turn"]
