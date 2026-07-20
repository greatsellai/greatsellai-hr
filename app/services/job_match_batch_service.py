from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.database import Database
from app.models import JobMatch, JobMatchBatch, JobMatchBatchItem, JobVersion, Resume, ResumeFactSnapshot
from app.schemas import JobMatchBatchItemResponse, JobMatchBatchResponse, JobMatchCreate
from app.tenant_scope import clear_organization_context, set_organization_context
from app.services.ai_gateway_service import (
    AiGatewayError,
    ai_gateway_credentials_configured,
    resolve_active_route_policy_version_id,
)
from app.services.ai_retry_policy import is_retryable_ai_transport_error
from app.services.deepseek_provider import DeepSeekProviderError
from app.services.job_service import (
    JobServiceError,
    JobVersionNotFoundError,
    run_job_match,
)
from app.services.resume_eligibility import has_unreliable_source_text


BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_COMPLETED = "completed"
BATCH_PARTIAL = "partial"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_COMPLETED = "completed"
ITEM_FAILED = "failed"
_LEASE_SECONDS = 180


@dataclass(frozen=True)
class ClaimedJobMatchBatchItem:
    item_id: str
    organization_id: str
    batch_id: str
    resume_id: str
    job_version_id: str
    ai_route_policy_version_id: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Bind all post-claim JD-match work to the item workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _batch_response(batch: JobMatchBatch) -> JobMatchBatchResponse:
    return JobMatchBatchResponse(
        batch_id=batch.id,
        job_version_id=batch.job_version_id,
        status=batch.status,
        total_count=batch.total_count,
        completed_count=batch.completed_count,
        failed_count=batch.failed_count,
        requested_at=batch.requested_at.isoformat(),
        started_at=batch.started_at.isoformat() if batch.started_at else None,
        completed_at=batch.completed_at.isoformat() if batch.completed_at else None,
        last_error=batch.last_error,
    )


def _require_ai_gateway_credentials(settings: AppSettings) -> None:
    """Retain the established no-key error while accepting credential refs.

    The selected route validates its own credential reference at execution
    time.  This preflight only decides whether a batch may be queued at all,
    and preserves the HTTP-compatible legacy error when the process has no
    provider credential source whatsoever.
    """

    if not ai_gateway_credentials_configured(settings):
        raise JobServiceError("deepseek_api_key_not_configured")


def _route_pin_for_new_batch(
    session: Session,
    *,
    settings: AppSettings,
) -> str:
    _require_ai_gateway_credentials(settings)
    try:
        return resolve_active_route_policy_version_id(
            session,
            settings=settings,
            feature="jd_match",
        )
    except AiGatewayError as exc:
        raise JobServiceError(str(exc)) from exc


def _persist_legacy_job_match_batch_route_pin(
    session: Session,
    *,
    batch: JobMatchBatch,
    settings: AppSettings,
) -> str | None:
    """Pin one route once for a pre-migration batch before its first call."""

    if batch.ai_route_policy_version_id is not None:
        return batch.ai_route_policy_version_id
    try:
        resolved_id = resolve_active_route_policy_version_id(
            session,
            settings=settings,
            feature="jd_match",
        )
    except AiGatewayError:
        return None
    session.execute(
        update(JobMatchBatch)
        .where(
            JobMatchBatch.id == batch.id,
            JobMatchBatch.organization_id == batch.organization_id,
            JobMatchBatch.ai_route_policy_version_id.is_(None),
        )
        .values(ai_route_policy_version_id=resolved_id)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire(batch, ["ai_route_policy_version_id"])
    return batch.ai_route_policy_version_id


def enqueue_job_version_match_batch(
    session: Session,
    *,
    job_version_id: str,
    settings: AppSettings,
) -> JobMatchBatchResponse:
    """Persist one full N×M side of the matrix without calling the model in HTTP."""

    _require_ai_gateway_credentials(settings)
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    if job_version.status != "confirmed":
        raise JobServiceError("job_version_must_be_confirmed_for_matching")
    if not job_version.requirements:
        raise JobServiceError("job_version_has_no_requirements")
    organization_id = job_version.organization_id

    existing = session.scalar(
        select(JobMatchBatch)
        .where(
            JobMatchBatch.job_version_id == job_version.id,
            JobMatchBatch.organization_id == organization_id,
            JobMatchBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING)),
        )
        .order_by(JobMatchBatch.requested_at.desc())
    )
    if existing is not None:
        return _batch_response(existing)

    # A queued/retried batch must keep the same approved route even if the
    # platform owner later switches the active JD-match model. The pin lives
    # on the durable batch (not an individual in-memory worker claim), so a
    # lease recovery or retry cannot silently move to a new model.
    route_policy_version_id = _route_pin_for_new_batch(session, settings=settings)

    now = _utcnow()
    snapshot_rows = session.execute(
        select(
            Resume.id,
            ResumeFactSnapshot.id,
            ResumeFactSnapshot.facts_version,
            Resume.quality_flags,
        )
        .join(
            ResumeFactSnapshot,
            and_(
                ResumeFactSnapshot.resume_id == Resume.id,
                ResumeFactSnapshot.facts_version == Resume.facts_version,
            ),
        )
        .where(Resume.is_active.is_(True), Resume.extraction_status == "ready")
        .where(
            Resume.organization_id == organization_id,
            ResumeFactSnapshot.organization_id == organization_id,
        )
        .order_by(Resume.created_at.asc(), Resume.id.asc())
    ).all()
    # Do not queue a model call when the parser has declared the source text
    # untrustworthy.  This also prevents a cached old match from re-entering a
    # new recruiter-facing batch.
    snapshots = [
        (resume_id, snapshot_id, facts_version)
        for resume_id, snapshot_id, facts_version, quality_flags in snapshot_rows
        if not has_unreliable_source_text(quality_flags)
    ]
    batch = JobMatchBatch(
        organization_id=organization_id,
        job_version_id=job_version.id,
        ai_route_policy_version_id=route_policy_version_id,
        status=BATCH_QUEUED if snapshots else BATCH_COMPLETED,
        total_count=len(snapshots),
        completed_count=0,
        failed_count=0,
        max_attempts=max(1, settings.ai_extraction_job_max_attempts),
        requested_at=now,
        completed_at=now if not snapshots else None,
    )
    session.add(batch)
    session.flush()

    snapshot_ids = [snapshot_id for _, snapshot_id, _ in snapshots]
    cached_by_snapshot: dict[str, str] = {}
    if snapshot_ids:
        cached = session.execute(
            select(JobMatch.fact_snapshot_id, JobMatch.id)
            .where(
                JobMatch.job_version_id == job_version.id,
                JobMatch.organization_id == organization_id,
                JobMatch.fact_snapshot_id.in_(snapshot_ids),
                JobMatch.status.in_(("succeeded", "needs_review")),
            )
            .order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
        ).all()
        for snapshot_id, match_id in cached:
            if snapshot_id is not None:
                cached_by_snapshot.setdefault(snapshot_id, match_id)

    for resume_id, snapshot_id, facts_version in snapshots:
        cached_match_id = cached_by_snapshot.get(snapshot_id)
        session.add(
            JobMatchBatchItem(
                organization_id=organization_id,
                batch_id=batch.id,
                resume_id=resume_id,
                fact_snapshot_id=snapshot_id,
                facts_version=facts_version,
                status=ITEM_COMPLETED if cached_match_id else ITEM_QUEUED,
                next_attempt_at=None if cached_match_id else now,
                job_match_id=cached_match_id,
                completed_at=now if cached_match_id else None,
            )
        )
        if cached_match_id:
            batch.completed_count += 1
    if batch.completed_count == batch.total_count:
        batch.status = BATCH_COMPLETED
        batch.completed_at = now
    session.flush()
    return _batch_response(batch)


def get_job_match_batch(session: Session, *, batch_id: str) -> JobMatchBatchResponse:
    batch = session.get(JobMatchBatch, batch_id)
    if batch is None:
        raise JobServiceError("job_match_batch_not_found")
    return _batch_response(batch)


def list_job_match_batch_items(
    session: Session,
    *,
    batch_id: str,
) -> list[JobMatchBatchItemResponse]:
    """Expose durable per-resume progress so a recruiter can inspect failures."""

    if session.get(JobMatchBatch, batch_id) is None:
        raise JobServiceError("job_match_batch_not_found")
    items = session.scalars(
        select(JobMatchBatchItem)
        .where(JobMatchBatchItem.batch_id == batch_id)
        .options(selectinload(JobMatchBatchItem.resume).selectinload(Resume.candidate))
        .order_by(JobMatchBatchItem.updated_at.desc(), JobMatchBatchItem.id.desc())
    ).all()
    return [
        JobMatchBatchItemResponse(
            item_id=item.id,
            resume_id=item.resume_id,
            candidate_id=item.resume.candidate_id,
            candidate_display_name=item.resume.candidate.display_name,
            facts_version=item.facts_version,
            status=item.status,
            attempt_count=item.attempt_count,
            last_error=item.last_error,
            job_match_id=item.job_match_id,
            completed_at=item.completed_at.isoformat() if item.completed_at else None,
            updated_at=item.updated_at.isoformat(),
        )
        for item in items
    ]


def run_job_match_batch_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    claimed = _claim_next_item(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_item(database, settings=settings, worker_id=worker_id, claimed=claimed)
    return True


def _recover_expired_items(session: Session, *, now: datetime) -> None:
    expired_rows = session.execute(
        select(JobMatchBatchItem, JobMatchBatch)
        .join(JobMatchBatch)
        .where(
            JobMatchBatchItem.status == ITEM_RUNNING,
            JobMatchBatchItem.lease_expires_at.is_not(None),
            JobMatchBatchItem.lease_expires_at <= now,
        )
        # Worker lease recovery is the only global scan.  Each recovered row
        # below is immediately re-bound to its own workspace before state is
        # changed or aggregate progress is recalculated.
        .execution_options(skip_organization_scope=True)
    ).all()
    for item, batch in expired_rows:
        organization_id = item.organization_id
        if not organization_id or batch.organization_id != organization_id:
            # A corrupt cross-workspace relationship must never be processed.
            # This bulk update intentionally runs from the global recovery
            # path, so it does not require a tenant context that is unsafe to
            # infer from malformed data.
            session.execute(
                JobMatchBatchItem.__table__.update()
                .where(JobMatchBatchItem.id == item.id)
                .values(
                    status=ITEM_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="job_match_workspace_mismatch",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            continue
        with _organization_session(session, organization_id):
            if item.attempt_count >= batch.max_attempts:
                item.status = ITEM_FAILED
                item.completed_at = now
                item.last_error = "job_match_worker_lease_expired"
            else:
                item.status = ITEM_QUEUED
                item.next_attempt_at = now
                item.last_error = "job_match_worker_lease_expired"
            item.lease_owner = None
            item.lease_expires_at = None
            _refresh_batch_progress(session, batch=batch, now=now)
            # The caller commits the global claim transaction after recovery.
            # Flushing here guarantees the tenant write guard sees this item
            # while its workspace context is still installed.
            session.flush()


def _claim_next_item(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedJobMatchBatchItem | None:
    now = _utcnow()
    with database.session_factory() as session:
        _recover_expired_items(session, now=now)
        if not ai_gateway_credentials_configured(settings):
            session.commit()
            return None
        row = session.execute(
            select(JobMatchBatchItem, JobMatchBatch)
            .join(JobMatchBatch)
            .where(
                JobMatchBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING)),
                JobMatchBatchItem.status == ITEM_QUEUED,
                or_(
                    JobMatchBatchItem.next_attempt_at.is_(None),
                    JobMatchBatchItem.next_attempt_at <= now,
                ),
            )
            .order_by(
                JobMatchBatch.requested_at.asc(),
                JobMatchBatchItem.next_attempt_at.asc(),
                JobMatchBatchItem.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        ).first()
        if row is None:
            session.commit()
            return None
        item, batch = row
        organization_id = item.organization_id
        if not organization_id or batch.organization_id != organization_id:
            session.execute(
                JobMatchBatchItem.__table__.update()
                .where(JobMatchBatchItem.id == item.id)
                .values(
                    status=ITEM_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="job_match_workspace_mismatch",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return None
        with _organization_session(session, organization_id):
            _persist_legacy_job_match_batch_route_pin(
                session,
                batch=batch,
                settings=settings,
            )
            item.status = ITEM_RUNNING
            item.attempt_count += 1
            item.next_attempt_at = None
            item.lease_owner = worker_id
            item.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
            item.last_error = None
            batch.status = BATCH_RUNNING
            batch.started_at = batch.started_at or now
            batch.lease_owner = worker_id
            batch.lease_expires_at = item.lease_expires_at
            session.commit()
            return ClaimedJobMatchBatchItem(
                item_id=item.id,
                organization_id=organization_id,
                batch_id=batch.id,
                resume_id=item.resume_id,
                job_version_id=batch.job_version_id,
                ai_route_policy_version_id=batch.ai_route_policy_version_id,
            )


def _process_claimed_item(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedJobMatchBatchItem,
) -> None:
    try:
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                item = _owned_item(
                    session,
                    item_id=claimed.item_id,
                    worker_id=worker_id,
                    organization_id=claimed.organization_id,
                )
                if item is None:
                    session.rollback()
                    return
                batch = item.batch
                if (
                    item.organization_id != claimed.organization_id
                    or batch is None
                    or batch.organization_id != claimed.organization_id
                    or batch.id != claimed.batch_id
                    or batch.job_version_id != claimed.job_version_id
                    or batch.ai_route_policy_version_id
                    != claimed.ai_route_policy_version_id
                ):
                    raise JobServiceError("job_match_workspace_mismatch")
                job_version = session.get(JobVersion, claimed.job_version_id)
                if job_version is None:
                    raise JobVersionNotFoundError("job_version_not_found")
                if job_version.organization_id != claimed.organization_id:
                    raise JobServiceError("job_match_workspace_mismatch")
                latest_snapshot_row = session.execute(
                    select(ResumeFactSnapshot, Resume.quality_flags)
                    .join(Resume)
                    .where(
                        Resume.id == item.resume_id,
                        Resume.organization_id == claimed.organization_id,
                        Resume.is_active.is_(True),
                        Resume.extraction_status == "ready",
                        ResumeFactSnapshot.resume_id == Resume.id,
                        ResumeFactSnapshot.organization_id == claimed.organization_id,
                        ResumeFactSnapshot.facts_version == Resume.facts_version,
                    )
                ).first()
                if latest_snapshot_row is None:
                    raise JobServiceError("resume_no_longer_ready_for_job_match")
                latest_snapshot, quality_flags = latest_snapshot_row
                if latest_snapshot.organization_id != claimed.organization_id:
                    raise JobServiceError("job_match_workspace_mismatch")
                if has_unreliable_source_text(quality_flags):
                    raise JobServiceError("resume_source_text_unreliable")
                item.fact_snapshot_id = latest_snapshot.id
                item.facts_version = latest_snapshot.facts_version
                cached_match = session.scalar(
                    select(JobMatch)
                    .where(
                        JobMatch.job_version_id == claimed.job_version_id,
                        JobMatch.organization_id == claimed.organization_id,
                        JobMatch.fact_snapshot_id == latest_snapshot.id,
                        JobMatch.status.in_(("succeeded", "needs_review")),
                    )
                    .order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
                )
                if cached_match is not None:
                    match_id = cached_match.id
                else:
                    matched = run_job_match(
                        session,
                        resume_id=item.resume_id,
                        payload=JobMatchCreate(job_version_id=claimed.job_version_id),
                        settings=settings,
                        pinned_route_policy_version_id=claimed.ai_route_policy_version_id,
                        ai_run_business_ref_type="job_match_batch_item",
                        ai_run_business_ref_id=item.id,
                    )
                    match_id = matched.match_id
                    persisted_match = session.get(JobMatch, match_id)
                    if (
                        persisted_match is None
                        or persisted_match.organization_id != claimed.organization_id
                    ):
                        raise JobServiceError("job_match_workspace_mismatch")
                _finish_item_success(session, item=item, worker_id=worker_id, match_id=match_id)
                session.commit()
    except DeepSeekProviderError as exc:
        error = str(exc)
        _finish_item_failure(
            database,
            item_id=claimed.item_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error=error,
            retryable=is_retryable_ai_transport_error(error),
        )
    except JobServiceError as exc:
        _finish_item_failure(
            database,
            item_id=claimed.item_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error=str(exc),
            retryable=False,
        )
    except Exception:
        _finish_item_failure(
            database,
            item_id=claimed.item_id,
            worker_id=worker_id,
            organization_id=claimed.organization_id,
            error="job_match_worker_error",
            retryable=True,
        )


def _owned_item(
    session: Session,
    *,
    item_id: str,
    worker_id: str,
    organization_id: str | None = None,
) -> JobMatchBatchItem | None:
    statement = select(JobMatchBatchItem).where(
        JobMatchBatchItem.id == item_id,
        JobMatchBatchItem.status == ITEM_RUNNING,
        JobMatchBatchItem.lease_owner == worker_id,
    )
    if organization_id is not None:
        statement = statement.where(JobMatchBatchItem.organization_id == organization_id)
    return session.scalar(statement)


def _finish_item_success(
    session: Session,
    *,
    item: JobMatchBatchItem,
    worker_id: str,
    match_id: str,
) -> None:
    if item.lease_owner != worker_id or item.status != ITEM_RUNNING:
        raise JobServiceError("job_match_batch_item_lease_lost")
    now = _utcnow()
    item.status = ITEM_COMPLETED
    item.job_match_id = match_id
    item.lease_owner = None
    item.lease_expires_at = None
    item.next_attempt_at = None
    item.last_error = None
    item.completed_at = now
    _refresh_batch_progress(session, batch=item.batch, now=now)


def _finish_item_failure(
    database: Database,
    *,
    item_id: str,
    worker_id: str,
    organization_id: str,
    error: str,
    retryable: bool,
) -> None:
    now = _utcnow()
    with database.session_factory() as session:
        with _organization_session(session, organization_id):
            item = _owned_item(
                session,
                item_id=item_id,
                worker_id=worker_id,
                organization_id=organization_id,
            )
            if item is None or item.organization_id != organization_id:
                session.rollback()
                return
            batch = item.batch
            if batch is None or batch.organization_id != organization_id:
                session.rollback()
                return
            if retryable and item.attempt_count < batch.max_attempts:
                item.status = ITEM_QUEUED
                item.next_attempt_at = now + timedelta(seconds=min(60, 2 ** (item.attempt_count - 1)))
                item.completed_at = None
            else:
                item.status = ITEM_FAILED
                item.next_attempt_at = None
                item.completed_at = now
            item.lease_owner = None
            item.lease_expires_at = None
            item.last_error = error[:2000]
            _refresh_batch_progress(session, batch=batch, now=now)
            session.commit()


def _refresh_batch_progress(session: Session, *, batch: JobMatchBatch, now: datetime) -> None:
    # The just-finished item must be visible to the aggregate query.  An
    # explicit flush also keeps this correct when the session was previously
    # used by the matching persistence path.
    session.flush()
    counts = dict(
        session.execute(
            select(JobMatchBatchItem.status, func.count())
            .where(
                JobMatchBatchItem.batch_id == batch.id,
                JobMatchBatchItem.organization_id == batch.organization_id,
            )
            .group_by(JobMatchBatchItem.status)
        ).all()
    )
    batch.completed_count = counts.get(ITEM_COMPLETED, 0)
    batch.failed_count = counts.get(ITEM_FAILED, 0)
    pending = counts.get(ITEM_QUEUED, 0) + counts.get(ITEM_RUNNING, 0)
    if pending:
        batch.status = BATCH_RUNNING
        return
    batch.status = BATCH_PARTIAL if batch.failed_count else BATCH_COMPLETED
    batch.completed_at = now
    batch.lease_owner = None
    batch.lease_expires_at = None
    if batch.failed_count:
        last_failed = session.scalar(
            select(JobMatchBatchItem.last_error)
            .where(
                JobMatchBatchItem.batch_id == batch.id,
                JobMatchBatchItem.organization_id == batch.organization_id,
                JobMatchBatchItem.status == ITEM_FAILED,
            )
            .order_by(JobMatchBatchItem.updated_at.desc())
        )
        batch.last_error = last_failed
    else:
        batch.last_error = None


__all__ = [
    "BATCH_COMPLETED",
    "BATCH_PARTIAL",
    "BATCH_QUEUED",
    "BATCH_RUNNING",
    "enqueue_job_version_match_batch",
    "get_job_match_batch",
    "run_job_match_batch_worker_once",
]
