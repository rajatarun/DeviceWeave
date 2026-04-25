"""
Amazon Bedrock LLM provider (Anthropic Claude via cross-region inference).

Default backend. Reads credentials from the Lambda execution role — no
explicit key management required.
"""

import json
import logging

from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)


class BedrockLLMProvider(BaseLLMProvider):

    def __init__(self, model_id: str, region: str = "us-east-1") -> None:
        self._model_id = model_id
        self._region = region

    @property
    def model_id(self) -> str:
        return self._model_id

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=self._region)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        })
        resp = client.invoke_model(modelId=self._model_id, body=body)
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()
        logger.debug("Bedrock (%s) response: %d chars", self._model_id, len(text))
        return text
