"""Rename the recruiting-agent route label from 招聘助手 to 招聘 Agent.

Revision ID: 20260806_0058
Revises: 20260805_0057
Create Date: 2026-08-06 12:00:00

The product now brands the recruiting assistant as 招聘 Agent.  Migration
20260722_0032 humanized legacy slug labels into 招聘助手对话, so environments
that already applied it keep that label in ``ai_route_policies``.  This
follow-up backfills only rows still carrying the old humanized default;
operator-created custom names are left untouched.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0058"
down_revision: Union[str, Sequence[str], None] = "20260805_0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_DISPLAY_NAME = "招聘助手对话"
_OLD_DESCRIPTION = "为招聘助手生成下一轮回复。"
_NEW_DISPLAY_NAME = "招聘 Agent 对话"
_NEW_DESCRIPTION = "为招聘 Agent 生成下一轮回复。"


def upgrade() -> None:
    routes = sa.table(
        "ai_route_policies",
        sa.column("feature", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
    )
    # Only the humanized default rows from 20260722_0032.  Custom names that
    # happen to mention 招聘助手 keep their wording.
    op.execute(
        routes.update()
        .where(
            routes.c.feature == "recruiting_agent_turn",
            routes.c.display_name == _OLD_DISPLAY_NAME,
        )
        .values(display_name=_NEW_DISPLAY_NAME, description=_NEW_DESCRIPTION)
    )


def downgrade() -> None:
    routes = sa.table(
        "ai_route_policies",
        sa.column("feature", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
    )
    op.execute(
        routes.update()
        .where(
            routes.c.feature == "recruiting_agent_turn",
            routes.c.display_name == _NEW_DISPLAY_NAME,
        )
        .values(display_name=_OLD_DISPLAY_NAME, description=_OLD_DESCRIPTION)
    )
