"""
Amazon Bedrock LLM provider (Anthropic Claude via cross-region inference).

Default backend. Reads credentials from the Lambda execution role — no
explicit key management required.
"""

import json
import logging
import threading
from typing import Optional

from llm_provider.base import BaseLLMProvider, LLMResult

logger = logging.getLogger(__name__)


class BedrockLLMProvider(BaseLLMProvider):

    provider_name = "bedrock"

    def __init__(self, model_id: str, region: str = "us-east-1", *,
                 reuse_client: bool = False) -> None:
        self._model_id = model_id
        self._region = region
        # Production authoring builds a client per call. The live benchmark
        # sets reuse_client so a thread pool shares one thread-safe client.
        self._reuse_client = reuse_client
        self._client = None
        self._client_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_id

    def _runtime_client(self):
        import boto3

        if not self._reuse_client:
            return boto3.client("bedrock-runtime", region_name=self._region)
        with self._client_lock:
            if self._client is None:
                self._client = boto3.client("bedrock-runtime", region_name=self._region)
            return self._client

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512,
               temperature: Optional[float] = None) -> str:
        return self.invoke_result(
            system_prompt, user_message, max_tokens=max_tokens, temperature=temperature,
        ).text

    def invoke_result(self, system_prompt: str, user_message: str, max_tokens: int = 512,
                      temperature: Optional[float] = None) -> LLMResult:
        body_obj = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }
        # Omit temperature unless the caller set it, so the authoring path
        # keeps sending the same request body it always has.
        if temperature is not None:
            body_obj["temperature"] = temperature
        resp = self._runtime_client().invoke_model(
            modelId=self._model_id, body=json.dumps(body_obj),
        )
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = None
        logger.debug("Bedrock (%s) response: %d chars", self._model_id, len(text))
        return LLMResult(text=text, usage=usage)
