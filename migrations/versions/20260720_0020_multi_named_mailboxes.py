"""Make mailbox ingestion sources independently named and resumable.

Revision ID: 20260720_0020
Revises: 20260720_0019
Create Date: 2026-07-20 16:30:00
"""
from __future__ import annotations

import unicodedata
from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0020"
down_revision: Union[str, Sequence[str], None] = "20260720_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_MAILBOX_LABEL = "默认收件邮箱"


def _display_name_key(value: str) -> str:
    """Return the durable comparison key used by the mailbox service.

    The runtime service applies the same NFKC + whitespace + casefold policy
    before inserting a new channel. Keeping the migration's backfill aligned
    prevents a legacy default label from becoming an accidental duplicate.
    """

    normalized = unicodedata.normalize("NFKC", value).strip()
    return " ".join(normalized.split()).casefold()


def _mailbox_configs_table() -> sa.Table:
    return sa.table(
        "mailbox_configs",
        sa.column("id", sa.String(length=36)),
        sa.column("organization_id", sa.String(length=36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("display_name", sa.String(length=32)),
        sa.column("display_name_key", sa.String(length=64)),
    )


def _backfill_mailbox_display_names() -> dict[str, str]:
    """Assign deterministic, per-workspace labels without changing IDs/state."""

    bind = op.get_bind()
    mailbox_configs = _mailbox_configs_table()
    rows = bind.execute(
        sa.select(
            mailbox_configs.c.id,
            mailbox_configs.c.organization_id,
        ).order_by(
            mailbox_configs.c.organization_id,
            mailbox_configs.c.created_at,
            mailbox_configs.c.id,
        )
    ).mappings()

    channel_count_by_organization: defaultdict[str | None, int] = defaultdict(int)
    label_by_config_id: dict[str, str] = {}
    for row in rows:
        organization_id = row["organization_id"]
        channel_index = channel_count_by_organization[organization_id]
        label = (
            DEFAULT_MAILBOX_LABEL
            if channel_index == 0
            else f"{DEFAULT_MAILBOX_LABEL} {channel_index + 1}"
        )
        channel_count_by_organization[organization_id] += 1
        label_by_config_id[row["id"]] = label
        bind.execute(
            mailbox_configs.update()
            .where(mailbox_configs.c.id == row["id"])
            .values(
                display_name=label,
                display_name_key=_display_name_key(label),
            )
        )

    return label_by_config_id


def _backfill_resume_mailbox_provenance(label_by_config_id: dict[str, str]) -> None:
    """Preserve a stable source for existing imported resumes.

    A resume can theoretically have multiple historical import records. The
    first durable record by timestamp and ID is its original source; later
    duplicate imports must not overwrite that provenance.
    """

    bind = op.get_bind()
    resumes = sa.table(
        "resumes",
        sa.column("id", sa.String(length=36)),
        sa.column("organization_id", sa.String(length=36)),
        sa.column("ingestion_source_type", sa.String(length=32)),
        sa.column("source_mailbox_config_id", sa.String(length=36)),
        sa.column("source_mailbox_label_snapshot", sa.String(length=64)),
    )
    imports = sa.table(
        "email_attachment_imports",
        sa.column("id", sa.String(length=36)),
        sa.column("organization_id", sa.String(length=36)),
        sa.column("mailbox_config_id", sa.String(length=36)),
        sa.column("resume_id", sa.String(length=36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    rows = bind.execute(
        sa.select(
            imports.c.id,
            imports.c.organization_id,
            imports.c.mailbox_config_id,
            imports.c.resume_id,
        )
        .where(imports.c.resume_id.is_not(None))
        .order_by(imports.c.resume_id, imports.c.created_at, imports.c.id)
    ).mappings()

    seen_resume_ids: set[str] = set()
    for row in rows:
        resume_id = row["resume_id"]
        if resume_id in seen_resume_ids:
            continue
        seen_resume_ids.add(resume_id)

        mailbox_config_id = row["mailbox_config_id"]
        label = label_by_config_id.get(mailbox_config_id)
        if label is None:
            # This should be unreachable with existing foreign keys. Leaving
            # the conservative manual-upload default is safer than attaching
            # a resume to an unknown channel.
            continue

        bind.execute(
            resumes.update()
            .where(
                resumes.c.id == resume_id,
                resumes.c.organization_id == row["organization_id"],
                resumes.c.source_mailbox_config_id.is_(None),
            )
            .values(
                ingestion_source_type="mailbox_attachment",
                source_mailbox_config_id=mailbox_config_id,
                source_mailbox_label_snapshot=label,
            )
        )


def upgrade() -> None:
    # Start nullable so existing configurations can be labelled deterministically
    # before the workspace-local uniqueness and non-null guarantees are added.
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "display_name",
                sa.String(length=32),
                nullable=True,
                server_default=sa.text(f"'{DEFAULT_MAILBOX_LABEL}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "display_name_key",
                sa.String(length=64),
                nullable=True,
                server_default=sa.text(f"'{_display_name_key(DEFAULT_MAILBOX_LABEL)}'"),
            )
        )
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_lease_token", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("sync_lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_sync_started_at", sa.DateTime(timezone=True), nullable=True)
        )

    label_by_config_id = _backfill_mailbox_display_names()
    # Existing successful sync timestamps are the best safe approximation for
    # a prior attempt.  This avoids a fleet-wide catch-up loop immediately
    # after the migration while preserving first-run configs as due now.
    op.execute(
        "UPDATE mailbox_configs SET last_sync_started_at = last_synced_at "
        "WHERE last_sync_started_at IS NULL AND last_synced_at IS NOT NULL"
    )

    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.alter_column(
            "display_name",
            existing_type=sa.String(length=32),
            nullable=False,
            existing_server_default=sa.text(f"'{DEFAULT_MAILBOX_LABEL}'"),
        )
        batch_op.alter_column(
            "display_name_key",
            existing_type=sa.String(length=64),
            nullable=False,
            existing_server_default=sa.text(f"'{_display_name_key(DEFAULT_MAILBOX_LABEL)}'"),
        )
        batch_op.create_unique_constraint(
            "uq_mailbox_configs_organization_display_name_key",
            ["organization_id", "display_name_key"],
        )
    op.create_index(
        "ix_mailbox_configs_organization_sync_claim",
        "mailbox_configs",
        ["organization_id", "enabled", "archived_at", "sync_lease_expires_at"],
    )

    # Resume provenance is nullable except for the conservative source type.
    # This preserves existing manual uploads while allowing mailbox imports to
    # retain a historical label even after a channel is renamed or archived.
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ingestion_source_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'manual_upload'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_mailbox_config_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_mailbox_label_snapshot",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_resumes_source_mailbox_config_id",
            "mailbox_configs",
            ["source_mailbox_config_id"],
            ["id"],
        )

    _backfill_resume_mailbox_provenance(label_by_config_id)
    op.create_index(
        "ix_resumes_organization_source_mailbox",
        "resumes",
        ["organization_id", "source_mailbox_config_id"],
    )
    op.create_index(
        "ix_resumes_organization_ingestion_source",
        "resumes",
        ["organization_id", "ingestion_source_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_resumes_organization_ingestion_source",
        table_name="resumes",
    )
    op.drop_index(
        "ix_resumes_organization_source_mailbox",
        table_name="resumes",
    )
    with op.batch_alter_table("resumes") as batch_op:
        batch_op.drop_constraint(
            "fk_resumes_source_mailbox_config_id",
            type_="foreignkey",
        )
        batch_op.drop_column("source_mailbox_label_snapshot")
        batch_op.drop_column("source_mailbox_config_id")
        batch_op.drop_column("ingestion_source_type")

    op.drop_index(
        "ix_mailbox_configs_organization_sync_claim",
        table_name="mailbox_configs",
    )
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.drop_constraint(
            "uq_mailbox_configs_organization_display_name_key",
            type_="unique",
        )
        batch_op.drop_column("sync_lease_expires_at")
        batch_op.drop_column("sync_lease_token")
        batch_op.drop_column("last_sync_started_at")
        batch_op.drop_column("archived_at")
        batch_op.drop_column("display_name_key")
        batch_op.drop_column("display_name")
