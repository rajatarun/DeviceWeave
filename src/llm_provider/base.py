"""
Abstract LLM provider interface.

All LLM calls in DeviceWeave — device resolution, policy compilation,
phrase generation — go through this interface. Swap the backend by
setting LLM_PROVIDER=bedrock|ollama (default: bedrock).
"""

from abc import ABC, abstractmethod


# Normalised stop reason a provider records when its output hit max_tokens.
STOP_MAX_TOKENS = "max_tokens"


class BaseLLMProvider(ABC):

    # Why the last invoke() stopped, normalised to STOP_MAX_TOKENS when the
    # output was cut at the token budget; None when the backend did not say.
    last_stop_reason = None

    @abstractmethod
    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        """Send a prompt and return the raw text response. Raises on failure."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Human-readable model identifier used in logs."""
