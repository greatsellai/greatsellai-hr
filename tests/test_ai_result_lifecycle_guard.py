from __future__ import annotations

from sqlalchemy import select

from app.models import JobMatch, Resume, ResumeFactSnapshot, ResumeScore, ResumeSummary
from app.schemas import JobCreate, JobRequirements
from app.services import job_match_batch_service, job_service
from app.services.candidate_data_lifecycle_service import delete_resume
from test_filter_mvp_contract import _save_ready_resume
from test_job_match_batches import _batch_match_output
from test_score_service import _fake_score_provider, _template_payload
from test_summary_service import _fake_summary_provider


def _delete_resume_in_separate_session(client, *, resume_id: str) -> None:
    """Simulate a recruiter deleting the privacy root during a model call."""

    database = client.app.state.database
    with database.session_factory() as deletion_session:
        delete_resume(
            deletion_session,
            settings=client.app.state.settings,
            resume_id=resume_id,
            actor_user_id=None,
            reason="candidate_request",
            private_note=None,
            source_kind="test",
        )
        deletion_session.commit()


def _replace_fact_snapshot_in_separate_session(client, *, resume_id: str) -> None:
    """Simulate a reviewer saving a newer immutable facts revision mid-call."""

    database = client.app.state.database
    with database.session_factory() as review_session:
        resume = review_session.get(Resume, resume_id)
        assert resume is not None
        current_snapshot = review_session.scalar(
            select(ResumeFactSnapshot)
            .where(ResumeFactSnapshot.resume_id == resume.id)
            .order_by(ResumeFactSnapshot.facts_version.desc())
        )
        assert current_snapshot is not None
        resume.facts_version += 1
        review_session.add(
            ResumeFactSnapshot(
                resume_id=resume.id,
                facts_version=resume.facts_version,
                canonical_facts_json=current_snapshot.canonical_facts_json,
                facts_sha256=current_snapshot.facts_sha256,
                source_block_ids=current_snapshot.source_block_ids,
                created_by="test-reviewer",
            )
        )
        review_session.commit()


def test_score_result_is_not_written_after_resume_is_deleted_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
    )
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text

    def delete_then_score(**kwargs: object) -> dict[str, object]:
        _delete_resume_in_separate_session(ai_client, resume_id=resume_id)
        return _fake_score_provider(**kwargs)

    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        delete_then_score,
    )
    response = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resume_changed_before_scoring_completed"

    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(
            select(ResumeScore.id).where(ResumeScore.resume_id == resume_id)
        ) is None


def test_score_result_is_not_written_after_facts_change_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
    )
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text

    def replace_facts_then_score(**kwargs: object) -> dict[str, object]:
        _replace_fact_snapshot_in_separate_session(ai_client, resume_id=resume_id)
        return _fake_score_provider(**kwargs)

    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        replace_facts_then_score,
    )
    response = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resume_changed_before_scoring_completed"

    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(
            select(ResumeScore.id).where(ResumeScore.resume_id == resume_id)
        ) is None


def test_summary_result_is_not_written_after_resume_is_deleted_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
    )

    def delete_then_summarize(**kwargs: object) -> dict[str, object]:
        _delete_resume_in_separate_session(ai_client, resume_id=resume_id)
        return _fake_summary_provider(**kwargs)

    monkeypatch.setattr(
        "app.services.summary_service.summarize_resume_fact_snapshot",
        delete_then_summarize,
    )
    response = ai_client.post(f"/v1/resumes/{resume_id}/summaries")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resume_changed_before_summary_completed"

    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(
            select(ResumeSummary.id).where(ResumeSummary.resume_id == resume_id)
        ) is None


def test_direct_job_match_result_is_not_written_after_resume_is_deleted_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    """The synchronous JD-match route has the same post-model guard as batches."""

    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 Acme Python Engineer Skills Python SQL",
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

    def delete_then_match(**kwargs: object) -> dict[str, object]:
        _delete_resume_in_separate_session(ai_client, resume_id=resume_id)
        return _batch_match_output(**kwargs)

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        delete_then_match,
    )
    response = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": created.json()["job_version_id"]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resume_changed_before_job_match_completed"
    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(
            select(JobMatch.id).where(JobMatch.resume_id == resume_id)
        ) is None


def test_direct_job_match_result_is_not_written_after_resume_facts_change_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    """A direct result cannot be attached to an obsolete fact snapshot."""

    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 Acme Python Engineer Skills Python SQL",
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

    def replace_facts_then_match(**kwargs: object) -> dict[str, object]:
        _replace_fact_snapshot_in_separate_session(ai_client, resume_id=resume_id)
        return _batch_match_output(**kwargs)

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        replace_facts_then_match,
    )
    response = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": created.json()["job_version_id"]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "resume_changed_before_job_match_completed"
    with ai_client.app.state.database.session_factory() as session:
        assert session.scalar(
            select(JobMatch.id).where(JobMatch.resume_id == resume_id)
        ) is None


def test_job_match_batch_rolls_back_result_after_resume_is_deleted_during_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="Education 清华大学 计算机 工作经历 Acme Python Engineer Skills Python SQL",
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

    def delete_then_match(**kwargs: object) -> dict[str, object]:
        _delete_resume_in_separate_session(ai_client, resume_id=resume_id)
        return _batch_match_output(**kwargs)

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        delete_then_match,
    )
    queued = ai_client.post(f"/v1/job-versions/{created.json()['job_version_id']}/match-all")
    assert queued.status_code == 200, queued.text

    database = ai_client.app.state.database
    assert job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="lifecycle-guard-test",
    )
    with database.session_factory() as session:
        assert session.scalar(select(JobMatch.id).where(JobMatch.resume_id == resume_id)) is None
