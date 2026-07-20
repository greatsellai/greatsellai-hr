from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import CompletionResult, NormalizedUsage, ToolCall
from app.ai.adapters import OpenAICompatibleAdapter
from app.ai.errors import ProviderError, ProviderErrorCategory
from app.config import AppSettings
from app.database import Database
from app.models import (
    AiModelPriceVersion,
    AiModelProfile,
    AiProviderProfile,
    AiRoutePolicy,
    AiRoutePolicyVersion,
    AiRun,
    ApiInvocation,
    utcnow,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    _bootstrap_legacy_route_if_available,
    ai_gateway_execution,
    resolve_active_route_policy_version_id,
)
from app.services.deepseek_provider import DeepSeekProviderError, call_strict_function


def _gateway_tool_result(*, include_usage: bool = True) -> CompletionResult:
    raw_response = {
        "id": "provider-response-1",
        "model": "actual-configured-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tool-call-1",
                            "type": "function",
                            "function": {
                                "name": "submit",
                                "arguments": '{"ok":true}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return CompletionResult(
        content=None,
        tool_calls=(ToolCall(id="tool-call-1", name="submit", arguments='{"ok":true}'),),
        finish_reason="tool_calls",
        provider_request_id="request-1",
        provider_response_id="provider-response-1",
        usage=(
            NormalizedUsage(
                input_tokens=10,
                output_tokens=5,
                request_units=1,
                provider_reported_total_tokens=15,
            )
            if include_usage
            else None
        ),
        raw_status_code=200,
        model_id="actual-configured-model",
        raw_response=raw_response,
    )


def _call_strict_tool(settings: object) -> dict[str, object]:
    return call_strict_function(
        api_key=getattr(settings, "deepseek_api_key") or "",
        model=getattr(settings, "deepseek_model"),
        timeout_seconds=getattr(settings, "deepseek_timeout_seconds"),
        function_name="submit",
        function_description="Submit a small test payload.",
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
        system_prompt="Return the requested function call.",
        user_prompt="Return ok true.",
        max_tokens=64,
    )


def _seed_legacy_route_price(session: Session, settings: object) -> None:
    resolve_active_route_policy_version_id(
        session,
        settings=settings,
        feature="resume_extract_rich",
    )
    model = session.scalar(
        select(AiModelProfile).where(AiModelProfile.slug == "legacy-runtime-default")
    )
    assert model is not None
    if not session.scalar(
        select(AiModelPriceVersion.id).where(AiModelPriceVersion.model_profile_id == model.id)
    ):
        session.add(
            AiModelPriceVersion(
                model_profile_id=model.id,
                version=1,
                currency="CNY",
                effective_from=model.created_at,
                input_price_per_million=Decimal("1"),
                output_price_per_million=Decimal("2"),
                source="test-price",
                is_active=True,
            )
        )
    session.commit()


def _seed_two_target_route(
    session: Session,
    *,
    feature: str,
    allow_fallback_on: list[str] | None,
) -> None:
    provider = AiProviderProfile(
        slug="fallback-test-provider",
        display_name="Fallback test provider",
        driver="openai_compatible",
        base_url="https://provider.invalid/v1/chat/completions",
        credential_ref="legacy-runtime-credential",
        request_defaults_json={},
        enabled=True,
    )
    session.add(provider)
    session.flush()
    models = [
        AiModelProfile(
            provider_profile_id=provider.id,
            slug=f"fallback-test-model-{index}",
            display_name=f"Fallback test model {index}",
            provider_model_id=f"provider-model-{index}",
            capabilities_json={"chat": True, "tools": True, "json_schema": True},
            data_classification_json={"candidate_data_allowed": True},
            enabled=True,
        )
        for index in (1, 2)
    ]
    session.add_all(models)
    session.flush()
    policy = AiRoutePolicy(
        feature=feature,
        display_name="Fallback test route",
        enabled=True,
    )
    session.add(policy)
    session.flush()
    first_target: dict[str, object] = {
        "model_profile_id": models[0].id,
        "max_attempts": 1,
    }
    if allow_fallback_on is not None:
        first_target["allow_fallback_on"] = allow_fallback_on
    version = AiRoutePolicyVersion(
        policy_id=policy.id,
        version=1,
        status="published",
        targets_json=[
            first_target,
            {
                "model_profile_id": models[1].id,
                "max_attempts": 1,
                "allow_fallback_on": [],
            },
        ],
        retry_policy_json={},
        max_cost_guard_json={},
        published_at=utcnow(),
    )
    session.add(version)
    session.flush()
    policy.active_version_id = version.id
    session.commit()


def test_gateway_writes_cost_ledger_without_persisting_prompt_or_output(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "complete",
        lambda self, request, route: _gateway_tool_result(),
    )

    with database.session_factory() as session:
        _seed_legacy_route_price(session, settings)
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_extract_rich",
                business_ref_type="test_resume",
                business_ref_id="resume-gateway-success",
                prompt_revision="test.prompt.v1",
                contract_version="test.contract.v1",
            ),
        ):
            assert _call_strict_tool(settings) == {"ok": True}

        session.expire_all()
        run = session.scalar(
            select(AiRun).where(AiRun.business_ref_id == "resume-gateway-success")
        )
        assert run is not None
        assert run.status == "succeeded"
        assert run.total_cost_reporting_micros == 20
        assert run.cost_status == "known"
        invocation = session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == run.id)
        )
        assert invocation is not None
        assert invocation.status == "succeeded"
        assert invocation.provider_model_id == "actual-configured-model"
        assert invocation.input_tokens == 10
        assert invocation.output_tokens == 5
        assert invocation.reporting_cost_micros == 20
        assert invocation.price_snapshot_json["input_price_per_million"] == "1.00000000"
        assert not hasattr(invocation, "prompt")
        assert not hasattr(invocation, "response")
        assert not hasattr(invocation, "api_key")


def test_gateway_keeps_successful_attempt_when_local_validation_fails(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "complete",
        lambda self, request, route: _gateway_tool_result(),
    )

    with database.session_factory() as session:
        _seed_legacy_route_price(session, settings)
        with pytest.raises(ValueError, match="local_schema_rejected"):
            with ai_gateway_execution(
                session,
                settings=settings,
                spec=AiExecutionSpec(
                    feature="resume_extract_rich",
                    business_ref_type="test_resume",
                    business_ref_id="resume-gateway-validation-failure",
                ),
            ):
                assert _call_strict_tool(settings) == {"ok": True}
                raise ValueError("local_schema_rejected")

        session.expire_all()
        run = session.scalar(
            select(AiRun).where(AiRun.business_ref_id == "resume-gateway-validation-failure")
        )
        assert run is not None
        assert run.status == "failed"
        assert run.failure_code == "local_schema_rejected"
        invocation = session.scalar(select(ApiInvocation).where(ApiInvocation.ai_run_id == run.id))
        assert invocation is not None
        assert invocation.status == "succeeded"
        assert invocation.reporting_cost_micros == 20


def test_gateway_records_timeout_as_potentially_billable_attempt(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings

    def raise_timeout(self: OpenAICompatibleAdapter, request: object, route: object) -> CompletionResult:
        raise ProviderError(ProviderErrorCategory.TIMEOUT, may_have_billed=True)

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", raise_timeout)

    with database.session_factory() as session:
        _seed_legacy_route_price(session, settings)
        with pytest.raises(DeepSeekProviderError, match="ai_provider_timeout"):
            with ai_gateway_execution(
                session,
                settings=settings,
                spec=AiExecutionSpec(
                    feature="resume_extract_rich",
                    business_ref_type="test_resume",
                    business_ref_id="resume-gateway-timeout",
                ),
            ):
                _call_strict_tool(settings)

        session.expire_all()
        run = session.scalar(
            select(AiRun).where(AiRun.business_ref_id == "resume-gateway-timeout")
        )
        assert run is not None
        assert run.status == "failed"
        assert run.cost_status == "partial"
        invocation = session.scalar(select(ApiInvocation).where(ApiInvocation.ai_run_id == run.id))
        assert invocation is not None
        assert invocation.status == "failed"
        assert invocation.may_have_billed is True
        assert invocation.error_category == "timeout"


def test_gateway_does_not_cross_targets_without_explicit_fallback_allowlist(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    calls: list[str] = []

    def timeout_primary(
        self: OpenAICompatibleAdapter,
        request: object,
        route: object,
    ) -> CompletionResult:
        model_id = str(getattr(route, "provider_model_id"))
        calls.append(model_id)
        if model_id == "provider-model-1":
            raise ProviderError(ProviderErrorCategory.TIMEOUT)
        return _gateway_tool_result()

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", timeout_primary)
    with database.session_factory() as session:
        _seed_two_target_route(
            session,
            feature="resume_summary",
            allow_fallback_on=None,
        )
        with pytest.raises(DeepSeekProviderError, match="ai_provider_timeout"):
            with ai_gateway_execution(
                session,
                settings=settings,
                spec=AiExecutionSpec(
                    feature="resume_summary",
                    business_ref_type="test_resume",
                    business_ref_id="fallback-default-deny",
                ),
            ):
                _call_strict_tool(settings)

    assert calls == ["provider-model-1"]


def test_gateway_crosses_targets_only_for_an_allowlisted_failure(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    calls: list[str] = []

    def timeout_then_succeed(
        self: OpenAICompatibleAdapter,
        request: object,
        route: object,
    ) -> CompletionResult:
        model_id = str(getattr(route, "provider_model_id"))
        calls.append(model_id)
        if model_id == "provider-model-1":
            raise ProviderError(ProviderErrorCategory.TIMEOUT)
        return _gateway_tool_result()

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", timeout_then_succeed)
    with database.session_factory() as session:
        _seed_two_target_route(
            session,
            feature="resume_summary",
            allow_fallback_on=["timeout"],
        )
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_summary",
                business_ref_type="test_resume",
                business_ref_id="fallback-explicit-allow",
            ),
        ):
            assert _call_strict_tool(settings) == {"ok": True}

    assert calls == ["provider-model-1", "provider-model-2"]


def test_gateway_marks_known_plus_successful_unknown_cost_as_partial(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    results = [_gateway_tool_result(), _gateway_tool_result(include_usage=False)]
    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "complete",
        lambda self, request, route: results.pop(0),
    )

    with database.session_factory() as session:
        _seed_legacy_route_price(session, settings)
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_extract_rich",
                business_ref_type="test_resume",
                business_ref_id="resume-gateway-mixed-cost",
            ),
        ):
            assert _call_strict_tool(settings) == {"ok": True}
            assert _call_strict_tool(settings) == {"ok": True}

        run = session.scalar(
            select(AiRun).where(AiRun.business_ref_id == "resume-gateway-mixed-cost")
        )
        assert run is not None
        assert run.total_cost_reporting_micros == 20
        assert run.cost_status == "partial"


def test_legacy_bootstrap_handles_a_duplicate_race_without_committing_outer_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'bootstrap-race.db').as_posix()}")
    database.create_all()
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url=f"sqlite:///{(tmp_path / 'bootstrap-race.db').as_posix()}",
        deepseek_api_key="unit-test-key",
        deepseek_model="unit-test-model",
    )
    with database.session_factory() as seed_session:
        seed_session.add(
            AiProviderProfile(
                slug="legacy-runtime-openai-compatible",
                display_name="Concurrent winner",
                driver="openai_compatible",
                base_url=settings.legacy_openai_compatible_endpoint,
                credential_ref="legacy-runtime-credential",
                request_defaults_json={},
                enabled=True,
            )
        )
        seed_session.commit()

    with database.session_factory() as session:
        outer_pending = AiProviderProfile(
            slug="outer-transaction-sentinel",
            display_name="Outer transaction sentinel",
            driver="openai_compatible",
            base_url="https://sentinel.invalid/v1/chat/completions",
            credential_ref="sentinel-credential",
            request_defaults_json={},
            enabled=True,
        )
        session.add(outer_pending)
        original_scalar = session.scalar
        lookup_count = 0

        def stale_provider_lookup_once(statement: object, *args: object, **kwargs: object):
            nonlocal lookup_count
            lookup_count += 1
            if lookup_count == 1:
                return None
            return original_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalar", stale_provider_lookup_once)
        _bootstrap_legacy_route_if_available(
            session,
            settings=settings,
            feature="resume_summary",
        )

        assert session.in_transaction()
        assert original_scalar(
            select(AiProviderProfile).where(
                AiProviderProfile.slug == "outer-transaction-sentinel"
            )
        ) is outer_pending
        with database.session_factory() as observer:
            assert observer.scalar(
                select(AiProviderProfile).where(
                    AiProviderProfile.slug == "outer-transaction-sentinel"
                )
            ) is None
            assert observer.scalar(
                select(AiRoutePolicy).where(AiRoutePolicy.feature == "resume_summary")
            ) is None
        session.commit()

    with database.session_factory() as observer:
        assert observer.scalar(
            select(AiProviderProfile).where(
                AiProviderProfile.slug == "outer-transaction-sentinel"
            )
        ) is not None
        policy = observer.scalar(
            select(AiRoutePolicy).where(AiRoutePolicy.feature == "resume_summary")
        )
        assert policy is not None
        assert policy.active_version_id is not None
    database.dispose()
