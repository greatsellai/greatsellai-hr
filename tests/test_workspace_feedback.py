from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Organization,
    WorkspaceFeedbackImageAttachment,
    WorkspaceFeedbackSubmission,
    utcnow,
)
from app.services.workspace_feedback_service import (
    run_workspace_feedback_reward_worker_once,
)
from app.tenant_scope import set_organization_context


# A compact, valid PNG header is enough for the server's deliberately
# signature-based image acceptance boundary. No user/candidate material is
# used anywhere in this fixture.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


@pytest.fixture
def feedback_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient, TestClient]]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        admin_token="workspace-feedback-platform-token",
        legacy_admin_token_enabled=True,
        session_secret="workspace-feedback-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app):
        client_a = TestClient(app)
        client_b = TestClient(app)
        platform_client = TestClient(app)
        try:
            yield client_a, client_b, platform_client
        finally:
            client_a.close()
            client_b.close()
            platform_client.close()


def _register_and_login(
    client: TestClient,
    *,
    organization_name: str,
    full_name: str,
    email: str,
) -> dict[str, object]:
    password = "workspace-feedback-password"
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
    logged_in = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()


def _feedback_payload() -> dict[str, str]:
    return {
        "use_case": "批量筛选候选人并核对项目经历。",
        "intended_outcome": "快速找到适合当前岗位的候选人。",
        "friction": "筛选结果中的技能说明不够容易核对。",
        "desired_change": "希望结果能直接展示对应项目证据。",
    }


def _submit(
    client: TestClient,
    *,
    idempotency_key: str,
    with_image: bool = False,
) -> object:
    files = (
        [("attachments", ("feedback-evidence.png", PNG_BYTES, "image/png"))]
        if with_image
        else None
    )
    return client.post(
        "/v1/workspace-feedback",
        data=_feedback_payload(),
        files=files,
        headers={"Idempotency-Key": idempotency_key},
    )


def test_workspace_feedback_is_complete_private_and_attachment_is_scoped(
    feedback_clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    client_a, client_b, _ = feedback_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Feedback workspace A",
        full_name="Feedback owner A",
        email="feedback-owner-a@example.test",
    )
    _register_and_login(
        client_b,
        organization_name="Feedback workspace B",
        full_name="Feedback owner B",
        email="feedback-owner-b@example.test",
    )

    missing_required_answer = client_a.post(
        "/v1/workspace-feedback",
        data={key: value for key, value in _feedback_payload().items() if key != "friction"},
        headers={"Idempotency-Key": "feedback-missing-answer"},
    )
    assert missing_required_answer.status_code == 422, missing_required_answer.text

    created = _submit(client_a, idempotency_key="feedback-private-001", with_image=True)
    assert created.status_code == 200, created.text
    created_payload = created.json()
    assert created_payload["next_submission_at"]
    assert len(created_payload["items"]) == 1
    feedback = created_payload["items"][0]
    assert feedback["reward_status"] == "queued"
    assert feedback["reward_call_count"] == 500
    assert len(feedback["attachments"]) == 1
    assert "storage_key" not in feedback["attachments"][0]
    assert "content_sha256" not in feedback["attachments"][0]

    own_history = client_a.get("/v1/workspace-feedback")
    assert own_history.status_code == 200, own_history.text
    assert own_history.json()["items"][0]["feedback_id"] == feedback["feedback_id"]
    other_history = client_b.get("/v1/workspace-feedback")
    assert other_history.status_code == 200, other_history.text
    assert other_history.json()["items"] == []

    attachment_id = feedback["attachments"][0]["attachment_id"]
    own_attachment = client_a.get(
        f"/v1/workspace-feedback/{feedback['feedback_id']}/attachments/{attachment_id}"
    )
    assert own_attachment.status_code == 200, own_attachment.text
    assert own_attachment.content == PNG_BYTES
    assert own_attachment.headers["cache-control"] == "no-store, private"
    assert own_attachment.headers["x-content-type-options"] == "nosniff"
    assert client_b.get(
        f"/v1/workspace-feedback/{feedback['feedback_id']}/attachments/{attachment_id}"
    ).status_code == 404

    # The test's raw database check verifies the attachment remains attached
    # to A's workspace rather than trusting only the serialized response.
    organization_id = session_a["organization"]["organization_id"]
    with client_a.app.state.database.session_factory() as database_session:
        set_organization_context(database_session, organization_id)
        attachment = database_session.scalar(select(WorkspaceFeedbackImageAttachment))
        assert attachment is not None
        assert attachment.organization_id == organization_id


def test_workspace_feedback_cooldown_and_idempotent_retry_leave_one_image(
    feedback_clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    client_a, _, _ = feedback_clients
    session = _register_and_login(
        client_a,
        organization_name="Feedback retry workspace",
        full_name="Feedback retry owner",
        email="feedback-retry@example.test",
    )

    first = _submit(client_a, idempotency_key="feedback-retry-001", with_image=True)
    assert first.status_code == 200, first.text
    replayed = _submit(client_a, idempotency_key="feedback-retry-001", with_image=True)
    assert replayed.status_code == 200, replayed.text
    assert len(replayed.json()["items"]) == 1

    cooldown = _submit(client_a, idempotency_key="feedback-retry-002", with_image=False)
    assert cooldown.status_code == 409, cooldown.text
    assert cooldown.json()["detail"] == "workspace_feedback_cooldown"

    organization_id = session["organization"]["organization_id"]
    with client_a.app.state.database.session_factory() as database_session:
        set_organization_context(database_session, organization_id)
        submissions = database_session.scalars(select(WorkspaceFeedbackSubmission)).all()
        attachments = database_session.scalars(select(WorkspaceFeedbackImageAttachment)).all()
        organization = database_session.get(Organization, organization_id)
        assert len(submissions) == 1
        assert len(attachments) == 1
        assert organization is not None and organization.feedback_reward_available_at is not None

    stored_files = list(
        (
            client_a.app.state.settings.upload_dir
            / "workspace-feedback"
            / organization_id
        ).glob("*")
    )
    assert len(stored_files) == 1
    assert stored_files[0].is_file()


def test_due_feedback_reward_grants_exactly_once_and_platform_can_read(
    feedback_clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    client_a, _, platform_client = feedback_clients
    session = _register_and_login(
        client_a,
        organization_name="Feedback reward workspace",
        full_name="Feedback reward owner",
        email="feedback-reward@example.test",
    )
    created = _submit(client_a, idempotency_key="feedback-worker-001", with_image=True)
    assert created.status_code == 200, created.text
    feedback = created.json()["items"][0]
    organization_id = session["organization"]["organization_id"]

    # A queued reward must never be granted before its server-owned due time.
    assert not run_workspace_feedback_reward_worker_once(
        client_a.app.state.database,
        worker_id="workspace-feedback-test-worker-early",
    )

    with client_a.app.state.database.session_factory() as database_session:
        set_organization_context(database_session, organization_id)
        submission = database_session.get(WorkspaceFeedbackSubmission, feedback["feedback_id"])
        organization = database_session.get(Organization, organization_id)
        assert submission is not None and organization is not None
        before_limit = organization.trial_llm_call_limit
        submission.reward_due_at = utcnow() - timedelta(seconds=1)
        database_session.commit()

    assert run_workspace_feedback_reward_worker_once(
        client_a.app.state.database,
        worker_id="workspace-feedback-test-worker",
    )
    assert not run_workspace_feedback_reward_worker_once(
        client_a.app.state.database,
        worker_id="workspace-feedback-test-worker-repeat",
    )

    with client_a.app.state.database.session_factory() as database_session:
        set_organization_context(database_session, organization_id)
        submission = database_session.get(WorkspaceFeedbackSubmission, feedback["feedback_id"])
        organization = database_session.get(Organization, organization_id)
        assert submission is not None and organization is not None
        assert submission.reward_status == "granted"
        assert submission.reward_granted_at is not None
        assert organization.trial_llm_call_limit == before_limit + 500

    history = client_a.get("/v1/workspace-feedback")
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["reward_status"] == "granted"

    assert platform_client.post(
        "/v1/auth/login",
        json={"password": "workspace-feedback-platform-token"},
    ).status_code == 200
    platform_rows = platform_client.get("/v1/platform/workspace-feedback")
    assert platform_rows.status_code == 200, platform_rows.text
    row = next(
        item
        for item in platform_rows.json()["items"]
        if item["feedback_id"] == feedback["feedback_id"]
    )
    assert row["organization_id"] == organization_id
    assert row["submitter_email"] == "feedback-reward@example.test"
    assert row["friction"] == _feedback_payload()["friction"]
    platform_attachment = platform_client.get(
        f"/v1/platform/workspace-feedback/{feedback['feedback_id']}/attachments/"
        f"{feedback['attachments'][0]['attachment_id']}"
    )
    assert platform_attachment.status_code == 200, platform_attachment.text
    assert platform_attachment.content == PNG_BYTES
