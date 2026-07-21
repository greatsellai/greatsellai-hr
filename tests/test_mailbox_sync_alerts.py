from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select

from app.models import MailboxConfig, MailboxSyncFailureAlert
from app.schemas import MailboxSyncResponse
from app.services import mailbox_background_job_service, mailbox_import_service
from app.services.mailbox_sync_alert_service import record_terminal_sync_failure


def _create_config(client) -> str:
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
            import_start_uid=42,
            imap_uidvalidity=9,
        )
        session.add(config)
        session.commit()
        return config.id


def _exhaust_sync_failure(client, *, mailbox_id: str, error_code: str) -> None:
    queued = client.post(f"/v1/mailboxes/{mailbox_id}/sync")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    for attempt in range(3):
        assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id=f"sync-alert-failure-{attempt}",
        )
        if attempt < 2:
            with client.app.state.database.session_factory() as session:
                job = session.get(mailbox_background_job_service.MailboxBackgroundJob, job_id)
                assert job is not None
                job.next_attempt_at = (
                    mailbox_background_job_service._utcnow() - timedelta(seconds=1)
                )
                session.commit()


def test_alert_opens_after_three_terminal_sync_failures_and_resolves_after_success(
    client,
    monkeypatch,
) -> None:
    mailbox_id = _create_config(client)

    def transient_failure(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_connection_failed")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", transient_failure)

    for completed_sync in range(1, 4):
        _exhaust_sync_failure(
            client,
            mailbox_id=mailbox_id,
            error_code="mailbox_connection_failed",
        )
        with client.app.state.database.session_factory() as session:
            alert = session.scalar(select(MailboxSyncFailureAlert))
            assert alert is not None
            assert alert.consecutive_failures == completed_sync
            assert alert.state == ("open" if completed_sync == 3 else "monitoring")

    config_response = client.get(f"/v1/mailboxes/{mailbox_id}")
    assert config_response.status_code == 200, config_response.text
    summary = config_response.json()["active_sync_alert"]
    assert summary is not None
    assert summary["consecutive_failures"] == 3
    assert summary["last_error_code"] == "mailbox_connection_failed"

    def successful_sync(*args, **kwargs):
        return MailboxSyncResponse(configured=True)

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", successful_sync)
    queued = client.post(f"/v1/mailboxes/{mailbox_id}/sync")
    assert queued.status_code == 202, queued.text
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="sync-alert-recovery",
    )

    with client.app.state.database.session_factory() as session:
        alert = session.scalar(select(MailboxSyncFailureAlert))
        assert alert is not None
        assert alert.state == "resolved"
        assert alert.consecutive_failures == 0
        assert alert.resolution == "sync_succeeded"

    recovered_response = client.get(f"/v1/mailboxes/{mailbox_id}")
    assert recovered_response.status_code == 200, recovered_response.text
    assert recovered_response.json()["active_sync_alert"] is None


def test_critical_imap_security_failure_opens_alert_without_three_task_failures(
    client,
    monkeypatch,
) -> None:
    mailbox_id = _create_config(client)

    def blocked_endpoint(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_imap_host_not_allowed")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", blocked_endpoint)
    queued = client.post(f"/v1/mailboxes/{mailbox_id}/sync")
    assert queued.status_code == 202, queued.text
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="sync-alert-critical",
    )

    with client.app.state.database.session_factory() as session:
        alert = session.scalar(select(MailboxSyncFailureAlert))
        assert alert is not None
        assert alert.state == "open"
        assert alert.severity == "critical"
        assert alert.consecutive_failures == 1


def test_open_sync_alert_does_not_disappear_when_its_failure_window_elapses(client) -> None:
    mailbox_id = _create_config(client)
    settings = replace(
        client.app.state.settings,
        mailbox_consecutive_failure_alert_threshold=1,
        mailbox_consecutive_failure_window_seconds=60,
    )
    first_failure = mailbox_background_job_service._utcnow()

    with client.app.state.database.session_factory() as session:
        opened = record_terminal_sync_failure(
            session,
            settings=settings,
            mailbox_config_id=mailbox_id,
            job_id="first-failure",
            error_code="mailbox_connection_failed",
            now=first_failure,
        )
        assert opened is not None
        assert opened.state == "open"
        opened_at = opened.opened_at
        session.commit()

        still_open = record_terminal_sync_failure(
            session,
            settings=settings,
            mailbox_config_id=mailbox_id,
            job_id="later-failure",
            error_code="mailbox_connection_failed",
            now=first_failure + timedelta(seconds=61),
        )
        assert still_open is not None
        assert still_open.state == "open"
        assert still_open.opened_at == opened_at
        assert still_open.consecutive_failures == 2
        session.commit()

    response = client.get(f"/v1/mailboxes/{mailbox_id}")
    assert response.status_code == 200, response.text
    assert response.json()["active_sync_alert"] is not None


def test_search_response_limit_opens_an_alert_after_three_terminal_sync_tasks(
    client,
    monkeypatch,
) -> None:
    mailbox_id = _create_config(client)

    def oversized_search(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_search_response_too_large")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", oversized_search)
    # The oversized search is intentionally terminal for each task (retrying
    # cannot make an unbounded UID result safe), so three independent queued
    # syncs make up the consecutive failure streak.
    for attempt in range(3):
        queued = client.post(f"/v1/mailboxes/{mailbox_id}/sync")
        assert queued.status_code == 202, queued.text
        assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id=f"sync-alert-search-{attempt}",
        )

    with client.app.state.database.session_factory() as session:
        alert = session.scalar(select(MailboxSyncFailureAlert))
        assert alert is not None
        assert alert.state == "open"
        assert alert.severity == "warning"
        assert alert.last_error_code == "mailbox_search_response_too_large"


def test_sync_lease_collisions_do_not_create_a_failure_alert(client, monkeypatch) -> None:
    mailbox_id = _create_config(client)

    def already_running(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_sync_in_progress")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", already_running)
    for attempt in range(3):
        queued = client.post(f"/v1/mailboxes/{mailbox_id}/sync")
        assert queued.status_code == 202, queued.text
        assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id=f"sync-alert-lease-{attempt}",
        )

    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(MailboxSyncFailureAlert)) is None


def test_attachment_retry_failure_does_not_create_mailbox_sync_alert(client, monkeypatch) -> None:
    mailbox_id = _create_config(client)

    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, mailbox_id)
        assert config is not None
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<retry@example.test>",
            filename="resume.pdf",
            attachment_sha256="a" * 64,
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        import_id = record.id

    def failed_retry(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_connection_failed")

    monkeypatch.setattr(mailbox_background_job_service, "retry_mailbox_attachment", failed_retry)
    queued = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert queued.status_code == 202, queued.text
    for attempt in range(3):
        assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id=f"attachment-alert-{attempt}",
        )
        if attempt < 2:
            with client.app.state.database.session_factory() as session:
                job = session.get(mailbox_background_job_service.MailboxBackgroundJob, queued.json()["job_id"])
                assert job is not None
                job.next_attempt_at = mailbox_background_job_service._utcnow() - timedelta(seconds=1)
                session.commit()

    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(MailboxSyncFailureAlert)) is None
