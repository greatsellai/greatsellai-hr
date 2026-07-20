from __future__ import annotations

import hashlib
import hmac
from ipaddress import ip_address, ip_network
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.config import AppSettings
from app.database import Database, get_session
from app.filter_options import filter_options_payload
from app.models import Candidate, Resume
from app.schemas import (
    AuthLogin,
    AuthRegistration,
    AuthSession,
    EmailVerificationComplete,
    EmailVerificationResendResult,
    OrganizationInvitationAccept,
    OrganizationInvitationCreate,
    OrganizationInvitationResponse,
    OrganizationPlanAssign,
    OrganizationPlanResponse,
    PasswordResetComplete,
    PasswordResetRequest,
    PasswordResetRequestResult,
    ProductPlanResponse,
    ProductPlanUpdate,
    RegistrationOfferResponse,
    MailboxConfigResponse,
    MailboxConfigUpdate,
    MailboxImportResponse,
    MailboxImportHistoryResponse,
    MailboxRetentionCleanupRunHistoryResponse,
    MailboxRetentionCleanupRunResponse,
    MailboxRetentionPolicyUpdate,
    MailboxRetentionPreviewResponse,
    MailboxRetentionSummaryResponse,
    MailboxSyncResponse,
    CandidateCreate,
    CandidateCreated,
    CandidateSearchRequest,
    CandidateSearchResponse,
    JobCreate,
    JobGenerationRequest,
    JobGenerationResponse,
    JobMatchBatchResponse,
    JobMatchBatchItemResponse,
    JobMatchCreate,
    JobMatchResponse,
    OriginalJobPublishRequest,
    JobVersionRequirementsUpdate,
    JobVersionResponse,
    ResumeDetail,
    ResumeActivateRequest,
    ResumeFactsSaveRequest,
    ResumeReviewActionResponse,
    ResumeReviewDetail,
    ResumeSourceBlockResponse,
    ResumeEducationResponse,
    ResumeExperienceDetailResponse,
    ResumeExperienceResponse,
    ResumeReviewQueueItem,
    ResumeReviewQueueResponse,
    ResumeLibraryResponse,
    ResumeLanguageCredentialResponse,
    ResumeScholarshipResponse,
    ResumeSkillResponse,
    ResumeUploadResponse,
    RecruitingAgentRequest,
    RecruitingAgentResponse,
    ResumeScoreCreate,
    ResumeScoreBatchItemResponse,
    ResumeScoreBatchResponse,
    ResumeScoreOverride,
    ResumeScoreResponse,
    ResumeSummaryManualCreate,
    ResumeSummaryResponse,
    SavedFilterCreate,
    SavedFilterResponse,
    ScoreTemplateCreate,
    ScoreTemplateResponse,
)
from app.services.identity_service import (
    AuthPrincipal,
    IdentityServiceError,
    accept_invitation,
    assign_organization_plan,
    auth_session_response,
    authenticate_email_password,
    clear_session,
    complete_password_reset,
    complete_email_verification,
    create_invitation,
    create_registration,
    current_plan_response,
    ensure_identity_bootstrap,
    establish_session,
    issue_password_reset,
    issue_email_verification,
    legacy_principal,
    list_product_plans,
    normalize_email,
    principal_from_session,
    registration_offer,
    record_email_verification_delivery,
    require_feature,
    trial_access,
    update_product_plan,
)
from app.services.transactional_email import (
    TransactionalEmailError,
    TransactionalEmailProvider,
    VerificationDelivery,
    build_transactional_email_provider,
    email_verification_url,
)
from app.services.registration_rate_limit import (
    RegistrationRateLimitError,
    enforce_registration_rate_limit,
)
from app.tenant_scope import organization_context_id, set_organization_context
from app.services.institution_service import (
    is_institution_registry_seeded,
    seed_institution_registry,
)
from app.services.ai_extraction_job_service import (
    AiExtractionJobError,
    ai_extraction_state,
    enqueue_uploaded_resume_ai_extraction,
    request_resume_ai_extraction,
    request_resume_filter_v2_enrichment,
)
from app.services.resume_service import (
    FactValidationError,
    IdempotencyConflictError,
    NotFoundError,
    ResumeServiceError,
    UploadValidationError,
    activate_ready_resume,
    create_candidate,
    discard_uploaded_pdf,
    get_idempotent_upload_resume,
    get_resume,
    normalize_upload_idempotency_key,
    reparse_active_resume_as_new_version,
    reconcile_legacy_completed_ai_resumes,
    register_upload_idempotency_key,
    resolve_uploaded_resume_path,
    save_facts,
    save_pdf_resume,
    validate_pdf_resume_upload,
)
from app.services.saved_filter_service import (
    SavedFilterNotFoundError,
    create_saved_filter,
    delete_saved_filter,
    list_saved_filters,
)
from app.services.search_service import SearchValidationError, search_candidates
from app.services.resume_library_service import list_resume_library
from app.services.recruiting_agent_service import (
    RecruitingAgentServiceError,
    run_recruiting_agent_turn,
)
from app.services.score_service import (
    DeepSeekProviderError,
    ResumeScoreNotFoundError,
    ScoreServiceError,
    ScoreTemplateNotFoundError,
    create_score_template,
    get_resume_score,
    list_score_templates,
    list_resume_scores,
    override_score_dimension,
    run_resume_score,
)
from app.services.resume_score_batch_service import (
    enqueue_resume_score_batch,
    get_resume_score_batch,
    list_resume_score_batch_items,
)
from app.services.summary_service import (
    DeepSeekProviderError as SummaryDeepSeekProviderError,
    ResumeSummaryNotFoundError,
    SummaryServiceError,
    create_manual_summary_version,
    generate_resume_summary,
    get_resume_summary,
    list_resume_summaries,
)
from app.services.job_service import (
    DeepSeekProviderError as JobDeepSeekProviderError,
    JobMatchNotFoundError,
    JobNotFoundError,
    JobServiceError,
    JobVersionNotFoundError,
    confirm_job_version,
    create_job,
    create_job_version,
    extract_job_version_requirements,
    generate_job_description,
    get_job_match,
    get_latest_confirmed_job_version,
    get_job_version,
    list_confirmed_job_versions,
    list_job_version_matches,
    list_job_versions,
    list_resume_job_matches,
    publish_original_job,
    run_job_match,
    update_job_version_requirements,
)
from app.services.job_match_batch_service import (
    enqueue_job_version_match_batch,
    get_job_match_batch,
    list_job_match_batch_items,
)
from app.services.mailbox_import_service import (
    MailboxImportError,
    get_mailbox_config,
    list_mailbox_imports,
    retry_mailbox_attachment,
    save_mailbox_config,
    sync_mailbox,
)
from app.services.mailbox_retention_service import (
    MailboxRetentionError,
    cleanup_mailbox_retention,
    get_mailbox_retention_summary,
    list_mailbox_retention_cleanup_runs,
    preview_mailbox_retention_cleanup,
    update_mailbox_retention_policy,
)


logger = logging.getLogger(__name__)


def _resume_detail(resume: object) -> ResumeDetail:
    ai_extraction_status, ai_extraction_error = ai_extraction_state(resume)
    return ResumeDetail(
        resume_id=resume.id,
        candidate_id=resume.candidate_id,
        candidate_display_name=resume.candidate.display_name,
        extraction_status=resume.extraction_status,
        ai_extraction_status=ai_extraction_status,
        ai_extraction_error=ai_extraction_error,
        is_active=resume.is_active,
        is_985_211=resume.is_985_211,
        highest_degree=resume.highest_degree,
        employment_months=resume.employment_months,
        employment_or_internship_months=resume.employment_or_internship_months,
        source_page_count=resume.source_page_count,
        parsed_page_count=resume.parsed_page_count,
        quality_flags=resume.quality_flags or [],
    )


def _resume_upload_response(resume: object) -> ResumeUploadResponse:
    ai_extraction_status, ai_extraction_error = ai_extraction_state(resume)
    return ResumeUploadResponse(
        resume_id=resume.id,
        candidate_id=resume.candidate_id,
        candidate_display_name=resume.candidate.display_name,
        extraction_status=resume.extraction_status,
        ai_extraction_status=ai_extraction_status,
        ai_extraction_error=ai_extraction_error,
        source_page_count=resume.source_page_count,
        parsed_page_count=resume.parsed_page_count,
        quality_flags=resume.quality_flags or [],
    )


_SUPPORTED_RESUME_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
        "image/jpeg",
        "text/html",
        "application/octet-stream",
    }
)


def _resume_original_file_path(
    *,
    settings: AppSettings,
    storage_key: str,
    organization_id: str,
) -> Path:
    """Resolve an original strictly inside its owning workspace directory."""
    try:
        return resolve_uploaded_resume_path(
            settings,
            storage_key=storage_key,
            organization_id=organization_id,
        )
    except ResumeServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="resume_original_file_not_found",
        ) from exc


def _commit_or_raise(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="database_conflict",
        ) from exc


def _deliver_email_verification(
    *,
    session: Session,
    settings: AppSettings,
    provider: TransactionalEmailProvider,
    verification_id: str,
    recipient: str,
    token: str,
) -> bool:
    """Send one verification link and retain only non-sensitive delivery state."""

    try:
        provider.send_email_verification(
            VerificationDelivery(
                recipient=recipient,
                verification_url=email_verification_url(settings, token=token),
                expires_minutes=max(1, settings.email_verification_ttl_seconds // 60),
            )
        )
    except TransactionalEmailError as exc:
        record_email_verification_delivery(
            session,
            verification_id=verification_id,
            delivered=False,
            error_code=str(exc),
        )
        try:
            _commit_or_raise(session)
        except HTTPException:
            logger.warning("email_verification_delivery_state_not_recorded")
        logger.warning("email_verification_delivery_failed")
        return False
    except Exception:
        # The public registration response must not expose a provider error or
        # accidentally leave a user with an unexplained 500 after the account
        # row was committed.  The user remains on the verification page and
        # can resend, while the durable record keeps a safe error code.
        record_email_verification_delivery(
            session,
            verification_id=verification_id,
            delivered=False,
            error_code="email_delivery_provider_failed",
        )
        try:
            _commit_or_raise(session)
        except HTTPException:
            logger.warning("email_verification_delivery_state_not_recorded")
        logger.warning("email_verification_delivery_failed")
        return False

    record_email_verification_delivery(
        session,
        verification_id=verification_id,
        delivered=True,
    )
    try:
        _commit_or_raise(session)
    except HTTPException:
        logger.warning("email_verification_delivery_state_not_recorded")
    return True


def _registration_client_identifier(request: Request, settings: AppSettings) -> str:
    """Return a safe signup throttle key without trusting spoofable headers."""

    direct_peer = request.client.host if request.client is not None else "unknown"
    if not _is_trusted_proxy(direct_peer, settings.trusted_proxy_cidrs):
        return f"peer:{direct_peer}"

    # Caddy appends the remote address to X-Forwarded-For.  Reading the last
    # valid value preserves the actual browser address even if an earlier,
    # client-supplied value reached Caddy.  The header is ignored entirely
    # unless the direct TCP peer is explicitly trusted above.
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        candidate = forwarded_for.rsplit(",", maxsplit=1)[-1].strip()
        try:
            return f"ip:{ip_address(candidate).compressed}"
        except ValueError:
            pass
    return f"peer:{direct_peer}"


def _is_trusted_proxy(host: str, cidrs: tuple[str, ...]) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in ip_network(cidr, strict=False) for cidr in cidrs)


def _raise_job_service_error(exc: JobServiceError) -> None:
    """Translate predictable JD workflow failures into stable HTTP results."""

    code = str(exc)
    if code in {"resume_not_found"}:
        response_status = status.HTTP_404_NOT_FOUND
    elif code == "deepseek_api_key_not_configured":
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif code in {
        "jd_text_has_no_clauses",
        "job_requirement_clause_not_found",
        "job_requirement_not_grounded_in_clauses",
        "job_requirement_not_grounded_in_jd",
        "job_requirement_keys_must_be_unique",
    }:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        response_status = status.HTTP_409_CONFLICT
    raise HTTPException(status_code=response_status, detail=code) from exc


def _resume_review_detail(resume: object) -> ResumeReviewDetail:
    base = _resume_detail(resume)
    return ResumeReviewDetail(
        **base.model_dump(),
        original_filename=resume.original_filename,
        facts_version=resume.facts_version,
        source_blocks=[
            ResumeSourceBlockResponse(
                block_id=block.block_id,
                page_no=block.page_no,
                block_type=block.block_type,
                text=block.text,
            )
            for block in sorted(
                resume.source_blocks,
                key=lambda block: (block.page_no, block.block_id),
            )
        ],
        education=[
            ResumeEducationResponse(
                school_name_raw=education.school_name_raw,
                school_match_state=education.school_match_state,
                degree=education.degree,
                major_raw=education.major_raw,
                start_month=education.start_month,
                end_month=education.end_month,
                institution_tiers=education.institution_tiers or [],
                average_score=education.average_score,
                gpa_value=education.gpa_value,
                gpa_scale=education.gpa_scale,
                gpa_percent=education.gpa_percent,
                rank_position=education.rank_position,
                rank_total=education.rank_total,
                rank_percent=education.rank_percent,
                evidence_block_ids=education.evidence_block_ids or [],
            )
            for education in resume.educations
        ],
        experiences=[
            ResumeExperienceResponse(
                experience_type=experience.experience_type,
                experience_name_raw=experience.experience_name_raw,
                organization_name_raw=experience.organization_name_raw,
                title_raw=experience.title_raw,
                start_month=experience.start_month,
                end_month=experience.end_month,
                is_current=experience.is_current,
                evidence_block_ids=experience.evidence_block_ids or [],
                classification_evidence_block_ids=(
                    experience.classification_evidence_block_ids or []
                ),
                detail_items=[
                    ResumeExperienceDetailResponse(
                        detail_raw=item["detail_raw"],
                        evidence_block_ids=item["evidence_block_ids"],
                    )
                    for item in (experience.detail_items or [])
                    if isinstance(item, dict)
                    and isinstance(item.get("detail_raw"), str)
                    and isinstance(item.get("evidence_block_ids"), list)
                ],
                leadership_context=experience.leadership_context,
                leadership_role=experience.leadership_role,
                award_level=experience.award_level,
                award_result_raw=experience.award_result_raw,
            )
            for experience in resume.experiences
        ],
        skills=[
            ResumeSkillResponse(
                skill_display=skill.skill_display,
                skill_category=skill.skill_category,
                evidence_block_ids=skill.evidence_block_ids or [],
            )
            for skill in resume.skills
        ],
        language_credentials=[
            ResumeLanguageCredentialResponse(
                credential_code=credential.credential_code,
                credential_name_raw=credential.credential_name_raw,
                score=credential.score,
                passed=credential.passed,
                evidence_block_ids=credential.evidence_block_ids or [],
            )
            for credential in resume.language_credentials
        ],
        scholarships=[
            ResumeScholarshipResponse(
                scholarship_name_raw=scholarship.scholarship_name_raw,
                scholarship_level=scholarship.scholarship_level,
                evidence_block_ids=scholarship.evidence_block_ids or [],
            )
            for scholarship in resume.scholarships
        ],
        review_actions=[
            ResumeReviewActionResponse(
                action=action.action,
                actor=action.actor,
                note=action.note,
                created_at=action.created_at.isoformat(),
            )
            for action in sorted(
                resume.review_actions,
                key=lambda action: (action.created_at, action.id),
                reverse=True,
            )
        ],
    )


async def require_authenticated_member(
    request: Request,
    session: Session = Depends(get_session),
    x_admin_token: Annotated[str | None, Header()] = None,
) -> AuthPrincipal:
    """Resolve one session member and bind its workspace to Session.

    Email verification is intentionally not checked here: an authenticated,
    unverified account needs this dependency to resend its own link.  Every
    business route continues through ``require_single_admin`` below.
    """

    settings: AppSettings = request.app.state.settings
    principal: AuthPrincipal | None = None
    if settings.allow_unauthenticated:
        principal = legacy_principal(session)
    else:
        principal = principal_from_session(session, request.session)
        # Existing signed browser sessions and the optional header remain a
        # migration bridge into *only* the legacy workspace.
        if principal is None and request.session.get("resume_v3_authenticated") is True:
            principal = legacy_principal(session)
        if (
            principal is None
            and settings.admin_token
            and x_admin_token
            and hmac.compare_digest(x_admin_token, settings.admin_token)
        ):
            principal = legacy_principal(session)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=("invalid_admin_token" if settings.admin_token else "authentication_required"),
        )

    set_organization_context(session, principal.organization_id)
    return principal


async def require_single_admin(
    principal: AuthPrincipal = Depends(require_authenticated_member),
) -> AuthPrincipal:
    """Require an activated, verified workspace member for business APIs."""

    if not principal.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_verification_required",
        )
    if not trial_access(principal).access_enabled:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="trial_expired",
        )
    return principal


async def require_organization_admin(
    principal: AuthPrincipal = Depends(require_single_admin),
) -> AuthPrincipal:
    if principal.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="organization_admin_required")
    return principal


async def require_mailbox_feature(
    principal: AuthPrincipal = Depends(require_organization_admin),
) -> AuthPrincipal:
    if not require_feature(principal, "mailbox_import"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="feature_not_available")
    return principal


async def require_ai_jd_feature(
    principal: AuthPrincipal = Depends(require_single_admin),
) -> AuthPrincipal:
    if not require_feature(principal, "ai_jd_generation"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="feature_not_available")
    return principal


def create_app(settings_override: AppSettings | None = None) -> FastAPI:
    settings = settings_override or AppSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_runtime()
        settings.ensure_directories()
        database = Database(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
        if settings.auto_create_schema:
            database.create_all()
        with database.session_factory() as session:
            ensure_identity_bootstrap(session)
            if settings.seed_registry_on_startup:
                seed_institution_registry(session)
            elif not is_institution_registry_seeded(session):
                raise RuntimeError("institution_registry_not_seeded")
            reconcile_legacy_completed_ai_resumes(session)
            session.commit()
        app.state.settings = settings
        app.state.database = database
        app.state.transactional_email_provider = build_transactional_email_provider(settings)
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title="Resume Screening V3",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        # The fallback only serves explicitly unauthenticated local workspaces.
        # Production validation requires an independently configured secret.
        secret_key=settings.session_secret or settings.admin_token or "resume-v3-development-session",
        session_cookie="resume_v3_session",
        max_age=60 * 60 * 12,
        same_site="strict",
        https_only=settings.session_cookie_secure,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/auth/session", response_model=AuthSession)
    async def get_auth_session(
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        principal = (
            legacy_principal(session)
            if settings.allow_unauthenticated
            else principal_from_session(session, request.session)
        )
        # Preserve an existing migration session only as a legacy identity.
        if principal is None and request.session.get("resume_v3_authenticated") is True:
            principal = legacy_principal(session)
        if principal is not None:
            set_organization_context(session, principal.organization_id)
        return auth_session_response(principal, login_required=not settings.allow_unauthenticated)

    @app.post("/v1/auth/login", response_model=AuthSession)
    async def post_auth_login(
        payload: AuthLogin,
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        try:
            if payload.email:
                principal = authenticate_email_password(
                    session,
                    email_value=payload.email,
                    password=payload.password,
                )
            elif (
                settings.admin_token
                and hmac.compare_digest(payload.password, settings.admin_token)
            ):
                principal = legacy_principal(session)
            elif settings.allow_unauthenticated:
                principal = legacy_principal(session)
            else:
                raise IdentityServiceError("invalid_login_credentials")
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_login_credentials",
            ) from exc
        _commit_or_raise(session)
        establish_session(request.session, principal)
        set_organization_context(session, principal.organization_id)
        return auth_session_response(principal, login_required=not settings.allow_unauthenticated)

    @app.get("/v1/auth/registration-offer", response_model=RegistrationOfferResponse)
    async def get_registration_offer(
        session: Session = Depends(get_session),
    ) -> RegistrationOfferResponse:
        try:
            return registration_offer(session)
        except IdentityServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.post("/v1/auth/register", response_model=AuthSession, status_code=status.HTTP_201_CREATED)
    async def post_auth_register(
        payload: AuthRegistration,
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        provider: TransactionalEmailProvider = request.app.state.transactional_email_provider
        if not provider.configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="email_delivery_not_configured",
            )
        try:
            _, email_key = normalize_email(payload.email)
            enforce_registration_rate_limit(
                session,
                secret=(
                    settings.session_secret
                    or settings.admin_token
                    or "resume-v3-development-registration-rate-limit"
                ),
                client_identifier=_registration_client_identifier(request, settings),
                email_key=email_key,
                global_limit=settings.registration_rate_limit_global_limit,
                global_window_seconds=settings.registration_rate_limit_global_window_seconds,
                client_limit=settings.registration_rate_limit_client_limit,
                client_window_seconds=settings.registration_rate_limit_client_window_seconds,
                email_limit=settings.registration_rate_limit_email_limit,
                email_window_seconds=settings.registration_rate_limit_email_window_seconds,
            )
            # Preserve the anti-abuse accounting even when account creation
            # subsequently fails (for example, for a duplicate address).
            _commit_or_raise(session)
        except RegistrationRateLimitError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="registration_rate_limit_exceeded",
            ) from exc
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        try:
            principal = create_registration(session, payload)
            verification, raw_token = issue_email_verification(
                session,
                user=principal.user,
                ttl_seconds=settings.email_verification_ttl_seconds,
                resend_cooldown_seconds=settings.email_verification_resend_cooldown_seconds,
                daily_limit=settings.email_verification_daily_limit,
                enforce_resend_limit=False,
            )
            _commit_or_raise(session)
        except IdentityServiceError as exc:
            session.rollback()
            code = str(exc)
            response_status = (
                status.HTTP_409_CONFLICT
                if code == "email_already_registered"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=code) from exc
        establish_session(request.session, principal)
        set_organization_context(session, principal.organization_id)
        _deliver_email_verification(
            session=session,
            settings=settings,
            provider=provider,
            verification_id=verification.id,
            recipient=principal.user.email,
            token=raw_token,
        )
        return auth_session_response(principal, login_required=not settings.allow_unauthenticated)

    @app.post("/v1/auth/email-verification/complete", response_model=AuthSession)
    async def post_email_verification_complete(
        payload: EmailVerificationComplete,
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        existing_principal = principal_from_session(session, request.session)
        if (
            existing_principal is None
            and request.session.get("resume_v3_authenticated") is True
        ):
            existing_principal = legacy_principal(session)
        try:
            principal = complete_email_verification(
                session,
                token=payload.token,
                expected_user_id=(existing_principal.user.id if existing_principal else None),
            )
            _commit_or_raise(session)
        except IdentityServiceError as exc:
            session.rollback()
            response_status = (
                status.HTTP_409_CONFLICT
                if str(exc) == "email_verification_account_mismatch"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(
                status_code=response_status,
                detail=str(exc),
            ) from exc
        establish_session(request.session, principal)
        set_organization_context(session, principal.organization_id)
        return auth_session_response(principal, login_required=not settings.allow_unauthenticated)

    @app.post(
        "/v1/auth/email-verification/resend",
        response_model=EmailVerificationResendResult,
    )
    async def post_email_verification_resend(
        request: Request,
        principal: AuthPrincipal = Depends(require_authenticated_member),
        session: Session = Depends(get_session),
    ) -> EmailVerificationResendResult:
        if principal.email_verified:
            return EmailVerificationResendResult(accepted=True, delivery_available=True)

        settings: AppSettings = request.app.state.settings
        provider: TransactionalEmailProvider = request.app.state.transactional_email_provider
        if not provider.configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="email_delivery_not_configured",
            )
        try:
            verification, raw_token = issue_email_verification(
                session,
                user=principal.user,
                ttl_seconds=settings.email_verification_ttl_seconds,
                resend_cooldown_seconds=settings.email_verification_resend_cooldown_seconds,
                daily_limit=settings.email_verification_daily_limit,
                enforce_resend_limit=True,
            )
            _commit_or_raise(session)
        except IdentityServiceError as exc:
            session.rollback()
            code = str(exc)
            response_status = (
                status.HTTP_429_TOO_MANY_REQUESTS
                if code in {
                    "email_verification_resend_too_soon",
                    "email_verification_resend_limit_reached",
                }
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=code) from exc

        delivered = _deliver_email_verification(
            session=session,
            settings=settings,
            provider=provider,
            verification_id=verification.id,
            recipient=principal.user.email,
            token=raw_token,
        )
        return EmailVerificationResendResult(
            accepted=True,
            delivery_available=delivered,
        )

    @app.post("/v1/auth/password-reset/request", response_model=PasswordResetRequestResult)
    async def post_password_reset_request(
        payload: PasswordResetRequest,
        session: Session = Depends(get_session),
    ) -> PasswordResetRequestResult:
        # The token is digest-only in the database and never appears in the
        # response.  A mail-delivery adapter can be connected later without
        # changing this enumeration-safe public contract.
        issue_password_reset(session, email_value=payload.email)
        _commit_or_raise(session)
        return PasswordResetRequestResult(accepted=True, delivery_available=False)

    @app.post("/v1/auth/password-reset/complete", status_code=status.HTTP_204_NO_CONTENT)
    async def post_password_reset_complete(
        payload: PasswordResetComplete,
        session: Session = Depends(get_session),
    ) -> None:
        try:
            complete_password_reset(session, token=payload.token, password=payload.password)
            _commit_or_raise(session)
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    @app.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def post_auth_logout(request: Request) -> None:
        clear_session(request.session)

    @app.get(
        "/v1/organization/plan",
        response_model=OrganizationPlanResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_current_organization_plan(
        principal: AuthPrincipal = Depends(require_single_admin),
    ) -> OrganizationPlanResponse:
        return current_plan_response(principal)

    @app.post(
        "/v1/organization/invitations",
        response_model=OrganizationInvitationResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_organization_invitation(
        payload: OrganizationInvitationCreate,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> OrganizationInvitationResponse:
        try:
            response = create_invitation(session, principal=principal, payload=payload)
            _commit_or_raise(session)
            return response
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    @app.post("/v1/auth/invitations/accept", response_model=AuthSession)
    async def post_accept_organization_invitation(
        payload: OrganizationInvitationAccept,
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        try:
            principal = accept_invitation(session, payload=payload)
            _commit_or_raise(session)
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        establish_session(request.session, principal)
        set_organization_context(session, principal.organization_id)
        return auth_session_response(principal, login_required=True)

    @app.get("/v1/platform/plans", response_model=list[ProductPlanResponse])
    def get_platform_plans(
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> list[ProductPlanResponse]:
        if not principal.is_platform_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="platform_admin_required")
        return list_product_plans(session)

    @app.put("/v1/platform/plans/{plan_code}", response_model=ProductPlanResponse)
    def put_platform_plan(
        plan_code: str,
        payload: ProductPlanUpdate,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> ProductPlanResponse:
        if not principal.is_platform_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="platform_admin_required")
        try:
            response = update_product_plan(session, code=plan_code, payload=payload)
            _commit_or_raise(session)
            return response
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "product_plan_not_found"
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc

    @app.put(
        "/v1/platform/organizations/{organization_id}/plan",
        response_model=OrganizationPlanResponse,
    )
    def put_platform_organization_plan(
        organization_id: str,
        payload: OrganizationPlanAssign,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> OrganizationPlanResponse:
        if not principal.is_platform_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="platform_admin_required")
        try:
            response = assign_organization_plan(
                session,
                organization_id=organization_id,
                payload=payload,
            )
            _commit_or_raise(session)
            return response
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/mailbox/config",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_configuration(
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        return get_mailbox_config(session)

    @app.put(
        "/v1/mailbox/config",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def put_mailbox_configuration(
        payload: MailboxConfigUpdate,
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return save_mailbox_config(session, settings=settings, payload=payload)
        except MailboxImportError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    @app.get(
        "/v1/mailbox/retention",
        response_model=MailboxRetentionSummaryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_retention(
        session: Session = Depends(get_session),
    ) -> MailboxRetentionSummaryResponse:
        return get_mailbox_retention_summary(session, settings=settings)

    @app.put(
        "/v1/mailbox/retention",
        response_model=MailboxRetentionSummaryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def put_mailbox_retention(
        payload: MailboxRetentionPolicyUpdate,
        session: Session = Depends(get_session),
    ) -> MailboxRetentionSummaryResponse:
        try:
            return update_mailbox_retention_policy(
                session,
                settings=settings,
                retention_policy=payload.retention_policy,
            )
        except MailboxRetentionError as exc:
            session.rollback()
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "mailbox_not_configured"
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=str(exc),
            ) from exc

    @app.post(
        "/v1/mailbox/retention/preview",
        response_model=MailboxRetentionPreviewResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_retention_preview(
        session: Session = Depends(get_session),
    ) -> MailboxRetentionPreviewResponse:
        return preview_mailbox_retention_cleanup(session, settings=settings)

    @app.post(
        "/v1/mailbox/retention/cleanup",
        response_model=MailboxRetentionCleanupRunResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_retention_cleanup(
        session: Session = Depends(get_session),
    ) -> MailboxRetentionCleanupRunResponse:
        try:
            return cleanup_mailbox_retention(
                session,
                settings=settings,
                trigger_type="manual",
            )
        except MailboxRetentionError as exc:
            session.rollback()
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "mailbox_not_configured"
                    else status.HTTP_409_CONFLICT
                ),
                detail=str(exc),
            ) from exc

    @app.get(
        "/v1/mailbox/retention/runs",
        response_model=MailboxRetentionCleanupRunHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_retention_cleanup_runs(
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> MailboxRetentionCleanupRunHistoryResponse:
        return list_mailbox_retention_cleanup_runs(
            session,
            settings=settings,
            limit=limit,
        )

    @app.post(
        "/v1/mailbox/sync",
        response_model=MailboxSyncResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_sync(
        session: Session = Depends(get_session),
    ) -> MailboxSyncResponse:
        try:
            result = sync_mailbox(session, settings=settings)
        except MailboxImportError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "mailbox_config_not_found"
                    else status.HTTP_409_CONFLICT
                ),
                detail=str(exc),
            ) from exc
        if not result.configured:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="mailbox_not_configured",
            )
        return result

    @app.post(
        "/v1/mailbox/imports/{import_id}/retry",
        response_model=MailboxImportResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_attachment_retry(
        import_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxImportResponse:
        try:
            return retry_mailbox_attachment(
                session,
                settings=settings,
                import_id=import_id,
            )
        except MailboxImportError as exc:
            session.rollback()
            code = str(exc)
            if code == "mailbox_import_not_found":
                response_status = status.HTTP_404_NOT_FOUND
            elif code in {
                "mailbox_import_not_retryable",
                "mailbox_import_retry_in_progress",
                "mailbox_import_retry_superseded",
            }:
                response_status = status.HTTP_409_CONFLICT
            else:
                response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
            raise HTTPException(status_code=response_status, detail=code) from exc

    @app.get(
        "/v1/mailbox/imports",
        response_model=MailboxImportHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_import_history(
        limit: int = Query(default=40, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> MailboxImportHistoryResponse:
        return list_mailbox_imports(session, limit=limit)

    @app.post(
        "/v1/recruiting-agent/turns",
        response_model=RecruitingAgentResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_recruiting_agent_turn(
        payload: RecruitingAgentRequest,
        session: Session = Depends(get_session),
    ) -> RecruitingAgentResponse:
        """Run one bounded, tool-backed recruiter assistant turn."""

        try:
            response = run_recruiting_agent_turn(
                session,
                payload=payload,
                settings=settings,
            )
            _commit_or_raise(session)
        except RecruitingAgentServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            session.rollback()
            logger.exception("Recruiting-agent request failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="agent_service_unavailable",
            ) from exc
        return response

    @app.post(
        "/v1/candidates",
        response_model=CandidateCreated,
        dependencies=[Depends(require_single_admin)],
    )
    def post_candidate(
        payload: CandidateCreate,
        session: Session = Depends(get_session),
    ) -> CandidateCreated:
        candidate = create_candidate(session, display_name=payload.display_name)
        _commit_or_raise(session)
        return CandidateCreated(candidate_id=candidate.id)

    @app.post(
        "/v1/candidates/{candidate_id}/resumes",
        response_model=ResumeUploadResponse,
        dependencies=[Depends(require_single_admin)],
    )
    async def post_resume(
        candidate_id: str,
        file: UploadFile = File(...),
        session: Session = Depends(get_session),
    ) -> ResumeUploadResponse:
        if file.content_type and file.content_type not in _SUPPORTED_RESUME_MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="content_type_not_supported",
            )
        content = await file.read(settings.max_upload_bytes + 1)
        storage_key: str | None = None
        try:
            resume = save_pdf_resume(
                session,
                candidate_id=candidate_id,
                original_filename=file.filename,
                content=content,
                settings=settings,
            )
            storage_key = resume.storage_key
            enqueue_uploaded_resume_ai_extraction(
                session,
                resume=resume,
                settings=settings,
            )
            _commit_or_raise(session)
        except NotFoundError as exc:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except UploadValidationError as exc:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            response_status = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if str(exc) == "file_too_large"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        except HTTPException:
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            raise
        except Exception:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            raise
        return _resume_upload_response(resume)

    @app.post(
        "/v1/resumes/upload",
        response_model=ResumeUploadResponse,
        dependencies=[Depends(require_single_admin)],
    )
    async def post_new_candidate_resume(
        file: UploadFile = File(...),
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
        session: Session = Depends(get_session),
    ) -> ResumeUploadResponse:
        """Convenience upload flow used by the single-account web app."""

        if file.content_type and file.content_type not in _SUPPORTED_RESUME_MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="content_type_not_supported",
            )
        try:
            normalized_idempotency_key = normalize_upload_idempotency_key(
                idempotency_key
            )
        except UploadValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        content = await file.read(settings.max_upload_bytes + 1)
        try:
            validate_pdf_resume_upload(
                original_filename=file.filename,
                content=content,
                settings=settings,
            )
        except UploadValidationError as exc:
            response_status = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if str(exc) == "file_too_large"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

        content_sha256 = hashlib.sha256(content).hexdigest()
        if normalized_idempotency_key is not None:
            try:
                replayed_resume = get_idempotent_upload_resume(
                    session,
                    idempotency_key=normalized_idempotency_key,
                    content_sha256=content_sha256,
                )
            except IdempotencyConflictError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            if replayed_resume is not None:
                return _resume_upload_response(replayed_resume)

        storage_key: str | None = None
        try:
            # A new upload starts unnamed. The AI extraction worker may fill
            # Candidate.display_name only from source-grounded resume text.
            candidate = create_candidate(session, display_name=None)
            resume = save_pdf_resume(
                session,
                candidate_id=candidate.id,
                original_filename=file.filename,
                content=content,
                settings=settings,
            )
            storage_key = resume.storage_key
            if normalized_idempotency_key is not None:
                register_upload_idempotency_key(
                    session,
                    idempotency_key=normalized_idempotency_key,
                    content_sha256=content_sha256,
                    resume_id=resume.id,
                )
                # Surface a competing idempotency key before the transaction
                # commits, so its just-written PDF can be removed.
                session.flush()
            enqueue_uploaded_resume_ai_extraction(
                session,
                resume=resume,
                settings=settings,
            )
            session.commit()
        except UploadValidationError as exc:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            response_status = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if str(exc) == "file_too_large"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        except IntegrityError as exc:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            if normalized_idempotency_key is not None:
                try:
                    replayed_resume = get_idempotent_upload_resume(
                        session,
                        idempotency_key=normalized_idempotency_key,
                        content_sha256=content_sha256,
                    )
                except IdempotencyConflictError as conflict:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(conflict),
                    ) from conflict
                if replayed_resume is not None:
                    return _resume_upload_response(replayed_resume)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="database_conflict",
            ) from exc
        except Exception:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=storage_key,
                organization_id=organization_context_id(session),
            )
            raise
        return _resume_upload_response(resume)

    @app.get(
        "/v1/resumes/review-queue",
        response_model=ResumeReviewQueueResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_review_queue(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
        session: Session = Depends(get_session),
    ) -> ResumeReviewQueueResponse:
        """List pending uploads for the manual review workflow, newest first."""

        pending = Resume.is_active.is_(False)
        total = session.scalar(
            select(func.count()).select_from(Resume).where(pending)
        )
        rows = session.execute(
            select(Resume, Candidate.display_name)
            .join(Candidate, Resume.candidate_id == Candidate.id)
            .options(selectinload(Resume.ai_extraction_job))
            .where(pending)
            .order_by(Resume.created_at.desc(), Resume.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return ResumeReviewQueueResponse(
            items=[
                ResumeReviewQueueItem(
                    resume_id=resume.id,
                    candidate_id=resume.candidate_id,
                    candidate_display_name=display_name,
                    original_filename=resume.original_filename,
                    extraction_status=resume.extraction_status,
                    ai_extraction_status=ai_extraction_state(resume)[0],
                    ai_extraction_error=ai_extraction_state(resume)[1],
                    quality_flags=resume.quality_flags or [],
                    created_at=resume.created_at,
                )
                for resume, display_name in rows
            ],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )

    @app.get(
        "/v1/resumes/{resume_id}",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_detail(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        try:
            resume = get_resume(session, resume_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _resume_detail(resume)

    @app.get(
        "/v1/resumes/{resume_id}/review",
        response_model=ResumeReviewDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_review_detail(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeReviewDetail:
        try:
            resume = get_resume(session, resume_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _resume_review_detail(resume)

    @app.get(
        "/v1/resumes/{resume_id}/original-file",
        response_class=FileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_original_file(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> FileResponse:
        try:
            resume = get_resume(session, resume_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

        source_path = _resume_original_file_path(
            settings=settings,
            storage_key=resume.storage_key,
            organization_id=organization_context_id(session),
        )
        return FileResponse(
            path=source_path,
            media_type=(
                mimetypes.guess_type(resume.original_filename)[0]
                or "application/octet-stream"
            ),
            filename=resume.original_filename,
            content_disposition_type="inline",
        )

    @app.put(
        "/v1/resumes/{resume_id}/facts",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def put_resume_facts(
        resume_id: str,
        payload: ResumeFactsSaveRequest,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        try:
            resume = save_facts(session, resume_id=resume_id, request=payload)
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except FactValidationError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        _commit_or_raise(session)
        return _resume_detail(resume)

    @app.post(
        "/v1/resumes/{resume_id}/activate",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def post_activate_resume(
        resume_id: str,
        payload: ResumeActivateRequest,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        try:
            resume = activate_ready_resume(
                session,
                resume_id=resume_id,
                note=payload.note,
            )
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except FactValidationError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        _commit_or_raise(session)
        return _resume_detail(resume)

    @app.post(
        "/v1/resumes/{resume_id}/reparse-source",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def post_reparse_resume_source(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        """Create a fresh parser-repair version without mutating evidence.

        An already-active resume can have summaries, scores and JD matches
        grounded in its current source blocks.  A parser correction therefore
        creates a separate version; the worker activates it only after a new
        grounded AI extraction succeeds.
        """

        replacement_storage_key: str | None = None
        try:
            replacement = reparse_active_resume_as_new_version(
                session,
                resume_id=resume_id,
                settings=settings,
            )
            replacement_storage_key = replacement.storage_key
            _commit_or_raise(session)
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ResumeServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=(
                    status.HTTP_422_UNPROCESSABLE_CONTENT
                    if str(exc) == "unsupported_document_type"
                    else status.HTTP_409_CONFLICT
                ),
                detail=str(exc),
            ) from exc
        except HTTPException:
            # A failed commit leaves no durable Resume row for this copied
            # original, so remove the just-created file as well.
            discard_uploaded_pdf(
                settings,
                storage_key=replacement_storage_key,
                organization_id=organization_context_id(session),
            )
            raise
        except Exception:
            session.rollback()
            discard_uploaded_pdf(
                settings,
                storage_key=replacement_storage_key,
                organization_id=organization_context_id(session),
            )
            raise
        return _resume_detail(replacement)

    @app.post(
        "/v1/resumes/{resume_id}/queue-ai-extraction",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def post_queue_resume_ai_extraction(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        try:
            resume = request_resume_ai_extraction(
                session,
                resume_id=resume_id,
                settings=settings,
            )
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except AiExtractionJobError as exc:
            session.rollback()
            response_status = (
                status.HTTP_422_UNPROCESSABLE_CONTENT
                if str(exc) == "resume_has_no_native_text_for_ai_extraction"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        _commit_or_raise(session)
        return _resume_detail(resume)

    @app.post(
        "/v1/resumes/{resume_id}/extract-facts",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
        deprecated=True,
    )
    def post_extract_resume_facts_compatibility_alias(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        """Compatibility alias; it queues work and never calls a model inline."""

        try:
            resume = request_resume_ai_extraction(
                session,
                resume_id=resume_id,
                settings=settings,
            )
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except AiExtractionJobError as exc:
            session.rollback()
            response_status = (
                status.HTTP_422_UNPROCESSABLE_CONTENT
                if str(exc) == "resume_has_no_native_text_for_ai_extraction"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        _commit_or_raise(session)
        return _resume_detail(resume)

    @app.post(
        "/v1/resumes/{resume_id}/enrich-filter-facts",
        response_model=ResumeDetail,
        dependencies=[Depends(require_single_admin)],
    )
    def post_enrich_resume_filter_facts(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeDetail:
        try:
            resume = request_resume_filter_v2_enrichment(
                session,
                resume_id=resume_id,
                settings=settings,
            )
        except NotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except AiExtractionJobError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        _commit_or_raise(session)
        return _resume_detail(resume)

    @app.post(
        "/v1/candidates/search",
        response_model=CandidateSearchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_candidate_search(
        payload: CandidateSearchRequest,
        session: Session = Depends(get_session),
    ) -> CandidateSearchResponse:
        try:
            return search_candidates(session, payload)
        except SearchValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    @app.get(
        "/v1/filter-options",
        response_model=dict[str, object],
        dependencies=[Depends(require_single_admin)],
    )
    def get_filter_options() -> dict[str, object]:
        return filter_options_payload()

    @app.get(
        "/v1/resume-library",
        response_model=ResumeLibraryResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_library(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 50,
        session: Session = Depends(get_session),
    ) -> ResumeLibraryResponse:
        return list_resume_library(session, page=page, page_size=page_size)

    @app.post(
        "/v1/saved-filters",
        response_model=SavedFilterResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_saved_filter(
        payload: SavedFilterCreate,
        session: Session = Depends(get_session),
    ) -> SavedFilterResponse:
        response = create_saved_filter(session, payload=payload)
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/saved-filters",
        response_model=list[SavedFilterResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_saved_filters(
        session: Session = Depends(get_session),
    ) -> list[SavedFilterResponse]:
        return list_saved_filters(session)

    @app.delete(
        "/v1/saved-filters/{saved_filter_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_single_admin)],
    )
    def delete_saved_filter_endpoint(
        saved_filter_id: str,
        session: Session = Depends(get_session),
    ) -> None:
        try:
            delete_saved_filter(session, saved_filter_id=saved_filter_id)
        except SavedFilterNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        _commit_or_raise(session)

    @app.post(
        "/v1/score-templates",
        response_model=ScoreTemplateResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_score_template(
        payload: ScoreTemplateCreate,
        session: Session = Depends(get_session),
    ) -> ScoreTemplateResponse:
        response = create_score_template(session, payload=payload)
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/score-templates",
        response_model=list[ScoreTemplateResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_score_templates(
        session: Session = Depends(get_session),
    ) -> list[ScoreTemplateResponse]:
        return list_score_templates(session)

    @app.post(
        "/v1/score-templates/{template_id}/score-all",
        response_model=ResumeScoreBatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_enqueue_resume_score_batch(
        template_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeScoreBatchResponse:
        try:
            response = enqueue_resume_score_batch(
                session,
                template_id=template_id,
                settings=settings,
            )
        except ScoreTemplateNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ScoreServiceError as exc:
            session.rollback()
            response_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if str(exc) == "deepseek_api_key_not_configured"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/resume-score-batches/{batch_id}",
        response_model=ResumeScoreBatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_score_batch_status(
        batch_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeScoreBatchResponse:
        try:
            return get_resume_score_batch(session, batch_id=batch_id)
        except ScoreServiceError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if str(exc) == "resume_score_batch_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @app.get(
        "/v1/resume-score-batches/{batch_id}/items",
        response_model=list[ResumeScoreBatchItemResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_score_batch_item_status(
        batch_id: str,
        session: Session = Depends(get_session),
    ) -> list[ResumeScoreBatchItemResponse]:
        try:
            return list_resume_score_batch_items(session, batch_id=batch_id)
        except ScoreServiceError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if str(exc) == "resume_score_batch_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @app.post(
        "/v1/resumes/{resume_id}/scores",
        response_model=ResumeScoreResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_resume_score(
        resume_id: str,
        payload: ResumeScoreCreate,
        session: Session = Depends(get_session),
    ) -> ResumeScoreResponse:
        try:
            response = run_resume_score(
                session,
                resume_id=resume_id,
                payload=payload,
                settings=settings,
            )
        except ScoreTemplateNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ScoreServiceError as exc:
            session.rollback()
            response_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if str(exc) == "deepseek_api_key_not_configured"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        except DeepSeekProviderError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="score_provider_failed",
            ) from exc
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/resume-scores/{score_id}",
        response_model=ResumeScoreResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_score(
        score_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeScoreResponse:
        try:
            return get_resume_score(session, score_id=score_id)
        except ResumeScoreNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/resumes/{resume_id}/scores",
        response_model=list[ResumeScoreResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_score_history(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> list[ResumeScoreResponse]:
        try:
            return list_resume_scores(session, resume_id=resume_id)
        except ScoreServiceError as exc:
            response_status = (
                status.HTTP_404_NOT_FOUND
                if str(exc) == "resume_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc

    @app.post(
        "/v1/resume-scores/{score_id}/dimensions/{dimension_key}/override",
        response_model=ResumeScoreResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_score_override(
        score_id: str,
        dimension_key: str,
        payload: ResumeScoreOverride,
        session: Session = Depends(get_session),
    ) -> ResumeScoreResponse:
        try:
            response = override_score_dimension(
                session,
                score_id=score_id,
                dimension_key=dimension_key,
                payload=payload,
            )
        except ResumeScoreNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ScoreServiceError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/resumes/{resume_id}/summaries",
        response_model=ResumeSummaryResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_resume_summary(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeSummaryResponse:
        try:
            response = generate_resume_summary(
                session,
                resume_id=resume_id,
                settings=settings,
            )
        except SummaryServiceError as exc:
            session.rollback()
            response_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if str(exc) == "deepseek_api_key_not_configured"
                else (
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "resume_not_found"
                    else status.HTTP_409_CONFLICT
                )
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        except SummaryDeepSeekProviderError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="summary_provider_failed",
            ) from exc
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/resume-summaries/{summary_id}",
        response_model=ResumeSummaryResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_summary(
        summary_id: str,
        session: Session = Depends(get_session),
    ) -> ResumeSummaryResponse:
        try:
            return get_resume_summary(session, summary_id=summary_id)
        except ResumeSummaryNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/resumes/{resume_id}/summaries",
        response_model=list[ResumeSummaryResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_summary_versions(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> list[ResumeSummaryResponse]:
        try:
            return list_resume_summaries(session, resume_id=resume_id)
        except SummaryServiceError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if str(exc) == "resume_not_found"
                    else status.HTTP_409_CONFLICT
                ),
                detail=str(exc),
            ) from exc

    @app.post(
        "/v1/resume-summaries/{summary_id}/manual-versions",
        response_model=ResumeSummaryResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_manual_summary_version(
        summary_id: str,
        payload: ResumeSummaryManualCreate,
        session: Session = Depends(get_session),
    ) -> ResumeSummaryResponse:
        try:
            response = create_manual_summary_version(
                session,
                summary_id=summary_id,
                payload=payload,
            )
        except ResumeSummaryNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SummaryServiceError as exc:
            session.rollback()
            response_status = (
                status.HTTP_404_NOT_FOUND
                if str(exc) == "resume_not_found"
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/jobs/generate-jd",
        response_model=JobGenerationResponse,
        dependencies=[Depends(require_ai_jd_feature)],
    )
    def post_generate_job_description(
        payload: JobGenerationRequest,
    ) -> JobGenerationResponse:
        """Generate an editable JD before the client persists one confirmed version."""

        try:
            return generate_job_description(payload=payload, settings=settings)
        except JobServiceError as exc:
            _raise_job_service_error(exc)
        except JobDeepSeekProviderError as exc:
            logger.warning("JD generation provider failed: %s", exc)
            detail = (
                "jd_generation_response_truncated"
                if str(exc) == "deepseek_response_truncated"
                else "jd_generation_provider_failed"
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            ) from exc
        except Exception as exc:  # pragma: no cover - final availability guard
            logger.exception("JD generation service failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="jd_generation_service_unavailable",
            ) from exc

    @app.post(
        "/v1/jobs",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_job(
        payload: JobCreate,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        """Create a new immutable JD version (draft unless requirements are supplied)."""

        try:
            response = create_job(session, payload=payload)
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/jobs/publish-original",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_publish_original_job(
        payload: OriginalJobPublishRequest,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        """Publish an externally supplied JD as-is, without calling an AI model."""

        try:
            response = publish_original_job(session, payload=payload)
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/jobs/{job_id}/versions",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_job_version(
        job_id: str,
        payload: JobCreate,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            response = create_job_version(session, job_id=job_id, payload=payload)
        except JobNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/jobs/{job_id}/versions",
        response_model=list[JobVersionResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_versions(
        job_id: str,
        session: Session = Depends(get_session),
    ) -> list[JobVersionResponse]:
        try:
            return list_job_versions(session, job_id=job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/jobs/latest-confirmed-version",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_latest_confirmed_job_version_detail(
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            return get_latest_confirmed_job_version(session)
        except JobVersionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/jobs/confirmed-versions",
        response_model=list[JobVersionResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_confirmed_job_versions(
        session: Session = Depends(get_session),
    ) -> list[JobVersionResponse]:
        """List the saved JDs available in the workspace switcher."""

        return list_confirmed_job_versions(session)

    @app.get(
        "/v1/job-versions/{job_version_id}",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_version_detail(
        job_version_id: str,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            return get_job_version(session, job_version_id=job_version_id)
        except JobVersionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/job-versions/{job_version_id}/matches",
        response_model=list[JobMatchResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_version_match_history(
        job_version_id: str,
        session: Session = Depends(get_session),
    ) -> list[JobMatchResponse]:
        try:
            return list_job_version_matches(session, job_version_id=job_version_id)
        except JobVersionNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post(
        "/v1/job-versions/{job_version_id}/extract",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_extract_job_version_requirements(
        job_version_id: str,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            response = extract_job_version_requirements(
                session,
                job_version_id=job_version_id,
                settings=settings,
            )
        except JobVersionNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        except JobDeepSeekProviderError as exc:
            session.rollback()
            detail = (
                "jd_requirements_response_truncated"
                if str(exc) == "deepseek_response_truncated"
                else "jd_requirements_provider_failed"
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=detail,
            ) from exc
        _commit_or_raise(session)
        return response

    @app.put(
        "/v1/job-versions/{job_version_id}/requirements",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def put_job_version_requirements(
        job_version_id: str,
        payload: JobVersionRequirementsUpdate,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            response = update_job_version_requirements(
                session,
                job_version_id=job_version_id,
                payload=payload,
            )
        except JobVersionNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/job-versions/{job_version_id}/confirm",
        response_model=JobVersionResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_confirm_job_version(
        job_version_id: str,
        session: Session = Depends(get_session),
    ) -> JobVersionResponse:
        try:
            response = confirm_job_version(session, job_version_id=job_version_id)
        except JobVersionNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/job-versions/{job_version_id}/match-all",
        response_model=JobMatchBatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_enqueue_job_version_match_batch(
        job_version_id: str,
        session: Session = Depends(get_session),
    ) -> JobMatchBatchResponse:
        try:
            response = enqueue_job_version_match_batch(
                session,
                job_version_id=job_version_id,
                settings=settings,
            )
        except JobVersionNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/job-match-batches/{batch_id}",
        response_model=JobMatchBatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_match_batch_status(
        batch_id: str,
        session: Session = Depends(get_session),
    ) -> JobMatchBatchResponse:
        try:
            return get_job_match_batch(session, batch_id=batch_id)
        except JobServiceError as exc:
            _raise_job_service_error(exc)

    @app.get(
        "/v1/job-match-batches/{batch_id}/items",
        response_model=list[JobMatchBatchItemResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_match_batch_items(
        batch_id: str,
        session: Session = Depends(get_session),
    ) -> list[JobMatchBatchItemResponse]:
        try:
            return list_job_match_batch_items(session, batch_id=batch_id)
        except JobServiceError as exc:
            _raise_job_service_error(exc)

    @app.post(
        "/v1/resumes/{resume_id}/job-matches",
        response_model=JobMatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_job_match(
        resume_id: str,
        payload: JobMatchCreate,
        session: Session = Depends(get_session),
    ) -> JobMatchResponse:
        try:
            response = run_job_match(
                session,
                resume_id=resume_id,
                payload=payload,
                settings=settings,
            )
        except JobVersionNotFoundError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except JobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        except JobDeepSeekProviderError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="jd_match_provider_failed",
            ) from exc
        _commit_or_raise(session)
        return response

    @app.get(
        "/v1/resumes/{resume_id}/job-matches",
        response_model=list[JobMatchResponse],
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_job_match_history(
        resume_id: str,
        session: Session = Depends(get_session),
    ) -> list[JobMatchResponse]:
        try:
            return list_resume_job_matches(session, resume_id=resume_id)
        except JobServiceError as exc:
            _raise_job_service_error(exc)

    @app.get(
        "/v1/job-matches/{match_id}",
        response_model=JobMatchResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_job_match_detail(
        match_id: str,
        session: Session = Depends(get_session),
    ) -> JobMatchResponse:
        try:
            return get_job_match(session, match_id=match_id)
        except JobMatchNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return app


app = create_app()
