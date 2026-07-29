from __future__ import annotations

import ast
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import (
    CandidateNameExtractionJob,
    MailboxConfig,
    MailboxOAuthConnectIntent,
    RecruitingAgentCandidateSet,
    RecruitingAgentCandidateSetItem,
    RecruitingAgentConversation,
    RecruitingAgentConversationTurn,
    ResumeSummaryJob,
    RuntimeWorkerHeartbeat,
    TalentSearchRun,
    WorkspaceFeedbackImageAttachment,
    WorkspaceFeedbackSubmission,
)


def test_alembic_history_has_one_canonical_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))

    assert script.get_heads() == ["20260729_0049"]


def test_migration_revision_identifiers_are_unique() -> None:
    """Alembic can silently mask duplicate revision IDs during graph loading."""

    migration_directory = Path("migrations/versions")
    seen_revisions: dict[str, Path] = {}
    for migration_path in sorted(migration_directory.glob("*.py")):
        module = ast.parse(migration_path.read_text(encoding="utf-8"))
        revision = next(
            (
                statement.value.value
                for statement in module.body
                if isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "revision"
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ),
            None,
        )
        assert revision is not None, f"missing revision ID: {migration_path}"
        assert revision not in seen_revisions, (
            f"duplicate Alembic revision {revision}: "
            f"{seen_revisions[revision]} and {migration_path}"
        )
        seen_revisions[revision] = migration_path


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


def test_candidate_name_extraction_job_ddl_identifiers_fit_postgresql() -> None:
    """Name-only task identifiers must remain valid in production PostgreSQL."""

    dialect = postgresql.dialect()
    CreateTable(CandidateNameExtractionJob.__table__).compile(dialect=dialect)
    for index in CandidateNameExtractionJob.__table__.indexes:
        CreateIndex(index).compile(dialect=dialect)


def test_workspace_feedback_reward_ddl_identifiers_fit_postgresql() -> None:
    """Feedback queue constraints must remain portable to production PostgreSQL."""

    dialect = postgresql.dialect()
    for table in (
        WorkspaceFeedbackSubmission.__table__,
        WorkspaceFeedbackImageAttachment.__table__,
    ):
        CreateTable(table).compile(dialect=dialect)
        for index in table.indexes:
            CreateIndex(index).compile(dialect=dialect)


def test_runtime_observability_ddl_identifiers_fit_postgresql() -> None:
    """Durable worker liveness identifiers must fit production PostgreSQL."""

    dialect = postgresql.dialect()
    CreateTable(RuntimeWorkerHeartbeat.__table__).compile(dialect=dialect)
    for index in RuntimeWorkerHeartbeat.__table__.indexes:
        CreateIndex(index).compile(dialect=dialect)
