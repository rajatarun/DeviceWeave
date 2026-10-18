"""
Room fingerprints and the classifier that reads them.

A fingerprint is the samples taken while a person stood in one room:
the eero node they were on, a coarse signal bucket, and the time.
Those samples are the model. Live polls are not appended to them.

The eero cloud API does not publish a live RSSI field or a
signal-updated timestamp. ``connectivity.score_bars`` (1–5),
``connectivity.signal`` / ``signal_avg`` (quality words, sometimes a
dBm reading), and an integer ``connectivity.score`` in 1–5 are the
fields the client libraries actually carry. ``last_active`` is the
client's last activity, not a fresh signal sample. Buckets stay coarse
on purpose: two rooms that share a node and a bucket are a tie, and a
tie is ``unknown``.

Device identifiers stored here and on the presence item are HMAC-SHA256
of the normalized MAC. The key is a dedicated secret, not the eero
session token (that token rotates and would invalidate every label).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_SAMPLES = 8
DEFAULT_SAMPLES = 5
SAMPLE_INTERVAL_SECONDS = 12
CONFIDENCE_FLOOR = 0.5
BUCKET_SPAN = 4  # buckets are 1..5, so the farthest pair is 4 apart
SMOOTHING_READINGS = 2

_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,38}[A-Za-z0-9])?$")
_DBM_RE = re.compile(r"^(-?\d+)(?:\s*dbm)?$")
_BARS_RE = re.compile(r"^([1-5])/5$")

# Values seen for connectivity.signal. Unknown words are not guessed into a bucket.
_QUALITY = {
    "excellent": 5,
    "strong": 4,
    "good": 4,
    "fair": 3,
    "average": 3,
    "weak": 2,
    "poor": 1,
}


class LabelError(ValueError):
    """A room name, person name, or sample count the API must reject."""


def validate_room(value: Any) -> str:
    label = _validate_label(value, "room")
    if label.casefold() == "unknown":
        raise LabelError("room name 'unknown' is reserved")
    return label


def validate_person(value: Any) -> str:
    return _validate_label(value, "person")


def is_room_label(value: Any, *, allow_unknown: bool = False) -> bool:
    if allow_unknown and value == "unknown":
        return True
    try:
        validate_room(value)
    except LabelError:
        return False
    return True


def validate_samples(value: Any, default: int = DEFAULT_SAMPLES) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabelError("samples must be an integer")
    if not 1 <= value <= MAX_SAMPLES:
        raise LabelError(f"samples must be from 1 to {MAX_SAMPLES}")
    return value


def signal_bucket(device: Any) -> Optional[int]:
    """Coarse 1–5 bucket from fields eero's device payload actually has.

    ``score_bars`` wins when it is in 1–5 (0 means the API left it unset).
    Otherwise a quality word or a negative dBm number on ``signal`` or
    ``signal_avg``. A fractional ``score`` (the libraries type it as a
    float) is not mapped: there is no published scale for it.
    """
    if not isinstance(device, dict):
        return None
    connectivity = device.get("connectivity") if isinstance(device.get("connectivity"), dict) else {}
    bars = _as_int(connectivity.get("score_bars"))
    if bars is not None and 1 <= bars <= 5:
        return bars
    for key in ("signal", "signal_avg"):
        bucket = _bucket_from_value(connectivity.get(key))
        if bucket is not None:
            return bucket
    score = connectivity.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        if float(score).is_integer() and 1 <= int(score) <= 5:
            return int(score)
    return _bucket_from_value(device.get("signal"))


def device_hmac(mac: str, key: str) -> str:
    """HMAC-SHA256 of the hex MAC. Empty when either side is missing."""
    from eero_presence import normalize_mac

    normalized = normalize_mac(mac)
    secret = (key or "").strip()
    if not normalized or not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def load_hmac_key(secret_name: str, client: Any = None) -> str:
    """Read the HMAC key. The value is never logged."""
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_name)
    secret = resp.get("SecretString") or ""
    if not isinstance(secret, str):
        return ""
    return secret.strip()


def classify(
    node: Any,
    bucket: Any,
    fingerprints: Iterable[Dict[str, Any]],
) -> Tuple[str, float]:
    """Nearest room on this node, or ``unknown``.

    A room competes only when it has a sample on ``node``. Distance is the
    smallest absolute bucket difference. An equal distance across rooms,
    an unseen node, or a confidence under ``CONFIDENCE_FLOOR`` is
    ``unknown`` — the node zone is left to the caller, and this function
    does not pick a runner-up.
    """
    if not isinstance(node, str) or not node.strip():
        return "unknown", 0.0
    node_name = node.strip()
    live_bucket = _as_bucket(bucket)
    ranked: List[Tuple[str, int, bool]] = []
    for fp in fingerprints:
        room = fp.get("room")
        if not isinstance(room, str) or not room.strip() or room.casefold() == "unknown":
            continue
        on_node = [
            sample for sample in fp.get("samples") or []
            if isinstance(sample, dict) and str(sample.get("node") or "") == node_name
        ]
        if not on_node:
            continue
        bucketed = [b for b in (_as_bucket(sample.get("bucket")) for sample in on_node) if b is not None]
        if live_bucket is not None and bucketed:
            dist = min(abs(b - live_bucket) for b in bucketed)
            had_bucket = True
        else:
            dist = 0
            had_bucket = False
        ranked.append((room, dist, had_bucket))
    if not ranked:
        return "unknown", 0.0
    best_dist = min(item[1] for item in ranked)
    winners = [item for item in ranked if item[1] == best_dist]
    rooms = {item[0] for item in winners}
    if len(rooms) != 1:
        return "unknown", 0.0
    room = next(iter(rooms))
    had_bucket = any(item[2] for item in winners)
    if live_bucket is None or not had_bucket:
        confidence = 0.5 if best_dist == 0 else 0.0
    else:
        confidence = 1.0 - (best_dist / BUCKET_SPAN)
        others = [item[1] for item in ranked if item[0] != room]
        if others and (min(others) - best_dist) <= 1:
            confidence *= 0.5
    confidence = round(confidence, 2)
    if confidence < CONFIDENCE_FLOOR:
        return "unknown", confidence
    return room, confidence


def smooth(previous: Optional[Dict[str, Any]], proposed: str, confidence: float) -> Dict[str, Any]:
    """Hold the displayed room until ``proposed`` wins two readings in a row.

    The first assignment is a change from ``unknown`` and waits too, so a
    single poll cannot flicker the room.
    """
    prev = previous or {}
    displayed = prev.get("room") or "unknown"
    pending = prev.get("room_pending")
    count = _as_int(prev.get("room_pending_count")) or 0
    if displayed == proposed:
        return {
            "room": proposed,
            "room_confidence": _shown_confidence(proposed, confidence),
            "room_pending": proposed,
            "room_pending_count": SMOOTHING_READINGS,
        }
    if pending == proposed:
        count += 1
    else:
        pending = proposed
        count = 1
    if count >= SMOOTHING_READINGS:
        displayed = proposed
        shown = _shown_confidence(proposed, confidence)
    else:
        shown = 0.0 if displayed == "unknown" else _as_float(prev.get("room_confidence"))
    return {
        "room": displayed,
        "room_confidence": shown,
        "room_pending": pending,
        "room_pending_count": count,
    }


def carry_forward(updated: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep the last room when this poll cannot classify.

    Used when the HMAC key or the fingerprint table cannot be read.
    Away still clears the room. A held room is not a new guess.
    """
    prev_people = {
        str(person.get("person") or "").casefold(): person
        for person in (previous or {}).get("people") or []
        if isinstance(person, dict) and person.get("person")
    }
    for person in updated.get("people") or []:
        if not isinstance(person, dict):
            continue
        if not person.get("is_home"):
            _clear_room(person)
            continue
        _hold_room(person, prev_people.get(str(person.get("person") or "").casefold()) or {})
    _aggregate_room(updated)
    return updated


def apply_rooms(
    updated: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    readings: Dict[str, Dict[str, Any]],
    fingerprints: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Set ``room`` and ``room_confidence`` on the presence item.

    ``readings`` maps a person's casefolded name to ``node``, ``bucket``,
    and ``device_hmac`` for this poll. A debounce miss is not a reading:
    the previous room is held. Away clears the room to ``unknown``.
    Fingerprints are used only when their HMAC matches the live device.
    """
    prev_people = {
        str(person.get("person") or "").casefold(): person
        for person in (previous or {}).get("people") or []
        if isinstance(person, dict) and person.get("person")
    }
    by_hmac: Dict[str, List[Dict[str, Any]]] = {}
    for fp in fingerprints:
        if not isinstance(fp, dict):
            continue
        digest = str(fp.get("device_hmac") or "")
        if digest:
            by_hmac.setdefault(digest, []).append(fp)

    for person in updated.get("people") or []:
        if not isinstance(person, dict):
            continue
        key = str(person.get("person") or "").casefold()
        prev = prev_people.get(key) or {}
        if not person.get("is_home"):
            _clear_room(person)
            continue
        if (_as_int(person.get("miss_count")) or 0) > 0:
            _hold_room(person, prev)
            continue
        reading = readings.get(key) or {}
        digest = str(reading.get("device_hmac") or "")
        proposed, confidence = classify(
            reading.get("node"),
            reading.get("bucket"),
            by_hmac.get(digest, []),
        )
        person.update(smooth(prev, proposed, confidence))
        if digest:
            person["device_hmac"] = digest
        else:
            person.pop("device_hmac", None)
    _aggregate_room(updated)
    return updated


def person_pk(person: str) -> str:
    return "person#" + person.casefold()


def room_sk(room: str) -> str:
    return "room#" + room.casefold()


def fingerprint_item(
    person: str,
    room: str,
    device_hmac_hex: str,
    samples: List[Dict[str, Any]],
    updated_at: str,
) -> Dict[str, Any]:
    """One item per person and room. Samples replace; they are not a log."""
    cleaned = [_clean_sample(sample) for sample in samples[:MAX_SAMPLES]]
    return {
        "pk": person_pk(person),
        "sk": room_sk(room),
        "person": person,
        "room": room,
        "device_hmac": device_hmac_hex,
        "samples": cleaned,
        "sample_count": len(cleaned),
        "updated_at": updated_at,
    }


def load_all(table: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(item for item in (resp.get("Items") or []) if isinstance(item, dict))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def save_fingerprint(table: Any, item: Dict[str, Any]) -> None:
    import presence_store

    table.put_item(Item=presence_store.to_dynamo(item))


def list_fingerprints(table: Any, person: Optional[str] = None) -> List[Dict[str, Any]]:
    """Authenticated view: room, counts, nodes, buckets, samples. No HMAC."""
    import presence_store

    wanted = person.casefold() if person else None
    views = []
    for raw in load_all(table):
        item = presence_store.from_dynamo(raw)
        if wanted and str(item.get("person") or "").casefold() != wanted:
            continue
        samples = [sample for sample in item.get("samples") or [] if isinstance(sample, dict)]
        nodes = []
        buckets = []
        for sample in samples:
            node = sample.get("node")
            if isinstance(node, str) and node not in nodes:
                nodes.append(node)
            bucket = _as_bucket(sample.get("bucket"))
            if bucket is not None and bucket not in buckets:
                buckets.append(bucket)
        views.append({
            "person": item.get("person"),
            "room": item.get("room"),
            "sample_count": _as_int(item.get("sample_count")) if item.get("sample_count") is not None else len(samples),
            "nodes": nodes,
            "buckets": buckets,
            "updated_at": item.get("updated_at"),
            "samples": [_public_sample(sample) for sample in samples],
        })
    views.sort(key=lambda view: (str(view.get("person") or ""), str(view.get("room") or "")))
    return views


def delete_fingerprints(table: Any, room: str, person: Optional[str] = None) -> int:
    wanted_room = room.casefold()
    wanted_person = person.casefold() if person else None
    deleted = 0
    for item in load_all(table):
        if str(item.get("room") or "").casefold() != wanted_room:
            continue
        if wanted_person and str(item.get("person") or "").casefold() != wanted_person:
            continue
        table.delete_item(Key={"pk": item.get("pk"), "sk": item.get("sk")})
        deleted += 1
    return deleted


def _validate_label(value: Any, kind: str) -> str:
    if not isinstance(value, str):
        raise LabelError(f"{kind} must be a string")
    label = value.strip()
    if not _LABEL_RE.fullmatch(label):
        raise LabelError(
            f"{kind} must be 1-40 characters and use only letters, digits, spaces, hyphens, and underscores"
        )
    return label


def _bucket_from_value(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _bucket_from_dbm(float(value))
    text = str(value).strip().casefold()
    if not text:
        return None
    if text in _QUALITY:
        return _QUALITY[text]
    bars = _BARS_RE.fullmatch(text)
    if bars:
        return int(bars.group(1))
    dbm = _DBM_RE.fullmatch(text)
    if dbm:
        return _bucket_from_dbm(float(dbm.group(1)))
    return None


def _bucket_from_dbm(dbm: float) -> Optional[int]:
    """Coarse buckets for a negative dBm reading. Positive numbers are not dBm."""
    if dbm >= 0:
        return None
    if dbm >= -50:
        return 5
    if dbm >= -60:
        return 4
    if dbm >= -70:
        return 3
    if dbm >= -80:
        return 2
    return 1


def _as_bucket(value: Any) -> Optional[int]:
    number = _as_int(value)
    if number is None or not 1 <= number <= 5:
        return None
    return number


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    # DynamoDB numbers come back as Decimal.
    try:
        from decimal import Decimal

        if isinstance(value, Decimal) and value == int(value):
            return int(value)
    except Exception:
        return None
    return None


def _as_float(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _shown_confidence(room: str, confidence: float) -> float:
    if room == "unknown":
        return 0.0
    return _as_float(confidence)


def _clear_room(person: Dict[str, Any]) -> None:
    person["room"] = "unknown"
    person["room_confidence"] = 0.0
    person.pop("room_pending", None)
    person.pop("room_pending_count", None)
    person.pop("device_hmac", None)


def _hold_room(person: Dict[str, Any], previous: Dict[str, Any]) -> None:
    person["room"] = previous.get("room") or "unknown"
    person["room_confidence"] = 0.0 if person["room"] == "unknown" else _as_float(previous.get("room_confidence"))
    if previous.get("room_pending"):
        person["room_pending"] = previous["room_pending"]
        person["room_pending_count"] = _as_int(previous.get("room_pending_count")) or 0
    else:
        person.pop("room_pending", None)
        person.pop("room_pending_count", None)
    if previous.get("device_hmac"):
        person["device_hmac"] = previous["device_hmac"]


def _aggregate_room(updated: Dict[str, Any]) -> None:
    home = [
        person for person in updated.get("people") or []
        if isinstance(person, dict) and person.get("is_home")
    ]
    if not home:
        updated["room"] = "unknown"
        updated["room_confidence"] = 0.0
        return
    chosen = max(home, key=lambda person: person.get("last_seen") or "")
    updated["room"] = chosen.get("room") or "unknown"
    updated["room_confidence"] = _as_float(chosen.get("room_confidence"))


def _clean_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {
        "node": str(sample.get("node") or "")[:120],
        "taken_at": str(sample.get("taken_at") or "")[:40],
    }
    bucket = _as_bucket(sample.get("bucket"))
    if bucket is not None:
        cleaned["bucket"] = bucket
    signal = sample.get("signal")
    if isinstance(signal, str) and signal.strip():
        cleaned["signal"] = signal.strip()[:40]
    return cleaned


def _public_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
    view = {
        "node": sample.get("node"),
        "taken_at": sample.get("taken_at"),
    }
    bucket = _as_bucket(sample.get("bucket"))
    if bucket is not None:
        view["bucket"] = bucket
    if sample.get("signal"):
        view["signal"] = sample.get("signal")
    return view
