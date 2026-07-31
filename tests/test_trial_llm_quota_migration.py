from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_trial_llm_quota_migration_backfills_all_traceable_in_window_provider_attempts(
    tmp_path,
) -> None:
    database_path = tmp_path / "trial-llm-quota.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260722_0032")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        users = Table("user_accounts", metadata, autoload_with=engine)
        providers = Table("ai_provider_profiles", metadata, autoload_with=engine)
        models = Table("ai_model_profiles", metadata, autoload_with=engine)
        runs = Table("ai_runs", metadata, autoload_with=engine)
        invocations = Table("api_invocations", metadata, autoload_with=engine)
        organization_id = "00000000-0000-4000-8000-000000000001"
        now = datetime.now(timezone.utc)
        trial_start = now - timedelta(days=2)
        trial_end = now + timedelta(days=2)

        with engine.begin() as connection:
            # These provider attempts are legacy-workspace history. Seed the
            # explicit, verified platform administrator needed by static-auth
            # retirement before this fixture upgrades through that migration.
            connection.execute(
                users.insert(),
                {
                    "id": "quota-migration-platform-admin",
                    "email": "quota-migration-admin@example.test",
                    "email_key": "quota-migration-admin@example.test",
                    "full_name": "Trial quota migration admin",
                    "password_hash": "migration-fixture-not-a-login-password",
                    "is_active": True,
                    "is_platform_admin": True,
                    "email_verified_at": now,
                    "last_login_at": None,
                    "created_at": now,
                    "updated_at": now,
                    "auth_session_version": 1,
                },
            )
            connection.execute(
                organizations.update()
                .where(organizations.c.id == organization_id)
                .values(
                    plan_status="trial",
                    trial_started_at=trial_start,
                    trial_ends_at=trial_end,
                )
            )
            connection.execute(
                providers.insert(),
                {
                    "id": "quota-migration-provider",
                    "slug": "quota-migration-provider",
                    "display_name": "Quota migration provider",
                    "driver": "openai_compatible",
                    "base_url": "https://provider.example.test/v1/chat/completions",
                    "credential_ref": "test",
                    "request_defaults_json": {},
                    "enabled": True,
                    "retired_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                models.insert(),
                {
                    "id": "quota-migration-model",
                    "provider_profile_id": "quota-migration-provider",
                    "slug": "quota-migration-model",
                    "display_name": "Quota migration model",
                    "provider_model_id": "quota-migration-model",
                    "capabilities_json": {"chat": True},
                    "context_window": None,
                    "max_output_tokens": None,
                    "data_classification_json": {"candidate_data_allowed": True},
                    "enabled": True,
                    "retired_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            run_rows = [
                _run_row(
                    run_id="quota-migration-llm-one",
                    organization_id=organization_id,
                    service_kind="llm",
                    now=now,
                ),
                _run_row(
                    run_id="quota-migration-llm-two",
                    organization_id=organization_id,
                    service_kind="llm",
                    now=now,
                ),
                _run_row(
                    run_id="quota-migration-ocr",
                    organization_id=organization_id,
                    service_kind="ocr",
                    now=now,
                ),
                _run_row(
                    run_id="quota-migration-before-trial",
                    organization_id=organization_id,
                    service_kind="llm",
                    now=now,
                ),
            ]
            connection.execute(runs.insert(), run_rows)
            connection.execute(
                invocations.insert(),
                [
                    _invocation_row(
                        invocation_id="quota-migration-invocation-one",
                        run_id="quota-migration-llm-one",
                        organization_id=organization_id,
                        started_at=now,
                        now=now,
                    ),
                    _invocation_row(
                        invocation_id="quota-migration-invocation-two",
                        run_id="quota-migration-llm-two",
                        organization_id=organization_id,
                        started_at=now,
                        now=now,
                    ),
                    _invocation_row(
                        invocation_id="quota-migration-invocation-ocr",
                        run_id="quota-migration-ocr",
                        organization_id=organization_id,
                        started_at=now,
                        now=now,
                    ),
                    _invocation_row(
                        invocation_id="quota-migration-invocation-before-trial",
                        run_id="quota-migration-before-trial",
                        organization_id=organization_id,
                        started_at=trial_start - timedelta(seconds=1),
                        now=now,
                    ),
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        organizations = Table("organizations", metadata, autoload_with=engine)
        with engine.connect() as connection:
            row = connection.execute(
                select(
                    organizations.c.trial_llm_call_limit,
                    organizations.c.trial_llm_call_used,
                ).where(organizations.c.id == "00000000-0000-4000-8000-000000000001")
            ).one()
    finally:
        engine.dispose()

    assert row.trial_llm_call_limit == 1000
    assert row.trial_llm_call_used == 3


def _run_row(*, run_id: str, organization_id: str, service_kind: str, now: datetime) -> dict[str, object]:
    return {
        "id": run_id,
        "organization_id": organization_id,
        "actor_user_id": None,
        "feature": "resume_summary",
        "service_kind": service_kind,
        "business_ref_type": "migration_test",
        "business_ref_id": run_id,
        "correlation_id": None,
        "route_policy_version_id": None,
        "prompt_revision": None,
        "contract_version": None,
        "source_snapshot_hmac": None,
        "input_size_bytes": None,
        "status": "succeeded",
        "cache_hit": False,
        "failure_code": None,
        "started_at": now,
        "finished_at": now,
        "total_cost_reporting_micros": None,
        "reporting_currency": "CNY",
        "cost_status": "unavailable",
        "created_at": now,
        "updated_at": now,
    }


def _invocation_row(
    *,
    invocation_id: str,
    run_id: str,
    organization_id: str,
    started_at: datetime,
    now: datetime,
) -> dict[str, object]:
    return {
        "id": invocation_id,
        "organization_id": organization_id,
        "ai_run_id": run_id,
        "attempt_no": 1,
        "target_index": 0,
        "fallback_of_id": None,
        "provider_profile_id": "quota-migration-provider",
        "model_profile_id": "quota-migration-model",
        "provider_driver": "openai_compatible",
        "provider_model_id": "quota-migration-model",
        "provider_request_id": None,
        "http_status": 200,
        "status": "succeeded",
        "error_category": None,
        "error_code": None,
        "may_have_billed": False,
        "started_at": started_at,
        "completed_at": now,
        "latency_ms": 1,
        "input_tokens": None,
        "cached_read_input_tokens": None,
        "cached_write_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "image_units": None,
        "page_units": None,
        "request_units": 1,
        "usage_source": "provider",
        "usage_details_json": {},
        "price_version_id": None,
        "price_snapshot_json": {},
        "provider_reported_cost_micros": None,
        "calculated_cost_provider_micros": None,
        "provider_currency": None,
        "reporting_cost_micros": None,
        "reporting_currency": "CNY",
        "fx_snapshot_json": {},
        "cost_source": "unavailable",
        "created_at": now,
    }
