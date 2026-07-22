from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, select


def test_humanize_legacy_ai_labels_migration_updates_only_legacy_defaults(
    tmp_path,
) -> None:
    database_path = tmp_path / "humanize-legacy-ai-labels.sqlite"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.cmd_opts = SimpleNamespace(x=[f"database_url={database_url}"])

    command.upgrade(config, "20260722_0031")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        providers = Table("ai_provider_profiles", metadata, autoload_with=engine)
        models = Table("ai_model_profiles", metadata, autoload_with=engine)
        routes = Table("ai_route_policies", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)

        with engine.begin() as connection:
            connection.execute(
                providers.insert(),
                {
                    "id": "legacy-deepseek-provider",
                    "slug": "legacy-runtime-openai-compatible",
                    "display_name": "Legacy runtime provider",
                    "driver": "openai_compatible",
                    "base_url": "https://api.deepseek.example.test/chat/completions",
                    "credential_ref": "legacy-runtime-credential",
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
                    "id": "legacy-deepseek-model",
                    "provider_profile_id": "legacy-deepseek-provider",
                    "slug": "legacy-runtime-default",
                    "display_name": "Legacy runtime default model",
                    "provider_model_id": "deepseek-v4-flash",
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
            connection.execute(
                routes.insert(),
                [
                    {
                        "id": "legacy-route-extraction",
                        "feature": "resume_extract_rich",
                        "display_name": "resume_extract_rich",
                        "description": None,
                        "active_version_id": None,
                        "enabled": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": "legacy-route-score",
                        "feature": "resume_score",
                        "display_name": "resume_score",
                        "description": None,
                        "active_version_id": None,
                        "enabled": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": "legacy-route-agent",
                        "feature": "recruiting_agent_turn",
                        "display_name": "recruiting_agent_turn",
                        "description": None,
                        "active_version_id": None,
                        "enabled": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": "custom-route-match",
                        "feature": "jd_match",
                        "display_name": "自定义岗位匹配",
                        "description": "保留的操作员说明",
                        "active_version_id": None,
                        "enabled": True,
                        "created_at": now,
                        "updated_at": now,
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        providers = Table("ai_provider_profiles", metadata, autoload_with=engine)
        models = Table("ai_model_profiles", metadata, autoload_with=engine)
        routes = Table("ai_route_policies", metadata, autoload_with=engine)
        with engine.connect() as connection:
            provider_name = connection.execute(
                select(providers.c.display_name).where(
                    providers.c.slug == "legacy-runtime-openai-compatible"
                )
            ).scalar_one()
            model_name = connection.execute(
                select(models.c.display_name).where(
                    models.c.slug == "legacy-runtime-default"
                )
            ).scalar_one()
            route_rows = {
                row.feature: (row.display_name, row.description)
                for row in connection.execute(
                    select(routes.c.feature, routes.c.display_name, routes.c.description)
                )
            }
    finally:
        engine.dispose()

    assert provider_name == "DeepSeek"
    assert model_name == "DeepSeek 默认模型"
    assert route_rows["resume_extract_rich"] == (
        "简历深度提取",
        "提取完整的候选人结构化信息。",
    )
    assert route_rows["resume_score"] == (
        "简历评分",
        "根据岗位要求生成候选人评分。",
    )
    assert route_rows["recruiting_agent_turn"] == (
        "招聘助手对话",
        "为招聘助手生成下一轮回复。",
    )
    assert route_rows["jd_match"] == ("自定义岗位匹配", "保留的操作员说明")
