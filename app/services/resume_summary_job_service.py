"""Durable, workspace-scoped automatic AI resume-summary jobs.

Facts extraction and summary generation deliberately have separate queues:
the candidate becomes searchable as soon as grounded facts are ready, while a
slow or failed summary can retry without changing that candidate state.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import Resume, ResumeFactSnapshot, ResumeSummary, ResumeSummaryJob
from app.observability import log_exception_event
from app.services.ai_gateway_service import (
    AiGatewayError,
    ai_gateway_credentials_configured,
    resolve_active_route_policy_version_id,
)
from app.services.ai_retry_policy import is_retryable_ai_transport_error
from app.services.deepseek_provider import DeepSeekProviderError
from app.services.resume_eligibility import has_unreliable_source_text
from app.services.summary_service import SummaryServiceError, generate_resume_summary
from app.tenant_scope import clear_organization_context, set_organization_context


SUMMARY_JOB_QUEUED = "queued"
SUMMARY_JOB_RUNNING = "running"
SUMMARY_JOB_SUCCEEDED = "succeeded"
SUMMARY_JOB_FAILED = "failed"
SUMMARY_JOB_UNAVAILABLE = "unavailable"
SUMMARY_JOB_SUPERSEDED = "superseded"
SUMMARY_JOB_CANCELLED = "cancelled"

_NO_KEY_ERROR = "deepseek_api_key_not_configured"
_UNAVAILABLE_ERRORS = frozenset(
    {
        _NO_KEY_ERROR,
        "ai_route_not_configured",
        "ai_route_disabled",
        "ai_route_not_published",
        "ai_pinned_route_not_available",
    }
)


class ResumeSummaryJobError(RuntimeError):
    """A stable, content-free automatic-summary queue failure."""


@dataclass(frozen=True)
class ClaimedResumeSummaryJob:
    job_id: str
    organization_id: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int
    ai_route_policy_version_id: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Scope every post-claim read and write to the claimed workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _route_pin_for_new_summary_job(
    session: Session,
    *,
    settings: AppSettings,
) -> tuple[str | None, str | None]:
    if not ai_gateway_credentials_configured(settings):
        return None, _NO_KEY_ERROR
    try:
        return (
            resolve_active_route_policy_version_id(
                session,
                settings=settings,
                feature="resume_summary",
            ),
            None,
        )
    except AiGatewayError as exc:
        return None, str(exc)


def _current_summary_for_facts(resume: Resume) -> ResumeSummary | None:
    candidates = [
        summary
        for summary in resume.summaries
        if summary.is_current
        and summary.status == "succeeded"
        and summary.facts_version == resume.facts_version
    ]
    return max(candidates, key=lambda summary: (summary.created_at, summary.id), default=None)


def summary_generation_state(resume: Resume) -> tuple[str | None, str | None]:
    """Return the UI-safe automatic-summary state for current facts only.

    Old fact revisions are intentionally invisible here.  They remain in the
    audit history but cannot make a freshly edited resume look completed.
    """

    if _current_summary_for_facts(resume) is not None:
        return SUMMARY_JOB_SUCCEEDED, None
    candidates = [
        job
        for job in resume.summary_jobs
        if job.facts_version == resume.facts_version
    ]
    job = max(candidates, key=lambda item: (item.updated_at, item.id), default=None)
    if job is None:
        return None, None
    return job.status, job.last_error


def enqueue_resume_summary_job(
    session: Session,
    *,
    resume: Resume,
    settings: AppSettings,
) -> ResumeSummaryJob | None:
    """Queue one automatic summary for an active, immutable fact snapshot.

    This function is safe to call in the same transaction that saves facts.
    It does not invoke a model or commit the caller's transaction.
    """

    if (
        resume.extraction_status != "ready"
        or not resume.is_active
        or has_unreliable_source_text(resume.quality_flags)
    ):
        return None
    snapshot = session.scalar(
        select(ResumeFactSnapshot).where(
            ResumeFactSnapshot.resume_id == resume.id,
            ResumeFactSnapshot.organization_id == resume.organization_id,
            ResumeFactSnapshot.facts_version == resume.facts_version,
        )
    )
    if snapshot is None:
        raise ResumeSummaryJobError("resume_fact_snapshot_not_current")

    existing = session.scalar(
        select(ResumeSummaryJob).where(
            ResumeSummaryJob.resume_id == resume.id,
            ResumeSummaryJob.facts_version == resume.facts_version,
        )
    )
    if existing is not None:
        return existing

    now = _utcnow()
    route_policy_version_id, availability_error = _route_pin_for_new_summary_job(
        session,
        settings=settings,
    )
    job = ResumeSummaryJob(
        organization_id=resume.organization_id,
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        facts_version=snapshot.facts_version,
        ai_route_policy_version_id=route_policy_version_id,
        status=(
            SUMMARY_JOB_QUEUED
            if availability_error is None
            else SUMMARY_JOB_UNAVAILABLE
        ),
        attempt_count=0,
        max_attempts=max(1, settings.ai_extraction_job_max_attempts),
        next_attempt_at=now if availability_error is None else None,
        last_error=availability_error,
        requested_at=now,
    )
    try:
        # The unique resume/facts-version key closes the two-worker race while
        # preserving the caller's outer transaction.
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(ResumeSummaryJob).where(
                ResumeSummaryJob.resume_id == resume.id,
                ResumeSummaryJob.facts_version == resume.facts_version,
            )
        )
        if existing is not None:
            return existing
        raise
    return job


def run_resume_summary_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one automatic summary task."""

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


def _mark_jobs_unavailable_without_key(session: Session, *, now: datetime) -> None:
    for status in (SUMMARY_JOB_QUEUED, SUMMARY_JOB_RUNNING):
        statement = update(ResumeSummaryJob).where(ResumeSummaryJob.status == status)
        if status == SUMMARY_JOB_RUNNING:
            statement = statement.where(
                ResumeSummaryJob.lease_expires_at.is_not(None),
                ResumeSummaryJob.lease_expires_at <= now,
            )
        session.execute(
            statement.values(
                status=SUMMARY_JOB_UNAVAILABLE,
                next_attempt_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error=_NO_KEY_ERROR,
                completed_at=now,
            ).execution_options(skip_organization_scope=True)
        )


def _recover_expired_leases(session: Session, *, now: datetime) -> None:
    expired = and_(
        ResumeSummaryJob.status == SUMMARY_JOB_RUNNING,
        ResumeSummaryJob.lease_expires_at.is_not(None),
        ResumeSummaryJob.lease_expires_at <= now,
    )
    session.execute(
        update(ResumeSummaryJob)
        .where(expired, ResumeSummaryJob.attempt_count >= ResumeSummaryJob.max_attempts)
        .values(
            status=SUMMARY_JOB_FAILED,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error="resume_summary_worker_lease_expired",
            completed_at=now,
        )
        .execution_options(skip_organization_scope=True)
    )
    session.execute(
        update(ResumeSummaryJob)
        .where(expired, ResumeSummaryJob.attempt_count < ResumeSummaryJob.max_attempts)
        .values(
            status=SUMMARY_JOB_QUEUED,
            next_attempt_at=now,
            lease_owner=None,
            lease_expires_at=None,
            last_error="resume_summary_worker_lease_expired",
            completed_at=None,
        )
        .execution_options(skip_organization_scope=True)
    )


def _claim_next_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedResumeSummaryJob | None:
    now = _utcnow()
    with database.session_factory() as session:
        if not ai_gateway_credentials_configured(settings):
            _mark_jobs_unavailable_without_key(session, now=now)
            session.commit()
            return None

        # A deployment that restores credentials should make the explicit
        # missing-key state runnable again.  Other unavailable states require
        # an intentional retry after their route policy is repaired.
        session.execute(
            update(ResumeSummaryJob)
            .where(
                ResumeSummaryJob.status == SUMMARY_JOB_UNAVAILABLE,
                ResumeSummaryJob.last_error == _NO_KEY_ERROR,
            )
            .values(
                status=SUMMARY_JOB_QUEUED,
                next_attempt_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                completed_at=None,
            )
            .execution_options(skip_organization_scope=True)
        )
        _recover_expired_leases(session, now=now)
        eligible = and_(
            ResumeSummaryJob.status == SUMMARY_JOB_QUEUED,
            ResumeSummaryJob.attempt_count < ResumeSummaryJob.max_attempts,
            or_(
                ResumeSummaryJob.next_attempt_at.is_(None),
                ResumeSummaryJob.next_attempt_at <= now,
            ),
        )
        candidate = session.execute(
            select(
                ResumeSummaryJob.id,
                ResumeSummaryJob.organization_id,
                ResumeSummaryJob.resume_id,
                ResumeSummaryJob.fact_snapshot_id,
                ResumeSummaryJob.facts_version,
                ResumeSummaryJob.ai_route_policy_version_id,
            )
            .where(eligible)
            .order_by(
                ResumeSummaryJob.next_attempt_at.asc(),
                ResumeSummaryJob.requested_at.asc(),
                ResumeSummaryJob.id.asc(),
            )
            .limit(1)
            .execution_options(skip_organization_scope=True)
        ).one_or_none()
        if candidate is None:
            session.commit()
            return None
        (
            job_id,
            organization_id,
            resume_id,
            fact_snapshot_id,
            facts_version,
            route_policy_version_id,
        ) = candidate
        if not organization_id:
            session.execute(
                update(ResumeSummaryJob)
                .where(ResumeSummaryJob.id == job_id)
                .values(
                    status=SUMMARY_JOB_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="resume_summary_workspace_missing",
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
                    feature="resume_summary",
                )
            except AiGatewayError as exc:
                session.execute(
                    update(ResumeSummaryJob)
                    .where(
                        ResumeSummaryJob.id == job_id,
                        ResumeSummaryJob.organization_id == organization_id,
                        eligible,
                    )
                    .values(
                        status=SUMMARY_JOB_UNAVAILABLE,
                        next_attempt_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=str(exc),
                        completed_at=now,
                    )
                    .execution_options(skip_organization_scope=True)
                )
                session.commit()
                return None

        lease_expires_at = now + timedelta(seconds=settings.ai_extraction_job_lease_seconds)
        claimed_update = session.execute(
            update(ResumeSummaryJob)
            .where(
                ResumeSummaryJob.id == job_id,
                ResumeSummaryJob.organization_id == organization_id,
                eligible,
            )
            .values(
                status=SUMMARY_JOB_RUNNING,
                attempt_count=ResumeSummaryJob.attempt_count + 1,
                started_at=now,
                next_attempt_at=None,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                last_error=None,
                ai_route_policy_version_id=route_policy_version_id,
            )
            .execution_options(skip_organization_scope=True, synchronize_session=False)
        )
        if claimed_update.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        return ClaimedResumeSummaryJob(
            job_id=job_id,
            organization_id=organization_id,
            resume_id=resume_id,
            fact_snapshot_id=fact_snapshot_id,
            facts_version=facts_version,
            ai_route_policy_version_id=route_policy_version_id,
        )


def _owned_running_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str,
    for_update: bool = False,
) -> ResumeSummaryJob | None:
    statement = select(ResumeSummaryJob).where(
        ResumeSummaryJob.id == job_id,
        ResumeSummaryJob.organization_id == organization_id,
        ResumeSummaryJob.status == SUMMARY_JOB_RUNNING,
        ResumeSummaryJob.lease_owner == worker_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _current_summary_for_job(
    session: Session,
    *,
    job: ResumeSummaryJob,
) -> ResumeSummary | None:
    return session.scalar(
        select(ResumeSummary)
        .where(
            ResumeSummary.resume_id == job.resume_id,
            ResumeSummary.organization_id == job.organization_id,
            ResumeSummary.fact_snapshot_id == job.fact_snapshot_id,
            ResumeSummary.facts_version == job.facts_version,
            ResumeSummary.is_current.is_(True),
            ResumeSummary.status == "succeeded",
        )
        .order_by(ResumeSummary.created_at.desc(), ResumeSummary.id.desc())
    )


def _finish_success(
    session: Session,
    *,
    job: ResumeSummaryJob,
    worker_id: str,
    summary_id: str,
) -> None:
    if job.status != SUMMARY_JOB_RUNNING or job.lease_owner != worker_id:
        raise ResumeSummaryJobError("resume_summary_job_lease_lost")
    job.status = SUMMARY_JOB_SUCCEEDED
    job.summary_id = summary_id
    job.next_attempt_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    job.completed_at = _utcnow()


def _finish_superseded(
    session: Session,
    *,
    job: ResumeSummaryJob,
    worker_id: str,
    error: str,
) -> None:
    if job.status != SUMMARY_JOB_RUNNING or job.lease_owner != worker_id:
        raise ResumeSummaryJobError("resume_summary_job_lease_lost")
    job.status = SUMMARY_JOB_SUPERSEDED
    job.next_attempt_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = error[:2000]
    job.completed_at = _utcnow()


def _process_claimed_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedResumeSummaryJob,
) -> None:
    try:
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                job = _owned_running_job(
                    session,
                    job_id=claimed.job_id,
                    worker_id=worker_id,
                    organization_id=claimed.organization_id,
                )
                if job is None:
                    session.rollback()
                    return
                if (
                    job.resume_id != claimed.resume_id
                    or job.fact_snapshot_id != claimed.fact_snapshot_id
                    or job.facts_version != claimed.facts_version
                    or job.ai_route_policy_version_id
                    != claimed.ai_route_policy_version_id
                ):
                    raise ResumeSummaryJobError("resume_summary_workspace_mismatch")
                resume = session.scalar(
                    select(Resume)
                    .where(Resume.id == job.resume_id)
                )
                if resume is None or resume.organization_id != claimed.organization_id:
                    raise ResumeSummaryJobError("resume_summary_workspace_mismatch")
                snapshot = session.scalar(
                    select(ResumeFactSnapshot).where(
                        ResumeFactSnapshot.id == job.fact_snapshot_id,
                        ResumeFactSnapshot.resume_id == resume.id,
                        ResumeFactSnapshot.organization_id == claimed.organization_id,
                    )
                )
                if (
                    snapshot is None
                    or snapshot.facts_version != job.facts_version
                    or resume.facts_version != job.facts_version
                    or resume.extraction_status != "ready"
                    or not resume.is_active
                ):
                    _finish_superseded(
                        session,
                        job=job,
                        worker_id=worker_id,
                        error="resume_changed_before_summary_completed",
                    )
                    session.commit()
                    return
                if has_unreliable_source_text(resume.quality_flags):
                    raise ResumeSummaryJobError("resume_source_text_unreliable")
                existing = _current_summary_for_job(session, job=job)
                if existing is not None:
                    _finish_success(
                        session,
                        job=job,
                        worker_id=worker_id,
                        summary_id=existing.id,
                    )
                    session.commit()
                    return
                # Do not retain a row lock or the validation transaction
                # while the provider is running.  The lease remains durable,
                # and both the summary service and the short final transaction
                # re-check the exact immutable fact revision.
                resume_id = resume.id
                session.rollback()
                response = generate_resume_summary(
                    session,
                    resume_id=resume_id,
                    settings=settings,
                    pinned_route_policy_version_id=claimed.ai_route_policy_version_id,
                    preserve_manual_current=True,
                    release_read_transaction=True,
                )
                job = _owned_running_job(
                    session,
                    job_id=claimed.job_id,
                    worker_id=worker_id,
                    organization_id=claimed.organization_id,
                    for_update=True,
                )
                if job is None:
                    # The candidate may have been deleted or the lease may
                    # have been cancelled while the provider was running.
                    # Roll back the uncommitted generated row in that case.
                    session.rollback()
                    return
                summary = session.get(ResumeSummary, response.summary_id)
                if (
                    summary is None
                    or summary.organization_id != claimed.organization_id
                    or summary.resume_id != claimed.resume_id
                    or summary.fact_snapshot_id != job.fact_snapshot_id
                    or summary.facts_version != job.facts_version
                ):
                    raise ResumeSummaryJobError("resume_summary_persist_mismatch")
                _finish_success(
                    session,
                    job=job,
                    worker_id=worker_id,
                    summary_id=summary.id,
                )
                session.commit()
    except DeepSeekProviderError as exc:
        _finish_failure(
            database,
            job_id=claimed.job_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=is_retryable_ai_transport_error(str(exc)),
        )
    except SummaryServiceError as exc:
        error = str(exc)
        if error == "resume_changed_before_summary_completed":
            _mark_superseded_after_error(
                database,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                error=error,
            )
            return
        _finish_failure(
            database,
            job_id=claimed.job_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error=error,
            retryable=is_retryable_ai_transport_error(error),
        )
    except ResumeSummaryJobError as exc:
        _finish_failure(
            database,
            job_id=claimed.job_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=False,
        )
    except Exception as exc:  # pragma: no cover - defensive containment for workers
        log_exception_event(
            "resume_summary_worker_failed",
            error_code="resume_summary_worker_error",
            exception=exc,
            job_id=claimed.job_id,
            workspace_id=claimed.organization_id,
        )
        _finish_failure(
            database,
            job_id=claimed.job_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error="resume_summary_worker_error",
            retryable=True,
        )


def _mark_superseded_after_error(
    database: Database,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str,
    error: str,
) -> None:
    with database.session_factory() as session:
        with _organization_session(session, organization_id):
            job = _owned_running_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                organization_id=organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return
            _finish_superseded(
                session,
                job=job,
                worker_id=worker_id,
                error=error,
            )
            session.commit()


def _finish_failure(
    database: Database,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str,
    error: str,
    retryable: bool,
) -> None:
    now = _utcnow()
    with database.session_factory() as session:
        with _organization_session(session, organization_id):
            job = _owned_running_job(
                session,
                job_id=job_id,
                worker_id=worker_id,
                organization_id=organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return
            if error in _UNAVAILABLE_ERRORS:
                status = SUMMARY_JOB_UNAVAILABLE
                next_attempt_at = None
                completed_at = now
            elif retryable and job.attempt_count < job.max_attempts:
                status = SUMMARY_JOB_QUEUED
                next_attempt_at = now + timedelta(
                    seconds=min(60, 2 ** max(job.attempt_count - 1, 0))
                )
                completed_at = None
            else:
                status = SUMMARY_JOB_FAILED
                next_attempt_at = None
                completed_at = now
            job.status = status
            job.next_attempt_at = next_attempt_at
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = error[:2000]
            job.completed_at = completed_at
            session.commit()


__all__ = [
    "SUMMARY_JOB_CANCELLED",
    "SUMMARY_JOB_FAILED",
    "SUMMARY_JOB_QUEUED",
    "SUMMARY_JOB_RUNNING",
    "SUMMARY_JOB_SUCCEEDED",
    "SUMMARY_JOB_SUPERSEDED",
    "SUMMARY_JOB_UNAVAILABLE",
    "ClaimedResumeSummaryJob",
    "ResumeSummaryJobError",
    "enqueue_resume_summary_job",
    "run_resume_summary_worker_once",
    "summary_generation_state",
]
