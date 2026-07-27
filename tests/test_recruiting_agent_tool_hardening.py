from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.schemas import (
    RecruitingAgentCandidate,
    RecruitingAgentSearchSummary,
    RecruitingAgentToolTrace,
)
from app.services import recruiting_agent_service
from test_resume_flow import create_candidate, replace_page_evidence, upload_text_resume


def _create_confirmed_job(client: TestClient) -> str:
    response = client.post(
        "/v1/jobs",
        json={
            "title": "Agent tool hardening fixture",
            "jd_text": "Python service development.",
            "requirements": {"must_have": ["Python"], "preferred": []},
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["job_version_id"])


def _tool_call(*, name: str, arguments: object, call_id: str) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _create_ready_resume(client: TestClient) -> str:
    """Create an active, ready resume so scope reads retain the opaque ID."""

    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(client, resume_id, "北京大学 本科")
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


def _server_search_scope(resume_id: str) -> recruiting_agent_service.ToolRun:
    """Return a server-produced Agent scope without browser candidate IDs."""

    return recruiting_agent_service.ToolRun(
        payload={"result_count": 1},
        cards=[
            RecruitingAgentCandidate(
                candidate_id="server-owned-agent-scope-candidate",
                resume_id=resume_id,
                display_name="范围内候选人",
                detail="测试范围内候选人",
            )
        ],
        traces=[
            RecruitingAgentToolTrace(
                tool="简历筛选",
                summary="已完成候选人筛选：找到 1 人",
            )
        ],
        search_summary=RecruitingAgentSearchSummary(
            confirmed_count=1,
            displayed_count=1,
        ),
        intent="search_candidates",
        context_resume_ids=[resume_id],
    )


def _create_active_search_scope(
    ai_client: TestClient,
    monkeypatch,
    *,
    job_version_id: str,
) -> tuple[dict[str, object], str]:
    """Create a conversation through the real Agent graph and save one scope."""

    calls = 0
    resume_id = _create_ready_resume(ai_client)

    def fake_completion(*, settings, messages):
        nonlocal calls
        del settings, messages
        calls += 1
        if calls == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="search_candidates",
                        arguments={"skills_all_of": ["Python"]},
                        call_id="create-server-scope",
                    )
                ],
            }
        assert calls == 2
        return {"content": "已保存刚才筛选出的候选人范围。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(
        recruiting_agent_service,
        "_search",
        lambda session, arguments: _server_search_scope(resume_id),
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "筛选 Python 候选人", "job_version_id": job_version_id},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["active_context"]["candidate_count"] == 1
    assert payload["active_context"]["candidate_set_source"] == "agent_search"
    assert calls == 2
    return payload, resume_id


def test_unknown_search_tool_key_does_not_fall_back_to_a_broader_search(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A misspelled model filter must not silently become an unfiltered query."""

    search_called = False
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings
        completions += 1
        if completions == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="search_candidates",
                        arguments={
                            "skills_all_of": ["Python"],
                            "unknown_model_filter": "must-not-be-ignored",
                        },
                        call_id="unknown-search-field",
                    )
                ],
            }
        assert completions == 2
        tool_payload = json.loads(str(messages[-1]["content"]))
        assert "未执行任何操作" in tool_payload["error"]
        return {"content": "筛选参数无效，未执行检索。"}

    def unexpected_search(*args, **kwargs):
        nonlocal search_called
        search_called = True
        raise AssertionError("unknown filter must not trigger a broader candidate search")

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(recruiting_agent_service, "search_candidates", unexpected_search)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找有 Python 技能的候选人"},
    )

    assert response.status_code == 200, response.text
    assert search_called is False
    assert response.json()["candidates"] == []
    assert response.json()["tool_trace"] == [
        {"tool": "Agent 工具", "summary": "工具调用参数无法识别，未执行任何操作。"}
    ]


def test_profile_draft_rejects_model_arguments_and_skips_mixed_candidate_tools(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A malformed draft call cannot become draft plus an immediate search."""

    generate_called = False
    search_called = False
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings, messages
        completions += 1
        assert completions == 1
        return {
            "content": None,
            "tool_calls": [
                _tool_call(
                    name="draft_talent_search_profile",
                    arguments={"profile_id": "browser-and-model-must-not-control-this"},
                    call_id="invalid-draft",
                ),
                _tool_call(
                    name="search_candidates",
                    arguments={"skills_all_of": ["Python"]},
                    call_id="must-not-run-search",
                ),
            ],
        }

    def unexpected_generate(*args, **kwargs):
        nonlocal generate_called
        generate_called = True
        raise AssertionError("invalid profile tool arguments must not generate a draft")

    def unexpected_search(*args, **kwargs):
        nonlocal search_called
        search_called = True
        raise AssertionError("a draft turn must not read candidates")

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(recruiting_agent_service, "generate_profile", unexpected_generate)
    monkeypatch.setattr(recruiting_agent_service, "search_candidates", unexpected_search)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找有 Python 经验的人"},
    )

    assert response.status_code == 200, response.text
    assert generate_called is False
    assert search_called is False
    payload = response.json()
    assert payload["talent_profile"] is None
    assert payload["candidates"] == []
    assert payload["intent"] == "help"
    summaries = [item["summary"] for item in payload["tool_trace"]]
    assert "本轮只生成画像草案，未执行其他候选人操作" in summaries
    assert "工具调用参数无法识别，未执行任何操作。" in summaries


def test_invalid_workspace_jd_batch_arguments_create_no_batch_side_effect(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """An invalid global JD batch call cannot enqueue a workspace-wide job."""

    expected_job_version_id = _create_confirmed_job(ai_client)
    enqueue_called = False
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings
        completions += 1
        if completions == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="start_current_job_match_batch",
                        arguments={"unexpected_scope": "all"},
                        call_id="invalid-workspace-batch",
                    )
                ],
            }
        assert completions == 2
        assert "未执行任何操作" in messages[-1]["content"]
        return {"content": "批量匹配参数无效，未创建任务。"}

    def unexpected_enqueue(*args, **kwargs):
        nonlocal enqueue_called
        enqueue_called = True
        raise AssertionError("invalid batch arguments must not enqueue any job")

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(
        recruiting_agent_service,
        "enqueue_job_version_match_batch",
        unexpected_enqueue,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "为当前 JD 创建批量匹配",
            "job_version_id": expected_job_version_id,
        },
    )

    assert response.status_code == 200, response.text
    assert enqueue_called is False
    assert response.json()["batch_id"] is None
    assert response.json()["tool_trace"] == [
        {"tool": "Agent 工具", "summary": "工具调用参数无法识别，未执行任何操作。"}
    ]


def test_oversized_single_model_tool_response_executes_zero_tools(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """Five model calls in one message are rejected atomically before any tool runs."""

    completions = 0
    search_called = False

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings, messages
        completions += 1
        assert completions == 1
        return {
            "content": None,
            "tool_calls": [
                _tool_call(
                    name="search_candidates",
                    arguments={"skills_all_of": ["Python"]},
                    call_id=f"too-many-calls-{index}",
                )
                for index in range(5)
            ],
        }

    def unexpected_search(*args, **kwargs):
        nonlocal search_called
        search_called = True
        raise AssertionError("oversized tool-call response must execute zero tools")

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(recruiting_agent_service, "search_candidates", unexpected_search)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "找 Python 候选人"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert search_called is False
    assert completions == 1
    assert payload["message"] == "本次请求包含过多操作，未执行任何工具。请拆分后重试。"
    assert payload["candidates"] == []
    assert payload["tool_trace"] == [
        {"tool": "Agent 工具", "summary": "工具调用数量超出单轮上限，未执行任何操作"}
    ]


def test_wrong_global_ranking_tool_is_forced_into_the_active_candidate_scope(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """“刚才筛选的人” overrides a model's accidental global ranking call."""

    job_version_id = _create_confirmed_job(ai_client)
    created, scope_resume_id = _create_active_search_scope(
        ai_client,
        monkeypatch,
        job_version_id=job_version_id,
    )
    global_ranking_called = False
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings, messages
        completions += 1
        if completions == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="get_current_job_ranking",
                        arguments={"limit": 10},
                        call_id="wrong-global-ranking",
                    )
                ],
            }
        assert completions == 2
        return {"content": "已仅在刚才筛选的人中完成当前 JD 排名。"}

    def unexpected_global_ranking(*args, **kwargs):
        nonlocal global_ranking_called
        global_ranking_called = True
        raise AssertionError("active-scope wording must not call the global ranking tool")

    def fake_matches(session, *, job_version_id):
        assert job_version_id == created["active_context"]["active_job_version_id"]
        return [
            SimpleNamespace(
                candidate_id="server-owned-agent-scope-candidate",
                resume_id=scope_resume_id,
                candidate_display_name="范围内候选人",
                total_score=81.0,
                hard_requirement_status="met",
            ),
            SimpleNamespace(
                candidate_id="outside-active-scope",
                resume_id="outside-active-scope-resume",
                candidate_display_name="范围外候选人",
                total_score=99.0,
                hard_requirement_status="met",
            ),
        ]

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(recruiting_agent_service, "_ranking", unexpected_global_ranking)
    monkeypatch.setattr(recruiting_agent_service, "list_job_version_matches", fake_matches)

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "请在刚才筛选的人里按当前 JD 排名",
            "conversation_id": created["conversation_id"],
            "context_version": created["context_version"],
        },
    )

    assert response.status_code == 200, response.text
    assert global_ranking_called is False
    assert [item["resume_id"] for item in response.json()["candidates"]] == [
        scope_resume_id
    ]
    assert response.json()["tool_trace"][0]["tool"] == "当前会话 JD 排名"


def test_wrong_global_batch_tool_is_forced_into_the_active_candidate_scope(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """“这些人” overrides a model's accidental workspace-wide batch call."""

    job_version_id = _create_confirmed_job(ai_client)
    created, scope_resume_id = _create_active_search_scope(
        ai_client,
        monkeypatch,
        job_version_id=job_version_id,
    )
    global_batch_called = False
    captured: dict[str, object] = {}
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings, messages
        completions += 1
        if completions == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="start_current_job_match_batch",
                        arguments={},
                        call_id="wrong-global-batch",
                    )
                ],
            }
        assert completions == 2
        return {"content": "已只为这些人创建当前 JD 匹配任务。"}

    def unexpected_global_batch(*args, **kwargs):
        nonlocal global_batch_called
        global_batch_called = True
        raise AssertionError("active-scope wording must not create a global batch")

    def fake_enqueue(session, *, job_version_id, settings, resume_ids):
        captured["job_version_id"] = job_version_id
        captured["settings"] = settings
        captured["resume_ids"] = resume_ids
        return SimpleNamespace(batch_id="scoped-batch", status="queued")

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(recruiting_agent_service, "_start_batch", unexpected_global_batch)
    monkeypatch.setattr(
        recruiting_agent_service,
        "enqueue_job_version_match_batch",
        fake_enqueue,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "给这些人做当前 JD 匹配",
            "conversation_id": created["conversation_id"],
            "context_version": created["context_version"],
        },
    )

    assert response.status_code == 200, response.text
    assert global_batch_called is False
    assert response.json()["batch_id"] == "scoped-batch"
    assert captured == {
        "job_version_id": created["active_context"]["active_job_version_id"],
        "settings": ai_client.app.state.settings,
        "resume_ids": [scope_resume_id],
    }
    assert response.json()["tool_trace"][0]["tool"] == "当前会话 JD 匹配"


def test_same_model_response_search_then_global_ranking_uses_the_new_scope(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """A model's legacy ranking name cannot skip a just-created private scope.

    Function calls from one provider response execute in order.  The model may
    search and then accidentally use the old global ranking tool in that same
    response, so the service must materialize the new server-produced scope
    before its defensive routing turns the second call into a scoped ranking.
    """

    expected_job_version_id = _create_confirmed_job(ai_client)
    scope_resume_id = _create_ready_resume(ai_client)
    completions = 0

    def fake_completion(*, settings, messages):
        nonlocal completions
        del settings, messages
        completions += 1
        if completions == 1:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="search_candidates",
                        arguments={"skills_all_of": ["Python"]},
                        call_id="same-turn-search",
                    ),
                    _tool_call(
                        name="get_current_job_ranking",
                        arguments={"limit": 10},
                        call_id="same-turn-legacy-ranking",
                    ),
                ],
            }
        assert completions == 2
        return {"content": "已只在本次筛选出的候选人中完成当前 JD 排名。"}

    def fake_matches(session, *, job_version_id):
        assert job_version_id == expected_job_version_id
        return [
            SimpleNamespace(
                candidate_id="server-owned-agent-scope-candidate",
                resume_id=scope_resume_id,
                candidate_display_name="范围内候选人",
                total_score=82.0,
                hard_requirement_status="met",
            ),
            SimpleNamespace(
                candidate_id="outside-active-scope",
                resume_id="outside-active-scope-resume",
                candidate_display_name="范围外候选人",
                total_score=99.0,
                hard_requirement_status="met",
            ),
        ]

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(
        recruiting_agent_service,
        "_search",
        lambda session, arguments: _server_search_scope(scope_resume_id),
    )
    monkeypatch.setattr(
        recruiting_agent_service,
        "list_job_version_matches",
        fake_matches,
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={
            "message": "先筛选 Python 候选人，再按当前 JD 排名",
            "job_version_id": expected_job_version_id,
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["resume_id"] for item in payload["candidates"]] == [scope_resume_id]
    assert payload["active_context"]["candidate_set_source"] == "agent_search"
    assert payload["active_context"]["candidate_count"] == 1
    assert payload["active_context"]["active_job_version_id"] == expected_job_version_id
    assert [item["tool"] for item in payload["tool_trace"]] == [
        "简历筛选",
        "当前会话 JD 排名",
    ]


def test_four_valid_tool_rounds_still_receive_a_tool_free_final_reply(
    ai_client: TestClient,
    monkeypatch,
) -> None:
    """The bounded graph closes normally after four tool rounds, not with 503."""

    model_tool_flags: list[bool] = []
    completion_count = 0

    def fake_completion(*, settings, messages, tools_enabled=True):
        nonlocal completion_count
        del settings, messages
        completion_count += 1
        model_tool_flags.append(tools_enabled)
        if completion_count <= 4:
            return {
                "content": None,
                "tool_calls": [
                    _tool_call(
                        name="search_candidates",
                        arguments={"skills_all_of": ["Python"]},
                        call_id=f"bounded-tool-round-{completion_count}",
                    )
                ],
            }
        assert completion_count == 5
        assert tools_enabled is False
        return {"content": "四轮工具查询已完成，以上结果仅供招聘人员复核。"}

    monkeypatch.setattr(recruiting_agent_service, "_model_completion", fake_completion)
    monkeypatch.setattr(
        recruiting_agent_service,
        "_search",
        lambda session, arguments: recruiting_agent_service.ToolRun(
            payload={"result_count": 0},
            intent="search_candidates",
            context_resume_ids=[],
        ),
    )

    response = ai_client.post(
        "/v1/recruiting-agent/turns",
        json={"message": "请连续优化筛选条件后给出结论"},
    )

    assert response.status_code == 200, response.text
    assert model_tool_flags == [True, True, True, True, False]
    assert response.json()["message"] == "四轮工具查询已完成，以上结果仅供招聘人员复核。"
