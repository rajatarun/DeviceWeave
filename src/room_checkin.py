"""Labeled room check-ins.

Raja answers which room he is in. The server reads eero at ask time and
stores that snapshot with the label in DynamoDB. Nothing in this module
writes a file or returns a raw MAC. Device ids are the same HMAC used
for fingerprints, under the dedicated device secret.

``asked_at`` is when the question was asked. ``answered_at`` is when the
label was stored. Hour and weekday are ``asked_at`` in America/Chicago
(Monday is 0). The eero fields are whatever the cloud returns at the
moment of the call, not a historical reading for a past ``asked_at``.
Use ``capture`` at ask time and ``label`` when the answer arrives so the
signal matches the question.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import eero_client
import eero_presence
import eero_presence_handler
import presence_store
import room_fingerprint
from room_fingerprint import LabelError

CHICAGO = ZoneInfo("America/Chicago")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_MAX_ROOM = 80
_MAX_SOURCE = 40
_SOURCE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _-]{0,38}[A-Za-z0-9])?$")
_SK_RE = re.compile(r"^\d{8}T\d{6}Z~[0-9a-f]{8}$")
_PUBLIC_KEYS = (
    "checkin_id",
    "person",
    "room",
    "source",
    "asked_at",
    "answered_at",
    "timezone",
    "local_hour",
    "weekday",
    "device_hmac",
    "node",
    "signal",
    "signal_bucket",
    "freshness",
    "last_seen",
    "is_home",
    "predicted_room",
    "predicted_confidence",
    "predicted_source",
)


class CheckinError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now() -> datetime:
    return datetime.now(timezone.utc)


def validate_room_label(value: Any) -> str:
    """A check-in room. ``Not home`` is allowed. Blank is not."""
    if not isinstance(value, str):
        raise CheckinError(400, "room must be a string")
    label = value.strip()
    if not label or len(label) > _MAX_ROOM:
        raise CheckinError(400, "room must be 1-80 characters")
    if any(ord(ch) < 32 for ch in label):
        raise CheckinError(400, "room must not contain control characters")
    return label


def validate_source(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or not _SOURCE_RE.fullmatch(value.strip()):
        raise CheckinError(
            400,
            "source must be 1-40 letters, digits, spaces, hyphens, or underscores",
        )
    return value.strip()


def parse_time(value: Any, fallback: datetime, field: str = "asked_at") -> datetime:
    if value is None or value == "":
        return fallback
    if not isinstance(value, str):
        raise CheckinError(400, f"{field} must be an ISO-8601 timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CheckinError(400, f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_since(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    return parse_time(value, now(), field="since")


def parse_limit(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckinError(400, "limit must be an integer")
    if not 1 <= value <= MAX_LIMIT:
        raise CheckinError(400, f"limit must be from 1 to {MAX_LIMIT}")
    return value


def record(
    table: Any,
    person: Any,
    room: Any,
    asked_at: Any = None,
    source: Any = None,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    person_name = _person(person)
    room_name = validate_room_label(room)
    moment = at or now()
    asked = parse_time(asked_at, moment)
    item = _item(
        person_name,
        asked,
        source=validate_source(source, "record"),
        room=room_name,
        answered_at=moment,
        reading=_reading(person_name),
    )
    put_checkin(table, item)
    return public_record(item)


def capture(
    table: Any,
    person: Any,
    asked_at: Any = None,
    source: Any = None,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    person_name = _person(person)
    moment = at or now()
    asked = parse_time(asked_at, moment)
    item = _item(
        person_name,
        asked,
        source=validate_source(source, "capture"),
        room=None,
        answered_at=None,
        reading=_reading(person_name),
    )
    put_checkin(table, item)
    return public_record(item)


def label(
    table: Any,
    checkin_id: Any,
    room: Any,
    at: Optional[datetime] = None,
) -> Dict[str, Any]:
    room_name = validate_room_label(room)
    existing = _load(table, checkin_id)
    if existing is None:
        raise CheckinError(404, "check-in not found")
    existing["room"] = room_name
    existing["answered_at"] = _iso(at or now())
    put_checkin(table, existing)
    return public_record(existing)


def list_checkins(
    table: Any,
    person: Any = None,
    since: Any = None,
    limit: Any = None,
) -> List[Dict[str, Any]]:
    cap = parse_limit(limit)
    start = parse_since(since)
    person_name = _person(person) if person else None
    if person_name:
        items = _query(table, person_name, start, cap)
    else:
        items = _scan(table, start, cap)
    return [public_record(item) for item in items]


def list_all_checkins(
    table: Any,
    person: Any = None,
    since: Any = None,
) -> List[Dict[str, Any]]:
    """All matching rows, oldest first. For training loads; API list stays capped."""
    start = parse_since(since)
    person_name = _person(person) if person else None
    if person_name:
        items = _query_all(table, person_name, start)
    else:
        items = _scan_all(table, start)
    return [public_record(item) for item in items]


def delete_checkin(table: Any, checkin_id: Any) -> int:
    person_fold, sk = _parse_id(checkin_id)
    existing = table.get_item(Key={"pk": _pk(person_fold), "sk": sk}).get("Item")
    if not existing:
        return 0
    table.delete_item(Key={"pk": _pk(person_fold), "sk": sk})
    return 1


def store_observed(
    table: Any,
    person: Any,
    room: Any,
    reading: Dict[str, Any],
    *,
    source: str,
    at: datetime,
    timezone_name: str = "America/Chicago",
) -> Dict[str, Any]:
    """Store a labeled row from a reading the caller already has.

    Does not call eero. ``reading`` may carry node, signal, signal_bucket,
    freshness, last_seen, is_home, and device_hmac. A MAC is ignored.
    """
    person_name = _person(person)
    room_name = validate_room_label(room)
    zone = _zone(timezone_name)
    safe = _observed_reading(reading)
    item = _item(
        person_name,
        at,
        source=validate_source(source, "schedule_rule"),
        room=room_name,
        answered_at=at,
        reading=safe,
        zone=zone,
        timezone_name=timezone_name,
    )
    put_checkin(table, item)
    return public_record(item)


def put_checkin(table: Any, item: Dict[str, Any]) -> None:
    """Store one check-in. Floats become Decimal before put_item."""
    table.put_item(Item=presence_store.to_dynamo(item))


def public_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe view. Decimals become int or float. Internal keys are omitted."""
    data = presence_store.from_dynamo(item)
    view = {}
    for key in _PUBLIC_KEYS:
        if key not in data or data[key] is None:
            continue
        view[key] = data[key]
    return view


def _item(
    person: str,
    asked: datetime,
    source: str,
    room: Optional[str],
    answered_at: Optional[datetime],
    reading: Dict[str, Any],
    zone: ZoneInfo = CHICAGO,
    timezone_name: str = "America/Chicago",
) -> Dict[str, Any]:
    local = asked.astimezone(zone)
    pk, sk, checkin_id = _ids(person, asked)
    item: Dict[str, Any] = {
        "pk": pk,
        "sk": sk,
        "checkin_id": checkin_id,
        "person": person,
        "source": source,
        "asked_at": _iso(asked),
        "timezone": timezone_name,
        "local_hour": local.hour,
        "weekday": local.weekday(),
        "freshness": reading.get("freshness") or "stale",
    }
    if room:
        item["room"] = room
    if answered_at is not None:
        item["answered_at"] = _iso(answered_at)
    for key in (
        "device_hmac",
        "node",
        "signal",
        "signal_bucket",
        "last_seen",
        "is_home",
        "predicted_room",
        "predicted_confidence",
        "predicted_source",
    ):
        value = reading.get(key)
        if value is not None and value != "":
            item[key] = value
    return item


def _reading(person: str) -> Dict[str, Any]:
    """One eero snapshot for the person's tracked device. Never includes a MAC."""
    if not _tracked(person):
        raise CheckinError(400, "person is not tracked")
    session_name = os.environ.get("EERO_SESSION_SECRET_NAME", "").strip()
    try:
        token = eero_presence_handler.load_session_token(session_name) if session_name else ""
    except Exception:
        return {"freshness": "stale"}
    if not token:
        return {"freshness": "stale"}
    secret_name = os.environ.get("EERO_DEVICE_HMAC_SECRET_NAME", "").strip()
    try:
        key = room_fingerprint.load_hmac_key(secret_name) if secret_name else ""
    except Exception:
        key = ""
    network_id = os.environ.get("EERO_NETWORK_ID", "").strip() or None
    try:
        clients = eero_client.list_connected_clients(token, network_id=network_id)
    except eero_client.EeroAuthError:
        return {"freshness": "stale"}
    except eero_client.EeroError:
        return {"freshness": "stale"}
    match = eero_presence.connected_match(_tracked_rows(), clients, person, _iso(now()))
    if not match:
        return {"freshness": "fresh", "is_home": False}
    mac = str(match.get("mac") or "")
    digest = room_fingerprint.device_hmac(mac, key) if key and mac else ""
    signal = match.get("signal")
    bucket = room_fingerprint.signal_bucket(match.get("device"))
    reading: Dict[str, Any] = {
        "freshness": "fresh",
        "is_home": True,
        "node": match.get("node") if isinstance(match.get("node"), str) else None,
        "last_seen": match.get("last_seen") if isinstance(match.get("last_seen"), str) else None,
        "signal_bucket": bucket,
    }
    if isinstance(signal, str) and signal.strip():
        reading["signal"] = signal.strip()[:40]
    if digest:
        reading["device_hmac"] = digest
    reading.update(_presence_prediction(person))
    return reading


def _tracked(person: str) -> bool:
    wanted = person.casefold()
    return any(str(row.get("person") or "").casefold() == wanted for row in _tracked_rows())


def _tracked_rows() -> List[Dict[str, Any]]:
    table_name = os.environ.get("EERO_TRACKED_TABLE", "").strip()
    if not table_name:
        return []
    import aws_clients

    table = aws_clients.get_dynamodb_resource().Table(table_name)
    return room_fingerprint.load_all(table)


def _observed_reading(reading: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(reading, dict):
        return {"freshness": "stale"}
    safe: Dict[str, Any] = {}
    for key in ("device_hmac", "node", "signal", "signal_bucket", "freshness", "last_seen", "is_home"):
        if key in reading and reading[key] is not None and reading[key] != "":
            safe[key] = reading[key]
    return safe


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise CheckinError(400, "timezone is not a known zone") from exc


def _person(value: Any) -> str:
    try:
        return room_fingerprint.validate_person(value)
    except LabelError as exc:
        raise CheckinError(400, str(exc)) from exc


def _ids(person: str, asked: datetime) -> tuple:
    sk = f"{_compact(asked)}~{secrets.token_hex(4)}"
    person_fold = person.casefold()
    return _pk(person_fold), sk, f"{person_fold}~{sk}"


def _parse_id(checkin_id: Any) -> tuple:
    if not isinstance(checkin_id, str) or "~" not in checkin_id:
        raise CheckinError(400, "checkin_id is not valid")
    person_fold, sk = checkin_id.split("~", 1)
    try:
        room_fingerprint.validate_person(person_fold)
    except LabelError as exc:
        raise CheckinError(400, "checkin_id is not valid") from exc
    if not _SK_RE.fullmatch(sk):
        raise CheckinError(400, "checkin_id is not valid")
    return person_fold, sk


def _load(table: Any, checkin_id: Any) -> Optional[Dict[str, Any]]:
    person_fold, sk = _parse_id(checkin_id)
    item = table.get_item(Key={"pk": _pk(person_fold), "sk": sk}).get("Item")
    return item if isinstance(item, dict) else None


def _presence_prediction(person: str) -> Dict[str, Any]:
    """Room assignment on the home_state item at capture/record time (not re-read on label)."""
    table_name = os.environ.get("PRESENCE_TABLE_NAME", "").strip()
    if not table_name:
        return {}
    try:
        import aws_clients

        table = aws_clients.get_dynamodb_resource().Table(table_name)
        home = presence_store.read_home(table)
    except Exception:
        return {}
    if not home:
        return {}
    data = presence_store.from_dynamo(home)
    wanted = person.casefold()
    for raw in data.get("people") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("person") or "").casefold() != wanted:
            continue
        out: Dict[str, Any] = {}
        room = raw.get("room")
        if isinstance(room, str) and room.strip():
            out["predicted_room"] = room.strip()
        conf = raw.get("room_confidence")
        if conf is not None and not isinstance(conf, bool):
            try:
                out["predicted_confidence"] = round(float(conf), 2)
            except (TypeError, ValueError):
                pass
        src = raw.get("room_source")
        if isinstance(src, str) and src.strip():
            out["predicted_source"] = src.strip()
        return out
    return {}


def _query(table: Any, person: str, since: Optional[datetime], limit: int) -> List[Dict[str, Any]]:
    return _query_all(table, person, since)[:limit]


def _query_all(table: Any, person: str, since: Optional[datetime]) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    pk = _pk(person.casefold())
    condition = Key("pk").eq(pk)
    if since is not None:
        condition = condition & Key("sk").gte(_compact(since))
    found: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"KeyConditionExpression": condition, "ScanIndexForward": True}
    while True:
        resp = table.query(**kwargs)
        found.extend(item for item in (resp.get("Items") or []) if isinstance(item, dict))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return found
        kwargs["ExclusiveStartKey"] = last


def _scan(table: Any, since: Optional[datetime], limit: int) -> List[Dict[str, Any]]:
    """Scan, then keep the oldest rows. A scan is not ordered, so the limit applies after the sort."""
    found: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items") or []:
            if not isinstance(item, dict):
                continue
            if since is not None and str(item.get("sk") or "") < _compact(since):
                continue
            found.append(item)
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    found.sort(key=lambda item: str(item.get("sk") or ""))
    return found[:limit]


def _scan_all(table: Any, since: Optional[datetime]) -> List[Dict[str, Any]]:
    return _scan(table, since, limit=2**31 - 1)


def _pk(person_fold: str) -> str:
    return f"person#{person_fold}"


def _compact(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()
