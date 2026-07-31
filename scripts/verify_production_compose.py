"""Verify production Compose forwarding and artifact contracts without logging env values.

This runs only against the committed example environment. It deliberately
captures Compose output instead of printing it because a real rendered config
would include deployment-only secrets.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CADDY_PROXY_IP = "172.30.0.2"
EXPECTED_API_PROXY_IP = "172.30.0.3"
EXPECTED_PROXY_CIDR = f"{EXPECTED_CADDY_PROXY_IP}/32"
EXPECTED_UPLOADS_VOLUME = "resume-screening-v3_uploads_data"


def _fail(reason: str) -> None:
    raise RuntimeError(f"production_compose_contract_failed:{reason}")


def _render_compose() -> dict[str, object]:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.production.example",
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        # Do not return stderr: Compose may include environment substitutions.
        _fail(f"docker_compose_config_exit_{completed.returncode}")
    try:
        rendered = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("production_compose_contract_failed:invalid_json") from exc
    if not isinstance(rendered, dict):
        _fail("rendered_root_not_object")
    return rendered


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label}_not_object")
    return value


def main() -> None:
    rendered = _render_compose()
    services = _mapping(rendered.get("services"), label="services")
    api = _mapping(services.get("api"), label="api")
    caddy = _mapping(services.get("caddy"), label="caddy")
    api_environment = _mapping(api.get("environment"), label="api_environment")
    if api_environment.get("RESUME_V3_TRUSTED_PROXY_CIDRS") != EXPECTED_PROXY_CIDR:
        _fail("trusted_proxy_cidr_not_exact_caddy_address")
    api_networks = _mapping(api.get("networks"), label="api_networks")
    caddy_networks = _mapping(caddy.get("networks"), label="caddy_networks")
    if set(caddy_networks) != {"proxy"}:
        _fail("caddy_has_unexpected_network_membership")
    if "proxy" not in api_networks:
        _fail("api_missing_proxy_network")
    api_proxy = _mapping(api_networks.get("proxy"), label="api_proxy_network")
    if api_proxy.get("ipv4_address") != EXPECTED_API_PROXY_IP:
        _fail("api_proxy_address_not_static")
    caddy_proxy = _mapping(caddy_networks.get("proxy"), label="caddy_proxy_network")
    if caddy_proxy.get("ipv4_address") != EXPECTED_CADDY_PROXY_IP:
        _fail("caddy_proxy_address_not_static")
    if api_proxy.get("ipv4_address") == caddy_proxy.get("ipv4_address"):
        _fail("api_and_caddy_proxy_addresses_collide")

    for service_name in ("db", "migrate", "worker"):
        service = _mapping(services.get(service_name), label=service_name)
        networks = _mapping(service.get("networks"), label=f"{service_name}_networks")
        if "proxy" in networks:
            _fail(f"{service_name}_must_not_join_proxy_network")

    volumes = _mapping(rendered.get("volumes"), label="volumes")
    uploads = _mapping(volumes.get("uploads_data"), label="uploads_volume")
    if uploads.get("name") != EXPECTED_UPLOADS_VOLUME:
        _fail("uploads_volume_name_changed")

    print("production-compose-contract: passed")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
