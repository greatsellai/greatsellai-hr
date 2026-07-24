"""OAuth 2.0 helpers for reviewed IMAP mailbox providers.

Only authorization-code and refresh exchanges are implemented here.  Browser
state, workspace binding and encrypted persistence stay in the mailbox domain
service so neither raw tokens nor client secrets reach API responses.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.mailbox_provider_catalog import (
    MailboxProvider,
    MailboxProviderError,
    mailbox_provider_by_key,
    provider_oauth_is_configured,
)

if TYPE_CHECKING:
    from app.config import AppSettings


class MailboxOAuthError(RuntimeError):
    """A stable OAuth error that is safe to show in the recruitment UI."""


@dataclass(frozen=True)
class OAuthClientConfiguration:
    provider: MailboxProvider
    client_id: str
    client_secret: str
    redirect_uri: str
    authorization_endpoint: str
    token_endpoint: str
    scopes: tuple[str, ...]
    authorization_extra_parameters: tuple[tuple[str, str], ...] = ()


def _oauth_client_configuration(
    settings: "AppSettings",
    *,
    provider_key: str,
) -> OAuthClientConfiguration:
    try:
        provider = mailbox_provider_by_key(provider_key)
    except MailboxProviderError as exc:
        raise MailboxOAuthError(str(exc)) from exc
    if provider.authentication_mode != "oauth2":
        raise MailboxOAuthError("mailbox_provider_oauth_not_supported")
    if not provider_oauth_is_configured(settings, provider):
        raise MailboxOAuthError("mailbox_oauth_not_configured")

    if provider.key == "gmail_oauth":
        assert settings.mailbox_google_oauth_client_id is not None
        assert settings.mailbox_google_oauth_client_secret is not None
        assert settings.mailbox_google_oauth_redirect_uri is not None
        return OAuthClientConfiguration(
            provider=provider,
            client_id=settings.mailbox_google_oauth_client_id,
            client_secret=settings.mailbox_google_oauth_client_secret,
            redirect_uri=settings.mailbox_google_oauth_redirect_uri,
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            # IMAP XOAUTH2 needs Gmail's mail scope.  We do not use an OAuth
            # identity/profile response, so requesting ``openid`` or ``email``
            # would widen consent without serving the mailbox import flow.
            scopes=("https://mail.google.com/",),
            authorization_extra_parameters=(
                ("access_type", "offline"),
                ("prompt", "consent"),
            ),
        )
    if provider.key == "microsoft_oauth":
        assert settings.mailbox_microsoft_oauth_client_id is not None
        assert settings.mailbox_microsoft_oauth_client_secret is not None
        assert settings.mailbox_microsoft_oauth_redirect_uri is not None
        return OAuthClientConfiguration(
            provider=provider,
            client_id=settings.mailbox_microsoft_oauth_client_id,
            client_secret=settings.mailbox_microsoft_oauth_client_secret,
            redirect_uri=settings.mailbox_microsoft_oauth_redirect_uri,
            authorization_endpoint=(
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
            ),
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scopes=(
                "offline_access",
                "https://outlook.office.com/IMAP.AccessAsUser.All",
            ),
        )
    raise MailboxOAuthError("mailbox_provider_oauth_not_supported")


def create_oauth_state() -> str:
    """Return an opaque state value; only its SHA-256 digest is persisted."""

    return secrets.token_urlsafe(32)


def create_pkce_code_verifier() -> str:
    """Generate an RFC 7636 high-entropy verifier kept server-side only."""

    return secrets.token_urlsafe(64)


def _pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(
    settings: "AppSettings",
    *,
    provider_key: str,
    state: str,
    code_verifier: str,
) -> str:
    """Build an OAuth authorization URL without exposing a client secret."""

    configuration = _oauth_client_configuration(settings, provider_key=provider_key)
    parameters: list[tuple[str, str]] = [
        ("response_type", "code"),
        ("client_id", configuration.client_id),
        ("redirect_uri", configuration.redirect_uri),
        ("scope", " ".join(configuration.scopes)),
        ("state", state),
        ("code_challenge", _pkce_code_challenge(code_verifier)),
        ("code_challenge_method", "S256"),
    ]
    parameters.extend(configuration.authorization_extra_parameters)
    return f"{configuration.authorization_endpoint}?{urlencode(parameters)}"


def _post_form_json(
    *,
    url: str,
    values: dict[str, str],
    timeout_seconds: int,
) -> dict[str, object]:
    request = Request(
        url,
        data=urlencode(values).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            # The endpoint is a fixed, code-owned OAuth token endpoint. No
            # workspace value influences this URL.
            payload = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise MailboxOAuthError("mailbox_oauth_token_exchange_failed") from exc
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailboxOAuthError("mailbox_oauth_token_exchange_failed") from exc
    if not isinstance(decoded, dict):
        raise MailboxOAuthError("mailbox_oauth_token_exchange_failed")
    return decoded


def exchange_authorization_code(
    settings: "AppSettings",
    *,
    provider_key: str,
    code: str,
    code_verifier: str,
) -> str:
    """Exchange a one-time code and return only its refresh token."""

    if not code or len(code) > 8192:
        raise MailboxOAuthError("mailbox_oauth_callback_invalid")
    configuration = _oauth_client_configuration(settings, provider_key=provider_key)
    response = _post_form_json(
        url=configuration.token_endpoint,
        values={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": configuration.client_id,
            "client_secret": configuration.client_secret,
            "redirect_uri": configuration.redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout_seconds=settings.mailbox_oauth_http_timeout_seconds,
    )
    refresh_token = response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip() or len(refresh_token) > 8192:
        raise MailboxOAuthError("mailbox_oauth_refresh_token_missing")
    return refresh_token.strip()


def refresh_access_token(
    settings: "AppSettings",
    *,
    provider_key: str,
    refresh_token: str,
) -> str:
    """Exchange encrypted-at-rest refresh material for an in-memory token."""

    if not refresh_token or len(refresh_token) > 8192:
        raise MailboxOAuthError("mailbox_oauth_reauthorization_required")
    configuration = _oauth_client_configuration(settings, provider_key=provider_key)
    response = _post_form_json(
        url=configuration.token_endpoint,
        values={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": configuration.client_id,
            "client_secret": configuration.client_secret,
        },
        timeout_seconds=settings.mailbox_oauth_http_timeout_seconds,
    )
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip() or len(access_token) > 16384:
        raise MailboxOAuthError("mailbox_oauth_reauthorization_required")
    return access_token.strip()


__all__ = [
    "MailboxOAuthError",
    "authorization_url",
    "create_oauth_state",
    "create_pkce_code_verifier",
    "exchange_authorization_code",
    "refresh_access_token",
]
