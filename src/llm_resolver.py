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
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import weather_client
from llm_provider import get_llm_provider
from llm_provider.base import STOP_MAX_TOKENS

logger = logging.getLogger(__name__)

_CENTRAL_TZ = ZoneInfo("America/Chicago")
_MAX_OUTPUT_TOKENS = 4096
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

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
- Treat "dark hours" as local evening/night based on the supplied Central Time \
timestamp rather than UTC.
- If you cannot determine with reasonable confidence, return confidence 0 \
and an empty devices list.

Return ONLY a raw JSON object — no markdown, no explanation:
{
  "devices": [
    {"device_id": "<exact id from list>", "action": "<capability>", "params": {}}
  ],
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}
Omit params entirely when empty — do not write "params": {}."""


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
    central_now = datetime.now(_CENTRAL_TZ)
    central_time_line = (
        f"\nCurrent local time (America/Chicago): "
        f"{central_now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    weather_line = f"\n{weather_client.summary_line()}"

    user_msg = (
        f'User command: "{query}"{hint_line}{central_time_line}{weather_line}\n\n'
        f"Available devices:\n{device_lines}\n\n"
        f"Return the JSON object."
    )

    max_tokens = _output_budget(devices)

    try:
        llm = get_llm_provider()
        logger.debug("LLM resolver using provider: %s", llm.model_id)
        text = llm.invoke(_SYSTEM_PROMPT, user_msg, max_tokens=max_tokens)

        if getattr(llm, "last_stop_reason", None) == STOP_MAX_TOKENS:
            # The JSON is cut off; parsing it only produces a decode traceback.
            logger.error(
                "LLM resolver output truncated at max_tokens=%d (%d devices, %d chars) for %r",
                max_tokens, len(devices), len(text), query,
            )
            return None

        raw = json.loads(_extract_json(text))
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


def _output_budget(devices: List[Dict[str, Any]]) -> int:
    """
    Output tokens for a step per device ("for every device ..." returns all).

    A step is ~15 tokens of JSON plus the id, which runs to ~25 tokens for a
    40-hex Kasa id, so budget the ids by length (~3 chars a token). The old
    flat 20 per device cut those requests off mid-string. Capped well under
    every pinnable model's output limit.
    """
    per_device = sum(20 + len(str(d.get("id", ""))) // 3 for d in devices)
    return min(_MAX_OUTPUT_TOKENS, max(256, per_device + 200))


def _extract_json(text: str) -> str:
    """Strip a ``` fence or prose around the object the prompt asked for."""
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text.strip()


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
            "action": raw.get("action", ""),
            "params": raw.get("params") or {},
        }],
        "confidence": float(raw.get("confidence", 0)),
        "reasoning": raw.get("reasoning", ""),
    }
