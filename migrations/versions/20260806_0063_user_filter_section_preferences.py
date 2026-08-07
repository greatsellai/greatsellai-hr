"""Per-user initial-filter panel section preference.

Revision ID: 20260806_0063
Revises: 20260806_0062
Create Date: 2026-08-07

Adds a filter_section_keys JSON array to the existing per-user filter
preference row so the same (user, organization) scope that already holds the
result-table column selection can also hold which "初筛条件板块" sections the
filter panel keeps visible. Existing rows backfill to the full section list,
which is the "show every section" product default; a stored empty array only
arises from an explicit save and means "hide the whole panel".
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260806_0063"
down_revision: Union[str, Sequence[str], None] = "20260806_0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JSON arrays are stored as TEXT on SQLite; batch mode rebuilds the table
    # there (and issues plain DDL on PostgreSQL). server_default backfills the
    # new NOT NULL column for rows written before the column existed. The
    # default is the full section list (product default = every section shown);
    # an empty list is reserved for an explicit "hide the whole panel" save.
    default_sections = (
        '["condition_mode","institution","basic_profile","academic",'
        '"graduation","experience","source_channel","keywords"]'
    )
    with op.batch_alter_table("user_filter_display_preferences") as batch_op:
        batch_op.add_column(
            sa.Column(
                "filter_section_keys",
                sa.JSON(),
                nullable=False,
                server_default=sa.text(f"'{default_sections}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_filter_display_preferences") as batch_op:
        batch_op.drop_column("filter_section_keys")
