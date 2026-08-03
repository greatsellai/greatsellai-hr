from __future__ import annotations

import hashlib
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
    assert "actions: read" in workflow
    assert "scripts/verify-staging-release.sh" in workflow
    assert "scripts/load-verified-release-images.sh" in workflow
    assert "scripts/verify-release-images.sh" in workflow
    assert "mapfile -t staging_tags" in workflow
    assert "Current main has multiple production tags; use Production deploy for an existing release." in workflow
    assert "Expected exactly one staging tag for current main" in workflow

    preflight = workflow.index("Preflight production configuration before tagging")
    download = workflow.index("Download completed staging CI images")
    verify_and_load = workflow.index("Verify and load completed staging CI images")
    transfer = workflow.index("Transfer exact completed staging images to production")
    image_identity = workflow.index("Verify production host holds the exact staged images")
    create_tag = workflow.index("Create immutable production tag for the staged candidate")
    deploy = workflow.index("Deploy tagged release from the exact staged images")
    assert preflight < download < verify_and_load < transfer < image_identity < create_tag < deploy
    assert 'scripts/preflight-production-release.sh "$RELEASE_SHA"' in workflow
    assert "--prebuilt-images" in workflow
    assert 'scripts/transfer-production-images.sh "$RELEASE_SHA"' in workflow
    assert 'name: release-images-${{ env.RELEASE_SHA }}-${{ env.CI_RUN_ID }}-${{ env.CI_RUN_ATTEMPT }}' in workflow
    assert 'run-id: ${{ env.CI_RUN_ID }}' in workflow
    assert '--expected-api-image-id "$STAGING_API_IMAGE_ID"' in workflow
    assert '--expected-caddy-image-id "$STAGING_CADDY_IMAGE_ID"' in workflow
    assert '--expected-ci-run-id "$CI_RUN_ID"' in workflow
    assert '--expected-ci-run-attempt "$CI_RUN_ATTEMPT"' in workflow
    assert "ci_run_id: ${{ steps.verified.outputs.ci_run_id }}" in workflow
    assert "ci_run_attempt: ${{ steps.verified.outputs.ci_run_attempt }}" in workflow
    assert "main advanced after staging verification" in workflow
    assert "scripts/ensure-staging-gateway.sh" not in workflow


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
    assert "scripts/ensure-staging-gateway.sh" not in workflow


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


def test_public_repository_routes_ci_and_release_jobs_to_hosted_runners() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    encoding = (ROOT / ".github" / "workflows" / "text-encoding.yml").read_text(
        encoding="utf-8"
    )

    runner_selector = (
        "github.event.repository.private && "
        "fromJSON('[\"self-hosted\", \"Linux\", \"X64\", \"greatsell-ci\"]') "
        "|| 'ubuntu-latest'"
    )
    assert ci.count(runner_selector) == 5
    assert runner_selector in encoding

    production_images = ci.split("  production-images:", maxsplit=1)[1]
    assert runner_selector in production_images
    assert "needs.main-release-provenance.result == 'success'" in production_images
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main'" in production_images
    assert "runs-on: [self-hosted, Linux, X64, greatsell-ci]" not in production_images


def test_public_repository_routes_all_release_orchestration_to_hosted_runners() -> None:
    runner_selector = (
        "github.event.repository.private && "
        "fromJSON('[\"self-hosted\", \"Linux\", \"X64\", \"greatsell-ci\"]') "
        "|| 'ubuntu-latest'"
    )
    workflow_paths = (
        ".github/workflows/staging-release.yml",
        ".github/workflows/production-release.yml",
        ".github/workflows/production-deploy.yml",
        ".github/workflows/production-rollback.yml",
        ".github/workflows/production-healthy-pending-finalize.yml",
        ".github/workflows/production-pending-finalize.yml",
        ".github/workflows/production-legacy-reconcile.yml",
    )

    for workflow_path in workflow_paths:
        workflow = (ROOT / workflow_path).read_text(encoding="utf-8")
        assert "runs-on: [self-hosted, Linux, X64, greatsell-ci]" not in workflow
        assert runner_selector in workflow


def test_cross_host_production_edge_never_claims_the_legacy_staging_gateway() -> None:
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    helper = (ROOT / "scripts" / "remote-release-helper.sh").read_text(
        encoding="utf-8"
    )

    assert "staging.hr.greatsellai.net" not in caddy
    assert not (ROOT / "scripts" / "ensure-staging-gateway.sh").exists()
    assert not (ROOT / ".github" / "workflows" / "staging-gateway-bootstrap.yml").exists()
    assert "require_target_source_without_legacy_staging_gateway" in helper
    assert "require_production_caddy_image_without_legacy_staging_gateway" in helper
    assert "Target Caddy image retains the legacy staging gateway route" in helper
    assert helper.index("require_target_source_without_legacy_staging_gateway") < helper.index(
        "create_backup_bundle"
    )

    for workflow_name in (
        "production-release.yml",
        "production-deploy.yml",
        "production-rollback.yml",
        "production-pending-finalize.yml",
        "production-healthy-pending-finalize.yml",
        "production-legacy-reconcile.yml",
    ):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )
        assert "ensure-staging-gateway" not in workflow
        assert "Preserve the staging gateway" not in workflow


def test_main_ci_archives_only_labeled_images_that_the_release_workflow_can_transfer() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    transfer = (ROOT / "scripts" / "transfer-production-images.sh").read_text(
        encoding="utf-8"
    )

    assert '--tag "greatsellai-hr-api:${{ github.sha }}"' in ci
    assert '--tag "greatsellai-hr-caddy:${{ github.sha }}"' in ci
    assert 'org.opencontainers.image.revision=${{ github.sha }}' in ci
    assert 'org.opencontainers.image.workflow_run_id=${{ github.run_id }}' in ci
    assert 'org.opencontainers.image.workflow_run_attempt=${{ github.run_attempt }}' in ci
    assert 'org.opencontainers.image.source=https://github.com/${{ github.repository }}' in ci
    assert '--image "greatsellai-hr-api:${{ github.sha }}"' in ci
    assert "Archive CI-verified release images" in ci
    assert "actions/upload-artifact@v4" in ci
    assert "name: release-images-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}" in ci
    assert 'archive_name="release-images-${GITHUB_SHA}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tar.gz"' in ci
    assert 'metadata_name="${archive_name%.tar.gz}.metadata"' in ci
    assert "ci_run_attempt=%s" in ci
    assert 'docker image save \\' in ci
    assert "sha256sum \"$archive_name\" > \"$archive_name.sha256\"" in ci
    assert "retention-days: 30" in ci
    assert "compression-level: 0" in ci
    assert 'docker image save "$api_image" "$caddy_image" | gzip -1' in transfer
    assert "StrictHostKeyChecking=yes" in transfer
    assert "org.opencontainers.image.revision" in transfer
    assert "org.opencontainers.image.workflow_run_id" in transfer
    assert "org.opencontainers.image.workflow_run_attempt" in transfer
    assert "--expected-ci-run-id" in transfer
    assert "--expected-ci-run-attempt" in transfer
    assert "docker image build" not in transfer
    assert "id: release_runtime_regression" in ci
    assert 'docker image rm -f "greatsellai-hr-api:${{ github.sha }}" || true' in ci
    assert 'docker image rm -f "greatsellai-hr-caddy:${{ github.sha }}" || true' in ci


def test_release_image_loader_rechecks_artifact_integrity_and_completed_staging_identity() -> None:
    loader = (ROOT / "scripts" / "load-verified-release-images.sh").read_text(
        encoding="utf-8"
    )
    staging_verify = (ROOT / "scripts" / "verify-staging-release.sh").read_text(
        encoding="utf-8"
    )
    image_verify = (ROOT / "scripts" / "verify-release-images.sh").read_text(
        encoding="utf-8"
    )

    assert "sha256sum --check" in loader
    assert "checksum does not name the expected archive exactly once" in loader
    assert "docker image load --input" in loader
    assert "org.opencontainers.image.revision" in loader
    assert "org.opencontainers.image.workflow_run_id" in loader
    assert "org.opencontainers.image.workflow_run_attempt" in loader
    assert "--expected-api-image-id" in loader
    assert "--expected-caddy-image-id" in loader
    assert "Expected API and Caddy image IDs must be provided together." in loader
    assert "Loaded API image identity does not match completed staging." in loader
    assert "ci_run_id=%s" in staging_verify
    assert "ci_run_attempt=%s" in staging_verify
    assert "Staging image CI workflow run identities are invalid." in staging_verify
    assert "Staging image CI workflow run attempts are invalid." in staging_verify
    assert "docker version --format '{{.Server.Os}}/{{.Server.Arch}}'" in image_verify
    assert "Promotion target platform must be linux/amd64" in image_verify


def test_image_transfer_script_loads_and_rechecks_the_ci_images(tmp_path: Path) -> None:
    """Exercise the encrypted-stream command shape without a real Docker host."""

    if os.name != "posix":
        pytest.skip("the image transfer shell harness is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the image transfer contract")

    release_commit = "a" * 40
    ci_run_id = "123456789"
    ci_run_attempt = "2"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  if [[ "$*" == *org.opencontainers.image.workflow_run_attempt* ]]; then
    printf '%s\\n' "$EXPECTED_CI_RUN_ATTEMPT"
  elif [[ "$*" == *org.opencontainers.image.workflow_run_id* ]]; then
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
            "--expected-ci-run-attempt",
            ci_run_attempt,
        ],
        text=True,
        capture_output=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "EXPECTED_RELEASE_COMMIT": release_commit,
            "EXPECTED_CI_RUN_ID": ci_run_id,
            "EXPECTED_CI_RUN_ATTEMPT": ci_run_attempt,
        },
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Production images transferred and verified" in completed.stdout


def test_release_image_loader_checks_metadata_labels_and_staging_image_ids(
    tmp_path: Path,
) -> None:
    """Exercise the shared artifact verifier without a Docker daemon."""

    if os.name != "posix":
        pytest.skip("the release-image shell harness is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the release-image contract")

    release_commit = "b" * 40
    ci_run_id = "123456790"
    ci_run_attempt = "3"
    api_image_id = f"sha256:{'1' * 64}"
    caddy_image_id = f"sha256:{'2' * 64}"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    archive_name = f"release-images-{release_commit}-{ci_run_id}-{ci_run_attempt}.tar.gz"
    archive = artifact_dir / archive_name
    archive.write_bytes(b"fake docker release image archive\n")
    (artifact_dir / f"{archive_name}.sha256").write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive_name}\n",
        encoding="utf-8",
    )
    (artifact_dir / f"{archive_name.removesuffix('.tar.gz')}.metadata").write_text(
        "\n".join(
            (
                "repository=greatsellai/greatsellai-hr",
                f"release_sha={release_commit}",
                f"ci_run_id={ci_run_id}",
                f"ci_run_attempt={ci_run_attempt}",
                f"archive={archive_name}",
                "",
            )
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$1" == "image" && "$2" == "load" ]]; then
  cat "$4" >/dev/null
  exit 0
fi
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  format="$4"
  image="$5"
  if [[ "$format" == *".Id"* ]]; then
    if [[ "$image" == *"api:"* ]]; then
      printf '%s\\n' "$EXPECTED_API_IMAGE_ID"
    else
      printf '%s\\n' "$EXPECTED_CADDY_IMAGE_ID"
    fi
  elif [[ "$format" == *"workflow_run_attempt"* ]]; then
    printf '%s\\n' "$EXPECTED_CI_RUN_ATTEMPT"
  elif [[ "$format" == *"workflow_run_id"* ]]; then
    printf '%s\\n' "$EXPECTED_CI_RUN_ID"
  elif [[ "$format" == *"revision"* ]]; then
    printf '%s\\n' "$EXPECTED_RELEASE_COMMIT"
  else
    exit 1
  fi
  exit 0
fi
printf 'unexpected docker call: %s\\n' "$*" >&2
exit 1
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").chmod(0o755)
    github_output = tmp_path / "github-output"
    github_output.write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            bash,
            str(ROOT / "scripts" / "load-verified-release-images.sh"),
            release_commit,
            "--artifact-dir",
            str(artifact_dir),
            "--repository",
            "greatsellai/greatsellai-hr",
            "--ci-run-id",
            ci_run_id,
            "--ci-run-attempt",
            ci_run_attempt,
            "--expected-api-image-id",
            api_image_id,
            "--expected-caddy-image-id",
            caddy_image_id,
            "--github-output",
            str(github_output),
        ],
        text=True,
        capture_output=True,
        env=os.environ
        | {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "EXPECTED_RELEASE_COMMIT": release_commit,
            "EXPECTED_CI_RUN_ID": ci_run_id,
            "EXPECTED_CI_RUN_ATTEMPT": ci_run_attempt,
            "EXPECTED_API_IMAGE_ID": api_image_id,
            "EXPECTED_CADDY_IMAGE_ID": caddy_image_id,
        },
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "CI-verified release images loaded" in completed.stdout
    assert github_output.read_text(encoding="utf-8") == (
        f"api_image_id={api_image_id}\n"
        f"caddy_image_id={caddy_image_id}\n"
    )

    incomplete_identity = subprocess.run(
        [
            bash,
            str(ROOT / "scripts" / "load-verified-release-images.sh"),
            release_commit,
            "--artifact-dir",
            str(artifact_dir),
            "--repository",
            "greatsellai/greatsellai-hr",
            "--ci-run-id",
            ci_run_id,
            "--ci-run-attempt",
            ci_run_attempt,
            "--expected-caddy-image-id",
            caddy_image_id,
        ],
        text=True,
        capture_output=True,
        env=os.environ | {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=False,
    )
    assert incomplete_identity.returncode != 0
    assert "Expected API and Caddy image IDs must be provided together." in incomplete_identity.stderr


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
