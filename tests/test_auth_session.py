from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient


def _register_and_verify(client: TestClient) -> None:
    email = "named-session-owner@example.test"
    password = "named-session-owner-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Named session workspace",
            "full_name": "Named session owner",
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


def test_login_session_protects_resume_endpoints_with_named_account(
    protected_client: TestClient,
) -> None:
    session = protected_client.get("/v1/auth/session")
    assert session.status_code == 200
    assert session.json() == {
        "authenticated": False,
        "login_required": True,
        "is_platform_admin": False,
        "email_verified": False,
        "email_verification_required": False,
        "user": None,
        "organization": None,
        "role": None,
        "plan": None,
        "trial": None,
    }

    denied = protected_client.get("/v1/resume-library")
    assert denied.status_code == 401

    _register_and_verify(protected_client)

    invalid = protected_client.post(
        "/v1/auth/login",
        json={
            "email": "named-session-owner@example.test",
            "password": "wrong",
        },
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "invalid_login_credentials"

    authenticated = protected_client.post(
        "/v1/auth/login",
        json={
            "email": "named-session-owner@example.test",
            "password": "named-session-owner-password",
        },
    )
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["authenticated"] is True
    assert payload["login_required"] is True
    assert payload["organization"]["name"] == "Named session workspace"
    assert payload["role"] == "admin"
    assert "httponly" in authenticated.headers["set-cookie"].lower()
    assert "samesite=strict" in authenticated.headers["set-cookie"].lower()

    allowed = protected_client.get("/v1/resume-library")
    assert allowed.status_code == 200

    logged_out = protected_client.post("/v1/auth/logout")
    assert logged_out.status_code == 204
    denied_again = protected_client.get("/v1/resume-library")
    assert denied_again.status_code == 401
