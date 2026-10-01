"""Exercise the real application factory, not a router-only browser mock."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import UserAccount
from app.services.integration_auth_service import integration_now


def settings_for(tmp_path, **overrides):
    return AppSettings(project_dir=tmp_path, data_dir=tmp_path / "data", upload_dir=tmp_path / "uploads",
        database_url=f"sqlite:///{tmp_path / 'integration-app.sqlite'}",
        session_secret="synthetic-app-integration-session-secret",
        public_app_url="http://testserver", transactional_email_provider="test",
        **overrides)


def register_named_test_account(client, app):
    response = client.post("/v1/auth/register", json={"organization_name": "Synthetic connection team",
        "full_name": "Synthetic recruiter", "email": "app-integration@example.test",
        "password": "synthetic-test-password"})
    assert response.status_code == 201
    with app.state.database.session_factory() as session:
        user = session.scalar(select(UserAccount).where(UserAccount.email == "app-integration@example.test"))
        assert user is not None
        user.email_verified_at = integration_now()
        session.commit()


def test_real_app_never_inherits_legacy_unauthenticated_principal(tmp_path):
    app = create_app(settings_for(tmp_path, allow_unauthenticated=True, integrations_enabled=True))
    with TestClient(app) as client:
        response = client.get("/v1/integration-settings")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/health").json() == {"status": "ok"}


def test_real_app_cookie_csrf_issue_revoke_and_bearer_boundary(tmp_path):
    app = create_app(settings_for(tmp_path, allow_unauthenticated=False, integrations_enabled=True))
    with TestClient(app) as client:
        register_named_test_account(client, app)
        overview = client.get("/v1/integration-settings")
        assert overview.status_code == 200
        assert overview.json()["workspace"]["enabled"] is False
        headers = {"Origin": "http://testserver", "X-CSRF-Token": overview.json()["csrf_token"]}
        policy = {"enabled": True, "allowed_scopes": ["candidates:read", "jobs:read", "assessments:read"]}
        assert client.patch("/v1/integration-settings/workspace", json=policy).status_code == 403
        assert client.patch("/v1/integration-settings/workspace", json=policy, headers=headers).status_code == 200
        created = client.post("/v1/integration-settings/grants", json={"name": "Synthetic connection"}, headers=headers)
        assert created.status_code == 201
        token = created.json()["token"]
        grant_id = created.json()["grant"]["id"]
        assert created.headers["cache-control"] == "no-store"
        assert token not in client.get("/v1/integration-settings").text
        assert client.get("/v1/integration-settings", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        # An authenticated browser cookie alone never authorizes external reads.
        assert client.get("/v1/integrations/connection").status_code == 401
        connected = client.get("/v1/integrations/connection", headers={"Authorization": f"Bearer {token}"})
        assert connected.status_code == 200
        assert connected.json()["audience"] == "rest"
        assert connected.headers["cache-control"] == "no-store"
        assert client.delete(f"/v1/integration-settings/grants/{grant_id}", headers=headers).status_code == 204
        assert client.get("/v1/integrations/connection", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/v1/integration-settings").json()["grants"][0]["status"] == "revoked"


def test_real_app_global_off_is_visible_but_cannot_issue_credentials(tmp_path):
    app = create_app(settings_for(tmp_path, allow_unauthenticated=False))
    with TestClient(app) as client:
        register_named_test_account(client, app)
        overview = client.get("/v1/integration-settings").json()
        assert not any(overview["features"].values())
        assert overview["permissions"]["can_create"] is False
        headers = {"Origin": "http://testserver", "X-CSRF-Token": overview["csrf_token"]}
        assert client.post("/v1/integration-settings/grants", json={"name": "not-enabled"}, headers=headers).status_code == 403
        assert client.post("/v1/mcp", json={}).status_code == 404


def test_real_app_mcp_exact_route_lifespan_discovery_and_revocation(tmp_path):
    app = create_app(settings_for(tmp_path, allow_unauthenticated=False,
        integrations_enabled=True, integrations_mcp_enabled=True))
    with TestClient(app) as client:
        register_named_test_account(client, app)
        overview = client.get("/v1/integration-settings").json()
        browser_headers = {"Origin": "http://testserver", "X-CSRF-Token": overview["csrf_token"]}
        assert client.patch("/v1/integration-settings/workspace", headers=browser_headers,
            json={"enabled": True, "allowed_scopes": ["candidates:read", "jobs:read", "assessments:read"]}).status_code == 200
        issued = client.post("/v1/integration-settings/grants", headers=browser_headers,
            json={"name": "Synthetic MCP", "audience": "mcp"}).json()
        headers = {"Authorization": f"Bearer {issued['token']}", "Accept": "application/json, text/event-stream"}
        assert client.post("/v1/mcp", json={}).status_code == 401
        discovery = client.get("/.well-known/oauth-protected-resource/v1/mcp")
        assert discovery.status_code == 200
        assert discovery.json()["resource"] == "http://testserver/v1/mcp"
        initialized = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "synthetic-app-wiring", "version": "1"}}})
        assert initialized.status_code == 200, initialized.text
        assert "result" in initialized.json()
        connected = client.post("/v1/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2,
            "method": "tools/call", "params": {"name": "get_connection_info", "arguments": {}}})
        assert connected.status_code == 200, connected.text
        assert connected.json()["result"]["structuredContent"]["audience"] == "mcp"
        assert client.get("/v1/integrations/connection", headers=headers).status_code == 401
        assert client.get("/health").status_code == 200
        assert client.delete(f"/v1/integration-settings/grants/{issued['grant']['id']}", headers=browser_headers).status_code == 204
        assert client.post("/v1/mcp", headers=headers, json={}).status_code == 401
    assert app.state.integration_mcp_app is None


def test_real_app_oauth_issues_mcp_scoped_access_and_revokes_it(tmp_path):
    from test_integration_oauth import issue
    app = create_app(settings_for(tmp_path, allow_unauthenticated=False,
        integrations_enabled=True, integrations_mcp_enabled=True, integrations_oauth_enabled=True))
    # Test-only routers added by local fixture launchers remain reachable.
    @app.get("/__synthetic_probe__")
    def probe():
        return {"synthetic": True}

    with TestClient(app) as client:
        assert client.get("/__synthetic_probe__").status_code == 200
        register_named_test_account(client, app)
        overview = client.get("/v1/integration-settings").json()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": overview["csrf_token"]}
        assert client.patch("/v1/integration-settings/workspace", headers=headers,
            json={"enabled": True, "allowed_scopes": ["candidates:read", "jobs:read", "assessments:read"]}).status_code == 200
        client_id, token, _code = issue(client)
        mcp_headers = {"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json, text/event-stream"}
        connected = client.post("/v1/mcp", headers=mcp_headers, json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": "get_connection_info", "arguments": {}}})
        assert connected.status_code == 200
        assert connected.json()["result"]["structuredContent"]["scopes"] == ["candidates:read"]
        assert client.get("/v1/integrations/connection", headers=mcp_headers).status_code == 401
        assert client.post("/v1/oauth/revoke", data={"client_id": client_id, "token": token["refresh_token"]}).status_code == 200
        assert client.post("/v1/mcp", headers=mcp_headers, json={}).status_code == 401


def test_real_app_private_draft_browser_route_is_cookie_only_and_gated(tmp_path):
    app = create_app(settings_for(tmp_path, allow_unauthenticated=False,
        integrations_enabled=True, integrations_analysis_enabled=True))
    with TestClient(app) as client:
        route = "/v1/integration-settings/analysis-reports"
        assert client.get(route).status_code == 401
        register_named_test_account(client, app)
        overview = client.get("/v1/integration-settings").json()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": overview["csrf_token"]}
        policy = {"enabled": True, "allowed_scopes": ["candidates:read", "jobs:read",
            "assessments:read", "analyses:read", "analyses:write"]}
        assert client.patch("/v1/integration-settings/workspace", json=policy, headers=headers).status_code == 200
        listed = client.get(route)
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        assert listed.json() == {"items": [], "next_cursor": None}
        assert client.get(route, headers={"Authorization": "Bearer synthetic-invalid"}).status_code == 401
        assert client.get(f"{route}/missing").status_code == 404

    app = create_app(settings_for(tmp_path, allow_unauthenticated=False,
        integrations_enabled=True, integrations_analysis_enabled=False))
    with TestClient(app) as client:
        assert client.post("/v1/auth/login", json={"email": "app-integration@example.test",
            "password": "synthetic-test-password"}).status_code == 200
        disabled = client.get("/v1/integration-settings/analysis-reports")
        assert disabled.status_code == 403
        assert disabled.json()["detail"] == "integrations_disabled"

