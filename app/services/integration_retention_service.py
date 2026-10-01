"""Erase retained integration copies with their source lifecycle.

This is an internal lifecycle/worker service, never an external API. It also
runs when integrations are disabled so the kill switch cannot stop erasure.
"""
from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import and_, delete, or_, select, union, update
from sqlalchemy.orm import Session

from app.database import Database
from app.models import (
    IntegrationAnalysisReference, IntegrationAnalysisReport, IntegrationAuditEvent,
    IntegrationDailyCandidateAccess, IntegrationIdempotencyRecord,
    IntegrationRateLimitBucket, IntegrationRequestLease,
)
from app.services.integration_auth_service import integration_now
from app.services.integration_oauth_retention_service import (
    cleanup_terminal_oauth_families_global,
    cleanup_unused_oauth_clients,
)
from app.tenant_scope import organization_context_id, set_organization_context


def invalidate_analysis_reports_for_sources(
    session: Session, *, organization_id: str,
    candidate_ids: Collection[str] = (), resume_ids: Collection[str] = (),
    now: datetime | None = None,
) -> int:
    """Scrub the WHOLE draft before releasing any provenance foreign key.

    The caller already holds the candidate/resume lifecycle locks. Never
    commit here: erasure and source deletion must share the caller transaction.
    Historical internal lifecycle calls may use the legacy workspace, unlike
    external authorization, which must reject it.
    """
    if not organization_id or organization_context_id(session) != organization_id:
        raise ValueError("integration_lifecycle_context_required")
    conditions = []
    if candidate_ids:
        conditions.append(IntegrationAnalysisReference.candidate_id.in_(tuple(candidate_ids)))
    if resume_ids:
        conditions.append(IntegrationAnalysisReference.resume_id.in_(tuple(resume_ids)))
    if not conditions:
        return 0
    report_ids = tuple(session.scalars(select(IntegrationAnalysisReference.report_id).where(
        IntegrationAnalysisReference.organization_id == organization_id, or_(*conditions),
    ).distinct()).all())
    if not report_ids:
        return 0
    # Lock in stable order. Draft saves use candidate -> resume -> report too.
    locked_ids = tuple(session.scalars(select(IntegrationAnalysisReport.id).where(
        IntegrationAnalysisReport.organization_id == organization_id,
        IntegrationAnalysisReport.id.in_(report_ids),
    ).order_by(IntegrationAnalysisReport.id).with_for_update()).all())
    current = integration_now(now)
    session.execute(update(IntegrationAnalysisReport).where(
        IntegrationAnalysisReport.organization_id == organization_id,
        IntegrationAnalysisReport.id.in_(locked_ids),
    ).values(title="已失效的分析草稿", content_json={}, job_id=None, job_version_id=None,
        invalidated_at=current, invalidation_reason="source_deleted", updated_at=current,
        version=IntegrationAnalysisReport.version + 1).execution_options(synchronize_session=False))
    # Release all references, not just the deleted candidate's portion: no
    # remaining candidate can make this report readable again after restore.
    session.execute(delete(IntegrationAnalysisReference).where(
        IntegrationAnalysisReference.organization_id == organization_id,
        IntegrationAnalysisReference.report_id.in_(locked_ids),
    ).execution_options(synchronize_session=False))
    return len(locked_ids)


def cleanup_expired_integration_records(
    database: Database, *, now: datetime | None = None, workspace_limit: int = 10,
) -> int:
    """Bounded worker cleanup of expired copies and content-free metadata.

    Global abandoned OAuth registrations are handled separately. Workspace
    enumeration reads only IDs/expiry, then every customer-data write has an
    explicit workspace predicate. No customer originals or facts are touched.
    """
    current = integration_now(now)
    with database.session_factory() as cleanup:
        removed = cleanup_terminal_oauth_families_global(cleanup, now=current)
        removed += cleanup_unused_oauth_clients(cleanup, now=current)
        cleanup.commit()
    models = (IntegrationAnalysisReport, IntegrationIdempotencyRecord, IntegrationAuditEvent,
        IntegrationRateLimitBucket, IntegrationDailyCandidateAccess, IntegrationRequestLease)
    report_table = IntegrationAnalysisReport.__table__
    idempotency_table = IntegrationIdempotencyRecord.__table__
    live_receipt = select(idempotency_table.c.id).where(
        idempotency_table.c.organization_id == report_table.c.organization_id,
        idempotency_table.c.report_id == report_table.c.id,
        idempotency_table.c.expires_at > current,
    ).exists()
    # Invalidated rows have already been scrubbed. Do not let the same first
    # 100 receipt tombstones occupy every batch (or due-workspace selection)
    # while their 24-hour idempotency window is still live.
    report_due = and_(
        report_table.c.expires_at <= current,
        or_(report_table.c.invalidated_at.is_(None), ~live_receipt),
    )
    due = union(*(select(model.__table__.c.organization_id).where(
        report_due if model is IntegrationAnalysisReport else model.__table__.c.expires_at <= current
    ) for model in models)).subquery()
    with database.session_factory() as discovery:
        organizations = tuple(discovery.scalars(select(due.c.organization_id)
            .order_by(due.c.organization_id).limit(max(1, min(workspace_limit, 100)))).all())
    for organization_id in organizations:
        with database.session_factory() as session:
            set_organization_context(session, organization_id)
            reports = tuple(session.scalars(select(IntegrationAnalysisReport.id).where(
                IntegrationAnalysisReport.organization_id == organization_id,
                report_due,
            ).order_by(IntegrationAnalysisReport.id).limit(100).with_for_update()).all())
            expired_idempotency_ids = tuple(session.scalars(select(IntegrationIdempotencyRecord.id).where(
                IntegrationIdempotencyRecord.organization_id == organization_id,
                IntegrationIdempotencyRecord.expires_at <= current,
            ).order_by(IntegrationIdempotencyRecord.id).limit(1000)).all())
            live_idempotency_report_ids = set(session.scalars(select(IntegrationIdempotencyRecord.report_id).where(
                IntegrationIdempotencyRecord.organization_id == organization_id,
                IntegrationIdempotencyRecord.report_id.in_(reports),
                IntegrationIdempotencyRecord.expires_at > current,
            )).all()) if reports else set()
            idem_condition = IntegrationIdempotencyRecord.id.in_(expired_idempotency_ids)
            if reports:
                idem_condition = or_(
                    idem_condition,
                    and_(
                        IntegrationIdempotencyRecord.report_id.in_(reports),
                        IntegrationIdempotencyRecord.expires_at <= current,
                    ),
                )
            deleted_idempotency = session.execute(delete(IntegrationIdempotencyRecord).where(
                IntegrationIdempotencyRecord.organization_id == organization_id, idem_condition,
            ).execution_options(synchronize_session=False))
            removed += max(0, deleted_idempotency.rowcount)
            if reports:
                retained_receipts = tuple(report_id for report_id in reports if report_id in live_idempotency_report_ids)
                deletable_reports = tuple(report_id for report_id in reports if report_id not in live_idempotency_report_ids)
                if retained_receipts:
                    # Keep only the content-free report identity required by
                    # the 24-hour idempotency FK. Replays return `expired`
                    # without re-exposing source data.
                    session.execute(delete(IntegrationAnalysisReference).where(
                        IntegrationAnalysisReference.organization_id == organization_id,
                        IntegrationAnalysisReference.report_id.in_(retained_receipts),
                    ).execution_options(synchronize_session=False))
                    session.execute(update(IntegrationAnalysisReport).where(
                        IntegrationAnalysisReport.organization_id == organization_id,
                        IntegrationAnalysisReport.id.in_(retained_receipts),
                    ).values(
                        title="已过期的分析草稿",
                        content_json={},
                        job_id=None,
                        job_version_id=None,
                        invalidated_at=current,
                        invalidation_reason="retention_expired",
                        updated_at=current,
                    ).execution_options(synchronize_session=False))
                session.execute(delete(IntegrationAnalysisReference).where(
                    IntegrationAnalysisReference.organization_id == organization_id,
                    IntegrationAnalysisReference.report_id.in_(deletable_reports),
                ).execution_options(synchronize_session=False))
                session.execute(delete(IntegrationAnalysisReport).where(
                    IntegrationAnalysisReport.organization_id == organization_id,
                    IntegrationAnalysisReport.id.in_(deletable_reports),
                ).execution_options(synchronize_session=False))
                removed += len(deletable_reports)
            for model in models[2:]:
                expired_ids = tuple(session.scalars(select(model.id).where(
                    model.organization_id == organization_id, model.expires_at <= current,
                ).order_by(model.id).limit(1000)).all())
                result = session.execute(delete(model).where(
                    model.organization_id == organization_id, model.id.in_(expired_ids),
                ).execution_options(synchronize_session=False))
                removed += max(0, result.rowcount)
            session.commit()
    return removed
