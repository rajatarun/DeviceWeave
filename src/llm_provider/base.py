"""
Abstract LLM provider interface.

All LLM calls in DeviceWeave — device resolution, policy compilation,
phrase generation — go through this interface. Swap the backend by
setting LLM_PROVIDER=bedrock|ollama (default: bedrock).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class LLMResult:
    """One model call. ``usage`` is the token object the API returned, if any."""

    text: str
    usage: Optional[Dict[str, Any]] = None


class BaseLLMProvider(ABC):

    @abstractmethod
    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512,
               temperature: Optional[float] = None) -> str:
        """Send a prompt and return the raw text response. Raises on failure.

        ``temperature`` is omitted from the request when it is ``None``, which
        is the historical behaviour. Pass a number to set it.
        """

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Human-readable model identifier used in logs."""
