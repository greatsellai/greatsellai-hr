from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from datetime import datetime
from ipaddress import ip_address, ip_network
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

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
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.sessions import SessionMiddleware

from app.config import AppSettings
from app.database import Database, get_session
from app.filter_options import filter_options_payload
from app.models import Candidate, MailboxConfig, Organization, ProductPlan, Resume
from app.schemas import (
    AuthLogin,
    AuthRegistration,
    AuthSession,
    AiModelPriceVersionCreate,
    AiModelPriceVersionResponse,
    AiModelProfileCreate,
    AiModelProfileResponse,
    AiProviderProfileCreate,
    AiProviderProfileResponse,
    AiRoutePolicyPublish,
    AiRoutePolicyResponse,
    AiRoutePolicyVersionResponse,
    AiRunUsageSummaryResponse,
    AiUsageAggregateResponse,
    AiUsageTrendBucketResponse,
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
    PlatformAuditEventListResponse,
    PlatformDashboardResponse,
    PlatformOrganizationDetailResponse,
    PlatformOrganizationListResponse,
    PlatformOrganizationPatch,
    PlatformUserDetailResponse,
    PlatformUserListResponse,
    PlatformUserPatch,
    RegistrationOfferResponse,
    MailboxConfigCreate,
    MailboxConfigListResponse,
    MailboxConfigPatch,
    MailboxConfigResponse,
    MailboxConfigUpdate,
    MailboxBackgroundJobBatchResponse,
    MailboxBackgroundJobHistoryResponse,
    MailboxBackgroundJobResponse,
    MailboxImportResponse,
    MailboxImportHistoryResponse,
    MailboxRetentionCleanupRunHistoryResponse,
    MailboxRetentionCleanupRunResponse,
    MailboxRetentionPolicyUpdate,
    MailboxRetentionPreviewResponse,
    MailboxRetentionSummaryResponse,
    CandidateCreate,
    CandidateCreated,
    CandidateDataAuditEventListResponse,
    CandidateDataDeletionBatchListResponse,
    CandidateDataDeletionRequest,
    CandidateDataDeletionResponse,
    CandidateDataExportCreate,
    CandidateDataExportListResponse,
    CandidateDataExportResponse,
    CandidateDataFileAccessRequest,
    CandidateDataFileAccessResponse,
    CandidateDataRestoreResponse,
    CandidateDataRetentionCleanupRunHistoryResponse,
    CandidateDataRetentionCleanupRunResponse,
    CandidateDataRetentionHoldUpdate,
    CandidateDataRetentionPolicyResponse,
    CandidateDataRetentionPolicyUpdate,
    CandidateDataRetentionPreviewRequest,
    CandidateDataRetentionPreviewResponse,
    CandidateSearchRequest,
    CandidateSearchResponse,
    TalentSearchProfileConfirmRequest,
    TalentSearchProfileGenerateRequest,
    TalentSearchProfileListResponse,
    TalentSearchProfileRefineRequest,
    TalentSearchProfileResponse,
    TalentSearchProfileRunRequest,
    TalentSearchProfileSearchRequest,
    TalentSearchRunResponse,
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
    ResumeContactResponse,
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
    RecruitingAgentContextBindRequest,
    RecruitingAgentConversationResponse,
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
    legacy_principal_from_session,
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
    LoginRateLimitError,
    PasswordResetRateLimitError,
    RegistrationRateLimitError,
    clear_login_account_backpressure,
    ensure_login_rate_limit_available,
    enforce_password_reset_rate_limit,
    enforce_registration_rate_limit,
    login_account_backpressure_delay_seconds,
    record_login_account_backpressure_failure,
    record_login_failure,
)
from app.services.public_auth_timing import (
    begin_password_reset_response,
    enforce_password_reset_minimum_response_time,
)
from app.services.transactional_email_outbox_service import (
    TransactionalEmailOutboxError,
    enqueue_password_reset_delivery,
)
from app.tenant_scope import organization_context_id, set_organization_context
from app.services.institution_service import (
    is_institution_registry_seeded,
    seed_institution_registry,
)
from app.services.ai_gateway_configuration_service import (
    AiGatewayConfigurationError,
    create_model_price_version,
    create_model_profile,
    create_provider_profile,
    list_model_price_versions,
    list_model_profiles,
    list_provider_profiles,
    list_route_policies,
    list_route_policy_versions,
    publish_route_policy,
)
from app.services.ai_usage_reporting_service import (
    AiUsageQuery,
    AiUsageReportingError,
    AiUsageTrendQuery,
    list_platform_ai_run_summaries,
    summarize_platform_ai_usage,
    summarize_platform_ai_usage_trend,
)
from app.services.platform_admin_service import (
    PlatformAdminServiceError,
    get_platform_dashboard,
    get_platform_organization,
    get_platform_user,
    list_platform_audit_events,
    list_platform_organizations,
    list_platform_users,
    organization_control_snapshot,
    patch_platform_organization,
    patch_platform_user,
    product_plan_snapshot,
    record_platform_audit_event,
)
from app.services.ai_extraction_job_service import (
    AiExtractionJobError,
    ai_extraction_state,
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
    RecruitingAgentConversationConflictError,
    RecruitingAgentConversationNotFoundError,
    RecruitingAgentContextReferenceNotFoundError,
    RecruitingAgentServiceError,
    bind_recruiting_agent_context,
    delete_recruiting_agent_conversation,
    get_recruiting_agent_conversation,
    run_recruiting_agent_turn,
)
from app.services.talent_search_profile_service import (
    DeepSeekProviderError as TalentProfileDeepSeekProviderError,
    JobServiceError as TalentProfileJobServiceError,
    TalentSearchProfileNotFoundError,
    TalentSearchProfileServiceError,
    confirm_profile,
    generate_profile,
    get_profile,
    get_profile_run,
    list_profiles as list_talent_search_profiles,
    refine_profile,
    start_profile_search,
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
    archive_mailbox_config,
    create_mailbox_config,
    get_mailbox_config,
    get_mailbox_config_by_id,
    list_mailbox_configs,
    list_mailbox_imports,
    save_mailbox_config,
    update_mailbox_config,
)
from app.services.mailbox_background_job_service import (
    enqueue_all_mailbox_sync_jobs,
    enqueue_mailbox_attachment_retry_job,
    enqueue_mailbox_sync_job,
    get_mailbox_background_job,
    list_mailbox_background_jobs,
)
from app.services.mailbox_retention_service import (
    MailboxRetentionError,
    cleanup_mailbox_retention,
    get_mailbox_retention_summary,
    list_mailbox_retention_cleanup_runs,
    preview_mailbox_retention_cleanup,
    update_mailbox_retention_policy,
)
from app.services.candidate_data_lifecycle_service import (
    CandidateDataLifecycleError,
    authorize_resume_original_access,
    delete_candidate,
    delete_resume,
    list_candidate_data_deletions,
    list_candidate_data_audit_events,
    list_retention_cleanup_runs,
    preview_retention_policy,
    resolve_resume_original_access,
    restore_deletion_batch,
    retention_policy_response,
    run_retention_cleanup,
    set_candidate_retention_hold,
    update_retention_policy,
)
from app.services.candidate_data_export_service import (
    authorize_candidate_data_export_download,
    cancel_candidate_data_export,
    create_candidate_data_export,
    get_candidate_data_export,
    list_candidate_data_exports,
    resolve_candidate_data_export_download,
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
        retention_hold=resume.retention_hold,
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


def _raise_ai_gateway_configuration_error(exc: AiGatewayConfigurationError) -> None:
    code = str(exc)
    if code in {
        "ai_provider_not_found",
        "ai_model_not_found",
        "ai_route_policy_not_found",
        "ai_route_model_not_found",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {
        "ai_provider_slug_exists",
        "ai_model_slug_exists",
        "ai_gateway_configuration_conflict",
    }:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(status_code=response_status, detail=code) from exc


def _raise_platform_admin_service_error(exc: PlatformAdminServiceError) -> None:
    code = str(exc)
    if code in {"platform_organization_not_found", "platform_user_not_found", "platform_plan_not_found"}:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {
        "platform_admin_self_deactivation_forbidden",
        "platform_admin_deactivation_forbidden",
    }:
        response_status = status.HTTP_403_FORBIDDEN
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(status_code=response_status, detail=code) from exc


def _mailbox_error_http_exception(exc: MailboxImportError) -> HTTPException:
    """Map stable mailbox service errors without exposing IMAP details."""

    code = str(exc)
    if code in {
        "mailbox_background_job_not_found",
        "mailbox_config_not_found",
        "mailbox_import_not_found",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {
        "mailbox_legacy_endpoint_ambiguous",
        "mailbox_duplicate_display_name",
        "mailbox_source_identity_locked",
        "mailbox_sync_in_progress",
        "mailbox_sync_claim_failed",
        "mailbox_config_archived",
        "mailbox_import_not_retryable",
        "mailbox_import_retry_in_progress",
        "mailbox_import_retry_superseded",
    }:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(status_code=response_status, detail=code)


def _mailbox_retention_error_http_exception(exc: MailboxRetentionError) -> HTTPException:
    """Map retention failures without selecting another mailbox channel."""

    code = str(exc)
    if code in {"mailbox_not_configured", "mailbox_config_not_found"}:
        response_status = status.HTTP_404_NOT_FOUND
    elif code == "mailbox_legacy_endpoint_ambiguous":
        response_status = status.HTTP_409_CONFLICT
    elif code == "mailbox_retention_policy_invalid":
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        response_status = status.HTTP_409_CONFLICT
    return HTTPException(status_code=response_status, detail=code)


def _candidate_data_error_http_exception(exc: CandidateDataLifecycleError) -> HTTPException:
    """Translate lifecycle failures without disclosing another workspace."""

    code = str(exc)
    if code in {
        "resume_not_found",
        "candidate_not_found",
        "candidate_data_file_access_not_found",
        "resume_original_file_not_found",
        "candidate_data_deletion_batch_not_found",
        "candidate_data_export_not_found",
        "candidate_data_export_candidate_not_found",
        "candidate_data_export_download_not_found",
        "candidate_data_export_output_not_found",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {
        "candidate_data_tombstone_secret_not_configured",
    }:
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif code in {
        "candidate_data_deletion_batch_not_restorable",
        "candidate_data_recovery_window_closed",
        "candidate_data_retention_preview_stale",
        "candidate_data_export_not_cancellable",
    }:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(status_code=response_status, detail=code)


def _private_file_response_headers() -> dict[str, str]:
    """Prevent candidate originals and exports from landing in shared caches."""

    return {
        "Cache-Control": "no-store, private",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


_FORCE_DOWNLOAD_ORIGINAL_SUFFIXES = frozenset({".htm", ".html"})


def _original_file_response_options(
    *,
    original_filename: str,
    requested_purpose: Literal["view", "download"],
) -> tuple[str, Literal["inline", "attachment"]]:
    """Return a browser-safe response policy for an untrusted original.

    HTML resumes are accepted only as source documents for text extraction.
    They must never render in the authenticated HR origin: a same-origin
    ``text/html`` response could execute candidate-controlled script with the
    viewer's session.  All HTML originals therefore download as opaque bytes,
    including the legacy compatibility endpoint that otherwise supports PDF
    inline preview.
    """

    if Path(original_filename).suffix.casefold() in _FORCE_DOWNLOAD_ORIGINAL_SUFFIXES:
        return "application/octet-stream", "attachment"
    media_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
    return media_type, "attachment" if requested_purpose == "download" else "inline"


def _candidate_data_session_nonce(request: Request) -> str:
    """Get a per-login opaque nonce for server-side file grants.

    Legacy local sessions may predate the nonce field.  Issuing one in the
    signed session preserves the compatibility path without binding a grant to
    every browser session of the same legacy identity.
    """

    nonce = request.session.get("resume_v3_session_nonce")
    if isinstance(nonce, str) and 32 <= len(nonce) <= 512:
        return nonce
    nonce = secrets.token_urlsafe(32)
    request.session["resume_v3_session_nonce"] = nonce
    return nonce


def _candidate_data_request_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        return None
    return normalized


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
                verification_token=token,
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
    """Return a safe public-auth throttle key without trusting spoofed headers.

    Registration, login, and password reset intentionally share this
    trusted-proxy resolver. It only accepts Caddy's appended final
    X-Forwarded-For value when the direct ASGI peer is explicitly trusted.
    """

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


def _password_reset_rate_limit_email_key(value: str) -> str:
    """Return an opaque namespace value for the password-reset email bucket.

    Valid addresses use the same normalized key as identity lookup. Invalid
    address-shaped input still receives a deterministic HMAC-only bucket so
    recovery requests retain their non-enumerating public behavior. The raw
    value never reaches the database: the rate-limit service hashes it with a
    server secret before persistence.
    """

    try:
        _, email_key = normalize_email(value)
    except IdentityServiceError:
        return f"invalid:{value.strip().casefold()}"
    return f"email:{email_key}"


def _login_rate_limit_email_key(value: str | None) -> str:
    """Return a HMAC-only account namespace for failed-login buckets."""

    if value is None or not value.strip():
        # The optional no-email shape belongs only to an explicitly enabled
        # legacy migration bridge. It still receives a durable budget.
        return "legacy_static_token"
    try:
        _, email_key = normalize_email(value)
    except IdentityServiceError:
        return f"invalid:{value.strip().casefold()}"
    return f"email:{email_key}"


def _public_auth_rate_limit_secret(settings: AppSettings) -> str:
    """Use the session key, never a static administrator token, for HMACs."""

    return settings.session_signing_secret()


def _raise_job_service_error(exc: JobServiceError) -> None:
    """Translate predictable JD workflow failures into stable HTTP results."""

    code = str(exc)
    if code in {"resume_not_found", "job_match_batch_not_found"}:
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


def _raise_talent_search_profile_error(exc: TalentSearchProfileServiceError) -> None:
    """Map profile errors without exposing another workspace or provider detail."""

    code = str(exc)
    if code in {
        "talent_search_profile_not_found",
        "talent_search_run_not_found",
        "job_version_not_found",
    }:
        response_status = status.HTTP_404_NOT_FOUND
    elif code in {
        "deepseek_api_key_not_configured",
        "ai_route_not_configured",
        "ai_route_not_published",
        "ai_route_disabled",
    }:
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif code in {
        "talent_search_profile_not_confirmed",
        "talent_search_profile_not_draft",
        "talent_search_profile_revision_not_current",
        "talent_search_profile_revision_superseded",
        "talent_search_profile_revision_missing",
        "talent_search_profile_search_in_progress",
    }:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(status_code=response_status, detail=code) from exc


def _resume_review_detail(resume: object) -> ResumeReviewDetail:
    base = _resume_detail(resume)
    return ResumeReviewDetail(
        **base.model_dump(),
        original_filename=resume.original_filename,
        facts_version=resume.facts_version,
        contacts=_resume_contacts(resume),
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
                institution_classification=education.institution_classification,
                classification_basis=education.classification_basis,
                classification_registry_version=education.classification_registry_version,
                classification_evidence_block_ids=(
                    education.classification_evidence_block_ids or []
                ),
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


def _resume_contacts(resume: object) -> list[ResumeContactResponse]:
    """Project validated local contacts only on an explicit review request."""

    contacts: list[ResumeContactResponse] = []
    for item in getattr(resume, "contact_details", None) or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        value = item.get("value")
        evidence_block_ids = item.get("evidence_block_ids")
        if kind not in {"email", "phone"} or not isinstance(value, str):
            continue
        normalized_value = value.strip()
        if not normalized_value or len(normalized_value) > 254:
            continue
        contacts.append(
            ResumeContactResponse(
                kind=kind,
                value=normalized_value,
                evidence_block_ids=[
                    block_id
                    for block_id in (evidence_block_ids or [])
                    if isinstance(block_id, str) and block_id.strip()
                ],
            )
        )
    return contacts


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
        if principal is None:
            principal = legacy_principal_from_session(session, request.session)
        if (
            principal is None
            and settings.legacy_admin_token_enabled
            and settings.admin_token
            and x_admin_token
            and hmac.compare_digest(x_admin_token, settings.admin_token)
        ):
            principal = legacy_principal(session)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            # Never reveal whether a legacy compatibility token exists or is
            # enabled. New production access is always a named account.
            detail="authentication_required",
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


async def require_platform_admin(
    principal: AuthPrincipal = Depends(require_authenticated_member),
) -> AuthPrincipal:
    """Gate platform-wide AI control plane endpoints.

    A workspace administrator is intentionally not sufficient: these routes
    change the model and credential-reference policy used by every customer.
    """

    if not principal.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_verification_required",
        )
    if not principal.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="platform_admin_required",
        )
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
        # Production never falls back to a legacy admin token or source-code
        # literal. The settings helper retains a local-development fallback.
        secret_key=settings.session_signing_secret(),
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
        if principal is None:
            principal = legacy_principal_from_session(session, request.session)
        if principal is not None:
            set_organization_context(session, principal.organization_id)
        return auth_session_response(principal, login_required=not settings.allow_unauthenticated)

    @app.post("/v1/auth/login", response_model=AuthSession)
    async def post_auth_login(
        payload: AuthLogin,
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthSession:
        rate_limit_kwargs = {
            "secret": _public_auth_rate_limit_secret(settings),
            "client_identifier": _registration_client_identifier(request, settings),
            "email_key": _login_rate_limit_email_key(payload.email),
            "client_limit": settings.login_rate_limit_client_limit,
            "client_window_seconds": settings.login_rate_limit_client_window_seconds,
            "email_limit": settings.login_rate_limit_email_limit,
            "email_window_seconds": settings.login_rate_limit_email_window_seconds,
        }
        if not settings.allow_unauthenticated:
            try:
                ensure_login_rate_limit_available(session, **rate_limit_kwargs)
            except LoginRateLimitError as exc:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="login_rate_limit_exceeded",
                ) from exc
            # Rotating source IPs cannot bypass this account-keyed pressure:
            # it is read before the expensive scrypt verification and grows
            # only after failed credentials. It is never a hard lock; the
            # configured delay is capped and a valid sign-in clears it.
            backpressure_delay = login_account_backpressure_delay_seconds(
                session,
                secret=rate_limit_kwargs["secret"],
                email_key=rate_limit_kwargs["email_key"],
                window_seconds=settings.login_account_backpressure_window_seconds,
                free_failures=settings.login_account_backpressure_free_failures,
                base_delay_seconds=settings.login_account_backpressure_base_delay_seconds,
                max_delay_seconds=settings.login_account_backpressure_max_delay_seconds,
            )
            if backpressure_delay > 0:
                await asyncio.sleep(backpressure_delay)
        try:
            if payload.email:
                principal = authenticate_email_password(
                    session,
                    email_value=payload.email,
                    password=payload.password,
                )
            elif (
                settings.legacy_admin_token_enabled
                and settings.admin_token
                and hmac.compare_digest(payload.password, settings.admin_token)
            ):
                principal = legacy_principal(session)
            elif settings.allow_unauthenticated:
                principal = legacy_principal(session)
            else:
                raise IdentityServiceError("invalid_login_credentials")
        except IdentityServiceError as exc:
            try:
                if not settings.allow_unauthenticated:
                    # A failed static-token compatibility attempt and a
                    # failed email/password attempt share the same durable
                    # non-enumerating public limiter.
                    #
                    # Commit the account-only progressive counter first. If
                    # the per-client hard limiter has already exhausted its
                    # short window, a distributed attack still cannot evade
                    # the cross-IP pre-verification delay by changing IP.
                    record_login_account_backpressure_failure(
                        session,
                        secret=rate_limit_kwargs["secret"],
                        email_key=rate_limit_kwargs["email_key"],
                        window_seconds=settings.login_account_backpressure_window_seconds,
                    )
                    _commit_or_raise(session)
                    record_login_failure(session, **rate_limit_kwargs)
                    _commit_or_raise(session)
            except LoginRateLimitError as rate_limit_exc:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="login_rate_limit_exceeded",
                ) from rate_limit_exc
            except HTTPException:
                session.rollback()
                raise
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_login_credentials",
            ) from exc
        if not settings.allow_unauthenticated:
            clear_login_account_backpressure(
                session,
                secret=rate_limit_kwargs["secret"],
                email_key=rate_limit_kwargs["email_key"],
            )
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
                secret=_public_auth_rate_limit_secret(settings),
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
        if existing_principal is None:
            existing_principal = legacy_principal_from_session(session, request.session)
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
        request: Request,
        session: Session = Depends(get_session),
    ) -> PasswordResetRequestResult:
        provider: TransactionalEmailProvider = request.app.state.transactional_email_provider
        settings: AppSettings = request.app.state.settings
        response_started_at = begin_password_reset_response()
        email_key = _password_reset_rate_limit_email_key(payload.email)
        timing_secret = _public_auth_rate_limit_secret(settings)
        try:
            try:
                # Persist abuse accounting before looking up the account or
                # issuing a token. In particular, a rejected request must never
                # reach issue_password_reset(), because that method intentionally
                # invalidates an older active recovery link when it replaces it.
                delivery_allowed = enforce_password_reset_rate_limit(
                    session,
                    secret=timing_secret,
                    client_identifier=_registration_client_identifier(request, settings),
                    email_key=email_key,
                    client_limit=settings.password_reset_rate_limit_client_limit,
                    client_window_seconds=settings.password_reset_rate_limit_client_window_seconds,
                    email_limit=settings.password_reset_rate_limit_email_limit,
                    email_window_seconds=settings.password_reset_rate_limit_email_window_seconds,
                )
                _commit_or_raise(session)
            except PasswordResetRateLimitError as exc:
                session.rollback()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="password_reset_rate_limit_exceeded",
                ) from exc
            # Registered, unknown, email-suppressed, and client-throttled
            # requests retain the same public response/timing strategy. A
            # known account receives a durable encrypted outbox row only when
            # the opaque email budget permits it; all provider I/O happens in
            # the worker after the HTTP response.
            if provider.password_reset_configured and delivery_allowed:
                try:
                    issued = issue_password_reset(
                        session,
                        email_value=payload.email,
                        ttl_seconds=settings.password_reset_ttl_seconds,
                    )
                    if issued is not None:
                        enqueue_password_reset_delivery(
                            session,
                            settings=settings,
                            issued=issued,
                        )
                    _commit_or_raise(session)
                except (TransactionalEmailOutboxError, IntegrityError, HTTPException):
                    # A production startup validates the key, but retain a safe
                    # public response if an operator rotates it incorrectly while
                    # the API is live or a concurrent reset races the enqueue.
                    # Do not leave an undeliverable active link, and never turn a
                    # registered account into a public existence signal.
                    session.rollback()
                    logger.warning("password_reset_outbox_enqueue_unavailable")
            return PasswordResetRequestResult(
                accepted=True,
                delivery_available=provider.password_reset_configured,
            )
        finally:
            await enforce_password_reset_minimum_response_time(
                started_at=response_started_at,
                minimum_seconds=settings.password_reset_min_response_seconds,
                jitter_seconds=settings.password_reset_response_jitter_seconds,
                secret=timing_secret,
                email_key=email_key,
            )

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

    @app.get(
        "/v1/platform/dashboard",
        response_model=PlatformDashboardResponse,
    )
    def get_platform_dashboard_endpoint(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformDashboardResponse:
        return get_platform_dashboard(session)

    @app.get(
        "/v1/platform/organizations",
        response_model=PlatformOrganizationListResponse,
    )
    def get_platform_organizations(
        search: str | None = Query(default=None, max_length=200),
        plan_code: str | None = Query(default=None, min_length=1, max_length=64),
        plan_status: str | None = Query(default=None, pattern="^(trial|active|expired|suspended|legacy)$"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformOrganizationListResponse:
        return list_platform_organizations(
            session,
            search=search,
            plan_code=plan_code,
            plan_status=plan_status,
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/v1/platform/organizations/{organization_id}",
        response_model=PlatformOrganizationDetailResponse,
    )
    def get_platform_organization_endpoint(
        organization_id: str,
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformOrganizationDetailResponse:
        try:
            return get_platform_organization(session, organization_id=organization_id)
        except PlatformAdminServiceError as exc:
            _raise_platform_admin_service_error(exc)

    @app.patch(
        "/v1/platform/organizations/{organization_id}",
        response_model=PlatformOrganizationDetailResponse,
    )
    def patch_platform_organization_endpoint(
        organization_id: str,
        payload: PlatformOrganizationPatch,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> PlatformOrganizationDetailResponse:
        try:
            response = patch_platform_organization(
                session,
                organization_id=organization_id,
                payload=payload,
                actor_user_id=principal.user.id,
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except PlatformAdminServiceError as exc:
            session.rollback()
            _raise_platform_admin_service_error(exc)

    @app.get(
        "/v1/platform/users",
        response_model=PlatformUserListResponse,
    )
    def get_platform_users(
        search: str | None = Query(default=None, max_length=320),
        is_active: bool | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformUserListResponse:
        return list_platform_users(
            session,
            search=search,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/v1/platform/users/{user_id}",
        response_model=PlatformUserDetailResponse,
    )
    def get_platform_user_endpoint(
        user_id: str,
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformUserDetailResponse:
        try:
            return get_platform_user(session, user_id=user_id)
        except PlatformAdminServiceError as exc:
            _raise_platform_admin_service_error(exc)

    @app.patch(
        "/v1/platform/users/{user_id}",
        response_model=PlatformUserDetailResponse,
    )
    def patch_platform_user_endpoint(
        user_id: str,
        payload: PlatformUserPatch,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> PlatformUserDetailResponse:
        try:
            response = patch_platform_user(
                session,
                user_id=user_id,
                payload=payload,
                actor_user_id=principal.user.id,
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except PlatformAdminServiceError as exc:
            session.rollback()
            _raise_platform_admin_service_error(exc)

    @app.get(
        "/v1/platform/audit-events",
        response_model=PlatformAuditEventListResponse,
    )
    def get_platform_audit_events(
        actor_user_id: str | None = Query(default=None, min_length=1, max_length=64),
        action: str | None = Query(default=None, min_length=1, max_length=100),
        target_type: str | None = Query(default=None, min_length=1, max_length=64),
        organization_id: str | None = Query(default=None, min_length=1, max_length=64),
        created_at_from: datetime | None = Query(default=None),
        created_at_to: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> PlatformAuditEventListResponse:
        try:
            return list_platform_audit_events(
                session,
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                organization_id=organization_id,
                created_at_from=created_at_from,
                created_at_to=created_at_to,
                limit=limit,
                offset=offset,
            )
        except PlatformAdminServiceError as exc:
            _raise_platform_admin_service_error(exc)

    @app.get("/v1/platform/plans", response_model=list[ProductPlanResponse])
    def get_platform_plans(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[ProductPlanResponse]:
        return list_product_plans(session)

    @app.put("/v1/platform/plans/{plan_code}", response_model=ProductPlanResponse)
    def put_platform_plan(
        plan_code: str,
        payload: ProductPlanUpdate,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> ProductPlanResponse:
        try:
            plan = session.scalar(select(ProductPlan).where(ProductPlan.code == plan_code))
            before = product_plan_snapshot(plan) if plan is not None else {}
            response = update_product_plan(session, code=plan_code, payload=payload)
            plan = session.scalar(select(ProductPlan).where(ProductPlan.code == plan_code))
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="product_plan.updated",
                target_type="product_plan",
                target_id=plan.id if plan is not None else plan_code,
                reason=payload.reason or "platform_plan_updated",
                before_state=before,
                after_state=product_plan_snapshot(plan) if plan is not None else {},
                request_id=x_request_id,
            )
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
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> OrganizationPlanResponse:
        try:
            organization = session.get(Organization, organization_id)
            before = organization_control_snapshot(organization) if organization is not None else {}
            response = assign_organization_plan(
                session,
                organization_id=organization_id,
                payload=payload,
            )
            organization = session.get(Organization, organization_id)
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="organization.plan_assigned",
                target_type="organization",
                target_id=organization_id,
                organization_id=organization_id,
                reason=payload.reason or "platform_organization_plan_assigned",
                before_state=before,
                after_state=(
                    organization_control_snapshot(organization)
                    if organization is not None
                    else {}
                ),
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except IdentityServiceError as exc:
            session.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.get(
        "/v1/platform/ai/providers",
        response_model=list[AiProviderProfileResponse],
    )
    def get_platform_ai_providers(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiProviderProfileResponse]:
        return list_provider_profiles(session, settings=settings)

    @app.post(
        "/v1/platform/ai/providers",
        response_model=AiProviderProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_platform_ai_provider(
        payload: AiProviderProfileCreate,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> AiProviderProfileResponse:
        try:
            response = create_provider_profile(
                session,
                payload=payload,
                settings=settings,
            )
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="ai_provider.created",
                target_type="ai_provider",
                target_id=response.provider_id,
                reason=payload.reason or "platform_ai_provider_created",
                after_state={
                    "provider_id": response.provider_id,
                    "slug": response.slug,
                    "driver": response.driver,
                    "is_enabled": response.is_enabled,
                },
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except AiGatewayConfigurationError as exc:
            session.rollback()
            _raise_ai_gateway_configuration_error(exc)

    @app.get(
        "/v1/platform/ai/models",
        response_model=list[AiModelProfileResponse],
    )
    def get_platform_ai_models(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiModelProfileResponse]:
        return list_model_profiles(session)

    @app.post(
        "/v1/platform/ai/models",
        response_model=AiModelProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_platform_ai_model(
        payload: AiModelProfileCreate,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> AiModelProfileResponse:
        try:
            response = create_model_profile(session, payload=payload)
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="ai_model.created",
                target_type="ai_model",
                target_id=response.model_id,
                reason=payload.reason or "platform_ai_model_created",
                after_state={
                    "model_id": response.model_id,
                    "slug": response.slug,
                    "provider_slug": response.provider_slug,
                    "capabilities": list(response.capabilities),
                    "is_enabled": response.is_enabled,
                },
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except AiGatewayConfigurationError as exc:
            session.rollback()
            _raise_ai_gateway_configuration_error(exc)

    @app.get(
        "/v1/platform/ai/model-prices",
        response_model=list[AiModelPriceVersionResponse],
    )
    def get_platform_ai_model_prices(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiModelPriceVersionResponse]:
        return list_model_price_versions(session)

    @app.post(
        "/v1/platform/ai/model-prices",
        response_model=AiModelPriceVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def post_platform_ai_model_price(
        payload: AiModelPriceVersionCreate,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> AiModelPriceVersionResponse:
        try:
            response = create_model_price_version(
                session,
                payload=payload,
                created_by_user_id=principal.user.id,
            )
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="ai_model_price.created",
                target_type="ai_model_price",
                target_id=response.price_version_id,
                reason=payload.reason or "platform_ai_model_price_created",
                after_state={
                    "price_version_id": response.price_version_id,
                    "model_slug": response.model_slug,
                    "currency": response.currency,
                    "effective_from": response.effective_from.isoformat(),
                    "effective_to": (
                        response.effective_to.isoformat()
                        if response.effective_to is not None
                        else None
                    ),
                    "input_per_million": (
                        str(response.input_per_million)
                        if response.input_per_million is not None
                        else None
                    ),
                    "output_per_million": (
                        str(response.output_per_million)
                        if response.output_per_million is not None
                        else None
                    ),
                    "request_unit_price": (
                        str(response.request_unit_price)
                        if response.request_unit_price is not None
                        else None
                    ),
                    "page_unit_price": (
                        str(response.page_unit_price)
                        if response.page_unit_price is not None
                        else None
                    ),
                    "is_active": response.is_active,
                },
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except AiGatewayConfigurationError as exc:
            session.rollback()
            _raise_ai_gateway_configuration_error(exc)

    @app.get(
        "/v1/platform/ai/routes",
        response_model=list[AiRoutePolicyResponse],
    )
    def get_platform_ai_routes(
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiRoutePolicyResponse]:
        return list_route_policies(session)

    @app.get(
        "/v1/platform/ai/routes/{feature}/versions",
        response_model=list[AiRoutePolicyVersionResponse],
    )
    def get_platform_ai_route_versions(
        feature: str,
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiRoutePolicyVersionResponse]:
        try:
            return list_route_policy_versions(session, feature=feature)
        except AiGatewayConfigurationError as exc:
            _raise_ai_gateway_configuration_error(exc)

    @app.put(
        "/v1/platform/ai/routes/{feature}",
        response_model=AiRoutePolicyVersionResponse,
    )
    def put_platform_ai_route(
        feature: str,
        payload: AiRoutePolicyPublish,
        principal: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
        x_request_id: Annotated[str | None, Header(max_length=128)] = None,
    ) -> AiRoutePolicyVersionResponse:
        try:
            current_policy = next(
                (policy for policy in list_route_policies(session) if policy.feature == feature),
                None,
            )
            response = publish_route_policy(
                session,
                feature=feature,
                payload=payload,
                published_by_user_id=principal.user.id,
                settings=settings,
            )
            record_platform_audit_event(
                session,
                actor_user_id=principal.user.id,
                action="ai_route.published",
                target_type="ai_route",
                target_id=response.policy_id,
                reason=payload.reason or "platform_ai_route_published",
                before_state=(
                    {
                        "feature": current_policy.feature,
                        "current_version": current_policy.current_version,
                        "is_enabled": current_policy.is_enabled,
                    }
                    if current_policy is not None
                    else {}
                ),
                after_state={
                    "feature": response.feature,
                    "version": response.version,
                    "targets": [
                        {
                            "model_slug": target.model_slug,
                            "max_attempts": target.max_attempts,
                            "allow_fallback_on": list(target.allow_fallback_on),
                        }
                        for target in response.targets
                    ],
                },
                request_id=x_request_id,
            )
            _commit_or_raise(session)
            return response
        except AiGatewayConfigurationError as exc:
            session.rollback()
            _raise_ai_gateway_configuration_error(exc)

    @app.get(
        "/v1/platform/ai/usage/runs",
        response_model=list[AiRunUsageSummaryResponse],
    )
    def get_platform_ai_usage_runs(
        organization_id: str | None = Query(default=None, min_length=1, max_length=64),
        feature: str | None = Query(default=None, min_length=1, max_length=64),
        started_at_from: datetime | None = Query(default=None),
        started_at_to: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiRunUsageSummaryResponse]:
        """List safe ledger metadata without candidate or prompt content."""

        try:
            rows = list_platform_ai_run_summaries(
                session,
                query=AiUsageQuery(
                    organization_id=organization_id,
                    feature=feature,
                    started_at_from=started_at_from,
                    started_at_to=started_at_to,
                    limit=limit,
                    offset=offset,
                ),
            )
        except AiUsageReportingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return [
            AiRunUsageSummaryResponse(
                run_id=row.run_id,
                organization_id=row.organization_id,
                feature=row.feature,
                service_kind=row.service_kind,
                status=row.status,
                started_at=row.started_at,
                finished_at=row.finished_at,
                total_cost_cny_micros=row.total_cost_cny_micros,
                cost_status=row.cost_status,
                invocation_count=row.invocation_count,
                potentially_billed_invocation_count=(
                    row.potentially_billed_invocation_count
                ),
                token_usage_invocation_count=row.token_usage_invocation_count,
                total_tokens=row.total_tokens,
            )
            for row in rows
        ]

    @app.get(
        "/v1/platform/ai/usage/summary",
        response_model=list[AiUsageAggregateResponse],
    )
    def get_platform_ai_usage_summary(
        organization_id: str | None = Query(default=None, min_length=1, max_length=64),
        feature: str | None = Query(default=None, min_length=1, max_length=64),
        provider_slug: str | None = Query(default=None, min_length=1, max_length=64),
        model_slug: str | None = Query(default=None, min_length=1, max_length=128),
        started_at_from: datetime | None = Query(default=None),
        started_at_to: datetime | None = Query(default=None),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiUsageAggregateResponse]:
        """Aggregate platform AI usage by workspace, feature, Provider, and model."""

        try:
            rows = summarize_platform_ai_usage(
                session,
                query=AiUsageQuery(
                    organization_id=organization_id,
                    feature=feature,
                    provider_slug=provider_slug,
                    model_slug=model_slug,
                    started_at_from=started_at_from,
                    started_at_to=started_at_to,
                    # Aggregation is not paginated; it has one bounded group
                    # per workspace/feature/Provider/model and does not expose rows.
                    limit=500,
                ),
            )
        except AiUsageReportingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return [
            AiUsageAggregateResponse(
                organization_id=row.organization_id,
                feature=row.feature,
                provider_slug=row.provider_slug,
                model_slug=row.model_slug,
                invocation_count=row.invocation_count,
                costed_invocation_count=row.costed_invocation_count,
                unavailable_cost_invocation_count=row.unavailable_cost_invocation_count,
                potentially_billed_invocation_count=(
                    row.potentially_billed_invocation_count
                ),
                reported_cost_cny_micros=row.reported_cost_cny_micros,
                token_usage_invocation_count=row.token_usage_invocation_count,
                input_tokens=row.input_tokens,
                cached_read_input_tokens=row.cached_read_input_tokens,
                cached_write_input_tokens=row.cached_write_input_tokens,
                output_tokens=row.output_tokens,
                reasoning_tokens=row.reasoning_tokens,
                total_tokens=row.total_tokens,
                known_run_count=row.known_run_count,
                partial_run_count=row.partial_run_count,
                unavailable_run_count=row.unavailable_run_count,
            )
            for row in rows
        ]

    @app.get(
        "/v1/platform/ai/usage/trend",
        response_model=list[AiUsageTrendBucketResponse],
    )
    def get_platform_ai_usage_trend(
        organization_id: str | None = Query(default=None, min_length=1, max_length=64),
        feature: str | None = Query(default=None, min_length=1, max_length=64),
        provider_slug: str | None = Query(default=None, min_length=1, max_length=64),
        model_slug: str | None = Query(default=None, min_length=1, max_length=128),
        started_at_from: datetime | None = Query(default=None),
        started_at_to: datetime | None = Query(default=None),
        granularity: Literal["hour", "day"] = Query(default="day"),
        time_zone: str = Query(
            default="UTC",
            min_length=1,
            max_length=64,
            description="IANA timezone used for calendar buckets, for example Asia/Shanghai.",
        ),
        _: AuthPrincipal = Depends(require_platform_admin),
        session: Session = Depends(get_session),
    ) -> list[AiUsageTrendBucketResponse]:
        """Return bounded, model-scoped Token buckets for the platform chart.

        The interval is applied to actual provider invocation start times.  A
        missing interval defaults to the latest 30 days; hourly requests are
        capped at 31 days and daily requests at 90 days.  Boundaries are
        absolute timestamps; ``time_zone`` controls only the calendar bucket
        labels and defaults to UTC for backwards compatibility.
        """

        try:
            rows = summarize_platform_ai_usage_trend(
                session,
                query=AiUsageTrendQuery(
                    organization_id=organization_id,
                    feature=feature,
                    provider_slug=provider_slug,
                    model_slug=model_slug,
                    started_at_from=started_at_from,
                    started_at_to=started_at_to,
                    granularity=granularity,
                    time_zone=time_zone,
                ),
            )
        except AiUsageReportingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        return [
            AiUsageTrendBucketResponse(
                bucket_started_at=row.bucket_started_at,
                time_zone=row.time_zone,
                provider_slug=row.provider_slug,
                model_slug=row.model_slug,
                invocation_count=row.invocation_count,
                token_usage_invocation_count=row.token_usage_invocation_count,
                input_tokens=row.input_tokens,
                cached_read_input_tokens=row.cached_read_input_tokens,
                cached_write_input_tokens=row.cached_write_input_tokens,
                output_tokens=row.output_tokens,
                reasoning_tokens=row.reasoning_tokens,
                total_tokens=row.total_tokens,
            )
            for row in rows
        ]

    @app.get(
        "/v1/mailboxes",
        response_model=MailboxConfigListResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailboxes(
        include_archived: bool = False,
        session: Session = Depends(get_session),
    ) -> MailboxConfigListResponse:
        return list_mailbox_configs(session, include_archived=include_archived)

    @app.post(
        "/v1/mailboxes",
        response_model=MailboxConfigResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox(
        payload: MailboxConfigCreate,
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return create_mailbox_config(session, settings=settings, payload=payload)
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    # Keep this static route before ``/{mailbox_id}`` so routing never treats
    # the literal word "sync" as a mailbox identifier.
    @app.post(
        "/v1/mailboxes/sync",
        response_model=MailboxBackgroundJobBatchResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_all_mailbox_syncs(
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobBatchResponse:
        try:
            return enqueue_all_mailbox_sync_jobs(session, settings=settings)
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailboxes/{mailbox_id}",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return get_mailbox_config_by_id(session, config_id=mailbox_id)
        except MailboxImportError as exc:
            raise _mailbox_error_http_exception(exc) from exc

    @app.patch(
        "/v1/mailboxes/{mailbox_id}",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def patch_mailbox(
        mailbox_id: str,
        payload: MailboxConfigPatch,
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return update_mailbox_config(
                session,
                settings=settings,
                config_id=mailbox_id,
                payload=payload,
            )
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailboxes/{mailbox_id}/sync",
        response_model=MailboxBackgroundJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_sync_by_id(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobResponse:
        try:
            return enqueue_mailbox_sync_job(
                session,
                settings=settings,
                mailbox_config_id=mailbox_id,
            )
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailboxes/{mailbox_id}/archive",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_archive(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return archive_mailbox_config(session, config_id=mailbox_id)
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailboxes/{mailbox_id}/retention",
        response_model=MailboxRetentionSummaryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_named_mailbox_retention(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxRetentionSummaryResponse:
        try:
            return get_mailbox_retention_summary(
                session,
                settings=settings,
                config_id=mailbox_id,
            )
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.put(
        "/v1/mailboxes/{mailbox_id}/retention",
        response_model=MailboxRetentionSummaryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def put_named_mailbox_retention(
        mailbox_id: str,
        payload: MailboxRetentionPolicyUpdate,
        session: Session = Depends(get_session),
    ) -> MailboxRetentionSummaryResponse:
        try:
            return update_mailbox_retention_policy(
                session,
                settings=settings,
                retention_policy=payload.retention_policy,
                config_id=mailbox_id,
            )
        except MailboxRetentionError as exc:
            session.rollback()
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailboxes/{mailbox_id}/retention/preview",
        response_model=MailboxRetentionPreviewResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_named_mailbox_retention_preview(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxRetentionPreviewResponse:
        try:
            return preview_mailbox_retention_cleanup(
                session,
                settings=settings,
                config_id=mailbox_id,
            )
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailboxes/{mailbox_id}/retention/cleanup",
        response_model=MailboxRetentionCleanupRunResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_named_mailbox_retention_cleanup(
        mailbox_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxRetentionCleanupRunResponse:
        try:
            return cleanup_mailbox_retention(
                session,
                settings=settings,
                trigger_type="manual",
                config_id=mailbox_id,
            )
        except MailboxRetentionError as exc:
            session.rollback()
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailboxes/{mailbox_id}/retention/runs",
        response_model=MailboxRetentionCleanupRunHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_named_mailbox_retention_cleanup_runs(
        mailbox_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> MailboxRetentionCleanupRunHistoryResponse:
        try:
            return list_mailbox_retention_cleanup_runs(
                session,
                settings=settings,
                limit=limit,
                config_id=mailbox_id,
            )
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

    # Compatibility routes remain safe only while one active source exists.
    # They deliberately fail instead of guessing the latest mailbox once a
    # workspace has more than one named channel.
    @app.get(
        "/v1/mailbox/config",
        response_model=MailboxConfigResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_configuration(
        session: Session = Depends(get_session),
    ) -> MailboxConfigResponse:
        try:
            return get_mailbox_config(session)
        except MailboxImportError as exc:
            raise _mailbox_error_http_exception(exc) from exc

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
            raise _mailbox_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailbox/retention",
        response_model=MailboxRetentionSummaryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_retention(
        session: Session = Depends(get_session),
    ) -> MailboxRetentionSummaryResponse:
        try:
            return get_mailbox_retention_summary(session, settings=settings)
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

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
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailbox/retention/preview",
        response_model=MailboxRetentionPreviewResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_retention_preview(
        session: Session = Depends(get_session),
    ) -> MailboxRetentionPreviewResponse:
        try:
            return preview_mailbox_retention_cleanup(session, settings=settings)
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

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
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailbox/retention/runs",
        response_model=MailboxRetentionCleanupRunHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_retention_cleanup_runs(
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> MailboxRetentionCleanupRunHistoryResponse:
        try:
            return list_mailbox_retention_cleanup_runs(
                session,
                settings=settings,
                limit=limit,
            )
        except MailboxRetentionError as exc:
            raise _mailbox_retention_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailbox/sync",
        response_model=MailboxBackgroundJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_sync(
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobResponse:
        try:
            config = get_mailbox_config(session)
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc
        if not config.configured or not config.mailbox_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="mailbox_not_configured",
            )
        try:
            return enqueue_mailbox_sync_job(
                session,
                settings=settings,
                mailbox_config_id=config.mailbox_id,
            )
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.post(
        "/v1/mailbox/imports/{import_id}/retry",
        response_model=MailboxBackgroundJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def post_mailbox_attachment_retry(
        import_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobResponse:
        try:
            return enqueue_mailbox_attachment_retry_job(
                session,
                settings=settings,
                import_id=import_id,
            )
        except MailboxImportError as exc:
            session.rollback()
            raise _mailbox_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailbox/tasks/{job_id}",
        response_model=MailboxBackgroundJobResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_task(
        job_id: str,
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobResponse:
        try:
            return get_mailbox_background_job(session, job_id=job_id)
        except MailboxImportError as exc:
            raise _mailbox_error_http_exception(exc) from exc

    @app.get(
        "/v1/mailbox/tasks",
        response_model=MailboxBackgroundJobHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_mailbox_tasks(
        mailbox_id: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_session),
    ) -> MailboxBackgroundJobHistoryResponse:
        try:
            return list_mailbox_background_jobs(
                session,
                limit=limit,
                offset=offset,
                mailbox_config_id=mailbox_id,
            )
        except MailboxImportError as exc:
            raise _mailbox_error_http_exception(exc) from exc

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

    @app.get(
        "/v1/mailbox-imports",
        response_model=MailboxImportHistoryResponse,
        dependencies=[Depends(require_mailbox_feature)],
    )
    def get_named_mailbox_import_history(
        mailbox_id: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=40, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> MailboxImportHistoryResponse:
        try:
            return list_mailbox_imports(
                session,
                limit=limit,
                mailbox_config_id=mailbox_id,
            )
        except MailboxImportError as exc:
            raise _mailbox_error_http_exception(exc) from exc

    @app.post(
        "/v1/recruiting-agent/turns",
        response_model=RecruitingAgentResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_recruiting_agent_turn(
        payload: RecruitingAgentRequest,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> RecruitingAgentResponse:
        """Run one bounded, tool-backed recruiter assistant turn."""

        try:
            response = run_recruiting_agent_turn(
                session,
                payload=payload,
                settings=settings,
                actor_user_id=principal.user.id,
                # Core recruiting tools remain available to an active
                # recruiter. Mailbox operations use the same entitlement and
                # organization-admin boundary as the dedicated mailbox APIs.
                mailbox_tools_available=(
                    principal.role == "admin"
                    and require_feature(principal, "mailbox_import")
                ),
            )
            _commit_or_raise(session)
        except (
            RecruitingAgentConversationNotFoundError,
            RecruitingAgentContextReferenceNotFoundError,
        ) as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "agent_context_reference_not_found"
                    if isinstance(exc, RecruitingAgentContextReferenceNotFoundError)
                    else "agent_conversation_not_found"
                ),
            ) from exc
        except RecruitingAgentConversationConflictError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_conversation_stale",
            ) from exc
        except StaleDataError as exc:
            # A row-version conflict can be raised by the final request
            # commit, after the graph has finished. Preserve the same public
            # stale-session contract as an early conflict.
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_conversation_stale",
            ) from exc
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

    @app.get(
        "/v1/recruiting-agent/conversations/{conversation_id}",
        response_model=RecruitingAgentConversationResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_recruiting_agent_work_session(
        conversation_id: str,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> RecruitingAgentConversationResponse:
        """Restore only the caller's safe Agent work-state summary."""

        try:
            return get_recruiting_agent_conversation(
                session,
                conversation_id=conversation_id,
                actor_user_id=principal.user.id,
            )
        except RecruitingAgentConversationNotFoundError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent_conversation_not_found",
            ) from exc

    @app.post(
        "/v1/recruiting-agent/conversations/context",
        response_model=RecruitingAgentConversationResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def bind_recruiting_agent_work_context(
        payload: RecruitingAgentContextBindRequest,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> RecruitingAgentConversationResponse:
        """Use one verified talent-profile run as the private Agent scope.

        This is intentionally not an AI turn: it lets the recruiter choose a
        visible result set before asking a follow-up question, without storing
        browser candidate IDs or spending an LLM call.
        """

        try:
            response = bind_recruiting_agent_context(
                session,
                payload=payload,
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
        except (
            RecruitingAgentConversationNotFoundError,
            RecruitingAgentContextReferenceNotFoundError,
        ) as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "agent_context_reference_not_found"
                    if isinstance(exc, RecruitingAgentContextReferenceNotFoundError)
                    else "agent_conversation_not_found"
                ),
            ) from exc
        except RecruitingAgentConversationConflictError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_conversation_stale",
            ) from exc
        except StaleDataError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_conversation_stale",
            ) from exc
        return response

    @app.delete(
        "/v1/recruiting-agent/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_single_admin)],
    )
    def delete_recruiting_agent_work_session(
        conversation_id: str,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> None:
        """Forget the caller's private Agent work-state immediately."""

        try:
            delete_recruiting_agent_conversation(
                session,
                conversation_id=conversation_id,
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
        except RecruitingAgentConversationNotFoundError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent_conversation_not_found",
            ) from exc
        except StaleDataError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_conversation_stale",
            ) from exc

    @app.post(
        "/v1/talent-search-profiles/generate",
        response_model=TalentSearchProfileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_generate_talent_search_profile(
        payload: TalentSearchProfileGenerateRequest,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> TalentSearchProfileResponse:
        """Ask AI for a draft only; candidate recall begins after HR confirmation."""

        try:
            response = generate_profile(
                session,
                payload=payload,
                settings=settings,
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
        except TalentSearchProfileNotFoundError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentProfileDeepSeekProviderError as exc:
            session.rollback()
            logger.warning("Talent-search profile provider failed: %s", exc)
            detail = (
                "talent_search_profile_response_truncated"
                if str(exc) == "deepseek_response_truncated"
                else "talent_search_profile_provider_failed"
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
        except Exception as exc:
            session.rollback()
            logger.exception("Talent-search profile generation failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc
        return response

    @app.get(
        "/v1/talent-search-profiles",
        response_model=TalentSearchProfileListResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_talent_search_profiles(
        limit: int = Query(default=12, ge=1, le=50),
        session: Session = Depends(get_session),
    ) -> TalentSearchProfileListResponse:
        try:
            return TalentSearchProfileListResponse(
                items=list_talent_search_profiles(session, limit=limit)
            )
        except TalentSearchProfileServiceError as exc:
            _raise_talent_search_profile_error(exc)
        except Exception as exc:
            logger.exception("Talent-search profile read failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc

    @app.get(
        "/v1/talent-search-profiles/{profile_id}",
        response_model=TalentSearchProfileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_talent_search_profile(
        profile_id: str,
        session: Session = Depends(get_session),
    ) -> TalentSearchProfileResponse:
        try:
            return get_profile(session, profile_id=profile_id)
        except TalentSearchProfileNotFoundError as exc:
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            _raise_talent_search_profile_error(exc)
        except Exception as exc:
            logger.exception("Talent-search profile read failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc

    @app.post(
        "/v1/talent-search-profiles/{profile_id}/refine",
        response_model=TalentSearchProfileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_refine_talent_search_profile(
        profile_id: str,
        payload: TalentSearchProfileRefineRequest,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> TalentSearchProfileResponse:
        try:
            response = refine_profile(
                session,
                profile_id=profile_id,
                payload=payload,
                settings=settings,
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
        except TalentSearchProfileNotFoundError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentProfileDeepSeekProviderError as exc:
            session.rollback()
            logger.warning("Talent-search profile refinement provider failed: %s", exc)
            detail = (
                "talent_search_profile_response_truncated"
                if str(exc) == "deepseek_response_truncated"
                else "talent_search_profile_provider_failed"
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
        except Exception as exc:
            session.rollback()
            logger.exception("Talent-search profile refinement failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc
        return response

    @app.post(
        "/v1/talent-search-profiles/{profile_id}/confirm",
        response_model=TalentSearchProfileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_confirm_talent_search_profile(
        profile_id: str,
        payload: TalentSearchProfileConfirmRequest,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> TalentSearchProfileResponse:
        try:
            response = confirm_profile(
                session,
                profile_id=profile_id,
                payload=payload,
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
        except TalentSearchProfileNotFoundError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentProfileJobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        except Exception as exc:
            session.rollback()
            logger.exception("Talent-search profile confirmation failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc
        return response

    @app.post(
        "/v1/talent-search-profiles/{profile_id}/runs",
        response_model=TalentSearchRunResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_talent_search_profile_run(
        profile_id: str,
        payload: TalentSearchProfileRunRequest,
        session: Session = Depends(get_session),
    ) -> TalentSearchRunResponse:
        try:
            response = start_profile_search(
                session,
                profile_id=profile_id,
                payload=payload,
                settings=settings,
            )
            _commit_or_raise(session)
        except TalentSearchProfileNotFoundError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            session.rollback()
            _raise_talent_search_profile_error(exc)
        except TalentProfileJobServiceError as exc:
            session.rollback()
            _raise_job_service_error(exc)
        except Exception as exc:
            session.rollback()
            logger.exception("Talent-search profile run failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc
        return response

    @app.get(
        "/v1/talent-search-profiles/{profile_id}/runs/{run_id}",
        response_model=TalentSearchRunResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_talent_search_profile_run(
        profile_id: str,
        run_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=200),
        session: Session = Depends(get_session),
    ) -> TalentSearchRunResponse:
        try:
            return get_profile_run(
                session,
                profile_id=profile_id,
                run_id=run_id,
                payload=TalentSearchProfileSearchRequest(limit=limit, cursor=cursor),
            )
        except TalentSearchProfileNotFoundError as exc:
            _raise_talent_search_profile_error(exc)
        except TalentSearchProfileServiceError as exc:
            _raise_talent_search_profile_error(exc)
        except Exception as exc:
            logger.exception("Talent-search profile run read failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="talent_search_profile_service_unavailable",
            ) from exc

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
            .options(
                selectinload(Resume.document_extraction_job),
                selectinload(Resume.ai_extraction_job),
            )
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

    @app.post(
        "/v1/resumes/{resume_id}/file-access",
        response_model=CandidateDataFileAccessResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def post_resume_original_file_access(
        resume_id: str,
        payload: CandidateDataFileAccessRequest,
        request: Request,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataFileAccessResponse:
        try:
            access = authorize_resume_original_access(
                session,
                settings=settings,
                resume_id=resume_id,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
                purpose=payload.purpose,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc
        return CandidateDataFileAccessResponse(
            access_url=f"/v1/file-access/{access.token}",
            expires_at=access.expires_at,
        )

    @app.get(
        "/v1/file-access/{opaque_token}",
        response_class=FileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_authorized_candidate_file(
        opaque_token: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> FileResponse:
        try:
            access = resolve_resume_original_access(
                session,
                settings=settings,
                opaque_token=opaque_token,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
            )
        except CandidateDataLifecycleError as exc:
            raise _candidate_data_error_http_exception(exc) from exc
        media_type, content_disposition_type = _original_file_response_options(
            original_filename=access.original_filename,
            requested_purpose=access.purpose,
        )
        return FileResponse(
            path=access.path,
            media_type=media_type,
            filename=access.original_filename,
            content_disposition_type=content_disposition_type,
            headers=_private_file_response_headers(),
        )

    @app.get(
        "/v1/resumes/{resume_id}/original-file",
        response_class=FileResponse,
        dependencies=[Depends(require_single_admin)],
    )
    def get_resume_original_file(
        resume_id: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_single_admin),
        session: Session = Depends(get_session),
    ) -> FileResponse:
        try:
            # Compatibility route: legacy clients still receive an inline
            # response, but no longer bypass the same explicit audit and
            # session-bound authorization rules as the new UI.
            granted = authorize_resume_original_access(
                session,
                settings=settings,
                resume_id=resume_id,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
                purpose="view",
                request_id=_candidate_data_request_id(request),
                source_kind="compatibility",
            )
            _commit_or_raise(session)
            access = resolve_resume_original_access(
                session,
                settings=settings,
                opaque_token=granted.token,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
            )
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc
        media_type, content_disposition_type = _original_file_response_options(
            original_filename=access.original_filename,
            requested_purpose="view",
        )
        return FileResponse(
            path=access.path,
            media_type=media_type,
            filename=access.original_filename,
            content_disposition_type=content_disposition_type,
            headers=_private_file_response_headers(),
        )

    @app.delete(
        "/v1/resumes/{resume_id}",
        response_model=CandidateDataDeletionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_organization_admin)],
    )
    def delete_resume_candidate_data(
        resume_id: str,
        payload: CandidateDataDeletionRequest,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataDeletionResponse:
        try:
            response = delete_resume(
                session,
                settings=settings,
                resume_id=resume_id,
                actor_user_id=principal.user.id,
                reason=payload.reason,
                private_note=payload.other_note,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.delete(
        "/v1/candidates/{candidate_id}",
        response_model=CandidateDataDeletionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_organization_admin)],
    )
    def delete_candidate_data(
        candidate_id: str,
        payload: CandidateDataDeletionRequest,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataDeletionResponse:
        try:
            response = delete_candidate(
                session,
                settings=settings,
                candidate_id=candidate_id,
                actor_user_id=principal.user.id,
                reason=payload.reason,
                private_note=payload.other_note,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.post(
        "/v1/candidate-data/deletions/{deletion_batch_id}/restore",
        response_model=CandidateDataRestoreResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_restore_candidate_data_deletion(
        deletion_batch_id: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataRestoreResponse:
        try:
            response = restore_deletion_batch(
                session,
                deletion_batch_id=deletion_batch_id,
                actor_user_id=principal.user.id,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.get(
        "/v1/candidate-data/deletions",
        response_model=CandidateDataDeletionBatchListResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_deletions(
        limit: int = Query(default=50, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> CandidateDataDeletionBatchListResponse:
        """Recovery console data, deliberately limited to opaque metadata."""

        return list_candidate_data_deletions(session, limit=limit)

    @app.put(
        "/v1/candidates/{candidate_id}/retention-hold",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_organization_admin)],
    )
    def put_candidate_retention_hold(
        candidate_id: str,
        payload: CandidateDataRetentionHoldUpdate,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> None:
        try:
            set_candidate_retention_hold(
                session,
                candidate_id=candidate_id,
                retention_hold=payload.retention_hold,
                actor_user_id=principal.user.id,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.get(
        "/v1/candidate-data/retention",
        response_model=CandidateDataRetentionPolicyResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_retention_policy(
        session: Session = Depends(get_session),
    ) -> CandidateDataRetentionPolicyResponse:
        response = retention_policy_response(session)
        _commit_or_raise(session)
        return response

    @app.post(
        "/v1/candidate-data/retention/preview",
        response_model=CandidateDataRetentionPreviewResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_candidate_data_retention_preview(
        payload: CandidateDataRetentionPreviewRequest,
        session: Session = Depends(get_session),
    ) -> CandidateDataRetentionPreviewResponse:
        try:
            response = preview_retention_policy(
                session,
                settings=settings,
                retention_days=payload.retention_days,
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.put(
        "/v1/candidate-data/retention",
        response_model=CandidateDataRetentionPolicyResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def put_candidate_data_retention_policy(
        payload: CandidateDataRetentionPolicyUpdate,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataRetentionPolicyResponse:
        try:
            response = update_retention_policy(
                session,
                settings=settings,
                mode=payload.mode,
                retention_days=payload.retention_days,
                preview_token=payload.preview_token,
                actor_user_id=principal.user.id,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.post(
        "/v1/candidate-data/retention/cleanup",
        response_model=CandidateDataRetentionCleanupRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_candidate_data_retention_cleanup(
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataRetentionCleanupRunResponse:
        try:
            response = run_retention_cleanup(
                session,
                settings=settings,
                trigger_type="manual",
                actor_user_id=principal.user.id,
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.get(
        "/v1/candidate-data/retention/runs",
        response_model=CandidateDataRetentionCleanupRunHistoryResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_retention_cleanup_runs(
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> CandidateDataRetentionCleanupRunHistoryResponse:
        return list_retention_cleanup_runs(session, limit=limit)

    @app.get(
        "/v1/candidate-data/audit-events",
        response_model=CandidateDataAuditEventListResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_audit_events(
        limit: int = Query(default=100, ge=1, le=200),
        session: Session = Depends(get_session),
    ) -> CandidateDataAuditEventListResponse:
        return list_candidate_data_audit_events(session, limit=limit)

    @app.post(
        "/v1/candidate-data-exports",
        response_model=CandidateDataExportResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_candidate_data_export(
        payload: CandidateDataExportCreate,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataExportResponse:
        """Queue a privacy-safe, asynchronous workspace export."""

        try:
            response = create_candidate_data_export(
                session,
                settings=settings,
                candidate_ids=payload.candidate_ids,
                include_originals=payload.include_originals,
                actor_user_id=principal.user.id,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.get(
        "/v1/candidate-data-exports",
        response_model=CandidateDataExportListResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_exports(
        limit: int = Query(default=50, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> CandidateDataExportListResponse:
        return list_candidate_data_exports(session, limit=limit)

    @app.get(
        "/v1/candidate-data-exports/{export_id}",
        response_model=CandidateDataExportResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_candidate_data_export_status(
        export_id: str,
        session: Session = Depends(get_session),
    ) -> CandidateDataExportResponse:
        try:
            return get_candidate_data_export(session, export_id=export_id)
        except CandidateDataLifecycleError as exc:
            raise _candidate_data_error_http_exception(exc) from exc

    @app.delete(
        "/v1/candidate-data-exports/{export_id}",
        response_model=CandidateDataExportResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_organization_admin)],
    )
    def delete_candidate_data_export(
        export_id: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataExportResponse:
        try:
            response = cancel_candidate_data_export(
                session,
                export_id=export_id,
                actor_user_id=principal.user.id,
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return response
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.post(
        "/v1/candidate-data-exports/{export_id}/download-access",
        response_model=CandidateDataFileAccessResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def post_candidate_data_export_download_access(
        export_id: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> CandidateDataFileAccessResponse:
        try:
            access = authorize_candidate_data_export_download(
                session,
                settings=settings,
                export_id=export_id,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
                request_id=_candidate_data_request_id(request),
            )
            _commit_or_raise(session)
            return CandidateDataFileAccessResponse(
                access_url=(
                    f"/v1/candidate-data-export-file-access/{access.token}"
                ),
                expires_at=access.expires_at,
            )
        except CandidateDataLifecycleError as exc:
            session.rollback()
            raise _candidate_data_error_http_exception(exc) from exc

    @app.get(
        "/v1/candidate-data-export-file-access/{opaque_token}",
        response_class=FileResponse,
        dependencies=[Depends(require_organization_admin)],
    )
    def get_authorized_candidate_data_export(
        opaque_token: str,
        request: Request,
        principal: AuthPrincipal = Depends(require_organization_admin),
        session: Session = Depends(get_session),
    ) -> FileResponse:
        try:
            access = resolve_candidate_data_export_download(
                session,
                settings=settings,
                opaque_token=opaque_token,
                actor_user_id=principal.user.id,
                session_nonce=_candidate_data_session_nonce(request),
            )
        except CandidateDataLifecycleError as exc:
            raise _candidate_data_error_http_exception(exc) from exc
        return FileResponse(
            path=access.path,
            media_type="application/zip",
            filename=access.filename,
            content_disposition_type="attachment",
            headers=_private_file_response_headers(),
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
        mailbox_id: str | None = Query(default=None, min_length=1, max_length=64),
        session: Session = Depends(get_session),
    ) -> ResumeLibraryResponse:
        if mailbox_id is not None and session.scalar(
            select(MailboxConfig.id).where(MailboxConfig.id == mailbox_id)
        ) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="mailbox_config_not_found",
            )
        return list_resume_library(
            session,
            page=page,
            page_size=page_size,
            mailbox_config_id=mailbox_id,
        )

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
        session: Session = Depends(get_session),
    ) -> JobGenerationResponse:
        """Generate an editable JD before the client persists one confirmed version."""

        try:
            return generate_job_description(
                session=session,
                payload=payload,
                settings=settings,
            )
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
