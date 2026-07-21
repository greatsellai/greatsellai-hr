from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main as main_module
from app.config import AppSettings
from app.main import create_app
from app.models import PasswordResetToken, TransactionalEmailOutbox
from app.services.identity_service import utcnow
from app.services.transactional_email import TransactionalEmailError
from app.services.transactional_email_outbox_service import (
    OUTBOX_CANCELLED,
    OUTBOX_COMPLETED,
    OUTBOX_QUEUED,
    TransactionalEmailOutboxError,
    run_transactional_email_outbox_worker_once,
)


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "sqlite://",
        "session_secret": "transactional-outbox-test-session-secret",
        "transactional_email_provider": "test",
        "public_app_url": "http://testserver",
        "allow_unauthenticated": False,
        "password_reset_rate_limit_global_limit": 100,
        "password_reset_rate_limit_client_limit": 100,
        "password_reset_rate_limit_email_limit": 100,
    }
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


def _register_and_verify(client: TestClient, *, email: str, password: str) -> None:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Transactional outbox fixture workspace",
            "full_name": "Transactional outbox fixture owner",
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text
    delivery = client.app.state.transactional_email_provider.deliveries[-1]
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert client.post("/v1/auth/logout").status_code == 204


def _request_reset(client: TestClient, *, email: str) -> dict[str, object]:
    response = client.post("/v1/auth/password-reset/request", json={"email": email})
    assert response.status_code == 200, response.text
    return response.json()


def _queued_jobs(client: TestClient) -> list[TransactionalEmailOutbox]:
    with client.app.state.database.session_factory() as session:
        return session.scalars(
            select(TransactionalEmailOutbox).order_by(
                TransactionalEmailOutbox.requested_at,
                TransactionalEmailOutbox.id,
            )
        ).all()


def _run_once(client: TestClient, *, worker_id: str = "transactional-outbox-test-worker") -> bool:
    return run_transactional_email_outbox_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id=worker_id,
        provider=client.app.state.transactional_email_provider,
    )


def test_password_reset_queues_encrypted_work_without_synchronous_provider_io(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        email = "queued-reset@example.test"
        _register_and_verify(client, email=email, password="queued-reset-password")
        provider = client.app.state.transactional_email_provider
        original_send = provider.send_password_reset
        attempted_synchronously = False

        def reject_sync_delivery(_delivery) -> None:
            nonlocal attempted_synchronously
            attempted_synchronously = True
            raise AssertionError("password reset endpoint must not call the provider")

        monkeypatch.setattr(provider, "send_password_reset", reject_sync_delivery)
        known = _request_reset(client, email=email)
        unknown = _request_reset(client, email="unknown-queued-reset@example.test")

        assert known == unknown == {"accepted": True, "delivery_available": True}
        assert attempted_synchronously is False
        jobs = _queued_jobs(client)
        assert len(jobs) == 1
        queued = jobs[0]
        assert queued.status == OUTBOX_QUEUED
        assert email not in queued.encrypted_payload
        assert "reset-password" not in queued.encrypted_payload

        monkeypatch.setattr(provider, "send_password_reset", original_send)
        assert _run_once(client)
        delivery = provider.password_reset_deliveries[-1]
        raw_token = parse_qs(urlsplit(delivery.reset_url).query)["token"][0]

        completed = _queued_jobs(client)[0]
        assert completed.status == OUTBOX_COMPLETED
        assert raw_token not in completed.encrypted_payload
        assert completed.sent_at is not None


def test_transactional_outbox_retries_safe_provider_failures(tmp_path: Path, monkeypatch) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        email = "retry-reset@example.test"
        _register_and_verify(client, email=email, password="retry-reset-password")
        _request_reset(client, email=email)

        provider = client.app.state.transactional_email_provider
        original_send = provider.send_password_reset

        def fail_delivery(_delivery) -> None:
            raise TransactionalEmailError("email_delivery_provider_failed")

        monkeypatch.setattr(provider, "send_password_reset", fail_delivery)
        assert _run_once(client)
        retried = _queued_jobs(client)[0]
        assert retried.status == OUTBOX_QUEUED
        assert retried.attempt_count == 1
        assert retried.next_attempt_at is not None
        assert retried.last_error == "email_delivery_provider_failed"

        with client.app.state.database.session_factory() as session:
            job = session.get(TransactionalEmailOutbox, retried.id)
            assert job is not None
            job.next_attempt_at = utcnow() - timedelta(seconds=1)
            session.commit()

        monkeypatch.setattr(provider, "send_password_reset", original_send)
        assert _run_once(client)
        delivered = _queued_jobs(client)[0]
        assert delivered.status == OUTBOX_COMPLETED
        assert delivered.attempt_count == 2
        assert provider.password_reset_deliveries[-1].recipient == email


def test_outbox_enqueue_failure_keeps_password_reset_response_non_enumerating(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        email = "outbox-failure-reset@example.test"
        _register_and_verify(client, email=email, password="outbox-failure-password")

        def fail_enqueue(*_args, **_kwargs):
            raise TransactionalEmailOutboxError("test-only-enqueue-failure")

        monkeypatch.setattr(main_module, "enqueue_password_reset_delivery", fail_enqueue)
        known = _request_reset(client, email=email)
        unknown = _request_reset(client, email="unknown-outbox-failure@example.test")

        assert known == unknown == {"accepted": True, "delivery_available": True}
        assert _queued_jobs(client) == []
        with client.app.state.database.session_factory() as session:
            assert session.scalars(select(PasswordResetToken)).all() == []


def test_worker_cancels_invalidated_reset_before_it_can_be_delivered(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        email = "stale-reset@example.test"
        _register_and_verify(client, email=email, password="stale-reset-password")
        _request_reset(client, email=email)
        first = _queued_jobs(client)[0]
        _request_reset(client, email=email)
        jobs = _queued_jobs(client)
        assert len(jobs) == 2
        second = next(job for job in jobs if job.id != first.id)

        # Make the order independent of timestamp resolution on SQLite.
        with client.app.state.database.session_factory() as session:
            first_job = session.get(TransactionalEmailOutbox, first.id)
            second_job = session.get(TransactionalEmailOutbox, second.id)
            assert first_job is not None and second_job is not None
            first_job.next_attempt_at = utcnow() - timedelta(seconds=1)
            second_job.next_attempt_at = utcnow()
            session.commit()

        assert _run_once(client)
        after_first_claim = {job.id: job for job in _queued_jobs(client)}
        assert after_first_claim[first.id].status == OUTBOX_CANCELLED
        assert client.app.state.transactional_email_provider.password_reset_deliveries == []

        assert _run_once(client)
        after_second_claim = {job.id: job for job in _queued_jobs(client)}
        assert after_second_claim[second.id].status == OUTBOX_COMPLETED
        assert len(client.app.state.transactional_email_provider.password_reset_deliveries) == 1
