from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.models import EmailAttachmentImport
from app.services import mailbox_import_service


def _create_mailbox(client, *, host: str = "imap.example.test") -> str:
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "资源限制测试",
            "imap_host": host,
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["mailbox_id"]


def test_configuration_rejects_unapproved_imap_host_before_opening_connection(client, monkeypatch) -> None:
    opened = False

    def unexpected_connection(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("a blocked hostname must not open a socket")

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", unexpected_connection)
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "错误目标",
            "imap_host": "127.0.0.1",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_imap_host_not_allowed"
    assert opened is False


def test_configuration_rejects_non_imaps_port_before_opening_connection(client, monkeypatch) -> None:
    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: pytest.fail("a blocked port must not open a socket"),
    )
    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "错误端口",
            "imap_host": "imap.example.test",
            "imap_port": 143,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_imap_port_not_allowed"


def test_sync_skips_declared_oversized_message_without_fetching_rfc822(client, monkeypatch) -> None:
    class OversizedImap:
        full_fetch_attempted = False
        size_fetches = 0
        status_calls = 0

        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 43
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"42"]
            if command == "fetch" and args[-1] == "(RFC822.SIZE)":
                self.__class__.size_fetches += 1
                return "OK", [b"42 (RFC822.SIZE 999999999)"]
            if command == "fetch":
                self.__class__.full_fetch_attempted = True
                raise AssertionError("oversized mail must not fetch RFC822")
            raise AssertionError(f"unexpected command {command}")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: OversizedImap(),
    )
    mailbox_id = _create_mailbox(client)

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=mailbox_id,
        )
        records = session.scalars(select(EmailAttachmentImport)).all()

    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert OversizedImap.full_fetch_attempted is False
    assert OversizedImap.size_fetches == 1
    assert len(records) == 1
    assert records[0].status == "skipped"
    assert records[0].error == "mailbox_message_too_large"

    # The resource-limit audit record makes the UID known, so a later worker
    # does not repeatedly preflight or fetch the same oversized mail.
    with client.app.state.database.session_factory() as session:
        repeat = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=mailbox_id,
        )

    assert repeat.imported_count == 0
    assert repeat.skipped_count == 0
    assert OversizedImap.size_fetches == 1


def test_message_fetch_rechecks_actual_bytes_after_server_size_preflight() -> None:
    class MisreportingImap:
        requested_item: str | None = None

        def uid(self, command: str, *args):
            assert command == "fetch"
            self.__class__.requested_item = args[-1]
            return "OK", [(b"42 (RFC822)", b"x" * 32)]

    imap = MisreportingImap()
    with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
        mailbox_import_service._fetch_message_bytes(
            imap,  # type: ignore[arg-type]
            raw_uid=b"42",
            max_bytes=16,
        )

    assert str(exc_info.value) == "mailbox_message_too_large"
    assert imap.requested_item == "(BODY.PEEK[]<0.17>)"


def test_mime_limits_reject_the_whole_message_before_attachment_ingestion(client) -> None:
    settings = replace(
        client.app.state.settings,
        max_upload_bytes=100,
        mailbox_max_attachments_per_message=2,
    )
    message = EmailMessage()
    message.set_content("resume attachments")
    for index in range(3):
        message.add_attachment(
            b"%PDF-1.7 content",
            maintype="application",
            subtype="pdf",
            filename=f"resume-{index}.pdf",
        )

    with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
        mailbox_import_service._attachments(
            mailbox_import_service._parse_mailbox_message(
                message.as_bytes(),
                settings=settings,
            ),
            settings=settings,
        )

    assert str(exc_info.value) == "mailbox_attachment_count_exceeded"


def test_mime_part_limit_stops_the_parser_before_an_unbounded_tree_is_built(client) -> None:
    settings = replace(client.app.state.settings, mailbox_max_mime_parts=2)
    message = EmailMessage()
    message.set_content("resume attachments")
    for index in range(3):
        message.add_attachment(
            b"%PDF-1.7 content",
            maintype="application",
            subtype="pdf",
            filename=f"resume-{index}.pdf",
        )

    with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
        mailbox_import_service._parse_mailbox_message(
            message.as_bytes(),
            settings=settings,
        )

    assert str(exc_info.value) == "mailbox_mime_structure_too_complex"


def test_body_cache_does_not_decode_a_large_plain_text_part(client) -> None:
    settings = replace(client.app.state.settings, mailbox_max_body_cache_bytes=8)
    message = EmailMessage()
    message.set_content("x" * 64)
    parsed = mailbox_import_service._parse_mailbox_message(message.as_bytes(), settings=settings)

    assert mailbox_import_service._message_body_bytes(parsed, settings=settings) == b""
