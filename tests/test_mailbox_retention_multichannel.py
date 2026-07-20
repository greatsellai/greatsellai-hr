from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.models import MailboxConfig
from app.services.mailbox_retention_service import (
    cleanup_due_mailbox_retention,
    resolve_mailbox_replica_path,
    store_mailbox_body_copy,
)
from app.tenant_scope import set_organization_context


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _create_mailbox(client: TestClient, *, label: str, host: str) -> dict[str, object]:
    """Seed a channel without turning a retention test into an IMAP test."""

    database = client.app.state.database
    with database.session_factory() as session:
        config = MailboxConfig(
            display_name=label,
            display_name_key=label.casefold(),
            imap_host=host,
            imap_port=993,
            email_address=f"{host.replace('.', '-')}@example.test",
            mailbox="INBOX",
            encrypted_password="retention-test-ciphertext",
            enabled=True,
            retention_policy="standard",
        )
        session.add(config)
        session.flush()
        payload = {
            "mailbox_id": config.id,
            "display_name": config.display_name,
        }
        session.commit()
    return payload


def _seed_expired_body_copy(
    client: TestClient,
    *,
    mailbox_id: str,
    content: bytes,
    source_reference: str,
) -> Path:
    """Create synthetic cached mail content for one channel only."""

    database = client.app.state.database
    settings = client.app.state.settings
    with database.session_factory() as session:
        config = session.get(MailboxConfig, mailbox_id)
        assert config is not None
        set_organization_context(session, config.organization_id)
        replica = store_mailbox_body_copy(
            session,
            settings=settings,
            config=config,
            message_uid=source_reference,
            content=content,
        )
        assert replica is not None
        replica.expires_at = _utcnow() - timedelta(seconds=1)
        replica_path = resolve_mailbox_replica_path(
            settings,
            storage_key=replica.storage_key,
            organization_id=config.organization_id,
        )
        session.commit()
    return replica_path


def test_named_retention_endpoints_scope_policy_cache_cleanup_and_runs(client) -> None:
    """Retention actions must target the requested source, never the newest one."""

    first = _create_mailbox(client, label="社招通道", host="imap.social.test")
    second = _create_mailbox(client, label="校园通道", host="imap.campus.test")
    first_id = str(first["mailbox_id"])
    second_id = str(second["mailbox_id"])

    initial_first = client.get(f"/v1/mailboxes/{first_id}/retention")
    initial_second = client.get(f"/v1/mailboxes/{second_id}/retention")
    assert initial_first.status_code == 200, initial_first.text
    assert initial_second.status_code == 200, initial_second.text

    updated_first = client.put(
        f"/v1/mailboxes/{first_id}/retention",
        json={"retention_policy": "audit"},
    )
    assert updated_first.status_code == 200, updated_first.text
    assert updated_first.json()["retention_policy"] == "audit"
    unchanged_second = client.get(f"/v1/mailboxes/{second_id}/retention")
    assert unchanged_second.status_code == 200, unchanged_second.text
    assert unchanged_second.json()["retention_policy"] == "standard"

    first_content = b"first channel transient mail body"
    second_content = b"second channel transient mail body"
    first_path = _seed_expired_body_copy(
        client,
        mailbox_id=first_id,
        content=first_content,
        source_reference="first-channel-expired-body",
    )
    second_path = _seed_expired_body_copy(
        client,
        mailbox_id=second_id,
        content=second_content,
        source_reference="second-channel-expired-body",
    )
    scoped_first = client.get(f"/v1/mailboxes/{first_id}/retention")
    scoped_second = client.get(f"/v1/mailboxes/{second_id}/retention")
    assert scoped_first.status_code == 200, scoped_first.text
    assert scoped_second.status_code == 200, scoped_second.text
    assert scoped_first.json()["cache_bytes"] == len(first_content)
    assert scoped_second.json()["cache_bytes"] == len(second_content)

    first_preview = client.post(f"/v1/mailboxes/{first_id}/retention/preview")
    second_preview = client.post(f"/v1/mailboxes/{second_id}/retention/preview")
    assert first_preview.status_code == 200, first_preview.text
    assert second_preview.status_code == 200, second_preview.text
    assert first_preview.json()["expired_body_count"] == 1
    assert second_preview.json()["expired_body_count"] == 1

    first_cleanup = client.post(f"/v1/mailboxes/{first_id}/retention/cleanup")
    assert first_cleanup.status_code == 200, first_cleanup.text
    assert first_cleanup.json()["deleted_count"] == 1
    assert first_path.exists() is False
    assert second_path.exists() is True

    first_runs = client.get(f"/v1/mailboxes/{first_id}/retention/runs")
    second_runs = client.get(f"/v1/mailboxes/{second_id}/retention/runs")
    assert first_runs.status_code == 200, first_runs.text
    assert second_runs.status_code == 200, second_runs.text
    assert first_runs.json()["total"] == 1
    assert first_runs.json()["items"][0]["run_id"] == first_cleanup.json()["run_id"]
    assert second_runs.json() == {"items": [], "total": 0}

    # The compatibility routes are intentionally unable to choose between two
    # active channels. A 409 is safer than applying retention to whichever
    # mailbox happened to be created last.
    legacy_responses = (
        client.get("/v1/mailbox/retention"),
        client.put("/v1/mailbox/retention", json={"retention_policy": "audit"}),
        client.post("/v1/mailbox/retention/preview"),
        client.post("/v1/mailbox/retention/cleanup"),
        client.get("/v1/mailbox/retention/runs"),
    )
    for response in legacy_responses:
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "mailbox_legacy_endpoint_ambiguous"


@pytest.fixture
def tenant_clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
        allow_unauthenticated=False,
        session_secret="retention-multichannel-tenant-test-secret",
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
    current = client.get("/v1/auth/session")
    assert current.status_code == 200, current.text
    return current.json()


def _seed_workspace_mailbox(
    client: TestClient,
    *,
    organization_id: str,
    label: str,
) -> str:
    database = client.app.state.database
    with database.session_factory() as session:
        set_organization_context(session, organization_id)
        config = MailboxConfig(
            display_name=label,
            display_name_key=label.casefold(),
            imap_host="imap.workspace-retention.test",
            imap_port=993,
            email_address="workspace-mailbox@example.test",
            mailbox="INBOX",
            encrypted_password="retention-test-ciphertext",
            enabled=True,
            retention_policy="standard",
        )
        session.add(config)
        session.flush()
        config_id = config.id
        session.commit()
    return config_id


def test_named_retention_endpoints_hide_foreign_mailbox_ids(
    tenant_clients: tuple[TestClient, TestClient],
) -> None:
    client_a, client_b = tenant_clients
    _register_and_login(
        client_a,
        organization_name="Retention Alpha",
        full_name="Alpha Admin",
        email="retention-alpha@example.test",
        password="tenant-test-password-a",
    )
    session_b = _register_and_login(
        client_b,
        organization_name="Retention Beta",
        full_name="Beta Admin",
        email="retention-beta@example.test",
        password="tenant-test-password-b",
    )
    foreign_mailbox_id = _seed_workspace_mailbox(
        client_b,
        organization_id=str(session_b["organization"]["organization_id"]),
        label="Beta 通道",
    )

    responses = (
        client_a.get(f"/v1/mailboxes/{foreign_mailbox_id}/retention"),
        client_a.put(
            f"/v1/mailboxes/{foreign_mailbox_id}/retention",
            json={"retention_policy": "audit"},
        ),
        client_a.post(f"/v1/mailboxes/{foreign_mailbox_id}/retention/preview"),
        client_a.post(f"/v1/mailboxes/{foreign_mailbox_id}/retention/cleanup"),
        client_a.get(f"/v1/mailboxes/{foreign_mailbox_id}/retention/runs"),
    )
    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "mailbox_config_not_found"


def test_archived_mailbox_cache_remains_eligible_for_explicit_and_due_cleanup(client) -> None:
    """Archiving stops intake; it must not leave transient cache files forever."""

    explicit = _create_mailbox(client, label="已归档手动清理", host="imap.explicit.test")
    scheduled = _create_mailbox(client, label="已归档定时清理", host="imap.scheduled.test")
    explicit_id = str(explicit["mailbox_id"])
    scheduled_id = str(scheduled["mailbox_id"])
    explicit_path = _seed_expired_body_copy(
        client,
        mailbox_id=explicit_id,
        content=b"expired content for explicit archive cleanup",
        source_reference="archived-explicit-expired-body",
    )
    scheduled_path = _seed_expired_body_copy(
        client,
        mailbox_id=scheduled_id,
        content=b"expired content for scheduled archive cleanup",
        source_reference="archived-scheduled-expired-body",
    )

    for mailbox_id in (explicit_id, scheduled_id):
        archived = client.post(f"/v1/mailboxes/{mailbox_id}/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived_at"] is not None

    explicit_cleanup = client.post(
        f"/v1/mailboxes/{explicit_id}/retention/cleanup"
    )
    assert explicit_cleanup.status_code == 200, explicit_cleanup.text
    assert explicit_cleanup.json()["deleted_count"] == 1
    assert explicit_path.exists() is False
    assert scheduled_path.exists() is True

    # A scheduled worker scans due configurations independently of whether
    # they still accept new email. The archived second channel is still the
    # next due item after the explicit cleanup above.
    assert cleanup_due_mailbox_retention(
        database=client.app.state.database,
        settings=client.app.state.settings,
    ) is True
    assert scheduled_path.exists() is False
