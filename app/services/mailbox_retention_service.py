"""Privacy-first retention for transient mailbox content.

Mailbox ingestion intentionally treats a successfully imported attachment as a
candidate resume, not as an email cache.  This module owns the *separate*
short-lived files that are useful for a failed retry or a narrowly scoped mail
body audit.  Its path resolver accepts only the dedicated cache namespace, so
the cleanup worker cannot resolve or unlink a ``Resume.storage_key``.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal
from uuid import uuid4

from sqlalchemy import desc, or_, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    EmailAttachmentImport,
    MailboxConfig,
    MailboxContentReplica,
    MailboxRetentionCleanupRun,
)
from app.schemas import (
    MailboxRetentionCleanupRunHistoryResponse,
    MailboxRetentionCleanupRunResponse,
    MailboxRetentionPreviewResponse,
    MailboxRetentionSummaryResponse,
)
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


RetentionPolicy = Literal["minimal", "standard", "audit"]

_CACHE_DIRECTORY = "mail-cache"
_CLEANUP_LEASE_SECONDS = 120
_POLICY_DAYS: dict[RetentionPolicy, dict[str, int]] = {
    # Minimal never writes a body cache.  It keeps a short failure artifact so
    # an HR user can still retry a transient parser failure without resending.
    "minimal": {"body": 0, "attachment_copy": 0, "failed_attachment": 7},
    # Default: body audit for one week, failed retry source for one month.
    "standard": {"body": 7, "attachment_copy": 1, "failed_attachment": 30},
    "audit": {"body": 30, "attachment_copy": 7, "failed_attachment": 90},
}


class MailboxRetentionError(RuntimeError):
    """A safe, non-content-bearing mailbox retention error."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalized_policy(value: str | None) -> RetentionPolicy:
    policy = (value or "standard").strip().lower()
    if policy not in _POLICY_DAYS:
        return "standard"
    return policy  # type: ignore[return-value]


def _retention_duration(policy: RetentionPolicy, kind: str) -> timedelta:
    return timedelta(days=_POLICY_DAYS[policy].get(kind, 0))


def _validated_organization_id(organization_id: str) -> str:
    normalized = organization_id.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise MailboxRetentionError("mailbox_retention_storage_invalid")
    return normalized


def _cache_key_parts(storage_key: str, *, organization_id: str) -> tuple[str, str, str]:
    if not storage_key or "\\" in storage_key:
        raise MailboxRetentionError("mailbox_retention_storage_invalid")
    path = PurePosixPath(storage_key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MailboxRetentionError("mailbox_retention_storage_invalid")
    namespace = _validated_organization_id(organization_id)
    parts = path.parts
    if len(parts) != 3 or parts[0] != namespace or parts[1] != _CACHE_DIRECTORY:
        raise MailboxRetentionError("mailbox_retention_storage_invalid")
    return parts[0], parts[1], parts[2]


def _build_cache_storage_key(*, organization_id: str, suffix: str) -> str:
    namespace = _validated_organization_id(organization_id)
    normalized_suffix = suffix.lower().strip()
    if not normalized_suffix.startswith(".") or len(normalized_suffix) > 16:
        normalized_suffix = ".bin"
    if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789" for character in normalized_suffix):
        normalized_suffix = ".bin"
    return f"{namespace}/{_CACHE_DIRECTORY}/{uuid4().hex}{normalized_suffix}"


def resolve_mailbox_replica_path(
    settings: AppSettings,
    *,
    storage_key: str,
    organization_id: str,
    require_file: bool = True,
) -> Path:
    """Resolve only a file in ``<workspace>/mail-cache``.

    It is intentionally independent from the resume original resolver.  A
    retention record cannot point at a candidate's direct workspace file even
    if a database row is damaged or maliciously edited.
    """

    namespace, _, filename = _cache_key_parts(
        storage_key,
        organization_id=organization_id,
    )
    try:
        upload_root = settings.upload_dir.resolve()
        workspace_directory = upload_root / namespace
        cache_directory = workspace_directory / _CACHE_DIRECTORY
        raw_path = cache_directory / filename
        if (
            workspace_directory.is_symlink()
            or cache_directory.is_symlink()
            or raw_path.is_symlink()
        ):
            raise MailboxRetentionError("mailbox_retention_storage_invalid")
        resolved_path = raw_path.resolve()
        expected_parent = cache_directory.resolve()
        if resolved_path.parent != expected_parent:
            raise MailboxRetentionError("mailbox_retention_storage_invalid")
        resolved_path.relative_to(upload_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise MailboxRetentionError("mailbox_retention_storage_invalid") from exc
    if require_file and not resolved_path.is_file():
        raise MailboxRetentionError("mailbox_retention_file_missing")
    return resolved_path


def _prepare_replica_path(
    settings: AppSettings,
    *,
    storage_key: str,
    organization_id: str,
) -> Path:
    namespace = _validated_organization_id(organization_id)
    settings.ensure_directories()
    try:
        workspace_directory = settings.upload_dir.resolve() / namespace
        cache_directory = workspace_directory / _CACHE_DIRECTORY
        workspace_directory.mkdir(parents=True, exist_ok=True)
        cache_directory.mkdir(parents=True, exist_ok=True)
        if workspace_directory.is_symlink() or cache_directory.is_symlink():
            raise MailboxRetentionError("mailbox_retention_storage_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        raise MailboxRetentionError("mailbox_retention_storage_invalid") from exc
    return resolve_mailbox_replica_path(
        settings,
        storage_key=storage_key,
        organization_id=namespace,
        require_file=False,
    )


def _write_atomically(*, storage_path: Path, content: bytes) -> None:
    temporary_path = storage_path.with_name(
        f".{storage_path.name}.{uuid4().hex}.mail-cache"
    )
    try:
        if storage_path.exists():
            raise MailboxRetentionError("mailbox_retention_storage_conflict")
        with temporary_path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, storage_path)
    except MailboxRetentionError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise MailboxRetentionError("mailbox_retention_storage_write_failed") from exc


def _active_config(session: Session, *, config_id: str | None = None) -> MailboxConfig | None:
    query = select(MailboxConfig).order_by(desc(MailboxConfig.created_at))
    if config_id:
        query = select(MailboxConfig).where(MailboxConfig.id == config_id)
    return session.scalar(query)


def _replica_expiry(*, created_at: datetime, policy: RetentionPolicy, kind: str) -> datetime:
    duration = _retention_duration(policy, kind)
    base = _as_utc(created_at) or _utcnow()
    return base + duration


def _store_replica(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig,
    kind: str,
    source_reference: str,
    content: bytes,
    suffix: str,
    attachment_import_id: str | None,
) -> MailboxContentReplica | None:
    policy = _normalized_policy(config.retention_policy)
    duration = _retention_duration(policy, kind)
    if not content or duration <= timedelta(0):
        return None
    organization_id = organization_context_id(session)
    if config.organization_id != organization_id:
        raise MailboxRetentionError("mailbox_retention_workspace_mismatch")

    existing = session.scalar(
        select(MailboxContentReplica).where(
            MailboxContentReplica.mailbox_config_id == config.id,
            MailboxContentReplica.kind == kind,
            MailboxContentReplica.source_reference == source_reference[:128],
        )
    )
    if existing is not None and existing.cleaned_at is None:
        return existing

    now = _utcnow()
    storage_key = _build_cache_storage_key(
        organization_id=organization_id,
        suffix=suffix,
    )
    storage_path = _prepare_replica_path(
        settings,
        storage_key=storage_key,
        organization_id=organization_id,
    )
    _write_atomically(storage_path=storage_path, content=content)
    try:
        if existing is None:
            replica = MailboxContentReplica(
                organization_id=organization_id,
                mailbox_config_id=config.id,
                email_attachment_import_id=attachment_import_id,
                kind=kind,
                source_reference=source_reference[:128],
                storage_key=storage_key,
                content_sha256=hashlib.sha256(content).hexdigest(),
                byte_size=len(content),
                expires_at=now + duration,
                cleaned_at=None,
                cleanup_error=None,
                cleanup_claim_token=None,
                cleanup_lease_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(replica)
        else:
            replica = existing
            replica.email_attachment_import_id = attachment_import_id
            replica.storage_key = storage_key
            replica.content_sha256 = hashlib.sha256(content).hexdigest()
            replica.byte_size = len(content)
            replica.expires_at = now + duration
            replica.cleaned_at = None
            replica.cleanup_error = None
            replica.cleanup_claim_token = None
            replica.cleanup_lease_expires_at = None
            replica.updated_at = now
        session.flush()
        return replica
    except Exception:
        try:
            storage_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def store_mailbox_body_copy(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig,
    message_uid: str,
    content: bytes,
) -> MailboxContentReplica | None:
    return _store_replica(
        session,
        settings=settings,
        config=config,
        kind="body",
        source_reference=message_uid,
        content=content,
        suffix=".txt",
        attachment_import_id=None,
    )


def store_failed_attachment_copy(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig,
    attachment_import: EmailAttachmentImport,
    content: bytes,
    suffix: str,
) -> MailboxContentReplica | None:
    return _store_replica(
        session,
        settings=settings,
        config=config,
        kind="failed_attachment",
        source_reference=attachment_import.id,
        content=content,
        suffix=suffix,
        attachment_import_id=attachment_import.id,
    )


def store_success_attachment_copy(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig,
    attachment_import: EmailAttachmentImport,
    content: bytes,
    suffix: str,
) -> MailboxContentReplica | None:
    """Keep a policy-bounded duplicate only when the policy calls for one.

    The duplicate is never used as the candidate original.  Minimal retention
    returns ``None`` here, while standard and audit keep it briefly for mail
    traceability before the scheduled cleaner removes it.
    """

    return _store_replica(
        session,
        settings=settings,
        config=config,
        kind="attachment_copy",
        source_reference=attachment_import.id,
        content=content,
        suffix=suffix,
        attachment_import_id=attachment_import.id,
    )


def read_retained_failed_attachment(
    session: Session,
    *,
    settings: AppSettings,
    attachment_import: EmailAttachmentImport,
) -> bytes | None:
    """Read a still-valid failure cache for retry, verifying its digest."""

    now = _utcnow()
    replica = session.scalar(
        select(MailboxContentReplica)
        .where(
            MailboxContentReplica.email_attachment_import_id == attachment_import.id,
            MailboxContentReplica.kind == "failed_attachment",
            MailboxContentReplica.cleaned_at.is_(None),
            MailboxContentReplica.expires_at > now,
        )
        .order_by(desc(MailboxContentReplica.created_at))
    )
    if replica is None:
        return None
    try:
        content = resolve_mailbox_replica_path(
            settings,
            storage_key=replica.storage_key,
            organization_id=attachment_import.organization_id,
        ).read_bytes()
    except (OSError, MailboxRetentionError):
        return None
    if hashlib.sha256(content).hexdigest() != replica.content_sha256:
        return None
    return content


def discard_retained_failed_attachment(
    session: Session,
    *,
    settings: AppSettings,
    attachment_import_id: str,
) -> None:
    """Purge a retry cache as soon as its candidate resume is imported."""

    now = _utcnow()
    replicas = session.scalars(
        select(MailboxContentReplica).where(
            MailboxContentReplica.email_attachment_import_id == attachment_import_id,
            MailboxContentReplica.kind == "failed_attachment",
            MailboxContentReplica.cleaned_at.is_(None),
        )
    ).all()
    changed = False
    for replica in replicas:
        try:
            path = resolve_mailbox_replica_path(
                settings,
                storage_key=replica.storage_key,
                organization_id=replica.organization_id,
                require_file=False,
            )
            path.unlink(missing_ok=True)
        except (OSError, MailboxRetentionError):
            # A subsequent scheduled cleanup can retry.  The imported resume
            # remains correct regardless of a cache deletion failure.
            replica.cleanup_error = "storage_delete_failed"
            changed = True
            continue
        replica.cleaned_at = now
        replica.cleanup_error = None
        replica.cleanup_claim_token = None
        replica.cleanup_lease_expires_at = None
        replica.updated_at = now
        changed = True
    if changed:
        session.commit()


def _summary_values(
    session: Session,
    *,
    settings: AppSettings,
    config: MailboxConfig | None,
) -> dict[str, object]:
    if config is None:
        return {
            "configured": False,
            "retention_policy": "standard",
            "body_copy_count": 0,
            "attachment_copy_count": 0,
            "failure_artifact_count": 0,
            "cache_bytes": 0,
            "expired_body_count": 0,
            "expired_attachment_copy_count": 0,
            "expired_failure_artifact_count": 0,
            "expired_bytes": 0,
            "earliest_expires_at": None,
            "last_cleanup_at": None,
            "next_cleanup_at": None,
        }

    now = _utcnow()
    replicas = session.scalars(
        select(MailboxContentReplica)
        .where(
            MailboxContentReplica.mailbox_config_id == config.id,
            MailboxContentReplica.cleaned_at.is_(None),
        )
        .order_by(MailboxContentReplica.expires_at)
    ).all()
    counts = {"body": 0, "attachment_copy": 0, "failed_attachment": 0}
    expired_counts = {"body": 0, "attachment_copy": 0, "failed_attachment": 0}
    cache_bytes = expired_bytes = 0
    earliest_expires_at: datetime | None = None
    for replica in replicas:
        if replica.kind not in counts:
            continue
        counts[replica.kind] += 1
        cache_bytes += int(replica.byte_size or 0)
        expiry = _as_utc(replica.expires_at)
        if earliest_expires_at is None or (
            expiry is not None and expiry < earliest_expires_at
        ):
            earliest_expires_at = expiry
        if expiry is not None and expiry <= now:
            expired_counts[replica.kind] += 1
            expired_bytes += int(replica.byte_size or 0)

    last_cleanup_at = _as_utc(config.last_retention_cleanup_at)
    interval = timedelta(seconds=settings.mailbox_retention_cleanup_interval_seconds)
    scheduled_at = (last_cleanup_at + interval) if last_cleanup_at else now
    if earliest_expires_at is not None:
        scheduled_at = min(scheduled_at, max(now, earliest_expires_at))
    return {
        "configured": True,
        "retention_policy": _normalized_policy(config.retention_policy),
        "body_copy_count": counts["body"],
        "attachment_copy_count": counts["attachment_copy"],
        "failure_artifact_count": counts["failed_attachment"],
        "cache_bytes": cache_bytes,
        "expired_body_count": expired_counts["body"],
        "expired_attachment_copy_count": expired_counts["attachment_copy"],
        "expired_failure_artifact_count": expired_counts["failed_attachment"],
        "expired_bytes": expired_bytes,
        "earliest_expires_at": earliest_expires_at,
        "last_cleanup_at": last_cleanup_at,
        "next_cleanup_at": scheduled_at,
    }


def get_mailbox_retention_summary(
    session: Session,
    *,
    settings: AppSettings,
) -> MailboxRetentionSummaryResponse:
    return MailboxRetentionSummaryResponse(
        **_summary_values(
            session,
            settings=settings,
            config=_active_config(session),
        )
    )


def update_mailbox_retention_policy(
    session: Session,
    *,
    settings: AppSettings,
    retention_policy: str,
) -> MailboxRetentionSummaryResponse:
    config = _active_config(session)
    if config is None:
        raise MailboxRetentionError("mailbox_not_configured")
    policy = _normalized_policy(retention_policy)
    if retention_policy.strip().lower() not in _POLICY_DAYS:
        raise MailboxRetentionError("mailbox_retention_policy_invalid")
    config.retention_policy = policy
    for replica in session.scalars(
        select(MailboxContentReplica).where(
            MailboxContentReplica.mailbox_config_id == config.id,
            MailboxContentReplica.cleaned_at.is_(None),
        )
    ):
        replica.expires_at = _replica_expiry(
            created_at=replica.created_at,
            policy=policy,
            kind=replica.kind,
        )
        replica.updated_at = _utcnow()
    session.commit()
    return MailboxRetentionSummaryResponse(
        **_summary_values(session, settings=settings, config=config)
    )


def _retry_is_active(session: Session, replica: MailboxContentReplica, *, now: datetime) -> bool:
    if not replica.email_attachment_import_id:
        return False
    attachment_import = session.scalar(
        select(EmailAttachmentImport).where(
            EmailAttachmentImport.id == replica.email_attachment_import_id
        )
    )
    if attachment_import is None or attachment_import.status != "retrying":
        return False
    lease_expires_at = _as_utc(attachment_import.retry_lease_expires_at)
    return lease_expires_at is not None and lease_expires_at > now


def preview_mailbox_retention_cleanup(
    session: Session,
    *,
    settings: AppSettings,
) -> MailboxRetentionPreviewResponse:
    config = _active_config(session)
    values = _summary_values(session, settings=settings, config=config)
    if config is None:
        return MailboxRetentionPreviewResponse(**values, skipped_count=0)
    now = _utcnow()
    skipped_count = 0
    for replica in session.scalars(
        select(MailboxContentReplica).where(
            MailboxContentReplica.mailbox_config_id == config.id,
            MailboxContentReplica.cleaned_at.is_(None),
            MailboxContentReplica.expires_at <= now,
        )
    ):
        if _retry_is_active(session, replica, now=now):
            skipped_count += 1
    return MailboxRetentionPreviewResponse(**values, skipped_count=skipped_count)


def _run_response(
    run: MailboxRetentionCleanupRun,
    *,
    next_cleanup_at: datetime | None,
) -> MailboxRetentionCleanupRunResponse:
    return MailboxRetentionCleanupRunResponse(
        run_id=run.id,
        trigger_type=run.trigger_type,  # type: ignore[arg-type]
        status=run.status,
        retention_policy=_normalized_policy(run.retention_policy),
        started_at=run.started_at,
        finished_at=run.finished_at,
        scanned_count=run.scanned_count,
        deleted_count=run.deleted_count,
        skipped_count=run.skipped_count,
        failed_count=run.failed_count,
        reclaimed_bytes=run.reclaimed_bytes,
        next_cleanup_at=next_cleanup_at,
        error_code=run.error_code,
    )


def _claim_replica_cleanup(
    session: Session,
    *,
    replica: MailboxContentReplica,
    now: datetime,
) -> str | None:
    claim_token = uuid4().hex
    claimed = session.execute(
        update(MailboxContentReplica)
        .where(
            MailboxContentReplica.id == replica.id,
            MailboxContentReplica.organization_id == organization_context_id(session),
            MailboxContentReplica.cleaned_at.is_(None),
            or_(
                MailboxContentReplica.cleanup_lease_expires_at.is_(None),
                MailboxContentReplica.cleanup_lease_expires_at <= now,
            ),
        )
        .values(
            cleanup_claim_token=claim_token,
            cleanup_lease_expires_at=now + timedelta(seconds=_CLEANUP_LEASE_SECONDS),
            updated_at=now,
        )
    )
    if claimed.rowcount != 1:
        return None
    session.commit()
    return claim_token


def _finish_replica_cleanup(
    session: Session,
    *,
    replica: MailboxContentReplica,
    claim_token: str,
    now: datetime,
    cleaned: bool,
    error_code: str | None,
) -> bool:
    completed = session.execute(
        update(MailboxContentReplica)
        .where(
            MailboxContentReplica.id == replica.id,
            MailboxContentReplica.organization_id == organization_context_id(session),
            MailboxContentReplica.cleaned_at.is_(None),
            MailboxContentReplica.cleanup_claim_token == claim_token,
        )
        .values(
            cleaned_at=now if cleaned else None,
            cleanup_error=error_code,
            cleanup_claim_token=None,
            cleanup_lease_expires_at=None,
            updated_at=now,
        )
    )
    session.commit()
    return completed.rowcount == 1


def cleanup_mailbox_retention(
    session: Session,
    *,
    settings: AppSettings,
    trigger_type: Literal["manual", "scheduled"],
    config_id: str | None = None,
) -> MailboxRetentionCleanupRunResponse:
    config = _active_config(session, config_id=config_id)
    if config is None:
        raise MailboxRetentionError("mailbox_not_configured")
    now = _utcnow()
    run = MailboxRetentionCleanupRun(
        organization_id=organization_context_id(session),
        mailbox_config_id=config.id,
        trigger_type=trigger_type,
        retention_policy=_normalized_policy(config.retention_policy),
        status="running",
        started_at=now,
    )
    session.add(run)
    session.commit()

    scanned = deleted = skipped = failed = reclaimed_bytes = 0
    replicas = session.scalars(
        select(MailboxContentReplica)
        .where(
            MailboxContentReplica.mailbox_config_id == config.id,
            MailboxContentReplica.cleaned_at.is_(None),
            MailboxContentReplica.expires_at <= now,
        )
        .order_by(MailboxContentReplica.expires_at)
    ).all()
    for replica in replicas:
        scanned += 1
        if _retry_is_active(session, replica, now=now):
            skipped += 1
            continue
        claim_token = _claim_replica_cleanup(session, replica=replica, now=now)
        if claim_token is None:
            skipped += 1
            continue
        try:
            cache_path = resolve_mailbox_replica_path(
                settings,
                storage_key=replica.storage_key,
                organization_id=replica.organization_id,
                require_file=False,
            )
            cache_path.unlink(missing_ok=True)
        except (OSError, MailboxRetentionError):
            _finish_replica_cleanup(
                session,
                replica=replica,
                claim_token=claim_token,
                now=now,
                cleaned=False,
                error_code="storage_delete_failed",
            )
            failed += 1
            continue
        if _finish_replica_cleanup(
            session,
            replica=replica,
            claim_token=claim_token,
            now=now,
            cleaned=True,
            error_code=None,
        ):
            deleted += 1
            reclaimed_bytes += int(replica.byte_size or 0)
        else:
            skipped += 1

    config = _active_config(session, config_id=config.id)
    if config is None:
        raise MailboxRetentionError("mailbox_not_configured")
    config.last_retention_cleanup_at = now
    run = session.get(MailboxRetentionCleanupRun, run.id)
    if run is None:
        raise MailboxRetentionError("mailbox_retention_run_not_found")
    run.scanned_count = scanned
    run.deleted_count = deleted
    run.skipped_count = skipped
    run.failed_count = failed
    run.reclaimed_bytes = reclaimed_bytes
    run.status = "completed_with_errors" if failed else "completed"
    run.error_code = "storage_delete_failed" if failed else None
    run.finished_at = _utcnow()
    session.commit()
    summary = _summary_values(session, settings=settings, config=config)
    return _run_response(run, next_cleanup_at=summary["next_cleanup_at"])


def list_mailbox_retention_cleanup_runs(
    session: Session,
    *,
    settings: AppSettings,
    limit: int = 20,
) -> MailboxRetentionCleanupRunHistoryResponse:
    config = _active_config(session)
    if config is None:
        return MailboxRetentionCleanupRunHistoryResponse(items=[], total=0)
    runs = session.scalars(
        select(MailboxRetentionCleanupRun)
        .where(MailboxRetentionCleanupRun.mailbox_config_id == config.id)
        .order_by(desc(MailboxRetentionCleanupRun.started_at))
        .limit(limit)
    ).all()
    total = len(
        session.scalars(
            select(MailboxRetentionCleanupRun.id).where(
                MailboxRetentionCleanupRun.mailbox_config_id == config.id
            )
        ).all()
    )
    summary = _summary_values(session, settings=settings, config=config)
    return MailboxRetentionCleanupRunHistoryResponse(
        items=[
            _run_response(run, next_cleanup_at=summary["next_cleanup_at"])
            for run in runs
        ],
        total=total,
    )


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def cleanup_due_mailbox_retention(*, database, settings: AppSettings) -> bool:
    """Run one due workspace cleanup without ever using an unscoped mutation."""

    now = _utcnow()
    cutoff = now - timedelta(seconds=settings.mailbox_retention_cleanup_interval_seconds)
    with database.session_factory() as session:
        claimed = session.execute(
            select(MailboxConfig.id, MailboxConfig.organization_id)
            .where(
                (MailboxConfig.last_retention_cleanup_at.is_(None))
                | (MailboxConfig.last_retention_cleanup_at <= cutoff)
            )
            .order_by(MailboxConfig.last_retention_cleanup_at)
            .execution_options(skip_organization_scope=True)
        ).first()
        if claimed is None:
            return False
        config_id, organization_id = claimed
        if not organization_id:
            return False
        with _organization_session(session, organization_id):
            try:
                cleanup_mailbox_retention(
                    session,
                    settings=settings,
                    trigger_type="scheduled",
                    config_id=config_id,
                )
            except MailboxRetentionError:
                return True
    return True
