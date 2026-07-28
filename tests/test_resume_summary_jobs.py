from __future__ import annotations

from sqlalchemy import select

from app.models import Resume, ResumeSourceBlock, ResumeSummary, ResumeSummaryJob
from app.schemas import ResumeFactsSubmission
from app.services import ai_extraction_job_service, document_extraction_job_service
from app.services import resume_summary_job_service
from app.services.deepseek_provider import DeepSeekProviderError
from app.services.institution_service import load_registry
from test_resume_flow import make_pdf_with_text


def _upload_for_automatic_summary(ai_client) -> tuple[str, str]:
    """Persist source text and prepare the ordinary extraction queue path."""

    uploaded = ai_client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "automatic-summary.pdf",
                make_pdf_with_text("Candidate Education Experience Python " * 20),
                "application/pdf",
            )
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    resume_id = str(uploaded.json()["resume_id"])
    database = ai_client.app.state.database
    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="summary-document-worker",
    )

    school = load_registry().institutions[0].canonical_name
    with database.session_factory() as session:
        source_block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert source_block is not None
        source_block.text = (
            f"Candidate Education {school} Computer Science "
            "Experience Acme Python Engineer Skills Python SQL"
        )
        session.commit()
    return resume_id, school


def _grounded_facts(*, school: str) -> ResumeFactsSubmission:
    return ResumeFactsSubmission.model_validate(
        {
            "candidate_name_raw": "Summary Candidate",
            "candidate_name_evidence_block_ids": ["page-001"],
            "education": [
                {
                    "school_name_raw": school,
                    "degree": "bachelor",
                    "major_raw": "Computer Science",
                    "evidence_block_ids": ["page-001"],
                }
            ],
            "experiences": [
                {
                    "experience_type": "employment",
                    "organization_name_raw": "Acme",
                    "title_raw": "Python Engineer",
                    "evidence_block_ids": ["page-001"],
                    "classification_evidence_block_ids": ["page-001"],
                }
            ],
            "skills": [
                {"skill_display": "Python", "evidence_block_ids": ["page-001"]},
                {"skill_display": "SQL", "evidence_block_ids": ["page-001"]},
            ],
        }
    )


def _summary_output(**kwargs: object) -> dict[str, object]:
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    fact_id = snapshot["skills"][0]["fact_id"]
    return {
        "schema_version": "resume_summary.v1",
        "sections": {
            "candidate_positioning": {
                "content": "候选人具备 Python 后端开发基础。",
                "fact_ids": [fact_id],
            },
            "core_skills": {
                "content": "已确认技能包括 Python 和 SQL。",
                "fact_ids": [fact_id],
            },
        },
    }


def _complete_ai_facts_and_assert_summary_is_queued(ai_client, monkeypatch) -> str:
    resume_id, school = _upload_for_automatic_summary(ai_client)
    monkeypatch.setattr(
        ai_extraction_job_service,
        "extract_resume_facts",
        lambda **_kwargs: _grounded_facts(school=school),
    )
    database = ai_client.app.state.database
    assert ai_extraction_job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="summary-facts-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        assert resume.extraction_status == "ready"
        assert resume.is_active is True
        jobs = session.scalars(
            select(ResumeSummaryJob).where(ResumeSummaryJob.resume_id == resume_id)
        ).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.status == "queued"
        assert job.facts_version == resume.facts_version
        assert job.fact_snapshot_id
        assert job.summary_id is None
        assert session.scalars(
            select(ResumeSummary).where(ResumeSummary.resume_id == resume_id)
        ).all() == []
    return resume_id


def test_ai_fact_activation_queues_and_worker_persists_an_automatic_summary(
    ai_client,
    monkeypatch,
) -> None:
    """Summary work is durable and never runs inline with extraction/upload."""

    resume_id = _complete_ai_facts_and_assert_summary_is_queued(ai_client, monkeypatch)
    monkeypatch.setattr(
        "app.services.summary_service.summarize_resume_fact_snapshot",
        _summary_output,
    )

    database = ai_client.app.state.database
    assert resume_summary_job_service.run_resume_summary_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="summary-worker",
    )
    assert not resume_summary_job_service.run_resume_summary_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="summary-worker",
    )

    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeSummaryJob).where(ResumeSummaryJob.resume_id == resume_id)
        )
        assert job is not None
        assert job.status == "succeeded"
        assert job.attempt_count == 1
        assert job.summary_id
        assert job.completed_at is not None
        summary = session.get(ResumeSummary, job.summary_id)
        assert summary is not None
        assert summary.is_current is True
        assert summary.source == "ai"
        assert summary.status == "succeeded"
        assert summary.facts_version == job.facts_version

    library = ai_client.get("/v1/resume-library")
    assert library.status_code == 200, library.text
    item = next(item for item in library.json()["items"] if item["resume_id"] == resume_id)
    assert item["ai_summary_status"] == "succeeded"
    assert item["ai_summary_error"] is None
    assert item["summary_preview"] == "候选人具备 Python 后端开发基础。"


def test_summary_failure_keeps_the_resume_ready_and_searchable(
    ai_client,
    monkeypatch,
) -> None:
    """A separate summary failure must never retract validated resume facts."""

    resume_id = _complete_ai_facts_and_assert_summary_is_queued(ai_client, monkeypatch)

    def reject_summary(**_kwargs: object) -> dict[str, object]:
        raise DeepSeekProviderError("ai_provider_auth")

    monkeypatch.setattr(
        "app.services.summary_service.summarize_resume_fact_snapshot",
        reject_summary,
    )
    database = ai_client.app.state.database
    assert resume_summary_job_service.run_resume_summary_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="summary-failure-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.scalar(
            select(ResumeSummaryJob).where(ResumeSummaryJob.resume_id == resume_id)
        )
        assert resume is not None
        assert job is not None
        assert resume.extraction_status == "ready"
        assert resume.is_active is True
        assert job.status == "failed"
        assert job.summary_id is None
        assert job.last_error == "ai_provider_auth"

    search = ai_client.post("/v1/candidates/search", json={"skills_all_of": ["Python"]})
    assert search.status_code == 200, search.text
    assert {item["resume_id"] for item in search.json()["items"]} == {resume_id}

    library = ai_client.get("/v1/resume-library")
    assert library.status_code == 200, library.text
    item = next(item for item in library.json()["items"] if item["resume_id"] == resume_id)
    assert item["ai_summary_status"] == "failed"
    assert item["ai_summary_error"] == "ai_provider_auth"
    assert item["summary_preview"] is None


def test_automatic_summary_never_replaces_a_current_manual_version(
    ai_client,
    monkeypatch,
) -> None:
    """A delayed worker must preserve recruiter-authored summary content."""

    resume_id = _complete_ai_facts_and_assert_summary_is_queued(ai_client, monkeypatch)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeSummaryJob).where(ResumeSummaryJob.resume_id == resume_id)
        )
        assert job is not None
        manual = ResumeSummary(
            organization_id=job.organization_id,
            resume_id=job.resume_id,
            fact_snapshot_id=job.fact_snapshot_id,
            facts_version=job.facts_version,
            content={
                "schema_version": "resume_summary.manual.v1",
                "sections": {"candidate_positioning": "招聘人员已确认的总结。"},
            },
            source="manual",
            is_current=True,
            status="succeeded",
            model_name=None,
        )
        session.add(manual)
        session.commit()
        manual_id = manual.id

    def _must_not_generate(**_kwargs: object) -> object:
        raise AssertionError("automatic worker replaced a manual summary")

    monkeypatch.setattr(
        resume_summary_job_service,
        "generate_resume_summary",
        _must_not_generate,
    )
    assert resume_summary_job_service.run_resume_summary_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="manual-summary-protection-worker",
    )

    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeSummaryJob).where(ResumeSummaryJob.resume_id == resume_id)
        )
        assert job is not None
        assert job.status == "succeeded"
        assert job.summary_id == manual_id
        summaries = session.scalars(
            select(ResumeSummary).where(ResumeSummary.resume_id == resume_id)
        ).all()
        assert len(summaries) == 1
        assert summaries[0].id == manual_id
        assert summaries[0].is_current is True
