from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app import ai_extraction_worker
from app.ai_extraction_worker import _log_worker_lifecycle_event
from app.main import create_app
from app.models import RuntimeWorkerHeartbeat, WorkspaceFeedbackSubmission
from app.services.identity_service import LEGACY_ORGANIZATION_ID, LEGACY_USER_ID
from app.services.platform_admin_service import get_platform_runtime_overview
from app.services.runtime_observability_service import (
    RuntimeReadinessError,
    mark_worker_stopped,
    record_worker_heartbeat,
)
from app.tenant_scope import set_organization_context


@pytest.fixture
def runtime_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        admin_token="runtime-observability-platform-token",
        legacy_admin_token_enabled=True,
        session_secret="runtime-observability-session-secret",
        allow_unauthenticated=False,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _register_and_verify(client: TestClient) -> None:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Runtime Overview Tenant",
            "full_name": "Runtime Overview Member",
            "email": "runtime-overview@example.test",
            "password": "runtime-overview-member-password",
        },
    )
    assert registered.status_code == 201, registered.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text


def _login_platform_admin(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/login",
        json={"password": "runtime-observability-platform-token"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_platform_admin"] is True


def _feedback_with_unsafe_error(
    client: TestClient,
    *,
    error: str,
) -> None:
    """Create a retrying durable job whose raw error must never reach HTTP."""

    with client.app.state.database.session_factory() as session:
        set_organization_context(session, LEGACY_ORGANIZATION_ID)
        session.add(
            WorkspaceFeedbackSubmission(
                submitted_by_user_id=LEGACY_USER_ID,
                idempotency_key_hash="a" * 64,
                request_fingerprint="b" * 64,
                use_case="synthetic observability test",
                intended_outcome="synthetic observability test",
                friction="synthetic observability test",
                desired_change="synthetic observability test",
                reward_status="queued",
                reward_due_at=datetime.now(timezone.utc),
                reward_attempt_count=2,
                reward_last_error=error,
            )
        )
        session.commit()


def test_readyz_is_database_backed_and_hides_database_details(
    runtime_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = runtime_client.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json() == {"status": "ready"}

    def fail_readiness(*_args: object, **_kwargs: object) -> None:
        raise RuntimeReadinessError("private database exception must not leak")

    monkeypatch.setattr("app.main.check_database_ready", fail_readiness)
    unavailable = runtime_client.get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "database_unavailable"}
    assert "private database" not in unavailable.text


def test_worker_lifecycle_logging_uses_only_fixed_safe_event_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, int, dict[str, object]]] = []
    configured: list[bool] = []
    observability = ModuleType("app.observability")

    def capture(event: str, *, level: int = logging.INFO, **fields: object) -> None:
        captured.append((event, level, fields))

    def configure() -> None:
        configured.append(True)

    observability.configure_observability_logging = configure  # type: ignore[attr-defined]
    observability.log_event = capture  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.observability", observability)

    _log_worker_lifecycle_event("worker_started")
    _log_worker_lifecycle_event("worker_cycle_completed")
    _log_worker_lifecycle_event("worker_cycle_failed")
    _log_worker_lifecycle_event("untrusted_event_name")

    assert configured == [True, True, True]
    assert captured == [
        ("worker_started", logging.INFO, {}),
        ("worker_cycle_completed", logging.INFO, {}),
        (
            "worker_cycle_failed",
            logging.ERROR,
            {"error_code": "worker_cycle_failed"},
        ),
    ]


def test_worker_task_boundary_uses_the_rate_limited_liveness_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long queue tasks refresh liveness without bypassing the rate limit."""

    calls: list[dict[str, object]] = []

    def record_boundary(*args: object, **kwargs: object) -> tuple[float, bool]:
        calls.append({"args": args, **kwargs})
        return 123.0, True

    monkeypatch.setattr(ai_extraction_worker, "_touch_worker_heartbeat", record_boundary)
    database = object()

    updated = ai_extraction_worker._touch_worker_task_boundary(
        database,  # type: ignore[arg-type]
        worker_id="runtime-worker-test",
        last_heartbeat_monotonic=42.0,
    )

    assert updated == 123.0
    assert calls == [
        {
            "args": (database,),
            "worker_id": "runtime-worker-test",
            "last_heartbeat_monotonic": 42.0,
        }
    ]


def test_worker_heartbeat_is_durable_and_marks_clean_or_failed_shutdown(
    runtime_client: TestClient,
) -> None:
    database = runtime_client.app.state.database
    observed_at = datetime.now(timezone.utc)
    assert record_worker_heartbeat(
        database,
        worker_id="runtime-worker-a",
        cycle_completed=True,
        now=observed_at,
    )
    assert mark_worker_stopped(
        database,
        worker_id="runtime-worker-a",
        last_error_code="private provider response must not persist",
    )

    with database.session_factory() as session:
        heartbeat = session.get(RuntimeWorkerHeartbeat, "runtime-worker-a")
        assert heartbeat is not None
        assert heartbeat.status == "stopped"
        assert heartbeat.last_cycle_completed_at is not None
        completed_at = heartbeat.last_cycle_completed_at.replace(tzinfo=timezone.utc)
        assert completed_at == observed_at
        assert heartbeat.last_error_code == "worker_task_failed"


def test_runtime_overview_is_platform_only_global_and_content_free(
    runtime_client: TestClient,
) -> None:
    assert runtime_client.get("/v1/platform/runtime/overview").status_code == 401

    _register_and_verify(runtime_client)
    denied = runtime_client.get("/v1/platform/runtime/overview")
    assert denied.status_code == 403
    assert denied.json()["detail"] == "platform_admin_required"
    assert runtime_client.post("/v1/auth/logout").status_code == 204

    database = runtime_client.app.state.database
    current_time = datetime.now(timezone.utc)
    assert record_worker_heartbeat(
        database,
        worker_id="runtime-private-worker-identifier",
        worker_kind="background",
        now=current_time,
    )
    with database.session_factory() as session:
        session.add(
            RuntimeWorkerHeartbeat(
                worker_id="runtime-stale-private-worker-identifier",
                worker_kind="private-worker-kind",
                status="running",
                started_at=current_time - timedelta(minutes=15),
                last_seen_at=current_time - timedelta(minutes=6),
                last_error_code="Sensitive Candidate Name must not leave the database",
            )
        )
        session.commit()
    _feedback_with_unsafe_error(
        runtime_client,
        # This looks like a syntactically-valid identifier. A regex-only
        # sanitizer would leak it, so the endpoint must use its fixed allowlist.
        error="john_doe",
    )

    _login_platform_admin(runtime_client)
    overview = runtime_client.get("/v1/platform/runtime/overview")
    assert overview.status_code == 200, overview.text
    payload = overview.json()
    assert payload["worker_liveness"] == "live"
    assert payload["worker_stale_after_seconds"] == 300
    assert payload["live_worker_process_count"] == 1
    assert payload["configured_worker_concurrency"] == 1
    assert {worker["liveness"] for worker in payload["workers"]} >= {"live", "stale"}
    assert "unknown" in {worker["worker_kind"] for worker in payload["workers"]}
    feedback_queue = next(
        queue
        for queue in payload["queues"]
        if queue["queue_key"] == "workspace_feedback_reward"
    )
    assert feedback_queue["queued_count"] == 1
    assert feedback_queue["failed_count"] == 1
    feedback_failure = next(
        failure
        for failure in payload["recent_failures"]
        if failure["queue_key"] == "workspace_feedback_reward"
    )
    assert feedback_failure["error_code"] == "worker_task_failed"
    assert feedback_failure["attempt_count"] == 2

    # The control-plane response is intentionally aggregate-only. It cannot
    # become a side channel for host, queue-row, workspace, or candidate data.
    serialized = overview.text
    for forbidden in (
        "runtime-private-worker-identifier",
        "runtime-stale-private-worker-identifier",
        "private-worker-kind",
        "john_doe",
        "Sensitive Candidate Name",
        "organization_id",
        "worker_id",
        "job_id",
    ):
        assert forbidden not in serialized


def test_runtime_overview_reports_stale_worker_when_no_live_worker_exists(
    runtime_client: TestClient,
) -> None:
    database = runtime_client.app.state.database
    current_time = datetime.now(timezone.utc)
    with database.session_factory() as session:
        session.add(
            RuntimeWorkerHeartbeat(
                worker_id="runtime-only-stale-worker",
                worker_kind="background",
                status="running",
                started_at=current_time - timedelta(minutes=10),
                last_seen_at=current_time - timedelta(minutes=6),
            )
        )
        session.commit()

    with database.session_factory() as session:
        overview = get_platform_runtime_overview(session, now=current_time)
    assert overview.worker_liveness == "stale"
    assert overview.workers[0].liveness == "stale"


def test_runtime_overview_collapses_restarted_processes_by_worker_kind(
    runtime_client: TestClient,
) -> None:
    """A stopped predecessor must not duplicate a healthy worker row."""

    database = runtime_client.app.state.database
    current_time = datetime.now(timezone.utc)
    with database.session_factory() as session:
        session.add_all(
            [
                RuntimeWorkerHeartbeat(
                    worker_id="runtime-predecessor-worker",
                    worker_kind="background",
                    status="stopped",
                    started_at=current_time - timedelta(minutes=8),
                    last_seen_at=current_time - timedelta(minutes=6),
                ),
                RuntimeWorkerHeartbeat(
                    worker_id="runtime-current-worker",
                    worker_kind="background",
                    status="running",
                    started_at=current_time - timedelta(minutes=2),
                    last_seen_at=current_time - timedelta(seconds=5),
                ),
            ]
        )
        session.commit()

    with database.session_factory() as session:
        overview = get_platform_runtime_overview(session, now=current_time)

    assert overview.worker_liveness == "live"
    assert len(overview.workers) == 1
    assert overview.workers[0].worker_kind == "background"
    assert overview.workers[0].liveness == "live"
    assert overview.live_worker_process_count == 1


def test_runtime_overview_reports_live_process_count_without_exposing_ids(
    runtime_client: TestClient,
) -> None:
    database = runtime_client.app.state.database
    current_time = datetime.now(timezone.utc)
    with database.session_factory() as session:
        for index in range(3):
            session.add(
                RuntimeWorkerHeartbeat(
                    worker_id=f"runtime-pool-worker-{index}",
                    worker_kind="background",
                    status="running",
                    started_at=current_time - timedelta(minutes=1),
                    last_seen_at=current_time - timedelta(seconds=index),
                )
            )
        session.commit()

    configured = replace(runtime_client.app.state.settings, worker_concurrency=3)
    with database.session_factory() as session:
        overview = get_platform_runtime_overview(
            session,
            settings=configured,
            now=current_time,
        )

    assert overview.worker_liveness == "live"
    assert overview.live_worker_process_count == 3
    assert overview.configured_worker_concurrency == 3
    assert len(overview.workers) == 1


def test_runtime_overview_drops_expired_process_heartbeats(
    runtime_client: TestClient,
) -> None:
    """A hard-killed old process must not remain stale forever in the UI."""

    database = runtime_client.app.state.database
    current_time = datetime.now(timezone.utc)
    with database.session_factory() as session:
        session.add(
            RuntimeWorkerHeartbeat(
                worker_id="runtime-expired-worker",
                worker_kind="background",
                status="running",
                started_at=current_time - timedelta(days=3),
                last_seen_at=current_time - timedelta(days=2),
            )
        )
        session.commit()

    with database.session_factory() as session:
        overview = get_platform_runtime_overview(session, now=current_time)
    assert overview.worker_liveness == "missing"
    assert overview.workers == []
