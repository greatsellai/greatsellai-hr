"""Small, provider-neutral transactional email boundary.

Resume mailbox ingestion uses IMAP and must never be reused for account
messages.  Account verification is sent through this module so business
routes never handle cloud-provider SDK details or log raw action links.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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


def build_transactional_email_provider(settings: AppSettings) -> TransactionalEmailProvider:
    if settings.transactional_email_provider == "tencent_ses":
        return TencentSesTransactionalEmailProvider(settings)
    if settings.transactional_email_provider == "test":
        return TestTransactionalEmailProvider()
    return DisabledTransactionalEmailProvider()


def email_verification_url(settings: AppSettings, *, token: str) -> str:
    if not settings.public_app_url:
        raise TransactionalEmailError("email_delivery_not_configured")
    base_url = settings.public_app_url.rstrip("/")
    return f"{base_url}/verify-email?{urlencode({'token': token})}"
