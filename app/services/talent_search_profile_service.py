"""Confirmation-first AI talent-search profiles.

The service deliberately separates three moments that used to be conflated by
the recruiting Agent: drafting a search plan, an HR confirming that plan, and
running deterministic recall plus evidence-grounded semantic matching.  A
browser cannot supply a candidate set or bypass a confirmed hard-filter
snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.models import (
    JobMatch,
    JobMatchBatch,
    JobMatchBatchItem,
    JobMatchRequirementResult,
    JobVersion,
    Resume,
    TalentSearchProfile,
    TalentSearchProfileRevision,
    TalentSearchRun,
)
from app.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    EducationFilter,
    JobMatchRequirementResponse,
    TalentSearchHardFilters,
    TalentSearchProfileConfirmRequest,
    TalentSearchProfileGenerateRequest,
    TalentSearchProfileRefineRequest,
    TalentSearchProfileResponse,
    TalentSearchProfileRevisionResponse,
    TalentSearchProfileRunRequest,
    TalentSearchProfileSearchRequest,
    TalentSearchProfileMatchResult,
    TalentSearchRecallDiagnosticStep,
    TalentSearchRecallDiagnostics,
    TalentSearchRunResponse,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
)
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    generate_talent_search_profile,
)
from app.services.job_match_batch_service import enqueue_job_version_match_batch
from app.services.job_service import (
    JobServiceError,
    classify_job_match_lane,
    create_talent_search_match_job_version,
    derive_job_match_score,
)
from app.services.search_service import SearchValidationError, search_candidates
from app.services.normalization import normalized_key


class TalentSearchProfileServiceError(RuntimeError):
    """Stable, non-sensitive profile workflow errors."""


class TalentSearchProfileNotFoundError(TalentSearchProfileServiceError):
    pass


_DEGREE_LABELS = {
    "vocational_or_below": "中专/职高及以下",
    "high_school": "高中",
    "associate": "大专",
    "bachelor": "本科",
    "master": "硕士",
    "doctor": "博士",
    "unknown": "待识别",
}
_INSTITUTION_CLASSIFICATION_LABELS = {
    "985": "985",
    "211": "211",
    "undergraduate": "本科院校",
    "associate": "大专院校",
    "secondary_vocational": "中专院校",
    "overseas": "海外院校",
}
_EXPERIENCE_TYPE_LABELS = {
    "employment": "正式工作",
    "internship": "实习",
    "project": "项目",
    "research": "科研",
    "competition": "技能竞赛",
    "campus": "校内/学生组织",
    "club": "社团",
    "volunteer": "志愿活动/社会实践",
    "entrepreneurship": "创业",
    "training": "培训",
    "other": "其他经历",
    "unknown": "待识别经历",
}
_PROJECT_EVIDENCE_MARKERS = (
    "项目",
    "实践",
    "落地",
    "实习",
    "工作经历",
    "工作职责",
    "职责",
    "科研",
    "研究",
    "竞赛",
    "project",
    "projects",
    "projectexperience",
    "practicalexperience",
    "practice",
    "internship",
    "workexperience",
    "workhistory",
    "responsibility",
    "responsibilities",
    "research",
    "competition",
    "delivery",
    "shipped",
    "built",
)
_EXPERIENCE_POLICY_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("项目", "project"),
    ("project", "project"),
    ("projects", "project"),
    ("落地", "project"),
    ("交付", "project"),
    ("delivery", "project"),
    ("shipped", "project"),
    ("built", "project"),
    ("实习", "internship"),
    ("internship", "internship"),
    ("工作经历", "employment"),
    ("工作职责", "employment"),
    ("正式工作", "employment"),
    ("employment", "employment"),
    ("workexperience", "employment"),
    ("workhistory", "employment"),
    ("科研", "research"),
    ("研究", "research"),
    ("research", "research"),
    ("竞赛", "competition"),
    ("competition", "competition"),
)
_PRACTICAL_EXPERIENCE_MARKERS = (
    "实践",
    "实战",
    "practicalexperience",
    "practice",
)
_EXPERIENCE_POLICY_TYPE_ORDER = (
    "project",
    "internship",
    "employment",
    "research",
    "competition",
)
_EXPERIENCE_POLICY_TYPE_LABELS = {
    "project": "项目",
    "internship": "实习",
    "employment": "工作",
    "research": "科研",
    "competition": "竞赛",
}
_BACHELOR_INSTITUTION_MARKERS = (
    "本科院校",
    "普通本科",
    "本科高校",
    "本科毕业于",
    "本科学校",
    "本科就读于",
)
_BACHELOR_NEGATION_OR_RANGE_MARKERS = (
    "不要本科",
    "非本科",
    "不是本科",
    "排除本科",
    "不招本科",
    "拒绝本科",
    "本科及以下",
    "本科以下",
)
_OTHER_DEGREE_MARKERS = (
    "硕士",
    "博士",
    "研究生",
    "大专",
    "专科",
    "中专",
    "高中",
    "职高",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_ai_gateway_credentials(settings: AppSettings) -> None:
    if not ai_gateway_credentials_configured(settings):
        raise TalentSearchProfileServiceError("deepseek_api_key_not_configured")


def _message_requests_project_evidence(message: str, *, term: str) -> bool:
    """Return whether one explicit term is requested as practical experience.

    This is intentionally a narrow guard, not a hidden semantic taxonomy.  It
    only corrects an unsafe AI draft when the recruiter's own wording places a
    named technology close to an experience marker such as “项目” or “实习”.
    """

    message_key = normalized_key(message)
    term_key = normalized_key(term)
    if not message_key or not term_key:
        return False
    start = 0
    while True:
        index = message_key.find(term_key, start)
        if index < 0:
            return False
        window = message_key[max(0, index - 18) : index + len(term_key) + 18]
        if any(marker in window for marker in _PROJECT_EVIDENCE_MARKERS):
            return True
        start = index + len(term_key)


def _experience_types_for_explicit_term(message: str, *, term: str) -> list[str]:
    """Compile only the recruiter's stated acceptable experience contexts.

    This policy is deliberately derived from the same narrow window that
    proved the named term is an experience request.  ``research`` and
    ``competition`` remain valid only when the recruiter explicitly names
    them; they never become a substitute for a requested project, internship,
    or employment record.
    """

    message_key = normalized_key(message)
    term_key = normalized_key(term)
    if not message_key or not term_key:
        return ["project", "internship", "employment"]
    found_types: set[str] = set()
    found_practical_marker = False
    start = 0
    while True:
        index = message_key.find(term_key, start)
        if index < 0:
            break
        window = message_key[max(0, index - 36) : index + len(term_key) + 36]
        for marker, experience_type in _EXPERIENCE_POLICY_TYPE_MARKERS:
            if marker in window:
                found_types.add(experience_type)
        if any(marker in window for marker in _PRACTICAL_EXPERIENCE_MARKERS):
            found_practical_marker = True
        start = index + len(term_key)
    if not found_types and found_practical_marker:
        found_types.update({"project", "internship", "employment"})
    if not found_types:
        # The caller has already established that the request is practical
        # experience. Preserve recall for wording such as "真实实战" while
        # keeping research and contests out unless they were named.
        found_types.update({"project", "internship", "employment"})
    return [
        experience_type
        for experience_type in _EXPERIENCE_POLICY_TYPE_ORDER
        if experience_type in found_types
    ]


def _experience_detail_policy(*, term: str, allowed_types: list[str]) -> dict[str, object]:
    return {
        "kind": "experience_detail_terms",
        "allowed_experience_types": allowed_types,
        "terms_all_of": [term],
    }


def _ensure_project_verification_requirement(
    requirements: list[object],
    *,
    term: str,
    allowed_types: list[str],
) -> list[object]:
    """Keep an explicit project-practice requirement visible to HR.

    The provider normally produces this itself.  This fallback only applies
    when the provider incorrectly put a project-experience term in the exact
    skill checklist, so removing that strict filter never silently removes the
    recruiter's stated requirement.
    """

    term_key = normalized_key(term)
    for index, value in enumerate(requirements):
        if not isinstance(value, Mapping):
            continue
        text = " ".join(
            str(value.get(field, ""))
            for field in ("label", "evidence_hint")
        )
        if term_key and term_key in normalized_key(text):
            updated = dict(value)
            # The source request is more precise than a provider draft. Keep
            # its visible wording, but make the required evidence executable
            # so a skill-only fact cannot later become a hard pass.
            updated["evidence_policy"] = _experience_detail_policy(
                term=term,
                allowed_types=allowed_types,
            )
            return [*requirements[:index], updated, *requirements[index + 1 :]]

    used_keys = {
        str(value.get("key"))
        for value in requirements
        if isinstance(value, Mapping) and isinstance(value.get("key"), str)
    }
    suffix = 1
    while f"project_evidence_{suffix}" in used_keys:
        suffix += 1
    experience_label = "、".join(
        _EXPERIENCE_POLICY_TYPE_LABELS.get(value, value)
        for value in allowed_types
    )
    return [
        *requirements,
        {
            "key": f"project_evidence_{suffix}",
            "label": f"具备 {term} 的{experience_label}实践",
            "evidence_hint": (
                f"核验{experience_label}经历的名称、职责或结果中是否明确、正向使用 {term}，"
                "以及候选人的具体实现、贡献或结果。"
            ),
            "evidence_policy": _experience_detail_policy(
                term=term,
                allowed_types=allowed_types,
            ),
        },
    ]


def _request_unambiguously_requires_any_bachelor_degree(message_key: str) -> bool:
    """Whether “本科” means any bachelor record, rather than a different rule.

    This deliberately refuses to reinterpret mixed conditions.  For example,
    “硕士及以上、本科毕业于 985” needs both the higher-degree filter and the
    school requirement the model generated; converting it to “any bachelor”
    would lose the recruiter's actual bar.
    """

    if "本科" not in message_key:
        return False
    if any(marker in message_key for marker in _BACHELOR_NEGATION_OR_RANGE_MARKERS):
        return False
    if "本科及以上" in message_key or "本科以上" in message_key:
        return False
    if "最高学历" in message_key:
        return False
    if any(marker in message_key for marker in _BACHELOR_INSTITUTION_MARKERS):
        return False
    if any(marker in message_key for marker in _OTHER_DEGREE_MARKERS):
        return False
    return True


def _normalize_explicit_profile_intent(
    generated: Mapping[str, object],
    *,
    request_message: str,
) -> dict[str, object]:
    """Correct a few unambiguous recruiter phrases before persisting a draft.

    The profile model is asked to use the right fields, but this small
    deterministic guard prevents two costly failures when it does not: treating
    “本科毕业” as an exact highest-degree filter, and treating a named project
    technology as an exact skill tag.  It never invents a new requirement; it
    only preserves the recruiter's explicit wording in the appropriate,
    visible profile section.
    """

    normalized = dict(generated)
    try:
        hard_filters = TalentSearchHardFilters.model_validate(
            generated.get("hard_filters", {})
        )
    except ValueError:
        # The provider validator normally catches this before the service sees
        # it.  Keep the service defensive for test doubles and future routes.
        return normalized

    message_key = normalized_key(request_message)
    hard_values = hard_filters.model_dump(mode="json")
    has_bachelor_negation_or_range = any(
        marker in message_key for marker in _BACHELOR_NEGATION_OR_RANGE_MARKERS
    )
    if (
        not has_bachelor_negation_or_range
        and ("本科及以上" in message_key or "本科以上" in message_key)
    ):
        hard_values["education_degree_in"] = []
        hard_values["highest_degree_in"] = ["bachelor", "master", "doctor"]
    elif (
        not has_bachelor_negation_or_range
        and "最高学历" in message_key
        and "本科" in message_key
    ):
        hard_values["education_degree_in"] = []
        hard_values["highest_degree_in"] = ["bachelor"]
    elif (
        _request_unambiguously_requires_any_bachelor_degree(message_key)
        and hard_values["highest_degree_in"] in ([], ["bachelor"])
    ):
        hard_values["education_degree_in"] = ["bachelor"]
        hard_values["highest_degree_in"] = []

    verification_requirements = list(
        generated.get("verification_requirements", [])
        if isinstance(generated.get("verification_requirements"), list)
        else []
    )
    exact_skill_terms = list(hard_values.get("skills_all_of", []))
    project_terms = [
        term
        for term in exact_skill_terms
        if isinstance(term, str)
        and _message_requests_project_evidence(request_message, term=term)
    ]
    if project_terms:
        hard_values["skills_all_of"] = [
            term for term in exact_skill_terms if term not in project_terms
        ]
        for term in project_terms:
            verification_requirements = _ensure_project_verification_requirement(
                verification_requirements,
                term=term,
                allowed_types=_experience_types_for_explicit_term(
                    request_message,
                    term=term,
                ),
            )

    try:
        normalized["hard_filters"] = TalentSearchHardFilters.model_validate(
            hard_values
        ).model_dump(mode="json")
    except ValueError:
        return dict(generated)
    normalized["verification_requirements"] = verification_requirements
    return normalized


def _current_revision(
    session: Session,
    *,
    profile: TalentSearchProfile,
) -> TalentSearchProfileRevision:
    revision = session.scalar(
        select(TalentSearchProfileRevision).where(
            TalentSearchProfileRevision.profile_id == profile.id,
            TalentSearchProfileRevision.revision_number
            == profile.current_revision_number,
        )
    )
    if revision is None:
        raise TalentSearchProfileServiceError("talent_search_profile_revision_missing")
    return revision


def _current_displayed_revision(
    session: Session,
    *,
    profile: TalentSearchProfile,
    expected_revision_id: str,
) -> TalentSearchProfileRevision:
    """Return only the revision the recruiter explicitly reviewed.

    A profile can be open in more than one browser tab.  Without this check,
    one tab could confirm or run a newer AI revision created by another tab.
    """

    revision = _current_revision(session, profile=profile)
    if revision.id != expected_revision_id:
        raise TalentSearchProfileServiceError("talent_search_profile_revision_not_current")
    return revision


def _profile_or_not_found(
    session: Session,
    *,
    profile_id: str,
    for_update: bool = False,
) -> TalentSearchProfile:
    statement = select(TalentSearchProfile).where(TalentSearchProfile.id == profile_id)
    if for_update:
        # Postgres serializes confirm/refine/start for one profile. SQLite
        # safely ignores this clause in local tests, while the explicit
        # revision token still protects stale browser actions everywhere.
        statement = statement.with_for_update()
    profile = session.scalar(statement)
    if profile is None:
        # Keep tenant and object existence indistinguishable to callers.
        raise TalentSearchProfileNotFoundError("talent_search_profile_not_found")
    return profile


def _revision_response(
    revision: TalentSearchProfileRevision,
) -> TalentSearchProfileRevisionResponse:
    try:
        hard_filters = TalentSearchHardFilters.model_validate(revision.hard_filters or {})
    except ValueError as exc:  # Defensive guard for historical malformed rows.
        raise TalentSearchProfileServiceError("talent_search_profile_revision_invalid") from exc
    return TalentSearchProfileRevisionResponse(
        revision_id=revision.id,
        revision_number=revision.revision_number,
        source=revision.source,
        status=revision.status,
        title=revision.title,
        summary=revision.summary,
        hard_filters=hard_filters,
        verification_requirements=revision.verification_requirements or [],
        preferred_requirements=revision.preferred_requirements or [],
        aliases=revision.aliases or [],
        clarifying_questions=revision.clarifying_questions or [],
        created_at=revision.created_at.isoformat(),
        confirmed_at=(revision.confirmed_at.isoformat() if revision.confirmed_at else None),
    )


def _profile_response(
    session: Session,
    *,
    profile: TalentSearchProfile,
) -> TalentSearchProfileResponse:
    return TalentSearchProfileResponse(
        profile_id=profile.id,
        source_type=profile.source_type,
        source_job_version_id=profile.source_job_version_id,
        original_request=profile.original_request,
        status=profile.status,
        current_revision=_revision_response(_current_revision(session, profile=profile)),
        created_at=profile.created_at.isoformat(),
        updated_at=profile.updated_at.isoformat(),
    )


def _normal_source_job_version(
    session: Session,
    *,
    job_version_id: str | None,
) -> JobVersion | None:
    if job_version_id is None:
        return None
    job_version = session.get(JobVersion, job_version_id)
    if job_version is None or job_version.job.kind != "job":
        raise TalentSearchProfileNotFoundError("job_version_not_found")
    return job_version


def _generate_profile_payload(
    session: Session,
    *,
    settings: AppSettings,
    profile_id: str,
    actor_user_id: str | None,
    request_message: str,
    source_job_version: JobVersion | None,
    previous_revision: TalentSearchProfileRevision | None,
) -> dict[str, object]:
    _require_ai_gateway_credentials(settings)
    api_key, model, timeout_seconds = gateway_prompt_transport_arguments(settings)
    previous_profile: dict[str, object] | None = None
    if previous_revision is not None:
        previous_profile = {
            "title": previous_revision.title,
            "summary": previous_revision.summary,
            "hard_filters": previous_revision.hard_filters or {},
            "verification_requirements": previous_revision.verification_requirements or [],
            "preferred_requirements": previous_revision.preferred_requirements or [],
            "aliases": previous_revision.aliases or [],
            "clarifying_questions": previous_revision.clarifying_questions or [],
        }
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="talent_search_profile",
                business_ref_type="talent_search_profile",
                business_ref_id=profile_id,
                actor_user_id=actor_user_id,
                contract_version="talent_search_profile.v1",
            ),
        ):
            generated = generate_talent_search_profile(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                request_message=request_message,
                source_job_text=(source_job_version.raw_text if source_job_version else None),
                previous_profile=previous_profile,
            )
            return _normalize_explicit_profile_intent(
                generated,
                request_message=request_message,
            )
    except AiGatewayError as exc:
        raise TalentSearchProfileServiceError(str(exc)) from exc


def _new_revision(
    *,
    profile: TalentSearchProfile,
    source: str,
    revision_number: int,
    generated: Mapping[str, object],
) -> TalentSearchProfileRevision:
    return TalentSearchProfileRevision(
        profile_id=profile.id,
        revision_number=revision_number,
        source=source,
        status="draft",
        title=str(generated["title"]),
        summary=str(generated["summary"]),
        hard_filters=dict(generated["hard_filters"]),
        verification_requirements=list(generated["verification_requirements"]),
        preferred_requirements=list(generated["preferred_requirements"]),
        aliases=list(generated["aliases"]),
        clarifying_questions=list(generated["clarifying_questions"]),
        model_name="gateway-managed",
    )


def generate_profile(
    session: Session,
    *,
    payload: TalentSearchProfileGenerateRequest,
    settings: AppSettings,
    actor_user_id: str | None,
) -> TalentSearchProfileResponse:
    """Generate a draft only. No recall, no candidate access, no matching."""

    source_job_version = _normal_source_job_version(
        session,
        job_version_id=payload.job_version_id,
    )
    profile_id = str(uuid4())
    generated = _generate_profile_payload(
        session,
        settings=settings,
        profile_id=profile_id,
        actor_user_id=actor_user_id,
        request_message=payload.message,
        source_job_version=source_job_version,
        previous_revision=None,
    )
    profile = TalentSearchProfile(
        id=profile_id,
        title=str(generated["title"]),
        source_type="job" if source_job_version is not None else "freeform",
        source_job_version_id=(source_job_version.id if source_job_version else None),
        original_request=payload.message,
        status="draft",
        current_revision_number=1,
        created_by_user_id=actor_user_id,
    )
    session.add(profile)
    revision = _new_revision(
        profile=profile,
        source="ai_generated",
        revision_number=1,
        generated=generated,
    )
    session.add(revision)
    session.flush()
    return _profile_response(session, profile=profile)


def refine_profile(
    session: Session,
    *,
    profile_id: str,
    payload: TalentSearchProfileRefineRequest,
    settings: AppSettings,
    actor_user_id: str | None,
) -> TalentSearchProfileResponse:
    """Create a new draft revision without overwriting HR's prior draft."""

    profile = _profile_or_not_found(session, profile_id=profile_id, for_update=True)
    current = _current_displayed_revision(
        session,
        profile=profile,
        expected_revision_id=payload.revision_id,
    )
    if current.status == "superseded":
        raise TalentSearchProfileServiceError("talent_search_profile_revision_superseded")
    source_job_version = _normal_source_job_version(
        session,
        job_version_id=profile.source_job_version_id,
    )
    generated = _generate_profile_payload(
        session,
        settings=settings,
        profile_id=profile.id,
        actor_user_id=actor_user_id,
        request_message=payload.message,
        source_job_version=source_job_version,
        previous_revision=current,
    )
    current.status = "superseded"
    profile.status = "draft"
    profile.current_revision_number += 1
    profile.title = str(generated["title"])
    revision = _new_revision(
        profile=profile,
        source="ai_refined",
        revision_number=profile.current_revision_number,
        generated=generated,
    )
    session.add(revision)
    session.flush()
    return _profile_response(session, profile=profile)


def confirm_profile(
    session: Session,
    *,
    profile_id: str,
    payload: TalentSearchProfileConfirmRequest,
    actor_user_id: str | None,
) -> TalentSearchProfileResponse:
    """Freeze the current draft and create its private semantic-match target."""

    profile = _profile_or_not_found(session, profile_id=profile_id, for_update=True)
    revision = _current_displayed_revision(
        session,
        profile=profile,
        expected_revision_id=payload.revision_id,
    )
    if revision.status != "draft" or profile.status != "draft":
        raise TalentSearchProfileServiceError("talent_search_profile_not_draft")
    match_version = create_talent_search_match_job_version(
        session,
        title=revision.title,
        verification_requirements=revision.verification_requirements or [],
        preferred_requirements=revision.preferred_requirements or [],
    )
    revision.match_job_version_id = match_version.id if match_version else None
    revision.status = "confirmed"
    revision.confirmed_at = _utcnow()
    revision.confirmed_by_user_id = actor_user_id
    profile.status = "confirmed"
    profile.confirmed_revision_number = revision.revision_number
    session.flush()
    return _profile_response(session, profile=profile)


def _search_request_from_hard_filters(
    hard_filters: TalentSearchHardFilters,
    *,
    limit: int,
    cursor: str | None,
    included_filter_keys: set[str] | None = None,
) -> CandidateSearchRequest:
    """Compile one recruiter-visible profile snapshot into deterministic recall.

    ``included_filter_keys`` is used only to construct the server-side
    zero-result funnel.  ``None`` means the complete confirmed profile.  The
    browser never controls either the snapshot or this subset.
    """

    def include(key: str) -> bool:
        return included_filter_keys is None or key in included_filter_keys

    education_any_of = (
        [
            EducationFilter(
                institution_classifications_any_of=(
                    hard_filters.institution_classifications_any_of
                )
            )
        ]
        if (
            include("institution_classifications_any_of")
            and hard_filters.institution_classifications_any_of
        )
        else []
    )
    return CandidateSearchRequest(
        education_degree_in=(
            hard_filters.education_degree_in
            if include("education_degree_in")
            else []
        ),
        highest_degree_in=(
            hard_filters.highest_degree_in
            if include("highest_degree_in")
            else []
        ),
        graduation_status=(
            hard_filters.graduation_status
            if include("graduation_status")
            else "any"
        ),
        fresh_graduate_start_month=(
            hard_filters.fresh_graduate_start_month
            if include("graduation_status")
            else None
        ),
        fresh_graduate_end_month=(
            hard_filters.fresh_graduate_end_month
            if include("graduation_status")
            else None
        ),
        min_employment_months=(
            hard_filters.min_employment_months
            if include("min_employment_months")
            else None
        ),
        min_employment_or_internship_months=(
            hard_filters.min_employment_or_internship_months
            if include("min_employment_or_internship_months")
            else None
        ),
        experience_types_all_of=(
            hard_filters.experience_types_all_of
            if include("experience_types_all_of")
            else []
        ),
        education_any_of=education_any_of,
        skills_all_of=(
            hard_filters.skills_all_of if include("skills_all_of") else []
        ),
        language_credentials_all_of=(
            hard_filters.language_credentials_all_of
            if include("language_credentials_all_of")
            else []
        ),
        limit=limit,
        cursor=cursor,
    )


def _recall_filter_steps(
    hard_filters: TalentSearchHardFilters,
) -> list[tuple[str, str]]:
    """Return the stable, recruiter-readable strict-recall order."""

    steps: list[tuple[str, str]] = []
    if hard_filters.institution_classifications_any_of:
        labels = [
            _INSTITUTION_CLASSIFICATION_LABELS.get(value, value)
            for value in hard_filters.institution_classifications_any_of
        ]
        steps.append(("institution_classifications_any_of", f"院校类型：{' / '.join(labels)}（任一）"))
    if hard_filters.education_degree_in:
        labels = [
            _DEGREE_LABELS.get(value, value)
            for value in hard_filters.education_degree_in
        ]
        steps.append(("education_degree_in", f"教育经历：含{' / '.join(labels)}（任一）"))
    if hard_filters.highest_degree_in:
        labels = [
            _DEGREE_LABELS.get(value, value)
            for value in hard_filters.highest_degree_in
        ]
        steps.append(("highest_degree_in", f"最高学历：{' / '.join(labels)}（任一）"))
    if hard_filters.graduation_status != "any":
        status_label = "应届" if hard_filters.graduation_status == "fresh" else "往届"
        steps.append(("graduation_status", f"毕业状态：{status_label}"))
    if hard_filters.min_employment_months is not None:
        steps.append(
            (
                "min_employment_months",
                f"正式工作不少于 {hard_filters.min_employment_months} 个月",
            )
        )
    if hard_filters.min_employment_or_internship_months is not None:
        steps.append(
            (
                "min_employment_or_internship_months",
                "工作加实习不少于 "
                f"{hard_filters.min_employment_or_internship_months} 个月",
            )
        )
    if hard_filters.experience_types_all_of:
        labels = [
            _EXPERIENCE_TYPE_LABELS.get(value, value)
            for value in hard_filters.experience_types_all_of
        ]
        steps.append(("experience_types_all_of", f"经历：{' + '.join(labels)}（全部）"))
    if hard_filters.skills_all_of:
        steps.append(("skills_all_of", f"精确技能：{'、'.join(hard_filters.skills_all_of)}（全部）"))
    if hard_filters.language_credentials_all_of:
        labels = [
            item.custom_name_contains or item.credential_code.upper()
            for item in hard_filters.language_credentials_all_of
        ]
        steps.append(("language_credentials_all_of", f"证书：{'、'.join(labels)}（全部）"))
    return steps


def _build_zero_result_diagnostics(
    session: Session,
    *,
    hard_filters: TalentSearchHardFilters,
) -> TalentSearchRecallDiagnostics:
    """Build and persist an honest funnel only when strict recall is zero."""

    baseline = search_candidates(
        session,
        _search_request_from_hard_filters(
            hard_filters,
            limit=1,
            cursor=None,
            included_filter_keys=set(),
        ),
    )
    previous_count = baseline.total_count
    active_keys: set[str] = set()
    steps: list[TalentSearchRecallDiagnosticStep] = []
    for key, label in _recall_filter_steps(hard_filters):
        active_keys.add(key)
        result = search_candidates(
            session,
            _search_request_from_hard_filters(
                hard_filters,
                limit=1,
                cursor=None,
                included_filter_keys=active_keys,
            ),
        )
        remaining_count = result.total_count
        steps.append(
            TalentSearchRecallDiagnosticStep(
                key=key,
                label=label,
                remaining_count=remaining_count,
                removed_count=max(previous_count - remaining_count, 0),
            )
        )
        previous_count = remaining_count
    return TalentSearchRecallDiagnostics(
        eligible_resume_count=baseline.total_count,
        needs_review_count=baseline.needs_review_count,
        strict_match_count=previous_count,
        steps=steps,
    )


def _recall_all_matching_resume_ids(
    session: Session,
    *,
    hard_filters: TalentSearchHardFilters,
) -> list[str]:
    cursor: str | None = None
    recalled: list[str] = []
    seen: set[str] = set()
    while True:
        response = search_candidates(
            session,
            _search_request_from_hard_filters(
                hard_filters,
                limit=100,
                cursor=cursor,
            ),
        )
        for item in response.items:
            if item.resume_id not in seen:
                seen.add(item.resume_id)
                recalled.append(item.resume_id)
        if response.next_cursor is None:
            return recalled
        cursor = response.next_cursor


def _candidate_recall_response(
    session: Session,
    *,
    run: TalentSearchRun,
    limit: int,
    cursor: str | None,
) -> CandidateSearchResponse:
    try:
        hard_filters = TalentSearchHardFilters.model_validate(run.hard_filter_snapshot or {})
    except ValueError as exc:
        raise TalentSearchProfileServiceError("talent_search_run_snapshot_invalid") from exc
    try:
        return search_candidates(
            session,
            _search_request_from_hard_filters(
                hard_filters,
                limit=limit,
                cursor=cursor,
            ),
            resume_ids=set(run.recalled_resume_ids or []),
        )
    except SearchValidationError as exc:
        # A cursor is only pagination state; never leak low-level parser
        # details or turn a bad browser token into a 500.
        raise TalentSearchProfileServiceError("talent_search_profile_invalid_cursor") from exc


def _run_status(run: TalentSearchRun, batch: JobMatchBatch | None) -> str:
    if batch is not None and batch.status in {"queued", "running", "completed", "partial"}:
        return batch.status
    return run.status if run.status in {"queued", "running", "completed", "partial"} else "completed"


def _profile_match_result(match: JobMatch) -> TalentSearchProfileMatchResult:
    """Serialize an internal match without leaking its hidden JD carrier."""

    confidence = match.evidence_coverage
    return TalentSearchProfileMatchResult(
        match_id=match.id,
        resume_id=match.resume_id,
        candidate_id=match.resume.candidate_id,
        candidate_display_name=(
            match.resume.candidate.display_name
            if match.resume.candidate is not None
            else None
        ),
        facts_version=match.facts_version,
        match_score=derive_job_match_score(
            total_score=match.total_score,
            evidence_coverage=confidence,
        ),
        match_confidence=confidence,
        match_lane=classify_job_match_lane(
            hard_requirement_status=match.hard_requirement_status,
            match_confidence=confidence,
        ),
        hard_requirement_status=match.hard_requirement_status,
        analysis=match.analysis or {},
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
                match.requirement_results,
                key=lambda item: item.requirement.sort_order,
            )
        ],
        status=match.status,
        created_at=match.created_at.isoformat(),
    )


def _profile_match_results(
    session: Session,
    *,
    batch: JobMatchBatch | None,
) -> list[TalentSearchProfileMatchResult]:
    """Return only successfully materialized results from this one batch."""

    if batch is None:
        return []
    items = session.scalars(
        select(JobMatchBatchItem)
        .where(JobMatchBatchItem.batch_id == batch.id)
        .options(
            selectinload(JobMatchBatchItem.job_match)
            .selectinload(JobMatch.resume)
            .selectinload(Resume.candidate),
            selectinload(JobMatchBatchItem.job_match)
            .selectinload(JobMatch.requirement_results)
            .selectinload(JobMatchRequirementResult.requirement),
        )
    ).all()
    matches = [item.job_match for item in items if item.job_match is not None]
    matches.sort(
        key=lambda match: (
            {"recommended": 0, "pending": 1, "unmet": 2}[
                classify_job_match_lane(
                    hard_requirement_status=match.hard_requirement_status,
                    match_confidence=match.evidence_coverage,
                )
            ],
            -derive_job_match_score(
                total_score=match.total_score,
                evidence_coverage=match.evidence_coverage,
            ),
            -(match.evidence_coverage or 0.0),
            match.id,
        )
    )
    return [_profile_match_result(match) for match in matches]


def _run_hard_filter_snapshot(run: TalentSearchRun) -> TalentSearchHardFilters:
    try:
        return TalentSearchHardFilters.model_validate(run.hard_filter_snapshot or {})
    except ValueError as exc:
        raise TalentSearchProfileServiceError("talent_search_run_snapshot_invalid") from exc


def _run_recall_diagnostics(
    run: TalentSearchRun,
) -> TalentSearchRecallDiagnostics | None:
    if not run.recall_diagnostics:
        return None
    try:
        return TalentSearchRecallDiagnostics.model_validate(run.recall_diagnostics)
    except ValueError as exc:
        raise TalentSearchProfileServiceError(
            "talent_search_run_diagnostics_invalid"
        ) from exc


def _run_response(
    session: Session,
    *,
    run: TalentSearchRun,
    limit: int,
    cursor: str | None,
) -> TalentSearchRunResponse:
    batch = session.get(JobMatchBatch, run.job_match_batch_id) if run.job_match_batch_id else None
    hard_filters = _run_hard_filter_snapshot(run)
    return TalentSearchRunResponse(
        run_id=run.id,
        profile_id=run.profile_id,
        revision_id=run.revision_id,
        status=_run_status(run, batch),
        total_recalled_count=run.total_recalled_count,
        job_match_batch_id=run.job_match_batch_id,
        match_total_count=batch.total_count if batch is not None else 0,
        match_completed_count=batch.completed_count if batch is not None else 0,
        match_failed_count=batch.failed_count if batch is not None else 0,
        match_results=_profile_match_results(session, batch=batch),
        created_at=run.created_at.isoformat(),
        updated_at=run.updated_at.isoformat(),
        applied_hard_filters=hard_filters,
        recall_diagnostics=_run_recall_diagnostics(run),
        candidate_recall=_candidate_recall_response(
            session,
            run=run,
            limit=limit,
            cursor=cursor,
        ),
    )


def _existing_run_for_revision(
    session: Session,
    *,
    revision: TalentSearchProfileRevision,
) -> TalentSearchRun | None:
    runs = session.scalars(
        select(TalentSearchRun)
        .where(
            TalentSearchRun.revision_id == revision.id,
        )
        .order_by(TalentSearchRun.created_at.desc())
    ).all()
    for run in runs:
        batch = session.get(JobMatchBatch, run.job_match_batch_id) if run.job_match_batch_id else None
        status = _run_status(run, batch)
        if run.status != status:
            run.status = status
        # A confirmed revision has one durable search execution. Returning a
        # completed run as well prevents a double click or page reload from
        # silently spending another N×M AI batch. A future explicit rerun can
        # create a new revision instead of mutating this audit record.
        return run
    return None


def start_profile_search(
    session: Session,
    *,
    profile_id: str,
    payload: TalentSearchProfileRunRequest,
    settings: AppSettings,
) -> TalentSearchRunResponse:
    """Recall from the frozen profile, then queue semantic matching for that set."""

    profile = _profile_or_not_found(session, profile_id=profile_id, for_update=True)
    revision = _current_displayed_revision(
        session,
        profile=profile,
        expected_revision_id=payload.revision_id,
    )
    if (
        profile.status != "confirmed"
        or revision.status != "confirmed"
        or profile.confirmed_revision_number != revision.revision_number
    ):
        raise TalentSearchProfileServiceError("talent_search_profile_not_confirmed")
    existing_run = _existing_run_for_revision(session, revision=revision)
    if existing_run is not None:
        return _run_response(
            session,
            run=existing_run,
            limit=payload.limit,
            cursor=payload.cursor,
        )
    try:
        hard_filters = TalentSearchHardFilters.model_validate(revision.hard_filters or {})
    except ValueError as exc:
        raise TalentSearchProfileServiceError("talent_search_profile_revision_invalid") from exc
    recalled_resume_ids = _recall_all_matching_resume_ids(
        session,
        hard_filters=hard_filters,
    )
    recall_diagnostics = (
        _build_zero_result_diagnostics(session, hard_filters=hard_filters)
        if not recalled_resume_ids
        else None
    )
    run = TalentSearchRun(
        profile_id=profile.id,
        revision_id=revision.id,
        hard_filter_snapshot=hard_filters.model_dump(mode="json"),
        recall_diagnostics=(
            recall_diagnostics.model_dump(mode="json")
            if recall_diagnostics is not None
            else {}
        ),
        recalled_resume_ids=recalled_resume_ids,
        status="completed",
        total_recalled_count=len(recalled_resume_ids),
    )
    session.add(run)
    session.flush()
    if revision.match_job_version_id and recalled_resume_ids:
        match_version = session.get(JobVersion, revision.match_job_version_id)
        if (
            match_version is None
            or match_version.organization_id != profile.organization_id
            or match_version.job.kind != "talent_search_profile"
        ):
            raise TalentSearchProfileServiceError("talent_search_profile_match_target_invalid")
        batch = enqueue_job_version_match_batch(
            session,
            job_version_id=revision.match_job_version_id,
            settings=settings,
            resume_ids=recalled_resume_ids,
            allow_internal_job=True,
        )
        run.job_match_batch_id = batch.batch_id
        run.status = batch.status
    session.flush()
    return _run_response(
        session,
        run=run,
        limit=payload.limit,
        cursor=payload.cursor,
    )


def get_profile(session: Session, *, profile_id: str) -> TalentSearchProfileResponse:
    return _profile_response(session, profile=_profile_or_not_found(session, profile_id=profile_id))


def list_profiles(
    session: Session,
    *,
    limit: int = 12,
) -> list[TalentSearchProfileResponse]:
    """Return recent profile drafts/runs for drawer recovery after reload."""

    profiles = session.scalars(
        select(TalentSearchProfile)
        .order_by(TalentSearchProfile.updated_at.desc(), TalentSearchProfile.id.desc())
        .limit(limit)
    ).all()
    return [_profile_response(session, profile=profile) for profile in profiles]


def get_profile_run(
    session: Session,
    *,
    profile_id: str,
    run_id: str,
    payload: TalentSearchProfileSearchRequest,
) -> TalentSearchRunResponse:
    _profile_or_not_found(session, profile_id=profile_id)
    run = session.get(TalentSearchRun, run_id)
    if run is None or run.profile_id != profile_id:
        raise TalentSearchProfileNotFoundError("talent_search_run_not_found")
    return _run_response(session, run=run, limit=payload.limit, cursor=payload.cursor)


__all__ = [
    "DeepSeekProviderError",
    "JobServiceError",
    "TalentSearchProfileNotFoundError",
    "TalentSearchProfileServiceError",
    "confirm_profile",
    "generate_profile",
    "get_profile",
    "get_profile_run",
    "list_profiles",
    "refine_profile",
    "start_profile_search",
]
