from __future__ import annotations

import asyncio

import pytest

from app.services import public_auth_timing


def test_password_reset_timing_floor_is_deterministic_and_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timing guard sleeps only for the remaining bounded async budget."""

    clock_values = iter((100.0, 100.06))
    monkeypatch.setattr(public_auth_timing, "monotonic", lambda: next(clock_values))
    monkeypatch.setattr(
        public_auth_timing,
        "_sample_password_reset_jitter_seconds",
        lambda maximum_seconds: maximum_seconds / 2,
    )
    sleeps: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(public_auth_timing.asyncio, "sleep", capture_sleep)

    started_at = public_auth_timing.begin_password_reset_response()
    asyncio.run(
        public_auth_timing.enforce_password_reset_minimum_response_time(
            started_at=started_at,
            minimum_seconds=0.2,
            jitter_seconds=0.08,
            secret="test-timing-secret",
            email_key="email:timing@example.test",
        )
    )

    assert sleeps == [pytest.approx(0.18)]


def test_password_reset_timing_guard_does_not_claim_fixed_time_after_slow_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow known-path work may exceed the bounded target without extra delay."""

    clock_values = iter((100.0, 101.1))
    monkeypatch.setattr(public_auth_timing, "monotonic", lambda: next(clock_values))
    monkeypatch.setattr(
        public_auth_timing,
        "_sample_password_reset_jitter_seconds",
        lambda maximum_seconds: maximum_seconds,
    )
    sleeps: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(public_auth_timing.asyncio, "sleep", capture_sleep)
    started_at = public_auth_timing.begin_password_reset_response()
    asyncio.run(
        public_auth_timing.enforce_password_reset_minimum_response_time(
            started_at=started_at,
            minimum_seconds=0.75,
            jitter_seconds=0.25,
            secret="test-timing-secret",
            email_key="email:slow@example.test",
        )
    )

    assert sleeps == []
