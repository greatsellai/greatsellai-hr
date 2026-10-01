from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.integration_oauth_router import router as oauth_router
from app.integration_settings_router import router as settings_router
from app.models import IntegrationCredential, IntegrationGrant, IntegrationOAuthClient, IntegrationOAuthCode, IntegrationOAuthConsent, IntegrationOAuthFamily, IntegrationOAuthRefresh, UserAccount
from app.services.integration_auth_service import IntegrationAccessError, authenticate_integration_token, integration_now
from app.services.integration_oauth_service import redirect_matches, validate_redirect
from test_integration_auth_helpers import make_context

VERIFIER = "synthetic_pkce_verifier_" + "a" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
RESOURCE = "http://testserver/v1/mcp"
CALLBACK = "http://127.0.0.1:54321/callback"
PRIVATE_CALLBACK = "workbuddy://workbuddy/mcp/connector%3Asynthetic/oauth/callback"


@contextmanager
def oauth_browser(context, *, signed=True, settings=None, values=None, peer=("127.0.0.1", 54321)):
    app = FastAPI()
    app.state.settings = settings or context.settings
    app.state.database = context.database
    app.add_middleware(SessionMiddleware, secret_key=app.state.settings.session_signing_secret(),
        session_cookie="resume_v3_session", same_site="strict")
    app.include_router(settings_router)
    app.include_router(oauth_router)
    with TestClient(app, client=peer) as client:
        if signed:
            cookie = TimestampSigner(app.state.settings.session_signing_secret()).sign(
                base64.b64encode(json.dumps(values or context.values).encode())).decode()
            client.cookies.set("resume_v3_session", cookie)
        yield client


@pytest.fixture
def context(tmp_path):
    result = make_context(tmp_path, integrations_oauth_enabled=True, integrations_mcp_enabled=True)
    yield result
    result.database.dispose()


def register(client, **overrides):
    response = client.post("/v1/oauth/register", json={"client_name": "Synthetic desktop",
        "redirect_uris": [CALLBACK], **overrides})
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def start(client, client_id, **overrides):
    response = client.get("/v1/oauth/authorize", params={"client_id": client_id, "response_type": "code",
        "redirect_uri": CALLBACK, "scope": "candidates:read", "resource": RESOURCE,
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "synthetic-state", **overrides}, follow_redirects=False)
    assert response.status_code == 303, response.text
    target = urlsplit(response.headers["location"])
    assert target.path == "/settings/integrations/authorize"
    return parse_qs(target.query)["request_id"][0]


def consent(client, request_id, *, approve=True):
    path = f"/v1/integration-settings/oauth/consents/{request_id}"
    view = client.get(path)
    assert view.status_code == 200, view.text
    result = client.post(path, json={"approve": approve}, headers={"Origin": "http://testserver", "X-CSRF-Token": view.json()["csrf_token"]})
    assert result.status_code == 200, result.text
    params = parse_qs(urlsplit(result.json()["redirect_url"]).query)
    assert params["iss"] == ["http://testserver/"]
    assert params["iss"] == [client.get("/.well-known/oauth-authorization-server").json()["issuer"]]
    assert params["state"] == ["synthetic-state"]
    return params


def exchange(client, registered_client, code, **overrides):
    return client.post("/v1/oauth/token", data={"client_id": registered_client, "grant_type": "authorization_code",
        "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER, "resource": RESOURCE, **overrides})


def issue(client):
    client_id = register(client)
    request_id = start(client, client_id)
    params = consent(client, request_id)
    response = exchange(client, client_id, params["code"][0])
    assert response.status_code == 200, response.text
    return client_id, response.json(), params["code"][0]


def test_pkce_round_trip_reuses_bound_authorization_and_digests(context, caplog):
    caplog.set_level(logging.DEBUG)
    with oauth_browser(context) as client:
        client_id, token, code = issue(client)
        assert token["expires_in"] == 900
        assert token["access_token"].startswith("gs_oat_")
        with context.database.session_factory() as session:
            principal = authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=context.settings)
            assert principal.user_id == context.user_id and principal.scopes == {"candidates:read"}
            with pytest.raises(IntegrationAccessError):
                authenticate_integration_token(session, token=token["access_token"], audience="rest", settings=context.settings)
            rows = [session.scalar(select(IntegrationCredential).where(IntegrationCredential.id == principal.credential_id)),
                session.scalar(select(IntegrationOAuthCode)), session.scalar(select(IntegrationOAuthRefresh))]
            storage = repr([vars(row) for row in rows])
            assert token["access_token"] not in storage and token["refresh_token"] not in storage and code not in storage
        assert token["access_token"] not in caplog.text and token["refresh_token"] not in caplog.text and code not in caplog.text
        settings = client.get("/v1/integration-settings").json()
        oauth = next(grant for grant in settings["grants"] if grant["kind"] == "oauth")
        response = client.post(f"/v1/integration-settings/grants/{oauth['id']}/rotate", json={},
            headers={"Origin": "http://testserver", "X-CSRF-Token": settings["csrf_token"]})
        assert response.status_code == 409


def test_refresh_rotation_and_replay_revokes_all_family_access(context):
    with oauth_browser(context) as client:
        client_id, original, _ = issue(client)
        data = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": original["refresh_token"], "resource": RESOURCE}
        rotated = client.post("/v1/oauth/token", data=data)
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["refresh_token"] != original["refresh_token"]
        assert client.post("/v1/oauth/token", data=data).status_code == 400
        with context.database.session_factory() as session:
            for access in (original["access_token"], rotated.json()["access_token"]):
                with pytest.raises(IntegrationAccessError):
                    authenticate_integration_token(session, token=access, audience="mcp", settings=context.settings)


def test_code_replay_revokes_family(context):
    with oauth_browser(context) as client:
        client_id, token, code = issue(client)
        assert exchange(client, client_id, code).status_code == 400
        with context.database.session_factory() as session:
            with pytest.raises(IntegrationAccessError):
                authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=context.settings)


def test_oauth_connection_capacity_allows_reauthorization_but_rejects_new_client(context, monkeypatch):
    from app.services import integration_oauth_service

    monkeypatch.setattr(integration_oauth_service, "OAUTH_CLIENT_LIMIT_PER_USER", 1)
    monkeypatch.setattr(integration_oauth_service, "OAUTH_CLIENT_LIMIT_PER_WORKSPACE", 1)
    with oauth_browser(context) as browser:
        client_id = register(browser)
        consent(browser, start(browser, client_id))
        # The same public client already owns the single slot, so reauth does
        # not consume another connection slot.
        consent(browser, start(browser, client_id))

        another_client = register(browser)
        request_id = start(browser, another_client)
        path = f"/v1/integration-settings/oauth/consents/{request_id}"
        view = browser.get(path)
        assert view.status_code == 200, view.text
        rejected = browser.post(path, json={"approve": True}, headers={
            "Origin": "http://testserver", "X-CSRF-Token": view.json()["csrf_token"],
        })
        assert rejected.status_code == 429
        assert rejected.json() == {"detail": "oauth_connection_limit_reached"}


def test_oauth_user_client_limit_is_global_across_workspaces(context, monkeypatch):
    from app.integration_schemas import IntegrationPolicyPatch
    from app.models import UserAccount
    from app.schemas import AuthRegistration
    from app.services import integration_oauth_service
    from app.services.identity_service import AuthPrincipal, create_registration, establish_session
    from app.services.integration_management_service import update_integration_policy

    monkeypatch.setattr(integration_oauth_service, "OAUTH_CLIENT_LIMIT_PER_USER", 1)
    monkeypatch.setattr(integration_oauth_service, "OAUTH_CLIENT_LIMIT_PER_WORKSPACE", 2)
    with oauth_browser(context) as browser:
        client_id = register(browser)
        consent(browser, start(browser, client_id))

    second_values = {}
    with context.database.session_factory() as session:
        second = create_registration(session, AuthRegistration(
            organization_name="Synthetic second workspace", full_name="Synthetic second account",
            email="oauth-second-workspace@example.test", password="synthetic-only-password",
        ))
        second.user.email_verified_at = integration_now()
        second.organization.trial_ends_at = integration_now() + timedelta(days=30)
        session.commit()
        update_integration_policy(session, second, settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=["candidates:read"]))
        original_user = session.get(UserAccount, context.user_id)
        second.membership.user_id = original_user.id
        session.commit()
        second = AuthPrincipal(user=original_user, membership=second.membership,
            organization=second.organization, plan=second.plan)
        establish_session(second_values, second)

    with oauth_browser(context, values=second_values) as browser:
        another_client = register(browser)
        request_id = start(browser, another_client)
        path = f"/v1/integration-settings/oauth/consents/{request_id}"
        view = browser.get(path)
        assert view.status_code == 200, view.text
        rejected = browser.post(path, json={"approve": True}, headers={
            "Origin": "http://testserver", "X-CSRF-Token": view.json()["csrf_token"],
        })
        assert rejected.status_code == 429
        assert rejected.json() == {"detail": "oauth_connection_limit_reached"}
        # This second workspace was empty when another_client was rejected,
        # isolating the cross-workspace user cap from the workspace cap.
        # Reusing the already-authorized client ID still consumes no new user
        # slot and is permitted in this workspace.
        consent(browser, start(browser, client_id))


def test_revoke_is_non_enumerating_if_grant_disappears_after_token_lookup(context):
    from app.services.integration_oauth_service import revoke_oauth_token

    with oauth_browser(context) as browser:
        client_id, token, _ = issue(browser)

    class EmptyGrantResult:
        def mappings(self):
            return self

        def first(self):
            return None

    class GrantDisappearedSession:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self, statement, *args, **kwargs):
            if "integration_grants" in str(statement):
                return EmptyGrantResult()
            return self.wrapped.execute(statement, *args, **kwargs)

    with context.database.session_factory() as session:
        # Model the race where retention deletes the terminal grant between
        # resolving the opaque token digest and resolving its grant owner.
        revoke_oauth_token(GrantDisappearedSession(session), settings=context.settings, data={
            "client_id": client_id, "token": token["refresh_token"],
        })


@pytest.mark.parametrize("overrides", [{"code_verifier": "b" * 64}, {"resource": "http://testserver/v1/integrations"}, {"redirect_uri": CALLBACK + "/wrong"}, {"client_id": "wrong-client"}])
def test_code_bindings_cannot_be_changed(context, overrides):
    with oauth_browser(context) as client:
        client_id = register(client)
        code = consent(client, start(client, client_id))["code"][0]
        response = exchange(client, client_id, code, **overrides)
        assert response.status_code in {400, 401}
        assert exchange(client, client_id, code).status_code == 200


def test_authorize_without_cookie_then_cookie_consent_and_csrf(context):
    with oauth_browser(context, signed=False) as client:
        client_id = register(client)
        request_id = start(client, client_id)
        assert client.get(f"/v1/integration-settings/oauth/consents/{request_id}").status_code == 401
    with oauth_browser(context) as client:
        path = f"/v1/integration-settings/oauth/consents/{request_id}"
        assert client.post(path, json={"approve": True}).status_code == 403
        assert client.get(path, headers={"Authorization": "Bearer " + context.token}).status_code == 401
        result = consent(client, request_id, approve=False)
        assert result["error"] == ["access_denied"]
        assert client.get(path).status_code == 410


@pytest.mark.parametrize("change", ["expired", "other_session", "logout"])
def test_consent_expiry_and_session_binding(context, change):
    with oauth_browser(context) as client:
        request_id = start(client, register(client))
        view = client.get(f"/v1/integration-settings/oauth/consents/{request_id}")
        assert view.status_code == 200
        if change in {"expired", "logout"}:
            with context.database.session_factory() as session:
                if change == "expired":
                    session.scalar(select(IntegrationOAuthConsent)).expires_at = integration_now() - timedelta(seconds=1)
                else:
                    session.get(UserAccount, context.user_id).auth_session_version += 1
                session.commit()
            assert client.get(f"/v1/integration-settings/oauth/consents/{request_id}").status_code in {401, 410}
        else:
            values = {**context.values, "resume_v3_session_nonce": "z" * 64}
            with oauth_browser(context, values=values) as other:
                assert other.get(f"/v1/integration-settings/oauth/consents/{request_id}").status_code == 403


def test_metadata_gates_and_no_fake_capabilities(context):
    with oauth_browser(context) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert "client_id_metadata_document_supported" not in metadata
    with oauth_browser(context, settings=replace(context.settings, integrations_oauth_enabled=False)) as client:
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert client.post("/v1/oauth/register", json={}).status_code == 404


def test_oauth_rate_limit_uses_caddy_client_ip_and_ignores_spoofed_prefix(context):
    settings = replace(context.settings, trusted_proxy_cidrs=("172.30.0.2/32",))
    payload = {"client_name": "Synthetic desktop", "redirect_uris": ["http://evil.test/callback"]}
    with oauth_browser(context, settings=settings, peer=("172.30.0.2", 54321)) as client:
        first_ip_headers = {"x-forwarded-for": "198.51.100.99, 198.51.100.10"}
        second_ip_headers = {"x-forwarded-for": "203.0.113.20"}

        for _ in range(10):
            response = client.post("/v1/oauth/register", json=payload, headers=first_ip_headers)
            assert response.status_code == 400

        # A different Caddy-appended browser address has its own bucket,
        # while a changed client-supplied prefix cannot evade the first.
        assert client.post("/v1/oauth/register", json=payload, headers=second_ip_headers).status_code == 400
        spoofed_prefix = {"x-forwarded-for": "192.0.2.77, 198.51.100.10"}
        limited = client.post("/v1/oauth/register", json=payload, headers=spoofed_prefix)
        assert limited.status_code == 429
        assert limited.json() == {"error": "oauth_rate_limited"}


def test_oauth_rate_limit_ignores_forwarded_headers_from_untrusted_peer(context):
    settings = replace(context.settings, trusted_proxy_cidrs=("172.30.0.2/32",))
    payload = {"client_name": "Synthetic desktop", "redirect_uris": ["http://evil.test/callback"]}
    headers = {"x-forwarded-for": "198.51.100.10"}
    with oauth_browser(context, settings=settings, peer=("198.51.100.1", 54321)) as first_peer:
        for _ in range(10):
            assert first_peer.post("/v1/oauth/register", json=payload, headers=headers).status_code == 400
        assert first_peer.post("/v1/oauth/register", json=payload, headers=headers).status_code == 429
    with oauth_browser(context, settings=settings, peer=("198.51.100.2", 54321)) as second_peer:
        # The same spoofed X-Forwarded-For cannot make an untrusted peer share
        # or consume another direct peer's bucket.
        assert second_peer.post("/v1/oauth/register", json=payload, headers=headers).status_code == 400


@pytest.mark.parametrize("suffix", ["", "/"])
def test_root_issuer_matches_sdk_metadata_and_every_callback_exactly(context, suffix):
    from mcp.server.auth.settings import AuthSettings
    from mcp.shared.auth import ProtectedResourceMetadata
    from pydantic import AnyHttpUrl
    from app.services.integration_oauth_service import (
        IntegrationAuthorizationServer, oauth_issuer_identifier, oauth_metadata, resource_audience,
    )
    settings = replace(context.settings, public_app_url="https://hr.greatsellai.cn" + suffix)
    # Match create_integration_mcp's explicit AnyHttpUrl construction: the SDK
    # models preserve raw string paths, but an already parsed root URL has '/'.
    mcp_auth = AuthSettings(issuer_url=AnyHttpUrl(settings.public_app_url.rstrip("/")),
        resource_server_url=AnyHttpUrl("https://hr.greatsellai.cn/v1/mcp"), validate_token_resource=False)
    protected = ProtectedResourceMetadata(resource=mcp_auth.resource_server_url,
        authorization_servers=[mcp_auth.issuer_url]).model_dump(mode="json")
    metadata = oauth_metadata(settings)
    expected = protected["authorization_servers"][0]
    assert expected == metadata["issuer"] == oauth_issuer_identifier(settings) == "https://hr.greatsellai.cn/"
    assert protected["resource"] == "https://hr.greatsellai.cn/v1/mcp"
    assert resource_audience(protected["resource"], settings) == "mcp"
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert "//" not in urlsplit(metadata[field]).path
    with context.database.session_factory() as session:
        server = IntegrationAuthorizationServer(session, settings)
        for query in ("code=synthetic-code&state=synthetic-state", "error=access_denied&state=synthetic-state"):
            status, _, headers = server.handle_response(302, "", {"Location": CALLBACK + "?" + query})
            assert status == 302
            assert parse_qs(urlsplit(headers["Location"]).query)["iss"] == [expected]


@pytest.mark.parametrize("uri", [PRIVATE_CALLBACK, "other-desktop://client/callback",
    "https://example.test/callback#fragment", "http://evil.test/callback",
    "https://user:pass@example.test/callback", "javascript:alert(1)",
    "http://localhost.evil.test/callback", "https://*.example.test/callback"])
def test_registration_rejects_untrusted_redirects(context, uri):
    with oauth_browser(context) as client:
        response = client.post("/v1/oauth/register", json={"client_name": "Synthetic desktop",
            "redirect_uris": [uri], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}, follow_redirects=False)
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_redirect_uri"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "location" not in response.headers
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationOAuthClient)) is None
    # The RFC 7591 mapping belongs only to registration, not shared policy.
    with pytest.raises(IntegrationAccessError) as invalid:
        validate_redirect(uri)
    assert invalid.value.code == "oauth_redirect_invalid"


def test_registration_rejects_mixed_redirects_without_partial_client(context):
    with oauth_browser(context) as client:
        response = client.post("/v1/oauth/register", json={"redirect_uris": [CALLBACK, PRIVATE_CALLBACK]})
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_redirect_uri"}
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationOAuthClient)) is None


def test_loopback_registration_metadata_and_pkce_round_trip(context):
    # The documented HTTP-only fallback shape is supported, without admitting
    # private schemes. This is synthetic protocol coverage, not client acceptance.
    callback = "http://127.0.0.1:54321/oauth/callback"
    payload = {"client_name": "Synthetic desktop", "redirect_uris": [callback],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"}
    with oauth_browser(context) as client:
        response = client.post("/v1/oauth/register", json=payload)
        assert response.status_code == 201
        metadata = response.json()
        assert {key: metadata[key] for key in payload} == payload
        assert "client_secret" not in metadata
        client_id = metadata["client_id"]
        code = consent(client, start(client, client_id, redirect_uri=callback))["code"][0]
        tokens = exchange(client, client_id, code, redirect_uri=callback)
        assert tokens.status_code == 200
        assert tokens.json()["expires_in"] == 900
        with context.database.session_factory() as session:
            registered = session.get(IntegrationOAuthClient, client_id)
            assert registered.redirect_uris == [callback]
            principal = authenticate_integration_token(session, token=tokens.json()["access_token"],
                audience="mcp", settings=context.settings)
            assert principal.user_id == context.user_id
            assert principal.scopes == {"candidates:read"}


def test_registration_keeps_non_redirect_metadata_error(context):
    with oauth_browser(context) as client:
        response = client.post("/v1/oauth/register", json={"redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "client_secret_basic"})
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_client_metadata"}
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationOAuthClient)) is None


def test_loopback_only_port_can_change():
    assert redirect_matches(CALLBACK, "http://127.0.0.1:12345/callback")
    assert not redirect_matches(CALLBACK, "http://localhost:12345/callback")
    assert not redirect_matches(CALLBACK, "http://127.0.0.1:12345/callback/other")
    assert not redirect_matches(CALLBACK, "http://127.0.0.1:12345/callback?other=1")


def test_duplicate_parameters_pkce_plain_and_forbidden_grants(context):
    with oauth_browser(context) as client:
        client_id = register(client)
        response = client.get("/v1/oauth/authorize", params={"client_id": client_id, "response_type": "code",
            "redirect_uri": CALLBACK, "scope": "candidates:read", "resource": RESOURCE,
            "code_challenge": CHALLENGE, "code_challenge_method": "plain"})
        assert response.status_code == 400
        assert client.post("/v1/oauth/token", content="grant_type=refresh_token&grant_type=authorization_code", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
        for grant in ("password", "client_credentials"):
            response = client.post("/v1/oauth/token", data={"grant_type": grant, "client_id": client_id, "resource": RESOURCE})
            assert response.status_code == 400


def test_disabled_access_and_revoke_still_available(context):
    with oauth_browser(context) as client:
        client_id, token, _ = issue(client)
    settings = replace(context.settings, integrations_enabled=False)
    with context.database.session_factory() as session:
        with pytest.raises(IntegrationAccessError):
            authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=settings)
    with oauth_browser(context, settings=settings) as client:
        assert client.post("/v1/oauth/revoke", data={"client_id": client_id, "token": token["refresh_token"]}).status_code == 200
    with context.database.session_factory() as session:
        assert session.scalar(select(IntegrationGrant.__table__.c.revoked_at).where(IntegrationGrant.__table__.c.kind == "oauth")) is not None


def test_audit_failure_returns_no_token_and_does_not_consume_code(context, monkeypatch):
    from app.services import integration_oauth_service as service
    with oauth_browser(context) as client:
        client_id = register(client)
        code = consent(client, start(client, client_id))["code"][0]
        with monkeypatch.context() as patch:
            patch.setattr(service, "record_integration_audit", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic audit failure")))
            response = exchange(client, client_id, code)
            assert response.status_code == 503 and "access_token" not in response.text
        assert exchange(client, client_id, code).status_code == 200


def test_access_expiry_and_absolute_refresh_expiry(context):
    from app.tenant_scope import set_organization_context
    with oauth_browser(context) as client:
        client_id, token, _ = issue(client)
        with context.database.session_factory() as session:
            who = authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=context.settings)
            session.get(IntegrationCredential, who.credential_id).expires_at = integration_now() - timedelta(seconds=1)
            session.commit()
            with pytest.raises(IntegrationAccessError):
                authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=context.settings)
        with context.database.session_factory() as session:
            set_organization_context(session, context.organization_id)
            session.scalar(select(IntegrationOAuthFamily)).expires_at = integration_now() - timedelta(seconds=1)
            session.commit()
        response = client.post("/v1/oauth/token", data={"client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"], "resource": RESOURCE})
        assert response.status_code == 400


def test_refresh_scope_cannot_gain_or_silently_ignore_narrowing(context):
    with oauth_browser(context) as client:
        client_id, token, _ = issue(client)
        for scope in ("candidates:read evidence:read", "jobs:read", ""):
            result = client.post("/v1/oauth/token", data={"client_id": client_id, "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"], "resource": RESOURCE, "scope": scope})
            assert result.status_code == 400


def test_browser_revoke_prevents_refresh_without_revoking_other_pat(context):
    with oauth_browser(context) as client:
        client_id, token, _ = issue(client)
        body = client.get("/v1/integration-settings").json()
        grant_id = next(row["id"] for row in body["grants"] if row["kind"] == "oauth")
        assert client.delete(f"/v1/integration-settings/grants/{grant_id}", headers={"Origin": "http://testserver", "X-CSRF-Token": body["csrf_token"]}).status_code == 204
        assert client.post("/v1/oauth/token", data={"client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"], "resource": RESOURCE}).status_code == 400
    with context.database.session_factory() as session:
        assert authenticate_integration_token(session, token=context.token, audience="rest", settings=context.settings)


@pytest.mark.parametrize("change", ["logout", "membership_disabled", "plan_expired", "policy_disabled", "scope_removed"])
def test_refresh_rechecks_live_owner_membership_plan_and_policy(context, change):
    from app.models import Organization, OrganizationMembership, IntegrationWorkspacePolicy
    from app.tenant_scope import set_organization_context
    with oauth_browser(context) as client:
        client_id, token, _ = issue(client)
        with context.database.session_factory() as session:
            set_organization_context(session, context.organization_id)
            if change == "logout":
                session.get(UserAccount, context.user_id).auth_session_version += 1
            elif change == "membership_disabled":
                session.get(OrganizationMembership, context.membership_id).is_active = False
            elif change == "plan_expired":
                session.get(Organization, context.organization_id).trial_ends_at = integration_now() - timedelta(seconds=1)
            elif change == "policy_disabled":
                session.get(IntegrationWorkspacePolicy, context.organization_id).enabled = False
            else:
                session.get(IntegrationWorkspacePolicy, context.organization_id).allowed_scopes = []
            session.commit()
        response = client.post("/v1/oauth/token", data={"client_id": client_id, "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"], "resource": RESOURCE})
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("same_user", [False, True])
def test_claimed_consent_rejects_different_user_or_workspace(context, same_user):
    from uuid import uuid4
    from app.integration_schemas import IntegrationPolicyPatch
    from app.schemas import AuthRegistration
    from app.services.identity_service import AuthPrincipal, create_registration, establish_session
    from app.services.integration_management_service import update_integration_policy
    with oauth_browser(context) as client:
        request_id = start(client, register(client))
        assert client.get(f"/v1/integration-settings/oauth/consents/{request_id}").status_code == 200
    with context.database.session_factory() as session:
        other = create_registration(session, AuthRegistration(organization_name="Synthetic second workspace",
            full_name="Synthetic second member", email=f"other-oauth-{uuid4()}@example.test", password="synthetic-only-password"))
        other.user.email_verified_at = integration_now()
        other.organization.trial_ends_at = integration_now() + timedelta(days=30)
        session.commit()
        update_integration_policy(session, other, settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=["candidates:read"]))
        if same_user:
            original = session.get(UserAccount, context.user_id)
            other.membership.user_id = original.id
            session.commit()
            other = AuthPrincipal(user=original, membership=other.membership, organization=other.organization, plan=other.plan)
        values = {}
        establish_session(values, other)
    with oauth_browser(context, values=values) as client:
        assert client.get(f"/v1/integration-settings/oauth/consents/{request_id}").status_code == 403
