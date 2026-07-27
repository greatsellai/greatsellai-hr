from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_contact_details_migration_backfills_existing_source_blocks(tmp_path) -> None:
    database_path = tmp_path / "resume-contact-details.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "20260724_0036")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        candidates = Table("candidates", metadata, autoload_with=engine)
        resumes = Table("resumes", metadata, autoload_with=engine)
        source_blocks = Table("resume_source_blocks", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                {
                    "id": "contact-migration-organization",
                    "name": "Contact migration workspace",
                    "plan_status": "trial",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                candidates.insert(),
                {
                    "id": "contact-migration-candidate",
                    "organization_id": "contact-migration-organization",
                    "display_name": "Contact migration fixture",
                    "created_at": now,
                },
            )
            connection.execute(
                resumes.insert(),
                {
                    "id": "contact-migration-resume",
                    "organization_id": "contact-migration-organization",
                    "candidate_id": "contact-migration-candidate",
                    "original_filename": "fixture.pdf",
                    "storage_key": "contact-migration/fixture.pdf",
                    "sha256": "a" * 64,
                    "source_page_count": 1,
                    "parsed_page_count": 1,
                    "extraction_status": "text_ready",
                    "quality_flags": [],
                    "parser_version": "migration-test",
                    "is_active": False,
                    "employment_months": 0,
                    "employment_or_internship_months": 0,
                    "facts_version": 0,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                source_blocks.insert(),
                {
                    "id": "contact-migration-block",
                    "resume_id": "contact-migration-resume",
                    "block_id": "page-001",
                    "page_no": 1,
                    "block_type": "page_text",
                    "text": (
                        "Candidate 138 0000 0000 candidate@example.test "
                        "+1 415 555 2671 0086 139-0013-8000"
                    ),
                },
            )

        command.upgrade(config, "head")
        metadata = MetaData()
        resumes = Table("resumes", metadata, autoload_with=engine)
        assert "contact_details" in {
            column["name"] for column in inspect(engine).get_columns("resumes")
        }
        with engine.connect() as connection:
            details = connection.execute(
                select(resumes.c.contact_details).where(
                    resumes.c.id == "contact-migration-resume"
                )
            ).scalar_one()
        assert details == [
            {
                "kind": "phone",
                "value": "13800000000",
                "evidence_block_ids": ["page-001"],
            },
            {
                "kind": "email",
                "value": "candidate@example.test",
                "evidence_block_ids": ["page-001"],
            },
            {
                "kind": "phone",
                "value": "+14155552671",
                "evidence_block_ids": ["page-001"],
            },
            {
                "kind": "phone",
                "value": "13900138000",
                "evidence_block_ids": ["page-001"],
            },
        ]
    finally:
        engine.dispose()
