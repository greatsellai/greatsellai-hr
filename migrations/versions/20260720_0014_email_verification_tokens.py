"""Add one-time account email verification tokens.

Revision ID: 20260720_0014
Revises: 20260720_0013
Create Date: 2026-07-20 20:30:00

Existing accounts predate an email-verification flow.  They are explicitly
grandfathered as verified so this security upgrade does not lock any current
workspace out of its own data.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0014"
down_revision: Union[str, Sequence[str], None] = "20260720_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_delivery_error", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_email_verification_tokens_token_digest",
        "email_verification_tokens",
        ["token_digest"],
        unique=True,
    )
    op.create_index(
        "ix_email_verification_tokens_expires_at",
        "email_verification_tokens",
        ["expires_at"],
    )
    op.create_index(
        "ix_email_verification_tokens_user_requested",
        "email_verification_tokens",
        ["user_id", "requested_at"],
    )
    op.create_index(
        "ix_email_verification_tokens_expiry",
        "email_verification_tokens",
        ["expires_at"],
    )

    accounts = sa.table(
        "user_accounts",
        sa.column("email_verified_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        accounts.update()
        .where(accounts.c.email_verified_at.is_(None))
        .values(email_verified_at=accounts.c.created_at)
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_tokens_expiry", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_user_requested", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_expires_at", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_token_digest", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_user_id", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
