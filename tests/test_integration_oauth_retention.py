"""Unused public registrations are reclaimable without erasing auth history."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from app.models import (
    IntegrationAnalysisReport, IntegrationCredential, IntegrationGrant,
    IntegrationOAuthClient, IntegrationOAuthCode, IntegrationOAuthConsent,
    IntegrationOAuthFamily, IntegrationOAuthRefresh,
)
from app.integration_schemas import IntegrationPolicyPatch
from app.schemas import AuthRegistration
from app.services.integration_auth_service import IntegrationAccessError, authenticate_integration_token, integration_now
from app.services.identity_service import create_registration, establish_session
from app.services.integration_management_service import update_integration_policy
from app.services.integration_oauth_retention_service import (
    cleanup_terminal_oauth_families, cleanup_unused_oauth_clients,
)
from app.services.integration_retention_service import cleanup_expired_integration_records
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context
from test_integration_oauth import CALLBACK, RESOURCE, context, exchange, issue, oauth_browser, register, start, consent


def test_anonymous_dcr_recovers_from_old_full_registration_capacity(context):
    now = integration_now()
    with context.database.session_factory() as session:
        session.execute(insert(IntegrationOAuthClient.__table__), [
            {"id": str(uuid4()), "name": "Abandoned synthetic registration", "redirect_uris": [CALLBACK],
                "created_at": now - timedelta(hours=2)} for _ in range(10000)])
        session.commit()
    with oauth_browser(context, signed=False) as client:
        registered = register(client)
        # Still supports an ordinary anonymous native/WorkBuddy consent start.
        assert start(client, registered)
    with context.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IntegrationOAuthClient)) == 9001


def test_cleanup_preserves_recent_registration_live_consent_and_any_workspace_family(context):
    with oauth_browser(context) as browser:
        bound, token, _ = issue(browser)
        pending = register(browser)
        request_id = start(browser, pending)
        abandoned = register(browser)
        abandoned_request = start(browser, abandoned)
        recent = register(browser)
    now = integration_now()
    with context.database.session_factory() as session:
        session.execute(update(IntegrationOAuthClient).where(IntegrationOAuthClient.id.in_([bound, pending, abandoned]))
            .values(created_at=now - timedelta(hours=2)))
        session.execute(update(IntegrationOAuthConsent).where(IntegrationOAuthConsent.client_id.in_([bound, abandoned]))
            .values(expires_at=now - timedelta(seconds=1)))
        session.commit()
        # Anonymous/another-workspace context must not hide the bound family.
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        assert session.scalar(select(IntegrationOAuthFamily)) is None
        assert cleanup_unused_oauth_clients(session, now=now) == 2
        session.commit()
        assert session.get(IntegrationOAuthClient, abandoned) is None
        for client_id in (bound, pending, recent):
            assert session.get(IntegrationOAuthClient, client_id) is not None
        principal = authenticate_integration_token(session, token=token["access_token"], audience="mcp", settings=context.settings)
        assert principal.scopes == {"candidates:read"}
    with oauth_browser(context) as browser:
        code = consent(browser, request_id)["code"][0]
        assert exchange(browser, pending, code).status_code == 200
        assert browser.get(f"/v1/integration-settings/oauth/consents/{abandoned_request}").status_code == 410


def test_worker_cleanup_runs_bounded_even_with_integrations_disabled(context):
    now = integration_now()
    with context.database.session_factory() as session:
        session.add_all([IntegrationOAuthClient(name="Synthetic unused registration", redirect_uris=[CALLBACK],
            created_at=now - timedelta(hours=2)) for _ in range(1005)])
        session.commit()
    # The worker service accepts a DB, not feature flags: no need to enable OAuth.
    assert cleanup_expired_integration_records(context.database, now=now) == 1000
    assert cleanup_expired_integration_records(context.database, now=now) == 5
    assert cleanup_expired_integration_records(context.database, now=now) == 0


def test_worker_reclaims_authorized_pool_after_family_and_client_grace(context):
    with oauth_browser(context) as browser:
        client_id, _, _ = issue(browser)
    now = integration_now()
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        family = session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id))
        family.created_at = now - timedelta(days=60)
        family.expires_at = now - timedelta(days=30)
        session.execute(update(IntegrationOAuthConsent).where(
            IntegrationOAuthConsent.client_id == client_id,
        ).values(expires_at=now - timedelta(days=30)))
        client_row = session.get(IntegrationOAuthClient, client_id)
        client_row.last_authorized_at = now - timedelta(days=60)
        client_row.created_at = now - timedelta(days=60)
        session.commit()

    removed = cleanup_expired_integration_records(context.database, now=now)
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        assert session.get(IntegrationOAuthClient, client_id) is None
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id)) is None
    assert removed >= 2


def test_terminal_family_is_retained_during_recovery_window(context):
    with oauth_browser(context) as browser:
        client_id, _, _ = issue(browser)
    now = integration_now()
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        family = session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id))
        family.expires_at = now - timedelta(days=10)
        session.commit()
        assert cleanup_terminal_oauth_families(
            session, organization_id=context.organization_id, now=now,
        ) == 0
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id)) is not None
        assert session.scalar(select(IntegrationOAuthRefresh)) is not None


def test_worker_keeps_cached_client_id_reauthorizable_during_recovery_window(context):
    with oauth_browser(context) as browser:
        client_id, token, _ = issue(browser)
    now = integration_now()
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        family = session.scalar(select(IntegrationOAuthFamily).where(IntegrationOAuthFamily.client_id == client_id))
        family.created_at = now - timedelta(days=59)
        family.expires_at = now - timedelta(days=29)
        client_row = session.get(IntegrationOAuthClient, client_id)
        client_row.created_at = now - timedelta(days=59)
        client_row.last_authorized_at = now - timedelta(days=59)
        session.commit()
    cleanup_expired_integration_records(context.database, now=now)
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id)) is not None
        assert session.scalar(select(IntegrationOAuthRefresh)) is not None
        assert session.scalar(select(IntegrationCredential).where(
            IntegrationCredential.kind == "oauth")) is not None
        assert session.get(IntegrationOAuthClient, client_id) is not None
        with pytest.raises(IntegrationAccessError):
            authenticate_integration_token(
                session, token=token["access_token"], audience="mcp", settings=context.settings,
            )
        # T0 authorization, T0+30d natural expiry, and cleanup at T0+59d.
        # The old access/refresh tokens are invalid, but a cached client ID
        # remains recoverable until the family's 30-day terminal grace ends.
    with oauth_browser(context) as browser:
        refresh = browser.post("/v1/oauth/token", data={"client_id": client_id,
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"], "resource": RESOURCE})
        assert refresh.status_code == 400
        request_id = start(browser, client_id)
        params = consent(browser, request_id)
        reauthorized = exchange(browser, client_id, params["code"][0])
        assert reauthorized.status_code == 200, reauthorized.text


def test_terminal_family_cleanup_preserves_private_draft_provenance(context):
    with oauth_browser(context) as browser:
        client_id, _, _ = issue(browser)
    now = integration_now()
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        family = session.scalar(select(IntegrationOAuthFamily).where(IntegrationOAuthFamily.client_id == client_id))
        grant = session.scalar(select(IntegrationGrant).where(IntegrationGrant.id == family.grant_id))
        grant_id = grant.id
        session.add(IntegrationAnalysisReport(
            organization_id=context.organization_id, owner_user_id=context.user_id,
            membership_id=context.membership_id, source_grant_id=grant_id,
            title="Synthetic retained draft", content_json={"observations": ["Synthetic provenance"]},
            created_at=now - timedelta(days=70), updated_at=now - timedelta(days=70),
            expires_at=now + timedelta(days=100),
            confirmed_at=now - timedelta(days=70),
        ))
        family.expires_at = now - timedelta(days=31)
        session.commit()
        assert cleanup_terminal_oauth_families(
            session, organization_id=context.organization_id, now=now,
        ) == 1
        session.commit()
        assert session.get(IntegrationGrant, grant_id) is not None
        report = session.scalar(select(IntegrationAnalysisReport).where(
            IntegrationAnalysisReport.source_grant_id == grant_id))
        assert report is not None and report.content_json == {"observations": ["Synthetic provenance"]}
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id)) is None
    with oauth_browser(context) as browser:
        settings = browser.get("/v1/integration-settings")
        assert settings.status_code == 200, settings.text
        retained = next(item for item in settings.json()["grants"] if item["kind"] == "oauth")
        assert retained["status"] == "expired"
        assert retained["token_prefix"] == ""


def test_terminal_cleanup_does_not_remove_a_client_with_another_live_family(context):
    with oauth_browser(context) as browser:
        client_id, _, _ = issue(browser)
        second_request = start(browser, client_id)
        second_code = consent(browser, second_request)["code"][0]
        second_response = exchange(browser, client_id, second_code)
        assert second_response.status_code == 200
    now = integration_now()
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        families = session.scalars(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id).order_by(IntegrationOAuthFamily.created_at)).all()
        assert len(families) == 2
        families[0].expires_at = now - timedelta(days=31)
        session.commit()
        assert cleanup_terminal_oauth_families(
            session, organization_id=context.organization_id, now=now,
        ) == 1
        session.commit()
        assert session.get(IntegrationOAuthClient, client_id) is not None
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.client_id == client_id)) is not None
    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session, token=second_response.json()["access_token"], audience="mcp", settings=context.settings,
        )
        assert principal.user_id == context.user_id


def test_terminal_cleanup_preserves_shared_client_across_workspaces(context):
    with oauth_browser(context) as browser:
        client_id, _, _ = issue(browser)

    now = integration_now()
    second_values: dict[str, object] = {}
    with context.database.session_factory() as session:
        second_auth = create_registration(session, AuthRegistration(
            organization_name="Synthetic second workspace", full_name="Synthetic second account",
            email=f"integration-second-{uuid4()}@example.test", password="synthetic-only-password",
        ))
        second_auth.user.email_verified_at = now
        second_auth.organization.trial_started_at = now - timedelta(days=1)
        second_auth.organization.trial_ends_at = now + timedelta(days=30)
        session.commit()
        update_integration_policy(session, second_auth, settings=context.settings,
            payload=IntegrationPolicyPatch(enabled=True, allowed_scopes=[
                "candidates:read", "jobs:read", "assessments:read",
            ]))
        establish_session(second_values, second_auth)
        second_organization_id = second_auth.organization_id
        second_user_id = second_auth.user.id
        session.commit()

    second_context = SimpleNamespace(
        database=context.database, settings=context.settings, values=second_values,
        organization_id=second_organization_id, user_id=second_user_id,
    )
    with oauth_browser(context, values=second_values) as browser:
        request_id = start(browser, client_id)
        code = consent(browser, request_id)["code"][0]
        live_token = exchange(browser, client_id, code)
        assert live_token.status_code == 200

    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        first_family = session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.organization_id == context.organization_id,
            IntegrationOAuthFamily.client_id == client_id,
        ))
        first_family.created_at = now - timedelta(days=61)
        first_family.expires_at = now - timedelta(days=31)
        client_row = session.get(IntegrationOAuthClient, client_id)
        client_row.created_at = now - timedelta(days=61)
        client_row.last_authorized_at = now - timedelta(days=61)
        session.commit()
    cleanup_expired_integration_records(context.database, now=now)
    with context.database.session_factory() as session:
        set_organization_context(session, context.organization_id)
        assert session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.organization_id == context.organization_id,
            IntegrationOAuthFamily.client_id == client_id,
        )) is None
        assert session.get(IntegrationOAuthClient, client_id) is not None
        set_organization_context(session, second_context.organization_id)
        other_family = session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.organization_id == second_context.organization_id,
            IntegrationOAuthFamily.client_id == client_id,
        ))
        assert other_family is not None

    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session, token=live_token.json()["access_token"], audience="mcp", settings=context.settings,
        )
        assert principal.organization_id == second_context.organization_id
