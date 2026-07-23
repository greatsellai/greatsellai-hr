from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.filter_options import (
    AWARD_LEVEL_OPTIONS,
    LANGUAGE_CREDENTIAL_ALIASES,
    language_credential_label,
    normalize_language_credential,
)
from app.models import (
    Candidate,
    Resume,
    ResumeLanguageCredential,
    ResumeScore,
    ResumeSummary,
    ScoreTemplate,
)
from app.schemas import (
    CandidateSearchDisplayField,
    CandidateSearchItem,
    CandidateSearchMatch,
    CandidateSearchRequest,
    CandidateSearchResponse,
    EducationFilter,
    ExperienceFilter,
    LeadershipFilter,
    LanguageCredentialFilter,
)
from app.services.institution_service import (
    INSTITUTION_CLASSIFICATION_ORDER,
    resolve_institution,
)
from app.services.normalization import DEGREE_RANK, normalized_contains, normalized_key
from app.services.resume_eligibility import is_resume_screening_eligible


# A score that needs a recruiter review is still the latest usable AI score.  Hiding
# it from search/library results makes newly completed batch work look as if no
# score exists at all.
_CURRENT_SCORE_STATUSES = {"succeeded", "needs_review", "overridden"}
_SUMMARY_PREVIEW_MAX_CHARS = 180
_AMBIGUOUS_LANGUAGE_SOURCE_ALIASES = {
    normalized_key("四级"),
    normalized_key("六级"),
}
_LANGUAGE_MENTION_NEGATION_HINTS = tuple(
    normalized_key(value)
    for value in (
        "未通过",
        "未过",
        "未取得",
        "未获得",
        "备考",
        "备考中",
        "准备",
        "待考",
        "待出",
        "未出",
        "未达到",
    )
)
_DISPLAY_FIELD_ORDER = (
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
)


def _summary_preview(summary: ResumeSummary | None) -> str | None:
    """Return one compact, recruiter-safe line from the current summary."""

    if summary is None or not isinstance(summary.content, dict):
        return None
    sections = summary.content.get("sections")
    if not isinstance(sections, dict):
        return None
    for key in (
        "candidate_positioning",
        "work_and_internship",
        "core_skills",
        "strengths",
    ):
        value = sections.get(key)
        rendered = value.get("content") if isinstance(value, dict) else value
        if not isinstance(rendered, str) or not rendered.strip():
            continue
        normalized = " ".join(rendered.split())
        if len(normalized) <= _SUMMARY_PREVIEW_MAX_CHARS:
            return normalized
        return f"{normalized[: _SUMMARY_PREVIEW_MAX_CHARS - 1].rstrip()}…"
    return None


def _current_summary(resume: Resume) -> ResumeSummary | None:
    candidates = [
        summary
        for summary in resume.summaries
        if summary.is_current
        and summary.status == "succeeded"
        and summary.facts_version == resume.facts_version
    ]
    return max(candidates, key=lambda item: (item.created_at, item.id), default=None)


def _latest_score(
    resume: Resume,
    *,
    template_id: str | None = None,
    template_version: int | None = None,
) -> ResumeScore | None:
    candidates = [
        score
        for score in resume.scores
        if score.facts_version == resume.facts_version
        and score.status in _CURRENT_SCORE_STATUSES
        and (template_id is None or score.template_id == template_id)
        and (template_version is None or score.template_version == template_version)
    ]
    return max(candidates, key=lambda item: (item.created_at, item.id), default=None)


@dataclass
class _DisplayFieldValues:
    values: list[str]
    evidence_block_ids: list[str]


def _add_display_field(
    fields: dict[str, _DisplayFieldValues],
    *,
    key: str,
    values: list[str | None],
    evidence_block_ids: list[str] | None,
) -> None:
    """Accumulate compact table values without losing their source blocks."""

    field = fields.setdefault(key, _DisplayFieldValues([], []))
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = " ".join(value.split())
        if normalized and normalized not in field.values:
            field.values.append(normalized)
    for block_id in evidence_block_ids or []:
        if block_id and block_id not in field.evidence_block_ids:
            field.evidence_block_ids.append(block_id)


def _display_fields(
    fields: dict[str, _DisplayFieldValues],
) -> list[CandidateSearchDisplayField]:
    return [
        CandidateSearchDisplayField(
            key=key,
            values=fields[key].values,
            evidence_block_ids=fields[key].evidence_block_ids,
        )
        for key in _DISPLAY_FIELD_ORDER
        if key in fields
    ]


def _award_level_label(value: str | None) -> str | None:
    if not value:
        return None
    return next(
        (item["label"] for item in AWARD_LEVEL_OPTIONS if item["value"] == value),
        value,
    )


def _language_display_label(credential: ResumeLanguageCredential) -> str:
    """Prefer the actual source name for an explicitly custom credential."""

    if credential.credential_code == "custom":
        custom_name = (credential.credential_name_raw or "").strip()
        if custom_name:
            return custom_name
    return language_credential_label(credential.credential_code)


def _matches_any_text(value: str | None, expected_values: list[str]) -> bool:
    return not expected_values or any(
        normalized_contains(value, expected) for expected in expected_values
    )


def _matches_school_name(
    session: Session,
    education: object,
    expected_values: list[str],
) -> bool:
    if not expected_values:
        return True
    for value in expected_values:
        institution = resolve_institution(session, value)
        if institution is not None and education.institution_id == institution.id:
            return True
        if institution is None and normalized_contains(education.school_name_raw, value):
            return True
    return False


def _matches_education(
    session: Session,
    filter_item: EducationFilter,
    education: object,
) -> bool:
    degree_in = filter_item.degree_in
    return (
        (not degree_in or education.degree in degree_in)
        and _matches_school_name(session, education, filter_item.school_name_contains)
        and _matches_any_text(education.major_raw, filter_item.major_contains)
        and (
            not filter_item.institution_classifications_any_of
            or education.institution_classification
            in filter_item.institution_classifications_any_of
        )
        and (
            not filter_item.institution_tiers_any_of
            or bool(
                set(education.institution_tiers or [])
                & set(filter_item.institution_tiers_any_of)
            )
        )
        and (
            filter_item.min_average_score is None
            or (
                education.average_score is not None
                and education.average_score >= filter_item.min_average_score
            )
        )
        and (
            filter_item.min_gpa_percent is None
            or (
                education.gpa_percent is not None
                and education.gpa_percent >= filter_item.min_gpa_percent
            )
        )
        and (
            filter_item.max_rank_position is None
            or (
                education.rank_position is not None
                and education.rank_position <= filter_item.max_rank_position
            )
        )
        and (
            filter_item.max_rank_percent is None
            or (
                education.rank_percent is not None
                and education.rank_percent <= filter_item.max_rank_percent
            )
        )
    )


def _resume_institution_classifications(resume: Resume) -> list[str]:
    """Return distinct per-record categories in one stable UI/API order."""

    known = {
        education.institution_classification
        for education in resume.educations
        if education.institution_classification
    }
    return [
        classification
        for classification in INSTITUTION_CLASSIFICATION_ORDER
        if classification in known
    ]


def _education_institution_classifications(education: object) -> list[str]:
    """Return only the new exact classification, never legacy tier aliases."""

    classification = getattr(education, "institution_classification", None)
    if classification in INSTITUTION_CLASSIFICATION_ORDER:
        return [classification]
    return []


def _matches_experience(filter_item: ExperienceFilter, experience: object) -> bool:
    return (
        experience.experience_type in filter_item.experience_types
        and _matches_any_text(
            experience.experience_name_raw,
            filter_item.experience_name_contains,
        )
        and _matches_any_text(
            experience.organization_name_raw,
            filter_item.organization_name_contains,
        )
        and _matches_any_text(experience.title_raw, filter_item.title_contains)
        and (
            not filter_item.leadership_contexts_any_of
            or experience.leadership_context in filter_item.leadership_contexts_any_of
        )
        and (
            not filter_item.leadership_roles_any_of
            or _matches_any_text(
                experience.leadership_role,
                filter_item.leadership_roles_any_of,
            )
        )
        and (
            not filter_item.award_levels_any_of
            or experience.award_level in filter_item.award_levels_any_of
        )
        and _matches_any_text(
            experience.award_result_raw,
            filter_item.award_result_contains,
        )
    )


def _highest_education(resume: Resume) -> object | None:
    return max(
        resume.educations,
        key=lambda item: (
            DEGREE_RANK.get(item.degree, 0),
            item.end_month or "",
            item.id,
        ),
        default=None,
    )


def _latest_relevant_experience(resume: Resume) -> object | None:
    """Return the most recent job, or internship when no job is present.

    A recruiter-facing table needs one compact experience label.  Only formal
    work and internships belong there, so projects and competitions cannot
    accidentally look like an employer or current role.
    """

    relevant = [
        experience
        for experience in resume.experiences
        if experience.experience_type in {"employment", "internship"}
    ]
    return max(
        relevant,
        key=lambda item: (
            item.experience_type == "employment",
            bool(item.is_current),
            item.end_month or "",
            item.start_month or "",
            item.id,
        ),
        default=None,
    )


def _score_confidence(score: ResumeScore | None) -> float | None:
    """Return the share of score weight supported by cited resume facts.

    This is intentionally named confidence in the API/UI, but it measures
    evidence coverage only.  A low value means information needs checking; it
    never means the candidate is weak.
    """

    if score is None or not isinstance(score.dimension_scores, list):
        return None

    total_weight = 0.0
    grounded_weight = 0.0
    for record in score.dimension_scores:
        if not isinstance(record, dict):
            continue
        try:
            weight = max(float(record.get("weight", 0)), 0.0)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        total_weight += weight
        evidence_state = record.get("evidence_state")
        # Older immutable rows predate the explicit state.  Their cited fact
        # ids are still the only safe evidence signal available to the table.
        grounded = evidence_state == "grounded" or (
            evidence_state is None and bool(record.get("fact_ids"))
        )
        if grounded:
            grounded_weight += weight

    if total_weight <= 0:
        return None
    return round(grounded_weight / total_weight * 100, 1)


def _matches_graduation_status(resume: Resume, request: CandidateSearchRequest) -> bool:
    if request.graduation_status == "any":
        return True
    education = _highest_education(resume)
    if education is None or not education.end_month:
        return False
    if request.graduation_status == "fresh":
        return bool(
            request.fresh_graduate_start_month
            <= education.end_month
            <= request.fresh_graduate_end_month
        )
    return bool(education.end_month < request.fresh_graduate_start_month)


def _matches_language_filter(
    filter_item: LanguageCredentialFilter,
    credential: object,
) -> bool:
    if credential.credential_code != filter_item.credential_code:
        return False
    if filter_item.custom_name_contains and not normalized_contains(
        credential.credential_name_raw,
        filter_item.custom_name_contains,
    ):
        return False
    return filter_item.min_score is None or (
        credential.score is not None and credential.score >= filter_item.min_score
    )


@dataclass(frozen=True)
class _LanguageEvidenceMatch:
    """One language condition backed by either a fact or direct resume text."""

    label: str
    evidence_block_ids: list[str]
    evidence_origin: str


def _language_aliases_for_language_filter(
    filter_item: LanguageCredentialFilter,
) -> tuple[str, ...]:
    """Return conservative aliases for direct, page-grounded evidence."""

    if filter_item.credential_code == "custom":
        return (
            (filter_item.custom_name_contains or "").strip(),
        ) if filter_item.custom_name_contains else ()
    return tuple(
        alias
        for alias in LANGUAGE_CREDENTIAL_ALIASES.get(
            filter_item.credential_code,
            (),
        )
        if normalized_key(alias) not in _AMBIGUOUS_LANGUAGE_SOURCE_ALIASES
    )


def _source_aliases_for_language_filter(
    filter_item: LanguageCredentialFilter,
) -> tuple[str, ...]:
    """Return aliases eligible for Agent-only source-text fallback.

    A source-only mention cannot safely prove a requested minimum score.  The
    single-word aliases ``四级`` and ``六级`` are deliberately excluded because
    they also occur in professional-English credentials such as ``专四``.
    """

    if filter_item.min_score is not None:
        return ()
    return _language_aliases_for_language_filter(filter_item)


def _source_language_alias_state(
    source_text: str,
    aliases: tuple[str, ...],
) -> tuple[bool, bool]:
    """Return ``(positive_mention, negative_mention)`` for one credential."""

    normalized_source = normalized_key(source_text)
    positive_mention = False
    negative_mention = False
    for alias in aliases:
        alias_key = normalized_key(alias)
        if not alias_key:
            continue
        start = normalized_source.find(alias_key)
        while start >= 0:
            end = start + len(alias_key)
            context = normalized_source[max(0, start - 12) : end + 12]
            if any(hint in context for hint in _LANGUAGE_MENTION_NEGATION_HINTS):
                negative_mention = True
            else:
                positive_mention = True
            start = normalized_source.find(alias_key, start + len(alias_key))
    return positive_mention, negative_mention


def _source_mentions_language_alias(
    source_text: str,
    aliases: tuple[str, ...],
) -> bool:
    """Match a direct resume mention while rejecting obvious negative context."""

    return _source_language_alias_state(source_text, aliases)[0]


def _source_has_negative_language_alias(
    source_text: str,
    aliases: tuple[str, ...],
) -> bool:
    """Identify a clear not-passed or in-progress mention for one credential."""

    return _source_language_alias_state(source_text, aliases)[1]


def _source_language_evidence_block_ids(
    resume: Resume,
    filter_item: LanguageCredentialFilter,
) -> list[str]:
    aliases = _source_aliases_for_language_filter(filter_item)
    if not aliases:
        return []
    return sorted(
        block.block_id
        for block in resume.source_blocks
        if _source_mentions_language_alias(block.text, aliases)
    )


def _credential_has_negative_source_context(
    resume: Resume,
    filter_item: LanguageCredentialFilter,
    credential: ResumeLanguageCredential,
) -> bool:
    """Keep an unqualified extracted row from becoming a passed credential.

    Some extraction runs identify a credential name but leave ``passed`` null.
    If the source block grounding that row clearly says it is being prepared
    for or was not passed, the Agent must leave it unconfirmed.
    """

    evidence_block_ids = set(credential.evidence_block_ids or [])
    aliases = _language_aliases_for_language_filter(filter_item)
    if not evidence_block_ids or not aliases:
        return False
    return any(
        _source_has_negative_language_alias(block.text, aliases)
        for block in resume.source_blocks
        if block.block_id in evidence_block_ids
    )


def _matching_language_evidence(
    resume: Resume,
    filters: list[LanguageCredentialFilter],
    *,
    include_source_language_evidence: bool,
) -> list[_LanguageEvidenceMatch]:
    """Resolve language conditions without changing the public hard-filter API.

    The normal recruiter table continues to use only extracted credential
    facts.  The bounded Agent can opt into direct, page-grounded source text
    when an extractor missed a clearly stated credential.  Neither path writes
    a new fact or changes a resume's extraction state.
    """

    matches: list[_LanguageEvidenceMatch] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for filter_item in filters:
        # An extracted explicit ``passed=False`` is stronger than a loose
        # source-text alias.  Do not let the fallback reverse that known
        # negative into a recruiter-facing confirmation.
        has_explicit_not_passed = any(
            _matches_language_filter(filter_item, credential)
            and credential.passed is False
            for credential in resume.language_credentials
        )
        structured_matches = [
            credential
            for credential in resume.language_credentials
            if _matches_language_filter(filter_item, credential)
            and (
                not include_source_language_evidence
                or (
                    credential.passed is not False
                    and bool(credential.evidence_block_ids)
                    and not (
                        credential.passed is None
                        and _credential_has_negative_source_context(
                            resume,
                            filter_item,
                            credential,
                        )
                    )
                )
            )
        ]
        for credential in structured_matches:
            evidence_ids = tuple(sorted(credential.evidence_block_ids or []))
            key = (
                _language_display_label(credential),
                "structured_fact",
                evidence_ids,
            )
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                _LanguageEvidenceMatch(
                    label=(
                        _language_display_label(credential)
                        + (
                            f" {credential.score:g}"
                            if credential.score is not None
                            else ""
                        )
                    ),
                    evidence_block_ids=list(evidence_ids),
                    evidence_origin="structured_fact",
                )
            )
        if (
            structured_matches
            or has_explicit_not_passed
            or not include_source_language_evidence
        ):
            continue

        source_ids = _source_language_evidence_block_ids(resume, filter_item)
        if not source_ids:
            continue
        label = language_credential_label(filter_item.credential_code)
        if filter_item.credential_code == "custom" and filter_item.custom_name_contains:
            label = filter_item.custom_name_contains
        key = (label, "resume_text", tuple(source_ids))
        if key in seen:
            continue
        seen.add(key)
        matches.append(
            _LanguageEvidenceMatch(
                label=label,
                evidence_block_ids=source_ids,
                evidence_origin="resume_text",
            )
        )
    return matches


def _matches_leadership(filter_item: LeadershipFilter, experience: object) -> bool:
    return bool(
        experience.leadership_role
        and (
            not filter_item.contexts_any_of
            or experience.leadership_context in filter_item.contexts_any_of
        )
        and (
            not filter_item.roles_any_of
            or _matches_any_text(experience.leadership_role, filter_item.roles_any_of)
        )
    )


class SearchValidationError(ValueError):
    pass


@dataclass(frozen=True)
class _SearchResult:
    item: CandidateSearchItem
    updated_at: datetime


def _encode_cursor(*, updated_at: datetime, resume_id: str) -> str:
    payload = json.dumps(
        {"updated_at": updated_at.isoformat(), "resume_id": resume_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw_payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw_payload)
        updated_at = datetime.fromisoformat(payload["updated_at"])
        resume_id = payload["resume_id"]
    except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise SearchValidationError("invalid_cursor") from exc
    if not isinstance(resume_id, str) or len(resume_id) != 36:
        raise SearchValidationError("invalid_cursor")
    return updated_at, resume_id


def _matches_keywords(resume: Resume, *, all_of: list[str], any_of: list[str]) -> bool:
    if not all_of and not any_of:
        return True
    source_text = "\n".join(block.text for block in resume.source_blocks)
    normalized_source = normalized_key(source_text)
    all_keys = {normalized_key(value) for value in all_of}
    any_keys = {normalized_key(value) for value in any_of}
    return (
        (not all_keys or all(key in normalized_source for key in all_keys))
        and (not any_keys or any(key in normalized_source for key in any_keys))
    )


def _matches_v2_keywords(resume: Resume, *, keywords: list[str], mode: str) -> bool:
    if not keywords:
        return True
    source = normalized_key("\n".join(block.text for block in resume.source_blocks))
    if mode == "precise":
        return all(normalized_key(keyword) in source for keyword in keywords)
    credential_codes = {
        credential.credential_code for credential in resume.language_credentials
    }
    for keyword in keywords:
        credential_code = normalize_language_credential(keyword)
        if credential_code is not None:
            if credential_code in credential_codes:
                return True
            continue
        if normalized_key(keyword) in source:
            return True
    return False


def _matching_keyword_block_ids(resume: Resume, keywords: list[str]) -> list[str]:
    keys = {normalized_key(keyword) for keyword in keywords}
    return sorted(
        block.block_id
        for block in resume.source_blocks
        if any(key in normalized_key(block.text) for key in keys)
    )


def _matching_v2_keyword_block_ids(
    resume: Resume,
    *,
    keywords: list[str],
    mode: str,
) -> list[str]:
    if mode == "precise":
        keyword_keys = {normalized_key(keyword) for keyword in keywords}
        return sorted(
            block.block_id
            for block in resume.source_blocks
            if any(key in normalized_key(block.text) for key in keyword_keys)
        )

    matched_ids: set[str] = set()
    source_keywords: list[str] = []
    for keyword in keywords:
        credential_code = normalize_language_credential(keyword)
        if credential_code is None:
            source_keywords.append(keyword)
            continue
        matched_ids.update(
            block_id
            for credential in resume.language_credentials
            if credential.credential_code == credential_code
            for block_id in (credential.evidence_block_ids or [])
        )
    source_keys = {normalized_key(keyword) for keyword in source_keywords}
    matched_ids.update(
        block.block_id
        for block in resume.source_blocks
        if any(key in normalized_key(block.text) for key in source_keys)
    )
    return sorted(matched_ids)


def search_candidates(
    session: Session,
    request: CandidateSearchRequest,
    *,
    include_source_language_evidence: bool = False,
) -> CandidateSearchResponse:
    """Search one workspace's eligible resumes.

    ``include_source_language_evidence`` is reserved for the bounded
    recruiting Agent.  It lets the Agent treat a direct, reliable resume-text
    mention as a recruiter-confirmed self-report when structured extraction
    missed the same language credential.  Public filter endpoints intentionally
    retain their existing extracted-fact-only contract.
    """

    score_template: ScoreTemplate | None = None
    if request.score_template_id is not None:
        score_template = session.get(ScoreTemplate, request.score_template_id)
        if score_template is None or score_template.is_archived:
            raise SearchValidationError("score_template_not_found")

    statement = (
        select(Resume)
        .join(Resume.candidate)
        .options(
            selectinload(Resume.educations),
            selectinload(Resume.experiences),
            selectinload(Resume.skills),
            selectinload(Resume.language_credentials),
            selectinload(Resume.scholarships),
            selectinload(Resume.source_blocks),
            selectinload(Resume.candidate),
            selectinload(Resume.summaries),
            selectinload(Resume.scores).selectinload(ResumeScore.template),
        )
        .where(Resume.is_active.is_(True), Resume.extraction_status == "ready")
        .order_by(Resume.updated_at.desc(), Resume.id.desc())
    )
    if request.is_985_211 is not None:
        statement = statement.where(Resume.is_985_211.is_(request.is_985_211))
    if request.highest_degree_in:
        statement = statement.where(Resume.highest_degree.in_(request.highest_degree_in))
    if request.min_employment_months is not None:
        statement = statement.where(
            Resume.employment_months >= request.min_employment_months
        )
    if request.min_employment_or_internship_months is not None:
        statement = statement.where(
            Resume.employment_or_internship_months
            >= request.min_employment_or_internship_months
        )
    # Filtering already evaluates the candidate facts in Python so that one
    # education/experience record must satisfy a compound condition.  Keep the
    # cursor out of the SQL statement, then apply it after the same score-first
    # ordering used by the recruiter table is known.
    cursor_resume_id: str | None = None
    if request.cursor is not None:
        _, cursor_resume_id = _decode_cursor(request.cursor)

    results: list[_SearchResult] = []
    for resume in session.scalars(statement).all():
        # ``ready`` only means structured facts were persisted.  It does not
        # override a parser warning that the source text itself was garbled.
        # Do not let that version enter the recruiter-facing screening index.
        if not is_resume_screening_eligible(resume):
            continue
        matched_filters: list[str] = []
        matched_evidence: list[CandidateSearchMatch] = []
        display_field_values: dict[str, _DisplayFieldValues] = {}

        if not _matches_graduation_status(resume, request):
            continue

        if request.highest_degree_in:
            matched_filters.append("highest_degree_in")
            highest_education = _highest_education(resume)
            highest_education_block_ids = (
                highest_education.evidence_block_ids
                if highest_education is not None
                else []
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="highest_degree_in",
                    label=resume.highest_degree or "",
                    fact_type="education",
                    evidence_block_ids=highest_education_block_ids,
                )
            )
            _add_display_field(
                display_field_values,
                key="highest_degree",
                values=[resume.highest_degree],
                evidence_block_ids=highest_education_block_ids,
            )

        if request.graduation_status != "any":
            highest_education = _highest_education(resume)
            highest_education_block_ids = (
                highest_education.evidence_block_ids
                if highest_education is not None
                else []
            )
            matched_filters.append("graduation_status")
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="graduation_status",
                    label=(
                        f"{request.graduation_status}: "
                        f"{highest_education.end_month if highest_education else ''}"
                    ),
                    fact_type="education",
                    evidence_block_ids=highest_education_block_ids,
                )
            )
            _add_display_field(
                display_field_values,
                key="graduation",
                values=[highest_education.end_month if highest_education else None],
                evidence_block_ids=highest_education_block_ids,
            )

        if request.is_985_211 is not None:
            matched_filters.append(f"is_985_211={str(request.is_985_211).lower()}")
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="is_985_211",
                    label=f"985/211={str(request.is_985_211).lower()}",
                    fact_type="aggregate",
                    evidence_block_ids=[],
                )
            )
            _add_display_field(
                display_field_values,
                key="institution_classifications",
                values=_resume_institution_classifications(resume),
                evidence_block_ids=[],
            )
        if request.min_employment_months is not None:
            matched_filters.append(
                f"employment_months>={request.min_employment_months}"
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="min_employment_months",
                    label=f"employment_months>={request.min_employment_months}",
                    fact_type="aggregate",
                    evidence_block_ids=[],
                )
            )
            _add_display_field(
                display_field_values,
                key="employment_months",
                values=[str(resume.employment_months)],
                evidence_block_ids=[],
            )
        if request.min_employment_or_internship_months is not None:
            matched_filters.append(
                "employment_or_internship_months"
                f">={request.min_employment_or_internship_months}"
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="min_employment_or_internship_months",
                    label=(
                        "employment_or_internship_months"
                        f">={request.min_employment_or_internship_months}"
                    ),
                    fact_type="aggregate",
                    evidence_block_ids=[],
                )
            )
            _add_display_field(
                display_field_values,
                key="employment_or_internship_months",
                values=[str(resume.employment_or_internship_months)],
                evidence_block_ids=[],
            )

        if request.education_any_of:
            matching_education_pairs = [
                (filter_item, education)
                for filter_item in request.education_any_of
                for education in resume.educations
                if _matches_education(session, filter_item, education)
            ]
            if not matching_education_pairs:
                continue
            matching_education = [
                education for _, education in matching_education_pairs
            ]
            matched_filters.append("education")
            for filter_item, education in matching_education_pairs:
                classification_block_ids = (
                    education.classification_evidence_block_ids
                    or education.evidence_block_ids
                    or []
                )
                education_block_ids = education.evidence_block_ids or []
                if filter_item.school_name_contains:
                    _add_display_field(
                        display_field_values,
                        key="school",
                        values=[education.school_name_raw],
                        evidence_block_ids=education_block_ids,
                    )
                if filter_item.major_contains:
                    _add_display_field(
                        display_field_values,
                        key="major",
                        values=[education.major_raw],
                        evidence_block_ids=education_block_ids,
                    )
                if (
                    filter_item.institution_classifications_any_of
                    or filter_item.institution_tiers_any_of
                ):
                    _add_display_field(
                        display_field_values,
                        key="institution_classifications",
                        values=_education_institution_classifications(education),
                        evidence_block_ids=classification_block_ids,
                    )
                if filter_item.degree_in:
                    _add_display_field(
                        display_field_values,
                        key="education_degree",
                        values=[education.degree],
                        evidence_block_ids=education_block_ids,
                    )
                academic_values: list[str | None] = []
                if (
                    filter_item.min_average_score is not None
                    and education.average_score is not None
                ):
                    academic_values.append(f"平均分 {education.average_score:g}")
                if (
                    filter_item.min_gpa_percent is not None
                    and education.gpa_percent is not None
                ):
                    if education.gpa_value is not None and education.gpa_scale is not None:
                        academic_values.append(
                            "GPA "
                            f"{education.gpa_value:g}/{education.gpa_scale:g} "
                            f"({education.gpa_percent:g}%)"
                        )
                    else:
                        academic_values.append(f"GPA {education.gpa_percent:g}%")
                if (
                    filter_item.max_rank_position is not None
                    and education.rank_position is not None
                ):
                    academic_values.append(
                        "排名 "
                        f"{education.rank_position}"
                        + (
                            f"/{education.rank_total}"
                            if education.rank_total is not None
                            else ""
                        )
                    )
                if (
                    filter_item.max_rank_percent is not None
                    and education.rank_percent is not None
                ):
                    academic_values.append(f"排名前 {education.rank_percent:g}%")
                if academic_values:
                    _add_display_field(
                        display_field_values,
                        key="academic_performance",
                        values=academic_values,
                        evidence_block_ids=education_block_ids,
                    )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="education",
                    label=" | ".join(
                        value
                        for value in (
                            education.school_name_raw,
                            education.institution_classification,
                            education.degree,
                            education.major_raw,
                        )
                        if value
                    ),
                    fact_type="education",
                    evidence_block_ids=(
                        education.evidence_block_ids
                        or education.classification_evidence_block_ids
                        or []
                    ),
                )
                for education in matching_education
            )

        if request.experience_any_of:
            matching_experience_pairs = [
                (filter_item, experience)
                for filter_item in request.experience_any_of
                for experience in resume.experiences
                if _matches_experience(filter_item, experience)
            ]
            if not matching_experience_pairs:
                continue
            matching_experience = [
                experience for _, experience in matching_experience_pairs
            ]
            matched_filters.append("experience")
            for filter_item, experience in matching_experience_pairs:
                experience_block_ids = experience.evidence_block_ids or []
                classification_block_ids = (
                    experience.classification_evidence_block_ids
                    or experience_block_ids
                )
                if filter_item.experience_types:
                    _add_display_field(
                        display_field_values,
                        key="experience_type",
                        values=[experience.experience_type],
                        evidence_block_ids=classification_block_ids,
                    )
                if filter_item.experience_name_contains:
                    _add_display_field(
                        display_field_values,
                        key="experience_name",
                        values=[experience.experience_name_raw],
                        evidence_block_ids=experience_block_ids,
                    )
                if filter_item.organization_name_contains:
                    _add_display_field(
                        display_field_values,
                        key="organization",
                        values=[experience.organization_name_raw],
                        evidence_block_ids=experience_block_ids,
                    )
                if filter_item.title_contains:
                    _add_display_field(
                        display_field_values,
                        key="title",
                        values=[experience.title_raw],
                        evidence_block_ids=experience_block_ids,
                    )
                if filter_item.award_result_contains:
                    _add_display_field(
                        display_field_values,
                        key="experience_award",
                        values=[experience.award_result_raw],
                        evidence_block_ids=experience_block_ids,
                    )
                if filter_item.award_levels_any_of:
                    _add_display_field(
                        display_field_values,
                        key="experience_award",
                        values=[_award_level_label(experience.award_level)],
                        evidence_block_ids=classification_block_ids,
                    )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="experience",
                    label=" | ".join(
                        value
                        for value in (
                            experience.organization_name_raw,
                            experience.title_raw,
                            experience.experience_type,
                        )
                        if value
                    ),
                    fact_type="experience",
                    evidence_block_ids=experience.evidence_block_ids or [],
                )
                for experience in matching_experience
            )

        if request.skill_categories_any_of:
            matching_category_skills = [
                skill
                for skill in resume.skills
                if skill.skill_category in request.skill_categories_any_of
            ]
            if not matching_category_skills:
                continue
            matched_filters.append("skill_categories_any_of")
            _add_display_field(
                display_field_values,
                key="skills",
                values=[skill.skill_display for skill in matching_category_skills],
                evidence_block_ids=[
                    block_id
                    for skill in matching_category_skills
                    for block_id in (skill.evidence_block_ids or [])
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="skill_categories_any_of",
                    label=skill.skill_display,
                    fact_type="skill",
                    evidence_block_ids=skill.evidence_block_ids or [],
                )
                for skill in matching_category_skills
            )

        skill_keys = {skill.skill_key for skill in resume.skills}
        required_skill_keys = {normalized_key(item) for item in request.skills_all_of}
        optional_skill_keys = {normalized_key(item) for item in request.skills_any_of}
        if required_skill_keys and not required_skill_keys.issubset(skill_keys):
            continue
        if optional_skill_keys and not optional_skill_keys.intersection(skill_keys):
            continue
        if required_skill_keys:
            matched_filters.append("skills_all_of")
            matching_required_skills = [
                skill
                for skill in resume.skills
                if skill.skill_key in required_skill_keys
            ]
            _add_display_field(
                display_field_values,
                key="skills",
                values=[skill.skill_display for skill in matching_required_skills],
                evidence_block_ids=[
                    block_id
                    for skill in matching_required_skills
                    for block_id in (skill.evidence_block_ids or [])
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="skills_all_of",
                    label=skill.skill_display,
                    fact_type="skill",
                    evidence_block_ids=skill.evidence_block_ids or [],
                )
                for skill in resume.skills
                if skill.skill_key in required_skill_keys
            )
        if optional_skill_keys:
            matched_filters.append("skills_any_of")
            matching_optional_skills = [
                skill
                for skill in resume.skills
                if skill.skill_key in optional_skill_keys
            ]
            _add_display_field(
                display_field_values,
                key="skills",
                values=[skill.skill_display for skill in matching_optional_skills],
                evidence_block_ids=[
                    block_id
                    for skill in matching_optional_skills
                    for block_id in (skill.evidence_block_ids or [])
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="skills_any_of",
                    label=skill.skill_display,
                    fact_type="skill",
                    evidence_block_ids=skill.evidence_block_ids or [],
                )
                for skill in resume.skills
                if skill.skill_key in optional_skill_keys
            )

        if request.language_credentials_any_of:
            matching_language_evidence = _matching_language_evidence(
                resume,
                request.language_credentials_any_of,
                include_source_language_evidence=include_source_language_evidence,
            )
            if not matching_language_evidence:
                continue
            matched_filters.append("language_credentials_any_of")
            _add_display_field(
                display_field_values,
                key="language",
                values=[item.label for item in matching_language_evidence],
                evidence_block_ids=[
                    block_id
                    for item in matching_language_evidence
                    for block_id in item.evidence_block_ids
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="language_credentials_any_of",
                    label=item.label,
                    fact_type="language",
                    evidence_block_ids=item.evidence_block_ids,
                    evidence_origin=item.evidence_origin,
                )
                for item in matching_language_evidence
            )

        if request.scholarship_status == "unknown":
            if resume.scholarships:
                continue
            matched_filters.append("scholarship_status=unknown")
            _add_display_field(
                display_field_values,
                key="scholarship",
                values=["未识别"],
                evidence_block_ids=[],
            )
        elif request.scholarship_status == "present" and not resume.scholarships:
            continue
        matching_scholarships = [
            scholarship
            for scholarship in resume.scholarships
            if (
                not request.scholarship_levels_any_of
                or scholarship.scholarship_level in request.scholarship_levels_any_of
            )
            and _matches_any_text(
                scholarship.scholarship_name_raw,
                request.scholarship_name_contains,
            )
        ]
        if (
            request.scholarship_status == "present"
            or request.scholarship_levels_any_of
            or request.scholarship_name_contains
        ):
            if not matching_scholarships:
                continue
            matched_filters.append("scholarship")
            _add_display_field(
                display_field_values,
                key="scholarship",
                values=[
                    scholarship.scholarship_name_raw
                    for scholarship in matching_scholarships
                ],
                evidence_block_ids=[
                    block_id
                    for scholarship in matching_scholarships
                    for block_id in (scholarship.evidence_block_ids or [])
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="scholarship",
                    label=scholarship.scholarship_name_raw,
                    fact_type="scholarship",
                    evidence_block_ids=scholarship.evidence_block_ids or [],
                )
                for scholarship in matching_scholarships
            )

        competition_experiences = [
            experience
            for experience in resume.experiences
            if experience.experience_type == "competition"
        ]
        competition_awards = [
            experience
            for experience in competition_experiences
            if experience.award_level or experience.award_result_raw
        ]
        if request.competition_status == "present" and not competition_experiences:
            continue
        if request.competition_status == "unknown" and competition_experiences:
            continue
        if request.competition_award_status == "present" and not competition_awards:
            continue
        if request.competition_award_status == "unknown" and competition_awards:
            continue
        if request.competition_status != "any" or request.competition_award_status != "any":
            matched_filters.append("competition")
            matching_competitions = competition_awards or competition_experiences
            if matching_competitions:
                _add_display_field(
                    display_field_values,
                    key="competition",
                    values=[
                        " | ".join(
                            value
                            for value in (
                                experience.experience_name_raw,
                                experience.award_result_raw,
                            )
                            if value
                        )
                        for experience in matching_competitions
                    ],
                    evidence_block_ids=[
                        block_id
                        for experience in matching_competitions
                        for block_id in (experience.evidence_block_ids or [])
                    ],
                )
            else:
                _add_display_field(
                    display_field_values,
                    key="competition",
                    values=["未识别"],
                    evidence_block_ids=[],
                )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="competition",
                    label=" | ".join(
                        value
                        for value in (
                            experience.experience_name_raw,
                            experience.award_result_raw,
                        )
                        if value
                    ),
                    fact_type="experience",
                    evidence_block_ids=experience.evidence_block_ids or [],
                )
                for experience in (competition_awards or competition_experiences)
            )

        if request.leadership_any_of:
            matching_leadership = [
                experience
                for filter_item in request.leadership_any_of
                for experience in resume.experiences
                if _matches_leadership(filter_item, experience)
            ]
            if not matching_leadership:
                continue
            matched_filters.append("leadership")
            _add_display_field(
                display_field_values,
                key="leadership",
                values=[
                    experience.leadership_role
                    for experience in matching_leadership
                ],
                evidence_block_ids=[
                    block_id
                    for experience in matching_leadership
                    for block_id in (experience.evidence_block_ids or [])
                ],
            )
            matched_evidence.extend(
                CandidateSearchMatch(
                    filter_key="leadership",
                    label=experience.leadership_role or "",
                    fact_type="experience",
                    evidence_block_ids=experience.evidence_block_ids or [],
                )
                for experience in matching_leadership
            )

        if not _matches_keywords(
            resume,
            all_of=request.keywords_all_of,
            any_of=request.keywords_any_of,
        ):
            continue
        if not _matches_v2_keywords(
            resume,
            keywords=request.keywords,
            mode=request.keyword_match_mode,
        ):
            continue
        if request.keywords_all_of:
            matched_filters.append("keywords_all_of")
            keyword_block_ids = _matching_keyword_block_ids(
                resume,
                request.keywords_all_of,
            )
            _add_display_field(
                display_field_values,
                key="keywords",
                values=request.keywords_all_of,
                evidence_block_ids=keyword_block_ids,
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="keywords_all_of",
                    label=", ".join(request.keywords_all_of),
                    fact_type="keyword",
                    evidence_block_ids=keyword_block_ids,
                )
            )
        if request.keywords_any_of:
            matched_filters.append("keywords_any_of")
            keyword_block_ids = _matching_keyword_block_ids(
                resume,
                request.keywords_any_of,
            )
            _add_display_field(
                display_field_values,
                key="keywords",
                values=request.keywords_any_of,
                evidence_block_ids=keyword_block_ids,
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="keywords_any_of",
                    label=", ".join(request.keywords_any_of),
                    fact_type="keyword",
                    evidence_block_ids=keyword_block_ids,
                )
            )
        if request.keywords:
            matched_filters.append(f"keywords_{request.keyword_match_mode}")
            keyword_block_ids = _matching_v2_keyword_block_ids(
                resume,
                keywords=request.keywords,
                mode=request.keyword_match_mode,
            )
            _add_display_field(
                display_field_values,
                key="keywords",
                values=request.keywords,
                evidence_block_ids=keyword_block_ids,
            )
            matched_evidence.append(
                CandidateSearchMatch(
                    filter_key="keywords",
                    label=", ".join(request.keywords),
                    fact_type="keyword",
                    evidence_block_ids=keyword_block_ids,
                )
            )

        candidate: Candidate = resume.candidate
        summary = _current_summary(resume)
        score = _latest_score(
            resume,
            template_id=score_template.id if score_template is not None else None,
            template_version=(
                score_template.version if score_template is not None else None
            ),
        )
        highest_education = _highest_education(resume)
        latest_experience = _latest_relevant_experience(resume)
        skill_highlights = sorted(
            {
                skill.skill_display.strip()
                for skill in resume.skills
                if isinstance(skill.skill_display, str) and skill.skill_display.strip()
            },
            key=normalized_key,
        )
        results.append(
            _SearchResult(
                item=CandidateSearchItem(
                candidate_id=candidate.id,
                display_name=candidate.display_name,
                resume_id=resume.id,
                original_filename=resume.original_filename,
                is_985_211=bool(resume.is_985_211),
                institution_classifications=_resume_institution_classifications(resume),
                highest_degree=resume.highest_degree,
                employment_months=resume.employment_months,
                employment_or_internship_months=resume.employment_or_internship_months,
                education_school=(
                    highest_education.school_name_raw
                    if highest_education is not None
                    else None
                ),
                education_major=(
                    highest_education.major_raw
                    if highest_education is not None
                    else None
                ),
                latest_experience_title=(
                    latest_experience.title_raw
                    if latest_experience is not None
                    else None
                ),
                latest_experience_organization=(
                    latest_experience.organization_name_raw
                    if latest_experience is not None
                    else None
                ),
                latest_experience_type=(
                    latest_experience.experience_type
                    if latest_experience is not None
                    else None
                ),
                skill_highlights=skill_highlights,
                summary_preview=_summary_preview(summary),
                score_id=score.id if score else None,
                score_template_id=score.template_id if score else None,
                score_total=score.total_score if score else None,
                score_status=score.status if score else None,
                score_template_name=score.template.name if score and score.template else None,
                score_confidence=_score_confidence(score),
                display_fields=_display_fields(display_field_values),
                matched_filters=matched_filters,
                matched_evidence=matched_evidence,
                ),
                updated_at=resume.updated_at,
            )
        )
    if score_template is not None:
        # Scores from different templates, or from an older version of this
        # template, are not the same measurement.  Only a recruiter-selected
        # current template is allowed to determine the default score order.
        # Records without that score follow all scored candidates, then keep
        # existing recency/id tie-breakers for a stable cursor.
        results.sort(
            key=lambda result: (
                result.item.score_total is not None,
                (
                    result.item.score_total
                    if result.item.score_total is not None
                    else -1.0
                ),
                result.updated_at,
                result.item.resume_id,
            ),
            reverse=True,
        )
    else:
        # A score without a selected common template remains visible as a
        # per-candidate reference, but must not silently rank candidates.
        results.sort(
            key=lambda result: (result.updated_at, result.item.resume_id),
            reverse=True,
        )
    total_count = len(results)
    if cursor_resume_id is not None:
        cursor_index = next(
            (
                index
                for index, result in enumerate(results)
                if result.item.resume_id == cursor_resume_id
            ),
            None,
        )
        if cursor_index is None:
            raise SearchValidationError("invalid_cursor")
        results = results[cursor_index + 1 :]

    page_results = results[: request.limit]
    page_items = [result.item for result in page_results]
    next_cursor = (
        _encode_cursor(
            updated_at=page_results[-1].updated_at,
            resume_id=page_results[-1].item.resume_id,
        )
        if len(results) > len(page_results) and page_results
        else None
    )
    needs_review_candidate_ids = set(
        session.scalars(
            select(Resume.candidate_id).where(Resume.extraction_status == "needs_review")
        ).all()
    )
    # An older bad extraction may already be ``ready`` and active.  Surface it
    # in the same review counter so it is not silently lost after exclusion
    # from search results.
    unreliable_active_candidate_ids = {
        resume.candidate_id
        for resume in session.scalars(
            select(Resume).where(
                Resume.is_active.is_(True),
                Resume.extraction_status == "ready",
            )
        ).all()
        if not is_resume_screening_eligible(resume)
    }
    needs_review_candidate_ids.update(unreliable_active_candidate_ids)
    return CandidateSearchResponse(
        items=page_items,
        next_cursor=next_cursor,
        needs_review_count=len(needs_review_candidate_ids),
        total_count=total_count,
    )
