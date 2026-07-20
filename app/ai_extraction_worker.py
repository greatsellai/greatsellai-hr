from __future__ import annotations

import argparse
import os
import socket
import time
from uuid import uuid4

from app.config import AppSettings
from app.database import Database
from app.services.ai_extraction_job_service import run_ai_extraction_worker_once
from app.services.job_match_batch_service import run_job_match_batch_worker_once
from app.services.mailbox_import_service import sync_due_mailboxes
from app.services.mailbox_retention_service import cleanup_due_mailbox_retention
from app.services.resume_score_batch_service import run_resume_score_batch_worker_once
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
            ran_extraction = run_ai_extraction_worker_once(
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
            ran_mailbox_sync = sync_due_mailboxes(database=database, settings=settings)
            ran_mailbox_retention_cleanup = cleanup_due_mailbox_retention(
                database=database,
                settings=settings,
            )
            if (
                not ran_extraction
                and not ran_job_match
                and not ran_score_batch
                and not ran_mailbox_sync
                and not ran_mailbox_retention_cleanup
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
        ran_extraction = run_ai_extraction_worker_once(
            database,
            settings=settings,
            worker_id=_worker_id(),
        )
        if not ran_extraction:
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
        sync_due_mailboxes(database=database, settings=settings)
        cleanup_due_mailbox_retention(database=database, settings=settings)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
