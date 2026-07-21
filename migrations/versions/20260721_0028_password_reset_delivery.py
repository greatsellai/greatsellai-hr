"""Make password reset links single-active and delivery-ready.

Revision ID: 20260721_0028
Revises: 20260721_0027
Create Date: 2026-07-21 16:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0028"
down_revision: Union[str, Sequence[str], None] = "20260721_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "password_reset_tokens",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Before this migration the reset flow allowed multiple unused rows for
    # the same account.  Invalidate them all before introducing the partial
    # unique index: an old, already-issued recovery link must not survive a
    # security upgrade, and retaining more than one would make the index fail
    # on a populated production database.
    op.execute(
        """
        UPDATE password_reset_tokens
        SET invalidated_at = requested_at
        WHERE used_at IS NULL
        """
    )
    op.create_index(
        "uq_active_password_reset_per_user",
        "password_reset_tokens",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
        postgresql_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_password_reset_per_user", table_name="password_reset_tokens")
    op.drop_column("password_reset_tokens", "invalidated_at")
