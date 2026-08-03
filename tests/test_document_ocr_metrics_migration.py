from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_document_ocr_metrics_upgrade_and_downgrade_are_sqlite_safe(tmp_path) -> None:
    """The additive aggregate table must not rebuild historical CV tables."""

    database_path = tmp_path / "document-ocr-metrics.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260731_0053")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_name = "document_extraction_ocr_daily_metrics"
        assert table_name in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert {
            "metric_date",
            "document_kind",
            "document_count",
            "total_source_pages",
            "ocr_attempted_page_count",
            "ocr_successful_page_count",
            "ocr_selected_page_count",
            "ocr_failed_page_count",
        }.issubset(columns)
        primary_key = inspector.get_pk_constraint(table_name)
        assert primary_key["constrained_columns"] == ["metric_date", "document_kind"]
    finally:
        engine.dispose()

    command.downgrade(config, "20260731_0053")
    engine = create_engine(database_url)
    try:
        assert "document_extraction_ocr_daily_metrics" not in inspect(
            engine
        ).get_table_names()
    finally:
        engine.dispose()
