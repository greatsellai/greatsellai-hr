from __future__ import annotations

import hashlib
from datetime import timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DataError

from app.models import Candidate, EmailAttachmentImport, EmailAttachmentImportAttempt, MailboxConfig, Resume
from app.services import mailbox_import_service


class StatusOnlyImap:
    """Enough IMAP behavior for configuration saves that capture UIDNEXT."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
        return "OK", [b'INBOX (UIDVALIDITY 9 UIDNEXT 42)']

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b"logged out"]


def test_mailbox_configuration_never_returns_the_authorization_code(client, monkeypatch) -> None:
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", StatusOnlyImap)
    initial = client.get("/v1/mailbox/config")
    assert initial.status_code == 200, initial.text
    assert initial.json()["configured"] is False

    saved = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert saved.status_code == 200, saved.text
    payload = saved.json()
    assert payload["configured"] is True
    assert payload["password_configured"] is True
    assert payload["import_started_at"] is not None
    assert "password" not in payload
    assert "test-authorization-code" not in saved.text

    fetched = client.get("/v1/mailbox/config")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["email_address"] == "recruiting@example.test"
    assert "test-authorization-code" not in fetched.text


def test_mailbox_binding_searches_only_messages_at_or_after_uidnext(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message["Message-ID"] = "<new-mail-after-binding@example.test>"
    message.set_content("Resume attached")
    message.add_attachment(
        b"not a supported resume format",
        maintype="text",
        subtype="plain",
        filename="resume.txt",
    )
    raw_message = message.as_bytes()

    class BoundMailboxImap:
        search_args: list[tuple[object, ...]] = []
        fetched_uids: list[bytes] = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b'INBOX (UIDVALIDITY 9 UIDNEXT 42)']

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            if command == "search":
                self.search_args.append(args)
                return "OK", [b"42"]
            if command == "fetch":
                self.fetched_uids.append(args[0])
                return "OK", [(b"42 (RFC822)", raw_message)]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", BoundMailboxImap)
    saved = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert saved.status_code == 200, saved.text

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
        )

    assert result.imported_count == 0
    assert result.skipped_count == 1
    assert BoundMailboxImap.search_args == [(None, "UID 42:*")]
    assert BoundMailboxImap.fetched_uids == [b"42"]


def test_existing_mailbox_without_watermark_skips_its_history_once(
    client,
    monkeypatch,
) -> None:
    class LegacyMailboxImap:
        selected = False
        searched = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b'INBOX (UIDVALIDITY 33 UIDNEXT 108)']

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.selected = True
            raise AssertionError("legacy history must not be selected or fetched")

        def uid(self, command: str, *args):
            self.searched = True
            raise AssertionError("legacy history must not be searched")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", LegacyMailboxImap)
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings,
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.commit()
        config_id = config.id

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=config_id,
        )
        stored = session.get(MailboxConfig, config_id)
        assert stored is not None
        assert stored.import_start_uid == 108
        assert stored.imap_uidvalidity == 33
        assert stored.import_started_at is not None

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert LegacyMailboxImap.selected is False
    assert LegacyMailboxImap.searched is False


def test_mailbox_rebinding_resets_the_uid_watermark_but_settings_edits_do_not(
    client,
    monkeypatch,
) -> None:
    class RebindingImap:
        status_replies = [
            b'INBOX (UIDVALIDITY 9 UIDNEXT 42)',
            b'Archive (UIDVALIDITY 12 UIDNEXT 90)',
        ]
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            reply = self.status_replies[self.status_calls]
            self.__class__.status_calls += 1
            return "OK", [reply]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", RebindingImap)
    initial = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert initial.status_code == 200, initial.text

    settings_only = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "enabled": False,
        },
    )
    assert settings_only.status_code == 200, settings_only.text
    assert RebindingImap.status_calls == 1

    rebound = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "Archive",
            "enabled": True,
        },
    )
    assert rebound.status_code == 200, rebound.text
    assert RebindingImap.status_calls == 2

    with client.app.state.database.session_factory() as session:
        config = session.scalar(select(MailboxConfig))
        assert config is not None
        assert config.import_start_uid == 90
        assert config.imap_uidvalidity == 12


def test_html_resume_upload_is_preserved_and_served_as_html(client) -> None:
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate.html",
                b"<html><body><h1>Candidate</h1><p>Python FastAPI machine learning engineer</p></body></html>",
                "text/html",
            )
        },
    )
    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]

    original = client.get(f"/v1/resumes/{resume_id}/original-file")
    assert original.status_code == 200, original.text
    assert original.headers["content-type"].startswith("text/html")
    assert b"Candidate" in original.content


def test_mailbox_sync_records_database_attachment_failure_without_crashing_worker(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message["Message-ID"] = "<mailbox-failure-test@example.test>"
    message.set_content("Resume attached")
    message.add_attachment(
        b"%PDF-1.7 test attachment",
        maintype="application",
        subtype="pdf",
        filename="resume.pdf",
    )
    raw_message = message.as_bytes()

    class FakeImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b'INBOX (UIDVALIDITY 9 UIDNEXT 42)']

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"42"]
            if command == "fetch":
                return "OK", [(b"42 (RFC822)", raw_message)]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    def database_failure(*args, **kwargs):
        raise DataError("INSERT", {}, ValueError("unexpected NUL byte"))

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", database_failure)

    saved = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert saved.status_code == 200, saved.text

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
        )

    assert result.imported_count == 0
    assert result.failed_count == 1
    assert result.last_sync_error is None


def test_failed_attachment_retries_retained_copy_and_updates_the_same_record(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message["Message-ID"] = "<retry-exact-attachment@example.test>"
    message.set_content("Resume attached")
    message.add_attachment(
        b"%PDF-1.7 retry attachment",
        maintype="application",
        subtype="pdf",
        filename="retry.pdf",
    )
    raw_message = message.as_bytes()

    class RetryImap:
        calls: list[tuple[str, bytes | None]] = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            first = args[0] if args and isinstance(args[0], bytes) else None
            self.__class__.calls.append((command, first))
            if command == "search":
                return "OK", [b"42"]
            if command == "fetch":
                assert args[0] == b"42"
                return "OK", [(b"42 (RFC822)", raw_message)]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    def database_failure(*args, **kwargs):
        raise DataError("INSERT", {}, ValueError("temporary database issue"))

    def successful_save(
        session,
        *,
        candidate_id: str,
        original_filename: str | None,
        content: bytes,
        settings,
    ) -> Resume:
        resume = Resume(
            candidate_id=candidate_id,
            original_filename=original_filename or "retry.pdf",
            storage_key="retry-success.pdf",
            sha256="a" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="mailbox-retry-test",
            raw_text="retry test text",
            is_active=False,
        )
        session.add(resume)
        session.flush()
        return resume

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", RetryImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", database_failure)
    saved = client.put(
        "/v1/mailbox/config",
        json={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "recruiting@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert saved.status_code == 200, saved.text

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
        )
    assert result.failed_count == 1

    history = client.get("/v1/mailbox/imports")
    assert history.status_code == 200, history.text
    item = history.json()["items"][0]
    assert item["status"] == "failed"
    assert item["error"] == "attachment_import_failed"
    assert item["can_retry"] is True
    assert item["attempt_count"] == 1

    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", successful_save)
    RetryImap.calls.clear()
    retried = client.post(f"/v1/mailbox/imports/{item['import_id']}/retry")
    assert retried.status_code == 200, retried.text
    payload = retried.json()
    assert payload["import_id"] == item["import_id"]
    assert payload["status"] == "imported"
    assert payload["resume_id"]
    assert payload["attempt_count"] == 2
    assert payload["can_retry"] is False
    # A fresh failed import has a short-lived, hash-checked retry copy, so
    # recovery avoids both the incremental search and a second IMAP fetch.
    assert RetryImap.calls == []

    repeated = client.post(f"/v1/mailbox/imports/{item['import_id']}/retry")
    assert repeated.status_code == 409, repeated.text

    with client.app.state.database.session_factory() as session:
        imports = session.scalars(select(EmailAttachmentImport)).all()
        attempts = session.scalars(select(EmailAttachmentImportAttempt)).all()
        candidates = session.scalars(select(Candidate)).all()
        resumes = session.scalars(select(Resume)).all()
    assert len(imports) == 1
    assert len(attempts) == 2
    assert len(candidates) == 1
    assert len(resumes) == 1


def test_attachment_retry_cleans_uploaded_file_when_completion_audit_fails(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message.set_content("Resume attached")
    attachment = b"%PDF-1.7 retry cleanup attachment"
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="pdf",
        filename="retry-cleanup.pdf",
    )
    raw_message = message.as_bytes()

    class RetryImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 99)"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            assert command == "fetch"
            assert args[0] == b"42"
            return "OK", [(b"42 (RFC822)", raw_message)]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    def successful_save(
        session,
        *,
        candidate_id: str,
        original_filename: str | None,
        content: bytes,
        settings,
    ) -> Resume:
        resume = Resume(
            candidate_id=candidate_id,
            original_filename=original_filename or "retry-cleanup.pdf",
            storage_key="retry-cleanup.pdf",
            sha256="b" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="mailbox-retry-test",
            raw_text="retry cleanup test text",
            is_active=False,
        )
        session.add(resume)
        session.flush()
        return resume

    original_complete = mailbox_import_service._complete_retry
    completion_calls = 0

    def fail_first_completion(*args, **kwargs):
        nonlocal completion_calls
        completion_calls += 1
        if completion_calls == 1:
            raise DataError("UPDATE", {}, ValueError("audit write failed"))
        return original_complete(*args, **kwargs)

    discarded: list[tuple[str | None, str]] = []

    def record_discard(settings, *, storage_key: str | None, organization_id: str) -> None:
        discarded.append((storage_key, organization_id))

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", RetryImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", successful_save)
    monkeypatch.setattr(mailbox_import_service, "_complete_retry", fail_first_completion)
    monkeypatch.setattr(mailbox_import_service, "discard_uploaded_pdf", record_discard)

    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.flush()
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<retry-cleanup@example.test>",
            filename="retry-cleanup.pdf",
            attachment_sha256=hashlib.sha256(attachment).hexdigest(),
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        record_id = record.id
        organization_id = config.organization_id

    retried = client.post(f"/v1/mailbox/imports/{record_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "failed"
    assert retried.json()["error"] == "mailbox_connection_failed"
    assert completion_calls == 2
    assert discarded == [("retry-cleanup.pdf", organization_id)]

    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Candidate)) == 0
        assert session.scalar(select(func.count()).select_from(Resume)) == 0


def test_attachment_retry_stops_when_the_imap_source_epoch_changed(
    client,
    monkeypatch,
) -> None:
    class SourceChangedImap:
        fetched = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 10 UIDNEXT 99)"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            raise AssertionError("source mismatch must stop before selecting mail")

        def uid(self, command: str, *args):
            self.__class__.fetched = True
            raise AssertionError("source mismatch must never fetch mail")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", SourceChangedImap)
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.flush()
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<source-changed@example.test>",
            filename="retry.pdf",
            attachment_sha256="c" * 64,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        record_id = record.id

    retried = client.post(f"/v1/mailbox/imports/{record_id}/retry")
    assert retried.status_code == 200, retried.text
    payload = retried.json()
    assert payload["status"] == "failed"
    assert payload["error"] == "attachment_source_changed"
    assert payload["can_retry"] is False
    assert payload["attempt_count"] == 2
    assert SourceChangedImap.fetched is False


def test_attachment_retry_refuses_a_different_attachment_with_the_same_message_uid(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message.set_content("Different attachment")
    message.add_attachment(
        b"different content",
        maintype="application",
        subtype="pdf",
        filename="different.pdf",
    )
    raw_message = message.as_bytes()

    class HashMismatchImap:
        fetched = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 99)"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            assert command == "fetch"
            assert args[0] == b"42"
            self.__class__.fetched = True
            return "OK", [(b"42 (RFC822)", raw_message)]

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", HashMismatchImap)
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.flush()
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<hash-mismatch@example.test>",
            filename="retry.pdf",
            attachment_sha256="d" * 64,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        record_id = record.id

    retried = client.post(f"/v1/mailbox/imports/{record_id}/retry")
    assert retried.status_code == 200, retried.text
    payload = retried.json()
    assert payload["status"] == "failed"
    assert payload["error"] == "attachment_message_unavailable"
    assert payload["can_retry"] is False
    assert HashMismatchImap.fetched is True

    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Candidate)) == 0
        assert session.scalar(select(func.count()).select_from(Resume)) == 0


def test_expired_retry_claim_cannot_be_completed_by_the_previous_request(client) -> None:
    """A stale retry token cannot overwrite the retry that recovered its lease."""

    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.flush()
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<lease-recovery@example.test>",
            filename="retry.pdf",
            attachment_sha256="e" * 64,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        record_id = record.id

        first_claim = mailbox_import_service._claim_retry(session, import_id=record_id)
        first_token = first_claim.retry_claim_token
        assert first_token
        first_claim.retry_lease_expires_at = (
            mailbox_import_service._utcnow() - timedelta(seconds=1)
        )
        session.commit()

        recovered_claim = mailbox_import_service._claim_retry(
            session,
            import_id=record_id,
        )
        recovered_token = recovered_claim.retry_claim_token
        assert recovered_token and recovered_token != first_token

        with pytest.raises(mailbox_import_service._RetryClaimLost):
            mailbox_import_service._complete_retry(
                session,
                import_id=record_id,
                claim_token=first_token,
                status="failed",
                error="mailbox_connection_failed",
                resume_id=None,
            )

        # The old request lost its conditional write; only the recovered
        # claim may now complete and append the second audit attempt.
        completed = mailbox_import_service._complete_retry(
            session,
            import_id=record_id,
            claim_token=recovered_token,
            status="failed",
            error="mailbox_connection_failed",
            resume_id=None,
        )
        assert completed.status == "failed"
        assert completed.attempt_count == 3

        attempts = session.scalars(
            select(EmailAttachmentImportAttempt).order_by(
                EmailAttachmentImportAttempt.attempt_number
            )
        ).all()
        assert len(attempts) == 3
        assert attempts[1].error == "attachment_retry_interrupted"
        assert attempts[2].status == "failed"
