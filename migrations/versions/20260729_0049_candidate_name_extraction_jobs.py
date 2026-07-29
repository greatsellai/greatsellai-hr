"""Add durable source-grounded candidate-name extraction jobs.

Revision ID: 20260729_0049
Revises: 20260729_0048
Create Date: 2026-07-29 09:00:00

Candidate identity completion is intentionally isolated from structured-fact
extraction: a name-only provider failure must never alter an otherwise usable
resume. Existing active, ready unnamed resumes are queued by ID only, without
copying raw source text or candidate data into the migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0049"
down_revision: Union[str, Sequence[str], None] = "20260729_0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_name_extraction_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("ai_route_policy_version_id", sa.String(length=36), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["ai_route_policy_version_id"], ["ai_route_policy_versions.id"]
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resume_id",
            name="uq_candidate_name_extraction_job_resume",
        ),
    )
    op.create_index(
        "ix_candidate_name_extraction_jobs_organization_id",
        "candidate_name_extraction_jobs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_jobs_resume_id",
        "candidate_name_extraction_jobs",
        ["resume_id"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_jobs_ai_route_policy_version_id",
        "candidate_name_extraction_jobs",
        ["ai_route_policy_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_jobs_status",
        "candidate_name_extraction_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_job_claim",
        "candidate_name_extraction_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_job_lease",
        "candidate_name_extraction_jobs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_candidate_name_extraction_job_organization_claim",
        "candidate_name_extraction_jobs",
        ["organization_id", "status", "next_attempt_at"],
        unique=False,
    )
    _queue_existing_active_unnamed_resumes()


def _queue_existing_active_unnamed_resumes() -> None:
    """Queue only metadata for active, source-backed historical resumes.

    The query deliberately requires both privacy roots to be live. It is kept
    in Python rather than using a database UUID function so SQLite and
    PostgreSQL receive the same deterministic migration behaviour.
    """

    bind = op.get_bind()
    metadata = sa.MetaData()
    resumes = sa.Table(
        "resumes",
        metadata,
        sa.Column("id", sa.String()),
        sa.Column("organization_id", sa.String()),
        sa.Column("candidate_id", sa.String()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("extraction_status", sa.String()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    candidates = sa.Table(
        "candidates",
        metadata,
        sa.Column("id", sa.String()),
        sa.Column("organization_id", sa.String()),
        sa.Column("display_name", sa.String()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    source_blocks = sa.Table(
        "resume_source_blocks",
        metadata,
        sa.Column("id", sa.String()),
        sa.Column("resume_id", sa.String()),
    )
    jobs = sa.table(
        "candidate_name_extraction_jobs",
        sa.column("id", sa.String()),
        sa.column("organization_id", sa.String()),
        sa.column("resume_id", sa.String()),
        sa.column("ai_route_policy_version_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.column("lease_owner", sa.String()),
        sa.column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.column("last_error", sa.Text()),
        sa.column("requested_at", sa.DateTime(timezone=True)),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(
            resumes.c.id.label("resume_id"),
            resumes.c.organization_id.label("organization_id"),
        )
        .select_from(
            resumes.join(
                candidates,
                sa.and_(
                    candidates.c.id == resumes.c.candidate_id,
                    candidates.c.organization_id == resumes.c.organization_id,
                ),
            )
        )
        .where(
            resumes.c.is_active.is_(True),
            resumes.c.extraction_status == "ready",
            resumes.c.deleted_at.is_(None),
            candidates.c.deleted_at.is_(None),
            sa.or_(
                candidates.c.display_name.is_(None),
                sa.func.trim(candidates.c.display_name) == "",
            ),
            sa.exists(
                sa.select(source_blocks.c.id).where(
                    source_blocks.c.resume_id == resumes.c.id
                )
            ),
        )
        .order_by(resumes.c.created_at.asc(), resumes.c.id.asc())
    ).mappings().all()
    if not rows:
        return

    now = datetime.now(timezone.utc)
    op.bulk_insert(
        jobs,
        [
            {
                "id": str(uuid4()),
                "organization_id": row["organization_id"],
                "resume_id": row["resume_id"],
                "ai_route_policy_version_id": None,
                "status": "queued",
                "attempt_count": 0,
                "max_attempts": 3,
                "next_attempt_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": None,
                "requested_at": now,
                "started_at": None,
                "completed_at": None,
                "updated_at": now,
            }
            for row in rows
        ],
    )


def downgrade() -> None:
    for index_name in (
        "ix_candidate_name_extraction_job_organization_claim",
        "ix_candidate_name_extraction_job_lease",
        "ix_candidate_name_extraction_job_claim",
        "ix_candidate_name_extraction_jobs_status",
        "ix_candidate_name_extraction_jobs_ai_route_policy_version_id",
        "ix_candidate_name_extraction_jobs_resume_id",
        "ix_candidate_name_extraction_jobs_organization_id",
    ):
        op.drop_index(index_name, table_name="candidate_name_extraction_jobs")
    op.drop_table("candidate_name_extraction_jobs")
