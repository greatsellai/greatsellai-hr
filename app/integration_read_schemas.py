"""Strict, privacy-minimized wire contracts for integration reads.

These DTOs intentionally do not inherit the browser API schemas.  Adding a
field to the browser response must never widen the external REST/MCP contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class IntegrationReadSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntegrationConnectionInfo(IntegrationReadSchema):
    status: Literal["connected"] = "connected"
    api_version: Literal["2026-09-22"] = "2026-09-22"
    audience: Literal["rest", "mcp"]
    scopes: list[str]
    organization_id: str
    user_id: str
    candidate_identity_default: Literal["code"] = "code"
    mcp_url: str
    api_base_url: str


class IntegrationOption(IntegrationReadSchema):
    value: str
    label: str


class IntegrationFilterOptions(IntegrationReadSchema):
    schema_version: str
    degrees: list[IntegrationOption]
    institution_classifications: list[IntegrationOption]
    experience_types: list[IntegrationOption]
    skill_categories: list[IntegrationOption]
    language_credentials: list[IntegrationOption]
    graduation_statuses: list[IntegrationOption]
    presence_statuses: list[IntegrationOption]
    keyword_modes: list[IntegrationOption]


DegreeLevel = Literal[
    "doctor",
    "master",
    "bachelor",
    "associate",
    "high_school",
    "vocational_or_below",
    "unknown",
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
]
InstitutionClassification = Literal[
    "985",
    "211",
    "undergraduate",
    "associate",
    "secondary_vocational",
    "overseas",
]
LanguageCredentialCode = Literal[
    "cet4",
    "cet6",
    "ielts",
    "toefl",
    "tem4",
    "tem8",
    "bec",
    "toeic",
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
FilterText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
]
SourceBlockId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class IntegrationCandidateSearchRequest(IntegrationReadSchema):
    condition_match_mode: Literal["all", "any"] = "all"
    is_985_211: bool | None = None
    education_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    institution_classifications_any_of: list[InstitutionClassification] = Field(
        default_factory=list,
        max_length=6,
    )
    highest_degree_in: list[DegreeLevel] = Field(default_factory=list, max_length=6)
    graduation_status: Literal["any", "fresh", "previous"] = "any"
    fresh_graduate_start_month: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}$"
    )
    fresh_graduate_end_month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    min_employment_months: int | None = Field(default=None, ge=0, le=720)
    min_employment_or_internship_months: int | None = Field(default=None, ge=0, le=720)
    experience_types_all_of: list[ExperienceType] = Field(
        default_factory=list, max_length=12
    )
    skill_categories_any_of: list[SkillCategory] = Field(
        default_factory=list, max_length=10
    )
    skills_all_of: list[FilterText] = Field(default_factory=list, max_length=20)
    skills_any_of: list[FilterText] = Field(default_factory=list, max_length=20)
    language_credentials_any_of: list[LanguageCredentialCode] = Field(
        default_factory=list,
        max_length=8,
    )
    keywords_all_of: list[FilterText] = Field(default_factory=list, max_length=10)
    keywords_any_of: list[FilterText] = Field(default_factory=list, max_length=10)
    keyword_match_mode: Literal["broad", "precise"] = "broad"
    scholarship_status: Literal["any", "present", "unknown"] = "any"
    competition_status: Literal["any", "present", "unknown"] = "any"
    competition_award_status: Literal["any", "present", "unknown"] = "any"
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator(
        "skill_categories_any_of",
        "skills_all_of",
        "skills_any_of",
        "keywords_all_of",
        "keywords_any_of",
    )
    @classmethod
    def clean_string_lists(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) != len(value) or len(set(cleaned)) != len(cleaned):
            raise ValueError("blank_or_duplicate_filter")
        return cleaned

    @model_validator(mode="after")
    def validate_graduation_window(self) -> "IntegrationCandidateSearchRequest":
        if self.graduation_status != "any":
            if not self.fresh_graduate_start_month or not self.fresh_graduate_end_month:
                raise ValueError("fresh_graduate_window_required")
            if self.fresh_graduate_end_month < self.fresh_graduate_start_month:
                raise ValueError("invalid_fresh_graduate_window")
        text_values = (
            self.skill_categories_any_of
            + self.skills_all_of
            + self.skills_any_of
            + self.keywords_all_of
            + self.keywords_any_of
        )
        if sum(len(item) for item in text_values) > 8_000:
            raise ValueError("filter_text_budget_exceeded")
        return self


class IntegrationCandidateSearchItem(IntegrationReadSchema):
    candidate_id: str
    candidate_code: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int
    is_985_211: bool | None
    highest_degree: str | None
    employment_months: int | None
    employment_or_internship_months: int | None
    education_school: str | None
    education_major: str | None
    latest_experience_title: str | None
    latest_experience_organization: str | None
    latest_experience_type: str | None
    skill_highlights: list[str]
    score_total: float | None
    score_status: str | None
    evidence_source_block_ids: list[str]
    omitted_fields: list[str] = Field(default_factory=list)


class IntegrationCandidateSearchResponse(IntegrationReadSchema):
    items: list[IntegrationCandidateSearchItem]
    next_cursor: str | None
    total_count: int
    needs_review_count: int


class IntegrationFactBase(IntegrationReadSchema):
    fact_id: str
    evidence_source_block_ids: list[str]


class IntegrationEducationFact(IntegrationFactBase):
    school: str | None
    degree: str | None
    major: str | None
    start_month: str | None
    end_month: str | None
    institution_tiers: list[str]
    institution_classification: str | None
    average_score: float | None
    gpa_value: float | None
    gpa_scale: float | None
    rank_position: int | None
    rank_total: int | None


class IntegrationExperienceDetail(IntegrationReadSchema):
    detail: str
    evidence_source_block_ids: list[str]


class IntegrationExperienceFact(IntegrationFactBase):
    experience_type: str | None
    experience_name: str | None
    organization: str | None
    title: str | None
    start_month: str | None
    end_month: str | None
    is_current: bool | None
    leadership_context: str | None
    leadership_role: str | None
    award_level: str | None
    award_result: str | None
    details: list[IntegrationExperienceDetail]


class IntegrationSkillFact(IntegrationFactBase):
    skill: str
    category: str | None


class IntegrationLanguageFact(IntegrationFactBase):
    credential: str
    score: str | None


class IntegrationScholarshipFact(IntegrationFactBase):
    name: str
    level: str | None


class IntegrationCandidateFacts(IntegrationReadSchema):
    is_985_211: bool | None
    highest_degree: str | None
    employment_months: int | None
    employment_or_internship_months: int | None
    education: list[IntegrationEducationFact]
    experiences: list[IntegrationExperienceFact]
    skills: list[IntegrationSkillFact]
    language_credentials: list[IntegrationLanguageFact]
    scholarships: list[IntegrationScholarshipFact]


class IntegrationCandidateProfile(IntegrationReadSchema):
    candidate_id: str
    candidate_code: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int
    facts: IntegrationCandidateFacts
    evidence_source_block_ids: list[str]
    omitted_fields: list[str]


class IntegrationCandidateEvidenceRequest(IntegrationReadSchema):
    source_block_ids: list[SourceBlockId] = Field(min_length=1, max_length=20)

    @field_validator("source_block_ids")
    @classmethod
    def unique_source_blocks(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_source_block_id")
        return value


class IntegrationEvidenceExcerpt(IntegrationReadSchema):
    source_block_id: str
    page_no: int
    text: str | None
    truncated: bool
    omitted: bool
    omission_reason: str | None


class IntegrationCandidateEvidence(IntegrationReadSchema):
    candidate_id: str
    candidate_code: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int
    excerpts: list[IntegrationEvidenceExcerpt]


class IntegrationScoreAssessment(IntegrationReadSchema):
    score_id: str
    fact_snapshot_id: str
    facts_version: int
    template_id: str
    template_version: int
    total_score: float
    evidence_coverage: float | None
    status: str
    created_at: datetime


class IntegrationJobMatchAssessment(IntegrationReadSchema):
    match_id: str
    fact_snapshot_id: str
    facts_version: int
    job_id: str
    job_version_id: str
    job_version: int
    total_score: float
    must_have_passed: bool | None
    evidence_coverage: float | None
    hard_requirement_status: str | None
    status: str
    cited_fact_ids: list[str]
    created_at: datetime


class IntegrationSummarySection(IntegrationReadSchema):
    key: str
    text: str
    fact_ids: list[str]
    evidence_source_block_ids: list[str]


class IntegrationSummaryAssessment(IntegrationReadSchema):
    summary_id: str
    fact_snapshot_id: str
    facts_version: int
    source: str
    sections: list[IntegrationSummarySection]
    omitted_sections: list[str]
    created_at: datetime


class IntegrationCandidateAssessments(IntegrationReadSchema):
    candidate_id: str
    candidate_code: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int
    summaries: list[IntegrationSummaryAssessment]
    scores: list[IntegrationScoreAssessment]
    job_matches: list[IntegrationJobMatchAssessment]


class IntegrationJobSummary(IntegrationReadSchema):
    job_id: str
    title: str
    recruiting_status: str
    current_version: int
    current_version_id: str | None
    updated_at: datetime


class IntegrationJobList(IntegrationReadSchema):
    items: list[IntegrationJobSummary]
    next_cursor: str | None


class IntegrationJobRequirement(IntegrationReadSchema):
    requirement_id: str
    requirement_key: str
    priority: str
    category: str
    requirement: str
    minimum_months: int | None
    weight: int
    source_clause_ids: list[str]


class IntegrationJobClause(IntegrationReadSchema):
    clause_id: str
    ordinal: int
    text: str


class IntegrationJobRequirements(IntegrationReadSchema):
    job_id: str
    job_version_id: str
    version: int
    title: str
    status: str
    requirements: list[IntegrationJobRequirement]
    clauses: list[IntegrationJobClause]
