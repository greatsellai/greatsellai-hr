from __future__ import annotations

import base64
import hashlib
import imaplib
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Iterator
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    MailboxConfig,
    Resume,
)
from app.schemas import (
    MailboxConfigResponse,
    MailboxConfigUpdate,
    MailboxImportHistoryResponse,
    MailboxImportResponse,
    MailboxSyncResponse,
)
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)
from app.services.ai_extraction_job_service import enqueue_uploaded_resume_ai_extraction
from app.services.document_text_extraction import SUPPORTED_DOCUMENT_EXTENSIONS
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
        imap_host=config.imap_host,
        imap_port=config.imap_port,
        email_address=config.email_address,
        mailbox=config.mailbox,
        enabled=config.enabled,
        password_configured=bool(config.encrypted_password),
        import_started_at=config.import_started_at,
        last_synced_at=config.last_synced_at,
        last_sync_error=config.last_sync_error,
    )


def get_mailbox_config(session: Session) -> MailboxConfigResponse:
    config = session.scalar(select(MailboxConfig).order_by(desc(MailboxConfig.created_at)))
    return _config_response(config)


def save_mailbox_config(
    session: Session,
    *,
    settings: AppSettings,
    payload: MailboxConfigUpdate,
) -> MailboxConfigResponse:
    config = session.scalar(select(MailboxConfig).order_by(desc(MailboxConfig.created_at)))
    if config is None and not payload.password:
        raise MailboxImportError("mailbox_password_required")
    imap_host = payload.imap_host.strip()
    email_address = payload.email_address.strip()
    mailbox = payload.mailbox.strip()
    source_changed = config is None or not _same_mailbox_source(
        config,
        imap_host=imap_host,
        imap_port=payload.imap_port,
        email_address=email_address,
        mailbox=mailbox,
    )
    needs_watermark = source_changed or (
        config is not None
        and (config.import_start_uid is None or config.imap_uidvalidity is None)
    )
    encrypted_password = config.encrypted_password if config is not None else ""
    password = payload.password
    if payload.password:
        encrypted_password = _fernet(settings).encrypt(payload.password.encode("utf-8")).decode("ascii")
    if needs_watermark:
        if password is None:
            try:
                password = _fernet(settings).decrypt(encrypted_password.encode("ascii")).decode("utf-8")
            except (MailboxImportError, InvalidToken, UnicodeDecodeError) as exc:
                raise MailboxImportError("mailbox_credentials_unavailable") from exc
        imap_uidvalidity, import_start_uid = _read_initial_mailbox_watermark(
            imap_host=imap_host,
            imap_port=payload.imap_port,
            email_address=email_address,
            mailbox=mailbox,
            password=password,
        )
        import_started_at = _utcnow()
    if config is None:
        config = MailboxConfig(
            imap_host=imap_host,
            imap_port=payload.imap_port,
            email_address=email_address,
            mailbox=mailbox,
            encrypted_password=encrypted_password,
            enabled=payload.enabled,
            import_start_uid=import_start_uid,
            imap_uidvalidity=imap_uidvalidity,
            import_started_at=import_started_at,
        )
        session.add(config)
    else:
        config.imap_host = imap_host
        config.imap_port = payload.imap_port
        config.email_address = email_address
        config.mailbox = mailbox
        config.encrypted_password = encrypted_password
        config.enabled = payload.enabled
        if needs_watermark:
            config.import_start_uid = import_start_uid
            config.imap_uidvalidity = imap_uidvalidity
            config.import_started_at = import_started_at
            # The worker should check a newly bound mailbox immediately.  The
            # stored UIDNEXT keeps that check from importing its history.
            config.last_synced_at = None
        config.last_sync_error = None
    session.commit()
    return _config_response(config)


def list_mailbox_imports(session: Session, *, limit: int = 40) -> MailboxImportHistoryResponse:
    records = session.scalars(
        select(EmailAttachmentImport)
        .order_by(
            desc(EmailAttachmentImport.last_attempted_at),
            desc(EmailAttachmentImport.created_at),
        )
        .limit(limit)
    ).all()
    total = session.scalar(select(func.count()).select_from(EmailAttachmentImport))
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


def _can_retry(item: EmailAttachmentImport) -> bool:
    if not _has_retryable_source(item):
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
        if not _has_retryable_source(record):
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
            return complete(
                status="failed",
                error=exc.code,
                resume_id=None,
            )
        return complete(
            status="imported",
            error=None,
            resume_id=resume.id,
        )
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


def sync_mailbox(
    session: Session,
    *,
    settings: AppSettings,
    config_id: str | None = None,
) -> MailboxSyncResponse:
    config_query = select(MailboxConfig).order_by(desc(MailboxConfig.created_at))
    if config_id:
        config_query = select(MailboxConfig).where(MailboxConfig.id == config_id)
    config = session.scalar(config_query)
    if config is None:
        return MailboxSyncResponse(configured=False)
    organization_id = config.organization_id
    if not organization_id:
        # A configuration without a workspace is never allowed to read mail
        # or create a candidate.  Keep the failure generic and non-sensitive.
        raise MailboxImportError("mailbox_workspace_missing")
    if not config.enabled:
        return MailboxSyncResponse(
            configured=True,
            last_synced_at=config.last_synced_at,
            last_sync_error=config.last_sync_error,
        )
    mailbox_config_id = config.id
    try:
        password = _fernet(settings).decrypt(config.encrypted_password.encode("ascii")).decode("utf-8")
    except (MailboxImportError, InvalidToken, UnicodeDecodeError) as exc:
        config.last_sync_error = "mailbox_credentials_unavailable"
        session.commit()
        raise MailboxImportError("mailbox_credentials_unavailable") from exc

    imported = duplicates = skipped = failed = 0
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=30)
        login_status, _ = client.login(config.email_address, password)
        if login_status != "OK":
            raise MailboxImportError("mailbox_connection_failed")
        imap_uidvalidity, current_uidnext = _read_mailbox_status(
            client,
            mailbox=config.mailbox,
        )
        # Older installations have no watermark until their first sync after
        # this feature ships.  Establish it and deliberately leave all
        # existing messages untouched.
        if config.import_start_uid is None or config.imap_uidvalidity is None:
            config.import_start_uid = current_uidnext
            config.imap_uidvalidity = imap_uidvalidity
            config.import_started_at = _utcnow()
            config.last_synced_at = _utcnow()
            config.last_sync_error = None
            session.commit()
            return MailboxSyncResponse(
                configured=True,
                last_synced_at=config.last_synced_at,
                last_sync_error=config.last_sync_error,
            )
        # A UID has meaning only inside one UIDVALIDITY epoch.  If the server
        # reports a new epoch, take a fresh UIDNEXT watermark and skip this
        # run rather than risking old mail or UID reuse being imported.
        if config.imap_uidvalidity != imap_uidvalidity:
            config.import_start_uid = current_uidnext
            config.imap_uidvalidity = imap_uidvalidity
            config.import_started_at = _utcnow()
            config.last_synced_at = _utcnow()
            config.last_sync_error = None
            session.commit()
            return MailboxSyncResponse(
                configured=True,
                last_synced_at=config.last_synced_at,
                last_sync_error=config.last_sync_error,
            )
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
                try:
                    resume = _ingest_attachment(
                        session,
                        config=config,
                        filename=filename,
                        content=content,
                        settings=settings,
                    )
                    _record(
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
                    imported += 1
                except _AttachmentIngestionFailure as exc:
                    session.rollback()
                    _discard_failed_attachment(
                        settings=settings,
                        organization_id=organization_id,
                        failure=exc,
                    )
                    config = session.get(MailboxConfig, mailbox_config_id)
                    if config is None or config.organization_id != organization_id:
                        raise MailboxImportError("mailbox_config_not_found")
                    _record(
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
                    failed += 1
        config = session.get(MailboxConfig, mailbox_config_id)
        if config is None or config.organization_id != organization_id:
            raise MailboxImportError("mailbox_config_not_found")
        config.last_synced_at = _utcnow()
        config.last_sync_error = None
        session.commit()
    except (imaplib.IMAP4.error, OSError, MailboxImportError, SQLAlchemyError) as exc:
        session.rollback()
        config = session.get(MailboxConfig, mailbox_config_id)
        if config is not None and config.organization_id == organization_id:
            config.last_sync_error = (
                str(exc)
                if str(exc).startswith("mailbox_")
                else "mailbox_sync_failed"
                if isinstance(exc, SQLAlchemyError)
                else "mailbox_connection_failed"
            )
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
    return MailboxSyncResponse(
        configured=True,
        imported_count=imported,
        duplicate_count=duplicates,
        skipped_count=skipped,
        failed_count=failed,
        last_synced_at=config.last_synced_at,
        last_sync_error=config.last_sync_error,
    )


def sync_due_mailboxes(*, database, settings: AppSettings) -> bool:
    cutoff = _utcnow() - timedelta(seconds=settings.mailbox_sync_interval_seconds)
    with database.session_factory() as session:
        claimed = session.execute(
            select(MailboxConfig.id, MailboxConfig.organization_id)
            .where(MailboxConfig.enabled.is_(True))
            .where(
                (MailboxConfig.import_start_uid.is_(None))
                | (MailboxConfig.imap_uidvalidity.is_(None))
                | (MailboxConfig.last_synced_at.is_(None))
                | (MailboxConfig.last_synced_at <= cutoff)
            )
            .order_by(MailboxConfig.last_synced_at)
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
                .values(last_sync_error="mailbox_workspace_missing")
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
