from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Resume
from test_resume_flow import make_pdf_with_text


def _upload_new_resume(
    client,
    *,
    content: bytes,
    filename: str,
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/v1/resumes/upload",
        files={"file": (filename, content, "application/pdf")},
        headers=headers,
    )


def test_combined_upload_idempotency_replays_the_original_upload(client) -> None:
    content = make_pdf_with_text("Python SQL FastAPI " * 20)
    headers = {"Idempotency-Key": "batch-upload-001"}

    first = _upload_new_resume(
        client,
        content=content,
        filename="first-name.pdf",
        headers=headers,
    )
    second = _upload_new_resume(
        client,
        content=content,
        filename="retry-with-another-name.pdf",
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()

    queue = client.get("/v1/resumes/review-queue")
    assert queue.status_code == 200, queue.text
    assert queue.json()["total"] == 1
    assert queue.json()["items"] == [
        {
            "resume_id": first.json()["resume_id"],
            "candidate_id": first.json()["candidate_id"],
            "candidate_display_name": None,
                "original_filename": "first-name.pdf",
            "extraction_status": first.json()["extraction_status"],
            "ai_extraction_status": first.json()["ai_extraction_status"],
            "ai_extraction_error": first.json()["ai_extraction_error"],
            "candidate_name_extraction_status": first.json()[
                "candidate_name_extraction_status"
            ],
            "candidate_name_extraction_error": first.json()[
                "candidate_name_extraction_error"
            ],
            "ai_summary_status": first.json()["ai_summary_status"],
            "ai_summary_error": first.json()["ai_summary_error"],
            "quality_flags": first.json()["quality_flags"],
            "created_at": queue.json()["items"][0]["created_at"],
        }
    ]
    assert len(list(client.app.state.settings.upload_dir.rglob("*.pdf"))) == 1


def test_combined_upload_idempotency_rejects_different_pdf_without_orphan(client) -> None:
    headers = {"Idempotency-Key": "batch-upload-002"}
    first = _upload_new_resume(
        client,
        content=make_pdf_with_text("first document " * 30),
        filename="first.pdf",
        headers=headers,
    )
    conflict = _upload_new_resume(
        client,
        content=make_pdf_with_text("different document " * 30),
        filename="different.pdf",
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "idempotency_key_reused_with_different_pdf"
    assert len(list(client.app.state.settings.upload_dir.rglob("*.pdf"))) == 1
    assert client.get("/v1/resumes/review-queue").json()["total"] == 1


def test_combined_upload_removes_pdf_when_transaction_commit_fails(
    client,
    monkeypatch,
) -> None:
    def fail_commit(self: Session) -> None:
        raise IntegrityError("forced transaction failure", {}, RuntimeError("forced"))

    monkeypatch.setattr(Session, "commit", fail_commit)

    response = _upload_new_resume(
        client,
        content=make_pdf_with_text("transaction cleanup " * 30),
        filename="cleanup.pdf",
        headers={"Idempotency-Key": "batch-upload-003"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "database_conflict"
    assert list(client.app.state.settings.upload_dir.rglob("*.pdf")) == []
    assert list(client.app.state.settings.upload_dir.rglob("*.uploading")) == []


def test_review_queue_requires_auth_and_paginates_non_active_uploads(
    protected_client,
) -> None:
    unauthenticated = protected_client.get("/v1/resumes/review-queue")
    assert unauthenticated.status_code == 401

    headers = {"X-Admin-Token": "test-admin-token"}
    uploaded: list[str] = []
    for index, name in enumerate(("First", "Second", "Third"), start=1):
        response = _upload_new_resume(
            protected_client,
            content=make_pdf_with_text(f"resume {index} " * 30),
            filename=f"resume-{index}.pdf",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        uploaded.append(response.json()["resume_id"])

    database = protected_client.app.state.database
    base_time = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    with database.session_factory() as session:
        for index, resume_id in enumerate(uploaded):
            resume = session.get(Resume, resume_id)
            assert resume is not None
            resume.created_at = base_time + timedelta(minutes=index)
        first_resume = session.get(Resume, uploaded[0])
        assert first_resume is not None
        first_resume.is_active = True
        session.commit()

    first_page = protected_client.get(
        "/v1/resumes/review-queue?page=1&page_size=1",
        headers=headers,
    )
    second_page = protected_client.get(
        "/v1/resumes/review-queue?page=2&page_size=1",
        headers=headers,
    )

    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    first_payload = first_page.json()
    second_payload = second_page.json()
    assert first_payload["total"] == 2
    assert first_payload["page"] == 1
    assert first_payload["page_size"] == 1
    assert first_payload["items"][0]["resume_id"] == uploaded[2]
    assert first_payload["items"][0]["candidate_display_name"] is None
    assert first_payload["items"][0]["original_filename"] == "resume-3.pdf"
    assert first_payload["items"][0]["created_at"].startswith("2026-07-16T10:02:00")
    assert second_payload["items"][0]["resume_id"] == uploaded[1]
    assert second_payload["items"][0]["candidate_display_name"] is None
