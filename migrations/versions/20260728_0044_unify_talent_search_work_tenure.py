"""Unify historic talent-profile work-tenure filters.

Revision ID: 20260728_0044
Revises: 20260727_0043
Create Date: 2026-07-28 17:20:00

Older profile revisions and historic search runs could persist a
``min_employment_months`` threshold alongside the newer combined threshold.
The recruiter-facing product now has one work-tenure definition: explicit
employment and internship duration, with overlap removed.  This data-only
migration folds the stricter old value into that combined threshold and
removes the retired key from both immutable revision payloads and run
snapshots.

The source experience types remain untouched.  They are evidence facts used to
avoid treating projects, competitions, or research as work tenure.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0044"
down_revision: Union[str, Sequence[str], None] = "20260727_0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _month_count(value: object) -> int | None:
    """Return a valid legacy month count without coercing arbitrary JSON."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 720:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        if 0 <= parsed <= 720:
            return parsed
    return None


def _unify_work_tenure(payload: object) -> tuple[object, bool]:
    """Move the legacy formal-work threshold into the combined field."""

    if not isinstance(payload, Mapping) or "min_employment_months" not in payload:
        return payload, False

    normalized = dict(payload)
    legacy_value = normalized.pop("min_employment_months", None)
    month_counts = [
        month_count
        for month_count in (
            _month_count(legacy_value),
            _month_count(normalized.get("min_employment_or_internship_months")),
        )
        if month_count is not None
    ]
    if month_counts:
        normalized["min_employment_or_internship_months"] = max(month_counts)
    return normalized, True


def _backfill_json_column(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("id", sa.String(length=36)),
        sa.column(column_name, sa.JSON()),
    )
    rows = bind.execute(sa.select(table.c.id, table.c[column_name])).all()
    for row_id, payload in rows:
        normalized, changed = _unify_work_tenure(payload)
        if changed:
            bind.execute(
                table.update()
                .where(table.c.id == row_id)
                .values({column_name: normalized})
            )


def upgrade() -> None:
    _backfill_json_column("talent_search_profile_revisions", "hard_filters")
    _backfill_json_column("talent_search_runs", "hard_filter_snapshot")


def downgrade() -> None:
    # This is intentionally irreversible: recreating a formal-work-only
    # threshold from a combined work-and-internship value would be misleading.
    pass
