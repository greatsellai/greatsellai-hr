from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import AppSettings


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "postgresql+psycopg://user:pass@db/resume_v3",
        "environment": "production",
        "auto_create_schema": False,
        "seed_registry_on_startup": False,
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


def test_production_postgresql_with_explicit_migration_path_is_valid(tmp_path: Path) -> None:
    _settings(tmp_path).validate_runtime()


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
