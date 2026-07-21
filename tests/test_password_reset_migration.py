from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select
from sqlalchemy.exc import IntegrityError


def test_password_reset_migration_invalidates_legacy_active_links_before_unique_index(
    tmp_path,
) -> None:
    """A pre-0028 database can contain two unused reset links per user."""

    database_path = tmp_path / "password-reset-migration.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260721_0027")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        tokens = Table("password_reset_tokens", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                {
                    "id": "password-reset-migration-user",
                    "email": "migration-reset@example.test",
                    "email_key": "migration-reset@example.test",
                    "full_name": "Migration Reset User",
                    "password_hash": "legacy-password-hash",
                    "is_active": True,
                    "is_platform_admin": False,
                    "email_verified_at": now,
                    "last_login_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                tokens.insert(),
                [
                    {
                        "id": "password-reset-migration-old",
                        "user_id": "password-reset-migration-user",
                        "token_digest": "a" * 64,
                        "expires_at": now + timedelta(hours=1),
                        "used_at": None,
                        "requested_at": now - timedelta(minutes=1),
                    },
                    {
                        "id": "password-reset-migration-new",
                        "user_id": "password-reset-migration-user",
                        "token_digest": "b" * 64,
                        "expires_at": now + timedelta(hours=1),
                        "used_at": None,
                        "requested_at": now,
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        tokens = Table("password_reset_tokens", metadata, autoload_with=engine)
        with engine.begin() as connection:
            migrated = connection.execute(
                select(tokens.c.id, tokens.c.invalidated_at).where(
                    tokens.c.user_id == "password-reset-migration-user"
                )
            ).all()
            assert {row.id for row in migrated} == {
                "password-reset-migration-old",
                "password-reset-migration-new",
            }
            assert all(row.invalidated_at is not None for row in migrated)

            connection.execute(
                tokens.insert(),
                {
                    "id": "password-reset-migration-current",
                    "user_id": "password-reset-migration-user",
                    "token_digest": "c" * 64,
                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                    "used_at": None,
                    "invalidated_at": None,
                    "requested_at": datetime.now(timezone.utc),
                },
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    tokens.insert(),
                    {
                        "id": "password-reset-migration-duplicate",
                        "user_id": "password-reset-migration-user",
                        "token_digest": "d" * 64,
                        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                        "used_at": None,
                        "invalidated_at": None,
                        "requested_at": datetime.now(timezone.utc),
                    },
                )
    finally:
        engine.dispose()
