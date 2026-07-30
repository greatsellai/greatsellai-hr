"""Fair, fenced workspace lanes for shared heavy-work workers.

The deployment deliberately uses a shared worker pool rather than one
container per customer.  This service gives every workspace one *logical*
heavy-work slot at a time, then rotates the next free process to the least
recently served workspace.  The durable task's own lease remains the source
of truth for task recovery; this separate fence controls only fair capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import WorkspaceBackgroundLane


HEAVY_WORKSPACE_LANE = "heavy_background"
_MAX_WORKSPACE_CANDIDATES_PER_CLAIM = 32


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
