from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Candidate,
    CandidateDataAuditEvent,
    CandidateDataDeletionBatch,
    CandidateDataFileAccessGrant,
    CandidateDataRetentionCleanupRun,
    CandidateDataRetentionPolicy,
    Resume,
    utcnow,
)
from app.services import candidate_data_lifecycle_service
from app.services.candidate_data_lifecycle_service import CandidateDataLifecycleError
from test_resume_flow import make_pdf_with_text


def _register_and_login(
    client: TestClient,
    *,
    organization_name: str,
    email: str,
) -> dict[str, object]:
    password = "candidate-data-lifecycle-test-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": "Lifecycle Test Admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text

    provider = client.app.state.transactional_email_provider
    delivery = next(item for item in reversed(provider.deliveries) if item.recipient == email)
    verification_token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post(
        "/v1/auth/email-verification/complete",
        json={"token": verification_token},
    )
    assert verified.status_code == 200, verified.text

    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()


@pytest.fixture
def lifecycle_workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two signed-in workspace admins sharing a single ephemeral database."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="candidate-data-lifecycle-test-session-secret",
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


def _upload_resume(client: TestClient, *, filename: str = "lifecycle-resume.pdf") -> tuple[str, str, bytes]:
    candidate = client.post("/v1/candidates", json={"display_name": "Lifecycle fixture"})
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["candidate_id"]
    resume_id, content = _upload_resume_for_candidate(
        client,
        candidate_id=candidate_id,
        filename=filename,
    )
    return candidate_id, resume_id, content


def _upload_resume_for_candidate(
    client: TestClient,
    *,
    candidate_id: str,
    filename: str,
) -> tuple[str, bytes]:
    content = make_pdf_with_text("Candidate lifecycle PDF fixture Python SQL " * 8)
    uploaded = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": (filename, content, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()["resume_id"], content


def _audit_payload(client: TestClient) -> dict[str, object]:
    response = client.get("/v1/candidate-data/audit-events")
    assert response.status_code == 200, response.text
    return response.json()


def test_original_file_access_is_explicit_audited_and_purpose_specific(client: TestClient) -> None:
    """Only an explicit intent grants a session-bound original-file response."""

    _, resume_id, content = _upload_resume(client)

    unknown = client.get("/v1/file-access/not-a-real-access-token")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "candidate_data_file_access_not_found"
    assert _audit_payload(client)["total"] == 0

    view_grant = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "view"},
    )
    assert view_grant.status_code == 200, view_grant.text
    view_url = view_grant.json()["access_url"]
    viewed = client.get(view_url)
    assert viewed.status_code == 200, viewed.text
    assert viewed.content == content
    assert viewed.headers["content-disposition"].startswith("inline;")
    assert viewed.headers["cache-control"] == "no-store, private"
    assert viewed.headers["referrer-policy"] == "no-referrer"

    # Browser range/iframe retries reuse the same server grant; they must not
    # fabricate a second human-access audit event.
    viewed_again = client.get(view_url)
    assert viewed_again.status_code == 200, viewed_again.text
    assert viewed_again.content == content

    download_grant = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "download"},
    )
    assert download_grant.status_code == 200, download_grant.text
    downloaded = client.get(download_grant.json()["access_url"])
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content
    assert downloaded.headers["content-disposition"].startswith("attachment;")

    audit = _audit_payload(client)
    assert audit["total"] == 2
    events = audit["items"]
    assert isinstance(events, list)
    assert {item["action"] for item in events} == {
        "resume_original_view_authorized",
        "resume_original_download_authorized",
    }
    for event in events:
        assert event["target_type"] == "resume"
        assert event["target_id"] == resume_id
        # The API audit projection is deliberately metadata-only.
        assert not {
            "candidate_name",
            "display_name",
            "original_filename",
            "storage_key",
            "raw_text",
            "email_address",
        }.intersection(event)


def test_candidate_delete_immediately_hides_and_restore_requires_fresh_file_grant(
    client: TestClient,
) -> None:
    candidate_id, resume_id, content = _upload_resume(client)
    existing_grant = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "view"},
    )
    assert existing_grant.status_code == 200, existing_grant.text

    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    deletion = deleted.json()
    assert deletion["affected_candidate_count"] == 1
    assert deletion["affected_resume_count"] == 1

    # The logical delete removes the record from ordinary reads and revokes
    # grants immediately, before the later physical-purge worker runs.
    hidden_detail = client.get(f"/v1/resumes/{resume_id}")
    assert hidden_detail.status_code == 404
    assert hidden_detail.json()["detail"] == "resume_not_found"
    hidden_original = client.get(existing_grant.json()["access_url"])
    assert hidden_original.status_code == 404
    assert hidden_original.json()["detail"] == "candidate_data_file_access_not_found"

    restored = client.post(
        f"/v1/candidate-data/deletions/{deletion['deletion_batch_id']}/restore"
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["restored_candidate_count"] == 1
    assert restored.json()["restored_resume_count"] == 1
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 200

    # Restoration does not revive a previously issued URL; a new explicit
    # intent is required and receives a new audit record.
    still_revoked = client.get(existing_grant.json()["access_url"])
    assert still_revoked.status_code == 404
    fresh_grant = client.post(
        f"/v1/resumes/{resume_id}/file-access",
        json={"purpose": "view"},
    )
    assert fresh_grant.status_code == 200, fresh_grant.text
    assert client.get(fresh_grant.json()["access_url"]).content == content


def test_single_resume_delete_preserves_other_candidate_versions(client: TestClient) -> None:
    candidate = client.post("/v1/candidates", json={"display_name": "Lifecycle fixture"})
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["candidate_id"]
    first_resume_id, _ = _upload_resume_for_candidate(
        client,
        candidate_id=candidate_id,
        filename="lifecycle-first.pdf",
    )
    second_resume_id, _ = _upload_resume_for_candidate(
        client,
        candidate_id=candidate_id,
        filename="lifecycle-second.pdf",
    )

    deleted = client.request(
        "DELETE",
        f"/v1/resumes/{first_resume_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    assert deleted.json()["affected_candidate_count"] == 0
    assert deleted.json()["affected_resume_count"] == 1
    assert client.get(f"/v1/resumes/{first_resume_id}").status_code == 404
    assert client.get(f"/v1/resumes/{second_resume_id}").status_code == 200

    restored = client.post(
        f"/v1/candidate-data/deletions/{deleted.json()['deletion_batch_id']}/restore"
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["restored_candidate_count"] == 0
    assert restored.json()["restored_resume_count"] == 1
    assert client.get(f"/v1/resumes/{first_resume_id}").status_code == 200


def test_candidate_retention_hold_applies_to_all_current_resume_versions(
    client: TestClient,
) -> None:
    candidate = client.post("/v1/candidates", json={"display_name": "Lifecycle fixture"})
    assert candidate.status_code == 200, candidate.text
    candidate_id = candidate.json()["candidate_id"]
    first_resume_id, _ = _upload_resume_for_candidate(
        client,
        candidate_id=candidate_id,
        filename="retention-hold-first.pdf",
    )
    second_resume_id, _ = _upload_resume_for_candidate(
        client,
        candidate_id=candidate_id,
        filename="retention-hold-second.pdf",
    )

    enabled = client.put(
        f"/v1/candidates/{candidate_id}/retention-hold",
        json={"retention_hold": True},
    )
    assert enabled.status_code == 204, enabled.text
    assert client.get(f"/v1/resumes/{first_resume_id}").json()["retention_hold"] is True
    assert client.get(f"/v1/resumes/{second_resume_id}").json()["retention_hold"] is True

    disabled = client.put(
        f"/v1/candidates/{candidate_id}/retention-hold",
        json={"retention_hold": False},
    )
    assert disabled.status_code == 204, disabled.text
    assert client.get(f"/v1/resumes/{first_resume_id}").json()["retention_hold"] is False
    assert client.get(f"/v1/resumes/{second_resume_id}").json()["retention_hold"] is False


def test_deletion_recovery_list_is_metadata_only_and_workspace_scoped(
    lifecycle_workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = lifecycle_workspace_clients
    _register_and_login(
        client_a,
        organization_name="Lifecycle recovery alpha",
        email="lifecycle-recovery-alpha@example.test",
    )
    _register_and_login(
        client_b,
        organization_name="Lifecycle recovery beta",
        email="lifecycle-recovery-beta@example.test",
    )
    candidate_a_id, _, _ = _upload_resume(client_a, filename="alpha-private-resume.pdf")
    candidate_b_id, _, _ = _upload_resume(client_b, filename="beta-private-resume.pdf")
    deleted_a = client_a.request(
        "DELETE", f"/v1/candidates/{candidate_a_id}", json={"reason": "duplicate"}
    )
    deleted_b = client_b.request(
        "DELETE", f"/v1/candidates/{candidate_b_id}", json={"reason": "duplicate"}
    )
    assert deleted_a.status_code == deleted_b.status_code == 202

    listed_a = client_a.get("/v1/candidate-data/deletions")
    assert listed_a.status_code == 200, listed_a.text
    payload = listed_a.json()
    assert payload["total"] == 1
    assert payload["items"][0]["deletion_batch_id"] == deleted_a.json()["deletion_batch_id"]
    assert payload["items"][0]["restorable"] is True
    assert payload["items"][0]["affected_candidate_count"] == 1
    assert payload["items"][0]["affected_resume_count"] == 1
    # A recovery list is intentionally usable without becoming a backdoor for
    # deleted candidate data or user-provided notes.
    assert not {
        "candidate_id",
        "resume_id",
        "display_name",
        "original_filename",
        "private_note",
    }.intersection(payload["items"][0])
    assert deleted_b.json()["deletion_batch_id"] not in {
        item["deletion_batch_id"] for item in payload["items"]
    }
    foreign_restore = client_a.post(
        f"/v1/candidate-data/deletions/{deleted_b.json()['deletion_batch_id']}/restore"
    )
    absent_restore = client_a.post(
        "/v1/candidate-data/deletions/not-a-real-deletion-batch/restore"
    )
    assert foreign_restore.status_code == absent_restore.status_code == 404
    assert (
        foreign_restore.json()["detail"]
        == absent_restore.json()["detail"]
        == "candidate_data_deletion_batch_not_found"
    )


def test_cross_workspace_resource_and_file_grant_are_indistinguishable(
    lifecycle_workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = lifecycle_workspace_clients
    _register_and_login(
        client_a,
        organization_name="Lifecycle workspace alpha",
        email="lifecycle-alpha@example.test",
    )
    _register_and_login(
        client_b,
        organization_name="Lifecycle workspace beta",
        email="lifecycle-beta@example.test",
    )
    candidate_b_id, resume_b_id, content = _upload_resume(client_b)
    grant_b = client_b.post(
        f"/v1/resumes/{resume_b_id}/file-access",
        json={"purpose": "view"},
    )
    assert grant_b.status_code == 200, grant_b.text
    private_url = grant_b.json()["access_url"]

    foreign_resource = client_a.post(
        f"/v1/resumes/{resume_b_id}/file-access",
        json={"purpose": "view"},
    )
    absent_resource = client_a.post(
        "/v1/resumes/not-a-real-resume/file-access",
        json={"purpose": "view"},
    )
    assert foreign_resource.status_code == absent_resource.status_code == 404
    assert foreign_resource.json()["detail"] == absent_resource.json()["detail"] == "resume_not_found"

    foreign_grant = client_a.get(private_url)
    absent_grant = client_a.get("/v1/file-access/not-a-real-access-token")
    assert foreign_grant.status_code == absent_grant.status_code == 404
    assert (
        foreign_grant.json()["detail"]
        == absent_grant.json()["detail"]
        == "candidate_data_file_access_not_found"
    )

    foreign_delete = client_a.request(
        "DELETE",
        f"/v1/candidates/{candidate_b_id}",
        json={"reason": "duplicate"},
    )
    absent_delete = client_a.request(
        "DELETE",
        "/v1/candidates/not-a-real-candidate",
        json={"reason": "duplicate"},
    )
    assert foreign_delete.status_code == absent_delete.status_code == 404
    assert foreign_delete.json()["detail"] == absent_delete.json()["detail"] == "candidate_not_found"
    assert client_b.get(f"/v1/resumes/{resume_b_id}").status_code == 200
    assert client_b.get(private_url).content == content
    assert _audit_payload(client_a)["total"] == 0


def test_retention_preview_does_not_delete_or_mutate_existing_policy(client: TestClient) -> None:
    _, resume_id, _ = _upload_resume(client)
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        candidate = session.get(Candidate, resume.candidate_id)
        assert candidate is not None
        expired_fixture_time = utcnow() - timedelta(days=31)
        candidate.created_at = expired_fixture_time
        resume.created_at = expired_fixture_time
        session.commit()

    # Establish the default policy first.  Production migration creates this
    # record up front; the assertion below proves the preview itself does not
    # change it or enqueue any cleanup work.
    before = client.get("/v1/candidate-data/retention")
    assert before.status_code == 200, before.text
    before_policy = before.json()
    assert before_policy["mode"] == "manual"

    preview = client.post(
        "/v1/candidate-data/retention/preview",
        json={"retention_days": 30},
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["eligible_candidate_count"] == 1
    assert payload["eligible_resume_count"] == 1
    assert payload["held_candidate_count"] == 0
    assert payload["already_deleted_count"] == 0

    after = client.get("/v1/candidate-data/retention")
    assert after.status_code == 200, after.text
    assert {
        key: after.json()[key]
        for key in ("mode", "retention_days", "version")
    } == {
        key: before_policy[key]
        for key in ("mode", "retention_days", "version")
    }
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 200

    with database.session_factory() as session:
        assert session.scalar(select(func.count(CandidateDataRetentionPolicy.id))) == 1
        assert session.scalar(select(func.count(CandidateDataDeletionBatch.id))) == 0
        assert session.scalar(select(func.count(CandidateDataRetentionCleanupRun.id))) == 0
        assert session.scalar(select(func.count(CandidateDataAuditEvent.id))) == 0
        resume = session.get(Resume, resume_id)
        assert resume is not None
        assert resume.deleted_at is None


def _enable_automatic_retention(client: TestClient, *, retention_days: int = 30) -> None:
    preview = client.post(
        "/v1/candidate-data/retention/preview",
        json={"retention_days": retention_days},
    )
    assert preview.status_code == 200, preview.text
    updated = client.put(
        "/v1/candidate-data/retention",
        json={
            "mode": "automatic",
            "retention_days": retention_days,
            "preview_token": preview.json()["preview_token"],
        },
    )
    assert updated.status_code == 200, updated.text


def _make_candidate_retention_eligible(client: TestClient) -> tuple[str, str]:
    candidate_id, resume_id, _ = _upload_resume(client)
    database = client.app.state.database
    expired_fixture_time = utcnow() - timedelta(days=31)
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        candidate = session.get(Candidate, candidate_id)
        assert candidate is not None
        candidate.created_at = expired_fixture_time
        resume.created_at = expired_fixture_time
        session.commit()
    return candidate_id, resume_id


def test_retention_rechecks_a_hold_added_after_the_scan(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate worklist is advisory; the live hold wins at deletion."""

    candidate_id, resume_id = _make_candidate_retention_eligible(client)
    _enable_automatic_retention(client)
    real_scan = candidate_data_lifecycle_service._eligible_retention_candidate_ids

    def scan_then_enable_hold(session, **kwargs):
        candidate_ids = real_scan(session, **kwargs)
        assert candidate_ids == [candidate_id]
        candidate_data_lifecycle_service.set_candidate_retention_hold(
            session,
            candidate_id=candidate_id,
            retention_hold=True,
            actor_user_id=None,
        )
        return candidate_ids

    monkeypatch.setattr(
        candidate_data_lifecycle_service,
        "_eligible_retention_candidate_ids",
        scan_then_enable_hold,
    )
    cleaned = client.post("/v1/candidate-data/retention/cleanup")
    assert cleaned.status_code == 202, cleaned.text
    assert cleaned.json()["queued_count"] == 0
    assert cleaned.json()["skipped_hold_count"] == 1
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 200


def test_retention_rechecks_manual_policy_changed_after_the_scan(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching off automatic retention prevents queued IDs from deletion."""

    _, resume_id = _make_candidate_retention_eligible(client)
    _enable_automatic_retention(client)
    real_scan = candidate_data_lifecycle_service._eligible_retention_candidate_ids

    def scan_then_switch_manual(session, **kwargs):
        candidate_ids = real_scan(session, **kwargs)
        policy = session.scalar(select(CandidateDataRetentionPolicy))
        assert policy is not None
        policy.mode = "manual"
        policy.retention_days = None
        policy.version += 1
        session.flush()
        return candidate_ids

    monkeypatch.setattr(
        candidate_data_lifecycle_service,
        "_eligible_retention_candidate_ids",
        scan_then_switch_manual,
    )
    cleaned = client.post("/v1/candidate-data/retention/cleanup")
    assert cleaned.status_code == 202, cleaned.text
    assert cleaned.json()["queued_count"] == 0
    assert cleaned.json()["skipped_hold_count"] == 1
    assert client.get(f"/v1/resumes/{resume_id}").status_code == 200
    assert client.get("/v1/candidate-data/retention").json()["mode"] == "manual"


def test_delayed_duplicate_resume_claim_cannot_create_a_second_batch(client: TestClient) -> None:
    """A stale lifecycle version loses the conditional root claim cleanly."""

    candidate_id, resume_id, _ = _upload_resume(client)
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        stale_version = resume.lifecycle_version

    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text

    with database.session_factory() as session:
        now = utcnow()
        delayed_batch = CandidateDataDeletionBatch(
            trigger_type="manual_resume",
            reason="duplicate",
            status="deleted",
            recovery_deadline_at=now + timedelta(days=7),
            purge_after_at=now + timedelta(days=7),
        )
        session.add(delayed_batch)
        session.flush()
        stale_resume = Resume(
            id=resume_id,
            candidate_id=candidate_id,
            lifecycle_version=stale_version,
        )
        assert not candidate_data_lifecycle_service._claim_live_resume_for_deletion(
            session,
            resume=stale_resume,
            deletion_batch_id=delayed_batch.id,
            actor_user_id=None,
            purge_after_at=delayed_batch.purge_after_at,
            now=now,
            require_visible_candidate=True,
        )
        session.rollback()

    with database.session_factory() as session:
        assert session.scalar(select(func.count(CandidateDataDeletionBatch.id))) == 1


def test_late_original_file_grant_stays_invalid_after_delete_and_restore(
    client: TestClient,
) -> None:
    """A grant that captured the old resume epoch cannot revive on restore."""

    candidate_id, resume_id, _ = _upload_resume(client)
    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        original_lifecycle_version = resume.lifecycle_version
        organization_id = resume.organization_id

    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    restored = client.post(
        f"/v1/candidate-data/deletions/{deleted.json()['deletion_batch_id']}/restore"
    )
    assert restored.status_code == 200, restored.text

    opaque_token = "late-original-file-grant-token"
    session_nonce = "late-original-file-grant-session"
    with database.session_factory() as session:
        session.add(
            CandidateDataFileAccessGrant(
                organization_id=organization_id,
                actor_user_id=None,
                resource_type="resume_original",
                resource_id=resume_id,
                purpose="view",
                token_digest=hashlib.sha256(opaque_token.encode("utf-8")).hexdigest(),
                session_nonce_digest=hashlib.sha256(
                    session_nonce.encode("utf-8")
                ).hexdigest(),
                resource_lifecycle_version=original_lifecycle_version,
                expires_at=utcnow() + timedelta(minutes=5),
            )
        )
        session.commit()

    with database.session_factory() as session:
        with pytest.raises(
            CandidateDataLifecycleError,
            match="candidate_data_file_access_not_found",
        ):
            candidate_data_lifecycle_service.resolve_resume_original_access(
                session,
                settings=settings,
                opaque_token=opaque_token,
                actor_user_id=None,
                session_nonce=session_nonce,
            )
