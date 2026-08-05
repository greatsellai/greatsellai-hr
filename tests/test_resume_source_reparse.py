from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from app.models import (
    Resume,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    ResumeFactSnapshot,
    ResumeSourceBlock,
    ResumeSourceTag,
    SourceTag,
)
from app.schemas import ResumeFactsSubmission
from app.services import ai_extraction_job_service as job_service
from app.services import document_extraction_job_service
from app.services import resume_service
from app.services.institution_service import load_registry
from app.services.text_extraction import ExtractedPage, PdfExtractionResult
from test_resume_flow import create_candidate, upload_text_resume


def test_reparse_replaces_source_evidence_and_resets_inactive_job(
    client,
    monkeypatch,
) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    database = client.app.state.database
    inline_parse_calls: list[object] = []
    original_resolver = resume_service.resolve_uploaded_resume_path

    def inline_parse_must_not_run(*_args: object, **_kwargs: object) -> None:
        inline_parse_calls.append(True)
        raise AssertionError("source reparse service must not open or parse the original")

    monkeypatch.setattr(
        resume_service,
        "resolve_uploaded_resume_path",
        inline_parse_must_not_run,
    )
    monkeypatch.setattr(
        document_extraction_job_service,
        "extract_document_text",
        lambda *args, **kwargs: PdfExtractionResult(
            source_page_count=1,
            parsed_page_count=1,
            pages=[
                ExtractedPage(
                    page_no=1,
                    text="OCR recovered education skills project experience",
                    non_whitespace_chars=48,
                )
            ],
            raw_text="--- PAGE 1 ---\nOCR recovered education skills project experience",
            quality_flags=[],
            parser_version="pypdf-test+tencent-ocr",
        ),
    )

    with database.session_factory() as session:
        resume = resume_service.reparse_inactive_resume_source_text(
            session,
            resume_id=resume_id,
            settings=client.app.state.settings,
        )
        session.commit()

        document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert document_job is not None
        assert document_job.status == "queued"

    assert inline_parse_calls == []
    monkeypatch.setattr(
        resume_service,
        "resolve_uploaded_resume_path",
        original_resolver,
    )
    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="inactive-reparse-document-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )

    assert resume.extraction_status == "text_ready"
    assert resume.parser_version == "pypdf-test+tencent-ocr"
    assert resume.facts_version == 1
    assert block is not None
    assert block.text == "OCR recovered education skills project experience"
    assert job is not None
    assert job.status == "unavailable"
    assert job.attempt_count == 0


def _ready_source_resume(client) -> str:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    database = client.app.state.database
    source_text = (
        "Education Test University Computer Science Bachelor. "
        "Work Experience Example Company Python Engineer 2022-07 to 2024-06. "
        "Skills Python SQL."
    )
    with database.session_factory() as session:
        page = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert page is not None
        page.text = source_text
        session.commit()

    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v1",
                "education": [
                    {
                        "school_name_raw": "Test University",
                        "degree": "bachelor",
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "experiences": [
                    {
                        "experience_type": "employment",
                        "organization_name_raw": "Example Company",
                        "title_raw": "Python Engineer",
                        "start_month": "2022-07",
                        "end_month": "2024-06",
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]},
                    {"skill_display": "SQL", "evidence_block_ids": ["page-001"]},
                ],
            },
            "complete_review": True,
            "review_note": "Test source verified.",
            "is_985_211_override": False,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["is_active"] is True
    return resume_id


def test_active_source_reparse_creates_isolated_new_resume_version(
    client,
    monkeypatch,
) -> None:
    source_resume_id = _ready_source_resume(client)
    database = client.app.state.database
    recovered_text = (
        "Education Test University Computer Science Bachelor. "
        "Work Experience Example Company Python Engineer. Skills Python SQL."
    )
    inline_parse_calls: list[object] = []

    def inline_parse_must_not_run(*_args: object, **_kwargs: object) -> PdfExtractionResult:
        inline_parse_calls.append(True)
        raise AssertionError("reparse HTTP/service path must not parse the original")

    monkeypatch.setattr(
        resume_service,
        "extract_document_text",
        inline_parse_must_not_run,
        raising=False,
    )
    monkeypatch.setattr(
        document_extraction_job_service,
        "extract_document_text",
        lambda *args, **kwargs: PdfExtractionResult(
            source_page_count=1,
            parsed_page_count=1,
            pages=[
                ExtractedPage(
                    page_no=1,
                    text=recovered_text,
                    non_whitespace_chars=len(recovered_text.replace(" ", "")),
                )
            ],
            raw_text=f"--- PAGE 1 ---\\n{recovered_text}",
            quality_flags=[],
            parser_version="pymupdf-test",
        ),
    )

    with database.session_factory() as session:
        source_before = session.get(Resume, source_resume_id)
        assert source_before is not None
        original_storage_key = source_before.storage_key
        original_facts_version = source_before.facts_version
        original_snapshot = session.scalar(
            select(ResumeFactSnapshot).where(
                ResumeFactSnapshot.resume_id == source_resume_id,
                ResumeFactSnapshot.facts_version == original_facts_version,
            )
        )
        original_page = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == source_resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert original_snapshot is not None
        assert original_page is not None
        original_snapshot_json = original_snapshot.canonical_facts_json
        original_page_text = original_page.text

        replacement = resume_service.reparse_active_resume_as_new_version(
            session,
            resume_id=source_resume_id,
            settings=client.app.state.settings,
        )
        replacement_id = replacement.id
        replacement_storage_key = replacement.storage_key
        session.commit()

    assert inline_parse_calls == []
    with database.session_factory() as session:
        pending_document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == replacement_id
            )
        )
        pending_blocks = session.scalars(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == replacement_id
            )
        ).all()
        pending_ai_job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == replacement_id
            )
        )
    assert pending_document_job is not None
    assert pending_document_job.status == "queued"
    assert pending_blocks == []
    assert pending_ai_job is None

    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="source-reparse-document-worker",
    )

    with database.session_factory() as session:
        source_after = session.get(Resume, source_resume_id)
        replacement_after = session.get(Resume, replacement_id)
        source_page_after = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == source_resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        replacement_page = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == replacement_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        source_snapshot_after = session.scalar(
            select(ResumeFactSnapshot).where(
                ResumeFactSnapshot.resume_id == source_resume_id,
                ResumeFactSnapshot.facts_version == original_facts_version,
            )
        )
        replacement_job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == replacement_id
            )
        )

    assert source_after is not None
    assert replacement_after is not None
    assert source_page_after is not None
    assert replacement_page is not None
    assert source_snapshot_after is not None
    assert replacement_job is not None
    assert source_after.is_active is True
    assert source_after.extraction_status == "ready"
    assert "source_text_unreliable" in source_after.quality_flags
    assert source_after.facts_version == original_facts_version
    assert source_after.storage_key == original_storage_key
    assert source_page_after.text == original_page_text
    assert source_snapshot_after.canonical_facts_json == original_snapshot_json
    assert replacement_after.id != source_after.id
    assert replacement_after.storage_key != source_after.storage_key
    assert replacement_after.sha256 == source_after.sha256
    assert replacement_after.is_active is False
    assert replacement_after.extraction_status == "text_ready"
    assert replacement_after.facts_version == 0
    assert replacement_after.parser_version == "pymupdf-test"
    assert replacement_page.text == recovered_text
    assert replacement_job.status == "unavailable"
    assert replacement_job.input_facts_version == 0
    upload_dir = client.app.state.settings.upload_dir
    assert (upload_dir / original_storage_key).is_file()
    assert (upload_dir / replacement_storage_key).is_file()


def test_active_source_reparse_endpoint_creates_a_new_version(
    client,
    monkeypatch,
) -> None:
    source_resume_id = _ready_source_resume(client)
    recovered_text = "Recovered source text with sufficient content for a resume."
    inline_parse_calls: list[object] = []

    def inline_parse_must_not_run(*_args: object, **_kwargs: object) -> PdfExtractionResult:
        inline_parse_calls.append(True)
        raise AssertionError("reparse endpoint must not parse the original")

    monkeypatch.setattr(
        resume_service,
        "extract_document_text",
        inline_parse_must_not_run,
        raising=False,
    )
    monkeypatch.setattr(
        document_extraction_job_service,
        "extract_document_text",
        lambda *args, **kwargs: PdfExtractionResult(
            source_page_count=1,
            parsed_page_count=1,
            pages=[
                ExtractedPage(
                    page_no=1,
                    text=recovered_text,
                    non_whitespace_chars=len(recovered_text.replace(" ", "")),
                )
            ],
            raw_text=recovered_text,
            quality_flags=[],
            parser_version="pymupdf-test",
        ),
    )

    response = client.post(f"/v1/resumes/{source_resume_id}/reparse-source")
    assert response.status_code == 200, response.text
    replacement = response.json()
    assert replacement["resume_id"] != source_resume_id
    assert replacement["candidate_id"]
    assert replacement["is_active"] is False
    assert replacement["extraction_status"] == "queued"
    assert inline_parse_calls == []

    database = client.app.state.database
    with database.session_factory() as session:
        source = session.get(Resume, source_resume_id)
        clone = session.get(Resume, replacement["resume_id"])
        clone_document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == replacement["resume_id"]
            )
        )
        clone_blocks = session.scalars(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == replacement["resume_id"]
            )
        ).all()
    assert source is not None
    assert clone is not None
    assert clone_document_job is not None
    assert clone_document_job.status == "queued"
    assert clone_blocks == []
    assert source.is_active is True
    assert "source_text_unreliable" in source.quality_flags
    assert clone.storage_key != source.storage_key

    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="source-reparse-endpoint-document-worker",
    )
    completed = client.get(f"/v1/resumes/{replacement['resume_id']}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["extraction_status"] == "text_ready"


def test_reparse_clone_auto_activates_only_after_new_grounded_ai_facts(
    ai_client,
    monkeypatch,
) -> None:
    source_resume_id = _ready_source_resume(ai_client)
    database = ai_client.app.state.database
    school = load_registry().institutions[0].canonical_name
    recovered_text = f"Education {school} Computer Science Skills Python SQL."
    monkeypatch.setattr(
        document_extraction_job_service,
        "extract_document_text",
        lambda *args, **kwargs: PdfExtractionResult(
            source_page_count=1,
            parsed_page_count=1,
            pages=[
                ExtractedPage(
                    page_no=1,
                    text=recovered_text,
                    non_whitespace_chars=len(recovered_text.replace(" ", "")),
                )
            ],
            raw_text=recovered_text,
            quality_flags=[],
            parser_version="pymupdf-test",
        ),
    )

    created = ai_client.post(f"/v1/resumes/{source_resume_id}/reparse-source")
    assert created.status_code == 200, created.text
    replacement_id = created.json()["resume_id"]
    with database.session_factory() as session:
        source_job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == source_resume_id
            )
        )
        assert source_job is not None
        assert source_job.status == "needs_attention"
        assert source_job.last_error == "source_reparse_superseded_ai_extraction"

    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="source-reparse-document-worker",
    )

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "education": [
                    {
                        "school_name_raw": school,
                        "degree": "bachelor",
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]},
                    {"skill_display": "SQL", "evidence_block_ids": ["page-001"]},
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="source-reparse-worker",
    )

    source = ai_client.get(f"/v1/resumes/{source_resume_id}")
    replacement = ai_client.get(f"/v1/resumes/{replacement_id}")
    assert source.status_code == 200, source.text
    assert replacement.status_code == 200, replacement.text
    assert source.json()["is_active"] is False
    assert "source_text_unreliable" in source.json()["quality_flags"]
    assert replacement.json()["is_active"] is True
    assert replacement.json()["extraction_status"] == "ready"
    assert replacement.json()["ai_extraction_status"] == "completed"
    assert "source_text_unreliable" not in replacement.json()["quality_flags"]


def test_active_source_reparse_keeps_submission_source_after_auto_activation(
    ai_client,
    monkeypatch,
) -> None:
    """A repaired email resume must remain visible through its source tag."""

    source_resume_id = _ready_source_resume(ai_client)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        source = session.get(Resume, source_resume_id)
        assert source is not None
        source.ingestion_source_type = "mailbox_attachment"
        source.source_mailbox_label_snapshot = "Recruiting inbox"
        source_tag = SourceTag(
            organization_id=source.organization_id,
            display_name="Platform X",
            name_key="platform-x",
            sort_order=10,
        )
        session.add(source_tag)
        session.flush()
        session.add(
            ResumeSourceTag(
                organization_id=source.organization_id,
                resume_id=source.id,
                source_tag_id=source_tag.id,
                tag_name_snapshot=source_tag.display_name,
                source_count=3,
            )
        )
        session.commit()
        source_tag_id = source_tag.id

    school = load_registry().institutions[0].canonical_name
    recovered_text = f"Education {school} Computer Science Skills Python SQL."
    monkeypatch.setattr(
        document_extraction_job_service,
        "extract_document_text",
        lambda *args, **kwargs: PdfExtractionResult(
            source_page_count=1,
            parsed_page_count=1,
            pages=[
                ExtractedPage(
                    page_no=1,
                    text=recovered_text,
                    non_whitespace_chars=len(recovered_text.replace(" ", "")),
                )
            ],
            raw_text=recovered_text,
            quality_flags=[],
            parser_version="pymupdf-test",
        ),
    )
    created = ai_client.post(f"/v1/resumes/{source_resume_id}/reparse-source")
    assert created.status_code == 200, created.text
    replacement_id = created.json()["resume_id"]

    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="source-reparse-source-tags-document-worker",
    )

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "education": [
                    {
                        "school_name_raw": school,
                        "degree": "bachelor",
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="source-reparse-source-tags-ai-worker",
    )

    replacement = ai_client.get(f"/v1/resumes/{replacement_id}")
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["is_active"] is True
    assert replacement.json()["source_mailbox_label"] == "Recruiting inbox"
    assert replacement.json()["source_tags"] == [
        {"source_tag_id": source_tag_id, "display_name": "Platform X"}
    ]
    with database.session_factory() as session:
        clone = session.get(Resume, replacement_id)
        copied_source_tag = session.scalar(
            select(ResumeSourceTag).where(ResumeSourceTag.resume_id == replacement_id)
        )
    assert clone is not None
    assert clone.ingestion_source_type == "mailbox_attachment"
    assert clone.source_mailbox_label_snapshot == "Recruiting inbox"
    assert copied_source_tag is not None
    assert copied_source_tag.source_tag_id == source_tag_id
    assert copied_source_tag.source_count == 3
    # The original mail event remains tied to the historic source version so
    # deleting that archived version cannot be blocked by this repair clone.
    assert copied_source_tag.first_import_id is None
    assert copied_source_tag.last_import_id is None

    filtered = ai_client.post(
        "/v1/candidates/search",
        json={"limit": 20, "source_tag_ids_any_of": [source_tag_id]},
    )
    assert filtered.status_code == 200, filtered.text
    assert {item["resume_id"] for item in filtered.json()["items"]} == {replacement_id}


def test_active_source_reparse_rejects_second_pending_clone(
    client,
) -> None:
    source_resume_id = _ready_source_resume(client)
    database = client.app.state.database
    # The document job itself is enough to make a second request conflict;
    # AI work is intentionally not enqueued before source normalization.
    queued_settings = replace(client.app.state.settings, deepseek_api_key="test-key")
    with database.session_factory() as session:
        first = resume_service.reparse_active_resume_as_new_version(
            session,
            resume_id=source_resume_id,
            settings=queued_settings,
        )
        assert first.document_extraction_job is not None
        assert first.document_extraction_job.status == "queued"
        assert first.ai_extraction_job is None
        try:
            resume_service.reparse_active_resume_as_new_version(
                session,
                resume_id=source_resume_id,
                settings=queued_settings,
            )
        except resume_service.ResumeServiceError as exc:
            assert str(exc) == "source_resume_reparse_already_running"
        else:  # pragma: no cover - defensive failure message for the contract
            raise AssertionError("expected duplicate source-reparse protection")
        session.rollback()


def test_reparse_clone_activation_guard_requires_unchanged_active_source(
    client,
) -> None:
    source_resume_id = _ready_source_resume(client)
    database = client.app.state.database
    queued_settings = replace(client.app.state.settings, deepseek_api_key="test-key")
    with database.session_factory() as session:
        replacement = resume_service.reparse_active_resume_as_new_version(
            session,
            resume_id=source_resume_id,
            settings=queued_settings,
        )
        assert resume_service.reparse_clone_auto_activation_allowed(
            session,
            resume=replacement,
        )

        source = session.get(Resume, source_resume_id)
        assert source is not None
        source.is_active = False
        assert not resume_service.reparse_clone_auto_activation_allowed(
            session,
            resume=replacement,
        )
        session.rollback()
