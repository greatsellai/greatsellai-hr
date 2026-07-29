"""Add durable worker liveness records for safe runtime diagnostics.

Revision ID: 20260729_0047
Revises: 20260728_0046
Create Date: 2026-07-29 09:00:00

The table contains process-health metadata only. It intentionally has no
workspace, candidate, mailbox, user, source-file, request, or provider-payload
columns, so a platform runtime view cannot become a secondary data archive.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0047"
down_revision: Union[str, Sequence[str], None] = "20260728_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=160), nullable=False),
        sa.Column("worker_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_cycle_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'stopped')",
            name="ck_runtime_worker_heartbeat_status",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_runtime_worker_heartbeat_kind_seen",
        "runtime_worker_heartbeats",
        ["worker_kind", "last_seen_at"],
    )
    op.create_index(
        "ix_runtime_worker_heartbeat_status_seen",
        "runtime_worker_heartbeats",
        ["status", "last_seen_at"],
    )
    op.create_index(
        "ix_runtime_worker_heartbeat_last_seen",
        "runtime_worker_heartbeats",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_worker_heartbeat_last_seen",
        table_name="runtime_worker_heartbeats",
    )
    op.drop_index(
        "ix_runtime_worker_heartbeat_status_seen",
        table_name="runtime_worker_heartbeats",
    )
    op.drop_index(
        "ix_runtime_worker_heartbeat_kind_seen",
        table_name="runtime_worker_heartbeats",
    )
    op.drop_table("runtime_worker_heartbeats")
