from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai import CompletionResult, NormalizedUsage, ToolCall
from app.models import AiRun, ApiInvocation
from app.services.ai_gateway_service import AiGatewayError
from app.services.recruiting_agent_service import ResolvedJob, _TOOLS, _resolve_job
from test_score_service import _template_payload
from test_resume_flow import create_candidate, replace_page_evidence, upload_text_resume


def _save_agent_source_only_resume(
    client: TestClient,
    *,
    source_text: str,
) -> str:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(client, resume_id, source_text)
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    return resume_id


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
    assert payload["tool_trace"][0]["summary"] == "已完成候选人筛选：找到 0 人"
    assert "{" not in payload["tool_trace"][0]["summary"]
    assert calls == 2


def test_agent_language_search_returns_source_grounded_confirmation_and_unconfirmed_count(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    confirmed_resume_id = _save_agent_source_only_resume(
        ai_client,
        source_text="教育经历 北京大学 本科。大学英语四级 CET-4 成绩 520。",
    )
    _save_agent_source_only_resume(
        ai_client,
        source_text="教育经历 北京大学 本科。具备英语沟通能力。",
    )
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
                        "id": "call-cet4-search",
                        "type": "function",
                        "function": {
                            "name": "search_candidates",
                            "arguments": json.dumps(
                                {
                                    "language_credentials_any_of": [
                                        {"credential_code": "cet4"}
                                    ]
                                }
                            ),
                        },
                    }
                ],
            }
        captured["tool_payload"] = json.loads(messages[-1]["content"])
        return {
            "content": (
                "找到 1 位简历明确提到英语四级的候选人。"
                "另有 1 份简历未确认英语四级，不代表未通过。"
            )
        }

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "给我找过了英语四级的人"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["search_summary"] == {
        "confirmed_count": 1,
        "displayed_count": 1,
        "unconfirmed_count": 1,
        "confirmation_basis": "已确认表示简历明确提及；未确认不代表未通过。",
    }
    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert candidate["resume_id"] == confirmed_resume_id
    assert candidate["verification_status"] == "confirmed"
    assert candidate["verification_evidence"] == [
        {
            "label": "大学英语四级（CET-4）",
            "source": "resume_text",
        }
    ]
    trace = payload["tool_trace"][0]["summary"]
    assert trace == "已完成大学英语四级（CET-4）检索：已确认 1 人，未确认 1 份"
    assert "{" not in trace
    assert "language_credentials_any_of" not in trace
    tool_payload = captured["tool_payload"]
    assert tool_payload["search_summary"]["confirmed_count"] == 1
    assert tool_payload["candidates"][0]["verification_evidence"] == [
        {
            "label": "大学英语四级（CET-4）",
            "source": "resume_text",
        }
    ]
    serialized_tool_payload = json.dumps(tool_payload, ensure_ascii=False)
    assert "大学英语四级 CET-4 成绩 520" not in serialized_tool_payload
    assert "original_filename" not in serialized_tool_payload
    serialized_response = json.dumps(payload, ensure_ascii=False)
    assert "page-001" not in serialized_response
    assert "original_filename" not in serialized_response


def test_agent_refined_search_replaces_prior_cards_and_summary(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    _save_agent_source_only_resume(
        ai_client,
        source_text="教育经历 北京大学 本科。大学英语四级 CET-4 成绩 520。",
    )
    calls = 0

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            arguments = {"language_credentials_any_of": [{"credential_code": "cet4"}]}
        elif calls == 2:
            arguments = {"language_credentials_any_of": [{"credential_code": "ielts"}]}
        else:
            return {"content": "已按更严格的雅思条件重新检索。"}
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-search-{calls}",
                    "type": "function",
                    "function": {
                        "name": "search_candidates",
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "先找英语四级，再收窄为雅思的人"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # The second search validly has no confirmed result.  Its zero-result
    # summary must not be paired with candidate cards from the first search.
    assert payload["candidates"] == []
    assert payload["search_summary"] == {
        "confirmed_count": 0,
        "displayed_count": 0,
        "unconfirmed_count": 1,
        "confirmation_basis": "已确认表示简历明确提及；未确认不代表未通过。",
    }
    assert [item["summary"] for item in payload["tool_trace"]] == [
        "已完成大学英语四级（CET-4）检索：已确认 1 人，未确认 0 份",
        "已完成雅思（IELTS）检索：已确认 0 人，未确认 1 份",
    ]


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

    def fake_search(session, request, **kwargs):
        captured.setdefault("requests", []).append(request)
        captured.setdefault("options", []).append(kwargs)
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
    request = captured["requests"][0]
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
    assert captured["options"][0] == {"include_source_language_evidence": True}
    assert len(captured["requests"]) == 2

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


def test_agent_rejects_client_supplied_candidate_binding(client: TestClient) -> None:
    response = client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 Python 候选人", "resume_id": "resume-not-real"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"] == ["body", "resume_id"] for item in detail)


def test_agent_model_context_has_no_selected_candidate_scope(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_completion(*, settings, messages):
        captured["context"] = messages[-1]["content"]
        return {"content": "已按当前工作区范围准备检索。"}

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 Python 候选人"},
    )

    assert response.status_code == 200, response.text
    prompt = str(captured["context"])
    serialized_context = prompt.split("当前工作台上下文：", 1)[1].split("\n\n用户请求：", 1)[0]
    context = json.loads(serialized_context)
    assert "current_resume_id" not in context
    assert "resume_id" not in context
    assert isinstance(context["current_score_templates"], list)
    tool_names = {item["function"]["name"] for item in _TOOLS}
    assert "explain_current_candidate_match" not in tool_names
    assert "score_current_candidate" not in tool_names
    assert "start_workspace_score_batch" in tool_names


def test_agent_starts_workspace_score_batch_with_existing_template(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    template_id = template.json()["template_id"]
    calls = 0
    captured: dict[str, object] = {}

    def fake_completion(*, settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert template_id in messages[-1]["content"]
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-workspace-score-batch",
                        "type": "function",
                        "function": {
                            "name": "start_workspace_score_batch",
                            "arguments": json.dumps({"template_id": template_id}),
                        },
                    }
                ],
            }
        return {"content": "已创建当前工作区的全量评分任务。"}

    def fake_enqueue(session, *, template_id, settings):
        captured["template_id"] = template_id
        captured["settings"] = settings
        return SimpleNamespace(
            batch_id="score-batch-001",
            template_id=template_id,
            template_name="Backend Engineer",
            template_version=1,
            status="queued",
            total_count=3,
            completed_count=0,
            cached_count=0,
        )

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.enqueue_resume_score_batch",
        fake_enqueue,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "按现有评分规则给当前工作区所有候选人评分"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["intent"] == "run_workspace_scoring"
    assert payload["batch_id"] == "score-batch-001"
    assert payload["tool_trace"] == [
        {
            "tool": "全量评分",
            "summary": "已按“Backend Engineer”v1 为当前工作区创建 3 份简历的评分任务",
        }
    ]
    assert payload["actions"] == [
        {
            "action": "open_score_workspace",
            "label": "打开评分工作台",
            "resume_id": None,
        }
    ]
    assert captured == {
        "template_id": template_id,
        "settings": ai_client.app.state.settings,
    }


def test_agent_never_starts_workspace_score_batch_with_an_unlisted_template(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    calls = 0
    enqueue_called = False

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
                            "name": "start_workspace_score_batch",
                            "arguments": json.dumps({"template_id": "invented-template"}),
                        },
                    }
                ],
            }
        assert "不存在或已归档" in messages[-1]["content"]
        return {"content": "当前评分规则不可用，未创建评分任务。"}

    def unexpected_enqueue(*args, **kwargs):
        nonlocal enqueue_called
        enqueue_called = True
        raise AssertionError("unlisted template must not reach the score batch service")

    monkeypatch.setattr(
        "app.services.recruiting_agent_service._model_completion",
        fake_completion,
    )
    monkeypatch.setattr(
        "app.services.recruiting_agent_service.enqueue_resume_score_batch",
        unexpected_enqueue,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "按评分规则给所有候选人评分"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["intent"] == "run_workspace_scoring"
    assert enqueue_called is False
