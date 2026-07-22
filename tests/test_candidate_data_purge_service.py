"""Physical-purge worker coverage for candidate-data lifecycle deletes.

These tests intentionally drive the public logical-delete API first, then
exercise the worker against the same database.  This verifies the recovery
window and tenant boundary rather than treating physical deletion as a raw
database operation.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Candidate,
    CandidateDataDeletionBatch,
    CandidateDataPurgeJob,
    Resume,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    ResumeFactSnapshot,
    ResumeSourceBlock,
    ResumeSummary,
    utcnow,
)
from app.services import candidate_data_purge_service
from app.services.candidate_data_purge_service import run_candidate_data_purge_worker_once
from app.services.resume_service import resolve_uploaded_resume_path
from app.tenant_scope import bypass_organization_scope, set_organization_context
from test_resume_flow import make_pdf_with_text


def _upload_fixture_resume(client: TestClient, *, label: str) -> tuple[str, str, bytes]:
    candidate = client.post("/v1/candidates", json={"display_name": f"{label} fixture"})
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["candidate_id"]
    content = make_pdf_with_text("Synthetic resume fixture Python SQL " * 8)
    uploaded = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": (f"{label}.pdf", content, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return candidate_id, uploaded.json()["resume_id"], content


def _delete_candidate(client: TestClient, *, candidate_id: str) -> str:
    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    return deleted.json()["deletion_batch_id"]


def _force_purge_due(
    client: TestClient,
    *,
    deletion_batch_id: str,
    organization_id: str | None = None,
) -> None:
    """Move only this batch's lease work to the past for deterministic tests."""

    database = client.app.state.database
    due = utcnow() - timedelta(seconds=1)
    with database.session_factory() as session:
        if organization_id is not None:
            set_organization_context(session, organization_id)
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        assert batch is not None
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert job is not None
        batch.purge_after_at = due
        batch.recovery_deadline_at = due
        job.next_attempt_at = due
        session.commit()


def _read_deleted_root(
    client: TestClient,
    *,
    model: type[Candidate] | type[Resume],
    row_id: str,
    organization_id: str | None = None,
):
    database = client.app.state.database
    with database.session_factory() as session:
        if organization_id is not None:
            set_organization_context(session, organization_id)
        return session.scalar(
            select(model)
            .where(model.id == row_id)
            .execution_options(include_deleted_candidate_data=True)
        )


def _resume_storage_path(
    client: TestClient,
    *,
    resume_id: str,
    organization_id: str | None = None,
) -> Path:
    resume = _read_deleted_root(
        client,
        model=Resume,
        row_id=resume_id,
        organization_id=organization_id,
    )
    assert resume is not None
    return resolve_uploaded_resume_path(
        client.app.state.settings,
        storage_key=resume.storage_key,
        organization_id=resume.organization_id,
    )


def _purge_once(client: TestClient, *, worker_id: str = "purge-test-worker") -> bool:
    return run_candidate_data_purge_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id=worker_id,
    )


def test_purge_worker_does_not_claim_before_recovery_window(client: TestClient) -> None:
    """A queued deletion remains recoverable until its configured deadline."""

    candidate_id, resume_id, _ = _upload_fixture_resume(client, label="not-due")
    original_path = _resume_storage_path(client, resume_id=resume_id)
    assert original_path.exists()
    deletion_batch_id = _delete_candidate(client, candidate_id=candidate_id)

    assert _purge_once(client) is False

    deleted_resume = _read_deleted_root(client, model=Resume, row_id=resume_id)
    assert deleted_resume is not None
    assert deleted_resume.deleted_at is not None
    assert original_path.exists()
    with client.app.state.database.session_factory() as session:
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert job is not None
        assert job.status == "queued"
        assert job.attempt_count == 0


def test_due_purge_removes_original_before_dependencies_and_roots(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Original storage must be gone before dependent rows and root rows vanish."""

    candidate_id, resume_id, _ = _upload_fixture_resume(client, label="due")
    original_path = _resume_storage_path(client, resume_id=resume_id)
    database = client.app.state.database
    with database.session_factory() as session:
        source_block = ResumeSourceBlock(
            resume_id=resume_id,
            block_id="fixture-block",
            page_no=1,
            block_type="text",
            text="synthetic source block",
        )
        session.add(source_block)
        session.flush()
        snapshot = ResumeFactSnapshot(
            resume_id=resume_id,
            facts_version=1,
            canonical_facts_json='{"fixture": true}',
            facts_sha256="a" * 64,
            source_block_ids=[source_block.block_id],
            created_by="purge-test",
        )
        session.add(snapshot)
        session.flush()
        session.add(
            ResumeSummary(
                resume_id=resume_id,
                fact_snapshot_id=snapshot.id,
                facts_version=1,
                content={"summary": "synthetic"},
                source="manual",
                is_current=True,
                status="succeeded",
                model_name=None,
            )
        )
        session.commit()
    deletion_batch_id = _delete_candidate(client, candidate_id=candidate_id)
    _force_purge_due(client, deletion_batch_id=deletion_batch_id)

    observed: dict[str, bool] = {}
    real_purge_database_rows = candidate_data_purge_service._purge_database_rows

    def observe_database_cleanup(session, **kwargs) -> None:
        # This seam is immediately after _resume_originals_removed.  It proves
        # the durable original disappears before dependent/root DB cleanup.
        observed["original_removed_before_database_cleanup"] = not original_path.exists()
        return real_purge_database_rows(session, **kwargs)

    monkeypatch.setattr(
        candidate_data_purge_service,
        "_purge_database_rows",
        observe_database_cleanup,
    )

    assert _purge_once(client) is True
    assert observed == {"original_removed_before_database_cleanup": True}
    assert not original_path.exists()

    with database.session_factory() as session:
        assert session.scalar(
            select(Resume)
            .where(Resume.id == resume_id)
            .execution_options(include_deleted_candidate_data=True)
        ) is None
        assert session.scalar(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted_candidate_data=True)
        ) is None
        assert session.scalar(
            select(func.count(ResumeSourceBlock.id)).where(
                ResumeSourceBlock.resume_id == resume_id
            )
        ) == 0
        assert session.scalar(
            select(func.count(ResumeAiExtractionJob.id)).where(
                ResumeAiExtractionJob.resume_id == resume_id
            )
        ) == 0
        assert session.scalar(
            select(func.count(ResumeDocumentExtractionJob.id)).where(
                ResumeDocumentExtractionJob.resume_id == resume_id
            )
        ) == 0
        assert session.scalar(
            select(func.count(ResumeFactSnapshot.id)).where(
                ResumeFactSnapshot.resume_id == resume_id
            )
        ) == 0
        assert session.scalar(
            select(func.count(ResumeSummary.id)).where(
                ResumeSummary.resume_id == resume_id
            )
        ) == 0
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert batch is not None and batch.status == "purged"
        assert job is not None and job.status == "completed"


def test_storage_delete_failure_keeps_data_hidden_and_is_retryable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed unlink must not expose data or destroy retry state prematurely."""

    candidate_id, resume_id, _ = _upload_fixture_resume(client, label="retry")
    original_path = _resume_storage_path(client, resume_id=resume_id)
    deletion_batch_id = _delete_candidate(client, candidate_id=candidate_id)
    _force_purge_due(client, deletion_batch_id=deletion_batch_id)

    real_resolver = candidate_data_purge_service.resolve_uploaded_resume_path

    class FailingPath:
        def unlink(self, *, missing_ok: bool = False) -> None:
            raise OSError("synthetic storage unlink failure")

    monkeypatch.setattr(
        candidate_data_purge_service,
        "resolve_uploaded_resume_path",
        lambda *args, **kwargs: FailingPath(),
    )
    assert _purge_once(client) is True
    assert original_path.exists()
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 404

    with client.app.state.database.session_factory() as session:
        failed_resume = session.scalar(
            select(Resume)
            .where(Resume.id == resume_id)
            .execution_options(include_deleted_candidate_data=True)
        )
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert failed_resume is not None and failed_resume.deleted_at is not None
        assert job is not None
        assert job.status == "queued"
        assert job.attempt_count == 1
        assert job.last_error == "candidate_data_storage_delete_failed"
        assert job.next_attempt_at is not None

    # The retry must use the original resolver and only claim after the
    # backoff timestamp.  It removes the still-hidden record cleanly.
    monkeypatch.setattr(
        candidate_data_purge_service,
        "resolve_uploaded_resume_path",
        real_resolver,
    )
    _force_purge_due(client, deletion_batch_id=deletion_batch_id)
    assert _purge_once(client) is True
    assert not original_path.exists()
    assert _read_deleted_root(client, model=Resume, row_id=resume_id) is None


def test_restored_batch_is_never_physically_purged(client: TestClient) -> None:
    """Restoring during the recovery window cancels the queued purge forever."""

    candidate_id, resume_id, _ = _upload_fixture_resume(client, label="restored")
    original_path = _resume_storage_path(client, resume_id=resume_id)
    deletion_batch_id = _delete_candidate(client, candidate_id=candidate_id)

    restored = client.post(f"/v1/candidate-data/deletions/{deletion_batch_id}/restore")
    assert restored.status_code == 200, restored.text
    _force_purge_due(client, deletion_batch_id=deletion_batch_id)

    assert _purge_once(client) is False
    assert original_path.exists()
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 200
    with client.app.state.database.session_factory() as session:
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert batch is not None and batch.status == "restored"
        assert job is not None and job.status == "cancelled"


def test_purge_never_unlinks_when_restore_fence_already_won(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable restoring state is checked before any filesystem action."""

    candidate_id, resume_id, _ = _upload_fixture_resume(client, label="restore-fence")
    original_path = _resume_storage_path(client, resume_id=resume_id)
    deletion_batch_id = _delete_candidate(client, candidate_id=candidate_id)
    _force_purge_due(client, deletion_batch_id=deletion_batch_id)
    database = client.app.state.database
    with database.session_factory() as session:
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        assert batch is not None
        # This is the durable state written by restore's delete -> restoring
        # CAS just before it begins to unhide roots.
        batch.status = "restoring"
        session.commit()

    def should_not_touch_files(*args, **kwargs) -> None:
        raise AssertionError("purge touched originals after restore fence")

    monkeypatch.setattr(
        candidate_data_purge_service,
        "_resume_originals_removed",
        should_not_touch_files,
    )
    assert _purge_once(client) is True
    assert original_path.exists()
    with database.session_factory() as session:
        job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert job is not None and job.status == "cancelled"


def _register_and_login(
    client: TestClient,
    *,
    organization_name: str,
    email: str,
) -> dict[str, object]:
    password = "purge-worker-test-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": "Purge Worker Test Admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    provider = client.app.state.transactional_email_provider
    delivery = next(item for item in reversed(provider.deliveries) if item.recipient == email)
    verification_token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": verification_token})
    assert verified.status_code == 200, verified.text
    logged_in = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()


@pytest.fixture
def purge_workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two independent workspace sessions backed by the same in-memory DB."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="purge-worker-test-session-secret",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
    with TestClient(app):
        client_a = TestClient(app)
        client_b = TestClient(app)
        try:
            yield client_a, client_b
        finally:
            client_a.close()
            client_b.close()


def test_worker_claims_due_workspace_only_and_never_purges_foreign_data(
    purge_workspace_clients: tuple[TestClient, TestClient],
) -> None:
    """A global claim may find A, but its cleanup scope cannot touch B."""

    client_a, client_b = purge_workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Purge Alpha",
        email="purge-alpha@example.test",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Purge Beta",
        email="purge-beta@example.test",
    )
    organization_a_id = str(session_a["organization"]["organization_id"])
    organization_b_id = str(session_b["organization"]["organization_id"])
    candidate_a_id, resume_a_id, _ = _upload_fixture_resume(client_a, label="workspace-a")
    candidate_b_id, resume_b_id, _ = _upload_fixture_resume(client_b, label="workspace-b")
    original_a = _resume_storage_path(
        client_a,
        resume_id=resume_a_id,
        organization_id=organization_a_id,
    )
    original_b = _resume_storage_path(
        client_b,
        resume_id=resume_b_id,
        organization_id=organization_b_id,
    )
    deletion_a = _delete_candidate(client_a, candidate_id=candidate_a_id)
    deletion_b = _delete_candidate(client_b, candidate_id=candidate_b_id)
    _force_purge_due(
        client_a,
        deletion_batch_id=deletion_a,
        organization_id=organization_a_id,
    )

    assert _purge_once(client_a, worker_id="cross-workspace-purge-worker") is True
    assert not original_a.exists()
    assert original_b.exists()

    database = client_a.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            resume_a = session.scalar(
                select(Resume)
                .where(Resume.id == resume_a_id)
                .execution_options(include_deleted_candidate_data=True)
            )
            resume_b = session.scalar(
                select(Resume)
                .where(Resume.id == resume_b_id)
                .execution_options(include_deleted_candidate_data=True)
            )
            candidate_b = session.scalar(
                select(Candidate)
                .where(Candidate.id == candidate_b_id)
                .execution_options(include_deleted_candidate_data=True)
            )
            job_b = session.scalar(
                select(CandidateDataPurgeJob).where(
                    CandidateDataPurgeJob.deletion_batch_id == deletion_b
                )
            )
        assert resume_a is None
        assert resume_b is not None and resume_b.organization_id == organization_b_id
        assert resume_b.deleted_at is not None
        assert candidate_b is not None and candidate_b.deleted_at is not None
        assert job_b is not None and job_b.status == "queued"
        assert job_b.attempt_count == 0
