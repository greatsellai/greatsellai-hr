"""Persist exact, evidence-led education institution classifications.

Revision ID: 20260721_0027
Revises: 20260721_0026
Create Date: 2026-07-21 14:00:00

The migration only backfills classifications that were already provable from
the bundled historical 985/211 registry or old stored tags.  It intentionally
does not infer ordinary undergraduate, associate, secondary-vocational, or
overseas status from a candidate's degree wording.  New and re-saved facts use
the versioned registry service for those categories.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0027"
down_revision: Union[str, Sequence[str], None] = "20260721_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_HISTORICAL_985_211_VERSION = "moe-985-211-2005-2006.v1"


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, tuple):
        return [item for item in value if isinstance(item, str)]
    return []


def _classification_from_legacy(
    *,
    roster_id: str | None,
    institution_tiers: object,
) -> str | None:
    if isinstance(roster_id, str):
        if roster_id.startswith("cn-985-"):
            return "985"
        if roster_id.startswith("cn-211-"):
            return "211"
    tiers = set(_json_list(institution_tiers))
    # Historical 985 rows were written as ["211", "985"].  Prefer 985 even
    # where an old relation is missing, then retain a genuine 211-only value.
    if "985" in tiers:
        return "985"
    if "211" in tiers:
        return "211"
    return None


def upgrade() -> None:
    op.add_column(
        "resume_educations",
        sa.Column("institution_classification", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "resume_educations",
        sa.Column("classification_basis", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "resume_educations",
        sa.Column(
            "classification_registry_version",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "resume_educations",
        sa.Column(
            "classification_evidence_block_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_index(
        "ix_resume_educations_resume_id_institution_classification",
        "resume_educations",
        ["resume_id", "institution_classification"],
    )

    bind = op.get_bind()
    institutions = sa.table(
        "institutions",
        sa.column("id", sa.String()),
        sa.column("roster_id", sa.String()),
        sa.column("tier_tags", sa.JSON()),
    )
    educations = sa.table(
        "resume_educations",
        sa.column("id", sa.String()),
        sa.column("institution_id", sa.String()),
        sa.column("institution_tiers", sa.JSON()),
        sa.column("evidence_block_ids", sa.JSON()),
        sa.column("institution_classification", sa.String()),
        sa.column("classification_basis", sa.String()),
        sa.column("classification_registry_version", sa.String()),
        sa.column("classification_evidence_block_ids", sa.JSON()),
    )

    # Correct the seed-table representation too.  The old double tag made a
    # 211 filter include 985 records even before candidate facts were read.
    bind.execute(
        institutions.update()
        .where(institutions.c.roster_id.like("cn-985-%"))
        .values(tier_tags=["985"])
    )
    bind.execute(
        institutions.update()
        .where(institutions.c.roster_id.like("cn-211-%"))
        .values(tier_tags=["211"])
    )

    rows = bind.execute(
        sa.select(
            educations.c.id,
            educations.c.institution_id,
            educations.c.institution_tiers,
            educations.c.evidence_block_ids,
            institutions.c.roster_id,
        ).select_from(
            educations.outerjoin(
                institutions,
                educations.c.institution_id == institutions.c.id,
            )
        )
    ).mappings()
    for row in rows:
        classification = _classification_from_legacy(
            roster_id=row["roster_id"],
            institution_tiers=row["institution_tiers"],
        )
        if classification is None:
            continue
        bind.execute(
            educations.update()
            .where(educations.c.id == row["id"])
            .values(
                institution_tiers=[classification],
                institution_classification=classification,
                classification_basis="moe_985_211_registry",
                classification_registry_version=_HISTORICAL_985_211_VERSION,
                classification_evidence_block_ids=_json_list(
                    row["evidence_block_ids"]
                ),
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_resume_educations_resume_id_institution_classification",
        table_name="resume_educations",
    )
    op.drop_column("resume_educations", "classification_evidence_block_ids")
    op.drop_column("resume_educations", "classification_registry_version")
    op.drop_column("resume_educations", "classification_basis")
    op.drop_column("resume_educations", "institution_classification")
