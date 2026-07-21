"""Revoke signed browser sessions after account password changes.

Revision ID: 20260721_0029
Revises: 20260721_0028
Create Date: 2026-07-21 17:15:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0029"
down_revision: Union[str, Sequence[str], None] = "20260721_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The server default safely initializes every historical account and any
    # direct maintenance insert.  Runtime sessions issued before this upgrade
    # are treated as version 1 for a compatible, one-time transition.
    op.add_column(
        "user_accounts",
        sa.Column(
            "auth_session_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_accounts", "auth_session_version")
