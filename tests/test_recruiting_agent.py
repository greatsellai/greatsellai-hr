from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai import CompletionResult, NormalizedUsage, ToolCall
from app.models import AiRun, ApiInvocation
from app.services.ai_gateway_service import AiGatewayError
from app.services.recruiting_agent_service import ResolvedJob, _TOOLS, _resolve_job
from test_filter_mvp_contract import _save_ready_resume
from test_score_service import _fake_score_provider, _template_payload


def test_agent_fails_visibly_when_model_is_not_configured(client: TestClient) -> None:
    response = client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 985/211、3 年以上的候选人"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "agent_model_not_configured"


def test_agent_executes_model_selected_search_tool(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-search",
                        "type": "function",
                        "function": {
                            "name": "search_candidates",
                            "arguments": json.dumps(
                                {
                                    "is_985_211": True,
                                    "min_employment_months": 36,
                                    "skills_all_of": ["Python"],
                                    "limit": None,
                                }
                            ),
                        },
                    }
                ],
            }
        return {
            "content": "## 筛选结果\n\n已按 **985/211、3 年以上和 Python** 条件完成筛选。"
        }

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 985/211、3 年以上 Python 的候选人"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "## 筛选结果\n\n已按 **985/211、3 年以上和 Python** 条件完成筛选。"
    assert payload["tool_trace"][0]["tool"] == "简历筛选"
    assert "Python" in payload["tool_trace"][0]["summary"]
    assert calls == 2


def test_agent_timeout_is_returned_as_a_retryable_service_error(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    def timeout_executor(_payload):
        raise AiGatewayError("ai_provider_timeout")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service.active_legacy_payload_executor",
        lambda: timeout_executor,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 985/211、3 年以上的候选人"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "agent_model_timeout"


def test_agent_tool_loop_records_one_gateway_run_with_one_invocation_per_model_step(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    completion_calls = 0

    def fake_complete(_self, request, route):
        nonlocal completion_calls
        completion_calls += 1
        assert request.feature == "recruiting_agent_turn"
        assert route.provider_model_id == "unit-test-model"
        if completion_calls == 1:
            tool_call = ToolCall(
                id="call-search",
                name="search_candidates",
                arguments=json.dumps({"is_985_211": True}),
            )
            return CompletionResult(
                content=None,
                tool_calls=(tool_call,),
                finish_reason="tool_calls",
                provider_request_id="provider-request-1",
                usage=NormalizedUsage(input_tokens=20, output_tokens=5, request_units=1),
                raw_status_code=200,
                model_id="unit-test-model",
                raw_response={
                    "id": "response-1",
                    "model": "unit-test-model",
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-search",
                                        "type": "function",
                                        "function": {
                                            "name": "search_candidates",
                                            "arguments": json.dumps({"is_985_211": True}),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                },
            )

        assert [message.role for message in request.messages][-2:] == ["assistant", "tool"]
        assert request.messages[-2].tool_calls[0].id == "call-search"
        assert request.messages[-1].tool_call_id == "call-search"
        return CompletionResult(
            content="## 筛选结果\n\n已按 985/211 条件检索。",
            tool_calls=(),
            finish_reason="stop",
            provider_request_id="provider-request-2",
            usage=NormalizedUsage(input_tokens=30, output_tokens=8, request_units=1),
            raw_status_code=200,
            model_id="unit-test-model",
            raw_response={
                "id": "response-2",
                "model": "unit-test-model",
                "usage": {"prompt_tokens": 30, "completion_tokens": 8},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "## 筛选结果\n\n已按 985/211 条件检索。",
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "app.services.ai_gateway_service.OpenAICompatibleAdapter.complete",
        fake_complete,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "筛选 985/211 候选人"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["message"] == "## 筛选结果\n\n已按 985/211 条件检索。"
    assert completion_calls == 2
    database = ai_client.app.state.database
    with database.session_factory() as session:
        runs = list(
            session.scalars(
                select(AiRun).where(AiRun.feature == "recruiting_agent_turn")
            )
        )
        invocations = list(session.scalars(select(ApiInvocation)))
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert len(invocations) == 2
    assert {item.ai_run_id for item in invocations} == {runs[0].id}
    assert [item.attempt_no for item in invocations] == [1, 2]


def test_agent_unexpected_exception_never_becomes_raw_internal_server_error(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    def unexpected_failure(*args, **kwargs):
        raise RuntimeError("unexpected agent failure")

    monkeypatch.setattr("app.main.run_recruiting_agent_turn", unexpected_failure)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "查看当前 JD 排行榜"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "agent_service_unavailable"


def test_agent_never_uses_an_original_jd_without_matching_requirements(
    client: TestClient,
) -> None:
    original = client.post(
        "/v1/jobs/publish-original",
        json={
            "title": "Original source JD",
            "jd_text": "This source JD must not invoke AI matching.",
        },
    )
    assert original.status_code == 200, original.text
    job_version_id = original.json()["job_version_id"]

    database = client.app.state.database
    with database.session_factory() as session:
        assert _resolve_job(session, job_version_id) is None


def test_agent_starts_current_job_batch_with_runtime_settings(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    calls = 0
    captured: dict[str, object] = {}

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-start-batch",
                        "type": "function",
                        "function": {
                            "name": "start_current_job_match_batch",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        return {"content": "已启动当前 JD 的批量匹配。"}

    def fake_enqueue(session, *, job_version_id, settings):
        captured["job_version_id"] = job_version_id
        captured["settings"] = settings
        return SimpleNamespace(batch_id="batch-001", total_count=2, status="queued")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service._resolve_job",
        lambda session, requested_job_version_id: ResolvedJob(
            job_version_id="job-version-001",
            title="Backend Engineer",
        ),
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.enqueue_job_version_match_batch",
        fake_enqueue,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "为当前 JD 批量匹配"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_id"] == "batch-001"
    assert captured == {
        "job_version_id": "job-version-001",
        "settings": ai_client.app.state.settings,
    }


def test_agent_search_supports_full_recruiter_filter_contract(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    calls = 0
    captured: dict[str, object] = {}

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-complete-search",
                        "type": "function",
                        "function": {
                            "name": "search_candidates",
                            "arguments": json.dumps(
                                {
                                    "is_985_211": True,
                                    "min_employment_months": 36,
                                    "min_employment_or_internship_months": 42,
                                    "education_any_of": [
                                        {
                                            "degree_in": ["master"],
                                            "school_name_contains": ["清华大学"],
                                            "major_contains": ["计算机"],
                                            "institution_classifications_any_of": ["211"],
                                            "min_average_score": 85,
                                            "max_rank_position": 10,
                                        }
                                    ],
                                    "experience_any_of": [
                                        {
                                            "experience_types": [
                                                "employment",
                                                "internship",
                                            ],
                                            "organization_name_contains": ["Acme"],
                                            "title_contains": ["Engineer"],
                                            "leadership_contexts_any_of": ["company"],
                                            "leadership_roles_any_of": ["主管"],
                                            "award_levels_any_of": ["national"],
                                            "award_result_contains": ["一等奖"],
                                        }
                                    ],
                                    "skill_categories_any_of": ["software"],
                                    "skills_all_of": ["Python", "SQL"],
                                    "skills_any_of": ["Kubernetes", "Ray"],
                                    "language_credentials_any_of": [
                                        {"credential_code": "cet4", "min_score": 500},
                                        {"credential_code": "ielts", "min_score": 6.5},
                                    ],
                                    "scholarship_status": "present",
                                    "scholarship_levels_any_of": ["national"],
                                    "scholarship_name_contains": ["国家奖学金"],
                                    "competition_status": "present",
                                    "competition_award_status": "present",
                                    "leadership_any_of": [
                                        {"contexts_any_of": ["company"], "roles_any_of": ["经理"]}
                                    ],
                                    "keywords": ["CET-4"],
                                    "keyword_match_mode": "broad",
                                    "keywords_all_of": ["FastAPI"],
                                    "keywords_any_of": ["LLM", "Agent"],
                                }
                            ),
                        },
                    }
                ],
            }
        return {"content": "已按完整条件完成筛选。"}

    def fake_search(session, request):
        captured["request"] = request
        return SimpleNamespace(items=[], needs_review_count=0)

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.search_candidates",
        fake_search,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "筛 985/211、清华计算机硕士、3 年工作且工作加实习 42 个月的 Python 人才"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "search_candidates"
    request = captured["request"]
    assert request.is_985_211 is True
    assert request.min_employment_months == 36
    assert request.min_employment_or_internship_months == 42
    assert request.education_any_of[0].degree_in == ["master"]
    assert request.education_any_of[0].school_name_contains == ["清华大学"]
    assert request.education_any_of[0].major_contains == ["计算机"]
    assert request.education_any_of[0].institution_classifications_any_of == ["211"]
    assert request.education_any_of[0].min_average_score == 85
    assert request.education_any_of[0].max_rank_position == 10
    assert request.experience_any_of[0].experience_types == ["employment", "internship"]
    assert request.experience_any_of[0].organization_name_contains == ["Acme"]
    assert request.experience_any_of[0].title_contains == ["Engineer"]
    assert request.experience_any_of[0].leadership_contexts_any_of == ["company"]
    assert request.experience_any_of[0].leadership_roles_any_of == ["主管"]
    assert request.experience_any_of[0].award_levels_any_of == ["national"]
    assert request.experience_any_of[0].award_result_contains == ["一等奖"]
    assert request.skill_categories_any_of == ["software"]
    assert request.skills_all_of == ["Python", "SQL"]
    assert request.skills_any_of == ["Kubernetes", "Ray"]
    assert [item.credential_code for item in request.language_credentials_any_of] == [
        "cet4", "ielts"
    ]
    assert request.scholarship_levels_any_of == ["national"]
    assert request.scholarship_name_contains == ["国家奖学金"]
    assert request.leadership_any_of[0].roles_any_of == ["经理"]
    assert request.keywords == ["CET-4"]
    assert request.keyword_match_mode == "broad"
    assert request.keywords_all_of == ["FastAPI"]
    assert request.keywords_any_of == ["LLM", "Agent"]

    search_tool = next(
        item["function"] for item in _TOOLS if item["function"]["name"] == "search_candidates"
    )
    properties = search_tool["parameters"]["properties"]
    assert {
        "education_any_of", "experience_any_of", "skill_categories_any_of",
        "skills_all_of", "skills_any_of", "language_credentials_any_of",
        "scholarship_status", "scholarship_levels_any_of",
        "scholarship_name_contains", "competition_status",
        "competition_award_status", "leadership_any_of", "keywords",
        "keyword_match_mode", "keywords_all_of", "keywords_any_of",
    }.issubset(properties)


def test_agent_runs_current_candidate_score_with_existing_template(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education 清华大学 计算机 工作经历 "
            "Acme Python Engineer Skills Python SQL"
        ),
    )
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    template_id = template.json()["template_id"]
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            # The model receives only server-owned template IDs, not an
            # unbounded template-creation surface.
            assert template_id in messages[-1]["content"]
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-score-current",
                        "type": "function",
                        "function": {
                            "name": "score_current_candidate",
                            "arguments": json.dumps({"template_id": template_id}),
                        },
                    }
                ],
            }
        return {"content": "## 评分结果\n\n已生成评分，请结合证据与不确定项判断。"}

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        _fake_score_provider,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "用现有模板给当前候选人评分", "resume_id": resume_id},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["intent"] == "score_current_candidate"
    assert payload["message"].startswith("## 评分结果")
    assert payload["tool_trace"] == [
        {
            "tool": "候选人评分",
            "summary": "已按“Backend Engineer”v1 为当前候选人生成 44.0 分评分",
        }
    ]
    assert payload["actions"] == [
        {
            "action": "open_resume",
            "label": "打开候选人评分详情",
            "resume_id": resume_id,
        }
    ]
    saved_scores = ai_client.get(f"/v1/resumes/{resume_id}/scores")
    assert saved_scores.status_code == 200, saved_scores.text
    assert len(saved_scores.json()) == 1
    assert saved_scores.json()[0]["template_id"] == template_id


def test_agent_never_runs_score_with_an_unlisted_template(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    calls = 0
    score_called = False

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-invalid-score-template",
                        "type": "function",
                        "function": {
                            "name": "score_current_candidate",
                            "arguments": json.dumps({"template_id": "invented-template"}),
                        },
                    }
                ],
            }
        assert "不存在或已归档" in messages[-1]["content"]
        return {"content": "当前模板不可用，未执行评分。"}

    def unexpected_score(*args, **kwargs):
        nonlocal score_called
        score_called = True
        raise AssertionError("unlisted template must not reach the scoring service")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.run_resume_score",
        unexpected_score,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "给当前候选人评分", "resume_id": "resume-not-real"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "score_current_candidate"
    assert score_called is False
