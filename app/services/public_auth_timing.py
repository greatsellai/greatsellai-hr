"""Timing equalization for public account-recovery responses.

Password-reset requests must not reveal whether an account exists merely
because issuing a real token and outbox row does more work than an unknown
address.  The endpoint therefore finishes every response on a configurable
minimum clock budget.  This module deliberately performs only transient,
keyed dummy work: it never writes an email address, token, or response timing
record anywhere.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from time import monotonic


def begin_password_reset_response() -> float:
    """Capture a monotonic start time for one public recovery request."""

    return monotonic()


def _perform_password_reset_dummy_crypto(*, secret: str, email_key: str) -> None:
    """Do bounded, non-persistent keyed work for every recovery response.

    The public endpoint already derives an opaque normalized-or-invalid
    address key for its durable limiter.  Reusing that transient value here
    avoids creating a raw-email side channel while ensuring the unknown and
    failed-enqueue branches do not skip all secret-keyed cryptographic work.
    The timing floor below, rather than this inexpensive HMAC, is the primary
    equalization control.
    """

    material = f"password-reset-response-v1:{email_key}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), material, hashlib.sha256).digest()
    # Consume the digest in constant time so the compiler/runtime cannot make
    # this branch observably conditional on the opaque account key.
    hmac.compare_digest(digest, digest)


async def enforce_password_reset_minimum_response_time(
    *,
    started_at: float,
    minimum_seconds: float,
    secret: str,
    email_key: str,
) -> None:
    """Finish a recovery response no sooner than its configured time budget.

    ``minimum_seconds`` is validated by :class:`AppSettings` before serving
    requests.  The sleep is async and capped to that same bounded budget, so
    it does not block the event loop or turn a malformed clock value into an
    unbounded wait.
    """

    _perform_password_reset_dummy_crypto(secret=secret, email_key=email_key)
    elapsed = max(0.0, monotonic() - started_at)
    remaining = min(max(0.0, minimum_seconds - elapsed), minimum_seconds)
    if remaining > 0:
        await asyncio.sleep(remaining)


__all__ = [
    "begin_password_reset_response",
    "enforce_password_reset_minimum_response_time",
]
