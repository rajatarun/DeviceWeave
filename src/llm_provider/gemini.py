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

from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"

_api_key_cache: Optional[str] = None


def _load_api_key(secret_name: str) -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    import boto3
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
    _api_key_cache = json.loads(resp["SecretString"])["key"]
    logger.info("Gemini API key loaded from Secrets Manager (%s).", secret_name)
    return _api_key_cache


class GeminiLLMProvider(BaseLLMProvider):
    """
    Calls the Gemini generateContent endpoint with a system instruction and
    a single user turn.  Compatible with all gemini-2.x and gemini-1.5 models.
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
            "generationConfig": {"maxOutputTokens": max_tokens},
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
            text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                f"Unexpected Gemini response shape: {json.dumps(payload)[:200]}"
            ) from exc

        logger.debug("Gemini (%s) response: %d chars", self._model, len(text))
        return text
