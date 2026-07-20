"""Serialize email verification and add durable public-signup rate limits.

Revision ID: 20260720_0015
Revises: 20260720_0014
Create Date: 2026-07-20 22:15:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0015"
down_revision: Union[str, Sequence[str], None] = "20260720_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Canonicalize any rows that could have been created by an earlier build
    # before adding the database invariant.  This is portable to SQLite and
    # PostgreSQL because it only relies on a CTE and a window function.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id
                    ORDER BY requested_at DESC, id DESC
                ) AS row_rank
            FROM email_verification_tokens
            WHERE used_at IS NULL AND invalidated_at IS NULL
        )
        UPDATE email_verification_tokens
        SET invalidated_at = requested_at
        WHERE id IN (SELECT id FROM ranked WHERE row_rank > 1)
        """
    )
    op.create_index(
        "uq_active_email_verification_per_user",
        "email_verification_tokens",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
        postgresql_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
    )

    op.create_table(
        "registration_rate_limit_buckets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "key_digest",
            "window_started_at",
            name="uq_registration_rate_limit_window",
        ),
    )


def downgrade() -> None:
    op.drop_table("registration_rate_limit_buckets")
    op.drop_index(
        "uq_active_email_verification_per_user",
        table_name="email_verification_tokens",
    )
