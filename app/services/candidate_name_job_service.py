"""Durable, workspace-scoped source-grounded candidate-name completion.

Candidate names are helpful presentation metadata, but they must never become
an implicit prerequisite for a usable resume. This queue is therefore
independent from document normalization and structured-fact extraction: a
provider timeout, an unclear header, or a rejected name can only affect this
task's own status and never retract facts, summaries, scores, or eligibility.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import Candidate, CandidateNameExtractionJob, Resume, ResumeSourceBlock
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
    resolve_active_route_policy_version_id,
)
from app.services.ai_retry_policy import is_retryable_ai_transport_error
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    EvidenceBlock,
    extract_resume_candidate_name,
)
from app.services.resume_eligibility import has_unreliable_source_text
from app.services.resume_service import (
    FactValidationError,
    _assert_raw_value_grounded,
    _source_text_by_ids,
)
from app.services.workspace_background_lane_service import (
    acquire_workspace_background_lane,
    fair_available_workspace_ids,
    release_workspace_background_lane,
    release_workspace_lane_for_inactive_job,
)
from app.tenant_scope import clear_organization_context, set_organization_context


logger = logging.getLogger(__name__)

CANDIDATE_NAME_JOB_QUEUED = "queued"
CANDIDATE_NAME_JOB_RUNNING = "running"
CANDIDATE_NAME_JOB_SUCCEEDED = "succeeded"
CANDIDATE_NAME_JOB_SKIPPED = "skipped"
CANDIDATE_NAME_JOB_CANCELLED = "cancelled"
CANDIDATE_NAME_JOB_FAILED = "failed"
CANDIDATE_NAME_JOB_UNAVAILABLE = "unavailable"
CANDIDATE_NAME_JOB_SUPERSEDED = "superseded"

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


class CandidateNameExtractionJobError(RuntimeError):
    """A stable, content-free candidate-name task failure."""


@dataclass(frozen=True)
class ClaimedCandidateNameExtractionJob:
    job_id: str
    organization_id: str
    resume_id: str
    ai_route_policy_version_id: str | None
    workspace_lane_token: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Scope every post-claim read and write to one workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _has_candidate_name(candidate: Candidate | None) -> bool:
    return bool(candidate and candidate.display_name and candidate.display_name.strip())


def _resume_is_current_for_candidate_name(
    resume: Resume,
    candidate: Candidate | None,
) -> bool:
    """Return whether this source may still set the shared display name.

    A candidate can have several resume versions. Only the current ready
    version may supply a previously blank display name; otherwise an old
    queued task could win a race after a newer version has become active.
    This is intentionally checked both before provider I/O and immediately
    before persistence.
    """

    return bool(
        candidate is not None
        and candidate.deleted_at is None
        and resume.is_active
        and resume.extraction_status == "ready"
        and resume.deleted_at is None
    )


def candidate_name_extraction_state(resume: Resume) -> tuple[str | None, str | None]:
    """Return a UI-safe state for one resume's candidate-name completion.

    A name written by the structured-facts worker predates this dedicated
    queue, but is still semantically complete to callers. This keeps UI/API
    consumers from treating already named historical candidates as pending.
    """

    if _has_candidate_name(resume.candidate):
        return CANDIDATE_NAME_JOB_SUCCEEDED, None
    job = resume.candidate_name_extraction_job
    if job is None:
        return None, None
    return job.status, job.last_error


def _route_pin_for_new_candidate_name_job(
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
                feature="candidate_name_backfill",
            ),
            None,
        )
    except AiGatewayError as exc:
        return None, str(exc)


def enqueue_candidate_name_extraction_job(
    session: Session,
    *,
    resume: Resume,
    settings: AppSettings,
) -> CandidateNameExtractionJob | None:
    """Create one name-only task without invoking a provider or committing.

    This is safe inside the structured-facts transaction. Existing jobs are
    returned unchanged, and an already-present candidate name always wins over
    automatic extraction.
    """

    # The task is deliberately downstream of successful, trusted facts. It
    # must not introduce an additional model call for an inactive, deleted,
    # or text-unreliable document merely because a caller still holds an ORM
    # instance for it.
    if (
        not resume.is_active
        or resume.extraction_status != "ready"
        or resume.deleted_at is not None
        or has_unreliable_source_text(resume.quality_flags)
    ):
        return None

    candidate = session.scalar(
        select(Candidate).where(
            Candidate.id == resume.candidate_id,
            Candidate.organization_id == resume.organization_id,
        )
    )
    if (
        candidate is None
        or candidate.deleted_at is not None
        or _has_candidate_name(candidate)
    ):
        return None

    existing = session.scalar(
        select(CandidateNameExtractionJob).where(
            CandidateNameExtractionJob.resume_id == resume.id,
            CandidateNameExtractionJob.organization_id == resume.organization_id,
        )
    )
    if existing is not None:
        return existing

    now = _utcnow()
    route_policy_version_id, availability_error = _route_pin_for_new_candidate_name_job(
        session,
        settings=settings,
    )
    job = CandidateNameExtractionJob(
        organization_id=resume.organization_id,
        resume_id=resume.id,
        ai_route_policy_version_id=route_policy_version_id,
        status=(
            CANDIDATE_NAME_JOB_QUEUED
            if availability_error is None
            else CANDIDATE_NAME_JOB_UNAVAILABLE
        ),
        attempt_count=0,
        max_attempts=max(1, settings.ai_extraction_job_max_attempts),
        next_attempt_at=now if availability_error is None else None,
        last_error=availability_error,
        requested_at=now,
    )
    try:
        # The unique resume key closes the concurrent caller race without
        # committing the facts transaction that requested this task.
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(CandidateNameExtractionJob).where(
                CandidateNameExtractionJob.resume_id == resume.id,
                CandidateNameExtractionJob.organization_id == resume.organization_id,
            )
        )
        if existing is not None:
            return existing
        raise
    return job


def run_candidate_name_extraction_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one independent candidate-name task."""

    claimed = _claim_next_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    try:
        _process_claimed_job(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
        )
    finally:
        with database.session_factory() as session:
            release_workspace_background_lane(
                session,
                organization_id=claimed.organization_id,
                lease_token=claimed.workspace_lane_token,
            )
            session.commit()
    return True


def _mark_jobs_unavailable_without_key(session: Session, *, now: datetime) -> None:
    for status in (CANDIDATE_NAME_JOB_QUEUED, CANDIDATE_NAME_JOB_RUNNING):
        statement = update(CandidateNameExtractionJob).where(
            CandidateNameExtractionJob.status == status
        )
        if status == CANDIDATE_NAME_JOB_RUNNING:
            statement = statement.where(
                CandidateNameExtractionJob.lease_expires_at.is_not(None),
                CandidateNameExtractionJob.lease_expires_at <= now,
            )
        session.execute(
            statement.values(
                status=CANDIDATE_NAME_JOB_UNAVAILABLE,
                next_attempt_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error=_NO_KEY_ERROR,
                completed_at=now,
            ).execution_options(skip_organization_scope=True)
        )


def _recover_expired_leases(session: Session, *, now: datetime) -> None:
    expired = and_(
        CandidateNameExtractionJob.status == CANDIDATE_NAME_JOB_RUNNING,
        CandidateNameExtractionJob.lease_expires_at.is_not(None),
        CandidateNameExtractionJob.lease_expires_at <= now,
    )
    expired_jobs = session.execute(
        select(
            CandidateNameExtractionJob.id,
            CandidateNameExtractionJob.organization_id,
        )
        .where(expired)
        .execution_options(skip_organization_scope=True)
    ).all()
    session.execute(
        update(CandidateNameExtractionJob)
        .where(
            expired,
            CandidateNameExtractionJob.attempt_count
            >= CandidateNameExtractionJob.max_attempts,
        )
        .values(
            status=CANDIDATE_NAME_JOB_FAILED,
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error="candidate_name_worker_lease_expired",
            completed_at=now,
        )
        .execution_options(skip_organization_scope=True)
    )
    session.execute(
        update(CandidateNameExtractionJob)
        .where(
            expired,
            CandidateNameExtractionJob.attempt_count
            < CandidateNameExtractionJob.max_attempts,
        )
        .values(
            status=CANDIDATE_NAME_JOB_QUEUED,
            next_attempt_at=now,
            lease_owner=None,
            lease_expires_at=None,
            last_error="candidate_name_worker_lease_expired",
            completed_at=None,
        )
        .execution_options(skip_organization_scope=True)
    )
    for job_id, organization_id in expired_jobs:
        if organization_id:
            release_workspace_lane_for_inactive_job(
                session,
                job_model=CandidateNameExtractionJob,
                job_id=job_id,
                organization_id=organization_id,
                job_kind="candidate_name_extraction",
                running_status=CANDIDATE_NAME_JOB_RUNNING,
                now=now,
            )


def _claim_next_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedCandidateNameExtractionJob | None:
    now = _utcnow()
    with database.session_factory() as session:
        if not ai_gateway_credentials_configured(settings):
            _mark_jobs_unavailable_without_key(session, now=now)
            session.commit()
            return None

        # Restoring credentials revives only the explicit missing-key state.
        # A disabled or unpublished route still requires an intentional route
        # repair before the task may run again.
        session.execute(
            update(CandidateNameExtractionJob)
            .where(
                CandidateNameExtractionJob.status == CANDIDATE_NAME_JOB_UNAVAILABLE,
                CandidateNameExtractionJob.last_error == _NO_KEY_ERROR,
            )
            .values(
                status=CANDIDATE_NAME_JOB_QUEUED,
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
            CandidateNameExtractionJob.status == CANDIDATE_NAME_JOB_QUEUED,
            CandidateNameExtractionJob.attempt_count
            < CandidateNameExtractionJob.max_attempts,
            or_(
                CandidateNameExtractionJob.next_attempt_at.is_(None),
                CandidateNameExtractionJob.next_attempt_at <= now,
            ),
        )
        missing_workspace_job_id = session.scalar(
            select(CandidateNameExtractionJob.id)
            .where(
                eligible,
                CandidateNameExtractionJob.organization_id.is_(None),
            )
            .order_by(
                CandidateNameExtractionJob.next_attempt_at.asc(),
                CandidateNameExtractionJob.requested_at.asc(),
                CandidateNameExtractionJob.id.asc(),
            )
            .limit(1)
            .execution_options(skip_organization_scope=True)
        )
        if missing_workspace_job_id is not None:
            session.execute(
                update(CandidateNameExtractionJob)
                .where(CandidateNameExtractionJob.id == missing_workspace_job_id)
                .values(
                    status=CANDIDATE_NAME_JOB_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="candidate_name_workspace_missing",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return None

        organization_ids = fair_available_workspace_ids(
            session,
            source=CandidateNameExtractionJob,
            organization_id_column=CandidateNameExtractionJob.organization_id,
            eligible=eligible,
            next_attempt_at_column=CandidateNameExtractionJob.next_attempt_at,
            requested_at_column=CandidateNameExtractionJob.requested_at,
            now=now,
        )
        if not organization_ids:
            session.commit()
            return None

        for organization_id in organization_ids:
            candidate = session.execute(
            select(
                CandidateNameExtractionJob.id,
                CandidateNameExtractionJob.resume_id,
                CandidateNameExtractionJob.ai_route_policy_version_id,
            )
            .where(
                eligible,
                CandidateNameExtractionJob.organization_id == organization_id,
            )
            .order_by(
                CandidateNameExtractionJob.next_attempt_at.asc(),
                CandidateNameExtractionJob.requested_at.asc(),
                CandidateNameExtractionJob.id.asc(),
            )
            .limit(1)
            .execution_options(skip_organization_scope=True)
            ).one_or_none()
            if candidate is None:
                continue
            job_id, resume_id, route_policy_version_id = candidate
            if route_policy_version_id is None:
                try:
                    route_policy_version_id = resolve_active_route_policy_version_id(
                        session,
                        settings=settings,
                        feature="candidate_name_backfill",
                    )
                except AiGatewayError as exc:
                    session.execute(
                        update(CandidateNameExtractionJob)
                        .where(
                            CandidateNameExtractionJob.id == job_id,
                            CandidateNameExtractionJob.organization_id == organization_id,
                            eligible,
                        )
                        .values(
                            status=CANDIDATE_NAME_JOB_UNAVAILABLE,
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

            lane = acquire_workspace_background_lane(
                session,
                organization_id=organization_id,
                worker_id=worker_id,
                job_kind="candidate_name_extraction",
                job_id=job_id,
                lease_seconds=max(
                    settings.worker_workspace_lane_lease_seconds,
                    settings.ai_extraction_job_lease_seconds,
                ),
                now=now,
            )
            if lane is None:
                continue
            lease_expires_at = now + timedelta(
                seconds=settings.ai_extraction_job_lease_seconds
            )
            claimed_update = session.execute(
                update(CandidateNameExtractionJob)
                .where(
                    CandidateNameExtractionJob.id == job_id,
                    CandidateNameExtractionJob.organization_id == organization_id,
                    eligible,
                )
                .values(
                    status=CANDIDATE_NAME_JOB_RUNNING,
                    attempt_count=CandidateNameExtractionJob.attempt_count + 1,
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
            return ClaimedCandidateNameExtractionJob(
                job_id=job_id,
                organization_id=organization_id,
                resume_id=resume_id,
                ai_route_policy_version_id=route_policy_version_id,
                workspace_lane_token=lane.lease_token,
            )
        session.commit()
        return None


def _owned_running_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str,
    for_update: bool = False,
) -> CandidateNameExtractionJob | None:
    statement = select(CandidateNameExtractionJob).where(
        CandidateNameExtractionJob.id == job_id,
        CandidateNameExtractionJob.organization_id == organization_id,
        CandidateNameExtractionJob.status == CANDIDATE_NAME_JOB_RUNNING,
        CandidateNameExtractionJob.lease_owner == worker_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _finish_succeeded(
    session: Session,
    *,
    job: CandidateNameExtractionJob,
    worker_id: str,
) -> None:
    if job.status != CANDIDATE_NAME_JOB_RUNNING or job.lease_owner != worker_id:
        raise CandidateNameExtractionJobError("candidate_name_job_lease_lost")
    job.status = CANDIDATE_NAME_JOB_SUCCEEDED
    job.next_attempt_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    job.completed_at = _utcnow()


def _finish_skipped(
    session: Session,
    *,
    job: CandidateNameExtractionJob,
    worker_id: str,
    reason: str,
) -> None:
    if job.status != CANDIDATE_NAME_JOB_RUNNING or job.lease_owner != worker_id:
        raise CandidateNameExtractionJobError("candidate_name_job_lease_lost")
    job.status = CANDIDATE_NAME_JOB_SKIPPED
    job.next_attempt_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = reason[:2000]
    job.completed_at = _utcnow()


def _finish_superseded(
    session: Session,
    *,
    job: CandidateNameExtractionJob,
    worker_id: str,
    reason: str,
) -> None:
    if job.status != CANDIDATE_NAME_JOB_RUNNING or job.lease_owner != worker_id:
        raise CandidateNameExtractionJobError("candidate_name_job_lease_lost")
    job.status = CANDIDATE_NAME_JOB_SUPERSEDED
    job.next_attempt_at = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = reason[:2000]
    job.completed_at = _utcnow()


def _load_claimed_source_blocks(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedCandidateNameExtractionJob,
) -> list[EvidenceBlock] | None:
    """Read one workspace-owned source without holding a transaction for AI."""

    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return None
            if (
                job.resume_id != claimed.resume_id
                or job.ai_route_policy_version_id
                != claimed.ai_route_policy_version_id
            ):
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_job_changed_before_run",
                )
                session.commit()
                return None
            resume = session.scalar(select(Resume).where(Resume.id == job.resume_id))
            if resume is None:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_resume_not_found",
                )
                session.commit()
                return None
            if resume.organization_id != claimed.organization_id:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_workspace_mismatch",
                )
                session.commit()
                return None
            candidate = session.scalar(
                select(Candidate).where(Candidate.id == resume.candidate_id)
            )
            if candidate is None or candidate.organization_id != claimed.organization_id:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_workspace_mismatch",
                )
                session.commit()
                return None
            if not _resume_is_current_for_candidate_name(resume, candidate):
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_resume_not_current",
                )
                session.commit()
                return None
            if _has_candidate_name(candidate):
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_already_set",
                )
                session.commit()
                return None
            if has_unreliable_source_text(resume.quality_flags):
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="resume_source_text_unreliable",
                )
                session.commit()
                return None
            source_blocks = session.scalars(
                select(ResumeSourceBlock)
                .where(ResumeSourceBlock.resume_id == resume.id)
                .order_by(ResumeSourceBlock.page_no, ResumeSourceBlock.block_id)
            ).all()
            if not source_blocks:
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="resume_has_no_native_text_for_candidate_name",
                )
                session.commit()
                return None
            blocks = [
                EvidenceBlock(
                    block_id=block.block_id,
                    page_no=block.page_no,
                    block_type=block.block_type,
                    text=block.text,
                )
                for block in source_blocks
            ]
            # No row lock or read transaction stays open during provider I/O.
            session.rollback()
            return blocks


def _persist_candidate_name(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedCandidateNameExtractionJob,
    value: str,
    evidence_block_ids: list[str],
) -> None:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return
            if (
                job.resume_id != claimed.resume_id
                or job.ai_route_policy_version_id
                != claimed.ai_route_policy_version_id
            ):
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_job_changed_before_completion",
                )
                session.commit()
                return
            resume = session.scalar(
                select(Resume).where(Resume.id == job.resume_id).with_for_update()
            )
            if resume is None:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_resume_not_found",
                )
                session.commit()
                return
            if resume.organization_id != claimed.organization_id:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_workspace_mismatch",
                )
                session.commit()
                return
            candidate = session.scalar(
                select(Candidate)
                .where(Candidate.id == resume.candidate_id)
                .with_for_update()
            )
            if candidate is None or candidate.organization_id != claimed.organization_id:
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_workspace_mismatch",
                )
                session.commit()
                return
            if not _resume_is_current_for_candidate_name(resume, candidate):
                _finish_superseded(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_resume_not_current",
                )
                session.commit()
                return
            if _has_candidate_name(candidate):
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_already_set",
                )
                session.commit()
                return
            if has_unreliable_source_text(resume.quality_flags):
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="resume_source_text_unreliable",
                )
                session.commit()
                return
            try:
                evidence_text = _source_text_by_ids(
                    session,
                    resume_id=resume.id,
                    block_ids=evidence_block_ids,
                )
                _assert_raw_value_grounded(
                    value=value,
                    source_text=evidence_text,
                    label="candidate_name_raw",
                )
            except FactValidationError:
                # Do not retry an ungrounded provider result and never make a
                # name guess from another source such as a filename or email.
                _finish_skipped(
                    session,
                    job=job,
                    worker_id=worker_id,
                    reason="candidate_name_not_grounded",
                )
                session.commit()
                return
            candidate.display_name = value.strip()
            _finish_succeeded(session, job=job, worker_id=worker_id)
            session.commit()


def _mark_skipped_after_empty_result(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedCandidateNameExtractionJob,
) -> None:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return
            _finish_skipped(
                session,
                job=job,
                worker_id=worker_id,
                reason="candidate_name_not_explicit",
            )
            session.commit()


def _process_claimed_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedCandidateNameExtractionJob,
) -> None:
    try:
        blocks = _load_claimed_source_blocks(
            database,
            worker_id=worker_id,
            claimed=claimed,
        )
    except CandidateNameExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=str(exc),
            retryable=False,
        )
        return
    except Exception:  # pragma: no cover - defensive containment for workers
        logger.exception("Unable to load candidate-name source text")
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error="candidate_name_source_load_failed",
            retryable=True,
        )
        return
    if blocks is None:
        return

    compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
        gateway_prompt_transport_arguments(settings)
    )
    try:
        with database.session_factory() as gateway_session:
            with _organization_session(gateway_session, claimed.organization_id):
                with ai_gateway_execution(
                    gateway_session,
                    settings=settings,
                    spec=AiExecutionSpec(
                        feature="candidate_name_backfill",
                        business_ref_type="candidate_name_extraction_job",
                        business_ref_id=claimed.job_id,
                        contract_version="candidate_name.v1",
                        pinned_route_policy_version_id=(
                            claimed.ai_route_policy_version_id
                        ),
                    ),
                ):
                    draft = extract_resume_candidate_name(
                        api_key=compatibility_api_key,
                        model=compatibility_model,
                        timeout_seconds=compatibility_timeout_seconds,
                        blocks=blocks,
                    )
    except (DeepSeekProviderError, AiGatewayError) as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=str(exc),
            retryable=is_retryable_ai_transport_error(str(exc)),
        )
        return
    except Exception:  # pragma: no cover - defensive containment for workers
        logger.exception("Unexpected candidate-name worker failure")
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error="candidate_name_worker_error",
            retryable=True,
        )
        return

    try:
        if draft.value is None:
            _mark_skipped_after_empty_result(
                database,
                worker_id=worker_id,
                claimed=claimed,
            )
            return
        _persist_candidate_name(
            database,
            worker_id=worker_id,
            claimed=claimed,
            value=draft.value,
            evidence_block_ids=draft.evidence_block_ids,
        )
    except CandidateNameExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=str(exc),
            retryable=False,
        )
    except Exception:  # pragma: no cover - defensive containment for workers
        logger.exception("Unable to persist candidate-name completion")
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error="candidate_name_persist_failed",
            retryable=True,
        )


def _finish_failure(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedCandidateNameExtractionJob,
    error: str,
    retryable: bool,
) -> None:
    now = _utcnow()
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                session.rollback()
                return
            if error in _UNAVAILABLE_ERRORS:
                status = CANDIDATE_NAME_JOB_UNAVAILABLE
                next_attempt_at = None
                completed_at = now
            elif retryable and job.attempt_count < job.max_attempts:
                status = CANDIDATE_NAME_JOB_QUEUED
                next_attempt_at = now + timedelta(
                    seconds=min(60, 2 ** max(job.attempt_count - 1, 0))
                )
                completed_at = None
            else:
                status = CANDIDATE_NAME_JOB_FAILED
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
    "CANDIDATE_NAME_JOB_CANCELLED",
    "CANDIDATE_NAME_JOB_FAILED",
    "CANDIDATE_NAME_JOB_QUEUED",
    "CANDIDATE_NAME_JOB_RUNNING",
    "CANDIDATE_NAME_JOB_SKIPPED",
    "CANDIDATE_NAME_JOB_SUCCEEDED",
    "CANDIDATE_NAME_JOB_SUPERSEDED",
    "CANDIDATE_NAME_JOB_UNAVAILABLE",
    "CandidateNameExtractionJobError",
    "ClaimedCandidateNameExtractionJob",
    "candidate_name_extraction_state",
    "enqueue_candidate_name_extraction_job",
    "run_candidate_name_extraction_worker_once",
]
