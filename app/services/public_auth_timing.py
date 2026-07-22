"""Timing-noise controls for public account-recovery responses.

Password-reset requests must not reveal whether an account exists merely
because issuing a real token and outbox row does more work than an unknown
address. Every valid request outcome therefore reaches the same lower-bound
clock budget plus bounded random jitter. This is deliberately *not* described
as mathematical constant-time behavior: database contention, scheduling and
network delivery can still make a request exceed the target. The bounded
budget, transient keyed work, and per-client/account throttles make repeated
remote timing comparisons materially less useful without blocking the event
loop or storing timing telemetry.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
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


def _sample_password_reset_jitter_seconds(maximum_seconds: float) -> float:
    """Return bounded entropy used by every valid public reset outcome."""

    if maximum_seconds <= 0:
        return 0.0
    # 10k evenly-spaced values are enough to defeat a fixed timing floor while
    # keeping tests deterministic by monkeypatching this private sampler.
    return maximum_seconds * (secrets.randbelow(10_000) / 10_000)


async def enforce_password_reset_minimum_response_time(
    *,
    started_at: float,
    minimum_seconds: float,
    jitter_seconds: float,
    secret: str,
    email_key: str,
) -> None:
    """Finish a recovery response no sooner than a bounded target budget.

    The endpoint invokes this from ``finally`` for known, unknown,
    enqueue-failure, and rate-limited outcomes. ``minimum_seconds`` and
    ``jitter_seconds`` are validated by :class:`AppSettings`; the resulting
    intentional delay is bounded at configuration time. If preceding work
    already exceeds the target, no extra sleep is added rather than claiming
    a false fixed response duration.
    """

    _perform_password_reset_dummy_crypto(secret=secret, email_key=email_key)
    target_seconds = minimum_seconds + _sample_password_reset_jitter_seconds(jitter_seconds)
    elapsed = max(0.0, monotonic() - started_at)
    remaining = min(max(0.0, target_seconds - elapsed), target_seconds)
    if remaining > 0:
        await asyncio.sleep(remaining)


__all__ = [
    "begin_password_reset_response",
    "enforce_password_reset_minimum_response_time",
]
