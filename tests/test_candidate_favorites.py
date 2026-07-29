from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    CandidateFavorite,
    CandidateDataDeletionBatch,
    CandidateDataPurgeJob,
    OrganizationMembership,
    Resume,
    UserAccount,
    utcnow,
)
from app.services.candidate_data_purge_service import run_candidate_data_purge_worker_once
from app.services.identity_service import hash_password
from app.tenant_scope import set_organization_context
from test_tenant_isolation import (
    _create_candidate_and_resume,
    _pdf_with_text,
    _register_and_login,
    workspace_clients,
)


def _activate_resume(
    client: TestClient,
    *,
    organization_id: str,
    resume_id: str,
) -> None:
    with client.app.state.database.session_factory() as session:
        set_organization_context(session, organization_id)
        resume = session.scalar(select(Resume).where(Resume.id == resume_id))
        assert resume is not None
        resume.extraction_status = "ready"
        resume.is_active = True
        session.commit()


def _force_purge_due(
    client: TestClient,
    *,
    organization_id: str,
    deletion_batch_id: str,
) -> None:
    due = utcnow() - timedelta(seconds=1)
    with client.app.state.database.session_factory() as session:
        set_organization_context(session, organization_id)
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
        assert batch is not None
        assert job is not None
        batch.purge_after_at = due
        batch.recovery_deadline_at = due
        job.next_attempt_at = due
        session.commit()


def test_candidate_favorites_are_private_to_each_workspace(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Favorite workspace alpha",
        full_name="Alpha recruiter",
        email="favorite-alpha@example.test",
        password="favorite-alpha-password",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Favorite workspace beta",
        full_name="Beta recruiter",
        email="favorite-beta@example.test",
        password="favorite-beta-password",
    )
    organization_a_id = str(session_a["organization"]["organization_id"])
    organization_b_id = str(session_b["organization"]["organization_id"])
    candidate_a_id, resume_a_id = _create_candidate_and_resume(
        client_a,
        display_name="Alpha favorite candidate",
    )
    candidate_b_id, resume_b_id = _create_candidate_and_resume(
        client_b,
        display_name="Beta favorite candidate",
    )
    _activate_resume(client_a, organization_id=organization_a_id, resume_id=resume_a_id)
    _activate_resume(client_b, organization_id=organization_b_id, resume_id=resume_b_id)

    first_save = client_a.put(f"/v1/candidates/{candidate_a_id}/favorite")
    repeated_save = client_a.put(f"/v1/candidates/{candidate_a_id}/favorite")
    assert first_save.status_code == 200, first_save.text
    assert repeated_save.status_code == 200, repeated_save.text
    assert first_save.json() == {
        "candidate_id": candidate_a_id,
        "is_favorited": True,
    }
    assert repeated_save.json() == first_save.json()

    alpha_library = client_a.get("/v1/resume-library")
    assert alpha_library.status_code == 200, alpha_library.text
    assert alpha_library.json()["items"][0]["is_favorited"] is True
    alpha_review = client_a.get(f"/v1/resumes/{resume_a_id}/review")
    assert alpha_review.status_code == 200, alpha_review.text
    assert alpha_review.json()["is_favorited"] is True

    alpha_favorites = client_a.get("/v1/candidate-favorites")
    assert alpha_favorites.status_code == 200, alpha_favorites.text
    assert alpha_favorites.json()["total"] == 1
    assert [item["candidate_id"] for item in alpha_favorites.json()["items"]] == [
        candidate_a_id
    ]
    assert alpha_favorites.json()["items"][0]["is_favorited"] is True

    foreign_save = client_a.put(f"/v1/candidates/{candidate_b_id}/favorite")
    foreign_remove = client_a.delete(f"/v1/candidates/{candidate_b_id}/favorite")
    assert foreign_save.status_code == 404, foreign_save.text
    assert foreign_remove.status_code == 404, foreign_remove.text
    assert foreign_save.json()["detail"] == "candidate_not_found"
    assert foreign_remove.json()["detail"] == "candidate_not_found"
    assert client_b.get("/v1/candidate-favorites").json()["total"] == 0

    first_remove = client_a.delete(f"/v1/candidates/{candidate_a_id}/favorite")
    repeated_remove = client_a.delete(f"/v1/candidates/{candidate_a_id}/favorite")
    assert first_remove.status_code == 200, first_remove.text
    assert repeated_remove.status_code == 200, repeated_remove.text
    assert first_remove.json() == {
        "candidate_id": candidate_a_id,
        "is_favorited": False,
    }
    assert repeated_remove.json() == first_remove.json()
    assert client_a.get("/v1/candidate-favorites").json()["total"] == 0

    # A saved marker must never revive a candidate after the privacy lifecycle
    # removes that candidate from the active workspace view.
    assert client_a.put(f"/v1/candidates/{candidate_a_id}/favorite").status_code == 200
    deleted = client_a.request(
        "DELETE",
        f"/v1/candidates/{candidate_a_id}",
        json={"reason": "other", "other_note": "favorite lifecycle test"},
    )
    assert deleted.status_code == 202, deleted.text
    assert client_a.get("/v1/candidate-favorites").json()["total"] == 0
    assert client_a.put(f"/v1/candidates/{candidate_a_id}/favorite").status_code == 404


def test_candidate_favorite_keeps_pending_resume_visible(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client, _ = workspace_clients
    _register_and_login(
        client,
        organization_name="Pending favorite workspace",
        full_name="Pending recruiter",
        email="favorite-pending@example.test",
        password="favorite-pending-password",
    )
    candidate_id, resume_id = _create_candidate_and_resume(
        client,
        display_name="Pending favorite candidate",
    )

    saved = client.put(f"/v1/candidates/{candidate_id}/favorite")
    assert saved.status_code == 200, saved.text

    favorites = client.get("/v1/candidate-favorites")
    assert favorites.status_code == 200, favorites.text
    assert favorites.json()["total"] == 1
    items = favorites.json()["items"]
    assert len(items) == 1
    assert items[0]["resume_id"] == resume_id
    assert items[0]["candidate_id"] == candidate_id
    assert items[0]["is_active"] is False
    assert items[0]["is_favorited"] is True


def test_candidate_favorite_prefers_active_resume_over_newer_pending_version(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client, _ = workspace_clients
    session_payload = _register_and_login(
        client,
        organization_name="Preferred favorite workspace",
        full_name="Preferred recruiter",
        email="favorite-preferred@example.test",
        password="favorite-preferred-password",
    )
    organization_id = str(session_payload["organization"]["organization_id"])
    candidate_id, active_resume_id = _create_candidate_and_resume(
        client,
        display_name="Preferred favorite candidate",
    )
    _activate_resume(
        client,
        organization_id=organization_id,
        resume_id=active_resume_id,
    )
    pending_upload = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={
            "file": (
                "newer-pending.pdf",
                _pdf_with_text("A newer pending resume version"),
                "application/pdf",
            )
        },
    )
    assert pending_upload.status_code == 200, pending_upload.text
    pending_resume_id = pending_upload.json()["resume_id"]
    assert pending_resume_id != active_resume_id

    assert client.put(f"/v1/candidates/{candidate_id}/favorite").status_code == 200
    favorites = client.get("/v1/candidate-favorites")
    assert favorites.status_code == 200, favorites.text
    assert favorites.json()["total"] == 1
    assert [item["resume_id"] for item in favorites.json()["items"]] == [active_resume_id]
    assert favorites.json()["items"][0]["is_active"] is True


def test_candidate_favorite_is_physically_removed_with_candidate_data(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client, _ = workspace_clients
    session_payload = _register_and_login(
        client,
        organization_name="Purge favorite workspace",
        full_name="Purge recruiter",
        email="favorite-purge@example.test",
        password="favorite-purge-password",
    )
    organization_id = str(session_payload["organization"]["organization_id"])
    candidate_id, _ = _create_candidate_and_resume(
        client,
        display_name="Purge favorite candidate",
    )
    assert client.put(f"/v1/candidates/{candidate_id}/favorite").status_code == 200

    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "other", "other_note": "favorite physical purge test"},
    )
    assert deleted.status_code == 202, deleted.text
    _force_purge_due(
        client,
        organization_id=organization_id,
        deletion_batch_id=deleted.json()["deletion_batch_id"],
    )
    assert run_candidate_data_purge_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="favorite-purge-test-worker",
    ) is True
    with client.app.state.database.session_factory() as session:
        set_organization_context(session, organization_id)
        assert session.scalar(
            select(CandidateFavorite).where(
                CandidateFavorite.candidate_id == candidate_id
            )
        ) is None


def test_candidate_favorites_do_not_leak_between_recruiters_in_one_workspace(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    owner_client, _ = workspace_clients
    owner_session = _register_and_login(
        owner_client,
        organization_name="Shared favorite workspace",
        full_name="Owner recruiter",
        email="favorite-owner@example.test",
        password="favorite-owner-password",
    )
    organization_id = str(owner_session["organization"]["organization_id"])
    owner_id = str(owner_session["user"]["user_id"])
    candidate_id, resume_id = _create_candidate_and_resume(
        owner_client,
        display_name="Shared workspace candidate",
    )
    _activate_resume(owner_client, organization_id=organization_id, resume_id=resume_id)

    member_email = "favorite-member@example.test"
    member_password = "favorite-member-password"
    database = owner_client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        member = UserAccount(
            email=member_email,
            email_key=member_email,
            full_name="Second recruiter",
            password_hash=hash_password(member_password),
            email_verified_at=utcnow(),
        )
        session.add(member)
        session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization_id,
                user_id=member.id,
                role="recruiter",
            )
        )
        session.commit()
        member_id = member.id

    assert owner_client.put(f"/v1/candidates/{candidate_id}/favorite").status_code == 200

    member_client = TestClient(owner_client.app)
    try:
        login = member_client.post(
            "/v1/auth/login",
            json={"email": member_email, "password": member_password},
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["user_id"] == member_id
        assert login.json()["organization"]["organization_id"] == organization_id

        member_favorites = member_client.get("/v1/candidate-favorites")
        assert member_favorites.status_code == 200, member_favorites.text
        assert member_favorites.json()["total"] == 0
        member_library = member_client.get("/v1/resume-library")
        assert member_library.status_code == 200, member_library.text
        assert member_library.json()["items"][0]["is_favorited"] is False
        member_review = member_client.get(f"/v1/resumes/{resume_id}/review")
        assert member_review.status_code == 200, member_review.text
        assert member_review.json()["is_favorited"] is False

        # A recruiter can only remove their own marker. This idempotent call
        # must not remove the owner's favorite.
        member_remove = member_client.delete(f"/v1/candidates/{candidate_id}/favorite")
        assert member_remove.status_code == 200, member_remove.text
        assert member_remove.json()["is_favorited"] is False
        assert owner_client.get("/v1/candidate-favorites").json()["total"] == 1

        assert member_client.put(f"/v1/candidates/{candidate_id}/favorite").status_code == 200
        with database.session_factory() as session:
            set_organization_context(session, organization_id)
            favorite_rows = session.scalars(
                select(CandidateFavorite).where(
                    CandidateFavorite.candidate_id == candidate_id
                )
            ).all()
        assert {row.user_id for row in favorite_rows} == {owner_id, member_id}
    finally:
        member_client.close()
