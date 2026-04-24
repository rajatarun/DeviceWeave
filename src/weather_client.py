"""
Open-Meteo weather client — free, no API key, no sign-up.

Fetches current conditions once per calendar day (UTC) and caches the result
in the Lambda container for the rest of that day.  On the second and subsequent
requests within the same day the cached value is returned immediately with no
network call.

API: https://open-meteo.com/
No credentials required.  Rate limit is generous for single-device use.

Required env vars:
    WEATHER_LAT   float   Latitude   (default 37.7749  — San Francisco)
    WEATHER_LON   float   Longitude  (default -122.4194)

The defaults deliberately point somewhere so the system degrades gracefully
(reasonable weather data) rather than hard-failing when lat/lon are unconfigured.
"""

import json
import logging
import os
from datetime import date, timezone, datetime
from typing import Any, Dict
from urllib.request import urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

_LAT: float = float(os.environ.get("WEATHER_LAT", "37.7749"))
_LON: float = float(os.environ.get("WEATHER_LON", "-122.4194"))
_API_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 3  # fail fast — weather is enrichment, not critical path

# Module-level cache — one fetch per calendar day per Lambda container
_cache: Dict[str, Any] = {}
_cache_date: date = date.min


def get_weather() -> Dict[str, Any]:
    """
    Return today's weather snapshot, fetching once and caching for the day.

    Keys returned (all may be None if the API returned no value):
        temperature_c     float   air temperature at 2 m (°C)
        feels_like_c      float   apparent / feels-like temperature (°C)
        humidity_pct      int     relative humidity (%)
        cloud_cover_pct   int     total cloud cover (%)
        wind_speed_kmh    float   wind speed at 10 m (km/h)
        is_hot            bool    temperature_c > 26
        is_cold           bool    temperature_c < 10
        is_overcast       bool    cloud_cover_pct > 70
        is_humid          bool    humidity_pct > 70

    Returns an empty dict on any error so callers degrade gracefully.
    """
    global _cache, _cache_date

    today = datetime.now(timezone.utc).date()
    if _cache and _cache_date == today:
        logger.debug("Weather cache hit for %s", today)
        return _cache

    _cache = _fetch()
    _cache_date = today
    return _cache


def _fetch() -> Dict[str, Any]:
    params = (
        f"latitude={_LAT}&longitude={_LON}"
        "&current=temperature_2m,apparent_temperature,"
        "relative_humidity_2m,cloud_cover,wind_speed_10m"
        "&timezone=auto"
    )
    url = f"{_API_URL}?{params}"

    try:
        with urlopen(url, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())

        current = data.get("current", {})
        temp = current.get("temperature_2m")
        humidity = current.get("relative_humidity_2m")
        cloud = current.get("cloud_cover")

        result: Dict[str, Any] = {
            "temperature_c": temp,
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_pct": humidity,
            "cloud_cover_pct": cloud,
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "is_hot": (temp or 0) > 26,
            "is_cold": (temp or 20) < 10,
            "is_overcast": (cloud or 0) > 70,
            "is_humid": (humidity or 0) > 70,
        }
        logger.info(
            "Weather fetched: %.1f°C (feels %.1f) humidity=%d%% cloud=%d%%",
            temp or 0,
            result["feels_like_c"] or 0,
            humidity or 0,
            cloud or 0,
        )
        return result

    except URLError as exc:
        logger.warning("Weather fetch network error: %s — context will be time-only", exc)
    except Exception as exc:
        logger.warning("Weather fetch failed: %s — context will be time-only", exc)
    return {}


def summary_line() -> str:
    """
    One-line human-readable weather summary for LLM prompts.

    Examples:
        "Current weather: 28°C, feels like 31°C, humidity 80%, overcast (cloud 85%)"
        "Current weather: unavailable"
    """
    w = get_weather()
    if not w:
        return "Current weather: unavailable"

    parts = []
    if w.get("temperature_c") is not None:
        parts.append(f"{w['temperature_c']:.1f}°C")
    if w.get("feels_like_c") is not None:
        parts.append(f"feels like {w['feels_like_c']:.1f}°C")
    if w.get("humidity_pct") is not None:
        parts.append(f"humidity {w['humidity_pct']}%")
    if w.get("cloud_cover_pct") is not None:
        label = "overcast" if w["is_overcast"] else "partly cloudy" if w["cloud_cover_pct"] > 30 else "clear"
        parts.append(f"{label} (cloud {w['cloud_cover_pct']}%)")
    if w.get("wind_speed_kmh") is not None and w["wind_speed_kmh"] > 20:
        parts.append(f"windy ({w['wind_speed_kmh']:.0f} km/h)")

    return "Current weather: " + ", ".join(parts) if parts else "Current weather: unavailable"
