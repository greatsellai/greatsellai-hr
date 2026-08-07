from __future__ import annotations

import json

from app.models import ResumeFactSnapshot
from app.schemas import ResumeScoreFactEvidence
from app.services import fact_evidence


def _snapshot(payload: dict[str, object]) -> ResumeFactSnapshot:
    return ResumeFactSnapshot(canonical_facts_json=json.dumps(payload, ensure_ascii=False))


_CANONICAL_PAYLOAD: dict[str, object] = {
    "education": [
        {
            "fact_id": "education-001",
            "school_name_raw": "清华大学",
            "degree": "bachelor",
            "major_raw": "计算机",
            "evidence_block_ids": ["block-a"],
        },
    ],
    "experiences": [
        {
            "fact_id": "experience-001",
            "experience_type": "employment",
            "organization_name_raw": "Acme",
            "title_raw": "Python Engineer",
            "experience_name_raw": None,
            "evidence_block_ids": ["block-b"],
        },
    ],
    "skills": [
        {"fact_id": "skill-001", "skill_display": "Python", "evidence_block_ids": ["block-c"]},
    ],
    "language_credentials": [
        {
            "fact_id": "language-001",
            "credential_name_raw": "大学英语六级",
            "score": "550",
            "evidence_block_ids": ["block-d"],
        },
    ],
    "scholarships": [
        {
            "fact_id": "scholarship-001",
            "scholarship_name_raw": "国家奖学金",
            "scholarship_level": "national",
            "evidence_block_ids": ["block-e"],
        },
    ],
}


def test_fact_evidence_map_resolves_all_canonical_categories() -> None:
    by_id = fact_evidence.fact_evidence_map(_snapshot(_CANONICAL_PAYLOAD))

    assert list(by_id) == [
        "education-001",
        "experience-001",
        "skill-001",
        "language-001",
        "scholarship-001",
    ]
    assert by_id["education-001"] == ResumeScoreFactEvidence(
        fact_id="education-001",
        fact_type="education",
        summary="清华大学 · bachelor · 计算机",
        evidence_block_ids=["block-a"],
    )
    assert by_id["experience-001"] == ResumeScoreFactEvidence(
        fact_id="experience-001",
        fact_type="experience",
        summary="employment · Acme · Python Engineer",
        evidence_block_ids=["block-b"],
    )
    assert by_id["skill-001"] == ResumeScoreFactEvidence(
        fact_id="skill-001",
        fact_type="skill",
        summary="Python",
        evidence_block_ids=["block-c"],
    )
    assert by_id["language-001"] == ResumeScoreFactEvidence(
        fact_id="language-001",
        fact_type="language",
        summary="大学英语六级 · 550",
        evidence_block_ids=["block-d"],
    )
    assert by_id["scholarship-001"] == ResumeScoreFactEvidence(
        fact_id="scholarship-001",
        fact_type="scholarship",
        summary="国家奖学金 · national",
        evidence_block_ids=["block-e"],
    )


def test_resolve_fact_evidence_preserves_citation_order_and_deduplicates() -> None:
    resolved = fact_evidence.resolve_fact_evidence(
        _snapshot(_CANONICAL_PAYLOAD),
        ["skill-001", "education-001", "skill-001", "language-001", "missing-001"],
    )

    assert [item.fact_id for item in resolved] == [
        "skill-001",
        "education-001",
        "language-001",
    ]
    assert [item.summary for item in resolved] == ["Python", "清华大学 · bachelor · 计算机", "大学英语六级 · 550"]


def test_resolve_fact_evidence_without_ids_or_snapshot_is_empty() -> None:
    assert fact_evidence.resolve_fact_evidence(_snapshot(_CANONICAL_PAYLOAD), []) == []
    assert fact_evidence.resolve_fact_evidence(None, ["skill-001"]) == []


def test_fact_evidence_map_tolerates_missing_and_malformed_snapshots() -> None:
    assert fact_evidence.fact_evidence_map(None) == {}
    assert fact_evidence.fact_evidence_map(_snapshot({})) == {}
    assert fact_evidence.fact_evidence_map(_snapshot(["not", "a", "dict"])) == {}
    broken = ResumeFactSnapshot(canonical_facts_json="{not-valid-json")
    assert fact_evidence.fact_evidence_map(broken) == {}


def test_fact_evidence_map_skips_entries_without_a_fact_id() -> None:
    payload: dict[str, object] = {
        "skills": [
            {"skill_display": "Python", "evidence_block_ids": ["block-a"]},
            {"fact_id": "skill-001", "skill_display": "Go", "evidence_block_ids": ["block-b"]},
        ]
    }
    assert list(fact_evidence.fact_evidence_map(_snapshot(payload))) == ["skill-001"]
