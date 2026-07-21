"""Lease-based physical cleanup for logically deleted candidate data.

The HTTP layer only marks candidate roots deleted.  This worker-owned module
waits for the recovery window, removes original files first, then removes the
dependent database rows in a deliberate foreign-key-safe order.  It never
uses the mailbox cache cleaner: originals and short-lived mail replicas have
separate path resolvers by design.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import (
    AiRun,
    Candidate,
    CandidateDataDeletionBatch,
    CandidateDataDeletionBatchItem,
    CandidateDataFileAccessGrant,
    CandidateDataPurgeJob,
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    JobMatch,
    JobMatchBatchItem,
    JobMatchRequirementResult,
    MailboxAttachmentContentIdentity,
    MailboxBackgroundJob,
    MailboxContentReplica,
    Resume,
    ResumeAiExtractionJob,
    ResumeEducation,
    ResumeExperience,
    ResumeFactSnapshot,
    ResumeLanguageCredential,
    ResumeReviewAction,
    ResumeScholarship,
    ResumeScore,
    ResumeScoreBatchItem,
    ResumeSkill,
    ResumeSourceBlock,
    ResumeSummary,
    ResumeUploadIdempotencyKey,
)
from app.services.candidate_data_lifecycle_service import _record_audit, as_utc, utcnow
from app.services.mailbox_retention_service import (
    MailboxRetentionError,
    resolve_mailbox_replica_path,
)
from app.services.resume_service import ResumeServiceError, resolve_uploaded_resume_path
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


PURGE_JOB_QUEUED = "queued"
PURGE_JOB_RUNNING = "running"
PURGE_JOB_COMPLETED = "completed"
PURGE_JOB_CANCELLED = "cancelled"
PURGE_JOB_FAILED = "failed"
_RETRYABLE_STATUSES = (PURGE_JOB_QUEUED, "retryable_failed")


class CandidateDataPurgeError(RuntimeError):
    """A stable, content-free cleanup failure code."""


@dataclass(frozen=True)
class ClaimedCandidateDataPurgeJob:
    job_id: str
    organization_id: str
    deletion_batch_id: str


def _retry_delay_seconds(attempt_count: int) -> int:
    return min(60 * 60, 30 * (2 ** max(0, attempt_count - 1)))


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _with_deleted(statement):
    return statement.execution_options(include_deleted_candidate_data=True)


def _recover_expired_purge_leases(session: Session, *, now: datetime) -> None:
    """Return abandoned claims to the queue without taking a global scope."""

    expired = session.scalars(
        select(CandidateDataPurgeJob)
        .where(
            CandidateDataPurgeJob.status == PURGE_JOB_RUNNING,
            CandidateDataPurgeJob.lease_expires_at.is_not(None),
            CandidateDataPurgeJob.lease_expires_at <= now,
        )
        .execution_options(skip_organization_scope=True)
    ).all()
    for job in expired:
        if not job.organization_id:
            continue
        with _organization_session(session, job.organization_id):
            retry = job.attempt_count < job.max_attempts
            recovered = session.execute(
                update(CandidateDataPurgeJob)
                .where(
                    CandidateDataPurgeJob.id == job.id,
                    CandidateDataPurgeJob.organization_id == job.organization_id,
                    CandidateDataPurgeJob.status == PURGE_JOB_RUNNING,
                    CandidateDataPurgeJob.lease_expires_at <= now,
                )
                .values(
                    status=PURGE_JOB_QUEUED if retry else PURGE_JOB_FAILED,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=(
                        now + timedelta(seconds=_retry_delay_seconds(job.attempt_count))
                        if retry
                        else None
                    ),
                    last_error="candidate_data_purge_lease_expired",
                    completed_at=None if retry else now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if recovered.rowcount == 1:
                # The filesystem fence is durable so a crash after it but
                # before database cleanup cannot leave a batch permanently
                # un-restorable/un-purgeable.  It is safe to return to
                # ``deleted`` here: this lease has expired and the recovery
                # deadline was already closed before fencing.
                session.execute(
                    update(CandidateDataDeletionBatch)
                    .where(
                        CandidateDataDeletionBatch.id == job.deletion_batch_id,
                        CandidateDataDeletionBatch.organization_id == job.organization_id,
                        CandidateDataDeletionBatch.status == "purging",
                    )
                    .values(status="deleted", updated_at=now)
                    .execution_options(synchronize_session=False)
                )
    if expired:
        session.commit()


def _claim_next_purge_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedCandidateDataPurgeJob | None:
    now = utcnow()
    with database.session_factory() as session:
        _recover_expired_purge_leases(session, now=now)
        candidate = session.scalar(
            select(CandidateDataPurgeJob)
            .where(
                CandidateDataPurgeJob.status.in_(_RETRYABLE_STATUSES),
                CandidateDataPurgeJob.attempt_count < CandidateDataPurgeJob.max_attempts,
                or_(
                    CandidateDataPurgeJob.next_attempt_at.is_(None),
                    CandidateDataPurgeJob.next_attempt_at <= now,
                ),
            )
            .order_by(
                CandidateDataPurgeJob.requested_at.asc(),
                CandidateDataPurgeJob.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        )
        if candidate is None or not candidate.organization_id:
            session.commit()
            return None
        organization_id = candidate.organization_id
        with _organization_session(session, organization_id):
            claimed = session.execute(
                update(CandidateDataPurgeJob)
                .where(
                    CandidateDataPurgeJob.id == candidate.id,
                    CandidateDataPurgeJob.organization_id == organization_id,
                    CandidateDataPurgeJob.status.in_(_RETRYABLE_STATUSES),
                    CandidateDataPurgeJob.attempt_count
                    < CandidateDataPurgeJob.max_attempts,
                    or_(
                        CandidateDataPurgeJob.next_attempt_at.is_(None),
                        CandidateDataPurgeJob.next_attempt_at <= now,
                    ),
                )
                .values(
                    status=PURGE_JOB_RUNNING,
                    attempt_count=CandidateDataPurgeJob.attempt_count + 1,
                    lease_owner=worker_id,
                    lease_expires_at=now
                    + timedelta(seconds=settings.candidate_data_purge_lease_seconds),
                    next_attempt_at=None,
                    last_error=None,
                    started_at=func.coalesce(CandidateDataPurgeJob.started_at, now),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return ClaimedCandidateDataPurgeJob(
                job_id=candidate.id,
                organization_id=organization_id,
                deletion_batch_id=candidate.deletion_batch_id,
            )


def _owned_running_job(
    session: Session,
    *,
    claimed: ClaimedCandidateDataPurgeJob,
    worker_id: str,
) -> CandidateDataPurgeJob | None:
    return session.scalar(
        select(CandidateDataPurgeJob).where(
            CandidateDataPurgeJob.id == claimed.job_id,
            CandidateDataPurgeJob.organization_id == claimed.organization_id,
            CandidateDataPurgeJob.status == PURGE_JOB_RUNNING,
            CandidateDataPurgeJob.lease_owner == worker_id,
        )
    )


def _mark_cancelled(
    session: Session,
    *,
    job: CandidateDataPurgeJob,
    now: datetime,
    reason: str,
) -> None:
    job.status = PURGE_JOB_CANCELLED
    job.lease_owner = None
    job.lease_expires_at = None
    job.next_attempt_at = None
    job.last_error = reason
    job.completed_at = now
    job.updated_at = now


def _fail_claim(
    session: Session,
    *,
    claimed: ClaimedCandidateDataPurgeJob,
    worker_id: str,
    error_code: str,
) -> None:
    now = utcnow()
    job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
    if job is None:
        session.rollback()
        return
    session.execute(
        update(CandidateDataDeletionBatch)
        .where(
            CandidateDataDeletionBatch.id == job.deletion_batch_id,
            CandidateDataDeletionBatch.organization_id == job.organization_id,
            CandidateDataDeletionBatch.status == "purging",
        )
        .values(status="deleted", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    retry = job.attempt_count < job.max_attempts
    job.status = PURGE_JOB_QUEUED if retry else PURGE_JOB_FAILED
    job.lease_owner = None
    job.lease_expires_at = None
    job.next_attempt_at = (
        now + timedelta(seconds=_retry_delay_seconds(job.attempt_count)) if retry else None
    )
    job.last_error = error_code
    job.completed_at = None if retry else now
    job.updated_at = now
    session.commit()


def _resume_originals_removed(
    resumes: list[Resume],
    *,
    settings: AppSettings,
) -> None:
    for resume in resumes:
        try:
            path = resolve_uploaded_resume_path(
                settings,
                storage_key=resume.storage_key,
                organization_id=resume.organization_id,
                require_file=False,
            )
            path.unlink(missing_ok=True)
        except (OSError, ResumeServiceError) as exc:
            raise CandidateDataPurgeError("candidate_data_storage_delete_failed") from exc


def _related_import_ids(session: Session, *, resume_ids: tuple[str, ...]) -> tuple[str, ...]:
    if not resume_ids:
        return ()
    known = set(
        session.scalars(
            select(EmailAttachmentImport.id).where(
                EmailAttachmentImport.resume_id.in_(resume_ids)
            )
        ).all()
    )
    # A forwarded duplicate can point at the canonical import rather than at
    # a resume.  Follow that finite self-reference so it cannot preserve mail
    # metadata after the canonical candidate is physically gone.
    while known:
        additions = set(
            session.scalars(
                select(EmailAttachmentImport.id).where(
                    EmailAttachmentImport.canonical_import_id.in_(tuple(known))
                )
            ).all()
        ) - known
        if not additions:
            break
        known.update(additions)
    return tuple(sorted(known))


def _mail_replicas_removed(
    session: Session,
    *,
    settings: AppSettings,
    import_ids: tuple[str, ...],
) -> list[MailboxContentReplica]:
    if not import_ids:
        return []
    replicas = session.scalars(
        select(MailboxContentReplica).where(
            MailboxContentReplica.email_attachment_import_id.in_(import_ids)
        )
    ).all()
    for replica in replicas:
        try:
            path = resolve_mailbox_replica_path(
                settings,
                storage_key=replica.storage_key,
                organization_id=replica.organization_id,
                require_file=False,
            )
            path.unlink(missing_ok=True)
        except (OSError, MailboxRetentionError) as exc:
            raise CandidateDataPurgeError("candidate_data_storage_delete_failed") from exc
    return replicas


def _scrub_ai_ledger(
    session: Session,
    *,
    organization_id: str,
    resume_ids: tuple[str, ...],
    extraction_job_ids: tuple[str, ...],
    score_item_ids: tuple[str, ...],
    match_item_ids: tuple[str, ...],
) -> None:
    """Keep cost totals but remove every candidate-facing business pointer."""

    predicates = []
    if resume_ids:
        predicates.extend(
            [
                and_(AiRun.business_ref_type == "resume", AiRun.business_ref_id.in_(resume_ids)),
                and_(
                    AiRun.business_ref_type.in_(("resume_summary", "resume_score")),
                    or_(
                        *[
                            AiRun.business_ref_id.like(f"{resume_id}:%")
                            for resume_id in resume_ids
                        ]
                    ),
                ),
                and_(
                    AiRun.business_ref_type == "job_match",
                    or_(
                        *[
                            AiRun.business_ref_id.like(f"%:{resume_id}:%")
                            for resume_id in resume_ids
                        ]
                    ),
                ),
            ]
        )
    if extraction_job_ids:
        predicates.append(
            and_(
                AiRun.business_ref_type == "resume_ai_extraction_job",
                AiRun.business_ref_id.in_(extraction_job_ids),
            )
        )
    if score_item_ids:
        predicates.append(
            and_(
                AiRun.business_ref_type == "resume_score_batch_item",
                AiRun.business_ref_id.in_(score_item_ids),
            )
        )
    if match_item_ids:
        predicates.append(
            and_(
                AiRun.business_ref_type == "job_match_batch_item",
                AiRun.business_ref_id.in_(match_item_ids),
            )
        )
    if not predicates:
        return
    session.execute(
        update(AiRun)
        .where(AiRun.organization_id == organization_id, or_(*predicates))
        .values(
            business_ref_type="candidate_data_purged",
            business_ref_id="redacted",
            source_snapshot_hmac=None,
        )
        .execution_options(synchronize_session=False)
    )


def _purge_database_rows(
    session: Session,
    *,
    batch: CandidateDataDeletionBatch,
    resumes: list[Resume],
    items: list[CandidateDataDeletionBatchItem],
    replicas: list[MailboxContentReplica],
    now: datetime,
) -> None:
    """Delete dependent rows only after all associated files are gone."""

    organization_id = organization_context_id(session)
    resume_ids = tuple(resume.id for resume in resumes)
    candidate_ids = tuple(sorted({item.candidate_id for item in items}))
    if not resume_ids:
        # A prior retry may have removed every target before crashing during
        # bookkeeping.  Complete the tombstone rather than leaving a forever
        # runnable job, while still removing a now-orphaned deleted root.
        for candidate_id in candidate_ids:
            remaining = session.scalar(
                _with_deleted(
                    select(Resume.id).where(Resume.candidate_id == candidate_id).limit(1)
                )
            )
            if remaining is None:
                session.execute(
                    delete(Candidate).where(
                        Candidate.id == candidate_id,
                        Candidate.organization_id == organization_id,
                        Candidate.deleted_at.is_not(None),
                    )
                )
        batch.status = "purged"
        batch.private_note = None
        batch.purged_at = now
        batch.updated_at = now
        _record_audit(
            session,
            actor_user_id=None,
            actor_kind="worker",
            action="candidate_data_physically_purged",
            target_type="deletion_batch",
            target_id=batch.id,
            source_kind="worker",
            result="completed",
            reason_code=batch.reason,
        )
        return

    import_ids = _related_import_ids(session, resume_ids=resume_ids)
    snapshot_ids = tuple(
        session.scalars(
            select(ResumeFactSnapshot.id).where(ResumeFactSnapshot.resume_id.in_(resume_ids))
        ).all()
    )
    score_ids = tuple(
        session.scalars(select(ResumeScore.id).where(ResumeScore.resume_id.in_(resume_ids))).all()
    )
    match_ids = tuple(
        session.scalars(select(JobMatch.id).where(JobMatch.resume_id.in_(resume_ids))).all()
    )
    summary_ids = tuple(
        session.scalars(select(ResumeSummary.id).where(ResumeSummary.resume_id.in_(resume_ids))).all()
    )
    extraction_job_ids = tuple(
        session.scalars(
            select(ResumeAiExtractionJob.id).where(ResumeAiExtractionJob.resume_id.in_(resume_ids))
        ).all()
    )
    score_item_ids = tuple(
        session.scalars(
            select(ResumeScoreBatchItem.id).where(ResumeScoreBatchItem.resume_id.in_(resume_ids))
        ).all()
    )
    match_item_ids = tuple(
        session.scalars(
            select(JobMatchBatchItem.id).where(JobMatchBatchItem.resume_id.in_(resume_ids))
        ).all()
    )

    _scrub_ai_ledger(
        session,
        organization_id=organization_id,
        resume_ids=resume_ids,
        extraction_job_ids=extraction_job_ids,
        score_item_ids=score_item_ids,
        match_item_ids=match_item_ids,
    )
    session.execute(
        update(CandidateDataFileAccessGrant)
        .where(
            CandidateDataFileAccessGrant.organization_id == organization_id,
            CandidateDataFileAccessGrant.resource_type == "resume_original",
            CandidateDataFileAccessGrant.resource_id.in_(resume_ids),
            CandidateDataFileAccessGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    # Access events remain in the content-free audit table.  The grant itself
    # is disposable authorization state and should not retain a durable link
    # to an erased resume.
    session.execute(
        delete(CandidateDataFileAccessGrant).where(
            CandidateDataFileAccessGrant.organization_id == organization_id,
            CandidateDataFileAccessGrant.resource_type == "resume_original",
            CandidateDataFileAccessGrant.resource_id.in_(resume_ids),
        )
    )
    session.execute(
        delete(JobMatchRequirementResult).where(
            JobMatchRequirementResult.job_match_id.in_(match_ids)
        )
    ) if match_ids else None
    session.execute(
        delete(JobMatchBatchItem).where(JobMatchBatchItem.resume_id.in_(resume_ids))
    )
    session.execute(
        delete(ResumeScoreBatchItem).where(ResumeScoreBatchItem.resume_id.in_(resume_ids))
    )
    if summary_ids:
        session.execute(
            update(ResumeSummary)
            .where(ResumeSummary.supersedes_id.in_(summary_ids))
            .values(supersedes_id=None)
            .execution_options(synchronize_session=False)
        )
    session.execute(delete(JobMatch).where(JobMatch.resume_id.in_(resume_ids)))
    session.execute(delete(ResumeScore).where(ResumeScore.resume_id.in_(resume_ids)))
    session.execute(delete(ResumeSummary).where(ResumeSummary.resume_id.in_(resume_ids)))
    if snapshot_ids:
        session.execute(delete(ResumeFactSnapshot).where(ResumeFactSnapshot.id.in_(snapshot_ids)))

    for model in (
        ResumeSourceBlock,
        ResumeEducation,
        ResumeExperience,
        ResumeSkill,
        ResumeLanguageCredential,
        ResumeScholarship,
        ResumeReviewAction,
    ):
        session.execute(delete(model).where(model.resume_id.in_(resume_ids)))
    session.execute(delete(ResumeAiExtractionJob).where(ResumeAiExtractionJob.resume_id.in_(resume_ids)))
    session.execute(
        delete(ResumeUploadIdempotencyKey).where(ResumeUploadIdempotencyKey.resume_id.in_(resume_ids))
    )

    if import_ids:
        session.execute(
            delete(MailboxBackgroundJob).where(
                MailboxBackgroundJob.email_attachment_import_id.in_(import_ids)
            )
        )
        session.execute(
            delete(EmailAttachmentImportAttempt).where(
                or_(
                    EmailAttachmentImportAttempt.email_attachment_import_id.in_(import_ids),
                    EmailAttachmentImportAttempt.resume_id.in_(resume_ids),
                )
            )
        )
        session.execute(
            delete(MailboxContentReplica).where(
                MailboxContentReplica.email_attachment_import_id.in_(import_ids)
            )
        )
        session.execute(
            delete(MailboxAttachmentContentIdentity).where(
                or_(
                    MailboxAttachmentContentIdentity.processing_import_id.in_(import_ids),
                    MailboxAttachmentContentIdentity.canonical_import_id.in_(import_ids),
                    MailboxAttachmentContentIdentity.canonical_resume_id.in_(resume_ids),
                )
            )
        )
        session.execute(
            update(EmailAttachmentImport)
            .where(EmailAttachmentImport.canonical_import_id.in_(import_ids))
            .values(canonical_import_id=None)
            .execution_options(synchronize_session=False)
        )
        session.execute(delete(EmailAttachmentImport).where(EmailAttachmentImport.id.in_(import_ids)))
    elif replicas:
        # Defensive cleanup for legacy rows whose import had already gone.
        session.execute(
            delete(MailboxContentReplica).where(
                MailboxContentReplica.id.in_(tuple(replica.id for replica in replicas))
            )
        )

    session.execute(delete(Resume).where(Resume.id.in_(resume_ids)))
    for candidate_id in candidate_ids:
        remaining = session.scalar(
            _with_deleted(
                select(Resume.id).where(Resume.candidate_id == candidate_id).limit(1)
            )
        )
        if remaining is None:
            session.execute(
                delete(Candidate).where(
                    Candidate.id == candidate_id,
                    Candidate.organization_id == organization_id,
                    Candidate.deleted_at.is_not(None),
                )
            )

    batch.status = "purged"
    batch.private_note = None
    batch.purged_at = now
    batch.updated_at = now
    _record_audit(
        session,
        actor_user_id=None,
        actor_kind="worker",
        action="candidate_data_physically_purged",
        target_type="deletion_batch",
        target_id=batch.id,
        source_kind="worker",
        result="completed",
        reason_code=batch.reason,
    )


def _process_claimed_purge_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedCandidateDataPurgeJob,
) -> None:
    try:
        # Persist a delete -> purging fence before resolving or unlinking a
        # single path.  Restore has the inverse delete -> restoring CAS, so
        # whichever transition commits first owns the next irreversible step.
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
                if job is None:
                    session.rollback()
                    return
                batch = session.scalar(
                    select(CandidateDataDeletionBatch).where(
                        CandidateDataDeletionBatch.id == claimed.deletion_batch_id
                    )
                )
                now = utcnow()
                if batch is None or batch.status != "deleted":
                    _mark_cancelled(
                        session,
                        job=job,
                        now=now,
                        reason="candidate_data_deletion_not_purgeable",
                    )
                    session.commit()
                    return
                purge_after_at = as_utc(batch.purge_after_at)
                recovery_deadline_at = as_utc(batch.recovery_deadline_at)
                if (
                    purge_after_at is None
                    or recovery_deadline_at is None
                    or purge_after_at > now
                    or recovery_deadline_at > now
                ):
                    job.status = PURGE_JOB_QUEUED
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.next_attempt_at = batch.purge_after_at
                    job.updated_at = now
                    session.commit()
                    return
                fenced = session.execute(
                    update(CandidateDataDeletionBatch)
                    .where(
                        CandidateDataDeletionBatch.id == batch.id,
                        CandidateDataDeletionBatch.organization_id
                        == claimed.organization_id,
                        CandidateDataDeletionBatch.status == "deleted",
                        CandidateDataDeletionBatch.purge_after_at <= now,
                        CandidateDataDeletionBatch.recovery_deadline_at <= now,
                    )
                    .values(status="purging", updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if fenced.rowcount != 1:
                    session.expire_all()
                    batch = session.scalar(
                        select(CandidateDataDeletionBatch).where(
                            CandidateDataDeletionBatch.id
                            == claimed.deletion_batch_id
                        )
                    )
                    if batch is None or batch.status in {
                        "restoring",
                        "restored",
                        "purged",
                    }:
                        _mark_cancelled(
                            session,
                            job=job,
                            now=utcnow(),
                            reason="candidate_data_deletion_not_purgeable",
                        )
                    else:
                        # A concurrent state update won but did not complete a
                        # terminal transition.  Leave the data hidden and let
                        # the durable job retry instead of touching files.
                        job.status = PURGE_JOB_QUEUED
                        job.lease_owner = None
                        job.lease_expires_at = None
                        job.next_attempt_at = utcnow() + timedelta(seconds=30)
                        job.updated_at = utcnow()
                    session.commit()
                    return

                # Commit the fence before filesystem work.  A restorer can no
                # longer pass its own conditional transition once this point
                # is durable, and a worker crash is recovered by lease repair.
                session.commit()

        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
                if job is None:
                    session.rollback()
                    return
                batch = session.scalar(
                    select(CandidateDataDeletionBatch).where(
                        CandidateDataDeletionBatch.id == claimed.deletion_batch_id,
                        CandidateDataDeletionBatch.status == "purging",
                    )
                )
                if batch is None:
                    session.rollback()
                    return
                items = session.scalars(
                    select(CandidateDataDeletionBatchItem)
                    .where(CandidateDataDeletionBatchItem.deletion_batch_id == batch.id)
                    .order_by(CandidateDataDeletionBatchItem.created_at, CandidateDataDeletionBatchItem.id)
                ).all()
                resume_ids = tuple(item.resume_id for item in items)
                resumes = session.scalars(
                    _with_deleted(
                        select(Resume).where(
                            Resume.id.in_(resume_ids),
                            Resume.deletion_batch_id == batch.id,
                            Resume.deleted_at.is_not(None),
                        )
                    )
                ).all() if resume_ids else []

                # Do filesystem work before removing the corresponding
                # storage_key from the database.  A missing file is already
                # clean; a failure leaves all candidate data hidden and the
                # leased job retryable.
                import_ids = _related_import_ids(
                    session,
                    resume_ids=tuple(resume.id for resume in resumes),
                )
                replicas = _mail_replicas_removed(
                    session,
                    settings=settings,
                    import_ids=import_ids,
                )
                _resume_originals_removed(resumes, settings=settings)
                job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
                batch = session.scalar(
                    select(CandidateDataDeletionBatch)
                    .where(
                        CandidateDataDeletionBatch.id == claimed.deletion_batch_id,
                        CandidateDataDeletionBatch.status == "purging",
                    )
                    .execution_options(populate_existing=True)
                )
                if job is None or batch is None:
                    session.rollback()
                    return
                _purge_database_rows(
                    session,
                    batch=batch,
                    resumes=resumes,
                    items=items,
                    replicas=replicas,
                    now=utcnow(),
                )
                job.status = PURGE_JOB_COMPLETED
                job.lease_owner = None
                job.lease_expires_at = None
                job.next_attempt_at = None
                job.last_error = None
                job.completed_at = utcnow()
                job.updated_at = job.completed_at
                session.commit()
    except CandidateDataPurgeError as exc:
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                _fail_claim(
                    session,
                    claimed=claimed,
                    worker_id=worker_id,
                    error_code=str(exc),
                )
    except Exception:
        # Keep details out of the durable candidate-data record; logs remain
        # available to operators without persisting raw storage/provider text.
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                _fail_claim(
                    session,
                    claimed=claimed,
                    worker_id=worker_id,
                    error_code="candidate_data_purge_failed",
                )


def run_candidate_data_purge_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one due physical purge."""

    claimed = _claim_next_purge_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_purge_job(
        database,
        settings=settings,
        worker_id=worker_id,
        claimed=claimed,
    )
    return True


__all__ = [
    "CandidateDataPurgeError",
    "ClaimedCandidateDataPurgeJob",
    "run_candidate_data_purge_worker_once",
]
