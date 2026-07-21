from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_auth_session_version_migration_initializes_existing_accounts(tmp_path) -> None:
    database_path = tmp_path / "auth-session-version-migration.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260721_0028")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                users.insert(),
                {
                    "id": "auth-session-version-migration-user",
                    "email": "migration-session@example.test",
                    "email_key": "migration-session@example.test",
                    "full_name": "Migration Session User",
                    "password_hash": "legacy-password-hash",
                    "is_active": True,
                    "is_platform_admin": False,
                    "email_verified_at": now,
                    "last_login_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        users = Table("user_accounts", metadata, autoload_with=engine)
        assert "auth_session_version" in {
            column["name"] for column in inspect(engine).get_columns("user_accounts")
        }
        with engine.connect() as connection:
            version = connection.execute(
                select(users.c.auth_session_version).where(
                    users.c.id == "auth-session-version-migration-user"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert version == 1
