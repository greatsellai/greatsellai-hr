from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Candidate,
    EmailAttachmentImport,
    Job,
    JobVersion,
    MailboxConfig,
    MailboxAttachmentContentIdentity,
    MailboxSyncFailureAlert,
    Organization,
    Resume,
    ResumeAiExtractionJob,
    ResumeEducation,
    ResumeScore,
    ResumeSourceBlock,
    ResumeSummary,
    RecruitingAgentConversation,
    RecruitingAgentConversationTurn,
    ScoreTemplate,
    OrganizationMembership,
    UserAccount,
    utcnow,
)
from app.schemas import CandidateSearchRequest
from app.services import mailbox_import_service
from app.services.search_service import search_candidates
from app.tenant_scope import bypass_organization_scope, set_organization_context


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
        deepseek_api_key="unit-test-key",
        deepseek_model="unit-test-model",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
        # This two-session integration fixture uses a deterministic IMAP
        # double for one provider name. Keep that test-only endpoint explicit
        # instead of weakening the production exact-host allowlist.
        mailbox_imap_allowed_hosts=("imap.example.test",),
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
        search_candidate = Candidate(display_name="Workspace B search fixture")
        session.add(task_candidate)
        session.add(search_candidate)
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
        search_resume = Resume(
            candidate_id=search_candidate.id,
            original_filename="workspace-b-search-fixture.pdf",
            storage_key="workspace-b-search-fixture.pdf",
            sha256="c" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="tenant-test-fixture",
            raw_text="synthetic searchable tenant scope fixture",
            is_active=True,
        )
        session.add(search_resume)
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

        mailbox_alert = MailboxSyncFailureAlert(
            mailbox_config_id=mailbox_config.id,
            state="open",
            severity="warning",
            consecutive_failures=3,
            first_failed_at=task_resume.created_at,
            last_failed_at=task_resume.created_at,
            last_error_code="mailbox_connection_failed",
            opened_at=task_resume.created_at,
        )
        session.add(mailbox_alert)

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
            "search_resume_id": search_resume.id,
            "score_id": score.id,
            "score_template_id": score_template.id,
            "summary_id": summary.id,
            "job_id": job.id,
            "job_version_id": job_version.id,
            "mailbox_config_id": mailbox_config.id,
            "mailbox_import_id": mailbox_import.id,
        }


def _seed_source_only_language_resume(
    client: TestClient,
    *,
    organization_id: str,
    suffix: str,
) -> str:
    """Create one ready resume whose CET-4 evidence exists only in source text."""

    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        candidate = Candidate(display_name=f"Agent source fixture {suffix}")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename=f"agent-source-{suffix}.pdf",
            storage_key=f"agent-source-{suffix}.pdf",
            sha256=(suffix * 64)[:64],
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="tenant-test-fixture",
            raw_text="CET-4 source-only fixture",
            is_active=True,
            highest_degree="bachelor",
            facts_version=1,
        )
        session.add(resume)
        session.flush()
        session.add_all(
            (
                ResumeSourceBlock(
                    resume_id=resume.id,
                    block_id="page-001",
                    page_no=1,
                    block_type="paragraph",
                    text="Test University bachelor degree. CET-4 520.",
                ),
                ResumeEducation(
                    resume_id=resume.id,
                    school_name_raw="Test University",
                    degree="bachelor",
                    evidence_block_ids=["page-001"],
                ),
            )
        )
        session.commit()
        return resume.id


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


def test_agent_source_text_language_evidence_never_crosses_workspaces(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Agent evidence workspace alpha",
        full_name="Alpha Admin",
        email="agent-evidence-alpha@example.test",
        password="tenant-test-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Agent evidence workspace beta",
        full_name="Beta Admin",
        email="agent-evidence-beta@example.test",
        password="tenant-test-password-b",
    )
    organization_a_id = str(session_a["organization"]["organization_id"])
    organization_b_id = str(session_b["organization"]["organization_id"])
    resume_a_id = _seed_source_only_language_resume(
        client_a,
        organization_id=organization_a_id,
        suffix="a",
    )
    _seed_source_only_language_resume(
        client_b,
        organization_id=organization_b_id,
        suffix="b",
    )

    database = client_a.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_a_id)
        result = search_candidates(
            session,
            CandidateSearchRequest(
                language_credentials_any_of=[{"credential_code": "cet4"}],
            ),
            include_source_language_evidence=True,
        )

    assert result.total_count == 1
    assert [item.resume_id for item in result.items] == [resume_a_id]
    evidence = next(
        match
        for match in result.items[0].matched_evidence
        if match.filter_key == "language_credentials_any_of"
    )
    assert evidence.evidence_origin == "resume_text"


def test_recruiting_agent_conversations_are_private_to_owner_and_workspace(
    workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    """An opaque work-session ID is not a cross-user or cross-tenant handle."""

    client_a, client_b = workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Agent context workspace alpha",
        full_name="Alpha Admin",
        email="agent-context-alpha@example.test",
        password="tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Agent context workspace beta",
        full_name="Beta Admin",
        email="agent-context-beta@example.test",
        password="tenant-test-password-b",
    )
    organization_a_id = str(session_a["organization"]["organization_id"])
    owner_a_id = str(session_a["user"]["user_id"])
    database = client_a.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_a_id)
        another_member = UserAccount(
            email="agent-context-other@example.test",
            email_key="agent-context-other@example.test",
            full_name="Other recruiter",
            password_hash="not-used-in-this-test",
            email_verified_at=utcnow(),
        )
        session.add(another_member)
        session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization_a_id,
                user_id=another_member.id,
                role="recruiter",
            )
        )
        owner_conversation = RecruitingAgentConversation(
            owner_user_id=owner_a_id,
            expires_at=utcnow() + timedelta(hours=1),
        )
        other_member_conversation = RecruitingAgentConversation(
            owner_user_id=another_member.id,
            expires_at=utcnow() + timedelta(hours=1),
        )
        session.add_all((owner_conversation, other_member_conversation))
        session.flush()
        session.add(
            RecruitingAgentConversationTurn(
                conversation_id=owner_conversation.id,
                context_version=owner_conversation.context_version,
                user_message="Owner-only recruiter question.",
                assistant_message="Owner-only recruiter reply.",
            )
        )
        session.commit()
        owner_conversation_id = owner_conversation.id
        other_member_conversation_id = other_member_conversation.id

    owner_read = client_a.get(
        f"/v1/recruiting-agent/conversations/{owner_conversation_id}"
    )
    assert owner_read.status_code == 200, owner_read.text
    assert owner_read.json()["conversation_id"] == owner_conversation_id
    assert owner_read.json()["chat_history"] == [
        {
            "context_version": 1,
            "user_message": "Owner-only recruiter question.",
            "assistant_message": "Owner-only recruiter reply.",
            "created_at": owner_read.json()["chat_history"][0]["created_at"],
        }
    ]

    foreign_workspace_read = client_b.get(
        f"/v1/recruiting-agent/conversations/{owner_conversation_id}"
    )
    non_owner_read = client_a.get(
        f"/v1/recruiting-agent/conversations/{other_member_conversation_id}"
    )
    assert foreign_workspace_read.status_code == 404, foreign_workspace_read.text
    assert non_owner_read.status_code == 404, non_owner_read.text
    assert foreign_workspace_read.json()["detail"] == "agent_conversation_not_found"
    assert non_owner_read.json()["detail"] == "agent_conversation_not_found"

    def model_must_not_run(*args, **kwargs):
        raise AssertionError("an inaccessible Agent conversation must fail before the model")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        model_must_not_run,
    )
    foreign_workspace_turn = client_b.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "continue",
            "conversation_id": owner_conversation_id,
            "context_version": 1,
        },
    )
    non_owner_turn = client_a.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "continue",
            "conversation_id": other_member_conversation_id,
            "context_version": 1,
        },
    )
    assert foreign_workspace_turn.status_code == 404, foreign_workspace_turn.text
    assert non_owner_turn.status_code == 404, non_owner_turn.text
    assert foreign_workspace_turn.json()["detail"] == "agent_conversation_not_found"
    assert non_owner_turn.json()["detail"] == "agent_conversation_not_found"

    foreign_workspace_delete = client_b.delete(
        f"/v1/recruiting-agent/conversations/{owner_conversation_id}"
    )
    assert foreign_workspace_delete.status_code == 404, foreign_workspace_delete.text
    assert (
        client_a.get(f"/v1/recruiting-agent/conversations/{owner_conversation_id}").status_code
        == 200
    )


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


def test_contact_details_remain_scoped_to_the_resume_workspace(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Contact workspace alpha",
        full_name="Contact Alpha",
        email="contact-alpha@example.test",
        password="tenant-contact-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Contact workspace beta",
        full_name="Contact Beta",
        email="contact-beta@example.test",
        password="tenant-contact-password-b",
    )
    _, resume_b_id = _create_candidate_and_resume(
        client_b,
        display_name="Contact detail fixture",
    )
    with client_b.app.state.database.session_factory() as session:
        organization_b = session.scalar(
            select(Organization).where(Organization.name == "Contact workspace beta")
        )
        assert organization_b is not None
        set_organization_context(session, organization_b.id)
        resume = session.get(Resume, resume_b_id)
        assert resume is not None
        resume.contact_details = [
            {
                "kind": "email",
                "value": "candidate-contact@example.test",
                "evidence_block_ids": ["page-001"],
            }
        ]
        session.commit()

    owned_review = client_b.get(f"/v1/resumes/{resume_b_id}/review")
    assert owned_review.status_code == 200, owned_review.text
    assert owned_review.json()["contacts"] == [
        {
            "kind": "email",
            "value": "candidate-contact@example.test",
            "evidence_block_ids": ["page-001"],
        }
    ]
    owned_detail = client_b.get(f"/v1/resumes/{resume_b_id}")
    assert owned_detail.status_code == 200, owned_detail.text
    assert "contacts" not in owned_detail.json()

    missing_review = client_a.get("/v1/resumes/not-a-real-resume/review")
    foreign_detail = client_a.get(f"/v1/resumes/{resume_b_id}")
    foreign_review = client_a.get(f"/v1/resumes/{resume_b_id}/review")
    for response in (foreign_detail, foreign_review):
        assert response.status_code == missing_review.status_code == 404
        assert response.json()["detail"] == missing_review.json()["detail"] == "resume_not_found"


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
    b_mailboxes = client_b.get("/v1/mailboxes")
    assert b_mailboxes.status_code == 200, b_mailboxes.text
    b_mailbox = next(
        item
        for item in b_mailboxes.json()["items"]
        if item["mailbox_id"] == private["mailbox_config_id"]
    )
    assert b_mailbox["active_sync_alert"] is not None

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

    # The recruiter-facing search index is a separate composite query with
    # relation preloads for score, education, experience, and skills. It must
    # retain the same workspace boundary as direct resource endpoints.
    b_search = client_b.post("/v1/candidates/search", json={"limit": 20})
    a_search = client_a.post("/v1/candidates/search", json={"limit": 20})
    assert b_search.status_code == 200, b_search.text
    assert a_search.status_code == 200, a_search.text
    assert private["search_resume_id"] in {
        item["resume_id"] for item in b_search.json()["items"]
    }
    assert private["search_resume_id"] not in {
        item["resume_id"] for item in a_search.json()["items"]
    }

    # Fuzzy matching takes a separate evaluation path. It must retain the
    # same organization boundary before it evaluates or explains any result.
    b_fuzzy_search = client_b.post(
        "/v1/candidates/search",
        json={"limit": 20, "condition_match_mode": "any"},
    )
    a_fuzzy_search = client_a.post(
        "/v1/candidates/search",
        json={"limit": 20, "condition_match_mode": "any"},
    )
    assert b_fuzzy_search.status_code == 200, b_fuzzy_search.text
    assert a_fuzzy_search.status_code == 200, a_fuzzy_search.text
    assert private["search_resume_id"] in {
        item["resume_id"] for item in b_fuzzy_search.json()["items"]
    }
    assert private["search_resume_id"] not in {
        item["resume_id"] for item in a_fuzzy_search.json()["items"]
    }

    foreign_score_sort = client_a.post(
        "/v1/candidates/search",
        json={"limit": 20, "score_template_id": private["score_template_id"]},
    )
    assert foreign_score_sort.status_code == 422, foreign_score_sort.text
    assert foreign_score_sort.json()["detail"] == "score_template_not_found"

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
    a_mailboxes = client_a.get("/v1/mailboxes")
    assert a_mailboxes.status_code == 200, a_mailboxes.text
    assert a_mailboxes.json() == {"items": [], "total": 0}

    foreign_mailbox_retry = client_a.post(
        f"/v1/mailbox/imports/{private['mailbox_import_id']}/retry"
    )
    assert foreign_mailbox_retry.status_code == 404, foreign_mailbox_retry.text

    # New named-channel routes must use the current workspace scope just as
    # strictly as the compatibility endpoints above. An opaque ID from B must
    # never become a readable or mutable resource for A.
    foreign_mailbox = client_a.get(f"/v1/mailboxes/{private['mailbox_config_id']}")
    foreign_mailbox_patch = client_a.patch(
        f"/v1/mailboxes/{private['mailbox_config_id']}",
        json={"display_name": "not-allowed"},
    )
    foreign_mailbox_sync = client_a.post(
        f"/v1/mailboxes/{private['mailbox_config_id']}/sync"
    )
    foreign_mailbox_archive = client_a.post(
        f"/v1/mailboxes/{private['mailbox_config_id']}/archive"
    )
    foreign_mailbox_history = client_a.get(
        f"/v1/mailbox-imports?mailbox_id={private['mailbox_config_id']}"
    )
    foreign_library_source = client_a.get(
        f"/v1/resume-library?mailbox_id={private['mailbox_config_id']}"
    )
    for response in (
        foreign_mailbox,
        foreign_mailbox_patch,
        foreign_mailbox_sync,
        foreign_mailbox_archive,
        foreign_mailbox_history,
        foreign_library_source,
    ):
        assert response.status_code == 404, response.text

    # Background task IDs are tenant-owned resources too. A task accepted in
    # B must not become a probeable detail or list entry for A.
    b_task = client_b.post(f"/v1/mailboxes/{private['mailbox_config_id']}/sync")
    assert b_task.status_code == 202, b_task.text
    task_id = b_task.json()["job_id"]
    foreign_task = client_a.get(f"/v1/mailbox/tasks/{task_id}")
    foreign_task_history = client_a.get(
        f"/v1/mailbox/tasks?mailbox_id={private['mailbox_config_id']}"
    )
    a_task_history = client_a.get("/v1/mailbox/tasks")
    assert foreign_task.status_code == 404, foreign_task.text
    assert foreign_task_history.status_code == 404, foreign_task_history.text
    assert a_task_history.status_code == 200, a_task_history.text
    assert task_id not in {item["job_id"] for item in a_task_history.json()["items"]}


def test_identical_mailbox_attachment_is_not_deduplicated_across_workspaces(
    workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    """The byte identity is workspace-scoped, never global across customers."""

    client_a, client_b = workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Dedup Alpha",
        full_name="Alpha Recruiter",
        email="dedup-alpha@example.test",
        password="tenant-test-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Dedup Beta",
        full_name="Beta Recruiter",
        email="dedup-beta@example.test",
        password="tenant-test-password-b",
    )

    pdf = _pdf_with_text("Tenant-safe shared mailbox attachment Python SQL " * 8)
    message = EmailMessage()
    message["Message-ID"] = "<same-bytes-different-workspaces@example.test>"
    message.set_content("Resume attached")
    message.add_attachment(
        pdf,
        maintype="application",
        subtype="pdf",
        filename="same-resume.pdf",
    )
    raw_message = message.as_bytes()

    class SharedAttachmentImap:
        status_calls_by_email: dict[str, int] = {}

        def __init__(self, *args, **kwargs) -> None:
            self.email_address = ""

        def login(self, email_address: str, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.email_address = email_address
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            calls = self.__class__.status_calls_by_email.get(self.email_address, 0)
            self.__class__.status_calls_by_email[self.email_address] = calls + 1
            uidnext = 42 if calls == 0 else 43
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"42"]
            if command == "fetch":
                return "OK", [(b"42 (RFC822)", raw_message)]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", SharedAttachmentImap)
    organization_a_id = str(session_a["organization"]["organization_id"])
    organization_b_id = str(session_b["organization"]["organization_id"])
    for client, email_address, organization_id in (
        (client_a, "alpha-resumes@example.test", organization_a_id),
        (client_b, "beta-resumes@example.test", organization_b_id),
    ):
        configured = client.put(
            "/v1/mailbox/config",
            json={
                "imap_host": "imap.example.test",
                "imap_port": 993,
                "email_address": email_address,
                "mailbox": "INBOX",
                "password": "test-authorization-code",
                "enabled": True,
            },
        )
        assert configured.status_code == 200, configured.text
        with client.app.state.database.session_factory() as database_session:
            set_organization_context(database_session, organization_id)
            synced = mailbox_import_service.sync_mailbox(
                database_session,
                settings=client.app.state.settings,
                config_id=configured.json()["mailbox_id"],
            )
        assert synced.imported_count == 1
        assert synced.duplicate_count == 0

    assert client_a.get("/v1/mailbox/imports").json()["total"] == 1
    assert client_b.get("/v1/mailbox/imports").json()["total"] == 1

    with client_a.app.state.database.session_factory() as database_session:
        with bypass_organization_scope(database_session):
            identities = database_session.scalars(
                select(MailboxAttachmentContentIdentity).order_by(
                    MailboxAttachmentContentIdentity.organization_id
                )
            ).all()
            resumes = database_session.scalars(select(Resume)).all()
        assert len(identities) == 2
        assert {identity.organization_id for identity in identities} == {
            organization_a_id,
            organization_b_id,
        }
        assert len({identity.attachment_sha256 for identity in identities}) == 1
        assert len(resumes) == 2
