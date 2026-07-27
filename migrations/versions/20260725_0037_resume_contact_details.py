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
from typing import Sequence, Union

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
_HEADER_TEXT_LIMIT = 2_000


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
            for pattern in (_MOBILE_PHONE_PATTERN, _LANDLINE_PHONE_PATTERN):
                for match in pattern.finditer(segment):
                    normalized = _normalize_phone(match.group(0))
                    if normalized is not None:
                        add("phone", normalized, block_id)
            for match in _EMAIL_PATTERN.finditer(segment):
                normalized = match.group("email").strip().casefold()
                if normalized and len(normalized) <= 254:
                    add("email", normalized, block_id)

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
        bind.execute(
            resumes.update()
            .where(resumes.c.id == resume_id)
            .values(contact_details=_contact_storage_values(blocks))
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
