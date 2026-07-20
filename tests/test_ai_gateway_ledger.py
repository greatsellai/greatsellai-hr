from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import CompletionResult, NormalizedUsage, ToolCall
from app.ai.adapters import OpenAICompatibleAdapter
from app.ai.errors import ProviderError, ProviderErrorCategory
from app.models import AiModelPriceVersion, AiModelProfile, AiRun, ApiInvocation
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    ai_gateway_execution,
    resolve_active_route_policy_version_id,
)
from app.services.deepseek_provider import DeepSeekProviderError, call_strict_function


def _gateway_tool_result() -> CompletionResult:
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
        usage=NormalizedUsage(
            input_tokens=10,
            output_tokens=5,
            request_units=1,
            provider_reported_total_tokens=15,
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
