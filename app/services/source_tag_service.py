"""Workspace-scoped submission-source tags for mailbox resume ingestion.

The durable source of truth is an individual ``EmailAttachmentImport``.  A
resume owns a small, query-friendly projection of every tag assigned to its
mail import events.  This deliberately avoids writing a single mutable source
onto ``Candidate``: one candidate or resume can arrive through more than one
platform without losing provenance.

Raw mail headers are inspected only while an IMAP message is in memory.  The
database stores configured matching rules and the resulting tag snapshots,
never a candidate sender address or a message subject.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses
import re
import unicodedata
from typing import Iterable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    EmailAttachmentImport,
    EmailAttachmentImportTag,
    MailboxConfig,
    MailboxSourceTagRule,
    Resume,
    ResumeSourceTag,
    SourceTag,
)
from app.schemas import (
    MailboxSourceTagRuleCreate,
    MailboxSourceTagRulePatch,
    MailboxSourceTagRuleResponse,
    SourceTagCreate,
    SourceTagPatch,
    SourceTagReference,
    SourceTagResponse,
)
from app.services.normalization import normalized_key
from app.tenant_scope import organization_context_id


class SourceTagServiceError(RuntimeError):
    """Stable, privacy-safe errors for source-tag APIs and mailbox workers."""


@dataclass(frozen=True)
class SourceTagMatch:
    """One tag selected while a mailbox message is still in memory."""

    source_tag_id: str
    display_name_snapshot: str
    assignment_kind: Literal["builtin", "mailbox_rule"]
    matched_rule_id: str | None = None


@dataclass(frozen=True)
class _BuiltinPlatform:
    key: str
    display_name: str
    sender_domains: tuple[str, ...]
    subject_keywords: tuple[str, ...]
    sort_order: int


# A deliberately conservative set.  A platform is only attached when a
# reviewed sender domain or a clear subject marker is present.  Custom inbox
# rules cover niche platforms, referral providers, and company-specific
# forwarding addresses without persisting raw sender information.
_BUILTIN_PLATFORMS: tuple[_BuiltinPlatform, ...] = (
    _BuiltinPlatform(
        key="boss",
        display_name="BOSS直聘",
        sender_domains=("zhipin.com", "zhipin.com.cn"),
        subject_keywords=("BOSS直聘",),
        sort_order=10,
    ),
    _BuiltinPlatform(
        key="zhaopin",
        display_name="智联招聘",
        sender_domains=("zhaopin.com",),
        subject_keywords=("智联招聘",),
        sort_order=20,
    ),
    _BuiltinPlatform(
        key="liepin",
        display_name="猎聘",
        sender_domains=("liepin.com",),
        subject_keywords=("猎聘",),
        sort_order=30,
    ),
    _BuiltinPlatform(
        key="51job",
        display_name="前程无忧",
        sender_domains=("51job.com",),
        subject_keywords=("前程无忧", "51job"),
        sort_order=40,
    ),
    _BuiltinPlatform(
        key="lagou",
        display_name="拉勾招聘",
        sender_domains=("lagou.com",),
        subject_keywords=("拉勾", "拉勾招聘"),
        sort_order=50,
    ),
    _BuiltinPlatform(
        key="linkedin",
        display_name="LinkedIn",
        sender_domains=("linkedin.com",),
        subject_keywords=("LinkedIn",),
        sort_order=60,
    ),
    _BuiltinPlatform(
        key="maimai",
        display_name="脉脉",
        sender_domains=("maimai.cn",),
        subject_keywords=("脉脉",),
        sort_order=70,
    ),
)

_DOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_RULE_VALUE_LENGTH = 255


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decode_header(value: object) -> str:
    """Decode a bounded header in memory without persisting its contents."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()[:2048]
    except (TypeError, ValueError):
        return raw[:2048]


def _header_values(message: Message, *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        # ``get_all`` preserves multiple sender-related fields while a normal
        # ``get`` would discard alternate RFC-compatible headers.
        for value in message.get_all(name, []):
            decoded = _decode_header(value)
            if decoded:
                values.append(decoded)
    return values


def _sender_addresses(message: Message) -> set[str]:
    addresses = {
        address.strip().casefold()
        for _, address in getaddresses(
            _header_values(message, "From", "Sender", "Reply-To")
        )
        if address and "@" in address
    }
    return {address for address in addresses if len(address) <= 320}


def _sender_domains(addresses: Iterable[str]) -> set[str]:
    domains: set[str] = set()
    for address in addresses:
        _, _, domain = address.rpartition("@")
        normalized = _normalize_domain(domain)
        if normalized:
            domains.add(normalized)
    return domains


def _normalize_domain(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().casefold().rstrip(".")
    if not normalized or len(normalized) > 253:
        return ""
    try:
        ascii_domain = normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return ascii_domain if _DOMAIN_PATTERN.fullmatch(ascii_domain) else ""


def _domain_matches(domain: str, configured_domain: str) -> bool:
    return domain == configured_domain or domain.endswith(f".{configured_domain}")


def _normalized_display_name(value: str) -> tuple[str, str]:
    display_name = " ".join(value.split()).strip()
    key = normalized_key(display_name)
    if not display_name or not key:
        raise SourceTagServiceError("source_tag_name_required")
    return display_name, key


def _rule_value(
    *,
    match_kind: str,
    match_value: str,
) -> tuple[str, str]:
    value = " ".join(match_value.split()).strip()
    if not value:
        raise SourceTagServiceError("source_tag_rule_value_required")
    if len(value) > _MAX_RULE_VALUE_LENGTH:
        raise SourceTagServiceError("source_tag_rule_value_too_long")
    if match_kind == "sender_domain":
        normalized = _normalize_domain(value)
        if not normalized:
            raise SourceTagServiceError("source_tag_rule_domain_invalid")
        return normalized, normalized
    if match_kind == "sender_address":
        parsed = getaddresses([value])
        address = parsed[0][1].strip().casefold() if len(parsed) == 1 else ""
        if not address or "@" not in address or len(address) > 320:
            raise SourceTagServiceError("source_tag_rule_sender_invalid")
        local_part, _, domain = address.rpartition("@")
        if not local_part or not _normalize_domain(domain):
            raise SourceTagServiceError("source_tag_rule_sender_invalid")
        return address, address
    if match_kind == "subject_keyword":
        key = normalized_key(value)
        if not key:
            raise SourceTagServiceError("source_tag_rule_value_required")
        return value, key
    raise SourceTagServiceError("source_tag_rule_match_kind_invalid")


def _source_tag_reference(tag: SourceTag) -> SourceTagReference:
    return SourceTagReference(
        source_tag_id=tag.id,
        display_name=tag.display_name,
    )


def _source_tag_response(tag: SourceTag) -> SourceTagResponse:
    return SourceTagResponse(
        source_tag_id=tag.id,
        display_name=tag.display_name,
        enabled=tag.enabled,
        is_system=bool(tag.system_key),
        sort_order=tag.sort_order,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
    )


def _rule_response(rule: MailboxSourceTagRule, tag: SourceTag) -> MailboxSourceTagRuleResponse:
    return MailboxSourceTagRuleResponse(
        rule_id=rule.id,
        mailbox_config_id=rule.mailbox_config_id,
        source_tag=_source_tag_reference(tag),
        match_kind=rule.match_kind,
        match_value=rule.match_value,
        priority=rule.priority,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _visible_source_tag(session: Session, *, source_tag_id: str) -> SourceTag:
    tag = session.scalar(select(SourceTag).where(SourceTag.id == source_tag_id))
    if tag is None:
        raise SourceTagServiceError("source_tag_not_found")
    return tag


def _visible_mailbox(session: Session, *, mailbox_config_id: str) -> MailboxConfig:
    mailbox = session.scalar(select(MailboxConfig).where(MailboxConfig.id == mailbox_config_id))
    if mailbox is None:
        raise SourceTagServiceError("mailbox_config_not_found")
    return mailbox


def list_source_tags(
    session: Session,
    *,
    include_disabled: bool = True,
) -> list[SourceTagResponse]:
    statement = select(SourceTag)
    if not include_disabled:
        statement = statement.where(SourceTag.enabled.is_(True))
    tags = session.scalars(
        statement.order_by(SourceTag.sort_order, SourceTag.name_key, SourceTag.id)
    ).all()
    return [_source_tag_response(tag) for tag in tags]


def source_tag_filter_options(session: Session) -> list[dict[str, str]]:
    """Return only tags already present on live resume projections.

    This prevents the initial-screen filter from displaying a newly configured
    rule that has not yet received any candidates.  Detail views still show
    historical, disabled tags through their snapshots.
    """

    rows = session.execute(
        select(SourceTag.id, SourceTag.display_name)
        .join(ResumeSourceTag, ResumeSourceTag.source_tag_id == SourceTag.id)
        .join(Resume, Resume.id == ResumeSourceTag.resume_id)
        .where(
            SourceTag.enabled.is_(True),
            # Candidate filtering only searches the current resume version;
            # do not offer an otherwise-empty platform from an archived
            # historical version.
            Resume.is_active.is_(True),
            Resume.extraction_status == "ready",
        )
        .group_by(SourceTag.id, SourceTag.display_name, SourceTag.sort_order, SourceTag.name_key)
        .order_by(SourceTag.sort_order, SourceTag.name_key, SourceTag.id)
    ).all()
    return [
        {"value": str(source_tag_id), "label": str(display_name)}
        for source_tag_id, display_name in rows
    ]


def create_source_tag(
    session: Session,
    *,
    payload: SourceTagCreate,
) -> SourceTagResponse:
    display_name, display_name_key = _normalized_display_name(payload.display_name)
    existing = session.scalar(
        select(SourceTag).where(SourceTag.name_key == display_name_key)
    )
    if existing is not None:
        raise SourceTagServiceError("source_tag_duplicate_display_name")
    tag = SourceTag(
        organization_id=organization_context_id(session),
        display_name=display_name,
        name_key=display_name_key,
        enabled=payload.enabled,
        sort_order=payload.sort_order,
    )
    try:
        with session.begin_nested():
            session.add(tag)
            session.flush()
    except IntegrityError as exc:
        raise SourceTagServiceError("source_tag_duplicate_display_name") from exc
    return _source_tag_response(tag)


def update_source_tag(
    session: Session,
    *,
    source_tag_id: str,
    payload: SourceTagPatch,
) -> SourceTagResponse:
    tag = _visible_source_tag(session, source_tag_id=source_tag_id)
    updates = payload.model_dump(exclude_unset=True)
    if "display_name" in updates:
        display_name, display_name_key = _normalized_display_name(str(updates["display_name"]))
        duplicate = session.scalar(
            select(SourceTag.id).where(
                SourceTag.name_key == display_name_key,
                SourceTag.id != tag.id,
            )
        )
        if duplicate is not None:
            raise SourceTagServiceError("source_tag_duplicate_display_name")
        tag.display_name = display_name
        tag.name_key = display_name_key
    if "enabled" in updates:
        tag.enabled = bool(updates["enabled"])
    if "sort_order" in updates:
        tag.sort_order = int(updates["sort_order"])
    session.flush()
    return _source_tag_response(tag)


def list_mailbox_source_tag_rules(
    session: Session,
    *,
    mailbox_config_id: str,
) -> list[MailboxSourceTagRuleResponse]:
    _visible_mailbox(session, mailbox_config_id=mailbox_config_id)
    rows = session.execute(
        select(MailboxSourceTagRule, SourceTag)
        .join(SourceTag, SourceTag.id == MailboxSourceTagRule.source_tag_id)
        .where(MailboxSourceTagRule.mailbox_config_id == mailbox_config_id)
        .order_by(
            MailboxSourceTagRule.priority,
            MailboxSourceTagRule.created_at,
            MailboxSourceTagRule.id,
        )
    ).all()
    return [_rule_response(rule, tag) for rule, tag in rows]


def _duplicate_rule_exists(
    session: Session,
    *,
    mailbox_config_id: str,
    source_tag_id: str,
    match_kind: str,
    match_value_key: str,
    excluding_rule_id: str | None = None,
) -> bool:
    statement = select(MailboxSourceTagRule.id).where(
        MailboxSourceTagRule.mailbox_config_id == mailbox_config_id,
        MailboxSourceTagRule.source_tag_id == source_tag_id,
        MailboxSourceTagRule.match_kind == match_kind,
        MailboxSourceTagRule.match_value_key == match_value_key,
    )
    if excluding_rule_id:
        statement = statement.where(MailboxSourceTagRule.id != excluding_rule_id)
    return session.scalar(statement) is not None


def create_mailbox_source_tag_rule(
    session: Session,
    *,
    mailbox_config_id: str,
    payload: MailboxSourceTagRuleCreate,
) -> MailboxSourceTagRuleResponse:
    mailbox = _visible_mailbox(session, mailbox_config_id=mailbox_config_id)
    tag = _visible_source_tag(session, source_tag_id=payload.source_tag_id)
    if not tag.enabled:
        raise SourceTagServiceError("source_tag_disabled")
    value, value_key = _rule_value(
        match_kind=payload.match_kind,
        match_value=payload.match_value,
    )
    existing = session.scalar(
        select(MailboxSourceTagRule).where(
            MailboxSourceTagRule.mailbox_config_id == mailbox.id,
            MailboxSourceTagRule.source_tag_id == tag.id,
            MailboxSourceTagRule.match_kind == payload.match_kind,
            MailboxSourceTagRule.match_value_key == value_key,
        )
    )
    if existing is not None:
        if existing.enabled:
            raise SourceTagServiceError("source_tag_rule_duplicate")
        # Rules that have historical audit references are deliberately
        # disabled instead of deleted. Re-adding the identical condition is
        # therefore an explicit re-enable, retaining the same durable rule
        # ID and its past assignment trail.
        existing.match_value = value
        existing.priority = payload.priority
        existing.enabled = payload.enabled
        session.flush()
        return _rule_response(existing, tag)
    rule = MailboxSourceTagRule(
        organization_id=organization_context_id(session),
        mailbox_config_id=mailbox.id,
        source_tag_id=tag.id,
        match_kind=payload.match_kind,
        match_value=value,
        match_value_key=value_key,
        priority=payload.priority,
        enabled=payload.enabled,
    )
    try:
        with session.begin_nested():
            session.add(rule)
            session.flush()
    except IntegrityError as exc:
        raise SourceTagServiceError("source_tag_rule_duplicate") from exc
    return _rule_response(rule, tag)


def _visible_rule(session: Session, *, rule_id: str) -> MailboxSourceTagRule:
    rule = session.scalar(select(MailboxSourceTagRule).where(MailboxSourceTagRule.id == rule_id))
    if rule is None:
        raise SourceTagServiceError("source_tag_rule_not_found")
    return rule


def update_mailbox_source_tag_rule(
    session: Session,
    *,
    mailbox_config_id: str,
    rule_id: str,
    payload: MailboxSourceTagRulePatch,
) -> MailboxSourceTagRuleResponse:
    _visible_mailbox(session, mailbox_config_id=mailbox_config_id)
    rule = _visible_rule(session, rule_id=rule_id)
    if rule.mailbox_config_id != mailbox_config_id:
        raise SourceTagServiceError("source_tag_rule_not_found")
    updates = payload.model_dump(exclude_unset=True)
    match_kind = str(updates.get("match_kind", rule.match_kind))
    match_value = str(updates.get("match_value", rule.match_value))
    value, value_key = _rule_value(match_kind=match_kind, match_value=match_value)
    source_tag_id = str(updates.get("source_tag_id", rule.source_tag_id))
    tag = _visible_source_tag(session, source_tag_id=source_tag_id)
    if not tag.enabled:
        raise SourceTagServiceError("source_tag_disabled")
    if _duplicate_rule_exists(
        session,
        mailbox_config_id=mailbox_config_id,
        source_tag_id=source_tag_id,
        match_kind=match_kind,
        match_value_key=value_key,
        excluding_rule_id=rule.id,
    ):
        raise SourceTagServiceError("source_tag_rule_duplicate")
    rule.source_tag_id = source_tag_id
    rule.match_kind = match_kind
    rule.match_value = value
    rule.match_value_key = value_key
    if "priority" in updates:
        rule.priority = int(updates["priority"])
    if "enabled" in updates:
        rule.enabled = bool(updates["enabled"])
    session.flush()
    return _rule_response(rule, tag)


def delete_mailbox_source_tag_rule(
    session: Session,
    *,
    mailbox_config_id: str,
    rule_id: str,
) -> None:
    _visible_mailbox(session, mailbox_config_id=mailbox_config_id)
    rule = _visible_rule(session, rule_id=rule_id)
    if rule.mailbox_config_id != mailbox_config_id:
        raise SourceTagServiceError("source_tag_rule_not_found")
    # Event assignments retain an enforced rule reference for auditability.
    # Treat a delete request as an immediate disable instead of removing a
    # rule that historical attachments may still rely on.
    rule.enabled = False


def _ensure_builtin_tag(
    session: Session,
    *,
    platform: _BuiltinPlatform,
) -> SourceTag | None:
    tag = session.scalar(select(SourceTag).where(SourceTag.system_key == platform.key))
    if tag is not None:
        return tag if tag.enabled else None
    display_name, display_name_key = _normalized_display_name(platform.display_name)
    tag = session.scalar(select(SourceTag).where(SourceTag.name_key == display_name_key))
    if tag is not None:
        # A recruiter may have created the familiar label before its first
        # matching email arrived.  Reuse it so filters and audit history do
        # not split into duplicate-looking platform chips.
        if tag.system_key is None:
            try:
                # Keep a concurrent first mailbox sync from poisoning the
                # outer import transaction. If another worker wins the system
                # key, use that canonical row instead.
                with session.begin_nested():
                    tag.system_key = platform.key
                    session.flush()
            except IntegrityError:
                canonical = session.scalar(
                    select(SourceTag).where(SourceTag.system_key == platform.key)
                )
                if canonical is None:
                    raise
                tag = canonical
        return tag if tag.enabled else None
    tag = SourceTag(
        organization_id=organization_context_id(session),
        display_name=display_name,
        name_key=display_name_key,
        system_key=platform.key,
        enabled=True,
        sort_order=platform.sort_order,
    )
    try:
        with session.begin_nested():
            session.add(tag)
            session.flush()
    except IntegrityError:
        tag = session.scalar(select(SourceTag).where(SourceTag.system_key == platform.key))
        if tag is None:
            raise
    return tag if tag.enabled else None


def _builtin_matches(
    *,
    platform: _BuiltinPlatform,
    sender_domains: set[str],
    subject: str,
) -> bool:
    if any(
        _domain_matches(domain, configured_domain)
        for domain in sender_domains
        for configured_domain in platform.sender_domains
    ):
        return True
    subject_key = normalized_key(subject)
    return any(
        normalized_key(keyword) in subject_key
        for keyword in platform.subject_keywords
        if normalized_key(keyword)
    )


def _custom_rule_matches(
    *,
    rule: MailboxSourceTagRule,
    sender_addresses: set[str],
    sender_domains: set[str],
    subject: str,
) -> bool:
    if rule.match_kind == "sender_domain":
        return any(_domain_matches(domain, rule.match_value_key) for domain in sender_domains)
    if rule.match_kind == "sender_address":
        return rule.match_value_key in sender_addresses
    if rule.match_kind == "subject_keyword":
        return bool(
            rule.match_value_key
            and rule.match_value_key in normalized_key(subject)
        )
    return False


def match_mailbox_source_tags(
    session: Session,
    *,
    config: MailboxConfig,
    message: Message,
) -> list[SourceTagMatch]:
    """Classify one parsed message without persisting its raw headers."""

    if config.organization_id != organization_context_id(session):
        raise SourceTagServiceError("mailbox_workspace_mismatch")
    sender_addresses = _sender_addresses(message)
    sender_domains = _sender_domains(sender_addresses)
    subject = _decode_header(message.get("Subject"))
    matches: dict[str, SourceTagMatch] = {}

    for platform in _BUILTIN_PLATFORMS:
        if not _builtin_matches(
            platform=platform,
            sender_domains=sender_domains,
            subject=subject,
        ):
            continue
        tag = _ensure_builtin_tag(session, platform=platform)
        if tag is not None:
            matches[tag.id] = SourceTagMatch(
                source_tag_id=tag.id,
                display_name_snapshot=tag.display_name,
                assignment_kind="builtin",
            )

    rules = session.scalars(
        select(MailboxSourceTagRule)
        .join(SourceTag, SourceTag.id == MailboxSourceTagRule.source_tag_id)
        .where(
            MailboxSourceTagRule.mailbox_config_id == config.id,
            MailboxSourceTagRule.enabled.is_(True),
            SourceTag.enabled.is_(True),
        )
        .order_by(MailboxSourceTagRule.priority, MailboxSourceTagRule.id)
    ).all()
    for rule in rules:
        if not _custom_rule_matches(
            rule=rule,
            sender_addresses=sender_addresses,
            sender_domains=sender_domains,
            subject=subject,
        ):
            continue
        tag = _visible_source_tag(session, source_tag_id=rule.source_tag_id)
        existing_match = matches.get(tag.id)
        # A custom rule is more useful audit detail than a built-in fallback
        # when both happen to lead to the same tag.  Rules are ordered by
        # priority ascending, so retain the first custom match: a smaller
        # priority value is the documented higher priority and must not be
        # overwritten by a later, lower-priority rule for the same tag.
        if existing_match is not None and existing_match.assignment_kind == "mailbox_rule":
            continue
        matches[tag.id] = SourceTagMatch(
            source_tag_id=tag.id,
            display_name_snapshot=tag.display_name,
            assignment_kind="mailbox_rule",
            matched_rule_id=rule.id,
        )
    return sorted(matches.values(), key=lambda match: (normalized_key(match.display_name_snapshot), match.source_tag_id))


def attach_source_tag_matches_to_import(
    session: Session,
    *,
    attachment_import: EmailAttachmentImport,
    matches: Iterable[SourceTagMatch],
) -> None:
    """Persist the immutable source-tag facts for one mail attachment."""

    expected_organization_id = organization_context_id(session)
    if attachment_import.organization_id != expected_organization_id:
        raise SourceTagServiceError("mailbox_workspace_mismatch")
    existing_tag_ids = set(
        session.scalars(
            select(EmailAttachmentImportTag.source_tag_id).where(
                EmailAttachmentImportTag.email_attachment_import_id == attachment_import.id
            )
        ).all()
    )
    for match in matches:
        if match.source_tag_id in existing_tag_ids:
            continue
        session.add(
            EmailAttachmentImportTag(
                organization_id=expected_organization_id,
                email_attachment_import_id=attachment_import.id,
                source_tag_id=match.source_tag_id,
                assignment_kind=match.assignment_kind,
                matched_rule_id=match.matched_rule_id,
                tag_name_snapshot=match.display_name_snapshot,
                assigned_at=_utcnow(),
            )
        )
    session.flush()


def sync_resume_source_tag_projection(
    session: Session,
    *,
    resume_id: str,
) -> None:
    """Rebuild one resume's compact tag projection from mail-event facts.

    Recalculation rather than incrementing makes forwarded duplicates and a
    retry completing concurrently idempotent.  It also means a stale worker
    can safely run this after a newer duplicate has already linked the same
    canonical resume.
    """

    resume = session.scalar(select(Resume).where(Resume.id == resume_id))
    if resume is None:
        raise SourceTagServiceError("resume_not_found")
    rows = session.execute(
        select(
            EmailAttachmentImportTag.source_tag_id,
            EmailAttachmentImportTag.tag_name_snapshot,
            EmailAttachmentImportTag.assigned_at,
            EmailAttachmentImportTag.email_attachment_import_id,
        )
        .join(
            EmailAttachmentImport,
            EmailAttachmentImport.id
            == EmailAttachmentImportTag.email_attachment_import_id,
        )
        .where(EmailAttachmentImport.resume_id == resume.id)
        .order_by(
            EmailAttachmentImportTag.source_tag_id,
            EmailAttachmentImportTag.assigned_at,
            EmailAttachmentImportTag.email_attachment_import_id,
        )
    ).all()
    grouped: dict[str, list[tuple[str, datetime, str]]] = defaultdict(list)
    for source_tag_id, snapshot, assigned_at, import_id in rows:
        if source_tag_id and snapshot and assigned_at and import_id:
            grouped[str(source_tag_id)].append(
                (str(snapshot), assigned_at, str(import_id))
            )
    for source_tag_id, occurrences in grouped.items():
        first_name, first_seen_at, first_import_id = occurrences[0]
        last_name, last_seen_at, last_import_id = occurrences[-1]
        projection = session.scalar(
            select(ResumeSourceTag).where(
                ResumeSourceTag.resume_id == resume.id,
                ResumeSourceTag.source_tag_id == source_tag_id,
            )
        )
        if projection is None:
            projection = ResumeSourceTag(
                organization_id=resume.organization_id,
                resume_id=resume.id,
                source_tag_id=source_tag_id,
                tag_name_snapshot=last_name or first_name,
                first_import_id=first_import_id,
                last_import_id=last_import_id,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                source_count=len(occurrences),
            )
            try:
                with session.begin_nested():
                    session.add(projection)
                    session.flush()
            except IntegrityError:
                projection = session.scalar(
                    select(ResumeSourceTag).where(
                        ResumeSourceTag.resume_id == resume.id,
                        ResumeSourceTag.source_tag_id == source_tag_id,
                    )
                )
                if projection is None:
                    raise
        projection.tag_name_snapshot = last_name or first_name
        projection.first_import_id = first_import_id
        projection.last_import_id = last_import_id
        projection.first_seen_at = first_seen_at
        projection.last_seen_at = last_seen_at
        projection.source_count = len(occurrences)
    session.flush()


def resume_source_tag_references(
    session: Session,
    *,
    resume_ids: Iterable[str],
) -> dict[str, list[SourceTagReference]]:
    """Return stable, snapshot-backed tag chips for a set of visible resumes."""

    normalized_ids = sorted({resume_id for resume_id in resume_ids if resume_id})
    if not normalized_ids:
        return {}
    rows = session.execute(
        select(
            ResumeSourceTag.resume_id,
            ResumeSourceTag.source_tag_id,
            ResumeSourceTag.tag_name_snapshot,
            ResumeSourceTag.last_seen_at,
        )
        .where(ResumeSourceTag.resume_id.in_(normalized_ids))
        .order_by(
            ResumeSourceTag.resume_id,
            ResumeSourceTag.last_seen_at.desc(),
            ResumeSourceTag.source_tag_id,
        )
    ).all()
    result: dict[str, list[SourceTagReference]] = defaultdict(list)
    for resume_id, source_tag_id, snapshot, _last_seen_at in rows:
        result[str(resume_id)].append(
            SourceTagReference(
                source_tag_id=str(source_tag_id),
                display_name=str(snapshot),
            )
        )
    return dict(result)


def mailbox_import_source_tag_references(
    session: Session,
    *,
    import_ids: Iterable[str],
) -> dict[str, list[SourceTagReference]]:
    normalized_ids = sorted({import_id for import_id in import_ids if import_id})
    if not normalized_ids:
        return {}
    rows = session.execute(
        select(
            EmailAttachmentImportTag.email_attachment_import_id,
            EmailAttachmentImportTag.source_tag_id,
            EmailAttachmentImportTag.tag_name_snapshot,
        )
        .where(EmailAttachmentImportTag.email_attachment_import_id.in_(normalized_ids))
        .order_by(
            EmailAttachmentImportTag.email_attachment_import_id,
            EmailAttachmentImportTag.assigned_at,
            EmailAttachmentImportTag.id,
        )
    ).all()
    result: dict[str, list[SourceTagReference]] = defaultdict(list)
    for import_id, source_tag_id, snapshot in rows:
        result[str(import_id)].append(
            SourceTagReference(
                source_tag_id=str(source_tag_id),
                display_name=str(snapshot),
            )
        )
    return dict(result)


def validate_source_tag_ids(
    session: Session,
    *,
    source_tag_ids: Iterable[str],
) -> set[str]:
    normalized_ids = {source_tag_id for source_tag_id in source_tag_ids if source_tag_id}
    if not normalized_ids:
        return set()
    found_ids = set(
        session.scalars(select(SourceTag.id).where(SourceTag.id.in_(sorted(normalized_ids)))).all()
    )
    if found_ids != normalized_ids:
        raise SourceTagServiceError("source_tag_not_found")
    return found_ids


__all__ = [
    "SourceTagMatch",
    "SourceTagServiceError",
    "attach_source_tag_matches_to_import",
    "create_mailbox_source_tag_rule",
    "create_source_tag",
    "delete_mailbox_source_tag_rule",
    "list_mailbox_source_tag_rules",
    "list_source_tags",
    "mailbox_import_source_tag_references",
    "match_mailbox_source_tags",
    "resume_source_tag_references",
    "source_tag_filter_options",
    "sync_resume_source_tag_projection",
    "update_mailbox_source_tag_rule",
    "update_source_tag",
    "validate_source_tag_ids",
]
