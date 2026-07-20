from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    JSON,
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
    """The single account's IMAP source. Its password is always encrypted."""

    __tablename__ = "mailbox_configs"
    __table_args__ = (
        Index("ix_mailbox_configs_organization_enabled", "organization_id", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
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
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    imports: Mapped[list["EmailAttachmentImport"]] = relationship(
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


# Register the session-level tenant criteria only after every mapped business
# root above exists.  The helper imports this module lazily to avoid a model
# import cycle.
from app.tenant_scope import install_tenant_scope

install_tenant_scope()
