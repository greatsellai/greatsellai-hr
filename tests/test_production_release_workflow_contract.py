from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
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
    assert "timeout-minutes: 75" in promote.group("body")
    assert "actions: read" in workflow
    assert "scripts/verify-staging-release.sh" in workflow
    assert "scripts/verify-preloaded-production-images.sh" not in workflow
    assert "scripts/pull-tcr-release-images.sh" not in workflow
    assert "scripts/verify-release-images.sh" not in workflow
    assert "mapfile -t staging_tags" in workflow
    assert "Current main has multiple production tags; use Production deploy for an existing release." in workflow
    assert "Expected exactly one staging tag for current main" in workflow

    preflight = workflow.index("Preflight production configuration before tagging")
    create_tag = workflow.index("Create immutable production tag for the staged candidate")
    deploy = workflow.index("Build and deploy tagged release on the production host")
    assert preflight < create_tag < deploy
    assert 'scripts/preflight-production-release.sh "$RELEASE_SHA"' in workflow
    # Production images are built on the production host from the tagged source
    # (deploy-production.sh image_mode=build, Tencent mirror defaults); nothing
    # is preloaded and the deploy step must not use the retired prebuilt path.
    assert "--prebuilt-images" not in workflow
    assert 'scripts/transfer-production-images.sh' not in workflow
    assert 'scripts/load-verified-release-images.sh' not in workflow
    assert "STAGING_API_IMAGE_ID" not in workflow
    assert "STAGING_CADDY_IMAGE_ID" not in workflow
    assert "TCR_PASSWORD" not in workflow
    assert "STAGING_API_REGISTRY_IMAGE" not in workflow
    assert "image_metadata_sha256" not in workflow
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
    # Retried deployments also build on the production host; the retired
    # prebuilt-image path must not be used.
    assert "--prebuilt-images" not in workflow
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


def test_main_release_uses_verified_pull_request_provenance_for_staging() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    text_encoding = (ROOT / ".github" / "workflows" / "text-encoding.yml").read_text(
        encoding="utf-8"
    )

    assert ci.count("if: github.event_name != 'push'") == 3
    assert "main-release-provenance:" in ci
    assert "name: Main release provenance" in ci
    assert "python scripts/verify_main_release_provenance.py" in ci
    assert "actions: read" in ci
    assert "checks: read" in ci
    assert "pull-requests: read" in ci
    # Main CI no longer builds or publishes images: staging builds its own and
    # production is not released from this repository right now.
    assert "needs: [main-release-provenance]" not in ci
    assert "needs.main-release-provenance.result" not in ci
    assert ci.count("python scripts/run_release_regression.py --all") == 0
    assert "--documents" not in ci
    assert 'python scripts/check_text_encoding.py --github-event "$GITHUB_EVENT_PATH"' in text_encoding
    assert "  push:" not in text_encoding


def test_public_repository_routes_ci_jobs_to_hosted_runners() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    encoding = (ROOT / ".github" / "workflows" / "text-encoding.yml").read_text(
        encoding="utf-8"
    )

    runner_selector = (
        "github.event.repository.private && "
        "fromJSON('[\"self-hosted\", \"Linux\", \"X64\", \"greatsell-ci\"]') "
        "|| 'ubuntu-latest'"
    )
    # Provenance plus the three PR test jobs all route through the selector.
    assert ci.count(runner_selector) == 4
    assert runner_selector in encoding
    # The release-image build was removed from main CI; no hosted build job
    # should remain behind.
    assert "production-images" not in ci


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


def test_main_ci_no_longer_builds_or_publishes_tcr_images() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish = (ROOT / "scripts" / "publish-tcr-release-images.sh").read_text(
        encoding="utf-8"
    )
    metadata = (ROOT / "scripts" / "verify-tcr-release-metadata.sh").read_text(
        encoding="utf-8"
    )

    # Staging builds and streams its own exact images, and production is not
    # released from this repository right now, so main CI no longer builds or
    # pushes TCR images. The publish/verify scripts stay as the future
    # production image path.
    assert "--tag \"greatsellai-hr-api:" not in ci
    assert "--tag \"greatsellai-hr-caddy:" not in ci
    assert "org.opencontainers.image.revision=" not in ci
    assert "Publish immutable CI images to TCR" not in ci
    assert "scripts/write-docker-registry-auth.sh" not in ci
    assert "scripts/publish-tcr-release-images.sh" not in ci
    assert "release-image-metadata-" not in ci
    assert "TCR_REGISTRY" not in ci
    assert "docker login" not in ci
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


def test_production_builds_images_on_host_while_staging_streams_direct() -> None:
    staging = (ROOT / ".github" / "workflows" / "staging-release.yml").read_text(
        encoding="utf-8"
    )
    production = (ROOT / ".github" / "workflows" / "production-release.yml").read_text(
        encoding="utf-8"
    )
    staging_verify = (ROOT / "scripts" / "verify-staging-release.sh").read_text(
        encoding="utf-8"
    )

    # Production promotion no longer pulls TCR images or consumes preloaded
    # images. It builds the API/Caddy images directly on the production host
    # from the tagged source (deploy-production.sh image_mode=build, Tencent
    # mirror defaults), then tags and deploys. No image is transferred or
    # preloaded across the border.
    assert "scripts/pull-tcr-release-images.sh" not in production
    assert "scripts/verify-release-images.sh" not in production
    assert "TCR_USERNAME" not in production
    assert "TCR_PASSWORD" not in production
    assert "--api-registry-image" not in production
    assert "--caddy-registry-image" not in production
    assert "--password-stdin" not in production
    assert "scripts/verify-preloaded-production-images.sh" not in production
    assert "STAGING_API_IMAGE_ID" not in production
    assert "STAGING_CADDY_IMAGE_ID" not in production
    assert "--prebuilt-images" not in production
    assert "transfer-production-images.sh" not in production
    assert "load-verified-release-images.sh" not in production
    assert "release-images-" not in production
    assert "timeout-minutes: 75" in production

    # Staging builds the exact commit images on the US release runner and
    # streams them straight to the US staging host; it must never touch TCR
    # credentials, the CI metadata artifact handoff, or the production host.
    # Production image building stays a manual production-Environment step.
    assert "scripts/pull-tcr-release-images.sh" not in staging
    assert "scripts/verify-tcr-release-metadata.sh" not in staging
    assert "TCR_USERNAME" not in staging
    assert "TCR_PASSWORD" not in staging
    assert "--password-stdin" not in staging
    assert "actions/download-artifact@v4" not in staging
    assert "release-image-metadata-" not in staging
    assert "transfer-production-images.sh" not in staging
    assert "load-verified-release-images.sh" not in staging
    assert "--delivery direct" in staging
    assert "sudo -n docker load" in staging
    assert "Pre-load verified images to production host" not in staging
    assert "scripts/stream-images-to-production.sh" not in staging
    assert "PROD_RELAY_HOST" not in staging
    assert "PROD_RELAY_SSH_PRIVATE_KEY" not in staging
    assert "PROD_RELAY_SSH_KNOWN_HOSTS" not in staging
    assert "bash -s $RELEASE_SHA" not in staging

    # verify-staging-release.sh branches on image_delivery: direct records are
    # attested by image content ID + revision; legacy TCR records keep the full
    # registry/config/CI checks.
    assert "image_delivery" in staging_verify
    assert "image_metadata_sha256" in staging_verify
    assert "api_registry_image" in staging_verify
    assert "caddy_registry_image" in staging_verify
    assert "RepoDigests" in staging_verify


def test_tcr_docker_auth_writer_creates_private_standard_credentials(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("the Bash Docker auth writer is exercised in Linux CI")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for the Docker auth writer contract")

    docker_config = tmp_path / "docker-config"
    docker_config.mkdir(mode=0o700)
    writer = ROOT / "scripts" / "write-docker-registry-auth.sh"
    command = (
        bash,
        str(writer),
        "--registry",
        "ccr.ccs.tencentyun.com",
        "--username",
        "100051348794",
        "--docker-config",
        str(docker_config),
        "--password-stdin",
    )

    completed = subprocess.run(
        command,
        input="example-tcr-password\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    expected_auth = base64.b64encode(
        b"100051348794:example-tcr-password"
    ).decode("ascii")
    config = docker_config / "config.json"
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "auths": {"ccr.ccs.tencentyun.com": {"auth": expected_auth}}
    }
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    refused = subprocess.run(
        command,
        input="example-tcr-password\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "must not already contain config.json" in refused.stderr
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "auths": {"ccr.ccs.tencentyun.com": {"auth": expected_auth}}
    }


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
