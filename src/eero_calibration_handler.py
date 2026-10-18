"""
Room calibration Lambda.

HTTP routes (same bearer token as POST /mcp):

  POST   /presence/rooms/calibrate     start a sample run, return 202
  GET    /presence/rooms                list stored fingerprints
  DELETE /presence/rooms/{room}         delete one room's samples

API Gateway's integration limit is about 30 seconds, and a minute of
eero reads does not fit in that. The HTTP request checks the room name,
the tracked person, and the secrets, then invokes this function again
asynchronously to take the samples. List and delete run in the request.

The function is not in a VPC, for the same reason as the presence
poller: eero's API is on the public internet. It is the only role that
can write the fingerprint table. The poller can read that table.

Direct invokes (the async sample run, and the MCP tools, which call
this function rather than writing the table themselves) use ``action``
of ``collect``, ``start``, ``list``, or ``delete``. Those invokes are
IAM-gated. The public routes are the HTTP ones, and they require the
bearer token.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import aws_clients
import eero_client
import eero_presence
import eero_presence_handler
import room_fingerprint
from room_fingerprint import LabelError

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_MAX_BODY = 8000


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return _error(400, "bad request")
    if _http_method(event):
        return _http(event)
    action = event.get("action")
    if action == "collect":
        return collect_room(event.get("person"), event.get("room"), event.get("samples"))
    if action == "start":
        return start_job(event.get("person"), event.get("room"), event.get("samples"))
    if action == "list":
        return list_rooms(event.get("person"))
    if action == "delete":
        return delete_room(event.get("room"), event.get("person"))
    return _error(400, "unknown action")


def start_job(person: Any, room: Any, samples: Any) -> Dict[str, Any]:
    """Validate and queue a sample run. Returns an API Gateway response."""
    try:
        person_name = room_fingerprint.validate_person(person)
        room_name = room_fingerprint.validate_room(room)
        sample_count = room_fingerprint.validate_samples(samples)
    except LabelError as exc:
        return _error(400, str(exc))
    if not os.environ.get("ROOM_FINGERPRINT_TABLE", "").strip():
        return _error(503, "Room fingerprint table is not configured")
    secret_name = os.environ.get("EERO_DEVICE_HMAC_SECRET_NAME", "").strip()
    if not secret_name:
        return _error(503, "Device HMAC secret is not configured")
    session_name = os.environ.get("EERO_SESSION_SECRET_NAME", "").strip()
    if not session_name:
        return _error(503, "eero session secret is not configured")
    try:
        if not room_fingerprint.load_hmac_key(secret_name):
            logger.error("calibration refused room=%s reason=hmac_secret_empty", room_name)
            return _error(503, "Device HMAC secret is empty")
    except Exception:
        logger.error("calibration refused room=%s reason=hmac_secret_unreadable", room_name)
        return _error(503, "Device HMAC secret is unreadable")
    try:
        if not eero_presence_handler.load_session_token(session_name):
            logger.error("calibration refused room=%s reason=session_secret_empty", room_name)
            return _error(503, "eero session secret has no session_token")
    except Exception:
        logger.error("calibration refused room=%s reason=session_secret_unreadable", room_name)
        return _error(503, "eero session secret is unreadable")
    if not _person_is_tracked(person_name):
        logger.info("calibration refused room=%s reason=person_not_tracked", room_name)
        return _error(400, "person is not tracked")
    payload = {
        "action": "collect",
        "person": person_name,
        "room": room_name,
        "samples": sample_count,
    }
    try:
        _invoke_async(payload)
    except Exception:
        logger.error("calibration start failed room=%s", room_name)
        return _error(502, "Could not start room calibration")
    logger.info("calibration started room=%s samples=%s", room_name, sample_count)
    return _json(202, {
        "status": "started",
        "person": person_name,
        "room": room_name,
        "samples": sample_count,
        "interval_seconds": _interval(),
    })


def collect_room(person: Any, room: Any, samples: Any) -> Dict[str, Any]:
    """Read eero ``samples`` times and replace that room's fingerprint.

    A failed read or a minute with no connected device leaves the previous
    samples in place. Recalibrating a room that already has samples
    overwrites them.
    """
    try:
        person_name = room_fingerprint.validate_person(person)
        room_name = room_fingerprint.validate_room(room)
        sample_count = room_fingerprint.validate_samples(samples)
    except LabelError:
        logger.error("calibration collect rejected reason=invalid_label")
        return {"status": "rejected", "reason": "invalid_label"}

    table = _fingerprint_table()
    if table is None:
        logger.error("calibration collect skipped room=%s reason=table_unset", room_name)
        return {"status": "unchanged", "room": room_name, "samples": 0}
    secret_name = os.environ.get("EERO_DEVICE_HMAC_SECRET_NAME", "").strip()
    session_name = os.environ.get("EERO_SESSION_SECRET_NAME", "").strip()
    try:
        key = room_fingerprint.load_hmac_key(secret_name) if secret_name else ""
        token = eero_presence_handler.load_session_token(session_name) if session_name else ""
    except Exception:
        logger.error("calibration collect skipped room=%s reason=secret_unreadable", room_name)
        return {"status": "unchanged", "room": room_name, "samples": 0}
    if not key or not token:
        logger.error("calibration collect skipped room=%s reason=secret_empty", room_name)
        return {"status": "unchanged", "room": room_name, "samples": 0}

    tracked = _load_tracked()
    interval = _interval()
    collected: List[Dict[str, Any]] = []
    digest = ""
    network_id = os.environ.get("EERO_NETWORK_ID", "").strip() or None
    for index in range(sample_count):
        if index and interval > 0:
            _sleep(interval)
        try:
            clients = eero_client.list_connected_clients(token, network_id=network_id)
        except eero_client.EeroAuthError:
            logger.error("calibration stopped room=%s reason=session_rejected", room_name)
            return {"status": "unchanged", "room": room_name, "samples": 0}
        except eero_client.EeroError:
            logger.error("calibration stopped room=%s reason=poll_failed", room_name)
            return {"status": "unchanged", "room": room_name, "samples": 0}
        match = eero_presence.connected_match(tracked, clients, person_name, _now())
        if not match:
            continue
        mac = str(match.get("mac") or "")
        sample_digest = room_fingerprint.device_hmac(mac, key)
        node = match.get("node")
        if not sample_digest or not isinstance(node, str) or not node.strip():
            continue
        if digest and sample_digest != digest:
            continue
        digest = sample_digest
        bucket = room_fingerprint.signal_bucket(match.get("device"))
        sample: Dict[str, Any] = {"node": node.strip(), "taken_at": _now()}
        if bucket is not None:
            sample["bucket"] = bucket
        signal = match.get("signal")
        if isinstance(signal, str) and signal.strip():
            sample["signal"] = signal.strip()
        collected.append(sample)

    if not collected or not digest:
        logger.info("calibration kept existing room=%s samples=0", room_name)
        return {"status": "unchanged", "room": room_name, "samples": 0}

    item = room_fingerprint.fingerprint_item(person_name, room_name, digest, collected, _now())
    room_fingerprint.save_fingerprint(table, item)
    logger.info("calibration stored room=%s samples=%s", room_name, item["sample_count"])
    return {"status": "stored", "room": room_name, "samples": item["sample_count"]}


def list_rooms(person: Any) -> Dict[str, Any]:
    try:
        person_name = room_fingerprint.validate_person(person) if person else None
    except LabelError as exc:
        return _error(400, str(exc))
    table = _fingerprint_table()
    if table is None:
        return _error(503, "Room fingerprint table is not configured")
    rooms = room_fingerprint.list_fingerprints(table, person_name)
    logger.info("calibration listed rooms=%s", len(rooms))
    return _json(200, {"rooms": rooms})


def delete_room(room: Any, person: Any) -> Dict[str, Any]:
    try:
        room_name = room_fingerprint.validate_room(room)
        person_name = room_fingerprint.validate_person(person) if person else None
    except LabelError as exc:
        return _error(400, str(exc))
    table = _fingerprint_table()
    if table is None:
        return _error(503, "Room fingerprint table is not configured")
    deleted = room_fingerprint.delete_fingerprints(table, room_name, person_name)
    logger.info("calibration deleted room=%s count=%s", room_name, deleted)
    return _json(200, {"room": room_name, "deleted": deleted})


def _http(event: Dict[str, Any]) -> Dict[str, Any]:
    denied = _authorized(event)
    if denied is not None:
        return denied
    method = _http_method(event)
    path = _http_path(event)
    route = _route(method, path)
    if route == "calibrate":
        try:
            body = _body_json(event)
        except LabelError as exc:
            return _error(400, str(exc))
        return start_job(body.get("person"), body.get("room"), body.get("samples"))
    if route == "list":
        return list_rooms(_query(event, "person"))
    if route == "delete":
        return delete_room(_room_from_path(event, path), _query(event, "person"))
    return _error(404, "not found")


def _authorized(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from mcp_handler import (
        McpAuthUnavailable,
        _bearer_token,
        _load_expected_token,
        _normalize_headers,
        tokens_equal,
    )

    provided = _bearer_token(_normalize_headers(event.get("headers")))
    if not provided:
        return _unauthorized()
    try:
        expected = _load_expected_token()
    except McpAuthUnavailable:
        logger.error("calibration auth unavailable")
        return _unauthorized()
    if not expected or not tokens_equal(provided, expected):
        return _unauthorized()
    return None


def _route(method: str, path: str) -> str:
    trimmed = path.rstrip("/")
    if method == "POST" and trimmed.endswith("/presence/rooms/calibrate"):
        return "calibrate"
    if method == "GET" and trimmed.endswith("/presence/rooms"):
        return "list"
    if method == "DELETE" and "/presence/rooms/" in trimmed:
        return "delete"
    return ""


def _room_from_path(event: Dict[str, Any], path: str) -> str:
    params = event.get("pathParameters") or {}
    if isinstance(params, dict) and params.get("room"):
        return urllib.parse.unquote(str(params["room"]))
    tail = path.rstrip("/").split("/presence/rooms/", 1)[-1]
    return urllib.parse.unquote(tail)


def _query(event: Dict[str, Any], key: str) -> Optional[str]:
    query = event.get("queryStringParameters") or {}
    if not isinstance(query, dict):
        return None
    value = query.get(key)
    if value is None:
        return None
    return str(value)


def _body_json(event: Dict[str, Any]) -> Dict[str, Any]:
    body = event.get("body")
    if body is None or body == "":
        return {}
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        raise LabelError("body must be JSON")
    if event.get("isBase64Encoded") and body:
        import base64

        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise LabelError("body must be JSON") from exc
    if len(body) > _MAX_BODY:
        raise LabelError("body is too large")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LabelError("body must be JSON") from exc
    if not isinstance(parsed, dict):
        raise LabelError("body must be a JSON object")
    return parsed


def _person_is_tracked(person: str) -> bool:
    rows = _load_tracked()
    wanted = person.casefold()
    return any(str(row.get("person") or "").casefold() == wanted for row in rows)


def _load_tracked() -> List[Dict[str, Any]]:
    table_name = os.environ.get("EERO_TRACKED_TABLE", "").strip()
    if not table_name:
        return []
    table = aws_clients.get_dynamodb_resource().Table(table_name)
    return room_fingerprint.load_all(table)


def _fingerprint_table() -> Any:
    table_name = os.environ.get("ROOM_FINGERPRINT_TABLE", "").strip()
    if not table_name:
        return None
    return aws_clients.get_dynamodb_resource().Table(table_name)


def _invoke_async(payload: Dict[str, Any]) -> None:
    name = os.environ.get("EERO_CALIBRATION_FUNCTION_NAME", "").strip()
    if not name:
        raise RuntimeError("calibration function name is unset")
    import boto3

    boto3.client("lambda").invoke(
        FunctionName=name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def _interval() -> float:
    raw = os.environ.get("EERO_CALIBRATION_INTERVAL_SECONDS", "").strip()
    if not raw:
        return float(room_fingerprint.SAMPLE_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(room_fingerprint.SAMPLE_INTERVAL_SECONDS)
    if value < 0 or value > 60:
        return float(room_fingerprint.SAMPLE_INTERVAL_SECONDS)
    return value


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _http_method(event: Dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    if not isinstance(http, dict):
        return ""
    return str(http.get("method") or "").upper()


def _http_path(event: Dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    if isinstance(http, dict) and http.get("path"):
        return str(http["path"])
    return str(event.get("rawPath") or "")


def _json(status: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error(status: int, message: str) -> Dict[str, Any]:
    return _json(status, {"error": message})


def _unauthorized() -> Dict[str, Any]:
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json",
            "WWW-Authenticate": 'Bearer realm="deviceweave"',
        },
        "body": json.dumps({"error": "Unauthorized"}),
    }
