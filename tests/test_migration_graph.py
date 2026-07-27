from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import (
    RecruitingAgentCandidateSet,
    RecruitingAgentCandidateSetItem,
    RecruitingAgentConversation,
    RecruitingAgentConversationTurn,
)


def test_alembic_history_has_one_canonical_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260727_0041"]


def test_recruiting_agent_context_ddl_identifiers_fit_postgresql() -> None:
    """Do not let SQLite hide PostgreSQL's 63-byte identifier limit."""

    dialect = postgresql.dialect()
    for table in (
        RecruitingAgentConversation.__table__,
        RecruitingAgentConversationTurn.__table__,
        RecruitingAgentCandidateSet.__table__,
        RecruitingAgentCandidateSetItem.__table__,
    ):
        CreateTable(table).compile(dialect=dialect)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)
