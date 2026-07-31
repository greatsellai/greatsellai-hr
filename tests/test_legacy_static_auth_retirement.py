from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app.config import AppSettings
from app.main import create_app
from app.models import Candidate, Organization, OrganizationMembership, Resume, UserAccount
from app.services.identity_service import (
    RETIRED_LEGACY_MEMBERSHIP_ID,
    RETIRED_LEGACY_USER_ID,
    hash_password,
    principal_from_session,
)
from app.tenant_scope import (
    LEGACY_ORGANIZATION_ID,
    clear_organization_context,
    set_organization_context,
)


def _settings(tmp_path: Path) -> AppSettings:
    data_dir = tmp_path / "data"
    return AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        session_secret="legacy-static-auth-retirement-test-secret",
        allow_unauthenticated=False,
        transactional_email_provider="test",
        public_app_url="http://testserver",
        min_text_chars_per_page=20,
    )


def _set_signed_session_cookie(client: TestClient, values: dict[str, object]) -> None:
    encoded = base64.b64encode(json.dumps(values).encode("utf-8"))
    signed = TimestampSigner(client.app.state.settings.session_signing_secret()).sign(encoded)
    client.cookies.set("resume_v3_session", signed.decode("utf-8"))


def _register_verify_and_login(client: TestClient) -> None:
    email = "formal-account@example.test"
    password = "formal-account-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Formal account workspace",
            "full_name": "Formal account owner",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text

    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert client.post("/v1/auth/logout").status_code == 204

    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["authenticated"] is True


def _seed_active_retired_identity(app) -> None:
    """Create only synthetic historical rows to prove the session fails closed."""

    database = app.state.database
    with database.session_factory() as session:
        workspace = Organization(
            id=LEGACY_ORGANIZATION_ID,
            name="Retired shared workspace fixture",
            plan_status="active",
        )
        user = UserAccount(
            id=RETIRED_LEGACY_USER_ID,
            email="retired-shared-account@example.test",
            email_key="retired-shared-account@example.test",
            full_name="Retired shared account",
            password_hash="not-a-real-password-hash",
            auth_session_version=1,
            is_active=True,
            is_platform_admin=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        membership = OrganizationMembership(
            id=RETIRED_LEGACY_MEMBERSHIP_ID,
            organization_id=LEGACY_ORGANIZATION_ID,
            user_id=RETIRED_LEGACY_USER_ID,
            role="admin",
            is_active=True,
        )
        # Flush each synthetic identity root before inserting workspace-owned
        # rows. The fixture intentionally uses explicit foreign-key values
        # rather than ORM relationship assignment so it mirrors historic data.
        session.add(workspace)
        session.flush()
        session.add(user)
        session.flush()
        session.add(membership)
        session.flush()
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        try:
            candidate = Candidate(display_name="Synthetic legacy candidate")
            session.add(candidate)
            session.flush()
            session.add(
                Resume(
                    candidate_id=candidate.id,
                    original_filename="synthetic-legacy.pdf",
                    storage_key=f"{LEGACY_ORGANIZATION_ID}/synthetic-legacy.pdf",
                    sha256="a" * 64,
                    source_page_count=1,
                    parsed_page_count=1,
                    extraction_status="ready",
                    quality_flags=[],
                    parser_version="test-fixture",
                    is_active=True,
                    employment_months=0,
                    employment_or_internship_months=0,
                    facts_version=0,
                    raw_text="synthetic fixture only",
                )
            )
            session.commit()
        finally:
            clear_organization_context(session)

        assert principal_from_session(
            session,
            {
                "resume_v3_user_id": RETIRED_LEGACY_USER_ID,
                "resume_v3_organization_id": LEGACY_ORGANIZATION_ID,
                "resume_v3_membership_id": RETIRED_LEGACY_MEMBERSHIP_ID,
                "resume_v3_auth_session_version": 1,
            },
        ) is None


def _seed_adopted_workspace_switch_fixture(app) -> dict[str, str]:
    """Create a formal platform operator with two isolated workspaces."""

    database = app.state.database
    password = "adopted-workspace-password"
    with database.session_factory() as session:
        legacy_workspace = Organization(
            id=LEGACY_ORGANIZATION_ID,
            name="Retained historical workspace",
            plan_status="active",
        )
        home_workspace = Organization(
            id="adopted-home-workspace",
            name="Formal operator home workspace",
            plan_status="active",
        )
        foreign_workspace = Organization(
            id="adopted-foreign-workspace",
            name="Other operator workspace",
            plan_status="active",
        )
        formal_admin = UserAccount(
            id="adopted-formal-platform-admin",
            email="adopted-platform-admin@example.test",
            email_key="adopted-platform-admin@example.test",
            full_name="Adopted formal platform admin",
            password_hash=hash_password(password),
            is_active=True,
            is_platform_admin=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        foreign_user = UserAccount(
            id="adopted-foreign-user",
            email="adopted-foreign-user@example.test",
            email_key="adopted-foreign-user@example.test",
            full_name="Other workspace user",
            password_hash=hash_password("adopted-foreign-password"),
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        session.add_all(
            (
                legacy_workspace,
                home_workspace,
                foreign_workspace,
                formal_admin,
                foreign_user,
            )
        )
        session.flush()
        legacy_membership = OrganizationMembership(
            id="adopted-legacy-membership",
            organization_id=LEGACY_ORGANIZATION_ID,
            user_id=formal_admin.id,
            role="admin",
            is_active=True,
        )
        home_membership = OrganizationMembership(
            id="adopted-home-membership",
            organization_id=home_workspace.id,
            user_id=formal_admin.id,
            role="admin",
            is_active=True,
        )
        foreign_membership = OrganizationMembership(
            id="adopted-foreign-membership",
            organization_id=foreign_workspace.id,
            user_id=foreign_user.id,
            role="admin",
            is_active=True,
        )
        session.add_all((legacy_membership, home_membership, foreign_membership))
        session.flush()

        def seed_resume(*, organization_id: str, label: str) -> str:
            set_organization_context(session, organization_id)
            try:
                candidate = Candidate(display_name=f"Synthetic {label} candidate")
                session.add(candidate)
                session.flush()
                resume = Resume(
                    candidate_id=candidate.id,
                    original_filename=f"{label}.pdf",
                    storage_key=f"{organization_id}/{label}.pdf",
                    sha256=("b" if label == "legacy" else "c") * 64,
                    source_page_count=1,
                    parsed_page_count=1,
                    extraction_status="ready",
                    quality_flags=[],
                    parser_version="test-fixture",
                    is_active=True,
                    employment_months=0,
                    employment_or_internship_months=0,
                    facts_version=0,
                    raw_text="synthetic fixture only",
                )
                session.add(resume)
                session.flush()
                return resume.id
            finally:
                clear_organization_context(session)

        legacy_resume_id = seed_resume(
            organization_id=LEGACY_ORGANIZATION_ID,
            label="legacy",
        )
        home_resume_id = seed_resume(
            organization_id=home_workspace.id,
            label="home",
        )
        foreign_resume_id = seed_resume(
            organization_id=foreign_workspace.id,
            label="foreign",
        )
        session.commit()

    # The workspace-switch regression exercises the real server-side file
    # grant resolver, so provide opaque fixture bytes at the exact scoped
    # storage paths. Nothing here contains a real candidate record.
    for organization_id, label in (
        (LEGACY_ORGANIZATION_ID, "legacy"),
        ("adopted-home-workspace", "home"),
        ("adopted-foreign-workspace", "foreign"),
    ):
        original_path = app.state.settings.upload_dir / organization_id / f"{label}.pdf"
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(b"%PDF-1.4\nSynthetic workspace-switch fixture\n")

    return {
        "password": password,
        "legacy_membership_id": "adopted-legacy-membership",
        "home_membership_id": "adopted-home-membership",
        "foreign_membership_id": "adopted-foreign-membership",
        "home_organization_id": "adopted-home-workspace",
        "legacy_resume_id": legacy_resume_id,
        "home_resume_id": home_resume_id,
        "foreign_resume_id": foreign_resume_id,
    }


def test_protected_startup_does_not_create_retired_static_account(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app):
        with app.state.database.session_factory() as session:
            assert session.get(UserAccount, RETIRED_LEGACY_USER_ID) is None
            assert session.get(OrganizationMembership, RETIRED_LEGACY_MEMBERSHIP_ID) is None
            assert session.get(Organization, LEGACY_ORGANIZATION_ID) is None


def test_legacy_http_inputs_are_rejected_but_formal_account_still_works(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        _seed_active_retired_identity(app)

        # The former password-only payload cannot be parsed as a login.
        password_only = client.post(
            "/v1/auth/login",
            json={"password": "synthetic-historical-static-password"},
        )
        assert password_only.status_code == 422

        # The former boolean cookie did not identify a formal account and is
        # no longer enough to access either an auth session or business data.
        _set_signed_session_cookie(
            client,
            {
                "resume_v3_authenticated": True,
                "resume_v3_auth_session_version": 1,
            },
        )
        old_boolean_session = client.get("/v1/auth/session")
        assert old_boolean_session.status_code == 200
        assert old_boolean_session.json()["authenticated"] is False
        assert client.get("/v1/resume-library").status_code == 401

        client.cookies.clear()
        # A complete signed cookie for the physically present retired rows is
        # rejected before any active membership can turn it into a principal.
        _set_signed_session_cookie(
            client,
            {
                "resume_v3_user_id": RETIRED_LEGACY_USER_ID,
                "resume_v3_organization_id": LEGACY_ORGANIZATION_ID,
                "resume_v3_membership_id": RETIRED_LEGACY_MEMBERSHIP_ID,
                "resume_v3_auth_session_version": 1,
            },
        )
        assert client.get("/v1/auth/session").json()["authenticated"] is False
        assert client.get("/v1/resume-library").status_code == 401

        client.cookies.clear()
        header_attempt = client.get(
            "/v1/resume-library",
            headers={"X-Admin-Token": "synthetic-historical-static-password"},
        )
        assert header_attempt.status_code == 401
        assert header_attempt.json()["detail"] == "authentication_required"

        _register_verify_and_login(client)
        assert client.get("/v1/resume-library").status_code == 200


def test_adopted_platform_admin_switches_only_its_own_active_membership(
    tmp_path: Path,
) -> None:
    """Historic access stays usable without making membership IDs global selectors."""

    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        fixture = _seed_adopted_workspace_switch_fixture(app)

        logged_in = client.post(
            "/v1/auth/login",
            json={
                "email": "adopted-platform-admin@example.test",
                "password": fixture["password"],
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        # The adoption-specific default preserves immediate access to historic
        # candidates even when the formal platform user also owns a workspace.
        assert logged_in.json()["organization"]["organization_id"] == LEGACY_ORGANIZATION_ID
        cookie_before_switch = client.cookies.get("resume_v3_session")

        available = client.get("/v1/auth/workspaces")
        assert available.status_code == 200, available.text
        assert {item["membership_id"] for item in available.json()["items"]} == {
            fixture["legacy_membership_id"],
            fixture["home_membership_id"],
        }

        old_grant = client.post(
            f"/v1/resumes/{fixture['legacy_resume_id']}/file-access",
            json={"purpose": "view"},
        )
        assert old_grant.status_code == 200, old_grant.text
        assert client.get(old_grant.json()["access_url"]).status_code == 200

        switched = client.post(
            f"/v1/auth/workspaces/{fixture['home_membership_id']}/switch"
        )
        assert switched.status_code == 200, switched.text
        assert switched.json()["organization"]["organization_id"] == fixture["home_organization_id"]
        assert client.cookies.get("resume_v3_session") != cookie_before_switch
        # A grant is tied both to the account and this browser-session nonce,
        # so changing workspace must not leave a usable legacy-file URL.
        revoked_after_switch = client.get(old_grant.json()["access_url"])
        assert revoked_after_switch.status_code == 404
        assert revoked_after_switch.json()["detail"] == "candidate_data_file_access_not_found"

        # A real membership ID belonging to another user remains indistinguish-
        # able from an unknown selector and cannot replace the current session.
        cross_user_switch = client.post(
            f"/v1/auth/workspaces/{fixture['foreign_membership_id']}/switch"
        )
        assert cross_user_switch.status_code == 404, cross_user_switch.text
        assert cross_user_switch.json()["detail"] == "workspace_membership_not_found"
        current = client.get("/v1/auth/session")
        assert current.json()["organization"]["organization_id"] == fixture["home_organization_id"]

        home_library = client.get("/v1/resume-library")
        assert home_library.status_code == 200, home_library.text
        assert {item["resume_id"] for item in home_library.json()["items"]} == {
            fixture["home_resume_id"]
        }

        switched_back = client.post(
            f"/v1/auth/workspaces/{fixture['legacy_membership_id']}/switch"
        )
        assert switched_back.status_code == 200, switched_back.text
        legacy_library = client.get("/v1/resume-library")
        assert legacy_library.status_code == 200, legacy_library.text
        assert {item["resume_id"] for item in legacy_library.json()["items"]} == {
            fixture["legacy_resume_id"]
        }
        assert fixture["foreign_resume_id"] not in {
            item["resume_id"] for item in legacy_library.json()["items"]
        }
