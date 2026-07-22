from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.database import Database
from app.models import Organization
from app.services.resume_service import (
    IdempotencyConflictError,
    NotFoundError,
    ResumeServiceError,
    build_resume_storage_key,
    create_candidate,
    discard_uploaded_pdf,
    get_idempotent_upload_resume,
    get_resume,
    register_upload_idempotency_key,
    resolve_uploaded_resume_path,
    save_pdf_resume,
)
from app.tenant_scope import (
    LEGACY_ORGANIZATION_ID,
    clear_organization_context,
    set_organization_context,
)


ORGANIZATION_A = "00000000-0000-4000-8000-0000000000a1"
ORGANIZATION_B = "00000000-0000-4000-8000-0000000000b2"


@contextmanager
def _workspace(session: Session, organization_id: str) -> Iterator[None]:
    set_organization_context(session, organization_id)
    try:
        yield
    finally:
        clear_organization_context(session)


def _settings(tmp_path: Path) -> AppSettings:
    data_dir = tmp_path / "data"
    return AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        database_url="sqlite://",
        min_text_chars_per_page=20,
    )


def _database_with_workspaces() -> Database:
    database = Database("sqlite://")
    database.create_all()
    with database.session_factory() as session:
        session.add_all(
            (
                Organization(id=LEGACY_ORGANIZATION_ID, name="Legacy"),
                Organization(id=ORGANIZATION_A, name="Workspace A"),
                Organization(id=ORGANIZATION_B, name="Workspace B"),
            )
        )
        session.commit()
    return database


def _save_resume(
    session: Session,
    *,
    settings: AppSettings,
    organization_id: str,
):
    with _workspace(session, organization_id):
        candidate = create_candidate(session, display_name=None)
        resume = save_pdf_resume(
            session,
            candidate_id=candidate.id,
            original_filename="candidate.pdf",
            content=b"%PDF-1.7\nfixture",
            settings=settings,
        )
        session.commit()
        return candidate, resume


def test_new_upload_keys_and_files_are_workspaced(
    tmp_path: Path,
) -> None:
    database = _database_with_workspaces()
    settings = _settings(tmp_path)

    with database.session_factory() as session:
        _, resume = _save_resume(
            session,
            settings=settings,
            organization_id=ORGANIZATION_A,
        )
        assert resume.storage_key.startswith(f"{ORGANIZATION_A}/")
        assert (settings.upload_dir / resume.storage_key).is_file()

        with _workspace(session, ORGANIZATION_A):
            resolved = resolve_uploaded_resume_path(
                settings,
                storage_key=resume.storage_key,
                organization_id=ORGANIZATION_A,
            )
        assert resolved == (settings.upload_dir / resume.storage_key).resolve()

        with _workspace(session, ORGANIZATION_B):
            with pytest.raises(ResumeServiceError, match="resume_original_file_not_found"):
                resolve_uploaded_resume_path(
                    settings,
                    storage_key=resume.storage_key,
                    organization_id=ORGANIZATION_B,
                )
            with pytest.raises(NotFoundError, match="resume_not_found"):
                get_resume(session, resume.id)

        # Rollback cleanup must be as constrained as the normal read path:
        # another workspace cannot turn a known storage key into a delete.
        discard_uploaded_pdf(
            settings,
            storage_key=resume.storage_key,
            organization_id=ORGANIZATION_B,
        )
        assert (settings.upload_dir / resume.storage_key).is_file()

    database.dispose()


def test_flat_legacy_files_are_not_available_to_new_workspaces(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    legacy_path = settings.upload_dir / "historic.pdf"
    legacy_path.write_bytes(b"fixture")

    assert resolve_uploaded_resume_path(
        settings,
        storage_key="historic.pdf",
        organization_id=LEGACY_ORGANIZATION_ID,
    ) == legacy_path.resolve()
    with pytest.raises(ResumeServiceError, match="resume_original_file_not_found"):
        resolve_uploaded_resume_path(
            settings,
            storage_key="historic.pdf",
            organization_id=ORGANIZATION_A,
        )
    with pytest.raises(ResumeServiceError, match="resume_original_file_not_found"):
        resolve_uploaded_resume_path(
            settings,
            storage_key=f"{ORGANIZATION_A}/../historic.pdf",
            organization_id=ORGANIZATION_A,
        )


def test_same_idempotency_key_is_independent_per_workspace(
    tmp_path: Path,
) -> None:
    database = _database_with_workspaces()
    settings = _settings(tmp_path)
    key = "same-client-retry-key"
    content_hash = "a" * 64

    with database.session_factory() as session:
        _, resume_a = _save_resume(
            session,
            settings=settings,
            organization_id=ORGANIZATION_A,
        )
        with _workspace(session, ORGANIZATION_A):
            register_upload_idempotency_key(
                session,
                idempotency_key=key,
                content_sha256=content_hash,
                resume_id=resume_a.id,
            )
            session.commit()

        _, resume_b = _save_resume(
            session,
            settings=settings,
            organization_id=ORGANIZATION_B,
        )
        with _workspace(session, ORGANIZATION_B):
            register_upload_idempotency_key(
                session,
                idempotency_key=key,
                content_sha256=content_hash,
                resume_id=resume_b.id,
            )
            session.commit()
            assert (
                get_idempotent_upload_resume(
                    session,
                    idempotency_key=key,
                    content_sha256=content_hash,
                ).id
                == resume_b.id
            )

        with _workspace(session, ORGANIZATION_A):
            assert (
                get_idempotent_upload_resume(
                    session,
                    idempotency_key=key,
                    content_sha256=content_hash,
                ).id
                == resume_a.id
            )
            with pytest.raises(IdempotencyConflictError):
                get_idempotent_upload_resume(
                    session,
                    idempotency_key=key,
                    content_sha256="b" * 64,
                )

    database.dispose()


def test_new_storage_key_is_never_flat() -> None:
    key = build_resume_storage_key(organization_id=ORGANIZATION_A, suffix=".PDF")
    assert key.startswith(f"{ORGANIZATION_A}/")
    assert key.endswith(".pdf")
    assert key.count("/") == 1
