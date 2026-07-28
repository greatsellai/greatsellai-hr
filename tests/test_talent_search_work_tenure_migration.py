from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_talent_search_work_tenure_migration_unifies_historic_profiles_and_runs(
    tmp_path,
) -> None:
    database_path = tmp_path / "talent-search-work-tenure.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260727_0043")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        revisions = Table(
            "talent_search_profile_revisions",
            metadata,
            autoload_with=engine,
        )
        runs = Table("talent_search_runs", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        revision_values = {
            "organization_id": "organization-work-tenure",
            "profile_id": "profile-work-tenure",
            "revision_number": 1,
            "source": "ai_generated",
            "status": "confirmed",
            "title": "Historic profile",
            "summary": "Historic summary",
            "verification_requirements": [],
            "preferred_requirements": [],
            "aliases": [],
            "clarifying_questions": [],
            "match_job_version_id": None,
            "model_name": None,
            "created_at": now,
            "confirmed_at": now,
            "confirmed_by_user_id": None,
        }
        run_values = {
            "organization_id": "organization-work-tenure",
            "profile_id": "profile-work-tenure",
            "revision_id": "revision-work-tenure-max",
            "scope_kind": "global",
            "scope_fingerprint": None,
            "scope_candidate_count": 0,
            "recall_diagnostics": {},
            "recalled_resume_ids": [],
            "status": "completed",
            "total_recalled_count": 0,
            "job_match_batch_id": None,
            "created_at": now,
            "updated_at": now,
        }
        with engine.begin() as connection:
            connection.execute(
                revisions.insert(),
                [
                    {
                        **revision_values,
                        "id": "revision-work-tenure-max",
                        "hard_filters": {
                            "min_employment_months": 36,
                            "min_employment_or_internship_months": 24,
                        },
                    },
                    {
                        **revision_values,
                        "id": "revision-work-tenure-legacy",
                        "revision_number": 2,
                        "hard_filters": {"min_employment_months": "48"},
                    },
                    {
                        **revision_values,
                        "id": "revision-work-tenure-null",
                        "revision_number": 3,
                        "hard_filters": {
                            "min_employment_months": None,
                            "min_employment_or_internship_months": 12,
                        },
                    },
                    {
                        **revision_values,
                        "id": "revision-work-tenure-current",
                        "revision_number": 4,
                        "hard_filters": {"min_employment_or_internship_months": 18},
                    },
                ],
            )
            connection.execute(
                runs.insert(),
                [
                    {
                        **run_values,
                        "id": "run-work-tenure-max",
                        "hard_filter_snapshot": {
                            "min_employment_months": 60,
                            "min_employment_or_internship_months": 42,
                        },
                    },
                    {
                        **run_values,
                        "id": "run-work-tenure-current",
                        "revision_id": "revision-work-tenure-current",
                        "hard_filter_snapshot": {"min_employment_or_internship_months": 18},
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        revisions = Table(
            "talent_search_profile_revisions",
            metadata,
            autoload_with=engine,
        )
        runs = Table("talent_search_runs", metadata, autoload_with=engine)
        with engine.connect() as connection:
            migrated_revisions = {
                row.id: row.hard_filters
                for row in connection.execute(
                    select(revisions.c.id, revisions.c.hard_filters)
                )
            }
            migrated_runs = {
                row.id: row.hard_filter_snapshot
                for row in connection.execute(
                    select(runs.c.id, runs.c.hard_filter_snapshot)
                )
            }
    finally:
        engine.dispose()

    assert migrated_revisions["revision-work-tenure-max"] == {
        "min_employment_or_internship_months": 36,
    }
    assert migrated_revisions["revision-work-tenure-legacy"] == {
        "min_employment_or_internship_months": 48,
    }
    assert migrated_revisions["revision-work-tenure-null"] == {
        "min_employment_or_internship_months": 12,
    }
    assert migrated_revisions["revision-work-tenure-current"] == {
        "min_employment_or_internship_months": 18,
    }
    assert migrated_runs["run-work-tenure-max"] == {
        "min_employment_or_internship_months": 60,
    }
    assert migrated_runs["run-work-tenure-current"] == {
        "min_employment_or_internship_months": 18,
    }
