from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.models import EmailAttachmentImport, MailboxConfig, Resume
from app.services import mailbox_import_service
from test_resume_flow import create_candidate, make_pdf_with_text


def test_deleted_mailbox_attachment_is_not_reimported_but_manual_upload_remains_allowed(
    client,
) -> None:
    """A deletion tombstone applies only to automatic mailbox ingestion.

    The same attachment hash must not recreate a candidate when an IMAP worker
    later encounters a forwarded/retried copy.  A recruiter can still choose a
    fresh browser upload of those bytes, which is deliberately outside the
    mailbox anti-replay boundary.
    """

    content = make_pdf_with_text("Mailbox lifecycle test resume Python SQL " * 8)
    digest = hashlib.sha256(content).hexdigest()
    candidate_id = create_candidate(client)
    uploaded = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": ("mailbox-original.pdf", content, "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    resume_id = uploaded.json()["resume_id"]

    database = client.app.state.database
    encrypted_password = mailbox_import_service._fernet(
        client.app.state.settings,
    ).encrypt(b"test-authorization-code").decode("ascii")
    with database.session_factory() as session:
        config = MailboxConfig(
            display_name="Lifecycle replay mailbox",
            display_name_key="lifecycle replay mailbox",
            imap_host="imap.example.test",
            imap_port=993,
            email_address="recruiting@example.test",
            mailbox="INBOX",
            encrypted_password=encrypted_password,
            enabled=True,
        )
        session.add(config)
        session.flush()
        mailbox_import_service._record(
            session,
            config=config,
            uid="42",
            message_id="<lifecycle-original@example.test>",
            filename="mailbox-original.pdf",
            attachment_sha256=digest,
            status="imported",
            error=None,
            resume_id=resume_id,
            received_at=None,
            source_uidvalidity=9,
        )
        session.commit()
        config_id = config.id

    deleted = client.request(
        "DELETE",
        f"/v1/resumes/{resume_id}",
        json={"reason": "candidate_request"},
    )
    assert deleted.status_code == 202, deleted.text

    with database.session_factory() as session:
        config = session.get(MailboxConfig, config_id)
        assert config is not None
        replay = mailbox_import_service._record(
            session,
            config=config,
            uid="43",
            message_id="<lifecycle-forwarded@example.test>",
            filename="mailbox-forwarded.pdf",
            attachment_sha256=digest,
            status="processing",
            error=None,
            resume_id=None,
            received_at=None,
            source_uidvalidity=9,
            attempt_completed=False,
        )
        claim = mailbox_import_service._claim_attachment_content(
            session,
            record=replay,
            settings=client.app.state.settings,
        )
        assert claim.outcome == "deleted"
        completed = mailbox_import_service._complete_non_owner_processing_import(
            session,
            record=replay,
            claim=claim,
        )
        assert completed.status == "skipped"
        assert completed.error == "attachment_deleted_by_candidate_lifecycle"
        assert completed.resume_id is None
        assert session.scalar(
            select(Resume.id).where(Resume.sha256 == digest)
        ) is None
        replayed = session.get(EmailAttachmentImport, replay.id)
        assert replayed is not None
        assert replayed.status == "skipped"
        assert replayed.error == "attachment_deleted_by_candidate_lifecycle"

    manual_candidate_id = create_candidate(client)
    manual_upload = client.post(
        f"/v1/candidates/{manual_candidate_id}/resumes",
        files={"file": ("manual-reupload.pdf", content, "application/pdf")},
    )
    assert manual_upload.status_code == 200, manual_upload.text
    manual_resume_id = manual_upload.json()["resume_id"]
    assert manual_resume_id != resume_id

    with database.session_factory() as session:
        manual_resume = session.scalar(
            select(Resume).where(Resume.id == manual_resume_id)
        )
        assert manual_resume is not None
        assert manual_resume.sha256 == digest
