from __future__ import annotations

import json
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
import sqlalchemy as sa


def test_workspace_ai_import_settings_upgrade_and_downgrade_are_sqlite_safe(
    tmp_path,
) -> None:
    """The additive per-workspace settings table must migrate cleanly on SQLite."""

    database_path = tmp_path / "workspace-ai-import-settings.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260805_0057")
    command.upgrade(config, "20260806_0063")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_name = "workspace_ai_import_settings"
        assert table_name in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert {
            "id",
            "organization_id",
            "auto_summary_enabled",
            "auto_score_enabled",
            "score_template_ids",
            "trigger_manual_upload",
            "trigger_mailbox_import",
            "updated_by_user_id",
            "created_at",
            "updated_at",
        }.issubset(columns)
        unique_constraints = inspector.get_unique_constraints(table_name)
        assert "uq_workspace_ai_import_settings_organization" in {
            constraint["name"] for constraint in unique_constraints
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260805_0057")
    engine = create_engine(database_url)
    try:
        assert "workspace_ai_import_settings" not in inspect(
            engine
        ).get_table_names()
    finally:
        engine.dispose()


def test_workspace_ai_import_single_template_backfilled_to_array(tmp_path) -> None:
    """0062 must migrate a single-template row into the JSON array and back."""

    database_path = tmp_path / "workspace-ai-import-backfill.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260806_0061")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workspace_ai_import_settings "
                "(id, organization_id, auto_summary_enabled, auto_score_enabled, "
                " default_score_template_id, trigger_manual_upload, "
                " trigger_mailbox_import, created_at, updated_at) "
                "VALUES (:id, :org, 1, 1, :tid, 1, 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": "settings-one", "org": "org-one", "tid": "template-one"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO workspace_ai_import_settings "
                "(id, organization_id, auto_summary_enabled, auto_score_enabled, "
                " trigger_manual_upload, trigger_mailbox_import, "
                " created_at, updated_at) "
                "VALUES (:id, :org, 1, 1, 1, 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": "settings-two", "org": "org-two"},
        )
    engine.dispose()

    command.upgrade(config, "20260806_0063")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT score_template_ids FROM workspace_ai_import_settings "
                    "WHERE id = 'settings-one'"
                )
            ).one()
            assert json.loads(row[0]) == ["template-one"]
            row_two = connection.execute(
                sa.text(
                    "SELECT score_template_ids FROM workspace_ai_import_settings "
                    "WHERE id = 'settings-two'"
                )
            ).one()
            assert json.loads(row_two[0]) == []
    finally:
        engine.dispose()

    command.downgrade(config, "20260806_0061")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            restored = connection.execute(
                sa.text(
                    "SELECT default_score_template_id FROM workspace_ai_import_settings "
                    "WHERE id = 'settings-one'"
                )
            ).one()
            assert restored[0] == "template-one"
            restored_two = connection.execute(
                sa.text(
                    "SELECT default_score_template_id FROM workspace_ai_import_settings "
                    "WHERE id = 'settings-two'"
                )
            ).one()
            assert restored_two[0] is None
    finally:
        engine.dispose()
