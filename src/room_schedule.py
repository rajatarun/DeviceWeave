"""Fixed local-time room rules.

A rule says a person is in one room between two clock times, in one
timezone. The window may wrap midnight (23:00–06:00). The presence
poller applies a matching rule when that person is home, and writes one
labeled check-in per local hour so the overnight hours are training
data. Rules live in DynamoDB. This module does not call eero.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import room_fingerprint
from room_fingerprint import LabelError

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_TIMEZONE = "America/Chicago"
SCHEDULE_SOURCE = "schedule_rule"
HIGH_CONFIDENCE = 1.0


class ScheduleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_clock(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value.strip()):
        raise ScheduleError(400, f"{field} must be HH:MM in 24-hour time")
    return value.strip()


def validate_timezone(value: Any) -> str:
    if value is None or value == "":
        return DEFAULT_TIMEZONE
    if not isinstance(value, str):
        raise ScheduleError(400, "timezone must be an IANA name")
    name = value.strip()
    try:
        ZoneInfo(name)
    except Exception as exc:
        raise ScheduleError(400, "timezone must be an IANA name") from exc
    return name


def rule_id_for(person: str, start: str, end: str) -> str:
    return f"{person.casefold()}~{start}~{end}"


def parse_rule_id(rule_id: Any) -> tuple:
    if not isinstance(rule_id, str) or rule_id.count("~") != 2:
        raise ScheduleError(400, "rule_id is not valid")
    person_fold, start, end = rule_id.split("~")
    try:
        room_fingerprint.validate_person(person_fold)
    except LabelError as exc:
        raise ScheduleError(400, "rule_id is not valid") from exc
    if not _TIME_RE.fullmatch(start) or not _TIME_RE.fullmatch(end) or start == end:
        raise ScheduleError(400, "rule_id is not valid")
    return person_fold, start, end


def in_window(start: str, end: str, local: datetime) -> bool:
    """True when ``local``'s clock time is in ``[start, end)``.

    If ``end`` is earlier than ``start``, the window wraps midnight.
    """
    start_m = _minutes(start)
    end_m = _minutes(end)
    if start_m == end_m:
        return False
    current = local.hour * 60 + local.minute
    if start_m < end_m:
        return start_m <= current < end_m
    return current >= start_m or current < end_m


def set_rule(
    table: Any,
    person: Any,
    room: Any,
    start: Any,
    end: Any,
    timezone_name: Any = None,
) -> Dict[str, Any]:
    try:
        person_name = room_fingerprint.validate_person(person)
        room_name = room_fingerprint.validate_room(room)
    except LabelError as exc:
        raise ScheduleError(400, str(exc)) from exc
    start_clock = validate_clock(start, "start")
    end_clock = validate_clock(end, "end")
    if start_clock == end_clock:
        raise ScheduleError(400, "start and end must be different")
    zone = validate_timezone(timezone_name)
    item = _item(person_name, room_name, start_clock, end_clock, zone)
    others = [
        existing for existing in load_rules(table)
        if str(existing.get("person") or "").casefold() == person_name.casefold()
        and existing.get("rule_id") != item["rule_id"]
    ]
    for existing in others:
        if _overlaps(start_clock, end_clock, str(existing.get("start")), str(existing.get("end"))):
            raise ScheduleError(400, "that window overlaps another rule for this person")
    import presence_store

    table.put_item(Item=presence_store.to_dynamo(item))
    return public_rule(item)


def list_rules(table: Any, person: Any = None) -> List[Dict[str, Any]]:
    person_name = None
    if person:
        try:
            person_name = room_fingerprint.validate_person(person)
        except LabelError as exc:
            raise ScheduleError(400, str(exc)) from exc
    rows = []
    for item in load_rules(table):
        if person_name and str(item.get("person") or "").casefold() != person_name.casefold():
            continue
        rows.append(public_rule(item))
    rows.sort(key=lambda row: (str(row.get("person") or "").casefold(), str(row.get("start") or "")))
    return rows


def delete_rule(table: Any, rule_id: Any) -> int:
    person_fold, start, end = parse_rule_id(rule_id)
    key = {"pk": _pk(person_fold), "sk": _sk(start, end)}
    existing = table.get_item(Key=key).get("Item")
    if not existing:
        return 0
    table.delete_item(Key=key)
    return 1


def load_rules(table: Any) -> List[Dict[str, Any]]:
    return room_fingerprint.load_all(table)


def apply_rules(
    updated: Dict[str, Any],
    rules: List[Dict[str, Any]],
    moment: datetime,
) -> List[Dict[str, Any]]:
    """Overwrite home rooms that fall inside a rule. Returns the matches.

    Confidence is 1.0 and ``room_source`` is ``schedule_rule``. The
    displayed room does not wait for a second poll. Away is left alone.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    applied: List[Dict[str, Any]] = []
    for person in updated.get("people") or []:
        if not isinstance(person, dict):
            continue
        if not person.get("is_home"):
            person.pop("room_source", None)
            continue
        rule = matching_rule(rules, person.get("person"), moment)
        if rule is None:
            person.pop("room_source", None)
            continue
        person["room"] = rule["room"]
        person["room_confidence"] = HIGH_CONFIDENCE
        person["room_source"] = SCHEDULE_SOURCE
        person["room_pending"] = rule["room"]
        person["room_pending_count"] = room_fingerprint.SMOOTHING_READINGS
        applied.append({"person": person.get("person"), "rule": rule})
    room_fingerprint._aggregate_room(updated)
    return applied


def matching_rule(
    rules: List[Dict[str, Any]],
    person: Any,
    moment: datetime,
) -> Optional[Dict[str, Any]]:
    wanted = str(person or "").casefold()
    if not wanted:
        return None
    found: List[Dict[str, Any]] = []
    for rule in rules:
        if str(rule.get("person") or "").casefold() != wanted:
            continue
        zone_name = str(rule.get("timezone") or DEFAULT_TIMEZONE)
        try:
            local = moment.astimezone(ZoneInfo(zone_name))
        except Exception:
            continue
        if in_window(str(rule.get("start") or ""), str(rule.get("end") or ""), local):
            found.append(rule)
    if not found:
        return None
    found.sort(key=lambda rule: str(rule.get("start") or ""))
    return public_rule(found[0])


def hour_key(moment: datetime, timezone_name: str) -> str:
    local = moment.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%Y-%m-%dT%H")


def public_rule(item: Dict[str, Any]) -> Dict[str, Any]:
    import presence_store

    data = presence_store.from_dynamo(item)
    view = {}
    for key in ("rule_id", "person", "room", "start", "end", "timezone"):
        if data.get(key) is not None:
            view[key] = data[key]
    return view


def _item(person: str, room: str, start: str, end: str, timezone_name: str) -> Dict[str, Any]:
    person_fold = person.casefold()
    return {
        "pk": _pk(person_fold),
        "sk": _sk(start, end),
        "rule_id": rule_id_for(person, start, end),
        "person": person,
        "room": room,
        "start": start,
        "end": end,
        "timezone": timezone_name,
    }


def _overlaps(start: str, end: str, other_start: str, other_end: str) -> bool:
    """True when the two half-open clock windows share a minute."""
    if not (_TIME_RE.fullmatch(other_start) and _TIME_RE.fullmatch(other_end)):
        return False
    for minute in range(24 * 60):
        if _contains(start, end, minute) and _contains(other_start, other_end, minute):
            return True
    return False


def _contains(start: str, end: str, minute: int) -> bool:
    start_m = _minutes(start)
    end_m = _minutes(end)
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= minute < end_m
    return minute >= start_m or minute < end_m


def _minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _pk(person_fold: str) -> str:
    return f"person#{person_fold}"


def _sk(start: str, end: str) -> str:
    return f"{start}~{end}"
