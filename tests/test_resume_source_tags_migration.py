from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_resume_source_tags_migration_adds_workspace_bound_audit_and_projection_tables(
    tmp_path,
) -> None:
    """The upgrade keeps mailbox provenance queryable without weakening tenant FKs."""

    database_path = tmp_path / "resume-source-tags.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260803_0054")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert {
            "source_tags",
            "mailbox_source_tag_rules",
            "email_attachment_import_tags",
            "resume_source_tags",
        }.issubset(table_names)

        rule_indexes = inspector.get_indexes("mailbox_source_tag_rules")
        projection_indexes = inspector.get_indexes("resume_source_tags")
        assert any(
            index["column_names"]
            == ["organization_id", "mailbox_config_id", "enabled", "priority"]
            for index in rule_indexes
        )
        assert any(
            index["column_names"] == ["organization_id", "source_tag_id", "resume_id"]
            for index in projection_indexes
        )

        rule_foreign_keys = {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys("mailbox_source_tag_rules")
        }
        projection_foreign_keys = {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys("resume_source_tags")
        }
        assert ("mailbox_config_id", "organization_id") in rule_foreign_keys
        assert ("source_tag_id", "organization_id") in rule_foreign_keys
        assert ("resume_id", "organization_id") in projection_foreign_keys
        assert ("source_tag_id", "organization_id") in projection_foreign_keys
    finally:
        engine.dispose()

    command.downgrade(config, "20260803_0054")

    engine = create_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
        assert "source_tags" not in table_names
        assert "mailbox_source_tag_rules" not in table_names
        assert "email_attachment_import_tags" not in table_names
        assert "resume_source_tags" not in table_names
    finally:
        engine.dispose()
