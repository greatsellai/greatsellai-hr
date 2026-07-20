"""Add isolated mailbox content retention and cleanup audit records.

Revision ID: 20260720_0019
Revises: 20260720_0018
Create Date: 2026-07-20 18:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0019"
down_revision: Union[str, Sequence[str], None] = "20260720_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "retention_policy",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'standard'"),
            )
        )
        batch_op.add_column(
            sa.Column("last_retention_cleanup_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        "mailbox_content_replicas",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("email_attachment_import_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_reference", sa.String(length=128), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_error", sa.String(length=128), nullable=True),
        sa.Column("cleanup_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_claim_token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["mailbox_config_id"], ["mailbox_configs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["email_attachment_import_id"],
            ["email_attachment_imports.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mailbox_config_id",
            "kind",
            "source_reference",
            name="uq_mailbox_content_replicas_source",
        ),
        sa.UniqueConstraint(
            "storage_key",
            name="uq_mailbox_content_replicas_storage_key",
        ),
    )
    op.create_index(
        "ix_mailbox_content_replicas_organization_id",
        "mailbox_content_replicas",
        ["organization_id"],
    )
    op.create_index(
        "ix_mailbox_content_replicas_mailbox_config_id",
        "mailbox_content_replicas",
        ["mailbox_config_id"],
    )
    op.create_index(
        "ix_mailbox_content_replicas_email_attachment_import_id",
        "mailbox_content_replicas",
        ["email_attachment_import_id"],
    )
    op.create_index(
        "ix_mailbox_content_replicas_kind",
        "mailbox_content_replicas",
        ["kind"],
    )
    op.create_index(
        "ix_mailbox_content_replicas_expires_at",
        "mailbox_content_replicas",
        ["expires_at"],
    )
    op.create_index(
        "ix_mailbox_content_replicas_cleanup",
        "mailbox_content_replicas",
        ["organization_id", "cleaned_at", "expires_at"],
    )

    op.create_table(
        "mailbox_retention_cleanup_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("retention_policy", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("reclaimed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["mailbox_config_id"], ["mailbox_configs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mailbox_retention_cleanup_runs_organization_id",
        "mailbox_retention_cleanup_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_mailbox_retention_cleanup_runs_mailbox_config_id",
        "mailbox_retention_cleanup_runs",
        ["mailbox_config_id"],
    )
    op.create_index(
        "ix_mailbox_retention_cleanup_runs_organization_started",
        "mailbox_retention_cleanup_runs",
        ["organization_id", "started_at"],
    )
    op.create_index(
        "ix_mailbox_retention_cleanup_runs_config_started",
        "mailbox_retention_cleanup_runs",
        ["mailbox_config_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mailbox_retention_cleanup_runs_config_started",
        table_name="mailbox_retention_cleanup_runs",
    )
    op.drop_index(
        "ix_mailbox_retention_cleanup_runs_organization_started",
        table_name="mailbox_retention_cleanup_runs",
    )
    op.drop_index(
        "ix_mailbox_retention_cleanup_runs_mailbox_config_id",
        table_name="mailbox_retention_cleanup_runs",
    )
    op.drop_index(
        "ix_mailbox_retention_cleanup_runs_organization_id",
        table_name="mailbox_retention_cleanup_runs",
    )
    op.drop_table("mailbox_retention_cleanup_runs")

    op.drop_index(
        "ix_mailbox_content_replicas_cleanup",
        table_name="mailbox_content_replicas",
    )
    op.drop_index(
        "ix_mailbox_content_replicas_expires_at",
        table_name="mailbox_content_replicas",
    )
    op.drop_index(
        "ix_mailbox_content_replicas_kind",
        table_name="mailbox_content_replicas",
    )
    op.drop_index(
        "ix_mailbox_content_replicas_email_attachment_import_id",
        table_name="mailbox_content_replicas",
    )
    op.drop_index(
        "ix_mailbox_content_replicas_mailbox_config_id",
        table_name="mailbox_content_replicas",
    )
    op.drop_index(
        "ix_mailbox_content_replicas_organization_id",
        table_name="mailbox_content_replicas",
    )
    op.drop_table("mailbox_content_replicas")

    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.drop_column("last_retention_cleanup_at")
        batch_op.drop_column("retention_policy")
