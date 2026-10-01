"""OAuth protocol endpoints and same-origin cookie/CSRF consent endpoints."""
from __future__ import annotations

import json
from urllib.parse import parse_qsl

from authlib.oauth2.rfc6749.errors import OAuth2Error
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_session
from app.integration_oauth_schemas import (OAuthConsentDecision, OAuthConsentRedirect, OAuthConsentResponse,
    OAuthErrorResponse, OAuthMetadata, OAuthRegistrationResponse, OAuthTokenResponse)
from app.integration_settings_router import _call, require_integration_browser_member, require_integration_csrf
from app.services.identity_service import AuthPrincipal
from app.services.integration_auth_service import IntegrationAccessError
from app.services.integration_oauth_service import (NO_STORE, consent_view, decide_consent,
    exchange_token, oauth_metadata, preauth_budget, register_public_client, revoke_oauth_token, start_consent)
from app.trusted_proxy import client_rate_limit_identifier

router = APIRouter(tags=["integration-oauth"])


def _error(exc):
    headers = dict(NO_STORE)
    if isinstance(exc, IntegrationAccessError):
        status, code = exc.status_code, exc.code
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
    elif isinstance(exc, OAuth2Error):
        status, code = exc.status_code, exc.error
    else:
        status, code = 503, "oauth_storage_unavailable"
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise IntegrationAccessError("invalid_request", 400)
        data[key] = value
    return data


async def _body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise IntegrationAccessError("invalid_request", 413)
    return bytes(body)


async def _form(request):
    if "authorization" in request.headers or request.query_params:
        raise IntegrationAccessError("invalid_request", 400)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise IntegrationAccessError("invalid_request", 415)
    try:
        return _unique_pairs(parse_qsl((await _body(request)).decode("utf-8"), keep_blank_values=True,
            strict_parsing=True, errors="strict", max_num_fields=16))
    except (ValueError, UnicodeError):
        raise IntegrationAccessError("invalid_request", 400) from None


def _budget(session, request, operation):
    preauth_budget(session, settings=request.app.state.settings, operation=operation,
        peer=client_rate_limit_identifier(request, request.app.state.settings.trusted_proxy_cidrs))


@router.get("/.well-known/oauth-authorization-server", response_model=OAuthMetadata)
def metadata(request: Request, response: Response):
    response.headers.update(NO_STORE)
    return _call(lambda: oauth_metadata(request.app.state.settings))


@router.post("/v1/oauth/register", response_model=OAuthRegistrationResponse, status_code=201)
async def register(request: Request, session: Session = Depends(get_session)):
    try:
        if "authorization" in request.headers or request.query_params:
            raise IntegrationAccessError("invalid_request", 400)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise IntegrationAccessError("invalid_request", 415)
        try:
            payload = json.loads(await _body(request), object_pairs_hook=_unique_pairs)
        except (ValueError, UnicodeError):
            raise IntegrationAccessError("invalid_client_metadata", 400) from None
        if not isinstance(payload, dict):
            raise IntegrationAccessError("invalid_client_metadata", 400)
        def operation():
            _budget(session, request, "register")
            return register_public_client(session, payload)
        result = await run_in_threadpool(operation)
        return JSONResponse(OAuthRegistrationResponse.model_validate(result).model_dump(), status_code=201, headers=NO_STORE)
    except (IntegrationAccessError, OAuth2Error, SQLAlchemyError) as exc:
        session.rollback()
        if isinstance(exc, IntegrationAccessError) and exc.code == "oauth_redirect_invalid":
            # RFC 7591 section 3.2.2; keep shared redirect validation unchanged.
            return _error(IntegrationAccessError("invalid_redirect_uri", 400))
        return _error(exc)


@router.get("/v1/oauth/authorize")
def authorize(request: Request, session: Session = Depends(get_session)):
    try:
        if "authorization" in request.headers or len(request.scope.get("query_string", b"")) > 8192:
            raise IntegrationAccessError("invalid_request", 400)
        data = _unique_pairs(request.query_params.multi_items())
        _budget(session, request, "authorize")
        target = start_consent(session, settings=request.app.state.settings, data=data)
        return RedirectResponse(target, status_code=303, headers=NO_STORE)
    except (IntegrationAccessError, OAuth2Error, SQLAlchemyError) as exc:
        session.rollback()
        return _error(exc)


@router.post("/v1/oauth/token", response_model=OAuthTokenResponse | OAuthErrorResponse)
async def token(request: Request, session: Session = Depends(get_session)):
    try:
        data = await _form(request)
        def operation():
            _budget(session, request, "token")
            return exchange_token(session, settings=request.app.state.settings, data=data)
        status, body, headers = await run_in_threadpool(operation)
        dto = OAuthTokenResponse.model_validate(body) if status == 200 else OAuthErrorResponse.model_validate(body)
        return JSONResponse(dto.model_dump(exclude_none=True), status_code=status, headers={**headers, **NO_STORE})
    except (IntegrationAccessError, OAuth2Error, SQLAlchemyError) as exc:
        session.rollback()
        return _error(exc)


@router.post("/v1/oauth/revoke", status_code=200)
async def revoke(request: Request, session: Session = Depends(get_session)):
    try:
        data = await _form(request)
        def operation():
            _budget(session, request, "revoke")
            revoke_oauth_token(session, settings=request.app.state.settings, data=data)
        await run_in_threadpool(operation)
        return Response(status_code=200, headers=NO_STORE)
    except (IntegrationAccessError, OAuth2Error, SQLAlchemyError) as exc:
        session.rollback()
        return _error(exc)


@router.get("/v1/integration-settings/oauth/consents/{request_id}", response_model=OAuthConsentResponse)
def get_consent(request_id: str, request: Request, response: Response,
    auth: AuthPrincipal = Depends(require_integration_browser_member), session: Session = Depends(get_session)):
    response.headers.update(NO_STORE)
    return _call(lambda: consent_view(session, auth, settings=request.app.state.settings,
        values=request.session, request_id=request_id))


@router.post("/v1/integration-settings/oauth/consents/{request_id}", response_model=OAuthConsentRedirect)
def post_consent(request_id: str, payload: OAuthConsentDecision, request: Request, response: Response,
    auth: AuthPrincipal = Depends(require_integration_csrf), session: Session = Depends(get_session)):
    response.headers.update(NO_STORE)
    return _call(lambda: decide_consent(session, auth, settings=request.app.state.settings,
        values=request.session, request_id=request_id, approve=payload.approve, approved_scopes=payload.approved_scopes))
