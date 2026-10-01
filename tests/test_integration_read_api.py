from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.integration_router import router
from app.models import IntegrationRequestLease
from app.tenant_scope import set_organization_context
from test_integration_auth_helpers import make_context


def _client(context, *, settings=None) -> TestClient:
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = settings or context.settings
    app.include_router(router)
    return TestClient(app)


def test_rest_connection_uses_bearer_only_and_no_store(tmp_path):
    context = make_context(tmp_path)
    with _client(context) as client:
        anonymous = client.get("/v1/integrations/connection")
        client.cookies.set("resume_v3_session", "synthetic-cookie")
        cookie_only = client.get("/v1/integrations/connection")
        client.cookies.clear()
        connected = client.get(
            "/v1/integrations/connection",
            headers={"Authorization": f"Bearer {context.token}"},
        )
    assert anonymous.status_code == 401
    assert cookie_only.status_code == 401
    assert connected.status_code == 200
    assert connected.headers["cache-control"] == "no-store"
    assert connected.json()["audience"] == "rest"
    assert connected.json()["organization_id"] == context.organization_id


def test_rest_feature_off_and_unknown_search_fields_fail_closed(tmp_path):
    context = make_context(tmp_path)
    disabled = replace(context.settings, integrations_enabled=False)
    with _client(context, settings=disabled) as client:
        response = client.get(
            "/v1/integrations/connection",
            headers={"Authorization": f"Bearer {context.token}"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "integrations_disabled"

    with _client(context) as client:
        response = client.post(
            "/v1/integrations/candidates/search",
            headers={"Authorization": f"Bearer {context.token}"},
            json={"gender_in": ["female"], "age_min": 18, "organization_id": "foreign"},
        )
    assert response.status_code == 422


def test_failed_read_releases_its_concurrency_lease(tmp_path):
    context = make_context(tmp_path)
    with _client(context) as client:
        response = client.get(
            "/v1/integrations/candidates/00000000-0000-4000-8000-000000000099",
            headers={"Authorization": f"Bearer {context.token}"},
        )
    assert response.status_code == 404
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        leases = session.scalars(select(IntegrationRequestLease)).all()
    assert len(leases) == 1
    assert leases[0].released_at is not None

