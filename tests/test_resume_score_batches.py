from __future__ import annotations

from sqlalchemy import select

from app.models import ResumeScoreBatchItem
from app.services import resume_score_batch_service
from app.services.deepseek_provider import DeepSeekProviderError, FACT_SNAPSHOT_SCHEMA_VERSION
from test_filter_mvp_contract import _save_ready_resume


def _template_payload() -> dict[str, object]:
    return {
        "name": "Batch Backend Engineer",
        "description": "Fixed 100-point batch scoring template.",
        "dimensions": [
            {
                "label": "Skills",
                "weight": 60,
                "guidance": "Assess only explicit relevant skills.",
            },
            {
                "label": "Experience",
                "weight": 40,
                "guidance": "Assess only explicit work evidence.",
            },
        ],
    }


def _score_output(**kwargs: object) -> dict[str, object]:
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["schema_version"] == FACT_SNAPSHOT_SCHEMA_VERSION
    fact_id = snapshot["skills"][0]["fact_id"]
    dimensions = kwargs["dimensions"]
    assert isinstance(dimensions, list)
    assert len(dimensions) == 2
    assert all(isinstance(dimension, dict) for dimension in dimensions)
    skill_key, experience_key = [str(dimension["key"]) for dimension in dimensions]
    return {
        "schema_version": "resume_score.v1",
        "dimension_scores": [
            {
                "key": skill_key,
                "raw_score": 40,
                "rationale": "Explicit Python evidence is present.",
                "fact_ids": [fact_id],
                "uncertainties": [],
            },
            {
                "key": experience_key,
                "raw_score": 50,
                "rationale": "One explicit employment record is present.",
                "fact_ids": [],
                "uncertainties": ["Dates need review."],
            },
        ],
        "overall_summary": "Grounded batch score.",
        "risk_flags": [],
        "needs_human_review": False,
    }


def test_score_batch_deduplicates_active_work_runs_and_reuses_cached_scores(
    ai_client,
    monkeypatch,
) -> None:
    source_text = "教育经历 清华大学 计算机 工作经历 Acme Python Engineer 技能 Python SQL"
    _, first_resume_id = _save_ready_resume(ai_client, source_text=source_text)
    _, second_resume_id = _save_ready_resume(ai_client, source_text=source_text)
    provider_calls: list[dict[str, object]] = []

    def fake_score_provider(**kwargs: object) -> dict[str, object]:
        snapshot = kwargs["fact_snapshot"]
        assert isinstance(snapshot, dict)
        provider_calls.append(snapshot)
        return _score_output(**kwargs)

    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        fake_score_provider,
    )

    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    template_id = template.json()["template_id"]

    queued = ai_client.post(f"/v1/score-templates/{template_id}/score-all")
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "queued"
    assert queued.json()["total_count"] == 2
    assert queued.json()["completed_count"] == 0
    assert queued.json()["cached_count"] == 0
    batch_id = queued.json()["batch_id"]

    # Two clicks while a batch is active must attach to the same durable work,
    # rather than enqueueing a duplicate model-call matrix.
    duplicate = ai_client.post(f"/v1/score-templates/{template_id}/score-all")
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["batch_id"] == batch_id
    assert duplicate.json()["status"] == "queued"

    database = ai_client.app.state.database
    assert resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-score-worker",
    )
    assert resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-score-worker",
    )
    assert not resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-score-worker",
    )
    assert len(provider_calls) == 2

    completed = ai_client.get(f"/v1/resume-score-batches/{batch_id}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["completed_count"] == 2
    assert completed.json()["failed_count"] == 0
    assert completed.json()["cached_count"] == 0

    items = ai_client.get(f"/v1/resume-score-batches/{batch_id}/items")
    assert items.status_code == 200, items.text
    assert {item["resume_id"] for item in items.json()} == {
        first_resume_id,
        second_resume_id,
    }
    assert {item["status"] for item in items.json()} == {"completed"}
    assert {item["attempt_count"] for item in items.json()} == {1}
    assert {item["was_cached"] for item in items.json()} == {False}
    assert all(item["resume_score_id"] for item in items.json())

    with database.session_factory() as session:
        persisted_items = session.execute(
            select(
                ResumeScoreBatchItem.status,
                ResumeScoreBatchItem.resume_score_id,
                ResumeScoreBatchItem.was_cached,
            ).where(ResumeScoreBatchItem.batch_id == batch_id)
        ).all()
    assert {item.status for item in persisted_items} == {"completed"}
    assert all(item.resume_score_id for item in persisted_items)
    assert {item.was_cached for item in persisted_items} == {False}

    for resume_id in (first_resume_id, second_resume_id):
        scores = ai_client.get(f"/v1/resumes/{resume_id}/scores")
        assert scores.status_code == 200, scores.text
        assert len(scores.json()) == 1
        assert scores.json()[0]["total_score"] == 44.0

    # Once completed, a new historical batch is allowed.  It must resolve
    # immediately from immutable template-version + fact-snapshot scores.
    cached = ai_client.post(f"/v1/score-templates/{template_id}/score-all")
    assert cached.status_code == 200, cached.text
    assert cached.json()["batch_id"] != batch_id
    assert cached.json()["status"] == "completed"
    assert cached.json()["total_count"] == 2
    assert cached.json()["completed_count"] == 2
    assert cached.json()["cached_count"] == 2
    assert len(provider_calls) == 2
    assert not resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-score-worker",
    )

    cached_items = ai_client.get(
        f"/v1/resume-score-batches/{cached.json()['batch_id']}/items"
    )
    assert cached_items.status_code == 200, cached_items.text
    assert {item["status"] for item in cached_items.json()} == {"completed"}
    assert {item["was_cached"] for item in cached_items.json()} == {True}


def test_score_batch_does_not_retry_non_transport_provider_failure(
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
    provider_calls = 0

    def reject_auth(**kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise DeepSeekProviderError("ai_provider_auth")

    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        reject_auth,
    )
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    queued = ai_client.post(
        f"/v1/score-templates/{template.json()['template_id']}/score-all"
    )
    assert queued.status_code == 200, queued.text
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings

    assert resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=settings,
        worker_id="score-terminal-error-test-worker",
    )
    with database.session_factory() as session:
        item = session.scalar(
            select(ResumeScoreBatchItem).where(
                ResumeScoreBatchItem.batch_id == queued.json()["batch_id"]
            )
        )
        assert item is not None
        assert item.status == "failed"
        assert item.attempt_count == 1
        assert item.next_attempt_at is None
        assert item.last_error == "ai_provider_auth"
    assert not resume_score_batch_service.run_resume_score_batch_worker_once(
        database,
        settings=settings,
        worker_id="score-terminal-error-test-worker",
    )
    assert provider_calls == 1
