from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    CandidateNameExtractionJob,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    ResumeSourceBlock,
)
from app.schemas import ResumeFactsSubmission
from app.services import ai_extraction_job_service as job_service
from app.services import document_extraction_job_service
from app.services.deepseek_provider import DeepSeekProviderError
from app.services.institution_service import load_registry
from test_resume_flow import make_pdf_with_text


def _upload_new_resume(
    client,
    *,
    filename: str = "resume.pdf",
    process_document: bool = True,
) -> dict[str, object]:
    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                filename,
                make_pdf_with_text("Education Skills Python " * 20),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200, response.text
    if process_document:
        assert document_extraction_job_service.run_document_extraction_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id="ai-job-document-worker",
        )
    return response.json()


def test_both_upload_paths_enqueue_jobs_without_inline_model_calls(
    ai_client,
    monkeypatch,
) -> None:
    calls: list[object] = []

    def should_not_run(**kwargs: object) -> ResumeFactsSubmission:
        calls.append(kwargs)
        raise AssertionError("the upload HTTP request must not call the model")

    monkeypatch.setattr(job_service, "extract_resume_facts", should_not_run)

    combined = _upload_new_resume(
        ai_client,
        filename="combined.pdf",
        process_document=False,
    )
    candidate = ai_client.post(
        "/v1/candidates", json={"display_name": "Candidate path"}
    )
    assert candidate.status_code == 200, candidate.text
    candidate_upload = ai_client.post(
        f"/v1/candidates/{candidate.json()['candidate_id']}/resumes",
        files={
            "file": (
                "candidate.pdf",
                make_pdf_with_text("Python SQL FastAPI " * 20),
                "application/pdf",
            )
        },
    )
    assert candidate_upload.status_code == 200, candidate_upload.text

    assert combined["ai_extraction_status"] == "queued"
    assert candidate_upload.json()["ai_extraction_status"] == "queued"
    assert calls == []

    database = ai_client.app.state.database
    with database.session_factory() as session:
        jobs = session.scalars(select(ResumeDocumentExtractionJob)).all()
        assert len(jobs) == 2
        assert {job.status for job in jobs} == {"queued"}
        assert session.scalars(select(ResumeAiExtractionJob)).all() == []


def test_worker_auto_activates_grounded_ai_facts_for_search(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    school = load_registry().institutions[0].canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = f"Name: AI Candidate Education {school} Computer Science Skills Python"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        blocks = kwargs["blocks"]
        assert [block.block_id for block in blocks] == ["page-001"]
        return ResumeFactsSubmission.model_validate(
            {
                "candidate_name_raw": "AI Candidate",
                "candidate_name_evidence_block_ids": ["page-001"],
                "education": [
                    {
                        "school_name_raw": school,
                        "degree": "bachelor",
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["ai_extraction_error"] is None
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert payload["is_985_211"] is True
    assert payload["candidate_display_name"] == "AI Candidate"

    # Source-grounded AI facts are immediately eligible for screening.
    search = ai_client.post("/v1/candidates/search", json={"limit": 10})
    assert search.status_code == 200, search.text
    assert len(search.json()["items"]) == 1
    assert search.json()["items"][0]["resume_id"] == resume_id
    assert search.json()["items"][0]["display_name"] == "AI Candidate"

    library = ai_client.get("/v1/resume-library")
    assert library.status_code == 200, library.text
    assert library.json()["items"][0]["display_name"] == "AI Candidate"


def test_worker_drops_ungrounded_candidate_name_but_keeps_grounded_facts(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    assert uploaded["candidate_display_name"] is None
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = "Skills Python"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "candidate_name_raw": "Invented Candidate",
                "candidate_name_evidence_block_ids": ["page-001"],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["extraction_status"] == "ready"
    assert payload["candidate_display_name"] is None
    assert "ai_draft_partial_source_grounding" in payload["quality_flags"]


def test_worker_never_overwrites_an_existing_candidate_name(
    ai_client,
    monkeypatch,
) -> None:
    created = ai_client.post(
        "/v1/candidates",
        json={"display_name": "Existing candidate name"},
    )
    assert created.status_code == 200, created.text
    uploaded = ai_client.post(
        f"/v1/candidates/{created.json()['candidate_id']}/resumes",
        files={
            "file": (
                "resume.pdf",
                make_pdf_with_text("Name: AI Extracted Name Skills Python"),
                "application/pdf",
            )
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["candidate_display_name"] == "Existing candidate name"
    resume_id = str(uploaded.json()["resume_id"])
    database = ai_client.app.state.database
    assert document_extraction_job_service.run_document_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="ai-job-document-worker",
    )

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "candidate_name_raw": "AI Extracted Name",
                "candidate_name_evidence_block_ids": ["page-001"],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["candidate_display_name"] == "Existing candidate name"


def test_worker_keeps_named_experiences_roles_and_source_cited_detail_items(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    school = load_registry().institutions[0].canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = (
            f"Education {school} Computer Science. Internship Experience Acme "
            "Backend Intern Backend Internship Implemented candidate import API. "
            "Project Experience University Lab Technical Lead Resume Intelligence Platform "
            "Designed source-cited extraction workflow Reduced review time. "
            "Competition Competition Committee Team Lead National Data Challenge "
            "Built ranking model Presented the final solution. Skills Python SQL."
        )
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
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
                        "experience_type": "internship",
                        "experience_name_raw": "Backend Internship",
                        "organization_name_raw": "Acme",
                        "title_raw": "Backend Intern",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": ["page-001"],
                        "detail_items": [
                            {
                                "detail_raw": "Implemented candidate import API",
                                "evidence_block_ids": ["page-001"],
                            }
                        ],
                    },
                    {
                        "experience_type": "project",
                        "experience_name_raw": "Resume Intelligence Platform",
                        "organization_name_raw": "University Lab",
                        "title_raw": "Technical Lead",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": [],
                        "detail_items": [
                            {
                                "detail_raw": "Designed source-cited extraction workflow",
                                "evidence_block_ids": ["page-001"],
                            },
                            {
                                "detail_raw": "Reduced review time",
                                "evidence_block_ids": ["page-001"],
                            },
                        ],
                    },
                    {
                        "experience_type": "competition",
                        "experience_name_raw": "National Data Challenge",
                        "organization_name_raw": "Competition Committee",
                        "title_raw": "Team Lead",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": [],
                        "detail_items": [
                            {
                                "detail_raw": "Built ranking model",
                                "evidence_block_ids": ["page-001"],
                            },
                            {
                                "detail_raw": "Presented the final solution",
                                "evidence_block_ids": ["page-001"],
                            },
                        ],
                    },
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    review = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert review.status_code == 200, review.text
    payload = review.json()
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert [item["experience_name_raw"] for item in payload["experiences"]] == [
        "Backend Internship",
        "Resume Intelligence Platform",
        "National Data Challenge",
    ]
    assert payload["experiences"][1]["title_raw"] == "Technical Lead"
    assert payload["experiences"][1]["detail_items"] == [
        {
            "detail_raw": "Designed source-cited extraction workflow",
            "evidence_block_ids": ["page-001"],
        },
        {
            "detail_raw": "Reduced review time",
            "evidence_block_ids": ["page-001"],
        },
    ]
    assert payload["experiences"][2]["detail_items"][1]["detail_raw"] == (
        "Presented the final solution"
    )


def test_worker_drops_only_ungrounded_experience_detail_item(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = (
            "Project Experience Example Lab Developer Data Platform Project "
            "Built ingestion pipeline Skills Python."
        )
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "experiences": [
                    {
                        "experience_type": "project",
                        "experience_name_raw": "Data Platform Project",
                        "organization_name_raw": "Example Lab",
                        "title_raw": "Developer",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": [],
                        "detail_items": [
                            {
                                "detail_raw": "Built ingestion pipeline",
                                "evidence_block_ids": ["page-001"],
                            },
                            {
                                "detail_raw": "Eliminated all operational incidents",
                                "evidence_block_ids": ["page-001"],
                            },
                        ],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    review = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert review.status_code == 200, review.text
    payload = review.json()
    assert payload["experiences"][0]["experience_name_raw"] == "Data Platform Project"
    assert payload["experiences"][0]["detail_items"] == [
        {
            "detail_raw": "Built ingestion pipeline",
            "evidence_block_ids": ["page-001"],
        }
    ]
    assert "ai_draft_partial_source_grounding" in payload["quality_flags"]


def test_worker_accepts_a_source_matched_positive_ai_rulebook_reference(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    roster_entry = load_registry().institutions[0]
    school_raw = roster_entry.canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = f"Education {school_raw} Computer Science Skills Python"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "education": [
                    {
                        "school_name_raw": school_raw,
                        "degree": "bachelor",
                        "ai_985_211_judgment": True,
                        "ai_institution_roster_id": roster_entry.roster_id,
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert payload["is_985_211"] is True
    assert payload["education"][0]["school_match_state"] == "exact"
    assert "ai_985_211_invalid_rulebook_reference" not in payload["quality_flags"]


def test_worker_maps_ai_nonmember_or_invalid_reference_to_false(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    wrong_roster_id = load_registry().institutions[0].roster_id
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = "Education Example University Computer Science Skills Python"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "education": [
                    {
                        "school_name_raw": "Example University",
                        "degree": "bachelor",
                        "ai_985_211_judgment": True,
                        # A real roster ID is still invalid when it does not
                        # exactly match the grounded raw school name.
                        "ai_institution_roster_id": wrong_roster_id,
                        "major_raw": "Computer Science",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["is_985_211"] is False
    assert payload["education"][0]["school_match_state"] == "ai_non_member"
    assert "ai_985_211_invalid_rulebook_reference" in payload["quality_flags"]


def test_worker_maps_missing_ai_school_evidence_to_binary_false(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = "Skills Python SQL"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "education": [],
                "experiences": [],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["ai_extraction_status"] == "completed"
    assert detail.json()["is_985_211"] is False
    assert "school_unresolved" not in detail.json()["quality_flags"]


def test_worker_auto_activates_with_ambiguous_ai_work_item_stored_as_unknown(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    school = load_registry().institutions[0].canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = (
            f"Education {school} Computer Science. Project Experience "
            "Acme Python Engineer 2022-07 to 2024-06. Skills Python SQL."
        )
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
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
                        "start_month": "2022-07",
                        "end_month": "2024-06",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert payload["experiences"][0]["experience_type"] == "unknown"
    assert payload["employment_months"] == 0
    assert "work_context_ambiguous" in payload["quality_flags"]


@pytest.mark.parametrize("section_heading", ["Work History", "工作履历"])
def test_worker_counts_grounded_explicit_work_history_as_employment(
    ai_client,
    monkeypatch,
    section_heading: str,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    school = load_registry().institutions[0].canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = (
            f"Education {school} Computer Science. {section_heading} "
            "Acme Python Engineer 2022-07 to 2024-06. Skills Python SQL."
        )
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
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
                        "start_month": "2022-07",
                        "end_month": "2024-06",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["experiences"][0]["experience_type"] == "employment"
    assert payload["employment_months"] == 24
    assert payload["employment_or_internship_months"] == 24
    assert "work_context_ambiguous" not in payload["quality_flags"]


def test_worker_persists_grounded_ai_facts_when_one_item_is_not_grounded(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    school = load_registry().institutions[0].canonical_name
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = (
            f"Education {school} Computer Science. Professional Experience "
            "Acme Engineer 2022-07 to 2024-06. Skills Python."
        )
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
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
                        "title_raw": "Invented title",
                        "start_month": "2022-07",
                        "end_month": "2024-06",
                        "is_current": False,
                        "evidence_block_ids": ["page-001"],
                        "classification_evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "completed"
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert len(payload["education"]) == 1
    assert payload["experiences"] == []
    assert len(payload["skills"]) == 1
    assert "ai_draft_partial_source_grounding" in payload["quality_flags"]


def test_worker_does_not_activate_resume_when_no_ai_fact_is_grounded(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = "Skills Python"
        session.commit()

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "skills": [
                    {"skill_display": "Invented Skill", "evidence_block_ids": ["page-001"]}
                ]
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    detail = ai_client.get(f"/v1/resumes/{resume_id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["ai_extraction_status"] == "needs_attention"
    assert payload["ai_extraction_error"] == "ai_extraction_no_grounded_facts"
    assert payload["is_active"] is False

    search = ai_client.post("/v1/candidates/search", json={"limit": 10})
    assert search.status_code == 200, search.text
    assert search.json()["items"] == []


def test_terminal_ai_failure_can_be_safely_requeued(ai_client, monkeypatch) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])

    def invalid_model_result(**kwargs: object) -> ResumeFactsSubmission:
        raise DeepSeekProviderError("deepseek_http_400")

    monkeypatch.setattr(job_service, "extract_resume_facts", invalid_model_result)
    database = ai_client.app.state.database
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    failed = ai_client.get(f"/v1/resumes/{resume_id}")
    assert failed.status_code == 200, failed.text
    assert failed.json()["ai_extraction_status"] == "needs_attention"
    assert failed.json()["ai_extraction_error"] == "deepseek_http_400"

    queued = ai_client.post(f"/v1/resumes/{resume_id}/queue-ai-extraction")
    assert queued.status_code == 200, queued.text
    assert queued.json()["ai_extraction_status"] == "queued"
    assert queued.json()["ai_extraction_error"] is None

    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        assert job.attempt_count == 0
        assert job.next_attempt_at is not None


def test_structured_provider_failure_retries_with_core_facts_fallback(
    ai_client,
    monkeypatch,
) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    calls: list[dict[str, object]] = []

    def invalid_model_result(**kwargs: object) -> ResumeFactsSubmission:
        calls.append(kwargs)
        raise DeepSeekProviderError("deepseek_invalid_structured_response")

    monkeypatch.setattr(job_service, "extract_resume_facts", invalid_model_result)
    database = ai_client.app.state.database
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert job.last_error == "deepseek_invalid_structured_response"
        job.next_attempt_at = job_service.utcnow() - timedelta(seconds=1)
        session.commit()

    def successful_core_result(**kwargs: object) -> ResumeFactsSubmission:
        calls.append(kwargs)
        return ResumeFactsSubmission.model_validate(
            {
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ]
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_core_facts", successful_core_result)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    completed = ai_client.get(f"/v1/resumes/{resume_id}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["ai_extraction_status"] == "completed"
    assert "retry_reason" not in calls[0]
    assert "retry_reason" not in calls[1]
    assert "ai_draft_details_pending" in completed.json()["quality_flags"]


def test_core_fallback_saves_source_grounded_name_in_primary_extraction(
    ai_client,
    monkeypatch,
) -> None:
    """Core facts write an explicit source-backed name without a second AI call."""

    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = "Source Candidate\\nSkills: Python"
        session.commit()

    monkeypatch.setattr(
        job_service,
        "extract_resume_facts",
        lambda **_kwargs: (_ for _ in ()).throw(
            DeepSeekProviderError("deepseek_invalid_structured_response")
        ),
    )
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="name-fallback-facts-worker",
    )
    with database.session_factory() as session:
        extraction_job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert extraction_job is not None
        extraction_job.next_attempt_at = job_service.utcnow() - timedelta(seconds=1)
        session.commit()

    monkeypatch.setattr(
        job_service,
        "extract_resume_core_facts",
        lambda **_kwargs: ResumeFactsSubmission.model_validate(
            {
                "candidate_name_raw": "Source Candidate",
                "candidate_name_evidence_block_ids": ["page-001"],
                "skills": [
                    {"skill_display": "Python", "evidence_block_ids": ["page-001"]}
                ]
            }
        ),
    )
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="name-fallback-facts-worker",
    )

    after_core = ai_client.get(f"/v1/resumes/{resume_id}")
    assert after_core.status_code == 200, after_core.text
    assert after_core.json()["ai_extraction_status"] == "completed"
    assert after_core.json()["candidate_display_name"] == "Source Candidate"
    assert after_core.json()["candidate_name_extraction_status"] == "succeeded"
    with database.session_factory() as session:
        name_job = session.scalar(
            select(CandidateNameExtractionJob).where(
                CandidateNameExtractionJob.resume_id == resume_id
            )
        )
        assert name_job is None


def test_structured_provider_failure_stops_at_the_attempt_budget(ai_client, monkeypatch) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        job.max_attempts = 1
        session.commit()

    def invalid_model_result(**kwargs: object) -> ResumeFactsSubmission:
        raise DeepSeekProviderError("deepseek_empty_structured_facts")

    monkeypatch.setattr(job_service, "extract_resume_facts", invalid_model_result)
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    failed = ai_client.get(f"/v1/resumes/{resume_id}")
    assert failed.status_code == 200, failed.text
    assert failed.json()["ai_extraction_status"] == "needs_attention"
    assert failed.json()["ai_extraction_error"] == "deepseek_empty_structured_facts"


def test_retryable_provider_failure_is_requeued_with_backoff(ai_client, monkeypatch) -> None:
    _upload_new_resume(ai_client)

    def timeout(**kwargs: object) -> ResumeFactsSubmission:
        raise DeepSeekProviderError("deepseek_timeout")

    monkeypatch.setattr(job_service, "extract_resume_facts", timeout)
    database = ai_client.app.state.database
    assert job_service.run_ai_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="test-worker",
    )

    with database.session_factory() as session:
        job = session.scalar(select(ResumeAiExtractionJob))
        assert job is not None
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert job.last_error == "deepseek_timeout"
        assert job.next_attempt_at is not None
        assert job.next_attempt_at > job.updated_at - timedelta(seconds=1)


def test_worker_claim_persists_route_pin_for_legacy_null_job(ai_client) -> None:
    uploaded = _upload_new_resume(ai_client)
    resume_id = str(uploaded["resume_id"])
    database = ai_client.app.state.database
    settings = ai_client.app.state.settings
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        expected_route_id = job.ai_route_policy_version_id
        assert expected_route_id is not None
        job.ai_route_policy_version_id = None
        session.commit()

    claimed = job_service._claim_next_job(
        database,
        settings=settings,
        worker_id="legacy-null-pin-test-worker",
    )
    assert claimed is not None
    assert claimed.ai_route_policy_version_id == expected_route_id
    with database.session_factory() as session:
        job = session.scalar(
            select(ResumeAiExtractionJob).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        )
        assert job is not None
        assert job.ai_route_policy_version_id == expected_route_id
        assert job.status == "running"
