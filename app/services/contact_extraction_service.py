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
    r"(?<![0-9])(?P<phone>(?:\+?[ \t]*86[ \t.-]*)?1[3-9](?:[ \t.-]?[0-9]){9})(?![0-9])"
)
_LANDLINE_PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?P<phone>(?:\+?[ \t]*86[ \t.-]*)?0[0-9]{2,3}(?:[ \t.-]?[0-9]){7,8})(?![0-9])"
)
# A value with an explicit ``+`` or ``00`` international prefix is safe to
# recognize globally.  This intentionally covers the common formats found in
# resumes (for example ``+1 415 555 2671`` and ``0086 138-0013-8000``) without
# treating ordinary dates, employee IDs, or unlabelled digit strings as phone
# numbers.  The final validation happens in ``_normalize_phone``.
_INTERNATIONAL_PHONE_START_PATTERN = re.compile(r"(?<![0-9+])(?:\+|00)")
_PHONE_PATTERNS = (
    _MOBILE_PHONE_PATTERN,
    _LANDLINE_PHONE_PATTERN,
)
_INTERNATIONAL_PHONE_SEPARATOR_CHARS = frozenset(" \t().-")
_EXPLICIT_CONTACT_LINE_PATTERN = re.compile(
    r"(?im)^\s*(?:"
    r"联系方式|联系(?:电话|邮箱)|电子?邮箱|邮箱|邮件|"
    r"手机(?:号码)?|电话(?:号码)?|座机(?:号码)?|"
    r"e-?mail|email|mail|mobile|phone|tel(?:ephone)?"
    r")\s*[:：]"
)
_CONTACT_LABEL_AT_END_PATTERN = re.compile(
    r"(?i)(?P<label>(?<![A-Z0-9_-])(?:"
    r"联系方式|联系(?:电话|邮箱)|电子?邮箱|邮箱|邮件|"
    r"手机(?:号码)?|电话(?:号码)?|座机(?:号码)?|"
    r"e-?mail|email|mail|mobile|phone|tel(?:ephone)?"
    r")(?![A-Z0-9_-]))\s*[:：]?\s*$"
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
            for kind, value, _, _ in _contact_value_matches(segment):
                add(kind, value, block_id)

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

    This is deliberately broader than extraction: the screening and Agent
    search paths must not become a back door for a recruiter to query a phone
    number or email address, even when the value appears outside the first-page
    header or an explicitly labelled line.  Matches are removed rather than
    replaced with semantic markers: a query for ``email``, ``phone``, or a
    redaction-token word must not become an accidental contact-data search.
    """

    if not text:
        return text

    spans: list[tuple[int, int]] = []
    for _, _, start, end in _contact_value_matches(text):
        label_start = _contact_label_start(text, start)
        spans.append((label_start if label_start is not None else start, end))
    return _replace_spans_with_whitespace(text, spans)


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
    stripped = value.strip()
    digits = re.sub(r"\D", "", stripped)
    is_international = stripped.startswith("+") or digits.startswith("00")
    if is_international:
        international_digits = digits[2:] if digits.startswith("00") else digits
        if not re.fullmatch(r"[1-9][0-9]{7,14}", international_digits):
            return None
        if international_digits.startswith("86"):
            china_local = international_digits[2:]
            if re.fullmatch(r"1[3-9][0-9]{9}", china_local):
                return china_local
            if re.fullmatch(r"0[0-9]{9,11}", china_local):
                return china_local
        return f"+{international_digits}"
    if re.fullmatch(r"1[3-9][0-9]{9}", digits):
        return digits
    if re.fullmatch(r"0[0-9]{9,11}", digits):
        return digits
    return None


def _contact_value_matches(text: str) -> list[tuple[ContactKind, str, int, int]]:
    """Return non-overlapping, normalized contacts in document order."""

    candidates: list[tuple[ContactKind, str, int, int]] = []
    for match in _EMAIL_PATTERN.finditer(text):
        normalized = _normalize_email(match.group("email"))
        if normalized is not None:
            candidates.append(("email", normalized, match.start(), match.end()))
    for raw, start, end in _international_phone_matches(text):
        normalized = _normalize_phone(raw)
        if normalized is not None:
            candidates.append(("phone", normalized, start, end))
    for pattern in _PHONE_PATTERNS:
        for match in pattern.finditer(text):
            normalized = _normalize_phone(match.group("phone"))
            if normalized is not None:
                candidates.append(("phone", normalized, match.start(), match.end()))

    ordered = sorted(candidates, key=lambda item: (item[2], -(item[3] - item[2]), item[0]))
    selected: list[tuple[ContactKind, str, int, int]] = []
    for candidate in ordered:
        _, _, start, end = candidate
        if any(start < chosen_end and end > chosen_start for _, _, chosen_start, chosen_end in selected):
            continue
        selected.append(candidate)
    return selected


def _international_phone_matches(text: str) -> Iterable[tuple[str, int, int]]:
    """Yield bounded international phone candidates without crossing contacts.

    A regex that allows arbitrary whitespace can accidentally merge two phone
    values on a header line.  Scanning from the explicit country prefix lets us
    stop before a following ``+``/``00`` number while still accepting normal
    spaces, parentheses, dots, and hyphens inside one number.
    """

    for match in _INTERNATIONAL_PHONE_START_PATTERN.finditer(text):
        start = match.start()
        index = match.end()
        digit_count = 0
        last_digit_end: int | None = None
        while index < len(text):
            character = text[index]
            if "0" <= character <= "9":
                if digit_count >= 15:
                    break
                digit_count += 1
                last_digit_end = index + 1
                index += 1
                continue
            if character not in _INTERNATIONAL_PHONE_SEPARATOR_CHARS:
                break
            if character in " \t":
                next_index = index
                while (
                    next_index < len(text)
                    and text[next_index] in " \t"
                ):
                    next_index += 1
                if (
                    text.startswith("+", next_index)
                    or text.startswith("00", next_index)
                ):
                    break
            index += 1
        if last_digit_end is not None:
            yield text[start:last_digit_end], start, last_digit_end


def _contact_label_start(text: str, value_start: int) -> int | None:
    """Find a directly preceding contact label without deleting unrelated text."""

    line_start = text.rfind("\n", 0, value_start) + 1
    prefix = text[line_start:value_start]
    match = _CONTACT_LABEL_AT_END_PATTERN.search(prefix)
    if match is not None:
        return line_start + match.start("label")

    # Some extracted PDF text puts the label on one line and the value on the
    # next.  Remove that standalone label too, but never consume another line's
    # content merely because it happens to contain a contact-related word.
    if prefix.strip():
        return None
    previous_end = max(line_start - 1, 0)
    previous_start = text.rfind("\n", 0, previous_end) + 1
    previous_line = text[previous_start:previous_end]
    match = _CONTACT_LABEL_AT_END_PATTERN.fullmatch(previous_line)
    if match is not None:
        return previous_start + match.start("label")
    return None


def _replace_spans_with_whitespace(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Replace merged ranges with spaces, never searchable redaction tokens."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start < 0 or end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    if not merged:
        return text

    parts: list[str] = []
    offset = 0
    for start, end in merged:
        parts.append(text[offset:start])
        parts.append(" ")
        offset = end
    parts.append(text[offset:])
    return "".join(parts)
