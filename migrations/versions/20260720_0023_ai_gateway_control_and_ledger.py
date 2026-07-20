"""Create the AI gateway control plane and tenant-scoped cost ledger.

Revision ID: 20260720_0023
Revises: 20260720_0022
Create Date: 2026-07-20 19:10:00

The platform tables intentionally store only provider/model configuration
references and pricing metadata.  The tenant ledger stores normalized usage
and cost snapshots, never prompts, responses, source documents, headers, or
secret values.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0023"
down_revision: Union[str, Sequence[str], None] = "20260720_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Platform control plane: provider protocol configurations and selectable
    # models. ``credential_ref`` is a server-side reference, never a key.
    op.create_table(
        "ai_provider_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("driver", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("request_defaults_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_provider_profiles_slug",
        "ai_provider_profiles",
        ["slug"],
        unique=True,
    )
    op.create_index("ix_ai_provider_profiles_driver", "ai_provider_profiles", ["driver"])
    op.create_index("ix_ai_provider_profiles_enabled", "ai_provider_profiles", ["enabled"])

    op.create_table(
        "ai_model_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_profile_id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("provider_model_id", sa.String(length=255), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("data_classification_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_profile_id"], ["ai_provider_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_profile_id",
            "provider_model_id",
            name="uq_ai_model_profile_provider_model",
        ),
    )
    op.create_index(
        "ix_ai_model_profiles_provider_profile_id",
        "ai_model_profiles",
        ["provider_profile_id"],
    )
    op.create_index(
        "ix_ai_model_profiles_slug",
        "ai_model_profiles",
        ["slug"],
        unique=True,
    )
    op.create_index("ix_ai_model_profiles_enabled", "ai_model_profiles", ["enabled"])
    op.create_index(
        "ix_ai_model_profiles_provider_enabled",
        "ai_model_profiles",
        ["provider_profile_id", "enabled"],
    )

    op.create_table(
        "ai_model_price_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("model_profile_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_price_per_million", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("output_price_per_million", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column(
            "cached_read_input_price_per_million",
            sa.Numeric(precision=20, scale=8),
            nullable=True,
        ),
        sa.Column(
            "cached_write_input_price_per_million",
            sa.Numeric(precision=20, scale=8),
            nullable=True,
        ),
        sa.Column("reasoning_price_per_million", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("request_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("page_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("source", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(["model_profile_id"], ["ai_model_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_profile_id", "version", name="uq_ai_model_price_version"),
    )
    op.create_index(
        "ix_ai_model_price_versions_model_profile_id",
        "ai_model_price_versions",
        ["model_profile_id"],
    )
    op.create_index(
        "ix_ai_model_price_versions_effective_from",
        "ai_model_price_versions",
        ["effective_from"],
    )
    op.create_index(
        "ix_ai_model_price_versions_is_active",
        "ai_model_price_versions",
        ["is_active"],
    )
    op.create_index(
        "ix_ai_model_price_versions_created_by_user_id",
        "ai_model_price_versions",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_ai_model_price_versions_model_active_effective",
        "ai_model_price_versions",
        ["model_profile_id", "is_active", "effective_from"],
    )

    # The active-version foreign key is added after the version table exists;
    # this keeps the circular policy/version relationship portable to SQLite.
    op.create_table(
        "ai_route_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active_version_id", sa.String(length=36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_route_policies_feature",
        "ai_route_policies",
        ["feature"],
        unique=True,
    )
    op.create_index(
        "ix_ai_route_policies_active_version_id",
        "ai_route_policies",
        ["active_version_id"],
    )
    op.create_index("ix_ai_route_policies_enabled", "ai_route_policies", ["enabled"])

    op.create_table(
        "ai_route_policy_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("targets_json", sa.JSON(), nullable=False),
        sa.Column("retry_policy_json", sa.JSON(), nullable=False),
        sa.Column("max_cost_guard_json", sa.JSON(), nullable=False),
        sa.Column("prompt_revision", sa.String(length=120), nullable=True),
        sa.Column("published_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["policy_id"], ["ai_route_policies.id"]),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["ai_route_policy_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "version", name="uq_ai_route_policy_version"),
    )
    op.create_index(
        "ix_ai_route_policy_versions_policy_id",
        "ai_route_policy_versions",
        ["policy_id"],
    )
    op.create_index(
        "ix_ai_route_policy_versions_status",
        "ai_route_policy_versions",
        ["status"],
    )
    op.create_index(
        "ix_ai_route_policy_versions_published_by_user_id",
        "ai_route_policy_versions",
        ["published_by_user_id"],
    )
    op.create_index(
        "ix_ai_route_policy_versions_supersedes_version_id",
        "ai_route_policy_versions",
        ["supersedes_version_id"],
    )
    op.create_index(
        "ix_ai_route_policy_versions_policy_status",
        "ai_route_policy_versions",
        ["policy_id", "status"],
    )
    op.create_index(
        "ix_ai_route_policy_versions_published",
        "ai_route_policy_versions",
        ["status", "published_at"],
    )
    with op.batch_alter_table("ai_route_policies") as batch_op:
        batch_op.create_foreign_key(
            "fk_ai_route_policies_active_version",
            "ai_route_policy_versions",
            ["active_version_id"],
            ["id"],
        )

    # Queued work pins the policy version chosen at enqueue time.  The
    # nullable rollout keeps pending work created before this release safely
    # compatible; new gateway-aware enqueue paths will always fill it.
    with op.batch_alter_table("resume_ai_extraction_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("ai_route_policy_version_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_resume_ai_extraction_jobs_route_policy_version",
            "ai_route_policy_versions",
            ["ai_route_policy_version_id"],
            ["id"],
        )
    op.create_index(
        "ix_resume_ai_extraction_jobs_ai_route_policy_version_id",
        "resume_ai_extraction_jobs",
        ["ai_route_policy_version_id"],
    )

    with op.batch_alter_table("resume_score_batches") as batch_op:
        batch_op.add_column(
            sa.Column("ai_route_policy_version_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_resume_score_batches_route_policy_version",
            "ai_route_policy_versions",
            ["ai_route_policy_version_id"],
            ["id"],
        )
    op.create_index(
        "ix_resume_score_batches_ai_route_policy_version_id",
        "resume_score_batches",
        ["ai_route_policy_version_id"],
    )

    with op.batch_alter_table("job_match_batches") as batch_op:
        batch_op.add_column(
            sa.Column("ai_route_policy_version_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_job_match_batches_route_policy_version",
            "ai_route_policy_versions",
            ["ai_route_policy_version_id"],
            ["id"],
        )
    op.create_index(
        "ix_job_match_batches_ai_route_policy_version_id",
        "job_match_batches",
        ["ai_route_policy_version_id"],
    )

    # Tenant runtime ledger. Both tables carry organization_id directly.  The
    # composite invocation foreign key prevents a child record from pointing
    # at an AI run owned by a different workspace.
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("service_kind", sa.String(length=32), nullable=False),
        sa.Column("business_ref_type", sa.String(length=64), nullable=False),
        sa.Column("business_ref_id", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("route_policy_version_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_revision", sa.String(length=128), nullable=True),
        sa.Column("contract_version", sa.String(length=128), nullable=True),
        sa.Column("source_snapshot_hmac", sa.String(length=64), nullable=True),
        sa.Column("input_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_cost_reporting_micros", sa.BigInteger(), nullable=True),
        sa.Column("reporting_currency", sa.String(length=3), nullable=False),
        sa.Column("cost_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user_accounts.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["route_policy_version_id"],
            ["ai_route_policy_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_ai_run_id_organization"),
    )
    op.create_index("ix_ai_runs_organization_id", "ai_runs", ["organization_id"])
    op.create_index("ix_ai_runs_actor_user_id", "ai_runs", ["actor_user_id"])
    op.create_index("ix_ai_runs_feature", "ai_runs", ["feature"])
    op.create_index("ix_ai_runs_service_kind", "ai_runs", ["service_kind"])
    op.create_index("ix_ai_runs_correlation_id", "ai_runs", ["correlation_id"])
    op.create_index(
        "ix_ai_runs_route_policy_version_id",
        "ai_runs",
        ["route_policy_version_id"],
    )
    op.create_index("ix_ai_runs_status", "ai_runs", ["status"])
    op.create_index("ix_ai_runs_cache_hit", "ai_runs", ["cache_hit"])
    op.create_index(
        "ix_ai_runs_organization_created",
        "ai_runs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_ai_runs_organization_feature_started",
        "ai_runs",
        ["organization_id", "feature", "started_at"],
    )
    op.create_index(
        "ix_ai_runs_organization_status_started",
        "ai_runs",
        ["organization_id", "status", "started_at"],
    )
    op.create_index(
        "ix_ai_runs_organization_business_ref",
        "ai_runs",
        ["organization_id", "business_ref_type", "business_ref_id"],
    )
    op.create_index("ix_ai_runs_correlation", "ai_runs", ["correlation_id"])

    op.create_table(
        "api_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("ai_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("target_index", sa.Integer(), nullable=False),
        sa.Column("fallback_of_id", sa.String(length=36), nullable=True),
        sa.Column("provider_profile_id", sa.String(length=36), nullable=False),
        sa.Column("model_profile_id", sa.String(length=36), nullable=False),
        sa.Column("provider_driver", sa.String(length=64), nullable=False),
        sa.Column("provider_model_id", sa.String(length=255), nullable=False),
        sa.Column("provider_request_id", sa.String(length=512), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("may_have_billed", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_read_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_write_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("image_units", sa.BigInteger(), nullable=True),
        sa.Column("page_units", sa.BigInteger(), nullable=True),
        sa.Column("request_units", sa.BigInteger(), nullable=True),
        sa.Column("usage_source", sa.String(length=32), nullable=False),
        sa.Column("usage_details_json", sa.JSON(), nullable=False),
        sa.Column("price_version_id", sa.String(length=36), nullable=True),
        sa.Column("price_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("provider_reported_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("calculated_cost_provider_micros", sa.BigInteger(), nullable=True),
        sa.Column("provider_currency", sa.String(length=3), nullable=True),
        sa.Column("reporting_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("reporting_currency", sa.String(length=3), nullable=False),
        sa.Column("fx_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("cost_source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_profile_id"], ["ai_model_profiles.id"]),
        sa.ForeignKeyConstraint(
            ["price_version_id"],
            ["ai_model_price_versions.id"],
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["provider_profile_id"],
            ["ai_provider_profiles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["ai_run_id", "organization_id"],
            ["ai_runs.id", "ai_runs.organization_id"],
            name="fk_api_invocations_run_organization",
        ),
        sa.ForeignKeyConstraint(
            ["fallback_of_id", "organization_id"],
            ["api_invocations.id", "api_invocations.organization_id"],
            name="fk_api_invocations_fallback_organization",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_api_invocation_id_organization",
        ),
        sa.UniqueConstraint("ai_run_id", "attempt_no", name="uq_api_invocation_run_attempt"),
    )
    op.create_index(
        "ix_api_invocations_organization_id",
        "api_invocations",
        ["organization_id"],
    )
    op.create_index("ix_api_invocations_ai_run_id", "api_invocations", ["ai_run_id"])
    op.create_index(
        "ix_api_invocations_fallback_of_id",
        "api_invocations",
        ["fallback_of_id"],
    )
    op.create_index(
        "ix_api_invocations_provider_profile_id",
        "api_invocations",
        ["provider_profile_id"],
    )
    op.create_index(
        "ix_api_invocations_model_profile_id",
        "api_invocations",
        ["model_profile_id"],
    )
    op.create_index("ix_api_invocations_status", "api_invocations", ["status"])
    op.create_index(
        "ix_api_invocations_may_have_billed",
        "api_invocations",
        ["may_have_billed"],
    )
    op.create_index(
        "ix_api_invocations_price_version_id",
        "api_invocations",
        ["price_version_id"],
    )
    op.create_index(
        "ix_api_invocations_organization_created",
        "api_invocations",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_api_invocations_organization_status_started",
        "api_invocations",
        ["organization_id", "status", "started_at"],
    )
    op.create_index(
        "ix_api_invocations_provider_request",
        "api_invocations",
        ["provider_profile_id", "provider_request_id"],
    )
    op.create_index(
        "ix_api_invocations_organization_cost_created",
        "api_invocations",
        ["organization_id", "reporting_currency", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("api_invocations")
    op.drop_table("ai_runs")

    op.drop_index(
        "ix_job_match_batches_ai_route_policy_version_id",
        table_name="job_match_batches",
    )
    with op.batch_alter_table("job_match_batches") as batch_op:
        batch_op.drop_constraint(
            "fk_job_match_batches_route_policy_version",
            type_="foreignkey",
        )
        batch_op.drop_column("ai_route_policy_version_id")

    op.drop_index(
        "ix_resume_score_batches_ai_route_policy_version_id",
        table_name="resume_score_batches",
    )
    with op.batch_alter_table("resume_score_batches") as batch_op:
        batch_op.drop_constraint(
            "fk_resume_score_batches_route_policy_version",
            type_="foreignkey",
        )
        batch_op.drop_column("ai_route_policy_version_id")

    op.drop_index(
        "ix_resume_ai_extraction_jobs_ai_route_policy_version_id",
        table_name="resume_ai_extraction_jobs",
    )
    with op.batch_alter_table("resume_ai_extraction_jobs") as batch_op:
        batch_op.drop_constraint(
            "fk_resume_ai_extraction_jobs_route_policy_version",
            type_="foreignkey",
        )
        batch_op.drop_column("ai_route_policy_version_id")

    with op.batch_alter_table("ai_route_policies") as batch_op:
        batch_op.drop_constraint(
            "fk_ai_route_policies_active_version",
            type_="foreignkey",
        )
    op.drop_table("ai_route_policy_versions")
    op.drop_table("ai_route_policies")

    op.drop_table("ai_model_price_versions")
    op.drop_table("ai_model_profiles")
    op.drop_table("ai_provider_profiles")
