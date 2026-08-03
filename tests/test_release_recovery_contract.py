from __future__ import annotations

import hashlib
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


def test_any_active_pending_release_blocks_normal_release_until_audited_reconciliation() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    pending_gate = helper.split("resolve_pending_release()", maxsplit=1)[1].split(
        "create_backup_bundle()", maxsplit=1
    )[0]

    assert "refusing to overwrite interrupted release state" in pending_gate
    assert 'rm -f "$pending_record"' not in pending_gate
    assert "Use an explicit, audited reconciliation" in pending_gate
    restore = helper.split("restore_unlocked()", maxsplit=1)[1].split(
        "restore()", maxsplit=1
    )[0]
    assert 'resolve_pending_release "$history_dir"' in restore


def test_public_runtime_verification_can_read_a_root_owned_environment_file() -> None:
    """Release health checks must not fail merely because the env file is private."""

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    verification = helper.split("verify_public_runtime()", maxsplit=1)[1].split(
        "validate_pending_target_source()", maxsplit=1
    )[0]

    assert "sudo -n sed -n 's/^RESUME_V3_DOMAIN=//p'" in verification
    assert 'cat "$environment_dir/.env.production"' not in verification


def test_pending_target_finalizer_is_specific_and_preserves_the_normal_pending_gate() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    wrapper = (REPOSITORY_ROOT / "scripts" / "finalize-pending-release.sh").read_text(
        encoding="utf-8"
    )
    finalizer = helper.split("finalize_pending_target_unlocked()", maxsplit=1)[1].split(
        "finalize_pending_target()", maxsplit=1
    )[0]

    assert "FINALIZE_PENDING_PROXY_STARTUP" in finalizer
    assert "validate_pending_target_source" in finalizer
    assert "validate_pending_target_backup" in finalizer
    assert "Pending target paired backup" in helper
    assert 'docker network connect --alias api --ip "$api_proxy_ip"' in finalizer
    assert 'docker inspect --format \'{{.Name}}\' "$api_container"' in finalizer
    assert "api_proxy_aliases" in finalizer
    assert '{{$container.Name}}' in finalizer
    assert '{{"\\n"}}' in finalizer
    assert '{{"\\\\n"}}' not in finalizer
    assert 'docker network rm' not in finalizer
    assert "Pending target migration did not complete successfully." in finalizer
    assert "verify_public_runtime" in finalizer
    assert "archive_finalized_pending_record" in finalizer
    assert "finalize-pending-target" in helper
    assert "FINALIZE_PENDING_PROXY_STARTUP" in wrapper


def test_healthy_pending_finalizer_archives_only_an_already_healthy_target() -> None:
    """This recovery path must not become a second deployment mechanism."""

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    wrapper = (
        REPOSITORY_ROOT / "scripts" / "finalize-healthy-pending-release.sh"
    ).read_text(encoding="utf-8")
    finalizer = helper.split("finalize_healthy_pending_target_unlocked()", maxsplit=1)[
        1
    ].split("finalize_healthy_pending_target()", maxsplit=1)[0]

    assert "FINALIZE_HEALTHY_PENDING_RUNTIME" in finalizer
    assert "Current release is not the exact healthy pending target." in finalizer
    assert "validate_pending_target_source" in finalizer
    assert "validate_pending_target_backup" in finalizer
    assert "Healthy pending target API is not healthy." in finalizer
    assert "Healthy pending target proxy network has unexpected members." in finalizer
    assert "caddy validate --config /etc/caddy/Caddyfile" in finalizer
    assert "verify_public_runtime" in finalizer
    assert "archive_verified_healthy_pending_record" in finalizer
    assert "write_release_records" not in finalizer
    for prohibited in (
        "docker stop",
        "docker start",
        "docker rm",
        "docker network connect",
        "docker network disconnect",
        " compose_run \"$pending_source_dir\" \"$environment_dir\" \"$pending_commit\" up",
        "alembic",
        "pg_restore",
    ):
        assert prohibited not in finalizer

    assert "FINALIZE_HEALTHY_PENDING_RUNTIME" in wrapper
    assert "StrictHostKeyChecking=yes" in wrapper
    assert "finalize-healthy-pending-target" in wrapper
    assert "does not build, migrate, stop, start, recreate, restore, remove" in wrapper


def test_healthy_pending_finalizer_only_accepts_a_replayed_current_target(
    tmp_path: Path,
) -> None:
    """Exercise the actual Bash guard for the one recoverable record shape."""

    if os.name != "posix":
        pytest.skip("the release helper is executed by Bash on the production host")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    start = helper.index("archive_verified_healthy_pending_record()")
    end = helper.index("\nrestore_unlocked()", start)
    definitions = helper[start:end]
    tag = "prod-20260722-aaaaaaaa"
    commit = "a" * 40
    source_dir = tmp_path / "history" / "release-sources" / commit

    def run_case(*, pending_previous_tag: str, pending_previous_commit: str) -> subprocess.CompletedProcess[str]:
        case_root = tmp_path / pending_previous_commit[:7]
        history_dir = case_root / "history"
        project_dir = case_root / "project"
        source = history_dir / "release-sources" / commit
        (history_dir / "releases").mkdir(parents=True)
        project_dir.mkdir()
        current = history_dir / "current-release.env"
        pending = history_dir / "pending-release.env"
        current.write_text(
            "\n".join(
                (
                    "state=complete",
                    f"tag={tag}",
                    f"commit={commit}",
                    f"source_dir={source}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        pending.write_text(
            "\n".join(
                (
                    "format_version=1",
                    "state=prepared",
                    f"tag={tag}",
                    f"commit={commit}",
                    f"source_dir={source}",
                    f"previous_tag={pending_previous_tag}",
                    f"previous_commit={pending_previous_commit}",
                    "mode=deploy",
                    "backup_state=complete",
                    "backup_id=pre-synthetic",
                    "prepared_at=2026-07-22T00:00:00Z",
                    "",
                )
            ),
            encoding="utf-8",
        )
        harness = case_root / "healthy-pending-finalize.sh"
        harness.write_text(
            definitions
            + f"""
set -Eeuo pipefail
die() {{ echo "$*" >&2; exit 1; }}
validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_release_reference() {{ :; }}
record_value() {{ sed -n "s/^$2=//p" "$1" | tail -n 1; }}
load_current_runtime() {{
  current_tag="$(record_value "$2/current-release.env" tag)"
  current_commit="$(record_value "$2/current-release.env" commit)"
  current_source_dir="$(record_value "$2/current-release.env" source_dir)"
}}
validate_pending_target_source() {{ touch {str(case_root / 'source-validated')!r}; }}
validate_pending_target_backup() {{ touch {str(case_root / 'backup-validated')!r}; }}
require_production_caddy_image_without_legacy_staging_gateway() {{ :; }}
compose_service_container_id() {{ printf 'synthetic-%s' "$4"; }}
require_container_image() {{ :; }}
require_container_state() {{ :; }}
compose_run() {{ :; }}
verify_public_runtime() {{ touch {str(case_root / 'public-verified')!r}; }}
sudo() {{
  [[ "${{1:-}}" == -n ]] && shift
  [[ "${{1:-}}" == docker ]] || return 1
  shift
  case "${{1:-}}" in
    inspect)
      template="${{3:-}}"
      container="${{@: -1}}"
      case "$template" in
        *State.ExitCode*) printf 0 ;;
        *State.Health*) printf healthy ;;
        *range\\ .Aliases*) printf api ;;
        *NetworkSettings.Networks*)
          if [[ "$container" == synthetic-api ]]; then printf 172.30.0.3; else printf 172.30.0.2; fi
          ;;
        *Name*) printf '/%s' "$container" ;;
      esac
      ;;
    network)
      [[ "${{2:-}}" == inspect ]] && printf 'synthetic-api\\nsynthetic-caddy\\n'
      ;;
  esac
}}
api_proxy_ip=172.30.0.3
caddy_proxy_ip=172.30.0.2
proxy_network_name=resume-screening-v3_proxy
finalize_healthy_pending_target_unlocked \\
  {str(project_dir)!r} \\
  {str(history_dir)!r} \\
  {tag!r} \\
  {commit!r} \\
  FINALIZE_HEALTHY_PENDING_RUNTIME \\
  {'b' * 64}
""",
            encoding="utf-8",
        )
        return subprocess.run(
            [bash, str(harness)],
            text=True,
            capture_output=True,
            check=False,
        )

    successful = run_case(pending_previous_tag=tag, pending_previous_commit=commit)
    assert successful.returncode == 0, successful.stderr
    successful_root = tmp_path / commit[:7]
    assert not (successful_root / "history" / "pending-release.env").exists()
    assert (successful_root / "source-validated").exists()
    assert (successful_root / "backup-validated").exists()
    assert (successful_root / "public-verified").exists()
    assert list((successful_root / "history" / "releases").glob("finalized-healthy-*.env"))

    rejected = run_case(
        pending_previous_tag="prod-20260722-bbbbbbbb",
        pending_previous_commit="b" * 40,
    )
    assert rejected.returncode != 0
    rejected_root = tmp_path / "bbbbbbb"
    assert (rejected_root / "history" / "pending-release.env").exists()
    assert not list((rejected_root / "history" / "releases").glob("finalized-healthy-*.env"))


def test_legacy_reconciliation_refuses_structured_pending_metadata() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    transaction = helper.split("reconcile_legacy_pending_unlocked()", maxsplit=1)[1].split(
        "reconcile_legacy_pending()", maxsplit=1
    )[0]

    assert "pending_source_dir" in transaction
    assert "structured deployment fields" in transaction
    assert transaction.index("structured deployment fields") < transaction.index(
        "create_legacy_reconciliation_backup"
    )


def test_legacy_pending_reconciliation_requires_a_fresh_paired_backup_before_archiving() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    reconcile = (
        REPOSITORY_ROOT / "scripts" / "reconcile-legacy-pending-release.sh"
    ).read_text(encoding="utf-8")

    transaction = helper.split("reconcile_legacy_pending_unlocked()", maxsplit=1)[1].split(
        "reconcile_legacy_pending()", maxsplit=1
    )[0]
    backup = helper.split("create_legacy_reconciliation_backup()", maxsplit=1)[1].split(
        "reconcile_legacy_pending_unlocked()", maxsplit=1
    )[0]

    assert "RECONCILE_LEGACY_PENDING" in transaction
    assert "does not match the interrupted release's recorded predecessor" in transaction
    assert "require_legacy_runtime_quiescent" in transaction
    assert "uploads_volume_exists" in transaction
    assert "postgres_volume_exists" in transaction
    assert "require_legacy_compose_volume" in transaction
    assert "require_legacy_container_volume_mount" in transaction
    assert "require_legacy_uploads_volume_provenance" in transaction
    assert "legacy_migrate_state" in transaction
    assert transaction.index("create_legacy_reconciliation_backup") < transaction.index(
        'rm -f "$pending_record"'
    )
    assert "state=interrupted" in transaction
    assert "docker compose" not in transaction
    assert "pg_dump" in backup
    assert "checksums.sha256" in backup
    assert "pg_restore --list /backup/database.dump" in backup
    assert "--confirm" in reconcile
    assert "does not deploy, build, migrate, restart services" in reconcile
    assert "git merge-base --is-ancestor" in reconcile
    assert "StrictHostKeyChecking=yes" in reconcile
    assert "mktemp /tmp/greatsell-legacy-reconcile.XXXXXXXX" in reconcile


def test_legacy_pending_reconciliation_archives_only_a_matching_legacy_record(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash reconciliation harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    current_tag = "prod-20260721-1234567"
    current_commit = "a" * 40
    pending_tag = "prod-20260722-7654321"
    pending_commit = "b" * 40
    (history_dir / "current-release.env").write_text(
        f"tag={current_tag}\ncommit={current_commit}\n",
        encoding="utf-8",
    )
    pending_path = history_dir / "pending-release.env"
    pending_path.write_text(
        "\n".join(
            (
                f"tag={pending_tag}",
                f"commit={pending_commit}",
                f"previous_tag={current_tag}",
                f"previous_commit={current_commit}",
                "mode=deploy",
                "",
            )
        ),
        encoding="utf-8",
    )

    harness = tmp_path / "legacy-reconcile.sh"
    harness.write_text(
        definitions
        + f"""
validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_release_reference() {{ :; }}
uploads_volume_exists() {{ :; }}
postgres_volume_exists() {{ :; }}
require_legacy_runtime_quiescent() {{ :; }}
legacy_db_container() {{ printf synthetic-db; }}
create_legacy_reconciliation_backup() {{ printf reconcile-synthetic-backup; }}
require_safe_backup_id() {{ :; }}
require_legacy_compose_volume() {{ :; }}
require_legacy_container_volume_mount() {{ :; }}
require_legacy_uploads_volume_provenance() {{ :; }}
legacy_migrate_state() {{ printf exited:1; }}

reconcile_legacy_pending_unlocked \\
  {project_dir.as_posix()!r} \\
  {history_dir.as_posix()!r} \\
  {pending_tag} \\
  {'b' * 40} \\
  RECONCILE_LEGACY_PENDING
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
    assert not pending_path.exists()
    archives = list((history_dir / "releases").glob("interrupted-*.env"))
    assert len(archives) == 1
    archive = archives[0].read_text(encoding="utf-8")
    assert f"tag={pending_tag}" in archive
    assert "state=interrupted" in archive
    assert "reconciliation_backup_id=reconcile-synthetic-backup" in archive


def test_legacy_pending_reconciliation_preserves_pending_when_predecessor_mismatches(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash reconciliation harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    current_tag = "prod-20260721-1234567"
    pending_previous_tag = "prod-20260721-7654321"
    current_commit = "a" * 40
    pending_tag = "prod-20260722-7654321"
    pending_commit = "b" * 40
    (history_dir / "current-release.env").write_text(
        f"tag={current_tag}\ncommit={current_commit}\n",
        encoding="utf-8",
    )
    pending_path = history_dir / "pending-release.env"
    original_pending = "\n".join(
        (
            f"tag={pending_tag}",
            f"commit={pending_commit}",
            f"previous_tag={pending_previous_tag}",
            f"previous_commit={current_commit}",
            "mode=deploy",
            "",
        )
    )
    pending_path.write_text(original_pending, encoding="utf-8")

    harness = tmp_path / "legacy-reconcile-reject.sh"
    harness.write_text(
        definitions
        + f"""
validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_release_reference() {{ :; }}
uploads_volume_exists() {{ :; }}
postgres_volume_exists() {{ :; }}
require_legacy_runtime_quiescent() {{ :; }}
legacy_db_container() {{ printf synthetic-db; }}
create_legacy_reconciliation_backup() {{ printf should-not-run; }}
require_safe_backup_id() {{ :; }}
require_legacy_compose_volume() {{ :; }}
require_legacy_container_volume_mount() {{ :; }}
require_legacy_uploads_volume_provenance() {{ :; }}
legacy_migrate_state() {{ printf exited:1; }}

reconcile_legacy_pending_unlocked \\
  {project_dir.as_posix()!r} \\
  {history_dir.as_posix()!r} \\
  {pending_tag} \\
  {'b' * 40} \\
  RECONCILE_LEGACY_PENDING
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [bash, str(harness)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert pending_path.read_text(encoding="utf-8") == original_pending
    assert not list((history_dir / "releases").glob("interrupted-*.env"))


@pytest.mark.parametrize(
    (
        "api_proxy_address",
        "initial_caddy_state",
        "caddy_error",
        "api_proxy_aliases",
        "should_succeed",
    ),
    (
        (
            "172.30.0.2",
            "created",
            "failed to set up container networking: Address already in use",
            "api",
            True,
        ),
        ("172.30.0.3", "exited", "", "", True),
        ("172.30.0.3", "exited", "", "api", False),
    ),
    ids=(
        "initial-static-address-collision",
        "retry-after-api-reassignment",
        "reject-retry-after-alias-is-already-restored",
    ),
)
def test_pending_target_finalizer_advances_only_after_the_verified_proxy_recovery(
    tmp_path: Path,
    api_proxy_address: str,
    initial_caddy_state: str,
    caddy_error: str,
    api_proxy_aliases: str,
    should_succeed: bool,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash pending-target harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    history_dir = tmp_path / "history"
    (history_dir / "releases").mkdir(parents=True)
    (history_dir / "backups").mkdir()
    previous_tag = "prod-20260721-1234567"
    previous_commit = "a" * 40
    pending_tag = "prod-20260722-7654321"
    pending_commit = "b" * 40
    source_dir = history_dir / "release-sources" / pending_commit
    source_dir.mkdir(parents=True)
    (history_dir / "current-release.env").write_text(
        "\n".join(
            (
                "state=complete",
                f"tag={previous_tag}",
                f"commit={previous_commit}",
                f"source_dir={project_dir}",
                "",
            )
        ),
        encoding="utf-8",
    )
    pending_path = history_dir / "pending-release.env"
    pending_path.write_text(
        "\n".join(
            (
                f"tag={pending_tag}",
                f"commit={pending_commit}",
                f"source_dir={source_dir}",
                f"previous_tag={previous_tag}",
                f"previous_commit={previous_commit}",
                f"previous_source_dir={project_dir}",
                "mode=deploy",
                "backup_state=complete",
                "backup_id=pre-synthetic",
                "prepared_at=2026-07-22T00:00:00Z",
                "",
            )
        ),
        encoding="utf-8",
    )
    stage_tool = tmp_path / "activate.py"
    stage_tool.write_text("raise SystemExit(0)\n", encoding="utf-8")

    harness = tmp_path / "pending-target-finalize.sh"
    harness.write_text(
        definitions
        + f"""
set -Eeuo pipefail
validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_release_reference() {{ :; }}
load_current_runtime() {{
  current_tag={previous_tag!r}
  current_commit={'a' * 40!r}
  current_source_dir={project_dir.as_posix()!r}
}}
validate_pending_target_source() {{ touch {str(tmp_path / 'source-validated')!r}; }}
validate_pending_target_backup() {{ touch {str(tmp_path / 'backup-validated')!r}; }}
compose_service_container_id() {{ printf 'synthetic-%s' "${{4}}"; }}
require_container_image() {{ :; }}
require_container_state() {{ :; }}
compose_run() {{
  shift 3
  if [[ "${{1:-}}" == ps ]]; then
    printf 'synthetic-%s' "${{@: -1}}"
  fi
}}
verify_public_runtime() {{ touch {str(tmp_path / 'public-verified')!r}; }}
write_release_records() {{ touch {str(tmp_path / 'record-written')!r}; }}
archive_finalized_pending_record() {{
  touch {str(tmp_path / 'pending-archived')!r}
  rm -f "${{2}}"
}}
caddy_recreated=0
network_connect_args=""
sudo() {{
  [[ "${{1:-}}" == -n ]] && shift
  [[ "${{1:-}}" == docker ]] || return 1
  shift
  case "${{1:-}}" in
    image) return 0 ;;
    inspect)
      local template="${{3:-}}" container="${{@: -1}}"
      case "$template" in
        *"range .Aliases"*) printf {api_proxy_aliases!r} ;;
        *State.ExitCode*) printf 0 ;;
        *NetworkSettings.Networks*) printf {api_proxy_address!r} ;;
        *State.Error*) printf {caddy_error!r} ;;
        *State.Health*) printf healthy ;;
        *Name*) printf '/synthetic-api' ;;
        *State.Status*)
          if [[ "$container" == synthetic-caddy && "$caddy_recreated" == 0 ]]; then printf {initial_caddy_state!r}
          elif [[ "$container" == synthetic-migrate ]]; then printf exited
          else printf running
          fi
          ;;
        *) : ;;
      esac
      ;;
        network)
          if [[ "${{2:-}}" == inspect ]]; then
            printf synthetic-api
          elif [[ "${{2:-}}" == connect ]]; then
            network_connect_args="$*"
          else
            :
          fi
          ;;
    rm) caddy_recreated=1 ;;
    stop|start) : ;;
    *) : ;;
  esac
}}

finalize_pending_target_unlocked \\
  {project_dir.as_posix()!r} \\
  {history_dir.as_posix()!r} \\
  {pending_tag} \\
  {'b' * 40} \\
  FINALIZE_PENDING_PROXY_STARTUP \\
  {stage_tool.as_posix()!r} \\
  {'c' * 64}
printf '%s' "$network_connect_args" > {str(tmp_path / 'network-connect-args')!r}
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [bash, str(harness)],
        check=False,
        text=True,
        capture_output=True,
    )

    if not should_succeed:
        assert completed.returncode != 0
        assert pending_path.exists()
        return

    assert completed.returncode == 0, completed.stderr
    assert not pending_path.exists()
    assert "--alias api" in (tmp_path / "network-connect-args").read_text(encoding="utf-8")
    for marker in (
        "source-validated",
        "backup-validated",
        "public-verified",
        "record-written",
        "pending-archived",
    ):
        assert (tmp_path / marker).exists()


def test_pending_target_source_validation_binds_the_staged_tree_to_its_checksum(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash staged-source harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    commit = "c" * 40
    history_dir = tmp_path / "history"
    source_dir = history_dir / "release-sources" / commit
    (source_dir / "deploy").mkdir(parents=True)
    for relative in (
        "compose.yml",
        "Dockerfile",
        "deploy/Caddy.Dockerfile",
        "deploy/Caddyfile",
    ):
        (source_dir / relative).write_text(f"synthetic:{relative}\n", encoding="utf-8")
    (source_dir / ".greatsell-release-source.json").write_text(
        '{"archive_sha256":"' + "a" * 64 + '","format_version":1,"release_commit":"' + commit + '"}\n',
        encoding="utf-8",
    )

    rows: list[bytes] = []
    for candidate in sorted(source_dir.rglob("*")):
        if candidate.name == ".greatsell-release-source.json" or candidate.is_dir():
            continue
        content = candidate.read_bytes()
        blob = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content
        ).hexdigest()
        mode = "100755" if candidate.stat().st_mode & 0o111 else "100644"
        rows.append(
            f"{mode} blob {blob}\t{candidate.relative_to(source_dir).as_posix()}\n".encode()
        )
    checksum = hashlib.sha256(b"".join(rows)).hexdigest()

    harness = tmp_path / "validate-staged-source.sh"
    harness.write_text(
        definitions
        + f"""
validate_pending_target_source \\
  {history_dir.as_posix()!r} \\
  {source_dir.as_posix()!r} \\
  {commit} \\
  {checksum}
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
    assert 'image_mode="${12:-build}"' in helper
    assert 'release_phase "Verify CI-transferred images"' in transaction
    assert 'require_prebuilt_image "greatsellai-hr-api:$target_commit" "$target_commit"' in helper
    assert 'require_prebuilt_image "greatsellai-hr-caddy:$target_commit" "$target_commit"' in helper
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


def test_deploy_failure_cleanup_does_not_mask_the_original_error_under_nounset() -> None:
    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    deploy = helper.split("deploy_target()", maxsplit=1)[1].split(
        "release_unlocked()", maxsplit=1
    )[0]

    assert '"${deployment_succeeded:-0}"' in deploy
    assert '"${current_commit:-}"' in deploy
    assert '"${migration_changed:-1}"' in deploy


def test_schema_migration_quiesces_old_writers_before_target_migrate() -> None:
    """An old API must not write while the target migration is in flight."""

    helper = (REPOSITORY_ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )
    deploy = helper.split("deploy_target()", maxsplit=1)[1].split(
        "release_unlocked()", maxsplit=1
    )[0]

    phase = 'release_phase "Quiesce current writers before schema migration"'
    stop_writers = 'stop api worker </dev/null'
    target_migrate = 'up -d --no-build api worker caddy </dev/null'
    assert phase in deploy
    assert stop_writers in deploy
    assert target_migrate in deploy
    assert deploy.index(phase) < deploy.index(stop_writers) < deploy.index(target_migrate)
    quiesce = deploy.split(phase, maxsplit=1)[1].split(target_migrate, maxsplit=1)[0]
    assert quiesce.index("writers_quiesced=1") < quiesce.index(stop_writers)
    # The explicit branch protects normal no-schema-change releases from an
    # unnecessary second interruption while making every schema change safe.
    assert '[[ "$migration_changed" == "1" && -n "${current_commit:-}" ]]' in deploy
    assert "previous_schema_revision" in deploy
    assert "database_schema_revision" in helper
    # A migration may reject its no-write preflight (for example, no eligible
    # formal administrator). In that narrow state the recorded runtime can be
    # restored, but a changed revision deliberately leaves old writers down.
    assert 'if [[ "$observed_schema_revision" == "$previous_schema_revision" ]]; then' in deploy
    assert "Target migration may have advanced the database" in deploy


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
    captured_image_mode = tmp_path / "captured-image-mode.txt"
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
prepare_target_images() {{
  printf '%s\n' "$5" > {captured_image_mode.as_posix()!r}
}}
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
  {stage_tool} \
  prebuilt
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
    assert captured_image_mode.read_text(encoding="utf-8").strip() == "prebuilt"
