from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models import AiModelProfile, AiProviderProfile, AiRun, ApiInvocation, Organization
from app.services.ai_usage_reporting_service import (
    AiUsageQuery,
    AiUsageReportingError,
    AiUsageTrendQuery,
    _postgresql_trend_statement,
    list_platform_ai_run_summaries,
    summarize_platform_ai_usage,
    summarize_platform_ai_usage_trend,
)
from app.tenant_scope import clear_organization_context, set_organization_context


UTC = timezone.utc
ORG_A_ID = "00000000-0000-4000-8000-0000000000a1"
ORG_B_ID = "00000000-0000-4000-8000-0000000000b2"


def _at(day: int, hour: int = 9, minute: int = 30) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


def _seed_usage_ledger(session: Session) -> None:
    provider = AiProviderProfile(
        slug="usage-reporting-provider",
        display_name="Usage reporting provider",
        driver="openai_compatible",
        base_url="https://provider.example.test/v1/chat/completions",
        credential_ref="usage-reporting-provider-credential",
    )
    model_primary = AiModelProfile(
        provider_profile=provider,
        slug="usage-reporting-primary",
        display_name="Usage reporting primary",
        provider_model_id="provider-primary",
    )
    model_secondary = AiModelProfile(
        provider_profile=provider,
        slug="usage-reporting-secondary",
        display_name="Usage reporting secondary",
        provider_model_id="provider-secondary",
    )
    organization_a = Organization(id=ORG_A_ID, name="Usage reporting workspace A")
    organization_b = Organization(id=ORG_B_ID, name="Usage reporting workspace B")
    session.add_all([provider, model_primary, model_secondary, organization_a, organization_b])
    session.commit()

    set_organization_context(session, ORG_A_ID)
    known_run = AiRun(
        feature="resume_score",
        service_kind="llm",
        business_ref_type="fixture",
        business_ref_id="a-score-private-reference",
        status="succeeded",
        started_at=_at(2),
        finished_at=_at(2),
        total_cost_reporting_micros=120,
        reporting_currency="CNY",
        cost_status="known",
    )
    session.add(known_run)
    session.flush()
    session.add(
        ApiInvocation(
            ai_run_id=known_run.id,
            attempt_no=1,
            provider_profile_id=provider.id,
            model_profile_id=model_primary.id,
            provider_driver="openai_compatible",
            provider_model_id="provider-primary",
            status="succeeded",
            started_at=_at(2),
            completed_at=_at(2),
            reporting_cost_micros=120,
            reporting_currency="CNY",
            cost_source="price_snapshot",
            usage_source="provider",
            input_tokens=100,
            cached_read_input_tokens=10,
            cached_write_input_tokens=5,
            output_tokens=20,
            reasoning_tokens=3,
        )
    )

    partial_run = AiRun(
        feature="jd_match",
        service_kind="llm",
        business_ref_type="fixture",
        business_ref_id="a-match-private-reference",
        status="failed",
        started_at=_at(3),
        finished_at=_at(3),
        total_cost_reporting_micros=30,
        reporting_currency="CNY",
        cost_status="partial",
    )
    session.add(partial_run)
    session.flush()
    session.add_all(
        [
            ApiInvocation(
                ai_run_id=partial_run.id,
                attempt_no=1,
                provider_profile_id=provider.id,
                model_profile_id=model_secondary.id,
                provider_driver="openai_compatible",
                provider_model_id="provider-secondary",
                status="succeeded",
                started_at=_at(3),
                completed_at=_at(3),
                reporting_cost_micros=30,
                reporting_currency="CNY",
                cost_source="price_snapshot",
                usage_source="provider",
                input_tokens=30,
                cached_read_input_tokens=2,
                cached_write_input_tokens=0,
                output_tokens=15,
                reasoning_tokens=5,
            ),
            ApiInvocation(
                ai_run_id=partial_run.id,
                attempt_no=2,
                provider_profile_id=provider.id,
                model_profile_id=model_secondary.id,
                provider_driver="openai_compatible",
                provider_model_id="provider-secondary",
                status="failed",
                may_have_billed=True,
                started_at=_at(3),
                reporting_currency="CNY",
                cost_source="unavailable",
            ),
        ]
    )
    session.commit()

    set_organization_context(session, ORG_B_ID)
    other_tenant_run = AiRun(
        feature="resume_score",
        service_kind="llm",
        business_ref_type="fixture",
        business_ref_id="b-score-private-reference",
        status="succeeded",
        started_at=_at(4),
        finished_at=_at(4),
        total_cost_reporting_micros=700,
        reporting_currency="CNY",
        cost_status="known",
    )
    session.add(other_tenant_run)
    session.flush()
    session.add(
        ApiInvocation(
            ai_run_id=other_tenant_run.id,
            attempt_no=1,
            provider_profile_id=provider.id,
            model_profile_id=model_primary.id,
            provider_driver="openai_compatible",
            provider_model_id="provider-primary",
            status="succeeded",
            started_at=_at(4),
            completed_at=_at(4),
            reporting_cost_micros=700,
            reporting_currency="CNY",
            cost_source="price_snapshot",
            # A provider may legitimately return a zero-token usage payload.
            # This must remain distinct from a provider that returned no
            # metering payload at all.
            usage_source="provider",
            input_tokens=0,
            cached_read_input_tokens=0,
            cached_write_input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
        )
    )
    session.commit()
    clear_organization_context(session)


def test_platform_reporting_is_explicitly_global_but_can_filter_one_workspace(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)

        # Deliberately bind the session to the other tenant first.  The
        # reporting queries must use their statement-level platform bypass,
        # otherwise the request would silently return only organization B.
        set_organization_context(session, ORG_B_ID)
        summaries_for_a = list_platform_ai_run_summaries(
            session,
            query=AiUsageQuery(organization_id=ORG_A_ID),
        )
        assert {(row.organization_id, row.feature) for row in summaries_for_a} == {
            (ORG_A_ID, "resume_score"),
            (ORG_A_ID, "jd_match"),
        }
        assert all("business_ref" not in row.__dataclass_fields__ for row in summaries_for_a)
        assert all("prompt" not in row.__dataclass_fields__ for row in summaries_for_a)
        assert all("output" not in row.__dataclass_fields__ for row in summaries_for_a)

        summaries_by_feature = {row.feature: row for row in summaries_for_a}
        score_summary = summaries_by_feature["resume_score"]
        assert score_summary.invocation_count == 1
        assert score_summary.token_usage_invocation_count == 1
        assert score_summary.total_tokens == 138

        match_summary = summaries_by_feature["jd_match"]
        assert match_summary.invocation_count == 2
        assert match_summary.token_usage_invocation_count == 1
        assert match_summary.total_tokens == 52

        all_summaries = list_platform_ai_run_summaries(session, query=AiUsageQuery())
        assert {row.organization_id for row in all_summaries} == {ORG_A_ID, ORG_B_ID}

        date_limited = list_platform_ai_run_summaries(
            session,
            query=AiUsageQuery(
                started_at_from=_at(2),
                started_at_to=_at(3),
            ),
        )
        assert {row.organization_id for row in date_limited} == {ORG_A_ID}


def test_platform_reporting_aggregates_cost_status_and_uncertain_billing(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)

        aggregates = summarize_platform_ai_usage(
            session,
            query=AiUsageQuery(organization_id=ORG_A_ID),
        )
        by_feature = {row.feature: row for row in aggregates}

        score = by_feature["resume_score"]
        assert score.organization_id == ORG_A_ID
        assert score.provider_slug == "usage-reporting-provider"
        assert score.model_slug == "usage-reporting-primary"
        assert score.invocation_count == 1
        assert score.costed_invocation_count == 1
        assert score.unavailable_cost_invocation_count == 0
        assert score.reported_cost_cny_micros == 120
        assert score.known_run_count == 1
        assert score.partial_run_count == 0
        assert score.unavailable_run_count == 0
        assert score.potentially_billed_invocation_count == 0
        assert score.token_usage_invocation_count == 1
        assert score.input_tokens == 100
        assert score.cached_read_input_tokens == 10
        assert score.cached_write_input_tokens == 5
        assert score.output_tokens == 20
        assert score.reasoning_tokens == 3
        assert score.total_tokens == 138

        match = by_feature["jd_match"]
        assert match.organization_id == ORG_A_ID
        assert match.provider_slug == "usage-reporting-provider"
        assert match.model_slug == "usage-reporting-secondary"
        assert match.invocation_count == 2
        assert match.costed_invocation_count == 1
        assert match.unavailable_cost_invocation_count == 1
        assert match.reported_cost_cny_micros == 30
        assert match.known_run_count == 0
        assert match.partial_run_count == 1
        assert match.unavailable_run_count == 0
        assert match.potentially_billed_invocation_count == 1
        assert match.token_usage_invocation_count == 1
        assert match.input_tokens == 30
        assert match.cached_read_input_tokens == 2
        assert match.cached_write_input_tokens == 0
        assert match.output_tokens == 15
        assert match.reasoning_tokens == 5
        assert match.total_tokens == 52

        all_aggregates = summarize_platform_ai_usage(session, query=AiUsageQuery())
        assert {(row.organization_id, row.reported_cost_cny_micros) for row in all_aggregates} == {
            (ORG_A_ID, 120),
            (ORG_A_ID, 30),
            (ORG_B_ID, 700),
        }
        other_workspace_score = next(
            row
            for row in all_aggregates
            if row.organization_id == ORG_B_ID and row.feature == "resume_score"
        )
        assert other_workspace_score.token_usage_invocation_count == 1
        assert other_workspace_score.total_tokens == 0


def test_platform_usage_summary_filters_tokens_by_provider_invocation_time(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)
        set_organization_context(session, ORG_A_ID)
        score_run = session.scalar(
            select(AiRun).where(
                AiRun.organization_id == ORG_A_ID,
                AiRun.feature == "resume_score",
            )
        )
        assert score_run is not None
        score_invocation = session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == score_run.id)
        )
        assert score_invocation is not None
        # The durable run was created on day 2, but its provider call happened
        # on day 5.  A Token report must follow the call, not the run shell.
        score_invocation.started_at = _at(5)
        session.commit()

        aggregates = summarize_platform_ai_usage(
            session,
            query=AiUsageQuery(started_at_from=_at(5), started_at_to=_at(5)),
        )

        assert [(row.provider_slug, row.model_slug, row.total_tokens) for row in aggregates] == [
            ("usage-reporting-provider", "usage-reporting-primary", 138),
        ]
        assert list_platform_ai_run_summaries(
            session,
            query=AiUsageQuery(started_at_from=_at(5), started_at_to=_at(5)),
        ) == []


def test_platform_usage_summary_keeps_provider_model_token_rows_separate(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)
        set_organization_context(session, ORG_A_ID)
        alternate_provider = AiProviderProfile(
            slug="usage-reporting-alternate-provider",
            display_name="Usage reporting alternate provider",
            driver="openai_compatible",
            base_url="https://alternate-provider.example.test/v1/chat/completions",
            credential_ref="usage-reporting-alternate-credential",
        )
        alternate_model = AiModelProfile(
            provider_profile=alternate_provider,
            slug="usage-reporting-alternate-model",
            display_name="Usage reporting alternate model",
            provider_model_id="alternate-model",
        )
        session.add_all([alternate_provider, alternate_model])
        session.flush()
        score_run = session.scalar(
            select(AiRun).where(
                AiRun.organization_id == ORG_A_ID,
                AiRun.feature == "resume_score",
            )
        )
        assert score_run is not None
        session.add(
            ApiInvocation(
                ai_run_id=score_run.id,
                attempt_no=2,
                provider_profile_id=alternate_provider.id,
                model_profile_id=alternate_model.id,
                provider_driver="openai_compatible",
                provider_model_id="alternate-model",
                status="succeeded",
                started_at=_at(2),
                completed_at=_at(2),
                usage_source="provider",
                input_tokens=7,
                cached_read_input_tokens=2,
                cached_write_input_tokens=1,
                output_tokens=11,
                reasoning_tokens=4,
            )
        )
        session.commit()

        aggregates = summarize_platform_ai_usage(
            session,
            query=AiUsageQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
            ),
        )

        assert [
            (
                row.provider_slug,
                row.model_slug,
                row.invocation_count,
                row.total_tokens,
            )
            for row in aggregates
        ] == [
            ("usage-reporting-alternate-provider", "usage-reporting-alternate-model", 1, 25),
            ("usage-reporting-provider", "usage-reporting-primary", 1, 138),
        ]

        alternate_only = summarize_platform_ai_usage(
            session,
            query=AiUsageQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
                provider_slug="usage-reporting-alternate-provider",
                model_slug="usage-reporting-alternate-model",
            ),
        )
        assert [
            (row.provider_slug, row.model_slug, row.total_tokens)
            for row in alternate_only
        ] == [
            ("usage-reporting-alternate-provider", "usage-reporting-alternate-model", 25)
        ]


def test_platform_usage_trend_uses_invocation_time_and_keeps_models_separate(
    ai_client,
) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)
        set_organization_context(session, ORG_A_ID)
        alternate_provider = AiProviderProfile(
            slug="usage-trend-alternate-provider",
            display_name="Usage trend alternate provider",
            driver="openai_compatible",
            base_url="https://trend-alternate-provider.example.test/v1/chat/completions",
            credential_ref="usage-trend-alternate-provider-credential",
        )
        alternate_model = AiModelProfile(
            provider_profile=alternate_provider,
            slug="usage-trend-alternate-model",
            display_name="Usage trend alternate model",
            provider_model_id="trend-alternate-model",
        )
        session.add_all([alternate_provider, alternate_model])
        session.flush()
        score_run = session.scalar(
            select(AiRun).where(
                AiRun.organization_id == ORG_A_ID,
                AiRun.feature == "resume_score",
            )
        )
        assert score_run is not None
        score_invocation = session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == score_run.id)
        )
        assert score_invocation is not None

        # The enclosing business run remains on day 2.  Both calls happened
        # in the same hour on day 5, so the chart must use that actual
        # invocation hour and preserve their different Provider/model labels.
        score_invocation.started_at = _at(5, 9, 20)
        session.add(
            ApiInvocation(
                ai_run_id=score_run.id,
                attempt_no=2,
                provider_profile_id=alternate_provider.id,
                model_profile_id=alternate_model.id,
                provider_driver="openai_compatible",
                provider_model_id="trend-alternate-model",
                status="succeeded",
                started_at=_at(5, 9, 45),
                completed_at=_at(5, 9, 46),
                usage_source="provider",
                input_tokens=7,
                cached_read_input_tokens=2,
                cached_write_input_tokens=1,
                output_tokens=11,
                reasoning_tokens=4,
            )
        )
        session.commit()

        query = AiUsageTrendQuery(
            organization_id=ORG_A_ID,
            feature="resume_score",
            started_at_from=_at(5, 9, 0),
            started_at_to=_at(5, 9, 59),
            granularity="hour",
        )
        buckets = summarize_platform_ai_usage_trend(session, query=query)

        assert [
            (
                row.bucket_started_at,
                row.provider_slug,
                row.model_slug,
                row.invocation_count,
                row.token_usage_invocation_count,
                row.total_tokens,
            )
            for row in buckets
        ] == [
            (
                _at(5, 9, 0),
                "usage-reporting-provider",
                "usage-reporting-primary",
                1,
                1,
                138,
            ),
            (
                _at(5, 9, 0),
                "usage-trend-alternate-provider",
                "usage-trend-alternate-model",
                1,
                1,
                25,
            ),
        ]
        assert buckets[1].input_tokens == 7
        assert buckets[1].cached_read_input_tokens == 2
        assert buckets[1].cached_write_input_tokens == 1
        assert buckets[1].output_tokens == 11
        assert buckets[1].reasoning_tokens == 4

        # The date predicate follows ApiInvocation.started_at, rather than the
        # run's day-2 timestamp, and identity filters narrow only the selected
        # Provider/model series.
        assert summarize_platform_ai_usage_trend(
            session,
            query=AiUsageTrendQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
                started_at_from=_at(2, 0, 0),
                started_at_to=_at(2, 23, 59),
                granularity="hour",
            ),
        ) == []
        alternate_only = summarize_platform_ai_usage_trend(
            session,
            query=AiUsageTrendQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
                provider_slug="usage-trend-alternate-provider",
                model_slug="usage-trend-alternate-model",
                started_at_from=_at(5, 9, 0),
                started_at_to=_at(5, 9, 59),
                granularity="hour",
            ),
        )
        assert [
            (row.provider_slug, row.model_slug, row.total_tokens)
            for row in alternate_only
        ] == [
            ("usage-trend-alternate-provider", "usage-trend-alternate-model", 25)
        ]

        daily_buckets = summarize_platform_ai_usage_trend(
            session,
            query=AiUsageTrendQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
                started_at_from=_at(5, 0, 0),
                started_at_to=_at(5, 23, 59),
                granularity="day",
            ),
        )
        assert [
            (row.bucket_started_at, row.provider_slug, row.model_slug, row.total_tokens)
            for row in daily_buckets
        ] == [
            (
                _at(5, 0, 0),
                "usage-reporting-provider",
                "usage-reporting-primary",
                138,
            ),
            (
                _at(5, 0, 0),
                "usage-trend-alternate-provider",
                "usage-trend-alternate-model",
                25,
            ),
        ]


def test_platform_usage_trend_uses_the_requested_iana_civil_calendar(
    ai_client,
) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)
        set_organization_context(session, ORG_A_ID)
        score_run = session.scalar(
            select(AiRun).where(
                AiRun.organization_id == ORG_A_ID,
                AiRun.feature == "resume_score",
            )
        )
        assert score_run is not None
        score_invocation = session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == score_run.id)
        )
        assert score_invocation is not None

        # The invocation is still on July 1 in UTC but is July 2 in China.
        # A daily bucket must begin at China midnight (July 1 16:00 UTC), not
        # the UTC calendar midnight that the previous implementation used.
        score_invocation.started_at = datetime(2026, 7, 1, 16, 30, tzinfo=UTC)
        session.commit()
        shanghai_query = AiUsageTrendQuery(
            organization_id=ORG_A_ID,
            feature="resume_score",
            started_at_from=datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            started_at_to=datetime(2026, 7, 2, 15, 59, tzinfo=UTC),
            granularity="day",
            time_zone="Asia/Shanghai",
        )
        assert shanghai_query.bucket_time_zone == ZoneInfo("Asia/Shanghai")
        shanghai_buckets = summarize_platform_ai_usage_trend(
            session,
            query=shanghai_query,
        )
        assert [
            (row.bucket_started_at, row.time_zone, row.total_tokens)
            for row in shanghai_buckets
        ] == [
            (
                datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
                "Asia/Shanghai",
                138,
            )
        ]

        # Nepal is UTC+05:45.  This proves hourly grouping follows a local
        # clock hour, rather than a shifted UTC-hour bucket such as 00:45.
        score_invocation.started_at = datetime(2026, 7, 1, 18, 45, tzinfo=UTC)
        session.commit()
        kathmandu_buckets = summarize_platform_ai_usage_trend(
            session,
            query=AiUsageTrendQuery(
                organization_id=ORG_A_ID,
                feature="resume_score",
                started_at_from=datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
                started_at_to=datetime(2026, 7, 1, 19, 0, tzinfo=UTC),
                granularity="hour",
                time_zone="Asia/Kathmandu",
            ),
        )
        assert [
            (row.bucket_started_at, row.time_zone, row.total_tokens)
            for row in kathmandu_buckets
        ] == [
            (
                datetime(2026, 7, 1, 18, 15, tzinfo=UTC),
                "Asia/Kathmandu",
                138,
            )
        ]


def test_postgresql_trend_statement_uses_parameterized_iana_buckets() -> None:
    statement = _postgresql_trend_statement(
        AiUsageTrendQuery(
            started_at_from=_at(1),
            started_at_to=_at(2),
            time_zone="Asia/Shanghai",
        )
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()
    assert "date_trunc" in sql
    assert "timezone" in sql
    assert "asia/shanghai" not in sql
    assert "Asia/Shanghai" in compiled.params.values()


def test_platform_usage_api_returns_token_totals_without_business_content(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)

    runs_response = ai_client.get(
        "/v1/platform/ai/usage/runs",
        params={"organization_id": ORG_A_ID},
    )
    assert runs_response.status_code == 200, runs_response.text
    runs_by_feature = {item["feature"]: item for item in runs_response.json()}
    assert runs_by_feature["resume_score"]["token_usage_invocation_count"] == 1
    assert runs_by_feature["resume_score"]["total_tokens"] == 138
    assert "business_ref_id" not in runs_by_feature["resume_score"]
    assert "prompt" not in runs_by_feature["resume_score"]

    usage_response = ai_client.get(
        "/v1/platform/ai/usage/summary",
        params={
            "organization_id": ORG_A_ID,
            "started_at_from": _at(2).isoformat(),
            "started_at_to": _at(2).isoformat(),
        },
    )
    assert usage_response.status_code == 200, usage_response.text
    assert len(usage_response.json()) == 1
    usage_by_feature = {item["feature"]: item for item in usage_response.json()}
    score = usage_by_feature["resume_score"]
    assert score["provider_slug"] == "usage-reporting-provider"
    assert score["model_slug"] == "usage-reporting-primary"
    assert score["input_tokens"] == 100
    assert score["cached_read_input_tokens"] == 10
    assert score["cached_write_input_tokens"] == 5
    assert score["output_tokens"] == 20
    assert score["reasoning_tokens"] == 3
    assert score["total_tokens"] == 138

    trend_response = ai_client.get(
        "/v1/platform/ai/usage/trend",
        params={
            "organization_id": ORG_A_ID,
            "provider_slug": "usage-reporting-provider",
            "model_slug": "usage-reporting-primary",
            "started_at_from": _at(2, 0, 0).isoformat(),
            "started_at_to": _at(2, 23, 59).isoformat(),
            "granularity": "hour",
        },
    )
    assert trend_response.status_code == 200, trend_response.text
    trend_payload = trend_response.json()
    assert len(trend_payload) == 1
    trend = trend_payload[0]
    assert set(trend) == {
        "bucket_started_at",
        "time_zone",
        "provider_slug",
        "model_slug",
        "invocation_count",
        "token_usage_invocation_count",
        "input_tokens",
        "cached_read_input_tokens",
        "cached_write_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
    assert trend["bucket_started_at"].startswith("2026-07-02T09:00:00")
    assert trend["time_zone"] == "UTC"
    assert trend["provider_slug"] == "usage-reporting-provider"
    assert trend["model_slug"] == "usage-reporting-primary"
    assert trend["invocation_count"] == 1
    assert trend["token_usage_invocation_count"] == 1
    assert trend["input_tokens"] == 100
    assert trend["cached_read_input_tokens"] == 10
    assert trend["cached_write_input_tokens"] == 5
    assert trend["output_tokens"] == 20
    assert trend["reasoning_tokens"] == 3
    assert trend["total_tokens"] == 138


def test_platform_usage_trend_api_accepts_an_iana_time_zone(ai_client) -> None:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        _seed_usage_ledger(session)

    response = ai_client.get(
        "/v1/platform/ai/usage/trend",
        params={
            "organization_id": ORG_A_ID,
            "provider_slug": "usage-reporting-provider",
            "model_slug": "usage-reporting-primary",
            # July 2 in Asia/Shanghai, represented as absolute UTC bounds.
            "started_at_from": "2026-07-01T16:00:00+00:00",
            "started_at_to": "2026-07-02T15:59:59.999000+00:00",
            "granularity": "day",
            "time_zone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["time_zone"] == "Asia/Shanghai"
    bucket_started_at = datetime.fromisoformat(
        payload[0]["bucket_started_at"].replace("Z", "+00:00")
    )
    assert bucket_started_at == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)


def test_platform_reporting_rejects_invalid_filters() -> None:
    with pytest.raises(AiUsageReportingError, match="ai_usage_date_range_invalid"):
        AiUsageQuery(started_at_from=_at(4), started_at_to=_at(2))
    with pytest.raises(AiUsageReportingError, match="ai_usage_limit_invalid"):
        AiUsageQuery(limit=501)
    with pytest.raises(AiUsageReportingError, match="ai_usage_trend_granularity_invalid"):
        AiUsageTrendQuery(granularity="month")  # type: ignore[arg-type]
    with pytest.raises(AiUsageReportingError, match="ai_usage_trend_date_range_incomplete"):
        AiUsageTrendQuery(started_at_from=_at(2))
    with pytest.raises(AiUsageReportingError, match="ai_usage_trend_date_range_too_large"):
        AiUsageTrendQuery(
            started_at_from=_at(1),
            started_at_to=datetime(2026, 8, 2, 9, 30, tzinfo=UTC),
            granularity="hour",
        )
    with pytest.raises(AiUsageReportingError, match="ai_usage_trend_time_zone_invalid"):
        AiUsageTrendQuery(time_zone="../outside")
    default_trend = AiUsageTrendQuery()
    assert default_trend.started_at_from is not None
    assert default_trend.started_at_to is not None
    assert default_trend.started_at_to - default_trend.started_at_from == timedelta(days=30)
    assert default_trend.time_zone == "UTC"
