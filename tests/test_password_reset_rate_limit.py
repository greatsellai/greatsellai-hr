from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import PasswordResetToken, RegistrationRateLimitBucket
from app.services.identity_service import digest_token


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "sqlite://",
        "session_secret": "password-reset-rate-limit-test-session-secret",
        "transactional_email_provider": "test",
        "public_app_url": "http://testserver",
        "allow_unauthenticated": False,
        "trusted_proxy_cidrs": ("127.0.0.1/32",),
        "password_reset_rate_limit_global_limit": 100,
        "password_reset_rate_limit_global_window_seconds": 60 * 60,
        "password_reset_rate_limit_client_limit": 100,
        "password_reset_rate_limit_client_window_seconds": 60 * 60,
        "password_reset_rate_limit_email_limit": 100,
        "password_reset_rate_limit_email_window_seconds": 24 * 60 * 60,
    }
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


def _register_and_verify(client: TestClient, *, email: str) -> None:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Password reset rate-limit workspace",
            "full_name": "Synthetic administrator",
            "email": email,
            "password": "password-reset-rate-limit-password",
        },
    )
    assert registered.status_code == 201, registered.text
    verification = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(verification.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert client.post("/v1/auth/logout").status_code == 204


def _request_reset(
    client: TestClient,
    *,
    email: str,
    forwarded_for: str,
):
    return client.post(
        "/v1/auth/password-reset/request",
        headers={"x-forwarded-for": forwarded_for},
        json={"email": email},
    )


def test_password_reset_rate_limit_uses_caddy_client_ip_and_keeps_active_link_on_429(
    tmp_path: Path,
) -> None:
    """A Caddy-forwarded client limit never replaces an existing reset token.

    The direct ASGI peer is a trusted Caddy stand-in. The last forwarded
    component is therefore the real browser identity; a spoofed prefix must
    not change it.
    """

    settings = _settings(
        tmp_path,
        password_reset_rate_limit_client_limit=1,
    )
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 2015)) as client:
        known_email = "known-password-reset-rate@example.test"
        _register_and_verify(client, email=known_email)

        known = _request_reset(
            client,
            email=known_email,
            forwarded_for="client-spoofed-prefix, 198.51.100.10",
        )
        assert known.status_code == 200, known.text
        assert known.json() == {"accepted": True, "delivery_available": True}
        delivery = client.app.state.transactional_email_provider.password_reset_deliveries[-1]
        reset_token = parse_qs(urlsplit(delivery.reset_url).query)["token"][0]

        # A different Caddy-forwarded browser gets its own persisted client
        # bucket. Unknown and registered addresses expose the same accepted
        # public response; only a real delivery is omitted for the unknown
        # address.
        unknown = _request_reset(
            client,
            email="unknown-password-reset-rate@example.test",
            forwarded_for="another-spoofed-prefix, 203.0.113.20",
        )
        assert unknown.status_code == 200, unknown.text
        assert unknown.json() == known.json()
        assert len(client.app.state.transactional_email_provider.password_reset_deliveries) == 1

        with app.state.database.session_factory() as session:
            client_buckets = session.scalars(
                select(RegistrationRateLimitBucket).where(
                    RegistrationRateLimitBucket.scope == "password_reset_client"
                )
            ).all()
            active_reset = session.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_digest == digest_token(reset_token)
                )
            )
            assert active_reset is not None
            assert active_reset.invalidated_at is None
        assert len(client_buckets) == 2
        assert sorted(bucket.request_count for bucket in client_buckets) == [1, 1]

        throttled = _request_reset(
            client,
            email=known_email,
            forwarded_for="changed-spoofed-prefix, 198.51.100.10",
        )
        assert throttled.status_code == 429, throttled.text
        assert throttled.json()["detail"] == "password_reset_rate_limit_exceeded"
        assert len(client.app.state.transactional_email_provider.password_reset_deliveries) == 1

        # The 429 was emitted before issue_password_reset(), so the original
        # recovery link remains usable rather than being silently replaced.
        with app.state.database.session_factory() as session:
            active_reset = session.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_digest == digest_token(reset_token)
                )
            )
            assert active_reset is not None
            assert active_reset.invalidated_at is None

        completed = client.post(
            "/v1/auth/password-reset/complete",
            json={"token": reset_token, "password": "reset-after-429-password"},
        )
        assert completed.status_code == 204, completed.text


def test_password_reset_rate_limit_email_scope_throttles_unknown_addresses(tmp_path: Path) -> None:
    """Unknown email requests consume the same opaque email-scope budget."""

    settings = _settings(
        tmp_path,
        password_reset_rate_limit_email_limit=1,
    )
    with TestClient(create_app(settings), client=("127.0.0.1", 2015)) as client:
        first = _request_reset(
            client,
            email="unknown-email-scope@example.test",
            forwarded_for="198.51.100.11",
        )
        second = _request_reset(
            client,
            email="unknown-email-scope@example.test",
            forwarded_for="203.0.113.21",
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 429, second.text
        assert second.json()["detail"] == "password_reset_rate_limit_exceeded"
        assert client.app.state.transactional_email_provider.password_reset_deliveries == []


def test_password_reset_rate_limit_global_scope_is_shared_across_clients(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        password_reset_rate_limit_global_limit=1,
    )
    with TestClient(create_app(settings), client=("127.0.0.1", 2015)) as client:
        first = _request_reset(
            client,
            email="global-first@example.test",
            forwarded_for="198.51.100.12",
        )
        second = _request_reset(
            client,
            email="global-second@example.test",
            forwarded_for="203.0.113.22",
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 429, second.text
        assert second.json()["detail"] == "password_reset_rate_limit_exceeded"


def test_password_reset_rate_limit_settings_load_from_env_and_validate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_GLOBAL_LIMIT", "71")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_GLOBAL_WINDOW_SECONDS", "3601")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_LIMIT", "6")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_CLIENT_WINDOW_SECONDS", "901")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_LIMIT", "4")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RATE_LIMIT_EMAIL_WINDOW_SECONDS", "86401")

    loaded = AppSettings.from_env()
    assert loaded.password_reset_rate_limit_global_limit == 71
    assert loaded.password_reset_rate_limit_global_window_seconds == 3601
    assert loaded.password_reset_rate_limit_client_limit == 6
    assert loaded.password_reset_rate_limit_client_window_seconds == 901
    assert loaded.password_reset_rate_limit_email_limit == 4
    assert loaded.password_reset_rate_limit_email_window_seconds == 86401

    with pytest.raises(ValueError, match="PASSWORD_RESET_RATE_LIMIT_EMAIL_LIMIT"):
        replace(
            _settings(tmp_path),
            password_reset_rate_limit_email_limit=0,
        ).validate_runtime()
    with pytest.raises(ValueError, match="PASSWORD_RESET_RATE_LIMIT_CLIENT_WINDOW_SECONDS"):
        replace(
            _settings(tmp_path),
            password_reset_rate_limit_client_window_seconds=59,
        ).validate_runtime()
