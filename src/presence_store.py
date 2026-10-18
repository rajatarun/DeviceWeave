"""
Home-occupancy item shared by POST/GET /presence and the eero poller.

The policy engine reads one boolean, ``is_home``, from ``pk=home_state``.
Richer eero fields (zone, node, signal, room, room_confidence, last_seen,
freshness, people) live on that same item so a presence read stays one
GetItem and a manual POST /presence does not need a second store.
``room`` is ``unknown`` until calibration and two agreeing polls name one.

``freshness`` is ``fresh`` after a successful poll or a manual write,
``stale`` when a poll could not trust eero (the last ``is_home`` is kept),
and ``unknown`` when nothing has been written yet.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

HOME_PK = "home_state"


def read_home(table: Any) -> Optional[Dict[str, Any]]:
    resp = table.get_item(Key={"pk": HOME_PK})
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def write_home(table: Any, item: Dict[str, Any]) -> Dict[str, Any]:
    stored = {"pk": HOME_PK}
    for key, value in item.items():
        if key == "pk" or value is None:
            continue
        stored[key] = _dynamo_value(value)
    table.put_item(Item=stored)
    return stored


def apply_manual(previous: Optional[Dict[str, Any]], is_home: bool, now: str) -> Dict[str, Any]:
    """Set occupancy by hand without erasing the last eero zone or people."""
    item: Dict[str, Any] = {}
    if previous:
        item.update(previous)
    item.pop("pk", None)
    item["is_home"] = bool(is_home)
    item["updated_at"] = now
    item["source"] = "manual"
    item["freshness"] = "fresh"
    item.pop("stale_reason", None)
    item.pop("stale_at", None)
    return item


def apply_stale(previous: Optional[Dict[str, Any]], reason: str, now: str) -> Dict[str, Any]:
    """
    Mark the last presence stale without changing occupancy.

    A missing record becomes ``is_home=true`` (the same safe default GET
    /presence and the policy engine already use). This path never flips a
    home record to away.
    """
    item: Dict[str, Any] = {}
    if previous:
        item.update(previous)
    item.pop("pk", None)
    was_home = True if "is_home" not in item else bool(item.get("is_home"))
    item["is_home"] = was_home
    item["freshness"] = "stale"
    item["stale_reason"] = (reason or "eero poll failed")[:500]
    item["stale_at"] = now
    if not item.get("source"):
        item["source"] = "eero"
    return item


def public_view(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """JSON-safe presence body for GET /presence and the get_presence tool."""
    if not item:
        return {
            "is_home": True,
            "updated_at": None,
            "zone": None,
            "node": None,
            "signal": None,
            "room": "unknown",
            "room_confidence": 0.0,
            "last_seen": None,
            "freshness": "unknown",
            "source": None,
            "people": [],
        }
    people = []
    for raw in item.get("people") or []:
        if not isinstance(raw, dict):
            continue
        people.append({
            "person": raw.get("person"),
            "is_home": bool(raw.get("is_home", False)),
            "zone": raw.get("zone"),
            "node": raw.get("node"),
            "signal": _text(raw.get("signal")),
            "room": _room(raw.get("room")),
            "room_confidence": _confidence(raw.get("room_confidence")),
            "last_seen": raw.get("last_seen"),
            "freshness": raw.get("freshness") or item.get("freshness") or "unknown",
            "miss_count": _as_int(raw.get("miss_count"), 0),
        })
    view: Dict[str, Any] = {
        "is_home": bool(item.get("is_home", True)),
        "updated_at": item.get("updated_at"),
        "zone": item.get("zone"),
        "node": item.get("node"),
        "signal": _text(item.get("signal")),
        "room": _room(item.get("room")),
        "room_confidence": _confidence(item.get("room_confidence")),
        "last_seen": item.get("last_seen"),
        "freshness": item.get("freshness") or "unknown",
        "source": item.get("source"),
        "people": people,
    }
    if item.get("stale_reason"):
        view["stale_reason"] = item["stale_reason"]
    return view


def _dynamo_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _dynamo_value(inner) for key, inner in value.items() if inner is not None}
    if isinstance(value, list):
        return [_dynamo_value(inner) for inner in value if inner is not None]
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _room(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "unknown"


def _confidence(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0
