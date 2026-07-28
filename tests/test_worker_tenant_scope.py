from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import (
    Candidate,
    EmailAttachmentImport,
    Job,
    JobMatchBatch,
    JobMatchBatchItem,
    JobVersion,
    MailboxConfig,
    Organization,
    Resume,
    ResumeAiExtractionJob,
    ResumeFactSnapshot,
    ResumeSourceBlock,
    ResumeSummaryJob,
)
from app.services import (
    ai_extraction_job_service,
    job_match_batch_service,
    mailbox_background_job_service,
    mailbox_import_service,
    resume_summary_job_service,
)
from app.services.ai_extraction_job_service import (
    AI_EXTRACTION_NEEDS_ATTENTION,
    AI_EXTRACTION_QUEUED,
    run_ai_extraction_worker_once,
    utcnow as extraction_utcnow,
)
from app.services.job_match_batch_service import (
    BATCH_QUEUED,
    ITEM_FAILED,
    ITEM_QUEUED,
    run_job_match_batch_worker_once,
)
from app.services.mailbox_import_service import MailboxSyncResponse, sync_due_mailboxes
from app.services.resume_summary_job_service import run_resume_summary_worker_once
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        deepseek_api_key="worker-scope-test-key",
        deepseek_timeout_seconds=1,
        ai_extraction_job_lease_seconds=60,
        mailbox_sync_interval_seconds=60,
        min_text_chars_per_page=20,
    )


def _database(tmp_path: Path) -> Database:
    database = Database("sqlite://")
    database.create_all()
    return database


@contextmanager
def _workspace_session(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _organizations(database: Database) -> tuple[str, str]:
    with database.session_factory() as session:
        organization_a = Organization(name="Worker scope A")
        organization_b = Organization(name="Worker scope B")
        session.add_all((organization_a, organization_b))
        session.commit()
        return organization_a.id, organization_b.id


def _resume(
    session: Session,
    *,
    organization_id: str,
    label: str,
    active: bool = False,
) -> Resume:
    candidate = Candidate(display_name=f"Candidate {label}")
    session.add(candidate)
    session.flush()
    resume = Resume(
        candidate_id=candidate.id,
        original_filename=f"{label}.pdf",
        storage_key=f"{label}.pdf",
        sha256=(label * 64)[:64],
        source_page_count=1,
        parsed_page_count=1,
        extraction_status="ready" if active else "text_ready",
        quality_flags=[],
        parser_version="tenant-worker-test",
        is_active=active,
        facts_version=0,
    )
    session.add(resume)
    session.flush()
    return resume


def _snapshot(session: Session, *, resume: Resume) -> ResumeFactSnapshot:
    snapshot = ResumeFactSnapshot(
        resume_id=resume.id,
        facts_version=resume.facts_version,
        canonical_facts_json="{}",
        facts_sha256="a" * 64,
        source_block_ids=[],
        created_by="worker-test",
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def test_ai_worker_marks_cross_workspace_job_safe_without_reading_foreign_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A global queue claim cannot cause one tenant's job to read another's resume."""

    database = _database(tmp_path)
    settings = _settings(tmp_path)
    organization_a, organization_b = _organizations(database)
    now = extraction_utcnow()

    with database.session_factory() as session:
        with _workspace_session(session, organization_b):
            foreign_resume = _resume(session, organization_id=organization_b, label="foreign")
            session.add(
                ResumeSourceBlock(
                    resume_id=foreign_resume.id,
                    block_id="page-001",
                    page_no=1,
                    block_type="text",
                    text="This text must never be passed across workspaces.",
                )
            )
            untouched_resume = _resume(session, organization_id=organization_b, label="untouched")
            session.add(
                ResumeAiExtractionJob(
                    resume_id=untouched_resume.id,
                    job_kind="initial",
                    status=AI_EXTRACTION_QUEUED,
                    max_attempts=1,
                    input_facts_version=0,
                    next_attempt_at=now,
                    requested_at=now,
                )
            )
            session.flush()
        with _workspace_session(session, organization_a):
            # This deliberately malformed foreign reference is possible at the
            # database level without a composite tenant FK.  The worker must
            # fail its own A job before it ever reads B's source blocks.
            cross_workspace_job = ResumeAiExtractionJob(
                resume_id=foreign_resume.id,
                job_kind="initial",
                status=AI_EXTRACTION_QUEUED,
                max_attempts=1,
                input_facts_version=0,
                next_attempt_at=now - timedelta(seconds=10),
                requested_at=now - timedelta(seconds=10),
            )
            session.add(cross_workspace_job)
            session.commit()
            cross_job_id = cross_workspace_job.id

    provider_called = False

    def _provider_must_not_run(**_: object) -> object:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("foreign resume text reached the AI provider")

    monkeypatch.setattr(ai_extraction_job_service, "extract_resume_facts", _provider_must_not_run)

    assert run_ai_extraction_worker_once(database, settings=settings, worker_id="scope-test")
    assert provider_called is False

    with database.session_factory() as session:
        with bypass_organization_scope(session):
            failed = session.get(ResumeAiExtractionJob, cross_job_id)
            other = session.scalar(
                select(ResumeAiExtractionJob).where(
                    ResumeAiExtractionJob.organization_id == organization_b
                )
            )
        assert failed is not None
        assert failed.organization_id == organization_a
        assert failed.status == AI_EXTRACTION_NEEDS_ATTENTION
        assert failed.last_error == "resume_not_found"
        assert other is not None
        assert other.status == AI_EXTRACTION_QUEUED
        assert other.attempt_count == 0

    database.dispose()


def test_summary_worker_never_sends_a_foreign_resume_to_the_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A malformed summary task must fail inside its own workspace boundary."""

    database = _database(tmp_path)
    settings = _settings(tmp_path)
    organization_a, organization_b = _organizations(database)
    now = extraction_utcnow()

    with database.session_factory() as session:
        with _workspace_session(session, organization_b):
            foreign_resume = _resume(
                session,
                organization_id=organization_b,
                label="foreign-summary",
                active=True,
            )
            foreign_snapshot = _snapshot(session, resume=foreign_resume)
            untouched_resume = _resume(
                session,
                organization_id=organization_b,
                label="untouched-summary",
                active=True,
            )
            untouched_snapshot = _snapshot(session, resume=untouched_resume)
            untouched_job = ResumeSummaryJob(
                resume_id=untouched_resume.id,
                fact_snapshot_id=untouched_snapshot.id,
                facts_version=untouched_snapshot.facts_version,
                status="queued",
                max_attempts=1,
                next_attempt_at=now,
                requested_at=now,
            )
            session.add(untouched_job)
            session.flush()
            untouched_job_id = untouched_job.id
        with _workspace_session(session, organization_a):
            cross_workspace_job = ResumeSummaryJob(
                resume_id=foreign_resume.id,
                fact_snapshot_id=foreign_snapshot.id,
                facts_version=foreign_snapshot.facts_version,
                status="queued",
                max_attempts=1,
                next_attempt_at=now - timedelta(seconds=10),
                requested_at=now - timedelta(seconds=10),
            )
            session.add(cross_workspace_job)
            session.commit()
            cross_job_id = cross_workspace_job.id

    def _provider_must_not_run(**_: object) -> object:
        raise AssertionError("foreign resume facts reached automatic summary")

    monkeypatch.setattr(
        resume_summary_job_service,
        "generate_resume_summary",
        _provider_must_not_run,
    )

    assert run_resume_summary_worker_once(
        database,
        settings=settings,
        worker_id="summary-scope-test",
    )

    with database.session_factory() as session:
        with bypass_organization_scope(session):
            failed = session.get(ResumeSummaryJob, cross_job_id)
            untouched = session.get(ResumeSummaryJob, untouched_job_id)
        assert failed is not None
        assert failed.organization_id == organization_a
        assert failed.status == "failed"
        assert failed.last_error == "resume_summary_workspace_mismatch"
        assert untouched is not None
        assert untouched.organization_id == organization_b
        assert untouched.status == "queued"
        assert untouched.attempt_count == 0

    database.dispose()


def test_job_match_worker_fails_cross_workspace_item_without_matching_foreign_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A claimed JD batch item may only resolve its resume inside its own workspace."""

    database = _database(tmp_path)
    settings = _settings(tmp_path)
    organization_a, organization_b = _organizations(database)
    now = extraction_utcnow()

    with database.session_factory() as session:
        with _workspace_session(session, organization_b):
            foreign_resume = _resume(
                session,
                organization_id=organization_b,
                label="foreign-match",
                active=True,
            )
            foreign_snapshot = _snapshot(session, resume=foreign_resume)
            untouched_resume = _resume(
                session,
                organization_id=organization_b,
                label="untouched-match",
                active=True,
            )
            untouched_snapshot = _snapshot(session, resume=untouched_resume)
            job_b = Job(title="B job", jd_text="B", requirements={})
            session.add(job_b)
            session.flush()
            version_b = JobVersion(
                job_id=job_b.id,
                version=1,
                title="B job",
                raw_text="B",
                status="confirmed",
            )
            session.add(version_b)
            session.flush()
            batch_b = JobMatchBatch(
                job_version_id=version_b.id,
                status=BATCH_QUEUED,
                total_count=1,
                max_attempts=1,
                requested_at=now,
            )
            session.add(batch_b)
            session.flush()
            untouched_item = JobMatchBatchItem(
                batch_id=batch_b.id,
                resume_id=untouched_resume.id,
                fact_snapshot_id=untouched_snapshot.id,
                facts_version=0,
                status=ITEM_QUEUED,
                next_attempt_at=now,
            )
            session.add(untouched_item)
            session.flush()
            untouched_item_id = untouched_item.id
        with _workspace_session(session, organization_a):
            job_a = Job(title="A job", jd_text="A", requirements={})
            session.add(job_a)
            session.flush()
            version_a = JobVersion(
                job_id=job_a.id,
                version=1,
                title="A job",
                raw_text="A",
                status="confirmed",
            )
            session.add(version_a)
            session.flush()
            batch_a = JobMatchBatch(
                job_version_id=version_a.id,
                status=BATCH_QUEUED,
                total_count=1,
                max_attempts=1,
                requested_at=now - timedelta(seconds=10),
            )
            session.add(batch_a)
            session.flush()
            cross_workspace_item = JobMatchBatchItem(
                batch_id=batch_a.id,
                resume_id=foreign_resume.id,
                fact_snapshot_id=foreign_snapshot.id,
                facts_version=0,
                status=ITEM_QUEUED,
                next_attempt_at=now - timedelta(seconds=10),
            )
            session.add(cross_workspace_item)
            session.commit()
            cross_item_id = cross_workspace_item.id

    def _match_must_not_run(**_: object) -> object:
        raise AssertionError("foreign resume reached JD matching")

    monkeypatch.setattr(job_match_batch_service, "run_job_match", _match_must_not_run)

    assert run_job_match_batch_worker_once(database, settings=settings, worker_id="scope-test")

    with database.session_factory() as session:
        with bypass_organization_scope(session):
            failed = session.get(JobMatchBatchItem, cross_item_id)
            untouched = session.get(JobMatchBatchItem, untouched_item_id)
        assert failed is not None
        assert failed.organization_id == organization_a
        assert failed.status == ITEM_FAILED
        assert failed.last_error == "resume_no_longer_ready_for_job_match"
        assert untouched is not None
        assert untouched.organization_id == organization_b
        assert untouched.status == ITEM_QUEUED
        assert untouched.attempt_count == 0

    database.dispose()


def test_due_mailbox_sync_reopens_only_the_claimed_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The global scheduler may discover configs, but IMAP work is tenant-bound."""

    database = _database(tmp_path)
    settings = _settings(tmp_path)
    organization_a, organization_b = _organizations(database)
    now = extraction_utcnow()

    with database.session_factory() as session:
        with _workspace_session(session, organization_a):
            config_a = MailboxConfig(
                imap_host="imap.a.test",
                imap_port=993,
                email_address="mailbox-a@example.test",
                mailbox="INBOX",
                encrypted_password="not-used-in-test",
                enabled=True,
                last_synced_at=now - timedelta(seconds=120),
            )
            session.add(config_a)
            session.flush()
            config_a_id = config_a.id
        with _workspace_session(session, organization_b):
            config_b = MailboxConfig(
                imap_host="imap.b.test",
                imap_port=993,
                email_address="mailbox-b@example.test",
                mailbox="INBOX",
                encrypted_password="not-used-in-test",
                enabled=True,
                last_synced_at=now,
            )
            session.add(config_b)
            session.commit()
            config_b_id = config_b.id

    observed: list[tuple[str, str]] = []

    def _scoped_sync(
        session: Session,
        *,
        settings: AppSettings,
        config_id: str | None = None,
        expected_source_fingerprint: str | None = None,
        heartbeat=None,
    ) -> MailboxSyncResponse:
        assert config_id == config_a_id
        assert organization_context_id(session) == organization_a
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        assert config.organization_id == organization_a
        assert session.get(MailboxConfig, config_b_id) is None
        mailbox_import_service._record(
            session,
            config=config,
            uid="1",
            message_id=None,
            filename="safe.pdf",
            attachment_sha256="b" * 64,
            status="skipped",
            error="no_supported_attachment",
            resume_id=None,
            received_at=None,
        )
        # Mailbox records now include an attempt-audit row and are flushed by
        # the helper. The worker service owns the unit-of-work boundary.
        session.commit()
        observed.append((config.id, organization_context_id(session)))
        return MailboxSyncResponse(configured=True)

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", _scoped_sync)

    assert sync_due_mailboxes(database=database, settings=settings)
    assert observed == []
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        database,
        settings=settings,
        worker_id="mailbox-worker-scope-test",
    )
    assert observed == [(config_a_id, organization_a)]

    with database.session_factory() as session:
        with bypass_organization_scope(session):
            records = session.scalars(select(EmailAttachmentImport)).all()
        assert len(records) == 1
        assert records[0].organization_id == organization_a
        assert records[0].mailbox_config_id == config_a_id

    database.dispose()
