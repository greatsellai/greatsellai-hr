from __future__ import annotations

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
