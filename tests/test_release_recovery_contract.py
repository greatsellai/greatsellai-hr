from __future__ import annotations

import io
import re
import sys
import tarfile
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPOSITORY_ROOT / "scripts" / "run_release_regression.py"


def _load_runner():
    module_name = "release_regression_contract_runner"
    specification = spec_from_file_location(module_name, RUNNER_PATH)
    assert specification is not None and specification.loader is not None
    module = module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    return module


def _write_upload_archive(path: Path, *, member_name: str = "originals/synthetic.pdf") -> None:
    payload = b"synthetic-original-file"
    with tarfile.open(path, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def test_release_backup_bundle_is_published_only_after_both_artifacts_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()

    def fake_dump_database(*, destination: Path, **_: object) -> None:
        destination.write_bytes(b"synthetic-postgres-custom-format")

    def fake_archive_uploads_volume(*, archive_path: Path, **_: object) -> None:
        _write_upload_archive(archive_path)

    monkeypatch.setattr(runner, "_dump_database", fake_dump_database)
    monkeypatch.setattr(runner, "_archive_uploads_volume", fake_archive_uploads_volume)

    backup = runner._create_release_backup_bundle(
        postgres_container="synthetic-postgres",
        uploads_volume="synthetic-uploads",
        bundle_root=tmp_path,
        password="not-persisted",
        run_label="synthetic-run",
    )

    assert backup.is_dir()
    assert not list(tmp_path.glob(".*.partial"))
    assert (backup / "database.dump").read_bytes() == b"synthetic-postgres-custom-format"
    runner._validate_release_backup_bundle(
        completed=backup,
        expected_backup_id="synthetic-synthetic-run",
    )

    (backup / "database.dump").write_bytes(b"tampered")
    with pytest.raises(runner.RegressionFailure, match="checksum_mismatch"):
        runner._validate_release_backup_bundle(
            completed=backup,
            expected_backup_id="synthetic-synthetic-run",
        )


def test_failed_bundle_never_leaves_a_partial_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()

    def fake_dump_database(*, destination: Path, **_: object) -> None:
        destination.write_bytes(b"synthetic-postgres-custom-format")

    def failing_archive_uploads_volume(**_: object) -> None:
        raise runner.RegressionFailure("synthetic_archive_failure")

    monkeypatch.setattr(runner, "_dump_database", fake_dump_database)
    monkeypatch.setattr(runner, "_archive_uploads_volume", failing_archive_uploads_volume)

    with pytest.raises(runner.RegressionFailure, match="synthetic_archive_failure"):
        runner._create_release_backup_bundle(
            postgres_container="synthetic-postgres",
            uploads_volume="synthetic-uploads",
            bundle_root=tmp_path,
            password="not-persisted",
            run_label="synthetic-run",
        )

    assert not list(tmp_path.iterdir())


def test_upload_backup_validation_rejects_traversal_members(tmp_path: Path) -> None:
    runner = _load_runner()
    archive_path = tmp_path / "uploads.tar.gz"
    _write_upload_archive(archive_path, member_name="../outside")

    with pytest.raises(runner.RegressionFailure, match="path_unsafe"):
        runner._validate_upload_archive(archive_path)


def test_named_upload_volume_is_initialized_for_the_unprivileged_app_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_docker(*arguments: str, **kwargs: object) -> str:
        calls.append((arguments, kwargs))
        return ""

    monkeypatch.setattr(runner, "_docker", fake_docker)

    runner._create_volume(volume_name="synthetic-uploads", run_label="synthetic-run")

    assert calls[0][0][:2] == ("volume", "create")
    initialize_arguments, initialize_kwargs = calls[1]
    assert initialize_arguments[:4] == ("run", "--rm", "--network", "none")
    assert "--user" in initialize_arguments
    assert initialize_arguments[initialize_arguments.index("--user") + 1] == "0"
    assert "type=volume,src=synthetic-uploads,dst=/uploads" in initialize_arguments
    assert "mkdir -p /uploads && chown -R 10001:10001 /uploads" in initialize_arguments
    assert initialize_kwargs["label"] == "temporary_upload_volume_initialize"


def test_release_scripts_require_paired_recovery_and_explicit_targeting() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    deploy = (REPOSITORY_ROOT / "scripts" / "deploy-production.sh").read_text(
        encoding="utf-8"
    )
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "uploads.tar.gz" in helper
    assert "checksums.sha256" in helper
    assert "state=complete" in helper
    assert "Restore requires the exact confirmation RESTORE." in helper
    assert "pg_restore --list /backup/database.dump" in helper
    assert "Missing deployment target" in deploy
    assert "58.87.96.20" not in deploy
    assert "git show \"$tag:compose.yml\"" in deploy
    assert "config --quiet" in deploy
    assert deploy.index('git show "$tag:compose.yml"') < deploy.index("remote_release precheck")
    assert "requirements-production.lock" in dockerfile


def test_production_lock_covers_every_runtime_dependency_exactly() -> None:
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock_lines = (REPOSITORY_ROOT / "requirements-production.lock").read_text(
        encoding="utf-8"
    ).splitlines()
    lock = {
        line.split("==", maxsplit=1)[0].lower().replace("_", "-")
        for line in lock_lines
        if line and not line.startswith("#") and "==" in line
    }
    assert all(
        line.startswith("#") or not line or "==" in line
        for line in lock_lines
    )

    runtime_packages = {
        re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0]
        .strip()
        .lower()
        .replace("_", "-")
        for dependency in project["project"]["dependencies"]
    }
    assert runtime_packages <= lock
