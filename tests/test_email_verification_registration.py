from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.config import AppSettings
from app.main import _registration_client_identifier, create_app
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
    return _register_with_email(client, "verification-admin@example.test")


def _register_with_email(client: TestClient, email: str) -> tuple[dict[str, object], str]:
    response = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Verification fixture workspace",
            "full_name": "Verification fixture admin",
            "email": email,
            "password": "verification-fixture-password",
        },
    )
    assert response.status_code == 201, response.text
    delivery = next(
        item
        for item in reversed(client.app.state.transactional_email_provider.deliveries)
        if item.recipient == email
    )
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


def test_verification_link_cannot_replace_another_workspace_session(
    registration_client: TestClient,
) -> None:
    first_registered, first_token = _register(registration_client)
    first_verified = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": first_token},
    )
    assert first_verified.status_code == 200
    first_user_id = first_registered["user"]["user_id"]

    assert registration_client.post("/v1/auth/logout").status_code == 204
    _, second_token = _register_with_email(registration_client, "second-admin@example.test")

    logged_in = registration_client.post(
        "/v1/auth/login",
        json={
            "email": "verification-admin@example.test",
            "password": "verification-fixture-password",
        },
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["user_id"] == first_user_id

    mismatch = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": second_token},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "email_verification_account_mismatch"
    assert registration_client.get("/v1/auth/session").json()["user"]["user_id"] == first_user_id

    database = registration_client.app.state.database
    with database.session_factory() as session:
        second_user = session.scalar(
            select(UserAccount).where(UserAccount.email_key == "second-admin@example.test")
        )
        assert second_user is not None
        assert second_user.email_verified_at is None
        second_verification = session.scalar(
            select(EmailVerificationToken).where(EmailVerificationToken.user_id == second_user.id)
        )
        assert second_verification is not None
        assert second_verification.used_at is None

    assert registration_client.post("/v1/auth/logout").status_code == 204
    second_verified = registration_client.post(
        "/v1/auth/email-verification/complete",
        json={"token": second_token},
    )
    assert second_verified.status_code == 200
    assert second_verified.json()["email_verified"] is True


def test_database_permits_only_one_active_verification_token_per_user(
    registration_client: TestClient,
) -> None:
    _register(registration_client)
    database = registration_client.app.state.database
    with database.session_factory() as session:
        user = session.scalar(
            select(UserAccount).where(UserAccount.email_key == "verification-admin@example.test")
        )
        assert user is not None
        session.add(
            EmailVerificationToken(
                user_id=user.id,
                token_digest="f" * 64,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_public_registration_has_a_durable_global_rate_limit(tmp_path: Path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="registration-rate-limit-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
        registration_rate_limit_global_limit=1,
        registration_rate_limit_global_window_seconds=60 * 60,
        registration_rate_limit_client_limit=10,
        registration_rate_limit_client_window_seconds=60 * 60,
        registration_rate_limit_email_limit=10,
        registration_rate_limit_email_window_seconds=24 * 60 * 60,
    )
    with TestClient(create_app(settings)) as client:
        _register_with_email(client, "first-rate-limit@example.test")
        throttled = client.post(
            "/v1/auth/register",
            json={
                "organization_name": "Rate limit workspace",
                "full_name": "Rate limit admin",
                "email": "second-rate-limit@example.test",
                "password": "verification-fixture-password",
            },
        )
        assert throttled.status_code == 429
        assert throttled.json()["detail"] == "registration_rate_limit_exceeded"
        assert len(client.app.state.transactional_email_provider.deliveries) == 1


def test_registration_does_not_trust_forwarded_client_headers_by_default(tmp_path: Path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="forwarded-header-rate-limit-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
        registration_rate_limit_global_limit=10,
        registration_rate_limit_global_window_seconds=60 * 60,
        registration_rate_limit_client_limit=1,
        registration_rate_limit_client_window_seconds=60 * 60,
        registration_rate_limit_email_limit=10,
        registration_rate_limit_email_window_seconds=24 * 60 * 60,
    )
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/v1/auth/register",
            headers={"x-forwarded-for": "198.51.100.10"},
            json={
                "organization_name": "Forwarded header workspace",
                "full_name": "Forwarded header admin",
                "email": "first-forwarded@example.test",
                "password": "verification-fixture-password",
            },
        )
        assert first.status_code == 201
        second = client.post(
            "/v1/auth/register",
            headers={"x-forwarded-for": "203.0.113.20"},
            json={
                "organization_name": "Forwarded header workspace",
                "full_name": "Forwarded header admin",
                "email": "second-forwarded@example.test",
                "password": "verification-fixture-password",
            },
        )
        assert second.status_code == 429
        assert second.json()["detail"] == "registration_rate_limit_exceeded"


def test_trusted_proxy_uses_the_last_forwarded_client_address(tmp_path: Path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        trusted_proxy_cidrs=("127.0.0.1/32",),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/auth/register",
            "raw_path": b"/v1/auth/register",
            "query_string": b"",
            "headers": [(b"x-forwarded-for", b"198.51.100.10, 203.0.113.20")],
            "client": ("127.0.0.1", 8000),
            "server": ("testserver", 443),
        }
    )
    assert _registration_client_identifier(request, settings) == "ip:203.0.113.20"
