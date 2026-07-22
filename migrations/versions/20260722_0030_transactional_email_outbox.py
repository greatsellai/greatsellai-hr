"""Queue password-recovery email outside HTTP request handling.

Revision ID: 20260722_0030
Revises: 20260721_0029
Create Date: 2026-07-22 10:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0030"
down_revision: Union[str, Sequence[str], None] = "20260721_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transactional_email_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_kind", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("password_reset_token_id", sa.String(length=36), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["password_reset_token_id"], ["password_reset_tokens.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "password_reset_token_id",
            name="uq_transactional_email_outbox_password_reset_token",
        ),
    )
    op.create_index(
        "ix_transactional_email_outbox_due",
        "transactional_email_outbox",
        ["status", "next_attempt_at", "requested_at"],
    )
    op.create_index(
        "ix_transactional_email_outbox_user_requested",
        "transactional_email_outbox",
        ["user_id", "requested_at"],
    )
    op.create_index(
        "ix_transactional_email_outbox_message_kind",
        "transactional_email_outbox",
        ["message_kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactional_email_outbox_message_kind", table_name="transactional_email_outbox")
    op.drop_index("ix_transactional_email_outbox_user_requested", table_name="transactional_email_outbox")
    op.drop_index("ix_transactional_email_outbox_due", table_name="transactional_email_outbox")
    op.drop_table("transactional_email_outbox")
