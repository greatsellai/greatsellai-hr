"""Workspace feedback questionnaires with server-controlled quota rewards.

The service deliberately separates two transactions:

* submitting a complete questionnaire atomically reserves the workspace's
  eight-hour cooldown and persists one queued reward; and
* a leased worker later grants the fixed allowance in a database-only
  transaction after the server-side review queue is due.

No feedback text is logged, sent to an AI provider, or copied into generic
platform audit snapshots.  Image bytes are outside this module: callers pass
metadata only after a trusted upload boundary has validated and stored them.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re
import secrets

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import Database
from app.models import (
    Organization,
    UserAccount,
    WorkspaceFeedbackImageAttachment,
    WorkspaceFeedbackSubmission,
    utcnow,
)
from app.schemas import (
    WorkspaceFeedbackAttachmentResponse,
    WorkspaceFeedbackListResponse,
    WorkspaceFeedbackResponse,
    WorkspaceFeedbackSubmitResponse,
    PlatformWorkspaceFeedbackListResponse,
    PlatformWorkspaceFeedbackResponse,
)
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


WORKSPACE_FEEDBACK_REWARD_CALL_COUNT = 500
WORKSPACE_FEEDBACK_COOLDOWN = timedelta(hours=8)
WORKSPACE_FEEDBACK_REWARD_MIN_DELAY_SECONDS = 5 * 60
WORKSPACE_FEEDBACK_REWARD_MAX_DELAY_SECONDS = 10 * 60
WORKSPACE_FEEDBACK_REWARD_LEASE_SECONDS = 120
WORKSPACE_FEEDBACK_RETRY_DELAY_SECONDS = 60
WORKSPACE_FEEDBACK_MAX_ANSWER_LENGTH = 4_000
WORKSPACE_FEEDBACK_MAX_CONTACT_PHONE_LENGTH = 32
WORKSPACE_FEEDBACK_MAX_IMAGE_ATTACHMENTS = 5
WORKSPACE_FEEDBACK_MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
WORKSPACE_FEEDBACK_ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)

WORKSPACE_FEEDBACK_REWARD_QUEUED = "queued"
WORKSPACE_FEEDBACK_REWARD_RUNNING = "running"
WORKSPACE_FEEDBACK_REWARD_GRANTED = "granted"

_IDEMPOTENCY_KEY_MAX_LENGTH = 255
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTACT_PHONE_CHARACTERS_PATTERN = re.compile(r"^[0-9+()\-\s]+$")


class WorkspaceFeedbackServiceError(RuntimeError):
    """Stable, non-sensitive feedback domain failure."""


class WorkspaceFeedbackCooldownError(WorkspaceFeedbackServiceError):
    """Raised when this workspace has already claimed its current window."""

    def __init__(self, next_submission_at: datetime | None) -> None:
        super().__init__("workspace_feedback_cooldown")
        self.next_submission_at = next_submission_at


class WorkspaceFeedbackIdempotencyConflictError(WorkspaceFeedbackServiceError):
    """A transport retry key cannot silently overwrite earlier feedback."""

    def __init__(self) -> None:
        super().__init__("workspace_feedback_idempotency_key_reused")


@dataclass(frozen=True)
class WorkspaceFeedbackAttachmentInput:
    """Trusted image metadata prepared by the HTTP upload boundary."""

    storage_key: str
    original_filename: str
    content_type: str
    size_bytes: int
    content_sha256: str


@dataclass(frozen=True)
class ClaimedWorkspaceFeedbackReward:
    feedback_id: str
    organization_id: str


def normalize_workspace_feedback_idempotency_key(value: str | None) -> str:
    """Validate a required retry key without retaining its raw value."""

    if value is None:
        raise WorkspaceFeedbackServiceError("workspace_feedback_idempotency_key_required")
    normalized = value.strip()
    if not normalized or len(normalized) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise WorkspaceFeedbackServiceError("invalid_workspace_feedback_idempotency_key")
    return normalized


def submit_workspace_feedback(
    session: Session,
    *,
    organization_id: str,
    submitted_by_user_id: str,
    idempotency_key: str | None,
    use_case: str,
    intended_outcome: str,
    friction: str,
    desired_change: str,
    contact_phone: str,
    attachments: Sequence[WorkspaceFeedbackAttachmentInput] = (),
    now: datetime | None = None,
) -> WorkspaceFeedbackSubmitResponse:
    """Persist a complete questionnaire and atomically reserve its cooldown.

    A successful first submission queues a fixed +500 call reward for
    server-side review processing.  The cooldown update and durable feedback
    row share one transaction, so a rollback cannot consume a user's window
    without a queued reward.  Replaying the same idempotency key returns the
    original submission; using it for different answers or a different contact
    number is rejected.
    """

    expected_organization_id = _require_current_organization(session, organization_id)
    normalized_user_id = _required_identifier(submitted_by_user_id, "workspace_feedback_submitter_required")
    normalized_key = normalize_workspace_feedback_idempotency_key(idempotency_key)
    normalized_use_case = _normalize_answer(use_case, "workspace_feedback_use_case_required")
    normalized_outcome = _normalize_answer(
        intended_outcome,
        "workspace_feedback_intended_outcome_required",
    )
    normalized_friction = _normalize_answer(friction, "workspace_feedback_friction_required")
    normalized_change = _normalize_answer(
        desired_change,
        "workspace_feedback_desired_change_required",
    )
    normalized_contact_phone = _normalize_contact_phone(contact_phone)
    normalized_attachments = _normalize_attachments(attachments)
    key_hash = _hash_text(normalized_key)
    request_fingerprint = _request_fingerprint(
        use_case=normalized_use_case,
        intended_outcome=normalized_outcome,
        friction=normalized_friction,
        desired_change=normalized_change,
        contact_phone=normalized_contact_phone,
        attachments=normalized_attachments,
    )
    current_time = _as_utc(now) or utcnow()

    existing = _feedback_by_idempotency_key(
        session,
        organization_id=expected_organization_id,
        key_hash=key_hash,
    )
    if existing is not None:
        return _replay_response(session, existing=existing, request_fingerprint=request_fingerprint)

    cooldown_until = current_time + WORKSPACE_FEEDBACK_COOLDOWN
    reward_due_at = _random_reward_due_at(current_time)
    # Match the project's established duplicate-race pattern.  Flushing first
    # prevents a caller-owned integrity error from being mistaken for a raced
    # idempotency key, while the nested transaction keeps the cooldown update
    # reversible if the feedback row cannot be created.
    session.flush()
    try:
        with session.begin_nested():
            existing = _feedback_by_idempotency_key(
                session,
                organization_id=expected_organization_id,
                key_hash=key_hash,
            )
            if existing is not None:
                return _replay_response(session, existing=existing, request_fingerprint=request_fingerprint)

            reserved = session.execute(
                update(Organization)
                .where(
                    Organization.id == expected_organization_id,
                    or_(
                        Organization.feedback_reward_available_at.is_(None),
                        Organization.feedback_reward_available_at <= current_time,
                    ),
                )
                .values(
                    feedback_reward_available_at=cooldown_until,
                    updated_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if reserved.rowcount != 1:
                # A concurrent retry using this same idempotency key can have
                # won the cooldown race.  Re-check before surfacing the normal
                # cooldown response so an uncertain browser retry remains
                # idempotent.
                existing = _feedback_by_idempotency_key(
                    session,
                    organization_id=expected_organization_id,
                    key_hash=key_hash,
                )
                if existing is not None:
                    return _replay_response(
                        session,
                        existing=existing,
                        request_fingerprint=request_fingerprint,
                    )
                raise WorkspaceFeedbackCooldownError(
                    _next_submission_at(session, expected_organization_id)
                )

            feedback = WorkspaceFeedbackSubmission(
                organization_id=expected_organization_id,
                submitted_by_user_id=normalized_user_id,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fingerprint,
                use_case=normalized_use_case,
                intended_outcome=normalized_outcome,
                friction=normalized_friction,
                desired_change=normalized_change,
                contact_phone=normalized_contact_phone,
                reward_status=WORKSPACE_FEEDBACK_REWARD_QUEUED,
                reward_call_count=WORKSPACE_FEEDBACK_REWARD_CALL_COUNT,
                reward_due_at=reward_due_at,
            )
            session.add(feedback)
            session.flush([feedback])
            for sort_order, attachment in enumerate(normalized_attachments):
                session.add(
                    WorkspaceFeedbackImageAttachment(
                        organization_id=expected_organization_id,
                        feedback_submission_id=feedback.id,
                        sort_order=sort_order,
                        original_filename=attachment.original_filename,
                        content_type=attachment.content_type,
                        size_bytes=attachment.size_bytes,
                        storage_key=attachment.storage_key,
                        content_sha256=attachment.content_sha256,
                    )
                )
            session.flush()
    except IntegrityError:
        existing = _feedback_by_idempotency_key(
            session,
            organization_id=expected_organization_id,
            key_hash=key_hash,
        )
        if existing is None:
            raise
        return _replay_response(session, existing=existing, request_fingerprint=request_fingerprint)

    session.expire(feedback, ["image_attachments"])
    return WorkspaceFeedbackSubmitResponse(
        item=workspace_feedback_response(feedback),
        next_submission_at=cooldown_until,
        replayed=False,
    )


def list_workspace_feedback(
    session: Session,
    *,
    organization_id: str,
    submitted_by_user_id: str,
    limit: int = 50,
) -> WorkspaceFeedbackListResponse:
    """Return the caller's own feedback in the current workspace.

    Product feedback can include operational details about a teammate's hiring
    process, so the normal workspace endpoint deliberately does not expose a
    whole-workspace feedback feed.  A separately authorized platform-only
    read path may use the raw models under an explicit global scope when that
    is implemented.
    """

    expected_organization_id = _require_current_organization(session, organization_id)
    normalized_submitter_id = _required_identifier(
        submitted_by_user_id,
        "workspace_feedback_submitter_required",
    )
    bounded_limit = max(1, min(int(limit), 100))
    items = session.scalars(
        select(WorkspaceFeedbackSubmission)
        .options(selectinload(WorkspaceFeedbackSubmission.image_attachments))
        .where(
            WorkspaceFeedbackSubmission.organization_id == expected_organization_id,
            WorkspaceFeedbackSubmission.submitted_by_user_id == normalized_submitter_id,
        )
        .order_by(WorkspaceFeedbackSubmission.created_at.desc(), WorkspaceFeedbackSubmission.id.desc())
        .limit(bounded_limit)
    ).all()
    return WorkspaceFeedbackListResponse(
        items=[workspace_feedback_response(item) for item in items],
        next_submission_at=_next_submission_at(session, expected_organization_id),
    )


def workspace_feedback_response(
    feedback: WorkspaceFeedbackSubmission,
) -> WorkspaceFeedbackResponse:
    """Build the browser-safe workspace response from a scoped model row."""

    attachments = sorted(
        feedback.image_attachments or [],
        key=lambda item: (item.sort_order, item.id),
    )
    return WorkspaceFeedbackResponse(
        feedback_id=feedback.id,
        use_case=feedback.use_case,
        intended_outcome=feedback.intended_outcome,
        friction=feedback.friction,
        desired_change=feedback.desired_change,
        reward_status=feedback.reward_status,  # type: ignore[arg-type]
        reward_due_at=feedback.reward_due_at,
        reward_granted_at=feedback.reward_granted_at,
        reward_call_count=feedback.reward_call_count,
        attachments=[
            WorkspaceFeedbackAttachmentResponse(
                attachment_id=attachment.id,
                original_filename=attachment.original_filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size_bytes,
            )
            for attachment in attachments
        ],
        created_at=feedback.created_at,
    )


def get_workspace_feedback_attachment(
    session: Session,
    *,
    organization_id: str,
    submitted_by_user_id: str,
    feedback_id: str,
    attachment_id: str,
) -> WorkspaceFeedbackImageAttachment:
    """Return an image only to the member who submitted that feedback."""

    expected_organization_id = _require_current_organization(session, organization_id)
    normalized_submitter_id = _required_identifier(
        submitted_by_user_id,
        "workspace_feedback_submitter_required",
    )
    attachment = session.scalar(
        select(WorkspaceFeedbackImageAttachment)
        .join(
            WorkspaceFeedbackSubmission,
            WorkspaceFeedbackImageAttachment.feedback_submission_id
            == WorkspaceFeedbackSubmission.id,
        )
        .where(
            WorkspaceFeedbackImageAttachment.id == _required_identifier(
                attachment_id,
                "workspace_feedback_attachment_not_found",
            ),
            WorkspaceFeedbackImageAttachment.feedback_submission_id
            == _required_identifier(feedback_id, "workspace_feedback_not_found"),
            WorkspaceFeedbackImageAttachment.organization_id == expected_organization_id,
            WorkspaceFeedbackSubmission.organization_id == expected_organization_id,
            WorkspaceFeedbackSubmission.submitted_by_user_id == normalized_submitter_id,
        )
    )
    if attachment is None:
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_not_found")
    return attachment


def list_platform_workspace_feedback(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> PlatformWorkspaceFeedbackListResponse:
    """Read all questionnaire records for the platform-only feedback console."""

    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    with bypass_organization_scope(session):
        total = int(
            session.scalar(select(func.count()).select_from(WorkspaceFeedbackSubmission))
            or 0
        )
        rows = session.execute(
            select(
                WorkspaceFeedbackSubmission,
                Organization.name,
                UserAccount.full_name,
                UserAccount.email,
            )
            .join(
                Organization,
                WorkspaceFeedbackSubmission.organization_id == Organization.id,
            )
            .join(
                UserAccount,
                WorkspaceFeedbackSubmission.submitted_by_user_id == UserAccount.id,
            )
            .options(selectinload(WorkspaceFeedbackSubmission.image_attachments))
            .order_by(
                WorkspaceFeedbackSubmission.created_at.desc(),
                WorkspaceFeedbackSubmission.id.desc(),
            )
            .offset(bounded_offset)
            .limit(bounded_limit)
        ).all()

    return PlatformWorkspaceFeedbackListResponse(
        items=[
            _platform_workspace_feedback_response(
                feedback=feedback,
                organization_name=organization_name,
                submitter_name=submitter_name,
                submitter_email=submitter_email,
            )
            for feedback, organization_name, submitter_name, submitter_email in rows
        ],
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
    )


def get_platform_workspace_feedback_attachment(
    session: Session,
    *,
    feedback_id: str,
    attachment_id: str,
) -> WorkspaceFeedbackImageAttachment:
    """Read a protected image for a separately authorized platform request."""

    with bypass_organization_scope(session):
        attachment = session.scalar(
            select(WorkspaceFeedbackImageAttachment)
            .join(
                WorkspaceFeedbackSubmission,
                WorkspaceFeedbackImageAttachment.feedback_submission_id
                == WorkspaceFeedbackSubmission.id,
            )
            .where(
                WorkspaceFeedbackImageAttachment.id == _required_identifier(
                    attachment_id,
                    "workspace_feedback_attachment_not_found",
                ),
                WorkspaceFeedbackImageAttachment.feedback_submission_id
                == _required_identifier(feedback_id, "workspace_feedback_not_found"),
                WorkspaceFeedbackImageAttachment.organization_id
                == WorkspaceFeedbackSubmission.organization_id,
            )
        )
    if attachment is None:
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_not_found")
    return attachment


def _platform_workspace_feedback_response(
    *,
    feedback: WorkspaceFeedbackSubmission,
    organization_name: str,
    submitter_name: str,
    submitter_email: str,
) -> PlatformWorkspaceFeedbackResponse:
    attachments = sorted(
        feedback.image_attachments or [],
        key=lambda item: (item.sort_order, item.id),
    )
    return PlatformWorkspaceFeedbackResponse(
        feedback_id=feedback.id,
        organization_id=feedback.organization_id,
        organization_name=organization_name,
        submitted_by_user_id=feedback.submitted_by_user_id,
        submitter_name=submitter_name,
        submitter_email=submitter_email,
        contact_phone=feedback.contact_phone,
        use_case=feedback.use_case,
        intended_outcome=feedback.intended_outcome,
        friction=feedback.friction,
        desired_change=feedback.desired_change,
        reward_status=feedback.reward_status,  # type: ignore[arg-type]
        reward_due_at=feedback.reward_due_at,
        reward_granted_at=feedback.reward_granted_at,
        reward_call_count=feedback.reward_call_count,
        attachments=[
            WorkspaceFeedbackAttachmentResponse(
                attachment_id=attachment.id,
                original_filename=attachment.original_filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size_bytes,
            )
            for attachment in attachments
        ],
        created_at=feedback.created_at,
    )


def run_workspace_feedback_reward_worker_once(
    database: Database,
    *,
    worker_id: str,
) -> bool:
    """Claim and grant at most one due feedback incentive.

    The worker never reads feedback text.  A lease can expire safely: the
    grant transaction fences the exact lease before it increments the existing
    organization allowance and marks the row granted in the same commit.
    """

    normalized_worker_id = _required_identifier(worker_id, "workspace_feedback_worker_id_required", 160)
    claimed = _claim_next_workspace_feedback_reward(
        database,
        worker_id=normalized_worker_id,
    )
    if claimed is None:
        return False
    _grant_claimed_workspace_feedback_reward(
        database,
        worker_id=normalized_worker_id,
        claimed=claimed,
    )
    return True


def _claim_next_workspace_feedback_reward(
    database: Database,
    *,
    worker_id: str,
) -> ClaimedWorkspaceFeedbackReward | None:
    now = utcnow()
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            _recover_expired_reward_leases(session, now=now)
            eligible = and_(
                WorkspaceFeedbackSubmission.reward_status == WORKSPACE_FEEDBACK_REWARD_QUEUED,
                WorkspaceFeedbackSubmission.reward_due_at <= now,
            )
            candidate = session.execute(
                select(
                    WorkspaceFeedbackSubmission.id,
                    WorkspaceFeedbackSubmission.organization_id,
                )
                .where(eligible)
                .order_by(
                    WorkspaceFeedbackSubmission.reward_due_at.asc(),
                    WorkspaceFeedbackSubmission.created_at.asc(),
                    WorkspaceFeedbackSubmission.id.asc(),
                )
                .limit(1)
            ).first()
            if candidate is None:
                session.commit()
                return None
            feedback_id, organization_id = candidate
            claimed = session.execute(
                update(WorkspaceFeedbackSubmission)
                .where(
                    WorkspaceFeedbackSubmission.id == feedback_id,
                    WorkspaceFeedbackSubmission.organization_id == organization_id,
                    eligible,
                )
                .values(
                    reward_status=WORKSPACE_FEEDBACK_REWARD_RUNNING,
                    reward_attempt_count=WorkspaceFeedbackSubmission.reward_attempt_count + 1,
                    reward_lease_owner=worker_id,
                    reward_lease_expires_at=now
                    + timedelta(seconds=WORKSPACE_FEEDBACK_REWARD_LEASE_SECONDS),
                    reward_last_error=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                session.rollback()
                return None
            session.commit()
            return ClaimedWorkspaceFeedbackReward(
                feedback_id=str(feedback_id),
                organization_id=str(organization_id),
            )


def _recover_expired_reward_leases(session: Session, *, now: datetime) -> None:
    """Return unfinished database-only grants to the durable due queue."""

    session.execute(
        update(WorkspaceFeedbackSubmission)
        .where(
            WorkspaceFeedbackSubmission.reward_status == WORKSPACE_FEEDBACK_REWARD_RUNNING,
            WorkspaceFeedbackSubmission.reward_granted_at.is_(None),
            WorkspaceFeedbackSubmission.reward_lease_expires_at.is_not(None),
            WorkspaceFeedbackSubmission.reward_lease_expires_at <= now,
        )
        .values(
            reward_status=WORKSPACE_FEEDBACK_REWARD_QUEUED,
            reward_due_at=now,
            reward_lease_owner=None,
            reward_lease_expires_at=None,
            reward_last_error="workspace_feedback_reward_lease_expired",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


def _grant_claimed_workspace_feedback_reward(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedWorkspaceFeedbackReward,
) -> None:
    try:
        with database.session_factory() as session:
            set_organization_context(session, claimed.organization_id)
            try:
                now = utcnow()
                feedback = session.scalar(
                    select(WorkspaceFeedbackSubmission).where(
                        WorkspaceFeedbackSubmission.id == claimed.feedback_id,
                        WorkspaceFeedbackSubmission.organization_id == claimed.organization_id,
                        WorkspaceFeedbackSubmission.reward_status
                        == WORKSPACE_FEEDBACK_REWARD_RUNNING,
                        WorkspaceFeedbackSubmission.reward_lease_owner == worker_id,
                        WorkspaceFeedbackSubmission.reward_lease_expires_at.is_not(None),
                        WorkspaceFeedbackSubmission.reward_lease_expires_at > now,
                    )
                )
                if feedback is None:
                    session.rollback()
                    return

                # Claim fence first, then increment the organization allowance.
                # They commit together, so a crash yields either neither change
                # or one immutable ``granted`` row and exactly one +500 update.
                fenced = session.execute(
                    update(WorkspaceFeedbackSubmission)
                    .where(
                        WorkspaceFeedbackSubmission.id == claimed.feedback_id,
                        WorkspaceFeedbackSubmission.organization_id == claimed.organization_id,
                        WorkspaceFeedbackSubmission.reward_status
                        == WORKSPACE_FEEDBACK_REWARD_RUNNING,
                        WorkspaceFeedbackSubmission.reward_lease_owner == worker_id,
                        WorkspaceFeedbackSubmission.reward_lease_expires_at.is_not(None),
                        WorkspaceFeedbackSubmission.reward_lease_expires_at > now,
                    )
                    .values(
                        reward_status=WORKSPACE_FEEDBACK_REWARD_GRANTED,
                        reward_granted_at=now,
                        reward_lease_owner=None,
                        reward_lease_expires_at=None,
                        reward_last_error=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if fenced.rowcount != 1:
                    session.rollback()
                    return
                granted = session.execute(
                    update(Organization)
                    .where(Organization.id == claimed.organization_id)
                    .values(
                        trial_llm_call_limit=Organization.trial_llm_call_limit
                        + feedback.reward_call_count,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if granted.rowcount != 1:
                    raise WorkspaceFeedbackServiceError("workspace_feedback_organization_not_found")
                session.commit()
            finally:
                clear_organization_context(session)
    except Exception:
        # Do not retain exception text: a database driver or a future storage
        # hook may include sensitive operational detail.  Re-queueing is safe
        # because a committed grant no longer matches the leased running state.
        _reschedule_claimed_workspace_feedback_reward(
            database,
            worker_id=worker_id,
            claimed=claimed,
        )


def _reschedule_claimed_workspace_feedback_reward(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedWorkspaceFeedbackReward,
) -> None:
    now = utcnow()
    with database.session_factory() as session:
        set_organization_context(session, claimed.organization_id)
        try:
            session.execute(
                update(WorkspaceFeedbackSubmission)
                .where(
                    WorkspaceFeedbackSubmission.id == claimed.feedback_id,
                    WorkspaceFeedbackSubmission.organization_id == claimed.organization_id,
                    WorkspaceFeedbackSubmission.reward_status
                    == WORKSPACE_FEEDBACK_REWARD_RUNNING,
                    WorkspaceFeedbackSubmission.reward_lease_owner == worker_id,
                )
                .values(
                    reward_status=WORKSPACE_FEEDBACK_REWARD_QUEUED,
                    reward_due_at=now
                    + timedelta(seconds=WORKSPACE_FEEDBACK_RETRY_DELAY_SECONDS),
                    reward_lease_owner=None,
                    reward_lease_expires_at=None,
                    reward_last_error="workspace_feedback_reward_retry",
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
        finally:
            clear_organization_context(session)


def _feedback_by_idempotency_key(
    session: Session,
    *,
    organization_id: str,
    key_hash: str,
) -> WorkspaceFeedbackSubmission | None:
    return session.scalar(
        select(WorkspaceFeedbackSubmission)
        .options(selectinload(WorkspaceFeedbackSubmission.image_attachments))
        .where(
            WorkspaceFeedbackSubmission.organization_id == organization_id,
            WorkspaceFeedbackSubmission.idempotency_key_hash == key_hash,
        )
    )


def _replay_response(
    session: Session,
    *,
    existing: WorkspaceFeedbackSubmission,
    request_fingerprint: str,
) -> WorkspaceFeedbackSubmitResponse:
    if existing.request_fingerprint != request_fingerprint:
        raise WorkspaceFeedbackIdempotencyConflictError()
    return WorkspaceFeedbackSubmitResponse(
        item=workspace_feedback_response(existing),
        next_submission_at=_next_submission_at(session, existing.organization_id),
        replayed=True,
    )


def _next_submission_at(session: Session, organization_id: str) -> datetime | None:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise WorkspaceFeedbackServiceError("workspace_feedback_organization_not_found")
    session.expire(organization, ["feedback_reward_available_at"])
    return _as_utc(organization.feedback_reward_available_at)


def _require_current_organization(session: Session, organization_id: str) -> str:
    normalized = _required_identifier(organization_id, "workspace_feedback_organization_required")
    expected = organization_context_id(session)
    if normalized != expected:
        raise WorkspaceFeedbackServiceError("workspace_feedback_workspace_mismatch")
    return expected


def _required_identifier(value: str, code: str, maximum: int = 255) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise WorkspaceFeedbackServiceError(code)
    return normalized


def _normalize_answer(value: str, code: str) -> str:
    normalized = value.replace("\x00", "").strip() if isinstance(value, str) else ""
    if not normalized:
        raise WorkspaceFeedbackServiceError(code)
    if len(normalized) > WORKSPACE_FEEDBACK_MAX_ANSWER_LENGTH:
        raise WorkspaceFeedbackServiceError("workspace_feedback_answer_too_long")
    return normalized


def _normalize_contact_phone(value: str | None) -> str:
    """Keep a compact, display-safe phone value for platform follow-up only."""

    raw = value.replace("\x00", "").strip() if isinstance(value, str) else ""
    if not raw:
        raise WorkspaceFeedbackServiceError("workspace_feedback_contact_phone_required")
    if (
        len(raw) > WORKSPACE_FEEDBACK_MAX_CONTACT_PHONE_LENGTH
        or not _CONTACT_PHONE_CHARACTERS_PATTERN.fullmatch(raw)
    ):
        raise WorkspaceFeedbackServiceError("workspace_feedback_contact_phone_invalid")

    normalized = re.sub(r"[\s().-]", "", raw)
    if normalized.startswith("00"):
        normalized = "+" + normalized[2:]
    digits = normalized[1:] if normalized.startswith("+") else normalized
    if (
        not digits.isdigit()
        or not 7 <= len(digits) <= 15
        or "+" in normalized[1:]
    ):
        raise WorkspaceFeedbackServiceError("workspace_feedback_contact_phone_invalid")
    return f"+{digits}" if normalized.startswith("+") else digits


def _normalize_attachments(
    values: Sequence[WorkspaceFeedbackAttachmentInput],
) -> tuple[WorkspaceFeedbackAttachmentInput, ...]:
    if len(values) > WORKSPACE_FEEDBACK_MAX_IMAGE_ATTACHMENTS:
        raise WorkspaceFeedbackServiceError("workspace_feedback_too_many_attachments")
    normalized: list[WorkspaceFeedbackAttachmentInput] = []
    storage_keys: set[str] = set()
    for value in values:
        if not isinstance(value, WorkspaceFeedbackAttachmentInput):
            raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_invalid")
        storage_key = _normalize_storage_key(value.storage_key)
        if storage_key in storage_keys:
            raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_duplicate")
        storage_keys.add(storage_key)
        original_filename = _normalize_original_filename(value.original_filename)
        content_type = _normalize_content_type(value.content_type)
        size_bytes = _normalize_size_bytes(value.size_bytes)
        content_sha256 = _normalize_sha256(value.content_sha256)
        normalized.append(
            WorkspaceFeedbackAttachmentInput(
                storage_key=storage_key,
                original_filename=original_filename,
                content_type=content_type,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
            )
        )
    return tuple(normalized)


def _normalize_storage_key(value: str) -> str:
    normalized = value.strip().replace("\\", "/") if isinstance(value, str) else ""
    if not normalized or len(normalized) > 512:
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_storage_invalid")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_storage_invalid")
    return path.as_posix()


def _normalize_original_filename(value: str) -> str:
    normalized = value.replace("\x00", "").strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > 255
        or "/" in normalized
        or "\\" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_filename_invalid")
    return normalized


def _normalize_content_type(value: str) -> str:
    normalized = value.strip().casefold() if isinstance(value, str) else ""
    if normalized not in WORKSPACE_FEEDBACK_ALLOWED_IMAGE_CONTENT_TYPES:
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_type_invalid")
    return normalized


def _normalize_size_bytes(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_size_invalid")
    if value < 0 or value > WORKSPACE_FEEDBACK_MAX_IMAGE_SIZE_BYTES:
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_size_invalid")
    return value


def _normalize_sha256(value: str) -> str:
    normalized = value.strip().casefold() if isinstance(value, str) else ""
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise WorkspaceFeedbackServiceError("workspace_feedback_attachment_digest_invalid")
    return normalized


def _random_reward_due_at(now: datetime) -> datetime:
    delay_seconds = WORKSPACE_FEEDBACK_REWARD_MIN_DELAY_SECONDS + secrets.randbelow(
        WORKSPACE_FEEDBACK_REWARD_MAX_DELAY_SECONDS
        - WORKSPACE_FEEDBACK_REWARD_MIN_DELAY_SECONDS
        + 1
    )
    return now + timedelta(seconds=delay_seconds)


def _request_fingerprint(
    *,
    use_case: str,
    intended_outcome: str,
    friction: str,
    desired_change: str,
    contact_phone: str,
    attachments: Sequence[WorkspaceFeedbackAttachmentInput],
) -> str:
    payload = {
        "use_case": use_case,
        "intended_outcome": intended_outcome,
        "friction": friction,
        "desired_change": desired_change,
        "contact_phone": contact_phone,
        "attachments": [
            # A trusted uploader may mint a fresh opaque storage key for an
            # HTTP retry of the same bytes.  It is transport state, not part
            # of the user's questionnaire identity.
            {
                "original_filename": item.original_filename,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
                "content_sha256": item.content_sha256,
            }
            for item in attachments
        ],
    }
    return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ClaimedWorkspaceFeedbackReward",
    "WORKSPACE_FEEDBACK_ALLOWED_IMAGE_CONTENT_TYPES",
    "WORKSPACE_FEEDBACK_COOLDOWN",
    "WORKSPACE_FEEDBACK_MAX_IMAGE_ATTACHMENTS",
    "WORKSPACE_FEEDBACK_MAX_IMAGE_SIZE_BYTES",
    "WORKSPACE_FEEDBACK_REWARD_CALL_COUNT",
    "WORKSPACE_FEEDBACK_REWARD_GRANTED",
    "WORKSPACE_FEEDBACK_REWARD_MAX_DELAY_SECONDS",
    "WORKSPACE_FEEDBACK_REWARD_MIN_DELAY_SECONDS",
    "WORKSPACE_FEEDBACK_REWARD_QUEUED",
    "WORKSPACE_FEEDBACK_REWARD_RUNNING",
    "WorkspaceFeedbackAttachmentInput",
    "WorkspaceFeedbackCooldownError",
    "WorkspaceFeedbackIdempotencyConflictError",
    "WorkspaceFeedbackServiceError",
    "get_platform_workspace_feedback_attachment",
    "get_workspace_feedback_attachment",
    "list_platform_workspace_feedback",
    "list_workspace_feedback",
    "normalize_workspace_feedback_idempotency_key",
    "run_workspace_feedback_reward_worker_once",
    "submit_workspace_feedback",
    "workspace_feedback_response",
]
