from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import Candidate, Organization, OrganizationMembership, Resume, UserAccount
from app.services.identity_service import LEGACY_ORGANIZATION_ID, LEGACY_USER_ID
from app.services.platform_admin_service import PlatformAdminServiceError
from app.tenant_scope import set_organization_context


@pytest.fixture
def platform_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        admin_token="platform-console-test-token",
        session_secret="platform-console-session-secret",
        allow_unauthenticated=False,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _register_and_verify(
    client: TestClient,
    *,
    organization_name: str,
    full_name: str,
    email: str,
) -> dict[str, object]:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": full_name,
            "email": email,
            "password": "platform-console-user-password",
        },
    )
    assert registered.status_code == 201, registered.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post(
        "/v1/auth/email-verification/complete",
        json={"token": token},
    )
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["is_platform_admin"] is False
    assert client.post("/v1/auth/logout").status_code == 204
    return payload


def _login_platform_admin(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/v1/auth/login",
        json={"password": "platform-console-test-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["is_platform_admin"] is True
    return payload


def test_platform_permission_is_independent_from_workspace_plan_access(
    platform_client: TestClient,
) -> None:
    assert platform_client.get("/v1/platform/dashboard").status_code == 401

    _register_and_verify(
        platform_client,
        organization_name="Ordinary workspace",
        full_name="Ordinary admin",
        email="ordinary-admin@example.test",
    )
    ordinary_login = platform_client.post(
        "/v1/auth/login",
        json={
            "email": "ordinary-admin@example.test",
            "password": "platform-console-user-password",
        },
    )
    assert ordinary_login.status_code == 200
    assert ordinary_login.json()["is_platform_admin"] is False
    denied = platform_client.get("/v1/platform/dashboard")
    assert denied.status_code == 403
    assert denied.json()["detail"] == "platform_admin_required"
    assert platform_client.post("/v1/auth/logout").status_code == 204

    _login_platform_admin(platform_client)
    with platform_client.app.state.database.session_factory() as session:
        legacy = session.get(Organization, LEGACY_ORGANIZATION_ID)
        assert legacy is not None
        legacy.plan_status = "suspended"
        session.commit()

    dashboard = platform_client.get("/v1/platform/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    business_route = platform_client.get("/v1/resume-library")
    assert business_route.status_code == 402
    assert business_route.json()["detail"] == "trial_expired"


def test_platform_admin_cannot_open_another_workspace_resume_or_original(
    platform_client: TestClient,
) -> None:
    workspace = _register_and_verify(
        platform_client,
        organization_name="Private Resume Tenant",
        full_name="Private Resume Owner",
        email="private-resume@example.test",
    )
    organization_id = workspace["organization"]["organization_id"]
    with platform_client.app.state.database.session_factory() as session:
        set_organization_context(session, organization_id)
        candidate = Candidate(display_name="Synthetic Private Candidate")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="synthetic-private.pdf",
            storage_key=f"{organization_id}/synthetic-private.pdf",
            sha256="0" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="platform-isolation-test",
            is_active=True,
            raw_text="Synthetic fixture that must remain workspace scoped.",
        )
        session.add(resume)
        session.commit()
        resume_id = resume.id

    _login_platform_admin(platform_client)
    review = platform_client.get(f"/v1/resumes/{resume_id}")
    assert review.status_code == 404, review.text
    original = platform_client.get(f"/v1/resumes/{resume_id}/original-file")
    assert original.status_code == 404, original.text


def test_dashboard_organization_management_and_audit_are_safe_and_atomic(
    platform_client: TestClient,
) -> None:
    workspace = _register_and_verify(
        platform_client,
        organization_name="Searchable Tenant",
        full_name="Tenant Owner",
        email="tenant-owner@example.test",
    )
    organization_id = workspace["organization"]["organization_id"]
    with platform_client.app.state.database.session_factory() as session:
        organization = session.get(Organization, organization_id)
        assert organization is not None
        organization.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()
    _login_platform_admin(platform_client)

    dashboard = platform_client.get("/v1/platform/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    dashboard_payload = dashboard.json()
    assert dashboard_payload["organizations_total"] == 2
    assert dashboard_payload["users_total"] == 2
    assert dashboard_payload["organizations_by_status"]["expired"] == 1
    assert dashboard_payload["resumes_total"] == 0
    assert dashboard_payload["ai_runs_total"] == 0

    listed = platform_client.get(
        "/v1/platform/organizations",
        params={"search": "searchable", "plan_code": "advanced"},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["organization_id"] == organization_id

    listed_by_id = platform_client.get(
        "/v1/platform/organizations",
        params={"search": organization_id[:12]},
    )
    assert listed_by_id.status_code == 200, listed_by_id.text
    assert listed_by_id.json()["total"] == 1
    assert listed_by_id.json()["items"][0]["organization_id"] == organization_id
    expired_list = platform_client.get(
        "/v1/platform/organizations",
        params={"plan_status": "expired"},
    )
    assert expired_list.status_code == 200, expired_list.text
    assert expired_list.json()["total"] == 1
    assert expired_list.json()["items"][0]["organization_id"] == organization_id

    detail = platform_client.get(f"/v1/platform/organizations/{organization_id}")
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert detail_payload["members"][0]["email"] == "tenant-owner@example.test"
    assert detail_payload["resume_count"] == 0
    serialized = detail.text.casefold()
    for forbidden in ("password_hash", "resume_text", "prompt", "credential_ref"):
        assert forbidden not in serialized

    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=14)
    confirmation_required = platform_client.patch(
        f"/v1/platform/organizations/{organization_id}",
        json={
            "plan_status": "suspended",
            "reason": "High-risk access change",
        },
    )
    assert confirmation_required.status_code == 422, confirmation_required.text
    assert (
        confirmation_required.json()["detail"]
        == "platform_organization_confirmation_required"
    )

    patched = platform_client.patch(
        f"/v1/platform/organizations/{organization_id}",
        headers={"X-Request-ID": "platform-request-001"},
        json={
            "name": "Renamed Tenant",
            "plan_code": "basic",
            "plan_status": "active",
            "trial_ends_at": trial_ends_at.isoformat(),
            "reason": "Customer support adjustment",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Renamed Tenant"
    assert patched.json()["plan_code"] == "basic"
    assert patched.json()["plan_status"] == "active"

    audit = platform_client.get(
        "/v1/platform/audit-events",
        params={"organization_id": organization_id, "action": "organization.updated"},
    )
    assert audit.status_code == 200, audit.text
    audit_payload = audit.json()
    assert audit_payload["total"] == 1
    event = audit_payload["items"][0]
    assert event["actor_user_id"] == LEGACY_USER_ID
    assert event["reason"] == "Customer support adjustment"
    assert event["request_id"] == "platform-request-001"
    assert event["before_state"]["name"] == "Searchable Tenant"
    assert event["after_state"]["name"] == "Renamed Tenant"
    assert "email" not in event["before_state"]

    plan_update = platform_client.put(
        "/v1/platform/plans/basic",
        json={"monthly_price_cents": 9900, "reason": "Pricing rollout"},
    )
    assert plan_update.status_code == 200, plan_update.text
    assert plan_update.json()["monthly_price_cents"] == 9900

    plan_assign = platform_client.put(
        f"/v1/platform/organizations/{organization_id}/plan",
        json={
            "plan_code": "advanced",
            "plan_status": "trial",
            "reason": "Restore trial plan",
        },
    )
    assert plan_assign.status_code == 200, plan_assign.text
    assert plan_assign.json()["plan_code"] == "advanced"
    all_audits = platform_client.get("/v1/platform/audit-events")
    assert all_audits.status_code == 200
    actions = {item["action"] for item in all_audits.json()["items"]}
    assert {
        "organization.updated",
        "product_plan.updated",
        "organization.plan_assigned",
    }.issubset(actions)


def test_platform_user_management_protects_platform_administrators(
    platform_client: TestClient,
) -> None:
    ordinary = _register_and_verify(
        platform_client,
        organization_name="User Management Tenant",
        full_name="Managed User",
        email="managed-user@example.test",
    )
    managed_user_id = ordinary["user"]["user_id"]
    other_admin = _register_and_verify(
        platform_client,
        organization_name="Other Platform Tenant",
        full_name="Other Platform Admin",
        email="other-platform@example.test",
    )
    other_admin_id = other_admin["user"]["user_id"]
    with platform_client.app.state.database.session_factory() as session:
        user = session.scalar(
            select(UserAccount).where(UserAccount.id == other_admin_id)
        )
        assert user is not None
        user.is_platform_admin = True
        session.commit()

    _login_platform_admin(platform_client)
    listed = platform_client.get(
        "/v1/platform/users",
        params={"search": "managed-user", "is_active": True},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["membership_count"] == 1

    listed_by_id = platform_client.get(
        "/v1/platform/users",
        params={"search": managed_user_id[:12]},
    )
    assert listed_by_id.status_code == 200, listed_by_id.text
    assert listed_by_id.json()["total"] == 1
    assert listed_by_id.json()["items"][0]["user_id"] == managed_user_id

    detail = platform_client.get(f"/v1/platform/users/{managed_user_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["memberships"][0]["organization_name"] == "User Management Tenant"
    assert "password_hash" not in detail.text

    disabled = platform_client.patch(
        f"/v1/platform/users/{managed_user_id}",
        json={"is_active": False, "reason": "Requested account suspension"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["is_active"] is False

    managed_organization_id = ordinary["organization"]["organization_id"]
    organization = platform_client.get(
        f"/v1/platform/organizations/{managed_organization_id}"
    )
    assert organization.status_code == 200, organization.text
    assert organization.json()["member_count"] == 1
    assert organization.json()["active_member_count"] == 0

    self_denied = platform_client.patch(
        f"/v1/platform/users/{LEGACY_USER_ID}",
        json={"is_active": False, "reason": "Unsafe self disable"},
    )
    assert self_denied.status_code == 403
    assert self_denied.json()["detail"] == "platform_admin_self_deactivation_forbidden"

    other_admin_denied = platform_client.patch(
        f"/v1/platform/users/{other_admin_id}",
        json={"is_active": False, "reason": "Unsafe admin disable"},
    )
    assert other_admin_denied.status_code == 403
    assert other_admin_denied.json()["detail"] == "platform_admin_deactivation_forbidden"

    audit = platform_client.get(
        "/v1/platform/audit-events",
        params={"target_type": "user", "action": "user.activation_changed"},
    )
    assert audit.status_code == 200, audit.text
    assert audit.json()["total"] == 1
    assert audit.json()["items"][0]["target_id"] == managed_user_id
    assert audit.json()["items"][0]["before_state"]["is_active"] is True
    assert audit.json()["items"][0]["after_state"]["is_active"] is False


def test_every_platform_endpoint_rejects_anonymous_and_tenant_roles(
    platform_client: TestClient,
) -> None:
    ordinary = _register_and_verify(
        platform_client,
        organization_name="Permission Matrix Tenant",
        full_name="Permission Matrix User",
        email="permission-matrix@example.test",
    )
    organization_id = ordinary["organization"]["organization_id"]
    user_id = ordinary["user"]["user_id"]
    now = datetime.now(timezone.utc).isoformat()
    cases: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", "/v1/platform/dashboard", None),
        ("GET", "/v1/platform/organizations", None),
        ("GET", f"/v1/platform/organizations/{organization_id}", None),
        (
            "PATCH",
            f"/v1/platform/organizations/{organization_id}",
            {"name": "Denied Rename", "reason": "permission test"},
        ),
        ("GET", "/v1/platform/users", None),
        ("GET", f"/v1/platform/users/{user_id}", None),
        (
            "PATCH",
            f"/v1/platform/users/{user_id}",
            {"is_active": False, "reason": "permission test"},
        ),
        ("GET", "/v1/platform/audit-events", None),
        ("GET", "/v1/platform/plans", None),
        (
            "PUT",
            "/v1/platform/plans/basic",
            {"monthly_price_cents": 100, "reason": "permission test"},
        ),
        (
            "PUT",
            f"/v1/platform/organizations/{organization_id}/plan",
            {"plan_code": "basic", "reason": "permission test"},
        ),
        ("GET", "/v1/platform/ai/providers", None),
        (
            "POST",
            "/v1/platform/ai/providers",
            {
                "slug": "denied-provider",
                "display_name": "Denied provider",
                "driver": "openai_compatible",
                "endpoint_url": "https://api.example.test/v1/chat/completions",
                "credential_ref": "denied-reference",
                "reason": "permission test",
            },
        ),
        ("GET", "/v1/platform/ai/models", None),
        (
            "POST",
            "/v1/platform/ai/models",
            {
                "slug": "denied-model",
                "provider_slug": "denied-provider",
                "display_name": "Denied model",
                "provider_model_id": "denied-model-id",
                "reason": "permission test",
            },
        ),
        ("GET", "/v1/platform/ai/model-prices", None),
        (
            "POST",
            "/v1/platform/ai/model-prices",
            {
                "model_slug": "denied-model",
                "effective_from": now,
                "source": "permission-test",
                "reason": "permission test",
            },
        ),
        ("GET", "/v1/platform/ai/routes", None),
        ("GET", "/v1/platform/ai/routes/resume_score/versions", None),
        (
            "PUT",
            "/v1/platform/ai/routes/resume_score",
            {
                "display_name": "Denied route",
                "targets": [{"model_slug": "denied-model"}],
                "reason": "permission test",
            },
        ),
        ("GET", "/v1/platform/ai/usage/runs", None),
        ("GET", "/v1/platform/ai/usage/summary", None),
    ]

    for method, path, body in cases:
        response = platform_client.request(method, path, json=body)
        assert response.status_code == 401, (method, path, response.text)

    for expected_role in ("admin", "recruiter"):
        login = platform_client.post(
            "/v1/auth/login",
            json={
                "email": "permission-matrix@example.test",
                "password": "platform-console-user-password",
            },
        )
        assert login.status_code == 200, login.text
        assert login.json()["role"] == expected_role
        for method, path, body in cases:
            response = platform_client.request(method, path, json=body)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["detail"] == "platform_admin_required"
        assert platform_client.post("/v1/auth/logout").status_code == 204
        if expected_role == "admin":
            with platform_client.app.state.database.session_factory() as session:
                membership = session.scalar(
                    select(OrganizationMembership).where(
                        OrganizationMembership.user_id == user_id
                    )
                )
                assert membership is not None
                membership.role = "recruiter"
                session.commit()


def test_platform_mutation_rolls_back_when_audit_append_fails(
    platform_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _register_and_verify(
        platform_client,
        organization_name="Atomic Audit Tenant",
        full_name="Atomic Audit Owner",
        email="atomic-audit@example.test",
    )
    organization_id = workspace["organization"]["organization_id"]
    _login_platform_admin(platform_client)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise PlatformAdminServiceError("forced_platform_audit_failure")

    monkeypatch.setattr(
        "app.services.platform_admin_service.record_platform_audit_event",
        fail_audit,
    )
    failed = platform_client.patch(
        f"/v1/platform/organizations/{organization_id}",
        json={"name": "Must Roll Back", "reason": "atomicity test"},
    )
    assert failed.status_code == 422, failed.text
    assert failed.json()["detail"] == "forced_platform_audit_failure"

    detail = platform_client.get(f"/v1/platform/organizations/{organization_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "Atomic Audit Tenant"
    audits = platform_client.get(
        "/v1/platform/audit-events",
        params={"organization_id": organization_id},
    )
    assert audits.status_code == 200
    assert audits.json()["total"] == 0
