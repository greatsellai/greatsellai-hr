from __future__ import annotations

import importlib.metadata
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.services.tencent_ocr_provider import (
    TencentOcrConfig,
    TencentOcrError,
    extract_pdf_page_text,
)


NON_WHITESPACE = re.compile(r"\S")
_LATIN1_MOJIBAKE_PAIR = re.compile(r"[\u00c2-\u00f4][\u0080-\u00bf]")
_CJK_RADICAL_RANGES = (
    (0x2E80, 0x2EFF),  # CJK Radicals Supplement
    (0x2F00, 0x2FD5),  # Kangxi Radicals
    (0x2FF0, 0x2FFF),  # Ideographic Description Characters
    (0x31C0, 0x31EF),  # CJK Strokes
)
_NON_BLOCKING_QUALITY_FLAG_SUFFIXES = ("_pymupdf_text_recovered",)
# A page is not blocked from the hiring workflow merely because a handful of
# glyphs could not be recovered.  The repair path is deliberately sensitive;
# the final evidence gate is deliberately stricter.
_HARD_SOURCE_TEXT_DAMAGE_RATIO = 0.10


class PdfExtractionError(RuntimeError):
    """A stable parser error that can retain content-free OCR usage counts.

    Most parser errors occur before an OCR request.  A text-size limit can be
    reached only after one or more page recoveries, however, so dropping the
    counts there would make the operational OCR rate under-report real calls.
    These attributes intentionally contain counts only, never page text or
    provider payloads.
    """

    def __init__(
        self,
        error_code: str,
        *,
        source_page_count: int = 0,
        ocr_attempted_page_count: int = 0,
        ocr_successful_page_count: int = 0,
        ocr_selected_page_count: int = 0,
        ocr_failed_page_count: int = 0,
    ) -> None:
        super().__init__(error_code)
        self.source_page_count = source_page_count
        self.ocr_attempted_page_count = ocr_attempted_page_count
        self.ocr_successful_page_count = ocr_successful_page_count
        self.ocr_selected_page_count = ocr_selected_page_count
        self.ocr_failed_page_count = ocr_failed_page_count


@dataclass(frozen=True)
class ExtractedPage:
    page_no: int
    text: str
    non_whitespace_chars: int


@dataclass(frozen=True)
class PdfExtractionResult:
    source_page_count: int
    parsed_page_count: int
    pages: list[ExtractedPage]
    raw_text: str
    quality_flags: list[str]
    parser_version: str
    # These are the counters for the *latest* normalization attempt.  They
    # make runtime reporting possible without storing page images, OCR output,
    # filenames or any other candidate content in platform diagnostics.
    ocr_attempted_page_count: int = 0
    ocr_successful_page_count: int = 0
    ocr_selected_page_count: int = 0
    ocr_failed_page_count: int = 0

    @property
    def status(self) -> str:
        return (
            "text_ready"
            if all(
                flag.endswith(_NON_BLOCKING_QUALITY_FLAG_SUFFIXES)
                for flag in self.quality_flags
            )
            else "needs_review"
        )


@dataclass(frozen=True)
class _TextQuality:
    """Small, explainable signal set for choosing a native PDF extractor.

    A page can be long but unusable when a PDF font's ToUnicode map is wrong.
    In that case pypdf commonly returns Kangxi/CJK radical glyphs, private-use
    code points, or replacement characters instead of the visible text.
    """

    non_whitespace_chars: int
    textual_chars: int
    replacement_chars: int
    private_use_chars: int
    control_chars: int
    unassigned_chars: int
    cjk_radical_chars: int
    latin1_mojibake_pairs: int
    damaged_char_count: int

    @property
    def unicode_damage(self) -> int:
        """Weighted count used only to compare two extractions of one page."""

        return (
            self.replacement_chars * 8
            + self.private_use_chars * 6
            + self.control_chars * 8
            + self.unassigned_chars * 8
            + self.cjk_radical_chars * 3
            + self.latin1_mojibake_pairs * 3
        )

    @property
    def score(self) -> int:
        """Higher is more likely to be text a recruiter can actually read."""

        return (
            self.non_whitespace_chars
            + self.textual_chars
            - self.unicode_damage * 4
        )

    @property
    def unicode_suspect(self) -> bool:
        """Whether this page merits a second native extractor attempt.

        The thresholds intentionally require a pattern, rather than reacting to
        one emoji or an isolated replacement character. That keeps ordinary
        English PDFs on the pypdf fast path.
        """

        non_whitespace = max(self.non_whitespace_chars, 1)
        return any(
            (
                self.replacement_chars > max(1, math.ceil(non_whitespace * 0.01)),
                self.private_use_chars >= max(3, math.ceil(non_whitespace * 0.01)),
                self.control_chars >= 2,
                self.unassigned_chars >= 2,
                self.cjk_radical_chars >= max(4, math.ceil(non_whitespace * 0.005)),
                self.latin1_mojibake_pairs >= max(3, math.ceil(non_whitespace * 0.01)),
            )
        )

    @property
    def source_text_unreliable(self) -> bool:
        """Whether unrecovered text is too damaged to support AI conclusions.

        A low signal remains useful for asking PyMuPDF/Tencent OCR to repair
        the page.  It becomes a user-visible extraction failure only when at
        least ten percent of the readable payload is made up of known broken
        Unicode glyphs.  Ordinary layout symbols are intentionally excluded.
        """

        return (
            self.non_whitespace_chars > 0
            and self.damaged_char_count / self.non_whitespace_chars
            >= _HARD_SOURCE_TEXT_DAMAGE_RATIO
        )

    @property
    def replacement_character_ratio(self) -> float:
        if not self.non_whitespace_chars:
            return 0.0
        return self.replacement_chars / self.non_whitespace_chars


def extract_pdf_text(
    path: Path,
    *,
    min_text_chars_per_page: int,
    ocr_sparse_text_chars_per_page: int = 200,
    tencent_ocr_config: TencentOcrConfig | None = None,
    max_pages: int | None = None,
    max_text_chars: int | None = None,
) -> PdfExtractionResult:
    try:
        reader = PdfReader(str(path))
    except (PdfReadError, OSError, ValueError) as exc:
        raise PdfExtractionError("pdf_open_failed") from exc

    if reader.is_encrypted:
        raise PdfExtractionError("encrypted_pdf")

    try:
        source_page_count = len(reader.pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise PdfExtractionError("pdf_page_count_failed") from exc

    if source_page_count < 1:
        raise PdfExtractionError("empty_pdf")
    if max_pages is not None and source_page_count > max_pages:
        raise PdfExtractionError("document_page_limit_exceeded")

    if ocr_sparse_text_chars_per_page < min_text_chars_per_page:
        raise ValueError("ocr_sparse_text_chars_per_page_must_cover_minimum_text")

    page_texts: list[str] = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except (PdfReadError, OSError, ValueError) as exc:
            # PyMuPDF and OCR can still recover this page; do not fail the
            # whole upload before the configured fallbacks get a chance.
            text = ""

        page_texts.append(text)

    parser_labels = [f"pypdf-{importlib.metadata.version('pypdf')}"]
    pymupdf_recovered_page_numbers: set[int] = set()
    pypdf_qualities = [_assess_text_quality(text) for text in page_texts]
    sparse_page_numbers = [
        page_no
        for page_no, quality in enumerate(pypdf_qualities, start=1)
        if quality.non_whitespace_chars < ocr_sparse_text_chars_per_page
    ]
    unicode_suspect_page_numbers = [
        page_no
        for page_no, quality in enumerate(pypdf_qualities, start=1)
        if quality.unicode_suspect
    ]
    native_fallback_page_numbers = sorted(
        set(sparse_page_numbers).union(unicode_suspect_page_numbers)
    )
    if native_fallback_page_numbers:
        native_fallbacks = _extract_pymupdf_page_texts(
            path,
            native_fallback_page_numbers,
        )
        for page_no in native_fallback_page_numbers:
            page_index = page_no - 1
            current_quality = _assess_text_quality(page_texts[page_index])
            fallback_text = native_fallbacks.get(page_no, "")
            fallback_quality = _assess_text_quality(fallback_text)
            if not _should_prefer_recovery(
                current_quality,
                fallback_quality,
                sparse_text_chars=ocr_sparse_text_chars_per_page,
            ):
                continue

            page_texts[page_index] = fallback_text
            if current_quality.unicode_suspect:
                if not fallback_quality.unicode_suspect:
                    pymupdf_recovered_page_numbers.add(page_no)
                _append_parser_label(parser_labels, "pymupdf-unicode-fallback")
            else:
                _append_parser_label(parser_labels, "pymupdf-sparse-fallback")

    ocr_attempted_pages: set[int] = set()
    ocr_successful_pages: set[int] = set()
    ocr_selected_pages: set[int] = set()
    ocr_failed_pages: set[int] = set()
    if tencent_ocr_config is not None:
        for page_no, text in enumerate(page_texts, start=1):
            page_index = page_no - 1
            current_quality = _assess_text_quality(text)
            if (
                current_quality.non_whitespace_chars
                >= ocr_sparse_text_chars_per_page
                and not current_quality.unicode_suspect
            ):
                continue
            ocr_attempted_pages.add(page_no)
            try:
                ocr_text = extract_pdf_page_text(
                    path=path,
                    page_no=page_no,
                    config=tencent_ocr_config,
                )
            except TencentOcrError:
                ocr_failed_pages.add(page_no)
                continue
            ocr_successful_pages.add(page_no)
            ocr_quality = _assess_text_quality(ocr_text)
            if _should_prefer_recovery(
                current_quality,
                ocr_quality,
                sparse_text_chars=ocr_sparse_text_chars_per_page,
            ):
                page_texts[page_index] = ocr_text
                ocr_selected_pages.add(page_no)
                _append_parser_label(parser_labels, "tencent-ocr")

    pages: list[ExtractedPage] = []
    flags: list[str] = []
    for page_no, text in enumerate(page_texts, start=1):
        non_whitespace_chars = len(NON_WHITESPACE.findall(text))
        pages.append(
            ExtractedPage(
                page_no=page_no,
                text=text,
                non_whitespace_chars=non_whitespace_chars,
            )
        )
        if non_whitespace_chars < min_text_chars_per_page:
            flags.append(f"page_{page_no}_insufficient_text")
        quality = _assess_text_quality(text)
        if quality.replacement_character_ratio >= _HARD_SOURCE_TEXT_DAMAGE_RATIO:
            flags.append(f"page_{page_no}_possible_mojibake")
        if page_no in pymupdf_recovered_page_numbers:
            flags.append(f"page_{page_no}_pymupdf_text_recovered")
        if quality.source_text_unreliable:
            flags.append(f"page_{page_no}_source_text_unreliable")
        if (
            page_no in ocr_failed_pages
            and non_whitespace_chars < min_text_chars_per_page
        ):
            flags.append(f"page_{page_no}_tencent_ocr_failed")

    parsed_page_count = sum(
        page.non_whitespace_chars >= min_text_chars_per_page for page in pages
    )
    if parsed_page_count != source_page_count:
        flags.append("parsed_page_count_mismatch")

    raw_text = "\n\n".join(
        f"--- PAGE {page.page_no} ---\n{page.text}" for page in pages if page.text
    )
    if max_text_chars is not None and len(raw_text) > max_text_chars:
        raise PdfExtractionError(
            "document_text_limit_exceeded",
            source_page_count=source_page_count,
            ocr_attempted_page_count=len(ocr_attempted_pages),
            ocr_successful_page_count=len(ocr_successful_pages),
            ocr_selected_page_count=len(ocr_selected_pages),
            ocr_failed_page_count=len(ocr_failed_pages),
        )
    if not raw_text:
        flags.append("no_extractable_text")

    return PdfExtractionResult(
        source_page_count=source_page_count,
        parsed_page_count=parsed_page_count,
        pages=pages,
        raw_text=raw_text,
        quality_flags=sorted(set(flags)),
        parser_version="+".join(parser_labels),
        ocr_attempted_page_count=len(ocr_attempted_pages),
        ocr_successful_page_count=len(ocr_successful_pages),
        ocr_selected_page_count=len(ocr_selected_pages),
        ocr_failed_page_count=len(ocr_failed_pages),
    )


def _non_whitespace_char_count(text: str) -> int:
    return len(NON_WHITESPACE.findall(text))


def _assess_text_quality(text: str) -> _TextQuality:
    non_whitespace_chars = 0
    textual_chars = 0
    replacement_chars = 0
    private_use_chars = 0
    control_chars = 0
    unassigned_chars = 0
    cjk_radical_chars = 0
    damaged_character_positions: set[int] = set()

    for index, character in enumerate(text):
        if character.isspace():
            continue

        non_whitespace_chars += 1
        category = unicodedata.category(character)
        if category[:1] in {"L", "M", "N"}:
            textual_chars += 1
        if character == "\ufffd":
            replacement_chars += 1
            damaged_character_positions.add(index)
        if category == "Co":
            private_use_chars += 1
            damaged_character_positions.add(index)
        elif category == "Cc":
            control_chars += 1
            damaged_character_positions.add(index)
        elif category == "Cn":
            unassigned_chars += 1
            damaged_character_positions.add(index)
        if _is_cjk_radical(character):
            cjk_radical_chars += 1
            damaged_character_positions.add(index)

    for match in _LATIN1_MOJIBAKE_PAIR.finditer(text):
        damaged_character_positions.update(range(match.start(), match.end()))

    return _TextQuality(
        non_whitespace_chars=non_whitespace_chars,
        textual_chars=textual_chars,
        replacement_chars=replacement_chars,
        private_use_chars=private_use_chars,
        control_chars=control_chars,
        unassigned_chars=unassigned_chars,
        cjk_radical_chars=cjk_radical_chars,
        latin1_mojibake_pairs=len(_LATIN1_MOJIBAKE_PAIR.findall(text)),
        damaged_char_count=sum(
            1
            for index in damaged_character_positions
            if not text[index].isspace()
        ),
    )


def _is_cjk_radical(character: str) -> bool:
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _CJK_RADICAL_RANGES)


def _should_prefer_recovery(
    current: _TextQuality,
    candidate: _TextQuality,
    *,
    sparse_text_chars: int,
) -> bool:
    """Choose a fallback only when it fixes a meaningful extraction problem.

    Sparse pages retain the existing behaviour: more usable text wins. For a
    long but malformed page, a slightly shorter PyMuPDF/OCR result is allowed
    to win only when it removes substantial Unicode damage and still preserves
    at least half of the page's text volume.
    """

    if candidate.non_whitespace_chars == 0:
        return False

    if current.unicode_suspect:
        minimum_candidate_chars = max(20, math.ceil(current.non_whitespace_chars * 0.5))
        minimum_score_gain = max(25, math.ceil(current.non_whitespace_chars * 0.04))
        return (
            candidate.non_whitespace_chars >= minimum_candidate_chars
            and candidate.unicode_damage < current.unicode_damage
            and candidate.score >= current.score + minimum_score_gain
        )

    if current.non_whitespace_chars < sparse_text_chars:
        return (
            candidate.non_whitespace_chars > current.non_whitespace_chars
            and candidate.score >= current.score
        )

    return False


def _append_parser_label(parser_labels: list[str], label: str) -> None:
    if label not in parser_labels:
        parser_labels.append(label)


def _extract_pymupdf_page_texts(path: Path, page_numbers: list[int]) -> dict[int, str]:
    """Return a best-effort native fallback; OCR remains the final fallback."""

    try:
        document = fitz.open(str(path))
        try:
            return {
                page_no: document.load_page(page_no - 1).get_text("text").strip()
                for page_no in page_numbers
                if 1 <= page_no <= document.page_count
            }
        finally:
            document.close()
    except (fitz.FileDataError, OSError, RuntimeError, ValueError):
        return {}
