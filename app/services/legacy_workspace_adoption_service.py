"""One-time adoption of the historical recruiting workspace.

The original workspace owns a large graph of tenant-scoped rows and some
flat, pre-tenant original-file keys. Moving that graph to a newly registered
organization would be fragile and unnecessary. This service instead transfers
the *membership boundary* to a verified named account while preserving every
historical organization identifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import Base
from app.models import (
    CandidateDataFileAccessGrant,
    CandidateDataRetentionPolicy,
    LegacyWorkspaceAdoption,
    MailboxOAuthConnectIntent,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationScoped,
    RecruitingAgentConversation,
    UserAccount,
)
from app.services.identity_service import (
    AuthPrincipal,
    LEGACY_MEMBERSHIP_ID,
    LEGACY_USER_ID,
    utcnow,
)
from app.services.platform_admin_service import record_platform_audit_event
from app.tenant_scope import LEGACY_ORGANIZATION_ID, set_organization_context


class LegacyWorkspaceAdoptionError(RuntimeError):
    """A stable, non-sensitive adoption failure safe for the account UI."""


@dataclass(frozen=True)
class LegacyWorkspaceAdoptionResult:
    """The post-adoption principal and whether this call changed state."""

    principal: AuthPrincipal
    adopted: bool


def _skip_scope(statement: object) -> object:
    """Use an explicit, fully-qualified control-plane query.

    The handover intentionally spans the source and target organizations. Each
    bypassed statement below includes a concrete organization predicate, so it
    cannot become an unbounded tenant query by accident.
    """

    return statement.execution_options(skip_organization_scope=True)


def _target_workspace_has_business_data(session: Session, *, organization_id: str) -> bool:
    """Return whether an auto-created target workspace has user-created data.

    Registration creates exactly one retention-policy row. That operational
    default is not user data and may be safely left in the suspended empty
    workspace. Every other current and future ``OrganizationScoped`` model is
    inspected through SQLAlchemy's mapper registry, which makes the guard
    fail closed when a new business root is introduced.
    """

    allowed_empty_workspace_models = {CandidateDataRetentionPolicy}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if (
            not isinstance(model, type)
            or not issubclass(model, OrganizationScoped)
            or model in allowed_empty_workspace_models
        ):
            continue
        organization_column = getattr(model, "organization_id", None)
        if organization_column is None:
            # Every scoped root must expose its ownership column. Treat an
            # unexpected model shape as non-empty rather than skipping it.
            return True
        found = session.scalar(
            _skip_scope(
                # Some scoped models intentionally use a composite primary
                # key rather than an ``id`` column. The concrete ownership
                # column is sufficient for this existence-only guard.
                select(organization_column)
                .where(organization_column == organization_id)
                .limit(1)
            )
        )
        if found is not None:
            return True
    return False


def _active_memberships_for_user(session: Session, *, user_id: str) -> list[OrganizationMembership]:
    return session.scalars(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
        .with_for_update()
    ).all()


def _active_memberships_for_organization(
    session: Session,
    *,
    organization_id: str,
) -> list[OrganizationMembership]:
    return session.scalars(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
        .with_for_update()
    ).all()


def _target_workspace_is_safe_to_suspend(
    session: Session,
    *,
    principal: AuthPrincipal,
    target_membership: OrganizationMembership,
    now: datetime,
) -> None:
    """Reject anything other than an untouched registration workspace."""

    if target_membership.organization_id == LEGACY_ORGANIZATION_ID:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_already_current")
    if target_membership.role != "admin":
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_admin_required")

    active_for_user = _active_memberships_for_user(session, user_id=principal.user.id)
    if len(active_for_user) != 1 or active_for_user[0].id != target_membership.id:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_account_unavailable")

    active_for_workspace = _active_memberships_for_organization(
        session,
        organization_id=target_membership.organization_id,
    )
    if len(active_for_workspace) != 1 or active_for_workspace[0].id != target_membership.id:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_target_workspace_not_empty")

    pending_invitation = session.scalar(
        select(OrganizationInvitation.id)
        .where(
            OrganizationInvitation.organization_id == target_membership.organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
        .limit(1)
        .with_for_update()
    )
    if pending_invitation is not None or _target_workspace_has_business_data(
        session,
        organization_id=target_membership.organization_id,
    ):
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_target_workspace_not_empty")


def _existing_adoption_principal(
    session: Session,
    *,
    adoption: LegacyWorkspaceAdoption,
    principal: AuthPrincipal,
) -> AuthPrincipal | None:
    """Resolve an idempotent retry only for the account that already adopted."""

    if adoption.target_user_id != principal.user.id:
        return None
    membership = session.scalar(
        select(OrganizationMembership)
        .options(
            joinedload(OrganizationMembership.organization).joinedload(Organization.plan),
            joinedload(OrganizationMembership.user),
        )
        .where(
            OrganizationMembership.id == adoption.target_membership_id,
            OrganizationMembership.user_id == principal.user.id,
            OrganizationMembership.organization_id == LEGACY_ORGANIZATION_ID,
            OrganizationMembership.is_active.is_(True),
        )
    )
    if membership is None or not membership.user.is_active:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_inconsistent")
    return AuthPrincipal(
        user=membership.user,
        membership=membership,
        organization=membership.organization,
        plan=membership.organization.plan,
    )


def legacy_workspace_adoption_available(session: Session) -> bool:
    """Return whether the one historical workspace is still eligible to adopt."""

    existing = session.scalar(
        _skip_scope(
            select(LegacyWorkspaceAdoption.id)
            .where(LegacyWorkspaceAdoption.source_organization_id == LEGACY_ORGANIZATION_ID)
            .limit(1)
        )
    )
    if existing is not None:
        return False
    legacy_membership = session.get(OrganizationMembership, LEGACY_MEMBERSHIP_ID)
    legacy_user = session.get(UserAccount, LEGACY_USER_ID)
    return bool(
        legacy_membership is not None
        and legacy_membership.is_active
        and legacy_user is not None
        and legacy_user.is_active
    )


def adopt_legacy_workspace(
    session: Session,
    *,
    principal: AuthPrincipal,
    request_id: str | None = None,
) -> LegacyWorkspaceAdoptionResult:
    """Hand the historical workspace to the current verified account.

    The caller owns the enclosing transaction. This method changes no record
    until every target-workspace precondition has passed, then lets a single
    database commit make the membership switch, revocations and audit event
    visible together.
    """

    if principal.legacy_compatibility or not principal.user.is_active:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_account_unavailable")
    if principal.user.email_verified_at is None:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_email_verification_required")
    if principal.role != "admin":
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_admin_required")

    now = utcnow()
    target_user = session.scalar(
        select(UserAccount).where(UserAccount.id == principal.user.id).with_for_update()
    )
    target_membership = session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.id == principal.membership.id,
            OrganizationMembership.user_id == principal.user.id,
            OrganizationMembership.organization_id == principal.organization_id,
            OrganizationMembership.is_active.is_(True),
        )
        .with_for_update()
    )
    target_organization = session.scalar(
        select(Organization)
        .where(Organization.id == principal.organization_id)
        .with_for_update()
    )
    legacy_organization = session.scalar(
        select(Organization)
        .options(joinedload(Organization.plan))
        .where(Organization.id == LEGACY_ORGANIZATION_ID)
        .with_for_update()
    )
    legacy_user = session.scalar(
        select(UserAccount).where(UserAccount.id == LEGACY_USER_ID).with_for_update()
    )
    legacy_membership = session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.id == LEGACY_MEMBERSHIP_ID,
            OrganizationMembership.organization_id == LEGACY_ORGANIZATION_ID,
            OrganizationMembership.user_id == LEGACY_USER_ID,
        )
        .with_for_update()
    )
    existing_adoption = session.scalar(
        _skip_scope(
            select(LegacyWorkspaceAdoption)
            .where(LegacyWorkspaceAdoption.source_organization_id == LEGACY_ORGANIZATION_ID)
            .with_for_update()
        )
    )

    if target_user is None or target_membership is None or target_organization is None:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_account_unavailable")
    if existing_adoption is not None:
        existing_principal = _existing_adoption_principal(
            session,
            adoption=existing_adoption,
            principal=principal,
        )
        if existing_principal is not None:
            return LegacyWorkspaceAdoptionResult(
                principal=existing_principal,
                adopted=False,
            )
        raise LegacyWorkspaceAdoptionError("legacy_workspace_already_adopted")
    if (
        legacy_organization is None
        or legacy_user is None
        or legacy_membership is None
        or not legacy_membership.is_active
        or not legacy_user.is_active
    ):
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_unavailable")

    # A user must not already have a historical-workspace membership, even an
    # inactive one.  The unique membership key would otherwise turn a
    # handover retry or a stale prior membership into a partially-applied
    # integrity error after other state has been changed.
    prior_target_legacy_membership = session.scalar(
        select(OrganizationMembership.id)
        .where(
            OrganizationMembership.organization_id == LEGACY_ORGANIZATION_ID,
            OrganizationMembership.user_id == target_user.id,
        )
        .with_for_update()
    )
    if prior_target_legacy_membership is not None:
        raise LegacyWorkspaceAdoptionError("legacy_workspace_adoption_account_unavailable")

    _target_workspace_is_safe_to_suspend(
        session,
        principal=principal,
        target_membership=target_membership,
        now=now,
    )

    # The original product was single-account. If a deployment was already
    # configured with additional members or invitations, automatically
    # expelling them would be destructive and ambiguous. Refuse that unusual
    # source state instead of guessing ownership; a platform operator can
    # resolve it explicitly before retrying the handover.
    source_active_memberships = _active_memberships_for_organization(
        session,
        organization_id=LEGACY_ORGANIZATION_ID,
    )
    source_pending_invitations = session.scalars(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == LEGACY_ORGANIZATION_ID,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
        .with_for_update()
    ).all()
    additional_source_memberships = [
        membership
        for membership in source_active_memberships
        if membership.id != LEGACY_MEMBERSHIP_ID
    ]
    if additional_source_memberships or source_pending_invitations:
        raise LegacyWorkspaceAdoptionError(
            "legacy_workspace_adoption_source_workspace_not_ready"
        )

    target_membership.is_active = False
    target_organization.plan_status = "suspended"
    legacy_organization.name = target_organization.name

    new_membership = OrganizationMembership(
        organization_id=LEGACY_ORGANIZATION_ID,
        user_id=target_user.id,
        role="admin",
        is_active=True,
    )
    session.add(new_membership)
    session.flush()

    legacy_membership.is_active = False
    legacy_user.is_active = False
    legacy_user.auth_session_version += 1
    target_user.auth_session_version += 1

    # Callback correlation is intentionally tied to the user and membership
    # that started it. Consume every unfinished legacy or soon-to-be-inactive
    # target-workspace handoff before those identities are retired.
    session.execute(
        _skip_scope(
            update(MailboxOAuthConnectIntent)
            .where(
                MailboxOAuthConnectIntent.consumed_at.is_(None),
                or_(
                    MailboxOAuthConnectIntent.organization_id == LEGACY_ORGANIZATION_ID,
                    (
                        MailboxOAuthConnectIntent.organization_id
                        == target_organization.id
                    )
                    & (MailboxOAuthConnectIntent.user_id == target_user.id)
                    & (MailboxOAuthConnectIntent.membership_id == target_membership.id),
                ),
            )
            .values(consumed_at=now)
        )
    )
    session.execute(
        _skip_scope(
            update(CandidateDataFileAccessGrant)
            .where(
                CandidateDataFileAccessGrant.organization_id == LEGACY_ORGANIZATION_ID,
                CandidateDataFileAccessGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    )
    session.execute(
        _skip_scope(
            update(RecruitingAgentConversation)
            .where(
                RecruitingAgentConversation.organization_id == LEGACY_ORGANIZATION_ID,
                RecruitingAgentConversation.owner_user_id == LEGACY_USER_ID,
            )
            .values(owner_user_id=target_user.id, updated_at=now)
        )
    )

    adoption = LegacyWorkspaceAdoption(
        source_organization_id=LEGACY_ORGANIZATION_ID,
        target_previous_organization_id=target_organization.id,
        target_user_id=target_user.id,
        target_membership_id=new_membership.id,
        retired_membership_id=legacy_membership.id,
        adopted_at=now,
    )
    session.add(adoption)
    try:
        session.flush()
    except IntegrityError as exc:
        # The source-organization uniqueness constraint is the final guard if
        # two browsers race past the initial read on a database with weak row
        # locks. The enclosing handler rolls back before replying.
        raise LegacyWorkspaceAdoptionError("legacy_workspace_already_adopted") from exc

    record_platform_audit_event(
        session,
        actor_user_id=target_user.id,
        action="legacy_workspace.adopted",
        target_type="organization",
        target_id=LEGACY_ORGANIZATION_ID,
        organization_id=LEGACY_ORGANIZATION_ID,
        reason="historical_workspace_handover",
        before_state={
            "source_organization_id": LEGACY_ORGANIZATION_ID,
            "source_membership_id": legacy_membership.id,
            "source_active_membership_count": len(source_active_memberships),
            "source_pending_invitation_count": len(source_pending_invitations),
            "target_previous_organization_id": target_organization.id,
            "target_user_id": target_user.id,
        },
        after_state={
            "target_membership_id": new_membership.id,
            "source_membership_active": False,
            "target_previous_workspace_status": "suspended",
        },
        request_id=request_id,
    )

    set_organization_context(session, LEGACY_ORGANIZATION_ID)
    return LegacyWorkspaceAdoptionResult(
        principal=AuthPrincipal(
            user=target_user,
            membership=new_membership,
            organization=legacy_organization,
            plan=legacy_organization.plan,
        ),
        adopted=True,
    )
