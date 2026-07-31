from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app


# The production default permits only deployment-owned provider names. Mailbox
# tests use deterministic local IMAP doubles, so they receive their own exact
# test allowlist instead of weakening the runtime policy with a wildcard.
TEST_MAILBOX_IMAP_HOSTS = (
    "imap.feishu.cn",
    "imap.a.test",
    "imap.agent-active-en-all.test",
    "imap.agent-active-en-named.test",
    "imap.agent-active-zh-all.test",
    "imap.agent-active-zh-named.test",
    "imap.agent-all-hunter.test",
    "imap.agent-all-name-collision.test",
    "imap.agent-all-social.test",
    "imap.agent-ambiguous-campus.test",
    "imap.agent-ambiguous-social.test",
    "imap.agent-archived-en-all.test",
    "imap.agent-archived-en-named.test",
    "imap.agent-archived-zh-all.test",
    "imap.agent-archived-zh-named.test",
    "imap.agent-attachment-authorization.test",
    "imap.agent-disabled.test",
    "imap.agent-imports.test",
    "imap.agent-long-target.test",
    "imap.agent-named-all-collision.test",
    "imap.agent-named-authorization.test",
    "imap.agent-recruiter-role.test",
    "imap.agent-short-target.test",
    "imap.agent-status.test",
    "imap.agent-sync.test",
    "imap.b.test",
    "imap.campus.test",
    "imap.changed.example.test",
    "imap.duplicate.test",
    "imap.engineering.test",
    "imap.epoch.test",
    "imap.example.test",
    "imap.explicit.test",
    "imap.first.test",
    "imap.fixture.invalid",
    "imap.forwarded.test",
    "imap.lease.test",
    "imap.other-product.test",
    "imap.owner.test",
    "imap.product.test",
    "imap.race.test",
    "imap.retention.test",
    "imap.sales.test",
    "imap.scheduled.test",
    "imap.second.test",
    "imap.social.test",
    "imap.sync-all-one.test",
    "imap.sync-all-two.test",
    "imap.task-one.test",
    "imap.task-two.test",
    "imap.tenant-retention.test",
    "imap.workspace-retention.test",
)


@pytest.fixture(autouse=True)
def mailbox_imap_test_double_adapter(monkeypatch):
    """Keep legacy IMAP doubles at the service boundary for integration tests.

    Focused transport tests exercise the pinned resolver and TLS connection
    directly. Other mailbox tests only need deterministic protocol behavior,
    never a real network socket.
    """

    from app.services import mailbox_import_service

    class ImapDoubleAdapter:
        def __init__(self, client) -> None:
            self._client = client

        def uid(self, command: str, *args):
            if command.casefold() == "fetch" and args and args[-1] == "(RFC822.SIZE)":
                # Existing protocol doubles model only a full RFC822 fetch.
                # The real transport preflights size first; this tiny response
                # lets those tests retain their focused behavior.
                return "OK", [b"(RFC822.SIZE 1)"]
            return self._client.uid(command, *args)

        def __getattr__(self, name: str):
            return getattr(self._client, name)

    def create_test_client(settings: AppSettings, *, host: str, port: int):
        return ImapDoubleAdapter(
            mailbox_import_service.imaplib.IMAP4_SSL(
                host,
                port,
                timeout=settings.mailbox_imap_connect_timeout_seconds,
            )
        )

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", create_test_client)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
        mailbox_imap_allowed_hosts=TEST_MAILBOX_IMAP_HOSTS,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def ai_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=True,
        deepseek_api_key="unit-test-key",
        deepseek_model="unit-test-model",
        min_text_chars_per_page=20,
        mailbox_imap_allowed_hosts=TEST_MAILBOX_IMAP_HOSTS,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def protected_client(tmp_path: Path) -> TestClient:
    """A production-like client with no compatibility-password backdoor.

    Tests that need this fixture must create a named account through the
    public registration flow and establish a regular browser session.  Keeping
    the fixture anonymous makes the authentication boundary explicit.
    """

    data_dir = tmp_path / "data"
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        session_secret="protected-client-test-session-secret",
        allow_unauthenticated=False,
        min_text_chars_per_page=20,
        mailbox_imap_allowed_hosts=TEST_MAILBOX_IMAP_HOSTS,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
