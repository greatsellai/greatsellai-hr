from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai.contracts import GatewayContractError, validate_external_https_endpoint
from app.services.normalization import normalized_contains, normalized_key


Month = str
DegreeLevel = Literal[
    "unknown",
    "vocational_or_below",
    "high_school",
    "associate",
    "bachelor",
    "master",
    "doctor",
]
ExperienceType = Literal[
    "employment",
    "internship",
    "project",
    "research",
    "competition",
    "campus",
    "club",
    "volunteer",
    "entrepreneurship",
    "training",
    "other",
    "unknown",
]
InstitutionTier = Literal[
    "211",
    "985",
    "double_first_class",
    "key_undergraduate",
    "first_tier",
    "second_tier",
    "regular_undergraduate",
    "private_undergraduate",
    "higher_vocational",
    "overseas",
    "undergraduate",
    "associate",
    "secondary_vocational",
]
InstitutionClassification = Literal[
    "985",
    "211",
    "undergraduate",
    "associate",
    "secondary_vocational",
    "overseas",
]
SkillCategory = Literal[
    "software",
    "data_ai",
    "product_project",
    "design_content",
    "marketing_ecommerce_operations",
    "sales_customer_service",
    "supply_chain_logistics",
    "finance_legal_hr",
    "office_collaboration",
    "industry_professional",
]
LanguageCredentialCode = Literal[
    "cet4", "cet6", "ielts", "toefl", "tem4", "tem8", "bec", "toeic", "custom"
]
PresenceStatus = Literal["any", "present", "unknown"]

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CANDIDATE_NAME_LABEL_PATTERN = re.compile(
    r"(?i)^\s*(?:\u59d3\u540d|name)\s*[:\uff1a]"
)
CANDIDATE_NAME_UNSAFE_CHARACTER_PATTERN = re.compile(r"[\r\n@]")
AI_CONFIG_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PROTECTED_CANDIDATE_KEYWORD_EXACT_KEYS = frozenset(
    normalized_key(value)
    for value in (
        "年龄",
        "年纪",
        "性别",
        "男",
        "女",
        "男性",
        "女性",
        "男女",
        "出生年月",
        "出生日期",
        "生日",
        "婚姻",
        "婚育",
        "已婚",
        "未婚",
        "生育",
        "怀孕",
        "民族",
        "宗教",
        "籍贯",
        "户籍",
        "国籍",
        "残障",
        "健康",
        "age",
        "gender",
        "male",
        "female",
        "sex",
        "date of birth",
        "birthday",
        "marital status",
        "marriage",
        "pregnancy",
        "ethnicity",
        "religion",
        "hometown",
        "household registration",
        "nationality",
        "disability",
        "health",
    )
)
_PROTECTED_CANDIDATE_KEYWORD_PREFIXES = tuple(
    normalized_key(value)
    for value in (
        "年龄",
        "年纪",
        "性别",
        "男性",
        "女性",
        "男生",
        "女生",
        "出生",
        "婚姻",
        "婚育",
        "生育",
        "孕",
        "民族",
        "宗教",
        "籍贯",
        "户籍",
        "国籍",
        "gender",
        "male",
        "female",
        "sex",
        "birthday",
        "date of birth",
        "marital",
        "pregnan",
        "ethnic",
        "religion",
        "hometown",
        "household registration",
        "nationality",
        "disability",
    )
)
_PROTECTED_CANDIDATE_AGE_KEY = re.compile(
    r"^(?:\d{1,3}岁(?:以下|以上|及以下|及以上|左右)?|(?:age)?(?:under|over|below|above)\d{1,3}|age\d{1,3})$"
)


def clean_string_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise ValueError("list values must not be blank")
        if not normalized_key(normalized):
            raise ValueError("list values must contain searchable characters")
        if normalized not in seen:
            seen.add(normalized)
            cleaned.append(normalized)
    return cleaned


def _is_protected_candidate_keyword(value: str) -> bool:
    key = normalized_key(value)
    return bool(
        key
        and (
            key in _PROTECTED_CANDIDATE_KEYWORD_EXACT_KEYS
            or key.startswith(_PROTECTED_CANDIDATE_KEYWORD_PREFIXES)
            or _PROTECTED_CANDIDATE_AGE_KEY.fullmatch(key)
        )
    )


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthLogin(ApiModel):
    # ``email`` is optional only for the temporary legacy-admin compatibility
    # path. New accounts always authenticate with email + password.
    email: str | None = Field(default=None, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class AuthUserResponse(ApiModel):
    user_id: str
    display_name: str
    email: str


class AuthOrganizationResponse(ApiModel):
    organization_id: str
    name: str


class AuthPlanResponse(ApiModel):
    code: str
    name: str
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class TrialAccessResponse(ApiModel):
    plan_status: Literal["trial", "active", "expired", "suspended"]
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    trial_days_remaining: int | None = None
    llm_call_limit: int | None = Field(default=None, ge=0)
    llm_call_used: int | None = Field(default=None, ge=0)
    llm_call_remaining: int | None = Field(default=None, ge=0)
    access_enabled: bool


class WorkspaceFeedbackAttachmentResponse(ApiModel):
    attachment_id: str
    original_filename: str
    content_type: str
    size_bytes: int = Field(ge=0)


class WorkspaceFeedbackResponse(ApiModel):
    feedback_id: str
    use_case: str
    intended_outcome: str
    friction: str
    desired_change: str
    reward_status: Literal["queued", "running", "granted"]
    reward_due_at: datetime
    reward_granted_at: datetime | None = None
    reward_call_count: int = Field(ge=0)
    attachments: list[WorkspaceFeedbackAttachmentResponse] = Field(default_factory=list)
    created_at: datetime


class WorkspaceFeedbackListResponse(ApiModel):
    items: list[WorkspaceFeedbackResponse] = Field(default_factory=list)
    next_submission_at: datetime | None = None


class WorkspaceFeedbackSubmitResponse(ApiModel):
    item: WorkspaceFeedbackResponse
    next_submission_at: datetime | None = None
    replayed: bool = False


class PlatformWorkspaceFeedbackResponse(ApiModel):
    """Platform-only view of feedback, including its submitter and workspace."""

    feedback_id: str
    organization_id: str
    organization_name: str
    submitted_by_user_id: str
    submitter_name: str
    submitter_email: str
    contact_phone: str | None = Field(default=None, max_length=32)
    use_case: str
    intended_outcome: str
    friction: str
    desired_change: str
    reward_status: Literal["queued", "running", "granted"]
    reward_due_at: datetime
    reward_granted_at: datetime | None = None
    reward_call_count: int = Field(ge=0)
    attachments: list[WorkspaceFeedbackAttachmentResponse] = Field(default_factory=list)
    created_at: datetime


class PlatformWorkspaceFeedbackListResponse(ApiModel):
    items: list[PlatformWorkspaceFeedbackResponse] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AuthSession(ApiModel):
    authenticated: bool
    login_required: bool
    is_platform_admin: bool = False
    email_verified: bool = False
    email_verification_required: bool = False
    user: AuthUserResponse | None = None
    organization: AuthOrganizationResponse | None = None
    role: Literal["admin", "recruiter"] | None = None
    plan: AuthPlanResponse | None = None
    trial: TrialAccessResponse | None = None


class AuthRegistration(ApiModel):
    organization_name: str = Field(min_length=2, max_length=200)
    full_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=512)


class RegistrationOfferResponse(ApiModel):
    """The current server-owned self-service registration offer."""

    plan_code: str
    plan_name: str
    trial_days: int = Field(ge=0)
    llm_call_limit: int = Field(ge=0)


class EmailVerificationComplete(ApiModel):
    token: str = Field(min_length=20, max_length=512)


class EmailVerificationResendResult(ApiModel):
    accepted: bool = True
    delivery_available: bool = True


class PasswordResetRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetRequestResult(ApiModel):
    accepted: bool = True
    delivery_available: bool = False


class PasswordResetComplete(ApiModel):
    token: str = Field(min_length=20, max_length=512)
    password: str = Field(min_length=8, max_length=512)


class OrganizationInvitationCreate(ApiModel):
    role: Literal["admin", "recruiter"] = "recruiter"
    email: str | None = Field(default=None, max_length=320)
    expires_in_days: int = Field(default=7, ge=1, le=30)


class OrganizationInvitationResponse(ApiModel):
    invitation_id: str
    role: Literal["admin", "recruiter"]
    email: str | None = None
    expires_at: datetime
    invitation_token: str | None = None


class OrganizationInvitationAccept(ApiModel):
    invitation_token: str = Field(min_length=20, max_length=512)
    full_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=512)


class OrganizationPlanResponse(ApiModel):
    organization_id: str
    plan_code: str
    plan_name: str
    monthly_price_cents: int
    plan_status: Literal["trial", "active", "expired", "suspended"]
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    feature_flags: dict[str, bool] = Field(default_factory=dict)


class ProductPlanResponse(ApiModel):
    plan_id: str
    code: str
    name: str
    monthly_price_cents: int
    trial_days: int
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    is_active: bool
    is_available_for_signup: bool
    is_default_trial: bool
    sort_order: int


class ProductPlanUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    monthly_price_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    trial_days: int | None = Field(default=None, ge=0, le=365)
    feature_flags: dict[str, bool] | None = None
    is_active: bool | None = None
    is_available_for_signup: bool | None = None
    is_default_trial: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_optional_plan_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized


class OrganizationPlanAssign(ApiModel):
    plan_code: str = Field(min_length=1, max_length=64)
    plan_status: Literal["trial", "active", "expired", "suspended"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_optional_assignment_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized


class PlatformDashboardResponse(ApiModel):
    generated_at: datetime
    organizations_total: int
    organizations_by_status: dict[str, int] = Field(default_factory=dict)
    trials_expiring_within_7_days: int
    users_total: int
    users_active: int
    users_verified: int
    resumes_total: int
    jobs_total: int
    mailboxes_total: int
    ai_runs_total: int
    ai_runs_succeeded: int
    ai_runs_failed: int
    ai_cost_cny_micros: int
    ai_cost_unavailable_runs: int


class PlatformRuntimeWorkerResponse(ApiModel):
    """Content-free health of one recent worker process.

    Worker and host identifiers deliberately stay server-side. The platform
    console needs liveness, not a process-to-customer correlation handle.
    """

    worker_kind: Literal["background", "unknown"]
    liveness: Literal["live", "stale", "stopped"]
    started_at: datetime
    last_seen_at: datetime
    last_cycle_completed_at: datetime | None = None
    last_error_code: str | None = None


class PlatformRuntimeQueueResponse(ApiModel):
    """Aggregate durable-queue state without task or workspace identifiers."""

    queue_key: str
    queued_count: int = 0
    running_count: int = 0
    failed_count: int = 0
    oldest_pending_at: datetime | None = None


class PlatformRuntimeFailureResponse(ApiModel):
    """A normalized operational failure, never a raw exception or task row."""

    queue_key: str
    error_code: str
    occurred_at: datetime
    attempt_count: int | None = None


class PlatformRuntimeOverviewResponse(ApiModel):
    generated_at: datetime
    worker_stale_after_seconds: int
    worker_liveness: Literal["live", "stale", "stopped", "missing"]
    workers: list[PlatformRuntimeWorkerResponse] = Field(default_factory=list)
    queues: list[PlatformRuntimeQueueResponse] = Field(default_factory=list)
    recent_failures: list[PlatformRuntimeFailureResponse] = Field(default_factory=list)


class PlatformOrganizationListItem(ApiModel):
    organization_id: str
    name: str
    plan_id: str | None = None
    plan_code: str | None = None
    plan_name: str | None = None
    plan_status: Literal["trial", "active", "expired", "suspended", "legacy"]
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    member_count: int
    active_member_count: int
    created_at: datetime
    updated_at: datetime


class PlatformOrganizationListResponse(ApiModel):
    items: list[PlatformOrganizationListItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PlatformOrganizationMemberResponse(ApiModel):
    membership_id: str
    user_id: str
    full_name: str
    email: str
    role: Literal["admin", "recruiter"]
    is_active: bool
    user_is_active: bool
    email_verified: bool
    last_login_at: datetime | None = None
    joined_at: datetime


class PlatformOrganizationDetailResponse(PlatformOrganizationListItem):
    resume_count: int
    job_count: int
    mailbox_count: int
    ai_run_count: int
    members: list[PlatformOrganizationMemberResponse] = Field(default_factory=list)


class PlatformOrganizationPatch(ApiModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    plan_code: str | None = Field(default=None, min_length=1, max_length=64)
    plan_status: Literal["trial", "active", "expired", "suspended"] | None = None
    trial_ends_at: datetime | None = None
    confirmation_name: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("name", "plan_code", "confirmation_name", "reason")
    @classmethod
    def strip_platform_organization_patch_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_patch_text_must_not_be_blank")
        return normalized

    @model_validator(mode="after")
    def require_platform_organization_change(self) -> "PlatformOrganizationPatch":
        change_fields = {"name", "plan_code", "plan_status", "trial_ends_at"}
        if not self.model_fields_set.intersection(change_fields):
            raise ValueError("platform_organization_change_required")
        return self


class PlatformUserListItem(ApiModel):
    user_id: str
    full_name: str
    email: str
    is_active: bool
    is_platform_admin: bool
    email_verified: bool
    last_login_at: datetime | None = None
    created_at: datetime
    membership_count: int


class PlatformUserListResponse(ApiModel):
    items: list[PlatformUserListItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PlatformUserMembershipResponse(ApiModel):
    membership_id: str
    organization_id: str
    organization_name: str
    role: Literal["admin", "recruiter"]
    is_active: bool
    joined_at: datetime


class PlatformUserDetailResponse(PlatformUserListItem):
    memberships: list[PlatformUserMembershipResponse] = Field(default_factory=list)


class PlatformUserPatch(ApiModel):
    is_active: bool
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_platform_user_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized


class PlatformAuditEventResponse(ApiModel):
    audit_id: str
    actor_user_id: str
    action: str
    target_type: str
    target_id: str
    organization_id: str | None = None
    reason: str
    before_state: dict[str, object] = Field(default_factory=dict)
    after_state: dict[str, object] = Field(default_factory=dict)
    request_id: str | None = None
    created_at: datetime


class PlatformAuditEventListResponse(ApiModel):
    items: list[PlatformAuditEventResponse] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class AiProviderProfileCreate(ApiModel):
    """A platform-only provider connection profile.

    ``credential_ref`` is deliberately a non-secret environment reference;
    the actual credential is never accepted or returned by the API.
    """

    slug: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    driver: Literal["openai_compatible"]
    endpoint_url: str = Field(min_length=8, max_length=1000)
    credential_ref: str = Field(min_length=1, max_length=120)
    request_defaults: dict[str, object] = Field(default_factory=dict)
    is_enabled: bool = True
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not AI_CONFIG_SLUG_PATTERN.fullmatch(normalized):
            raise ValueError("invalid_ai_config_slug")
        return normalized

    @field_validator("display_name", "credential_ref")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value_must_not_be_blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_provider_audit_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized

    @field_validator("endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        try:
            return validate_external_https_endpoint(
                value,
                field_name="ai_endpoint_url",
            )
        except GatewayContractError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("request_defaults")
    @classmethod
    def validate_safe_request_defaults(cls, value: dict[str, object]) -> dict[str, object]:
        """Reject connection, credential, and protected transport controls.

        Provider-specific non-secret payload defaults (for example a thinking
        toggle) remain configurable.  Authentication, endpoints, model
        selection, headers, and request-controlled fields belong exclusively
        to the server route/adapter and must never be persisted or echoed by
        the platform API.
        """

        forbidden = {
            "apikey",
            "authorization",
            "credential",
            "secret",
            "token",
            "password",
            "model",
            "messages",
            "tools",
            "toolchoice",
            "maxtokens",
            "maxoutputtokens",
            "temperature",
            "stream",
            "url",
            "endpoint",
            "baseurl",
            "headers",
        }

        def walk(item: object) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise ValueError("ai_request_defaults_key_invalid")
                    normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
                    if normalized_key in forbidden:
                        raise ValueError("ai_request_defaults_protected_key")
                    walk(nested)
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)

        walk(value)
        return value


class AiProviderProfileResponse(ApiModel):
    provider_id: str
    slug: str
    display_name: str
    driver: str
    endpoint_url: str
    credential_ref: str
    # This is intentionally only a boolean. Platform operators can see whether
    # the current process has the referenced secret, but never the value or
    # any other reference in the deployment map.
    credential_configured: bool
    request_defaults: dict[str, object] = Field(default_factory=dict)
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class AiModelProfileCreate(ApiModel):
    slug: str = Field(min_length=2, max_length=64)
    provider_slug: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    provider_model_id: str = Field(min_length=1, max_length=255)
    capabilities: list[Literal["chat", "tools", "json_schema"]] = Field(default_factory=lambda: ["chat"])
    context_window_tokens: int | None = Field(default=None, ge=1, le=20_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=2_000_000)
    is_enabled: bool = True
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("slug", "provider_slug")
    @classmethod
    def validate_model_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not AI_CONFIG_SLUG_PATTERN.fullmatch(normalized):
            raise ValueError("invalid_ai_config_slug")
        return normalized

    @field_validator("display_name", "provider_model_id")
    @classmethod
    def normalize_model_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value_must_not_be_blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_model_audit_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized


class AiModelProfileResponse(ApiModel):
    model_id: str
    slug: str
    provider_id: str
    provider_slug: str
    display_name: str
    provider_model_id: str
    capabilities: list[str] = Field(default_factory=list)
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


class AiModelPriceVersionCreate(ApiModel):
    model_slug: str = Field(min_length=2, max_length=64)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    effective_from: datetime
    effective_to: datetime | None = None
    input_per_million: Decimal | None = Field(default=None, ge=0)
    cached_read_input_per_million: Decimal | None = Field(default=None, ge=0)
    cached_write_input_per_million: Decimal | None = Field(default=None, ge=0)
    output_per_million: Decimal | None = Field(default=None, ge=0)
    reasoning_per_million: Decimal | None = Field(default=None, ge=0)
    request_unit_price: Decimal | None = Field(default=None, ge=0)
    page_unit_price: Decimal | None = Field(default=None, ge=0)
    source: str = Field(min_length=1, max_length=120)
    is_active: bool = True
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("model_slug")
    @classmethod
    def validate_price_model_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not AI_CONFIG_SLUG_PATTERN.fullmatch(normalized):
            raise ValueError("invalid_ai_config_slug")
        return normalized

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("invalid_currency")
        return normalized

    @model_validator(mode="after")
    def validate_price_window(self) -> "AiModelPriceVersionCreate":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("price_effective_to_must_follow_effective_from")
        return self

    @field_validator("reason")
    @classmethod
    def normalize_price_audit_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized


class AiModelPriceVersionResponse(ApiModel):
    price_version_id: str
    model_id: str
    model_slug: str
    currency: str
    effective_from: datetime
    effective_to: datetime | None = None
    input_per_million: Decimal | None = None
    cached_read_input_per_million: Decimal | None = None
    cached_write_input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    reasoning_per_million: Decimal | None = None
    request_unit_price: Decimal | None = None
    page_unit_price: Decimal | None = None
    source: str
    is_active: bool
    created_at: datetime


class AiRouteTargetInput(ApiModel):
    model_slug: str = Field(min_length=2, max_length=64)
    max_attempts: int = Field(default=1, ge=1, le=3)
    allow_fallback_on: list[
        Literal[
            "rate_limited",
            "quota_exhausted",
            "timeout",
            "network",
            "provider_5xx",
        ]
    ] = Field(default_factory=list, max_length=5)

    @field_validator("model_slug")
    @classmethod
    def validate_route_model_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not AI_CONFIG_SLUG_PATTERN.fullmatch(normalized):
            raise ValueError("invalid_ai_config_slug")
        return normalized

    @field_validator("allow_fallback_on")
    @classmethod
    def validate_unique_fallback_categories(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate_ai_fallback_category")
        return value


class AiRoutePolicyPublish(ApiModel):
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    targets: list[AiRouteTargetInput] = Field(min_length=1, max_length=4)
    prompt_revision: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("display_name")
    @classmethod
    def normalize_route_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value_must_not_be_blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def normalize_route_audit_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("platform_audit_reason_required")
        return normalized

    @model_validator(mode="after")
    def validate_unique_route_targets(self) -> "AiRoutePolicyPublish":
        slugs = [target.model_slug for target in self.targets]
        if len(slugs) != len(set(slugs)):
            raise ValueError("duplicate_route_model_target")
        return self


class AiRoutePolicyResponse(ApiModel):
    policy_id: str
    feature: str
    display_name: str
    description: str | None = None
    current_version: int | None = None
    is_enabled: bool
    updated_at: datetime


class AiRoutePolicyVersionResponse(ApiModel):
    route_policy_version_id: str
    policy_id: str
    feature: str
    version: int
    targets: list[AiRouteTargetInput]
    prompt_revision: str | None = None
    published_at: datetime
    published_by_user_id: str | None = None


class AiRunUsageSummaryResponse(ApiModel):
    """Safe platform-only view of one AI run.

    It deliberately excludes business references and every candidate/input/
    output field.  The opaque run ID is sufficient to correlate internal
    operational support records without exposing resume data.
    """

    run_id: str
    organization_id: str
    feature: str
    service_kind: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_cost_cny_micros: int | None = None
    cost_status: str
    invocation_count: int
    potentially_billed_invocation_count: int
    token_usage_invocation_count: int
    total_tokens: int


class AiUsageAggregateResponse(ApiModel):
    """Platform-only aggregate by workspace, feature, Provider, and model."""

    organization_id: str
    feature: str
    provider_slug: str
    model_slug: str
    invocation_count: int
    costed_invocation_count: int
    unavailable_cost_invocation_count: int
    potentially_billed_invocation_count: int
    reported_cost_cny_micros: int
    token_usage_invocation_count: int
    input_tokens: int
    cached_read_input_tokens: int
    cached_write_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    known_run_count: int
    partial_run_count: int
    unavailable_run_count: int


class AiUsageTrendBucketResponse(ApiModel):
    """One platform-only Provider/model Token time bucket.

    It intentionally excludes price, cost, business references, candidate
    data, prompts, source documents, and model outputs.  ``bucket_started_at``
    is a UTC instant corresponding to the beginning of the requested IANA
    ``time_zone`` civil bucket, never the database server's local timezone.
    """

    bucket_started_at: datetime
    time_zone: str
    provider_slug: str
    model_slug: str
    invocation_count: int
    token_usage_invocation_count: int
    input_tokens: int
    cached_read_input_tokens: int
    cached_write_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int


class MailboxConfigCreate(ApiModel):
    """Create one independently named ingestion source.

    New product clients select a reviewed ``provider_key``.  The explicit
    ``generic_imap`` provider additionally accepts a domain-only IMAPS host;
    it is still fixed to TLS port 993 and receives the transport's public DNS
    and certificate checks.  Raw fields for all other clients remain a
    compatibility path with the deployment allowlist.
    """

    display_name: str = Field(min_length=1, max_length=32)
    provider_key: str | None = Field(default=None, min_length=1, max_length=64)
    imap_host: str | None = Field(default=None, min_length=1, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    email_address: str = Field(min_length=3, max_length=320)
    # Deprecated compatibility input. New product clients omit this field; all
    # newly created channels are constrained to INBOX by the service layer.
    mailbox: str = Field(default="INBOX", min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool = True
    # Only accepted while a new source is being bound. The service records an
    # immutable date at bind time; later PATCH calls deliberately cannot
    # retroactively scan mail that pre-dated a connection.
    initial_sync_lookback_days: int = Field(default=0, ge=0, le=365)


class MailboxConfigPatch(ApiModel):
    """Update a named IMAP source without ever returning its password."""

    display_name: str | None = Field(default=None, min_length=1, max_length=32)
    provider_key: str | None = Field(default=None, min_length=1, max_length=64)
    imap_host: str | None = Field(default=None, min_length=1, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    email_address: str | None = Field(default=None, min_length=3, max_length=320)
    # Deprecated compatibility input. Existing historical channels may only
    # retain their current source mailbox; no arbitrary folder switch is allowed.
    mailbox: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool | None = None


class MailboxConfigUpdate(ApiModel):
    """Compatibility payload for the former single-mailbox endpoint.

    The plural endpoints use ``MailboxConfigCreate`` and ``MailboxConfigPatch``.
    Keeping this shape avoids silently changing one-channel clients during the
    transition.
    """

    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    email_address: str = Field(min_length=3, max_length=320)
    # Deprecated compatibility input for the former single-mailbox endpoint.
    mailbox: str = Field(default="INBOX", min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool = True


class MailboxSyncAlertSummary(ApiModel):
    """A safe, workspace-local summary of an open mailbox sync incident."""

    severity: Literal["warning", "critical"]
    consecutive_failures: int
    opened_at: datetime
    last_failed_at: datetime
    last_error_code: str


class MailboxConfigResponse(ApiModel):
    configured: bool
    mailbox_id: str | None = None
    display_name: str | None = None
    provider_key: str | None = None
    provider_display_name: str | None = None
    authentication_mode: Literal["app_password", "oauth2"] | None = None
    authorization_status: Literal[
        "not_connected", "connected", "reauthorization_required", "unavailable"
    ] | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    email_address: str | None = None
    mailbox: str | None = None
    enabled: bool = False
    archived_at: datetime | None = None
    password_configured: bool = False
    # Deliberately expose the binding time, but not the IMAP UID internals.
    import_started_at: datetime | None = None
    initial_sync_lookback_days: int = 0
    initial_backfill_since_date: date | None = None
    initial_backfill_completed_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    active_sync_alert: MailboxSyncAlertSummary | None = None


class MailboxProviderResponse(ApiModel):
    """A reviewed provider presentation record, never a credential record."""

    provider_key: str
    display_name: str
    authentication_mode: Literal["app_password", "oauth2"]
    available: bool
    # Fixed providers return their reviewed endpoint.  The generic IMAP
    # provider intentionally returns ``null`` and asks for a domain at bind
    # time instead.
    imap_host: str | None = None
    imap_port: int
    allows_custom_endpoint: bool = False
    default_mailbox: str
    credential_label: str
    help_text: str


class MailboxProviderListResponse(ApiModel):
    items: list[MailboxProviderResponse]


class MailboxOAuthStartRequest(ApiModel):
    """Begin an OAuth mailbox connection without exposing a token to the UI."""

    provider_key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=32)
    email_address: str = Field(min_length=3, max_length=320)
    # Deprecated compatibility input. New OAuth starts are fixed to INBOX.
    mailbox: str = Field(default="INBOX", min_length=1, max_length=255)
    initial_sync_lookback_days: int = Field(default=0, ge=0, le=365)


class MailboxOAuthStartResponse(ApiModel):
    authorization_url: str


class MailboxConfigListResponse(ApiModel):
    items: list[MailboxConfigResponse]
    total: int


class MailboxSyncResponse(ApiModel):
    configured: bool
    mailbox_id: str | None = None
    display_name: str | None = None
    imported_count: int = 0
    duplicate_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class MailboxBackgroundJobResponse(ApiModel):
    """Safe, pollable state for one durable IMAP operation."""

    job_id: str
    mailbox_id: str
    job_kind: Literal["sync", "attachment_retry"]
    trigger_type: Literal["manual", "scheduled"]
    status: Literal["queued", "running", "completed", "failed"]
    import_id: str | None = None
    attempt_count: int = 0
    max_attempts: int = 1
    imported_count: int = 0
    duplicate_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    last_error: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deduplicated: bool = False


class MailboxBackgroundJobHistoryResponse(ApiModel):
    items: list[MailboxBackgroundJobResponse]
    total: int


class MailboxBackgroundJobBatchResponse(ApiModel):
    """The independent tasks created by a single "sync all" request."""

    items: list[MailboxBackgroundJobResponse]
    queued_count: int = 0
    deduplicated_count: int = 0


class MailboxImportResponse(ApiModel):
    import_id: str
    mailbox_config_id: str
    mailbox_display_name: str | None = None
    attachment_filename: str
    status: str
    error: str | None = None
    resume_id: str | None = None
    attempt_count: int = 1
    last_attempted_at: datetime | None = None
    can_retry: bool = False
    created_at: datetime


class MailboxImportHistoryResponse(ApiModel):
    items: list[MailboxImportResponse]
    total: int


class MailboxRetentionPolicyUpdate(ApiModel):
    retention_policy: Literal["minimal", "standard", "audit"]


class MailboxRetentionSummaryResponse(ApiModel):
    configured: bool
    retention_policy: Literal["minimal", "standard", "audit"] = "standard"
    body_copy_count: int = 0
    attachment_copy_count: int = 0
    failure_artifact_count: int = 0
    cache_bytes: int = 0
    expired_body_count: int = 0
    expired_attachment_copy_count: int = 0
    expired_failure_artifact_count: int = 0
    expired_bytes: int = 0
    earliest_expires_at: datetime | None = None
    last_cleanup_at: datetime | None = None
    next_cleanup_at: datetime | None = None


class MailboxRetentionPreviewResponse(MailboxRetentionSummaryResponse):
    skipped_count: int = 0


class MailboxRetentionCleanupRunResponse(ApiModel):
    run_id: str
    trigger_type: Literal["manual", "scheduled"]
    status: str
    retention_policy: Literal["minimal", "standard", "audit"]
    started_at: datetime
    finished_at: datetime | None = None
    scanned_count: int = 0
    deleted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    reclaimed_bytes: int = 0
    next_cleanup_at: datetime | None = None
    error_code: str | None = None


class MailboxRetentionCleanupRunHistoryResponse(ApiModel):
    items: list[MailboxRetentionCleanupRunResponse]
    total: int


class CandidateDataFileAccessRequest(ApiModel):
    """An explicit user intent, so preview and download audit separately."""

    purpose: Literal["view", "download"]


class CandidateDataFileAccessResponse(ApiModel):
    access_url: str
    expires_at: datetime


CandidateDataDeletionReason = Literal[
    "candidate_request",
    "recruitment_closed",
    "duplicate",
    "retention_expired",
    "other",
]


class CandidateDataDeletionRequest(ApiModel):
    reason: CandidateDataDeletionReason
    other_note: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_other_note(self) -> "CandidateDataDeletionRequest":
        if self.reason == "other" and not self.other_note:
            raise ValueError("other_note is required when reason is other")
        if self.reason != "other" and self.other_note is not None:
            raise ValueError("other_note is only allowed when reason is other")
        return self


class CandidateDataDeletionResponse(ApiModel):
    deletion_batch_id: str
    recovery_deadline_at: datetime
    purge_after_at: datetime
    affected_candidate_count: int
    affected_resume_count: int


class CandidateDataRestoreResponse(ApiModel):
    deletion_batch_id: str
    restored_candidate_count: int
    restored_resume_count: int
    restored_at: datetime


class CandidateDataDeletionBatchResponse(ApiModel):
    """Metadata-only recovery item; it never exposes candidate content."""

    deletion_batch_id: str
    trigger_type: str
    reason: CandidateDataDeletionReason
    status: str
    recovery_deadline_at: datetime
    purge_after_at: datetime
    affected_candidate_count: int
    affected_resume_count: int
    restorable: bool
    restored_at: datetime | None = None
    purged_at: datetime | None = None


class CandidateDataDeletionBatchListResponse(ApiModel):
    items: list[CandidateDataDeletionBatchResponse]
    total: int


class CandidateDataRetentionPolicyUpdate(ApiModel):
    mode: Literal["manual", "automatic"]
    retention_days: int | None = Field(default=None, ge=30, le=3650)
    preview_token: str | None = Field(default=None, min_length=32, max_length=512)

    @model_validator(mode="after")
    def validate_retention_fields(self) -> "CandidateDataRetentionPolicyUpdate":
        if self.mode == "automatic" and self.retention_days is None:
            raise ValueError("retention_days is required for automatic mode")
        if self.mode == "manual" and self.retention_days is not None:
            raise ValueError("retention_days must be omitted for manual mode")
        if self.mode == "automatic" and not self.preview_token:
            raise ValueError("preview_token is required for automatic mode")
        return self


class CandidateDataRetentionPolicyResponse(ApiModel):
    mode: Literal["manual", "automatic"]
    retention_days: int | None = None
    version: int
    updated_at: datetime


class CandidateDataRetentionPreviewRequest(ApiModel):
    retention_days: int = Field(ge=30, le=3650)


class CandidateDataRetentionPreviewResponse(ApiModel):
    preview_token: str
    policy_version: int
    retention_days: int
    eligible_candidate_count: int
    eligible_resume_count: int
    held_candidate_count: int
    already_deleted_count: int
    calculated_at: datetime


class CandidateDataRetentionCleanupRunResponse(ApiModel):
    run_id: str
    trigger_type: Literal["manual", "scheduled"]
    status: str
    policy_version: int
    retention_days: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    scanned_count: int = 0
    queued_count: int = 0
    skipped_hold_count: int = 0
    failed_count: int = 0
    error_code: str | None = None


class CandidateDataRetentionCleanupRunHistoryResponse(ApiModel):
    items: list[CandidateDataRetentionCleanupRunResponse]
    total: int


class CandidateDataRetentionHoldUpdate(ApiModel):
    retention_hold: bool


class CandidateDataExportCreate(ApiModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=1000)
    include_originals: bool = False

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidate_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("candidate_ids must be non-empty and unique")
        return normalized


class CandidateDataExportResponse(ApiModel):
    export_id: str
    status: str
    item_count: int
    include_originals: bool
    requested_at: datetime
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    error_code: str | None = None


class CandidateDataExportListResponse(ApiModel):
    items: list[CandidateDataExportResponse]
    total: int


class CandidateDataAuditEventResponse(ApiModel):
    event_id: str
    actor_user_id: str | None = None
    actor_kind: str
    action: str
    target_type: str
    target_id: str
    result: str
    reason_code: str | None = None
    created_at: datetime


class CandidateDataAuditEventListResponse(ApiModel):
    items: list[CandidateDataAuditEventResponse]
    total: int


class RecruitingAgentContextReference(ApiModel):
    """A browser-selectable server-owned context source.

    The server validates every reference under the authenticated workspace
    before it changes a private Agent conversation.  Resume IDs, candidate
    IDs, source text, and arbitrary conversation history are intentionally
    not accepted.
    """

    kind: Literal["talent_search_run", "talent_search_profile"]
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    revision_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_source(self) -> "RecruitingAgentContextReference":
        if self.kind == "talent_search_run":
            if self.run_id and self.profile_id is None and self.revision_id is None:
                return self
        elif (
            self.profile_id
            and self.revision_id
            and self.run_id is None
        ):
            return self
        raise ValueError("invalid_recruiting_agent_context_reference")


class RecruitingAgentActiveTalentProfile(ApiModel):
    """Safe summary of the profile currently attached to a private chat."""

    profile_id: str
    revision_id: str
    revision_number: int = Field(ge=1)
    title: str
    status: Literal["draft", "confirmed"]


class RecruitingAgentActiveContext(ApiModel):
    """Safe, recruiter-visible status for the current work-session scope."""

    candidate_set_source: Literal[
        "agent_search",
        "candidate_filter",
        "talent_search_run",
    ] | None = None
    candidate_count: int = Field(default=0, ge=0)
    active_job_version_id: str | None = None
    active_job_title: str | None = None
    active_talent_profile: RecruitingAgentActiveTalentProfile | None = None
    expires_at: datetime


class RecruitingAgentConversationTurnResponse(ApiModel):
    """One completed, short-lived recruiter-visible Agent exchange.

    This intentionally omits internal prompts, tool calls, candidate cards,
    source evidence, and provider payloads.  The parent conversation enforces
    the tenant, owner, TTL, and bounded-history policy.
    """

    context_version: int = Field(ge=1)
    user_message: str
    assistant_message: str
    created_at: datetime


class RecruitingAgentConversationResponse(ApiModel):
    conversation_id: str
    context_version: int = Field(ge=1)
    active_context: RecruitingAgentActiveContext
    chat_history: list[RecruitingAgentConversationTurnResponse] = Field(
        default_factory=list
    )


class RecruitingAgentContextBindRequest(ApiModel):
    """Bind a server-owned talent-search run without spending an AI turn.

    This keeps the browser interaction explicit: selecting a result scope in
    the recruiting UI updates only the private work session.  It cannot carry
    candidate IDs, chat history, source text, or arbitrary model instructions.
    """

    context_ref: RecruitingAgentContextReference
    # Unlike a normal turn, this action always applies the visible JD choice:
    # null deliberately clears a previously selected JD from the work scope.
    job_version_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    context_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_version_for_existing_conversation(
        self,
    ) -> "RecruitingAgentContextBindRequest":
        if self.conversation_id is not None and self.context_version is None:
            raise ValueError("context_version_required_for_conversation")
        if self.conversation_id is None and self.context_version is not None:
            raise ValueError("context_version_requires_conversation")
        return self


class RecruitingAgentFilterScopeRequest(ApiModel):
    """Freeze the current server-derived basic-filter result for one Agent chat.

    The browser may describe factual filter conditions only.  It cannot submit
    candidate IDs, resume IDs, resume text, or a previously rendered page of
    results.  The service deliberately ignores the request pagination and
    reconstructs the complete tenant-scoped result before it stores opaque
    resume references.
    """

    filter: "CandidateSearchRequest"
    job_version_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    context_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_version_for_existing_conversation(
        self,
    ) -> "RecruitingAgentFilterScopeRequest":
        if self.conversation_id is not None and self.context_version is None:
            raise ValueError("context_version_required_for_conversation")
        if self.conversation_id is None and self.context_version is not None:
            raise ValueError("context_version_requires_conversation")
        return self


class RecruitingAgentRequest(ApiModel):
    """One bounded recruiting-assistant turn.

    The browser supplies the user's request and, optionally, the current JD.
    The assistant works across the authenticated workspace; it never receives
    a candidate selection, a PDF, or unrestricted database access from the
    client.
    """

    message: str = Field(min_length=1, max_length=2000)
    job_version_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)
    context_version: int | None = Field(default=None, ge=1)
    context_ref: RecruitingAgentContextReference | None = None

    @model_validator(mode="after")
    def require_version_for_existing_conversation(self) -> "RecruitingAgentRequest":
        """Make stale-tab protection mandatory after a session has been created."""

        if self.conversation_id is not None and self.context_version is None:
            raise ValueError("context_version_required_for_conversation")
        if self.conversation_id is None and self.context_version is not None:
            raise ValueError("context_version_requires_conversation")
        return self


class RecruitingAgentCandidate(ApiModel):
    candidate_id: str
    resume_id: str
    display_name: str | None
    detail: str
    score: float | None = None
    # ``confirmed`` means the resume itself contains a source-grounded
    # statement for the requested condition.  It is not third-party
    # credential verification and it is never inferred from a missing value.
    verification_status: Literal["confirmed", "unconfirmed"] | None = None
    verification_evidence: list["RecruitingAgentVerificationEvidence"] = Field(
        default_factory=list
    )


class RecruitingAgentVerificationEvidence(ApiModel):
    """One compact, safe-to-display basis for an Agent search result.

    Source block identifiers remain server-side provenance.  The Agent and
    browser receive only the matched recruiter-facing label and whether it
    came from an extracted fact or a direct resume-text mention.
    """

    label: str
    source: Literal["structured_fact", "resume_text"]


class RecruitingAgentSearchSummary(ApiModel):
    """Deterministic recruiter-facing status for one candidate search.

    A language condition is special because a missing extracted credential
    must never be presented as a failed credential.  ``unconfirmed_count``
    is therefore scoped to candidates that match every other requested
    condition, but have no clear resume evidence for the language condition.
    """

    confirmed_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    unconfirmed_count: int | None = Field(default=None, ge=0)
    confirmation_basis: str | None = None


class RecruitingAgentAction(ApiModel):
    action: Literal[
        "open_resume",
        "open_match_workspace",
        "open_score_workspace",
        "open_mailbox_workspace",
    ]
    label: str
    resume_id: str | None = None


class RecruitingAgentToolTrace(ApiModel):
    tool: str
    summary: str


class RecruitingAgentResponse(ApiModel):
    conversation_id: str
    context_version: int = Field(ge=1)
    active_context: RecruitingAgentActiveContext
    chat_history: list[RecruitingAgentConversationTurnResponse] = Field(
        default_factory=list
    )
    message: str
    intent: Literal[
        "draft_talent_search_profile",
        "refine_active_talent_search_profile",
        "search_candidates",
        "read_resume_content",
        "run_job_matching",
        "run_workspace_scoring",
        "show_job_ranking",
        "show_mailbox_status",
        "show_mailbox_imports",
        "sync_mailbox",
        "help",
    ]
    job_version_id: str | None = None
    candidates: list[RecruitingAgentCandidate] = Field(default_factory=list)
    actions: list[RecruitingAgentAction] = Field(default_factory=list)
    tool_trace: list[RecruitingAgentToolTrace] = Field(default_factory=list)
    search_summary: RecruitingAgentSearchSummary | None = None
    batch_id: str | None = None
    # The profile API already returns this same recruiter-safe shape.  The
    # explicit forward reference keeps the generic Agent contract close to its
    # conversation models while the reusable profile schemas remain together.
    talent_profile: "TalentSearchProfileResponse | None" = None


class CandidateCreate(ApiModel):
    display_name: str | None = Field(default=None, max_length=200)


class CandidateCreated(ApiModel):
    candidate_id: str


class ResumeUploadResponse(ApiModel):
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    extraction_status: str
    ai_extraction_status: str
    ai_extraction_error: str | None
    # A separate, source-grounded task runs only when structured fact
    # extraction could not safely supply the candidate's name. It is
    # deliberately informational: its failure never changes resume readiness.
    candidate_name_extraction_status: str | None = None
    candidate_name_extraction_error: str | None = None
    ai_summary_status: str | None = None
    ai_summary_error: str | None = None
    source_page_count: int
    parsed_page_count: int
    quality_flags: list[str]


class ResumeReviewQueueItem(ApiModel):
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    original_filename: str
    extraction_status: str
    ai_extraction_status: str
    ai_extraction_error: str | None
    candidate_name_extraction_status: str | None = None
    candidate_name_extraction_error: str | None = None
    ai_summary_status: str | None = None
    ai_summary_error: str | None = None
    quality_flags: list[str]
    created_at: datetime


class ResumeReviewQueueResponse(ApiModel):
    items: list[ResumeReviewQueueItem]
    total: int
    page: int
    page_size: int


class ResumeDetail(ApiModel):
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    extraction_status: str
    ai_extraction_status: str
    ai_extraction_error: str | None
    candidate_name_extraction_status: str | None = None
    candidate_name_extraction_error: str | None = None
    ai_summary_status: str | None = None
    ai_summary_error: str | None = None
    is_active: bool
    retention_hold: bool
    is_985_211: bool | None
    highest_degree: DegreeLevel | None
    employment_months: int
    employment_or_internship_months: int
    source_page_count: int
    parsed_page_count: int
    quality_flags: list[str]


class ResumeSourceBlockResponse(ApiModel):
    block_id: str
    page_no: int
    block_type: str
    text: str


class ResumeEducationResponse(ApiModel):
    school_name_raw: str
    school_match_state: str
    degree: DegreeLevel
    major_raw: str | None
    start_month: Month | None
    end_month: Month | None
    institution_tiers: list[InstitutionTier]
    institution_classification: InstitutionClassification | None = None
    classification_basis: str | None = None
    classification_registry_version: str | None = None
    classification_evidence_block_ids: list[str] = Field(default_factory=list)
    average_score: float | None
    gpa_value: float | None
    gpa_scale: float | None
    gpa_percent: float | None
    rank_position: int | None
    rank_total: int | None
    rank_percent: float | None
    evidence_block_ids: list[str]


class ResumeExperienceDetailResponse(ApiModel):
    detail_raw: str
    evidence_block_ids: list[str]


class ResumeExperienceResponse(ApiModel):
    experience_type: ExperienceType
    experience_name_raw: str | None
    organization_name_raw: str | None
    title_raw: str | None
    start_month: Month | None
    end_month: Month | None
    is_current: bool
    evidence_block_ids: list[str]
    classification_evidence_block_ids: list[str]
    detail_items: list[ResumeExperienceDetailResponse]
    leadership_context: str | None
    leadership_role: str | None
    award_level: str | None
    award_result_raw: str | None


class ResumeSkillResponse(ApiModel):
    skill_display: str
    skill_category: SkillCategory | None
    evidence_block_ids: list[str]


class ResumeLanguageCredentialResponse(ApiModel):
    credential_code: LanguageCredentialCode
    credential_name_raw: str
    score: float | None
    passed: bool | None
    evidence_block_ids: list[str]


class ResumeScholarshipResponse(ApiModel):
    scholarship_name_raw: str
    scholarship_level: str | None
    evidence_block_ids: list[str]


class ResumeReviewActionResponse(ApiModel):
    action: str
    actor: str
    note: str | None
    created_at: str


ResumeContactKind = Literal["email", "phone"]


class ResumeContactResponse(ApiModel):
    kind: ResumeContactKind
    value: str
    evidence_block_ids: list[str] = Field(default_factory=list)


class ResumeReviewDetail(ResumeDetail):
    original_filename: str
    facts_version: int
    contacts: list[ResumeContactResponse] = Field(default_factory=list)
    source_blocks: list[ResumeSourceBlockResponse]
    education: list[ResumeEducationResponse]
    experiences: list[ResumeExperienceResponse]
    skills: list[ResumeSkillResponse]
    language_credentials: list[ResumeLanguageCredentialResponse]
    scholarships: list[ResumeScholarshipResponse]
    review_actions: list[ResumeReviewActionResponse]


class EducationFact(ApiModel):
    school_name_raw: str = Field(min_length=1, max_length=255)
    degree: DegreeLevel = "unknown"
    # Produced by the AI rulebook only. Manual save paths may omit both fields
    # and the backend still performs its own local-registry calculation.
    ai_985_211_judgment: bool = False
    ai_institution_roster_id: str | None = Field(default=None, max_length=64)
    major_raw: str | None = Field(default=None, max_length=255)
    start_month: Month | None = None
    end_month: Month | None = None
    institution_tiers: list[InstitutionTier] = Field(default_factory=list, max_length=10)
    average_score: float | None = Field(default=None, ge=0, le=100)
    gpa_value: float | None = Field(default=None, ge=0, le=100)
    gpa_scale: float | None = Field(default=None, gt=0, le=100)
    rank_position: int | None = Field(default=None, ge=1, le=1_000_000)
    rank_total: int | None = Field(default=None, ge=1, le=1_000_000)
    evidence_block_ids: list[str] = Field(min_length=1, max_length=8)

    @field_validator("start_month", "end_month")
    @classmethod
    def valid_month(cls, value: Month | None) -> Month | None:
        if value is not None and not MONTH_PATTERN.fullmatch(value):
            raise ValueError("month must use YYYY-MM")
        return value

    @field_validator("evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)

    @model_validator(mode="after")
    def valid_date_range(self) -> "EducationFact":
        if self.start_month and self.end_month and self.end_month < self.start_month:
            raise ValueError("end_month must not be earlier than start_month")
        if self.gpa_value is not None and self.gpa_scale is None:
            raise ValueError("gpa_scale is required when gpa_value is provided")
        if self.gpa_value is None and self.gpa_scale is not None:
            raise ValueError("gpa_value is required when gpa_scale is provided")
        if self.gpa_value is not None and self.gpa_scale is not None and self.gpa_value > self.gpa_scale:
            raise ValueError("gpa_value must not exceed gpa_scale")
        if self.rank_position is not None and self.rank_total is None:
            raise ValueError("rank_total is required when rank_position is provided")
        if self.rank_position is None and self.rank_total is not None:
            raise ValueError("rank_position is required when rank_total is provided")
        if self.rank_position is not None and self.rank_total is not None and self.rank_position > self.rank_total:
            raise ValueError("rank_position must not exceed rank_total")
        return self


class ExperienceDetailItem(ApiModel):
    """One verbatim, source-cited responsibility or contribution."""

    detail_raw: str = Field(min_length=1, max_length=800)
    evidence_block_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("detail_raw")
    @classmethod
    def valid_detail_raw(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not normalized_key(normalized):
            raise ValueError("detail_raw must contain searchable characters")
        return normalized

    @field_validator("evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class ExperienceFact(ApiModel):
    experience_type: ExperienceType
    experience_name_raw: str | None = Field(default=None, max_length=255)
    organization_name_raw: str | None = Field(default=None, max_length=255)
    title_raw: str | None = Field(default=None, max_length=255)
    start_month: Month | None = None
    end_month: Month | None = None
    is_current: bool = False
    evidence_block_ids: list[str] = Field(min_length=1, max_length=8)
    classification_evidence_block_ids: list[str] = Field(default_factory=list, max_length=8)
    detail_items: list[ExperienceDetailItem] = Field(default_factory=list, max_length=12)
    leadership_context: Literal["class", "student_org", "club", "project_team", "company"] | None = None
    leadership_role: str | None = Field(default=None, max_length=64)
    award_level: Literal["national", "provincial", "school", "department", "other"] | None = None
    award_result_raw: str | None = Field(default=None, max_length=255)

    @field_validator("start_month", "end_month")
    @classmethod
    def valid_month(cls, value: Month | None) -> Month | None:
        if value is not None and not MONTH_PATTERN.fullmatch(value):
            raise ValueError("month must use YYYY-MM")
        return value

    @field_validator("evidence_block_ids", "classification_evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)

    @model_validator(mode="after")
    def enforce_work_context(self) -> "ExperienceFact":
        if self.experience_type in {"employment", "internship"}:
            if not self.organization_name_raw or not self.title_raw:
                raise ValueError(
                    "employment and internship require organization_name_raw and title_raw"
                )
            if not self.classification_evidence_block_ids:
                raise ValueError(
                    "employment and internship require classification_evidence_block_ids"
                )
        if self.start_month and self.end_month and self.end_month < self.start_month:
            raise ValueError("end_month must not be earlier than start_month")
        if self.is_current and self.end_month:
            raise ValueError("current experience must not have end_month")
        if bool(self.leadership_context) != bool(self.leadership_role):
            raise ValueError("leadership context and source-grounded role must be provided together")
        if self.award_level is not None and not self.award_result_raw:
            raise ValueError("award level requires a source-grounded award result")
        return self


class SkillFact(ApiModel):
    skill_display: str = Field(min_length=1, max_length=120)
    skill_category: SkillCategory | None = None
    evidence_block_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class LanguageCredentialFact(ApiModel):
    credential_code: LanguageCredentialCode
    credential_name_raw: str = Field(min_length=1, max_length=120)
    score: float | None = Field(default=None, ge=0, le=1000)
    passed: bool | None = None
    evidence_block_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class ScholarshipFact(ApiModel):
    scholarship_name_raw: str = Field(min_length=1, max_length=255)
    scholarship_level: Literal[
        "national", "provincial", "school", "department", "enterprise", "other"
    ] | None = None
    evidence_block_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("evidence_block_ids")
    @classmethod
    def valid_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class ResumeFactsSubmission(ApiModel):
    schema_version: Literal["resume_facts.v1", "resume_facts.v2"] = "resume_facts.v2"
    # Identity is used only to name the candidate record after AI extraction.
    # It is intentionally excluded from the immutable fact snapshot consumed
    # by summaries, scoring, and JD matching.
    candidate_name_raw: str | None = Field(default=None, max_length=80)
    candidate_name_evidence_block_ids: list[str] = Field(
        default_factory=list,
        max_length=2,
    )
    education: list[EducationFact] = Field(default_factory=list, max_length=8)
    experiences: list[ExperienceFact] = Field(default_factory=list, max_length=20)
    skills: list[SkillFact] = Field(default_factory=list, max_length=50)
    language_credentials: list[LanguageCredentialFact] = Field(default_factory=list, max_length=12)
    scholarships: list[ScholarshipFact] = Field(default_factory=list, max_length=20)

    @field_validator("candidate_name_raw")
    @classmethod
    def valid_candidate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if CANDIDATE_NAME_LABEL_PATTERN.search(cleaned):
            raise ValueError("candidate_name_raw_must_not_include_label")
        if CANDIDATE_NAME_UNSAFE_CHARACTER_PATTERN.search(cleaned):
            raise ValueError("candidate_name_raw_contains_unsafe_character")
        return cleaned

    @field_validator("candidate_name_evidence_block_ids")
    @classmethod
    def valid_candidate_name_evidence_block_ids(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)

    @model_validator(mode="after")
    def has_at_least_one_fact(self) -> "ResumeFactsSubmission":
        if self.candidate_name_raw and not self.candidate_name_evidence_block_ids:
            raise ValueError("candidate_name_requires_evidence_block_id")
        if not self.candidate_name_raw and self.candidate_name_evidence_block_ids:
            raise ValueError("candidate_name_evidence_requires_candidate_name")
        if not (
            self.education
            or self.experiences
            or self.skills
            or self.language_credentials
            or self.scholarships
        ):
            raise ValueError("at least one structured fact is required")
        return self


class ResumeFactsSaveRequest(ApiModel):
    facts: ResumeFactsSubmission
    complete_review: bool = False
    review_note: str | None = Field(default=None, max_length=1000)
    # A manual decision is only allowed while completing review.  Automatic
    # extraction must never turn an unresolved school name into false.
    is_985_211_override: bool | None = None

    @model_validator(mode="after")
    def valid_manual_override(self) -> "ResumeFactsSaveRequest":
        if self.is_985_211_override is not None and not self.complete_review:
            raise ValueError("is_985_211_override_requires_complete_review")
        return self


class ResumeActivateRequest(ApiModel):
    note: str | None = Field(default=None, max_length=1000)


class EducationFilter(ApiModel):
    degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=5)
    school_name_contains: list[str] = Field(default_factory=list, max_length=8)
    major_contains: list[str] = Field(default_factory=list, max_length=8)
    institution_classifications_any_of: list[InstitutionClassification] = Field(
        default_factory=list,
        max_length=6,
    )
    institution_tiers_any_of: list[InstitutionTier] = Field(default_factory=list, max_length=10)
    # A recruiter-facing convenience threshold.  A record satisfies it when
    # either its explicit percentage average or its normalized GPA percentage
    # reaches the selected value.  The more specific fields below remain
    # available for API and Agent callers that intentionally require both.
    min_academic_score_percent: float | None = Field(default=None, gt=0, le=100)
    min_average_score: float | None = Field(default=None, ge=0, le=100)
    min_gpa_percent: float | None = Field(default=None, ge=0, le=100)
    max_rank_position: int | None = Field(default=None, ge=1, le=1_000_000)
    max_rank_percent: float | None = Field(default=None, gt=0, le=100)

    @field_validator("school_name_contains", "major_contains")
    @classmethod
    def valid_text_filters(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class ExperienceFilter(ApiModel):
    experience_types: list[ExperienceType] = Field(
        default_factory=lambda: ["employment", "internship"],
        max_length=12,
    )
    experience_name_contains: list[str] = Field(default_factory=list, max_length=8)
    organization_name_contains: list[str] = Field(default_factory=list, max_length=8)
    title_contains: list[str] = Field(default_factory=list, max_length=8)
    leadership_contexts_any_of: list[
        Literal["class", "student_org", "club", "project_team", "company"]
    ] = Field(default_factory=list, max_length=5)
    leadership_roles_any_of: list[str] = Field(default_factory=list, max_length=12)
    award_levels_any_of: list[
        Literal["national", "provincial", "school", "department", "other"]
    ] = Field(default_factory=list, max_length=5)
    award_result_contains: list[str] = Field(default_factory=list, max_length=8)

    @field_validator(
        "experience_name_contains",
        "organization_name_contains",
        "title_contains",
        "leadership_roles_any_of",
        "award_result_contains",
    )
    @classmethod
    def valid_text_filters(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class LanguageCredentialFilter(ApiModel):
    credential_code: LanguageCredentialCode
    custom_name_contains: str | None = Field(default=None, max_length=120)
    min_score: float | None = Field(default=None, ge=0, le=1000)

    @model_validator(mode="after")
    def custom_name_is_scoped(self) -> "LanguageCredentialFilter":
        if self.credential_code == "custom" and not self.custom_name_contains:
            raise ValueError("custom language credential requires a name")
        if self.credential_code != "custom" and self.custom_name_contains:
            raise ValueError("custom language credential name is only valid for custom")
        return self


class LeadershipFilter(ApiModel):
    contexts_any_of: list[
        Literal["class", "student_org", "club", "project_team", "company"]
    ] = Field(default_factory=list, max_length=5)
    roles_any_of: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("roles_any_of")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class CandidateSearchRequest(ApiModel):
    schema_version: Literal["candidate_filter.v2"] = "candidate_filter.v2"
    is_985_211: bool | None = None
    # Matches any one education record.  This is deliberately different from
    # ``highest_degree_in``: a recruiter asking for someone who has completed
    # a bachelor degree should still see a candidate whose highest degree is a
    # master or doctor degree.
    education_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    highest_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    graduation_status: Literal["any", "fresh", "previous"] = "any"
    fresh_graduate_start_month: Month | None = None
    fresh_graduate_end_month: Month | None = None
    min_employment_months: int | None = Field(default=None, ge=0, le=720)
    min_employment_or_internship_months: int | None = Field(default=None, ge=0, le=720)
    # Unlike the older compound experience filter, these top-level types are
    # a recruiter-selected checklist. Every selected type must exist somewhere
    # in the candidate's history, so “正式工作 + 实习” is genuinely both.
    experience_types_all_of: list[ExperienceType] = Field(default_factory=list, max_length=12)
    education_any_of: list[EducationFilter] = Field(default_factory=list, max_length=10)
    experience_any_of: list[ExperienceFilter] = Field(default_factory=list, max_length=10)
    skill_categories_any_of: list[SkillCategory] = Field(default_factory=list, max_length=10)
    skills_all_of: list[str] = Field(default_factory=list, max_length=20)
    skills_any_of: list[str] = Field(default_factory=list, max_length=20)
    language_credentials_any_of: list[LanguageCredentialFilter] = Field(default_factory=list, max_length=12)
    language_credentials_all_of: list[LanguageCredentialFilter] = Field(default_factory=list, max_length=12)
    scholarship_status: PresenceStatus = "any"
    scholarship_levels_any_of: list[
        Literal["national", "provincial", "school", "department", "enterprise", "other"]
    ] = Field(default_factory=list, max_length=6)
    scholarship_name_contains: list[str] = Field(default_factory=list, max_length=8)
    competition_status: PresenceStatus = "any"
    competition_award_status: PresenceStatus = "any"
    leadership_any_of: list[LeadershipFilter] = Field(default_factory=list, max_length=5)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    keyword_match_mode: Literal["broad", "precise"] = "broad"
    keywords_all_of: list[str] = Field(default_factory=list, max_length=10)
    keywords_any_of: list[str] = Field(default_factory=list, max_length=10)
    # A score is only comparable with scores generated by the same current
    # template version.  This selector controls recruiter-table ordering; it
    # is deliberately separate from the factual screening conditions above.
    score_template_id: str | None = Field(default=None, min_length=1, max_length=36)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator(
        "skills_all_of",
        "skills_any_of",
        "keywords_all_of",
        "keywords_any_of",
        "keywords",
        "scholarship_name_contains",
    )
    @classmethod
    def valid_skill_filters(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)

    @field_validator("fresh_graduate_start_month", "fresh_graduate_end_month")
    @classmethod
    def valid_filter_month(cls, value: Month | None) -> Month | None:
        if value is not None and not MONTH_PATTERN.fullmatch(value):
            raise ValueError("month must use YYYY-MM")
        return value

    @model_validator(mode="after")
    def valid_v2_semantics(self) -> "CandidateSearchRequest":
        if self.graduation_status != "any":
            if not self.fresh_graduate_start_month or not self.fresh_graduate_end_month:
                raise ValueError("fresh graduate window is required")
            if self.fresh_graduate_end_month < self.fresh_graduate_start_month:
                raise ValueError("fresh graduate window end must not be earlier than start")
        if self.scholarship_status == "unknown" and (
            self.scholarship_levels_any_of or self.scholarship_name_contains
        ):
            raise ValueError("unknown scholarship status cannot include detail filters")
        if any(
            _is_protected_candidate_keyword(keyword)
            for keywords in (
                self.keywords,
                self.keywords_all_of,
                self.keywords_any_of,
            )
            for keyword in keywords
        ):
            raise ValueError("sensitive_candidate_keyword_not_supported")
        return self


SearchDisplayFieldKey = Literal[
    "institution_classifications",
    "highest_degree",
    "education_degree",
    "graduation",
    "employment_months",
    "employment_or_internship_months",
    "school",
    "major",
    "academic_performance",
    "experience_type",
    "experience_name",
    "organization",
    "title",
    "experience_award",
    "skills",
    "language",
    "scholarship",
    "competition",
    "leadership",
    "keywords",
]


class CandidateSearchDisplayField(ApiModel):
    """One normalized value group for the active result-table columns.

    The client chooses which groups become visible columns from the successful
    filter snapshot.  Keeping the values structured avoids trying to split a
    human-readable evidence sentence back into school, major, company, title,
    or score values on the client. Evidence ids are supplied for source-backed
    values; aggregate counts and query terms intentionally have none.
    """

    key: SearchDisplayFieldKey
    values: list[str] = Field(default_factory=list)
    evidence_block_ids: list[str] = Field(default_factory=list)


class CandidateSearchItem(ApiModel):
    candidate_id: str
    display_name: str | None
    resume_id: str
    original_filename: str
    is_985_211: bool
    institution_classifications: list[InstitutionClassification] = Field(
        default_factory=list
    )
    highest_degree: DegreeLevel | None
    employment_months: int
    employment_or_internship_months: int
    # Compact, source-backed profile fields for the recruiter result table.
    # They deliberately do not include resume text, contact details, or an AI
    # inference.  The client can keep its table useful without having to infer
    # a school, current role, or skill list from matched filter labels.
    education_school: str | None = None
    education_major: str | None = None
    latest_experience_title: str | None = None
    latest_experience_organization: str | None = None
    latest_experience_type: str | None = None
    skill_highlights: list[str] = Field(default_factory=list)
    summary_preview: str | None = None
    score_id: str | None = None
    score_template_id: str | None = None
    score_total: float | None = None
    score_status: str | None = None
    score_template_name: str | None = None
    # Percentage of configured score weight backed by resume facts.  This is
    # evidence coverage, not an assessment of the candidate's ability.
    score_confidence: float | None = Field(default=None, ge=0, le=100)
    display_fields: list[CandidateSearchDisplayField] = Field(default_factory=list)
    matched_filters: list[str]
    matched_evidence: list["CandidateSearchMatch"] = Field(default_factory=list)


class CandidateSearchMatch(ApiModel):
    filter_key: str
    label: str
    fact_type: Literal[
        "aggregate", "education", "experience", "skill", "language", "scholarship", "keyword"
    ]
    evidence_block_ids: list[str]
    # Internal-only provenance used by the bounded recruiting Agent.  It is
    # deliberately excluded from the public candidate-search response so the
    # established filter API remains byte-for-byte compatible.
    evidence_origin: Literal["structured_fact", "resume_text"] = Field(
        default="structured_fact",
        exclude=True,
    )


class CandidateSearchResponse(ApiModel):
    items: list[CandidateSearchItem]
    next_cursor: str | None = None
    needs_review_count: int = 0
    # Total matching records before cursor pagination.  Existing callers may
    # continue to use ``items`` and ``next_cursor`` unchanged.
    total_count: int = 0


class TalentSearchHardFilters(ApiModel):
    """The intentionally small, recruiter-visible hard-filter subset.

    It is separate from the historical, broad candidate-filter schema so an
    AI draft cannot silently reintroduce removed sidebar fields or browser
    supplied arbitrary conditions.
    """

    institution_classifications_any_of: list[InstitutionClassification] = Field(
        default_factory=list,
        max_length=6,
    )
    # Any education record may satisfy this group.  Keep it independent from
    # both school classification and highest-degree filters so a profile can
    # accurately express “有本科学历” without excluding later master's or
    # doctorate study.
    education_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    highest_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    graduation_status: Literal["any", "fresh", "previous"] = "any"
    fresh_graduate_start_month: Month | None = None
    fresh_graduate_end_month: Month | None = None
    # Legacy profile revisions may still carry this value. Keep accepting it
    # on read, but do not expose or persist a formal-work-only threshold.
    min_employment_months: int | None = Field(
        default=None,
        ge=0,
        le=720,
        exclude=True,
    )
    min_employment_or_internship_months: int | None = Field(default=None, ge=0, le=720)
    experience_types_all_of: list[ExperienceType] = Field(default_factory=list, max_length=12)
    skills_all_of: list[str] = Field(default_factory=list, max_length=20)
    language_credentials_all_of: list[LanguageCredentialFilter] = Field(
        default_factory=list,
        max_length=12,
    )

    @field_validator("skills_all_of")
    @classmethod
    def valid_required_skills(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)

    @field_validator("fresh_graduate_start_month", "fresh_graduate_end_month")
    @classmethod
    def valid_months(cls, value: Month | None) -> Month | None:
        if value is not None and not MONTH_PATTERN.fullmatch(value):
            raise ValueError("month must use YYYY-MM")
        return value

    @model_validator(mode="after")
    def valid_graduation_window(self) -> "TalentSearchHardFilters":
        if self.graduation_status == "any":
            if self.fresh_graduate_start_month or self.fresh_graduate_end_month:
                raise ValueError("graduation window requires fresh or previous status")
            return self
        if not self.fresh_graduate_start_month or not self.fresh_graduate_end_month:
            raise ValueError("fresh graduate window is required")
        if self.fresh_graduate_end_month < self.fresh_graduate_start_month:
            raise ValueError("fresh graduate window end must not be earlier than start")
        return self

    @model_validator(mode="after")
    def normalize_work_tenure(self) -> "TalentSearchHardFilters":
        """Expose one recruiter-visible work-tenure condition.

        Old drafts can retain a formal-work threshold in persisted JSON. The
        current product definition of work tenure includes both employment and
        internship, so migrate the stricter numeric threshold into that one
        field instead of carrying a hidden second condition forward.
        """

        if self.min_employment_months is not None:
            self.min_employment_or_internship_months = max(
                self.min_employment_months,
                self.min_employment_or_internship_months or 0,
            )
            self.min_employment_months = None
        return self


class TalentSearchEvidencePolicy(ApiModel):
    """The source-fact boundary for one talent-profile requirement.

    ``any_fact`` preserves ordinary semantic requirements.  A recruiter who
    asks for a named technology *in a specific kind of experience* needs the
    stricter ``experience_detail_terms`` policy: a skill list or adjacent
    project is useful context, but cannot itself prove the requirement.
    """

    kind: Literal["any_fact", "experience_detail_terms"] = "any_fact"
    allowed_experience_types: list[ExperienceType] = Field(
        default_factory=list,
        max_length=12,
    )
    terms_all_of: list[str] = Field(default_factory=list, max_length=12)
    terms_any_of: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("allowed_experience_types")
    @classmethod
    def valid_allowed_experience_types(
        cls,
        value: list[ExperienceType],
    ) -> list[ExperienceType]:
        if len(value) != len(set(value)):
            raise ValueError("evidence policy experience types must be unique")
        return value

    @field_validator("terms_all_of")
    @classmethod
    def valid_terms_all_of(cls, value: list[str]) -> list[str]:
        cleaned = clean_string_list(value)
        if any(len(term) > 120 or "\x00" in term for term in cleaned):
            raise ValueError("evidence policy terms must be safe and at most 120 characters")
        return cleaned

    @field_validator("terms_any_of")
    @classmethod
    def valid_terms_any_of(cls, value: list[str]) -> list[str]:
        cleaned = clean_string_list(value)
        if any(len(term) > 120 or "\x00" in term for term in cleaned):
            raise ValueError("evidence policy terms must be safe and at most 120 characters")
        return cleaned

    @model_validator(mode="after")
    def valid_scope(self) -> "TalentSearchEvidencePolicy":
        if self.kind == "any_fact":
            if (
                self.allowed_experience_types
                or self.terms_all_of
                or self.terms_any_of
            ):
                raise ValueError("any_fact policy cannot constrain experience evidence")
            return self
        if not self.allowed_experience_types or not (
            self.terms_all_of or self.terms_any_of
        ):
            raise ValueError(
                "experience_detail_terms policy requires experience types and terms"
            )
        if self.terms_all_of and self.terms_any_of:
            raise ValueError(
                "experience_detail_terms policy cannot combine all-of and any-of terms"
            )
        return self


class TalentSearchProfileRequirement(ApiModel):
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=500)
    evidence_hint: str = Field(min_length=1, max_length=800)
    # Older confirmed drafts did not persist a machine-enforceable scope.
    # Keep them readable while new drafts always receive an explicit policy.
    evidence_policy: TalentSearchEvidencePolicy = Field(
        default_factory=TalentSearchEvidencePolicy
    )


class TalentSearchProfileGenerateRequest(ApiModel):
    message: str = Field(min_length=1, max_length=4000)
    job_version_id: str | None = Field(default=None, max_length=64)


class TalentSearchProfileRefineRequest(ApiModel):
    # The browser must identify the draft it actually displayed.  This keeps
    # one HR tab from silently refining a newer draft written in another tab.
    revision_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class TalentSearchProfileConfirmRequest(ApiModel):
    # Confirmation is an explicit acceptance of one visible draft, never an
    # implicit confirmation of whichever revision happens to be current.
    revision_id: str = Field(min_length=1, max_length=64)


class TalentSearchProfileRunRequest(ApiModel):
    # A confirmed revision is immutable.  Require the client to name it so a
    # stale card cannot start a different, later revision.
    revision_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=200)


class RecruitingAgentTalentSearchProfileRunRequest(TalentSearchProfileRunRequest):
    """Run a confirmed talent profile only inside an active Agent filter scope."""

    conversation_id: str = Field(min_length=1, max_length=64)
    context_version: int = Field(ge=1)


class TalentSearchProfileSearchRequest(ApiModel):
    """Pagination only for reading a historic talent-search run."""

    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=200)


class TalentSearchProfileRevisionResponse(ApiModel):
    revision_id: str
    revision_number: int
    source: Literal["ai_generated", "ai_refined"]
    status: Literal["draft", "confirmed", "superseded"]
    title: str
    summary: str
    hard_filters: TalentSearchHardFilters
    verification_requirements: list[TalentSearchProfileRequirement] = Field(default_factory=list)
    preferred_requirements: list[TalentSearchProfileRequirement] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    created_at: str
    confirmed_at: str | None = None


class TalentSearchProfileResponse(ApiModel):
    profile_id: str
    source_type: Literal["freeform", "job"]
    source_job_version_id: str | None = None
    original_request: str
    status: Literal["draft", "confirmed"]
    current_revision: TalentSearchProfileRevisionResponse
    created_at: str
    updated_at: str


class TalentSearchProfileListResponse(ApiModel):
    items: list[TalentSearchProfileResponse] = Field(default_factory=list)


class ResumeLibraryItem(ApiModel):
    """A compact, recruiter-facing row for the persistent resume library."""

    resume_id: str
    candidate_id: str
    display_name: str | None
    original_filename: str
    created_at: str
    extraction_status: str
    ai_extraction_status: str
    ai_extraction_error: str | None = None
    candidate_name_extraction_status: str | None = None
    candidate_name_extraction_error: str | None = None
    ai_summary_status: str | None = None
    ai_summary_error: str | None = None
    is_active: bool
    ingestion_source_type: str = "manual_upload"
    source_mailbox_config_id: str | None = None
    source_mailbox_label: str | None = None
    # Keep source-quality state on the list row.  A resume can be active from
    # an older extraction even when its stored source text has since been
    # identified as unreliable, so the client must not infer trust from
    # ``is_active`` alone.
    quality_flags: list[str] = Field(default_factory=list)
    summary_preview: str | None = None
    summary_created_at: str | None = None
    score_total: float | None = None
    score_status: str | None = None
    score_template_name: str | None = None
    score_created_at: str | None = None


class ResumeLibraryResponse(ApiModel):
    items: list[ResumeLibraryItem]
    total: int
    page: int
    page_size: int


class SavedFilterCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    filters: CandidateSearchRequest

    @model_validator(mode="after")
    def no_cursor_in_saved_filter(self) -> "SavedFilterCreate":
        if self.filters.cursor is not None:
            raise ValueError("saved_filter_cannot_include_cursor")
        return self


class SavedFilterResponse(ApiModel):
    saved_filter_id: str
    name: str
    filters: CandidateSearchRequest
    created_at: str
    updated_at: str


class ScoreDimensionCreateInput(ApiModel):
    label: str = Field(min_length=1, max_length=120)
    weight: int = Field(ge=0, le=100)
    guidance: str | None = Field(default=None, max_length=1000)

    @field_validator("label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("dimension label must not be blank")
        return normalized


class ScoreDimensionInput(ScoreDimensionCreateInput):
    """A persisted dimension with its server-managed model key.

    The key binds model output, manual adjustments, and audit data. It is an
    implementation detail, so clients receive it for compatibility but never
    submit it when creating a scoring template.
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class ScoreTemplateCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    dimensions: list[ScoreDimensionCreateInput] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_weights_and_labels(self) -> "ScoreTemplateCreate":
        if sum(item.weight for item in self.dimensions) != 100:
            raise ValueError("dimension weights must sum to 100")
        labels = [" ".join(item.label.split()).casefold() for item in self.dimensions]
        if len(labels) != len(set(labels)):
            raise ValueError("dimension labels must be unique")
        return self


class ScoreTemplateResponse(ApiModel):
    template_id: str
    name: str
    description: str | None
    version: int
    dimensions: list[ScoreDimensionInput]


class ResumeScoreFactEvidence(ApiModel):
    """A score citation resolved against the immutable fact snapshot.

    The provider only returns opaque fact IDs.  Returning the small, factual
    projection here lets the client explain a score without sending it back to
    the model or trying to reconstruct a historical snapshot in the browser.
    """

    fact_id: str
    fact_type: Literal["education", "experience", "skill", "unknown"]
    summary: str
    evidence_block_ids: list[str]


class ResumeScoreManualAdjustment(ApiModel):
    """The current manual value for one dimension, if it differs from AI."""

    raw_score: float
    reason: str
    actor: str
    adjusted_at: str


class ResumeScoreDimensionResponse(ApiModel):
    key: str
    label: str
    weight: int
    ai_raw_score: float
    final_raw_score: float
    # ``weighted_score`` remains for clients of the original API.  It is the
    # final contribution, while the two explicit fields make an override
    # unambiguous in new clients.
    weighted_score: float
    ai_weighted_score: float
    final_weighted_score: float
    rationale: str
    fact_ids: list[str]
    fact_evidence: list[ResumeScoreFactEvidence] = Field(default_factory=list)
    evidence_state: Literal["grounded", "insufficient_information"]
    uncertainties: list[str]
    manual_reason: str | None
    adjusted_at: str | None
    manual_adjustment: ResumeScoreManualAdjustment | None = None


class ResumeScoreRiskFlag(ApiModel):
    message: str
    fact_ids: list[str]
    fact_evidence: list[ResumeScoreFactEvidence] = Field(default_factory=list)


class ResumeScoreAnalysisResponse(ApiModel):
    schema_version: str | None = None
    overall_summary: str = ""
    risk_flags: list[ResumeScoreRiskFlag] = Field(default_factory=list)
    needs_human_review: bool = False


class ResumeScoreAuditEntry(ApiModel):
    audit_id: str
    action: str
    actor: str
    reason: str | None
    dimension_key: str | None
    ai_raw_score: float | None
    previous_final_raw_score: float | None
    final_raw_score: float | None
    facts_version: int | None
    template_version: int | None
    created_at: str


class ResumeScoreResponse(ApiModel):
    score_id: str
    resume_id: str
    fact_snapshot_id: str | None
    template_id: str
    template_name: str | None
    template_description: str | None
    facts_version: int
    template_version: int
    fact_snapshot_created_at: str | None
    is_current_facts_version: bool
    is_current_template_version: bool
    total_score: float
    ai_total_score: float | None
    dimension_scores: list[ResumeScoreDimensionResponse]
    analysis: ResumeScoreAnalysisResponse
    audit_trail: list[ResumeScoreAuditEntry] = Field(default_factory=list)
    status: str
    model_name: str | None
    created_at: str


class ResumeScoreCreate(ApiModel):
    template_id: str


class ResumeScoreBatchResponse(ApiModel):
    batch_id: str
    template_id: str
    template_name: str | None
    template_version: int
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    cached_count: int
    requested_at: str
    started_at: str | None
    completed_at: str | None
    last_error: str | None


class ResumeScoreBatchItemResponse(ApiModel):
    item_id: str
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    facts_version: int
    status: str
    attempt_count: int
    last_error: str | None
    resume_score_id: str | None
    was_cached: bool
    completed_at: str | None
    updated_at: str


class ResumeScoreOverride(ApiModel):
    raw_score: float = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1000)


class ResumeSummaryResponse(ApiModel):
    summary_id: str
    resume_id: str
    fact_snapshot_id: str | None
    facts_version: int
    content: dict[str, object]
    source: str
    supersedes_id: str | None
    is_current: bool
    status: str
    model_name: str | None
    created_at: str


class ResumeSummaryManualCreate(ApiModel):
    content: dict[str, str]

    @model_validator(mode="after")
    def non_empty_content(self) -> "ResumeSummaryManualCreate":
        if not self.content or not any(value.strip() for value in self.content.values()):
            raise ValueError("manual_summary_content_must_not_be_empty")
        if any(not key.strip() or not value.strip() for key, value in self.content.items()):
            raise ValueError("manual_summary_sections_must_not_be_blank")
        return self


class JobRequirements(ApiModel):
    must_have: list[str] = Field(default_factory=list, max_length=20)
    preferred: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("must_have", "preferred")
    @classmethod
    def valid_requirements(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class JobCreate(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=1, max_length=20000)
    requirements: JobRequirements = Field(default_factory=JobRequirements)


class OriginalJobPublishRequest(ApiModel):
    """Publish an externally supplied JD without invoking any AI workflow.

    ``jd_text`` deliberately is not normalized or stripped: this endpoint is
    for retaining the source JD exactly as supplied.  Validation only rejects
    unusable values while leaving every valid character and whitespace intact.
    """

    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=1, max_length=20000)

    @field_validator("title")
    @classmethod
    def non_blank_title_without_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("original_job_title_must_not_contain_nul")
        normalized = value.strip()
        if not normalized:
            raise ValueError("original_job_title_must_not_be_blank")
        return normalized

    @field_validator("jd_text")
    @classmethod
    def non_blank_jd_without_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("original_jd_text_must_not_contain_nul")
        if not value.strip():
            raise ValueError("original_jd_text_must_not_be_blank")
        return value


class JobGenerationRequest(ApiModel):
    """Business context used to create an editable, recruiter-ready JD."""

    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=12000)

    @field_validator("title", "brief")
    @classmethod
    def non_blank_generation_input(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("job_generation_input_must_not_be_blank")
        if "\x00" in normalized:
            raise ValueError("job_generation_input_must_not_contain_nul")
        return normalized


class JobGenerationResponse(ApiModel):
    """A generated JD ready to be persisted through the normal jobs endpoint."""

    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=1, max_length=20000)
    requirements: JobRequirements

    @model_validator(mode="after")
    def requirements_are_verbatim_in_jd(self) -> "JobGenerationResponse":
        if not self.requirements.must_have:
            raise ValueError("generated_job_requires_must_have_requirement")
        requirement_values = [
            *self.requirements.must_have,
            *self.requirements.preferred,
        ]
        normalized_values = [" ".join(value.casefold().split()) for value in requirement_values]
        if len(normalized_values) != len(set(normalized_values)):
            raise ValueError("generated_job_requirements_must_be_unique")
        if any(
            not normalized_contains(self.jd_text, value)
            for value in requirement_values
        ):
            raise ValueError("generated_job_requirement_not_grounded_in_jd")
        return self


class JobResponse(ApiModel):
    job_id: str
    title: str
    jd_text: str
    requirements: JobRequirements
    version: int


JobRequirementPriority = Literal["must_have", "preferred"]
JobRequirementCategory = Literal[
    "skill",
    "experience",
    "education",
    "major",
    "keyword",
    "other",
]


class JobClauseResponse(ApiModel):
    clause_id: str
    ordinal: int
    text: str


class JobRequirementInput(ApiModel):
    # Keep this compatible with the strict JD-provider contract.  The model is
    # allowed to issue stable keys such as `requirement-001`, while manually
    # created requirements can still use the shorter `req-001` form.
    requirement_key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,63}$",
    )
    priority: JobRequirementPriority
    category: JobRequirementCategory
    raw_requirement: str = Field(min_length=1, max_length=1000)
    terms: list[str] = Field(default_factory=list, max_length=10)
    minimum_months: int | None = Field(default=None, ge=0, le=720)
    clause_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("terms", "clause_ids")
    @classmethod
    def valid_requirement_lists(cls, value: list[str]) -> list[str]:
        return clean_string_list(value)


class JobRequirementResponse(ApiModel):
    requirement_id: str
    requirement_key: str
    priority: JobRequirementPriority
    category: JobRequirementCategory
    raw_requirement: str
    terms: list[str]
    minimum_months: int | None
    weight: int
    clause_ids: list[str]
    sort_order: int


class JobVersionResponse(ApiModel):
    job_version_id: str
    job_id: str
    version: int
    title: str
    raw_text: str
    status: Literal["draft", "confirmed", "archived"]
    created_at: str
    confirmed_at: str | None
    clauses: list[JobClauseResponse]
    requirements: list[JobRequirementResponse]


class JobVersionRequirementsUpdate(ApiModel):
    title: str = Field(min_length=1, max_length=200)
    requirements: list[JobRequirementInput] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_requirement_keys(self) -> "JobVersionRequirementsUpdate":
        explicit_keys = [
            requirement.requirement_key
            for requirement in self.requirements
            if requirement.requirement_key is not None
        ]
        if len(explicit_keys) != len(set(explicit_keys)):
            raise ValueError("job_requirement_keys_must_be_unique")
        return self


class JobMatchCreate(ApiModel):
    job_version_id: str


class JobMatchBatchResponse(ApiModel):
    batch_id: str
    job_version_id: str
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    requested_at: str
    started_at: str | None
    completed_at: str | None
    last_error: str | None


class JobMatchBatchItemResponse(ApiModel):
    item_id: str
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    facts_version: int
    status: str
    attempt_count: int
    last_error: str | None
    job_match_id: str | None
    completed_at: str | None
    updated_at: str


class JobMatchRequirementResponse(ApiModel):
    requirement_id: str
    requirement_key: str
    priority: JobRequirementPriority
    requirement_text: str
    clause_ids: list[str]
    outcome: Literal["met", "partial", "not_met", "unknown"]
    reason: str
    fact_ids: list[str]
    missing_or_uncertain: str | None
    score_contribution: float


class JobMatchResponse(ApiModel):
    match_id: str
    job_id: str
    job_version_id: str | None
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    fact_snapshot_id: str | None
    facts_version: int
    job_version: int
    total_score: float
    must_have_passed: bool | None
    evidence_coverage: float | None
    # `total_score` is retained as the historical, all-requirements score.  It
    # treats an unknown requirement as a zero contribution.  The UI should use
    # `match_score` together with `match_confidence` for candidate ranking.
    match_score: float
    match_confidence: float | None
    match_lane: Literal["recommended", "pending", "unmet"]
    hard_requirement_status: str | None
    analysis: dict[str, object]
    requirement_results: list[JobMatchRequirementResponse]
    status: str
    model_name: str | None
    created_at: str


class TalentSearchProfileMatchResult(ApiModel):
    """A profile-safe view of one internal semantic match.

    Internal profile matcher job IDs are deliberately omitted so this endpoint
    cannot become another route into the normal JD workspace.
    """

    match_id: str
    resume_id: str
    candidate_id: str
    candidate_display_name: str | None
    facts_version: int
    match_score: float
    match_confidence: float | None
    match_lane: Literal["recommended", "pending", "unmet"]
    hard_requirement_status: str | None
    analysis: dict[str, object]
    requirement_results: list[JobMatchRequirementResponse]
    status: str
    created_at: str


class TalentSearchRecallDiagnosticStep(ApiModel):
    """One server-derived stage in a strict profile-recall funnel."""

    key: str
    label: str
    remaining_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)


class TalentSearchRecallDiagnostics(ApiModel):
    """Safe aggregate explanation for a zero-result profile search.

    These are workspace-scoped counts only.  They deliberately do not expose
    candidate names, source text, or an unsupported claim that an omitted fact
    means a candidate lacks that capability.
    """

    eligible_resume_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    strict_match_count: int = Field(ge=0)
    steps: list[TalentSearchRecallDiagnosticStep] = Field(default_factory=list)


class TalentSearchRunResponse(ApiModel):
    run_id: str
    profile_id: str
    revision_id: str
    # Global runs search the whole workspace. A ``candidate_filter`` run is
    # confined to a private, frozen Agent scope; its opaque digest is never
    # returned to the browser.
    scope_kind: Literal["global", "candidate_filter"] = "global"
    scope_candidate_count: int = Field(default=0, ge=0)
    status: Literal["queued", "running", "completed", "partial"]
    # A pure hard-filter profile intentionally has no semantic match batch.
    # Expose that distinction so an empty AI-match list is never rendered as
    # “zero candidates found”.
    result_mode: Literal["hard_filter_recall", "semantic_verification"]
    total_recalled_count: int = Field(ge=0)
    job_match_batch_id: str | None = None
    match_total_count: int = Field(default=0, ge=0)
    match_completed_count: int = Field(default=0, ge=0)
    match_failed_count: int = Field(default=0, ge=0)
    # Results are constrained to the durable, server-derived batch items for
    # this one profile run. They never expose the internal matcher JD.
    match_results: list[TalentSearchProfileMatchResult] = Field(default_factory=list)
    created_at: str
    updated_at: str
    # Read from the immutable run snapshot rather than the mutable current
    # profile revision, so a historic zero-result explanation remains honest
    # after HR later refines the profile.
    applied_hard_filters: TalentSearchHardFilters
    recall_diagnostics: TalentSearchRecallDiagnostics | None = None
    candidate_recall: CandidateSearchResponse
    # Present only for the conversation-scoped run endpoint.  Returning the
    # advanced version lets the next Agent turn retain stale-tab protection.
    conversation_id: str | None = None
    context_version: int | None = Field(default=None, ge=1)
    active_context: RecruitingAgentActiveContext | None = None


# ``RecruitingAgentResponse`` is declared before the reusable talent-profile
# response models to keep related Agent request/response schemas together.
# Rebuild its forward reference after this module has defined the full profile
# shape, rather than weakening the contract to an untyped dictionary.
RecruitingAgentResponse.model_rebuild()
