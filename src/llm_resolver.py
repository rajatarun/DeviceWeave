"""
Tier 2 LLM resolver — contextual intent inference via Claude Haiku 4.5.

Called only when Tier 1 (TF cosine) fails to reach the confidence threshold.
Receives the full user query and the complete device roster, and asks Claude
to infer which device the user wants to control and what action to perform.

Successful resolutions are auto-learned by the caller so future identical
or similar queries hit Tier 1 without a Bedrock call.

Cost profile: ~100–150 tokens per call at Haiku 4.5 rates (~$0.0002/call).
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = """\
You are a home automation assistant. Given a user's natural-language command \
and a list of available smart home devices, determine which device the user \
wants to control and what action to perform.

Rules:
- device_id MUST be copied exactly from the provided device list.
- action MUST be one of the listed capabilities for the chosen device.
- Use contextual clues: "going to kitchen" implies turning on the kitchen light; \
"getting warm" implies a fan; "heading to bed" implies bedroom light off, etc.
- If you cannot determine with reasonable confidence, return confidence 0.

Return ONLY a raw JSON object — no markdown, no explanation:
{
  "device_id": "<exact id from list or null>",
  "device_name": "<device name>",
  "action": "<capability>",
  "params": {},
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}"""


def llm_resolve(
    query: str,
    action_hint: Optional[str],
    devices: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Ask Claude Haiku 4.5 to resolve a query to a device + action.

    Returns a dict with device_id, action, params, confidence, reasoning,
    or None if the model call fails entirely.
    """
    if not devices:
        logger.warning("LLM resolver called with empty device list.")
        return None

    device_lines = "\n".join(
        f"  id={d['id']} | name={d['name']} | type={d['device_type']} "
        f"| capabilities={', '.join(d.get('capabilities', []))}"
        for d in devices
    )

    hint_line = f"\nRule-based parser action hint: {action_hint}" if action_hint else ""

    user_msg = (
        f'User command: "{query}"{hint_line}\n\n'
        f"Available devices:\n{device_lines}\n\n"
        f"Return the JSON object."
    )

    import boto3
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 256,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    })

    try:
        resp = client.invoke_model(modelId=_MODEL_ID, body=body)
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(text)
        logger.info(
            "LLM resolver — device=%r action=%r confidence=%.2f reasoning=%r",
            result.get("device_name"),
            result.get("action"),
            result.get("confidence", 0),
            result.get("reasoning"),
        )
        return result
    except Exception as exc:
        logger.error("LLM resolver failed for %r: %s", query, exc, exc_info=True)
        return None
