from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.deepseek_provider import (
    FACT_SNAPSHOT_SCHEMA_VERSION,
    DeepSeekProviderError,
    SCORE_SCHEMA_VERSION,
    SUMMARY_SCHEMA_VERSION,
    SUMMARY_SECTION_KEYS,
    _validate_fact_snapshot,
    resume_score_tool_schema,
    resume_summary_tool_schema,
    score_resume_fact_snapshot,
    summarize_resume_fact_snapshot,
    validate_resume_score_output,
    validate_resume_summary_output,
)


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


def _v3_fact_snapshot() -> dict[str, object]:
    snapshot = deepcopy(_fact_snapshot())
    snapshot["schema_version"] = FACT_SNAPSHOT_SCHEMA_VERSION
    snapshot["facts_schema_version"] = "resume_facts.v2"
    snapshot["language_credentials"] = []
    snapshot["scholarships"] = []
    snapshot["source_block_ids"] = ["page-001", "page-002"]
    education = snapshot["education"][0]
    assert isinstance(education, dict)
    education.update(
        {
            "institution_tiers": [],
            "institution_classification": None,
            "classification_basis": None,
            "classification_registry_version": None,
            "classification_evidence_block_ids": [],
            "average_score": None,
            "gpa_value": None,
            "gpa_scale": None,
            "gpa_percent": None,
            "rank_position": None,
            "rank_total": None,
            "rank_percent": None,
        }
    )
    experience = snapshot["experiences"][0]
    assert isinstance(experience, dict)
    experience.update(
        {
            "experience_name_raw": "Recruiting Data Platform",
            "experience_name_key": "recruiting data platform",
            "detail_items": [
                {
                    "detail_raw": "Built source-cited candidate ingestion",
                    "evidence_block_ids": ["page-002"],
                }
            ],
            "leadership_context": None,
            "leadership_role": None,
            "award_level": None,
            "award_result_raw": None,
        }
    )
    skill = snapshot["skills"][0]
    assert isinstance(skill, dict)
    skill["skill_category"] = None
    return snapshot


def _dimensions() -> list[dict[str, object]]:
    return [
        {
            "key": "skills",
            "label": "核心技能",
            "weight": 40,
            "guidance": "Assess only explicit skills.",
        },
        {
            "key": "experience",
            "label": "相关经历",
            "weight": 60,
            "guidance": None,
        },
    ]


def _fact_ids() -> list[str]:
    return ["education-001", "experience-001", "skill-001"]


def _valid_score_output() -> dict[str, object]:
    return {
        "schema_version": SCORE_SCHEMA_VERSION,
        "dimension_scores": [
            {
                "key": "skills",
                "raw_score": 32,
                "rationale": "简历明确列出了 Python 技能。",
                "fact_ids": ["skill-001"],
                "uncertainties": [],
            },
            {
                "key": "experience",
                "raw_score": 48,
                "rationale": "简历中有一条明确的正式工作经历。",
                "fact_ids": ["experience-001"],
                "uncertainties": ["尚未说明与目标行业的直接相关性。"],
            },
        ],
        "overall_summary": "现有事实显示候选人具备 Python 技能并拥有一段工作经历。",
        "risk_flags": [
            {
                "message": "需要在后续沟通中核实与目标行业的相关性。",
                "fact_ids": ["experience-001"],
            }
        ],
        "needs_human_review": True,
    }


def _english_score_output() -> dict[str, object]:
    payload = _valid_score_output()
    dimensions = payload["dimension_scores"]
    assert isinstance(dimensions, list)
    first = dimensions[0]
    second = dimensions[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first["rationale"] = "Python is explicitly listed."
    second["rationale"] = "The snapshot has one employment record."
    second["uncertainties"] = ["No domain relevance is stated."]
    payload["overall_summary"] = "The factual record shows Python and one role."
    risk_flags = payload["risk_flags"]
    assert isinstance(risk_flags, list)
    risk_flag = risk_flags[0]
    assert isinstance(risk_flag, dict)
    risk_flag["message"] = "Domain relevance needs verification."
    return payload


def _valid_summary_output() -> dict[str, object]:
    sections = {
        section_key: {
            "content": "Information is unavailable in the fact snapshot.",
            "fact_ids": [],
        }
        for section_key in SUMMARY_SECTION_KEYS
    }
    sections["candidate_positioning"] = {
        "content": "Python-oriented candidate with one recorded role.",
        "fact_ids": ["experience-001", "skill-001"],
    }
    sections["education_background"] = {
        "content": "Bachelor degree in Computer Science.",
        "fact_ids": ["education-001"],
    }
    return {"schema_version": SUMMARY_SCHEMA_VERSION, "sections": sections}


def _valid_chinese_summary_output() -> dict[str, object]:
    payload = _valid_summary_output()
    sections = payload["sections"]
    assert isinstance(sections, dict)
    for section in sections.values():
        assert isinstance(section, dict)
        section["content"] = "简历信息不足。"
    sections["candidate_positioning"] = {
        "content": "候选人具备 Python 相关能力和一段明确工作经历。",
        "fact_ids": ["experience-001", "skill-001"],
    }
    return payload


def test_score_schema_is_bound_to_supplied_dimension_and_fact_ids() -> None:
    schema = resume_score_tool_schema(
        dimension_keys=["skills", "experience"],
        fact_ids=_fact_ids(),
    )
    item_schema = schema["properties"]["dimension_scores"]["items"]
    assert item_schema["properties"]["key"]["enum"] == ["skills", "experience"]
    assert item_schema["properties"]["fact_ids"]["items"]["enum"] == _fact_ids()

    with pytest.raises(DeepSeekProviderError, match="dimension_keys"):
        resume_score_tool_schema(
            dimension_keys=["skills", "skills"],
            fact_ids=_fact_ids(),
        )


def test_score_output_requires_each_unique_dimension_and_known_fact_ids() -> None:
    validated = validate_resume_score_output(
        _valid_score_output(),
        dimensions=_dimensions(),
        fact_ids=_fact_ids(),
    )
    assert [item["key"] for item in validated["dimension_scores"]] == [
        "skills",
        "experience",
    ]

    duplicate_key = _valid_score_output()
    duplicate_key["dimension_scores"][1]["key"] = "skills"  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="score_dimension_key"):
        validate_resume_score_output(
            duplicate_key,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    missing_key = _valid_score_output()
    missing_key["dimension_scores"] = missing_key["dimension_scores"][:1]  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="score_dimension_keys"):
        validate_resume_score_output(
            missing_key,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    unknown_fact = _valid_score_output()
    unknown_fact["dimension_scores"][0]["fact_ids"] = ["skill-999"]  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="score_fact_ids"):
        validate_resume_score_output(
            unknown_fact,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )


def test_score_output_rejects_scores_above_hundred() -> None:
    invalid_score = _valid_score_output()
    invalid_score["dimension_scores"][0]["raw_score"] = 101  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="score_value"):
        validate_resume_score_output(
            invalid_score,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )


def test_score_output_rejects_english_recruiter_text() -> None:
    invalid_rationale = _valid_score_output()
    rationale_dimensions = invalid_rationale["dimension_scores"]
    assert isinstance(rationale_dimensions, list)
    assert isinstance(rationale_dimensions[0], dict)
    rationale_dimensions[0]["rationale"] = "Python is explicitly listed."
    with pytest.raises(DeepSeekProviderError, match="score_rationale_language"):
        validate_resume_score_output(
            invalid_rationale,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    mixed_language_rationale = _valid_score_output()
    mixed_dimensions = mixed_language_rationale["dimension_scores"]
    assert isinstance(mixed_dimensions, list)
    assert isinstance(mixed_dimensions[0], dict)
    mixed_dimensions[0]["rationale"] = "Python is explicitly listed。中文提示。"
    with pytest.raises(DeepSeekProviderError, match="score_rationale_language"):
        validate_resume_score_output(
            mixed_language_rationale,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    invalid_uncertainty = _valid_score_output()
    uncertainty_dimensions = invalid_uncertainty["dimension_scores"]
    assert isinstance(uncertainty_dimensions, list)
    assert isinstance(uncertainty_dimensions[1], dict)
    uncertainty_dimensions[1]["uncertainties"] = ["No domain relevance is stated."]
    with pytest.raises(DeepSeekProviderError, match="score_uncertainties_language"):
        validate_resume_score_output(
            invalid_uncertainty,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    invalid_summary = _valid_score_output()
    invalid_summary["overall_summary"] = "The factual record shows Python and one role."
    with pytest.raises(DeepSeekProviderError, match="score_overall_summary_language"):
        validate_resume_score_output(
            invalid_summary,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )

    invalid_risk = _valid_score_output()
    risk_flags = invalid_risk["risk_flags"]
    assert isinstance(risk_flags, list)
    assert isinstance(risk_flags[0], dict)
    risk_flags[0]["message"] = "Domain relevance needs verification."
    with pytest.raises(DeepSeekProviderError, match="score_risk_flag_message_language"):
        validate_resume_score_output(
            invalid_risk,
            dimensions=_dimensions(),
            fact_ids=_fact_ids(),
        )


def test_score_prompt_requires_simplified_chinese_content(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_provider_call(**kwargs):
        captured.update(kwargs)
        return _valid_score_output()

    monkeypatch.setattr(
        "app.services.deepseek_provider.call_strict_function",
        fake_provider_call,
    )

    score_resume_fact_snapshot(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=_fact_snapshot(),
        dimensions=_dimensions(),
    )

    assert "简体中文" in str(captured["system_prompt"])
    assert "不得输出英文完整句" in str(captured["system_prompt"])
    assert "输出语言要求" in str(captured["user_prompt"])


def test_score_retries_once_to_correct_english_output(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_provider_call(**kwargs):
        calls.append(kwargs)
        return _english_score_output() if len(calls) == 1 else _valid_score_output()

    monkeypatch.setattr(
        "app.services.deepseek_provider.call_strict_function",
        fake_provider_call,
    )

    result = score_resume_fact_snapshot(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=_fact_snapshot(),
        dimensions=_dimensions(),
    )

    assert result["overall_summary"].startswith("现有事实")
    assert len(calls) == 2
    assert "纠正重试" in str(calls[1]["system_prompt"])


def test_score_rejects_two_english_outputs_after_one_correction(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_provider_call(**kwargs):
        calls.append(kwargs)
        return _english_score_output()

    monkeypatch.setattr(
        "app.services.deepseek_provider.call_strict_function",
        fake_provider_call,
    )

    with pytest.raises(DeepSeekProviderError, match="score_rationale_language"):
        score_resume_fact_snapshot(
            api_key="not-used",
            model="not-used",
            timeout_seconds=1,
            fact_snapshot=_fact_snapshot(),
            dimensions=_dimensions(),
        )
    assert len(calls) == 2


def test_summary_schema_and_output_require_fixed_sections_and_known_citations() -> None:
    schema = resume_summary_tool_schema(fact_ids=_fact_ids())
    assert schema["properties"]["sections"]["required"] == list(SUMMARY_SECTION_KEYS)

    validated = validate_resume_summary_output(
        _valid_summary_output(),
        fact_ids=_fact_ids(),
    )
    assert set(validated["sections"]) == set(SUMMARY_SECTION_KEYS)

    missing_section = _valid_summary_output()
    del missing_section["sections"]["strengths"]  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="summary_section_keys"):
        validate_resume_summary_output(missing_section, fact_ids=_fact_ids())

    unknown_fact = _valid_summary_output()
    unknown_fact["sections"]["core_skills"]["fact_ids"] = ["skill-999"]  # type: ignore[index]
    with pytest.raises(DeepSeekProviderError, match="summary_section_fact_ids"):
        validate_resume_summary_output(unknown_fact, fact_ids=_fact_ids())

    with pytest.raises(DeepSeekProviderError, match="summary_section_language"):
        validate_resume_summary_output(
            _valid_summary_output(),
            fact_ids=_fact_ids(),
            require_simplified_chinese=True,
        )
    chinese_validated = validate_resume_summary_output(
        _valid_chinese_summary_output(),
        fact_ids=_fact_ids(),
        require_simplified_chinese=True,
    )
    assert chinese_validated["sections"]["candidate_positioning"]["content"].startswith("候选人")


def test_summary_prompt_requires_simplified_chinese_content(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_provider_call(**kwargs):
        captured.update(kwargs)
        return _valid_chinese_summary_output()

    monkeypatch.setattr(
        "app.services.deepseek_provider.call_strict_function",
        fake_provider_call,
    )

    summarize_resume_fact_snapshot(
        api_key="not-used",
        model="not-used",
        timeout_seconds=1,
        fact_snapshot=_fact_snapshot(),
    )

    assert "Simplified Chinese" in str(captured["system_prompt"])
    assert "每个 content 都必须为简体中文" in str(captured["user_prompt"])


def test_score_helper_rejects_raw_pdf_like_input_before_any_provider_call() -> None:
    raw_input = deepcopy(_fact_snapshot())
    raw_input["raw_text"] = "This must never be sent to the provider."

    with pytest.raises(DeepSeekProviderError, match="snapshot_unexpected_fields"):
        score_resume_fact_snapshot(
            api_key="not-used",
            model="not-used",
            timeout_seconds=1,
            fact_snapshot=raw_input,
            dimensions=_dimensions(),
        )


def test_fact_snapshot_validator_accepts_legacy_and_current_fact_snapshots() -> None:
    v2, v2_fact_ids = _validate_fact_snapshot(_fact_snapshot())
    v3, v3_fact_ids = _validate_fact_snapshot(_v3_fact_snapshot())
    legacy_v4 = deepcopy(_v3_fact_snapshot())
    legacy_v4["schema_version"] = "resume_fact_snapshot.v4"
    education = legacy_v4["education"][0]
    assert isinstance(education, dict)
    for key in (
        "institution_classification",
        "classification_basis",
        "classification_registry_version",
        "classification_evidence_block_ids",
    ):
        education.pop(key)
    validated_v4, v4_fact_ids = _validate_fact_snapshot(legacy_v4)

    assert v2["schema_version"] == "resume_fact_snapshot.v2"
    assert v3["schema_version"] == FACT_SNAPSHOT_SCHEMA_VERSION
    assert validated_v4["schema_version"] == "resume_fact_snapshot.v4"
    assert v2_fact_ids == v3_fact_ids == v4_fact_ids == _fact_ids()
    assert v3["experiences"][0]["detail_items"] == [
        {
            "detail_raw": "Built source-cited candidate ingestion",
            "evidence_block_ids": ["page-002"],
        }
    ]


def test_fact_snapshot_validator_accepts_demographic_derived_fields() -> None:
    # #194 added gender/birth_date to the derived block written by
    # _canonical_fact_payload. The validator must accept that current form as
    # well as the pre-demographic four-key form covered above.
    snapshot = _fact_snapshot()
    derived = snapshot["derived"]
    assert isinstance(derived, dict)
    derived["gender"] = "male"
    derived["birth_date"] = "2002-02-01"
    validated, _ = _validate_fact_snapshot(snapshot)
    assert validated["derived"]["gender"] == "male"
    assert validated["derived"]["birth_date"] == "2002-02-01"
