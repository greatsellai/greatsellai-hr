from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
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
    assert 'postgres_volume_name="resume-screening-v3_postgres_data"' in helper
    assert "checksums.sha256" in helper
    assert "state=complete" in helper
    assert "Restore requires the exact confirmation RESTORE." in helper
    assert "tar -tzf /backup/uploads.tar.gz >/dev/null" in helper
    assert "Unsupported uploads archive member type." in helper
    assert "pg_restore --list /backup/database.dump" in helper
    assert "Missing deployment target" in deploy
    assert "58.87.96.20" not in deploy
    assert "release-sources" in helper
    assert "with_release_lock" in helper
    assert "up -d --no-build --no-deps api worker" in helper
    assert 'compose_run "$runtime_source_dir" "$environment_dir" "$previous_commit"' in helper
    assert "source_dir=$target_source_dir" in helper
    assert "current-release.env" in helper
    assert "Unresolved pending release" in helper
    assert 'tar -x -C $(shell_quote "$project_dir")' not in deploy
    assert 'cat "$repo_root/scripts/remote-release-helper.sh"' in deploy
    assert 'cat "$repo_root/scripts/release_source_stage.py"' in deploy
    assert 'bash $(shell_quote "$remote_helper") release' in deploy
    assert "Target tag predates the current production release." in deploy
    assert "state=complete" in deploy
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


def test_release_transaction_prepares_target_before_quiescing_current_runtime() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    transaction = helper.split("release_unlocked()", maxsplit=1)[1].split(
        "with_release_lock()", maxsplit=1
    )[0]

    assert transaction.index("prepare_target_images") < transaction.index("create_backup_bundle")
    assert "compose_run \"$target_source_dir\" \"$environment_dir\" \"$target_commit\" build api caddy" in helper
    assert "up -d --no-build api worker caddy" in helper
    assert "flock -n 9" in helper
    assert "refusing to treat persistent data as an initial deployment" in helper.lower()


def test_current_release_record_commits_before_the_complete_history_record() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    record_writer = helper.split("write_release_records()", maxsplit=1)[1].split(
        "deploy_target()", maxsplit=1
    )[0]

    current_commit = 'mv -f "$temporary_current" "$history_dir/current-release.env"'
    history_publish = 'mv "$temporary_record" "$record"'
    assert record_writer.index(current_commit) < record_writer.index("deployment_succeeded=1")
    assert record_writer.index("deployment_succeeded=1") < record_writer.index(history_publish)


def test_release_transaction_passes_all_double_digit_arguments_to_the_source_stager(
    tmp_path: Path,
) -> None:
    """Guard the shell positional-parameter boundary used by the SSH release call.

    The remote helper receives eleven release arguments.  In Bash, ``$10`` is
    parsed as ``${1}0`` rather than the tenth argument, so this tiny harness
    executes the real transaction with all side effects stubbed out.  It keeps
    the archive checksum and staged-tool path from silently becoming literals.
    """

    if os.name != "posix":
        pytest.skip("the Bash transaction harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    history_dir = tmp_path / "history"
    captured_arguments = tmp_path / "captured-stage-arguments.txt"
    checksum = "a" * 64
    stage_tool = "/tmp/release-stage.py"
    harness = tmp_path / "release-helper-arguments.sh"
    harness.write_text(
        definitions
        + f"""
validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_release_reference() {{ :; }}
stage_target_source() {{
  printf '%s|%s\\n' "$3" "$4" > {captured_arguments.as_posix()!r}
  printf '%s/source' "$1"
}}
prepare_target_images() {{ :; }}
load_current_runtime() {{
  current_tag=""
  current_commit=""
  current_source_dir="$1"
}}
compose_run() {{ :; }}
deploy_target() {{ :; }}

release_unlocked \
  {tmp_path.as_posix()!r} \
  {history_dir.as_posix()!r} \
  prod-20260722-1234567 \
  {'b' * 40} \
  __none__ \
  __none__ \
  deploy \
  0 \
  0 \
  {checksum} \
  {stage_tool}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [bash, str(harness)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert captured_arguments.read_text(encoding="utf-8").strip() == f"{checksum}|{stage_tool}"
