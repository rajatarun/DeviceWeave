"""Check-in trained room classifier: features, training, inference, schedule precedence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import eero_presence_handler
import room_classifier
import room_fingerprint
import room_schedule

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "room_checkins_sample.json"
NOW = datetime(2026, 10, 18, 21, 0, tzinfo=timezone.utc)


def _fixture_rows():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_canonical_room_labels_merge_case():
    rows = [
        {"room": "Master bedroom"},
        {"room": "Master Bedroom"},
        {"room": "Master bedroom"},
    ]
    canon = room_classifier.canonical_room_labels(rows)
    assert canon["master bedroom"] == "Master bedroom"


def test_features_require_node_or_bucket():
    assert room_classifier.features_from_checkin({"node": "Office eero", "signal_bucket": 5})
    assert room_classifier.features_from_checkin({"signal_bucket": 4}) is not None
    assert room_classifier.features_from_checkin({}) is None


def test_train_and_leave_one_out_on_fixture():
    model = room_classifier.train_from_checkins(_fixture_rows(), person="Raja")
    assert model["sample_count"] >= room_classifier.MIN_TRAIN_ROWS
    metrics = model["metrics"]
    assert metrics["total"] == model["sample_count"]
    assert metrics["accuracy"] >= 0.85
    assert "living room" in model["rooms"]
    assert model["canonical"]["master bedroom"] == "Master bedroom"


def test_train_rows_exclude_schedule_and_hourly_sources():
    rows = _fixture_rows()
    schedule_count = sum(1 for row in rows if row.get("source") == "schedule_rule")
    assert schedule_count >= 3
    labeled, _ = room_classifier.train_rows(rows, person="Raja")
    assert len(labeled) == len(rows) - schedule_count
    hourly = dict(rows[0])
    hourly["source"] = "hourly_routine"
    hourly["room"] = "Kitchen"
    labeled2, _ = room_classifier.train_rows(rows + [hourly], person="Raja")
    assert len(labeled2) == len(labeled)


def test_training_checkins_from_table_pages_all_rows():
    import boto3
    import room_checkin
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="checkins",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        for index in range(250):
            moment = base.replace(minute=index % 60, hour=12 + index // 60)
            table.put_item(
                Item={
                    "pk": "person#raja",
                    "sk": f"{moment.strftime('%Y%m%dT%H%M%SZ')}~{index:04x}",
                    "person": "Raja",
                    "room": "Kitchen",
                    "source": "record",
                    "node": "Office eero",
                    "signal_bucket": 5,
                    "local_hour": 8,
                    "weekday": 0,
                }
            )
        capped = room_checkin.list_checkins(table, person="Raja", limit=room_checkin.MAX_LIMIT)
        assert len(capped) == room_checkin.MAX_LIMIT
        all_rows = room_classifier.training_checkins_from_table(table, person="Raja")
        assert len(all_rows) == 250


def test_living_vs_master_on_family_room_node():
    model = room_classifier.train_from_checkins(_fixture_rows(), person="Raja")
    living_feats = {
        "node": "family room",
        "bucket": "5",
        "node_bucket": "family room|5",
        "hour": "19",
        "weekday": "3",
    }
    master_feats = {
        "node": "family room",
        "bucket": "4",
        "node_bucket": "family room|4",
        "hour": "23",
        "weekday": "0",
    }
    living_room, living_conf = room_classifier.predict(model, living_feats)
    master_room, master_conf = room_classifier.predict(model, master_feats)
    assert living_room == "Living room"
    assert master_room == "Master bedroom"
    assert living_conf >= room_fingerprint.CONFIDENCE_FLOOR
    assert master_conf >= room_fingerprint.CONFIDENCE_FLOOR


def test_fingerprint_tie_unknown_classifier_wins():
    model = room_classifier.train_from_checkins(_fixture_rows(), person="Raja")
    updated = {
        "people": [{"person": "Raja", "is_home": True, "miss_count": 0, "last_seen": "t"}],
    }
    reading = {"node": "Family Room eero", "bucket": 5, "zone": "Family Room"}
    tie_fp = {
        "person": "Raja",
        "room": "Living room",
        "samples": [{"node": "Family Room eero", "bucket": 5}],
    }
    tie_fp2 = {
        "person": "Raja",
        "room": "Master bedroom",
        "samples": [{"node": "Family Room eero", "bucket": 5}],
    }
    room_classifier.apply_with_models(
        updated,
        None,
        {"raja": reading},
        [tie_fp, tie_fp2],
        {"raja": model},
        NOW,
    )
    assert updated["people"][0]["room"] == "unknown"
    room_classifier.apply_with_models(
        updated,
        updated,
        {"raja": {**reading, "bucket": 5}},
        [tie_fp, tie_fp2],
        {"raja": model},
        NOW,
    )
    assert updated["people"][0]["room"] == "Living room"


def test_schedule_overrides_classifier():
    model = room_classifier.train_from_checkins(_fixture_rows(), person="Raja")
    updated = {
        "people": [{"person": "Raja", "is_home": True, "miss_count": 0, "last_seen": "t"}],
    }
    reading = {"node": "Family Room eero", "bucket": 5}
    for _ in range(2):
        room_classifier.apply_with_models(
            updated,
            updated,
            {"raja": reading},
            [],
            {"raja": model},
            NOW,
        )
    assert updated["people"][0]["room"] == "Living room"
    rules = [
        {
            "person": "Raja",
            "room": "Master bedroom",
            "start": "23:00",
            "end": "06:00",
            "timezone": "America/Chicago",
        }
    ]
    overnight = datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)
    room_schedule.apply_rules(updated, rules, overnight)
    assert updated["people"][0]["room"] == "Master bedroom"
    assert updated["people"][0]["room_confidence"] == 1.0
    assert updated["people"][0]["room_source"] == room_schedule.SCHEDULE_SOURCE


def test_apply_failure_carries_forward(monkeypatch):
    updated = {
        "people": [{"person": "Raja", "is_home": True, "miss_count": 0, "room": "Kitchen", "room_confidence": 0.8}],
    }
    previous = {"people": [{"person": "Raja", "is_home": True, "room": "Kitchen", "room_confidence": 0.8}]}

    def boom(*_a, **_k):
        raise RuntimeError("fail")

    monkeypatch.setattr(room_classifier, "apply_with_models", boom)
    room_fingerprint.carry_forward(updated, previous)
    assert updated["people"][0]["room"] == "Kitchen"


def test_classifier_items_filtered_from_fingerprint_load(fake_dynamodb):
    table = fake_dynamodb.Table("fingerprints")
    table.put_item(
        Item={
            "pk": "person#raja",
            "sk": "room#kitchen",
            "person": "Raja",
            "room": "Kitchen",
            "samples": [],
        }
    )
    table.put_item(
        Item={
            "pk": room_classifier.classifier_pk("Raja"),
            "sk": room_classifier.MODEL_SK,
            "model": "{}",
        }
    )
    loaded = room_fingerprint.load_all(table)
    assert len(loaded) == 1
    assert loaded[0]["sk"] == "room#kitchen"
