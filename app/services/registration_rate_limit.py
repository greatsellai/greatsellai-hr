"""Durable, privacy-preserving protection for public self-registration."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RegistrationRateLimitBucket


class RegistrationRateLimitError(RuntimeError):
    """Stable public registration throttle response."""


@dataclass(frozen=True)
class RegistrationRateLimitRule:
    scope: str
    value: str
    limit: int
    window_seconds: int


def enforce_registration_rate_limit(
    session: Session,
    *,
    secret: str,
    client_identifier: str,
    email_key: str,
    global_limit: int,
    global_window_seconds: int,
    client_limit: int,
    client_window_seconds: int,
    email_limit: int,
    email_window_seconds: int,
    now: datetime | None = None,
) -> None:
    """Consume one registration allowance or fail before account creation.

    Every API replica uses the same database buckets.  PostgreSQL row locks
    serialize updates to existing buckets; the unique window key handles the
    first-row race.  No raw IP address or email address is persisted.
    """

    current_time = _aware(now) or datetime.now(timezone.utc)
    rules = (
        RegistrationRateLimitRule(
            scope="registration_global",
            value="global",
            limit=global_limit,
            window_seconds=global_window_seconds,
        ),
        RegistrationRateLimitRule(
            scope="registration_client",
            value=client_identifier,
            limit=client_limit,
            window_seconds=client_window_seconds,
        ),
        RegistrationRateLimitRule(
            scope="registration_email",
            value=email_key,
            limit=email_limit,
            window_seconds=email_window_seconds,
        ),
    )
    # Buckets are security counters, not an event log.  Retain only the
    # longest active window so unbounded unique-email traffic cannot grow the
    # table forever.
    retention_cutoff = current_time - timedelta(
        seconds=max(rule.window_seconds for rule in rules)
    )
    session.execute(
        delete(RegistrationRateLimitBucket).where(
            RegistrationRateLimitBucket.window_started_at < retention_cutoff
        )
    )
    # Fixed lock order avoids a deadlock when separate signup requests share
    # only some of their buckets.
    for rule in rules:
        _consume_bucket(session, secret=secret, rule=rule, now=current_time)


def _consume_bucket(
    session: Session,
    *,
    secret: str,
    rule: RegistrationRateLimitRule,
    now: datetime,
) -> None:
    window_started_at = _window_started_at(now, rule.window_seconds)
    key_digest = _rate_limit_digest(secret, scope=rule.scope, value=rule.value)
    statement = (
        select(RegistrationRateLimitBucket)
        .where(
            RegistrationRateLimitBucket.scope == rule.scope,
            RegistrationRateLimitBucket.key_digest == key_digest,
            RegistrationRateLimitBucket.window_started_at == window_started_at,
        )
        .with_for_update()
    )
    bucket = session.scalar(statement)
    if bucket is None:
        # A missing row cannot be locked.  The unique constraint resolves the
        # resulting first-request race, then the loser re-reads the winner
        # under the usual row lock.
        try:
            with session.begin_nested():
                bucket = RegistrationRateLimitBucket(
                    scope=rule.scope,
                    key_digest=key_digest,
                    window_started_at=window_started_at,
                    request_count=0,
                )
                session.add(bucket)
                session.flush()
        except IntegrityError:
            bucket = session.scalar(statement)
            if bucket is None:
                raise

    if bucket.request_count >= rule.limit:
        raise RegistrationRateLimitError("registration_rate_limit_exceeded")
    bucket.request_count += 1


def _rate_limit_digest(secret: str, *, scope: str, value: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{scope}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _window_started_at(now: datetime, window_seconds: int) -> datetime:
    timestamp = int(now.timestamp())
    return datetime.fromtimestamp(
        timestamp - (timestamp % window_seconds),
        tz=timezone.utc,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
