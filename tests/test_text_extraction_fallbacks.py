from __future__ import annotations

import pytest

from app.services import text_extraction
from app.services.tencent_ocr_provider import TencentOcrConfig, TencentOcrError


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    is_encrypted = False

    def __init__(self, text: str) -> None:
        self.pages = [_FakePage(text)]


def _ocr_config() -> TencentOcrConfig:
    return TencentOcrConfig(
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        region="ap-guangzhou",
        timeout_seconds=5,
    )


def test_sparse_native_text_is_replaced_by_tencent_ocr(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader("short"),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {},
    )
    calls: list[dict[str, object]] = []

    def fake_ocr(**kwargs: object) -> str:
        calls.append(kwargs)
        return "OCR recovered education skills project experience " * 20

    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", fake_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "text_ready"
    assert result.pages[0].text.startswith("OCR recovered")
    assert result.parsed_page_count == 1
    assert "tencent-ocr" in result.parser_version
    assert result.ocr_attempted_page_count == 1
    assert result.ocr_successful_page_count == 1
    assert result.ocr_selected_page_count == 1
    assert result.ocr_failed_page_count == 0
    assert len(calls) == 1
    assert calls[0]["page_no"] == 1


def test_late_text_limit_preserves_ocr_usage_counts(monkeypatch, tmp_path) -> None:
    """A rejected PDF must not make a real OCR request disappear from metrics."""

    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader("short"),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {},
    )
    monkeypatch.setattr(
        text_extraction,
        "extract_pdf_page_text",
        lambda **kwargs: "Recovered text " * 20,
    )

    with pytest.raises(text_extraction.PdfExtractionError) as raised:
        text_extraction.extract_pdf_text(
            tmp_path / "resume.pdf",
            min_text_chars_per_page=1,
            ocr_sparse_text_chars_per_page=100,
            tencent_ocr_config=_ocr_config(),
            max_text_chars=20,
        )

    error = raised.value
    assert str(error) == "document_text_limit_exceeded"
    assert error.source_page_count == 1
    assert error.ocr_attempted_page_count == 1
    assert error.ocr_successful_page_count == 1
    assert error.ocr_selected_page_count == 1
    assert error.ocr_failed_page_count == 0


def test_better_pymupdf_text_avoids_an_ocr_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader("short native text"),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {1: "PyMuPDF recovered text " * 30},
    )

    def should_not_call_ocr(**kwargs: object) -> str:
        raise AssertionError("OCR should not run when native fallback is sufficient")

    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", should_not_call_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "text_ready"
    assert result.pages[0].text.startswith("PyMuPDF recovered")
    assert "pymupdf" in result.parser_version
    assert "tencent-ocr" not in result.parser_version
    assert result.ocr_attempted_page_count == 0


def test_failed_ocr_does_not_silently_accept_sparse_native_text(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader("short"),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {},
    )

    def failed_ocr(**kwargs: object) -> str:
        raise TencentOcrError("tencent_ocr_request_failed")

    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", failed_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=10,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "needs_review"
    assert result.pages[0].text == "short"
    assert "page_1_tencent_ocr_failed" in result.quality_flags
    assert result.ocr_attempted_page_count == 1
    assert result.ocr_successful_page_count == 0
    assert result.ocr_selected_page_count == 0
    assert result.ocr_failed_page_count == 1


def test_unicode_suspect_pypdf_text_uses_pymupdf_before_ocr(
    monkeypatch,
    tmp_path,
) -> None:
    broken_pypdf_text = (
        "Candidate experience and project delivery " * 30
        + "\u2f64" * 40
    )
    recovered_pymupdf_text = "候选人项目经历与工作成果 " * 120
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(broken_pypdf_text),
    )
    pymupdf_calls: list[list[int]] = []

    def fake_pymupdf(path, pages):
        pymupdf_calls.append(pages)
        return {1: recovered_pymupdf_text}

    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        fake_pymupdf,
    )

    def should_not_call_ocr(**kwargs: object) -> str:
        raise AssertionError("OCR must be the final fallback after PyMuPDF recovery")

    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", should_not_call_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "text_ready"
    assert result.pages[0].text == recovered_pymupdf_text
    assert pymupdf_calls == [[1]]
    assert "pymupdf-unicode-fallback" in result.parser_version
    assert "tencent-ocr" not in result.parser_version
    assert "page_1_pymupdf_text_recovered" in result.quality_flags
    assert "page_1_source_text_unreliable" not in result.quality_flags


def test_normal_english_pypdf_text_keeps_fast_path(monkeypatch, tmp_path) -> None:
    normal_english_text = "Candidate experience, project delivery, and skills. " * 30
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(normal_english_text),
    )

    def should_not_call_pymupdf(path, pages):
        raise AssertionError("normal English text should not need a second parser")

    def should_not_call_ocr(**kwargs: object) -> str:
        raise AssertionError("normal English text should not need OCR")

    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        should_not_call_pymupdf,
    )
    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", should_not_call_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "text_ready"
    assert result.pages[0].text == normal_english_text.strip()
    assert result.parser_version.startswith("pypdf-")
    assert "pymupdf" not in result.parser_version
    assert "tencent-ocr" not in result.parser_version


def test_unrecovered_unicode_suspect_runs_ocr_as_last_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    broken_pypdf_text = "Candidate experience " * 50 + "\u2f64" * 40
    recovered_ocr_text = "OCR recovered candidate education and experience " * 30
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(broken_pypdf_text),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {1: "too short"},
    )
    calls: list[dict[str, object]] = []

    def fake_ocr(**kwargs: object) -> str:
        calls.append(kwargs)
        return recovered_ocr_text

    monkeypatch.setattr(text_extraction, "extract_pdf_page_text", fake_ocr)

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
        tencent_ocr_config=_ocr_config(),
    )

    assert result.status == "text_ready"
    assert result.pages[0].text == recovered_ocr_text
    assert len(calls) == 1
    assert "pymupdf" not in result.parser_version
    assert "tencent-ocr" in result.parser_version


def test_unrecovered_unicode_suspect_below_ten_percent_remains_eligible(
    monkeypatch,
    tmp_path,
) -> None:
    broken_pypdf_text = "Candidate experience " * 50 + "\u2f64" * 40
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(broken_pypdf_text),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {},
    )

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
    )

    assert result.status == "text_ready"
    assert result.pages[0].text == broken_pypdf_text
    assert "page_1_source_text_unreliable" not in result.quality_flags
    assert "pymupdf" not in result.parser_version


def test_unrecovered_unicode_damage_at_ten_percent_blocks_ai_conclusions(
    monkeypatch,
    tmp_path,
) -> None:
    broken_pypdf_text = "Candidate experience " * 20 + "\u2f64" * 100
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(broken_pypdf_text),
    )
    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        lambda path, pages: {},
    )

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
    )

    assert result.status == "needs_review"
    assert "page_1_source_text_unreliable" in result.quality_flags


def test_symbol_heavy_text_does_not_trigger_unicode_repair_or_failure(
    monkeypatch,
    tmp_path,
) -> None:
    text_with_layout_symbols = "Candidate experience and skills " * 30 + "★" * 90
    monkeypatch.setattr(
        text_extraction,
        "PdfReader",
        lambda path: _FakeReader(text_with_layout_symbols),
    )

    def should_not_call_pymupdf(path, pages):
        raise AssertionError("ordinary layout symbols must not alone trigger repair")

    monkeypatch.setattr(
        text_extraction,
        "_extract_pymupdf_page_texts",
        should_not_call_pymupdf,
    )

    result = text_extraction.extract_pdf_text(
        tmp_path / "resume.pdf",
        min_text_chars_per_page=20,
        ocr_sparse_text_chars_per_page=100,
    )

    assert result.status == "text_ready"
    assert "page_1_source_text_unreliable" not in result.quality_flags
