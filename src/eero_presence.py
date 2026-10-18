"""
Map an eero client list onto DeviceWeave presence.

Pure functions: no HTTP and no DynamoDB. The poller Lambda feeds them the
raw ``/devices`` payload and the tracked-device rows, then writes the result
through presence_store (the same home_state item GET /presence reads).

Departure is debounced. Arrival is immediate. A client counts as home only
when eero says it is connected; a row that is merely remembered does not
keep someone home by itself, but it also does not flip them to away until
``miss_threshold`` consecutive polls miss every device mapped to them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

_MAC_RE = re.compile(r"[^0-9a-fA-F]")
_MAX_LABEL = 120


def normalize_mac(value: Any) -> str:
    """Lowercase hex only, so ``AA:BB`` and ``aa-bb`` compare equal."""
    return _MAC_RE.sub("", str(value or "")).lower()


def compute_presence(
    previous: Optional[Dict[str, Any]],
    tracked: Iterable[Dict[str, Any]],
    clients: Iterable[Dict[str, Any]],
    *,
    miss_threshold: int,
    now: str,
) -> Optional[Dict[str, Any]]:
    """
    Build the next home_state item, or None when nothing is tracked.

    None means "do not change occupancy". An empty client list is a real
    observation and still returns an item, so debounce can count the miss.
    """
    groups = _group_tracked(tracked)
    if not groups:
        return None

    threshold = miss_threshold if miss_threshold >= 1 else 1
    parsed = [parse_client(client, now) for client in clients if isinstance(client, dict)]
    previous_people = {
        str(person.get("person") or "").casefold(): person
        for person in (previous or {}).get("people") or []
        if isinstance(person, dict) and person.get("person")
    }

    people: List[Dict[str, Any]] = []
    for group in groups:
        prev = previous_people.get(group["person"].casefold())
        seen = _best_match(group["devices"], parsed)
        people.append(_person_state(group["person"], prev, seen, threshold))

    return _aggregate(people, now)


def parse_client(device: Dict[str, Any], now: str) -> Dict[str, Any]:
    """Normalize one raw eero client into the fields presence cares about."""
    mac = normalize_mac(device.get("mac") or device.get("mac_address") or "")
    names: List[str] = []
    for key in ("nickname", "hostname", "display_name", "name"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    source = device.get("source") if isinstance(device.get("source"), dict) else {}
    location = _label(source.get("location"))
    node_name = _label(source.get("display_name")) or _label(source.get("model"))
    connected = _is_connected(device)
    last_seen = _label(device.get("last_active"), limit=40) or _label(device.get("last_seen"), limit=40)
    if connected and not last_seen:
        last_seen = now
    return {
        "mac": mac,
        "name_keys": {name.casefold() for name in names},
        "connected": connected,
        "zone": location or node_name,
        "node": node_name or location,
        "signal": _signal(device),
        "last_seen": last_seen,
    }


def _group_tracked(tracked: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in tracked:
        if not isinstance(row, dict):
            continue
        person = str(row.get("person") or "").strip()
        match = str(row.get("match") or "").strip().lower()
        if not person or match not in ("mac", "name"):
            continue
        if match == "mac" and not normalize_mac(row.get("mac")):
            continue
        if match == "name" and not str(row.get("name") or "").strip():
            continue
        key = person.casefold()
        if key not in groups:
            groups[key] = {"person": person, "devices": []}
            order.append(key)
        groups[key]["devices"].append(row)
    return [groups[key] for key in order]


def _best_match(devices: List[Dict[str, Any]], clients: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for row in devices:
        for client in clients:
            if client.get("connected") and _row_matches(row, client):
                found.append(client)
    if not found:
        return None
    return max(found, key=lambda client: client.get("last_seen") or "")


def connected_match(
    tracked: Iterable[Dict[str, Any]],
    clients: Iterable[Dict[str, Any]],
    person: str,
    now: str,
) -> Optional[Dict[str, Any]]:
    """The connected client mapped to ``person``, or None.

    The returned ``mac`` and ``device`` are for the caller to hash and
    then drop. They must not be written to presence or fingerprints.
    """
    wanted = person.casefold()
    group = next((item for item in _group_tracked(tracked) if item["person"].casefold() == wanted), None)
    if group is None:
        return None
    found: List[Dict[str, Any]] = []
    for row in group["devices"]:
        for client in clients:
            if not isinstance(client, dict):
                continue
            parsed = parse_client(client, now)
            if parsed.get("connected") and _row_matches(row, parsed):
                found.append({
                    "mac": parsed.get("mac") or "",
                    "node": parsed.get("node"),
                    "signal": parsed.get("signal"),
                    "last_seen": parsed.get("last_seen") or "",
                    "device": client,
                })
    if not found:
        return None
    return max(found, key=lambda match: match.get("last_seen") or "")


def _row_matches(row: Dict[str, Any], client: Dict[str, Any]) -> bool:
    match = str(row.get("match") or "").strip().lower()
    if match == "mac":
        wanted = normalize_mac(row.get("mac"))
        return bool(wanted) and wanted == client.get("mac")
    if match == "name":
        wanted = str(row.get("name") or "").strip().casefold()
        return bool(wanted) and wanted in (client.get("name_keys") or set())
    return False


def _person_state(
    person: str,
    previous: Optional[Dict[str, Any]],
    seen: Optional[Dict[str, Any]],
    threshold: int,
) -> Dict[str, Any]:
    if seen is not None:
        return _without_empty({
            "person": person,
            "is_home": True,
            "zone": seen.get("zone"),
            "node": seen.get("node"),
            "signal": seen.get("signal"),
            "last_seen": seen.get("last_seen"),
            "freshness": "fresh",
            "miss_count": 0,
        })

    misses = _as_int((previous or {}).get("miss_count"), 0) + 1
    if misses >= threshold:
        return _without_empty({
            "person": person,
            "is_home": False,
            "zone": None,
            "node": None,
            "signal": None,
            "last_seen": (previous or {}).get("last_seen"),
            "freshness": "fresh",
            "miss_count": misses,
        })

    # Hold the last occupancy through the debounce window. Someone we have
    # never successfully placed defaults to home so a single dark poll cannot
    # flip the house to away.
    if previous is not None and "is_home" in previous:
        is_home = bool(previous.get("is_home"))
    else:
        is_home = True
    return _without_empty({
        "person": person,
        "is_home": is_home,
        "zone": (previous or {}).get("zone"),
        "node": (previous or {}).get("node"),
        "signal": (previous or {}).get("signal"),
        "last_seen": (previous or {}).get("last_seen"),
        "freshness": "fresh",
        "miss_count": misses,
    })


def _aggregate(people: List[Dict[str, Any]], now: str) -> Dict[str, Any]:
    home = [person for person in people if person.get("is_home")]
    if home:
        chosen = max(home, key=lambda person: person.get("last_seen") or "")
        zone = chosen.get("zone")
        node = chosen.get("node")
        signal = chosen.get("signal")
        last_seen = chosen.get("last_seen")
    else:
        zone = None
        node = None
        signal = None
        stamped = [person.get("last_seen") for person in people if person.get("last_seen")]
        last_seen = max(stamped) if stamped else None
    return _without_empty({
        "is_home": bool(home),
        "updated_at": now,
        "zone": zone,
        "node": node,
        "signal": signal,
        "last_seen": last_seen,
        "freshness": "fresh",
        "source": "eero",
        "people": people,
    })


def _is_connected(device: Dict[str, Any]) -> bool:
    connected = device.get("connected")
    if connected is True:
        return True
    if isinstance(connected, str) and connected.strip().lower() == "true":
        return True
    status = device.get("status") or device.get("connection_status")
    return isinstance(status, str) and status.strip().lower() == "connected"


def _signal(device: Dict[str, Any]) -> Optional[str]:
    connectivity = device.get("connectivity") if isinstance(device.get("connectivity"), dict) else {}
    signal = connectivity.get("signal")
    if signal is None and connectivity.get("score_bars") is not None:
        signal = f"{connectivity.get('score_bars')}/5"
    elif signal is None and connectivity.get("score") is not None:
        signal = connectivity.get("score")
    if signal is None and device.get("signal") is not None:
        signal = device.get("signal")
    if signal is None:
        return None
    return _label(signal, limit=40)


def _label(value: Any, limit: int = _MAX_LABEL) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _without_empty(item: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values. DynamoDB rejects them, and False/0 must stay."""
    cleaned: Dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        cleaned[key] = value
    return cleaned
