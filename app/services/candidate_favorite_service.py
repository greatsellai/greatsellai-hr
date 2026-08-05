"""Private, workspace-scoped candidate bookmark operations.

The only durable state here is that one signed-in user bookmarked one
candidate in one workspace.  Resume versions, source files, AI summaries,
and scoring records remain in their existing tables and are read only when a
screen needs them.  This keeps a personal favorite from becoming a copied or
shared talent pool.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Candidate, CandidateFavorite
from app.schemas import (
    CandidateFavoriteListResponse,
    CandidateFavoriteState,
    CandidateResumeVersionPreview,
    CandidateResumeVersionsResponse,
    FavoriteCandidateItem,
)
from app.services.source_tag_service import resume_source_tag_references
from app.tenant_scope import organization_context_id


class CandidateFavoriteNotFoundError(RuntimeError):
    """Use one stable response for missing, deleted, and foreign candidates."""


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _visible_candidate(session: Session, *, candidate_id: str) -> Candidate:
    """Resolve a live candidate through the current workspace scope only."""

    candidate = session.scalar(select(Candidate).where(Candidate.id == candidate_id))
    if candidate is None:
        # The automatic tenant/lifecycle criteria ensure a cross-workspace ID
        # and a deleted candidate deliberately look exactly like a missing ID.
        raise CandidateFavoriteNotFoundError("candidate_not_found")
    return candidate


def _favorite_row(
    session: Session,
    *,
    user_id: str,
    candidate_id: str,
) -> CandidateFavorite | None:
    return session.scalar(
        select(CandidateFavorite).where(
            CandidateFavorite.user_id == user_id,
            CandidateFavorite.candidate_id == candidate_id,
        )
    )


def _state(candidate_id: str, favorite: CandidateFavorite | None) -> CandidateFavoriteState:
    return CandidateFavoriteState(
        candidate_id=candidate_id,
        is_favorited=favorite is not None,
        favorited_at=_isoformat(favorite.created_at) if favorite is not None else None,
    )


def candidate_favorite_state(
    session: Session,
    *,
    user_id: str,
    candidate_id: str,
) -> CandidateFavoriteState:
    """Return current-user state after proving the candidate is visible."""

    _visible_candidate(session, candidate_id=candidate_id)
    return _state(
        candidate_id,
        _favorite_row(session, user_id=user_id, candidate_id=candidate_id),
    )


def favorite_candidate(
    session: Session,
    *,
    user_id: str,
    candidate_id: str,
) -> CandidateFavoriteState:
    """Idempotently bookmark a currently visible candidate.

    The unique database constraint is the concurrency boundary.  A savepoint
    lets a second browser tab recover from the expected race without rolling
    back unrelated work in the request transaction.
    """

    _visible_candidate(session, candidate_id=candidate_id)
    existing = _favorite_row(session, user_id=user_id, candidate_id=candidate_id)
    if existing is not None:
        return _state(candidate_id, existing)

    favorite = CandidateFavorite(
        organization_id=organization_context_id(session),
        user_id=user_id,
        candidate_id=candidate_id,
    )
    try:
        with session.begin_nested():
            session.add(favorite)
            session.flush()
    except IntegrityError:
        # A duplicate request can race after the initial read.  Re-read only
        # within the same current workspace and never infer a foreign record.
        existing = _favorite_row(session, user_id=user_id, candidate_id=candidate_id)
        if existing is None:
            # The candidate may have been deleted between visibility check and
            # insert. Preserve the same non-enumerating response contract.
            _visible_candidate(session, candidate_id=candidate_id)
            raise
        return _state(candidate_id, existing)
    return _state(candidate_id, favorite)


def unfavorite_candidate(
    session: Session,
    *,
    user_id: str,
    candidate_id: str,
) -> None:
    """Idempotently remove one private bookmark for a visible candidate."""

    _visible_candidate(session, candidate_id=candidate_id)
    favorite = _favorite_row(session, user_id=user_id, candidate_id=candidate_id)
    if favorite is not None:
        session.delete(favorite)


def favorite_candidate_ids(
    session: Session,
    *,
    user_id: str,
    candidate_ids: set[str] | list[str] | tuple[str, ...],
) -> set[str]:
    """Project one user's private state onto an already-visible candidate set."""

    normalized_ids = sorted({candidate_id for candidate_id in candidate_ids if candidate_id})
    if not normalized_ids:
        return set()
    return set(
        session.scalars(
            select(CandidateFavorite.candidate_id).where(
                CandidateFavorite.user_id == user_id,
                CandidateFavorite.candidate_id.in_(normalized_ids),
            )
        ).all()
    )


def _version_previews(
    candidate: Candidate,
    *,
    source_tags_by_resume: dict[str, list] | None = None,
) -> list[CandidateResumeVersionPreview]:
    """Return live version metadata in deterministic current-first order."""

    resumes = sorted(
        candidate.resumes,
        key=lambda resume: (resume.is_active, resume.created_at, resume.id),
        reverse=True,
    )
    return [
        CandidateResumeVersionPreview(
            resume_id=resume.id,
            original_filename=resume.original_filename,
            created_at=_isoformat(resume.created_at),
            extraction_status=resume.extraction_status,
            is_active=resume.is_active,
            source_mailbox_label=resume.source_mailbox_label_snapshot,
            source_tags=(source_tags_by_resume or {}).get(resume.id, []),
        )
        for resume in resumes
    ]


def list_candidate_resume_versions(
    session: Session,
    *,
    candidate_id: str,
) -> CandidateResumeVersionsResponse:
    """List only metadata for every live version of one visible candidate."""

    candidate = session.scalar(
        select(Candidate)
        .options(selectinload(Candidate.resumes))
        .where(Candidate.id == candidate_id)
    )
    if candidate is None:
        raise CandidateFavoriteNotFoundError("candidate_not_found")
    source_tags_by_resume = resume_source_tag_references(
        session,
        resume_ids=[resume.id for resume in candidate.resumes],
    )
    return CandidateResumeVersionsResponse(
        candidate_id=candidate.id,
        display_name=candidate.display_name,
        items=_version_previews(candidate, source_tags_by_resume=source_tags_by_resume),
    )


def list_candidate_favorites(
    session: Session,
    *,
    user_id: str,
    page: int,
    page_size: int,
) -> CandidateFavoriteListResponse:
    """List one user's private bookmarks, grouped by candidate identity."""

    filters = (CandidateFavorite.user_id == user_id,)
    total = int(
        session.scalar(
            select(func.count(CandidateFavorite.id))
            .join(CandidateFavorite.candidate)
            .where(*filters)
        )
        or 0
    )
    favorites = session.scalars(
        select(CandidateFavorite)
        .join(CandidateFavorite.candidate)
        .options(selectinload(CandidateFavorite.candidate).selectinload(Candidate.resumes))
        .where(*filters)
        .order_by(CandidateFavorite.created_at.desc(), CandidateFavorite.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items: list[FavoriteCandidateItem] = []
    source_tags_by_resume = resume_source_tag_references(
        session,
        resume_ids=[
            resume.id
            for favorite in favorites
            for resume in favorite.candidate.resumes
        ],
    )
    for favorite in favorites:
        candidate = favorite.candidate
        versions = _version_previews(
            candidate,
            source_tags_by_resume=source_tags_by_resume,
        )
        current_version = next(
            (version for version in versions if version.is_active),
            versions[0] if versions else None,
        )
        items.append(
            FavoriteCandidateItem(
                candidate_id=candidate.id,
                display_name=candidate.display_name,
                favorited_at=_isoformat(favorite.created_at),
                current_resume_id=(current_version.resume_id if current_version else None),
                resume_versions=versions,
            )
        )
    return CandidateFavoriteListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = [
    "CandidateFavoriteNotFoundError",
    "candidate_favorite_state",
    "favorite_candidate",
    "favorite_candidate_ids",
    "list_candidate_favorites",
    "list_candidate_resume_versions",
    "unfavorite_candidate",
]
