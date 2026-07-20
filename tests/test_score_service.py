from __future__ import annotations

from test_filter_mvp_contract import _education, _employment, _facts, _save_ready_resume
from app.services.deepseek_provider import FACT_SNAPSHOT_SCHEMA_VERSION


def _template_payload() -> dict[str, object]:
    return {
        "name": "Backend Engineer",
        "description": "Grounded scoring test template",
        "dimensions": [
            {
                "key": "skills",
                "label": "Skills",
                "weight": 60,
                "guidance": "Assess explicit relevant skills only.",
            },
            {
                "key": "experience",
                "label": "Experience",
                "weight": 40,
                "guidance": "Assess explicit work evidence only.",
            },
        ],
    }


def _fake_score_provider(**kwargs: object) -> dict[str, object]:
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["schema_version"] == FACT_SNAPSHOT_SCHEMA_VERSION
    fact_id = snapshot["skills"][0]["fact_id"]
    return {
        "schema_version": "resume_score.v1",
        "dimension_scores": [
            {
                "key": "skills",
                "raw_score": 40,
                "rationale": "Explicit Python and SQL facts are present.",
                "fact_ids": [fact_id],
                "uncertainties": [],
            },
            {
                "key": "experience",
                "raw_score": 50,
                "rationale": "No directly cited fact supports this dimension.",
                "fact_ids": [],
                "uncertainties": ["Dates are incomplete."],
            },
        ],
        "overall_summary": "Grounded test score.",
        "risk_flags": [
            {
                "message": "Confirm the explicit skill depth during interview.",
                "fact_ids": [fact_id],
            }
        ],
        "needs_human_review": False,
    }


def test_score_template_score_run_and_manual_override(ai_client, monkeypatch) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL"
        ),
    )
    # The test helper's source text is replaced after upload, so use a direct
    # provider substitute rather than making a live model call.
    monkeypatch.setattr(
        "app.services.score_service.score_resume_fact_snapshot",
        _fake_score_provider,
    )

    template = ai_client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    template_id = template.json()["template_id"]
    assert "max_raw_score" not in template.json()["dimensions"][0]

    score = ai_client.post(
        f"/v1/resumes/{resume_id}/scores",
        json={"template_id": template_id},
    )
    assert score.status_code == 200, score.text
    payload = score.json()
    assert payload["fact_snapshot_id"]
    assert payload["ai_total_score"] == 44.0
    assert payload["total_score"] == 44.0
    assert payload["status"] == "succeeded"
    assert payload["model_name"] == "gateway-managed"
    assert payload["created_at"]
    assert payload["template_name"] == "Backend Engineer"
    assert payload["template_description"] == "Grounded scoring test template"
    assert payload["fact_snapshot_created_at"]
    assert payload["is_current_facts_version"] is True
    assert payload["is_current_template_version"] is True
    assert payload["analysis"]["overall_summary"] == "Grounded test score."
    assert payload["analysis"]["risk_flags"] == [
        {
            "message": "Confirm the explicit skill depth during interview.",
            "fact_ids": [payload["dimension_scores"][0]["fact_ids"][0]],
            "fact_evidence": [
                {
                    "fact_id": payload["dimension_scores"][0]["fact_ids"][0],
                    "fact_type": "skill",
                    "summary": "Python",
                    "evidence_block_ids": ["page-001"],
                }
            ],
        }
    ]
    assert payload["dimension_scores"][0]["ai_raw_score"] == 40.0
    assert payload["dimension_scores"][0]["ai_weighted_score"] == 24.0
    assert payload["dimension_scores"][0]["final_weighted_score"] == 24.0
    assert payload["dimension_scores"][0]["evidence_state"] == "grounded"
    assert payload["dimension_scores"][0]["fact_evidence"] == [
        {
            "fact_id": payload["dimension_scores"][0]["fact_ids"][0],
            "fact_type": "skill",
            "summary": "Python",
            "evidence_block_ids": ["page-001"],
        }
    ]
    assert payload["dimension_scores"][1]["fact_evidence"] == []
    assert payload["dimension_scores"][1]["evidence_state"] == "insufficient_information"
    assert payload["audit_trail"] == []

    overridden = ai_client.post(
        f"/v1/resume-scores/{payload['score_id']}/dimensions/skills/override",
        json={"raw_score": 80, "reason": "Verified with portfolio evidence."},
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["ai_total_score"] == 44.0
    assert overridden.json()["total_score"] == 68.0
    assert overridden.json()["status"] == "overridden"
    assert overridden.json()["dimension_scores"][0]["manual_reason"] == (
        "Verified with portfolio evidence."
    )
    overridden_dimension = overridden.json()["dimension_scores"][0]
    assert overridden_dimension["ai_raw_score"] == 40.0
    assert overridden_dimension["final_raw_score"] == 80.0
    assert overridden_dimension["weighted_score"] == 48.0
    assert overridden_dimension["ai_weighted_score"] == 24.0
    assert overridden_dimension["final_weighted_score"] == 48.0
    assert overridden_dimension["manual_adjustment"] == {
        "raw_score": 80.0,
        "reason": "Verified with portfolio evidence.",
        "actor": "single_admin",
        "adjusted_at": overridden_dimension["adjusted_at"],
    }
    assert overridden_dimension["adjusted_at"]
    assert overridden.json()["audit_trail"] == [
        {
            "audit_id": overridden.json()["audit_trail"][0]["audit_id"],
            "action": "score_dimension_overridden",
            "actor": "single_admin",
            "reason": "Verified with portfolio evidence.",
            "dimension_key": "skills",
            "ai_raw_score": 40.0,
            "previous_final_raw_score": 40.0,
            "final_raw_score": 80.0,
            "facts_version": payload["facts_version"],
            "template_version": payload["template_version"],
            "created_at": overridden.json()["audit_trail"][0]["created_at"],
        }
    ]

    overridden_again = ai_client.post(
        f"/v1/resume-scores/{payload['score_id']}/dimensions/skills/override",
        json={"raw_score": 60, "reason": "Adjusted after a second review."},
    )
    assert overridden_again.status_code == 200, overridden_again.text
    assert overridden_again.json()["total_score"] == 56.0
    assert overridden_again.json()["dimension_scores"][0]["final_weighted_score"] == 36.0
    assert [entry["previous_final_raw_score"] for entry in overridden_again.json()["audit_trail"]] == [
        40.0,
        80.0,
    ]
    assert [entry["final_raw_score"] for entry in overridden_again.json()["audit_trail"]] == [
        80.0,
        60.0,
    ]

    detail = ai_client.get(f"/v1/resume-scores/{payload['score_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["dimension_scores"][0]["manual_adjustment"]["reason"] == (
        "Adjusted after a second review."
    )
    assert len(detail.json()["audit_trail"]) == 2

    history = ai_client.get(f"/v1/resumes/{resume_id}/scores")
    assert history.status_code == 200, history.text
    assert [item["score_id"] for item in history.json()] == [payload["score_id"]]
    assert len(history.json()[0]["audit_trail"]) == 2

    # Score history remains available but is visibly stale after the immutable
    # fact version changes; it is never silently overwritten.
    updated_facts = ai_client.put(
        f"/v1/resumes/{resume_id}/facts",
        json=_facts(
            educations=[_education("清华大学", "bachelor", "计算机")],
            experiences=[_employment("Acme", "Python Engineer")],
        ),
    )
    assert updated_facts.status_code == 200, updated_facts.text
    stale_detail = ai_client.get(f"/v1/resume-scores/{payload['score_id']}")
    assert stale_detail.status_code == 200, stale_detail.text
    assert stale_detail.json()["is_current_facts_version"] is False

    exceeds_hundred = ai_client.post(
        f"/v1/resume-scores/{payload['score_id']}/dimensions/skills/override",
        json={"raw_score": 101, "reason": "Too high."},
    )
    assert exceeds_hundred.status_code == 422


def test_score_requires_server_side_model_key(client) -> None:
    template = client.post("/v1/score-templates", json=_template_payload())
    assert template.status_code == 200, template.text
    response = client.post(
        "/v1/resumes/not-a-real-resume/scores",
        json={"template_id": template.json()["template_id"]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "deepseek_api_key_not_configured"
