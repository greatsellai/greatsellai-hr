from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import IntegrationCredential, IntegrationGrant, Organization, OrganizationMembership, UserAccount
from app.services.identity_service import revoke_user_auth_sessions
from app.services.integration_auth_service import IntegrationAccessError, authenticate_integration_token, integration_now
from app.services.integration_management_service import revoke_integration_grant
from test_integration_auth_helpers import make_context, named_auth


def resolve(session, context, **kwargs):
    return authenticate_integration_token(session, token=context.token,
        audience=kwargs.pop("audience", "rest"), settings=kwargs.pop("settings", context.settings), **kwargs)


def test_named_pat_is_digest_only_and_rest_audience_bound(tmp_path):
    ctx = make_context(tmp_path, integrations_mcp_enabled=True)
    with ctx.database.session_factory() as session:
        principal = resolve(session, ctx, required_scopes=["candidates:read"])
        assert principal.organization_id == ctx.organization_id
        assert principal.user_id == ctx.user_id and principal.membership_id == ctx.membership_id
        row = session.scalar(select(IntegrationCredential))
        assert row.token_digest == hashlib.sha256(ctx.token.encode()).hexdigest()
        assert ctx.token not in repr(row.__dict__)
        with pytest.raises(IntegrationAccessError, match="invalid_token"):
            resolve(session, ctx, audience="mcp")
        with pytest.raises(IntegrationAccessError, match="scope_forbidden"):
            resolve(session, ctx, required_scopes=["evidence:read"])
    ctx.database.dispose()


@pytest.mark.parametrize("change", ["logout", "password_version", "inactive_user", "inactive_member", "expired", "suspended", "missing_trial_end", "revoked"])
def test_authority_changes_are_reloaded_without_cache_fallback(tmp_path, change):
    ctx = make_context(tmp_path)
    with ctx.database.session_factory() as first:
        resolve(first, ctx)
        first.commit()
        with ctx.database.session_factory() as second:
            principal = named_auth(second, ctx)
            if change == "logout":
                revoke_user_auth_sessions(second, principal=principal)
            elif change == "password_version":
                second.execute(update(UserAccount).where(UserAccount.id == ctx.user_id).values(auth_session_version=2))
            elif change == "inactive_user":
                second.execute(update(UserAccount).where(UserAccount.id == ctx.user_id).values(is_active=False))
            elif change == "inactive_member":
                second.execute(update(OrganizationMembership).where(OrganizationMembership.id == ctx.membership_id).values(is_active=False))
            elif change == "expired":
                second.execute(update(Organization).where(Organization.id == ctx.organization_id).values(trial_ends_at=integration_now() - timedelta(seconds=1)))
            elif change == "suspended":
                second.execute(update(Organization).where(Organization.id == ctx.organization_id).values(plan_status="suspended"))
            elif change == "missing_trial_end":
                second.execute(update(Organization).where(Organization.id == ctx.organization_id).values(trial_ends_at=None))
            else:
                revoke_integration_grant(second, principal, grant_id=ctx.grant_id)
            second.commit()
        with pytest.raises(IntegrationAccessError):
            resolve(first, ctx)
    ctx.database.dispose()


def test_active_plan_valid_trial_and_all_feature_gates(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.database.session_factory() as session:
        assert resolve(session, ctx)
        session.execute(update(Organization).where(Organization.id == ctx.organization_id).values(plan_status="active", trial_ends_at=None))
        session.commit()
        assert resolve(session, ctx)
        for settings in (replace(ctx.settings, integrations_enabled=False),
                         replace(ctx.settings, integrations_pilot_organization_ids=("another-workspace",))):
            with pytest.raises(IntegrationAccessError, match="disabled"):
                resolve(session, ctx, settings=settings)
        with pytest.raises(IntegrationAccessError, match="invalid_token"):
            authenticate_integration_token(session, token="not-a-token", audience="rest", settings=ctx.settings)
    ctx.database.dispose()


def test_database_rejects_mismatched_membership_owner_binding(tmp_path):
    from sqlalchemy.exc import IntegrityError
    from app.tenant_scope import set_organization_context
    ctx = make_context(tmp_path)
    with ctx.database.session_factory() as session:
        set_organization_context(session, ctx.organization_id)
        other = UserAccount(email="binding-test@example.test", email_key="binding-test@example.test",
            full_name="Synthetic other owner", password_hash="synthetic-unusable-hash", is_active=True)
        session.add(other)
        session.flush()
        session.add(IntegrationGrant(organization_id=ctx.organization_id, user_id=other.id,
            membership_id=ctx.membership_id, audience="rest", name="Synthetic wrong binding", scopes=["candidates:read"]))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    ctx.database.dispose()

