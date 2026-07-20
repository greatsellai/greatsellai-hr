from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import EmailVerificationToken, UserAccount


@pytest.fixture
def registration_client(tmp_path: Path) -> TestClient:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="email-verification-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _register(client: TestClient) -> tuple[dict[str, object], str]:
    response = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Verification fixture workspace",
            "full_name": "Verification fixture admin",
            "email": "verification-admin@example.test",
            "password": "verification-fixture-password",
        },
    )
    assert response.status_code == 201, response.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    return response.json(), token


def test_registration_requires_email_verification_before_business_access(
    registration_client: TestClient,
) -> None:
    registered, token = _register(registration_client)

    assert registered["authenticated"] is True
    assert registered["email_verified"] is False
    assert registered["email_verification_required"] is True
    assert registration_client.get("/v1/resume-library").status_code == 403
    assert registration_client.post("/v1/candidates", json={"display_name": "fixture"}).status_code == 403

    database = registration_client.app.state.database
    with database.session_factory() as session:
        verification = session.scalar(select(EmailVerificationToken))
        assert verification is not None
        assert verification.token_digest != token
        assert verification.delivered_at is not None
        assert verification.delivery_attempt_count == 1

    verified = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": token},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["email_verified"] is True
    assert verified.json()["email_verification_required"] is False
    assert registration_client.get("/v1/resume-library").status_code == 200

    reused = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": token},
    )
    assert reused.status_code == 422
    assert reused.json()["detail"] == "email_verification_invalid_or_expired"


def test_resend_rate_limit_invalidates_older_link_and_keeps_tenant_gated(
    registration_client: TestClient,
) -> None:
    _, first_token = _register(registration_client)

    too_soon = registration_client.post("/v1/auth/email-verification/resend")
    assert too_soon.status_code == 429
    assert too_soon.json()["detail"] == "email_verification_resend_too_soon"

    database = registration_client.app.state.database
    with database.session_factory() as session:
        verification = session.scalar(select(EmailVerificationToken))
        assert verification is not None
        verification.requested_at -= timedelta(seconds=61)
        session.commit()

    resent = registration_client.post("/v1/auth/email-verification/resend")
    assert resent.status_code == 200, resent.text
    assert resent.json() == {"accepted": True, "delivery_available": True}
    second_delivery = registration_client.app.state.transactional_email_provider.deliveries[-1]
    second_token = parse_qs(urlsplit(second_delivery.verification_url).query)["token"][0]
    assert second_token != first_token

    invalidated = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": first_token},
    )
    assert invalidated.status_code == 422
    assert registration_client.get("/v1/resume-library").status_code == 403

    verified = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": second_token},
    )
    assert verified.status_code == 200


def test_registration_never_creates_a_dead_unverified_account_without_sender(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="disabled-sender-test-session-secret",
        allow_unauthenticated=False,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/auth/register",
            json={
                "organization_name": "No sender workspace",
                "full_name": "No sender admin",
                "email": "no-sender@example.test",
                "password": "no-sender-fixture-password",
            },
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "email_delivery_not_configured"

        with client.app.state.database.session_factory() as session:
            account = session.scalar(
                select(UserAccount).where(UserAccount.email_key == "no-sender@example.test")
            )
            assert account is None


def test_tencent_ses_configuration_requires_verified_sender_credentials(tmp_path: Path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        transactional_email_provider="tencent_ses",
        transactional_email_from="noreply@mail.example.test",
        public_app_url="https://hr.example.test",
        tencent_ses_verification_template_id=123,
    )
    with pytest.raises(ValueError, match="Tencent SES requires"):
        settings.validate_runtime()
