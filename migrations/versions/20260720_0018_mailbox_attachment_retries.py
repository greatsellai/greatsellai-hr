"""Add exact retry state and attempt audit for mailbox attachments.

Revision ID: 20260720_0018
Revises: 20260720_0017
Create Date: 2026-07-20 13:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0018"
down_revision: Union[str, Sequence[str], None] = "20260720_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("email_attachment_imports") as batch_op:
        batch_op.add_column(
            sa.Column("source_uidvalidity", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("source_fingerprint", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(
            sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("retry_lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("retry_claim_token", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )

    # Existing rows were created before the source identity was recorded. They
    # stay readable but intentionally cannot be retried against a later
    # mailbox binding. Their initial attempt remains visible via these values.
    op.execute(
        "UPDATE email_attachment_imports "
        "SET last_attempted_at = created_at, updated_at = created_at "
        "WHERE last_attempted_at IS NULL OR updated_at IS NULL"
    )

    op.create_index(
        "ix_email_attachment_imports_retry_lease",
        "email_attachment_imports",
        ["organization_id", "status", "retry_lease_expires_at"],
    )
    op.create_table(
        "email_attachment_import_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("email_attachment_import_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resume_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["email_attachment_import_id"],
            ["email_attachment_imports.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "email_attachment_import_id",
            "attempt_number",
            name="uq_email_attachment_import_attempt_number",
        ),
    )
    op.create_index(
        "ix_email_attachment_import_attempts_email_attachment_import_id",
        "email_attachment_import_attempts",
        ["email_attachment_import_id"],
    )
    op.create_index(
        "ix_email_attachment_import_attempts_status",
        "email_attachment_import_attempts",
        ["status"],
    )
    op.create_index(
        "ix_email_attachment_import_attempts_organization_id",
        "email_attachment_import_attempts",
        ["organization_id"],
    )
    op.create_index(
        "ix_email_attachment_import_attempts_organization_created",
        "email_attachment_import_attempts",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_attachment_import_attempts_organization_created",
        table_name="email_attachment_import_attempts",
    )
    op.drop_index(
        "ix_email_attachment_import_attempts_organization_id",
        table_name="email_attachment_import_attempts",
    )
    op.drop_index(
        "ix_email_attachment_import_attempts_status",
        table_name="email_attachment_import_attempts",
    )
    op.drop_index(
        "ix_email_attachment_import_attempts_email_attachment_import_id",
        table_name="email_attachment_import_attempts",
    )
    op.drop_table("email_attachment_import_attempts")
    op.drop_index(
        "ix_email_attachment_imports_retry_lease",
        table_name="email_attachment_imports",
    )
    with op.batch_alter_table("email_attachment_imports") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("retry_claim_token")
        batch_op.drop_column("retry_lease_expires_at")
        batch_op.drop_column("last_attempted_at")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("source_fingerprint")
        batch_op.drop_column("source_uidvalidity")
