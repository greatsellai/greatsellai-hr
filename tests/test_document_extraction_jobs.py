from __future__ import annotations

import hashlib
from io import BytesIO
from dataclasses import replace
from datetime import timedelta
import zipfile

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.config import AppSettings
from app.database import Database
from app.models import (
    Candidate,
    Organization,
    Resume,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    ResumeSourceBlock,
)
from app.services import document_extraction_job_service as job_service
from app.services.document_text_extraction import (
    DocumentExtractionError,
    extract_document_text,
)
from app.services.text_extraction import ExtractedPage, PdfExtractionResult
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    set_organization_context,
)
from test_resume_flow import make_pdf_with_text


def _minimal_docx_bytes() -> bytes:
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", "<w:document></w:document>")
    return content.getvalue()


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    workbook.active.append(["Candidate", "Python"])
    content = BytesIO()
    workbook.save(content)
    return content.getvalue()


def test_upload_only_persists_original_and_document_job_before_worker_runs(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def extraction_must_not_run(*_args: object, **_kwargs: object) -> PdfExtractionResult:
        calls.append(True)
        raise AssertionError("upload HTTP request must not parse an original")

    monkeypatch.setattr(job_service, "extract_document_text", extraction_must_not_run)
    response = ai_client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("Candidate Python SQL " * 20),
                "application/pdf",
            )
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["extraction_status"] == "queued"
    assert payload["ai_extraction_status"] == "queued"
    assert calls == []

    database = ai_client.app.state.database
    with database.session_factory() as session:
        document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == payload["resume_id"]
            )
        )
        assert document_job is not None
        assert document_job.status == "queued"
        assert session.scalars(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == payload["resume_id"]
            )
        ).all() == []
        assert session.scalars(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == payload["resume_id"]
            )
        ).all() == []

    premature_ai_request = ai_client.post(
        f"/v1/resumes/{payload['resume_id']}/queue-ai-extraction"
    )
    assert premature_ai_request.status_code == 409, premature_ai_request.text
    assert (
        premature_ai_request.json()["detail"]
        == "resume_document_extraction_in_progress"
    )


@pytest.mark.parametrize(
    ("filename", "content", "media_type"),
    [
        ("candidate.docx", _minimal_docx_bytes(), "application/octet-stream"),
        (
            "candidate.xlsx",
            _xlsx_bytes(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("candidate.html", b"<html><body>Candidate</body></html>", "text/html"),
        (
            "candidate.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
            "image/png",
        ),
    ],
)
def test_supported_non_pdf_uploads_only_queue_the_document_worker(
    client,
    filename: str,
    content: bytes,
    media_type: str,
) -> None:
    response = client.post(
        "/v1/resumes/upload",
        files={"file": (filename, content, media_type)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["extraction_status"] == "queued"
    assert payload["ai_extraction_status"] == "queued"
    with client.app.state.database.session_factory() as session:
        resume = session.get(Resume, payload["resume_id"])
        document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == payload["resume_id"]
            )
        )
        assert resume is not None
        assert resume.original_filename == filename
        assert resume.source_blocks == []
        assert document_job is not None
        assert document_job.status == "queued"


def test_document_worker_persists_source_blocks_then_queues_ai_job(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = PdfExtractionResult(
        source_page_count=1,
        parsed_page_count=1,
        pages=[
            ExtractedPage(
                page_no=1,
                text="Candidate\x00 Python SQL",
                non_whitespace_chars=18,
            )
        ],
        raw_text="--- PAGE 1 ---\nCandidate\x00 Python SQL",
        quality_flags=[],
        parser_version="document-worker-test",
    )
    monkeypatch.setattr(job_service, "extract_document_text", lambda *_a, **_k: result)
    response = ai_client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("candidate source"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]

    assert job_service.run_document_extraction_worker_once(
        ai_client.app.state.database,
        settings=ai_client.app.state.settings,
        worker_id="document-worker-test",
    )

    with ai_client.app.state.database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        assert resume.extraction_status == "text_ready"
        assert "\x00" not in (resume.raw_text or "")
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        assert "\x00" not in block.text
        document_job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert document_job is not None
        assert document_job.status == "completed"
        ai_job = session.scalar(
            select(ResumeAiExtractionJob).where(ResumeAiExtractionJob.resume_id == resume_id)
        )
        assert ai_job is not None
        assert ai_job.status == "queued"


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("renamed.png", b"not a PNG"),
        ("renamed.docx", b"PK\x03\x04not-an-office-package"),
        ("renamed.html", b"not html"),
    ],
)
def test_upload_rejects_extension_spoofs_before_persisting_original(
    client,
    filename: str,
    content: bytes,
) -> None:
    response = client.post(
        "/v1/resumes/upload",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "invalid_document_signature"
    with client.app.state.database.session_factory() as session:
        assert session.scalars(select(Resume)).all() == []
        assert session.scalars(select(ResumeDocumentExtractionJob)).all() == []
    assert list(client.app.state.settings.upload_dir.rglob("*")) == []


def test_document_worker_rechecks_signature_before_opening_a_parser(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("candidate source"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]
    parser_calls: list[object] = []

    def parser_must_not_run(*_args: object, **_kwargs: object) -> PdfExtractionResult:
        parser_calls.append(True)
        raise AssertionError("invalid stored bytes must fail before parsing")

    monkeypatch.setattr(job_service, "extract_document_text", parser_must_not_run)
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        tampered = b"not actually a PDF"
        (client.app.state.settings.upload_dir / resume.storage_key).write_bytes(tampered)
        # Simulate a legacy/manual storage repair that updated the database
        # digest but bypassed browser-upload validation. The worker must still
        # reject the format before it invokes a parser.
        resume.sha256 = hashlib.sha256(tampered).hexdigest()
        session.commit()

    assert job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="document-signature-worker",
    )
    assert parser_calls == []
    detail = client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "failed"
    assert detail.json()["quality_flags"] == ["invalid_document_signature"]


def test_document_worker_enforces_pdf_page_quota_with_explainable_failure(client) -> None:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    content = BytesIO()
    writer.write(content)
    response = client.post(
        "/v1/resumes/upload",
        files={"file": ("two-pages.pdf", content.getvalue(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    settings = replace(client.app.state.settings, document_max_pages=1)
    assert job_service.run_document_extraction_worker_once(
        client.app.state.database,
        settings=settings,
        worker_id="document-page-limit-worker",
    )
    detail = client.get(f"/v1/resumes/{response.json()['resume_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "failed"
    assert detail.json()["quality_flags"] == ["document_page_limit_exceeded"]


def test_retryable_document_timeout_retries_then_becomes_actionable(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_service,
        "extract_document_text",
        lambda *_a, **_k: (_ for _ in ()).throw(
            DocumentExtractionError("image_ocr_timed_out")
        ),
    )
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("timeout fixture"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]
    database = client.app.state.database
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        job.max_attempts = 2
        session.commit()

    assert job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="document-retry-worker",
    )
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert job.last_error == "image_ocr_timed_out"
        job.next_attempt_at = job_service.utcnow() - timedelta(seconds=1)
        session.commit()

    assert job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="document-retry-worker",
    )
    detail = client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "failed"
    assert detail.json()["quality_flags"] == ["image_ocr_timed_out"]


def test_expired_terminal_document_lease_marks_its_owned_resume_failed(client) -> None:
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("lease fixture"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert resume is not None
        assert job is not None
        resume.extraction_status = "extracting"
        job.status = "running"
        job.attempt_count = 1
        job.max_attempts = 1
        job.lease_owner = "crashed-worker"
        job.lease_expires_at = job_service.utcnow() - timedelta(seconds=1)
        session.commit()

    assert not job_service.run_document_extraction_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="recovery-worker",
    )
    detail = client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "failed"
    assert detail.json()["quality_flags"] == [
        "document_extraction_worker_lease_expired"
    ]
    with database.session_factory() as session:
        recovered = session.scalar(
            select(ResumeDocumentExtractionJob).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        )
        assert recovered is not None
        assert recovered.status == "needs_attention"
        assert recovered.last_error == "document_extraction_worker_lease_expired"


def test_spreadsheet_row_limit_is_enforced_before_text_is_persisted(tmp_path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Candidate", "Python"])
    sheet.append(["Candidate", "SQL"])
    path = tmp_path / "candidate.xlsx"
    workbook.save(path)

    with pytest.raises(DocumentExtractionError, match="spreadsheet_row_limit_exceeded"):
        extract_document_text(
            path,
            min_text_chars_per_page=1,
            ocr_sparse_text_chars_per_page=1,
            max_pages=10,
            max_text_chars=10_000,
            max_archive_uncompressed_bytes=10_000_000,
            max_spreadsheet_sheets=5,
            max_spreadsheet_rows_per_sheet=1,
            max_spreadsheet_cells=100,
        )


def test_document_worker_refuses_cross_workspace_resume_reference(tmp_path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        min_text_chars_per_page=1,
    )
    database = Database(settings.database_url)
    database.create_all()
    try:
        with database.session_factory() as session:
            organization_a = Organization(name="Document worker A")
            organization_b = Organization(name="Document worker B")
            session.add_all((organization_a, organization_b))
            session.flush()
            set_organization_context(session, organization_b.id)
            foreign_candidate = Candidate(display_name="Foreign candidate")
            session.add(foreign_candidate)
            session.flush()
            foreign_resume = Resume(
                candidate_id=foreign_candidate.id,
                original_filename="foreign.pdf",
                storage_key="foreign.pdf",
                sha256="a" * 64,
                source_page_count=0,
                parsed_page_count=0,
                extraction_status="queued",
                quality_flags=[],
                parser_version="fixture",
                is_active=False,
            )
            session.add(foreign_resume)
            session.flush()
            clear_organization_context(session)
            set_organization_context(session, organization_a.id)
            cross_workspace_job = ResumeDocumentExtractionJob(
                resume_id=foreign_resume.id,
                status="queued",
                max_attempts=1,
                next_attempt_at=job_service.utcnow() - timedelta(seconds=1),
            )
            session.add(cross_workspace_job)
            session.commit()
            cross_job_id = cross_workspace_job.id

        assert job_service.run_document_extraction_worker_once(
            database,
            settings=settings,
            worker_id="document-scope-worker",
        )
        with database.session_factory() as session:
            with bypass_organization_scope(session):
                failed = session.get(ResumeDocumentExtractionJob, cross_job_id)
                untouched = session.get(Resume, foreign_resume.id)
            assert failed is not None
            assert failed.organization_id == organization_a.id
            assert failed.status == "needs_attention"
            assert failed.last_error == "resume_not_found"
            assert untouched is not None
            assert untouched.organization_id == organization_b.id
            assert untouched.extraction_status == "queued"
    finally:
        database.dispose()
