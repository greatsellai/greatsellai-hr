"""Add a platform-only contact phone to feedback submissions.

Revision ID: 20260729_0047
Revises: 20260728_0046
Create Date: 2026-07-29 10:00:00

Historical feedback predates contact collection, so the new column remains
nullable.  The application requires and validates a phone number for every
new submission without inventing a value for existing rows.
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
    with op.batch_alter_table("workspace_feedback_submissions") as batch_op:
        batch_op.add_column(sa.Column("contact_phone", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspace_feedback_submissions") as batch_op:
        batch_op.drop_column("contact_phone")
