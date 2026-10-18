"""DynamoDB number serialization for the eero writers.

The in-memory table fake accepts Python floats. boto3's Table does not:
put_item raises TypeError and the poller crashes after classification.
These tests use moto's DynamoDB and a real boto3 Table resource.
"""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import boto3
from moto import mock_aws

import eero_presence
import eero_presence_handler
import presence_store
import room_fingerprint

_eero_spec = importlib.util.spec_from_file_location(
    "test_eero_presence_numbers",
    Path(__file__).with_name("test_eero_presence.py"),
)
_eero = importlib.util.module_from_spec(_eero_spec)
_eero_spec.loader.exec_module(_eero)

_login_spec = importlib.util.spec_from_file_location(
    "eero_login_script",
    Path(__file__).resolve().parents[1] / "scripts" / "eero_login.py",
)
_login = importlib.util.module_from_spec(_login_spec)
_login_spec.loader.exec_module(_login)


def _table(resource, name: str, *, sort: bool = False):
    keys = [{"AttributeName": "pk", "KeyType": "HASH"}]
    attrs = [{"AttributeName": "pk", "AttributeType": "S"}]
    if sort:
        keys.append({"AttributeName": "sk", "KeyType": "RANGE"})
        attrs.append({"AttributeName": "sk", "AttributeType": "S"})
    table = resource.create_table(
        TableName=name,
        KeySchema=keys,
        AttributeDefinitions=attrs,
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table


def test_write_home_stores_confidence_as_decimal_and_json_reads_it_back():
    with mock_aws():
        table = _table(boto3.resource("dynamodb", region_name="us-east-1"), "presence")
        presence_store.write_home(table, {
            "is_home": True,
            "zone": "Office",
            "node": "Office eero",
            "signal": "strong",
            "room": "unknown",
            "room_confidence": 0.0,
            "last_seen": "2026-10-18T16:00:00.000Z",
            "freshness": "fresh",
            "updated_at": "2026-10-18T16:05:00+00:00",
            "source": "eero",
            "people": [{
                "person": "Raja",
                "is_home": True,
                "signal": "strong",
                "room": "unknown",
                "room_confidence": 0.5,
                "last_seen": "2026-10-18T16:00:00.000Z",
                "freshness": "fresh",
                "miss_count": 1,
                "room_pending_count": 1,
            }],
        })
        stored = table.get_item(Key={"pk": "home_state"})["Item"]
        assert isinstance(stored["room_confidence"], Decimal)
        assert isinstance(stored["people"][0]["room_confidence"], Decimal)
        assert not isinstance(stored["people"][0]["miss_count"], float)

        view = presence_store.public_view(stored)
        body = json.loads(json.dumps(view))
        assert body["room_confidence"] == 0.0
        assert body["people"][0]["room_confidence"] == 0.5
        assert body["people"][0]["miss_count"] == 1
        assert body["signal"] == "strong"
        assert body["last_seen"] == "2026-10-18T16:00:00.000Z"
        assert body["freshness"] == "fresh"


def test_poller_classification_write_survives_a_real_table(monkeypatch):
    """The production crash: classify, then put_item the float confidence."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "presence")
        tracked = _table(resource, "tracked")
        fingerprints = _table(resource, "fingerprints", sort=True)
        monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
        monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
        monkeypatch.setenv("ROOM_FINGERPRINT_TABLE", "fingerprints")
        monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
        monkeypatch.setenv("EERO_DEVICE_HMAC_SECRET_NAME", "deviceweave/eero-device-hmac")
        monkeypatch.setattr(
            __import__("aws_clients"), "get_dynamodb_resource", lambda: resource,
        )
        monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
        monkeypatch.setattr(room_fingerprint, "load_hmac_key", lambda name, client=None: "test-hmac-key")
        tracked.put_item(Item={
            "pk": f"mac:{eero_presence.normalize_mac(_eero.MAC)}",
            "person": "Raja",
            "match": "mac",
            "mac": _eero.MAC,
        })
        digest = room_fingerprint.device_hmac(_eero.MAC, "test-hmac-key")
        room_fingerprint.save_fingerprint(
            fingerprints,
            room_fingerprint.fingerprint_item("Raja", "Office", digest, [
                {"node": "Office eero", "bucket": 4, "signal": "strong", "taken_at": _eero.NOW},
            ], _eero.NOW),
        )
        _eero._install(monkeypatch, {
            "/account": _eero._account(),
            "/networks/1001/devices": _eero._envelope([_eero._client()]),
        })

        first = eero_presence_handler.handler({"source": "scheduler"}, None)
        second = eero_presence_handler.handler({"source": "scheduler"}, None)

        assert first["status"] == "updated"
        assert second["status"] == "updated"
        assert second["room"] == "Office"
        stored = resource.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
        assert isinstance(stored["room_confidence"], Decimal)
        assert isinstance(stored["people"][0]["room_confidence"], Decimal)
        body = json.loads(json.dumps(presence_store.public_view(stored)))
        assert body["room"] == "Office"
        assert body["room_confidence"] == 1.0
        assert body["people"][0]["person"] == "Raja"


def test_write_failure_returns_without_raising(monkeypatch):
    """A raised put_item must not escape. Scheduler retries a failed invoke."""
    tracked_items = [{
        "pk": f"mac:{eero_presence.normalize_mac(_eero.MAC)}",
        "person": "Raja",
        "match": "mac",
        "mac": _eero.MAC,
    }]

    class _Table:
        def __init__(self, items):
            self._items = items

        def get_item(self, Key):
            return {"Item": {
                "pk": "home_state", "is_home": True, "freshness": "fresh", "room": "Office",
            }}

        def put_item(self, Item):
            raise TypeError("Float types are not supported. Use Decimal types instead.")

        def scan(self, **kwargs):
            return {"Items": self._items}

    class _Resource:
        def Table(self, name):
            if name == "tracked":
                return _Table(tracked_items)
            return _Table([])

    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    monkeypatch.setattr(eero_presence_handler.aws_clients, "get_dynamodb_resource", lambda: _Resource())
    _eero._install(monkeypatch, {
        "/account": _eero._account(),
        "/networks/1001/devices": _eero._envelope([_eero._client()]),
    })

    result = eero_presence_handler.handler({"source": "scheduler"}, None)
    assert result["status"] == "write_failed"
    assert result["is_home"] is True
    assert result["freshness"] == "fresh"
    assert result["room"] == "Office"


def test_stale_write_failure_returns_without_raising(monkeypatch):
    class _Table:
        def get_item(self, Key):
            return {"Item": {"pk": "home_state", "is_home": True, "freshness": "fresh"}}

        def put_item(self, Item):
            raise TypeError("Float types are not supported. Use Decimal types instead.")

        def scan(self, **kwargs):
            return {"Items": []}

    class _Resource:
        def Table(self, name):
            return _Table()

    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    monkeypatch.setattr(eero_presence_handler.aws_clients, "get_dynamodb_resource", lambda: _Resource())
    _eero._install(monkeypatch, {
        "/account": _eero._account(),
        "/networks/1001/devices": _eero._envelope([_eero._client()]),
    })

    result = eero_presence_handler.handler({"source": "scheduler"}, None)
    assert result["status"] == "stale"
    assert result["persisted"] is False
    assert result["is_home"] is True


def test_fingerprint_float_and_tracked_name_round_trip():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        fingerprints = _table(resource, "fingerprints", sort=True)
        tracked = _table(resource, "tracked")
        room_fingerprint.save_fingerprint(fingerprints, {
            "pk": "person#raja",
            "sk": "room#office",
            "person": "Raja",
            "room": "Office",
            "device_hmac": "abc",
            "sample_count": 1,
            "updated_at": "2026-10-18T16:05:00+00:00",
            "samples": [{
                "node": "Office eero",
                "taken_at": "2026-10-18T16:00:00.000Z",
                "bucket": 4,
                "signal": "strong",
                "weight": 0.25,
            }],
        })
        stored = fingerprints.get_item(Key={"pk": "person#raja", "sk": "room#office"})["Item"]
        assert isinstance(stored["samples"][0]["weight"], Decimal)
        listed = room_fingerprint.list_fingerprints(fingerprints)
        json.dumps({"rooms": listed})
        assert listed[0]["samples"][0]["bucket"] == 4
        assert listed[0]["samples"][0]["signal"] == "strong"
        assert "device_hmac" not in listed[0]
        assert "weight" not in listed[0]["samples"][0]

        item = {"pk": "name:iphone", "person": "Raja", "match": "name", "name": "iphone"}
        assert not any(isinstance(value, float) for value in item.values())
        _login._put_tracked("tracked", item, "us-east-1")
        saved = tracked.get_item(Key={"pk": "name:iphone"})["Item"]
        assert saved["name"] == "iphone"
        assert saved["person"] == "Raja"


def test_tracked_writer_converts_a_float_attribute():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        tracked = _table(resource, "tracked")
        _login._put_tracked("tracked", {
            "pk": "name:iphone",
            "person": "Raja",
            "match": "name",
            "name": "iphone",
            "score": 0.5,
        }, "us-east-1")
        saved = tracked.get_item(Key={"pk": "name:iphone"})["Item"]
        assert isinstance(saved["score"], Decimal)
        assert float(saved["score"]) == 0.5
