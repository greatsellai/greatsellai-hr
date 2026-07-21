"""Persist workspace-local mailbox sync failure incidents.

Revision ID: 20260721_0025
Revises: 20260721_0024
Create Date: 2026-07-21 11:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0025"
down_revision: Union[str, Sequence[str], None] = "20260721_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mailbox_sync_failure_alerts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_job_id", sa.String(length=36), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mailbox_sync_failure_alerts_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_config_id"],
            ["mailbox_configs.id"],
            ondelete="CASCADE",
            name="fk_mailbox_sync_failure_alerts_mailbox_config_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mailbox_config_id",
            name="uq_mailbox_sync_failure_alerts_mailbox_config_id",
        ),
    )
    op.create_index(
        "ix_mailbox_sync_failure_alerts_organization_id",
        "mailbox_sync_failure_alerts",
        ["organization_id"],
    )
    op.create_index(
        "ix_mailbox_sync_failure_alerts_mailbox_config_id",
        "mailbox_sync_failure_alerts",
        ["mailbox_config_id"],
    )
    op.create_index(
        "ix_mailbox_sync_failure_alerts_state",
        "mailbox_sync_failure_alerts",
        ["state"],
    )
    op.create_index(
        "ix_mailbox_sync_failure_alerts_organization_state",
        "mailbox_sync_failure_alerts",
        ["organization_id", "state", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mailbox_sync_failure_alerts_organization_state",
        table_name="mailbox_sync_failure_alerts",
    )
    op.drop_index(
        "ix_mailbox_sync_failure_alerts_state",
        table_name="mailbox_sync_failure_alerts",
    )
    op.drop_index(
        "ix_mailbox_sync_failure_alerts_mailbox_config_id",
        table_name="mailbox_sync_failure_alerts",
    )
    op.drop_index(
        "ix_mailbox_sync_failure_alerts_organization_id",
        table_name="mailbox_sync_failure_alerts",
    )
    op.drop_table("mailbox_sync_failure_alerts")
