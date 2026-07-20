from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_mailbox_background_jobs_upgrade_from_retention_schema(tmp_path, monkeypatch) -> None:
    """The production upgrade adds queue storage without rebuilding old mail data."""

    database_path = tmp_path / "mailbox-background-jobs.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("RESUME_V3_DATABASE_URL", database_url)
    config = Config("alembic.ini")

    command.upgrade(config, "20260720_0020")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "mailbox_background_jobs" in inspector.get_table_names()
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("mailbox_background_jobs")
        }
        assert {
            "ix_mailbox_background_jobs_claim",
            "uq_mailbox_background_jobs_active_sync",
            "uq_mailbox_background_jobs_active_attachment_retry",
        }.issubset(indexes)
    finally:
        engine.dispose()

    command.downgrade(config, "20260720_0020")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert "mailbox_background_jobs" not in inspector.get_table_names()
    finally:
        engine.dispose()
