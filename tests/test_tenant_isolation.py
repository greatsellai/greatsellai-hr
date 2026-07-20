from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Candidate,
    EmailAttachmentImport,
    Job,
    JobVersion,
    MailboxConfig,
    Resume,
    ResumeAiExtractionJob,
    ResumeScore,
    ResumeSummary,
    ScoreTemplate,
)
from app.tenant_scope import set_organization_context


# Tenant-auth contract under test:
# - POST /v1/auth/register creates a new organization and its first admin.
# - POST /v1/auth/login establishes a session for that user's organization.
# - All candidate and resume reads/writes are constrained by that session.
#
# These tests intentionally do not use the legacy unauthenticated fixture in
# tests/conftest.py: two cookie jars must share one database to exercise the
# tenant boundary.


def _pdf_with_text(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two independent browser sessions sharing an isolated test database."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="tenant-isolation-test-session-secret",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)

    # One active lifespan owns the shared in-memory database. Each plain client
    # retains its own cookie jar, which mirrors two separate browser sessions.
    with TestClient(app):
        client_a = TestClient(app)
        client_b = TestClient(app)
        try:
            yield client_a, client_b
        finally:
            client_a.close()
            client_b.close()


def _register_and_login(
    client: TestClient,
    *,
    organization_name: str,
    full_name: str,
    email: str,
    password: str,
) -> dict[str, object]:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": full_name,
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text

    provider = client.app.state.transactional_email_provider
    delivery = next(item for item in reversed(provider.deliveries) if item.recipient == email)
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text

    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text

    session = client.get("/v1/auth/session")
    assert session.status_code == 200, session.text
    payload = session.json()
    assert payload["authenticated"] is True
    assert payload["organization"]["name"] == organization_name
    assert payload["user"]["email"] == email
    return payload


def _create_candidate_and_resume(client: TestClient, *, display_name: str) -> tuple[str, str]:
    candidate = client.post("/v1/candidates", json={"display_name": display_name})
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["candidate_id"]

    uploaded = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={
            "file": (
                "tenant-fixture.pdf",
                _pdf_with_text("Tenant fixture Python SQL experience " * 8),
                "application/pdf",
            )
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    return candidate_id, uploaded.json()["resume_id"]


def _seed_workspace_b_private_resources(
    client: TestClient,
    *,
    organization_id: str,
) -> dict[str, str]:
    """Add minimal synthetic B-only resources without any external provider."""

    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)

        task_candidate = Candidate(display_name="Workspace B task fixture")
        session.add(task_candidate)
        session.flush()

        task_resume = Resume(
            candidate_id=task_candidate.id,
            original_filename="workspace-b-task-fixture.pdf",
            storage_key="workspace-b-task-fixture.pdf",
            sha256="b" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="tenant-test-fixture",
            raw_text="synthetic tenant scope fixture",
            is_active=False,
        )
        session.add(task_resume)
        session.flush()

        score_template = ScoreTemplate(
            name="Workspace B score fixture",
            description="synthetic tenant scope fixture",
            version=1,
            is_archived=False,
        )
        session.add(score_template)
        session.flush()

        score = ResumeScore(
            resume_id=task_resume.id,
            fact_snapshot_id=None,
            template_id=score_template.id,
            facts_version=0,
            template_version=1,
            total_score=0.0,
            ai_total_score=0.0,
            dimension_scores=[],
            analysis={},
            status="succeeded",
            model_name="tenant-test-fixture",
        )
        summary = ResumeSummary(
            resume_id=task_resume.id,
            fact_snapshot_id=None,
            facts_version=0,
            content={},
            source="manual",
            is_current=False,
            status="succeeded",
            model_name="tenant-test-fixture",
        )
        extraction_job = ResumeAiExtractionJob(
            resume_id=task_resume.id,
            job_kind="initial",
            status="queued",
            attempt_count=0,
            max_attempts=1,
            input_facts_version=0,
        )
        job = Job(
            title="Workspace B JD fixture",
            jd_text="Synthetic tenant-scoped JD fixture.",
            requirements={},
            version=1,
        )
        mailbox_config = MailboxConfig(
            imap_host="imap.fixture.invalid",
            imap_port=993,
            email_address="workspace-b-mailbox@example.test",
            mailbox="INBOX",
            encrypted_password="fixture-ciphertext",
            enabled=True,
        )
        session.add_all((score, summary, extraction_job, job, mailbox_config))
        session.flush()

        mailbox_import = EmailAttachmentImport(
            mailbox_config_id=mailbox_config.id,
            message_uid="workspace-b-failed-message",
            message_id="<workspace-b-failed@example.test>",
            attachment_filename="workspace-b-failed.pdf",
            attachment_sha256="f" * 64,
            source_uidvalidity=9,
            source_fingerprint="e" * 64,
            status="failed",
            error="attachment_import_failed",
            attempt_count=1,
            last_attempted_at=task_resume.created_at,
        )
        session.add(mailbox_import)
        session.flush()

        job_version = JobVersion(
            job_id=job.id,
            version=1,
            title=job.title,
            raw_text=job.jd_text,
            status="confirmed",
        )
        session.add(job_version)
        session.commit()

        return {
            "task_resume_id": task_resume.id,
            "score_id": score.id,
            "summary_id": summary.id,
            "job_id": job.id,
            "job_version_id": job_version.id,
            "mailbox_import_id": mailbox_import.id,
        }


def test_registration_and_login_keep_workspace_sessions_separate(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients

    session_a = _register_and_login(
        client_a,
        organization_name="Workspace Alpha",
        full_name="Alpha Admin",
        email="alpha-admin@example.test",
        password="tenant-test-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Workspace Beta",
        full_name="Beta Admin",
        email="beta-admin@example.test",
        password="tenant-test-password-b",
    )

    assert (
        session_a["organization"]["organization_id"]
        != session_b["organization"]["organization_id"]
    )
    assert session_a["user"]["user_id"] != session_b["user"]["user_id"]
    assert session_a["organization"]["name"] == "Workspace Alpha"
    assert session_b["organization"]["name"] == "Workspace Beta"
    assert session_a["user"]["email"] == "alpha-admin@example.test"
    assert session_b["user"]["email"] == "beta-admin@example.test"

    # The session endpoint is a current-workspace identity payload, not a
    # membership directory or a cross-workspace discovery API.
    forbidden_directory_fields = {"organizations", "memberships", "workspace_ids"}
    assert not forbidden_directory_fields.intersection(session_a)
    assert not forbidden_directory_fields.intersection(session_b)


def test_workspaces_can_reuse_candidate_names_without_cross_tenant_access(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Workspace Alpha",
        full_name="Alpha Admin",
        email="alpha-admin@example.test",
        password="tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Workspace Beta",
        full_name="Beta Admin",
        email="beta-admin@example.test",
        password="tenant-test-password-b",
    )

    shared_name = "Shared fixture candidate"
    candidate_a_id, resume_a_id = _create_candidate_and_resume(
        client_a,
        display_name=shared_name,
    )
    candidate_b_id, resume_b_id = _create_candidate_and_resume(
        client_b,
        display_name=shared_name,
    )
    assert candidate_a_id != candidate_b_id
    assert resume_a_id != resume_b_id

    library_a = client_a.get("/v1/resume-library")
    library_b = client_b.get("/v1/resume-library")
    assert library_a.status_code == 200, library_a.text
    assert library_b.status_code == 200, library_b.text
    a_rows = library_a.json()["items"]
    b_rows = library_b.json()["items"]
    assert {row["candidate_id"] for row in a_rows} == {candidate_a_id}
    assert {row["resume_id"] for row in a_rows} == {resume_a_id}
    assert {row["candidate_id"] for row in b_rows} == {candidate_b_id}
    assert {row["resume_id"] for row in b_rows} == {resume_b_id}

    # IDs are unguessable in normal use, but authorization must still treat a
    # foreign resource exactly as nonexistent.
    foreign_candidate = client_a.post(
        f"/v1/candidates/{candidate_b_id}/resumes",
        files={
            "file": (
                "foreign-access.pdf",
                _pdf_with_text("Foreign tenant access must fail " * 8),
                "application/pdf",
            )
        },
    )
    assert foreign_candidate.status_code == 404, foreign_candidate.text

    foreign_resume = client_a.get(f"/v1/resumes/{resume_b_id}")
    assert foreign_resume.status_code == 404, foreign_resume.text

    foreign_original = client_a.get(f"/v1/resumes/{resume_b_id}/original-file")
    assert foreign_original.status_code == 404, foreign_original.text


def test_workspace_scopes_jd_score_summary_tasks_and_mailbox_configuration(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Workspace Alpha",
        full_name="Alpha Admin",
        email="alpha-admin@example.test",
        password="tenant-test-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Workspace Beta",
        full_name="Beta Admin",
        email="beta-admin@example.test",
        password="tenant-test-password-b",
    )
    organization_b_id = str(session_b["organization"]["organization_id"])
    private = _seed_workspace_b_private_resources(
        client_b,
        organization_id=organization_b_id,
    )

    # Positive controls establish that the synthetic B records are valid
    # server-side resources before the A-session isolation assertions below.
    assert client_b.get(f"/v1/jobs/{private['job_id']}/versions").status_code == 200
    assert client_b.get(f"/v1/job-versions/{private['job_version_id']}").status_code == 200
    assert client_b.get(f"/v1/resume-scores/{private['score_id']}").status_code == 200
    assert client_b.get(f"/v1/resume-summaries/{private['summary_id']}").status_code == 200
    assert client_b.get("/v1/mailbox/config").json()["configured"] is True

    foreign_job_versions = client_a.get(f"/v1/jobs/{private['job_id']}/versions")
    assert foreign_job_versions.status_code == 404, foreign_job_versions.text

    foreign_job_version = client_a.get(f"/v1/job-versions/{private['job_version_id']}")
    assert foreign_job_version.status_code == 404, foreign_job_version.text

    visible_confirmed_jobs = client_a.get("/v1/jobs/confirmed-versions")
    assert visible_confirmed_jobs.status_code == 200, visible_confirmed_jobs.text
    assert private["job_version_id"] not in {
        item["job_version_id"] for item in visible_confirmed_jobs.json()
    }

    foreign_score = client_a.get(f"/v1/resume-scores/{private['score_id']}")
    assert foreign_score.status_code == 404, foreign_score.text

    foreign_summary = client_a.get(f"/v1/resume-summaries/{private['summary_id']}")
    assert foreign_summary.status_code == 404, foreign_summary.text

    foreign_score_history = client_a.get(
        f"/v1/resumes/{private['task_resume_id']}/scores"
    )
    assert foreign_score_history.status_code == 404, foreign_score_history.text

    foreign_summary_history = client_a.get(
        f"/v1/resumes/{private['task_resume_id']}/summaries"
    )
    assert foreign_summary_history.status_code == 404, foreign_summary_history.text

    b_review_queue = client_b.get("/v1/resumes/review-queue")
    a_review_queue = client_a.get("/v1/resumes/review-queue")
    assert b_review_queue.status_code == 200, b_review_queue.text
    assert a_review_queue.status_code == 200, a_review_queue.text
    assert private["task_resume_id"] in {
        item["resume_id"] for item in b_review_queue.json()["items"]
    }
    assert private["task_resume_id"] not in {
        item["resume_id"] for item in a_review_queue.json()["items"]
    }

    foreign_task_retry = client_a.post(
        f"/v1/resumes/{private['task_resume_id']}/queue-ai-extraction"
    )
    assert foreign_task_retry.status_code == 404, foreign_task_retry.text

    mailbox_for_a = client_a.get("/v1/mailbox/config")
    mailbox_history_for_a = client_a.get("/v1/mailbox/imports")
    assert mailbox_for_a.status_code == 200, mailbox_for_a.text
    assert mailbox_for_a.json()["configured"] is False
    assert mailbox_history_for_a.status_code == 200, mailbox_history_for_a.text
    assert mailbox_history_for_a.json() == {"items": [], "total": 0}

    foreign_mailbox_retry = client_a.post(
        f"/v1/mailbox/imports/{private['mailbox_import_id']}/retry"
    )
    assert foreign_mailbox_retry.status_code == 404, foreign_mailbox_retry.text
