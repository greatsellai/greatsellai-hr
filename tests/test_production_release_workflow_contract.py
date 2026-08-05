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
    promote = re.search(
        r"(?ms)^  promote:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert promote is not None
    assert "timeout-minutes: 45" in promote.group("body")
    assert "actions: read" in workflow
    assert "scripts/verify-staging-release.sh" in workflow
    assert "scripts/pull-tcr-release-images.sh" in workflow
    assert "scripts/verify-release-images.sh" in workflow
    assert "mapfile -t staging_tags" in workflow
    assert "Current main has multiple production tags; use Production deploy for an existing release." in workflow
    assert "Expected exactly one staging tag for current main" in workflow

    preflight = workflow.index("Preflight production configuration before tagging")
    pull = workflow.index("Pull exact completed staging images from TCR")
    image_identity = workflow.index("Verify production host holds CI-attested images")
    create_tag = workflow.index("Create immutable production tag for the staged candidate")
    deploy = workflow.index("Deploy tagged release from the exact staged images")
    assert preflight < pull < image_identity < create_tag < deploy
    assert 'scripts/preflight-production-release.sh "$RELEASE_SHA"' in workflow
    assert "--prebuilt-images" in workflow
    assert 'scripts/transfer-production-images.sh' not in workflow
    assert 'scripts/load-verified-release-images.sh' not in workflow
    assert 'printf \'%s\\n\' "$TCR_PASSWORD" | scripts/pull-tcr-release-images.sh "$RELEASE_SHA"' in workflow
    assert '--api-registry-image "$STAGING_API_REGISTRY_IMAGE"' in workflow
    assert '--caddy-registry-image "$STAGING_CADDY_REGISTRY_IMAGE"' in workflow
    assert '--password-stdin' in workflow
    assert 'STAGING_IMAGE_METADATA_SHA256' in workflow
    assert '--api-image-config-digest "$STAGING_API_IMAGE_CONFIG_DIGEST"' in workflow
    assert '--caddy-image-config-digest "$STAGING_CADDY_IMAGE_CONFIG_DIGEST"' in workflow
    assert '--expected-ci-run-id "$CI_RUN_ID"' in workflow
    assert '--expected-ci-run-attempt "$CI_RUN_ATTEMPT"' in workflow
    assert "ci_run_id: ${{ steps.verified.outputs.ci_run_id }}" in workflow
    assert "ci_run_attempt: ${{ steps.verified.outputs.ci_run_attempt }}" in workflow
    assert "image_metadata_sha256: ${{ steps.verified.outputs.image_metadata_sha256 }}" in workflow
    assert "api_registry_image: ${{ steps.verified.outputs.api_registry_image }}" in workflow
    assert "caddy_registry_image: ${{ steps.verified.outputs.caddy_registry_image }}" in workflow
    assert "api_image_config_digest: ${{ steps.verified.outputs.api_image_config_digest }}" in workflow
    assert "caddy_image_config_digest: ${{ steps.verified.outputs.caddy_image_config_digest }}" in workflow
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
        ".github/workflows/production-bootstrap-import.yml",
        ".github/workflows/production-bootstrap-restore.yml",
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


def test_main_ci_publishes_labeled_images_to_tcr_and_hands_off_small_metadata() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish = (ROOT / "scripts" / "publish-tcr-release-images.sh").read_text(
        encoding="utf-8"
    )
    metadata = (ROOT / "scripts" / "verify-tcr-release-metadata.sh").read_text(
        encoding="utf-8"
    )

    assert '--tag "greatsellai-hr-api:' in ci
    assert '--tag "greatsellai-hr-caddy:' in ci
    assert "org.opencontainers.image.revision=" in ci
    assert "org.opencontainers.image.workflow_run_id=" in ci
    assert "org.opencontainers.image.workflow_run_attempt=" in ci
    assert "org.opencontainers.image.source=https://github.com/" in ci
    assert '--image "greatsellai-hr-api:' in ci
    assert "Log in to Tencent Container Registry" in ci
    assert 'printf \'%s\\n\' "$TCR_PASSWORD" | docker login "$TCR_REGISTRY"' in ci
    assert "--password-stdin" in ci
    assert "Publish immutable CI images to TCR" in ci
    assert "scripts/publish-tcr-release-images.sh" in ci
    assert "release-image-metadata-" in ci
    assert "docker image save" not in ci
    assert "transfer-production-images.sh" not in ci
    assert "load-verified-release-images.sh" not in ci

    assert "ci-" in publish
    assert "docker push" in publish
    assert "docker pull" in publish
    assert "RepoDigests" in publish
    assert "api_registry_image=" in publish
    assert "caddy_registry_image=" in publish
    assert "sha256sum" in publish
    assert "repo@sha256" in publish
    assert "sha256sum --check" in metadata
    assert "api_registry_image" in metadata
    assert "caddy_registry_image" in metadata
    assert "TCR release metadata artifact is incomplete." in metadata


def test_staging_and_production_pull_the_same_digest_pinned_tcr_images() -> None:
    staging = (ROOT / ".github" / "workflows" / "staging-release.yml").read_text(
        encoding="utf-8"
    )
    production = (ROOT / ".github" / "workflows" / "production-release.yml").read_text(
        encoding="utf-8"
    )
    pull = (ROOT / "scripts" / "pull-tcr-release-images.sh").read_text(encoding="utf-8")
    staging_verify = (ROOT / "scripts" / "verify-staging-release.sh").read_text(
        encoding="utf-8"
    )
    production_verify = (ROOT / "scripts" / "verify-release-images.sh").read_text(
        encoding="utf-8"
    )

    for workflow in (staging, production):
        assert "scripts/pull-tcr-release-images.sh" in workflow
        assert "TCR_USERNAME:" in workflow
        assert "TCR_PASSWORD:" in workflow
        assert "--api-registry-image" in workflow
        assert "--caddy-registry-image" in workflow
        assert "--password-stdin" in workflow
        assert "transfer-production-images.sh" not in workflow
        assert "load-verified-release-images.sh" not in workflow
        assert "release-images-" not in workflow

    assert "release-image-metadata-$RELEASE_SHA-$ci_run_id-$ci_run_attempt" in staging
    assert "scripts/verify-tcr-release-metadata.sh" in staging
    assert "image_metadata_sha256" in staging_verify
    assert "api_registry_image" in staging_verify
    assert "caddy_registry_image" in staging_verify
    assert "RepoDigests" in staging_verify
    assert "timeout-minutes: 45" in production
    assert "STAGING_API_REGISTRY_IMAGE" in production
    assert "STAGING_CADDY_REGISTRY_IMAGE" in production
    assert "RepoDigests" in production_verify
    assert "Promotion image config identity does not match completed staging" in production_verify

    assert "password-stdin" in pull
    assert "read -r registry_password" in pull
    assert 'printf \'%s\\n\' "$registry_password" | ssh' in pull
    assert 'DOCKER_CONFIG="$docker_config" docker login "$registry"' in pull
    assert 'DOCKER_CONFIG="$docker_config" docker pull "$registry_image"' in pull
    assert 'mktemp -d "${TMPDIR:-/tmp}/greatsell-tcr.XXXXXXXX"' in pull
    assert 'trap cleanup_docker_credentials EXIT' in pull
    assert 'rm -rf -- "$docker_config"' in pull
    assert "RepoDigests" in pull
    assert "sudo -n docker tag" in pull
    assert "TCR_PASSWORD" not in pull


def test_tcr_metadata_verifier_rejects_tampering_and_returns_digest_pinned_references(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash metadata verifier is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the TCR metadata contract")

    release_commit = "a" * 40
    ci_run_id = "123456789"
    ci_run_attempt = "2"
    registry = "ccr.ccs.tencentyun.com"
    namespace = "greatsellaihr"
    api_manifest = f"sha256:{'1' * 64}"
    caddy_manifest = f"sha256:{'2' * 64}"
    api_config = f"sha256:{'3' * 64}"
    caddy_config = f"sha256:{'4' * 64}"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    metadata_name = (
        f"release-image-metadata-{release_commit}-{ci_run_id}-{ci_run_attempt}.env"
    )
    metadata = artifact_dir / metadata_name
    metadata_payload = "\n".join(
        (
            "format_version=1",
            "repository=greatsellai/greatsellai-hr",
            f"release_sha={release_commit}",
            f"ci_run_id={ci_run_id}",
            f"ci_run_attempt={ci_run_attempt}",
            f"api_registry_image={registry}/{namespace}/hr-api@{api_manifest}",
            f"caddy_registry_image={registry}/{namespace}/hr-caddy@{caddy_manifest}",
            f"api_image_config_digest={api_config}",
            f"caddy_image_config_digest={caddy_config}",
            "",
        )
    )
    metadata.write_text(metadata_payload, encoding="utf-8")
    metadata_sha256 = hashlib.sha256(metadata_payload.encode()).hexdigest()
    (artifact_dir / f"{metadata_name}.sha256").write_text(
        f"{metadata_sha256}  {metadata_name}\n", encoding="utf-8"
    )
    github_output = tmp_path / "github-output"
    github_output.touch()

    command = [
        bash,
        str(ROOT / "scripts" / "verify-tcr-release-metadata.sh"),
        release_commit,
        "--artifact-dir",
        str(artifact_dir),
        "--repository",
        "greatsellai/greatsellai-hr",
        "--ci-run-id",
        ci_run_id,
        "--ci-run-attempt",
        ci_run_attempt,
        "--registry",
        registry,
        "--namespace",
        namespace,
        "--github-output",
        str(github_output),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert github_output.read_text(encoding="utf-8") == (
        f"image_metadata_sha256={metadata_sha256}\n"
        f"api_registry_image={registry}/{namespace}/hr-api@{api_manifest}\n"
        f"caddy_registry_image={registry}/{namespace}/hr-caddy@{caddy_manifest}\n"
        f"api_image_config_digest={api_config}\n"
        f"caddy_image_config_digest={caddy_config}\n"
    )

    metadata.write_text(metadata_payload.replace("hr-api", "hr-api-tampered", 1), encoding="utf-8")
    tampered = subprocess.run(command, text=True, capture_output=True, check=False)
    assert tampered.returncode != 0
    assert "FAILED" in tampered.stdout or "Warning" in tampered.stderr


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
