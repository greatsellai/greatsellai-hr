"""Durable, privacy-preserving throttles for public account endpoints.

The database model keeps its original ``RegistrationRateLimitBucket`` name
for migration compatibility, but it intentionally stores counters for more
than registration.  Each endpoint gets a separate, namespaced scope so a
password-reset request can never spend a registration allowance (or vice
versa).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import RegistrationRateLimitBucket


class PublicRateLimitError(RuntimeError):
    """Base class for a stable, endpoint-specific public throttle response."""

    code = "public_rate_limit_exceeded"


class RegistrationRateLimitError(PublicRateLimitError):
    """Stable public registration throttle response."""

    code = "registration_rate_limit_exceeded"


class PasswordResetRateLimitError(PublicRateLimitError):
    """Stable public password-reset throttle response."""

    code = "password_reset_rate_limit_exceeded"


class LoginRateLimitError(PublicRateLimitError):
    """Stable public login throttle response without account disclosure."""

    code = "login_rate_limit_exceeded"


@dataclass(frozen=True)
class PublicRateLimitRule:
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
    """Consume one registration allowance or fail before account creation."""

    _enforce_public_rate_limit(
        session,
        secret=secret,
        rules=(
            PublicRateLimitRule(
                scope="registration_global",
                value="global",
                limit=global_limit,
                window_seconds=global_window_seconds,
            ),
            PublicRateLimitRule(
                scope="registration_client",
                value=client_identifier,
                limit=client_limit,
                window_seconds=client_window_seconds,
            ),
            PublicRateLimitRule(
                scope="registration_email",
                value=email_key,
                limit=email_limit,
                window_seconds=email_window_seconds,
            ),
        ),
        error_type=RegistrationRateLimitError,
        now=now,
    )


def enforce_password_reset_rate_limit(
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
    """Consume one password-reset request allowance before issuing a token.

    The email key is an opaque normalized-or-invalid-input namespace that is
    HMACed before persistence.  This function is deliberately called before
    ``issue_password_reset``: a rejected request must not invalidate an
    already-delivered, still-valid recovery link.
    """

    _enforce_public_rate_limit(
        session,
        secret=secret,
        rules=(
            PublicRateLimitRule(
                scope="password_reset_global",
                value="global",
                limit=global_limit,
                window_seconds=global_window_seconds,
            ),
            PublicRateLimitRule(
                scope="password_reset_client",
                value=client_identifier,
                limit=client_limit,
                window_seconds=client_window_seconds,
            ),
            PublicRateLimitRule(
                scope="password_reset_email",
                value=email_key,
                limit=email_limit,
                window_seconds=email_window_seconds,
            ),
        ),
        error_type=PasswordResetRateLimitError,
        now=now,
    )


def ensure_login_rate_limit_available(
    session: Session,
    *,
    secret: str,
    client_identifier: str,
    email_key: str,
    client_limit: int,
    client_window_seconds: int,
    email_limit: int,
    email_window_seconds: int,
    now: datetime | None = None,
) -> None:
    """Reject a credential attempt only when its failure budget is exhausted.

    A correct password does not spend the budget.  The account-oriented
    bucket is deliberately keyed by the trusted client and normalized account
    together, so a hostile network cannot exhaust another network's account
    budget.  Keys are HMACed in exactly the same durable bucket table as
    registration and reset limits.
    """

    _assert_public_rate_limit_available(
        session,
        secret=secret,
        rules=_login_rate_limit_rules(
            client_identifier=client_identifier,
            email_key=email_key,
            client_limit=client_limit,
            client_window_seconds=client_window_seconds,
            email_limit=email_limit,
            email_window_seconds=email_window_seconds,
        ),
        error_type=LoginRateLimitError,
        now=now,
    )


def record_login_failure(
    session: Session,
    *,
    secret: str,
    client_identifier: str,
    email_key: str,
    client_limit: int,
    client_window_seconds: int,
    email_limit: int,
    email_window_seconds: int,
    now: datetime | None = None,
) -> None:
    """Persist one failed login attempt across API replicas."""

    _enforce_public_rate_limit(
        session,
        secret=secret,
        rules=_login_rate_limit_rules(
            client_identifier=client_identifier,
            email_key=email_key,
            client_limit=client_limit,
            client_window_seconds=client_window_seconds,
            email_limit=email_limit,
            email_window_seconds=email_window_seconds,
        ),
        error_type=LoginRateLimitError,
        now=now,
    )


def _login_rate_limit_rules(
    *,
    client_identifier: str,
    email_key: str,
    client_limit: int,
    client_window_seconds: int,
    email_limit: int,
    email_window_seconds: int,
) -> tuple[PublicRateLimitRule, ...]:
    return (
        PublicRateLimitRule(
            scope="login_client",
            value=client_identifier,
            limit=client_limit,
            window_seconds=client_window_seconds,
        ),
        PublicRateLimitRule(
            # Do not make this account-only: an attacker could otherwise
            # deny service to a known account from any network.  This durable
            # composite still slows repeated credential stuffing for the
            # same trusted client/account pair without storing either value.
            scope="login_client_account",
            value=f"{client_identifier}\x00{email_key}",
            limit=email_limit,
            window_seconds=email_window_seconds,
        ),
    )


def _enforce_public_rate_limit(
    session: Session,
    *,
    secret: str,
    rules: tuple[PublicRateLimitRule, ...],
    error_type: type[PublicRateLimitError],
    now: datetime | None,
) -> None:
    """Persist a namespaced set of public API counters.

    Every API replica uses the same database buckets.  PostgreSQL row locks
    serialize updates to existing buckets; the unique window key handles the
    first-row race.  No raw IP address or email address is persisted.
    """

    current_time = _aware(now) or datetime.now(timezone.utc)
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
    # Fixed lock order avoids a deadlock when concurrent public-account
    # requests share only some of their buckets.
    for rule in rules:
        _consume_bucket(
            session,
            secret=secret,
            rule=rule,
            now=current_time,
            error_type=error_type,
        )


def _assert_public_rate_limit_available(
    session: Session,
    *,
    secret: str,
    rules: tuple[PublicRateLimitRule, ...],
    error_type: type[PublicRateLimitError],
    now: datetime | None,
) -> None:
    """Read the existing buckets without consuming a successful-login slot."""

    current_time = _aware(now) or datetime.now(timezone.utc)
    for rule in rules:
        window_started_at = _window_started_at(current_time, rule.window_seconds)
        key_digest = _rate_limit_digest(secret, scope=rule.scope, value=rule.value)
        request_count = session.scalar(
            select(RegistrationRateLimitBucket.request_count).where(
                RegistrationRateLimitBucket.scope == rule.scope,
                RegistrationRateLimitBucket.key_digest == key_digest,
                RegistrationRateLimitBucket.window_started_at == window_started_at,
            )
        )
        if request_count is not None and request_count >= rule.limit:
            raise error_type(error_type.code)


def _consume_bucket(
    session: Session,
    *,
    secret: str,
    rule: PublicRateLimitRule,
    now: datetime,
    error_type: type[PublicRateLimitError],
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
        raise error_type(error_type.code)
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
