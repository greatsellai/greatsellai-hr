from __future__ import annotations

from test_resume_flow import create_candidate, replace_page_evidence, upload_text_resume
from app.models import ResumeAiExtractionJob
from app.schemas import ResumeFactsSubmission
from app.services import ai_extraction_job_service as job_service


def _save_v2_resume(client) -> str:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 北京大学 计算机 本科 2022-09 至 2026-06，"
        "平均成绩 92 分，GPA 3.8/4.0，专业排名 5/100。获得国家奖学金。"
        "英语四级 520 分。全国大学生技能竞赛一等奖，担任项目组组长。"
        "技能 Python SQL。",
    )
    response = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "major_raw": "计算机",
                        "start_month": "2022-09",
                        "end_month": "2026-06",
                        "average_score": 92,
                        "gpa_value": 3.8,
                        "gpa_scale": 4.0,
                        "rank_position": 5,
                        "rank_total": 100,
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "experiences": [
                    {
                        "experience_type": "competition",
                        "experience_name_raw": "全国大学生技能竞赛",
                        "title_raw": "组长",
                        "leadership_context": "project_team",
                        "leadership_role": "组长",
                        "award_level": "national",
                        "award_result_raw": "一等奖",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {
                        "skill_display": "Python",
                        "skill_category": "software",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "language_credentials": [
                    {
                        "credential_code": "cet4",
                        "credential_name_raw": "英语四级",
                        "score": 520,
                        "passed": True,
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "scholarships": [
                    {
                        "scholarship_name_raw": "国家奖学金",
                        "scholarship_level": "national",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert response.status_code == 200, response.text
    return resume_id


def test_filter_options_use_confirmed_order_and_bilingual_english_names(client) -> None:
    response = client.get("/v1/filter-options")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["label"] for item in payload["degrees"]] == [
        "博士", "硕士", "本科", "大专", "高中", "中专/职高及以下"
    ]
    assert [item["label"] for item in payload["institution_classifications"]] == [
        "985", "211", "本科", "大专", "中专", "海外院校"
    ]
    assert [item["label"] for item in payload["language_credentials"][:4]] == [
        "大学英语四级（CET-4）",
        "大学英语六级（CET-6）",
        "雅思（IELTS）",
        "托福（TOEFL）",
    ]
    assert [item["value"] for item in payload["leadership_contexts"]] == [
        "class", "student_org", "club", "project_team", "company"
    ]
    assert [item["value"] for item in payload["award_levels"]] == [
        "national", "provincial", "school", "department", "other"
    ]


def test_v2_filters_match_same_grounded_facts_and_school_alias(client) -> None:
    resume_id = _save_v2_resume(client)
    response = client.post(
        "/v1/candidates/search",
        json={
            "highest_degree_in": ["bachelor"],
            "graduation_status": "fresh",
            "fresh_graduate_start_month": "2026-01",
            "fresh_graduate_end_month": "2027-12",
            "education_any_of": [
                {
                    "school_name_contains": ["北大"],
                    "major_contains": ["计算机"],
                    "institution_classifications_any_of": ["985"],
                    "min_average_score": 90,
                    "min_gpa_percent": 90,
                    "max_rank_position": 10,
                    "max_rank_percent": 10,
                }
            ],
            "experience_any_of": [
                {
                    "experience_types": ["competition"],
                    "award_levels_any_of": ["national"],
                    "award_result_contains": ["一等奖"],
                }
            ],
            "skill_categories_any_of": ["software"],
            "language_credentials_any_of": [
                {"credential_code": "cet4", "min_score": 500}
            ],
            "scholarship_status": "present",
            "scholarship_levels_any_of": ["national"],
            "competition_status": "present",
            "competition_award_status": "present",
            "leadership_any_of": [
                {"contexts_any_of": ["project_team"], "roles_any_of": ["组长"]}
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert [item["resume_id"] for item in response.json()["items"]] == [resume_id]
    item = response.json()["items"][0]
    assert item["institution_classifications"] == ["985"]
    evidence_types = {
        match["fact_type"] for match in item["matched_evidence"]
    }
    assert {"education", "skill", "language", "scholarship", "experience"} <= evidence_types
    display_fields = {
        field["key"]: field["values"] for field in item["display_fields"]
    }
    assert display_fields["highest_degree"] == ["bachelor"]
    assert display_fields["institution_classifications"] == ["985"]
    assert display_fields["school"] == ["\u5317\u4eac\u5927\u5b66"]
    assert display_fields["major"] == ["\u8ba1\u7b97\u673a"]
    assert display_fields["academic_performance"] == [
        "\u5e73\u5747\u5206 92",
        "GPA 3.8/4 (95%)",
        "\u6392\u540d 5/100",
        "\u6392\u540d\u524d 5%",
    ]
    assert display_fields["skills"] == ["Python"]
    assert display_fields["language"][-1].endswith("520")
    assert display_fields["scholarship"] == ["\u56fd\u5bb6\u5956\u5b66\u91d1"]
    assert display_fields["competition"] == [
        "\u5168\u56fd\u5927\u5b66\u751f\u6280\u80fd\u7ade\u8d5b | \u4e00\u7b49\u5956"
    ]
    assert display_fields["leadership"] == ["\u7ec4\u957f"]

    # A 985 education is no longer silently included by the exact 211 filter.
    exact_211 = client.post(
        "/v1/candidates/search",
        json={
            "education_any_of": [
                {"institution_classifications_any_of": ["211"]}
            ]
        },
    )
    assert exact_211.status_code == 200, exact_211.text
    assert exact_211.json()["items"] == []


def test_exact_211_filter_matches_only_a_211_only_school(client) -> None:
    _save_v2_resume(client)

    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 北京工业大学 计算机 本科 2022-09 至 2026-06。",
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京工业大学",
                        "degree": "bachelor",
                        "major_raw": "计算机",
                        "start_month": "2022-09",
                        "end_month": "2026-06",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.post(
        "/v1/candidates/search",
        json={
            "education_any_of": [
                {"institution_classifications_any_of": ["211"]}
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert [item["resume_id"] for item in response.json()["items"]] == [resume_id]
    assert response.json()["items"][0]["institution_classifications"] == ["211"]


def test_search_display_fields_keep_matching_experience_values_structured(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 清华大学 计算机科学 本科，2018-09 至 2022-06。"
        "工作经历 GreatSell 智能招聘平台，担任后端工程师，2022-07 至 2024-06。"
        "获得全国技术创新赛一等奖。技能 Python。",
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "清华大学",
                        "degree": "bachelor",
                        "major_raw": "计算机科学",
                        "start_month": "2018-09",
                        "end_month": "2022-06",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "experiences": [
                    {
                        "experience_type": "employment",
                        "experience_name_raw": "智能招聘平台",
                        "organization_name_raw": "GreatSell",
                        "title_raw": "后端工程师",
                        "start_month": "2022-07",
                        "end_month": "2024-06",
                        "award_level": "national",
                        "award_result_raw": "一等奖",
                        "classification_evidence_block_ids": ["page-001"],
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {
                        "skill_display": "Python",
                        "skill_category": "software",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.post(
        "/v1/candidates/search",
        json={
            "experience_any_of": [
                {
                    "experience_types": ["employment"],
                    "experience_name_contains": ["招聘平台"],
                    "organization_name_contains": ["greatsell"],
                    "title_contains": ["后端"],
                    "award_levels_any_of": ["national"],
                    "award_result_contains": ["一等奖"],
                }
            ],
            "skills_all_of": ["Python"],
        },
    )
    assert response.status_code == 200, response.text
    assert [item["resume_id"] for item in response.json()["items"]] == [resume_id]
    display_fields = {
        field["key"]: field["values"]
        for field in response.json()["items"][0]["display_fields"]
    }
    assert display_fields["experience_type"] == ["employment"]
    assert display_fields["experience_name"] == ["智能招聘平台"]
    assert display_fields["organization"] == ["GreatSell"]
    assert display_fields["title"] == ["后端工程师"]
    assert display_fields["experience_award"] == ["一等奖", "国家级"]
    assert display_fields["skills"] == ["Python"]


def test_search_display_fields_preserve_custom_language_name(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 清华大学 本科。获得 CATTI 三级口译证书，成绩 60。",
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "清华大学",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "language_credentials": [
                    {
                        "credential_code": "custom",
                        "credential_name_raw": "CATTI 三级口译证书",
                        "score": 60,
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.post(
        "/v1/candidates/search",
        json={
            "language_credentials_any_of": [
                {
                    "credential_code": "custom",
                    "custom_name_contains": "CATTI",
                    "min_score": 60,
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    display_fields = {
        field["key"]: field["values"]
        for field in response.json()["items"][0]["display_fields"]
    }
    assert display_fields["language"] == ["CATTI 三级口译证书 60"]


def test_search_display_fields_do_not_mix_values_from_or_filter_groups(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 清华大学 物理学 本科，2018-09 至 2022-06。"
        "教育经历 复旦大学 数学 本科，2022-09 至 2026-06。",
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "清华大学",
                        "degree": "bachelor",
                        "major_raw": "物理学",
                        "start_month": "2018-09",
                        "end_month": "2022-06",
                        "evidence_block_ids": ["page-001"],
                    },
                    {
                        "school_name_raw": "复旦大学",
                        "degree": "bachelor",
                        "major_raw": "数学",
                        "start_month": "2022-09",
                        "end_month": "2026-06",
                        "evidence_block_ids": ["page-001"],
                    },
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text

    response = client.post(
        "/v1/candidates/search",
        json={
            "education_any_of": [
                {"school_name_contains": ["清华大学"]},
                {"major_contains": ["数学"]},
            ]
        },
    )
    assert response.status_code == 200, response.text
    display_fields = {
        field["key"]: field["values"]
        for field in response.json()["items"][0]["display_fields"]
    }
    assert display_fields["school"] == ["清华大学"]
    assert display_fields["major"] == ["数学"]


def test_english_aliases_are_or_for_broad_keywords_but_precise_stays_literal(client) -> None:
    resume_id = _save_v2_resume(client)

    tem_candidate_id = create_candidate(client)
    tem_resume_id = upload_text_resume(client, tem_candidate_id)
    replace_page_evidence(
        client,
        tem_resume_id,
        "教育经历 北京大学 本科。英语专业四级 TEM-4。",
    )
    tem_saved = client.put(
        f"/v1/resumes/{tem_resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "language_credentials": [
                    {
                        "credential_code": "tem4",
                        "credential_name_raw": "英语专业四级",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert tem_saved.status_code == 200, tem_saved.text

    broad = client.post(
        "/v1/candidates/search",
        json={"keywords": ["CET-4"], "keyword_match_mode": "broad"},
    )
    assert broad.status_code == 200, broad.text
    assert [item["resume_id"] for item in broad.json()["items"]] == [resume_id]

    precise = client.post(
        "/v1/candidates/search",
        json={"keywords": ["大学英语四级"], "keyword_match_mode": "precise"},
    )
    assert precise.status_code == 200, precise.text
    assert precise.json()["items"] == []


def test_language_minimum_score_does_not_match_missing_or_lower_score(client) -> None:
    _save_v2_resume(client)
    response = client.post(
        "/v1/candidates/search",
        json={
            "language_credentials_any_of": [
                {"credential_code": "cet4", "min_score": 600},
                {"credential_code": "ielts", "min_score": 6.5},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_unquoted_academic_number_is_rejected_instead_of_becoming_a_filter_fact(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(client, resume_id, "教育经历 北京大学 本科，平均成绩 80 分。")
    response = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "average_score": 99,
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "average_score_not_grounded_in_evidence"


def test_active_resume_can_queue_additive_filter_v2_enrichment(client) -> None:
    resume_id = _save_v2_resume(client)
    response = client.post(f"/v1/resumes/{resume_id}/enrich-filter-facts")
    assert response.status_code == 200, response.text
    assert response.json()["ai_extraction_status"] == "unavailable"

    with client.app.state.database.session_factory() as session:
        job = session.query(ResumeAiExtractionJob).filter_by(resume_id=resume_id).one()
        assert job.job_kind == "filter_v2_enrichment"
        assert job.input_facts_version == 1


def test_filter_v2_enrichment_worker_preserves_active_facts_and_adds_new_evidence(
    ai_client,
    monkeypatch,
) -> None:
    candidate_id = create_candidate(ai_client)
    resume_id = upload_text_resume(ai_client, candidate_id)
    replace_page_evidence(
        ai_client,
        resume_id,
        "教育经历 北京大学 本科。技能 Python。英语四级 520 分。获得国家奖学金。",
    )
    saved = ai_client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v1",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {
                        "skill_display": "Python",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text

    with ai_client.app.state.database.session_factory() as session:
        initial_job = session.query(ResumeAiExtractionJob).filter_by(resume_id=resume_id).one()
        initial_job.status = "completed"
        session.commit()

    queued = ai_client.post(f"/v1/resumes/{resume_id}/enrich-filter-facts")
    assert queued.status_code == 200, queued.text
    assert queued.json()["ai_extraction_status"] == "queued"

    def fake_extract(**kwargs: object) -> ResumeFactsSubmission:
        return ResumeFactsSubmission.model_validate(
            {
                "schema_version": "resume_facts.v2",
                "education": [
                    {
                        "school_name_raw": "北京大学",
                        "degree": "bachelor",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "skills": [
                    {
                        "skill_display": "Python",
                        "skill_category": "software",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "language_credentials": [
                    {
                        "credential_code": "cet4",
                        "credential_name_raw": "英语四级",
                        "score": 520,
                        "passed": True,
                        "evidence_block_ids": ["page-001"],
                    }
                ],
                "scholarships": [
                    {
                        "scholarship_name_raw": "国家奖学金",
                        "scholarship_level": "national",
                        "evidence_block_ids": ["page-001"],
                    }
                ],
            }
        )

    monkeypatch.setattr(job_service, "extract_resume_facts", fake_extract)
    assert job_service.run_ai_extraction_worker_once(
        ai_client.app.state.database,
        settings=ai_client.app.state.settings,
        worker_id="filter-v2-test-worker",
    )

    review = ai_client.get(f"/v1/resumes/{resume_id}/review")
    assert review.status_code == 200, review.text
    payload = review.json()
    assert payload["extraction_status"] == "ready"
    assert payload["is_active"] is True
    assert payload["facts_version"] == 2
    assert payload["education"][0]["school_name_raw"] == "北京大学"
    assert payload["skills"][0]["skill_display"] == "Python"
    assert payload["skills"][0]["skill_category"] == "software"
    assert payload["language_credentials"][0]["credential_code"] == "cet4"
    assert payload["scholarships"][0]["scholarship_name_raw"] == "国家奖学金"
