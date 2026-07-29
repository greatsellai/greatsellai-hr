from __future__ import annotations

from sqlalchemy import select

from app.models import Candidate, CandidateNameExtractionJob, Resume, ResumeSourceBlock
from app.services import candidate_name_job_service as job_service
from app.services import document_extraction_job_service
from app.services.deepseek_provider import CandidateNameDraft, DeepSeekProviderError
from test_resume_flow import make_pdf_with_text


def _prepare_name_job(ai_client) -> tuple[str, str, int, str]:
    """Create one active source-backed resume with an intentionally blank name."""

    uploaded = ai_client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "candidate-name-fixture.pdf",
                make_pdf_with_text("Header source fixture Python SQL " * 20),
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
        worker_id="candidate-name-document-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        candidate = session.get(Candidate, resume.candidate_id)
        assert candidate is not None
        source_block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume.id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert source_block is not None
        source_block.text = "Name: Source Grounded Fixture\nSkills: Python SQL"
        resume.extraction_status = "ready"
        resume.is_active = True
        resume.quality_flags = []
        candidate.display_name = None
        facts_version_before = resume.facts_version
        source_text_before = source_block.text
        job = job_service.enqueue_candidate_name_extraction_job(
            session,
            resume=resume,
            settings=ai_client.app.state.settings,
        )
        assert job is not None
        assert job.status == job_service.CANDIDATE_NAME_JOB_QUEUED
        job_id = job.id
        session.commit()
    return resume_id, job_id, facts_version_before, source_text_before


def test_name_worker_completes_only_the_empty_display_name_and_keeps_facts(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, facts_version_before, source_text_before = _prepare_name_job(
        ai_client
    )

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        lambda **_kwargs: CandidateNameDraft(
            value="Source Grounded Fixture",
            evidence_block_ids=["page-001"],
        ),
    )

    database = ai_client.app.state.database
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-worker",
    )
    assert not job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name == "Source Grounded Fixture"
        assert job.status == job_service.CANDIDATE_NAME_JOB_SUCCEEDED
        assert job.attempt_count == 1
        assert job.last_error is None
        # This name-only task must not re-run structured fact processing or
        # change a resume's readiness/searchability state.
        assert resume.facts_version == facts_version_before
        assert resume.extraction_status == "ready"
        assert resume.is_active is True
        source_block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert source_block is not None
        assert source_block.text == source_text_before
        assert job_service.candidate_name_extraction_state(resume) == (
            job_service.CANDIDATE_NAME_JOB_SUCCEEDED,
            None,
        )


def test_name_worker_rejects_ungrounded_value_without_touching_facts(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, facts_version_before, source_text_before = _prepare_name_job(
        ai_client
    )

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        lambda **_kwargs: CandidateNameDraft(
            value="Invented Fixture",
            evidence_block_ids=["page-001"],
        ),
    )

    database = ai_client.app.state.database
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-grounding-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name is None
        assert job.status == job_service.CANDIDATE_NAME_JOB_SKIPPED
        assert job.last_error == "candidate_name_not_grounded"
        assert resume.facts_version == facts_version_before
        assert resume.extraction_status == "ready"
        assert resume.is_active is True
        source_block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert source_block is not None
        assert source_block.text == source_text_before


def test_name_worker_never_overwrites_a_name_added_after_queueing(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        candidate = session.get(Candidate, resume.candidate_id)
        assert candidate is not None
        candidate.display_name = "Manual Fixture"
        session.commit()

    def must_not_call_provider(**_kwargs: object) -> CandidateNameDraft:
        raise AssertionError("candidate name was already set before AI execution")

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        must_not_call_provider,
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-manual-protection-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name == "Manual Fixture"
        assert job.status == job_service.CANDIDATE_NAME_JOB_SKIPPED
        assert job.last_error == "candidate_name_already_set"


def test_name_worker_retries_a_transient_provider_failure(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database

    def transient_failure(**_kwargs: object) -> CandidateNameDraft:
        raise DeepSeekProviderError("deepseek_timeout")

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        transient_failure,
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-retry-worker",
    )
    with database.session_factory() as session:
        job = session.get(CandidateNameExtractionJob, job_id)
        assert job is not None
        assert job.status == job_service.CANDIDATE_NAME_JOB_QUEUED
        assert job.attempt_count == 1
        assert job.last_error == "deepseek_timeout"
        # Make the retry immediately eligible without depending on clock
        # resolution differences between SQLite and PostgreSQL.
        job.next_attempt_at = None
        session.commit()

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        lambda **_kwargs: CandidateNameDraft(
            value="Source Grounded Fixture",
            evidence_block_ids=["page-001"],
        ),
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-retry-worker",
    )
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name == "Source Grounded Fixture"
        assert job.status == job_service.CANDIDATE_NAME_JOB_SUCCEEDED
        assert job.attempt_count == 2


def test_name_worker_skips_unreliable_source_text_without_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        resume.quality_flags = ["source_text_unreliable"]
        session.commit()

    def must_not_call_provider(**_kwargs: object) -> CandidateNameDraft:
        raise AssertionError("unreliable source must never reach a provider")

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        must_not_call_provider,
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-unreliable-source-worker",
    )
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name is None
        assert job.status == job_service.CANDIDATE_NAME_JOB_SKIPPED
        assert job.last_error == "resume_source_text_unreliable"


def test_name_worker_supersedes_an_inactive_source_before_provider_call(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        # A newer version can take over the candidate's active slot after this
        # older task was queued. The old source must not even reach a provider.
        resume.is_active = False
        session.commit()

    def must_not_call_provider(**_kwargs: object) -> CandidateNameDraft:
        raise AssertionError("inactive source must never reach a provider")

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        must_not_call_provider,
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-stale-source-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name is None
        assert job.status == job_service.CANDIDATE_NAME_JOB_SUPERSEDED
        assert job.last_error == "candidate_name_resume_not_current"


def test_name_worker_does_not_persist_when_source_becomes_stale_during_ai_call(
    ai_client,
    monkeypatch,
) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database

    def mark_source_stale_then_return(**_kwargs: object) -> CandidateNameDraft:
        with database.session_factory() as session:
            resume = session.get(Resume, resume_id)
            assert resume is not None
            resume.is_active = False
            session.commit()
        return CandidateNameDraft(
            value="Source Grounded Fixture",
            evidence_block_ids=["page-001"],
        )

    monkeypatch.setattr(
        job_service,
        "extract_resume_candidate_name",
        mark_source_stale_then_return,
    )
    assert job_service.run_candidate_name_extraction_worker_once(
        database,
        settings=ai_client.app.state.settings,
        worker_id="candidate-name-stale-persist-worker",
    )

    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        job = session.get(CandidateNameExtractionJob, job_id)
        assert resume is not None
        assert job is not None
        assert resume.candidate.display_name is None
        assert job.status == job_service.CANDIDATE_NAME_JOB_SUPERSEDED
        assert job.last_error == "candidate_name_resume_not_current"


def test_logical_delete_cancels_a_queued_name_task(ai_client) -> None:
    resume_id, job_id, _, _ = _prepare_name_job(ai_client)
    database = ai_client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        candidate_id = resume.candidate_id

    deleted = ai_client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text

    with database.session_factory() as session:
        job = session.get(CandidateNameExtractionJob, job_id)
        assert job is not None
        assert job.status == job_service.CANDIDATE_NAME_JOB_CANCELLED
        assert job.last_error == "candidate_data_deleted"
        assert job.next_attempt_at is None
