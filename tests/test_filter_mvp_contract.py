from __future__ import annotations

from test_resume_flow import (
    create_candidate,
    replace_page_evidence,
    upload_text_resume,
)


def _facts(
    *,
    educations: list[dict[str, object]],
    experiences: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "facts": {
            "schema_version": "resume_facts.v1",
            "education": educations,
            "experiences": experiences,
            "skills": [
                {"skill_display": "Python", "evidence_block_ids": ["page-001"]},
                {"skill_display": "SQL", "evidence_block_ids": ["page-001"]},
            ],
        }
    }


def _education(
    school: str,
    degree: str,
    major: str,
) -> dict[str, object]:
    return {
        "school_name_raw": school,
        "degree": degree,
        "major_raw": major,
        "evidence_block_ids": ["page-001"],
    }


def _employment(organization: str, title: str) -> dict[str, object]:
    return {
        "experience_type": "employment",
        "organization_name_raw": organization,
        "title_raw": title,
        "evidence_block_ids": ["page-001"],
        "classification_evidence_block_ids": ["page-001"],
    }


def _save_cross_record_resume(client) -> tuple[str, str]:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 清华大学 数学 本科。教育经历 普通大学 计算机 硕士。"
        "工作经历 Acme Sales。工作经历 Beta Python Engineer。技能 Python SQL。",
    )
    response = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json=_facts(
            educations=[
                _education("清华大学", "bachelor", "数学"),
                _education("普通大学", "master", "计算机"),
            ],
            experiences=[
                _employment("Acme", "Sales"),
                _employment("Beta", "Python Engineer"),
            ],
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True
    return candidate_id, resume_id


def test_education_and_experience_conditions_cannot_combine_across_records(client) -> None:
    _save_cross_record_resume(client)

    education_response = client.post(
        "/v1/candidates/search",
        json={
            "education_any_of": [
                {
                    "degree_in": ["bachelor"],
                    "school_name_contains": ["清华大学"],
                    "major_contains": ["计算机"],
                }
            ]
        },
    )
    assert education_response.status_code == 200, education_response.text
    assert education_response.json()["items"] == []

    experience_response = client.post(
        "/v1/candidates/search",
        json={
            "experience_any_of": [
                {
                    "experience_types": ["employment"],
                    "organization_name_contains": ["Acme"],
                    "title_contains": ["Python"],
                }
            ]
        },
    )
    assert experience_response.status_code == 200, experience_response.text
    assert experience_response.json()["items"] == []


def test_ready_new_resume_replaces_prior_active_version_in_search(client) -> None:
    candidate_id = create_candidate(client)
    first_resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        first_resume_id,
        "教育经历 清华大学 计算机 本科。工作经历 Acme Python Engineer。技能 Python SQL。",
    )
    first_save = client.put(
        f"/v1/resumes/{first_resume_id}/facts",
        json=_facts(
            educations=[_education("清华大学", "bachelor", "计算机")],
            experiences=[_employment("Acme", "Python Engineer")],
        ),
    )
    assert first_save.status_code == 200, first_save.text
    assert first_save.json()["is_active"] is True

    second_resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        second_resume_id,
        "教育经历 清华大学 计算机 硕士。工作经历 Beta Python Engineer。技能 Python SQL。",
    )
    second_save = client.put(
        f"/v1/resumes/{second_resume_id}/facts",
        json=_facts(
            educations=[_education("清华大学", "master", "计算机")],
            experiences=[_employment("Beta", "Python Engineer")],
        ),
    )
    assert second_save.status_code == 200, second_save.text
    assert second_save.json()["is_active"] is True

    assert client.get(f"/v1/resumes/{first_resume_id}").json()["is_active"] is False
    results = client.post("/v1/candidates/search", json={"skills_all_of": ["Python"]})
    assert results.status_code == 200, results.text
    assert results.json()["items"] == [
        {
            "candidate_id": candidate_id,
            "display_name": "测试候选人",
            "resume_id": second_resume_id,
            "original_filename": "resume.pdf",
            "is_985_211": True,
            "institution_classifications": ["985"],
            "highest_degree": "master",
            "employment_months": 0,
            "employment_or_internship_months": 0,
            "education_school": "清华大学",
            "education_major": "计算机",
            "latest_experience_title": "Python Engineer",
            "latest_experience_organization": "Beta",
            "latest_experience_type": "employment",
            "skill_highlights": ["Python", "SQL"],
            "summary_preview": None,
            "score_id": None,
            "score_template_id": None,
            "score_total": None,
            "score_status": None,
            "score_template_name": None,
            "score_confidence": None,
            "display_fields": [
                {
                    "key": "skills",
                    "values": ["Python"],
                    "evidence_block_ids": ["page-001"],
                }
            ],
            "matched_filters": ["skills_all_of"],
            "matched_evidence": [
                {
                    "filter_key": "skills_all_of",
                    "label": "Python",
                    "fact_type": "skill",
                    "evidence_block_ids": ["page-001"],
                }
            ],
        }
    ]


def _save_ready_resume(
    client,
    *,
    source_text: str,
) -> tuple[str, str]:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(client, resume_id, source_text)
    response = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json=_facts(
            educations=[_education("清华大学", "bachelor", "计算机")],
            experiences=[_employment("Acme", "Python Engineer")],
        ),
    )
    assert response.status_code == 200, response.text
    return candidate_id, resume_id


def test_keyword_filter_and_cursor_pagination_use_active_ready_resumes(client) -> None:
    _, first_resume_id = _save_ready_resume(
        client,
        source_text=(
            "教育经历 清华大学 计算机 本科。工作经历 Acme Python Engineer。"
            "技能 Python SQL Kubernetes。"
        ),
    )
    _, second_resume_id = _save_ready_resume(
        client,
        source_text=(
            "教育经历 清华大学 计算机 本科。工作经历 Acme Python Engineer。"
            "技能 Python SQL Terraform。"
        ),
    )

    keyword_response = client.post(
        "/v1/candidates/search",
        json={"keywords_all_of": ["Kubernetes"]},
    )
    assert keyword_response.status_code == 200, keyword_response.text
    assert [item["resume_id"] for item in keyword_response.json()["items"]] == [
        first_resume_id
    ]

    first_page = client.post("/v1/candidates/search", json={"limit": 1})
    assert first_page.status_code == 200, first_page.text
    first_payload = first_page.json()
    assert len(first_payload["items"]) == 1
    assert first_payload["next_cursor"]

    second_page = client.post(
        "/v1/candidates/search",
        json={"limit": 1, "cursor": first_payload["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    second_payload = second_page.json()
    assert len(second_payload["items"]) == 1
    assert second_payload["next_cursor"] is None
    assert {
        first_payload["items"][0]["resume_id"],
        second_payload["items"][0]["resume_id"],
    } == {first_resume_id, second_resume_id}

    invalid_cursor = client.post(
        "/v1/candidates/search",
        json={"cursor": "not-a-resume-id"},
    )
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["detail"] == "invalid_cursor"


def test_saved_filter_keeps_filter_payload_without_cursor(client) -> None:
    created = client.post(
        "/v1/saved-filters",
        json={
            "name": "Python 985",
            "filters": {
                "is_985_211": True,
                "skills_all_of": ["Python"],
                "limit": 50,
            },
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["name"] == "Python 985"
    assert payload["filters"]["cursor"] is None
    assert payload["filters"]["skills_all_of"] == ["Python"]

    listed = client.get("/v1/saved-filters")
    assert listed.status_code == 200, listed.text
    assert [item["saved_filter_id"] for item in listed.json()] == [
        payload["saved_filter_id"]
    ]

    deleted = client.delete(f"/v1/saved-filters/{payload['saved_filter_id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get("/v1/saved-filters").json() == []

    cursor_rejected = client.post(
        "/v1/saved-filters",
        json={
            "name": "Invalid cursor preset",
            "filters": {"cursor": "not-allowed"},
        },
    )
    assert cursor_rejected.status_code == 422
    assert "saved_filter_cannot_include_cursor" in cursor_rejected.text
