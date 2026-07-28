"""Add durable automatic AI resume-summary jobs.

Revision ID: 20260728_0045
Revises: 20260728_0044
Create Date: 2026-07-28 12:00:00

Summary generation is deliberately separate from structured-facts extraction:
one immutable fact revision receives one retryable, workspace-owned task.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0045"
down_revision: Union[str, Sequence[str], None] = "20260728_0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resume_summary_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("fact_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("facts_version", sa.Integer(), nullable=False),
        sa.Column("ai_route_policy_version_id", sa.String(length=36), nullable=True),
        sa.Column("summary_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ai_route_policy_version_id"], ["ai_route_policy_versions.id"]),
        sa.ForeignKeyConstraint(["fact_snapshot_id"], ["resume_fact_snapshots.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
        sa.ForeignKeyConstraint(["summary_id"], ["resume_summaries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resume_id",
            "facts_version",
            name="uq_resume_summary_job_facts_version",
        ),
    )
    op.create_index(
        "ix_resume_summary_jobs_organization_id",
        "resume_summary_jobs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_jobs_resume_id",
        "resume_summary_jobs",
        ["resume_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_jobs_fact_snapshot_id",
        "resume_summary_jobs",
        ["fact_snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_jobs_ai_route_policy_version_id",
        "resume_summary_jobs",
        ["ai_route_policy_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_jobs_summary_id",
        "resume_summary_jobs",
        ["summary_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_jobs_status",
        "resume_summary_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_job_claim",
        "resume_summary_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_job_lease",
        "resume_summary_jobs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_resume_summary_job_organization_claim",
        "resume_summary_jobs",
        ["organization_id", "status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    for index_name in (
        "ix_resume_summary_job_organization_claim",
        "ix_resume_summary_job_lease",
        "ix_resume_summary_job_claim",
        "ix_resume_summary_jobs_status",
        "ix_resume_summary_jobs_summary_id",
        "ix_resume_summary_jobs_ai_route_policy_version_id",
        "ix_resume_summary_jobs_fact_snapshot_id",
        "ix_resume_summary_jobs_resume_id",
        "ix_resume_summary_jobs_organization_id",
    ):
        op.drop_index(index_name, table_name="resume_summary_jobs")
    op.drop_table("resume_summary_jobs")
