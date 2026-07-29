from __future__ import annotations

import argparse
import logging
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
from app.services.workspace_feedback_service import (
    run_workspace_feedback_reward_worker_once,
)
from app.services.institution_service import (
    is_institution_registry_seeded,
    seed_institution_registry,
)
from app.services.runtime_observability_service import (
    mark_worker_stopped,
    record_worker_heartbeat,
)


_WORKER_HEARTBEAT_INTERVAL_SECONDS = 30.0
_SAFE_WORKER_LIFECYCLE_EVENTS = frozenset(
    {
        "worker_started",
        "worker_cycle_completed",
        "worker_stopped",
        "worker_cycle_failed",
    }
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


def _log_worker_lifecycle_event(
    event: str,
) -> None:
    """Emit a fixed, content-free worker lifecycle event.

    ``app.observability`` is provided by the request-correlation/logging
    baseline. The import stays local so this worker module remains importable
    while that independently-reviewed baseline is being merged. If an older
    image is temporarily running without that module, heartbeat processing
    continues rather than failing all background work; the combined release
    emits the event through the strict JSON logger.
    """

    try:
        from app.observability import configure_observability_logging, log_event
    except ModuleNotFoundError as exc:
        if exc.name == "app.observability":
            return
        raise

    if event not in _SAFE_WORKER_LIFECYCLE_EVENTS:
        return
    # The worker process does not construct the FastAPI application, so it
    # must install the same isolated stdout handler before emitting its own
    # events. The setup is idempotent and never changes legacy loggers.
    configure_observability_logging()
    if event == "worker_cycle_failed":
        log_event(event, level=logging.ERROR, error_code="worker_cycle_failed")
        return
    log_event(event)


def _touch_worker_heartbeat(
    database: Database,
    *,
    worker_id: str,
    last_heartbeat_monotonic: float,
    force: bool = False,
    cycle_completed: bool = False,
    clear_last_error: bool = False,
) -> tuple[float, bool]:
    """Persist a bounded-rate, best-effort liveness signal.

    The durable worker queues remain the source of truth for business work.
    This intentionally writes at most once per short interval so an idle
    worker does not create unnecessary database load just to prove it is live.
    """

    current_monotonic = time.monotonic()
    if (
        not force
        and current_monotonic - last_heartbeat_monotonic
        < _WORKER_HEARTBEAT_INTERVAL_SECONDS
    ):
        return last_heartbeat_monotonic, False
    record_worker_heartbeat(
        database,
        worker_id=worker_id,
        cycle_completed=cycle_completed,
        clear_last_error=clear_last_error,
    )
    # A failed observability write is still throttled. It must not turn a
    # temporary missing migration or database issue into a write storm.
    return current_monotonic, True


def _touch_worker_task_boundary(
    database: Database,
    *,
    worker_id: str,
    last_heartbeat_monotonic: float,
) -> float:
    """Refresh liveness after one durable task boundary when due.

    A worker cycle deliberately visits several independent queues.  A document
    conversion, IMAP sync, or model call can take long enough that waiting
    until the *whole* cycle finishes would make a healthy process look stale.
    The underlying touch remains rate-limited, so fast queue checks do not
    turn the heartbeat into a database write per task.
    """

    updated_monotonic, _ = _touch_worker_heartbeat(
        database,
        worker_id=worker_id,
        last_heartbeat_monotonic=last_heartbeat_monotonic,
    )
    return updated_monotonic


def run_forever(settings: AppSettings) -> None:
    database = _create_worker_database(settings)
    worker_id = _worker_id()
    last_heartbeat_monotonic, _ = _touch_worker_heartbeat(
        database,
        worker_id=worker_id,
        last_heartbeat_monotonic=0.0,
        force=True,
    )
    _log_worker_lifecycle_event("worker_started")
    worker_failed = False
    try:
        while True:
            last_heartbeat_monotonic, _ = _touch_worker_heartbeat(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_workspace_feedback_reward = run_workspace_feedback_reward_worker_once(
                database,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_transactional_email = run_transactional_email_outbox_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_mailbox_job = run_mailbox_background_job_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_document_extraction = run_document_extraction_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_extraction = run_ai_extraction_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_summary = run_resume_summary_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_job_match = run_job_match_batch_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_score_batch = run_resume_score_batch_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            queued_due_mailbox_sync = enqueue_due_mailbox_sync_jobs(
                database=database,
                settings=settings,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_mailbox_retention_cleanup = cleanup_due_mailbox_retention(
                database=database,
                settings=settings,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_candidate_data_retention_cleanup = (
                run_due_candidate_data_retention_cleanup(
                    database,
                    settings=settings,
                )
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_candidate_data_purge = run_candidate_data_purge_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            ran_candidate_data_export = run_candidate_data_export_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            cleaned_candidate_data_exports = cleanup_expired_candidate_data_exports(
                database,
                settings=settings,
            )
            last_heartbeat_monotonic = _touch_worker_task_boundary(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
            )
            purged_recruiting_agent_contexts = (
                purge_expired_recruiting_agent_conversations(database)
            )
            last_heartbeat_monotonic, heartbeat_recorded = _touch_worker_heartbeat(
                database,
                worker_id=worker_id,
                last_heartbeat_monotonic=last_heartbeat_monotonic,
                cycle_completed=True,
                clear_last_error=True,
            )
            if heartbeat_recorded:
                _log_worker_lifecycle_event("worker_cycle_completed")
            if (
                not ran_extraction
                and not ran_summary
                and not ran_document_extraction
                and not ran_job_match
                and not ran_score_batch
                and not ran_workspace_feedback_reward
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
    except Exception:
        worker_failed = True
        mark_worker_stopped(
            database,
            worker_id=worker_id,
            last_error_code="worker_cycle_failed",
        )
        _log_worker_lifecycle_event("worker_cycle_failed")
        raise
    finally:
        if not worker_failed:
            mark_worker_stopped(database, worker_id=worker_id)
            _log_worker_lifecycle_event("worker_stopped")
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
    worker_id = _worker_id()
    _, _ = _touch_worker_heartbeat(
        database,
        worker_id=worker_id,
        last_heartbeat_monotonic=0.0,
        force=True,
    )
    _log_worker_lifecycle_event("worker_started")
    worker_failed = False
    try:
        run_workspace_feedback_reward_worker_once(
            database,
            worker_id=worker_id,
        )
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
        if not ran_transactional_email and not ran_mailbox_job:
            ran_document_extraction = run_document_extraction_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            if not ran_document_extraction:
                ran_extraction = run_ai_extraction_worker_once(
                    database,
                    settings=settings,
                    worker_id=worker_id,
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
                worker_id=worker_id,
            )
        if not ran_extraction and not ran_summary:
            ran_job_match = run_job_match_batch_worker_once(
                database,
                settings=settings,
                worker_id=worker_id,
            )
            if not ran_job_match:
                run_resume_score_batch_worker_once(
                    database,
                    settings=settings,
                    worker_id=worker_id,
                )
        enqueue_due_mailbox_sync_jobs(database=database, settings=settings)
        cleanup_due_mailbox_retention(database=database, settings=settings)
        run_due_candidate_data_retention_cleanup(database, settings=settings)
        run_candidate_data_purge_worker_once(
            database,
            settings=settings,
            worker_id=worker_id,
        )
        run_candidate_data_export_worker_once(
            database,
            settings=settings,
            worker_id=worker_id,
        )
        cleanup_expired_candidate_data_exports(database, settings=settings)
        purge_expired_recruiting_agent_conversations(database)
        _, _ = _touch_worker_heartbeat(
            database,
            worker_id=worker_id,
            last_heartbeat_monotonic=0.0,
            force=True,
            cycle_completed=True,
            clear_last_error=True,
        )
        _log_worker_lifecycle_event("worker_cycle_completed")
    except Exception:
        worker_failed = True
        mark_worker_stopped(
            database,
            worker_id=worker_id,
            last_error_code="worker_cycle_failed",
        )
        _log_worker_lifecycle_event("worker_cycle_failed")
        raise
    finally:
        if not worker_failed:
            mark_worker_stopped(database, worker_id=worker_id)
            _log_worker_lifecycle_event("worker_stopped")
        database.dispose()


if __name__ == "__main__":
    main()
