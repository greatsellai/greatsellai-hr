from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app


@pytest.fixture
def identity_client(tmp_path: Path) -> Iterator[TestClient]:
    """A disposable identity/plan database with legacy-token compatibility."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        admin_token="legacy-platform-test-token",
        session_secret="identity-plan-test-session-secret",
        allow_unauthenticated=False,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _register_workspace(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Plan fixture workspace",
            "full_name": "Plan fixture admin",
            "email": "plan-fixture-admin@example.test",
            "password": "plan-fixture-password",
        },
    )
    assert response.status_code == 201, response.text
    provider = client.app.state.transactional_email_provider
    delivery = provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    return verified.json()


def test_new_registration_uses_advanced_30_day_trial_and_cannot_manage_platform_plans(
    identity_client: TestClient,
) -> None:
    registered = _register_workspace(identity_client)

    assert registered["authenticated"] is True
    assert registered["role"] == "admin"
    assert registered["plan"]["code"] == "advanced"
    assert registered["trial"]["plan_status"] == "trial"
    assert registered["trial"]["access_enabled"] is True
    assert registered["trial"]["trial_days_remaining"] == 30

    current_plan = identity_client.get("/v1/organization/plan")
    assert current_plan.status_code == 200, current_plan.text
    current_payload = current_plan.json()
    assert current_payload["plan_code"] == "advanced"
    assert current_payload["plan_status"] == "trial"
    assert current_payload["organization_id"] == registered["organization"]["organization_id"]

    starts_at = datetime.fromisoformat(current_payload["trial_started_at"])
    ends_at = datetime.fromisoformat(current_payload["trial_ends_at"])
    assert (ends_at.date() - starts_at.date()).days == 30

    denied = identity_client.get("/v1/platform/plans")
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == "platform_admin_required"


def test_legacy_platform_admin_can_list_and_update_product_plans(
    identity_client: TestClient,
) -> None:
    initial_offer = identity_client.get("/v1/auth/registration-offer")
    assert initial_offer.status_code == 200, initial_offer.text
    assert initial_offer.json() == {
        "plan_code": "advanced",
        "plan_name": "进阶版",
        "trial_days": 30,
    }

    legacy_login = identity_client.post(
        "/v1/auth/login",
        json={"password": "legacy-platform-test-token"},
    )
    assert legacy_login.status_code == 200, legacy_login.text

    listed = identity_client.get("/v1/platform/plans")
    assert listed.status_code == 200, listed.text
    original_plans = {plan["code"]: plan for plan in listed.json()}
    assert set(original_plans) == {"basic", "advanced", "professional"}

    updated_flags = {
        "resume_library": True,
        "candidate_filtering": True,
        "mailbox_import": False,
        "ai_jd_generation": False,
    }
    updated = identity_client.put(
        "/v1/platform/plans/advanced",
        json={"trial_days": 21, "feature_flags": updated_flags},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["code"] == "advanced"
    assert updated.json()["trial_days"] == 21
    assert updated.json()["feature_flags"] == updated_flags

    reloaded = identity_client.get("/v1/platform/plans")
    assert reloaded.status_code == 200, reloaded.text
    reloaded_plans = {plan["code"]: plan for plan in reloaded.json()}
    assert reloaded_plans["advanced"]["trial_days"] == 21
    assert reloaded_plans["advanced"]["feature_flags"] == updated_flags

    updated_offer = identity_client.get("/v1/auth/registration-offer")
    assert updated_offer.status_code == 200, updated_offer.text
    assert updated_offer.json()["plan_code"] == "advanced"
    assert updated_offer.json()["trial_days"] == 21
