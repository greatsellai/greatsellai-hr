"""Add reviewed mailbox providers and OAuth credential storage.

Revision ID: 20260727_0038
Revises: 20260725_0037
Create Date: 2026-07-27 16:10:00

Existing IMAP channels retain their encrypted app password, UID watermark,
imports and source identity.  OAuth refresh tokens are stored in a new table
and never reuse the historical password column.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0038"
down_revision: Union[str, Sequence[str], None] = "20260725_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mailbox_configs",
        sa.Column(
            "provider_key",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'legacy_imap'"),
        ),
    )
    op.add_column(
        "mailbox_configs",
        sa.Column(
            "authentication_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'app_password'"),
        ),
    )
    # Each reauthorization browser handoff gets an incremented generation.
    # Existing app-password and OAuth rows start at zero; the runtime only
    # uses a positive generation for an actual OAuth reauthorization.
    op.add_column(
        "mailbox_configs",
        sa.Column(
            "oauth_reauthorization_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    mailbox_configs = sa.table(
        "mailbox_configs",
        sa.column("imap_host", sa.String()),
        sa.column("imap_port", sa.Integer()),
        sa.column("provider_key", sa.String()),
    )
    op.execute(
        mailbox_configs.update().values(
            provider_key=sa.case(
                (
                    sa.and_(
                        sa.func.lower(mailbox_configs.c.imap_host) == "imap.feishu.cn",
                        mailbox_configs.c.imap_port == 993,
                    ),
                    "feishu_app_password",
                ),
                (
                    sa.and_(
                        sa.func.lower(mailbox_configs.c.imap_host) == "imap.exmail.qq.com",
                        mailbox_configs.c.imap_port == 993,
                    ),
                    "tencent_exmail_app_password",
                ),
                (
                    sa.and_(
                        sa.func.lower(mailbox_configs.c.imap_host) == "imap.qq.com",
                        mailbox_configs.c.imap_port == 993,
                    ),
                    "qq_mail_app_password",
                ),
                else_="legacy_imap",
            )
        )
    )
    # The legacy column must be nullable because OAuth-backed channels do not
    # contain an app password. SQLite needs batch mode for this alteration.
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.alter_column("encrypted_password", existing_type=sa.Text(), nullable=True)

    op.create_table(
        "mailbox_oauth_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("mailbox_config_id", sa.String(length=36), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("reauthorization_required_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["mailbox_config_id"], ["mailbox_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mailbox_config_id",
            name="uq_mailbox_oauth_credentials_mailbox_config",
        ),
    )
    op.create_index(
        "ix_mailbox_oauth_credentials_organization_mailbox",
        "mailbox_oauth_credentials",
        ["organization_id", "mailbox_config_id"],
        unique=False,
    )
    op.create_index(
        "ix_mailbox_oauth_credentials_organization_id",
        "mailbox_oauth_credentials",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "mailbox_oauth_connect_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("membership_id", sa.String(), nullable=False),
        sa.Column("target_mailbox_config_id", sa.String(length=36), nullable=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=32), nullable=False),
        sa.Column("email_address", sa.String(length=320), nullable=False),
        sa.Column("mailbox", sa.String(length=255), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=False),
        sa.Column(
            "reauthorization_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(["membership_id"], ["organization_memberships.id"]),
        sa.ForeignKeyConstraint(["target_mailbox_config_id"], ["mailbox_configs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "state_hash",
            name="uq_mailbox_oauth_connect_intents_state_hash",
        ),
    )
    op.create_index(
        "ix_mailbox_oauth_connect_intents_organization_expiry",
        "mailbox_oauth_connect_intents",
        ["organization_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_mailbox_oauth_connect_intents_organization_id",
        "mailbox_oauth_connect_intents",
        ["organization_id"],
        unique=False,
    )

    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("mailbox_configs", "provider_key", server_default=None)
        op.alter_column("mailbox_configs", "authentication_mode", server_default=None)


def downgrade() -> None:
    # Production application rollbacks never run schema downgrades.  Keeping
    # the historical encrypted-password column nullable is the only safe
    # non-destructive fallback for a database that may contain OAuth channels.
    op.drop_index(
        "ix_mailbox_oauth_connect_intents_organization_id",
        table_name="mailbox_oauth_connect_intents",
    )
    op.drop_index(
        "ix_mailbox_oauth_connect_intents_organization_expiry",
        table_name="mailbox_oauth_connect_intents",
    )
    op.drop_table("mailbox_oauth_connect_intents")
    op.drop_index(
        "ix_mailbox_oauth_credentials_organization_id",
        table_name="mailbox_oauth_credentials",
    )
    op.drop_index(
        "ix_mailbox_oauth_credentials_organization_mailbox",
        table_name="mailbox_oauth_credentials",
    )
    op.drop_table("mailbox_oauth_credentials")
    with op.batch_alter_table("mailbox_configs") as batch_op:
        batch_op.drop_column("oauth_reauthorization_generation")
        batch_op.drop_column("authentication_mode")
        batch_op.drop_column("provider_key")
