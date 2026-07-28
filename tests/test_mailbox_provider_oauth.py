from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import AppSettings
from app.main import create_app
from app.models import MailboxConfig, MailboxOAuthConnectIntent, MailboxOAuthCredential
from app.services import mailbox_import_service
from app.services.identity_service import legacy_principal
from app.services.mailbox_import_service import MailboxImportError
from app.services.mailbox_oauth_service import MailboxOAuthError, OAuthAccessTokenRefresh
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context


@pytest.fixture
def oauth_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
        public_app_url="https://testserver",
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-tests",
        mailbox_google_oauth_client_secret="google-client-secret-for-tests",
        mailbox_google_oauth_redirect_uri="https://testserver/v1/mailbox-oauth/callback",
    )
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
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
        public_app_url="https://testserver",
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-tests",
        mailbox_google_oauth_client_secret="google-client-secret-for-tests",
        mailbox_google_oauth_redirect_uri="https://testserver/v1/mailbox-oauth/callback",
    )
    app = create_app(settings)
    with TestClient(app):
        client_a = TestClient(app, base_url="https://testserver")
        client_b = TestClient(app, base_url="https://testserver")
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


def _copy_mailbox_oauth_correlation_cookie(
    source: TestClient,
    target: TestClient,
) -> None:
    """Model a provider callback that has no SameSite=Strict app session."""

    for cookie in source.cookies.jar:
        if cookie.name == "__Secure-resume_v3_mailbox_oauth":
            target.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )
            return
    raise AssertionError("mailbox OAuth correlation cookie was not issued")


def test_provider_catalog_exposes_fixed_presets_and_generic_imap_metadata(client) -> None:
    response = client.get("/v1/mailbox-providers")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["provider_key"] for item in payload["items"]] == [
        "feishu_app_password",
        "tencent_exmail_app_password",
        "qq_mail_app_password",
        "gmail_oauth",
        "microsoft_oauth",
        "generic_imap",
    ]
    feishu = payload["items"][0]
    assert feishu["available"] is True
    assert feishu["authentication_mode"] == "app_password"
    tencent_exmail = next(
        item for item in payload["items"] if item["provider_key"] == "tencent_exmail_app_password"
    )
    assert tencent_exmail["display_name"] == "腾讯企业邮箱"
    gmail = next(item for item in payload["items"] if item["provider_key"] == "gmail_oauth")
    assert gmail["available"] is False
    assert gmail["authentication_mode"] == "oauth2"
    generic = next(item for item in payload["items"] if item["provider_key"] == "generic_imap")
    assert generic["display_name"] == "通用 IMAP 邮箱"
    assert generic["authentication_mode"] == "app_password"
    assert generic["available"] is True
    assert generic["imap_host"] is None
    assert generic["imap_port"] == 993
    assert generic["allows_custom_endpoint"] is True
    # The authentication *mode* is intentionally public (for example,
    # ``app_password``), but the catalogue must never return a credential,
    # OAuth client secret, or an authorization token.
    sensitive_keys = {"password", "token", "secret", "client_secret"}
    assert not sensitive_keys.intersection(
        key
        for item in payload["items"]
        for key in item
    )


def test_generic_imap_provider_binds_a_custom_domain_with_fixed_imaps(
    client: TestClient,
    monkeypatch,
) -> None:
    """The generic product path is explicit, encrypted and never a raw API fallback."""

    monkeypatch.setattr(
        mailbox_import_service,
        "_read_initial_mailbox_watermark",
        lambda **_: (9, 42),
    )
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "海外招聘邮箱",
            "provider_key": "generic_imap",
            "imap_host": "imap.corporate-mail.example",
            "imap_port": 993,
            "email_address": "recruiting@corporate-mail.example",
            "mailbox": "INBOX",
            "password": "test-only-authorization-code",
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["provider_key"] == "generic_imap"
    assert payload["provider_display_name"] == "通用 IMAP 邮箱"
    assert payload["authentication_mode"] == "app_password"
    assert payload["imap_host"] == "imap.corporate-mail.example"
    assert payload["imap_port"] == 993
    assert payload["password_configured"] is True
    assert "password" not in payload


def test_generic_imap_connection_factory_is_the_only_custom_host_escape_hatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fixed and legacy callers must retain the exact-host default guard."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
    )
    calls: list[dict[str, object]] = []

    def create_client_stub(*args, **kwargs):
        del args
        calls.append(dict(kwargs))
        return object()

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", create_client_stub)

    mailbox_import_service._create_imap_client_for_provider(
        settings,
        provider_key="generic_imap",
        host="imap.corporate-mail.example",
        port=993,
    )
    mailbox_import_service._create_imap_client_for_provider(
        settings,
        provider_key="feishu_app_password",
        host="imap.feishu.cn",
        port=993,
    )
    mailbox_import_service._create_imap_client_for_provider(
        settings,
        provider_key="legacy_imap",
        host="imap.feishu.cn",
        port=993,
    )

    assert calls == [
        {
            "host": "imap.corporate-mail.example",
            "port": 993,
            "allow_custom_host": True,
        },
        {"host": "imap.feishu.cn", "port": 993},
        {"host": "imap.feishu.cn", "port": 993},
    ]


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (
            {
                "provider_key": "generic_imap",
                "imap_port": 993,
            },
            "mailbox_imap_host_required",
        ),
        (
            {
                "provider_key": "generic_imap",
                "imap_host": "imap.corporate-mail.example",
                "imap_port": 143,
            },
            "mailbox_imap_port_not_allowed",
        ),
        (
            {
                "provider_key": "generic_imap",
                "imap_host": "127.0.0.1",
                "imap_port": 993,
            },
            "mailbox_imap_host_not_allowed",
        ),
    ],
)
def test_generic_imap_provider_rejects_unsafe_endpoint_inputs(
    client: TestClient,
    payload: dict[str, object],
    error_code: str,
) -> None:
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "通用安全测试",
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-only-authorization-code",
            **payload,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == error_code


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
            "initial_sync_lookback_days": 7,
        },
    )
    assert start.status_code == 200, start.text
    authorization_url = start.json()["authorization_url"]
    query = parse_qs(urlsplit(authorization_url).query)
    state = query["state"][0]
    assert "google-client-secret-for-tests" not in start.text
    assert "refresh-token-for-test-only" not in start.text
    assert "access-token-for-test-only" not in start.text
    assert start.headers["cache-control"] == "no-store, private"
    assert start.headers["referrer-policy"] == "no-referrer"
    assert any(
        "__Secure-resume_v3_mailbox_oauth=" in header
        and "httponly" in header.casefold()
        and "secure" in header.casefold()
        and "samesite=lax" in header.casefold()
        for header in start.headers.get_list("set-cookie")
    )

    with oauth_client.app.state.database.session_factory() as session:
        intent = session.scalar(select(MailboxOAuthConnectIntent))
        assert intent is not None
        # The initial history choice must survive the external OAuth redirect;
        # it is later frozen into the newly bound mailbox configuration.
        assert intent.initial_sync_lookback_days == 7
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
    assert callback.headers["cache-control"] == "no-store, private"
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert any(
        "resume_v3_session=" in header and "samesite=strict" in header.casefold()
        for header in callback.headers.get_list("set-cookie")
    )
    assert any(
        "__Secure-resume_v3_mailbox_oauth=" in header
        and "max-age=0" in header.casefold()
        for header in callback.headers.get_list("set-cookie")
    )
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
        assert config.initial_sync_lookback_days == 7
        assert config.initial_backfill_since_date is not None
        assert config.initial_backfill_completed_at is None
        assert credential.encrypted_refresh_token != "refresh-token-for-test-only"

    replay = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "provider-authorization-code"},
        follow_redirects=False,
    )
    assert replay.status_code == 303, replay.text
    assert "mailbox_oauth=failed" in replay.headers["location"]


def test_oauth_reauthorization_uses_callback_binding_and_replaces_credentials(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    class OAuthImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "OK", [b"authenticated"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    authorization_codes: list[str] = []

    def exchange_code(*args, **kwargs) -> str:
        authorization_code = str(kwargs["code"])
        authorization_codes.append(authorization_code)
        return {
            "initial-code": "initial-refresh-token",
            "reauthorize-code": "reauthorize-refresh-token",
        }[authorization_code]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", OAuthImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        exchange_code,
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: OAuthAccessTokenRefresh(
            access_token="access-token-for-reauthorization-test",
            replacement_refresh_token={
                "initial-refresh-token": "initial-rotated-refresh-token",
                "reauthorize-refresh-token": "reauthorize-rotated-refresh-token",
            }[str(kwargs["refresh_token"])],
        ),
    )

    initial_start = oauth_client.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Google reauthorization test",
            "email_address": "reauthorize@example.test",
            "mailbox": "INBOX",
        },
    )
    assert initial_start.status_code == 200, initial_start.text
    initial_state = parse_qs(urlsplit(initial_start.json()["authorization_url"]).query)[
        "state"
    ][0]
    initial_callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": initial_state, "code": "initial-code"},
        follow_redirects=False,
    )
    assert initial_callback.status_code == 303, initial_callback.text

    mailbox_id = oauth_client.get("/v1/mailboxes").json()["items"][0]["mailbox_id"]
    with oauth_client.app.state.database.session_factory() as session:
        config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_id))
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == mailbox_id
            )
        )
        assert config is not None
        assert credential is not None
        original_watermark = (config.import_start_uid, config.imap_uidvalidity)
        original_encrypted_refresh_token = credential.encrypted_refresh_token
        credential.reauthorization_required_at = datetime.now(timezone.utc)
        credential.last_error_code = "mailbox_oauth_reauthorization_required"
        session.commit()

    missing_cookie_start = oauth_client.post(
        f"/v1/mailboxes/{mailbox_id}/oauth/reauthorize"
    )
    assert missing_cookie_start.status_code == 200, missing_cookie_start.text
    missing_cookie_state = parse_qs(
        urlsplit(missing_cookie_start.json()["authorization_url"]).query
    )["state"][0]
    assert missing_cookie_start.headers["cache-control"] == "no-store, private"
    assert any(
        "__Secure-resume_v3_mailbox_oauth=" in header
        and "httponly" in header.casefold()
        and "secure" in header.casefold()
        and "samesite=lax" in header.casefold()
        for header in missing_cookie_start.headers.get_list("set-cookie")
    )
    oauth_client.cookies.clear()
    missing_cookie_callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": missing_cookie_state, "code": "must-not-be-exchanged"},
        follow_redirects=False,
    )
    assert missing_cookie_callback.status_code == 303, missing_cookie_callback.text
    assert "mailbox_oauth=failed" in missing_cookie_callback.headers["location"]
    assert authorization_codes == ["initial-code"]
    with oauth_client.app.state.database.session_factory() as session:
        pending = session.scalar(
            select(MailboxOAuthConnectIntent).where(
                MailboxOAuthConnectIntent.state_hash
                == mailbox_import_service.hashlib.sha256(
                    missing_cookie_state.encode("utf-8")
                ).hexdigest()
            )
        )
        assert pending is not None
        assert pending.consumed_at is None

    reauthorize_start = oauth_client.post(
        f"/v1/mailboxes/{mailbox_id}/oauth/reauthorize"
    )
    assert reauthorize_start.status_code == 200, reauthorize_start.text
    reauthorize_state = parse_qs(
        urlsplit(reauthorize_start.json()["authorization_url"]).query
    )["state"][0]
    reauthorize_callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": reauthorize_state, "code": "reauthorize-code"},
        follow_redirects=False,
    )
    assert reauthorize_callback.status_code == 303, reauthorize_callback.text
    assert "mailbox_oauth=connected" in reauthorize_callback.headers["location"]
    assert authorization_codes == ["initial-code", "reauthorize-code"]
    configured_mailboxes = oauth_client.get("/v1/mailboxes")
    assert configured_mailboxes.status_code == 200, configured_mailboxes.text
    assert configured_mailboxes.json()["total"] == 1
    assert configured_mailboxes.json()["items"][0]["mailbox_id"] == mailbox_id

    with oauth_client.app.state.database.session_factory() as session:
        config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_id))
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == mailbox_id
            )
        )
        assert config is not None
        assert credential is not None
        assert (config.import_start_uid, config.imap_uidvalidity) == original_watermark
        assert credential.encrypted_refresh_token != original_encrypted_refresh_token
        assert (
            mailbox_import_service._decrypt_mailbox_secret(
                oauth_client.app.state.settings,
                credential.encrypted_refresh_token,
            )
            == "reauthorize-rotated-refresh-token"
        )
        assert credential.reauthorization_required_at is None
        assert credential.last_error_code is None
        intent_count_before_wrong_origin = session.scalar(
            select(func.count()).select_from(MailboxOAuthConnectIntent)
        )

    wrong_origin = oauth_client.post(
        f"/v1/mailboxes/{mailbox_id}/oauth/reauthorize",
        headers={"host": "wrong.example.test"},
    )
    assert wrong_origin.status_code == 422, wrong_origin.text
    assert wrong_origin.json()["detail"] == "mailbox_oauth_callback_origin_invalid"
    with oauth_client.app.state.database.session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(MailboxOAuthConnectIntent)
            )
            == intent_count_before_wrong_origin
        )


def test_reauthorization_persists_rotated_token_when_imap_verification_fails(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    class DenyingImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "NO", [b"denied"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    database = oauth_client.app.state.database
    settings = oauth_client.app.state.settings
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = MailboxConfig(
            display_name="Failed reauthorization test",
            display_name_key="failed reauthorization test",
            provider_key="gmail_oauth",
            authentication_mode="oauth2",
            imap_host="imap.gmail.com",
            imap_port=993,
            email_address="failed-reauthorize@example.test",
            mailbox="INBOX",
            encrypted_password=None,
            enabled=True,
            import_start_uid=42,
            imap_uidvalidity=9,
        )
        session.add(config)
        session.flush()
        session.add(
            MailboxOAuthCredential(
                organization_id=LEGACY_ORGANIZATION_ID,
                mailbox_config_id=config.id,
                encrypted_refresh_token=mailbox_import_service._encrypt_mailbox_secret(
                    settings,
                    "old-refresh-token",
                ),
                reauthorization_required_at=datetime.now(timezone.utc),
                last_error_code="mailbox_oauth_reauthorization_required",
            )
        )
        session.commit()
        mailbox_id = config.id

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", DenyingImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: "reauthorization-refresh-token",
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: OAuthAccessTokenRefresh(
            access_token="access-token-for-failed-reauthorization",
            replacement_refresh_token="rotated-refresh-token",
        ),
    )

    started = oauth_client.post(f"/v1/mailboxes/{mailbox_id}/oauth/reauthorize")
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "provider-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert "mailbox_oauth=failed" in callback.headers["location"]

    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == mailbox_id
            )
        )
        assert credential is not None
        assert (
            mailbox_import_service._decrypt_mailbox_secret(
                settings,
                credential.encrypted_refresh_token,
            )
            == "rotated-refresh-token"
        )
        assert credential.reauthorization_required_at is not None
        assert credential.last_error_code == "mailbox_oauth_reauthorization_required"


def test_oauth_callback_requires_signed_correlation_cookie(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    exchanges: list[str] = []
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: exchanges.append(str(kwargs["code"]))
        or "refresh-token-for-missing-cookie-test",
    )

    started = oauth_client.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Google callback binding test",
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
        },
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

    # A provider callback is deliberately unauthenticated by the normal strict
    # session cookie. Removing the short-lived signed binding must therefore
    # fail before the authorization code exchange or intent consumption.
    oauth_client.cookies.clear()
    callback = oauth_client.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state, "code": "must-not-be-exchanged"},
        follow_redirects=False,
    )

    assert callback.status_code == 303, callback.text
    assert "mailbox_oauth=failed" in callback.headers["location"]
    assert exchanges == []
    with oauth_client.app.state.database.session_factory() as session:
        intent = session.scalar(select(MailboxOAuthConnectIntent))
        assert intent is not None
        assert intent.consumed_at is None
    assert callback.headers["cache-control"] == "no-store, private"
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert any(
        "__Secure-resume_v3_mailbox_oauth=" in header
        and "max-age=0" in header.casefold()
        for header in callback.headers.get_list("set-cookie")
    )


def test_logout_revokes_only_its_pending_oauth_intent(
    oauth_workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    """A retained OAuth callback cookie must not recreate a logged-out session."""

    client_a, client_b = oauth_workspace_clients
    _register_and_login(
        client_a,
        organization_name="Logout OAuth Alpha",
        email="logout-oauth-alpha@example.test",
        password="logout-oauth-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Logout OAuth Beta",
        email="logout-oauth-beta@example.test",
        password="logout-oauth-test-password-b",
    )

    exchanges: list[str] = []
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: exchanges.append(str(kwargs["code"]))
        or "must-not-be-used-after-logout",
    )

    started_a = client_a.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Logout OAuth Alpha mailbox",
            "email_address": "logout-oauth-alpha@example.test",
            "mailbox": "INBOX",
        },
    )
    started_b = client_b.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Logout OAuth Beta mailbox",
            "email_address": "logout-oauth-beta@example.test",
            "mailbox": "INBOX",
        },
    )
    assert started_a.status_code == 200, started_a.text
    assert started_b.status_code == 200, started_b.text
    state_a = parse_qs(urlsplit(started_a.json()["authorization_url"]).query)["state"][0]
    state_b = parse_qs(urlsplit(started_b.json()["authorization_url"]).query)["state"][0]

    logged_out = client_a.post("/v1/auth/logout")
    assert logged_out.status_code == 204, logged_out.text

    database = client_a.app.state.database
    with database.session_factory() as session:
        intents = {
            intent.state_hash: intent
            for intent in session.scalars(
                select(MailboxOAuthConnectIntent)
                .where(
                    MailboxOAuthConnectIntent.state_hash.in_(
                        {
                            hashlib.sha256(state_a.encode("utf-8")).hexdigest(),
                            hashlib.sha256(state_b.encode("utf-8")).hexdigest(),
                        }
                    )
                )
                .execution_options(skip_organization_scope=True)
            )
        }
        assert intents[hashlib.sha256(state_a.encode("utf-8")).hexdigest()].consumed_at is not None
        assert intents[hashlib.sha256(state_b.encode("utf-8")).hexdigest()].consumed_at is None

    callback = client_a.get(
        "/v1/mailbox-oauth/callback",
        params={"state": state_a, "code": "must-not-be-exchanged"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert "mailbox_oauth=failed" in callback.headers["location"]
    assert exchanges == []
    assert client_a.get("/v1/auth/session").json()["authenticated"] is False
    assert client_a.get("/v1/mailboxes").status_code == 401


def test_logout_invalidates_every_existing_browser_session_for_that_account(
    oauth_workspace_clients: tuple[TestClient, TestClient],
) -> None:
    """OAuth callback safety relies on the same account-wide version guard."""

    client_a, client_b = oauth_workspace_clients
    email = "logout-all-sessions@example.test"
    password = "logout-all-sessions-password"
    _register_and_login(
        client_a,
        organization_name="Logout All Sessions",
        email=email,
        password=password,
    )
    second_browser_login = client_b.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert second_browser_login.status_code == 200, second_browser_login.text
    assert client_b.get("/v1/auth/session").json()["authenticated"] is True

    assert client_a.post("/v1/auth/logout").status_code == 204

    # The stale signed cookie remains in client_b's jar, but the server rejects
    # it after the account session version advances.
    assert client_b.get("/v1/auth/session").json()["authenticated"] is False
    assert client_b.get("/v1/mailboxes").status_code == 401


def test_failed_oauth_callback_never_issues_a_browser_session(
    oauth_workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    """A provider exchange failure cannot turn correlation state into login."""

    client_a, _ = oauth_workspace_clients
    _register_and_login(
        client_a,
        organization_name="Failed OAuth Callback",
        email="failed-oauth-callback@example.test",
        password="failed-oauth-callback-password",
    )
    started = client_a.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Failed OAuth callback mailbox",
            "email_address": "failed-oauth-callback@example.test",
            "mailbox": "INBOX",
        },
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            MailboxOAuthError("mailbox_oauth_token_exchange_failed")
        ),
    )

    callback_browser = TestClient(client_a.app, base_url="https://testserver")
    try:
        _copy_mailbox_oauth_correlation_cookie(client_a, callback_browser)
        callback = callback_browser.get(
            "/v1/mailbox-oauth/callback",
            params={"state": state, "code": "failed-provider-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert "mailbox_oauth=failed" in callback.headers["location"]
        assert not any(
            header.startswith("resume_v3_session=")
            for header in callback.headers.get_list("set-cookie")
        )
        assert callback_browser.get("/v1/auth/session").json()["authenticated"] is False
    finally:
        callback_browser.close()


def test_logout_during_oauth_callback_cannot_restore_a_browser_session(
    oauth_workspace_clients: tuple[TestClient, TestClient],
    monkeypatch,
) -> None:
    """A claimed intent still fails closed when another browser logs out."""

    client_a, client_b = oauth_workspace_clients
    email = "oauth-callback-race@example.test"
    password = "oauth-callback-race-password"
    _register_and_login(
        client_a,
        organization_name="OAuth Callback Race",
        email=email,
        password=password,
    )
    assert client_b.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    ).status_code == 200
    started = client_a.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "OAuth callback race mailbox",
            "email_address": email,
            "mailbox": "INBOX",
        },
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

    def exchange_then_logout(*args, **kwargs) -> str:
        logged_out = client_b.post("/v1/auth/logout")
        assert logged_out.status_code == 204, logged_out.text
        return "refresh-token-after-logout"

    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        exchange_then_logout,
    )
    callback_browser = TestClient(client_a.app, base_url="https://testserver")
    try:
        _copy_mailbox_oauth_correlation_cookie(client_a, callback_browser)
        callback = callback_browser.get(
            "/v1/mailbox-oauth/callback",
            params={"state": state, "code": "race-provider-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert "mailbox_oauth=failed" in callback.headers["location"]
        assert not any(
            header.startswith("resume_v3_session=")
            for header in callback.headers.get_list("set-cookie")
        )
        assert callback_browser.get("/v1/auth/session").json()["authenticated"] is False
    finally:
        callback_browser.close()

    assert client_a.get("/v1/auth/session").json()["authenticated"] is False
    assert client_b.get("/v1/auth/session").json()["authenticated"] is False


def test_late_reauthorization_callback_cannot_replace_newer_generation(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    """An older OAuth tab must lose its final write to the newest tab."""

    class OAuthImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "OK", [b"authenticated"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    database = oauth_client.app.state.database
    settings = oauth_client.app.state.settings
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = MailboxConfig(
            display_name="Concurrent OAuth reauthorization",
            display_name_key="concurrent oauth reauthorization",
            provider_key="gmail_oauth",
            authentication_mode="oauth2",
            imap_host="imap.gmail.com",
            imap_port=993,
            email_address="concurrent-reauthorization@example.test",
            mailbox="INBOX",
            encrypted_password=None,
            enabled=True,
            import_start_uid=42,
            imap_uidvalidity=9,
        )
        session.add(config)
        session.flush()
        session.add(
            MailboxOAuthCredential(
                organization_id=LEGACY_ORGANIZATION_ID,
                mailbox_config_id=config.id,
                encrypted_refresh_token=mailbox_import_service._encrypt_mailbox_secret(
                    settings,
                    "original-refresh-token",
                ),
                reauthorization_required_at=datetime.now(timezone.utc),
                last_error_code="mailbox_oauth_reauthorization_required",
            )
        )
        session.commit()
        mailbox_id = config.id

    authorization_codes: list[str] = []
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", OAuthImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: authorization_codes.append(str(kwargs["code"]))
        or {
            "older-code": "older-refresh-token",
            "newer-code": "newer-refresh-token",
        }[str(kwargs["code"])],
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: OAuthAccessTokenRefresh(
            access_token="access-token-for-concurrent-reauthorization",
        ),
    )

    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        principal = legacy_principal(session)
        older_start = mailbox_import_service.start_mailbox_oauth_reauthorization(
            session,
            settings=settings,
            principal=principal,
            config_id=mailbox_id,
        )
        newer_start = mailbox_import_service.start_mailbox_oauth_reauthorization(
            session,
            settings=settings,
            principal=principal,
            config_id=mailbox_id,
        )
        older_state = parse_qs(urlsplit(older_start.authorization_url).query)["state"][0]
        newer_state = parse_qs(urlsplit(newer_start.authorization_url).query)["state"][0]

        completed = mailbox_import_service.complete_mailbox_oauth_connection(
            session,
            settings=settings,
            principal=principal,
            state=newer_state,
            code="newer-code",
            callback_is_still_current=lambda: True,
        )
        assert completed.mailbox_id == mailbox_id
        with pytest.raises(MailboxImportError, match="mailbox_oauth_callback_invalid"):
            mailbox_import_service.complete_mailbox_oauth_connection(
                session,
                settings=settings,
                principal=principal,
                state=older_state,
                code="older-code",
                callback_is_still_current=lambda: True,
            )

    assert authorization_codes == ["newer-code", "older-code"]
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_id))
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == mailbox_id
            )
        )
        assert config is not None
        assert credential is not None
        assert config.oauth_reauthorization_generation == 2
        assert (
            mailbox_import_service._decrypt_mailbox_secret(
                settings,
                credential.encrypted_refresh_token,
            )
            == "newer-refresh-token"
        )
        assert credential.reauthorization_required_at is None
        assert credential.last_error_code is None


def test_oauth_start_rejects_callback_origin_mismatch(oauth_client: TestClient) -> None:
    response = oauth_client.post(
        "/v1/mailbox-oauth/start",
        headers={"host": "wrong.example.test"},
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Invalid callback origin",
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_oauth_callback_origin_invalid"
    with oauth_client.app.state.database.session_factory() as session:
        assert session.scalar(select(MailboxOAuthConnectIntent)) is None


def test_oauth_start_rejects_non_inbox_without_creating_intent(
    oauth_client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mailbox_import_service,
        "authorization_url",
        lambda *args, **kwargs: pytest.fail("a non-INBOX channel must not start OAuth"),
    )

    response = oauth_client.post(
        "/v1/mailbox-oauth/start",
        json={
            "provider_key": "gmail_oauth",
            "display_name": "Non-INBOX OAuth test",
            "email_address": "recruiting@example.test",
            "mailbox": "Archive",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_folder_fixed_to_inbox"
    with oauth_client.app.state.database.session_factory() as session:
        assert session.scalar(select(MailboxOAuthConnectIntent)) is None


def test_oauth_compatibility_entry_can_finish_on_canonical_callback_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A parent compatibility host may safely hand off to the canonical host."""

    class OAuthImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "OK", [b"authenticated"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
        public_app_url="https://hr.greatsellai.net",
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-compat-test",
        mailbox_google_oauth_client_secret="google-client-secret-for-compat-test",
        mailbox_google_oauth_redirect_uri=(
            "https://hr.greatsellai.net/v1/mailbox-oauth/callback"
        ),
    )
    app = create_app(settings)
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", OAuthImap)
    monkeypatch.setattr(
        mailbox_import_service,
        "exchange_authorization_code",
        lambda *args, **kwargs: "refresh-token-for-compat-test",
    )
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: "access-token-for-compat-test",
    )

    with TestClient(app, base_url="https://greatsellai.net") as client:
        started = client.post(
            "/v1/mailbox-oauth/start",
            json={
                "provider_key": "gmail_oauth",
                "display_name": "Compatibility mailbox",
                "email_address": "recruiting@example.test",
                "mailbox": "INBOX",
            },
        )
        assert started.status_code == 200, started.text
        assert any(
            "__Secure-resume_v3_mailbox_oauth=" in header
            and "domain=greatsellai.net" in header.casefold()
            for header in started.headers.get_list("set-cookie")
        )
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]

        callback = client.get(
            "https://hr.greatsellai.net/v1/mailbox-oauth/callback",
            params={"state": state, "code": "compat-provider-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303, callback.text
    assert callback.headers["location"].startswith(
        "https://hr.greatsellai.net/?mailbox_oauth=connected"
    )
    assert any(
        "resume_v3_session=" in header and "samesite=strict" in header.casefold()
        for header in callback.headers.get_list("set-cookie")
    )


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
