"""Workspace-scoped health incidents for durable mailbox sync tasks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.models import MailboxConfig, MailboxSyncFailureAlert
from app.tenant_scope import organization_context_id


ALERT_STATE_MONITORING = "monitoring"
ALERT_STATE_OPEN = "open"
ALERT_STATE_RESOLVED = "resolved"

# These errors describe a disabled/removed source, an intentional task
# invalidation, or a bounded message that was safely skipped. They are not an
# unhealthy mailbox connection and must not train users to ignore an alert.
_NON_ALERTING_SYNC_FAILURES = frozenset(
    {
        "mailbox_config_not_found",
        "mailbox_config_archived",
        "mailbox_workspace_missing",
        "mailbox_workspace_mismatch",
        "mailbox_not_enabled",
        "mailbox_task_source_changed",
        # A sync lease collision is operationally normal: another durable
        # worker already owns the same mailbox. It is not a mailbox outage.
        "mailbox_sync_in_progress",
        "mailbox_sync_claim_failed",
        "mailbox_message_too_large",
        "mailbox_message_headers_too_large",
        "mailbox_mime_structure_too_complex",
        "mailbox_attachment_count_exceeded",
        "mailbox_attachment_too_large",
        "mailbox_attachment_total_too_large",
    }
)

# A bad credential, changed source epoch, or blocked endpoint cannot recover
# through the normal retry loop. Surface it immediately instead of making a
# recruiter wait for three terminal scheduled tasks.
_IMMEDIATE_CRITICAL_FAILURES = frozenset(
    {
        "mailbox_credentials_unavailable",
        "mailbox_credentials_key_invalid",
        "mailbox_source_epoch_changed",
        "mailbox_source_watermark_invalid",
        "mailbox_imap_host_not_allowed",
        "mailbox_imap_port_not_allowed",
        "mailbox_imap_address_not_allowed",
        "mailbox_imap_dns_failed",
        "mailbox_imap_argument_invalid",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mailbox_alert_or_none(
    session: Session,
    *,
    mailbox_config_id: str,
) -> tuple[MailboxConfig, MailboxSyncFailureAlert | None] | None:
    organization_id = organization_context_id(session)
    config = session.scalar(
        select(MailboxConfig).where(
            MailboxConfig.id == mailbox_config_id,
            MailboxConfig.organization_id == organization_id,
        )
    )
    if config is None:
        return None
    alert = session.scalar(
        select(MailboxSyncFailureAlert).where(
            MailboxSyncFailureAlert.mailbox_config_id == mailbox_config_id,
            MailboxSyncFailureAlert.organization_id == organization_id,
        )
    )
    return config, alert


def _new_or_concurrently_created_alert(
    session: Session,
    *,
    organization_id: str,
    config: MailboxConfig,
) -> MailboxSyncFailureAlert:
    """Create the one-row incident without racing a second worker.

    Normal mailbox jobs are deduplicated, but lease recovery can still cause
    two workers to finish neighboring terminal tasks at once. The unique
    mailbox constraint remains the source of truth; a savepoint lets the
    losing transaction load the winner instead of rolling back its job update.
    """

    created = MailboxSyncFailureAlert(
        organization_id=organization_id,
        mailbox_config_id=config.id,
        state=ALERT_STATE_MONITORING,
        severity="warning",
    )
    try:
        with session.begin_nested():
            session.add(created)
            session.flush()
    except IntegrityError:
        concurrent = session.scalar(
            select(MailboxSyncFailureAlert).where(
                MailboxSyncFailureAlert.mailbox_config_id == config.id,
                MailboxSyncFailureAlert.organization_id == organization_id,
            )
        )
        if concurrent is None:
            raise
        return concurrent
    return created


def record_terminal_sync_failure(
    session: Session,
    *,
    settings: AppSettings,
    mailbox_config_id: str,
    job_id: str,
    error_code: str,
    now: datetime | None = None,
) -> MailboxSyncFailureAlert | None:
    """Update one incident after a sync task has exhausted its own retries.

    Callers must invoke this only for a terminal ``sync`` job. That placement
    is what makes three task retries count as one failed synchronization.
    """

    if error_code in _NON_ALERTING_SYNC_FAILURES:
        return None
    current_time = now or _utcnow()
    scoped = _mailbox_alert_or_none(session, mailbox_config_id=mailbox_config_id)
    if scoped is None:
        return None
    config, alert = scoped
    if (
        (config.archived_at is not None or not config.enabled)
        and error_code not in _IMMEDIATE_CRITICAL_FAILURES
    ):
        return None
    organization_id = organization_context_id(session)
    if alert is None:
        alert = _new_or_concurrently_created_alert(
            session,
            organization_id=organization_id,
            config=config,
        )

    previous_failure = _as_utc(alert.last_failed_at)
    continuous = bool(
        # An already open incident stays visible until a successful sync or a
        # deliberate lifecycle action resolves it. The time window only
        # determines whether an *unopened* streak should restart.
        alert.state == ALERT_STATE_OPEN
        or (
            alert.state == ALERT_STATE_MONITORING
            and previous_failure is not None
            and current_time - previous_failure
            <= timedelta(seconds=settings.mailbox_consecutive_failure_window_seconds)
        )
    )
    if not continuous:
        alert.consecutive_failures = 0
        alert.first_failed_at = current_time
        alert.opened_at = None
        alert.resolved_at = None
        alert.resolution = None

    alert.consecutive_failures += 1
    alert.last_failed_at = current_time
    alert.last_error_code = error_code[:128]
    alert.last_job_id = job_id
    immediate = error_code in _IMMEDIATE_CRITICAL_FAILURES
    if immediate or alert.consecutive_failures >= settings.mailbox_consecutive_failure_alert_threshold:
        if alert.state != ALERT_STATE_OPEN:
            alert.opened_at = current_time
        alert.state = ALERT_STATE_OPEN
        alert.severity = (
            "critical"
            if immediate or alert.severity == "critical"
            else "warning"
        )
    else:
        alert.state = ALERT_STATE_MONITORING
        alert.severity = "warning"
    session.flush()
    return alert


def resolve_mailbox_sync_alert(
    session: Session,
    *,
    mailbox_config_id: str,
    resolution: str,
    now: datetime | None = None,
) -> MailboxSyncFailureAlert | None:
    """Resolve an active streak after a healthy sync or intentional stop."""

    scoped = _mailbox_alert_or_none(session, mailbox_config_id=mailbox_config_id)
    if scoped is None:
        return None
    _, alert = scoped
    if alert is None or alert.state == ALERT_STATE_RESOLVED:
        return alert
    alert.state = ALERT_STATE_RESOLVED
    alert.consecutive_failures = 0
    alert.resolved_at = now or _utcnow()
    alert.resolution = resolution[:32]
    session.flush()
    return alert


def active_sync_alert(alert: MailboxSyncFailureAlert | None) -> MailboxSyncFailureAlert | None:
    """Return only an incident that the workspace should be asked to handle."""

    return alert if alert is not None and alert.state == ALERT_STATE_OPEN else None


__all__ = [
    "ALERT_STATE_OPEN",
    "MailboxSyncFailureAlert",
    "active_sync_alert",
    "record_terminal_sync_failure",
    "resolve_mailbox_sync_alert",
]
