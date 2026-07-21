from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.models import (
    Resume,
    ResumeFactSnapshot,
    ResumeReviewAction,
    ResumeScore,
    ScoreTemplate,
    ScoreTemplateDimension,
)
from app.schemas import (
    ResumeScoreAnalysisResponse,
    ResumeScoreAuditEntry,
    ResumeScoreCreate,
    ResumeScoreDimensionResponse,
    ResumeScoreFactEvidence,
    ResumeScoreManualAdjustment,
    ResumeScoreOverride,
    ResumeScoreResponse,
    ResumeScoreRiskFlag,
    ScoreDimensionInput,
    ScoreTemplateCreate,
    ScoreTemplateResponse,
)
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    score_resume_fact_snapshot,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
)
from app.services.resume_eligibility import has_unreliable_source_text


class ScoreServiceError(RuntimeError):
    pass


class ScoreTemplateNotFoundError(ScoreServiceError):
    pass


class ResumeScoreNotFoundError(ScoreServiceError):
    pass


def _dimension_input(dimension: ScoreTemplateDimension) -> ScoreDimensionInput:
    return ScoreDimensionInput(
        key=dimension.key,
        label=dimension.label,
        weight=dimension.weight,
        guidance=dimension.guidance,
    )


def _template_response(template: ScoreTemplate) -> ScoreTemplateResponse:
    return ScoreTemplateResponse(
        template_id=template.id,
        name=template.name,
        description=template.description,
        version=template.version,
        dimensions=[
            _dimension_input(dimension)
            for dimension in sorted(template.dimensions, key=lambda item: item.sort_order)
        ],
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _score_fact_summary(*, fact_type: str, entry: Mapping[str, object]) -> str:
    """Build a concise, structured fact label without exposing raw PDF text."""

    def text(key: str) -> str | None:
        return _optional_string(entry.get(key))

    if fact_type == "education":
        values = [text("school_name_raw"), text("degree"), text("major_raw")]
    elif fact_type == "experience":
        values = [
            text("experience_type"),
            text("organization_name_raw"),
            text("title_raw"),
            text("experience_name_raw"),
        ]
    else:
        values = [text("skill_display")]
    summary = " · ".join(value for value in values if value)
    return summary or "已提取简历事实"


def _fact_evidence_by_id(score: ResumeScore) -> dict[str, ResumeScoreFactEvidence]:
    """Resolve score citations using the exact immutable snapshot scored by AI.

    This is intentionally a small projection of persisted facts.  It gives the
    UI enough context to explain a score while preserving the score's original
    facts version even after a resume is updated.
    """

    snapshot = score.fact_snapshot
    if snapshot is None:
        return {}
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    evidence: dict[str, ResumeScoreFactEvidence] = {}
    categories: tuple[
        tuple[str, Literal["education", "experience", "skill"]], ...
    ] = (
        ("education", "education"),
        ("experiences", "experience"),
        ("skills", "skill"),
    )
    for field, fact_type in categories:
        entries = payload.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fact_id = _optional_string(entry.get("fact_id"))
            if fact_id is None:
                continue
            evidence[fact_id] = ResumeScoreFactEvidence(
                fact_id=fact_id,
                fact_type=fact_type,
                summary=_score_fact_summary(fact_type=fact_type, entry=entry),
                evidence_block_ids=_string_list(entry.get("evidence_block_ids")),
            )
    return evidence


def _score_audit_entries(
    session: Session,
    *,
    score: ResumeScore,
) -> list[ResumeScoreAuditEntry]:
    """Return all manual score edits for one score in chronological order."""

    actions = session.scalars(
        select(ResumeReviewAction)
        .where(
            ResumeReviewAction.resume_id == score.resume_id,
            ResumeReviewAction.action == "score_dimension_overridden",
        )
        .order_by(ResumeReviewAction.created_at.asc(), ResumeReviewAction.id.asc())
    ).all()
    entries: list[ResumeScoreAuditEntry] = []
    for action in actions:
        old_values = action.old_values if isinstance(action.old_values, dict) else {}
        new_values = action.new_values if isinstance(action.new_values, dict) else {}
        # ResumeReviewAction is shared with fact review events, so its
        # score-specific ownership is encoded in the immutable score ID.
        if new_values.get("score_id") != score.id:
            continue
        ai_raw_score = _optional_float(new_values.get("ai_raw_score"))
        if ai_raw_score is None:
            ai_raw_score = _optional_float(old_values.get("ai_raw_score"))
        facts_version = _optional_int(new_values.get("facts_version"))
        if facts_version is None:
            facts_version = score.facts_version
        template_version = _optional_int(new_values.get("template_version"))
        if template_version is None:
            template_version = score.template_version
        entries.append(
            ResumeScoreAuditEntry(
                audit_id=action.id,
                action=action.action,
                actor=action.actor,
                reason=action.note,
                dimension_key=_optional_string(new_values.get("dimension_key")),
                ai_raw_score=ai_raw_score,
                previous_final_raw_score=_optional_float(
                    old_values.get("final_raw_score")
                ),
                final_raw_score=_optional_float(new_values.get("final_raw_score")),
                facts_version=facts_version,
                template_version=template_version,
                created_at=action.created_at.isoformat(),
            )
        )
    return entries


def _dimension_response(
    record: Mapping[str, object],
    *,
    fact_evidence_by_id: Mapping[str, ResumeScoreFactEvidence],
    latest_audit_by_dimension: Mapping[str, ResumeScoreAuditEntry],
) -> ResumeScoreDimensionResponse:
    key = _optional_string(record.get("key")) or "unknown"
    label = _optional_string(record.get("label")) or key
    weight = _optional_int(record.get("weight")) or 0
    ai_raw_score = _optional_float(record.get("ai_raw_score")) or 0.0
    final_raw_score = _optional_float(record.get("final_raw_score"))
    if final_raw_score is None:
        final_raw_score = ai_raw_score
    ai_weighted_score = round(ai_raw_score / 100 * weight, 4)
    final_weighted_score = round(final_raw_score / 100 * weight, 4)
    fact_ids = _string_list(record.get("fact_ids"))
    fact_evidence = [
        fact_evidence_by_id[fact_id]
        for fact_id in fact_ids
        if fact_id in fact_evidence_by_id
    ]
    manual_reason = _optional_string(record.get("manual_reason"))
    adjusted_at = _optional_string(record.get("adjusted_at"))
    latest_audit = latest_audit_by_dimension.get(key)
    manual_adjustment = None
    if manual_reason is not None:
        manual_adjustment = ResumeScoreManualAdjustment(
            raw_score=final_raw_score,
            reason=manual_reason,
            actor=(
                _optional_string(record.get("manual_actor"))
                or (latest_audit.actor if latest_audit else "single_admin")
            ),
            adjusted_at=(
                adjusted_at
                or (latest_audit.created_at if latest_audit is not None else "")
            ),
        )
    return ResumeScoreDimensionResponse(
        key=key,
        label=label,
        weight=weight,
        ai_raw_score=ai_raw_score,
        final_raw_score=final_raw_score,
        weighted_score=final_weighted_score,
        ai_weighted_score=ai_weighted_score,
        final_weighted_score=final_weighted_score,
        rationale=_optional_string(record.get("rationale")) or "信息不足",
        fact_ids=fact_ids,
        fact_evidence=fact_evidence,
        evidence_state=("grounded" if fact_evidence else "insufficient_information"),
        uncertainties=_string_list(record.get("uncertainties")),
        manual_reason=manual_reason,
        adjusted_at=adjusted_at,
        manual_adjustment=manual_adjustment,
    )


def _analysis_response(
    raw_analysis: object,
    *,
    fact_evidence_by_id: Mapping[str, ResumeScoreFactEvidence],
) -> ResumeScoreAnalysisResponse:
    analysis = raw_analysis if isinstance(raw_analysis, dict) else {}
    risk_flags: list[ResumeScoreRiskFlag] = []
    raw_flags = analysis.get("risk_flags")
    if isinstance(raw_flags, list):
        for raw_flag in raw_flags:
            if isinstance(raw_flag, dict):
                message = _optional_string(raw_flag.get("message"))
                fact_ids = _string_list(raw_flag.get("fact_ids"))
            elif isinstance(raw_flag, str):
                # Older score rows used a string-only risk list. Preserve the
                # warning rather than making a historical score unreadable.
                message = _optional_string(raw_flag)
                fact_ids = []
            else:
                continue
            if message is None:
                continue
            risk_flags.append(
                ResumeScoreRiskFlag(
                    message=message,
                    fact_ids=fact_ids,
                    fact_evidence=[
                        fact_evidence_by_id[fact_id]
                        for fact_id in fact_ids
                        if fact_id in fact_evidence_by_id
                    ],
                )
            )
    return ResumeScoreAnalysisResponse(
        schema_version=_optional_string(analysis.get("schema_version")),
        overall_summary=_optional_string(analysis.get("overall_summary")) or "信息不足",
        risk_flags=risk_flags,
        needs_human_review=analysis.get("needs_human_review") is True,
    )


def _score_response(session: Session, score: ResumeScore) -> ResumeScoreResponse:
    fact_evidence_by_id = _fact_evidence_by_id(score)
    audit_trail = _score_audit_entries(session, score=score)
    latest_audit_by_dimension = {
        entry.dimension_key: entry
        for entry in audit_trail
        if entry.dimension_key is not None
    }
    dimensions = [
        _dimension_response(
            record,
            fact_evidence_by_id=fact_evidence_by_id,
            latest_audit_by_dimension=latest_audit_by_dimension,
        )
        for record in (score.dimension_scores or [])
        if isinstance(record, dict)
    ]
    template = score.template
    snapshot = score.fact_snapshot
    resume = score.resume
    return ResumeScoreResponse(
        score_id=score.id,
        resume_id=score.resume_id,
        fact_snapshot_id=score.fact_snapshot_id,
        template_id=score.template_id,
        template_name=template.name if template is not None else None,
        template_description=template.description if template is not None else None,
        facts_version=score.facts_version,
        template_version=score.template_version,
        fact_snapshot_created_at=(
            snapshot.created_at.isoformat() if snapshot is not None else None
        ),
        is_current_facts_version=(
            resume is not None and resume.facts_version == score.facts_version
        ),
        is_current_template_version=(
            template is not None and template.version == score.template_version
        ),
        total_score=score.total_score,
        ai_total_score=score.ai_total_score,
        dimension_scores=dimensions,
        analysis=_analysis_response(
            score.analysis,
            fact_evidence_by_id=fact_evidence_by_id,
        ),
        audit_trail=audit_trail,
        status=score.status,
        model_name=score.model_name,
        created_at=score.created_at.isoformat(),
    )


def _require_valid_dimensions(dimensions: list[ScoreTemplateDimension]) -> None:
    if not dimensions:
        raise ScoreServiceError("score_template_has_no_dimensions")
    if sum(dimension.weight for dimension in dimensions) != 100:
        raise ScoreServiceError("score_template_weights_must_sum_to_100")
    if len({dimension.key for dimension in dimensions}) != len(dimensions):
        raise ScoreServiceError("score_template_dimension_keys_must_be_unique")


def create_score_template(
    session: Session,
    *,
    payload: ScoreTemplateCreate,
) -> ScoreTemplateResponse:
    template = ScoreTemplate(
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        version=1,
    )
    session.add(template)
    session.flush()
    for sort_order, dimension in enumerate(payload.dimensions):
        session.add(
            ScoreTemplateDimension(
                template_id=template.id,
                key=dimension.key,
                label=dimension.label.strip(),
                weight=dimension.weight,
                guidance=dimension.guidance.strip() if dimension.guidance else None,
                sort_order=sort_order,
            )
        )
    session.flush()
    template = session.get(ScoreTemplate, template.id)
    assert template is not None
    _require_valid_dimensions(template.dimensions)
    return _template_response(template)


def list_score_templates(session: Session) -> list[ScoreTemplateResponse]:
    templates = session.scalars(
        select(ScoreTemplate)
        .where(ScoreTemplate.is_archived.is_(False))
        .order_by(ScoreTemplate.updated_at.desc(), ScoreTemplate.id.desc())
    ).all()
    return [_template_response(template) for template in templates]


def _load_ready_resume_and_snapshot(
    session: Session,
    *,
    resume_id: str,
) -> tuple[Resume, ResumeFactSnapshot, dict[str, object]]:
    # Do not use ``session.get`` here.  A long-running model call can leave a
    # Resume instance in the identity map while another request has logically
    # deleted it or written a newer fact snapshot.  ``populate_existing``
    # forces a fresh scoped read, so the CandidateDataLifecycle filter also
    # turns a newly deleted resume into a normal not-found result.
    resume = session.scalar(
        select(Resume)
        .where(Resume.id == resume_id)
        .execution_options(populate_existing=True)
    )
    if resume is None:
        raise ScoreServiceError("resume_not_found")
    if resume.extraction_status != "ready" or not resume.is_active:
        raise ScoreServiceError("resume_must_be_active_and_ready_for_scoring")
    if has_unreliable_source_text(resume.quality_flags):
        raise ScoreServiceError("resume_source_text_unreliable")
    snapshot = session.scalar(
        select(ResumeFactSnapshot)
        .where(ResumeFactSnapshot.resume_id == resume.id)
        .order_by(ResumeFactSnapshot.facts_version.desc())
        .execution_options(populate_existing=True)
    )
    if snapshot is None or snapshot.facts_version != resume.facts_version:
        raise ScoreServiceError("resume_fact_snapshot_not_current")
    try:
        parsed_snapshot = json.loads(snapshot.canonical_facts_json)
    except json.JSONDecodeError as exc:
        raise ScoreServiceError("resume_fact_snapshot_invalid") from exc
    if not isinstance(parsed_snapshot, dict):
        raise ScoreServiceError("resume_fact_snapshot_invalid")
    return resume, snapshot, parsed_snapshot


def _require_unchanged_resume_snapshot(
    session: Session,
    *,
    resume_id: str,
    expected_snapshot_id: str,
    expected_facts_version: int,
) -> tuple[Resume, ResumeFactSnapshot]:
    """Re-check the privacy root immediately before writing model output.

    The provider request runs outside the database's control.  During that
    time a recruiter may delete the resume, or an editor may save a newer
    reviewed-facts version.  In either case the model output is no longer
    eligible to become a candidate-visible score.
    """

    # Flush only work that pre-dates the result write (for example the gateway
    # ledger), then discard cached ORM state so the following scoped SELECT
    # cannot accidentally reuse the pre-call Resume instance.
    session.flush()
    session.expire_all()
    try:
        resume, snapshot, _ = _load_ready_resume_and_snapshot(
            session,
            resume_id=resume_id,
        )
    except ScoreServiceError as exc:
        raise ScoreServiceError("resume_changed_before_scoring_completed") from exc
    if (
        snapshot.id != expected_snapshot_id
        or snapshot.facts_version != expected_facts_version
        or resume.facts_version != expected_facts_version
    ):
        raise ScoreServiceError("resume_changed_before_scoring_completed")
    return resume, snapshot


def _dimension_records(
    *,
    dimensions: list[ScoreTemplateDimension],
    provider_result: dict[str, object],
) -> tuple[list[dict[str, object]], float]:
    result_by_key = {
        str(item["key"]): item
        for item in provider_result["dimension_scores"]
        if isinstance(item, dict)
    }
    records: list[dict[str, object]] = []
    total = 0.0
    for dimension in sorted(dimensions, key=lambda item: item.sort_order):
        provider_dimension = result_by_key.get(dimension.key)
        if provider_dimension is None:
            raise ScoreServiceError("score_provider_missing_dimension")
        raw_score = float(provider_dimension["raw_score"])
        weighted_score = raw_score / 100 * dimension.weight
        total += weighted_score
        records.append(
            {
                "key": dimension.key,
                "label": dimension.label,
                "weight": dimension.weight,
                "ai_raw_score": raw_score,
                "final_raw_score": raw_score,
                "weighted_score": round(weighted_score, 4),
                "ai_weighted_score": round(weighted_score, 4),
                "final_weighted_score": round(weighted_score, 4),
                "rationale": provider_dimension["rationale"],
                "fact_ids": provider_dimension["fact_ids"],
                "uncertainties": provider_dimension["uncertainties"],
                "manual_reason": None,
                "manual_actor": None,
                "adjusted_at": None,
            }
        )
    return records, round(total, 2)


def run_resume_score(
    session: Session,
    *,
    resume_id: str,
    payload: ResumeScoreCreate,
    settings: AppSettings,
    pinned_route_policy_version_id: str | None = None,
) -> ResumeScoreResponse:
    # The settings-level model/key fields remain legacy call arguments while
    # older prompt helpers are being migrated.  They are deliberately not the
    # source of the actual provider, endpoint, credential, or model choice:
    # the gateway resolves those from the published platform route.
    if not ai_gateway_credentials_configured(settings):
        raise ScoreServiceError("deepseek_api_key_not_configured")
    template = session.get(ScoreTemplate, payload.template_id)
    if template is None or template.is_archived:
        raise ScoreTemplateNotFoundError("score_template_not_found")
    dimensions = session.scalars(
        select(ScoreTemplateDimension)
        .where(ScoreTemplateDimension.template_id == template.id)
        .order_by(ScoreTemplateDimension.sort_order)
    ).all()
    _require_valid_dimensions(dimensions)
    resume, snapshot, fact_snapshot = _load_ready_resume_and_snapshot(
        session,
        resume_id=resume_id,
    )
    expected_snapshot_id = snapshot.id
    expected_facts_version = snapshot.facts_version
    compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
        gateway_prompt_transport_arguments(settings)
    )
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_score",
                business_ref_type="resume_score",
                business_ref_id=(
                    f"{resume.id}:{template.id}:v{template.version}:facts{snapshot.facts_version}"
                ),
                contract_version="resume_score.v1",
                pinned_route_policy_version_id=pinned_route_policy_version_id,
            ),
        ):
            provider_result = score_resume_fact_snapshot(
                # These legacy arguments are consumed only by the existing
                # prompt/schema helper.  Inside the gateway context its
                # transport ignores them and uses the resolved route instead.
                api_key=compatibility_api_key,
                model=compatibility_model,
                timeout_seconds=compatibility_timeout_seconds,
                fact_snapshot=fact_snapshot,
                dimensions=[
                    {
                        "key": dimension.key,
                        "label": dimension.label,
                        "weight": dimension.weight,
                        "guidance": dimension.guidance,
                    }
                    for dimension in dimensions
                ],
            )
    except AiGatewayError as exc:
        # Keep domain callers/provider-agnostic HTTP handling independent of
        # the current gateway implementation while preserving stable errors.
        raise ScoreServiceError(str(exc)) from exc
    resume, snapshot = _require_unchanged_resume_snapshot(
        session,
        resume_id=resume_id,
        expected_snapshot_id=expected_snapshot_id,
        expected_facts_version=expected_facts_version,
    )
    dimension_scores, ai_total_score = _dimension_records(
        dimensions=dimensions,
        provider_result=provider_result,
    )
    analysis = {
        "schema_version": provider_result["schema_version"],
        "overall_summary": provider_result["overall_summary"],
        "risk_flags": provider_result["risk_flags"],
        "needs_human_review": provider_result["needs_human_review"],
    }
    score = ResumeScore(
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        template_id=template.id,
        facts_version=snapshot.facts_version,
        template_version=template.version,
        total_score=ai_total_score,
        ai_total_score=ai_total_score,
        dimension_scores=dimension_scores,
        analysis=analysis,
        status=("needs_review" if provider_result["needs_human_review"] else "succeeded"),
        # The legacy result column remains for API compatibility only.  The
        # actual resolved model is recorded in the immutable gateway ledger,
        # so this business record must not claim a settings-bound model.
        model_name="gateway-managed",
    )
    session.add(score)
    session.flush()
    return _score_response(session, score)


def get_resume_score(session: Session, *, score_id: str) -> ResumeScoreResponse:
    score = session.scalar(
        select(ResumeScore)
        .join(Resume, Resume.id == ResumeScore.resume_id)
        .where(ResumeScore.id == score_id)
        .options(
            selectinload(ResumeScore.resume),
            selectinload(ResumeScore.fact_snapshot),
            selectinload(ResumeScore.template),
        )
    )
    if score is None:
        raise ResumeScoreNotFoundError("resume_score_not_found")
    return _score_response(session, score)


def list_resume_scores(
    session: Session,
    *,
    resume_id: str,
) -> list[ResumeScoreResponse]:
    if session.get(Resume, resume_id) is None:
        raise ScoreServiceError("resume_not_found")
    scores = session.scalars(
        select(ResumeScore)
        .join(Resume, Resume.id == ResumeScore.resume_id)
        .where(ResumeScore.resume_id == resume_id)
        .options(
            selectinload(ResumeScore.resume),
            selectinload(ResumeScore.fact_snapshot),
            selectinload(ResumeScore.template),
        )
        .order_by(ResumeScore.created_at.desc(), ResumeScore.id.desc())
    ).all()
    return [_score_response(session, score) for score in scores]


def _recalculate_final_total(dimension_scores: list[dict[str, object]]) -> float:
    total = 0.0
    for dimension in dimension_scores:
        final_raw_score = float(dimension["final_raw_score"])
        weight = float(dimension["weight"])
        total += final_raw_score / 100 * weight
    return round(total, 2)


def override_score_dimension(
    session: Session,
    *,
    score_id: str,
    dimension_key: str,
    payload: ResumeScoreOverride,
) -> ResumeScoreResponse:
    score = session.scalar(
        select(ResumeScore)
        .join(Resume, Resume.id == ResumeScore.resume_id)
        .where(ResumeScore.id == score_id)
    )
    if score is None:
        raise ResumeScoreNotFoundError("resume_score_not_found")
    records = [dict(record) for record in (score.dimension_scores or [])]
    target = next((record for record in records if record.get("key") == dimension_key), None)
    if target is None:
        raise ScoreServiceError("score_dimension_not_found")
    old_value = target.get("final_raw_score")
    final_raw_score = float(payload.raw_score)
    final_weighted_score = round(
        final_raw_score / 100 * float(target["weight"]),
        4,
    )
    target["final_raw_score"] = final_raw_score
    # Keep the original field correct for old clients, and retain both values
    # explicitly so a client can show the AI score and the manual final score.
    target["weighted_score"] = final_weighted_score
    target["final_weighted_score"] = final_weighted_score
    target["manual_reason"] = payload.reason.strip()
    target["manual_actor"] = "single_admin"
    target["adjusted_at"] = datetime.now(timezone.utc).isoformat()
    score.dimension_scores = records
    score.total_score = _recalculate_final_total(records)
    score.status = "overridden"
    session.add(
        ResumeReviewAction(
            resume_id=score.resume_id,
            action="score_dimension_overridden",
            note=payload.reason.strip(),
            old_values={
                "score_id": score.id,
                "dimension_key": dimension_key,
                "final_raw_score": old_value,
                "ai_raw_score": target.get("ai_raw_score"),
                "facts_version": score.facts_version,
                "template_version": score.template_version,
            },
            new_values={
                "score_id": score.id,
                "dimension_key": dimension_key,
                "final_raw_score": final_raw_score,
                "ai_raw_score": target.get("ai_raw_score"),
                "facts_version": score.facts_version,
                "template_version": score.template_version,
            },
        )
    )
    session.flush()
    return _score_response(session, score)


__all__ = [
    "DeepSeekProviderError",
    "ResumeScoreNotFoundError",
    "ScoreServiceError",
    "ScoreTemplateNotFoundError",
    "create_score_template",
    "get_resume_score",
    "list_resume_scores",
    "list_score_templates",
    "override_score_dimension",
    "run_resume_score",
]
