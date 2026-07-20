from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    access_enabled: bool


class AuthSession(ApiModel):
    authenticated: bool
    login_required: bool
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


class OrganizationPlanAssign(ApiModel):
    plan_code: str = Field(min_length=1, max_length=64)
    plan_status: Literal["trial", "active", "expired", "suspended"] | None = None


class MailboxConfigUpdate(ApiModel):
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    email_address: str = Field(min_length=3, max_length=320)
    mailbox: str = Field(default="INBOX", min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool = True


class MailboxConfigResponse(ApiModel):
    configured: bool
    imap_host: str | None = None
    imap_port: int | None = None
    email_address: str | None = None
    mailbox: str | None = None
    enabled: bool = False
    password_configured: bool = False
    # Deliberately expose the binding time, but not the IMAP UID internals.
    import_started_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class MailboxSyncResponse(ApiModel):
    configured: bool
    imported_count: int = 0
    duplicate_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class MailboxImportResponse(ApiModel):
    import_id: str
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


class RecruitingAgentRequest(ApiModel):
    """One bounded recruiting-assistant turn.

    The browser supplies only the user's current selection.  The assistant
    never receives a PDF or unrestricted database access from the client.
    """

    message: str = Field(min_length=1, max_length=2000)
    job_version_id: str | None = Field(default=None, max_length=64)
    resume_id: str | None = Field(default=None, max_length=64)


class RecruitingAgentCandidate(ApiModel):
    candidate_id: str
    resume_id: str
    display_name: str | None
    detail: str
    score: float | None = None


class RecruitingAgentAction(ApiModel):
    action: Literal["open_resume", "open_match_workspace"]
    label: str
    resume_id: str | None = None


class RecruitingAgentToolTrace(ApiModel):
    tool: str
    summary: str


class RecruitingAgentResponse(ApiModel):
    message: str
    intent: Literal[
        "search_candidates",
        "run_job_matching",
        "show_job_ranking",
        "explain_candidate",
        "score_current_candidate",
        "help",
    ]
    job_version_id: str | None = None
    candidates: list[RecruitingAgentCandidate] = Field(default_factory=list)
    actions: list[RecruitingAgentAction] = Field(default_factory=list)
    tool_trace: list[RecruitingAgentToolTrace] = Field(default_factory=list)
    batch_id: str | None = None


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
    is_active: bool
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


class ResumeReviewDetail(ResumeDetail):
    original_filename: str
    facts_version: int
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
    institution_tiers_any_of: list[InstitutionTier] = Field(default_factory=list, max_length=10)
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
    highest_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    graduation_status: Literal["any", "fresh", "previous"] = "any"
    fresh_graduate_start_month: Month | None = None
    fresh_graduate_end_month: Month | None = None
    min_employment_months: int | None = Field(default=None, ge=0, le=720)
    min_employment_or_internship_months: int | None = Field(default=None, ge=0, le=720)
    education_any_of: list[EducationFilter] = Field(default_factory=list, max_length=10)
    experience_any_of: list[ExperienceFilter] = Field(default_factory=list, max_length=10)
    skill_categories_any_of: list[SkillCategory] = Field(default_factory=list, max_length=10)
    skills_all_of: list[str] = Field(default_factory=list, max_length=20)
    skills_any_of: list[str] = Field(default_factory=list, max_length=20)
    language_credentials_any_of: list[LanguageCredentialFilter] = Field(default_factory=list, max_length=12)
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
        return self


class CandidateSearchItem(ApiModel):
    candidate_id: str
    display_name: str | None
    resume_id: str
    original_filename: str
    is_985_211: bool
    highest_degree: DegreeLevel | None
    employment_months: int
    employment_or_internship_months: int
    summary_preview: str | None = None
    score_total: float | None = None
    score_template_name: str | None = None
    matched_filters: list[str]
    matched_evidence: list["CandidateSearchMatch"] = Field(default_factory=list)


class CandidateSearchMatch(ApiModel):
    filter_key: str
    label: str
    fact_type: Literal[
        "aggregate", "education", "experience", "skill", "language", "scholarship", "keyword"
    ]
    evidence_block_ids: list[str]


class CandidateSearchResponse(ApiModel):
    items: list[CandidateSearchItem]
    next_cursor: str | None = None
    needs_review_count: int = 0


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
    is_active: bool
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


class ScoreDimensionInput(ApiModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str = Field(min_length=1, max_length=120)
    weight: int = Field(ge=0, le=100)
    guidance: str | None = Field(default=None, max_length=1000)


class ScoreTemplateCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    dimensions: list[ScoreDimensionInput] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_weights_and_keys(self) -> "ScoreTemplateCreate":
        if sum(item.weight for item in self.dimensions) != 100:
            raise ValueError("dimension weights must sum to 100")
        keys = [item.key for item in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValueError("dimension keys must be unique")
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
