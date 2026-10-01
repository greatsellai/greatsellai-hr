"""Browser-owned PAT management, transactionally audited and separate from bearer APIs."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.integration_schemas import (
    IntegrationActivity, IntegrationActivityList, IntegrationCredentialRotate,
    IntegrationGrantCreate, IntegrationGrantSummary, IntegrationIssuedCredential,
    IntegrationPolicyPatch, IntegrationSettingsResponse, IntegrationWorkspace,
)
from app.models import (
    IntegrationAuditEvent, IntegrationCredential, IntegrationGrant, IntegrationWorkspacePolicy, IntegrationOAuthFamily, IntegrationOAuthClient,
    OrganizationMembership, UserAccount,
)
from app.services.identity_service import AuthPrincipal
from app.services.integration_auth_service import (
    DEFAULT_INTEGRATION_SCOPES, INTEGRATION_SCOPES, IntegrationAccessError, aware,
    ensure_integration_entitlement, integration_features, integration_now,
    lock_integration_policy, record_integration_audit, reload_bound_auth,
)


def integration_csrf_token(settings: AppSettings, principal: AuthPrincipal, values: dict[str, object]) -> str:
    nonce = values.get("resume_v3_session_nonce")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise IntegrationAccessError("integration_reauthentication_required", 401)
    binding = json.dumps(["integration-management-v1", nonce, principal.user.id,
        principal.membership.id, principal.organization_id, principal.user.auth_session_version], separators=(",", ":"))
    return hmac.new(settings.session_signing_secret().encode(), binding.encode(), hashlib.sha256).hexdigest()


def _refresh_browser(session: Session, principal: AuthPrincipal, *, lock: bool = False) -> AuthPrincipal:
    return reload_bound_auth(session, organization_id=principal.organization_id,
        user_id=principal.user.id, membership_id=principal.membership.id,
        auth_session_version=principal.user.auth_session_version, lock=lock)


def _policy(session: Session, organization_id: str) -> IntegrationWorkspacePolicy | None:
    return session.scalar(select(IntegrationWorkspacePolicy).where(
        IntegrationWorkspacePolicy.organization_id == organization_id,
    ).execution_options(populate_existing=True))


def _policy_response(principal: AuthPrincipal, policy: IntegrationWorkspacePolicy | None, settings: AppSettings) -> IntegrationWorkspace:
    return IntegrationWorkspace(organization_id=principal.organization_id, name=principal.organization.name,
        enabled=bool(policy and policy.enabled),
        allowed_scopes=sorted(policy.allowed_scopes if policy else DEFAULT_INTEGRATION_SCOPES),
        available_scopes=sorted(scope for scope in INTEGRATION_SCOPES
            if not scope.startswith("analyses:") or settings.integrations_analysis_enabled))


def _can_use(principal: AuthPrincipal, now: datetime) -> bool:
    try:
        ensure_integration_entitlement(principal, now=now)
        return True
    except IntegrationAccessError:
        return False


def _summary(
    session: Session, principal: AuthPrincipal, grant: IntegrationGrant,
    policy: IntegrationWorkspacePolicy | None, *, settings: AppSettings, now: datetime,
) -> IntegrationGrantSummary:
    credential = session.scalar(select(IntegrationCredential).where(
        IntegrationCredential.organization_id == principal.organization_id,
        IntegrationCredential.grant_id == grant.id,
    ).order_by(IntegrationCredential.created_at.desc(), IntegrationCredential.id.desc()).limit(1))
    family = session.scalar(select(IntegrationOAuthFamily).where(
        IntegrationOAuthFamily.organization_id == principal.organization_id,
        IntegrationOAuthFamily.grant_id == grant.id)) if grant.kind == "oauth" else None
    if credential is None and family is None:
        if grant.kind != "oauth":
            raise IntegrationAccessError("integration_storage_unavailable", 503)
        # Retention may remove terminal OAuth token material while a private
        # analysis draft still references its source grant. Keep that history
        # visible as inert, never as a live connection.
        terminal_at = grant.revoked_at or grant.updated_at
        return IntegrationGrantSummary(
            id=grant.id, name=grant.name, kind=grant.kind, audience=grant.audience,
            scopes=grant.scopes, status="revoked" if grant.revoked_at else "expired",
            token_prefix="", created_at=aware(grant.created_at),
            expires_at=aware(terminal_at), last_used_at=None,
            revoked_at=aware(grant.revoked_at) if grant.revoked_at else None,
        )
    expires_at = family.expires_at if family is not None else credential.expires_at
    session_version = family.auth_session_version if family is not None else credential.auth_session_version
    oauth_client = session.get(IntegrationOAuthClient, family.client_id, populate_existing=True) if family else None
    features = integration_features(settings, principal.organization_id)
    owner = session.scalar(select(UserAccount).where(UserAccount.id == grant.user_id).execution_options(populate_existing=True))
    member = session.scalar(select(OrganizationMembership).where(
        OrganizationMembership.id == grant.membership_id, OrganizationMembership.user_id == grant.user_id,
        OrganizationMembership.organization_id == grant.organization_id,
    ).execution_options(populate_existing=True))
    blocked = (not features["api"] or (grant.audience == "mcp" and not features["mcp"])
        or policy is None or not policy.enabled or not set(grant.scopes) <= set(policy.allowed_scopes)
        or (any(scope.startswith("analyses:") for scope in grant.scopes) and not features["analyses"])
        or owner is None or not owner.is_active or owner.email_verified_at is None
        or member is None or not member.is_active or owner.auth_session_version != session_version
        or (family is not None and (credential is None or family.revoked_at is not None or not features["oauth"]
            or oauth_client is None or oauth_client.disabled_at is not None))
        or (family is None and credential.revoked_at is not None) or not _can_use(principal, now))
    status = ("revoked" if grant.revoked_at is not None else "expired" if aware(expires_at) <= now
        else "blocked" if blocked else "active")
    return IntegrationGrantSummary(id=grant.id, name=grant.name, kind=grant.kind, audience=grant.audience,
        scopes=grant.scopes, status=status, token_prefix=credential.token_prefix if credential else "",
        created_at=aware(grant.created_at), expires_at=aware(expires_at),
        last_used_at=aware(credential.last_used_at) if credential and credential.last_used_at else None,
        revoked_at=aware(grant.revoked_at) if grant.revoked_at else None)


def list_integration_grants(
    session: Session, principal: AuthPrincipal, *, settings: AppSettings,
    workspace_admin: bool = False, now: datetime | None = None,
) -> list[IntegrationGrantSummary]:
    principal = _refresh_browser(session, principal)
    if workspace_admin and principal.role != "admin":
        raise IntegrationAccessError("organization_admin_required", 403)
    statement = select(IntegrationGrant).where(IntegrationGrant.organization_id == principal.organization_id)
    if not workspace_admin:
        statement = statement.where(IntegrationGrant.user_id == principal.user.id,
            IntegrationGrant.membership_id == principal.membership.id)
    policy = _policy(session, principal.organization_id)
    return [_summary(session, principal, grant, policy, settings=settings, now=integration_now(now))
        for grant in session.scalars(statement.order_by(IntegrationGrant.created_at.desc(), IntegrationGrant.id.desc())).all()]


def integration_settings_response(
    session: Session, principal: AuthPrincipal, *, settings: AppSettings,
    session_values: dict[str, object], now: datetime | None = None,
) -> IntegrationSettingsResponse:
    principal = _refresh_browser(session, principal)
    current = integration_now(now)
    policy = _policy(session, principal.organization_id)
    features = integration_features(settings, principal.organization_id)
    base = (settings.public_app_url or "").rstrip("/")
    return IntegrationSettingsResponse(
        user={"id": principal.user.id, "display_name": principal.user.full_name, "email": principal.user.email},
        workspace=_policy_response(principal, policy, settings), features=features,
        endpoints={"api_base_url": base + "/v1/integrations", "mcp_url": base + "/v1/mcp"},
        permissions={"can_create": features["api"] and bool(policy and policy.enabled) and _can_use(principal, current),
            "can_admin": principal.role == "admin", "can_revoke_own": True},
        csrf_token=integration_csrf_token(settings, principal, session_values),
        grants=list_integration_grants(session, principal, settings=settings, now=current))


def _ensure_issuance(
    principal: AuthPrincipal, policy: IntegrationWorkspacePolicy, *, settings: AppSettings,
    audience: str, scopes: list[str], now: datetime,
) -> None:
    ensure_integration_entitlement(principal, now=now)
    features = integration_features(settings, principal.organization_id)
    if not features["api"] or not policy.enabled or (audience == "mcp" and not features["mcp"]):
        raise IntegrationAccessError("integrations_disabled", 403)
    if (audience not in {"rest", "mcp"} or not scopes or not set(scopes) <= INTEGRATION_SCOPES
            or not set(scopes) <= set(policy.allowed_scopes)
            or (any(scope.startswith("analyses:") for scope in scopes) and not features["analyses"])):
        raise IntegrationAccessError("integration_scope_forbidden", 403)


def _new_credential(session: Session, principal: AuthPrincipal, grant: IntegrationGrant, *, days: int, now: datetime) -> str:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise IntegrationAccessError("integration_expiry_invalid", 422)
    public_id = secrets.token_hex(8)
    token = f"gs_pat_{public_id}_{secrets.token_urlsafe(32)}"
    session.add(IntegrationCredential(organization_id=principal.organization_id, grant_id=grant.id,
        token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(), token_prefix=f"gs_pat_{public_id}",
        auth_session_version=principal.user.auth_session_version, expires_at=now + timedelta(days=days), created_at=now))
    session.flush()
    return token


def create_integration_grant(
    session: Session, principal: AuthPrincipal, *, settings: AppSettings,
    payload: IntegrationGrantCreate, request_id: str | None = None, now: datetime | None = None,
) -> IntegrationIssuedCredential:
    current = integration_now(now)
    try:
        principal = _refresh_browser(session, principal, lock=True)
        policy = lock_integration_policy(session, principal.organization_id)
        current = integration_now(now)
        _ensure_issuance(principal, policy, settings=settings, audience=payload.audience, scopes=payload.scopes, now=current)
        grant = IntegrationGrant(organization_id=principal.organization_id, user_id=principal.user.id,
            membership_id=principal.membership.id, audience=payload.audience, name=payload.name,
            scopes=sorted(set(payload.scopes)), created_at=current, updated_at=current)
        session.add(grant)
        session.flush()
        token = _new_credential(session, principal, grant, days=payload.expires_in_days, now=current)
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user.id, grant_id=grant.id, action="grant.created", resource_type="grant",
            resource_id=grant.id, resource_count=1, request_id=request_id, now=current)
        response = IntegrationIssuedCredential(grant=_summary(session, principal, grant, policy, settings=settings, now=current), token=token)
        session.commit()
        return response
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_audit_unavailable", 503) from None


def _owned_grant(session: Session, principal: AuthPrincipal, grant_id: str, *, workspace_admin: bool = False) -> IntegrationGrant:
    statement = select(IntegrationGrant).where(IntegrationGrant.id == grant_id,
        IntegrationGrant.organization_id == principal.organization_id)
    if workspace_admin:
        if principal.role != "admin":
            raise IntegrationAccessError("organization_admin_required", 403)
    else:
        statement = statement.where(IntegrationGrant.user_id == principal.user.id,
            IntegrationGrant.membership_id == principal.membership.id)
    grant = session.scalar(statement.execution_options(populate_existing=True))
    if grant is None:
        raise IntegrationAccessError("integration_grant_not_found", 404)
    return grant


def rotate_integration_credential(
    session: Session, principal: AuthPrincipal, *, grant_id: str, settings: AppSettings,
    payload: IntegrationCredentialRotate, request_id: str | None = None, now: datetime | None = None,
) -> IntegrationIssuedCredential:
    current = integration_now(now)
    try:
        principal = _refresh_browser(session, principal, lock=True)
        policy = lock_integration_policy(session, principal.organization_id)
        current = integration_now(now)
        grant = _owned_grant(session, principal, grant_id)
        if grant.kind != "pat":
            raise IntegrationAccessError("oauth_connection_reauthorization_required", 409)
        if grant.revoked_at is not None:
            raise IntegrationAccessError("integration_grant_revoked", 409)
        _ensure_issuance(principal, policy, settings=settings, audience=grant.audience, scopes=grant.scopes, now=current)
        session.execute(update(IntegrationCredential).where(
            IntegrationCredential.organization_id == principal.organization_id,
            IntegrationCredential.grant_id == grant.id, IntegrationCredential.revoked_at.is_(None),
        ).values(revoked_at=current))
        token = _new_credential(session, principal, grant, days=payload.expires_in_days, now=current)
        grant.updated_at = current
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user.id, grant_id=grant.id, action="grant.rotated", resource_type="grant",
            resource_id=grant.id, resource_count=1, request_id=request_id, now=current)
        response = IntegrationIssuedCredential(grant=_summary(session, principal, grant, policy, settings=settings, now=current), token=token)
        session.commit()
        return response
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_audit_unavailable", 503) from None


def revoke_integration_grant(
    session: Session, principal: AuthPrincipal, *, grant_id: str,
    workspace_admin: bool = False, request_id: str | None = None, now: datetime | None = None,
) -> None:
    current = integration_now(now)
    try:
        principal = _refresh_browser(session, principal, lock=True)
        lock_integration_policy(session, principal.organization_id)
        grant = _owned_grant(session, principal, grant_id, workspace_admin=workspace_admin)
        grant.revoked_at = grant.revoked_at or current
        grant.updated_at = current
        session.execute(update(IntegrationCredential).where(
            IntegrationCredential.organization_id == principal.organization_id,
            IntegrationCredential.grant_id == grant.id, IntegrationCredential.revoked_at.is_(None),
        ).values(revoked_at=current))
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user.id, grant_id=grant.id,
            action="grant.admin_revoked" if workspace_admin else "grant.revoked", resource_type="grant",
            resource_id=grant.id, resource_count=1, request_id=request_id, now=current)
        session.commit()
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_audit_unavailable", 503) from None


def update_integration_policy(
    session: Session, principal: AuthPrincipal, *, settings: AppSettings,
    payload: IntegrationPolicyPatch, request_id: str | None = None, now: datetime | None = None,
) -> IntegrationWorkspace:
    current = integration_now(now)
    try:
        principal = _refresh_browser(session, principal, lock=True)
        if principal.role != "admin":
            raise IntegrationAccessError("organization_admin_required", 403)
        previous = _policy(session, principal.organization_id)
        previous_scopes = set(previous.allowed_scopes if previous else DEFAULT_INTEGRATION_SCOPES)
        if payload.enabled or set(payload.allowed_scopes) - previous_scopes:
            ensure_integration_entitlement(principal, now=current)
            if not integration_features(settings, principal.organization_id)["api"]:
                raise IntegrationAccessError("integrations_disabled", 403)
        insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
        session.execute(insert(IntegrationWorkspacePolicy).values(organization_id=principal.organization_id,
            enabled=False, allowed_scopes=sorted(DEFAULT_INTEGRATION_SCOPES),
            policy_version=1, lock_version=0, updated_at=current).on_conflict_do_nothing(index_elements=["organization_id"]))
        policy = lock_integration_policy(session, principal.organization_id)
        policy.enabled = payload.enabled
        policy.allowed_scopes = sorted(set(payload.allowed_scopes))
        policy.policy_version += 1
        policy.updated_at = current
        record_integration_audit(session, organization_id=principal.organization_id,
            actor_user_id=principal.user.id, action="workspace.policy_updated", resource_type="workspace",
            resource_id=principal.organization_id, resource_count=1, request_id=request_id, now=current)
        response = _policy_response(principal, policy, settings)
        session.commit()
        return response
    except IntegrationAccessError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise IntegrationAccessError("integration_audit_unavailable", 503) from None


def list_integration_activity(
    session: Session, principal: AuthPrincipal, *, limit: int = 50,
    cursor: str | None = None, now: datetime | None = None,
) -> IntegrationActivityList:
    principal = _refresh_browser(session, principal)
    if not 1 <= limit <= 100:
        raise IntegrationAccessError("integration_pagination_invalid", 422)
    filters = [IntegrationAuditEvent.organization_id == principal.organization_id,
        IntegrationAuditEvent.actor_user_id == principal.user.id,
        IntegrationAuditEvent.expires_at > integration_now(now)]
    if cursor:
        anchor = session.scalar(select(IntegrationAuditEvent).where(*filters, IntegrationAuditEvent.id == cursor))
        if anchor is None:
            raise IntegrationAccessError("integration_pagination_invalid", 422)
        filters.append(or_(IntegrationAuditEvent.created_at < anchor.created_at,
            and_(IntegrationAuditEvent.created_at == anchor.created_at, IntegrationAuditEvent.id < anchor.id)))
    rows = session.scalars(select(IntegrationAuditEvent).where(*filters).order_by(
        IntegrationAuditEvent.created_at.desc(), IntegrationAuditEvent.id.desc()).limit(limit + 1)).all()
    return IntegrationActivityList(items=[IntegrationActivity(id=row.id, grant_id=row.grant_id,
        action=row.action, resource_type=row.resource_type, resource_count=row.resource_count,
        candidate_count=row.candidate_count, result=row.result, reason_code=row.reason_code,
        created_at=aware(row.created_at)) for row in rows[:limit]],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None)
