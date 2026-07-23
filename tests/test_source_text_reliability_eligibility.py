from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import JobMatchBatchItem, Resume
from app.schemas import JobCreate, JobRequirements
from app.services import job_match_batch_service
from test_filter_mvp_contract import _save_ready_resume
from test_score_service import _template_payload


_SOURCE_TEXT = (
    "\u6559\u80b2\u7ecf\u5386 \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a "
    "\u5de5\u4f5c\u7ecf\u5386 Acme Python Engineer \u6280\u80fd Python SQL"
)
_UNRELIABLE_FLAGS = (
    "source_text_unreliable",
    "page_1_source_text_unreliable",
    "page_1_possible_mojibake",
)


def _set_quality_flags(client, *, resume_id: str, flags: list[str]) -> None:
    with client.app.state.database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        resume.quality_flags = flags
        session.commit()


@pytest.mark.parametrize("quality_flag", _UNRELIABLE_FLAGS)
def test_unreliable_source_is_excluded_from_search_and_counted_for_repair(
    client,
    quality_flag: str,
) -> None:
    _, unreliable_resume_id = _save_ready_resume(client, source_text=_SOURCE_TEXT)
    _set_quality_flags(
        client,
        resume_id=unreliable_resume_id,
        flags=[quality_flag],
    )
    _, recovered_resume_id = _save_ready_resume(client, source_text=_SOURCE_TEXT)
    _set_quality_flags(
        client,
        resume_id=recovered_resume_id,
        flags=["page_1_pymupdf_text_recovered"],
    )

    response = client.post("/v1/candidates/search", json={"limit": 10})

    assert response.status_code == 200, response.text
    assert [item["resume_id"] for item in response.json()["items"]] == [
        recovered_resume_id
    ]
    assert response.json()["needs_review_count"] == 1


def test_unreliable_source_blocks_direct_score_and_jd_match(ai_client) -> None:
    _, resume_id = _save_ready_resume(ai_client, source_text=_SOURCE_TEXT)
    _set_quality_flags(
        ai_client,
        resume_id=resume_id,
        flags=["page_1_source_text_unreliable"],
    )

    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    score = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert score.status_code == 409
    assert score.json()["detail"] == "resume_source_text_unreliable"

    job = ai_client.post(
        "/v1/jobs",
        json=JobCreate(
            title="Backend Engineer",
            jd_text="Python experience is required.",
            requirements=JobRequirements(must_have=["Python experience"]),
        ).model_dump(),
    )
    assert job.status_code == 200, job.text
    job_version_id = job.json()["job_version_id"]

    direct_match = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": job_version_id},
    )
    assert direct_match.status_code == 409
    assert direct_match.json()["detail"] == "resume_source_text_unreliable"

    batch = ai_client.post(f"/v1/job-versions/{job_version_id}/match-all")
    assert batch.status_code == 200, batch.text
    assert batch.json()["status"] == "completed"
    assert batch.json()["total_count"] == 0


def test_job_match_worker_rechecks_source_quality_before_model_call(ai_client) -> None:
    _, resume_id = _save_ready_resume(ai_client, source_text=_SOURCE_TEXT)
    job = ai_client.post(
        "/v1/jobs",
        json=JobCreate(
            title="Backend Engineer",
            jd_text="Python experience is required.",
            requirements=JobRequirements(must_have=["Python experience"]),
        ).model_dump(),
    )
    assert job.status_code == 200, job.text
    job_version_id = job.json()["job_version_id"]
    queued = ai_client.post(f"/v1/job-versions/{job_version_id}/match-all")
    assert queued.status_code == 200, queued.text
    assert queued.json()["total_count"] == 1

    _set_quality_flags(
        ai_client,
        resume_id=resume_id,
        flags=["source_text_unreliable"],
    )
    database = ai_client.app.state.database
    assert job_match_batch_service.run_job_match_batch_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="quality-check-worker",
    )
    with database.session_factory() as session:
        item = session.scalar(
            select(JobMatchBatchItem).where(
                JobMatchBatchItem.batch_id == queued.json()["batch_id"]
            )
        )
        assert item is not None
        assert item.status == "failed"
        assert item.last_error == "resume_source_text_unreliable"
