"""Queue mailbox synchronization and exact retries outside HTTP requests.

Revision ID: 20260720_0021
Revises: 20260720_0020
Create Date: 2026-07-20 19:15:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0021"
down_revision: Union[str, Sequence[str], None] = "20260720_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mailbox_background_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("email_attachment_import_id", sa.String(length=36), nullable=True),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mailbox_background_jobs_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_config_id"],
            ["mailbox_configs.id"],
            ondelete="CASCADE",
            name="fk_mailbox_background_jobs_mailbox_config_id",
        ),
        sa.ForeignKeyConstraint(
            ["email_attachment_import_id"],
            ["email_attachment_imports.id"],
            ondelete="CASCADE",
            name="fk_mailbox_background_jobs_attachment_import_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mailbox_background_jobs_organization_id",
        "mailbox_background_jobs",
        ["organization_id"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_mailbox_config_id",
        "mailbox_background_jobs",
        ["mailbox_config_id"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_email_attachment_import_id",
        "mailbox_background_jobs",
        ["email_attachment_import_id"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_job_kind",
        "mailbox_background_jobs",
        ["job_kind"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_status",
        "mailbox_background_jobs",
        ["status"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_claim",
        "mailbox_background_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_organization_claim",
        "mailbox_background_jobs",
        ["organization_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "ix_mailbox_background_jobs_organization_lease",
        "mailbox_background_jobs",
        ["organization_id", "status", "lease_expires_at"],
    )
    op.create_index(
        "uq_mailbox_background_jobs_active_sync",
        "mailbox_background_jobs",
        ["organization_id", "mailbox_config_id", "job_kind"],
        unique=True,
        sqlite_where=sa.text("job_kind = 'sync' AND status IN ('queued', 'running')"),
        postgresql_where=sa.text("job_kind = 'sync' AND status IN ('queued', 'running')"),
    )
    op.create_index(
        "uq_mailbox_background_jobs_active_attachment_retry",
        "mailbox_background_jobs",
        ["organization_id", "email_attachment_import_id"],
        unique=True,
        sqlite_where=sa.text(
            "job_kind = 'attachment_retry' "
            "AND status IN ('queued', 'running') "
            "AND email_attachment_import_id IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "job_kind = 'attachment_retry' "
            "AND status IN ('queued', 'running') "
            "AND email_attachment_import_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_mailbox_background_jobs_active_attachment_retry",
        table_name="mailbox_background_jobs",
    )
    op.drop_index(
        "uq_mailbox_background_jobs_active_sync",
        table_name="mailbox_background_jobs",
    )
    op.drop_index(
        "ix_mailbox_background_jobs_organization_lease",
        table_name="mailbox_background_jobs",
    )
    op.drop_index(
        "ix_mailbox_background_jobs_organization_claim",
        table_name="mailbox_background_jobs",
    )
    op.drop_index("ix_mailbox_background_jobs_claim", table_name="mailbox_background_jobs")
    op.drop_index("ix_mailbox_background_jobs_status", table_name="mailbox_background_jobs")
    op.drop_index("ix_mailbox_background_jobs_job_kind", table_name="mailbox_background_jobs")
    op.drop_index(
        "ix_mailbox_background_jobs_email_attachment_import_id",
        table_name="mailbox_background_jobs",
    )
    op.drop_index(
        "ix_mailbox_background_jobs_mailbox_config_id",
        table_name="mailbox_background_jobs",
    )
    op.drop_index(
        "ix_mailbox_background_jobs_organization_id",
        table_name="mailbox_background_jobs",
    )
    op.drop_table("mailbox_background_jobs")
