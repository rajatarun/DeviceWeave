"""
Room calibration: storage, classification, smoothing, HMAC, and auth.

Every eero HTTP call is a stand-in. Nothing in this file opens a socket.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import aws_clients
import eero_calibration_handler
import eero_presence
import eero_presence_handler
import mcp_handler
import presence_store
import room_calibration_client
import room_fingerprint
from policy_authoring.rule_set_checker import analyze
from policy_authoring.validator import ValidationError, validate_policy
from policy_engine.evaluator import compute_verdict
from room_fingerprint import LabelError

_eero_spec = importlib.util.spec_from_file_location(
    "test_eero_presence",
    Path(__file__).with_name("test_eero_presence.py"),
)
_eero = importlib.util.module_from_spec(_eero_spec)
_eero_spec.loader.exec_module(_eero)
MAC = _eero.MAC
_account = _eero._account
_client = _eero._client
_envelope = _eero._envelope
_install = _eero._install

TOKEN = "expected-token-value"
HMAC_KEY = "test-hmac-key"
NOW = "2026-10-18T16:05:00+00:00"


def _http(method, path, body=None, token=TOKEN, query=None, path_params=None):
    headers = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
        "queryStringParameters": query,
        "pathParameters": path_params,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _tracked(table):
    table.put_item(Item={
        "pk": f"mac:{eero_presence.normalize_mac(MAC)}",
        "person": "Raja",
        "match": "mac",
        "mac": MAC,
    })


def _ready(monkeypatch, fake_dynamodb, *, interval="0"):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("ROOM_FINGERPRINT_TABLE", "fingerprints")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setenv("EERO_DEVICE_HMAC_SECRET_NAME", "deviceweave/eero-device-hmac")
    monkeypatch.setenv("EERO_CALIBRATION_FUNCTION_NAME", "deviceweave-eero-calibration-test")
    monkeypatch.setenv("EERO_CALIBRATION_INTERVAL_SECONDS", interval)
    monkeypatch.setenv("MCP_AUTH_TOKEN_SECRET_NAME", "deviceweave/mcp-auth-token")
    monkeypatch.setattr(aws_clients, "get_dynamodb_resource", lambda: fake_dynamodb)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    monkeypatch.setattr(room_fingerprint, "load_hmac_key", lambda name, client=None: HMAC_KEY)
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)
    monkeypatch.setattr(eero_calibration_handler, "_now", lambda: NOW)
    monkeypatch.setattr(eero_calibration_handler, "_sleep", lambda _seconds: None)
    _tracked(fake_dynamodb.Table("tracked"))


def _no_raw_mac(blob: str):
    assert MAC not in blob
    assert "Raja phone" not in blob
    assert "session-token" not in blob


def _walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk_keys(inner)


# ---------------------------------------------------------------------------
# Buckets and labels
# ---------------------------------------------------------------------------

def test_signal_bucket_uses_only_real_eero_fields():
    assert room_fingerprint.signal_bucket(
        {"connectivity": {"signal": "weak", "score": 0.9, "score_bars": 4}}
    ) == 4
    assert room_fingerprint.signal_bucket(
        {"connectivity": {"signal": "strong", "score_bars": 0}}
    ) == 4
    assert room_fingerprint.signal_bucket({"connectivity": {"signal": "-55 dBm"}}) == 4
    assert room_fingerprint.signal_bucket({"connectivity": {"signal_avg": "fair"}}) == 3
    assert room_fingerprint.signal_bucket({"connectivity": {"score": 0.9}}) is None
    assert room_fingerprint.signal_bucket({"rssi": -40, "signal_strength": -40}) is None
    assert room_fingerprint.signal_bucket({}) is None


def test_room_names_are_capped_and_samples_are_capped():
    assert room_fingerprint.validate_room("  Living Room ") == "Living Room"
    assert room_fingerprint.validate_samples(None) == 5
    assert room_fingerprint.validate_samples(8) == 8
    for bad in ("", "unknown", "Unknown", "../office", "a/b", "a" * 41, "office!", 12):
        with pytest.raises(LabelError):
            room_fingerprint.validate_room(bad)
    for bad in (0, 9, True, "5"):
        with pytest.raises(LabelError):
            room_fingerprint.validate_samples(bad)


def test_hmac_is_stable_and_is_not_the_mac():
    digest = room_fingerprint.device_hmac(MAC, HMAC_KEY)
    assert digest == room_fingerprint.device_hmac("AA-BB-CC-DD-EE-FF", HMAC_KEY)
    assert digest != room_fingerprint.device_hmac(MAC, "other-key")
    assert len(digest) == 64
    assert eero_presence.normalize_mac(MAC) not in digest
    item = room_fingerprint.fingerprint_item("Raja", "Office", digest, [
        {"node": "Office eero", "bucket": 4, "signal": "strong", "taken_at": NOW, "mac": MAC},
    ], NOW)
    blob = json.dumps(item)
    _no_raw_mac(blob)
    assert "mac" not in list(_walk_keys(item))
    assert item["device_hmac"] == digest
    assert item["samples"] == [{"node": "Office eero", "bucket": 4, "signal": "strong", "taken_at": NOW}]


# ---------------------------------------------------------------------------
# Classification and smoothing
# ---------------------------------------------------------------------------

def _fp(room, node, bucket):
    return {
        "room": room,
        "device_hmac": "abc",
        "samples": [{"node": node, "bucket": bucket}],
    }


def test_classify_exact_match_low_confidence_tie_and_unseen_node():
    office = _fp("Office", "Office eero", 4)
    kitchen = _fp("Kitchen", "Kitchen eero", 2)

    room, confidence = room_fingerprint.classify("Office eero", 4, [office, kitchen])
    assert (room, confidence) == ("Office", 1.0)

    room, confidence = room_fingerprint.classify("Office eero", 1, [office])
    assert room == "unknown"
    assert confidence < room_fingerprint.CONFIDENCE_FLOOR

    room, confidence = room_fingerprint.classify(
        "Office eero", 4, [_fp("Office", "Office eero", 4), _fp("Den", "Office eero", 4)]
    )
    assert (room, confidence) == ("unknown", 0.0)

    room, confidence = room_fingerprint.classify("Garage eero", 4, [office, kitchen])
    assert (room, confidence) == ("unknown", 0.0)

    room, confidence = room_fingerprint.classify("Office eero", None, [office])
    assert (room, confidence) == ("Office", 0.5)


def test_smoothing_waits_for_two_readings_and_holds_through_a_miss():
    first = room_fingerprint.smooth(None, "Office", 1.0)
    assert first["room"] == "unknown"
    assert first["room_pending"] == "Office"
    assert first["room_pending_count"] == 1

    second = room_fingerprint.smooth(first, "Office", 1.0)
    assert second["room"] == "Office"
    assert second["room_confidence"] == 1.0

    flicker = room_fingerprint.smooth(second, "Kitchen", 1.0)
    assert flicker["room"] == "Office"
    assert flicker["room_pending"] == "Kitchen"
    assert flicker["room_pending_count"] == 1

    changed = room_fingerprint.smooth(flicker, "Kitchen", 1.0)
    assert changed["room"] == "Kitchen"

    updated = {
        "people": [{
            "person": "Raja",
            "is_home": True,
            "miss_count": 1,
            "last_seen": NOW,
            "zone": "Office",
        }],
    }
    room_fingerprint.apply_rooms(updated, {"people": [{"person": "Raja", **changed}]}, {}, [])
    assert updated["people"][0]["room"] == "Kitchen"
    assert updated["room"] == "Kitchen"


def test_away_clears_the_room_and_does_not_guess():
    updated = {
        "people": [{
            "person": "Raja",
            "is_home": False,
            "miss_count": 3,
            "last_seen": NOW,
        }],
    }
    previous = {"people": [{"person": "Raja", "room": "Office", "room_confidence": 1.0, "is_home": True}]}
    room_fingerprint.apply_rooms(
        updated, previous,
        {"raja": {"node": "Kitchen eero", "bucket": 2, "device_hmac": "abc"}},
        [_fp("Kitchen", "Kitchen eero", 2)],
    )
    assert updated["room"] == "unknown"
    assert updated["room_confidence"] == 0.0
    assert "room_pending" not in updated["people"][0]


def test_rule_set_accepts_a_room_and_does_not_treat_different_rooms_as_one():
    office = {
        "rule_id": "office",
        "scope": {"device_type": "light"},
        "conditions": [{"field": "room", "operator": "==", "value": "Office"}],
        "action": {"type": "block", "reason": "office", "params": {}},
        "confidence": 0.95,
    }
    validated = validate_policy(office)
    assert validated["conditions"][0]["value"] == "Office"
    with pytest.raises(ValidationError):
        validate_policy({**office, "conditions": [{"field": "room", "operator": ">", "value": "Office"}]})

    findings = analyze([validated])
    assert not any(item.kind == "unsatisfiable" for item in findings)

    kitchen = {
        **office,
        "rule_id": "kitchen",
        "conditions": [{"field": "room", "operator": "==", "value": "Kitchen"}],
        "action": {"type": "allow", "reason": "kitchen", "params": {}},
    }
    assert not any(item.kind == "conflict_block_allow" for item in analyze([validated, kitchen]))

    open_allow = {
        **kitchen,
        "rule_id": "anytime",
        "conditions": [{"field": "is_home", "operator": "==", "value": True}],
    }
    assert any(item.kind == "conflict_block_allow" for item in analyze([validated, open_allow]))

    ctx = {
        "temperature": 70, "humidity": 50, "cloud_cover_pct": 0,
        "is_overcast": False, "time_hour": 12, "is_home": True, "room": "Office",
    }
    assert compute_verdict([validated], "light", "turn_on", ctx).verdict == "block"
    assert compute_verdict([validated], "light", "turn_on", {**ctx, "room": "unknown"}).verdict == "allow"


def test_policy_context_reads_room_and_defaults_to_unknown(monkeypatch, fake_dynamodb):
    import policy_engine.context_provider as context_provider

    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setattr(context_provider, "get_dynamodb_resource", lambda: fake_dynamodb)
    fake_dynamodb.Table("presence").put_item(Item={
        "pk": "home_state", "is_home": True, "room": "Office", "room_confidence": 0.75,
    })
    assert context_provider.get_context()["room"] == "Office"

    fake_dynamodb.Table("presence").put_item(Item={"pk": "home_state", "is_home": True})
    assert context_provider.get_context()["room"] == "unknown"


# ---------------------------------------------------------------------------
# Calibration storage, auth, recalibrate
# ---------------------------------------------------------------------------

def test_collect_stores_samples_and_recalibrate_replaces_them(monkeypatch, fake_dynamodb, caplog):
    _ready(monkeypatch, fake_dynamodb, interval="12")
    slept = []
    monkeypatch.setattr(eero_calibration_handler, "_sleep", lambda seconds: slept.append(seconds))
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })

    with caplog.at_level("INFO"):
        result = eero_calibration_handler.collect_room("Raja", "Office", 3)

    assert result == {"status": "stored", "room": "Office", "samples": 3}
    assert slept == [12, 12]
    table = fake_dynamodb.Table("fingerprints")
    item = table.get_item(Key={
        "pk": room_fingerprint.person_pk("Raja"),
        "sk": room_fingerprint.room_sk("Office"),
    })["Item"]
    assert item["sample_count"] == 3
    assert {sample["node"] for sample in item["samples"]} == {"Office eero"}
    assert {sample["bucket"] for sample in item["samples"]} == {4}
    blob = json.dumps(item)
    _no_raw_mac(blob)
    assert "mac" not in list(_walk_keys(item))
    assert item["device_hmac"] == room_fingerprint.device_hmac(MAC, HMAC_KEY)
    assert MAC not in caplog.text
    assert "Raja phone" not in caplog.text
    assert "session-token" not in caplog.text
    assert "strong" not in caplog.text
    assert "Office" in caplog.text

    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client(source={"location": "Kitchen", "display_name": "Kitchen eero"})]),
    })
    replaced = eero_calibration_handler.collect_room(" Raja ", "Office", 2)
    assert replaced["samples"] == 2
    item = table.get_item(Key={"pk": item["pk"], "sk": item["sk"]})["Item"]
    assert item["sample_count"] == 2
    assert {sample["node"] for sample in item["samples"]} == {"Kitchen eero"}


def test_a_failed_or_empty_run_keeps_the_previous_fingerprint(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })
    assert eero_calibration_handler.collect_room("Raja", "Office", 1)["status"] == "stored"
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client(connected=False)]),
    })
    assert eero_calibration_handler.collect_room("Raja", "Office", 2)["status"] == "unchanged"
    item = fake_dynamodb.Table("fingerprints").get_item(Key={
        "pk": room_fingerprint.person_pk("Raja"),
        "sk": room_fingerprint.room_sk("Office"),
    })["Item"]
    assert item["sample_count"] == 1
    assert item["samples"][0]["node"] == "Office eero"


def test_room_routes_require_the_mcp_bearer_token(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    invoked = []
    monkeypatch.setattr(eero_calibration_handler, "_invoke_async", lambda payload: invoked.append(payload))

    def urlopen(req, timeout=0):
        raise AssertionError("eero was called")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    missing = eero_calibration_handler.handler(
        _http("POST", "/prod/presence/rooms/calibrate", {"person": "Raja", "room": "Office"}, token=None),
        None,
    )
    wrong = eero_calibration_handler.handler(
        _http("POST", "/prod/presence/rooms/calibrate", {"person": "Raja", "room": "Office"}, token="nope"),
        None,
    )
    listing = eero_calibration_handler.handler(_http("GET", "/prod/presence/rooms", token=None), None)
    assert missing["statusCode"] == 401
    assert wrong["statusCode"] == 401
    assert listing["statusCode"] == 401
    assert invoked == []

    started = eero_calibration_handler.handler(
        _http("POST", "/prod/presence/rooms/calibrate", {"person": "Raja", "room": "Office", "samples": 5}),
        None,
    )
    assert started["statusCode"] == 202
    body = json.loads(started["body"])
    assert body["status"] == "started"
    assert body["samples"] == 5
    assert body["interval_seconds"] == 0
    assert invoked == [{"action": "collect", "person": "Raja", "room": "Office", "samples": 5}]
    assert MAC not in json.dumps(invoked)

    rejected = eero_calibration_handler.handler(
        _http("POST", "/prod/presence/rooms/calibrate", {"person": "Raja", "room": "../office", "samples": 9}),
        None,
    )
    assert rejected["statusCode"] == 400
    assert len(invoked) == 1


def test_list_and_delete_are_authenticated_and_do_not_return_the_hmac(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })
    eero_calibration_handler.collect_room("Raja", "Office", 1)

    listed = eero_calibration_handler.handler(
        _http("GET", "/prod/presence/rooms", query={"person": "Raja"}),
        None,
    )
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["rooms"][0]["room"] == "Office"
    assert body["rooms"][0]["sample_count"] == 1
    assert "device_hmac" not in json.dumps(body)
    _no_raw_mac(json.dumps(body))

    removed = eero_calibration_handler.handler(
        _http("DELETE", "/prod/presence/rooms/Office", path_params={"room": "Office"}),
        None,
    )
    assert removed["statusCode"] == 200
    assert json.loads(removed["body"])["deleted"] == 1
    assert room_fingerprint.list_fingerprints(fake_dynamodb.Table("fingerprints")) == []


def test_mcp_tools_call_the_calibration_lambda(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)
    seen = []

    def invoke(action, payload):
        seen.append((action, payload))
        return {"statusCode": 202, "body": json.dumps({"status": "started", "room": payload.get("room")})}

    monkeypatch.setattr(room_calibration_client, "invoke", invoke)
    response = mcp_handler.handler({
        "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
        "headers": {"authorization": f"Bearer {TOKEN}"},
        "body": json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "calibrate_room",
                "arguments": {"person": "Raja", "room": "Office", "samples": 5},
            },
        }),
    }, None)
    assert response["statusCode"] == 200
    assert seen == [("start", {"person": "Raja", "room": "Office", "samples": 5})]
    assert json.loads(response["body"])["result"]["isError"] is False


# ---------------------------------------------------------------------------
# Poller classification
# ---------------------------------------------------------------------------

def _seed_office(fake_dynamodb):
    digest = room_fingerprint.device_hmac(MAC, HMAC_KEY)
    room_fingerprint.save_fingerprint(
        fake_dynamodb.Table("fingerprints"),
        room_fingerprint.fingerprint_item("Raja", "Office", digest, [
            {"node": "Office eero", "bucket": 4, "signal": "strong", "taken_at": NOW},
        ], NOW),
    )


def _poll(monkeypatch, client):
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([client]),
    })
    return eero_presence_handler.handler({"source": "scheduler"}, None)


def test_poller_names_a_room_only_after_two_readings(monkeypatch, fake_dynamodb, caplog):
    _ready(monkeypatch, fake_dynamodb)
    _seed_office(fake_dynamodb)

    with caplog.at_level("INFO"):
        first = _poll(monkeypatch, _client())
    assert first["room"] == "unknown"
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert stored["people"][0]["room_pending"] == "Office"
    assert stored["people"][0]["device_hmac"] == room_fingerprint.device_hmac(MAC, HMAC_KEY)
    assert "mac" not in list(_walk_keys(stored["people"]))
    _no_raw_mac(json.dumps({
        "presence": stored,
        "fingerprints": fake_dynamodb.Table("fingerprints").scan()["Items"],
    }))
    assert "strong" not in caplog.text
    assert "Raja phone" not in caplog.text

    second = _poll(monkeypatch, _client())
    assert second["room"] == "Office"
    assert second["room_confidence"] == 1.0
    view = presence_store.public_view(
        fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    )
    assert view["zone"] == "Office"
    assert view["room"] == "Office"
    assert view["room_confidence"] == 1.0
    assert view["people"][0]["room"] == "Office"
    assert "device_hmac" not in json.dumps(view)
    assert "room_pending" not in json.dumps(view)
    # The fingerprint was not appended with the live poll.
    assert fake_dynamodb.Table("fingerprints").scan()["Items"][0]["sample_count"] == 1


def test_poller_falls_back_to_the_node_zone_when_the_node_is_unseen(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    _seed_office(fake_dynamodb)
    client = _client(source={"location": "Kitchen", "display_name": "Kitchen eero"})
    _poll(monkeypatch, client)
    second = _poll(monkeypatch, client)
    assert second["room"] == "unknown"
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert stored["zone"] == "Kitchen"
    assert stored["room"] == "unknown"
    assert presence_store.public_view(stored)["room"] == "unknown"


def test_unreadable_hmac_keeps_the_last_room(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    _seed_office(fake_dynamodb)
    _poll(monkeypatch, _client())
    _poll(monkeypatch, _client())
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert stored["room"] == "Office"

    def _boom(name, client=None):
        raise RuntimeError("secret unavailable")

    monkeypatch.setattr(room_fingerprint, "load_hmac_key", _boom)
    result = _poll(monkeypatch, _client())
    assert result["is_home"] is True
    kept = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert kept["room"] == "Office"
    assert kept["freshness"] == "fresh"
    assert "secret unavailable" not in json.dumps(kept)


def test_poller_does_not_guess_when_confidence_is_low(monkeypatch, fake_dynamodb):
    _ready(monkeypatch, fake_dynamodb)
    _seed_office(fake_dynamodb)
    weak = _client(connectivity={"signal": "weak", "score_bars": 1})
    _poll(monkeypatch, weak)
    second = _poll(monkeypatch, weak)
    assert second["room"] == "unknown"
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert stored["zone"] == "Office"
    assert stored["room"] == "unknown"
    assert stored["room_confidence"] == 0
