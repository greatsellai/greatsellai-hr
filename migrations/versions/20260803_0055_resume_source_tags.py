"""Add workspace-scoped source tags for mailbox-originated resumes.

Revision ID: 20260803_0055
Revises: 20260803_0054
Create Date: 2026-08-03 16:30:00

Source tags are intentionally recorded at two levels:

* ``email_attachment_import_tags`` is immutable event truth for an individual
  message attachment.
* ``resume_source_tags`` is the current, deduplicated projection used by
  candidate filtering and display.

All child links carry ``organization_id`` in their foreign keys. The three
existing parent tables receive redundant unique composite indexes so SQLite
and PostgreSQL can enforce the workspace boundary at the database level
without rewriting existing candidate or mailbox data.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260803_0055"
down_revision: Union[str, Sequence[str], None] = "20260803_0054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A primary key on ``id`` alone does not permit a composite FK that proves
    # an ID and organization belong together. These indexes are intentionally
    # redundant and do not alter existing row values.
    op.create_index(
        "uq_resumes_id_organization",
        "resumes",
        ["id", "organization_id"],
        unique=True,
    )
    op.create_index(
        "uq_mailbox_configs_id_organization",
        "mailbox_configs",
        ["id", "organization_id"],
        unique=True,
    )
    op.create_index(
        "uq_email_attachment_imports_id_organization",
        "email_attachment_imports",
        ["id", "organization_id"],
        unique=True,
    )

    op.create_table(
        "source_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("name_key", sa.String(length=128), nullable=False),
        sa.Column("system_key", sa.String(length=64), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_source_tags_sort_order_nonnegative",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_source_tags_id_organization",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "name_key",
            name="uq_source_tags_organization_name_key",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "system_key",
            name="uq_source_tags_organization_system_key",
        ),
    )
    op.create_index(
        "ix_source_tags_organization_id",
        "source_tags",
        ["organization_id"],
        unique=False,
    )
    op.create_index("ix_source_tags_enabled", "source_tags", ["enabled"], unique=False)
    op.create_index(
        "ix_source_tags_organization_enabled_order",
        "source_tags",
        ["organization_id", "enabled", "sort_order", "display_name"],
        unique=False,
    )

    op.create_table(
        "mailbox_source_tag_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("source_tag_id", sa.String(length=36), nullable=False),
        sa.Column("match_kind", sa.String(length=32), nullable=False),
        sa.Column("match_value", sa.String(length=320), nullable=False),
        sa.Column("match_value_key", sa.String(length=320), nullable=False),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "match_kind IN ('sender_domain', 'sender_address', 'subject_keyword')",
            name="ck_mailbox_source_tag_rule_match_kind",
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name="ck_mailbox_source_tag_rule_priority_nonnegative",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["mailbox_config_id", "organization_id"],
            ["mailbox_configs.id", "mailbox_configs.organization_id"],
            name="fk_mailbox_source_tag_rules_mailbox_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_tag_id", "organization_id"],
            ["source_tags.id", "source_tags.organization_id"],
            name="fk_mailbox_source_tag_rules_source_tag_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_mailbox_source_tag_rules_id_organization",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "mailbox_config_id",
            "source_tag_id",
            "match_kind",
            "match_value_key",
            name="uq_mailbox_source_tag_rule_match",
        ),
    )
    op.create_index(
        "ix_mailbox_source_tag_rules_organization_id",
        "mailbox_source_tag_rules",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_mailbox_source_tag_rules_mailbox_config_id",
        "mailbox_source_tag_rules",
        ["mailbox_config_id"],
        unique=False,
    )
    op.create_index(
        "ix_mailbox_source_tag_rules_source_tag_id",
        "mailbox_source_tag_rules",
        ["source_tag_id"],
        unique=False,
    )
    op.create_index(
        "ix_mailbox_source_tag_rules_enabled",
        "mailbox_source_tag_rules",
        ["enabled"],
        unique=False,
    )
    op.create_index(
        "ix_mb_source_tag_rules_org_mb_active_priority",
        "mailbox_source_tag_rules",
        ["organization_id", "mailbox_config_id", "enabled", "priority"],
        unique=False,
    )

    op.create_table(
        "email_attachment_import_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("email_attachment_import_id", sa.String(length=36), nullable=False),
        sa.Column("source_tag_id", sa.String(length=36), nullable=False),
        sa.Column("assignment_kind", sa.String(length=32), nullable=False),
        sa.Column("matched_rule_id", sa.String(length=36), nullable=True),
        sa.Column("tag_name_snapshot", sa.String(length=64), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assignment_kind IN ('builtin', 'mailbox_rule')",
            name="ck_email_attachment_import_tag_assignment_kind",
        ),
        sa.CheckConstraint(
            "assignment_kind != 'mailbox_rule' OR matched_rule_id IS NOT NULL",
            name="ck_email_attachment_import_tag_rule_required",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["email_attachment_import_id", "organization_id"],
            [
                "email_attachment_imports.id",
                "email_attachment_imports.organization_id",
            ],
            name="fk_email_attachment_import_tags_import_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_tag_id", "organization_id"],
            ["source_tags.id", "source_tags.organization_id"],
            name="fk_email_attachment_import_tags_source_tag_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matched_rule_id", "organization_id"],
            ["mailbox_source_tag_rules.id", "mailbox_source_tag_rules.organization_id"],
            name="fk_email_attachment_import_tags_rule_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_email_attachment_import_tags_id_organization",
        ),
        sa.UniqueConstraint(
            "email_attachment_import_id",
            "source_tag_id",
            name="uq_email_attachment_import_tag",
        ),
    )
    op.create_index(
        "ix_email_attachment_import_tags_organization_id",
        "email_attachment_import_tags",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_attachment_import_tags_email_attachment_import_id",
        "email_attachment_import_tags",
        ["email_attachment_import_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_attachment_import_tags_source_tag_id",
        "email_attachment_import_tags",
        ["source_tag_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_attachment_import_tags_matched_rule_id",
        "email_attachment_import_tags",
        ["matched_rule_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_attachment_import_tags_organization_tag_import",
        "email_attachment_import_tags",
        ["organization_id", "source_tag_id", "email_attachment_import_id"],
        unique=False,
    )

    op.create_table(
        "resume_source_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("resume_id", sa.String(length=36), nullable=False),
        sa.Column("source_tag_id", sa.String(length=36), nullable=False),
        sa.Column("tag_name_snapshot", sa.String(length=64), nullable=False),
        sa.Column("first_import_id", sa.String(length=36), nullable=True),
        sa.Column("last_import_id", sa.String(length=36), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_count >= 0",
            name="ck_resume_source_tag_source_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["resume_id", "organization_id"],
            ["resumes.id", "resumes.organization_id"],
            name="fk_resume_source_tags_resume_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_tag_id", "organization_id"],
            ["source_tags.id", "source_tags.organization_id"],
            name="fk_resume_source_tags_source_tag_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_import_id", "organization_id"],
            [
                "email_attachment_imports.id",
                "email_attachment_imports.organization_id",
            ],
            name="fk_resume_source_tags_first_import_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id", "organization_id"],
            [
                "email_attachment_imports.id",
                "email_attachment_imports.organization_id",
            ],
            name="fk_resume_source_tags_last_import_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_resume_source_tags_id_organization",
        ),
        sa.UniqueConstraint(
            "resume_id",
            "source_tag_id",
            name="uq_resume_source_tag",
        ),
    )
    op.create_index(
        "ix_resume_source_tags_organization_id",
        "resume_source_tags",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_source_tags_resume_id",
        "resume_source_tags",
        ["resume_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_source_tags_source_tag_id",
        "resume_source_tags",
        ["source_tag_id"],
        unique=False,
    )
    op.create_index(
        "ix_resume_source_tags_organization_tag_resume",
        "resume_source_tags",
        ["organization_id", "source_tag_id", "resume_id"],
        unique=False,
    )


def downgrade() -> None:
    # Child tables must disappear before the redundant parent keys they use.
    op.drop_table("resume_source_tags")
    op.drop_table("email_attachment_import_tags")
    op.drop_table("mailbox_source_tag_rules")
    op.drop_table("source_tags")

    op.drop_index(
        "uq_email_attachment_imports_id_organization",
        table_name="email_attachment_imports",
    )
    op.drop_index(
        "uq_mailbox_configs_id_organization",
        table_name="mailbox_configs",
    )
    op.drop_index("uq_resumes_id_organization", table_name="resumes")
