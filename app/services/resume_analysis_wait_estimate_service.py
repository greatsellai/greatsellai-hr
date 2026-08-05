"""Conservative, workspace-local wait estimates for unnamed candidates.

The estimate intentionally describes an interval, not an SLA.  It only uses
durable queue timestamps from the current workspace, so a recruiter never
learns another workspace's volume or activity.  The shared worker pool can
still interleave other kinds of work and other workspaces, which is why the
client must present this as a live estimate that can move on refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CandidateNameExtractionJob,
    Resume,
    ResumeAiExtractionJob,
    ResumeDocumentExtractionJob,
)


_ACTIVE_STATUSES = frozenset({"queued", "running"})
_HISTORY_LIMIT = 30
_MINIMUM_OBSERVED_SAMPLES = 3
_MAX_STAGE_SECONDS = 300
_MIN_RUNNING_REMAINING_SECONDS = 15
_MAX_ESTIMATE_SECONDS = 30 * 60

# These are deliberately conservative first-run defaults. Once the workspace
# has enough completed jobs, they are replaced by the recent median for that
# stage. They are not a promise from the model provider or the worker.
_FALLBACK_STAGE_SECONDS = {
    "document": 45,
    "ai": 90,
    "candidate_name": 35,
}
_SUCCESS_STATUS_BY_STAGE = {
    "document": "completed",
    "ai": "completed",
    "candidate_name": "succeeded",
}
_PUBLIC_PHASE_BY_STAGE = {
    # These phase names describe what a recruiter can understand from the
    # pipeline. They intentionally do not expose parser/OCR implementation
    # choices, worker identity, or model-routing internals.
    "document": "source_reading",
    "ai": "resume_analysis",
    "candidate_name": "name_completion",
}
_MODEL_BY_STAGE: dict[str, type[Any]] = {
    "document": ResumeDocumentExtractionJob,
    "ai": ResumeAiExtractionJob,
    "candidate_name": CandidateNameExtractionJob,
}


@dataclass(frozen=True)
class ResumeAnalysisWaitEstimate:
    """A UI-safe estimate for the next meaningful unnamed-candidate update."""

    target: str
    phase: str
    state: str
    estimated_min_seconds: int
    estimated_max_seconds: int
    confidence: str


@dataclass(frozen=True)
class _PendingJob:
    stage: str
    job_id: str
    status: str
    requested_at: datetime | None
    started_at: datetime | None
    next_attempt_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_display_name(resume: Resume) -> bool:
    candidate = resume.candidate
    return bool(candidate and candidate.display_name and candidate.display_name.strip())


def _pending_stage(resume: Resume, *, now: datetime) -> _PendingJob | None:
    """Return the earliest pending stage that can still name this candidate."""

    if _has_display_name(resume):
        return None
    for stage, job in (
        ("document", resume.document_extraction_job),
        ("ai", resume.ai_extraction_job),
        ("candidate_name", resume.candidate_name_extraction_job),
    ):
        if job is None or job.status not in _ACTIVE_STATUSES:
            continue
        next_attempt_at = _as_utc(job.next_attempt_at)
        # A delayed retry has a known state, but it is not genuinely waiting in
        # the claimable queue yet. Avoid inventing a short ETA for it.
        if job.status == "queued" and next_attempt_at is not None and next_attempt_at > now:
            return None
        return _PendingJob(
            stage=stage,
            job_id=job.id,
            status=job.status,
            requested_at=_as_utc(job.requested_at),
            started_at=_as_utc(job.started_at),
            next_attempt_at=next_attempt_at,
        )
    return None


def _recent_stage_duration(
    session: Session,
    *,
    organization_id: str,
    stage: str,
) -> tuple[int, int]:
    """Return a bounded recent median duration and its usable sample count."""

    model = _MODEL_BY_STAGE[stage]
    rows = session.execute(
        select(model.started_at, model.completed_at)
        .where(
            model.organization_id == organization_id,
            model.status == _SUCCESS_STATUS_BY_STAGE[stage],
            model.started_at.is_not(None),
            model.completed_at.is_not(None),
        )
        .order_by(model.completed_at.desc(), model.id.desc())
        .limit(_HISTORY_LIMIT)
    ).all()
    durations: list[int] = []
    for started_at, completed_at in rows:
        started = _as_utc(started_at)
        completed = _as_utc(completed_at)
        if started is None or completed is None:
            continue
        elapsed = int((completed - started).total_seconds())
        if elapsed < 0:
            continue
        durations.append(max(5, min(_MAX_STAGE_SECONDS, elapsed)))
    if len(durations) < _MINIMUM_OBSERVED_SAMPLES:
        return _FALLBACK_STAGE_SECONDS[stage], len(durations)
    return int(median(durations)), len(durations)


def _active_jobs(
    session: Session,
    *,
    organization_id: str,
    now: datetime,
) -> list[_PendingJob]:
    """Read only claimable/running work in this workspace, never global load."""

    rows: list[_PendingJob] = []
    for stage, model in _MODEL_BY_STAGE.items():
        job_rows = session.execute(
            select(
                model.id,
                model.status,
                model.requested_at,
                model.started_at,
                model.next_attempt_at,
            ).where(
                model.organization_id == organization_id,
                model.status.in_(_ACTIVE_STATUSES),
            )
        ).all()
        for job_id, status, requested_at, started_at, next_attempt_at in job_rows:
            due_at = _as_utc(next_attempt_at)
            if status == "queued" and due_at is not None and due_at > now:
                continue
            rows.append(
                _PendingJob(
                    stage=stage,
                    job_id=str(job_id),
                    status=str(status),
                    requested_at=_as_utc(requested_at),
                    started_at=_as_utc(started_at),
                    next_attempt_at=due_at,
                )
            )
    return rows


def _was_requested_before(left: _PendingJob, right: _PendingJob) -> bool:
    left_requested = left.requested_at or datetime.min.replace(tzinfo=timezone.utc)
    right_requested = right.requested_at or datetime.min.replace(tzinfo=timezone.utc)
    return (left_requested, left.job_id) < (right_requested, right.job_id)


def _remaining_stage_seconds(
    job: _PendingJob,
    *,
    stage_seconds: dict[str, int],
    now: datetime,
) -> int:
    expected = stage_seconds[job.stage]
    if job.status != "running" or job.started_at is None:
        return expected
    elapsed = max(0, int((now - job.started_at).total_seconds()))
    return max(_MIN_RUNNING_REMAINING_SECONDS, expected - elapsed)


def estimate_pending_resume_analysis_waits(
    session: Session,
    *,
    resumes: Iterable[Resume],
) -> dict[str, ResumeAnalysisWaitEstimate]:
    """Estimate remaining time for unnamed resumes on one library page.

    The caller already scopes ``resumes`` to the authenticated workspace. The
    implementation repeats that boundary for every aggregate query because
    queue depth and historical timing are operational metadata that must not
    cross workspaces either.
    """

    now = _utcnow()
    pending_by_resume_id: dict[str, _PendingJob] = {}
    organization_ids: set[str] = set()
    for resume in resumes:
        pending = _pending_stage(resume, now=now)
        if pending is None:
            continue
        pending_by_resume_id[resume.id] = pending
        organization_ids.add(resume.organization_id)
    if not pending_by_resume_id:
        return {}

    durations_by_organization: dict[str, dict[str, int]] = {}
    samples_by_organization: dict[str, dict[str, int]] = {}
    active_by_organization: dict[str, list[_PendingJob]] = {}
    for organization_id in organization_ids:
        durations: dict[str, int] = {}
        samples: dict[str, int] = {}
        for stage in _MODEL_BY_STAGE:
            durations[stage], samples[stage] = _recent_stage_duration(
                session,
                organization_id=organization_id,
                stage=stage,
            )
        durations_by_organization[organization_id] = durations
        samples_by_organization[organization_id] = samples
        active_by_organization[organization_id] = _active_jobs(
            session,
            organization_id=organization_id,
            now=now,
        )

    estimates: dict[str, ResumeAnalysisWaitEstimate] = {}
    for resume in resumes:
        target_job = pending_by_resume_id.get(resume.id)
        if target_job is None:
            continue
        durations = durations_by_organization[resume.organization_id]
        samples = samples_by_organization[resume.organization_id]
        work_ahead_seconds = sum(
            _remaining_stage_seconds(job, stage_seconds=durations, now=now)
            for job in active_by_organization[resume.organization_id]
            if job.job_id != target_job.job_id and _was_requested_before(job, target_job)
        )
        expected_seconds = work_ahead_seconds + _remaining_stage_seconds(
            target_job,
            stage_seconds=durations,
            now=now,
        )
        # Document normalization is only the first visible wait. Include the
        # following rich-facts pass, which commonly supplies the candidate's
        # name, but not the optional name-only fallback because it may never be
        # necessary.
        required_stages = [target_job.stage]
        if target_job.stage == "document":
            expected_seconds += durations["ai"]
            required_stages.append("ai")
        confidence = (
            "observed"
            if all(samples[stage] >= _MINIMUM_OBSERVED_SAMPLES for stage in required_stages)
            else "baseline"
        )
        minimum = max(15, int(round(expected_seconds * 0.65)))
        maximum = max(60, int(round(expected_seconds * 1.55)) + 20)
        estimates[resume.id] = ResumeAnalysisWaitEstimate(
            target="candidate_name" if target_job.stage == "candidate_name" else "analysis",
            phase=_PUBLIC_PHASE_BY_STAGE[target_job.stage],
            state=target_job.status,
            estimated_min_seconds=min(_MAX_ESTIMATE_SECONDS, minimum),
            estimated_max_seconds=min(_MAX_ESTIMATE_SECONDS, maximum),
            confidence=confidence,
        )
    return estimates


__all__ = ["ResumeAnalysisWaitEstimate", "estimate_pending_resume_analysis_waits"]
