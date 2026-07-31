"""Safely retire static authentication after adopting its legacy workspace.

Revision ID: 20260731_0053
Revises: 20260730_0052
Create Date: 2026-07-31 10:30:00

The former shared-password user owns the deterministic legacy workspace used
for historical recruiting records.  Before disabling that identity, grant the
same workspace to every verified, active, non-legacy platform administrator.
If there is no such formal administrator, fail before changing a row: leaving
the legacy sign-in active is safer than making historical candidates or their
original files inaccessible.

The migration never moves business data.  Candidate, resume, original-file,
and audit references retain their existing organization and storage IDs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0053"
down_revision: Union[str, Sequence[str], None] = "20260730_0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_USER_ID = "00000000-0000-4000-8000-000000000002"
LEGACY_MEMBERSHIP_ID = "00000000-0000-4000-8000-000000000003"


def _tables() -> tuple[sa.Table, sa.Table, sa.Table, sa.Table, sa.Table]:
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.String()),
    )
    users = sa.table(
        "user_accounts",
        sa.column("id", sa.String()),
        sa.column("auth_session_version", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_platform_admin", sa.Boolean()),
        sa.column("email_verified_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    memberships = sa.table(
        "organization_memberships",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("user_id", sa.String()),
        sa.column("role", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    invitations = sa.table(
        "organization_invitations",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("accepted_at", sa.DateTime(timezone=True)),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    platform_audit_events = sa.table(
        "platform_audit_events",
        sa.column("id", sa.String()),
        sa.column("actor_user_id", sa.String()),
        sa.column("action", sa.String()),
        sa.column("target_type", sa.String()),
        sa.column("target_id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("reason", sa.String()),
        sa.column("before_json", sa.JSON()),
        sa.column("after_json", sa.JSON()),
        sa.column("request_id", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    return organizations, users, memberships, invitations, platform_audit_events


def upgrade() -> None:
    """Adopt historical data first, then revoke only the shared identity."""

    organizations, users, memberships, invitations, platform_audit_events = _tables()
    bind = op.get_bind()

    # All preconditions are checked before any INSERT or UPDATE.  Alembic runs
    # this DML in one transaction, but this ordering also makes the failure
    # contract explicit for every supported database engine.
    source_exists = all(
        (
            bind.execute(
                sa.select(table.c.id).where(table.c.id == identifier)
            ).scalar_one_or_none()
            is not None
        )
        for table, identifier in (
            (organizations, LEGACY_ORGANIZATION_ID),
            (users, LEGACY_USER_ID),
            (memberships, LEGACY_MEMBERSHIP_ID),
        )
    )
    if not source_exists:
        raise RuntimeError("legacy_workspace_adoption_source_missing")

    administrator_ids = list(
        bind.execute(
            sa.select(users.c.id)
            .where(
                users.c.is_active.is_(True),
                users.c.is_platform_admin.is_(True),
                users.c.email_verified_at.is_not(None),
                users.c.id != LEGACY_USER_ID,
            )
            .order_by(users.c.id)
        ).scalars()
    )
    if not administrator_ids:
        raise RuntimeError("legacy_workspace_adoption_requires_verified_platform_admin")

    existing_rows = bind.execute(
        sa.select(
            memberships.c.id,
            memberships.c.user_id,
            memberships.c.role,
            memberships.c.is_active,
        ).where(
            memberships.c.organization_id == LEGACY_ORGANIZATION_ID,
            memberships.c.user_id.in_(administrator_ids),
        )
    ).mappings()
    existing_by_user_id = {str(row["user_id"]): row for row in existing_rows}
    now = datetime.now(timezone.utc)

    for administrator_id in administrator_ids:
        existing = existing_by_user_id.get(str(administrator_id))
        if existing is None:
            membership_id = str(uuid4())
            before_state: dict[str, object] = {"membership_present": False}
            bind.execute(
                sa.insert(memberships).values(
                    id=membership_id,
                    organization_id=LEGACY_ORGANIZATION_ID,
                    user_id=administrator_id,
                    role="admin",
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            membership_id = str(existing["id"])
            before_state = {
                "membership_present": True,
                "role": str(existing["role"]),
                "is_active": bool(existing["is_active"]),
            }
            bind.execute(
                sa.update(memberships)
                .where(memberships.c.id == membership_id)
                .values(role="admin", is_active=True, updated_at=now)
            )

        # A normal platform audit event is available before this migration.
        # It records only opaque IDs and status metadata, never CV contents,
        # source paths, email addresses, credentials, or prompt data.
        bind.execute(
            sa.insert(platform_audit_events).values(
                id=str(uuid4()),
                actor_user_id=administrator_id,
                action="legacy_workspace_adopted",
                target_type="organization_membership",
                target_id=membership_id,
                organization_id=LEGACY_ORGANIZATION_ID,
                reason="legacy_static_auth_retirement",
                before_json=before_state,
                after_json={
                    "membership_present": True,
                    "role": "admin",
                    "is_active": True,
                },
                request_id=None,
                created_at=now,
            )
        )

    # Old invitations were issued by a shared account and must not become a
    # side-door into the retained workspace after that account is retired.
    # The model has no cancellation column, so an immediate expiry is the
    # durable revocation mechanism already enforced by accept_invitation().
    # Keep the rows for historical/audit purposes; accepted invitations are
    # deliberately untouched.
    pending_invitation_ids = list(
        bind.execute(
            sa.select(invitations.c.id).where(
                invitations.c.organization_id == LEGACY_ORGANIZATION_ID,
                invitations.c.accepted_at.is_(None),
                invitations.c.expires_at > now,
            )
        ).scalars()
    )
    if pending_invitation_ids:
        bind.execute(
            sa.update(invitations)
            .where(invitations.c.id.in_(pending_invitation_ids))
            .values(expires_at=now)
        )
        bind.execute(
            sa.insert(platform_audit_events).values(
                id=str(uuid4()),
                actor_user_id=administrator_ids[0],
                action="legacy_workspace_invitations_revoked",
                target_type="organization_invitation_batch",
                target_id=LEGACY_ORGANIZATION_ID,
                organization_id=LEGACY_ORGANIZATION_ID,
                reason="legacy_static_auth_retirement",
                before_json={"pending_invitation_count": len(pending_invitation_ids)},
                after_json={"pending_invitation_count": 0},
                request_id=None,
                created_at=now,
            )
        )

    # Do not suspend or otherwise mutate the workspace.  Its plan state,
    # organization ID, candidate records, resume rows, storage paths, and
    # existing audit history remain untouched.
    bind.execute(
        sa.update(users)
        .where(users.c.id == LEGACY_USER_ID)
        .values(
            is_active=False,
            is_platform_admin=False,
            auth_session_version=users.c.auth_session_version + 1,
            updated_at=now,
        )
    )
    bind.execute(
        sa.update(memberships)
        .where(memberships.c.id == LEGACY_MEMBERSHIP_ID)
        .values(is_active=False, updated_at=now)
    )


def downgrade() -> None:
    """Never restore a retired shared credential during a code rollback."""

    # The adopted memberships and their audit trail remain intact. Re-enabling
    # the static shared account requires an explicit, separately reviewed
    # recovery operation rather than an incidental source rollback.
    return None
