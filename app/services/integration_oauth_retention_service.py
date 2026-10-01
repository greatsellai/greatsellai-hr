"""Bounded reclamation of abandoned registrations and terminal OAuth grants."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.models import (
    IntegrationAnalysisReport,
    IntegrationCredential,
    IntegrationGrant,
    IntegrationIdempotencyRecord,
    IntegrationOAuthClient,
    IntegrationOAuthCode,
    IntegrationOAuthConsent,
    IntegrationOAuthFamily,
    IntegrationOAuthRefresh,
    IntegrationRequestLease,
)
from app.services.integration_auth_service import (
    IntegrationAccessError,
    integration_now,
    lock_integration_policy,
)
from app.tenant_scope import (
    clear_organization_context,
    organization_context_id,
    set_organization_context,
)

UNUSED_CLIENT_RETENTION = timedelta(hours=1)
AUTHORIZED_CLIENT_RETENTION = timedelta(days=30)
TERMINAL_FAMILY_RETENTION = timedelta(days=30)


def _terminal_family_filter(now: datetime):
    family = IntegrationOAuthFamily.__table__
    grant = IntegrationGrant.__table__
    cutoff = now - TERMINAL_FAMILY_RETENTION
    return and_(
        grant.c.id == family.c.grant_id,
        grant.c.organization_id == family.c.organization_id,
        or_(
            family.c.expires_at <= cutoff,
            and_(family.c.revoked_at.is_not(None), family.c.revoked_at <= cutoff),
            and_(grant.c.revoked_at.is_not(None), grant.c.revoked_at <= cutoff),
        ),
    )


def cleanup_terminal_oauth_families(
    session: Session,
    *,
    organization_id: str,
    now: datetime | None = None,
    limit: int = 1000,
    policy_locked: bool = False,
) -> int:
    """Remove expired/revoked families only after a 30-day recovery window.

    The workspace policy row serializes this with token validation, rotation,
    revocation, and consent approval. Active family replay rows are retained.
    Historical private drafts keep their source grant; grants without drafts are
    removed after their credentials and transient references are cleared.
    """
    current = integration_now(now)
    batch = max(1, min(limit, 1000))
    if not policy_locked:
        try:
            lock_integration_policy(session, organization_id)
        except IntegrationAccessError:
            # No policy means no external OAuth operation can hold a live family.
            # Keep anomalous/orphan families for diagnosis rather than deleting
            # them without the serialization point.
            return 0

    family_table = IntegrationOAuthFamily.__table__
    grant_table = IntegrationGrant.__table__
    lease_table = IntegrationRequestLease.__table__
    active_lease = select(lease_table.c.id).where(
        lease_table.c.organization_id == organization_id,
        lease_table.c.grant_id == grant_table.c.id,
        lease_table.c.released_at.is_(None),
        lease_table.c.expires_at > current,
    ).exists()
    candidates = tuple(session.scalars(
        select(IntegrationOAuthFamily)
        .join(IntegrationGrant, and_(
            IntegrationGrant.id == IntegrationOAuthFamily.grant_id,
            IntegrationGrant.organization_id == IntegrationOAuthFamily.organization_id,
        ))
        .where(
            IntegrationOAuthFamily.organization_id == organization_id,
            _terminal_family_filter(current),
            ~active_lease,
        )
        .order_by(IntegrationOAuthFamily.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all())
    if not candidates:
        return 0

    family_ids = [row.id for row in candidates]
    grant_ids = [row.grant_id for row in candidates]

    # A public client may be shared by users/workspaces. Preserve it as long as
    # any family remains. The terminal family itself holds the client through
    # its 30-day recovery window; an already-old client may be deleted in the
    # same pass that reclaims its last family.
    session.execute(delete(IntegrationOAuthCode).where(
        IntegrationOAuthCode.organization_id == organization_id,
        IntegrationOAuthCode.family_id.in_(family_ids),
    ).execution_options(synchronize_session=False))
    session.execute(delete(IntegrationOAuthRefresh).where(
        IntegrationOAuthRefresh.organization_id == organization_id,
        IntegrationOAuthRefresh.family_id.in_(family_ids),
    ).execution_options(synchronize_session=False))
    session.execute(delete(IntegrationCredential).where(
        IntegrationCredential.organization_id == organization_id,
        IntegrationCredential.grant_id.in_(grant_ids),
        IntegrationCredential.kind == "oauth",
    ).execution_options(synchronize_session=False))
    session.execute(delete(IntegrationIdempotencyRecord).where(
        IntegrationIdempotencyRecord.organization_id == organization_id,
        IntegrationIdempotencyRecord.grant_id.in_(grant_ids),
    ).execution_options(synchronize_session=False))
    session.execute(delete(IntegrationRequestLease).where(
        IntegrationRequestLease.organization_id == organization_id,
        IntegrationRequestLease.grant_id.in_(grant_ids),
    ).execution_options(synchronize_session=False))

    # Preserve a terminal grant only when a private analysis draft still uses
    # it as provenance. Such a grant intentionally has no credential/family;
    # the settings projection renders it as expired/revoked, never as usable.
    retained_grants: set[str] = set()
    grant_objects: dict[str, IntegrationGrant] = {}
    for grant_id in grant_ids:
        grant = session.scalar(select(IntegrationGrant).where(
            IntegrationGrant.id == grant_id,
            IntegrationGrant.organization_id == organization_id,
        ).execution_options(populate_existing=True))
        if grant is None:
            continue
        grant_objects[grant_id] = grant
        family = next(row for row in candidates if row.grant_id == grant_id)
        current_revocation = grant.revoked_at or family.revoked_at
        if current_revocation is not None:
            grant.revoked_at = current_revocation
            grant.updated_at = current_revocation
        else:
            grant.updated_at = family.expires_at
        has_report = session.scalar(select(IntegrationAnalysisReport.id).where(
            IntegrationAnalysisReport.organization_id == organization_id,
            IntegrationAnalysisReport.source_grant_id == grant_id,
        ).limit(1)) is not None
        if has_report:
            retained_grants.add(grant_id)

    session.execute(delete(IntegrationOAuthFamily).where(
        IntegrationOAuthFamily.organization_id == organization_id,
        IntegrationOAuthFamily.id.in_(family_ids),
    ).execution_options(synchronize_session=False))
    removable_grants = set(grant_ids) - retained_grants
    if removable_grants:
        # These ORM instances were loaded to project terminal metadata. Detach
        # them before the explicit bulk DELETE so a later flush cannot try to
        # UPDATE rows that this same transaction has removed.
        for grant_id in removable_grants:
            grant = grant_objects.get(grant_id)
            if grant is not None:
                session.expunge(grant)
        session.execute(delete(IntegrationGrant).where(
            IntegrationGrant.organization_id == organization_id,
            IntegrationGrant.id.in_(removable_grants),
        ).execution_options(synchronize_session=False))

    return len(family_ids)


def cleanup_terminal_oauth_families_global(
    session: Session, *, now: datetime | None = None, limit: int = 1000,
) -> int:
    """Worker/public-registration wrapper with explicit per-workspace scope."""
    current = integration_now(now)
    remaining = max(1, min(limit, 1000))
    family_table = IntegrationOAuthFamily.__table__
    grant_table = IntegrationGrant.__table__
    due_organizations = tuple(session.execute(
        select(family_table.c.organization_id)
        .select_from(family_table.join(grant_table, and_(
            grant_table.c.id == family_table.c.grant_id,
            grant_table.c.organization_id == family_table.c.organization_id,
        )))
        .where(_terminal_family_filter(current))
        .distinct()
        .order_by(family_table.c.organization_id)
        .limit(100)
    ).scalars().all())

    previous_organization = session.info.get("greatsell_organization_id")
    previous_bypass = session.info.get("greatsell_skip_organization_scope")
    removed = 0
    try:
        clear_organization_context(session)
        for organization_id in due_organizations:
            if remaining <= 0:
                break
            set_organization_context(session, organization_id)
            count = cleanup_terminal_oauth_families(
                session,
                organization_id=organization_id,
                now=current,
                limit=remaining,
            )
            removed += count
            remaining -= count
    finally:
        clear_organization_context(session)
        if previous_organization:
            set_organization_context(session, str(previous_organization))
        if previous_bypass:
            session.info["greatsell_skip_organization_scope"] = previous_bypass
    return removed


def cleanup_unused_oauth_clients(
    session: Session, *, now: datetime | None = None, limit: int = 1000,
) -> int:
    """Reclaim abandoned or long-dormant clients with no family anywhere.

    Never-authorized registrations have a one-hour grace. A client with no
    remaining family is retained until 30 days after its last authorization;
    while a family exists, terminal-family retention independently keeps the
    client through the family's 30-day recovery window.
    """
    current = integration_now(now)
    unused_cutoff = current - UNUSED_CLIENT_RETENTION
    authorized_cutoff = current - AUTHORIZED_CLIENT_RETENTION
    batch = max(1, min(limit, 1000))
    clients = IntegrationOAuthClient.__table__
    consents = IntegrationOAuthConsent.__table__
    families = IntegrationOAuthFamily.__table__
    never_authorized = ~select(families.c.id).where(
        families.c.client_id == clients.c.id,
    ).exists()
    old_client = or_(
        and_(clients.c.last_authorized_at.is_(None), clients.c.created_at <= unused_cutoff),
        and_(clients.c.last_authorized_at.is_not(None), clients.c.last_authorized_at <= authorized_cutoff),
    )
    unused_clients = select(clients.c.id).where(old_client, never_authorized)

    # Consent-first, nonblocking locks avoid deadlocking consent approval, which
    # holds its consent before creating a family. A locked consent stays present
    # and therefore excludes its client from the orphan-only second phase.
    expired_consents = tuple(session.scalars(select(consents.c.id).where(
        consents.c.client_id.in_(unused_clients), consents.c.expires_at <= current,
    ).order_by(consents.c.expires_at, consents.c.id).limit(batch)
        .with_for_update(skip_locked=True)).all())
    removed = session.execute(delete(consents).where(consents.c.id.in_(expired_consents)))
    no_consent = ~select(consents.c.id).where(consents.c.client_id == clients.c.id).exists()
    orphan_ids = tuple(session.scalars(select(clients.c.id).where(
        old_client, never_authorized, no_consent,
    ).order_by(clients.c.created_at, clients.c.id).limit(batch)
        .with_for_update(skip_locked=True)).all())
    # Fresh statement after locking: recheck references committed since selection.
    deleted = session.execute(delete(clients).where(
        clients.c.id.in_(orphan_ids), old_client, never_authorized, no_consent,
    ))
    return max(0, removed.rowcount) + max(0, deleted.rowcount)
