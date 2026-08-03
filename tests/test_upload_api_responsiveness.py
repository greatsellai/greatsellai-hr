from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest

import app.main as main_module
from app.config import AppSettings
from app.main import create_app
from app.services import resume_service
from test_resume_flow import make_pdf_with_text


@pytest.fixture
def anyio_backend() -> str:
    """Keep these event-loop isolation checks on asyncio."""

    return "asyncio"


def _settings(tmp_path: Path) -> AppSettings:
    data_dir = tmp_path / "data"
    return AppSettings(
        project_dir=tmp_path,
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        # The responsive path deliberately uses an auth Session plus a
        # separate persistence Session. A file-backed SQLite pool mirrors that
        # production shape; ``sqlite://`` uses one StaticPool connection and
        # cannot safely model two concurrent transactions.
        database_url=f"sqlite:///{(data_dir / 'test.db').as_posix()}",
        allow_unauthenticated=True,
        min_text_chars_per_page=20,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("upload_target", ["new_candidate", "existing_candidate"])
async def test_slow_upload_persistence_does_not_block_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upload_target: str,
) -> None:
    """A slow fsync must not stop the API loop from serving /health."""

    app = create_app(_settings(tmp_path))
    write_started = threading.Event()
    write_finished = threading.Event()
    original_write = resume_service._write_upload_atomically

    def slow_write(*, storage_path: Path, content: bytes) -> None:
        write_started.set()
        time.sleep(0.4)
        try:
            original_write(storage_path=storage_path, content=content)
        finally:
            write_finished.set()

    monkeypatch.setattr(resume_service, "_write_upload_atomically", slow_write)
    document = make_pdf_with_text("resilient upload health check " * 20)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            if upload_target == "existing_candidate":
                candidate = await client.post(
                    "/v1/candidates",
                    json={"display_name": "Responsiveness test"},
                )
                assert candidate.status_code == 200, candidate.text
                upload_path = f"/v1/candidates/{candidate.json()['candidate_id']}/resumes"
            else:
                upload_path = "/v1/resumes/upload"

            upload_task = asyncio.create_task(
                client.post(
                    upload_path,
                    files={"file": ("slow-write.pdf", document, "application/pdf")},
                )
            )
            assert await asyncio.to_thread(write_started.wait, 1.0)
            assert not write_finished.is_set()

            health = await asyncio.wait_for(client.get("/health"), timeout=0.2)
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            auth_session = await asyncio.wait_for(
                client.get("/v1/auth/session"),
                timeout=0.2,
            )
            assert auth_session.status_code == 200, auth_session.text
            # The old inline implementation cannot reach this point until the
            # blocking write returns, which makes this assertion fail. The
            # auth-session request covers the browser's initial login check,
            # not only the dependency-free health endpoint.
            assert not write_finished.is_set()

            uploaded = await asyncio.wait_for(upload_task, timeout=2.0)
            assert uploaded.status_code == 200, uploaded.text
            assert write_finished.is_set()


@pytest.mark.anyio
async def test_upload_persistence_backpressure_rejects_without_queueing_unbounded_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saturated persistence lane must return a retryable 503 promptly."""

    monkeypatch.setattr(main_module, "_UPLOAD_PERSISTENCE_CONCURRENCY", 1)
    monkeypatch.setattr(main_module, "_UPLOAD_PERSISTENCE_QUEUE_TIMEOUT_SECONDS", 0.05)

    app = create_app(_settings(tmp_path))
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = resume_service._write_upload_atomically

    def blocked_write(*, storage_path: Path, content: bytes) -> None:
        write_started.set()
        if not release_write.wait(2.0):
            raise RuntimeError("test upload write was not released")
        original_write(storage_path=storage_path, content=content)

    monkeypatch.setattr(resume_service, "_write_upload_atomically", blocked_write)
    first_task: asyncio.Task[httpx.Response] | None = None

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                first_task = asyncio.create_task(
                    client.post(
                        "/v1/resumes/upload",
                        files={
                            "file": (
                                "first.pdf",
                                make_pdf_with_text("first upload " * 30),
                                "application/pdf",
                            )
                        },
                    )
                )
                # The assertion only waits for the executor to *begin* the
                # deliberately blocked fsync.  On a loaded CI runner, thread
                # scheduling can exceed one second even though the bounded
                # persistence lane is working correctly; the 503 assertion
                # below remains the actual backpressure deadline.
                assert await asyncio.to_thread(write_started.wait, 3.0)

                waiting_started_at = time.perf_counter()
                saturated = await client.post(
                    "/v1/resumes/upload",
                    files={
                        "file": (
                            "second.pdf",
                            make_pdf_with_text("second upload " * 30),
                            "application/pdf",
                        )
                    },
                )
                waiting_elapsed = time.perf_counter() - waiting_started_at

                assert saturated.status_code == 503, saturated.text
                assert saturated.json()["detail"] == "upload_persistence_busy"
                assert waiting_elapsed < 0.5

                release_write.set()
                first = await asyncio.wait_for(first_task, timeout=2.0)
                assert first.status_code == 200, first.text

                queue = await client.get("/v1/resumes/review-queue")
                assert queue.status_code == 200, queue.text
                assert queue.json()["total"] == 1
    finally:
        release_write.set()
        if first_task is not None and not first_task.done():
            await asyncio.wait_for(first_task, timeout=2.0)
