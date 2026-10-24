"""Clamp known numeric action params to a device's capability ranges.

A MODIFY verdict replaces the request params wholesale. A rule can name a
brightness, or a duration, outside what the device accepts, and that value
used to be forwarded to the adapter unchanged. Clamping runs when the
verdict is applied and again immediately before the adapter call.

Brightness defaults to 0–100, the percentage scale the light adapters share,
unless the device declares its own range. Any other numeric param, including
duration, is clamped only when the device declares a range for it::

    device["capability_ranges"] = {
        "brightness": {"min": 1, "max": 100},
        "duration": {"min": 0, "max": 3600},
    }

``min`` or ``max`` may be omitted to bound one side. A two-item list
``[min, max]`` is accepted. An inverted or unreadable declaration is
ignored, and the previous range (the brightness default, or none) stands.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Percentage brightness. A tighter vendor floor, when the device declares
# one, replaces this. Adapters are not asked to reject the out-of-range
# value themselves.
_DEFAULT_RANGES: Dict[str, Tuple[Any, Any]] = {
    "brightness": (0, 100),
}

Bound = Tuple[Optional[Any], Optional[Any]]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return True


def _bound(value: Any) -> Optional[Any]:
    if value is None or not _is_number(value) or not _finite(value):
        return None
    return value


def _parse_range(spec: Any) -> Optional[Bound]:
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        lo, hi = _bound(spec[0]), _bound(spec[1])
        if lo is None and hi is None:
            return None
    elif isinstance(spec, Mapping) and ("min" in spec or "max" in spec):
        lo = _bound(spec.get("min")) if "min" in spec else None
        hi = _bound(spec.get("max")) if "max" in spec else None
        if "min" in spec and lo is None and spec.get("min") is not None:
            return None
        if "max" in spec and hi is None and spec.get("max") is not None:
            return None
        if lo is None and hi is None:
            return None
    else:
        return None
    if lo is not None and hi is not None and lo > hi:
        return None
    return (lo, hi)


def capability_ranges(device: Optional[Mapping[str, Any]]) -> Dict[str, Bound]:
    """Ranges that apply to ``device``, including the brightness default."""
    ranges: Dict[str, Bound] = dict(_DEFAULT_RANGES)
    if not isinstance(device, Mapping):
        return ranges
    declared = device.get("capability_ranges")
    if not isinstance(declared, Mapping):
        return ranges
    for name, spec in declared.items():
        parsed = _parse_range(spec)
        key = str(name)
        if parsed is None:
            logger.warning(
                "Ignoring unreadable capability range for %s on device %s",
                key, device.get("id"),
            )
            continue
        lo, hi = parsed
        # A one-sided declaration on a param that already has a default
        # (brightness) replaces only the side it names. ``{"max": 40}``
        # is 0–40, not "no floor". A two-item list states both sides.
        if key in ranges and isinstance(spec, Mapping):
            old_lo, old_hi = ranges[key]
            if "min" not in spec:
                lo = old_lo
            if "max" not in spec:
                hi = old_hi
        ranges[key] = (lo, hi)
    return ranges


def _snap(clamped: Any) -> Any:
    """Prefer an int when the bound we snapped to is a whole number."""
    if isinstance(clamped, bool):
        return clamped
    if isinstance(clamped, int):
        return clamped
    if isinstance(clamped, Decimal):
        if clamped == clamped.to_integral_value():
            return int(clamped)
        return float(clamped)
    if isinstance(clamped, float) and clamped.is_integer():
        return int(clamped)
    return clamped


def clamp_numeric_params(
    params: Optional[Mapping[str, Any]],
    device: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a copy of ``params`` with known numeric values inside range.

    Non-numeric values, and numeric params with no declared range, are
    copied unchanged. Brightness always has a range.
    """
    if not isinstance(params, Mapping):
        return {}
    ranges = capability_ranges(device)
    out: Dict[str, Any] = {}
    device_id = device.get("id") if isinstance(device, Mapping) else None
    for key, value in params.items():
        bounds = ranges.get(str(key))
        if bounds is None or not _is_number(value) or not _finite(value):
            out[key] = value
            continue
        lo, hi = bounds
        clamped = value
        if lo is not None and clamped < lo:
            clamped = lo
        if hi is not None and clamped > hi:
            clamped = hi
        if clamped != value:
            clamped = _snap(clamped)
            logger.info(
                "Clamped %s for device %s: %s -> %s",
                key, device_id, value, clamped,
            )
        out[key] = clamped
    return out
