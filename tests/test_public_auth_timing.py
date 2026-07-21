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
    sleeps: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(public_auth_timing.asyncio, "sleep", capture_sleep)

    started_at = public_auth_timing.begin_password_reset_response()
    asyncio.run(
        public_auth_timing.enforce_password_reset_minimum_response_time(
            started_at=started_at,
            minimum_seconds=0.2,
            secret="test-timing-secret",
            email_key="email:timing@example.test",
        )
    )

    assert sleeps == [pytest.approx(0.14)]
