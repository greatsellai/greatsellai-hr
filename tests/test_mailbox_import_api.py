from __future__ import annotations

import hashlib
from datetime import timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DataError

from app.models import (
    Candidate,
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    MailboxAttachmentContentIdentity,
    MailboxConfig,
    Resume,
)
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


def _mail_with_attachment(
    *,
    message_id: str,
    filename: str,
    content: bytes,
) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message.set_content("Resume attached")
    message.add_attachment(
        content,
        maintype="application",
        subtype="pdf",
        filename=filename,
    )
    return message.as_bytes()


def _successful_mailbox_save(calls: list[bytes]):
    def save(
        session,
        *,
        candidate_id: str,
        original_filename: str | None,
        content: bytes,
        settings,
    ) -> Resume:
        calls.append(content)
        resume = Resume(
            candidate_id=candidate_id,
            original_filename=original_filename or "resume.pdf",
            storage_key=f"mailbox-dedup-{len(calls)}.pdf",
            sha256=hashlib.sha256(content).hexdigest(),
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="mailbox-dedup-test",
            raw_text="mailbox dedup source text",
            is_active=False,
        )
        session.add(resume)
        session.flush()
        return resume

    return save


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
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 43
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

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
    assert BoundMailboxImap.search_args == [(None, "UID 42:42")]
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
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 43
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

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


def test_forwarded_identical_attachment_creates_one_resume_and_two_audit_records(
    client,
    monkeypatch,
) -> None:
    attachment = b"%PDF-1.7 exact forwarded attachment"
    messages = {
        b"42": _mail_with_attachment(
            message_id="<original@example.test>",
            filename="original.pdf",
            content=attachment,
        ),
        b"43": _mail_with_attachment(
            message_id="<forwarded@example.test>",
            filename="forwarded-copy.pdf",
            content=attachment,
        ),
    }

    class ForwardedImap:
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 44
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"2"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"42 43"]
            if command == "fetch":
                return "OK", [(b"RFC822", messages[args[0]])]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    save_calls: list[bytes] = []
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", ForwardedImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", _successful_mailbox_save(save_calls))

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
        imports = session.scalars(
            select(EmailAttachmentImport).order_by(EmailAttachmentImport.message_uid)
        ).all()
        identities = session.scalars(select(MailboxAttachmentContentIdentity)).all()
        candidates = session.scalars(select(Candidate)).all()
        resumes = session.scalars(select(Resume)).all()

    assert result.imported_count == 1
    assert result.duplicate_count == 1
    assert save_calls == [attachment]
    assert len(candidates) == 1
    assert len(resumes) == 1
    assert len(imports) == 2
    assert [item.status for item in imports] == ["imported", "duplicate"]
    assert imports[0].resume_id == resumes[0].id
    assert imports[1].resume_id == resumes[0].id
    assert imports[1].canonical_import_id == imports[0].id
    assert len(identities) == 1
    assert identities[0].status == "imported"
    assert identities[0].canonical_import_id == imports[0].id
    assert identities[0].canonical_resume_id == resumes[0].id

    history = client.get("/v1/mailbox/imports")
    assert history.status_code == 200, history.text
    assert {item["status"] for item in history.json()["items"]} == {"imported", "duplicate"}


def test_same_filename_with_different_attachment_bytes_imports_two_resumes(
    client,
    monkeypatch,
) -> None:
    messages = {
        b"42": _mail_with_attachment(
            message_id="<first@example.test>",
            filename="resume.pdf",
            content=b"%PDF-1.7 first distinct attachment",
        ),
        b"43": _mail_with_attachment(
            message_id="<second@example.test>",
            filename="resume.pdf",
            content=b"%PDF-1.7 second distinct attachment",
        ),
    }

    class DistinctBytesImap:
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 44
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"2"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"42 43"]
            if command == "fetch":
                return "OK", [(b"RFC822", messages[args[0]])]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    save_calls: list[bytes] = []
    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", DistinctBytesImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", _successful_mailbox_save(save_calls))

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
        imports = session.scalars(select(EmailAttachmentImport)).all()
        identities = session.scalars(select(MailboxAttachmentContentIdentity)).all()
        resumes = session.scalars(select(Resume)).all()

    assert result.imported_count == 2
    assert result.duplicate_count == 0
    assert len(save_calls) == 2
    assert len(imports) == 2
    assert {item.status for item in imports} == {"imported"}
    assert len(identities) == 2
    assert len(resumes) == 2


def test_forwarded_duplicate_adopts_a_successful_import_created_before_identity_schema(
    client,
) -> None:
    """Existing mailbox records become canonical lazily after deployment."""

    attachment = b"%PDF-1.7 historical mailbox attachment"
    digest = hashlib.sha256(attachment).hexdigest()
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
        candidate = Candidate(display_name=None)
        session.add_all((config, candidate))
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="historical.pdf",
            storage_key="historical-mailbox-resume.pdf",
            sha256=digest,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="historical-mailbox-test",
            raw_text="historical mailbox source text",
            is_active=False,
        )
        session.add(resume)
        session.flush()
        historical = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<historical@example.test>",
            filename="historical.pdf",
            attachment_sha256=digest,
            status="imported",
            error=None,
            resume_id=resume.id,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        historical_id = historical.id
        resume_id = resume.id

        forwarded = mailbox_import_service._record(
            session,
            config=config,
            uid="43",
            message_id="<forwarded-after-upgrade@example.test>",
            filename="forwarded.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        claim = mailbox_import_service._claim_attachment_content(session, record=forwarded)
        assert claim.outcome == "duplicate"
        assert claim.canonical_import_id == historical_id
        assert claim.canonical_resume_id == resume_id
        completed = mailbox_import_service._complete_non_owner_processing_import(
            session,
            record=forwarded,
            claim=claim,
        )
        identity = session.scalar(select(MailboxAttachmentContentIdentity))

    assert completed.status == "duplicate"
    assert completed.resume_id == resume_id
    assert identity is not None
    assert identity.status == "imported"
    assert identity.canonical_import_id == historical_id
    assert identity.canonical_resume_id == resume_id


def test_forwarded_attachment_can_retry_after_the_first_canonical_import_fails(
    client,
    monkeypatch,
) -> None:
    attachment = b"%PDF-1.7 retry after forwarded failure"
    messages = {
        b"42": _mail_with_attachment(
            message_id="<failed-original@example.test>",
            filename="resume.pdf",
            content=attachment,
        ),
        b"43": _mail_with_attachment(
            message_id="<forwarded-retry@example.test>",
            filename="resume-copy.pdf",
            content=attachment,
        ),
    }

    class FailureThenForwardedImap:
        search_payload = b"42"
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = (
                42
                if self.__class__.status_calls == 1
                else max(int(value) for value in self.__class__.search_payload.split()) + 1
            )
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"2"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [self.__class__.search_payload]
            if command == "fetch":
                return "OK", [(b"RFC822", messages[args[0]])]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    success_calls: list[bytes] = []
    successful_save = _successful_mailbox_save(success_calls)
    attempts = 0

    def fail_once_then_save(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DataError("INSERT", {}, ValueError("temporary database issue"))
        return successful_save(*args, **kwargs)

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", FailureThenForwardedImap)
    monkeypatch.setattr(mailbox_import_service, "save_pdf_resume", fail_once_then_save)

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
        first = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
        )
    assert first.failed_count == 1

    FailureThenForwardedImap.search_payload = b"42 43"
    with client.app.state.database.session_factory() as session:
        second = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
        )
        imports = session.scalars(
            select(EmailAttachmentImport).order_by(EmailAttachmentImport.message_uid)
        ).all()
        identities = session.scalars(select(MailboxAttachmentContentIdentity)).all()
        candidates = session.scalars(select(Candidate)).all()
        resumes = session.scalars(select(Resume)).all()

    assert second.imported_count == 1
    assert second.duplicate_count == 0
    assert attempts == 2
    assert success_calls == [attachment]
    assert len(candidates) == 1
    assert len(resumes) == 1
    assert [item.status for item in imports] == ["failed", "imported"]
    assert imports[1].resume_id == resumes[0].id
    assert len(identities) == 1
    assert identities[0].status == "imported"
    assert identities[0].canonical_import_id == imports[1].id

    with client.app.state.database.session_factory() as session:
        retried_original = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=imports[0].id,
        )
    assert retried_original.status == "duplicate"
    assert retried_original.resume_id == resumes[0].id
    assert attempts == 2
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Candidate)) == 1
        assert session.scalar(select(func.count()).select_from(Resume)) == 1


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
        status_calls = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 43
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

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
    with client.app.state.database.session_factory() as session:
        retried = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=item["import_id"],
        )
    assert retried.import_id == item["import_id"]
    assert retried.status == "imported"
    assert retried.resume_id
    assert retried.attempt_count == 2
    assert retried.can_retry is False
    # A fresh failed import has a short-lived, hash-checked retry copy, so
    # recovery avoids both the incremental search and a second IMAP fetch.
    assert RetryImap.calls == []

    with client.app.state.database.session_factory() as session:
        with pytest.raises(mailbox_import_service.MailboxImportError, match="mailbox_import_not_retryable"):
            mailbox_import_service.retry_mailbox_attachment(
                session,
                settings=client.app.state.settings,
                import_id=item["import_id"],
            )

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

    with client.app.state.database.session_factory() as session:
        retried = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=record_id,
        )
    assert retried.status == "failed"
    assert retried.error == "mailbox_connection_failed"
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

    with client.app.state.database.session_factory() as session:
        retried = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=record_id,
        )
    assert retried.status == "failed"
    assert retried.error == "attachment_source_changed"
    assert retried.can_retry is False
    assert retried.attempt_count == 2
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

    with client.app.state.database.session_factory() as session:
        retried = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=record_id,
        )
    assert retried.status == "failed"
    assert retried.error == "attachment_message_unavailable"
    assert retried.can_retry is False
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


def test_content_identity_claim_query_locks_the_postgresql_handshake_row() -> None:
    statement = mailbox_import_service._content_identity_claim_statement(
        organization_id="00000000-0000-4000-8000-000000000001",
        attachment_sha256="a" * 64,
    )

    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in compiled
    assert statement.get_execution_options()["populate_existing"] is True


def test_waiting_forward_does_not_count_as_duplicate_when_owner_later_fails(
    client,
    monkeypatch,
) -> None:
    """An in-flight byte match is not a successful duplicate until its owner succeeds."""

    attachment = b"%PDF-1.7 owner eventually fails"
    digest = hashlib.sha256(attachment).hexdigest()
    raw_message = _mail_with_attachment(
        message_id="<forwarded-while-owner-runs@example.test>",
        filename="forwarded.pdf",
        content=attachment,
    )

    class ForwardedWhileOwnerRunsImap:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def login(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 44)"]

        def select(self, *args, **kwargs) -> tuple[str, list[bytes]]:
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            if command == "search":
                return "OK", [b"43"]
            if command == "fetch":
                return "OK", [(b"RFC822", raw_message)]
            raise AssertionError(f"unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service.imaplib,
        "IMAP4_SSL",
        ForwardedWhileOwnerRunsImap,
    )
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        owner_config = MailboxConfig(
            display_name="Owner",
            display_name_key="owner",
            imap_host="imap.owner.test",
            imap_port=993,
            email_address="owner@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
            import_start_uid=42,
            imap_uidvalidity=9,
        )
        forwarded_config = MailboxConfig(
            display_name="Forwarded",
            display_name_key="forwarded",
            imap_host="imap.forwarded.test",
            imap_port=993,
            email_address="forwarded@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
            import_start_uid=43,
            imap_uidvalidity=9,
        )
        session.add_all((owner_config, forwarded_config))
        session.flush()
        owner = mailbox_import_service._record(
            session,
            config=owner_config,
            uid="42",
            message_id="<canonical-owner@example.test>",
            filename="owner.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        owner_claim = mailbox_import_service._claim_attachment_content(
            session,
            record=owner,
        )
        assert owner_claim.outcome == "owner"
        session.commit()
        owner_id = owner.id
        forwarded_config_id = forwarded_config.id

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=forwarded_config_id,
        )
        waiter = session.scalar(
            select(EmailAttachmentImport).where(
                EmailAttachmentImport.mailbox_config_id == forwarded_config_id,
                EmailAttachmentImport.message_uid == "43",
            )
        )
        assert waiter is not None
        waiter_id = waiter.id
        assert waiter.status == "deduplicating"

    assert result.imported_count == 0
    assert result.duplicate_count == 0
    assert result.failed_count == 0

    with client.app.state.database.session_factory() as session:
        stored_owner = session.get(EmailAttachmentImport, owner_id)
        assert stored_owner is not None
        mailbox_import_service._complete_processing_import(
            session,
            record=stored_owner,
            claim=owner_claim,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
        )

    with client.app.state.database.session_factory() as session:
        stored_waiter = session.get(EmailAttachmentImport, waiter_id)
        assert stored_waiter is not None
        waiter_attempt = session.scalar(
            select(EmailAttachmentImportAttempt).where(
                EmailAttachmentImportAttempt.email_attachment_import_id == waiter_id,
            )
        )

    assert stored_waiter.status == "failed"
    assert stored_waiter.error == "attachment_import_failed"
    assert waiter_attempt is not None
    assert waiter_attempt.status == "failed"
    assert waiter_attempt.completed_at is not None


def test_expired_content_claim_cannot_complete_after_a_forwarded_copy_takes_over(client) -> None:
    """The byte-identity lease, not timing, decides which mail may import."""

    attachment = b"%PDF-1.7 content-claim-lease"
    digest = hashlib.sha256(attachment).hexdigest()
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
        first_record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<first-owner@example.test>",
            filename="resume.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        session.commit()
        first_claim = mailbox_import_service._claim_attachment_content(
            session,
            record=first_record,
        )
        assert first_claim.outcome == "owner"
        session.commit()

        identity = session.scalar(select(MailboxAttachmentContentIdentity))
        assert identity is not None
        identity.claim_lease_expires_at = mailbox_import_service._utcnow() - timedelta(seconds=1)
        session.commit()

        second_record = mailbox_import_service._record(
            session,
            config=config,
            uid="43",
            message_id="<forwarded-owner@example.test>",
            filename="resume-copy.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        second_claim = mailbox_import_service._claim_attachment_content(
            session,
            record=second_record,
        )
        assert second_claim.outcome == "owner"
        assert second_claim.claim_token != first_claim.claim_token
        session.commit()

        with pytest.raises(mailbox_import_service._ContentClaimLost):
            mailbox_import_service._complete_content_claim(
                session,
                claim=first_claim,
                attachment_sha256=digest,
                status="failed",
                error="attachment_import_failed",
                canonical_import_id=None,
                canonical_resume_id=None,
            )

        mailbox_import_service._complete_content_claim(
            session,
            claim=second_claim,
            attachment_sha256=digest,
            status="failed",
            error="attachment_import_failed",
            canonical_import_id=None,
            canonical_resume_id=None,
        )
        session.commit()

        identity = session.scalar(select(MailboxAttachmentContentIdentity))
        first_stored = session.get(EmailAttachmentImport, first_record.id)
        assert identity is not None
        assert identity.status == "failed"
        assert first_stored is not None
        assert first_stored.status == "failed"
        assert first_stored.error == "attachment_content_claim_expired"


def test_expired_content_claim_is_recovered_to_retryable_mail_audits(client) -> None:
    """A crashed owner cannot leave known mailbox UIDs stuck in processing."""

    attachment = b"%PDF-1.7 abandoned content-claim"
    digest = hashlib.sha256(attachment).hexdigest()
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
        owner = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<abandoned-owner@example.test>",
            filename="resume.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        waiter = mailbox_import_service._record(
            session,
            config=config,
            uid="43",
            message_id="<abandoned-forward@example.test>",
            filename="resume-copy.pdf",
            attachment_sha256=digest,
            status="deduplicating",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        session.commit()

        claim = mailbox_import_service._claim_attachment_content(session, record=owner)
        assert claim.outcome == "owner"
        session.commit()

        identity = session.scalar(select(MailboxAttachmentContentIdentity))
        assert identity is not None
        identity.claim_lease_expires_at = mailbox_import_service._utcnow() - timedelta(
            seconds=1
        )
        session.commit()

        assert (
            mailbox_import_service._recover_expired_content_claims(
                session,
                organization_id=config.organization_id,
            )
            == 1
        )
        session.expire_all()

        stored_owner = session.get(EmailAttachmentImport, owner.id)
        stored_waiter = session.get(EmailAttachmentImport, waiter.id)
        stored_identity = session.get(MailboxAttachmentContentIdentity, identity.id)
        attempts = session.scalars(
            select(EmailAttachmentImportAttempt).order_by(
                EmailAttachmentImportAttempt.email_attachment_import_id
            )
        ).all()
        assert stored_owner is not None
        assert stored_waiter is not None
        owner_can_retry = mailbox_import_service._can_retry(stored_owner)
        waiter_can_retry = mailbox_import_service._can_retry(stored_waiter)

    assert stored_owner is not None
    assert stored_waiter is not None
    assert stored_identity is not None
    assert stored_owner.status == "failed"
    assert stored_waiter.status == "failed"
    assert stored_owner.error == "attachment_content_claim_expired"
    assert stored_waiter.error == "attachment_content_claim_expired"
    assert owner_can_retry
    assert waiter_can_retry
    assert stored_identity.status == "failed"
    assert stored_identity.claim_token is None
    assert all(attempt.status == "failed" and attempt.completed_at for attempt in attempts)
