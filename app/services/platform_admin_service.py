"""Platform administrator control-plane queries and audited mutations.

This module is the only HTTP-facing service allowed to perform cross-workspace
reporting.  Its response contracts deliberately exclude candidates, resume
content, source files, prompts, model outputs, and credential material.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AiRun,
    Job,
    MailboxConfig,
    Organization,
    OrganizationMembership,
    PlatformAuditEvent,
    ProductPlan,
    Resume,
    UserAccount,
)
from app.schemas import (
    PlatformAuditEventListResponse,
    PlatformAuditEventResponse,
    PlatformDashboardResponse,
    PlatformOrganizationDetailResponse,
    PlatformOrganizationListItem,
    PlatformOrganizationListResponse,
    PlatformOrganizationMemberResponse,
    PlatformOrganizationPatch,
    PlatformUserDetailResponse,
    PlatformUserListItem,
    PlatformUserListResponse,
    PlatformUserMembershipResponse,
    PlatformUserPatch,
)


class PlatformAdminServiceError(RuntimeError):
    """Stable, non-sensitive failures safe for platform API responses."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _platform_statement(statement: object) -> object:
    """Opt one explicit control-plane statement out of tenant loader criteria."""

    return statement.execution_options(skip_organization_scope=True)


def _count(session: Session, statement: object) -> int:
    return int(session.scalar(_platform_statement(statement)) or 0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _effective_plan_status(organization: Organization, *, now: datetime | None = None) -> str:
    current_time = now or _utcnow()
    trial_ends_at = _as_utc(organization.trial_ends_at)
    if (
        organization.plan_status == "trial"
        and trial_ends_at is not None
        and trial_ends_at <= current_time
    ):
        return "expired"
    return organization.plan_status


def _effective_plan_status_expression(now: datetime) -> object:
    return case(
        (
            and_(
                Organization.plan_status == "trial",
                Organization.trial_ends_at.is_not(None),
                Organization.trial_ends_at <= now,
            ),
            "expired",
        ),
        else_=Organization.plan_status,
    )


def _normalized_reason(value: str, *, default: str | None = None) -> str:
    normalized = value.strip() if value else ""
    if not normalized:
        if default is not None:
            return default
        raise PlatformAdminServiceError("platform_audit_reason_required")
    return normalized[:500]


def record_platform_audit_event(
    session: Session,
    *,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    before_state: dict[str, object] | None = None,
    after_state: dict[str, object] | None = None,
    organization_id: str | None = None,
    request_id: str | None = None,
) -> PlatformAuditEvent:
    """Append one safe audit record in the caller's business transaction."""

    event = PlatformAuditEvent(
        actor_user_id=actor_user_id,
        action=action[:100],
        target_type=target_type[:64],
        target_id=target_id[:128],
        organization_id=organization_id,
        reason=_normalized_reason(reason),
        before_json=dict(before_state or {}),
        after_json=dict(after_state or {}),
        request_id=request_id[:128] if request_id else None,
    )
    session.add(event)
    session.flush()
    return event


def product_plan_snapshot(plan: ProductPlan) -> dict[str, object]:
    return {
        "code": plan.code,
        "name": plan.name,
        "monthly_price_cents": plan.monthly_price_cents,
        "currency": plan.currency,
        "trial_days": plan.trial_days,
        "feature_flags": dict(plan.feature_flags or {}),
        "is_active": plan.is_active,
        "is_available_for_signup": plan.is_available_for_signup,
        "is_default_trial": plan.is_default_trial,
        "sort_order": plan.sort_order,
    }


def organization_control_snapshot(organization: Organization) -> dict[str, object]:
    plan = organization.plan
    return {
        "organization_id": organization.id,
        "name": organization.name,
        "plan_code": plan.code if plan is not None else None,
        "plan_status": organization.plan_status,
        "trial_started_at": _iso(organization.trial_started_at),
        "trial_ends_at": _iso(organization.trial_ends_at),
    }


def user_control_snapshot(user: UserAccount) -> dict[str, object]:
    return {
        "user_id": user.id,
        "is_active": user.is_active,
        "is_platform_admin": user.is_platform_admin,
    }


def get_platform_dashboard(session: Session) -> PlatformDashboardResponse:
    now = _utcnow()
    effective_status = _effective_plan_status_expression(now)
    status_rows = session.execute(
        _platform_statement(
            select(effective_status, func.count(Organization.id)).group_by(effective_status)
        )
    ).all()
    statuses = {str(status): int(count or 0) for status, count in status_rows}
    return PlatformDashboardResponse(
        generated_at=now,
        organizations_total=_count(session, select(func.count(Organization.id))),
        organizations_by_status=statuses,
        trials_expiring_within_7_days=_count(
            session,
            select(func.count(Organization.id)).where(
                Organization.plan_status == "trial",
                Organization.trial_ends_at.is_not(None),
                Organization.trial_ends_at >= now,
                Organization.trial_ends_at <= now + timedelta(days=7),
            ),
        ),
        users_total=_count(session, select(func.count(UserAccount.id))),
        users_active=_count(
            session,
            select(func.count(UserAccount.id)).where(UserAccount.is_active.is_(True)),
        ),
        users_verified=_count(
            session,
            select(func.count(UserAccount.id)).where(
                UserAccount.email_verified_at.is_not(None)
            ),
        ),
        resumes_total=_count(session, select(func.count(Resume.id))),
        jobs_total=_count(session, select(func.count(Job.id))),
        mailboxes_total=_count(session, select(func.count(MailboxConfig.id))),
        ai_runs_total=_count(session, select(func.count(AiRun.id))),
        ai_runs_succeeded=_count(
            session,
            select(func.count(AiRun.id)).where(AiRun.status == "succeeded"),
        ),
        ai_runs_failed=_count(
            session,
            select(func.count(AiRun.id)).where(AiRun.status == "failed"),
        ),
        ai_cost_cny_micros=_count(
            session,
            select(func.coalesce(func.sum(AiRun.total_cost_reporting_micros), 0)).where(
                AiRun.reporting_currency == "CNY"
            ),
        ),
        ai_cost_unavailable_runs=_count(
            session,
            select(func.count(AiRun.id)).where(AiRun.cost_status == "unavailable"),
        ),
    )


def _organization_filters(
    *,
    search: str | None,
    plan_code: str | None,
    plan_status: str | None,
    now: datetime,
) -> list[object]:
    filters: list[object] = []
    if search and search.strip():
        pattern = f"%{search.strip().lower()}%"
        filters.append(
            or_(
                func.lower(Organization.name).like(pattern),
                func.lower(Organization.id).like(pattern),
            )
        )
    if plan_code:
        filters.append(ProductPlan.code == plan_code.strip().lower())
    if plan_status:
        filters.append(_effective_plan_status_expression(now) == plan_status)
    return filters


def list_platform_organizations(
    session: Session,
    *,
    search: str | None,
    plan_code: str | None,
    plan_status: str | None,
    limit: int,
    offset: int,
) -> PlatformOrganizationListResponse:
    now = _utcnow()
    filters = _organization_filters(
        search=search,
        plan_code=plan_code,
        plan_status=plan_status,
        now=now,
    )
    member_count = func.count(OrganizationMembership.id).label("member_count")
    active_member_count = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        OrganizationMembership.is_active.is_(True),
                        UserAccount.is_active.is_(True),
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    ).label("active_member_count")
    statement = (
        select(Organization, ProductPlan, member_count, active_member_count)
        .outerjoin(ProductPlan, ProductPlan.id == Organization.plan_id)
        .outerjoin(
            OrganizationMembership,
            OrganizationMembership.organization_id == Organization.id,
        )
        .outerjoin(UserAccount, UserAccount.id == OrganizationMembership.user_id)
        .where(*filters)
        .group_by(Organization.id, ProductPlan.id)
        .order_by(Organization.created_at.desc(), Organization.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = session.execute(_platform_statement(statement)).all()
    total_statement = (
        select(func.count(Organization.id))
        .select_from(Organization)
        .outerjoin(ProductPlan, ProductPlan.id == Organization.plan_id)
        .where(*filters)
    )
    items = [
        _organization_list_item(
            organization,
            plan,
            member_count=int(total_members or 0),
            active_member_count=int(active_members or 0),
        )
        for organization, plan, total_members, active_members in rows
    ]
    return PlatformOrganizationListResponse(
        items=items,
        total=_count(session, total_statement),
        limit=limit,
        offset=offset,
    )


def _organization_list_item(
    organization: Organization,
    plan: ProductPlan | None,
    *,
    member_count: int,
    active_member_count: int,
) -> PlatformOrganizationListItem:
    return PlatformOrganizationListItem(
        organization_id=organization.id,
        name=organization.name,
        plan_id=organization.plan_id,
        plan_code=plan.code if plan is not None else None,
        plan_name=plan.name if plan is not None else None,
        plan_status=_effective_plan_status(organization),
        trial_started_at=organization.trial_started_at,
        trial_ends_at=organization.trial_ends_at,
        member_count=member_count,
        active_member_count=active_member_count,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


def _load_organization(session: Session, organization_id: str) -> Organization:
    organization = session.scalar(
        _platform_statement(
            select(Organization)
            .options(joinedload(Organization.plan))
            .where(Organization.id == organization_id)
        )
    )
    if organization is None:
        raise PlatformAdminServiceError("platform_organization_not_found")
    return organization


def get_platform_organization(
    session: Session,
    *,
    organization_id: str,
) -> PlatformOrganizationDetailResponse:
    organization = _load_organization(session, organization_id)
    member_rows = session.execute(
        _platform_statement(
            select(OrganizationMembership, UserAccount)
            .join(UserAccount, UserAccount.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == organization.id)
            .order_by(OrganizationMembership.created_at, OrganizationMembership.id)
        )
    ).all()
    members = [
        PlatformOrganizationMemberResponse(
            membership_id=membership.id,
            user_id=user.id,
            full_name=user.full_name,
            email=user.email,
            role=membership.role,
            is_active=membership.is_active,
            user_is_active=user.is_active,
            email_verified=user.email_verified_at is not None,
            last_login_at=user.last_login_at,
            joined_at=membership.created_at,
        )
        for membership, user in member_rows
    ]
    plan = organization.plan
    base = _organization_list_item(
        organization,
        plan,
        member_count=len(members),
        active_member_count=sum(
            1 for member in members if member.is_active and member.user_is_active
        ),
    )
    return PlatformOrganizationDetailResponse(
        **base.model_dump(),
        resume_count=_count(
            session,
            select(func.count(Resume.id)).where(Resume.organization_id == organization.id),
        ),
        job_count=_count(
            session,
            select(func.count(Job.id)).where(Job.organization_id == organization.id),
        ),
        mailbox_count=_count(
            session,
            select(func.count(MailboxConfig.id)).where(
                MailboxConfig.organization_id == organization.id
            ),
        ),
        ai_run_count=_count(
            session,
            select(func.count(AiRun.id)).where(AiRun.organization_id == organization.id),
        ),
        members=members,
    )


def patch_platform_organization(
    session: Session,
    *,
    organization_id: str,
    payload: PlatformOrganizationPatch,
    actor_user_id: str,
    request_id: str | None,
) -> PlatformOrganizationDetailResponse:
    organization = _load_organization(session, organization_id)
    before = organization_control_snapshot(organization)
    fields = payload.model_fields_set
    requested_trial_end = (
        _as_utc(payload.trial_ends_at) if "trial_ends_at" in fields else None
    )
    current_trial_end = _as_utc(organization.trial_ends_at)
    requires_confirmation = (
        (
            "plan_status" in fields
            and payload.plan_status in {"expired", "suspended"}
            and payload.plan_status != organization.plan_status
        )
        or (
            "trial_ends_at" in fields
            and requested_trial_end is not None
            and current_trial_end is not None
            and requested_trial_end < current_trial_end
        )
    )
    if requires_confirmation and payload.confirmation_name != organization.name:
        raise PlatformAdminServiceError("platform_organization_confirmation_required")
    if "name" in fields:
        if payload.name is None or not payload.name.strip():
            raise PlatformAdminServiceError("platform_organization_name_invalid")
        organization.name = payload.name.strip()
    if "plan_code" in fields:
        if payload.plan_code is None:
            raise PlatformAdminServiceError("platform_plan_not_found")
        plan = session.scalar(
            _platform_statement(
                select(ProductPlan).where(
                    ProductPlan.code == payload.plan_code.strip().lower(),
                    ProductPlan.is_active.is_(True),
                )
            )
        )
        if plan is None:
            raise PlatformAdminServiceError("platform_plan_not_found")
        organization.plan = plan
    if "plan_status" in fields:
        if payload.plan_status is None:
            raise PlatformAdminServiceError("platform_plan_status_invalid")
        organization.plan_status = payload.plan_status
    if "trial_ends_at" in fields:
        organization.trial_ends_at = requested_trial_end
    session.flush()
    after = organization_control_snapshot(organization)
    record_platform_audit_event(
        session,
        actor_user_id=actor_user_id,
        action="organization.updated",
        target_type="organization",
        target_id=organization.id,
        organization_id=organization.id,
        reason=payload.reason,
        before_state=before,
        after_state=after,
        request_id=request_id,
    )
    return get_platform_organization(session, organization_id=organization.id)


def _user_filters(*, search: str | None, is_active: bool | None) -> list[object]:
    filters: list[object] = []
    if search and search.strip():
        pattern = f"%{search.strip().lower()}%"
        filters.append(
            or_(
                func.lower(UserAccount.full_name).like(pattern),
                func.lower(UserAccount.email).like(pattern),
                func.lower(UserAccount.id).like(pattern),
            )
        )
    if is_active is not None:
        filters.append(UserAccount.is_active.is_(is_active))
    return filters


def list_platform_users(
    session: Session,
    *,
    search: str | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> PlatformUserListResponse:
    filters = _user_filters(search=search, is_active=is_active)
    membership_count = func.count(OrganizationMembership.id).label("membership_count")
    statement = (
        select(UserAccount, membership_count)
        .outerjoin(
            OrganizationMembership,
            OrganizationMembership.user_id == UserAccount.id,
        )
        .where(*filters)
        .group_by(UserAccount.id)
        .order_by(UserAccount.created_at.desc(), UserAccount.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = session.execute(_platform_statement(statement)).all()
    items = [
        _user_list_item(user, membership_count=int(count or 0))
        for user, count in rows
    ]
    return PlatformUserListResponse(
        items=items,
        total=_count(
            session,
            select(func.count(UserAccount.id)).where(*filters),
        ),
        limit=limit,
        offset=offset,
    )


def _user_list_item(user: UserAccount, *, membership_count: int) -> PlatformUserListItem:
    return PlatformUserListItem(
        user_id=user.id,
        full_name=user.full_name,
        email=user.email,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        email_verified=user.email_verified_at is not None,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        membership_count=membership_count,
    )


def _load_user(session: Session, user_id: str) -> UserAccount:
    user = session.scalar(
        _platform_statement(select(UserAccount).where(UserAccount.id == user_id))
    )
    if user is None:
        raise PlatformAdminServiceError("platform_user_not_found")
    return user


def get_platform_user(
    session: Session,
    *,
    user_id: str,
) -> PlatformUserDetailResponse:
    user = _load_user(session, user_id)
    rows = session.execute(
        _platform_statement(
            select(OrganizationMembership, Organization)
            .join(Organization, Organization.id == OrganizationMembership.organization_id)
            .where(OrganizationMembership.user_id == user.id)
            .order_by(OrganizationMembership.created_at, OrganizationMembership.id)
        )
    ).all()
    memberships = [
        PlatformUserMembershipResponse(
            membership_id=membership.id,
            organization_id=organization.id,
            organization_name=organization.name,
            role=membership.role,
            is_active=membership.is_active,
            joined_at=membership.created_at,
        )
        for membership, organization in rows
    ]
    base = _user_list_item(user, membership_count=len(memberships))
    return PlatformUserDetailResponse(**base.model_dump(), memberships=memberships)


def patch_platform_user(
    session: Session,
    *,
    user_id: str,
    payload: PlatformUserPatch,
    actor_user_id: str,
    request_id: str | None,
) -> PlatformUserDetailResponse:
    user = _load_user(session, user_id)
    if not payload.is_active:
        if user.id == actor_user_id:
            raise PlatformAdminServiceError("platform_admin_self_deactivation_forbidden")
        if user.is_platform_admin:
            raise PlatformAdminServiceError("platform_admin_deactivation_forbidden")
    before = user_control_snapshot(user)
    user.is_active = payload.is_active
    session.flush()
    after = user_control_snapshot(user)
    organization_id = session.scalar(
        _platform_statement(
            select(OrganizationMembership.organization_id)
            .where(OrganizationMembership.user_id == user.id)
            .order_by(OrganizationMembership.created_at)
            .limit(1)
        )
    )
    record_platform_audit_event(
        session,
        actor_user_id=actor_user_id,
        action="user.activation_changed",
        target_type="user",
        target_id=user.id,
        organization_id=organization_id,
        reason=payload.reason,
        before_state=before,
        after_state=after,
        request_id=request_id,
    )
    return get_platform_user(session, user_id=user.id)


def list_platform_audit_events(
    session: Session,
    *,
    actor_user_id: str | None,
    action: str | None,
    target_type: str | None,
    organization_id: str | None,
    created_at_from: datetime | None,
    created_at_to: datetime | None,
    limit: int,
    offset: int,
) -> PlatformAuditEventListResponse:
    created_at_from = _as_utc(created_at_from)
    created_at_to = _as_utc(created_at_to)
    if created_at_from and created_at_to and created_at_from > created_at_to:
        raise PlatformAdminServiceError("platform_audit_date_range_invalid")
    filters: list[object] = []
    if actor_user_id:
        filters.append(PlatformAuditEvent.actor_user_id == actor_user_id)
    if action:
        filters.append(PlatformAuditEvent.action == action)
    if target_type:
        filters.append(PlatformAuditEvent.target_type == target_type)
    if organization_id:
        filters.append(PlatformAuditEvent.organization_id == organization_id)
    if created_at_from:
        filters.append(PlatformAuditEvent.created_at >= created_at_from)
    if created_at_to:
        filters.append(PlatformAuditEvent.created_at <= created_at_to)
    events = list(
        session.scalars(
            _platform_statement(
                select(PlatformAuditEvent)
                .where(*filters)
                .order_by(PlatformAuditEvent.created_at.desc(), PlatformAuditEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
    )
    return PlatformAuditEventListResponse(
        items=[_audit_response(event) for event in events],
        total=_count(
            session,
            select(func.count(PlatformAuditEvent.id)).where(*filters),
        ),
        limit=limit,
        offset=offset,
    )


def _audit_response(event: PlatformAuditEvent) -> PlatformAuditEventResponse:
    return PlatformAuditEventResponse(
        audit_id=event.id,
        actor_user_id=event.actor_user_id,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        organization_id=event.organization_id,
        reason=event.reason,
        before_state=dict(event.before_json or {}),
        after_state=dict(event.after_json or {}),
        request_id=event.request_id,
        created_at=event.created_at,
    )
