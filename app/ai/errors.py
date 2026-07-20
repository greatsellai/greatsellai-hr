"""Stable provider-independent errors emitted by AI protocol adapters."""

from __future__ import annotations

from enum import StrEnum


class ProviderErrorCategory(StrEnum):
    """Categories used by routing, retry, and user-facing error mapping."""

    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    NETWORK = "network"
    PROVIDER_5XX = "provider_5xx"
    TRUNCATED = "truncated"
    STRUCTURED_INVALID = "structured_invalid"
    POLICY_BLOCKED = "policy_blocked"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONFIGURATION = "configuration"


_FALLBACK_ELIGIBLE_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.RATE_LIMITED,
        ProviderErrorCategory.QUOTA_EXHAUSTED,
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.NETWORK,
        ProviderErrorCategory.PROVIDER_5XX,
    }
)


class ProviderError(RuntimeError):
    """A sanitized adapter failure suitable for the gateway ledger.

    Raw response bodies, credentials, prompts, and provider-specific error
    messages are deliberately not retained on this exception.
    """

    def __init__(
        self,
        category: ProviderErrorCategory,
        *,
        retryable: bool | None = None,
        may_have_billed: bool = False,
        http_status_code: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        self.category = ProviderErrorCategory(category)
        self.retryable = (
            self.category in _FALLBACK_ELIGIBLE_CATEGORIES
            if retryable is None
            else retryable
        )
        self.may_have_billed = may_have_billed
        self.http_status_code = http_status_code
        self.provider_request_id = provider_request_id
        super().__init__(f"ai_provider_{self.category.value}")

    @property
    def fallback_eligible(self) -> bool:
        return self.retryable and self.category in _FALLBACK_ELIGIBLE_CATEGORIES


class ProviderConfigurationError(ProviderError):
    def __init__(self) -> None:
        super().__init__(ProviderErrorCategory.CONFIGURATION, retryable=False)


class ProviderResponseError(ProviderError):
    def __init__(
        self,
        *,
        category: ProviderErrorCategory = ProviderErrorCategory.STRUCTURED_INVALID,
        http_status_code: int | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        if category not in {
            ProviderErrorCategory.STRUCTURED_INVALID,
            ProviderErrorCategory.TRUNCATED,
        }:
            raise ValueError("provider_response_error_category_invalid")
        super().__init__(
            category,
            retryable=False,
            may_have_billed=True,
            http_status_code=http_status_code,
            provider_request_id=provider_request_id,
        )


__all__ = [
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderErrorCategory",
    "ProviderResponseError",
]
