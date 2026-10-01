from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from app.models import IntegrationAuditEvent, IntegrationGrant, Organization, OrganizationMembership, UserAccount
from app.services.integration_auth_service import IntegrationAccessError, authenticate_integration_token
from test_integration_auth_helpers import browser_client, csrf_headers, make_context


def test_cookie_management_csrf_one_time_token_rotation_and_activity(tmp_path):
    ctx = make_context(tmp_path)
    with browser_client(ctx) as client:
        headers = csrf_headers(client)
        initial = client.get("/v1/integration-settings")
        assert initial.headers["cache-control"] == "no-store"
        body = initial.json()
        assert body["features"] == {"api": True, "mcp": False, "analyses": False, "oauth": False}
        assert body["endpoints"]["mcp_url"] == "http://testserver/v1/mcp"
        assert "evidence:read" in body["workspace"]["available_scopes"]
        assert "analyses:read" not in body["workspace"]["available_scopes"]
        assert body["logout_revokes_connections"] is True
        assert ctx.token not in initial.text
        assert client.post("/v1/integration-settings/grants", json={"name": "Synthetic"}).status_code == 403
        assert client.post("/v1/integration-settings/grants", headers={**headers, "Origin": "https://foreign.example.test"}, json={"name": "Synthetic"}).status_code == 403
        assert client.get("/v1/integration-settings", headers={"Authorization": f"Bearer {ctx.token}"}).status_code == 401
        created = client.post("/v1/integration-settings/grants", headers=headers, json={"name": "Synthetic"})
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        grant_id = created.json()["grant"]["id"]
        assert token not in client.get("/v1/integration-settings").text
        rotated = client.post(f"/v1/integration-settings/grants/{grant_id}/rotate", headers=headers, json={})
        assert rotated.status_code == 200, rotated.text
        with ctx.database.session_factory() as session:
            with pytest.raises(IntegrationAccessError):
                authenticate_integration_token(session, token=token, audience="rest", settings=ctx.settings)
            assert authenticate_integration_token(session, token=rotated.json()["token"], audience="rest", settings=ctx.settings)
        assert client.delete(f"/v1/integration-settings/grants/{grant_id}", headers=headers).status_code == 204
        activity = client.get("/v1/integration-settings/activity?limit=2")
        assert activity.status_code == 200 and len(activity.json()["items"]) == 2
        assert activity.json()["next_cursor"]
        assert token not in activity.text and "token_digest" not in activity.text and "resource_id" not in activity.text
    ctx.database.dispose()


def test_disabled_and_expired_users_can_inspect_and_revoke_not_issue(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.database.session_factory() as session:
        session.execute(update(Organization).where(Organization.id == ctx.organization_id).values(plan_status="expired"))
        session.commit()
    with browser_client(ctx, settings=replace(ctx.settings, integrations_enabled=False)) as client:
        headers = csrf_headers(client)
        assert client.get("/v1/integration-settings").json()["permissions"]["can_create"] is False
        assert client.post("/v1/integration-settings/grants", headers=headers, json={"name": "Synthetic"}).status_code in {402, 403}
        assert client.post(f"/v1/integration-settings/grants/{ctx.grant_id}/rotate", headers=headers, json={}).status_code in {402, 403}
        assert client.delete(f"/v1/integration-settings/grants/{ctx.grant_id}", headers=headers).status_code == 204
    ctx.database.dispose()


def test_issuance_validates_unknown_fields_disabled_scopes_and_expiration(tmp_path):
    ctx = make_context(tmp_path)
    with browser_client(ctx) as client:
        headers = csrf_headers(client)
        for payload in ({"name": "Synthetic", "organization_id": ctx.organization_id},
                        {"name": "Synthetic", "expires_in_days": 91},
                        {"name": "Synthetic", "expires_in_days": True}):
            assert client.post("/v1/integration-settings/grants", headers=headers, json=payload).status_code == 422
        for payload in ({"name": "Synthetic", "audience": "mcp"},
                        {"name": "Synthetic", "scopes": ["analyses:write"]}):
            assert client.post("/v1/integration-settings/grants", headers=headers, json=payload).status_code == 403
        assert client.post("/v1/integration-settings/grants", headers=headers, json={"name": "Synthetic", "expires_in_days": 90}).status_code == 201
    ctx.database.dispose()


def test_audit_failure_rolls_back_issuance_and_never_returns_token(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    from app.services import integration_management_service
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(integration_management_service, "record_integration_audit", fail)
    with browser_client(ctx) as client:
        response = client.post("/v1/integration-settings/grants", headers=csrf_headers(client), json={"name": "Synthetic blocked creation"})
        assert response.status_code == 503
        assert "gs_pat_" not in response.text and "Synthetic blocked creation" not in response.text
    with ctx.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IntegrationGrant.__table__)) == 1
    ctx.database.dispose()


def test_same_workspace_other_member_cannot_manage_owner_grants(tmp_path):
    from app.services.identity_service import AuthPrincipal, establish_session, hash_password
    from app.services.integration_auth_service import integration_now
    from test_integration_auth_helpers import named_auth
    ctx = make_context(tmp_path)
    with ctx.database.session_factory() as session:
        owner = named_auth(session, ctx)
        user = UserAccount(email="synthetic-colleague@example.test", email_key="synthetic-colleague@example.test",
            full_name="Synthetic colleague", password_hash=hash_password("synthetic-only-password"),
            email_verified_at=integration_now(), is_active=True)
        session.add(user)
        session.flush()
        membership = OrganizationMembership(organization_id=ctx.organization_id, user_id=user.id, role="recruiter", is_active=True)
        session.add(membership)
        session.commit()
        values = {}
        establish_session(values, AuthPrincipal(user=user, membership=membership, organization=owner.organization, plan=owner.plan))
    with browser_client(ctx, values=values) as client:
        headers = csrf_headers(client)
        assert client.get("/v1/integration-settings").json()["grants"] == []
        assert client.get("/v1/integration-settings/activity").json()["items"] == []
        assert client.get("/v1/integration-settings/workspace/grants").status_code == 403
        assert client.post(f"/v1/integration-settings/grants/{ctx.grant_id}/rotate", headers=headers, json={}).status_code == 404
        assert client.delete(f"/v1/integration-settings/grants/{ctx.grant_id}", headers=headers).status_code == 404
        assert client.delete(f"/v1/integration-settings/workspace/grants/{ctx.grant_id}", headers=headers).status_code == 403
    ctx.database.dispose()

