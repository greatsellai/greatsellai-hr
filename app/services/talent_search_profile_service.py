"""Confirmation-first AI talent-search profiles.

The service deliberately separates three moments that used to be conflated by
the recruiting Agent: drafting a search plan, an HR confirming that plan, and
running deterministic recall plus evidence-grounded semantic matching.  A
browser cannot supply a candidate set or bypass a confirmed hard-filter
snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
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
# These phrases are purposefully concrete.  A nearby broad word such as
# ``experience`` or a separate project-duration condition must never turn an
# unrelated exact skill into a project-proof requirement.
_EXPERIENCE_POLICY_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    ("科研项目", "research"),
    ("研究项目", "research"),
    ("科研经历", "research"),
    ("研究经历", "research"),
    ("research project", "research"),
    ("research experience", "research"),
    ("researchproject", "research"),
    ("researchexperience", "research"),
    ("竞赛项目", "competition"),
    ("竞赛经历", "competition"),
    ("competition project", "competition"),
    ("competition experience", "competition"),
    ("competitionproject", "competition"),
    ("competitionexperience", "competition"),
    ("项目经验", "project"),
    ("项目经历", "project"),
    ("project experience", "project"),
    ("projectexperience", "project"),
    ("实习经验", "internship"),
    ("实习经历", "internship"),
    ("internship experience", "internship"),
    ("internshipexperience", "internship"),
    ("工作经验", "employment"),
    ("工作经历", "employment"),
    ("工作职责", "employment"),
    ("正式工作", "employment"),
    ("work experience", "employment"),
    ("workexperience", "employment"),
    ("work history", "employment"),
    ("workhistory", "employment"),
    ("employment", "employment"),
    ("项目", "project"),
    ("project", "project"),
    ("实习", "internship"),
    ("internship", "internship"),
    ("科研", "research"),
    ("研究", "research"),
    ("research", "research"),
    ("竞赛", "competition"),
    ("competition", "competition"),
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
_EXPERIENCE_CLAUSE_BREAKS = "，,；;。.!！？?\n"
_EXPERIENCE_DIRECT_BEFORE_MARKER_GAPS = {
    "",
    "的",
    "相关",
    "相关的",
    "实际",
    "实际的",
    "实战",
    "实践",
    "有",
    "具备",
    "拥有",
    "做过",
    "参与",
}
_EXPERIENCE_DIRECT_AFTER_MARKER_GAPS = {
    "",
    "中",
    "里",
    "中使用",
    "中用",
    "中同时使用",
    "中同时用",
    "中使用了",
    "中采用",
    "中应用",
    "中集成",
    "中实现",
    "里使用",
    "内使用",
    "里同时使用",
    "内同时使用",
    "中基于",
    "基于",
    "使用",
    "采用",
    "with",
    "using",
    "used",
    "in",
}
_EXPERIENCE_AFTER_MARKER_TERM_PREFIXES = (
    "中同时使用",
    "中同时用",
    "中使用了",
    "中使用",
    "中采用",
    "中应用",
    "中集成",
    "中实现",
    "里使用",
    "内使用",
    "里同时使用",
    "内同时使用",
    "中基于",
    "基于",
    "中用",
    "使用",
    "采用",
    "应用",
    "集成",
    "实现",
    "with",
    "using",
    "used",
    "in",
)
_EXPERIENCE_OPTIONAL_MARKERS = (
    "优先",
    "加分",
    "非必须",
    "不是必须",
    "可选",
    "preferred",
    "nicetohave",
    "bonus",
)
_EXPERIENCE_EXCLUDED_MARKERS = (
    "不要求",
    "无需",
    "不要",
    "不需要",
    "不接受",
    "排除",
    "不考虑",
    "notrequired",
    "donotrequire",
    "norequirement",
    "withoutrequiring",
    "exclude",
    "donotaccept",
    "reject",
    "without",
)
_EXPERIENCE_ONLY_ACCEPTANCE_MARKERS = (
    "只要",
    "只接受",
    "仅接受",
    "only",
    "onlyaccept",
    "require",
    "accept",
    "需要",
    "要求",
    "接受",
)
_EXPERIENCE_TERM_PREFIXES = (
    # Keep the longest forms first: this parser removes prefixes repeatedly,
    # so a generic phrase such as "需要具备相关项目经验" never becomes a fake
    # technology requirement.
    "不接受没有",
    "不考虑没有",
    "不接受无",
    "不考虑无",
    "优先寻找有",
    "优先找有",
    "只寻找有",
    "只找有",
    "只寻找",
    "只找",
    "寻找有",
    "找有",
    "需要有",
    "要求有",
    "希望有",
    "不要求",
    "不需要",
    "不接受",
    "不考虑",
    "排除",
    "拒绝",
    "不要",
    "无需",
    "需要",
    "要求",
    "寻找",
    "找",
    "具备",
    "具有",
    "拥有",
    "做过",
    "参与过",
    "熟悉",
    "有",
)
_EXPERIENCE_NON_TERMS = {
    "and",
    "or",
    "only",
    "onlyaccept",
    "no",
    "优先",
    "只要",
    "只接受",
    "仅接受",
    "寻找",
    "找",
    "需要",
    "要求",
    "项目",
    "实习",
    "工作",
    "科研",
    "竞赛",
    "经验",
    "经历",
}
_EXPERIENCE_LIST_CONNECTORS = re.compile(r"(?:或|和|及|与|以及|、|/|and|or)")
_EXPERIENCE_ANY_TERM_CONNECTORS = re.compile(r"(?:或|/|\bor\b)")
_EXPERIENCE_GROUP_ANY_SUFFIX = re.compile(
    r"\s*(?:任一|任意|任何一个|任选其一|即可|either)\s*$"
)
_EXPERIENCE_CHINESE_TERM = re.compile(r"[\u4e00-\u9fff]{2,30}$")
_EXPERIENCE_ASCII_TERM_LIST = re.compile(
    r"[a-z][a-z0-9+.#/_-]*(?:\s*(?:、|,|/|and|or|和|及|与|以及|或)\s*"
    r"[a-z][a-z0-9+.#/_-]*)*$"
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


@dataclass(frozen=True)
class _ExplicitExperienceIntent:
    terms_all_of: tuple[str, ...]
    terms_any_of: tuple[str, ...]
    allowed_types: tuple[str, ...]
    priority: str

    @property
    def terms(self) -> tuple[str, ...]:
        return self.terms_all_of or self.terms_any_of


@dataclass(frozen=True)
class _SourceExperienceTermGroup:
    """A direct ``terms + typed experience`` phrase from the recruiter's text."""

    terms: tuple[str, ...]
    match_mode: str
    allowed_types: tuple[str, ...]
    priority: str


def _normalized_source_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _source_term_occurrences(source: str, term: str) -> list[tuple[int, int]]:
    """Find one source term without allowing ``AI`` inside ``LangChain``."""

    normalized_term = _normalized_source_text(term).strip()
    if not normalized_term:
        return []
    body = re.escape(normalized_term)
    if re.fullmatch(r"[a-z0-9]+", normalized_term):
        pattern = re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")
    else:
        pattern = re.compile(body)
    return [(match.start(), match.end()) for match in pattern.finditer(source)]


def _source_clause_bounds(source: str, position: int) -> tuple[int, int]:
    start = max((source.rfind(marker, 0, position) for marker in _EXPERIENCE_CLAUSE_BREAKS), default=-1) + 1
    following = [
        index
        for marker in _EXPERIENCE_CLAUSE_BREAKS
        if (index := source.find(marker, position)) >= 0
    ]
    return start, min(following) if following else len(source)


def _marker_hits_in_clause(
    source: str,
    *,
    clause_start: int,
    clause_end: int,
) -> list[tuple[int, int, str]]:
    raw_hits: list[tuple[int, int, str]] = []
    for marker, experience_type in _EXPERIENCE_POLICY_TYPE_MARKERS:
        start = source.find(marker, clause_start, clause_end)
        while start >= 0:
            end = start + len(marker)
            if not (
                experience_type == "research"
                and source[end:clause_end].startswith(("方向", "领域", "课题", "主题", " direction", " field", " topic"))
            ):
                raw_hits.append((start, end, experience_type))
            start = source.find(marker, start + len(marker), clause_end)
    raw_hits.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    hits: list[tuple[int, int, str]] = []
    for hit in raw_hits:
        if any(hit[0] < existing[1] and existing[0] < hit[1] for existing in hits):
            continue
        hits.append(hit)
    return hits


def _compact_experience_gap(value: str) -> str:
    return re.sub(r"[\s,，;；:：()（）\[\]【】\-—]", "", value.casefold())


def _is_direct_experience_relation(
    source: str,
    *,
    term_start: int,
    term_end: int,
    marker_start: int,
    marker_end: int,
) -> bool:
    if term_end <= marker_start:
        return _compact_experience_gap(source[term_end:marker_start]) in _EXPERIENCE_DIRECT_BEFORE_MARKER_GAPS
    if marker_end <= term_start:
        return _compact_experience_gap(source[marker_end:term_start]) in _EXPERIENCE_DIRECT_AFTER_MARKER_GAPS
    return False


def _last_marker_position(value: str, markers: tuple[str, ...]) -> int:
    return max((value.rfind(marker) for marker in markers), default=-1)


def _experience_priority_for_relation(
    source: str,
    *,
    clause_start: int,
    term_start: int,
) -> str:
    """Classify language that modifies *this* term, not an entire clause.

    A recruiter can write “优先业务背景且必须有 LangChain 项目经验”.  The
    earlier preference applies to business background, while the later “必须”
    applies to LangChain.  Looking at the whole comma clause silently weakens
    the condition and makes recall/matching misleading.
    """

    before = source[max(clause_start, term_start - 160) : term_start]
    compact = normalized_key(before)

    # “不接受没有 / 不考虑没有 X 项目经验” and “不是不要 X” are double
    # negatives: the candidate must have the named experience.  Check these
    # before generic exclusion language.
    if re.search(
        r"(?:不接受|不考虑|拒绝|排除)(?:没有|无|未|缺少)$",
        compact,
    ) or re.search(r"(?:不是|并非)(?:不要|不要求|不需要|不接受|不考虑)$", compact):
        return "must_have"
    if re.search(
        r"(?:donotaccept|reject|exclude).*(?:without|no)$",
        compact,
    ):
        return "must_have"

    # A directly adjacent exclusion wins for this relation.  It deliberately
    # runs before broad marker ranking so ``不要求`` is not mistaken for a
    # positive ``要求``.
    excluded_tail = re.compile(
        r"(?:不要求|无需|不需要|不要|不接受|排除|不考虑|拒绝|"
        r"notrequired|donotrequire|norequirement|withoutrequiring|"
        r"exclude|donotaccept|reject|without|no)(?:有|具备|拥有)?$"
    )
    if excluded_tail.search(compact):
        return "excluded"

    must_markers = (
        "必须",
        "只找",
        "只接受",
        "仅",
        "需要",
        "要求",
        "must",
        "require",
        "only",
    )
    must_position = _last_marker_position(compact, must_markers)
    optional_position = _last_marker_position(compact, _EXPERIENCE_OPTIONAL_MARKERS)
    excluded_position = _last_marker_position(compact, _EXPERIENCE_EXCLUDED_MARKERS)
    if must_position >= max(optional_position, excluded_position):
        return "must_have"
    if excluded_position > optional_position or re.search(r"^\s*no\s+", before):
        return "excluded"
    if optional_position >= 0:
        return "preferred"
    return "must_have"


def _linked_experience_types(
    source: str,
    *,
    marker_hits: list[tuple[int, int, str]],
    marker_index: int,
) -> list[str]:
    """Include ``项目或实习`` but never another clause's type marker."""

    _, previous_end, experience_type = marker_hits[marker_index]
    types = [experience_type]
    for next_start, next_end, next_type in marker_hits[marker_index + 1 :]:
        connector = normalized_key(source[previous_end:next_start])
        if connector not in {"或", "和", "及", "与", "以及", "and", "or"}:
            break
        types.append(next_type)
        previous_end = next_end
    return types


def _explicit_experience_relations(
    source: str,
    *,
    term: str,
) -> list[tuple[str, str]]:
    """Return directly stated (type, priority) relations for one source term."""

    relations: list[tuple[str, str]] = []
    for term_start, term_end in _source_term_occurrences(source, term):
        clause_start, clause_end = _source_clause_bounds(source, term_start)
        marker_hits = _marker_hits_in_clause(
            source,
            clause_start=clause_start,
            clause_end=clause_end,
        )
        for marker_index, (marker_start, marker_end, _) in enumerate(marker_hits):
            if not _is_direct_experience_relation(
                source,
                term_start=term_start,
                term_end=term_end,
                marker_start=marker_start,
                marker_end=marker_end,
            ):
                continue
            priority = _experience_priority_for_relation(
                source,
                clause_start=clause_start,
                term_start=term_start,
            )
            relations.extend(
                (experience_type, priority)
                for experience_type in _linked_experience_types(
                    source,
                    marker_hits=marker_hits,
                    marker_index=marker_index,
                )
            )
            if priority == "excluded":
                next_clause_start = clause_end + 1
                if next_clause_start < len(source):
                    _, next_clause_end = _source_clause_bounds(source, next_clause_start)
                    next_clause = source[next_clause_start:next_clause_end]
                    if any(
                        marker in normalized_key(next_clause)
                        for marker in _EXPERIENCE_ONLY_ACCEPTANCE_MARKERS
                    ):
                        relations.extend(
                            (experience_type, "must_have")
                            for _, _, experience_type in _marker_hits_in_clause(
                                source,
                                clause_start=next_clause_start,
                                clause_end=next_clause_end,
                            )
                        )
            break
    return relations


def _experience_types_for_explicit_term(message: str, *, term: str) -> list[str]:
    """Compile only the source term's directly stated acceptable contexts."""

    source = _normalized_source_text(message)
    found_types = {
        experience_type
        for experience_type, priority in _explicit_experience_relations(source, term=term)
        if priority != "excluded"
    }
    return [
        experience_type
        for experience_type in _EXPERIENCE_POLICY_TYPE_ORDER
        if experience_type in found_types
    ]


def _experience_detail_policy(
    *,
    terms_all_of: list[str],
    terms_any_of: list[str],
    allowed_types: list[str],
) -> dict[str, object]:
    return {
        "kind": "experience_detail_terms",
        "allowed_experience_types": allowed_types,
        "terms_all_of": terms_all_of,
        "terms_any_of": terms_any_of,
    }


def _text_mentions_term(text: str, term: str) -> bool:
    return bool(_source_term_occurrences(_normalized_source_text(text), term))


def _term_is_covered_by_group_term(term: str, group_term: str) -> bool:
    """Whether a standalone draft term is already part of a promoted phrase.

    This is intentionally word-safe for ASCII.  ``AI`` is not a subterm of
    ``LangChain``, while ``Agent`` is a subterm of ``AI Agent`` and must not
    remain as a separate hard filter after the phrase becomes project evidence.
    """

    term_normalized = _normalized_source_text(term).strip()
    group_normalized = _normalized_source_text(group_term).strip()
    if not term_normalized or not group_normalized:
        return False
    if term_normalized == group_normalized:
        return True
    if re.fullmatch(r"[a-z0-9]+", term_normalized):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(term_normalized)}(?![a-z0-9])",
            group_normalized,
        ) is not None
    return normalized_key(term) in normalized_key(group_term)


def _profile_requirement_mentions_term(value: Mapping[str, object], *, term: str) -> bool:
    text_parts = [value.get("label"), value.get("evidence_hint")]
    evidence_policy = value.get("evidence_policy")
    if isinstance(evidence_policy, Mapping):
        for policy_key in ("terms_all_of", "terms_any_of"):
            terms = evidence_policy.get(policy_key)
            if isinstance(terms, list):
                text_parts.extend(terms)
    return any(
        _text_mentions_term(text, term)
        for text in text_parts
        if isinstance(text, str)
    )


def _profile_requirement_mentions_any_term(
    value: Mapping[str, object],
    *,
    terms: tuple[str, ...],
) -> bool:
    return any(_profile_requirement_mentions_term(value, term=term) for term in terms)


def _clean_source_experience_term(value: str) -> str | None:
    cleaned = " ".join(value.split()).strip("：:，,、/ ")
    while True:
        prefix = next(
            (item for item in _EXPERIENCE_TERM_PREFIXES if cleaned.startswith(item)),
            None,
        )
        if prefix is None:
            break
        cleaned = cleaned[len(prefix) :].strip("：:，,、/ ")
    generic_terms = {
        "无",
        "没有",
        "未",
        "相关",
        "实际",
        "实战",
        "具备",
        "需要具备",
        "丰富",
        "较强",
        "具有",
        "开发",
        "企业级",
        "大型",
        "成熟",
        "完整",
        "项目实施",
        "同一",
        "同一个",
        "在同一个",
        "same",
        "thesame",
        "inthesame",
    }
    if (
        not cleaned
        or len(cleaned) > 120
        or re.search(r"\d|[一二三四五六七八九十两]+年", cleaned)
        or normalized_key(cleaned) in _EXPERIENCE_NON_TERMS
        or normalized_key(cleaned) in generic_terms
    ):
        return None
    return cleaned


def _source_group_parts_from_raw(
    raw_group: str,
    *,
    group_start: int,
    is_any: bool,
) -> tuple[tuple[str, ...], str, int] | None:
    raw_terms = _EXPERIENCE_LIST_CONNECTORS.split(raw_group)
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        cleaned = _clean_source_experience_term(raw_term)
        key = normalized_key(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            terms.append(cleaned)
    if not terms:
        return None
    match_mode = (
        "any_of"
        if is_any or _EXPERIENCE_ANY_TERM_CONNECTORS.search(raw_group)
        else "all_of"
    )
    return tuple(terms), match_mode, group_start


def _source_group_parts_before_marker(
    source: str,
    *,
    clause_start: int,
    marker_start: int,
    marker_end: int,
    clause_end: int,
) -> tuple[tuple[str, ...], str, int] | None:
    """Read a terminal technical term list immediately before an experience marker."""

    # In “同一项目中同时使用 LangChain”, the text before 项目 describes the
    # context, not a technology.  The terms come after the marker instead.
    marker_suffix = source[marker_end:clause_end].lstrip()
    if any(marker_suffix.startswith(prefix) for prefix in _EXPERIENCE_AFTER_MARKER_TERM_PREFIXES):
        return None
    prefix = source[clause_start:marker_start].rstrip()
    any_suffix = _EXPERIENCE_GROUP_ANY_SUFFIX.search(prefix)
    is_any = any_suffix is not None
    if any_suffix is not None:
        prefix = prefix[: any_suffix.start()].rstrip()
    for suffix in ("相关的", "实际的", "相关", "实际", "的"):
        if prefix.endswith(suffix):
            prefix = prefix[: -len(suffix)].rstrip()
            break
    ascii_match = _EXPERIENCE_ASCII_TERM_LIST.search(prefix)
    if ascii_match:
        raw_group = ascii_match.group(0)
        group_start = clause_start + ascii_match.start()
    else:
        chinese_match = _EXPERIENCE_CHINESE_TERM.search(prefix)
        if chinese_match is None:
            return None
        raw_group = chinese_match.group(0)
        group_start = clause_start + chinese_match.start()

    return _source_group_parts_from_raw(
        raw_group,
        group_start=group_start,
        is_any=is_any,
    )


def _source_group_parts_after_marker(
    source: str,
    *,
    marker_end: int,
    clause_end: int,
) -> tuple[tuple[str, ...], str, int] | None:
    """Read ``项目中使用 LangChain 和 RAG`` as one same-experience group."""

    suffix = source[marker_end:clause_end]
    leading_whitespace = len(suffix) - len(suffix.lstrip())
    suffix = suffix.lstrip()
    prefix = next(
        (value for value in _EXPERIENCE_AFTER_MARKER_TERM_PREFIXES if suffix.startswith(value)),
        None,
    )
    if prefix is None:
        return None
    suffix_start = marker_end + leading_whitespace + len(prefix)
    remaining = source[suffix_start:clause_end].lstrip()
    suffix_start += len(source[suffix_start:clause_end]) - len(remaining)
    ascii_match = re.match(
        r"[a-z][a-z0-9+.#/_-]*(?:\s*(?:、|,|/|and|or|和|及|与|以及|或)\s*"
        r"[a-z][a-z0-9+.#/_-]*)*",
        remaining,
    )
    if ascii_match is None:
        return None
    raw_group = ascii_match.group(0)
    after_group = remaining[ascii_match.end() :]
    is_any = _EXPERIENCE_GROUP_ANY_SUFFIX.match(after_group) is not None
    return _source_group_parts_from_raw(
        raw_group,
        group_start=suffix_start,
        is_any=is_any,
    )


_EXPERIENCE_TERM_CONNECTOR_KEYS = {"或", "和", "及", "与", "以及", "and", "or", "/", "、"}
_EXPERIENCE_ANY_CONNECTOR_KEYS = {"或", "or", "/"}


def _known_term_occurrences_in_range(
    source: str,
    *,
    candidate_terms: tuple[str, ...],
    start: int,
    end: int,
) -> list[tuple[int, int, str]]:
    """Find visible draft terms, preferring a full phrase over its substring."""

    occurrences: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int, str]] = set()
    for term in candidate_terms:
        key = normalized_key(term)
        if not key:
            continue
        for term_start, term_end in _source_term_occurrences(source, term):
            if term_start < start or term_end > end:
                continue
            occurrence = (term_start, term_end, term)
            occurrence_key = (term_start, term_end, key)
            if occurrence_key not in seen:
                seen.add(occurrence_key)
                occurrences.append(occurrence)
    return occurrences


def _best_known_occurrence(
    occurrences: list[tuple[int, int, str]],
    *,
    sort_key: str,
) -> tuple[int, int, str] | None:
    if not occurrences:
        return None
    if sort_key == "last":
        return max(occurrences, key=lambda item: (item[1], item[1] - item[0]))
    return min(occurrences, key=lambda item: (item[0], -(item[1] - item[0])))


def _known_term_group_before_marker(
    source: str,
    *,
    candidate_terms: tuple[str, ...],
    clause_start: int,
    marker_start: int,
) -> tuple[tuple[str, ...], str, int] | None:
    """Use draft terms to retain mixed and multi-word source term lists."""

    prefix = source[clause_start:marker_start]
    any_suffix = _EXPERIENCE_GROUP_ANY_SUFFIX.search(prefix)
    term_limit = marker_start
    if any_suffix is not None:
        term_limit = clause_start + any_suffix.start()
    occurrences = _known_term_occurrences_in_range(
        source,
        candidate_terms=candidate_terms,
        start=clause_start,
        end=term_limit,
    )
    last = _best_known_occurrence(
        [
            occurrence
            for occurrence in occurrences
            if _compact_experience_gap(source[occurrence[1] : term_limit])
            in _EXPERIENCE_DIRECT_BEFORE_MARKER_GAPS
        ],
        sort_key="last",
    )
    if last is None:
        return None

    selected = [last]
    connectors: list[str] = []
    current = last
    while True:
        previous = _best_known_occurrence(
            [
                occurrence
                for occurrence in occurrences
                if occurrence[1] <= current[0]
                and not (occurrence[0] < current[1] and current[0] < occurrence[1])
                and _compact_experience_gap(source[occurrence[1] : current[0]])
                in _EXPERIENCE_TERM_CONNECTOR_KEYS
            ],
            sort_key="last",
        )
        if previous is None:
            break
        connectors.append(_compact_experience_gap(source[previous[1] : current[0]]))
        selected.append(previous)
        current = previous

    selected.reverse()
    terms = tuple(item[2] for item in selected)
    if len({normalized_key(term) for term in terms}) != len(terms):
        return None
    match_mode = (
        "any_of"
        if any_suffix is not None or any(connector in _EXPERIENCE_ANY_CONNECTOR_KEYS for connector in connectors)
        else "all_of"
    )
    return terms, match_mode, selected[0][0]


def _known_term_group_after_marker(
    source: str,
    *,
    candidate_terms: tuple[str, ...],
    marker_end: int,
) -> tuple[tuple[str, ...], str, int] | None:
    """Use a direct `项目中/里/内使用 ...` phrase as one evidence group."""

    search_end = min(len(source), marker_end + 180)
    occurrences = _known_term_occurrences_in_range(
        source,
        candidate_terms=candidate_terms,
        start=marker_end,
        end=search_end,
    )
    first = _best_known_occurrence(
        [
            occurrence
            for occurrence in occurrences
            if _compact_experience_gap(source[marker_end : occurrence[0]])
            in _EXPERIENCE_DIRECT_AFTER_MARKER_GAPS
        ],
        sort_key="first",
    )
    if first is None:
        return None

    selected = [first]
    connectors: list[str] = []
    current = first
    while True:
        following = _best_known_occurrence(
            [
                occurrence
                for occurrence in occurrences
                if occurrence[0] >= current[1]
                and not (occurrence[0] < current[1] and current[0] < occurrence[1])
                and _compact_experience_gap(source[current[1] : occurrence[0]])
                in _EXPERIENCE_TERM_CONNECTOR_KEYS
            ],
            sort_key="first",
        )
        if following is None:
            break
        connectors.append(_compact_experience_gap(source[current[1] : following[0]]))
        selected.append(following)
        current = following

    terms = tuple(item[2] for item in selected)
    if len({normalized_key(term) for term in terms}) != len(terms):
        return None
    trailing = source[selected[-1][1] : min(len(source), selected[-1][1] + 20)]
    match_mode = (
        "any_of"
        if _EXPERIENCE_GROUP_ANY_SUFFIX.match(trailing)
        or any(connector in _EXPERIENCE_ANY_CONNECTOR_KEYS for connector in connectors)
        else "all_of"
    )
    return terms, match_mode, selected[0][0]


def _source_experience_term_groups(
    source: str,
    *,
    candidate_terms: tuple[str, ...] = (),
) -> list[_SourceExperienceTermGroup]:
    """Compile direct typed-experience groups without inferring nearby skills."""

    groups: list[_SourceExperienceTermGroup] = []
    seen: set[tuple[tuple[str, ...], str, tuple[str, ...], str]] = set()
    marker_hits = _marker_hits_in_clause(source, clause_start=0, clause_end=len(source))
    for marker_index, (marker_start, _, _) in enumerate(marker_hits):
        marker_end = marker_hits[marker_index][1]
        clause_start, clause_end = _source_clause_bounds(source, marker_start)
        allowed_types = tuple(
            value
            for value in _EXPERIENCE_POLICY_TYPE_ORDER
            if value
            in _linked_experience_types(
                source,
                marker_hits=marker_hits,
                marker_index=marker_index,
            )
        )
        if not allowed_types:
            continue
        known_groups = (
            _known_term_group_before_marker(
                source,
                candidate_terms=candidate_terms,
                clause_start=clause_start,
                marker_start=marker_start,
            ),
            _known_term_group_after_marker(
                source,
                candidate_terms=candidate_terms,
                marker_end=marker_end,
            ),
        )
        parsed_groups = (
            known_groups
            if any(group is not None for group in known_groups)
            else (
                _source_group_parts_before_marker(
                    source,
                    clause_start=clause_start,
                    marker_start=marker_start,
                    marker_end=marker_end,
                    clause_end=clause_end,
                ),
                _source_group_parts_after_marker(
                    source,
                    marker_end=marker_end,
                    clause_end=clause_end,
                ),
            )
        )
        for parsed in parsed_groups:
            if parsed is None:
                continue
            terms, match_mode, group_start = parsed
            priority = _experience_priority_for_relation(
                source,
                clause_start=clause_start,
                term_start=group_start,
            )
            group = _SourceExperienceTermGroup(
                terms=terms,
                match_mode=match_mode,
                allowed_types=allowed_types,
                priority=priority,
            )
            group_key = (
                tuple(normalized_key(term) for term in group.terms),
                group.match_mode,
                group.allowed_types,
                group.priority,
            )
            if group_key not in seen:
                seen.add(group_key)
                groups.append(group)
    return groups


def _source_terms_before_experience_markers(source: str) -> list[str]:
    """Compatibility helper used by focused parser tests and diagnostics."""

    return [term for group in _source_experience_term_groups(source) for term in group.terms]


def _requirement_term_spelling(
    term: str,
    requirements: list[object],
) -> str:
    """Prefer the reviewable spelling already visible in an AI draft."""

    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        for field in ("label", "evidence_hint"):
            value = requirement.get(field)
            if not isinstance(value, str):
                continue
            occurrences = _source_term_occurrences(_normalized_source_text(value), term)
            if occurrences:
                start, end = occurrences[0]
                return value[start:end]
    return term


def _source_term_display_spelling(
    term: str,
    *,
    hard_values: Mapping[str, object],
    requirements: list[object],
) -> str:
    """Use the draft's stable spelling where it names the same source term."""

    skills = hard_values.get("skills_all_of")
    if isinstance(skills, list):
        for skill in skills:
            if isinstance(skill, str) and normalized_key(skill) == normalized_key(term):
                return skill
    return _requirement_term_spelling(term, requirements)


def _term_is_in_source_alternative(source: str, *, term: str) -> bool:
    """Leave term alternatives untouched until the policy can express OR."""

    for term_start, _ in _source_term_occurrences(source, term):
        clause_start, clause_end = _source_clause_bounds(source, term_start)
        for marker_start, _, _ in _marker_hits_in_clause(
            source,
            clause_start=clause_start,
            clause_end=clause_end,
        ):
            prefix = source[clause_start:marker_start]
            if ("或" in prefix or re.search(r"\bor\b", prefix)) and _text_mentions_term(prefix, term):
                return True
    return False


def _append_explicit_experience_candidate(
    candidates: dict[str, str],
    *,
    value: object,
    source: str,
) -> None:
    if not isinstance(value, str):
        return
    cleaned = " ".join(value.split())
    key = normalized_key(cleaned)
    if not key or len(key) > 120 or not _source_term_occurrences(source, cleaned):
        return
    candidates.setdefault(key, cleaned)


def _explicit_experience_intents_from_profile(
    *,
    request_message: str,
    hard_values: Mapping[str, object],
    verification_requirements: list[object],
    preferred_requirements: list[object],
) -> list[_ExplicitExperienceIntent]:
    """Compile source-grounded project/experience conditions without a broad window.

    AI labels can help locate a condition, but only the recruiter's own direct
    ``term + 项目/实习/工作`` relationship decides whether it becomes an
    executable evidence policy.
    """

    source = _normalized_source_text(request_message)
    all_requirements = [*verification_requirements, *preferred_requirements]
    intents: list[_ExplicitExperienceIntent] = []
    seen_intents: set[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]] = set()

    def append_intent(intent: _ExplicitExperienceIntent) -> None:
        key = (
            tuple(normalized_key(term) for term in intent.terms_all_of),
            tuple(normalized_key(term) for term in intent.terms_any_of),
            intent.allowed_types,
            intent.priority,
        )
        if key not in seen_intents:
            seen_intents.add(key)
            intents.append(intent)

    # Use the AI draft only to retain the recruiter's original multi-word or
    # mixed-language spelling.  A term still has to be directly tied to a
    # typed experience phrase in the source below.
    candidates: dict[str, str] = {}
    skills = hard_values.get("skills_all_of")
    if isinstance(skills, list):
        for skill in skills:
            _append_explicit_experience_candidate(candidates, value=skill, source=source)
    for requirement in all_requirements:
        if not isinstance(requirement, Mapping):
            continue
        policy = requirement.get("evidence_policy")
        if not isinstance(policy, Mapping):
            continue
        for policy_key in ("terms_all_of", "terms_any_of"):
            policy_terms = policy.get(policy_key)
            if isinstance(policy_terms, list):
                for term in policy_terms:
                    _append_explicit_experience_candidate(candidates, value=term, source=source)

    source_groups = _source_experience_term_groups(
        source,
        candidate_terms=tuple(candidates.values()),
    )
    for group in source_groups:
        terms = tuple(
            _source_term_display_spelling(
                term,
                hard_values=hard_values,
                requirements=all_requirements,
            )
            for term in group.terms
        )
        append_intent(
            _ExplicitExperienceIntent(
                terms_all_of=terms if group.match_mode == "all_of" else (),
                terms_any_of=terms if group.match_mode == "any_of" else (),
                allowed_types=group.allowed_types if group.priority != "excluded" else (),
                priority=group.priority,
            )
        )

    # The AI draft can still express a direct post-marker relation such as
    # “项目中使用 LangChain”.  It may not broaden a nearby skill; only a term
    # with an exact typed relation from the recruiter's text gets promoted.
    for term in candidates.values():
        term_key = normalized_key(term)
        if term_key and any(
            _term_is_covered_by_group_term(term, grouped_term)
            for group in source_groups
            if group.priority != "excluded"
            for grouped_term in group.terms
        ):
            continue
        relations = _explicit_experience_relations(source, term=term)
        must_types = {value for value, priority in relations if priority == "must_have"}
        preferred_types = {value for value, priority in relations if priority == "preferred"}
        if must_types:
            priority = "must_have"
            allowed_types = must_types
        elif preferred_types:
            priority = "preferred"
            allowed_types = preferred_types
        elif any(priority == "excluded" for _, priority in relations):
            append_intent(
                _ExplicitExperienceIntent(
                    terms_all_of=(term,),
                    terms_any_of=(),
                    allowed_types=(),
                    priority="excluded",
                )
            )
            continue
        else:
            continue
        append_intent(
            _ExplicitExperienceIntent(
                terms_all_of=(term,),
                terms_any_of=(),
                allowed_types=tuple(
                    value
                    for value in _EXPERIENCE_POLICY_TYPE_ORDER
                    if value in allowed_types
                ),
                priority=priority,
            )
        )
    return intents


def _remove_profile_requirements_for_term(
    requirements: list[object],
    *,
    term: str,
) -> list[object]:
    return [
        value
        for value in requirements
        if not isinstance(value, Mapping)
        or not _profile_requirement_mentions_term(value, term=term)
    ]


def _ensure_experience_requirement(
    verification_requirements: list[object],
    preferred_requirements: list[object],
    *,
    terms_all_of: list[str],
    terms_any_of: list[str],
    allowed_types: list[str],
    priority: str,
) -> tuple[list[object], list[object]]:
    """Keep a direct experience condition visible without changing its priority."""

    terms = tuple(terms_all_of or terms_any_of)
    if not terms:
        return verification_requirements, preferred_requirements
    target_is_verification = priority == "must_have"
    target = verification_requirements if target_is_verification else preferred_requirements
    other = preferred_requirements if target_is_verification else verification_requirements

    experience_label = "、".join(
        _EXPERIENCE_POLICY_TYPE_LABELS.get(value, value)
        for value in allowed_types
    )
    term_label = "、".join(terms_all_of) if terms_all_of else " 或 ".join(terms_any_of)

    def normalized_requirement(value: Mapping[str, object]) -> dict[str, object]:
        updated = dict(value)
        updated["label"] = f"具备 {term_label} 的{experience_label}实践"
        updated["evidence_hint"] = (
            f"核验{experience_label}经历的名称、职责或结果中是否明确、正向使用 {term_label}，"
            "以及候选人的具体实现、贡献或结果。"
        )
        updated["evidence_policy"] = _experience_detail_policy(
            terms_all_of=terms_all_of,
            terms_any_of=terms_any_of,
            allowed_types=allowed_types,
        )
        return updated

    def matching_index(values: list[object]) -> int | None:
        for index, value in enumerate(values):
            if isinstance(value, Mapping) and _profile_requirement_mentions_any_term(
                value,
                terms=terms,
            ):
                return index
        return None

    def without_other_group_terms(
        values: list[object],
        *,
        preserve_index: int | None,
    ) -> list[object]:
        return [
            value
            for index, value in enumerate(values)
            if index == preserve_index
            or not isinstance(value, Mapping)
            or not _profile_requirement_mentions_any_term(value, terms=terms)
        ]

    target_index = matching_index(target)
    if target_index is not None:
        target = without_other_group_terms(target, preserve_index=target_index)
        target = [
            normalized_requirement(value)
            if index == target_index and isinstance(value, Mapping)
            else value
            for index, value in enumerate(target)
        ]
        other = without_other_group_terms(other, preserve_index=None)
        return (target, other) if target_is_verification else (other, target)

    other_index = matching_index(other)
    if other_index is not None:
        matched = other[other_index]
        other = without_other_group_terms(other, preserve_index=None)
        target = [*without_other_group_terms(target, preserve_index=None)]
        if isinstance(matched, Mapping):
            target.append(normalized_requirement(matched))
        return (target, other) if target_is_verification else (other, target)

    used_keys = {
        str(value.get("key"))
        for value in [*verification_requirements, *preferred_requirements]
        if isinstance(value, Mapping) and isinstance(value.get("key"), str)
    }
    suffix = 1
    while f"experience_evidence_{suffix}" in used_keys:
        suffix += 1
    target = [
        *target,
        {
            "key": f"experience_evidence_{suffix}",
            "label": f"具备 {term_label} 的{experience_label}实践",
            "evidence_hint": (
                f"核验{experience_label}经历的名称、职责或结果中是否明确、正向使用 {term_label}，"
                "以及候选人的具体实现、贡献或结果。"
            ),
            "evidence_policy": _experience_detail_policy(
                terms_all_of=terms_all_of,
                terms_any_of=terms_any_of,
                allowed_types=allowed_types,
            ),
        },
    ]
    return (target, other) if target_is_verification else (other, target)


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
    preferred_requirements = list(
        generated.get("preferred_requirements", [])
        if isinstance(generated.get("preferred_requirements"), list)
        else []
    )
    experience_intents = _explicit_experience_intents_from_profile(
        request_message=request_message,
        hard_values=hard_values,
        verification_requirements=verification_requirements,
        preferred_requirements=preferred_requirements,
    )
    if experience_intents:
        hard_values["skills_all_of"] = [
            term
            for term in hard_values.get("skills_all_of", [])
            if not isinstance(term, str)
            or not any(
                _term_is_covered_by_group_term(term, intent_term)
                for intent in experience_intents
                for intent_term in intent.terms
            )
        ]
        for intent in experience_intents:
            if intent.priority == "excluded":
                for term in intent.terms:
                    verification_requirements = _remove_profile_requirements_for_term(
                        verification_requirements,
                        term=term,
                    )
                    preferred_requirements = _remove_profile_requirements_for_term(
                        preferred_requirements,
                        term=term,
                    )
                continue
            verification_requirements, preferred_requirements = (
                _ensure_experience_requirement(
                    verification_requirements,
                    preferred_requirements,
                    terms_all_of=list(intent.terms_all_of),
                    terms_any_of=list(intent.terms_any_of),
                    allowed_types=list(intent.allowed_types),
                    priority=intent.priority,
                )
            )

    try:
        normalized["hard_filters"] = TalentSearchHardFilters.model_validate(
            hard_values
        ).model_dump(mode="json")
    except ValueError:
        return dict(generated)
    normalized["verification_requirements"] = verification_requirements
    normalized["preferred_requirements"] = preferred_requirements
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
    if hard_filters.min_employment_or_internship_months is not None:
        steps.append(
            (
                "min_employment_or_internship_months",
                "工作年限不少于 "
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
    scope_resume_ids: set[str] | None = None,
) -> TalentSearchRecallDiagnostics:
    """Build an honest funnel inside the requested global or frozen scope."""

    baseline = search_candidates(
        session,
        _search_request_from_hard_filters(
            hard_filters,
            limit=1,
            cursor=None,
            included_filter_keys=set(),
        ),
        resume_ids=scope_resume_ids,
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
            resume_ids=scope_resume_ids,
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
    scope_resume_ids: set[str] | None = None,
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
            resume_ids=scope_resume_ids,
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


def _run_result_mode(
    session: Session,
    *,
    run: TalentSearchRun,
) -> str:
    """Describe whether this immutable revision required semantic AI matching."""

    revision = session.get(TalentSearchProfileRevision, run.revision_id)
    if revision is None or revision.profile_id != run.profile_id:
        raise TalentSearchProfileServiceError("talent_search_run_revision_missing")
    return (
        "semantic_verification"
        if revision.match_job_version_id
        else "hard_filter_recall"
    )


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
        scope_kind=(
            run.scope_kind
            if run.scope_kind in {"global", "candidate_filter"}
            else "global"
        ),
        scope_candidate_count=max(run.scope_candidate_count or 0, 0),
        status=_run_status(run, batch),
        result_mode=_run_result_mode(session, run=run),
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
    scope_kind: str,
    scope_fingerprint: str | None,
) -> TalentSearchRun | None:
    runs = session.scalars(
        select(TalentSearchRun)
        .where(
            TalentSearchRun.revision_id == revision.id,
        )
        .order_by(TalentSearchRun.created_at.desc())
    ).all()
    for run in runs:
        existing_scope_kind = run.scope_kind or "global"
        if existing_scope_kind != scope_kind:
            continue
        if scope_kind == "candidate_filter":
            if not scope_fingerprint or run.scope_fingerprint != scope_fingerprint:
                continue
        elif run.scope_fingerprint is not None:
            # A malformed historic row must never be reused as the global
            # run; that could widen a later Agent-scoped request.
            continue
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
    scope_kind: str = "global",
    scope_fingerprint: str | None = None,
    scope_resume_ids: list[str] | None = None,
) -> TalentSearchRunResponse:
    """Recall from a confirmed profile, globally or inside a frozen scope."""

    if scope_kind not in {"global", "candidate_filter"}:
        raise TalentSearchProfileServiceError("talent_search_profile_scope_invalid")
    if scope_kind == "global":
        if scope_fingerprint is not None or scope_resume_ids is not None:
            raise TalentSearchProfileServiceError("talent_search_profile_scope_invalid")
        normalized_scope_resume_ids: list[str] | None = None
        scope_resume_id_set: set[str] | None = None
    else:
        if not scope_fingerprint:
            raise TalentSearchProfileServiceError("talent_search_profile_scope_invalid")
        # These opaque IDs are server-derived by the recruiting-Agent service.
        # De-duplicate defensively before they become an immutable run scope.
        normalized_scope_resume_ids = list(
            dict.fromkeys(
                resume_id.strip()
                for resume_id in (scope_resume_ids or [])
                if isinstance(resume_id, str) and resume_id.strip()
            )
        )
        scope_resume_id_set = set(normalized_scope_resume_ids)

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
    existing_run = _existing_run_for_revision(
        session,
        revision=revision,
        scope_kind=scope_kind,
        scope_fingerprint=scope_fingerprint,
    )
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
        scope_resume_ids=scope_resume_id_set,
    )
    recall_diagnostics = (
        _build_zero_result_diagnostics(
            session,
            hard_filters=hard_filters,
            scope_resume_ids=scope_resume_id_set,
        )
        if not recalled_resume_ids
        else None
    )
    run = TalentSearchRun(
        profile_id=profile.id,
        revision_id=revision.id,
        scope_kind=scope_kind,
        scope_fingerprint=scope_fingerprint,
        scope_candidate_count=(
            len(normalized_scope_resume_ids)
            if normalized_scope_resume_ids is not None
            else 0
        ),
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
