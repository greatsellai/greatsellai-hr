from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import Resume, ResumeDocumentExtractionJob, ResumeSourceBlock
from app.services.document_text_extraction import (
    DocumentExtractionError,
    extract_document_text,
    validate_document_path_signature,
)
from app.services.tencent_ocr_provider import TencentOcrConfig
from app.tenant_scope import clear_organization_context, set_organization_context


logger = logging.getLogger(__name__)

DOCUMENT_EXTRACTION_QUEUED = "queued"
DOCUMENT_EXTRACTION_RUNNING = "running"
DOCUMENT_EXTRACTION_COMPLETED = "completed"
DOCUMENT_EXTRACTION_NEEDS_ATTENTION = "needs_attention"


class DocumentExtractionJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedDocumentExtractionJob:
    job_id: str
    organization_id: str
    resume_id: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Bind every worker read and write after claim to one workspace."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def document_extraction_state(resume: Resume) -> tuple[str, str | None]:
    """Return the current normalization state without exposing worker internals."""

    job = resume.document_extraction_job
    if job is not None:
        return job.status, job.last_error
    if resume.extraction_status in {"queued", "extracting"}:
        # Historical rows can exist between the schema migration and the
        # deployment of the new enqueue code. Treat them as pending rather
        # than exposing a misleading terminal error to recruiters.
        return DOCUMENT_EXTRACTION_QUEUED, None
    if resume.extraction_status == "failed":
        return DOCUMENT_EXTRACTION_NEEDS_ATTENTION, _first_quality_error(resume)
    return DOCUMENT_EXTRACTION_COMPLETED, None


def enqueue_uploaded_resume_document_extraction(
    session: Session,
    *,
    resume: Resume,
    settings: AppSettings,
) -> ResumeDocumentExtractionJob:
    """Create one persisted normalization job in the upload transaction."""

    existing = resume.document_extraction_job
    if existing is not None:
        return existing
    now = utcnow()
    job = ResumeDocumentExtractionJob(
        organization_id=resume.organization_id,
        resume_id=resume.id,
        status=DOCUMENT_EXTRACTION_QUEUED,
        attempt_count=0,
        max_attempts=settings.document_extraction_job_max_attempts,
        next_attempt_at=now,
        requested_at=now,
    )
    session.add(job)
    resume.document_extraction_job = job
    session.flush()
    return job


def request_resume_document_extraction(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Queue/requeue source normalization without parsing in the caller.

    This is the inactive-version counterpart to the upload path.  It is kept
    separate from ``enqueue_uploaded_resume_document_extraction`` because a
    terminal job may be deliberately retried after a parser/OCR upgrade.  An
    active or ready screening version remains immutable, and an in-flight AI
    job is never allowed to race a replacement source document.
    """

    resume = session.scalar(select(Resume).where(Resume.id == resume_id))
    if resume is None:
        raise DocumentExtractionJobError("resume_not_found")
    if resume.is_active or resume.extraction_status == "ready":
        raise DocumentExtractionJobError("active_resume_cannot_be_reparsed")
    ai_job = resume.ai_extraction_job
    if ai_job is not None and ai_job.status in {"queued", "running"}:
        raise DocumentExtractionJobError("resume_ai_extraction_already_running")

    job = resume.document_extraction_job
    if job is not None and job.status in {
        DOCUMENT_EXTRACTION_QUEUED,
        DOCUMENT_EXTRACTION_RUNNING,
    }:
        return resume

    now = utcnow()
    if job is None:
        job = ResumeDocumentExtractionJob(
            organization_id=resume.organization_id,
            resume_id=resume.id,
        )
        session.add(job)
        resume.document_extraction_job = job
    job.status = DOCUMENT_EXTRACTION_QUEUED
    job.attempt_count = 0
    job.max_attempts = settings.document_extraction_job_max_attempts
    job.next_attempt_at = now
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error = None
    job.requested_at = now
    job.started_at = None
    job.completed_at = None

    # A reparse produces a new evidence version.  Existing source blocks stay
    # transactionally intact until the worker has saved the replacement, but
    # no model job may consume them while normalization is pending.
    resume.facts_version += 1
    resume.extraction_status = DOCUMENT_EXTRACTION_QUEUED
    session.flush()
    return resume


def run_document_extraction_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one untrusted original-file parse job."""

    claimed = _claim_next_job(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_claimed_job(
        database,
        settings=settings,
        worker_id=worker_id,
        claimed=claimed,
    )
    return True


def _claim_next_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedDocumentExtractionJob | None:
    now = utcnow()
    with database.session_factory() as session:
        _recover_expired_leases(session, now=now)
        eligible = and_(
            ResumeDocumentExtractionJob.status == DOCUMENT_EXTRACTION_QUEUED,
            ResumeDocumentExtractionJob.attempt_count
            < ResumeDocumentExtractionJob.max_attempts,
            or_(
                ResumeDocumentExtractionJob.next_attempt_at.is_(None),
                ResumeDocumentExtractionJob.next_attempt_at <= now,
            ),
        )
        candidate = session.execute(
            select(
                ResumeDocumentExtractionJob.id,
                ResumeDocumentExtractionJob.organization_id,
                ResumeDocumentExtractionJob.resume_id,
            )
            .where(eligible)
            .order_by(
                ResumeDocumentExtractionJob.next_attempt_at.asc(),
                ResumeDocumentExtractionJob.requested_at.asc(),
                ResumeDocumentExtractionJob.id.asc(),
            )
            .limit(1)
            .execution_options(skip_organization_scope=True)
        ).one_or_none()
        if candidate is None:
            session.commit()
            return None

        job_id, organization_id, resume_id = candidate
        if not organization_id:
            session.execute(
                update(ResumeDocumentExtractionJob)
                .where(ResumeDocumentExtractionJob.id == job_id)
                .values(
                    status=DOCUMENT_EXTRACTION_NEEDS_ATTENTION,
                    next_attempt_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error="document_extraction_workspace_missing",
                    completed_at=now,
                )
                .execution_options(skip_organization_scope=True)
            )
            session.commit()
            return None

        lease_expires_at = now + timedelta(
            seconds=settings.document_extraction_job_lease_seconds
        )
        claim = session.execute(
            update(ResumeDocumentExtractionJob)
            .where(
                ResumeDocumentExtractionJob.id == job_id,
                ResumeDocumentExtractionJob.organization_id == organization_id,
                eligible,
            )
            .values(
                status=DOCUMENT_EXTRACTION_RUNNING,
                attempt_count=ResumeDocumentExtractionJob.attempt_count + 1,
                started_at=now,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                next_attempt_at=None,
                last_error=None,
            )
            .execution_options(skip_organization_scope=True)
        )
        if claim.rowcount != 1:
            session.rollback()
            return None
        # This is deliberately a narrow global update. The job identity and
        # workspace are both fenced, and all later file/database work opens a
        # scoped session. It provides a truthful API state while parsing runs.
        session.execute(
            update(Resume)
            .where(
                Resume.id == resume_id,
                Resume.organization_id == organization_id,
                Resume.extraction_status.in_({"queued", "extracting"}),
            )
            .values(extraction_status="extracting")
            .execution_options(skip_organization_scope=True)
        )
        session.commit()
        return ClaimedDocumentExtractionJob(
            job_id=job_id,
            organization_id=organization_id,
            resume_id=resume_id,
        )


def _recover_expired_leases(session: Session, *, now: datetime) -> None:
    expired = and_(
        ResumeDocumentExtractionJob.status == DOCUMENT_EXTRACTION_RUNNING,
        ResumeDocumentExtractionJob.lease_expires_at.is_not(None),
        ResumeDocumentExtractionJob.lease_expires_at <= now,
    )
    expired_jobs = session.execute(
        select(
            ResumeDocumentExtractionJob.id,
            ResumeDocumentExtractionJob.organization_id,
            ResumeDocumentExtractionJob.resume_id,
            ResumeDocumentExtractionJob.attempt_count,
            ResumeDocumentExtractionJob.max_attempts,
        )
        .where(expired)
        .execution_options(skip_organization_scope=True)
    ).all()
    for job_id, organization_id, resume_id, attempt_count, max_attempts in expired_jobs:
        retry = attempt_count < max_attempts
        updated = session.execute(
            update(ResumeDocumentExtractionJob)
            .where(
                ResumeDocumentExtractionJob.id == job_id,
                ResumeDocumentExtractionJob.organization_id == organization_id,
                expired,
            )
            .values(
                status=(
                    DOCUMENT_EXTRACTION_QUEUED
                    if retry
                    else DOCUMENT_EXTRACTION_NEEDS_ATTENTION
                ),
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=now if retry else None,
                last_error="document_extraction_worker_lease_expired",
                completed_at=None if retry else now,
            )
            .execution_options(skip_organization_scope=True)
        )
        if updated.rowcount != 1:
            continue
        # The job discovery must be global, but resume writes remain fenced
        # by both the claimed workspace and resume id. A malformed job can
        # therefore never move a foreign workspace's status.
        resume_filter = (
            Resume.id == resume_id,
            Resume.organization_id == organization_id,
            Resume.extraction_status == "extracting",
        )
        if retry:
            session.execute(
                update(Resume)
                .where(*resume_filter)
                .values(extraction_status="queued")
                .execution_options(skip_organization_scope=True)
            )
            continue
        failed_resume = session.execute(
            update(Resume)
            .where(*resume_filter)
            .values(
                source_page_count=0,
                parsed_page_count=0,
                extraction_status="failed",
                quality_flags=["document_extraction_worker_lease_expired"],
                parser_version="document-worker",
                raw_text=None,
            )
            .execution_options(skip_organization_scope=True)
        )
        if failed_resume.rowcount == 1:
            session.execute(
                delete(ResumeSourceBlock)
                .where(ResumeSourceBlock.resume_id == resume_id)
                .execution_options(skip_organization_scope=True)
            )


def _process_claimed_job(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedDocumentExtractionJob,
) -> None:
    try:
        path = _load_claimed_original(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
        )
        result = extract_document_text(
            path,
            min_text_chars_per_page=settings.min_text_chars_per_page,
            ocr_sparse_text_chars_per_page=settings.ocr_sparse_text_chars_per_page,
            tencent_ocr_config=_tencent_ocr_config(settings),
            max_pages=settings.document_max_pages,
            max_text_chars=settings.document_max_text_chars,
            max_archive_uncompressed_bytes=settings.document_max_archive_uncompressed_bytes,
            max_spreadsheet_sheets=settings.document_max_spreadsheet_sheets,
            max_spreadsheet_rows_per_sheet=settings.document_max_spreadsheet_rows_per_sheet,
            max_spreadsheet_cells=settings.document_max_spreadsheet_cells,
            office_timeout_seconds=settings.document_office_timeout_seconds,
            image_ocr_timeout_seconds=settings.document_image_ocr_timeout_seconds,
        )
    except DocumentExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=str(exc),
            retryable=False,
        )
        return
    except DocumentExtractionError as exc:
        error = str(exc)
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=error,
            retryable=_is_retryable_document_error(error),
        )
        return
    except Exception:  # pragma: no cover - defensive worker containment
        logger.exception("Unexpected document extraction worker failure")
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error="document_extraction_worker_error",
            retryable=True,
        )
        return

    try:
        _save_completed_document_extraction(
            database,
            settings=settings,
            worker_id=worker_id,
            claimed=claimed,
            result=result,
        )
    except DocumentExtractionJobError as exc:
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error=str(exc),
            retryable=False,
        )
    except Exception:  # pragma: no cover - defensive database containment
        logger.exception("Unable to persist document extraction result")
        _finish_failure(
            database,
            worker_id=worker_id,
            claimed=claimed,
            error="document_extraction_persist_failed",
            retryable=True,
        )


def _load_claimed_original(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedDocumentExtractionJob,
) -> Path:
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
            )
            if job is None:
                raise DocumentExtractionJobError("document_extraction_job_lease_lost")
            if job.resume_id != claimed.resume_id:
                raise DocumentExtractionJobError("document_extraction_workspace_mismatch")
            resume = session.get(Resume, claimed.resume_id)
            if resume is None or resume.organization_id != claimed.organization_id:
                # Do not distinguish a missing original from a malformed
                # cross-workspace reference. Either outcome must fail before
                # this worker opens a source file or learns tenant B data.
                raise DocumentExtractionJobError("resume_not_found")
            if resume.is_active or resume.extraction_status == "ready":
                raise DocumentExtractionJobError("resume_changed_before_document_extraction")
            try:
                # Imported here to keep the upload service and worker service
                # acyclic. It also means every filesystem path gets the same
                # tenant namespace and symlink checks as original downloads.
                from app.services.resume_service import resolve_uploaded_resume_path

                path = resolve_uploaded_resume_path(
                    settings,
                    storage_key=resume.storage_key,
                    organization_id=claimed.organization_id,
                )
            except Exception as exc:
                raise DocumentExtractionJobError(
                    "resume_original_file_not_found"
                ) from exc
            try:
                if path.stat().st_size > settings.max_upload_bytes:
                    raise DocumentExtractionJobError("document_original_file_too_large")
                actual_sha256 = _sha256_file(path)
            except OSError as exc:
                raise DocumentExtractionJobError("resume_original_file_not_found") from exc
            if actual_sha256 != resume.sha256:
                raise DocumentExtractionJobError("resume_original_hash_mismatch")
            try:
                validate_document_path_signature(
                    path=path,
                    filename=resume.original_filename,
                )
            except DocumentExtractionError as exc:
                raise DocumentExtractionJobError(str(exc)) from exc
            # No database transaction remains open while a converter, OCR or
            # archive reader runs.
            session.rollback()
            return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tencent_ocr_config(settings: AppSettings) -> TencentOcrConfig | None:
    if not settings.tencent_secret_id or not settings.tencent_secret_key:
        return None
    return TencentOcrConfig(
        secret_id=settings.tencent_secret_id,
        secret_key=settings.tencent_secret_key,
        region=settings.tencent_ocr_region,
        timeout_seconds=settings.tencent_ocr_timeout_seconds,
    )


def _save_completed_document_extraction(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
    claimed: ClaimedDocumentExtractionJob,
    result: object,
) -> None:
    # Keep the result duck-typed so a test can supply the existing immutable
    # PdfExtractionResult without a second transport schema.
    source_page_count = int(getattr(result, "source_page_count"))
    parsed_page_count = int(getattr(result, "parsed_page_count"))
    pages = list(getattr(result, "pages"))
    raw_text = str(getattr(result, "raw_text"))
    quality_flags = list(getattr(result, "quality_flags"))
    parser_version = str(getattr(result, "parser_version"))
    extraction_status = str(getattr(result, "status"))

    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None:
                raise DocumentExtractionJobError("document_extraction_job_lease_lost")
            resume = session.scalar(
                select(Resume)
                .where(Resume.id == claimed.resume_id)
                .with_for_update()
            )
            if resume is None:
                raise DocumentExtractionJobError("resume_not_found")
            if resume.organization_id != claimed.organization_id or job.resume_id != resume.id:
                raise DocumentExtractionJobError("document_extraction_workspace_mismatch")
            if resume.is_active or resume.extraction_status == "ready":
                raise DocumentExtractionJobError("resume_changed_before_document_extraction")

            session.execute(
                delete(ResumeSourceBlock).where(ResumeSourceBlock.resume_id == resume.id)
            )
            resume.source_page_count = source_page_count
            resume.parsed_page_count = parsed_page_count
            resume.extraction_status = extraction_status
            resume.quality_flags = sorted({str(flag) for flag in quality_flags})
            resume.parser_version = parser_version[:100]
            resume.raw_text = _database_safe_text(raw_text) or None
            has_source_text = False
            for page in pages:
                page_text = _database_safe_text(str(getattr(page, "text", "")))
                if not page_text:
                    continue
                has_source_text = True
                session.add(
                    ResumeSourceBlock(
                        resume_id=resume.id,
                        block_id=f"page-{int(getattr(page, 'page_no')):03d}",
                        page_no=int(getattr(page, "page_no")),
                        block_type="page_text",
                        text=page_text,
                    )
                )
            job.status = DOCUMENT_EXTRACTION_COMPLETED
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = None
            job.completed_at = utcnow()
            session.flush()
            if has_source_text:
                # A reparse can have a terminal prior AI job.  Requeue it
                # against the freshly incremented facts version rather than
                # silently retaining that stale terminal state.  The document
                # job is marked completed first, so the AI request is never
                # blocked by an in-flight source-normalization guard.
                from app.services.ai_extraction_job_service import (
                    request_resume_ai_extraction,
                )

                request_resume_ai_extraction(
                    session,
                    resume_id=resume.id,
                    settings=settings,
                )
            session.commit()


def _finish_failure(
    database: Database,
    *,
    worker_id: str,
    claimed: ClaimedDocumentExtractionJob,
    error: str,
    retryable: bool,
) -> None:
    now = utcnow()
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            job = _owned_running_job(
                session,
                job_id=claimed.job_id,
                worker_id=worker_id,
                organization_id=claimed.organization_id,
                for_update=True,
            )
            if job is None or job.resume_id != claimed.resume_id:
                session.rollback()
                return
            resume = session.scalar(
                select(Resume)
                .where(Resume.id == claimed.resume_id)
                .with_for_update()
            )
            if resume is None or resume.organization_id != claimed.organization_id:
                # A corrupted queue row may refer to a different workspace's
                # resume. The worker is still allowed to retire its own job,
                # but it must not touch the foreign resume or leave a leaked
                # running lease that will be repeatedly reclaimed forever.
                job.status = DOCUMENT_EXTRACTION_NEEDS_ATTENTION
                job.next_attempt_at = None
                job.completed_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = error[:2000]
                session.commit()
                return
            retry = retryable and job.attempt_count < job.max_attempts
            if retry:
                delay_seconds = min(60, 2 ** max(job.attempt_count - 1, 0))
                job.status = DOCUMENT_EXTRACTION_QUEUED
                job.next_attempt_at = now + timedelta(seconds=delay_seconds)
                job.completed_at = None
                if not resume.is_active and resume.extraction_status != "ready":
                    resume.extraction_status = "queued"
            else:
                job.status = DOCUMENT_EXTRACTION_NEEDS_ATTENTION
                job.next_attempt_at = None
                job.completed_at = now
                if not resume.is_active and resume.extraction_status != "ready":
                    session.execute(
                        delete(ResumeSourceBlock).where(
                            ResumeSourceBlock.resume_id == resume.id
                        )
                    )
                    resume.source_page_count = 0
                    resume.parsed_page_count = 0
                    resume.extraction_status = "failed"
                    resume.quality_flags = [error[:2000]]
                    resume.parser_version = "document-worker"
                    resume.raw_text = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = error[:2000]
            session.commit()


def _owned_running_job(
    session: Session,
    *,
    job_id: str,
    worker_id: str,
    organization_id: str,
    for_update: bool = False,
) -> ResumeDocumentExtractionJob | None:
    statement = select(ResumeDocumentExtractionJob).where(
        ResumeDocumentExtractionJob.id == job_id,
        ResumeDocumentExtractionJob.organization_id == organization_id,
        ResumeDocumentExtractionJob.status == DOCUMENT_EXTRACTION_RUNNING,
        ResumeDocumentExtractionJob.lease_owner == worker_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _is_retryable_document_error(error: str) -> bool:
    return error in {
        "office_conversion_timed_out",
        "spreadsheet_conversion_timed_out",
        "image_ocr_timed_out",
        "document_extraction_worker_error",
        "document_extraction_persist_failed",
    }


def _database_safe_text(value: str) -> str:
    return value.replace("\x00", "")


def _first_quality_error(resume: Resume) -> str | None:
    for flag in resume.quality_flags or []:
        if isinstance(flag, str) and flag:
            return flag
    return None


__all__ = [
    "DOCUMENT_EXTRACTION_COMPLETED",
    "DOCUMENT_EXTRACTION_NEEDS_ATTENTION",
    "DOCUMENT_EXTRACTION_QUEUED",
    "DOCUMENT_EXTRACTION_RUNNING",
    "DocumentExtractionJobError",
    "document_extraction_state",
    "enqueue_uploaded_resume_document_extraction",
    "request_resume_document_extraction",
    "run_document_extraction_worker_once",
]
