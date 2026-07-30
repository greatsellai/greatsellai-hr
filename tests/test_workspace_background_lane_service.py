from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import Database
from app.models import (
    Candidate,
    Organization,
    Resume,
    ResumeAiExtractionJob,
    WorkspaceBackgroundLane,
)
from app.services.workspace_background_lane_service import (
    acquire_workspace_background_lane,
    fair_available_workspace_ids,
    release_workspace_background_lane,
    renew_workspace_background_lane,
)
from app.tenant_scope import bypass_organization_scope


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
