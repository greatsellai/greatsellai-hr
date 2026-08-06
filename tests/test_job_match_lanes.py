from __future__ import annotations

import pytest

from app.schemas import JobRequirements
from app.services import job_service
from test_filter_mvp_contract import _save_ready_resume
from test_job_service import _create_job


def _provider_result_for_statuses(statuses: list[str]):
    """Build a stable provider double for the three confirmed JD requirements."""

    def fake_provider(**kwargs: object) -> dict[str, object]:
        requirements = kwargs["confirmed_requirements"]
        assert isinstance(requirements, list)
        assert len(requirements) == len(statuses)
        requirement_matches: list[dict[str, object]] = []
        for requirement, outcome in zip(requirements, statuses, strict=True):
            assert isinstance(requirement, dict)
            if outcome == "unknown":
                fact_ids: list[str] = []
                uncertainties = ["No explicit supporting fact is available."]
            elif outcome == "partial":
                fact_ids = ["skill-001"]
                uncertainties = ["Evidence is incomplete."]
            else:
                fact_ids = ["skill-001"]
                uncertainties = []
            requirement_matches.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "status": outcome,
                    "rationale": f"Test outcome: {outcome}.",
                    "fact_ids": fact_ids,
                    "uncertainties": uncertainties,
                }
            )
        return {
            "schema_version": "jd_match.v1",
            "requirement_matches": requirement_matches,
            "needs_human_review": False,
        }

    return fake_provider


@pytest.mark.parametrize(
    ("total_score", "evidence_coverage", "expected_score"),
    [
        # Unknown requirements contribute zero to the legacy score, but are
        # intentionally excluded from the normalized candidate match score.
        (50.0, 65.0, 76.92),
        (70.0, 70.0, 100.0),
        (0.0, 0.0, 0.0),
        (100.0, None, 0.0),
    ],
)
def test_derive_job_match_score_keeps_unknown_separate_from_fit(
    total_score: float,
    evidence_coverage: float | None,
    expected_score: float,
) -> None:
    assert job_service.derive_job_match_score(
        total_score=total_score,
        evidence_coverage=evidence_coverage,
    ) == expected_score


@pytest.mark.parametrize(
    ("hard_requirement_status", "expected_lane"),
    [
        ("unmet", "unmet"),
        ("pass", "recommended"),
        ("not_applicable", "recommended"),
        # A partially evidenced hard requirement is a lead for recruiter
        # review, not a recommendation. This is especially important for
        # talent-profile searches such as "LangChain project experience".
        ("partial", "pending"),
        # Legacy rows produced before "没写就当作不会" stay reviewable instead
        # of being silently re-labeled as rejected.
        ("information_insufficient", "pending"),
        (None, "pending"),
    ],
)
def test_classify_job_match_lane_preserves_review_and_rejection_boundaries(
    hard_requirement_status: str | None,
    expected_lane: str,
) -> None:
    assert job_service.classify_job_match_lane(
        hard_requirement_status=hard_requirement_status,
    ) == expected_lane


def test_job_match_api_treats_missing_must_have_as_unmet(
    ai_client,
    monkeypatch,
) -> None:
    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "\u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Python SQL"
        ),
    )
    job = _create_job(
        ai_client,
        requirements=JobRequirements(
            must_have=["Python experience", "Go experience"],
            preferred=["Kubernetes experience"],
        ),
    )
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _provider_result_for_statuses(["met", "unknown", "partial"]),
    )

    response = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": job["job_version_id"]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    # A must-have the resume never mentions is scored as not met (\u201c\u6ca1\u5199\u5c31\u5f53\u4f5c
    # \u4e0d\u4f1a\u201d), so the match degree is the raw weighted total \u2014 never normalized
    # up by evidence coverage. The missing Go requirement also moves the lane
    # to a rejection instead of a review.
    assert payload["total_score"] == 50.0
    assert payload["match_score"] == 50.0
    assert payload["evidence_coverage"] == 65.0
    assert payload["match_confidence"] == 65.0
    assert payload["hard_requirement_status"] == "unmet"
    assert payload["must_have_passed"] is False
    assert payload["status"] == "needs_review"
    assert payload["match_lane"] == "unmet"


def test_partial_must_have_stays_in_pending_review_lane(
    ai_client,
    monkeypatch,
) -> None:
    """Partial proof of a mandatory requirement must never look recommended."""

    _, resume_id = _save_ready_resume(
        ai_client,
        source_text=(
            "Education \u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
            "Acme Python Engineer Python SQL"
        ),
    )
    job = _create_job(
        ai_client,
        requirements=JobRequirements(must_have=["Python experience"]),
    )
    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        _provider_result_for_statuses(["partial"]),
    )

    response = ai_client.post(
        f"/v1/resumes/{resume_id}/job-matches",
        json={"job_version_id": job["job_version_id"]},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["hard_requirement_status"] == "partial"
    assert payload["must_have_passed"] is None
    assert payload["status"] == "needs_review"
    assert payload["match_lane"] == "pending"


def test_job_version_match_list_orders_the_three_lanes_and_their_rankings(
    ai_client,
    monkeypatch,
) -> None:
    resume_ids = [
        _save_ready_resume(
            ai_client,
            source_text=(
                "\u6e05\u534e\u5927\u5b66 \u8ba1\u7b97\u673a \u5de5\u4f5c\u7ecf\u5386 "
                f"Acme Python Engineer Python SQL evidence {index}."
            ),
        )[1]
        for index in range(4)
    ]
    job = _create_job(
        ai_client,
        requirements=JobRequirements(
            must_have=["Python experience", "Go experience"],
            preferred=["Kubernetes experience"],
        ),
    )
    status_sets = iter(
        [
            # Recommended: full evidence; should lead its lane.
            ["met", "met", "met"],
            # Recommended: preferred information is absent; same lane, lower
            # match degree, so it follows the first recommended record.
            ["met", "met", "unknown"],
            # Pending: a partially evidenced must-have stays reviewable.
            ["met", "partial", "met"],
            # Unmet: an explicitly failed must-have always belongs last.
            ["met", "not_met", "met"],
        ]
    )

    def sequential_provider(**kwargs: object) -> dict[str, object]:
        return _provider_result_for_statuses(next(status_sets))(**kwargs)

    monkeypatch.setattr(
        job_service,
        "match_resume_fact_snapshot_against_requirements",
        sequential_provider,
    )
    created_match_ids: list[str] = []
    for resume_id in resume_ids:
        response = ai_client.post(
            f"/v1/resumes/{resume_id}/job-matches",
            json={"job_version_id": job["job_version_id"]},
        )
        assert response.status_code == 200, response.text
        created_match_ids.append(response.json()["match_id"])

    response = ai_client.get(f"/v1/job-versions/{job['job_version_id']}/matches")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["match_id"] for item in payload] == created_match_ids
    assert [item["match_lane"] for item in payload] == [
        "recommended",
        "recommended",
        "pending",
        "unmet",
    ]
    assert [item["match_score"] for item in payload] == [100.0, 70.0, 82.5, 65.0]
    assert [item["match_confidence"] for item in payload] == [100.0, 70.0, 100.0, 100.0]
