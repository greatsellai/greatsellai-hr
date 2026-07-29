from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import Candidate, Resume, ResumeAiExtractionJob, ResumeSourceBlock
from app.observability import log_exception_event
from app.schemas import ResumeFactsSaveRequest, ResumeFactsSubmission
from app.tenant_scope import clear_organization_context, set_organization_context
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    EvidenceBlock,
    extract_resume_candidate_name,
    extract_resume_core_facts,
    extract_resume_facts,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
    resolve_active_route_policy_version_id,
)
from app.services.resume_service import (
    FactValidationError,
    _assert_raw_value_grounded,
    _source_text_by_ids,
    get_resume,
    merge_filter_v2_enrichment,
    prepare_ai_draft_facts,
    reparse_clone_auto_activation_allowed,
    save_facts,
)
from app.services.resume_summary_job_service import enqueue_resume_summary_job


AI_EXTRACTION_QUEUED = "queued"
AI_EXTRACTION_RUNNING = "running"
AI_EXTRACTION_COMPLETED = "completed"
AI_EXTRACTION_NEEDS_ATTENTION = "needs_attention"
AI_EXTRACTION_UNAVAILABLE = "unavailable"

_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_NO_KEY_ERROR = "deepseek_api_key_not_configured"
_RETRYABLE_STRUCTURED_RESPONSE_ERRORS = frozenset(
    {
        "deepseek_empty_structured_facts",
        "deepseek_invalid_structured_response",
        "deepseek_tool_call_missing",
        "deepseek_arguments_missing",
        "deepseek_response_truncated",
    }
)


class AiExtractionJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedAiExtractionJob:
    job_id: str
    organization_id: str
    resume_id: str
    input_facts_version: int
    job_kind: str
    previous_error: str | None
    ai_route_policy_version_id: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _route_pin_for_new_extraction_job(
    session: Session,
    *,
    settings: AppSettings,
) -> tuple[str | None, str | None]:
    """Pin the current extraction route or make the queue state actionable."""

    if not ai_gateway_credentials_configured(settings):
        return None, _NO_KEY_ERROR
    try:
        return (
            resolve_active_route_policy_version_id(
                session,
                settings=settings,
                feature="resume_extract_rich",
            ),
            None,
        )
    except AiGatewayError as exc:
        return None, str(exc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Bind a worker-owned session to exactly one workspace.

    Queue discovery is deliberately global so one worker can serve every
    customer.  Nothing after that discovery is global: every source read,
    model-result write, and failure update executes with the claimed
    workspace installed on the SQLAlchemy session.
    """

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def ai_extraction_state(
    resume: Resume,
) -> tuple[str, str | None]:
    """Return the UI-safe status for the most recent durable AI job."""

    document_job = resume.document_extraction_job
    if document_job is not None:
        if document_job.status in {"queued", "running"}:
            # The browser already understands the existing queued/running AI
            # vocabulary. Keeping it here avoids a misleading
            # `needs_attention` state while source text is still being safely
            # normalized by the preceding worker stage.
            return AI_EXTRACTION_QUEUED, None
        if document_job.status == "needs_attention":
            return AI_EXTRACTION_NEEDS_ATTENTION, document_job.last_error
    job = resume.ai_extraction_job
    if job is not None:
        return job.status, job.last_error
    if resume.source_blocks:
        # This is only expected for pre-worker legacy data.  New uploads create
        # a job in the same transaction as the Resume row.
        return AI_EXTRACTION_NEEDS_ATTENTION, "ai_extraction_not_queued"
    return AI_EXTRACTION_NEEDS_ATTENTION, "resume_source_text_unavailable"


def enqueue_uploaded_resume_ai_extraction(
    session: Session,
    *,
    resume: Resume,
    settings: AppSettings,
) -> ResumeAiExtractionJob | None:
    """Create an AI job when standardized source text is available.

    Every supported file is normalized into source-cited text before this
    point.  A quality flag may still ask the recruiter to inspect the source,
    but it must not discard useful OCR, Office, spreadsheet, or HTML text.
    """

    if not resume.source_blocks:
        return None
    existing = resume.ai_extraction_job
    if existing is not None:
        return existing
    now = utcnow()
    route_policy_version_id, availability_error = _route_pin_for_new_extraction_job(
        session,
        settings=settings,
    )
    job = ResumeAiExtractionJob(
        organization_id=resume.organization_id,
        resume_id=resume.id,
        job_kind="initial",
        status=(
            AI_EXTRACTION_QUEUED
            if availability_error is None
            else AI_EXTRACTION_UNAVAILABLE
        ),
        attempt_count=0,
        max_attempts=settings.ai_extraction_job_max_attempts,
        input_facts_version=resume.facts_version,
        ai_route_policy_version_id=route_policy_version_id,
        next_attempt_at=now if availability_error is None else None,
        last_error=availability_error,
        requested_at=now,
    )
    session.add(job)
    resume.ai_extraction_job = job
    session.flush()
    return job


def request_resume_ai_extraction(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Queue/requeue AI extraction without executing a model call in HTTP.

    A completed human review is immutable here.  Retry is available only while
    the resume is still inactive and pending review, so an old delayed job can
    never replace confirmed screening data.
    """

    resume = get_resume(session, resume_id)
    document_job = resume.document_extraction_job
    if document_job is not None and document_job.status in {"queued", "running"}:
        # Source blocks from a previous failed/reparse attempt must never be
        # sent to the model while their replacement is still being normalized.
        raise AiExtractionJobError("resume_document_extraction_in_progress")
    job = resume.ai_extraction_job
    if job is None:
        if not resume.source_blocks:
            raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")
        enqueue_uploaded_resume_ai_extraction(session, resume=resume, settings=settings)
        return resume

    if job.status in {AI_EXTRACTION_QUEUED, AI_EXTRACTION_RUNNING}:
        return resume
    if resume.is_active or resume.extraction_status == "ready":
        raise AiExtractionJobError("completed_resume_cannot_be_reextracted")
    if resume.extraction_status not in {"text_ready", "needs_review"}:
        raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")
    if not resume.source_blocks:
        raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")

    now = utcnow()
    route_policy_version_id, availability_error = _route_pin_for_new_extraction_job(
        session,
        settings=settings,
    )
    job.status = (
        AI_EXTRACTION_QUEUED if availability_error is None else AI_EXTRACTION_UNAVAILABLE
    )
    job.attempt_count = 0
    job.max_attempts = settings.ai_extraction_job_max_attempts
    job.input_facts_version = resume.facts_version
    job.ai_route_policy_version_id = route_policy_version_id
    job.next_attempt_at = now if availability_error is None else None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = availability_error
    job.requested_at = now
    job.started_at = None
    job.completed_at = None
    session.flush()
    return resume


def request_resume_filter_v2_enrichment(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Queue a version-guarded, additive V2 enrichment for an active resume."""

    resume = get_resume(session, resume_id)
    if not resume.is_active or resume.extraction_status != "ready":
        raise AiExtractionJobError("filter_enrichment_requires_active_ready_resume")
    if not resume.source_blocks:
        raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")
    job = resume.ai_extraction_job
    if job is None:
        job = ResumeAiExtractionJob(
            organization_id=resume.organization_id,
            resume_id=resume.id,
            status=AI_EXTRACTION_QUEUED,
        )
        session.add(job)
        resume.ai_extraction_job = job
    elif job.status in {AI_EXTRACTION_QUEUED, AI_EXTRACTION_RUNNING}:
        return resume
    now = utcnow()
    route_policy_version_id, availability_error = _route_pin_for_new_extraction_job(
        session,
        settings=settings,
    )
    job.job_kind = "filter_v2_enrichment"
    job.status = (
        AI_EXTRACTION_QUEUED if availability_error is None else AI_EXTRACTION_UNAVAILABLE
    )
    job.attempt_count = 0
    job.max_attempts = settings.ai_extraction_job_max_attempts
    job.input_facts_version = resume.facts_version
    job.ai_route_policy_version_id = route_policy_version_id
    job.next_attempt_at = now if availability_error is None else None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = availability_error
    job.requested_at = now
    job.started_at = None
    job.completed_at = None
    session.flush()
    return resume


def backfill_unnamed_candidate_names(
    database: Database,
    *,
    settings: AppSettings,
    limit: int = 100,
) -> tuple[int, int]:
    """Fill only empty candidate names without touching completed facts.

    The compact extraction fallback deliberately omits identity. This bounded
    repair calls a name-only contract and keeps the same page-evidence check
    before writing, so it can never replace an existing name or alter scores,
    summaries, or screening facts.
    """

    if limit < 1:
        return 0, 0
    if not ai_gateway_credentials_configured(settings):
        raise AiExtractionJobError(_NO_KEY_ERROR)

    with database.session_factory() as session:
        resume_rows = session.execute(
            select(Resume.id, Resume.organization_id)
            .join(Candidate, Candidate.id == Resume.candidate_id)
            .where(
                or_(Candidate.display_name.is_(None), Candidate.display_name == ""),
            )
            .order_by(Resume.created_at.asc(), Resume.id.asc())
            .limit(limit)
            # This is a platform repair scan.  It deliberately discovers
            # independent work items globally, but every individual item is
            # re-opened below under its own organization context.
            .execution_options(skip_organization_scope=True)
        ).all()

    updated = 0
    skipped = 0
    for resume_id, organization_id in resume_rows:
        if not organization_id:
            skipped += 1
            continue
        with database.session_factory() as session:
            with _organization_session(session, organization_id):
                resume = session.get(Resume, resume_id)
                if resume is None or resume.organization_id != organization_id:
                    continue
                source_blocks = session.scalars(
                    select(ResumeSourceBlock)
                    .where(ResumeSourceBlock.resume_id == resume.id)
                    .order_by(ResumeSourceBlock.page_no, ResumeSourceBlock.block_id)
                ).all()
                blocks = [
                    EvidenceBlock(
                        block_id=block.block_id,
                        page_no=block.page_no,
                        block_type=block.block_type,
                        text=block.text,
                    )
                    for block in source_blocks
                ]
        if not blocks:
            skipped += 1
            continue
        compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
            gateway_prompt_transport_arguments(settings)
        )
        try:
            with database.session_factory() as gateway_session:
                with _organization_session(gateway_session, organization_id):
                    with ai_gateway_execution(
                        gateway_session,
                        settings=settings,
                        spec=AiExecutionSpec(
                            feature="candidate_name_backfill",
                            business_ref_type="resume",
                            business_ref_id=resume_id,
                            contract_version="candidate_name.v1",
                        ),
                    ):
                        draft = extract_resume_candidate_name(
                            api_key=compatibility_api_key,
                            model=compatibility_model,
                            timeout_seconds=compatibility_timeout_seconds,
                            blocks=blocks,
                        )
        except (DeepSeekProviderError, AiGatewayError):
            skipped += 1
            continue
        if draft.value is None:
            skipped += 1
            continue

        with database.session_factory() as session:
            with _organization_session(session, organization_id):
                resume = session.scalar(
                    select(Resume).where(Resume.id == resume_id).with_for_update()
                )
                if resume is None or resume.organization_id != organization_id:
                    continue
                candidate = session.scalar(
                    select(Candidate)
                    .where(Candidate.id == resume.candidate_id)
                    .with_for_update()
                )
                if candidate is None or candidate.organization_id != organization_id:
                    session.rollback()
                    continue
                if candidate.display_name and candidate.display_name.strip():
                    session.rollback()
                    continue
                try:
                    evidence_text = _source_text_by_ids(
                        session,
                        resume_id=resume.id,
                        block_ids=draft.evidence_block_ids,
                    )
                    _assert_raw_value_grounded(
                        value=draft.value,
                        source_text=evidence_text,
                        label="candidate_name_raw",
                    )
                except FactValidationError:
                    session.rollback()
                    skipped += 1
                    continue
                candidate.display_name = draft.value
                session.commit()
                updated += 1
    return updated, skipped


def run_ai_extraction_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one persisted job.  Returns whether one ran."""

    claimed = _claim_next_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_job(
        database,
        settings=settings,
        worker_id=worker_id,
        claimed=claimed,
    )
    return True


def _claim_next_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedAiExtractionJob | None:
    now = utcnow()
    with database.session_factory() as session:
        if not ai_gateway_credentials_configured(settings):
            _mark_jobs_unavailable_without_key(session, now=now)
            session.commit()
            return None

        # A deploy/restart can make a previously unavailable job executable.
        session.execute(
            update(ResumeAiExtractionJob)
            .where(
                ResumeAiExtractionJob.status == AI_EXTRACTION_UNAVAILABLE,
                ResumeAiExtractionJob.last_error == _NO_KEY_ERROR,
            )
            .values(
                status=AI_EXTRACTION_QUEUED,
                next_attempt_at=now,
                last_error=None,
                lease_owner=None,
                lease_expires_at=None,
            )
            .execution_options(skip_organization_scope=True)
        )
        _recover_expired_leases(session, now=now)

        eligible = and_(
            ResumeAiExtractionJob.status == AI_EXTRACTION_QUEUED,
            ResumeAiExtractionJob.attempt_count < ResumeAiExtractionJob.max_attempts,
            or_(
                ResumeAiExtractionJob.next_attempt_at.is_(None),
                ResumeAiExtractionJob.next_attempt_at <= now,
            ),
        )
        candidate = session.execute(
            select(
                ResumeAiExtractionJob.id,
                ResumeAiExtractionJob.organization_id,
                ResumeAiExtractionJob.resume_id,
                ResumeAiExtractionJob.input_facts_version,
                ResumeAiExtractionJob.job_kind,
                ResumeAiExtractionJob.last_error,
                ResumeAiExtractionJob.ai_route_policy_version_id,
            )
            .where(eligible)
            .order_by(
                ResumeAiExtractionJob.next_attempt_at.asc(),
                ResumeAiExtractionJob.requested_at.asc(),
                ResumeAiExtractionJob.id.asc(),
            )
            .limit(1)
            .execution_options(skip_organization_scope=True)
        ).one_or_none()
        if candidate is None:
            session.commit()
            return None
        (
            candidate_id,
            organization_id,
            resume_id,
            input_facts_version,
            job_kind,
            previous_error,
            route_policy_version_id,
        ) = candidate
        if not organization_id:
            # A row without a workspace must never be handed to a model call.
            # Mark it terminally from the global claim path and leave all
            # workspace-bound reads/writes untouched.
            session.execute(
                update(ResumeAiExtractionJob)
                .where(ResumeAiExtractionJob.id == candidate_id)
                .values(
                    status=AI_EXTRACTION_NEEDS_ATTENTION,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="ai_extraction_workspace_missing",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return None

        if route_policy_version_id is None:
            try:
                route_policy_version_id = resolve_active_route_policy_version_id(
                    session,
                    settings=settings,
                    feature="resume_extract_rich",
                )
            except AiGatewayError:
                # Let the established execution/failure path record the
                # actionable route error when no version is available. When a
                # route exists, the conditional claim below persists it before
                # any source text is sent to a provider.
                route_policy_version_id = None

        lease_expires_at = now + timedelta(
            seconds=settings.ai_extraction_job_lease_seconds
        )
        claim = session.execute(
            update(ResumeAiExtractionJob)
            .where(
                ResumeAiExtractionJob.id == candidate_id,
                ResumeAiExtractionJob.organization_id == organization_id,
                eligible,
            )
            .values(
                status=AI_EXTRACTION_RUNNING,
                attempt_count=ResumeAiExtractionJob.attempt_count + 1,
                started_at=now,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                next_attempt_at=None,
                last_error=None,
                ai_route_policy_version_id=route_policy_version_id,
            )
            .execution_options(skip_organization_scope=True)
        )
        if claim.rowcount != 1:
            session.rollback()
            return None
        claimed = ClaimedAiExtractionJob(
            job_id=candidate_id,
            organization_id=organization_id,
            resume_id=resume_id,
            input_facts_version=input_facts_version,
            job_kind=job_kind,
            previous_error=previous_error,
            ai_route_policy_version_id=route_policy_version_id,
        )
        session.commit()
        return claimed


def _mark_jobs_unavailable_without_key(session: Session, *, now: datetime) -> None:
    session.execute(
        update(ResumeAiExtractionJob)
        .where(ResumeAiExtractionJob.status == AI_EXTRACTION_QUEUED)
        .values(
            status=AI_EXTRACTION_UNAVAILABLE,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=_NO_KEY_ERROR,
            completed_at=now,
        )
        .execution_options(skip_organization_scope=True)
    )
    session.execute(
        update(ResumeAiExtractionJob)
        .where(
            ResumeAiExtractionJob.status == AI_EXTRACTION_RUNNING,
            ResumeAiExtractionJob.lease_expires_at.is_not(None),
            ResumeAiExtractionJob.lease_expires_at <= now,
        )
        .values(
            status=AI_EXTRACTION_UNAVAILABLE,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=_NO_KEY_ERROR,
            completed_at=now,
        )
        .execution_options(skip_organization_scope=True)
    )


def _recover_expired_leases(session: Session, *, now: datetime) -> None:
    expired = and_(
        ResumeAiExtractionJob.status == AI_EXTRACTION_RUNNING,
        ResumeAiExtractionJob.lease_expires_at.is_not(None),
        ResumeAiExtractionJob.lease_expires_at <= now,
    )
    session.execute(
        update(ResumeAiExtractionJob)
        .where(expired, ResumeAiExtractionJob.attempt_count >= ResumeAiExtractionJob.max_attempts)
        .values(
            status=AI_EXTRACTION_NEEDS_ATTENTION,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error="ai_extraction_worker_lease_expired",
            completed_at=now,
        )
        .execution_options(skip_organization_scope=True)
    )
    session.execute(
        update(ResumeAiExtractionJob)
        .where(expired, ResumeAiExtractionJob.attempt_count < ResumeAiExtractionJob.max_attempts)
        .values(
            status=AI_EXTRACTION_QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=now,
            last_error="ai_extraction_worker_lease_expired",
        )
        .execution_options(skip_organization_scope=True)
    )


def _process_claimed_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedAiExtractionJob,
) -> None:
    try:
        blocks = _load_claimed_source_blocks(
            database,
            worker_id=worker_id,
            claimed=claimed,
        )
    except AiExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=False,
        )
        return

    core_fallback = (
        claimed.job_kind == "initial"
        and claimed.previous_error in _RETRYABLE_STRUCTURED_RESPONSE_ERRORS
    )
    compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
        gateway_prompt_transport_arguments(settings)
    )
    try:
        with database.session_factory() as gateway_session:
            with _organization_session(gateway_session, claimed.organization_id):
                # The durable job pins the rich-extraction route.  A compact
                # prompt retry intentionally stays on that same route version
                # so a model switch cannot change an already queued resume.
                with ai_gateway_execution(
                    gateway_session,
                    settings=settings,
                    spec=AiExecutionSpec(
                        feature="resume_extract_rich",
                        business_ref_type="resume_ai_extraction_job",
                        business_ref_id=claimed.job_id,
                        contract_version=(
                            "resume_facts.core.v1" if core_fallback else "resume_facts.rich.v2"
                        ),
                        pinned_route_policy_version_id=claimed.ai_route_policy_version_id,
                    ),
                ):
                    if core_fallback:
                        facts = extract_resume_core_facts(
                            api_key=compatibility_api_key,
                            model=compatibility_model,
                            timeout_seconds=compatibility_timeout_seconds,
                            blocks=blocks,
                        )
                    else:
                        facts = extract_resume_facts(
                            api_key=compatibility_api_key,
                            model=compatibility_model,
                            timeout_seconds=compatibility_timeout_seconds,
                            blocks=blocks,
                        )
    except (DeepSeekProviderError, AiGatewayError) as exc:
        error = str(exc)
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error=error,
            retryable=_is_retryable_deepseek_error(error),
        )
        return
    except Exception as exc:  # pragma: no cover - defensive containment for the worker
        log_exception_event(
            "ai_extraction_worker_failed",
            error_code="ai_extraction_worker_error",
            exception=exc,
            job_id=claimed.job_id,
            workspace_id=claimed.organization_id,
        )
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error="ai_extraction_worker_error",
            retryable=True,
        )
        return

    try:
        _save_completed_ai_facts(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
            facts=facts,
            core_fallback=core_fallback,
        )
    except FactValidationError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=False,
        )
    except AiExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=False,
        )
    except Exception as exc:  # pragma: no cover - defensive containment for database faults
        log_exception_event(
            "ai_extraction_persist_failed",
            error_code="ai_extraction_persist_failed",
            exception=exc,
            job_id=claimed.job_id,
            workspace_id=claimed.organization_id,
        )
        _finish_failure(
            database,
            worker_id=worker_id,
            job_id=claimed.job_id,
            organization_id=claimed.organization_id,
            error="ai_extraction_persist_failed",
            retryable=True,
        )


def _load_claimed_source_blocks(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedAiExtractionJob,
) -> list[EvidenceBlock]:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
            )
            if job is None:
                raise AiExtractionJobError("ai_extraction_job_lease_lost")
            if job.organization_id != claimed.organization_id or job.resume_id != claimed.resume_id:
                raise AiExtractionJobError("ai_extraction_workspace_mismatch")
            resume = session.get(Resume, claimed.resume_id)
            if resume is None:
                raise AiExtractionJobError("resume_not_found")
            if resume.organization_id != claimed.organization_id:
                raise AiExtractionJobError("ai_extraction_workspace_mismatch")
            _assert_resume_unchanged_for_job(resume, job=job)
            source_blocks = session.scalars(
                select(ResumeSourceBlock)
                .where(ResumeSourceBlock.resume_id == resume.id)
                .order_by(ResumeSourceBlock.page_no, ResumeSourceBlock.block_id)
            ).all()
            if not source_blocks:
                raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")
            blocks = [
                EvidenceBlock(
                    block_id=block.block_id,
                    page_no=block.page_no,
                    block_type=block.block_type,
                    text=block.text,
                )
                for block in source_blocks
            ]
            # No transaction is held while the external provider call is in flight.
            session.rollback()
            return blocks


def _save_completed_ai_facts(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedAiExtractionJob,
    facts: ResumeFactsSubmission,
    core_fallback: bool = False,
) -> None:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            # Lock the job and resume only for the short persistence transaction.
            # The external AI request has already completed, so this does not hold
            # a database lock during network I/O.  It closes the race where a human
            # completes review between the version check and the AI result save.
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                raise AiExtractionJobError("ai_extraction_job_lease_lost")
            if job.organization_id != claimed.organization_id or job.resume_id != claimed.resume_id:
                session.rollback()
                raise AiExtractionJobError("ai_extraction_workspace_mismatch")
            resume = session.scalar(
                select(Resume)
                .where(Resume.id == claimed.resume_id)
                .with_for_update()
            )
            if resume is None:
                session.rollback()
                raise AiExtractionJobError("resume_not_found")
            if resume.organization_id != claimed.organization_id:
                session.rollback()
                raise AiExtractionJobError("ai_extraction_workspace_mismatch")
            _assert_resume_unchanged_for_job(resume, job=job)
            try:
                prepared_facts, is_partial_draft = prepare_ai_draft_facts(
                    session,
                    resume_id=resume.id,
                    facts=facts,
                )
                auto_activate = reparse_clone_auto_activation_allowed(
                    session,
                    resume=resume,
                )
                facts_to_save = (
                    merge_filter_v2_enrichment(
                        session,
                        resume_id=resume.id,
                        enrichment=prepared_facts,
                    )
                    if claimed.job_kind == "filter_v2_enrichment"
                    else prepared_facts
                )
                saved_resume = save_facts(
                    session,
                    resume_id=resume.id,
                    request=ResumeFactsSaveRequest(facts=facts_to_save),
                    # Model provenance lives in the immutable gateway ledger;
                    # facts must not claim the legacy settings model.
                    created_by="ai:gateway",
                    force_pending_review=True,
                    auto_activate=auto_activate,
                )
                if saved_resume.organization_id != claimed.organization_id:
                    raise AiExtractionJobError("ai_extraction_workspace_mismatch")
                quality_flags = set(saved_resume.quality_flags or [])
                if is_partial_draft:
                    quality_flags.add("ai_draft_partial_source_grounding")
                else:
                    quality_flags.discard("ai_draft_partial_source_grounding")
                if core_fallback:
                    quality_flags.add("ai_draft_details_pending")
                else:
                    quality_flags.discard("ai_draft_details_pending")
                if claimed.job_kind == "filter_v2_enrichment":
                    quality_flags.add("filter_v2_enriched")
                if auto_activate:
                    quality_flags.discard("reparse_source_superseded_before_completion")
                else:
                    # The original candidate version changed while the new source
                    # text was being analyzed.  Preserve both records and require
                    # an explicit fresh reparse instead of allowing a stale job to
                    # replace newer screening data.
                    quality_flags.add("reparse_source_superseded_before_completion")
                saved_resume.quality_flags = sorted(quality_flags)
                # Ordinary uploads auto-activate as before. Parser-repair clones
                # only do so when their source version still owns the candidate's
                # screening slot; otherwise their grounded facts stay as an
                # inactive reviewable version.
                if auto_activate:
                    if saved_resume.extraction_status != "ready" or not saved_resume.is_active:
                        raise AiExtractionJobError("ai_extraction_must_auto_activate")
                elif saved_resume.extraction_status != "needs_review" or saved_resume.is_active:
                    raise AiExtractionJobError("stale_reparse_must_remain_inactive")
                # Facts are now durable and active.  Queue the independent
                # summary stage in this same transaction, never in the HTTP
                # upload request and never by reusing the extraction lease.
                # A route/credential problem creates an actionable
                # ``unavailable`` summary job rather than rolling back a
                # valid searchable candidate.
                enqueue_resume_summary_job(
                    session,
                    resume=saved_resume,
                    settings=settings,
                )
                job.status = AI_EXTRACTION_COMPLETED
                job.lease_owner = None
                job.lease_expires_at = None
                job.next_attempt_at = None
                job.last_error = None
                job.completed_at = utcnow()
                session.commit()
            except Exception:
                session.rollback()
                raise


def _finish_failure(
    database: Database,
    *,
    worker_id: str,
    job_id: str,
    organization_id: str,
    error: str,
    retryable: bool,
) -> None:
    now = utcnow()
    with database.session_factory() as session:
        with _organization_session(session, organization_id):
            job = _owned_running_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                organization_id=organization_id,
            )
            if job is None or job.organization_id != organization_id:
                session.rollback()
                return
            retry = retryable and job.attempt_count < job.max_attempts
            if retry:
                delay_seconds = min(60, 2 ** max(job.attempt_count - 1, 0))
                job.status = AI_EXTRACTION_QUEUED
                job.next_attempt_at = now + timedelta(seconds=delay_seconds)
                job.completed_at = None
            else:
                job.status = AI_EXTRACTION_NEEDS_ATTENTION
                job.next_attempt_at = None
                job.completed_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = error[:2000]
            session.commit()


def _owned_running_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str | None = None,
    for_update: bool = False,
) -> ResumeAiExtractionJob | None:
    statement = select(ResumeAiExtractionJob).where(
        ResumeAiExtractionJob.id == job_id,
        ResumeAiExtractionJob.status == AI_EXTRACTION_RUNNING,
        ResumeAiExtractionJob.lease_owner == worker_id,
    )
    if organization_id is not None:
        statement = statement.where(ResumeAiExtractionJob.organization_id == organization_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _assert_resume_unchanged_for_job(
    resume: Resume,
    *,
    job: ResumeAiExtractionJob,
) -> None:
    if job.job_kind == "filter_v2_enrichment":
        if not resume.is_active or resume.extraction_status != "ready":
            raise AiExtractionJobError("resume_changed_before_ai_extraction_completed")
    else:
        if resume.is_active or resume.extraction_status == "ready":
            raise AiExtractionJobError("resume_changed_before_ai_extraction_completed")
        if resume.extraction_status not in {"text_ready", "needs_review"}:
            raise AiExtractionJobError("resume_has_no_native_text_for_ai_extraction")
    if resume.facts_version != job.input_facts_version:
        raise AiExtractionJobError("resume_changed_before_ai_extraction_completed")


def _is_retryable_deepseek_error(error: str) -> bool:
    if error in {
        "deepseek_network_error",
        "deepseek_timeout",
        *_RETRYABLE_STRUCTURED_RESPONSE_ERRORS,
    }:
        return True
    matched = re.fullmatch(r"deepseek_http_(\d{3})", error)
    if matched and int(matched.group(1)) in _RETRYABLE_HTTP_STATUSES:
        return True
    return error in {
        "ai_provider_network",
        "ai_provider_timeout",
        "ai_provider_rate_limited",
        "ai_provider_quota_exhausted",
        "ai_provider_provider_5xx",
    }


__all__ = [
    "AI_EXTRACTION_COMPLETED",
    "AI_EXTRACTION_NEEDS_ATTENTION",
    "AI_EXTRACTION_QUEUED",
    "AI_EXTRACTION_RUNNING",
    "AI_EXTRACTION_UNAVAILABLE",
    "AiExtractionJobError",
    "ai_extraction_state",
    "backfill_unnamed_candidate_names",
    "enqueue_uploaded_resume_ai_extraction",
    "request_resume_ai_extraction",
    "request_resume_filter_v2_enrichment",
    "run_ai_extraction_worker_once",
]
