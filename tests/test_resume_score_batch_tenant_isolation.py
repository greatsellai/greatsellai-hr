from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.main import create_app
from app.models import (
    Candidate,
    Organization,
    Resume,
    ResumeFactSnapshot,
    ResumeScoreBatch,
    ResumeScoreBatchItem,
    ScoreTemplate,
    ScoreTemplateDimension,
)
from app.services import resume_score_batch_service
from app.tenant_scope import (
    bypass_organization_scope,
    clear_organization_context,
    set_organization_context,
)


@contextmanager
def _workspace(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _settings(tmp_path: Path) -> AppSettings:
    data_dir = tmp_path / "data"
    return AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="resume-score-batch-tenant-test-secret",
        deepseek_api_key="resume-score-batch-tenant-test-key",
        deepseek_model="unit-test-model",
        min_text_chars_per_page=20,
    )


@pytest.fixture
def score_batch_workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two authenticated workspaces sharing one test database."""

    app = create_app(_settings(tmp_path))
    with TestClient(app):
        client_a = TestClient(app)
        client_b = TestClient(app)
        try:
            yield client_a, client_b
        finally:
            client_a.close()
            client_b.close()


def _register_and_login(
    client: TestClient,
    *,
    organization_name: str,
    email: str,
) -> str:
    password = "resume-score-batch-tenant-password"
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": f"{organization_name} admin",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    logged_in = client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    session = client.get("/v1/auth/session")
    assert session.status_code == 200, session.text
    return str(session.json()["organization"]["organization_id"])


def _template_payload(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": "Workspace-scoped batch scoring fixture.",
        "dimensions": [
            {
                "key": "skills",
                "label": "Skills",
                "weight": 100,
                "guidance": "Use explicit resume facts only.",
            }
        ],
    }


def _create_template(client: TestClient, *, name: str) -> str:
    response = client.post("/v1/score-templates", json=_template_payload(name))
    assert response.status_code == 200, response.text
    return str(response.json()["template_id"])


def _seed_ready_resume(
    session: Session,
    *,
    organization_id: str,
    label: str,
) -> tuple[str, str]:
    """Persist a ready, trusted resume and its current immutable fact snapshot."""

    with _workspace(session, organization_id):
        candidate = Candidate(display_name=f"Candidate {label}")
        session.add(candidate)
        session.flush()
        resume = Resume(
            candidate_id=candidate.id,
            original_filename=f"{label}.pdf",
            storage_key=f"{organization_id}/{label}.pdf",
            sha256=(label * 64)[:64],
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="ready",
            quality_flags=[],
            parser_version="resume-score-batch-tenant-test",
            raw_text="Synthetic resume used only for workspace-bound scoring tests.",
            is_active=True,
            facts_version=1,
        )
        session.add(resume)
        session.flush()
        snapshot = ResumeFactSnapshot(
            resume_id=resume.id,
            facts_version=1,
            canonical_facts_json='{"schema_version":"resume_facts.v1","education":[],"experiences":[],"skills":[]}',
            facts_sha256=(f"snapshot-{label}" * 64)[:64],
            source_block_ids=[],
            created_by="tenant-test",
        )
        session.add(snapshot)
        session.flush()
        return resume.id, snapshot.id


def test_score_batch_api_hides_foreign_batches_and_enqueues_only_current_workspace_resumes(
    score_batch_workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = score_batch_workspace_clients
    organization_a = _register_and_login(
        client_a,
        organization_name="Score Batch Alpha",
        email="score-batch-alpha@example.test",
    )
    organization_b = _register_and_login(
        client_b,
        organization_name="Score Batch Beta",
        email="score-batch-beta@example.test",
    )

    database = client_a.app.state.database
    with database.session_factory() as session:
        resume_a_id, _ = _seed_ready_resume(
            session,
            organization_id=organization_a,
            label="alpha-ready",
        )
        resume_b_id, _ = _seed_ready_resume(
            session,
            organization_id=organization_b,
            label="beta-ready",
        )
        session.commit()

    template_a_id = _create_template(client_a, name="Alpha score template")
    template_b_id = _create_template(client_b, name="Beta score template")

    # B establishes a real private batch, including a private item, before A
    # attempts direct-ID reads.  IDs must not act as a capability.
    batch_b = client_b.post(f"/v1/score-templates/{template_b_id}/score-all")
    assert batch_b.status_code == 200, batch_b.text
    batch_b_id = batch_b.json()["batch_id"]
    assert batch_b.json()["total_count"] == 1

    visible_b = client_b.get(f"/v1/resume-score-batches/{batch_b_id}")
    visible_b_items = client_b.get(f"/v1/resume-score-batches/{batch_b_id}/items")
    assert visible_b.status_code == 200, visible_b.text
    assert visible_b_items.status_code == 200, visible_b_items.text
    assert {item["resume_id"] for item in visible_b_items.json()} == {resume_b_id}

    foreign_batch = client_a.get(f"/v1/resume-score-batches/{batch_b_id}")
    foreign_items = client_a.get(f"/v1/resume-score-batches/{batch_b_id}/items")
    assert foreign_batch.status_code == 404, foreign_batch.text
    assert foreign_items.status_code == 404, foreign_items.text

    # A cannot turn B's template ID into a batch either.
    foreign_enqueue = client_a.post(f"/v1/score-templates/{template_b_id}/score-all")
    assert foreign_enqueue.status_code == 404, foreign_enqueue.text

    # The all-resume query is scoped to the authenticated workspace.  B's
    # ready resume exists in the same physical database but is never admitted
    # to A's batch.
    batch_a = client_a.post(f"/v1/score-templates/{template_a_id}/score-all")
    assert batch_a.status_code == 200, batch_a.text
    batch_a_id = batch_a.json()["batch_id"]
    assert batch_a.json()["total_count"] == 1
    batch_a_items = client_a.get(f"/v1/resume-score-batches/{batch_a_id}/items")
    assert batch_a_items.status_code == 200, batch_a_items.text
    assert {item["resume_id"] for item in batch_a_items.json()} == {resume_a_id}
    assert resume_b_id not in {item["resume_id"] for item in batch_a_items.json()}


def test_score_batch_worker_never_scores_a_foreign_workspace_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed cross-workspace item must fail before any model call."""

    settings = _settings(tmp_path)
    database = Database("sqlite://")
    database.create_all()
    now = datetime.now(timezone.utc)
    try:
        with database.session_factory() as session:
            organization_a = Organization(name="Worker score batch Alpha")
            organization_b = Organization(name="Worker score batch Beta")
            session.add_all((organization_a, organization_b))
            session.flush()
            organization_a_id = organization_a.id
            organization_b_id = organization_b.id

            resume_b_id, snapshot_b_id = _seed_ready_resume(
                session,
                organization_id=organization_b_id,
                label="worker-beta-ready",
            )
            with _workspace(session, organization_b_id):
                template_b = ScoreTemplate(name="Worker beta template", version=1)
                session.add(template_b)
                session.flush()
                session.add(
                    ScoreTemplateDimension(
                        template_id=template_b.id,
                        key="skills",
                        label="Skills",
                        weight=100,
                        guidance=None,
                        sort_order=0,
                    )
                )
                batch_b = ResumeScoreBatch(
                    template_id=template_b.id,
                    template_version=1,
                    status="queued",
                    total_count=1,
                    max_attempts=1,
                    requested_at=now,
                )
                session.add(batch_b)
                session.flush()
                untouched_b_item = ResumeScoreBatchItem(
                    batch_id=batch_b.id,
                    resume_id=resume_b_id,
                    fact_snapshot_id=snapshot_b_id,
                    facts_version=1,
                    status="queued",
                    next_attempt_at=now,
                )
                session.add(untouched_b_item)
                session.flush()

            with _workspace(session, organization_a_id):
                template_a = ScoreTemplate(name="Worker alpha template", version=1)
                session.add(template_a)
                session.flush()
                session.add(
                    ScoreTemplateDimension(
                        template_id=template_a.id,
                        key="skills",
                        label="Skills",
                        weight=100,
                        guidance=None,
                        sort_order=0,
                    )
                )
                batch_a = ResumeScoreBatch(
                    template_id=template_a.id,
                    template_version=1,
                    status="queued",
                    total_count=1,
                    max_attempts=1,
                    requested_at=now - timedelta(seconds=10),
                )
                session.add(batch_a)
                session.flush()
                # Deliberately bypasses application-level invariants at the
                # database edge: A's batch points at B's resume/snapshot.
                # The worker must refuse it after it claims the global queue.
                cross_workspace_item = ResumeScoreBatchItem(
                    batch_id=batch_a.id,
                    resume_id=resume_b_id,
                    fact_snapshot_id=snapshot_b_id,
                    facts_version=1,
                    status="queued",
                    next_attempt_at=now - timedelta(seconds=10),
                )
                session.add(cross_workspace_item)
                session.flush()
                cross_item_id = cross_workspace_item.id
            untouched_b_item_id = untouched_b_item.id
            session.commit()

        def _score_must_not_run(**_: object) -> object:
            raise AssertionError("a foreign workspace resume reached scoring")

        monkeypatch.setattr(resume_score_batch_service, "run_resume_score", _score_must_not_run)

        assert resume_score_batch_service.run_resume_score_batch_worker_once(
            database,
            settings=settings,
            worker_id="tenant-isolation-test-worker",
        )

        with database.session_factory() as session:
            with bypass_organization_scope(session):
                failed = session.get(ResumeScoreBatchItem, cross_item_id)
                untouched = session.get(ResumeScoreBatchItem, untouched_b_item_id)
            assert failed is not None
            assert failed.organization_id == organization_a_id
            assert failed.status == "failed"
            assert failed.last_error == "resume_no_longer_ready_for_scoring"
            assert failed.attempt_count == 1
            assert untouched is not None
            assert untouched.organization_id == organization_b_id
            assert untouched.status == "queued"
            assert untouched.attempt_count == 0
    finally:
        database.dispose()
