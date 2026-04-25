"""
Vision intent source — ambient observation → device action.

Accepts structured observations from a camera or vision model and converts
them into the same IntentPayload the text pipeline uses. The downstream
resolution pipeline (cosine matching, LLM fallback, policy engine) is
completely unaware of the source.

Two accepted payload shapes:

Shape A — pre-structured (vision model already resolved device + action):
    {
        "source": "vision",
        "observation": {
            "device_query": "desk light",
            "action": "turn_on",
            "confidence": 0.87,
            "context": "Room dark, person seated at desk"
        }
    }

Shape B — raw description (LLM vision model output, fed into text pipeline):
    {
        "source": "vision",
        "description": "The room is dark and someone just sat at the desk.",
        "confidence": 0.79,
        "camera_id": "office-cam-01"
    }

In both cases the result is an IntentPayload whose raw_text feeds the
existing resolution pipeline unchanged.

Production path (not yet implemented here):
    Camera frame  →  Vision LLM (Claude Vision / LLaVA)
                  →  Structured observation or natural language description
                  →  VisionIntentSource.extract()
                  →  IntentPayload
                  →  DeviceWeave execution pipeline (unchanged)
"""

import logging
from typing import Any, Dict, Optional

from intent_sources.base import BaseIntentSource, IntentPayload

logger = logging.getLogger(__name__)


class VisionIntentSource(BaseIntentSource):

    def extract(self, body: Dict[str, Any]) -> Optional[IntentPayload]:
        # Shape B — raw natural-language description from a vision model
        description = (body.get("description") or "").strip()
        if description:
            return IntentPayload(
                raw_text=description,
                source="vision",
                confidence=float(body.get("confidence", 0.5)),
                metadata={
                    "camera_id": body.get("camera_id", ""),
                    "shape": "description",
                },
            )

        # Shape A — structured observation with device_query + action resolved
        obs = body.get("observation") or {}
        device_query = (obs.get("device_query") or "").strip()
        if not device_query:
            logger.warning("VisionIntentSource: no 'description' or 'observation.device_query'")
            return None

        action = obs.get("action", "turn_on")
        context = (obs.get("context") or "").strip()
        # Use the richer context text if available so the LLM resolver has
        # more signal; otherwise fall back to a minimal command-style string.
        raw_text = context if context else f"{action} {device_query}"

        return IntentPayload(
            raw_text=raw_text,
            source="vision",
            confidence=float(obs.get("confidence", 0.5)),
            metadata={
                "device_query": device_query,
                "action_hint": action,
                "context": context,
                "shape": "structured",
            },
        )
