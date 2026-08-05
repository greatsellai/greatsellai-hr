from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import tencent_ocr_provider as provider
from app.services.tencent_ocr_provider import TencentOcrConfig, TencentOcrError


def _config(*, api: str = "GeneralBasicOCR") -> TencentOcrConfig:
    return TencentOcrConfig(
        secret_id="test-secret-id",
        secret_key="test-secret-key",
        region="ap-guangzhou",
        timeout_seconds=5,
        api=api,
    )


def _minimal_jpeg(*, width: int = 100, height: int = 100) -> bytes:
    return (
        b"\xff\xd8"
        b"\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x01\x01\x11\x00\xff\xd9"
    )


def _minimal_png(*, width: int = 100, height: int = 100) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def test_image_ocr_uses_legacy_basic_api_by_default_without_public_image_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "resume.jpg"
    image_bytes = _minimal_jpeg()
    image_path.write_bytes(image_bytes)
    captured: dict[str, object] = {}

    class FakeRequest:
        def from_json_string(self, value: str) -> None:
            captured["request"] = json.loads(value)

    class FakeClient:
        def __init__(self, *_args: object) -> None:
            pass

        def GeneralBasicOCR(self, _request: object) -> object:
            return SimpleNamespace(
                TextDetections=[
                    SimpleNamespace(DetectedText="  Candidate Name  "),
                    SimpleNamespace(DetectedText=""),
                    SimpleNamespace(DetectedText="Python"),
                ]
            )

    monkeypatch.setattr(provider.models, "GeneralBasicOCRRequest", FakeRequest)
    monkeypatch.setattr(provider.ocr_client, "OcrClient", FakeClient)

    result = provider.extract_image_text(path=image_path, config=_config())

    assert result == "Candidate Name\nPython"
    payload = captured["request"]
    assert isinstance(payload, dict)
    assert set(payload) == {"ImageBase64"}
    assert base64.b64decode(str(payload["ImageBase64"])) == image_bytes


def test_image_ocr_uses_explicit_accurate_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "resume.jpg"
    image_bytes = _minimal_jpeg()
    image_path.write_bytes(image_bytes)
    captured: dict[str, object] = {}

    class FakeRequest:
        def from_json_string(self, value: str) -> None:
            captured["request"] = json.loads(value)

    class FakeClient:
        def __init__(self, *_args: object) -> None:
            pass

        def GeneralAccurateOCR(self, _request: object) -> object:
            captured["api"] = "GeneralAccurateOCR"
            return SimpleNamespace(
                TextDetections=[SimpleNamespace(DetectedText="Accurate candidate name")]
            )

        def GeneralBasicOCR(self, _request: object) -> object:
            raise AssertionError("the explicit accurate API must not call GeneralBasicOCR")

    monkeypatch.setattr(provider.models, "GeneralAccurateOCRRequest", FakeRequest)
    monkeypatch.setattr(provider.ocr_client, "OcrClient", FakeClient)

    result = provider.extract_image_text(
        path=image_path,
        config=_config(api="GeneralAccurateOCR"),
    )

    assert result == "Accurate candidate name"
    assert captured["api"] == "GeneralAccurateOCR"
    payload = captured["request"]
    assert isinstance(payload, dict)
    assert base64.b64decode(str(payload["ImageBase64"])) == image_bytes


def test_oversized_image_is_reencoded_before_tencent_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "large.jpg"
    image_path.write_bytes(b"x" * (provider._MAX_OCR_IMAGE_BYTES + 1))
    captured: dict[str, object] = {}

    def fake_reencode(*, path: Path) -> bytes:
        captured["reencode_path"] = path
        return b"compressed-image"

    def fake_request(*, image_bytes: bytes, config: TencentOcrConfig) -> str:
        captured["image_bytes"] = image_bytes
        captured["config"] = config
        return "Recovered text"

    monkeypatch.setattr(provider, "_reencode_image_within_ocr_limit", fake_reencode)
    monkeypatch.setattr(provider, "_extract_tencent_ocr", fake_request)
    monkeypatch.setattr(provider, "_image_dimensions", lambda _bytes: (100, 100))

    assert provider.extract_image_text(path=image_path, config=_config()) == "Recovered text"
    assert captured["reencode_path"] == image_path
    assert captured["image_bytes"] == b"compressed-image"


def test_provider_failure_does_not_expose_upstream_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "resume.png"
    image_path.write_bytes(_minimal_png())

    class FailingClient:
        def __init__(self, *_args: object) -> None:
            raise RuntimeError("upstream credential detail must not escape")

    monkeypatch.setattr(provider.ocr_client, "OcrClient", FailingClient)

    with pytest.raises(TencentOcrError) as raised:
        provider.extract_image_text(path=image_path, config=_config())

    assert str(raised.value) == "tencent_ocr_request_failed"


def test_tencent_auth_and_throttle_codes_are_classified_without_leaking_details() -> None:
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException,
    )

    assert (
        provider._provider_error_code(
            TencentCloudSDKException("AuthFailure.SignatureFailure", "synthetic", "id")
        )
        == "tencent_ocr_auth_failed"
    )
    assert (
        provider._provider_error_code(
            TencentCloudSDKException("RequestLimitExceeded", "synthetic", "id")
        )
        == "tencent_ocr_rate_limited"
    )


def test_image_dimension_limit_is_checked_before_local_reencoding(tmp_path: Path) -> None:
    image_path = tmp_path / "pixel-bomb.png"
    image_path.write_bytes(_minimal_png(width=100_000, height=100_000))

    with pytest.raises(TencentOcrError) as raised:
        provider._prepare_image_for_ocr(path=image_path)

    assert str(raised.value) == "tencent_ocr_image_dimensions_too_large"


def test_transparent_png_is_flattened_before_jpeg_reencoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fitz

    transparent = fitz.Pixmap(
        fitz.csRGB,
        2,
        2,
        bytes([10, 20, 30, 128]) * 4,
        True,
    )
    image_path = tmp_path / "transparent.png"
    transparent.save(image_path)
    monkeypatch.setattr(provider, "_MAX_OCR_IMAGE_BYTES", 1_000)

    encoded = provider._reencode_image_within_ocr_limit(path=image_path)

    assert encoded.startswith(b"\xff\xd8")


def test_transparent_pixels_are_composited_on_white_before_jpeg_encoding() -> None:
    import fitz

    transparent_black_and_opaque_black = fitz.Pixmap(
        fitz.csRGB,
        2,
        1,
        bytes([0, 0, 0, 0, 0, 0, 0, 255]),
        True,
    )

    flattened = provider._flatten_alpha_on_white(transparent_black_and_opaque_black)

    assert flattened.alpha == 0
    assert list(flattened.samples) == [255, 255, 255, 0, 0, 0]
    assert provider._encode_pixmap_as_jpeg(
        transparent_black_and_opaque_black,
        quality=85,
    ).startswith(b"\xff\xd8")


def test_semi_transparent_pixels_are_blended_on_white() -> None:
    import fitz

    semi_transparent_black = fitz.Pixmap(
        fitz.csRGB,
        1,
        1,
        bytes([0, 0, 0, 128]),
        True,
    )

    flattened = provider._flatten_alpha_on_white(semi_transparent_black)

    assert list(flattened.samples) == [127, 127, 127]
