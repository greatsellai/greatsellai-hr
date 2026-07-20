"""Durable, workspace-scoped batches for AI resume scoring.

HTTP requests only enqueue durable work.  The worker claims one item at a
time with a conditional update, rechecks the current resume facts, and then
calls the existing fact-grounded scoring service.  This keeps model requests
recoverable, observable, and isolated by organization.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.database import Database
from app.models import (
    Resume,
    ResumeFactSnapshot,
    ResumeScore,
    ResumeScoreBatch,
    ResumeScoreBatchItem,
    ScoreTemplate,
    ScoreTemplateDimension,
)
from app.schemas import (
    ResumeScoreBatchItemResponse,
    ResumeScoreBatchResponse,
    ResumeScoreCreate,
)
from app.services.deepseek_provider import DeepSeekProviderError
from app.services.ai_gateway_service import (
    AiGatewayError,
    ai_gateway_credentials_configured,
    resolve_active_route_policy_version_id,
)
from app.services.ai_retry_policy import is_retryable_ai_transport_error
from app.services.resume_eligibility import has_unreliable_source_text
from app.services.score_service import (
    ScoreServiceError,
    ScoreTemplateNotFoundError,
    run_resume_score,
)
from app.tenant_scope import clear_organization_context, set_organization_context


BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_COMPLETED = "completed"
BATCH_PARTIAL = "partial"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_COMPLETED = "completed"
ITEM_FAILED = "failed"
_LEASE_SECONDS = 180
_REUSABLE_SCORE_STATUSES = ("succeeded", "needs_review", "overridden")


@dataclass(frozen=True)
class ClaimedResumeScoreBatchItem:
    item_id: str
    organization_id: str
    batch_id: str
    resume_id: str
    template_id: str
    template_version: int
    ai_route_policy_version_id: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Bind all post-claim work to the item workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _batch_response(batch: ResumeScoreBatch) -> ResumeScoreBatchResponse:
    template = batch.template
    return ResumeScoreBatchResponse(
        batch_id=batch.id,
        template_id=batch.template_id,
        template_name=template.name if template is not None else None,
        template_version=batch.template_version,
        status=batch.status,
        total_count=batch.total_count,
        completed_count=batch.completed_count,
        failed_count=batch.failed_count,
        cached_count=batch.cached_count,
        requested_at=batch.requested_at.isoformat(),
        started_at=batch.started_at.isoformat() if batch.started_at else None,
        completed_at=batch.completed_at.isoformat() if batch.completed_at else None,
        last_error=batch.last_error,
    )


def _require_scoreable_template(
    session: Session,
    *,
    template_id: str,
) -> tuple[ScoreTemplate, list[ScoreTemplateDimension]]:
    template = session.get(ScoreTemplate, template_id)
    if template is None or template.is_archived:
        raise ScoreTemplateNotFoundError("score_template_not_found")
    dimensions = session.scalars(
        select(ScoreTemplateDimension)
        .where(ScoreTemplateDimension.template_id == template.id)
        .order_by(ScoreTemplateDimension.sort_order)
    ).all()
    if not dimensions:
        raise ScoreServiceError("score_template_has_no_dimensions")
    if sum(dimension.weight for dimension in dimensions) != 100:
        raise ScoreServiceError("score_template_weights_must_sum_to_100")
    if len({dimension.key for dimension in dimensions}) != len(dimensions):
        raise ScoreServiceError("score_template_dimension_keys_must_be_unique")
    return template, dimensions


def _existing_active_batch(
    session: Session,
    *,
    template_id: str,
    template_version: int,
    organization_id: str,
) -> ResumeScoreBatch | None:
    return session.scalar(
        select(ResumeScoreBatch)
        .where(
            ResumeScoreBatch.template_id == template_id,
            ResumeScoreBatch.template_version == template_version,
            ResumeScoreBatch.organization_id == organization_id,
            ResumeScoreBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING)),
        )
        .order_by(ResumeScoreBatch.requested_at.desc(), ResumeScoreBatch.id.desc())
    )


def _route_pin_for_new_score_batch(
    session: Session,
    *,
    settings: AppSettings,
) -> tuple[str | None, str | None]:
    """Resolve the route once at enqueue time for deterministic retries."""

    if not ai_gateway_credentials_configured(settings):
        # The HTTP/API contract has historically exposed this stable code.
        # Preserve it while allowing the generic credential map to enable the
        # gateway without a legacy provider-specific key.
        return None, "deepseek_api_key_not_configured"
    try:
        return (
            resolve_active_route_policy_version_id(
                session,
                settings=settings,
                feature="resume_score",
            ),
            None,
        )
    except AiGatewayError as exc:
        return None, str(exc)


def _persist_legacy_score_batch_route_pin(
    session: Session,
    *,
    batch: ResumeScoreBatch,
    settings: AppSettings,
) -> str | None:
    """Compare-and-set a route for batches created before the pin column.

    Multiple workers may discover different queued items in the same legacy
    batch. The conditional batch update makes the first resolved version win;
    every later worker reloads and uses that durable value.
    """

    if batch.ai_route_policy_version_id is not None:
        return batch.ai_route_policy_version_id
    try:
        resolved_id = resolve_active_route_policy_version_id(
            session,
            settings=settings,
            feature="resume_score",
        )
    except AiGatewayError:
        # Preserve the established worker failure path when no route can be
        # resolved. No external call occurs, and the item becomes terminal.
        return None
    session.execute(
        update(ResumeScoreBatch)
        .where(
            ResumeScoreBatch.id == batch.id,
            ResumeScoreBatch.organization_id == batch.organization_id,
            ResumeScoreBatch.ai_route_policy_version_id.is_(None),
        )
        .values(ai_route_policy_version_id=resolved_id)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.expire(batch, ["ai_route_policy_version_id"])
    return batch.ai_route_policy_version_id


def enqueue_resume_score_batch(
    session: Session,
    *,
    template_id: str,
    settings: AppSettings,
) -> ResumeScoreBatchResponse:
    """Queue all currently scoreable resumes for one fixed score template."""

    template, _ = _require_scoreable_template(session, template_id=template_id)
    route_policy_version_id, route_error = _route_pin_for_new_score_batch(
        session,
        settings=settings,
    )
    if route_error is not None:
        raise ScoreServiceError(route_error)
    assert route_policy_version_id is not None
    organization_id = template.organization_id
    existing = _existing_active_batch(
        session,
        template_id=template.id,
        template_version=template.version,
        organization_id=organization_id,
    )
    if existing is not None:
        return _batch_response(existing)

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
        .where(
            Resume.is_active.is_(True),
            Resume.extraction_status == "ready",
            Resume.organization_id == organization_id,
            ResumeFactSnapshot.organization_id == organization_id,
        )
        .order_by(Resume.created_at.asc(), Resume.id.asc())
    ).all()
    snapshots = [
        (resume_id, snapshot_id, facts_version)
        for resume_id, snapshot_id, facts_version, quality_flags in snapshot_rows
        if not has_unreliable_source_text(quality_flags)
    ]

    batch = ResumeScoreBatch(
        organization_id=organization_id,
        template_id=template.id,
        template_version=template.version,
        ai_route_policy_version_id=route_policy_version_id,
        status=BATCH_QUEUED if snapshots else BATCH_COMPLETED,
        total_count=len(snapshots),
        completed_count=0,
        failed_count=0,
        cached_count=0,
        max_attempts=max(1, settings.ai_extraction_job_max_attempts),
        requested_at=now,
        completed_at=now if not snapshots else None,
    )
    # The partial unique index provides race-safe idempotency when two browser
    # tabs start the same template at almost the same time.
    try:
        with session.begin_nested():
            session.add(batch)
            session.flush()
    except IntegrityError:
        existing = _existing_active_batch(
            session,
            template_id=template.id,
            template_version=template.version,
            organization_id=organization_id,
        )
        if existing is not None:
            return _batch_response(existing)
        raise

    snapshot_ids = [snapshot_id for _, snapshot_id, _ in snapshots]
    cached_by_snapshot: dict[str, str] = {}
    if snapshot_ids:
        cached = session.execute(
            select(ResumeScore.fact_snapshot_id, ResumeScore.id)
            .where(
                ResumeScore.organization_id == organization_id,
                ResumeScore.template_id == template.id,
                ResumeScore.template_version == template.version,
                ResumeScore.fact_snapshot_id.in_(snapshot_ids),
                ResumeScore.status.in_(_REUSABLE_SCORE_STATUSES),
            )
            .order_by(ResumeScore.created_at.desc(), ResumeScore.id.desc())
        ).all()
        for snapshot_id, score_id in cached:
            if snapshot_id is not None:
                cached_by_snapshot.setdefault(snapshot_id, score_id)

    for resume_id, snapshot_id, facts_version in snapshots:
        cached_score_id = cached_by_snapshot.get(snapshot_id)
        session.add(
            ResumeScoreBatchItem(
                organization_id=organization_id,
                batch_id=batch.id,
                resume_id=resume_id,
                fact_snapshot_id=snapshot_id,
                facts_version=facts_version,
                status=ITEM_COMPLETED if cached_score_id else ITEM_QUEUED,
                next_attempt_at=None if cached_score_id else now,
                resume_score_id=cached_score_id,
                was_cached=bool(cached_score_id),
                completed_at=now if cached_score_id else None,
            )
        )
        if cached_score_id:
            batch.completed_count += 1
            batch.cached_count += 1
    if batch.completed_count == batch.total_count:
        batch.status = BATCH_COMPLETED
        batch.completed_at = now
    session.flush()
    return _batch_response(batch)


def get_resume_score_batch(
    session: Session,
    *,
    batch_id: str,
) -> ResumeScoreBatchResponse:
    batch = session.get(ResumeScoreBatch, batch_id)
    if batch is None:
        raise ScoreServiceError("resume_score_batch_not_found")
    return _batch_response(batch)


def list_resume_score_batch_items(
    session: Session,
    *,
    batch_id: str,
) -> list[ResumeScoreBatchItemResponse]:
    if session.get(ResumeScoreBatch, batch_id) is None:
        raise ScoreServiceError("resume_score_batch_not_found")
    items = session.scalars(
        select(ResumeScoreBatchItem)
        .where(ResumeScoreBatchItem.batch_id == batch_id)
        .options(
            selectinload(ResumeScoreBatchItem.resume).selectinload(Resume.candidate)
        )
        .order_by(ResumeScoreBatchItem.updated_at.desc(), ResumeScoreBatchItem.id.desc())
    ).all()
    return [
        ResumeScoreBatchItemResponse(
            item_id=item.id,
            resume_id=item.resume_id,
            candidate_id=item.resume.candidate_id,
            candidate_display_name=item.resume.candidate.display_name,
            facts_version=item.facts_version,
            status=item.status,
            attempt_count=item.attempt_count,
            last_error=item.last_error,
            resume_score_id=item.resume_score_id,
            was_cached=item.was_cached,
            completed_at=item.completed_at.isoformat() if item.completed_at else None,
            updated_at=item.updated_at.isoformat(),
        )
        for item in items
    ]


def run_resume_score_batch_worker_once(
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
        select(ResumeScoreBatchItem, ResumeScoreBatch)
        .join(ResumeScoreBatch)
        .where(
            ResumeScoreBatchItem.status == ITEM_RUNNING,
            ResumeScoreBatchItem.lease_expires_at.is_not(None),
            ResumeScoreBatchItem.lease_expires_at <= now,
        )
        .execution_options(skip_organization_scope=True)
    ).all()
    for item, batch in expired_rows:
        organization_id = item.organization_id
        if not organization_id or batch.organization_id != organization_id:
            session.execute(
                ResumeScoreBatchItem.__table__.update()
                .where(ResumeScoreBatchItem.id == item.id)
                .values(
                    status=ITEM_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="resume_score_workspace_mismatch",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            # Do not allow a corrupt cross-workspace child row to leave the
            # owning batch permanently queued.  Recalculate using only the
            # batch's own workspace, which neither reads nor exposes the
            # mismatched item's candidate data.
            if batch.organization_id:
                with _organization_session(session, batch.organization_id):
                    session.expire(item)
                    _refresh_batch_progress(session, batch=batch, now=now)
                    session.flush()
            continue
        with _organization_session(session, organization_id):
            if item.attempt_count >= batch.max_attempts:
                item.status = ITEM_FAILED
                item.completed_at = now
            else:
                item.status = ITEM_QUEUED
                item.next_attempt_at = now
                item.completed_at = None
            item.lease_owner = None
            item.lease_expires_at = None
            item.last_error = "resume_score_worker_lease_expired"
            _refresh_batch_progress(session, batch=batch, now=now)
            session.flush()


def _claim_next_item(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedResumeScoreBatchItem | None:
    now = _utcnow()
    with database.session_factory() as session:
        _recover_expired_items(session, now=now)
        if not ai_gateway_credentials_configured(settings):
            session.commit()
            return None
        row = session.execute(
            select(ResumeScoreBatchItem, ResumeScoreBatch)
            .join(ResumeScoreBatch)
            .where(
                ResumeScoreBatch.status.in_((BATCH_QUEUED, BATCH_RUNNING)),
                ResumeScoreBatchItem.status == ITEM_QUEUED,
                or_(
                    ResumeScoreBatchItem.next_attempt_at.is_(None),
                    ResumeScoreBatchItem.next_attempt_at <= now,
                ),
            )
            .order_by(
                ResumeScoreBatch.requested_at.asc(),
                ResumeScoreBatchItem.next_attempt_at.asc(),
                ResumeScoreBatchItem.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        ).first()
        if row is None:
            session.commit()
            return None
        candidate_item, candidate_batch = row
        organization_id = candidate_item.organization_id
        if not organization_id or candidate_batch.organization_id != organization_id:
            session.execute(
                ResumeScoreBatchItem.__table__.update()
                .where(ResumeScoreBatchItem.id == candidate_item.id)
                .values(
                    status=ITEM_FAILED,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="resume_score_workspace_mismatch",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            if candidate_batch.organization_id:
                with _organization_session(session, candidate_batch.organization_id):
                    session.expire(candidate_item)
                    _refresh_batch_progress(session, batch=candidate_batch, now=now)
                    session.flush()
            session.commit()
            return None

        with _organization_session(session, organization_id):
            _persist_legacy_score_batch_route_pin(
                session,
                batch=candidate_batch,
                settings=settings,
            )
            claimed = session.execute(
                update(ResumeScoreBatchItem)
                .where(
                    ResumeScoreBatchItem.id == candidate_item.id,
                    ResumeScoreBatchItem.organization_id == organization_id,
                    ResumeScoreBatchItem.status == ITEM_QUEUED,
                    ResumeScoreBatchItem.attempt_count < candidate_batch.max_attempts,
                    or_(
                        ResumeScoreBatchItem.next_attempt_at.is_(None),
                        ResumeScoreBatchItem.next_attempt_at <= now,
                    ),
                )
                .values(
                    status=ITEM_RUNNING,
                    attempt_count=ResumeScoreBatchItem.attempt_count + 1,
                    next_attempt_at=None,
                    lease_owner=worker_id,
                    lease_expires_at=now + timedelta(seconds=_LEASE_SECONDS),
                    last_error=None,
                )
                # SQLite returns naive timestamps while the worker clock is
                # timezone-aware.  Never ask SQLAlchemy's in-Python
                # synchronizer to evaluate this lease predicate.
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.commit()
                return None
            # The conditional UPDATE deliberately bypassed ORM state
            # synchronization.  Reload the claimed row before serializing the
            # lease so we never return stale queued state to the worker.
            session.expire_all()
            item = session.get(ResumeScoreBatchItem, candidate_item.id)
            batch = session.get(ResumeScoreBatch, candidate_batch.id)
            if (
                item is None
                or batch is None
                or item.organization_id != organization_id
                or batch.organization_id != organization_id
                or item.batch_id != batch.id
            ):
                session.rollback()
                return None
            batch.status = BATCH_RUNNING
            batch.started_at = batch.started_at or now
            batch.lease_owner = worker_id
            batch.lease_expires_at = item.lease_expires_at
            session.commit()
            return ClaimedResumeScoreBatchItem(
                item_id=item.id,
                organization_id=organization_id,
                batch_id=batch.id,
                resume_id=item.resume_id,
                template_id=batch.template_id,
                template_version=batch.template_version,
                ai_route_policy_version_id=batch.ai_route_policy_version_id,
            )


def _process_claimed_item(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedResumeScoreBatchItem,
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
                    or batch.template_id != claimed.template_id
                    or batch.template_version != claimed.template_version
                    or batch.ai_route_policy_version_id
                    != claimed.ai_route_policy_version_id
                ):
                    raise ScoreServiceError("resume_score_workspace_mismatch")
                template, _ = _require_scoreable_template(
                    session,
                    template_id=claimed.template_id,
                )
                if (
                    template.organization_id != claimed.organization_id
                    or template.version != claimed.template_version
                ):
                    raise ScoreServiceError("resume_score_template_version_changed")
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
                    raise ScoreServiceError("resume_no_longer_ready_for_scoring")
                latest_snapshot, quality_flags = latest_snapshot_row
                if latest_snapshot.organization_id != claimed.organization_id:
                    raise ScoreServiceError("resume_score_workspace_mismatch")
                if has_unreliable_source_text(quality_flags):
                    raise ScoreServiceError("resume_source_text_unreliable")
                item.fact_snapshot_id = latest_snapshot.id
                item.facts_version = latest_snapshot.facts_version
                cached_score = session.scalar(
                    select(ResumeScore)
                    .where(
                        ResumeScore.organization_id == claimed.organization_id,
                        ResumeScore.template_id == claimed.template_id,
                        ResumeScore.template_version == claimed.template_version,
                        ResumeScore.fact_snapshot_id == latest_snapshot.id,
                        ResumeScore.status.in_(_REUSABLE_SCORE_STATUSES),
                    )
                    .order_by(ResumeScore.created_at.desc(), ResumeScore.id.desc())
                )
                if cached_score is not None:
                    score_id = cached_score.id
                    cached = True
                else:
                    response = run_resume_score(
                        session,
                        resume_id=item.resume_id,
                        payload=ResumeScoreCreate(template_id=claimed.template_id),
                        settings=settings,
                        pinned_route_policy_version_id=claimed.ai_route_policy_version_id,
                    )
                    persisted_score = session.get(ResumeScore, response.score_id)
                    if (
                        persisted_score is None
                        or persisted_score.organization_id != claimed.organization_id
                    ):
                        raise ScoreServiceError("resume_score_workspace_mismatch")
                    score_id = response.score_id
                    cached = False
                _finish_item_success(
                    session,
                    item=item,
                    worker_id=worker_id,
                    score_id=score_id,
                    was_cached=cached,
                )
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
    except (ScoreTemplateNotFoundError, ScoreServiceError) as exc:
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
            error="resume_score_worker_error",
            retryable=True,
        )


def _owned_item(
    session: Session,
    *,
    item_id: str,
    worker_id: str,
    organization_id: str,
) -> ResumeScoreBatchItem | None:
    return session.scalar(
        select(ResumeScoreBatchItem).where(
            ResumeScoreBatchItem.id == item_id,
            ResumeScoreBatchItem.organization_id == organization_id,
            ResumeScoreBatchItem.status == ITEM_RUNNING,
            ResumeScoreBatchItem.lease_owner == worker_id,
        )
    )


def _finish_item_success(
    session: Session,
    *,
    item: ResumeScoreBatchItem,
    worker_id: str,
    score_id: str,
    was_cached: bool,
) -> None:
    if item.lease_owner != worker_id or item.status != ITEM_RUNNING:
        raise ScoreServiceError("resume_score_batch_item_lease_lost")
    now = _utcnow()
    item.status = ITEM_COMPLETED
    item.resume_score_id = score_id
    item.was_cached = was_cached
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
                item.next_attempt_at = now + timedelta(
                    seconds=min(60, 2 ** (item.attempt_count - 1))
                )
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


def _refresh_batch_progress(
    session: Session,
    *,
    batch: ResumeScoreBatch,
    now: datetime,
) -> None:
    session.flush()
    counts = dict(
        session.execute(
            select(ResumeScoreBatchItem.status, func.count())
            .where(
                ResumeScoreBatchItem.batch_id == batch.id,
                ResumeScoreBatchItem.organization_id == batch.organization_id,
            )
            .group_by(ResumeScoreBatchItem.status)
        ).all()
    )
    # In the normal case this equals the immutable request-time count.  If a
    # database has a corrupt child row pointing at a batch in another
    # workspace, exclude that row rather than leaking its existence through a
    # tenant-visible aggregate or leaving this batch stuck forever.
    batch.total_count = sum(counts.values())
    batch.completed_count = counts.get(ITEM_COMPLETED, 0)
    batch.failed_count = counts.get(ITEM_FAILED, 0)
    batch.cached_count = int(
        session.scalar(
            select(func.count())
            .select_from(ResumeScoreBatchItem)
            .where(
                ResumeScoreBatchItem.batch_id == batch.id,
                ResumeScoreBatchItem.organization_id == batch.organization_id,
                ResumeScoreBatchItem.status == ITEM_COMPLETED,
                ResumeScoreBatchItem.was_cached.is_(True),
            )
        )
        or 0
    )
    pending = counts.get(ITEM_QUEUED, 0) + counts.get(ITEM_RUNNING, 0)
    if pending:
        batch.status = BATCH_RUNNING
        return
    batch.status = BATCH_PARTIAL if batch.failed_count else BATCH_COMPLETED
    batch.completed_at = now
    batch.lease_owner = None
    batch.lease_expires_at = None
    if batch.failed_count:
        batch.last_error = session.scalar(
            select(ResumeScoreBatchItem.last_error)
            .where(
                ResumeScoreBatchItem.batch_id == batch.id,
                ResumeScoreBatchItem.organization_id == batch.organization_id,
                ResumeScoreBatchItem.status == ITEM_FAILED,
            )
            .order_by(ResumeScoreBatchItem.updated_at.desc())
        )
    else:
        batch.last_error = None


__all__ = [
    "BATCH_COMPLETED",
    "BATCH_PARTIAL",
    "BATCH_QUEUED",
    "BATCH_RUNNING",
    "enqueue_resume_score_batch",
    "get_resume_score_batch",
    "list_resume_score_batch_items",
    "run_resume_score_batch_worker_once",
]
