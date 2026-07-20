from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import AiModelProfile, AiProviderProfile, AiRun, ApiInvocation, Organization
from app.services.ai_usage_reporting_service import (
    AiUsageQuery,
    AiUsageReportingError,
    list_platform_ai_run_summaries,
    summarize_platform_ai_usage,
)
from app.tenant_scope import clear_organization_context, set_organization_context


UTC = timezone.utc
ORG_A_ID = "00000000-0000-4000-8000-0000000000a1"
ORG_B_ID = "00000000-0000-4000-8000-0000000000b2"


def _at(day: int) -> datetime:
    return datetime(2026, 7, day, 9, 30, tzinfo=UTC)


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
        assert score.model_slug == "usage-reporting-primary"
        assert score.invocation_count == 1
        assert score.costed_invocation_count == 1
        assert score.unavailable_cost_invocation_count == 0
        assert score.reported_cost_cny_micros == 120
        assert score.known_run_count == 1
        assert score.partial_run_count == 0
        assert score.unavailable_run_count == 0
        assert score.potentially_billed_invocation_count == 0

        match = by_feature["jd_match"]
        assert match.organization_id == ORG_A_ID
        assert match.model_slug == "usage-reporting-secondary"
        assert match.invocation_count == 2
        assert match.costed_invocation_count == 1
        assert match.unavailable_cost_invocation_count == 1
        assert match.reported_cost_cny_micros == 30
        assert match.known_run_count == 0
        assert match.partial_run_count == 1
        assert match.unavailable_run_count == 0
        assert match.potentially_billed_invocation_count == 1

        all_aggregates = summarize_platform_ai_usage(session, query=AiUsageQuery())
        assert {(row.organization_id, row.reported_cost_cny_micros) for row in all_aggregates} == {
            (ORG_A_ID, 120),
            (ORG_A_ID, 30),
            (ORG_B_ID, 700),
        }


def test_platform_reporting_rejects_invalid_filters() -> None:
    with pytest.raises(AiUsageReportingError, match="ai_usage_date_range_invalid"):
        AiUsageQuery(started_at_from=_at(4), started_at_to=_at(2))
    with pytest.raises(AiUsageReportingError, match="ai_usage_limit_invalid"):
        AiUsageQuery(limit=501)
