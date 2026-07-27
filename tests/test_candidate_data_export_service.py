from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import (
    Candidate,
    CandidateDataAuditEvent,
    CandidateDataExport,
    Resume,
    ResumeFactSnapshot,
    ResumeSummary,
)
from app.services.candidate_data_export_service import (
    CandidateDataExportError,
    authorize_candidate_data_export_download,
    cancel_candidate_data_export,
    cleanup_expired_candidate_data_exports,
    create_candidate_data_export,
    resolve_candidate_data_export_download,
    run_candidate_data_export_worker_once,
)
from app.services import candidate_data_export_service
from app.tenant_scope import LEGACY_ORGANIZATION_ID


def _seed_exportable_candidate(client) -> tuple[str, str]:
    database = client.app.state.database
    settings = client.app.state.settings
    storage_key = f"{LEGACY_ORGANIZATION_ID}/candidate-data-export-fixture.pdf"
    source_path = settings.upload_dir / storage_key
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"candidate-data-export-original")

    facts = {
        "schema_version": "resume_fact_snapshot.v4",
        "source_blocks": ["must-not-export"],
        "source_block_ids": ["page-001"],
        "derived": {
            "highest_degree": "bachelor",
            "is_985_211": False,
            "employment_months": 24,
            "employment_or_internship_months": 30,
        },
        "education": [
            {
                "school_name_raw": "Example University",
                "evidence_block_ids": ["page-001"],
            }
        ],
        "skills": [
            {
                "skill_display": "=FormulaLikeSkill",
                "evidence_block_ids": ["page-001"],
            }
        ],
    }
    canonical_facts = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    with database.session_factory() as session:
        candidate = Candidate(display_name="=FormulaLikeName")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="private-resume.pdf",
            storage_key=storage_key,
            sha256="a" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="test",
            is_active=True,
            facts_version=1,
            contact_details=[
                {
                    "kind": "email",
                    "value": "export-contact@example.test",
                    "evidence_block_ids": ["page-001"],
                }
            ],
        )
        session.add(resume)
        session.flush()
        snapshot = ResumeFactSnapshot(
            resume_id=resume.id,
            facts_version=1,
            canonical_facts_json=canonical_facts,
            facts_sha256=hashlib.sha256(canonical_facts.encode("utf-8")).hexdigest(),
            source_block_ids=["page-001"],
            created_by="test",
        )
        session.add(snapshot)
        session.flush()
        summary = ResumeSummary(
            resume_id=resume.id,
            fact_snapshot_id=snapshot.id,
            facts_version=1,
            content={"summary": "safe", "raw_text": "must-not-export"},
            source="ai",
            is_current=True,
            status="succeeded",
            model_name="test",
        )
        session.add(summary)
        session.commit()
        return candidate.id, resume.id


def test_export_worker_builds_safe_archive_and_download_grant(client) -> None:
    candidate_id, _ = _seed_exportable_candidate(client)
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        created = create_candidate_data_export(
            session,
            settings=settings,
            candidate_ids=[candidate_id],
            include_originals=False,
            actor_user_id=None,
        )
        session.commit()

    assert run_candidate_data_export_worker_once(
        database, settings=settings, worker_id="candidate-export-test"
    )

    with database.session_factory() as session:
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None
        assert export.status == "completed"
        assert export.output_storage_key is not None
        granted = authorize_candidate_data_export_download(
            session,
            settings=settings,
            export_id=export.id,
            actor_user_id=None,
            session_nonce="candidate-data-export-test-session-nonce",
        )
        session.commit()
        resolved = resolve_candidate_data_export_download(
            session,
            settings=settings,
            opaque_token=granted.token,
            actor_user_id=None,
            session_nonce="candidate-data-export-test-session-nonce",
        )
        assert resolved.path.is_file()
        assert resolved.filename.endswith(".zip")
        audit_count = session.scalar(
            select(func.count(CandidateDataAuditEvent.id)).where(
                CandidateDataAuditEvent.action == "candidate_data_export_download_authorized"
            )
        )
        assert audit_count == 1
        archive_path = resolved.path

    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) >= {
            "candidates.csv",
            "candidates.xlsx",
            "facts.json",
            "contacts.json",
            "summaries.json",
            "scores.json",
            "job_matches.json",
            "manifest.json",
        }
        assert not any(name.startswith("originals/") for name in archive.namelist())
        facts = archive.read("facts.json").decode("utf-8")
        contacts = json.loads(archive.read("contacts.json").decode("utf-8"))
        summaries = archive.read("summaries.json").decode("utf-8")
        csv_text = archive.read("candidates.csv").decode("utf-8-sig")
        assert "must-not-export" not in facts
        assert contacts[0]["contacts"] == [
            {"kind": "email", "value": "export-contact@example.test"}
        ]
        assert all(
            set(contact) == {"kind", "value"}
            for document in contacts
            for contact in document["contacts"]
        )
        assert "must-not-export" not in summaries
        assert "private-resume.pdf" not in archive.read("manifest.json").decode("utf-8")
        assert "'=FormulaLikeName" in csv_text
        assert "'=FormulaLikeSkill" in csv_text

    with database.session_factory() as session:
        cancel_candidate_data_export(
            session,
            export_id=created.export_id,
            actor_user_id=None,
        )
        session.commit()
        with pytest.raises(CandidateDataExportError, match="candidate_data_export_download_not_found"):
            resolve_candidate_data_export_download(
                session,
                settings=settings,
                opaque_token=granted.token,
                actor_user_id=None,
                session_nonce="candidate-data-export-test-session-nonce",
            )

    assert cleanup_expired_candidate_data_exports(database, settings=settings) == 1
    assert not archive_path.exists()
    # A completed cleanup is not selected forever; otherwise the worker would
    # spin without sleeping once one old export exists.
    assert cleanup_expired_candidate_data_exports(database, settings=settings) == 0


def test_export_worker_includes_originals_only_when_explicitly_selected(client) -> None:
    candidate_id, _ = _seed_exportable_candidate(client)
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        created = create_candidate_data_export(
            session,
            settings=settings,
            candidate_ids=[candidate_id],
            include_originals=True,
            actor_user_id=None,
        )
        session.commit()

    assert run_candidate_data_export_worker_once(
        database, settings=settings, worker_id="candidate-export-originals-test"
    )
    with database.session_factory() as session:
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None
        assert export.status == "completed"
        assert export.output_storage_key is not None
        granted = authorize_candidate_data_export_download(
            session,
            settings=settings,
            export_id=export.id,
            actor_user_id=None,
            session_nonce="candidate-data-export-originals-test-session-nonce",
        )
        session.commit()
        archive_path = resolve_candidate_data_export_download(
            session,
            settings=settings,
            opaque_token=granted.token,
            actor_user_id=None,
            session_nonce="candidate-data-export-originals-test-session-nonce",
        ).path

    with zipfile.ZipFile(archive_path) as archive:
        original_paths = [name for name in archive.namelist() if name.startswith("originals/")]
        assert len(original_paths) == 1
        assert "private-resume.pdf" not in original_paths[0]
        assert archive.read(original_paths[0]) == b"candidate-data-export-original"


def test_revoked_export_is_never_claimed_by_worker(client) -> None:
    candidate_id, _ = _seed_exportable_candidate(client)
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        created = create_candidate_data_export(
            session,
            settings=settings,
            candidate_ids=[candidate_id],
            include_originals=False,
            actor_user_id=None,
        )
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None
        export.status = "revoked"
        export.revoked_at = datetime.now(timezone.utc)
        session.commit()

    assert not run_candidate_data_export_worker_once(
        database, settings=settings, worker_id="candidate-export-revoked-test"
    )


def test_export_output_uses_the_shared_upload_volume_not_container_local_data_dir(
    client,
) -> None:
    """A worker archive must remain readable by a separately mounted API."""

    candidate_id, _ = _seed_exportable_candidate(client)
    database = client.app.state.database
    api_settings = client.app.state.settings
    worker_settings = replace(
        api_settings,
        data_dir=api_settings.data_dir / "simulated-worker-local-data",
        # This is the production invariant: both services mount upload_dir.
        upload_dir=api_settings.upload_dir,
    )
    with database.session_factory() as session:
        created = create_candidate_data_export(
            session,
            settings=api_settings,
            candidate_ids=[candidate_id],
            include_originals=False,
            actor_user_id=None,
        )
        session.commit()

    assert run_candidate_data_export_worker_once(
        database,
        settings=worker_settings,
        worker_id="candidate-export-shared-volume-test",
    )
    with database.session_factory() as session:
        granted = authorize_candidate_data_export_download(
            session,
            settings=api_settings,
            export_id=created.export_id,
            actor_user_id=None,
            session_nonce="candidate-data-export-shared-volume-session",
        )
        session.commit()
        resolved = resolve_candidate_data_export_download(
            session,
            settings=api_settings,
            opaque_token=granted.token,
            actor_user_id=None,
            session_nonce="candidate-data-export-shared-volume-session",
        )
        assert resolved.path.is_file()
        assert resolved.path.is_relative_to(api_settings.upload_dir)


def test_export_cleanup_retains_retryable_storage_pointer_when_unlink_fails(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient unlink error must not orphan a candidate-data ZIP."""

    candidate_id, _ = _seed_exportable_candidate(client)
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        created = create_candidate_data_export(
            session,
            settings=settings,
            candidate_ids=[candidate_id],
            include_originals=False,
            actor_user_id=None,
        )
        session.commit()
    assert run_candidate_data_export_worker_once(
        database,
        settings=settings,
        worker_id="candidate-export-cleanup-retry-test",
    )

    with database.session_factory() as session:
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None and export.output_storage_key is not None
        storage_key = export.output_storage_key
        archive_path = candidate_data_export_service.resolve_candidate_data_export_path(
            settings,
            organization_id=export.organization_id,
            export_id=export.id,
            storage_key=storage_key,
        )
        export.status = "revoked"
        export.revoked_at = datetime.now(timezone.utc)
        export.expires_at = datetime.now(timezone.utc)
        session.commit()

    real_resolver = candidate_data_export_service.resolve_candidate_data_export_path

    class FailingPath:
        def unlink(self, *, missing_ok: bool = False) -> None:
            raise OSError("synthetic export cleanup failure")

    monkeypatch.setattr(
        candidate_data_export_service,
        "resolve_candidate_data_export_path",
        lambda *args, **kwargs: FailingPath(),
    )
    assert cleanup_expired_candidate_data_exports(database, settings=settings) == 0
    assert archive_path.exists()
    with database.session_factory() as session:
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None
        assert export.output_storage_key == storage_key
        assert export.next_attempt_at is not None
        assert export.last_error == "candidate_data_export_cleanup_retryable"

    monkeypatch.setattr(
        candidate_data_export_service,
        "resolve_candidate_data_export_path",
        real_resolver,
    )
    with database.session_factory() as session:
        export = session.scalar(
            select(CandidateDataExport).where(CandidateDataExport.id == created.export_id)
        )
        assert export is not None
        export.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert cleanup_expired_candidate_data_exports(database, settings=settings) == 1
    assert not archive_path.exists()
    assert cleanup_expired_candidate_data_exports(database, settings=settings) == 0
