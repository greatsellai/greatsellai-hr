from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_automatic_release_preflights_the_candidate_before_creating_a_tag() -> None:
    workflow = (ROOT / ".github" / "workflows" / "production-release.yml").read_text(
        encoding="utf-8"
    )

    preflight = workflow.index("Preflight production configuration before tagging")
    create_tag = workflow.index("Create immutable production tag")
    deploy = workflow.index("Deploy tagged release")
    assert preflight < create_tag < deploy
    assert 'scripts/preflight-production-release.sh "$RELEASE_SHA"' in workflow
    assert "Reconfirm release target after preflight" in workflow
    assert "steps.ready.outputs.deploy == 'true'" in workflow


def test_retry_deploy_is_manual_and_uses_current_reviewed_tooling() -> None:
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "  push:" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "ref: main" in workflow
    assert 'git fetch origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"' in workflow


def test_legacy_pending_reconciliation_is_manual_and_uses_the_production_lock() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "production-legacy-reconcile.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "RECONCILE_LEGACY_PENDING" in workflow
    assert "group: greatsellai-hr-production" in workflow
    assert "environment:" in workflow and "name: production" in workflow
    assert "ref: main" in workflow
    assert 'scripts/reconcile-legacy-pending-release.sh "$PENDING_TAG" "$PENDING_COMMIT"' in workflow


def test_pending_target_finalization_is_manual_and_narrowly_confirmed() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "production-pending-finalize.yml"
    ).read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "finalize-pending-release.sh").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "FINALIZE_PENDING_PROXY_STARTUP" in workflow
    assert "group: greatsellai-hr-production" in workflow
    assert "environment:" in workflow and "name: production" in workflow
    assert "ref: main" in workflow
    assert 'scripts/finalize-pending-release.sh "$PENDING_TAG" "$PENDING_COMMIT"' in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "StrictHostKeyChecking=yes" in wrapper
    assert "does not build, migrate, restore, remove Docker networks" in wrapper


def test_healthy_pending_finalization_is_manual_and_read_only_for_runtime_state() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "production-healthy-pending-finalize.yml"
    ).read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "finalize-healthy-pending-release.sh").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "FINALIZE_HEALTHY_PENDING_RUNTIME" in workflow
    assert "group: greatsellai-hr-production" in workflow
    assert "environment:" in workflow and "name: production" in workflow
    assert "ref: main" in workflow
    assert 'scripts/finalize-healthy-pending-release.sh "$PENDING_TAG" "$PENDING_COMMIT"' in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert "StrictHostKeyChecking=yes" in wrapper
    assert "does not build, migrate, stop, start, recreate, restore, remove" in wrapper


def test_server_preflight_is_read_only_and_uses_candidate_compose() -> None:
    script = (ROOT / "scripts" / "preflight-production-release.sh").read_text(
        encoding="utf-8"
    )

    assert 'git show "$release_commit:compose.yml"' in script
    assert "config --quiet" in script
    assert "git archive" not in script
    assert " docker compose " in script
    assert not re.search(r"docker compose[^\\n]*\\b(?:up|stop|build|exec)\\b", script)
    assert "pg_dump" not in script
    assert "alembic" not in script


def test_automatic_release_waits_for_encoding_validation_in_its_ci_gate() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'python scripts/check_text_encoding.py --github-event "$GITHUB_EVENT_PATH"' in ci


def test_remote_preflight_template_uses_its_stdin_compose_and_removes_it(
    tmp_path: Path,
) -> None:
    """Exercise the rendered remote Bash template without an SSH target."""

    if os.name != "posix":
        pytest.skip("the remote preflight template is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the remote preflight contract")

    source = (ROOT / "scripts" / "preflight-production-release.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"remote_preflight_script=\"\$\(cat <<'EOF'\n(?P<template>.*?)\nEOF\n\)\"",
        source,
        flags=re.DOTALL,
    )
    assert match, "remote preflight Bash template is missing"

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env.production").write_text("unused-by-fake-docker\n", encoding="utf-8")
    history_dir = tmp_path / "history" / "nested"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    captured_compose = tmp_path / "captured-compose-path.txt"
    (fake_bin / "sudo").write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n[[ \"$1\" == \"-n\" ]] && shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    fake_docker = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -Eeuo pipefail",
            "[[ \"$1\" == \"compose\" ]]",
            "shift",
            "if [[ \"${1:-}\" == \"version\" ]]; then exit 0; fi",
            "compose_file=\"\"",
            "while (($#)); do",
            "  if [[ \"$1\" == \"-f\" ]]; then compose_file=\"$2\"; shift 2; continue; fi",
            "  if [[ \"$1\" == \"--project-directory\" || \"$1\" == \"--env-file\" ]]; then shift 2; continue; fi",
            "  shift",
            "done",
            "[[ -n \"$compose_file\" ]]",
            "grep -Fx 'services: {}' \"$compose_file\" >/dev/null",
            "printf '%s\\n' \"$compose_file\" > \"$FAKE_CAPTURED_COMPOSE\"",
            "",
        )
    )
    (fake_bin / "docker").write_text(fake_docker, encoding="utf-8")
    for command in (fake_bin / "sudo", fake_bin / "docker"):
        command.chmod(0o755)

    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_CAPTURED_COMPOSE": str(captured_compose),
    }
    completed = subprocess.run(
        [
            bash,
            "-c",
            match.group("template"),
            "--",
            str(project_dir),
            str(history_dir),
            "a" * 40,
        ],
        input="services: {}\n",
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not Path(captured_compose.read_text(encoding="utf-8").strip()).exists()
