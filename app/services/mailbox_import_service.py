from __future__ import annotations

import base64
import hashlib
import imaplib
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterator, Literal
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, desc, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.models import (
    Candidate,
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    MailboxAttachmentContentIdentity,
    MailboxConfig,
    MailboxSyncFailureAlert,
    Resume,
)
from app.schemas import (
    MailboxConfigCreate,
    MailboxConfigListResponse,
    MailboxConfigPatch,
    MailboxConfigResponse,
    MailboxConfigUpdate,
    MailboxImportHistoryResponse,
    MailboxImportResponse,
    MailboxSyncAlertSummary,
    MailboxSyncResponse,
)
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)
from app.services.ai_extraction_job_service import enqueue_uploaded_resume_ai_extraction
from app.services.document_text_extraction import SUPPORTED_DOCUMENT_EXTENSIONS
from app.services.mailbox_retention_service import (
    MailboxRetentionError,
    discard_retained_failed_attachment,
    read_retained_failed_attachment,
    store_failed_attachment_copy,
    store_mailbox_body_copy,
    store_success_attachment_copy,
)
from app.services.mailbox_imap_transport import (
    MailboxImapResponseLimitError,
    MailboxImapTransportError,
    create_imap_client,
    validate_imap_endpoint,
)
from app.services.mailbox_sync_alert_service import (
    active_sync_alert,
    resolve_mailbox_sync_alert,
)
from app.services.resume_service import (
    UploadValidationError,
    create_candidate,
    discard_uploaded_pdf,
    save_pdf_resume,
)
from app.services.candidate_data_lifecycle_service import (
    CandidateDataLifecycleError,
    mailbox_attachment_is_tombstoned,
)


class MailboxImportError(RuntimeError):
    pass


class _RetryClaimLost(MailboxImportError):
    """A newer request owns this attachment retry now."""


class _ContentClaimLost(MailboxImportError):
    """A newer mailbox attachment owns this content identity now."""


_RETRY_LEASE_SECONDS = 180
_SYNC_LEASE_SECONDS = 600
_CONTENT_CLAIM_LEASE_SECONDS = 180
_IMAP_NZ_NUMBER_MAX = (1 << 32) - 1
_IMAP_CANONICAL_NZ_NUMBER_PATTERN = re.compile(rb"[1-9][0-9]{0,9}\Z")
_NON_RETRYABLE_ATTACHMENT_ERRORS = frozenset(
    {
        "attachment_validation_failed",
        "attachment_message_unavailable",
        "attachment_source_changed",
        "attachment_source_unavailable",
        "mailbox_message_too_large",
        "mailbox_message_headers_too_large",
        "mailbox_mime_structure_too_complex",
        "mailbox_attachment_count_exceeded",
        "mailbox_attachment_too_large",
        "mailbox_attachment_total_too_large",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite's naive timestamp reads for lease comparisons."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_imap_text(value: str) -> str:
    """Return one printable ASCII IMAP value or fail with a stable code.

    ``imaplib`` concatenates most command arguments without quoting or
    rejecting CRLF.  Validate every stored and request-supplied value again at
    the network boundary so legacy rows cannot inject a second IMAP command.
    Printable non-ASCII strings are rejected too: this client has not enabled
    IMAP UTF8 and would otherwise surface a raw ``UnicodeEncodeError``.
    """

    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MailboxImportError("mailbox_imap_argument_invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MailboxImportError("mailbox_imap_argument_invalid") from exc
    return value


def _quoted_imap_string(value: str) -> str:
    """Encode one validated value as an IMAP quoted string."""

    safe_value = _validated_imap_text(value)
    return '"' + safe_value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate_imap_connection_arguments(
    *,
    email_address: str,
    mailbox: str,
    password: str | None,
) -> None:
    _validated_imap_text(email_address)
    _validated_imap_text(mailbox)
    if password is not None:
        _validated_imap_text(password)


def _login_imap_client(
    client: imaplib.IMAP4_SSL,
    *,
    email_address: str,
    password: str,
) -> tuple[str, list[bytes]]:
    """Authenticate without allowing the username or password to add commands."""

    return client.login(
        _quoted_imap_string(email_address),
        _validated_imap_text(password),
    )


def _select_mailbox_readonly(
    client: imaplib.IMAP4_SSL,
    *,
    mailbox: str,
) -> tuple[str, list[bytes]]:
    return client.select(_quoted_imap_string(mailbox), readonly=True)


def _parse_imap_nz_number(value: object) -> int | None:
    """Parse an RFC IMAP ``nz-number`` without accepting alternate spellings."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= _IMAP_NZ_NUMBER_MAX else None
    if isinstance(value, bytes):
        raw_value = value
    elif isinstance(value, str):
        try:
            raw_value = value.encode("ascii")
        except UnicodeEncodeError:
            return None
    else:
        return None
    if _IMAP_CANONICAL_NZ_NUMBER_PATTERN.fullmatch(raw_value) is None:
        return None
    parsed = int(raw_value)
    return parsed if parsed <= _IMAP_NZ_NUMBER_MAX else None


def _canonical_imap_uid(value: object) -> tuple[int, bytes] | None:
    parsed = _parse_imap_nz_number(value)
    if parsed is None:
        return None
    return parsed, str(parsed).encode("ascii")


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Scope a worker-owned IMAP run to the mailbox workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _fernet(settings: AppSettings) -> Fernet:
    """Use a dedicated key when present, otherwise derive from app session key."""

    material = (
        settings.email_credentials_key
        or settings.session_secret
        or settings.admin_token
        or "resume-v3-development-session"
    )
    if settings.email_credentials_key:
        try:
            return Fernet(material.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise MailboxImportError("mailbox_credentials_key_invalid") from exc
    derived = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(derived)


def _safe_filename(value: str | None) -> str:
    if not value:
        return "attachment"
    try:
        decoded = str(make_header(decode_header(value)))
    except (TypeError, ValueError):
        decoded = value
    return (
        decoded.replace("/", "_")
        .replace("\\", "_")
        .replace("\x00", "")
        .strip()[:255]
        or "attachment"
    )


def _received_at(message: Message) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_MAILBOX_STATUS_VALUE_PATTERN = re.compile(
    r"\b(UIDVALIDITY|UIDNEXT)\s+([^\s()]+)",
    re.IGNORECASE,
)


def _parse_mailbox_status(data: list[bytes] | list[str] | None) -> tuple[int, int]:
    """Return the UIDVALIDITY and UIDNEXT values from an IMAP STATUS reply.

    IMAP servers format the surrounding STATUS text differently, so only the
    two protocol fields are parsed.  Any malformed reply is intentionally
    surfaced as a stable application error rather than returning raw server
    text to the user.
    """

    values: dict[str, int] = {}
    for item in data or []:
        if isinstance(item, bytes):
            try:
                text = item.decode("ascii")
            except UnicodeDecodeError as exc:
                raise MailboxImportError("mailbox_status_failed") from exc
        else:
            text = str(item)
        for name, value in _MAILBOX_STATUS_VALUE_PATTERN.findall(text):
            parsed = _parse_imap_nz_number(value)
            if parsed is None:
                raise MailboxImportError("mailbox_status_failed")
            values[name.upper()] = parsed
    uidvalidity = values.get("UIDVALIDITY")
    uidnext = values.get("UIDNEXT")
    if uidvalidity is None or uidnext is None:
        raise MailboxImportError("mailbox_status_failed")
    return uidvalidity, uidnext


def _read_mailbox_status(
    client: imaplib.IMAP4_SSL,
    *,
    mailbox: str,
) -> tuple[int, int]:
    """Read the mailbox watermark without selecting or scanning messages."""

    try:
        status, data = client.status(
            _quoted_imap_string(mailbox),
            "(UIDVALIDITY UIDNEXT)",
        )
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailboxImportError("mailbox_status_failed") from exc
    if status != "OK":
        raise MailboxImportError("mailbox_status_failed")
    return _parse_mailbox_status(data)


def _read_initial_mailbox_watermark(
    *,
    settings: AppSettings,
    imap_host: str,
    imap_port: int,
    email_address: str,
    mailbox: str,
    password: str,
) -> tuple[int, int]:
    """Authenticate once while binding and capture the starting UIDNEXT.

    This happens before changing the stored configuration.  A mistyped target
    therefore cannot accidentally replace a working mailbox or advance its
    import watermark.
    """

    client: imaplib.IMAP4_SSL | None = None
    try:
        _validate_imap_connection_arguments(
            email_address=email_address,
            mailbox=mailbox,
            password=password,
        )
        client = create_imap_client(
            settings,
            host=imap_host,
            port=imap_port,
        )
        login_status, _ = _login_imap_client(
            client,
            email_address=email_address,
            password=password,
        )
        if login_status != "OK":
            raise MailboxImportError("mailbox_connection_failed")
        return _read_mailbox_status(client, mailbox=mailbox)
    except MailboxImportError:
        raise
    except (imaplib.IMAP4.error, OSError, MailboxImapTransportError) as exc:
        if isinstance(exc, MailboxImapTransportError):
            raise MailboxImportError(str(exc)) from exc
        raise MailboxImportError("mailbox_connection_failed") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def _same_mailbox_source(
    config: MailboxConfig,
    *,
    imap_host: str,
    imap_port: int,
    email_address: str,
    mailbox: str,
) -> bool:
    """Compare connection coordinates while ignoring harmless host/email case."""

    return (
        config.imap_host.strip().casefold() == imap_host.casefold()
        and config.imap_port == imap_port
        and config.email_address.strip().casefold() == email_address.casefold()
        and config.mailbox.strip() == mailbox
    )


def _mailbox_source_fingerprint(config: MailboxConfig) -> str:
    """Return a non-reversible identity for the configured IMAP source."""

    source = "\x1f".join(
        (
            config.imap_host.strip().casefold(),
            str(config.imap_port),
            config.email_address.strip().casefold(),
            config.mailbox.strip(),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def mailbox_source_fingerprint(config: MailboxConfig) -> str:
    """Expose the safe source identity to the durable job queue.

    The digest deliberately excludes the encrypted password.  A queued sync
    therefore refuses to run if its host, account, port, or folder changed
    after it was accepted, without storing a reusable credential in the job.
    """

    return _mailbox_source_fingerprint(config)


def _config_response(config: MailboxConfig | None) -> MailboxConfigResponse:
    if config is None:
        return MailboxConfigResponse(configured=False)
    alert = active_sync_alert(config.sync_failure_alert)
    return MailboxConfigResponse(
        configured=True,
        mailbox_id=config.id,
        display_name=config.display_name,
        imap_host=config.imap_host,
        imap_port=config.imap_port,
        email_address=config.email_address,
        mailbox=config.mailbox,
        enabled=config.enabled,
        archived_at=config.archived_at,
        password_configured=bool(config.encrypted_password),
        import_started_at=config.import_started_at,
        last_synced_at=config.last_synced_at,
        last_sync_error=config.last_sync_error,
        active_sync_alert=(
            MailboxSyncAlertSummary(
                severity=alert.severity,  # type: ignore[arg-type]
                consecutive_failures=alert.consecutive_failures,
                opened_at=alert.opened_at or alert.last_failed_at,
                last_failed_at=alert.last_failed_at,
                last_error_code=alert.last_error_code or "mailbox_sync_failed",
            )
            if alert is not None
            and alert.opened_at is not None
            and alert.last_failed_at is not None
            else None
        ),
    )


def _normalized_display_name(value: str) -> tuple[str, str]:
    display_name = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display_name:
        raise MailboxImportError("mailbox_display_name_required")
    return display_name, display_name.casefold()


def _mailbox_config_or_error(
    session: Session,
    *,
    config_id: str,
    include_archived: bool = False,
) -> MailboxConfig:
    statement = select(MailboxConfig).where(MailboxConfig.id == config_id)
    if not include_archived:
        statement = statement.where(MailboxConfig.archived_at.is_(None))
    config = session.scalar(statement)
    if config is None:
        raise MailboxImportError("mailbox_config_not_found")
    return config


def _ensure_display_name_available(
    session: Session,
    *,
    display_name_key: str,
    excluding_config_id: str | None = None,
) -> None:
    statement = select(MailboxConfig.id).where(
        MailboxConfig.display_name_key == display_name_key
    )
    if excluding_config_id:
        statement = statement.where(MailboxConfig.id != excluding_config_id)
    if session.scalar(statement) is not None:
        raise MailboxImportError("mailbox_duplicate_display_name")


def _config_has_imports(session: Session, *, config_id: str) -> bool:
    return (
        session.scalar(
            select(EmailAttachmentImport.id)
            .where(EmailAttachmentImport.mailbox_config_id == config_id)
            .limit(1)
        )
        is not None
    )


def list_mailbox_configs(
    session: Session,
    *,
    include_archived: bool = False,
) -> MailboxConfigListResponse:
    statement = select(MailboxConfig)
    if not include_archived:
        statement = statement.where(MailboxConfig.archived_at.is_(None))
    configs = session.scalars(
        statement.order_by(desc(MailboxConfig.created_at), MailboxConfig.id)
    ).all()
    return MailboxConfigListResponse(
        items=[_config_response(config) for config in configs],
        total=len(configs),
    )


def get_mailbox_config_by_id(session: Session, *, config_id: str) -> MailboxConfigResponse:
    return _config_response(
        _mailbox_config_or_error(session, config_id=config_id, include_archived=True)
    )


def _legacy_single_config(session: Session) -> MailboxConfig | None:
    """Return the one active config for compatibility-only endpoints.

    The former API had no mailbox ID.  It is safe only while exactly one
    active channel exists.  Choosing the newest channel after a workspace
    adds another source would send commands to the wrong mailbox.
    """

    configs = session.scalars(
        select(MailboxConfig)
        .where(MailboxConfig.archived_at.is_(None))
        .order_by(desc(MailboxConfig.created_at), MailboxConfig.id)
        .limit(2)
    ).all()
    if len(configs) > 1:
        raise MailboxImportError("mailbox_legacy_endpoint_ambiguous")
    return configs[0] if configs else None


def _next_legacy_mailbox_label(session: Session) -> str:
    """Keep the compatibility create route usable after an archive.

    Archived channels retain their labels for auditability, so a newly created
    legacy channel must use the same deterministic suffixing as migration
    backfill instead of colliding with a hidden archived default.
    """

    base_label = "默认收件邮箱"
    index = 1
    while True:
        label = base_label if index == 1 else f"{base_label} {index}"
        _, key = _normalized_display_name(label)
        if session.scalar(
            select(MailboxConfig.id).where(MailboxConfig.display_name_key == key)
        ) is None:
            return label
        index += 1


def get_mailbox_config(session: Session) -> MailboxConfigResponse:
    return _config_response(_legacy_single_config(session))


def _encrypt_password(settings: AppSettings, password: str) -> str:
    return _fernet(settings).encrypt(password.encode("utf-8")).decode("ascii")


def _decrypt_password(settings: AppSettings, encrypted_password: str) -> str:
    try:
        return _fernet(settings).decrypt(encrypted_password.encode("ascii")).decode("utf-8")
    except (MailboxImportError, InvalidToken, UnicodeDecodeError) as exc:
        raise MailboxImportError("mailbox_credentials_unavailable") from exc


def _update_config_values(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig | None,
    display_name: str,
    imap_host: str,
    imap_port: int,
    email_address: str,
    mailbox: str,
    password: str | None,
    enabled: bool,
) -> MailboxConfig:
    """Persist one source after validating its source identity and watermark."""

    normalized_name, display_name_key = _normalized_display_name(display_name)
    _ensure_display_name_available(
        session,
        display_name_key=display_name_key,
        excluding_config_id=config.id if config is not None else None,
    )
    try:
        normalized_host = validate_imap_endpoint(
            settings,
            host=imap_host,
            port=imap_port,
        )
    except MailboxImapTransportError as exc:
        raise MailboxImportError(str(exc)) from exc
    _validate_imap_connection_arguments(
        email_address=email_address,
        mailbox=mailbox,
        password=password,
    )
    normalized_email = email_address.strip()
    normalized_mailbox = mailbox.strip()
    _validate_imap_connection_arguments(
        email_address=normalized_email,
        mailbox=normalized_mailbox,
        password=None,
    )
    source_changed = config is None or not _same_mailbox_source(
        config,
        imap_host=normalized_host,
        imap_port=imap_port,
        email_address=normalized_email,
        mailbox=normalized_mailbox,
    )
    if config is not None and source_changed and _config_has_imports(session, config_id=config.id):
        # A retry must always point to the exact historical source.  Reusing
        # an existing channel for another inbox would break that guarantee.
        raise MailboxImportError("mailbox_source_identity_locked")

    needs_watermark = source_changed or (
        config is not None
        and (config.import_start_uid is None or config.imap_uidvalidity is None)
    )
    encrypted_password = config.encrypted_password if config is not None else ""
    if password is not None:
        encrypted_password = _encrypt_password(settings, password)
    if not encrypted_password:
        raise MailboxImportError("mailbox_password_required")
    if needs_watermark:
        binding_password = password or _decrypt_password(settings, encrypted_password)
        imap_uidvalidity, import_start_uid = _read_initial_mailbox_watermark(
            settings=settings,
            imap_host=normalized_host,
            imap_port=imap_port,
            email_address=normalized_email,
            mailbox=normalized_mailbox,
            password=binding_password,
        )
        import_started_at = _utcnow()

    if config is None:
        config = MailboxConfig(
            display_name=normalized_name,
            display_name_key=display_name_key,
            imap_host=normalized_host,
            imap_port=imap_port,
            email_address=normalized_email,
            mailbox=normalized_mailbox,
            encrypted_password=encrypted_password,
            enabled=enabled,
            import_start_uid=import_start_uid,
            imap_uidvalidity=imap_uidvalidity,
            import_started_at=import_started_at,
        )
        session.add(config)
        return config

    config.display_name = normalized_name
    config.display_name_key = display_name_key
    config.imap_host = normalized_host
    config.imap_port = imap_port
    config.email_address = normalized_email
    config.mailbox = normalized_mailbox
    config.encrypted_password = encrypted_password
    config.enabled = enabled
    if needs_watermark:
        config.import_start_uid = import_start_uid
        config.imap_uidvalidity = imap_uidvalidity
        config.import_started_at = import_started_at
        # The worker should check a newly bound mailbox immediately.  The
        # stored UIDNEXT keeps that check from importing its history.
        config.last_synced_at = None
    config.last_sync_error = None
    if not enabled:
        resolve_mailbox_sync_alert(
            session,
            mailbox_config_id=config.id,
            resolution="disabled",
        )
    elif source_changed:
        resolve_mailbox_sync_alert(
            session,
            mailbox_config_id=config.id,
            resolution="reconfigured",
        )
    return config


def create_mailbox_config(
    session: Session,
    *,
    settings: AppSettings,
    payload: MailboxConfigCreate,
) -> MailboxConfigResponse:
    config = _update_config_values(
        session,
        settings=settings,
        config=None,
        display_name=payload.display_name,
        imap_host=payload.imap_host,
        imap_port=payload.imap_port,
        email_address=payload.email_address,
        mailbox=payload.mailbox,
        password=payload.password,
        enabled=payload.enabled,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise MailboxImportError("mailbox_duplicate_display_name") from exc
    return _config_response(config)


def update_mailbox_config(
    session: Session,
    *,
    settings: AppSettings,
    config_id: str,
    payload: MailboxConfigPatch,
) -> MailboxConfigResponse:
    config = _mailbox_config_or_error(session, config_id=config_id)
    config = _update_config_values(
        session,
        settings=settings,
        config=config,
        display_name=payload.display_name if payload.display_name is not None else config.display_name,
        imap_host=payload.imap_host if payload.imap_host is not None else config.imap_host,
        imap_port=payload.imap_port if payload.imap_port is not None else config.imap_port,
        email_address=(
            payload.email_address if payload.email_address is not None else config.email_address
        ),
        mailbox=payload.mailbox if payload.mailbox is not None else config.mailbox,
        password=payload.password,
        enabled=payload.enabled if payload.enabled is not None else config.enabled,
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise MailboxImportError("mailbox_duplicate_display_name") from exc
    return _config_response(config)


def archive_mailbox_config(
    session: Session,
    *,
    config_id: str,
) -> MailboxConfigResponse:
    config = _mailbox_config_or_error(session, config_id=config_id)
    config.enabled = False
    config.archived_at = _utcnow()
    config.sync_lease_token = None
    config.sync_lease_expires_at = None
    resolve_mailbox_sync_alert(
        session,
        mailbox_config_id=config.id,
        resolution="archived",
    )
    session.commit()
    return _config_response(config)


def save_mailbox_config(
    session: Session,
    *,
    settings: AppSettings,
    payload: MailboxConfigUpdate,
) -> MailboxConfigResponse:
    config = _legacy_single_config(session)
    if config is None:
        if not payload.password:
            raise MailboxImportError("mailbox_password_required")
        return create_mailbox_config(
            session,
            settings=settings,
            payload=MailboxConfigCreate(
                display_name=_next_legacy_mailbox_label(session),
                imap_host=payload.imap_host,
                imap_port=payload.imap_port,
                email_address=payload.email_address,
                mailbox=payload.mailbox,
                password=payload.password,
                enabled=payload.enabled,
            ),
        )
    return update_mailbox_config(
        session,
        settings=settings,
        config_id=config.id,
        payload=MailboxConfigPatch(
            imap_host=payload.imap_host,
            imap_port=payload.imap_port,
            email_address=payload.email_address,
            mailbox=payload.mailbox,
            password=payload.password,
            enabled=payload.enabled,
        ),
    )


def list_mailbox_imports(
    session: Session,
    *,
    limit: int = 40,
    mailbox_config_id: str | None = None,
) -> MailboxImportHistoryResponse:
    if mailbox_config_id is not None:
        _mailbox_config_or_error(
            session,
            config_id=mailbox_config_id,
            include_archived=True,
        )
    visible_resume = exists(
        select(Resume.id)
        .join(Candidate, Candidate.id == Resume.candidate_id)
        .where(
            Resume.id == EmailAttachmentImport.resume_id,
            Resume.deleted_at.is_(None),
            Candidate.deleted_at.is_(None),
        )
    )
    visibility_predicate = or_(
        EmailAttachmentImport.resume_id.is_(None),
        visible_resume,
    )
    statement = select(EmailAttachmentImport).options(
        selectinload(EmailAttachmentImport.mailbox_config)
    ).where(visibility_predicate)
    count_statement = select(func.count()).select_from(EmailAttachmentImport).where(
        visibility_predicate
    )
    if mailbox_config_id is not None:
        statement = statement.where(EmailAttachmentImport.mailbox_config_id == mailbox_config_id)
        count_statement = count_statement.where(
            EmailAttachmentImport.mailbox_config_id == mailbox_config_id
        )
    records = session.scalars(
        statement
        .order_by(
            desc(EmailAttachmentImport.last_attempted_at),
            desc(EmailAttachmentImport.created_at),
        )
        .limit(limit)
    ).all()
    total = session.scalar(count_statement)
    return MailboxImportHistoryResponse(
        items=[_import_response(item) for item in records],
        total=int(total or 0),
    )


def _has_retryable_source(item: EmailAttachmentImport) -> bool:
    return bool(
        item.source_uidvalidity is not None
        and item.source_fingerprint
        and item.error not in _NON_RETRYABLE_ATTACHMENT_ERRORS
    )


def mailbox_attachment_has_retryable_remote_source(
    item: EmailAttachmentImport,
) -> bool:
    """Expose only the safe source-availability decision to job enqueueing."""

    return _has_retryable_source(item)


def _has_retained_retry_copy(item: EmailAttachmentImport) -> bool:
    now = _utcnow()
    return any(
        replica.kind == "failed_attachment"
        and replica.cleaned_at is None
        and (_as_utc(replica.expires_at) or now) > now
        for replica in item.retention_replicas
    )


def _can_retry(item: EmailAttachmentImport) -> bool:
    if not (_has_retryable_source(item) or _has_retained_retry_copy(item)):
        return False
    if item.status == "failed":
        return True
    if item.status == "retrying":
        lease_expires_at = _as_utc(item.retry_lease_expires_at)
        return lease_expires_at is not None and lease_expires_at <= _utcnow()
    return False


def get_retryable_mailbox_attachment(
    session: Session,
    *,
    import_id: str,
) -> EmailAttachmentImport:
    """Return an attachment that may be queued for an exact retry.

    This intentionally does not claim the attachment or touch IMAP.  The
    durable worker owns the later claim so an accepted HTTP request remains a
    cheap database operation and queued work stays distinguishable from work
    that is actually running.
    """

    record = session.scalar(
        select(EmailAttachmentImport).where(EmailAttachmentImport.id == import_id)
    )
    if record is None:
        raise MailboxImportError("mailbox_import_not_found")
    if record.resume_id is not None:
        visible = session.scalar(
            select(Resume.id)
            .join(Candidate, Candidate.id == Resume.candidate_id)
            .where(
                Resume.id == record.resume_id,
                Resume.deleted_at.is_(None),
                Candidate.deleted_at.is_(None),
            )
        )
        if visible is None:
            raise MailboxImportError("mailbox_import_not_found")
    if not _can_retry(record):
        raise MailboxImportError("mailbox_import_not_retryable")
    return record


def _import_response(item: EmailAttachmentImport) -> MailboxImportResponse:
    return MailboxImportResponse(
        import_id=item.id,
        mailbox_config_id=item.mailbox_config_id,
        mailbox_display_name=(
            item.mailbox_config.display_name if item.mailbox_config is not None else None
        ),
        attachment_filename=item.attachment_filename,
        status=item.status,
        error=item.error,
        resume_id=item.resume_id,
        attempt_count=item.attempt_count,
        last_attempted_at=item.last_attempted_at,
        can_retry=_can_retry(item),
        created_at=item.created_at,
    )


def _record(
    session: Session,
    *,
    config: MailboxConfig,
    uid: str,
    message_id: str | None,
    filename: str,
    attachment_sha256: str,
    status: str,
    error: str | None,
    resume_id: str | None,
    received_at: datetime | None,
    canonical_import_id: str | None = None,
    source_uidvalidity: int | None = None,
    trigger: str = "automatic",
    attempt_completed: bool = True,
) -> EmailAttachmentImport:
    now = _utcnow()
    record = EmailAttachmentImport(
        organization_id=config.organization_id,
        mailbox_config_id=config.id,
        message_uid=uid,
        message_id=message_id[:998] if message_id else None,
        attachment_filename=filename,
        attachment_sha256=attachment_sha256,
        source_uidvalidity=source_uidvalidity,
        source_fingerprint=(
            _mailbox_source_fingerprint(config) if source_uidvalidity is not None else None
        ),
        status=status,
        error=error,
        resume_id=resume_id,
        canonical_import_id=canonical_import_id,
        attempt_count=1,
        last_attempted_at=now,
        received_at=received_at,
        updated_at=now,
    )
    session.add(record)
    session.flush()
    session.add(
        EmailAttachmentImportAttempt(
            organization_id=config.organization_id,
            email_attachment_import_id=record.id,
            attempt_number=1,
            trigger=trigger,
            status=status,
            error=error,
            resume_id=resume_id,
            started_at=now,
            completed_at=now if attempt_completed else None,
        )
    )
    session.flush()
    return record


@dataclass(frozen=True)
class _ContentClaim:
    """The outcome of claiming one workspace-scoped attachment byte identity."""

    outcome: Literal["owner", "duplicate", "waiting", "deleted"]
    identity_id: str
    owner_import_id: str | None = None
    claim_token: str | None = None
    canonical_import_id: str | None = None
    canonical_resume_id: str | None = None


def _content_claim_is_active(
    identity: MailboxAttachmentContentIdentity,
    *,
    now: datetime,
) -> bool:
    lease_expires_at = _as_utc(identity.claim_lease_expires_at)
    return bool(
        identity.status == "processing"
        and identity.claim_token
        and lease_expires_at is not None
        and lease_expires_at > now
    )


def _historical_canonical_import(
    session: Session,
    *,
    organization_id: str,
    attachment_sha256: str,
) -> EmailAttachmentImport | None:
    """Adopt a successful pre-identity import on first forwarded duplicate.

    The migration only creates schema.  Reading a historical successful import
    here keeps the upgrade safe across SQLite and PostgreSQL without a
    potentially long data migration, while still making the new unique table
    the concurrency boundary from this first lookup onward.
    """

    return session.scalar(
        select(EmailAttachmentImport)
        .join(Resume, Resume.id == EmailAttachmentImport.resume_id)
        .where(
            EmailAttachmentImport.organization_id == organization_id,
            EmailAttachmentImport.attachment_sha256 == attachment_sha256,
            EmailAttachmentImport.status == "imported",
            EmailAttachmentImport.resume_id.is_not(None),
            Resume.organization_id == organization_id,
        )
        .order_by(EmailAttachmentImport.created_at, EmailAttachmentImport.id)
    )


def _identity_has_canonical_resume(
    session: Session,
    *,
    identity: MailboxAttachmentContentIdentity,
    organization_id: str,
) -> bool:
    if not identity.canonical_import_id or not identity.canonical_resume_id:
        return False
    return (
        session.scalar(
            select(Resume.id).where(
                Resume.id == identity.canonical_resume_id,
                Resume.organization_id == organization_id,
            )
        )
        is not None
    )


def _content_identity_claim_statement(
    *,
    organization_id: str,
    attachment_sha256: str,
):
    """Build the locking read used to handshake owners and waiters."""

    return (
        select(MailboxAttachmentContentIdentity)
        .where(
            MailboxAttachmentContentIdentity.organization_id == organization_id,
            MailboxAttachmentContentIdentity.attachment_sha256 == attachment_sha256,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _mark_expired_content_owner_failed(
    session: Session,
    *,
    organization_id: str,
    previous_owner_import_id: str | None,
    current_import_id: str,
    now: datetime,
) -> None:
    """Release an abandoned owner audit row before another mail takes over."""

    if not previous_owner_import_id or previous_owner_import_id == current_import_id:
        return
    session.execute(
        update(EmailAttachmentImport)
        .where(
            EmailAttachmentImport.id == previous_owner_import_id,
            EmailAttachmentImport.organization_id == organization_id,
            EmailAttachmentImport.status.in_(("processing", "retrying")),
        )
        .values(
            status="failed",
            error="attachment_content_claim_expired",
            resume_id=None,
            canonical_import_id=None,
            last_attempted_at=now,
            retry_lease_expires_at=None,
            retry_claim_token=None,
            updated_at=now,
        )
    )
    session.execute(
        update(EmailAttachmentImportAttempt)
        .where(
            EmailAttachmentImportAttempt.organization_id == organization_id,
            EmailAttachmentImportAttempt.email_attachment_import_id
            == previous_owner_import_id,
            EmailAttachmentImportAttempt.status.in_(("processing", "retrying")),
            EmailAttachmentImportAttempt.completed_at.is_(None),
        )
        .values(
            status="failed",
            error="attachment_content_claim_expired",
            completed_at=now,
        )
    )


def _recover_expired_content_claims(
    session: Session,
    *,
    organization_id: str,
) -> int:
    """Release abandoned canonical claims before an incremental mailbox scan.

    The owner lease is committed before document conversion deliberately, so a
    second forwarded copy can see and wait for it.  If that process exits
    between the claim commit and completion, the source UID would otherwise be
    considered handled forever by ``_known_message_uids``.  Close the owner
    and every waiter as retryable failures once the lease expires; a manual
    retry or a later forwarded copy can safely claim the bytes again.
    """

    now = _utcnow()
    identities = list(
        session.scalars(
            select(MailboxAttachmentContentIdentity).where(
                MailboxAttachmentContentIdentity.organization_id == organization_id,
                MailboxAttachmentContentIdentity.status == "processing",
                or_(
                    MailboxAttachmentContentIdentity.claim_lease_expires_at.is_(None),
                    MailboxAttachmentContentIdentity.claim_lease_expires_at <= now,
                ),
            )
        ).all()
    )
    recovered = 0
    for identity in identities:
        previous_owner_import_id = identity.processing_import_id
        attachment_sha256 = identity.attachment_sha256
        released = session.execute(
            update(MailboxAttachmentContentIdentity)
            .where(
                MailboxAttachmentContentIdentity.id == identity.id,
                MailboxAttachmentContentIdentity.organization_id == organization_id,
                MailboxAttachmentContentIdentity.status == "processing",
                or_(
                    MailboxAttachmentContentIdentity.claim_lease_expires_at.is_(None),
                    MailboxAttachmentContentIdentity.claim_lease_expires_at <= now,
                ),
            )
            .values(
                status="failed",
                processing_import_id=None,
                canonical_import_id=None,
                canonical_resume_id=None,
                claim_token=None,
                claim_lease_expires_at=None,
                last_error="attachment_content_claim_expired",
                updated_at=now,
            )
        )
        if released.rowcount != 1:
            session.expire_all()
            continue
        _mark_expired_content_owner_failed(
            session,
            organization_id=organization_id,
            previous_owner_import_id=previous_owner_import_id,
            current_import_id="",
            now=now,
        )
        _resolve_waiting_content_imports(
            session,
            organization_id=organization_id,
            attachment_sha256=attachment_sha256,
            canonical_import_id=None,
            canonical_resume_id=None,
            status="failed",
            error="attachment_content_claim_expired",
            now=now,
        )
        recovered += 1
    if recovered:
        session.commit()
    return recovered


def _claim_attachment_content(
    session: Session,
    *,
    record: EmailAttachmentImport,
    settings: AppSettings,
) -> _ContentClaim:
    """Atomically make one import the owner of its attachment bytes.

    The caller has already created an audit row for the mail attachment.  A
    SAVEPOINT isolates a duplicate-key race from that audit row, then the
    unique `(organization_id, attachment_sha256)` constraint decides which
    worker may create a candidate.  Slow file parsing happens only after the
    winning claim is committed.
    """

    organization_id = organization_context_id(session)
    if record.organization_id != organization_id:
        raise MailboxImportError("mailbox_workspace_mismatch")

    try:
        if mailbox_attachment_is_tombstoned(
            session,
            settings=settings,
            attachment_sha256=record.attachment_sha256,
        ):
            # This is intentionally a terminal non-owner result: automatic
            # IMAP polling and attachment retries never recreate a candidate
            # the workspace has deleted, while a fresh manual upload remains
            # an explicit separate choice.
            return _ContentClaim(outcome="deleted", identity_id="tombstone")
    except CandidateDataLifecycleError as exc:
        raise MailboxImportError("mailbox_attachment_tombstone_unavailable") from exc

    now = _utcnow()
    for _ in range(4):
        # This row is the handshake between an active canonical owner and
        # every forwarded copy.  The waiter keeps the lock until its
        # ``deduplicating`` audit row commits.  An owner finishing at the same
        # time must therefore resolve that committed waiter; if the owner won
        # the lock first, PostgreSQL returns its new terminal state here and
        # the copy is written terminally straight away.
        identity = session.scalar(
            _content_identity_claim_statement(
                organization_id=organization_id,
                attachment_sha256=record.attachment_sha256,
            )
        )

        if identity is None:
            historical = _historical_canonical_import(
                session,
                organization_id=organization_id,
                attachment_sha256=record.attachment_sha256,
            )
            if historical is not None:
                candidate_identity = MailboxAttachmentContentIdentity(
                    organization_id=organization_id,
                    attachment_sha256=record.attachment_sha256,
                    status="imported",
                    canonical_import_id=historical.id,
                    canonical_resume_id=historical.resume_id,
                    last_error=None,
                )
                outcome: Literal["owner", "duplicate"] = "duplicate"
                token = None
            else:
                token = uuid4().hex
                candidate_identity = MailboxAttachmentContentIdentity(
                    organization_id=organization_id,
                    attachment_sha256=record.attachment_sha256,
                    status="processing",
                    processing_import_id=record.id,
                    claim_token=token,
                    claim_lease_expires_at=now
                    + timedelta(seconds=_CONTENT_CLAIM_LEASE_SECONDS),
                    last_error=None,
                )
                outcome = "owner"
            try:
                # ``begin_nested`` keeps the newly-created mail audit record
                # alive if a second worker committed the same identity first.
                with session.begin_nested():
                    session.add(candidate_identity)
                    session.flush()
            except IntegrityError:
                session.expire_all()
                continue
            if outcome == "duplicate":
                return _ContentClaim(
                    outcome="duplicate",
                    identity_id=candidate_identity.id,
                    canonical_import_id=historical.id,
                    canonical_resume_id=historical.resume_id,
                )
            return _ContentClaim(
                outcome="owner",
                identity_id=candidate_identity.id,
                owner_import_id=record.id,
                claim_token=token,
            )

        if identity.status == "imported":
            if _identity_has_canonical_resume(
                session,
                identity=identity,
                organization_id=organization_id,
            ):
                return _ContentClaim(
                    outcome="duplicate",
                    identity_id=identity.id,
                    canonical_import_id=identity.canonical_import_id,
                    canonical_resume_id=identity.canonical_resume_id,
                )
            # A manually removed canonical resume must never leave this hash
            # permanently blocked.  Convert the stale identity back to a
            # retryable state, then claim it in the next loop iteration.
            reset = session.execute(
                update(MailboxAttachmentContentIdentity)
                .where(
                    MailboxAttachmentContentIdentity.id == identity.id,
                    MailboxAttachmentContentIdentity.organization_id == organization_id,
                    MailboxAttachmentContentIdentity.status == "imported",
                )
                .values(
                    status="failed",
                    processing_import_id=None,
                    canonical_import_id=None,
                    canonical_resume_id=None,
                    claim_token=None,
                    claim_lease_expires_at=None,
                    last_error="canonical_resume_unavailable",
                    updated_at=now,
                )
            )
            if reset.rowcount == 1:
                session.expire_all()
            continue

        if _content_claim_is_active(identity, now=now):
            return _ContentClaim(
                outcome="waiting",
                identity_id=identity.id,
                owner_import_id=identity.processing_import_id,
            )

        claim_token = uuid4().hex
        previous_owner_import_id = identity.processing_import_id
        if identity.status == "failed":
            claim_conditions = (MailboxAttachmentContentIdentity.status == "failed",)
        elif identity.status == "processing":
            claim_conditions = (
                MailboxAttachmentContentIdentity.status == "processing",
                or_(
                    MailboxAttachmentContentIdentity.claim_lease_expires_at.is_(None),
                    MailboxAttachmentContentIdentity.claim_lease_expires_at <= now,
                ),
            )
        else:
            # Future/invalid values are never trusted as a completed import.
            claim_conditions = (MailboxAttachmentContentIdentity.status == identity.status,)

        claimed = session.execute(
            update(MailboxAttachmentContentIdentity)
            .execution_options(synchronize_session=False)
            .where(
                MailboxAttachmentContentIdentity.id == identity.id,
                MailboxAttachmentContentIdentity.organization_id == organization_id,
                *claim_conditions,
            )
            .values(
                status="processing",
                processing_import_id=record.id,
                canonical_import_id=None,
                canonical_resume_id=None,
                claim_token=claim_token,
                claim_lease_expires_at=now
                + timedelta(seconds=_CONTENT_CLAIM_LEASE_SECONDS),
                last_error=None,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            session.expire_all()
            continue
        if identity.status == "processing":
            _mark_expired_content_owner_failed(
                session,
                organization_id=organization_id,
                previous_owner_import_id=previous_owner_import_id,
                current_import_id=record.id,
                now=now,
            )
        return _ContentClaim(
            outcome="owner",
            identity_id=identity.id,
            owner_import_id=record.id,
            claim_token=claim_token,
        )

    raise MailboxImportError("mailbox_content_claim_conflict")


def _resolve_waiting_content_imports(
    session: Session,
    *,
    organization_id: str,
    attachment_sha256: str,
    canonical_import_id: str | None,
    canonical_resume_id: str | None,
    status: Literal["imported", "failed"],
    error: str | None,
    now: datetime,
) -> None:
    """Finish forwarded mails that arrived while the canonical bytes ran."""

    waiting_ids = list(
        session.scalars(
            select(EmailAttachmentImport.id).where(
                EmailAttachmentImport.organization_id == organization_id,
                EmailAttachmentImport.attachment_sha256 == attachment_sha256,
                EmailAttachmentImport.status == "deduplicating",
            )
        ).all()
    )
    if not waiting_ids:
        return

    terminal_status = "duplicate" if status == "imported" else "failed"
    terminal_error = None if status == "imported" else error
    session.execute(
        update(EmailAttachmentImport)
        .where(
            EmailAttachmentImport.organization_id == organization_id,
            EmailAttachmentImport.id.in_(waiting_ids),
            EmailAttachmentImport.status == "deduplicating",
        )
        .values(
            status=terminal_status,
            error=terminal_error,
            resume_id=canonical_resume_id if status == "imported" else None,
            canonical_import_id=canonical_import_id if status == "imported" else None,
            last_attempted_at=now,
            updated_at=now,
        )
    )
    session.execute(
        update(EmailAttachmentImportAttempt)
        .where(
            EmailAttachmentImportAttempt.organization_id == organization_id,
            EmailAttachmentImportAttempt.email_attachment_import_id.in_(waiting_ids),
            EmailAttachmentImportAttempt.status == "deduplicating",
            EmailAttachmentImportAttempt.completed_at.is_(None),
        )
        .values(
            status=terminal_status,
            error=terminal_error,
            resume_id=canonical_resume_id if status == "imported" else None,
            completed_at=now,
        )
    )


def _complete_content_claim(
    session: Session,
    *,
    claim: _ContentClaim,
    attachment_sha256: str,
    status: Literal["imported", "failed"],
    error: str | None,
    canonical_import_id: str | None,
    canonical_resume_id: str | None,
) -> None:
    """Close a content identity only if this request still owns its token."""

    if claim.outcome != "owner" or not claim.owner_import_id or not claim.claim_token:
        raise _ContentClaimLost("mailbox_content_claim_lost")
    organization_id = organization_context_id(session)
    now = _utcnow()
    completed = session.execute(
        update(MailboxAttachmentContentIdentity)
        .where(
            MailboxAttachmentContentIdentity.id == claim.identity_id,
            MailboxAttachmentContentIdentity.organization_id == organization_id,
            MailboxAttachmentContentIdentity.status == "processing",
            MailboxAttachmentContentIdentity.processing_import_id == claim.owner_import_id,
            MailboxAttachmentContentIdentity.claim_token == claim.claim_token,
        )
        .values(
            status=status,
            processing_import_id=None,
            canonical_import_id=canonical_import_id if status == "imported" else None,
            canonical_resume_id=canonical_resume_id if status == "imported" else None,
            claim_token=None,
            claim_lease_expires_at=None,
            last_error=None if status == "imported" else error,
            updated_at=now,
        )
    )
    if completed.rowcount != 1:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    _resolve_waiting_content_imports(
        session,
        organization_id=organization_id,
        attachment_sha256=attachment_sha256,
        canonical_import_id=canonical_import_id,
        canonical_resume_id=canonical_resume_id,
        status=status,
        error=error,
        now=now,
    )


def _complete_processing_import(
    session: Session,
    *,
    record: EmailAttachmentImport,
    claim: _ContentClaim,
    status: Literal["imported", "failed"],
    error: str | None,
    resume_id: str | None,
) -> MailboxImportResponse:
    """Finish an automatic canonical import and its forwarded waiters."""

    organization_id = organization_context_id(session)
    now = _utcnow()
    completed = session.execute(
        update(EmailAttachmentImport)
        .where(
            EmailAttachmentImport.id == record.id,
            EmailAttachmentImport.organization_id == organization_id,
            EmailAttachmentImport.status == "processing",
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            canonical_import_id=record.id if status == "imported" else None,
            last_attempted_at=now,
            updated_at=now,
        )
    )
    if completed.rowcount != 1:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    _complete_content_claim(
        session,
        claim=claim,
        attachment_sha256=record.attachment_sha256,
        status=status,
        error=error,
        canonical_import_id=record.id if status == "imported" else None,
        canonical_resume_id=resume_id if status == "imported" else None,
    )
    session.expire_all()
    stored = session.scalar(
        select(EmailAttachmentImport).where(
            EmailAttachmentImport.id == record.id,
            EmailAttachmentImport.organization_id == organization_id,
        )
    )
    if stored is None:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    attempt = session.execute(
        update(EmailAttachmentImportAttempt)
        .where(
            EmailAttachmentImportAttempt.organization_id == organization_id,
            EmailAttachmentImportAttempt.email_attachment_import_id == stored.id,
            EmailAttachmentImportAttempt.attempt_number == stored.attempt_count,
            EmailAttachmentImportAttempt.status == "processing",
            EmailAttachmentImportAttempt.completed_at.is_(None),
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            completed_at=now,
        )
    )
    if attempt.rowcount != 1:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    session.commit()
    return _import_response(stored)


def _complete_non_owner_processing_import(
    session: Session,
    *,
    record: EmailAttachmentImport,
    claim: _ContentClaim,
) -> MailboxImportResponse:
    """Persist either a completed duplicate or an in-flight forwarding audit."""

    organization_id = organization_context_id(session)
    now = _utcnow()
    if claim.outcome == "duplicate":
        status = "duplicate"
        error = None
        resume_id = claim.canonical_resume_id
        canonical_import_id = claim.canonical_import_id
    elif claim.outcome == "waiting":
        status = "deduplicating"
        error = None
        resume_id = None
        canonical_import_id = None
    elif claim.outcome == "deleted":
        status = "skipped"
        error = "attachment_deleted_by_candidate_lifecycle"
        resume_id = None
        canonical_import_id = None
    else:
        raise MailboxImportError("mailbox_content_claim_conflict")

    completed = session.execute(
        update(EmailAttachmentImport)
        .where(
            EmailAttachmentImport.id == record.id,
            EmailAttachmentImport.organization_id == organization_id,
            EmailAttachmentImport.status == "processing",
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            canonical_import_id=canonical_import_id,
            last_attempted_at=now,
            updated_at=now,
        )
    )
    if completed.rowcount != 1:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    session.expire_all()
    stored = session.scalar(
        select(EmailAttachmentImport).where(
            EmailAttachmentImport.id == record.id,
            EmailAttachmentImport.organization_id == organization_id,
        )
    )
    if stored is None:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    attempt = session.execute(
        update(EmailAttachmentImportAttempt)
        .where(
            EmailAttachmentImportAttempt.organization_id == organization_id,
            EmailAttachmentImportAttempt.email_attachment_import_id == stored.id,
            EmailAttachmentImportAttempt.attempt_number == stored.attempt_count,
            EmailAttachmentImportAttempt.status == "processing",
            EmailAttachmentImportAttempt.completed_at.is_(None),
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            completed_at=now if status in {"duplicate", "skipped"} else None,
        )
    )
    if attempt.rowcount != 1:
        session.rollback()
        raise _ContentClaimLost("mailbox_content_claim_lost")
    session.commit()
    return _import_response(stored)


def _begin_automatic_content_import(
    session: Session,
    *,
    config: MailboxConfig,
    uid: str,
    message_id: str | None,
    filename: str,
    attachment_sha256: str,
    received_at: datetime | None,
    source_uidvalidity: int | None,
    settings: AppSettings,
) -> tuple[EmailAttachmentImport, _ContentClaim | None, MailboxImportResponse | None]:
    """Create the per-email audit row, then reserve or reuse its bytes."""

    record = _record(
        session,
        config=config,
        uid=uid,
        message_id=message_id,
        filename=filename,
        attachment_sha256=attachment_sha256,
        status="processing",
        error=None,
        resume_id=None,
        received_at=received_at,
        source_uidvalidity=source_uidvalidity,
        attempt_completed=False,
    )
    claim = _claim_attachment_content(session, record=record, settings=settings)
    if claim.outcome == "owner":
        # Do not keep a database write or unique-index lock while document
        # conversion runs.  The committed lease is what protects the owner.
        session.commit()
        return record, claim, None
    return record, None, _complete_non_owner_processing_import(
        session,
        record=record,
        claim=claim,
    )


def _already_imported(
    session: Session,
    *,
    config_id: str,
    organization_id: str,
    uid: str,
    digest: str,
) -> bool:
    return (
        session.scalar(
            select(EmailAttachmentImport.id).where(
                EmailAttachmentImport.mailbox_config_id == config_id,
                EmailAttachmentImport.organization_id == organization_id,
                EmailAttachmentImport.message_uid == uid,
                EmailAttachmentImport.attachment_sha256 == digest,
            )
        )
        is not None
    )


def _known_message_uids(
    session: Session,
    *,
    config_id: str,
    organization_id: str,
    message_uids: set[str],
) -> set[str]:
    """Return only selected UIDs that have already been handled.

    The old implementation loaded every historical UID for the mailbox into
    worker memory.  A bounded sync batch only needs to check the handful of
    UIDs it is about to fetch.
    """

    if not message_uids:
        return set()
    return set(
        session.scalars(
            select(EmailAttachmentImport.message_uid).where(
                EmailAttachmentImport.mailbox_config_id == config_id,
                EmailAttachmentImport.organization_id == organization_id,
                EmailAttachmentImport.message_uid.in_(message_uids),
            )
        ).all()
    )


@dataclass(frozen=True)
class _MailboxAttachment:
    filename: str
    content: bytes | None
    supported: bool
    ordinal: int


_MAILBOX_RESOURCE_LIMIT_ERRORS = frozenset(
    {
        "mailbox_message_too_large",
        "mailbox_message_headers_too_large",
        "mailbox_mime_structure_too_complex",
        "mailbox_attachment_count_exceeded",
        "mailbox_attachment_too_large",
        "mailbox_attachment_total_too_large",
    }
)
_RFC822_SIZE_PATTERN = re.compile(r"\bRFC822\.SIZE\s+(\d+)\b", re.IGNORECASE)


def _is_supported_document_filename(filename: str) -> bool:
    normalized = filename.casefold()
    return any(normalized.endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


def _iter_bounded_leaf_parts(
    message: Message,
    *,
    settings: AppSettings,
) -> Iterator[Message]:
    """Walk MIME iteratively while enforcing total part and depth budgets."""

    pending: list[tuple[Message, int]] = [(message, 0)]
    part_count = 0
    while pending:
        part, depth = pending.pop()
        part_count += 1
        if part_count > settings.mailbox_max_mime_parts or depth > settings.mailbox_max_mime_depth:
            raise MailboxImportError("mailbox_mime_structure_too_complex")
        if not part.is_multipart():
            yield part
            continue
        children = part.get_payload()
        if not isinstance(children, list):
            raise MailboxImportError("mailbox_mime_structure_too_complex")
        for child in reversed(children):
            if not isinstance(child, Message):
                raise MailboxImportError("mailbox_mime_structure_too_complex")
            pending.append((child, depth + 1))


def _encoded_payload_size(part: Message) -> int:
    """Estimate decoded payload size before allocating decoded attachment bytes."""

    encoded = part.get_payload(decode=False)
    transfer_encoding = part.get("Content-Transfer-Encoding", "").strip().casefold()
    if isinstance(encoded, str):
        if transfer_encoding != "base64":
            # Four bytes is a safe UTF-8 upper bound without allocating a
            # second copy of a potentially multi-megabyte MIME part.
            return len(encoded) * 4
        non_whitespace = 0
        previous = last = ""
        for character in encoded:
            if character.isspace():
                continue
            non_whitespace += 1
            previous, last = last, character
        padding = 2 if previous == last == "=" else 1 if last == "=" else 0
        return max(0, (non_whitespace // 4) * 3 - padding)
    elif isinstance(encoded, bytes):
        if transfer_encoding != "base64":
            return len(encoded)
        non_whitespace = 0
        previous = last = -1
        for byte in encoded:
            if byte in b" \t\r\n":
                continue
            non_whitespace += 1
            previous, last = last, byte
        padding = 2 if previous == last == ord("=") else 1 if last == ord("=") else 0
        return max(0, (non_whitespace // 4) * 3 - padding)
    else:
        return 0


def _attachments(
    message: Message,
    *,
    settings: AppSettings,
) -> list[_MailboxAttachment]:
    """Inspect all MIME parts before decoding any supported attachment.

    A message that exceeds a structural or decoded-byte budget is rejected as
    a whole.  That avoids importing the first few attachments and discovering
    an exhaustion payload only after work has already escaped the transaction.
    Unsupported files are audited without decoding their bytes.
    """

    candidates: list[tuple[str, Message, bool, int]] = []
    for part in _iter_bounded_leaf_parts(message, settings=settings):
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename or disposition not in {"attachment", "inline"}:
            continue
        safe_filename = _safe_filename(filename)
        candidates.append(
            (
                safe_filename,
                part,
                _is_supported_document_filename(safe_filename),
                len(candidates),
            )
        )
        if len(candidates) > settings.mailbox_max_attachments_per_message:
            raise MailboxImportError("mailbox_attachment_count_exceeded")

    estimated_total = 0
    for _, part, supported, _ in candidates:
        if not supported:
            continue
        estimate = _encoded_payload_size(part)
        if estimate > settings.max_upload_bytes:
            raise MailboxImportError("mailbox_attachment_too_large")
        estimated_total += estimate
        if estimated_total > settings.max_upload_bytes:
            raise MailboxImportError("mailbox_attachment_total_too_large")

    decoded_total = 0
    found: list[_MailboxAttachment] = []
    for filename, part, supported, ordinal in candidates:
        if not supported:
            found.append(
                _MailboxAttachment(
                    filename=filename,
                    content=None,
                    supported=False,
                    ordinal=ordinal,
                )
            )
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        if not isinstance(payload, bytes):
            raise MailboxImportError("mailbox_mime_structure_too_complex")
        if len(payload) > settings.max_upload_bytes:
            raise MailboxImportError("mailbox_attachment_too_large")
        decoded_total += len(payload)
        if decoded_total > settings.max_upload_bytes:
            raise MailboxImportError("mailbox_attachment_total_too_large")
        found.append(
            _MailboxAttachment(
                filename=filename,
                content=payload,
                supported=True,
                ordinal=ordinal,
            )
        )
    return found


def _message_body_bytes(message: Message, *, settings: AppSettings) -> bytes:
    """Return a bounded plain-text cache without decoding an oversized body."""

    remaining = settings.mailbox_max_body_cache_bytes
    parts: list[bytes] = []
    for part in _iter_bounded_leaf_parts(message, settings=settings):
        if remaining <= 0:
            break
        if part.get_filename() or part.get_content_type() != "text/plain":
            continue
        if _encoded_payload_size(part) > remaining:
            # Retention is optional. Skip a large mail body rather than
            # decoding it only to discard almost all of it.
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        clipped = payload[:remaining]
        parts.append(clipped)
        remaining -= len(clipped)
    return b"\n\n".join(parts)[: settings.mailbox_max_body_cache_bytes]


def _message_header_size(raw_message: bytes) -> int:
    separators = [
        position
        for position in (
            raw_message.find(b"\r\n\r\n"),
            raw_message.find(b"\n\n"),
        )
        if position >= 0
    ]
    return min(separators) if separators else len(raw_message)


def _bounded_message_factory(*, max_parts: int) -> Callable[..., Message]:
    """Stop ``email`` from constructing an unbounded MIME object tree.

    ``BytesParser`` normally creates every nested ``Message`` before callers
    can inspect the finished tree. Its parser probes a supplied factory once
    at construction time, so the first invocation below is deliberately not a
    real part; every later invocation is a root or MIME child.
    """

    factory_probe_seen = False
    part_count = 0

    def create_message(*, policy: policy.Policy) -> Message:
        nonlocal factory_probe_seen, part_count
        if not factory_probe_seen:
            factory_probe_seen = True
            return Message(policy=policy)
        part_count += 1
        if part_count > max_parts:
            raise MailboxImportError("mailbox_mime_structure_too_complex")
        return Message(policy=policy)

    return create_message


def _parse_mailbox_message(
    raw_message: bytes,
    *,
    settings: AppSettings,
) -> Message:
    if len(raw_message) > settings.mailbox_max_raw_message_bytes:
        raise MailboxImportError("mailbox_message_too_large")
    if _message_header_size(raw_message) > settings.mailbox_max_header_bytes:
        raise MailboxImportError("mailbox_message_headers_too_large")
    try:
        return BytesParser(
            _class=_bounded_message_factory(
                max_parts=settings.mailbox_max_mime_parts,
            ),
            policy=policy.default,
        ).parsebytes(raw_message)
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise MailboxImportError("mailbox_mime_structure_too_complex") from exc


def _parse_search_uids(
    data: list[bytes] | list[str] | None,
    *,
    settings: AppSettings,
    minimum_uid: int,
    maximum_uid: int,
) -> list[bytes]:
    chunks: list[bytes] = []
    size = 0
    for item in data or []:
        chunk = item if isinstance(item, bytes) else str(item).encode("utf-8")
        size += len(chunk)
        if size > settings.mailbox_max_search_response_bytes:
            raise MailboxImportError("mailbox_search_response_too_large")
        chunks.append(chunk)
    validated: list[bytes] = []
    for token in b" ".join(chunks).split():
        canonical = _canonical_imap_uid(token)
        if canonical is None:
            continue
        uid, raw_uid = canonical
        if minimum_uid <= uid <= maximum_uid:
            validated.append(raw_uid)
    return validated


def _extract_rfc822_size(data: list[object] | None) -> int | None:
    for item in data or []:
        values = item if isinstance(item, tuple) else (item,)
        for value in values:
            if isinstance(value, bytes):
                text = value.decode("ascii", errors="ignore")
            else:
                text = str(value)
            match = _RFC822_SIZE_PATTERN.search(text)
            if match is not None:
                try:
                    return int(match.group(1))
                except ValueError:
                    return None
    return None


def _fetch_message_size(
    client: imaplib.IMAP4_SSL,
    *,
    raw_uid: bytes,
) -> int | None:
    status, data = client.uid("fetch", raw_uid, "(RFC822.SIZE)")
    if status != "OK":
        return None
    return _extract_rfc822_size(data)


def _fetch_message_bytes(
    client: imaplib.IMAP4_SSL,
    *,
    raw_uid: bytes,
    max_bytes: int,
) -> bytes | None:
    # Request one byte beyond the policy ceiling. A compliant IMAP server
    # returns either the complete message or this bounded prefix, so we can
    # reject an oversized message without ever asking it to materialize the
    # full RFC822 payload. The pinned transport separately refuses a peer that
    # ignores this partial range and declares a larger literal.
    try:
        status, data = client.uid(
            "fetch",
            raw_uid,
            f"(BODY.PEEK[]<0.{max_bytes + 1}>)",
        )
    except MailboxImapResponseLimitError as exc:
        raise MailboxImportError(str(exc)) from exc
    if status != "OK" or not data:
        return None
    chunks: list[bytes] = []
    total = 0
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[1], bytes):
            continue
        chunk = item[1]
        total += len(chunk)
        if total > max_bytes:
            raise MailboxImportError("mailbox_message_too_large")
        chunks.append(chunk)
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


def _message_resource_digest(*, uid: str, error_code: str) -> str:
    return hashlib.sha256(
        f"mailbox-resource-v1\x1f{uid}\x1f{error_code}".encode("utf-8")
    ).hexdigest()


def _unsupported_attachment_digest(
    *,
    uid: str,
    filename: str,
    ordinal: int,
) -> str:
    return hashlib.sha256(
        f"mailbox-unsupported-v1\x1f{uid}\x1f{ordinal}\x1f{filename}".encode("utf-8")
    ).hexdigest()


def _store_replica_safely(session: Session, callback) -> None:
    """A transient cache must never turn a resume import into a failure."""

    try:
        callback()
        session.commit()
    except (MailboxRetentionError, SQLAlchemyError):
        # The source mailbox remains available for retry.  Do not persist a
        # filesystem path, email content, or provider detail in the import UI.
        # Callers invoke this helper only after their durable import record
        # committed, so this rollback cannot undo a candidate/resume import.
        session.rollback()
        return


class _AttachmentIngestionFailure(RuntimeError):
    """A UI-safe attachment failure plus an optional cleanup target."""

    def __init__(self, code: str, *, storage_key: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.storage_key = storage_key


def _ingest_attachment(
    session: Session,
    *,
    config: MailboxConfig,
    filename: str,
    content: bytes,
    settings: AppSettings,
) -> Resume:
    """Create one candidate/resume through the same path as browser upload."""

    resume: Resume | None = None
    try:
        candidate = create_candidate(session, display_name=None)
        if candidate.organization_id != config.organization_id:
            raise MailboxImportError("mailbox_workspace_mismatch")
        resume = save_pdf_resume(
            session,
            candidate_id=candidate.id,
            original_filename=filename,
            content=content,
            settings=settings,
        )
        if resume.organization_id != config.organization_id:
            raise MailboxImportError("mailbox_workspace_mismatch")
        # Keep a stable, source-side provenance record on the resume so the
        # library can filter by inbox without relying on mutable attachment
        # history.  The label is a snapshot: later renaming a channel must not
        # retroactively rewrite where a candidate entered the workspace.
        resume.ingestion_source_type = "mailbox_attachment"
        resume.source_mailbox_config_id = config.id
        resume.source_mailbox_label_snapshot = config.display_name
        if resume.extraction_status == "failed":
            raise _AttachmentIngestionFailure(
                "attachment_text_extraction_failed",
                storage_key=resume.storage_key,
            )
        enqueue_uploaded_resume_ai_extraction(session, resume=resume, settings=settings)
        return resume
    except _AttachmentIngestionFailure:
        raise
    except MailboxImportError:
        raise
    except UploadValidationError as exc:
        raise _AttachmentIngestionFailure("attachment_validation_failed") from exc
    except (RuntimeError, SQLAlchemyError) as exc:
        raise _AttachmentIngestionFailure(
            "attachment_import_failed",
            storage_key=resume.storage_key if resume is not None else None,
        ) from exc


def _discard_failed_attachment(
    *,
    settings: AppSettings,
    organization_id: str,
    failure: _AttachmentIngestionFailure,
) -> None:
    discard_uploaded_pdf(
        settings,
        storage_key=failure.storage_key,
        organization_id=organization_id,
    )


def _attachment_with_digest(
    message: Message,
    *,
    digest: str,
    settings: AppSettings,
) -> tuple[str, bytes] | None:
    """Return only the exact previously-recorded attachment content."""

    for attachment in _attachments(message, settings=settings):
        if attachment.content is None:
            continue
        if hashlib.sha256(attachment.content).hexdigest() == digest:
            return attachment.filename, attachment.content
    return None


def _complete_retry(
    session: Session,
    *,
    import_id: str,
    claim_token: str,
    status: str,
    error: str | None,
    resume_id: str | None,
    canonical_import_id: str | None = None,
    content_claim: _ContentClaim | None = None,
) -> MailboxImportResponse:
    """Commit a retry only while this request still owns its lease token."""

    now = _utcnow()
    expected_organization_id = organization_context_id(session)
    completed = session.execute(
        update(EmailAttachmentImport)
        .execution_options(synchronize_session=False)
        .where(
            EmailAttachmentImport.id == import_id,
            EmailAttachmentImport.organization_id == expected_organization_id,
            EmailAttachmentImport.status == "retrying",
            EmailAttachmentImport.retry_claim_token == claim_token,
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            canonical_import_id=canonical_import_id,
            last_attempted_at=now,
            retry_lease_expires_at=None,
            retry_claim_token=None,
            updated_at=now,
        )
    )
    if completed.rowcount != 1:
        # The candidate/resume created by this stale request remains in the
        # current transaction, so rolling back here prevents a second import.
        session.rollback()
        raise _RetryClaimLost("mailbox_import_retry_superseded")

    if content_claim is not None and content_claim.outcome == "owner":
        if status not in {"imported", "failed"}:
            session.rollback()
            raise _ContentClaimLost("mailbox_content_claim_lost")
        attachment_sha256 = session.scalar(
            select(EmailAttachmentImport.attachment_sha256).where(
                EmailAttachmentImport.id == import_id,
                EmailAttachmentImport.organization_id == expected_organization_id,
            )
        )
        if not attachment_sha256:
            session.rollback()
            raise _ContentClaimLost("mailbox_content_claim_lost")
        _complete_content_claim(
            session,
            claim=content_claim,
            attachment_sha256=attachment_sha256,
            status=status,
            error=error,
            canonical_import_id=import_id if status == "imported" else None,
            canonical_resume_id=resume_id if status == "imported" else None,
        )

    session.expire_all()
    record = session.scalar(
        select(EmailAttachmentImport).where(EmailAttachmentImport.id == import_id)
    )
    if record is None:
        session.rollback()
        raise _RetryClaimLost("mailbox_import_retry_superseded")
    completed_attempt = session.execute(
        update(EmailAttachmentImportAttempt)
        .where(
            EmailAttachmentImportAttempt.organization_id == expected_organization_id,
            EmailAttachmentImportAttempt.email_attachment_import_id == record.id,
            EmailAttachmentImportAttempt.attempt_number == record.attempt_count,
            EmailAttachmentImportAttempt.trigger == "manual_retry",
            EmailAttachmentImportAttempt.status == "retrying",
        )
        .values(
            status=status,
            error=error,
            resume_id=resume_id,
            completed_at=None if status == "deduplicating" else now,
        )
    )
    if completed_attempt.rowcount != 1:
        session.rollback()
        raise _RetryClaimLost("mailbox_import_retry_superseded")
    session.commit()
    return _import_response(record)


def _claim_retry(
    session: Session,
    *,
    import_id: str,
    lease_seconds: int = _RETRY_LEASE_SECONDS,
) -> EmailAttachmentImport:
    """Atomically claim one failed attachment without holding a DB lock over IMAP."""

    expected_organization_id = organization_context_id(session)
    record = session.scalar(
        select(EmailAttachmentImport).where(
            EmailAttachmentImport.id == import_id,
            EmailAttachmentImport.organization_id == expected_organization_id,
        )
    )
    if record is None:
        raise MailboxImportError("mailbox_import_not_found")

    now = _utcnow()
    claim_token = uuid4().hex
    previous_attempt_number = record.attempt_count
    previous_status = record.status
    if record.status == "failed":
        if not _can_retry(record):
            raise MailboxImportError("mailbox_import_not_retryable")
        claim_conditions = (EmailAttachmentImport.status == "failed",)
    elif record.status == "retrying":
        if not (_has_retryable_source(record) or _has_retained_retry_copy(record)):
            raise MailboxImportError("mailbox_import_not_retryable")
        lease_expires_at = _as_utc(record.retry_lease_expires_at)
        if (
            lease_expires_at is None
            or lease_expires_at > now
        ):
            raise MailboxImportError("mailbox_import_retry_in_progress")
        # Do not first reset the stale row to ``failed``.  The conditional
        # update below keeps a late finisher from overwriting a newer claim.
        claim_conditions = (
            EmailAttachmentImport.status == "retrying",
            EmailAttachmentImport.retry_lease_expires_at <= now,
        )
    else:
        raise MailboxImportError("mailbox_import_not_retryable")

    claimed = session.execute(
        update(EmailAttachmentImport)
        .execution_options(synchronize_session=False)
        .where(
            EmailAttachmentImport.id == record.id,
            EmailAttachmentImport.organization_id == expected_organization_id,
            *claim_conditions,
        )
        .values(
            status="retrying",
            attempt_count=EmailAttachmentImport.attempt_count + 1,
            last_attempted_at=now,
            retry_lease_expires_at=now + timedelta(seconds=lease_seconds),
            retry_claim_token=claim_token,
            updated_at=now,
        )
    )
    if claimed.rowcount != 1:
        session.rollback()
        latest = session.scalar(
            select(EmailAttachmentImport).where(
                EmailAttachmentImport.id == import_id,
                EmailAttachmentImport.organization_id == expected_organization_id,
            )
        )
        if latest is None:
            raise MailboxImportError("mailbox_import_not_found")
        if latest.status == "retrying":
            raise MailboxImportError("mailbox_import_retry_in_progress")
        raise MailboxImportError("mailbox_import_not_retryable")
    session.expire_all()
    claimed_record = session.scalar(
        select(EmailAttachmentImport).where(EmailAttachmentImport.id == record.id)
    )
    if claimed_record is None or claimed_record.retry_claim_token != claim_token:
        session.rollback()
        raise MailboxImportError("mailbox_import_not_found")
    if previous_status == "retrying":
        session.execute(
            update(EmailAttachmentImportAttempt)
            .where(
                EmailAttachmentImportAttempt.organization_id == expected_organization_id,
                EmailAttachmentImportAttempt.email_attachment_import_id == claimed_record.id,
                EmailAttachmentImportAttempt.attempt_number == previous_attempt_number,
                EmailAttachmentImportAttempt.status == "retrying",
            )
            .values(
                status="failed",
                error="attachment_retry_interrupted",
                completed_at=now,
            )
        )
    session.add(
        EmailAttachmentImportAttempt(
            organization_id=claimed_record.organization_id,
            email_attachment_import_id=claimed_record.id,
            attempt_number=claimed_record.attempt_count,
            trigger="manual_retry",
            status="retrying",
            error=None,
            resume_id=None,
            started_at=now,
            completed_at=None,
        )
    )
    session.commit()
    return claimed_record


def _renew_retry_claim(
    session: Session,
    *,
    import_id: str,
    claim_token: str,
    lease_seconds: int,
) -> None:
    """Extend an owned attachment claim before a potentially slow operation."""

    now = _utcnow()
    renewed = session.execute(
        update(EmailAttachmentImport)
        .execution_options(synchronize_session=False)
        .where(
            EmailAttachmentImport.id == import_id,
            EmailAttachmentImport.organization_id == organization_context_id(session),
            EmailAttachmentImport.status == "retrying",
            EmailAttachmentImport.retry_claim_token == claim_token,
        )
        .values(
            retry_lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
    )
    if renewed.rowcount != 1:
        session.rollback()
        raise _RetryClaimLost("mailbox_import_retry_superseded")
    session.commit()


def retry_mailbox_attachment(
    session: Session,
    *,
    settings: AppSettings,
    import_id: str,
    retry_lease_seconds: int = _RETRY_LEASE_SECONDS,
    heartbeat: Callable[[], None] | None = None,
) -> MailboxImportResponse:
    """Retry precisely one failed attachment without scanning the mailbox."""

    record = _claim_retry(
        session,
        import_id=import_id,
        lease_seconds=retry_lease_seconds,
    )
    claim_token = record.retry_claim_token
    if not claim_token:
        raise MailboxImportError("mailbox_import_retry_in_progress")
    organization_id = organization_context_id(session)
    mailbox_config_id = record.mailbox_config_id
    client: imaplib.IMAP4_SSL | None = None
    resume: Resume | None = None
    content_claim: _ContentClaim | None = None

    def discard_retry_resume() -> None:
        """Remove an uploaded file if this retry transaction later rolls back."""

        if resume is not None:
            discard_uploaded_pdf(
                settings,
                storage_key=resume.storage_key,
                organization_id=organization_id,
            )

    def complete(
        *,
        status: str,
        error: str | None,
        resume_id: str | None,
        canonical_import_id: str | None = None,
    ) -> MailboxImportResponse:
        return _complete_retry(
            session,
            import_id=record.id,
            claim_token=claim_token,
            status=status,
            error=error,
            resume_id=resume_id,
            canonical_import_id=canonical_import_id,
            content_claim=content_claim,
        )

    def pulse() -> None:
        """Keep both the durable task and exact attachment claim alive."""

        if heartbeat is not None:
            heartbeat()
        _renew_retry_claim(
            session,
            import_id=record.id,
            claim_token=claim_token,
            lease_seconds=retry_lease_seconds,
        )

    try:
        pulse()
        content_claim = _claim_attachment_content(
            session,
            record=record,
            settings=settings,
        )
        if content_claim.outcome == "duplicate":
            result = complete(
                status="duplicate",
                error=None,
                resume_id=content_claim.canonical_resume_id,
                canonical_import_id=content_claim.canonical_import_id,
            )
            discard_retained_failed_attachment(
                session,
                settings=settings,
                attachment_import_id=record.id,
            )
            return result
        if content_claim.outcome == "waiting":
            return complete(
                status="deduplicating",
                error=None,
                resume_id=None,
            )
        if content_claim.outcome == "deleted":
            return complete(
                status="skipped",
                error="attachment_deleted_by_candidate_lifecycle",
                resume_id=None,
            )
        # Persist the ownership lease before any IMAP, document conversion, or
        # candidate write.  A concurrent forwarded copy now becomes an audit
        # row that waits for this one canonical result.
        session.commit()
        pulse()
        config = session.scalar(
            select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id)
        )
        if config is None or config.organization_id != organization_id:
            return complete(
                status="failed",
                error="attachment_source_unavailable",
                resume_id=None,
            )
        if config.archived_at is not None:
            return complete(
                status="failed",
                error="attachment_source_unavailable",
                resume_id=None,
            )
        # A locally retained failure artifact is self-contained and can be
        # retried even if the sender later deletes the source message.  Its
        # hash is checked by the retention service before it is returned.
        filename = record.attachment_filename
        pulse()
        content = read_retained_failed_attachment(
            session,
            settings=settings,
            attachment_import=record,
        )
        if content is None:
            if not config.enabled:
                return complete(
                    status="failed",
                    error="mailbox_not_enabled",
                    resume_id=None,
                )
            if record.source_fingerprint != _mailbox_source_fingerprint(config):
                return complete(
                    status="failed",
                    error="attachment_source_changed",
                    resume_id=None,
                )
            canonical_uid = _canonical_imap_uid(record.message_uid)
            source_uidvalidity = _parse_imap_nz_number(record.source_uidvalidity)
            if canonical_uid is None or source_uidvalidity is None:
                return complete(
                    status="failed",
                    error="attachment_source_changed",
                    resume_id=None,
                )
            message_uid, raw_uid = canonical_uid
            try:
                password = _fernet(settings).decrypt(
                    config.encrypted_password.encode("ascii")
                ).decode("utf-8")
            except (MailboxImportError, InvalidToken, UnicodeDecodeError):
                return complete(
                    status="failed",
                    error="mailbox_credentials_unavailable",
                    resume_id=None,
                )
            _validate_imap_connection_arguments(
                email_address=config.email_address,
                mailbox=config.mailbox,
                password=password,
            )
            pulse()
            client = create_imap_client(
                settings,
                host=config.imap_host,
                port=config.imap_port,
            )
            pulse()
            login_status, _ = _login_imap_client(
                client,
                email_address=config.email_address,
                password=password,
            )
            if login_status != "OK":
                return complete(
                    status="failed",
                    error="mailbox_connection_failed",
                    resume_id=None,
                )
            pulse()
            current_uidvalidity, current_uidnext = _read_mailbox_status(
                client,
                mailbox=config.mailbox,
            )
            if (
                current_uidvalidity != source_uidvalidity
                or message_uid >= current_uidnext
            ):
                return complete(
                    status="failed",
                    error="attachment_source_changed",
                    resume_id=None,
                )
            pulse()
            select_status, _ = _select_mailbox_readonly(
                client,
                mailbox=config.mailbox,
            )
            if select_status != "OK":
                return complete(
                    status="failed",
                    error="mailbox_select_failed",
                    resume_id=None,
                )
            pulse()
            selected_uidvalidity, selected_uidnext = _read_mailbox_status(
                client,
                mailbox=config.mailbox,
            )
            if (
                selected_uidvalidity != source_uidvalidity
                or selected_uidnext < current_uidnext
                or message_uid >= selected_uidnext
            ):
                return complete(
                    status="failed",
                    error="attachment_source_changed",
                    resume_id=None,
                )
            pulse()
            declared_size = _fetch_message_size(
                client,
                raw_uid=raw_uid,
            )
            if declared_size is None:
                return complete(
                    status="failed",
                    error="attachment_message_unavailable",
                    resume_id=None,
                )
            if declared_size > settings.mailbox_max_raw_message_bytes:
                return complete(
                    status="failed",
                    error="mailbox_message_too_large",
                    resume_id=None,
                )
            pulse()
            raw_message = _fetch_message_bytes(
                client,
                raw_uid=raw_uid,
                max_bytes=settings.mailbox_max_raw_message_bytes,
            )
            if raw_message is None:
                return complete(
                    status="failed",
                    error="attachment_message_unavailable",
                    resume_id=None,
                )
            message = _parse_mailbox_message(raw_message, settings=settings)
            attachment = _attachment_with_digest(
                message,
                digest=record.attachment_sha256,
                settings=settings,
            )
            if attachment is None:
                return complete(
                    status="failed",
                    error="attachment_message_unavailable",
                    resume_id=None,
                )
            filename, content = attachment
        try:
            pulse()
            resume = _ingest_attachment(
                session,
                config=config,
                filename=filename,
                content=content,
                settings=settings,
            )
        except _AttachmentIngestionFailure as exc:
            session.rollback()
            _discard_failed_attachment(
                settings=settings,
                organization_id=organization_id,
                failure=exc,
            )
            result = complete(
                status="failed",
                error=exc.code,
                resume_id=None,
            )
            latest_config = session.scalar(
                select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id)
            )
            latest_record = session.scalar(
                select(EmailAttachmentImport).where(EmailAttachmentImport.id == record.id)
            )
            if (
                latest_config is not None
                and latest_record is not None
                and exc.code not in _NON_RETRYABLE_ATTACHMENT_ERRORS
            ):
                _store_replica_safely(
                    session,
                    lambda: store_failed_attachment_copy(
                        session,
                        settings=settings,
                        config=latest_config,
                        attachment_import=latest_record,
                        content=content,
                        suffix=Path(filename).suffix,
                    ),
                )
            return result
        result = complete(
            status="imported",
            error=None,
            resume_id=resume.id,
        )
        latest_config = session.scalar(
            select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id)
        )
        latest_record = session.scalar(
            select(EmailAttachmentImport).where(EmailAttachmentImport.id == record.id)
        )
        if latest_config is not None and latest_record is not None:
            _store_replica_safely(
                session,
                lambda: store_success_attachment_copy(
                    session,
                    settings=settings,
                    config=latest_config,
                    attachment_import=latest_record,
                    content=content,
                    suffix=Path(filename).suffix,
                ),
            )
        discard_retained_failed_attachment(
            session,
            settings=settings,
            attachment_import_id=record.id,
        )
        return result
    except (_RetryClaimLost, _ContentClaimLost):
        session.rollback()
        discard_retry_resume()
        raise MailboxImportError("mailbox_import_retry_superseded")
    except MailboxImapTransportError as exc:
        session.rollback()
        discard_retry_resume()
        return complete(
            status="failed",
            error=str(exc),
            resume_id=None,
        )
    except MailboxImportError as exc:
        session.rollback()
        discard_retry_resume()
        if str(exc) == "mailbox_workspace_mismatch":
            raise
        return complete(
            status="failed",
            error=str(exc)
            if str(exc).startswith("mailbox_")
            else "attachment_import_failed",
            resume_id=None,
        )
    except (imaplib.IMAP4.error, OSError, SQLAlchemyError):
        session.rollback()
        discard_retry_resume()
        return complete(
            status="failed",
            error="mailbox_connection_failed",
            resume_id=None,
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def _sync_response(
    config: MailboxConfig,
    *,
    imported_count: int = 0,
    duplicate_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> MailboxSyncResponse:
    return MailboxSyncResponse(
        configured=True,
        mailbox_id=config.id,
        display_name=config.display_name,
        imported_count=imported_count,
        duplicate_count=duplicate_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        last_synced_at=config.last_synced_at,
        last_sync_error=config.last_sync_error,
    )


def _claim_mailbox_sync(session: Session, *, config: MailboxConfig) -> str:
    """Claim one source without keeping a database transaction over IMAP."""

    now = _utcnow()
    claim_token = uuid4().hex
    organization_id = organization_context_id(session)
    claimed = session.execute(
        update(MailboxConfig)
        .execution_options(synchronize_session=False)
        .where(
            MailboxConfig.id == config.id,
            MailboxConfig.organization_id == organization_id,
            MailboxConfig.enabled.is_(True),
            MailboxConfig.archived_at.is_(None),
            or_(
                MailboxConfig.sync_lease_expires_at.is_(None),
                MailboxConfig.sync_lease_expires_at <= now,
            ),
        )
        .values(
            sync_lease_token=claim_token,
            sync_lease_expires_at=now + timedelta(seconds=_SYNC_LEASE_SECONDS),
            last_sync_started_at=now,
        )
    )
    if claimed.rowcount == 1:
        session.commit()
        return claim_token

    session.rollback()
    current = session.scalar(
        select(MailboxConfig).where(MailboxConfig.id == config.id)
    )
    if current is None:
        raise MailboxImportError("mailbox_config_not_found")
    if current.archived_at is not None:
        raise MailboxImportError("mailbox_config_archived")
    lease_expires_at = _as_utc(current.sync_lease_expires_at)
    if lease_expires_at is not None and lease_expires_at > now:
        raise MailboxImportError("mailbox_sync_in_progress")
    raise MailboxImportError("mailbox_sync_claim_failed")


def _release_mailbox_sync(
    session: Session,
    *,
    config_id: str,
    claim_token: str,
) -> None:
    """Release only the lease owned by this run, never a newer worker's."""

    organization_id = organization_context_id(session)
    session.execute(
        update(MailboxConfig)
        .execution_options(synchronize_session=False)
        .where(
            MailboxConfig.id == config_id,
            MailboxConfig.organization_id == organization_id,
            MailboxConfig.sync_lease_token == claim_token,
        )
        .values(sync_lease_token=None, sync_lease_expires_at=None)
    )
    session.commit()
    session.expire_all()


def _renew_mailbox_sync(
    session: Session,
    *,
    config_id: str,
    claim_token: str,
) -> None:
    """Extend the per-channel IMAP lease while a worker is still healthy."""

    now = _utcnow()
    renewed = session.execute(
        update(MailboxConfig)
        .execution_options(synchronize_session=False)
        .where(
            MailboxConfig.id == config_id,
            MailboxConfig.organization_id == organization_context_id(session),
            MailboxConfig.enabled.is_(True),
            MailboxConfig.archived_at.is_(None),
            MailboxConfig.sync_lease_token == claim_token,
        )
        .values(sync_lease_expires_at=now + timedelta(seconds=_SYNC_LEASE_SECONDS))
    )
    if renewed.rowcount != 1:
        session.rollback()
        raise MailboxImportError("mailbox_sync_claim_failed")
    session.commit()
    session.expire_all()


def _sync_config_for_run(
    session: Session,
    *,
    config_id: str | None,
) -> MailboxConfig | None:
    if config_id is None:
        return _legacy_single_config(session)
    config = _mailbox_config_or_error(
        session,
        config_id=config_id,
        include_archived=True,
    )
    if config.archived_at is not None:
        raise MailboxImportError("mailbox_config_archived")
    return config


def sync_mailbox(
    session: Session,
    *,
    settings: AppSettings,
    config_id: str | None = None,
    expected_source_fingerprint: str | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> MailboxSyncResponse:
    config = _sync_config_for_run(session, config_id=config_id)
    if config is None:
        return MailboxSyncResponse(configured=False)
    if (
        expected_source_fingerprint is not None
        and _mailbox_source_fingerprint(config) != expected_source_fingerprint
    ):
        raise MailboxImportError("mailbox_task_source_changed")
    organization_id = config.organization_id
    if not organization_id:
        # A configuration without a workspace is never allowed to read mail
        # or create a candidate. Keep the failure generic and non-sensitive.
        raise MailboxImportError("mailbox_workspace_missing")
    if not config.enabled:
        return _sync_response(config)

    mailbox_config_id = config.id
    claim_token = _claim_mailbox_sync(session, config=config)
    config = _mailbox_config_or_error(
        session,
        config_id=mailbox_config_id,
        include_archived=True,
    )
    if config.archived_at is not None:
        _release_mailbox_sync(
            session,
            config_id=mailbox_config_id,
            claim_token=claim_token,
        )
        raise MailboxImportError("mailbox_config_archived")
    if not config.enabled:
        _release_mailbox_sync(
            session,
            config_id=mailbox_config_id,
            claim_token=claim_token,
        )
        return _sync_response(config)

    if (
        expected_source_fingerprint is not None
        and _mailbox_source_fingerprint(config) != expected_source_fingerprint
    ):
        _release_mailbox_sync(
            session,
            config_id=mailbox_config_id,
            claim_token=claim_token,
        )
        raise MailboxImportError("mailbox_task_source_changed")

    def pulse() -> None:
        """Keep the durable task and channel source lease current."""

        if heartbeat is not None:
            heartbeat()
        _renew_mailbox_sync(
            session,
            config_id=mailbox_config_id,
            claim_token=claim_token,
        )

    def stop_for_source_change(error_code: str) -> None:
        """Disable a source whose immutable IMAP identity became unsafe."""

        config.enabled = False
        config.last_sync_error = error_code
        session.commit()
        raise MailboxImportError(error_code)

    imported = duplicates = skipped = failed = 0
    client: imaplib.IMAP4_SSL | None = None
    try:
        pulse()
        _recover_expired_content_claims(session, organization_id=organization_id)
        pulse()
        password = _decrypt_password(settings, config.encrypted_password)
        _validate_imap_connection_arguments(
            email_address=config.email_address,
            mailbox=config.mailbox,
            password=password,
        )
        pulse()
        client = create_imap_client(
            settings,
            host=config.imap_host,
            port=config.imap_port,
        )
        pulse()
        login_status, _ = _login_imap_client(
            client,
            email_address=config.email_address,
            password=password,
        )
        if login_status != "OK":
            raise MailboxImportError("mailbox_connection_failed")
        pulse()
        imap_uidvalidity, current_uidnext = _read_mailbox_status(
            client,
            mailbox=config.mailbox,
        )
        # Older installations have no watermark until their first sync after
        # this feature ships. Establish it and deliberately leave all existing
        # messages untouched.
        if config.import_start_uid is None or config.imap_uidvalidity is None:
            config.import_start_uid = current_uidnext
            config.imap_uidvalidity = imap_uidvalidity
            config.import_started_at = _utcnow()
            config.last_synced_at = _utcnow()
            config.last_sync_error = None
            session.commit()
            return _sync_response(config)
        # A UID has meaning only inside one UIDVALIDITY epoch. A changed epoch
        # is a different source identity, so do not silently reset the
        # watermark and continue. The owner must archive this channel and bind
        # a new one, which keeps historical retry provenance trustworthy.
        import_start_uid = _parse_imap_nz_number(config.import_start_uid)
        configured_uidvalidity = _parse_imap_nz_number(config.imap_uidvalidity)
        if import_start_uid is None or configured_uidvalidity is None:
            stop_for_source_change("mailbox_source_watermark_invalid")
        if configured_uidvalidity != imap_uidvalidity:
            stop_for_source_change("mailbox_source_epoch_changed")
        if current_uidnext < import_start_uid:
            stop_for_source_change("mailbox_source_watermark_invalid")
        pulse()
        status, _ = _select_mailbox_readonly(
            client,
            mailbox=config.mailbox,
        )
        if status != "OK":
            raise MailboxImportError("mailbox_select_failed")
        pulse()
        selected_uidvalidity, selected_uidnext = _read_mailbox_status(
            client,
            mailbox=config.mailbox,
        )
        if selected_uidvalidity != configured_uidvalidity:
            stop_for_source_change("mailbox_source_epoch_changed")
        if selected_uidnext < current_uidnext or selected_uidnext < import_start_uid:
            stop_for_source_change("mailbox_source_watermark_invalid")
        pulse()
        search_uids: list[bytes]
        if selected_uidnext == import_start_uid:
            # ``UID N:*`` includes the last existing message when N is larger
            # than every assigned UID. Avoid that reversed-range behavior by
            # issuing no SEARCH until the snapshot has a real post-bind UID.
            search_uids = []
        else:
            maximum_uid = selected_uidnext - 1
            status, data = client.uid(
                "search",
                None,
                f"UID {import_start_uid}:{maximum_uid}",
            )
            if status != "OK":
                raise MailboxImportError("mailbox_search_failed")
            search_uids = _parse_search_uids(
                data,
                settings=settings,
                minimum_uid=import_start_uid,
                maximum_uid=maximum_uid,
            )
        selected_uids: list[bytes] = []
        seen_uids: set[str] = set()
        candidate_batch: list[bytes] = []

        def choose_unknown_uids() -> None:
            if not candidate_batch:
                return
            candidate_values = {raw.decode("ascii") for raw in candidate_batch}
            known_uids = _known_message_uids(
                session,
                config_id=config.id,
                organization_id=organization_id,
                message_uids=candidate_values,
            )
            for candidate in candidate_batch:
                candidate_uid = candidate.decode("ascii")
                if candidate_uid and candidate_uid not in known_uids:
                    selected_uids.append(candidate)
                    if len(selected_uids) >= settings.mailbox_sync_attachment_limit:
                        break
            candidate_batch.clear()

        # Work newest-first so freshly received resumes arrive immediately.
        # The server has already limited this search to UIDs at or after the
        # binding watermark. Querying historical handling state in small
        # batches keeps a long-lived source from loading every prior UID.
        for raw_uid in reversed(search_uids):
            uid = raw_uid.decode("ascii")
            if not uid or uid in seen_uids:
                continue
            seen_uids.add(uid)
            candidate_batch.append(raw_uid)
            if len(candidate_batch) >= 100:
                choose_unknown_uids()
            if len(selected_uids) >= settings.mailbox_sync_attachment_limit:
                break
        if len(selected_uids) < settings.mailbox_sync_attachment_limit:
            choose_unknown_uids()
        uids = list(reversed(selected_uids))
        for raw_uid in uids:
            uid = raw_uid.decode("ascii")
            pulse()
            declared_size = _fetch_message_size(client, raw_uid=raw_uid)
            if declared_size is None:
                failed += 1
                continue
            if declared_size > settings.mailbox_max_raw_message_bytes:
                _record(
                    session,
                    config=config,
                    uid=uid,
                    message_id=None,
                    filename="[邮件大小超限]",
                    attachment_sha256=_message_resource_digest(
                        uid=uid,
                        error_code="mailbox_message_too_large",
                    ),
                    status="skipped",
                    error="mailbox_message_too_large",
                    resume_id=None,
                    received_at=None,
                    source_uidvalidity=imap_uidvalidity,
                )
                session.commit()
                skipped += 1
                continue
            pulse()
            try:
                raw_message = _fetch_message_bytes(
                    client,
                    raw_uid=raw_uid,
                    max_bytes=settings.mailbox_max_raw_message_bytes,
                )
                if raw_message is None:
                    failed += 1
                    continue
                message = _parse_mailbox_message(raw_message, settings=settings)
                attachments = _attachments(message, settings=settings)
            except MailboxImportError as exc:
                error_code = str(exc)
                if error_code not in _MAILBOX_RESOURCE_LIMIT_ERRORS:
                    raise
                _record(
                    session,
                    config=config,
                    uid=uid,
                    message_id=None,
                    filename="[邮件资源限制]",
                    attachment_sha256=_message_resource_digest(
                        uid=uid,
                        error_code=error_code,
                    ),
                    status="skipped",
                    error=error_code,
                    resume_id=None,
                    received_at=None,
                    source_uidvalidity=imap_uidvalidity,
                )
                session.commit()
                skipped += 1
                if isinstance(exc.__cause__, MailboxImapResponseLimitError):
                    # The pinned transport closes a peer that ignores our
                    # bounded BODY.PEEK range. The UID is now durably marked
                    # as skipped; finish this batch cleanly and reconnect on
                    # the next scheduled task for any remaining messages.
                    client = None
                    break
                continue
            message_id = str(message.get("Message-ID") or "").strip() or None
            received_at = _received_at(message)
            # Keep only a bounded plain-text body cache, and only for mail
            # carrying a supported resume.  The IMAP RFC822 payload itself is
            # never persisted.
            if any(attachment.supported for attachment in attachments):
                body_content = _message_body_bytes(message, settings=settings)
                if body_content:
                    _store_replica_safely(
                        session,
                        lambda: store_mailbox_body_copy(
                            session,
                            settings=settings,
                            config=config,
                            message_uid=uid,
                            content=body_content,
                        ),
                    )
            if not attachments:
                digest = hashlib.sha256(b"").hexdigest()
                _record(
                    session,
                    config=config,
                    uid=uid,
                    message_id=message_id,
                    filename="[no supported attachment]",
                    attachment_sha256=digest,
                    status="skipped",
                    error="no_supported_attachment",
                    resume_id=None,
                    received_at=received_at,
                    source_uidvalidity=imap_uidvalidity,
                )
                session.commit()
                skipped += 1
                continue
            for attachment in attachments:
                filename = attachment.filename
                if attachment.content is None:
                    digest = _unsupported_attachment_digest(
                        uid=uid,
                        filename=filename,
                        ordinal=attachment.ordinal,
                    )
                    if _already_imported(
                        session,
                        config_id=config.id,
                        organization_id=organization_id,
                        uid=uid,
                        digest=digest,
                    ):
                        duplicates += 1
                        continue
                    _record(
                        session,
                        config=config,
                        uid=uid,
                        message_id=message_id,
                        filename=filename,
                        attachment_sha256=digest,
                        status="skipped",
                        error="unsupported_document_type",
                        resume_id=None,
                        received_at=received_at,
                        source_uidvalidity=imap_uidvalidity,
                    )
                    session.commit()
                    skipped += 1
                    continue
                content = attachment.content
                digest = hashlib.sha256(content).hexdigest()
                if _already_imported(
                    session,
                    config_id=config.id,
                    organization_id=organization_id,
                    uid=uid,
                    digest=digest,
                ):
                    duplicates += 1
                    continue
                record, content_claim, terminal = _begin_automatic_content_import(
                    session,
                    config=config,
                    uid=uid,
                    message_id=message_id,
                    filename=filename,
                    attachment_sha256=digest,
                    received_at=received_at,
                    source_uidvalidity=imap_uidvalidity,
                    settings=settings,
                )
                if terminal is not None:
                    # A waiter has not become a duplicate yet: its canonical
                    # owner may still fail, in which case both audit rows are
                    # retryable failures.  Count only a terminal duplicate in
                    # the synchronous result instead of reporting a success
                    # that the owner can subsequently reverse.
                    if terminal.status == "duplicate":
                        duplicates += 1
                    elif terminal.status == "failed":
                        failed += 1
                    else:
                        skipped += 1
                    continue

                assert content_claim is not None
                resume: Resume | None = None
                try:
                    pulse()
                    resume = _ingest_attachment(
                        session,
                        config=config,
                        filename=filename,
                        content=content,
                        settings=settings,
                    )
                    _complete_processing_import(
                        session,
                        record=record,
                        claim=content_claim,
                        status="imported",
                        error=None,
                        resume_id=resume.id,
                    )
                    source_config = _mailbox_config_or_error(
                        session,
                        config_id=mailbox_config_id,
                        include_archived=True,
                    )
                    stored_record = session.scalar(
                        select(EmailAttachmentImport).where(
                            EmailAttachmentImport.id == record.id,
                            EmailAttachmentImport.organization_id == organization_id,
                        )
                    )
                    if stored_record is None:
                        raise MailboxImportError("mailbox_config_not_found")
                    if source_config.archived_at is None:
                        _store_replica_safely(
                            session,
                            lambda: store_success_attachment_copy(
                                session,
                                settings=settings,
                                config=source_config,
                                attachment_import=stored_record,
                                content=content,
                                suffix=Path(filename).suffix,
                            ),
                        )
                    imported += 1
                except _AttachmentIngestionFailure as exc:
                    session.rollback()
                    _discard_failed_attachment(
                        settings=settings,
                        organization_id=organization_id,
                        failure=exc,
                    )
                    source_config = _mailbox_config_or_error(
                        session,
                        config_id=mailbox_config_id,
                        include_archived=True,
                    )
                    try:
                        _complete_processing_import(
                            session,
                            record=record,
                            claim=content_claim,
                            status="failed",
                            error=exc.code,
                            resume_id=None,
                        )
                    except _ContentClaimLost:
                        session.rollback()
                        duplicates += 1
                        continue
                    failed_record = session.scalar(
                        select(EmailAttachmentImport).where(
                            EmailAttachmentImport.id == record.id,
                            EmailAttachmentImport.organization_id == organization_id,
                        )
                    )
                    if (
                        source_config.archived_at is None
                        and exc.code not in _NON_RETRYABLE_ATTACHMENT_ERRORS
                        and failed_record is not None
                    ):
                        _store_replica_safely(
                            session,
                            lambda: store_failed_attachment_copy(
                                session,
                                settings=settings,
                                config=source_config,
                                attachment_import=failed_record,
                                content=content,
                                suffix=Path(filename).suffix,
                            ),
                        )
                    if source_config.archived_at is not None:
                        raise MailboxImportError("mailbox_config_archived")
                    failed += 1
                except _ContentClaimLost:
                    session.rollback()
                    if resume is not None:
                        discard_uploaded_pdf(
                            settings,
                            storage_key=resume.storage_key,
                            organization_id=organization_id,
                        )
                    duplicates += 1
        config = _mailbox_config_or_error(
            session,
            config_id=mailbox_config_id,
            include_archived=True,
        )
        if config.archived_at is not None:
            raise MailboxImportError("mailbox_config_archived")
        config.last_synced_at = _utcnow()
        config.last_sync_error = None
        session.commit()
        return _sync_response(
            config,
            imported_count=imported,
            duplicate_count=duplicates,
            skipped_count=skipped,
            failed_count=failed,
        )
    except (
        imaplib.IMAP4.error,
        OSError,
        MailboxImapTransportError,
        MailboxImportError,
        SQLAlchemyError,
    ) as exc:
        session.rollback()
        error_code = (
            str(exc)
            if isinstance(exc, (MailboxImportError, MailboxImapTransportError))
            else "mailbox_sync_failed"
            if isinstance(exc, SQLAlchemyError)
            else "mailbox_connection_failed"
        )
        config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id))
        if config is not None and config.organization_id == organization_id:
            config.last_sync_error = error_code
            session.commit()
        if isinstance(exc, MailboxImapTransportError):
            raise MailboxImportError(error_code) from exc
        if isinstance(exc, MailboxImportError):
            raise
        if isinstance(exc, SQLAlchemyError):
            raise MailboxImportError("mailbox_sync_failed") from exc
        raise MailboxImportError("mailbox_connection_failed") from exc
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        # A normal early return still reaches this block. The conditional
        # release cannot erase a newer worker's lease after expiry.
        try:
            _release_mailbox_sync(
                session,
                config_id=mailbox_config_id,
                claim_token=claim_token,
            )
        except SQLAlchemyError:
            session.rollback()


def sync_due_mailboxes(*, database, settings: AppSettings) -> bool:
    """Compatibility wrapper that only queues a due sync.

    The import is intentionally local to avoid a module-level cycle: the
    durable job service invokes ``sync_mailbox`` while this legacy name is
    still imported by a few integrations.
    """

    from app.services.mailbox_background_job_service import enqueue_due_mailbox_sync_jobs

    return enqueue_due_mailbox_sync_jobs(database=database, settings=settings)
