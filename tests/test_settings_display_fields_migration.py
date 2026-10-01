from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_user_filter_display_preferences_upgrade_and_downgrade_are_sqlite_safe(
    tmp_path,
) -> None:
    """The additive per-user filter preference table must migrate cleanly on SQLite."""

    database_path = tmp_path / "user-filter-display-preferences.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260806_0059")
    command.upgrade(config, "20260806_0063")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_name = "user_filter_display_preferences"
        assert table_name in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert {
            "id",
            "organization_id",
            "user_id",
            "display_field_keys",
            "filter_section_keys",
            "updated_at",
        }.issubset(columns)
        unique_constraints = inspector.get_unique_constraints(table_name)
        assert "uq_user_filter_display_preferences_user_org" in {
            constraint["name"] for constraint in unique_constraints
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260806_0059")
    engine = create_engine(database_url)
    try:
        assert "user_filter_display_preferences" not in inspect(
            engine
        ).get_table_names()
    finally:
        engine.dispose()


def test_user_filter_section_preferences_column_round_trips(tmp_path) -> None:
    """The filter_section_keys column appears at head and drops on downgrade."""

    database_path = tmp_path / "user-filter-section-preferences.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260806_0062")
    command.upgrade(config, "20260806_0063")

    engine = create_engine(database_url)
    try:
        columns = {
            column["name"]
            for column in inspect(engine).get_columns(
                "user_filter_display_preferences"
            )
        }
        assert "filter_section_keys" in columns
    finally:
        engine.dispose()

    command.downgrade(config, "20260806_0062")
    engine = create_engine(database_url)
    try:
        columns = {
            column["name"]
            for column in inspect(engine).get_columns(
                "user_filter_display_preferences"
            )
        }
        assert "filter_section_keys" not in columns
    finally:
        engine.dispose()
