"""Trusted-proxy-aware client identity for public request throttles."""
from __future__ import annotations

from ipaddress import ip_address, ip_network

from starlette.requests import Request


def client_rate_limit_identifier(request: Request, trusted_proxy_cidrs: tuple[str, ...]) -> str:
    """Resolve a stable client key without trusting caller-controlled headers.

    Only accept Caddy's final appended ``X-Forwarded-For`` address when the
    direct ASGI peer belongs to an explicitly configured trusted proxy range.
    Otherwise key by the direct peer and ignore all forwarded headers.
    """

    direct_peer = request.client.host if request.client is not None else "unknown"
    if not _is_trusted_proxy(direct_peer, trusted_proxy_cidrs):
        return f"peer:{direct_peer}"

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        candidate = forwarded_for.rsplit(",", maxsplit=1)[-1].strip()
        try:
            return f"ip:{ip_address(candidate).compressed}"
        except ValueError:
            pass
    return f"peer:{direct_peer}"


def _is_trusted_proxy(host: str, cidrs: tuple[str, ...]) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in ip_network(cidr, strict=False) for cidr in cidrs)
