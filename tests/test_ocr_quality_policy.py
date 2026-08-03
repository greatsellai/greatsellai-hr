from __future__ import annotations

from pathlib import Path

from app.config import AppSettings
from app.services import text_extraction


def test_default_sparse_ocr_threshold_is_two_hundred_characters(tmp_path: Path) -> None:
    settings = AppSettings(
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "data" / "uploads",
        database_url="sqlite://",
    )

    assert settings.ocr_sparse_text_chars_per_page == 200


def test_low_unicode_signal_still_requests_repair_without_becoming_a_hard_failure() -> None:
    # Four private-use glyphs are enough to try a recovery on a short page,
    # but not enough to discard an otherwise readable resume from screening.
    quality = text_extraction._assess_text_quality("可读简历内容" * 30 + "\ue001" * 4)

    assert quality.unicode_suspect is True
    assert quality.source_text_unreliable is False


def test_ten_percent_damage_is_the_hard_source_failure_boundary() -> None:
    quality = text_extraction._assess_text_quality("A" * 90 + "\ufffd" * 10)

    assert quality.source_text_unreliable is True
    assert quality.replacement_character_ratio == 0.10
