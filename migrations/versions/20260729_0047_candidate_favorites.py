"""Add private recruiter candidate favorites.

Revision ID: 20260729_0047
Revises: 20260728_0046
Create Date: 2026-07-29 10:00:00

Favorites contain only workspace, user and candidate identifiers. They do not
copy any candidate facts, document content or AI conclusions.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0047"
down_revision: Union[str, Sequence[str], None] = "20260728_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_favorites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "candidate_id",
            name="uq_candidate_favorites_org_user_candidate",
        ),
    )
    op.create_index(
        "ix_candidate_favorites_candidate_id",
        "candidate_favorites",
        ["candidate_id"],
    )
    op.create_index(
        "ix_candidate_favorites_user_id",
        "candidate_favorites",
        ["user_id"],
    )
    op.create_index(
        "ix_candidate_favorites_organization_id",
        "candidate_favorites",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_favorites_org_user_created",
        "candidate_favorites",
        ["organization_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_favorites_org_user_created",
        table_name="candidate_favorites",
    )
    op.drop_index(
        "ix_candidate_favorites_organization_id",
        table_name="candidate_favorites",
    )
    op.drop_index(
        "ix_candidate_favorites_user_id",
        table_name="candidate_favorites",
    )
    op.drop_index(
        "ix_candidate_favorites_candidate_id",
        table_name="candidate_favorites",
    )
    op.drop_table("candidate_favorites")
