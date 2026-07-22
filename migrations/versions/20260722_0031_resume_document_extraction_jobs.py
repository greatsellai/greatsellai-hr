"""Queue untrusted original-file parsing outside API requests.

Revision ID: 20260722_0031
Revises: 20260722_0030
Create Date: 2026-07-22 10:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0031"
down_revision: Union[str, Sequence[str], None] = "20260722_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "resume_document_extraction_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resume_id", name="uq_resume_document_extraction_job_resume"),
    )
    op.create_index(
        "ix_resume_document_extraction_jobs_organization_id",
        "resume_document_extraction_jobs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_document_extraction_jobs_resume_id",
        "resume_document_extraction_jobs",
        ["resume_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_document_extraction_jobs_status",
        "resume_document_extraction_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_resume_document_extraction_job_claim",
        "resume_document_extraction_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_resume_document_extraction_job_lease",
        "resume_document_extraction_jobs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_resume_document_extraction_job_organization_claim",
        "resume_document_extraction_jobs",
        ["organization_id", "status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resume_document_extraction_job_organization_claim",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_index(
        "ix_resume_document_extraction_job_lease",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_index(
        "ix_resume_document_extraction_job_claim",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_index(
        "ix_resume_document_extraction_jobs_status",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_index(
        "ix_resume_document_extraction_jobs_resume_id",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_index(
        "ix_resume_document_extraction_jobs_organization_id",
        table_name="resume_document_extraction_jobs",
    )
    op.drop_table("resume_document_extraction_jobs")
