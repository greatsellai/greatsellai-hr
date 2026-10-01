"""Bearer-only, tenant-scoped REST surface for approved integration reads."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import replace
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import get_session
from app.integration_analysis_schemas import (
    IntegrationAnalysisDraftDetail,
    IntegrationAnalysisDraftList,
    IntegrationAnalysisPrepare,
    IntegrationAnalysisPrepared,
)
from app.integration_read_schemas import (
    IntegrationCandidateAssessments,
    IntegrationCandidateEvidence,
    IntegrationCandidateEvidenceRequest,
    IntegrationCandidateProfile,
    IntegrationCandidateSearchRequest,
    IntegrationCandidateSearchResponse,
    IntegrationConnectionInfo,
    IntegrationFilterOptions,
    IntegrationJobList,
    IntegrationJobRequirements,
)
from app.services.integration_auth_service import (
    IntegrationAccessError,
    IntegrationPrincipal,
    authenticate_integration_token,
)
from app.services.integration_analysis_service import (
    IntegrationAnalysisError,
    analysis_candidate_ids,
    get_analysis_draft,
    list_analysis_drafts,
    required_save_scopes,
    prepare_analysis_draft,
)
from app.services.integration_read_service import (
    IntegrationReadError,
    execute_integration_read,
    get_candidate_assessments,
    get_candidate_evidence,
    get_candidate_profile,
    get_connection_info,
    get_filter_options,
    get_job_requirements,
    list_jobs,
    search_candidates,
)

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])
Result = TypeVar("Result")


def _error_headers(
    error: IntegrationAccessError | IntegrationReadError | IntegrationAnalysisError,
) -> dict[str, str]:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if isinstance(error, IntegrationAccessError):
        if error.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if error.retry_after is not None:
            headers["Retry-After"] = str(error.retry_after)
    return headers


def _raise_http(
    error: IntegrationAccessError | IntegrationReadError | IntegrationAnalysisError,
) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code},
        headers=_error_headers(error),
    ) from None


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise IntegrationAccessError("integration_authentication_required", 401)
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise IntegrationAccessError("integration_invalid_token", 401)
    return token


def _principal_dependency(required_scopes: Collection[str]):
    scopes = tuple(required_scopes)

    def dependency(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> IntegrationPrincipal:
        try:
            return authenticate_integration_token(
                session,
                token=_bearer_token(authorization),
                audience="rest",
                settings=request.app.state.settings,
                required_scopes=scopes,
            )
        except IntegrationAccessError as error:
            _raise_http(error)
        except SQLAlchemyError:
            # Driver diagnostics can contain bound values; never surface them
            # through a framework traceback for an external request.
            session.rollback()
            _raise_http(IntegrationAccessError("integration_storage_unavailable", 503))

    return dependency


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _run_read(
    *,
    request: Request,
    response: Response,
    session: Session,
    principal: IntegrationPrincipal,
    action: str,
    resource_type: str,
    read: Callable[[], Result],
    resource_ids: Callable[[Result], Collection[str]] = lambda _: (),
    candidate_ids: Callable[[Result], Collection[str]] = lambda _: (),
) -> Result:
    settings: AppSettings = request.app.state.settings
    try:
        result = execute_integration_read(
            session,
            principal=principal,
            settings=settings,
            action=action,
            resource_type=resource_type,
            read=read,
            resource_ids=resource_ids,
            candidate_ids=candidate_ids,
            request_id=getattr(request.state, "request_id", None),
        )
    except (
        IntegrationAccessError,
        IntegrationReadError,
        IntegrationAnalysisError,
    ) as error:
        _raise_http(error)
    except Exception:
        # Match the MCP boundary: a DB/serialization failure may carry draft
        # text or source data in its exception. Preserve a fixed failure code
        # without allowing the framework to log those values.
        session.rollback()
        _raise_http(IntegrationAccessError("integration_operation_unavailable", 503))
    _no_store(response)
    return result


@router.get("/connection", response_model=IntegrationConnectionInfo)
def connection(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[IntegrationPrincipal, Depends(_principal_dependency(()))],
) -> IntegrationConnectionInfo:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.connection.read",
        resource_type="connection",
        read=lambda: get_connection_info(principal, request.app.state.settings),
    )


@router.get("/filter-options", response_model=IntegrationFilterOptions)
def filter_options(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("candidates:read",))),
    ],
) -> IntegrationFilterOptions:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.filter_options.read",
        resource_type="filter_options",
        read=get_filter_options,
    )


@router.post("/candidates/search", response_model=IntegrationCandidateSearchResponse)
def candidates_search(
    payload: IntegrationCandidateSearchRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("candidates:read",))),
    ],
) -> IntegrationCandidateSearchResponse:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.candidates.search",
        resource_type="candidate_search",
        read=lambda: search_candidates(
            session,
            principal=principal,
            settings=request.app.state.settings,
            request=payload,
        ),
        resource_ids=lambda value: [item.resume_id for item in value.items],
        candidate_ids=lambda value: [item.candidate_id for item in value.items],
    )


@router.get("/candidates/{candidate_id}", response_model=IntegrationCandidateProfile)
def candidate_profile(
    candidate_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("candidates:read",))),
    ],
) -> IntegrationCandidateProfile:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.candidate.profile.read",
        resource_type="candidate_profile",
        read=lambda: get_candidate_profile(session, candidate_id=candidate_id),
        resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
        candidate_ids=lambda value: (value.candidate_id,),
    )


@router.post(
    "/candidates/{candidate_id}/evidence",
    response_model=IntegrationCandidateEvidence,
)
def candidate_evidence(
    candidate_id: str,
    payload: IntegrationCandidateEvidenceRequest,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("candidates:read", "evidence:read"))),
    ],
) -> IntegrationCandidateEvidence:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.candidate.evidence.read",
        resource_type="candidate_evidence",
        read=lambda: get_candidate_evidence(
            session, candidate_id=candidate_id, request=payload
        ),
        resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
        candidate_ids=lambda value: (value.candidate_id,),
    )


@router.get(
    "/candidates/{candidate_id}/assessments",
    response_model=IntegrationCandidateAssessments,
)
def candidate_assessments(
    candidate_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("candidates:read", "assessments:read"))),
    ],
) -> IntegrationCandidateAssessments:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.candidate.assessments.read",
        resource_type="candidate_assessment",
        read=lambda: get_candidate_assessments(session, candidate_id=candidate_id),
        resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
        candidate_ids=lambda value: (value.candidate_id,),
    )


@router.get("/jobs", response_model=IntegrationJobList)
def jobs(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("jobs:read",))),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> IntegrationJobList:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.jobs.list",
        resource_type="job_list",
        read=lambda: list_jobs(
            session,
            principal=principal,
            settings=request.app.state.settings,
            limit=limit,
            cursor=cursor,
        ),
        resource_ids=lambda value: [item.job_id for item in value.items],
    )


@router.get(
    "/jobs/{job_id}/versions/{version_id}",
    response_model=IntegrationJobRequirements,
)
def job_requirements(
    job_id: str,
    version_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("jobs:read",))),
    ],
) -> IntegrationJobRequirements:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.job.requirements.read",
        resource_type="job_requirements",
        read=lambda: get_job_requirements(
            session, job_id=job_id, version_id=version_id
        ),
        resource_ids=lambda value: (value.job_id, value.job_version_id),
    )


@router.post("/analysis-reports", response_model=IntegrationAnalysisPrepared, status_code=202)
def analysis_report_save(
    payload: IntegrationAnalysisPrepare,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("analyses:write",))),
    ],
) -> IntegrationAnalysisPrepared:
    scoped_principal = replace(
        principal,
        required_scopes=required_save_scopes(payload),
    )
    candidate_ids = tuple(str(item.candidate_id) for item in payload.candidates)
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=scoped_principal,
        action="integration.analysis.prepare",
        resource_type="analysis_confirmation",
        read=lambda: prepare_analysis_draft(
            session,
            scoped_principal,
            payload=payload,
        ),
        resource_ids=lambda value: (value.id,),
        candidate_ids=lambda _value: candidate_ids,
    )


@router.get("/analysis-reports", response_model=IntegrationAnalysisDraftList)
def analysis_reports(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("analyses:read",))),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> IntegrationAnalysisDraftList:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.analysis.list",
        resource_type="analysis_draft_list",
        read=lambda: list_analysis_drafts(
            session,
            principal,
            settings=request.app.state.settings,
            limit=limit,
            cursor=cursor,
        ),
        resource_ids=lambda value: [item.id for item in value.items],
        candidate_ids=analysis_candidate_ids,
    )


@router.get(
    "/analysis-reports/{report_id}",
    response_model=IntegrationAnalysisDraftDetail,
)
def analysis_report(
    report_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[
        IntegrationPrincipal,
        Depends(_principal_dependency(("analyses:read",))),
    ],
) -> IntegrationAnalysisDraftDetail:
    return _run_read(
        request=request,
        response=response,
        session=session,
        principal=principal,
        action="integration.analysis.read",
        resource_type="analysis_draft",
        read=lambda: get_analysis_draft(session, principal, report_id=report_id),
        resource_ids=lambda value: (value.id,),
        candidate_ids=analysis_candidate_ids,
    )
