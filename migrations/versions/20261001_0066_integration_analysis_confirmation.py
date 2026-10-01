"""Require browser confirmation before external AI drafts become readable.

Revision ID: 20261001_0066
Revises: 20260928_0065
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261001_0066"
down_revision: Union[str, Sequence[str], None] = "20260928_0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A pre-0066 report has no trustworthy browser-confirmation provenance.
    # Fail before any DDL if this revision is being applied to a database that
    # already contains reports; an operator must review that legacy state
    # instead of silently treating its creation time as human approval.
    if op.get_context().as_sql:
        raise RuntimeError("integration_confirmation_requires_online_preflight")
    legacy_report = op.get_bind().execute(sa.text(
        "SELECT 1 FROM integration_analysis_reports LIMIT 1"
    )).first()
    if legacy_report is not None:
        raise RuntimeError("integration_confirmation_legacy_reports_require_review")

    op.add_column(
        "integration_analysis_reports",
        sa.Column("source_credential_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "integration_analysis_reports",
        sa.Column("source_audience", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "integration_analysis_reports",
        sa.Column("source_auth_session_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "integration_analysis_reports",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    with op.batch_alter_table("integration_analysis_reports") as batch_op:
        batch_op.create_check_constraint(
            "ck_integration_report_pending_confirmation_binding",
            "confirmed_at IS NOT NULL OR (source_credential_id IS NOT NULL AND "
            "source_audience IN ('rest', 'mcp') AND source_auth_session_version IS NOT NULL)",
        )
    op.create_index(
        "ix_integration_report_confirmed_at",
        "integration_analysis_reports",
        ["confirmed_at"],
    )
    op.create_index(
        "ix_integration_report_pending",
        "integration_analysis_reports",
        ["organization_id", "owner_user_id", "membership_id", "confirmed_at", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError("integration_schema_downgrade_blocked")
