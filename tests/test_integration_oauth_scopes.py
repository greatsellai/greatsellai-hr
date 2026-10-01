"""Synthetic OAuth scope choice; product discovery is never a grant decision."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.integration_schemas import IntegrationPolicyPatch
from app.models import IntegrationGrant, IntegrationOAuthCode, IntegrationOAuthConsent
from app.services.integration_auth_service import (
    DEFAULT_INTEGRATION_SCOPES, INTEGRATION_SCOPES, IntegrationAccessError, authenticate_integration_token,
)
from app.services.integration_management_service import update_integration_policy
from test_integration_auth_helpers import make_context, named_auth
from test_integration_oauth import CALLBACK, CHALLENGE, RESOURCE, exchange, oauth_browser, register, start


@pytest.fixture
def context(tmp_path):
    result = make_context(tmp_path, integrations_oauth_enabled=True,
        integrations_mcp_enabled=True, integrations_analysis_enabled=True)
    yield result
    result.database.dispose()


def set_policy(context, scopes):
    with context.database.session_factory() as session:
        update_integration_policy(session, named_auth(session, context), settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=sorted(scopes)))


def view_consent(client, client_id, scopes):
    request_id = start(client, client_id, scope=" ".join(sorted(scopes)))
    path = f"/v1/integration-settings/oauth/consents/{request_id}"
    response = client.get(path)
    assert response.status_code == 200, response.text
    return path, response.json()


def decide(client, path, view, **payload):
    return client.post(path, json=payload,
        headers={"Origin": "http://testserver", "X-CSRF-Token": view["csrf_token"]})


def tokens_from_decision(client, client_id, response):
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
    assert query["state"] == ["synthetic-state"]
    assert query["iss"] == ["http://testserver/"]
    token = exchange(client, client_id, query["code"][0])
    assert token.status_code == 200, token.text
    return token.json()


@pytest.mark.parametrize("decision", [{"approve": True}, {"approve": True, "approved_scopes": None}])
def test_all_requested_scopes_with_read_policy_narrow_available_and_default(context, decision):
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
        assert view["scopes"] == sorted(INTEGRATION_SCOPES)
        assert view["available_scopes"] == view["default_scopes"] == sorted(DEFAULT_INTEGRATION_SCOPES)
        token = tokens_from_decision(client, client_id, decide(client, path, view, **decision))
        assert set(token["scope"].split()) == DEFAULT_INTEGRATION_SCOPES
        with context.database.session_factory() as session:
            principal = authenticate_integration_token(session, token=token["access_token"],
                audience="mcp", settings=context.settings)
            assert principal.scopes == DEFAULT_INTEGRATION_SCOPES


def test_optional_scopes_stay_default_off_even_if_allowed_and_requested(context):
    set_policy(context, INTEGRATION_SCOPES)
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
        assert view["available_scopes"] == sorted(INTEGRATION_SCOPES)
        assert view["default_scopes"] == sorted(DEFAULT_INTEGRATION_SCOPES)
        token = tokens_from_decision(client, client_id, decide(client, path, view, approve=True))
        assert set(token["scope"].split()) == DEFAULT_INTEGRATION_SCOPES
        with context.database.session_factory() as session:
            for scope in ("evidence:read", "analyses:read", "analyses:write"):
                with pytest.raises(IntegrationAccessError):
                    authenticate_integration_token(session, token=token["access_token"],
                        audience="mcp", settings=context.settings, required_scopes=[scope])


def test_explicit_selection_persists_only_chosen_scopes_through_code_grant_and_refresh(context):
    set_policy(context, INTEGRATION_SCOPES)
    selected = ["candidates:read", "evidence:read", "analyses:write"]
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
        token = tokens_from_decision(client, client_id,
            decide(client, path, view, approve=True, approved_scopes=selected))
        assert set(token["scope"].split()) == set(selected)
        with context.database.session_factory() as session:
            principal = authenticate_integration_token(session, token=token["access_token"],
                audience="mcp", settings=context.settings)
            assert set(session.get(IntegrationGrant, principal.grant_id).scopes) == set(selected)
            assert set(session.scalar(select(IntegrationOAuthCode)).scopes) == set(selected)
        rotated = client.post("/v1/oauth/token", data={"client_id": client_id,
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"], "resource": RESOURCE})
        assert rotated.status_code == 200
        assert set(rotated.json()["scope"].split()) == set(selected)


@pytest.mark.parametrize(("selected", "expected_status"), [
    ([], 403), (["candidates:read", "candidates:read"], 422),
    (["unknown:scope"], 422), (["jobs:read"], 403),
    (["candidates:read", "evidence:read"], 403), ("candidates:read", 422),
])
def test_scope_tampering_fails_without_consuming_consent(context, selected, expected_status):
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, ["candidates:read"])
        response = decide(client, path, view, approve=True, approved_scopes=selected)
        assert response.status_code == expected_status
        with context.database.session_factory() as session:
            assert session.scalar(select(IntegrationOAuthConsent)).consumed_at is None
            assert session.scalar(select(IntegrationOAuthCode)) is None
        token = tokens_from_decision(client, client_id,
            decide(client, path, view, approve=True, approved_scopes=["candidates:read"]))
        assert token["scope"] == "candidates:read"


def test_current_policy_is_rechecked_between_display_and_approval(context):
    set_policy(context, INTEGRATION_SCOPES)
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
        assert "evidence:read" in view["available_scopes"]
        set_policy(context, ["candidates:read"])
        denied = decide(client, path, view, approve=True,
            approved_scopes=["candidates:read", "evidence:read"])
        assert denied.status_code == 403
        refreshed = client.get(path).json()
        assert refreshed["available_scopes"] == refreshed["default_scopes"] == ["candidates:read"]
        token = tokens_from_decision(client, client_id, decide(client, path, refreshed, approve=True))
        assert token["scope"] == "candidates:read"


def test_explicit_scope_choice_still_requires_cookie_csrf_and_closed_payload(context):
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, ["candidates:read"])
        payload = {"approve": True, "approved_scopes": ["candidates:read"]}
        assert client.post(path, json=payload).status_code == 403
        assert client.post(path, json=payload, headers={"Origin": "https://foreign.example.test",
            "X-CSRF-Token": view["csrf_token"]}).status_code == 403
        assert decide(client, path, view, **payload, organization_id=context.organization_id).status_code == 422
        with oauth_browser(context, signed=False) as anonymous:
            assert decide(anonymous, path, view, **payload).status_code == 401
        token = tokens_from_decision(client, client_id, decide(client, path, view, **payload))
        assert token["scope"] == "candidates:read"


def test_analysis_feature_change_rechecks_selection_and_preserves_other_scopes(context):
    set_policy(context, INTEGRATION_SCOPES)
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
    with oauth_browser(context, settings=replace(context.settings, integrations_analysis_enabled=False)) as client:
        refreshed = client.get(path)
        assert refreshed.status_code == 200
        assert refreshed.json()["available_scopes"] == sorted(DEFAULT_INTEGRATION_SCOPES | {"evidence:read"})
        assert refreshed.json()["scopes"] == sorted(INTEGRATION_SCOPES)
        assert decide(client, path, view, approve=True, approved_scopes=["analyses:write"]).status_code == 403
        token = tokens_from_decision(client, client_id, decide(client, path, refreshed.json(), approve=True))
        assert set(token["scope"].split()) == DEFAULT_INTEGRATION_SCOPES


@pytest.mark.parametrize("selection", [[], ["unknown:scope"], ["candidates:read", "candidates:read"]])
def test_empty_available_scopes_can_deny_and_denial_ignores_selection(context, selection):
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, ["evidence:read"])
        assert view["available_scopes"] == view["default_scopes"] == []
        assert decide(client, path, view, approve=True).status_code == 403
        response = decide(client, path, view, approve=False, approved_scopes=selection)
        assert response.status_code == 200
        params = parse_qs(urlsplit(response.json()["redirect_url"]).query)
        assert params["error"] == ["access_denied"]
        assert "code" not in params
        assert client.get(path).status_code == 410
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationOAuthCode)) is None


def test_missing_authorize_scope_uses_only_predefined_base_read_ceiling(context):
    set_policy(context, INTEGRATION_SCOPES)
    with oauth_browser(context) as client:
        client_id = register(client)
        response = client.get("/v1/oauth/authorize", params={"client_id": client_id,
            "response_type": "code", "redirect_uri": CALLBACK, "resource": RESOURCE,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "synthetic-state"},
            follow_redirects=False)
        assert response.status_code == 303
        request_id = parse_qs(urlsplit(response.headers["location"]).query)["request_id"][0]
        path = f"/v1/integration-settings/oauth/consents/{request_id}"
        view = client.get(path).json()
        assert view["scopes"] == view["available_scopes"] == view["default_scopes"] == sorted(DEFAULT_INTEGRATION_SCOPES)
        assert decide(client, path, view, approve=True, approved_scopes=["evidence:read"]).status_code == 403
        token = tokens_from_decision(client, client_id, decide(client, path, view, approve=True))
        assert set(token["scope"].split()) == DEFAULT_INTEGRATION_SCOPES


@pytest.mark.parametrize("scope", ["", " ", "unknown:scope", "candidates:read unknown:scope"])
def test_explicit_empty_or_unknown_authorize_scope_is_not_defaulted(context, scope):
    with oauth_browser(context) as client:
        client_id = register(client)
        response = client.get("/v1/oauth/authorize", params={"client_id": client_id,
            "response_type": "code", "redirect_uri": CALLBACK, "resource": RESOURCE,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "scope": scope},
            follow_redirects=False)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_scope"}
        assert "location" not in response.headers
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationOAuthConsent)) is None


def test_failed_approval_audit_rolls_back_narrowed_scopes_and_consumption(context, monkeypatch):
    from app.services import integration_oauth_service as service
    set_policy(context, INTEGRATION_SCOPES)
    with oauth_browser(context) as client:
        client_id = register(client)
        path, view = view_consent(client, client_id, INTEGRATION_SCOPES)
        with monkeypatch.context() as patch:
            patch.setattr(service, "record_integration_audit",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic audit failure")))
            assert decide(client, path, view, approve=True, approved_scopes=["candidates:read"]).status_code == 503
        with context.database.session_factory() as session:
            row = session.scalar(select(IntegrationOAuthConsent))
            assert row.scopes == sorted(INTEGRATION_SCOPES)
            assert row.consumed_at is None
            assert session.scalar(select(IntegrationOAuthCode)) is None
        tokens_from_decision(client, client_id, decide(client, path, view, approve=True, approved_scopes=["candidates:read"]))

