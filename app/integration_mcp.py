"""Official MCP SDK adapter over the shared integration read service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TypeVar
from urllib.parse import urlparse

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from app.config import AppSettings
from app.database import Database
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
    get_analysis_draft as read_analysis_draft,
    list_analysis_drafts as read_analysis_drafts,
    required_save_scopes,
    prepare_analysis_draft as write_analysis_draft,
)
from app.services.integration_read_service import (
    IntegrationReadError,
    execute_integration_read,
    get_candidate_assessments as read_candidate_assessments,
    get_candidate_evidence as read_candidate_evidence,
    get_candidate_profile as read_candidate_profile,
    get_connection_info as read_connection_info,
    get_filter_options as read_filter_options,
    get_job_requirements as read_job_requirements,
    list_jobs as read_jobs,
    search_candidates as read_candidates,
)
from app.services.integration_oauth_service import oauth_supported_scopes

MCP_PATH = "/v1/mcp"
ToolResult = TypeVar("ToolResult")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True
)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True
)


class _IntegrationTokenVerifier(TokenVerifier):
    def __init__(
        self, database: Database, settings: AppSettings, resource_url: str
    ) -> None:
        self.database = database
        self.settings = settings
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        def verify_in_worker() -> AccessToken | None:
            with self.database.session_factory() as session:
                try:
                    principal = authenticate_integration_token(
                        session,
                        token=token,
                        audience="mcp",
                        settings=self.settings,
                    )
                except IntegrationAccessError:
                    return None
            return AccessToken(
                token=token,
                client_id=principal.grant_id,
                scopes=sorted(principal.scopes),
                subject=principal.user_id,
                resource=self.resource_url,
                claims={
                    "organization_id": principal.organization_id,
                    "membership_id": principal.membership_id,
                    "credential_id": principal.credential_id,
                },
            )

        return await anyio.to_thread.run_sync(verify_in_worker)


def _current_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise IntegrationAccessError("integration_authentication_required", 401)
    return access_token.token


def _run_tool(
    *,
    database: Database,
    settings: AppSettings,
    required_scopes: tuple[str, ...],
    action: str,
    resource_type: str,
    read,
    resource_ids=lambda _: (),
    candidate_ids=lambda _: (),
):
    try:
        with database.session_factory() as session:
            principal: IntegrationPrincipal = authenticate_integration_token(
                session,
                token=_current_token(),
                audience="mcp",
                settings=settings,
                required_scopes=required_scopes,
            )
            return execute_integration_read(
                session,
                principal=principal,
                settings=settings,
                action=action,
                resource_type=resource_type,
                read=lambda: read(session, principal),
                resource_ids=resource_ids,
                candidate_ids=candidate_ids,
            )
    except (
        IntegrationAccessError,
        IntegrationReadError,
        IntegrationAnalysisError,
    ) as error:
        # Deliberate SDK errors expose only the stable public code, never a
        # traceback from the private data operation or session teardown.
        raise ToolError(error.code) from None
    except Exception:
        # DB/driver exceptions can include SQL-bound draft content. Convert at
        # the shared tool boundary before the SDK logs unexpected errors, while
        # retaining a public failure signal and the SDK's normal failure log.
        raise ToolError("integration_operation_unavailable") from None


def build_integration_mcp_server(
    *,
    database: Database,
    settings: AppSettings,
) -> MCPServer | None:
    """Create the eight-tool server only while both integration flags are enabled."""

    if not settings.integrations_enabled or not settings.integrations_mcp_enabled:
        return None
    issuer = settings.public_app_url.rstrip("/")
    resource_url = f"{issuer}{MCP_PATH}"
    server = MCPServer(
        "GreatSell HR integrations",
        token_verifier=_IntegrationTokenVerifier(database, settings, resource_url),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=[],
            validate_token_resource=True,
        ),
    )

    @server.tool(name="get_connection_info", annotations=READ_ONLY)
    def get_connection_info() -> IntegrationConnectionInfo:
        """Return the authenticated connection, audience and granted scopes."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=(),
            action="integration.connection.read",
            resource_type="connection",
            read=lambda _session, principal: read_connection_info(principal, settings),
        )

    @server.tool(name="get_filter_options", annotations=READ_ONLY)
    def get_filter_options() -> IntegrationFilterOptions:
        """Return the approved job-related candidate filter vocabulary."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("candidates:read",),
            action="integration.filter_options.read",
            resource_type="filter_options",
            read=lambda _session, _principal: read_filter_options(),
        )

    @server.tool(name="search_candidates", annotations=READ_ONLY)
    def search_candidates(
        request: IntegrationCandidateSearchRequest,
    ) -> IntegrationCandidateSearchResponse:
        """Search current eligible candidate facts without identity or contact data."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("candidates:read",),
            action="integration.candidates.search",
            resource_type="candidate_search",
            read=lambda session, principal: read_candidates(
                session,
                principal=principal,
                settings=settings,
                request=request,
            ),
            resource_ids=lambda value: [item.resume_id for item in value.items],
            candidate_ids=lambda value: [item.candidate_id for item in value.items],
        )

    @server.tool(name="get_candidate_profile", annotations=READ_ONLY)
    def get_candidate_profile(candidate_id: str) -> IntegrationCandidateProfile:
        """Return the current whitelisted fact snapshot for one candidate."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("candidates:read",),
            action="integration.candidate.profile.read",
            resource_type="candidate_profile",
            read=lambda session, _principal: read_candidate_profile(
                session,
                candidate_id=candidate_id,
            ),
            resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
            candidate_ids=lambda value: (value.candidate_id,),
        )

    @server.tool(name="get_candidate_evidence", annotations=READ_ONLY)
    def get_candidate_evidence(
        candidate_id: str,
        request: IntegrationCandidateEvidenceRequest,
    ) -> IntegrationCandidateEvidence:
        """Return bounded redacted excerpts for cited blocks only."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("candidates:read", "evidence:read"),
            action="integration.candidate.evidence.read",
            resource_type="candidate_evidence",
            read=lambda session, _principal: read_candidate_evidence(
                session,
                candidate_id=candidate_id,
                request=request,
            ),
            resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
            candidate_ids=lambda value: (value.candidate_id,),
        )

    @server.tool(name="get_candidate_assessments", annotations=READ_ONLY)
    def get_candidate_assessments(candidate_id: str) -> IntegrationCandidateAssessments:
        """Return current deterministic score and job-match projections."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("candidates:read", "assessments:read"),
            action="integration.candidate.assessments.read",
            resource_type="candidate_assessment",
            read=lambda session, _principal: read_candidate_assessments(
                session,
                candidate_id=candidate_id,
            ),
            resource_ids=lambda value: (value.resume_id, value.fact_snapshot_id),
            candidate_ids=lambda value: (value.candidate_id,),
        )

    @server.tool(name="list_jobs", annotations=READ_ONLY)
    def list_jobs(limit: int = 20, cursor: str | None = None) -> IntegrationJobList:
        """List the authenticated workspace's jobs with a signed cursor."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("jobs:read",),
            action="integration.jobs.list",
            resource_type="job_list",
            read=lambda session, principal: read_jobs(
                session,
                principal=principal,
                settings=settings,
                limit=limit,
                cursor=cursor,
            ),
            resource_ids=lambda value: [item.job_id for item in value.items],
        )

    @server.tool(name="get_job_requirements", annotations=READ_ONLY)
    def get_job_requirements(
        job_id: str, version_id: str
    ) -> IntegrationJobRequirements:
        """Return one confirmed immutable job requirement version."""

        return _run_tool(
            database=database,
            settings=settings,
            required_scopes=("jobs:read",),
            action="integration.job.requirements.read",
            resource_type="job_requirements",
            read=lambda session, _principal: read_job_requirements(
                session,
                job_id=job_id,
                version_id=version_id,
            ),
            resource_ids=lambda value: (value.job_id, value.job_version_id),
        )

    if settings.integrations_analysis_enabled:

        @server.tool(name="prepare_analysis_draft", annotations=IDEMPOTENT_WRITE)
        def prepare_analysis_draft(
            request: IntegrationAnalysisPrepare,
        ) -> IntegrationAnalysisPrepared:
            """Prepare a private draft for browser review; never save before owner confirmation."""

            candidate_ids = tuple(str(item.candidate_id) for item in request.candidates)
            return _run_tool(
                database=database,
                settings=settings,
                required_scopes=tuple(sorted(required_save_scopes(request))),
                action="integration.analysis.prepare",
                resource_type="analysis_confirmation",
                read=lambda session, principal: write_analysis_draft(
                    session,
                    principal,
                    payload=request,
                ),
                resource_ids=lambda value: (value.id,),
                candidate_ids=lambda _value: candidate_ids,
            )

        @server.tool(name="list_analysis_drafts", annotations=READ_ONLY)
        def list_analysis_drafts(
            limit: int = 20,
            cursor: str | None = None,
        ) -> IntegrationAnalysisDraftList:
            """List only the current connection owner's retained drafts."""

            return _run_tool(
                database=database,
                settings=settings,
                required_scopes=("analyses:read",),
                action="integration.analysis.list",
                resource_type="analysis_draft_list",
                read=lambda session, principal: read_analysis_drafts(
                    session,
                    principal,
                    settings=settings,
                    limit=limit,
                    cursor=cursor,
                ),
                resource_ids=lambda value: [item.id for item in value.items],
                candidate_ids=analysis_candidate_ids,
            )

        @server.tool(name="get_analysis_draft", annotations=READ_ONLY)
        def get_analysis_draft(report_id: str) -> IntegrationAnalysisDraftDetail:
            """Read one private draft with its selected pinned source facts."""

            return _run_tool(
                database=database,
                settings=settings,
                required_scopes=("analyses:read",),
                action="integration.analysis.read",
                resource_type="analysis_draft",
                read=lambda session, principal: read_analysis_draft(
                    session,
                    principal,
                    report_id=report_id,
                ),
                resource_ids=lambda value: (value.id,),
                candidate_ids=analysis_candidate_ids,
            )

    return server


def create_integration_mcp_app(
    *,
    database: Database,
    settings: AppSettings,
) -> tuple[MCPServer, Starlette] | None:
    """Return a root-mountable SDK app; the host must enter its lifespan below."""

    server = build_integration_mcp_server(database=database, settings=settings)
    if server is None:
        return None
    parsed = urlparse(settings.public_app_url)
    allowed_host = parsed.netloc
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[allowed_host],
            allowed_origins=[],
        ),
    )
    # SDK 2.2.0 reuses required_scopes as advertised scopes. Keep transport auth
    # narrow-token compatible and replace only its public metadata route using
    # the SDK helper; individual tools retain their own scope checks above.
    auth = server.settings.auth
    metadata_routes = create_protected_resource_routes(
        resource_url=auth.resource_server_url,
        authorization_servers=[auth.issuer_url],
        scopes_supported=oauth_supported_scopes(settings),
    )
    replacements = {route.path: route for route in metadata_routes}
    app.router.routes[:] = [replacements.get(getattr(route, "path", None), route) for route in app.routes]
    return server, app


@asynccontextmanager
async def integration_mcp_lifespan(server: MCPServer):
    """Enter from the owning FastAPI lifespan; mounted Starlette apps do not."""

    async with server.session_manager.run():
        yield
