"""Persist privacy-safe daily OCR usage totals for document extraction.

Revision ID: 20260803_0054
Revises: 20260731_0053
Create Date: 2026-08-03 10:00:00

The table intentionally contains only daily aggregate counters.  It has no
workspace, candidate, resume, job, filename, source text, provider payload,
credential, or raw error columns, so platform runtime reporting cannot be
used to reconstruct an individual applicant's document activity.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0054"
down_revision: Union[str, Sequence[str], None] = "20260731_0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_extraction_ocr_daily_metrics",
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("document_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "completed_document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failed_document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_source_pages",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_attempted_document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_successful_document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_selected_document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_attempted_page_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_successful_page_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_selected_page_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ocr_failed_page_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "document_kind IN ('pdf', 'office', 'spreadsheet', 'image', 'html', 'other')",
            name="ck_doc_ocr_daily_metric_kind",
        ),
        sa.CheckConstraint(
            "document_count >= 0 AND completed_document_count >= 0 "
            "AND failed_document_count >= 0 AND total_source_pages >= 0 "
            "AND ocr_attempted_document_count >= 0 "
            "AND ocr_successful_document_count >= 0 "
            "AND ocr_selected_document_count >= 0 "
            "AND ocr_attempted_page_count >= 0 "
            "AND ocr_successful_page_count >= 0 "
            "AND ocr_selected_page_count >= 0 "
            "AND ocr_failed_page_count >= 0",
            name="ck_doc_ocr_daily_metric_nonnegative",
        ),
        sa.PrimaryKeyConstraint("metric_date", "document_kind"),
    )
    op.create_index(
        "ix_doc_ocr_daily_metric_date",
        "document_extraction_ocr_daily_metrics",
        ["metric_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_doc_ocr_daily_metric_date",
        table_name="document_extraction_ocr_daily_metrics",
    )
    op.drop_table("document_extraction_ocr_daily_metrics")
