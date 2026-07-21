"""Safe, pinned IMAPS transport for mailbox ingestion.

Mailbox host names originate in a workspace configuration, so handing them
directly to :class:`imaplib.IMAP4_SSL` would turn the application into an
arbitrary network client.  This module accepts only deployment-owned exact
host names, rejects non-public DNS results, then connects to the already
validated socket address while preserving the original host name for TLS SNI
and certificate verification.
"""
from __future__ import annotations

import ipaddress
import imaplib
import socket
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import AppSettings


IMAPS_PORT = 993


class MailboxImapTransportError(RuntimeError):
    """A stable, UI-safe reason for refusing or failing an IMAPS connection."""


class MailboxImapResponseLimitError(MailboxImapTransportError):
    """The remote server exceeded a bounded IMAP protocol response budget."""


@dataclass(frozen=True)
class _ResolvedImapAddress:
    family: int
    socktype: int
    protocol: int
    sockaddr: tuple[object, ...]


def _normalized_hostname(value: str) -> str:
    raw = value.strip().rstrip(".")
    if not raw or len(raw) > 253:
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    if any(marker in raw for marker in ("://", "/", "\\", "@", ":", "[", "]")):
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        pass
    else:
        # An IP literal has no provider identity to verify with SNI and would
        # let a workspace target a server-side network address directly.
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    try:
        normalized = raw.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed") from exc
    labels = normalized.split(".")
    if (
        len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    ):
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    return normalized


def validate_imap_endpoint(
    settings: "AppSettings",
    *,
    host: str,
    port: int,
) -> str:
    """Return a canonical permitted hostname without doing any network I/O."""

    if port != IMAPS_PORT:
        raise MailboxImapTransportError("mailbox_imap_port_not_allowed")
    normalized_host = _normalized_hostname(host)
    allowed_hosts = {
        _normalized_hostname(value)
        for value in settings.mailbox_imap_allowed_hosts
    }
    if normalized_host not in allowed_hosts:
        raise MailboxImapTransportError("mailbox_imap_host_not_allowed")
    return normalized_host


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # ``is_global`` excludes loopback, RFC1918, carrier-grade NAT,
    # link-local, multicast, unspecified, documentation and reserved ranges.
    return address.is_global


def _resolve_public_addresses(
    *,
    hostname: str,
    port: int,
    max_addresses: int,
) -> tuple[_ResolvedImapAddress, ...]:
    try:
        raw_addresses = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise MailboxImapTransportError("mailbox_imap_dns_failed") from exc
    if not raw_addresses or len(raw_addresses) > max_addresses:
        raise MailboxImapTransportError("mailbox_imap_address_not_allowed")

    resolved: list[_ResolvedImapAddress] = []
    seen: set[tuple[int, tuple[object, ...]]] = set()
    for family, socktype, protocol, _, sockaddr in raw_addresses:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            raise MailboxImapTransportError("mailbox_imap_address_not_allowed")
        address = str(sockaddr[0])
        if not _is_public_ip(address):
            # Reject mixed public/private responses as a whole. Selecting only
            # an apparently safe address would leave a DNS rebinding escape.
            raise MailboxImapTransportError("mailbox_imap_address_not_allowed")
        normalized_sockaddr = tuple(sockaddr)
        deduplication_key = (family, normalized_sockaddr)
        if deduplication_key in seen:
            continue
        seen.add(deduplication_key)
        resolved.append(
            _ResolvedImapAddress(
                family=family,
                socktype=socktype or socket.SOCK_STREAM,
                protocol=protocol or socket.IPPROTO_TCP,
                sockaddr=normalized_sockaddr,
            )
        )
    if not resolved:
        raise MailboxImapTransportError("mailbox_imap_address_not_allowed")
    return tuple(resolved)


def _open_pinned_tls_socket(
    *,
    hostname: str,
    addresses: tuple[_ResolvedImapAddress, ...],
    timeout_seconds: int,
    ssl_context: ssl.SSLContext,
) -> socket.socket:
    """Connect to an already verified address with one shared deadline."""

    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    for resolved in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        raw_socket: socket.socket | None = None
        try:
            raw_socket = socket.socket(
                resolved.family,
                resolved.socktype,
                resolved.protocol,
            )
            raw_socket.settimeout(remaining)
            raw_socket.connect(resolved.sockaddr)
            tls_socket = ssl_context.wrap_socket(
                raw_socket,
                server_hostname=hostname,
            )
            # ``wrap_socket`` preserves the timeout in CPython, but setting it
            # explicitly makes the remaining shared deadline unambiguous.
            tls_socket.settimeout(max(0.001, deadline - time.monotonic()))
            return tls_socket
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass
    if last_error is not None:
        raise last_error
    raise TimeoutError("IMAPS connection deadline exceeded")


class _PinnedIMAP4SSL(imaplib.IMAP4_SSL):
    """``imaplib`` client whose TCP peer cannot be replaced by a DNS rebind."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        addresses: tuple[_ResolvedImapAddress, ...],
        timeout_seconds: int,
        max_literal_bytes: int,
        max_response_line_bytes: int,
    ) -> None:
        self._pinned_addresses = addresses
        self._timeout_seconds = timeout_seconds
        self._max_literal_bytes = max_literal_bytes
        self._max_response_line_bytes = max_response_line_bytes
        self._uid_response_kind: str | None = None
        self._uid_search_response_bytes = 0
        self._uid_fetch_literal_bytes = 0
        self._uid_fetch_response_line_bytes = 0
        # ``imaplib`` defaults to an intentionally permissive stdlib context.
        # Mailbox credentials require ordinary browser-grade CA and hostname
        # verification in addition to the pinned TCP destination.
        super().__init__(
            host,
            port,
            ssl_context=ssl.create_default_context(),
            timeout=timeout_seconds,
        )

    def _create_socket(self, timeout: float | None) -> socket.socket:
        del timeout
        return _open_pinned_tls_socket(
            hostname=self.host,
            addresses=self._pinned_addresses,
            timeout_seconds=self._timeout_seconds,
            ssl_context=self.ssl_context,
        )

    def _close_after_response_limit(self) -> None:
        """Do not leave a connection with an unread oversized response."""

        try:
            self.shutdown()
        except (AttributeError, OSError):
            # This also runs while handling malformed greetings, where the
            # parent constructor may not have finished creating every field.
            pass

    def read(self, size: int) -> bytes:
        """Reject a declared IMAP literal before ``imaplib`` allocates it."""

        response_kind = getattr(self, "_uid_response_kind", None)
        fetch_literal_bytes = getattr(self, "_uid_fetch_literal_bytes", 0)
        if (
            size > self._max_literal_bytes
            or (
                response_kind == "fetch"
                and fetch_literal_bytes + size > self._max_literal_bytes
            )
        ):
            self._close_after_response_limit()
            raise MailboxImapResponseLimitError("mailbox_message_too_large")
        if response_kind == "fetch":
            self._uid_fetch_literal_bytes = fetch_literal_bytes + size
        # The stdlib implementation builds a large literal by repeatedly
        # concatenating intermediate byte strings. A bounded one-shot read
        # avoids that avoidable allocation amplification.
        data = self.file.read(size)
        if len(data) != size:
            raise self.abort("socket error: EOF")
        return data

    def readline(self) -> bytes:
        """Bound every tracked UID SEARCH/FETCH protocol response line."""

        line = self.file.readline(self._max_response_line_bytes + 1)
        response_kind = getattr(self, "_uid_response_kind", None)
        if len(line) > self._max_response_line_bytes:
            self._close_after_response_limit()
            raise MailboxImapResponseLimitError(
                "mailbox_search_response_too_large"
                if response_kind == "search"
                else "mailbox_imap_response_line_too_large"
            )
        if response_kind == "search":
            self._uid_search_response_bytes = (
                getattr(self, "_uid_search_response_bytes", 0) + len(line)
            )
            if self._uid_search_response_bytes > self._max_response_line_bytes:
                self._close_after_response_limit()
                raise MailboxImapResponseLimitError(
                    "mailbox_search_response_too_large"
                )
        elif response_kind == "fetch":
            # FETCH literals have a separate raw-message budget in ``read``.
            # The surrounding metadata still enters imaplib's response buffers,
            # so cap its cumulative size as well.
            self._uid_fetch_response_line_bytes = (
                getattr(self, "_uid_fetch_response_line_bytes", 0) + len(line)
            )
            if self._uid_fetch_response_line_bytes > self._max_response_line_bytes:
                self._close_after_response_limit()
                raise MailboxImapResponseLimitError(
                    "mailbox_imap_response_line_too_large"
                )
        return line

    def uid(self, command: str, *args: object):  # type: ignore[no-untyped-def]
        """Bound an entire UID SEARCH/FETCH response, not just one chunk."""

        command_kind = command.casefold()
        tracked_kind = command_kind if command_kind in {"search", "fetch"} else None
        if tracked_kind is not None:
            self._uid_response_kind = tracked_kind
            self._uid_search_response_bytes = 0
            self._uid_fetch_literal_bytes = 0
            self._uid_fetch_response_line_bytes = 0
        try:
            return super().uid(command, *args)
        finally:
            if tracked_kind is not None:
                self._uid_response_kind = None
                self._uid_search_response_bytes = 0
                self._uid_fetch_literal_bytes = 0
                self._uid_fetch_response_line_bytes = 0


def create_imap_client(
    settings: "AppSettings",
    *,
    host: str,
    port: int,
) -> imaplib.IMAP4_SSL:
    """Build a TLS-verified IMAP client for a permitted, pinned endpoint."""

    hostname = validate_imap_endpoint(settings, host=host, port=port)
    addresses = _resolve_public_addresses(
        hostname=hostname,
        port=port,
        max_addresses=settings.mailbox_imap_max_resolved_addresses,
    )
    try:
        return _PinnedIMAP4SSL(
            hostname,
            port,
            addresses=addresses,
            timeout_seconds=settings.mailbox_imap_connect_timeout_seconds,
            # ``BODY.PEEK[]<0.N>`` requests at most N bytes.  Permit the one
            # extra sentinel byte used to prove a message exceeds its raw
            # limit, but never let a peer-declared literal grow unbounded.
            max_literal_bytes=settings.mailbox_max_raw_message_bytes + 1,
            # IMAP SEARCH, STATUS and FETCH metadata are line-oriented. The
            # search budget is also the global protocol-line ceiling so an
            # untrusted server cannot make ``imaplib.readline`` allocate more.
            max_response_line_bytes=settings.mailbox_max_search_response_bytes,
        )
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailboxImapTransportError("mailbox_connection_failed") from exc


__all__ = [
    "IMAPS_PORT",
    "MailboxImapTransportError",
    "MailboxImapResponseLimitError",
    "create_imap_client",
    "validate_imap_endpoint",
]
