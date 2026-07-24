from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import MailboxConfig, MailboxOAuthConnectIntent, MailboxOAuthCredential
from app.services import mailbox_import_service


@pytest.fixture
def oauth_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-tests",
        mailbox_google_oauth_client_secret="google-client-secret-for-tests",
        mailbox_google_oauth_redirect_uri="http://testserver/v1/mailbox-oauth/callback",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def provider_change_client(tmp_path: Path) -> Iterator[TestClient]:
    """A reviewed-provider test client with every target endpoint enabled."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
        mailbox_imap_allowed_hosts=(
            "imap.feishu.cn",
            "imap.exmail.qq.com",
            "imap.gmail.com",
        ),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def oauth_workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two authenticated browser sessions sharing one isolated database."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="mailbox-oauth-tenant-test-session-secret",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-tests",
        mailbox_google_oauth_client_secret="google-client-secret-for-tests",
        mailbox_google_oauth_redirect_uri="http://testserver/v1/mailbox-oauth/callback",
    )
    app = create_app(settings)
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
    email: str,
    password: str,
) -> None:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": "OAuth Test Admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    provider = client.app.state.transactional_email_provider
    delivery = next(item for item in reversed(provider.deliveries) if item.recipient == email)
    verification_token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post(
        "/v1/auth/email-verification/complete",
        json={"token": verification_token},
    )
    assert verified.status_code == 200, verified.text
    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text


def test_provider_catalog_exposes_only_reviewed_preset_metadata(client) -> None:
    response = client.get("/v1/mailbox-providers")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["provider_key"] for item in payload["items"]] == [
        "feishu_app_password",
        "tencent_exmail_app_password",
        "qq_mail_app_password",
        "gmail_oauth",
        "microsoft_oauth",
    ]
    feishu = payload["items"][0]
    assert feishu["available"] is True
    assert feishu["authentication_mode"] == "app_password"
    gmail = next(item for item in payload["items"] if item["provider_key"] == "gmail_oauth")
    assert gmail["available"] is False
    assert gmail["authentication_mode"] == "oauth2"
    # The authentication *mode* is intentionally public (for example,
    # ``app_password``), but the catalogue must never return a credential,
    # OAuth client secret, or an authorization token.
    sensitive_keys = {"password", "token", "secret", "client_secret"}
    assert not sensitive_keys.intersection(
        key
        for item in payload["items"]
        for key in item
    )


def test_reviewed_provider_endpoint_cannot_be_overridden_by_a_browser(client) -> None:
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "受控端点测试",
            "provider_key": "feishu_app_password",
            "imap_host": "imap.unreviewed.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-only-authorization-code",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_provider_endpoint_mismatch"


def test_existing_app_password_mailbox_cannot_switch_provider_in_place(
    provider_change_client: TestClient,
    monkeypatch,
) -> None:
    class RecordingImap:
        opened_hosts: list[str] = []

        def __init__(self, host: str, *args, **kwargs) -> None:
            self.__class__.opened_hosts.append(host)

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", RecordingImap)
    created = provider_change_client.post(
        "/v1/mailboxes",
        json={
            "display_name": "飞书招聘邮箱",
            "provider_key": "feishu_app_password",
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-only-authorization-code",
        },
    )
    assert created.status_code == 201, created.text
    assert RecordingImap.opened_hosts == ["imap.feishu.cn"]
    RecordingImap.opened_hosts.clear()

    mailbox_id = created.json()["mailbox_id"]
    for provider_key in ("tencent_exmail_app_password", "gmail_oauth"):
        response = provider_change_client.patch(
            f"/v1/mailboxes/{mailbox_id}",
            json={"provider_key": provider_key},
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == (
            "mailbox_provider_change_requires_new_connection"
        )

    assert RecordingImap.opened_hosts == []
    current = provider_change_client.get(f"/v1/mailboxes/{mailbox_id}")
    assert current.status_code == 200, current.text
    assert current.json()["provider_key"] == "feishu_app_password"


def test_google_oauth_connection_is_one_time_and_never_returns_tokens(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    class OAuthImap:
        authentication_payload: bytes | None = None

        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            self.__class__.authentication_payload = callback(b"")
            return "OK", [b"authenticated"]

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            raise AssertionError("OAuth mailbox must not use IMAP LOGIN")

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", OAuthImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: "refresh-token-for-test-only",
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: "access-token-for-test-only",
    )

    start = oauth_client.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Google 招聘邮箱",
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
        },
    )
    assert start.status_code == 200, start.text
    authorization_url = start.json()["authorization_url"]
    query = parse_qs(urlsplit(authorization_url).query)
    state = query["state"][0]
    assert "google-client-secret-for-tests" not in start.text
    assert "refresh-token-for-test-only" not in start.text
    assert "access-token-for-test-only" not in start.text

    with oauth_client.app.state.database.session_factory() as session:
        intent = session.scalar(select(MailboxOAuthConnectIntent))
        assert intent is not None
        assert intent.state_hash != state
        assert state not in intent.encrypted_code_verifier

    callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "provider-authorization-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert "mailbox_oauth=connected" in callback.headers["location"]
    assert callback.headers["location"].endswith("#settings/mailbox")
    assert state not in callback.headers["location"]
    assert "provider-authorization-code" not in callback.headers["location"]
    assert "refresh-token-for-test-only" not in callback.headers["location"]
    assert "access-token-for-test-only" not in callback.headers["location"]
    assert b"auth=Bearer access-token-for-test-only" in OAuthImap.authentication_payload

    listed = oauth_client.get("/v1/mailboxes")
    assert listed.status_code == 200, listed.text
    mailbox = listed.json()["items"][0]
    assert mailbox["provider_key"] == "gmail_oauth"
    assert mailbox["provider_display_name"] == "Gmail / Google Workspace"
    assert mailbox["authentication_mode"] == "oauth2"
    assert mailbox["authorization_status"] == "connected"
    assert mailbox["password_configured"] is False
    assert "refresh-token-for-test-only" not in listed.text

    with oauth_client.app.state.database.session_factory() as session:
        config = session.scalar(select(MailboxConfig))
        credential = session.scalar(select(MailboxOAuthCredential))
        assert config is not None
        assert credential is not None
        assert config.encrypted_password is None
        assert credential.encrypted_refresh_token != "refresh-token-for-test-only"

    replay = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "provider-authorization-code"},
        follow_redirects=False,
    )
    assert replay.status_code == 303, replay.text
    assert "mailbox_oauth=failed" in replay.headers["location"]


def test_oauth_state_cannot_cross_workspaces_or_consume_another_admin_intent(
    oauth_workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    class OAuthImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "OK", [b"authenticated"]

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            raise AssertionError("OAuth mailbox must not use IMAP LOGIN")

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    client_a, client_b = oauth_workspace_clients
    _register_and_login(
        client_a,
        organization_name="OAuth Alpha",
        email="oauth-alpha@example.test",
        password="oauth-tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="OAuth Beta",
        email="oauth-beta@example.test",
        password="oauth-tenant-test-password-b",
    )

    exchanges: list[str] = []
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", OAuthImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: exchanges.append(str(kwargs["code"]))
        or "refresh-token-for-cross-workspace-test",
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: "access-token-for-cross-workspace-test",
    )

    started = client_a.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Alpha Google 招聘邮箱",
            "email_address": "alpha-recruiting@example.test",
            "mailbox": "INBOX",
        },
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

    foreign_callback = client_b.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "foreign-authorization-code"},
        follow_redirects=False,
    )
    assert foreign_callback.status_code == 303, foreign_callback.text
    assert "mailbox_oauth=failed" in foreign_callback.headers["location"]
    assert state not in foreign_callback.headers["location"]
    assert exchanges == []
    assert client_b.get("/v1/mailboxes").json() == {"items": [], "total": 0}

    owner_callback = client_a.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "owner-authorization-code"},
        follow_redirects=False,
    )
    assert owner_callback.status_code == 303, owner_callback.text
    assert "mailbox_oauth=connected" in owner_callback.headers["location"]
    assert owner_callback.headers["location"].endswith("#settings/mailbox")
    assert exchanges == ["owner-authorization-code"]
    mailboxes = client_a.get("/v1/mailboxes")
    assert mailboxes.status_code == 200, mailboxes.text
    assert [item["email_address"] for item in mailboxes.json()["items"]] == [
        "alpha-recruiting@example.test"
    ]
