"""
Local Ollama LLM provider.

Runs fully offline — no cloud dependency, no egress cost. Point it at any
model supported by Ollama (mistral, llama3, phi3, etc.).

Configuration (env vars):
    OLLAMA_BASE_URL   Base URL of the Ollama server   default: http://localhost:11434
    OLLAMA_MODEL      Model name                       default: mistral

To switch from Bedrock to Ollama:
    LLM_PROVIDER=ollama
    OLLAMA_BASE_URL=http://192.168.1.50:11434   # e.g. another machine on LAN
    OLLAMA_MODEL=mistral

No code changes required anywhere in the system.
"""

import json
import logging
import urllib.error
import urllib.request

from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)


class OllamaLLMProvider(BaseLLMProvider):

    def __init__(self, model: str = "mistral", base_url: str = "http://localhost:11434") -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def model_id(self) -> str:
        return f"ollama/{self._model}"

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            text = body["message"]["content"].strip()
            logger.debug("Ollama (%s) response: %d chars", self._model, len(text))
            return text
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Ollama unreachable at {self._base_url} — is the server running? ({exc})"
            ) from exc
