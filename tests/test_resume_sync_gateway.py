from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.ai import CompletionResult, NormalizedUsage, ToolCall
from app.ai.adapters import OpenAICompatibleAdapter
from app.models import (
    AiModelProfile,
    AiProviderProfile,
    AiRoutePolicy,
    AiRoutePolicyVersion,
    AiRun,
    ApiInvocation,
    ResumeFactSnapshot,
    utcnow,
)
from app.services.resume_service import ResumeServiceError, auto_extract_and_save_facts
from test_resume_flow import make_pdf_with_text


def _upload_text_ready_resume(client) -> str:
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "sync-gateway-resume.pdf",
                make_pdf_with_text("Candidate Example Python FastAPI SQL " * 12),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["extraction_status"] == "text_ready"
    return str(response.json()["resume_id"])


def _source_grounded_resume_completion() -> CompletionResult:
    arguments = {
        "schema_version": "resume_facts.v2",
        "candidate_name_raw": None,
        "candidate_name_evidence_block_ids": [],
        "education": [],
        "experiences": [],
        "skills": [
            {
                "skill_display": "Python",
                "skill_category": "software",
                "evidence_block_ids": ["page-001"],
            }
        ],
        "language_credentials": [],
        "scholarships": [],
    }
    arguments_json = json.dumps(arguments, ensure_ascii=False)
    raw_response = {
        "id": "sync-gateway-provider-response",
        "model": "configured-model-on-the-route",
        "usage": {"prompt_tokens": 37, "completion_tokens": 11, "total_tokens": 48},
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "sync-gateway-tool-call",
                            "type": "function",
                            "function": {
                                "name": "submit_resume_facts",
                                "arguments": arguments_json,
                            },
                        }
                    ],
                },
            }
        ],
    }
    return CompletionResult(
        content=None,
        tool_calls=(
            ToolCall(
                id="sync-gateway-tool-call",
                name="submit_resume_facts",
                arguments=arguments_json,
            ),
        ),
        finish_reason="tool_calls",
        provider_request_id="sync-gateway-request",
        provider_response_id="sync-gateway-provider-response",
        usage=NormalizedUsage(
            input_tokens=37,
            output_tokens=11,
            request_units=1,
            provider_reported_total_tokens=48,
        ),
        raw_status_code=200,
        model_id="configured-model-on-the-route",
        raw_response=raw_response,
    )


def test_sync_resume_extraction_uses_gateway_route_and_records_ledger(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = _upload_text_ready_resume(ai_client)
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    monkeypatch.setattr(
        OpenAICompatibleAdapter,
        "complete",
        lambda _self, _request, _route: _source_grounded_resume_completion(),
    )

    with database.session_factory() as session:
        resume = auto_extract_and_save_facts(
            session,
            resume_id=resume_id,
            settings=settings,
        )
        assert resume.extraction_status == "ready"
        session.commit()

    with database.session_factory() as session:
        run = session.scalar(
            select(AiRun).where(
                AiRun.feature == "resume_extract_rich",
                AiRun.business_ref_type == "resume",
                AiRun.business_ref_id == resume_id,
            )
        )
        assert run is not None
        assert run.status == "succeeded"
        # The route snapshot, not the legacy settings model, is the durable
        # model-selection record for this synchronous action.
        assert run.route_policy_version_id is not None
        assert run.prompt_revision == "resume_facts.rich.v2"
        invocation = session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == run.id)
        )
        assert invocation is not None
        assert invocation.status == "succeeded"
        assert invocation.provider_model_id == "configured-model-on-the-route"
        assert invocation.input_tokens == 37
        assert invocation.output_tokens == 11
        snapshot = session.scalar(
            select(ResumeFactSnapshot).where(ResumeFactSnapshot.resume_id == resume_id)
        )
        assert snapshot is not None
        assert snapshot.created_by == "ai:gateway"


def test_sync_resume_extraction_records_platform_route_error_without_calling_provider(
    ai_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = _upload_text_ready_resume(ai_client)
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    provider_called = False

    def must_not_call_provider(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("a disabled route must fail before provider execution")

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", must_not_call_provider)
    with database.session_factory() as session:
        policy = session.scalar(
            select(AiRoutePolicy).where(AiRoutePolicy.feature == "resume_extract_rich")
        )
        assert policy is not None
        policy.enabled = False
        session.commit()

    with database.session_factory() as session:
        resume = auto_extract_and_save_facts(
            session,
            resume_id=resume_id,
            settings=settings,
        )
        assert resume.extraction_status == "needs_review"
        assert "ai_extraction_ai_route_disabled" in (resume.quality_flags or [])
        session.commit()

    assert provider_called is False
    with database.session_factory() as session:
        run = session.scalar(
            select(AiRun).where(
                AiRun.feature == "resume_extract_rich",
                AiRun.business_ref_type == "resume",
                AiRun.business_ref_id == resume_id,
            )
        )
        assert run is not None
        assert run.status == "failed"
        assert run.failure_code == "ai_route_disabled"
        assert session.scalar(
            select(ApiInvocation).where(ApiInvocation.ai_run_id == run.id)
        ) is None


def test_sync_resume_extraction_accepts_platform_owned_generic_credential(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility helper receives no direct model/key selection."""

    resume_id = _upload_text_ready_resume(client)
    database = client.app.state.database
    settings = replace(
        client.app.state.settings,
        ai_provider_credentials={"sync-gateway-credential": "test-map-secret"},
    )
    with database.session_factory() as session:
        provider = AiProviderProfile(
            slug="sync-gateway-provider",
            display_name="Sync gateway provider",
            driver="openai_compatible",
            base_url="https://provider.invalid/v1/chat/completions",
            credential_ref="sync-gateway-credential",
            request_defaults_json={},
            enabled=True,
        )
        session.add(provider)
        session.flush()
        model = AiModelProfile(
            provider_profile_id=provider.id,
            slug="sync-gateway-model",
            display_name="Sync gateway model",
            provider_model_id="platform-chosen-sync-model",
            capabilities_json={"chat": True, "tools": True, "json_schema": True},
            data_classification_json={"candidate_data_allowed": True},
            enabled=True,
        )
        session.add(model)
        session.flush()
        policy = AiRoutePolicy(
            feature="resume_extract_rich",
            display_name="Rich resume extraction",
            enabled=True,
        )
        session.add(policy)
        session.flush()
        version = AiRoutePolicyVersion(
            policy_id=policy.id,
            version=1,
            status="published",
            targets_json=[{"model_profile_id": model.id, "max_attempts": 1}],
            retry_policy_json={},
            max_cost_guard_json={},
            published_at=utcnow(),
        )
        session.add(version)
        session.flush()
        policy.active_version_id = version.id
        session.commit()

    captured: dict[str, object] = {}

    def fake_complete(_self, _request, route):
        captured["route"] = route
        return _source_grounded_resume_completion()

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", fake_complete)
    with database.session_factory() as session:
        resume = auto_extract_and_save_facts(
            session,
            resume_id=resume_id,
            settings=settings,
        )
        assert resume.extraction_status == "ready"
        session.commit()

    route = captured["route"]
    assert getattr(route, "credential") == "test-map-secret"
    assert getattr(route, "provider_model_id") == "platform-chosen-sync-model"


def test_sync_resume_extraction_keeps_legacy_no_credential_error(client) -> None:
    resume_id = _upload_text_ready_resume(client)
    database = client.app.state.database

    with database.session_factory() as session:
        with pytest.raises(ResumeServiceError, match="deepseek_api_key_not_configured"):
            auto_extract_and_save_facts(
                session,
                resume_id=resume_id,
                settings=client.app.state.settings,
            )
