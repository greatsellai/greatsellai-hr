from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import RegistrationRateLimitBucket


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "sqlite://",
        "session_secret": "auth-hardening-test-session-secret",
        "transactional_email_provider": "test",
        "public_app_url": "http://testserver",
        "allow_unauthenticated": False,
        "trusted_proxy_cidrs": ("127.0.0.1/32",),
        "login_rate_limit_global_limit": 100,
        "login_rate_limit_global_window_seconds": 60 * 60,
        "login_rate_limit_client_limit": 10,
        "login_rate_limit_client_window_seconds": 15 * 60,
        "login_rate_limit_email_limit": 8,
        "login_rate_limit_email_window_seconds": 15 * 60,
    }
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


def _register_and_verify(client: TestClient, *, email: str, password: str) -> None:
    response = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Authentication hardening fixture workspace",
            "full_name": "Authentication hardening fixture owner",
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 201, response.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert client.post("/v1/auth/logout").status_code == 204


def test_legacy_static_token_is_disabled_without_explicit_compatibility_switch(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        admin_token="legacy-static-token-fixture",
        legacy_admin_token_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        static_login = client.post(
            "/v1/auth/login",
            json={"password": "legacy-static-token-fixture"},
        )
        assert static_login.status_code == 401
        assert static_login.json()["detail"] == "invalid_login_credentials"

        header_attempt = client.get(
            "/v1/resume-library",
            headers={"x-admin-token": "legacy-static-token-fixture"},
        )
        assert header_attempt.status_code == 401
        assert header_attempt.json()["detail"] == "authentication_required"


def test_failed_login_limit_is_durable_hmac_and_does_not_lock_another_network(
    tmp_path: Path,
) -> None:
    """A hostile source cannot spend a different trusted source's account budget."""

    settings = _settings(tmp_path, login_rate_limit_email_limit=1)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 2015)) as client:
        email = "login-limit-account@example.test"
        password = "login-limit-password"
        _register_and_verify(client, email=email, password=password)

        first_failure = client.post(
            "/v1/auth/login",
            headers={"x-forwarded-for": "spoofed-prefix, 198.51.100.10"},
            json={"email": email, "password": "wrong-password"},
        )
        assert first_failure.status_code == 401, first_failure.text
        assert first_failure.json()["detail"] == "invalid_login_credentials"

        same_source_retry = client.post(
            "/v1/auth/login",
            headers={"x-forwarded-for": "other-prefix, 198.51.100.10"},
            json={"email": email, "password": password},
        )
        assert same_source_retry.status_code == 429, same_source_retry.text
        assert same_source_retry.json()["detail"] == "login_rate_limit_exceeded"

        different_source_success = client.post(
            "/v1/auth/login",
            headers={"x-forwarded-for": "spoofed-prefix, 203.0.113.20"},
            json={"email": email, "password": password},
        )
        assert different_source_success.status_code == 200, different_source_success.text

        with app.state.database.session_factory() as session:
            buckets = session.scalars(
                select(RegistrationRateLimitBucket).where(
                    RegistrationRateLimitBucket.scope.in_(
                        {"login_global", "login_client", "login_client_account"}
                    )
                )
            ).all()
        assert {bucket.scope for bucket in buckets} == {
            "login_global",
            "login_client",
            "login_client_account",
        }
        assert all(bucket.request_count == 1 for bucket in buckets)
        assert all(re.fullmatch(r"[0-9a-f]{64}", bucket.key_digest) for bucket in buckets)
        assert all(email not in bucket.key_digest for bucket in buckets)
        assert all("198.51.100.10" not in bucket.key_digest for bucket in buckets)


def test_login_limit_and_legacy_compatibility_settings_are_explicit_and_validated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED", "true")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_GLOBAL_LIMIT", "71")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_GLOBAL_WINDOW_SECONDS", "3601")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_LIMIT", "6")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS", "901")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_LIMIT", "4")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_WINDOW_SECONDS", "901")

    loaded = AppSettings.from_env()
    assert loaded.legacy_admin_token_enabled is True
    assert loaded.login_rate_limit_global_limit == 71
    assert loaded.login_rate_limit_global_window_seconds == 3601
    assert loaded.login_rate_limit_client_limit == 6
    assert loaded.login_rate_limit_client_window_seconds == 901
    assert loaded.login_rate_limit_email_limit == 4
    assert loaded.login_rate_limit_email_window_seconds == 901

    with pytest.raises(ValueError, match="LOGIN_RATE_LIMIT_EMAIL_LIMIT"):
        replace(_settings(tmp_path), login_rate_limit_email_limit=0).validate_runtime()
    with pytest.raises(ValueError, match="LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS"):
        replace(_settings(tmp_path), login_rate_limit_client_window_seconds=59).validate_runtime()
