from __future__ import annotations

import argparse
import os
import socket
import time
from uuid import uuid4

from app.config import AppSettings
from app.database import Database
from app.services.ai_extraction_job_service import run_ai_extraction_worker_once
from app.services.resume_summary_job_service import run_resume_summary_worker_once
from app.services.document_extraction_job_service import (
    run_document_extraction_worker_once,
)
from app.services.job_match_batch_service import run_job_match_batch_worker_once
from app.services.mailbox_background_job_service import (
    enqueue_due_mailbox_sync_jobs,
    run_mailbox_background_job_worker_once,
)
from app.services.mailbox_retention_service import cleanup_due_mailbox_retention
from app.services.candidate_data_lifecycle_service import (
    run_due_candidate_data_retention_cleanup,
)
from app.services.candidate_data_purge_service import (
    run_candidate_data_purge_worker_once,
)
from app.services.candidate_data_export_service import (
    cleanup_expired_candidate_data_exports,
    run_candidate_data_export_worker_once,
)
from app.services.resume_score_batch_service import run_resume_score_batch_worker_once
from app.services.recruiting_agent_service import (
    purge_expired_recruiting_agent_conversations,
)
from app.services.transactional_email_outbox_service import (
    run_transactional_email_outbox_worker_once,
)
from app.services.institution_service import (
    is_institution_registry_seeded,
    seed_institution_registry,
)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


def _create_worker_database(settings: AppSettings) -> Database:
    settings.validate_runtime()
    settings.ensure_directories()
    database = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )
    if settings.auto_create_schema:
        database.create_all()
    with database.session_factory() as session:
        if settings.seed_registry_on_startup:
            seed_institution_registry(session)
            session.commit()
        elif not is_institution_registry_seeded(session):
            raise RuntimeError("institution_registry_not_seeded")
    return database


def run_forever(settings: AppSettings) -> None:
    database = _create_worker_database(settings)
    worker_id = _worker_id()
    try:
        while True:
            ran_transactional_email = run_transactional_email_outbox_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_mailbox_job = run_mailbox_background_job_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_document_extraction = run_document_extraction_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_extraction = run_ai_extraction_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_summary = run_resume_summary_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_job_match = run_job_match_batch_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_score_batch = run_resume_score_batch_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            queued_due_mailbox_sync = enqueue_due_mailbox_sync_jobs(
                database=database,
                settings=settings,
            )
            ran_mailbox_retention_cleanup = cleanup_due_mailbox_retention(
                database=database,
                settings=settings,
            )
            ran_candidate_data_retention_cleanup = (
                run_due_candidate_data_retention_cleanup(
                    database,
                    settings=settings,
                )
            )
            ran_candidate_data_purge = run_candidate_data_purge_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            ran_candidate_data_export = run_candidate_data_export_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            cleaned_candidate_data_exports = cleanup_expired_candidate_data_exports(
                database,
                settings=settings,
            )
            purged_recruiting_agent_contexts = (
                purge_expired_recruiting_agent_conversations(database)
            )
            if (
                not ran_extraction
                and not ran_summary
                and not ran_document_extraction
                and not ran_job_match
                and not ran_score_batch
                and not ran_transactional_email
                and not ran_mailbox_job
                and not queued_due_mailbox_sync
                and not ran_mailbox_retention_cleanup
                and not ran_candidate_data_retention_cleanup
                and not ran_candidate_data_purge
                and not ran_candidate_data_export
                and not cleaned_candidate_data_exports
                and not purged_recruiting_agent_contexts
            ):
                time.sleep(settings.ai_extraction_worker_poll_seconds)
    finally:
        database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run durable AI resume extraction jobs."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim and process at most one job, then exit.",
    )
    args = parser.parse_args()
    settings = AppSettings.from_env()
    if not args.once:
        run_forever(settings)
        return

    database = _create_worker_database(settings)
    try:
        ran_transactional_email = run_transactional_email_outbox_worker_once(
            database,
            settings=settings,
            worker_id=_worker_id(),
        )
        ran_mailbox_job = run_mailbox_background_job_worker_once(
            database,
            settings=settings,
            worker_id=_worker_id(),
        )
        if not ran_transactional_email and not ran_mailbox_job:
            ran_document_extraction = run_document_extraction_worker_once(
                database,
                settings=settings,
                worker_id=_worker_id(),
            )
            if not ran_document_extraction:
                ran_extraction = run_ai_extraction_worker_once(
                    database,
                    settings=settings,
                    worker_id=_worker_id(),
                )
            else:
                ran_extraction = True
        else:
            ran_extraction = True
        ran_summary = False
        if not ran_extraction:
            ran_summary = run_resume_summary_worker_once(
                database,
                settings=settings,
                worker_id=_worker_id(),
            )
        if not ran_extraction and not ran_summary:
            ran_job_match = run_job_match_batch_worker_once(
                database,
                settings=settings,
                worker_id=_worker_id(),
            )
            if not ran_job_match:
                run_resume_score_batch_worker_once(
                    database,
                    settings=settings,
                    worker_id=_worker_id(),
                )
        enqueue_due_mailbox_sync_jobs(database=database, settings=settings)
        cleanup_due_mailbox_retention(database=database, settings=settings)
        run_due_candidate_data_retention_cleanup(database, settings=settings)
        run_candidate_data_purge_worker_once(
            database,
            settings=settings,
            worker_id=_worker_id(),
        )
        run_candidate_data_export_worker_once(
            database,
            settings=settings,
            worker_id=_worker_id(),
        )
        cleanup_expired_candidate_data_exports(database, settings=settings)
        purge_expired_recruiting_agent_conversations(database)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
