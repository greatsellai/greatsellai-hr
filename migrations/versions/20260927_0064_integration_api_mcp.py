"""Add workspace-bound API/MCP credentials, drafts, limits and OAuth state.

Revision ID: 20260927_0064
Revises: 20260806_0063
Create Date: 2026-09-27

This revision is based on the source migration chain, not a deployed database.
All integration tables start empty, and workspace opt-in defaults to false.
The binding indexes include each existing table's primary key, so they do not
introduce a new uniqueness requirement for existing rows. They must precede
the composite foreign keys that reference them on PostgreSQL and SQLite.

Rollback is application-only: automatic schema downgrade is intentionally
blocked because dropping these tables could erase authorization, audit and
retained private-draft state. No runtime model imports or data backfill occur.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260927_0064"
down_revision: Union[str, Sequence[str], None] = "20260806_0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BINDING_INDEXES = (
    ("uq_integration_membership_binding", "organization_memberships", ["id", "organization_id", "user_id"]),
    ("uq_integration_candidate_org", "candidates", ["id", "organization_id"]),
    ("uq_integration_resume_binding", "resumes", ["id", "organization_id", "candidate_id"]),
    ("uq_integration_snapshot_binding", "resume_fact_snapshots", ["id", "organization_id", "resume_id", "facts_version"]),
    ("uq_integration_job_version_binding", "job_versions", ["id", "organization_id", "job_id"]),
)


def _id() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True, nullable=False)


def _organization_id() -> sa.Column:
    return sa.Column(
        "organization_id", sa.String(36), sa.ForeignKey("organizations.id"), nullable=False,
    )


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _indexes(table: str, *, organization: bool = True, expires: bool = False) -> None:
    if organization:
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    if expires:
        op.create_index(f"ix_{table}_expires_at", table, ["expires_at"])


def upgrade() -> None:
    for name, table, columns in _BINDING_INDEXES:
        op.create_index(name, table, columns, unique=True)

    op.create_table(
        "integration_workspace_policies",
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id"), primary_key=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("allowed_scopes", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _timestamp("updated_at"),
    )

    op.create_table(
        "integration_grants",
        _id(),
        _organization_id(),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("membership_id", sa.String(36), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'pat'")),
        sa.Column("scopes", sa.JSON(), nullable=False),
        _timestamp("revoked_at", nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("id", "organization_id", name="uq_integration_grant_org"),
        sa.ForeignKeyConstraint(
            ["membership_id", "organization_id", "user_id"],
            ["organization_memberships.id", "organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_integration_grant_membership",
        ),
        sa.CheckConstraint("audience IN ('rest', 'mcp')", name="ck_integration_grant_audience"),
        sa.CheckConstraint("kind IN ('pat', 'oauth')", name="ck_integration_grant_kind"),
    )
    _indexes("integration_grants")
    op.create_index("uq_integration_grant_owner_binding", "integration_grants", ["id", "organization_id", "user_id", "membership_id"], unique=True)
    op.create_index("uq_integration_grant_audience_binding", "integration_grants", ["id", "organization_id", "user_id", "membership_id", "audience"], unique=True)
    op.create_index("ix_integration_grant_owner", "integration_grants", ["organization_id", "user_id", "created_at"])

    op.create_table(
        "integration_credentials",
        _id(),
        _organization_id(),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'pat'")),
        sa.Column("auth_session_version", sa.Integer(), nullable=False),
        _timestamp("expires_at"),
        _timestamp("revoked_at", nullable=True),
        _timestamp("created_at"),
        _timestamp("last_used_at", nullable=True),
        sa.ForeignKeyConstraint(["grant_id", "organization_id"], ["integration_grants.id", "integration_grants.organization_id"], name="fk_integration_credential_grant"),
        sa.UniqueConstraint("id", "organization_id", name="uq_integration_credential_org"),
        sa.UniqueConstraint("token_digest", name="uq_integration_credential_digest"),
        sa.CheckConstraint("kind IN ('pat', 'oauth')", name="ck_integration_credential_kind"),
    )
    _indexes("integration_credentials", expires=True)
    op.create_index("ix_integration_credential_grant", "integration_credentials", ["organization_id", "grant_id", "created_at"])

    op.create_table(
        "integration_audit_events",
        _id(),
        _organization_id(),
        sa.Column("actor_user_id", sa.String(36), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("grant_id", sa.String(36), nullable=True),
        sa.Column("credential_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=True),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=True),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        _timestamp("created_at"),
        _timestamp("expires_at"),
    )
    _indexes("integration_audit_events", expires=True)
    op.create_index("ix_integration_audit_owner", "integration_audit_events", ["organization_id", "actor_user_id", "created_at", "id"])

    op.create_table(
        "integration_rate_limit_buckets",
        _id(),
        _organization_id(),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        _timestamp("window_started_at"),
        sa.Column("request_count", sa.Integer(), nullable=False),
        _timestamp("expires_at"),
        sa.UniqueConstraint("organization_id", "scope_kind", "scope_id", "window_started_at", name="uq_integration_rate_bucket"),
        sa.CheckConstraint("request_count >= 0", name="ck_integration_rate_nonnegative"),
    )
    _indexes("integration_rate_limit_buckets", expires=True)

    op.create_table(
        "integration_request_leases",
        _id(),
        _organization_id(),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        _timestamp("created_at"),
        _timestamp("expires_at"),
        _timestamp("released_at", nullable=True),
        sa.ForeignKeyConstraint(["grant_id", "organization_id"], ["integration_grants.id", "integration_grants.organization_id"], name="fk_integration_lease_grant"),
    )
    _indexes("integration_request_leases", expires=True)
    op.create_index("ix_integration_lease_active", "integration_request_leases", ["organization_id", "grant_id", "released_at", "expires_at"])

    op.create_table(
        "integration_daily_candidate_accesses",
        _id(),
        _organization_id(),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        _timestamp("expires_at"),
        sa.UniqueConstraint("organization_id", "scope_kind", "scope_id", "day", "candidate_id", name="uq_integration_daily_candidate"),
        sa.Index("ix_integration_daily_scope", "organization_id", "scope_kind", "scope_id", "day"),
    )
    _indexes("integration_daily_candidate_accesses", expires=True)

    op.create_table(
        "integration_analysis_reports",
        _id(),
        _organization_id(),
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("membership_id", sa.String(36), nullable=False),
        sa.Column("source_grant_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("job_version_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("expires_at"),
        _timestamp("invalidated_at", nullable=True),
        sa.Column("invalidation_reason", sa.String(64), nullable=True),
        sa.UniqueConstraint("id", "organization_id", name="uq_integration_report_org"),
        sa.UniqueConstraint("id", "organization_id", "owner_user_id", "membership_id", name="uq_integration_report_owner"),
        sa.ForeignKeyConstraint(
            ["membership_id", "organization_id", "owner_user_id"],
            ["organization_memberships.id", "organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_integration_report_membership", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_grant_id", "organization_id", "owner_user_id", "membership_id"],
            ["integration_grants.id", "integration_grants.organization_id", "integration_grants.user_id", "integration_grants.membership_id"],
            name="fk_integration_report_source_grant", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_version_id", "organization_id", "job_id"],
            ["job_versions.id", "job_versions.organization_id", "job_versions.job_id"],
            name="fk_integration_report_job_version", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("version >= 1", name="ck_integration_report_version"),
        sa.CheckConstraint("(job_id IS NULL AND job_version_id IS NULL) OR (job_id IS NOT NULL AND job_version_id IS NOT NULL)", name="ck_integration_report_job_pair"),
    )
    _indexes("integration_analysis_reports", expires=True)
    op.create_index("ix_integration_analysis_reports_invalidated_at", "integration_analysis_reports", ["invalidated_at"])
    op.create_index("ix_integration_report_owner_updated", "integration_analysis_reports", ["organization_id", "owner_user_id", "membership_id", "updated_at", "id"])

    op.create_table(
        "integration_analysis_references",
        _id(),
        _organization_id(),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=False),
        sa.Column("resume_id", sa.String(36), nullable=False),
        sa.Column("fact_snapshot_id", sa.String(36), nullable=False),
        sa.Column("facts_version", sa.Integer(), nullable=False),
        sa.Column("candidate_lifecycle_version", sa.Integer(), nullable=False),
        sa.Column("resume_lifecycle_version", sa.Integer(), nullable=False),
        sa.Column("fact_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_block_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        _timestamp("created_at"),
        sa.UniqueConstraint("report_id", "candidate_id", name="uq_integration_reference_candidate"),
        sa.ForeignKeyConstraint(["report_id", "organization_id"], ["integration_analysis_reports.id", "integration_analysis_reports.organization_id"], name="fk_integration_reference_report", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id", "organization_id"], ["candidates.id", "candidates.organization_id"], name="fk_integration_reference_candidate", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resume_id", "organization_id", "candidate_id"], ["resumes.id", "resumes.organization_id", "resumes.candidate_id"], name="fk_integration_reference_resume", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["fact_snapshot_id", "organization_id", "resume_id", "facts_version"],
            ["resume_fact_snapshots.id", "resume_fact_snapshots.organization_id", "resume_fact_snapshots.resume_id", "resume_fact_snapshots.facts_version"],
            name="fk_integration_reference_snapshot", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("facts_version >= 0", name="ck_integration_reference_facts_version"),
        sa.CheckConstraint("candidate_lifecycle_version >= 1 AND resume_lifecycle_version >= 1", name="ck_integration_reference_lifecycle"),
    )
    _indexes("integration_analysis_references")
    op.create_index("ix_integration_reference_candidate", "integration_analysis_references", ["organization_id", "candidate_id"])
    op.create_index("ix_integration_reference_resume", "integration_analysis_references", ["organization_id", "resume_id"])

    op.create_table(
        "integration_idempotency_records",
        _id(),
        _organization_id(),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("membership_id", sa.String(36), nullable=False),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("expires_at"),
        sa.UniqueConstraint("organization_id", "user_id", "membership_id", "grant_id", "audience", "operation", "key_digest", name="uq_integration_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["grant_id", "organization_id", "user_id", "membership_id", "audience"],
            ["integration_grants.id", "integration_grants.organization_id", "integration_grants.user_id", "integration_grants.membership_id", "integration_grants.audience"],
            name="fk_integration_idempotency_grant", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_id", "organization_id", "user_id", "membership_id"],
            ["integration_analysis_reports.id", "integration_analysis_reports.organization_id", "integration_analysis_reports.owner_user_id", "integration_analysis_reports.membership_id"],
            name="fk_integration_idempotency_report", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("audience IN ('rest', 'mcp')", name="ck_integration_idempotency_audience"),
        sa.CheckConstraint("report_version >= 1", name="ck_integration_idempotency_version"),
    )
    _indexes("integration_idempotency_records", expires=True)

    op.create_table(
        "integration_oauth_clients",
        _id(),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("disabled_at", nullable=True),
    )

    op.create_table(
        "integration_oauth_consents",
        _id(),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(36), sa.ForeignKey("integration_oauth_clients.id"), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("resource", sa.String(2048), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(1024), nullable=True),
        sa.Column("code_challenge", sa.String(43), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("membership_id", sa.String(36), nullable=True),
        sa.Column("auth_session_version", sa.Integer(), nullable=True),
        sa.Column("session_digest", sa.String(64), nullable=True),
        _timestamp("created_at"),
        _timestamp("expires_at"),
        _timestamp("consumed_at", nullable=True),
        sa.UniqueConstraint("request_digest"),
        sa.ForeignKeyConstraint(
            ["membership_id", "organization_id", "user_id"],
            ["organization_memberships.id", "organization_memberships.organization_id", "organization_memberships.user_id"],
            name="fk_integration_oauth_consent_member",
        ),
        sa.CheckConstraint("audience IN ('rest', 'mcp')", name="ck_integration_oauth_consent_audience"),
    )
    _indexes("integration_oauth_consents", organization=False, expires=True)

    op.create_table(
        "integration_oauth_families",
        _id(),
        _organization_id(),
        sa.Column("grant_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), sa.ForeignKey("integration_oauth_clients.id"), nullable=False),
        sa.Column("auth_session_version", sa.Integer(), nullable=False),
        sa.Column("resource", sa.String(2048), nullable=False),
        _timestamp("expires_at"),
        _timestamp("revoked_at", nullable=True),
        _timestamp("created_at"),
        sa.UniqueConstraint("id", "organization_id", name="uq_integration_oauth_family_org"),
        sa.UniqueConstraint("grant_id", name="uq_integration_oauth_family_grant"),
        sa.ForeignKeyConstraint(["grant_id", "organization_id"], ["integration_grants.id", "integration_grants.organization_id"], name="fk_integration_oauth_family_grant"),
    )
    _indexes("integration_oauth_families", expires=True)

    op.create_table(
        "integration_oauth_codes",
        _id(),
        _organization_id(),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("code_challenge", sa.String(43), nullable=False),
        sa.Column("code_challenge_method", sa.String(8), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        _timestamp("expires_at"),
        _timestamp("consumed_at", nullable=True),
        _timestamp("created_at"),
        sa.UniqueConstraint("code_digest"),
        sa.ForeignKeyConstraint(["family_id", "organization_id"], ["integration_oauth_families.id", "integration_oauth_families.organization_id"], name="fk_integration_oauth_code_family"),
    )
    _indexes("integration_oauth_codes", expires=True)

    op.create_table(
        "integration_oauth_refresh_tokens",
        _id(),
        _organization_id(),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        _timestamp("consumed_at", nullable=True),
        _timestamp("created_at"),
        sa.UniqueConstraint("token_digest"),
        sa.ForeignKeyConstraint(["family_id", "organization_id"], ["integration_oauth_families.id", "integration_oauth_families.organization_id"], name="fk_integration_oauth_refresh_family"),
    )
    _indexes("integration_oauth_refresh_tokens")

    op.create_table(
        "integration_oauth_rate_buckets",
        sa.Column("key", sa.String(100), primary_key=True, nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        _timestamp("expires_at"),
    )
    _indexes("integration_oauth_rate_buckets", organization=False, expires=True)


def downgrade() -> None:
    raise RuntimeError(
        "integration_schema_downgrade_blocked: this additive migration retains "
        "authorization, audit and private-draft state; roll back application "
        "code without downgrading the database"
    )
