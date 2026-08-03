from __future__ import annotations

from pathlib import Path

import pytest

from app.services import document_text_extraction as document_text
from app.services.document_text_extraction import DocumentExtractionError
from app.services.text_extraction import PdfExtractionError
from app.services.tencent_ocr_provider import TencentOcrConfig, TencentOcrError


def _config() -> TencentOcrConfig:
    return TencentOcrConfig(
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        region="ap-guangzhou",
        timeout_seconds=5,
    )


def _extract(path: Path, *, config: TencentOcrConfig | None):
    return document_text.extract_document_text(
        path,
        min_text_chars_per_page=1,
        ocr_sparse_text_chars_per_page=1,
        tencent_ocr_config=config,
    )


def test_png_and_jpg_use_tencent_ocr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_image_ocr(*, path: Path, config: TencentOcrConfig) -> str:
        calls.append({"path": path, "config": config})
        return "Candidate Python project experience"

    monkeypatch.setattr(document_text, "extract_image_text", fake_image_ocr)

    for suffix in (".png", ".jpg"):
        image_path = tmp_path / f"resume{suffix}"
        image_path.write_bytes(b"synthetic image")
        result = _extract(image_path, config=_config())
        assert result.parser_version == "tencent-ocr"
        assert result.raw_text.endswith("Candidate Python project experience")
        assert result.ocr_attempted_page_count == 1
        assert result.ocr_successful_page_count == 1
        assert result.ocr_selected_page_count == 1
        assert result.ocr_failed_page_count == 0

    assert [call["path"].suffix for call in calls] == [".png", ".jpg"]
    assert all(call["config"] == _config() for call in calls)


def test_image_requires_tencent_configuration(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "resume.png"
    image_path.write_bytes(b"synthetic image")

    with pytest.raises(DocumentExtractionError, match="tencent_ocr_not_configured"):
        _extract(image_path, config=None)


def test_image_provider_error_is_preserved_as_stable_document_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "resume.jpeg"
    image_path.write_bytes(b"synthetic image")

    def fail(*_args: object, **_kwargs: object) -> str:
        raise TencentOcrError("tencent_ocr_request_failed")

    monkeypatch.setattr(document_text, "extract_image_text", fail)

    with pytest.raises(DocumentExtractionError, match="tencent_ocr_request_failed"):
        _extract(image_path, config=_config())


def test_pdf_error_preserves_count_only_ocr_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The document boundary preserves counters but never parser text."""

    monkeypatch.setattr(
        document_text,
        "extract_pdf_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PdfExtractionError(
                "document_text_limit_exceeded",
                source_page_count=2,
                ocr_attempted_page_count=2,
                ocr_successful_page_count=1,
                ocr_selected_page_count=1,
                ocr_failed_page_count=1,
            )
        ),
    )

    with pytest.raises(DocumentExtractionError) as raised:
        _extract(tmp_path / "resume.pdf", config=_config())

    error = raised.value
    assert str(error) == "document_text_limit_exceeded"
    assert error.source_page_count == 2
    assert error.ocr_attempted_page_count == 2
    assert error.ocr_successful_page_count == 1
    assert error.ocr_selected_page_count == 1
    assert error.ocr_failed_page_count == 1
