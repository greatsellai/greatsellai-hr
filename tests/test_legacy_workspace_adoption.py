from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select

from app.config import AppSettings
from app.database import Base
from app.main import create_app
from app.models import (
    Candidate,
    CandidateDataFileAccessGrant,
    CandidateDataRetentionPolicy,
    LegacyWorkspaceAdoption,
    MailboxOAuthConnectIntent,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    PlatformAuditEvent,
    RecruitingAgentConversation,
    Resume,
    OrganizationScoped,
    UserAccount,
)
from app.services.identity_service import LEGACY_MEMBERSHIP_ID, LEGACY_USER_ID, utcnow
from app.tenant_scope import (
    LEGACY_ORGANIZATION_ID,
    SYSTEM_FALLBACK_ORGANIZATION_ID,
    organization_context_id,
    set_organization_context,
)


_SESSION_SECRET = "legacy-workspace-adoption-test-session-secret"
_LEGACY_MANAGEMENT_PASSWORD = "legacy-workspace-adoption-fixture-password"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret=_SESSION_SECRET,
        transactional_email_provider="test",
        public_app_url="http://testserver",
        admin_token=_LEGACY_MANAGEMENT_PASSWORD,
        legacy_admin_token_enabled=True,
        min_text_chars_per_page=20,
    )


def _register_and_verify(
    client: TestClient,
    *,
    organization_name: str,
    full_name: str,
    email: str,
    password: str,
) -> dict[str, object]:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": full_name,
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    return verified.json()


def _seed_legacy_workspace(client: TestClient) -> dict[str, str]:
    """Seed only opaque fixture IDs, never actual candidate source content."""

    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        candidate = Candidate(display_name="Historic workspace candidate fixture")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="historic-workspace-fixture.pdf",
            storage_key="historic-workspace-fixture.pdf",
            sha256="a" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="legacy-workspace-adoption-test",
            raw_text="synthetic fixture only",
            is_active=True,
        )
        session.add(resume)
        session.flush()
        conversation = RecruitingAgentConversation(
            owner_user_id=LEGACY_USER_ID,
            expires_at=utcnow() + timedelta(hours=1),
        )
        access_grant = CandidateDataFileAccessGrant(
            actor_user_id=LEGACY_USER_ID,
            resource_type="resume_original",
            resource_id=resume.id,
            purpose="view",
            token_digest="b" * 64,
            session_nonce_digest="c" * 64,
            resource_lifecycle_version=1,
            expires_at=utcnow() + timedelta(minutes=10),
        )
        oauth_intent = MailboxOAuthConnectIntent(
            user_id=LEGACY_USER_ID,
            membership_id=LEGACY_MEMBERSHIP_ID,
            provider_key="gmail_oauth",
            display_name="Historic OAuth fixture",
            email_address="historic-oauth-fixture@example.test",
            mailbox="INBOX",
            initial_sync_lookback_days=0,
            state_hash="d" * 64,
            encrypted_code_verifier="fixture-code-verifier",
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.add_all((conversation, access_grant, oauth_intent))
        session.commit()
        return {
            "candidate_id": candidate.id,
            "resume_id": resume.id,
            "conversation_id": conversation.id,
            "access_grant_id": access_grant.id,
            "oauth_intent_id": oauth_intent.id,
        }


def _old_boolean_session_cookie() -> str:
    payload = {
        "resume_v3_authenticated": True,
        "resume_v3_auth_session_version": 1,
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(_SESSION_SECRET).sign(encoded).decode("utf-8")


def _workspace_business_model_names(session, *, organization_id: str) -> list[str]:
    """Test the fail-closed empty-workspace premise against all scoped roots."""

    names: list[str] = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if (
            not isinstance(model, type)
            or not issubclass(model, OrganizationScoped)
            or model is CandidateDataRetentionPolicy
        ):
            continue
        organization_column = getattr(model, "organization_id", None)
        if organization_column is None:
            names.append(model.__name__)
            continue
        if session.scalar(
            select(organization_column)
            .where(organization_column == organization_id)
            .limit(1)
            .execution_options(skip_organization_scope=True)
        ) is not None:
            names.append(model.__name__)
    return sorted(names)


def test_verified_account_adopts_historic_workspace_without_cross_tenant_leak(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app):
        target_client = TestClient(app)
        other_client = TestClient(app)
        anonymous_client = TestClient(app)
        try:
            resources = _seed_legacy_workspace(target_client)

            # The old boolean cookie, password-only login body, and header
            # cannot open the historic workspace even before it is adopted.
            anonymous_client.cookies.set(
                "resume_v3_session",
                _old_boolean_session_cookie(),
                domain="testserver.local",
                path="/",
            )
            assert anonymous_client.get("/v1/resume-library").status_code == 401
            assert (
                anonymous_client.post(
                    "/v1/auth/login",
                    json={"password": _LEGACY_MANAGEMENT_PASSWORD},
                ).status_code
                == 422
            )
            assert (
                anonymous_client.get(
                    "/v1/resume-library",
                    headers={"x-admin-token": _LEGACY_MANAGEMENT_PASSWORD},
                ).status_code
                == 401
            )

            target_session = _register_and_verify(
                target_client,
                organization_name="Migrated recruiting workspace",
                full_name="Migration target",
                email="legacy-adoption-target@example.test",
                password="legacy-adoption-target-password",
            )
            target_user_id = str(target_session["user"]["user_id"])
            target_previous_organization_id = str(
                target_session["organization"]["organization_id"]
            )

            _register_and_verify(
                other_client,
                organization_name="Separate recruiting workspace",
                full_name="Separate owner",
                email="legacy-adoption-other@example.test",
                password="legacy-adoption-other-password",
            )

            available = target_client.get("/v1/auth/legacy-workspace-adoption")
            assert available.status_code == 200, available.text
            assert available.json() == {"available": True}

            with app.state.database.session_factory() as session:
                assert _workspace_business_model_names(
                    session,
                    organization_id=target_previous_organization_id,
                ) == []

            rejected = target_client.post(
                "/v1/auth/legacy-workspace-adoption",
                json={"legacy_admin_password": "wrong-password"},
            )
            assert rejected.status_code == 401, rejected.text
            assert rejected.json()["detail"] == "legacy_workspace_adoption_not_authorized"

            adopted = target_client.post(
                "/v1/auth/legacy-workspace-adoption",
                json={"legacy_admin_password": _LEGACY_MANAGEMENT_PASSWORD},
            )
            assert adopted.status_code == 200, adopted.text
            adopted_payload = adopted.json()
            assert adopted_payload["authenticated"] is True
            assert adopted_payload["organization"] == {
                "organization_id": LEGACY_ORGANIZATION_ID,
                "name": "Migrated recruiting workspace",
            }
            assert adopted_payload["user"]["user_id"] == target_user_id

            assert target_client.get(f"/v1/resumes/{resources['resume_id']}").status_code == 200
            foreign_read = other_client.get(f"/v1/resumes/{resources['resume_id']}")
            assert foreign_read.status_code == 404, foreign_read.text

            # A retry is idempotent for the same signed-in account; it never
            # creates a second membership or copies any business row.
            retry = target_client.post(
                "/v1/auth/legacy-workspace-adoption",
                json={"legacy_admin_password": _LEGACY_MANAGEMENT_PASSWORD},
            )
            assert retry.status_code == 200, retry.text
            assert retry.json()["organization"]["organization_id"] == LEGACY_ORGANIZATION_ID

            database = app.state.database
            with database.session_factory() as session:
                assert organization_context_id(session) == SYSTEM_FALLBACK_ORGANIZATION_ID
                adoption = session.scalar(select(LegacyWorkspaceAdoption))
                assert adoption is not None
                assert adoption.target_user_id == target_user_id
                assert adoption.target_previous_organization_id == target_previous_organization_id

                source_user = session.get(UserAccount, LEGACY_USER_ID)
                source_membership = session.get(OrganizationMembership, LEGACY_MEMBERSHIP_ID)
                target_previous_organization = session.get(
                    Organization,
                    target_previous_organization_id,
                )
                assert source_user is not None and source_user.is_active is False
                assert source_membership is not None and source_membership.is_active is False
                assert target_previous_organization is not None
                assert target_previous_organization.plan_status == "suspended"

                active_target_memberships = session.scalars(
                    select(OrganizationMembership).where(
                        OrganizationMembership.user_id == target_user_id,
                        OrganizationMembership.is_active.is_(True),
                    )
                ).all()
                assert [membership.organization_id for membership in active_target_memberships] == [
                    LEGACY_ORGANIZATION_ID
                ]

                set_organization_context(session, LEGACY_ORGANIZATION_ID)
                conversation = session.get(
                    RecruitingAgentConversation,
                    resources["conversation_id"],
                )
                grant = session.get(
                    CandidateDataFileAccessGrant,
                    resources["access_grant_id"],
                )
                intent = session.get(
                    MailboxOAuthConnectIntent,
                    resources["oauth_intent_id"],
                )
                assert conversation is not None and conversation.owner_user_id == target_user_id
                assert grant is not None and grant.revoked_at is not None
                assert intent is not None and intent.consumed_at is not None
                assert session.scalar(
                    select(PlatformAuditEvent.id).where(
                        PlatformAuditEvent.action == "legacy_workspace.adopted",
                        PlatformAuditEvent.actor_user_id == target_user_id,
                    )
                ) is not None
        finally:
            anonymous_client.close()
            other_client.close()
            target_client.close()


def test_adoption_rejects_a_target_workspace_that_already_has_business_data(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        target_session = _register_and_verify(
            client,
            organization_name="Nonempty migration target",
            full_name="Nonempty target",
            email="legacy-adoption-nonempty@example.test",
            password="legacy-adoption-nonempty-password",
        )
        target_organization_id = str(target_session["organization"]["organization_id"])
        created_candidate = client.post(
            "/v1/candidates",
            json={"display_name": "Existing target workspace candidate"},
        )
        assert created_candidate.status_code == 200, created_candidate.text

        rejected = client.post(
            "/v1/auth/legacy-workspace-adoption",
            json={"legacy_admin_password": _LEGACY_MANAGEMENT_PASSWORD},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"] == "legacy_workspace_adoption_target_workspace_not_empty"

        database = app.state.database
        with database.session_factory() as session:
            assert session.scalar(select(LegacyWorkspaceAdoption)) is None
            legacy_membership = session.get(OrganizationMembership, LEGACY_MEMBERSHIP_ID)
            assert legacy_membership is not None and legacy_membership.is_active is True
            target_membership = session.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == target_organization_id,
                    OrganizationMembership.is_active.is_(True),
                )
            )
            assert target_membership is not None


def test_adoption_refuses_a_source_workspace_with_existing_access_paths(
    tmp_path: Path,
) -> None:
    """Never guess which historic member or invite should lose access."""

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        target_session = _register_and_verify(
            client,
            organization_name="Conflict migration target",
            full_name="Conflict target",
            email="legacy-adoption-conflict@example.test",
            password="legacy-adoption-conflict-password",
        )
        target_organization_id = str(target_session["organization"]["organization_id"])

        database = app.state.database
        with database.session_factory() as session:
            existing_member = UserAccount(
                email="historic-member@example.test",
                email_key="historic-member@example.test",
                full_name="Historic workspace member",
                password_hash="not-used-by-this-test",
                email_verified_at=utcnow(),
            )
            session.add(existing_member)
            session.flush()
            existing_membership = OrganizationMembership(
                organization_id=LEGACY_ORGANIZATION_ID,
                user_id=existing_member.id,
                role="recruiter",
                is_active=True,
            )
            existing_invitation = OrganizationInvitation(
                organization_id=LEGACY_ORGANIZATION_ID,
                email_key="pending-historic-member@example.test",
                token_digest="f" * 64,
                role="recruiter",
                expires_at=utcnow() + timedelta(days=1),
                created_by_user_id=LEGACY_USER_ID,
            )
            session.add_all((existing_membership, existing_invitation))
            session.commit()
            existing_membership_id = existing_membership.id
            existing_invitation_id = existing_invitation.id

        rejected = client.post(
            "/v1/auth/legacy-workspace-adoption",
            json={"legacy_admin_password": _LEGACY_MANAGEMENT_PASSWORD},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"] == "legacy_workspace_adoption_source_workspace_not_ready"

        with database.session_factory() as session:
            assert session.scalar(select(LegacyWorkspaceAdoption)) is None
            legacy_user = session.get(UserAccount, LEGACY_USER_ID)
            legacy_membership = session.get(OrganizationMembership, LEGACY_MEMBERSHIP_ID)
            source_membership = session.get(OrganizationMembership, existing_membership_id)
            source_invitation = session.get(OrganizationInvitation, existing_invitation_id)
            target_membership = session.scalar(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == target_organization_id,
                    OrganizationMembership.is_active.is_(True),
                )
            )
            assert legacy_user is not None and legacy_user.is_active is True
            assert legacy_membership is not None and legacy_membership.is_active is True
            assert source_membership is not None and source_membership.is_active is True
            assert source_invitation is not None and source_invitation.accepted_at is None
            assert target_membership is not None
