from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.integration_analysis_schemas import IntegrationAnalysisPrepare


def payload():
    candidate_id = str(uuid4())
    return {
        "idempotency_key": str(uuid4()), "title": "岗位对照草稿",
        "candidates": [{"candidate_id": candidate_id, "resume_id": str(uuid4()),
            "fact_snapshot_id": str(uuid4()), "facts_version": 1, "fact_ids": ["experience:1"]}],
        "inferences": [{"candidate_id": candidate_id, "text": "工作经历可能与岗位相关，仍需面试核实。"}],
        "questions_to_verify": [{"candidate_id": candidate_id, "text": "请核实该项目中本人负责的具体工作。"}],
    }


def test_external_draft_can_only_prepare_content_and_has_no_confirmation_or_official_result_fields():
    value = payload()
    assert IntegrationAnalysisPrepare.model_validate(value).title == "岗位对照草稿"
    for key, addition in {
        "organization_id": str(uuid4()), "owner_user_id": str(uuid4()),
        "verified_facts": [{"text": "unverified client assertion"}],
        "official_score": 99, "recruitment_status": "hired", "chat_history": [],
        "user_confirmed": True, "report_id": str(uuid4()), "expected_version": 1,
    }.items():
        with pytest.raises(ValidationError):
            IntegrationAnalysisPrepare.model_validate({**value, key: addition})


def test_draft_reference_and_version_contract():
    value = payload()
    changed = deepcopy(value)
    changed["inferences"][0]["candidate_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
    changed = deepcopy(value)
    changed["candidates"].append(deepcopy(changed["candidates"][0]))
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
    changed = deepcopy(value)
    changed["candidates"][0]["fact_ids"] = []
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
    for addition in ({"user_confirmed": True}, {"report_id": str(uuid4())}, {"expected_version": 1}):
        with pytest.raises(ValidationError):
            IntegrationAnalysisPrepare.model_validate({**value, **addition})


def test_draft_limits_reject_oversize_or_duplicate_references():
    value = payload()
    changed = deepcopy(value)
    changed["candidates"][0]["fact_ids"] *= 2
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
    changed = deepcopy(value)
    changed["candidates"][0]["facts_version"] = True
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
    for title in ("\t", "x" * 121, "hidden\x00text"):
        with pytest.raises(ValidationError):
            IntegrationAnalysisPrepare.model_validate({**value, "title": title})
    changed = deepcopy(value)
    changed["inferences"] = [{"candidate_id": value["candidates"][0]["candidate_id"], "text": "x" * 2000}] * 9
    with pytest.raises(ValidationError):
        IntegrationAnalysisPrepare.model_validate(changed)
