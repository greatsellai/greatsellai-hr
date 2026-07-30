from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from sqlalchemy import select

from app.database import Database
from app.config import AppSettings
from app.models import (
    Candidate,
    Organization,
    Resume,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    WorkspaceBackgroundLane,
)
from app.services.workspace_background_lane_service import (
    acquire_workspace_background_lane,
    fair_available_workspace_ids,
    maintain_claimed_workspace_job_lease,
    release_workspace_background_lane,
    renew_workspace_background_lane,
)
from app.services import document_extraction_job_service
from app.services import workspace_background_lane_service
from app.tenant_scope import bypass_organization_scope, set_organization_context


def _database() -> Database:
    database = Database("sqlite://")
    database.create_all()
    return database


def _queued_job(session, *, organization_id: str, position: int) -> None:
    candidate = Candidate(
        organization_id=organization_id,
        display_name=f"Lane candidate {organization_id}-{position}",
    )
    session.add(candidate)
    session.flush()
    resume = Resume(
        organization_id=organization_id,
        candidate_id=candidate.id,
        original_filename=f"resume-{organization_id}-{position}.pdf",
        storage_key=f"resume-{organization_id}-{position}.pdf",
        sha256=(f"{position:x}" * 64)[:64],
        source_page_count=1,
        parsed_page_count=1,
        extraction_status="text_ready",
        quality_flags=[],
        parser_version="lane-test",
        facts_version=0,
    )
    session.add(resume)
    session.flush()
    session.add(
        ResumeAiExtractionJob(
            organization_id=organization_id,
            resume_id=resume.id,
            status="queued",
            attempt_count=0,
            max_attempts=3,
            input_facts_version=0,
            next_attempt_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=position),
        )
    )


def _eligible(now: datetime):
    from sqlalchemy import and_, or_

    return and_(
        ResumeAiExtractionJob.status == "queued",
        ResumeAiExtractionJob.attempt_count < ResumeAiExtractionJob.max_attempts,
        or_(
            ResumeAiExtractionJob.next_attempt_at.is_(None),
            ResumeAiExtractionJob.next_attempt_at <= now,
        ),
    )


def test_fair_lanes_give_another_workspace_the_next_slot() -> None:
    database = _database()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with database.session_factory() as session:
        first = Organization(name="A")
        second = Organization(name="B")
        session.add_all((first, second))
        session.flush()
        with bypass_organization_scope(session):
            for position in range(5):
                _queued_job(session, organization_id=first.id, position=position)
            _queued_job(session, organization_id=second.id, position=10)
            session.flush()
        session.commit()

    with database.session_factory() as session:
        available = fair_available_workspace_ids(
            session,
            source=ResumeAiExtractionJob,
            organization_id_column=ResumeAiExtractionJob.organization_id,
            eligible=_eligible(now),
            next_attempt_at_column=ResumeAiExtractionJob.next_attempt_at,
            requested_at_column=ResumeAiExtractionJob.requested_at,
            now=now,
        )
        assert available[:2] == [first.id, second.id]
        first_lane = acquire_workspace_background_lane(
            session,
            organization_id=first.id,
            worker_id="worker-a",
            job_kind="ai_extraction",
            job_id="first-job",
            lease_seconds=180,
            now=now,
        )
        assert first_lane is not None
        session.commit()

    with database.session_factory() as session:
        available = fair_available_workspace_ids(
            session,
            source=ResumeAiExtractionJob,
            organization_id_column=ResumeAiExtractionJob.organization_id,
            eligible=_eligible(now),
            next_attempt_at_column=ResumeAiExtractionJob.next_attempt_at,
            requested_at_column=ResumeAiExtractionJob.requested_at,
            now=now,
        )
        assert available == [second.id]
        second_lane = acquire_workspace_background_lane(
            session,
            organization_id=second.id,
            worker_id="worker-b",
            job_kind="ai_extraction",
            job_id="second-job",
            lease_seconds=180,
            now=now,
        )
        assert second_lane is not None
        session.commit()

    with database.session_factory() as session:
        assert not release_workspace_background_lane(
            session,
            organization_id=first.id,
            lease_token="not-the-owner",
            now=now,
        )
        assert renew_workspace_background_lane(
            session,
            organization_id=first.id,
            lease_token=first_lane.lease_token,
            lease_seconds=180,
            now=now + timedelta(seconds=10),
        )
        assert release_workspace_background_lane(
            session,
            organization_id=first.id,
            lease_token=first_lane.lease_token,
            now=now + timedelta(seconds=20),
        )
        session.commit()

    with database.session_factory() as session:
        lane = session.scalar(
            select(WorkspaceBackgroundLane).where(
                WorkspaceBackgroundLane.organization_id == first.id
            )
        )
        assert lane is not None
        assert lane.lease_token is None
        assert lane.last_claimed_at == now.replace(tzinfo=None)
    database.dispose()


def test_expired_workspace_lane_becomes_claimable_again() -> None:
    database = _database()
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    with database.session_factory() as session:
        organization = Organization(name="Expired lane")
        session.add(organization)
        session.flush()
        first = acquire_workspace_background_lane(
            session,
            organization_id=organization.id,
            worker_id="old-worker",
            job_kind="document_extraction",
            job_id="old-job",
            lease_seconds=5,
            now=now,
        )
        assert first is not None
        session.commit()

    with database.session_factory() as session:
        second = acquire_workspace_background_lane(
            session,
            organization_id=organization.id,
            worker_id="new-worker",
            job_kind="document_extraction",
            job_id="new-job",
            lease_seconds=5,
            now=now + timedelta(seconds=6),
        )
        assert second is not None
        assert second.lease_token != first.lease_token
        session.commit()
    database.dispose()


def test_document_workers_do_not_claim_two_heavy_jobs_from_one_workspace(
    tmp_path,
) -> None:
    """The real document queue skips A's second job and claims B's first."""

    database = _database()
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        document_extraction_job_lease_seconds=180,
        worker_workspace_lane_lease_seconds=210,
    )
    now = datetime.now(timezone.utc)
    with database.session_factory() as session:
        first = Organization(name="Document A")
        second = Organization(name="Document B")
        session.add_all((first, second))
        session.flush()
        with bypass_organization_scope(session):
            for position, organization_id in enumerate((first.id, first.id, second.id)):
                candidate = Candidate(
                    organization_id=organization_id,
                    display_name=f"Document candidate {position}",
                )
                session.add(candidate)
                session.flush()
                resume = Resume(
                    organization_id=organization_id,
                    candidate_id=candidate.id,
                    original_filename=f"document-{position}.pdf",
                    storage_key=f"document-{position}.pdf",
                    sha256=(f"{position:x}" * 64)[:64],
                    source_page_count=1,
                    parsed_page_count=0,
                    extraction_status="queued",
                    quality_flags=[],
                    parser_version="lane-test",
                    facts_version=0,
                )
                session.add(resume)
                session.flush()
                session.add(
                    ResumeDocumentExtractionJob(
                        organization_id=organization_id,
                        resume_id=resume.id,
                        status="queued",
                        attempt_count=0,
                        max_attempts=3,
                        next_attempt_at=now,
                        requested_at=now + timedelta(seconds=position),
                    )
                )
            session.flush()
        session.commit()

    first_claim = document_extraction_job_service._claim_next_job(
        database,
        settings=settings,
        worker_id="document-worker-a",
    )
    second_claim = document_extraction_job_service._claim_next_job(
        database,
        settings=settings,
        worker_id="document-worker-b",
    )

    assert first_claim is not None
    assert second_claim is not None
    assert first_claim.organization_id == first.id
    assert second_claim.organization_id == second.id

    with database.session_factory() as session:
        assert release_workspace_background_lane(
            session,
            organization_id=first_claim.organization_id,
            lease_token=first_claim.workspace_lane_token,
        )
        assert release_workspace_background_lane(
            session,
            organization_id=second_claim.organization_id,
            lease_token=second_claim.workspace_lane_token,
        )
        session.commit()
    database.dispose()


def test_document_lease_heartbeat_renews_the_task_and_workspace_lane(
    monkeypatch,
) -> None:
    """A long OCR pass cannot expose its workspace after the old task lease."""

    database = _database()
    now = datetime.now(timezone.utc)
    with database.session_factory() as session:
        organization = Organization(name="Renewed document lane")
        session.add(organization)
        session.flush()
        set_organization_context(session, organization.id)
        candidate = Candidate(
            organization_id=organization.id,
            display_name="Renewed document candidate",
        )
        session.add(candidate)
        session.flush()
        resume = Resume(
            organization_id=organization.id,
            candidate_id=candidate.id,
            original_filename="renewed.pdf",
            storage_key="renewed.pdf",
            sha256="b" * 64,
            source_page_count=1,
            parsed_page_count=0,
            extraction_status="extracting",
            quality_flags=[],
            parser_version="lane-test",
            facts_version=0,
        )
        session.add(resume)
        session.flush()
        job = ResumeDocumentExtractionJob(
            organization_id=organization.id,
            resume_id=resume.id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_owner="worker-a",
            lease_expires_at=now + timedelta(seconds=1),
            requested_at=now,
        )
        session.add(job)
        session.flush()
        lane = acquire_workspace_background_lane(
            session,
            organization_id=organization.id,
            worker_id="worker-a",
            job_kind="document_extraction",
            job_id=job.id,
            lease_seconds=3600,
            now=now,
        )
        assert lane is not None
        session.commit()

    monkeypatch.setattr(
        workspace_background_lane_service,
        "_HEARTBEAT_MIN_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        workspace_background_lane_service,
        "_HEARTBEAT_MAX_INTERVAL_SECONDS",
        0.01,
    )
    with maintain_claimed_workspace_job_lease(
        database,
        job_model=ResumeDocumentExtractionJob,
        job_id=job.id,
        organization_id=organization.id,
        worker_id="worker-a",
        running_status="running",
        job_lease_seconds=180,
        workspace_lane_token=lane.lease_token,
        workspace_lane_lease_seconds=3600,
    ):
        time.sleep(0.08)

    with database.session_factory() as session:
        set_organization_context(session, organization.id)
        stored_job = session.get(ResumeDocumentExtractionJob, job.id)
        stored_lane = session.scalar(
            select(WorkspaceBackgroundLane).where(
                WorkspaceBackgroundLane.organization_id == organization.id
            )
        )
        assert stored_job is not None
        assert stored_lane is not None
        assert stored_job.lease_expires_at is not None
        assert stored_job.lease_expires_at > now.replace(tzinfo=None) + timedelta(
            seconds=30
        )
        assert stored_lane.lease_expires_at is not None
        assert stored_lane.lease_expires_at > now.replace(tzinfo=None) + timedelta(
            seconds=30
        )
    database.dispose()


def test_document_recovery_releases_a_long_workspace_lane_immediately(
    tmp_path,
) -> None:
    """A crashed job must not strand its workspace until a 3600s lane expires."""

    database = _database()
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        document_extraction_job_lease_seconds=180,
        worker_workspace_lane_lease_seconds=3600,
    )
    now = datetime.now(timezone.utc)
    with database.session_factory() as session:
        organization = Organization(name="Recovered document lane")
        session.add(organization)
        session.flush()
        set_organization_context(session, organization.id)
        candidate = Candidate(
            organization_id=organization.id,
            display_name="Recovered document candidate",
        )
        session.add(candidate)
        session.flush()
        resume = Resume(
            organization_id=organization.id,
            candidate_id=candidate.id,
            original_filename="recovered.pdf",
            storage_key="recovered.pdf",
            sha256="c" * 64,
            source_page_count=1,
            parsed_page_count=0,
            extraction_status="extracting",
            quality_flags=[],
            parser_version="lane-test",
            facts_version=0,
        )
        session.add(resume)
        session.flush()
        job = ResumeDocumentExtractionJob(
            organization_id=organization.id,
            resume_id=resume.id,
            status="running",
            attempt_count=1,
            max_attempts=3,
            lease_owner="crashed-worker",
            lease_expires_at=now - timedelta(seconds=1),
            requested_at=now - timedelta(minutes=1),
        )
        session.add(job)
        session.flush()
        lane = acquire_workspace_background_lane(
            session,
            organization_id=organization.id,
            worker_id="crashed-worker",
            job_kind="document_extraction",
            job_id=job.id,
            lease_seconds=3600,
            now=now - timedelta(seconds=2),
        )
        assert lane is not None
        session.commit()

    with database.session_factory() as session:
        document_extraction_job_service._recover_expired_leases(session, now=now)
        session.commit()
        stored_lane = session.scalar(
            select(WorkspaceBackgroundLane).where(
                WorkspaceBackgroundLane.organization_id == organization.id
            )
        )
        assert stored_lane is not None
        assert stored_lane.lease_token is None

    reclaimed = document_extraction_job_service._claim_next_job(
        database,
        settings=settings,
        worker_id="recovery-worker",
    )
    assert reclaimed is not None
    assert reclaimed.job_id == job.id
    with database.session_factory() as session:
        assert release_workspace_background_lane(
            session,
            organization_id=organization.id,
            lease_token=reclaimed.workspace_lane_token,
        )
        session.commit()
    database.dispose()
