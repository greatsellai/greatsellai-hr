"""Shared read-only business service for external REST and MCP callers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, Any, TypeVar, get_args

from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer
from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import AppSettings
from app.filter_options import (
    DEGREE_OPTIONS,
    EXPERIENCE_TYPE_OPTIONS,
    FILTER_OPTIONS_VERSION,
    INSTITUTION_CLASSIFICATION_OPTIONS,
    LANGUAGE_CREDENTIAL_OPTIONS,
    SKILL_CATEGORY_OPTIONS,
)
from app.integration_read_schemas import (
    DegreeLevel,
    ExperienceType,
    InstitutionClassification,
    LanguageCredentialCode,
    SkillCategory,
    IntegrationCandidateAssessments,
    IntegrationCandidateEvidence,
    IntegrationCandidateEvidenceRequest,
    IntegrationCandidateFacts,
    IntegrationCandidateProfile,
    IntegrationCandidateSearchItem,
    IntegrationCandidateSearchRequest,
    IntegrationCandidateSearchResponse,
    IntegrationConnectionInfo,
    IntegrationEducationFact,
    IntegrationEvidenceExcerpt,
    IntegrationExperienceDetail,
    IntegrationExperienceFact,
    IntegrationFilterOptions,
    IntegrationJobClause,
    IntegrationJobList,
    IntegrationJobMatchAssessment,
    IntegrationJobRequirement,
    IntegrationJobRequirements,
    IntegrationJobSummary,
    IntegrationLanguageFact,
    IntegrationOption,
    IntegrationScholarshipFact,
    IntegrationScoreAssessment,
    IntegrationSkillFact,
    IntegrationSummaryAssessment,
    IntegrationSummarySection,
)
from app.models import (
    Candidate,
    Job,
    JobMatch,
    JobVersion,
    Resume,
    ResumeFactSnapshot,
    ResumeScore,
    ResumeSourceBlock,
    ResumeSummary,
)
from app.schemas import (
    CandidateSearchRequest,
    EducationFilter,
    LanguageCredentialFilter,
)
from app.services.deepseek_provider import redact_nonessential_personal_data
from app.services.integration_auth_service import assert_integration_context
from app.services.resume_eligibility import is_resume_screening_eligible
from app.services.search_service import (
    SearchValidationError,
    projected_search_skills,
    projected_search_source_blocks,
    search_candidates as search_internal_candidates,
)
from app.tenant_scope import organization_context_id

if TYPE_CHECKING:
    from app.services.integration_auth_service import IntegrationPrincipal


ReadResult = TypeVar("ReadResult")


_CURSOR_SALT = "greatsell-integration-read-cursor-v1"
_CURSOR_MAX_AGE_SECONDS = 15 * 60
_EVIDENCE_BLOCK_MAX_CHARS = 500
_EVIDENCE_RESPONSE_MAX_CHARS = 4_000
_CURRENT_SCORE_STATUSES = frozenset({"succeeded", "needs_review", "overridden"})
_CURRENT_MATCH_STATUSES = frozenset({"succeeded", "needs_review"})
_SUMMARY_SECTION_KEYS = (
    "candidate_positioning",
    "education_background",
    "work_and_internship",
    "core_skills",
    "representative_projects",
    "strengths",
    "verification_items",
)
_SUMMARY_SECTION_MAX_CHARS = 800
_SUMMARY_TOTAL_MAX_CHARS = 4_000
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SOCIAL_CONTACT_LABELS = (
    r"微信(?:号|账号|帐号|ID)?|QQ(?:号|号码)?|"
    r"(?<![a-z])(?:qq|we\s*chat|weixin|whats\s*app|telegram|skype|"
    r"discord|facebook|instagram|linkedin)(?:\s*(?:id|handle|username|account))?|"
    r"(?<![a-z])(?:signal|line)\s+(?:id|handle|username|account)"
)
_PRIVATE_LABELS = (
    r"(?:姓名|电话|手机(?:号码?)?|邮箱|电子邮件|地址|住址|出生年月|出生日期|生日|性别|"
    r"身份证(?:号码?)?|证件(?:号码?)?|护照(?:号码?)?|社保(?:号码?)?|银行卡号|"
    r"联系方式|" + _SOCIAL_CONTACT_LABELS + r"|"
    r"(?<![a-z])(?:signal|line)(?=\s*[:=])|"
    r"(?<![a-z])(?:name|phone|mobile|e-?mail|(?:home\s+)?address|gender|sex|"
    r"date\s+of\s+birth|birth\s*date|birthday|passport(?:\s+(?:no\.?|number))?|"
    r"(?:national|identity|social\s+security)\s+(?:id|no\.?|number)|ssn))"
)
_PRIVATE_LABEL = re.compile(_PRIVATE_LABELS + r"\s*[:=]", re.IGNORECASE)
_PRIVATE_LABEL_WHITESPACE_VALUE = re.compile(
    r"^\s*(?:" + _PRIVATE_LABELS + r")\s+\S.*$", re.IGNORECASE
)
_PRIVATE_LABEL_VALUE_ON_NEXT_LINE = re.compile(
    r"(?:^|[;；,，\s])" + _PRIVATE_LABELS + r"\s*[:=]?\s*$", re.IGNORECASE
)
_AMBIGUOUS_SOCIAL_LABEL_ONLY = re.compile(r"(?:signal|line)\s*[:=]?", re.IGNORECASE)
_SOCIAL_CONTACT_VALUE = re.compile(
    r"(?:" + _SOCIAL_CONTACT_LABELS + r")\s+(?=\S)", re.IGNORECASE
)
_PRIVATE_CONTACT_URL = re.compile(
    r"(?i)(?:weixin|tencent|tg|whatsapp|skype):/{0,2}|"
    r"(?<![a-z0-9])(?:https?://)?(?:wa\.me|t\.me|telegram\.me|"
    r"api\.whatsapp\.com|(?:www\.)?linkedin\.com/in|"
    r"(?:www\.)?facebook\.com|(?:www\.)?instagram\.com)(?:[/\s?#]|$)"
)
_IDENTITY_DOCUMENT = re.compile(
    r"(?i)身份证|证件号|护照号|(?<!\w)(?:\d{17}[\dX]|\d{15})(?!\w)|"
    r"(?<!\w)\d{3}-\d{2}-\d{4}(?!\w)"
)
_UNLABELLED_DASH_CHARS = r"[-‐‑‒–—―−]"
_UNLABELLED_DASH_RUN = _UNLABELLED_DASH_CHARS + r"+"
_UNPUNCTUATED_CJK_ADDRESS_NUMBER = (
    r"(?:\d{1,6}|"
    r"[〇零一二三四五六七八九十百千万两兩壹贰叁參肆伍陆陸柒捌玖拾佰仟万萬]{1,12})"
)
_UNPUNCTUATED_CJK_ADDRESS_UNIT = r"(?:号|號|栋|棟|幢|单元|單元|层|層|室|座|楼|樓)"
_UNPUNCTUATED_CJK_ADDRESS_PRE_NUMBER_UNIT = r"(?:栋|棟|幢|单元|單元|层|層|室|座|楼|樓)"
_UNPUNCTUATED_CJK_LETTERED_UNIT_SUFFIX = (
    r"[A-Za-z]{1,4}\s*(?:栋|棟|幢|单元|單元|层|層|室|座|楼|樓)"
)
_UNPUNCTUATED_CJK_ADDRESS_BOUNDARY = (
    r"(?=\s*(?:$|[，,、;；.!?。！？…⋯]|[0-9]|"
    + _UNPUNCTUATED_CJK_ADDRESS_UNIT + r"|"
    + _UNPUNCTUATED_CJK_LETTERED_UNIT_SUFFIX + r"))"
)
_CHINESE_PROVINCE_OR_REGION_NAMES = (
    r"(?:北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|"
    r"湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|台湾|香港|澳门)"
)
_UNLABELLED_CJK_LOCALITY_PREFIX = (
    r"(?:" + _CHINESE_PROVINCE_OR_REGION_NAMES
    + r"(?:省|市|自治区|特别行政区)?\s*)?"
    r"(?:[\u3400-\u9fff]{1,16}(?:市|地区|盟)\s*)?"
    r"(?:[\u3400-\u9fff]{1,16}(?:区|县|旗)\s*)?"
)
_UNLABELLED_ADDRESS_ROW_PREFIX = (
    r"^\s*(?:(?:[•●▪◦*|]|" + _UNLABELLED_DASH_RUN + r")\s*)?"
)
_UNLABELLED_CJK_ADDRESS_PHASE = (
    r"(?:[ \t]*(?:[A-Za-z]{1,2}|[0-9]{1,3}|"
    r"[一二三四五六七八九十]{1,3})(?:期|区|座|楼|栋|幢|单元)|"
    r"[ \t]*[东南西北中]区){0,2}"
)
_UNLABELLED_CJK_COMPOUND_ADDRESS_INLINE_VALUE = (
    _UNLABELLED_CJK_LOCALITY_PREFIX
    + r"[\u3400-\u9fff]{0,4}(?:花园|小区|公寓|社区|家园|苑|大厦|广场|科技园|工业园|"
    r"产业园|软件园|创业园|开发区|高新区)"
    + _UNLABELLED_CJK_ADDRESS_PHASE
    + r"[ \t]*"
    + r"(?:" + _UNPUNCTUATED_CJK_ADDRESS_NUMBER + r"\s*"
    + _UNPUNCTUATED_CJK_ADDRESS_UNIT + r"|"
    + _UNPUNCTUATED_CJK_ADDRESS_PRE_NUMBER_UNIT + r"\s*"
    + _UNPUNCTUATED_CJK_ADDRESS_NUMBER + r")"
)
_UNLABELLED_CJK_STREET_ADDRESS_INLINE_VALUE = (
    _UNLABELLED_CJK_LOCALITY_PREFIX
    + r"[\u3400-\u9fff]{1,32}(?:大道|大街|路|巷|弄)\s*"
    + _UNPUNCTUATED_CJK_ADDRESS_NUMBER + r"\s*"
    + _UNPUNCTUATED_CJK_ADDRESS_UNIT
)
_UNLABELLED_CJK_ADDRESS_COMPONENT = (
    r"(?>(?:[A-Za-z]{1,2}[ \t]*\d{1,6}|\d{1,6}|"
    r"[一二三四五六七八九十]{1,6}|[A-Za-z]{1,2}))"
)
_UNLABELLED_CJK_ADDRESS_COMPONENT_UNIT = (
    r"(?:栋|棟|幢|单元|單元|层|層|室|座|楼|樓|号|號)"
)
_UNLABELLED_CJK_ADDRESS_ROW_SUFFIX = (
    r"(?:[,，]?[ \t]*(?:"
    + _UNLABELLED_CJK_ADDRESS_COMPONENT
    + r"[ \t]*)?"
    + _UNLABELLED_CJK_ADDRESS_COMPONENT_UNIT
    + r"){0,3}"
)
_UNLABELLED_CJK_INLINE_ADDRESS_VALUE = re.compile(
    r"(?:"
    + _UNLABELLED_CJK_COMPOUND_ADDRESS_INLINE_VALUE
    + _UNPUNCTUATED_CJK_ADDRESS_BOUNDARY
    + r"|"
    + _UNLABELLED_CJK_STREET_ADDRESS_INLINE_VALUE
    + _UNPUNCTUATED_CJK_ADDRESS_BOUNDARY
    + r")"
)
_UNLABELLED_CJK_RESIDENCE_ADDRESS_VALUE = re.compile(
    r"(?:"
    + _UNLABELLED_CJK_COMPOUND_ADDRESS_INLINE_VALUE
    + r"|"
    + _UNLABELLED_CJK_STREET_ADDRESS_INLINE_VALUE
    + r")"
)
_UNLABELLED_CJK_COMPOUND_ADDRESS_ROW = re.compile(
    _UNLABELLED_ADDRESS_ROW_PREFIX + r"(?:"
    + _UNLABELLED_CJK_COMPOUND_ADDRESS_INLINE_VALUE
    + r"|"
    + _UNLABELLED_CJK_STREET_ADDRESS_INLINE_VALUE
    + r")"
    + _UNLABELLED_CJK_ADDRESS_ROW_SUFFIX
    + r"[ \t]*[,，、;；.!?。！？…⋯]*$"
)
_UNLABELLED_CJK_ADDRESS_ROW_TAIL = re.compile(
    r"(?:"
    + _UNLABELLED_CJK_COMPOUND_ADDRESS_INLINE_VALUE
    + r"|"
    + _UNLABELLED_CJK_STREET_ADDRESS_INLINE_VALUE
    + r")"
    + _UNLABELLED_CJK_ADDRESS_ROW_SUFFIX
    + r"[ \t]*[,，、;；.!?。！？…⋯]*$"
)
_UNLABELLED_ENGLISH_STREET_ADDRESS_VALUE = (
    r"\d{1,6}[A-Za-z]?(?:[-/]\d{1,6}[A-Za-z]?)?[ \t]+"
    r"[A-Za-z0-9#.'-]{1,40}(?:[ \t]+[A-Za-z0-9#.'-]{1,40}){0,5}[ \t]+"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|"
    r"lane|ln\.?|drive|dr\.?|court|ct\.?|place|pl\.?|highway|hwy\.?|"
    r"parkway|pkwy\.?)"
)
_UNLABELLED_ENGLISH_STREET_ADDRESS_LABEL_TAIL = re.compile(
    _UNLABELLED_ENGLISH_STREET_ADDRESS_VALUE
    + r"[ \t]*[,;.!?。！？…⋯]*$",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_COUNTRY_NAME = (
    r"(?:U\.?S\.?(?:A\.?)?|United States(?:\s+of\s+America)?)"
)
_UNLABELLED_ENGLISH_STATE_CODE = (
    r"(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|"
    r"UT|VT|VA|WA|WV|WI|WY|DC)"
)
_UNLABELLED_ENGLISH_STATE_CODE_UPPER = (
    r"(?-i:" + _UNLABELLED_ENGLISH_STATE_CODE + r")"
)
_UNLABELLED_ENGLISH_CITY_STATE_CONTINUATION = (
    r"(?:[A-Za-z][A-Za-z .'-]{1,48},[ \t]*"
    + _UNLABELLED_ENGLISH_STATE_CODE
    + r"[ \t]+\d{5}(?:-\d{4})?|"
    r"[A-Za-z][A-Za-z .'-]{1,48}[ \t]+"
    + _UNLABELLED_ENGLISH_STATE_CODE
    + r"[ \t]+\d{5}(?:-\d{4})?|"
    r"(?=[A-Za-z .'-]*(?-i:[a-z]))[A-Za-z][A-Za-z .'-]{1,48},[ \t]*"
    + _UNLABELLED_ENGLISH_STATE_CODE_UPPER
    + r"|(?=[A-Z .'-]{4,},)[A-Z][A-Z .'-]{3,},[ \t]*"
    + _UNLABELLED_ENGLISH_STATE_CODE_UPPER
    + r")"
)
_UNLABELLED_ENGLISH_ADDRESS_UNIT_FULL_VALUE = r"[A-Za-z0-9-]+"
_UNLABELLED_ENGLISH_ADDRESS_UNIT_CONTINUATION_VALUE = (
    r"(?:(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]+|[A-Z]|"
    r"(?:PH|PENTHOUSE)(?:-[A-Za-z0-9]{1,6})?)"
)
_UNLABELLED_ENGLISH_LOCALITY_ROW = re.compile(
    r"^(?:[A-Za-z][A-Za-z .'-]{1,48},[ \t]*"
    + _UNLABELLED_ENGLISH_STATE_CODE
    + r"[ \t]+\d{5}(?:-\d{4})?|"
    r"[A-Za-z][A-Za-z .'-]{1,48}[ \t]+"
    + _UNLABELLED_ENGLISH_STATE_CODE
    + r"[ \t]+\d{5}(?:-\d{4})?)"
    + r"(?:[,;]\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r")?"
    + r"(?:[ \t]*\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\))?"
    + r"[ \t]*[,;.!?。！？…⋯]*$",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_STREET_ADDRESS_ROW = re.compile(
    _UNLABELLED_ADDRESS_ROW_PREFIX
    + _UNLABELLED_ENGLISH_STREET_ADDRESS_VALUE
    + r"(?=$|[ \t]*[,;.!?。！？…⋯]|[ \t]*\(|[ \t]+(?:apt\.?|apartment|unit|suite|floor|#)\b)"
    + r"(?:(?:[ \t]+|[,;][ \t]*)(?:apt\.?|apartment|unit|suite|floor|#)\s*"
    + _UNLABELLED_ENGLISH_ADDRESS_UNIT_FULL_VALUE
    + r")?"
    + r"(?:[,;]\s*[A-Za-z][A-Za-z .'-]{0,48}"
    + r"(?:[, \t]+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)?"
    + r"(?:[,;]\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r")?"
    + r")?"
    + r"(?:[ \t]*\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\))?"
    + r"[ \t]*[,;.!?。！？…⋯]*$",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_STREET_ADDRESS_INLINE = re.compile(
    r"(?<![A-Za-z0-9])"
    + _UNLABELLED_ENGLISH_STREET_ADDRESS_VALUE
    + r"(?:"
    + r"(?:(?:[ \t]+|[,;][ \t]*)(?:apt\.?|apartment|unit|suite|floor|#)\s*"
    + _UNLABELLED_ENGLISH_ADDRESS_UNIT_FULL_VALUE
    + r"(?:[,;]\s*[A-Za-z][A-Za-z .'-]{0,48}[, \t]+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)?"
    + r"|[,;]\s*[A-Za-z][A-Za-z .'-]{0,48}[, \t]+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)"
    + r"(?:[,;]\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r")?"
    + r"(?:[ \t]*\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\))?"
    + r"|[,;]\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME
    + r"|[ \t]*\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\))"
    + r"(?=$|[ \t]*[.!?。！？…⋯])",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_ADDRESS_CONTINUATION_ROW = re.compile(
    r"^(?:"
    + r"(?:apt\.?|apartment|unit|suite|floor|#)\s*"
    + _UNLABELLED_ENGLISH_ADDRESS_UNIT_CONTINUATION_VALUE
    + r"|"
    + _UNLABELLED_ENGLISH_CITY_STATE_CONTINUATION
    + r"(?:[,;]\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r")?"
    + r"(?:[ \t]*\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\))?"
    + r"|\d{5}(?:-\d{4})?|"
    + _UNLABELLED_ENGLISH_COUNTRY_NAME
    + r"|\(\s*" + _UNLABELLED_ENGLISH_COUNTRY_NAME + r"\s*\)"
    + r")[ \t]*[,;.!?。！？…⋯]*$",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_NONLOCALITY_CONTINUATION = re.compile(
    r"^certificate\s+authority\s*,\s*"
    + _UNLABELLED_ENGLISH_STATE_CODE_UPPER
    + r"[ \t]*[,;.!?。！？…⋯]*$",
    re.IGNORECASE,
)
_ADDRESS_LABEL_VALUE_ON_NEXT_LINE = re.compile(
    r"(?:地址|住址|(?<![A-Za-z])(?:home\s+)?address)"
    r"(?:\s*[:=]\s*|\s*)$",
    re.IGNORECASE,
)
_ADDRESS_FIELD_LABEL = re.compile(
    r"(?<![A-Za-z])(?:地址|住址|(?:home\s+)?address)\s*[:=]",
    re.IGNORECASE,
)
_UNPUNCTUATED_ENGLISH_ADDRESS_LABEL_VALUE = re.compile(
    r"^(?:home\s+)?address\s+(.+)$",
    re.IGNORECASE,
)
_UNPUNCTUATED_CJK_ADDRESS_LABEL_VALUE = re.compile(r"^(?:地址|住址)\s+(.+)$")
_UNPUNCTUATED_INLINE_ADDRESS_LABEL = re.compile(
    r"(?<![A-Za-z])(?:home\s+)?address\s+|(?:地址|住址)\s*[:=]?\s*",
    re.IGNORECASE,
)
_UNLABELLED_ENGLISH_RESIDENCE_CUE = re.compile(
    r"\b(?:live|lives|living|lived|reside|resides|resided|residing)"
    r"\s+(?:at|in|near|on)\b|\b(?:home\s+)?(?:address|residence)\s+(?:is|at)\b",
    re.IGNORECASE,
)
_UNLABELLED_CJK_RESIDENCE_CUE = re.compile(
    r"(?:目前|当前|现在|现)?(?:居(?:住)?(?:在|于)|住在|家住)"
)
_UNLABELLED_ENGLISH_ADDRESS_AFTER_CUE = re.compile(
    _UNLABELLED_ENGLISH_STREET_ADDRESS_VALUE
    + r"(?=$|[ \t,;.!?。！？…⋯])",
    re.IGNORECASE,
)
_UNLABELLED_INLINE_ADDRESS_PREFIX = re.compile(
    r"(?:[•●▪◦*|]|(?>(?:"
    + _UNLABELLED_DASH_CHARS
    + r")+))[ \t]*"
)
_UNLABELLED_INLINE_ADDRESS_SEPARATOR = re.compile(
    r"[;；|｜•●▪◦*]"
    r"|(?<=\s)[-‐‑‒–—―−]{1,32}(?=\s)"
    r"|(?<=[A-Za-z0-9\u3400-\u9fff])(?>(?:"
    + _UNLABELLED_DASH_CHARS
    + r")+)(?=[ \t]*[\d\u3400-\u9fff])"
    r"|(?<=\s)(?>(?:"
    + _UNLABELLED_DASH_CHARS
    + r")+)(?=[ \t]*[\d\u3400-\u9fff])"
)


def _contains_unlabelled_inline_address(value: str) -> bool:
    """Detect high-confidence addresses embedded in a prose line."""
    for label in _UNPUNCTUATED_INLINE_ADDRESS_LABEL.finditer(value):
        address_start = label.end()
        while address_start < len(value) and value[address_start] in " \t:：,，":
            address_start += 1
        if (
            _UNLABELLED_ENGLISH_STREET_ADDRESS_LABEL_TAIL.fullmatch(value, address_start)
            or _UNLABELLED_CJK_ADDRESS_ROW_TAIL.fullmatch(value, address_start)
            or _UNLABELLED_ENGLISH_STREET_ADDRESS_INLINE.match(value, address_start)
            or _UNLABELLED_CJK_INLINE_ADDRESS_VALUE.match(value, address_start)
        ):
            return True
    for cue in _UNLABELLED_CJK_RESIDENCE_CUE.finditer(value):
        address_start = cue.end()
        while address_start < len(value) and value[address_start] in " \t:：,，":
            address_start += 1
        if _UNLABELLED_CJK_RESIDENCE_ADDRESS_VALUE.match(value, address_start):
            return True
    for cue in _UNLABELLED_ENGLISH_RESIDENCE_CUE.finditer(value):
        address_start = cue.end()
        while address_start < len(value) and value[address_start] in " \t:：,，":
            address_start += 1
        if _UNLABELLED_ENGLISH_ADDRESS_AFTER_CUE.match(value, address_start):
            return True
    for separator in _UNLABELLED_INLINE_ADDRESS_SEPARATOR.finditer(value):
        address_start = separator.end()
        while address_start < len(value) and value[address_start].isspace():
            address_start += 1
        prefix = _UNLABELLED_INLINE_ADDRESS_PREFIX.match(value, address_start)
        if prefix:
            address_start = prefix.end()
        if (
            _UNLABELLED_CJK_INLINE_ADDRESS_VALUE.match(value, address_start)
            or _UNLABELLED_ENGLISH_STREET_ADDRESS_INLINE.match(value, address_start)
            or _UNLABELLED_CJK_ADDRESS_ROW_TAIL.fullmatch(value, address_start)
            or _UNLABELLED_ENGLISH_STREET_ADDRESS_LABEL_TAIL.fullmatch(value, address_start)
        ):
            return True
    return False


def _is_unlabelled_english_address_continuation(value: str) -> bool:
    return bool(
        _UNLABELLED_ENGLISH_ADDRESS_CONTINUATION_ROW.fullmatch(value)
        and not _UNLABELLED_ENGLISH_NONLOCALITY_CONTINUATION.fullmatch(value)
    )


_UNLABELLED_CJK_ADDRESS_CONTINUATION_ROW = re.compile(
    r"^(?:"
    + _UNLABELLED_CJK_ADDRESS_COMPONENT
    + r"[ \t]*(?:期|区|座|楼|栋|幢|单元|层|室|号)|"
    + r"(?:期|区|座|楼|栋|幢|单元|层|室|号))"
    + r"(?:[,，]?[ \t]*"
    + _UNLABELLED_CJK_ADDRESS_COMPONENT
    + r"[ \t]*(?:期|区|座|楼|栋|幢|单元|层|室|号))*"
    + r"[,，。.!?；;]*$"
)


def _is_unlabelled_address_continuation(value: str) -> bool:
    return (
        _is_unlabelled_english_address_continuation(value)
        or bool(_UNLABELLED_CJK_ADDRESS_CONTINUATION_ROW.fullmatch(value))
    )


class IntegrationReadError(RuntimeError):
    def __init__(self, code: str, status_code: int = 422):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class _CurrentCandidate:
    candidate: Candidate
    resume: Resume
    snapshot: ResumeFactSnapshot
    payload: dict[str, Any]


@dataclass
class _IntegrationSanitizationState:
    omit_next_value: bool = False
    pending_address_label: bool = False
    omit_address_continuation: bool = False


def _assert_read_context(session: Session) -> None:
    assert_integration_context(session, organization_context_id(session))


def _validate_resource_ids(*values: str) -> None:
    if any(not value or len(value) > 128 for value in values):
        raise IntegrationReadError("integration_resource_not_found", 404)


def candidate_code(candidate_id: str) -> str:
    """Stable pseudonymous display code; never derived from candidate data."""

    compact = re.sub(r"[^A-Za-z0-9]", "", candidate_id).upper()
    return f"GS-{compact}"


def execute_integration_read(
    session: Session,
    *,
    principal: "IntegrationPrincipal",
    settings: AppSettings,
    action: str,
    resource_type: str,
    read: Callable[[], ReadResult],
    resource_ids: Callable[[ReadResult], Collection[str]] = lambda _: (),
    candidate_ids: Callable[[ReadResult], Collection[str]] = lambda _: (),
    request_id: str | None = None,
) -> ReadResult:
    """Run the one approved auth-adjacent quota/audit chain for all transports."""

    from app.services.integration_limit_service import (
        begin_integration_request,
        finalize_integration_read,
        release_integration_request,
        collect_integration_read_sources,
    )

    lease = begin_integration_request(
        session,
        principal,
        settings=settings,
        action=action,
        request_id=request_id,
    )
    finalized = False
    try:
        with collect_integration_read_sources(session) as collector:
            result = read()
            provenance = collector.bind(session, result, organization_id=principal.organization_id,
                user_id=principal.user_id, membership_id=principal.membership_id)
        finalize_integration_read(
            session,
            principal,
            settings=settings,
            action=action,
            resource_type=resource_type,
            resource_ids=resource_ids(result),
            candidate_ids=candidate_ids(result),
            request_id=request_id,
            lease_id=lease.id,
            provenance=provenance,
        )
        finalized = True
        return result
    finally:
        if not finalized:
            # Never let the lease-release commit make a failed read/write's
            # partially staged business rows durable.
            session.rollback()
            release_integration_request(session, principal, lease_id=lease.id)


def sanitize_integration_text(
    value: object,
    *,
    candidate_name: str | None = None,
    max_chars: int,
) -> tuple[str | None, bool]:
    """Remove known identity/contact data and return ``(text, truncated)``.

    An empty result is omitted. This is a conservative boundary, not a claim
    of perfect anonymization; callers must keep an omission marker.
    """

    if not isinstance(value, str):
        return None, False
    safe_lines = _sanitize_integration_lines(value, _IntegrationSanitizationState())
    return _finish_sanitized_integration_lines(
        safe_lines, candidate_name=candidate_name, max_chars=max_chars,
    )


def sanitize_integration_text_blocks(
    blocks: Collection[tuple[str, int, str]],
    *,
    candidate_name: str | None = None,
    max_chars: int,
) -> dict[str, tuple[str | None, bool]]:
    """Sanitize ordered source blocks with address context shared per resume."""
    state = _IntegrationSanitizationState()
    result: dict[str, tuple[str | None, bool]] = {}
    for block_id, page_no, value in sorted(blocks, key=lambda item: (item[1], item[0])):
        safe_lines = _sanitize_integration_lines(value, state)
        result[block_id] = _finish_sanitized_integration_lines(
            safe_lines, candidate_name=candidate_name, max_chars=max_chars,
        )
    return result


def _sanitize_integration_lines(
    value: object,
    state: _IntegrationSanitizationState,
) -> list[str]:
    if not isinstance(value, str):
        return []
    # Normalize full-width labels/digits and omit entire unsafe physical lines;
    # removing just a label would leave its private value in the response.
    text = "".join(character for character in value if unicodedata.category(character) != "Cf")
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARACTERS.sub(" ", text)
    safe_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if state.omit_address_continuation:
            if _is_unlabelled_address_continuation(stripped):
                continue
            state.omit_address_continuation = False
        # Some PDF/text extractors omit punctuation after a private label
        # (for example, "性别 男" or "Gender Female"). Omit the full line.
        if _PRIVATE_LABEL_WHITESPACE_VALUE.fullmatch(stripped):
            state.omit_next_value = False
            state.pending_address_label = False
            address_value = (
                _UNPUNCTUATED_ENGLISH_ADDRESS_LABEL_VALUE.fullmatch(stripped)
                or _UNPUNCTUATED_CJK_ADDRESS_LABEL_VALUE.fullmatch(stripped)
            )
            if address_value:
                value_text = address_value.group(1).strip()
                state.omit_address_continuation = bool(
                    _UNLABELLED_ENGLISH_STREET_ADDRESS_ROW.fullmatch(value_text)
                    or _UNLABELLED_CJK_COMPOUND_ADDRESS_ROW.fullmatch(value_text)
                )
            continue
        # PDF extraction can put a label and its value on separate lines.
        # Dropping only the label would disclose the following contact value.
        if (_PRIVATE_LABEL_VALUE_ON_NEXT_LINE.search(stripped)
                or _AMBIGUOUS_SOCIAL_LABEL_ONLY.fullmatch(stripped)):
            state.omit_next_value = True
            state.pending_address_label = bool(_ADDRESS_LABEL_VALUE_ON_NEXT_LINE.search(stripped))
            continue
        label = _PRIVATE_LABEL.search(line)
        if label:
            value_after_label = line[label.end():].strip()
            state.omit_next_value = not value_after_label
            address_label = _ADDRESS_FIELD_LABEL.search(line)
            state.pending_address_label = bool(address_label and not value_after_label)
            if address_label and value_after_label:
                address_value = line[address_label.end():].strip()
                state.omit_address_continuation = bool(
                    _UNLABELLED_ENGLISH_STREET_ADDRESS_ROW.fullmatch(address_value)
                    or _UNLABELLED_CJK_COMPOUND_ADDRESS_ROW.fullmatch(address_value)
                )
            continue
        if state.omit_next_value:
            state.omit_next_value = False
            state.omit_address_continuation = bool(
                state.pending_address_label
                and (
                    _UNLABELLED_ENGLISH_STREET_ADDRESS_ROW.fullmatch(stripped)
                    or _UNLABELLED_CJK_COMPOUND_ADDRESS_ROW.fullmatch(stripped)
                )
            )
            state.pending_address_label = False
            continue
        english_address_row = _UNLABELLED_ENGLISH_STREET_ADDRESS_ROW.fullmatch(stripped)
        inline_address = _contains_unlabelled_inline_address(stripped)
        if (
            _UNLABELLED_CJK_COMPOUND_ADDRESS_ROW.fullmatch(stripped)
            or english_address_row
            or inline_address
        ):
            state.omit_address_continuation = True
            continue
        if (_SOCIAL_CONTACT_VALUE.search(line) or _PRIVATE_CONTACT_URL.search(line)
                or _IDENTITY_DOCUMENT.search(line)):
            continue
        safe_lines.append(line)
    return safe_lines


def _finish_sanitized_integration_lines(
    safe_lines: list[str],
    *,
    candidate_name: str | None,
    max_chars: int,
) -> tuple[str | None, bool]:
    text = "\n".join(safe_lines)
    text = redact_nonessential_personal_data(text)
    if candidate_name and candidate_name.strip():
        normalized_name = "".join(
            character for character in candidate_name
            if unicodedata.category(character) != "Cf"
        )
        normalized_name = unicodedata.normalize("NFKC", normalized_name)
        name_parts = _CONTROL_CHARACTERS.sub(" ", normalized_name).split()
        if name_parts:
            pattern = r"\s+".join(re.escape(part) for part in name_parts)
            text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip(" ·,;:，；：-")
    if not text:
        return None, False
    truncated = len(text) > max_chars
    if truncated:
        text = f"{text[: max_chars - 1].rstrip()}…"
    return text, truncated


def _safe_optional_text(
    value: object,
    *,
    candidate_name: str | None,
    omitted_fields: list[str],
    field: str,
    max_chars: int = 300,
) -> str | None:
    if value is None:
        return None
    rendered, _ = sanitize_integration_text(
        value,
        candidate_name=candidate_name,
        max_chars=max_chars,
    )
    if rendered is None:
        omitted_fields.append(field)
    return rendered


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _cursor_serializer(settings: AppSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_signing_secret(), salt=_CURSOR_SALT)


def _query_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _principal_binding(principal: "IntegrationPrincipal") -> dict[str, str]:
    return {
        "organization_id": principal.organization_id,
        "user_id": principal.user_id,
        "grant_id": principal.grant_id,
        "audience": principal.audience,
    }


def _encode_cursor(
    *,
    settings: AppSettings,
    principal: "IntegrationPrincipal",
    kind: str,
    query_digest: str,
    value: dict[str, object],
) -> str:
    return _cursor_serializer(settings).dumps(
        {
            "kind": kind,
            "query": query_digest,
            "principal": _principal_binding(principal),
            "value": value,
        }
    )


def _decode_cursor(
    cursor: str,
    *,
    settings: AppSettings,
    principal: "IntegrationPrincipal",
    kind: str,
    query_digest: str,
) -> dict[str, object]:
    if len(cursor) > 2_048:
        raise IntegrationReadError("integration_invalid_cursor", 422)
    try:
        payload = _cursor_serializer(settings).loads(
            cursor,
            max_age=_CURSOR_MAX_AGE_SECONDS,
        )
    except SignatureExpired as exc:
        raise IntegrationReadError("integration_cursor_expired", 422) from exc
    except BadData as exc:
        raise IntegrationReadError("integration_invalid_cursor", 422) from exc
    if not isinstance(payload, dict) or (
        payload.get("kind") != kind
        or payload.get("query") != query_digest
        or payload.get("principal") != _principal_binding(principal)
        or not isinstance(payload.get("value"), dict)
    ):
        raise IntegrationReadError("integration_invalid_cursor", 422)
    return payload["value"]


def get_connection_info(principal: "IntegrationPrincipal", settings: AppSettings) -> IntegrationConnectionInfo:
    return IntegrationConnectionInfo(
        audience=principal.audience,
        scopes=sorted(principal.scopes),
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        mcp_url=f"{settings.public_app_url.rstrip('/')}/v1/mcp",
        api_base_url=f"{settings.public_app_url.rstrip('/')}/v1/integrations",
    )


def get_filter_options() -> IntegrationFilterOptions:
    option = lambda item: IntegrationOption(
        value=str(item["value"]), label=str(item["label"])
    )
    return IntegrationFilterOptions(
        schema_version=f"integration.{FILTER_OPTIONS_VERSION}",
        degrees=[option(item) for item in DEGREE_OPTIONS if item["value"] in get_args(DegreeLevel)],
        institution_classifications=[
            option(item) for item in INSTITUTION_CLASSIFICATION_OPTIONS
            if item["value"] in get_args(InstitutionClassification)
        ],
        experience_types=[option(item) for item in EXPERIENCE_TYPE_OPTIONS if item["value"] in get_args(ExperienceType)],
        skill_categories=[option(item) for item in SKILL_CATEGORY_OPTIONS if item["value"] in get_args(SkillCategory)],
        language_credentials=[option(item) for item in LANGUAGE_CREDENTIAL_OPTIONS if item["value"] in get_args(LanguageCredentialCode)],
        graduation_statuses=[
            IntegrationOption(value="any", label="不限"),
            IntegrationOption(value="fresh", label="应届"),
            IntegrationOption(value="previous", label="往届"),
        ],
        presence_statuses=[
            IntegrationOption(value="any", label="不限"),
            IntegrationOption(value="present", label="有明确记录"),
            IntegrationOption(value="unknown", label="未知"),
        ],
        keyword_modes=[
            IntegrationOption(value="broad", label="任一命中"),
            IntegrationOption(value="precise", label="全部命中"),
        ],
    )


def _load_current_candidate(
    session: Session, *, candidate_id: str
) -> _CurrentCandidate:
    _assert_read_context(session)
    _validate_resource_ids(candidate_id)
    row = session.execute(
        select(Candidate, Resume, ResumeFactSnapshot)
        .join(Resume, Resume.candidate_id == Candidate.id)
        .join(
            ResumeFactSnapshot,
            and_(
                ResumeFactSnapshot.resume_id == Resume.id,
                ResumeFactSnapshot.facts_version == Resume.facts_version,
            ),
        )
        .where(
            Candidate.id == candidate_id,
            Resume.is_active.is_(True),
            Resume.extraction_status == "ready",
        )
    ).first()
    if row is None:
        raise IntegrationReadError("integration_resource_not_found", 404)
    candidate, resume, snapshot = row
    if not is_resume_screening_eligible(resume):
        raise IntegrationReadError("integration_resource_not_found", 404)
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except (TypeError, ValueError) as exc:
        raise IntegrationReadError(
            "integration_fact_snapshot_unavailable", 503
        ) from exc
    if not isinstance(payload, dict):
        raise IntegrationReadError("integration_fact_snapshot_unavailable", 503)
    return _CurrentCandidate(
        candidate=candidate,
        resume=resume,
        snapshot=snapshot,
        payload=payload,
    )


def _all_fact_ids(payload: dict[str, Any]) -> frozenset[str]:
    fact_ids: set[str] = set()
    for section in (
        "education",
        "experiences",
        "skills",
        "language_credentials",
        "scholarships",
    ):
        entries = payload.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("fact_id"), str):
                fact_ids.add(entry["fact_id"])
    return frozenset(fact_ids)


def fact_reference_ids(profile: IntegrationCandidateProfile) -> frozenset[str]:
    """Public helper used by analysis-draft validation; never trusts caller IDs."""

    return frozenset(
        fact.fact_id
        for facts in (
            profile.facts.education,
            profile.facts.experiences,
            profile.facts.skills,
            profile.facts.language_credentials,
            profile.facts.scholarships,
        )
        for fact in facts
    )


def _profile_from_current(current: _CurrentCandidate) -> IntegrationCandidateProfile:
    candidate_name = current.candidate.display_name
    omitted: list[str] = [
        "candidate_name",
        "contacts",
        "original_file",
        "raw_resume_text",
    ]
    payload = current.payload

    education: list[IntegrationEducationFact] = []
    for index, entry in enumerate(payload.get("education") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_id"), str):
            continue
        prefix = f"education[{index}]"
        evidence_ids = sorted(
            set(_string_list(entry.get("evidence_block_ids")))
            | set(_string_list(entry.get("classification_evidence_block_ids")))
        )
        education.append(
            IntegrationEducationFact(
                fact_id=entry["fact_id"],
                evidence_source_block_ids=evidence_ids,
                school=_safe_optional_text(
                    entry.get("school_name_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.school",
                ),
                degree=_optional_str(entry.get("degree")),
                major=_safe_optional_text(
                    entry.get("major_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.major",
                ),
                start_month=_optional_str(entry.get("start_month")),
                end_month=_optional_str(entry.get("end_month")),
                institution_tiers=_string_list(entry.get("institution_tiers")),
                institution_classification=_optional_str(
                    entry.get("institution_classification")
                ),
                average_score=_optional_float(entry.get("average_score")),
                gpa_value=_optional_float(entry.get("gpa_value")),
                gpa_scale=_optional_float(entry.get("gpa_scale")),
                rank_position=_optional_int(entry.get("rank_position")),
                rank_total=_optional_int(entry.get("rank_total")),
            )
        )

    experiences: list[IntegrationExperienceFact] = []
    for index, entry in enumerate(payload.get("experiences") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_id"), str):
            continue
        prefix = f"experiences[{index}]"
        details: list[IntegrationExperienceDetail] = []
        for detail_index, detail in enumerate(entry.get("detail_items") or []):
            if not isinstance(detail, dict):
                continue
            rendered = _safe_optional_text(
                detail.get("detail_raw"),
                candidate_name=candidate_name,
                omitted_fields=omitted,
                field=f"{prefix}.details[{detail_index}]",
                max_chars=500,
            )
            if rendered is not None:
                details.append(
                    IntegrationExperienceDetail(
                        detail=rendered,
                        evidence_source_block_ids=_string_list(
                            detail.get("evidence_block_ids")
                        ),
                    )
                )
        evidence_ids = sorted(
            set(_string_list(entry.get("evidence_block_ids")))
            | set(_string_list(entry.get("classification_evidence_block_ids")))
            | {
                block_id
                for detail in details
                for block_id in detail.evidence_source_block_ids
            }
        )
        experiences.append(
            IntegrationExperienceFact(
                fact_id=entry["fact_id"],
                evidence_source_block_ids=evidence_ids,
                experience_type=_optional_str(entry.get("experience_type")),
                experience_name=_safe_optional_text(
                    entry.get("experience_name_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.experience_name",
                ),
                organization=_safe_optional_text(
                    entry.get("organization_name_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.organization",
                ),
                title=_safe_optional_text(
                    entry.get("title_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.title",
                ),
                start_month=_optional_str(entry.get("start_month")),
                end_month=_optional_str(entry.get("end_month")),
                is_current=_optional_bool(entry.get("is_current")),
                leadership_context=_optional_str(entry.get("leadership_context")),
                leadership_role=_safe_optional_text(
                    entry.get("leadership_role"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.leadership_role",
                ),
                award_level=_optional_str(entry.get("award_level")),
                award_result=_safe_optional_text(
                    entry.get("award_result_raw"),
                    candidate_name=candidate_name,
                    omitted_fields=omitted,
                    field=f"{prefix}.award_result",
                ),
                details=details,
            )
        )

    skills: list[IntegrationSkillFact] = []
    for index, entry in enumerate(payload.get("skills") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_id"), str):
            continue
        rendered = _safe_optional_text(
            entry.get("skill_display"),
            candidate_name=candidate_name,
            omitted_fields=omitted,
            field=f"skills[{index}].skill",
        )
        if rendered is not None:
            skills.append(
                IntegrationSkillFact(
                    fact_id=entry["fact_id"],
                    evidence_source_block_ids=_string_list(
                        entry.get("evidence_block_ids")
                    ),
                    skill=rendered,
                    category=_optional_str(entry.get("skill_category")),
                )
            )

    languages: list[IntegrationLanguageFact] = []
    for index, entry in enumerate(payload.get("language_credentials") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_id"), str):
            continue
        credential = entry.get("credential_name_raw") or entry.get("credential_code")
        rendered = _safe_optional_text(
            credential,
            candidate_name=candidate_name,
            omitted_fields=omitted,
            field=f"language_credentials[{index}].credential",
        )
        if rendered is not None:
            languages.append(
                IntegrationLanguageFact(
                    fact_id=entry["fact_id"],
                    evidence_source_block_ids=_string_list(
                        entry.get("evidence_block_ids")
                    ),
                    credential=rendered,
                    score=_safe_optional_text(
                        entry.get("score"),
                        candidate_name=candidate_name,
                        omitted_fields=omitted,
                        field=f"language_credentials[{index}].score",
                    ),
                )
            )

    scholarships: list[IntegrationScholarshipFact] = []
    for index, entry in enumerate(payload.get("scholarships") or []):
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_id"), str):
            continue
        rendered = _safe_optional_text(
            entry.get("scholarship_name_raw"),
            candidate_name=candidate_name,
            omitted_fields=omitted,
            field=f"scholarships[{index}].name",
        )
        if rendered is not None:
            scholarships.append(
                IntegrationScholarshipFact(
                    fact_id=entry["fact_id"],
                    evidence_source_block_ids=_string_list(
                        entry.get("evidence_block_ids")
                    ),
                    name=rendered,
                    level=_optional_str(entry.get("scholarship_level")),
                )
            )

    derived = payload.get("derived") if isinstance(payload.get("derived"), dict) else {}
    facts = IntegrationCandidateFacts(
        is_985_211=_optional_bool(derived.get("is_985_211")),
        highest_degree=_optional_str(derived.get("highest_degree")),
        employment_months=_optional_int(derived.get("employment_months")),
        employment_or_internship_months=_optional_int(
            derived.get("employment_or_internship_months")
        ),
        education=education,
        experiences=experiences,
        skills=skills,
        language_credentials=languages,
        scholarships=scholarships,
    )
    return IntegrationCandidateProfile(
        candidate_id=current.candidate.id,
        candidate_code=candidate_code(current.candidate.id),
        resume_id=current.resume.id,
        fact_snapshot_id=current.snapshot.id,
        facts_version=current.snapshot.facts_version,
        facts=facts,
        evidence_source_block_ids=sorted(
            set(_string_list(payload.get("source_block_ids")))
            & set(current.snapshot.source_block_ids or [])
        ),
        omitted_fields=sorted(set(omitted)),
    )


def get_candidate_profile(
    session: Session, *, candidate_id: str
) -> IntegrationCandidateProfile:
    return _profile_from_current(
        _load_current_candidate(session, candidate_id=candidate_id)
    )


def get_candidate_profile_snapshot(
    session: Session,
    *,
    candidate_id: str,
    resume_id: str,
    fact_snapshot_id: str,
    facts_version: int,
) -> IntegrationCandidateProfile:
    """Project one pinned source version while its privacy roots remain live."""

    _assert_read_context(session)
    _validate_resource_ids(candidate_id, resume_id, fact_snapshot_id)
    row = session.execute(
        select(Candidate, Resume, ResumeFactSnapshot)
        .join(
            Resume,
            and_(
                Resume.candidate_id == Candidate.id,
                Resume.id == resume_id,
            ),
        )
        .join(
            ResumeFactSnapshot,
            and_(
                ResumeFactSnapshot.resume_id == Resume.id,
                ResumeFactSnapshot.id == fact_snapshot_id,
                ResumeFactSnapshot.facts_version == facts_version,
            ),
        )
        .where(Candidate.id == candidate_id)
    ).first()
    if row is None:
        raise IntegrationReadError("integration_resource_not_found", 404)
    candidate, resume, snapshot = row
    try:
        payload = json.loads(snapshot.canonical_facts_json)
    except (TypeError, ValueError) as exc:
        raise IntegrationReadError(
            "integration_fact_snapshot_unavailable", 503
        ) from exc
    if not isinstance(payload, dict):
        raise IntegrationReadError("integration_fact_snapshot_unavailable", 503)
    return _profile_from_current(
        _CurrentCandidate(
            candidate=candidate,
            resume=resume,
            snapshot=snapshot,
            payload=payload,
        )
    )


def _validate_external_free_text(
    session: Session,
    request: IntegrationCandidateSearchRequest,
) -> None:
    values = [
        *request.skills_all_of,
        *request.skills_any_of,
        *request.keywords_all_of,
        *request.keywords_any_of,
    ]
    if not values:
        return
    for value in values:
        redacted, _ = sanitize_integration_text(value, max_chars=300)
        if redacted != " ".join(value.split()):
            raise IntegrationReadError(
                "integration_sensitive_filter_not_supported", 422
            )


def _external_search_source_blocks(resume: Resume) -> dict[str, str]:
    projected = sanitize_integration_text_blocks(
        [
            (block.block_id, block.page_no, block.text)
            for block in resume.source_blocks
        ],
        candidate_name=resume.candidate.display_name,
        max_chars=100_000,
    )
    return {block_id: text or "" for block_id, (text, _truncated) in projected.items()}


def _external_search_skill(resume: Resume, text: str) -> str:
    """Match skills only after applying the same identity projection as output."""
    projected, _ = sanitize_integration_text(
        text, candidate_name=resume.candidate.display_name, max_chars=100_000,
    )
    return projected or ""


def search_candidates(
    session: Session,
    *,
    principal: "IntegrationPrincipal",
    settings: AppSettings,
    request: IntegrationCandidateSearchRequest,
) -> IntegrationCandidateSearchResponse:
    assert_integration_context(session, principal.organization_id)
    _validate_external_free_text(session, request)
    query_payload = request.model_dump(exclude={"cursor"}, mode="json")
    digest = _query_digest(query_payload)
    internal_cursor: str | None = None
    if request.cursor:
        value = _decode_cursor(
            request.cursor,
            settings=settings,
            principal=principal,
            kind="candidates",
            query_digest=digest,
        )
        internal_cursor = (
            value.get("internal") if isinstance(value.get("internal"), str) else None
        )
        if internal_cursor is None:
            raise IntegrationReadError("integration_invalid_cursor", 422)
    try:
        internal_request = CandidateSearchRequest(
            condition_match_mode=request.condition_match_mode,
            is_985_211=request.is_985_211,
            education_degree_in=request.education_degree_in,
            education_any_of=(
                [
                    EducationFilter(
                        institution_classifications_any_of=request.institution_classifications_any_of,
                    )
                ]
                if request.institution_classifications_any_of
                else []
            ),
            highest_degree_in=request.highest_degree_in,
            graduation_status=request.graduation_status,
            fresh_graduate_start_month=request.fresh_graduate_start_month,
            fresh_graduate_end_month=request.fresh_graduate_end_month,
            min_employment_months=request.min_employment_months,
            min_employment_or_internship_months=request.min_employment_or_internship_months,
            experience_types_all_of=request.experience_types_all_of,
            skill_categories_any_of=request.skill_categories_any_of,
            skills_all_of=request.skills_all_of,
            skills_any_of=request.skills_any_of,
            language_credentials_any_of=[
                LanguageCredentialFilter(credential_code=code)
                for code in request.language_credentials_any_of
            ],
            keywords_all_of=request.keywords_all_of,
            keywords_any_of=request.keywords_any_of,
            keyword_match_mode=request.keyword_match_mode,
            scholarship_status=request.scholarship_status,
            competition_status=request.competition_status,
            competition_award_status=request.competition_award_status,
            limit=request.limit,
            cursor=internal_cursor,
        )
    except ValidationError:
        # Do not leak submitted filter text in framework validation diagnostics.
        # Keep external failures stable if the browser filter contract changes.
        raise IntegrationReadError("integration_invalid_filter", 422) from None
    current_resume_ids = set(
        session.scalars(
            select(Resume.id)
            .join(
                ResumeFactSnapshot,
                and_(
                    ResumeFactSnapshot.resume_id == Resume.id,
                    ResumeFactSnapshot.facts_version == Resume.facts_version,
                ),
            )
            .where(
                Resume.is_active.is_(True),
                Resume.extraction_status == "ready",
            )
        ).all()
    )
    try:
        with (
            projected_search_source_blocks(_external_search_source_blocks),
            projected_search_skills(_external_search_skill),
        ):
            internal = search_internal_candidates(
                session,
                internal_request,
                resume_ids=current_resume_ids,
            )
    except SearchValidationError as exc:
        raise IntegrationReadError(str(exc), 422) from exc

    resume_ids = [item.resume_id for item in internal.items]
    snapshots = (
        {
            snapshot.resume_id: snapshot
            for snapshot in session.scalars(
                select(ResumeFactSnapshot)
                .join(Resume, Resume.id == ResumeFactSnapshot.resume_id)
                .where(
                    ResumeFactSnapshot.resume_id.in_(resume_ids),
                    ResumeFactSnapshot.facts_version == Resume.facts_version,
                )
            ).all()
        }
        if resume_ids
        else {}
    )
    output: list[IntegrationCandidateSearchItem] = []
    include_assessments = "assessments:read" in principal.scopes
    for item in internal.items:
        snapshot = snapshots.get(item.resume_id)
        if snapshot is None:
            continue
        omitted = [
            "candidate_name",
            "contacts",
            "original_file",
            "raw_resume_text",
            "summary_preview",
        ]
        if not include_assessments:
            omitted.extend(("score_total", "score_status"))
        name = item.display_name
        output.append(
            IntegrationCandidateSearchItem(
                candidate_id=item.candidate_id,
                candidate_code=candidate_code(item.candidate_id),
                resume_id=item.resume_id,
                fact_snapshot_id=snapshot.id,
                facts_version=snapshot.facts_version,
                is_985_211=item.is_985_211,
                highest_degree=item.highest_degree,
                employment_months=item.employment_months,
                employment_or_internship_months=item.employment_or_internship_months,
                education_school=_safe_optional_text(
                    item.education_school,
                    candidate_name=name,
                    omitted_fields=omitted,
                    field="education_school",
                ),
                education_major=_safe_optional_text(
                    item.education_major,
                    candidate_name=name,
                    omitted_fields=omitted,
                    field="education_major",
                ),
                latest_experience_title=_safe_optional_text(
                    item.latest_experience_title,
                    candidate_name=name,
                    omitted_fields=omitted,
                    field="latest_experience_title",
                ),
                latest_experience_organization=_safe_optional_text(
                    item.latest_experience_organization,
                    candidate_name=name,
                    omitted_fields=omitted,
                    field="latest_experience_organization",
                ),
                latest_experience_type=item.latest_experience_type,
                skill_highlights=[
                    text
                    for index, value in enumerate(item.skill_highlights)
                    if (
                        text := _safe_optional_text(
                            value,
                            candidate_name=name,
                            omitted_fields=omitted,
                            field=f"skill_highlights[{index}]",
                        )
                    )
                    is not None
                ],
                score_total=item.score_total if include_assessments else None,
                score_status=item.score_status if include_assessments else None,
                evidence_source_block_ids=sorted(
                    {
                        block_id
                        for match in item.matched_evidence
                        for block_id in match.evidence_block_ids
                    }
                    & set(snapshot.source_block_ids or [])
                ),
                omitted_fields=sorted(set(omitted)),
            )
        )
    next_cursor = (
        _encode_cursor(
            settings=settings,
            principal=principal,
            kind="candidates",
            query_digest=digest,
            value={"internal": internal.next_cursor},
        )
        if internal.next_cursor
        else None
    )
    return IntegrationCandidateSearchResponse(
        items=output,
        next_cursor=next_cursor,
        total_count=internal.total_count,
        needs_review_count=internal.needs_review_count,
    )


def get_candidate_evidence(
    session: Session,
    *,
    candidate_id: str,
    request: IntegrationCandidateEvidenceRequest,
) -> IntegrationCandidateEvidence:
    current = _load_current_candidate(session, candidate_id=candidate_id)
    profile = _profile_from_current(current)
    allowed = set(profile.evidence_source_block_ids)
    requested = set(request.source_block_ids)
    if not requested <= allowed:
        raise IntegrationReadError("integration_evidence_not_found", 404)
    blocks = {
        block.block_id: block
        for block in session.scalars(
            select(ResumeSourceBlock).where(ResumeSourceBlock.resume_id == current.resume.id)
        ).all()
    }
    if not requested <= set(blocks):
        raise IntegrationReadError("integration_evidence_not_found", 404)
    projected_blocks = sanitize_integration_text_blocks(
        [
            (block.block_id, block.page_no, block.text)
            for block in blocks.values()
        ],
        candidate_name=current.candidate.display_name,
        max_chars=_EVIDENCE_BLOCK_MAX_CHARS,
    )
    remaining = _EVIDENCE_RESPONSE_MAX_CHARS
    excerpts: list[IntegrationEvidenceExcerpt] = []
    for block_id in request.source_block_ids:
        block = blocks[block_id]
        if remaining <= 0:
            excerpts.append(
                IntegrationEvidenceExcerpt(
                    source_block_id=block_id,
                    page_no=block.page_no,
                    text=None,
                    truncated=False,
                    omitted=True,
                    omission_reason="response_text_limit",
                )
            )
            continue
        max_chars = min(_EVIDENCE_BLOCK_MAX_CHARS, remaining)
        full_text, was_truncated = projected_blocks.get(block_id, (None, False))
        if full_text is None:
            text, truncated = None, False
        else:
            text = full_text[:max_chars]
            truncated = was_truncated or len(full_text) > max_chars
            if truncated and len(text) == max_chars:
                text = f"{text[:-1].rstrip()}…"
        if text is None:
            excerpts.append(
                IntegrationEvidenceExcerpt(
                    source_block_id=block_id,
                    page_no=block.page_no,
                    text=None,
                    truncated=False,
                    omitted=True,
                    omission_reason="unsafe_or_empty_after_redaction",
                )
            )
            continue
        remaining -= len(text)
        excerpts.append(
            IntegrationEvidenceExcerpt(
                source_block_id=block_id,
                page_no=block.page_no,
                text=text,
                truncated=truncated,
                omitted=False,
                omission_reason=None,
            )
        )
    return IntegrationCandidateEvidence(
        candidate_id=current.candidate.id,
        candidate_code=candidate_code(current.candidate.id),
        resume_id=current.resume.id,
        fact_snapshot_id=current.snapshot.id,
        facts_version=current.snapshot.facts_version,
        excerpts=excerpts,
    )


def get_candidate_assessments(
    session: Session, *, candidate_id: str
) -> IntegrationCandidateAssessments:
    current = _load_current_candidate(session, candidate_id=candidate_id)
    valid_fact_ids = _all_fact_ids(current.payload)
    profile = _profile_from_current(current)
    evidence_by_fact = {
        fact.fact_id: set(fact.evidence_source_block_ids)
        for facts in (
            profile.facts.education,
            profile.facts.experiences,
            profile.facts.skills,
            profile.facts.language_credentials,
            profile.facts.scholarships,
        )
        for fact in facts
    }
    summaries = session.scalars(
        select(ResumeSummary)
        .where(
            ResumeSummary.resume_id == current.resume.id,
            ResumeSummary.fact_snapshot_id == current.snapshot.id,
            ResumeSummary.facts_version == current.snapshot.facts_version,
            ResumeSummary.is_current.is_(True),
            ResumeSummary.status == "succeeded",
            ResumeSummary.source == "ai",
        )
        .order_by(ResumeSummary.created_at.desc(), ResumeSummary.id.desc())
        .limit(1)
    ).all()
    scores = session.scalars(
        select(ResumeScore)
        .where(
            ResumeScore.resume_id == current.resume.id,
            ResumeScore.fact_snapshot_id == current.snapshot.id,
            ResumeScore.facts_version == current.snapshot.facts_version,
            ResumeScore.status.in_(_CURRENT_SCORE_STATUSES),
        )
        .order_by(ResumeScore.created_at.desc(), ResumeScore.id.desc())
        .limit(20)
    ).all()
    summary_items: list[IntegrationSummaryAssessment] = []
    for summary in summaries:
        sections = (
            summary.content.get("sections")
            if isinstance(summary.content, dict)
            else None
        )
        output_sections: list[IntegrationSummarySection] = []
        omitted_sections: list[str] = []
        remaining = _SUMMARY_TOTAL_MAX_CHARS
        for key in _SUMMARY_SECTION_KEYS:
            section = sections.get(key) if isinstance(sections, dict) else None
            if not isinstance(section, dict):
                omitted_sections.append(f"{key}:missing_current_fact_references")
                continue
            text = section.get("content")
            fact_ids = section.get("fact_ids")
            if (
                not isinstance(fact_ids, list)
                or not fact_ids
                or any(not isinstance(item, str) for item in fact_ids)
                or not set(fact_ids) <= valid_fact_ids
            ):
                omitted_sections.append(f"{key}:invalid_current_fact_references")
                continue
            if remaining <= 0:
                omitted_sections.append(f"{key}:response_text_limit")
                continue
            rendered, _ = sanitize_integration_text(
                text,
                candidate_name=current.candidate.display_name,
                max_chars=min(_SUMMARY_SECTION_MAX_CHARS, remaining),
            )
            if rendered is None:
                omitted_sections.append(f"{key}:unsafe_or_empty_after_redaction")
                continue
            remaining -= len(rendered)
            output_sections.append(
                IntegrationSummarySection(
                    key=key,
                    text=rendered,
                    fact_ids=list(dict.fromkeys(fact_ids)),
                    evidence_source_block_ids=sorted(
                        {
                            block_id
                            for fact_id in fact_ids
                            for block_id in evidence_by_fact.get(fact_id, set())
                        }
                        & set(current.snapshot.source_block_ids or [])
                    ),
                )
            )
        summary_items.append(
            IntegrationSummaryAssessment(
                summary_id=summary.id,
                fact_snapshot_id=current.snapshot.id,
                facts_version=summary.facts_version,
                source=summary.source,
                sections=output_sections,
                omitted_sections=omitted_sections,
                created_at=summary.created_at,
            )
        )
    matches = session.scalars(
        select(JobMatch)
        .options(selectinload(JobMatch.requirement_results))
        .where(
            JobMatch.resume_id == current.resume.id,
            JobMatch.fact_snapshot_id == current.snapshot.id,
            JobMatch.facts_version == current.snapshot.facts_version,
            JobMatch.job_version_id.is_not(None),
            JobMatch.status.in_(_CURRENT_MATCH_STATUSES),
        )
        .order_by(JobMatch.created_at.desc(), JobMatch.id.desc())
        .limit(20)
    ).all()
    score_items = [
        IntegrationScoreAssessment(
            score_id=score.id,
            fact_snapshot_id=current.snapshot.id,
            facts_version=score.facts_version,
            template_id=score.template_id,
            template_version=score.template_version,
            total_score=score.total_score,
            evidence_coverage=None,
            status=score.status,
            created_at=score.created_at,
        )
        for score in scores
    ]
    match_items = [
        IntegrationJobMatchAssessment(
            match_id=match.id,
            fact_snapshot_id=current.snapshot.id,
            facts_version=match.facts_version,
            job_id=match.job_id,
            job_version_id=str(match.job_version_id),
            job_version=match.job_version,
            total_score=match.total_score,
            must_have_passed=match.must_have_passed,
            evidence_coverage=match.evidence_coverage,
            hard_requirement_status=match.hard_requirement_status,
            status=match.status,
            cited_fact_ids=sorted(
                {
                    fact_id
                    for result in match.requirement_results
                    for fact_id in (result.fact_ids or [])
                    if fact_id in valid_fact_ids
                }
            ),
            created_at=match.created_at,
        )
        for match in matches
    ]
    return IntegrationCandidateAssessments(
        candidate_id=current.candidate.id,
        candidate_code=candidate_code(current.candidate.id),
        resume_id=current.resume.id,
        fact_snapshot_id=current.snapshot.id,
        facts_version=current.snapshot.facts_version,
        summaries=summary_items,
        scores=score_items,
        job_matches=match_items,
    )


def list_jobs(
    session: Session,
    *,
    principal: "IntegrationPrincipal",
    settings: AppSettings,
    limit: int = 20,
    cursor: str | None = None,
) -> IntegrationJobList:
    assert_integration_context(session, principal.organization_id)
    if isinstance(limit, bool) or limit < 1 or limit > 100:
        raise IntegrationReadError("integration_invalid_limit", 422)
    digest = _query_digest({"limit": limit})
    cursor_updated_at: datetime | None = None
    cursor_id: str | None = None
    if cursor:
        value = _decode_cursor(
            cursor,
            settings=settings,
            principal=principal,
            kind="jobs",
            query_digest=digest,
        )
        try:
            cursor_updated_at = datetime.fromisoformat(str(value["updated_at"]))
            cursor_id = str(value["job_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrationReadError("integration_invalid_cursor", 422) from exc
    statement = (
        select(Job)
        .where(Job.kind == "job")
        .order_by(Job.updated_at.desc(), Job.id.desc())
    )
    if cursor_updated_at is not None and cursor_id is not None:
        statement = statement.where(
            or_(
                Job.updated_at < cursor_updated_at,
                and_(Job.updated_at == cursor_updated_at, Job.id < cursor_id),
            )
        )
    jobs = session.scalars(statement.limit(limit + 1)).all()
    page = jobs[:limit]
    current_versions = (
        {
            (version.job_id, version.version): version
            for version in session.scalars(
                select(JobVersion).where(
                    JobVersion.job_id.in_([job.id for job in page])
                )
            ).all()
        }
        if page
        else {}
    )
    items = [
        IntegrationJobSummary(
            job_id=job.id,
            title=sanitize_integration_text(job.title, max_chars=200)[0]
            or "未命名岗位",
            recruiting_status=job.recruiting_status,
            current_version=job.version,
            current_version_id=(
                current_versions.get((job.id, job.version)).id
                if current_versions.get((job.id, job.version)) is not None
                else None
            ),
            updated_at=job.updated_at,
        )
        for job in page
    ]
    next_cursor = None
    if len(jobs) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(
            settings=settings,
            principal=principal,
            kind="jobs",
            query_digest=digest,
            value={"updated_at": last.updated_at.isoformat(), "job_id": last.id},
        )
    return IntegrationJobList(items=items, next_cursor=next_cursor)


def get_job_requirements(
    session: Session, *, job_id: str, version_id: str
) -> IntegrationJobRequirements:
    _assert_read_context(session)
    _validate_resource_ids(job_id, version_id)
    row = session.execute(
        select(Job, JobVersion)
        .join(JobVersion, JobVersion.job_id == Job.id)
        .options(
            selectinload(JobVersion.requirements), selectinload(JobVersion.clauses)
        )
        .where(
            Job.id == job_id,
            Job.kind == "job",
            JobVersion.id == version_id,
            JobVersion.status == "confirmed",
        )
    ).first()
    if row is None:
        raise IntegrationReadError("integration_resource_not_found", 404)
    job, version = row
    requirements = []
    for item in sorted(
        version.requirements, key=lambda value: (value.sort_order, value.id)
    ):
        rendered, _ = sanitize_integration_text(item.raw_requirement, max_chars=800)
        if rendered is None:
            continue
        requirements.append(
            IntegrationJobRequirement(
                requirement_id=item.id,
                requirement_key=item.requirement_key,
                priority=item.priority,
                category=item.category,
                requirement=rendered,
                minimum_months=item.minimum_months,
                weight=item.weight,
                source_clause_ids=_string_list(item.clause_ids),
            )
        )
    clauses = []
    for item in sorted(version.clauses, key=lambda value: (value.ordinal, value.id)):
        rendered, _ = sanitize_integration_text(item.text, max_chars=1_000)
        if rendered is not None:
            clauses.append(
                IntegrationJobClause(
                    clause_id=item.clause_id, ordinal=item.ordinal, text=rendered
                )
            )
    title, _ = sanitize_integration_text(version.title or job.title, max_chars=200)
    return IntegrationJobRequirements(
        job_id=job.id,
        job_version_id=version.id,
        version=version.version,
        title=title or "未命名岗位",
        status=version.status,
        requirements=requirements,
        clauses=clauses,
    )
