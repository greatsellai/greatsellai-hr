from __future__ import annotations

from app.services.contact_extraction_service import (
    ContactSourceBlock,
    extract_resume_contacts,
    redact_contact_values,
)
from app.services.normalization import normalized_key


def test_extracts_and_deduplicates_header_and_labeled_resume_contacts() -> None:
    contacts = extract_resume_contacts(
        [
            ContactSourceBlock(
                block_id="page-001",
                page_no=1,
                text=(
                    "Candidate\n"
                    "138 0000 0000 | Candidate.Name@Example.Test\n"
                    "Python / FastAPI"
                ),
            ),
            ContactSourceBlock(
                block_id="page-002",
                page_no=2,
                text=(
                    "Email: candidate.name@example.test\n"
                    "Phone: +86 138-0000-0000\n"
                    "Tel: 010-12345678"
                ),
            ),
        ]
    )

    assert [(item.kind, item.value, item.evidence_block_ids) for item in contacts] == [
        ("phone", "13800000000", ("page-001", "page-002")),
        ("email", "candidate.name@example.test", ("page-001", "page-002")),
        ("phone", "01012345678", ("page-002",)),
    ]


def test_later_page_requires_an_explicit_contact_label() -> None:
    contacts = extract_resume_contacts(
        [
            ContactSourceBlock(
                block_id="page-001",
                page_no=1,
                text="Candidate\nPython / SQL / analytics",
            ),
            ContactSourceBlock(
                block_id="page-002",
                page_no=2,
                text=(
                    "Project contact vendor@example.test\n"
                    "Project number 13800000000\n"
                    "Email: candidate@example.test"
                ),
            ),
        ]
    )

    assert [(item.kind, item.value) for item in contacts] == [
        ("email", "candidate@example.test"),
    ]


def test_extracts_international_and_0086_phone_formats() -> None:
    contacts = extract_resume_contacts(
        [
            ContactSourceBlock(
                block_id="page-001",
                page_no=1,
                text=(
                    "Candidate\n"
                    "Phone: +1 415 555 2671 | Mobile: 0086 138-0013-8000\n"
                    "Python / FastAPI"
                ),
            ),
            ContactSourceBlock(
                block_id="page-002",
                page_no=2,
                text="Tel: +44 (20) 7946-0958",
            ),
        ]
    )

    assert [(item.kind, item.value, item.evidence_block_ids) for item in contacts] == [
        ("phone", "+14155552671", ("page-001",)),
        ("phone", "13800138000", ("page-001",)),
        ("phone", "+442079460958", ("page-002",)),
    ]


def test_ignores_malformed_or_non_phone_values() -> None:
    contacts = extract_resume_contacts(
        [
            ContactSourceBlock(
                block_id="page-001",
                page_no=1,
                text=(
                    "Candidate\n"
                    "mail candidate@example\n"
                    "date 2026-07-25\n"
                    "employee number 12345678901"
                ),
            )
        ]
    )

    assert contacts == []


def test_redacts_contact_values_from_all_source_text_for_non_contact_consumers() -> None:
    redacted = redact_contact_values(
        "Phone: 138 0000 0000, Tel: 010-12345678, "
        "Email: Candidate@Example.Test, Mobile: +1 415 555 2671, "
        "Callback: 0086 138-0013-8000, Skills: Python"
    )

    assert "138 0000 0000" not in redacted
    assert "010-12345678" not in redacted
    assert "Candidate@Example.Test" not in redacted
    assert "+1 415 555 2671" not in redacted
    assert "0086 138-0013-8000" not in redacted
    assert "Python" in redacted
    normalized = normalized_key(redacted)
    assert all(
        normalized_key(keyword) not in normalized
        for keyword in ("email", "phone", "tel", "mobile", "redacted")
    )
