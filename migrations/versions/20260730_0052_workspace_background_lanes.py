"""Add fair, fenced background-worker lanes per workspace.

Revision ID: 20260730_0052
Revises: 20260730_0051
Create Date: 2026-07-30 14:30:00

The table stores only scheduler metadata.  Candidate data, resume contents,
mailbox credentials, and provider payloads remain in their existing scoped
tables.  A row is shared by the worker pool to keep one busy workspace from
occupying every heavy-work slot.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0052"
down_revision: Union[str, Sequence[str], None] = "20260730_0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_background_lanes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lane_key", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_job_kind", sa.String(length=64), nullable=True),
        sa.Column("current_job_id", sa.String(length=36), nullable=True),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lane_key",
            "organization_id",
            name="uq_workspace_background_lane",
        ),
    )
    op.create_index(
        "ix_workspace_background_lanes_organization_id",
        "workspace_background_lanes",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_background_lane_claim",
        "workspace_background_lanes",
        ["lane_key", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_background_lane_fairness",
        "workspace_background_lanes",
        ["lane_key", "last_claimed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_background_lane_fairness",
        table_name="workspace_background_lanes",
    )
    op.drop_index(
        "ix_workspace_background_lane_claim",
        table_name="workspace_background_lanes",
    )
    op.drop_table("workspace_background_lanes")
