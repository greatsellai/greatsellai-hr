from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_candidate_name_job_migration_queues_only_live_active_unnamed_sources(
    tmp_path,
) -> None:
    database_path = tmp_path / "candidate-name-jobs.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "20260728_0046")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        candidates = Table("candidates", metadata, autoload_with=engine)
        resumes = Table("resumes", metadata, autoload_with=engine)
        source_blocks = Table("resume_source_blocks", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        organization_id = "candidate-name-migration-org"
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                {
                    "id": organization_id,
                    "name": "Candidate name migration workspace",
                    "plan_status": "trial",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            candidate_rows = [
                {
                    "id": "name-job-active-blank-candidate",
                    "organization_id": organization_id,
                    "display_name": "   ",
                    "deleted_at": None,
                    "created_at": now,
                },
                {
                    "id": "name-job-inactive-candidate",
                    "organization_id": organization_id,
                    "display_name": None,
                    "deleted_at": None,
                    "created_at": now,
                },
                {
                    "id": "name-job-named-candidate",
                    "organization_id": organization_id,
                    "display_name": "Existing migration fixture",
                    "deleted_at": None,
                    "created_at": now,
                },
                {
                    "id": "name-job-no-source-candidate",
                    "organization_id": organization_id,
                    "display_name": None,
                    "deleted_at": None,
                    "created_at": now,
                },
                {
                    "id": "name-job-not-ready-candidate",
                    "organization_id": organization_id,
                    "display_name": None,
                    "deleted_at": None,
                    "created_at": now,
                },
                {
                    "id": "name-job-deleted-candidate",
                    "organization_id": organization_id,
                    "display_name": None,
                    "deleted_at": now,
                    "created_at": now,
                },
            ]
            connection.execute(candidates.insert(), candidate_rows)
            resume_rows = [
                {
                    "id": "name-job-active-blank-resume",
                    "candidate_id": "name-job-active-blank-candidate",
                    "is_active": True,
                },
                {
                    "id": "name-job-inactive-resume",
                    "candidate_id": "name-job-inactive-candidate",
                    "is_active": False,
                },
                {
                    "id": "name-job-named-resume",
                    "candidate_id": "name-job-named-candidate",
                    "is_active": True,
                },
                {
                    "id": "name-job-no-source-resume",
                    "candidate_id": "name-job-no-source-candidate",
                    "is_active": True,
                },
                {
                    "id": "name-job-not-ready-resume",
                    "candidate_id": "name-job-not-ready-candidate",
                    "is_active": True,
                },
                {
                    "id": "name-job-deleted-resume",
                    "candidate_id": "name-job-deleted-candidate",
                    "is_active": True,
                },
            ]
            for offset, row in enumerate(resume_rows):
                row.update(
                    {
                        "organization_id": organization_id,
                        "original_filename": f"migration-{offset}.pdf",
                        "storage_key": f"migration/{offset}.pdf",
                        "sha256": f"{offset + 1:064x}",
                        "source_page_count": 1,
                        "parsed_page_count": 1,
                        "extraction_status": "ready",
                        "quality_flags": [],
                        "parser_version": "migration-test",
                        "employment_months": 0,
                        "employment_or_internship_months": 0,
                        "facts_version": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            next(
                row
                for row in resume_rows
                if row["id"] == "name-job-not-ready-resume"
            )["extraction_status"] = "needs_review"
            connection.execute(resumes.insert(), resume_rows)
            source_resume_ids = (
                "name-job-active-blank-resume",
                "name-job-inactive-resume",
                "name-job-named-resume",
                "name-job-not-ready-resume",
                "name-job-deleted-resume",
            )
            connection.execute(
                source_blocks.insert(),
                [
                    {
                        "id": f"name-job-block-{index}",
                        "resume_id": resume_id,
                        "block_id": "page-001",
                        "page_no": 1,
                        "block_type": "page_text",
                        "text": "Name: Migration Fixture",
                    }
                    for index, resume_id in enumerate(source_resume_ids)
                ],
            )

        command.upgrade(config, "head")
        metadata = MetaData()
        jobs = Table("candidate_name_extraction_jobs", metadata, autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    jobs.c.organization_id,
                    jobs.c.resume_id,
                    jobs.c.status,
                    jobs.c.attempt_count,
                    jobs.c.max_attempts,
                    jobs.c.next_attempt_at,
                    jobs.c.ai_route_policy_version_id,
                    jobs.c.last_error,
                )
            ).mappings().all()
        assert len(rows) == 1
        row = rows[0]
        assert {
            "organization_id": row["organization_id"],
            "resume_id": row["resume_id"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "ai_route_policy_version_id": row["ai_route_policy_version_id"],
            "last_error": row["last_error"],
        } == {
            "organization_id": organization_id,
            "resume_id": "name-job-active-blank-resume",
            "status": "queued",
            "attempt_count": 0,
            "max_attempts": 3,
            "ai_route_policy_version_id": None,
            "last_error": None,
        }
        assert row["next_attempt_at"] is not None
    finally:
        engine.dispose()
