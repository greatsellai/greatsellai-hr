from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppSettings
from app.services import mailbox_imap_transport


def _settings(tmp_path: Path, *, hosts: tuple[str, ...] = ("imap.feishu.cn",)) -> AppSettings:
    data_dir = tmp_path / "data"
    return AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        mailbox_imap_allowed_hosts=hosts,
    )


@pytest.mark.parametrize(
    ("host", "port", "error_code"),
    [
        ("localhost", 993, "mailbox_imap_host_not_allowed"),
        ("127.0.0.1", 993, "mailbox_imap_host_not_allowed"),
        ("[::1]", 993, "mailbox_imap_host_not_allowed"),
        ("imap.not-approved.test", 993, "mailbox_imap_host_not_allowed"),
        ("imap.feishu.cn", 143, "mailbox_imap_port_not_allowed"),
        ("https://imap.feishu.cn", 993, "mailbox_imap_host_not_allowed"),
    ],
)
def test_validate_imap_endpoint_refuses_user_controlled_destinations(
    tmp_path: Path,
    host: str,
    port: int,
    error_code: str,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(mailbox_imap_transport.MailboxImapTransportError) as exc_info:
        mailbox_imap_transport.validate_imap_endpoint(
            settings,
            host=host,
            port=port,
        )

    assert str(exc_info.value) == error_code


@pytest.mark.parametrize(
    ("host", "port", "error_code"),
    [
        ("127.0.0.1", 993, "mailbox_imap_host_not_allowed"),
        ("https://imap.example.test", 993, "mailbox_imap_host_not_allowed"),
        ("imap.example.test", 143, "mailbox_imap_port_not_allowed"),
    ],
)
def test_generic_imap_validation_keeps_domain_and_imaps_only_guards(
    tmp_path: Path,
    host: str,
    port: int,
    error_code: str,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(mailbox_imap_transport.MailboxImapTransportError) as exc_info:
        mailbox_imap_transport.validate_imap_endpoint(
            settings,
            host=host,
            port=port,
            allow_custom_host=True,
        )

    assert str(exc_info.value) == error_code


def test_generic_imap_validation_accepts_a_domain_without_weakening_default_allowlist(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    assert mailbox_imap_transport.validate_imap_endpoint(
        settings,
        host="imap.corporate-mail.example",
        port=993,
        allow_custom_host=True,
    ) == "imap.corporate-mail.example"

    with pytest.raises(mailbox_imap_transport.MailboxImapTransportError) as exc_info:
        mailbox_imap_transport.validate_imap_endpoint(
            settings,
            host="imap.corporate-mail.example",
            port=993,
        )

    assert str(exc_info.value) == "mailbox_imap_host_not_allowed"


@pytest.mark.parametrize("host", ("imap.feishu.cn:993", "127.0.0.1", "*.feishu.cn"))
def test_runtime_validation_rejects_a_malformed_deployment_allowlist(
    tmp_path: Path,
    host: str,
) -> None:
    settings = _settings(tmp_path, hosts=(host,))

    with pytest.raises(ValueError, match="MAILBOX_IMAP_ALLOWED_HOSTS"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    "addresses",
    [
        [(2, 1, 6, "", ("127.0.0.1", 993))],
        [(10, 1, 6, "", ("::1", 993, 0, 0))],
        [
            (2, 1, 6, "", ("8.8.8.8", 993)),
            (2, 1, 6, "", ("10.0.0.8", 993)),
        ],
    ],
)
def test_resolver_rejects_private_or_mixed_results(monkeypatch, addresses) -> None:
    monkeypatch.setattr(
        mailbox_imap_transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: addresses,
    )

    with pytest.raises(mailbox_imap_transport.MailboxImapTransportError) as exc_info:
        mailbox_imap_transport._resolve_public_addresses(
            hostname="imap.feishu.cn",
            port=993,
            max_addresses=8,
        )

    assert str(exc_info.value) == "mailbox_imap_address_not_allowed"


def test_pinned_tls_connection_uses_verified_socket_address_and_original_sni(monkeypatch) -> None:
    opened: list[FakeSocket] = []

    class FakeSocket:
        def __init__(self, family: int, socktype: int, protocol: int) -> None:
            self.family = family
            self.socktype = socktype
            self.protocol = protocol
            self.timeout: float | None = None
            self.connected_to: tuple[object, ...] | None = None
            self.closed = False
            opened.append(self)

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, sockaddr: tuple[object, ...]) -> None:
            self.connected_to = sockaddr

        def close(self) -> None:
            self.closed = True

    class FakeTlsSocket(FakeSocket):
        pass

    class FakeSslContext:
        def __init__(self) -> None:
            self.server_hostname: str | None = None
            self.raw_socket: FakeSocket | None = None
            self.tls_socket = FakeTlsSocket(2, 1, 6)

        def wrap_socket(self, raw_socket: FakeSocket, *, server_hostname: str):
            self.raw_socket = raw_socket
            self.server_hostname = server_hostname
            return self.tls_socket

    monkeypatch.setattr(mailbox_imap_transport.socket, "socket", FakeSocket)
    ssl_context = FakeSslContext()
    address = mailbox_imap_transport._ResolvedImapAddress(
        family=2,
        socktype=1,
        protocol=6,
        sockaddr=("8.8.8.8", 993),
    )

    result = mailbox_imap_transport._open_pinned_tls_socket(
        hostname="imap.feishu.cn",
        addresses=(address,),
        timeout_seconds=10,
        ssl_context=ssl_context,  # type: ignore[arg-type]
    )

    assert result is ssl_context.tls_socket
    assert ssl_context.server_hostname == "imap.feishu.cn"
    assert ssl_context.raw_socket is not None
    assert ssl_context.raw_socket.connected_to == ("8.8.8.8", 993)


def test_pinned_transport_rejects_an_oversized_literal_before_reading_it() -> None:
    client = object.__new__(mailbox_imap_transport._PinnedIMAP4SSL)
    client._max_literal_bytes = 16
    closed = False

    def close_after_limit() -> None:
        nonlocal closed
        closed = True

    client._close_after_response_limit = close_after_limit

    with pytest.raises(mailbox_imap_transport.MailboxImapResponseLimitError) as exc_info:
        client.read(17)

    assert str(exc_info.value) == "mailbox_message_too_large"
    assert closed is True


def test_pinned_transport_rejects_an_oversized_protocol_line_before_splitting() -> None:
    class FakeFile:
        def readline(self, size: int) -> bytes:
            return b"x" * size

    client = object.__new__(mailbox_imap_transport._PinnedIMAP4SSL)
    client._max_response_line_bytes = 32
    client._uid_response_kind = None
    client._uid_search_response_bytes = 0
    client._uid_fetch_literal_bytes = 0
    client.file = FakeFile()
    closed = False

    def close_after_limit() -> None:
        nonlocal closed
        closed = True

    client._close_after_response_limit = close_after_limit

    with pytest.raises(mailbox_imap_transport.MailboxImapResponseLimitError) as exc_info:
        client.readline()

    assert str(exc_info.value) == "mailbox_imap_response_line_too_large"
    assert closed is True


def test_pinned_transport_caps_cumulative_uid_search_response() -> None:
    class FakeFile:
        def readline(self, size: int) -> bytes:
            assert size == 33
            return b"1 2 3 4 5 6\r\n"

    client = object.__new__(mailbox_imap_transport._PinnedIMAP4SSL)
    client._max_response_line_bytes = 32
    client._uid_response_kind = "search"
    client._uid_search_response_bytes = 20
    client._uid_fetch_literal_bytes = 0
    client.file = FakeFile()
    closed = False

    def close_after_limit() -> None:
        nonlocal closed
        closed = True

    client._close_after_response_limit = close_after_limit

    with pytest.raises(mailbox_imap_transport.MailboxImapResponseLimitError) as exc_info:
        client.readline()

    assert str(exc_info.value) == "mailbox_search_response_too_large"
    assert closed is True


def test_pinned_transport_caps_cumulative_uid_fetch_literals_before_reading() -> None:
    client = object.__new__(mailbox_imap_transport._PinnedIMAP4SSL)
    client._max_literal_bytes = 32
    client._uid_response_kind = "fetch"
    client._uid_fetch_literal_bytes = 20
    client._uid_search_response_bytes = 0
    closed = False

    def close_after_limit() -> None:
        nonlocal closed
        closed = True

    client._close_after_response_limit = close_after_limit

    with pytest.raises(mailbox_imap_transport.MailboxImapResponseLimitError) as exc_info:
        client.read(13)

    assert str(exc_info.value) == "mailbox_message_too_large"
    assert closed is True


def test_pinned_transport_caps_cumulative_uid_fetch_metadata_lines() -> None:
    class FakeFile:
        def readline(self, size: int) -> bytes:
            assert size == 33
            return b"FLAGS (\\Seen)\r\n"

    client = object.__new__(mailbox_imap_transport._PinnedIMAP4SSL)
    client._max_response_line_bytes = 32
    client._uid_response_kind = "fetch"
    client._uid_search_response_bytes = 0
    client._uid_fetch_literal_bytes = 0
    client._uid_fetch_response_line_bytes = 20
    client.file = FakeFile()
    closed = False

    def close_after_limit() -> None:
        nonlocal closed
        closed = True

    client._close_after_response_limit = close_after_limit

    with pytest.raises(mailbox_imap_transport.MailboxImapResponseLimitError) as exc_info:
        client.readline()

    assert str(exc_info.value) == "mailbox_imap_response_line_too_large"
    assert closed is True
