from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.integration_read_schemas import (
    IntegrationCandidateEvidenceRequest,
    IntegrationCandidateSearchRequest,
)
from app.models import (
    Candidate,
    Job,
    JobVersion,
    Organization,
    Resume,
    ResumeEducation,
    ResumeExperience,
    ResumeFactSnapshot,
    ResumeLanguageCredential,
    ResumeScholarship,
    ResumeSkill,
    ResumeSourceBlock,
    ResumeSummary,
)
from app.services.identity_service import AuthPrincipal
from app.services.integration_auth_service import authenticate_integration_token
from app.services.integration_read_service import (
    IntegrationReadError,
    fact_reference_ids,
    get_candidate_evidence,
    get_candidate_assessments,
    get_candidate_profile,
    get_job_requirements,
    search_candidates,
)
from app.tenant_scope import bypass_organization_scope
from test_integration_auth_helpers import make_context


def _fact_payload(label: str, block_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": "resume_facts.v1",
        "source_block_ids": block_ids,
        "derived": {
            "is_985_211": label == "alpha",
            "highest_degree": {
                "alpha": "bachelor",
                "beta": "master",
                "gamma": "associate",
            }[label],
            "employment_months": {"alpha": 36, "beta": 6, "gamma": 0}[label],
            "employment_or_internship_months": {"alpha": 48, "beta": 12, "gamma": 0}[
                label
            ],
            # Demographics exist in the canonical source but must never be projected.
            "gender": "female",
            "birth_date": "1999-01-01",
        },
        "education": [
            {
                "fact_id": f"fact-{label}-education",
                "school_name_raw": f"{label.title()} University",
                "degree": {"alpha": "bachelor", "beta": "master", "gamma": "associate"}[
                    label
                ],
                "major_raw": "Computer Science" if label == "alpha" else "Design",
                "start_month": "2020-09",
                "end_month": {
                    "alpha": "2026-06",
                    "beta": "2024-06",
                    "gamma": "2023-06",
                }[label],
                "institution_classification": {
                    "alpha": "985",
                    "beta": "overseas",
                    "gamma": None,
                }[label],
                "institution_tiers": [],
                "evidence_block_ids": block_ids[:1],
            }
        ],
        "experiences": [
            {
                "fact_id": f"fact-{label}-experience",
                "experience_type": {
                    "alpha": "employment",
                    "beta": "internship",
                    "gamma": "volunteer",
                }[label],
                "experience_name_raw": f"{label.title()} project",
                "organization_name_raw": (
                    "Acme Commerce" if label == "alpha" else "Design Studio"
                ),
                "title_raw": "Automation Engineer" if label == "alpha" else "Designer",
                "start_month": "2023-01",
                "end_month": "2025-01",
                "is_current": False,
                "evidence_block_ids": block_ids[:1],
                "detail_items": [
                    {
                        "detail_raw": (
                            "Built Python warehouse automation"
                            if label == "alpha"
                            else "Created product assets"
                        ),
                        "evidence_block_ids": block_ids[:1],
                    }
                ],
            }
        ],
        "skills": [
            {
                "fact_id": f"fact-{label}-skill",
                "skill_display": {"alpha": "Python", "beta": "Figma", "gamma": "Excel"}[
                    label
                ],
                "skill_category": {
                    "alpha": "software",
                    "beta": "design_content",
                    "gamma": "office_collaboration",
                }[label],
                "evidence_block_ids": block_ids[:1],
            }
        ],
        "language_credentials": (
            [
                {
                    "fact_id": f"fact-{label}-language",
                    "credential_code": "cet6" if label == "alpha" else "cet4",
                    "credential_name_raw": "CET-6" if label == "alpha" else "CET-4",
                    "score": "520",
                    "evidence_block_ids": block_ids[:1],
                }
            ]
            if label != "gamma"
            else []
        ),
        "scholarships": (
            [
                {
                    "fact_id": "fact-alpha-scholarship",
                    "scholarship_name_raw": "Academic Excellence",
                    "scholarship_level": "school",
                    "evidence_block_ids": block_ids[:1],
                }
            ]
            if label == "alpha"
            else []
        ),
    }


def seed_read_candidates(session, organization_id: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    for index, label in enumerate(("alpha", "beta", "gamma"), start=1):
        candidate = Candidate(
            organization_id=organization_id,
            display_name={
                "alpha": "Alice Secret",
                "beta": "Bob Private",
                "gamma": "Carol Hidden",
            }[label],
        )
        session.add(candidate)
        session.flush()
        resume = Resume(
            organization_id=organization_id,
            candidate_id=candidate.id,
            original_filename=f"private-{label}.pdf",
            storage_key=f"{organization_id}/private-{label}-{uuid4()}.pdf",
            sha256=f"{index}" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="integration-synthetic",
            is_active=True,
            is_985_211=True if label == "alpha" else False if label == "beta" else None,
            highest_degree={
                "alpha": "bachelor",
                "beta": "master",
                "gamma": "associate",
            }[label],
            employment_months={"alpha": 36, "beta": 6, "gamma": 0}[label],
            employment_or_internship_months={"alpha": 48, "beta": 12, "gamma": 0}[
                label
            ],
            facts_version=1,
            raw_text="private resume body alice@example.test 13800138000",
            contact_details=[{"kind": "email", "value": "alice@example.test"}],
        )
        session.add(resume)
        session.flush()
        main_block = f"{label}-main"
        identity_block = f"{label}-identity"
        session.add_all(
            [
                ResumeSourceBlock(
                    resume_id=resume.id,
                    block_id=main_block,
                    page_no=1,
                    block_type="paragraph",
                    text=(
                        f"姓名：{candidate.display_name}\n邮箱 alice@example.test 电话 13800138000\n"
                        f"Built {label} job evidence with measurable results."
                    ),
                ),
                ResumeSourceBlock(
                    resume_id=resume.id,
                    block_id=identity_block,
                    page_no=1,
                    block_type="paragraph",
                    text=f"姓名：{candidate.display_name}\n邮箱：alice@example.test",
                ),
                ResumeEducation(
                    resume_id=resume.id,
                    school_name_raw=f"{label.title()} University",
                    degree={
                        "alpha": "bachelor",
                        "beta": "master",
                        "gamma": "associate",
                    }[label],
                    major_raw="Computer Science" if label == "alpha" else "Design",
                    end_month={
                        "alpha": "2026-06",
                        "beta": "2024-06",
                        "gamma": "2023-06",
                    }[label],
                    institution_classification={
                        "alpha": "985",
                        "beta": "overseas",
                        "gamma": None,
                    }[label],
                    classification_basis=(
                        "registry" if label != "gamma" else None
                    ),
                    classification_registry_version=(
                        "integration-synthetic-v1" if label != "gamma" else None
                    ),
                    classification_evidence_block_ids=(
                        [main_block] if label != "gamma" else []
                    ),
                    evidence_block_ids=[main_block],
                ),
                ResumeSkill(
                    resume_id=resume.id,
                    skill_key={"alpha": "python", "beta": "figma", "gamma": "excel"}[
                        label
                    ],
                    skill_display={
                        "alpha": "Python",
                        "beta": "Figma",
                        "gamma": "Excel",
                    }[label],
                    skill_category={
                        "alpha": "software",
                        "beta": "design_content",
                        "gamma": "office_collaboration",
                    }[label],
                    evidence_block_ids=[main_block],
                ),
            ]
        )
        experience_types = {
            "alpha": [
                ("employment", None),
                ("internship", None),
                ("competition", "national"),
            ],
            "beta": [("internship", None), ("project", None), ("competition", None)],
            "gamma": [("volunteer", None)],
        }[label]
        for ordinal, (experience_type, award_level) in enumerate(experience_types):
            session.add(
                ResumeExperience(
                    resume_id=resume.id,
                    experience_type=experience_type,
                    experience_name_raw=f"{label} {experience_type}",
                    organization_name_raw=(
                        "Acme Commerce" if label == "alpha" else "Design Studio"
                    ),
                    title_raw="Automation Engineer" if label == "alpha" else "Designer",
                    start_month="2023-01",
                    end_month=f"2024-0{ordinal + 1}",
                    is_current=False,
                    award_level=award_level,
                    award_result_raw="Gold" if award_level else None,
                    evidence_block_ids=[main_block],
                )
            )
        if label != "gamma":
            session.add(
                ResumeLanguageCredential(
                    resume_id=resume.id,
                    credential_code="cet6" if label == "alpha" else "cet4",
                    credential_name_raw="CET-6" if label == "alpha" else "CET-4",
                    score=520,
                    passed=True,
                    evidence_block_ids=[main_block],
                )
            )
        if label == "alpha":
            session.add(
                ResumeScholarship(
                    resume_id=resume.id,
                    scholarship_name_raw="Academic Excellence",
                    scholarship_name_key="academicexcellence",
                    scholarship_level="school",
                    evidence_block_ids=[main_block],
                )
            )
        payload = _fact_payload(label, [main_block, identity_block])
        session.add(
            ResumeFactSnapshot(
                organization_id=organization_id,
                resume_id=resume.id,
                facts_version=1,
                canonical_facts_json=json.dumps(payload),
                facts_sha256=(f"snapshot-{index}" * 64)[:64],
                source_block_ids=[main_block, identity_block],
                created_by="integration-synthetic",
            )
        )
        ids[label] = candidate.id
    session.commit()
    return ids


@pytest.fixture()
def read_context(tmp_path):
    context = make_context(tmp_path / "integration-read")
    with context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=context.token,
            audience="rest",
            settings=context.settings,
            required_scopes=("candidates:read",),
        )
        ids = seed_read_candidates(session, context.organization_id)
        missing_snapshot = Candidate(
            organization_id=context.organization_id,
            display_name="No Current Snapshot",
        )
        session.add(missing_snapshot)
        session.flush()
        session.add(
            Resume(
                organization_id=context.organization_id,
                candidate_id=missing_snapshot.id,
                original_filename="not-exportable.pdf",
                storage_key=f"{context.organization_id}/not-exportable-{uuid4()}.pdf",
                sha256="9" * 64,
                source_page_count=1,
                parsed_page_count=1,
                extraction_status="ready",
                quality_flags=[],
                parser_version="integration-synthetic",
                is_active=True,
                facts_version=2,
            )
        )
        session.commit()
    context.ids = ids
    context.principal = principal
    return context


@pytest.mark.parametrize(
    ("payload", "labels"),
    [
        ({}, {"alpha", "beta", "gamma"}),
        ({"is_985_211": True}, {"alpha"}),
        ({"is_985_211": False}, {"beta"}),
        ({"highest_degree_in": ["bachelor"]}, {"alpha"}),
        ({"highest_degree_in": ["master"]}, {"beta"}),
        ({"education_degree_in": ["associate"]}, {"gamma"}),
        ({"institution_classifications_any_of": ["985"]}, {"alpha"}),
        ({"institution_classifications_any_of": ["overseas"]}, {"beta"}),
        ({"min_employment_months": 20}, {"alpha"}),
        ({"min_employment_or_internship_months": 10}, {"alpha", "beta"}),
        ({"experience_types_all_of": ["employment"]}, {"alpha"}),
        ({"experience_types_all_of": ["internship"]}, {"alpha", "beta"}),
        ({"experience_types_all_of": ["employment", "internship"]}, {"alpha"}),
        ({"skill_categories_any_of": ["software"]}, {"alpha"}),
        ({"skills_all_of": ["Python"]}, {"alpha"}),
        ({"skills_any_of": ["Figma"]}, {"beta"}),
        ({"language_credentials_any_of": ["cet6"]}, {"alpha"}),
        ({"scholarship_status": "present"}, {"alpha"}),
        ({"scholarship_status": "unknown"}, {"beta", "gamma"}),
        ({"competition_status": "present"}, {"alpha", "beta"}),
        ({"competition_award_status": "present"}, {"alpha"}),
        (
            {
                "condition_match_mode": "any",
                "is_985_211": True,
                "skills_all_of": ["Figma"],
            },
            {"alpha", "beta"},
        ),
    ],
)
def test_external_search_matches_approved_synthetic_queries(
    read_context, payload, labels
):
    with read_context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        result = search_candidates(
            session,
            principal=principal,
            settings=read_context.settings,
            request=IntegrationCandidateSearchRequest(**payload),
        )
    assert {item.candidate_id for item in result.items} == {
        read_context.ids[label] for label in labels
    }


def test_external_skill_filters_use_name_projected_skill_values(read_context):
    """A name accidentally embedded in skill extraction is not a search oracle."""
    private_name = "Synthetic Alice Secret"
    with read_context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        candidate = session.get(Candidate, read_context.ids["alpha"])
        resume = session.query(Resume).filter_by(
            candidate_id=read_context.ids["alpha"], is_active=True
        ).one()
        skill = session.query(ResumeSkill).filter_by(resume_id=resume.id).one()
        snapshot = session.query(ResumeFactSnapshot).filter_by(
            resume_id=resume.id, facts_version=resume.facts_version
        ).one()
        assert candidate is not None
        candidate.display_name = private_name
        skill.skill_display = f"{private_name} Python"
        skill.skill_key = "syntheticalicesecretpython"
        snapshot_payload = json.loads(snapshot.canonical_facts_json)
        snapshot_payload["skills"][0]["skill_display"] = f"{private_name} Python"
        snapshot.canonical_facts_json = json.dumps(snapshot_payload)
        session.commit()

    for payload in (
        {"skills_all_of": [f"{private_name} Python"]},
        {"skills_any_of": [private_name]},
        {
            "condition_match_mode": "any",
            "skills_all_of": [f"{private_name} Python"],
        },
    ):
        with read_context.database.session_factory() as session:
            principal = authenticate_integration_token(
                session,
                token=read_context.token,
                audience="rest",
                settings=read_context.settings,
                required_scopes=("candidates:read",),
            )
            result = search_candidates(
                session,
                principal=principal,
                settings=read_context.settings,
                request=IntegrationCandidateSearchRequest(**payload),
            )
        assert read_context.ids["alpha"] not in {
            item.candidate_id for item in result.items
        }


def test_profile_and_evidence_are_snapshot_bound_and_privacy_minimized(read_context):
    with read_context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        profile = get_candidate_profile(session, candidate_id=read_context.ids["alpha"])
        evidence = get_candidate_evidence(
            session,
            candidate_id=read_context.ids["alpha"],
            request=IntegrationCandidateEvidenceRequest(
                source_block_ids=["alpha-main", "alpha-identity"]
            ),
        )
    serialized = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
    assert profile.resume_id
    assert profile.fact_snapshot_id
    assert profile.facts_version == 1
    assert fact_reference_ids(profile) == {
        "fact-alpha-education",
        "fact-alpha-experience",
        "fact-alpha-skill",
        "fact-alpha-language",
        "fact-alpha-scholarship",
    }
    assert "Alice Secret" not in serialized
    assert "alice@example.test" not in serialized
    assert "private-alpha.pdf" not in serialized
    assert (
        evidence.excerpts[0].text == "Built alpha job evidence with measurable results."
    )
    assert evidence.excerpts[1].omitted is True
    assert evidence.excerpts[1].omission_reason == "unsafe_or_empty_after_redaction"


def test_current_summary_is_bounded_grounded_and_redacted(read_context):
    with read_context.database.session_factory() as session:
        authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read", "assessments:read"),
        )
        profile = get_candidate_profile(session, candidate_id=read_context.ids["alpha"])
        session.add(
            ResumeSummary(
                organization_id=read_context.organization_id,
                resume_id=profile.resume_id,
                fact_snapshot_id=profile.fact_snapshot_id,
                facts_version=profile.facts_version,
                content={
                    "schema_version": "resume_summary.v1",
                    "sections": {
                        "core_skills": {
                            "content": "Python automation is supported by the selected fact.",
                            "fact_ids": ["fact-alpha-skill"],
                        },
                        "candidate_positioning": {
                            "content": "Alice Secret alice@example.test",
                            "fact_ids": ["fact-alpha-skill"],
                        },
                        "strengths": {
                            "content": "Invented source must not pass.",
                            "fact_ids": ["invented-fact"],
                        },
                    },
                },
                source="ai",
                is_current=True,
                status="succeeded",
                model_name="synthetic-model-secret",
            )
        )
        session.commit()
        result = get_candidate_assessments(
            session,
            candidate_id=read_context.ids["alpha"],
        )
    assert len(result.summaries) == 1
    assert [section.key for section in result.summaries[0].sections] == [
        "core_skills"
    ], result.summaries[0].model_dump(mode="json")
    assert result.summaries[0].sections[0].fact_ids == ["fact-alpha-skill"]
    assert result.summaries[0].sections[0].evidence_source_block_ids == ["alpha-main"]
    serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "Alice Secret" not in serialized
    assert "alice@example.test" not in serialized
    assert "synthetic-model-secret" not in serialized
    assert any(
        item.startswith("candidate_positioning:")
        for item in result.summaries[0].omitted_sections
    )
    assert (
        "strengths:invalid_current_fact_references"
        in result.summaries[0].omitted_sections
    )


@pytest.mark.parametrize(
    "value", ["alice@example.test", "13800138000"]
)
def test_sensitive_free_text_filters_are_rejected(read_context, value):
    with read_context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        with pytest.raises(
            IntegrationReadError, match="integration_sensitive_filter_not_supported"
        ):
            search_candidates(
                session,
                principal=principal,
                settings=read_context.settings,
                request=IntegrationCandidateSearchRequest(keywords_all_of=[value]),
            )


def test_cursor_is_signed_and_bound_to_query_and_connection(read_context):
    with read_context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        first = search_candidates(
            session,
            principal=principal,
            settings=read_context.settings,
            request=IntegrationCandidateSearchRequest(limit=1),
        )
        assert first.next_cursor is not None
        for other_principal, request in (
            (
                principal,
                IntegrationCandidateSearchRequest(limit=2, cursor=first.next_cursor),
            ),
            (
                replace(principal, grant_id=str(uuid4())),
                IntegrationCandidateSearchRequest(limit=1, cursor=first.next_cursor),
            ),
            (
                principal,
                IntegrationCandidateSearchRequest(
                    limit=1, cursor=f"{first.next_cursor}x"
                ),
            ),
        ):
            with pytest.raises(
                IntegrationReadError, match="integration_invalid_cursor"
            ):
                search_candidates(
                    session,
                    principal=other_principal,
                    settings=read_context.settings,
                    request=request,
                )


def test_same_workspace_second_owner_sees_shared_facts_but_not_cursor_identity(
    read_context,
):
    with read_context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read",),
        )
        second_auth = AuthPrincipal(
            user=SimpleNamespace(id=str(uuid4())),
            membership=SimpleNamespace(id=str(uuid4()), role="recruiter"),
            organization=principal.auth.organization,
            plan=principal.auth.plan,
        )
        second_owner = replace(
            principal,
            auth=second_auth,
            grant_id=str(uuid4()),
            credential_id=str(uuid4()),
        )
        first = search_candidates(
            session,
            principal=principal,
            settings=read_context.settings,
            request=IntegrationCandidateSearchRequest(limit=1),
        )
        second = search_candidates(
            session,
            principal=second_owner,
            settings=read_context.settings,
            request=IntegrationCandidateSearchRequest(limit=100),
        )
        assert {item.candidate_id for item in second.items} == set(
            read_context.ids.values()
        )
        with pytest.raises(IntegrationReadError, match="integration_invalid_cursor"):
            search_candidates(
                session,
                principal=second_owner,
                settings=read_context.settings,
                request=IntegrationCandidateSearchRequest(
                    limit=1, cursor=first.next_cursor
                ),
            )


def test_cross_workspace_parent_and_nested_resources_are_not_observable(read_context):
    foreign_organization_id = str(uuid4())
    with read_context.database.session_factory() as session:
        principal = authenticate_integration_token(
            session,
            token=read_context.token,
            audience="rest",
            settings=read_context.settings,
            required_scopes=("candidates:read", "jobs:read"),
        )
        with bypass_organization_scope(session):
            session.add(
                Organization(
                    id=foreign_organization_id, name="Foreign synthetic workspace"
                )
            )
            session.flush()
            foreign_ids = seed_read_candidates(session, foreign_organization_id)
            foreign_job = Job(
                organization_id=foreign_organization_id,
                title="Foreign private job",
                jd_text="Do not expose",
                requirements={},
                version=1,
                recruiting_status="open",
            )
            session.add(foreign_job)
            session.flush()
            foreign_version = JobVersion(
                organization_id=foreign_organization_id,
                job_id=foreign_job.id,
                version=1,
                title="Foreign private job",
                raw_text="Do not expose",
                status="confirmed",
            )
            session.add(foreign_version)
            session.commit()

        for read in (
            lambda: get_candidate_profile(session, candidate_id=foreign_ids["alpha"]),
            lambda: get_candidate_evidence(
                session,
                candidate_id=foreign_ids["alpha"],
                request=IntegrationCandidateEvidenceRequest(
                    source_block_ids=["alpha-main"]
                ),
            ),
            lambda: get_job_requirements(
                session,
                job_id=foreign_job.id,
                version_id=foreign_version.id,
            ),
        ):
            with pytest.raises(
                IntegrationReadError, match="integration_resource_not_found"
            ):
                read()
        visible = search_candidates(
            session,
            principal=principal,
            settings=read_context.settings,
            request=IntegrationCandidateSearchRequest(limit=100),
        )
        assert {item.candidate_id for item in visible.items} == set(
            read_context.ids.values()
        )
