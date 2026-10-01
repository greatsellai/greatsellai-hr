"""Owner-private external-AI analysis drafts with pinned source provenance."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Collection
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta

from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.integration_analysis_schemas import (
    IntegrationAnalysisCandidateLink,
    IntegrationAnalysisCandidateReference,
    IntegrationAnalysisDraftDetail,
    IntegrationAnalysisDraftList,
    IntegrationAnalysisDraftInput,
    IntegrationAnalysisPrepare,
    IntegrationAnalysisDraftSummary,
    IntegrationAnalysisPendingDetail,
    IntegrationAnalysisPendingList,
    IntegrationAnalysisPendingSummary,
    IntegrationAnalysisPrepared,
    IntegrationAnalysisConfirmed,
    IntegrationAnalysisJobReference,
    IntegrationAnalysisObservation,
)
from app.integration_read_schemas import (
    IntegrationCandidateFacts,
    IntegrationCandidateProfile,
    IntegrationExperienceDetail,
    IntegrationExperienceFact,
)
from app.models import (
    Candidate,
    IntegrationGrant,
    IntegrationAnalysisReference,
    IntegrationAnalysisReport,
    IntegrationIdempotencyRecord,
    IntegrationWorkspacePolicy,
    Job,
    JobVersion,
    Resume,
    ResumeFactSnapshot,
)
from app.services.identity_service import AuthPrincipal
from app.services.integration_auth_service import (
    IntegrationAccessError,
    IntegrationPrincipal,
    assert_integration_context,
    aware,
    ensure_integration_entitlement,
    integration_features,
    integration_now,
    lock_integration_policy,
    record_integration_audit,
    reload_bound_auth,
    revalidate_integration_principal,
)
from app.services.integration_read_service import (
    IntegrationReadError,
    candidate_code,
    fact_reference_ids,
    get_candidate_profile,
    get_candidate_profile_snapshot,
    sanitize_integration_text,
)
from app.services.resume_eligibility import is_resume_screening_eligible

_CURSOR_SALT = "greatsell-integration-analysis-cursor-v1"
_CURSOR_MAX_AGE_SECONDS = 15 * 60
_REPORT_RETENTION = timedelta(days=180)
_PENDING_CONFIRMATION_RETENTION = timedelta(minutes=15)
_IDEMPOTENCY_RETENTION = timedelta(hours=24)
_MAX_PENDING_CONFIRMATIONS = 10
_OPERATION = "prepare_analysis_draft"


class IntegrationAnalysisError(RuntimeError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _Owner:
    organization_id: str
    user_id: str
    membership_id: str
    cursor_connection: str


@dataclass(frozen=True)
class _SourceState:
    references: tuple[IntegrationAnalysisReference, ...]
    profiles: tuple[IntegrationCandidateProfile, ...]
    status: str


def _owner(principal: IntegrationPrincipal | AuthPrincipal) -> _Owner:
    if isinstance(principal, IntegrationPrincipal):
        return _Owner(
            organization_id=principal.organization_id,
            user_id=principal.user_id,
            membership_id=principal.membership_id,
            cursor_connection=f"{principal.audience}:{principal.grant_id}",
        )
    return _Owner(
        organization_id=principal.organization_id,
        user_id=principal.user.id,
        membership_id=principal.membership.id,
        cursor_connection="browser",
    )


def ensure_browser_analysis_access(
    session: Session,
    principal: AuthPrincipal,
    *,
    settings: AppSettings,
    required_scope: str = "analyses:read",
    now: datetime | None = None,
) -> None:
    """Business reads are stricter than the always-available revoke screen."""

    assert_integration_context(session, principal.organization_id)
    ensure_integration_entitlement(principal, now=now)
    features = integration_features(settings, principal.organization_id)
    policy = session.scalar(
        select(IntegrationWorkspacePolicy).where(
            IntegrationWorkspacePolicy.organization_id == principal.organization_id
        ).execution_options(populate_existing=True)
    )
    if not features["analyses"] or policy is None or not policy.enabled:
        raise IntegrationAccessError("integrations_disabled", 403)
    if required_scope not in set(policy.allowed_scopes or []):
        raise IntegrationAccessError("integration_scope_forbidden", 403)


def execute_browser_analysis_read(session: Session, principal: AuthPrincipal, *,
    settings: AppSettings, read: Callable[[], object], required_scope: str = "analyses:read"):
    """Same source fence as REST/MCP, with fresh signed-session authority.

    Freeze the original session version before reads can refresh ORM identities.
    Source locks precede authority/policy locks, matching external draft saves.
    """
    from app.services.integration_limit_service import (
        check_locked_report_expiry, collect_integration_read_sources, lock_integration_read_sources,
    )
    organization_id, user_id, membership_id, session_version = (principal.organization_id,
        principal.user.id, principal.membership.id, principal.user.auth_session_version)
    try:
        ensure_browser_analysis_access(session, principal, settings=settings, required_scope=required_scope)
        with collect_integration_read_sources(session) as collector:
            result = read()
            provenance = collector.bind(session, result, organization_id=organization_id,
                user_id=user_id, membership_id=membership_id)
        lock_integration_read_sources(session, provenance)
        fresh = reload_bound_auth(session, organization_id=organization_id, user_id=user_id,
            membership_id=membership_id, auth_session_version=session_version, lock=True)
        lock_integration_policy(session, organization_id)
        ensure_browser_analysis_access(session, fresh, settings=settings, required_scope=required_scope)
        check_locked_report_expiry(session, provenance)
        detail = isinstance(result, (IntegrationAnalysisDraftDetail, IntegrationAnalysisPendingDetail))
        report_ids = [result.id] if detail else [item.id for item in result.items]
        record_integration_audit(
            session, organization_id=organization_id, actor_user_id=user_id,
            action=("integration.analysis.pending_read" if isinstance(result, IntegrationAnalysisPendingDetail)
                else "integration.analysis.browser_read" if detail
                else "integration.analysis.pending_list" if isinstance(result, IntegrationAnalysisPendingList)
                else "integration.analysis.browser_list"),
            resource_type=("analysis_confirmation" if isinstance(result, (IntegrationAnalysisPendingDetail, IntegrationAnalysisPendingList))
                else "analysis_draft" if detail else "analysis_draft_list"),
            resource_id=report_ids[0] if len(report_ids) == 1 else None,
            resource_count=len(report_ids), candidate_count=len(set(analysis_candidate_ids(result))),
        )
        session.commit()
        return result
    except (IntegrationAccessError, IntegrationAnalysisError):
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_storage_unavailable", 503) from None


def required_save_scopes(payload: IntegrationAnalysisDraftInput) -> frozenset[str]:
    scopes = {"analyses:write", "candidates:read"}
    if payload.job is not None:
        scopes.add("jobs:read")
    if any(reference.source_block_ids for reference in payload.candidates):
        scopes.add("evidence:read")
    return frozenset(scopes)


def _serializer(settings: AppSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_secret(), salt=_CURSOR_SALT)


def _cursor_binding(owner: _Owner) -> dict[str, str]:
    return {
        "organization_id": owner.organization_id,
        "user_id": owner.user_id,
        "membership_id": owner.membership_id,
        "connection": owner.cursor_connection,
    }


def _encode_cursor(
    settings: AppSettings,
    owner: _Owner,
    *,
    limit: int,
    updated_at: datetime,
    report_id: str,
) -> str:
    return _serializer(settings).dumps(
        {
            "binding": _cursor_binding(owner),
            "limit": limit,
            "updated_at": updated_at.isoformat(),
            "report_id": report_id,
        }
    )


def _decode_cursor(
    cursor: str,
    settings: AppSettings,
    owner: _Owner,
    *,
    limit: int,
) -> tuple[datetime, str]:
    if len(cursor) > 2_048:
        raise IntegrationAnalysisError("integration_invalid_cursor", 422)
    try:
        value = _serializer(settings).loads(cursor, max_age=_CURSOR_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise IntegrationAnalysisError("integration_cursor_expired", 422) from exc
    except BadData as exc:
        raise IntegrationAnalysisError("integration_invalid_cursor", 422) from exc
    if (
        not isinstance(value, dict)
        or value.get("binding") != _cursor_binding(owner)
        or value.get("limit") != limit
        or not isinstance(value.get("report_id"), str)
    ):
        raise IntegrationAnalysisError("integration_invalid_cursor", 422)
    try:
        return datetime.fromisoformat(str(value["updated_at"])), value["report_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationAnalysisError("integration_invalid_cursor", 422) from exc


def _request_digest(payload: IntegrationAnalysisDraftInput) -> str:
    serialized = json.dumps(
        payload.model_dump(mode="json", exclude={"idempotency_key"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _idempotency_key_digest(payload: IntegrationAnalysisDraftInput) -> str:
    return hashlib.sha256(str(payload.idempotency_key).encode("ascii")).hexdigest()


def _find_idempotency(
    session: Session,
    principal: IntegrationPrincipal,
    *,
    key_digest: str,
    now: datetime,
) -> IntegrationIdempotencyRecord | None:
    return session.scalar(
        select(IntegrationIdempotencyRecord).where(
            IntegrationIdempotencyRecord.organization_id == principal.organization_id,
            IntegrationIdempotencyRecord.user_id == principal.user_id,
            IntegrationIdempotencyRecord.membership_id == principal.membership_id,
            IntegrationIdempotencyRecord.grant_id == principal.grant_id,
            IntegrationIdempotencyRecord.audience == principal.audience,
            IntegrationIdempotencyRecord.operation == _OPERATION,
            IntegrationIdempotencyRecord.key_digest == key_digest,
            IntegrationIdempotencyRecord.expires_at > now,
        )
    )


def _replay_or_conflict(
    session: Session,
    record: IntegrationIdempotencyRecord | None,
    *,
    request_digest: str,
    now: datetime | None = None,
) -> IntegrationAnalysisPrepared | None:
    if record is None:
        return None
    if record.request_digest != request_digest:
        raise IntegrationAnalysisError("integration_idempotency_conflict", 409)
    report = session.scalar(select(IntegrationAnalysisReport).where(
        IntegrationAnalysisReport.organization_id == record.organization_id,
        IntegrationAnalysisReport.id == record.report_id,
        IntegrationAnalysisReport.owner_user_id == record.user_id,
        IntegrationAnalysisReport.membership_id == record.membership_id,
    ))
    if report is None or report.invalidated_at is not None or aware(report.expires_at) <= integration_now(now):
        status = "expired"
        expires_at = None if report is None else report.expires_at
    else:
        status = "saved" if report.confirmed_at is not None else "awaiting_confirmation"
        expires_at = report.expires_at
    return IntegrationAnalysisPrepared(
        id=record.report_id,
        version=record.report_version,
        status=status,
        expires_at=expires_at,
        replayed=True,
    )


def _lock_pending_confirmation_queue(
    session: Session,
    principal: IntegrationPrincipal,
) -> None:
    """Serialize the per-member pending-count check on PostgreSQL.

    Reuse the canonical workspace -> user -> membership -> plan lock order.
    Source rows are already locked, so this also matches the external finalizer
    and avoids introducing a membership -> workspace inversion.
    """

    reload_bound_auth(
        session,
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        membership_id=principal.membership_id,
        auth_session_version=principal.auth_session_version,
        lock=True,
    )


def _normalize_draft_text(
    value: str,
    *,
    candidate_names: Collection[str],
    max_chars: int,
) -> str:
    # The privacy projection normalizes ordinary full-width punctuation too.
    # Accept that canonicalization, but do not accept removed identity data or
    # invisible formatting characters as harmless normalization.
    expected = " ".join(unicodedata.normalize("NFKC", value).split()).strip(" ·,;:，；：-")
    rendered, _ = sanitize_integration_text(value, max_chars=max_chars)
    for name in candidate_names:
        if rendered is None:
            break
        rendered, _ = sanitize_integration_text(
            rendered,
            candidate_name=name,
            max_chars=max_chars,
        )
        if rendered is None:
            break
    if rendered is None or rendered != expected:
        raise IntegrationAnalysisError("integration_sensitive_draft_not_supported", 422)
    return rendered


def _selected_fact_evidence(
    profile: IntegrationCandidateProfile, fact_ids: set[str]
) -> set[str]:
    evidence: set[str] = set()
    for facts in (
        profile.facts.education,
        profile.facts.experiences,
        profile.facts.skills,
        profile.facts.language_credentials,
        profile.facts.scholarships,
    ):
        for fact in facts:
            if fact.fact_id in fact_ids:
                evidence.update(fact.evidence_source_block_ids)
    return evidence


def _lock_and_validate_sources(
    session: Session,
    principal: IntegrationPrincipal,
    payload: IntegrationAnalysisDraftInput,
) -> tuple[
    list[
        tuple[
            IntegrationAnalysisCandidateReference,
            Candidate,
            Resume,
            IntegrationCandidateProfile,
        ]
    ],
    list[str],
]:
    candidate_ids = sorted(
        str(reference.candidate_id) for reference in payload.candidates
    )
    candidates = {
        candidate.id: candidate
        for candidate in session.scalars(
            select(Candidate)
            .where(
                Candidate.organization_id == principal.organization_id,
                Candidate.id.in_(candidate_ids),
            )
            .order_by(Candidate.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    }
    if set(candidates) != set(candidate_ids):
        raise IntegrationAnalysisError("integration_resource_not_found", 404)

    resume_ids = sorted(str(reference.resume_id) for reference in payload.candidates)
    from app.services.integration_limit_service import integration_source_lock_conflict

    with integration_source_lock_conflict():
        resumes = {
            resume.id: resume
            for resume in session.scalars(
                select(Resume)
                .where(
                    Resume.organization_id == principal.organization_id,
                    Resume.id.in_(resume_ids),
                )
                .order_by(Resume.id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            ).all()
        }
    if set(resumes) != set(resume_ids):
        raise IntegrationAnalysisError("integration_resource_not_found", 404)

    validated = []
    candidate_names: list[str] = []
    for reference in payload.candidates:
        candidate_id = str(reference.candidate_id)
        resume_id = str(reference.resume_id)
        snapshot_id = str(reference.fact_snapshot_id)
        candidate = candidates[candidate_id]
        resume = resumes[resume_id]
        if (
            resume.candidate_id != candidate.id
            or not is_resume_screening_eligible(resume)
            or resume.facts_version != reference.facts_version
        ):
            raise IntegrationAnalysisError("integration_source_version_conflict", 409)
        snapshot = session.scalar(
            select(ResumeFactSnapshot).where(
                ResumeFactSnapshot.organization_id == principal.organization_id,
                ResumeFactSnapshot.id == snapshot_id,
                ResumeFactSnapshot.resume_id == resume.id,
                ResumeFactSnapshot.facts_version == reference.facts_version,
            )
        )
        if snapshot is None:
            raise IntegrationAnalysisError("integration_resource_not_found", 404)
        profile = get_candidate_profile(session, candidate_id=candidate.id)
        if (
            profile.resume_id != resume.id
            or profile.fact_snapshot_id != snapshot.id
            or profile.facts_version != reference.facts_version
        ):
            raise IntegrationAnalysisError("integration_source_version_conflict", 409)
        requested_fact_ids = set(reference.fact_ids)
        if not requested_fact_ids <= fact_reference_ids(profile):
            raise IntegrationAnalysisError("integration_fact_reference_not_found", 404)
        if not set(reference.source_block_ids) <= _selected_fact_evidence(
            profile, requested_fact_ids
        ):
            raise IntegrationAnalysisError("integration_evidence_not_found", 404)
        validated.append((reference, candidate, resume, profile))
        if candidate.display_name:
            candidate_names.append(candidate.display_name)
    return validated, candidate_names


def _validate_job(
    session: Session,
    principal: IntegrationPrincipal,
    payload: IntegrationAnalysisDraftInput,
) -> tuple[str | None, str | None]:
    if payload.job is None:
        return None, None
    job_id = str(payload.job.job_id)
    version_id = str(payload.job.job_version_id)
    row = session.execute(
        select(Job, JobVersion)
        .join(JobVersion, JobVersion.job_id == Job.id)
        .where(
            Job.organization_id == principal.organization_id,
            JobVersion.organization_id == principal.organization_id,
            Job.id == job_id,
            Job.kind == "job",
            JobVersion.id == version_id,
            JobVersion.status == "confirmed",
        )
    ).first()
    if row is None:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    return job_id, version_id


def prepare_analysis_draft(
    session: Session,
    principal: IntegrationPrincipal,
    *,
    payload: IntegrationAnalysisPrepare,
    now: datetime | None = None,
) -> IntegrationAnalysisPrepared:
    """Prepare a short-lived draft; the owner must confirm it in the browser."""

    assert_integration_context(session, principal.organization_id)
    current = integration_now(now)
    key_digest = _idempotency_key_digest(payload)
    request_digest = _request_digest(payload)
    replay = _replay_or_conflict(
        session,
        _find_idempotency(
            session,
            principal,
            key_digest=key_digest,
            now=current,
        ),
        request_digest=request_digest,
        now=current,
    )
    if replay is not None:
        return replay

    try:
        # The shared finalizer owns the outer commit. This is intentionally a
        # no-op context, not a SAVEPOINT: releasing a SAVEPOINT on SQLite can
        # make the draft durable before the final audit succeeds.
        with nullcontext():
            validated, candidate_names = _lock_and_validate_sources(
                session, principal, payload
            )
            # An identical request may have committed while we waited for its
            # source locks. Replay before taking authority locks/counting the
            # queue; otherwise the winner's tenth row can make this retry fail.
            # This also preserves source -> report -> authority lock ordering
            # when the shared finalizer validates the existing receipt.
            replay = _replay_or_conflict(
                session,
                _find_idempotency(
                    session, principal, key_digest=key_digest,
                    now=integration_now(now),
                ),
                request_digest=request_digest,
                now=now,
            )
            if replay is not None:
                return replay
            job_id, job_version_id = _validate_job(session, principal, payload)
            title = _normalize_draft_text(
                payload.title,
                candidate_names=candidate_names,
                max_chars=120,
            )
            inferences = [
                IntegrationAnalysisObservation(
                    candidate_id=item.candidate_id,
                    text=_normalize_draft_text(
                        item.text,
                        candidate_names=candidate_names,
                        max_chars=2_000,
                    ),
                )
                for item in payload.inferences
            ]
            questions = [
                IntegrationAnalysisObservation(
                    candidate_id=item.candidate_id,
                    text=_normalize_draft_text(
                        item.text,
                        candidate_names=candidate_names,
                        max_chars=2_000,
                    ),
                )
                for item in payload.questions_to_verify
            ]

            _lock_pending_confirmation_queue(session, principal)
            current = integration_now(now)
            # Different candidate sets do not contend on the source locks.
            # Recheck only after acquiring the shared identity lock so a
            # same-key request that waited here returns/rejects against the
            # winner instead of reaching the unique constraint or queue cap.
            replay = _replay_or_conflict(
                session,
                _find_idempotency(
                    session, principal, key_digest=key_digest, now=current,
                ),
                request_digest=request_digest,
                now=current,
            )
            if replay is not None:
                return replay
            # Expiry ends this key's uniqueness window even if the worker has
            # not collected its metadata yet. Keep the old report untouched.
            session.execute(delete(IntegrationIdempotencyRecord).where(
                IntegrationIdempotencyRecord.organization_id == principal.organization_id,
                IntegrationIdempotencyRecord.user_id == principal.user_id,
                IntegrationIdempotencyRecord.membership_id == principal.membership_id,
                IntegrationIdempotencyRecord.grant_id == principal.grant_id,
                IntegrationIdempotencyRecord.audience == principal.audience,
                IntegrationIdempotencyRecord.operation == _OPERATION,
                IntegrationIdempotencyRecord.key_digest == key_digest,
                IntegrationIdempotencyRecord.expires_at <= current,
            ).execution_options(synchronize_session=False))
            pending_count = session.scalar(select(func.count()).select_from(IntegrationAnalysisReport).where(
                IntegrationAnalysisReport.organization_id == principal.organization_id,
                IntegrationAnalysisReport.owner_user_id == principal.user_id,
                IntegrationAnalysisReport.membership_id == principal.membership_id,
                IntegrationAnalysisReport.confirmed_at.is_(None),
                IntegrationAnalysisReport.invalidated_at.is_(None),
                IntegrationAnalysisReport.expires_at > current,
            )) or 0
            if pending_count >= _MAX_PENDING_CONFIRMATIONS:
                raise IntegrationAnalysisError("integration_confirmation_queue_full", 429)

            report = IntegrationAnalysisReport(
                organization_id=principal.organization_id,
                owner_user_id=principal.user_id,
                membership_id=principal.membership_id,
                source_grant_id=principal.grant_id,
                source_credential_id=principal.credential_id,
                source_audience=principal.audience,
                source_auth_session_version=principal.auth_session_version,
                job_id=job_id,
                job_version_id=job_version_id,
                title=title,
                content_json={
                    "schema_version": "integration_analysis_draft.v1",
                    "inferences": [
                        item.model_dump(mode="json") for item in inferences
                    ],
                    "questions_to_verify": [
                        item.model_dump(mode="json") for item in questions
                    ],
                },
                version=1,
                created_at=current,
                updated_at=current,
                expires_at=current + _PENDING_CONFIRMATION_RETENTION,
                confirmed_at=None,
            )
            session.add(report)
            session.flush()

            for reference, candidate, resume, _profile in validated:
                session.add(
                    IntegrationAnalysisReference(
                        organization_id=principal.organization_id,
                        report_id=report.id,
                        candidate_id=candidate.id,
                        resume_id=resume.id,
                        fact_snapshot_id=str(reference.fact_snapshot_id),
                        facts_version=reference.facts_version,
                        candidate_lifecycle_version=candidate.lifecycle_version,
                        resume_lifecycle_version=resume.lifecycle_version,
                        fact_ids=list(reference.fact_ids),
                        source_block_ids=list(reference.source_block_ids),
                        created_at=current,
                    )
                )
            session.add(
                IntegrationIdempotencyRecord(
                    organization_id=principal.organization_id,
                    user_id=principal.user_id,
                    membership_id=principal.membership_id,
                    grant_id=principal.grant_id,
                    audience=principal.audience,
                    operation=_OPERATION,
                    key_digest=key_digest,
                    request_digest=request_digest,
                    report_id=report.id,
                    report_version=report.version,
                    created_at=current,
                    expires_at=current + _IDEMPOTENCY_RETENTION,
                )
            )
            session.flush()
            return IntegrationAnalysisPrepared(
                id=report.id,
                version=report.version,
                status="awaiting_confirmation",
                expires_at=report.expires_at,
            )
    except IntegrationAnalysisError:
        session.rollback()
        # A concurrent identical request may have committed while this call
        # waited for source locks. If source validation then observes deletion
        # or a version change, honor the already-committed idempotency receipt;
        # the outer finalizer still revalidates authorization and any live
        # source fence before returning it.
        current = integration_now(now)
        replay = _replay_or_conflict(
            session,
            _find_idempotency(
                session, principal, key_digest=key_digest, now=current,
            ),
            request_digest=request_digest,
            now=current,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError:
        session.rollback()
        replay = _replay_or_conflict(
            session,
            _find_idempotency(
                session,
                principal,
                key_digest=key_digest,
                now=current,
                ),
                request_digest=request_digest,
                now=current,
        )
        if replay is not None:
            return replay
        raise IntegrationAnalysisError("integration_write_conflict", 409) from None
    except Exception:
        session.rollback()
        raise


def _report_statement(owner: _Owner, now: datetime):
    return select(IntegrationAnalysisReport).where(
        IntegrationAnalysisReport.organization_id == owner.organization_id,
        IntegrationAnalysisReport.owner_user_id == owner.user_id,
        IntegrationAnalysisReport.membership_id == owner.membership_id,
        IntegrationAnalysisReport.confirmed_at.is_not(None),
        IntegrationAnalysisReport.invalidated_at.is_(None),
        IntegrationAnalysisReport.expires_at > now,
    )


def _profile_selected_facts(
    profile: IntegrationCandidateProfile,
    *,
    fact_ids: Collection[str],
    source_block_ids: Collection[str],
) -> IntegrationCandidateProfile:
    selected_ids = set(fact_ids)
    selected_sources = set(source_block_ids)

    def trim_fact(fact):
        return fact.model_copy(
            update={
                "evidence_source_block_ids": [
                    block_id
                    for block_id in fact.evidence_source_block_ids
                    if block_id in selected_sources
                ]
            }
        )

    experiences: list[IntegrationExperienceFact] = []
    for fact in profile.facts.experiences:
        if fact.fact_id not in selected_ids:
            continue
        details = [
            IntegrationExperienceDetail(
                detail=detail.detail,
                evidence_source_block_ids=[
                    block_id
                    for block_id in detail.evidence_source_block_ids
                    if block_id in selected_sources
                ],
            )
            for detail in fact.details
        ]
        experiences.append(trim_fact(fact).model_copy(update={"details": details}))

    facts = IntegrationCandidateFacts(
        is_985_211=None,
        highest_degree=None,
        employment_months=None,
        employment_or_internship_months=None,
        education=[
            trim_fact(fact)
            for fact in profile.facts.education
            if fact.fact_id in selected_ids
        ],
        experiences=experiences,
        skills=[
            trim_fact(fact)
            for fact in profile.facts.skills
            if fact.fact_id in selected_ids
        ],
        language_credentials=[
            trim_fact(fact)
            for fact in profile.facts.language_credentials
            if fact.fact_id in selected_ids
        ],
        scholarships=[
            trim_fact(fact)
            for fact in profile.facts.scholarships
            if fact.fact_id in selected_ids
        ],
    )
    return profile.model_copy(
        update={
            "facts": facts,
            "evidence_source_block_ids": [
                block_id
                for block_id in profile.evidence_source_block_ids
                if block_id in selected_sources
            ],
        }
    )


def _load_source_state(
    session: Session,
    report: IntegrationAnalysisReport,
) -> _SourceState | None:
    references = tuple(
        session.scalars(
            select(IntegrationAnalysisReference)
            .where(
                IntegrationAnalysisReference.organization_id == report.organization_id,
                IntegrationAnalysisReference.report_id == report.id,
            )
            .order_by(IntegrationAnalysisReference.candidate_id)
        ).all()
    )
    if not references:
        return None
    profiles: list[IntegrationCandidateProfile] = []
    source_changed = False
    for reference in references:
        candidate = session.scalar(
            select(Candidate).where(
                Candidate.organization_id == report.organization_id,
                Candidate.id == reference.candidate_id,
            )
        )
        resume = session.scalar(
            select(Resume).where(
                Resume.organization_id == report.organization_id,
                Resume.id == reference.resume_id,
            )
        )
        if candidate is None or resume is None or resume.candidate_id != candidate.id:
            return None
        # A delete/restore advances lifecycle_version. It must never revive a draft.
        if (
            candidate.lifecycle_version != reference.candidate_lifecycle_version
            or resume.lifecycle_version != reference.resume_lifecycle_version
        ):
            return None
        try:
            profile = get_candidate_profile_snapshot(
                session,
                candidate_id=candidate.id,
                resume_id=resume.id,
                fact_snapshot_id=reference.fact_snapshot_id,
                facts_version=reference.facts_version,
            )
        except IntegrationReadError:
            return None
        if (
            resume.facts_version != reference.facts_version
            or not is_resume_screening_eligible(resume)
        ):
            source_changed = True
        profiles.append(
            _profile_selected_facts(
                profile,
                fact_ids=reference.fact_ids,
                source_block_ids=reference.source_block_ids,
            )
        )
    return _SourceState(
        references=references,
        profiles=tuple(profiles),
        status="source_changed" if source_changed else "current",
    )


def _candidate_links(state: _SourceState) -> list[IntegrationAnalysisCandidateLink]:
    return [
        IntegrationAnalysisCandidateLink(
            candidate_id=profile.candidate_id,
            candidate_code=candidate_code(profile.candidate_id),
            resume_id=profile.resume_id,
            fact_snapshot_id=profile.fact_snapshot_id,
            facts_version=profile.facts_version,
        )
        for profile in state.profiles
    ]


def _job_reference(
    report: IntegrationAnalysisReport,
) -> IntegrationAnalysisJobReference | None:
    if report.job_id is None or report.job_version_id is None:
        return None
    return IntegrationAnalysisJobReference(
        job_id=report.job_id,
        job_version_id=report.job_version_id,
    )


def _summary(
    report: IntegrationAnalysisReport,
    state: _SourceState,
) -> IntegrationAnalysisDraftSummary:
    return IntegrationAnalysisDraftSummary(
        id=report.id,
        title=report.title,
        version=report.version,
        source_status=state.status,
        candidates=_candidate_links(state),
        job=_job_reference(report),
        created_at=report.created_at,
        updated_at=report.updated_at,
        expires_at=report.expires_at,
    )


def list_analysis_drafts(
    session: Session,
    principal: IntegrationPrincipal | AuthPrincipal,
    *,
    settings: AppSettings,
    limit: int = 20,
    cursor: str | None = None,
    now: datetime | None = None,
) -> IntegrationAnalysisDraftList:
    owner = _owner(principal)
    assert_integration_context(session, owner.organization_id)
    if isinstance(limit, bool) or limit < 1 or limit > 100:
        raise IntegrationAnalysisError("integration_invalid_limit", 422)
    current = integration_now(now)
    cursor_updated_at: datetime | None = None
    cursor_id: str | None = None
    if cursor:
        cursor_updated_at, cursor_id = _decode_cursor(
            cursor,
            settings,
            owner,
            limit=limit,
        )
    statement = _report_statement(owner, current).order_by(
        IntegrationAnalysisReport.updated_at.desc(),
        IntegrationAnalysisReport.id.desc(),
    )
    if cursor_updated_at is not None and cursor_id is not None:
        statement = statement.where(
            or_(
                IntegrationAnalysisReport.updated_at < cursor_updated_at,
                and_(
                    IntegrationAnalysisReport.updated_at == cursor_updated_at,
                    IntegrationAnalysisReport.id < cursor_id,
                ),
            )
        )
    reports = session.scalars(statement.limit(limit + 1)).all()
    items: list[IntegrationAnalysisDraftSummary] = []
    for report in reports[:limit]:
        state = _load_source_state(session, report)
        if state is not None:
            items.append(_summary(report, state))
    next_cursor = None
    if len(reports) > limit:
        last = reports[limit - 1]
        next_cursor = _encode_cursor(
            settings,
            owner,
            limit=limit,
            updated_at=last.updated_at,
            report_id=last.id,
        )
    return IntegrationAnalysisDraftList(items=items, next_cursor=next_cursor)


def _observations(
    report: IntegrationAnalysisReport,
    key: str,
) -> list[IntegrationAnalysisObservation]:
    content = report.content_json
    if not isinstance(content, dict) or not isinstance(content.get(key), list):
        raise IntegrationAnalysisError("integration_draft_unavailable", 503)
    try:
        return [
            IntegrationAnalysisObservation.model_validate(item) for item in content[key]
        ]
    except (TypeError, ValueError) as exc:
        raise IntegrationAnalysisError("integration_draft_unavailable", 503) from exc


def get_analysis_draft(
    session: Session,
    principal: IntegrationPrincipal | AuthPrincipal,
    *,
    report_id: str,
    now: datetime | None = None,
) -> IntegrationAnalysisDraftDetail:
    if len(report_id) > 128:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    owner = _owner(principal)
    assert_integration_context(session, owner.organization_id)
    current = integration_now(now)
    report = session.scalar(
        _report_statement(owner, current).where(
            IntegrationAnalysisReport.id == report_id
        )
    )
    if report is None:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    state = _load_source_state(session, report)
    if state is None:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    summary = _summary(report, state)
    return IntegrationAnalysisDraftDetail(
        **summary.model_dump(),
        referenced_facts=list(state.profiles),
        inferences=_observations(report, "inferences"),
        questions_to_verify=_observations(report, "questions_to_verify"),
    )


def _pending_report_statement(owner: _Owner, now: datetime):
    return select(IntegrationAnalysisReport).where(
        IntegrationAnalysisReport.organization_id == owner.organization_id,
        IntegrationAnalysisReport.owner_user_id == owner.user_id,
        IntegrationAnalysisReport.membership_id == owner.membership_id,
        IntegrationAnalysisReport.confirmed_at.is_(None),
        IntegrationAnalysisReport.invalidated_at.is_(None),
        IntegrationAnalysisReport.expires_at > now,
    )


def _pending_digest(
    session: Session,
    report: IntegrationAnalysisReport,
) -> str:
    references = session.scalars(select(IntegrationAnalysisReference).where(
        IntegrationAnalysisReference.organization_id == report.organization_id,
        IntegrationAnalysisReference.report_id == report.id,
    ).order_by(IntegrationAnalysisReference.candidate_id)).all()
    payload = {
        "schema": "integration_analysis_pending.v1",
        "report": {
            "id": report.id,
            "organization_id": report.organization_id,
            "owner_user_id": report.owner_user_id,
            "membership_id": report.membership_id,
            "source_grant_id": report.source_grant_id,
            "source_credential_id": report.source_credential_id,
            "source_audience": report.source_audience,
            "source_auth_session_version": report.source_auth_session_version,
            "job_id": report.job_id,
            "job_version_id": report.job_version_id,
            "title": report.title,
            "content_json": report.content_json,
            "version": report.version,
            "created_at": aware(report.created_at).isoformat(),
            "expires_at": aware(report.expires_at).isoformat(),
        },
        "references": [
            {
                "candidate_id": item.candidate_id,
                "resume_id": item.resume_id,
                "fact_snapshot_id": item.fact_snapshot_id,
                "facts_version": item.facts_version,
                "candidate_lifecycle_version": item.candidate_lifecycle_version,
                "resume_lifecycle_version": item.resume_lifecycle_version,
                "fact_ids": sorted(item.fact_ids or []),
                "source_block_ids": sorted(item.source_block_ids or []),
            }
            for item in references
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _pending_summary(
    session: Session,
    report: IntegrationAnalysisReport,
    state: _SourceState,
) -> IntegrationAnalysisPendingSummary:
    connection_name = session.scalar(select(IntegrationGrant.name).where(
        IntegrationGrant.id == report.source_grant_id,
        IntegrationGrant.organization_id == report.organization_id,
        IntegrationGrant.user_id == report.owner_user_id,
        IntegrationGrant.membership_id == report.membership_id,
    ))
    job_title = None
    if report.job_id is not None:
        job_title = session.scalar(select(Job.title).where(
            Job.id == report.job_id,
            Job.organization_id == report.organization_id,
        ))
    return IntegrationAnalysisPendingSummary(
        id=report.id,
        title=report.title,
        version=report.version,
        candidates=_candidate_links(state),
        job=_job_reference(report),
        job_title=job_title,
        source_connection_name=connection_name or "已撤销的连接",
        created_at=report.created_at,
        expires_at=report.expires_at,
    )


def list_pending_analysis_drafts(
    session: Session,
    principal: AuthPrincipal,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> IntegrationAnalysisPendingList:
    owner = _owner(principal)
    assert_integration_context(session, owner.organization_id)
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_PENDING_CONFIRMATIONS:
        raise IntegrationAnalysisError("integration_invalid_limit", 422)
    current = integration_now(now)
    reports = session.scalars(_pending_report_statement(owner, current).order_by(
        IntegrationAnalysisReport.created_at.desc(), IntegrationAnalysisReport.id.desc(),
    ).limit(limit)).all()
    items = []
    for report in reports:
        state = _load_source_state(session, report)
        if state is not None:
            items.append(_pending_summary(session, report, state))
    return IntegrationAnalysisPendingList(items=items)


def get_pending_analysis_draft(
    session: Session,
    principal: AuthPrincipal,
    *,
    report_id: str,
    now: datetime | None = None,
) -> IntegrationAnalysisPendingDetail:
    if len(report_id) > 128:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    owner = _owner(principal)
    assert_integration_context(session, owner.organization_id)
    current = integration_now(now)
    report = session.scalar(_pending_report_statement(owner, current).where(
        IntegrationAnalysisReport.id == report_id,
    ))
    if report is None:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    state = _load_source_state(session, report)
    if state is None:
        raise IntegrationAnalysisError("integration_resource_not_found", 404)
    summary = _pending_summary(session, report, state)
    return IntegrationAnalysisPendingDetail(
        id=report.id,
        title=report.title,
        version=report.version,
        candidates=summary.candidates,
        job=summary.job,
        job_title=summary.job_title,
        source_connection_name=summary.source_connection_name,
        payload_sha256=_pending_digest(session, report),
        source_status=state.status,
        referenced_facts=list(state.profiles),
        inferences=_observations(report, "inferences"),
        questions_to_verify=_observations(report, "questions_to_verify"),
        created_at=report.created_at,
        expires_at=report.expires_at,
    )


def confirm_pending_analysis_draft(
    session: Session,
    principal: AuthPrincipal,
    *,
    report_id: str,
    version: int,
    payload_sha256: str,
    settings: AppSettings,
    request_id: str | None = None,
    now: datetime | None = None,
) -> IntegrationAnalysisConfirmed:
    """Commit only the exact pending content reviewed by its owner in-browser."""
    from app.services.integration_limit_service import (
        check_locked_report_expiry,
        collect_integration_read_sources,
        lock_integration_read_sources,
    )

    organization_id, user_id, membership_id, session_version = (
        principal.organization_id,
        principal.user.id,
        principal.membership.id,
        principal.user.auth_session_version,
    )
    try:
        ensure_browser_analysis_access(
            session, principal, settings=settings, required_scope="analyses:write", now=now,
        )
        with collect_integration_read_sources(session) as collector:
            pending = get_pending_analysis_draft(
                session, principal, report_id=report_id, now=now,
            )
            provenance = collector.bind(
                session, pending, organization_id=organization_id,
                user_id=user_id, membership_id=membership_id,
            )
        lock_integration_read_sources(session, provenance)
        fresh = reload_bound_auth(
            session,
            organization_id=organization_id,
            user_id=user_id,
            membership_id=membership_id,
            auth_session_version=session_version,
            lock=True,
        )
        lock_integration_policy(session, organization_id)
        ensure_browser_analysis_access(
            session, fresh, settings=settings, required_scope="analyses:write", now=now,
        )
        check_locked_report_expiry(session, provenance, now=now)
        report = session.scalar(_pending_report_statement(
            _owner(fresh), integration_now(now),
        ).where(
            IntegrationAnalysisReport.id == report_id,
        ).with_for_update().execution_options(populate_existing=True))
        if report is None:
            raise IntegrationAnalysisError("integration_resource_not_found", 404)
        if report.version != version:
            raise IntegrationAnalysisError("integration_version_conflict", 409)
        if pending.payload_sha256 != payload_sha256 or _pending_digest(session, report) != payload_sha256:
            raise IntegrationAnalysisError("integration_confirmation_content_changed", 409)
        if pending.source_status != "current":
            raise IntegrationAnalysisError("integration_source_version_conflict", 409)
        if (
            report.source_credential_id is None
            or report.source_audience not in {"rest", "mcp"}
            or report.source_auth_session_version is None
        ):
            raise IntegrationAccessError("integration_invalid_token", 401)
        source_principal = IntegrationPrincipal(
            auth=fresh,
            grant_id=report.source_grant_id,
            credential_id=report.source_credential_id,
            audience=report.source_audience,
            scopes=frozenset(),
            auth_session_version=report.source_auth_session_version,
            required_scopes=frozenset({"analyses:write"}),
        )
        revalidate_integration_principal(
            session, source_principal, settings=settings, now=now, lock=True,
        )
        current = integration_now(now)
        report.confirmed_at = current
        report.updated_at = current
        report.expires_at = current + _REPORT_RETENTION
        record_integration_audit(
            session,
            organization_id=organization_id,
            actor_user_id=user_id,
            grant_id=report.source_grant_id,
            credential_id=report.source_credential_id,
            action="integration.analysis.browser_confirm",
            resource_type="analysis_draft",
            resource_id=report.id,
            resource_count=1,
            candidate_count=len(pending.candidates),
            request_id=request_id,
        )
        session.commit()
        return IntegrationAnalysisConfirmed(
            id=report.id,
            version=report.version,
            confirmed_at=current,
        )
    except (IntegrationAccessError, IntegrationAnalysisError):
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_storage_unavailable", 503) from None


def discard_pending_analysis_draft(
    session: Session,
    principal: AuthPrincipal,
    *,
    report_id: str,
    version: int,
    request_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Erase an unconfirmed draft; the owner may discard even if integration is disabled."""
    organization_id = principal.organization_id
    assert_integration_context(session, organization_id)
    try:
        # Lock the report before the authority rows, matching confirmation's
        # source -> report -> authority order and avoiding confirm/discard lock
        # inversion under concurrent browser actions.
        report = session.scalar(select(IntegrationAnalysisReport).where(
            IntegrationAnalysisReport.organization_id == organization_id,
            IntegrationAnalysisReport.owner_user_id == principal.user.id,
            IntegrationAnalysisReport.membership_id == principal.membership.id,
            IntegrationAnalysisReport.id == report_id,
            IntegrationAnalysisReport.confirmed_at.is_(None),
            IntegrationAnalysisReport.invalidated_at.is_(None),
        ).with_for_update().execution_options(populate_existing=True))
        if report is None:
            raise IntegrationAnalysisError("integration_resource_not_found", 404)
        if report.version != version:
            raise IntegrationAnalysisError("integration_version_conflict", 409)
        fresh = reload_bound_auth(
            session,
            organization_id=organization_id,
            user_id=principal.user.id,
            membership_id=principal.membership.id,
            auth_session_version=principal.user.auth_session_version,
            lock=True,
        )
        current = integration_now(now)
        session.execute(delete(IntegrationAnalysisReference).where(
            IntegrationAnalysisReference.organization_id == organization_id,
            IntegrationAnalysisReference.report_id == report.id,
        ))
        report.title = "已取消的待确认草稿"
        report.content_json = {}
        report.job_id = None
        report.job_version_id = None
        report.invalidated_at = current
        report.invalidation_reason = "user_discarded"
        report.updated_at = current
        report.version += 1
        record_integration_audit(
            session,
            organization_id=organization_id,
            actor_user_id=fresh.user.id,
            action="integration.analysis.browser_discard",
            resource_type="analysis_confirmation",
            resource_id=report.id,
            request_id=request_id,
        )
        session.commit()
    except (IntegrationAccessError, IntegrationAnalysisError):
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_storage_unavailable", 503) from None


def analysis_candidate_ids(
    result: IntegrationAnalysisDraftList | IntegrationAnalysisDraftDetail | IntegrationAnalysisPendingList | IntegrationAnalysisPendingDetail,
) -> list[str]:
    if isinstance(result, (IntegrationAnalysisDraftDetail, IntegrationAnalysisPendingDetail)):
        return [candidate.candidate_id for candidate in result.candidates]
    return [
        candidate.candidate_id for item in result.items for candidate in item.candidates
    ]
