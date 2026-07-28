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


def _generated_talent_profile() -> dict[str, object]:
    return {
        "schema_version": provider.TALENT_SEARCH_PROFILE_SCHEMA_VERSION,
        "title": "AI 应用工程师人才画像",
        "summary": "先按明确条件召回，再核验简历事实。",
        "hard_filters": {
            "institution_classifications_any_of": [],
            "education_degree_in": [],
            "highest_degree_in": [],
            "graduation_status": "any",
            "fresh_graduate_start_month": None,
            "fresh_graduate_end_month": None,
            "min_employment_months": None,
            "min_employment_or_internship_months": None,
            "experience_types_all_of": [],
            "skills_all_of": [],
            "language_credentials_all_of": [],
        },
        "verification_requirements": [
            {
                "key": "agent_delivery",
                "label": "具备 Agent 交付经历",
                "evidence_hint": "核验项目职责、技术方案和结果。",
                "evidence_policy": {
                    "kind": "any_fact",
                    "allowed_experience_types": [],
                    "terms_all_of": [],
                "terms_any_of": [],
                },
            }
        ],
        "preferred_requirements": [],
        "aliases": ["AI 应用工程师"],
        "clarifying_questions": [],
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
            "terms_any_of": [],
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


def _profile_any_project_requirement() -> list[dict[str, object]]:
    return [
        {
            "requirement_id": "requirement-001",
            "requirement_text": "Documented LangChain or LlamaIndex project delivery experience",
            "priority": "must_have",
            "clause_ids": ["clause-001"],
            "evidence_hint": "Verify affirmative use of either named framework in one project.",
            "evidence_policy": {
                "kind": "experience_detail_terms",
                "allowed_experience_types": ["project"],
                "terms_all_of": [],
                "terms_any_of": ["LangChain", "LlamaIndex"],
            },
        }
    ]


def test_talent_profile_generation_retries_one_invalid_structured_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if len(calls) == 1:
            return {"schema_version": provider.TALENT_SEARCH_PROFILE_SCHEMA_VERSION}
        return _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="寻找有 Agent 项目经验的工程师",
    )

    assert result["title"] == "AI 应用工程师人才画像"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 3200
    assert "correction retry" in str(calls[1]["system_prompt"])


def test_talent_profile_refinement_instructs_the_model_how_to_condense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="请精简当前人才画像",
        previous_profile={
            "title": "AI 应用工程师人才画像",
            "summary": "旧画像摘要",
            "hard_filters": {"skills_all_of": ["Python"]},
            "verification_requirements": [],
            "preferred_requirements": [],
            "aliases": [],
            "clarifying_questions": [],
        },
    )

    assert len(calls) == 1
    assert "preserve its hiring target and every explicit hard filter" in str(
        calls[0]["system_prompt"]
    )
    assert "remove duplicated, vague, or nonessential wording" in str(
        calls[0]["system_prompt"]
    )
    assert "All recruiter-visible title, summary, requirement label" in str(
        calls[0]["system_prompt"]
    )
    assert "Current draft to refine" in str(calls[0]["user_prompt"])


@pytest.mark.parametrize(
    ("field", "error_code"),
    [
        ("title", "talent_profile_title_language"),
        ("summary", "talent_profile_summary_language"),
        (
            "verification_label",
            "talent_profile_verification_requirements_label_language",
        ),
        (
            "verification_hint",
            "talent_profile_verification_requirements_evidence_hint_language",
        ),
        ("alias", "talent_profile_aliases_language"),
        ("question", "talent_profile_clarifying_questions_language"),
    ],
)
def test_talent_profile_rejects_english_recruiter_visible_text(
    field: str,
    error_code: str,
) -> None:
    invalid = _generated_talent_profile()
    english_sentence = "No experience fact of allowed types explicitly mentions this requirement."
    if field == "title":
        invalid["title"] = english_sentence
    elif field == "summary":
        invalid["summary"] = english_sentence
    elif field == "verification_label":
        requirements = invalid["verification_requirements"]
        assert isinstance(requirements, list)
        assert isinstance(requirements[0], dict)
        requirements[0]["label"] = english_sentence
    elif field == "verification_hint":
        requirements = invalid["verification_requirements"]
        assert isinstance(requirements, list)
        assert isinstance(requirements[0], dict)
        requirements[0]["evidence_hint"] = english_sentence
    elif field == "alias":
        invalid["aliases"] = [english_sentence]
    elif field == "question":
        invalid["clarifying_questions"] = [english_sentence]
    else:  # pragma: no cover - keeps future parametrization exhaustive.
        raise AssertionError(f"unsupported field: {field}")

    with pytest.raises(provider.DeepSeekProviderError, match=error_code):
        provider.validate_talent_search_profile_output(invalid)


def test_talent_profile_generation_retries_english_visible_prose_in_chinese(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    english_hint = _generated_talent_profile()
    requirements = english_hint["verification_requirements"]
    assert isinstance(requirements, list)
    assert isinstance(requirements[0], dict)
    requirements[0]["evidence_hint"] = (
        "No experience fact of allowed types explicitly mentions this requirement."
    )

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return english_hint if len(calls) == 1 else _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="寻找有 Agent 项目经验的工程师",
    )

    assert result["verification_requirements"][0]["evidence_hint"] == "核验项目职责、技术方案和结果。"
    assert len(calls) == 2
    assert "All recruiter-visible prose must be Simplified Chinese" in str(
        calls[1]["system_prompt"]
    )


def test_talent_profile_generation_retries_when_requirement_omits_evidence_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    incomplete = _generated_talent_profile()
    requirement = incomplete["verification_requirements"][0]
    assert isinstance(requirement, dict)
    del requirement["evidence_policy"]

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return incomplete if len(calls) == 1 else _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="寻找有 Agent 项目经验的工程师",
    )

    assert result["verification_requirements"][0]["evidence_policy"]["kind"] == "any_fact"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 3200


def test_talent_profile_generation_retries_when_evidence_policy_omits_term_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    incomplete = _generated_talent_profile()
    requirement = incomplete["verification_requirements"][0]
    assert isinstance(requirement, dict)
    policy = requirement["evidence_policy"]
    assert isinstance(policy, dict)
    del policy["terms_any_of"]

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return incomplete if len(calls) == 1 else _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="寻找有 Agent 项目经验的工程师",
    )

    assert result["verification_requirements"][0]["evidence_policy"]["terms_any_of"] == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "error_code",
    ["ai_provider_provider_5xx", "ai_provider_truncated", "ai_provider_structured_invalid"],
)
def test_talent_profile_generation_retries_one_transient_gateway_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise provider.DeepSeekProviderError(error_code)
        return _generated_talent_profile()

    monkeypatch.setattr(provider, "call_strict_function", fake_call)
    result = provider.generate_talent_search_profile(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        request_message="寻找有 Agent 项目经验的工程师",
    )

    assert result["title"] == "AI 应用工程师人才画像"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] == 3200


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


def test_experience_policy_accepts_matching_detail_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    snapshot["schema_version"] = "resume_fact_snapshot.v3"
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = "project"
    experience["title_raw"] = "Backend platform delivery"
    experience["title_key"] = "backend platform delivery"
    experience["experience_name_raw"] = "Candidate platform project"
    experience["experience_name_key"] = "candidate platform project"
    experience["detail_items"] = [
        {
            "detail_raw": "Implemented the WidgetFlow orchestration path and shipped it.",
            "evidence_block_ids": ["page-001"],
        }
    ]

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=["experience-001"]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "met"


@pytest.mark.parametrize(
    "title_raw",
    [
        "Built a LangChain orchestration project",
        "Built a LlamaIndex retrieval project",
    ],
)
def test_experience_policy_any_of_accepts_one_named_term_in_one_project(
    monkeypatch: pytest.MonkeyPatch,
    title_raw: str,
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience.update(
        {
            "experience_type": "project",
            "title_raw": title_raw,
            "title_key": title_raw.casefold(),
        }
    )

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="unknown", fact_ids=[]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_any_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "met"
    assert result["requirement_matches"][0]["fact_ids"] == ["experience-001"]


def test_experience_policy_all_of_requires_one_fact_to_show_every_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    experiences = snapshot["experiences"]
    assert isinstance(experiences, list)
    assert isinstance(experiences[0], dict)
    experiences[0].update(
        {
            "experience_type": "project",
            "title_raw": "Built a LangChain orchestration project",
            "title_key": "built a langchain orchestration project",
        }
    )
    experiences.append(
        {
            **deepcopy(experiences[0]),
            "fact_id": "experience-002",
            "title_raw": "Built a RAG evaluation project",
            "title_key": "built a rag evaluation project",
        }
    )
    requirements = _profile_project_requirement()
    requirements[0]["evidence_policy"] = {
        "kind": "experience_detail_terms",
        "allowed_experience_types": ["project"],
        "terms_all_of": ["LangChain", "RAG"],
        "terms_any_of": [],
    }

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=["experience-001"]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=requirements,
    )

    assert result["requirement_matches"][0]["status"] == "partial"
    assert result["needs_human_review"] is True


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


@pytest.mark.parametrize(
    "title_raw",
    [
        "Built a RAG project without using WidgetFlow",
        "This was not a WidgetFlow project.",
        "The project did not adopt WidgetFlow.",
        "The project did not include WidgetFlow.",
        "The project never adopted WidgetFlow.",
        "This project lacks WidgetFlow.",
        "The project excluded WidgetFlow.",
        "No WidgetFlow integration was used in the project.",
        "WidgetFlow is not part of the tech stack.",
        "WidgetFlow was absent from this project.",
        "Not a WidgetFlow project.",
        "WidgetFlow-free project.",
        "This project has no dependency on WidgetFlow.",
        "WidgetFlow wasn't used in this project.",
        "WidgetFlow was never used in this project.",
        "The project was not built with WidgetFlow.",
        "The project did not deploy WidgetFlow.",
        "The project did not rely on WidgetFlow.",
        "WidgetFlow was not selected.",
        "WidgetFlow is unsupported in the project.",
        "WidgetFlow is unavailable for this project.",
        "WidgetFlow was not enabled for this project.",
        "WidgetFlow was not configured in this project.",
        "WidgetFlow was not deployed with this project.",
        "WidgetFlow was not included in this project.",
        "WidgetFlow was not utilized by this project.",
        "The project omitted WidgetFlow.",
        "The project did not contain WidgetFlow.",
        "WidgetFlow was never part of this project.",
        "The project chose not to use WidgetFlow.",
        "The project doesn't use WidgetFlow.",
        "WidgetFlow was disabled for this project.",
        "The project decided against using WidgetFlow.",
        "The project opted out of using WidgetFlow.",
        "WidgetFlow was ruled out for this project.",
        "WidgetFlow was deliberately not used in this project.",
        "WidgetFlow could not be used in this project.",
        "Use of WidgetFlow was prohibited in this project.",
        "WidgetFlow was neither used nor supported in this project.",
    ],
)
def test_experience_policy_uses_explicit_negated_project_fact_for_not_met(
    monkeypatch: pytest.MonkeyPatch,
    title_raw: str,
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = "project"
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


@pytest.mark.parametrize(
    "title_raw",
    [
        "不仅使用 WidgetFlow，还完成了项目交付",
        "不但使用 WidgetFlow，还负责了系统上线",
    ],
)
def test_experience_policy_does_not_treat_not_only_as_negation(
    monkeypatch: pytest.MonkeyPatch,
    title_raw: str,
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience["experience_type"] = "project"
    experience["title_raw"] = title_raw
    experience["title_key"] = title_raw

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=["experience-001"]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "met"


def test_experience_policy_uses_word_safe_named_term_matching() -> None:
    assert not provider._experience_policy_term_occurs("Maintained email platform", "AI")
    assert not provider._experience_policy_term_occurs("Built a GraphRAG pipeline", "RAG")
    assert provider._experience_policy_term_occurs(
        "Built a Lang Chain project",
        "LangChain",
    )


@pytest.mark.parametrize(
    "text",
    [
        "使用非 WidgetFlow 的 RAG 框架",
        "The project used a non-WidgetFlow framework.",
        "Other than WidgetFlow, the project used a custom workflow.",
        "The project used another framework instead of WidgetFlow.",
        "WidgetFlow 不是本项目的技术栈。",
        "The project did not include WidgetFlow.",
        "The project never adopted WidgetFlow.",
        "This project lacks WidgetFlow.",
        "The project excluded WidgetFlow.",
        "WidgetFlow was absent from this project.",
        "Not a WidgetFlow project.",
        "WidgetFlow-free project.",
        "This project has no dependency on WidgetFlow.",
        "WidgetFlow wasn't used in this project.",
        "WidgetFlow was never used in this project.",
        "The project was not built with WidgetFlow.",
        "The project did not deploy WidgetFlow.",
        "The project did not rely on WidgetFlow.",
        "WidgetFlow was not selected.",
        "WidgetFlow is unsupported in the project.",
        "WidgetFlow is unavailable for this project.",
        "WidgetFlow was not enabled for this project.",
        "WidgetFlow was not configured in this project.",
        "WidgetFlow was not deployed with this project.",
        "WidgetFlow was not included in this project.",
        "WidgetFlow was not utilized by this project.",
        "The project omitted WidgetFlow.",
        "The project did not contain WidgetFlow.",
        "WidgetFlow was never part of this project.",
        "The project chose not to use WidgetFlow.",
        "The project doesn't use WidgetFlow.",
        "WidgetFlow was disabled for this project.",
        "The project decided against using WidgetFlow.",
        "The project opted out of using WidgetFlow.",
        "WidgetFlow was ruled out for this project.",
        "WidgetFlow was deliberately not used in this project.",
        "WidgetFlow could not be used in this project.",
        "Use of WidgetFlow was prohibited in this project.",
        "WidgetFlow was neither used nor supported in this project.",
    ],
)
def test_experience_policy_detects_direct_named_term_negation(text: str) -> None:
    affirmative, negated = provider._experience_term_polarities(text, "WidgetFlow")
    assert affirmative is False
    assert negated is True


@pytest.mark.parametrize(
    "text",
    [
        "未使用其他框架，但使用 WidgetFlow 完成交付",
        "没有使用 RAG，而是使用 WidgetFlow 完成交付",
        "Without using CrewAI, used WidgetFlow in the project",
    ],
)
def test_experience_policy_does_not_attach_other_technology_negation(text: str) -> None:
    affirmative, negated = provider._experience_term_polarities(text, "WidgetFlow")
    assert affirmative is True
    assert negated is False


def test_experience_policy_tracks_mixed_positive_and_negated_occurrences() -> None:
    affirmative, negated = provider._experience_term_polarities(
        "未使用 WidgetFlow，后续使用 WidgetFlow 完成上线",
        "WidgetFlow",
    )
    assert affirmative is True
    assert negated is True


def test_experience_policy_does_not_prove_technology_from_organization_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    snapshot["schema_version"] = "resume_fact_snapshot.v3"
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience.update(
        {
            "experience_type": "project",
            "experience_name_raw": "Customer service platform",
            "experience_name_key": "customer service platform",
            "organization_name_raw": "WidgetFlow Technologies",
            "title_raw": "Backend Engineer",
            "title_key": "backend engineer",
            "detail_items": [],
        }
    )

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="met", fact_ids=["experience-001"]),
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


def test_experience_policy_promotes_server_verified_fact_over_model_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _fact_snapshot()
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience.update(
        {
            "experience_type": "project",
            "title_raw": "WidgetFlow orchestration project",
            "title_key": "widgetflow orchestration project",
        }
    )

    monkeypatch.setattr(
        provider,
        "call_strict_function",
        lambda **_kwargs: _single_match_output(status="unknown", fact_ids=[]),
    )
    result = provider.match_resume_fact_snapshot_against_requirements(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=snapshot,
        confirmed_requirements=_profile_project_requirement(),
    )

    assert result["requirement_matches"][0]["status"] == "met"
    assert result["requirement_matches"][0]["fact_ids"] == ["experience-001"]
    assert result["needs_human_review"] is True


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

    assert result["requirement_matches"][0]["status"] == "met"
    assert result["requirement_matches"][0]["fact_ids"] == ["experience-002"]
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
