from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    Job,
    JobMatch,
    JobMatchBatch,
    JobMatchBatchItem,
    JobMatchRequirementResult,
    JobRequirement,
    JobVersion,
    ResumeFactSnapshot,
    TalentSearchProfileRevision,
    TalentSearchRun,
)
from app.services import talent_search_profile_service as profile_service
from app.services.deepseek_provider import DeepSeekProviderError, validate_talent_search_profile_output
from app.services.search_service import search_candidates as real_search_candidates
from app.tenant_scope import bypass_organization_scope
from test_resume_flow import create_candidate, replace_page_evidence, upload_text_resume
from test_tenant_isolation import _register_and_login, workspace_clients


def _profile_hard_filters(
    *,
    skills_all_of: list[str] | None = None,
    institution_classifications_any_of: list[str] | None = None,
    education_degree_in: list[str] | None = None,
    highest_degree_in: list[str] | None = None,
) -> dict[str, object]:
    """Return the full persisted hard-filter shape used by the profile draft."""

    return {
        "institution_classifications_any_of": (
            ["985", "211"]
            if institution_classifications_any_of is None
            else institution_classifications_any_of
        ),
        "education_degree_in": education_degree_in or [],
        "highest_degree_in": (
            ["bachelor"] if highest_degree_in is None else highest_degree_in
        ),
        "graduation_status": "any",
        "fresh_graduate_start_month": None,
        "fresh_graduate_end_month": None,
        "min_employment_months": None,
        "min_employment_or_internship_months": None,
        "experience_types_all_of": [],
        "skills_all_of": skills_all_of or [],
        "language_credentials_all_of": [],
    }


def _generated_profile(*, skills_all_of: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": "talent_search_profile.v1",
        "title": "AI 应用工程师人才画像",
        "summary": "先按已确认的硬条件召回，再核验项目与工程能力证据。",
        "hard_filters": _profile_hard_filters(skills_all_of=skills_all_of),
        "verification_requirements": [
            {
                "key": "agent_delivery",
                "label": "具备 Agent 系统的实际交付经历",
                "evidence_hint": "核验项目经历中的职责、技术方案与结果。",
            }
        ],
        "preferred_requirements": [
            {
                "key": "llm_deployment",
                "label": "有大模型部署或推理优化经验",
                "evidence_hint": "核验 vLLM、量化或服务部署相关事实。",
            }
        ],
        "aliases": ["AI 应用工程师", "LLM 应用工程师"],
        "clarifying_questions": ["是否有必须具备的行业背景？"],
    }


def _install_profile_ai_stub(
    monkeypatch,
    *,
    generated: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Bypass the provider transport while retaining the profile service flow."""

    calls: list[dict[str, object]] = []

    def fake_generate(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return generated or _generated_profile()

    monkeypatch.setattr(
        profile_service,
        "ai_gateway_credentials_configured",
        lambda _settings: True,
    )
    monkeypatch.setattr(
        profile_service,
        "ai_gateway_execution",
        lambda *args, **kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        profile_service,
        "generate_talent_search_profile",
        fake_generate,
    )
    return calls


def _save_ready_resume(
    client,
    *,
    skills: list[str],
    experience_types: list[str] | None = None,
    education_degrees: list[str] | None = None,
    source_suffix: str = "",
) -> str:
    """Persist a compact, source-grounded active resume for strict recall tests."""

    normalized_experience_types = experience_types or ["employment"]
    normalized_education_degrees = education_degrees or ["bachelor"]
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    experience_facts: list[dict[str, object]] = []
    experience_text: list[str] = []
    for experience_type in normalized_experience_types:
        if experience_type == "internship":
            organization = "实习示例公司"
            title = "实习工程师"
            prefix = "实习经历"
        elif experience_type == "project":
            organization = "项目示例"
            title = "项目开发者"
            prefix = "项目经历"
        else:
            organization = "工作示例公司"
            title = "后端工程师"
            prefix = "工作经历"
        experience_text.append(
            f"{prefix} {organization}，担任{title}，2023-01 至 2023-06。"
        )
        experience_fact: dict[str, object] = {
            "experience_type": experience_type,
            "experience_name_raw": organization,
            "organization_name_raw": organization,
            "title_raw": title,
            "start_month": "2023-01",
            "end_month": "2023-06",
            "evidence_block_ids": ["page-001"],
            "classification_evidence_block_ids": ["page-001"],
        }
        if experience_type == "project" and source_suffix.strip():
            experience_fact["detail_items"] = [
                {
                    "detail_raw": source_suffix.strip(),
                    "evidence_block_ids": ["page-001"],
                }
            ]
        experience_facts.append(experience_fact)
    education_facts: list[dict[str, object]] = []
    education_text: list[str] = []
    degree_labels = {"bachelor": "本科", "master": "硕士", "doctor": "博士"}
    for index, degree in enumerate(normalized_education_degrees):
        start_year = 2018 + index * 4
        end_year = start_year + 4
        education_text.append(
            "教育经历 北京大学 计算机 "
            f"{degree_labels.get(degree, degree)}，{start_year}-09 至 {end_year}-06。"
        )
        education_facts.append(
            {
                "school_name_raw": "北京大学",
                "degree": degree,
                "major_raw": "计算机",
                "start_month": f"{start_year}-09",
                "end_month": f"{end_year}-06",
                "evidence_block_ids": ["page-001"],
            }
        )
    source_text = "".join(
        [
            *education_text,
            *experience_text,
            "技能 ",
            " ".join(skills),
            "。",
            source_suffix,
        ]
    )
    replace_page_evidence(client, resume_id, source_text)
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": {
                "schema_version": "resume_facts.v1",
                "education": education_facts,
                "experiences": experience_facts,
                "skills": [
                    {"skill_display": skill, "evidence_block_ids": ["page-001"]}
                    for skill in skills
                ],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    return resume_id


def test_profile_generation_is_draft_only_and_cannot_run_before_confirmation(
    ai_client,
    monkeypatch,
) -> None:
    provider_calls = _install_profile_ai_stub(monkeypatch)

    def search_must_not_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("draft generation must not recall candidates")

    monkeypatch.setattr(profile_service, "search_candidates", search_must_not_run)
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找有 AI Agent 项目经验的工程师"},
    )

    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["status"] == "draft"
    assert payload["current_revision"]["status"] == "draft"
    assert payload["current_revision"]["hard_filters"] == _profile_hard_filters()
    assert len(provider_calls) == 1

    premature_run = ai_client.post(
        f"/v1/talent-search-profiles/{payload['profile_id']}/runs",
        json={"revision_id": payload["current_revision"]["revision_id"], "limit": 10},
    )
    assert premature_run.status_code == 409, premature_run.text
    assert premature_run.json()["detail"] == "talent_search_profile_not_confirmed"


def test_confirmed_profile_creates_hidden_match_job_not_visible_in_jd_lists(
    ai_client,
    monkeypatch,
) -> None:
    _install_profile_ai_stub(monkeypatch)
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "需要可核验的 AI 应用交付经验"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]

    confirmed = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": created.json()["current_revision"]["revision_id"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            private_version = session.scalar(
                select(JobVersion)
                .join(Job, Job.id == JobVersion.job_id)
                .where(Job.kind == "talent_search_profile")
            )
            assert private_version is not None
            assert private_version.status == "confirmed"
            private_job_version_id = private_version.id

    visible = ai_client.get("/v1/jobs/confirmed-versions")
    assert visible.status_code == 200, visible.text
    assert private_job_version_id not in {
        item["job_version_id"] for item in visible.json()
    }
    hidden_detail = ai_client.get(f"/v1/job-versions/{private_job_version_id}")
    assert hidden_detail.status_code == 404, hidden_detail.text
    # Internal profile-matcher carriers can never be reached through the normal
    # full-workspace match endpoint.
    bypass_attempt = ai_client.post(
        f"/v1/job-versions/{private_job_version_id}/match-all"
    )
    assert bypass_attempt.status_code == 404, bypass_attempt.text


def test_confirmed_profile_search_uses_frozen_snapshot_and_recalled_ids_only(
    ai_client,
    monkeypatch,
) -> None:
    matching_resume_id = _save_ready_resume(ai_client, skills=["Python"])
    excluded_resume_id = _save_ready_resume(ai_client, skills=["SQL"])
    _install_profile_ai_stub(
        monkeypatch,
        generated=_generated_profile(skills_all_of=["Python"]),
    )
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找熟练 Python 的候选人"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision_id = created.json()["current_revision"]["revision_id"]
    confirmed = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision_id},
    )
    assert confirmed.status_code == 200, confirmed.text

    search_calls: list[tuple[dict[str, object], set[str] | None]] = []

    def capture_search(session, request, **kwargs):
        search_calls.append((request.model_dump(mode="json"), kwargs.get("resume_ids")))
        return real_search_candidates(session, request, **kwargs)

    batch_calls: list[dict[str, object]] = []

    def fake_enqueue(session, **kwargs: object) -> SimpleNamespace:
        batch_calls.append({"session": session, **kwargs})
        job_version = session.get(JobVersion, kwargs["job_version_id"])
        assert job_version is not None
        batch = JobMatchBatch(
            organization_id=job_version.organization_id,
            job_version_id=job_version.id,
            status="queued",
            total_count=len(kwargs["resume_ids"]),
            completed_count=0,
            failed_count=0,
            max_attempts=1,
        )
        session.add(batch)
        session.flush()
        return SimpleNamespace(batch_id=batch.id, status=batch.status)

    monkeypatch.setattr(profile_service, "search_candidates", capture_search)
    monkeypatch.setattr(
        profile_service,
        "enqueue_job_version_match_batch",
        fake_enqueue,
    )
    started = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision_id, "limit": 10},
    )

    assert started.status_code == 200, started.text
    run_payload = started.json()
    assert run_payload["total_recalled_count"] == 1
    assert [item["resume_id"] for item in run_payload["candidate_recall"]["items"]] == [
        matching_resume_id
    ]
    assert len(batch_calls) == 1
    assert batch_calls[0]["resume_ids"] == [matching_resume_id]
    assert excluded_resume_id not in batch_calls[0]["resume_ids"]
    assert search_calls[0][0]["skills_all_of"] == ["Python"]
    assert search_calls[0][1] is None
    assert search_calls[-1][0]["skills_all_of"] == ["Python"]
    assert search_calls[-1][1] == {matching_resume_id}

    duplicate_start = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision_id, "limit": 10},
    )
    assert duplicate_start.status_code == 200, duplicate_start.text
    assert duplicate_start.json()["run_id"] == run_payload["run_id"]
    assert len(batch_calls) == 1
    # Public JD-batch endpoints must not expose the private profile carrier.
    assert ai_client.get(
        f"/v1/job-match-batches/{run_payload['job_match_batch_id']}"
    ).status_code == 404
    assert ai_client.get(
        f"/v1/job-match-batches/{run_payload['job_match_batch_id']}/items"
    ).status_code == 404

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            run = session.get(TalentSearchRun, run_payload["run_id"])
            assert run is not None
            assert run.hard_filter_snapshot == _profile_hard_filters(
                skills_all_of=["Python"]
            )
            assert run.recalled_resume_ids == [matching_resume_id]
            revision = session.get(TalentSearchProfileRevision, run.revision_id)
            assert revision is not None
            # A historic run must remain reproducible from its own snapshot
            # even if a later data repair touches the mutable revision row.
            revision.hard_filters = _profile_hard_filters(skills_all_of=["SQL"])
            session.commit()

    fetched = ai_client.get(
        f"/v1/talent-search-profiles/{profile_id}/runs/{run_payload['run_id']}"
    )
    assert fetched.status_code == 200, fetched.text
    assert [item["resume_id"] for item in fetched.json()["candidate_recall"]["items"]] == [
        matching_resume_id
    ]
    assert search_calls[-1][0]["skills_all_of"] == ["Python"]
    assert search_calls[-1][1] == {matching_resume_id}


def test_bachelor_degree_record_is_distinct_from_highest_degree_in_profile_recall(
    ai_client,
    monkeypatch,
) -> None:
    bachelor_resume_id = _save_ready_resume(ai_client, skills=["Python"])
    master_resume_id = _save_ready_resume(
        ai_client,
        skills=["Python"],
        education_degrees=["bachelor", "master"],
    )

    exact_highest = ai_client.post(
        "/v1/candidates/search",
        json={"highest_degree_in": ["bachelor"]},
    )
    assert exact_highest.status_code == 200, exact_highest.text
    assert exact_highest.json()["total_count"] == 1
    assert exact_highest.json()["items"][0]["resume_id"] == bachelor_resume_id

    any_bachelor_record = ai_client.post(
        "/v1/candidates/search",
        json={"education_degree_in": ["bachelor"]},
    )
    assert any_bachelor_record.status_code == 200, any_bachelor_record.text
    assert {item["resume_id"] for item in any_bachelor_record.json()["items"]} == {
        bachelor_resume_id,
        master_resume_id,
    }

    generated = _generated_profile()
    generated["hard_filters"] = _profile_hard_filters(
        institution_classifications_any_of=[],
        highest_degree_in=["bachelor"],
    )
    _install_profile_ai_stub(monkeypatch, generated=generated)
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找本科毕业的工程师"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision = created.json()["current_revision"]
    assert revision["hard_filters"]["education_degree_in"] == ["bachelor"]
    assert revision["hard_filters"]["highest_degree_in"] == []

    assert ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision["revision_id"]},
    ).status_code == 200

    def fake_enqueue(session, **kwargs: object) -> SimpleNamespace:
        job_version = session.get(JobVersion, kwargs["job_version_id"])
        assert job_version is not None
        batch = JobMatchBatch(
            organization_id=job_version.organization_id,
            job_version_id=job_version.id,
            status="completed",
            total_count=len(kwargs["resume_ids"]),
            completed_count=0,
            failed_count=0,
            max_attempts=1,
        )
        session.add(batch)
        session.flush()
        return SimpleNamespace(batch_id=batch.id, status=batch.status)

    monkeypatch.setattr(profile_service, "enqueue_job_version_match_batch", fake_enqueue)
    started = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision["revision_id"], "limit": 10},
    )
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["total_recalled_count"] == 2
    assert {item["resume_id"] for item in payload["candidate_recall"]["items"]} == {
        bachelor_resume_id,
        master_resume_id,
    }
    assert payload["applied_hard_filters"]["education_degree_in"] == ["bachelor"]


def test_bachelor_profile_normalizer_preserves_mixed_and_negative_degree_requests() -> None:
    mixed_generated = _generated_profile()
    mixed_generated["hard_filters"] = _profile_hard_filters(
        institution_classifications_any_of=["985"],
        highest_degree_in=["master", "doctor"],
    )
    mixed = profile_service._normalize_explicit_profile_intent(
        mixed_generated,
        request_message="寻找硕士及以上、本科毕业于985的工程师",
    )
    mixed_filters = mixed["hard_filters"]
    assert isinstance(mixed_filters, dict)
    assert mixed_filters["institution_classifications_any_of"] == ["985"]
    assert mixed_filters["highest_degree_in"] == ["master", "doctor"]
    assert mixed_filters["education_degree_in"] == []

    negative_generated = _generated_profile()
    negative_generated["hard_filters"] = _profile_hard_filters(
        institution_classifications_any_of=[],
        highest_degree_in=["master", "doctor"],
    )
    negative = profile_service._normalize_explicit_profile_intent(
        negative_generated,
        request_message="不要本科，只要硕士及以上",
    )
    negative_filters = negative["hard_filters"]
    assert isinstance(negative_filters, dict)
    assert negative_filters["highest_degree_in"] == ["master", "doctor"]
    assert negative_filters["education_degree_in"] == []


def test_project_experience_term_is_not_forced_into_exact_skill_recall(
    ai_client,
    monkeypatch,
) -> None:
    resume_id = _save_ready_resume(
        ai_client,
        skills=[],
        experience_types=["project"],
        source_suffix="项目中使用 LangChain 编排工具调用并完成上线。",
    )
    exact_skill = ai_client.post(
        "/v1/candidates/search",
        json={"skills_all_of": ["LangChain"]},
    )
    assert exact_skill.status_code == 200, exact_skill.text
    assert exact_skill.json()["total_count"] == 0

    generated = _generated_profile(skills_all_of=["LangChain"])
    generated["hard_filters"] = _profile_hard_filters(
        skills_all_of=["LangChain"],
        institution_classifications_any_of=[],
        highest_degree_in=[],
    )
    _install_profile_ai_stub(monkeypatch, generated=generated)
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找有 LangChain 项目经验的工程师"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision = created.json()["current_revision"]
    assert revision["hard_filters"]["skills_all_of"] == []
    assert any(
        "LangChain" in item["label"]
        for item in revision["verification_requirements"]
    )

    assert ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision["revision_id"]},
    ).status_code == 200

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            revision_row = session.get(TalentSearchProfileRevision, revision["revision_id"])
            assert revision_row is not None
            assert revision_row.match_job_version_id is not None
            private_requirement = session.scalar(
                select(JobRequirement)
                .where(JobRequirement.job_version_id == revision_row.match_job_version_id)
                .where(JobRequirement.raw_requirement.contains("LangChain"))
            )
            assert private_requirement is not None
            metadata = private_requirement.normalized_value
            assert metadata["evidence_hint"]
            assert metadata["evidence_policy"] == {
                "kind": "experience_detail_terms",
                "allowed_experience_types": ["project"],
                "terms_all_of": ["LangChain"],
            }

    def fake_enqueue(session, **kwargs: object) -> SimpleNamespace:
        job_version = session.get(JobVersion, kwargs["job_version_id"])
        assert job_version is not None
        batch = JobMatchBatch(
            organization_id=job_version.organization_id,
            job_version_id=job_version.id,
            status="completed",
            total_count=len(kwargs["resume_ids"]),
            completed_count=0,
            failed_count=0,
            max_attempts=1,
        )
        session.add(batch)
        session.flush()
        return SimpleNamespace(batch_id=batch.id, status=batch.status)

    monkeypatch.setattr(profile_service, "enqueue_job_version_match_batch", fake_enqueue)
    started = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision["revision_id"], "limit": 10},
    )
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["total_recalled_count"] == 1
    assert payload["candidate_recall"]["items"][0]["resume_id"] == resume_id


def test_english_project_experience_term_is_not_forced_into_exact_skill_recall() -> None:
    generated = _generated_profile(skills_all_of=["LangChain"])
    generated["hard_filters"] = _profile_hard_filters(
        skills_all_of=["LangChain"],
        institution_classifications_any_of=[],
        highest_degree_in=[],
    )

    normalized = profile_service._normalize_explicit_profile_intent(
        generated,
        request_message="Find engineers with LangChain project experience",
    )

    hard_filters = normalized["hard_filters"]
    assert isinstance(hard_filters, dict)
    assert hard_filters["skills_all_of"] == []
    verification_requirements = normalized["verification_requirements"]
    assert isinstance(verification_requirements, list)
    assert any(
        isinstance(item, dict) and "LangChain" in str(item.get("label", ""))
        for item in verification_requirements
    )


def test_profile_output_deduplicates_nonsemantic_aliases_and_questions() -> None:
    payload = _generated_profile()
    payload["aliases"] = ["LangChain 工程师", " LangChain 工程师 ", "LLM 应用工程师"]
    payload["clarifying_questions"] = ["是否有行业经验要求？", "是否有行业经验要求？"]

    normalized = validate_talent_search_profile_output(payload)

    assert normalized["aliases"] == ["LangChain 工程师", "LLM 应用工程师"]
    assert normalized["clarifying_questions"] == ["是否有行业经验要求？"]


def test_zero_profile_run_persists_funnel_and_uses_frozen_snapshot(
    ai_client,
    monkeypatch,
) -> None:
    _save_ready_resume(ai_client, skills=["Python"])
    _install_profile_ai_stub(
        monkeypatch,
        generated=_generated_profile(skills_all_of=["Rust"]),
    )
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找精确技能为 Rust 的工程师"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision = created.json()["current_revision"]
    assert ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision["revision_id"]},
    ).status_code == 200

    started = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision["revision_id"], "limit": 10},
    )
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["total_recalled_count"] == 0
    assert payload["recall_diagnostics"] is not None
    diagnostics = payload["recall_diagnostics"]
    assert diagnostics["eligible_resume_count"] == 1
    assert diagnostics["strict_match_count"] == 0
    assert diagnostics["steps"][-1] == {
        "key": "skills_all_of",
        "label": "精确技能：Rust（全部）",
        "remaining_count": 0,
        "removed_count": 1,
    }

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            run = session.get(TalentSearchRun, payload["run_id"])
            assert run is not None
            assert run.recall_diagnostics == diagnostics
            revision_row = session.get(TalentSearchProfileRevision, run.revision_id)
            assert revision_row is not None
            revision_row.hard_filters = _profile_hard_filters(skills_all_of=["Python"])
            session.commit()

    fetched = ai_client.get(
        f"/v1/talent-search-profiles/{profile_id}/runs/{payload['run_id']}"
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["recall_diagnostics"] == diagnostics
    assert fetched.json()["applied_hard_filters"]["skills_all_of"] == ["Rust"]


def test_stale_profile_revision_cannot_be_confirmed(ai_client, monkeypatch) -> None:
    _install_profile_ai_stub(monkeypatch)
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找有真实 Agent 项目经验的人"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    first_revision_id = created.json()["current_revision"]["revision_id"]

    refined = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/refine",
        json={
            "revision_id": first_revision_id,
            "message": "补充：要核验 RAG 的项目职责和落地结果。",
        },
    )
    assert refined.status_code == 200, refined.text
    second_revision_id = refined.json()["current_revision"]["revision_id"]
    assert second_revision_id != first_revision_id

    stale_confirmation = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": first_revision_id},
    )
    assert stale_confirmation.status_code == 409, stale_confirmation.text
    assert stale_confirmation.json()["detail"] == "talent_search_profile_revision_not_current"

    current_confirmation = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": second_revision_id},
    )
    assert current_confirmation.status_code == 200, current_confirmation.text


def test_profile_run_returns_only_its_private_semantic_match_results(
    ai_client,
    monkeypatch,
) -> None:
    resume_id = _save_ready_resume(ai_client, skills=["Python"])
    _install_profile_ai_stub(
        monkeypatch,
        generated=_generated_profile(skills_all_of=["Python"]),
    )
    created = ai_client.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找 Python 与 Agent 交付经验"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision_id = created.json()["current_revision"]["revision_id"]
    assert ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision_id},
    ).status_code == 200

    def fake_enqueue(session, **kwargs: object) -> SimpleNamespace:
        job_version = session.get(JobVersion, kwargs["job_version_id"])
        assert job_version is not None
        batch = JobMatchBatch(
            organization_id=job_version.organization_id,
            job_version_id=job_version.id,
            status="completed",
            total_count=1,
            completed_count=1,
            failed_count=0,
            max_attempts=1,
        )
        session.add(batch)
        session.flush()
        return SimpleNamespace(batch_id=batch.id, status=batch.status)

    monkeypatch.setattr(profile_service, "enqueue_job_version_match_batch", fake_enqueue)
    started = ai_client.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision_id, "limit": 10},
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with bypass_organization_scope(session):
            run = session.get(TalentSearchRun, run_id)
            assert run is not None and run.job_match_batch_id is not None
            batch = session.get(JobMatchBatch, run.job_match_batch_id)
            assert batch is not None
            job_version = session.get(JobVersion, batch.job_version_id)
            assert job_version is not None and job_version.requirements
            snapshot = session.scalar(
                select(ResumeFactSnapshot).where(ResumeFactSnapshot.resume_id == resume_id)
            )
            assert snapshot is not None
            match = JobMatch(
                organization_id=job_version.organization_id,
                job_id=job_version.job_id,
                job_version_id=job_version.id,
                resume_id=resume_id,
                fact_snapshot_id=snapshot.id,
                facts_version=snapshot.facts_version,
                job_version=job_version.version,
                total_score=0.8,
                must_have_passed=True,
                evidence_coverage=0.8,
                hard_requirement_status="pass",
                analysis={"summary": "具备可核验的 Agent 项目职责。"},
                status="succeeded",
                model_name="test-model",
            )
            session.add(match)
            session.flush()
            session.add(
                JobMatchRequirementResult(
                    job_match_id=match.id,
                    requirement_id=job_version.requirements[0].id,
                    outcome="met",
                    reason="项目职责中有明确的 Agent 系统交付证据。",
                    fact_ids=["experience:0"],
                    missing_or_uncertain=None,
                    score_contribution=0.8,
                )
            )
            session.add(
                JobMatchBatchItem(
                    organization_id=job_version.organization_id,
                    batch_id=batch.id,
                    resume_id=resume_id,
                    fact_snapshot_id=snapshot.id,
                    facts_version=snapshot.facts_version,
                    status="completed",
                    attempt_count=1,
                    job_match_id=match.id,
                )
            )
            session.commit()

    result = ai_client.get(
        f"/v1/talent-search-profiles/{profile_id}/runs/{run_id}"
    )
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["match_completed_count"] == 1
    assert len(payload["match_results"]) == 1
    assert payload["match_results"][0]["resume_id"] == resume_id
    assert payload["match_results"][0]["match_score"] == 100.0
    assert payload["match_results"][0]["match_confidence"] == 0.8
    assert "job_version_id" not in payload["match_results"][0]


def test_foreign_talent_search_profile_routes_are_indistinguishable_from_missing(
    workspace_clients,
    monkeypatch,
) -> None:
    _install_profile_ai_stub(monkeypatch)
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Talent profile workspace alpha",
        full_name="Alpha Admin",
        email="talent-profile-alpha@example.test",
        password="tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Talent profile workspace beta",
        full_name="Beta Admin",
        email="talent-profile-beta@example.test",
        password="tenant-test-password-b",
    )

    created = client_b.post(
        "/v1/talent-search-profiles/generate",
        json={"message": "寻找可核验 AI 项目经历的人选"},
    )
    assert created.status_code == 200, created.text
    profile_id = created.json()["profile_id"]
    revision_id = created.json()["current_revision"]["revision_id"]
    assert client_b.post(
        f"/v1/talent-search-profiles/{profile_id}/confirm",
        json={"revision_id": revision_id},
    ).status_code == 200
    run = client_b.post(
        f"/v1/talent-search-profiles/{profile_id}/runs",
        json={"revision_id": revision_id, "limit": 10},
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]

    foreign_responses = (
        client_a.get(f"/v1/talent-search-profiles/{profile_id}"),
        client_a.post(
            f"/v1/talent-search-profiles/{profile_id}/refine",
            json={"revision_id": revision_id, "message": "补充需要 Python"},
        ),
        client_a.post(
            f"/v1/talent-search-profiles/{profile_id}/confirm",
            json={"revision_id": revision_id},
        ),
        client_a.post(
            f"/v1/talent-search-profiles/{profile_id}/runs",
            json={"revision_id": revision_id, "limit": 10},
        ),
        client_a.get(
            f"/v1/talent-search-profiles/{profile_id}/runs/{run_id}"
        ),
    )
    for response in foreign_responses:
        assert response.status_code == 404, response.text


def test_experience_type_checklist_requires_every_selected_type(client) -> None:
    both_resume_id = _save_ready_resume(
        client,
        skills=["Python"],
        experience_types=["employment", "internship"],
    )
    _save_ready_resume(
        client,
        skills=["Python"],
        experience_types=["employment"],
    )

    response = client.post(
        "/v1/candidates/search",
        json={"experience_types_all_of": ["employment", "internship"]},
    )
    assert response.status_code == 200, response.text
    assert [item["resume_id"] for item in response.json()["items"]] == [both_resume_id]
    assert "experience_types_all_of" in response.json()["items"][0]["matched_filters"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Female AI engineer"),
        ("summary", "候选人年龄 30 岁以下"),
    ],
)
def test_profile_generator_rejects_discriminatory_text_in_every_visible_field(
    field: str,
    value: str,
) -> None:
    payload = _generated_profile()
    payload[field] = value
    with pytest.raises(DeepSeekProviderError, match="talent_profile_disallowed_condition"):
        validate_talent_search_profile_output(payload)

    hint_payload = _generated_profile()
    requirements = hint_payload["verification_requirements"]
    assert isinstance(requirements, list)
    requirements[0]["evidence_hint"] = "核验候选人的 gender 是否符合要求。"
    with pytest.raises(DeepSeekProviderError, match="talent_profile_disallowed_condition"):
        validate_talent_search_profile_output(hint_payload)
