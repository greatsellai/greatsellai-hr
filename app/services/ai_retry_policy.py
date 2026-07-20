"""Shared classification for durable AI worker retries.

Gateway routing already distinguishes transport failures from authentication,
request, configuration, and structured-output failures. Durable batch workers
must preserve the same boundary when a provider-neutral error travels through
the temporary ``DeepSeekProviderError`` compatibility wrapper.
"""

from __future__ import annotations

import re


_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT_CODES = frozenset(
    {
        "ai_provider_network",
        "ai_provider_timeout",
        "ai_provider_rate_limited",
        "ai_provider_quota_exhausted",
        "ai_provider_provider_5xx",
        # Compatibility codes emitted before all provider calls moved behind
        # the gateway. Keep them only while existing domain helpers still use
        # the legacy exception wrapper.
        "deepseek_network_error",
        "deepseek_timeout",
    }
)


def is_retryable_ai_transport_error(error: str) -> bool:
    """Allow durable retry only for transient transport/provider failures."""

    if error in _RETRYABLE_TRANSPORT_CODES:
        return True
    matched = re.fullmatch(r"deepseek_http_(\d{3})", error)
    return bool(matched and int(matched.group(1)) in _RETRYABLE_HTTP_STATUSES)


__all__ = ["is_retryable_ai_transport_error"]
