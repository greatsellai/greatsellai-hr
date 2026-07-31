from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.models import Resume
from test_resume_flow import create_candidate, make_pdf_with_text


def _upload_resume(client: TestClient, *, filename: str = "candidate-resume.pdf") -> tuple[str, bytes]:
    content = make_pdf_with_text("Python engineer resume " * 8)
    candidate_id = create_candidate(client)
    response = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": (filename, content, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["resume_id"], content


def _storage_key(client: TestClient, resume_id: str) -> str:
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        return resume.storage_key


def test_original_pdf_is_served_inline_with_the_original_filename(client: TestClient) -> None:
    resume_id, content = _upload_resume(client)
    storage_key = _storage_key(client, resume_id)

    response = client.get(f"/v1/resumes/{resume_id}/original-file")

    assert response.status_code == 200, response.text
    assert response.content == content
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith("inline;")
    assert "candidate-resume.pdf" in response.headers["content-disposition"]
    assert storage_key not in response.headers["content-disposition"]


def test_original_pdf_returns_clear_not_found_errors(client: TestClient) -> None:
    missing_resume = client.get("/v1/resumes/not-a-real-resume/original-file")
    assert missing_resume.status_code == 404
    assert missing_resume.json()["detail"] == "resume_not_found"

    resume_id, _ = _upload_resume(client)
    storage_key = _storage_key(client, resume_id)
    (client.app.state.settings.upload_dir / storage_key).unlink()

    missing_file = client.get(f"/v1/resumes/{resume_id}/original-file")
    assert missing_file.status_code == 404
    assert missing_file.json()["detail"] == "resume_original_file_not_found"


def test_original_pdf_does_not_follow_a_tampered_storage_path(client: TestClient) -> None:
    resume_id, content = _upload_resume(client)
    external_pdf = client.app.state.settings.data_dir.parent / "external.pdf"
    external_pdf.write_bytes(content)

    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        resume.storage_key = "../external.pdf"
        session.commit()

    response = client.get(f"/v1/resumes/{resume_id}/original-file")

    assert response.status_code == 404
    assert response.json()["detail"] == "resume_original_file_not_found"
    assert response.content != content


def test_original_file_requires_a_named_authenticated_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        session_secret="original-file-authentication-test-secret",
        allow_unauthenticated=False,
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app) as protected_client:
        response = protected_client.get("/v1/resumes/not-a-real-resume/original-file")

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication_required"
