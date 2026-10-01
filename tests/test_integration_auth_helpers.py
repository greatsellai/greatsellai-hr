"""Synthetic named-account fixtures shared by integration authorization tests."""
from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from app.config import AppSettings
from app.database import Database
from app.integration_schemas import IntegrationGrantCreate, IntegrationPolicyPatch
from app.integration_settings_router import router
from app.schemas import AuthRegistration
from app.services.identity_service import (
    create_registration, ensure_identity_bootstrap, establish_session, principal_from_session,
)
from app.services.integration_auth_service import integration_now
from app.services.integration_management_service import create_integration_grant, update_integration_policy


def make_context(tmp_path, *, database_url="sqlite://", **overrides):
    settings = AppSettings(project_dir=tmp_path, data_dir=tmp_path / "data", upload_dir=tmp_path / "uploads",
        database_url=database_url, integrations_enabled=True, public_app_url="http://testserver",
        session_secret="synthetic-integration-session-secret", **overrides)
    database = Database(database_url, pool_size=10, max_overflow=10)
    database.create_all()
    with database.session_factory() as session:
        ensure_identity_bootstrap(session, create_development_identity=False)
        session.commit()
        auth = create_registration(session, AuthRegistration(organization_name="Synthetic integration workspace",
            full_name="Synthetic account", email=f"integration-{uuid4()}@example.test", password="synthetic-only-password"))
        now = integration_now()
        auth.user.email_verified_at = now
        auth.organization.trial_started_at = now - timedelta(days=1)
        auth.organization.trial_ends_at = now + timedelta(days=30)
        session.commit()
        values = {}
        establish_session(values, auth)
        update_integration_policy(session, auth, settings=settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=["candidates:read", "jobs:read", "assessments:read"]))
        issued = create_integration_grant(session, auth, settings=settings, payload=IntegrationGrantCreate(name="Synthetic read connection"))
    return SimpleNamespace(database=database, settings=settings, values=values, token=issued.token,
        grant_id=issued.grant.id, user_id=auth.user.id, organization_id=auth.organization_id,
        membership_id=auth.membership.id)


def named_auth(session, context):
    principal = principal_from_session(session, context.values)
    assert principal is not None
    return principal


@contextmanager
def browser_client(context, *, values=None, settings=None):
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = settings or context.settings
    app.add_middleware(SessionMiddleware, secret_key=app.state.settings.session_signing_secret(),
        session_cookie="resume_v3_session", same_site="strict")
    app.include_router(router)
    signed = TimestampSigner(app.state.settings.session_signing_secret()).sign(
        base64.b64encode(json.dumps(values or context.values).encode())).decode()
    with TestClient(app) as client:
        client.cookies.set("resume_v3_session", signed)
        yield client


def csrf_headers(client):
    response = client.get("/v1/integration-settings")
    assert response.status_code == 200, response.text
    return {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf_token"]}

