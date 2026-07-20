from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    """A one-time, digest-only password reset token."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_user_requested", "user_id", "requested_at"),
        Index("ix_password_reset_tokens_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[UserAccount] = relationship(back_populates="password_reset_tokens")


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


class Candidate(OrganizationScoped, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        Index("ix_candidates_organization_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class Resume(OrganizationScoped, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        Index(
            "uq_active_resume_per_candidate",
            "candidate_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active = true"),
        ),
        Index("ix_resumes_organization_created", "organization_id", "created_at"),
        Index("ix_resumes_organization_candidate", "organization_id", "candidate_id"),
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
    source_mailbox_config: Mapped["MailboxConfig | None"] = relationship(
        back_populates="ingested_resumes",
        foreign_keys=[source_mailbox_config_id],
    )


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
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    email_address: Mapped[str] = mapped_column(String(320))
    mailbox: Mapped[str] = mapped_column(String(255), default="INBOX")
    encrypted_password: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # A binding starts at the mailbox's current UIDNEXT.  This makes the
    # inbox an append-only source from the moment the user connects it: mail
    # that was already present is never retrospectively scanned.
    import_start_uid: Mapped[int | None] = mapped_column(BigInteger)
    imap_uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    import_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class Job(OrganizationScoped, Base):
    """Current-version cache; immutable JD evidence lives in ``JobVersion``."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_organization_updated", "organization_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    jd_text: Mapped[str] = mapped_column(Text)
    requirements: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
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
