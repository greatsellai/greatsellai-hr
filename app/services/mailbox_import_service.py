from __future__ import annotations

import base64
import hashlib
import imaplib
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.models import (
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    MailboxConfig,
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
    MailboxSyncAllResponse,
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
from app.services.resume_service import (
    UploadValidationError,
    create_candidate,
    discard_uploaded_pdf,
    save_pdf_resume,
)


class MailboxImportError(RuntimeError):
    pass


class _RetryClaimLost(MailboxImportError):
    """A newer request owns this attachment retry now."""


_RETRY_LEASE_SECONDS = 180
_SYNC_LEASE_SECONDS = 600
_NON_RETRYABLE_ATTACHMENT_ERRORS = frozenset(
    {
        "attachment_validation_failed",
        "attachment_message_unavailable",
        "attachment_source_changed",
        "attachment_source_unavailable",
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
    r"\b(UIDVALIDITY|UIDNEXT)\s+(\d+)\b",
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
            text = item.decode("ascii", errors="ignore")
        else:
            text = str(item)
        for name, value in _MAILBOX_STATUS_VALUE_PATTERN.findall(text):
            try:
                values[name.upper()] = int(value)
            except ValueError:
                continue
    uidvalidity = values.get("UIDVALIDITY")
    uidnext = values.get("UIDNEXT")
    if uidvalidity is None or uidnext is None or uidvalidity <= 0 or uidnext <= 0:
        raise MailboxImportError("mailbox_status_failed")
    return uidvalidity, uidnext


def _read_mailbox_status(
    client: imaplib.IMAP4_SSL,
    *,
    mailbox: str,
) -> tuple[int, int]:
    """Read the mailbox watermark without selecting or scanning messages."""

    try:
        status, data = client.status(mailbox, "(UIDVALIDITY UIDNEXT)")
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailboxImportError("mailbox_status_failed") from exc
    if status != "OK":
        raise MailboxImportError("mailbox_status_failed")
    return _parse_mailbox_status(data)


def _read_initial_mailbox_watermark(
    *,
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
        client = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=30)
        login_status, _ = client.login(email_address, password)
        if login_status != "OK":
            raise MailboxImportError("mailbox_connection_failed")
        return _read_mailbox_status(client, mailbox=mailbox)
    except MailboxImportError:
        raise
    except (imaplib.IMAP4.error, OSError) as exc:
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


def _config_response(config: MailboxConfig | None) -> MailboxConfigResponse:
    if config is None:
        return MailboxConfigResponse(configured=False)
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
    normalized_host = imap_host.strip()
    normalized_email = email_address.strip()
    normalized_mailbox = mailbox.strip()
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
    statement = select(EmailAttachmentImport).options(
        selectinload(EmailAttachmentImport.mailbox_config)
    )
    count_statement = select(func.count()).select_from(EmailAttachmentImport)
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
    source_uidvalidity: int | None = None,
    trigger: str = "automatic",
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
            completed_at=now,
        )
    )
    session.flush()
    return record


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
) -> set[str]:
    """Return message UIDs already handled by an earlier incremental run."""

    return set(
        session.scalars(
            select(EmailAttachmentImport.message_uid).where(
                EmailAttachmentImport.mailbox_config_id == config_id,
                EmailAttachmentImport.organization_id == organization_id,
            )
        ).all()
    )


def _attachments(message: Message) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename or disposition not in {"attachment", "inline"}:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append((_safe_filename(filename), payload))
    return found


def _message_body_bytes(message: Message) -> bytes:
    """Return bounded plain-text mail content without retaining MIME headers.

    The body cache is for short retention/audit only.  It is deliberately
    capped and does not include recipients, headers, or binary inline parts.
    """

    parts: list[bytes] = []
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if payload:
            parts.append(payload)
    return b"\n\n".join(parts)[: 256 * 1024]


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
) -> tuple[str, bytes] | None:
    """Return only the exact previously-recorded attachment content."""

    for filename, content in _attachments(message):
        if hashlib.sha256(content).hexdigest() == digest:
            return filename, content
    return None


def _complete_retry(
    session: Session,
    *,
    import_id: str,
    claim_token: str,
    status: str,
    error: str | None,
    resume_id: str | None,
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
            completed_at=now,
        )
    )
    if completed_attempt.rowcount != 1:
        session.rollback()
        raise _RetryClaimLost("mailbox_import_retry_superseded")
    session.commit()
    return _import_response(record)


def _claim_retry(session: Session, *, import_id: str) -> EmailAttachmentImport:
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
            retry_lease_expires_at=now + timedelta(seconds=_RETRY_LEASE_SECONDS),
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


def retry_mailbox_attachment(
    session: Session,
    *,
    settings: AppSettings,
    import_id: str,
) -> MailboxImportResponse:
    """Retry precisely one failed attachment without scanning the mailbox."""

    record = _claim_retry(session, import_id=import_id)
    claim_token = record.retry_claim_token
    if not claim_token:
        raise MailboxImportError("mailbox_import_retry_in_progress")
    organization_id = organization_context_id(session)
    mailbox_config_id = record.mailbox_config_id
    client: imaplib.IMAP4_SSL | None = None
    resume: Resume | None = None

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
    ) -> MailboxImportResponse:
        return _complete_retry(
            session,
            import_id=record.id,
            claim_token=claim_token,
            status=status,
            error=error,
            resume_id=resume_id,
        )

    try:
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
            client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=30)
            login_status, _ = client.login(config.email_address, password)
            if login_status != "OK":
                return complete(
                    status="failed",
                    error="mailbox_connection_failed",
                    resume_id=None,
                )
            current_uidvalidity, _ = _read_mailbox_status(client, mailbox=config.mailbox)
            if current_uidvalidity != record.source_uidvalidity:
                return complete(
                    status="failed",
                    error="attachment_source_changed",
                    resume_id=None,
                )
            select_status, _ = client.select(config.mailbox, readonly=True)
            if select_status != "OK":
                return complete(
                    status="failed",
                    error="mailbox_select_failed",
                    resume_id=None,
                )
            fetch_status, fetched = client.uid(
                "fetch", record.message_uid.encode("ascii"), "(RFC822)"
            )
            if fetch_status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                return complete(
                    status="failed",
                    error="attachment_message_unavailable",
                    resume_id=None,
                )
            message = BytesParser(policy=policy.default).parsebytes(fetched[0][1])
            attachment = _attachment_with_digest(
                message,
                digest=record.attachment_sha256,
            )
            if attachment is None:
                return complete(
                    status="failed",
                    error="attachment_message_unavailable",
                    resume_id=None,
                )
            filename, content = attachment
        try:
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
            return complete(
                status="failed",
                error=exc.code,
                resume_id=None,
            )
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
    except _RetryClaimLost:
        session.rollback()
        discard_retry_resume()
        raise MailboxImportError("mailbox_import_retry_superseded")
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
) -> MailboxSyncResponse:
    config = _sync_config_for_run(session, config_id=config_id)
    if config is None:
        return MailboxSyncResponse(configured=False)
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

    imported = duplicates = skipped = failed = 0
    client: imaplib.IMAP4_SSL | None = None
    try:
        password = _decrypt_password(settings, config.encrypted_password)
        client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=30)
        login_status, _ = client.login(config.email_address, password)
        if login_status != "OK":
            raise MailboxImportError("mailbox_connection_failed")
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
        if config.imap_uidvalidity != imap_uidvalidity:
            config.enabled = False
            config.last_sync_error = "mailbox_source_epoch_changed"
            session.commit()
            raise MailboxImportError("mailbox_source_epoch_changed")
        status, _ = client.select(config.mailbox, readonly=True)
        if status != "OK":
            raise MailboxImportError("mailbox_select_failed")
        status, data = client.uid("search", None, f"UID {config.import_start_uid}:*")
        if status != "OK":
            raise MailboxImportError("mailbox_search_failed")
        known_uids = _known_message_uids(
            session,
            config_id=config.id,
            organization_id=organization_id,
        )
        selected_uids: list[bytes] = []
        # Work newest-first so freshly received resumes arrive immediately.
        # The server has already limited this search to UIDs at or after the
        # binding watermark; known UIDs avoid re-fetching work from an earlier
        # batch when a burst exceeds the per-run limit.
        for raw_uid in reversed((data[0] or b"").split()):
            uid = raw_uid.decode("ascii", errors="ignore")
            if not uid or uid in known_uids:
                continue
            selected_uids.append(raw_uid)
            if len(selected_uids) >= settings.mailbox_sync_attachment_limit:
                break
        uids = list(reversed(selected_uids))
        for raw_uid in uids:
            uid = raw_uid.decode("ascii", errors="ignore")
            status, fetched = client.uid("fetch", raw_uid, "(RFC822)")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                failed += 1
                continue
            message = BytesParser(policy=policy.default).parsebytes(fetched[0][1])
            message_id = str(message.get("Message-ID") or "").strip() or None
            received_at = _received_at(message)
            attachments = _attachments(message)
            # Keep only a bounded plain-text body cache, and only for mail
            # carrying a supported resume.  The IMAP RFC822 payload itself is
            # never persisted.
            if attachments and any(
                filename.lower().endswith(extension)
                for filename, _ in attachments
                for extension in SUPPORTED_DOCUMENT_EXTENSIONS
            ):
                body_content = _message_body_bytes(message)
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
                known_uids.add(uid)
                continue
            for filename, content in attachments:
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
                if not any(filename.lower().endswith(ext) for ext in SUPPORTED_DOCUMENT_EXTENSIONS):
                    record = _record(
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
                try:
                    resume = _ingest_attachment(
                        session,
                        config=config,
                        filename=filename,
                        content=content,
                        settings=settings,
                    )
                    record = _record(
                        session,
                        config=config,
                        uid=uid,
                        message_id=message_id,
                        filename=filename,
                        attachment_sha256=digest,
                        status="imported",
                        error=None,
                        resume_id=resume.id,
                        received_at=received_at,
                        source_uidvalidity=imap_uidvalidity,
                    )
                    session.commit()
                    _store_replica_safely(
                        session,
                        lambda: store_success_attachment_copy(
                            session,
                            settings=settings,
                            config=config,
                            attachment_import=record,
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
                    config = _mailbox_config_or_error(
                        session,
                        config_id=mailbox_config_id,
                        include_archived=True,
                    )
                    if config.archived_at is not None:
                        raise MailboxImportError("mailbox_config_archived")
                    record = _record(
                        session,
                        config=config,
                        uid=uid,
                        message_id=message_id,
                        filename=filename,
                        attachment_sha256=digest,
                        status="failed",
                        error=exc.code,
                        resume_id=None,
                        received_at=received_at,
                        source_uidvalidity=imap_uidvalidity,
                    )
                    session.commit()
                    if exc.code not in _NON_RETRYABLE_ATTACHMENT_ERRORS:
                        _store_replica_safely(
                            session,
                            lambda: store_failed_attachment_copy(
                                session,
                                settings=settings,
                                config=config,
                                attachment_import=record,
                                content=content,
                                suffix=Path(filename).suffix,
                            ),
                        )
                    failed += 1
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
    except (imaplib.IMAP4.error, OSError, MailboxImportError, SQLAlchemyError) as exc:
        session.rollback()
        error_code = (
            str(exc)
            if isinstance(exc, MailboxImportError)
            else "mailbox_sync_failed"
            if isinstance(exc, SQLAlchemyError)
            else "mailbox_connection_failed"
        )
        config = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id))
        if config is not None and config.organization_id == organization_id:
            config.last_sync_error = error_code
            session.commit()
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


def sync_all_mailboxes(
    session: Session,
    *,
    settings: AppSettings,
) -> MailboxSyncAllResponse:
    """Synchronize each active source independently and retain per-source errors."""

    config_ids = list(
        session.scalars(
            select(MailboxConfig.id)
            .where(MailboxConfig.archived_at.is_(None), MailboxConfig.enabled.is_(True))
            .order_by(MailboxConfig.created_at, MailboxConfig.id)
        ).all()
    )
    results: list[MailboxSyncResponse] = []
    for config_id in config_ids:
        try:
            results.append(sync_mailbox(session, settings=settings, config_id=config_id))
        except MailboxImportError as exc:
            session.rollback()
            config = session.scalar(
                select(MailboxConfig).where(MailboxConfig.id == config_id)
            )
            if config is not None:
                if config.last_sync_error != str(exc):
                    config.last_sync_error = str(exc)
                    session.commit()
                results.append(_sync_response(config))
    return MailboxSyncAllResponse(
        items=results,
        imported_count=sum(item.imported_count for item in results),
        duplicate_count=sum(item.duplicate_count for item in results),
        skipped_count=sum(item.skipped_count for item in results),
        failed_count=sum(item.failed_count for item in results),
    )


def sync_due_mailboxes(*, database, settings: AppSettings) -> bool:
    now = _utcnow()
    cutoff = now - timedelta(seconds=settings.mailbox_sync_interval_seconds)
    with database.session_factory() as session:
        claimed = session.execute(
            select(MailboxConfig.id, MailboxConfig.organization_id)
            .where(
                MailboxConfig.enabled.is_(True),
                MailboxConfig.archived_at.is_(None),
                or_(
                    MailboxConfig.sync_lease_expires_at.is_(None),
                    MailboxConfig.sync_lease_expires_at <= now,
                ),
            )
            .where(
                (MailboxConfig.import_start_uid.is_(None))
                | (MailboxConfig.imap_uidvalidity.is_(None))
                | (MailboxConfig.last_sync_started_at <= cutoff)
                | and_(
                    MailboxConfig.last_sync_started_at.is_(None),
                    or_(
                        MailboxConfig.last_synced_at.is_(None),
                        MailboxConfig.last_synced_at <= cutoff,
                    ),
                )
            )
            .order_by(
                func.coalesce(
                    MailboxConfig.last_sync_started_at,
                    MailboxConfig.last_synced_at,
                ),
                MailboxConfig.id,
            )
            # One scheduler serves all tenants, so discovery is global.  The
            # actual IMAP connection and every following write are scoped
            # immediately below before the config is re-read.
            .execution_options(skip_organization_scope=True)
        ).first()
        if claimed is None:
            return False
        config_id, organization_id = claimed
        if not organization_id:
            session.execute(
                MailboxConfig.__table__.update()
                .where(MailboxConfig.id == config_id)
                .values(
                    enabled=False,
                    last_sync_error="mailbox_workspace_missing",
                    sync_lease_token=None,
                    sync_lease_expires_at=None,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return True
        try:
            with _organization_session(session, organization_id):
                sync_mailbox(session, settings=settings, config_id=config_id)
        except MailboxImportError:
            return True
        return True
