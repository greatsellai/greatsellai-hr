"""Mailbox provider catalogue and its endpoint-ownership contract.

Most providers have a reviewed, fixed IMAPS endpoint.  ``generic_imap`` is a
deliberate exception for corporate mail systems not represented by a preset:
it accepts a domain name, but the transport still enforces IMAPS 993, public
DNS-only resolution, TLS hostname verification and DNS-pinned connections.
It is never an IP-address or arbitrary-port escape hatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.services.mailbox_imap_transport import (
    MailboxImapTransportError,
    validate_imap_endpoint,
)

if TYPE_CHECKING:
    from app.config import AppSettings


MailboxAuthenticationMode = Literal["app_password", "oauth2"]
GENERIC_IMAP_PROVIDER_KEY = "generic_imap"


class MailboxProviderError(RuntimeError):
    """A stable, non-sensitive provider catalogue error."""


@dataclass(frozen=True)
class MailboxProvider:
    """One mailbox connection type and its user-facing guidance.

    Fixed providers own their endpoint in this catalogue.  The one generic
    provider deliberately has no static host and asks the workspace admin for
    a domain that is validated again by the transport at every connection.
    """

    key: str
    display_name: str
    imap_host: str | None
    authentication_mode: MailboxAuthenticationMode
    credential_label: str
    help_text: str
    imap_port: int = 993
    default_mailbox: str = "INBOX"
    allows_custom_endpoint: bool = False


_PROVIDERS: tuple[MailboxProvider, ...] = (
    MailboxProvider(
        key="feishu_app_password",
        display_name="飞书邮箱",
        imap_host="imap.feishu.cn",
        authentication_mode="app_password",
        credential_label="飞书专用密码",
        help_text="请先由邮箱管理员开启第三方客户端登录，再粘贴飞书专用密码。",
    ),
    MailboxProvider(
        key="tencent_exmail_app_password",
        display_name="腾讯企业邮箱",
        imap_host="imap.exmail.qq.com",
        authentication_mode="app_password",
        credential_label="邮箱密码或客户端专用密码",
        help_text="使用腾讯企业邮箱已启用 IMAP 的账号；如开启安全登录，请填写客户端专用密码。",
    ),
    MailboxProvider(
        key="qq_mail_app_password",
        display_name="QQ 邮箱",
        imap_host="imap.qq.com",
        authentication_mode="app_password",
        credential_label="QQ 邮箱授权码",
        help_text="请先在 QQ 邮箱设置中开启 IMAP 服务，再填写生成的授权码。",
    ),
    MailboxProvider(
        key="gmail_oauth",
        display_name="Gmail / Google Workspace",
        imap_host="imap.gmail.com",
        authentication_mode="oauth2",
        credential_label="Google 授权",
        help_text="通过 Google 登录授权，不收集或保存 Google 登录密码。",
    ),
    MailboxProvider(
        key="microsoft_oauth",
        display_name="Microsoft 365 / Outlook",
        imap_host="outlook.office365.com",
        authentication_mode="oauth2",
        credential_label="Microsoft 授权",
        help_text="通过 Microsoft 登录授权，不收集或保存 Microsoft 登录密码。",
    ),
    MailboxProvider(
        key=GENERIC_IMAP_PROVIDER_KEY,
        display_name="通用 IMAP 邮箱",
        imap_host=None,
        authentication_mode="app_password",
        credential_label="专用授权码或客户端密码",
        help_text=(
            "填写邮箱服务商提供的 IMAP 服务器域名。系统只接受 SSL/TLS 的 993 "
            "端口，并在保存和同步时校验公网地址与证书。"
        ),
        allows_custom_endpoint=True,
    ),
)

_PROVIDERS_BY_KEY = {provider.key: provider for provider in _PROVIDERS}


def all_mailbox_providers() -> tuple[MailboxProvider, ...]:
    """Return reviewed providers in the deliberate UI presentation order."""

    return _PROVIDERS


def mailbox_provider_by_key(key: str) -> MailboxProvider:
    provider = _PROVIDERS_BY_KEY.get(key.strip())
    if provider is None:
        raise MailboxProviderError("mailbox_provider_not_supported")
    return provider


def known_mailbox_provider(
    *,
    host: str,
    port: int,
) -> MailboxProvider | None:
    """Infer a reviewed provider for old configurations without trusting it."""

    normalized_host = host.strip().rstrip(".").casefold()
    return next(
        (
            provider
            for provider in _PROVIDERS
            if provider.imap_host == normalized_host and provider.imap_port == port
        ),
        None,
    )


def provider_endpoint_is_enabled(
    settings: "AppSettings",
    provider: MailboxProvider,
) -> bool:
    """Check whether this provider can be connected in the deployment.

    Fixed providers must appear in the deployment allowlist.  Generic IMAP
    has no static host to preflight and is instead validated when the admin
    supplies its domain and whenever a worker reconnects.
    """

    if provider.allows_custom_endpoint:
        # A generic connection has no deployment-owned host to preflight.
        # The submitted hostname is checked by the transport when it is bound
        # and again on every later worker connection.
        return True
    assert provider.imap_host is not None
    try:
        validate_imap_endpoint(
            settings,
            host=provider.imap_host,
            port=provider.imap_port,
        )
    except MailboxImapTransportError:
        return False
    return True


def provider_oauth_is_configured(
    settings: "AppSettings",
    provider: MailboxProvider,
) -> bool:
    """Return whether this deployment has the server-side OAuth client."""

    if provider.authentication_mode != "oauth2":
        return True
    if provider.key == "gmail_oauth":
        return bool(
            settings.mailbox_google_oauth_client_id
            and settings.mailbox_google_oauth_client_secret
            and settings.mailbox_google_oauth_redirect_uri
        )
    if provider.key == "microsoft_oauth":
        return bool(
            settings.mailbox_microsoft_oauth_client_id
            and settings.mailbox_microsoft_oauth_client_secret
            and settings.mailbox_microsoft_oauth_redirect_uri
        )
    return False


def provider_is_available(
    settings: "AppSettings",
    provider: MailboxProvider,
) -> bool:
    """A provider must pass both network and authorization prerequisites."""

    return provider_endpoint_is_enabled(settings, provider) and provider_oauth_is_configured(
        settings,
        provider,
    )


def resolved_provider_key(
    *,
    configured_key: str | None,
    host: str,
    port: int,
) -> str:
    """Prefer a persisted key, otherwise infer a safe legacy presentation key."""

    if configured_key and configured_key != "legacy_imap":
        return configured_key
    provider = known_mailbox_provider(host=host, port=port)
    return provider.key if provider is not None else "legacy_imap"


__all__ = [
    "MailboxAuthenticationMode",
    "GENERIC_IMAP_PROVIDER_KEY",
    "MailboxProvider",
    "MailboxProviderError",
    "all_mailbox_providers",
    "known_mailbox_provider",
    "mailbox_provider_by_key",
    "provider_endpoint_is_enabled",
    "provider_is_available",
    "provider_oauth_is_configured",
    "resolved_provider_key",
]
