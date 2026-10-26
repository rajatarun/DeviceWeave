"""Room classifier trained from labeled check-ins.

Uses a dependency-free multinomial Naive Bayes model over coarse features
(node, signal bucket, local hour, weekday). Parameters are stored in the
room fingerprint DynamoDB table under ``pk=classifier#<person>`` so the
presence poller can load them with the same scan it already performs.

Inference runs before fingerprint matching when a model exists; fingerprint
classification is the fallback. Schedule rules still overwrite the room
after assignment (see ``room_schedule.apply_rules``).
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import room_fingerprint
from room_fingerprint import CONFIDENCE_FLOOR

MODEL_SK = "model#v1"
MODEL_VERSION = 1
DEFAULT_TIMEZONE = "America/Chicago"
SMOOTHING = 1.0  # Laplace add-one
MIN_TRAIN_ROWS = 8
MARGIN_FLOOR = 0.12  # top-two log-prob gap before calling a winner
NOT_HOME_LABELS = frozenset({"not home"})


class TrainError(ValueError):
    """Too little data or unusable labels for training."""


def classifier_pk(person: str) -> str:
    return "classifier#" + room_fingerprint.validate_person(person).casefold()


def is_classifier_item(item: Mapping[str, Any]) -> bool:
    pk = str(item.get("pk") or "")
    sk = str(item.get("sk") or "")
    return pk.startswith("classifier#") and sk == MODEL_SK


def fingerprint_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in items if isinstance(item, dict) and not is_classifier_item(item)]


def load_all_including_models(table: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(item for item in (resp.get("Items") or []) if isinstance(item, dict))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def load_models(items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map casefolded person name to a parsed model dict."""
    models: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not is_classifier_item(item):
            continue
        person_fold = str(item.get("pk") or "")[len("classifier#") :]
        payload = item.get("model")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict):
            models[person_fold] = payload
    return models


def model_item(person: str, model: Dict[str, Any], updated_at: str) -> Dict[str, Any]:
    return {
        "pk": classifier_pk(person),
        "sk": MODEL_SK,
        "person": room_fingerprint.validate_person(person),
        "model_version": MODEL_VERSION,
        "updated_at": updated_at,
        "sample_count": int(model.get("sample_count") or 0),
        "model": json.dumps(model, separators=(",", ":"), sort_keys=True),
    }


def save_model(table: Any, person: str, model: Dict[str, Any], updated_at: str) -> None:
    import presence_store

    table.put_item(Item=presence_store.to_dynamo(model_item(person, model, updated_at)))


def canonical_room_labels(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Map casefold room key to the most common original spelling."""
    counts: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        room = row.get("room")
        if not isinstance(room, str) or not room.strip():
            continue
        fold = room.strip().casefold()
        if fold in NOT_HOME_LABELS:
            continue
        counts[fold][room.strip()] += 1
    canonical: Dict[str, str] = {}
    for fold, counter in counts.items():
        canonical[fold] = counter.most_common(1)[0][0]
    return canonical


def normalize_training_room(room: Any, canonical: Mapping[str, str]) -> Optional[str]:
    if not isinstance(room, str) or not room.strip():
        return None
    label = room.strip()
    fold = label.casefold()
    if fold in NOT_HOME_LABELS:
        return None
    return canonical.get(fold, label)


def node_key(value: Any) -> str:
    return room_fingerprint._node_key(value)


def features_from_checkin(row: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    node = node_key(row.get("node"))
    bucket = room_fingerprint._as_bucket(row.get("signal_bucket"))
    hour = room_fingerprint._as_int(row.get("local_hour"))
    weekday = room_fingerprint._as_int(row.get("weekday"))
    if not node and bucket is None:
        return None
    bucket_s = str(bucket) if bucket is not None else "_none"
    node_s = node or "_none"
    return {
        "node": node_s,
        "bucket": bucket_s,
        "node_bucket": f"{node_s}|{bucket_s}",
        "hour": str(hour) if hour is not None and 0 <= hour <= 23 else "_none",
        "weekday": str(weekday) if weekday is not None and 0 <= weekday <= 6 else "_none",
    }


def features_from_reading(
    reading: Mapping[str, Any],
    moment: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> Optional[Dict[str, str]]:
    node = node_key(reading.get("node"))
    bucket = room_fingerprint._as_bucket(reading.get("bucket"))
    if not node and bucket is None:
        return None
    try:
        local = moment.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = moment.astimezone(timezone.utc)
    bucket_s = str(bucket) if bucket is not None else "_none"
    node_s = node or "_none"
    return {
        "node": node_s,
        "bucket": bucket_s,
        "node_bucket": f"{node_s}|{bucket_s}",
        "hour": str(local.hour),
        "weekday": str(local.weekday()),
    }


def train_rows(
    checkins: Sequence[Mapping[str, Any]],
    *,
    person: Optional[str] = None,
) -> Tuple[List[Tuple[Dict[str, str], str]], Dict[str, str]]:
    """Return (labeled feature rows, canonical room map). Room keys are casefold."""
    wanted = person.casefold() if person else None
    canonical = canonical_room_labels(checkins)
    labeled: List[Tuple[Dict[str, str], str]] = []
    for row in checkins:
        if wanted and str(row.get("person") or "").casefold() != wanted:
            continue
        room_key = normalize_training_room(row.get("room"), canonical)
        if room_key is None:
            continue
        feats = features_from_checkin(row)
        if feats is None:
            continue
        labeled.append((feats, room_key.casefold()))
    return labeled, {fold: canonical[fold] for fold in {r for _, r in labeled}}


def _count_table() -> Dict[str, Dict[str, Counter]]:
    return {
        "node": defaultdict(Counter),
        "bucket": defaultdict(Counter),
        "node_bucket": defaultdict(Counter),
        "hour": defaultdict(Counter),
        "weekday": defaultdict(Counter),
    }


def build_model(
    labeled: Sequence[Tuple[Dict[str, str], str]],
    canonical: Mapping[str, str],
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if len(labeled) < MIN_TRAIN_ROWS:
        raise TrainError(f"need at least {MIN_TRAIN_ROWS} labeled rows with node or bucket")
    room_counts: Counter = Counter(room for _, room in labeled)
    tables = _count_table()
    for feats, room in labeled:
        for key in ("node", "bucket", "node_bucket", "hour", "weekday"):
            tables[key][feats[key]][room] += 1
    return {
        "version": MODEL_VERSION,
        "timezone": timezone_name,
        "sample_count": len(labeled),
        "rooms": sorted(room_counts.keys()),
        "priors": dict(room_counts),
        "canonical": {fold: canonical.get(fold, fold) for fold in room_counts.keys()},
        "counts": {
            field: {value: dict(counter) for value, counter in field_map.items()}
            for field, field_map in tables.items()
        },
        "metrics": metrics or {},
    }


def _log_likelihood(model: Mapping[str, Any], room: str, feats: Mapping[str, str]) -> float:
    priors = model.get("priors") or {}
    total_prior = sum(int(v) for v in priors.values()) or 1
    score = math.log((int(priors.get(room, 0)) + SMOOTHING) / (total_prior + SMOOTHING * len(priors)))
    counts = model.get("counts") or {}
    for field in ("node", "bucket", "node_bucket", "hour", "weekday"):
        field_counts = counts.get(field) or {}
        value_counts = field_counts.get(feats[field], {})
        total = sum(int(v) for v in value_counts.values())
        vocab = max(len(value_counts), 1)
        score += math.log((int(value_counts.get(room, 0)) + SMOOTHING) / (total + SMOOTHING * vocab))
    return score


def predict(model: Mapping[str, Any], feats: Mapping[str, str]) -> Tuple[str, float]:
    rooms = model.get("rooms") or []
    if not rooms:
        return "unknown", 0.0
    scored = [(room, _log_likelihood(model, room, feats)) for room in rooms]
    scored.sort(key=lambda item: item[1], reverse=True)
    best_room, best_log = scored[0]
    second_log = scored[1][1] if len(scored) > 1 else best_log - 1.0
    logs = [log_p for _, log_p in scored]
    max_log = max(logs)
    probs = [math.exp(log_p - max_log) for log_p in logs]
    total = sum(probs) or 1.0
    confidence = round(probs[0] / total, 2)
    if confidence < CONFIDENCE_FLOOR:
        return "unknown", confidence
    if (best_log - second_log) < MARGIN_FLOOR:
        return "unknown", confidence
    canonical = (model.get("canonical") or {}).get(best_room, best_room)
    try:
        display = room_fingerprint.validate_room(canonical)
    except room_fingerprint.LabelError:
        display = canonical
    return display, confidence


def classify_reading(
    reading: Mapping[str, Any],
    model: Optional[Mapping[str, Any]],
    moment: datetime,
) -> Tuple[str, float]:
    if not model:
        return "unknown", 0.0
    feats = features_from_reading(reading, moment, str(model.get("timezone") or DEFAULT_TIMEZONE))
    if feats is None:
        return "unknown", 0.0
    try:
        return predict(model, feats)
    except Exception:
        return "unknown", 0.0


def apply_with_models(
    updated: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    readings: Dict[str, Dict[str, Any]],
    fingerprints: Iterable[Dict[str, Any]],
    models: Mapping[str, Mapping[str, Any]],
    moment: datetime,
) -> Dict[str, Any]:
    """Like ``room_fingerprint.apply_rooms`` but prefers check-in models."""
    prev_people = {
        str(person.get("person") or "").casefold(): person
        for person in (previous or {}).get("people") or []
        if isinstance(person, dict) and person.get("person")
    }
    stored = [fp for fp in fingerprints if isinstance(fp, dict)]

    for person in updated.get("people") or []:
        if not isinstance(person, dict):
            continue
        key = str(person.get("person") or "").casefold()
        prev = prev_people.get(key) or {}
        if not person.get("is_home"):
            room_fingerprint._clear_room(person)
            continue
        if (room_fingerprint._as_int(person.get("miss_count")) or 0) > 0:
            room_fingerprint._hold_room(person, prev)
            continue
        reading = readings.get(key) or {}
        digest = str(reading.get("device_hmac") or "")
        model = models.get(key)
        proposed, confidence = classify_reading(reading, model, moment)
        if proposed == "unknown":
            usable = [fp for fp in stored if room_fingerprint._fingerprint_applies(fp, key, digest)]
            proposed, confidence = room_fingerprint.classify(
                reading.get("node"),
                reading.get("bucket"),
                usable,
                zone=reading.get("zone"),
            )
        person.update(room_fingerprint.smooth(prev, proposed, confidence))
        if digest:
            person["device_hmac"] = digest
        else:
            person.pop("device_hmac", None)
    room_fingerprint._aggregate_room(updated)
    return updated


def confusion_matrix(
    labeled: Sequence[Tuple[Dict[str, str], str]],
    model: Mapping[str, Any],
) -> Dict[str, Any]:
    rooms = sorted(model.get("rooms") or [])
    matrix: Dict[str, Dict[str, int]] = {actual: {pred: 0 for pred in rooms + ["unknown"]} for actual in rooms}
    correct = 0
    for feats, actual in labeled:
        pred, _ = predict(model, feats)
        pred_key = pred.casefold() if pred != "unknown" else "unknown"
        if pred_key not in matrix.get(actual, {}):
            matrix.setdefault(actual, {pred: 0 for pred in rooms + ["unknown"]})
        matrix[actual][pred_key if pred_key in matrix[actual] else "unknown"] += 1
        if pred_key == actual:
            correct += 1
    return {
        "rooms": rooms,
        "matrix": matrix,
        "accuracy": round(correct / len(labeled), 3) if labeled else 0.0,
        "total": len(labeled),
    }


def leave_one_out_eval(labeled: Sequence[Tuple[Dict[str, str], str]], canonical: Mapping[str, str]) -> Dict[str, Any]:
    if len(labeled) < MIN_TRAIN_ROWS:
        raise TrainError(f"need at least {MIN_TRAIN_ROWS} rows for evaluation")
    preds: List[Tuple[str, str]] = []
    for index in range(len(labeled)):
        holdout = labeled[index]
        train = labeled[:index] + labeled[index + 1 :]
        model = build_model(train, canonical)
        pred, _ = predict(model, holdout[0])
        preds.append((holdout[1], pred.casefold() if pred != "unknown" else "unknown"))
    rooms = sorted({actual for actual, _ in preds})
    matrix: Dict[str, Dict[str, int]] = {actual: {pred: 0 for pred in rooms + ["unknown"]} for actual in rooms}
    correct = 0
    for actual, pred in preds:
        matrix[actual][pred if pred in matrix[actual] else "unknown"] += 1
        if pred == actual:
            correct += 1
    return {
        "method": "leave_one_out",
        "rooms": rooms,
        "matrix": matrix,
        "accuracy": round(correct / len(preds), 3),
        "total": len(preds),
    }


def training_checkins_from_table(table: Any, person: Optional[str] = None) -> List[Dict[str, Any]]:
    import room_checkin

    return room_checkin.list_checkins(table, person=person, limit=room_checkin.MAX_LIMIT)


def train_from_checkins(
    checkins: Sequence[Mapping[str, Any]],
    *,
    person: str,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> Dict[str, Any]:
    labeled, canonical = train_rows(checkins, person=person)
    metrics = leave_one_out_eval(labeled, canonical)
    model = build_model(labeled, canonical, timezone_name=timezone_name, metrics=metrics)
    return model


def format_confusion(report: Mapping[str, Any]) -> str:
    rooms = list(report.get("rooms") or [])
    headers = ["actual \\ pred"] + rooms + ["unknown"]
    lines = ["\t".join(headers)]
    matrix = report.get("matrix") or {}
    for actual in rooms:
        row = matrix.get(actual) or {}
        lines.append("\t".join([actual] + [str(row.get(pred, 0)) for pred in rooms + ["unknown"]]))
    lines.append(f"accuracy={report.get('accuracy')} n={report.get('total')}")
    return "\n".join(lines)
