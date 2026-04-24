"""
Behavior engine — context-aware device usage scoring.

Scores how likely a given device+action is at the current moment by comparing
the context (hour, day-of-week) against recorded historical usage patterns
stored in Memgraph.

Device type weights encode physical intuitions:
    fan   → frequency-heavy (fans are reactive to warmth; correlate with usage density)
    light → time-heavy (lights follow circadian patterns more than raw frequency)
    ac    → frequency-heavy (high temperature events drive AC more than clock time)

When Memgraph has no history for a device, the score is 0.5 (neutral — neither
boosts nor suppresses the cosine match).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import graph_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device type weight profiles
# ---------------------------------------------------------------------------
# Each profile weights two orthogonal signals:
#   time_weight      — how much the hour-of-day pattern matters
#   frequency_weight — how much raw historical frequency matters
#
# The two always sum to 1.0.

DEVICE_MODELS: Dict[str, Dict[str, float]] = {
    "light": {
        "time_weight": 0.6,       # lights follow strong circadian patterns
        "frequency_weight": 0.4,
        "hour_window": 3,         # ±3 h tolerance (morning / evening ramp)
    },
    "fan": {
        "time_weight": 0.3,       # fans are driven more by temperature/usage
        "frequency_weight": 0.7,
        "hour_window": 2,
    },
    "ac": {
        "time_weight": 0.2,
        "frequency_weight": 0.8,
        "hour_window": 2,
    },
    "switch": {
        "time_weight": 0.5,
        "frequency_weight": 0.5,
        "hour_window": 3,
    },
    "default": {
        "time_weight": 0.5,
        "frequency_weight": 0.5,
        "hour_window": 2,
    },
}

_NEUTRAL_SCORE = 0.5  # returned when no history exists


# ---------------------------------------------------------------------------
# Device type inference from name + capabilities
# ---------------------------------------------------------------------------

_NAME_HINTS: Dict[str, str] = {
    "fan": "fan",
    "ventilation": "fan",
    "cooling": "fan",
    "light": "light",
    "lamp": "light",
    "bulb": "light",
    "ceiling": "light",
    "strip": "light",
    "led": "light",
    "ac": "ac",
    "air": "ac",
    "conditioner": "ac",
    "heater": "ac",
    "thermostat": "ac",
    "switch": "switch",
    "plug": "switch",
    "outlet": "switch",
    "socket": "switch",
}


def infer_device_class(device: Dict[str, Any]) -> str:
    """
    Tri-layer device type classifier → returns a DEVICE_MODELS key.

    Layer 1 — capability truth:  if the device has brightness/color it is a light.
    Layer 2 — name heuristic:    keyword scan of the device name.
    Layer 3 — device_type field: fallback string match on the registered type.
    """
    caps = set(device.get("capabilities", []))
    name = device.get("name", "").lower()
    dtype = device.get("device_type", "").lower()

    # Layer 1 — capability authority
    if "set_brightness" in caps or "set_color" in caps:
        return "light"

    # Layer 2 — name keywords (longest match wins)
    for keyword, device_class in _NAME_HINTS.items():
        if keyword in name:
            return device_class

    # Layer 3 — device_type string
    for keyword, device_class in _NAME_HINTS.items():
        if keyword in dtype:
            return device_class

    return "default"


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def current_context() -> Dict[str, Any]:
    """Return the current temporal context for scoring."""
    now = datetime.now(timezone.utc)
    return {
        "hour": now.hour,
        "day_of_week": now.weekday(),  # 0=Mon … 6=Sun
        "is_weekend": now.weekday() >= 5,
    }


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def score(
    device: Dict[str, Any],
    action: str,
    context: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Compute a behavior score in [0, 1] for (device, action) at the given context.

    Returns 0.5 (neutral) when:
    - Memgraph is unavailable
    - No events have been recorded for this device yet

    Above 0.5 means the context matches observed patterns.
    Below 0.5 means this is unusual for this device at this time.
    """
    if context is None:
        context = current_context()

    device_class = infer_device_class(device)
    profile = DEVICE_MODELS.get(device_class, DEVICE_MODELS["default"])
    hour = context.get("hour", datetime.now().hour)
    hour_window = int(profile["hour_window"])

    history = graph_engine.query_behavior_history(
        device_id=device["id"],
        action=action,
        hour=hour,
        hour_window=hour_window,
    )

    total = history["total"]
    if total == 0:
        return _NEUTRAL_SCORE

    matching = history["matching"]

    # Frequency signal: how often has this action happened for this device?
    # Normalize against total events; saturates toward 1.0 with more data.
    frequency_ratio = matching / total
    frequency_score = min(0.95, 0.5 + frequency_ratio * 0.5)

    # Time signal: penalise if current hour never matches this action.
    # If ALL events for this device in the window have this action, time_score→1.0
    # If no events in the window at all, time_score→0.4 (mild penalty).
    time_score = 0.4 if matching == 0 else min(0.9, 0.5 + (matching / max(1, total)) * 0.8)

    combined = (
        profile["time_weight"] * time_score
        + profile["frequency_weight"] * frequency_score
    )

    logger.debug(
        "behavior_score device=%s action=%s hour=%d matching=%d total=%d "
        "class=%s → time=%.3f freq=%.3f combined=%.3f",
        device["id"], action, hour, matching, total,
        device_class, time_score, frequency_score, combined,
    )

    return round(combined, 4)
