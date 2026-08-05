from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import fitz
from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ocr.v20181119 import models, ocr_client

from app.config import (
    TENCENT_OCR_API_GENERAL_ACCURATE,
    TENCENT_OCR_API_GENERAL_BASIC,
    TENCENT_OCR_APIS,
)

# Tencent GeneralBasicOCR and GeneralAccurateOCR both accept an ImageBase64
# payload up to 10 MiB. Base64 expands its input by roughly 4/3, so keeping the
# raw image under 7 MiB leaves protocol headroom without relying on an
# undocumented server-side rounding rule. PDF pages retain a lower render
# budget below because they are created locally from an untrusted PDF and a
# conservative raster is sufficient for resume text.
_MAX_OCR_IMAGE_BYTES = 7 * 1024 * 1024
_MAX_RENDERED_PDF_IMAGE_BYTES = 3_500_000
# A 300 DPI A4 page is below this limit.  Capping it keeps the rare local
# image-normalisation step bounded on a small worker while OCR itself remains
# a Tencent Cloud request rather than a resident local engine.
_MAX_REENCODE_IMAGE_PIXELS = 12_000_000
_MAX_JPEG_DIMENSION_HEADER_BYTES = 1024 * 1024
_IMAGE_REENCODE_SCALES = (1.0, 0.8, 0.65, 0.5, 0.4)
_IMAGE_REENCODE_QUALITIES = (85, 75, 65)


class TencentOcrError(RuntimeError):
    """A UI-safe OCR provider failure; raw provider output is never retained."""


@dataclass(frozen=True)
class TencentOcrConfig:
    secret_id: str
    secret_key: str
    region: str
    timeout_seconds: int
    api: str = TENCENT_OCR_API_GENERAL_BASIC


def extract_pdf_page_text(
    *,
    path: Path,
    page_no: int,
    config: TencentOcrConfig,
) -> str:
    """Render one PDF page locally and submit its Base64 bytes to Tencent OCR.

    No file URL is created: the original PDF and rendered image stay on the
    server, and the image exists only in process memory for this request.
    """

    image_bytes = _render_page_for_ocr(path=path, page_no=page_no)
    return _extract_tencent_ocr(image_bytes=image_bytes, config=config)


def extract_image_text(
    *,
    path: Path,
    config: TencentOcrConfig,
) -> str:
    """Submit a PNG/JPEG original to Tencent OCR without publishing a URL.

    Small originals go directly from the workspace file to the provider.  A
    larger but accepted upload is locally re-encoded only when needed to stay
    below Tencent's Base64 request limit.  The original is never mutated,
    copied to public storage, or sent through an ImageUrl.
    """

    image_bytes = _prepare_image_for_ocr(path=path)
    return _extract_tencent_ocr(image_bytes=image_bytes, config=config)


def _extract_tencent_ocr(
    *,
    image_bytes: bytes,
    config: TencentOcrConfig,
) -> str:
    if not image_bytes:
        raise TencentOcrError("tencent_ocr_invalid_image")
    if len(image_bytes) > _MAX_OCR_IMAGE_BYTES:
        raise TencentOcrError("tencent_ocr_image_too_large")
    if config.api not in TENCENT_OCR_APIS:
        raise TencentOcrError("tencent_ocr_request_invalid")
    try:
        http_profile = HttpProfile()
        http_profile.reqTimeout = config.timeout_seconds
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = ocr_client.OcrClient(
            credential.Credential(config.secret_id, config.secret_key),
            config.region,
            client_profile,
        )
        request = (
            models.GeneralAccurateOCRRequest()
            if config.api == TENCENT_OCR_API_GENERAL_ACCURATE
            else models.GeneralBasicOCRRequest()
        )
        request.from_json_string(
            json.dumps(
                {"ImageBase64": base64.b64encode(image_bytes).decode("ascii")},
                separators=(",", ":"),
            )
        )
        response = (
            client.GeneralAccurateOCR(request)
            if config.api == TENCENT_OCR_API_GENERAL_ACCURATE
            else client.GeneralBasicOCR(request)
        )
    except TencentCloudSDKException as exc:
        raise TencentOcrError(_provider_error_code(exc)) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise TencentOcrError("tencent_ocr_request_failed") from exc

    try:
        lines = [
            item.DetectedText.strip()
            for item in (response.TextDetections or [])
            if item.DetectedText and item.DetectedText.strip()
        ]
    except (AttributeError, TypeError) as exc:
        raise TencentOcrError("tencent_ocr_invalid_response") from exc
    return "\n".join(lines)


def _provider_error_code(exc: TencentCloudSDKException) -> str:
    """Classify provider failures without retaining provider text or request IDs."""

    raw_code = (exc.get_code() or "").strip().lower()
    if raw_code in {"authfailure", "unauthorizedoperation"} or raw_code.startswith(
        ("authfailure.", "unauthorizedoperation.")
    ):
        return "tencent_ocr_auth_failed"
    if raw_code in {
        "invalidparameter",
        "invalidparametervalue",
        "unsupportedoperation",
    } or raw_code.startswith(
        ("invalidparameter.", "invalidparametervalue.", "unsupportedoperation.")
    ):
        return "tencent_ocr_request_invalid"
    if raw_code in {"requestlimitexceeded", "limitexceeded"} or raw_code.startswith(
        ("requestlimitexceeded.", "limitexceeded.")
    ):
        return "tencent_ocr_rate_limited"
    return "tencent_ocr_request_failed"


def _prepare_image_for_ocr(*, path: Path) -> bytes:
    try:
        image_bytes = path.read_bytes()
    except OSError as exc:
        raise TencentOcrError("tencent_ocr_image_open_failed") from exc
    if not image_bytes:
        raise TencentOcrError("tencent_ocr_invalid_image")
    width, height = _image_dimensions(image_bytes)
    if width * height > _MAX_REENCODE_IMAGE_PIXELS:
        raise TencentOcrError("tencent_ocr_image_dimensions_too_large")
    if len(image_bytes) <= _MAX_OCR_IMAGE_BYTES:
        return image_bytes
    return _reencode_image_within_ocr_limit(path=path)


def _reencode_image_within_ocr_limit(*, path: Path) -> bytes:
    """Shrink an oversized image for Tencent without changing its original.

    This branch is intentionally rare: normal resume screenshots are submitted
    as-is and no local OCR engine is involved.  The bounded scale/quality
    ladder prevents a 15 MiB browser upload from exceeding Tencent's Base64
    request size while retaining a legible raster for text recognition.
    """

    try:
        source = fitz.Pixmap(str(path))
        if source.width < 1 or source.height < 1:
            raise TencentOcrError("tencent_ocr_invalid_image")
        if source.width * source.height > _MAX_REENCODE_IMAGE_PIXELS:
            raise TencentOcrError("tencent_ocr_image_dimensions_too_large")
        for scale in _IMAGE_REENCODE_SCALES:
            width = max(1, round(source.width * scale))
            height = max(1, round(source.height * scale))
            pixmap = (
                source
                if width == source.width and height == source.height
                else fitz.Pixmap(source, width, height, None)
            )
            for quality in _IMAGE_REENCODE_QUALITIES:
                image_bytes = _encode_pixmap_as_jpeg(pixmap, quality=quality)
                if len(image_bytes) <= _MAX_OCR_IMAGE_BYTES:
                    return image_bytes
    except TencentOcrError:
        raise
    except (fitz.FileDataError, OSError, RuntimeError, ValueError) as exc:
        raise TencentOcrError("tencent_ocr_image_prepare_failed") from exc
    raise TencentOcrError("tencent_ocr_image_too_large")


def _encode_pixmap_as_jpeg(pixmap: fitz.Pixmap, *, quality: int) -> bytes:
    """Return a JPEG even when a PNG source has an alpha channel.

    PyMuPDF intentionally rejects direct JPEG output for an RGBA pixmap.  Do
    not use ``Pixmap(pixmap, 0)`` here: it merely discards alpha and leaves
    transparent pixels black.  Explicitly compositing onto white preserves
    the normal document-page appearance, including black text in a
    transparent PNG.
    """

    opaque_pixmap = _flatten_alpha_on_white(pixmap) if pixmap.alpha else pixmap
    return opaque_pixmap.tobytes("jpeg", jpg_quality=quality)


def _flatten_alpha_on_white(pixmap: fitz.Pixmap) -> fitz.Pixmap:
    """Return an opaque RGB pixmap after alpha compositing on white.

    Image uploads are bounded to a safe pixel count before this code runs, so
    the explicit byte buffer has a known upper bound.  The helper also keeps
    transparency handling deterministic across PyMuPDF versions.
    """

    rgba_pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
    if not rgba_pixmap.alpha or rgba_pixmap.n != 4:
        raise TencentOcrError("tencent_ocr_image_prepare_failed")

    source = rgba_pixmap.samples
    destination = bytearray(rgba_pixmap.width * rgba_pixmap.height * 3)
    destination_offset = 0
    for row in range(rgba_pixmap.height):
        source_offset = row * rgba_pixmap.stride
        for _column in range(rgba_pixmap.width):
            red = source[source_offset]
            green = source[source_offset + 1]
            blue = source[source_offset + 2]
            alpha = source[source_offset + 3]
            inverse_alpha = 255 - alpha
            destination[destination_offset] = (red * alpha + 255 * inverse_alpha + 127) // 255
            destination[destination_offset + 1] = (
                green * alpha + 255 * inverse_alpha + 127
            ) // 255
            destination[destination_offset + 2] = (blue * alpha + 255 * inverse_alpha + 127) // 255
            source_offset += 4
            destination_offset += 3
    return fitz.Pixmap(
        fitz.csRGB,
        rgba_pixmap.width,
        rgba_pixmap.height,
        bytes(destination),
        False,
    )


def _image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(image_bytes) < 24 or image_bytes[12:16] != b"IHDR":
            raise TencentOcrError("tencent_ocr_invalid_image")
        width, height = struct.unpack(">II", image_bytes[16:24])
        if width < 1 or height < 1:
            raise TencentOcrError("tencent_ocr_invalid_image")
        return width, height
    if image_bytes.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(image_bytes)
    raise TencentOcrError("tencent_ocr_invalid_image")


def _jpeg_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """Read JPEG dimensions without inflating the image into a bitmap."""

    limit = min(len(image_bytes), _MAX_JPEG_DIMENSION_HEADER_BYTES)
    offset = 2
    while offset < limit:
        while offset < limit and image_bytes[offset] != 0xFF:
            offset += 1
        while offset < limit and image_bytes[offset] == 0xFF:
            offset += 1
        if offset >= limit:
            break
        marker = image_bytes[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > limit:
            break
        segment_length = int.from_bytes(image_bytes[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > limit:
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 7:
                break
            height = int.from_bytes(image_bytes[offset + 3 : offset + 5], "big")
            width = int.from_bytes(image_bytes[offset + 5 : offset + 7], "big")
            if width > 0 and height > 0:
                return width, height
            break
        offset += segment_length
    raise TencentOcrError("tencent_ocr_invalid_image")


def _render_page_for_ocr(*, path: Path, page_no: int) -> bytes:
    if page_no < 1:
        raise TencentOcrError("tencent_ocr_invalid_page")
    try:
        document = fitz.open(str(path))
        try:
            if page_no > document.page_count:
                raise TencentOcrError("tencent_ocr_invalid_page")
            page = document.load_page(page_no - 1)
            # Grayscale prevents a photo or decorative background from making
            # the Base64 request oversized while retaining readable text.
            for scale in (2.0, 1.6, 1.3):
                pixmap = page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale),
                    colorspace=fitz.csGRAY,
                    alpha=False,
                )
                image_bytes = pixmap.tobytes("png")
                if len(image_bytes) <= _MAX_RENDERED_PDF_IMAGE_BYTES:
                    return image_bytes
        finally:
            document.close()
    except TencentOcrError:
        raise
    except (fitz.FileDataError, OSError, RuntimeError, ValueError) as exc:
        raise TencentOcrError("tencent_ocr_page_render_failed") from exc
    raise TencentOcrError("tencent_ocr_image_too_large")
