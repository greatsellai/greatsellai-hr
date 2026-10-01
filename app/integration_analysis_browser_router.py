"""Cookie-only owner-private analysis history for the integrations settings UI."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_session
from app.integration_analysis_schemas import (
    IntegrationAnalysisConfirmInput,
    IntegrationAnalysisConfirmed,
    IntegrationAnalysisDiscardInput,
    IntegrationAnalysisDraftDetail,
    IntegrationAnalysisDraftList,
    IntegrationAnalysisPendingDetail,
    IntegrationAnalysisPendingList,
)
from app.integration_settings_router import require_integration_browser_member, require_integration_csrf
from app.services.identity_service import AuthPrincipal
from app.services.integration_analysis_service import (
    IntegrationAnalysisError,
    confirm_pending_analysis_draft,
    discard_pending_analysis_draft,
    ensure_browser_analysis_access,
    execute_browser_analysis_read,
    get_analysis_draft,
    get_pending_analysis_draft,
    list_analysis_drafts,
    list_pending_analysis_drafts,
)
from app.services.integration_auth_service import IntegrationAccessError

router = APIRouter(
    prefix="/v1/integration-settings/analysis-reports",
    tags=["integration-settings"],
)
Result = TypeVar("Result")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _call(operation: Callable[[], Result]) -> Result:
    try:
        return operation()
    except (IntegrationAccessError, IntegrationAnalysisError) as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.code,
            headers=_NO_STORE,
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail="integration_storage_unavailable",
            headers=_NO_STORE,
        ) from None


@router.get("", response_model=IntegrationAnalysisDraftList)
def browser_analysis_reports(
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_browser_member),
    session: Session = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=2048),
) -> IntegrationAnalysisDraftList:
    def read() -> IntegrationAnalysisDraftList:
        ensure_browser_analysis_access(
            session,
            principal,
            settings=request.app.state.settings,
        )
        return list_analysis_drafts(
            session,
            principal,
            settings=request.app.state.settings,
            limit=limit,
            cursor=cursor,
        )

    return _call(lambda: execute_browser_analysis_read(session, principal,
        settings=request.app.state.settings, read=read))


@router.get("/pending", response_model=IntegrationAnalysisPendingList)
def browser_pending_analysis_reports(
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_browser_member),
    session: Session = Depends(get_session),
    limit: int = Query(default=10, ge=1, le=10),
) -> IntegrationAnalysisPendingList:
    return _call(lambda: execute_browser_analysis_read(
        session,
        principal,
        settings=request.app.state.settings,
        read=lambda: list_pending_analysis_drafts(session, principal, limit=limit),
        required_scope="analyses:write",
    ))


@router.get("/pending/{report_id}", response_model=IntegrationAnalysisPendingDetail)
def browser_pending_analysis_report(
    report_id: str,
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_browser_member),
    session: Session = Depends(get_session),
) -> IntegrationAnalysisPendingDetail:
    return _call(lambda: execute_browser_analysis_read(
        session,
        principal,
        settings=request.app.state.settings,
        read=lambda: get_pending_analysis_draft(session, principal, report_id=report_id),
        required_scope="analyses:write",
    ))


@router.post("/{report_id}/confirm", response_model=IntegrationAnalysisConfirmed)
def browser_confirm_analysis_report(
    report_id: str,
    payload: IntegrationAnalysisConfirmInput,
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf),
    session: Session = Depends(get_session),
) -> IntegrationAnalysisConfirmed:
    return _call(lambda: confirm_pending_analysis_draft(
        session,
        principal,
        report_id=report_id,
        version=payload.version,
        payload_sha256=payload.payload_sha256,
        settings=request.app.state.settings,
        request_id=getattr(request.state, "request_id", None),
    ))


@router.post("/{report_id}/discard", status_code=204)
def browser_discard_analysis_report(
    report_id: str,
    payload: IntegrationAnalysisDiscardInput,
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_csrf),
    session: Session = Depends(get_session),
) -> None:
    _call(lambda: discard_pending_analysis_draft(
        session,
        principal,
        report_id=report_id,
        version=payload.version,
        request_id=getattr(request.state, "request_id", None),
    ))


@router.get("/{report_id}", response_model=IntegrationAnalysisDraftDetail)
def browser_analysis_report(
    report_id: str,
    request: Request,
    principal: AuthPrincipal = Depends(require_integration_browser_member),
    session: Session = Depends(get_session),
) -> IntegrationAnalysisDraftDetail:
    def read() -> IntegrationAnalysisDraftDetail:
        ensure_browser_analysis_access(
            session,
            principal,
            settings=request.app.state.settings,
        )
        return get_analysis_draft(
            session,
            principal,
            report_id=report_id,
        )

    return _call(lambda: execute_browser_analysis_read(session, principal,
        settings=request.app.state.settings, read=read))
