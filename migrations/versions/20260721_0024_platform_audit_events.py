"""Add privacy-safe platform control-plane audit events.

Revision ID: 20260721_0024
Revises: 20260720_0023
Create Date: 2026-07-21 10:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0024"
down_revision: Union[str, Sequence[str], None] = "20260720_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("after_json", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_platform_audit_events_actor_user_id", "platform_audit_events", ["actor_user_id"])
    op.create_index("ix_platform_audit_events_action", "platform_audit_events", ["action"])
    op.create_index("ix_platform_audit_events_target_type", "platform_audit_events", ["target_type"])
    op.create_index("ix_platform_audit_events_target_id", "platform_audit_events", ["target_id"])
    op.create_index("ix_platform_audit_events_organization_id", "platform_audit_events", ["organization_id"])
    op.create_index("ix_platform_audit_events_request_id", "platform_audit_events", ["request_id"])
    op.create_index(
        "ix_platform_audit_events_created",
        "platform_audit_events",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_platform_audit_events_actor_created",
        "platform_audit_events",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_platform_audit_events_target_created",
        "platform_audit_events",
        ["target_type", "target_id", "created_at"],
    )
    op.create_index(
        "ix_platform_audit_events_organization_created",
        "platform_audit_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("platform_audit_events")
