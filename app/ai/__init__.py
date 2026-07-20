"""Vendor-neutral AI gateway contracts and protocol adapters.

This package intentionally contains no business prompts, candidate facts, or
provider-specific configuration.  Business services construct a
``CompletionRequest``; the gateway resolves the actual ``RouteTarget`` before
an adapter is allowed to make a network request.
"""

from app.ai.contracts import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    NormalizedUsage,
    RouteAuthentication,
    RouteTarget,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from app.ai.errors import ProviderError, ProviderErrorCategory

__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResult",
    "NormalizedUsage",
    "ProviderError",
    "ProviderErrorCategory",
    "RouteAuthentication",
    "RouteTarget",
    "ToolCall",
    "ToolChoice",
    "ToolDefinition",
]
