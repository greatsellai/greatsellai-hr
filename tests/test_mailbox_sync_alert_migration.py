from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_mailbox_sync_alert_upgrade_and_downgrade(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "mailbox-sync-alerts.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("RESUME_V3_DATABASE_URL", database_url)
    config = Config("alembic.ini")

    command.upgrade(config, "20260721_0024")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "mailbox_sync_failure_alerts" in inspector.get_table_names()
        indexes = {
            index["name"]
            for index in inspector.get_indexes("mailbox_sync_failure_alerts")
        }
        assert "ix_mailbox_sync_failure_alerts_organization_state" in indexes
    finally:
        engine.dispose()

    command.downgrade(config, "20260721_0024")

    engine = create_engine(database_url)
    try:
        assert "mailbox_sync_failure_alerts" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
