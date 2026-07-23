from __future__ import annotations

from sqlalchemy import select

from app.models import AiRoutePolicy, AiRun, ResumeScoreBatch
from app.services import resume_score_batch_service
from app.services.ai_gateway_service import active_legacy_payload_executor
from test_filter_mvp_contract import _save_ready_resume


def _template_payload() -> dict[str, object]:
    return {
        "name": "Gateway scoring template",
        "dimensions": [
            {
                "label": "Skills",
                "weight": 100,
                "guidance": "Use explicit skill facts only.",
            }
        ],
    }


def _score_provider_inside_gateway(**kwargs: object) -> dict[str, object]:
    # The legacy prompt helper is deliberately still used by the business
    # service, but it must execute while the gateway transport context is
    # installed.  This prevents provider/model selection from leaking back
    # into the score domain.
    assert active_legacy_payload_executor() is not None
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    fact_id = snapshot["skills"][0]["fact_id"]
    dimensions = kwargs["dimensions"]
    assert isinstance(dimensions, list)
    assert len(dimensions) == 1
    assert isinstance(dimensions[0], dict)
    return {
        "schema_version": "resume_score.v1",
        "dimension_scores": [
            {
                "key": str(dimensions[0]["key"]),
                "raw_score": 75,
                "rationale": "An explicit skill fact is available.",
                "fact_ids": [fact_id],
                "uncertainties": [],
            }
        ],
        "overall_summary": "Grounded gateway score.",
        "risk_flags": [],
        "needs_human_review": False,
    }


def _summary_provider_inside_gateway(**kwargs: object) -> dict[str, object]:
    assert active_legacy_payload_executor() is not None
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    fact_id = snapshot["skills"][0]["fact_id"]
    return {
        "schema_version": "resume_summary.v1",
        "sections": {
            "candidate_positioning": {"content": "候选人具备明确技能事实。", "fact_ids": [fact_id]},
            "education_background": {"content": "学历信息以简历事实为准。", "fact_ids": []},
            "work_and_internship": {"content": "工作经历以简历事实为准。", "fact_ids": []},
            "core_skills": {"content": "已提取到明确技能。", "fact_ids": [fact_id]},
            "representative_projects": {"content": "暂未提取到可引用项目事实。", "fact_ids": []},
            "strengths": {"content": "技能信息可用于后续筛选。", "fact_ids": [fact_id]},
            "verification_items": {"content": "其余信息建议在面试中核实。", "fact_ids": []},
        },
    }


def test_score_and_summary_create_gateway_runs(ai_client, monkeypatch) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
    )
    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        _score_provider_inside_gateway,
    )
    monkeypatch.setattr(
        "app.services.summary_service.summarize_resume_fact_snapshot",
        _summary_provider_inside_gateway,
    )

    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    score = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert score.status_code == 200, score.text
    summary = ai_client.post(f"/v1/resumes/{resume_id}/summaries")
    assert summary.status_code == 200, summary.text

    database = ai_client.app.state.database
    with database.session_factory() as session:
        runs = session.scalars(
            select(AiRun)
            .where(AiRun.feature.in_(("resume_score", "resume_summary")))
            .order_by(AiRun.feature)
        ).all()
    assert [run.feature for run in runs] == ["resume_score", "resume_summary"]
    assert all(run.status == "succeeded" for run in runs)
    assert all(run.route_policy_version_id for run in runs)
    assert {run.contract_version for run in runs} == {
        "resume_score.v1",
        "resume_summary.v1",
    }


def test_score_batch_keeps_enqueue_route_when_active_policy_changes(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
    )
    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        _score_provider_inside_gateway,
    )

    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    queued = ai_client.post(
        f"/v1/score-templates/{template.json()['template_id']}/score-all"
    )
    assert queued.status_code == 200, queued.text

    database = ai_client.app.state.database
    with database.session_factory() as session:
        batch = session.get(ResumeScoreBatch, queued.json()["batch_id"])
        assert batch is not None
        route_policy_version_id = batch.ai_route_policy_version_id
        assert route_policy_version_id
        policy = session.scalar(
            select(AiRoutePolicy).where(AiRoutePolicy.feature == "resume_score")
        )
        assert policy is not None
        policy.enabled = False
        session.commit()

    # An unpinned worker would now see ``ai_route_disabled``.  The queued
    # item succeeds because it forwards the immutable enqueue-time pin.
    assert resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="score-route-pin-test-worker",
    )

    with database.session_factory() as session:
        batch = session.get(ResumeScoreBatch, queued.json()["batch_id"])
        assert batch is not None
        assert batch.status == "completed"
        run = session.scalar(
            select(AiRun)
            .where(
                AiRun.feature == "resume_score",
                AiRun.business_ref_id.like(f"{resume_id}:%"),
            )
        )
        assert run is not None
        assert run.route_policy_version_id == route_policy_version_id


def test_score_worker_claim_persists_route_pin_for_legacy_null_batch(
    ai_client,
) -> None:
    _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL"
        ),
    )
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    queued = ai_client.post(
        f"/v1/score-templates/{template.json()['template_id']}/score-all"
    )
    assert queued.status_code == 200, queued.text

    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    with database.session_factory() as session:
        batch = session.get(ResumeScoreBatch, queued.json()["batch_id"])
        assert batch is not None
        expected_route_id = batch.ai_route_policy_version_id
        assert expected_route_id is not None
        batch.ai_route_policy_version_id = None
        session.commit()

    claimed = resume_score_batch_service._claim_next_item(
        database,
        settings=settings,
        worker_id="legacy-null-score-pin-test-worker",
    )
    assert claimed is not None
    assert claimed.ai_route_policy_version_id == expected_route_id
    with database.session_factory() as session:
        batch = session.get(ResumeScoreBatch, queued.json()["batch_id"])
        assert batch is not None
        assert batch.ai_route_policy_version_id == expected_route_id
