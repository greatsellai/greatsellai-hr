from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import (
    MailboxConfig,
    MailboxOAuthConnectIntent,
    RecruitingAgentCandidateSet,
    RecruitingAgentCandidateSetItem,
    RecruitingAgentConversation,
    RecruitingAgentConversationTurn,
    ResumeSummaryJob,
    TalentSearchRun,
)


def test_alembic_history_has_one_canonical_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260728_0045"]


def test_recruiting_agent_context_ddl_identifiers_fit_postgresql() -> None:
    """Do not let SQLite hide PostgreSQL's 63-byte identifier limit."""

    dialect = postgresql.dialect()
    for table in (
        RecruitingAgentConversation.__table__,
        RecruitingAgentConversationTurn.__table__,
        RecruitingAgentCandidateSet.__table__,
        RecruitingAgentCandidateSetItem.__table__,
        TalentSearchRun.__table__,
    ):
        CreateTable(table).compile(dialect=dialect)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_mailbox_initial_sync_backfill_ddl_identifiers_fit_postgresql() -> None:
    """The new mailbox checks must remain valid under PostgreSQL's 63-byte limit."""

    dialect = postgresql.dialect()
    for table in (MailboxConfig.__table__, MailboxOAuthConnectIntent.__table__):
        CreateTable(table).compile(dialect=dialect)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_resume_summary_job_ddl_identifiers_fit_postgresql() -> None:
    """Automatic-summary queue identifiers must fit PostgreSQL's 63-byte limit."""

    dialect = postgresql.dialect()
    CreateTable(ResumeSummaryJob.__table__).compile(dialect=dialect)
    for index in ResumeSummaryJob.__table__.indexes:
        CreateIndex(index).compile(dialect=dialect)
