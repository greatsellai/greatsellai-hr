"""Add workspace feedback submissions and delayed API-call rewards.

Revision ID: 20260728_0046
Revises: 20260728_0045
Create Date: 2026-07-28 00:15:00

Feedback text is deliberately stored only on its dedicated, workspace-scoped
record.  The reward queue contains no candidate data and is fenced by a
durable lease so a worker retry cannot grant the same +500 allowance twice.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0046"
down_revision: Union[str, Sequence[str], None] = "20260728_0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "feedback_reward_available_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    op.create_table(
        "workspace_feedback_submissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submitted_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("use_case", sa.Text(), nullable=False),
        sa.Column("intended_outcome", sa.Text(), nullable=False),
        sa.Column("friction", sa.Text(), nullable=False),
        sa.Column("desired_change", sa.Text(), nullable=False),
        sa.Column(
            "reward_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "reward_call_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("500"),
        ),
        sa.Column("reward_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "reward_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("reward_lease_owner", sa.String(length=160), nullable=True),
        sa.Column("reward_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reward_last_error", sa.String(length=128), nullable=True),
        sa.Column("reward_granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint(
            "reward_status IN ('queued', 'running', 'granted')",
            name="ck_workspace_feedback_reward_status",
        ),
        sa.CheckConstraint(
            "reward_call_count = 500",
            name="ck_workspace_feedback_reward_call_count",
        ),
        sa.CheckConstraint(
            "reward_attempt_count >= 0",
            name="ck_workspace_feedback_reward_attempt_count",
        ),
        sa.ForeignKeyConstraint(["submitted_by_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_workspace_feedback_id_organization",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key_hash",
            name="uq_workspace_feedback_org_idempotency",
        ),
    )
    op.create_index(
        "ix_workspace_feedback_submissions_submitted_by_user_id",
        "workspace_feedback_submissions",
        ["submitted_by_user_id"],
    )
    op.create_index(
        "ix_workspace_feedback_submissions_reward_status",
        "workspace_feedback_submissions",
        ["reward_status"],
    )
    op.create_index(
        "ix_workspace_feedback_submissions_reward_due_at",
        "workspace_feedback_submissions",
        ["reward_due_at"],
    )
    op.create_index(
        "ix_workspace_feedback_submissions_organization_id",
        "workspace_feedback_submissions",
        ["organization_id"],
    )
    op.create_index(
        "ix_workspace_feedback_reward_due",
        "workspace_feedback_submissions",
        ["reward_status", "reward_due_at", "created_at"],
    )
    op.create_index(
        "ix_workspace_feedback_org_created",
        "workspace_feedback_submissions",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "workspace_feedback_image_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feedback_submission_id", sa.String(length=36), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_workspace_feedback_image_order"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_workspace_feedback_image_size"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["feedback_submission_id", "organization_id"],
            [
                "workspace_feedback_submissions.id",
                "workspace_feedback_submissions.organization_id",
            ],
            name="fk_workspace_feedback_image_submission_org",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "feedback_submission_id",
            "sort_order",
            name="uq_workspace_feedback_image_order",
        ),
    )
    op.create_index(
        "ix_workspace_feedback_image_attachments_feedback_submission_id",
        "workspace_feedback_image_attachments",
        ["feedback_submission_id"],
    )
    op.create_index(
        "ix_workspace_feedback_image_attachments_organization_id",
        "workspace_feedback_image_attachments",
        ["organization_id"],
    )
    op.create_index(
        "ix_workspace_feedback_image_org_submission",
        "workspace_feedback_image_attachments",
        ["organization_id", "feedback_submission_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_feedback_image_org_submission",
        table_name="workspace_feedback_image_attachments",
    )
    op.drop_index(
        "ix_workspace_feedback_image_attachments_organization_id",
        table_name="workspace_feedback_image_attachments",
    )
    op.drop_index(
        "ix_workspace_feedback_image_attachments_feedback_submission_id",
        table_name="workspace_feedback_image_attachments",
    )
    op.drop_table("workspace_feedback_image_attachments")

    op.drop_index(
        "ix_workspace_feedback_org_created",
        table_name="workspace_feedback_submissions",
    )
    op.drop_index(
        "ix_workspace_feedback_reward_due",
        table_name="workspace_feedback_submissions",
    )
    op.drop_index(
        "ix_workspace_feedback_submissions_organization_id",
        table_name="workspace_feedback_submissions",
    )
    op.drop_index(
        "ix_workspace_feedback_submissions_reward_due_at",
        table_name="workspace_feedback_submissions",
    )
    op.drop_index(
        "ix_workspace_feedback_submissions_reward_status",
        table_name="workspace_feedback_submissions",
    )
    op.drop_index(
        "ix_workspace_feedback_submissions_submitted_by_user_id",
        table_name="workspace_feedback_submissions",
    )
    op.drop_table("workspace_feedback_submissions")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("feedback_reward_available_at")
