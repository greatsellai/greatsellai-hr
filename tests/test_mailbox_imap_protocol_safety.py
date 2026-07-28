from __future__ import annotations

import imaplib
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.models import EmailAttachmentImport, MailboxConfig
from app.services import mailbox_import_service


class _CommandCaptureImap(imaplib.IMAP4):
    """Render stdlib IMAP commands without opening a network connection."""

    def __init__(self) -> None:
        self.state = "NONAUTH"
        self.literal = None
        self.untagged_responses: dict[str, list[bytes]] = {}
        self.tagpre = b"T"
        self.tagnum = 0
        self.tagged_commands: dict[bytes, object] = {}
        self._encoding = "ascii"
        self.debug = 0
        self.is_readonly = False
        self.sent: list[bytes] = []

    def _log(self, *args, **kwargs) -> None:
        return None

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def _simple_command(self, name: str, *args: object):
        self._command(name, *args)
        if name == "STATUS":
            self.untagged_responses["STATUS"] = [
                b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"
            ]
        return "OK", [b"ok"]


def _encrypted_password(client, value: str = "test-authorization-code") -> str:
    return mailbox_import_service._fernet(client.app.state.settings).encrypt(
        value.encode("utf-8")
    ).decode("ascii")


def _stored_mailbox(
    client,
    *,
    email_address: str = "recruiting@example.test",
    mailbox: str = "INBOX",
    password: str = "test-authorization-code",
    import_start_uid: int = 42,
    imap_uidvalidity: int = 9,
) -> str:
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address=email_address,
            mailbox=mailbox,
            encrypted_password=_encrypted_password(client, password),
            enabled=True,
            import_start_uid=import_start_uid,
            imap_uidvalidity=imap_uidvalidity,
        )
        session.add(config)
        session.commit()
        return config.id


def _failed_import(client, *, config_id: str, message_uid: str) -> str:
    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        record = mailbox_import_service._record(
            session,
            config=config,
            uid=message_uid,
            message_id="<protocol-safety@example.test>",
            filename="resume.pdf",
            attachment_sha256="a" * 64,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        return record.id


def test_imap_username_and_mailbox_are_quoted_into_single_command_lines() -> None:
    client = _CommandCaptureImap()

    login_status, _ = mailbox_import_service._login_imap_client(
        client,  # type: ignore[arg-type]
        email_address='owner"\\ops@example.test',
        password='pa"\\ssword',
    )
    assert login_status == "OK"
    assert mailbox_import_service._read_mailbox_status(
        client,  # type: ignore[arg-type]
        mailbox='INBOX\\Team "A"',
    ) == (9, 42)
    select_status, _ = mailbox_import_service._select_mailbox_readonly(
        client,  # type: ignore[arg-type]
        mailbox='INBOX\\Team "A"',
    )

    assert select_status == "OK"
    assert len(client.sent) == 3
    assert all(command.count(b"\r\n") == 1 for command in client.sent)
    assert client.sent[0] == (
        b'T0 LOGIN "owner\\"\\\\ops@example.test" "pa\\"\\\\ssword"\r\n'
    )
    assert client.sent[1] == (
        b'T1 STATUS "INBOX\\\\Team \\"A\\"" (UIDVALIDITY UIDNEXT)\r\n'
    )
    assert client.sent[2] == b'T2 EXAMINE "INBOX\\\\Team \\"A\\""\r\n'


@pytest.mark.parametrize(
    ("field", "injected_value"),
    [
        ("email_address", "owner@example.test\r\nX1 NOOP"),
        ("email_address", "\r\nowner@example.test"),
        ("mailbox", "INBOX\nX1 DELETE INBOX"),
        ("mailbox", "INBOX\t"),
        ("password", "authorization-code\x00X1 NOOP"),
    ],
)
def test_binding_rejects_imap_control_characters_before_network(
    client,
    monkeypatch,
    field: str,
    injected_value: str,
) -> None:
    opened = False

    def unexpected_client(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("invalid IMAP arguments must fail before network I/O")

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", unexpected_client)
    payload = {
        "display_name": "协议安全测试",
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "email_address": "owner@example.test",
        "mailbox": "INBOX",
        "password": "test-authorization-code",
        "enabled": True,
    }
    payload[field] = injected_value

    response = client.post("/v1/mailboxes", json=payload)

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_imap_argument_invalid"
    assert injected_value not in response.text
    assert opened is False


def test_new_mailbox_rejects_non_inbox_before_network(client, monkeypatch) -> None:
    opened = False

    def unexpected_client(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("a non-INBOX channel must fail before network I/O")

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", unexpected_client)

    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "非收件箱测试",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "owner@example.test",
            "mailbox": "Archive",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_folder_fixed_to_inbox"
    assert opened is False
    with client.app.state.database.session_factory() as session:
        assert session.scalars(select(MailboxConfig)).all() == []


def test_new_mailbox_defaults_to_inbox_when_mailbox_is_omitted(client, monkeypatch) -> None:
    monkeypatch.setattr(
        mailbox_import_service,
        "_read_initial_mailbox_watermark",
        lambda **_: (9, 42),
    )

    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "默认收件箱测试",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "owner@example.test",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )

    assert response.status_code == 201, response.text
    config_id = response.json()["mailbox_id"]
    assert response.json()["mailbox"] == "INBOX"
    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        assert config.mailbox == "INBOX"


def test_existing_mailbox_rejects_folder_change_without_rebinding(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client)
    opened = False

    def unexpected_client(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("a rejected mailbox change must not open IMAP")

    monkeypatch.setattr(mailbox_import_service, "create_imap_client", unexpected_client)

    response = client.patch(
        f"/v1/mailboxes/{config_id}",
        json={"mailbox": "Archive"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_folder_fixed_to_inbox"
    assert opened is False
    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        assert config.mailbox == "INBOX"


def test_sync_revalidates_stored_imap_arguments_before_network(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client, mailbox="INBOX\r\nX1 STORE 1 +FLAGS (\\Seen)")
    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: pytest.fail("unsafe legacy config must not open IMAP"),
    )

    with client.app.state.database.session_factory() as session:
        with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
            mailbox_import_service.sync_mailbox(
                session,
                settings=client.app.state.settings,
                config_id=config_id,
            )
        stored = session.get(MailboxConfig, config_id)

    assert str(exc_info.value) == "mailbox_imap_argument_invalid"
    assert stored is not None
    assert stored.last_sync_error == "mailbox_imap_argument_invalid"
    assert "STORE" not in stored.last_sync_error


@pytest.mark.parametrize(
    "message_uid",
    ["0", "01", "+42", "4294967296", "42\r\nX1 NOOP", "四二"],
)
def test_retry_rejects_invalid_historical_uid_before_network(
    client,
    monkeypatch,
    message_uid: str,
) -> None:
    config_id = _stored_mailbox(client)
    import_id = _failed_import(client, config_id=config_id, message_uid=message_uid)
    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: pytest.fail("an invalid historical UID must not open IMAP"),
    )

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=import_id,
        )

    assert result.status == "failed"
    assert result.error == "attachment_source_changed"
    assert message_uid not in (result.error or "")


def test_retry_revalidates_stored_password_before_network(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client, password="authorization-code\nX1 NOOP")
    import_id = _failed_import(client, config_id=config_id, message_uid="42")
    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: pytest.fail("unsafe credentials must not open IMAP"),
    )

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=import_id,
        )

    assert result.status == "failed"
    assert result.error == "mailbox_imap_argument_invalid"
    assert "authorization-code" not in (result.error or "")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (4294967295, 4294967295),
        (b"42", 42),
        ("42", 42),
        (True, None),
        (False, None),
        (0, None),
        (-1, None),
        (4294967296, None),
        (b"0", None),
        (b"042", None),
        (b"+42", None),
        (b"42x", None),
        ("四二", None),
    ],
)
def test_imap_nz_number_accepts_only_canonical_uint32(
    value: object,
    expected: int | None,
) -> None:
    assert mailbox_import_service._parse_imap_nz_number(value) == expected


@pytest.mark.parametrize(
    "status_reply",
    [
        b"INBOX (UIDVALIDITY 0 UIDNEXT 42)",
        b"INBOX (UIDVALIDITY 09 UIDNEXT 42)",
        b"INBOX (UIDVALIDITY 9 UIDNEXT 042)",
        b"INBOX (UIDVALIDITY 9 UIDNEXT 4294967296)",
        b"INBOX (UIDVALIDITY 9 UIDNEXT 4\xff2)",
    ],
)
def test_binding_rejects_noncanonical_or_out_of_range_status_numbers(
    client,
    monkeypatch,
    status_reply: bytes,
) -> None:
    class InvalidStatusImap:
        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            return "OK", [status_reply]

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: InvalidStatusImap(),
    )

    response = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "非法水位线",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "owner@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "mailbox_status_failed"
    assert status_reply.decode("ascii", errors="replace") not in response.text


def test_sync_with_no_new_uid_never_issues_search_or_fetch(client, monkeypatch) -> None:
    class NoNewMailImap:
        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 42)"]

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def uid(self, *args, **kwargs):
            raise AssertionError("UID 42:* would incorrectly include the last old message")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: NoNewMailImap(),
    )
    configured = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "无新邮件",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "owner@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert configured.status_code == 201, configured.text

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=configured.json()["mailbox_id"],
        )

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.failed_count == 0


def test_search_filters_old_malformed_out_of_snapshot_and_duplicate_uids(
    client,
    monkeypatch,
) -> None:
    message = EmailMessage()
    message.set_content("No resume attachment")
    raw_message = message.as_bytes()

    class FilteredSearchImap:
        status_calls = 0
        search_args: list[tuple[object, ...]] = []
        body_fetches: list[bytes] = []

        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            self.__class__.status_calls += 1
            uidnext = 42 if self.__class__.status_calls == 1 else 46
            return "OK", [f"INBOX (UIDVALIDITY 9 UIDNEXT {uidnext})".encode()]

        def select(self, *args, **kwargs):
            return "OK", [b"4"]

        def uid(self, command: str, *args):
            if command == "search":
                self.__class__.search_args.append(args)
                return "OK", [
                    b"45 41 042 0 -1 +43 4294967296 44 42 45 46 43\xff 43"
                ]
            if command == "fetch" and args[-1] == "(RFC822.SIZE)":
                return "OK", [b"(RFC822.SIZE 128)"]
            if command == "fetch":
                self.__class__.body_fetches.append(args[0])
                return "OK", [(b"RFC822", raw_message)]
            raise AssertionError(f"unexpected command {command}")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: FilteredSearchImap(),
    )
    configured = client.post(
        "/v1/mailboxes",
        json={
            "display_name": "UID 过滤",
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "email_address": "owner@example.test",
            "mailbox": "INBOX",
            "password": "test-authorization-code",
            "enabled": True,
        },
    )
    assert configured.status_code == 201, configured.text

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.sync_mailbox(
            session,
            settings=client.app.state.settings,
            config_id=configured.json()["mailbox_id"],
        )

    assert result.skipped_count == 4
    assert FilteredSearchImap.search_args == [(None, "UID 42:45")]
    assert set(FilteredSearchImap.body_fetches) == {b"42", b"43", b"44", b"45"}
    assert len(FilteredSearchImap.body_fetches) == 4


def test_sync_disables_same_epoch_uidnext_regression(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client, import_start_uid=42, imap_uidvalidity=9)

    class RegressedImap:
        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 41)"]

        def select(self, *args, **kwargs):
            raise AssertionError("regressed UIDNEXT must stop before EXAMINE")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: RegressedImap(),
    )

    with client.app.state.database.session_factory() as session:
        with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
            mailbox_import_service.sync_mailbox(
                session,
                settings=client.app.state.settings,
                config_id=config_id,
            )
        session.expire_all()
        config = session.get(MailboxConfig, config_id)

    assert str(exc_info.value) == "mailbox_source_watermark_invalid"
    assert config is not None
    assert config.enabled is False
    assert config.last_sync_error == "mailbox_source_watermark_invalid"


def test_sync_rechecks_uidvalidity_after_examine(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client)

    class EpochChangesDuringExamine:
        status_calls = 0

        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            self.__class__.status_calls += 1
            uidvalidity = 9 if self.__class__.status_calls == 1 else 10
            return "OK", [f"INBOX (UIDVALIDITY {uidvalidity} UIDNEXT 43)".encode()]

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def uid(self, *args, **kwargs):
            raise AssertionError("changed UIDVALIDITY must stop before SEARCH")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: EpochChangesDuringExamine(),
    )

    with client.app.state.database.session_factory() as session:
        with pytest.raises(mailbox_import_service.MailboxImportError) as exc_info:
            mailbox_import_service.sync_mailbox(
                session,
                settings=client.app.state.settings,
                config_id=config_id,
            )
        session.expire_all()
        config = session.get(MailboxConfig, config_id)

    assert str(exc_info.value) == "mailbox_source_epoch_changed"
    assert config is not None
    assert config.enabled is False


def test_retry_rechecks_uidvalidity_after_examine(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client)
    import_id = _failed_import(client, config_id=config_id, message_uid="42")

    class RetryEpochChangesDuringExamine:
        status_calls = 0

        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            self.__class__.status_calls += 1
            uidvalidity = 9 if self.__class__.status_calls == 1 else 10
            return "OK", [f"INBOX (UIDVALIDITY {uidvalidity} UIDNEXT 43)".encode()]

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def uid(self, *args, **kwargs):
            raise AssertionError("changed UIDVALIDITY must stop before FETCH")

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: RetryEpochChangesDuringExamine(),
    )

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=import_id,
        )

    assert result.status == "failed"
    assert result.error == "attachment_source_changed"


def test_retry_uses_canonical_uid_bytes_after_snapshot_validation(client, monkeypatch) -> None:
    config_id = _stored_mailbox(client)
    import_id = _failed_import(client, config_id=config_id, message_uid="42")
    message = EmailMessage()
    message.set_content("No matching attachment")
    raw_message = message.as_bytes()

    class RetryUidImap:
        fetched_uids: list[bytes] = []

        def login(self, *args, **kwargs):
            return "OK", [b"logged in"]

        def status(self, *args, **kwargs):
            return "OK", [b"INBOX (UIDVALIDITY 9 UIDNEXT 43)"]

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def uid(self, command: str, *args):
            assert command == "fetch"
            self.__class__.fetched_uids.append(args[0])
            if args[-1] == "(RFC822.SIZE)":
                return "OK", [b"(RFC822.SIZE 128)"]
            return "OK", [(b"RFC822", raw_message)]

        def logout(self):
            return "BYE", [b"logged out"]

    monkeypatch.setattr(
        mailbox_import_service,
        "create_imap_client",
        lambda *args, **kwargs: RetryUidImap(),
    )

    with client.app.state.database.session_factory() as session:
        result = mailbox_import_service.retry_mailbox_attachment(
            session,
            settings=client.app.state.settings,
            import_id=import_id,
        )

    assert result.status == "failed"
    assert result.error == "attachment_message_unavailable"
    assert RetryUidImap.fetched_uids == [b"42", b"42"]


def test_search_uid_filter_does_not_persist_invalid_tokens(client) -> None:
    settings = client.app.state.settings
    parsed = mailbox_import_service._parse_search_uids(
        [b"41 42 043 43 4294967295 4294967296", "44 四二"],
        settings=settings,
        minimum_uid=42,
        maximum_uid=44,
    )

    assert parsed == [b"42", b"43", b"44"]
    with client.app.state.database.session_factory() as session:
        assert session.scalars(select(EmailAttachmentImport)).all() == []
