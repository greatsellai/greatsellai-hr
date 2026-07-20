"""Base contract for AI protocol adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.ai.contracts import CompletionRequest, CompletionResult, RouteTarget


class CompletionAdapter(ABC):
    """Turns a resolved route and a vendor-neutral request into a completion."""

    driver: str

    @abstractmethod
    def complete(
        self,
        request: CompletionRequest,
        route: RouteTarget,
    ) -> CompletionResult:
        """Execute one external request or raise ``ProviderError``."""


__all__ = ["CompletionAdapter"]
