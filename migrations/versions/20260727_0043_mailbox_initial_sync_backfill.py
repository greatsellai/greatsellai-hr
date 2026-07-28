"""Add bounded first-bind mailbox history import state.

Revision ID: 20260727_0043
Revises: 20260727_0042
Create Date: 2026-07-27 23:20:00

Existing channels receive a zero-day window and no cutoff, so this migration
does not retrospectively import mail for any already connected inbox. New
channels can persist one frozen IMAP calendar cutoff and a completion marker.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0043"
down_revision: Union[str, Sequence[str], None] = "20260727_0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LOOKBACK_CHECK = "initial_sync_lookback_days >= 0 AND initial_sync_lookback_days <= 365"


def upgrade() -> None:
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "initial_sync_lookback_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column("initial_backfill_since_date", sa.Date(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "initial_backfill_completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_mailbox_configs_initial_sync_lookback_days",
            _LOOKBACK_CHECK,
        )

    with op.batch_alter_table("mailbox_oauth_connect_intents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "initial_sync_lookback_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_check_constraint(
            "ck_mailbox_oauth_connect_intents_initial_sync_lookback_days",
            _LOOKBACK_CHECK,
        )


def downgrade() -> None:
    with op.batch_alter_table("mailbox_oauth_connect_intents") as batch_op:
        batch_op.drop_constraint(
            "ck_mailbox_oauth_connect_intents_initial_sync_lookback_days",
            type_="check",
        )
        batch_op.drop_column("initial_sync_lookback_days")

    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.drop_constraint(
            "ck_mailbox_configs_initial_sync_lookback_days",
            type_="check",
        )
        batch_op.drop_column("initial_backfill_completed_at")
        batch_op.drop_column("initial_backfill_since_date")
        batch_op.drop_column("initial_sync_lookback_days")
