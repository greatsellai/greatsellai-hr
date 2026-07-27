from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import RecruitingAgentConversationTurn


def test_chat_memory_migration_upgrades_existing_agent_conversations(tmp_path) -> None:
    database_path = tmp_path / "agent-chat-memory.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260727_0040")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("recruiting_agent_conversation_turns")
        }
        assert {
            "id",
            "organization_id",
            "conversation_id",
            "context_version",
            "user_message",
            "assistant_message",
            "created_at",
        }.issubset(columns)
        indexes = {
            index["name"]
            for index in inspector.get_indexes("recruiting_agent_conversation_turns")
        }
        assert {
            "ix_agent_turn_org_conversation_version",
            "ix_recruiting_agent_conversation_turns_organization_id",
            "ix_recruiting_agent_conversation_turns_conversation_id",
        }.issubset(indexes)
        foreign_keys = inspector.get_foreign_keys("recruiting_agent_conversation_turns")
        assert any(
            foreign_key["referred_table"] == "recruiting_agent_conversations"
            and foreign_key["constrained_columns"]
            == ["conversation_id", "organization_id"]
            for foreign_key in foreign_keys
        )
    finally:
        engine.dispose()


def test_chat_memory_ddl_identifiers_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    table = RecruitingAgentConversationTurn.__table__
    str(CreateTable(table).compile(dialect=dialect))
    for index in table.indexes:
        assert index.name is not None
        assert len(index.name) <= 63
        str(CreateIndex(index).compile(dialect=dialect))
