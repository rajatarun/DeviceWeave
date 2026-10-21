"""
Amazon Bedrock LLM provider.

The model comes from ModelWeave's "reasoning" role pin (``modelweave_roles``);
the model id this provider is built with is only the fallback when no pin can
be read. The call is the Converse API rather than ``invoke_model``: the old
request body was Anthropic's own message format, so a pin naming Nova or
Llama would have been sent a body it cannot read. Reads credentials from the
Lambda execution role.
"""

import logging

import bedrock_cost
import modelweave_roles
from llm_provider.base import BaseLLMProvider

logger = logging.getLogger(__name__)

MODEL_ROLE = "reasoning"


class BedrockLLMProvider(BaseLLMProvider):

    def __init__(self, model_id: str, region: str = "us-east-1") -> None:
        self._model_id = model_id
        self._region = region

    @property
    def model_id(self) -> str:
        """The model a call would use now: the role pin, else the fallback."""
        return modelweave_roles.model_for(MODEL_ROLE, self._model_id)

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        import boto3

        # Billed to DeviceWeave through its tagged inference profiles (bedrock_cost.py).
        client = bedrock_cost.attribute(boto3.client("bedrock-runtime", region_name=self._region))
        resp, used = modelweave_roles.converse(
            client,
            MODEL_ROLE,
            self._model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()
        logger.debug("Bedrock (%s) response: %d chars", used, len(text))
        return text
