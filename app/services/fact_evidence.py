"""Resolve opaque canonical fact IDs into readable resume-fact summaries.

AI providers only return opaque fact IDs (``education-001``, ``experience-002``,
...).  This small projection of a resume's immutable fact snapshot turns those
IDs back into the readable, factual labels the UI shows as evidence — for both
AI scoring and JD matching.  It is snapshot-based on purpose so a previously
computed result stays explainable even after the resume is updated.
"""

import json
from typing import Literal, Mapping

from app.models import ResumeFactSnapshot
from app.schemas import ResumeScoreFactEvidence

# Canonical-facts JSON categories mapped to the fact type surfaced in the UI.
_CATEGORIES: tuple[tuple[str, Literal["education", "experience", "skill", "language", "scholarship"]], ...] = (
    ("education", "education"),
    ("experiences", "experience"),
    ("skills", "skill"),
    ("language_credentials", "language"),
    ("scholarships", "scholarship"),
)


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def fact_summary(*, fact_type: str, entry: Mapping[str, object]) -> str:
    """Build a concise, structured fact label without exposing raw PDF text."""

    def text(key: str) -> str | None:
        return _optional_string(entry.get(key))

    if fact_type == "education":
        values = [text("school_name_raw"), text("degree"), text("major_raw")]
    elif fact_type == "experience":
        values = [
            text("experience_type"),
            text("organization_name_raw"),
            text("title_raw"),
            text("experience_name_raw"),
        ]
    elif fact_type == "language":
        values = [text("credential_name_raw"), text("score")]
    elif fact_type == "scholarship":
        values = [text("scholarship_name_raw"), text("scholarship_level")]
    else:
        values = [text("skill_display")]
    summary = " · ".join(value for value in values if value)
    return summary or "已提取简历事实"


def fact_evidence_map(snapshot: ResumeFactSnapshot | None) -> dict[str, ResumeScoreFactEvidence]:
    """Resolve every canonical fact in a snapshot to its readable summary.

    This is intentionally a small projection of persisted facts.  It gives the
    UI enough context to explain a decision while preserving the original facts
    version even after a resume is updated.
    """

    if snapshot is None:
        return {}
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    evidence: dict[str, ResumeScoreFactEvidence] = {}
    for field, fact_type in _CATEGORIES:
        entries = payload.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fact_id = _optional_string(entry.get("fact_id"))
            if fact_id is None:
                continue
            evidence[fact_id] = ResumeScoreFactEvidence(
                fact_id=fact_id,
                fact_type=fact_type,
                summary=fact_summary(fact_type=fact_type, entry=entry),
                evidence_block_ids=_string_list(entry.get("evidence_block_ids")),
            )
    return evidence


def resolve_fact_evidence_from_map(
    fact_evidence_by_id: Mapping[str, ResumeScoreFactEvidence],
    fact_ids: list[str],
) -> list[ResumeScoreFactEvidence]:
    """Resolve cited fact IDs against a precomputed evidence map, in order.

    Facts the snapshot does not know are silently dropped, mirroring the score
    feature: only grounded citations are ever surfaced as evidence.  Passing a
    map in lets a caller that resolves many requirements parse the snapshot
    once instead of once per requirement.
    """

    if not fact_ids:
        return []
    seen: set[str] = set()
    resolved: list[ResumeScoreFactEvidence] = []
    for fact_id in fact_ids:
        if fact_id in seen:
            continue
        seen.add(fact_id)
        evidence = fact_evidence_by_id.get(fact_id)
        if evidence is not None:
            resolved.append(evidence)
    return resolved


def resolve_fact_evidence(
    snapshot: ResumeFactSnapshot | None,
    fact_ids: list[str],
) -> list[ResumeScoreFactEvidence]:
    """Resolve cited fact IDs against a snapshot, preserving citation order."""

    return resolve_fact_evidence_from_map(fact_evidence_map(snapshot), fact_ids)
