from __future__ import annotations

import json

import pytest

from app.services.deepseek_provider import (
    _downgrade_incomplete_work_experiences,
    _flatten_evidence_block_ids,
    call_strict_function,
    DeepSeekProviderError,
    EvidenceBlock,
    extract_resume_core_facts,
    extract_resume_facts,
    legacy_direct_transport_for_testing,
    redact_nonessential_personal_data,
    render_evidence_blocks,
    resume_core_facts_tool_schema,
    resume_facts_tool_schema,
)
from app.services.institution_service import build_985_211_ai_rulebook


@pytest.fixture(autouse=True)
def _enable_retired_transport_only_for_legacy_protocol_contracts():
    """Keep direct-HTTP assertions isolated from application AI paths."""

    with legacy_direct_transport_for_testing():
        yield


def test_sensitive_contact_data_is_redacted_before_model_input() -> None:
    rendered = redact_nonessential_personal_data(
        "姓名：测试用户\n电话：13800138000\n邮箱：person@example.com\n技能：Python"
    )
    assert "13800138000" not in rendered
    assert "person@example.com" not in rendered
    assert "Python" in rendered


def test_candidate_name_can_be_retained_without_exposing_contact_data() -> None:
    rendered = redact_nonessential_personal_data(
        "\u59d3\u540d\uff1aAI Candidate \u5730\u5740\uff1aShanghai\n"
        "\u7535\u8bdd\uff1a13800138000\n\u90ae\u7bb1\uff1aperson@example.com",
        retain_candidate_name=True,
    )

    assert "AI Candidate" in rendered
    assert "Shanghai" not in rendered
    assert "13800138000" not in rendered
    assert "person@example.com" not in rendered


def test_model_evidence_rendering_never_includes_local_contact_values() -> None:
    rendered = render_evidence_blocks(
        [
            EvidenceBlock(
                block_id="page-001",
                page_no=1,
                block_type="page_text",
                text=(
                    "Name: AI Candidate\n138 0000 0000\n010-12345678\n"
                    "Email: person@example.com\n+1 415 555 2671\n"
                    "0086 138-0013-8000\nSkills: Python"
                ),
            )
        ],
        retain_candidate_name=True,
    )

    assert "AI Candidate" in rendered
    assert "138 0000 0000" not in rendered
    assert "010-12345678" not in rendered
    assert "person@example.com" not in rendered
    assert "+1 415 555 2671" not in rendered
    assert "0086 138-0013-8000" not in rendered
    assert "REDACTED" not in rendered
    assert "Python" in rendered


def test_tool_evidence_ids_accept_current_strings_and_legacy_objects() -> None:
    payload = {
        "education": [
            {
                "evidence_block_ids": [{"block_id": "page-001"}],
            }
        ],
        "experiences": [
            {
                "evidence_block_ids": [{"block_id": "page-002"}],
                "classification_evidence_block_ids": [{"block_id": "page-001"}],
                "detail_items": [
                    {
                        "detail_raw": "Built the API",
                        "evidence_block_ids": [{"block_id": "page-002"}],
                    }
                ],
            }
        ],
        "skills": [
            {
                "evidence_block_ids": ["page-003"],
            }
        ],
    }
    _flatten_evidence_block_ids(payload)
    assert payload["education"][0]["evidence_block_ids"] == ["page-001"]
    assert payload["experiences"][0]["classification_evidence_block_ids"] == [
        "page-001"
    ]
    assert payload["experiences"][0]["detail_items"][0]["evidence_block_ids"] == [
        "page-002"
    ]
    assert payload["skills"][0]["evidence_block_ids"] == ["page-003"]


def test_incomplete_model_work_item_is_downgraded_to_unknown() -> None:
    payload = {
        "experiences": [
            {
                "experience_type": "employment",
                "organization_name_raw": "Acme",
                "title_raw": "Engineer",
                "classification_evidence_block_ids": [],
            }
        ]
    }

    _downgrade_incomplete_work_experiences(payload)

    assert payload["experiences"][0]["experience_type"] == "unknown"


def test_versioned_ai_rulebook_contains_the_complete_registry() -> None:
    rulebook = build_985_211_ai_rulebook()

    roster_lines = [line for line in rulebook.splitlines() if line.startswith("- cn-")]
    assert len(roster_lines) == 112
    assert "moe-985-211-2005-2006.v1" in rulebook
    assert any("cn-985-001" in line for line in roster_lines)
    assert any("cn-211-048" in line for line in roster_lines)
    assert "{{ROSTER_ENTRIES}}" not in rulebook


def test_resume_fact_tool_schema_requires_binary_ai_school_judgment() -> None:
    schema = resume_facts_tool_schema()
    identity = schema["properties"]
    assert identity["candidate_name_raw"] == {
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 80},
            {"type": "null"},
        ]
    }
    assert identity["candidate_name_evidence_block_ids"] == {
        "type": "array",
        "items": {"type": "string", "pattern": "^page-\\d{3}$"},
        "maxItems": 2,
    }
    assert "candidate_name_raw" in schema["required"]
    assert "candidate_name_evidence_block_ids" in schema["required"]
    education = schema["properties"]["education"]["items"]
    properties = education["properties"]

    assert properties["ai_985_211_judgment"] == {"type": "boolean"}
    assert properties["ai_institution_roster_id"] == {
        "anyOf": [{"type": "string", "maxLength": 64}, {"type": "null"}]
    }
    assert "ai_985_211_judgment" in education["required"]
    assert "ai_institution_roster_id" in education["required"]
    experience = schema["properties"]["experiences"]["items"]
    assert "experience_name_raw" in experience["required"]
    assert "detail_items" in experience["required"]
    detail = experience["properties"]["detail_items"]["items"]
    assert detail["required"] == ["detail_raw", "evidence_block_ids"]
    assert detail["additionalProperties"] is False


def test_core_fact_tool_schema_keeps_identity_and_omits_enrichment_fields() -> None:
    schema = resume_core_facts_tool_schema()

    assert set(schema["properties"]) == {
        "schema_version",
        "candidate_name_raw",
        "candidate_name_evidence_block_ids",
        "education",
        "experiences",
        "skills",
    }
    assert schema["properties"]["candidate_name_raw"] == {
        "anyOf": [
            {"type": "string", "minLength": 1, "maxLength": 80},
            {"type": "null"},
        ]
    }
    assert schema["properties"]["candidate_name_evidence_block_ids"] == {
        "type": "array",
        "items": {"type": "string", "pattern": "^page-\\d{3}$"},
        "maxItems": 2,
    }
    assert "candidate_name_raw" in schema["required"]
    assert "candidate_name_evidence_block_ids" in schema["required"]
    education = schema["properties"]["education"]["items"]
    experience = schema["properties"]["experiences"]["items"]
    assert "ai_985_211_judgment" not in education["properties"]
    assert "ai_institution_roster_id" not in education["properties"]
    assert "detail_items" not in experience["properties"]
    assert education["properties"]["evidence_block_ids"]["maxItems"] == 8


def test_core_extraction_fills_existing_submission_defaults(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {
                                                    "schema_version": "resume_facts.v1",
                                                    "candidate_name_raw": "Test Candidate",
                                                    "candidate_name_evidence_block_ids": [
                                                        "page-001"
                                                    ],
                                                    "education": [],
                                                    "experiences": [
                                                        {
                                                            "experience_type": "project",
                                                            "experience_name_raw": "Data Platform",
                                                            "organization_name_raw": None,
                                                            "title_raw": "Developer",
                                                            "start_month": None,
                                                            "end_month": None,
                                                            "is_current": False,
                                                            "evidence_block_ids": ["page-001"],
                                                            "classification_evidence_block_ids": [],
                                                        }
                                                    ],
                                                    "skills": [
                                                        {
                                                            "skill_display": "Python",
                                                            "evidence_block_ids": ["page-001"],
                                                        }
                                                    ],
                                                }
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout: int):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr("app.services.deepseek_provider.urllib.request.urlopen", fake_urlopen)
    result = extract_resume_core_facts(
        api_key="test-key",
        model="test-model",
        timeout_seconds=5,
        blocks=[
            EvidenceBlock(
                block_id="page-001",
                page_no=1,
                block_type="page",
                text=(
                    "Test Candidate\\n"
                    "Phone: 13800138000\\n"
                    "Email: person@example.com\\n"
                    "Project Data Platform Developer Skills Python"
                ),
            )
        ],
    )

    assert result.candidate_name_raw == "Test Candidate"
    assert result.candidate_name_evidence_block_ids == ["page-001"]
    assert result.education == []
    assert result.experiences[0].detail_items == []
    assert result.experiences[0].experience_name_raw == "Data Platform"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["tools"][0]["function"]["name"] == "submit_resume_core_facts"
    prompt = payload["messages"][1]["content"]
    assert "Test Candidate" in prompt
    assert "13800138000" not in prompt
    assert "person@example.com" not in prompt
    assert "985/211" not in prompt
    assert "clear page header" in payload["messages"][0]["content"]


def test_extraction_prompt_contains_the_versioned_ai_rulebook(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {
                                                    "schema_version": "resume_facts.v1",
                                                    "candidate_name_raw": "Test Candidate",
                                                    "candidate_name_evidence_block_ids": [
                                                        "page-001"
                                                    ],
                                                    "education": [
                                                        {
                                                            "school_name_raw": "Test University",
                                                            "degree": "bachelor",
                                                            "ai_985_211_judgment": False,
                                                            "ai_institution_roster_id": None,
                                                            "major_raw": "Computer Science",
                                                            "start_month": None,
                                                            "end_month": None,
                                                            "evidence_block_ids": ["page-001"],
                                                        }
                                                    ],
                                                    "experiences": [],
                                                    "skills": [],
                                                }
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout: int):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr("app.services.deepseek_provider.urllib.request.urlopen", fake_urlopen)
    result = extract_resume_facts(
        api_key="test-key",
        model="test-model",
        timeout_seconds=5,
        blocks=[
            EvidenceBlock(
                block_id="page-001",
                page_no=1,
                block_type="page",
                text=(
                    "\u59d3\u540d\uff1aTest Candidate \u7535\u8bdd\uff1a13800138000 010-12345678 person@example.com "
                    "Education Test University "
                    "Computer Science Project Data Platform Developer Built ingestion "
                    "pipeline Reduced report latency"
                ),
            )
        ],
    )

    assert result.education[0].ai_985_211_judgment is False
    assert result.candidate_name_raw == "Test Candidate"
    assert result.candidate_name_evidence_block_ids == ["page-001"]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    prompt = payload["messages"][1]["content"]
    assert "cn-985-001" in prompt
    assert "moe-985-211-2005-2006.v1" in prompt
    assert "detail_items must contain every separately written task" in prompt
    assert "Test Candidate" in prompt
    assert "13800138000" not in prompt
    assert "010-12345678" not in prompt
    assert "person@example.com" not in prompt


def test_strict_function_reports_output_truncation_before_json_parsing(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"tool_calls": []},
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr("app.services.deepseek_provider.urllib.request.urlopen", fake_urlopen)
    try:
        call_strict_function(
            api_key="test-key",
            model="test-model",
            timeout_seconds=5,
            function_name="submit_test",
            function_description="Submit a test payload.",
            parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
            system_prompt="Test.",
            user_prompt="Test.",
            max_tokens=100,
        )
    except DeepSeekProviderError as exc:
        assert str(exc) == "deepseek_response_truncated"
    else:  # pragma: no cover - the assertion above is the expected path
        raise AssertionError("truncated structured output must be distinguishable")


def test_empty_resume_fact_arrays_have_a_stable_retry_code(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {
                                                    "schema_version": "resume_facts.v1",
                                                    "candidate_name_raw": "Test Candidate",
                                                    "candidate_name_evidence_block_ids": [
                                                        "page-001"
                                                    ],
                                                    "education": [],
                                                    "experiences": [],
                                                    "skills": [],
                                                }
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 5
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("app.services.deepseek_provider.urllib.request.urlopen", fake_urlopen)

    try:
        extract_resume_facts(
            api_key="test-key",
            model="test-model",
            timeout_seconds=5,
            blocks=[
                EvidenceBlock(
                    block_id="page-001",
                    page_no=1,
                    block_type="page",
                    text="Education Test University Skills Python",
                )
            ],
        )
    except DeepSeekProviderError as exc:
        assert str(exc) == "deepseek_empty_structured_facts"
    else:  # pragma: no cover - the assertion above is the expected path
        raise AssertionError("empty fact arrays must not be accepted")

    assert len(requests) == 1


def test_extraction_retry_prompt_requests_a_fresh_grounded_submission(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "function": {
                                            "arguments": json.dumps(
                                                {
                                                    "schema_version": "resume_facts.v1",
                                                    "candidate_name_raw": None,
                                                    "candidate_name_evidence_block_ids": [],
                                                    "education": [],
                                                    "experiences": [],
                                                    "skills": [
                                                        {
                                                            "skill_display": "Python",
                                                            "evidence_block_ids": ["page-001"],
                                                        }
                                                    ],
                                                }
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 5
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("app.services.deepseek_provider.urllib.request.urlopen", fake_urlopen)
    result = extract_resume_facts(
        api_key="test-key",
        model="test-model",
        timeout_seconds=5,
        retry_reason="deepseek_empty_structured_facts",
        blocks=[
            EvidenceBlock(
                block_id="page-001",
                page_no=1,
                block_type="page",
                text="Skills Python",
            )
        ],
    )

    assert result.skills[0].skill_display == "Python"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    system_prompt = payload["messages"][0]["content"]
    assert "retry after the previous function arguments failed validation" in system_prompt
    assert "Do not invent facts" in system_prompt
