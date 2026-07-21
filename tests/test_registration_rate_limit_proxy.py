from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import RegistrationRateLimitBucket


def _registration_payload(email: str) -> dict[str, str]:
    return {
        "organization_name": "Proxy rate-limit fixture",
        "full_name": "Synthetic administrator",
        "email": email,
        "password": "proxy-rate-limit-test-password",
    }


def test_caddy_forwarded_clients_receive_distinct_persisted_registration_buckets(
    tmp_path: Path,
) -> None:
    """A trusted Caddy peer must not collapse all browsers into one bucket.

    ``TestClient.client`` is the direct TCP peer seen by ASGI.  This exercises
    the actual registration endpoint rather than only constructing a Request
    object, with Caddy's last-appended client address represented by the last
    X-Forwarded-For component.
    """

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        session_secret="proxy-rate-limit-session-secret",
        transactional_email_provider="test",
        public_app_url="http://testserver",
        allow_unauthenticated=False,
        trusted_proxy_cidrs=("127.0.0.1/32",),
        registration_rate_limit_global_limit=100,
        registration_rate_limit_global_window_seconds=60 * 60,
        registration_rate_limit_client_limit=1,
        registration_rate_limit_client_window_seconds=60 * 60,
        registration_rate_limit_email_limit=100,
        registration_rate_limit_email_window_seconds=24 * 60 * 60,
    )
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 2015)) as client:
        first = client.post(
            "/v1/auth/register",
            headers={"x-forwarded-for": "198.51.100.71, 198.51.100.10"},
            json=_registration_payload("first-proxy-rate-limit@example.test"),
        )
        second = client.post(
            "/v1/auth/register",
            headers={"x-forwarded-for": "203.0.113.19, 203.0.113.20"},
            json=_registration_payload("second-proxy-rate-limit@example.test"),
        )
        repeated_first = client.post(
            "/v1/auth/register",
            headers={"x-forwarded-for": "spoofed-prefix, 198.51.100.10"},
            json=_registration_payload("third-proxy-rate-limit@example.test"),
        )

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert repeated_first.status_code == 429, repeated_first.text
        assert repeated_first.json()["detail"] == "registration_rate_limit_exceeded"

        with app.state.database.session_factory() as session:
            client_buckets = session.scalars(
                select(RegistrationRateLimitBucket).where(
                    RegistrationRateLimitBucket.scope == "registration_client"
                )
            ).all()
        assert len(client_buckets) == 2
        assert sorted(bucket.request_count for bucket in client_buckets) == [1, 1]
