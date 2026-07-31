from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main_module
from app.config import AppSettings
from app.main import create_app
from app.models import RegistrationRateLimitBucket, UserAccount
from app.services import identity_service


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


def test_password_only_and_admin_header_authentication_are_rejected(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        static_login = client.post(
            "/v1/auth/login",
            json={"password": "historical-static-token"},
        )
        assert static_login.status_code == 422

        header_attempt = client.get(
            "/v1/resume-library",
            headers={"x-admin-token": "historical-static-token"},
        )
        assert header_attempt.status_code == 401
        assert header_attempt.json()["detail"] == "authentication_required"


def test_failed_login_limit_is_durable_and_does_not_lock_correct_login_on_another_network(
    tmp_path: Path,
) -> None:
    """A hostile source cannot spend a different trusted source's account budget."""

    settings = _settings(
        tmp_path,
        login_rate_limit_client_limit=1,
        login_rate_limit_email_limit=1,
    )
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
                        {"login_client", "login_client_account"}
                    )
                )
            ).all()
        assert {bucket.scope for bucket in buckets} == {
            "login_client",
            "login_client_account",
        }
        assert all(bucket.request_count == 1 for bucket in buckets)
        assert all(re.fullmatch(r"[0-9a-f]{64}", bucket.key_digest) for bucket in buckets)
        assert all(email not in bucket.key_digest for bucket in buckets)
        assert all("198.51.100.10" not in bucket.key_digest for bucket in buckets)


def test_rotating_ips_trigger_capped_account_backpressure_but_correct_password_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distributed failures delay verification; they never create an account 429."""

    settings = _settings(
        tmp_path,
        login_rate_limit_client_limit=100,
        login_rate_limit_email_limit=100,
        login_account_backpressure_free_failures=1,
        login_account_backpressure_base_delay_seconds=0.25,
        login_account_backpressure_max_delay_seconds=0.5,
    )
    app = create_app(settings)
    observed: list[tuple[str, float | None]] = []
    original_authenticate = main_module.authenticate_email_password

    async def capture_sleep(seconds: float) -> None:
        observed.append(("sleep", seconds))

    def capture_authenticate(*args: object, **kwargs: object):
        observed.append(("verify", None))
        return original_authenticate(*args, **kwargs)

    monkeypatch.setattr(main_module.asyncio, "sleep", capture_sleep)
    monkeypatch.setattr(main_module, "authenticate_email_password", capture_authenticate)

    with TestClient(app, client=("127.0.0.1", 2015)) as client:
        email = "rotating-ip-account@example.test"
        password = "rotating-ip-correct-password"
        _register_and_verify(client, email=email, password=password)

        for source in ("198.51.100.31", "203.0.113.31"):
            wrong = client.post(
                "/v1/auth/login",
                headers={"x-forwarded-for": source},
                json={"email": email, "password": "wrong-password"},
            )
            assert wrong.status_code == 401, wrong.text

        # The third attempt comes from a fresh client identity, so the normal
        # per-client limiter cannot explain this delay. The async delay occurs
        # before the password verifier and a correct password still succeeds.
        correct = client.post(
            "/v1/auth/login",
            headers={"x-forwarded-for": "192.0.2.31"},
            json={"email": email, "password": password},
        )
        assert correct.status_code == 200, correct.text
        assert observed[-2:] == [("sleep", 0.25), ("verify", None)]

        with app.state.database.session_factory() as session:
            assert session.scalars(
                select(RegistrationRateLimitBucket).where(
                    RegistrationRateLimitBucket.scope == "login_account_backpressure"
                )
            ).all() == []

        # After success clears the bounded counter, a fresh wrong attempt does
        # not inherit a delay from the earlier distributed attack.
        observed.clear()
        after_success = client.post(
            "/v1/auth/login",
            headers={"x-forwarded-for": "192.0.2.32"},
            json={"email": email, "password": "wrong-password"},
        )
        assert after_success.status_code == 401, after_success.text
        assert observed == [("verify", None)]


def test_unknown_and_inactive_logins_perform_dummy_scrypt_before_shared_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither account state may skip password work and reveal an existence timing gap."""

    app = create_app(_settings(tmp_path))
    observed_hashes: list[str] = []
    original_verify_password = identity_service.verify_password

    def capture_verify_password(password: str, password_hash: str) -> bool:
        observed_hashes.append(password_hash)
        return original_verify_password(password, password_hash)

    with TestClient(app) as client:
        email = "inactive-login-timing@example.test"
        _register_and_verify(client, email=email, password="inactive-login-password")
        monkeypatch.setattr(identity_service, "verify_password", capture_verify_password)

        unknown = client.post(
            "/v1/auth/login",
            json={"email": "unknown-login-timing@example.test", "password": "wrong-password"},
        )

        with app.state.database.session_factory() as session:
            user = session.scalar(select(UserAccount).where(UserAccount.email == email))
            assert user is not None
            user.is_active = False
            session.commit()

        inactive = client.post(
            "/v1/auth/login",
            json={"email": email, "password": "wrong-password"},
        )

    assert unknown.status_code == inactive.status_code == 401
    assert unknown.json() == inactive.json() == {"detail": "invalid_login_credentials"}
    assert observed_hashes == [
        identity_service._LOGIN_DUMMY_PASSWORD_HASH,
        identity_service._LOGIN_DUMMY_PASSWORD_HASH,
    ]


def test_public_auth_limit_settings_are_explicit_and_validated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_LIMIT", "6")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS", "901")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_LIMIT", "4")
    monkeypatch.setenv("RESUME_V3_LOGIN_RATE_LIMIT_EMAIL_WINDOW_SECONDS", "901")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_MIN_RESPONSE_SECONDS", "0.4")
    monkeypatch.setenv("RESUME_V3_PASSWORD_RESET_RESPONSE_JITTER_SECONDS", "0.1")

    loaded = AppSettings.from_env()
    assert not hasattr(loaded, "admin_token")
    assert not hasattr(loaded, "legacy_admin_token_enabled")
    assert loaded.login_rate_limit_client_limit == 6
    assert loaded.login_rate_limit_client_window_seconds == 901
    assert loaded.login_rate_limit_email_limit == 4
    assert loaded.login_rate_limit_email_window_seconds == 901
    assert loaded.password_reset_min_response_seconds == 0.4
    assert loaded.password_reset_response_jitter_seconds == 0.1
    assert not hasattr(loaded, "login_rate_limit_global_limit")

    with pytest.raises(ValueError, match="LOGIN_RATE_LIMIT_EMAIL_LIMIT"):
        replace(_settings(tmp_path), login_rate_limit_email_limit=0).validate_runtime()
    with pytest.raises(ValueError, match="LOGIN_RATE_LIMIT_CLIENT_WINDOW_SECONDS"):
        replace(_settings(tmp_path), login_rate_limit_client_window_seconds=59).validate_runtime()
    with pytest.raises(ValueError, match="PASSWORD_RESET_MIN_RESPONSE_SECONDS"):
        replace(_settings(tmp_path), password_reset_min_response_seconds=0.04).validate_runtime()
