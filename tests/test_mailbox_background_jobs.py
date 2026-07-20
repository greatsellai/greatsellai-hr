from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    EmailAttachmentImport,
    MailboxBackgroundJob,
    MailboxConfig,
    MailboxContentReplica,
)
from app.schemas import MailboxImportResponse, MailboxSyncResponse
from app.services import (
    mailbox_background_job_service,
    mailbox_import_service,
    mailbox_retention_service,
)


def _create_config(client, *, enabled: bool = True) -> tuple[str, str]:
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings
    ).encrypt(b"unit-test-authorization-code").decode("ascii")
    with client.app.state.database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=enabled,
            import_start_uid=42,
            imap_uidvalidity=9,
        )
        session.add(config)
        session.commit()
        return config.id, config.organization_id


def _create_failed_import(client, *, config_id: str) -> str:
    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        record = mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<background-job@example.test>",
            filename="retry.pdf",
            attachment_sha256=hashlib.sha256(b"retry source").hexdigest(),
            status="failed",
            error="attachment_import_failed",
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        return record.id


def test_manual_sync_enqueues_without_connecting_to_imap_then_worker_completes(
    client,
    monkeypatch,
) -> None:
    _create_config(client)

    def unexpected_imap(*args, **kwargs):
        raise AssertionError("HTTP request must not open IMAP")

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", unexpected_imap)
    queued = client.post("/v1/mailbox/sync")
    assert queued.status_code == 202, queued.text
    payload = queued.json()
    assert payload["status"] == "queued"
    assert payload["job_kind"] == "sync"
    assert payload["mailbox_id"]

    polled = client.get(f"/v1/mailbox/tasks/{payload['job_id']}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "queued"

    def completed_sync(
        session,
        *,
        settings,
        config_id,
        expected_source_fingerprint=None,
        heartbeat=None,
    ):
        assert heartbeat is not None
        heartbeat()
        return MailboxSyncResponse(
            configured=True,
            imported_count=2,
            duplicate_count=1,
            skipped_count=3,
            failed_count=4,
        )

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", completed_sync)
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="mailbox-job-test",
    )

    completed = client.get(f"/v1/mailbox/tasks/{payload['job_id']}")
    assert completed.status_code == 200, completed.text
    completed_payload = completed.json()
    assert completed_payload["status"] == "completed"
    assert completed_payload["imported_count"] == 2
    assert completed_payload["duplicate_count"] == 1
    assert completed_payload["skipped_count"] == 3
    assert completed_payload["failed_count"] == 4


def test_history_prune_failure_cannot_change_completed_job(
    client,
    monkeypatch,
) -> None:
    _create_config(client)
    queued = client.post("/v1/mailbox/sync")
    assert queued.status_code == 202, queued.text

    def completed_sync(*args, **kwargs):
        return MailboxSyncResponse(configured=True, imported_count=1)

    def failed_prune(*args, **kwargs):
        raise RuntimeError("simulated maintenance failure")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", completed_sync)
    monkeypatch.setattr(
        mailbox_background_job_service,
        "_prune_terminal_job_history",
        failed_prune,
    )

    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="mailbox-prune-failure-test",
    )
    completed = client.get(f"/v1/mailbox/tasks/{queued.json()['job_id']}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["last_error"] is None


def test_duplicate_sync_requests_coalesce_to_one_active_job(client) -> None:
    _create_config(client)

    first = client.post("/v1/mailbox/sync")
    second = client.post("/v1/mailbox/sync")
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["deduplicated"] is True

    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MailboxBackgroundJob)) == 1


def test_task_history_is_strictly_bounded_and_supports_stable_offset(client) -> None:
    config_id, organization_id = _create_config(client)
    now = mailbox_background_job_service._utcnow()
    with client.app.state.database.session_factory() as session:
        session.add_all(
            (
                MailboxBackgroundJob(
                    organization_id=organization_id,
                    mailbox_config_id=config_id,
                    job_kind="sync",
                    trigger_type="manual",
                    status="queued",
                    attempt_count=0,
                    max_attempts=3,
                    requested_at=now - timedelta(seconds=1),
                ),
                MailboxBackgroundJob(
                    organization_id=organization_id,
                    mailbox_config_id=config_id,
                    job_kind="sync",
                    trigger_type="manual",
                    status="completed",
                    attempt_count=1,
                    max_attempts=3,
                    requested_at=now,
                    completed_at=now,
                ),
            )
        )
        session.commit()

    history = client.get("/v1/mailbox/tasks?limit=1")
    assert history.status_code == 200, history.text
    payload = history.json()
    assert payload["total"] == 2
    assert len(payload["items"]) == 1
    assert payload["items"][0]["status"] == "queued"

    second_page = client.get("/v1/mailbox/tasks?limit=1&offset=1")
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["total"] == 2
    assert len(second_page.json()["items"]) == 1
    assert second_page.json()["items"][0]["status"] == "completed"


def test_sync_worker_retries_transient_failure_then_records_terminal_failure(
    client,
    monkeypatch,
) -> None:
    _create_config(client)
    queued = client.post("/v1/mailbox/sync")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]

    def transient_failure(*args, **kwargs):
        raise mailbox_import_service.MailboxImportError("mailbox_connection_failed")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", transient_failure)

    for attempt in range(1, 4):
        assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
            client.app.state.database,
            settings=client.app.state.settings,
            worker_id="mailbox-transient-failure-test",
        )
        with client.app.state.database.session_factory() as session:
            job = session.get(MailboxBackgroundJob, job_id)
            assert job is not None
            assert job.attempt_count == attempt
            assert job.lease_owner is None
            assert job.lease_expires_at is None
            assert job.last_error == "mailbox_connection_failed"
            if attempt < 3:
                assert job.status == "queued"
                assert job.next_attempt_at is not None
                # Move only the test's durable retry timestamp forward.  The
                # production worker still uses its exponential backoff.
                job.next_attempt_at = mailbox_background_job_service._utcnow() - timedelta(seconds=1)
                session.commit()
            else:
                assert job.status == "failed"
                assert job.next_attempt_at is None
                assert job.completed_at is not None


def test_sync_worker_hides_unexpected_exception_text_from_task_status(
    client,
    monkeypatch,
) -> None:
    _create_config(client)
    queued = client.post("/v1/mailbox/sync")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]

    def unexpected_failure(*args, **kwargs):
        raise RuntimeError("private provider response must not reach the browser")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", unexpected_failure)
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="mailbox-unexpected-failure-test",
    )

    task = client.get(f"/v1/mailbox/tasks/{job_id}")
    assert task.status_code == 200, task.text
    payload = task.json()
    assert payload["status"] == "queued"
    assert payload["last_error"] == "mailbox_background_job_failed"
    assert "private provider" not in payload["last_error"]


def test_attachment_retry_enqueues_without_fetching_then_worker_updates_history(
    client,
    monkeypatch,
) -> None:
    config_id, _ = _create_config(client)
    import_id = _create_failed_import(client, config_id=config_id)

    def unexpected_imap(*args, **kwargs):
        raise AssertionError("retry HTTP request must not open IMAP")

    monkeypatch.setattr(mailbox_import_service.imaplib, "IMAP4_SSL", unexpected_imap)
    queued = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"
    assert queued.json()["import_id"] == import_id
    duplicate = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["job_id"] == queued.json()["job_id"]
    assert duplicate.json()["deduplicated"] is True

    with client.app.state.database.session_factory() as session:
        record = session.get(EmailAttachmentImport, import_id)
        assert record is not None
        assert record.status == "failed"
        assert record.attempt_count == 1

    def completed_retry(session, *, settings, import_id, retry_lease_seconds, heartbeat=None):
        assert heartbeat is not None
        heartbeat()
        record = session.get(EmailAttachmentImport, import_id)
        assert record is not None
        record.status = "imported"
        record.error = None
        record.attempt_count += 1
        session.commit()
        return MailboxImportResponse(
            import_id=record.id,
            mailbox_config_id=record.mailbox_config_id,
            attachment_filename=record.attachment_filename,
            status="imported",
            error=None,
            resume_id=None,
            attempt_count=record.attempt_count,
            last_attempted_at=record.last_attempted_at,
            can_retry=False,
            created_at=record.created_at,
        )

    monkeypatch.setattr(mailbox_background_job_service, "retry_mailbox_attachment", completed_retry)
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="mailbox-retry-job-test",
    )

    completed = client.get(f"/v1/mailbox/tasks/{queued.json()['job_id']}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["imported_count"] == 1
    history = client.get("/v1/mailbox/imports")
    assert history.status_code == 200, history.text
    assert history.json()["items"][0]["status"] == "imported"
    assert history.json()["items"][0]["attempt_count"] == 2


def test_source_change_fails_queued_sync_without_opening_imap(client, monkeypatch) -> None:
    config_id, _ = _create_config(client)
    queued = client.post("/v1/mailbox/sync")
    assert queued.status_code == 202, queued.text

    with client.app.state.database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        config.imap_host = "imap.changed.example.test"
        session.commit()

    def unexpected_sync(*args, **kwargs):
        raise AssertionError("a source-changed task must not open IMAP")

    monkeypatch.setattr(mailbox_background_job_service, "sync_mailbox", unexpected_sync)
    assert mailbox_background_job_service.run_mailbox_background_job_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="mailbox-source-changed-test",
    )

    task = client.get(f"/v1/mailbox/tasks/{queued.json()['job_id']}")
    assert task.status_code == 200, task.text
    payload = task.json()
    assert payload["status"] == "failed"
    assert payload["attempt_count"] == 1
    assert payload["completed_at"] is not None
    assert payload["last_error"] == "mailbox_task_source_changed"


def test_queued_retry_protects_retained_failure_artifact_from_cleanup(client) -> None:
    config_id, organization_id = _create_config(client)
    import_id = _create_failed_import(client, config_id=config_id)
    queued = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert queued.status_code == 202, queued.text

    with client.app.state.database.session_factory() as session:
        replica = MailboxContentReplica(
            organization_id=organization_id,
            mailbox_config_id=config_id,
            email_attachment_import_id=import_id,
            kind="failed_attachment",
            source_reference=import_id,
            storage_key="mail-cache/unit-test/retry.pdf",
            content_sha256=hashlib.sha256(b"retry source").hexdigest(),
            byte_size=12,
            expires_at=mailbox_retention_service._utcnow() - timedelta(seconds=1),
        )
        session.add(replica)
        session.commit()
        assert mailbox_retention_service._retry_is_active(
            session,
            replica,
            now=mailbox_retention_service._utcnow(),
        )


def test_retry_enqueue_closes_cleanup_selection_race(client) -> None:
    config_id, organization_id = _create_config(client)
    import_id = _create_failed_import(client, config_id=config_id)
    cleanup_observed_at = mailbox_retention_service._utcnow()
    with client.app.state.database.session_factory() as session:
        replica = MailboxContentReplica(
            organization_id=organization_id,
            mailbox_config_id=config_id,
            email_attachment_import_id=import_id,
            kind="failed_attachment",
            source_reference=import_id,
            storage_key=f"{organization_id}/mail-cache/retry-race.pdf",
            content_sha256=hashlib.sha256(b"retry source").hexdigest(),
            byte_size=12,
            expires_at=cleanup_observed_at - timedelta(seconds=1),
        )
        session.add(replica)
        session.commit()
        replica_id = replica.id

    queued = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert queued.status_code == 202, queued.text

    with client.app.state.database.session_factory() as session:
        replica = session.get(MailboxContentReplica, replica_id)
        assert replica is not None
        assert (
            mailbox_retention_service._as_utc(replica.expires_at)
            > cleanup_observed_at
        )
        # A cleaner that selected the old expiry before the enqueue committed
        # must fail its conditional claim after the retry protection update.
        assert mailbox_retention_service._claim_replica_cleanup(
            session,
            replica=replica,
            now=cleanup_observed_at,
        ) is None


def test_retry_enqueue_rejects_when_cleanup_already_owns_retained_source(client) -> None:
    config_id, organization_id = _create_config(client)
    import_id = _create_failed_import(client, config_id=config_id)
    now = mailbox_retention_service._utcnow()
    with client.app.state.database.session_factory() as session:
        session.add(
            MailboxContentReplica(
                organization_id=organization_id,
                mailbox_config_id=config_id,
                email_attachment_import_id=import_id,
                kind="failed_attachment",
                source_reference=import_id,
                storage_key=f"{organization_id}/mail-cache/cleanup-owned.pdf",
                content_sha256=hashlib.sha256(b"only retained source").hexdigest(),
                byte_size=20,
                expires_at=now + timedelta(minutes=5),
                cleanup_claim_token="cleanup-owns-this-copy",
                cleanup_lease_expires_at=now + timedelta(minutes=2),
            )
        )
        session.commit()

    response = client.post(f"/v1/mailbox/imports/{import_id}/retry")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "mailbox_import_not_retryable"
    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(MailboxBackgroundJob))
            == 0
        )


def test_terminal_job_history_is_retained_but_bounded(client, monkeypatch) -> None:
    config_id, organization_id = _create_config(client)
    now = mailbox_background_job_service._utcnow()
    old_job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config_id,
        job_kind="sync",
        trigger_type="scheduled",
        status="completed",
        requested_at=now - timedelta(days=45),
        completed_at=now - timedelta(days=45),
    )
    overflow_job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config_id,
        job_kind="sync",
        trigger_type="scheduled",
        status="completed",
        requested_at=now - timedelta(days=2),
        completed_at=now - timedelta(days=2),
    )
    newest_job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config_id,
        job_kind="sync",
        trigger_type="manual",
        status="failed",
        requested_at=now - timedelta(days=1),
        completed_at=now - timedelta(days=1),
    )
    active_job = MailboxBackgroundJob(
        organization_id=organization_id,
        mailbox_config_id=config_id,
        job_kind="attachment_retry",
        trigger_type="manual",
        status="running",
        requested_at=now - timedelta(days=60),
    )
    with client.app.state.database.session_factory() as session:
        session.add_all((old_job, overflow_job, newest_job, active_job))
        session.commit()
        kept_ids = {newest_job.id, active_job.id}

        monkeypatch.setattr(
            mailbox_background_job_service,
            "_TERMINAL_JOB_MAX_PER_ORGANIZATION",
            1,
        )
        monkeypatch.setattr(
            mailbox_background_job_service,
            "_TERMINAL_JOB_PRUNE_BATCH_SIZE",
            10,
        )
        deleted = mailbox_background_job_service._prune_terminal_job_history(
            session,
            now=now,
        )
        assert deleted == 2
        assert set(session.scalars(select(MailboxBackgroundJob.id)).all()) == kept_ids
