"""Unify score dimensions to 100 points and add durable score batches.

The raw-score normalization is intentionally irreversible: downgrade restores
the old column shape with a default maximum of 100, but cannot reconstruct
each historical per-dimension maximum after values have been normalized.

Revision ID: 20260720_0016
Revises: 20260720_0013
Create Date: 2026-07-20 14:00:00
"""
from __future__ import annotations

import math
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0016"
down_revision: Union[str, Sequence[str], None] = "20260720_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _normalize_score_records() -> None:
    """Preserve historical score meaning while moving every raw score to /100.

    Existing score JSON stores the old per-dimension maximum alongside the raw
    value.  Scaling the raw values before dropping the template column keeps
    total contributions unchanged and avoids presenting an old 40/80 score as
    an incorrect 40/100 score.  Manual-adjustment audit values are scaled with
    the same dimension map so score history remains internally consistent.
    """

    bind = op.get_bind()
    metadata = sa.MetaData()
    scores = sa.Table(
        "resume_scores",
        metadata,
        sa.Column("id", sa.String(length=36)),
        sa.Column("dimension_scores", sa.JSON()),
    )
    actions = sa.Table(
        "resume_review_actions",
        metadata,
        sa.Column("id", sa.String(length=36)),
        sa.Column("action", sa.String(length=64)),
        sa.Column("old_values", sa.JSON()),
        sa.Column("new_values", sa.JSON()),
    )

    max_by_score_and_dimension: dict[tuple[str, str], float] = {}
    score_rows = bind.execute(
        sa.select(scores.c.id, scores.c.dimension_scores)
    ).mappings()
    for row in score_rows:
        score_id = str(row["id"])
        raw_dimensions = row["dimension_scores"]
        if not isinstance(raw_dimensions, list):
            continue
        normalized_dimensions: list[object] = []
        changed = False
        for raw_dimension in raw_dimensions:
            if not isinstance(raw_dimension, dict):
                normalized_dimensions.append(raw_dimension)
                continue
            dimension = dict(raw_dimension)
            key = dimension.get("key")
            raw_max = _finite_number(dimension.get("max_raw_score"))
            max_raw_score = raw_max if raw_max is not None and raw_max > 0 else 100.0
            if isinstance(key, str) and key:
                max_by_score_and_dimension[(score_id, key)] = max_raw_score
            for score_key in ("ai_raw_score", "final_raw_score"):
                raw_score = _finite_number(dimension.get(score_key))
                if raw_score is not None:
                    dimension[score_key] = round(raw_score / max_raw_score * 100, 4)
            if "max_raw_score" in dimension:
                dimension.pop("max_raw_score", None)
                changed = True
            normalized_dimensions.append(dimension)
        if changed:
            bind.execute(
                scores.update()
                .where(scores.c.id == score_id)
                .values(dimension_scores=normalized_dimensions)
            )

    action_rows = bind.execute(
        sa.select(
            actions.c.id,
            actions.c.action,
            actions.c.old_values,
            actions.c.new_values,
        ).where(actions.c.action == "score_dimension_overridden")
    ).mappings()
    for row in action_rows:
        raw_old = row["old_values"]
        raw_new = row["new_values"]
        old_values = dict(raw_old) if isinstance(raw_old, dict) else None
        new_values = dict(raw_new) if isinstance(raw_new, dict) else None
        ownership = new_values or old_values or {}
        score_id = ownership.get("score_id")
        dimension_key = ownership.get("dimension_key")
        if not isinstance(score_id, str) or not isinstance(dimension_key, str):
            continue
        max_raw_score = max_by_score_and_dimension.get((score_id, dimension_key))
        if max_raw_score is None or max_raw_score <= 0:
            continue

        def normalize_audit_values(values: dict[str, object] | None) -> dict[str, object] | None:
            if values is None:
                return None
            normalized = dict(values)
            for score_key in ("ai_raw_score", "final_raw_score"):
                raw_score = _finite_number(normalized.get(score_key))
                if raw_score is not None:
                    normalized[score_key] = round(raw_score / max_raw_score * 100, 4)
            return normalized

        bind.execute(
            actions.update()
            .where(actions.c.id == row["id"])
            .values(
                old_values=normalize_audit_values(old_values),
                new_values=normalize_audit_values(new_values),
            )
        )


def upgrade() -> None:
    _normalize_score_records()
    with op.batch_alter_table("score_template_dimensions") as batch_op:
        batch_op.drop_column("max_raw_score")

    op.create_table(
        "resume_score_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("cached_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_resume_score_batches_organization_id"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["score_templates.id"], name="fk_resume_score_batches_template_id"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resume_score_batches_organization_claim",
        "resume_score_batches",
        ["organization_id", "status", "lease_expires_at"],
    )
    op.create_index(
        "ix_resume_score_batches_organization_id",
        "resume_score_batches",
        ["organization_id"],
    )
    op.create_index(
        "ix_resume_score_batches_template_id",
        "resume_score_batches",
        ["template_id"],
    )
    op.create_index(
        "ix_resume_score_batches_status",
        "resume_score_batches",
        ["status"],
    )
    active_batch_predicate = sa.text("status IN ('queued', 'running')")
    op.create_index(
        "uq_resume_score_batches_active_template",
        "resume_score_batches",
        ["organization_id", "template_id", "template_version"],
        unique=True,
        sqlite_where=active_batch_predicate,
        postgresql_where=active_batch_predicate,
    )

    op.create_table(
        "resume_score_batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("fact_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("facts_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("resume_score_id", sa.String(length=36), nullable=True),
        sa.Column("was_cached", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_resume_score_batch_items_organization_id"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["resume_score_batches.id"], name="fk_resume_score_batch_items_batch_id"
        ),
        sa.ForeignKeyConstraint(
            ["resume_id"], ["resumes.id"], name="fk_resume_score_batch_items_resume_id"
        ),
        sa.ForeignKeyConstraint(
            ["fact_snapshot_id"], ["resume_fact_snapshots.id"], name="fk_resume_score_batch_items_snapshot_id"
        ),
        sa.ForeignKeyConstraint(
            ["resume_score_id"], ["resume_scores.id"], name="fk_resume_score_batch_items_score_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "resume_id", name="uq_resume_score_batch_item_resume"),
    )
    op.create_index(
        "ix_resume_score_batch_item_claim",
        "resume_score_batch_items",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_resume_score_batch_item_organization_claim",
        "resume_score_batch_items",
        ["organization_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "ix_resume_score_batch_items_organization_id",
        "resume_score_batch_items",
        ["organization_id"],
    )
    op.create_index(
        "ix_resume_score_batch_items_batch_id",
        "resume_score_batch_items",
        ["batch_id"],
    )
    op.create_index(
        "ix_resume_score_batch_items_resume_id",
        "resume_score_batch_items",
        ["resume_id"],
    )
    op.create_index(
        "ix_resume_score_batch_items_fact_snapshot_id",
        "resume_score_batch_items",
        ["fact_snapshot_id"],
    )
    op.create_index(
        "ix_resume_score_batch_items_status",
        "resume_score_batch_items",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_resume_score_batch_items_status", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_items_fact_snapshot_id", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_items_resume_id", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_items_batch_id", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_items_organization_id", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_item_organization_claim", table_name="resume_score_batch_items")
    op.drop_index("ix_resume_score_batch_item_claim", table_name="resume_score_batch_items")
    op.drop_table("resume_score_batch_items")
    op.drop_index("uq_resume_score_batches_active_template", table_name="resume_score_batches")
    op.drop_index("ix_resume_score_batches_status", table_name="resume_score_batches")
    op.drop_index("ix_resume_score_batches_template_id", table_name="resume_score_batches")
    op.drop_index("ix_resume_score_batches_organization_id", table_name="resume_score_batches")
    op.drop_index("ix_resume_score_batches_organization_claim", table_name="resume_score_batches")
    op.drop_table("resume_score_batches")
    with op.batch_alter_table("score_template_dimensions") as batch_op:
        batch_op.add_column(
            sa.Column("max_raw_score", sa.Integer(), server_default="100", nullable=False)
        )
