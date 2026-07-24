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
from app.services.identity_service import LEGACY_MEMBERSHIP_ID, LEGACY_USER_ID
from app.services.mailbox_background_job_service import (
    _retryable_error,
    enqueue_due_mailbox_sync_jobs,
)
from app.services.mailbox_import_service import (
    MailboxImportError,
    cleanup_expired_mailbox_oauth_intents,
)
from app.services.mailbox_oauth_service import MailboxOAuthError, refresh_access_token
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
                    user_id=LEGACY_USER_ID,
                    membership_id=LEGACY_MEMBERSHIP_ID,
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
