from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_mailbox_provider_oauth_migration_upgrades_current_production_revision_without_branching(
    tmp_path,
) -> None:
    database_path = tmp_path / "mailbox-provider-oauth.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    # ``20260724_0036`` is the last production revision before the contact
    # details and mailbox OAuth branches. The linear chain must apply both
    # additions together and land on the one canonical head.
    command.upgrade(config, "20260724_0036")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        users = Table("user_accounts", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        common_values = {
            "imap_port": 993,
            "mailbox": "INBOX",
            "encrypted_password": "opaque-existing-app-password-ciphertext",
            "enabled": True,
            "last_synced_at": now,
            "last_sync_error": None,
            "created_at": now,
            "updated_at": now,
            "import_start_uid": 42,
            "imap_uidvalidity": 9,
            "import_started_at": now,
            "organization_id": "00000000-0000-4000-8000-000000000001",
            "retention_policy": "standard",
            "last_retention_cleanup_at": None,
            "archived_at": None,
            "sync_lease_token": None,
            "sync_lease_expires_at": None,
            "last_sync_started_at": now,
        }
        rows = [
            {
                **common_values,
                "id": "mailbox-provider-migration-feishu",
                "imap_host": "IMAP.FEISHU.CN",
                "email_address": "feishu@example.test",
                "display_name": "飞书招聘邮箱",
                "display_name_key": "飞书招聘邮箱",
            },
            {
                **common_values,
                "id": "mailbox-provider-migration-exmail",
                "imap_host": "imap.exmail.qq.com",
                "email_address": "exmail@example.test",
                "display_name": "企业邮招聘邮箱",
                "display_name_key": "企业邮招聘邮箱",
            },
            {
                **common_values,
                "id": "mailbox-provider-migration-qq",
                "imap_host": "imap.qq.com",
                "email_address": "qq@example.test",
                "display_name": "QQ 招聘邮箱",
                "display_name_key": "qq 招聘邮箱",
            },
            {
                **common_values,
                "id": "mailbox-provider-migration-legacy",
                "imap_host": "imap.legacy.example.test",
                "email_address": "legacy@example.test",
                "display_name": "旧版招聘邮箱",
                "display_name_key": "旧版招聘邮箱",
            },
        ]
        with engine.begin() as connection:
            # The mailbox rows below are real legacy-workspace history. The
            # static-auth retirement migration therefore needs an explicit,
            # verified platform administrator before this fixture upgrades on.
            connection.execute(
                users.insert(),
                {
                    "id": "mailbox-provider-migration-platform-admin",
                    "email": "mailbox-provider-admin@example.test",
                    "email_key": "mailbox-provider-admin@example.test",
                    "full_name": "Mailbox provider migration admin",
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
            connection.execute(mailboxes.insert(), rows)
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        mailboxes = Table("mailbox_configs", metadata, autoload_with=engine)
        oauth_credentials = Table(
            "mailbox_oauth_credentials",
            metadata,
            autoload_with=engine,
        )
        oauth_intents = Table(
            "mailbox_oauth_connect_intents",
            metadata,
            autoload_with=engine,
        )
        workspace_lanes = Table(
            "workspace_background_lanes",
            metadata,
            autoload_with=engine,
        )
        alembic_version = Table("alembic_version", metadata, autoload_with=engine)
        inspector = inspect(engine)
        mailbox_columns = {
            column["name"]: column for column in inspector.get_columns("mailbox_configs")
        }
        resume_columns = {column["name"] for column in inspector.get_columns("resumes")}
        assert mailbox_columns["provider_key"]["nullable"] is False
        assert mailbox_columns["authentication_mode"]["nullable"] is False
        assert mailbox_columns["oauth_reauthorization_generation"]["nullable"] is False
        assert mailbox_columns["encrypted_password"]["nullable"] is True
        # Existing mailbox channels keep their pre-feature zero-day behavior.
        assert mailbox_columns["initial_sync_lookback_days"]["nullable"] is False
        assert "initial_backfill_since_date" in mailbox_columns
        assert "initial_backfill_completed_at" in mailbox_columns
        assert "contact_details" in resume_columns
        assert "reauthorization_generation" in oauth_intents.c
        assert "initial_sync_lookback_days" in oauth_intents.c
        assert {
            "mailbox_oauth_credentials",
            "mailbox_oauth_connect_intents",
            "workspace_background_lanes",
        } <= set(inspector.get_table_names())
        assert {
            "ix_mailbox_oauth_credentials_organization_mailbox",
            "ix_mailbox_oauth_credentials_organization_id",
        } <= {
            index["name"]
            for index in inspector.get_indexes("mailbox_oauth_credentials")
        }
        assert {
            "lane_key",
            "organization_id",
            "lease_token",
            "lease_expires_at",
            "current_job_kind",
            "current_job_id",
            "last_claimed_at",
        } <= set(workspace_lanes.c.keys())
        assert "uq_workspace_background_lane" in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "workspace_background_lanes"
            )
        }
        assert {
            "ix_workspace_background_lanes_organization_id",
            "ix_workspace_background_lane_claim",
            "ix_workspace_background_lane_fairness",
        } <= {
            index["name"]
            for index in inspector.get_indexes("workspace_background_lanes")
        }

        with engine.begin() as connection:
            result = connection.execute(
                select(
                    mailboxes.c.id,
                    mailboxes.c.provider_key,
                    mailboxes.c.authentication_mode,
                    mailboxes.c.encrypted_password,
                    mailboxes.c.import_start_uid,
                    mailboxes.c.imap_uidvalidity,
                    mailboxes.c.last_sync_started_at,
                    mailboxes.c.oauth_reauthorization_generation,
                    mailboxes.c.initial_sync_lookback_days,
                    mailboxes.c.initial_backfill_since_date,
                    mailboxes.c.initial_backfill_completed_at,
                ).order_by(mailboxes.c.id)
            ).mappings()
            upgraded = {row["id"]: row for row in result}

            feishu = upgraded["mailbox-provider-migration-feishu"]
            assert feishu["provider_key"] == "feishu_app_password"
            assert feishu["authentication_mode"] == "app_password"
            assert feishu["encrypted_password"] == "opaque-existing-app-password-ciphertext"
            assert feishu["import_start_uid"] == 42
            assert feishu["imap_uidvalidity"] == 9
            assert feishu["last_sync_started_at"] is not None
            assert feishu["oauth_reauthorization_generation"] == 0
            assert feishu["initial_sync_lookback_days"] == 0
            assert feishu["initial_backfill_since_date"] is None
            assert feishu["initial_backfill_completed_at"] is None
            assert upgraded["mailbox-provider-migration-exmail"]["provider_key"] == (
                "tencent_exmail_app_password"
            )
            assert upgraded["mailbox-provider-migration-qq"]["provider_key"] == (
                "qq_mail_app_password"
            )
            assert upgraded["mailbox-provider-migration-legacy"]["provider_key"] == (
                "legacy_imap"
            )

            connection.execute(
                mailboxes.insert(),
                {
                    **common_values,
                    "id": "mailbox-provider-migration-google",
                    "imap_host": "imap.gmail.com",
                    "email_address": "google@example.test",
                    "display_name": "Google 招聘邮箱",
                    "display_name_key": "google 招聘邮箱",
                    "encrypted_password": None,
                    "provider_key": "gmail_oauth",
                    "authentication_mode": "oauth2",
                },
            )
            connection.execute(
                oauth_credentials.insert(),
                {
                    "id": "mailbox-provider-migration-google-credential",
                    "organization_id": "00000000-0000-4000-8000-000000000001",
                    "mailbox_config_id": "mailbox-provider-migration-google",
                    "encrypted_refresh_token": "opaque-refresh-token-ciphertext",
                    "reauthorization_required_at": None,
                    "last_error_code": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            assert connection.scalar(select(alembic_version.c.version_num)) == (
                ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            )
    finally:
        engine.dispose()
