"""
Intent source registry.

Dispatches incoming request bodies to the correct BaseIntentSource
based on the "source" field (default: "text").

Supported sources:
    text    {"command": "turn on the fan"}                   — explicit user command
    vision  {"source":"vision", "observation": {...}}        — camera / vision model
    vision  {"source":"vision", "description": "..."}       — raw vision model output

Adding a new source (e.g. "sensor"):
    1. Create src/intent_sources/sensor.py implementing BaseIntentSource
    2. Register it in _REGISTRY below
    No other changes required.
"""

from typing import Any, Dict, Optional

from intent_sources.base import IntentPayload
from intent_sources.text import TextIntentSource
from intent_sources.vision import VisionIntentSource

_REGISTRY = {
    "text":   TextIntentSource(),
    "vision": VisionIntentSource(),
}


def get_intent_from_payload(body: Dict[str, Any]) -> Optional[IntentPayload]:
    """
    Extract a normalised IntentPayload from a raw API request body.
    Returns None if the body cannot be interpreted as any known intent source.
    """
    source_type = (body.get("source") or "text").lower()
    source = _REGISTRY.get(source_type)
    if source is None:
        # Unknown source — try text as a safe fallback
        source = _REGISTRY["text"]
    return source.extract(body)
