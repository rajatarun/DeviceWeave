"""
Tier 2 LLM resolver — contextual intent inference via Claude Haiku 4.5.

Called only when Tier 1 (TF cosine) fails to reach the confidence threshold.
Receives the full user query, current weather, and the complete device roster,
and asks Claude to infer which device(s) the user wants to control and what
action to perform.  Weather is used to select additional devices (e.g. hot +
humid → fan AND AC) and to rank ambiguous matches.

Successful resolutions are auto-learned by the caller so future identical
or similar queries hit Tier 1 without a Bedrock call.

Cost profile: ~100–200 tokens per call at Haiku 4.5 rates (~$0.0002/call).
"""

import json
import logging
from typing import Any, Dict, List, Optional

import weather_client

logger = logging.getLogger(__name__)

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = """\
You are a home automation assistant. Given a user's natural-language command, \
the current weather conditions, and a list of available smart home devices, \
determine which device(s) the user wants to control and what action to perform.

Rules:
- Every device_id MUST be copied exactly from the provided device list.
- Every action MUST be one of the listed capabilities for the chosen device.
- Use contextual clues: "going to kitchen" → kitchen light on; \
"getting warm" → fan on; "heading to bed" → bedroom light off.
- Use the weather data to select the right device(s) and action:
    hot or humid            → prefer fan and/or AC; avoid heaters
    cold                    → avoid fan/AC; prefer heaters
    overcast or dark hours  → lights more likely needed
    hot AND humid           → include BOTH fan and AC if both are available
- Return MULTIPLE devices when weather or context clearly implies several \
should be activated together (e.g. hot+humid → fan + AC; movie night → \
lights dim + TV on).  Only include devices where the action is genuinely \
warranted — do not pad the list.
- If you cannot determine with reasonable confidence, return confidence 0 \
and an empty devices list.

Return ONLY a raw JSON object — no markdown, no explanation:
{
  "devices": [
    {
      "device_id": "<exact id from list>",
      "device_name": "<device name>",
      "action": "<capability>",
      "params": {}
    }
  ],
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}"""


def llm_resolve(
    query: str,
    action_hint: Optional[str],
    devices: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Ask Claude Haiku 4.5 to resolve a query to one or more devices + actions.

    Returns a normalised dict:
        {
            "devices": [{"device_id", "device_name", "action", "params"}, ...],
            "confidence": float,
            "reasoning": str,
        }
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
    weather_line = f"\n{weather_client.summary_line()}"

    user_msg = (
        f'User command: "{query}"{hint_line}{weather_line}\n\n'
        f"Available devices:\n{device_lines}\n\n"
        f"Return the JSON object."
    )

    import boto3
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    })

    try:
        resp = client.invoke_model(modelId=_MODEL_ID, body=body)
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        raw = json.loads(text)
        result = _normalise(raw)

        logger.info(
            "LLM resolver — devices=%s confidence=%.2f reasoning=%r",
            [(d.get("device_id"), d.get("action")) for d in result["devices"]],
            result["confidence"],
            result["reasoning"],
        )
        return result
    except Exception as exc:
        logger.error("LLM resolver failed for %r: %s", query, exc, exc_info=True)
        return None


def _normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accept both the new devices-list format and the legacy single-device format.

    New:  {"devices": [...], "confidence": ..., "reasoning": ...}
    Old:  {"device_id": ..., "action": ..., "confidence": ..., ...}
    """
    if "devices" in raw:
        return {
            "devices": raw["devices"],
            "confidence": float(raw.get("confidence", 0)),
            "reasoning": raw.get("reasoning", ""),
        }
    # Legacy single-device format — wrap in a list
    return {
        "devices": [{
            "device_id": raw.get("device_id"),
            "device_name": raw.get("device_name", ""),
            "action": raw.get("action", ""),
            "params": raw.get("params") or {},
        }],
        "confidence": float(raw.get("confidence", 0)),
        "reasoning": raw.get("reasoning", ""),
    }
