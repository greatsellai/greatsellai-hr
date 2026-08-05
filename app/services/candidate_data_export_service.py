"""Asynchronous, workspace-scoped candidate data exports.

The HTTP layer only creates an opaque export request.  This service freezes a
safe snapshot of the selected candidate roots, lets a worker build the archive
outside the request transaction, and exposes the completed archive only via a
short-lived session-bound download grant.  It deliberately never writes
candidate content into audit events, task errors, storage keys, or logs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Literal

from openpyxl import Workbook
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import (
    Candidate,
    CandidateDataExport,
    CandidateDataFileAccessGrant,
    Job,
    JobMatch,
    Resume,
    ResumeFactSnapshot,
    ResumeScore,
    ResumeSummary,
)
from app.schemas import CandidateDataExportListResponse, CandidateDataExportResponse
from app.services.candidate_data_lifecycle_service import (
    CandidateDataLifecycleError,
    _record_audit,
    as_utc,
    utcnow,
)
from app.services.resume_service import ResumeServiceError, resolve_uploaded_resume_path
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)


EXPORT_QUEUED = "queued"
EXPORT_RUNNING = "running"
EXPORT_COMPLETED = "completed"
EXPORT_RETRYABLE_FAILED = "retryable_failed"
EXPORT_FAILED = "failed"
EXPORT_CANCELLED = "cancelled"
EXPORT_REVOKED = "revoked"
EXPORT_EXPIRED = "expired"

_EXPORT_ACCESS_RESOURCE_TYPE = "candidate_data_export"
_EXPORT_STORAGE_NAMESPACE = "candidate-data-exports"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SAFE_ATTEMPT_TOKEN = re.compile(r"^[a-f0-9]{16,64}$")
_FORMULA_PREFIXES = frozenset(("=", "+", "-", "@"))

# These fields either hold a full source/original, an access path, mail
# metadata, or raw model transport payload.  They must not travel in a normal
# structured-data export.  Structured facts such as ``school_name_raw`` and
# ``detail_raw`` are intentionally not in this set: they are the recruiter
# facing facts that the export is meant to contain.
_EXCLUDED_EXPORT_KEYS = frozenset(
    {
        "raw_text",
        "source_blocks",
        "source_block_ids",
        "evidence_block_ids",
        "classification_evidence_block_ids",
        "original_filename",
        "storage_key",
        "sha256",
        "attachment_filename",
        "email_address",
        "sender",
        "sender_address",
        "from_address",
        "mail_subject",
        "message_id",
        "prompt",
        "model_prompt",
        "raw_response",
        "model_response",
        "response_raw",
        "chain_of_thought",
        "reasoning_trace",
    }
)
_EXCLUDED_EXPORT_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _EXCLUDED_EXPORT_KEYS
)


class CandidateDataExportError(CandidateDataLifecycleError):
    """Stable, privacy-safe error codes for candidate data export work."""


@dataclass(frozen=True)
class CandidateDataExportDownloadAccess:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedCandidateDataExportDownload:
    path: Path
    filename: str
    purpose: Literal["download"]


@dataclass(frozen=True)
class ClaimedCandidateDataExport:
    export_id: str
    organization_id: str


@dataclass(frozen=True)
class _ExportCandidatePayload:
    candidate_id: str
    display_name: str | None
    resume_id: str
    facts_version: int
    fact_snapshot_id: str
    facts: dict[str, object]
    contact_details: list[dict[str, str]]
    summaries: list[dict[str, object]]
    scores: list[dict[str, object]]
    job_matches: list[dict[str, object]]
    original_path: Path | None
    original_suffix: str | None


@dataclass(frozen=True)
class _ExportBuildPayload:
    export_id: str
    organization_id: str
    include_originals: bool
    candidates: list[_ExportCandidatePayload]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _organization_session(session: Session, organization_id: str) -> Iterator[None]:
    """Install a concrete tenant scope for one worker unit of work."""

    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _safe_identifier(value: str, *, error_code: str) -> str:
    normalized = value.strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        raise CandidateDataExportError(error_code)
    return normalized


def _safe_original_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        return suffix
    return ".bin"


def _export_attempt_storage_key(
    *, organization_id: str, export_id: str, attempt_token: str
) -> str:
    organization = _safe_identifier(
        organization_id, error_code="candidate_data_export_storage_invalid"
    )
    export = _safe_identifier(export_id, error_code="candidate_data_export_storage_invalid")
    if not _SAFE_ATTEMPT_TOKEN.fullmatch(attempt_token):
        raise CandidateDataExportError("candidate_data_export_storage_invalid")
    return f"{_EXPORT_STORAGE_NAMESPACE}/{organization}/{export}-{attempt_token}.zip"


def resolve_candidate_data_export_path(
    settings: AppSettings,
    *,
    organization_id: str,
    export_id: str,
    storage_key: str,
    require_file: bool = True,
) -> Path:
    """Resolve a completed export strictly inside its isolated namespace."""

    organization = _safe_identifier(
        organization_id, error_code="candidate_data_export_output_not_found"
    )
    export = _safe_identifier(
        export_id, error_code="candidate_data_export_output_not_found"
    )
    expected_prefix = f"{_EXPORT_STORAGE_NAMESPACE}/{organization}/{export}-"
    if not storage_key.startswith(expected_prefix) or not storage_key.endswith(".zip"):
        raise CandidateDataExportError("candidate_data_export_output_not_found")
    suffix = storage_key[len(expected_prefix) : -len(".zip")]
    if not _SAFE_ATTEMPT_TOKEN.fullmatch(suffix) or "\\" in storage_key:
        raise CandidateDataExportError("candidate_data_export_output_not_found")
    # API and worker already share ``upload_dir`` in the approved production
    # topology.  Keep exports in a dedicated namespace below that durable
    # volume rather than ``data_dir``, which can be container-local and would
    # make a worker-produced archive invisible to the API process.
    namespace_directory = settings.upload_dir / _EXPORT_STORAGE_NAMESPACE
    workspace_directory = namespace_directory / organization
    if namespace_directory.is_symlink() or workspace_directory.is_symlink():
        raise CandidateDataExportError("candidate_data_export_output_not_found")
    root = workspace_directory.resolve()
    candidate = (settings.upload_dir / storage_key).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CandidateDataExportError("candidate_data_export_output_not_found") from exc
    if candidate.is_symlink() or (require_file and (not candidate.is_file() or candidate.is_symlink())):
        raise CandidateDataExportError("candidate_data_export_output_not_found")
    return candidate


def _export_response(export: CandidateDataExport, *, now: datetime | None = None) -> CandidateDataExportResponse:
    current = now or utcnow()
    status = export.status
    expires_at = as_utc(export.expires_at)
    if (
        status == EXPORT_COMPLETED
        and export.revoked_at is None
        and expires_at is not None
        and expires_at <= current
    ):
        # Do not mutate state in a list/get response.  The worker cleanup path
        # owns the durable transition and file removal.
        status = EXPORT_EXPIRED
    return CandidateDataExportResponse(
        export_id=export.id,
        status=status,
        item_count=export.item_count,
        include_originals=export.include_originals,
        requested_at=export.requested_at,
        completed_at=export.completed_at,
        expires_at=export.expires_at,
        error_code=export.last_error,
    )


def _visible_export(session: Session, *, export_id: str) -> CandidateDataExport:
    export = session.scalar(
        select(CandidateDataExport).where(CandidateDataExport.id == export_id)
    )
    if export is None:
        raise CandidateDataExportError("candidate_data_export_not_found")
    return export


def _snapshot_selection(
    session: Session,
    *,
    candidate_ids: list[str],
) -> list[dict[str, object]]:
    """Freeze only opaque IDs and version references, never candidate content."""

    rows = session.execute(
        select(Candidate, Resume, ResumeFactSnapshot)
        .join(Resume, Resume.candidate_id == Candidate.id)
        .join(
            ResumeFactSnapshot,
            (ResumeFactSnapshot.resume_id == Resume.id)
            & (ResumeFactSnapshot.facts_version == Resume.facts_version),
        )
        .where(
            Candidate.id.in_(candidate_ids),
            Resume.is_active.is_(True),
            ResumeFactSnapshot.facts_version == Resume.facts_version,
        )
    ).all()
    by_candidate_id = {candidate.id: (candidate, resume, fact_snapshot) for candidate, resume, fact_snapshot in rows}
    if len(by_candidate_id) != len(candidate_ids):
        # A caller must not learn whether an unknown identifier belongs to a
        # different workspace, has no active resume, or lacks a verified facts
        # snapshot.  The public API maps this uniformly to 404.
        raise CandidateDataExportError("candidate_data_export_candidate_not_found")

    resume_ids = [row[1].id for row in by_candidate_id.values()]
    fact_snapshot_ids = [row[2].id for row in by_candidate_id.values()]
    summaries_by_resume: dict[str, list[str]] = {resume_id: [] for resume_id in resume_ids}
    scores_by_resume: dict[str, list[str]] = {resume_id: [] for resume_id in resume_ids}
    matches_by_resume: dict[str, list[str]] = {resume_id: [] for resume_id in resume_ids}
    if resume_ids:
        snapshots_by_resume = {resume.id: fact_snapshot.id for _, resume, fact_snapshot in by_candidate_id.values()}
        versions_by_resume = {resume.id: fact_snapshot.facts_version for _, resume, fact_snapshot in by_candidate_id.values()}
        summaries = session.scalars(
            select(ResumeSummary).where(
                ResumeSummary.resume_id.in_(resume_ids),
                ResumeSummary.is_current.is_(True),
            )
        ).all()
        for summary in summaries:
            expected_snapshot = snapshots_by_resume.get(summary.resume_id)
            expected_version = versions_by_resume.get(summary.resume_id)
            if expected_snapshot is not None and (
                summary.fact_snapshot_id == expected_snapshot
                or (
                    summary.fact_snapshot_id is None
                    and summary.facts_version == expected_version
                )
            ):
                summaries_by_resume[summary.resume_id].append(summary.id)

        scores = session.scalars(
            select(ResumeScore).where(ResumeScore.resume_id.in_(resume_ids))
        ).all()
        for score in scores:
            expected_snapshot = snapshots_by_resume.get(score.resume_id)
            expected_version = versions_by_resume.get(score.resume_id)
            if expected_snapshot is not None and (
                score.fact_snapshot_id == expected_snapshot
                or (score.fact_snapshot_id is None and score.facts_version == expected_version)
            ):
                scores_by_resume[score.resume_id].append(score.id)

        matches = session.scalars(
            select(JobMatch)
            .join(Job, Job.id == JobMatch.job_id)
            .where(
                JobMatch.resume_id.in_(resume_ids),
                JobMatch.fact_snapshot_id.in_(fact_snapshot_ids),
                Job.kind == "job",
            )
        ).all()
        for match in matches:
            if match.fact_snapshot_id == snapshots_by_resume.get(match.resume_id):
                matches_by_resume[match.resume_id].append(match.id)

    snapshot: list[dict[str, object]] = []
    for candidate_id in candidate_ids:
        _, resume, fact_snapshot = by_candidate_id[candidate_id]
        snapshot.append(
            {
                "candidate_id": candidate_id,
                "resume_id": resume.id,
                "fact_snapshot_id": fact_snapshot.id,
                "facts_version": fact_snapshot.facts_version,
                "summary_ids": sorted(summaries_by_resume[resume.id]),
                "score_ids": sorted(scores_by_resume[resume.id]),
                "job_match_ids": sorted(matches_by_resume[resume.id]),
            }
        )
    return snapshot


def create_candidate_data_export(
    session: Session,
    *,
    settings: AppSettings,
    candidate_ids: list[str],
    include_originals: bool,
    actor_user_id: str | None,
    request_id: str | None = None,
) -> CandidateDataExportResponse:
    """Create an asynchronous, immutable export request for visible roots."""

    normalized_ids = [candidate_id.strip() for candidate_id in candidate_ids]
    if (
        not normalized_ids
        or any(not candidate_id for candidate_id in normalized_ids)
        or len(set(normalized_ids)) != len(normalized_ids)
        or len(normalized_ids) > settings.candidate_data_export_max_items
    ):
        raise CandidateDataExportError("candidate_data_export_candidate_selection_invalid")
    snapshot = _snapshot_selection(session, candidate_ids=normalized_ids)
    now = utcnow()
    export = CandidateDataExport(
        organization_id=organization_context_id(session),
        requested_by_user_id=actor_user_id,
        status=EXPORT_QUEUED,
        snapshot_json=snapshot,
        include_originals=include_originals,
        item_count=len(snapshot),
        next_attempt_at=now,
        requested_at=now,
        expires_at=now + timedelta(seconds=settings.candidate_data_export_ttl_seconds),
    )
    session.add(export)
    session.flush()
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "legacy_member",
        action="candidate_data_export_requested",
        target_type="candidate_data_export",
        target_id=export.id,
        request_id=request_id,
        source_kind="web",
        result="queued",
    )
    session.flush()
    return _export_response(export, now=now)


def get_candidate_data_export(
    session: Session,
    *,
    export_id: str,
) -> CandidateDataExportResponse:
    return _export_response(_visible_export(session, export_id=export_id))


def list_candidate_data_exports(
    session: Session,
    *,
    limit: int = 50,
) -> CandidateDataExportListResponse:
    bounded_limit = min(max(limit, 1), 100)
    exports = session.scalars(
        select(CandidateDataExport)
        .order_by(CandidateDataExport.requested_at.desc(), CandidateDataExport.id.desc())
        .limit(bounded_limit)
    ).all()
    total = int(session.scalar(select(func.count(CandidateDataExport.id))) or 0)
    now = utcnow()
    return CandidateDataExportListResponse(
        items=[_export_response(export, now=now) for export in exports], total=total
    )


def _revoke_export_download_grants(
    session: Session,
    *,
    export_id: str,
    now: datetime,
) -> None:
    session.execute(
        update(CandidateDataFileAccessGrant)
        .where(
            CandidateDataFileAccessGrant.organization_id == organization_context_id(session),
            CandidateDataFileAccessGrant.resource_type == _EXPORT_ACCESS_RESOURCE_TYPE,
            CandidateDataFileAccessGrant.resource_id == export_id,
            CandidateDataFileAccessGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


def cancel_candidate_data_export(
    session: Session,
    *,
    export_id: str,
    actor_user_id: str | None,
    request_id: str | None = None,
) -> CandidateDataExportResponse:
    """Immediately revoke access; a worker later removes any output bytes."""

    export = _visible_export(session, export_id=export_id)
    now = utcnow()
    if export.status in {EXPORT_CANCELLED, EXPORT_REVOKED, EXPORT_EXPIRED}:
        return _export_response(export, now=now)
    if export.status == EXPORT_FAILED:
        raise CandidateDataExportError("candidate_data_export_not_cancellable")
    export.status = EXPORT_REVOKED if export.status == EXPORT_COMPLETED else EXPORT_CANCELLED
    export.revoked_at = now
    export.expires_at = now
    export.next_attempt_at = None
    export.lease_owner = None
    export.lease_expires_at = None
    export.completed_at = export.completed_at or now
    _revoke_export_download_grants(session, export_id=export.id, now=now)
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "legacy_member",
        action="candidate_data_export_cancelled",
        target_type="candidate_data_export",
        target_id=export.id,
        request_id=request_id,
        source_kind="web",
        result=export.status,
    )
    session.flush()
    return _export_response(export, now=now)


def authorize_candidate_data_export_download(
    session: Session,
    *,
    settings: AppSettings,
    export_id: str,
    actor_user_id: str | None,
    session_nonce: str,
    request_id: str | None = None,
    source_kind: str = "web",
) -> CandidateDataExportDownloadAccess:
    """Create one audited, short-lived export download grant."""

    if not session_nonce:
        raise CandidateDataExportError("candidate_data_session_nonce_missing")
    export = _visible_export(session, export_id=export_id)
    now = utcnow()
    if (
        export.status != EXPORT_COMPLETED
        or export.revoked_at is not None
        or as_utc(export.expires_at) is None
        or as_utc(export.expires_at) <= now
        or not export.output_storage_key
    ):
        raise CandidateDataExportError("candidate_data_export_download_not_found")
    try:
        resolve_candidate_data_export_path(
            settings,
            organization_id=export.organization_id,
            export_id=export.id,
            storage_key=export.output_storage_key,
        )
    except CandidateDataExportError as exc:
        raise CandidateDataExportError("candidate_data_export_download_not_found") from exc

    token = secrets.token_urlsafe(32)
    grant = CandidateDataFileAccessGrant(
        organization_id=export.organization_id,
        actor_user_id=actor_user_id,
        resource_type=_EXPORT_ACCESS_RESOURCE_TYPE,
        resource_id=export.id,
        purpose="download",
        token_digest=_digest(token),
        session_nonce_digest=_digest(session_nonce),
        expires_at=min(
            as_utc(export.expires_at) or now,
            now + timedelta(seconds=settings.candidate_data_file_access_ttl_seconds),
        ),
    )
    session.add(grant)
    _record_audit(
        session,
        actor_user_id=actor_user_id,
        actor_kind="user" if actor_user_id else "legacy_member",
        action="candidate_data_export_download_authorized",
        target_type="candidate_data_export",
        target_id=export.id,
        request_id=request_id,
        source_kind=source_kind,
    )
    session.flush()
    return CandidateDataExportDownloadAccess(token=token, expires_at=grant.expires_at)


def resolve_candidate_data_export_download(
    session: Session,
    *,
    settings: AppSettings,
    opaque_token: str,
    actor_user_id: str | None,
    session_nonce: str,
) -> ResolvedCandidateDataExportDownload:
    """Resolve a grant without creating a duplicate audit event on retries."""

    if not opaque_token or not session_nonce:
        raise CandidateDataExportError("candidate_data_export_download_not_found")
    grant = session.scalar(
        select(CandidateDataFileAccessGrant).where(
            CandidateDataFileAccessGrant.token_digest == _digest(opaque_token),
            CandidateDataFileAccessGrant.resource_type == _EXPORT_ACCESS_RESOURCE_TYPE,
            CandidateDataFileAccessGrant.purpose == "download",
            CandidateDataFileAccessGrant.actor_user_id == actor_user_id,
            CandidateDataFileAccessGrant.session_nonce_digest == _digest(session_nonce),
            CandidateDataFileAccessGrant.revoked_at.is_(None),
        )
    )
    if grant is None or as_utc(grant.expires_at) is None or as_utc(grant.expires_at) <= utcnow():
        raise CandidateDataExportError("candidate_data_export_download_not_found")
    export = _visible_export(session, export_id=grant.resource_id)
    if (
        export.status != EXPORT_COMPLETED
        or export.revoked_at is not None
        or as_utc(export.expires_at) is None
        or as_utc(export.expires_at) <= utcnow()
        or not export.output_storage_key
    ):
        raise CandidateDataExportError("candidate_data_export_download_not_found")
    try:
        path = resolve_candidate_data_export_path(
            settings,
            organization_id=export.organization_id,
            export_id=export.id,
            storage_key=export.output_storage_key,
        )
    except CandidateDataExportError as exc:
        raise CandidateDataExportError("candidate_data_export_download_not_found") from exc
    return ResolvedCandidateDataExportDownload(
        path=path,
        filename=f"candidate-data-export-{export.id}.zip",
        purpose="download",
    )


def _safe_export_value(value: object) -> object:
    """Recursively strip raw source and transport fields from export payloads."""

    if isinstance(value, dict):
        clean: dict[str, object] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.casefold()
            compact = re.sub(r"[^a-z0-9]", "", normalized)
            if (
                normalized in _EXCLUDED_EXPORT_KEYS
                or compact in _EXCLUDED_EXPORT_COMPACT_KEYS
                or normalized.endswith("_source_block_ids")
                or compact.endswith("sourceblockids")
            ):
                continue
            clean[key] = _safe_export_value(nested_value)
        return clean
    if isinstance(value, list):
        return [_safe_export_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_export_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Database JSON should already be built-in data, but unknown values must
    # never fall back to a repr that could include a secret or filesystem path.
    return None


def _load_snapshot_facts(fact_snapshot: ResumeFactSnapshot) -> dict[str, object]:
    try:
        parsed = json.loads(fact_snapshot.canonical_facts_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid") from exc
    if not isinstance(parsed, dict):
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
    safe = _safe_export_value(parsed)
    if not isinstance(safe, dict):
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
    return safe


def _safe_contact_details(value: object) -> list[dict[str, str]]:
    """Project local contacts into an entitled data export without evidence."""

    if not isinstance(value, list):
        return []
    contacts: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        contact_value = item.get("value")
        if kind not in {"email", "phone"} or not isinstance(contact_value, str):
            continue
        normalized_value = contact_value.strip()
        if normalized_value:
            contacts.append({"kind": kind, "value": normalized_value})
    return contacts


def _snapshot_entry_value(entry: dict[str, object], key: str) -> object:
    value = entry.get(key)
    return value


def _snapshot_entry_ids(entry: dict[str, object], key: str) -> list[str]:
    value = entry.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
    return list(value)


def _load_export_build_payload(
    session: Session,
    *,
    settings: AppSettings,
    export_id: str,
    worker_id: str,
) -> _ExportBuildPayload:
    export = session.scalar(
        select(CandidateDataExport).where(
            CandidateDataExport.id == export_id,
            CandidateDataExport.status == EXPORT_RUNNING,
            CandidateDataExport.lease_owner == worker_id,
            CandidateDataExport.revoked_at.is_(None),
        )
    )
    if export is None:
        raise CandidateDataExportError("candidate_data_export_lease_lost")
    if as_utc(export.expires_at) is None or as_utc(export.expires_at) <= utcnow():
        raise CandidateDataExportError("candidate_data_export_expired")
    if not isinstance(export.snapshot_json, list) or not export.snapshot_json:
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
    if len(export.snapshot_json) != export.item_count:
        raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
    entries: list[dict[str, object]] = []
    for value in export.snapshot_json:
        if not isinstance(value, dict):
            raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
        entries.append(value)

    # Keep settings outside any database JSON/session field.  It is passed to
    # the resolver below through a tiny local closure rather than persisted.
    payload_candidates: list[_ExportCandidatePayload] = []
    for entry in entries:
        candidate_id = _snapshot_entry_value(entry, "candidate_id")
        resume_id = _snapshot_entry_value(entry, "resume_id")
        fact_snapshot_id = _snapshot_entry_value(entry, "fact_snapshot_id")
        facts_version = _snapshot_entry_value(entry, "facts_version")
        if not all(isinstance(value, str) and value for value in (candidate_id, resume_id, fact_snapshot_id)) or not isinstance(facts_version, int):
            raise CandidateDataExportError("candidate_data_export_snapshot_invalid")
        row = session.execute(
            select(Candidate, Resume, ResumeFactSnapshot)
            .join(Resume, Resume.candidate_id == Candidate.id)
            .join(ResumeFactSnapshot, ResumeFactSnapshot.resume_id == Resume.id)
            .where(
                Candidate.id == candidate_id,
                Resume.id == resume_id,
                ResumeFactSnapshot.id == fact_snapshot_id,
                ResumeFactSnapshot.facts_version == facts_version,
            )
        ).first()
        if row is None:
            raise CandidateDataExportError("candidate_data_export_snapshot_unavailable")
        candidate, resume, fact_snapshot = row
        if resume.facts_version != facts_version:
            raise CandidateDataExportError("candidate_data_export_snapshot_unavailable")
        summary_ids = _snapshot_entry_ids(entry, "summary_ids")
        score_ids = _snapshot_entry_ids(entry, "score_ids")
        job_match_ids = _snapshot_entry_ids(entry, "job_match_ids")
        summaries = session.scalars(
            select(ResumeSummary)
            .join(Resume, Resume.id == ResumeSummary.resume_id)
            .where(ResumeSummary.id.in_(summary_ids), ResumeSummary.resume_id == resume.id)
        ).all() if summary_ids else []
        scores = session.scalars(
            select(ResumeScore)
            .join(Resume, Resume.id == ResumeScore.resume_id)
            .where(ResumeScore.id.in_(score_ids), ResumeScore.resume_id == resume.id)
        ).all() if score_ids else []
        job_matches = session.scalars(
            select(JobMatch)
            .join(Resume, Resume.id == JobMatch.resume_id)
            .join(Job, Job.id == JobMatch.job_id)
            .where(
                JobMatch.id.in_(job_match_ids),
                JobMatch.resume_id == resume.id,
                Job.kind == "job",
            )
        ).all() if job_match_ids else []
        original_path: Path | None = None
        original_suffix: str | None = None
        if export.include_originals:
            try:
                original_path = resolve_uploaded_resume_path(
                    settings,
                    storage_key=resume.storage_key,
                    organization_id=resume.organization_id,
                )
            except ResumeServiceError as exc:
                raise CandidateDataExportError("candidate_data_export_original_unavailable") from exc
            original_suffix = _safe_original_suffix(resume.original_filename)
        payload_candidates.append(
            _ExportCandidatePayload(
                candidate_id=candidate.id,
                display_name=candidate.display_name,
                resume_id=resume.id,
                facts_version=fact_snapshot.facts_version,
                fact_snapshot_id=fact_snapshot.id,
                facts=_load_snapshot_facts(fact_snapshot),
                contact_details=_safe_contact_details(resume.contact_details),
                summaries=[
                    {
                        "summary_id": summary.id,
                        "facts_version": summary.facts_version,
                        "source": summary.source,
                        "status": summary.status,
                        "model_name": summary.model_name,
                        "content": _safe_export_value(summary.content or {}),
                    }
                    for summary in summaries
                ],
                scores=[
                    {
                        "score_id": score.id,
                        "facts_version": score.facts_version,
                        "template_id": score.template_id,
                        "template_version": score.template_version,
                        "total_score": score.total_score,
                        "ai_total_score": score.ai_total_score,
                        "status": score.status,
                        "model_name": score.model_name,
                        "dimension_scores": _safe_export_value(score.dimension_scores or []),
                        "analysis": _safe_export_value(score.analysis or {}),
                    }
                    for score in scores
                ],
                job_matches=[
                    {
                        "job_match_id": match.id,
                        "job_id": match.job_id,
                        "job_version_id": match.job_version_id,
                        "facts_version": match.facts_version,
                        "job_version": match.job_version,
                        "total_score": match.total_score,
                        "must_have_passed": match.must_have_passed,
                        "evidence_coverage": match.evidence_coverage,
                        "hard_requirement_status": match.hard_requirement_status,
                        "status": match.status,
                        "model_name": match.model_name,
                        "analysis": _safe_export_value(match.analysis or {}),
                    }
                    for match in job_matches
                ],
                original_path=original_path,
                original_suffix=original_suffix,
            )
        )
    return _ExportBuildPayload(
        export_id=export.id,
        organization_id=export.organization_id,
        include_originals=export.include_originals,
        candidates=payload_candidates,
    )


def _spreadsheet_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        first = value.lstrip()[:1]
        return f"'{value}" if first in _FORMULA_PREFIXES else value
    return value


def _summary_rows(payload: _ExportBuildPayload) -> list[list[object]]:
    rows: list[list[object]] = []
    for item in payload.candidates:
        derived = item.facts.get("derived")
        derived_values = derived if isinstance(derived, dict) else {}
        skills = item.facts.get("skills")
        skill_names = ", ".join(
            str(skill.get("skill_display") or "")
            for skill in skills
            if isinstance(skill, dict) and skill.get("skill_display")
        ) if isinstance(skills, list) else ""
        rows.append(
            [
                item.candidate_id,
                item.display_name or "",
                item.resume_id,
                item.facts_version,
                derived_values.get("highest_degree"),
                derived_values.get("is_985_211"),
                derived_values.get("employment_months"),
                derived_values.get("employment_or_internship_months"),
                skill_names,
                len(item.summaries),
                len(item.scores),
                len(item.job_matches),
            ]
        )
    return rows


_SUMMARY_HEADERS = [
    "candidate_id",
    "candidate_name",
    "resume_id",
    "facts_version",
    "highest_degree",
    "is_985_211",
    "employment_months",
    "employment_or_internship_months",
    "skills",
    "summary_count",
    "score_count",
    "job_match_count",
]


def _csv_bytes(rows: list[list[object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow([_spreadsheet_cell(value) for value in _SUMMARY_HEADERS])
    for row in rows:
        writer.writerow([_spreadsheet_cell(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Candidates"
    worksheet.append([_spreadsheet_cell(value) for value in _SUMMARY_HEADERS])
    for row in rows:
        worksheet.append([_spreadsheet_cell(value) for value in row])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _archive_documents(payload: _ExportBuildPayload) -> dict[str, object]:
    facts: list[dict[str, object]] = []
    contacts: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    scores: list[dict[str, object]] = []
    job_matches: list[dict[str, object]] = []
    original_manifest: list[dict[str, object]] = []
    for item in payload.candidates:
        root = {
            "candidate_id": item.candidate_id,
            "candidate_name": item.display_name,
            "resume_id": item.resume_id,
            "fact_snapshot_id": item.fact_snapshot_id,
            "facts_version": item.facts_version,
        }
        facts.append({**root, "facts": item.facts})
        contacts.append({**root, "contacts": item.contact_details})
        summaries.extend({**root, **summary} for summary in item.summaries)
        scores.extend({**root, **score} for score in item.scores)
        job_matches.extend({**root, **job_match} for job_match in item.job_matches)
        if item.original_path is not None:
            archive_path = f"originals/{item.candidate_id}/{item.resume_id}{item.original_suffix or '.bin'}"
            try:
                size_bytes = item.original_path.stat().st_size
            except OSError as exc:
                raise CandidateDataExportError("candidate_data_export_original_unavailable") from exc
            original_manifest.append(
                {
                    "candidate_id": item.candidate_id,
                    "resume_id": item.resume_id,
                    "archive_path": archive_path,
                    "size_bytes": size_bytes,
                }
            )
    return {
        "facts": facts,
        "contacts": contacts,
        "summaries": summaries,
        "scores": scores,
        "job_matches": job_matches,
        "original_manifest": original_manifest,
    }


def _write_export_archive(
    *,
    payload: _ExportBuildPayload,
    target_path: Path,
    settings: AppSettings,
) -> int:
    """Write a complete ZIP to a temporary sibling then atomically publish it."""

    target_path.parent.mkdir(parents=True, exist_ok=True)
    file_handle, temporary_name = tempfile.mkstemp(
        prefix=f".{payload.export_id}.", suffix=".tmp", dir=target_path.parent
    )
    os.close(file_handle)
    temporary_path = Path(temporary_name)
    try:
        documents = _archive_documents(payload)
        original_total = 0
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            summary_rows = _summary_rows(payload)
            archive.writestr("candidates.csv", _csv_bytes(summary_rows))
            archive.writestr("candidates.xlsx", _xlsx_bytes(summary_rows))
            archive.writestr("facts.json", _json_bytes(documents["facts"]))
            archive.writestr("contacts.json", _json_bytes(documents["contacts"]))
            archive.writestr("summaries.json", _json_bytes(documents["summaries"]))
            archive.writestr("scores.json", _json_bytes(documents["scores"]))
            archive.writestr("job_matches.json", _json_bytes(documents["job_matches"]))
            if payload.include_originals:
                for item in payload.candidates:
                    if item.original_path is None:
                        raise CandidateDataExportError("candidate_data_export_original_unavailable")
                    try:
                        size = item.original_path.stat().st_size
                    except OSError as exc:
                        raise CandidateDataExportError("candidate_data_export_original_unavailable") from exc
                    original_total += size
                    if original_total > settings.candidate_data_export_max_original_bytes:
                        raise CandidateDataExportError("candidate_data_export_original_bytes_exceeded")
                    archive_path = f"originals/{item.candidate_id}/{item.resume_id}{item.original_suffix or '.bin'}"
                    try:
                        archive.write(item.original_path, archive_path)
                    except OSError as exc:
                        raise CandidateDataExportError("candidate_data_export_original_unavailable") from exc
            manifest = {
                "schema_version": "candidate_data_export.v1",
                "export_id": payload.export_id,
                "generated_at": utcnow().isoformat(),
                "item_count": len(payload.candidates),
                "include_originals": payload.include_originals,
                "files": [
                    "candidates.csv",
                    "candidates.xlsx",
                    "facts.json",
                    "contacts.json",
                    "summaries.json",
                    "scores.json",
                    "job_matches.json",
                    *( ["originals/"] if payload.include_originals else [] ),
                ],
                "originals": documents["original_manifest"],
            }
            archive.writestr("manifest.json", _json_bytes(manifest))
        size = temporary_path.stat().st_size
        os.replace(temporary_path, target_path)
        return size
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def _recover_expired_export_leases(session: Session, *, now: datetime) -> None:
    """Return abandoned work to the queue without reading candidate content globally."""

    rows = session.execute(
        select(CandidateDataExport.id, CandidateDataExport.organization_id)
        .where(
            CandidateDataExport.status == EXPORT_RUNNING,
            CandidateDataExport.lease_expires_at.is_not(None),
            CandidateDataExport.lease_expires_at <= now,
        )
        .execution_options(skip_organization_scope=True)
    ).all()
    for export_id, organization_id in rows:
        if not organization_id:
            continue
        with _organization_session(session, organization_id):
            export = session.scalar(
                select(CandidateDataExport).where(CandidateDataExport.id == export_id)
            )
            if export is None or export.status != EXPORT_RUNNING:
                continue
            if export.revoked_at is not None or (
                as_utc(export.expires_at) is not None and as_utc(export.expires_at) <= now
            ):
                export.status = EXPORT_REVOKED if export.revoked_at is not None else EXPORT_EXPIRED
                export.completed_at = now
                export.next_attempt_at = None
            elif export.attempt_count >= export.max_attempts:
                export.status = EXPORT_FAILED
                export.last_error = "candidate_data_export_worker_lease_expired"
                export.completed_at = now
                export.next_attempt_at = None
            else:
                export.status = EXPORT_RETRYABLE_FAILED
                export.last_error = "candidate_data_export_worker_lease_expired"
                export.next_attempt_at = now
            export.lease_owner = None
            export.lease_expires_at = None
            session.flush()


def _claim_candidate_data_export(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> ClaimedCandidateDataExport | None:
    now = utcnow()
    with database.session_factory() as session:
        _recover_expired_export_leases(session, now=now)
        rows = session.execute(
            select(CandidateDataExport.id, CandidateDataExport.organization_id)
            .where(
                CandidateDataExport.status.in_((EXPORT_QUEUED, EXPORT_RETRYABLE_FAILED)),
                CandidateDataExport.revoked_at.is_(None),
                or_(
                    CandidateDataExport.next_attempt_at.is_(None),
                    CandidateDataExport.next_attempt_at <= now,
                ),
                or_(
                    CandidateDataExport.expires_at.is_(None),
                    CandidateDataExport.expires_at > now,
                ),
            )
            .order_by(
                CandidateDataExport.requested_at.asc(),
                CandidateDataExport.id.asc(),
            )
            .execution_options(skip_organization_scope=True)
        ).all()
        for export_id, organization_id in rows:
            if not organization_id:
                continue
            with _organization_session(session, organization_id):
                claimed = session.execute(
                    update(CandidateDataExport)
                    .where(
                        CandidateDataExport.id == export_id,
                        CandidateDataExport.organization_id == organization_id,
                        CandidateDataExport.status.in_((EXPORT_QUEUED, EXPORT_RETRYABLE_FAILED)),
                        CandidateDataExport.revoked_at.is_(None),
                        or_(
                            CandidateDataExport.next_attempt_at.is_(None),
                            CandidateDataExport.next_attempt_at <= now,
                        ),
                    )
                    .values(
                        status=EXPORT_RUNNING,
                        attempt_count=CandidateDataExport.attempt_count + 1,
                        next_attempt_at=None,
                        lease_owner=worker_id,
                        lease_expires_at=now
                        + timedelta(seconds=settings.candidate_data_export_lease_seconds),
                        last_error=None,
                        started_at=func.coalesce(CandidateDataExport.started_at, now),
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(claimed.rowcount or 0) != 1:
                    continue
                session.commit()
                return ClaimedCandidateDataExport(
                    export_id=export_id, organization_id=organization_id
                )
        session.commit()
    return None


def _complete_candidate_data_export(
    database: Database,
    *,
    settings: AppSettings,
    claimed: ClaimedCandidateDataExport,
    worker_id: str,
    output_storage_key: str,
    output_size_bytes: int,
) -> bool:
    now = utcnow()
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            completed = session.execute(
                update(CandidateDataExport)
                .where(
                    CandidateDataExport.id == claimed.export_id,
                    CandidateDataExport.organization_id == claimed.organization_id,
                    CandidateDataExport.status == EXPORT_RUNNING,
                    CandidateDataExport.lease_owner == worker_id,
                    CandidateDataExport.revoked_at.is_(None),
                    CandidateDataExport.expires_at > now,
                )
                .values(
                    status=EXPORT_COMPLETED,
                    output_storage_key=output_storage_key,
                    output_content_type="application/zip",
                    output_size_bytes=output_size_bytes,
                    lease_owner=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    completed_at=now,
                    last_error=None,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return int(completed.rowcount or 0) == 1


def _finish_candidate_data_export_failure(
    database: Database,
    *,
    claimed: ClaimedCandidateDataExport,
    worker_id: str,
    error_code: str,
    retryable: bool,
) -> None:
    now = utcnow()
    with database.session_factory() as session:
        with _organization_session(session, claimed.organization_id):
            export = session.scalar(
                select(CandidateDataExport).where(
                    CandidateDataExport.id == claimed.export_id,
                    CandidateDataExport.status == EXPORT_RUNNING,
                    CandidateDataExport.lease_owner == worker_id,
                )
            )
            if export is None:
                session.rollback()
                return
            if export.revoked_at is not None:
                export.status = EXPORT_REVOKED
            elif as_utc(export.expires_at) is not None and as_utc(export.expires_at) <= now:
                export.status = EXPORT_EXPIRED
            elif retryable and export.attempt_count < export.max_attempts:
                export.status = EXPORT_RETRYABLE_FAILED
                export.next_attempt_at = now + timedelta(
                    seconds=min(300, 10 * (2 ** max(0, export.attempt_count - 1)))
                )
            else:
                export.status = EXPORT_FAILED
                export.completed_at = now
                export.next_attempt_at = None
            export.lease_owner = None
            export.lease_expires_at = None
            export.last_error = error_code[:128]
            session.commit()


def _process_candidate_data_export(
    database: Database,
    *,
    settings: AppSettings,
    claimed: ClaimedCandidateDataExport,
    worker_id: str,
) -> None:
    target_path: Path | None = None
    try:
        with database.session_factory() as session:
            with _organization_session(session, claimed.organization_id):
                payload = _load_export_build_payload(
                    session,
                    settings=settings,
                    export_id=claimed.export_id,
                    worker_id=worker_id,
                )
                session.commit()

        attempt_token = secrets.token_hex(16)
        storage_key = _export_attempt_storage_key(
            organization_id=claimed.organization_id,
            export_id=claimed.export_id,
            attempt_token=attempt_token,
        )
        target_path = resolve_candidate_data_export_path(
            settings,
            organization_id=claimed.organization_id,
            export_id=claimed.export_id,
            storage_key=storage_key,
            require_file=False,
        )
        output_size = _write_export_archive(
            payload=payload, target_path=target_path, settings=settings
        )
        if not _complete_candidate_data_export(
            database,
            settings=settings,
            claimed=claimed,
            worker_id=worker_id,
            output_storage_key=storage_key,
            output_size_bytes=output_size,
        ):
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass
    except CandidateDataExportError as exc:
        if target_path is not None:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass
        error_code = str(exc)
        _finish_candidate_data_export_failure(
            database,
            claimed=claimed,
            worker_id=worker_id,
            error_code=error_code,
            retryable=error_code
            in {
                "candidate_data_export_original_unavailable",
                "candidate_data_export_worker_lease_lost",
            },
        )
    except Exception:
        if target_path is not None:
            try:
                target_path.unlink(missing_ok=True)
            except OSError:
                pass
        _finish_candidate_data_export_failure(
            database,
            claimed=claimed,
            worker_id=worker_id,
            error_code="candidate_data_export_worker_error",
            retryable=True,
        )


def run_candidate_data_export_worker_once(
    database: Database,
    *,
    settings: AppSettings,
    worker_id: str,
) -> bool:
    """Claim and process at most one export without a cross-tenant payload read."""

    claimed = _claim_candidate_data_export(database, settings=settings, worker_id=worker_id)
    if claimed is None:
        return False
    _process_candidate_data_export(
        database, settings=settings, claimed=claimed, worker_id=worker_id
    )
    return True


def _cleanup_export_output(
    export: CandidateDataExport,
    *,
    settings: AppSettings,
) -> bool:
    """Remove one archive and report whether it is definitely absent.

    The caller may discard a storage key only after this returns ``True``.
    Losing that pointer after an I/O failure would orphan a ZIP containing
    candidate data on the shared upload volume with no safe retry path.
    """

    if not export.output_storage_key:
        return True
    try:
        path = resolve_candidate_data_export_path(
            settings,
            organization_id=export.organization_id,
            export_id=export.id,
            storage_key=export.output_storage_key,
            require_file=False,
        )
        path.unlink(missing_ok=True)
        return True
    except (CandidateDataExportError, OSError):
        # Cleanup is idempotent and may be retry-run.  Do not retain a raw path
        # or operating-system error in the export row.
        return False


def cleanup_expired_candidate_data_exports(
    database: Database,
    *,
    settings: AppSettings,
    limit: int = 100,
) -> int:
    """Remove expired/revoked output bytes and retain only safe task history."""

    bounded_limit = min(max(limit, 1), 500)
    now = utcnow()
    cleaned = 0
    with database.session_factory() as session:
        needs_expiry_transition = and_(
            CandidateDataExport.status == EXPORT_COMPLETED,
            CandidateDataExport.revoked_at.is_(None),
            CandidateDataExport.expires_at.is_not(None),
            CandidateDataExport.expires_at <= now,
        )
        needs_output_cleanup = and_(
            CandidateDataExport.output_storage_key.is_not(None),
            or_(
                CandidateDataExport.revoked_at.is_not(None),
                CandidateDataExport.status.in_(
                    (EXPORT_CANCELLED, EXPORT_REVOKED, EXPORT_EXPIRED)
                ),
                CandidateDataExport.expires_at <= now,
            ),
            or_(
                CandidateDataExport.next_attempt_at.is_(None),
                CandidateDataExport.next_attempt_at <= now,
            ),
        )
        rows = session.execute(
            select(CandidateDataExport.id, CandidateDataExport.organization_id)
            .where(
                or_(
                    needs_expiry_transition,
                    needs_output_cleanup,
                ),
            )
            .order_by(CandidateDataExport.updated_at.asc(), CandidateDataExport.id.asc())
            .limit(bounded_limit)
            .execution_options(skip_organization_scope=True)
        ).all()
        for export_id, organization_id in rows:
            if not organization_id:
                continue
            with _organization_session(session, organization_id):
                export = session.scalar(
                    select(CandidateDataExport).where(CandidateDataExport.id == export_id)
                )
                if export is None:
                    continue
                expired = (
                    as_utc(export.expires_at) is not None
                    and as_utc(export.expires_at) <= now
                )
                if expired and export.revoked_at is None and export.status not in {
                    EXPORT_CANCELLED,
                    EXPORT_REVOKED,
                }:
                    export.status = EXPORT_EXPIRED
                _revoke_export_download_grants(session, export_id=export.id, now=now)
                if not _cleanup_export_output(export, settings=settings):
                    # Retain the opaque storage key so a later worker can
                    # retry the same bounded path.  The error is deliberately
                    # content-free and the archive has already lost all
                    # download grants.
                    export.next_attempt_at = now + timedelta(seconds=60)
                    export.last_error = "candidate_data_export_cleanup_retryable"
                    export.lease_owner = None
                    export.lease_expires_at = None
                    session.flush()
                    continue
                export.output_storage_key = None
                export.output_content_type = None
                export.output_size_bytes = 0
                export.lease_owner = None
                export.lease_expires_at = None
                export.next_attempt_at = None
                if export.last_error == "candidate_data_export_cleanup_retryable":
                    export.last_error = None
                session.flush()
                cleaned += 1
        session.commit()
    return cleaned


__all__ = [
    "CandidateDataExportDownloadAccess",
    "CandidateDataExportError",
    "ClaimedCandidateDataExport",
    "ResolvedCandidateDataExportDownload",
    "cancel_candidate_data_export",
    "cleanup_expired_candidate_data_exports",
    "create_candidate_data_export",
    "get_candidate_data_export",
    "list_candidate_data_exports",
    "authorize_candidate_data_export_download",
    "resolve_candidate_data_export_download",
    "resolve_candidate_data_export_path",
    "run_candidate_data_export_worker_once",
]
