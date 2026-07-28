"""Workspace-scoped candidate data lifecycle services.

This module is intentionally separate from mailbox cache retention and the
platform audit console.  It owns only the candidate/resume privacy boundary:
explicit original-file grants, reversible deletion, retention policy and the
worker-safe records used by later physical cleanup/export services.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import (
    Candidate,
    CandidateDataAuditEvent,
    CandidateDataDeletionBatch,
    CandidateDataDeletionBatchItem,
    CandidateDataFileAccessGrant,
    CandidateDataPurgeJob,
    CandidateDataRetentionCleanupRun,
    CandidateDataRetentionPolicy,
    CandidateDataExport,
    EmailAttachmentImport,
    JobMatchBatchItem,
    MailboxDeletedAttachmentTombstone,
    Resume,
    ResumeAiExtractionJob,
    ResumeSummaryJob,
    ResumeScoreBatchItem,
)
from app.schemas import (
    CandidateDataAuditEventListResponse,
    CandidateDataAuditEventResponse,
    CandidateDataDeletionBatchListResponse,
    CandidateDataDeletionBatchResponse,
    CandidateDataDeletionResponse,
    CandidateDataRestoreResponse,
    CandidateDataRetentionCleanupRunHistoryResponse,
    CandidateDataRetentionCleanupRunResponse,
    CandidateDataRetentionPolicyResponse,
    CandidateDataRetentionPreviewResponse,
)
from app.services.resume_service import ResumeServiceError, resolve_uploaded_resume_path
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


class CandidateDataLifecycleError(RuntimeError):
    """A stable, privacy-safe lifecycle domain error."""


_DELETION_REASONS = frozenset(
    {
        "candidate_request",
        "recruitment_closed",
        "duplicate",
        "retention_expired",
        "other",
    }
)
_FILE_ACCESS_PURPOSES = frozenset({"view", "download"})
_AUTOMATIC_RETENTION_DAYS = frozenset({90, 180, 365, 730})
_TOMBSTONE_DOMAIN = b"greatsell-hr/mailbox-deleted-attachment/v1\x00"


@dataclass(frozen=True)
class AuthorizedFileAccess:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedFileAccess:
    path: Path
    original_filename: str
    purpose: Literal["view", "download"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lifecycle_statement(statement):
    """Read deleted lifecycle roots only inside this private service."""

    return statement.execution_options(include_deleted_candidate_data=True)


def _record_audit(
    session: Session,
    *,
    actor_user_id: str | None,
    actor_kind: str,
    action: str,
    target_type: str,
    target_id: str,
    candidate_id: str | None = None,
    resume_id: str | None = None,
    request_id: str | None = None,
    source_kind: str = "web",
    result: str = "authorized",
    reason_code: str | None = None,
) -> CandidateDataAuditEvent:
    """Append a deliberately content-free, workspace-private audit event."""

    event = CandidateDataAuditEvent(
        organization_id=organization_context_id(session),
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        action=action,
        target_type=target_type,
        target_id=target_id,
        candidate_id=candidate_id,
        resume_id=resume_id,
        request_id=request_id,
        source_kind=source_kind,
        result=result,
        reason_code=reason_code,
    )
    session.add(event)
    return event


def _visible_resume(
    session: Session,
    *,
    resume_id: str,
    for_update: bool = False,
) -> Resume:
    statement = (
        select(Resume)
        .join(Candidate, Candidate.id == Resume.candidate_id)
        .where(Resume.id == resume_id)
    )
    if for_update:
        statement = statement.with_for_update()
    resume = session.scalar(statement)
    if resume is None:
        raise CandidateDataLifecycleError("resume_not_found")
    return resume


def _visible_candidate(
    session: Session,
    *,
    candidate_id: str,
    for_update: bool = False,
) -> Candidate:
    statement = select(Candidate).where(Candidate.id == candidate_id)
    if for_update:
        statement = statement.with_for_update()
    candidate = session.scalar(statement)
    if candidate is None:
        raise CandidateDataLifecycleError("candidate_not_found")
    return candidate


def _deleted_candidate_or_error(session: Session, *, candidate_id: str) -> Candidate:
    candidate = session.scalar(
        _lifecycle_statement(select(Candidate).where(Candidate.id == candidate_id))
    )
    if candidate is None:
        raise CandidateDataLifecycleError("candidate_not_found")
    return candidate


def _deleted_resume_or_error(session: Session, *, resume_id: str) -> Resume:
    resume = session.scalar(
        _lifecycle_statement(select(Resume).where(Resume.id == resume_id))
    )
    if resume is None:
        raise CandidateDataLifecycleError("resume_not_found")
    return resume


def _locked_visible_candidate_resumes(
    session: Session,
    *,
    candidate_id: str,
) -> list[Resume]:
    """Lock current versions after locking their parent candidate.

    PostgreSQL keeps the parent and child locks to the request commit; SQLite
    still benefits from the conditional state transitions below.  Taking both
    prevents a normal delete, retention hold, and new child write from
    observing different candidate states in the same lifecycle operation.
    """

    return session.scalars(
        select(Resume)
        .where(Resume.candidate_id == candidate_id)
        .order_by(Resume.created_at.asc(), Resume.id.asc())
        .with_for_update()
    ).all()


def _claim_live_resume_for_deletion(
    session: Session,
    *,
    resume: Resume,
    deletion_batch_id: str,
    actor_user_id: str | None,
    purge_after_at: datetime,
    now: datetime,
    require_visible_candidate: bool,
) -> bool:
    """Atomically move one live resume into a deletion batch.

    The conditional version prevents two request transactions from assigning
    the same root to different recovery batches on engines where row locks are
    unavailable or deliberately deferred.
    """

    organization_id = organization_context_id(session)
    conditions = [
        Resume.id == resume.id,
        Resume.organization_id == organization_id,
        Resume.deleted_at.is_(None),
        Resume.lifecycle_version == resume.lifecycle_version,
    ]
    if require_visible_candidate:
        conditions.append(
            select(Candidate.id)
            .where(
                Candidate.id == Resume.candidate_id,
                Candidate.organization_id == organization_id,
                Candidate.deleted_at.is_(None),
            )
            .exists()
        )
    result = session.execute(
        update(Resume)
        .where(*conditions)
        .values(
            deleted_at=now,
            deleted_by_user_id=actor_user_id,
            deletion_batch_id=deletion_batch_id,
            purge_after_at=purge_after_at,
            is_active=False,
            lifecycle_version=Resume.lifecycle_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _claim_live_candidate_for_deletion(
    session: Session,
    *,
    candidate: Candidate,
    deletion_batch_id: str,
    actor_user_id: str | None,
    purge_after_at: datetime,
    now: datetime,
) -> bool:
    """Atomically claim the candidate root before touching child versions."""

    organization_id = organization_context_id(session)
    has_visible_resume = (
        select(Resume.id)
        .where(
            Resume.candidate_id == Candidate.id,
            Resume.organization_id == organization_id,
            Resume.deleted_at.is_(None),
        )
        .exists()
    )
    result = session.execute(
        update(Candidate)
        .where(
            Candidate.id == candidate.id,
            Candidate.organization_id == organization_id,
            Candidate.deleted_at.is_(None),
            Candidate.lifecycle_version == candidate.lifecycle_version,
            has_visible_resume,
        )
        .values(
            deleted_at=now,
            deleted_by_user_id=actor_user_id,
            deletion_batch_id=deletion_batch_id,
            purge_after_at=purge_after_at,
            lifecycle_version=Candidate.lifecycle_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def authorize_resume_original_access(
    session: Session,
    *,
    settings: AppSettings,
    resume_id: str,
    actor_user_id: str | None,
    session_nonce: str,
    purpose: str,
    request_id: str | None = None,
    source_kind: str = "web",
) -> AuthorizedFileAccess:
    """Create a short-lived, session-bound, audited original-file grant.

    The caller commits this transaction before returning the opaque URL.  A
    failed audit/commit therefore cannot accidentally become an unaudited file
    response.
    """

    if purpose not in _FILE_ACCESS_PURPOSES:
        raise CandidateDataLifecycleError("candidate_data_file_access_purpose_invalid")
    if not session_nonce:
        raise CandidateDataLifecycleError("candidate_data_session_nonce_missing")
    # Keep the same parent -> child lock ordering as lifecycle mutation.  The
    # version stored on the grant is the durable fence if a database releases
    # a lock before a racing request commits.
    initial_resume = _visible_resume(session, resume_id=resume_id)
    _visible_candidate(
        session,
        candidate_id=initial_resume.candidate_id,
        for_update=True,
    )
    resume = _visible_resume(session, resume_id=resume_id, for_update=True)
    try:
        resolve_uploaded_resume_path(
            settings,
            storage_key=resume.storage_key,
            organization_id=resume.organization_id,
        )
    except ResumeServiceError as exc:
        raise CandidateDataLifecycleError("resume_original_file_not_found") from exc

    now = utcnow()
    token = secrets.token_urlsafe(32)
    access = CandidateDataFileAccessGrant(
        organization_id=resume.organization_id,
        actor_user_id=actor_user_id,
        resource_type="resume_original",
        resource_id=resume.id,
        purpose=purpose,
        token_digest=_digest(token),
        session_nonce_digest=_digest(session_nonce),
        resource_lifecycle_version=resume.lifecycle_version,
        expires_at=now + timedelta(seconds=settings.candidate_data_file_access_ttl_seconds),
    )
    session.add(access)
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "legacy_member",
        action=(
            "resume_original_view_authorized"
            if purpose == "view"
            else "resume_original_download_authorized"
        ),
        target_type="resume",
        target_id=resume.id,
        candidate_id=resume.candidate_id,
        resume_id=resume.id,
        request_id=request_id,
        source_kind=source_kind,
    )
    session.flush()
    return AuthorizedFileAccess(token=token, expires_at=access.expires_at)


def resolve_resume_original_access(
    session: Session,
    *,
    settings: AppSettings,
    opaque_token: str,
    actor_user_id: str | None,
    session_nonce: str,
) -> ResolvedFileAccess:
    """Validate one opaque grant without adding another audit event."""

    if not opaque_token or not session_nonce:
        raise CandidateDataLifecycleError("candidate_data_file_access_not_found")
    grant = session.scalar(
        select(CandidateDataFileAccessGrant).where(
            CandidateDataFileAccessGrant.token_digest == _digest(opaque_token),
            CandidateDataFileAccessGrant.resource_type == "resume_original",
            CandidateDataFileAccessGrant.actor_user_id == actor_user_id,
            CandidateDataFileAccessGrant.session_nonce_digest == _digest(session_nonce),
            CandidateDataFileAccessGrant.revoked_at.is_(None),
        )
    )
    if grant is None or as_utc(grant.expires_at) is None or as_utc(grant.expires_at) <= utcnow():
        raise CandidateDataLifecycleError("candidate_data_file_access_not_found")
    resume = _visible_resume(session, resume_id=grant.resource_id)
    if (
        grant.resource_lifecycle_version is None
        or grant.resource_lifecycle_version != resume.lifecycle_version
    ):
        raise CandidateDataLifecycleError("candidate_data_file_access_not_found")
    try:
        path = resolve_uploaded_resume_path(
            settings,
            storage_key=resume.storage_key,
            organization_id=resume.organization_id,
        )
    except ResumeServiceError as exc:
        raise CandidateDataLifecycleError("resume_original_file_not_found") from exc
    if grant.purpose not in _FILE_ACCESS_PURPOSES:
        raise CandidateDataLifecycleError("candidate_data_file_access_not_found")
    return ResolvedFileAccess(
        path=path,
        original_filename=resume.original_filename,
        purpose=grant.purpose,  # type: ignore[arg-type]
    )


def _revoke_resume_access_grants(
    session: Session,
    *,
    resume_ids: Iterable[str],
    now: datetime,
) -> None:
    identifiers = tuple(sorted({identifier for identifier in resume_ids if identifier}))
    if not identifiers:
        return
    session.execute(
        update(CandidateDataFileAccessGrant)
        .where(
            CandidateDataFileAccessGrant.organization_id == organization_context_id(session),
            CandidateDataFileAccessGrant.resource_type == "resume_original",
            CandidateDataFileAccessGrant.resource_id.in_(identifiers),
            CandidateDataFileAccessGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


def _revoke_workspace_candidate_exports(
    session: Session,
    *,
    organization_id: str,
    now: datetime,
) -> None:
    """Fail closed for exports when any included candidate data is deleted.

    Snapshot JSON intentionally contains only opaque IDs and is portable
    across SQLite/PostgreSQL, so querying its nested values precisely would
    introduce divergent privacy behavior.  Revoking the workspace's active
    exports is conservative but guarantees that a completed archive cannot
    outlive a fresh deletion request.
    """

    session.execute(
        update(CandidateDataExport)
        .where(
            CandidateDataExport.organization_id == organization_id,
            CandidateDataExport.status.in_(("queued", "running", "completed")),
            CandidateDataExport.revoked_at.is_(None),
        )
        .values(revoked_at=now, status="revoked")
        .execution_options(synchronize_session=False)
    )


def _cancel_resume_async_work(
    session: Session,
    *,
    resume_ids: Iterable[str],
    now: datetime,
) -> None:
    identifiers = tuple(sorted({identifier for identifier in resume_ids if identifier}))
    if not identifiers:
        return
    organization_id = organization_context_id(session)
    session.execute(
        update(ResumeAiExtractionJob)
        .where(
            ResumeAiExtractionJob.organization_id == organization_id,
            ResumeAiExtractionJob.resume_id.in_(identifiers),
            ResumeAiExtractionJob.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error="candidate_data_deleted",
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(ResumeSummaryJob)
        .where(
            ResumeSummaryJob.organization_id == organization_id,
            ResumeSummaryJob.resume_id.in_(identifiers),
            ResumeSummaryJob.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            next_attempt_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error="candidate_data_deleted",
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    for item_model in (ResumeScoreBatchItem, JobMatchBatchItem):
        session.execute(
            update(item_model)
            .where(
                item_model.organization_id == organization_id,
                item_model.resume_id.in_(identifiers),
                item_model.status.in_(("queued", "running")),
            )
            .values(
                status="cancelled",
                next_attempt_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error="candidate_data_deleted",
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )


def _tombstone_secret(settings: AppSettings) -> bytes:
    if settings.candidate_data_tombstone_secret:
        return settings.candidate_data_tombstone_secret.encode("utf-8")
    # Reuse the already-required server-side session secret only with an
    # explicit domain separator in ``_mailbox_tombstone_digest``.  This keeps
    # the approved API/worker deployment topology functional without adding a
    # second Compose secret mapping.  Operators can still set the dedicated
    # value above for cryptographic key separation.
    if settings.session_secret:
        return settings.session_secret.encode("utf-8")
    # Local/test instances may intentionally have neither production secret.
    # Production without either key remains fail-closed.
    if settings.environment in {"production", "prod"}:
        raise CandidateDataLifecycleError("candidate_data_tombstone_secret_not_configured")
    return (
        settings.session_secret
        or settings.admin_token
        or "resume-v3-development-candidate-data-tombstone"
    ).encode("utf-8")


def _mailbox_tombstone_digest(
    *,
    settings: AppSettings,
    organization_id: str,
    attachment_sha256: str,
) -> str:
    message = _TOMBSTONE_DOMAIN + organization_id.encode("utf-8") + b"\x00" + attachment_sha256.encode("ascii")
    return hmac.new(_tombstone_secret(settings), message, hashlib.sha256).hexdigest()


def _record_mailbox_tombstones(
    session: Session,
    *,
    settings: AppSettings,
    resume_ids: Iterable[str],
    deletion_batch_id: str,
    now: datetime,
) -> None:
    identifiers = tuple(sorted({identifier for identifier in resume_ids if identifier}))
    if not identifiers:
        return
    imports = session.scalars(
        select(EmailAttachmentImport).where(EmailAttachmentImport.resume_id.in_(identifiers))
    ).all()
    organization_id = organization_context_id(session)
    for attachment_import in imports:
        digest = _mailbox_tombstone_digest(
            settings=settings,
            organization_id=organization_id,
            attachment_sha256=attachment_import.attachment_sha256,
        )
        record = session.scalar(
            select(MailboxDeletedAttachmentTombstone).where(
                MailboxDeletedAttachmentTombstone.digest == digest,
                MailboxDeletedAttachmentTombstone.key_version == "v1",
            )
        )
        if record is None:
            session.add(
                MailboxDeletedAttachmentTombstone(
                    organization_id=organization_id,
                    digest=digest,
                    key_version="v1",
                    deletion_batch_id=deletion_batch_id,
                    # Tombstones are intentionally finite.  A fresh manual
                    # upload is never blocked, and a later inbox submission
                    # can be reassessed after this bounded anti-replay window.
                    expires_at=now + timedelta(days=365),
                )
            )


def mailbox_attachment_is_tombstoned(
    session: Session,
    *,
    settings: AppSettings,
    attachment_sha256: str,
) -> bool:
    """Return whether automatic mailbox ingestion must not recreate bytes.

    This intentionally operates on a keyed digest, never on the raw content
    hash.  It is not used by manual browser upload, so an administrator can
    later make an explicit, informed re-upload decision.
    """

    if len(attachment_sha256) != 64:
        return False
    digest = _mailbox_tombstone_digest(
        settings=settings,
        organization_id=organization_context_id(session),
        attachment_sha256=attachment_sha256,
    )
    now = utcnow()
    return (
        session.scalar(
            select(MailboxDeletedAttachmentTombstone.id).where(
                MailboxDeletedAttachmentTombstone.digest == digest,
                MailboxDeletedAttachmentTombstone.key_version == "v1",
                or_(
                    MailboxDeletedAttachmentTombstone.expires_at.is_(None),
                    MailboxDeletedAttachmentTombstone.expires_at > now,
                ),
            )
        )
        is not None
    )


def _replacement_ready_resume(session: Session, *, candidate_id: str) -> Resume | None:
    return session.scalar(
        select(Resume)
        .where(
            Resume.candidate_id == candidate_id,
            Resume.extraction_status == "ready",
        )
        .order_by(Resume.created_at.desc(), Resume.id.desc())
    )


def _new_deletion_batch(
    session: Session,
    *,
    trigger_type: str,
    reason: str,
    private_note: str | None,
    actor_user_id: str | None,
    now: datetime,
    recovery_days: int,
) -> CandidateDataDeletionBatch:
    if reason not in _DELETION_REASONS:
        raise CandidateDataLifecycleError("candidate_data_deletion_reason_invalid")
    recovery_deadline = now + timedelta(days=recovery_days)
    batch = CandidateDataDeletionBatch(
        organization_id=organization_context_id(session),
        requested_by_user_id=actor_user_id,
        trigger_type=trigger_type,
        reason=reason,
        # ``other`` still requires an explanatory input at the API boundary
        # so deletion is intentional, but free-form text may itself contain
        # personal data.  Keep only the controlled reason code in durable
        # lifecycle/audit records.
        private_note=None,
        status="deleted",
        recovery_deadline_at=recovery_deadline,
        purge_after_at=recovery_deadline,
    )
    session.add(batch)
    session.flush()
    return batch


def _deletion_response(
    batch: CandidateDataDeletionBatch,
    *,
    candidate_count: int,
    resume_count: int,
) -> CandidateDataDeletionResponse:
    return CandidateDataDeletionResponse(
        deletion_batch_id=batch.id,
        recovery_deadline_at=batch.recovery_deadline_at,
        purge_after_at=batch.purge_after_at,
        affected_candidate_count=candidate_count,
        affected_resume_count=resume_count,
    )


def _assert_current_retention_eligibility(
    session: Session,
    *,
    candidate: Candidate,
    resumes: list[Resume],
    retention_days: int | None,
    retention_policy_version: int | None,
    now: datetime,
) -> None:
    """Fence automatic deletion against later policy/hold/data changes.

    The worker's initial scan is only a worklist.  This check runs under the
    current policy and candidate/resume locks immediately before the root CAS,
    so enabling a hold, switching back to manual, or adding a newer resume
    cannot be ignored because it happened after that scan.
    """

    if retention_days is None or retention_policy_version is None:
        raise CandidateDataLifecycleError("candidate_data_retention_policy_changed")
    policy = session.scalar(
        select(CandidateDataRetentionPolicy)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        policy is None
        or policy.mode != "automatic"
        or policy.retention_days != retention_days
        or policy.version != retention_policy_version
    ):
        raise CandidateDataLifecycleError("candidate_data_retention_policy_changed")
    if candidate.retention_hold or any(resume.retention_hold for resume in resumes):
        raise CandidateDataLifecycleError("candidate_data_retention_hold")
    cutoff = now - timedelta(days=retention_days)
    if not resumes or any((as_utc(resume.created_at) or now) > cutoff for resume in resumes):
        raise CandidateDataLifecycleError("candidate_data_retention_not_eligible")


def delete_resume(
    session: Session,
    *,
    settings: AppSettings,
    resume_id: str,
    actor_user_id: str | None,
    reason: str,
    private_note: str | None,
    request_id: str | None = None,
    source_kind: str = "web",
) -> CandidateDataDeletionResponse:
    """Logically delete exactly one resume version and revoke access now."""

    # A savepoint keeps a raced request from leaving a half-created batch in a
    # retention run that continues to process other candidates.
    with session.begin_nested():
        now = utcnow()
        initial_resume = _visible_resume(session, resume_id=resume_id)
        candidate = _visible_candidate(
            session,
            candidate_id=initial_resume.candidate_id,
            for_update=True,
        )
        resume = _visible_resume(session, resume_id=resume_id, for_update=True)
        was_active = resume.is_active
        organization_id = resume.organization_id
        candidate_id = candidate.id
        batch = _new_deletion_batch(
            session,
            trigger_type="manual_resume",
            reason=reason,
            private_note=private_note,
            actor_user_id=actor_user_id,
            now=now,
            recovery_days=settings.candidate_data_recovery_days,
        )
        if not _claim_live_resume_for_deletion(
            session,
            resume=resume,
            deletion_batch_id=batch.id,
            actor_user_id=actor_user_id,
            purge_after_at=batch.purge_after_at,
            now=now,
            require_visible_candidate=True,
        ):
            raise CandidateDataLifecycleError("resume_not_found")
        session.add(
            CandidateDataDeletionBatchItem(
                organization_id=organization_id,
                deletion_batch_id=batch.id,
                candidate_id=candidate_id,
                resume_id=resume.id,
                was_active=was_active,
            )
        )
        _record_mailbox_tombstones(
            session,
            settings=settings,
            resume_ids=(resume.id,),
            deletion_batch_id=batch.id,
            now=now,
        )
        if was_active:
            replacement = _replacement_ready_resume(session, candidate_id=candidate_id)
            if replacement is not None:
                replacement.is_active = True
        _cancel_resume_async_work(session, resume_ids=(resume.id,), now=now)
        _revoke_resume_access_grants(session, resume_ids=(resume.id,), now=now)
        _revoke_workspace_candidate_exports(
            session,
            organization_id=organization_id,
            now=now,
        )
        session.add(
            CandidateDataPurgeJob(
                organization_id=organization_id,
                deletion_batch_id=batch.id,
                status="queued",
                next_attempt_at=batch.purge_after_at,
            )
        )
        _record_audit(
            session,
            actor_user_id=actor_user_id,
            actor_kind="user" if actor_user_id else "worker",
            action="resume_delete_requested",
            target_type="resume",
            target_id=resume.id,
            candidate_id=candidate_id,
            resume_id=resume.id,
            request_id=request_id,
            source_kind=source_kind,
            reason_code=reason,
        )
        session.flush()
        return _deletion_response(batch, candidate_count=0, resume_count=1)


def delete_candidate(
    session: Session,
    *,
    settings: AppSettings,
    candidate_id: str,
    actor_user_id: str | None,
    reason: str,
    private_note: str | None,
    request_id: str | None = None,
    source_kind: str = "web",
    retention_days: int | None = None,
    retention_policy_version: int | None = None,
) -> CandidateDataDeletionResponse:
    """Logically delete a candidate and every currently visible version."""

    # See ``delete_resume``: a failed conditional claim must roll back only
    # this candidate's batch, not earlier work in the same scheduled run.
    with session.begin_nested():
        now = utcnow()
        candidate = _visible_candidate(session, candidate_id=candidate_id, for_update=True)
        resumes = _locked_visible_candidate_resumes(session, candidate_id=candidate.id)
        if not resumes:
            raise CandidateDataLifecycleError("candidate_has_no_resume")
        if reason == "retention_expired":
            _assert_current_retention_eligibility(
                session,
                candidate=candidate,
                resumes=resumes,
                retention_days=retention_days,
                retention_policy_version=retention_policy_version,
                now=now,
            )
        organization_id = candidate.organization_id
        batch = _new_deletion_batch(
            session,
            trigger_type="manual_candidate" if source_kind == "web" else "retention",
            reason=reason,
            private_note=private_note,
            actor_user_id=actor_user_id,
            now=now,
            recovery_days=settings.candidate_data_recovery_days,
        )
        if not _claim_live_candidate_for_deletion(
            session,
            candidate=candidate,
            deletion_batch_id=batch.id,
            actor_user_id=actor_user_id,
            purge_after_at=batch.purge_after_at,
            now=now,
        ):
            raise CandidateDataLifecycleError("candidate_not_found")
        for resume in resumes:
            if not _claim_live_resume_for_deletion(
                session,
                resume=resume,
                deletion_batch_id=batch.id,
                actor_user_id=actor_user_id,
                purge_after_at=batch.purge_after_at,
                now=now,
                require_visible_candidate=False,
            ):
                raise CandidateDataLifecycleError("candidate_data_delete_conflict")
        for resume in resumes:
            session.add(
                CandidateDataDeletionBatchItem(
                    organization_id=organization_id,
                    deletion_batch_id=batch.id,
                    candidate_id=candidate.id,
                    resume_id=resume.id,
                    was_active=resume.is_active,
                )
            )
        resume_ids = tuple(resume.id for resume in resumes)
        _record_mailbox_tombstones(
            session,
            settings=settings,
            resume_ids=resume_ids,
            deletion_batch_id=batch.id,
            now=now,
        )
        _cancel_resume_async_work(session, resume_ids=resume_ids, now=now)
        _revoke_resume_access_grants(session, resume_ids=resume_ids, now=now)
        # A completed export may include any candidate snapshot in this workspace.
        # Revoke it conservatively; a stale export is never allowed to bypass a
        # freshly requested deletion.
        _revoke_workspace_candidate_exports(
            session,
            organization_id=organization_id,
            now=now,
        )
        session.add(
            CandidateDataPurgeJob(
                organization_id=organization_id,
                deletion_batch_id=batch.id,
                status="queued",
                next_attempt_at=batch.purge_after_at,
            )
        )
        _record_audit(
            session,
            actor_user_id=actor_user_id,
            actor_kind="user" if actor_user_id else "worker",
            action="candidate_delete_requested",
            target_type="candidate",
            target_id=candidate.id,
            candidate_id=candidate.id,
            request_id=request_id,
            source_kind=source_kind,
            reason_code=reason,
        )
        session.flush()
        return _deletion_response(batch, candidate_count=1, resume_count=len(resumes))


def restore_deletion_batch(
    session: Session,
    *,
    deletion_batch_id: str,
    actor_user_id: str | None,
    request_id: str | None = None,
) -> CandidateDataRestoreResponse:
    """Restore only roots affected by this still-recoverable batch."""

    now = utcnow()
    # Move through an explicit in-transaction fence before reading any roots.
    # A purge worker has an inverse ``deleted -> purging`` CAS and is required
    # to win that fence before it touches files.  Thus neither side can unlink
    # an original that the other side has successfully restored.
    claimed = session.execute(
        update(CandidateDataDeletionBatch)
        .where(
            CandidateDataDeletionBatch.id == deletion_batch_id,
            CandidateDataDeletionBatch.organization_id == organization_context_id(session),
            CandidateDataDeletionBatch.status == "deleted",
            CandidateDataDeletionBatch.recovery_deadline_at > now,
        )
        .values(status="restoring", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        session.expire_all()
        batch = session.scalar(
            select(CandidateDataDeletionBatch)
            .where(CandidateDataDeletionBatch.id == deletion_batch_id)
            .execution_options(populate_existing=True)
        )
        if batch is None:
            raise CandidateDataLifecycleError("candidate_data_deletion_batch_not_found")
        deadline = as_utc(batch.recovery_deadline_at)
        if batch.status == "deleted" and (deadline is None or deadline <= now):
            raise CandidateDataLifecycleError("candidate_data_recovery_window_closed")
        raise CandidateDataLifecycleError("candidate_data_deletion_batch_not_restorable")
    batch = session.scalar(
        select(CandidateDataDeletionBatch)
        .where(CandidateDataDeletionBatch.id == deletion_batch_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if batch is None:
        raise CandidateDataLifecycleError("candidate_data_deletion_batch_not_found")
    items = session.scalars(
        select(CandidateDataDeletionBatchItem)
        .where(CandidateDataDeletionBatchItem.deletion_batch_id == batch.id)
        .order_by(CandidateDataDeletionBatchItem.created_at, CandidateDataDeletionBatchItem.id)
    ).all()
    resume_ids = tuple(item.resume_id for item in items)
    candidate_ids = tuple(sorted({item.candidate_id for item in items}))
    resumes = session.scalars(
        _lifecycle_statement(
            select(Resume).where(
                Resume.id.in_(resume_ids),
                Resume.deletion_batch_id == batch.id,
            )
        )
    ).all()
    candidates = session.scalars(
        _lifecycle_statement(
            select(Candidate).where(
                Candidate.id.in_(candidate_ids),
                Candidate.deletion_batch_id == batch.id,
            )
        )
    ).all()
    # Keep restored resumes inactive until their roots are visible again.  The
    # previous active snapshot below then chooses a safe active version only
    # when that candidate has no version selected during the recovery window.
    for resume in resumes:
        resume.is_active = False
    session.flush()
    for row in (*candidates, *resumes):
        row.deleted_at = None
        row.deleted_by_user_id = None
        row.deletion_batch_id = None
        row.purge_after_at = None
    session.flush()
    active_resume_ids = {item.resume_id for item in items if item.was_active}
    for candidate_id in candidate_ids:
        current_active = session.scalar(
            select(Resume).where(
                Resume.candidate_id == candidate_id,
                Resume.is_active.is_(True),
            )
        )
        if current_active is not None:
            continue
        restore_active = next(
            (
                resume
                for resume in resumes
                if resume.candidate_id == candidate_id
                and resume.id in active_resume_ids
                and resume.extraction_status == "ready"
            ),
            None,
        )
        if restore_active is not None:
            restore_active.is_active = True
    batch.status = "restored"
    batch.restored_at = now
    batch.restored_by_user_id = actor_user_id
    batch.updated_at = now
    purge_job = session.scalar(
        select(CandidateDataPurgeJob).where(
            CandidateDataPurgeJob.deletion_batch_id == batch.id
        ).with_for_update()
    )
    if purge_job is not None and purge_job.status in {"queued", "running", "retryable_failed"}:
        purge_job.status = "cancelled"
        purge_job.next_attempt_at = None
        purge_job.lease_owner = None
        purge_job.lease_expires_at = None
        purge_job.completed_at = now
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "worker",
        action=(
            "candidate_restored"
            if batch.trigger_type in {"manual_candidate", "retention"}
            else "resume_restored"
        ),
        target_type="deletion_batch",
        target_id=batch.id,
        request_id=request_id,
        reason_code=batch.reason,
    )
    session.flush()
    return CandidateDataRestoreResponse(
        deletion_batch_id=batch.id,
        restored_candidate_count=len(candidates),
        restored_resume_count=len(resumes),
        restored_at=now,
    )


def list_candidate_data_deletions(
    session: Session,
    *,
    limit: int = 50,
) -> CandidateDataDeletionBatchListResponse:
    """List recovery metadata without reintroducing deleted candidate data.

    The recovery console needs a durable restore target, but intentionally
    receives only opaque batch identifiers, timing and counts.  Candidate
    names, filenames and free-form deletion notes are never projected here.
    """

    bounded_limit = min(max(limit, 1), 100)
    batches = session.scalars(
        select(CandidateDataDeletionBatch)
        .order_by(
            CandidateDataDeletionBatch.created_at.desc(),
            CandidateDataDeletionBatch.id.desc(),
        )
        .limit(bounded_limit)
    ).all()
    total = int(
        session.scalar(select(func.count(CandidateDataDeletionBatch.id))) or 0
    )
    if not batches:
        return CandidateDataDeletionBatchListResponse(items=[], total=total)

    batch_ids = tuple(batch.id for batch in batches)
    count_rows = session.execute(
        select(
            CandidateDataDeletionBatchItem.deletion_batch_id,
            func.count(CandidateDataDeletionBatchItem.resume_id),
            func.count(func.distinct(CandidateDataDeletionBatchItem.candidate_id)),
        )
        .where(CandidateDataDeletionBatchItem.deletion_batch_id.in_(batch_ids))
        .group_by(CandidateDataDeletionBatchItem.deletion_batch_id)
    ).all()
    counts = {
        str(batch_id): (int(resume_count), int(candidate_count))
        for batch_id, resume_count, candidate_count in count_rows
    }
    now = utcnow()
    return CandidateDataDeletionBatchListResponse(
        items=[
            CandidateDataDeletionBatchResponse(
                deletion_batch_id=batch.id,
                trigger_type=batch.trigger_type,
                reason=batch.reason,  # type: ignore[arg-type]
                status=batch.status,
                recovery_deadline_at=batch.recovery_deadline_at,
                purge_after_at=batch.purge_after_at,
                affected_candidate_count=counts.get(batch.id, (0, 0))[1],
                affected_resume_count=counts.get(batch.id, (0, 0))[0],
                restorable=(
                    batch.status == "deleted"
                    and (as_utc(batch.recovery_deadline_at) or now) > now
                ),
                restored_at=batch.restored_at,
                purged_at=batch.purged_at,
            )
            for batch in batches
        ],
        total=total,
    )


def set_candidate_retention_hold(
    session: Session,
    *,
    candidate_id: str,
    retention_hold: bool,
    actor_user_id: str | None,
    request_id: str | None = None,
) -> None:
    with session.begin_nested():
        candidate = _visible_candidate(
            session,
            candidate_id=candidate_id,
            for_update=True,
        )
        resumes = _locked_visible_candidate_resumes(session, candidate_id=candidate.id)
        candidate.retention_hold = retention_hold
        for resume in resumes:
            resume.retention_hold = retention_hold
        _record_audit(
            session,
            actor_user_id=actor_user_id,
            actor_kind="user" if actor_user_id else "worker",
            action="candidate_retention_hold_changed",
            target_type="candidate",
            target_id=candidate.id,
            candidate_id=candidate.id,
            request_id=request_id,
            result="updated",
            reason_code="hold_enabled" if retention_hold else "hold_removed",
        )
        session.flush()


def _retention_policy(
    session: Session,
    *,
    for_update: bool = False,
    populate_existing: bool = False,
) -> CandidateDataRetentionPolicy:
    statement = select(CandidateDataRetentionPolicy)
    if populate_existing:
        statement = statement.execution_options(populate_existing=True)
    if for_update:
        statement = statement.with_for_update()
    policy = session.scalar(statement)
    if policy is not None:
        return policy
    policy = CandidateDataRetentionPolicy(
        organization_id=organization_context_id(session),
        mode="manual",
        retention_days=None,
        version=1,
    )
    session.add(policy)
    session.flush()
    return policy


def retention_policy_response(session: Session) -> CandidateDataRetentionPolicyResponse:
    policy = _retention_policy(session)
    return CandidateDataRetentionPolicyResponse(
        mode=policy.mode,  # type: ignore[arg-type]
        retention_days=policy.retention_days,
        version=policy.version,
        updated_at=policy.updated_at,
    )


def _retention_preview_token(
    *,
    settings: AppSettings,
    organization_id: str,
    policy_version: int,
    retention_days: int,
) -> str:
    secret = (
        settings.session_secret
        or settings.admin_token
        or "resume-v3-development-retention-preview"
    ).encode("utf-8")
    payload = f"candidate-retention-preview:v1:{organization_id}:{policy_version}:{retention_days}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _eligible_retention_candidate_ids(
    session: Session,
    *,
    retention_days: int,
    now: datetime,
) -> list[str]:
    cutoff = now - timedelta(days=retention_days)
    rows = session.scalars(
        select(Candidate.id)
        .join(Resume, Resume.candidate_id == Candidate.id)
        .where(Candidate.retention_hold.is_(False))
        .group_by(Candidate.id)
        .having(func.max(Resume.created_at) <= cutoff)
        .order_by(func.max(Resume.created_at), Candidate.id)
    ).all()
    return list(rows)


def preview_retention_policy(
    session: Session,
    *,
    settings: AppSettings,
    retention_days: int,
) -> CandidateDataRetentionPreviewResponse:
    if not 30 <= retention_days <= 3650:
        raise CandidateDataLifecycleError("candidate_data_retention_days_invalid")
    now = utcnow()
    # Preview must remain genuinely side-effect free.  Production migrations
    # seed this row for every existing workspace, but a newly created local
    # test/database can legitimately not have it yet.  Treat that case as the
    # same manual v1 default without inserting anything.
    policy = session.scalar(select(CandidateDataRetentionPolicy))
    policy_version = policy.version if policy is not None else 1
    eligible_ids = _eligible_retention_candidate_ids(
        session,
        retention_days=retention_days,
        now=now,
    )
    eligible_resume_count = int(
        session.scalar(
            select(func.count(Resume.id)).where(Resume.candidate_id.in_(eligible_ids))
        )
        or 0
    ) if eligible_ids else 0
    held_candidate_count = int(
        session.scalar(
            select(func.count(Candidate.id)).where(Candidate.retention_hold.is_(True))
        )
        or 0
    )
    already_deleted_count = int(
        session.scalar(
            _lifecycle_statement(
                select(func.count(Candidate.id)).where(Candidate.deleted_at.is_not(None))
            )
        )
        or 0
    )
    return CandidateDataRetentionPreviewResponse(
        preview_token=_retention_preview_token(
            settings=settings,
            organization_id=organization_context_id(session),
            policy_version=policy_version,
            retention_days=retention_days,
        ),
        policy_version=policy_version,
        retention_days=retention_days,
        eligible_candidate_count=len(eligible_ids),
        eligible_resume_count=eligible_resume_count,
        held_candidate_count=held_candidate_count,
        already_deleted_count=already_deleted_count,
        calculated_at=now,
    )


def update_retention_policy(
    session: Session,
    *,
    settings: AppSettings,
    mode: str,
    retention_days: int | None,
    preview_token: str | None,
    actor_user_id: str | None,
    request_id: str | None = None,
) -> CandidateDataRetentionPolicyResponse:
    policy = _retention_policy(
        session,
        for_update=True,
        populate_existing=True,
    )
    if mode not in {"manual", "automatic"}:
        raise CandidateDataLifecycleError("candidate_data_retention_policy_invalid")
    if mode == "manual":
        if retention_days is not None:
            raise CandidateDataLifecycleError("candidate_data_retention_policy_invalid")
    else:
        if retention_days is None or not 30 <= retention_days <= 3650:
            raise CandidateDataLifecycleError("candidate_data_retention_policy_invalid")
        expected = _retention_preview_token(
            settings=settings,
            organization_id=organization_context_id(session),
            policy_version=policy.version,
            retention_days=retention_days,
        )
        if not preview_token or not hmac.compare_digest(preview_token, expected):
            raise CandidateDataLifecycleError("candidate_data_retention_preview_stale")
    policy.mode = mode
    policy.retention_days = retention_days if mode == "automatic" else None
    policy.version += 1
    policy.updated_by_user_id = actor_user_id
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "worker",
        action="retention_policy_changed",
        target_type="retention_policy",
        target_id=policy.id,
        request_id=request_id,
        result="updated",
        reason_code=mode,
    )
    session.flush()
    return CandidateDataRetentionPolicyResponse(
        mode=policy.mode,  # type: ignore[arg-type]
        retention_days=policy.retention_days,
        version=policy.version,
        updated_at=policy.updated_at,
    )


def _cleanup_run_response(
    run: CandidateDataRetentionCleanupRun,
) -> CandidateDataRetentionCleanupRunResponse:
    return CandidateDataRetentionCleanupRunResponse(
        run_id=run.id,
        trigger_type=run.trigger_type,  # type: ignore[arg-type]
        status=run.status,
        policy_version=run.policy_version,
        retention_days=run.retention_days,
        started_at=run.started_at,
        finished_at=run.finished_at,
        scanned_count=run.scanned_count,
        queued_count=run.queued_count,
        skipped_hold_count=run.skipped_hold_count,
        failed_count=run.failed_count,
        error_code=run.error_code,
    )


def run_retention_cleanup(
    session: Session,
    *,
    settings: AppSettings,
    trigger_type: Literal["manual", "scheduled"],
    actor_user_id: str | None = None,
) -> CandidateDataRetentionCleanupRunResponse:
    """Enqueue bounded reversible deletions; never unlink in an HTTP call."""

    policy = _retention_policy(
        session,
        for_update=True,
        populate_existing=True,
    )
    now = utcnow()
    run = CandidateDataRetentionCleanupRun(
        organization_id=organization_context_id(session),
        trigger_type=trigger_type,
        policy_version=policy.version,
        retention_days=policy.retention_days,
        status="running",
        started_at=now,
    )
    session.add(run)
    session.flush()
    if policy.mode != "automatic" or policy.retention_days is None:
        run.status = "completed"
        run.finished_at = utcnow()
        session.flush()
        return _cleanup_run_response(run)

    candidate_ids = _eligible_retention_candidate_ids(
        session,
        retention_days=policy.retention_days,
        now=now,
    )[:100]
    run.scanned_count = len(candidate_ids)
    queued = failed = skipped_hold = 0
    for candidate_id in candidate_ids:
        try:
            delete_candidate(
                session,
                settings=settings,
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
                reason="retention_expired",
                private_note=None,
                source_kind="worker" if trigger_type == "scheduled" else "web",
                retention_days=policy.retention_days,
                retention_policy_version=policy.version,
            )
            queued += 1
        except CandidateDataLifecycleError as exc:
            if str(exc) in {
                "candidate_data_retention_hold",
                "candidate_data_retention_not_eligible",
                "candidate_data_retention_policy_changed",
                "candidate_not_found",
                "candidate_has_no_resume",
            }:
                skipped_hold += 1
            else:
                failed += 1
    run.queued_count = queued
    run.skipped_hold_count = skipped_hold
    run.failed_count = failed
    run.status = "completed_with_errors" if failed else "completed"
    run.error_code = "candidate_data_retention_partial_failure" if failed else None
    run.finished_at = utcnow()
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="worker" if trigger_type == "scheduled" else "user",
        action="retention_cleanup_completed",
        target_type="retention_cleanup_run",
        target_id=run.id,
        source_kind="worker" if trigger_type == "scheduled" else "web",
        result=run.status,
    )
    session.flush()
    return _cleanup_run_response(run)


def list_retention_cleanup_runs(
    session: Session,
    *,
    limit: int = 20,
) -> CandidateDataRetentionCleanupRunHistoryResponse:
    bounded_limit = min(max(limit, 1), 100)
    runs = session.scalars(
        select(CandidateDataRetentionCleanupRun)
        .order_by(
            CandidateDataRetentionCleanupRun.started_at.desc(),
            CandidateDataRetentionCleanupRun.id.desc(),
        )
        .limit(bounded_limit)
    ).all()
    total = int(session.scalar(select(func.count(CandidateDataRetentionCleanupRun.id))) or 0)
    return CandidateDataRetentionCleanupRunHistoryResponse(
        items=[_cleanup_run_response(run) for run in runs],
        total=total,
    )


def list_candidate_data_audit_events(
    session: Session,
    *,
    limit: int = 100,
) -> CandidateDataAuditEventListResponse:
    bounded_limit = min(max(limit, 1), 200)
    events = session.scalars(
        select(CandidateDataAuditEvent)
        .order_by(CandidateDataAuditEvent.created_at.desc(), CandidateDataAuditEvent.id.desc())
        .limit(bounded_limit)
    ).all()
    total = int(session.scalar(select(func.count(CandidateDataAuditEvent.id))) or 0)
    return CandidateDataAuditEventListResponse(
        items=[
            CandidateDataAuditEventResponse(
                event_id=event.id,
                actor_user_id=event.actor_user_id,
                actor_kind=event.actor_kind,
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                result=event.result,
                reason_code=event.reason_code,
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total,
    )


def run_due_candidate_data_retention_cleanup(
    database: Database,
    *,
    settings: AppSettings,
) -> bool:
    """Run one due automatic workspace retention evaluation.

    This worker-owned scheduler deliberately claims no candidate row globally.
    It first finds one policy, then installs that policy's verified workspace
    context before reading candidates or enqueuing deletion batches.
    """

    now = utcnow()
    interval = timedelta(
        seconds=settings.candidate_data_lifecycle_cleanup_interval_seconds
    )
    with database.session_factory() as session:
        policies = session.execute(
            select(
                CandidateDataRetentionPolicy.id,
                CandidateDataRetentionPolicy.organization_id,
            )
            .where(
                CandidateDataRetentionPolicy.mode == "automatic",
                CandidateDataRetentionPolicy.retention_days.is_not(None),
            )
            .order_by(
                CandidateDataRetentionPolicy.updated_at.asc(),
                CandidateDataRetentionPolicy.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        ).all()
        for policy_id, organization_id in policies:
            if not organization_id:
                continue
            set_organization_context(session, organization_id)
            try:
                # Re-load under the workspace criterion and lock the policy.
                # ``populate_existing`` is required because the initial
                # global scheduler scan may otherwise leave a stale automatic
                # ORM object in this session after an administrator switches
                # the workspace back to manual mode.
                policy = session.scalar(
                    select(CandidateDataRetentionPolicy)
                    .where(CandidateDataRetentionPolicy.id == policy_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if (
                    policy is None
                    or policy.mode != "automatic"
                    or policy.retention_days is None
                ):
                    session.rollback()
                    continue
                last_run_at = session.scalar(
                    select(func.max(CandidateDataRetentionCleanupRun.started_at)).where(
                        CandidateDataRetentionCleanupRun.organization_id == organization_id,
                        CandidateDataRetentionCleanupRun.trigger_type == "scheduled",
                    )
                )
                if (
                    last_run_at is not None
                    and (as_utc(last_run_at) or now) + interval > now
                ):
                    session.rollback()
                    continue
                run_retention_cleanup(
                    session,
                    settings=settings,
                    trigger_type="scheduled",
                    actor_user_id=None,
                )
                session.commit()
                return True
            finally:
                clear_organization_context(session)
        session.commit()
    return False


__all__ = [
    "AuthorizedFileAccess",
    "CandidateDataLifecycleError",
    "ResolvedFileAccess",
    "authorize_resume_original_access",
    "delete_candidate",
    "delete_resume",
    "list_candidate_data_deletions",
    "list_candidate_data_audit_events",
    "list_retention_cleanup_runs",
    "preview_retention_policy",
    "mailbox_attachment_is_tombstoned",
    "resolve_resume_original_access",
    "restore_deletion_batch",
    "retention_policy_response",
    "run_due_candidate_data_retention_cleanup",
    "run_retention_cleanup",
    "set_candidate_retention_hold",
    "update_retention_policy",
]
