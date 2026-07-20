"""Protocol adapters used by the AI gateway."""

from app.ai.adapters.base import CompletionAdapter
from app.ai.adapters.openai_compatible import OpenAICompatibleAdapter

__all__ = ["CompletionAdapter", "OpenAICompatibleAdapter"]
