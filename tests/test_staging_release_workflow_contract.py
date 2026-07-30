from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _compose_environment_expression(compose: str, key: str) -> str:
    match = re.search(rf"(?m)^  {re.escape(key)}: (?P<value>.+)$", compose)
    assert match, f"missing {key} from Compose application environment"
    return match.group("value")


def test_staging_workflow_only_accepts_a_successful_main_ci_or_confirmed_main_dispatch() -> None:
    workflow = (ROOT / ".github" / "workflows" / "staging-release.yml").read_text(
        encoding="utf-8"
    )

    assert 'workflows: ["Continuous integration"]' in workflow
    assert "workflow_run:" in workflow
    assert "branches: [main]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "workflow_dispatch:" in workflow
    assert "Confirmation must be STAGE." in workflow
    assert "Manual staging releases must be run from main." in workflow
    assert "actions: read" in workflow
    assert "Verify manual candidate passed main CI" in workflow
    assert "Manual staging requires a successful Continuous integration push run for this exact main commit." in workflow
    assert "actions/runs?branch=main&event=push&status=completed" in workflow
    assert "command -v python3 >/dev/null" in workflow
    assert "| python3 -c '" in workflow
    assert "ci_run_id=" in workflow
    assert 'output.write(f"ci_run_id={ci_run_id}\\n")' in workflow
    assert 'output.write(f"ci_run_id={ci_run_id}\\\\n")' not in workflow
    assert "environment:\n      name: staging" in workflow
    assert "group: greatsellai-hr-release-lane" in workflow
    assert "cancel-in-progress: false" in workflow


def test_staging_preflights_tags_transfers_deploys_and_smokes_in_order() -> None:
    workflow = (ROOT / ".github" / "workflows" / "staging-release.yml").read_text(
        encoding="utf-8"
    )

    preflight = workflow.index("Preflight staging configuration before tagging")
    tag = workflow.index("Create immutable staging tag")
    transfer = workflow.index("Transfer CI-verified images to staging")
    deploy = workflow.index("Deploy immutable candidate and run public smoke checks")
    cleanup = workflow.index("Remove CI images after successful staging deployment")
    assert preflight < tag < transfer < deploy < cleanup
    assert 'scripts/preflight-staging-release.sh "$RELEASE_SHA"' in workflow
    assert 'scripts/create-staging-tag.sh "$tag"' in workflow
    assert "Current main has multiple staging tags; refusing ambiguous promotion lineage." in workflow
    assert 'scripts/transfer-production-images.sh "$RELEASE_SHA"' in workflow
    assert '--expected-ci-run-id "$CI_RUN_ID"' in workflow
    assert 'id: transfer_ci_images' in workflow
    assert 'id: deploy_staging' in workflow
    assert "steps.deploy_staging.outcome == 'success'" in workflow
    assert "steps.target.outputs.stage == 'false'" in workflow
    assert "steps.ready.outputs.stage == 'false'" in workflow
    assert "scripts/ensure-staging-gateway.sh" not in workflow
    assert 'scripts/deploy-staging.sh "$STAGING_TAG"' in workflow
    assert 'docker image rm -f "greatsellai-hr-api:$RELEASE_SHA" || true' in workflow
    assert 'docker image rm -f "greatsellai-hr-caddy:$RELEASE_SHA" || true' in workflow


def test_staging_deployment_is_isolated_and_never_uses_production_env_or_builds() -> None:
    compose = (ROOT / "deploy" / "compose.staging.yml").read_text(encoding="utf-8")
    staging_env = (ROOT / ".env.staging.example").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "deploy-staging.sh").read_text(encoding="utf-8")
    preflight = (ROOT / "scripts" / "preflight-staging-release.sh").read_text(
        encoding="utf-8"
    )

    assert "name: resume-screening-v3-staging" in compose
    assert "RESUME_V3_ENVIRONMENT: production" in compose
    assert "172.31.0.0/24" in compose
    assert "172.31.1.0/24" in compose
    assert '"172.17.0.1:18080:80"' in compose
    assert "resume-screening-v3-staging_postgres_data" in compose
    assert "resume-screening-v3-staging_uploads_data" in compose
    assert "resume-screening-v3_uploads_data" not in re.sub(
        r"#.*", "", compose
    )
    assert not re.search(r"(?m)^\s*build:\s*", compose)
    assert 'entrypoint: ["caddy"]' in compose
    assert 'command: ["run", "--config", "/etc/caddy/Caddyfile.staging", "--adapter", "caddyfile"]' in compose
    assert "RESUME_V3_RELEASE_IMAGE_TAG=replace-with-full-40-character-git-commit-sha" in staging_env
    assert "DEEPSEEK_API_KEY=copy-the-production-value-here" in staging_env
    assert "TENCENT_SECRET_ID=copy-the-production-value-here" in staging_env
    assert "RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON=copy-the-production-value-here" in staging_env
    assert "RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED=copy-the-production-value-here" in staging_env
    assert "RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS=copy-the-production-value-here" in staging_env
    assert "RESUME_V3_STAGING_POSTGRES_PASSWORD=" in staging_env
    assert "RESUME_V3_TRUSTED_PROXY_CIDRS=172.31.0.2/32" in staging_env
    assert "RESUME_V3_STAGING_PUBLIC_APP_URL=https://staging.hr.greatsellai.net" in staging_env
    assert "RESUME_V3_DATABASE_URL: postgresql+psycopg://resume_v3_staging:" in compose
    assert "RESUME_V3_ADMIN_TOKEN: ${RESUME_V3_ADMIN_TOKEN:" in compose
    assert "RESUME_V3_SESSION_SECRET: ${RESUME_V3_SESSION_SECRET:" in compose
    assert "RESUME_V3_STAGING_ADMIN_TOKEN" not in compose
    assert "RESUME_V3_STAGING_SESSION_SECRET" not in compose
    assert "DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}" in compose
    assert "TENCENT_SECRET_ID: ${TENCENT_SECRET_ID:-}" in compose
    assert "RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER: ${RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER:-disabled}" in compose
    assert "RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS: ${RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS:-2}" in compose
    assert "RESUME_V3_PUBLIC_APP_URL: ${RESUME_V3_STAGING_PUBLIC_APP_URL:-https://staging.hr.greatsellai.net}" in compose

    for script in (deploy, preflight):
        assert ".env.staging" in script
        assert "test ! -e \"$project_dir/.env.production\"" in script
        assert "'RESUME_V3_ENVIRONMENT: production'" in script
        assert "StrictHostKeyChecking=yes" in script
        assert "compose_content()" in script
        assert "sed -E '/^[[:space:]]*#/d; s/[[:space:]]+#.*$//'" in script
        assert "! compose_contains \"$candidate_compose\" '.env.production'" in script or "! compose_contains \"$temporary_compose\" '.env.production'" in script
        assert "realpath -e" in script
        assert "realpath -m" in script
        assert "172.17.0.1:18080:80" in script
        assert "published: \"18080\"" in script
        assert (
            "resume-screening-v3_(postgres_data|uploads_data|caddy_data|caddy_config|proxy|backend)"
            in script
        )
    assert "up -d --no-build --remove-orphans" in deploy
    assert "docker compose " in preflight
    assert not re.search(r"docker compose[^\n]*\b(?:up|stop|build|exec)\b", preflight)


def test_staging_matches_production_for_shared_runtime_integrations() -> None:
    production = (ROOT / "compose.yml").read_text(encoding="utf-8")
    staging = (ROOT / "deploy" / "compose.staging.yml").read_text(encoding="utf-8")
    staging_env = (ROOT / ".env.staging.example").read_text(encoding="utf-8")

    shared_runtime_keys = (
        "DEEPSEEK_API_KEY",
        "RESUME_V3_AUTO_CREATE_SCHEMA",
        "RESUME_V3_SEED_REGISTRY_ON_STARTUP",
        "RESUME_V3_DATA_DIR",
        "RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED",
        "RESUME_V3_SESSION_COOKIE_SECURE",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON",
        "RESUME_V3_LEGACY_OPENAI_COMPATIBLE_ENDPOINT",
        "TENCENT_SECRET_ID",
        "TENCENT_SECRET_KEY",
        "TENCENT_OCR_REGION",
        "TENCENT_OCR_TIMEOUT_SECONDS",
        "OCR_SPARSE_TEXT_CHARS_PER_PAGE",
        "RESUME_V3_AI_EXTRACTION_JOB_MAX_ATTEMPTS",
        "RESUME_V3_AI_EXTRACTION_JOB_LEASE_SECONDS",
        "RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS",
        "RESUME_V3_MAILBOX_SYNC_INTERVAL_SECONDS",
        "RESUME_V3_MAILBOX_RETENTION_CLEANUP_INTERVAL_SECONDS",
        "RESUME_V3_MAILBOX_SYNC_ATTACHMENT_LIMIT",
        "RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS",
        "RESUME_V3_MAILBOX_IMAP_CONNECT_TIMEOUT_SECONDS",
        "RESUME_V3_MAILBOX_IMAP_MAX_RESOLVED_ADDRESSES",
        "RESUME_V3_MAILBOX_OAUTH_STATE_TTL_SECONDS",
        "RESUME_V3_MAILBOX_OAUTH_HTTP_TIMEOUT_SECONDS",
        "RESUME_V3_MAILBOX_MAX_RAW_MESSAGE_BYTES",
        "RESUME_V3_MAILBOX_MAX_HEADER_BYTES",
        "RESUME_V3_MAILBOX_MAX_MIME_PARTS",
        "RESUME_V3_MAILBOX_MAX_MIME_DEPTH",
        "RESUME_V3_MAILBOX_MAX_ATTACHMENTS_PER_MESSAGE",
        "RESUME_V3_MAILBOX_MAX_SEARCH_RESPONSE_BYTES",
        "RESUME_V3_MAILBOX_MAX_BODY_CACHE_BYTES",
        "RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_ALERT_THRESHOLD",
        "RESUME_V3_MAILBOX_CONSECUTIVE_FAILURE_WINDOW_SECONDS",
        "RESUME_V3_EMAIL_CREDENTIALS_KEY",
        "RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_ID",
        "RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_SECRET",
        "RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_ID",
        "RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_SECRET",
        "RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER",
        "RESUME_V3_TRANSACTIONAL_EMAIL_FROM",
        "TENCENT_SES_REGION",
        "TENCENT_SES_VERIFICATION_TEMPLATE_ID",
        "TENCENT_SES_PASSWORD_RESET_TEMPLATE_ID",
        "RESUME_V3_FEISHU_SMTP_HOST",
        "RESUME_V3_FEISHU_SMTP_PORT",
        "RESUME_V3_FEISHU_SMTP_TLS_MODE",
        "RESUME_V3_FEISHU_SMTP_USERNAME",
        "RESUME_V3_FEISHU_SMTP_PASSWORD",
        "RESUME_V3_FEISHU_SMTP_TIMEOUT_SECONDS",
    )

    for key in shared_runtime_keys:
        assert _compose_environment_expression(staging, key) == _compose_environment_expression(
            production, key
        )

    template_values = {
        key: value
        for line in staging_env.splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }
    copied_runtime_keys = {
        key for key in shared_runtime_keys if key in template_values
    } | {"RESUME_V3_ADMIN_TOKEN", "RESUME_V3_SESSION_SECRET"}
    assert copied_runtime_keys
    for key in copied_runtime_keys:
        assert template_values[key] == "copy-the-production-value-here"

    # These values must stay distinct even when every external integration is
    # intentionally production-equivalent: otherwise staging would either
    # attach to production data or send users back to the production host.
    assert _compose_environment_expression(staging, "RESUME_V3_ENVIRONMENT") == "production"
    assert "resume_v3_staging" in _compose_environment_expression(
        staging, "RESUME_V3_DATABASE_URL"
    )
    assert _compose_environment_expression(staging, "RESUME_V3_TRUSTED_PROXY_CIDRS") == "172.31.0.2/32"
    assert "staging.hr.greatsellai.net" in _compose_environment_expression(
        staging, "RESUME_V3_PUBLIC_APP_URL"
    )


def test_stage_attestation_requires_a_public_smoke_pass_and_exact_image_identities() -> None:
    deploy = (ROOT / "scripts" / "deploy-staging.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify-staging-release.sh").read_text(
        encoding="utf-8"
    )
    image_verify = (ROOT / "scripts" / "verify-release-images.sh").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / "scripts" / "smoke-test-staging.sh").read_text(encoding="utf-8")

    assert "archive_sha256=$archive_sha256" in deploy
    assert "public_smoke_check=pending" in deploy
    assert '"$repo_root/scripts/smoke-test-staging.sh" "$public_url"' in deploy
    assert "public_smoke_check=pass" in deploy
    assert '$(record_value "$record" state)" == "complete"' in verify
    assert '$(record_value "$record" public_smoke_check)" == "pass"' in verify
    assert "Staging API container differs from attested image." in verify
    assert "Staging Caddy container differs from attested image." in verify
    assert "Promotion image identity does not match completed staging" in image_verify
    assert "Promotion image revision does not match completed staging" in image_verify
    assert "https://staging.hr.greatsellai.net" in smoke
    assert '"$base_url/login"' in smoke
    assert '"$base_url/v1/auth/session"' in smoke
    assert "original-file" in smoke


def test_staging_route_is_exact_and_frontend_recognizes_the_staging_origin() -> None:
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    staging_caddy = (ROOT / "deploy" / "Caddyfile.staging").read_text(encoding="utf-8")
    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "staging.hr.greatsellai.net {" in caddy
    assert "reverse_proxy 172.17.0.1:18080" in caddy
    assert not re.search(r"(?m)^greatsellai\.net\s*\{", caddy)
    assert re.search(r"(?m)^:80\s*\{", staging_caddy)
    assert "staging.hr.greatsellai.net" not in staging_caddy
    assert 'hostname === "staging.hr.greatsellai.net"' in app


def test_legacy_production_caddy_redeploys_restore_only_the_exact_staging_route() -> None:
    gateway = (ROOT / "scripts" / "ensure-staging-gateway.sh").read_text(
        encoding="utf-8"
    )
    production_deploy = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(
        encoding="utf-8"
    )
    rollback = (ROOT / ".github" / "workflows" / "production-rollback.yml").read_text(
        encoding="utf-8"
    )

    assert "StrictHostKeyChecking=yes" in gateway
    assert "label=com.docker.compose.project=resume-screening-v3" in gateway
    assert "staging.hr.greatsellai.net" in gateway
    assert "reverse_proxy 172.17.0.1:18080" in gateway
    assert not re.search(r"(?m)^greatsellai\.net\s*\{", gateway)
    assert ".env.production" not in gateway
    assert "scripts/ensure-staging-gateway.sh" in production_deploy
    assert "scripts/ensure-staging-gateway.sh" in rollback


def test_staging_gateway_bootstrap_is_manual_and_production_approved() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "staging-gateway-bootstrap.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "ENABLE_STAGING_GATEWAY" in workflow
    assert "Staging gateway bootstrap must be started from main." in workflow
    assert "environment:\n      name: production" in workflow
    assert "group: greatsellai-hr-release-lane" in workflow
    assert "scripts/ensure-staging-gateway.sh" in workflow
    assert "greatsellai.net {" not in workflow


def test_production_recovery_workflows_restore_the_exact_staging_route_after_runtime_changes() -> None:
    for workflow_name in (
        "production-pending-finalize.yml",
        "production-healthy-pending-finalize.yml",
        "production-legacy-reconcile.yml",
    ):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )

        assert "id: production_ssh" in workflow
        assert "scripts/ensure-staging-gateway.sh" in workflow
        assert "steps.production_ssh.outcome == 'success'" in workflow
