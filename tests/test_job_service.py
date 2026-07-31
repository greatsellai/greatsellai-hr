from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Job, JobRequirement, JobVersion
from app.schemas import JobCreate, JobMatchCreate, JobRequirements
from app.services import job_service
from app.services.deepseek_provider import FACT_SNAPSHOT_SCHEMA_VERSION
from test_filter_mvp_contract import _save_ready_resume
from test_tenant_isolation import _register_and_login, workspace_clients


def _job_payload(*, requirements: JobRequirements | None = None) -> JobCreate:
    return JobCreate(
        title="Backend Engineer",
        jd_text=(
            "Must have Python experience.\n"
            "Must have Go experience.\n"
            "Kubernetes experience is preferred."
        ),
        requirements=requirements or JobRequirements(),
    )


def _create_job(ai_client, *, requirements: JobRequirements | None = None) -> dict[str, object]:
    database = ai_client.app.state.database
    with database.session_factory() as session:
        response = job_service.create_job(
            session,
            payload=_job_payload(requirements=requirements),
        )
        session.commit()
        return response.model_dump()


def test_private_match_requirement_payload_keeps_evidence_metadata() -> None:
    requirement = JobRequirement(
        job_version_id="job-version-1",
        requirement_key="profile-001",
        priority="must_have",
        category="other",
        raw_requirement="WidgetFlow project delivery experience",
        normalized_value={
            "terms": ["WidgetFlow project delivery experience"],
            "evidence_hint": "Verify WidgetFlow use in a project record.",
            "evidence_policy": {
                "kind": "experience_detail_terms",
                "allowed_experience_types": ["project"],
                "terms_all_of": ["WidgetFlow"],
            },
        },
        minimum_months=None,
        weight=10000,
        clause_ids=["clause-001"],
        sort_order=0,
    )

    assert job_service._match_requirement_payload(requirement) == {
        "requirement_id": "profile-001",
        "requirement_text": "WidgetFlow project delivery experience",
        "priority": "must_have",
        "clause_ids": ["clause-001"],
        "evidence_hint": "Verify WidgetFlow use in a project record.",
        "evidence_policy": {
            "kind": "experience_detail_terms",
            "allowed_experience_types": ["project"],
            "terms_all_of": ["WidgetFlow"],
        },
    }


def test_manual_requirements_are_clause_grounded_and_confirmed(ai_client) -> None:
    created = _create_job(
        ai_client,
        requirements=JobRequirements(
            must_have=["Python experience", "Go experience"],
            preferred=["Kubernetes experience"],
        ),
    )

    assert created["status"] == "confirmed"
    assert [clause["clause_id"] for clause in created["clauses"]] == [
        "clause-001",
        "clause-002",
        "clause-003",
    ]
    assert [
        (requirement["requirement_key"], requirement["clause_ids"], requirement["weight"])
        for requirement in created["requirements"]
    ] == [
        ("req-001", ["clause-001"], 3500),
        ("req-002", ["clause-002"], 3500),
        ("req-003", ["clause-003"], 3000),
    ]

    database = ai_client.app.state.database
    with database.session_factory() as session:
        with pytest.raises(
            job_service.JobServiceError,
            match="job_requirement_not_grounded_in_jd",
        ):
            job_service.create_job(
                session,
                payload=_job_payload(
                    requirements=JobRequirements(must_have=["Rust experience"])
                ),
            )
        session.rollback()


def test_confirmed_jobs_can_be_listed_for_the_workspace_switcher(ai_client) -> None:
    first = _create_job(
        ai_client,
        requirements=JobRequirements(must_have=["Python experience"]),
    )
    second = _create_job(
        ai_client,
        requirements=JobRequirements(must_have=["Go experience"]),
    )

    response = ai_client.get("/v1/jobs/confirmed-versions")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert {item["job_version_id"] for item in payload} == {
        first["job_version_id"],
        second["job_version_id"],
    }
    assert all(item["status"] == "confirmed" for item in payload)


def test_original_jd_publish_persists_verbatim_without_calling_any_provider(
    ai_client,
    monkeypatch,
) -> None:
    def provider_must_not_run(**_: object) -> dict[str, object]:
        raise AssertionError("original JD publishing must not call an LLM provider")

    monkeypatch.setattr(job_service, "generate_jd_from_brief", provider_must_not_run)
    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        provider_must_not_run,
    )
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        provider_must_not_run,
    )
    raw_jd = "  Original source JD.\r\n\r\n- Preserve this whitespace.\r\n  "

    created = ai_client.post(
        "/v1/jobs/publish-original",
        json={"title": " Source JD ", "jd_text": raw_jd},
    )

    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["title"] == "Source JD"
    assert payload["raw_text"] == raw_jd
    assert payload["status"] == "confirmed"
    assert payload["requirements"] == []

    database = ai_client.app.state.database
    with database.session_factory() as session:
        persisted = session.get(JobVersion, payload["job_version_id"])
        assert persisted is not None
        assert persisted.raw_text == raw_jd
        assert persisted.job.jd_text == raw_jd
        assert persisted.status == "confirmed"
        assert persisted.requirements == []
        jobs = session.scalars(select(Job).where(Job.kind == "job")).all()
        assert [job.id for job in jobs] == [payload["job_id"]]
        assert jobs[0].version == 1
        assert jobs[0].requirements == {"must_have": [], "preferred": []}

    matching = ai_client.post(
        f"/v1/job-versions/{payload['job_version_id']}/match-all"
    )
    assert matching.status_code == 409, matching.text
    assert matching.json()["detail"] == "job_version_has_no_requirements"


def test_original_jd_publish_version_reuses_existing_job_and_preserves_source_text(
    ai_client,
) -> None:
    first_raw_jd = "Must have Python experience."
    second_raw_jd = "  Original v2.\r\n\r\n- Keep all source whitespace.\r\n  "

    first = ai_client.post(
        "/v1/jobs",
        json={
            "title": "Source JD",
            "jd_text": first_raw_jd,
            "requirements": {"must_have": ["Python experience"], "preferred": []},
        },
    )
    assert first.status_code == 200, first.text
    first_payload = first.json()

    second = ai_client.post(
        f"/v1/jobs/{first_payload['job_id']}/publish-original-version",
        json={"title": "Source JD revision", "jd_text": second_raw_jd},
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()

    assert second_payload["job_id"] == first_payload["job_id"]
    assert first_payload["version"] == 1
    assert second_payload["version"] == 2
    assert second_payload["raw_text"] == second_raw_jd
    assert second_payload["status"] == "confirmed"
    assert second_payload["requirements"] == []
    assert [clause["text"] for clause in second_payload["clauses"]] == [
        "Original v2.",
        "Keep all source whitespace.",
    ]

    versions = ai_client.get(f"/v1/jobs/{first_payload['job_id']}/versions")
    assert versions.status_code == 200, versions.text
    assert [(item["job_id"], item["version"]) for item in versions.json()] == [
        (first_payload["job_id"], 2),
        (first_payload["job_id"], 1),
    ]

    database = ai_client.app.state.database
    with database.session_factory() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "job")).all()
        assert [job.id for job in jobs] == [first_payload["job_id"]]
        assert jobs[0].version == 2
        assert jobs[0].title == "Source JD revision"
        assert jobs[0].jd_text == second_raw_jd
        assert jobs[0].requirements == {"must_have": [], "preferred": []}
        persisted_versions = session.scalars(
            select(JobVersion)
            .where(JobVersion.job_id == first_payload["job_id"])
            .order_by(JobVersion.version)
        ).all()
        assert [(item.version, item.raw_text) for item in persisted_versions] == [
            (1, first_raw_jd),
            (2, second_raw_jd),
        ]
        assert len(persisted_versions[0].requirements) == 1
        assert persisted_versions[1].requirements == []


def test_original_jd_publish_version_rejects_missing_or_foreign_job(
    workspace_clients,
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Original JD alpha",
        full_name="Original JD alpha admin",
        email="original-jd-alpha@example.test",
        password="tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Original JD beta",
        full_name="Original JD beta admin",
        email="original-jd-beta@example.test",
        password="tenant-test-password-b",
    )
    created = client_b.post(
        "/v1/jobs/publish-original",
        json={"title": "Private role", "jd_text": "Private source JD."},
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]

    missing = client_a.post(
        "/v1/jobs/not-a-real-job/publish-original-version",
        json={"title": "Revision", "jd_text": "Revision source JD."},
    )
    foreign = client_a.post(
        f"/v1/jobs/{job_id}/publish-original-version",
        json={"title": "Revision", "jd_text": "Revision source JD."},
    )

    for response in (missing, foreign):
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "job_not_found"


@pytest.mark.parametrize(
    "jd_text, expected_error",
    [
        (" \t\r\n", "original_jd_text_must_not_be_blank"),
        ("Valid JD\x00but unsafe", "original_jd_text_must_not_contain_nul"),
    ],
)
def test_original_jd_publish_rejects_blank_or_nul_source_text(
    client,
    jd_text: str,
    expected_error: str,
) -> None:
    response = client.post(
        "/v1/jobs/publish-original",
        json={"title": "Source JD", "jd_text": jd_text},
    )

    assert response.status_code == 422, response.text
    assert expected_error in response.text


def test_original_jd_is_listed_but_not_selected_as_default_match_target(client) -> None:
    matchable = client.post(
        "/v1/jobs",
        json={
            "title": "Matchable JD",
            "jd_text": "Must have Python experience.",
            "requirements": {"must_have": ["Python experience"], "preferred": []},
        },
    )
    assert matchable.status_code == 200, matchable.text

    original = client.post(
        "/v1/jobs/publish-original",
        json={"title": "Source JD", "jd_text": "No AI extraction for this JD."},
    )
    assert original.status_code == 200, original.text

    listed = client.get("/v1/jobs/confirmed-versions")
    assert listed.status_code == 200, listed.text
    assert {item["job_version_id"] for item in listed.json()} == {
        matchable.json()["job_version_id"],
        original.json()["job_version_id"],
    }

    default_target = client.get("/v1/jobs/latest-confirmed-version")
    assert default_target.status_code == 200, default_target.text
    assert default_target.json()["job_version_id"] == matchable.json()["job_version_id"]


def _fake_requirement_extraction(**kwargs: object) -> dict[str, object]:
    clauses = kwargs["clauses"]
    assert clauses == [
        {"clause_id": "clause-001", "text": "Must have Python experience."},
        {"clause_id": "clause-002", "text": "Must have Go experience."},
        {
            "clause_id": "clause-003",
            "text": "Kubernetes experience is preferred.",
        },
    ]
    return {
        "schema_version": "jd_requirements.v1",
        "clause_coverage": [
            {"clause_id": "clause-001", "requirement_ids": ["requirement-001"]},
            {"clause_id": "clause-002", "requirement_ids": ["requirement-002"]},
            {"clause_id": "clause-003", "requirement_ids": ["requirement-003"]},
        ],
        "requirements": [
            {
                "requirement_id": "requirement-001",
                "requirement_text": "Python experience",
                "priority": "must_have",
                "clause_ids": ["clause-001"],
            },
            {
                "requirement_id": "requirement-002",
                "requirement_text": "Go experience",
                "priority": "must_have",
                "clause_ids": ["clause-002"],
            },
            {
                "requirement_id": "requirement-003",
                "requirement_text": "Kubernetes experience",
                "priority": "preferred",
                "clause_ids": ["clause-003"],
            },
        ],
    }


def _fake_generated_jd(**kwargs: object) -> dict[str, object]:
    assert kwargs["title"] == "Backend Engineer"
    assert kwargs["brief"] == "Build reliable recruiting platform services."
    return {
        "title": "Backend Engineer",
        "jd_text": (
            "Responsibilities\n"
            "Build reliable recruiting platform services.\n\n"
            "Requirements\n"
            "Must have Python experience.\n"
            "Kubernetes experience is preferred."
        ),
        "requirements": {
            "must_have": ["Must have Python experience."],
            "preferred": ["Kubernetes experience is preferred."],
        },
    }


def test_generated_jd_can_be_saved_as_a_confirmed_job_in_one_persistence_call(
    ai_client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(job_service, "generate_jd_from_brief", _fake_generated_jd)

    generated = ai_client.post(
        "/v1/jobs/generate-jd",
        json={
            "title": "Backend Engineer",
            "brief": "Build reliable recruiting platform services.",
        },
    )

    assert generated.status_code == 200, generated.text
    generated_payload = generated.json()
    assert generated_payload["requirements"]["must_have"] == [
        "Must have Python experience."
    ]
    persisted = ai_client.post("/v1/jobs", json=generated_payload)
    assert persisted.status_code == 200, persisted.text
    assert persisted.json()["status"] == "confirmed"


def test_generate_jd_api_returns_503_without_server_side_key(client) -> None:
    response = client.post(
        "/v1/jobs/generate-jd",
        json={"title": "Backend Engineer", "brief": "Build reliable services."},
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "deepseek_api_key_not_configured"


def test_generate_jd_api_returns_stable_provider_error(ai_client, monkeypatch) -> None:
    def provider_failure(**kwargs: object) -> dict[str, object]:
        raise job_service.DeepSeekProviderError("deepseek_response_truncated")

    monkeypatch.setattr(job_service, "generate_jd_from_brief", provider_failure)
    response = ai_client.post(
        "/v1/jobs/generate-jd",
        json={"title": "Backend Engineer", "brief": "Build reliable services."},
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "jd_generation_response_truncated"


def test_existing_jd_extraction_reports_truncated_provider_output(ai_client, monkeypatch) -> None:
    created = ai_client.post("/v1/jobs", json=_job_payload().model_dump())
    assert created.status_code == 200, created.text

    def provider_failure(**kwargs: object) -> dict[str, object]:
        raise job_service.DeepSeekProviderError("deepseek_response_truncated")

    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        provider_failure,
    )
    response = ai_client.post(f"/v1/job-versions/{created.json()['job_version_id']}/extract")

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "jd_requirements_response_truncated"


def test_draft_ai_extraction_can_be_reviewed_then_confirmed(ai_client, monkeypatch) -> None:
    draft = _create_job(ai_client)
    assert draft["status"] == "draft"
    assert draft["requirements"] == []

    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        _fake_requirement_extraction,
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        extracted = job_service.extract_job_version_requirements(
            session,
            job_version_id=draft["job_version_id"],
            settings=ai_client.app.state.settings,
        )
        session.commit()

    assert extracted.status == "draft"
    assert [item.requirement_key for item in extracted.requirements] == [
        "requirement-001",
        "requirement-002",
        "requirement-003",
    ]
    assert sum(item.weight for item in extracted.requirements) == 10000

    with database.session_factory() as session:
        confirmed = job_service.confirm_job_version(
            session,
            job_version_id=draft["job_version_id"],
        )
        session.commit()
    assert confirmed.status == "confirmed"

    with database.session_factory() as session:
        with pytest.raises(
            job_service.JobServiceError,
            match="confirmed_job_version_cannot_be_extracted",
        ):
            job_service.extract_job_version_requirements(
                session,
                job_version_id=draft["job_version_id"],
                settings=ai_client.app.state.settings,
            )


def _fake_match_with_unknown_must_have(**kwargs: object) -> dict[str, object]:
    snapshot = kwargs["fact_snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["schema_version"] == FACT_SNAPSHOT_SCHEMA_VERSION
    assert "raw_text" not in snapshot
    assert kwargs["confirmed_requirements"] == [
        {
            "requirement_id": "requirement-001",
            "requirement_text": "Python experience",
            "priority": "must_have",
            "clause_ids": ["clause-001"],
        },
        {
            "requirement_id": "requirement-002",
            "requirement_text": "Go experience",
            "priority": "must_have",
            "clause_ids": ["clause-002"],
        },
        {
            "requirement_id": "requirement-003",
            "requirement_text": "Kubernetes experience",
            "priority": "preferred",
            "clause_ids": ["clause-003"],
        },
    ]
    return {
        "schema_version": "jd_match.v1",
        "requirement_matches": [
            {
                "requirement_id": "requirement-001",
                "status": "met",
                "rationale": "Python is an explicit skill in the fact snapshot.",
                "fact_ids": ["skill-001"],
                "uncertainties": [],
            },
            {
                "requirement_id": "requirement-002",
                "status": "unknown",
                "rationale": "The snapshot does not state Go experience.",
                "fact_ids": [],
                "uncertainties": ["No Go fact is available."],
            },
            {
                "requirement_id": "requirement-003",
                "status": "partial",
                "rationale": "A related infrastructure fact is available.",
                "fact_ids": ["skill-001"],
                "uncertainties": ["Kubernetes depth is not stated."],
            },
        ],
        # The service, not the model, is responsible for forcing an
        # information-insufficient result into recruiter review.
        "needs_human_review": False,
    }


def test_snapshot_match_persists_evidence_and_unknown_hard_requirement(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education 清华大学 计算机 工作经历 "
            "Acme Python Engineer Skills Python SQL Kubernetes"
        ),
    )
    job = _create_job(ai_client)
    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        _fake_requirement_extraction,
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        job_service.extract_job_version_requirements(
            session,
            job_version_id=job["job_version_id"],
            settings=ai_client.app.state.settings,
        )
        session.commit()
    with database.session_factory() as session:
        job_service.confirm_job_version(
            session,
            job_version_id=job["job_version_id"],
        )
        session.commit()
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _fake_match_with_unknown_must_have,
    )

    with database.session_factory() as session:
        matched = job_service.run_job_match(
            session,
            resume_id=resume_id,
            payload=JobMatchCreate(job_version_id=job["job_version_id"]),
            settings=ai_client.app.state.settings,
        )
        session.commit()

    assert matched.fact_snapshot_id
    assert matched.facts_version == 1
    assert matched.total_score == 50.0
    assert matched.evidence_coverage == 65.0
    assert matched.hard_requirement_status == "information_insufficient"
    assert matched.must_have_passed is None
    assert matched.status == "needs_review"
    assert [
        (
            result.requirement_key,
            result.outcome,
            result.fact_ids,
            result.score_contribution,
        )
        for result in matched.requirement_results
    ] == [
        ("requirement-001", "met", ["skill-001"], 35.0),
        ("requirement-002", "unknown", [], 0.0),
        ("requirement-003", "partial", ["skill-001"], 15.0),
    ]
    assert matched.requirement_results[0].requirement_text == "Python experience"
    assert matched.requirement_results[0].clause_ids == ["clause-001"]

    with database.session_factory() as session:
        persisted = job_service.get_job_match(session, match_id=matched.match_id)
    assert persisted.fact_snapshot_id == matched.fact_snapshot_id
    assert persisted.requirement_results[0].fact_ids == ["skill-001"]


def test_jd_ai_operations_require_a_server_side_key(client) -> None:
    draft = _create_job(client)
    database = client.app.state.database
    with database.session_factory() as session:
        with pytest.raises(
            job_service.JobServiceError,
            match="deepseek_api_key_not_configured",
        ):
            job_service.extract_job_version_requirements(
                session,
                job_version_id=draft["job_version_id"],
                settings=client.app.state.settings,
            )
        with pytest.raises(
            job_service.JobServiceError,
            match="deepseek_api_key_not_configured",
        ):
            job_service.run_job_match(
                session,
                resume_id="not-a-real-resume",
                payload=JobMatchCreate(job_version_id=draft["job_version_id"]),
                settings=client.app.state.settings,
            )


def test_job_extract_api_returns_503_without_server_side_key(client) -> None:
    created = client.post("/v1/jobs", json=_job_payload().model_dump())
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "draft"

    extracted = client.post(
        f"/v1/job-versions/{created.json()['job_version_id']}/extract"
    )
    assert extracted.status_code == 503, extracted.text
    assert extracted.json()["detail"] == "deepseek_api_key_not_configured"


def test_job_api_extract_confirm_and_snapshot_match(ai_client, monkeypatch) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education 清华大学 计算机 工作经历 "
            "Acme Python Engineer Skills Python SQL Kubernetes"
        ),
    )
    created = ai_client.post("/v1/jobs", json=_job_payload().model_dump())
    assert created.status_code == 200, created.text
    job_version_id = created.json()["job_version_id"]

    monkeypatch.setattr(
        job_service,
        "extract_jd_requirements_from_clauses",
        _fake_requirement_extraction,
    )
    extracted = ai_client.post(f"/v1/job-versions/{job_version_id}/extract")
    assert extracted.status_code == 200, extracted.text
    assert [item["requirement_key"] for item in extracted.json()["requirements"]] == [
        "requirement-001",
        "requirement-002",
        "requirement-003",
    ]

    confirmed = ai_client.post(f"/v1/job-versions/{job_version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _fake_match_with_unknown_must_have,
    )
    matched = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": job_version_id},
    )
    assert matched.status_code == 200, matched.text
    match_payload = matched.json()
    assert match_payload["hard_requirement_status"] == "information_insufficient"
    assert match_payload["must_have_passed"] is None
    assert match_payload["requirement_results"][0]["fact_ids"] == ["skill-001"]

    fetched = ai_client.get(f"/v1/job-matches/{match_payload['match_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["fact_snapshot_id"] == match_payload["fact_snapshot_id"]
    assert fetched.json()["requirement_results"][0]["clause_ids"] == ["clause-001"]

    by_resume = ai_client.get(f"/v1/resumes/{resume_id}/job-matches")
    assert by_resume.status_code == 200, by_resume.text
    assert [item["match_id"] for item in by_resume.json()] == [match_payload["match_id"]]

    by_job_version = ai_client.get(f"/v1/job-versions/{job_version_id}/matches")
    assert by_job_version.status_code == 200, by_job_version.text
    assert [item["match_id"] for item in by_job_version.json()] == [
        match_payload["match_id"]
    ]


def _fake_preferred_only_match(**kwargs: object) -> dict[str, object]:
    requirements = kwargs["confirmed_requirements"]
    assert requirements == [
        {
            "requirement_id": "req-001",
            "requirement_text": "Kubernetes experience",
            "priority": "preferred",
            "clause_ids": ["clause-003"],
        }
    ]
    return {
        "schema_version": "jd_match.v1",
        "requirement_matches": [
            {
                "requirement_id": "req-001",
                "status": "met",
                "rationale": "Kubernetes is explicitly listed as a skill.",
                "fact_ids": ["skill-001"],
                "uncertainties": [],
            }
        ],
        "needs_human_review": False,
    }


def test_preferred_only_jd_has_no_hard_requirement_verdict(ai_client, monkeypatch) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Skills Python SQL Kubernetes"
        ),
    )
    job = _create_job(
        ai_client,
        requirements=JobRequirements(preferred=["Kubernetes experience"]),
    )
    assert job["status"] == "confirmed"
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _fake_preferred_only_match,
    )
    database = ai_client.app.state.database
    with database.session_factory() as session:
        matched = job_service.run_job_match(
            session,
            resume_id=resume_id,
            payload=JobMatchCreate(job_version_id=job["job_version_id"]),
            settings=ai_client.app.state.settings,
        )
        session.commit()

    assert matched.hard_requirement_status == "not_applicable"
    assert matched.must_have_passed is None
    assert matched.status == "succeeded"
