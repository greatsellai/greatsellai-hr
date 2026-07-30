from __future__ import annotations

from pathlib import Path

import pytest

from app import ai_extraction_worker
from app.config import AppSettings


def _settings(tmp_path: Path, **changes: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "sqlite://",
        "auto_create_schema": True,
        "seed_registry_on_startup": True,
    }
    values.update(changes)
    return AppSettings(**values)  # type: ignore[arg-type]


class _FakeProcess:
    def __init__(self, *, target: object, args: tuple[object, ...], name: str) -> None:
        self.target = target
        self.args = args
        self.name = name
        self.daemon = True
        self.started = False
        self.terminated = False
        self.exitcode: int | None = None

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.started and not self.terminated

    def terminate(self) -> None:
        self.terminated = True
        self.exitcode = 0

    def join(self, timeout: float | None = None) -> None:
        del timeout


class _FakeProcessContext:
    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []

    def Process(self, *, target: object, args: tuple[object, ...], name: str) -> _FakeProcess:
        process = _FakeProcess(target=target, args=args, name=name)
        self.processes.append(process)
        return process


def test_worker_supervisor_spawns_the_configured_number_of_isolated_processes(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        database_url="postgresql+psycopg://resume:test@db/resume",
        auto_create_schema=False,
        seed_registry_on_startup=False,
        worker_concurrency=3,
    )
    context = _FakeProcessContext()

    processes = ai_extraction_worker._spawn_worker_processes(
        settings,
        process_context=context,
    )

    assert processes == context.processes
    assert [process.name for process in processes] == [
        "resume-v3-worker-1",
        "resume-v3-worker-2",
        "resume-v3-worker-3",
    ]
    assert all(process.target is ai_extraction_worker._run_worker_process for process in processes)
    assert all(process.args == (settings,) for process in processes)
    assert all(process.daemon is False and process.started for process in processes)


def test_worker_supervisor_keeps_default_single_process_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    calls: list[AppSettings] = []
    monkeypatch.setattr(
        ai_extraction_worker,
        "_run_worker_process",
        lambda value: calls.append(value),
    )

    ai_extraction_worker.run_worker_supervisor(settings)

    assert calls == [settings]


def test_worker_supervisor_rejects_sqlite_for_multiple_processes(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_concurrency=2)

    with pytest.raises(
        ValueError,
        match="RESUME_V3_WORKER_CONCURRENCY_GT_1_REQUIRES_POSTGRESQL",
    ):
        ai_extraction_worker._validate_worker_supervisor_settings(settings)


def test_worker_supervisor_rejects_non_postgresql_for_multiple_processes(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        database_url="mysql+pymysql://resume:test@db/resume",
        auto_create_schema=False,
        seed_registry_on_startup=False,
        worker_concurrency=2,
    )

    with pytest.raises(
        ValueError,
        match="RESUME_V3_WORKER_CONCURRENCY_GT_1_REQUIRES_POSTGRESQL",
    ):
        ai_extraction_worker._validate_worker_supervisor_settings(settings)


def test_worker_database_uses_the_dedicated_worker_connection_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        tmp_path,
        auto_create_schema=False,
        seed_registry_on_startup=False,
        database_pool_size=9,
        database_max_overflow=8,
        worker_database_pool_size=2,
        worker_database_max_overflow=1,
    )
    captured: dict[str, object] = {}

    class _SessionFactory:
        def __call__(self) -> "_SessionFactory":
            return self

        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_: object) -> None:
            return None

    class _Database:
        session_factory = _SessionFactory()

        def create_all(self) -> None:
            raise AssertionError("schema bootstrap must stay disabled")

    def fake_database(
        database_url: str,
        *,
        pool_size: int,
        max_overflow: int,
    ) -> _Database:
        captured.update(
            {
                "database_url": database_url,
                "pool_size": pool_size,
                "max_overflow": max_overflow,
            }
        )
        return _Database()

    monkeypatch.setattr(ai_extraction_worker, "Database", fake_database)
    monkeypatch.setattr(
        ai_extraction_worker,
        "is_institution_registry_seeded",
        lambda _session: True,
    )

    ai_extraction_worker._create_worker_database(settings)

    assert captured == {
        "database_url": "sqlite://",
        "pool_size": 2,
        "max_overflow": 1,
    }


def test_worker_connection_budget_is_bounded(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        worker_concurrency=16,
        worker_database_pool_size=3,
        worker_database_max_overflow=0,
    )

    with pytest.raises(ValueError, match="worker database connection budget"):
        settings.validate_runtime()
