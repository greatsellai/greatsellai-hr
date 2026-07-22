from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Resume, ResumeScore, ResumeSummary
from app.schemas import ResumeLibraryItem, ResumeLibraryResponse
from app.services.ai_extraction_job_service import ai_extraction_state


_SUMMARY_SECTION_ORDER = (
    "candidate_positioning",
    "work_and_internship",
    "core_skills",
    "strengths",
)
# Keep review-needed AI scores visible in the recruiter library.  They remain
# clearly labelled by their status and are never an automatic hiring decision.
_CURRENT_SCORE_STATUSES = {"succeeded", "needs_review", "overridden"}
_PREVIEW_MAX_CHARS = 220


def _isoformat(value: datetime) -> str:
    """Preserve the API's UTC offset when SQLite reloads a naive timestamp."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _section_text(value: object) -> str | None:
    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _summary_preview(content: object) -> str | None:
    if not isinstance(content, Mapping):
        return None
    sections = content.get("sections")
    if not isinstance(sections, Mapping):
        return None

    text = next(
        (
            rendered
            for key in _SUMMARY_SECTION_ORDER
            if (rendered := _section_text(sections.get(key)))
        ),
        None,
    )
    if text is None:
        text = next(
            (
                rendered
                for value in sections.values()
                if (rendered := _section_text(value))
            ),
            None,
        )
    if text is None:
        return None
    normalized = " ".join(text.split())
    if len(normalized) <= _PREVIEW_MAX_CHARS:
        return normalized
    return f"{normalized[:_PREVIEW_MAX_CHARS - 1].rstrip()}…"


def _current_summary(resume: Resume) -> ResumeSummary | None:
    candidates = [
        summary
        for summary in resume.summaries
        if summary.is_current
        and summary.status == "succeeded"
        and summary.facts_version == resume.facts_version
    ]
    return max(candidates, key=lambda summary: (summary.created_at, summary.id), default=None)


def _latest_current_score(resume: Resume) -> ResumeScore | None:
    candidates = [
        score
        for score in resume.scores
        if score.facts_version == resume.facts_version
        and score.status in _CURRENT_SCORE_STATUSES
    ]
    return max(candidates, key=lambda score: (score.created_at, score.id), default=None)


def list_resume_library(
    session: Session,
    *,
    page: int,
    page_size: int,
    mailbox_config_id: str | None = None,
) -> ResumeLibraryResponse:
    """List uploaded resume versions without exposing raw extracted facts."""

    filters = []
    if mailbox_config_id is not None:
        filters.append(Resume.source_mailbox_config_id == mailbox_config_id)
    total = int(
        session.scalar(select(func.count(Resume.id)).where(*filters)) or 0
    )
    statement = (
        select(Resume)
        .options(
            selectinload(Resume.candidate),
            selectinload(Resume.document_extraction_job),
            selectinload(Resume.ai_extraction_job),
            selectinload(Resume.summaries),
            selectinload(Resume.scores).selectinload(ResumeScore.template),
        )
        .where(*filters)
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    resumes = session.scalars(statement).all()
    items: list[ResumeLibraryItem] = []
    for resume in resumes:
        summary = _current_summary(resume)
        score = _latest_current_score(resume)
        ai_status, ai_error = ai_extraction_state(resume)
        items.append(
            ResumeLibraryItem(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                display_name=resume.candidate.display_name,
                original_filename=resume.original_filename,
                created_at=_isoformat(resume.created_at),
                extraction_status=resume.extraction_status,
                ai_extraction_status=ai_status,
                ai_extraction_error=ai_error,
                is_active=resume.is_active,
                ingestion_source_type=resume.ingestion_source_type,
                source_mailbox_config_id=resume.source_mailbox_config_id,
                source_mailbox_label=resume.source_mailbox_label_snapshot,
                quality_flags=resume.quality_flags or [],
                summary_preview=_summary_preview(summary.content) if summary else None,
                summary_created_at=_isoformat(summary.created_at) if summary else None,
                score_total=score.total_score if score else None,
                score_status=score.status if score else None,
                score_template_name=score.template.name if score and score.template else None,
                score_created_at=_isoformat(score.created_at) if score else None,
            )
        )
    return ResumeLibraryResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["list_resume_library"]
