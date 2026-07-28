"""Add one-time historical workspace adoption state and an empty ORM fallback.

Revision ID: 20260728_0047
Revises: 20260728_0046
Create Date: 2026-07-28 18:10:00

This migration never reads account email addresses, files, candidate data,
credentials or deployment configuration. It creates only a fixed empty system
organization and the privacy-safe control-plane record used when a verified
account later adopts the existing historical organization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0047"
down_revision: Union[str, Sequence[str], None] = "20260728_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SYSTEM_FALLBACK_ORGANIZATION_ID = "00000000-0000-4000-8000-000000000004"
PLAN_ADVANCED_ID = "00000000-0000-4000-8000-000000000102"


def upgrade() -> None:
    op.create_table(
        "legacy_workspace_adoptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_organization_id", sa.String(length=36), nullable=False),
        sa.Column(
            "target_previous_organization_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("target_user_id", sa.String(length=36), nullable=False),
        sa.Column("target_membership_id", sa.String(length=36), nullable=False),
        sa.Column("retired_membership_id", sa.String(length=36), nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["target_previous_organization_id"],
            ["organizations.id"],
        ),
        sa.ForeignKeyConstraint(["target_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(
            ["target_membership_id"],
            ["organization_memberships.id"],
        ),
        sa.ForeignKeyConstraint(
            ["retired_membership_id"],
            ["organization_memberships.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_organization_id",
            name="uq_legacy_workspace_adoptions_source_organization",
        ),
        sa.UniqueConstraint(
            "target_membership_id",
            name="uq_legacy_workspace_adoptions_target_membership",
        ),
        sa.UniqueConstraint(
            "retired_membership_id",
            name="uq_legacy_workspace_adoptions_retired_membership",
        ),
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_source_organization_id",
        "legacy_workspace_adoptions",
        ["source_organization_id"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_target_previous_organization_id",
        "legacy_workspace_adoptions",
        ["target_previous_organization_id"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_target_user",
        "legacy_workspace_adoptions",
        ["target_user_id", "adopted_at"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_target_user_id",
        "legacy_workspace_adoptions",
        ["target_user_id"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_target_membership_id",
        "legacy_workspace_adoptions",
        ["target_membership_id"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_retired_membership_id",
        "legacy_workspace_adoptions",
        ["retired_membership_id"],
    )
    op.create_index(
        "ix_legacy_workspace_adoptions_adopted_at",
        "legacy_workspace_adoptions",
        ["adopted_at"],
    )

    organizations = sa.table(
        "organizations",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("plan_id", sa.String()),
        sa.column("plan_status", sa.String()),
        sa.column("trial_started_at", sa.DateTime(timezone=True)),
        sa.column("trial_ends_at", sa.DateTime(timezone=True)),
        sa.column("trial_llm_call_limit", sa.Integer()),
        sa.column("trial_llm_call_used", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        organizations,
        [
            {
                "id": SYSTEM_FALLBACK_ORGANIZATION_ID,
                "name": "System fallback workspace",
                "plan_id": PLAN_ADVANCED_ID,
                "plan_status": "suspended",
                "trial_started_at": None,
                "trial_ends_at": None,
                "trial_llm_call_limit": 1000,
                "trial_llm_call_used": 0,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("legacy_workspace_adoptions")
    # Keep the system fallback organization. It may safely own rows created by
    # an accidentally unscoped internal session; deleting it on code rollback
    # would risk foreign-key failure or data loss.
