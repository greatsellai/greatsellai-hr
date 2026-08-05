from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import (
    Candidate,
    Resume,
    ResumeAiExtractionJob,
    ResumeEducation,
    ResumeExperience,
    ResumeFactSnapshot,
    ResumeLanguageCredential,
    ResumeReviewAction,
    ResumeScholarship,
    ResumeSkill,
    ResumeSourceBlock,
    ResumeSourceTag,
    ResumeSummary,
    ResumeUploadIdempotencyKey,
    utcnow,
)
from app.filter_options import normalize_language_credential
from app.schemas import ResumeFactsSaveRequest, ResumeFactsSubmission
from app.services.deepseek_provider import (
    DeepSeekProviderError,
    EvidenceBlock,
    FACT_SNAPSHOT_SCHEMA_VERSION,
    extract_resume_facts,
)
from app.services.ai_gateway_service import (
    AiExecutionSpec,
    AiGatewayError,
    ai_gateway_credentials_configured,
    ai_gateway_execution,
    gateway_prompt_transport_arguments,
)
from app.services.institution_service import (
    classify_education_institution,
    resolve_institution,
    resolve_institution_by_roster_id,
    resolve_registry_institution,
)
from app.services.normalization import (
    highest_degree,
    merged_month_count,
    normalized_contains,
    normalized_key,
)
from app.services.document_text_extraction import (
    DocumentExtractionError,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    validate_document_signature,
)
from app.tenant_scope import LEGACY_ORGANIZATION_ID, organization_context_id


WORK_CONTEXT_MARKERS = (
    "工作经历",
    "工作经验",
    "工作履历",
    "任职经历",
    "任职履历",
    "职业履历",
    "职业经历",
    "从业经历",
    "任职",
    "就职",
    "入职",
    "全职",
    "work experience",
    "work history",
    "career history",
    "career experience",
    "professional experience",
    "professional history",
    "employment experience",
    "employment history",
    "employment",
    "full time",
    "full-time",
)
INTERNSHIP_CONTEXT_MARKERS = ("实习经历", "实习", "internship", "intern")
NON_WORK_CONTEXT_MARKERS = ("项目", "竞赛", "比赛", "课程设计", "科研", "论文", "社团", "获奖")


class ResumeServiceError(RuntimeError):
    pass


class NotFoundError(ResumeServiceError):
    pass


class UploadValidationError(ResumeServiceError):
    pass


class IdempotencyConflictError(ResumeServiceError):
    pass


class FactValidationError(ResumeServiceError):
    pass


def _database_safe_text(value: str) -> str:
    """Remove characters PostgreSQL cannot store in text columns."""

    return value.replace("\x00", "")


_MAX_IDEMPOTENCY_KEY_LENGTH = 255


def validate_pdf_resume_upload(
    *,
    original_filename: str | None,
    content: bytes,
    settings: AppSettings,
) -> str:
    """Validate a supported resume document before durable storage."""

    if not content:
        raise UploadValidationError("empty_upload")
    if len(content) > settings.max_upload_bytes:
        raise UploadValidationError("file_too_large")
    submitted_name = Path(original_filename or "resume.pdf").name
    suffix = Path(submitted_name).suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise UploadValidationError("unsupported_document_type")
    try:
        validate_document_signature(filename=submitted_name, content=content)
    except DocumentExtractionError as exc:
        # Preserve the legacy PDF-specific response for existing API clients
        # while applying the same signature gate to every supported format.
        if submitted_name.lower().endswith(".pdf") and str(exc) == "invalid_document_signature":
            raise UploadValidationError("not_a_pdf") from exc
        raise UploadValidationError(str(exc)) from exc
    # Image resumes have no native text layer. They would otherwise be
    # accepted and only fail later in the durable worker, which is misleading
    # now that Tencent is the sole image OCR provider. Keep this after the
    # signature gate so a spoofed image still reports its invalid file.
    if suffix in {".png", ".jpg", ".jpeg"} and (
        not settings.tencent_secret_id or not settings.tencent_secret_key
    ):
        raise UploadValidationError("tencent_ocr_not_configured")
    return submitted_name


def normalize_upload_idempotency_key(value: str | None) -> str | None:
    """Normalize the optional request header without persisting its raw value."""

    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise UploadValidationError("invalid_idempotency_key")
    return normalized


def _idempotency_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validated_organization_id(organization_id: str) -> str:
    """Reject a malformed workspace identifier before it reaches the filesystem."""

    normalized = organization_id.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ResumeServiceError("invalid_organization_storage_namespace")
    return normalized


def _storage_key_parts(storage_key: str) -> tuple[str, ...]:
    """Return a strictly relative, POSIX-style storage key.

    Database rows are not trusted as filesystem paths.  In particular, a
    backslash is rejected rather than treated as a harmless character because
    it is a directory separator on Windows development machines.
    """

    if not storage_key or "\\" in storage_key:
        raise ResumeServiceError("resume_original_file_not_found")
    path = PurePosixPath(storage_key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ResumeServiceError("resume_original_file_not_found")
    return path.parts


def build_resume_storage_key(*, organization_id: str, suffix: str) -> str:
    """Create the durable key for a newly uploaded original.

    New files are always direct children of their owning workspace directory.
    Old flat keys are read-only compatibility data for the legacy workspace;
    this function never creates another flat key.
    """

    namespace = _validated_organization_id(organization_id)
    normalized_suffix = suffix.lower()
    if normalized_suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ResumeServiceError("unsupported_document_type")
    return f"{namespace}/{uuid4().hex}{normalized_suffix}"


def resolve_uploaded_resume_path(
    settings: AppSettings,
    *,
    storage_key: str,
    organization_id: str,
    require_file: bool = True,
) -> Path:
    """Resolve one original only inside its current workspace namespace.

    A flat historical key is valid only for the deterministic legacy
    workspace.  Every new key has exactly two components:
    ``<organization_id>/<filename>``.  This guards downloads, reparses and
    rollback cleanup against path traversal and cross-workspace file access.
    """

    namespace = _validated_organization_id(organization_id)
    parts = _storage_key_parts(storage_key)
    legacy_flat_key = len(parts) == 1
    workspace_key = len(parts) == 2 and parts[0] == namespace
    if not (workspace_key or (legacy_flat_key and namespace == LEGACY_ORGANIZATION_ID)):
        raise ResumeServiceError("resume_original_file_not_found")

    try:
        upload_root = settings.upload_dir.resolve()
        raw_path = upload_root.joinpath(*parts)
        workspace_directory = upload_root / namespace
        # A directory symlink could make a syntactically valid key for A read
        # files physically stored under B.  Originals are regular files in
        # direct workspace directories, so reject symlinks rather than trying
        # to reason about their targets.
        if raw_path.is_symlink() or (
            not legacy_flat_key and workspace_directory.is_symlink()
        ):
            raise ResumeServiceError("resume_original_file_not_found")
        source_path = raw_path.resolve()
        expected_parent = (
            upload_root
            if legacy_flat_key
            else workspace_directory.resolve()
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResumeServiceError("resume_original_file_not_found") from exc

    # ``resolve`` follows symlinks.  Comparing the resolved parent therefore
    # also rejects a malicious symlink that points outside the assigned
    # workspace directory.
    if source_path.parent != expected_parent:
        raise ResumeServiceError("resume_original_file_not_found")
    try:
        source_path.relative_to(upload_root)
    except ValueError as exc:
        raise ResumeServiceError("resume_original_file_not_found") from exc
    if require_file and not source_path.is_file():
        raise ResumeServiceError("resume_original_file_not_found")
    return source_path


def _prepare_new_upload_path(
    settings: AppSettings,
    *,
    storage_key: str,
    organization_id: str,
) -> Path:
    """Create and verify the workspace directory before atomically writing."""

    namespace = _validated_organization_id(organization_id)
    settings.ensure_directories()
    upload_root = settings.upload_dir.resolve()
    workspace_directory = upload_root / namespace
    try:
        workspace_directory.mkdir(parents=True, exist_ok=True)
        if workspace_directory.resolve() != workspace_directory:
            raise ResumeServiceError("resume_original_file_not_found")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResumeServiceError("resume_original_file_not_found") from exc
    return resolve_uploaded_resume_path(
        settings,
        storage_key=storage_key,
        organization_id=namespace,
        require_file=False,
    )


def get_idempotent_upload_resume(
    session: Session,
    *,
    idempotency_key: str,
    content_sha256: str,
) -> Resume | None:
    """Return an earlier upload for a retry, or reject key reuse with new bytes."""

    organization_id = organization_context_id(session)
    record = session.scalar(
        select(ResumeUploadIdempotencyKey).where(
            ResumeUploadIdempotencyKey.organization_id == organization_id,
            ResumeUploadIdempotencyKey.idempotency_key_hash
            == _idempotency_key_hash(idempotency_key),
        )
    )
    if record is None:
        return None
    if record.content_sha256 != content_sha256:
        raise IdempotencyConflictError("idempotency_key_reused_with_different_pdf")
    resume = session.scalar(select(Resume).where(Resume.id == record.resume_id))
    if resume is None:  # Defensive guard for a manually damaged database.
        raise ResumeServiceError("idempotency_record_resume_not_found")
    return resume


def register_upload_idempotency_key(
    session: Session,
    *,
    idempotency_key: str,
    content_sha256: str,
    resume_id: str,
) -> None:
    session.add(
        ResumeUploadIdempotencyKey(
            organization_id=organization_context_id(session),
            idempotency_key_hash=_idempotency_key_hash(idempotency_key),
            content_sha256=content_sha256,
            resume_id=resume_id,
        )
    )


def discard_uploaded_pdf(
    settings: AppSettings,
    *,
    storage_key: str | None,
    organization_id: str = LEGACY_ORGANIZATION_ID,
) -> None:
    """Best-effort cleanup for an upload whose database transaction failed."""

    if not storage_key:
        return
    try:
        storage_path = resolve_uploaded_resume_path(
            settings,
            storage_key=storage_key,
            organization_id=organization_id,
            require_file=False,
        )
        storage_path.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError, ResumeServiceError):
        # The request is already failing.  Avoid replacing its database error
        # with a cleanup error; normal operation still removes the file.
        return


def _write_upload_atomically(*, storage_path: Path, content: bytes) -> None:
    """Write bytes through a same-directory temporary file before publishing."""

    temporary_path = storage_path.with_name(
        f".{storage_path.name}.{uuid4().hex}.uploading"
    )
    try:
        if storage_path.exists():
            raise ResumeServiceError("generated_storage_key_already_exists")
        with temporary_path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, storage_path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_uploaded_original_atomically(
    *,
    source_path: Path,
    storage_path: Path,
    expected_sha256: str,
    max_bytes: int,
) -> None:
    """Copy a prior original without loading it or parsing it in an API worker.

    Parser-repair requests duplicate a stored original into a new immutable
    resume version.  The copy stays bounded and independently hashes the
    source while it streams, so a changed or oversized filesystem object can
    never be promoted to a replacement version.
    """

    temporary_path = storage_path.with_name(
        f".{storage_path.name}.{uuid4().hex}.uploading"
    )
    try:
        if storage_path.exists():
            raise ResumeServiceError("generated_storage_key_already_exists")
        digest = hashlib.sha256()
        copied_bytes = 0
        with source_path.open("rb") as source, temporary_path.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                copied_bytes += len(chunk)
                if copied_bytes > max_bytes:
                    raise ResumeServiceError("resume_original_file_too_large")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise ResumeServiceError("resume_original_hash_mismatch")
        os.replace(temporary_path, storage_path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def create_candidate(session: Session, *, display_name: str | None) -> Candidate:
    candidate = Candidate(display_name=display_name.strip() if display_name else None)
    session.add(candidate)
    session.flush()
    return candidate


def get_resume(session: Session, resume_id: str) -> Resume:
    resume = session.scalar(select(Resume).where(Resume.id == resume_id))
    if resume is None:
        raise NotFoundError("resume_not_found")
    return resume


def save_pdf_resume(
    session: Session,
    *,
    candidate_id: str,
    original_filename: str | None,
    content: bytes,
    settings: AppSettings,
) -> Resume:
    organization_id = organization_context_id(session)
    candidate = session.scalar(select(Candidate).where(Candidate.id == candidate_id))
    if candidate is None:
        raise NotFoundError("candidate_not_found")
    if candidate.organization_id != organization_id:
        # This is normally enforced by the session criteria. Keep the service
        # boundary explicit in case it is ever called from a bypassed worker.
        raise NotFoundError("candidate_not_found")
    submitted_name = validate_pdf_resume_upload(
        original_filename=original_filename,
        content=content,
        settings=settings,
    )

    storage_key = build_resume_storage_key(
        organization_id=organization_id,
        suffix=Path(submitted_name).suffix,
    )
    storage_path = _prepare_new_upload_path(
        settings,
        storage_key=storage_key,
        organization_id=organization_id,
    )
    try:
        _write_upload_atomically(storage_path=storage_path, content=content)
        sha256 = hashlib.sha256(content).hexdigest()
        resume = Resume(
            candidate_id=candidate_id,
            original_filename=submitted_name[:255],
            storage_key=storage_key,
            sha256=sha256,
            source_page_count=0,
            parsed_page_count=0,
            extraction_status="queued",
            quality_flags=[],
            parser_version="document-worker",
            raw_text=None,
            is_985_211=None,
        )
        session.add(resume)
        session.flush()
        # Import lazily to avoid a service cycle: the worker reuses this
        # module's safe storage-path resolver when it claims the job. The HTTP
        # path therefore ends after validation, atomic storage and a durable
        # queue write; it never runs Office, OCR, OpenPyXL or PDF parsing.
        from app.services.document_extraction_job_service import (
            enqueue_uploaded_resume_document_extraction,
        )

        enqueue_uploaded_resume_document_extraction(
            session,
            resume=resume,
            settings=settings,
        )
        return resume
    except Exception:
        discard_uploaded_pdf(
            settings,
            storage_key=storage_key,
            organization_id=organization_id,
        )
        raise


def reparse_inactive_resume_source_text(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Requeue source evidence for an inactive resume after parser/OCR changes.

    The caller remains deliberately parse-free.  The worker owns all PDF,
    Office, OCR and spreadsheet parsing, and it only replaces source blocks
    once the new normalization result has passed its file/hash limits.
    """

    resume = get_resume(session, resume_id)
    old_snapshot = _fact_snapshot(resume)
    try:
        from app.services.document_extraction_job_service import (
            DocumentExtractionJobError,
            request_resume_document_extraction,
        )

        resume = request_resume_document_extraction(
            session,
            resume_id=resume.id,
            settings=settings,
        )
    except DocumentExtractionJobError as exc:
        raise ResumeServiceError(str(exc)) from exc

    session.add(
        ResumeReviewAction(
            resume_id=resume.id,
            action="resume_source_reparse_queued",
            actor="system:parser-repair",
            note=None,
            old_values=old_snapshot,
            new_values=_fact_snapshot(resume),
        )
    )
    session.flush()
    return resume


_SOURCE_REPARSE_REQUESTED_ACTION = "resume_source_reparse_requested"
_SOURCE_REPARSE_CLONE_CREATED_ACTION = "resume_source_reparse_clone_created"


def reparse_active_resume_as_new_version(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Create a separately stored parser-repair version of an active resume.

    A resume's ``ResumeSourceBlock`` rows are its live evidence contract.  It
    is therefore unsafe to overwrite those rows after facts, scores, summaries
    or JD matches have been created.  Instead this function copies the
    original into a new ``Resume`` row and durably queues its source
    normalization.  It never invokes an Office converter, OCR, PDF parser or
    spreadsheet reader in the HTTP request.  The original evidence and
    historical outputs remain untouched; it is immediately marked
    source-unreliable so it cannot continue to influence screening while the
    new version is being rebuilt.

    The new job carries an audit-only activation guard through a review action.
    The worker checks that the source version is still the active version just
    before auto-activation; a later upload or edit can therefore never be
    displaced by a delayed parser-repair job.
    """

    source_resume = session.scalar(
        select(Resume).where(Resume.id == resume_id).with_for_update()
    )
    if source_resume is None:
        raise NotFoundError("resume_not_found")
    if not source_resume.is_active or source_resume.extraction_status != "ready":
        raise ResumeServiceError("resume_must_be_active_and_ready_for_source_reparse")
    source_job = source_resume.ai_extraction_job
    if source_job is not None and source_job.status in {"queued", "running"}:
        # A ready active version already has a facts snapshot.  Any lingering
        # upload-time AI job can no longer write to it (the worker's version
        # guard rejects active resumes), so retire it instead of making a
        # parser repair wait behind stale work.
        source_job.status = "needs_attention"
        source_job.next_attempt_at = None
        source_job.lease_owner = None
        source_job.lease_expires_at = None
        source_job.last_error = "source_reparse_superseded_ai_extraction"
        source_job.completed_at = utcnow()
    _assert_no_pending_source_reparse(session, source_resume=source_resume)

    try:
        source_path = resolve_uploaded_resume_path(
            settings,
            storage_key=source_resume.storage_key,
            organization_id=organization_context_id(session),
        )
    except ResumeServiceError as exc:
        raise ResumeServiceError("resume_original_file_not_found") from exc

    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ResumeServiceError("unsupported_document_type")
    # This endpoint is only exposed for a source-quality repair.  Make the
    # existing version non-eligible immediately while the replacement is
    # rebuilt, rather than leaving known-bad facts searchable for the worker's
    # full AI turnaround.  Its evidence and all historical outputs remain
    # intact for audit; only the screening eligibility signal changes.
    source_old_snapshot = _fact_snapshot(source_resume)
    source_old_quality_flags = sorted(set(source_resume.quality_flags or []))
    source_new_quality_flags = sorted(
        {*source_old_quality_flags, "source_text_unreliable"}
    )
    source_resume.quality_flags = source_new_quality_flags

    # The canonical resume version can later be repaired because its source
    # text was unreliable.  Preserve submission provenance on the replacement
    # version before it becomes eligible; otherwise a successful parser repair
    # would make an email-imported candidate disappear from source-tag filters.
    # The historical mail import rows remain owned by the original version, so
    # clone their query projection without copying first/last import foreign
    # keys.  That keeps future physical deletion of the archived source from
    # being blocked by a repair clone while retaining the recruiter-facing tag
    # snapshot and occurrence count.
    source_tag_projections = session.scalars(
        select(ResumeSourceTag)
        .where(ResumeSourceTag.resume_id == source_resume.id)
        .order_by(ResumeSourceTag.source_tag_id)
    ).all()

    organization_id = organization_context_id(session)
    storage_key = build_resume_storage_key(
        organization_id=organization_id,
        suffix=suffix,
    )
    storage_path = _prepare_new_upload_path(
        settings,
        storage_key=storage_key,
        organization_id=organization_id,
    )
    try:
        _copy_uploaded_original_atomically(
            source_path=source_path,
            storage_path=storage_path,
            expected_sha256=source_resume.sha256,
            max_bytes=settings.max_upload_bytes,
        )
        replacement = Resume(
            candidate_id=source_resume.candidate_id,
            original_filename=source_resume.original_filename,
            storage_key=storage_key,
            sha256=source_resume.sha256,
            source_page_count=0,
            parsed_page_count=0,
            extraction_status="queued",
            quality_flags=[],
            parser_version="document-worker",
            raw_text=None,
            is_985_211=None,
            ingestion_source_type=source_resume.ingestion_source_type,
            source_mailbox_config_id=source_resume.source_mailbox_config_id,
            source_mailbox_label_snapshot=source_resume.source_mailbox_label_snapshot,
        )
        session.add(replacement)
        session.flush()
        for source_projection in source_tag_projections:
            session.add(
                ResumeSourceTag(
                    organization_id=organization_id,
                    resume_id=replacement.id,
                    source_tag_id=source_projection.source_tag_id,
                    tag_name_snapshot=source_projection.tag_name_snapshot,
                    first_import_id=None,
                    last_import_id=None,
                    first_seen_at=source_projection.first_seen_at,
                    last_seen_at=source_projection.last_seen_at,
                    source_count=source_projection.source_count,
                )
            )
        # This is the same durable parser queue used by browser and mailbox
        # uploads.  It is intentionally created before the audit rows so a
        # committed repair action can never point at a replacement that is
        # missing its source-normalization work item.
        from app.services.document_extraction_job_service import (
            enqueue_uploaded_resume_document_extraction,
        )

        enqueue_uploaded_resume_document_extraction(
            session,
            resume=replacement,
            settings=settings,
        )

        session.add(
            ResumeReviewAction(
                resume_id=source_resume.id,
                action=_SOURCE_REPARSE_REQUESTED_ACTION,
                actor="system:parser-repair",
                note=None,
                old_values={
                    **source_old_snapshot,
                    "quality_flags": source_old_quality_flags,
                },
                new_values={
                    "replacement_resume_id": replacement.id,
                    "quality_flags": source_new_quality_flags,
                },
            )
        )
        session.add(
            ResumeReviewAction(
                resume_id=replacement.id,
                action=_SOURCE_REPARSE_CLONE_CREATED_ACTION,
                actor="system:parser-repair",
                note=None,
                old_values={
                    "source_resume_id": source_resume.id,
                    "source_facts_version": source_resume.facts_version,
                    "source_parser_version": source_resume.parser_version,
                },
                new_values=_fact_snapshot(replacement),
            )
        )

        session.flush()
        return replacement
    except Exception:
        discard_uploaded_pdf(
            settings,
            storage_key=storage_key,
            organization_id=organization_id,
        )
        raise


def reparse_clone_auto_activation_allowed(
    session: Session,
    *,
    resume: Resume,
) -> bool:
    """Return whether an AI job may auto-activate a parser-repair clone.

    Normal uploads have no parser-repair marker and keep the existing automatic
    activation path.  A repair clone is allowed to replace its source only if
    that exact source facts version remains active.  Locking the source row
    prevents a concurrent upload, activation, or facts edit from racing the
    check with the clone's activation transaction.
    """

    action = session.scalar(
        select(ResumeReviewAction)
        .where(
            ResumeReviewAction.resume_id == resume.id,
            ResumeReviewAction.action == _SOURCE_REPARSE_CLONE_CREATED_ACTION,
        )
        .order_by(ResumeReviewAction.created_at.desc(), ResumeReviewAction.id.desc())
    )
    if action is None:
        return True
    guard = action.old_values if isinstance(action.old_values, dict) else {}
    source_resume_id = guard.get("source_resume_id")
    source_facts_version = guard.get("source_facts_version")
    if (
        not isinstance(source_resume_id, str)
        or not source_resume_id
        or isinstance(source_facts_version, bool)
        or not isinstance(source_facts_version, int)
    ):
        return False
    source_resume = session.scalar(
        select(Resume).where(Resume.id == source_resume_id).with_for_update()
    )
    return bool(
        source_resume is not None
        and source_resume.candidate_id == resume.candidate_id
        and source_resume.is_active
        and source_resume.extraction_status == "ready"
        and source_resume.facts_version == source_facts_version
    )


def _assert_no_pending_source_reparse(
    session: Session,
    *,
    source_resume: Resume,
) -> None:
    """Reject duplicate repair jobs for the same active source version."""

    actions = session.scalars(
        select(ResumeReviewAction)
        .where(
            ResumeReviewAction.resume_id == source_resume.id,
            ResumeReviewAction.action == _SOURCE_REPARSE_REQUESTED_ACTION,
        )
        .order_by(ResumeReviewAction.created_at.desc(), ResumeReviewAction.id.desc())
        .limit(20)
    ).all()
    for action in actions:
        replacement_id = (
            action.new_values.get("replacement_resume_id")
            if isinstance(action.new_values, dict)
            else None
        )
        if not isinstance(replacement_id, str) or not replacement_id:
            continue
        replacement = session.scalar(select(Resume).where(Resume.id == replacement_id))
        if replacement is None or replacement.candidate_id != source_resume.candidate_id:
            continue
        document_job = replacement.document_extraction_job
        if document_job is not None and document_job.status in {"queued", "running"}:
            raise ResumeServiceError("source_resume_reparse_already_running")
        job = replacement.ai_extraction_job
        if job is not None and job.status in {"queued", "running"}:
            raise ResumeServiceError("source_resume_reparse_already_running")


def _fact_snapshot(resume: Resume) -> dict[str, object]:
    return {
        "extraction_status": resume.extraction_status,
        "is_active": resume.is_active,
        "is_985_211": resume.is_985_211,
        "highest_degree": resume.highest_degree,
        "employment_months": resume.employment_months,
        "employment_or_internship_months": resume.employment_or_internship_months,
        "education_count": len(resume.educations),
        "experience_count": len(resume.experiences),
        "skill_count": len(resume.skills),
        "language_credential_count": len(resume.language_credentials),
        "scholarship_count": len(resume.scholarships),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sorted_block_ids(block_ids: list[str] | None) -> list[str]:
    return sorted({block_id for block_id in (block_ids or []) if block_id})


def _canonical_fact_payload(
    session: Session,
    *,
    resume: Resume,
) -> tuple[dict[str, object], list[str]]:
    """Build a stable payload from persisted facts, not caller input order or IDs."""

    educations = session.scalars(
        select(ResumeEducation).where(ResumeEducation.resume_id == resume.id)
    ).all()
    experiences = session.scalars(
        select(ResumeExperience).where(ResumeExperience.resume_id == resume.id)
    ).all()
    skills = session.scalars(
        select(ResumeSkill).where(ResumeSkill.resume_id == resume.id)
    ).all()
    language_credentials = session.scalars(
        select(ResumeLanguageCredential).where(
            ResumeLanguageCredential.resume_id == resume.id
        )
    ).all()
    scholarships = session.scalars(
        select(ResumeScholarship).where(ResumeScholarship.resume_id == resume.id)
    ).all()

    source_block_ids: set[str] = set()
    education_entries: list[dict[str, object]] = []
    for education in educations:
        evidence_block_ids = _sorted_block_ids(education.evidence_block_ids)
        classification_evidence_block_ids = _sorted_block_ids(
            education.classification_evidence_block_ids
        )
        source_block_ids.update(evidence_block_ids)
        source_block_ids.update(classification_evidence_block_ids)
        education_entries.append(
            {
                "school_name_raw": education.school_name_raw,
                "school_key": education.school_key,
                "school_match_state": education.school_match_state,
                "degree": education.degree,
                "major_raw": education.major_raw,
                "major_key": education.major_key,
                "start_month": education.start_month,
                "end_month": education.end_month,
                "institution_tiers": sorted(education.institution_tiers or []),
                "institution_classification": education.institution_classification,
                "classification_basis": education.classification_basis,
                "classification_registry_version": education.classification_registry_version,
                "classification_evidence_block_ids": classification_evidence_block_ids,
                "average_score": education.average_score,
                "gpa_value": education.gpa_value,
                "gpa_scale": education.gpa_scale,
                "gpa_percent": education.gpa_percent,
                "rank_position": education.rank_position,
                "rank_total": education.rank_total,
                "rank_percent": education.rank_percent,
                "evidence_block_ids": evidence_block_ids,
            }
        )

    experience_entries: list[dict[str, object]] = []
    for experience in experiences:
        evidence_block_ids = _sorted_block_ids(experience.evidence_block_ids)
        classification_evidence_block_ids = _sorted_block_ids(
            experience.classification_evidence_block_ids
        )
        detail_items: list[dict[str, object]] = []
        for detail in experience.detail_items or []:
            if not isinstance(detail, dict):
                continue
            detail_raw = detail.get("detail_raw")
            detail_evidence_block_ids = detail.get("evidence_block_ids")
            if not isinstance(detail_raw, str) or not isinstance(
                detail_evidence_block_ids, list
            ):
                continue
            normalized_detail_evidence = _sorted_block_ids(detail_evidence_block_ids)
            source_block_ids.update(normalized_detail_evidence)
            detail_items.append(
                {
                    "detail_raw": detail_raw,
                    "evidence_block_ids": normalized_detail_evidence,
                }
            )
        source_block_ids.update(evidence_block_ids)
        source_block_ids.update(classification_evidence_block_ids)
        experience_entries.append(
            {
                "experience_type": experience.experience_type,
                "experience_name_raw": experience.experience_name_raw,
                "experience_name_key": experience.experience_name_key,
                "organization_name_raw": experience.organization_name_raw,
                "organization_key": experience.organization_key,
                "title_raw": experience.title_raw,
                "title_key": experience.title_key,
                "start_month": experience.start_month,
                "end_month": experience.end_month,
                "is_current": experience.is_current,
                "evidence_block_ids": evidence_block_ids,
                "classification_evidence_block_ids": classification_evidence_block_ids,
                "detail_items": detail_items,
                "leadership_context": experience.leadership_context,
                "leadership_role": experience.leadership_role,
                "award_level": experience.award_level,
                "award_result_raw": experience.award_result_raw,
            }
        )

    skill_entries: list[dict[str, object]] = []
    for skill in skills:
        evidence_block_ids = _sorted_block_ids(skill.evidence_block_ids)
        source_block_ids.update(evidence_block_ids)
        skill_entries.append(
            {
                "skill_key": skill.skill_key,
                "skill_display": skill.skill_display,
                "skill_category": skill.skill_category,
                "evidence_block_ids": evidence_block_ids,
            }
        )

    language_entries: list[dict[str, object]] = []
    for credential in language_credentials:
        evidence_block_ids = _sorted_block_ids(credential.evidence_block_ids)
        source_block_ids.update(evidence_block_ids)
        language_entries.append(
            {
                "credential_code": credential.credential_code,
                "credential_name_raw": credential.credential_name_raw,
                "score": credential.score,
                "passed": credential.passed,
                "evidence_block_ids": evidence_block_ids,
            }
        )

    scholarship_entries: list[dict[str, object]] = []
    for scholarship in scholarships:
        evidence_block_ids = _sorted_block_ids(scholarship.evidence_block_ids)
        source_block_ids.update(evidence_block_ids)
        scholarship_entries.append(
            {
                "scholarship_name_raw": scholarship.scholarship_name_raw,
                "scholarship_name_key": scholarship.scholarship_name_key,
                "scholarship_level": scholarship.scholarship_level,
                "evidence_block_ids": evidence_block_ids,
            }
        )

    education_entries.sort(key=_canonical_json)
    experience_entries.sort(key=_canonical_json)
    skill_entries.sort(key=_canonical_json)
    language_entries.sort(key=_canonical_json)
    scholarship_entries.sort(key=_canonical_json)
    for index, entry in enumerate(education_entries, start=1):
        entry["fact_id"] = f"education-{index:03d}"
    for index, entry in enumerate(experience_entries, start=1):
        entry["fact_id"] = f"experience-{index:03d}"
    for index, entry in enumerate(skill_entries, start=1):
        entry["fact_id"] = f"skill-{index:03d}"
    for index, entry in enumerate(language_entries, start=1):
        entry["fact_id"] = f"language-{index:03d}"
    for index, entry in enumerate(scholarship_entries, start=1):
        entry["fact_id"] = f"scholarship-{index:03d}"
    sorted_source_block_ids = sorted(source_block_ids)
    return (
        {
            "schema_version": FACT_SNAPSHOT_SCHEMA_VERSION,
            "facts_schema_version": "resume_facts.v2",
            "education": education_entries,
            "experiences": experience_entries,
            "skills": skill_entries,
            "language_credentials": language_entries,
            "scholarships": scholarship_entries,
            "derived": {
                "is_985_211": resume.is_985_211,
                "highest_degree": resume.highest_degree,
                "employment_months": resume.employment_months,
                "employment_or_internship_months": (
                    resume.employment_or_internship_months
                ),
                "gender": resume.gender,
                "birth_date": (
                    resume.birth_date.isoformat() if resume.birth_date else None
                ),
            },
            "source_block_ids": sorted_source_block_ids,
        },
        sorted_source_block_ids,
    )


def _create_fact_snapshot(
    session: Session,
    *,
    resume: Resume,
    created_by: str,
) -> ResumeFactSnapshot:
    payload, source_block_ids = _canonical_fact_payload(session, resume=resume)
    canonical_facts_json = _canonical_json(payload)
    snapshot = ResumeFactSnapshot(
        resume_id=resume.id,
        facts_version=resume.facts_version,
        canonical_facts_json=canonical_facts_json,
        facts_sha256=hashlib.sha256(canonical_facts_json.encode("utf-8")).hexdigest(),
        source_block_ids=source_block_ids,
        created_by=(created_by.strip() or "single_admin")[:100],
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _source_text_by_ids(
    session: Session,
    *,
    resume_id: str,
    block_ids: list[str],
) -> str:
    clean_ids = [item.strip() for item in block_ids if item and item.strip()]
    if len(clean_ids) != len(set(clean_ids)) or len(clean_ids) != len(block_ids):
        raise FactValidationError("invalid_or_duplicate_evidence_block_id")
    if not clean_ids:
        raise FactValidationError("missing_evidence_block_id")
    blocks = session.scalars(
        select(ResumeSourceBlock).where(
            ResumeSourceBlock.resume_id == resume_id,
            ResumeSourceBlock.block_id.in_(clean_ids),
        )
    ).all()
    if len(blocks) != len(clean_ids):
        raise FactValidationError("evidence_block_not_found_for_resume")
    return "\n".join(block.text for block in blocks)


def _assert_raw_value_grounded(
    *,
    value: str | None,
    source_text: str,
    label: str,
) -> None:
    if value and not normalized_contains(source_text, value):
        raise FactValidationError(f"{label}_not_grounded_in_evidence")


def _assert_numeric_value_grounded(
    *,
    value: float | int | None,
    source_text: str,
    label: str,
) -> None:
    if value is None:
        return
    rendered = f"{value:g}" if isinstance(value, float) else str(value)
    if not normalized_contains(source_text, rendered):
        raise FactValidationError(f"{label}_not_grounded_in_evidence")


# Demographic normalization. A resume may spell gender and birth date in a few
# common Chinese or English forms; the normalized values back the recruiter
# screening index while the raw, evidence-grounded text stays in the facts.
_GENDER_NORMALIZATION: dict[str, str] = {
    "male": "male",
    "m": "male",
    "男": "male",
    "female": "female",
    "f": "female",
    "女": "female",
}
_BIRTH_DATE_PARSE = re.compile(
    r"^\s*(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*月?\s*(?:[日/.\-]\s*(\d{1,2})\s*日?)?\s*$"
)
_ENGLISH_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ENGLISH_BIRTH_DATE = re.compile(
    r"^\s*(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+)?"
    r"(?P<month>[A-Za-z]{3,9})\.?\s+(?P<year>\d{4})\s*$"
)
_ENGLISH_MONTH_DAY_YEAR = re.compile(
    r"^\s*(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4})\s*$"
)


def _normalize_gender(raw: str | None) -> str | None:
    """Map an evidence-grounded gender line to ``male``/``female`` or None."""
    if not raw:
        return None
    key = raw.strip().lower()
    return _GENDER_NORMALIZATION.get(key)


def _normalize_birth_date(raw: str | None) -> date | None:
    """Parse a source-written birth date to a normalized calendar date."""
    if not raw:
        return None
    text = raw.strip()
    match = _BIRTH_DATE_PARSE.match(text)
    year: int | None = None
    month: int | None = None
    day = 1
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3) or 1)
    else:
        match = _ENGLISH_BIRTH_DATE.match(text)
        if match:
            month = _ENGLISH_MONTHS.get(match.group("month").lower())
            year = int(match.group("year"))
            day = int(match.group("day") or 1)
        else:
            match = _ENGLISH_MONTH_DAY_YEAR.match(text)
            if match:
                month = _ENGLISH_MONTHS.get(match.group("month").lower())
                year = int(match.group("year"))
                day = int(match.group("day"))
    if not year or not month:
        return None
    # A calendar check rejects impossible dates; a broad sanity window rejects
    # garbage years without ever disallowing a legitimate candidate.
    if year < 1930 or year > date.today().year:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def prepare_ai_draft_facts(
    session: Session,
    *,
    resume_id: str,
    facts: ResumeFactsSubmission,
) -> tuple[ResumeFactsSubmission, bool]:
    """Keep only fully source-grounded facts from an AI draft.

    This runs before the atomic fact replacement. Manual submissions remain
    strict: the worker alone may omit a bad model item and preserve the other
    verified items for automatic activation.
    """

    payload: dict[str, object] = {
        "schema_version": facts.schema_version,
        "candidate_name_raw": None,
        "candidate_name_evidence_block_ids": [],
        "gender_raw": None,
        "gender_evidence_block_ids": [],
        "birth_date_raw": None,
        "birth_date_evidence_block_ids": [],
        "education": [],
        "experiences": [],
        "skills": [],
        "language_credentials": [],
        "scholarships": [],
    }
    education_payload = payload["education"]
    experience_payload = payload["experiences"]
    skill_payload = payload["skills"]
    language_payload = payload["language_credentials"]
    scholarship_payload = payload["scholarships"]
    assert isinstance(education_payload, list)
    assert isinstance(experience_payload, list)
    assert isinstance(skill_payload, list)
    assert isinstance(language_payload, list)
    assert isinstance(scholarship_payload, list)
    partial = False

    if facts.candidate_name_raw:
        try:
            candidate_name_source_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=facts.candidate_name_evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=facts.candidate_name_raw,
                source_text=candidate_name_source_text,
                label="candidate_name_raw",
            )
        except FactValidationError:
            # A bad identity result must never block the otherwise grounded
            # resume from entering the library. It is simply omitted, just as
            # a bad AI education/skill item is omitted below.
            partial = True
        else:
            payload["candidate_name_raw"] = facts.candidate_name_raw
            payload["candidate_name_evidence_block_ids"] = (
                facts.candidate_name_evidence_block_ids
            )

    for evidence_field, raw_field in (
        ("gender_evidence_block_ids", "gender_raw"),
        ("birth_date_evidence_block_ids", "birth_date_raw"),
    ):
        raw_value = getattr(facts, raw_field)
        if not raw_value:
            continue
        try:
            demographic_source_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=getattr(facts, evidence_field),
            )
            _assert_raw_value_grounded(
                value=raw_value,
                source_text=demographic_source_text,
                label=raw_field,
            )
        except FactValidationError:
            # Like a bad identity result, an ungrounded demographic must never
            # block the otherwise grounded resume from entering the library.
            partial = True
        else:
            payload[raw_field] = raw_value
            payload[evidence_field] = getattr(facts, evidence_field)

    for education in facts.education:
        try:
            evidence_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=education.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=education.school_name_raw,
                source_text=evidence_text,
                label="school_name_raw",
            )
            _assert_raw_value_grounded(
                value=education.major_raw,
                source_text=evidence_text,
                label="major_raw",
            )
            for label, value in (
                ("average_score", education.average_score),
                ("gpa_value", education.gpa_value),
                ("gpa_scale", education.gpa_scale),
                ("rank_position", education.rank_position),
                ("rank_total", education.rank_total),
            ):
                _assert_numeric_value_grounded(
                    value=value,
                    source_text=evidence_text,
                    label=label,
                )
        except FactValidationError:
            partial = True
            continue
        education_payload.append(education.model_dump())

    for experience in facts.experiences:
        try:
            evidence_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=experience.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=experience.experience_name_raw,
                source_text=evidence_text,
                label="experience_name_raw",
            )
            _assert_raw_value_grounded(
                value=experience.organization_name_raw,
                source_text=evidence_text,
                label="organization_name_raw",
            )
            _assert_raw_value_grounded(
                value=experience.title_raw,
                source_text=evidence_text,
                label="title_raw",
            )
            _assert_raw_value_grounded(
                value=experience.leadership_role,
                source_text=evidence_text,
                label="leadership_role",
            )
            _assert_raw_value_grounded(
                value=experience.award_result_raw,
                source_text=evidence_text,
                label="award_result_raw",
            )
            _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=(
                    experience.classification_evidence_block_ids
                    or experience.evidence_block_ids
                ),
            )
        except FactValidationError:
            partial = True
            continue
        valid_detail_items: list[dict[str, object]] = []
        for detail in experience.detail_items:
            try:
                detail_source_text = _source_text_by_ids(
                    session,
                    resume_id=resume_id,
                    block_ids=detail.evidence_block_ids,
                )
                _assert_raw_value_grounded(
                    value=detail.detail_raw,
                    source_text=detail_source_text,
                    label="experience_detail_raw",
                )
            except FactValidationError:
                partial = True
                continue
            valid_detail_items.append(detail.model_dump())
        sanitized_experience = experience.model_dump()
        sanitized_experience["detail_items"] = valid_detail_items
        experience_payload.append(sanitized_experience)

    for skill in facts.skills:
        try:
            evidence_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=skill.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=skill.skill_display,
                source_text=evidence_text,
                label="skill_display",
            )
        except FactValidationError:
            partial = True
            continue
        skill_payload.append(skill.model_dump())

    for credential in facts.language_credentials:
        try:
            evidence_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=credential.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=credential.credential_name_raw,
                source_text=evidence_text,
                label="credential_name_raw",
            )
            if (
                credential.credential_code != "custom"
                and normalize_language_credential(credential.credential_name_raw)
                != credential.credential_code
            ):
                raise FactValidationError("language_credential_code_not_grounded")
            _assert_numeric_value_grounded(
                value=credential.score,
                source_text=evidence_text,
                label="language_credential_score",
            )
        except FactValidationError:
            partial = True
            continue
        language_payload.append(credential.model_dump())

    for scholarship in facts.scholarships:
        try:
            evidence_text = _source_text_by_ids(
                session,
                resume_id=resume_id,
                block_ids=scholarship.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=scholarship.scholarship_name_raw,
                source_text=evidence_text,
                label="scholarship_name_raw",
            )
        except FactValidationError:
            partial = True
            continue
        scholarship_payload.append(scholarship.model_dump())

    if not (
        education_payload
        or experience_payload
        or skill_payload
        or language_payload
        or scholarship_payload
    ):
        raise FactValidationError("ai_extraction_no_grounded_facts")
    return ResumeFactsSubmission.model_validate(payload), partial


def _has_work_context(source_text: str, experience_type: str) -> bool:
    key = normalized_key(source_text)
    if experience_type == "internship":
        return any(normalized_key(marker) in key for marker in INTERNSHIP_CONTEXT_MARKERS)
    return any(normalized_key(marker) in key for marker in WORK_CONTEXT_MARKERS)


def _only_non_work_context(source_text: str) -> bool:
    key = normalized_key(source_text)
    has_non_work = any(normalized_key(marker) in key for marker in NON_WORK_CONTEXT_MARKERS)
    has_positive = any(
        normalized_key(marker) in key
        for marker in (*WORK_CONTEXT_MARKERS, *INTERNSHIP_CONTEXT_MARKERS)
    )
    return has_non_work and not has_positive


def _replace_facts(
    session: Session,
    resume: Resume,
    request: ResumeFactsSaveRequest,
    *,
    created_by: str,
    force_pending_review: bool = False,
    auto_activate: bool = False,
) -> None:
    facts = request.facts
    old_snapshot = _fact_snapshot(resume)
    has_ambiguous_work_context = False
    candidate = session.scalar(
        select(Candidate)
        .where(Candidate.id == resume.candidate_id)
        .with_for_update()
    )
    if candidate is None:
        raise NotFoundError("candidate_not_found")

    # The worker may name a fresh, unnamed candidate only after the same
    # source-grounding checks used for every persisted fact. A pre-existing
    # display name is user-owned/legacy data and must never be overwritten.
    if force_pending_review and facts.candidate_name_raw:
        candidate_name_source_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=facts.candidate_name_evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=facts.candidate_name_raw,
            source_text=candidate_name_source_text,
            label="candidate_name_raw",
        )
        if not candidate.display_name or not candidate.display_name.strip():
            candidate.display_name = facts.candidate_name_raw.strip()

    if force_pending_review:
        for raw_field, evidence_field in (
            ("gender_raw", "gender_evidence_block_ids"),
            ("birth_date_raw", "birth_date_evidence_block_ids"),
        ):
            raw_value = getattr(facts, raw_field)
            if not raw_value:
                continue
            demographic_source_text = _source_text_by_ids(
                session,
                resume_id=resume.id,
                block_ids=getattr(facts, evidence_field),
            )
            _assert_raw_value_grounded(
                value=raw_value,
                source_text=demographic_source_text,
                label=raw_field,
            )

    session.execute(delete(ResumeEducation).where(ResumeEducation.resume_id == resume.id))
    session.execute(delete(ResumeExperience).where(ResumeExperience.resume_id == resume.id))
    session.execute(delete(ResumeSkill).where(ResumeSkill.resume_id == resume.id))
    session.execute(
        delete(ResumeLanguageCredential).where(
            ResumeLanguageCredential.resume_id == resume.id
        )
    )
    session.execute(
        delete(ResumeScholarship).where(ResumeScholarship.resume_id == resume.id)
    )
    session.flush()

    has_985_211 = False
    has_known_non_985_211 = False
    has_unresolved_school = not facts.education
    has_ai_rulebook_match = False
    has_invalid_ai_rulebook_reference = False
    for education in facts.education:
        evidence_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=education.evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=education.school_name_raw,
            source_text=evidence_text,
            label="school_name_raw",
        )
        _assert_raw_value_grounded(
            value=education.major_raw,
            source_text=evidence_text,
            label="major_raw",
        )
        for label, value in (
            ("average_score", education.average_score),
            ("gpa_value", education.gpa_value),
            ("gpa_scale", education.gpa_scale),
            ("rank_position", education.rank_position),
            ("rank_total", education.rank_total),
        ):
            _assert_numeric_value_grounded(
                value=value,
                source_text=evidence_text,
                label=label,
            )
        institution = resolve_institution(session, education.school_name_raw)
        is_local_match = institution is not None
        is_ai_rulebook_match = False
        if not is_local_match and force_pending_review:
            if education.ai_985_211_judgment:
                source_registry_institution = resolve_registry_institution(
                    education.school_name_raw
                )
                # An LLM-supplied roster ID is never authority by itself.  It
                # may only recover a missing local relation when the raw,
                # source-grounded school name has already matched the same
                # controlled roster entry exactly.
                if (
                    source_registry_institution is not None
                    and education.ai_institution_roster_id
                    == source_registry_institution.roster_id
                ):
                    ai_institution = resolve_institution_by_roster_id(
                        session,
                        education.ai_institution_roster_id,
                    )
                    if ai_institution is not None and ai_institution.is_985_211:
                        institution = ai_institution
                        is_ai_rulebook_match = True
                        has_ai_rulebook_match = True
                    else:
                        has_invalid_ai_rulebook_reference = True
                else:
                    has_invalid_ai_rulebook_reference = True
            elif education.ai_institution_roster_id is not None:
                has_invalid_ai_rulebook_reference = True
        classification = classify_education_institution(
            school_name_raw=education.school_name_raw,
            degree=education.degree,
            evidence_text=evidence_text,
            evidence_block_ids=education.evidence_block_ids,
            registry_roster_id=institution.roster_id if institution is not None else None,
        )
        is_higher_education_match = (
            classification.basis == "moe_higher_education_registry"
        )
        is_matched = classification.classification is not None
        if not is_matched:
            has_unresolved_school = True
        if classification.classification in {"985", "211"}:
            has_985_211 = True
        elif classification.classification is not None:
            has_known_non_985_211 = True
        # New writes have one exact category, or no category when the source
        # cannot prove it.  Keep the JSON column for old snapshots/filters but
        # do not let unverified model-provided legacy labels create a false
        # school type.
        institution_tiers = (
            [classification.classification]
            if classification.classification is not None
            else []
        )
        gpa_percent = (
            round(education.gpa_value / education.gpa_scale * 100, 4)
            if education.gpa_value is not None and education.gpa_scale is not None
            else None
        )
        rank_percent = (
            round(education.rank_position / education.rank_total * 100, 4)
            if education.rank_position is not None and education.rank_total is not None
            else None
        )
        session.add(
            ResumeEducation(
                resume_id=resume.id,
                school_name_raw=education.school_name_raw.strip(),
                school_key=(
                    institution.canonical_key
                    if institution is not None
                    else normalized_key(education.school_name_raw)
                ),
                institution_id=institution.id if institution is not None else None,
                school_match_state=(
                    "exact"
                    if is_local_match
                    else (
                        "higher_education_registry"
                        if is_higher_education_match
                        else (
                            "source_evidence"
                            if classification.basis == "source_evidence"
                            else (
                                "ai_rulebook"
                                if is_ai_rulebook_match
                                else (
                                    "ai_non_member"
                                    if force_pending_review
                                    else (
                                        "manual"
                                        if (
                                            request.complete_review
                                            and request.is_985_211_override is not None
                                        )
                                        else "unmatched"
                                    )
                                )
                            )
                        )
                    )
                ),
                degree=education.degree,
                major_raw=education.major_raw.strip() if education.major_raw else None,
                major_key=normalized_key(education.major_raw) or None,
                start_month=education.start_month,
                end_month=education.end_month,
                institution_tiers=institution_tiers,
                institution_classification=classification.classification,
                classification_basis=classification.basis,
                classification_registry_version=classification.registry_version,
                classification_evidence_block_ids=list(
                    classification.evidence_block_ids
                ),
                average_score=education.average_score,
                gpa_value=education.gpa_value,
                gpa_scale=education.gpa_scale,
                gpa_percent=gpa_percent,
                rank_position=education.rank_position,
                rank_total=education.rank_total,
                rank_percent=rank_percent,
                evidence_block_ids=education.evidence_block_ids,
            )
        )

    employment_intervals: list[tuple[str | None, str | None, bool]] = []
    employment_or_internship_intervals: list[tuple[str | None, str | None, bool]] = []
    for experience in facts.experiences:
        evidence_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=experience.evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=experience.experience_name_raw,
            source_text=evidence_text,
            label="experience_name_raw",
        )
        _assert_raw_value_grounded(
            value=experience.organization_name_raw,
            source_text=evidence_text,
            label="organization_name_raw",
        )
        _assert_raw_value_grounded(
            value=experience.title_raw,
            source_text=evidence_text,
            label="title_raw",
        )
        _assert_raw_value_grounded(
            value=experience.leadership_role,
            source_text=evidence_text,
            label="leadership_role",
        )
        _assert_raw_value_grounded(
            value=experience.award_result_raw,
            source_text=evidence_text,
            label="award_result_raw",
        )
        classification_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=(
                experience.classification_evidence_block_ids
                or experience.evidence_block_ids
            ),
        )
        stored_detail_items: list[dict[str, object]] = []
        for detail in experience.detail_items:
            detail_source_text = _source_text_by_ids(
                session,
                resume_id=resume.id,
                block_ids=detail.evidence_block_ids,
            )
            _assert_raw_value_grounded(
                value=detail.detail_raw,
                source_text=detail_source_text,
                label="experience_detail_raw",
            )
            stored_detail_items.append(
                {
                    "detail_raw": detail.detail_raw.strip(),
                    "evidence_block_ids": detail.evidence_block_ids,
                }
            )
        stored_experience_type = experience.experience_type
        if experience.experience_type in {"employment", "internship"}:
            needs_override = (
                not _has_work_context(classification_text, experience.experience_type)
                or _only_non_work_context(classification_text)
            )
            if needs_override:
                if force_pending_review:
                    # Keep the grounded AI result useful even when one entry
                    # may be a project rather than employment. Store it as
                    # unknown so it cannot inflate filters or calculated
                    # tenure, while the rest of the resume can be enabled.
                    stored_experience_type = "unknown"
                    has_ambiguous_work_context = True
                elif not (
                    request.complete_review
                    and request.review_note
                    and request.review_note.strip()
                ):
                    raise FactValidationError("work_context_requires_manual_review_note")
        session.add(
            ResumeExperience(
                resume_id=resume.id,
                experience_type=stored_experience_type,
                experience_name_raw=(
                    experience.experience_name_raw.strip()
                    if experience.experience_name_raw
                    else None
                ),
                experience_name_key=normalized_key(experience.experience_name_raw) or None,
                organization_name_raw=(
                    experience.organization_name_raw.strip()
                    if experience.organization_name_raw
                    else None
                ),
                organization_key=normalized_key(experience.organization_name_raw) or None,
                title_raw=experience.title_raw.strip() if experience.title_raw else None,
                title_key=normalized_key(experience.title_raw) or None,
                start_month=experience.start_month,
                end_month=experience.end_month,
                is_current=experience.is_current,
                evidence_block_ids=experience.evidence_block_ids,
                classification_evidence_block_ids=experience.classification_evidence_block_ids,
                detail_items=stored_detail_items,
                leadership_context=experience.leadership_context,
                leadership_role=(
                    experience.leadership_role.strip()
                    if experience.leadership_role
                    else None
                ),
                award_level=experience.award_level,
                award_result_raw=(
                    experience.award_result_raw.strip()
                    if experience.award_result_raw
                    else None
                ),
            )
        )
        interval = (experience.start_month, experience.end_month, experience.is_current)
        if stored_experience_type == "employment":
            employment_intervals.append(interval)
            employment_or_internship_intervals.append(interval)
        elif stored_experience_type == "internship":
            employment_or_internship_intervals.append(interval)

    seen_skill_keys: set[str] = set()
    for skill in facts.skills:
        evidence_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=skill.evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=skill.skill_display,
            source_text=evidence_text,
            label="skill_display",
        )
        key = normalized_key(skill.skill_display)
        if not key or key in seen_skill_keys:
            continue
        seen_skill_keys.add(key)
        session.add(
            ResumeSkill(
                resume_id=resume.id,
                skill_key=key,
                skill_display=skill.skill_display.strip(),
                skill_category=skill.skill_category,
                evidence_block_ids=skill.evidence_block_ids,
            )
        )

    for credential in facts.language_credentials:
        evidence_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=credential.evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=credential.credential_name_raw,
            source_text=evidence_text,
            label="credential_name_raw",
        )
        normalized_code = normalize_language_credential(
            credential.credential_name_raw
        )
        if credential.credential_code != "custom" and normalized_code != credential.credential_code:
            raise FactValidationError("language_credential_code_not_grounded")
        if credential.score is not None and not normalized_contains(
            evidence_text, f"{credential.score:g}"
        ):
            raise FactValidationError("language_credential_score_not_grounded")
        session.add(
            ResumeLanguageCredential(
                resume_id=resume.id,
                credential_code=credential.credential_code,
                credential_name_raw=credential.credential_name_raw.strip(),
                score=credential.score,
                passed=credential.passed,
                evidence_block_ids=credential.evidence_block_ids,
            )
        )

    for scholarship in facts.scholarships:
        evidence_text = _source_text_by_ids(
            session,
            resume_id=resume.id,
            block_ids=scholarship.evidence_block_ids,
        )
        _assert_raw_value_grounded(
            value=scholarship.scholarship_name_raw,
            source_text=evidence_text,
            label="scholarship_name_raw",
        )
        session.add(
            ResumeScholarship(
                resume_id=resume.id,
                scholarship_name_raw=scholarship.scholarship_name_raw.strip(),
                scholarship_name_key=normalized_key(
                    scholarship.scholarship_name_raw
                ),
                scholarship_level=scholarship.scholarship_level,
                evidence_block_ids=scholarship.evidence_block_ids,
            )
        )

    if force_pending_review:
        # The user chose a binary product rule: a validated positive hit is
        # true; no positive hit (including ambiguous/missing school text) is
        # false. The AI path only reaches this branch after source grounding.
        resume.is_985_211 = has_985_211
    elif has_985_211:
        if request.is_985_211_override is False:
            raise FactValidationError("cannot_override_matched_985_211_to_false")
        resume.is_985_211 = True
    elif has_unresolved_school:
        if request.complete_review:
            if request.is_985_211_override is None:
                raise FactValidationError("school_review_requires_985_211_override")
            resume.is_985_211 = request.is_985_211_override
        else:
            resume.is_985_211 = None
    elif has_known_non_985_211:
        # An exact domestic Ministry of Education roster match, explicit
        # secondary-vocational evidence, or explicit overseas study evidence
        # is enough to establish that this record is not a historical 985/211
        # institution.  This is not inferred from degree wording alone.
        resume.is_985_211 = False
    else:
        # The local registry contains historical 985/211 institutions only.
        # Reaching this branch is defensive, but it must still not manufacture
        # a negative classification.
        resume.is_985_211 = None

    quality_flags = set(resume.quality_flags or [])
    if force_pending_review:
        quality_flags.discard("school_unresolved")
    elif resume.is_985_211 is None:
        quality_flags.add("school_unresolved")
    else:
        quality_flags.discard("school_unresolved")
    if has_ai_rulebook_match:
        quality_flags.add("ai_985_211_rulebook_match")
    else:
        quality_flags.discard("ai_985_211_rulebook_match")
    if has_invalid_ai_rulebook_reference:
        quality_flags.add("ai_985_211_invalid_rulebook_reference")
    else:
        quality_flags.discard("ai_985_211_invalid_rulebook_reference")
    if has_ambiguous_work_context:
        quality_flags.add("work_context_ambiguous")
    else:
        quality_flags.discard("work_context_ambiguous")
    resume.quality_flags = sorted(quality_flags)
    resume.highest_degree = highest_degree([item.degree for item in facts.education])
    resume.employment_months = merged_month_count(employment_intervals)
    resume.employment_or_internship_months = merged_month_count(
        employment_or_internship_intervals
    )
    # Demographics are only ever written from a stated value. An enrichment
    # merge that re-stated no gender/birth date must not wipe what an earlier
    # extraction already grounded, and a fresh resume row simply stays null.
    if facts.gender_raw:
        resume.gender = _normalize_gender(facts.gender_raw)
    if facts.birth_date_raw:
        resume.birth_date = _normalize_birth_date(facts.birth_date_raw)
    resume.facts_version += 1

    if resume.extraction_status == "failed":
        raise FactValidationError("failed_resume_cannot_be_completed")
    if force_pending_review and auto_activate:
        # The extraction worker has already rejected ungrounded facts. A
        # successful AI result therefore becomes the candidate's active,
        # searchable resume without a separate human-confirmation step.
        new_status = "ready"
        new_is_active = True
        action = "ai_facts_auto_activated"
    elif force_pending_review:
        new_status = "needs_review"
        new_is_active = False
        action = "ai_facts_saved_pending_review"
    elif resume.is_985_211 is None:
        new_status = "needs_review"
        new_is_active = False
        action = "facts_saved_pending_school_review"
    elif resume.extraction_status == "needs_review" and not request.complete_review:
        new_status = "needs_review"
        new_is_active = False
        action = "facts_saved_pending_review"
    else:
        new_status = "ready"
        new_is_active = True
        action = "manual_review_completed" if request.complete_review else "facts_saved"

    if new_is_active:
        session.execute(
            update(Resume)
            .where(
                Resume.candidate_id == resume.candidate_id,
                Resume.organization_id == resume.organization_id,
                Resume.id != resume.id,
            )
            .values(is_active=False)
        )
    resume.extraction_status = new_status
    resume.is_active = new_is_active
    session.flush()

    _create_fact_snapshot(session, resume=resume, created_by=created_by)
    # A current summary is only meaningful for the exact immutable fact
    # snapshot it was generated from.  Saving facts always creates a new
    # snapshot, so leave old summaries as history instead of presenting one as
    # current for changed candidate data.
    session.execute(
        update(ResumeSummary)
        .where(
            ResumeSummary.resume_id == resume.id,
            ResumeSummary.organization_id == resume.organization_id,
            ResumeSummary.is_current.is_(True),
        )
        .values(is_current=False, status="stale")
    )

    session.add(
        ResumeReviewAction(
            resume_id=resume.id,
            action=action,
            note=request.review_note.strip() if request.review_note else None,
            old_values=old_snapshot,
            new_values=_fact_snapshot(resume),
        )
    )


def save_facts(
    session: Session,
    *,
    resume_id: str,
    request: ResumeFactsSaveRequest,
    created_by: str = "single_admin",
    force_pending_review: bool = False,
    auto_activate: bool = False,
) -> Resume:
    resume = get_resume(session, resume_id)
    if resume.extraction_status == "failed":
        raise FactValidationError("failed_resume_cannot_accept_facts")
    if resume.extraction_status == "ready" and not resume.is_active:
        # A historical ready version is immutable for the screening index.  It
        # must not silently displace the candidate's newer active resume just
        # because somebody re-saved its facts.
        raise FactValidationError("inactive_ready_resume_requires_explicit_activation")
    if request.complete_review:
        if not request.review_note or not request.review_note.strip():
            raise FactValidationError("review_note_required_to_complete_review")
    if force_pending_review and request.complete_review:
        raise FactValidationError("ai_extraction_cannot_complete_human_review")
    if auto_activate and not force_pending_review:
        raise FactValidationError("auto_activation_requires_ai_extraction")
    _replace_facts(
        session,
        resume,
        request,
        created_by=created_by,
        force_pending_review=force_pending_review,
        auto_activate=auto_activate,
    )
    session.flush()
    return resume


def merge_filter_v2_enrichment(
    session: Session,
    *,
    resume_id: str,
    enrichment: ResumeFactsSubmission,
) -> ResumeFactsSubmission:
    """Merge a V2 extraction into current grounded facts without deleting them.

    Historical resumes can lack V2-only attributes. The enrichment worker runs
    against the same source blocks, then overlays only matching rows and adds
    newly discovered source-cited rows. Existing facts remain the baseline.
    """

    resume = get_resume(session, resume_id)
    education: list[dict[str, object]] = [
        {
            "school_name_raw": item.school_name_raw,
            "degree": item.degree,
            "major_raw": item.major_raw,
            "start_month": item.start_month,
            "end_month": item.end_month,
            "institution_tiers": item.institution_tiers or [],
            "average_score": item.average_score,
            "gpa_value": item.gpa_value,
            "gpa_scale": item.gpa_scale,
            "rank_position": item.rank_position,
            "rank_total": item.rank_total,
            "evidence_block_ids": item.evidence_block_ids or [],
        }
        for item in resume.educations
    ]
    education_by_key = {
        (normalized_key(item["school_name_raw"]), item["degree"]): item
        for item in education
    }
    for incoming in enrichment.education:
        values = incoming.model_dump(
            exclude={"ai_985_211_judgment", "ai_institution_roster_id"}
        )
        key = (normalized_key(incoming.school_name_raw), incoming.degree)
        existing = education_by_key.get(key)
        if existing is None:
            education.append(values)
            education_by_key[key] = values
            continue
        for field in (
            "institution_tiers",
            "average_score",
            "gpa_value",
            "gpa_scale",
            "rank_position",
            "rank_total",
        ):
            value = values.get(field)
            if value not in (None, []):
                existing[field] = value

    experiences: list[dict[str, object]] = [
        {
            "experience_type": item.experience_type,
            "experience_name_raw": item.experience_name_raw,
            "organization_name_raw": item.organization_name_raw,
            "title_raw": item.title_raw,
            "start_month": item.start_month,
            "end_month": item.end_month,
            "is_current": item.is_current,
            "evidence_block_ids": item.evidence_block_ids or [],
            "classification_evidence_block_ids": (
                item.classification_evidence_block_ids or []
            ),
            "detail_items": item.detail_items or [],
            "leadership_context": item.leadership_context,
            "leadership_role": item.leadership_role,
            "award_level": item.award_level,
            "award_result_raw": item.award_result_raw,
        }
        for item in resume.experiences
    ]

    def experience_key(value: object) -> tuple[str, str, str]:
        return (
            normalized_key(value.experience_name_raw),
            normalized_key(value.organization_name_raw),
            normalized_key(value.title_raw),
        )

    experience_by_key = {
        (
            normalized_key(item["experience_name_raw"]),
            normalized_key(item["organization_name_raw"]),
            normalized_key(item["title_raw"]),
        ): item
        for item in experiences
    }
    for incoming in enrichment.experiences:
        values = incoming.model_dump()
        key = experience_key(incoming)
        existing = experience_by_key.get(key)
        if existing is None:
            experiences.append(values)
            experience_by_key[key] = values
            continue
        if existing["experience_type"] in {"unknown", "other"}:
            existing["experience_type"] = incoming.experience_type
        for field in (
            "leadership_context",
            "leadership_role",
            "award_level",
            "award_result_raw",
        ):
            if values.get(field) is not None:
                existing[field] = values[field]

    skills: list[dict[str, object]] = [
        {
            "skill_display": item.skill_display,
            "skill_category": item.skill_category,
            "evidence_block_ids": item.evidence_block_ids or [],
        }
        for item in resume.skills
    ]
    skill_by_key = {normalized_key(item["skill_display"]): item for item in skills}
    for incoming in enrichment.skills:
        existing = skill_by_key.get(normalized_key(incoming.skill_display))
        if existing is None:
            values = incoming.model_dump()
            skills.append(values)
            skill_by_key[normalized_key(incoming.skill_display)] = values
        elif incoming.skill_category is not None:
            existing["skill_category"] = incoming.skill_category

    language_credentials = [
        {
            "credential_code": item.credential_code,
            "credential_name_raw": item.credential_name_raw,
            "score": item.score,
            "passed": item.passed,
            "evidence_block_ids": item.evidence_block_ids or [],
        }
        for item in resume.language_credentials
    ]
    language_keys = {
        (item["credential_code"], item["score"], normalized_key(item["credential_name_raw"]))
        for item in language_credentials
    }
    for incoming in enrichment.language_credentials:
        key = (
            incoming.credential_code,
            incoming.score,
            normalized_key(incoming.credential_name_raw),
        )
        if key not in language_keys:
            language_credentials.append(incoming.model_dump())
            language_keys.add(key)

    scholarships = [
        {
            "scholarship_name_raw": item.scholarship_name_raw,
            "scholarship_level": item.scholarship_level,
            "evidence_block_ids": item.evidence_block_ids or [],
        }
        for item in resume.scholarships
    ]
    scholarship_keys = {
        normalized_key(item["scholarship_name_raw"]) for item in scholarships
    }
    for incoming in enrichment.scholarships:
        key = normalized_key(incoming.scholarship_name_raw)
        if key not in scholarship_keys:
            scholarships.append(incoming.model_dump())
            scholarship_keys.add(key)

    return ResumeFactsSubmission.model_validate(
        {
            "schema_version": "resume_facts.v2",
            "gender_raw": enrichment.gender_raw,
            "gender_evidence_block_ids": enrichment.gender_evidence_block_ids or [],
            "birth_date_raw": enrichment.birth_date_raw,
            "birth_date_evidence_block_ids": enrichment.birth_date_evidence_block_ids or [],
            "education": education,
            "experiences": experiences,
            "skills": skills,
            "language_credentials": language_credentials,
            "scholarships": scholarships,
        }
    )


def activate_ready_resume(
    session: Session,
    *,
    resume_id: str,
    note: str | None = None,
) -> Resume:
    """Explicitly select a historical ready version for screening again."""

    resume = get_resume(session, resume_id)
    if resume.extraction_status != "ready" or resume.is_985_211 is None:
        raise FactValidationError("resume_must_be_ready_to_activate")
    old_active_resume_id = session.scalar(
        select(Resume.id).where(
            Resume.candidate_id == resume.candidate_id,
            Resume.organization_id == resume.organization_id,
            Resume.is_active.is_(True),
        )
    )
    session.execute(
        update(Resume)
        .where(
            Resume.candidate_id == resume.candidate_id,
            Resume.organization_id == resume.organization_id,
        )
        .values(is_active=False)
    )
    resume.is_active = True
    session.add(
        ResumeReviewAction(
            resume_id=resume.id,
            action="resume_version_activated",
            note=note.strip() if note else None,
            old_values={"active_resume_id": old_active_resume_id},
            new_values={"active_resume_id": resume.id},
        )
    )
    session.flush()
    return resume


def reconcile_legacy_completed_ai_resumes(session: Session) -> int:
    """Bring pre-auto-activation AI completions into the current ready state.

    Older builds could save a fully grounded AI extraction while leaving the
    resume in ``needs_review``. The current product has no human-confirmation
    step, so those completed records must not stay hidden indefinitely. A
    matching immutable snapshot is required before any transition; a newer
    active version for the same candidate is always left in place.
    """

    statement = (
        select(Resume)
        .join(ResumeAiExtractionJob)
        .where(
            ResumeAiExtractionJob.status == "completed",
            Resume.extraction_status == "needs_review",
            Resume.is_active.is_(False),
            Resume.facts_version > 0,
        )
        .order_by(Resume.created_at.desc(), Resume.id.desc())
    )
    reconciled = 0
    for resume in session.scalars(statement).all():
        snapshot_exists = session.scalar(
            select(ResumeFactSnapshot.id).where(
                ResumeFactSnapshot.resume_id == resume.id,
                ResumeFactSnapshot.organization_id == resume.organization_id,
                ResumeFactSnapshot.facts_version == resume.facts_version,
            )
        )
        if snapshot_exists is None:
            continue

        old_snapshot = _fact_snapshot(resume)
        if resume.is_985_211 is None:
            # The product contract is now binary: a verified positive hit is
            # true; every other grounded extraction is false.
            resume.is_985_211 = False
            flags = set(resume.quality_flags or [])
            flags.discard("school_unresolved")
            resume.quality_flags = sorted(flags)

        active_resume_id = session.scalar(
            select(Resume.id).where(
                Resume.candidate_id == resume.candidate_id,
                Resume.organization_id == resume.organization_id,
                Resume.is_active.is_(True),
            )
        )
        resume.extraction_status = "ready"
        if active_resume_id is None:
            session.execute(
                update(Resume)
                .where(
                    Resume.candidate_id == resume.candidate_id,
                    Resume.organization_id == resume.organization_id,
                )
                .values(is_active=False)
            )
            resume.is_active = True
            action = "legacy_ai_facts_auto_activated"
        else:
            # The newer active version remains the screening version. This
            # historical upload is still visible in the resume library.
            resume.is_active = False
            action = "legacy_ai_facts_marked_ready"

        session.add(
            ResumeReviewAction(
                resume_id=resume.id,
                action=action,
                note="Reconciled completed AI extraction from the pre-auto-activation workflow.",
                old_values=old_snapshot,
                new_values=_fact_snapshot(resume),
            )
        )
        reconciled += 1
    session.flush()
    return reconciled


def auto_extract_and_save_facts(
    session: Session,
    *,
    resume_id: str,
    settings: AppSettings,
) -> Resume:
    """Run the legacy synchronous extraction path through the AI gateway.

    New uploads use the durable worker, but this compatibility service is
    still called by older internal integrations.  It must never become a
    back door around platform-owned routing, credentials, or the cost ledger.
    The gateway records the route snapshot on its ``AiRun``; unlike a queued
    worker item there is no pre-existing durable request to pin beforehand.
    """

    if not ai_gateway_credentials_configured(settings):
        raise ResumeServiceError("deepseek_api_key_not_configured")
    resume = get_resume(session, resume_id)
    if resume.extraction_status != "text_ready":
        raise FactValidationError("resume_must_be_text_ready_for_ai_extraction")
    source_blocks = session.scalars(
        select(ResumeSourceBlock)
        .where(ResumeSourceBlock.resume_id == resume.id)
        .order_by(ResumeSourceBlock.page_no, ResumeSourceBlock.block_id)
    ).all()
    compatibility_api_key, compatibility_model, compatibility_timeout_seconds = (
        gateway_prompt_transport_arguments(settings)
    )
    try:
        with ai_gateway_execution(
            session,
            settings=settings,
            spec=AiExecutionSpec(
                feature="resume_extract_rich",
                business_ref_type="resume",
                business_ref_id=resume.id,
                prompt_revision="resume_facts.rich.v2",
                contract_version="resume_facts.rich.v2",
            ),
        ):
            # ``extract_resume_facts`` retains the prompt and strict evidence
            # validation contract.  Under the active gateway context its old
            # transport parameters are ignored; the platform route supplies
            # the provider, endpoint, credential, and effective model.
            facts = extract_resume_facts(
                api_key=compatibility_api_key,
                model=compatibility_model,
                timeout_seconds=compatibility_timeout_seconds,
                blocks=[
                    EvidenceBlock(
                        block_id=block.block_id,
                        page_no=block.page_no,
                        block_type=block.block_type,
                        text=block.text,
                    )
                    for block in source_blocks
                ],
            )
        return save_facts(
            session,
            resume_id=resume.id,
            request=ResumeFactsSaveRequest(facts=facts),
            created_by="ai:gateway",
            force_pending_review=True,
            auto_activate=True,
        )
    except (AiGatewayError, DeepSeekProviderError, FactValidationError) as exc:
        old_snapshot = _fact_snapshot(resume)
        flags = set(resume.quality_flags or [])
        flags.add(f"ai_extraction_{str(exc)}")
        resume.quality_flags = sorted(flags)
        resume.extraction_status = "needs_review"
        resume.is_active = False
        session.flush()
        session.add(
            ResumeReviewAction(
                resume_id=resume.id,
                action="ai_extraction_failed",
                note=str(exc),
                old_values=old_snapshot,
                new_values=_fact_snapshot(resume),
            )
        )
        session.flush()
        return resume
