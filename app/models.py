from __future__ import annotations

from decimal import Decimal
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationScoped:
    """Marker mixin for rows that must never cross a workspace boundary.

    The request/session layer installs tenant criteria for this type.  Keeping
    the foreign key on the directly queried roots (and worker queues) makes
    the boundary explicit even when a query does not join through a parent
    resume or job record.
    """

    __abstract__ = True

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"),
        index=True,
    )


class CandidateDataLifecycle:
    """Lifecycle state shared by candidate roots and resume originals.

    A logical delete is deliberately stored only on the two privacy roots.
    Derived records remain intact during the recovery window, but normal ORM
    reads automatically hide them through their deleted parent.  The purge
    worker is the only code path that is allowed to inspect these rows with
    the explicit lifecycle visibility bypass.
    """

    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    deleted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    deletion_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate_data_deletion_batches.id"),
        nullable=True,
        index=True,
    )
    purge_after_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    retention_hold: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        index=True,
    )
    # Every logical delete advances this value.  Original-file grants carry
    # the value they observed, so a grant created in a racing transaction can
    # never become valid again after delete -> restore.
    lifecycle_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
        nullable=False,
    )


class ProductPlan(Base):
    """A platform-managed, configurable product tier.

    Prices and feature access intentionally live in data rather than frontend
    constants so payment and platform-administration can be added later
    without changing tenant records.
    """

    __tablename__ = "product_plans"
    __table_args__ = (
        Index("ix_product_plans_signup_order", "is_available_for_signup", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    trial_days: Mapped[int] = mapped_column(Integer, default=30)
    feature_flags: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_available_for_signup: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default_trial: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    organizations: Mapped[list["Organization"]] = relationship(back_populates="plan")


class Organization(Base):
    """An isolated company workspace for recruiting data and jobs."""

    __tablename__ = "organizations"
    __table_args__ = (
        Index("ix_organizations_plan_status_trial_ends", "plan_status", "trial_ends_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_plans.id"),
        nullable=True,
        index=True,
    )
    # Values are application-validated: trial, active, expired, suspended,
    # and legacy.  A portable string is used instead of a database enum so the
    # same migration runs on SQLite and PostgreSQL.
    plan_status: Mapped[str] = mapped_column(String(32), default="trial", index=True)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # These values are copied onto a workspace when it enters self-service
    # trial. They deliberately do not live on ``ProductPlan.feature_flags``:
    # a later catalogue edit must not retroactively change an existing
    # workspace's already-consumed trial allowance.
    trial_llm_call_limit: Mapped[int] = mapped_column(
        Integer,
        default=1000,
        server_default=text("1000"),
    )
    trial_llm_call_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    # A workspace-level, server-owned cooldown for the feedback incentive.
    # It is intentionally stored on the organization rather than inferred from
    # a newest-feedback query: a single conditional UPDATE of this field is the
    # concurrency boundary that prevents two browser tabs or API replicas from
    # queuing two rewards inside the same cooldown period.
    feedback_reward_available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    plan: Mapped[ProductPlan | None] = relationship(back_populates="organizations")
    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    invitations: Mapped[list["OrganizationInvitation"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )


class UserAccount(Base):
    """A password-authenticated user identity, independent of any workspace."""

    __tablename__ = "user_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320))
    # ``email_key`` is the lower-cased, trimmed comparison key.  The original
    # address remains available for display and future verification mail.
    email_key: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(Text)
    # Every signed browser session carries this version.  Security-sensitive
    # account events such as password reset increment it to revoke all older
    # cookies without retaining a server-side session table.
    auth_session_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    accepted_invitations: Mapped[list["OrganizationInvitation"]] = relationship(
        back_populates="accepted_by_user",
        foreign_keys="OrganizationInvitation.accepted_by_user_id",
    )
    created_invitations: Mapped[list["OrganizationInvitation"]] = relationship(
        back_populates="created_by_user",
        foreign_keys="OrganizationInvitation.created_by_user_id",
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    transactional_email_outbox: Mapped[list["TransactionalEmailOutbox"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    email_verification_tokens: Mapped[list["EmailVerificationToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    published_ai_route_policy_versions: Mapped[list["AiRoutePolicyVersion"]] = relationship(
        back_populates="published_by_user",
        foreign_keys="AiRoutePolicyVersion.published_by_user_id",
    )
    created_ai_model_price_versions: Mapped[list["AiModelPriceVersion"]] = relationship(
        back_populates="created_by_user",
        foreign_keys="AiModelPriceVersion.created_by_user_id",
    )
    ai_runs: Mapped[list["AiRun"]] = relationship(
        back_populates="actor_user",
        foreign_keys="AiRun.actor_user_id",
    )
    candidate_favorites: Mapped[list["CandidateFavorite"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class PlatformAuditEvent(Base):
    """Immutable, privacy-safe record of one platform control-plane change.

    Snapshots are deliberately restricted by the calling service to account,
    workspace, plan, and routing metadata.  Candidate data, document content,
    prompts, provider responses, password material, and credentials must never
    be written to this table.
    """

    __tablename__ = "platform_audit_events"
    __table_args__ = (
        Index("ix_platform_audit_events_created", "created_at", "id"),
        Index("ix_platform_audit_events_actor_created", "actor_user_id", "created_at"),
        Index("ix_platform_audit_events_target_created", "target_type", "target_id", "created_at"),
        Index("ix_platform_audit_events_organization_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    organization_id: Mapped[str | None] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=True,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(500))
    before_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrganizationMembership(Base):
    """A user's role in one recruiting workspace."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_organization_membership"),
        Index("ix_organization_memberships_user_active", "user_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    # Application-validated role values: admin or recruiter.
    role: Mapped[str] = mapped_column(String(32), default="recruiter", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[UserAccount] = relationship(back_populates="memberships")


class OrganizationInvitation(Base):
    """A single-use digest-only invitation to join an existing workspace."""

    __tablename__ = "organization_invitations"
    __table_args__ = (
        Index("ix_organization_invitations_org_expiry", "organization_id", "expires_at"),
        Index("ix_organization_invitations_email_expiry", "email_key", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email_key: Mapped[str | None] = mapped_column(String(320), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="recruiter")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    organization: Mapped[Organization] = relationship(back_populates="invitations")
    accepted_by_user: Mapped[UserAccount | None] = relationship(
        back_populates="accepted_invitations",
        foreign_keys=[accepted_by_user_id],
    )
    created_by_user: Mapped[UserAccount | None] = relationship(
        back_populates="created_invitations",
        foreign_keys=[created_by_user_id],
    )


class PasswordResetToken(Base):
    """A one-time, digest-only password reset token.

    Only one reset link may remain usable for an account at a time.  The
    partial unique index is a database-level guard against concurrent requests
    bypassing the service-level row lock.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_user_requested", "user_id", "requested_at"),
        Index("ix_password_reset_tokens_expiry", "expires_at"),
        Index(
            "uq_active_password_reset_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("used_at IS NULL AND invalidated_at IS NULL"),
            postgresql_where=text("used_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[UserAccount] = relationship(back_populates="password_reset_tokens")
    delivery_jobs: Mapped[list["TransactionalEmailOutbox"]] = relationship(
        back_populates="password_reset_token",
        cascade="all, delete-orphan",
    )


class TransactionalEmailOutbox(Base):
    """Durable account-email work that must not run on an HTTP request.

    Reset links are stored only as Fernet ciphertext in ``encrypted_payload``;
    recipient email is resolved from the account at worker execution time. The
    queue deliberately contains only safe status/error metadata, never a raw
    token, URL, or provider response.
    """

    __tablename__ = "transactional_email_outbox"
    __table_args__ = (
        Index(
            "ix_transactional_email_outbox_due",
            "status",
            "next_attempt_at",
            "requested_at",
        ),
        Index(
            "ix_transactional_email_outbox_user_requested",
            "user_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Reserved for future account mail types; only ``password_reset`` is
    # currently accepted by the worker.
    message_kind: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"))
    password_reset_token_id: Mapped[str] = mapped_column(
        ForeignKey("password_reset_tokens.id"),
        unique=True,
    )
    encrypted_payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    user: Mapped[UserAccount] = relationship(back_populates="transactional_email_outbox")
    password_reset_token: Mapped[PasswordResetToken] = relationship(back_populates="delivery_jobs")


class EmailVerificationToken(Base):
    """A single-use, digest-only proof of control over an account email."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("ix_email_verification_tokens_user_requested", "user_id", "requested_at"),
        Index("ix_email_verification_tokens_expiry", "expires_at"),
        # A row lock serializes issuance in PostgreSQL, while this partial
        # unique index remains the database-level last line of defense: one
        # account can never have two simultaneously usable links.
        Index(
            "uq_active_email_verification_per_user",
            "user_id",
            unique=True,
            sqlite_where=text("used_at IS NULL AND invalidated_at IS NULL"),
            postgresql_where=text("used_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_delivery_error: Mapped[str | None] = mapped_column(String(128))

    user: Mapped[UserAccount] = relationship(back_populates="email_verification_tokens")


class RegistrationRateLimitBucket(Base):
    """A privacy-preserving, cross-replica counter for public signup limits.

    Keys are HMAC digests of the client or normalized email, never raw IP
    addresses or email addresses.  Time windows avoid an unbounded event log.
    """

    __tablename__ = "registration_rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "key_digest",
            "window_started_at",
            name="uq_registration_rate_limit_window",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(32))
    key_digest: Mapped[str] = mapped_column(String(64))
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class RuntimeWorkerHeartbeat(Base):
    """Durable, content-free liveness record for a background worker.

    The row is deliberately platform-scoped rather than organization-scoped:
    it records process health only and must never carry a candidate, mailbox,
    user, request body, or provider payload.  ``worker_id`` is internal
    process correlation; platform APIs expose only aggregate liveness fields.
    """

    __tablename__ = "runtime_worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'stopped')",
            name="ck_runtime_worker_heartbeat_status",
        ),
        Index(
            "ix_runtime_worker_heartbeat_kind_seen",
            "worker_kind",
            "last_seen_at",
        ),
        Index(
            "ix_runtime_worker_heartbeat_status_seen",
            "status",
            "last_seen_at",
        ),
        Index("ix_runtime_worker_heartbeat_last_seen", "last_seen_at"),
    )

    worker_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    worker_kind: Mapped[str] = mapped_column(String(64), default="background")
    status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        server_default=text("'running'"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    last_cycle_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # This holds a normalized operational code only. Raw exception text and
    # external provider responses must remain outside the runtime ledger.
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class WorkspaceBackgroundLane(Base):
    """A fair, fenced execution lane for one workspace's heavy work.

    This is deliberately platform-scoped operational metadata.  It contains
    no candidate, resume, mailbox, or model payload; it only prevents a busy
    workspace from consuming every shared worker process while another
    workspace has waiting work.  The token fences release and renewal so an
    old worker can never clear a newer worker's lease after a restart.
    """

    __tablename__ = "workspace_background_lanes"
    __table_args__ = (
        UniqueConstraint(
            "lane_key",
            "organization_id",
            name="uq_workspace_background_lane",
        ),
        Index(
            "ix_workspace_background_lane_claim",
            "lane_key",
            "lease_expires_at",
        ),
        Index(
            "ix_workspace_background_lane_fairness",
            "lane_key",
            "last_claimed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lane_key: Mapped[str] = mapped_column(String(64))
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    current_job_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class WorkspaceFeedbackSubmission(OrganizationScoped, Base):
    """One complete workspace-feedback questionnaire and its quota reward.

    The four text answers and contact number are product feedback, not
    candidate data.  They never belong in generic audit-event snapshots or
    application logs.  A durable reward state lets the worker grant the fixed
    allowance after server-side review processing without relying on a browser
    tab remaining open.
    """

    __tablename__ = "workspace_feedback_submissions"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_workspace_feedback_id_organization",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key_hash",
            name="uq_workspace_feedback_org_idempotency",
        ),
        CheckConstraint(
            "reward_status IN ('queued', 'running', 'granted')",
            name="ck_workspace_feedback_reward_status",
        ),
        CheckConstraint(
            "reward_call_count = 500",
            name="ck_workspace_feedback_reward_call_count",
        ),
        CheckConstraint(
            "reward_attempt_count >= 0",
            name="ck_workspace_feedback_reward_attempt_count",
        ),
        Index(
            "ix_workspace_feedback_reward_due",
            "reward_status",
            "reward_due_at",
            "created_at",
        ),
        Index(
            "ix_workspace_feedback_org_created",
            "organization_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submitted_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id"),
        index=True,
    )
    # Only digests are retained so a transport retry key cannot become a
    # durable browser identifier.  ``request_fingerprint`` detects accidental
    # reuse of the same key for a different questionnaire payload.
    idempotency_key_hash: Mapped[str] = mapped_column(String(64))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    use_case: Mapped[str] = mapped_column(Text)
    intended_outcome: Mapped[str] = mapped_column(Text)
    friction: Mapped[str] = mapped_column(Text)
    desired_change: Mapped[str] = mapped_column(Text)
    # Existing questionnaire rows predate contact collection.  New submissions
    # are validated as required at the service boundary, while the column stays
    # nullable so the migration never fabricates or invalidates historical PII.
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reward_status: Mapped[str] = mapped_column(
        String(32),
        default="queued",
        server_default=text("'queued'"),
        index=True,
    )
    reward_call_count: Mapped[int] = mapped_column(
        Integer,
        default=500,
        server_default=text("500"),
    )
    reward_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reward_attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    reward_lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reward_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reward_last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reward_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    image_attachments: Mapped[list["WorkspaceFeedbackImageAttachment"]] = relationship(
        back_populates="feedback_submission",
        cascade="all, delete-orphan",
    )


class WorkspaceFeedbackImageAttachment(OrganizationScoped, Base):
    """Metadata for an optional image already accepted by a trusted uploader.

    The feedback service deliberately handles metadata only.  The HTTP upload
    boundary owns byte validation and storage; this model keeps a scoped
    reference so a future attachment-serving endpoint can authorize it without
    trusting a browser-supplied path.
    """

    __tablename__ = "workspace_feedback_image_attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feedback_submission_id", "organization_id"],
            [
                "workspace_feedback_submissions.id",
                "workspace_feedback_submissions.organization_id",
            ],
            name="fk_workspace_feedback_image_submission_org",
        ),
        UniqueConstraint(
            "feedback_submission_id",
            "sort_order",
            name="uq_workspace_feedback_image_order",
        ),
        CheckConstraint("sort_order >= 0", name="ck_workspace_feedback_image_order"),
        CheckConstraint("size_bytes >= 0", name="ck_workspace_feedback_image_size"),
        Index(
            "ix_workspace_feedback_image_org_submission",
            "organization_id",
            "feedback_submission_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    feedback_submission_id: Mapped[str] = mapped_column(String(36), index=True)
    sort_order: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(512))
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    feedback_submission: Mapped[WorkspaceFeedbackSubmission] = relationship(
        back_populates="image_attachments",
    )


class Candidate(OrganizationScoped, CandidateDataLifecycle, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        Index("ix_candidates_organization_created", "organization_id", "created_at"),
        Index(
            "ix_candidates_organization_lifecycle",
            "organization_id",
            "deleted_at",
            "purge_after_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )
    favorites: Mapped[list["CandidateFavorite"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class CandidateFavorite(OrganizationScoped, Base):
    """One current user's private bookmark for one candidate.

    Favorites deliberately point at the candidate identity rather than a
    resume version.  They are neither a shared talent pool nor a copy of any
    resume, AI result, score, or source text.  Normal tenant and lifecycle
    scope rules protect the association and hide it whenever its candidate is
    logically deleted.
    """

    __tablename__ = "candidate_favorites"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "candidate_id",
            name="uq_candidate_favorite_owner",
        ),
        Index(
            "ix_candidate_favorites_organization_user_created",
            "organization_id",
            "user_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[UserAccount] = relationship(back_populates="candidate_favorites")
    candidate: Mapped[Candidate] = relationship(back_populates="favorites")


class Resume(OrganizationScoped, CandidateDataLifecycle, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        Index(
            "uq_active_resume_per_candidate",
            "candidate_id",
            unique=True,
            sqlite_where=text("is_active = 1 AND deleted_at IS NULL"),
            postgresql_where=text("is_active = true AND deleted_at IS NULL"),
        ),
        Index("ix_resumes_organization_created", "organization_id", "created_at"),
        Index("ix_resumes_organization_candidate", "organization_id", "candidate_id"),
        Index(
            "ix_resumes_organization_lifecycle",
            "organization_id",
            "deleted_at",
            "purge_after_at",
        ),
        Index(
            "ix_resumes_organization_source_mailbox",
            "organization_id",
            "source_mailbox_config_id",
        ),
        Index(
            "ix_resumes_organization_ingestion_source",
            "organization_id",
            "ingestion_source_type",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    source_page_count: Mapped[int] = mapped_column(Integer)
    parsed_page_count: Mapped[int] = mapped_column(Integer)
    extraction_status: Mapped[str] = mapped_column(String(32), index=True)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    parser_version: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_985_211: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    highest_degree: Mapped[str | None] = mapped_column(String(32), index=True)
    employment_months: Mapped[int] = mapped_column(Integer, default=0, index=True)
    employment_or_internship_months: Mapped[int] = mapped_column(Integer, default=0, index=True)
    facts_version: Mapped[int] = mapped_column(Integer, default=0)
    raw_text: Mapped[str | None] = mapped_column(Text)
    # Contacts derive locally from saved source blocks. They intentionally stay
    # outside AI facts and candidate search; only the protected review view and
    # a candidate-owned export project them.
    contact_details: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'"),
        nullable=False,
    )
    # Keep source provenance on the resume itself so the library can filter
    # and display a stable channel label without depending on mutable mailbox
    # configuration.  Existing/manual uploads retain the safe default.
    ingestion_source_type: Mapped[str] = mapped_column(
        String(32),
        default="manual_upload",
        server_default=text("'manual_upload'"),
    )
    source_mailbox_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_configs.id"),
        nullable=True,
    )
    source_mailbox_label_snapshot: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    candidate: Mapped[Candidate] = relationship(back_populates="resumes")
    source_blocks: Mapped[list["ResumeSourceBlock"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    educations: Mapped[list["ResumeEducation"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    experiences: Mapped[list["ResumeExperience"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    skills: Mapped[list["ResumeSkill"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    language_credentials: Mapped[list["ResumeLanguageCredential"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    scholarships: Mapped[list["ResumeScholarship"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    review_actions: Mapped[list["ResumeReviewAction"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    fact_snapshots: Mapped[list["ResumeFactSnapshot"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    scores: Mapped[list["ResumeScore"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    summaries: Mapped[list["ResumeSummary"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    summary_jobs: Mapped[list["ResumeSummaryJob"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    candidate_name_extraction_job: Mapped["CandidateNameExtractionJob | None"] = (
        relationship(
            back_populates="resume",
            cascade="all, delete-orphan",
            uselist=False,
        )
    )
    job_matches: Mapped[list["JobMatch"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
    upload_idempotency_keys: Mapped[list["ResumeUploadIdempotencyKey"]] = (
        relationship(
            back_populates="resume",
            cascade="all, delete-orphan",
        )
    )
    ai_extraction_job: Mapped["ResumeAiExtractionJob | None"] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        uselist=False,
    )
    document_extraction_job: Mapped["ResumeDocumentExtractionJob | None"] = (
        relationship(
            back_populates="resume",
            cascade="all, delete-orphan",
            uselist=False,
        )
    )
    source_mailbox_config: Mapped["MailboxConfig | None"] = relationship(
        back_populates="ingested_resumes",
        foreign_keys=[source_mailbox_config_id],
    )


class CandidateDataDeletionBatch(OrganizationScoped, Base):
    """One reversible request to remove candidate data from a workspace."""

    __tablename__ = "candidate_data_deletion_batches"
    __table_args__ = (
        Index(
            "ix_candidate_data_deletion_batches_organization_created",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_candidate_data_deletion_batches_organization_recovery",
            "organization_id",
            "status",
            "recovery_deadline_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    # ``manual_resume``, ``manual_candidate`` and ``retention`` are
    # application-validated values.  They remain portable strings so the
    # same migration works on SQLite and PostgreSQL.
    trigger_type: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    # Kept only for migration compatibility.  New lifecycle requests never
    # persist free-form notes: a user-entered explanation can itself contain
    # candidate personal data and must not outlive the candidate record.
    private_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="deleted", index=True)
    recovery_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purge_after_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    items: Mapped[list["CandidateDataDeletionBatchItem"]] = relationship(
        back_populates="deletion_batch",
        cascade="all, delete-orphan",
    )
    purge_job: Mapped["CandidateDataPurgeJob | None"] = relationship(
        back_populates="deletion_batch",
        uselist=False,
        cascade="all, delete-orphan",
    )


class CandidateDataDeletionBatchItem(OrganizationScoped, Base):
    """Opaque targets affected by one deletion batch.

    Target IDs intentionally do not carry foreign-key constraints.  Batch
    history remains available as a privacy-safe tombstone after physical
    purge, while ordinary business APIs never expose it.
    """

    __tablename__ = "candidate_data_deletion_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "deletion_batch_id",
            "resume_id",
            name="uq_candidate_data_deletion_batch_item_resume",
        ),
        Index(
            "ix_candidate_data_deletion_batch_items_organization_batch",
            "organization_id",
            "deletion_batch_id",
        ),
        Index(
            "ix_candidate_data_deletion_batch_items_organization_candidate",
            "organization_id",
            "candidate_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deletion_batch_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_data_deletion_batches.id", ondelete="CASCADE"),
        index=True,
    )
    candidate_id: Mapped[str] = mapped_column(String(36), index=True)
    resume_id: Mapped[str] = mapped_column(String(36), index=True)
    was_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    deletion_batch: Mapped[CandidateDataDeletionBatch] = relationship(
        back_populates="items"
    )


class CandidateDataPurgeJob(OrganizationScoped, Base):
    """Lease-protected physical cleanup work for one deletion batch."""

    __tablename__ = "candidate_data_purge_jobs"
    __table_args__ = (
        UniqueConstraint(
            "deletion_batch_id",
            name="uq_candidate_data_purge_job_batch",
        ),
        Index(
            "ix_candidate_data_purge_jobs_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_candidate_data_purge_jobs_organization_lease",
            "organization_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    deletion_batch_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_data_deletion_batches.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=20)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    deletion_batch: Mapped[CandidateDataDeletionBatch] = relationship(
        back_populates="purge_job"
    )


class CandidateDataAuditEvent(OrganizationScoped, Base):
    """Append-only, workspace-private audit without candidate content."""

    __tablename__ = "candidate_data_audit_events"
    __table_args__ = (
        Index(
            "ix_candidate_data_audit_events_organization_created",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_candidate_data_audit_events_organization_action_created",
            "organization_id",
            "action",
            "created_at",
        ),
        Index(
            "ix_candidate_data_audit_events_target_created",
            "target_type",
            "target_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    actor_kind: Mapped[str] = mapped_column(String(32), default="user")
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(128), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    resume_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), default="web")
    result: Mapped[str] = mapped_column(String(32), default="authorized")
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CandidateDataFileAccessGrant(OrganizationScoped, Base):
    """Opaque, short-lived access to one original or completed export."""

    __tablename__ = "candidate_data_file_access_grants"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_candidate_data_file_access_grant_token"),
        Index(
            "ix_candidate_data_file_access_grants_organization_resource",
            "organization_id",
            "resource_type",
            "resource_id",
        ),
        Index(
            "ix_candidate_data_file_access_grants_expiry",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(String(32), index=True)
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    token_digest: Mapped[str] = mapped_column(String(64))
    session_nonce_digest: Mapped[str] = mapped_column(String(64))
    # ``resume_original`` grants are fenced to the Resume lifecycle version.
    # Export grants intentionally leave this null because their own durable
    # revoke/expiry state is the fence.
    resource_lifecycle_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CandidateDataRetentionPolicy(OrganizationScoped, Base):
    """One opt-in candidate data retention policy per workspace."""

    __tablename__ = "candidate_data_retention_policies"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            name="uq_candidate_data_retention_policy_organization",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mode: Mapped[str] = mapped_column(String(32), default="manual")
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class CandidateDataRetentionCleanupRun(OrganizationScoped, Base):
    """Privacy-safe counts for one retention evaluation or enqueue run."""

    __tablename__ = "candidate_data_retention_cleanup_runs"
    __table_args__ = (
        Index(
            "ix_candidate_data_retention_cleanup_runs_organization_started",
            "organization_id",
            "started_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trigger_type: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[int] = mapped_column(Integer)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_hold_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CandidateDataExport(OrganizationScoped, Base):
    """A workspace-scoped asynchronous snapshot export.

    ``snapshot_json`` has only opaque candidate/resume/fact-version IDs and
    fixed options.  It deliberately never stores raw source text, original
    filenames, email metadata, model prompts, or model responses.
    """

    __tablename__ = "candidate_data_exports"
    __table_args__ = (
        Index(
            "ix_candidate_data_exports_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_candidate_data_exports_organization_created",
            "organization_id",
            "created_at",
        ),
        Index("ix_candidate_data_exports_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    snapshot_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    include_originals: Mapped[bool] = mapped_column(Boolean, default=False)
    output_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class MailboxDeletedAttachmentTombstone(OrganizationScoped, Base):
    """HMAC-only mailbox de-dup marker for a physically deleted resume."""

    __tablename__ = "mailbox_deleted_attachment_tombstones"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "digest",
            "key_version",
            name="uq_mailbox_deleted_attachment_tombstone_digest",
        ),
        Index(
            "ix_mailbox_deleted_attachment_tombstones_organization_expiry",
            "organization_id",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    key_version: Mapped[str] = mapped_column(String(32), default="v1")
    deletion_batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResumeUploadIdempotencyKey(OrganizationScoped, Base):
    """Durable replay record for the convenience resume upload endpoint.

    The client supplied key is stored only as a SHA-256 digest.  This keeps the
    key opaque to database readers while still allowing a retry to find the
    original upload deterministically.
    """

    __tablename__ = "resume_upload_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("resume_id", name="uq_resume_upload_idempotency_resume"),
        Index(
            "ix_resume_upload_idempotency_keys_organization_created",
            "organization_id",
            "created_at",
        ),
    )

    # A caller may legitimately reuse the same opaque idempotency key in two
    # unrelated workspaces, so the workspace is part of the durable key.
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"),
        primary_key=True,
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64))
    resume_id: Mapped[str] = mapped_column(
        ForeignKey("resumes.id"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="upload_idempotency_keys")


class MailboxConfig(OrganizationScoped, Base):
    """An independently named IMAP source. Its password is always encrypted."""

    __tablename__ = "mailbox_configs"
    __table_args__ = (
        CheckConstraint(
            "initial_sync_lookback_days >= 0 AND initial_sync_lookback_days <= 365",
            name="ck_mailbox_configs_initial_sync_lookback_days",
        ),
        UniqueConstraint(
            "organization_id",
            "display_name_key",
            name="uq_mailbox_configs_organization_display_name_key",
        ),
        Index("ix_mailbox_configs_organization_enabled", "organization_id", "enabled"),
        Index(
            "ix_mailbox_configs_organization_sync_claim",
            "organization_id",
            "enabled",
            "archived_at",
            "sync_lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # The service validates the visible label and writes its NFKC/casefolded
    # comparison key.  Defaults preserve the legacy one-mailbox behaviour
    # until callers are migrated to the explicit multi-channel API.
    display_name: Mapped[str] = mapped_column(
        String(32),
        default="默认收件邮箱",
        server_default=text("'默认收件邮箱'"),
    )
    display_name_key: Mapped[str] = mapped_column(
        String(64),
        default="默认收件邮箱",
        server_default=text("'默认收件邮箱'"),
    )
    # A reviewed provider identifier lets the UI describe a connection in
    # human terms. ``legacy_imap`` preserves old rows whose endpoint does not
    # map to one of the current presets.
    provider_key: Mapped[str] = mapped_column(
        String(64),
        default="legacy_imap",
        server_default=text("'legacy_imap'"),
    )
    authentication_mode: Mapped[str] = mapped_column(
        String(16),
        default="app_password",
        server_default=text("'app_password'"),
    )
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    email_address: Mapped[str] = mapped_column(String(320))
    mailbox: Mapped[str] = mapped_column(String(255), default="INBOX")
    # Retained for app-password channels only. OAuth refresh tokens live in a
    # dedicated one-to-one record so they cannot be confused with a mailbox
    # password by old operational tooling.
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # A binding starts at the mailbox's current UIDNEXT.  This makes the
    # inbox an append-only source from the moment the user connects it: mail
    # that was already present is never retrospectively scanned.
    import_start_uid: Mapped[int | None] = mapped_column(BigInteger)
    imap_uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    import_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A new source may opt into a one-time bounded historical import.  The
    # date is fixed when the connection is successfully bound rather than
    # recalculated by a delayed worker, so retries and restarts cannot widen
    # the selected window.  Existing rows migrate to zero/no date and retain
    # their original "from now" behavior.
    initial_sync_lookback_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    initial_backfill_since_date: Mapped[date | None] = mapped_column(Date)
    initial_backfill_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Scheduler fairness uses the last attempted run, not only successful
    # syncs, so one broken source cannot monopolize the worker loop.
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    # Mail content is kept in a separate, short-lived cache.  Candidate
    # originals and parsed resume facts never participate in this policy.
    retention_policy: Mapped[str] = mapped_column(String(16), default="standard")
    last_retention_cleanup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A per-channel lease prevents manual and scheduled sync calls from
    # processing the same IMAP source concurrently without blocking another
    # channel in the same workspace.
    sync_lease_token: Mapped[str | None] = mapped_column(String(64))
    sync_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Every browser reauthorization is assigned a monotonically increasing
    # generation.  A late callback from an older browser tab must never replace
    # the refresh material produced by the most recently started flow.
    oauth_reauthorization_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    imports: Mapped[list["EmailAttachmentImport"]] = relationship(
        back_populates="mailbox_config", cascade="all, delete-orphan"
    )
    content_replicas: Mapped[list["MailboxContentReplica"]] = relationship(
        back_populates="mailbox_config", cascade="all, delete-orphan"
    )
    retention_cleanup_runs: Mapped[list["MailboxRetentionCleanupRun"]] = relationship(
        back_populates="mailbox_config", cascade="all, delete-orphan"
    )
    ingested_resumes: Mapped[list[Resume]] = relationship(
        back_populates="source_mailbox_config",
        foreign_keys="Resume.source_mailbox_config_id",
    )
    background_jobs: Mapped[list["MailboxBackgroundJob"]] = relationship(
        back_populates="mailbox_config", cascade="all, delete-orphan"
    )
    sync_failure_alert: Mapped["MailboxSyncFailureAlert | None"] = relationship(
        back_populates="mailbox_config",
        cascade="all, delete-orphan",
        uselist=False,
    )
    oauth_credential: Mapped["MailboxOAuthCredential | None"] = relationship(
        back_populates="mailbox_config",
        cascade="all, delete-orphan",
        uselist=False,
    )


class MailboxOAuthCredential(OrganizationScoped, Base):
    """Encrypted refresh material for one OAuth-backed mailbox.

    Access tokens are intentionally refreshed in memory for each IMAP run and
    never persisted.  This row contains neither an OAuth client secret nor a
    user-visible password.
    """

    __tablename__ = "mailbox_oauth_credentials"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_config_id",
            name="uq_mailbox_oauth_credentials_mailbox_config",
        ),
        Index(
            "ix_mailbox_oauth_credentials_organization_mailbox",
            "organization_id",
            "mailbox_config_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_configs.id"),
        nullable=False,
    )
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    reauthorization_required_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    mailbox_config: Mapped[MailboxConfig] = relationship(
        back_populates="oauth_credential"
    )


class MailboxOAuthConnectIntent(OrganizationScoped, Base):
    """One short-lived, account-bound OAuth authorization-code intent.

    The browser and OAuth provider only receive the opaque random state.  Its
    digest, the encrypted PKCE verifier and the intended workspace are stored
    here so a callback cannot be replayed or completed by another member.
    """

    __tablename__ = "mailbox_oauth_connect_intents"
    __table_args__ = (
        CheckConstraint(
            "initial_sync_lookback_days >= 0 AND initial_sync_lookback_days <= 365",
            name="ck_mailbox_oauth_connect_intents_initial_sync_lookback_days",
        ),
        UniqueConstraint("state_hash", name="uq_mailbox_oauth_connect_intents_state_hash"),
        Index(
            "ix_mailbox_oauth_connect_intents_organization_expiry",
            "organization_id",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), nullable=False)
    membership_id: Mapped[str] = mapped_column(
        ForeignKey("organization_memberships.id"),
        nullable=False,
    )
    target_mailbox_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("mailbox_configs.id"),
        nullable=True,
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(32), nullable=False)
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    mailbox: Mapped[str] = mapped_column(String(255), nullable=False)
    # The create-flow selection survives the provider redirect. It is ignored
    # for a reauthorization intent, which always preserves the existing
    # channel's immutable historical-import policy.
    initial_sync_lookback_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    # ``0`` is reserved for a first-time connection. Reauthorization intents
    # carry the exact mailbox generation that was current when their browser
    # handoff began, enabling a final compare-and-swap before token persistence.
    reauthorization_generation: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MailboxSyncFailureAlert(OrganizationScoped, Base):
    """One durable sync-health incident for a named mailbox source.

    The row contains only stable internal error codes and timestamps. It never
    stores IMAP credentials, email content, provider diagnostics, or candidate
    information, so it can safely surface a workspace-local operational alert.
    """

    __tablename__ = "mailbox_sync_failure_alerts"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_config_id",
            name="uq_mailbox_sync_failure_alerts_mailbox_config_id",
        ),
        Index(
            "ix_mailbox_sync_failure_alerts_organization_state",
            "organization_id",
            "state",
            "last_failed_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_configs.id", ondelete="CASCADE"),
        index=True,
    )
    # ``monitoring`` retains a short failure streak below the alert threshold;
    # ``open`` is visible in the workspace; ``resolved`` preserves the last
    # recovery without leaving an active alert behind.
    state: Mapped[str] = mapped_column(String(16), default="monitoring", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    # Job history is pruned independently, so this deliberately remains a
    # plain identifier rather than a foreign key that would block retention.
    last_job_id: Mapped[str | None] = mapped_column(String(36))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    mailbox_config: Mapped[MailboxConfig] = relationship(
        back_populates="sync_failure_alert"
    )


class EmailAttachmentImport(OrganizationScoped, Base):
    """One idempotent attachment record, including its retryable source identity."""

    __tablename__ = "email_attachment_imports"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_config_id",
            "message_uid",
            "attachment_sha256",
            name="uq_email_attachment_import_message_attachment",
        ),
        Index("ix_email_attachment_imports_resume_id", "resume_id"),
        Index(
            "ix_email_attachment_imports_canonical_import_id",
            "canonical_import_id",
        ),
        Index("ix_email_attachment_imports_config_created", "mailbox_config_id", "created_at"),
        Index("ix_email_attachment_imports_organization_created", "organization_id", "created_at"),
        Index(
            "ix_email_attachment_imports_retry_lease",
            "organization_id",
            "status",
            "retry_lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(ForeignKey("mailbox_configs.id"), index=True)
    message_uid: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str | None] = mapped_column(String(998))
    attachment_filename: Mapped[str] = mapped_column(String(255))
    attachment_sha256: Mapped[str] = mapped_column(String(64))
    # UID values have meaning only for a single IMAP UIDVALIDITY epoch.  Both
    # this and the source fingerprint must match before a manual retry can
    # fetch the original attachment again.
    source_uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    resume_id: Mapped[str | None] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    # A duplicate mail attachment points at the one canonical import that
    # created the resume.  The field stays nullable for canonical, failed,
    # skipped, and historical rows.
    canonical_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_claim_token: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    mailbox_config: Mapped[MailboxConfig] = relationship(back_populates="imports")
    attempts: Mapped[list["EmailAttachmentImportAttempt"]] = relationship(
        back_populates="attachment_import",
        cascade="all, delete-orphan",
        order_by="EmailAttachmentImportAttempt.attempt_number",
    )
    retention_replicas: Mapped[list["MailboxContentReplica"]] = relationship(
        back_populates="attachment_import",
    )
    background_jobs: Mapped[list["MailboxBackgroundJob"]] = relationship(
        back_populates="attachment_import",
    )


class MailboxAttachmentContentIdentity(OrganizationScoped, Base):
    """One workspace-scoped byte identity for mailbox attachment ingestion.

    This intentionally does not use ``Resume.sha256``.  A resume can be
    uploaded through other sources or have historical duplicate rows, whereas
    the mailbox needs one short-lived, atomic ownership claim before it can
    create a candidate.  The unique organization/hash pair is the database
    boundary that prevents two forwarded copies from creating two candidates.
    """

    __tablename__ = "mailbox_attachment_content_identities"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "attachment_sha256",
            name="uq_mailbox_attachment_content_identity_org_sha",
        ),
        Index(
            "ix_mailbox_attachment_content_identity_claim",
            "organization_id",
            "status",
            "claim_lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attachment_sha256: Mapped[str] = mapped_column(String(64))
    # ``processing`` owns an ingestion lease, ``imported`` has a canonical
    # resume, and ``failed`` may be safely claimed by a later forwarded copy.
    status: Mapped[str] = mapped_column(String(32), index=True)
    processing_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="SET NULL"),
        nullable=True,
    )
    canonical_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="SET NULL"),
        nullable=True,
    )
    canonical_resume_id: Mapped[str | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EmailAttachmentImportAttempt(OrganizationScoped, Base):
    """Immutable audit result for one automatic or manual import attempt."""

    __tablename__ = "email_attachment_import_attempts"
    __table_args__ = (
        UniqueConstraint(
            "email_attachment_import_id",
            "attempt_number",
            name="uq_email_attachment_import_attempt_number",
        ),
        Index(
            "ix_email_attachment_import_attempts_organization_created",
            "organization_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email_attachment_import_id: Mapped[str] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="CASCADE"),
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    resume_id: Mapped[str | None] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    attachment_import: Mapped[EmailAttachmentImport] = relationship(back_populates="attempts")


class MailboxBackgroundJob(OrganizationScoped, Base):
    """Durable IMAP work that is never executed by a web request.

    A task stores only source IDs and safe counters. Credentials, RFC822 bytes,
    and provider-specific errors remain in the worker-owned path.
    """

    __tablename__ = "mailbox_background_jobs"
    __table_args__ = (
        Index("ix_mailbox_background_jobs_claim", "status", "next_attempt_at"),
        Index(
            "ix_mailbox_background_jobs_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_mailbox_background_jobs_organization_lease",
            "organization_id",
            "status",
            "lease_expires_at",
        ),
        # One active sync per named mailbox avoids concurrent IMAP scans while
        # allowing different channels in the same workspace to proceed.
        Index(
            "uq_mailbox_background_jobs_active_sync",
            "organization_id",
            "mailbox_config_id",
            "job_kind",
            unique=True,
            sqlite_where=text("job_kind = 'sync' AND status IN ('queued', 'running')"),
            postgresql_where=text("job_kind = 'sync' AND status IN ('queued', 'running')"),
        ),
        Index(
            "uq_mailbox_background_jobs_active_attachment_retry",
            "organization_id",
            "email_attachment_import_id",
            unique=True,
            sqlite_where=text(
                "job_kind = 'attachment_retry' "
                "AND status IN ('queued', 'running') "
                "AND email_attachment_import_id IS NOT NULL"
            ),
            postgresql_where=text(
                "job_kind = 'attachment_retry' "
                "AND status IN ('queued', 'running') "
                "AND email_attachment_import_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_configs.id", ondelete="CASCADE"),
        index=True,
    )
    email_attachment_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    job_kind: Mapped[str] = mapped_column(String(32), index=True)
    trigger_type: Mapped[str] = mapped_column(String(32))
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    mailbox_config: Mapped[MailboxConfig] = relationship(back_populates="background_jobs")
    attachment_import: Mapped[EmailAttachmentImport | None] = relationship(
        back_populates="background_jobs"
    )


class MailboxContentReplica(OrganizationScoped, Base):
    """A short-lived mailbox body or attachment copy, never a resume original.

    Files are stored under a dedicated workspace cache namespace.  Keeping the
    cache index separate from ``Resume.storage_key`` makes retention cleanup
    incapable of deleting a candidate's uploaded original by design.
    """

    __tablename__ = "mailbox_content_replicas"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_config_id",
            "kind",
            "source_reference",
            name="uq_mailbox_content_replicas_source",
        ),
        UniqueConstraint("storage_key", name="uq_mailbox_content_replicas_storage_key"),
        Index(
            "ix_mailbox_content_replicas_cleanup",
            "organization_id",
            "cleaned_at",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_configs.id", ondelete="CASCADE"),
        index=True,
    )
    email_attachment_import_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_attachment_imports.id", ondelete="SET NULL"),
        index=True,
    )
    # ``body`` and ``failed_attachment`` are used today.  The explicit kind
    # also reserves a safe place for a future success-copy policy without
    # ever conflating it with a candidate resume.
    kind: Mapped[str] = mapped_column(String(32), index=True)
    source_reference: Mapped[str] = mapped_column(String(128))
    storage_key: Mapped[str] = mapped_column(String(512))
    content_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_error: Mapped[str | None] = mapped_column(String(128))
    cleanup_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cleanup_claim_token: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    mailbox_config: Mapped[MailboxConfig] = relationship(back_populates="content_replicas")
    attachment_import: Mapped[EmailAttachmentImport | None] = relationship(
        back_populates="retention_replicas"
    )


class MailboxRetentionCleanupRun(OrganizationScoped, Base):
    """A privacy-safe audit record for one cleanup execution."""

    __tablename__ = "mailbox_retention_cleanup_runs"
    __table_args__ = (
        Index(
            "ix_mailbox_retention_cleanup_runs_organization_started",
            "organization_id",
            "started_at",
        ),
        Index(
            "ix_mailbox_retention_cleanup_runs_config_started",
            "mailbox_config_id",
            "started_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mailbox_config_id: Mapped[str] = mapped_column(
        ForeignKey("mailbox_configs.id", ondelete="CASCADE"),
        index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(32))
    retention_policy: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="completed")
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    reclaimed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    mailbox_config: Mapped[MailboxConfig] = relationship(
        back_populates="retention_cleanup_runs"
    )


class ResumeDocumentExtractionJob(OrganizationScoped, Base):
    """Durable, lease-based normalization work for one uploaded original.

    Upload handlers never run an untrusted document converter in the API
    process.  They persist an original and one row in this queue instead; the
    worker claims the row under its owning organization before it opens the
    file.  There is intentionally one mutable job per resume so retries are
    idempotent and cannot create duplicate source evidence.
    """

    __tablename__ = "resume_document_extraction_jobs"
    __table_args__ = (
        UniqueConstraint("resume_id", name="uq_resume_document_extraction_job_resume"),
        Index(
            "ix_resume_document_extraction_job_claim",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_resume_document_extraction_job_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_resume_document_extraction_job_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    resume: Mapped[Resume] = relationship(back_populates="document_extraction_job")


class ResumeAiExtractionJob(OrganizationScoped, Base):
    """Durable, lease-based AI structured-facts extraction work for one resume.

    There is intentionally one mutable job per resume.  Re-running extraction
    resets that job only while the resume remains pending human review, which
    prevents a delayed worker from silently replacing already-confirmed facts.
    """

    __tablename__ = "resume_ai_extraction_jobs"
    __table_args__ = (
        UniqueConstraint("resume_id", name="uq_resume_ai_extraction_job_resume"),
        Index(
            "ix_resume_ai_extraction_job_claim",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_resume_ai_extraction_job_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_resume_ai_extraction_job_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    job_kind: Mapped[str] = mapped_column(String(32), default="initial")
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    input_facts_version: Mapped[int] = mapped_column(Integer, default=0)
    # A queued task pins the published route that existed when it was created.
    # Existing rows remain nullable until the gateway migration begins using it.
    ai_route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    resume: Mapped[Resume] = relationship(back_populates="ai_extraction_job")
    ai_route_policy_version: Mapped["AiRoutePolicyVersion | None"] = relationship()


class CandidateNameExtractionJob(OrganizationScoped, Base):
    """Durable, source-grounded candidate-name completion work.

    The task is intentionally separate from structured-facts extraction. A
    name-only provider failure must never retract otherwise usable facts,
    scores, summaries, or screening eligibility. One mutable task belongs to
    each resume source so retries remain idempotent and a user-owned name is
    never overwritten.
    """

    __tablename__ = "candidate_name_extraction_jobs"
    __table_args__ = (
        UniqueConstraint(
            "resume_id",
            name="uq_candidate_name_extraction_job_resume",
        ),
        Index(
            "ix_candidate_name_extraction_job_claim",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_candidate_name_extraction_job_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_candidate_name_extraction_job_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    # A queued task pins the published route that existed when it was created.
    # Name extraction is independently retryable, so a later route change
    # cannot silently alter an already queued candidate conclusion.
    ai_route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    resume: Mapped[Resume] = relationship(back_populates="candidate_name_extraction_job")
    ai_route_policy_version: Mapped["AiRoutePolicyVersion | None"] = relationship()


class ResumeSummaryJob(OrganizationScoped, Base):
    """Durable, immutable-facts AI summary work for one resume revision.

    A resume can have several fact revisions.  Each revision owns at most one
    automatic summary task, which makes retries idempotent without conflating
    summary failures with the preceding extraction job.
    """

    __tablename__ = "resume_summary_jobs"
    __table_args__ = (
        UniqueConstraint(
            "resume_id",
            "facts_version",
            name="uq_resume_summary_job_facts_version",
        ),
        Index(
            "ix_resume_summary_job_claim",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_resume_summary_job_lease",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_resume_summary_job_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"),
        index=True,
    )
    facts_version: Mapped[int] = mapped_column(Integer)
    # Queue creation freezes the published route that existed for this facts
    # revision.  A later platform route change cannot silently alter a queued
    # candidate conclusion.
    ai_route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    summary_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_summaries.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    resume: Mapped[Resume] = relationship(back_populates="summary_jobs")
    fact_snapshot: Mapped["ResumeFactSnapshot"] = relationship(
        back_populates="summary_jobs"
    )
    summary: Mapped["ResumeSummary | None"] = relationship()
    ai_route_policy_version: Mapped["AiRoutePolicyVersion | None"] = relationship()


class ResumeSourceBlock(Base):
    __tablename__ = "resume_source_blocks"
    __table_args__ = (UniqueConstraint("resume_id", "block_id", name="uq_resume_block_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    block_id: Mapped[str] = mapped_column(String(64))
    page_no: Mapped[int] = mapped_column(Integer)
    block_type: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)

    resume: Mapped[Resume] = relationship(back_populates="source_blocks")


class ResumeEducation(Base):
    __tablename__ = "resume_educations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    school_name_raw: Mapped[str] = mapped_column(String(255))
    school_key: Mapped[str | None] = mapped_column(String(255), index=True)
    institution_id: Mapped[str | None] = mapped_column(ForeignKey("institutions.id"), index=True)
    school_match_state: Mapped[str] = mapped_column(String(32), default="unmatched")
    degree: Mapped[str] = mapped_column(String(32), index=True)
    major_raw: Mapped[str | None] = mapped_column(String(255))
    major_key: Mapped[str | None] = mapped_column(String(255), index=True)
    start_month: Mapped[str | None] = mapped_column(String(7))
    end_month: Mapped[str | None] = mapped_column(String(7))
    institution_tiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Canonical, education-record-level classification used by the recruiter
    # UI. A candidate may have several education records and therefore several
    # classifications, but a single record never receives overlapping labels
    # such as both 985 and 211.
    institution_classification: Mapped[str | None] = mapped_column(
        String(32),
    )
    classification_basis: Mapped[str | None] = mapped_column(String(32))
    classification_registry_version: Mapped[str | None] = mapped_column(
        String(64)
    )
    classification_evidence_block_ids: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
    )
    average_score: Mapped[float | None] = mapped_column(Float)
    gpa_value: Mapped[float | None] = mapped_column(Float)
    gpa_scale: Mapped[float | None] = mapped_column(Float)
    gpa_percent: Mapped[float | None] = mapped_column(Float, index=True)
    rank_position: Mapped[int | None] = mapped_column(Integer)
    rank_total: Mapped[int | None] = mapped_column(Integer)
    rank_percent: Mapped[float | None] = mapped_column(Float, index=True)
    evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    resume: Mapped[Resume] = relationship(back_populates="educations")


class ResumeExperience(Base):
    __tablename__ = "resume_experiences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    experience_type: Mapped[str] = mapped_column(String(32), index=True)
    # The explicit name of a project, competition, research activity, or
    # employment entry.  It is intentionally separate from the organization
    # and role so all three recruiter-facing concepts remain traceable.
    experience_name_raw: Mapped[str | None] = mapped_column(String(255))
    experience_name_key: Mapped[str | None] = mapped_column(String(255), index=True)
    organization_name_raw: Mapped[str | None] = mapped_column(String(255))
    organization_key: Mapped[str | None] = mapped_column(String(255), index=True)
    title_raw: Mapped[str | None] = mapped_column(String(255))
    title_key: Mapped[str | None] = mapped_column(String(255), index=True)
    start_month: Mapped[str | None] = mapped_column(String(7))
    end_month: Mapped[str | None] = mapped_column(String(7))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    classification_evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Each item is {"detail_raw": str, "evidence_block_ids": list[str]}.
    # V1 does not filter on individual responsibilities, so a JSON column
    # keeps the parent fact and its per-detail proof atomic at save time.
    detail_items: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    leadership_context: Mapped[str | None] = mapped_column(String(32), index=True)
    leadership_role: Mapped[str | None] = mapped_column(String(64), index=True)
    award_level: Mapped[str | None] = mapped_column(String(32), index=True)
    award_result_raw: Mapped[str | None] = mapped_column(String(255))

    resume: Mapped[Resume] = relationship(back_populates="experiences")


class ResumeSkill(Base):
    __tablename__ = "resume_skills"
    __table_args__ = (UniqueConstraint("resume_id", "skill_key", name="uq_resume_skill"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    skill_key: Mapped[str] = mapped_column(String(120), index=True)
    skill_display: Mapped[str] = mapped_column(String(120))
    skill_category: Mapped[str | None] = mapped_column(String(64), index=True)
    evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    resume: Mapped[Resume] = relationship(back_populates="skills")


class ResumeLanguageCredential(Base):
    __tablename__ = "resume_language_credentials"
    __table_args__ = (
        UniqueConstraint(
            "resume_id",
            "credential_code",
            "score",
            name="uq_resume_language_credential",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    credential_code: Mapped[str] = mapped_column(String(32), index=True)
    credential_name_raw: Mapped[str] = mapped_column(String(120))
    score: Mapped[float | None] = mapped_column(Float, index=True)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    resume: Mapped[Resume] = relationship(back_populates="language_credentials")


class ResumeScholarship(Base):
    __tablename__ = "resume_scholarships"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    scholarship_name_raw: Mapped[str] = mapped_column(String(255))
    scholarship_name_key: Mapped[str] = mapped_column(String(255), index=True)
    scholarship_level: Mapped[str | None] = mapped_column(String(32), index=True)
    evidence_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)

    resume: Mapped[Resume] = relationship(back_populates="scholarships")


class ResumeReviewAction(Base):
    __tablename__ = "resume_review_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100), default="single_admin")
    note: Mapped[str | None] = mapped_column(Text)
    old_values: Mapped[dict[str, object] | None] = mapped_column(JSON)
    new_values: Mapped[dict[str, object] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="review_actions")


class ResumeFactSnapshot(OrganizationScoped, Base):
    """Append-only, reproducible representation of one saved facts revision."""

    __tablename__ = "resume_fact_snapshots"
    __table_args__ = (
        UniqueConstraint("resume_id", "facts_version", name="uq_resume_fact_snapshot_version"),
        Index("ix_resume_fact_snapshot_sha256", "facts_sha256"),
        Index("ix_resume_fact_snapshot_organization_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    facts_version: Mapped[int] = mapped_column(Integer)
    canonical_facts_json: Mapped[str] = mapped_column(Text)
    facts_sha256: Mapped[str] = mapped_column(String(64))
    source_block_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="fact_snapshots")
    scores: Mapped[list["ResumeScore"]] = relationship(back_populates="fact_snapshot")
    job_matches: Mapped[list["JobMatch"]] = relationship(back_populates="fact_snapshot")
    summary_jobs: Mapped[list["ResumeSummaryJob"]] = relationship(
        back_populates="fact_snapshot"
    )


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    roster_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    canonical_name: Mapped[str] = mapped_column(String(255))
    canonical_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_985_211: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tier_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    registry_version: Mapped[str] = mapped_column(String(64))

    aliases: Mapped[list["InstitutionAlias"]] = relationship(
        back_populates="institution",
        cascade="all, delete-orphan",
    )


class InstitutionAlias(Base):
    __tablename__ = "institution_aliases"

    alias_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey("institutions.id"), index=True)

    institution: Mapped[Institution] = relationship(back_populates="aliases")


class SavedFilter(OrganizationScoped, Base):
    __tablename__ = "saved_filters"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_saved_filter_organization_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    filters: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class RecruitingAgentConversation(OrganizationScoped, Base):
    """One private, short-lived recruiting-Agent work session.

    The conversation keeps controlled work state plus a deliberately bounded
    short-term transcript.  The transcript records only a recruiter's visible
    input and the final Markdown reply that was shown back to that recruiter.
    It never stores system prompts, graph messages, tool calls or payloads,
    candidate cards, source blocks, or resume text. Candidate membership lives
    in the normalized child table below so a browser can never submit an
    arbitrary set of resume IDs.
    """

    __tablename__ = "recruiting_agent_conversations"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_recruiting_agent_conversation_id_organization",
        ),
        Index(
            "ix_recruiting_agent_conversations_organization_owner_updated",
            "organization_id",
            "owner_user_id",
            "updated_at",
        ),
        Index(
            "ix_recruiting_agent_conversations_organization_expiry",
            "organization_id",
            "expires_at",
        ),
        # Keep explicit names below PostgreSQL's 63-character identifier
        # limit.  The column names themselves intentionally remain verbose.
        Index(
            "ix_agent_conv_active_talent_profile",
            "active_talent_profile_id",
        ),
        Index(
            "ix_agent_conv_active_talent_profile_revision",
            "active_talent_profile_revision_id",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # A workspace can eventually support shared Agent conversations through an
    # explicit ACL.  Until then, a conversation remains private to the user
    # who started it even when multiple recruiters share one organization.
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("user_accounts.id"),
        index=True,
    )
    active_job_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_versions.id"),
        nullable=True,
        index=True,
    )
    # The reference is verified against ``id + organization + conversation``
    # whenever it is read.  Keeping it as a scalar avoids a circular foreign
    # key while the candidate-set table still has a composite tenant FK back
    # to this conversation.
    active_candidate_set_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    # A direct Agent request can create or refine a confirmation-first talent
    # profile.  Keep only opaque references here: profile contents remain in
    # their own workspace-scoped, revisioned records and chat messages never
    # enter this table.
    active_talent_profile_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    active_talent_profile_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    # Protect two browser tabs from silently replacing each other's "just now"
    # candidate scope.  The client returns this value on the next turn.
    context_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    # ``context_version`` is both the recruiter-visible optimistic-concurrency
    # token and SQLAlchemy's row version. Every turn advances it, so a stale
    # browser tab cannot silently replace another tab's saved candidate scope.
    # The service owns the increment explicitly to keep the returned token
    # deterministic and to avoid a hidden ORM-generated value.
    __mapper_args__ = {
        "version_id_col": context_version,
        "version_id_generator": False,
    }
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    candidate_sets: Mapped[list["RecruitingAgentCandidateSet"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        foreign_keys="RecruitingAgentCandidateSet.conversation_id",
    )
    turns: Mapped[list["RecruitingAgentConversationTurn"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        foreign_keys="RecruitingAgentConversationTurn.conversation_id",
    )


class RecruitingAgentConversationTurn(OrganizationScoped, Base):
    """One completed, recruiter-visible turn in a private Agent session.

    The row is intentionally a complete user/assistant pair rather than a
    partial message stream. A failed model call or stale browser tab therefore
    cannot leave an orphaned prompt that later changes the meaning of a retry.
    ``context_version`` is the parent's post-turn version and gives each turn
    a deterministic order while the parent row is locked by the service.
    """

    __tablename__ = "recruiting_agent_conversation_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            [
                "recruiting_agent_conversations.id",
                "recruiting_agent_conversations.organization_id",
            ],
            name="fk_agent_turn_conversation_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "conversation_id",
            "organization_id",
            "context_version",
            name="uq_agent_turn_org_conversation_version",
        ),
        Index(
            "ix_agent_turn_org_conversation_version",
            "organization_id",
            "conversation_id",
            "context_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    context_version: Mapped[int] = mapped_column(Integer)
    user_message: Mapped[str] = mapped_column(Text)
    assistant_message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[RecruitingAgentConversation] = relationship(
        back_populates="turns",
        foreign_keys=[conversation_id],
        primaryjoin=(
            "and_(RecruitingAgentConversationTurn.conversation_id == "
            "RecruitingAgentConversation.id, RecruitingAgentConversationTurn.organization_id == "
            "RecruitingAgentConversation.organization_id)"
        ),
    )


class RecruitingAgentCandidateSet(OrganizationScoped, Base):
    """A frozen, server-derived candidate scope for one Agent conversation."""

    __tablename__ = "recruiting_agent_candidate_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            [
                "recruiting_agent_conversations.id",
                "recruiting_agent_conversations.organization_id",
            ],
            name="fk_recruiting_agent_candidate_set_conversation_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_recruiting_agent_candidate_set_id_organization",
        ),
        Index(
            "ix_agent_sets_org_conv_created",
            "organization_id",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    # ``agent_search`` means visible results from a normal Agent search.
    # ``candidate_filter`` means a full server-reconstructed sidebar filter
    # result. ``talent_search_run`` means the recall set from one confirmed
    # talent-profile run.
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    # Source IDs are opaque internal references only.  They are never accepted
    # as a resume selection from the browser and do not contain candidate data.
    source_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[RecruitingAgentConversation] = relationship(
        back_populates="candidate_sets",
        foreign_keys=[conversation_id],
        primaryjoin=(
            "and_(RecruitingAgentCandidateSet.conversation_id == "
            "RecruitingAgentConversation.id, RecruitingAgentCandidateSet.organization_id == "
            "RecruitingAgentConversation.organization_id)"
        ),
    )
    items: Mapped[list["RecruitingAgentCandidateSetItem"]] = relationship(
        back_populates="candidate_set",
        cascade="all, delete-orphan",
        foreign_keys="RecruitingAgentCandidateSetItem.candidate_set_id",
    )


class RecruitingAgentCandidateSetItem(OrganizationScoped, Base):
    """One opaque resume reference inside a frozen Agent candidate set."""

    __tablename__ = "recruiting_agent_candidate_set_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_set_id", "organization_id"],
            [
                "recruiting_agent_candidate_sets.id",
                "recruiting_agent_candidate_sets.organization_id",
            ],
            name="fk_recruiting_agent_candidate_set_item_set_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "candidate_set_id",
            "resume_id",
            name="uq_recruiting_agent_candidate_set_item_resume",
        ),
        Index(
            "ix_agent_set_items_org_set_ordinal",
            "organization_id",
            "candidate_set_id",
            "ordinal",
        ),
        Index(
            "ix_recruiting_agent_candidate_set_items_organization_resume",
            "organization_id",
            "resume_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_set_id: Mapped[str] = mapped_column(String(36), index=True)
    # This is intentionally not a relationship to Resume.  A deleted/purged
    # resume must not be revived through historical chat state; every read
    # re-checks normal tenant and candidate-lifecycle visibility instead.
    resume_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    candidate_set: Mapped[RecruitingAgentCandidateSet] = relationship(
        back_populates="items",
        foreign_keys=[candidate_set_id],
        primaryjoin=(
            "and_(RecruitingAgentCandidateSetItem.candidate_set_id == "
            "RecruitingAgentCandidateSet.id, RecruitingAgentCandidateSetItem.organization_id == "
            "RecruitingAgentCandidateSet.organization_id)"
        ),
    )


class TalentSearchProfile(OrganizationScoped, Base):
    """A recruiter-confirmed, versioned AI talent-search brief.

    This is deliberately distinct from a published JD.  A confirmed revision
    may point to an internal ``JobVersion`` solely so the established,
    evidence-grounded JD matching worker can perform the expensive precision
    evaluation after deterministic candidate recall.
    """

    __tablename__ = "talent_search_profiles"
    __table_args__ = (
        Index(
            "ix_talent_search_profiles_organization_updated",
            "organization_id",
            "updated_at",
        ),
        Index(
            "ix_talent_search_profiles_organization_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(32), default="freeform", index=True)
    source_job_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_versions.id"),
        nullable=True,
        index=True,
    )
    original_request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    current_revision_number: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_revision_number: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    revisions: Mapped[list["TalentSearchProfileRevision"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
    )
    runs: Mapped[list["TalentSearchRun"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
    )


class TalentSearchProfileRevision(OrganizationScoped, Base):
    """An immutable AI- or recruiter-refined version of one search brief."""

    __tablename__ = "talent_search_profile_revisions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "revision_number",
            name="uq_talent_search_profile_revision_number",
        ),
        Index(
            "ix_talent_search_profile_revisions_organization_profile",
            "organization_id",
            "profile_id",
            "revision_number",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("talent_search_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="ai_generated")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    hard_filters: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    verification_requirements: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        default=list,
    )
    preferred_requirements: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        default=list,
    )
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    clarifying_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # The private job version is never included in normal JD lists.  It only
    # carries confirmed, source-readable requirements into the existing
    # evidence-based precision matching worker.
    match_job_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_versions.id"),
        nullable=True,
        index=True,
    )
    model_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )

    profile: Mapped[TalentSearchProfile] = relationship(back_populates="revisions")


class TalentSearchRun(OrganizationScoped, Base):
    """One confirmed talent-profile recall plus optional precision batch."""

    __tablename__ = "talent_search_runs"
    __table_args__ = (
        Index(
            "ix_talent_search_runs_organization_profile_created",
            "organization_id",
            "profile_id",
            "created_at",
        ),
        Index(
            "ix_talent_search_runs_organization_status",
            "organization_id",
            "status",
        ),
        # A confirmed revision can be run globally and inside one frozen
        # initial-filter scope.  Keep their identities separate so a global
        # result is never reused for a narrower Agent request (or vice versa).
        Index(
            "ix_talent_search_runs_organization_revision_scope",
            "organization_id",
            "revision_id",
            "scope_kind",
            "scope_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("talent_search_profiles.id", ondelete="CASCADE"),
        index=True,
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("talent_search_profile_revisions.id"),
        index=True,
    )
    # ``global`` is the historical profile workflow. ``candidate_filter`` is
    # a private Agent run constrained to a frozen, server-derived sidebar
    # result.  The digest is derived from opaque visible resume IDs and never
    # stores the browser's original query, prompt, candidate names, or text.
    scope_kind: Mapped[str] = mapped_column(
        String(32),
        default="global",
        server_default=text("'global'"),
        index=True,
    )
    scope_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )
    scope_candidate_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    hard_filter_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    # Server-derived counts for the strict-recall funnel.  Keeping this on the
    # run makes a historic zero-result explanation stable even after the
    # profile is refined or more resumes are later uploaded.
    recall_diagnostics: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    # This is the server-derived strict-recall target set used to constrain the
    # later semantic match batch.  It is never accepted from the browser.
    recalled_resume_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total_recalled_count: Mapped[int] = mapped_column(Integer, default=0)
    job_match_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_match_batches.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    profile: Mapped[TalentSearchProfile] = relationship(back_populates="runs")


class ScoreTemplate(OrganizationScoped, Base):
    __tablename__ = "score_templates"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_score_template_organization_name"),
        Index("ix_score_templates_organization_archived", "organization_id", "is_archived"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    dimensions: Mapped[list["ScoreTemplateDimension"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
    )
    scores: Mapped[list["ResumeScore"]] = relationship(back_populates="template")


class ScoreTemplateDimension(Base):
    __tablename__ = "score_template_dimensions"
    __table_args__ = (
        UniqueConstraint("template_id", "key", name="uq_score_template_dimension_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(ForeignKey("score_templates.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120))
    weight: Mapped[int] = mapped_column(Integer)
    guidance: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)

    template: Mapped[ScoreTemplate] = relationship(back_populates="dimensions")


class ResumeScore(OrganizationScoped, Base):
    __tablename__ = "resume_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"),
        nullable=True,
        index=True,
    )
    template_id: Mapped[str] = mapped_column(ForeignKey("score_templates.id"), index=True)
    facts_version: Mapped[int] = mapped_column(Integer)
    template_version: Mapped[int] = mapped_column(Integer)
    total_score: Mapped[float] = mapped_column(Float)
    ai_total_score: Mapped[float | None] = mapped_column(Float)
    dimension_scores: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    analysis: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="scores")
    fact_snapshot: Mapped[ResumeFactSnapshot | None] = relationship(back_populates="scores")
    template: Mapped[ScoreTemplate] = relationship(back_populates="scores")


class ResumeScoreBatch(OrganizationScoped, Base):
    """A durable, template-scoped batch of AI resume scores.

    Each batch freezes the template version at request time.  Individual
    items keep their own immutable resume fact version so the worker can
    safely retry without ever scoring facts from another workspace or a newer
    resume version by accident.
    """

    __tablename__ = "resume_score_batches"
    __table_args__ = (
        Index(
            "ix_resume_score_batches_organization_claim",
            "organization_id",
            "status",
            "lease_expires_at",
        ),
        Index(
            "uq_resume_score_batches_active_template",
            "organization_id",
            "template_id",
            "template_version",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    template_id: Mapped[str] = mapped_column(ForeignKey("score_templates.id"), index=True)
    template_version: Mapped[int] = mapped_column(Integer)
    ai_route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    cached_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    template: Mapped[ScoreTemplate] = relationship()
    ai_route_policy_version: Mapped["AiRoutePolicyVersion | None"] = relationship()
    items: Mapped[list["ResumeScoreBatchItem"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class ResumeScoreBatchItem(OrganizationScoped, Base):
    """One resume's durable place in a score batch."""

    __tablename__ = "resume_score_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "resume_id", name="uq_resume_score_batch_item_resume"),
        Index("ix_resume_score_batch_item_claim", "status", "next_attempt_at"),
        Index(
            "ix_resume_score_batch_item_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("resume_score_batches.id"), index=True)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"), index=True
    )
    facts_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    resume_score_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_scores.id"), nullable=True
    )
    was_cached: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    batch: Mapped[ResumeScoreBatch] = relationship(back_populates="items")
    resume: Mapped[Resume] = relationship()
    fact_snapshot: Mapped[ResumeFactSnapshot] = relationship()
    resume_score: Mapped[ResumeScore | None] = relationship()


class ResumeSummary(OrganizationScoped, Base):
    __tablename__ = "resume_summaries"
    __table_args__ = (
        Index(
            "uq_current_resume_summary",
            "resume_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
        Index("ix_resume_summaries_organization_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"),
        nullable=True,
        index=True,
    )
    facts_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(
        String(32),
        default="ai",
        server_default=text("'ai'"),
    )
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_summaries.id"),
        nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="summaries")
    fact_snapshot: Mapped[ResumeFactSnapshot | None] = relationship()


class RecruitingWorkflow(OrganizationScoped, Base):
    """A reusable recruiting-process template owned by one workspace.

    The workflow is only the stable template identity.  Recruitable jobs and
    candidate applications always bind an immutable ``RecruitingWorkflowVersion``
    so editing a later version cannot rewrite a live candidate's process.
    """

    __tablename__ = "recruiting_workflows"
    __table_args__ = (
        # The otherwise redundant composite key is intentional: child rows use
        # it for a database-enforced workspace-bound foreign key.
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_recruiting_workflows_id_organization",
        ),
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_recruiting_workflows_organization_name",
        ),
        Index(
            "ix_recruiting_workflows_organization_updated",
            "organization_id",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    versions: Mapped[list["RecruitingWorkflowVersion"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        foreign_keys="RecruitingWorkflowVersion.workflow_id",
    )


class RecruitingWorkflowVersion(OrganizationScoped, Base):
    """An immutable, publishable revision of a recruiting workflow template."""

    __tablename__ = "recruiting_workflow_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_id", "organization_id"],
            [
                "recruiting_workflows.id",
                "recruiting_workflows.organization_id",
            ],
            name="fk_recruiting_workflow_versions_workflow_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_recruiting_workflow_versions_id_organization",
        ),
        UniqueConstraint(
            "workflow_id",
            "version",
            name="uq_recruiting_workflow_version",
        ),
        CheckConstraint("version >= 1", name="ck_recruiting_workflow_version_positive"),
        Index(
            "ix_recruiting_workflow_versions_organization_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # The composite FK above carries the tenant boundary.  Do not add a second
    # single-column FK here: it would make accidental cross-workspace joins
    # appear valid to the database.
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # Application-validated values: draft, published, archived.  A published
    # version is never edited in place; create a new version instead.
    status: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        server_default=text("'draft'"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[RecruitingWorkflow] = relationship(
        back_populates="versions",
        foreign_keys=[workflow_id],
        primaryjoin=(
            "and_(RecruitingWorkflowVersion.workflow_id == RecruitingWorkflow.id, "
            "RecruitingWorkflowVersion.organization_id == RecruitingWorkflow.organization_id)"
        ),
    )
    stages: Mapped[list["RecruitingWorkflowStage"]] = relationship(
        back_populates="workflow_version",
        cascade="all, delete-orphan",
        foreign_keys="RecruitingWorkflowStage.workflow_version_id",
    )
    applications: Mapped[list["JobApplication"]] = relationship(
        back_populates="workflow_version",
        foreign_keys="JobApplication.workflow_version_id",
        primaryjoin=(
            "and_(RecruitingWorkflowVersion.id == JobApplication.workflow_version_id, "
            "RecruitingWorkflowVersion.organization_id == JobApplication.organization_id)"
        ),
    )


class RecruitingWorkflowStage(OrganizationScoped, Base):
    """One immutable stage inside a workflow version.

    ``stage_type`` distinguishes normal ordered stages from the two terminal
    outcomes.  This is necessary because a recruiter must be able to manually
    mark a candidate as eliminated from an early stage without pretending the
    process is a single linear list.
    """

    __tablename__ = "recruiting_workflow_stages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_version_id", "organization_id"],
            [
                "recruiting_workflow_versions.id",
                "recruiting_workflow_versions.organization_id",
            ],
            name="fk_recruiting_workflow_stages_version_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_recruiting_workflow_stages_id_organization",
        ),
        UniqueConstraint(
            "workflow_version_id",
            "stage_key",
            name="uq_recruiting_workflow_stage_key",
        ),
        UniqueConstraint(
            "workflow_version_id",
            "sort_order",
            name="uq_recruiting_workflow_stage_order",
        ),
        CheckConstraint("sort_order >= 0", name="ck_recruiting_workflow_stage_order"),
        Index(
            "ix_recruiting_workflow_stages_organization_version_order",
            "organization_id",
            "workflow_version_id",
            "sort_order",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_version_id: Mapped[str] = mapped_column(String(36), index=True)
    stage_key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    # Application-validated values: active, hired, rejected.
    stage_type: Mapped[str] = mapped_column(String(32), default="active")
    sort_order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workflow_version: Mapped[RecruitingWorkflowVersion] = relationship(
        back_populates="stages",
        foreign_keys=[workflow_version_id],
        primaryjoin=(
            "and_(RecruitingWorkflowStage.workflow_version_id == "
            "RecruitingWorkflowVersion.id, RecruitingWorkflowStage.organization_id == "
            "RecruitingWorkflowVersion.organization_id)"
        ),
    )


class Job(OrganizationScoped, Base):
    """Current-version cache; immutable JD evidence lives in ``JobVersion``."""

    __tablename__ = "jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["recruiting_workflow_version_id", "organization_id"],
            [
                "recruiting_workflow_versions.id",
                "recruiting_workflow_versions.organization_id",
            ],
            name="fk_jobs_recruiting_workflow_version_organization",
            ondelete="RESTRICT",
        ),
        CheckConstraint("hc_total >= 1", name="ck_jobs_hc_total_positive"),
        Index("ix_jobs_organization_updated", "organization_id", "updated_at"),
        Index("ix_jobs_organization_kind_updated", "organization_id", "kind", "updated_at"),
        Index(
            "ix_jobs_organization_recruiting_status",
            "organization_id",
            "recruiting_status",
        ),
        Index(
            "ix_jobs_organization_owner_user",
            "organization_id",
            "owner_user_id",
        ),
        Index(
            "ix_jobs_organization_recruiting_workflow",
            "organization_id",
            "recruiting_workflow_version_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # ``talent_search_profile`` rows are internal requirement carriers. They
    # never appear in the normal JD workspace, even though they deliberately
    # reuse the same evidence-grounded matching engine.
    kind: Mapped[str] = mapped_column(
        String(32),
        default="job",
        server_default=text("'job'"),
    )
    title: Mapped[str] = mapped_column(String(200))
    jd_text: Mapped[str] = mapped_column(Text)
    requirements: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # This is the recruiting lifecycle of the position.  It is intentionally
    # separate from JobVersion.status (draft/confirmed/archived), which only
    # describes whether one immutable JD revision is usable for matching.
    recruiting_status: Mapped[str] = mapped_column(
        String(32),
        # Existing user-created JDs are publishable/usable by default.  A
        # caller that is composing an unpublished position explicitly sets
        # ``draft`` through the recruiting settings service.
        default="open",
        server_default=text("'open'"),
        index=True,
    )
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The service validates this user has an active membership in this Job's
    # organization before it may become the responsible recruiter.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    hc_total: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    # This is the default process for *future* applications only.  Each
    # JobApplication retains its own workflow-version snapshot.
    recruiting_workflow_version_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    matches: Mapped[list["JobMatch"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list["JobVersion"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    owner_user: Mapped[UserAccount | None] = relationship(
        foreign_keys=[owner_user_id],
    )
    recruiting_workflow_version: Mapped[RecruitingWorkflowVersion | None] = relationship(
        foreign_keys=[recruiting_workflow_version_id],
        primaryjoin=(
            "and_(Job.recruiting_workflow_version_id == RecruitingWorkflowVersion.id, "
            "Job.organization_id == RecruitingWorkflowVersion.organization_id)"
        ),
    )
    applications: Mapped[list["JobApplication"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        foreign_keys="JobApplication.job_id",
    )


class JobVersion(OrganizationScoped, Base):
    __tablename__ = "job_versions"
    __table_args__ = (
        UniqueConstraint("job_id", "version", name="uq_job_version"),
        Index("ix_job_versions_organization_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    raw_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="versions")
    clauses: Mapped[list["JobSourceClause"]] = relationship(
        back_populates="job_version",
        cascade="all, delete-orphan",
    )
    requirements: Mapped[list["JobRequirement"]] = relationship(
        back_populates="job_version",
        cascade="all, delete-orphan",
    )
    matches: Mapped[list["JobMatch"]] = relationship(back_populates="job_version_record")


class JobApplication(OrganizationScoped, Base):
    """One candidate's independent, manually managed record for one job.

    This deliberately is not a favorite and does not duplicate candidate facts.
    It only pins the immutable JD, workflow, and resume-fact revisions that
    were visible when a recruiter added the candidate to the position.
    """

    __tablename__ = "job_applications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_version_id", "organization_id"],
            [
                "recruiting_workflow_versions.id",
                "recruiting_workflow_versions.organization_id",
            ],
            name="fk_job_applications_workflow_version_organization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["current_stage_id", "organization_id"],
            [
                "recruiting_workflow_stages.id",
                "recruiting_workflow_stages.organization_id",
            ],
            name="fk_job_applications_current_stage_organization",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_job_applications_id_organization",
        ),
        UniqueConstraint(
            "organization_id",
            "job_id",
            "candidate_id",
            "round_number",
            name="uq_job_application_round",
        ),
        CheckConstraint(
            "round_number >= 1",
            name="ck_job_applications_round_positive",
        ),
        CheckConstraint(
            "resume_facts_version >= 0",
            name="ck_job_applications_resume_facts_version",
        ),
        CheckConstraint(
            "job_version_number >= 1",
            name="ck_job_applications_job_version_positive",
        ),
        CheckConstraint(
            "workflow_version_number >= 1",
            name="ck_job_applications_workflow_version_positive",
        ),
        CheckConstraint(
            "state_version >= 1",
            name="ck_job_applications_state_version_positive",
        ),
        Index(
            "uq_current_job_application_candidate",
            "organization_id",
            "job_id",
            "candidate_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
        Index(
            "ix_job_applications_organization_job_stage",
            "organization_id",
            "job_id",
            "current_stage_id",
        ),
        Index(
            "ix_job_applications_organization_candidate_created",
            "organization_id",
            "candidate_id",
            "created_at",
        ),
        Index(
            "ix_job_applications_organization_resume",
            "organization_id",
            "resume_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    resume_fact_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"),
        index=True,
    )
    resume_facts_version: Mapped[int] = mapped_column(Integer)
    job_version_id: Mapped[str] = mapped_column(ForeignKey("job_versions.id"), index=True)
    job_version_number: Mapped[int] = mapped_column(Integer)
    workflow_version_id: Mapped[str] = mapped_column(String(36), index=True)
    workflow_version_number: Mapped[int] = mapped_column(Integer)
    current_stage_id: Mapped[str] = mapped_column(String(36), index=True)
    # Keep recruiter-visible stage labels as small snapshots.  These are not
    # resume data and allow historical transitions to remain intelligible even
    # after a later workflow version changes its stage wording.
    current_stage_key: Mapped[str] = mapped_column(String(64))
    current_stage_name: Mapped[str] = mapped_column(String(120))
    current_stage_type: Mapped[str] = mapped_column(String(32))
    # Application-validated values: active, hired, rejected, withdrawn.
    status: Mapped[str] = mapped_column(
        String(32),
        default="active",
        server_default=text("'active'"),
        index=True,
    )
    # A terminal historical record remains current until a recruiter explicitly
    # adds the candidate again.  Re-application marks the old row false, then
    # creates the next round number.
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        index=True,
    )
    round_number: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # This is both the client-facing optimistic-concurrency token and the ORM
    # version column.  The stage-flow service increments it explicitly.
    state_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    added_by_user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    __mapper_args__ = {
        "version_id_col": state_version,
        "version_id_generator": False,
    }

    job: Mapped[Job] = relationship(back_populates="applications", foreign_keys=[job_id])
    candidate: Mapped[Candidate] = relationship(foreign_keys=[candidate_id])
    resume: Mapped[Resume] = relationship(foreign_keys=[resume_id])
    resume_fact_snapshot: Mapped[ResumeFactSnapshot] = relationship(
        foreign_keys=[resume_fact_snapshot_id]
    )
    job_version_record: Mapped[JobVersion] = relationship(foreign_keys=[job_version_id])
    workflow_version: Mapped[RecruitingWorkflowVersion] = relationship(
        back_populates="applications",
        foreign_keys=[workflow_version_id],
        primaryjoin=(
            "and_(JobApplication.workflow_version_id == RecruitingWorkflowVersion.id, "
            "JobApplication.organization_id == RecruitingWorkflowVersion.organization_id)"
        ),
    )
    current_stage: Mapped[RecruitingWorkflowStage] = relationship(
        foreign_keys=[current_stage_id],
        primaryjoin=(
            "and_(JobApplication.current_stage_id == RecruitingWorkflowStage.id, "
            "JobApplication.organization_id == RecruitingWorkflowStage.organization_id)"
        ),
    )
    added_by_user: Mapped[UserAccount] = relationship(foreign_keys=[added_by_user_id])
    stage_transitions: Mapped[list["JobApplicationStageTransition"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        foreign_keys="JobApplicationStageTransition.application_id",
        primaryjoin=(
            "and_(JobApplication.id == JobApplicationStageTransition.application_id, "
            "JobApplication.organization_id == JobApplicationStageTransition.organization_id)"
        ),
    )


class JobApplicationStageTransition(OrganizationScoped, Base):
    """Append-only record of one human-controlled application stage change."""

    __tablename__ = "job_application_stage_transitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "organization_id"],
            ["job_applications.id", "job_applications.organization_id"],
            name="fk_job_application_stage_transitions_application_organization",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "application_id",
            "state_version_after",
            name="uq_job_application_stage_transition_version",
        ),
        CheckConstraint(
            "state_version_after >= 1",
            name="ck_job_application_stage_transition_version_positive",
        ),
        Index(
            "ix_job_application_transition_org_app_version",
            "organization_id",
            "application_id",
            "state_version_after",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), index=True)
    state_version_after: Mapped[int] = mapped_column(Integer)
    # The stage ID/key/name/type values are snapshots rather than live stage
    # relationships.  A process history therefore never depends on mutating a
    # template version and contains no copied resume facts or source text.
    from_stage_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_stage_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_stage_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    from_stage_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_stage_id: Mapped[str] = mapped_column(String(36))
    to_stage_key: Mapped[str] = mapped_column(String(64))
    to_stage_name: Mapped[str] = mapped_column(String(120))
    to_stage_type: Mapped[str] = mapped_column(String(32))
    # Application-validated values: initial, advance, return, hire, reject.
    action: Mapped[str] = mapped_column(String(32))
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    application: Mapped[JobApplication] = relationship(
        back_populates="stage_transitions",
        foreign_keys=[application_id],
        primaryjoin=(
            "and_(JobApplicationStageTransition.application_id == JobApplication.id, "
            "JobApplicationStageTransition.organization_id == JobApplication.organization_id)"
        ),
    )
    actor_user: Mapped[UserAccount] = relationship(foreign_keys=[actor_user_id])


class JobSourceClause(Base):
    __tablename__ = "job_source_clauses"
    __table_args__ = (
        UniqueConstraint("job_version_id", "clause_id", name="uq_job_clause_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_version_id: Mapped[str] = mapped_column(ForeignKey("job_versions.id"), index=True)
    clause_id: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)

    job_version: Mapped[JobVersion] = relationship(back_populates="clauses")


class JobRequirement(Base):
    __tablename__ = "job_requirements"
    __table_args__ = (
        UniqueConstraint("job_version_id", "requirement_key", name="uq_job_requirement_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_version_id: Mapped[str] = mapped_column(ForeignKey("job_versions.id"), index=True)
    requirement_key: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    raw_requirement: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    minimum_months: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[int] = mapped_column(Integer)
    clause_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer)

    job_version: Mapped[JobVersion] = relationship(back_populates="requirements")
    match_results: Mapped[list["JobMatchRequirementResult"]] = relationship(
        back_populates="requirement",
    )


class JobMatch(OrganizationScoped, Base):
    __tablename__ = "job_matches"
    __table_args__ = (
        Index("ix_job_matches_organization_created", "organization_id", "created_at"),
        Index("ix_job_matches_organization_job", "organization_id", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_versions.id"),
        nullable=True,
        index=True,
    )
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_fact_snapshots.id"),
        nullable=True,
        index=True,
    )
    facts_version: Mapped[int] = mapped_column(Integer)
    job_version: Mapped[int] = mapped_column(Integer)
    total_score: Mapped[float] = mapped_column(Float)
    must_have_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evidence_coverage: Mapped[float | None] = mapped_column(Float)
    hard_requirement_status: Mapped[str | None] = mapped_column(String(32))
    analysis: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="matches")
    job_version_record: Mapped[JobVersion | None] = relationship(back_populates="matches")
    resume: Mapped[Resume] = relationship(back_populates="job_matches")
    fact_snapshot: Mapped[ResumeFactSnapshot | None] = relationship(back_populates="job_matches")
    requirement_results: Mapped[list["JobMatchRequirementResult"]] = relationship(
        back_populates="job_match",
        cascade="all, delete-orphan",
    )


class JobMatchRequirementResult(Base):
    __tablename__ = "job_match_requirement_results"
    __table_args__ = (
        UniqueConstraint("job_match_id", "requirement_id", name="uq_job_match_requirement"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_match_id: Mapped[str] = mapped_column(ForeignKey("job_matches.id"), index=True)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("job_requirements.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_or_uncertain: Mapped[str | None] = mapped_column(Text)
    score_contribution: Mapped[float] = mapped_column(Float)

    job_match: Mapped[JobMatch] = relationship(back_populates="requirement_results")
    requirement: Mapped[JobRequirement] = relationship(back_populates="match_results")


class JobMatchBatch(OrganizationScoped, Base):
    """A durable, JD-version-scoped batch of AI resume matches."""

    __tablename__ = "job_match_batches"
    __table_args__ = (
        Index(
            "ix_job_match_batches_organization_claim",
            "organization_id",
            "status",
            "lease_expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_version_id: Mapped[str] = mapped_column(ForeignKey("job_versions.id"), index=True)
    ai_route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job_version_record: Mapped[JobVersion] = relationship()
    ai_route_policy_version: Mapped["AiRoutePolicyVersion | None"] = relationship()
    items: Mapped[list["JobMatchBatchItem"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
    )


class JobMatchBatchItem(OrganizationScoped, Base):
    __tablename__ = "job_match_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "resume_id", name="uq_job_match_batch_item_resume"),
        Index("ix_job_match_batch_item_claim", "status", "next_attempt_at"),
        Index(
            "ix_job_match_batch_item_organization_claim",
            "organization_id",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("job_match_batches.id"), index=True)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id"), index=True)
    fact_snapshot_id: Mapped[str] = mapped_column(ForeignKey("resume_fact_snapshots.id"), index=True)
    facts_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    job_match_id: Mapped[str | None] = mapped_column(ForeignKey("job_matches.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    batch: Mapped[JobMatchBatch] = relationship(back_populates="items")
    resume: Mapped[Resume] = relationship()
    fact_snapshot: Mapped[ResumeFactSnapshot] = relationship()
    job_match: Mapped[JobMatch | None] = relationship()


class AiProviderProfile(Base):
    """A platform-managed connection profile for one AI/OCR provider protocol.

    ``credential_ref`` is only a reference to a server-side secret (for
    example, an environment-variable or secret-manager key).  It must never
    contain a credential value.  Request defaults are likewise limited to
    non-secret protocol defaults.
    """

    __tablename__ = "ai_provider_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    driver: Mapped[str] = mapped_column(String(64), index=True)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_defaults_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    model_profiles: Mapped[list["AiModelProfile"]] = relationship(
        back_populates="provider_profile",
    )
    api_invocations: Mapped[list["ApiInvocation"]] = relationship(
        back_populates="provider_profile",
    )


class AiModelProfile(Base):
    """A selectable model/service profile, independent from business features."""

    __tablename__ = "ai_model_profiles"
    __table_args__ = (
        UniqueConstraint(
            "provider_profile_id",
            "provider_model_id",
            name="uq_ai_model_profile_provider_model",
        ),
        Index(
            "ix_ai_model_profiles_provider_enabled",
            "provider_profile_id",
            "enabled",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_profile_id: Mapped[str] = mapped_column(
        ForeignKey("ai_provider_profiles.id"),
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    provider_model_id: Mapped[str] = mapped_column(String(255))
    capabilities_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_classification_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    provider_profile: Mapped[AiProviderProfile] = relationship(
        back_populates="model_profiles",
    )
    price_versions: Mapped[list["AiModelPriceVersion"]] = relationship(
        back_populates="model_profile",
    )
    api_invocations: Mapped[list["ApiInvocation"]] = relationship(
        back_populates="model_profile",
    )


class AiModelPriceVersion(Base):
    """An immutable, platform-owned price rule snapshot for a model profile.

    Token prices are expressed per one million tokens in ``currency``.  The
    request and page prices are expressed per one request/page respectively.
    Nullable units deliberately support providers such as OCR that do not
    publish LLM token prices.  Historical invocation rows store their own
    price snapshot and never recalculate from this table.
    """

    __tablename__ = "ai_model_price_versions"
    __table_args__ = (
        UniqueConstraint(
            "model_profile_id",
            "version",
            name="uq_ai_model_price_version",
        ),
        Index(
            "ix_ai_model_price_versions_model_active_effective",
            "model_profile_id",
            "is_active",
            "effective_from",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("ai_model_profiles.id"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    currency: Mapped[str] = mapped_column(String(3))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_price_per_million: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    output_price_per_million: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cached_read_input_price_per_million: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8)
    )
    cached_write_input_price_per_million: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8)
    )
    reasoning_price_per_million: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    request_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    page_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    source: Mapped[str] = mapped_column(String(1024))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    model_profile: Mapped[AiModelProfile] = relationship(back_populates="price_versions")
    created_by_user: Mapped[UserAccount | None] = relationship(
        back_populates="created_ai_model_price_versions",
        foreign_keys=[created_by_user_id],
    )
    api_invocations: Mapped[list["ApiInvocation"]] = relationship(
        back_populates="price_version",
    )


class AiRoutePolicy(Base):
    """The stable, platform-owned policy container for one business feature."""

    __tablename__ = "ai_route_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    feature: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "ai_route_policy_versions.id",
            name="fk_ai_route_policies_active_version",
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    versions: Mapped[list["AiRoutePolicyVersion"]] = relationship(
        back_populates="policy",
        foreign_keys="AiRoutePolicyVersion.policy_id",
    )
    active_version: Mapped["AiRoutePolicyVersion | None"] = relationship(
        foreign_keys=[active_version_id],
        post_update=True,
    )


class AiRoutePolicyVersion(Base):
    """An append-only, publishable routing and retry policy snapshot."""

    __tablename__ = "ai_route_policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_ai_route_policy_version"),
        Index("ix_ai_route_policy_versions_policy_status", "policy_id", "status"),
        Index("ix_ai_route_policy_versions_published", "status", "published_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("ai_route_policies.id"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    # The ordered targets and retry/fallback rules are intentionally explicit
    # snapshots.  They contain profile IDs and limits, never key values.
    targets_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    retry_policy_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    max_cost_guard_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    prompt_revision: Mapped[str | None] = mapped_column(String(120), nullable=True)
    published_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    policy: Mapped[AiRoutePolicy] = relationship(
        back_populates="versions",
        foreign_keys=[policy_id],
    )
    published_by_user: Mapped[UserAccount | None] = relationship(
        back_populates="published_ai_route_policy_versions",
        foreign_keys=[published_by_user_id],
    )
    supersedes_version: Mapped["AiRoutePolicyVersion | None"] = relationship(
        back_populates="superseded_by_versions",
        foreign_keys=[supersedes_version_id],
        remote_side="AiRoutePolicyVersion.id",
    )
    superseded_by_versions: Mapped[list["AiRoutePolicyVersion"]] = relationship(
        back_populates="supersedes_version",
        foreign_keys="AiRoutePolicyVersion.supersedes_version_id",
    )
    ai_runs: Mapped[list["AiRun"]] = relationship(
        back_populates="route_policy_version",
    )


class AiRun(OrganizationScoped, Base):
    """A tenant-scoped root record for one business-level AI/OCR action.

    It stores safe correlation and version metadata only.  Prompts, candidate
    source text, tool arguments, model output, headers, and provider keys are
    intentionally absent from this durable ledger.
    """

    __tablename__ = "ai_runs"
    __table_args__ = (
        # This redundant candidate key lets child ledger rows enforce that a
        # run and its invocation share the same workspace at the database
        # level, not merely through service-layer filtering.
        UniqueConstraint("id", "organization_id", name="uq_ai_run_id_organization"),
        Index("ix_ai_runs_organization_created", "organization_id", "created_at"),
        Index(
            "ix_ai_runs_organization_feature_started",
            "organization_id",
            "feature",
            "started_at",
        ),
        Index(
            "ix_ai_runs_organization_status_started",
            "organization_id",
            "status",
            "started_at",
        ),
        Index("ix_ai_runs_organization_business_ref", "organization_id", "business_ref_type", "business_ref_id"),
        Index("ix_ai_runs_correlation", "correlation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    feature: Mapped[str] = mapped_column(String(64), index=True)
    service_kind: Mapped[str] = mapped_column(String(32), default="llm", index=True)
    business_ref_type: Mapped[str] = mapped_column(String(64))
    business_ref_id: Mapped[str] = mapped_column(String(128))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    route_policy_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_route_policy_versions.id"),
        nullable=True,
        index=True,
    )
    # Prompt bodies remain code-controlled; the ledger keeps just a safe
    # revision label and contract marker needed for reproducibility.
    prompt_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contract_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_snapshot_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_cost_reporting_micros: Mapped[int | None] = mapped_column(BigInteger)
    reporting_currency: Mapped[str] = mapped_column(String(3), default="CNY")
    # ``known``, ``partial`` and ``unavailable`` let reporting distinguish a
    # genuine zero-cost cache hit from a provider response with unknown usage.
    cost_status: Mapped[str] = mapped_column(String(32), default="unavailable")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    actor_user: Mapped[UserAccount | None] = relationship(
        back_populates="ai_runs",
        foreign_keys=[actor_user_id],
    )
    route_policy_version: Mapped[AiRoutePolicyVersion | None] = relationship(
        back_populates="ai_runs",
    )
    api_invocations: Mapped[list["ApiInvocation"]] = relationship(
        back_populates="ai_run",
    )


class ApiInvocation(OrganizationScoped, Base):
    """One immutable external provider attempt belonging to an ``AiRun``.

    This is a usage and cost ledger rather than a request/response archive.
    It deliberately stores only safe operational metadata and normalized usage
    buckets; no prompts, source documents, outputs, raw provider errors, or
    secrets belong here.
    """

    __tablename__ = "api_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["ai_run_id", "organization_id"],
            ["ai_runs.id", "ai_runs.organization_id"],
            name="fk_api_invocations_run_organization",
        ),
        # A fallback attempt must be part of the same workspace as the
        # invocation it follows; a simple self-FK would permit a cross-tenant
        # chain when writes bypass ORM scope.
        ForeignKeyConstraint(
            ["fallback_of_id", "organization_id"],
            ["api_invocations.id", "api_invocations.organization_id"],
            name="fk_api_invocations_fallback_organization",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_api_invocation_id_organization",
        ),
        UniqueConstraint("ai_run_id", "attempt_no", name="uq_api_invocation_run_attempt"),
        Index("ix_api_invocations_organization_created", "organization_id", "created_at"),
        Index(
            "ix_api_invocations_organization_status_started",
            "organization_id",
            "status",
            "started_at",
        ),
        Index(
            "ix_api_invocations_provider_request",
            "provider_profile_id",
            "provider_request_id",
        ),
        Index(
            "ix_api_invocations_organization_cost_created",
            "organization_id",
            "reporting_currency",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ai_run_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    target_index: Mapped[int] = mapped_column(Integer, default=0)
    fallback_of_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    provider_profile_id: Mapped[str] = mapped_column(
        ForeignKey("ai_provider_profiles.id"),
        index=True,
    )
    model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("ai_model_profiles.id"),
        index=True,
    )
    # These snapshots retain historical meaning if a profile is later edited
    # or retired; they do not include provider request content.
    provider_driver: Mapped[str] = mapped_column(String(64))
    provider_model_id: Mapped[str] = mapped_column(String(255))
    provider_request_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="started", index=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    may_have_billed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_read_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_write_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(32), default="unavailable")
    usage_details_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    price_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_model_price_versions.id"),
        nullable=True,
        index=True,
    )
    price_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    provider_reported_cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    calculated_cost_provider_micros: Mapped[int | None] = mapped_column(BigInteger)
    provider_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    reporting_cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    reporting_currency: Mapped[str] = mapped_column(String(3), default="CNY")
    fx_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    cost_source: Mapped[str] = mapped_column(String(32), default="unavailable")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ai_run: Mapped[AiRun] = relationship(back_populates="api_invocations")
    provider_profile: Mapped[AiProviderProfile] = relationship(
        back_populates="api_invocations",
    )
    model_profile: Mapped[AiModelProfile] = relationship(back_populates="api_invocations")
    price_version: Mapped[AiModelPriceVersion | None] = relationship(
        back_populates="api_invocations",
    )


# Register the session-level tenant criteria only after every mapped business
# root above exists.  The helper imports this module lazily to avoid a model
# import cycle.
from app.tenant_scope import install_tenant_scope

install_tenant_scope()
