from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
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


def test_platform_admin_alone_can_publish_ai_model_route(
    identity_client: TestClient,
) -> None:
    _register_workspace(identity_client)

    denied = identity_client.get("/v1/platform/ai/providers")
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == "platform_admin_required"

    usage_denied = identity_client.get("/v1/platform/ai/usage/runs")
    assert usage_denied.status_code == 403, usage_denied.text
    assert usage_denied.json()["detail"] == "platform_admin_required"

    legacy_login = identity_client.post(
        "/v1/auth/login",
        json={"password": "legacy-platform-test-token"},
    )
    assert legacy_login.status_code == 200, legacy_login.text

    usage_runs = identity_client.get("/v1/platform/ai/usage/runs")
    assert usage_runs.status_code == 200, usage_runs.text
    assert usage_runs.json() == []
    usage_summary = identity_client.get("/v1/platform/ai/usage/summary")
    assert usage_summary.status_code == 200, usage_summary.text
    assert usage_summary.json() == []

    insecure_provider = identity_client.post(
        "/v1/platform/ai/providers",
        json={
            "slug": "insecure-provider",
            "display_name": "Insecure provider",
            "driver": "openai_compatible",
            "endpoint_url": "http://127.0.0.1/v1/chat/completions",
            "credential_ref": "ignored",
        },
    )
    assert insecure_provider.status_code == 422, insecure_provider.text
    assert "ai_endpoint_url" in insecure_provider.text

    secret_defaults = identity_client.post(
        "/v1/platform/ai/providers",
        json={
            "slug": "secret-defaults-provider",
            "display_name": "Secret defaults provider",
            "driver": "openai_compatible",
            "endpoint_url": "https://api.example.test/v1/chat/completions",
            "credential_ref": "ignored",
            "request_defaults": {"nested": {"Authorization": "must-not-store"}},
        },
    )
    assert secret_defaults.status_code == 422, secret_defaults.text
    assert "ai_request_defaults_protected_key" in secret_defaults.text

    provider = identity_client.post(
        "/v1/platform/ai/providers",
        json={
            "slug": "test-provider",
            "display_name": "Test provider",
            "driver": "openai_compatible",
            "endpoint_url": "https://api.example.test/v1/chat/completions",
            "credential_ref": "test-provider-credential",
            "request_defaults": {"thinking": {"type": "disabled"}},
        },
    )
    assert provider.status_code == 201, provider.text
    assert provider.json()["credential_ref"] == "test-provider-credential"

    model = identity_client.post(
        "/v1/platform/ai/models",
        json={
            "slug": "test-model",
            "provider_slug": "test-provider",
            "display_name": "Test model",
            "provider_model_id": "provider-side-model-id",
            "capabilities": ["chat", "tools", "json_schema"],
        },
    )
    assert model.status_code == 201, model.text
    assert model.json()["provider_slug"] == "test-provider"

    price = identity_client.post(
        "/v1/platform/ai/model-prices",
        json={
            "model_slug": "test-model",
            "effective_from": datetime.now(timezone.utc).isoformat(),
            "input_per_million": "1.25",
            "output_per_million": "2.50",
            "source": "platform-test",
        },
    )
    assert price.status_code == 201, price.text
    assert price.json()["model_slug"] == "test-model"
    assert price.json()["source"] == "platform-test"

    published = identity_client.put(
        "/v1/platform/ai/routes/resume_score",
        json={
            "display_name": "Resume score route",
            "description": "Platform-owned test route",
            "targets": [
                {
                    "model_slug": "test-model",
                    "max_attempts": 2,
                    "allow_fallback_on": ["timeout", "provider_5xx"],
                }
            ],
            "prompt_revision": "resume-score.prompt.v1",
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["feature"] == "resume_score"
    assert published.json()["version"] == 1
    assert published.json()["targets"] == [
        {
            "model_slug": "test-model",
            "max_attempts": 2,
            "allow_fallback_on": ["timeout", "provider_5xx"],
        }
    ]

    listed = identity_client.get("/v1/platform/ai/routes")
    assert listed.status_code == 200, listed.text
    policy = next(item for item in listed.json() if item["feature"] == "resume_score")
    assert policy["current_version"] == 1

    versions = identity_client.get("/v1/platform/ai/routes/resume_score/versions")
    assert versions.status_code == 200, versions.text
    assert versions.json()[0]["prompt_revision"] == "resume-score.prompt.v1"
