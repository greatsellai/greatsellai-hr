"""Persist source-grounded resume contact details.

Revision ID: 20260725_0037
Revises: 20260724_0036
Create Date: 2026-07-25 10:00:00

The data migration reads only already persisted resume source blocks and uses a
version-fixed copy of the conservative local extractor rules. It never calls an
AI provider and never writes contacts into a fact snapshot.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260725_0037"
down_revision: Union[str, Sequence[str], None] = "20260724_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Alembic revisions must stay runnable after runtime services evolve. Keep a
# small, version-fixed copy of the contact rules instead of importing app code.
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
_HEADER_TEXT_LIMIT = 2_000


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


def _contact_value_matches(text: str) -> list[tuple[str, str, int, int]]:
    candidates: list[tuple[str, str, int, int]] = []
    for match in _EMAIL_PATTERN.finditer(text):
        normalized = match.group("email").strip().casefold()
        if normalized and len(normalized) <= 254:
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
    selected: list[tuple[str, str, int, int]] = []
    for candidate in ordered:
        _, _, start, end = candidate
        if any(start < chosen_end and end > chosen_start for _, _, chosen_start, chosen_end in selected):
            continue
        selected.append(candidate)
    return selected


def _international_phone_matches(text: str) -> Iterable[tuple[str, int, int]]:
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


def _contact_storage_values(
    blocks: Sequence[tuple[str, int, str]],
) -> list[dict[str, object]]:
    evidence_by_contact: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []

    def add(kind: str, value: str, block_id: str) -> None:
        key = (kind, value)
        evidence = evidence_by_contact.get(key)
        if evidence is None:
            evidence_by_contact[key] = [block_id]
            order.append(key)
        elif block_id not in evidence:
            evidence.append(block_id)

    for block_id, page_no, text in sorted(blocks, key=lambda block: (block[1], block[0])):
        segments = [text[:_HEADER_TEXT_LIMIT]] if page_no == 1 else []
        segments.extend(
            line
            for line in text.splitlines()
            if _EXPLICIT_CONTACT_LINE_PATTERN.search(line)
        )
        for segment in segments:
            for kind, value, _, _ in _contact_value_matches(segment):
                add(kind, value, block_id)

    return [
        {
            "kind": kind,
            "value": value,
            "evidence_block_ids": evidence_by_contact[(kind, value)],
        }
        for kind, value in order
    ]


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column(
            "contact_details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )

    bind = op.get_bind()
    resumes = sa.table(
        "resumes",
        sa.column("id", sa.String()),
        sa.column("contact_details", sa.JSON()),
    )
    source_blocks = sa.table(
        "resume_source_blocks",
        sa.column("resume_id", sa.String()),
        sa.column("block_id", sa.String()),
        sa.column("page_no", sa.Integer()),
        sa.column("text", sa.Text()),
    )
    rows = bind.execute(
        sa.select(
            source_blocks.c.resume_id,
            source_blocks.c.block_id,
            source_blocks.c.page_no,
            source_blocks.c.text,
        ).order_by(
            source_blocks.c.resume_id,
            source_blocks.c.page_no,
            source_blocks.c.block_id,
        )
    )
    current_resume_id: str | None = None
    current_blocks: list[tuple[str, int, str]] = []

    def persist_contacts(
        resume_id: str,
        blocks: Sequence[tuple[str, int, str]],
    ) -> None:
        contacts = _contact_storage_values(blocks)
        # New columns default to ``[]``. Avoid one write lock per unrelated
        # historical resume during this online data backfill.
        if not contacts:
            return
        bind.execute(
            resumes.update()
            .where(resumes.c.id == resume_id)
            .values(contact_details=contacts)
        )

    for resume_id, block_id, page_no, text in rows:
        if not isinstance(resume_id, str) or not resume_id:
            continue
        if current_resume_id is None:
            current_resume_id = resume_id
        elif resume_id != current_resume_id:
            persist_contacts(current_resume_id, current_blocks)
            current_resume_id = resume_id
            current_blocks = []
        if not isinstance(block_id, str) or not block_id:
            continue
        if not isinstance(page_no, int) or not isinstance(text, str):
            continue
        current_blocks.append((block_id, page_no, text))
    if current_resume_id is not None:
        persist_contacts(current_resume_id, current_blocks)


def downgrade() -> None:
    op.drop_column("resumes", "contact_details")
