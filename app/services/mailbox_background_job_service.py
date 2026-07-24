"""Durable, workspace-scoped background jobs for mailbox IMAP work.

HTTP endpoints only create records in ``mailbox_background_jobs`` and return a
pollable task.  The existing worker process claims those records before it
opens an IMAP connection or reads an attachment.  This deliberately avoids
FastAPI in-process background work: a web restart must not lose an accepted
mailbox synchronization or exact attachment retry.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Literal

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import (
    EmailAttachmentImport,
    MailboxBackgroundJob,
    MailboxConfig,
    MailboxOAuthCredential,
)
from app.schemas import (
    MailboxBackgroundJobBatchResponse,
    MailboxBackgroundJobHistoryResponse,
    MailboxBackgroundJobResponse,
)
from app.services.mailbox_import_service import (
    MailboxImportError,
    cleanup_expired_mailbox_oauth_intents,
    get_retryable_mailbox_attachment,
    mailbox_attachment_has_retryable_remote_source,
    mailbox_source_fingerprint,
    retry_mailbox_attachment,
    sync_mailbox,
)
from app.services.mailbox_retention_service import (
    protect_retained_failed_attachment_for_retry,
)
from app.services.mailbox_sync_alert_service import (
    record_terminal_sync_failure,
    resolve_mailbox_sync_alert,
)
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


MAILBOX_JOB_SYNC = "sync"
MAILBOX_JOB_ATTACHMENT_RETRY = "attachment_retry"
MAILBOX_JOB_QUEUED = "queued"
MAILBOX_JOB_RUNNING = "running"
MAILBOX_JOB_COMPLETED = "completed"
MAILBOX_JOB_FAILED = "failed"
_ACTIVE_JOB_STATUSES = (MAILBOX_JOB_QUEUED, MAILBOX_JOB_RUNNING)
_DEFAULT_MAX_ATTEMPTS = 3
# IMAP synchronization can process a bounded batch of attachments.  Its lease
# is intentionally much longer than a single network timeout, so a healthy
# worker cannot be superseded just because it is parsing a large batch.
_MAILBOX_JOB_LEASE_SECONDS = 15 * 60
# Keep enough task history for operational diagnosis without allowing the
# ten-minute scheduler to grow this table forever.  The cap is workspace-wide
# and applies only to terminal rows; queued/running work is never pruned.
_TERMINAL_JOB_RETENTION = timedelta(days=30)
_TERMINAL_JOB_MAX_PER_ORGANIZATION = 5_000
_TERMINAL_JOB_PRUNE_BATCH_SIZE = 500

logger = logging.getLogger(__name__)

_TERMINAL_ERROR_CODES = frozenset(
    {
        "mailbox_config_not_found",
        "mailbox_config_archived",
        "mailbox_workspace_missing",
        "mailbox_workspace_mismatch",
        "mailbox_not_enabled",
        "mailbox_credentials_unavailable",
        "mailbox_credentials_key_invalid",
        # Refresh credentials cannot be repaired by retrying the same worker
        # job.  Mark the channel for a user-authorized reconnect instead of
        # repeatedly contacting the provider with an invalid token.
        "mailbox_oauth_reauthorization_required",
        "mailbox_oauth_not_configured",
        "mailbox_provider_oauth_not_supported",
        "mailbox_provider_not_supported",
        "mailbox_provider_not_available",
        "mailbox_imap_host_not_allowed",
        "mailbox_imap_port_not_allowed",
        "mailbox_imap_address_not_allowed",
        "mailbox_imap_dns_failed",
        "mailbox_imap_argument_invalid",
        "mailbox_imap_response_line_too_large",
        "mailbox_task_source_changed",
        "mailbox_source_epoch_changed",
        "mailbox_source_watermark_invalid",
        "mailbox_sync_in_progress",
        "mailbox_sync_claim_failed",
        "mailbox_search_response_too_large",
        "mailbox_message_too_large",
        "mailbox_message_headers_too_large",
        "mailbox_mime_structure_too_complex",
        "mailbox_attachment_count_exceeded",
        "mailbox_attachment_too_large",
        "mailbox_attachment_total_too_large",
        "mailbox_import_not_found",
        "mailbox_import_not_retryable",
        "attachment_validation_failed",
        "attachment_message_unavailable",
        "attachment_source_changed",
        "attachment_source_unavailable",
    }
)


@dataclass(frozen=True)
class ClaimedMailboxBackgroundJob:
    job_id: str
    organization_id: str
    mailbox_config_id: str
    email_attachment_import_id: str | None
    job_kind: str
    source_fingerprint: str | None


class _MailboxBackgroundJobLeaseLost(RuntimeError):
    """Raised inside IMAP work when another worker has already reclaimed it."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _job_response(
    job: MailboxBackgroundJob,
    *,
    deduplicated: bool = False,
) -> MailboxBackgroundJobResponse:
    return MailboxBackgroundJobResponse(
        job_id=job.id,
        mailbox_id=job.mailbox_config_id,
        job_kind=job.job_kind,  # type: ignore[arg-type]
        trigger_type=job.trigger_type,  # type: ignore[arg-type]
        status=job.status,  # type: ignore[arg-type]
        import_id=job.email_attachment_import_id,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        imported_count=job.imported_count,
        duplicate_count=job.duplicate_count,
        skipped_count=job.skipped_count,
        failed_count=job.failed_count,
        last_error=job.last_error,
        requested_at=job.requested_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        deduplicated=deduplicated,
    )


def _active_sync_job(
    session: Session,
    *,
    organization_id: str,
    mailbox_config_id: str,
) -> MailboxBackgroundJob | None:
    return session.scalar(
        select(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.organization_id == organization_id,
            MailboxBackgroundJob.mailbox_config_id == mailbox_config_id,
            MailboxBackgroundJob.job_kind == MAILBOX_JOB_SYNC,
            MailboxBackgroundJob.status.in_(_ACTIVE_JOB_STATUSES),
        )
        .order_by(MailboxBackgroundJob.requested_at.desc(), MailboxBackgroundJob.id.desc())
    )


def _active_retry_job(
    session: Session,
    *,
    organization_id: str,
    import_id: str,
) -> MailboxBackgroundJob | None:
    return session.scalar(
        select(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.organization_id == organization_id,
            MailboxBackgroundJob.email_attachment_import_id == import_id,
            MailboxBackgroundJob.job_kind == MAILBOX_JOB_ATTACHMENT_RETRY,
            MailboxBackgroundJob.status.in_(_ACTIVE_JOB_STATUSES),
        )
        .order_by(MailboxBackgroundJob.requested_at.desc(), MailboxBackgroundJob.id.desc())
    )


def _mailbox_config_or_error(
    session: Session,
    *,
    config_id: str,
    include_archived: bool = False,
) -> MailboxConfig:
    config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == config_id))
    if config is None:
        raise MailboxImportError("mailbox_config_not_found")
    if config.archived_at is not None and not include_archived:
        raise MailboxImportError("mailbox_config_archived")
    expected_organization_id = organization_context_id(session)
    if config.organization_id != expected_organization_id:
        raise MailboxImportError("mailbox_workspace_mismatch")
    return config


def _enqueue_sync_for_config(
    session: Session,
    *,
    config: MailboxConfig,
    trigger_type: Literal["manual", "scheduled"],
) -> MailboxBackgroundJobResponse:
    if not config.enabled:
        raise MailboxImportError("mailbox_not_enabled")
    organization_id = organization_context_id(session)
    if config.organization_id != organization_id:
        raise MailboxImportError("mailbox_workspace_mismatch")

    now = _utcnow()
    existing = _active_sync_job(
        session,
        organization_id=organization_id,
        mailbox_config_id=config.id,
    )
    if existing is not None:
        session.commit()
        return _job_response(existing, deduplicated=True)

    job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config.id,
        job_kind=MAILBOX_JOB_SYNC,
        trigger_type=trigger_type,
        source_fingerprint=mailbox_source_fingerprint(config),
        status=MAILBOX_JOB_QUEUED,
        max_attempts=_DEFAULT_MAX_ATTEMPTS,
        next_attempt_at=now,
        requested_at=now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = _active_sync_job(
            session,
            organization_id=organization_id,
            mailbox_config_id=config.id,
        )
        if existing is None:
            raise
        session.commit()
        return _job_response(existing, deduplicated=True)
    session.commit()
    return _job_response(job)


def enqueue_mailbox_sync_job(
    session: Session,
    *,
    settings: AppSettings,
    mailbox_config_id: str,
) -> MailboxBackgroundJobResponse:
    """Queue one manual mailbox synchronization without opening IMAP."""

    del settings  # Kept in the public signature for caller consistency.
    config = _mailbox_config_or_error(session, config_id=mailbox_config_id)
    return _enqueue_sync_for_config(
        session,
        config=config,
        trigger_type="manual",
    )


def enqueue_all_mailbox_sync_jobs(
    session: Session,
    *,
    settings: AppSettings,
) -> MailboxBackgroundJobBatchResponse:
    """Queue one independent sync per active named mailbox.

    The request never connects to IMAP.  Each result carries its mailbox ID so
    the caller can show progress without serializing unrelated channels.
    """

    del settings  # Kept in the public signature for caller consistency.
    configs = session.scalars(
        select(MailboxConfig)
        .where(MailboxConfig.enabled.is_(True), MailboxConfig.archived_at.is_(None))
        .order_by(MailboxConfig.created_at, MailboxConfig.id)
    ).all()
    responses = [
        _enqueue_sync_for_config(session, config=config, trigger_type="manual")
        for config in configs
    ]
    return MailboxBackgroundJobBatchResponse(
        items=responses,
        queued_count=sum(not item.deduplicated for item in responses),
        deduplicated_count=sum(item.deduplicated for item in responses),
    )


def enqueue_mailbox_attachment_retry_job(
    session: Session,
    *,
    settings: AppSettings,
    import_id: str,
) -> MailboxBackgroundJobResponse:
    """Queue an exact attachment retry without reading its content yet."""

    record = get_retryable_mailbox_attachment(session, import_id=import_id)
    organization_id = organization_context_id(session)
    if record.organization_id != organization_id:
        raise MailboxImportError("mailbox_import_not_found")
    config = _mailbox_config_or_error(
        session,
        config_id=record.mailbox_config_id,
        include_archived=True,
    )

    existing = _active_retry_job(
        session,
        organization_id=organization_id,
        import_id=record.id,
    )
    if existing is not None:
        return _job_response(existing, deduplicated=True)

    retained_copy_protection = protect_retained_failed_attachment_for_retry(
        session,
        attachment_import=record,
        protection_seconds=_MAILBOX_JOB_LEASE_SECONDS,
    )
    if (
        retained_copy_protection == "cleanup_claimed"
        or (
            retained_copy_protection == "not_found"
            and not mailbox_attachment_has_retryable_remote_source(record)
        )
    ):
        # Cleanup may already own the only retained source.  Do not commit a
        # job that can no longer perform the retry it promises.
        session.rollback()
        raise MailboxImportError("mailbox_import_not_retryable")

    now = _utcnow()
    job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config.id,
        email_attachment_import_id=record.id,
        job_kind=MAILBOX_JOB_ATTACHMENT_RETRY,
        trigger_type="manual",
        source_fingerprint=record.source_fingerprint,
        status=MAILBOX_JOB_QUEUED,
        max_attempts=_DEFAULT_MAX_ATTEMPTS,
        next_attempt_at=now,
        requested_at=now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = _active_retry_job(
            session,
            organization_id=organization_id,
            import_id=record.id,
        )
        if existing is None:
            raise
        return _job_response(existing, deduplicated=True)
    session.commit()
    return _job_response(job)


def get_mailbox_background_job(
    session: Session,
    *,
    job_id: str,
) -> MailboxBackgroundJobResponse:
    job = session.scalar(
        select(MailboxBackgroundJob).where(MailboxBackgroundJob.id == job_id)
    )
    if job is None:
        raise MailboxImportError("mailbox_background_job_not_found")
    return _job_response(job)


def list_mailbox_background_jobs(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    mailbox_config_id: str | None = None,
) -> MailboxBackgroundJobHistoryResponse:
    # A single bounded, stable page covers both active and terminal work.
    # Individual task polling remains available by ID, while an accidental
    # retry is still coalesced by the database's active-job unique indexes.
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    if mailbox_config_id is not None:
        _mailbox_config_or_error(session, config_id=mailbox_config_id)
    filters = []
    if mailbox_config_id is not None:
        filters.append(MailboxBackgroundJob.mailbox_config_id == mailbox_config_id)
    jobs = session.scalars(
        select(MailboxBackgroundJob)
        .where(*filters)
        .order_by(
            case(
                (MailboxBackgroundJob.status.in_(_ACTIVE_JOB_STATUSES), 0),
                else_=1,
            ),
            MailboxBackgroundJob.requested_at.desc(),
            MailboxBackgroundJob.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    total = session.scalar(
        select(func.count()).select_from(MailboxBackgroundJob).where(*filters)
    )
    return MailboxBackgroundJobHistoryResponse(
        items=[_job_response(job) for job in jobs],
        total=int(total or 0),
    )


def _prune_terminal_job_history(
    session: Session,
    *,
    now: datetime,
) -> int:
    """Delete a bounded batch of old terminal rows in the current workspace."""

    organization_id = organization_context_id(session)
    terminal_filter = (
        MailboxBackgroundJob.organization_id == organization_id,
        MailboxBackgroundJob.status.in_((MAILBOX_JOB_COMPLETED, MAILBOX_JOB_FAILED)),
    )
    cutoff = now - _TERMINAL_JOB_RETENTION
    expired_ids = list(
        session.scalars(
            select(MailboxBackgroundJob.id)
            .where(
                *terminal_filter,
                MailboxBackgroundJob.completed_at.is_not(None),
                MailboxBackgroundJob.completed_at <= cutoff,
            )
            .order_by(
                MailboxBackgroundJob.completed_at.asc(),
                MailboxBackgroundJob.id.asc(),
            )
            .limit(_TERMINAL_JOB_PRUNE_BATCH_SIZE)
        )
    )

    terminal_count = int(
        session.scalar(
            select(func.count())
            .select_from(MailboxBackgroundJob)
            .where(*terminal_filter)
        )
        or 0
    )
    overflow = max(
        0,
        terminal_count
        - len(expired_ids)
        - _TERMINAL_JOB_MAX_PER_ORGANIZATION,
    )
    remaining_capacity = _TERMINAL_JOB_PRUNE_BATCH_SIZE - len(expired_ids)
    if overflow and remaining_capacity:
        overflow_statement = (
            select(MailboxBackgroundJob.id)
            .where(*terminal_filter)
            .order_by(
                MailboxBackgroundJob.completed_at.asc(),
                MailboxBackgroundJob.requested_at.asc(),
                MailboxBackgroundJob.id.asc(),
            )
            .limit(min(overflow, remaining_capacity))
        )
        if expired_ids:
            overflow_statement = overflow_statement.where(
                MailboxBackgroundJob.id.not_in(expired_ids)
            )
        expired_ids.extend(session.scalars(overflow_statement).all())

    if not expired_ids:
        return 0
    deleted = session.execute(
        delete(MailboxBackgroundJob).where(
            MailboxBackgroundJob.organization_id == organization_id,
            MailboxBackgroundJob.id.in_(expired_ids),
            MailboxBackgroundJob.status.in_(
                (MAILBOX_JOB_COMPLETED, MAILBOX_JOB_FAILED)
            ),
        )
    )
    session.commit()
    return int(deleted.rowcount or 0)


def _prune_terminal_job_history_safely(
    session: Session,
    *,
    now: datetime,
) -> None:
    """Keep maintenance failure independent from an already durable result."""

    try:
        _prune_terminal_job_history(session, now=now)
    except Exception:
        session.rollback()
        logger.warning("mailbox_terminal_job_history_prune_failed", exc_info=True)


def enqueue_due_mailbox_sync_jobs(*, database: Database, settings: AppSettings) -> bool:
    """Have the scheduler enqueue at most one due IMAP sync, never execute it."""

    now = _utcnow()
    cutoff = now - timedelta(seconds=settings.mailbox_sync_interval_seconds)
    with database.session_factory() as session:
        # Browser-abandoned authorization attempts contain an encrypted PKCE
        # verifier. Keep their retention bounded without allowing a cleanup
        # fault to prevent unrelated mailbox scheduling.
        try:
            cleanup_expired_mailbox_oauth_intents(session, now=now)
        except SQLAlchemyError:
            session.rollback()
            logger.warning("mailbox_oauth_intent_cleanup_failed", exc_info=True)
        active_sync_job = exists(
            select(MailboxBackgroundJob.id).where(
                MailboxBackgroundJob.organization_id == MailboxConfig.organization_id,
                MailboxBackgroundJob.mailbox_config_id == MailboxConfig.id,
                MailboxBackgroundJob.job_kind == MAILBOX_JOB_SYNC,
                MailboxBackgroundJob.status.in_(_ACTIVE_JOB_STATUSES),
            )
        )
        oauth_reauthorization_pending = exists(
            select(MailboxOAuthCredential.id).where(
                MailboxOAuthCredential.organization_id == MailboxConfig.organization_id,
                MailboxOAuthCredential.mailbox_config_id == MailboxConfig.id,
                MailboxOAuthCredential.reauthorization_required_at.is_not(None),
            )
        )
        candidate = session.execute(
            select(MailboxConfig.id, MailboxConfig.organization_id)
            .where(
                MailboxConfig.enabled.is_(True),
                MailboxConfig.archived_at.is_(None),
                ~oauth_reauthorization_pending,
                or_(
                    MailboxConfig.sync_lease_expires_at.is_(None),
                    MailboxConfig.sync_lease_expires_at <= now,
                ),
                ~active_sync_job,
            )
            .where(
                or_(
                    MailboxConfig.import_start_uid.is_(None),
                    MailboxConfig.imap_uidvalidity.is_(None),
                    MailboxConfig.last_sync_started_at <= cutoff,
                    (
                        MailboxConfig.last_sync_started_at.is_(None)
                        & or_(
                            MailboxConfig.last_synced_at.is_(None),
                            MailboxConfig.last_synced_at <= cutoff,
                        )
                    ),
                )
            )
            .order_by(
                func.coalesce(
                    MailboxConfig.last_sync_started_at,
                    MailboxConfig.last_synced_at,
                ),
                MailboxConfig.id,
            )
            # A scheduler discovers work globally, then immediately switches
            # to its claimed workspace before loading source details.
            .execution_options(skip_organization_scope=True)
        ).first()
        if candidate is None:
            return False
        config_id, organization_id = candidate
        if not organization_id:
            session.execute(
                MailboxConfig.__table__.update()
                .where(MailboxConfig.id == config_id)
                .values(
                    enabled=False,
                    last_sync_error="mailbox_workspace_missing",
                    sync_lease_token=None,
                    sync_lease_expires_at=None,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return True
        with _organization_session(session, organization_id):
            try:
                config = _mailbox_config_or_error(session, config_id=config_id)
                response = _enqueue_sync_for_config(
                    session,
                    config=config,
                    trigger_type="scheduled",
                )
            except MailboxImportError:
                session.rollback()
                return False
        return not response.deduplicated


def run_mailbox_background_job_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and execute one mailbox job outside the HTTP request lifecycle."""

    claimed = _claim_next_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_job(database, settings=settings, worker_id=worker_id, claimed=claimed)
    return True


def _recover_expired_jobs(
    session: Session,
    *,
    settings: AppSettings,
    now: datetime,
) -> None:
    expired = session.scalars(
        select(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
            MailboxBackgroundJob.lease_expires_at.is_not(None),
            MailboxBackgroundJob.lease_expires_at <= now,
        )
        .execution_options(skip_organization_scope=True)
    ).all()
    for job in expired:
        organization_id = job.organization_id
        if not organization_id:
            session.execute(
                MailboxBackgroundJob.__table__.update()
                .where(
                    MailboxBackgroundJob.id == job.id,
                    MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
                )
                .values(
                    status=MAILBOX_JOB_FAILED,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="mailbox_workspace_missing",
                    completed_at=now,
                    updated_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            continue
        with _organization_session(session, organization_id):
            retry = job.attempt_count < job.max_attempts
            values: dict[str, object] = {
                "status": MAILBOX_JOB_QUEUED if retry else MAILBOX_JOB_FAILED,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": "mailbox_background_job_lease_expired",
                "updated_at": now,
                "completed_at": None if retry else now,
                "next_attempt_at": (
                    now + timedelta(seconds=_retry_delay_seconds(job.attempt_count))
                    if retry
                    else None
                ),
            }
            recovered = session.execute(
                update(MailboxBackgroundJob)
                .where(
                    MailboxBackgroundJob.id == job.id,
                    MailboxBackgroundJob.organization_id == organization_id,
                    MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if (
                recovered.rowcount == 1
                and not retry
                and job.job_kind == MAILBOX_JOB_SYNC
            ):
                record_terminal_sync_failure(
                    session,
                    settings=settings,
                    mailbox_config_id=job.mailbox_config_id,
                    job_id=job.id,
                    error_code="mailbox_background_job_lease_expired",
                    now=now,
                )
    if expired:
        session.commit()


def _claim_next_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedMailboxBackgroundJob | None:
    now = _utcnow()
    with database.session_factory() as session:
        _recover_expired_jobs(session, settings=settings, now=now)
        candidate = session.scalar(
            select(MailboxBackgroundJob)
            .where(
                MailboxBackgroundJob.status == MAILBOX_JOB_QUEUED,
                MailboxBackgroundJob.attempt_count < MailboxBackgroundJob.max_attempts,
                or_(
                    MailboxBackgroundJob.next_attempt_at.is_(None),
                    MailboxBackgroundJob.next_attempt_at <= now,
                ),
            )
            .order_by(
                MailboxBackgroundJob.requested_at.asc(),
                MailboxBackgroundJob.next_attempt_at.asc(),
                MailboxBackgroundJob.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        )
        if candidate is None:
            session.commit()
            return None
        organization_id = candidate.organization_id
        if not organization_id:
            session.execute(
                MailboxBackgroundJob.__table__.update()
                .where(
                    MailboxBackgroundJob.id == candidate.id,
                    MailboxBackgroundJob.status == MAILBOX_JOB_QUEUED,
                )
                .values(
                    status=MAILBOX_JOB_FAILED,
                    last_error="mailbox_workspace_missing",
                    completed_at=now,
                    next_attempt_at=None,
                    updated_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return None

        with _organization_session(session, organization_id):
            claimed = session.execute(
                update(MailboxBackgroundJob)
                .where(
                    MailboxBackgroundJob.id == candidate.id,
                    MailboxBackgroundJob.organization_id == organization_id,
                    MailboxBackgroundJob.status == MAILBOX_JOB_QUEUED,
                    MailboxBackgroundJob.attempt_count < MailboxBackgroundJob.max_attempts,
                    or_(
                        MailboxBackgroundJob.next_attempt_at.is_(None),
                        MailboxBackgroundJob.next_attempt_at <= now,
                    ),
                )
                .values(
                    status=MAILBOX_JOB_RUNNING,
                    attempt_count=MailboxBackgroundJob.attempt_count + 1,
                    next_attempt_at=None,
                    lease_owner=worker_id,
                    lease_expires_at=now + timedelta(seconds=_MAILBOX_JOB_LEASE_SECONDS),
                    last_error=None,
                    started_at=func.coalesce(MailboxBackgroundJob.started_at, now),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.commit()
                return None
            session.expire_all()
            job = session.scalar(
                select(MailboxBackgroundJob).where(
                    MailboxBackgroundJob.id == candidate.id,
                    MailboxBackgroundJob.organization_id == organization_id,
                )
            )
            if job is None or job.status != MAILBOX_JOB_RUNNING or job.lease_owner != worker_id:
                session.rollback()
                return None
            session.commit()
            return ClaimedMailboxBackgroundJob(
                job_id=job.id,
                organization_id=organization_id,
                mailbox_config_id=job.mailbox_config_id,
                email_attachment_import_id=job.email_attachment_import_id,
                job_kind=job.job_kind,
                source_fingerprint=job.source_fingerprint,
            )


def _owned_running_job(
    session: Session,
    *,
    claimed: ClaimedMailboxBackgroundJob,
    worker_id: str,
) -> MailboxBackgroundJob | None:
    return session.scalar(
        select(MailboxBackgroundJob).where(
            MailboxBackgroundJob.id == claimed.job_id,
            MailboxBackgroundJob.organization_id == claimed.organization_id,
            MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
            MailboxBackgroundJob.lease_owner == worker_id,
        )
    )


def _renew_job_lease(
    session: Session,
    *,
    claimed: ClaimedMailboxBackgroundJob,
    worker_id: str,
) -> bool:
    """Persist a fresh lease before the next potentially slow IMAP operation."""

    now = _utcnow()
    renewed = session.execute(
        update(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.id == claimed.job_id,
            MailboxBackgroundJob.organization_id == claimed.organization_id,
            MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
            MailboxBackgroundJob.lease_owner == worker_id,
        )
        .values(
            lease_expires_at=now + timedelta(seconds=_MAILBOX_JOB_LEASE_SECONDS),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return renewed.rowcount == 1


def _retry_delay_seconds(attempt_count: int) -> int:
    return min(60, 2 ** max(0, attempt_count - 1))


def _retryable_error(error: str) -> bool:
    return error not in _TERMINAL_ERROR_CODES


def _complete_job(
    session: Session,
    *,
    claimed: ClaimedMailboxBackgroundJob,
    worker_id: str,
    settings: AppSettings | None = None,
    imported_count: int = 0,
    duplicate_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> bool:
    now = _utcnow()
    completed = session.execute(
        update(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.id == claimed.job_id,
            MailboxBackgroundJob.organization_id == claimed.organization_id,
            MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
            MailboxBackgroundJob.lease_owner == worker_id,
        )
        .values(
            status=MAILBOX_JOB_COMPLETED,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error=None,
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    was_completed = completed.rowcount == 1
    if (
        was_completed
        and claimed.job_kind == MAILBOX_JOB_SYNC
        and settings is not None
    ):
        resolve_mailbox_sync_alert(
            session,
            mailbox_config_id=claimed.mailbox_config_id,
            resolution="sync_succeeded",
            now=now,
        )
    session.commit()
    if was_completed:
        _prune_terminal_job_history_safely(session, now=now)
    return was_completed


def _fail_job(
    session: Session,
    *,
    claimed: ClaimedMailboxBackgroundJob,
    worker_id: str,
    error: str,
    retryable: bool,
    settings: AppSettings | None = None,
) -> bool:
    now = _utcnow()
    job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
    if job is None:
        session.rollback()
        return False
    should_retry = retryable and job.attempt_count < job.max_attempts
    updated = session.execute(
        update(MailboxBackgroundJob)
        .where(
            MailboxBackgroundJob.id == claimed.job_id,
            MailboxBackgroundJob.organization_id == claimed.organization_id,
            MailboxBackgroundJob.status == MAILBOX_JOB_RUNNING,
            MailboxBackgroundJob.lease_owner == worker_id,
        )
        .values(
            status=MAILBOX_JOB_QUEUED if should_retry else MAILBOX_JOB_FAILED,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=(
                now + timedelta(seconds=_retry_delay_seconds(job.attempt_count))
                if should_retry
                else None
            ),
            last_error=error[:2000],
            completed_at=None if should_retry else now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    was_updated = updated.rowcount == 1
    if (
        was_updated
        and not should_retry
        and claimed.job_kind == MAILBOX_JOB_SYNC
        and settings is not None
    ):
        record_terminal_sync_failure(
            session,
            settings=settings,
            mailbox_config_id=claimed.mailbox_config_id,
            job_id=claimed.job_id,
            error_code=error,
            now=now,
        )
    session.commit()
    if was_updated and not should_retry:
        _prune_terminal_job_history_safely(session, now=now)
    return was_updated


def _process_claimed_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedMailboxBackgroundJob,
) -> None:
    try:
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
                if job is None:
                    session.rollback()
                    return

                def heartbeat() -> None:
                    if not _renew_job_lease(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                    ):
                        raise _MailboxBackgroundJobLeaseLost()

                config = session.scalar(
                    select(MailboxConfig).where(MailboxConfig.id == claimed.mailbox_config_id)
                )
                if config is None:
                    _fail_job(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                        error="mailbox_config_not_found",
                        retryable=False,
                        settings=settings,
                    )
                    return
                if config.organization_id != claimed.organization_id:
                    _fail_job(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                        error="mailbox_workspace_mismatch",
                        retryable=False,
                        settings=settings,
                    )
                    return
                if (
                    claimed.job_kind == MAILBOX_JOB_SYNC
                    and claimed.source_fingerprint
                    and mailbox_source_fingerprint(config) != claimed.source_fingerprint
                ):
                    _fail_job(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                        error="mailbox_task_source_changed",
                        retryable=False,
                        settings=settings,
                    )
                    return
                if claimed.job_kind == MAILBOX_JOB_SYNC:
                    if not config.enabled:
                        _fail_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            error="mailbox_not_enabled",
                            retryable=False,
                            settings=settings,
                        )
                        return
                    try:
                        result = sync_mailbox(
                            session,
                            settings=settings,
                            config_id=config.id,
                            expected_source_fingerprint=claimed.source_fingerprint,
                            heartbeat=heartbeat,
                        )
                    except MailboxImportError as exc:
                        error = str(exc)
                        _fail_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            error=error,
                            retryable=_retryable_error(error),
                            settings=settings,
                        )
                        return
                    _complete_job(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                        settings=settings,
                        imported_count=result.imported_count,
                        duplicate_count=result.duplicate_count,
                        skipped_count=result.skipped_count,
                        failed_count=result.failed_count,
                    )
                    return
                if claimed.job_kind == MAILBOX_JOB_ATTACHMENT_RETRY:
                    if not claimed.email_attachment_import_id:
                        _fail_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            error="mailbox_import_not_found",
                            retryable=False,
                            settings=settings,
                        )
                        return
                    attachment_import = session.scalar(
                        select(EmailAttachmentImport).where(
                            EmailAttachmentImport.id == claimed.email_attachment_import_id
                        )
                    )
                    if (
                        attachment_import is None
                        or attachment_import.organization_id != claimed.organization_id
                        or attachment_import.mailbox_config_id != config.id
                    ):
                        _fail_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            error="mailbox_import_not_found",
                            retryable=False,
                            settings=settings,
                        )
                        return
                    # A worker may have committed the attachment result just
                    # before a process restart, while this job lease was still
                    # running.  Make recovery idempotent instead of retrying a
                    # successfully imported attachment.
                    if attachment_import.status == "imported":
                        _complete_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            settings=settings,
                        )
                        return
                    try:
                        result = retry_mailbox_attachment(
                            session,
                            settings=settings,
                            import_id=attachment_import.id,
                            retry_lease_seconds=_MAILBOX_JOB_LEASE_SECONDS,
                            heartbeat=heartbeat,
                        )
                    except MailboxImportError as exc:
                        error = str(exc)
                        _fail_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            error=error,
                            retryable=_retryable_error(error),
                            settings=settings,
                        )
                        return
                    if result.status == "imported":
                        _complete_job(
                            session,
                            claimed=claimed,
                            worker_id=worker_id,
                            settings=settings,
                            imported_count=1,
                        )
                        return
                    error = result.error or "attachment_import_failed"
                    _fail_job(
                        session,
                        claimed=claimed,
                        worker_id=worker_id,
                        error=error,
                        retryable=_retryable_error(error),
                        settings=settings,
                    )
                    return
                _fail_job(
                    session,
                    claimed=claimed,
                    worker_id=worker_id,
                    error="mailbox_background_job_invalid",
                    retryable=False,
                    settings=settings,
                )
    except Exception:
        # Never surface a raw exception or provider text through a task.  A
        # later worker can retry a transient failure using the durable lease.
        _finish_unexpected_failure(
            database,
            settings=settings,
            claimed=claimed,
            worker_id=worker_id,
        )


def _finish_unexpected_failure(
    database: Database,
    *,
    settings: AppSettings,
    claimed: ClaimedMailboxBackgroundJob,
    worker_id: str,
) -> None:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            _fail_job(
                session,
                claimed=claimed,
                worker_id=worker_id,
                error="mailbox_background_job_failed",
                retryable=True,
                settings=settings,
            )


__all__ = [
    "MAILBOX_JOB_ATTACHMENT_RETRY",
    "MAILBOX_JOB_COMPLETED",
    "MAILBOX_JOB_FAILED",
    "MAILBOX_JOB_QUEUED",
    "MAILBOX_JOB_RUNNING",
    "MAILBOX_JOB_SYNC",
    "enqueue_all_mailbox_sync_jobs",
    "enqueue_due_mailbox_sync_jobs",
    "enqueue_mailbox_attachment_retry_job",
    "enqueue_mailbox_sync_job",
    "get_mailbox_background_job",
    "list_mailbox_background_jobs",
    "run_mailbox_background_job_worker_once",
]
