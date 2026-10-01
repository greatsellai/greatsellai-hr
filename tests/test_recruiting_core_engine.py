"""Contract coverage for the human-controlled recruiting core.

The tests use only opaque IDs and synthetic records. They verify that a
candidate×job application is separate from a favorite or an AI conclusion,
and that its snapshots and manual stage history remain workspace-private.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from app.models import (
    CandidateDataDeletionBatch,
    CandidateDataPurgeJob,
    JobApplication,
    JobApplicationStageTransition,
    utcnow,
)
from app.services.candidate_data_purge_service import run_candidate_data_purge_worker_once
from test_filter_mvp_contract import _education, _employment, _facts, _save_ready_resume
from test_resume_flow import replace_page_evidence, upload_text_resume
from test_tenant_isolation import _register_and_login, workspace_clients


def _create_confirmed_job(client: TestClient, *, title: str = "Platform engineer") -> dict[str, object]:
    response = client.post(
        "/v1/jobs",
        json={
            "title": title,
            "jd_text": "Python experience is required.",
            "requirements": {"must_have": ["Python experience"], "preferred": []},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ready_candidate(client: TestClient) -> tuple[str, str]:
    return _save_ready_resume(
        client,
        source_text=(
            "教育经历 清华大学 计算机 本科。"
            "工作经历 Acme Python Engineer。"
            "技能 Python SQL。"
        ),
    )


def _ready_resume_for_candidate(client: TestClient, *, candidate_id: str) -> str:
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "Education Example University Computer Science Bachelor. "
        "Professional Experience Example Company Python Engineer. Skills Python SQL.",
    )
    saved = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json={
            "facts": _facts(
                educations=[_education("Example University", "bachelor", "Computer Science")],
                experiences=[_employment("Example Company", "Python Engineer")],
            )["facts"],
            "complete_review": True,
            "review_note": "Synthetic regression review.",
            "is_985_211_override": False,
        },
    )
    assert saved.status_code == 200, saved.text
    return resume_id


def _create_application(
    client: TestClient,
    *,
    job_id: str,
    candidate_id: str,
) -> dict[str, object]:
    response = client.post(
        f"/v1/recruiting/jobs/{job_id}/applications",
        json={"candidate_id": candidate_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_application_pins_snapshots_and_records_only_manual_stage_changes(client: TestClient) -> None:
    candidate_id, resume_id = _ready_candidate(client)
    job = _create_confirmed_job(client)
    application = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )

    assert application["resume_id"] == resume_id
    assert application["job_version_id"] == job["job_version_id"]
    assert application["status"] == "active"
    assert application["current_stage_key"] == "pending_screen"
    assert application["current_stage_sort_order"] == 10
    assert application["round_number"] == 1
    assert application["workflow_name"] == "默认招聘流程"

    # A later JD revision changes the Job cache, never the application
    # snapshot a recruiter used when adding this candidate.
    revised = client.post(
        f"/v1/jobs/{job['job_id']}/versions",
        json={
            "title": "Platform engineer revised",
            "jd_text": "Python and SQL experience are required.",
            "requirements": {"must_have": ["Python"], "preferred": ["SQL"]},
        },
    )
    assert revised.status_code == 200, revised.text

    detail = client.get(f"/v1/recruiting/applications/{application['application_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["job_title"] == "Platform engineer"
    assert [item["action"] for item in detail.json()["stage_transitions"]] == ["initial"]

    advanced = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/advance",
        json={"expected_state_version": 1, "note": "recruiter reviewed"},
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["current_stage_key"] == "initial_screen"
    assert advanced.json()["state_version"] == 2
    assert [item["action"] for item in advanced.json()["stage_transitions"]] == [
        "initial",
        "advance",
    ]

    returned = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/return",
        json={"expected_state_version": 2},
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["current_stage_key"] == "pending_screen"
    assert returned.json()["state_version"] == 3

    rejected = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/reject",
        json={"expected_state_version": 3},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["current_stage_key"] == "rejected"

    # A new recruiting round keeps the first round/history instead of
    # overwriting it. A favorite cannot satisfy this invariant.
    second_round = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )
    assert second_round["round_number"] == 2
    assert second_round["resume_fact_snapshot_id"] == application["resume_fact_snapshot_id"]
    assert second_round["job_version_id"] == revised.json()["job_version_id"]
    assert second_round["job_version_id"] != application["job_version_id"]
    history = client.get(
        f"/v1/recruiting/jobs/{job['job_id']}/applications?include_history=true"
    )
    assert history.status_code == 200, history.text
    assert [item["round_number"] for item in history.json()["items"]] == [2, 1]


def test_workflow_versions_only_change_future_applications(client: TestClient) -> None:
    first_candidate_id, _ = _ready_candidate(client)
    second_candidate_id, _ = _ready_candidate(client)
    job = _create_confirmed_job(client)
    original = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=first_candidate_id,
    )

    custom = client.post(
        "/v1/recruiting/workflows",
        json={
            "name": "Lean process",
            "stages": [
                {"stage_key": "review", "name": "Review", "stage_type": "active", "sort_order": 10},
                {"stage_key": "offer", "name": "Offer", "stage_type": "active", "sort_order": 20},
                {"stage_key": "hired", "name": "Hired", "stage_type": "hired", "sort_order": 90},
                {"stage_key": "rejected", "name": "Rejected", "stage_type": "rejected", "sort_order": 100},
            ],
        },
    )
    assert custom.status_code == 200, custom.text
    custom_payload = custom.json()
    custom_version = custom_payload["versions"][0]
    assert custom_version["status"] == "published"

    update = client.patch(
        f"/v1/recruiting/jobs/{job['job_id']}",
        json={"recruiting_workflow_version_id": custom_version["workflow_version_id"]},
    )
    assert update.status_code == 200, update.text

    later = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=second_candidate_id,
    )
    assert original["workflow_version_id"] != later["workflow_version_id"]
    assert original["current_stage_key"] == "pending_screen"
    assert later["current_stage_key"] == "review"

    next_version = client.post(
        f"/v1/recruiting/workflows/{custom_payload['workflow_id']}/versions",
        json={
            "stages": [
                {"stage_key": "review", "name": "Review", "stage_type": "active", "sort_order": 10},
                {"stage_key": "panel", "name": "Panel", "stage_type": "active", "sort_order": 20},
                {"stage_key": "offer", "name": "Offer", "stage_type": "active", "sort_order": 30},
                {"stage_key": "hired", "name": "Hired", "stage_type": "hired", "sort_order": 90},
                {"stage_key": "rejected", "name": "Rejected", "stage_type": "rejected", "sort_order": 100},
            ],
        },
    )
    assert next_version.status_code == 200, next_version.text
    assert next_version.json()["status"] == "draft"
    published = client.post(
        f"/v1/recruiting/workflow-versions/{next_version.json()['workflow_version_id']}/publish"
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    update = client.patch(
        f"/v1/recruiting/jobs/{job['job_id']}",
        json={"recruiting_workflow_version_id": published.json()["workflow_version_id"]},
    )
    assert update.status_code == 200, update.text
    third_candidate_id, _ = _ready_candidate(client)
    newest = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=third_candidate_id,
    )
    assert newest["workflow_version_id"] == published.json()["workflow_version_id"]
    assert newest["current_stage_key"] == "review"
    # The v1 application retains v1, even after a new version is published.
    assert later["workflow_version_id"] != newest["workflow_version_id"]


def test_job_status_and_terminal_actions_are_human_controlled(client: TestClient) -> None:
    candidate_id, _ = _ready_candidate(client)
    second_candidate_id, _ = _ready_candidate(client)
    job = _create_confirmed_job(client)
    application = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )

    # A direct terminal action cannot skip the recruiter-controlled stages.
    premature_hire = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/hire",
        json={"expected_state_version": 1},
    )
    assert premature_hire.status_code == 409, premature_hire.text
    assert premature_hire.json()["detail"] == "job_application_hire_requires_final_active_stage"

    paused = client.patch(
        f"/v1/recruiting/jobs/{job['job_id']}",
        json={"recruiting_status": "paused"},
    )
    assert paused.status_code == 200, paused.text
    blocked_add = client.post(
        f"/v1/recruiting/jobs/{job['job_id']}/applications",
        json={"candidate_id": second_candidate_id},
    )
    blocked_move = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/advance",
        json={"expected_state_version": 1},
    )
    assert blocked_add.status_code == blocked_move.status_code == 409
    assert blocked_add.json()["detail"] == blocked_move.json()["detail"] == "recruiting_job_not_open"

    reopened = client.patch(
        f"/v1/recruiting/jobs/{job['job_id']}",
        json={"recruiting_status": "open"},
    )
    assert reopened.status_code == 200, reopened.text
    current_state_version = 1
    for _ in range(4):
        advanced = client.post(
            f"/v1/recruiting/applications/{application['application_id']}/advance",
            json={"expected_state_version": current_state_version},
        )
        assert advanced.status_code == 200, advanced.text
        current_state_version += 1
    hired = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/hire",
        json={"expected_state_version": current_state_version, "note": "manual decision"},
    )
    assert hired.status_code == 200, hired.text
    assert hired.json()["status"] == "hired"
    assert hired.json()["current_stage_key"] == "hired"
    assert hired.json()["stage_transitions"][-1]["action"] == "hire"


def test_unconfirmed_jd_is_created_as_a_recruiting_draft(client: TestClient) -> None:
    created = client.post(
        "/v1/jobs",
        json={
            "title": "Draft role",
            "jd_text": "Requirements still being prepared.",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "draft"

    recruiting_job = client.get(f"/v1/recruiting/jobs/{created.json()['job_id']}")
    assert recruiting_job.status_code == 200, recruiting_job.text
    assert recruiting_job.json()["recruiting_status"] == "draft"


def test_stale_stage_transition_is_rejected_without_silent_overwrite(client: TestClient) -> None:
    candidate_id, _ = _ready_candidate(client)
    job = _create_confirmed_job(client)
    application = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )

    first = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/advance",
        json={"expected_state_version": 1},
    )
    assert first.status_code == 200, first.text
    stale = client.post(
        f"/v1/recruiting/applications/{application['application_id']}/advance",
        json={"expected_state_version": 1},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == "job_application_state_version_conflict"

    detail = client.get(f"/v1/recruiting/applications/{application['application_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["state_version"] == 2
    assert detail.json()["current_stage_key"] == "initial_screen"
    assert [item["action"] for item in detail.json()["stage_transitions"]] == [
        "initial",
        "advance",
    ]


def test_deleted_candidate_hides_then_purges_application_history(client: TestClient) -> None:
    candidate_id, _ = _ready_candidate(client)
    job = _create_confirmed_job(client)
    application = _create_application(
        client,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )

    deleted = client.request(
        "DELETE",
        f"/v1/candidates/{candidate_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    assert client.get(
        f"/v1/recruiting/jobs/{job['job_id']}/applications"
    ).json()["items"] == []
    hidden = client.get(f"/v1/recruiting/applications/{application['application_id']}")
    assert hidden.status_code == 404, hidden.text

    deletion_batch_id = deleted.json()["deletion_batch_id"]
    database = client.app.state.database
    due = utcnow() - timedelta(seconds=1)
    with database.session_factory() as session:
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        assert batch is not None
        purge_job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert purge_job is not None
        batch.purge_after_at = due
        batch.recovery_deadline_at = due
        purge_job.next_attempt_at = due
        session.commit()

    assert run_candidate_data_purge_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="recruiting-purge-test",
    )
    with database.session_factory() as session:
        assert session.scalar(
            select(JobApplication)
            .where(JobApplication.id == application["application_id"])
            .execution_options(include_deleted_candidate_data=True)
        ) is None
        assert session.scalar(
            select(JobApplicationStageTransition.id).where(
                JobApplicationStageTransition.application_id == application["application_id"]
            )
        ) is None


def test_single_resume_purge_keeps_applications_on_other_live_versions(client: TestClient) -> None:
    candidate_id, first_resume_id = _ready_candidate(client)
    first_job = _create_confirmed_job(client, title="First application")
    first_application = _create_application(
        client,
        job_id=str(first_job["job_id"]),
        candidate_id=candidate_id,
    )
    second_resume_id = _ready_resume_for_candidate(client, candidate_id=candidate_id)
    second_job = _create_confirmed_job(client, title="Second application")
    second_application = _create_application(
        client,
        job_id=str(second_job["job_id"]),
        candidate_id=candidate_id,
    )
    assert first_application["resume_id"] == first_resume_id
    assert second_application["resume_id"] == second_resume_id

    deleted = client.request(
        "DELETE",
        f"/v1/resumes/{first_resume_id}",
        json={"reason": "duplicate"},
    )
    assert deleted.status_code == 202, deleted.text
    deletion_batch_id = deleted.json()["deletion_batch_id"]
    database = client.app.state.database
    due = utcnow() - timedelta(seconds=1)
    with database.session_factory() as session:
        batch = session.scalar(
            select(CandidateDataDeletionBatch).where(
                CandidateDataDeletionBatch.id == deletion_batch_id
            )
        )
        purge_job = session.scalar(
            select(CandidateDataPurgeJob).where(
                CandidateDataPurgeJob.deletion_batch_id == deletion_batch_id
            )
        )
        assert batch is not None and purge_job is not None
        batch.purge_after_at = due
        batch.recovery_deadline_at = due
        purge_job.next_attempt_at = due
        session.commit()

    assert run_candidate_data_purge_worker_once(
        database,
        settings=client.app.state.settings,
        worker_id="recruiting-single-resume-purge-test",
    )
    with database.session_factory() as session:
        assert session.scalar(
            select(JobApplication)
            .where(JobApplication.id == first_application["application_id"])
            .execution_options(include_deleted_candidate_data=True)
        ) is None
        assert session.scalar(
            select(JobApplication)
            .where(JobApplication.id == second_application["application_id"])
            .execution_options(include_deleted_candidate_data=True)
        ) is not None
        assert session.scalar(
            select(JobApplicationStageTransition.id).where(
                JobApplicationStageTransition.application_id == second_application["application_id"]
            )
        ) is not None


def test_cross_workspace_job_and_application_ids_are_not_found(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    _register_and_login(
        client_a,
        organization_name="Recruiting Alpha",
        full_name="Alpha recruiter",
        email="recruiting-alpha@example.test",
        password="tenant-test-password-a",
    )
    _register_and_login(
        client_b,
        organization_name="Recruiting Beta",
        full_name="Beta recruiter",
        email="recruiting-beta@example.test",
        password="tenant-test-password-b",
    )
    candidate_id, _ = _ready_candidate(client_b)
    job = _create_confirmed_job(client_b, title="Private role")
    application = _create_application(
        client_b,
        job_id=str(job["job_id"]),
        candidate_id=candidate_id,
    )

    missing_job = client_a.get("/v1/recruiting/jobs/not-a-real-job")
    foreign_job = client_a.get(f"/v1/recruiting/jobs/{job['job_id']}")
    foreign_application = client_a.get(
        f"/v1/recruiting/applications/{application['application_id']}"
    )
    foreign_add = client_a.post(
        f"/v1/recruiting/jobs/{job['job_id']}/applications",
        json={"candidate_id": candidate_id},
    )
    foreign_patch = client_a.patch(
        f"/v1/recruiting/jobs/{job['job_id']}",
        json={"hc_total": 2},
    )
    foreign_candidate_history = client_a.get(
        f"/v1/recruiting/candidates/{candidate_id}/applications"
    )
    workflow = client_b.get("/v1/recruiting/workflows")
    assert workflow.status_code == 200, workflow.text
    workflow_payload = workflow.json()[0]
    foreign_workflow_version = client_a.post(
        f"/v1/recruiting/workflows/{workflow_payload['workflow_id']}/versions",
        json={
            "stages": [
                {"stage_key": "screen", "name": "Screen", "stage_type": "active", "sort_order": 10},
                {"stage_key": "hired", "name": "Hired", "stage_type": "hired", "sort_order": 90},
                {"stage_key": "rejected", "name": "Rejected", "stage_type": "rejected", "sort_order": 100},
            ]
        },
    )
    foreign_publish = client_a.post(
        f"/v1/recruiting/workflow-versions/{workflow_payload['versions'][0]['workflow_version_id']}/publish"
    )
    foreign_transitions = [
        client_a.post(
            f"/v1/recruiting/applications/{application['application_id']}/{action}",
            json={"expected_state_version": 1},
        )
        for action in ("advance", "return", "reject", "hire")
    ]
    for response in (
        foreign_job,
        foreign_application,
        foreign_add,
        foreign_patch,
        foreign_candidate_history,
        foreign_workflow_version,
        foreign_publish,
        *foreign_transitions,
    ):
        assert response.status_code == missing_job.status_code == 404, response.text


def test_migration_backfills_existing_jobs_with_a_published_default_workflow(
    tmp_path,
) -> None:
    """Legacy jobs must receive a process version before recruiters use them."""

    database_path = tmp_path / "recruiting-core-engine.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])
    command.upgrade(config, "20260729_0049")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        jobs = Table("jobs", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                organizations.insert(),
                {
                    "id": "recruiting-migration-organization",
                    "name": "Recruiting migration workspace",
                    "plan_status": "trial",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                jobs.insert(),
                {
                    "id": "recruiting-migration-job",
                    "organization_id": "recruiting-migration-organization",
                    "kind": "job",
                    "title": "Existing role",
                    "jd_text": "Existing role requirement",
                    "requirements": {},
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )

        command.upgrade(config, "20260806_0063")
        metadata = MetaData()
        jobs = Table("jobs", metadata, autoload_with=engine)
        workflow_versions = Table(
            "recruiting_workflow_versions", metadata, autoload_with=engine
        )
        stages = Table("recruiting_workflow_stages", metadata, autoload_with=engine)
        with engine.connect() as connection:
            job = connection.execute(
                select(
                    jobs.c.recruiting_status,
                    jobs.c.recruiting_workflow_version_id,
                ).where(jobs.c.id == "recruiting-migration-job")
            ).mappings().one()
            assert job["recruiting_status"] == "open"
            assert job["recruiting_workflow_version_id"] is not None
            workflow_version = connection.execute(
                select(
                    workflow_versions.c.status,
                    workflow_versions.c.version,
                ).where(
                    workflow_versions.c.id == job["recruiting_workflow_version_id"]
                )
            ).mappings().one()
            stage_keys = connection.execute(
                select(stages.c.stage_key)
                .where(
                    stages.c.workflow_version_id
                    == job["recruiting_workflow_version_id"]
                )
                .order_by(stages.c.sort_order)
            ).scalars().all()
        assert workflow_version == {"status": "published", "version": 1}
        assert stage_keys == [
            "pending_screen",
            "initial_screen",
            "interview",
            "final_interview",
            "offer",
            "hired",
            "rejected",
        ]

        # Downgrade is intentionally schema-only: release rollback keeps the
        # former Job row while removing this feature's dependent records and
        # columns. Use a new engine after disposal so SQLite cannot retain a
        # schema lock from the upgrade assertions.
        engine.dispose()
        command.downgrade(config, "20260729_0049")
        downgraded_engine = create_engine(database_url)
        try:
            tables = set(inspect(downgraded_engine).get_table_names())
            downgraded_jobs = Table("jobs", MetaData(), autoload_with=downgraded_engine)
            assert "recruiting_workflows" not in tables
            assert "job_applications" not in tables
            assert "recruiting_status" not in downgraded_jobs.c
            assert "recruiting_workflow_version_id" not in downgraded_jobs.c
            with downgraded_engine.connect() as connection:
                assert connection.execute(
                    select(downgraded_jobs.c.id).where(
                        downgraded_jobs.c.id == "recruiting-migration-job"
                    )
                ).scalar_one() == "recruiting-migration-job"
        finally:
            downgraded_engine.dispose()
    finally:
        engine.dispose()
