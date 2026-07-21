"""Add candidate-data lifecycle, retention, export, and audit foundations.

Revision ID: 20260721_0026
Revises: 20260721_0025
Create Date: 2026-07-21 15:30:00
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0026"
down_revision: Union[str, Sequence[str], None] = "20260721_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_index(name: str, table: str, columns: list[str]) -> None:
    op.create_index(name, table, columns)


def upgrade() -> None:
    op.create_table(
        "candidate_data_deletion_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("private_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("recovery_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_deletion_batches_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_deletion_batches_requested_by_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["restored_by_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_deletion_batches_restored_by_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index(
        "ix_candidate_data_deletion_batches_organization_id",
        "candidate_data_deletion_batches",
        ["organization_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batches_requested_by_user_id",
        "candidate_data_deletion_batches",
        ["requested_by_user_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batches_organization_created",
        "candidate_data_deletion_batches",
        ["organization_id", "created_at"],
    )
    _create_index(
        "ix_candidate_data_deletion_batches_organization_recovery",
        "candidate_data_deletion_batches",
        ["organization_id", "status", "recovery_deadline_at"],
    )

    op.create_table(
        "candidate_data_deletion_batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("deletion_batch_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("was_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_deletion_batch_items_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["deletion_batch_id"],
            ["candidate_data_deletion_batches.id"],
            ondelete="CASCADE",
            name="fk_candidate_data_deletion_batch_items_batch_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deletion_batch_id",
            "resume_id",
            name="uq_candidate_data_deletion_batch_item_resume",
        ),
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_organization_id",
        "candidate_data_deletion_batch_items",
        ["organization_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_deletion_batch_id",
        "candidate_data_deletion_batch_items",
        ["deletion_batch_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_candidate_id",
        "candidate_data_deletion_batch_items",
        ["candidate_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_resume_id",
        "candidate_data_deletion_batch_items",
        ["resume_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_organization_batch",
        "candidate_data_deletion_batch_items",
        ["organization_id", "deletion_batch_id"],
    )
    _create_index(
        "ix_candidate_data_deletion_batch_items_organization_candidate",
        "candidate_data_deletion_batch_items",
        ["organization_id", "candidate_id"],
    )

    op.create_table(
        "candidate_data_purge_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("deletion_batch_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_purge_jobs_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["deletion_batch_id"],
            ["candidate_data_deletion_batches.id"],
            ondelete="CASCADE",
            name="fk_candidate_data_purge_jobs_batch_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deletion_batch_id",
            name="uq_candidate_data_purge_job_batch",
        ),
    )
    _create_index("ix_candidate_data_purge_jobs_organization_id", "candidate_data_purge_jobs", ["organization_id"])
    _create_index("ix_candidate_data_purge_jobs_deletion_batch_id", "candidate_data_purge_jobs", ["deletion_batch_id"])
    _create_index(
        "ix_candidate_data_purge_jobs_status",
        "candidate_data_purge_jobs",
        ["status"],
    )
    _create_index(
        "ix_candidate_data_purge_jobs_organization_claim",
        "candidate_data_purge_jobs",
        ["organization_id", "status", "next_attempt_at"],
    )
    _create_index(
        "ix_candidate_data_purge_jobs_organization_lease",
        "candidate_data_purge_jobs",
        ["organization_id", "status", "lease_expires_at"],
    )

    op.create_table(
        "candidate_data_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=True),
        sa.Column("resume_id", sa.String(length=36), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_audit_events_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_audit_events_actor_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index("ix_candidate_data_audit_events_organization_id", "candidate_data_audit_events", ["organization_id"])
    _create_index("ix_candidate_data_audit_events_actor_user_id", "candidate_data_audit_events", ["actor_user_id"])
    _create_index("ix_candidate_data_audit_events_action", "candidate_data_audit_events", ["action"])
    _create_index("ix_candidate_data_audit_events_target_type", "candidate_data_audit_events", ["target_type"])
    _create_index("ix_candidate_data_audit_events_target_id", "candidate_data_audit_events", ["target_id"])
    _create_index("ix_candidate_data_audit_events_candidate_id", "candidate_data_audit_events", ["candidate_id"])
    _create_index("ix_candidate_data_audit_events_resume_id", "candidate_data_audit_events", ["resume_id"])
    _create_index("ix_candidate_data_audit_events_request_id", "candidate_data_audit_events", ["request_id"])
    _create_index(
        "ix_candidate_data_audit_events_organization_created",
        "candidate_data_audit_events",
        ["organization_id", "created_at"],
    )
    _create_index(
        "ix_candidate_data_audit_events_organization_action_created",
        "candidate_data_audit_events",
        ["organization_id", "action", "created_at"],
    )
    _create_index(
        "ix_candidate_data_audit_events_target_created",
        "candidate_data_audit_events",
        ["target_type", "target_id", "created_at"],
    )

    op.create_table(
        "candidate_data_file_access_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("session_nonce_digest", sa.String(length=64), nullable=False),
        sa.Column("resource_lifecycle_version", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_file_access_grants_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_file_access_grants_actor_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_candidate_data_file_access_grant_token"),
    )
    _create_index("ix_candidate_data_file_access_grants_organization_id", "candidate_data_file_access_grants", ["organization_id"])
    _create_index("ix_candidate_data_file_access_grants_actor_user_id", "candidate_data_file_access_grants", ["actor_user_id"])
    _create_index("ix_candidate_data_file_access_grants_resource_type", "candidate_data_file_access_grants", ["resource_type"])
    _create_index("ix_candidate_data_file_access_grants_resource_id", "candidate_data_file_access_grants", ["resource_id"])
    _create_index("ix_candidate_data_file_access_grants_expires_at", "candidate_data_file_access_grants", ["expires_at"])
    _create_index(
        "ix_candidate_data_file_access_grants_organization_resource",
        "candidate_data_file_access_grants",
        ["organization_id", "resource_type", "resource_id"],
    )
    _create_index(
        "ix_candidate_data_file_access_grants_expiry",
        "candidate_data_file_access_grants",
        ["expires_at"],
    )

    op.create_table(
        "candidate_data_retention_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_retention_policies_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_retention_policies_updated_by_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            name="uq_candidate_data_retention_policy_organization",
        ),
    )
    _create_index("ix_candidate_data_retention_policies_organization_id", "candidate_data_retention_policies", ["organization_id"])
    _create_index("ix_candidate_data_retention_policies_updated_by_user_id", "candidate_data_retention_policies", ["updated_by_user_id"])

    op.create_table(
        "candidate_data_retention_cleanup_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("queued_count", sa.Integer(), nullable=False),
        sa.Column("skipped_hold_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_retention_cleanup_runs_organization_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index("ix_candidate_data_retention_cleanup_runs_organization_id", "candidate_data_retention_cleanup_runs", ["organization_id"])
    _create_index(
        "ix_candidate_data_retention_cleanup_runs_organization_started",
        "candidate_data_retention_cleanup_runs",
        ["organization_id", "started_at"],
    )

    op.create_table(
        "candidate_data_exports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("include_originals", sa.Boolean(), nullable=False),
        sa.Column("output_storage_key", sa.String(length=255), nullable=True),
        sa.Column("output_content_type", sa.String(length=128), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_candidate_data_exports_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user_accounts.id"],
            name="fk_candidate_data_exports_requested_by_user_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_index("ix_candidate_data_exports_organization_id", "candidate_data_exports", ["organization_id"])
    _create_index("ix_candidate_data_exports_requested_by_user_id", "candidate_data_exports", ["requested_by_user_id"])
    _create_index("ix_candidate_data_exports_status", "candidate_data_exports", ["status"])
    _create_index("ix_candidate_data_exports_expires_at", "candidate_data_exports", ["expires_at"])
    _create_index(
        "ix_candidate_data_exports_organization_claim",
        "candidate_data_exports",
        ["organization_id", "status", "next_attempt_at"],
    )
    _create_index(
        "ix_candidate_data_exports_organization_created",
        "candidate_data_exports",
        ["organization_id", "created_at"],
    )
    _create_index("ix_candidate_data_exports_expiry", "candidate_data_exports", ["expires_at"])

    op.create_table(
        "mailbox_deleted_attachment_tombstones",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("key_version", sa.String(length=32), nullable=False),
        sa.Column("deletion_batch_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_mailbox_deleted_attachment_tombstones_organization_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "digest",
            "key_version",
            name="uq_mailbox_deleted_attachment_tombstone_digest",
        ),
    )
    _create_index("ix_mailbox_deleted_attachment_tombstones_organization_id", "mailbox_deleted_attachment_tombstones", ["organization_id"])
    _create_index("ix_mailbox_deleted_attachment_tombstones_digest", "mailbox_deleted_attachment_tombstones", ["digest"])
    _create_index(
        "ix_mailbox_deleted_attachment_tombstones_organization_expiry",
        "mailbox_deleted_attachment_tombstones",
        ["organization_id", "expires_at"],
    )

    # The lifecycle root fields are added only after their batch table exists
    # so SQLite's batch table rebuild can keep real foreign-key enforcement.
    with op.batch_alter_table("candidates") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("deleted_by_user_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("deletion_batch_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("retention_hold", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("lifecycle_version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
        batch_op.create_foreign_key(
            "fk_candidates_deleted_by_user_id",
            "user_accounts",
            ["deleted_by_user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_candidates_deletion_batch_id",
            "candidate_data_deletion_batches",
            ["deletion_batch_id"],
            ["id"],
        )
    _create_index("ix_candidates_deleted_at", "candidates", ["deleted_at"])
    _create_index("ix_candidates_deleted_by_user_id", "candidates", ["deleted_by_user_id"])
    _create_index("ix_candidates_deletion_batch_id", "candidates", ["deletion_batch_id"])
    _create_index("ix_candidates_purge_after_at", "candidates", ["purge_after_at"])
    _create_index("ix_candidates_retention_hold", "candidates", ["retention_hold"])
    _create_index(
        "ix_candidates_organization_lifecycle",
        "candidates",
        ["organization_id", "deleted_at", "purge_after_at"],
    )

    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_index("uq_active_resume_per_candidate")
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("deleted_by_user_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("deletion_batch_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("retention_hold", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("lifecycle_version", sa.Integer(), nullable=False, server_default=sa.text("1"))
        )
        batch_op.create_foreign_key(
            "fk_resumes_deleted_by_user_id",
            "user_accounts",
            ["deleted_by_user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_resumes_deletion_batch_id",
            "candidate_data_deletion_batches",
            ["deletion_batch_id"],
            ["id"],
        )
        batch_op.create_index(
            "uq_active_resume_per_candidate",
            ["candidate_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1 AND deleted_at IS NULL"),
            postgresql_where=sa.text("is_active = true AND deleted_at IS NULL"),
        )
    _create_index("ix_resumes_deleted_at", "resumes", ["deleted_at"])
    _create_index("ix_resumes_deleted_by_user_id", "resumes", ["deleted_by_user_id"])
    _create_index("ix_resumes_deletion_batch_id", "resumes", ["deletion_batch_id"])
    _create_index("ix_resumes_purge_after_at", "resumes", ["purge_after_at"])
    _create_index("ix_resumes_retention_hold", "resumes", ["retention_hold"])
    _create_index(
        "ix_resumes_organization_lifecycle",
        "resumes",
        ["organization_id", "deleted_at", "purge_after_at"],
    )

    # New and existing workspaces start in explicit manual mode.  No existing
    # candidate receives a retrospective expiry as part of this migration.
    bind = op.get_bind()
    organizations = bind.execute(sa.text("SELECT id FROM organizations")).scalars().all()
    policies = sa.table(
        "candidate_data_retention_policies",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("mode", sa.String()),
        sa.column("retention_days", sa.Integer()),
        sa.column("version", sa.Integer()),
        sa.column("updated_by_user_id", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    for organization_id in organizations:
        bind.execute(
            policies.insert().values(
                id=str(uuid4()),
                organization_id=organization_id,
                mode="manual",
                retention_days=None,
                version=1,
                updated_by_user_id=None,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    for name in (
        "ix_resumes_organization_lifecycle",
        "ix_resumes_retention_hold",
        "ix_resumes_purge_after_at",
        "ix_resumes_deletion_batch_id",
        "ix_resumes_deleted_by_user_id",
        "ix_resumes_deleted_at",
    ):
        op.drop_index(name, table_name="resumes")
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_index("uq_active_resume_per_candidate")
        batch_op.drop_constraint("fk_resumes_deletion_batch_id", type_="foreignkey")
        batch_op.drop_constraint("fk_resumes_deleted_by_user_id", type_="foreignkey")
        batch_op.drop_column("retention_hold")
        batch_op.drop_column("lifecycle_version")
        batch_op.drop_column("purge_after_at")
        batch_op.drop_column("deletion_batch_id")
        batch_op.drop_column("deleted_by_user_id")
        batch_op.drop_column("deleted_at")
        batch_op.create_index(
            "uq_active_resume_per_candidate",
            ["candidate_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
            postgresql_where=sa.text("is_active = true"),
        )

    for name in (
        "ix_candidates_organization_lifecycle",
        "ix_candidates_retention_hold",
        "ix_candidates_purge_after_at",
        "ix_candidates_deletion_batch_id",
        "ix_candidates_deleted_by_user_id",
        "ix_candidates_deleted_at",
    ):
        op.drop_index(name, table_name="candidates")
    with op.batch_alter_table("candidates") as batch_op:
        batch_op.drop_constraint("fk_candidates_deletion_batch_id", type_="foreignkey")
        batch_op.drop_constraint("fk_candidates_deleted_by_user_id", type_="foreignkey")
        batch_op.drop_column("retention_hold")
        batch_op.drop_column("lifecycle_version")
        batch_op.drop_column("purge_after_at")
        batch_op.drop_column("deletion_batch_id")
        batch_op.drop_column("deleted_by_user_id")
        batch_op.drop_column("deleted_at")

    for table, indexes in (
        (
            "mailbox_deleted_attachment_tombstones",
            (
                "ix_mailbox_deleted_attachment_tombstones_organization_expiry",
                "ix_mailbox_deleted_attachment_tombstones_digest",
                "ix_mailbox_deleted_attachment_tombstones_organization_id",
            ),
        ),
        (
            "candidate_data_exports",
            (
                "ix_candidate_data_exports_expiry",
                "ix_candidate_data_exports_organization_created",
                "ix_candidate_data_exports_organization_claim",
                "ix_candidate_data_exports_expires_at",
                "ix_candidate_data_exports_status",
                "ix_candidate_data_exports_requested_by_user_id",
                "ix_candidate_data_exports_organization_id",
            ),
        ),
        (
            "candidate_data_retention_cleanup_runs",
            (
                "ix_candidate_data_retention_cleanup_runs_organization_started",
                "ix_candidate_data_retention_cleanup_runs_organization_id",
            ),
        ),
        (
            "candidate_data_retention_policies",
            (
                "ix_candidate_data_retention_policies_updated_by_user_id",
                "ix_candidate_data_retention_policies_organization_id",
            ),
        ),
        (
            "candidate_data_file_access_grants",
            (
                "ix_candidate_data_file_access_grants_expiry",
                "ix_candidate_data_file_access_grants_organization_resource",
                "ix_candidate_data_file_access_grants_expires_at",
                "ix_candidate_data_file_access_grants_resource_id",
                "ix_candidate_data_file_access_grants_resource_type",
                "ix_candidate_data_file_access_grants_actor_user_id",
                "ix_candidate_data_file_access_grants_organization_id",
            ),
        ),
        (
            "candidate_data_audit_events",
            (
                "ix_candidate_data_audit_events_target_created",
                "ix_candidate_data_audit_events_organization_action_created",
                "ix_candidate_data_audit_events_organization_created",
                "ix_candidate_data_audit_events_request_id",
                "ix_candidate_data_audit_events_resume_id",
                "ix_candidate_data_audit_events_candidate_id",
                "ix_candidate_data_audit_events_target_id",
                "ix_candidate_data_audit_events_target_type",
                "ix_candidate_data_audit_events_action",
                "ix_candidate_data_audit_events_actor_user_id",
                "ix_candidate_data_audit_events_organization_id",
            ),
        ),
        (
            "candidate_data_purge_jobs",
            (
                "ix_candidate_data_purge_jobs_organization_lease",
                "ix_candidate_data_purge_jobs_organization_claim",
                "ix_candidate_data_purge_jobs_status",
                "ix_candidate_data_purge_jobs_deletion_batch_id",
                "ix_candidate_data_purge_jobs_organization_id",
            ),
        ),
        (
            "candidate_data_deletion_batch_items",
            (
                "ix_candidate_data_deletion_batch_items_organization_candidate",
                "ix_candidate_data_deletion_batch_items_organization_batch",
                "ix_candidate_data_deletion_batch_items_resume_id",
                "ix_candidate_data_deletion_batch_items_candidate_id",
                "ix_candidate_data_deletion_batch_items_deletion_batch_id",
                "ix_candidate_data_deletion_batch_items_organization_id",
            ),
        ),
        (
            "candidate_data_deletion_batches",
            (
                "ix_candidate_data_deletion_batches_organization_recovery",
                "ix_candidate_data_deletion_batches_organization_created",
                "ix_candidate_data_deletion_batches_requested_by_user_id",
                "ix_candidate_data_deletion_batches_organization_id",
            ),
        ),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
