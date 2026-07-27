"""Local, source-grounded candidate contact extraction.

Candidate contact details are useful to a recruiter, but they are not hiring
facts. This module deliberately stays outside every model-provider payload:
it extracts only explicit phone/email strings from resume source blocks already
persisted by the document worker.

The returned values are versioned with the resume and retain source block IDs.
They are for the protected resume-detail view and a candidate-owned data export
only, never search, scoring, JD matching, summaries, or recruiting-agent
context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal


ContactKind = Literal["email", "phone"]


@dataclass(frozen=True)
class ContactSourceBlock:
    """Minimal source-block shape used by the extractor and migration."""

    block_id: str
    page_no: int
    text: str


@dataclass(frozen=True)
class ExtractedResumeContact:
    kind: ContactKind
    value: str
    evidence_block_ids: tuple[str, ...]

    def as_storage_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "value": self.value,
            "evidence_block_ids": list(self.evidence_block_ids),
        }


# This intentionally recognizes ordinary resume layouts rather than every
# RFC-valid email or every digit sequence. A conservative false negative is
# safer than showing unrelated personal data to a recruiter.
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"(?P<email>[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)"
    r"(?![A-Za-z0-9._%+-])",
    re.IGNORECASE,
)
_MOBILE_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?\s*86[\s-]*)?1[3-9](?:[\s-]?\d){9}(?!\d)"
)
_LANDLINE_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?\s*86[\s-]*)?0\d{2,3}(?:[\s-]?\d){7,8}(?!\d)"
)
_EXPLICIT_CONTACT_LINE_PATTERN = re.compile(
    r"(?im)^\s*(?:"
    r"联系方式|联系(?:电话|邮箱)|电子?邮箱|邮箱|邮件|"
    r"手机(?:号码)?|电话(?:号码)?|座机(?:号码)?|"
    r"e-?mail|email|mail|mobile|phone|tel(?:ephone)?"
    r")\s*[:：]"
)

# Most resumes put contact details in the first-page header. Bounding the
# unlabeled region prevents a later, unrelated address from becoming a contact.
_HEADER_TEXT_LIMIT = 2_000


def extract_resume_contacts(
    source_blocks: Iterable[ContactSourceBlock],
) -> list[ExtractedResumeContact]:
    """Extract deduplicated contacts with source-block provenance.

    The first page may contain unlabeled header contacts. On later pages,
    values must appear on an explicit contact-label line. Values are never
    inferred from file names, mail metadata, names, or model output.
    """

    contact_evidence: dict[tuple[ContactKind, str], list[str]] = {}
    contact_order: list[tuple[ContactKind, str]] = []

    def add(kind: ContactKind, value: str, block_id: str) -> None:
        key = (kind, value)
        evidence = contact_evidence.get(key)
        if evidence is None:
            contact_evidence[key] = [block_id]
            contact_order.append(key)
        elif block_id not in evidence:
            evidence.append(block_id)

    ordered_blocks = sorted(
        source_blocks,
        key=lambda block: (int(block.page_no), str(block.block_id)),
    )
    for block in ordered_blocks:
        block_id = str(block.block_id).strip()
        text = str(block.text or "")
        if not block_id or not text:
            continue
        segments: list[str] = []
        if int(block.page_no) == 1:
            segments.append(text[:_HEADER_TEXT_LIMIT])
        segments.extend(_explicit_contact_segments(text))
        for segment in segments:
            for match in _MOBILE_PHONE_PATTERN.finditer(segment):
                normalized = _normalize_phone(match.group(0))
                if normalized is not None:
                    add("phone", normalized, block_id)
            for match in _LANDLINE_PHONE_PATTERN.finditer(segment):
                normalized = _normalize_phone(match.group(0))
                if normalized is not None:
                    add("phone", normalized, block_id)
            for match in _EMAIL_PATTERN.finditer(segment):
                normalized = _normalize_email(match.group("email"))
                if normalized is not None:
                    add("email", normalized, block_id)

    return [
        ExtractedResumeContact(
            kind=kind,
            value=value,
            evidence_block_ids=tuple(contact_evidence[(kind, value)]),
        )
        for kind, value in contact_order
    ]


def contact_storage_values(
    source_blocks: Iterable[ContactSourceBlock],
) -> list[dict[str, object]]:
    """Return JSON-safe values for ``Resume.contact_details``."""

    return [item.as_storage_value() for item in extract_resume_contacts(source_blocks)]


def redact_contact_values(text: str) -> str:
    """Remove phone and email values before any non-contact consumer reads text.

    This is deliberately broader than extraction: the screening and Agent search
    paths must not become a back door for a recruiter to query a phone number
    or email address, even when the value appears outside the first-page header
    or an explicitly labelled line.  It returns text with stable placeholders
    so normal keyword matching and evidence block selection remain safe.
    """

    redacted = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    redacted = _MOBILE_PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    return _LANDLINE_PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)


def _explicit_contact_segments(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if _EXPLICIT_CONTACT_LINE_PATTERN.search(line)
    ]


def _normalize_email(value: str) -> str | None:
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 254:
        return None
    return normalized


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86"):
        digits = digits[2:]
    if re.fullmatch(r"1[3-9]\d{9}", digits):
        return digits
    if re.fullmatch(r"0\d{9,11}", digits):
        return digits
    return None
