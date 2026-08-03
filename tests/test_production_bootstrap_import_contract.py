from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_section(helper: str) -> str:
    return helper.split("bootstrap_import_unlocked()", maxsplit=1)[1].split(
        "verify_public_runtime()", maxsplit=1
    )[0]


def test_bootstrap_import_is_explicit_and_scoped_to_exact_production_resources() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    section = _bootstrap_section(helper)

    assert "IMPORT_PRODUCTION_SNAPSHOT" in section
    assert "resume-screening-v3_postgres_data" in helper
    assert "resume-screening-v3_uploads_data" in helper
    assert "resume-screening-v3-staging" not in section
    assert "docker volume prune" not in section
    assert "current-release.env" in section
    assert "pending-release.env" in section
    assert "Production bootstrap import refuses an existing PostgreSQL volume." in section
    assert "Production bootstrap import refuses an existing uploads volume." in section
    assert "require_no_production_runtime" in section
    assert "validate_bootstrap_import_bundle" in section
    assert "--network none" in section
    assert "chown -R 10001:10001 /target" in section
    assert "! -L \"$current_record\"" in section
    assert "! -L \"$pending_record\"" in section
    assert "! -L \"$marker\"" in section
    assert "Current production release record must be a regular file." in helper
    assert "Pending production release record must be a regular file." in helper


def test_first_standard_release_consumes_only_a_ready_import_marker_and_records_it() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    release = helper.split("release_unlocked()", maxsplit=1)[1].split(
        "with_release_lock()", maxsplit=1
    )[0]
    deploy = helper.split("deploy_target()", maxsplit=1)[1].split(
        "release_unlocked()", maxsplit=1
    )[0]

    assert "load_bootstrap_import" in release
    assert "create_bootstrap_import_backup" in release
    assert "bootstrap_import_id=$bootstrap_import_id" in release
    assert "only be consumed by a forward migration-aware deployment" in release
    assert "verify_bootstrap_target_runtime" in deploy
    assert "bootstrap_target_local" in deploy
    assert "archive_bootstrap_import_marker" in deploy
    assert "bootstrap_import_id=%s" in helper
    assert "public_cutover_check" in helper


def test_first_release_marks_recovery_state_before_starting_a_temporary_database() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    backup = helper.split("create_bootstrap_import_backup()", maxsplit=1)[1].split(
        "bootstrap_import_unlocked()", maxsplit=1
    )[0]
    release = helper.split("release_unlocked()", maxsplit=1)[1].split(
        "with_release_lock()", maxsplit=1
    )[0]

    assert backup.index("write_bootstrap_attempted_marker") < backup.index("up -d --no-build db")
    assert "mode=deploy" in backup
    assert "rm -sf db" in backup
    assert "--remove-orphans" not in _bootstrap_section(helper)
    assert "write_bootstrap_attempted_marker" not in release


def test_bootstrap_local_verifier_exercises_private_caddy_to_api_path() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")

    assert ":8081 {" in caddy
    assert "@bootstrap_local_health path /health" in caddy
    assert "reverse_proxy api:8000" in caddy
    assert "http://{caddy_address}:8081/health" in helper
    assert '"8081:8081"' not in compose


def test_healthy_pending_finalization_can_only_recover_a_matching_imported_first_release() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    finalizer = helper.split("finalize_healthy_pending_target_unlocked()", maxsplit=1)[1].split(
        "restore_unlocked()", maxsplit=1
    )[0]

    assert "pending_bootstrap_import_id" in finalizer
    assert "current_bootstrap_import_id" in finalizer
    assert "bootstrap_target_local" in finalizer
    assert "verify_bootstrap_target_runtime" in finalizer
    assert "archive_bootstrap_import_marker" in finalizer
    assert "current_previous_tag" in finalizer


def test_failed_first_release_has_a_separate_confirmed_restore_path() -> None:
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")
    restore_wrapper = (ROOT / "scripts" / "restore-production-bootstrap.sh").read_text(
        encoding="utf-8"
    )

    assert "recover_bootstrap_import_unlocked" in helper
    assert "RESTORE_PRODUCTION_BOOTSTRAP" in helper
    assert "deploy_attempted" in helper
    assert "stop_bootstrap_attempted_production_runtime" in helper
    assert "resume-screening-v3-staging" not in helper.split(
        "stop_bootstrap_attempted_production_runtime()", maxsplit=1
    )[1].split("verify_public_runtime()", maxsplit=1)[0]
    assert "--confirm-restore" in restore_wrapper
    assert "StrictHostKeyChecking=yes" in restore_wrapper
    assert "bootstrap-restore" in restore_wrapper


def test_wrappers_never_dump_the_production_environment_file() -> None:
    import_wrapper = (ROOT / "scripts" / "bootstrap-import-production.sh").read_text(
        encoding="utf-8"
    )
    restore_wrapper = (ROOT / "scripts" / "restore-production-bootstrap.sh").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8")

    assert "--confirm-import" in import_wrapper
    assert "StrictHostKeyChecking=yes" in import_wrapper
    assert "cat \"$project_dir/.env.production\"" not in import_wrapper
    assert "cat \"$environment_dir/.env.production\"" not in helper
    assert "--env-file \"$environment_dir/.env.production\"" in helper
    assert "validate_production_bootstrap_bundle.py" in import_wrapper
    assert "validate_production_bootstrap_bundle.py" in restore_wrapper


def test_bootstrap_workflows_are_manual_production_environment_actions() -> None:
    import_workflow = (ROOT / ".github" / "workflows" / "production-bootstrap-import.yml").read_text(
        encoding="utf-8"
    )
    restore_workflow = (ROOT / ".github" / "workflows" / "production-bootstrap-restore.yml").read_text(
        encoding="utf-8"
    )

    for workflow in (import_workflow, restore_workflow):
        assert "workflow_dispatch:" in workflow
        assert "  push:" not in workflow
        assert "environment:\n      name: production" in workflow
        assert "group: greatsellai-hr-release-lane" in workflow
        assert "refs/heads/main" in workflow
        assert "STAGING_" not in workflow
    assert "IMPORT_PRODUCTION_SNAPSHOT" in import_workflow
    assert "scripts/bootstrap-import-production.sh" in import_workflow
    assert "RESTORE_PRODUCTION_BOOTSTRAP" in restore_workflow
    assert "scripts/restore-production-bootstrap.sh" in restore_workflow


def test_successful_bootstrap_import_returns_zero_and_wrapper_only_reports_remote_success(
    tmp_path: Path,
) -> None:
    """A successful import must disarm its function-local EXIT cleanup trap.

    The remote helper is a top-level Bash program.  A trap installed inside
    ``bootstrap_import_unlocked`` otherwise remains registered after the
    function returns, when its locals no longer exist under ``set -u``.  This
    harness runs the real happy-path function with every Docker operation
    stubbed, then lets the shell exit exactly as the remote wrapper does.
    """

    if os.name != "posix":
        pytest.skip("the bootstrap success harness is exercised in Linux CI")

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote release helper contract")

    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(encoding="utf-8").replace(
        "\r\n", "\n"
    )
    definitions, separator, _ = helper.partition('case "${1:-}" in')
    assert separator, "remote helper dispatch block is missing"

    history_dir = tmp_path / "history"
    environment_dir = tmp_path / "environment"
    validator = tmp_path / "validator.py"
    harness = tmp_path / "bootstrap-import-success.sh"
    harness.write_text(
        definitions
        + f"""
history_dir={history_dir.as_posix()!r}
environment_dir={environment_dir.as_posix()!r}
validator={validator.as_posix()!r}
import_id=bootstrap-success

mkdir -p "$history_dir/incoming/$import_id" "$environment_dir"
: > "$history_dir/incoming/$import_id/database.dump"
: > "$validator"

validate_environment_dir() {{ :; }}
validate_history_dir() {{ :; }}
require_safe_bootstrap_import_id() {{ :; }}
require_no_production_runtime() {{ :; }}
uploads_volume_exists() {{ return 1; }}
postgres_volume_exists() {{ [[ "${{bootstrap_db_created:-0}}" == "1" ]]; }}
validate_bootstrap_import_bundle() {{ :; }}
write_bootstrap_database_compose() {{ : > "$1"; }}
wait_for_bootstrap_database() {{ :; }}
require_production_volume_provenance() {{ :; }}
bootstrap_compose_run() {{
  if [[ "$*" == *"up -d --no-build db"* ]]; then
    bootstrap_db_created=1
  fi
  return 0
}}
sudo() {{ return 0; }}
write_bootstrap_import_marker() {{
  printf 'state=%s\\n' "$4" > "$1/bootstrap-import.env"
}}

bootstrap_import_unlocked \
  "$environment_dir" \
  "$history_dir" \
  "$import_id" \
  IMPORT_PRODUCTION_SNAPSHOT \
  "$validator"

[[ -f "$history_dir/bootstrap-import.env" ]]
[[ -d "$history_dir/bootstrap-imports/$import_id" ]]
[[ ! -e "$history_dir/bootstrap-imports/.$import_id.partial" ]]
[[ -z "$(trap -p EXIT)" ]]
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
    assert "Production bootstrap import completed." in completed.stdout
    assert (history_dir / "bootstrap-import.env").read_text(encoding="utf-8") == "state=ready\n"

    restore = helper.split("recover_bootstrap_import_unlocked()", maxsplit=1)[1].split(
        "verify_public_runtime()", maxsplit=1
    )[0]
    assert restore.index("restored=1") < restore.index("trap - EXIT") < restore.index(
        "Production bootstrap restore completed."
    )

    import_wrapper = (ROOT / "scripts" / "bootstrap-import-production.sh").read_text(
        encoding="utf-8"
    )
    remote_call = 'ssh_run "bash $(shell_quote "$remote_helper") bootstrap-import'
    success_message = 'echo "Production bootstrap import completed for $import_id."'
    assert remote_call in import_wrapper
    assert "|| true" not in import_wrapper[import_wrapper.index(remote_call) :]
    assert import_wrapper.index(remote_call) < import_wrapper.index(success_message)


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is required on deployment targets")
def test_linux_release_and_bootstrap_shell_contracts_parse() -> None:
    for script_name in (
        "remote-release-helper.sh",
        "deploy-production.sh",
        "bootstrap-import-production.sh",
        "restore-production-bootstrap.sh",
    ):
        subprocess.run(
            ["bash", "-n"],
            input=(ROOT / "scripts" / script_name)
            .read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .encode("utf-8"),
            check=True,
            cwd=ROOT,
        )
