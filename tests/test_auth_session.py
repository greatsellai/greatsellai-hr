from __future__ import annotations


def test_login_session_protects_resume_endpoints(protected_client) -> None:
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

    invalid = protected_client.post("/v1/auth/login", json={"password": "wrong"})
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "invalid_login_credentials"

    authenticated = protected_client.post(
        "/v1/auth/login",
        json={"password": "test-admin-token"},
    )
    assert authenticated.status_code == 200
    payload = authenticated.json()
    assert payload["authenticated"] is True
    assert payload["login_required"] is True
    assert payload["organization"]["name"] == "Legacy workspace"
    assert payload["role"] == "admin"
    assert "httponly" in authenticated.headers["set-cookie"].lower()
    assert "samesite=strict" in authenticated.headers["set-cookie"].lower()

    allowed = protected_client.get("/v1/resume-library")
    assert allowed.status_code == 200

    logged_out = protected_client.post("/v1/auth/logout")
    assert logged_out.status_code == 204
    denied_again = protected_client.get("/v1/resume-library")
    assert denied_again.status_code == 401

