from __future__ import annotations

from sqlalchemy import select

from app.models import AiRun
from app.services.ai_gateway_service import active_legacy_payload_executor
from test_tenant_isolation import _register_and_login, workspace_clients


def _template_payload(name: str = "后端工程师初筛") -> dict[str, object]:
    return {
        "name": name,
        "description": "关注候选人的 Python 和服务端实践。",
        "dimensions": [
            {
                "label": "核心技能",
                "weight": 60,
                "guidance": "只核验简历中明确记录的 Python、SQL 与服务端技能。",
            },
            {
                "label": "实践经历",
                "weight": 40,
                "guidance": "重点核验实际职责、项目结果和持续时间。",
            },
        ],
    }


def _fake_template_optimizer(**kwargs: object) -> dict[str, object]:
    assert active_legacy_payload_executor() is not None
    source = kwargs["existing_template"]
    assert isinstance(source, dict)
    assert source["name"] == "后端工程师初筛"
    return {
        "schema_version": "score_template_optimization.v1",
        "proposed_template": {
            "name": "模型返回的名称会由服务端改为安全副本名称",
            "description": "将模糊的岗位偏好转为可核验的简历事实与清晰分档。",
            "dimensions": [
                {
                    "label": "核心技术匹配",
                    "weight": 55,
                    "guidance": "仅依据明确列出的 Python、SQL、服务端框架和可验证项目职责评分；信息不足时标记待复核。",
                },
                {
                    "label": "工程实践证据",
                    "weight": 45,
                    "guidance": "关注清楚记载的交付结果、系统复杂度、质量实践与职责范围，不根据个人特征推断。",
                },
            ],
        },
        "improvement_notes": [
            "将宽泛的技能描述改为只基于可验证事实的评分要求。",
            "补充信息不足时需要人工复核的说明。",
        ],
    }


def test_optimize_existing_template_returns_a_non_persisted_copy(
    ai_client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.score_service.optimize_score_template",
        _fake_template_optimizer,
    )
    created = ai_client.post("/v1/score-templates", json=_template_payload())
    assert created.status_code == 200, created.text
    source = created.json()

    response = ai_client.post(f"/v1/score-templates/{source['template_id']}/optimize")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source_template_id"] == source["template_id"]
    assert payload["source_template_version"] == source["version"]
    assert payload["proposed_template"]["name"] == "后端工程师初筛（AI 优化）"
    assert [item["weight"] for item in payload["proposed_template"]["dimensions"]] == [55, 45]
    assert payload["improvement_notes"] == [
        "将宽泛的技能描述改为只基于可验证事实的评分要求。",
        "补充信息不足时需要人工复核的说明。",
    ]

    # Generating a draft never changes the source or creates a persisted rule.
    templates = ai_client.get("/v1/score-templates")
    assert templates.status_code == 200, templates.text
    assert templates.json() == [source]

    database = ai_client.app.state.database
    with database.session_factory() as session:
        run = session.scalar(
            select(AiRun).where(AiRun.feature == "score_template_optimize")
        )
    assert run is not None
    assert run.status == "succeeded"
    assert run.contract_version == "score_template_optimization.v1"
    assert run.prompt_revision == "score_template_optimization.v1"
    assert run.business_ref_id == f"{source['template_id']}:v1"

    # The reviewed proposal can use the ordinary creation API, which creates
    # a new rule instead of changing the source template in place.
    accepted = ai_client.post("/v1/score-templates", json=payload["proposed_template"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["template_id"] != source["template_id"]
    assert accepted.json()["name"] == "后端工程师初筛（AI 优化）"


def test_optimize_template_chooses_a_unique_copy_name(ai_client, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.score_service.optimize_score_template",
        _fake_template_optimizer,
    )
    source = ai_client.post("/v1/score-templates", json=_template_payload())
    assert source.status_code == 200, source.text
    existing_copy = ai_client.post(
        "/v1/score-templates",
        json=_template_payload("后端工程师初筛（AI 优化）"),
    )
    assert existing_copy.status_code == 200, existing_copy.text

    response = ai_client.post(
        f"/v1/score-templates/{source.json()['template_id']}/optimize"
    )
    assert response.status_code == 200, response.text
    assert response.json()["proposed_template"]["name"] == "后端工程师初筛（AI 优化 2）"


def test_optimize_template_rejects_unknown_source_without_ai_call(ai_client) -> None:
    response = ai_client.post("/v1/score-templates/missing-template/optimize")
    assert response.status_code == 404
    assert response.json()["detail"] == "score_template_not_found"


def test_optimize_template_rejects_an_all_unsafe_source_before_model_call(
    ai_client,
    monkeypatch,
) -> None:
    unsafe_source = _template_payload("Unsafe-only template")
    unsafe_source["dimensions"] = [
        {
            "label": "Age requirement",
            "weight": 50,
            "guidance": "Only consider age.",
        },
        {
            "label": "Gender preference",
            "weight": 50,
            "guidance": "Only consider gender.",
        },
    ]
    source = ai_client.post("/v1/score-templates", json=unsafe_source)
    assert source.status_code == 200, source.text

    def unexpected_model_call(**kwargs: object) -> dict[str, object]:
        raise AssertionError("an unsafe-only source must not reach the model")

    monkeypatch.setattr(
        "app.services.deepseek_provider.call_strict_function",
        unexpected_model_call,
    )
    response = ai_client.post(
        f"/v1/score-templates/{source.json()['template_id']}/optimize"
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["detail"]
        == "score_template_optimization_source_has_no_safe_dimensions"
    )


def test_optimize_template_does_not_cross_workspace_boundary(
    workspace_clients,
    monkeypatch,
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Optimizer workspace A",
        full_name="Optimizer A admin",
        email="optimizer-a@example.test",
        password="template-optimizer-password",
    )
    _register_and_login(
        client_b,
        organization_name="Optimizer workspace B",
        full_name="Optimizer B admin",
        email="optimizer-b@example.test",
        password="template-optimizer-password",
    )

    private_template = client_b.post(
        "/v1/score-templates",
        json=_template_payload("Private workspace template"),
    )
    assert private_template.status_code == 200, private_template.text

    def unexpected_provider_call(**kwargs: object) -> dict[str, object]:
        raise AssertionError("foreign templates must not be sent to the AI provider")

    monkeypatch.setattr(
        "app.services.score_service.optimize_score_template",
        unexpected_provider_call,
    )
    response = client_a.post(
        f"/v1/score-templates/{private_template.json()['template_id']}/optimize"
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "score_template_not_found"
