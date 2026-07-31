"""Safely retire static authentication after adopting its legacy workspace.

Revision ID: 20260731_0053
Revises: 20260730_0052
Create Date: 2026-07-31 10:30:00

The former shared-password user owns the deterministic legacy workspace used
for historical recruiting records.  Before disabling that identity, grant the
same workspace to every verified, active, non-legacy platform administrator.
If there is no such formal administrator, fail before changing a row unless a
deployment owner explicitly identifies one already verified named account for
the one-time handoff. Leaving the legacy sign-in active is safer than making
historical candidates or their original files inaccessible. The sole exception
is an empty legacy bootstrap workspace: it has no historical workspace data
or extra legacy members, so it may retire the unused static identity without
blocking a fresh installation.

The migration never moves business data.  Candidate, resume, original-file,
and audit references retain their existing organization and storage IDs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0053"
down_revision: Union[str, Sequence[str], None] = "20260730_0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000001"
LEGACY_USER_ID = "00000000-0000-4000-8000-000000000002"
LEGACY_MEMBERSHIP_ID = "00000000-0000-4000-8000-000000000003"
LEGACY_ADOPTION_USER_ID_ENV = "RESUME_V3_LEGACY_WORKSPACE_ADOPTION_USER_ID"
SYSTEM_MIGRATION_ACTOR_KIND = "system_migration"
SYSTEM_MIGRATION_REQUEST_ID = "system:migration:20260731_0053"


# Memberships are checked separately because the deterministic legacy
# membership is a bootstrap row.  The retention-policy table is skipped by
# the generic row scanner only because it is checked below for one *exact*
# default row; a changed policy is operator intent and blocks retirement.
_BOOTSTRAP_ONLY_SCOPED_TABLES = {
    "candidate_data_retention_policies",
    "organization_memberships",
}


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
        sa.column("actor_kind", sa.String()),
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


def _legacy_workspace_has_business_records(bind: sa.Connection) -> bool:
    """Return whether a legacy-scoped row proves this is not a fresh install.

    The tenant model has grown over time, so hard-coding only today's resume
    tables would make a later root table invisible to this safety gate.  At
    this migration head every tenant-owned table carries ``organization_id``;
    inspect that schema and conservatively treat any non-bootstrap legacy row
    as historical data that requires a formal administrator before retirement.
    """

    inspector = sa.inspect(bind)
    metadata = sa.MetaData()
    for table_name in inspector.get_table_names():
        if table_name in _BOOTSTRAP_ONLY_SCOPED_TABLES:
            continue
        column_names = {column["name"] for column in inspector.get_columns(table_name)}
        if "organization_id" not in column_names:
            continue
        table = sa.Table(table_name, metadata, autoload_with=bind)
        row_exists = bind.execute(
            sa.select(sa.literal(True))
            .select_from(table)
            .where(table.c.organization_id == LEGACY_ORGANIZATION_ID)
            .limit(1)
        ).scalar_one_or_none()
        if row_exists is not None:
            return True
    return False


def _has_exact_default_legacy_retention_policy(bind: sa.Connection) -> bool:
    """Allow only the one unchanged retention-policy row created by bootstrap.

    This condition is intentionally exact.  A person may configure retention
    before uploading a candidate, and that is enough evidence that the legacy
    workspace is in use.  Treat any extra, missing, or changed policy row as
    history that requires a verified platform administrator to adopt access.
    """

    policies = sa.table(
        "candidate_data_retention_policies",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("mode", sa.String()),
        sa.column("retention_days", sa.Integer()),
        sa.column("version", sa.Integer()),
        sa.column("updated_by_user_id", sa.String()),
    )
    rows = bind.execute(
        sa.select(
            policies.c.id,
            policies.c.mode,
            policies.c.retention_days,
            policies.c.version,
            policies.c.updated_by_user_id,
        )
        .where(policies.c.organization_id == LEGACY_ORGANIZATION_ID)
        .with_for_update()
    ).mappings().all()
    if len(rows) != 1:
        return False

    policy = rows[0]
    return (
        policy["mode"] == "manual"
        and policy["retention_days"] is None
        and policy["version"] == 1
        and policy["updated_by_user_id"] is None
    )


def _is_pristine_legacy_bootstrap(
    bind: sa.Connection,
    *,
    memberships: sa.Table,
) -> bool:
    """Recognize only the exact no-data state of the legacy workspace.

    This is deliberately stricter than "no candidates".  An extra membership
    in the legacy workspace, invitation, job, mailbox record, audit record,
    changed retention configuration, or any other tenant row means an operator
    may need legacy access.  In that case a verified platform administrator
    must adopt it before static access can be retired.  Users and memberships
    in other workspaces are irrelevant: a newly registered tenant must not
    keep an empty, unrelated bootstrap workspace alive forever.
    """

    has_extra_legacy_membership = (
        bind.execute(
            sa.select(memberships.c.id)
            .where(
                memberships.c.organization_id == LEGACY_ORGANIZATION_ID,
                memberships.c.id != LEGACY_MEMBERSHIP_ID,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )
    if has_extra_legacy_membership:
        return False

    if not _has_exact_default_legacy_retention_policy(bind):
        return False

    return not _legacy_workspace_has_business_records(bind)


def _configured_legacy_adoption_user_id() -> str | None:
    """Return one operator-selected formal account, never an inferred user.

    A historical single-account installation can predate every named platform
    administrator.  In that state the deployment owner may explicitly name
    one *existing*, verified account for the one-time handoff.  The value is
    intentionally an opaque UUID rather than an email, and it is read only by
    the migration container.  Empty configuration is not an error by itself:
    the caller still decides whether the workspace is pristine or must fail
    closed.
    """

    raw_user_id = os.environ.get(LEGACY_ADOPTION_USER_ID_ENV, "").strip()
    if not raw_user_id:
        return None
    try:
        return str(UUID(raw_user_id))
    except (AttributeError, ValueError) as exc:
        raise RuntimeError(
            "legacy_workspace_adoption_configured_user_ineligible"
        ) from exc


def _resolve_configured_legacy_adopter(
    bind: sa.Connection,
    *,
    users: sa.Table,
) -> str | None:
    """Validate the operator-selected handoff target without writing state.

    Configuration may only select an account that already proved control of a
    real mailbox through the normal registration path.  In particular, it
    cannot revive the shared legacy identity, create a new user, or silently
    elevate whichever ordinary account happens to sort first.
    """

    configured_user_id = _configured_legacy_adoption_user_id()
    if configured_user_id is None:
        return None

    configured_user = (
        bind.execute(
            sa.select(
                users.c.id,
                users.c.is_active,
                users.c.email_verified_at,
            )
            .where(users.c.id == configured_user_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if (
        configured_user is None
        or str(configured_user["id"]) == LEGACY_USER_ID
        or not bool(configured_user["is_active"])
        or configured_user["email_verified_at"] is None
    ):
        raise RuntimeError("legacy_workspace_adoption_configured_user_ineligible")
    return str(configured_user["id"])


def upgrade() -> None:
    """Adopt historical data first, then revoke only the shared identity."""

    organizations, users, memberships, invitations, platform_audit_events = _tables()
    bind = op.get_bind()

    # Historical platform-audit rows all represent named control-plane actors.
    # This one-off deployment migration is different: it is performed by the
    # release system, not by the retiring shared account or by the account
    # receiving access.  Preserve that distinction in the schema instead of
    # falsely attributing the privilege change to either user.
    with op.batch_alter_table("platform_audit_events") as batch_op:
        batch_op.alter_column(
            "actor_user_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "actor_kind",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'user'"),
            )
        )

    # All preconditions are checked before any INSERT or UPDATE.  Lock the
    # static user and membership in the same order used by invitation writes.
    # The release helper also quiesces old API/worker writers before this
    # migration, which is the cross-version fence for an old binary that does
    # not yet know these locks.
    organization_exists = (
        bind.execute(
            sa.select(organizations.c.id).where(
                organizations.c.id == LEGACY_ORGANIZATION_ID
            )
        ).scalar_one_or_none()
        is not None
    )
    legacy_user_exists = (
        bind.execute(
            sa.select(users.c.id)
            .where(users.c.id == LEGACY_USER_ID)
            .with_for_update()
        ).scalar_one_or_none()
        is not None
    )
    legacy_membership_exists = (
        bind.execute(
            sa.select(memberships.c.id)
            .where(memberships.c.id == LEGACY_MEMBERSHIP_ID)
            .with_for_update()
        ).scalar_one_or_none()
        is not None
    )
    if not (organization_exists and legacy_user_exists and legacy_membership_exists):
        raise RuntimeError("legacy_workspace_adoption_source_missing")

    # Lock every existing invite before classifying or expiring it.  New code
    # reloads the same rows with ``FOR UPDATE`` before it can create/accept an
    # invitation.  The release quiesce above covers the previous API image.
    bind.execute(
        sa.select(invitations.c.id)
        .where(invitations.c.organization_id == LEGACY_ORGANIZATION_ID)
        .with_for_update()
    ).all()

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
    now = datetime.now(timezone.utc)
    if not administrator_ids:
        # A pristine install reaches this migration with only the deterministic
        # bootstrap organization/user/membership and its unchanged manual retention
        # policy.  No candidate or other tenant data can become inaccessible,
        # so retiring the unused static identity is safe and lets fresh/staging
        # databases reach ``head``.  Every other state fails before a write.
        if not _is_pristine_legacy_bootstrap(
            bind,
            memberships=memberships,
        ):
            configured_adopter_id = _resolve_configured_legacy_adopter(
                bind,
                users=users,
            )
            if configured_adopter_id is None:
                raise RuntimeError(
                    "legacy_workspace_adoption_requires_verified_platform_admin"
                )

            # This is an explicit deployment-owner handoff, not a user action.
            # ``actor_user_id`` is intentionally NULL and ``actor_kind`` says
            # ``system_migration`` so neither the retired shared identity nor
            # the selected recipient is misrepresented as the actor.  Do not
            # record the environment variable or an email.
            bind.execute(
                sa.update(users)
                .where(users.c.id == configured_adopter_id)
                .values(is_platform_admin=True, updated_at=now)
            )
            bind.execute(
                sa.insert(platform_audit_events).values(
                    id=str(uuid4()),
                    actor_user_id=None,
                    actor_kind=SYSTEM_MIGRATION_ACTOR_KIND,
                    action="legacy_workspace_adoption_bootstrap",
                    target_type="user_account",
                    target_id=configured_adopter_id,
                    organization_id=LEGACY_ORGANIZATION_ID,
                    reason="legacy_static_auth_retirement_explicit_deployment_adopter",
                    before_json={"is_platform_admin": False},
                    after_json={"is_platform_admin": True},
                    request_id=SYSTEM_MIGRATION_REQUEST_ID,
                    created_at=now,
                )
            )
            administrator_ids = [configured_adopter_id]

    existing_rows = bind.execute(
        sa.select(
            memberships.c.id,
            memberships.c.user_id,
            memberships.c.role,
            memberships.c.is_active,
        )
        .where(
            memberships.c.organization_id == LEGACY_ORGANIZATION_ID,
            memberships.c.user_id.in_(administrator_ids),
        )
        .with_for_update()
    ).mappings()
    existing_by_user_id = {str(row["user_id"]): row for row in existing_rows}
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
                actor_user_id=None,
                actor_kind=SYSTEM_MIGRATION_ACTOR_KIND,
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
                request_id=SYSTEM_MIGRATION_REQUEST_ID,
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
            sa.select(invitations.c.id)
            .where(
                invitations.c.organization_id == LEGACY_ORGANIZATION_ID,
                invitations.c.accepted_at.is_(None),
                invitations.c.expires_at > now,
            )
            .with_for_update()
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
                actor_user_id=None,
                actor_kind=SYSTEM_MIGRATION_ACTOR_KIND,
                action="legacy_workspace_invitations_revoked",
                target_type="organization_invitation_batch",
                target_id=LEGACY_ORGANIZATION_ID,
                organization_id=LEGACY_ORGANIZATION_ID,
                reason="legacy_static_auth_retirement",
                before_json={"pending_invitation_count": len(pending_invitation_ids)},
                after_json={"pending_invitation_count": 0},
                request_id=SYSTEM_MIGRATION_REQUEST_ID,
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
