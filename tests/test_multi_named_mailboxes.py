from __future__ import annotations

import hashlib
from datetime import timedelta
from email.message import EmailMessage

import pytest

from app.models import MailboxConfig
from app.services import mailbox_import_service


class _MailboxImap:
    """A deterministic IMAP double for binding and one-message sync tests."""

    connected_hosts: list[str] = []
    fetches: list[tuple[str, bytes]] = []

    def __init__(self, host: str, *args, **kwargs) -> None:
        self.host = host
        self.__class__.connected_hosts.append(host)

    def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

    def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        if command == "search":
            return "OK", [b"42"]
        if command == "fetch":
            raw_uid = args[0]
            self.__class__.fetches.append((self.host, raw_uid))
            message = EmailMessage()
            message.set_content("No resume attachment")
            return "OK", [(b"42 (RFC822)", message.as_bytes())]
        raise AssertionError(f"unexpected IMAP command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b"logged out"]


def _create_mailbox(client, *, label: str, host: str) -> dict[str, object]:
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": label,
            "imap_host": host,
            "imap_port": 993,
            "email_address": f"{host.replace('.', '-')}@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_named_mailboxes_are_independent_and_legacy_endpoint_does_not_guess(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", _MailboxImap)
    first = _create_mailbox(client, label="社招收件箱", host="imap.social.test")
    second = _create_mailbox(client, label="校园收件箱", host="imap.campus.test")

    listing = client.get("/v1/mailboxes")
    assert listing.status_code == 200, listing.text
    assert [item["display_name"] for item in listing.json()["items"]] == [
        "校园收件箱",
        "社招收件箱",
    ]
    assert all("password" not in item for item in listing.json()["items"])

    duplicate = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "  社招收件箱  ",
            "imap_host": "imap.duplicate.test",
            "imap_port": 993,
            "email_address": "duplicate@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"] == "mailbox_duplicate_display_name"

    # A caller without a source ID may not accidentally sync or modify the
    # newest mailbox after the workspace adds a second channel.
    legacy_get = client.get("/v1/mailbox/config")
    legacy_sync = client.post("/v1/mailbox/sync")
    assert legacy_get.status_code == 409, legacy_get.text
    assert legacy_sync.status_code == 409, legacy_sync.text
    assert legacy_get.json()["detail"] == "mailbox_legacy_endpoint_ambiguous"
    assert legacy_sync.json()["detail"] == "mailbox_legacy_endpoint_ambiguous"

    first_sync = client.post(f"/v1/mailboxes/{first['mailbox_id']}/sync")
    second_sync = client.post(f"/v1/mailboxes/{second['mailbox_id']}/sync")
    assert first_sync.status_code == 200, first_sync.text
    assert second_sync.status_code == 200, second_sync.text
    assert first_sync.json()["mailbox_id"] == first["mailbox_id"]
    assert second_sync.json()["mailbox_id"] == second["mailbox_id"]
    assert set(_MailboxImap.fetches) == {
        ("imap.social.test", b"42"),
        ("imap.campus.test", b"42"),
    }

    history = client.get("/v1/mailbox-imports")
    assert history.status_code == 200, history.text
    assert {item["mailbox_config_id"] for item in history.json()["items"]} == {
        first["mailbox_id"],
        second["mailbox_id"],
    }


def test_named_mailbox_locks_source_after_import_but_allows_safe_edits(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", _MailboxImap)
    mailbox = _create_mailbox(client, label="产品岗", host="imap.product.test")

    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, mailbox["mailbox_id"])
        assert config is not None
        mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<source-lock@example.test>",
            filename="resume.pdf",
            attachment_sha256="a" * 64,
            status="skipped",
            error="unsupported_document_type",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()

    safe_edit = client.patch(
        f"/v1/mailboxes/{mailbox['mailbox_id']}",
        json={"display_name": "产品招聘邮箱", "enabled": False},
    )
    assert safe_edit.status_code == 200, safe_edit.text
    assert safe_edit.json()["display_name"] == "产品招聘邮箱"
    assert safe_edit.json()["enabled"] is False

    locked = client.patch(
        f"/v1/mailboxes/{mailbox['mailbox_id']}",
        json={"imap_host": "imap.other-product.test"},
    )
    assert locked.status_code == 409, locked.text
    assert locked.json()["detail"] == "mailbox_source_identity_locked"


def test_archive_keeps_history_but_prevents_retrying_against_a_removed_source(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", _MailboxImap)
    source_a = _create_mailbox(client, label="研发岗", host="imap.engineering.test")
    source_b = _create_mailbox(client, label="销售岗", host="imap.sales.test")

    with client.app.state.database.session_factory() as session:
        config_a = session.get(MailboxConfig, source_a["mailbox_id"])
        config_b = session.get(MailboxConfig, source_b["mailbox_id"])
        assert config_a is not None and config_b is not None
        failed = mailbox_import_service._record(
            session,
            config=config_a,
            uid="88",
            message_id="<archive-a@example.test>",
            filename="retry.pdf",
            attachment_sha256=hashlib.sha256(b"retry").hexdigest(),
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        mailbox_import_service._record(
            session,
            config=config_b,
            uid="88",
            message_id="<archive-b@example.test>",
            filename="retry.pdf",
            attachment_sha256=hashlib.sha256(b"retry").hexdigest(),
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        failed_id = failed.id

    archive = client.post(f"/v1/mailboxes/{source_a['mailbox_id']}/archive")
    assert archive.status_code == 200, archive.text
    assert archive.json()["archived_at"] is not None
    assert archive.json()["enabled"] is False

    active = client.get("/v1/mailboxes")
    assert active.status_code == 200, active.text
    assert [item["mailbox_id"] for item in active.json()["items"]] == [source_b["mailbox_id"]]

    source_history = client.get(
        f"/v1/mailbox-imports?mailbox_id={source_a['mailbox_id']}"
    )
    assert source_history.status_code == 200, source_history.text
    item = source_history.json()["items"][0]
    assert item["mailbox_display_name"] == "研发岗"
    assert item["import_id"] == failed_id

    _MailboxImap.fetches.clear()
    retried = client.post(f"/v1/mailbox/imports/{failed_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["error"] == "attachment_source_unavailable"
    assert retried.json()["can_retry"] is False
    assert _MailboxImap.fetches == []


def test_named_mailbox_id_filters_are_not_a_cross_workspace_or_global_probe(client) -> None:
    # The default test client represents one safe legacy workspace. A made-up
    # ID must be a 404 rather than an empty successful filter response.
    missing = client.get("/v1/mailbox-imports?mailbox_id=foreign-workspace-mailbox")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "mailbox_config_not_found"


def test_sync_claim_is_per_channel_and_a_stale_owner_cannot_clear_a_new_lease(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", _MailboxImap)
    mailbox = _create_mailbox(client, label="独立租约", host="imap.lease.test")
    mailbox_id = str(mailbox["mailbox_id"])

    with client.app.state.database.session_factory() as first_session:
        first_config = first_session.get(MailboxConfig, mailbox_id)
        assert first_config is not None
        first_token = mailbox_import_service._claim_mailbox_sync(
            first_session,
            config=first_config,
        )

    with client.app.state.database.session_factory() as second_session:
        second_config = second_session.get(MailboxConfig, mailbox_id)
        assert second_config is not None
        with pytest.raises(mailbox_import_service.MailboxImportError, match="mailbox_sync_in_progress"):
            mailbox_import_service._claim_mailbox_sync(
                second_session,
                config=second_config,
            )

        second_config.sync_lease_expires_at = (
            mailbox_import_service._utcnow() - timedelta(seconds=1)
        )
        second_session.commit()
        recovered_token = mailbox_import_service._claim_mailbox_sync(
            second_session,
            config=second_config,
        )
        assert recovered_token != first_token

        mailbox_import_service._release_mailbox_sync(
            second_session,
            config_id=mailbox_id,
            claim_token=first_token,
        )
        current = second_session.get(MailboxConfig, mailbox_id)
        assert current is not None
        assert current.sync_lease_token == recovered_token

        mailbox_import_service._release_mailbox_sync(
            second_session,
            config_id=mailbox_id,
            claim_token=recovered_token,
        )
        released = second_session.get(MailboxConfig, mailbox_id)
        assert released is not None
        assert released.sync_lease_token is None


def test_due_sync_prioritizes_another_channel_after_one_channel_started(
    client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", _MailboxImap)
    first = _create_mailbox(client, label="先前尝试", host="imap.first.test")
    second = _create_mailbox(client, label="仍待处理", host="imap.second.test")
    now = mailbox_import_service._utcnow()
    with client.app.state.database.session_factory() as session:
        first_config = session.get(MailboxConfig, first["mailbox_id"])
        second_config = session.get(MailboxConfig, second["mailbox_id"])
        assert first_config is not None and second_config is not None
        first_config.last_sync_started_at = now
        second_config.last_sync_started_at = None
        session.commit()

    observed: list[str] = []

    def fake_sync(session, *, settings, config_id: str | None = None):
        assert config_id is not None
        observed.append(config_id)
        return mailbox_import_service.MailboxSyncResponse(configured=True)

    monkeypatch.setattr(mailbox_import_service, "sync_mailbox", fake_sync)
    assert mailbox_import_service.sync_due_mailboxes(
        database=client.app.state.database,
        settings=client.app.state.settings,
    )
    assert observed == [second["mailbox_id"]]


def test_uidvalidity_change_pauses_the_channel_without_rebinding_it(
    client,
    monkeypatch,
) -> None:
    class EpochChangedImap(_MailboxImap):
        status_calls = 0

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidvalidity = 9 if self.__class__.status_calls == 1 else 10
            return "OK", [f"INBOX (UIDVALIDITY {uidvalidity} UIDNEXT 42)".encode()]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", EpochChangedImap)
    mailbox = _create_mailbox(client, label="来源变化", host="imap.epoch.test")
    before = client.get(f"/v1/mailboxes/{mailbox['mailbox_id']}")
    assert before.status_code == 200, before.text
    assert before.json()["enabled"] is True

    sync = client.post(f"/v1/mailboxes/{mailbox['mailbox_id']}/sync")
    assert sync.status_code == 422, sync.text
    assert sync.json()["detail"] == "mailbox_source_epoch_changed"

    after = client.get(f"/v1/mailboxes/{mailbox['mailbox_id']}")
    assert after.status_code == 200, after.text
    assert after.json()["enabled"] is False
    assert after.json()["last_sync_error"] == "mailbox_source_epoch_changed"
