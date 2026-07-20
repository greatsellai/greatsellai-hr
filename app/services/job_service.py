from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    Job,
    JobMatch,
    JobMatchRequirementResult,
    JobRequirement,
    JobSourceClause,
    JobVersion,
    Resume,
    ResumeFactSnapshot,
)
from app.schemas import (
    JobClauseResponse,
    JobCreate,
    JobGenerationRequest,
    JobGenerationResponse,
    JobMatchCreate,
    JobMatchRequirementResponse,
    JobMatchResponse,
    JobRequirements,
    OriginalJobPublishRequest,
    JobRequirementInput,
    JobRequirementResponse,
    JobVersionRequirementsUpdate,
    JobVersionResponse,
)
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    extract_jd_requirements_from_clauses,
    generate_jd_from_brief,
    match_resume_fact_snapshot_against_requirements,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
)
from app.services.normalization import normalized_contains
from app.services.resume_eligibility import has_unreliable_source_text


class JobServiceError(RuntimeError):
    pass


class JobNotFoundError(JobServiceError):
    pass


class JobVersionNotFoundError(JobServiceError):
    pass


class JobMatchNotFoundError(JobServiceError):
    pass


# A JD match has two intentionally separate dimensions:
# - match score: how well the requirements with actual evidence match;
# - match confidence: how much of the JD has explicit evidence at all.
#
# Keep the legacy `total_score` persisted for auditability and backwards
# compatibility.  Its old semantics make unknown requirements contribute zero,
# so it must not be used as the default ranking score.
MATCH_CONFIDENCE_RECOMMENDED_THRESHOLD = 60.0
MATCH_LANE_RECOMMENDED = "recommended"
MATCH_LANE_PENDING = "pending"
MATCH_LANE_UNMET = "unmet"
_NO_KEY_ERROR = "deepseek_api_key_not_configured"


_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*(?:years?|年)(?!\w)", re.IGNORECASE)
_MONTH_PATTERN = re.compile(r"(?<!\d)(\d{1,3})\s*(?:months?|个月)(?!\w)", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_ai_gateway_credentials(settings: AppSettings) -> None:
    """Keep the established no-key error while accepting gateway credentials.

    Provider credentials now live behind non-secret references in the gateway
    control plane.  Existing installations only have the legacy runtime key,
    so callers and HTTP error mapping must retain their stable
    ``deepseek_api_key_not_configured`` response when neither source exists.
    """

    if not ai_gateway_credentials_configured(settings):
        raise JobServiceError(_NO_KEY_ERROR)


def _gateway_compatibility_credentials(settings: AppSettings) -> tuple[str, str, int]:
    """Return the legacy-provider arguments consumed only by prompt helpers.

    ``deepseek_provider`` owns the existing strict schema and evidence
    validation.  Once it is called inside ``ai_gateway_execution``, its
    transport is intercepted and the gateway replaces the model, endpoint,
    and credential with the platform-approved route.  An empty key is
    therefore valid here when the selected route resolves a generic
    credential-map entry.
    """

    return gateway_prompt_transport_arguments(settings)


def _split_clauses(raw_text: str) -> list[str]:
    lines = [line.strip(" \t-•·") for line in raw_text.replace("\r\n", "\n").split("\n")]
    clauses = [line for line in lines if line]
    if not clauses:
        raise JobServiceError("jd_text_has_no_clauses")
    return clauses


def _months_from_text(value: str) -> int | None:
    month_match = _MONTH_PATTERN.search(value)
    if month_match:
        return int(month_match.group(1))
    year_match = _YEAR_PATTERN.search(value)
    if year_match:
        return int(year_match.group(1)) * 12
    return None


def _clause_response(clause: JobSourceClause) -> JobClauseResponse:
    return JobClauseResponse(
        clause_id=clause.clause_id,
        ordinal=clause.ordinal,
        text=clause.text,
    )


def _requirement_response(requirement: JobRequirement) -> JobRequirementResponse:
    normalized_value = requirement.normalized_value or {}
    terms = normalized_value.get("terms", [])
    return JobRequirementResponse(
        requirement_id=requirement.id,
        requirement_key=requirement.requirement_key,
        priority=requirement.priority,
        category=requirement.category,
        raw_requirement=requirement.raw_requirement,
        terms=list(terms) if isinstance(terms, list) else [],
        minimum_months=requirement.minimum_months,
        weight=requirement.weight,
        clause_ids=requirement.clause_ids or [],
        sort_order=requirement.sort_order,
    )


def _version_response(job_version: JobVersion) -> JobVersionResponse:
    return JobVersionResponse(
        job_version_id=job_version.id,
        job_id=job_version.job_id,
        version=job_version.version,
        title=job_version.title,
        raw_text=job_version.raw_text,
        status=job_version.status,
        created_at=job_version.created_at.isoformat(),
        confirmed_at=(
            job_version.confirmed_at.isoformat()
            if job_version.confirmed_at is not None
            else None
        ),
        clauses=[
            _clause_response(clause)
            for clause in sorted(job_version.clauses, key=lambda item: item.ordinal)
        ],
        requirements=[
            _requirement_response(requirement)
            for requirement in sorted(
                job_version.requirements,
                key=lambda item: item.sort_order,
            )
        ],
    )


def _create_clauses(session: Session, *, job_version: JobVersion) -> list[JobSourceClause]:
    clauses: list[JobSourceClause] = []
    for ordinal, text in enumerate(_split_clauses(job_version.raw_text), start=1):
        clause = JobSourceClause(
            job_version_id=job_version.id,
            clause_id=f"clause-{ordinal:03d}",
            ordinal=ordinal,
            text=text,
        )
        session.add(clause)
        clauses.append(clause)
    session.flush()
    return clauses


def _grounded_clause_ids(
    clauses: list[JobSourceClause],
    *,
    raw_requirement: str,
) -> list[str]:
    return [
        clause.clause_id
        for clause in clauses
        if normalized_contains(clause.text, raw_requirement)
    ]


def _allocate_weights(requirements: list[JobRequirementInput]) -> list[int]:
    if not requirements:
        return []
    must_indexes = [
        index
        for index, requirement in enumerate(requirements)
        if requirement.priority == "must_have"
    ]
    preferred_indexes = [
        index
        for index, requirement in enumerate(requirements)
        if requirement.priority == "preferred"
    ]
    allocations: dict[int, int] = {}
    groups = [
        (must_indexes, 7000 if preferred_indexes else 10000),
        (preferred_indexes, 3000 if must_indexes else 10000),
    ]
    for indexes, total in groups:
        if not indexes:
            continue
        base, remainder = divmod(total, len(indexes))
        for position, index in enumerate(indexes):
            allocations[index] = base + (1 if position < remainder else 0)
    return [allocations[index] for index in range(len(requirements))]


def _validate_requirement_clauses(
    *,
    requirement: JobRequirementInput,
    clauses_by_id: dict[str, JobSourceClause],
) -> None:
    if not set(requirement.clause_ids).issubset(clauses_by_id):
        raise JobServiceError("job_requirement_clause_not_found")
    source_text = "\n".join(clauses_by_id[clause_id].text for clause_id in requirement.clause_ids)
    terms = requirement.terms or [requirement.raw_requirement]
    if not any(normalized_contains(source_text, term) for term in terms):
        raise JobServiceError("job_requirement_not_grounded_in_clauses")


def _persist_requirements(
    session: Session,
    *,
    job_version: JobVersion,
    requirements: list[JobRequirementInput],
    require_text_grounding: bool = True,
) -> None:
    clauses_by_id = {clause.clause_id: clause for clause in job_version.clauses}
    used_keys: set[str] = set()
    generated_index = 1
    resolved_keys: list[str] = []
    for requirement in requirements:
        if require_text_grounding:
            _validate_requirement_clauses(
                requirement=requirement,
                clauses_by_id=clauses_by_id,
            )
        elif not set(requirement.clause_ids).issubset(clauses_by_id):
            raise JobServiceError("job_requirement_clause_not_found")
        key = requirement.requirement_key
        if key is None:
            while f"req-{generated_index:03d}" in used_keys:
                generated_index += 1
            key = f"req-{generated_index:03d}"
            generated_index += 1
        if key in used_keys:
            raise JobServiceError("job_requirement_keys_must_be_unique")
        used_keys.add(key)
        resolved_keys.append(key)

    session.execute(delete(JobRequirement).where(JobRequirement.job_version_id == job_version.id))
    weights = _allocate_weights(requirements)
    for sort_order, (requirement, key, weight) in enumerate(
        zip(requirements, resolved_keys, weights, strict=True)
    ):
        terms = [term.strip() for term in (requirement.terms or [requirement.raw_requirement])]
        session.add(
            JobRequirement(
                job_version_id=job_version.id,
                requirement_key=key,
                priority=requirement.priority,
                category=requirement.category,
                raw_requirement=requirement.raw_requirement.strip(),
                normalized_value={"terms": terms},
                minimum_months=(
                    requirement.minimum_months
                    if requirement.minimum_months is not None
                    else _months_from_text(requirement.raw_requirement)
                ),
                weight=weight,
                clause_ids=list(requirement.clause_ids),
                sort_order=sort_order,
            )
        )
    session.flush()
    # Keep the legacy top-level Job fields as a convenient cache of the latest
    # working version. Every scoreable/matchable operation still reads the
    # immutable JobVersion rows, never these cache fields.
    if job_version.job.version == job_version.version:
        job_version.job.title = job_version.title
        job_version.job.jd_text = job_version.raw_text
        job_version.job.requirements = {
            "must_have": [
                requirement.raw_requirement.strip()
                for requirement in requirements
                if requirement.priority == "must_have"
            ],
            "preferred": [
                requirement.raw_requirement.strip()
                for requirement in requirements
                if requirement.priority == "preferred"
            ],
        }
        session.flush()
    # `delete()` intentionally bypasses ORM relationship bookkeeping.  Expire
    # the collection so the response and later confirmation always read the
    # newly persisted requirement set, rather than a stale collection.
    session.expire(job_version, ["requirements"])


def _manual_requirements_from_create(
    *,
    payload: JobCreate,
    clauses: list[JobSourceClause],
) -> list[JobRequirementInput]:
    inputs: list[JobRequirementInput] = []
    for priority, raw_requirements in (
        ("must_have", payload.requirements.must_have),
        ("preferred", payload.requirements.preferred),
    ):
        for raw_requirement in raw_requirements:
            clause_ids = _grounded_clause_ids(clauses, raw_requirement=raw_requirement)
            if not clause_ids:
                raise JobServiceError("job_requirement_not_grounded_in_jd")
            inputs.append(
                JobRequirementInput(
                    priority=priority,
                    category="other",
                    raw_requirement=raw_requirement,
                    terms=[raw_requirement],
                    clause_ids=clause_ids,
                )
            )
    return inputs


def _create_version(
    session: Session,
    *,
    job: Job,
    payload: JobCreate,
    version_number: int,
) -> JobVersion:
    job_version = JobVersion(
        job_id=job.id,
        version=version_number,
        title=payload.title.strip(),
        raw_text=payload.jd_text.strip(),
        status="draft",
    )
    session.add(job_version)
    session.flush()
    clauses = _create_clauses(session, job_version=job_version)
    requirements = _manual_requirements_from_create(payload=payload, clauses=clauses)
    if requirements:
        _persist_requirements(
            session,
            job_version=job_version,
            requirements=requirements,
        )
        job_version.status = "confirmed"
        job_version.confirmed_at = _utcnow()
    session.flush()
    return job_version


def create_job(session: Session, *, payload: JobCreate) -> JobVersionResponse:
    job = Job(
        title=payload.title.strip(),
        jd_text=payload.jd_text.strip(),
        requirements=payload.requirements.model_dump(),
        version=1,
    )
    session.add(job)
    session.flush()
    job_version = _create_version(
        session,
        job=job,
        payload=payload,
        version_number=1,
    )
    return _version_response(job_version)


def publish_original_job(
    session: Session,
    *,
    payload: OriginalJobPublishRequest,
) -> JobVersionResponse:
    """Persist a source JD exactly as provided, without deriving requirements.

    An original JD is intentionally confirmed so it is available in the JD
    workspace immediately.  It has no structured requirements, however, and
    therefore cannot be used by the matching services.  This path must remain
    model-free: do not route it through JD generation or requirement extraction.
    """

    job = Job(
        title=payload.title,
        jd_text=payload.jd_text,
        requirements=JobRequirements().model_dump(),
        version=1,
    )
    session.add(job)
    session.flush()
    job_version = JobVersion(
        job_id=job.id,
        version=1,
        title=payload.title,
        raw_text=payload.jd_text,
        status="confirmed",
        confirmed_at=_utcnow(),
    )
    session.add(job_version)
    session.flush()
    _create_clauses(session, job_version=job_version)
    session.flush()
    return _version_response(job_version)


def generate_job_description(
    *,
    session: Session,
    payload: JobGenerationRequest,
    settings: AppSettings,
) -> JobGenerationResponse:
    """Generate a JD that can be persisted as a confirmed job in one follow-up call."""

    _require_ai_gateway_credentials(settings)
    api_key, model, timeout_seconds = _gateway_compatibility_credentials(settings)
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="jd_generate",
                business_ref_type="job_generation",
                # The generated JD intentionally has no durable Job row yet.
                # Never persist title/brief text as a ledger reference.
                business_ref_id=str(uuid4()),
                contract_version="jd_generation.v1",
            ),
        ):
            generated = generate_jd_from_brief(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                title=payload.title,
                brief=payload.brief,
            )
    except AiGatewayError as exc:
        raise JobServiceError(str(exc)) from exc
    return JobGenerationResponse.model_validate(generated)


def create_job_version(
    session: Session,
    *,
    job_id: str,
    payload: JobCreate,
) -> JobVersionResponse:
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError("job_not_found")
    next_version = (session.scalar(select(JobVersion.version).where(JobVersion.job_id == job.id).order_by(JobVersion.version.desc())) or 0) + 1
    job.title = payload.title.strip()
    job.jd_text = payload.jd_text.strip()
    job.requirements = payload.requirements.model_dump()
    job.version = next_version
    job_version = _create_version(
        session,
        job=job,
        payload=payload,
        version_number=next_version,
    )
    return _version_response(job_version)


def get_job_version(session: Session, *, job_version_id: str) -> JobVersionResponse:
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    return _version_response(job_version)


def get_latest_confirmed_job_version(session: Session) -> JobVersionResponse:
    """Return the newest confirmed JD that is actually matchable.

    Original-published JDs are deliberately confirmed with zero requirements.
    They remain available through ``list_confirmed_job_versions`` for display,
    but must not become the implicit/default target for the Agent or matching
    workflow.
    """

    job_version = session.scalar(
        select(JobVersion)
        .where(
            JobVersion.status == "confirmed",
            JobVersion.requirements.any(),
        )
        .order_by(JobVersion.confirmed_at.desc(), JobVersion.created_at.desc())
    )
    if job_version is None:
        raise JobVersionNotFoundError("confirmed_job_version_not_found")
    return _version_response(job_version)


def list_confirmed_job_versions(session: Session) -> list[JobVersionResponse]:
    """Return every confirmed JD that can be selected for matching.

    Jobs are independent records in the first release, so the selector needs
    a workspace-level list rather than the versions for one job only.
    """

    versions = session.scalars(
        select(JobVersion)
        .where(JobVersion.status == "confirmed")
        .order_by(JobVersion.confirmed_at.desc(), JobVersion.created_at.desc())
    ).all()
    return [_version_response(version) for version in versions]


def list_job_versions(session: Session, *, job_id: str) -> list[JobVersionResponse]:
    if session.get(Job, job_id) is None:
        raise JobNotFoundError("job_not_found")
    versions = session.scalars(
        select(JobVersion)
        .where(JobVersion.job_id == job_id)
        .order_by(JobVersion.version.desc())
    ).all()
    return [_version_response(version) for version in versions]


def extract_job_version_requirements(
    session: Session,
    *,
    job_version_id: str,
    settings: AppSettings,
) -> JobVersionResponse:
    _require_ai_gateway_credentials(settings)
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    if job_version.status != "draft":
        raise JobServiceError("confirmed_job_version_cannot_be_extracted")
    api_key, model, timeout_seconds = _gateway_compatibility_credentials(settings)
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="jd_requirements_extract",
                business_ref_type="job_version",
                business_ref_id=job_version.id,
                contract_version="jd_requirements.v1",
            ),
        ):
            provider_result = extract_jd_requirements_from_clauses(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                clauses=[
                    {"clause_id": clause.clause_id, "text": clause.text}
                    for clause in sorted(job_version.clauses, key=lambda item: item.ordinal)
                ],
            )
    except AiGatewayError as exc:
        raise JobServiceError(str(exc)) from exc
    extracted_requirements = [
        JobRequirementInput(
            requirement_key=requirement["requirement_id"],
            priority=requirement["priority"],
            category="other",
            raw_requirement=requirement["requirement_text"],
            terms=[requirement["requirement_text"]],
            clause_ids=requirement["clause_ids"],
        )
        for requirement in provider_result["requirements"]
    ]
    _persist_requirements(
        session,
        job_version=job_version,
        requirements=extracted_requirements,
        # The provider contract has already checked complete, exact clause-ID
        # grounding.  A requirement may be a faithful paraphrase, so requiring
        # its literal wording to appear in the clause would reject valid output.
        require_text_grounding=False,
    )
    session.flush()
    return _version_response(job_version)


def update_job_version_requirements(
    session: Session,
    *,
    job_version_id: str,
    payload: JobVersionRequirementsUpdate,
) -> JobVersionResponse:
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    if job_version.status != "draft":
        raise JobServiceError("confirmed_job_version_cannot_be_edited")
    job_version.title = payload.title.strip()
    _persist_requirements(
        session,
        job_version=job_version,
        requirements=payload.requirements,
    )
    session.flush()
    return _version_response(job_version)


def confirm_job_version(session: Session, *, job_version_id: str) -> JobVersionResponse:
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    if job_version.status != "draft":
        raise JobServiceError("job_version_not_draft")
    if not job_version.requirements:
        raise JobServiceError("job_version_requires_requirements_before_confirmation")
    if sum(requirement.weight for requirement in job_version.requirements) != 10000:
        raise JobServiceError("job_version_requirement_weights_invalid")
    job_version.status = "confirmed"
    job_version.confirmed_at = _utcnow()
    session.flush()
    return _version_response(job_version)


def _ready_resume_snapshot(
    session: Session,
    *,
    resume_id: str,
) -> tuple[Resume, ResumeFactSnapshot, dict[str, object]]:
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise JobServiceError("resume_not_found")
    if resume.extraction_status != "ready" or not resume.is_active:
        raise JobServiceError("resume_must_be_active_and_ready_for_job_match")
    if has_unreliable_source_text(resume.quality_flags):
        raise JobServiceError("resume_source_text_unreliable")
    snapshot = session.scalar(
        select(ResumeFactSnapshot)
        .where(ResumeFactSnapshot.resume_id == resume.id)
        .order_by(ResumeFactSnapshot.facts_version.desc())
    )
    if snapshot is None or snapshot.facts_version != resume.facts_version:
        raise JobServiceError("resume_fact_snapshot_not_current")
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except json.JSONDecodeError as exc:
        raise JobServiceError("resume_fact_snapshot_invalid") from exc
    if not isinstance(payload, dict):
        raise JobServiceError("resume_fact_snapshot_invalid")
    return resume, snapshot, payload


def derive_job_match_score(
    *,
    total_score: float,
    evidence_coverage: float | None,
) -> float:
    """Return the evidence-normalized JD match score on a 0–100 scale.

    `total_score` remains the old all-requirements score.  Since unknown
    requirements used to contribute zero there, normalize it by the percentage
    of requirements for which the model found enough evidence.  A resume with
    no confirmed evidence deliberately receives a score of zero instead of a
    division error or an artificial perfect score.
    """

    if evidence_coverage is None or evidence_coverage <= 0:
        return 0.0
    return round(total_score / evidence_coverage * 100, 2)


def classify_job_match_lane(
    *,
    hard_requirement_status: str | None,
    match_confidence: float | None,
) -> str:
    """Assign a match to one of the three HR review lanes.

    An explicit failed hard requirement is the only route to the last lane.
    An unproven hard requirement is *not* a rejection: it stays in the review
    lane even when other evidence coverage is high.
    """

    if hard_requirement_status == "unmet":
        return MATCH_LANE_UNMET
    if (
        hard_requirement_status in {"pass", "not_applicable"}
        and match_confidence is not None
        and match_confidence >= MATCH_CONFIDENCE_RECOMMENDED_THRESHOLD
    ):
        return MATCH_LANE_RECOMMENDED
    return MATCH_LANE_PENDING


def _job_match_ranking_key(job_match: JobMatch) -> tuple[int, float, float]:
    """Default order for the JD workspace's three candidate lanes."""

    match_confidence = job_match.evidence_coverage
    lane = classify_job_match_lane(
        hard_requirement_status=job_match.hard_requirement_status,
        match_confidence=match_confidence,
    )
    lane_rank = {
        MATCH_LANE_RECOMMENDED: 0,
        MATCH_LANE_PENDING: 1,
        MATCH_LANE_UNMET: 2,
    }[lane]
    return (
        lane_rank,
        -derive_job_match_score(
            total_score=job_match.total_score,
            evidence_coverage=match_confidence,
        ),
        -(match_confidence or 0.0),
    )


def _match_response(job_match: JobMatch) -> JobMatchResponse:
    match_confidence = job_match.evidence_coverage
    return JobMatchResponse(
        match_id=job_match.id,
        job_id=job_match.job_id,
        job_version_id=job_match.job_version_id,
        resume_id=job_match.resume_id,
        candidate_id=job_match.resume.candidate_id,
        candidate_display_name=(
            job_match.resume.candidate.display_name
            if job_match.resume.candidate is not None
            else None
        ),
        fact_snapshot_id=job_match.fact_snapshot_id,
        facts_version=job_match.facts_version,
        job_version=job_match.job_version,
        total_score=job_match.total_score,
        must_have_passed=job_match.must_have_passed,
        evidence_coverage=job_match.evidence_coverage,
        match_score=derive_job_match_score(
            total_score=job_match.total_score,
            evidence_coverage=match_confidence,
        ),
        match_confidence=match_confidence,
        match_lane=classify_job_match_lane(
            hard_requirement_status=job_match.hard_requirement_status,
            match_confidence=match_confidence,
        ),
        hard_requirement_status=job_match.hard_requirement_status,
        analysis=job_match.analysis or {},
        requirement_results=[
            JobMatchRequirementResponse(
                requirement_id=result.requirement_id,
                requirement_key=result.requirement.requirement_key,
                priority=result.requirement.priority,
                requirement_text=result.requirement.raw_requirement,
                clause_ids=result.requirement.clause_ids or [],
                outcome=result.outcome,
                reason=result.reason,
                fact_ids=result.fact_ids or [],
                missing_or_uncertain=result.missing_or_uncertain,
                score_contribution=result.score_contribution,
            )
            for result in sorted(
                job_match.requirement_results,
                key=lambda item: item.requirement.sort_order,
            )
        ],
        status=job_match.status,
        model_name=job_match.model_name,
        created_at=job_match.created_at.isoformat(),
    )


def run_job_match(
    session: Session,
    *,
    resume_id: str,
    payload: JobMatchCreate,
    settings: AppSettings,
    pinned_route_policy_version_id: str | None = None,
    ai_run_business_ref_type: str = "job_match",
    ai_run_business_ref_id: str | None = None,
) -> JobMatchResponse:
    _require_ai_gateway_credentials(settings)
    job_version = session.get(JobVersion, payload.job_version_id)
    if job_version is None:
        raise JobVersionNotFoundError("job_version_not_found")
    if job_version.status != "confirmed":
        raise JobServiceError("job_version_must_be_confirmed_for_matching")
    requirements = sorted(job_version.requirements, key=lambda item: item.sort_order)
    if not requirements:
        raise JobServiceError("job_version_has_no_requirements")
    resume, snapshot, fact_snapshot = _ready_resume_snapshot(session, resume_id=resume_id)
    api_key, model, timeout_seconds = _gateway_compatibility_credentials(settings)
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="jd_match",
                business_ref_type=ai_run_business_ref_type,
                business_ref_id=(
                    ai_run_business_ref_id
                    or f"{job_version.id}:{resume.id}:{snapshot.id}"
                ),
                contract_version="jd_match.v1",
                pinned_route_policy_version_id=pinned_route_policy_version_id,
            ),
        ):
            provider_result = match_resume_fact_snapshot_against_requirements(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                fact_snapshot=fact_snapshot,
                confirmed_requirements=[
                    {
                        "requirement_id": requirement.requirement_key,
                        "requirement_text": requirement.raw_requirement,
                        "priority": requirement.priority,
                        "clause_ids": requirement.clause_ids or [],
                    }
                    for requirement in requirements
                ],
            )
    except AiGatewayError as exc:
        raise JobServiceError(str(exc)) from exc
    result_by_key = {
        result["requirement_id"]: result
        for result in provider_result["requirement_matches"]
    }
    multiplier = {"met": 1.0, "partial": 0.5, "not_met": 0.0, "unknown": 0.0}
    total_score = 0.0
    known_weight = 0
    outcomes_by_priority: dict[str, list[str]] = {"must_have": [], "preferred": []}
    job_match = JobMatch(
        job_id=job_version.job_id,
        job_version_id=job_version.id,
        resume_id=resume.id,
        fact_snapshot_id=snapshot.id,
        facts_version=snapshot.facts_version,
        job_version=job_version.version,
        total_score=0,
        must_have_passed=None,
        evidence_coverage=0,
        hard_requirement_status=None,
        analysis={
            "schema_version": provider_result["schema_version"],
            "needs_human_review": provider_result["needs_human_review"],
            "decision": "advisory_only",
        },
        status="succeeded",
        # Keep the historical API field without coupling this result to the
        # legacy settings model.  The actual model is on its gateway run.
        model_name="gateway-managed",
    )
    session.add(job_match)
    session.flush()
    for requirement in requirements:
        result = result_by_key.get(requirement.requirement_key)
        if result is None:
            raise JobServiceError("job_match_provider_missing_requirement")
        outcome = result["status"]
        outcomes_by_priority[requirement.priority].append(outcome)
        contribution = requirement.weight / 10000 * multiplier[outcome] * 100
        total_score += contribution
        if outcome != "unknown":
            known_weight += requirement.weight
        session.add(
            JobMatchRequirementResult(
                job_match_id=job_match.id,
                requirement_id=requirement.id,
                outcome=outcome,
                reason=result["rationale"],
                fact_ids=result["fact_ids"],
                missing_or_uncertain=(
                    "; ".join(result["uncertainties"])
                    if result["uncertainties"]
                    else None
                ),
                score_contribution=round(contribution, 4),
            )
        )
    must_outcomes = outcomes_by_priority["must_have"]
    if not must_outcomes:
        hard_requirement_status = "not_applicable"
        must_have_passed: bool | None = None
    elif any(outcome == "not_met" for outcome in must_outcomes):
        hard_requirement_status = "unmet"
        must_have_passed: bool | None = False
    elif any(outcome == "unknown" for outcome in must_outcomes):
        hard_requirement_status = "information_insufficient"
        must_have_passed = None
    else:
        hard_requirement_status = "pass"
        must_have_passed = True
    job_match.total_score = round(total_score, 2)
    job_match.evidence_coverage = round(known_weight / 100, 2)
    job_match.hard_requirement_status = hard_requirement_status
    job_match.must_have_passed = must_have_passed
    if provider_result["needs_human_review"] or any(
        outcome == "unknown"
        for outcomes in outcomes_by_priority.values()
        for outcome in outcomes
    ):
        job_match.status = "needs_review"
    session.flush()
    return _match_response(job_match)


def get_job_match(session: Session, *, match_id: str) -> JobMatchResponse:
    job_match = session.get(JobMatch, match_id)
    if job_match is None:
        raise JobMatchNotFoundError("job_match_not_found")
    return _match_response(job_match)


def list_resume_job_matches(
    session: Session,
    *,
    resume_id: str,
) -> list[JobMatchResponse]:
    if session.get(Resume, resume_id) is None:
        raise JobServiceError("resume_not_found")
    matches = session.scalars(
        select(JobMatch)
        .where(JobMatch.resume_id == resume_id)
        .order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
    ).all()
    return [_match_response(match) for match in matches]


def list_job_version_matches(
    session: Session,
    *,
    job_version_id: str,
) -> list[JobMatchResponse]:
    if session.get(JobVersion, job_version_id) is None:
        raise JobVersionNotFoundError("job_version_not_found")
    matches = session.scalars(
        select(JobMatch)
        .where(JobMatch.job_version_id == job_version_id)
        .order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
    ).all()
    # Python sorting intentionally retains the query's newest-first order for
    # exact ties while making the three UI lanes deterministic.  This also
    # keeps derived score semantics out of the persisted schema.
    matches.sort(key=_job_match_ranking_key)
    return [_match_response(match) for match in matches]


__all__ = [
    "DeepSeekProviderError",
    "JobMatchNotFoundError",
    "JobNotFoundError",
    "JobServiceError",
    "JobVersionNotFoundError",
    "confirm_job_version",
    "classify_job_match_lane",
    "create_job",
    "create_job_version",
    "derive_job_match_score",
    "extract_job_version_requirements",
    "generate_job_description",
    "get_job_match",
    "get_latest_confirmed_job_version",
    "get_job_version",
    "list_confirmed_job_versions",
    "list_job_version_matches",
    "list_resume_job_matches",
    "list_job_versions",
    "publish_original_job",
    "run_job_match",
    "update_job_version_requirements",
]
