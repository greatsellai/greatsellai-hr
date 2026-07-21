from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import PasswordResetToken
from app.services.identity_service import digest_token, utcnow


@pytest.fixture
def password_reset_client(tmp_path: Path) -> TestClient:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="password-reset-flow-test-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _register_and_verify(client: TestClient, *, email: str, password: str) -> None:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Password reset fixture workspace",
            "full_name": "Password reset fixture admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post(
        "/v1/auth/email-verification/complete",
        json={"token": token},
    )
    assert verified.status_code == 200, verified.text
    assert client.post("/v1/auth/logout").status_code == 204


def _request_reset(client: TestClient, email: str) -> tuple[dict[str, object], str]:
    response = client.post("/v1/auth/password-reset/request", json={"email": email})
    assert response.status_code == 200, response.text
    delivery = client.app.state.transactional_email_provider.password_reset_deliveries[-1]
    assert delivery.recipient == email
    parsed = urlsplit(delivery.reset_url)
    assert parsed.path == "/reset-password"
    token = parse_qs(parsed.query)["token"][0]
    return response.json(), token


def test_password_reset_delivers_link_changes_password_and_rejects_reuse(
    password_reset_client: TestClient,
) -> None:
    email = "password-reset-user@example.test"
    old_password = "old-password-fixture"
    new_password = "new-password-fixture"
    _register_and_verify(password_reset_client, email=email, password=old_password)

    response, token = _request_reset(password_reset_client, email)
    assert response == {"accepted": True, "delivery_available": True}
    assert password_reset_client.app.state.transactional_email_provider.outbox[-1].reset_url.endswith(token)

    database = password_reset_client.app.state.database
    with database.session_factory() as session:
        reset = session.scalar(select(PasswordResetToken))
        assert reset is not None
        assert reset.token_digest == digest_token(token)
        assert reset.token_digest != token
        assert reset.invalidated_at is None

    completed = password_reset_client.post(
        "/v1/auth/password-reset/complete",
        json={"token": token, "password": new_password},
    )
    assert completed.status_code == 204, completed.text

    old_login = password_reset_client.post(
        "/v1/auth/login",
        json={"email": email, "password": old_password},
    )
    assert old_login.status_code == 401
    new_login = password_reset_client.post(
        "/v1/auth/login",
        json={"email": email, "password": new_password},
    )
    assert new_login.status_code == 200, new_login.text

    reused = password_reset_client.post(
        "/v1/auth/password-reset/complete",
        json={"token": token, "password": "another-password-fixture"},
    )
    assert reused.status_code == 422
    assert reused.json()["detail"] == "password_reset_invalid_or_expired"


def test_password_reset_request_is_enumeration_safe_and_replaces_older_link(
    password_reset_client: TestClient,
) -> None:
    email = "password-reset-enumeration@example.test"
    _register_and_verify(
        password_reset_client,
        email=email,
        password="password-reset-enumeration-old",
    )

    first_response, first_token = _request_reset(password_reset_client, email)
    delivery_count = len(password_reset_client.app.state.transactional_email_provider.password_reset_deliveries)
    unknown = password_reset_client.post(
        "/v1/auth/password-reset/request",
        json={"email": "unknown-password-reset@example.test"},
    )
    assert unknown.status_code == 200
    assert unknown.json() == first_response
    assert len(password_reset_client.app.state.transactional_email_provider.password_reset_deliveries) == delivery_count

    _, second_token = _request_reset(password_reset_client, email)
    assert second_token != first_token

    database = password_reset_client.app.state.database
    with database.session_factory() as session:
        tokens = session.scalars(
            select(PasswordResetToken).order_by(PasswordResetToken.requested_at)
        ).all()
        assert len(tokens) == 2
        first = next(item for item in tokens if item.token_digest == digest_token(first_token))
        second = next(item for item in tokens if item.token_digest == digest_token(second_token))
        assert first.invalidated_at is not None
        assert second.invalidated_at is None

    invalidated = password_reset_client.post(
        "/v1/auth/password-reset/complete",
        json={"token": first_token, "password": "password-reset-enumeration-new"},
    )
    assert invalidated.status_code == 422
    assert invalidated.json()["detail"] == "password_reset_invalid_or_expired"


def test_password_reset_rejects_expired_link(password_reset_client: TestClient) -> None:
    email = "password-reset-expired@example.test"
    _register_and_verify(
        password_reset_client,
        email=email,
        password="password-reset-expired-old",
    )
    _, token = _request_reset(password_reset_client, email)

    database = password_reset_client.app.state.database
    with database.session_factory() as session:
        reset = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_digest == digest_token(token)
            )
        )
        assert reset is not None
        reset.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    expired = password_reset_client.post(
        "/v1/auth/password-reset/complete",
        json={"token": token, "password": "password-reset-expired-new"},
    )
    assert expired.status_code == 422
    assert expired.json()["detail"] == "password_reset_invalid_or_expired"

    old_password_still_works = password_reset_client.post(
        "/v1/auth/login",
        json={"email": email, "password": "password-reset-expired-old"},
    )
    assert old_password_still_works.status_code == 200, old_password_still_works.text


def test_password_reset_revokes_another_browser_session(tmp_path: Path) -> None:
    """A recovery action invalidates every signed session for that account."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="password-reset-session-revocation-test-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
    )
    app = create_app(settings)
    with TestClient(app):
        reset_browser = TestClient(app)
        other_browser = TestClient(app)
        try:
            email = "password-reset-session@example.test"
            old_password = "password-reset-session-old"
            new_password = "password-reset-session-new"
            _register_and_verify(reset_browser, email=email, password=old_password)

            existing_login = other_browser.post(
                "/v1/auth/login",
                json={"email": email, "password": old_password},
            )
            assert existing_login.status_code == 200, existing_login.text
            assert other_browser.get("/v1/resume-library").status_code == 200

            _, token = _request_reset(reset_browser, email)
            completed = reset_browser.post(
                "/v1/auth/password-reset/complete",
                json={"token": token, "password": new_password},
            )
            assert completed.status_code == 204, completed.text

            # The old signed cookie is still present in this browser, but the
            # server rejects it because its auth-session version is stale.
            assert other_browser.get("/v1/resume-library").status_code == 401
            assert other_browser.get("/v1/auth/session").json()["authenticated"] is False

            relogged = other_browser.post(
                "/v1/auth/login",
                json={"email": email, "password": new_password},
            )
            assert relogged.status_code == 200, relogged.text
            assert other_browser.get("/v1/resume-library").status_code == 200
        finally:
            other_browser.close()
            reset_browser.close()
