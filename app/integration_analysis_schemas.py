"""Private external-AI draft input, separate from official business results.

Clients select existing fact IDs. They cannot submit new text as a verified
resume fact, a formal score, or a recruitment decision through this surface.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.integration_read_schemas import IntegrationCandidateProfile


ReferenceKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
DraftText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class IntegrationAnalysisSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IntegrationAnalysisCandidateReference(IntegrationAnalysisSchema):
    candidate_id: UUID
    resume_id: UUID
    fact_snapshot_id: UUID
    facts_version: int = Field(ge=1, strict=True)
    fact_ids: list[ReferenceKey] = Field(min_length=1, max_length=40)
    source_block_ids: list[ReferenceKey] = Field(default_factory=list, max_length=10)

    @field_validator("fact_ids", "source_block_ids")
    @classmethod
    def bounded_unique_identifiers(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(any(ord(c) < 32 for c in item) for item in value):
            raise ValueError("invalid_or_duplicate_reference")
        return value


class IntegrationAnalysisJobReference(IntegrationAnalysisSchema):
    job_id: UUID
    job_version_id: UUID


class IntegrationAnalysisObservation(IntegrationAnalysisSchema):
    candidate_id: UUID
    text: DraftText

    @field_validator("text")
    @classmethod
    def no_hidden_control_characters(cls, value: str) -> str:
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("invalid_draft_text")
        return value


class IntegrationAnalysisDraftInput(IntegrationAnalysisSchema):
    idempotency_key: UUID
    title: str = Field(min_length=1, max_length=120)
    candidates: list[IntegrationAnalysisCandidateReference] = Field(min_length=1, max_length=20)
    job: IntegrationAnalysisJobReference | None = None
    inferences: list[IntegrationAnalysisObservation] = Field(default_factory=list, max_length=40)
    questions_to_verify: list[IntegrationAnalysisObservation] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def explicit_bounded_draft(self) -> "IntegrationAnalysisDraftInput":
        candidate_ids = {reference.candidate_id for reference in self.candidates}
        if len(candidate_ids) != len(self.candidates):
            raise ValueError("duplicate_candidate_reference")
        observations = self.inferences + self.questions_to_verify
        if any(item.candidate_id not in candidate_ids for item in observations):
            raise ValueError("unreferenced_candidate")
        if sum(len(item.text) for item in observations) > 16000:
            raise ValueError("draft_text_budget_exceeded")
        if any(ord(c) < 32 for c in self.title):
            raise ValueError("invalid_draft_title")
        return self


class IntegrationAnalysisPrepare(IntegrationAnalysisDraftInput):
    """External API/MCP payload; deliberately cannot choose a report to overwrite."""


class IntegrationAnalysisCandidateLink(IntegrationAnalysisSchema):
    candidate_id: str
    candidate_code: str
    resume_id: str
    fact_snapshot_id: str
    facts_version: int


class IntegrationAnalysisDraftSummary(IntegrationAnalysisSchema):
    id: str
    kind: Literal["external_ai_draft"] = "external_ai_draft"
    title: str
    version: int
    source_status: Literal["current", "source_changed"]
    candidates: list[IntegrationAnalysisCandidateLink]
    job: IntegrationAnalysisJobReference | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class IntegrationAnalysisDraftDetail(IntegrationAnalysisDraftSummary):
    # Each profile includes only the fact IDs explicitly selected on save.
    referenced_facts: list[IntegrationCandidateProfile]
    inferences: list[IntegrationAnalysisObservation]
    questions_to_verify: list[IntegrationAnalysisObservation]
    decision_authority: Literal["recruiting_team"] = "recruiting_team"


class IntegrationAnalysisDraftList(IntegrationAnalysisSchema):
    items: list[IntegrationAnalysisDraftSummary]
    next_cursor: str | None


class IntegrationAnalysisSaved(IntegrationAnalysisSchema):
    id: str
    version: int
    kind: Literal["external_ai_draft"] = "external_ai_draft"
    replayed: bool = False


class IntegrationAnalysisPrepared(IntegrationAnalysisSchema):
    """A short-lived draft awaiting explicit confirmation in the web app."""

    id: str
    version: int
    status: Literal["awaiting_confirmation", "saved", "expired"]
    expires_at: datetime | None
    replayed: bool = False


class IntegrationAnalysisPendingSummary(IntegrationAnalysisSchema):
    id: str
    title: str
    version: int
    candidates: list[IntegrationAnalysisCandidateLink]
    job: IntegrationAnalysisJobReference | None
    job_title: str | None
    source_connection_name: str
    created_at: datetime
    expires_at: datetime


class IntegrationAnalysisPendingDetail(IntegrationAnalysisSchema):
    id: str
    title: str
    version: int
    candidates: list[IntegrationAnalysisCandidateLink]
    job: IntegrationAnalysisJobReference | None
    job_title: str | None
    source_connection_name: str
    payload_sha256: str
    source_status: Literal["current", "source_changed"]
    referenced_facts: list[IntegrationCandidateProfile]
    inferences: list[IntegrationAnalysisObservation]
    questions_to_verify: list[IntegrationAnalysisObservation]
    created_at: datetime
    expires_at: datetime


class IntegrationAnalysisPendingList(IntegrationAnalysisSchema):
    items: list[IntegrationAnalysisPendingSummary]


class IntegrationAnalysisConfirmInput(IntegrationAnalysisSchema):
    version: int = Field(ge=1, strict=True)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class IntegrationAnalysisDiscardInput(IntegrationAnalysisSchema):
    version: int = Field(ge=1, strict=True)


class IntegrationAnalysisConfirmed(IntegrationAnalysisSchema):
    id: str
    version: int
    confirmed_at: datetime
    status: Literal["saved"] = "saved"
