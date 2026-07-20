"""Read-only, platform-only reporting queries for the AI usage ledger.

This module deliberately returns *operational* metadata only.  It never
selects a business reference, candidate identifier, prompt, source document,
tool arguments, model output, raw provider response, or credential.  The
callers are expected to require a platform administrator before calling these
functions.

The tenant ORM guard is intentionally bypassed on each statement via
``skip_organization_scope``.  That is necessary for a platform administrator
to report across workspaces, but it also means these functions must not be
used by tenant-facing routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import AiModelProfile, AiRun, ApiInvocation


class AiUsageReportingError(ValueError):
    """A safe validation error for platform reporting filters."""


@dataclass(frozen=True, slots=True)
class AiUsageQuery:
    """Filters for a platform-owned AI usage report.

    ``started_at_from`` and ``started_at_to`` are inclusive and apply to the
    durable run start time.  ``organization_id=None`` intentionally means all
    workspaces, which is why this query is only valid after platform-admin
    authorization at the API boundary.
    """

    organization_id: str | None = None
    feature: str | None = None
    started_at_from: datetime | None = None
    started_at_to: datetime | None = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        if self.organization_id is not None and not self.organization_id.strip():
            raise AiUsageReportingError("ai_usage_organization_id_invalid")
        if self.feature is not None and not self.feature.strip():
            raise AiUsageReportingError("ai_usage_feature_invalid")
        if self.started_at_from and self.started_at_to:
            if self.started_at_from > self.started_at_to:
                raise AiUsageReportingError("ai_usage_date_range_invalid")
        if not 1 <= self.limit <= 500:
            raise AiUsageReportingError("ai_usage_limit_invalid")
        if self.offset < 0:
            raise AiUsageReportingError("ai_usage_offset_invalid")


@dataclass(frozen=True, slots=True)
class AiRunUsageSummary:
    """Safe per-run information for an AI usage list.

    ``run_id`` is an opaque ledger correlation ID.  No business resource ID,
    candidate information, input, or output is represented here.
    """

    run_id: str
    organization_id: str
    feature: str
    service_kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    total_cost_cny_micros: int | None
    cost_status: str
    invocation_count: int
    potentially_billed_invocation_count: int


@dataclass(frozen=True, slots=True)
class AiUsageAggregate:
    """Cost and call totals grouped by workspace, feature, and model.

    ``reported_cost_cny_micros`` includes only CNY invocation costs whose
    value is known.  It intentionally does not turn unknown cost into zero.
    The three run status counts make that distinction visible, while
    ``potentially_billed_invocation_count`` highlights uncertain failures such
    as a timeout after a provider may have accepted a request.
    """

    organization_id: str
    feature: str
    model_slug: str
    invocation_count: int
    costed_invocation_count: int
    unavailable_cost_invocation_count: int
    potentially_billed_invocation_count: int
    reported_cost_cny_micros: int
    known_run_count: int
    partial_run_count: int
    unavailable_run_count: int


def list_platform_ai_run_summaries(
    session: Session,
    *,
    query: AiUsageQuery,
) -> list[AiRunUsageSummary]:
    """List safe AI-run summaries for one workspace or the whole platform.

    The explicit execution option is a narrowly scoped read bypass.  It does
    not modify ``session.info`` and therefore cannot accidentally relax the
    tenant write guard for the caller's subsequent work.
    """

    potentially_billed_count = func.coalesce(
        func.sum(
            case(
                (ApiInvocation.may_have_billed.is_(True), 1),
                else_=0,
            )
        ),
        0,
    ).label("potentially_billed_invocation_count")

    statement = (
        select(
            AiRun.id.label("run_id"),
            AiRun.organization_id,
            AiRun.feature,
            AiRun.service_kind,
            AiRun.status,
            AiRun.started_at,
            AiRun.finished_at,
            AiRun.total_cost_reporting_micros.label("total_cost_cny_micros"),
            AiRun.cost_status,
            func.count(ApiInvocation.id).label("invocation_count"),
            potentially_billed_count,
        )
        .select_from(AiRun)
        .outerjoin(
            ApiInvocation,
            and_(
                ApiInvocation.ai_run_id == AiRun.id,
                ApiInvocation.organization_id == AiRun.organization_id,
            ),
        )
        .group_by(
            AiRun.id,
            AiRun.organization_id,
            AiRun.feature,
            AiRun.service_kind,
            AiRun.status,
            AiRun.started_at,
            AiRun.finished_at,
            AiRun.total_cost_reporting_micros,
            AiRun.cost_status,
        )
        .order_by(AiRun.started_at.desc(), AiRun.id.desc())
        .limit(query.limit)
        .offset(query.offset)
        # Platform-only read path.  See module-level security boundary.
        .execution_options(skip_organization_scope=True)
    )
    statement = _apply_run_filters(statement, query)
    rows = session.execute(statement).mappings()
    return [
        AiRunUsageSummary(
            run_id=str(row["run_id"]),
            organization_id=str(row["organization_id"]),
            feature=str(row["feature"]),
            service_kind=str(row["service_kind"]),
            status=str(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            total_cost_cny_micros=_optional_int(row["total_cost_cny_micros"]),
            cost_status=str(row["cost_status"]),
            invocation_count=int(row["invocation_count"] or 0),
            potentially_billed_invocation_count=int(
                row["potentially_billed_invocation_count"] or 0
            ),
        )
        for row in rows
    ]


def summarize_platform_ai_usage(
    session: Session,
    *,
    query: AiUsageQuery,
) -> list[AiUsageAggregate]:
    """Aggregate AI calls and CNY costs by workspace, feature, and model.

    Runs without an external invocation have no selected model and are
    intentionally absent from this model-level report.  They remain visible
    through :func:`list_platform_ai_run_summaries`.
    """

    has_cny_cost = and_(
        ApiInvocation.reporting_currency == "CNY",
        ApiInvocation.reporting_cost_micros.is_not(None),
    )
    known_run_count = func.count(
        func.distinct(case((AiRun.cost_status == "known", AiRun.id)))
    ).label("known_run_count")
    partial_run_count = func.count(
        func.distinct(case((AiRun.cost_status == "partial", AiRun.id)))
    ).label("partial_run_count")
    unavailable_run_count = func.count(
        func.distinct(
            case(
                (
                    AiRun.cost_status.not_in(("known", "partial")),
                    AiRun.id,
                )
            )
        )
    ).label("unavailable_run_count")

    statement = (
        select(
            AiRun.organization_id,
            AiRun.feature,
            AiModelProfile.slug.label("model_slug"),
            func.count(ApiInvocation.id).label("invocation_count"),
            func.coalesce(func.sum(case((has_cny_cost, 1), else_=0)), 0).label(
                "costed_invocation_count"
            ),
            func.coalesce(func.sum(case((has_cny_cost, 0), else_=1)), 0).label(
                "unavailable_cost_invocation_count"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (ApiInvocation.may_have_billed.is_(True), 1),
                        else_=0,
                    )
                ),
                0,
            ).label("potentially_billed_invocation_count"),
            func.coalesce(
                func.sum(
                    case(
                        (has_cny_cost, ApiInvocation.reporting_cost_micros),
                        else_=0,
                    )
                ),
                0,
            ).label("reported_cost_cny_micros"),
            known_run_count,
            partial_run_count,
            unavailable_run_count,
        )
        .select_from(AiRun)
        .join(
            ApiInvocation,
            and_(
                ApiInvocation.ai_run_id == AiRun.id,
                ApiInvocation.organization_id == AiRun.organization_id,
            ),
        )
        .join(AiModelProfile, AiModelProfile.id == ApiInvocation.model_profile_id)
        .group_by(AiRun.organization_id, AiRun.feature, AiModelProfile.slug)
        .order_by(AiRun.organization_id, AiRun.feature, AiModelProfile.slug)
        # Platform-only read path.  See module-level security boundary.
        .execution_options(skip_organization_scope=True)
    )
    statement = _apply_run_filters(statement, query)
    rows = session.execute(statement).mappings()
    return [
        AiUsageAggregate(
            organization_id=str(row["organization_id"]),
            feature=str(row["feature"]),
            model_slug=str(row["model_slug"]),
            invocation_count=int(row["invocation_count"] or 0),
            costed_invocation_count=int(row["costed_invocation_count"] or 0),
            unavailable_cost_invocation_count=int(
                row["unavailable_cost_invocation_count"] or 0
            ),
            potentially_billed_invocation_count=int(
                row["potentially_billed_invocation_count"] or 0
            ),
            reported_cost_cny_micros=int(row["reported_cost_cny_micros"] or 0),
            known_run_count=int(row["known_run_count"] or 0),
            partial_run_count=int(row["partial_run_count"] or 0),
            unavailable_run_count=int(row["unavailable_run_count"] or 0),
        )
        for row in rows
    ]


def _apply_run_filters(statement: object, query: AiUsageQuery) -> object:
    """Apply the common safe run filters without exposing business fields."""

    # SQLAlchemy's generic Select typing is deliberately kept out of the
    # public service contract; both call sites pass a Select over AiRun.
    if query.organization_id is not None:
        statement = statement.where(AiRun.organization_id == query.organization_id)
    if query.feature is not None:
        statement = statement.where(AiRun.feature == query.feature)
    if query.started_at_from is not None:
        statement = statement.where(AiRun.started_at >= query.started_at_from)
    if query.started_at_to is not None:
        statement = statement.where(AiRun.started_at <= query.started_at_to)
    return statement


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
