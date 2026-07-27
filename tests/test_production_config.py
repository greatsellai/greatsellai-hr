from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import AppSettings
from app.services.mailbox_import_service import MailboxImportError, _fernet


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "postgresql+psycopg://user:pass@db/resume_v3",
        "environment": "production",
        "auto_create_schema": False,
        "seed_registry_on_startup": False,
        "trusted_proxy_cidrs": ("172.30.0.2/32",),
        "session_secret": "production-test-session-secret-that-is-independent",
        # Base64url encoding of 32 synthetic bytes; valid only for tests.
        "email_credentials_key": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    }
    values.update(overrides)
    return AppSettings(**values)


def test_production_refuses_sqlite_and_implicit_schema_bootstrap(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="production_requires_postgresql"):
        _settings(tmp_path, database_url="sqlite:///local.db").validate_runtime()
    with pytest.raises(RuntimeError, match="production_must_use_alembic"):
        _settings(tmp_path, auto_create_schema=True).validate_runtime()
    with pytest.raises(RuntimeError, match="production_must_seed_registry"):
        _settings(tmp_path, seed_registry_on_startup=True).validate_runtime()
    with pytest.raises(RuntimeError, match="production_must_not_allow_unauthenticated"):
        _settings(tmp_path, allow_unauthenticated=True).validate_runtime()
    with pytest.raises(RuntimeError, match="production_requires_trusted_proxy_cidrs"):
        _settings(tmp_path, trusted_proxy_cidrs=()).validate_runtime()


def test_production_postgresql_with_explicit_migration_path_is_valid(tmp_path: Path) -> None:
    _settings(tmp_path).validate_runtime()


@pytest.mark.parametrize(
    ("trusted_proxy_cidrs", "error"),
    [
        (("0.0.0.0/0",), "must not trust an all-address network"),
        (("8.8.8.8/32",), "must contain private or loopback networks"),
    ],
)
def test_proxy_forwarding_configuration_never_trusts_all_public_clients(
    tmp_path: Path,
    trusted_proxy_cidrs: tuple[str, ...],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _settings(tmp_path, trusted_proxy_cidrs=trusted_proxy_cidrs).validate_runtime()


def test_production_requires_independent_session_and_email_encryption_keys(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="production_session_secret_required"):
        _settings(tmp_path, session_secret=None).validate_runtime()
    with pytest.raises(RuntimeError, match="production_email_credentials_key_required"):
        _settings(
            tmp_path,
            transactional_email_provider="test",
            public_app_url="https://hr.example.test",
            email_credentials_key=None,
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="production_session_and_email_credentials_keys_must_differ"):
        _settings(
            tmp_path,
            session_secret="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            email_credentials_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="production_session_secret_must_differ_from_legacy_admin_token"):
        _settings(
            tmp_path,
            admin_token="production-test-session-secret-that-is-independent",
        ).validate_runtime()
    with pytest.raises(RuntimeError, match="legacy_admin_token_enabled_requires_admin_token"):
        _settings(tmp_path, legacy_admin_token_enabled=True, admin_token=None).validate_runtime()


def test_production_without_email_flows_can_start_without_an_unused_email_key(tmp_path: Path) -> None:
    """Mailbox and sender operations still fail closed when the key is absent."""

    settings = _settings(tmp_path, email_credentials_key=None)
    settings.validate_runtime()
    with pytest.raises(MailboxImportError, match="mailbox_credentials_key_not_configured"):
        _fernet(settings)


def test_settings_load_generic_provider_credential_map_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON",
        '{"configured-provider-ref":"test-only-provider-value"}',
    )

    settings = AppSettings.from_env()

    assert settings.ai_provider_credentials == {
        "configured-provider-ref": "test-only-provider-value"
    }


def test_compose_injects_generic_provider_credential_map_into_api_and_worker() -> None:
    """The control plane can only publish credentials the runtimes can resolve.

    This is intentionally a source-level Compose contract: it does not require
    Docker in the unit-test environment and never reads a deployment env file.
    The shared anchor is used by API and worker, so both processes receive the
    same server-only reference map when Compose renders production services.
    """

    compose = (Path(__file__).resolve().parents[1] / "compose.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON: '
        '"${RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON:-}"'
    ) in compose
    for service in ("migrate", "api", "worker"):
        match = re.search(
            rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  [a-z][a-z_]*:|\Z)",
            compose,
        )
        assert match is not None
        assert "    environment: *app-environment" in match.group("body")


def test_compose_injects_mailbox_oauth_clients_into_every_runtime() -> None:
    """OAuth code exchange and worker token refresh need the same config."""

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    production_example = (root / ".env.production.example").read_text(encoding="utf-8")
    variables = (
        "RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_ID",
        "RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_SECRET",
        "RESUME_V3_MAILBOX_GOOGLE_OAUTH_REDIRECT_URI",
        "RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_ID",
        "RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_SECRET",
        "RESUME_V3_MAILBOX_MICROSOFT_OAUTH_REDIRECT_URI",
    )

    for variable in variables:
        assert f"{variable}: ${{{variable}:-}}" in compose
        assert f"{variable}=" in production_example

    for service in ("migrate", "api", "worker"):
        match = re.search(
            rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  [a-z][a-z_]*:|\Z)", compose
        )
        assert match is not None
        assert "    environment: *app-environment" in match.group("body")


def test_compose_injects_one_complete_mailbox_policy_into_every_runtime() -> None:
    """API and worker must not disagree about a mailbox's safety envelope."""

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    production_example = (root / ".env.production.example").read_text(
        encoding="utf-8"
    )
    variables = (
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
    )

    for variable in variables:
        assert f"{variable}:" in compose
        assert f"{variable}=" in production_example

    for service in ("migrate", "api", "worker"):
        match = re.search(
            rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  [a-z][a-z_]*:|\Z)", compose
        )
        assert match is not None
        assert "    environment: *app-environment" in match.group("body")


def test_compose_injects_tencent_ses_templates_into_api_and_worker() -> None:
    """SES configuration must reach both synchronous and durable send paths."""

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    production_example = (root / ".env.production.example").read_text(
        encoding="utf-8"
    )

    assert "TENCENT_SES_REGION: ${TENCENT_SES_REGION:-ap-guangzhou}" in compose
    assert (
        "TENCENT_SES_VERIFICATION_TEMPLATE_ID: "
        "${TENCENT_SES_VERIFICATION_TEMPLATE_ID:-}"
    ) in compose
    assert (
        "TENCENT_SES_PASSWORD_RESET_TEMPLATE_ID: "
        "${TENCENT_SES_PASSWORD_RESET_TEMPLATE_ID:-}"
    ) in compose
    assert "TENCENT_SES_REGION=ap-guangzhou" in production_example
    assert "TENCENT_SES_VERIFICATION_TEMPLATE_ID=" in production_example
    assert "TENCENT_SES_PASSWORD_RESET_TEMPLATE_ID=" in production_example

    for service in ("migrate", "api", "worker"):
        match = re.search(
            rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  [a-z][a-z_]*:|\Z)", compose
        )
        assert match is not None
        assert "    environment: *app-environment" in match.group("body")


def test_compose_explicitly_wires_the_opt_in_legacy_admin_entry_flag() -> None:
    """A configured compatibility login flag must reach the API process.

    The identity service keeps this password-only entry disabled by default,
    but an operator must be able to enable the documented compatibility path
    without editing the Compose source on a production host.
    """

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    production_example = (root / ".env.production.example").read_text(
        encoding="utf-8"
    )

    assert (
        "RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED: "
        "${RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED:-0}"
    ) in compose
    assert "RESUME_V3_LEGACY_ADMIN_TOKEN_ENABLED=0" in production_example


def test_compose_pins_the_only_trusted_proxy_to_caddy_private_address() -> None:
    """Rendered production config must not silently ignore Caddy forwarding.

    The exact `172.30.0.2/32` value is safe because Compose reserves that
    address for Caddy on a network joined only by Caddy and API. The runtime
    setting then still verifies the direct TCP peer before reading XFF.
    """

    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yml").read_text(encoding="utf-8")
    production_example = (root / ".env.production.example").read_text(encoding="utf-8")

    assert (
        "RESUME_V3_TRUSTED_PROXY_CIDRS: "
        "${RESUME_V3_TRUSTED_PROXY_CIDRS:?set RESUME_V3_TRUSTED_PROXY_CIDRS in .env.production}"
    ) in compose
    assert "RESUME_V3_TRUSTED_PROXY_CIDRS=172.30.0.2/32" in production_example
    api_networks = re.search(
        r"(?ms)^  api:\n(?P<body>.*?)(?=^  [a-z][a-z_]*:|\Z)", compose
    )
    assert api_networks is not None
    assert "ipv4_address: 172.30.0.3" in api_networks.group("body")
    assert "ipv4_address: 172.30.0.2" in compose
    assert "subnet: 172.30.0.0/24" in compose
    assert "  caddy:\n" in compose
    assert "  api:\n" in compose
