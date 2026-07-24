from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.services import deepseek_provider as provider


def _clauses() -> list[dict[str, str]]:
    return [
        {
            "clause_id": "clause-001",
            "text": "Candidates must have explicit Python experience.",
        },
        {
            "clause_id": "clause-002",
            "text": "Cloud experience is preferred.",
        },
        {
            "clause_id": "clause-003",
            "text": "Our engineering team works across several time zones.",
        },
    ]


def _requirements() -> list[dict[str, object]]:
    return [
        {
            "requirement_id": "requirement-001",
            "requirement_text": "Explicit Python experience",
            "priority": "must_have",
            "clause_ids": ["clause-001"],
        },
        {
            "requirement_id": "requirement-002",
            "requirement_text": "Cloud experience",
            "priority": "preferred",
            "clause_ids": ["clause-002"],
        },
    ]


def _extracted_requirements() -> dict[str, object]:
    return {
        "schema_version": provider.JD_REQUIREMENTS_SCHEMA_VERSION,
        "clause_coverage": [
            {"clause_id": "clause-001", "requirement_ids": ["requirement-001"]},
            {"clause_id": "clause-002", "requirement_ids": ["requirement-002"]},
            {"clause_id": "clause-003", "requirement_ids": []},
        ],
        "requirements": _requirements(),
    }


def _generated_jd() -> dict[str, object]:
    return {
        "schema_version": provider.JD_GENERATION_SCHEMA_VERSION,
        "title": "Backend Engineer",
        "jd_text": (
            "Job Responsibilities\n"
            "Build and maintain backend services.\n\n"
            "Requirements\n"
            "Must have Python experience.\n"
            "Kubernetes experience is preferred."
        ),
        "requirements": {
            "must_have": ["Must have Python experience."],
            "preferred": ["Kubernetes experience is preferred."],
        },
    }


def _fact_snapshot() -> dict[str, object]:
    return {
        "schema_version": "resume_fact_snapshot.v2",
        "facts_schema_version": "resume_facts.v1",
        "education": [
            {
                "fact_id": "education-001",
                "school_name_raw": "Test University",
                "school_key": "test university",
                "school_match_state": "unresolved",
                "degree": "bachelor",
                "major_raw": "Computer Science",
                "major_key": "computer science",
                "start_month": "2018-09",
                "end_month": "2022-06",
                "evidence_block_ids": ["page-001"],
            }
        ],
        "experiences": [
            {
                "fact_id": "experience-001",
                "experience_type": "employment",
                "organization_name_raw": "Example Company",
                "organization_key": "example company",
                "title_raw": "Python Engineer",
                "title_key": "python engineer",
                "start_month": "2022-07",
                "end_month": "2024-06",
                "is_current": False,
                "evidence_block_ids": ["page-001"],
                "classification_evidence_block_ids": ["page-001"],
            }
        ],
        "skills": [
            {
                "fact_id": "skill-001",
                "skill_key": "python",
                "skill_display": "Python",
                "evidence_block_ids": ["page-001"],
            }
        ],
        "derived": {
            "is_985_211": False,
            "highest_degree": "bachelor",
            "employment_months": 24,
            "employment_or_internship_months": 24,
        },
        "source_block_ids": ["page-001"],
    }


def _match_output() -> dict[str, object]:
    return {
        "schema_version": provider.JD_MATCH_SCHEMA_VERSION,
        "requirement_matches": [
            {
                "requirement_id": "requirement-001",
                "status": "met",
                "rationale": "Python is listed as an explicit skill.",
                "fact_ids": ["skill-001"],
                "uncertainties": [],
            },
            {
                "requirement_id": "requirement-002",
                "status": "unknown",
                "rationale": "No cloud fact is present in the supplied snapshot.",
                "fact_ids": [],
                "uncertainties": ["Cloud experience is not explicit in the snapshot."],
            },
        ],
        "needs_human_review": True,
    }


def _profile_project_requirement() -> list[dict[str, object]]:
    return [
        {
            "requirement_id": "requirement-001",
            "requirement_text": "Documented WidgetFlow project delivery experience",
            "priority": "must_have",
            "clause_ids": ["clause-001"],
            "evidence_hint": (
                "Verify affirmative WidgetFlow use in a project, internship, or employment record."
            ),
            "evidence_policy": {
                "kind": "experience_detail_terms",
                "allowed_experience_types": ["project", "internship", "employment"],
                "terms_all_of": ["WidgetFlow"],
            },
        }
    ]


def _single_match_output(*, status: str, fact_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": provider.JD_MATCH_SCHEMA_VERSION,
        "requirement_matches": [
            {
                "requirement_id": "requirement-001",
                "status": status,
                "rationale": "Model supplied a source-cited result.",
                "fact_ids": fact_ids,
                "uncertainties": [],
            }
        ],
        "needs_human_review": False,
    }


def test_jd_extraction_schema_and_validation_require_complete_clause_coverage() -> None:
    schema = provider.jd_requirements_tool_schema(
        clause_ids=["clause-001", "clause-002", "clause-003"]
    )
    coverage_schema = schema["properties"]["clause_coverage"]
    requirement_schema = schema["properties"]["requirements"]["items"]
    assert coverage_schema["minItems"] == 3
    assert requirement_schema["properties"]["clause_ids"]["items"]["enum"] == [
        "clause-001",
        "clause-002",
        "clause-003",
    ]

    validated = provider.validate_jd_requirements_output(
        _extracted_requirements(),
        clauses=_clauses(),
    )
    assert [item["clause_id"] for item in validated["clause_coverage"]] == [
        "clause-001",
        "clause-002",
        "clause-003",
    ]

    broken_links = _extracted_requirements()
    broken_links["clause_coverage"][0]["requirement_ids"] = []  # type: ignore[index]
    with pytest.raises(provider.DeepSeekProviderError, match="jd_clause_coverage_links"):
        provider.validate_jd_requirements_output(broken_links, clauses=_clauses())


def test_jd_extraction_helper_accepts_only_structured_clauses_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return _extracted_requirements()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.extract_jd_requirements_from_clauses(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        clauses=_clauses(),
    )
    assert result["requirements"] == _requirements()
    assert calls[0]["function_name"] == "submit_jd_requirements"
    assert calls[0]["max_tokens"] == provider._jd_requirements_max_tokens(
        clauses=_clauses()
    )

    with pytest.raises(provider.DeepSeekProviderError, match="raw_pdf_not_allowed"):
        provider.extract_jd_requirements_from_clauses(
            api_key="not-used",
            model="not-used",
            timeout_seconds=1,
            clauses=[{"clause_id": "clause-001", "text": "%PDF-1.7 binary"}],
        )
    assert len(calls) == 1


def test_generated_jd_contract_returns_requirements_grounded_verbatim_in_jd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_call(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _generated_jd()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_jd_from_brief(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        title="Backend Engineer",
        brief="Build reliable services for a recruiting platform.",
    )

    assert result["title"] == "Backend Engineer"
    assert result["requirements"]["must_have"] == ["Must have Python experience."]
    assert captured["function_name"] == "submit_generated_jd"
    schema = captured["parameters_schema"]
    assert isinstance(schema, dict)
    assert schema["properties"]["requirements"]["required"] == [
        "must_have",
        "preferred",
    ]

    invalid = _generated_jd()
    invalid["requirements"]["must_have"] = ["Rust experience"]  # type: ignore[index]
    with pytest.raises(provider.DeepSeekProviderError, match="jd_generation_requirement_not_grounded"):
        provider.validate_generated_jd_output(invalid)

    normalized = _generated_jd()
    normalized["requirements"]["must_have"] = ["must have python experience"]  # type: ignore[index]
    assert provider.validate_generated_jd_output(normalized)["requirements"]["must_have"] == [
        "must have python experience"
    ]


def test_long_jd_requirement_extraction_receives_a_bounded_larger_token_budget() -> None:
    clauses = [
        {"clause_id": f"clause-{index:03d}", "text": "x" * 300}
        for index in range(1, 38)
    ]

    assert provider._jd_requirements_max_tokens(clauses=clauses) == 8000


def test_jd_match_contract_has_no_total_and_requires_exact_source_cited_statuses() -> None:
    schema = provider.jd_match_tool_schema(
        requirement_ids=["requirement-001", "requirement-002"],
        fact_ids=["education-001", "experience-001", "skill-001"],
    )
    assert "total" not in json.dumps(schema)
    match_item = schema["properties"]["requirement_matches"]["items"]
    assert match_item["properties"]["status"]["enum"] == [
        "met",
        "partial",
        "not_met",
        "unknown",
    ]

    validated = provider.validate_jd_match_output(
        _match_output(),
        confirmed_requirements=_requirements(),
        fact_ids=["education-001", "experience-001", "skill-001"],
    )
    assert [item["status"] for item in validated["requirement_matches"]] == [
        "met",
        "unknown",
    ]

    unknown_fact = _match_output()
    unknown_fact["requirement_matches"][0]["fact_ids"] = ["skill-999"]  # type: ignore[index]
    with pytest.raises(provider.DeepSeekProviderError, match="jd_match_fact_ids"):
        provider.validate_jd_match_output(
            unknown_fact,
            confirmed_requirements=_requirements(),
            fact_ids=["education-001", "experience-001", "skill-001"],
        )

    unsupported_not_met = _match_output()
    unsupported_not_met["requirement_matches"][1].update(  # type: ignore[index]
        {"status": "not_met", "uncertainties": []}
    )
    normalized = provider.validate_jd_match_output(
        unsupported_not_met,
        confirmed_requirements=_requirements(),
        fact_ids=["education-001", "experience-001", "skill-001"],
    )
    assert normalized["requirement_matches"][1]["status"] == "unknown"
    assert normalized["requirement_matches"][1]["fact_ids"] == []
    assert normalized["requirement_matches"][1]["uncertainties"]

    missing_requirement = _match_output()
    missing_requirement["requirement_matches"] = missing_requirement[
        "requirement_matches"
    ][0:1]  # type: ignore[index]
    with pytest.raises(
        provider.DeepSeekProviderError,
        match="jd_match_requirement_coverage",
    ):
        provider.validate_jd_match_output(
            missing_requirement,
            confirmed_requirements=_requirements(),
            fact_ids=["education-001", "experience-001", "skill-001"],
        )


def test_invalid_model_fact_ids_are_safely_downgraded_to_unknown() -> None:
    payload = _match_output()
    payload["requirement_matches"][0]["fact_ids"] = ["skill-999"]  # type: ignore[index]

    sanitized = provider._sanitize_jd_match_evidence_ids(
        payload,
        fact_ids=["education-001", "experience-001", "skill-001"],
    )
    validated = provider.validate_jd_match_output(
        sanitized,
        confirmed_requirements=_requirements(),
        fact_ids=["education-001", "experience-001", "skill-001"],
    )

    assert validated["requirement_matches"][0]["status"] == "unknown"
    assert validated["requirement_matches"][0]["fact_ids"] == []
    assert validated["requirement_matches"][0]["uncertainties"]


def test_experience_policy_preserves_a_matching_project_fact_and_reaches_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = "project"
    experience["title_raw"] = "Built a WidgetFlow orchestration project"
    experience["title_key"] = "built a widgetflow orchestration project"

    def fake_call(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _single_match_output(status="met", fact_ids=["experience-001"])

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "met"
    assert result["requirement_matches"][0]["fact_ids"] == ["experience-001"]
    assert "evidence_policy" in str(captured["user_prompt"])
    assert "experience_detail_terms" in str(captured["system_prompt"])


@pytest.mark.parametrize(
    ("experience_type", "title_raw", "fact_ids"),
    [
        ("employment", "Backend Engineer", ["skill-001"]),
        ("research", "WidgetFlow research assistant", ["experience-001"]),
        ("project", "Built an adjacent RAG workflow", ["experience-001"]),
    ],
)
def test_experience_policy_downgrades_skill_or_nonqualifying_evidence_to_review(
    monkeypatch: pytest.MonkeyPatch,
    experience_type: str,
    title_raw: str,
    fact_ids: list[str],
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = experience_type
    experience["title_raw"] = title_raw
    experience["title_key"] = title_raw.casefold()
    skills = snapshot["skills"]
    assert isinstance(skills, list)
    assert isinstance(skills[0], dict)
    skills[0]["skill_key"] = "widgetflow"
    skills[0]["skill_display"] = "WidgetFlow"

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=fact_ids),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "partial"
    assert result["needs_human_review"] is True


def test_experience_policy_uses_explicit_negated_project_fact_for_not_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = "project"
    experience["title_raw"] = "Built a RAG project without using WidgetFlow"
    experience["title_key"] = "built a rag project without using widgetflow"
    skills = snapshot["skills"]
    assert isinstance(skills, list)
    assert isinstance(skills[0], dict)
    skills[0]["skill_key"] = "widgetflow"
    skills[0]["skill_display"] = "WidgetFlow"

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=["skill-001"]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "not_met"
    assert result["requirement_matches"][0]["fact_ids"] == ["experience-001"]


def test_experience_policy_never_keeps_not_met_when_positive_project_evidence_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    experiences = snapshot["experiences"]
    assert isinstance(experiences, list)
    assert isinstance(experiences[0], dict)
    experiences[0].update(
        {
            "experience_type": "project",
            "title_raw": "RAG project without using WidgetFlow",
            "title_key": "rag project without using widgetflow",
        }
    )
    experiences.append(
        {
            **deepcopy(experiences[0]),
            "fact_id": "experience-002",
            "title_raw": "Built a WidgetFlow orchestration project",
            "title_key": "built a widgetflow orchestration project",
        }
    )

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="not_met", fact_ids=["experience-001"]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "unknown"
    assert result["requirement_matches"][0]["fact_ids"] == []
    assert result["needs_human_review"] is True


def test_jd_match_helper_rejects_raw_pdf_like_snapshot_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_call(**kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return _match_output()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    raw_snapshot = deepcopy(_fact_snapshot())
    raw_snapshot["raw_text"] = "%PDF-1.7"  # type: ignore[index]
    with pytest.raises(provider.DeepSeekProviderError, match="snapshot_unexpected_fields"):
        provider.match_resume_fact_snapshot_against_requirements(
            api_key="not-used",
            model="not-used",
            timeout_seconds=1,
            fact_snapshot=raw_snapshot,
            confirmed_requirements=_requirements(),
        )
    assert not called
