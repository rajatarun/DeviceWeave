"""
Runtime context provider for the Policy Engine.

Assembles the four scalar values that policy conditions can reference:

  temperature   float   Current outdoor temperature in °F.
                        Sourced from Open-Meteo via weather_client (which
                        returns °C — converted here).  Defaults to 70 °F
                        (neutral) on any fetch failure so a missing weather
                        reading never incorrectly triggers a temperature-
                        based policy.

  humidity      float   Relative humidity 0-100 %.
                        Defaults to 50 on failure.

  time_hour     int     Local hour-of-day in the America/Chicago timezone,
                        0-23.  Always available.

  is_home       bool    Whether the home is occupied.  Read from the
                        PresenceTable DynamoDB singleton (pk="home_state").
                        Defaults to True (home) so a missing or unavailable
                        reading never accidentally triggers a
                        "nobody_home → block everything" policy.

Design notes:
- Temperature conversion: °F = (°C × 9/5) + 32
- All failures are best-effort and logged at WARNING level.
- context dict keys match exactly the Policy DSL condition field names so
  evaluator.py can do a direct context[cond["field"]] lookup.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import weather_client

logger = logging.getLogger(__name__)

_CENTRAL_TZ = ZoneInfo("America/Chicago")
_PRESENCE_TABLE_NAME: str = os.environ.get("PRESENCE_TABLE_NAME", "")

# Safe neutral defaults — chosen so that a missing sensor value does NOT
# accidentally trigger a restrictive policy.
_DEFAULT_TEMPERATURE_F = 70.0  # comfortable — neither hot nor cold
_DEFAULT_HUMIDITY = 50.0       # comfortable — neither humid nor dry
_DEFAULT_IS_HOME = True        # assume home — avoids false lockout


def get_context() -> Dict[str, Any]:
    """
    Return the current evaluation context as a flat dict whose keys match
    the Policy DSL condition field names exactly:

        {
            "temperature": float,  # °F
            "humidity":    float,  # 0-100
            "time_hour":   int,    # 0-23 (local Chicago time)
            "is_home":     bool,
        }
    """
    weather = _get_weather()

    temp_c = weather.get("temperature_c")
    temp_f = (temp_c * 9 / 5 + 32) if temp_c is not None else _DEFAULT_TEMPERATURE_F

    humidity = weather.get("humidity_pct")
    if humidity is None:
        humidity = _DEFAULT_HUMIDITY

    return {
        "temperature": round(temp_f, 1),
        "humidity": float(humidity),
        "time_hour": datetime.now(_CENTRAL_TZ).hour,
        "is_home": _get_is_home(),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_weather() -> Dict[str, Any]:
    try:
        return weather_client.get_weather() or {}
    except Exception as exc:
        logger.warning("Policy context: weather fetch failed (%s) — using defaults", exc)
        return {}


def _get_is_home() -> bool:
    if not _PRESENCE_TABLE_NAME:
        return _DEFAULT_IS_HOME

    try:
        import boto3
        table = boto3.resource("dynamodb").Table(_PRESENCE_TABLE_NAME)
        resp = table.get_item(Key={"pk": "home_state"})
        item = resp.get("Item")
        if item is None:
            return _DEFAULT_IS_HOME
        return bool(item.get("is_home", _DEFAULT_IS_HOME))
    except Exception as exc:
        logger.warning(
            "Policy context: presence table read failed (%s) — defaulting is_home=True", exc
        )
        return _DEFAULT_IS_HOME
