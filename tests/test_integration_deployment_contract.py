from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/v1/mcp",
)
FEATURE_FLAGS = (
    "RESUME_V3_INTEGRATIONS_ENABLED",
    "RESUME_V3_INTEGRATIONS_MCP_ENABLED",
    "RESUME_V3_INTEGRATIONS_ANALYSIS_ENABLED",
    "RESUME_V3_INTEGRATIONS_OAUTH_ENABLED",
)


def test_caddy_proxies_only_the_exact_oauth_and_mcp_discovery_paths():
    for relative_path in ("deploy/Caddyfile", "deploy/Caddyfile.staging"):
        config = (ROOT / relative_path).read_text(encoding="utf-8")
        matcher = next(
            line for line in config.splitlines() if "@integration_discovery path" in line
        )

        assert all(path in matcher for path in DISCOVERY_PATHS)
        assert "/.well-known/*" not in config


def test_compose_defaults_all_integration_features_to_disabled():
    for relative_path in ("compose.yml", "deploy/compose.staging.yml"):
        config = (ROOT / relative_path).read_text(encoding="utf-8")
        for flag in FEATURE_FLAGS:
            assert f"{flag}: ${{{flag}:-0}}" in config


def test_env_templates_keep_all_integration_features_off():
    for relative_path in (".env.example", ".env.production.example"):
        config = (ROOT / relative_path).read_text(encoding="utf-8")
        for flag in FEATURE_FLAGS:
            assert f"{flag}=0" in config
