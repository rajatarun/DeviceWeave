"""
Google Gemini LLM provider via the Generative Language REST API.

Fallback backend used when the NatInstance EC2 is not running.

API key loaded once from Secrets Manager:
    secret name: gemini/api_key
    secret key:  key

No extra dependencies — uses stdlib urllib throughout, same as the
Ollama provider.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from llm_provider.base import STOP_MAX_TOKENS, BaseLLMProvider

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-3.8-flash"

# Gemini's flash models think before they answer, and thinking tokens count
# against maxOutputTokens. A cap sized for the answer alone (the resolver asks
# for a few hundred tokens) could be spent on thinking, leaving a visible
# reply cut off a few dozen characters in. The cap is raised by this much; it
# is a limit, not a charge, so unused headroom costs nothing.
_THINKING_HEADROOM = int(os.environ.get("GEMINI_THINKING_HEADROOM", "2048"))

_api_key_cache: Optional[str] = None


def _load_api_key(secret_name: str) -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    import boto3
    from botocore.config import Config as BotocoreConfig

    sm_client = boto3.client(
        "secretsmanager",
        config=BotocoreConfig(
            connect_timeout=5,
            read_timeout=5,
            retries={"max_attempts": 1},
            use_dualstack_endpoint=True,
        ),
    )
    resp = sm_client.get_secret_value(SecretId=secret_name)
    _api_key_cache = json.loads(resp["SecretString"])["key"]
    logger.info("Gemini API key loaded from Secrets Manager (%s).", secret_name)
    return _api_key_cache


class GeminiLLMProvider(BaseLLMProvider):
    """
    Calls the Gemini generateContent endpoint with a system instruction and
    a single user turn.  Defaults to gemini-3.8-flash but is not hard-pinned
    to that model — any gemini-* model name can be passed through.
    """

    def __init__(
        self,
        secret_name: str = "gemini/api_key",
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._secret_name = secret_name
        self._model = os.environ.get("GEMINI_MODEL", model)

    @property
    def model_id(self) -> str:
        return f"gemini/{self._model}"

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        api_key = _load_api_key(self._secret_name)
        url = f"{_GEMINI_API_BASE}/{self._model}:generateContent?key={api_key}"

        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {"maxOutputTokens": max_tokens + _THINKING_HEADROOM},
        }).encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            raise RuntimeError(
                f"Gemini API error {exc.code}: {body_bytes.decode(errors='replace')}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini unreachable: {exc}") from exc

        try:
            candidate = payload["candidates"][0]
            finish = candidate.get("finishReason")
            self.last_stop_reason = STOP_MAX_TOKENS if finish == "MAX_TOKENS" else finish
            parts = (candidate.get("content") or {}).get("parts") or []
            if not parts and finish == "MAX_TOKENS":
                # Thinking used the whole budget and nothing visible came
                # back. That is a truncated reply, which callers handle; it
                # is not a malformed response.
                thoughts = (payload.get("usageMetadata") or {}).get("thoughtsTokenCount")
                logger.warning("Gemini (%s) spent its output budget thinking (%s thought "
                               "tokens) and returned no text", self._model, thoughts)
                return ""
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                f"Unexpected Gemini response shape: {json.dumps(payload)[:200]}"
            ) from exc

        logger.debug("Gemini (%s) response: %d chars", self._model, len(text))
        return text
