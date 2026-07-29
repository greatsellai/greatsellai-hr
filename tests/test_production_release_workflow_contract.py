from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_production_is_a_manual_promotion_of_a_completed_staging_candidate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "production-release.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_run:" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "Confirmation must be PROMOTE." in workflow
    assert "verify-staging:" in workflow
    assert "needs: verify-staging" in workflow
    assert "environment:\n      name: staging" in workflow
    assert "environment:\n      name: production" in workflow
    assert "scripts/verify-staging-release.sh" in workflow
    assert "scripts/verify-release-images.sh" in workflow
    assert "mapfile -t staging_tags" in workflow
    assert "Current main has multiple production tags; use Production deploy for an existing release." in workflow
    assert "Expected exactly one staging tag for current main" in workflow

    image_identity = workflow.index("Verify production host holds the exact staged images")
    preflight = workflow.index("Preflight production configuration before tagging")
    create_tag = workflow.index("Create immutable production tag for the staged candidate")
    deploy = workflow.index("Deploy tagged release from the exact staged images")
    assert image_identity < preflight < create_tag < deploy
    assert 'scripts/preflight-production-release.sh "$RELEASE_SHA"' in workflow
    assert "--prebuilt-images" in workflow
    assert 'scripts/transfer-production-images.sh "$RELEASE_SHA"' not in workflow
    assert "main advanced after staging verification" in workflow
    assert "scripts/ensure-staging-gateway.sh" in workflow


def test_retry_deploy_is_manual_and_uses_current_reviewed_tooling() -> None:
    workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(
        encoding="utf-8"
    )

    assert "  push:" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "ref: main" in workflow
    assert 'git fetch origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"' in workflow
    assert "--prebuilt-images" in workflow
    assert "scripts/transfer-production-images.sh" not in workflow
    assert "scripts/ensure-staging-gateway.sh" in workflow


def test_legacy_pending_reconciliation_is_manual_and_uses_the_production_lock() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "production-legacy-reconcile.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "RECONCILE_LEGACY_PENDING" in workflow
    assert "group: greatsellai-hr-release-lane" in workflow
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
    assert "group: greatsellai-hr-release-lane" in workflow
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
    assert "group: greatsellai-hr-release-lane" in workflow
    assert "environment:" in workflow and "name: production" in workflow
    assert "ref: main" in workflow
    assert 'bash scripts/finalize-healthy-pending-release.sh "$PENDING_TAG" "$PENDING_COMMIT"' in workflow
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


def test_main_release_uses_verified_pull_request_provenance_instead_of_repeating_full_ci() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    text_encoding = (ROOT / ".github" / "workflows" / "text-encoding.yml").read_text(
        encoding="utf-8"
    )

    assert ci.count("if: github.event_name != 'push'") == 3
    assert "main-release-provenance:" in ci
    assert "name: Main release provenance" in ci
    assert "python scripts/verify_main_release_provenance.py" in ci
    assert "needs: [main-release-provenance]" in ci
    assert "needs.main-release-provenance.result == 'success'" in ci
    assert "actions: read" in ci
    assert "checks: read" in ci
    assert "pull-requests: read" in ci
    assert ci.count("python scripts/run_release_regression.py --all") == 1
    assert "--documents" not in ci
    assert 'python scripts/check_text_encoding.py --github-event "$GITHUB_EVENT_PATH"' in text_encoding
    assert "  push:" not in text_encoding


def test_public_repository_routes_default_pr_checks_to_hosted_runners() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    encoding = (ROOT / ".github" / "workflows" / "text-encoding.yml").read_text(
        encoding="utf-8"
    )

    runner_selector = (
        "github.event.repository.private && "
        "fromJSON('[\"self-hosted\", \"Linux\", \"X64\", \"greatsell-ci\"]') "
        "|| 'ubuntu-latest'"
    )
    assert ci.count(runner_selector) == 3
    assert runner_selector in encoding

    production_images = ci.split("  production-images:", maxsplit=1)[1]
    assert "github.event.repository.private ||" in production_images
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in production_images
    assert "runs-on: [self-hosted, Linux, X64, greatsell-ci]" in production_images


def test_main_ci_retains_only_labeled_images_that_the_release_workflow_can_transfer() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    transfer = (ROOT / "scripts" / "transfer-production-images.sh").read_text(
        encoding="utf-8"
    )

    assert '--tag "greatsellai-hr-api:${{ github.sha }}"' in ci
    assert '--tag "greatsellai-hr-caddy:${{ github.sha }}"' in ci
    assert 'org.opencontainers.image.revision=${{ github.sha }}' in ci
    assert 'org.opencontainers.image.workflow_run_id=${{ github.run_id }}' in ci
    assert 'org.opencontainers.image.source=https://github.com/${{ github.repository }}' in ci
    assert '--image "greatsellai-hr-api:${{ github.sha }}"' in ci
    assert '"$GITHUB_EVENT_NAME" == "push" && "$GITHUB_REF" == "refs/heads/main"' in ci
    assert 'docker image save "$api_image" "$caddy_image" | gzip -1' in transfer
    assert "StrictHostKeyChecking=yes" in transfer
    assert "org.opencontainers.image.revision" in transfer
    assert "org.opencontainers.image.workflow_run_id" in transfer
    assert "--expected-ci-run-id" in transfer
    assert "docker image build" not in transfer
    assert "id: release_runtime_regression" in ci
    assert 'RUNTIME_REGRESSION_OUTCOME: ${{ steps.release_runtime_regression.outcome }}' in ci


def test_image_transfer_script_loads_and_rechecks_the_ci_images(tmp_path: Path) -> None:
    """Exercise the encrypted-stream command shape without a real Docker host."""

    if os.name != "posix":
        pytest.skip("the image transfer shell harness is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the image transfer contract")

    release_commit = "a" * 40
    ci_run_id = "123456789"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  if [[ "$*" == *org.opencontainers.image.workflow_run_id* ]]; then
    printf '%s\\n' "$EXPECTED_CI_RUN_ID"
  else
    printf '%s\\n' "$EXPECTED_RELEASE_COMMIT"
  fi
elif [[ "$1" == "image" && "$2" == "save" ]]; then
  printf 'image archive stream\\n'
elif [[ "$1" == "image" && "$2" == "load" ]]; then
  cat >/dev/null
  printf 'loaded CI image stream\\n'
else
  printf 'unexpected docker call: %s\\n' "$*" >&2
  exit 1
fi
""",
        encoding="utf-8",
    )
    (fake_bin / "sudo").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$1" == "-n" ]] && shift
exec "$@"
""",
        encoding="utf-8",
    )
    (fake_bin / "ssh").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
command="${!#}"
exec bash -c "$command"
""",
        encoding="utf-8",
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    completed = subprocess.run(
        [
            bash,
            str(ROOT / "scripts" / "transfer-production-images.sh"),
            release_commit,
            "--host",
            "ubuntu@example.test",
            "--expected-ci-run-id",
            ci_run_id,
        ],
        text=True,
        capture_output=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "EXPECTED_RELEASE_COMMIT": release_commit,
            "EXPECTED_CI_RUN_ID": ci_run_id,
        },
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Production images transferred and verified" in completed.stdout


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
