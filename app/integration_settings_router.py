"""Cookie-only integration management. Mounted explicitly by the application factory."""
from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import get_session
from app.integration_schemas import (
    IntegrationActivityList, IntegrationCredentialRotate, IntegrationGrantCreate,
    IntegrationGrantList, IntegrationIssuedCredential, IntegrationPolicyPatch,
    IntegrationSettingsResponse, IntegrationWorkspace,
)
from app.services.identity_service import AuthPrincipal, principal_from_session
from app.services.integration_auth_service import (
    IntegrationAccessError, reload_bound_auth,
)
from app.services.integration_management_service import (
    create_integration_grant, integration_csrf_token, integration_settings_response,
    list_integration_activity, list_integration_grants, revoke_integration_grant,
    rotate_integration_credential, update_integration_policy,
)

router = APIRouter(prefix="/v1/integration-settings", tags=["integration-settings"])
T = TypeVar("T")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _http_error(exc: IntegrationAccessError) -> HTTPException:
    headers = dict(_NO_STORE)
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return HTTPException(status_code=exc.status_code, detail=exc.code, headers=headers)


def _call(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except IntegrationAccessError as exc:
        raise _http_error(exc) from None
    except SQLAlchemyError:
        raise _http_error(IntegrationAccessError("integration_storage_unavailable", 503)) from None


def require_integration_browser_member(
    request: Request, response: Response, session: Session = Depends(get_session),
) -> AuthPrincipal:
    response.headers.update(_NO_STORE)
    if "authorization" in request.headers:
        raise _http_error(IntegrationAccessError("integration_browser_session_required", 401))

    def resolve() -> AuthPrincipal:
        principal = principal_from_session(session, request.session)
        if principal is None:
            raise IntegrationAccessError("authentication_required", 401)
        principal = reload_bound_auth(session, organization_id=principal.organization_id,
            user_id=principal.user.id, membership_id=principal.membership.id,
            auth_session_version=principal.user.auth_session_version)
        # No allow_unauthenticated/development principal here, even in test mode.
        integration_csrf_token(request.app.state.settings, principal, request.session)
        return principal

    return _call(resolve)


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(character.isspace() for character in value)):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def require_integration_csrf(
    request: Request, principal: AuthPrincipal = Depends(require_integration_browser_member),
) -> AuthPrincipal:
    settings: AppSettings = request.app.state.settings
    expected_origin = _origin(settings.public_app_url or str(request.base_url))
    actual_origin = _origin(request.headers.get("origin", ""))
    provided = request.headers.get("x-csrf-token", "")
    expected = _call(lambda: integration_csrf_token(settings, principal, request.session))
    if (expected_origin is None or actual_origin != expected_origin
            or request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}
            or len(provided) != len(expected) or not hmac.compare_digest(provided, expected)):
        raise _http_error(IntegrationAccessError("integration_csrf_failed", 403))
    return principal


@router.get("", response_model=IntegrationSettingsResponse)
def get_settings(request: Request, principal: AuthPrincipal = Depends(require_integration_browser_member),
    session: Session = Depends(get_session)) -> IntegrationSettingsResponse:
    return _call(lambda: integration_settings_response(session, principal,
        settings=request.app.state.settings, session_values=request.session))


@router.post("/grants", response_model=IntegrationIssuedCredential, status_code=201)
def create_grant(payload: IntegrationGrantCreate, request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)) -> IntegrationIssuedCredential:
    return _call(lambda: create_integration_grant(session, principal, settings=request.app.state.settings,
        payload=payload, request_id=getattr(request.state, "request_id", None)))


@router.post("/grants/{grant_id}/rotate", response_model=IntegrationIssuedCredential)
def rotate_credential(grant_id: str, payload: IntegrationCredentialRotate, request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)) -> IntegrationIssuedCredential:
    return _call(lambda: rotate_integration_credential(session, principal, grant_id=grant_id,
        settings=request.app.state.settings, payload=payload,
        request_id=getattr(request.state, "request_id", None)))


@router.delete("/grants/{grant_id}", status_code=204)
def revoke_grant(grant_id: str, request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)) -> None:
    _call(lambda: revoke_integration_grant(session, principal, grant_id=grant_id,
        request_id=getattr(request.state, "request_id", None)))


@router.patch("/workspace", response_model=IntegrationWorkspace)
def patch_workspace(payload: IntegrationPolicyPatch, request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)) -> IntegrationWorkspace:
    return _call(lambda: update_integration_policy(session, principal, settings=request.app.state.settings,
        payload=payload, request_id=getattr(request.state, "request_id", None)))


@router.get("/workspace/grants", response_model=IntegrationGrantList)
def workspace_grants(request: Request,
    principal: AuthPrincipal = Depends(require_integration_browser_member), session: Session = Depends(get_session)) -> IntegrationGrantList:
    return _call(lambda: IntegrationGrantList(items=list_integration_grants(session, principal,
        settings=request.app.state.settings, workspace_admin=True)))


@router.delete("/workspace/grants/{grant_id}", status_code=204)
def admin_revoke_grant(grant_id: str, request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)) -> None:
    _call(lambda: revoke_integration_grant(session, principal, grant_id=grant_id,
        workspace_admin=True, request_id=getattr(request.state, "request_id", None)))


@router.get("/activity", response_model=IntegrationActivityList)
def activity(limit: int = Query(default=50, ge=1, le=100), cursor: str | None = Query(default=None, max_length=36),
    principal: AuthPrincipal = Depends(require_integration_browser_member), session: Session = Depends(get_session)) -> IntegrationActivityList:
    return _call(lambda: list_integration_activity(session, principal, limit=limit, cursor=cursor))
