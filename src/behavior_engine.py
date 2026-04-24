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
import weather_client

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
    # time_weight + frequency_weight + weather_weight = 1.0
    "light": {
        "time_weight": 0.5,       # circadian pattern — strong signal
        "frequency_weight": 0.3,
        "weather_weight": 0.2,    # overcast sky → lights needed
        "hour_window": 3,
    },
    "fan": {
        "time_weight": 0.2,       # fans respond to temperature, not clock
        "frequency_weight": 0.4,
        "weather_weight": 0.4,    # hot / humid → fan more likely
        "hour_window": 2,
    },
    "ac": {
        "time_weight": 0.1,
        "frequency_weight": 0.4,
        "weather_weight": 0.5,    # AC is almost purely temperature-driven
        "hour_window": 2,
    },
    "switch": {
        "time_weight": 0.5,
        "frequency_weight": 0.4,
        "weather_weight": 0.1,
        "hour_window": 3,
    },
    "default": {
        "time_weight": 0.45,
        "frequency_weight": 0.4,
        "weather_weight": 0.15,
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
    """
    Return the current context for scoring — temporal + weather.

    Weather is fetched from Open-Meteo once per calendar day and cached.
    The dict is safe to pass to behavior scoring and LLM prompts.
    """
    now = datetime.now(timezone.utc)
    ctx: Dict[str, Any] = {
        "hour": now.hour,
        "day_of_week": now.weekday(),   # 0=Mon … 6=Sun
        "is_weekend": now.weekday() >= 5,
    }
    ctx.update(weather_client.get_weather())   # adds temp, humidity, cloud, flags
    return ctx


# ---------------------------------------------------------------------------
# Weather scoring
# ---------------------------------------------------------------------------

def _weather_score(device_class: str, context: Dict[str, Any]) -> float:
    """
    Return a weather-based score in [0, 1] for this device class.

    0.5 = neutral (no weather data or irrelevant conditions)
    > 0.5 = weather conditions match expected usage of this device
    < 0.5 = conditions suggest this device is unlikely to be needed

    Rules (device physics):
        fan / ac  — hot or humid → high score; cold → low score
        light     — overcast or night hours → high score; bright midday → lower
        switch    — weather barely matters; slight boost if any extreme
    """
    if not context:
        return 0.5

    is_hot = context.get("is_hot", False)
    is_cold = context.get("is_cold", False)
    is_humid = context.get("is_humid", False)
    is_overcast = context.get("is_overcast", False)
    hour = context.get("hour", 12)
    temp = context.get("temperature_c")

    if device_class in ("fan", "ac"):
        if is_hot and is_humid:
            return 0.92
        if is_hot:
            return 0.82
        if is_humid:
            return 0.72
        if is_cold:
            return 0.25   # unlikely to run fan/AC when cold
        if temp is not None and temp > 22:
            return 0.60   # warm but not hot — mild boost
        return 0.5

    if device_class == "light":
        is_dark_hours = hour < 7 or hour >= 18
        if is_overcast and is_dark_hours:
            return 0.90
        if is_overcast:
            return 0.72
        if is_dark_hours:
            return 0.78
        # Bright sunny midday — lights less likely needed
        if not is_overcast and 10 <= hour <= 16:
            return 0.35
        return 0.5

    if device_class == "switch":
        if is_hot or is_cold or is_overcast:
            return 0.55   # slight bump during extreme conditions
        return 0.5

    return 0.5


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

    Three signals are combined via per-device-class weights:
        time      — hour-of-day pattern from Memgraph history
        frequency — raw action frequency from Memgraph history
        weather   — current conditions from Open-Meteo (day-cached)

    Returns 0.5 (neutral) when Memgraph has no history yet.
    Weather is always included; it provides signal even before history exists.
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

    w_time = profile["time_weight"]
    w_freq = profile["frequency_weight"]
    w_wthr = profile["weather_weight"]

    total = history["total"]
    w_score = _weather_score(device_class, context)

    if total == 0:
        # No history yet — weather is the only signal; blend with neutral
        combined = w_wthr * w_score + (1.0 - w_wthr) * _NEUTRAL_SCORE
        logger.debug(
            "behavior_score device=%s action=%s — no history, weather=%.3f → %.3f",
            device["id"], action, w_score, combined,
        )
        return round(combined, 4)

    matching = history["matching"]

    frequency_ratio = matching / total
    frequency_score = min(0.95, 0.5 + frequency_ratio * 0.5)

    time_score = 0.4 if matching == 0 else min(0.9, 0.5 + (matching / max(1, total)) * 0.8)

    combined = (
        w_time * time_score
        + w_freq * frequency_score
        + w_wthr * w_score
    )

    logger.debug(
        "behavior_score device=%s action=%s hour=%d matching=%d total=%d "
        "class=%s → time=%.3f freq=%.3f weather=%.3f combined=%.3f",
        device["id"], action, hour, matching, total,
        device_class, time_score, frequency_score, w_score, combined,
    )

    return round(combined, 4)
