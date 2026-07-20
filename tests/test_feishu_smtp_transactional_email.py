from __future__ import annotations

import logging
import smtplib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.services.transactional_email import (
    FeishuSmtpTransactionalEmailProvider,
    TransactionalEmailError,
    VerificationDelivery,
    build_transactional_email_provider,
)


def _settings(tmp_path: Path, **overrides: object) -> AppSettings:
    values: dict[str, object] = {
        "project_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "upload_dir": tmp_path / "data" / "uploads",
        "database_url": "sqlite://",
        "transactional_email_provider": "feishu_smtp",
        "transactional_email_from": "GreatSell AI <noreply@example.test>",
        "public_app_url": "https://hr.example.test",
        "feishu_smtp_username": "noreply@example.test",
        "feishu_smtp_password": "smtp-password-fixture",
    }
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


def _delivery() -> VerificationDelivery:
    return VerificationDelivery(
        recipient="candidate@example.test",
        verification_url="https://hr.example.test/verify-email?token=fixture-token",
        expires_minutes=60,
    )


class _SslSmtpCapture:
    instances: list["_SslSmtpCapture"] = []

    def __init__(self, host: str, port: int, *, timeout: int, context: object) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.login_args: tuple[str, str] | None = None
        self.message_args: tuple[object, str, list[str]] | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "_SslSmtpCapture":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: object, *, from_addr: str, to_addrs: list[str]) -> None:
        self.message_args = (message, from_addr, to_addrs)


class _StartTlsSmtpCapture:
    instances: list["_StartTlsSmtpCapture"] = []

    def __init__(self, host: str, port: int, *, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_calls = 0
        self.starttls_context: object | None = None
        self.login_args: tuple[str, str] | None = None
        self.message_args: tuple[object, str, list[str]] | None = None
        self.__class__.instances.append(self)

    def __enter__(self) -> "_StartTlsSmtpCapture":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self, *, context: object) -> None:
        self.starttls_context = context

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: object, *, from_addr: str, to_addrs: list[str]) -> None:
        self.message_args = (message, from_addr, to_addrs)


def test_feishu_smtp_ssl_provider_sends_a_multipart_verification_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SslSmtpCapture.instances.clear()
    monkeypatch.setattr(
        "app.services.transactional_email.smtplib.SMTP_SSL",
        _SslSmtpCapture,
    )
    settings = _settings(tmp_path)
    settings.validate_runtime()

    provider = build_transactional_email_provider(settings)
    assert isinstance(provider, FeishuSmtpTransactionalEmailProvider)
    provider.send_email_verification(_delivery())

    captured = _SslSmtpCapture.instances[-1]
    assert (captured.host, captured.port, captured.timeout) == ("smtp.feishu.cn", 465, 20)
    assert captured.login_args == ("noreply@example.test", "smtp-password-fixture")
    assert captured.message_args is not None
    message, from_address, recipients = captured.message_args
    assert from_address == "noreply@example.test"
    assert recipients == ["candidate@example.test"]
    assert message["Subject"] == "验证你的 GreatSell AI 工作邮箱"
    assert "fixture-token" in message.get_body(preferencelist=("plain",)).get_content()
    assert "验证工作邮箱" in message.get_body(preferencelist=("html",)).get_content()


def test_feishu_smtp_starttls_uses_tls_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _StartTlsSmtpCapture.instances.clear()
    monkeypatch.setattr(
        "app.services.transactional_email.smtplib.SMTP",
        _StartTlsSmtpCapture,
    )
    settings = _settings(
        tmp_path,
        feishu_smtp_port=587,
        feishu_smtp_tls_mode="starttls",
    )
    settings.validate_runtime()

    build_transactional_email_provider(settings).send_email_verification(_delivery())

    captured = _StartTlsSmtpCapture.instances[-1]
    assert (captured.host, captured.port, captured.timeout) == ("smtp.feishu.cn", 587, 20)
    assert captured.ehlo_calls == 2
    assert captured.starttls_context is not None
    assert captured.login_args == ("noreply@example.test", "smtp-password-fixture")


def test_feishu_smtp_provider_allows_a_real_registration_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SslSmtpCapture.instances.clear()
    monkeypatch.setattr(
        "app.services.transactional_email.smtplib.SMTP_SSL",
        _SslSmtpCapture,
    )
    settings = _settings(tmp_path, session_secret="feishu-smtp-registration-test-session")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/auth/register",
            json={
                "organization_name": "Feishu SMTP test workspace",
                "full_name": "Feishu SMTP test admin",
                "email": "feishu-smtp-admin@example.test",
                "password": "feishu-smtp-test-password",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["email_verification_required"] is True
    assert _SslSmtpCapture.instances[-1].message_args is not None


def test_feishu_smtp_rejects_incomplete_or_insecure_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires username and app password"):
        _settings(tmp_path, feishu_smtp_password=None).validate_runtime()

    with pytest.raises(ValueError, match="SSL must use port 465"):
        _settings(tmp_path, feishu_smtp_port=587).validate_runtime()

    with pytest.raises(ValueError, match="must match"):
        _settings(tmp_path, feishu_smtp_username="other@example.test").validate_runtime()


def test_settings_repr_never_includes_smtp_app_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert "smtp-password-fixture" not in repr(settings)


def test_feishu_smtp_hides_transport_details_when_authentication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingSmtp:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> "FailingSmtp":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def login(self, *_: object) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"authentication denied")

    monkeypatch.setattr("app.services.transactional_email.smtplib.SMTP_SSL", FailingSmtp)
    caplog.set_level(logging.WARNING)
    provider = build_transactional_email_provider(_settings(tmp_path))

    with pytest.raises(TransactionalEmailError, match="email_delivery_provider_failed"):
        provider.send_email_verification(_delivery())

    assert "smtp-password-fixture" not in caplog.text
    assert "candidate@example.test" not in caplog.text
    assert "fixture-token" not in caplog.text
