"""Private candidate favorites, always bound to the current workspace."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Candidate, CandidateFavorite


class CandidateFavoriteError(RuntimeError):
    """A controlled favorite-domain error safe to expose as an API code."""


def _visible_candidate(session: Session, *, candidate_id: str) -> Candidate:
    candidate = session.scalar(
        select(Candidate).where(Candidate.id == candidate_id).with_for_update()
    )
    if candidate is None:
        # The tenant and lifecycle query criteria deliberately make foreign or
        # deleted candidates indistinguishable from an unknown id.
        raise CandidateFavoriteError("candidate_not_found")
    return candidate


def favorite_candidate(
    session: Session,
    *,
    candidate_id: str,
    user_id: str,
) -> bool:
    """Save a visible candidate for one user. Repeated PUTs are idempotent."""

    candidate = _visible_candidate(session, candidate_id=candidate_id)
    existing = session.scalar(
        select(CandidateFavorite.id).where(
            CandidateFavorite.candidate_id == candidate.id,
            CandidateFavorite.user_id == user_id,
        )
    )
    if existing is not None:
        return True

    # A double click can race through the initial read. The unique constraint
    # is the final guard; a nested transaction lets us re-read the winning row
    # without rolling back the request's outer transaction.
    try:
        with session.begin_nested():
            session.add(CandidateFavorite(candidate_id=candidate.id, user_id=user_id))
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(CandidateFavorite.id).where(
                CandidateFavorite.candidate_id == candidate.id,
                CandidateFavorite.user_id == user_id,
            )
        )
        if existing is None:
            raise
    return True


def unfavorite_candidate(
    session: Session,
    *,
    candidate_id: str,
    user_id: str,
) -> bool:
    """Remove one user's marker. Repeated DELETEs are deliberately safe."""

    candidate = _visible_candidate(session, candidate_id=candidate_id)
    favorite = session.scalar(
        select(CandidateFavorite).where(
            CandidateFavorite.candidate_id == candidate.id,
            CandidateFavorite.user_id == user_id,
        )
    )
    if favorite is not None:
        session.delete(favorite)
    return False


def is_candidate_favorited(
    session: Session,
    *,
    candidate_id: str,
    user_id: str,
) -> bool:
    return session.scalar(
        select(CandidateFavorite.id).where(
            CandidateFavorite.candidate_id == candidate_id,
            CandidateFavorite.user_id == user_id,
        )
    ) is not None


def favorite_candidate_ids(
    session: Session,
    *,
    user_id: str,
    candidate_ids: Iterable[str],
) -> set[str]:
    """Return only favorites from the supplied candidate page, in one query."""

    ids = sorted({candidate_id for candidate_id in candidate_ids if candidate_id})
    if not ids:
        return set()
    return set(
        session.scalars(
            select(CandidateFavorite.candidate_id).where(
                CandidateFavorite.user_id == user_id,
                CandidateFavorite.candidate_id.in_(ids),
            )
        ).all()
    )


__all__ = [
    "CandidateFavoriteError",
    "favorite_candidate",
    "favorite_candidate_ids",
    "is_candidate_favorited",
    "unfavorite_candidate",
]
