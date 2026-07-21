from __future__ import annotations

import hashlib
import json
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.models import Resume, ResumeFactSnapshot
from app.services.deepseek_provider import FACT_SNAPSHOT_SCHEMA_VERSION


def _make_pdf_with_text(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _create_resume(client) -> str:
    candidate_response = client.post("/v1/candidates", json={"display_name": "Snapshot test"})
    assert candidate_response.status_code == 200, candidate_response.text
    candidate_id = candidate_response.json()["candidate_id"]
    source_text = (
        "Test University Computer Science Example Company Python Engineer "
        "Data Platform Project Designed ingestion pipeline Reduced report latency "
        "Python SQL project history "
    ) * 4
    upload_response = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": ("resume.pdf", _make_pdf_with_text(source_text), "application/pdf")},
    )
    assert upload_response.status_code == 200, upload_response.text
    assert upload_response.json()["extraction_status"] == "text_ready"
    return upload_response.json()["resume_id"]


def _facts(*, skills: list[str]) -> dict[str, object]:
    return {
        "facts": {
            "schema_version": "resume_facts.v1",
            "education": [
                {
                    "school_name_raw": "Test University",
                    "degree": "bachelor",
                    "major_raw": "Computer Science",
                    "start_month": "2018-09",
                    "end_month": "2022-06",
                    "evidence_block_ids": ["page-001"],
                }
            ],
            "experiences": [
                {
                    "experience_type": "project",
                    "experience_name_raw": "Data Platform Project",
                    "organization_name_raw": "Example Company",
                    "title_raw": "Python Engineer",
                    "start_month": "2022-07",
                    "end_month": "2024-06",
                    "evidence_block_ids": ["page-001"],
                    "classification_evidence_block_ids": [],
                    "detail_items": [
                        {
                            "detail_raw": "Designed ingestion pipeline",
                            "evidence_block_ids": ["page-001"],
                        },
                        {
                            "detail_raw": "Reduced report latency",
                            "evidence_block_ids": ["page-001"],
                        },
                    ],
                }
            ],
            "skills": [
                {"skill_display": skill, "evidence_block_ids": ["page-001"]}
                for skill in skills
            ],
        }
    }


def _snapshots_for_resume(client, resume_id: str) -> list[ResumeFactSnapshot]:
    database = client.app.state.database
    with database.session_factory() as session:
        return session.scalars(
            select(ResumeFactSnapshot)
            .where(ResumeFactSnapshot.resume_id == resume_id)
            .order_by(ResumeFactSnapshot.facts_version)
        ).all()


def test_saved_facts_create_append_only_canonical_snapshots(client) -> None:
    resume_id = _create_resume(client)

    first_save = client.put(f"/v1/resumes/{resume_id}/facts", json=_facts(skills=["Python"]))
    assert first_save.status_code == 200, first_save.text
    first_snapshot = _snapshots_for_resume(client, resume_id)[0]
    first_json = first_snapshot.canonical_facts_json
    first_hash = first_snapshot.facts_sha256

    canonical_payload = json.loads(first_json)
    assert first_snapshot.facts_version == 1
    assert first_snapshot.created_by == "single_admin"
    assert first_snapshot.source_block_ids == ["page-001"]
    assert canonical_payload["source_block_ids"] == ["page-001"]
    assert canonical_payload["schema_version"] == FACT_SNAPSHOT_SCHEMA_VERSION
    assert canonical_payload["facts_schema_version"] == "resume_facts.v2"
    assert canonical_payload["experiences"] == [
        {
            "classification_evidence_block_ids": [],
            "detail_items": [
                {
                    "detail_raw": "Designed ingestion pipeline",
                    "evidence_block_ids": ["page-001"],
                },
                {
                    "detail_raw": "Reduced report latency",
                    "evidence_block_ids": ["page-001"],
                },
            ],
            "leadership_context": None,
            "leadership_role": None,
            "award_level": None,
            "award_result_raw": None,
            "end_month": "2024-06",
            "evidence_block_ids": ["page-001"],
            "experience_name_key": "dataplatformproject",
            "experience_name_raw": "Data Platform Project",
            "experience_type": "project",
            "fact_id": "experience-001",
            "is_current": False,
            "organization_key": "examplecompany",
            "organization_name_raw": "Example Company",
            "start_month": "2022-07",
            "title_key": "pythonengineer",
            "title_raw": "Python Engineer",
        }
    ]
    assert canonical_payload["skills"] == [
        {
            "evidence_block_ids": ["page-001"],
            "fact_id": "skill-001",
            "skill_display": "Python",
            "skill_key": "python",
            "skill_category": None,
        }
    ]
    assert canonical_payload["language_credentials"] == []
    assert canonical_payload["scholarships"] == []
    assert first_json == json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first_hash == hashlib.sha256(first_json.encode("utf-8")).hexdigest()

    second_save = client.put(
        f"/v1/resumes/{resume_id}/facts",
        json=_facts(skills=["Python", "SQL"]),
    )
    assert second_save.status_code == 200, second_save.text
    snapshots = _snapshots_for_resume(client, resume_id)
    assert [snapshot.facts_version for snapshot in snapshots] == [1, 2]
    assert snapshots[0].canonical_facts_json == first_json
    assert snapshots[0].facts_sha256 == first_hash
    assert snapshots[1].facts_sha256 != first_hash


def test_completed_manual_review_creates_snapshot(client) -> None:
    resume_id = _create_resume(client)
    database = client.app.state.database
    with database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        assert resume is not None
        resume.extraction_status = "needs_review"
        resume.is_active = False
        session.commit()

    payload = _facts(skills=["Python"])
    payload["complete_review"] = True
    payload["review_note"] = "Verified the source text and completed the review."
    payload["is_985_211_override"] = False
    response = client.put(f"/v1/resumes/{resume_id}/facts", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["extraction_status"] == "ready"

    snapshots = _snapshots_for_resume(client, resume_id)
    assert len(snapshots) == 1
    assert snapshots[0].facts_version == 1
    assert snapshots[0].created_by == "single_admin"
