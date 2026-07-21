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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import AiModelProfile, AiProviderProfile, AiRun, ApiInvocation, utcnow


class AiUsageReportingError(ValueError):
    """A safe validation error for platform reporting filters."""


TrendGranularity = Literal["hour", "day"]

_DEFAULT_TREND_RANGE = timedelta(days=30)
_MAX_TREND_RANGE_BY_GRANULARITY: dict[TrendGranularity, timedelta] = {
    # Hourly series are intentionally capped more tightly: a platform-wide
    # report can fan out into one row per hour and Provider/model combination.
    "hour": timedelta(days=31),
    # Daily reporting remains useful for a quarter without letting the
    # endpoint become an unbounded historical export.
    "day": timedelta(days=90),
}

# ``ZoneInfo`` accepts IANA keys, but validating the key before resolving it
# keeps this public query parameter constrained to a safe, portable subset.
# The accepted form includes ordinary region names (``Asia/Shanghai``), UTC,
# and the ``Etc/GMT+8`` style identifiers that appear in the IANA database.
_IANA_TIME_ZONE_PATTERN = re.compile(
    r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$"
)


@dataclass(frozen=True, slots=True)
class AiUsageQuery:
    """Filters for a platform-owned AI usage report.

    ``started_at_from`` and ``started_at_to`` are inclusive.  Run-list queries
    apply them to the durable run start time; model-level usage aggregation
    applies them to the actual provider invocation start time, which is when
    the measured Token usage occurred.  ``provider_slug`` and ``model_slug``
    apply only to the model-level aggregation, because the run-list endpoint
    intentionally remains a model-agnostic operational view.
    ``organization_id=None`` intentionally means all workspaces, which is why
    this query is only valid after platform-admin authorization at the API
    boundary.
    """

    organization_id: str | None = None
    feature: str | None = None
    provider_slug: str | None = None
    model_slug: str | None = None
    started_at_from: datetime | None = None
    started_at_to: datetime | None = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        _validate_optional_filter(
            self.organization_id,
            code="ai_usage_organization_id_invalid",
        )
        _validate_optional_filter(self.feature, code="ai_usage_feature_invalid")
        _validate_optional_filter(
            self.provider_slug,
            code="ai_usage_provider_slug_invalid",
        )
        _validate_optional_filter(
            self.model_slug,
            code="ai_usage_model_slug_invalid",
        )
        if self.started_at_from and self.started_at_to:
            if self.started_at_from > self.started_at_to:
                raise AiUsageReportingError("ai_usage_date_range_invalid")
        if not 1 <= self.limit <= 500:
            raise AiUsageReportingError("ai_usage_limit_invalid")
        if self.offset < 0:
            raise AiUsageReportingError("ai_usage_offset_invalid")


@dataclass(frozen=True, slots=True)
class AiUsageTrendQuery:
    """Validated, bounded filters for a platform Token usage trend.

    The trend is intentionally an operational chart rather than a ledger
    export.  It uses ``ApiInvocation.started_at`` exclusively, so each Token
    is counted in the interval in which the provider call happened.  When no
    range is supplied it returns the latest 30 days; callers must provide both
    endpoints for a custom interval and the permitted span depends on the
    selected bucket size.
    """

    organization_id: str | None = None
    feature: str | None = None
    provider_slug: str | None = None
    model_slug: str | None = None
    started_at_from: datetime | None = None
    started_at_to: datetime | None = None
    granularity: TrendGranularity = "day"
    # The query boundaries remain absolute instants.  This IANA zone controls
    # only the civil calendar used to build chart buckets and is echoed in
    # each result so clients do not accidentally render a UTC day as a local
    # day.  UTC remains the backwards-compatible default for callers that
    # have not yet supplied their browser zone.
    time_zone: str = "UTC"
    _bucket_time_zone: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_optional_filter(
            self.organization_id,
            code="ai_usage_organization_id_invalid",
        )
        _validate_optional_filter(self.feature, code="ai_usage_feature_invalid")
        _validate_optional_filter(
            self.provider_slug,
            code="ai_usage_provider_slug_invalid",
        )
        _validate_optional_filter(
            self.model_slug,
            code="ai_usage_model_slug_invalid",
        )
        if self.granularity not in _MAX_TREND_RANGE_BY_GRANULARITY:
            raise AiUsageReportingError("ai_usage_trend_granularity_invalid")

        bucket_time_zone = _resolve_trend_time_zone(self.time_zone)

        started_at_from = _as_utc(self.started_at_from)
        started_at_to = _as_utc(self.started_at_to)
        if started_at_from is None and started_at_to is None:
            started_at_to = utcnow()
            started_at_from = started_at_to - _DEFAULT_TREND_RANGE
        elif started_at_from is None or started_at_to is None:
            raise AiUsageReportingError("ai_usage_trend_date_range_incomplete")

        assert started_at_from is not None
        assert started_at_to is not None
        if started_at_from > started_at_to:
            raise AiUsageReportingError("ai_usage_date_range_invalid")
        if (
            started_at_to - started_at_from
            > _MAX_TREND_RANGE_BY_GRANULARITY[self.granularity]
        ):
            raise AiUsageReportingError("ai_usage_trend_date_range_too_large")

        # Store the resolved, UTC-normalized interval back on the immutable
        # value object.  This keeps SQL comparison semantics stable across
        # SQLite tests and PostgreSQL production deployments.
        object.__setattr__(self, "started_at_from", started_at_from)
        object.__setattr__(self, "started_at_to", started_at_to)
        object.__setattr__(self, "time_zone", bucket_time_zone.key)
        object.__setattr__(self, "_bucket_time_zone", bucket_time_zone)

    @property
    def bucket_time_zone(self) -> ZoneInfo:
        """The validated IANA zone used for calendar bucketing."""

        return self._bucket_time_zone


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
    token_usage_invocation_count: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AiUsageAggregate:
    """Usage and call totals grouped by workspace, feature, Provider, and model.

    ``reported_cost_cny_micros`` includes only CNY invocation costs whose
    value is known.  It intentionally does not turn unknown cost into zero.
    The three run status counts make that distinction visible, while
    ``potentially_billed_invocation_count`` highlights uncertain failures such
    as a timeout after a provider may have accepted a request.
    """

    organization_id: str
    feature: str
    provider_slug: str
    model_slug: str
    invocation_count: int
    costed_invocation_count: int
    unavailable_cost_invocation_count: int
    potentially_billed_invocation_count: int
    reported_cost_cny_micros: int
    token_usage_invocation_count: int
    input_tokens: int
    cached_read_input_tokens: int
    cached_write_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    known_run_count: int
    partial_run_count: int
    unavailable_run_count: int


@dataclass(frozen=True, slots=True)
class AiUsageTrendBucket:
    """One Provider/model Token bucket for a platform usage chart.

    This deliberately contains no price, cost, business reference, prompt,
    candidate, or source-document field.  A bucket has data only when at
    least one external invocation exists; clients may render missing periods
    as zero without treating them as provider-reported usage.
    """

    # The UTC instant at which the requested timezone's local civil bucket
    # begins.  Clients can safely pass it to a normal ``Date`` formatter.
    bucket_started_at: datetime
    time_zone: str
    provider_slug: str
    model_slug: str
    invocation_count: int
    token_usage_invocation_count: int
    input_tokens: int
    cached_read_input_tokens: int
    cached_write_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int


def _has_provider_usage() -> object:
    """Return the durable condition for provider-reported metering.

    Token columns are nullable because historical or failed calls may not have
    returned a usage object.  The gateway writes ``usage_source='provider'``
    only after a provider returned a recognized usage payload, so this is more
    precise than treating a numeric zero as missing data.
    """

    return ApiInvocation.usage_source == "provider"


def _provider_usage_invocation_count() -> object:
    return func.coalesce(
        func.sum(case((_has_provider_usage(), 1), else_=0)),
        0,
    ).label("token_usage_invocation_count")


def _sum_provider_token_bucket(column: object, *, label: str) -> object:
    """Sum a disjoint token bucket only when the provider reported usage."""

    return func.coalesce(
        func.sum(
            case(
                (_has_provider_usage(), func.coalesce(column, 0)),
                else_=0,
            )
        ),
        0,
    ).label(label)


def _metered_token_total_expression() -> object:
    """Return the ledger's non-overlapping total-token expression.

    Cached and reasoning quantities are stored in dedicated buckets and
    removed from input/output at gateway normalization time.  Summing these
    buckets therefore matches the quantity used by the cost ledger without
    charging the same token twice.
    """

    return (
        func.coalesce(ApiInvocation.input_tokens, 0)
        + func.coalesce(ApiInvocation.cached_read_input_tokens, 0)
        + func.coalesce(ApiInvocation.cached_write_input_tokens, 0)
        + func.coalesce(ApiInvocation.output_tokens, 0)
        + func.coalesce(ApiInvocation.reasoning_tokens, 0)
    )


def _sum_provider_total_tokens() -> object:
    return func.coalesce(
        func.sum(
            case(
                (_has_provider_usage(), _metered_token_total_expression()),
                else_=0,
            )
        ),
        0,
    ).label("total_tokens")


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
    token_usage_invocation_count = _provider_usage_invocation_count()
    total_tokens = _sum_provider_total_tokens()

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
            token_usage_invocation_count,
            total_tokens,
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
            token_usage_invocation_count=int(
                row["token_usage_invocation_count"] or 0
            ),
            total_tokens=int(row["total_tokens"] or 0),
        )
        for row in rows
    ]


def summarize_platform_ai_usage(
    session: Session,
    *,
    query: AiUsageQuery,
) -> list[AiUsageAggregate]:
    """Aggregate AI calls and Token usage by workspace, feature, Provider, and model.

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
    token_usage_invocation_count = _provider_usage_invocation_count()
    input_tokens = _sum_provider_token_bucket(
        ApiInvocation.input_tokens,
        label="input_tokens",
    )
    cached_read_input_tokens = _sum_provider_token_bucket(
        ApiInvocation.cached_read_input_tokens,
        label="cached_read_input_tokens",
    )
    cached_write_input_tokens = _sum_provider_token_bucket(
        ApiInvocation.cached_write_input_tokens,
        label="cached_write_input_tokens",
    )
    output_tokens = _sum_provider_token_bucket(
        ApiInvocation.output_tokens,
        label="output_tokens",
    )
    reasoning_tokens = _sum_provider_token_bucket(
        ApiInvocation.reasoning_tokens,
        label="reasoning_tokens",
    )
    total_tokens = _sum_provider_total_tokens()

    statement = (
        select(
            AiRun.organization_id,
            AiRun.feature,
            AiProviderProfile.slug.label("provider_slug"),
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
            token_usage_invocation_count,
            input_tokens,
            cached_read_input_tokens,
            cached_write_input_tokens,
            output_tokens,
            reasoning_tokens,
            total_tokens,
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
        .join(
            AiProviderProfile,
            AiProviderProfile.id == ApiInvocation.provider_profile_id,
        )
        .group_by(
            AiRun.organization_id,
            AiRun.feature,
            AiProviderProfile.slug,
            AiModelProfile.slug,
        )
        .order_by(
            AiRun.organization_id,
            AiRun.feature,
            AiProviderProfile.slug,
            AiModelProfile.slug,
        )
        # Platform-only read path.  See module-level security boundary.
        .execution_options(skip_organization_scope=True)
    )
    statement = _apply_model_usage_filters(statement, query)
    rows = session.execute(statement).mappings()
    return [
        AiUsageAggregate(
            organization_id=str(row["organization_id"]),
            feature=str(row["feature"]),
            provider_slug=str(row["provider_slug"]),
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
            token_usage_invocation_count=int(
                row["token_usage_invocation_count"] or 0
            ),
            input_tokens=int(row["input_tokens"] or 0),
            cached_read_input_tokens=int(row["cached_read_input_tokens"] or 0),
            cached_write_input_tokens=int(
                row["cached_write_input_tokens"] or 0
            ),
            output_tokens=int(row["output_tokens"] or 0),
            reasoning_tokens=int(row["reasoning_tokens"] or 0),
            total_tokens=int(row["total_tokens"] or 0),
            known_run_count=int(row["known_run_count"] or 0),
            partial_run_count=int(row["partial_run_count"] or 0),
            unavailable_run_count=int(row["unavailable_run_count"] or 0),
        )
        for row in rows
    ]


def summarize_platform_ai_usage_trend(
    session: Session,
    *,
    query: AiUsageTrendQuery,
) -> list[AiUsageTrendBucket]:
    """Return bounded Provider/model Token totals grouped into time buckets.

    This is deliberately separate from the general summary endpoint: a chart
    needs a calendar bucket, but it must preserve the same per-Provider/model
    identity as the summary table.  All time filtering and grouping use the
    immutable provider invocation start time, never the enclosing business run
    start time.

    The query interval is made of UTC-normalized instants, while bucket
    boundaries are made in the caller's explicit IANA ``time_zone``.  This is
    intentionally not left to the database connection's session timezone:
    platform reports must not move a candidate day merely because a worker or
    database server runs in a different locale.

    PostgreSQL performs the bounded aggregation in SQL with ``timezone`` and
    ``date_trunc``.  SQLite has no IANA timezone database, so the test/local
    path streams the minimal metering columns and groups them in Python.  Both
    paths share the same civil-time semantics, including non-whole-hour zones
    such as ``Asia/Kathmandu``.
    """

    if _database_dialect_name(session) == "postgresql":
        return _summarize_platform_ai_usage_trend_postgresql(session, query=query)
    return _summarize_platform_ai_usage_trend_in_python(session, query=query)


def _summarize_platform_ai_usage_trend_postgresql(
    session: Session,
    *,
    query: AiUsageTrendQuery,
) -> list[AiUsageTrendBucket]:
    """Use PostgreSQL's IANA timezone data for the production aggregation."""

    rows = session.execute(
        _postgresql_trend_statement(query)
    ).mappings()
    buckets: list[AiUsageTrendBucket] = []
    for row in rows:
        database_bucket = row["bucket_started_at"]
        if not isinstance(database_bucket, datetime):
            raise RuntimeError("ai_usage_trend_bucket_timestamp_invalid")
        # ``timezone(text, timestamptz)`` yields a local timestamp without a
        # tzinfo.  Reattaching the validated IANA zone lets us return the UTC
        # instant of the local civil bucket's start, which is safe for normal
        # browser ``Date`` formatting and does not depend on server locale.
        bucket_started_at = database_bucket.replace(
            tzinfo=query.bucket_time_zone
        ).astimezone(timezone.utc)
        buckets.append(
            _trend_bucket_from_aggregate_row(
                row,
                bucket_started_at=bucket_started_at,
                time_zone=query.time_zone,
            )
        )
    return buckets


def _summarize_platform_ai_usage_trend_in_python(
    session: Session,
    *,
    query: AiUsageTrendQuery,
) -> list[AiUsageTrendBucket]:
    """Use a streaming, timezone-correct fallback for SQLite and local tests.

    SQLite deliberately ships without the IANA rule database, so attempting
    to translate named zones in SQL would silently produce server-local or
    UTC buckets.  The result set contains only metering fields and is streamed
    to keep the bounded fallback from materializing invocation history.
    """

    statement = (
        select(
            ApiInvocation.started_at.label("started_at"),
            AiProviderProfile.slug.label("provider_slug"),
            AiModelProfile.slug.label("model_slug"),
            ApiInvocation.usage_source,
            ApiInvocation.input_tokens,
            ApiInvocation.cached_read_input_tokens,
            ApiInvocation.cached_write_input_tokens,
            ApiInvocation.output_tokens,
            ApiInvocation.reasoning_tokens,
        )
        .select_from(ApiInvocation)
        .join(
            AiRun,
            and_(
                AiRun.id == ApiInvocation.ai_run_id,
                AiRun.organization_id == ApiInvocation.organization_id,
            ),
        )
        .join(AiModelProfile, AiModelProfile.id == ApiInvocation.model_profile_id)
        .join(
            AiProviderProfile,
            AiProviderProfile.id == ApiInvocation.provider_profile_id,
        )
        .order_by(
            ApiInvocation.started_at,
            AiProviderProfile.slug,
            AiModelProfile.slug,
        )
        # Platform-only read path.  See module-level security boundary.
        .execution_options(skip_organization_scope=True, stream_results=True)
    )
    statement = _apply_model_usage_filters(statement, query)
    accumulators: dict[tuple[object, ...], _TrendBucketAccumulator] = {}
    rows = session.execute(statement).mappings().yield_per(1_000)
    for row in rows:
        started_at = row["started_at"]
        if not isinstance(started_at, datetime):
            raise RuntimeError("ai_usage_trend_invocation_timestamp_invalid")
        local_bucket_started_at = _local_trend_bucket_started_at(
            started_at,
            granularity=query.granularity,
            bucket_time_zone=query.bucket_time_zone,
        )
        provider_slug = str(row["provider_slug"])
        model_slug = str(row["model_slug"])
        key = _trend_bucket_key(
            local_bucket_started_at,
            granularity=query.granularity,
            provider_slug=provider_slug,
            model_slug=model_slug,
        )
        accumulator = accumulators.get(key)
        if accumulator is None:
            accumulator = _TrendBucketAccumulator(
                bucket_started_at=local_bucket_started_at.astimezone(timezone.utc),
                time_zone=query.time_zone,
                provider_slug=provider_slug,
                model_slug=model_slug,
            )
            accumulators[key] = accumulator
        accumulator.add_invocation(row)

    return [
        accumulator.as_bucket()
        for accumulator in sorted(
            accumulators.values(),
            key=lambda value: (
                value.bucket_started_at,
                value.provider_slug,
                value.model_slug,
            ),
        )
    ]


@dataclass(slots=True)
class _TrendBucketAccumulator:
    """Mutable metering-only accumulator for the SQLite fallback."""

    bucket_started_at: datetime
    time_zone: str
    provider_slug: str
    model_slug: str
    invocation_count: int = 0
    token_usage_invocation_count: int = 0
    input_tokens: int = 0
    cached_read_input_tokens: int = 0
    cached_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def add_invocation(self, row: Mapping[str, object]) -> None:
        self.invocation_count += 1
        if row["usage_source"] != "provider":
            return

        self.token_usage_invocation_count += 1
        input_tokens = _token_value(row["input_tokens"])
        cached_read_input_tokens = _token_value(row["cached_read_input_tokens"])
        cached_write_input_tokens = _token_value(row["cached_write_input_tokens"])
        output_tokens = _token_value(row["output_tokens"])
        reasoning_tokens = _token_value(row["reasoning_tokens"])
        self.input_tokens += input_tokens
        self.cached_read_input_tokens += cached_read_input_tokens
        self.cached_write_input_tokens += cached_write_input_tokens
        self.output_tokens += output_tokens
        self.reasoning_tokens += reasoning_tokens
        self.total_tokens += (
            input_tokens
            + cached_read_input_tokens
            + cached_write_input_tokens
            + output_tokens
            + reasoning_tokens
        )

    def as_bucket(self) -> AiUsageTrendBucket:
        return AiUsageTrendBucket(
            bucket_started_at=self.bucket_started_at,
            time_zone=self.time_zone,
            provider_slug=self.provider_slug,
            model_slug=self.model_slug,
            invocation_count=self.invocation_count,
            token_usage_invocation_count=self.token_usage_invocation_count,
            input_tokens=self.input_tokens,
            cached_read_input_tokens=self.cached_read_input_tokens,
            cached_write_input_tokens=self.cached_write_input_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
        )


def _postgresql_trend_statement(query: AiUsageTrendQuery) -> object:
    """Build a PostgreSQL-only statement with parameterized IANA conversion."""

    # PostgreSQL's ``timezone`` resolves DST rules from its IANA database.  The
    # zone is a validated, bound value (never an interpolated SQL fragment).
    local_started_at = func.timezone(query.time_zone, ApiInvocation.started_at)
    bucket_started_at = func.date_trunc(
        query.granularity,
        local_started_at,
    ).label("bucket_started_at")
    token_usage_invocation_count = _provider_usage_invocation_count()
    input_tokens = _sum_provider_token_bucket(
        ApiInvocation.input_tokens,
        label="input_tokens",
    )
    cached_read_input_tokens = _sum_provider_token_bucket(
        ApiInvocation.cached_read_input_tokens,
        label="cached_read_input_tokens",
    )
    cached_write_input_tokens = _sum_provider_token_bucket(
        ApiInvocation.cached_write_input_tokens,
        label="cached_write_input_tokens",
    )
    output_tokens = _sum_provider_token_bucket(
        ApiInvocation.output_tokens,
        label="output_tokens",
    )
    reasoning_tokens = _sum_provider_token_bucket(
        ApiInvocation.reasoning_tokens,
        label="reasoning_tokens",
    )
    total_tokens = _sum_provider_total_tokens()

    statement = (
        select(
            bucket_started_at,
            AiProviderProfile.slug.label("provider_slug"),
            AiModelProfile.slug.label("model_slug"),
            func.count(ApiInvocation.id).label("invocation_count"),
            token_usage_invocation_count,
            input_tokens,
            cached_read_input_tokens,
            cached_write_input_tokens,
            output_tokens,
            reasoning_tokens,
            total_tokens,
        )
        .select_from(ApiInvocation)
        .join(
            AiRun,
            and_(
                AiRun.id == ApiInvocation.ai_run_id,
                AiRun.organization_id == ApiInvocation.organization_id,
            ),
        )
        .join(AiModelProfile, AiModelProfile.id == ApiInvocation.model_profile_id)
        .join(
            AiProviderProfile,
            AiProviderProfile.id == ApiInvocation.provider_profile_id,
        )
        .group_by(
            bucket_started_at,
            AiProviderProfile.slug,
            AiModelProfile.slug,
        )
        .order_by(
            bucket_started_at,
            AiProviderProfile.slug,
            AiModelProfile.slug,
        )
        # Platform-only read path.  See module-level security boundary.
        .execution_options(skip_organization_scope=True)
    )
    return _apply_model_usage_filters(statement, query)


def _trend_bucket_from_aggregate_row(
    row: Mapping[str, object],
    *,
    bucket_started_at: datetime,
    time_zone: str,
) -> AiUsageTrendBucket:
    return AiUsageTrendBucket(
        bucket_started_at=bucket_started_at,
        time_zone=time_zone,
        provider_slug=str(row["provider_slug"]),
        model_slug=str(row["model_slug"]),
        invocation_count=int(row["invocation_count"] or 0),
        token_usage_invocation_count=int(row["token_usage_invocation_count"] or 0),
        input_tokens=int(row["input_tokens"] or 0),
        cached_read_input_tokens=int(row["cached_read_input_tokens"] or 0),
        cached_write_input_tokens=int(row["cached_write_input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        reasoning_tokens=int(row["reasoning_tokens"] or 0),
        total_tokens=int(row["total_tokens"] or 0),
    )


def _local_trend_bucket_started_at(
    started_at: datetime,
    *,
    granularity: TrendGranularity,
    bucket_time_zone: ZoneInfo,
) -> datetime:
    """Floor an invocation instant to its local civil chart bucket."""

    local_started_at = _as_utc(started_at).astimezone(bucket_time_zone)
    if granularity == "day":
        return datetime(
            local_started_at.year,
            local_started_at.month,
            local_started_at.day,
            tzinfo=bucket_time_zone,
        )
    return datetime(
        local_started_at.year,
        local_started_at.month,
        local_started_at.day,
        local_started_at.hour,
        tzinfo=bucket_time_zone,
    )


def _trend_bucket_key(
    bucket_started_at: datetime,
    *,
    granularity: TrendGranularity,
    provider_slug: str,
    model_slug: str,
) -> tuple[object, ...]:
    """Key a local civil bucket in the same way as PostgreSQL ``date_trunc``.

    On the autumn DST transition two absolute hours can share one local clock
    hour.  They intentionally aggregate into that one civil-hour chart point,
    matching the PostgreSQL path and avoiding duplicate, visually ambiguous
    01:00 labels.
    """

    calendar_parts: tuple[int, ...]
    if granularity == "hour":
        calendar_parts = (
            bucket_started_at.year,
            bucket_started_at.month,
            bucket_started_at.day,
            bucket_started_at.hour,
        )
    else:
        calendar_parts = (
            bucket_started_at.year,
            bucket_started_at.month,
            bucket_started_at.day,
        )
    return (*calendar_parts, provider_slug, model_slug)


def _database_dialect_name(session: Session) -> str:
    return session.get_bind().dialect.name


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


def _apply_model_usage_filters(
    statement: object,
    query: AiUsageQuery | AiUsageTrendQuery,
) -> object:
    """Apply platform filters to model usage without blurring time semantics.

    Workspace and feature remain properties of the durable AI run.  The date
    range deliberately uses the provider invocation timestamp so a long-lived
    run is counted in the period where its measured Token use actually
    happened.
    """

    if query.organization_id is not None:
        statement = statement.where(AiRun.organization_id == query.organization_id)
    if query.feature is not None:
        statement = statement.where(AiRun.feature == query.feature)
    if query.provider_slug is not None:
        statement = statement.where(AiProviderProfile.slug == query.provider_slug)
    if query.model_slug is not None:
        statement = statement.where(AiModelProfile.slug == query.model_slug)
    if query.started_at_from is not None:
        statement = statement.where(ApiInvocation.started_at >= query.started_at_from)
    if query.started_at_to is not None:
        statement = statement.where(ApiInvocation.started_at <= query.started_at_to)
    return statement


def _validate_optional_filter(value: str | None, *, code: str) -> None:
    if value is not None and not value.strip():
        raise AiUsageReportingError(code)


def _resolve_trend_time_zone(value: str) -> ZoneInfo:
    """Resolve one safe IANA timezone key for a trend query."""

    if (
        not value
        or len(value) > 64
        or value != value.strip()
        or _IANA_TIME_ZONE_PATTERN.fullmatch(value) is None
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise AiUsageReportingError("ai_usage_trend_time_zone_invalid")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise AiUsageReportingError("ai_usage_trend_time_zone_invalid") from exc


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _token_value(value: object) -> int:
    return int(value or 0)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
