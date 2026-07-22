from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect
from sqlalchemy.exc import IntegrityError


def test_transactional_email_outbox_migration_adds_durable_reset_delivery_queue(tmp_path) -> None:
    database_path = tmp_path / "transactional-email-outbox-migration.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260721_0029")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        resets = Table("password_reset_tokens", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                {
                    "id": "outbox-migration-user",
                    "email": "outbox-migration@example.test",
                    "email_key": "outbox-migration@example.test",
                    "full_name": "Outbox Migration User",
                    "password_hash": "migration-password-hash",
                    "is_active": True,
                    "is_platform_admin": False,
                    "email_verified_at": now,
                    "last_login_at": None,
                    "auth_session_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                resets.insert(),
                {
                    "id": "outbox-migration-reset",
                    "user_id": "outbox-migration-user",
                    "token_digest": "a" * 64,
                    "expires_at": now + timedelta(hours=1),
                    "used_at": None,
                    "invalidated_at": None,
                    "requested_at": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        outbox = Table("transactional_email_outbox", metadata, autoload_with=engine)
        inspector = inspect(engine)
        assert {
            "id",
            "message_kind",
            "user_id",
            "password_reset_token_id",
            "encrypted_payload",
            "status",
            "attempt_count",
            "max_attempts",
            "next_attempt_at",
            "lease_owner",
            "lease_expires_at",
            "last_error",
            "requested_at",
            "started_at",
            "sent_at",
            "completed_at",
            "updated_at",
        } <= {column["name"] for column in inspector.get_columns("transactional_email_outbox")}
        assert {
            "ix_transactional_email_outbox_due",
            "ix_transactional_email_outbox_user_requested",
            "ix_transactional_email_outbox_message_kind",
        } <= {index["name"] for index in inspector.get_indexes("transactional_email_outbox")}

        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            values = {
                "id": "outbox-migration-delivery",
                "message_kind": "password_reset",
                "user_id": "outbox-migration-user",
                "password_reset_token_id": "outbox-migration-reset",
                "encrypted_payload": "opaque-ciphertext-only",
                "status": "queued",
                "attempt_count": 0,
                "max_attempts": 5,
                "next_attempt_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": None,
                "requested_at": now,
                "started_at": None,
                "sent_at": None,
                "completed_at": None,
                "updated_at": now,
            }
            connection.execute(outbox.insert(), values)
            with pytest.raises(IntegrityError):
                connection.execute(
                    outbox.insert(),
                    {**values, "id": "outbox-migration-duplicate"},
                )
    finally:
        engine.dispose()
