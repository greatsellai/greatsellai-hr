"""Replace legacy gateway labels with operator-facing DeepSeek names.

Revision ID: 20260722_0032
Revises: 20260722_0031
Create Date: 2026-07-22 13:00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0032"
down_revision: Union[str, Sequence[str], None] = "20260722_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_PROVIDER_SLUG = "legacy-runtime-openai-compatible"
_LEGACY_MODEL_SLUG = "legacy-runtime-default"
_LEGACY_PROVIDER_NAME = "Legacy runtime provider"
_LEGACY_MODEL_NAME = "Legacy runtime default model"

_LEGACY_ROUTE_COPY: dict[str, tuple[str, str]] = {
    "resume_extract_rich": ("简历深度提取", "提取完整的候选人结构化信息。"),
    "resume_extract_core": ("简历核心信息提取", "提取筛选所需的核心字段。"),
    "candidate_name_backfill": ("候选人姓名补全", "基于简历原文补全可核验的姓名。"),
    "resume_score": ("简历评分", "根据岗位要求生成候选人评分。"),
    "resume_summary": ("简历总结", "生成候选人经历与亮点摘要。"),
    "jd_generate": ("JD 生成", "根据岗位需求生成职位描述。"),
    "jd_requirements_extract": ("JD 要求提取", "将职位描述整理为评估要求。"),
    "jd_match": ("JD 匹配", "分析候选人与岗位的匹配情况。"),
    "recruiting_agent_turn": ("招聘助手对话", "为招聘助手生成下一轮回复。"),
    "resume_ocr_page": ("简历 OCR 识别", "识别扫描件或图片简历页面。"),
}


def upgrade() -> None:
    providers = sa.table(
        "ai_provider_profiles",
        sa.column("slug", sa.String()),
        sa.column("display_name", sa.String()),
    )
    models = sa.table(
        "ai_model_profiles",
        sa.column("slug", sa.String()),
        sa.column("display_name", sa.String()),
    )
    routes = sa.table(
        "ai_route_policies",
        sa.column("feature", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
    )

    # Only touch the old bootstrap labels. Operator-created names remain
    # untouched, even if they use the same provider or model internally.
    op.execute(
        providers.update()
        .where(
            providers.c.slug == _LEGACY_PROVIDER_SLUG,
            providers.c.display_name == _LEGACY_PROVIDER_NAME,
        )
        .values(display_name="DeepSeek")
    )
    op.execute(
        models.update()
        .where(
            models.c.slug == _LEGACY_MODEL_SLUG,
            models.c.display_name == _LEGACY_MODEL_NAME,
        )
        .values(display_name="DeepSeek 默认模型")
    )
    for feature, (display_name, description) in _LEGACY_ROUTE_COPY.items():
        op.execute(
            routes.update()
            .where(
                routes.c.feature == feature,
                routes.c.display_name == feature,
            )
            .values(display_name=display_name, description=description)
        )


def downgrade() -> None:
    # These are operator-facing labels. Keeping them on a downgrade avoids
    # reintroducing opaque legacy terminology into an already-used workspace.
    pass
