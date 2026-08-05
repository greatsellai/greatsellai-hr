"""Persist bounded recruiter-safe Agent execution summaries.

Revision ID: 20260803_0056
Revises: 20260803_0055
Create Date: 2026-08-03 14:00:00

Only a tool's short server-written label and recruiter-facing summary are
retained. This is not a checkpoint: prompts, internal reasoning, raw tool
arguments/results, candidate identifiers, and resume content are never stored
in this column.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0056"
down_revision: Union[str, Sequence[str], None] = "20260803_0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recruiting_agent_conversation_turns",
        sa.Column(
            "tool_trace",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("recruiting_agent_conversation_turns", "tool_trace")
