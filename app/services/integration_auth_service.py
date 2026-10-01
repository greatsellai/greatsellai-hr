"""Shared REST/MCP authorization. No cookies, legacy fallback or caller tenant IDs."""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    IntegrationAuditEvent, IntegrationCredential, IntegrationGrant,
    IntegrationWorkspacePolicy, IntegrationOAuthFamily, IntegrationOAuthClient, Organization, OrganizationMembership, ProductPlan,
    UserAccount,
)
from app.services.identity_service import (
    AuthPrincipal, DEVELOPMENT_USER_ID, RETIRED_LEGACY_USER_ID, trial_access,
)
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context

IntegrationAudience = Literal["rest", "mcp"]
DEFAULT_INTEGRATION_SCOPES = frozenset({"candidates:read", "jobs:read", "assessments:read"})
INTEGRATION_SCOPES = DEFAULT_INTEGRATION_SCOPES | {"evidence:read", "analyses:read", "analyses:write"}
_TOKEN_PATTERN = re.compile(r"gs_(?:pat|oat)_[a-f0-9]{16}_[A-Za-z0-9_-]{43}\Z")


class IntegrationAccessError(RuntimeError):
    def __init__(self, code: str, status_code: int = 403, *, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class IntegrationPrincipal:
    auth: AuthPrincipal
    grant_id: str
    credential_id: str
    audience: IntegrationAudience
    scopes: frozenset[str]
    auth_session_version: int
    required_scopes: frozenset[str] = frozenset()

    @property
    def organization_id(self) -> str:
        return self.auth.organization_id

    @property
    def user_id(self) -> str:
        return self.auth.user.id

    @property
    def membership_id(self) -> str:
        return self.auth.membership.id


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def integration_now(now: datetime | None = None) -> datetime:
    return aware(now) if now is not None else datetime.now(timezone.utc)


def integration_features(settings: AppSettings, organization_id: str) -> dict[str, bool]:
    enabled = settings.integrations_enabled and (
        not settings.integrations_pilot_organization_ids
        or organization_id in settings.integrations_pilot_organization_ids
    )
    return {"api": enabled, "mcp": enabled and settings.integrations_mcp_enabled,
            "analyses": enabled and settings.integrations_analysis_enabled,
            "oauth": enabled and settings.integrations_oauth_enabled}


def ensure_integration_entitlement(auth: AuthPrincipal, *, now: datetime | None = None) -> None:
    current = integration_now(now)
    if (auth.organization_id == LEGACY_ORGANIZATION_ID
            or auth.user.id in {DEVELOPMENT_USER_ID, RETIRED_LEGACY_USER_ID}
            or not auth.user.is_active or not auth.membership.is_active):
        raise IntegrationAccessError("integration_authentication_required", 401)
    if not auth.email_verified or auth.role not in {"admin", "recruiter"}:
        raise IntegrationAccessError("email_verification_required", 403)
    allowed = trial_access(auth, now=current).access_enabled
    if auth.organization.plan_status == "trial":
        ends = auth.organization.trial_ends_at
        starts = auth.organization.trial_started_at
        allowed = allowed and ends is not None and current < aware(ends)
        allowed = allowed and (starts is None or aware(starts) <= current)
    if not allowed:
        raise IntegrationAccessError("integration_plan_inactive", 402)


def assert_integration_context(session: Session, organization_id: str) -> None:
    if (not organization_id or organization_id == LEGACY_ORGANIZATION_ID
            or session.info.get("greatsell_organization_id") != organization_id
            or session.info.get("greatsell_skip_organization_scope")):
        raise IntegrationAccessError("integration_context_required", 403)


def reload_bound_auth(
    session: Session, *, organization_id: str, user_id: str, membership_id: str,
    auth_session_version: int, lock: bool = False,
) -> AuthPrincipal:
    """Load every authority-bearing row afresh; lock in workspace/user/member order."""
    if organization_id == LEGACY_ORGANIZATION_ID or user_id in {DEVELOPMENT_USER_ID, RETIRED_LEGACY_USER_ID}:
        raise IntegrationAccessError("integration_invalid_token", 401)

    def load(statement):
        if lock:
            statement = statement.with_for_update()
        return session.scalar(statement.execution_options(populate_existing=True))

    organization = load(select(Organization).where(Organization.id == organization_id))
    user = load(select(UserAccount).where(UserAccount.id == user_id))
    membership = load(select(OrganizationMembership).where(
        OrganizationMembership.id == membership_id,
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == user_id,
    ))
    if (organization is None or user is None or membership is None
            or not user.is_active or not membership.is_active
            or user.auth_session_version != auth_session_version):
        raise IntegrationAccessError("integration_invalid_token", 401)
    plan = load(select(ProductPlan).where(ProductPlan.id == organization.plan_id)) if organization.plan_id else None
    set_organization_context(session, organization_id)
    return AuthPrincipal(user=user, membership=membership, organization=organization, plan=plan)


def lock_integration_policy(session: Session, organization_id: str) -> IntegrationWorkspacePolicy:
    """A DB write serializes replicas, including SQLite where FOR UPDATE is a no-op."""
    assert_integration_context(session, organization_id)
    changed = session.execute(update(IntegrationWorkspacePolicy).where(
        IntegrationWorkspacePolicy.organization_id == organization_id,
    ).values(lock_version=IntegrationWorkspacePolicy.lock_version + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise IntegrationAccessError("integrations_disabled", 403)
    return session.scalar(select(IntegrationWorkspacePolicy).where(
        IntegrationWorkspacePolicy.organization_id == organization_id,
    ).execution_options(populate_existing=True))


def _resolve_credential(
    session: Session, *, credential_id: str, organization_id: str,
    audience: IntegrationAudience, settings: AppSettings,
    required_scopes: Collection[str], now: datetime | None, lock: bool,
    expected: IntegrationPrincipal | None = None,
) -> IntegrationPrincipal:
    current = integration_now(now)
    # The digest lookup authenticates the binding before any tenant-scoped ORM read.
    credential_row = session.execute(select(IntegrationCredential.__table__).where(
        IntegrationCredential.__table__.c.id == credential_id,
        IntegrationCredential.__table__.c.organization_id == organization_id,
    )).mappings().first()
    if credential_row is None:
        raise IntegrationAccessError("integration_invalid_token", 401)
    grant_row = session.execute(select(IntegrationGrant.__table__).where(
        IntegrationGrant.__table__.c.id == credential_row["grant_id"],
        IntegrationGrant.__table__.c.organization_id == organization_id,
    )).mappings().first()
    if grant_row is None:
        raise IntegrationAccessError("integration_invalid_token", 401)
    auth = reload_bound_auth(session, organization_id=organization_id,
        user_id=grant_row["user_id"], membership_id=grant_row["membership_id"],
        auth_session_version=credential_row["auth_session_version"], lock=lock)
    if lock:
        policy = lock_integration_policy(session, organization_id)
    else:
        policy = session.scalar(select(IntegrationWorkspacePolicy).where(
            IntegrationWorkspacePolicy.organization_id == organization_id,
        ).execution_options(populate_existing=True))
    # Policy serialization is also held by rotation/revocation; re-read AFTER it.
    credential = session.scalar(select(IntegrationCredential).where(
        IntegrationCredential.id == credential_id,
        IntegrationCredential.organization_id == organization_id,
    ).execution_options(populate_existing=True))
    if credential is None:
        raise IntegrationAccessError("integration_invalid_token", 401)
    grant = session.scalar(select(IntegrationGrant).where(
        IntegrationGrant.id == credential.grant_id,
        IntegrationGrant.organization_id == organization_id,
    ).execution_options(populate_existing=True))
    # A request may have waited for another replica's transaction. Expiry is
    # evaluated after acquiring the locks, never against arrival time.
    current = integration_now(now)
    if (grant is None or grant.revoked_at is not None or credential.revoked_at is not None
            or current >= aware(credential.expires_at) or grant.audience != audience
            or credential.auth_session_version != auth.user.auth_session_version):
        raise IntegrationAccessError("integration_invalid_token", 401)
    if expected is not None and (
        grant.id != expected.grant_id or auth.user.id != expected.user_id
        or auth.membership.id != expected.membership_id
        or credential.auth_session_version != expected.auth_session_version
    ):
        raise IntegrationAccessError("integration_invalid_token", 401)
    features = integration_features(settings, organization_id)
    if credential.kind == "oauth":
        family = session.scalar(select(IntegrationOAuthFamily).where(
            IntegrationOAuthFamily.organization_id == organization_id,
            IntegrationOAuthFamily.grant_id == grant.id,
        ).execution_options(populate_existing=True))
        client = session.get(IntegrationOAuthClient, family.client_id, populate_existing=True) if family else None
        expected_resource = (settings.public_app_url or "").rstrip("/") + ("/v1/mcp" if audience == "mcp" else "/v1/integrations")
        if (not features["oauth"] or family is None or family.revoked_at is not None
                or current >= aware(family.expires_at) or client is None or client.disabled_at is not None
                or family.auth_session_version != credential.auth_session_version
                or family.resource != expected_resource or grant.kind != "oauth"):
            raise IntegrationAccessError("integration_invalid_token", 401)
    elif credential.kind != "pat" or grant.kind != "pat":
        raise IntegrationAccessError("integration_invalid_token", 401)
    if not features["api"] or (audience == "mcp" and not features["mcp"]) or policy is None or not policy.enabled:
        raise IntegrationAccessError("integrations_disabled", 403)
    ensure_integration_entitlement(auth, now=current)
    scopes = frozenset(grant.scopes)
    requested = frozenset(required_scopes)
    if (not scopes <= INTEGRATION_SCOPES or not scopes <= set(policy.allowed_scopes)
            or not requested <= scopes
            or (any(scope.startswith("analyses:") for scope in scopes) and not features["analyses"])):
        raise IntegrationAccessError("integration_scope_forbidden", 403)
    return IntegrationPrincipal(auth=auth, grant_id=grant.id, credential_id=credential.id,
        audience=audience, scopes=scopes, auth_session_version=credential.auth_session_version,
        required_scopes=requested)


def authenticate_integration_token(
    session: Session, *, token: str, audience: IntegrationAudience, settings: AppSettings,
    required_scopes: Collection[str] = (), now: datetime | None = None,
) -> IntegrationPrincipal:
    if audience not in {"rest", "mcp"} or not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise IntegrationAccessError("integration_invalid_token", 401)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    table = IntegrationCredential.__table__
    binding = session.execute(select(table.c.id, table.c.organization_id, table.c.token_digest).where(
        table.c.token_digest == digest,
    )).first()
    if binding is None or not hmac.compare_digest(digest, binding.token_digest):
        raise IntegrationAccessError("integration_invalid_token", 401)
    return _resolve_credential(session, credential_id=binding.id,
        organization_id=binding.organization_id, audience=audience, settings=settings,
        required_scopes=required_scopes, now=now, lock=False)


def revalidate_integration_principal(
    session: Session, principal: IntegrationPrincipal, *, settings: AppSettings,
    now: datetime | None = None, lock: bool = True,
) -> IntegrationPrincipal:
    assert_integration_context(session, principal.organization_id)
    return _resolve_credential(session, credential_id=principal.credential_id,
        organization_id=principal.organization_id, audience=principal.audience, settings=settings,
        required_scopes=principal.required_scopes, now=now, lock=lock, expected=principal)


def safe_metadata_id(value: str | None) -> str | None:
    try:
        return str(UUID(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def record_integration_audit(
    session: Session, *, organization_id: str, actor_user_id: str, action: str,
    resource_type: str, grant_id: str | None = None, credential_id: str | None = None,
    resource_id: str | None = None, resource_count: int = 0, candidate_count: int = 0,
    request_id: str | None = None, result: str = "success", reason_code: str | None = None,
    now: datetime | None = None,
) -> None:
    assert_integration_context(session, organization_id)
    # Only server-authored vocabulary and UUIDs may reach the fixed audit schema.
    for value, limit in ((action, 64), (resource_type, 32), (result, 24), (reason_code or "ok", 64)):
        if len(value) > limit or not re.fullmatch(r"[a-z][a-z0-9_.:-]*", value):
            raise IntegrationAccessError("integration_audit_unavailable", 503)
    current = integration_now(now)
    session.add(IntegrationAuditEvent(organization_id=organization_id, actor_user_id=actor_user_id,
        grant_id=safe_metadata_id(grant_id), credential_id=safe_metadata_id(credential_id),
        action=action, resource_type=resource_type, resource_id=safe_metadata_id(resource_id),
        resource_count=resource_count, candidate_count=candidate_count,
        request_id=safe_metadata_id(request_id), result=result, reason_code=reason_code,
        created_at=current, expires_at=current + timedelta(days=90)))
    session.flush()
