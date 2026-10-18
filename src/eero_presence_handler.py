"""
Eero presence poller.

EventBridge Scheduler invokes this on a rate (default every 5 minutes).
It reads the session token from Secrets Manager, lists clients on eero's
unofficial cloud API, maps the configured devices to people, and writes
``pk=home_state`` through presence_store — the same item GET /presence and
the policy engine read.

The function is not in a VPC. The eero API is on the public internet, and
the private Lambda subnets only reach it through NAT. Keeping this function
out of the VPC preserves the pay-per-request model.

Fail-safe: a rejected session, a transport error, a blank secret, or an
empty tracked-device table never changes ``is_home``. The last presence is
kept and marked ``freshness=stale``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import aws_clients
import eero_client
import eero_presence
import presence_store
import room_fingerprint

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    del event, context  # Scheduler input is informational; the poll has no payload.
    table_name = os.environ.get("PRESENCE_TABLE_NAME", "").strip()
    if not table_name:
        logger.error(
            "PRESENCE_TABLE_NAME is unset — not polling and not marking anyone away"
        )
        return {"status": "skipped", "reason": "presence table unset"}

    table = aws_clients.get_dynamodb_resource().Table(table_name)
    secret_name = os.environ.get("EERO_SESSION_SECRET_NAME", "").strip()
    if not secret_name:
        logger.error(
            "EERO_SESSION_SECRET_NAME is blank — not polling and not marking anyone away"
        )
        return _mark_stale(table, "eero session secret name is blank")

    try:
        token = load_session_token(secret_name)
    except Exception as exc:
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        logger.error(
            "eero session secret unreadable (%s %s) — keeping last presence and "
            "marking it stale. Not marking anyone away.",
            exc.__class__.__name__,
            code,
        )
        return _mark_stale(table, "eero session secret unreadable")
    if not token:
        logger.error(
            "eero session secret has no session_token — keeping last presence and "
            "marking it stale. Not marking anyone away. Re-run scripts/eero_login.py."
        )
        return _mark_stale(table, "eero session secret has no session_token")

    network_id = os.environ.get("EERO_NETWORK_ID", "").strip() or None
    try:
        clients = eero_client.list_connected_clients(token, network_id=network_id)
    except eero_client.EeroAuthError as exc:
        logger.error(
            "eero session rejected (%s) — keeping last presence and marking it stale. "
            "Not marking anyone away. Re-run scripts/eero_login.py.",
            exc,
        )
        return _mark_stale(table, f"eero session rejected: {exc}")
    except eero_client.EeroError as exc:
        logger.error(
            "eero poll failed (%s) — keeping last presence and marking it stale. "
            "Not marking anyone away.",
            exc,
        )
        return _mark_stale(table, f"eero poll failed: {exc}")

    tracked_name = os.environ.get("EERO_TRACKED_TABLE", "").strip()
    tracked = _load_tracked(tracked_name)
    if not tracked:
        logger.error(
            "no tracked eero devices configured — keeping last presence and marking "
            "it stale. Not marking anyone away. Run scripts/eero_login.py track."
        )
        return _mark_stale(table, "no tracked eero devices configured")

    previous = presence_store.read_home(table)
    now = _now()
    updated = eero_presence.compute_presence(
        previous,
        tracked,
        clients,
        miss_threshold=_miss_threshold(),
        now=now,
    )
    if updated is None:
        logger.error(
            "tracked eero devices were unusable — keeping last presence and marking "
            "it stale. Not marking anyone away."
        )
        return _mark_stale(table, "tracked eero devices were unusable")

    _assign_rooms(updated, previous, tracked, clients)
    _apply_schedule(updated, previous)
    try:
        presence_store.write_home(table, updated)
    except Exception as exc:
        # Scheduler invokes this asynchronously and retries a raised error.
        # Returning leaves the stored item unchanged (last is_home kept) so a
        # retry does not poll eero again in the same minute or mark anyone away.
        logger.error(
            "eero presence write failed (%s) — stored item unchanged, not marking anyone away",
            exc.__class__.__name__,
        )
        return {
            "status": "write_failed",
            "is_home": bool((previous or {}).get("is_home", True)),
            "freshness": (previous or {}).get("freshness") or "unknown",
            "room": (previous or {}).get("room") or "unknown",
        }
    logger.info(
        "eero presence updated is_home=%s freshness=%s zone=%s room=%s people=%s",
        updated.get("is_home"),
        updated.get("freshness"),
        updated.get("zone"),
        updated.get("room"),
        [person.get("person") for person in updated.get("people") or []],
    )
    return {
        "status": "updated",
        "is_home": bool(updated.get("is_home")),
        "freshness": "fresh",
        "zone": updated.get("zone"),
        "room": updated.get("room") or "unknown",
        "room_confidence": updated.get("room_confidence") or 0,
    }


def load_session_token(secret_name: str, client: Any = None) -> str:
    """Read the session token. GetSecretValue only; the value is never logged."""
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_name)
    return eero_client.parse_secret_string(resp.get("SecretString") or "")


def _mark_stale(table: Any, reason: str) -> Dict[str, Any]:
    previous = presence_store.read_home(table)
    was_home = True if not previous else bool(previous.get("is_home", True))
    item = presence_store.apply_stale(previous, reason, _now())
    # Belt: this path keeps the last occupancy and never invents an away.
    if was_home:
        item["is_home"] = True
    try:
        presence_store.write_home(table, item)
    except Exception as exc:
        logger.error(
            "eero presence stale write failed (%s) — stored item unchanged, not marking anyone away",
            exc.__class__.__name__,
        )
        return {
            "status": "stale",
            "is_home": was_home,
            "freshness": "stale",
            "reason": reason,
            "persisted": False,
        }
    logger.info(
        "eero presence left unchanged is_home=%s freshness=stale reason=%s",
        item.get("is_home"),
        reason,
    )
    return {
        "status": "stale",
        "is_home": bool(item.get("is_home")),
        "freshness": "stale",
        "reason": reason,
    }


def _assign_rooms(
    updated: Dict[str, Any],
    previous: Any,
    tracked: List[Dict[str, Any]],
    clients: List[Dict[str, Any]],
) -> None:
    """Classify the live reading. A missing table or key leaves room unset.

    Occupancy is already decided. A failure here must not mark anyone away
    and must not write a guessed room.
    """
    table_name = os.environ.get("ROOM_FINGERPRINT_TABLE", "").strip()
    secret_name = os.environ.get("EERO_DEVICE_HMAC_SECRET_NAME", "").strip()
    if not table_name or not secret_name:
        return
    try:
        key = room_fingerprint.load_hmac_key(secret_name)
    except Exception:
        logger.error("room classification skipped reason=hmac_secret_unreadable")
        room_fingerprint.carry_forward(updated, previous)
        return
    if not key:
        logger.error("room classification skipped reason=hmac_secret_empty")
        room_fingerprint.carry_forward(updated, previous)
        return
    try:
        fingerprints = room_fingerprint.load_all(
            aws_clients.get_dynamodb_resource().Table(table_name)
        )
    except Exception:
        logger.error("room classification skipped reason=fingerprint_table_unreadable")
        room_fingerprint.carry_forward(updated, previous)
        return
    readings = _readings(tracked, clients, key)
    room_fingerprint.apply_rooms(updated, previous, readings, fingerprints)
    logger.info(
        "room classification room=%s confidence=%s",
        updated.get("room"),
        updated.get("room_confidence"),
    )


def _apply_schedule(updated: Dict[str, Any], previous: Any) -> None:
    """Apply a home time-window rule and record one training row per hour.

    A missing table or a failed check-in write does not change occupancy
    and does not undo the room the rule just set.
    """
    table_name = os.environ.get("ROOM_SCHEDULE_TABLE", "").strip()
    if not table_name:
        return
    try:
        import room_schedule

        rules = room_schedule.load_rules(
            aws_clients.get_dynamodb_resource().Table(table_name)
        )
        moment = datetime.fromisoformat(_now())
        applied = room_schedule.apply_rules(updated, rules, moment)
    except Exception:
        logger.error("room schedule skipped reason=unreadable")
        return
    if applied:
        logger.info(
            "room schedule room=%s source=%s",
            updated.get("room"),
            updated.get("room_source"),
        )
    _write_schedule_checkins(updated, previous, applied, moment)


def _write_schedule_checkins(
    updated: Dict[str, Any],
    previous: Any,
    applied: List[Dict[str, Any]],
    moment: datetime,
) -> None:
    if not applied:
        return
    import room_checkin
    import room_schedule

    table = None
    table_name = os.environ.get("ROOM_CHECKIN_TABLE", "").strip()
    if table_name:
        try:
            table = aws_clients.get_dynamodb_resource().Table(table_name)
        except Exception:
            logger.error("schedule check-in skipped reason=table_unreadable")
    prev_people = {
        str(person.get("person") or "").casefold(): person
        for person in (previous or {}).get("people") or []
        if isinstance(person, dict)
    }
    current = {
        str(person.get("person") or "").casefold(): person
        for person in updated.get("people") or []
        if isinstance(person, dict)
    }
    for match in applied:
        person = current.get(str(match.get("person") or "").casefold())
        rule = match.get("rule") or {}
        if not isinstance(person, dict) or not rule:
            continue
        try:
            misses = int(person.get("miss_count") or 0)
        except (TypeError, ValueError):
            misses = 0
        if misses > 0:
            continue
        zone_name = str(rule.get("timezone") or room_schedule.DEFAULT_TIMEZONE)
        try:
            key = room_schedule.hour_key(moment, zone_name)
        except Exception:
            continue
        previous_person = prev_people.get(str(person.get("person") or "").casefold()) or {}
        if previous_person.get("schedule_checkin_key") == key:
            person["schedule_checkin_key"] = key
            continue
        if table is None:
            continue
        signal = person.get("signal") if isinstance(person.get("signal"), str) else None
        reading: Dict[str, Any] = {
            "node": person.get("node"),
            "signal": signal,
            "freshness": "fresh",
            "last_seen": person.get("last_seen"),
            "is_home": True,
        }
        if person.get("device_hmac"):
            reading["device_hmac"] = person["device_hmac"]
        bucket = room_fingerprint.signal_bucket({"connectivity": {"signal": signal}})
        if bucket is not None:
            reading["signal_bucket"] = bucket
        try:
            room_checkin.store_observed(
                table,
                person.get("person"),
                rule.get("room"),
                reading,
                source=room_schedule.SCHEDULE_SOURCE,
                at=moment,
                timezone_name=zone_name,
            )
        except Exception as exc:
            logger.error("schedule check-in failed (%s)", exc.__class__.__name__)
            continue
        person["schedule_checkin_key"] = key
        logger.info("schedule check-in stored room=%s", rule.get("room"))


def _readings(
    tracked: List[Dict[str, Any]],
    clients: List[Dict[str, Any]],
    key: str,
) -> Dict[str, Dict[str, Any]]:
    now = _now()
    people: List[str] = []
    for row in tracked:
        person = str(row.get("person") or "").strip()
        if person and person.casefold() not in {item.casefold() for item in people}:
            people.append(person)
    readings: Dict[str, Dict[str, Any]] = {}
    for person in people:
        match = eero_presence.connected_match(tracked, clients, person, now)
        if not match:
            continue
        digest = room_fingerprint.device_hmac(str(match.get("mac") or ""), key)
        readings[person.casefold()] = {
            "node": match.get("node"),
            "zone": match.get("zone"),
            "bucket": room_fingerprint.signal_bucket(match.get("device")),
            "device_hmac": digest,
        }
    return readings


def _load_tracked(table_name: str) -> List[Dict[str, Any]]:
    if not table_name:
        return []
    table = aws_clients.get_dynamodb_resource().Table(table_name)
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items") or [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _miss_threshold() -> int:
    raw = os.environ.get("EERO_AWAY_MISS_THRESHOLD", "3").strip() or "3"
    try:
        value = int(raw)
    except ValueError:
        return 3
    return value if value >= 1 else 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
