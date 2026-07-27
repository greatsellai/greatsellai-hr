"""Store bounded, private recruiter-visible Agent chat turns.

Revision ID: 20260727_0041
Revises: 20260727_0040
Create Date: 2026-07-27 23:05:00

Each row is one successful user/assistant exchange.  This is deliberately not
a LangGraph checkpoint: prompts, tool calls and payloads, candidate cards,
source evidence, and resume text are never stored here.  The parent
conversation's tenant/owner boundary and 24-hour inactivity TTL apply to this
child through the composite cascade.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0041"
down_revision: Union[str, Sequence[str], None] = "20260727_0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recruiting_agent_conversation_turns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("context_version", sa.Integer(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("assistant_message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            [
                "recruiting_agent_conversations.id",
                "recruiting_agent_conversations.organization_id",
            ],
            name="fk_agent_turn_conversation_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "organization_id",
            "context_version",
            name="uq_agent_turn_org_conversation_version",
        ),
    )
    op.create_index(
        "ix_agent_turn_org_conversation_version",
        "recruiting_agent_conversation_turns",
        ["organization_id", "conversation_id", "context_version"],
    )
    op.create_index(
        "ix_recruiting_agent_conversation_turns_organization_id",
        "recruiting_agent_conversation_turns",
        ["organization_id"],
    )
    op.create_index(
        "ix_recruiting_agent_conversation_turns_conversation_id",
        "recruiting_agent_conversation_turns",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_table("recruiting_agent_conversation_turns")
