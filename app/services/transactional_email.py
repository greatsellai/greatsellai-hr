"""Small, provider-neutral transactional email boundary.

Resume mailbox ingestion uses IMAP and must never be reused for account
messages.  Account verification is sent through this module so business
routes never handle cloud-provider SDK details or log raw action links.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from typing import Protocol
from urllib.parse import urlencode

from app.config import AppSettings


logger = logging.getLogger(__name__)


class TransactionalEmailError(RuntimeError):
    """Stable, non-sensitive account-email delivery error."""


@dataclass(frozen=True)
class VerificationDelivery:
    recipient: str
    verification_url: str
    expires_minutes: int


class TransactionalEmailProvider(Protocol):
    """One-purpose interface kept intentionally small for future providers."""

    @property
    def configured(self) -> bool: ...

    def send_email_verification(self, delivery: VerificationDelivery) -> None: ...


class DisabledTransactionalEmailProvider:
    @property
    def configured(self) -> bool:
        return False

    def send_email_verification(self, delivery: VerificationDelivery) -> None:
        raise TransactionalEmailError("email_delivery_not_configured")


@dataclass
class TestTransactionalEmailProvider:
    """In-memory delivery capture used only by local tests.

    It is not selectable in production settings.  Keeping links in process
    memory makes end-to-end token tests possible without printing them.
    """

    deliveries: list[VerificationDelivery] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return True

    def send_email_verification(self, delivery: VerificationDelivery) -> None:
        self.deliveries.append(delivery)


class TencentSesTransactionalEmailProvider:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return True

    def send_email_verification(self, delivery: VerificationDelivery) -> None:
        # Imports stay local so local development with a disabled sender does
        # not initialize the cloud SDK or credentials.
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )
        from tencentcloud.ses.v20201002 import models, ses_client

        try:
            client = ses_client.SesClient(
                credential.Credential(
                    self._settings.tencent_secret_id,
                    self._settings.tencent_secret_key,
                ),
                self._settings.tencent_ses_region,
            )
            request = models.SendEmailRequest()
            request.FromEmailAddress = self._settings.transactional_email_from
            request.Subject = "验证你的 GreatSell AI 工作邮箱"
            request.Destination = [delivery.recipient]
            request.TriggerType = 1

            template = models.Template()
            template.TemplateID = self._settings.tencent_ses_verification_template_id
            template.TemplateData = json.dumps(
                {
                    "verify_url": delivery.verification_url,
                    "expires_minutes": str(delivery.expires_minutes),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            request.Template = template
            client.SendEmail(request)
        except TencentCloudSDKException as exc:
            logger.warning("transactional_email_provider_failed provider=tencent_ses")
            raise TransactionalEmailError("email_delivery_provider_failed") from exc
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("transactional_email_transport_failed provider=tencent_ses")
            raise TransactionalEmailError("email_delivery_provider_failed") from exc


class FeishuSmtpTransactionalEmailProvider:
    """Temporary SMTP sender backed by one dedicated Feishu public mailbox."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return True

    def send_email_verification(self, delivery: VerificationDelivery) -> None:
        message = _verification_email_message(self._settings, delivery)
        from_address = parseaddr(self._settings.transactional_email_from or "")[1]
        context = ssl.create_default_context()
        try:
            if self._settings.feishu_smtp_tls_mode == "ssl":
                with smtplib.SMTP_SSL(
                    self._settings.feishu_smtp_host,
                    self._settings.feishu_smtp_port,
                    timeout=self._settings.feishu_smtp_timeout_seconds,
                    context=context,
                ) as client:
                    _smtp_send_verification(
                        client,
                        username=self._settings.feishu_smtp_username or "",
                        password=self._settings.feishu_smtp_password or "",
                        message=message,
                        from_address=from_address,
                        recipient=delivery.recipient,
                    )
            else:
                with smtplib.SMTP(
                    self._settings.feishu_smtp_host,
                    self._settings.feishu_smtp_port,
                    timeout=self._settings.feishu_smtp_timeout_seconds,
                ) as client:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                    _smtp_send_verification(
                        client,
                        username=self._settings.feishu_smtp_username or "",
                        password=self._settings.feishu_smtp_password or "",
                        message=message,
                        from_address=from_address,
                        recipient=delivery.recipient,
                    )
        except (smtplib.SMTPException, OSError, TimeoutError, ValueError) as exc:
            logger.warning("transactional_email_transport_failed provider=feishu_smtp")
            raise TransactionalEmailError("email_delivery_provider_failed") from exc


def _smtp_send_verification(
    client: smtplib.SMTP,
    *,
    username: str,
    password: str,
    message: EmailMessage,
    from_address: str,
    recipient: str,
) -> None:
    client.login(username, password)
    client.send_message(message, from_addr=from_address, to_addrs=[recipient])


def _verification_email_message(
    settings: AppSettings,
    delivery: VerificationDelivery,
) -> EmailMessage:
    """Build a plain-text and HTML account-verification email in memory."""

    message = EmailMessage()
    message["From"] = settings.transactional_email_from or ""
    message["To"] = delivery.recipient
    message["Subject"] = "验证你的 GreatSell AI 工作邮箱"
    message.set_content(
        "您好，\n\n"
        "请打开以下链接验证你的 GreatSell AI 工作邮箱：\n"
        f"{delivery.verification_url}\n\n"
        f"链接将在 {delivery.expires_minutes} 分钟后失效。若不是你本人注册，请忽略此邮件。\n"
    )
    verification_url = escape(delivery.verification_url, quote=True)
    message.add_alternative(
        "<html><body>"
        "<p>您好，</p>"
        "<p>请点击以下按钮验证你的 GreatSell AI 工作邮箱：</p>"
        f'<p><a href="{verification_url}">验证工作邮箱</a></p>'
        f"<p>链接将在 {delivery.expires_minutes} 分钟后失效。若不是你本人注册，请忽略此邮件。</p>"
        "</body></html>",
        subtype="html",
    )
    return message


def build_transactional_email_provider(settings: AppSettings) -> TransactionalEmailProvider:
    if settings.transactional_email_provider == "tencent_ses":
        return TencentSesTransactionalEmailProvider(settings)
    if settings.transactional_email_provider == "feishu_smtp":
        return FeishuSmtpTransactionalEmailProvider(settings)
    if settings.transactional_email_provider == "test":
        return TestTransactionalEmailProvider()
    return DisabledTransactionalEmailProvider()


def email_verification_url(settings: AppSettings, *, token: str) -> str:
    if not settings.public_app_url:
        raise TransactionalEmailError("email_delivery_not_configured")
    base_url = settings.public_app_url.rstrip("/")
    return f"{base_url}/verify-email?{urlencode({'token': token})}"
