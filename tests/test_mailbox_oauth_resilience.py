from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import func, select

from app.config import AppSettings
from app.models import (
    MailboxBackgroundJob,
    MailboxConfig,
    MailboxOAuthConnectIntent,
    MailboxOAuthCredential,
)
from app.services import mailbox_import_service, mailbox_oauth_service
from app.services.identity_service import DEVELOPMENT_MEMBERSHIP_ID, DEVELOPMENT_USER_ID
from app.services.mailbox_background_job_service import (
    _retryable_error,
    enqueue_due_mailbox_sync_jobs,
)
from app.services.mailbox_import_service import (
    MailboxImportError,
    cleanup_expired_mailbox_oauth_intents,
    sync_mailbox,
)
from app.services.mailbox_oauth_service import (
    MailboxOAuthError,
    OAuthAccessTokenRefresh,
    refresh_access_token,
)
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context


def _oauth_settings(tmp_path) -> AppSettings:
    return AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        mailbox_imap_allowed_hosts=("imap.gmail.com",),
        mailbox_google_oauth_client_id="google-client-id-for-tests",
        mailbox_google_oauth_client_secret="google-client-secret-for-tests",
        mailbox_google_oauth_redirect_uri="http://testserver/v1/mailbox-oauth/callback",
    )


def test_oauth_refresh_transport_failure_stays_retryable(tmp_path, monkeypatch) -> None:
    settings = _oauth_settings(tmp_path)

    def unavailable_token_endpoint(*args, **kwargs):
        raise URLError("test provider temporarily unavailable")

    monkeypatch.setattr(mailbox_oauth_service, "urlopen", unavailable_token_endpoint)
    with pytest.raises(MailboxOAuthError, match="mailbox_oauth_token_exchange_failed"):
        refresh_access_token(
            settings,
            provider_key="gmail_oauth",
            refresh_token="refresh-token-for-test-only",
        )

    class UnusedImapClient:
        pass

    def unavailable_refresh(*args, **kwargs):
        raise MailboxOAuthError("mailbox_oauth_token_exchange_failed")

    monkeypatch.setattr(mailbox_import_service, "refresh_access_token", unavailable_refresh)
    with pytest.raises(MailboxImportError, match="mailbox_oauth_token_exchange_failed"):
        mailbox_import_service._authenticate_imap_client(
            UnusedImapClient(),
            settings=settings,
            provider_key="gmail_oauth",
            email_address="recruiting@example.test",
            credential=mailbox_import_service._MailboxCredential(
                authentication_mode="oauth2",
                secret="refresh-token-for-test-only",
            ),
        )
    assert _retryable_error("mailbox_oauth_token_exchange_failed") is True


def test_invalid_grant_and_imap_oauth_denial_require_reauthorization(tmp_path, monkeypatch) -> None:
    settings = _oauth_settings(tmp_path)

    def invalid_grant_token_endpoint(*args, **kwargs):
        raise HTTPError(
            url="https://oauth2.googleapis.com/token",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":"invalid_grant"}'),
        )

    monkeypatch.setattr(mailbox_oauth_service, "urlopen", invalid_grant_token_endpoint)
    with pytest.raises(MailboxOAuthError, match="mailbox_oauth_reauthorization_required"):
        refresh_access_token(
            settings,
            provider_key="gmail_oauth",
            refresh_token="refresh-token-for-test-only",
        )

    class DenyingImapClient:
        def authenticate(self, mechanism: str, callback):
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "NO", [b"denied"]

    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: "access-token-for-test-only",
    )
    with pytest.raises(MailboxImportError, match="mailbox_oauth_reauthorization_required"):
        mailbox_import_service._authenticate_imap_client(
            DenyingImapClient(),
            settings=settings,
            provider_key="gmail_oauth",
            email_address="recruiting@example.test",
            credential=mailbox_import_service._MailboxCredential(
                authentication_mode="oauth2",
                secret="refresh-token-for-test-only",
            ),
        )
    assert _retryable_error("mailbox_oauth_reauthorization_required") is False


def test_refresh_response_preserves_optional_provider_rotated_refresh_token(
    tmp_path,
    monkeypatch,
) -> None:
    settings = replace(
        _oauth_settings(tmp_path),
        mailbox_imap_allowed_hosts=("outlook.office365.com",),
        mailbox_microsoft_oauth_client_id="microsoft-client-id-for-tests",
        mailbox_microsoft_oauth_client_secret="microsoft-client-secret-for-tests",
        mailbox_microsoft_oauth_redirect_uri="http://testserver/v1/mailbox-oauth/callback",
    )

    class TokenResponse:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self.payload if size < 0 else self.payload[:size]

    monkeypatch.setattr(
        mailbox_oauth_service,
        "urlopen",
        lambda *args, **kwargs: TokenResponse(
            b'{"access_token":"microsoft-access-token","refresh_token":"microsoft-r1"}'
        ),
    )
    rotated = refresh_access_token(
        settings,
        provider_key="microsoft_oauth",
        refresh_token="microsoft-r0",
    )
    assert rotated == OAuthAccessTokenRefresh(
        access_token="microsoft-access-token",
        replacement_refresh_token="microsoft-r1",
    )

    monkeypatch.setattr(
        mailbox_oauth_service,
        "urlopen",
        lambda *args, **kwargs: TokenResponse(b'{"access_token":"microsoft-access-token"}'),
    )
    unchanged = refresh_access_token(
        settings,
        provider_key="microsoft_oauth",
        refresh_token="microsoft-r1",
    )
    assert unchanged.replacement_refresh_token is None

    monkeypatch.setattr(
        mailbox_oauth_service,
        "urlopen",
        lambda *args, **kwargs: TokenResponse(
            b'{"access_token":"microsoft-access-token","refresh_token":""}'
        ),
    )
    with pytest.raises(MailboxOAuthError, match="mailbox_oauth_token_exchange_failed"):
        refresh_access_token(
            settings,
            provider_key="microsoft_oauth",
            refresh_token="microsoft-r1",
        )


def test_oauth_token_response_body_is_bounded_before_json_decoding(
    tmp_path,
    monkeypatch,
) -> None:
    """A compromised provider endpoint cannot force an unbounded allocation."""

    settings = _oauth_settings(tmp_path)

    class OversizedTokenResponse:
        read_sizes: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            self.__class__.read_sizes.append(size)
            return b"x" * size

    monkeypatch.setattr(
        mailbox_oauth_service,
        "urlopen",
        lambda *args, **kwargs: OversizedTokenResponse(),
    )

    with pytest.raises(MailboxOAuthError, match="mailbox_oauth_token_exchange_failed"):
        refresh_access_token(
            settings,
            provider_key="gmail_oauth",
            refresh_token="refresh-token-for-test-only",
        )

    assert OversizedTokenResponse.read_sizes == [
        mailbox_oauth_service._OAUTH_TOKEN_RESPONSE_MAX_BYTES + 1
    ]


def test_rotated_refresh_token_survives_sync_imap_failure(client, monkeypatch) -> None:
    """A post-refresh IMAP failure must not restore a now-revoked token."""

    class DenyingImapClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def authenticate(self, mechanism: str, callback) -> tuple[str, list[bytes]]:
            assert mechanism == "XOAUTH2"
            assert callback(b"").startswith(b"user=")
            return "NO", [b"denied"]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = MailboxConfig(
            display_name="Microsoft rotation test",
            display_name_key="microsoft rotation test",
            provider_key="microsoft_oauth",
            authentication_mode="oauth2",
            imap_host="outlook.office365.com",
            imap_port=993,
            email_address="rotation@example.test",
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
                    "microsoft-r0",
                ),
                reauthorization_required_at=None,
                last_error_code=None,
            )
        )
        session.commit()
        config_id = config.id

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", DenyingImapClient)
    monkeypatch.setattr(
        mailbox_import_service,
        "refresh_access_token",
        lambda *args, **kwargs: OAuthAccessTokenRefresh(
            access_token="microsoft-access-token",
            replacement_refresh_token="microsoft-r1",
        ),
    )
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        with pytest.raises(MailboxImportError, match="mailbox_oauth_reauthorization_required"):
            sync_mailbox(session, settings=settings, config_id=config_id)

    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == config_id
            )
        )
        assert credential is not None
        assert (
            mailbox_import_service._decrypt_mailbox_secret(
                settings,
                credential.encrypted_refresh_token,
            )
            == "microsoft-r1"
        )
        assert credential.reauthorization_required_at is not None
        assert credential.last_error_code == "mailbox_oauth_reauthorization_required"


def test_late_refresh_rotation_cannot_overwrite_newer_persisted_token(client) -> None:
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = MailboxConfig(
            display_name="OAuth rotation compare-and-swap test",
            display_name_key="oauth rotation compare-and-swap test",
            provider_key="microsoft_oauth",
            authentication_mode="oauth2",
            imap_host="outlook.office365.com",
            imap_port=993,
            email_address="rotation-cas@example.test",
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
                    "microsoft-r2",
                ),
                reauthorization_required_at=None,
                last_error_code=None,
            )
        )
        session.commit()
        config_id = config.id

        persisted = mailbox_import_service._persist_rotated_oauth_refresh_token(
            session,
            settings=settings,
            config=config,
            previous_refresh_token="microsoft-r0",
            replacement_refresh_token="microsoft-r1",
        )
        assert persisted is False

    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        credential = session.scalar(
            select(MailboxOAuthCredential).where(
                MailboxOAuthCredential.mailbox_config_id == config_id
            )
        )
        assert credential is not None
        assert (
            mailbox_import_service._decrypt_mailbox_secret(
                settings,
                credential.encrypted_refresh_token,
            )
            == "microsoft-r2"
        )


def test_due_scheduler_skips_oauth_mailbox_waiting_for_reauthorization(client) -> None:
    database = client.app.state.database
    now = datetime.now(timezone.utc)
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        config = MailboxConfig(
            display_name="OAuth pending reauthorization",
            display_name_key="oauth pending reauthorization",
            provider_key="gmail_oauth",
            authentication_mode="oauth2",
            imap_host="imap.gmail.com",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=None,
            enabled=True,
            import_start_uid=42,
            imap_uidvalidity=9,
            last_sync_started_at=now - timedelta(hours=1),
        )
        session.add(config)
        session.flush()
        session.add(
            MailboxOAuthCredential(
                organization_id=LEGACY_ORGANIZATION_ID,
                mailbox_config_id=config.id,
                encrypted_refresh_token="opaque-test-ciphertext",
                reauthorization_required_at=now - timedelta(minutes=1),
                last_error_code="mailbox_oauth_reauthorization_required",
            )
        )
        session.commit()

    assert enqueue_due_mailbox_sync_jobs(
        database=database,
        settings=client.app.state.settings,
    ) is False

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MailboxBackgroundJob)) == 0


def test_oauth_intent_cleanup_is_bounded_and_keeps_active_intents(client) -> None:
    database = client.app.state.database
    now = datetime.now(timezone.utc)
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        for index, expires_at, consumed_at in (
            (1, now - timedelta(minutes=3), None),
            (2, now - timedelta(minutes=2), None),
            (3, now + timedelta(minutes=10), now - timedelta(hours=2)),
            (4, now + timedelta(minutes=10), None),
            (5, now - timedelta(minutes=1), now - timedelta(minutes=1)),
        ):
            session.add(
                MailboxOAuthConnectIntent(
                    organization_id=LEGACY_ORGANIZATION_ID,
                    user_id=DEVELOPMENT_USER_ID,
                    membership_id=DEVELOPMENT_MEMBERSHIP_ID,
                    target_mailbox_config_id=None,
                    provider_key="gmail_oauth",
                    display_name=f"OAuth intent {index}",
                    email_address=f"intent-{index}@example.test",
                    mailbox="INBOX",
                    state_hash=f"{index:064x}",
                    encrypted_code_verifier="opaque-test-ciphertext",
                    expires_at=expires_at,
                    consumed_at=consumed_at,
                )
            )
        session.commit()

    with database.session_factory() as session:
        assert cleanup_expired_mailbox_oauth_intents(session, now=now, limit=2) == 2

    with database.session_factory() as session:
        remaining = session.scalars(
            select(MailboxOAuthConnectIntent).order_by(MailboxOAuthConnectIntent.id)
        ).all()
        assert {intent.display_name for intent in remaining} == {
            "OAuth intent 3",
            "OAuth intent 4",
            "OAuth intent 5",
        }
        assert cleanup_expired_mailbox_oauth_intents(session, now=now, limit=2) == 1

    with database.session_factory() as session:
        remaining = session.scalars(select(MailboxOAuthConnectIntent)).all()
        assert {intent.display_name for intent in remaining} == {
            "OAuth intent 4",
            "OAuth intent 5",
        }


def test_provider_catalog_requires_credential_encryption_before_marking_available(tmp_path) -> None:
    settings = replace(_oauth_settings(tmp_path), environment="production")

    providers = mailbox_import_service.mailbox_provider_list(settings)
    gmail = next(item for item in providers.items if item.provider_key == "gmail_oauth")

    assert gmail.available is False
