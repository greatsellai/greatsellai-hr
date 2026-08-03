from __future__ import annotations

from sqlalchemy import select

from app.models import Resume, ResumeAiExtractionJob
from app.services.resume_service import reconcile_legacy_completed_ai_resumes
from test_filter_mvp_contract import _save_ready_resume
from test_score_service import _fake_score_provider, _template_payload
from test_summary_service import _fake_summary_provider


def test_resume_library_returns_current_ai_summary_preview_and_score(
    ai_client,
    monkeypatch,
) -> None:
    candidate_id, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "教育经历 清华大学 计算机 本科。工作经历 "
            "Acme Python Engineer。技能 Python SQL"
        ),
    )
    monkeypatch.setattr(
        "app.services.summary_service.summarize_resume_fact_snapshot",
        _fake_summary_provider,
    )
    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        _fake_score_provider,
    )

    summary = ai_client.post(f"/v1/resumes/{resume_id}/summaries")
    assert summary.status_code == 200, summary.text
    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    score = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert score.status_code == 200, score.text

    response = ai_client.get("/v1/resume-library")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    item = payload["items"][0]
    assert set(item) == {
        "resume_id",
        "candidate_id",
        "display_name",
        "original_filename",
        "is_favorited",
        "created_at",
        "extraction_status",
        "ai_extraction_status",
        "ai_extraction_error",
        "candidate_name_extraction_status",
        "candidate_name_extraction_error",
        "ai_summary_status",
        "ai_summary_error",
        "is_active",
        "ingestion_source_type",
        "source_mailbox_config_id",
        "source_mailbox_label",
        "quality_flags",
        "graduation_month",
        "employment_months",
        "education_school",
        "highest_degree",
        "summary_preview",
        "summary_created_at",
        "score_total",
        "score_status",
        "score_template_name",
        "score_created_at",
    }
    assert item["resume_id"] == resume_id
    assert item["candidate_id"] == candidate_id
    assert item["display_name"] == "测试候选人"
    assert item["original_filename"] == "resume.pdf"
    assert item["is_favorited"] is False
    assert item["created_at"]
    assert item["extraction_status"] == "ready"
    assert item["ai_extraction_status"] == "queued"
    assert item["ai_extraction_error"] is None
    assert item["candidate_name_extraction_status"] == "succeeded"
    assert item["candidate_name_extraction_error"] is None
    assert item["ai_summary_status"] == "succeeded"
    assert item["ai_summary_error"] is None
    assert item["is_active"] is True
    assert item["ingestion_source_type"] == "manual_upload"
    assert item["source_mailbox_config_id"] is None
    assert item["source_mailbox_label"] is None
    assert item["quality_flags"] == []
    assert item["graduation_month"] is None
    assert item["employment_months"] == 0
    assert item["education_school"] == "清华大学"
    assert item["highest_degree"] == "bachelor"
    assert item["summary_preview"] == "Backend-oriented candidate."
    assert item["summary_created_at"] == summary.json()["created_at"]
    # All scoring dimensions now use a fixed 100-point scale: 40 * 60% +
    # 50 * 40% = 44.
    assert item["score_total"] == 44.0
    assert item["score_status"] == "succeeded"
    assert item["score_template_name"] == "Backend Engineer"
    assert item["score_created_at"] == score.json()["created_at"]


def test_resume_library_keeps_pending_upload_visible_without_ai_outputs(client) -> None:
    uploaded = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "pending.pdf",
                b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<<>>\n%%EOF",
                "application/pdf",
            )
        },
    )
    # This deliberately minimal PDF can fail native-text extraction, but must
    # still stay visible in the persistent library instead of disappearing.
    assert uploaded.status_code == 200, uploaded.text

    response = client.get("/v1/resume-library")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["display_name"] is None
    assert item["original_filename"] == "pending.pdf"
    assert item["is_active"] is False
    assert item["summary_preview"] is None
    assert item["score_total"] is None


def test_resume_library_exposes_source_backed_candidate_profile_fields(ai_client) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "教育经历 清华大学 计算机 本科。工作经历 "
            "Acme Python Engineer。技能 Python SQL"
        ),
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        assert len(resume.educations) == 1
        resume.educations[0].end_month = "2026-06"
        resume.employment_months = 30
        session.commit()

    response = ai_client.get("/v1/resume-library")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["graduation_month"] == "2026-06"
    assert item["employment_months"] == 30
    assert item["education_school"] == "清华大学"
    assert item["highest_degree"] == "bachelor"


def test_resume_library_exposes_source_quality_flags_for_an_active_version(
    ai_client,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "教育经历 清华大学 计算机 本科。工作经历 "
            "Acme Python Engineer。技能 Python SQL"
        ),
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        resume.quality_flags = ["source_text_unreliable"]
        session.commit()

    response = ai_client.get("/v1/resume-library")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["is_active"] is True
    assert item["quality_flags"] == ["source_text_unreliable"]


def test_legacy_completed_ai_extraction_is_automatically_activated(ai_client) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text="教育经历 清华大学 计算机 本科。工作经历 Acme Python Engineer。技能 Python SQL",
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        job = session.scalar(
            select(ResumeAiExtractionJob).where(ResumeAiExtractionJob.resume_id == resume_id)
        )
        assert job is not None
        resume.extraction_status = "needs_review"
        resume.is_active = False
        job.status = "completed"
        session.commit()

    with database.session_factory() as session:
        assert reconcile_legacy_completed_ai_resumes(session) == 1
        session.commit()

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "ready"
    assert detail.json()["is_active"] is True
