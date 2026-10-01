"""Database-shared integration quotas, finite concurrency leases and fail-closed audit."""
from __future__ import annotations

from collections.abc import Collection
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    Candidate, IntegrationAuditEvent, IntegrationCredential, IntegrationDailyCandidateAccess,
    IntegrationRateLimitBucket, IntegrationRequestLease, IntegrationAnalysisReference,
    IntegrationAnalysisReport, Resume, ResumeFactSnapshot,
)
from app.services.resume_eligibility import is_resume_screening_eligible
from app.services.integration_auth_service import (
    IntegrationAccessError, IntegrationPrincipal, assert_integration_context, aware,
    integration_now, record_integration_audit, revalidate_integration_principal,
)


@dataclass(frozen=True)
class IntegrationReadProvenance:
    """Internal immutable row/version bindings, never added to public DTOs."""
    organization_id: str
    user_id: str
    membership_id: str
    # (candidate, resume, snapshot, snapshot facts version)
    sources: tuple[tuple[str, str, str, int], ...] = ()
    candidate_states: tuple[tuple[str, tuple], ...] = ()
    resume_states: tuple[tuple[str, tuple], ...] = ()
    snapshot_states: tuple[tuple[str, tuple], ...] = ()
    # (report id, version, sorted exact provenance references, confirmed)
    reports: tuple[tuple[str, int, tuple, bool], ...] = ()
    # An expired/discarded idempotent receipt contains no retained customer
    # data. It still goes through lease, grant/session revocation and audit,
    # but must not be reinterpreted as a live candidate/report read.
    terminal_receipt: bool = False


def _source_state(row) -> tuple | None:
    """Copy scalar versions at ORM load time, not from later-refreshed identities."""
    if isinstance(row, Candidate):
        return row.organization_id, row.lifecycle_version, row.deleted_at is not None
    if isinstance(row, Resume):
        return (row.organization_id, row.candidate_id, row.lifecycle_version, row.facts_version,
            row.deleted_at is not None, row.is_active, row.extraction_status,
            is_resume_screening_eligible(row))
    if isinstance(row, ResumeFactSnapshot):
        return row.organization_id, row.resume_id, row.facts_version, row.facts_sha256
    return None


class _ReadSourceCollector:
    """Observe this Session only; retain scalar states, then select returned rows.

    Search builds its DTO from eagerly loaded ORM projections. A Session-local
    load observer captures their versions without editing search/redaction logic
    or re-reading a newer version after the old DTO has already been constructed.
    No SQL, relationship access, body retention or row locks occur in the observer.
    """
    def __init__(self):
        self.states: dict[type, dict[str, tuple]] = {Candidate: {}, Resume: {}, ResumeFactSnapshot: {}}

    def observe(self, session: Session, row) -> None:
        state = _source_state(row)
        if state is not None:
            self.states[type(row)].setdefault(row.id, state)

    def bind(self, session: Session, result, *, organization_id: str, user_id: str,
        membership_id: str) -> IntegrationReadProvenance:
        from app.integration_analysis_schemas import (
            IntegrationAnalysisDraftDetail, IntegrationAnalysisDraftList,
            IntegrationAnalysisPendingDetail, IntegrationAnalysisPendingList,
            IntegrationAnalysisPrepared,
        )
        from app.integration_read_schemas import (
            IntegrationCandidateAssessments, IntegrationCandidateEvidence,
            IntegrationCandidateProfile, IntegrationCandidateSearchResponse,
        )
        sources, reports = set(), []
        terminal_receipt = False

        def source(value):
            key = (str(value.candidate_id), str(value.resume_id), str(value.fact_snapshot_id), value.facts_version)
            sources.add(key)
            return key

        def report(value, *, confirmed: bool):
            keys = tuple(sorted(source(item) for item in value.candidates))
            reports.append((value.id, value.version, keys, confirmed))

        if isinstance(result, (IntegrationCandidateProfile, IntegrationCandidateEvidence, IntegrationCandidateAssessments)):
            source(result)
        elif isinstance(result, IntegrationCandidateSearchResponse):
            for item in result.items:
                source(item)
        elif isinstance(result, IntegrationAnalysisDraftDetail):
            report(result, confirmed=True)
        elif isinstance(result, IntegrationAnalysisPendingDetail):
            report(result, confirmed=False)
        elif isinstance(result, IntegrationAnalysisDraftList):
            for item in result.items:
                report(item, confirmed=True)
        elif isinstance(result, IntegrationAnalysisPendingList):
            for item in result.items:
                report(item, confirmed=False)
        elif isinstance(result, IntegrationAnalysisPrepared):
            if result.status == "expired":
                terminal_receipt = True
            else:
                # A staged save and a saved idempotent receipt both need the
                # same owner/version/source fence. The status determines the
                # expected confirmation state; neither receipt caches a body.
                refs = session.scalars(select(IntegrationAnalysisReference).where(
                    IntegrationAnalysisReference.organization_id == organization_id,
                    IntegrationAnalysisReference.report_id == result.id,
                )).all()
                keys = tuple(sorted(source(item) for item in refs))
                reports.append((result.id, result.version, keys, result.status == "saved"))
                # Idempotency replay performs no source reads; capture its
                # pinned source rows now, then validate them under locks below.
                if keys:
                    for model, ids in ((Candidate, {key[0] for key in keys}),
                        (Resume, {key[1] for key in keys}), (ResumeFactSnapshot, {key[2] for key in keys})):
                        for row in session.scalars(select(model).where(model.organization_id == organization_id,
                            model.id.in_(ids))).all():
                            self.observe(session, row)

        if len(sources) > 2000 or len(reports) > 100:
            raise IntegrationAccessError("integration_response_too_large", 413)
        selected = []
        for model, position in ((Candidate, 0), (Resume, 1), (ResumeFactSnapshot, 2)):
            ids = {key[position] for key in sources}
            states = self.states[model]
            if not ids <= states.keys():
                raise IntegrationAccessError("integration_source_version_conflict", 409)
            selected.append(tuple((key, states[key]) for key in sorted(ids)))
        return IntegrationReadProvenance(organization_id=organization_id, user_id=user_id,
            membership_id=membership_id, sources=tuple(sorted(sources)), candidate_states=selected[0],
            resume_states=selected[1], snapshot_states=selected[2], reports=tuple(sorted(reports)),
            terminal_receipt=terminal_receipt)


@contextmanager
def collect_integration_read_sources(session: Session):
    collector = _ReadSourceCollector()
    # expire_on_commit=False means a source may already be cached before begin.
    for row in tuple(session.identity_map.values()):
        collector.observe(session, row)
    event.listen(session, "loaded_as_persistent", collector.observe)
    try:
        yield collector
    finally:
        event.remove(session, "loaded_as_persistent", collector.observe)


@contextmanager
def integration_source_lock_conflict():
    """Only PostgreSQL NOWAIT contention is retryable; other DB errors propagate.

    Existing fact writers lock Resume then Candidate. Integration callers lock
    Candidate first, so they must never wait for a contended Resume while holding
    Candidate. The owning wrapper rolls back the entire read/save and releases
    its lease; clients can re-read and retry on this 409, never receive partial data.
    """
    try:
        yield
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise IntegrationAccessError("integration_source_busy", 409) from None
        raise


def lock_integration_read_sources(session: Session, provenance: IntegrationReadProvenance,
    *, candidate_ids: Collection[str] = ()) -> None:
    """Linearization fence: candidate -> resume -> snapshot -> report -> authority.

    Only rows actually returned (or the staged save receipt's references) are
    locked. Call BEFORE acquiring identity/integration-policy locks. Normal
    lifecycle deletion uses candidate -> resume -> report; fact writes lock the
    resume before changing snapshots. No source locks survive the final commit.
    """
    organization_id = provenance.organization_id
    assert_integration_context(session, organization_id)
    candidate_states, resume_states, snapshot_states = (dict(provenance.candidate_states),
        dict(provenance.resume_states), dict(provenance.snapshot_states))
    ids = set(candidate_ids) | candidate_states.keys()
    if len(ids) > 2000:
        raise IntegrationAccessError("integration_response_too_large", 413)
    loaded = {}
    for model, requested, expected in ((Candidate, ids, candidate_states),
        (Resume, set(resume_states), resume_states), (ResumeFactSnapshot, set(snapshot_states), snapshot_states)):
        with integration_source_lock_conflict():
            rows = {row.id: row for row in session.scalars(select(model).where(
                model.organization_id == organization_id, model.id.in_(sorted(requested)),
            ).order_by(model.id).with_for_update(nowait=model is Resume)
                .execution_options(populate_existing=True)).all()} if requested else {}
        if rows.keys() != requested:
            raise IntegrationAccessError("integration_resource_not_found", 404)
        for key, state in expected.items():
            if _source_state(rows[key]) != state:
                raise IntegrationAccessError("integration_source_version_conflict", 409)
        loaded[model] = rows
    for candidate_id, resume_id, snapshot_id, facts_version in provenance.sources:
        resume, snapshot = loaded[Resume][resume_id], loaded[ResumeFactSnapshot][snapshot_id]
        if (resume.candidate_id != candidate_id or snapshot.resume_id != resume_id
                or snapshot.facts_version != facts_version):
            raise IntegrationAccessError("integration_source_version_conflict", 409)
    reports = {row.id: row for row in session.scalars(select(IntegrationAnalysisReport).where(
        IntegrationAnalysisReport.organization_id == organization_id,
        IntegrationAnalysisReport.owner_user_id == provenance.user_id,
        IntegrationAnalysisReport.membership_id == provenance.membership_id,
        IntegrationAnalysisReport.id.in_([item[0] for item in provenance.reports]),
    ).order_by(IntegrationAnalysisReport.id).with_for_update().execution_options(populate_existing=True)).all()} if provenance.reports else {}
    current = integration_now()
    for report_id, version, keys, confirmed in provenance.reports:
        row = reports.get(report_id)
        if row is None or row.invalidated_at is not None or aware(row.expires_at) <= current:
            raise IntegrationAccessError("integration_resource_not_found", 404)
        if row.version != version:
            raise IntegrationAccessError("integration_version_conflict", 409)
        if (row.confirmed_at is not None) != confirmed:
            raise IntegrationAccessError("integration_source_version_conflict", 409)
        references = session.scalars(select(IntegrationAnalysisReference).where(
            IntegrationAnalysisReference.organization_id == organization_id,
            IntegrationAnalysisReference.report_id == report_id,
        ).execution_options(populate_existing=True)).all()
        actual = tuple(sorted((ref.candidate_id, ref.resume_id, ref.fact_snapshot_id, ref.facts_version) for ref in references))
        if not keys or actual != keys:
            raise IntegrationAccessError("integration_source_version_conflict", 409)
        for ref in references:
            if (loaded[Candidate][ref.candidate_id].lifecycle_version != ref.candidate_lifecycle_version
                    or loaded[Resume][ref.resume_id].lifecycle_version != ref.resume_lifecycle_version):
                raise IntegrationAccessError("integration_source_version_conflict", 409)


def check_locked_report_expiry(session: Session, provenance: IntegrationReadProvenance,
    *, now: datetime | None = None) -> None:
    """Time can advance while waiting for authority locks; acquire no new locks."""
    report_ids = {item[0] for item in provenance.reports}
    if report_ids:
        visible = set(session.scalars(select(IntegrationAnalysisReport.id).where(
            IntegrationAnalysisReport.organization_id == provenance.organization_id,
            IntegrationAnalysisReport.id.in_(report_ids), IntegrationAnalysisReport.expires_at > integration_now(now),
            IntegrationAnalysisReport.invalidated_at.is_(None),
        )).all())
        if visible != report_ids:
            raise IntegrationAccessError("integration_resource_not_found", 404)


def prune_integration_metadata(session: Session, *, organization_id: str, now: datetime | None = None) -> None:
    """Only this integration namespace; never source data or existing business audit."""
    assert_integration_context(session, organization_id)
    current = integration_now(now)
    for model in (IntegrationAuditEvent, IntegrationRateLimitBucket, IntegrationDailyCandidateAccess, IntegrationRequestLease):
        expired_ids = tuple(session.scalars(select(model.id).where(
            model.organization_id == organization_id, model.expires_at <= current,
        ).order_by(model.expires_at, model.id).limit(1000)).all())
        session.execute(delete(model).where(model.organization_id == organization_id, model.id.in_(expired_ids))
            .execution_options(synchronize_session=False))


def _consume_minute(session: Session, principal: IntegrationPrincipal, settings: AppSettings, now: datetime) -> None:
    start = now.replace(second=0, microsecond=0)
    for kind, scope_id, maximum in (
        ("workspace", principal.organization_id, settings.integrations_workspace_requests_per_minute),
        ("grant", principal.grant_id, settings.integrations_grant_requests_per_minute),
    ):
        bucket = session.scalar(select(IntegrationRateLimitBucket).where(
            IntegrationRateLimitBucket.organization_id == principal.organization_id,
            IntegrationRateLimitBucket.scope_kind == kind,
            IntegrationRateLimitBucket.scope_id == scope_id,
            IntegrationRateLimitBucket.window_started_at == start,
        ).execution_options(populate_existing=True))
        if bucket is None:
            bucket = IntegrationRateLimitBucket(organization_id=principal.organization_id,
                scope_kind=kind, scope_id=scope_id, window_started_at=start,
                request_count=0, expires_at=start + timedelta(minutes=2))
            session.add(bucket)
        if bucket.request_count >= maximum:
            raise IntegrationAccessError("integration_rate_limit_exceeded", 429,
                retry_after=max(1, int((start + timedelta(minutes=1) - now).total_seconds()) + 1))
        bucket.request_count += 1


def begin_integration_request(
    session: Session, principal: IntegrationPrincipal, *, settings: AppSettings,
    action: str, request_id: str | None = None, now: datetime | None = None,
) -> IntegrationRequestLease:
    current = integration_now(now)
    try:
        principal = revalidate_integration_principal(session, principal, settings=settings, now=now, lock=True)
        current = integration_now(now)
        # The policy's atomic UPDATE is held until this transaction commits.
        # Counts/first-row inserts therefore serialize across every API replica.
        prune_integration_metadata(session, organization_id=principal.organization_id, now=current)
        _consume_minute(session, principal, settings, current)
        statement = select(func.count()).select_from(IntegrationRequestLease).where(
            IntegrationRequestLease.organization_id == principal.organization_id,
            IntegrationRequestLease.released_at.is_(None), IntegrationRequestLease.expires_at > current,
        )
        workspace_count = session.scalar(statement) or 0
        grant_count = session.scalar(statement.where(IntegrationRequestLease.grant_id == principal.grant_id)) or 0
        if workspace_count >= settings.integrations_workspace_concurrency or grant_count >= settings.integrations_grant_concurrency:
            raise IntegrationAccessError("integration_concurrency_limit_exceeded", 429, retry_after=1)
        lease = IntegrationRequestLease(organization_id=principal.organization_id,
            grant_id=principal.grant_id, credential_id=principal.credential_id,
            created_at=current, expires_at=current + timedelta(seconds=settings.integrations_request_lease_seconds))
        session.add(lease)
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user_id, grant_id=principal.grant_id,
            credential_id=principal.credential_id, action=action,
            resource_type="request", request_id=request_id, result="started", now=current)
        session.commit()
        return lease
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_storage_unavailable", 503) from None


def _consume_candidates(
    session: Session, principal: IntegrationPrincipal, *, settings: AppSettings,
    candidate_ids: Collection[str], now: datetime,
) -> None:
    ids = frozenset(candidate_ids)
    if len(ids) > 2000:
        raise IntegrationAccessError("integration_response_too_large", 413)
    if ids:
        visible = frozenset(session.scalars(select(Candidate.id).where(
            Candidate.organization_id == principal.organization_id, Candidate.id.in_(ids),
        )).all())
        if visible != ids:
            raise IntegrationAccessError("integration_resource_not_found", 404)
    for kind, scope_id, maximum in (
        ("workspace", principal.organization_id, settings.integrations_workspace_daily_candidates),
        ("grant", principal.grant_id, settings.integrations_grant_daily_candidates),
    ):
        condition = (
            IntegrationDailyCandidateAccess.organization_id == principal.organization_id,
            IntegrationDailyCandidateAccess.scope_kind == kind,
            IntegrationDailyCandidateAccess.scope_id == scope_id,
            IntegrationDailyCandidateAccess.day == now.date(),
        )
        existing_count = session.scalar(select(func.count()).select_from(IntegrationDailyCandidateAccess).where(*condition)) or 0
        existing_ids = frozenset(session.scalars(select(IntegrationDailyCandidateAccess.candidate_id).where(
            *condition, IntegrationDailyCandidateAccess.candidate_id.in_(ids),
        )).all()) if ids else frozenset()
        additions = ids - existing_ids
        if existing_count + len(additions) > maximum:
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            raise IntegrationAccessError("integration_daily_candidate_limit_exceeded", 429,
                retry_after=max(1, int((tomorrow - now).total_seconds()) + 1))
        session.add_all(IntegrationDailyCandidateAccess(organization_id=principal.organization_id,
            scope_kind=kind, scope_id=scope_id, day=now.date(), candidate_id=candidate_id,
            expires_at=now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2))
            for candidate_id in sorted(additions))


def finalize_integration_read(
    session: Session, principal: IntegrationPrincipal, *, settings: AppSettings,
    action: str, resource_type: str, resource_ids: Collection[str] = (),
    candidate_ids: Collection[str] = (), request_id: str | None = None,
    lease_id: str | None = None, now: datetime | None = None,
    provenance: IntegrationReadProvenance | None = None,
) -> None:
    """Call before returning ANY data. Missing/expired leases cannot bypass quotas."""
    current = integration_now(now)
    try:
        source_binding = provenance or IntegrationReadProvenance(organization_id=principal.organization_id,
            user_id=principal.user_id, membership_id=principal.membership_id)
        if (source_binding.organization_id != principal.organization_id or source_binding.user_id != principal.user_id
                or source_binding.membership_id != principal.membership_id):
            raise IntegrationAccessError("integration_context_required", 403)
        effective_candidate_ids = () if source_binding.terminal_receipt else candidate_ids
        lock_integration_read_sources(session, source_binding, candidate_ids=effective_candidate_ids)
        principal = revalidate_integration_principal(session, principal, settings=settings, now=now, lock=True)
        current = integration_now(now)
        check_locked_report_expiry(session, source_binding, now=current)
        lease = session.scalar(select(IntegrationRequestLease).where(
            IntegrationRequestLease.id == lease_id,
            IntegrationRequestLease.organization_id == principal.organization_id,
            IntegrationRequestLease.grant_id == principal.grant_id,
            IntegrationRequestLease.credential_id == principal.credential_id,
        ).execution_options(populate_existing=True)) if lease_id else None
        if lease is None or lease.released_at is not None or aware(lease.expires_at) <= current:
            raise IntegrationAccessError("integration_request_lease_expired", 409)
        _consume_candidates(session, principal, settings=settings, candidate_ids=effective_candidate_ids, now=current)
        ids = tuple(dict.fromkeys(resource_ids))
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user_id, grant_id=principal.grant_id,
            credential_id=principal.credential_id, action=action, resource_type=resource_type,
            resource_id=ids[0] if len(ids) == 1 else None, resource_count=len(ids),
            candidate_count=len(set(effective_candidate_ids)), request_id=request_id, now=current)
        lease.released_at = current
        session.execute(update(IntegrationCredential).where(
            IntegrationCredential.id == principal.credential_id,
            IntegrationCredential.organization_id == principal.organization_id,
        ).values(last_used_at=current))
        session.commit()
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_audit_unavailable", 503) from None


def release_integration_request(
    session: Session, principal: IntegrationPrincipal, *, lease_id: str,
    now: datetime | None = None,
) -> None:
    """Safe finally-path release, even after revocation; never refunds request counts."""
    try:
        assert_integration_context(session, principal.organization_id)
        session.execute(update(IntegrationRequestLease).where(
            IntegrationRequestLease.id == lease_id,
            IntegrationRequestLease.organization_id == principal.organization_id,
            IntegrationRequestLease.grant_id == principal.grant_id,
            IntegrationRequestLease.credential_id == principal.credential_id,
            IntegrationRequestLease.released_at.is_(None),
        ).values(released_at=integration_now(now)))
        session.commit()
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_storage_unavailable", 503) from None
