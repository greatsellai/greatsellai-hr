"""Keep global and Agent-scoped talent-profile runs distinct.

Revision ID: 20260727_0042
Revises: 20260727_0041
Create Date: 2026-07-27 22:20:00

Only a scope kind, opaque SHA-256 membership digest, and count are persisted.
The original filter request, resume text, candidate names, and prompt are not
stored by this migration or the associated workflow.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0042"
down_revision: Union[str, Sequence[str], None] = "20260727_0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "talent_search_runs",
        sa.Column(
            "scope_kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'global'"),
        ),
    )
    op.add_column(
        "talent_search_runs",
        sa.Column("scope_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "talent_search_runs",
        sa.Column(
            "scope_candidate_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_talent_search_runs_organization_revision_scope",
        "talent_search_runs",
        ["organization_id", "revision_id", "scope_kind", "scope_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_talent_search_runs_organization_revision_scope",
        table_name="talent_search_runs",
    )
    op.drop_column("talent_search_runs", "scope_candidate_count")
    op.drop_column("talent_search_runs", "scope_fingerprint")
    op.drop_column("talent_search_runs", "scope_kind")
