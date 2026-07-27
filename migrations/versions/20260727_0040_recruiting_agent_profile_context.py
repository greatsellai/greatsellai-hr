"""Persist the active confirmation-first profile for a private Agent chat.

Revision ID: 20260727_0040
Revises: 20260727_0039
Create Date: 2026-07-27 21:10:00

Only opaque profile and revision identifiers are added.  The conversation
table never stores recruiter prompts, model output, candidate names, resume
text, or profile contents.  The service re-validates both pointers under the
current workspace before every use.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0040"
down_revision: Union[str, Sequence[str], None] = "20260727_0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recruiting_agent_conversations",
        sa.Column("active_talent_profile_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "recruiting_agent_conversations",
        sa.Column(
            "active_talent_profile_revision_id",
            sa.String(length=36),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_agent_conv_active_talent_profile",
        "recruiting_agent_conversations",
        ["active_talent_profile_id"],
    )
    op.create_index(
        "ix_agent_conv_active_talent_profile_revision",
        "recruiting_agent_conversations",
        ["active_talent_profile_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conv_active_talent_profile_revision",
        table_name="recruiting_agent_conversations",
    )
    op.drop_index(
        "ix_agent_conv_active_talent_profile",
        table_name="recruiting_agent_conversations",
    )
    op.drop_column(
        "recruiting_agent_conversations",
        "active_talent_profile_revision_id",
    )
    op.drop_column(
        "recruiting_agent_conversations",
        "active_talent_profile_id",
    )
