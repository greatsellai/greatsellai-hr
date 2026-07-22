"""Durable transactional-email delivery for account recovery.

The public password-reset endpoint persists an encrypted work item and returns
without opening an SMTP/SES connection.  A single shared worker claims jobs
with a lease, retries safe provider failures, and never records a raw reset
token, URL, recipient address, or provider response in the outbox state.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, joinedload

from app.config import AppSettings
from app.database import Database
from app.models import PasswordResetToken, TransactionalEmailOutbox, UserAccount
from app.services.identity_service import IssuedPasswordReset, digest_token
from app.services.transactional_email import (
    PasswordResetDelivery,
    TransactionalEmailError,
    TransactionalEmailProvider,
    build_transactional_email_provider,
    password_reset_url,
)


logger = logging.getLogger(__name__)

OUTBOX_KIND_PASSWORD_RESET = "password_reset"
OUTBOX_QUEUED = "queued"
OUTBOX_RUNNING = "running"
OUTBOX_COMPLETED = "completed"
OUTBOX_FAILED = "failed"
OUTBOX_CANCELLED = "cancelled"


class TransactionalEmailOutboxError(RuntimeError):
    """Stable, non-sensitive outbox configuration or persistence failure."""


@dataclass(frozen=True)
class ClaimedTransactionalEmail:
    job_id: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _payload_fernet(settings: AppSettings) -> Fernet:
    """Resolve the dedicated at-rest key without using the legacy token.

    Production validation requires a real ``EMAIL_CREDENTIALS_KEY``. A stable
    development fallback keeps isolated local tests usable while remaining
    deliberately unavailable to production configurations.
    """

    if settings.email_credentials_key:
        try:
            return Fernet(settings.email_credentials_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise TransactionalEmailOutboxError(
                "transactional_email_outbox_key_invalid"
            ) from exc
    if settings.environment in {"production", "prod"}:
        raise TransactionalEmailOutboxError(
            "transactional_email_outbox_key_not_configured"
        )
    material = settings.session_signing_secret().encode("utf-8")
    derived = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(derived)


def enqueue_password_reset_delivery(
    session: Session,
    *,
    settings: AppSettings,
    issued: IssuedPasswordReset,
) -> TransactionalEmailOutbox:
    """Persist one encrypted reset-mail job in the caller's transaction."""

    encrypted_payload = _payload_fernet(settings).encrypt(issued.token.encode("utf-8")).decode(
        "ascii"
    )
    job = TransactionalEmailOutbox(
        message_kind=OUTBOX_KIND_PASSWORD_RESET,
        user_id=issued.user_id,
        password_reset_token_id=issued.password_reset_token_id,
        encrypted_payload=encrypted_payload,
        status=OUTBOX_QUEUED,
        max_attempts=settings.transactional_email_outbox_max_attempts,
        next_attempt_at=utcnow(),
    )
    session.add(job)
    session.flush()
    return job


def run_transactional_email_outbox_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    provider: TransactionalEmailProvider | None = None,
) -> bool:
    """Claim and process at most one durable transactional-email job."""

    claimed = _claim_next_outbox_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_outbox_job(
        database,
        settings=settings,
        worker_id=worker_id,
        claimed=claimed,
        provider=provider,
    )
    return True


def _claim_next_outbox_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedTransactionalEmail | None:
    now = utcnow()
    with database.session_factory() as session:
        _recover_expired_leases(session, now=now)
        eligible = and_(
            TransactionalEmailOutbox.status == OUTBOX_QUEUED,
            TransactionalEmailOutbox.attempt_count < TransactionalEmailOutbox.max_attempts,
            or_(
                TransactionalEmailOutbox.next_attempt_at.is_(None),
                TransactionalEmailOutbox.next_attempt_at <= now,
            ),
        )
        candidate_id = session.scalar(
            select(TransactionalEmailOutbox.id)
            .where(eligible)
            .order_by(
                TransactionalEmailOutbox.next_attempt_at.asc(),
                TransactionalEmailOutbox.requested_at.asc(),
                TransactionalEmailOutbox.id.asc(),
            )
            .limit(1)
        )
        if candidate_id is None:
            session.commit()
            return None

        claim = session.execute(
            update(TransactionalEmailOutbox)
            .where(TransactionalEmailOutbox.id == candidate_id, eligible)
            .values(
                status=OUTBOX_RUNNING,
                attempt_count=TransactionalEmailOutbox.attempt_count + 1,
                lease_owner=worker_id,
                lease_expires_at=now
                + timedelta(seconds=settings.transactional_email_outbox_lease_seconds),
                next_attempt_at=None,
                last_error=None,
                started_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        return ClaimedTransactionalEmail(job_id=candidate_id)


def _recover_expired_leases(session: Session, *, now: datetime) -> None:
    expired = and_(
        TransactionalEmailOutbox.status == OUTBOX_RUNNING,
        TransactionalEmailOutbox.lease_expires_at.is_not(None),
        TransactionalEmailOutbox.lease_expires_at <= now,
    )
    session.execute(
        update(TransactionalEmailOutbox)
        .where(expired, TransactionalEmailOutbox.attempt_count >= TransactionalEmailOutbox.max_attempts)
        .values(
            status=OUTBOX_FAILED,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error="transactional_email_worker_lease_expired",
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(TransactionalEmailOutbox)
        .where(expired, TransactionalEmailOutbox.attempt_count < TransactionalEmailOutbox.max_attempts)
        .values(
            status=OUTBOX_QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=now,
            last_error="transactional_email_worker_lease_expired",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


def _owned_running_job(
    session: Session,
    *,
    claimed: ClaimedTransactionalEmail,
    worker_id: str,
) -> TransactionalEmailOutbox | None:
    return session.scalar(
        select(TransactionalEmailOutbox)
        .options(
            joinedload(TransactionalEmailOutbox.user),
            joinedload(TransactionalEmailOutbox.password_reset_token),
        )
        .where(
            TransactionalEmailOutbox.id == claimed.job_id,
            TransactionalEmailOutbox.status == OUTBOX_RUNNING,
            TransactionalEmailOutbox.lease_owner == worker_id,
        )
    )


def _load_delivery(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedTransactionalEmail,
) -> tuple[str, str] | None:
    """Return recipient/token only for an owned, still-valid reset job."""

    with database.session_factory() as session:
        job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
        if job is None:
            session.rollback()
            return None
        if job.message_kind != OUTBOX_KIND_PASSWORD_RESET:
            _cancel_job(job, now=utcnow(), code="transactional_email_kind_unsupported")
            session.commit()
            return None
        reset = job.password_reset_token
        user = job.user
        if _reset_is_not_deliverable(reset, user, now=utcnow()):
            _cancel_job(job, now=utcnow(), code="password_reset_delivery_no_longer_valid")
            session.commit()
            return None
        try:
            raw_token = _payload_fernet(settings).decrypt(job.encrypted_payload.encode("ascii")).decode(
                "utf-8"
            )
        except (InvalidToken, UnicodeError, TransactionalEmailOutboxError):
            _fail_job(
                job,
                settings=settings,
                now=utcnow(),
                code="transactional_email_payload_unavailable",
                retryable=False,
            )
            session.commit()
            return None
        if digest_token(raw_token) != reset.token_digest:
            _cancel_job(job, now=utcnow(), code="password_reset_delivery_token_mismatch")
            session.commit()
            return None
        recipient = user.email
        session.commit()
        return recipient, raw_token


def _reset_is_not_deliverable(
    reset: PasswordResetToken | None,
    user: UserAccount | None,
    *,
    now: datetime,
) -> bool:
    if reset is None or user is None or not user.is_active:
        return True
    expires_at = _aware(reset.expires_at)
    return (
        reset.used_at is not None
        or reset.invalidated_at is not None
        or expires_at is None
        or expires_at <= now
    )


def _process_claimed_outbox_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedTransactionalEmail,
    provider: TransactionalEmailProvider | None,
) -> None:
    delivery = _load_delivery(
        database,
        settings=settings,
        worker_id=worker_id,
        claimed=claimed,
    )
    if delivery is None:
        return
    recipient, raw_token = delivery
    delivery_provider = provider or build_transactional_email_provider(settings)
    try:
        if not delivery_provider.password_reset_configured:
            raise TransactionalEmailError("password_reset_delivery_not_configured")
        delivery_provider.send_password_reset(
            PasswordResetDelivery(
                recipient=recipient,
                reset_url=password_reset_url(settings, token=raw_token),
                expires_minutes=max(1, settings.password_reset_ttl_seconds // 60),
            )
        )
    except TransactionalEmailError as exc:
        _finish_delivery_failure(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
            code=_safe_provider_error_code(exc),
        )
        return
    except Exception:
        # Do not retain provider exception text: it can contain remote
        # infrastructure details and must never include reset material.
        logger.warning("transactional_email_outbox_delivery_failed")
        _finish_delivery_failure(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
            code="transactional_email_delivery_failed",
        )
        return
    _finish_delivery_success(database, worker_id=worker_id, claimed=claimed)


def _safe_provider_error_code(exc: TransactionalEmailError) -> str:
    code = str(exc)
    allowed = {
        "email_delivery_not_configured",
        "password_reset_delivery_not_configured",
        "email_delivery_provider_failed",
    }
    return code if code in allowed else "transactional_email_delivery_failed"


def _finish_delivery_success(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedTransactionalEmail,
) -> None:
    now = utcnow()
    with database.session_factory() as session:
        job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
        if job is None:
            session.rollback()
            return
        job.status = OUTBOX_COMPLETED
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = None
        job.last_error = None
        job.sent_at = now
        job.completed_at = now
        job.updated_at = now
        session.commit()


def _finish_delivery_failure(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedTransactionalEmail,
    code: str,
) -> None:
    with database.session_factory() as session:
        job = _owned_running_job(session, claimed=claimed, worker_id=worker_id)
        if job is None:
            session.rollback()
            return
        _fail_job(
            job,
            settings=settings,
            now=utcnow(),
            code=code,
            retryable=True,
        )
        session.commit()


def _cancel_job(job: TransactionalEmailOutbox, *, now: datetime, code: str) -> None:
    job.status = OUTBOX_CANCELLED
    job.lease_owner = None
    job.lease_expires_at = None
    job.next_attempt_at = None
    job.last_error = code
    job.completed_at = now
    job.updated_at = now


def _fail_job(
    job: TransactionalEmailOutbox,
    *,
    settings: AppSettings,
    now: datetime,
    code: str,
    retryable: bool,
) -> None:
    will_retry = retryable and job.attempt_count < job.max_attempts
    job.status = OUTBOX_QUEUED if will_retry else OUTBOX_FAILED
    job.lease_owner = None
    job.lease_expires_at = None
    job.next_attempt_at = (
        now + timedelta(seconds=_retry_delay_seconds(job.attempt_count, settings=settings))
        if will_retry
        else None
    )
    job.last_error = code
    job.completed_at = None if will_retry else now
    job.updated_at = now


def _retry_delay_seconds(attempt_count: int, *, settings: AppSettings) -> int:
    # 1m, 2m, 4m, ... bounded to one day. The worker never leaks this delay to
    # the requester, so delivery retries cannot become an account oracle.
    exponent = max(0, min(10, attempt_count - 1))
    return min(24 * 60 * 60, settings.transactional_email_outbox_retry_base_seconds * (2**exponent))


__all__ = [
    "OUTBOX_CANCELLED",
    "OUTBOX_COMPLETED",
    "OUTBOX_FAILED",
    "OUTBOX_KIND_PASSWORD_RESET",
    "OUTBOX_QUEUED",
    "TransactionalEmailOutboxError",
    "enqueue_password_reset_delivery",
    "run_transactional_email_outbox_worker_once",
]
