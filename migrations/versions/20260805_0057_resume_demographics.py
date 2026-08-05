"""Add normalized gender and birth date to resumes for demographic screening.

Revision ID: 20260805_0057
Revises: 20260803_0056
Create Date: 2026-08-05 16:00:00

The recruiting workbench screens candidates by gender and age. ``gender`` is a
normalized ``male``/``female`` value (or null when a resume never states it)
and ``birth_date`` is the normalized calendar date used to compute age. Both
are denormalized onto ``resumes`` so the primary screening path can filter on
indexed columns, matching the existing ``is_985_211``/``highest_degree``
pattern.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260805_0057"
down_revision: Union[str, Sequence[str], None] = "20260803_0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column("gender", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "resumes",
        sa.Column("birth_date", sa.Date(), nullable=True),
    )
    op.create_index("ix_resumes_gender", "resumes", ["gender"], unique=False)
    op.create_index("ix_resumes_birth_date", "resumes", ["birth_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_resumes_birth_date", table_name="resumes")
    op.drop_index("ix_resumes_gender", table_name="resumes")
    op.drop_column("resumes", "birth_date")
    op.drop_column("resumes", "gender")
