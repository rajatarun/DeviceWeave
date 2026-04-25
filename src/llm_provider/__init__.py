"""
LLM provider registry.

Returns a singleton BaseLLMProvider configured by env vars:

    LLM_PROVIDER    bedrock | ollama          default: bedrock
    LLM_MODEL_ID    Bedrock model/profile ID  default: Claude Haiku 4.5
    OLLAMA_BASE_URL Ollama server URL          default: http://localhost:11434
    OLLAMA_MODEL    Ollama model name          default: mistral

Swapping from Bedrock to a local Ollama instance requires only env var
changes — no code changes in any caller.
"""

import logging
import os
from typing import Optional

from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_singleton: Optional[BaseLLMProvider] = None


def get_llm_provider() -> BaseLLMProvider:
    """Return the process-level LLM provider singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton

    provider_type = os.environ.get("LLM_PROVIDER", "bedrock").lower()

    if provider_type == "bedrock":
        from llm_provider.bedrock import BedrockLLMProvider
        model_id = os.environ.get("LLM_MODEL_ID", _DEFAULT_BEDROCK_MODEL)
        region = os.environ.get("AWS_REGION", "us-east-1")
        _singleton = BedrockLLMProvider(model_id=model_id, region=region)

    elif provider_type == "ollama":
        from llm_provider.ollama import OllamaLLMProvider
        model = os.environ.get("OLLAMA_MODEL", "mistral")
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        _singleton = OllamaLLMProvider(model=model, base_url=base_url)

    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider_type!r}. Supported: bedrock, ollama"
        )

    logger.info("LLM provider initialised: %s", _singleton.model_id)
    return _singleton
