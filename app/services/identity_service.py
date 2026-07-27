"""Account, workspace, membership, trial, and plan domain logic.

HTTP handlers keep only transport concerns.  This module owns password
verification, session identity resolution, registrations, invitations, and
platform-managed plan changes so each path applies the same rules.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload

from app.models import (
    CandidateDataRetentionPolicy,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    EmailVerificationToken,
    PasswordResetToken,
    ProductPlan,
    UserAccount,
)
from app.schemas import (
    AuthOrganizationResponse,
    AuthPlanResponse,
    AuthRegistration,
    AuthSession,
    AuthUserResponse,
    OrganizationInvitationAccept,
    OrganizationInvitationCreate,
    OrganizationInvitationResponse,
    OrganizationPlanAssign,
    OrganizationPlanResponse,
    ProductPlanResponse,
    ProductPlanUpdate,
    RegistrationOfferResponse,
    TrialAccessResponse,
)
from app.tenant_scope import (
    LEGACY_ORGANIZATION_ID,
    clear_organization_context,
    set_organization_context,
)
from app.services.trial_quota_service import TRIAL_LLM_CALL_LIMIT, trial_llm_call_snapshot


LEGACY_USER_ID = "00000000-0000-4000-8000-000000000002"
LEGACY_MEMBERSHIP_ID = "00000000-0000-4000-8000-000000000003"
PLAN_BASIC_ID = "00000000-0000-4000-8000-000000000101"
PLAN_ADVANCED_ID = "00000000-0000-4000-8000-000000000102"
PLAN_PROFESSIONAL_ID = "00000000-0000-4000-8000-000000000103"

PASSWORD_ALGORITHM = "scrypt"
PASSWORD_N = 2**14
PASSWORD_R = 8
PASSWORD_P = 1
PASSWORD_DKLEN = 64
PASSWORD_MIN_LENGTH = 8
# Public, fixed scrypt material used only to equalize unknown/inactive login
# work with a normal invalid-password check. It is deliberately not a secret,
# cannot authenticate any account, and uses the same cost parameters as a
# freshly created password hash. Keep it valid when changing PASSWORD_*.
_LOGIN_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$iZ7two2EVlo-4Kl43l_Opw$"
    "hVfkGHScvqF9wGt_799n6z1zSe99k1yIYswlD0axbjzXNF-qYl8-3NGyOECXv_GpcNz8Def00AgWmaRiEhWccA"
)


class IdentityServiceError(RuntimeError):
    """A stable, non-sensitive identity domain failure."""


@dataclass(frozen=True)
class AuthPrincipal:
    user: UserAccount
    membership: OrganizationMembership
    organization: Organization
    plan: ProductPlan | None
    legacy_compatibility: bool = False

    @property
    def role(self) -> str:
        return self.membership.role

    @property
    def organization_id(self) -> str:
        return self.organization.id

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user.is_platform_admin)

    @property
    def email_verified(self) -> bool:
        # The historical password-only entry point is deliberately confined
        # to its pre-existing legacy workspace.  It cannot be used to enter a
        # newly registered tenant, so it remains compatible without a real
        # deliverable email address.
        return self.legacy_compatibility or self.user.email_verified_at is not None


@dataclass(frozen=True)
class IssuedPasswordReset:
    """One in-memory recovery secret paired with its durable token row.

    The raw token is intentionally short-lived: callers must encrypt it into
    the transactional outbox in the same database transaction and must never
    serialize or log it. The outbox references the token by ID only.
    """

    token: str
    password_reset_token_id: str
    user_id: str
    recipient: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def normalize_email(value: str) -> tuple[str, str]:
    email = value.strip()
    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise IdentityServiceError("invalid_email")
    if len(email) > 320 or any(character.isspace() for character in email):
        raise IdentityServiceError("invalid_email")
    return email, email.casefold()


def _normalize_text(value: str, *, code: str, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum:
        raise IdentityServiceError(code)
    return normalized


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise IdentityServiceError("password_too_short")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=PASSWORD_N,
        r=PASSWORD_R,
        p=PASSWORD_P,
        dklen=PASSWORD_DKLEN,
    )
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"{PASSWORD_ALGORITHM}${PASSWORD_N}${PASSWORD_R}${PASSWORD_P}${encode(salt)}${encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = password_hash.split("$")
        if algorithm != PASSWORD_ALGORITHM:
            return False
        add_padding = lambda value: value + "=" * (-len(value) % 4)
        salt = base64.urlsafe_b64decode(add_padding(raw_salt))
        expected = base64.urlsafe_b64decode(add_padding(raw_digest))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _feature_flags(plan: ProductPlan | None) -> dict[str, bool]:
    raw_flags = plan.feature_flags if plan else {}
    if not isinstance(raw_flags, dict):
        return {}
    return {str(key): bool(value) for key, value in raw_flags.items()}


def _seed_plan_rows(session: Session) -> None:
    shared_flags = {
        "resume_library": True,
        "candidate_filtering": True,
        "ai_scoring": True,
        "ai_summary": True,
        "jd_matching": True,
        "recruiting_agent": True,
    }
    defaults = (
        (
            PLAN_BASIC_ID,
            "basic",
            "\u57fa\u7840\u7248",
            {**shared_flags, "mailbox_import": False, "ai_jd_generation": False, "interview_questions": False, "interview_records": False, "hrbp_reference": False},
            False,
            10,
        ),
        (
            PLAN_ADVANCED_ID,
            "advanced",
            "\u8fdb\u9636\u7248",
            {**shared_flags, "mailbox_import": True, "ai_jd_generation": True, "interview_questions": False, "interview_records": False, "hrbp_reference": False},
            True,
            20,
        ),
        (
            PLAN_PROFESSIONAL_ID,
            "professional",
            "\u4e13\u4e1a\u7248",
            # Professional-only features remain false until their actual C
            # phase endpoints and evidence model are implemented.
            {**shared_flags, "mailbox_import": True, "ai_jd_generation": True, "interview_questions": False, "interview_records": False, "hrbp_reference": False},
            False,
            30,
        ),
    )
    existing_by_code = {
        plan.code: plan
        for plan in session.scalars(select(ProductPlan)).all()
    }
    for plan_id, code, name, flags, is_default, sort_order in defaults:
        if code in existing_by_code:
            continue
        session.add(
            ProductPlan(
                id=plan_id,
                code=code,
                name=name,
                monthly_price_cents=0,
                currency="CNY",
                trial_days=30,
                feature_flags=flags,
                is_active=True,
                is_available_for_signup=True,
                is_default_trial=is_default,
                sort_order=sort_order,
            )
        )
    session.flush()


def ensure_identity_bootstrap(session: Session) -> None:
    """Idempotently seed plans and a safe owner for pre-tenant records."""

    _seed_plan_rows(session)
    advanced = session.scalar(select(ProductPlan).where(ProductPlan.code == "advanced"))
    if advanced is None:
        raise RuntimeError("default_advanced_plan_missing")

    legacy_organization = session.get(Organization, LEGACY_ORGANIZATION_ID)
    if legacy_organization is None:
        legacy_organization = Organization(
            id=LEGACY_ORGANIZATION_ID,
            name="Legacy workspace",
            plan=advanced,
            plan_status="active",
        )
        session.add(legacy_organization)

    legacy_user = session.get(UserAccount, LEGACY_USER_ID)
    if legacy_user is None:
        legacy_user = UserAccount(
            id=LEGACY_USER_ID,
            email="legacy-admin@system.invalid",
            email_key="legacy-admin@system.invalid",
            full_name="Legacy Administrator",
            password_hash="!legacy-configuration-authentication!",
            is_active=True,
            is_platform_admin=True,
        )
        session.add(legacy_user)
    elif not legacy_user.is_platform_admin:
        legacy_user.is_platform_admin = True
    if legacy_user.email_verified_at is None:
        # This identity is a compatibility bridge, not a deliverable inbox.
        # Marking it verified preserves its existing access after the email
        # gate is introduced and mirrors the one-time production migration.
        legacy_user.email_verified_at = utcnow()

    session.flush()
    membership = session.get(OrganizationMembership, LEGACY_MEMBERSHIP_ID)
    if membership is None:
        membership = session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == LEGACY_ORGANIZATION_ID,
                OrganizationMembership.user_id == LEGACY_USER_ID,
            )
        )
    if membership is None:
        session.add(
            OrganizationMembership(
                id=LEGACY_MEMBERSHIP_ID,
                organization_id=LEGACY_ORGANIZATION_ID,
                user_id=LEGACY_USER_ID,
                role="admin",
                is_active=True,
            )
        )
    # ``create_all`` development databases do not execute Alembic's data
    # seed, so give the compatibility workspace the same explicit manual
    # policy as migrated and newly registered workspaces.
    if session.scalar(
        select(CandidateDataRetentionPolicy.id).where(
            CandidateDataRetentionPolicy.organization_id == LEGACY_ORGANIZATION_ID
        )
    ) is None:
        session.add(
            CandidateDataRetentionPolicy(
                organization_id=LEGACY_ORGANIZATION_ID,
                mode="manual",
                retention_days=None,
                version=1,
            )
        )


def _membership_with_context(
    session: Session,
    *,
    membership_id: str,
    user_id: str,
    organization_id: str,
) -> AuthPrincipal | None:
    membership = session.scalar(
        select(OrganizationMembership)
        .options(
            joinedload(OrganizationMembership.user),
            joinedload(OrganizationMembership.organization).joinedload(Organization.plan),
        )
        .where(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    if membership is None or not membership.user.is_active:
        return None
    return AuthPrincipal(
        user=membership.user,
        membership=membership,
        organization=membership.organization,
        plan=membership.organization.plan,
    )


def legacy_principal(session: Session) -> AuthPrincipal:
    principal = _membership_with_context(
        session,
        membership_id=LEGACY_MEMBERSHIP_ID,
        user_id=LEGACY_USER_ID,
        organization_id=LEGACY_ORGANIZATION_ID,
    )
    if principal is None:
        raise RuntimeError("legacy_identity_not_initialized")
    return AuthPrincipal(
        user=principal.user,
        membership=principal.membership,
        organization=principal.organization,
        plan=principal.plan,
        legacy_compatibility=True,
    )


def legacy_principal_from_session(
    session: Session,
    values: dict[str, object],
) -> AuthPrincipal | None:
    """Resolve an old boolean-only legacy cookie with version revocation.

    Before tenant identity existed, the legacy workspace wrote only an
    authenticated flag.  Treat that historical shape as session version 1 so
    it continues to work after rollout, but no longer bypasses account-wide
    session revocation after a password reset.
    """

    if values.get("resume_v3_authenticated") is not True:
        return None
    raw_version = values.get("resume_v3_auth_session_version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
        or raw_version < 1
    ):
        return None
    principal = legacy_principal(session)
    if principal.user.auth_session_version != raw_version:
        return None
    return principal


def principal_from_session(session: Session, values: dict[str, object]) -> AuthPrincipal | None:
    user_id = values.get("resume_v3_user_id")
    organization_id = values.get("resume_v3_organization_id")
    membership_id = values.get("resume_v3_membership_id")
    if not all(isinstance(value, str) and value for value in (user_id, organization_id, membership_id)):
        return None
    # Sessions issued before auth-session versioning intentionally map to the
    # initial version.  That keeps the legacy migration bridge working, while
    # a password reset increments the account version and invalidates them.
    raw_version = values.get("resume_v3_auth_session_version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
        or raw_version < 1
    ):
        return None
    principal = _membership_with_context(
        session,
        membership_id=membership_id,
        user_id=user_id,
        organization_id=organization_id,
    )
    if principal is None or principal.user.auth_session_version != raw_version:
        return None
    return principal


def principal_from_mailbox_oauth_callback(
    session: Session,
    *,
    user_id: str,
    organization_id: str,
    membership_id: str,
    auth_session_version: int,
    legacy_compatibility: bool = False,
) -> AuthPrincipal | None:
    """Rebuild a revoked-session-safe principal for one mailbox OAuth callback.

    OAuth providers return to the application from another site, so the main
    strict session cookie is intentionally unavailable on that first request.
    The callback supplies only values from a short-lived, signed correlation
    cookie and a matching persisted intent.  This helper still reloads the
    membership from the database and verifies the current account session
    version, active membership and active user before the callback may issue a
    fresh browser session.

    ``legacy_compatibility`` is accepted only for the deterministic historical
    workspace used by local/transition deployments.  Callers remain
    responsible for allowing that bridge in their deployment settings.
    """

    if isinstance(auth_session_version, bool) or not isinstance(auth_session_version, int):
        return None
    if auth_session_version < 1:
        return None

    if legacy_compatibility:
        if (
            user_id != LEGACY_USER_ID
            or organization_id != LEGACY_ORGANIZATION_ID
            or membership_id != LEGACY_MEMBERSHIP_ID
        ):
            return None
        principal = legacy_principal(session)
    else:
        principal = _membership_with_context(
            session,
            membership_id=membership_id,
            user_id=user_id,
            organization_id=organization_id,
        )
    if principal is None or principal.user.auth_session_version != auth_session_version:
        return None
    return principal


def establish_session(values: dict[str, object], principal: AuthPrincipal) -> None:
    values.clear()
    values["resume_v3_user_id"] = principal.user.id
    values["resume_v3_organization_id"] = principal.organization.id
    values["resume_v3_membership_id"] = principal.membership.id
    values["resume_v3_auth_session_version"] = principal.user.auth_session_version
    # The signed browser session already authenticates the caller; a fresh
    # nonce lets short-lived file/export grants additionally bind to this
    # particular login rather than every browser session of the same user.
    values["resume_v3_session_nonce"] = secrets.token_urlsafe(32)


def clear_session(values: dict[str, object]) -> None:
    values.clear()


def revoke_user_auth_sessions(session: Session, *, principal: AuthPrincipal) -> bool:
    """Invalidate every signed browser session for one account.

    Browser sessions are stateless signed cookies.  A mailbox OAuth callback
    deliberately survives the provider's cross-site redirect through a
    short-lived, separately signed correlation cookie, so merely deleting the
    current browser cookie is not enough to make logout final.  Advancing the
    account version makes a delayed callback (and every pre-existing session)
    fail closed on its next server-side identity check.

    This is intentionally account-wide, matching password-reset revocation:
    the project does not retain a server-side per-browser session registry.
    The caller owns the transaction and still clears its browser cookie even
    when this update cannot be applied.
    """

    revoked = session.execute(
        update(UserAccount)
        .where(
            UserAccount.id == principal.user.id,
            UserAccount.is_active.is_(True),
        )
        .values(auth_session_version=UserAccount.auth_session_version + 1)
    )
    return bool(revoked.rowcount)


def create_registration(session: Session, payload: AuthRegistration) -> AuthPrincipal:
    organization_name = _normalize_text(
        payload.organization_name,
        code="invalid_organization_name",
        maximum=200,
    )
    full_name = _normalize_text(payload.full_name, code="invalid_full_name", maximum=200)
    email, email_key = normalize_email(payload.email)
    if session.scalar(select(UserAccount.id).where(UserAccount.email_key == email_key)):
        raise IdentityServiceError("email_already_registered")

    plan = _default_signup_trial_plan(session)
    if plan is None:
        raise IdentityServiceError("default_trial_plan_not_available")

    now = utcnow()
    organization = Organization(
        name=organization_name,
        plan=plan,
        plan_status="trial",
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=max(0, plan.trial_days)),
        trial_llm_call_limit=TRIAL_LLM_CALL_LIMIT,
        trial_llm_call_used=0,
    )
    user = UserAccount(
        email=email,
        email_key=email_key,
        full_name=full_name,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add_all((organization, user))
    session.flush()
    membership = OrganizationMembership(
        organization_id=organization.id,
        user_id=user.id,
        role="admin",
        is_active=True,
    )
    session.add(membership)
    session.flush()
    # The lifecycle migration seeds existing workspaces.  New registrations
    # need the same explicit manual default without making a future preview
    # endpoint create state as a side effect.
    set_organization_context(session, organization.id)
    try:
        session.add(
            CandidateDataRetentionPolicy(
                organization_id=organization.id,
                mode="manual",
                retention_days=None,
                version=1,
            )
        )
        session.flush()
    finally:
        clear_organization_context(session)
    return AuthPrincipal(user=user, membership=membership, organization=organization, plan=plan)


def _default_signup_trial_plan(session: Session) -> ProductPlan | None:
    return session.scalar(
        select(ProductPlan)
        .where(
            ProductPlan.is_active.is_(True),
            ProductPlan.is_available_for_signup.is_(True),
            ProductPlan.is_default_trial.is_(True),
        )
        .order_by(ProductPlan.sort_order, ProductPlan.created_at)
    )


def registration_offer(session: Session) -> RegistrationOfferResponse:
    plan = _default_signup_trial_plan(session)
    if plan is None:
        raise IdentityServiceError("default_trial_plan_not_available")
    return RegistrationOfferResponse(
        plan_code=plan.code,
        plan_name=plan.name,
        trial_days=max(0, plan.trial_days),
        llm_call_limit=TRIAL_LLM_CALL_LIMIT,
    )


def authenticate_email_password(
    session: Session,
    *,
    email_value: str,
    password: str,
) -> AuthPrincipal:
    _, email_key = normalize_email(email_value)
    user = session.scalar(select(UserAccount).where(UserAccount.email_key == email_key))
    if user is None or not user.is_active:
        # Do the same bounded scrypt work as a wrong password before returning
        # the shared public error. Without this, unknown/inactive accounts
        # would skip password verification and create an obvious remote timing
        # enumeration signal.
        verify_password(password, _LOGIN_DUMMY_PASSWORD_HASH)
        raise IdentityServiceError("invalid_login_credentials")
    if not verify_password(password, user.password_hash):
        raise IdentityServiceError("invalid_login_credentials")

    memberships = session.scalars(
        select(OrganizationMembership)
        .options(joinedload(OrganizationMembership.organization).joinedload(Organization.plan))
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
    ).all()
    if len(memberships) != 1:
        # Multi-workspace switching has intentionally not been introduced;
        # do not make an ambiguous membership silently select a tenant.
        raise IdentityServiceError("account_workspace_unavailable")
    membership = memberships[0]
    user.last_login_at = utcnow()
    return AuthPrincipal(
        user=user,
        membership=membership,
        organization=membership.organization,
        plan=membership.organization.plan,
    )


def trial_access(principal: AuthPrincipal, *, now: datetime | None = None) -> TrialAccessResponse:
    now = now or utcnow()
    organization = principal.organization
    status = organization.plan_status
    starts_at = _aware(organization.trial_started_at)
    ends_at = _aware(organization.trial_ends_at)
    if status == "trial" and ends_at is not None and now >= ends_at:
        status = "expired"
    access_enabled = status in {"trial", "active"}
    days_remaining: int | None = None
    if status == "trial" and ends_at is not None:
        days_remaining = max(0, (ends_at.date() - now.date()).days)
    llm_call_limit, llm_call_used, llm_call_remaining = trial_llm_call_snapshot(
        organization,
        plan_status=status,
    )
    return TrialAccessResponse(
        plan_status=status if status in {"trial", "active", "expired", "suspended"} else "suspended",
        trial_started_at=starts_at,
        trial_ends_at=ends_at,
        trial_days_remaining=days_remaining,
        llm_call_limit=llm_call_limit,
        llm_call_used=llm_call_used,
        llm_call_remaining=llm_call_remaining,
        access_enabled=access_enabled,
    )


def auth_session_response(
    principal: AuthPrincipal | None,
    *,
    login_required: bool,
) -> AuthSession:
    if principal is None:
        return AuthSession(authenticated=False, login_required=login_required)
    return AuthSession(
        authenticated=True,
        login_required=login_required,
        is_platform_admin=principal.is_platform_admin,
        user=AuthUserResponse(
            user_id=principal.user.id,
            display_name=principal.user.full_name,
            email=principal.user.email,
        ),
        organization=AuthOrganizationResponse(
            organization_id=principal.organization.id,
            name=principal.organization.name,
        ),
        role=principal.role if principal.role in {"admin", "recruiter"} else "recruiter",
        plan=(
            AuthPlanResponse(
                code=principal.plan.code,
                name=principal.plan.name,
                feature_flags=_feature_flags(principal.plan),
            )
            if principal.plan is not None
            else None
        ),
        trial=trial_access(principal),
        email_verified=principal.email_verified,
        email_verification_required=not principal.email_verified,
    )


def require_feature(principal: AuthPrincipal, feature_name: str) -> bool:
    return (
        principal.email_verified
        and trial_access(principal).access_enabled
        and _feature_flags(principal.plan).get(feature_name, False)
    )


def current_plan_response(principal: AuthPrincipal) -> OrganizationPlanResponse:
    plan = principal.plan
    if plan is None:
        raise IdentityServiceError("organization_plan_not_configured")
    access = trial_access(principal)
    return OrganizationPlanResponse(
        organization_id=principal.organization.id,
        plan_code=plan.code,
        plan_name=plan.name,
        monthly_price_cents=plan.monthly_price_cents,
        plan_status=access.plan_status,
        trial_started_at=access.trial_started_at,
        trial_ends_at=access.trial_ends_at,
        feature_flags=_feature_flags(plan),
    )


def list_product_plans(session: Session) -> list[ProductPlanResponse]:
    plans = session.scalars(select(ProductPlan).order_by(ProductPlan.sort_order, ProductPlan.created_at)).all()
    return [_product_plan_response(plan) for plan in plans]


def _product_plan_response(plan: ProductPlan) -> ProductPlanResponse:
    return ProductPlanResponse(
        plan_id=plan.id,
        code=plan.code,
        name=plan.name,
        monthly_price_cents=plan.monthly_price_cents,
        trial_days=plan.trial_days,
        feature_flags=_feature_flags(plan),
        is_active=plan.is_active,
        is_available_for_signup=plan.is_available_for_signup,
        is_default_trial=plan.is_default_trial,
        sort_order=plan.sort_order,
    )


def update_product_plan(
    session: Session,
    *,
    code: str,
    payload: ProductPlanUpdate,
) -> ProductPlanResponse:
    plan = session.scalar(select(ProductPlan).where(ProductPlan.code == code))
    if plan is None:
        raise IdentityServiceError("product_plan_not_found")
    updates = payload.model_dump(exclude_unset=True)
    # ``reason`` belongs to the platform audit event and is intentionally not
    # persisted on the reusable plan catalogue row.
    updates.pop("reason", None)
    if "name" in updates:
        plan.name = _normalize_text(str(updates["name"]), code="invalid_plan_name", maximum=120)
    for field in ("monthly_price_cents", "trial_days", "is_active", "is_available_for_signup", "sort_order"):
        if field in updates:
            setattr(plan, field, updates[field])
    if "feature_flags" in updates:
        plan.feature_flags = {str(key): bool(value) for key, value in updates["feature_flags"].items()}
    if updates.get("is_default_trial") is True:
        for other in session.scalars(select(ProductPlan).where(ProductPlan.id != plan.id)).all():
            other.is_default_trial = False
        plan.is_default_trial = True
    elif "is_default_trial" in updates:
        plan.is_default_trial = bool(updates["is_default_trial"])
    session.flush()
    available_default = session.scalar(
        select(ProductPlan.id).where(
            ProductPlan.is_active.is_(True),
            ProductPlan.is_available_for_signup.is_(True),
            ProductPlan.is_default_trial.is_(True),
        )
    )
    if available_default is None:
        raise IdentityServiceError("default_trial_plan_required")
    return _product_plan_response(plan)


def assign_organization_plan(
    session: Session,
    *,
    organization_id: str,
    payload: OrganizationPlanAssign,
) -> OrganizationPlanResponse:
    organization = session.get(Organization, organization_id)
    if organization is None:
        raise IdentityServiceError("organization_not_found")
    plan = session.scalar(
        select(ProductPlan).where(ProductPlan.code == payload.plan_code, ProductPlan.is_active.is_(True))
    )
    if plan is None:
        raise IdentityServiceError("product_plan_not_found")
    organization.plan = plan
    if payload.plan_status is not None:
        organization.plan_status = payload.plan_status
    session.flush()
    principal = session.scalar(
        select(OrganizationMembership)
        .options(joinedload(OrganizationMembership.user), joinedload(OrganizationMembership.organization).joinedload(Organization.plan))
        .where(OrganizationMembership.organization_id == organization.id, OrganizationMembership.is_active.is_(True))
        .order_by(OrganizationMembership.created_at)
    )
    if principal is None:
        raise IdentityServiceError("organization_membership_not_found")
    return current_plan_response(
        AuthPrincipal(
            user=principal.user,
            membership=principal,
            organization=principal.organization,
            plan=principal.organization.plan,
        )
    )


def create_invitation(
    session: Session,
    *,
    principal: AuthPrincipal,
    payload: OrganizationInvitationCreate,
) -> OrganizationInvitationResponse:
    email_key: str | None = None
    if payload.email:
        _, email_key = normalize_email(payload.email)
    token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation(
        organization_id=principal.organization.id,
        email_key=email_key,
        token_digest=digest_token(token),
        role=payload.role,
        expires_at=utcnow() + timedelta(days=payload.expires_in_days),
        created_by_user_id=principal.user.id,
    )
    session.add(invitation)
    session.flush()
    return OrganizationInvitationResponse(
        invitation_id=invitation.id,
        role=invitation.role,
        email=payload.email,
        expires_at=_aware(invitation.expires_at) or utcnow(),
        invitation_token=token,
    )


def accept_invitation(
    session: Session,
    *,
    payload: OrganizationInvitationAccept,
) -> AuthPrincipal:
    invitation = session.scalar(
        select(OrganizationInvitation)
        .options(joinedload(OrganizationInvitation.organization).joinedload(Organization.plan))
        .where(OrganizationInvitation.token_digest == digest_token(payload.invitation_token))
    )
    if invitation is None or invitation.accepted_at is not None or _aware(invitation.expires_at) <= utcnow():
        raise IdentityServiceError("invitation_invalid_or_expired")
    email, email_key = normalize_email(payload.email)
    if invitation.email_key is not None and invitation.email_key != email_key:
        raise IdentityServiceError("invitation_email_mismatch")
    if session.scalar(select(UserAccount.id).where(UserAccount.email_key == email_key)):
        raise IdentityServiceError("email_already_registered")
    user = UserAccount(
        email=email,
        email_key=email_key,
        full_name=_normalize_text(payload.full_name, code="invalid_full_name", maximum=200),
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add(user)
    session.flush()
    membership = OrganizationMembership(
        organization_id=invitation.organization_id,
        user_id=user.id,
        role=invitation.role,
        is_active=True,
    )
    invitation.accepted_at = utcnow()
    invitation.accepted_by_user_id = user.id
    session.add(membership)
    session.flush()
    return AuthPrincipal(
        user=user,
        membership=membership,
        organization=invitation.organization,
        plan=invitation.organization.plan,
    )


def issue_password_reset(
    session: Session,
    *,
    email_value: str,
    ttl_seconds: int = 60 * 60,
) -> IssuedPasswordReset | None:
    """Issue exactly one usable reset token for an active account.

    The raw value is returned only to the delivery adapter and must never be
    serialized through the HTTP response.  A new request invalidates every
    older unused link, including an expired-but-uncollected row, so the
    partial unique index remains a durable concurrency safeguard.
    """

    try:
        _, email_key = normalize_email(email_value)
    except IdentityServiceError:
        return None
    user = session.scalar(
        select(UserAccount)
        .where(
            UserAccount.email_key == email_key,
            UserAccount.is_active.is_(True),
        )
        .with_for_update()
    )
    if user is None:
        return None

    now = utcnow()
    active_resets = session.scalars(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.invalidated_at.is_(None),
        )
        .with_for_update()
    ).all()
    for active_reset in active_resets:
        active_reset.invalidated_at = now
    # Flush the invalidations before inserting the replacement.  PostgreSQL
    # otherwise has to reconcile a partial unique index with both states in
    # the same unit of work.
    if active_resets:
        session.flush()

    token = secrets.token_urlsafe(32)
    reset = PasswordResetToken(
        user_id=user.id,
        token_digest=digest_token(token),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(reset)
    session.flush()
    return IssuedPasswordReset(
        token=token,
        password_reset_token_id=reset.id,
        user_id=user.id,
        recipient=user.email,
    )


def _password_reset_completion_statement(*, token_digest: str):
    return (
        select(PasswordResetToken)
        .options(joinedload(PasswordResetToken.user))
        .where(PasswordResetToken.token_digest == token_digest)
        # ``joinedload`` uses a LEFT OUTER JOIN for the user relationship.
        # PostgreSQL rejects a bare ``FOR UPDATE`` on that query because it
        # would also attempt to lock the nullable joined side.  The reset row
        # is the only record that needs serialization here, so scope the lock
        # explicitly to it.
        .with_for_update(of=PasswordResetToken)
    )


def complete_password_reset(session: Session, *, token: str, password: str) -> None:
    reset = session.scalar(
        _password_reset_completion_statement(token_digest=digest_token(token))
    )
    if (
        reset is None
        or reset.used_at is not None
        or reset.invalidated_at is not None
        or _aware(reset.expires_at) <= utcnow()
    ):
        raise IdentityServiceError("password_reset_invalid_or_expired")
    if not reset.user.is_active:
        raise IdentityServiceError("password_reset_invalid_or_expired")
    reset.user.password_hash = hash_password(password)
    reset.user.auth_session_version += 1
    reset.used_at = utcnow()


def issue_email_verification(
    session: Session,
    *,
    user: UserAccount,
    ttl_seconds: int,
    resend_cooldown_seconds: int,
    daily_limit: int,
    enforce_resend_limit: bool,
    now: datetime | None = None,
) -> tuple[EmailVerificationToken, str]:
    """Create exactly one usable verification token for one account.

    The raw token is returned only to the caller that immediately builds the
    email link.  The database keeps a digest and delivery metadata only.
    """

    current_time = now or utcnow()
    # All issuance and completion paths lock this user row.  PostgreSQL then
    # serializes resend/verify races, while the partial unique index on the
    # token table independently enforces one usable link per user.
    locked_user = session.scalar(
        select(UserAccount)
        .where(UserAccount.id == user.id)
        .with_for_update()
    )
    if locked_user is None:
        raise IdentityServiceError("account_workspace_unavailable")
    if locked_user.email_verified_at is not None:
        raise IdentityServiceError("email_already_verified")

    if enforce_resend_limit:
        most_recent = session.scalar(
            select(EmailVerificationToken)
            .where(EmailVerificationToken.user_id == locked_user.id)
            .order_by(EmailVerificationToken.requested_at.desc(), EmailVerificationToken.id.desc())
            .limit(1)
        )
        if most_recent is not None:
            requested_at = _aware(most_recent.requested_at) or current_time
            elapsed_seconds = max(0.0, (current_time - requested_at).total_seconds())
            if elapsed_seconds < resend_cooldown_seconds:
                raise IdentityServiceError("email_verification_resend_too_soon")

        earliest = current_time - timedelta(days=1)
        issued_count = session.scalar(
            select(func.count())
            .select_from(EmailVerificationToken)
            .where(
                EmailVerificationToken.user_id == locked_user.id,
                EmailVerificationToken.requested_at >= earliest,
            )
        )
        if int(issued_count or 0) >= daily_limit:
            raise IdentityServiceError("email_verification_resend_limit_reached")

    # Only the latest link is valid.  Marking a superseded token separately
    # from `used_at` keeps security/audit semantics accurate.
    for pending in session.scalars(
        select(EmailVerificationToken).where(
            EmailVerificationToken.user_id == locked_user.id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.invalidated_at.is_(None),
        )
    ).all():
        pending.invalidated_at = current_time

    token = secrets.token_urlsafe(32)
    verification = EmailVerificationToken(
        user_id=locked_user.id,
        token_digest=digest_token(token),
        expires_at=current_time + timedelta(seconds=ttl_seconds),
        requested_at=current_time,
    )
    session.add(verification)
    session.flush()
    return verification, token


def record_email_verification_delivery(
    session: Session,
    *,
    verification_id: str,
    delivered: bool,
    error_code: str | None = None,
    now: datetime | None = None,
) -> None:
    verification = session.get(EmailVerificationToken, verification_id)
    if verification is None:
        return
    verification.delivery_attempt_count += 1
    verification.last_delivery_error = error_code
    if delivered:
        verification.delivered_at = now or utcnow()


def complete_email_verification(
    session: Session,
    *,
    token: str,
    expected_user_id: str | None = None,
    now: datetime | None = None,
) -> AuthPrincipal:
    current_time = now or utcnow()
    token_digest = digest_token(token)
    # Resolve the owner first, then use the same user-row lock as resend.
    # Re-reading the token after acquiring that lock closes the window where a
    # concurrent resend could otherwise replace the link being completed.
    token_user_id = session.scalar(
        select(EmailVerificationToken.user_id).where(
            EmailVerificationToken.token_digest == token_digest
        )
    )
    if token_user_id is None:
        raise IdentityServiceError("email_verification_invalid_or_expired")
    locked_user = session.scalar(
        select(UserAccount)
        .where(UserAccount.id == token_user_id)
        .with_for_update()
    )
    if locked_user is None:
        raise IdentityServiceError("email_verification_invalid_or_expired")
    verification = session.scalar(
        select(EmailVerificationToken)
        .options(joinedload(EmailVerificationToken.user))
        .where(EmailVerificationToken.token_digest == token_digest)
    )
    if (
        verification is None
        or verification.user_id != locked_user.id
        or verification.used_at is not None
        or verification.invalidated_at is not None
        or (_aware(verification.expires_at) or current_time) <= current_time
        or not locked_user.is_active
    ):
        raise IdentityServiceError("email_verification_invalid_or_expired")
    if expected_user_id is not None and expected_user_id != locked_user.id:
        # A link is proof of the target mailbox, not authorization to replace
        # an unrelated browser's active workspace session.
        raise IdentityServiceError("email_verification_account_mismatch")

    verification.used_at = current_time
    locked_user.email_verified_at = current_time
    for pending in session.scalars(
        select(EmailVerificationToken).where(
            EmailVerificationToken.user_id == verification.user_id,
            EmailVerificationToken.id != verification.id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.invalidated_at.is_(None),
        )
    ).all():
        pending.invalidated_at = current_time

    memberships = session.scalars(
        select(OrganizationMembership)
        .options(joinedload(OrganizationMembership.organization).joinedload(Organization.plan))
        .where(
            OrganizationMembership.user_id == verification.user_id,
            OrganizationMembership.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
    ).all()
    if len(memberships) != 1:
        raise IdentityServiceError("account_workspace_unavailable")
    membership = memberships[0]
    return AuthPrincipal(
        user=locked_user,
        membership=membership,
        organization=membership.organization,
        plan=membership.organization.plan,
    )
