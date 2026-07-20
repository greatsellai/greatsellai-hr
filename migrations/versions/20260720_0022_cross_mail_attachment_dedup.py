"""Deduplicate mailbox attachment bytes across forwarded messages.

Revision ID: 20260720_0022
Revises: 20260720_0021
Create Date: 2026-07-20 20:30:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0022"
down_revision: Union[str, Sequence[str], None] = "20260720_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("email_attachment_imports") as batch_op:
        batch_op.add_column(
            sa.Column("canonical_import_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_email_attachment_imports_canonical_import",
            "email_attachment_imports",
            ["canonical_import_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_email_attachment_imports_canonical_import_id",
            ["canonical_import_id"],
        )

    op.create_table(
        "mailbox_attachment_content_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("attachment_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("processing_import_id", sa.String(length=36), nullable=True),
        sa.Column("canonical_import_id", sa.String(length=36), nullable=True),
        sa.Column("canonical_resume_id", sa.String(length=36), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["processing_import_id"],
            ["email_attachment_imports.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_import_id"],
            ["email_attachment_imports.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_resume_id"],
            ["resumes.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "attachment_sha256",
            name="uq_mailbox_attachment_content_identity_org_sha",
        ),
    )
    op.create_index(
        "ix_mailbox_attachment_content_identities_organization_id",
        "mailbox_attachment_content_identities",
        ["organization_id"],
    )
    op.create_index(
        "ix_mailbox_attachment_content_identities_status",
        "mailbox_attachment_content_identities",
        ["status"],
    )
    op.create_index(
        "ix_mailbox_attachment_content_identity_claim",
        "mailbox_attachment_content_identities",
        ["organization_id", "status", "claim_lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mailbox_attachment_content_identity_claim",
        table_name="mailbox_attachment_content_identities",
    )
    op.drop_index(
        "ix_mailbox_attachment_content_identities_status",
        table_name="mailbox_attachment_content_identities",
    )
    op.drop_index(
        "ix_mailbox_attachment_content_identities_organization_id",
        table_name="mailbox_attachment_content_identities",
    )
    op.drop_table("mailbox_attachment_content_identities")

    with op.batch_alter_table("email_attachment_imports") as batch_op:
        batch_op.drop_index("ix_email_attachment_imports_canonical_import_id")
        batch_op.drop_constraint(
            "fk_email_attachment_imports_canonical_import",
            type_="foreignkey",
        )
        batch_op.drop_column("canonical_import_id")
