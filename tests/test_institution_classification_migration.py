from __future__ import annotations

from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_institution_classification_migration_corrects_legacy_985_211_rows(
    tmp_path,
) -> None:
    database_path = tmp_path / "institution-classification-migration.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260721_0025")
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        institutions = Table("institutions", metadata, autoload_with=engine)
        educations = Table("resume_educations", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                institutions.insert(),
                {
                    "id": "institution-985",
                    "roster_id": "cn-985-test",
                    "canonical_name": "School A",
                    "canonical_key": "schoola",
                    "is_985_211": True,
                    "tier_tags": ["211", "985"],
                    "registry_version": "legacy",
                },
            )
            connection.execute(
                educations.insert(),
                [
                    {
                        "id": "education-985",
                        "resume_id": "resume-a",
                        "school_name_raw": "School A",
                        "school_key": "schoola",
                        "institution_id": "institution-985",
                        "school_match_state": "exact",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                        "institution_tiers": ["211", "985"],
                    },
                    {
                        "id": "education-211",
                        "resume_id": "resume-b",
                        "school_name_raw": "School B",
                        "school_key": "schoolb",
                        "institution_id": None,
                        "school_match_state": "legacy",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-002"],
                        "institution_tiers": ["211"],
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        institutions = Table("institutions", metadata, autoload_with=engine)
        educations = Table("resume_educations", metadata, autoload_with=engine)
        with engine.connect() as connection:
            school_a = connection.execute(
                select(
                    educations.c.institution_tiers,
                    educations.c.institution_classification,
                    educations.c.classification_basis,
                    educations.c.classification_registry_version,
                    educations.c.classification_evidence_block_ids,
                ).where(educations.c.id == "education-985")
            ).one()
            school_b = connection.execute(
                select(
                    educations.c.institution_tiers,
                    educations.c.institution_classification,
                ).where(educations.c.id == "education-211")
            ).one()
            institution_tags = connection.execute(
                select(institutions.c.tier_tags).where(
                    institutions.c.id == "institution-985"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert school_a.institution_tiers == ["985"]
    assert school_a.institution_classification == "985"
    assert school_a.classification_basis == "moe_985_211_registry"
    assert school_a.classification_registry_version == "moe-985-211-2005-2006.v1"
    assert school_a.classification_evidence_block_ids == ["page-001"]
    assert school_b.institution_tiers == ["211"]
    assert school_b.institution_classification == "211"
    assert institution_tags == ["985"]
