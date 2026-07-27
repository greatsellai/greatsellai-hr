from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    return config


def _mailbox_values(*, mailbox_id: str, oauth: bool, archived: bool) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "id": mailbox_id,
        "organization_id": "00000000-0000-4000-8000-000000000037",
        "display_name": "OAuth 回滚测试邮箱",
        "display_name_key": f"oauth rollback {mailbox_id}",
        "provider_key": "gmail_oauth" if oauth else "feishu_app_password",
        "authentication_mode": "oauth2" if oauth else "app_password",
        "imap_host": "imap.gmail.com" if oauth else "imap.feishu.cn",
        "imap_port": 993,
        "email_address": f"{mailbox_id}@example.test",
        "mailbox": "INBOX",
        "encrypted_password": None if oauth else "safe-app-password-ciphertext",
        "enabled": True,
        "import_start_uid": 42,
        "imap_uidvalidity": 9,
        "import_started_at": now,
        "last_sync_started_at": now,
        "last_synced_at": now,
        "last_sync_error": None,
        "retention_policy": "standard",
        "last_retention_cleanup_at": None,
        "archived_at": now if archived else None,
        "sync_lease_token": None,
        "sync_lease_expires_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _upgrade_to_head(tmp_path) -> tuple[str, Config]:
    database_path = tmp_path / "mailbox-oauth-downgrade-guard.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    return database_url, config


@pytest.mark.parametrize(
    ("has_refresh_credential", "has_active_oauth_mailbox"),
    (
        (True, False),
        (False, True),
    ),
)
def test_oauth_downgrade_guard_preserves_unsafe_0037_state(
    tmp_path,
    has_refresh_credential: bool,
    has_active_oauth_mailbox: bool,
) -> None:
    database_url, config = _upgrade_to_head(tmp_path)
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        credentials = Table(
            "mailbox_oauth_credentials",
            metadata,
            autoload_with=engine,
        )
        mailbox_id = "mailbox-oauth-downgrade-guard"
        with engine.begin() as connection:
            connection.execute(
                mailboxes.insert(),
                _mailbox_values(
                    mailbox_id=mailbox_id,
                    oauth=True,
                    # A credential must block even when its source has been
                    # archived.  The active-channel branch independently
                    # exercises an unarchived OAuth row without a credential.
                    archived=has_refresh_credential and not has_active_oauth_mailbox,
                ),
            )
            if has_refresh_credential:
                now = datetime.now(timezone.utc)
                connection.execute(
                    credentials.insert(),
                    {
                        "id": "oauth-downgrade-guard-credential",
                        "organization_id": "00000000-0000-4000-8000-000000000037",
                        "mailbox_config_id": mailbox_id,
                        "encrypted_refresh_token": "opaque-refresh-token-ciphertext",
                        "reauthorization_required_at": None,
                        "last_error_code": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="mailbox_oauth_downgrade_blocked"):
        command.downgrade(config, "20260725_0037")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {
            "mailbox_oauth_credentials",
            "mailbox_oauth_connect_intents",
        } <= set(inspector.get_table_names())
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        credentials = Table(
            "mailbox_oauth_credentials",
            metadata,
            autoload_with=engine,
        )
        with engine.connect() as connection:
            assert connection.scalar(
                select(mailboxes.c.authentication_mode).where(mailboxes.c.id == mailbox_id)
            ) == "oauth2"
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260727_0039"
            )
            if has_refresh_credential:
                assert connection.scalar(select(credentials.c.id)) == (
                    "oauth-downgrade-guard-credential"
                )
    finally:
        engine.dispose()


def test_oauth_downgrade_guard_allows_safe_legacy_channel_rollback(tmp_path) -> None:
    database_url, config = _upgrade_to_head(tmp_path)
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                mailboxes.insert(),
                _mailbox_values(
                    mailbox_id="mailbox-safe-legacy-downgrade",
                    oauth=False,
                    archived=False,
                ),
            )
    finally:
        engine.dispose()

    command.downgrade(config, "20260725_0037")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "mailbox_oauth_credentials" not in inspector.get_table_names()
        mailbox_columns = {column["name"] for column in inspector.get_columns("mailbox_configs")}
        assert "provider_key" not in mailbox_columns
        assert "authentication_mode" not in mailbox_columns
        assert "oauth_reauthorization_generation" not in mailbox_columns
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        with engine.connect() as connection:
            assert connection.scalar(
                select(mailboxes.c.encrypted_password).where(
                    mailboxes.c.id == "mailbox-safe-legacy-downgrade"
                )
            ) == "safe-app-password-ciphertext"
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260725_0037"
            )
    finally:
        engine.dispose()
