"""Fair, fenced workspace lanes for shared heavy-work workers.

The deployment deliberately uses a shared worker pool rather than one
container per customer.  This service gives every workspace one *logical*
heavy-work slot at a time, then rotates the next free process to the least
recently served workspace.  The durable task's own lease remains the source
of truth for task recovery; this separate fence controls only fair capacity.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import WorkspaceBackgroundLane


if TYPE_CHECKING:
    from app.database import Database


HEAVY_WORKSPACE_LANE = "heavy_background"
_MAX_WORKSPACE_CANDIDATES_PER_CLAIM = 32
_HEARTBEAT_MAX_INTERVAL_SECONDS = 30.0
_HEARTBEAT_MIN_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class ClaimedWorkspaceBackgroundLane:
    organization_id: str
    lease_token: str
    lease_expires_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fair_available_workspace_ids(
    session: Session,
    *,
    source: Any,
    organization_id_column: Any,
    eligible: Any,
    next_attempt_at_column: Any,
    requested_at_column: Any,
    now: datetime,
    lane_key: str = HEAVY_WORKSPACE_LANE,
    limit: int = _MAX_WORKSPACE_CANDIDATES_PER_CLAIM,
) -> list[str]:
    """Return claimable workspaces in fair round-robin order.

    Rows with an unexpired lane are excluded in SQL, rather than being picked
    first and rejected afterwards.  That distinction is what lets workspace B
    run immediately when workspace A has hundreds of older queued resumes.
    ``source`` may be one ORM model or a joined selectable for batch queues.
    """

    lane = aliased(WorkspaceBackgroundLane)
    oldest_due = func.min(
        func.coalesce(next_attempt_at_column, requested_at_column)
    ).label("oldest_due")
    oldest_requested = func.min(requested_at_column).label("oldest_requested")
    unseen_rank = case((lane.last_claimed_at.is_(None), 0), else_=1)
    statement = (
        select(
            organization_id_column.label("organization_id"),
            oldest_due,
            oldest_requested,
            lane.last_claimed_at.label("last_claimed_at"),
        )
        .select_from(source)
        .outerjoin(
            lane,
            and_(
                lane.lane_key == lane_key,
                lane.organization_id == organization_id_column,
            ),
        )
        .where(
            eligible,
            organization_id_column.is_not(None),
            or_(
                lane.id.is_(None),
                lane.lease_expires_at.is_(None),
                lane.lease_expires_at <= now,
            ),
        )
        .group_by(
            organization_id_column,
            lane.id,
            lane.last_claimed_at,
        )
        .order_by(
            unseen_rank.asc(),
            lane.last_claimed_at.asc(),
            oldest_due.asc(),
            oldest_requested.asc(),
            organization_id_column.asc(),
        )
        .limit(limit)
        .execution_options(skip_organization_scope=True)
    )
    return [str(row.organization_id) for row in session.execute(statement).all()]


def acquire_workspace_background_lane(
    session: Session,
    *,
    organization_id: str,
    worker_id: str,
    job_kind: str,
    job_id: str,
    lease_seconds: int,
    now: datetime | None = None,
    lane_key: str = HEAVY_WORKSPACE_LANE,
) -> ClaimedWorkspaceBackgroundLane | None:
    """Atomically reserve a free workspace lane, returning a fenced token.

    Callers keep this operation in the same transaction as their task's
    conditional ``queued -> running`` transition.  If that task transition
    loses a race, rolling back also rolls back this lane reservation.
    """

    if not organization_id or lease_seconds < 1:
        return None
    claimed_at = now or utcnow()
    expires_at = claimed_at + timedelta(seconds=lease_seconds)
    token = uuid4().hex
    values = {
        "lease_owner": worker_id,
        "lease_token": token,
        "lease_expires_at": expires_at,
        "current_job_kind": job_kind,
        "current_job_id": job_id,
        "last_claimed_at": claimed_at,
        "updated_at": claimed_at,
    }
    renewed_existing = session.execute(
        update(WorkspaceBackgroundLane)
        .where(
            WorkspaceBackgroundLane.lane_key == lane_key,
            WorkspaceBackgroundLane.organization_id == organization_id,
            or_(
                WorkspaceBackgroundLane.lease_expires_at.is_(None),
                WorkspaceBackgroundLane.lease_expires_at <= claimed_at,
            ),
        )
        .values(**values)
        .execution_options(skip_organization_scope=True, synchronize_session=False)
    )
    if renewed_existing.rowcount == 1:
        return ClaimedWorkspaceBackgroundLane(
            organization_id=organization_id,
            lease_token=token,
            lease_expires_at=expires_at,
        )

    lane = WorkspaceBackgroundLane(
        lane_key=lane_key,
        organization_id=organization_id,
        **values,
    )
    try:
        # A nested transaction keeps a concurrent first-insert collision from
        # poisoning the caller's surrounding task-claim transaction.
        with session.begin_nested():
            session.add(lane)
            session.flush()
    except IntegrityError:
        return None
    return ClaimedWorkspaceBackgroundLane(
        organization_id=organization_id,
        lease_token=token,
        lease_expires_at=expires_at,
    )


def release_workspace_background_lane(
    session: Session,
    *,
    organization_id: str,
    lease_token: str,
    lane_key: str = HEAVY_WORKSPACE_LANE,
    now: datetime | None = None,
) -> bool:
    """Release only the exact fenced lease held by one claimed task."""

    if not organization_id or not lease_token:
        return False
    released_at = now or utcnow()
    released = session.execute(
        update(WorkspaceBackgroundLane)
        .where(
            WorkspaceBackgroundLane.lane_key == lane_key,
            WorkspaceBackgroundLane.organization_id == organization_id,
            WorkspaceBackgroundLane.lease_token == lease_token,
        )
        .values(
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            current_job_kind=None,
            current_job_id=None,
            updated_at=released_at,
        )
        .execution_options(skip_organization_scope=True, synchronize_session=False)
    )
    return released.rowcount == 1


def renew_workspace_background_lane(
    session: Session,
    *,
    organization_id: str,
    lease_token: str,
    lease_seconds: int,
    lane_key: str = HEAVY_WORKSPACE_LANE,
    now: datetime | None = None,
) -> bool:
    """Extend a live fenced lease before another slow external operation."""

    if not organization_id or not lease_token or lease_seconds < 1:
        return False
    renewed_at = now or utcnow()
    renewed = session.execute(
        update(WorkspaceBackgroundLane)
        .where(
            WorkspaceBackgroundLane.lane_key == lane_key,
            WorkspaceBackgroundLane.organization_id == organization_id,
            WorkspaceBackgroundLane.lease_token == lease_token,
            WorkspaceBackgroundLane.lease_expires_at.is_not(None),
            WorkspaceBackgroundLane.lease_expires_at > renewed_at,
        )
        .values(
            lease_expires_at=renewed_at + timedelta(seconds=lease_seconds),
            updated_at=renewed_at,
        )
        .execution_options(skip_organization_scope=True, synchronize_session=False)
    )
    return renewed.rowcount == 1


def renew_claimed_workspace_job_lease(
    session: Session,
    *,
    job_model: Any,
    job_id: str,
    organization_id: str,
    worker_id: str,
    running_status: str,
    job_lease_seconds: int,
    workspace_lane_token: str,
    workspace_lane_lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Atomically renew a running task and its fenced workspace lane.

    The durable task lease and the fair-lane lease are two halves of one
    ownership boundary.  Renewing only one lets a second process either
    recover the task or enter the workspace while an external model/OCR call
    is still running.  A failed renewal is deliberately not retried in this
    transaction: callers must treat the claim as lost and never revive it.
    """

    if (
        not job_id
        or not organization_id
        or not worker_id
        or not workspace_lane_token
        or job_lease_seconds < 1
        or workspace_lane_lease_seconds < 1
    ):
        return False
    renewed_at = now or utcnow()
    task_renewed = session.execute(
        update(job_model)
        .where(
            job_model.id == job_id,
            job_model.organization_id == organization_id,
            job_model.status == running_status,
            job_model.lease_owner == worker_id,
            job_model.lease_expires_at.is_not(None),
            job_model.lease_expires_at > renewed_at,
        )
        .values(
            lease_expires_at=renewed_at + timedelta(seconds=job_lease_seconds)
        )
        .execution_options(skip_organization_scope=True, synchronize_session=False)
    )
    if task_renewed.rowcount != 1:
        session.rollback()
        return False
    lane_renewed = renew_workspace_background_lane(
        session,
        organization_id=organization_id,
        lease_token=workspace_lane_token,
        lease_seconds=workspace_lane_lease_seconds,
        now=renewed_at,
    )
    if not lane_renewed:
        session.rollback()
        return False
    session.commit()
    return True


def release_workspace_lane_for_inactive_job(
    session: Session,
    *,
    job_model: Any,
    job_id: str,
    organization_id: str,
    job_kind: str,
    running_status: str,
    now: datetime | None = None,
    lane_key: str = HEAVY_WORKSPACE_LANE,
) -> bool:
    """Clear an orphaned lane only when its recorded task is no longer live.

    Lease recovery may requeue a crashed task before a configured lane's
    natural expiry.  This predicate protects a concurrently renewed/reclaimed
    task: a lane is cleared only when this exact job is not still running with
    an unexpired durable lease in the same transaction.
    """

    if not job_id or not organization_id or not job_kind:
        return False
    released_at = now or utcnow()
    still_running = session.scalar(
        select(job_model.id)
        .where(
            job_model.id == job_id,
            job_model.organization_id == organization_id,
            job_model.status == running_status,
            job_model.lease_expires_at.is_not(None),
            job_model.lease_expires_at > released_at,
        )
        .limit(1)
        .execution_options(skip_organization_scope=True)
    )
    if still_running is not None:
        return False
    released = session.execute(
        update(WorkspaceBackgroundLane)
        .where(
            WorkspaceBackgroundLane.lane_key == lane_key,
            WorkspaceBackgroundLane.organization_id == organization_id,
            WorkspaceBackgroundLane.current_job_kind == job_kind,
            WorkspaceBackgroundLane.current_job_id == job_id,
        )
        .values(
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            current_job_kind=None,
            current_job_id=None,
            updated_at=released_at,
        )
        .execution_options(skip_organization_scope=True, synchronize_session=False)
    )
    return released.rowcount == 1


def _lease_heartbeat_interval_seconds(
    *,
    job_lease_seconds: int,
    workspace_lane_lease_seconds: int,
) -> float:
    """Renew well before the shorter lease can expire without DB churn."""

    return max(
        _HEARTBEAT_MIN_INTERVAL_SECONDS,
        min(
            _HEARTBEAT_MAX_INTERVAL_SECONDS,
            min(job_lease_seconds, workspace_lane_lease_seconds) / 3,
        ),
    )


@contextmanager
def maintain_claimed_workspace_job_lease(
    database: Database,
    *,
    job_model: Any,
    job_id: str,
    organization_id: str,
    worker_id: str,
    running_status: str,
    job_lease_seconds: int,
    workspace_lane_token: str,
    workspace_lane_lease_seconds: int,
) -> Iterator[None]:
    """Keep a slow external operation fenced without holding a DB session.

    A worker uses one small connection pool per child process.  The heartbeat
    opens a short-lived separate session only while renewing, so model/OCR/
    IMAP work never holds a database transaction.  If ownership has already
    been lost, the heartbeat stops; existing completion writes are fenced by
    their normal running-job checks and will safely refuse to persist results.
    """

    stop_event = Event()
    interval_seconds = _lease_heartbeat_interval_seconds(
        job_lease_seconds=job_lease_seconds,
        workspace_lane_lease_seconds=workspace_lane_lease_seconds,
    )

    def heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                with database.session_factory() as session:
                    renewed = renew_claimed_workspace_job_lease(
                        session,
                        job_model=job_model,
                        job_id=job_id,
                        organization_id=organization_id,
                        worker_id=worker_id,
                        running_status=running_status,
                        job_lease_seconds=job_lease_seconds,
                        workspace_lane_token=workspace_lane_token,
                        workspace_lane_lease_seconds=workspace_lane_lease_seconds,
                    )
            except Exception:
                # A short database outage must not terminate the foreground
                # operation.  The next bounded heartbeat can still renew it;
                # completion remains conditionally fenced if the lease lapses.
                continue
            if not renewed:
                return

    thread = Thread(
        target=heartbeat,
        name="resume-v3-workspace-lease-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1.0)
