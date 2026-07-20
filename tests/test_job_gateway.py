from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from app.ai import CompletionResult
from app.ai.adapters import OpenAICompatibleAdapter
from app.models import (
    AiModelProfile,
    AiProviderProfile,
    AiRoutePolicy,
    AiRoutePolicyVersion,
    AiRun,
    JobMatchBatch,
    JobMatchBatchItem,
    utcnow,
)
from app.schemas import JobCreate, JobGenerationRequest, JobMatchCreate, JobRequirements
from app.services import job_match_batch_service, job_service
from app.services.ai_gateway_service import active_legacy_payload_executor
from app.services.deepseek_provider import DeepSeekProviderError
from test_filter_mvp_contract import _save_ready_resume


def _draft_job_payload() -> JobCreate:
    return JobCreate(
        title="Backend Engineer",
        jd_text="Python experience is required.\nKubernetes experience is preferred.",
        requirements=JobRequirements(),
    )


def _generated_jd_inside_gateway(**kwargs: object) -> dict[str, object]:
    assert active_legacy_payload_executor() is not None
    assert kwargs["title"] == "Backend Engineer"
    return {
        "title": "Backend Engineer",
        "jd_text": "Python experience is required.",
        "requirements": {
            "must_have": ["Python experience is required."],
            "preferred": [],
        },
    }


def _requirements_inside_gateway(**kwargs: object) -> dict[str, object]:
    assert active_legacy_payload_executor() is not None
    clauses = kwargs["clauses"]
    assert clauses == [
        {"clause_id": "clause-001", "text": "Python experience is required."},
        {
            "clause_id": "clause-002",
            "text": "Kubernetes experience is preferred.",
        },
    ]
    return {
        "schema_version": "jd_requirements.v1",
        "clause_coverage": [
            {"clause_id": "clause-001", "requirement_ids": ["req-001"]},
            {"clause_id": "clause-002", "requirement_ids": ["req-002"]},
        ],
        "requirements": [
            {
                "requirement_id": "req-001",
                "requirement_text": "Python experience is required.",
                "priority": "must_have",
                "clause_ids": ["clause-001"],
            },
            {
                "requirement_id": "req-002",
                "requirement_text": "Kubernetes experience is preferred.",
                "priority": "preferred",
                "clause_ids": ["clause-002"],
            },
        ],
    }


def _match_inside_gateway(**kwargs: object) -> dict[str, object]:
    assert active_legacy_payload_executor() is not None
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    skill_fact_id = snapshot["skills"][0]["fact_id"]
    requirements = kwargs["confirmed_requirements"]
    assert isinstance(requirements, list)
    return {
        "schema_version": "jd_match.v1",
        "requirement_matches": [
            {
                "requirement_id": requirement["requirement_id"],
                "status": "met",
                "rationale": "An explicit skill fact supports this requirement.",
                "fact_ids": [skill_fact_id],
                "uncertainties": [],
            }
            for requirement in requirements
        ],
        "needs_human_review": False,
    }


def test_jd_generate_extract_and_match_enter_gateway_context(
    ai_client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(job_service, "generate_jd_from_brief", _generated_jd_inside_gateway)
    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        _requirements_inside_gateway,
    )
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _match_inside_gateway,
    )

    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    with database.session_factory() as session:
        generated = job_service.generate_job_description(
            session=session,
            payload=JobGenerationRequest(
                title="Backend Engineer",
                brief="Build a reliable recruiting platform.",
            ),
            settings=settings,
        )
        assert generated.title == "Backend Engineer"

        created = job_service.create_job(session, payload=_draft_job_payload())
        session.commit()

    with database.session_factory() as session:
        extracted = job_service.extract_job_version_requirements(
            session,
            job_version_id=created.job_version_id,
            settings=settings,
        )
        assert extracted.status == "draft"
        job_service.confirm_job_version(session, job_version_id=created.job_version_id)
        session.commit()

    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL Kubernetes"
        ),
    )
    with database.session_factory() as session:
        match = job_service.run_job_match(
            session,
            resume_id=resume_id,
            payload=JobMatchCreate(job_version_id=created.job_version_id),
            settings=settings,
        )
        session.commit()
        assert match.status == "succeeded"

    with database.session_factory() as session:
        runs = session.scalars(
            select(AiRun)
            .where(
                AiRun.feature.in_(
                    ("jd_generate", "jd_requirements_extract", "jd_match")
                )
            )
            .order_by(AiRun.feature)
        ).all()
    assert [run.feature for run in runs] == [
        "jd_generate",
        "jd_match",
        "jd_requirements_extract",
    ]
    assert all(run.status == "succeeded" for run in runs)
    assert all(run.route_policy_version_id for run in runs)


def test_jd_generation_accepts_generic_gateway_credential_map(
    ai_client,
    monkeypatch,
) -> None:
    """A route-owned credential works even with no legacy provider key."""

    database = ai_client.app.state.database
    settings = replace(
        ai_client.app.state.settings,
        deepseek_api_key=None,
        ai_provider_credentials={"platform-jd-credential": "test-map-secret"},
    )
    with database.session_factory() as session:
        provider = AiProviderProfile(
            slug="test-jd-gateway-provider",
            display_name="Test JD gateway provider",
            driver="openai_compatible",
            base_url="https://provider.invalid/v1/chat/completions",
            credential_ref="platform-jd-credential",
            request_defaults_json={},
            enabled=True,
        )
        session.add(provider)
        session.flush()
        model = AiModelProfile(
            provider_profile_id=provider.id,
            slug="test-jd-gateway-model",
            display_name="Test JD gateway model",
            provider_model_id="owner-selected-jd-model",
            capabilities_json={"chat": True},
            data_classification_json={"candidate_data_allowed": True},
            enabled=True,
        )
        session.add(model)
        session.flush()
        policy = AiRoutePolicy(
            feature="jd_generate",
            display_name="JD generation",
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

    def fake_complete(
        self: OpenAICompatibleAdapter,
        request: object,
        route: object,
    ) -> CompletionResult:
        captured["route"] = route
        return CompletionResult(
            content="ok",
            tool_calls=(),
            finish_reason="stop",
            provider_request_id="provider-request",
            usage=None,
            raw_status_code=200,
            model_id="owner-selected-jd-model",
            raw_response={
                "id": "provider-response",
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    def fake_generation(**kwargs: object) -> dict[str, object]:
        # The old helper receives an empty compatibility argument, proving it
        # did not source a provider key itself. The gateway owns the selected
        # route and resolves its credential reference instead.
        assert kwargs["api_key"] == ""
        executor = active_legacy_payload_executor()
        assert executor is not None
        executor(
            {
                "model": "legacy-model-must-be-ignored",
                "messages": [{"role": "user", "content": "Generate a JD."}],
            }
        )
        return _generated_jd_inside_gateway(**kwargs)

    monkeypatch.setattr(OpenAICompatibleAdapter, "complete", fake_complete)
    monkeypatch.setattr(job_service, "generate_jd_from_brief", fake_generation)
    with database.session_factory() as session:
        generated = job_service.generate_job_description(
            session=session,
            payload=JobGenerationRequest(
                title="Backend Engineer",
                brief="Build a reliable recruiting platform.",
            ),
            settings=settings,
        )
    assert generated.title == "Backend Engineer"
    route = captured["route"]
    assert getattr(route, "credential") == "test-map-secret"
    assert getattr(route, "provider_model_id") == "owner-selected-jd-model"


def test_job_match_batch_retry_keeps_enqueue_route_pin(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL Kubernetes"
        ),
    )
    created = ai_client.post(
        "/v1/jobs",
        json=JobCreate(
            title="Backend Engineer",
            jd_text="Python experience is required.",
            requirements=JobRequirements(must_have=["Python experience"]),
        ).model_dump(),
    )
    assert created.status_code == 200, created.text
    job_version_id = created.json()["job_version_id"]

    calls = 0

    def retry_then_match(**kwargs: object) -> dict[str, object]:
        nonlocal calls
        assert active_legacy_payload_executor() is not None
        calls += 1
        if calls == 1:
            raise DeepSeekProviderError("deepseek_network_error")
        return _match_inside_gateway(**kwargs)

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        retry_then_match,
    )
    queued = ai_client.post(f"/v1/job-versions/{job_version_id}/match-all")
    assert queued.status_code == 200, queued.text

    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    with database.session_factory() as session:
        batch = session.get(JobMatchBatch, queued.json()["batch_id"])
        assert batch is not None
        route_policy_version_id = batch.ai_route_policy_version_id
        assert route_policy_version_id
        batch.ai_route_policy_version_id = None
        session.commit()

    # The first retryable failure releases the item back to the queue.  The
    # next attempt must use the pin, even after the currently active policy is
    # disabled.
    assert job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=settings,
        worker_id="jd-route-pin-test-worker",
    )
    with database.session_factory() as session:
        batch = session.get(JobMatchBatch, queued.json()["batch_id"])
        assert batch is not None
        assert batch.ai_route_policy_version_id == route_policy_version_id
        item = session.scalar(
            select(JobMatchBatchItem).where(
                JobMatchBatchItem.batch_id == queued.json()["batch_id"]
            )
        )
        assert item is not None
        assert item.status == "queued"
        item.next_attempt_at = job_match_batch_service._utcnow()
        policy = session.scalar(
            select(AiRoutePolicy).where(AiRoutePolicy.feature == "jd_match")
        )
        assert policy is not None
        policy.enabled = False
        session.commit()

    assert job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=settings,
        worker_id="jd-route-pin-test-worker",
    )

    with database.session_factory() as session:
        batch = session.get(JobMatchBatch, queued.json()["batch_id"])
        assert batch is not None
        assert batch.status == "completed"
        runs = session.scalars(
            select(AiRun)
            .where(
                AiRun.feature == "jd_match",
                AiRun.business_ref_id == (
                    session.scalar(
                        select(JobMatchBatchItem.id).where(
                            JobMatchBatchItem.batch_id == batch.id,
                            JobMatchBatchItem.resume_id == resume_id,
                        )
                    )
                ),
            )
            .order_by(AiRun.started_at)
        ).all()
    assert len(runs) == 2
    assert {run.route_policy_version_id for run in runs} == {route_policy_version_id}


def test_job_match_batch_does_not_retry_non_transport_provider_failure(
    ai_client,
    monkeypatch,
) -> None:
    _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL"
        ),
    )
    created = ai_client.post(
        "/v1/jobs",
        json=JobCreate(
            title="Backend Engineer",
            jd_text="Python experience is required.",
            requirements=JobRequirements(must_have=["Python experience"]),
        ).model_dump(),
    )
    assert created.status_code == 200, created.text

    provider_calls = 0

    def reject_auth(**kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise DeepSeekProviderError("ai_provider_auth")

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        reject_auth,
    )
    queued = ai_client.post(
        f"/v1/job-versions/{created.json()['job_version_id']}/match-all"
    )
    assert queued.status_code == 200, queued.text
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings

    assert job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=settings,
        worker_id="jd-terminal-error-test-worker",
    )
    with database.session_factory() as session:
        item = session.scalar(
            select(JobMatchBatchItem).where(
                JobMatchBatchItem.batch_id == queued.json()["batch_id"]
            )
        )
        assert item is not None
        assert item.status == "failed"
        assert item.attempt_count == 1
        assert item.next_attempt_at is None
        assert item.last_error == "ai_provider_auth"
    assert not job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=settings,
        worker_id="jd-terminal-error-test-worker",
    )
    assert provider_calls == 1
