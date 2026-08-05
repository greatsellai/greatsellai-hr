from __future__ import annotations

from typing import Final

from app.services.normalization import normalized_key


FILTER_OPTIONS_VERSION: Final = "filter-options.v5.20260803.1"

DEGREE_OPTIONS: Final = [
    {"value": "doctor", "label": "博士"},
    {"value": "master", "label": "硕士"},
    {"value": "bachelor", "label": "本科"},
    {"value": "associate", "label": "大专"},
    {"value": "high_school", "label": "高中"},
    {"value": "vocational_or_below", "label": "中专/职高及以下"},
]

INSTITUTION_TIER_OPTIONS: Final = [
    {"value": "211", "label": "211"},
    {"value": "985", "label": "985"},
    {"value": "double_first_class", "label": "双一流"},
    {"value": "key_undergraduate", "label": "重本"},
    {"value": "first_tier", "label": "一本"},
    {"value": "second_tier", "label": "二本"},
    {"value": "regular_undergraduate", "label": "普通本科"},
    {"value": "private_undergraduate", "label": "民办本科"},
    {"value": "higher_vocational", "label": "高职/高专"},
    {"value": "overseas", "label": "海外院校"},
]

# The active recruiter workflow uses these six mutually exclusive categories
# per education record. ``institution_tiers`` remains available only so old
# saved filters and fact snapshots can still be read.
INSTITUTION_CLASSIFICATION_OPTIONS: Final = [
    {"value": "985", "label": "985"},
    {"value": "211", "label": "211"},
    {"value": "undergraduate", "label": "本科"},
    {"value": "associate", "label": "大专"},
    {"value": "secondary_vocational", "label": "中专"},
    {"value": "overseas", "label": "海外院校"},
]

EXPERIENCE_TYPE_OPTIONS: Final = [
    {"value": "employment", "label": "正式工作"},
    {"value": "internship", "label": "实习"},
    {"value": "project", "label": "项目"},
    {"value": "research", "label": "科研"},
    {"value": "competition", "label": "技能竞赛"},
    {"value": "campus", "label": "校内/学生组织"},
    {"value": "club", "label": "社团"},
    {"value": "volunteer", "label": "志愿活动/社会实践"},
    {"value": "entrepreneurship", "label": "创业"},
    {"value": "training", "label": "培训"},
]

SKILL_CATEGORY_OPTIONS: Final = [
    {"value": "software", "label": "编程与开发"},
    {"value": "data_ai", "label": "数据与 AI"},
    {"value": "product_project", "label": "产品与项目"},
    {"value": "design_content", "label": "设计与内容"},
    {"value": "marketing_ecommerce_operations", "label": "市场、电商与运营"},
    {"value": "sales_customer_service", "label": "销售与客户服务"},
    {"value": "supply_chain_logistics", "label": "供应链与物流"},
    {"value": "finance_legal_hr", "label": "财务、法务与人力资源"},
    {"value": "office_collaboration", "label": "办公与协作工具"},
    {"value": "industry_professional", "label": "行业专业技能"},
]

LEADERSHIP_CONTEXT_OPTIONS: Final = [
    {"value": "class", "label": "班级"},
    {"value": "student_org", "label": "学生会/校内组织"},
    {"value": "club", "label": "社团"},
    {"value": "project_team", "label": "项目组"},
    {"value": "company", "label": "公司"},
]

AWARD_LEVEL_OPTIONS: Final = [
    {"value": "national", "label": "国家级"},
    {"value": "provincial", "label": "省级"},
    {"value": "school", "label": "校级"},
    {"value": "department", "label": "院系级"},
    {"value": "other", "label": "其他明确级别"},
]

SCHOLARSHIP_LEVEL_OPTIONS: Final = [
    {"value": "national", "label": "国家级"},
    {"value": "provincial", "label": "省级"},
    {"value": "school", "label": "校级"},
    {"value": "department", "label": "院系级"},
    {"value": "enterprise", "label": "企业/社会奖学金"},
    {"value": "other", "label": "其他明确级别"},
]

LANGUAGE_CREDENTIAL_OPTIONS: Final = [
    {"value": "cet4", "label": "大学英语四级（CET-4）"},
    {"value": "cet6", "label": "大学英语六级（CET-6）"},
    {"value": "ielts", "label": "雅思（IELTS）"},
    {"value": "toefl", "label": "托福（TOEFL）"},
    {"value": "tem4", "label": "英语专业四级（TEM-4）"},
    {"value": "tem8", "label": "英语专业八级（TEM-8）"},
    {"value": "bec", "label": "剑桥商务英语（BEC）"},
    {"value": "toeic", "label": "托业（TOEIC）"},
    {"value": "custom", "label": "其他英语证书（自定义填写）"},
]

LANGUAGE_CREDENTIAL_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "cet4": ("大学英语四级", "英语四级", "四级", "CET-4", "CET4", "CET 4"),
    "cet6": ("大学英语六级", "英语六级", "六级", "CET-6", "CET6", "CET 6"),
    "ielts": ("雅思", "IELTS"),
    "toefl": ("托福", "TOEFL", "TOEFL iBT", "托福网考"),
    "tem4": ("英语专业四级", "专业四级", "专四", "TEM-4", "TEM4", "TEM 4"),
    "tem8": ("英语专业八级", "专业八级", "专八", "TEM-8", "TEM8", "TEM 8"),
    "bec": ("剑桥商务英语", "商务英语证书", "BEC"),
    "toeic": ("托业", "TOEIC"),
}

_LANGUAGE_ALIAS_TO_CODE: Final = {
    normalized_key(alias): code
    for code, aliases in LANGUAGE_CREDENTIAL_ALIASES.items()
    for alias in aliases
}


def normalize_language_credential(value: str | None) -> str | None:
    key = normalized_key(value)
    if not key:
        return None
    if key in LANGUAGE_CREDENTIAL_ALIASES:
        return key
    return _LANGUAGE_ALIAS_TO_CODE.get(key)


def language_credential_label(code: str) -> str:
    return next(
        (item["label"] for item in LANGUAGE_CREDENTIAL_OPTIONS if item["value"] == code),
        code,
    )


def filter_options_payload(
    *,
    resume_source_tags: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": FILTER_OPTIONS_VERSION,
        "degrees": DEGREE_OPTIONS,
        "institution_classifications": INSTITUTION_CLASSIFICATION_OPTIONS,
        "institution_tiers": INSTITUTION_TIER_OPTIONS,
        "experience_types": EXPERIENCE_TYPE_OPTIONS,
        "skill_categories": SKILL_CATEGORY_OPTIONS,
        "leadership_contexts": LEADERSHIP_CONTEXT_OPTIONS,
        "award_levels": AWARD_LEVEL_OPTIONS,
        "scholarship_levels": SCHOLARSHIP_LEVEL_OPTIONS,
        "language_credentials": LANGUAGE_CREDENTIAL_OPTIONS,
        # This list is workspace-owned and intentionally dynamic. The caller
        # supplies only tags already represented by a resume source projection
        # so a recruiter's initial filter never offers an empty platform.
        "resume_source_tags": resume_source_tags or [],
        "graduation_statuses": [
            {"value": "any", "label": "不限"},
            {"value": "fresh", "label": "应届"},
            {"value": "previous", "label": "往届"},
        ],
        "presence_statuses": [
            {"value": "any", "label": "不限"},
            {"value": "present", "label": "有明确记录"},
            {"value": "unknown", "label": "未知"},
        ],
        "keyword_modes": [
            {"value": "broad", "label": "任一命中"},
            {"value": "precise", "label": "全部命中"},
        ],
    }
