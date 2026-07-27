from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.models import RecruitingAgentConversation


def test_profile_context_migration_upgrades_existing_agent_conversations(tmp_path) -> None:
    database_path = tmp_path / "agent-profile-context.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260727_0039")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("recruiting_agent_conversations")
        }
        assert {
            "active_talent_profile_id",
            "active_talent_profile_revision_id",
        }.issubset(columns)
        indexes = {
            index["name"]
            for index in inspector.get_indexes("recruiting_agent_conversations")
        }
        assert {
            "ix_agent_conv_active_talent_profile",
            "ix_agent_conv_active_talent_profile_revision",
        }.issubset(indexes)
    finally:
        engine.dispose()


def test_profile_context_indexes_compile_for_postgresql_identifier_limits() -> None:
    """The migration and ORM use explicit names below PostgreSQL's 63-char cap."""

    profile_index_names = {
        "ix_agent_conv_active_talent_profile",
        "ix_agent_conv_active_talent_profile_revision",
    }
    indexes = [
        index
        for index in RecruitingAgentConversation.__table__.indexes
        if index.name in profile_index_names
    ]
    assert {index.name for index in indexes} == profile_index_names
    for index in indexes:
        assert index.name is not None
        assert len(index.name) <= 63
        # This raises IdentifierError for names PostgreSQL cannot deploy.
        str(CreateIndex(index).compile(dialect=postgresql.dialect()))
