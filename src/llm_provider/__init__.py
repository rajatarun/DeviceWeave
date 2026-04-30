"""
LLM provider registry.

LLM_PROVIDER env var controls the backend:

    auto     (default) — probe outbound internet connectivity with a 2-second
                         TCP connect to 1.1.1.1:443 (Cloudflare DNS over HTTPS):
                           reachable    → Bedrock (Claude Haiku via cross-region inference)
                           unreachable  → Gemini  (API key from Secrets Manager gemini/api_key)
             The probe result is cached for 60 s to avoid a socket call on
             every LLM invocation.

    bedrock  — always use Bedrock regardless of NatInstance state
    gemini   — always use Gemini regardless of NatInstance state
    ollama   — always use local Ollama (dev/offline use)

Additional env vars:
    LLM_MODEL_ID      Bedrock cross-region inference profile  default: Claude Haiku 4.5
    GEMINI_MODEL      Gemini model name                       default: gemini-2.5-flash
    OLLAMA_BASE_URL   Ollama server URL                       default: http://localhost:11434
    OLLAMA_MODEL      Ollama model name                       default: mistral
"""

import logging
import os
import socket
import time
from typing import Optional

from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_GEMINI_SECRET_NAME = os.environ.get("GEMINI_SECRET_NAME", "gemini/api_key")
_NAT_CHECK_TTL = 60.0  # seconds
_NAT_PROBE_HOST = "1.1.1.1"
_NAT_PROBE_PORT = 443
_NAT_PROBE_TIMEOUT = 2.0  # seconds

# Per-provider singletons — created lazily, never reset.
_bedrock: Optional[BaseLLMProvider] = None
_gemini: Optional[BaseLLMProvider] = None
_ollama: Optional[BaseLLMProvider] = None

# NAT check cache
_nat_check_ts: float = 0.0
_nat_running: Optional[bool] = None


def _is_nat_running() -> bool:
    """Return True if outbound internet is reachable (NatInstance is up).

    Uses a raw TCP connect to 1.1.1.1:443 with a 2-second timeout — much
    faster and simpler than a boto3 describe_instances call, and guaranteed
    to fail within the timeout even when the VPC has no outbound route.
    Result is cached for _NAT_CHECK_TTL seconds.
    """
    global _nat_check_ts, _nat_running

    now = time.monotonic()
    if _nat_running is not None and (now - _nat_check_ts) < _NAT_CHECK_TTL:
        return _nat_running

    try:
        sock = socket.create_connection(
            (_NAT_PROBE_HOST, _NAT_PROBE_PORT),
            timeout=_NAT_PROBE_TIMEOUT,
        )
        sock.close()
        result = True
    except OSError:
        result = False

    _nat_check_ts = now
    _nat_running = result
    logger.info(
        "Internet probe %s:%s → %s (cached for %ds)",
        _NAT_PROBE_HOST,
        _NAT_PROBE_PORT,
        "reachable" if result else "unreachable",
        int(_NAT_CHECK_TTL),
    )
    return result


def _get_bedrock() -> BaseLLMProvider:
    global _bedrock
    if _bedrock is None:
        from llm_provider.bedrock import BedrockLLMProvider
        model_id = os.environ.get("LLM_MODEL_ID", _DEFAULT_BEDROCK_MODEL)
        region = os.environ.get("AWS_REGION", "us-east-1")
        _bedrock = BedrockLLMProvider(model_id=model_id, region=region)
        logger.info("Bedrock provider initialised: %s", _bedrock.model_id)
    return _bedrock


def _get_gemini() -> BaseLLMProvider:
    global _gemini
    if _gemini is None:
        from llm_provider.gemini import GeminiLLMProvider
        _gemini = GeminiLLMProvider(secret_name=_GEMINI_SECRET_NAME)
        logger.info("Gemini provider initialised: %s", _gemini.model_id)
    return _gemini


def _get_ollama() -> BaseLLMProvider:
    global _ollama
    if _ollama is None:
        from llm_provider.ollama import OllamaLLMProvider
        model = os.environ.get("OLLAMA_MODEL", "mistral")
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        _ollama = OllamaLLMProvider(model=model, base_url=base_url)
        logger.info("Ollama provider initialised: %s", _ollama.model_id)
    return _ollama


def get_llm_provider() -> BaseLLMProvider:
    """Return the appropriate LLM provider based on LLM_PROVIDER env var."""
    provider_type = os.environ.get("LLM_PROVIDER", "auto").lower()

    if provider_type == "auto":
        return _get_bedrock() if _is_nat_running() else _get_gemini()

    if provider_type == "bedrock":
        return _get_bedrock()

    if provider_type == "gemini":
        return _get_gemini()

    if provider_type == "ollama":
        return _get_ollama()

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider_type!r}. Supported: auto, bedrock, gemini, ollama"
    )
