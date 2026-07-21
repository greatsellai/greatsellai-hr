from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Institution, InstitutionAlias
from app.services.normalization import normalized_key


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "resources" / "985_211_institutions.json"
HIGHER_EDUCATION_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "resources"
    / "moe_higher_education_institutions_2026.json"
)
AI_RULEBOOK_PATH = Path(__file__).resolve().parents[1] / "resources" / "ai_985_211_rulebook.md"


# These are the only six recruiter-facing school classifications.  They are
# mutually exclusive on an individual education record.  A candidate can
# legitimately have more than one because they can have several educations.
INSTITUTION_CLASSIFICATION_ORDER = (
    "985",
    "211",
    "undergraduate",
    "associate",
    "secondary_vocational",
    "overseas",
)


# The current public source for nationwide secondary vocational schools is not
# a complete versioned roster we can safely bundle.  Until that exists, the
# record must explicitly say it is a secondary-vocational school; a generic
# high-school or vocational degree is deliberately not enough.
_SECONDARY_VOCATIONAL_MARKERS = (
    "\u4e2d\u4e13",
    "\u4e2d\u7b49\u4e13\u4e1a\u5b66\u6821",
    "\u804c\u4e1a\u4e2d\u4e13",
    "\u804c\u4e1a\u9ad8\u4e2d",
    "\u804c\u9ad8",
    "\u6280\u5de5\u5b66\u6821",
    "\u6280\u5e08\u5b66\u9662",
)


# Overseas classification is evidence-led.  English text or a name missing
# from the domestic list never proves that a school is overseas.  We also keep
# Hong Kong, Macao and Taiwan out until product policy explicitly defines them.
_OVERSEAS_CONTEXT_MARKERS = (
    "\u6d77\u5916",
    "\u5883\u5916",
    "\u7559\u5b66",
    "\u56fd\u5916",
    "\u7f8e\u56fd",
    "\u82f1\u56fd",
    "\u52a0\u62ff\u5927",
    "\u6fb3\u5927\u5229\u4e9a",
    "\u65b0\u897f\u5170",
    "\u6cd5\u56fd",
    "\u5fb7\u56fd",
    "\u65e5\u672c",
    "\u97e9\u56fd",
    "\u65b0\u52a0\u5761",
    "\u8377\u5170",
    "\u745e\u58eb",
    "\u745e\u5178",
    "\u7231\u5c14\u5170",
    "\u610f\u5927\u5229",
    "\u897f\u73ed\u7259",
    "\u6bd4\u5229\u65f6",
    "\u4e39\u9ea6",
    "\u632a\u5a01",
    "\u82ac\u5170",
    "\u5965\u5730\u5229",
    "\u4fc4\u7f57\u65af",
    "\u9a6c\u6765\u897f\u4e9a",
    "\u6cf0\u56fd",
    "\u5370\u5ea6",
    "\u963f\u8054\u914b",
)
_OVERSEAS_NON_DEGREE_MARKERS = (
    "\u4ea4\u6362",
    "\u8bbf\u5b66",
    "\u6691\u6821",
    "\u590f\u6821",
    "\u6e38\u5b66",
    "\u77ed\u671f",
    "\u57f9\u8bad",
    "\u4e2d\u5916\u5408\u4f5c",
    "\u5408\u4f5c\u529e\u5b66",
    "\u8054\u5408\u57f9\u517b",
)
_FOREIGN_NAME_TOKEN = re.compile(r"[A-Za-z]{3,}")

# Only unambiguous, commonly used abbreviations are accepted. Ambiguous short
# names (for example "广大") are intentionally absent and are never AI-guessed.
COMMON_INSTITUTION_ALIASES: dict[str, tuple[str, ...]] = {
    "北京大学": ("北大",),
    "清华大学": ("清华",),
    "北京航空航天大学": ("北航",),
    "北京理工大学": ("北理工",),
    "北京师范大学": ("北师大",),
    "北京邮电大学": ("北邮",),
    "复旦大学": ("复旦",),
    "上海交通大学": ("上海交大", "上交"),
    "浙江大学": ("浙大",),
    "中国科学技术大学": ("中科大",),
    "哈尔滨工业大学": ("哈工大",),
    "武汉大学": ("武大",),
    "华中科技大学": ("华中大", "华科"),
    "厦门大学": ("厦大",),
    "华南理工大学": ("华工",),
    "四川大学": ("川大",),
    "电子科技大学": ("电子科大", "成电"),
    "重庆大学": ("重大",),
    "西安交通大学": ("西安交大", "西交"),
}


class InstitutionRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegistryInstitution:
    roster_id: str
    canonical_name: str
    is_985_211: bool
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class InstitutionRegistry:
    version: str
    institutions: tuple[RegistryInstitution, ...]


@dataclass(frozen=True)
class HigherEducationRegistryInstitution:
    institution_code: str
    canonical_name: str
    classification: str


@dataclass(frozen=True)
class HigherEducationRegistry:
    version: str
    institutions: tuple[HigherEducationRegistryInstitution, ...]


@dataclass(frozen=True)
class EducationInstitutionClassification:
    """A source-grounded classification for one education record.

    ``classification`` is intentionally nullable: unknown is not a negative
    assertion.  The metadata makes any future roster revision auditable
    without exposing raw resume text.
    """

    classification: str | None
    basis: str | None
    registry_version: str | None
    evidence_block_ids: tuple[str, ...]


@lru_cache(maxsize=1)
def load_registry() -> InstitutionRegistry:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    version = raw.get("registry_version")
    records = raw.get("institutions")
    if not isinstance(version, str) or not isinstance(records, list):
        raise InstitutionRegistryError("invalid_registry_shape")

    institutions: list[RegistryInstitution] = []
    alias_owners: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise InstitutionRegistryError("invalid_registry_record")
        roster_id = record.get("roster_id")
        canonical_name = record.get("canonical_name")
        is_985_211 = record.get("is_985_211")
        aliases = record.get("aliases", [])
        if not isinstance(roster_id, str) or not isinstance(canonical_name, str):
            raise InstitutionRegistryError("invalid_registry_identity")
        if not isinstance(is_985_211, bool) or not isinstance(aliases, list):
            raise InstitutionRegistryError("invalid_registry_attributes")
        aliases = [*aliases, *COMMON_INSTITUTION_ALIASES.get(canonical_name, ())]
        all_names = [canonical_name, *aliases]
        for name in all_names:
            key = normalized_key(name)
            if not key:
                raise InstitutionRegistryError("blank_registry_alias")
            previous_owner = alias_owners.setdefault(key, roster_id)
            if previous_owner != roster_id:
                raise InstitutionRegistryError("conflicting_registry_alias")
        institutions.append(
            RegistryInstitution(
                roster_id=roster_id,
                canonical_name=canonical_name,
                is_985_211=is_985_211,
                aliases=tuple(aliases),
            )
        )

    if len(institutions) != 112:
        raise InstitutionRegistryError("unexpected_211_institution_count")
    if sum(item.roster_id.startswith("cn-985-") for item in institutions) != 39:
        raise InstitutionRegistryError("unexpected_985_institution_count")
    if not all(item.is_985_211 for item in institutions):
        raise InstitutionRegistryError("registry_contains_non_211_record")
    return InstitutionRegistry(version=version, institutions=tuple(institutions))


@lru_cache(maxsize=1)
def load_higher_education_registry() -> HigherEducationRegistry:
    """Load the versioned Ministry of Education regular-higher-ed roster.

    The roster is vendored rather than queried at request time, so searches
    are deterministic, work offline, and have a reviewable source version.
    It distinguishes a school's approved level (undergraduate / associate),
    which is not the same thing as the degree written on a candidate's CV.
    """

    raw = json.loads(HIGHER_EDUCATION_REGISTRY_PATH.read_text(encoding="utf-8"))
    version = raw.get("registry_version")
    records = raw.get("institutions")
    counts = raw.get("source_counts")
    if (
        not isinstance(version, str)
        or not isinstance(records, list)
        or not isinstance(counts, dict)
    ):
        raise InstitutionRegistryError("invalid_higher_education_registry_shape")

    institutions: list[HigherEducationRegistryInstitution] = []
    names: dict[str, str] = {}
    classification_counts = {"undergraduate": 0, "associate": 0}
    for record in records:
        if not isinstance(record, dict):
            raise InstitutionRegistryError("invalid_higher_education_registry_record")
        institution_code = record.get("institution_code")
        canonical_name = record.get("canonical_name")
        classification = record.get("classification")
        if (
            not isinstance(institution_code, str)
            or not institution_code.strip()
            or not isinstance(canonical_name, str)
            or not canonical_name.strip()
            or classification not in classification_counts
        ):
            raise InstitutionRegistryError("invalid_higher_education_registry_identity")
        name_key = normalized_key(canonical_name)
        if not name_key:
            raise InstitutionRegistryError("blank_higher_education_registry_name")
        previous_code = names.setdefault(name_key, institution_code)
        if previous_code != institution_code:
            raise InstitutionRegistryError("conflicting_higher_education_registry_name")
        classification_counts[classification] += 1
        institutions.append(
            HigherEducationRegistryInstitution(
                institution_code=institution_code,
                canonical_name=canonical_name,
                classification=classification,
            )
        )

    expected_counts = {"undergraduate": 1412, "associate": 1540}
    if (
        len(institutions) != sum(expected_counts.values())
        or classification_counts != expected_counts
        or counts != expected_counts
    ):
        raise InstitutionRegistryError("unexpected_higher_education_registry_count")
    return HigherEducationRegistry(version=version, institutions=tuple(institutions))


@lru_cache(maxsize=1)
def _registry_institution_by_key() -> dict[str, RegistryInstitution]:
    result: dict[str, RegistryInstitution] = {}
    for institution in load_registry().institutions:
        for name in (institution.canonical_name, *institution.aliases):
            result[normalized_key(name)] = institution
    return result


@lru_cache(maxsize=1)
def _registry_institution_by_roster_id() -> dict[str, RegistryInstitution]:
    return {
        institution.roster_id: institution
        for institution in load_registry().institutions
    }


@lru_cache(maxsize=1)
def _higher_education_institution_by_key() -> dict[str, HigherEducationRegistryInstitution]:
    return {
        normalized_key(institution.canonical_name): institution
        for institution in load_higher_education_registry().institutions
    }


def resolve_registry_institution(school_name_raw: str | None) -> RegistryInstitution | None:
    """Resolve an exact 985 / 211 historical-roster name without a database."""

    school_key = normalized_key(school_name_raw)
    if not school_key:
        return None
    return _registry_institution_by_key().get(school_key)


def resolve_higher_education_institution(
    school_name_raw: str | None,
) -> HigherEducationRegistryInstitution | None:
    """Resolve an exact name in the versioned nationwide higher-ed roster."""

    school_key = normalized_key(school_name_raw)
    if not school_key:
        return None
    return _higher_education_institution_by_key().get(school_key)


def _sorted_evidence_block_ids(block_ids: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(sorted({block_id for block_id in (block_ids or []) if block_id}))


def _has_any_marker(source_text: str, markers: tuple[str, ...]) -> bool:
    source_key = normalized_key(source_text)
    return any(normalized_key(marker) in source_key for marker in markers)


def _is_explicit_overseas_education(
    *,
    school_name_raw: str,
    evidence_text: str,
) -> bool:
    """Return true only for explicit foreign-study evidence.

    The school name can contain English but that is never independently
    sufficient.  This intentionally excludes short exchanges and cooperation
    programmes, whose degree-awarding institution cannot be assumed.
    """

    combined = f"{school_name_raw}\n{evidence_text}"
    if _has_any_marker(combined, _OVERSEAS_NON_DEGREE_MARKERS):
        return False
    if not _has_any_marker(combined, _OVERSEAS_CONTEXT_MARKERS):
        return False
    # A country/overseas marker elsewhere on a page is not enough by itself.
    # Require a foreign-style school name or a clear education completion cue.
    has_foreign_school_token = bool(_FOREIGN_NAME_TOKEN.search(school_name_raw))
    completion_markers = (
        "\u6bd5\u4e1a",
        "\u5b66\u58eb",
        "\u7855\u58eb",
        "\u535a\u58eb",
        "degree",
        "graduated",
    )
    return has_foreign_school_token or _has_any_marker(combined, completion_markers)


def classify_education_institution(
    *,
    school_name_raw: str,
    degree: str,
    evidence_text: str,
    evidence_block_ids: list[str] | tuple[str, ...] | None,
    registry_roster_id: str | None = None,
) -> EducationInstitutionClassification:
    """Classify one saved education fact using controlled sources first.

    This routine never treats a degree label as proof of a school's approved
    level.  It also never infers overseas status from English alone.  The
    caller has already grounded ``school_name_raw`` in the supplied source
    blocks, and those IDs are stored as the classification evidence.
    """

    block_ids = _sorted_evidence_block_ids(evidence_block_ids)
    registry_institution = (
        _registry_institution_by_roster_id().get(registry_roster_id or "")
        or resolve_registry_institution(school_name_raw)
    )
    if registry_institution is not None:
        classification = (
            "985"
            if registry_institution.roster_id.startswith("cn-985-")
            else "211"
        )
        return EducationInstitutionClassification(
            classification=classification,
            basis="moe_985_211_registry",
            registry_version=load_registry().version,
            evidence_block_ids=block_ids,
        )

    higher_education_institution = resolve_higher_education_institution(
        school_name_raw
    )
    if higher_education_institution is not None:
        return EducationInstitutionClassification(
            classification=higher_education_institution.classification,
            basis="moe_higher_education_registry",
            registry_version=load_higher_education_registry().version,
            evidence_block_ids=block_ids,
        )

    combined = f"{school_name_raw}\n{evidence_text}"
    if _has_any_marker(combined, _SECONDARY_VOCATIONAL_MARKERS):
        return EducationInstitutionClassification(
            classification="secondary_vocational",
            basis="source_evidence",
            registry_version=None,
            evidence_block_ids=block_ids,
        )
    # Extracted records with no degree are often courses, training, or a
    # partially parsed line.  Unlike an explicit secondary-school marker,
    # overseas context alone is not enough for those records.
    if degree == "unknown":
        return EducationInstitutionClassification(None, None, None, ())
    if _is_explicit_overseas_education(
        school_name_raw=school_name_raw,
        evidence_text=evidence_text,
    ):
        return EducationInstitutionClassification(
            classification="overseas",
            basis="source_evidence",
            registry_version=None,
            evidence_block_ids=block_ids,
        )
    return EducationInstitutionClassification(None, None, None, ())


@lru_cache(maxsize=1)
def build_985_211_ai_rulebook() -> str:
    """Render the exact versioned roster supplied to the extraction model."""

    template = AI_RULEBOOK_PATH.read_text(encoding="utf-8")
    registry = load_registry()
    roster_lines: list[str] = []
    for entry in registry.institutions:
        aliases = "、".join(entry.aliases) if entry.aliases else "无"
        roster_lines.append(
            f"- {entry.roster_id} | {entry.canonical_name} | {aliases}"
        )
    rendered = (
        template.replace("{{REGISTRY_VERSION}}", registry.version)
        .replace("{{ROSTER_ENTRIES}}", "\n".join(roster_lines))
    )
    if "{{REGISTRY_VERSION}}" in rendered or "{{ROSTER_ENTRIES}}" in rendered:
        raise InstitutionRegistryError("invalid_ai_rulebook_template")
    return rendered


def seed_institution_registry(session: Session) -> None:
    registry = load_registry()
    known_institutions = {
        item.roster_id: item
        for item in session.scalars(select(Institution)).all()
    }

    for entry in registry.institutions:
        institution = known_institutions.get(entry.roster_id)
        if institution is None:
            institution = Institution(
                roster_id=entry.roster_id,
                canonical_name=entry.canonical_name,
                canonical_key=normalized_key(entry.canonical_name),
                is_985_211=entry.is_985_211,
                # 985 is intentionally not also tagged 211.  The historical
                # combined boolean remains on Institution/Resume only for API
                # compatibility; recruiter filters use the exact category.
                tier_tags=["985"] if entry.roster_id.startswith("cn-985-") else ["211"],
                registry_version=registry.version,
            )
            session.add(institution)
        else:
            institution.canonical_name = entry.canonical_name
            institution.canonical_key = normalized_key(entry.canonical_name)
            institution.is_985_211 = entry.is_985_211
            institution.tier_tags = [
                "985" if entry.roster_id.startswith("cn-985-") else "211"
            ]
            institution.registry_version = registry.version
    session.flush()

    institutions_by_roster = {
        item.roster_id: item
        for item in session.scalars(select(Institution)).all()
    }
    aliases_by_key = {
        item.alias_key: item
        for item in session.scalars(select(InstitutionAlias)).all()
    }
    for entry in registry.institutions:
        institution = institutions_by_roster[entry.roster_id]
        for name in (entry.canonical_name, *entry.aliases):
            alias_key = normalized_key(name)
            existing_alias = aliases_by_key.get(alias_key)
            if existing_alias is not None and existing_alias.institution_id != institution.id:
                raise InstitutionRegistryError("stored_registry_alias_conflict")
            if existing_alias is None:
                session.add(
                    InstitutionAlias(
                        alias_key=alias_key,
                        institution_id=institution.id,
                    )
                )


def is_institution_registry_seeded(session: Session) -> bool:
    """Return whether every current registry institution is available locally.

    This deliberately checks the registry version as well as the primary names.
    A web process must never silently classify schools against a stale or partial
    roster in production.
    """

    registry = load_registry()
    institutions = {
        item.roster_id: item
        for item in session.scalars(select(Institution)).all()
    }
    if len(institutions) < len(registry.institutions):
        return False
    for entry in registry.institutions:
        institution = institutions.get(entry.roster_id)
        if institution is None or institution.registry_version != registry.version:
            return False
        for name in (entry.canonical_name, *entry.aliases):
            alias = session.get(InstitutionAlias, normalized_key(name))
            if alias is None or alias.institution_id != institution.id:
                return False
    return True


def resolve_institution(session: Session, school_name_raw: str) -> Institution | None:
    key = normalized_key(school_name_raw)
    if not key:
        return None
    alias = session.get(InstitutionAlias, key)
    return alias.institution if alias is not None else None


def resolve_institution_by_roster_id(
    session: Session,
    roster_id: str | None,
) -> Institution | None:
    if not roster_id or not roster_id.strip():
        return None
    return session.scalar(
        select(Institution).where(Institution.roster_id == roster_id.strip())
    )
