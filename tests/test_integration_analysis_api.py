from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from app.integration_analysis_browser_router import router as browser_router
from app.integration_router import router as external_router
from app.models import IntegrationAnalysisReport
from app.services.integration_auth_service import authenticate_integration_token, integration_now
from app.services.integration_management_service import integration_csrf_token
from app.services.integration_read_service import get_candidate_profile
from app.services.integration_retention_service import cleanup_expired_integration_records
from app.tenant_scope import set_organization_context
from test_integration_analysis_service import analysis_payload, make_analysis_context
from test_integration_auth_helpers import named_auth


def _external_client(context):
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = context.settings
    app.include_router(external_router)
    return TestClient(app)


def _browser_client(context):
    app = FastAPI()
    app.state.database = context.database
    app.state.settings = context.settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=context.settings.session_signing_secret(),
        session_cookie="resume_v3_session",
        same_site="strict",
    )
    app.include_router(browser_router)
    client = TestClient(app)
    signed = (
        TimestampSigner(context.settings.session_signing_secret())
        .sign(base64.b64encode(json.dumps(context.values).encode()))
        .decode()
    )
    client.cookies.set("resume_v3_session", signed)
    return client


def _csrf_headers(context):
    with context.database.session_factory() as session:
        principal = named_auth(session, context)
        token = integration_csrf_token(context.settings, principal, context.values)
    return {"Origin": "http://testserver", "X-CSRF-Token": token}


def test_external_proposal_requires_owner_confirmation_before_it_is_a_readable_draft(tmp_path):
    context = make_analysis_context(tmp_path)
    from app.services.integration_auth_service import authenticate_integration_token
    from app.services.integration_read_service import get_candidate_profile

    with context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=context.analysis_token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        context.alpha_profile = get_candidate_profile(
            session,
            candidate_id=context.ids["alpha"],
        )
    payload = analysis_payload(context)
    with _external_client(context) as client:
        denied = client.post(
            "/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.token}"},
            json=payload.model_dump(mode="json"),
        )
        forged_confirmation = client.post(
            "/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
            json={**payload.model_dump(mode="json"), "user_confirmed": True},
        )
        prepared = client.post(
            "/v1/integrations/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
            json=payload.model_dump(mode="json"),
        )
        preconfirm_detail = client.get(
            f"/v1/integrations/analysis-reports/{prepared.json()['id']}",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
        )

    assert denied.status_code == 403
    assert forged_confirmation.status_code == 422
    assert prepared.status_code == 202
    assert set(prepared.json()) == {"id", "version", "status", "expires_at", "replayed"}
    assert prepared.json()["status"] == "awaiting_confirmation"
    assert prepared.headers["cache-control"] == "no-store"
    assert preconfirm_detail.status_code == 404

    with _browser_client(context) as client:
        listed = client.get("/v1/integration-settings/analysis-reports")
        pending = client.get("/v1/integration-settings/analysis-reports/pending")
        pending_detail = client.get(f"/v1/integration-settings/analysis-reports/pending/{prepared.json()['id']}")
        bearer_denied = client.get(
            "/v1/integration-settings/analysis-reports",
            headers={"Authorization": f"Bearer {context.analysis_token}"},
        )
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert pending.status_code == 200
    assert [item["id"] for item in pending.json()["items"]] == [prepared.json()["id"]]
    assert pending_detail.status_code == 200
    assert pending_detail.json()["source_status"] == "current"
    assert pending_detail.json()["payload_sha256"]
    assert bearer_denied.status_code == 401

    with _browser_client(context) as client:
        no_csrf = client.post(
            f"/v1/integration-settings/analysis-reports/{prepared.json()['id']}/confirm",
            json={"version": pending_detail.json()["version"], "payload_sha256": pending_detail.json()["payload_sha256"]},
        )
        changed_content = client.post(
            f"/v1/integration-settings/analysis-reports/{prepared.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending_detail.json()["version"], "payload_sha256": "0" * 64},
        )
        confirmed = client.post(
            f"/v1/integration-settings/analysis-reports/{prepared.json()['id']}/confirm",
            headers=_csrf_headers(context),
            json={"version": pending_detail.json()["version"], "payload_sha256": pending_detail.json()["payload_sha256"]},
        )
        saved_list = client.get("/v1/integration-settings/analysis-reports")
    with _external_client(context) as client:
        saved_detail = client.get(f"/v1/integrations/analysis-reports/{prepared.json()['id']}",
            headers={"Authorization": f"Bearer {context.analysis_token}"})
    assert no_csrf.status_code == 403
    assert changed_content.status_code == 409
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "saved"
    assert confirmed.json()["version"] == pending_detail.json()["version"]
    assert saved_detail.status_code == 200
    assert saved_detail.json()["referenced_facts"][0]["facts"]["education"] == []
    assert [item["id"] for item in saved_list.json()["items"]] == [prepared.json()["id"]]


@pytest.mark.parametrize("terminal_state", ["saved", "expired", "discarded"])
def test_rest_replays_terminal_receipts_without_recreating_pending_drafts(tmp_path, terminal_state):
    context = make_analysis_context(tmp_path)
    try:
        with context.database.session_factory() as session:
            authenticate_integration_token(session, token=context.analysis_token,
                audience="rest", settings=context.settings)
            context.alpha_profile = get_candidate_profile(session, candidate_id=context.ids["alpha"])
        payload = analysis_payload(context).model_dump(mode="json")
        headers = {"Authorization": f"Bearer {context.analysis_token}"}
        with _external_client(context) as external, _browser_client(context) as browser:
            prepared = external.post("/v1/integrations/analysis-reports", headers=headers, json=payload)
            assert prepared.status_code == 202
            receipt = prepared.json()
            report_id = receipt["id"]
            if terminal_state == "saved":
                detail = browser.get(f"/v1/integration-settings/analysis-reports/pending/{report_id}")
                assert detail.status_code == 200
                response = browser.post(f"/v1/integration-settings/analysis-reports/{report_id}/confirm",
                    headers=_csrf_headers(context), json={"version": detail.json()["version"],
                        "payload_sha256": detail.json()["payload_sha256"]})
                assert response.status_code == 200
            elif terminal_state == "discarded":
                response = browser.post(f"/v1/integration-settings/analysis-reports/{report_id}/discard",
                    headers=_csrf_headers(context), json={"version": receipt["version"]})
                assert response.status_code == 204
            else:
                with context.database.session_factory() as session:
                    set_organization_context(session, context.organization_id)
                    session.get(IntegrationAnalysisReport, report_id).expires_at = integration_now() - timedelta(seconds=1)
                    session.commit()
                cleanup_expired_integration_records(context.database)
            replay = external.post("/v1/integrations/analysis-reports", headers=headers, json=payload)
            assert replay.status_code == 202, replay.text
            assert replay.json()["id"] == report_id
            assert replay.json()["version"] == receipt["version"]
            assert replay.json()["replayed"] is True
            assert replay.json()["status"] == ("saved" if terminal_state == "saved" else "expired")
            assert set(replay.json()) == {"id", "version", "status", "expires_at", "replayed"}
            pending = browser.get("/v1/integration-settings/analysis-reports/pending")
            assert pending.status_code == 200
            assert pending.json()["items"] == []
    finally:
        context.database.dispose()
