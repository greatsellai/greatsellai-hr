from __future__ import annotations

import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.config import AppSettings
from app.database import Base
from app.models import (
    EmailAttachmentImport,
    EmailAttachmentImportAttempt,
    MailboxConfig,
    Organization,
)
from app.services import mailbox_import_service
from app.tenant_scope import set_organization_context


_POSTGRES_TEST_URL = os.getenv("MAILBOX_DEDUP_POSTGRES_TEST_URL")


@pytest.mark.skipif(
    not _POSTGRES_TEST_URL,
    reason="set MAILBOX_DEDUP_POSTGRES_TEST_URL to a disposable PostgreSQL database",
)
def test_owner_cannot_finish_before_the_waiter_audit_is_committed() -> None:
    """Exercise the exact owner/waiter race against PostgreSQL row locking.

    Each run creates a private schema, so it can share a disposable CI database
    without reading or altering another test's rows.
    """

    assert _POSTGRES_TEST_URL is not None
    settings = AppSettings(
        project_dir=Path.cwd(),
        data_dir=Path.cwd(),
        upload_dir=Path.cwd() / "uploads",
        database_url=_POSTGRES_TEST_URL,
        candidate_data_tombstone_secret="postgres-lifecycle-test-secret",
    )
    schema_name = f"mailbox_dedup_{uuid4().hex}"
    admin_engine = create_engine(_POSTGRES_TEST_URL, pool_pre_ping=True)
    test_engine = None
    waiter_session = None
    owner_thread: threading.Thread | None = None
    owner_errors: list[BaseException] = []
    owner_reached_identity_update = threading.Event()
    owner_finished = threading.Event()

    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')

        test_engine = create_engine(
            _POSTGRES_TEST_URL,
            connect_args={"options": f"-csearch_path={schema_name}"},
            pool_pre_ping=True,
        )
        Base.metadata.create_all(test_engine)
        session_factory = sessionmaker(
            bind=test_engine,
            autoflush=False,
            expire_on_commit=False,
        )

        organization_id = str(uuid4())
        digest = "a" * 64
        with session_factory() as setup_session:
            setup_session.add(Organization(id=organization_id, name="Mailbox race test"))
            setup_session.flush()
            set_organization_context(setup_session, organization_id)
            config = MailboxConfig(
                display_name="Race",
                display_name_key="race",
                imap_host="imap.race.test",
                imap_port=993,
                email_address="race@example.test",
                mailbox="INBOX",
                encrypted_password="not-used-by-this-test",
                enabled=True,
            )
            setup_session.add(config)
            setup_session.flush()
            owner = mailbox_import_service._record(
                setup_session,
                config=config,
                uid="1",
                message_id="<owner@example.test>",
                filename="owner.pdf",
                attachment_sha256=digest,
                status="processing",
                error=None,
                resume_id=None,
                received_at=None,
                source_uidvalidity=1,
                attempt_completed=False,
            )
            owner_claim = mailbox_import_service._claim_attachment_content(
                setup_session,
                record=owner,
                settings=settings,
            )
            assert owner_claim.outcome == "owner"
            setup_session.commit()
            config_id = config.id
            owner_id = owner.id

        waiter_session = session_factory()
        set_organization_context(waiter_session, organization_id)
        waiter_config = waiter_session.get(MailboxConfig, config_id)
        assert waiter_config is not None
        waiter = mailbox_import_service._record(
            waiter_session,
            config=waiter_config,
            uid="2",
            message_id="<waiter@example.test>",
            filename="forwarded.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=1,
            attempt_completed=False,
        )
        waiter_claim = mailbox_import_service._claim_attachment_content(
            waiter_session,
            record=waiter,
            settings=settings,
        )
        assert waiter_claim.outcome == "waiting"
        waiter_id = waiter.id

        @event.listens_for(test_engine, "before_cursor_execute")
        def observe_identity_update(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ) -> None:
            if statement.lstrip().upper().startswith(
                "UPDATE MAILBOX_ATTACHMENT_CONTENT_IDENTITIES"
            ):
                owner_reached_identity_update.set()

        def finish_owner() -> None:
            try:
                with session_factory() as owner_session:
                    set_organization_context(owner_session, organization_id)
                    stored_owner = owner_session.get(EmailAttachmentImport, owner_id)
                    assert stored_owner is not None
                    mailbox_import_service._complete_processing_import(
                        owner_session,
                        record=stored_owner,
                        claim=owner_claim,
                        status="failed",
                        error="attachment_import_failed",
                        resume_id=None,
                    )
            except BaseException as exc:  # surfaced in the test thread below
                owner_errors.append(exc)
            finally:
                owner_finished.set()

        owner_thread = threading.Thread(target=finish_owner, daemon=True)
        owner_thread.start()
        assert owner_reached_identity_update.wait(timeout=5)
        assert not owner_finished.wait(timeout=0.25)

        # Committing this waiter releases the identity lock. The owner can now
        # publish its failure and must see and resolve this committed audit row.
        waiting_result = mailbox_import_service._complete_non_owner_processing_import(
            waiter_session,
            record=waiter,
            claim=waiter_claim,
        )
        assert waiting_result.status == "deduplicating"
        assert owner_finished.wait(timeout=5)
        owner_thread.join(timeout=1)
        assert not owner_errors

        waiter_session.expire_all()
        stored_waiter = waiter_session.get(EmailAttachmentImport, waiter_id)
        waiter_attempt = waiter_session.scalar(
            select(EmailAttachmentImportAttempt).where(
                EmailAttachmentImportAttempt.email_attachment_import_id == waiter_id,
            )
        )
        assert stored_waiter is not None
        assert stored_waiter.status == "failed"
        assert stored_waiter.error == "attachment_import_failed"
        assert waiter_attempt is not None
        assert waiter_attempt.status == "failed"
        assert waiter_attempt.completed_at is not None
    finally:
        if waiter_session is not None:
            waiter_session.rollback()
            waiter_session.close()
        if owner_thread is not None and owner_thread.is_alive():
            owner_thread.join(timeout=5)
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        admin_engine.dispose()
