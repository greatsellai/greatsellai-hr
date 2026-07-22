from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.models import Resume, ResumeSourceBlock
from app.services import document_extraction_job_service
from app.services.institution_service import load_registry
from app.services.text_extraction import ExtractedPage, PdfExtractionResult


def make_pdf_with_text(text: str) -> bytes:
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


def make_blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def create_candidate(client) -> str:
    response = client.post("/v1/candidates", json={"display_name": "测试候选人"})
    assert response.status_code == 200, response.text
    return response.json()["candidate_id"]


def upload_text_resume(client, candidate_id: str) -> str:
    content = make_pdf_with_text(
        "Python Developer internship Example Company SQL FastAPI " * 4
    )
    response = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": ("resume.pdf", content, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["extraction_status"] == "queued"
    assert document_extraction_job_service.run_document_extraction_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="resume-flow-document-worker",
    )
    detail = client.get(f"/v1/resumes/{payload['resume_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["extraction_status"] == "text_ready"
    assert detail.json()["source_page_count"] == 1
    assert detail.json()["parsed_page_count"] == 1
    return payload["resume_id"]


def test_upload_strips_database_unsafe_nulls_from_extracted_text(
    client,
    monkeypatch,
) -> None:
    extracted = PdfExtractionResult(
        source_page_count=1,
        parsed_page_count=1,
        pages=[
            ExtractedPage(
                page_no=1,
                text="Python\x00 FastAPI engineer",
                non_whitespace_chars=23,
            )
        ],
        raw_text="--- PAGE 1 ---\nPython\x00 FastAPI engineer",
        quality_flags=[],
        parser_version="test-parser",
    )
    monkeypatch.setattr(
        "app.services.document_extraction_job_service.extract_document_text",
        lambda *args, **kwargs: extracted,
    )

    response = client.post(
        "/v1/resumes/upload",
        files={
            "file": (
                "resume.pdf",
                make_pdf_with_text("Python FastAPI engineer"),
                "application/pdf",
            )
        },
    )

    assert response.status_code == 200, response.text
    resume_id = response.json()["resume_id"]
    assert document_extraction_job_service.run_document_extraction_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="resume-flow-document-worker",
    )
    with client.app.state.database.session_factory() as session:
        resume = session.get(Resume, resume_id)
        source_block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert resume is not None
        assert source_block is not None
        assert "\x00" not in (resume.raw_text or "")
        assert "\x00" not in source_block.text


def replace_page_evidence(client, resume_id: str, text: str) -> None:
    database = client.app.state.database
    with database.session_factory() as session:
        block = session.scalar(
            select(ResumeSourceBlock).where(
                ResumeSourceBlock.resume_id == resume_id,
                ResumeSourceBlock.block_id == "page-001",
            )
        )
        assert block is not None
        block.text = text
        session.commit()


def ready_facts() -> dict:
    return {
        "facts": {
            "schema_version": "resume_facts.v1",
            "education": [
                {
                    "school_name_raw": "清华大学",
                    "degree": "bachelor",
                    "major_raw": "计算机科学",
                    "start_month": "2018-09",
                    "end_month": "2022-06",
                    "evidence_block_ids": ["page-001"],
                }
            ],
            "experiences": [
                {
                    "experience_type": "employment",
                    "organization_name_raw": "示例科技公司",
                    "title_raw": "Python工程师",
                    "start_month": "2022-07",
                    "end_month": "2024-06",
                    "evidence_block_ids": ["page-001"],
                    "classification_evidence_block_ids": ["page-001"],
                }
            ],
            "skills": [
                {"skill_display": "Python", "evidence_block_ids": ["page-001"]},
                {"skill_display": "SQL", "evidence_block_ids": ["page-001"]},
            ],
        }
    }


def test_registry_contains_official_985_211_counts() -> None:
    registry = load_registry()
    assert len(registry.institutions) == 112
    assert sum(item.roster_id.startswith("cn-985-") for item in registry.institutions) == 39
    assert any(
        item.canonical_name == "中国地质大学"
        and "中国地质大学（武汉）" in item.aliases
        for item in registry.institutions
    )


def test_upload_then_save_facts_and_filter(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "教育经历 清华大学 计算机科学 本科。"
        "工作经历 示例科技公司 Python工程师 2022-07 至 2024-06。"
        "技能 Python SQL。",
    )

    facts_response = client.put(f"/v1/resumes/{resume_id}/facts", json=ready_facts())
    assert facts_response.status_code == 200, facts_response.text
    detail = facts_response.json()
    assert detail["extraction_status"] == "ready"
    assert detail["is_active"] is True
    assert detail["is_985_211"] is True
    assert detail["employment_months"] == 24

    search_response = client.post(
        "/v1/candidates/search",
        json={
            "is_985_211": True,
            "min_employment_months": 24,
            "education_any_of": [
                {
                    "degree_in": ["bachelor"],
                    "school_name_contains": ["清华大学"],
                    "major_contains": ["计算机"],
                }
            ],
            "experience_any_of": [
                {
                    "experience_types": ["employment"],
                    "organization_name_contains": ["示例科技"],
                    "title_contains": ["Python工程师"],
                }
            ],
            "skills_all_of": ["Python"],
            "skills_any_of": ["SQL"],
        },
    )
    assert search_response.status_code == 200, search_response.text
    result = search_response.json()["items"]
    assert len(result) == 1
    assert result[0]["candidate_id"] == candidate_id
    assert result[0]["is_985_211"] is True


def test_project_context_cannot_be_saved_as_employment_without_manual_review(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "项目经历 示例科技公司 Python工程师 2022-07 至 2024-06。技能 Python。",
    )
    payload = ready_facts()
    payload["facts"]["education"] = []
    response = client.put(f"/v1/resumes/{resume_id}/facts", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == "work_context_requires_manual_review_note"


def test_explicit_english_work_experience_is_accepted_without_override_note(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    replace_page_evidence(
        client,
        resume_id,
        "Professional Experience Acme Python Engineer 2022-07 to 2024-06. "
        "Skills Python SQL.",
    )
    payload = ready_facts()
    payload["facts"]["education"] = []
    payload["facts"]["experiences"][0]["organization_name_raw"] = "Acme"
    payload["facts"]["experiences"][0]["title_raw"] = "Python Engineer"
    response = client.put(f"/v1/resumes/{resume_id}/facts", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["extraction_status"] == "needs_review"
    assert response.json()["employment_months"] == 24


def test_blank_pdf_requires_review_and_is_not_searchable(client) -> None:
    candidate_id = create_candidate(client)
    response = client.post(
        f"/v1/candidates/{candidate_id}/resumes",
        files={"file": ("scan.pdf", make_blank_pdf(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    assert document_extraction_job_service.run_document_extraction_worker_once(
        client.app.state.database,
        settings=client.app.state.settings,
        worker_id="resume-flow-document-worker",
    )
    payload = client.get(f"/v1/resumes/{response.json()['resume_id']}").json()
    assert payload["extraction_status"] == "needs_review"
    assert "parsed_page_count_mismatch" in payload["quality_flags"]

    search_response = client.post("/v1/candidates/search", json={"limit": 10})
    assert search_response.status_code == 200
    assert search_response.json()["items"] == []


def test_ai_extraction_without_server_side_key_is_durably_marked_unavailable(client) -> None:
    candidate_id = create_candidate(client)
    resume_id = upload_text_resume(client, candidate_id)
    initial = client.get(f"/v1/resumes/{resume_id}")
    assert initial.status_code == 200, initial.text
    assert initial.json()["ai_extraction_status"] == "unavailable"
    assert initial.json()["ai_extraction_error"] == "deepseek_api_key_not_configured"

    response = client.post(f"/v1/resumes/{resume_id}/extract-facts")
    assert response.status_code == 200, response.text
    assert response.json()["ai_extraction_status"] == "unavailable"
    assert response.json()["ai_extraction_error"] == "deepseek_api_key_not_configured"
