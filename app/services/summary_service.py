from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import Resume, ResumeFactSnapshot, ResumeSummary
from app.schemas import (
    ResumeSummaryManualCreate,
    ResumeSummaryResponse,
)
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    summarize_resume_fact_snapshot,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
)
from app.services.resume_eligibility import has_unreliable_source_text


class SummaryServiceError(RuntimeError):
    pass


class ResumeSummaryNotFoundError(SummaryServiceError):
    pass


def _response(summary: ResumeSummary) -> ResumeSummaryResponse:
    return ResumeSummaryResponse(
        summary_id=summary.id,
        resume_id=summary.resume_id,
        fact_snapshot_id=summary.fact_snapshot_id,
        facts_version=summary.facts_version,
        content=summary.content or {},
        source=summary.source,
        supersedes_id=summary.supersedes_id,
        is_current=summary.is_current,
        status=summary.status,
        model_name=summary.model_name,
        created_at=summary.created_at.isoformat(),
    )


def _ready_resume_snapshot(
    session: Session,
    *,
    resume_id: str,
) -> tuple[Resume, ResumeFactSnapshot, dict[str, object]]:
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise SummaryServiceError("resume_not_found")
    if resume.extraction_status != "ready" or not resume.is_active:
        raise SummaryServiceError("resume_must_be_active_and_ready_for_summary")
    if has_unreliable_source_text(resume.quality_flags):
        raise SummaryServiceError("resume_source_text_unreliable")
    snapshot = session.scalar(
        select(ResumeFactSnapshot)
        .where(
            ResumeFactSnapshot.resume_id == resume.id,
            ResumeFactSnapshot.organization_id == resume.organization_id,
        )
        .order_by(ResumeFactSnapshot.facts_version.desc())
    )
    if snapshot is None or snapshot.facts_version != resume.facts_version:
        raise SummaryServiceError("resume_fact_snapshot_not_current")
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except json.JSONDecodeError as exc:
        raise SummaryServiceError("resume_fact_snapshot_invalid") from exc
    if not isinstance(payload, dict):
        raise SummaryServiceError("resume_fact_snapshot_invalid")
    return resume, snapshot, payload


def _set_current(
    session: Session,
    *,
    resume_id: str,
    organization_id: str,
) -> None:
    session.execute(
        update(ResumeSummary)
        .where(
            ResumeSummary.resume_id == resume_id,
            ResumeSummary.organization_id == organization_id,
            ResumeSummary.is_current.is_(True),
        )
        .values(is_current=False)
    )


def generate_resume_summary(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> ResumeSummaryResponse:
    # A generic server-side credential map is sufficient after the gateway
    # migration.  Preserve the pre-existing stable error for installations
    # where neither the legacy key nor any configured credential is present.
    if not ai_gateway_credentials_configured(settings):
        raise SummaryServiceError("deepseek_api_key_not_configured")
    resume, snapshot, fact_snapshot = _ready_resume_snapshot(session, resume_id=resume_id)
    compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
        gateway_prompt_transport_arguments(settings)
    )
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_summary",
                business_ref_type="resume_summary",
                business_ref_id=f"{resume.id}:facts{snapshot.facts_version}",
                contract_version="resume_summary.v1",
            ),
        ):
            content = summarize_resume_fact_snapshot(
                # Retained only for the legacy prompt helper.  The active
                # gateway transport resolves the actual model and credential.
                api_key=compatibility_api_key,
                model=compatibility_model,
                timeout_seconds=compatibility_timeout_seconds,
                fact_snapshot=fact_snapshot,
            )
    except AiGatewayError as exc:
        raise SummaryServiceError(str(exc)) from exc
    _set_current(
        session,
        resume_id=resume.id,
        organization_id=resume.organization_id,
    )
    summary = ResumeSummary(
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        facts_version=snapshot.facts_version,
        content=content,
        source="ai",
        is_current=True,
        status="succeeded",
        # The resolved model belongs to the gateway ledger, not a legacy
        # settings-bound result field.
        model_name="gateway-managed",
    )
    session.add(summary)
    session.flush()
    return _response(summary)


def get_resume_summary(session: Session, *, summary_id: str) -> ResumeSummaryResponse:
    summary = session.get(ResumeSummary, summary_id)
    if summary is None:
        raise ResumeSummaryNotFoundError("resume_summary_not_found")
    return _response(summary)


def list_resume_summaries(
    session: Session,
    *,
    resume_id: str,
) -> list[ResumeSummaryResponse]:
    # Checking the scoped parent first keeps a foreign resume ID
    # indistinguishable from a nonexistent one rather than returning an empty
    # collection that can be used as an authorization oracle.
    if session.get(Resume, resume_id) is None:
        raise SummaryServiceError("resume_not_found")
    summaries = session.scalars(
        select(ResumeSummary)
        .where(ResumeSummary.resume_id == resume_id)
        .order_by(ResumeSummary.created_at.desc(), ResumeSummary.id.desc())
    ).all()
    return [_response(summary) for summary in summaries]


def create_manual_summary_version(
    session: Session,
    *,
    summary_id: str,
    payload: ResumeSummaryManualCreate,
) -> ResumeSummaryResponse:
    prior_summary = session.get(ResumeSummary, summary_id)
    if prior_summary is None:
        raise ResumeSummaryNotFoundError("resume_summary_not_found")
    _, current_snapshot, _ = _ready_resume_snapshot(
        session,
        resume_id=prior_summary.resume_id,
    )
    if (
        prior_summary.fact_snapshot_id != current_snapshot.id
        or prior_summary.facts_version != current_snapshot.facts_version
    ):
        raise SummaryServiceError("resume_summary_not_current_facts")
    _set_current(
        session,
        resume_id=prior_summary.resume_id,
        organization_id=prior_summary.organization_id,
    )
    summary = ResumeSummary(
        resume_id=prior_summary.resume_id,
        fact_snapshot_id=prior_summary.fact_snapshot_id,
        facts_version=prior_summary.facts_version,
        content={
            "schema_version": "resume_summary.manual.v1",
            "sections": {key.strip(): value.strip() for key, value in payload.content.items()},
        },
        source="manual",
        supersedes_id=prior_summary.id,
        is_current=True,
        status="succeeded",
        model_name=None,
    )
    session.add(summary)
    session.flush()
    return _response(summary)


__all__ = [
    "DeepSeekProviderError",
    "ResumeSummaryNotFoundError",
    "SummaryServiceError",
    "create_manual_summary_version",
    "generate_resume_summary",
    "get_resume_summary",
    "list_resume_summaries",
]
