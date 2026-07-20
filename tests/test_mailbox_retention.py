from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppSettings
from app.main import create_app
from app.models import (
    Candidate,
    EmailAttachmentImport,
    MailboxConfig,
    MailboxContentReplica,
    Resume,
)
from app.services.mailbox_retention_service import (
    resolve_mailbox_replica_path,
    store_failed_attachment_copy,
    store_mailbox_body_copy,
    store_success_attachment_copy,
)
from app.services.resume_service import (
    build_resume_storage_key,
    resolve_uploaded_resume_path,
)
from app.tenant_scope import set_organization_context


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_mailbox_config(client: TestClient, *, retention_policy: str = "standard") -> str:
    """Add one mailbox without involving an external IMAP server."""

    database = client.app.state.database
    with database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.retention.test",
            imap_port=993,
            email_address="recruiting@retention.test",
            mailbox="INBOX",
            encrypted_password="retention-test-ciphertext",
            enabled=True,
            retention_policy=retention_policy,
        )
        session.add(config)
        session.flush()
        config_id = config.id
        session.commit()
    return config_id


def test_mailbox_retention_has_safe_default_then_accepts_all_policy_tiers(client) -> None:
    before_configuration = client.get("/v1/mailbox/retention")
    assert before_configuration.status_code == 200, before_configuration.text
    assert before_configuration.json() == {
        "configured": False,
        "retention_policy": "standard",
        "body_copy_count": 0,
        "attachment_copy_count": 0,
        "failure_artifact_count": 0,
        "cache_bytes": 0,
        "expired_body_count": 0,
        "expired_attachment_copy_count": 0,
        "expired_failure_artifact_count": 0,
        "expired_bytes": 0,
        "earliest_expires_at": None,
        "last_cleanup_at": None,
        "next_cleanup_at": None,
    }

    _seed_mailbox_config(client)
    for policy in ("minimal", "standard", "audit"):
        saved = client.put(
            "/v1/mailbox/retention",
            json={"retention_policy": policy},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
        assert saved.json()["retention_policy"] == policy

        fetched = client.get("/v1/mailbox/retention")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["retention_policy"] == policy


def test_retention_cleanup_deletes_only_expired_cache_and_skips_an_active_retry(client) -> None:
    """A cache cleaner must never reach a candidate's canonical resume file."""

    settings = client.app.state.settings
    database = client.app.state.database
    original_bytes = b"canonical candidate resume must survive retention cleanup"
    body_bytes = b"transient email body"
    attachment_copy_bytes = b"transient successful attachment duplicate"
    failed_attachment_bytes = b"transient failure retry attachment"

    with database.session_factory() as session:
        config = MailboxConfig(
            imap_host="imap.retention.test",
            imap_port=993,
            email_address="recruiting@retention.test",
            mailbox="INBOX",
            encrypted_password="retention-test-ciphertext",
            enabled=True,
            retention_policy="standard",
        )
        session.add(config)
        session.flush()

        candidate = Candidate(display_name="Retention fixture")
        session.add(candidate)
        session.flush()

        storage_key = build_resume_storage_key(
            organization_id=config.organization_id,
            suffix=".pdf",
        )
        original_path = resolve_uploaded_resume_path(
            settings,
            storage_key=storage_key,
            organization_id=config.organization_id,
            require_file=False,
        )
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(original_bytes)
        resume = Resume(
            candidate_id=candidate.id,
            original_filename="canonical.pdf",
            storage_key=storage_key,
            sha256="a" * 64,
            source_page_count=1,
            parsed_page_count=1,
            extraction_status="text_ready",
            quality_flags=[],
            parser_version="retention-test",
            raw_text="retention fixture text",
            is_active=False,
        )
        session.add(resume)
        session.flush()

        imported_attachment = EmailAttachmentImport(
            mailbox_config_id=config.id,
            message_uid="imported-message",
            message_id="<imported-message@retention.test>",
            attachment_filename="imported.pdf",
            attachment_sha256="b" * 64,
            source_uidvalidity=1,
            source_fingerprint="c" * 64,
            resume_id=resume.id,
            status="imported",
            attempt_count=1,
            last_attempted_at=_now(),
        )
        retrying_attachment = EmailAttachmentImport(
            mailbox_config_id=config.id,
            message_uid="retrying-message",
            message_id="<retrying-message@retention.test>",
            attachment_filename="retrying.pdf",
            attachment_sha256="d" * 64,
            source_uidvalidity=1,
            source_fingerprint="e" * 64,
            status="retrying",
            attempt_count=1,
            last_attempted_at=_now(),
            retry_lease_expires_at=_now() + timedelta(minutes=5),
        )
        session.add_all((imported_attachment, retrying_attachment))
        session.flush()

        body_replica = store_mailbox_body_copy(
            session,
            settings=settings,
            config=config,
            message_uid="body-message",
            content=body_bytes,
        )
        success_replica = store_success_attachment_copy(
            session,
            settings=settings,
            config=config,
            attachment_import=imported_attachment,
            content=attachment_copy_bytes,
            suffix=".pdf",
        )
        failure_replica = store_failed_attachment_copy(
            session,
            settings=settings,
            config=config,
            attachment_import=retrying_attachment,
            content=failed_attachment_bytes,
            suffix=".pdf",
        )
        assert body_replica is not None
        assert success_replica is not None
        assert failure_replica is not None

        expiry = _now() - timedelta(seconds=1)
        for replica in (body_replica, success_replica, failure_replica):
            replica.expires_at = expiry
        session.commit()

        replica_paths = {
            replica.id: resolve_mailbox_replica_path(
                settings,
                storage_key=replica.storage_key,
                organization_id=config.organization_id,
            )
            for replica in (body_replica, success_replica, failure_replica)
        }
        replica_ids = {
            "body": body_replica.id,
            "success": success_replica.id,
            "failure": failure_replica.id,
        }

    preview = client.post("/v1/mailbox/retention/preview")
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["expired_body_count"] == 1
    assert preview_payload["expired_attachment_copy_count"] == 1
    assert preview_payload["expired_failure_artifact_count"] == 1
    assert preview_payload["skipped_count"] == 1

    cleaned = client.post("/v1/mailbox/retention/cleanup")
    assert cleaned.status_code == 200, cleaned.text
    cleanup_payload = cleaned.json()
    assert cleanup_payload["trigger_type"] == "manual"
    assert cleanup_payload["scanned_count"] == 3
    assert cleanup_payload["deleted_count"] == 2
    assert cleanup_payload["skipped_count"] == 1
    assert cleanup_payload["failed_count"] == 0
    assert cleanup_payload["reclaimed_bytes"] == len(body_bytes) + len(attachment_copy_bytes)

    assert original_path.read_bytes() == original_bytes
    assert replica_paths[replica_ids["body"]].exists() is False
    assert replica_paths[replica_ids["success"]].exists() is False
    assert replica_paths[replica_ids["failure"]].read_bytes() == failed_attachment_bytes

    with database.session_factory() as session:
        replicas = {
            replica.id: replica
            for replica in session.scalars(
                select(MailboxContentReplica).where(
                    MailboxContentReplica.id.in_(tuple(replica_ids.values()))
                )
            ).all()
        }
    assert replicas[replica_ids["body"]].cleaned_at is not None
    assert replicas[replica_ids["success"]].cleaned_at is not None
    assert replicas[replica_ids["failure"]].cleaned_at is None


@pytest.fixture
def workspace_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two signed-in browser sessions backed by one tenant-aware database."""

    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="retention-tenant-test-session-secret",
        min_text_chars_per_page=20,
        transactional_email_provider="test",
        public_app_url="http://testserver",
    )
    app = create_app(settings)
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
    full_name: str,
    email: str,
    password: str,
) -> dict[str, object]:
    registered = client.post(
        "/v1/auth/register",
        json={
            "organization_name": organization_name,
            "full_name": full_name,
            "email": email,
            "password": password,
        },
    )
    assert registered.status_code == 201, registered.text

    provider = client.app.state.transactional_email_provider
    delivery = next(item for item in reversed(provider.deliveries) if item.recipient == email)
    token = parse_qs(urlsplit(delivery.verification_url).query)["token"][0]
    verified = client.post("/v1/auth/email-verification/complete", json={"token": token})
    assert verified.status_code == 200, verified.text

    logged_in = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    session = client.get("/v1/auth/session")
    assert session.status_code == 200, session.text
    return session.json()


def _seed_workspace_mailbox(
    client: TestClient,
    *,
    organization_id: str,
    policy: str,
) -> str:
    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        config = MailboxConfig(
            imap_host="imap.tenant-retention.test",
            imap_port=993,
            email_address=f"{policy}@retention.test",
            mailbox="INBOX",
            encrypted_password="retention-test-ciphertext",
            enabled=True,
            retention_policy=policy,
        )
        session.add(config)
        session.flush()
        config_id = config.id
        session.commit()
    return config_id


def test_mailbox_retention_endpoints_never_cross_workspaces(
    workspace_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = workspace_clients
    session_a = _register_and_login(
        client_a,
        organization_name="Retention Alpha",
        full_name="Alpha Admin",
        email="retention-alpha@example.test",
        password="retention-tenant-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Retention Beta",
        full_name="Beta Admin",
        email="retention-beta@example.test",
        password="retention-tenant-password-b",
    )
    organization_a_id = str(session_a["organization"]["organization_id"])
    organization_b_id = str(session_b["organization"]["organization_id"])
    _seed_workspace_mailbox(client_a, organization_id=organization_a_id, policy="minimal")
    config_b_id = _seed_workspace_mailbox(client_b, organization_id=organization_b_id, policy="audit")

    settings = client_b.app.state.settings
    database = client_b.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_b_id)
        config_b = session.get(MailboxConfig, config_b_id)
        assert config_b is not None
        replica = store_mailbox_body_copy(
            session,
            settings=settings,
            config=config_b,
            message_uid="workspace-b-expired-body",
            content=b"workspace B cache only",
        )
        assert replica is not None
        replica.expires_at = _now() - timedelta(seconds=1)
        cache_path = resolve_mailbox_replica_path(
            settings,
            storage_key=replica.storage_key,
            organization_id=organization_b_id,
        )
        session.commit()

    a_summary = client_a.get("/v1/mailbox/retention")
    b_summary = client_b.get("/v1/mailbox/retention")
    assert a_summary.status_code == 200, a_summary.text
    assert b_summary.status_code == 200, b_summary.text
    assert a_summary.json()["retention_policy"] == "minimal"
    assert a_summary.json()["cache_bytes"] == 0
    assert b_summary.json()["retention_policy"] == "audit"
    assert b_summary.json()["expired_body_count"] == 1

    a_preview = client_a.post("/v1/mailbox/retention/preview")
    assert a_preview.status_code == 200, a_preview.text
    assert a_preview.json()["expired_body_count"] == 0
    assert client_a.get("/v1/mailbox/retention/runs").json() == {"items": [], "total": 0}

    b_cleanup = client_b.post("/v1/mailbox/retention/cleanup")
    assert b_cleanup.status_code == 200, b_cleanup.text
    assert b_cleanup.json()["deleted_count"] == 1
    assert cache_path.exists() is False

    b_runs = client_b.get("/v1/mailbox/retention/runs")
    assert b_runs.status_code == 200, b_runs.text
    assert b_runs.json()["total"] == 1
    assert b_runs.json()["items"][0]["run_id"] == b_cleanup.json()["run_id"]

    a_runs_after_b_cleanup = client_a.get("/v1/mailbox/retention/runs")
    assert a_runs_after_b_cleanup.status_code == 200, a_runs_after_b_cleanup.text
    assert a_runs_after_b_cleanup.json() == {"items": [], "total": 0}
