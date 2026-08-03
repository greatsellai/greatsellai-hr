"""Minimal, privacy-safe runtime observability for the platform console.

This module deliberately reports aggregate operational state rather than
business records.  It never returns a task ID, workspace ID, account ID,
candidate, filename, source text, mailbox content, model prompt/output, or
raw exception/provider response.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import Database
from app.models import (
    AiRun,
    DocumentExtractionOcrDailyMetric,
    JobMatchBatchItem,
    MailboxBackgroundJob,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
    ResumeScoreBatchItem,
    ResumeSummaryJob,
    RuntimeWorkerHeartbeat,
    TransactionalEmailOutbox,
    WorkspaceFeedbackSubmission,
)
from app.schemas import (
    PlatformRuntimeFailureResponse,
    PlatformRuntimeOcrUsageResponse,
    PlatformRuntimeOverviewResponse,
    PlatformRuntimeQueueResponse,
    PlatformRuntimeWorkerResponse,
)


# A worker can be inside an OCR/office/LLM call for a few minutes.  The stale
# window is therefore intentionally longer than the ordinary 30-second touch
# interval, avoiding a false incident during a valid long-running task.
WORKER_HEARTBEAT_STALE_AFTER_SECONDS = 300
WORKER_HEARTBEAT_RETENTION_WINDOW = timedelta(days=1)
RUNTIME_RECENT_FAILURE_LIMIT = 20

_SAFE_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DEFAULT_TASK_FAILURE_CODE = "worker_task_failed"
_SAFE_WORKER_KINDS = frozenset({"background"})
# The runtime endpoint is intentionally stricter than a generic application
# logger. A stored queue ``last_error`` can originate from a third-party
# library, so syntactic validation alone would permit a crafted identifier
# such as ``john_doe``. Only reviewed, fixed operational labels may cross the
# platform HTTP boundary; all other values collapse to the generic fallback.
_SAFE_RUNTIME_ERROR_CODES = frozenset(
    {
        "worker_cycle_failed",
        "worker_task_failed",
        "ai_provider_auth",
        "ai_provider_timeout",
        "ai_provider_rate_limited",
        "ai_provider_unavailable",
        "ai_provider_invalid_response",
        "deepseek_api_key_not_configured",
        "deepseek_timeout",
        "deepseek_empty_structured_facts",
        "deepseek_invalid_structured_response",
        "deepseek_tool_call_missing",
        "deepseek_arguments_missing",
        "deepseek_response_truncated",
        "ai_route_not_configured",
        "ai_route_disabled",
        "ai_route_not_published",
        "ai_pinned_route_not_available",
        "document_extraction_worker_lease_expired",
        "document_extraction_failed",
        "document_text_extraction_failed",
        "document_conversion_timed_out",
        "tencent_ocr_not_configured",
        "tencent_ocr_request_failed",
        "tencent_ocr_auth_failed",
        "tencent_ocr_request_invalid",
        "tencent_ocr_rate_limited",
        "tencent_ocr_invalid_response",
        "tencent_ocr_image_open_failed",
        "tencent_ocr_invalid_image",
        "tencent_ocr_image_prepare_failed",
        "tencent_ocr_image_too_large",
        "tencent_ocr_image_dimensions_too_large",
        "tencent_ocr_invalid_page",
        "tencent_ocr_page_render_failed",
        "resume_not_found",
        "resume_source_text_unavailable",
        "resume_source_text_unreliable",
        "mailbox_connection_failed",
        "mailbox_background_job_failed",
        "mailbox_task_source_changed",
        "mailbox_authorization_invalid",
        "mailbox_sync_failed",
        "job_match_worker_lease_expired",
        "resume_no_longer_ready_for_job_match",
        "resume_score_worker_lease_expired",
        "resume_no_longer_ready_for_scoring",
        "email_delivery_provider_failed",
        "transactional_email_failed",
        "workspace_feedback_reward_failed",
    }
)


class RuntimeReadinessError(RuntimeError):
    """A stable readiness failure safe to map to an HTTP 503."""


@dataclass(frozen=True)
class _QueueSpec:
    queue_key: str
    model: type[Any]
    status_column: Any
    queued_statuses: tuple[str, ...]
    running_statuses: tuple[str, ...]
    failed_statuses: tuple[str, ...]
    pending_timestamp_column: Any
    failure_timestamp_column: Any
    error_column: Any
    attempt_count_column: Any | None
    # The feedback reward intentionally retries from a queued/running state
    # rather than creating a terminal failed state.  Its safe error code is
    # still important for a platform operator.
    failure_requires_error: bool = False


_QUEUE_SPECS: tuple[_QueueSpec, ...] = (
    _QueueSpec(
        queue_key="document_extraction",
        model=ResumeDocumentExtractionJob,
        status_column=ResumeDocumentExtractionJob.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("needs_attention",),
        pending_timestamp_column=ResumeDocumentExtractionJob.requested_at,
        failure_timestamp_column=ResumeDocumentExtractionJob.updated_at,
        error_column=ResumeDocumentExtractionJob.last_error,
        attempt_count_column=ResumeDocumentExtractionJob.attempt_count,
    ),
    _QueueSpec(
        queue_key="ai_extraction",
        model=ResumeAiExtractionJob,
        status_column=ResumeAiExtractionJob.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("needs_attention", "unavailable"),
        pending_timestamp_column=ResumeAiExtractionJob.requested_at,
        failure_timestamp_column=ResumeAiExtractionJob.updated_at,
        error_column=ResumeAiExtractionJob.last_error,
        attempt_count_column=ResumeAiExtractionJob.attempt_count,
    ),
    _QueueSpec(
        queue_key="resume_summary",
        model=ResumeSummaryJob,
        status_column=ResumeSummaryJob.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("failed", "unavailable"),
        pending_timestamp_column=ResumeSummaryJob.requested_at,
        failure_timestamp_column=ResumeSummaryJob.updated_at,
        error_column=ResumeSummaryJob.last_error,
        attempt_count_column=ResumeSummaryJob.attempt_count,
    ),
    _QueueSpec(
        queue_key="mailbox_background",
        model=MailboxBackgroundJob,
        status_column=MailboxBackgroundJob.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("failed",),
        pending_timestamp_column=MailboxBackgroundJob.requested_at,
        failure_timestamp_column=MailboxBackgroundJob.updated_at,
        error_column=MailboxBackgroundJob.last_error,
        attempt_count_column=MailboxBackgroundJob.attempt_count,
    ),
    _QueueSpec(
        queue_key="jd_match_item",
        model=JobMatchBatchItem,
        status_column=JobMatchBatchItem.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("failed",),
        # Batch items do not retain a separate queued timestamp.  ``updated``
        # is the durable, safe proxy for the oldest unresolved item.
        pending_timestamp_column=JobMatchBatchItem.updated_at,
        failure_timestamp_column=JobMatchBatchItem.updated_at,
        error_column=JobMatchBatchItem.last_error,
        attempt_count_column=JobMatchBatchItem.attempt_count,
    ),
    _QueueSpec(
        queue_key="resume_score_item",
        model=ResumeScoreBatchItem,
        status_column=ResumeScoreBatchItem.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("failed",),
        pending_timestamp_column=ResumeScoreBatchItem.updated_at,
        failure_timestamp_column=ResumeScoreBatchItem.updated_at,
        error_column=ResumeScoreBatchItem.last_error,
        attempt_count_column=ResumeScoreBatchItem.attempt_count,
    ),
    _QueueSpec(
        queue_key="transactional_email",
        model=TransactionalEmailOutbox,
        status_column=TransactionalEmailOutbox.status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=("failed",),
        pending_timestamp_column=TransactionalEmailOutbox.requested_at,
        failure_timestamp_column=TransactionalEmailOutbox.updated_at,
        error_column=TransactionalEmailOutbox.last_error,
        attempt_count_column=TransactionalEmailOutbox.attempt_count,
    ),
    _QueueSpec(
        queue_key="workspace_feedback_reward",
        model=WorkspaceFeedbackSubmission,
        status_column=WorkspaceFeedbackSubmission.reward_status,
        queued_statuses=("queued",),
        running_statuses=("running",),
        failed_statuses=(),
        pending_timestamp_column=WorkspaceFeedbackSubmission.created_at,
        failure_timestamp_column=WorkspaceFeedbackSubmission.updated_at,
        error_column=WorkspaceFeedbackSubmission.reward_last_error,
        attempt_count_column=WorkspaceFeedbackSubmission.reward_attempt_count,
        failure_requires_error=True,
    ),
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_runtime_error_code(
    value: object,
    *,
    fallback: str = _DEFAULT_TASK_FAILURE_CODE,
) -> str:
    """Return a safe operational code without ever echoing raw error text."""

    if isinstance(value, str):
        normalized = value.strip().lower()
        if (
            _SAFE_ERROR_CODE_PATTERN.fullmatch(normalized)
            and normalized in _SAFE_RUNTIME_ERROR_CODES
        ):
            return normalized
    normalized_fallback = fallback.strip().lower()
    if (
        _SAFE_ERROR_CODE_PATTERN.fullmatch(normalized_fallback)
        and normalized_fallback in _SAFE_RUNTIME_ERROR_CODES
    ):
        return normalized_fallback
    return _DEFAULT_TASK_FAILURE_CODE


def check_database_ready(session: Session) -> None:
    """Raise a stable error if a request cannot execute a trivial DB query."""

    try:
        session.execute(select(1)).scalar_one()
    except SQLAlchemyError as exc:
        raise RuntimeReadinessError("database_unavailable") from exc


def record_worker_heartbeat(
    database: Database,
    *,
    worker_id: str,
    worker_kind: str = "background",
    status: str = "running",
    cycle_completed: bool = False,
    last_error_code: str | None = None,
    clear_last_error: bool = False,
    now: datetime | None = None,
) -> bool:
    """Persist one best-effort, content-free heartbeat.

    Observability must not be able to take down job processing.  A missing
    migration or transient database issue therefore returns ``False`` rather
    than raising into the worker loop; the ordinary task paths still retain
    their existing failure handling.
    """

    normalized_worker_id = worker_id.strip()
    normalized_worker_kind = worker_kind.strip()
    if (
        not normalized_worker_id
        or len(normalized_worker_id) > 160
        or normalized_worker_kind not in _SAFE_WORKER_KINDS
        or status not in {"running", "stopped"}
    ):
        return False

    observed_at = _as_utc(now) or _utcnow()
    normalized_error = (
        normalize_runtime_error_code(last_error_code)
        if last_error_code is not None
        else None
    )
    try:
        with database.session_factory() as session:
            heartbeat = session.get(RuntimeWorkerHeartbeat, normalized_worker_id)
            if heartbeat is None:
                heartbeat = RuntimeWorkerHeartbeat(
                    worker_id=normalized_worker_id,
                    worker_kind=normalized_worker_kind,
                    status=status,
                    started_at=observed_at,
                    last_seen_at=observed_at,
                    last_cycle_completed_at=(observed_at if cycle_completed else None),
                    last_error_code=normalized_error,
                )
                session.add(heartbeat)
            else:
                heartbeat.worker_kind = normalized_worker_kind
                heartbeat.status = status
                heartbeat.last_seen_at = observed_at
                if cycle_completed:
                    heartbeat.last_cycle_completed_at = observed_at
                if normalized_error is not None:
                    heartbeat.last_error_code = normalized_error
                elif clear_last_error:
                    heartbeat.last_error_code = None
            session.commit()
        return True
    except SQLAlchemyError:
        return False


def mark_worker_stopped(
    database: Database,
    *,
    worker_id: str,
    worker_kind: str = "background",
    last_error_code: str | None = None,
) -> bool:
    """Record an orderly worker exit without serializing an exception."""

    return record_worker_heartbeat(
        database,
        worker_id=worker_id,
        worker_kind=worker_kind,
        status="stopped",
        last_error_code=last_error_code,
    )


def _failure_predicate(spec: _QueueSpec) -> Any:
    if spec.failure_requires_error:
        pending_statuses = spec.queued_statuses + spec.running_statuses
        return and_(
            spec.status_column.in_(pending_statuses),
            spec.error_column.is_not(None),
        )
    return spec.status_column.in_(spec.failed_statuses)


def _queue_overview(
    session: Session,
    spec: _QueueSpec,
    *,
    global_statement: Callable[[Any], Any],
) -> PlatformRuntimeQueueResponse:
    pending_statuses = spec.queued_statuses + spec.running_statuses
    failure_predicate = _failure_predicate(spec)
    row = session.execute(
        global_statement(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (spec.status_column.in_(spec.queued_statuses), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("queued_count"),
                func.coalesce(
                    func.sum(
                        case(
                            (spec.status_column.in_(spec.running_statuses), 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("running_count"),
                func.coalesce(
                    func.sum(case((failure_predicate, 1), else_=0)),
                    0,
                ).label("failed_count"),
                func.min(
                    case(
                        (
                            spec.status_column.in_(pending_statuses),
                            spec.pending_timestamp_column,
                        ),
                        else_=None,
                    )
                ).label("oldest_pending_at"),
            ).select_from(spec.model)
        )
    ).one()
    return PlatformRuntimeQueueResponse(
        queue_key=spec.queue_key,
        queued_count=int(row.queued_count or 0),
        running_count=int(row.running_count or 0),
        failed_count=int(row.failed_count or 0),
        oldest_pending_at=_as_utc(row.oldest_pending_at),
    )


def _recent_queue_failures(
    session: Session,
    spec: _QueueSpec,
    *,
    limit: int,
    global_statement: Callable[[Any], Any],
) -> list[PlatformRuntimeFailureResponse]:
    failure_predicate = _failure_predicate(spec)
    attempt_column = spec.attempt_count_column
    statement = select(
        spec.error_column.label("error_code"),
        spec.failure_timestamp_column.label("occurred_at"),
    )
    if attempt_column is not None:
        statement = statement.add_columns(attempt_column.label("attempt_count"))
    statement = (
        statement.select_from(spec.model)
        .where(failure_predicate)
        .order_by(spec.failure_timestamp_column.desc())
        .limit(limit)
    )
    rows = session.execute(global_statement(statement)).all()
    failures: list[PlatformRuntimeFailureResponse] = []
    for row in rows:
        occurred_at = _as_utc(row.occurred_at)
        if occurred_at is None:
            continue
        failures.append(
            PlatformRuntimeFailureResponse(
                queue_key=spec.queue_key,
                error_code=normalize_runtime_error_code(row.error_code),
                occurred_at=occurred_at,
                attempt_count=(
                    int(row.attempt_count)
                    if attempt_column is not None and row.attempt_count is not None
                    else None
                ),
            )
        )
    return failures


def _recent_ai_run_failures(
    session: Session,
    *,
    limit: int,
    global_statement: Callable[[Any], Any],
) -> list[PlatformRuntimeFailureResponse]:
    rows = session.execute(
        global_statement(
            select(AiRun.failure_code, AiRun.finished_at, AiRun.created_at)
            .where(AiRun.status == "failed")
            .order_by(AiRun.finished_at.desc(), AiRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    failures: list[PlatformRuntimeFailureResponse] = []
    for row in rows:
        occurred_at = _as_utc(row.finished_at) or _as_utc(row.created_at)
        if occurred_at is None:
            continue
        failures.append(
            PlatformRuntimeFailureResponse(
                queue_key="ai_run",
                error_code=normalize_runtime_error_code(row.failure_code),
                occurred_at=occurred_at,
                attempt_count=None,
            )
        )
    return failures


def _worker_response(
    heartbeat: RuntimeWorkerHeartbeat,
    *,
    stale_before: datetime,
) -> PlatformRuntimeWorkerResponse:
    last_seen_at = _as_utc(heartbeat.last_seen_at) or stale_before
    if heartbeat.status == "stopped":
        liveness = "stopped"
    elif last_seen_at < stale_before:
        liveness = "stale"
    else:
        liveness = "live"
    return PlatformRuntimeWorkerResponse(
        worker_kind=(
            heartbeat.worker_kind
            if heartbeat.worker_kind in _SAFE_WORKER_KINDS
            else "unknown"
        ),
        liveness=liveness,
        started_at=_as_utc(heartbeat.started_at) or last_seen_at,
        last_seen_at=last_seen_at,
        last_cycle_completed_at=_as_utc(heartbeat.last_cycle_completed_at),
        last_error_code=(
            normalize_runtime_error_code(heartbeat.last_error_code)
            if heartbeat.last_error_code is not None
            else None
        ),
    )


def _aggregate_worker_liveness(
    workers: list[PlatformRuntimeWorkerResponse],
) -> str:
    states = {worker.liveness for worker in workers}
    if "live" in states:
        return "live"
    if "stale" in states:
        return "stale"
    if "stopped" in states:
        return "stopped"
    return "missing"


def _aggregate_worker_processes(
    workers: list[PlatformRuntimeWorkerResponse],
) -> list[PlatformRuntimeWorkerResponse]:
    """Collapse recent process heartbeats into one row per safe worker type.

    A clean restart deliberately creates a new opaque process ID.  Showing
    every retained process record would make the platform console duplicate a
    single "background worker" row and would make an orderly, recently-stopped
    predecessor look like an incident while its successor is healthy.  The
    overview is a health summary, not a process inventory, so retain the most
    useful current state for each type: live takes precedence over stale, then
    stopped; records of the same state are already ordered newest-first.
    """

    liveness_priority = {"live": 3, "stale": 2, "stopped": 1}
    by_kind: dict[str, PlatformRuntimeWorkerResponse] = {}
    for worker in workers:
        current = by_kind.get(worker.worker_kind)
        if current is None or liveness_priority[worker.liveness] > liveness_priority[
            current.liveness
        ]:
            by_kind[worker.worker_kind] = worker
    return list(by_kind.values())


def _ocr_usage_overview(session: Session) -> PlatformRuntimeOcrUsageResponse:
    """Return only all-workspace OCR counter totals for platform operators.

    ``DocumentExtractionOcrDailyMetric`` is already a daily aggregate with no
    tenant or document reference.  Do not join it to resume or job tables:
    that would weaken its purpose as a content-free operational signal.
    """

    row = session.execute(
        select(
            func.min(DocumentExtractionOcrDailyMetric.metric_date).label(
                "recorded_from"
            ),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.document_count), 0
            ).label("document_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.completed_document_count),
                0,
            ).label("completed_document_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.failed_document_count),
                0,
            ).label("failed_document_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.total_source_pages), 0
            ).label("total_source_pages"),
            func.coalesce(
                func.sum(
                    DocumentExtractionOcrDailyMetric.ocr_attempted_document_count
                ),
                0,
            ).label("ocr_attempted_document_count"),
            func.coalesce(
                func.sum(
                    DocumentExtractionOcrDailyMetric.ocr_successful_document_count
                ),
                0,
            ).label("ocr_successful_document_count"),
            func.coalesce(
                func.sum(
                    DocumentExtractionOcrDailyMetric.ocr_selected_document_count
                ),
                0,
            ).label("ocr_selected_document_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.ocr_attempted_page_count),
                0,
            ).label("ocr_attempted_page_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.ocr_successful_page_count),
                0,
            ).label("ocr_successful_page_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.ocr_selected_page_count),
                0,
            ).label("ocr_selected_page_count"),
            func.coalesce(
                func.sum(DocumentExtractionOcrDailyMetric.ocr_failed_page_count),
                0,
            ).label("ocr_failed_page_count"),
        )
    ).one()
    return PlatformRuntimeOcrUsageResponse(
        recorded_from=row.recorded_from,
        document_count=int(row.document_count or 0),
        completed_document_count=int(row.completed_document_count or 0),
        failed_document_count=int(row.failed_document_count or 0),
        total_source_pages=int(row.total_source_pages or 0),
        ocr_attempted_document_count=int(row.ocr_attempted_document_count or 0),
        ocr_successful_document_count=int(row.ocr_successful_document_count or 0),
        ocr_selected_document_count=int(row.ocr_selected_document_count or 0),
        ocr_attempted_page_count=int(row.ocr_attempted_page_count or 0),
        ocr_successful_page_count=int(row.ocr_successful_page_count or 0),
        ocr_selected_page_count=int(row.ocr_selected_page_count or 0),
        ocr_failed_page_count=int(row.ocr_failed_page_count or 0),
    )


def build_platform_runtime_overview(
    session: Session,
    *,
    global_statement: Callable[[Any], Any],
    configured_worker_concurrency: int = 1,
    now: datetime | None = None,
) -> PlatformRuntimeOverviewResponse:
    """Build aggregate diagnostics through an injected platform scope bypass.

    This module cannot opt a query out of workspace filtering by itself.  The
    platform-admin control plane provides ``global_statement`` explicitly,
    keeping the cross-workspace authority in one auditable service boundary.
    """

    generated_at = _as_utc(now) or _utcnow()
    stale_before = generated_at - timedelta(
        seconds=WORKER_HEARTBEAT_STALE_AFTER_SECONDS
    )
    retained_since = generated_at - WORKER_HEARTBEAT_RETENTION_WINDOW
    heartbeat_rows = session.scalars(
        select(RuntimeWorkerHeartbeat)
        .where(
            RuntimeWorkerHeartbeat.last_seen_at >= retained_since
        )
        .order_by(RuntimeWorkerHeartbeat.last_seen_at.desc())
        .limit(50)
    ).all()
    worker_processes = [
        _worker_response(heartbeat, stale_before=stale_before)
        for heartbeat in heartbeat_rows
    ]
    workers = _aggregate_worker_processes(worker_processes)
    queues = [
        _queue_overview(session, spec, global_statement=global_statement)
        for spec in _QUEUE_SPECS
    ]
    failures = [
        failure
        for spec in _QUEUE_SPECS
        for failure in _recent_queue_failures(
            session,
            spec,
            limit=RUNTIME_RECENT_FAILURE_LIMIT,
            global_statement=global_statement,
        )
    ]
    failures.extend(
        _recent_ai_run_failures(
            session,
            limit=RUNTIME_RECENT_FAILURE_LIMIT,
            global_statement=global_statement,
        )
    )
    failures.sort(key=lambda failure: failure.occurred_at, reverse=True)
    return PlatformRuntimeOverviewResponse(
        generated_at=generated_at,
        worker_stale_after_seconds=WORKER_HEARTBEAT_STALE_AFTER_SECONDS,
        worker_liveness=_aggregate_worker_liveness(workers),
        live_worker_process_count=sum(
            worker.liveness == "live" for worker in worker_processes
        ),
        configured_worker_concurrency=max(1, configured_worker_concurrency),
        workers=workers,
        queues=queues,
        recent_failures=failures[:RUNTIME_RECENT_FAILURE_LIMIT],
        ocr_usage=_ocr_usage_overview(session),
    )
