from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_ai_gateway_migration_upgrades_a_file_sqlite_database(tmp_path) -> None:
    database_path = tmp_path / "ai-gateway-migration.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "ai_provider_profiles",
            "ai_model_profiles",
            "ai_model_price_versions",
            "ai_route_policies",
            "ai_route_policy_versions",
            "ai_runs",
            "api_invocations",
        }.issubset(tables)
        foreign_keys = inspector.get_foreign_keys("api_invocations")
        assert any(
            foreign_key["constrained_columns"] == ["ai_run_id", "organization_id"]
            and foreign_key["referred_table"] == "ai_runs"
            and foreign_key["referred_columns"] == ["id", "organization_id"]
            for foreign_key in foreign_keys
        )
        assert any(
            foreign_key["constrained_columns"] == ["fallback_of_id", "organization_id"]
            and foreign_key["referred_table"] == "api_invocations"
            and foreign_key["referred_columns"] == ["id", "organization_id"]
            for foreign_key in foreign_keys
        )
    finally:
        engine.dispose()
