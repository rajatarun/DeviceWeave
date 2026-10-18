"""Fixed local-time room rules, and the poller check-in they write.

eero HTTP is a stand-in. DynamoDB is moto through a real boto3 Table.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import boto3
from moto import mock_aws

import aws_clients
import eero_calibration_handler
import eero_presence
import eero_presence_handler
import mcp_handler
import presence_store
import room_calibration_client
import room_fingerprint
import room_schedule

_eero_spec = importlib.util.spec_from_file_location(
    "test_eero_presence_schedule",
    Path(__file__).with_name("test_eero_presence.py"),
)
_eero = importlib.util.module_from_spec(_eero_spec)
_eero_spec.loader.exec_module(_eero)

_cal_spec = importlib.util.spec_from_file_location(
    "test_room_calibration_schedule",
    Path(__file__).with_name("test_room_calibration.py"),
)
_cal = importlib.util.module_from_spec(_cal_spec)
_cal_spec.loader.exec_module(_cal)

MAC = _eero.MAC
TOKEN = _cal.TOKEN
HMAC_KEY = _cal.HMAC_KEY
_client = _eero._client
_http = _cal._http
_install = _eero._install

# 23:30 America/Chicago (CDT, UTC-5) on Sunday 18 Oct 2026.
NIGHT = "2026-10-19T04:30:00+00:00"
NIGHT_LATER = "2026-10-19T04:40:00+00:00"
# 00:30 the next local day.
NEXT_HOUR = "2026-10-19T05:30:00+00:00"
# 10:00 America/Chicago.
DAY = "2026-10-18T15:00:00+00:00"


def _table(resource, name, *, sort=False):
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


def test_window_wraps_midnight_and_rejects_an_overlap():
    zone = datetime.fromisoformat(NIGHT)
    assert room_schedule.in_window("23:00", "06:00", zone.astimezone(room_schedule.ZoneInfo("America/Chicago")))
    early = datetime.fromisoformat("2026-10-19T07:00:00+00:00")  # 02:00 CDT
    assert room_schedule.in_window("23:00", "06:00", early.astimezone(room_schedule.ZoneInfo("America/Chicago")))
    end = datetime.fromisoformat("2026-10-19T11:00:00+00:00")  # 06:00 CDT
    assert not room_schedule.in_window("23:00", "06:00", end.astimezone(room_schedule.ZoneInfo("America/Chicago")))
    day = datetime.fromisoformat(DAY)
    assert not room_schedule.in_window("23:00", "06:00", day.astimezone(room_schedule.ZoneInfo("America/Chicago")))


def test_set_list_delete_round_trip_and_http_auth(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "schedule", sort=True)
        monkeypatch.setenv("ROOM_SCHEDULE_TABLE", "schedule")
        monkeypatch.setenv("MCP_AUTH_TOKEN_SECRET_NAME", "deviceweave/mcp-auth-token")
        monkeypatch.setattr(aws_clients, "get_dynamodb_resource", lambda: resource)
        monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)

        denied = eero_calibration_handler.handler(
            _http("POST", "/presence/schedule", {
                "person": "Raja", "room": "Master bedroom", "start": "23:00", "end": "06:00",
            }, token=None),
            None,
        )
        assert denied["statusCode"] == 401

        stored = eero_calibration_handler.handler(
            _http("POST", "/presence/schedule", {
                "person": "Raja",
                "room": "Master bedroom",
                "start": "23:00",
                "end": "06:00",
            }),
            None,
        )
        assert stored["statusCode"] == 200
        rule = json.loads(stored["body"])
        assert rule["rule_id"] == "raja~23:00~06:00"
        assert rule["room"] == "Master bedroom"
        assert rule["timezone"] == "America/Chicago"

        overlap = eero_calibration_handler.handler({
            "action": "set_schedule",
            "person": "Raja",
            "room": "Office",
            "start": "01:00",
            "end": "07:00",
        }, None)
        assert overlap["statusCode"] == 400

        listed = json.loads(eero_calibration_handler.handler(
            _http("GET", "/presence/schedule", query={"person": "Raja"}),
            None,
        )["body"])
        assert [item["rule_id"] for item in listed["rules"]] == ["raja~23:00~06:00"]

        removed = json.loads(eero_calibration_handler.handler(
            _http(
                "DELETE",
                "/presence/schedule/raja~23:00~06:00",
                path_params={"rule_id": "raja~23:00~06:00"},
            ),
            None,
        )["body"])
        assert removed == {"rule_id": "raja~23:00~06:00", "deleted": 1}
        again = json.loads(eero_calibration_handler.handler(
            {"action": "delete_schedule", "rule_id": "raja~23:00~06:00"},
            None,
        )["body"])
        assert again["deleted"] == 0


def test_poller_applies_the_night_rule_and_writes_one_checkin_per_hour(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        presence = _table(resource, "presence")
        _table(resource, "tracked")
        schedule = _table(resource, "schedule", sort=True)
        checkins = _table(resource, "checkins", sort=True)
        monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
        monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
        monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
        monkeypatch.setenv("EERO_DEVICE_HMAC_SECRET_NAME", "deviceweave/eero-device-hmac")
        monkeypatch.setenv("ROOM_SCHEDULE_TABLE", "schedule")
        monkeypatch.setenv("ROOM_CHECKIN_TABLE", "checkins")
        monkeypatch.setattr(aws_clients, "get_dynamodb_resource", lambda: resource)
        monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
        monkeypatch.setattr(room_fingerprint, "load_hmac_key", lambda name, client=None: HMAC_KEY)
        resource.Table("tracked").put_item(Item={
            "pk": f"mac:{eero_presence.normalize_mac(MAC)}",
            "person": "Raja",
            "match": "mac",
            "mac": MAC,
        })
        room_schedule.set_rule(schedule, "Raja", "Master bedroom", "23:00", "06:00")
        client = _client(
            source={"location": "Family Room", "display_name": "Family Room eero"},
            connectivity={"signal": "-54 dBm", "score": 1.0, "score_bars": 1},
        )
        _install(monkeypatch, {
            "/account": _eero._account(),
            "/networks/1001/devices": _eero._envelope([client]),
        })
        clock = {"at": NIGHT}
        monkeypatch.setattr(eero_presence_handler, "_now", lambda: clock["at"])

        first = eero_presence_handler.handler({"source": "scheduler"}, None)
        assert first["room"] == "Master bedroom"
        assert first["room_confidence"] == 1.0
        stored = presence.get_item(Key={"pk": "home_state"})["Item"]
        assert isinstance(stored["room_confidence"], Decimal)
        assert stored["room_confidence"] == Decimal("1")
        assert stored["room_source"] == "schedule_rule"
        view = presence_store.public_view(stored)
        assert view["room_source"] == "schedule_rule"
        assert "schedule_checkin_key" not in json.dumps(view)
        rows = checkins.scan()["Items"]
        assert len(rows) == 1
        row = presence_store.from_dynamo(rows[0])
        assert row["room"] == "Master bedroom"
        assert row["source"] == "schedule_rule"
        assert row["local_hour"] == 23
        assert row["weekday"] == 6
        assert row["timezone"] == "America/Chicago"
        assert row["node"] == "Family Room eero"
        assert row["signal"] == "-54 dBm"
        assert row["signal_bucket"] == 4
        assert row["is_home"] is True
        blob = json.dumps(row)
        assert MAC not in blob
        assert "Raja phone" not in blob

        clock["at"] = NIGHT_LATER
        eero_presence_handler.handler({"source": "scheduler"}, None)
        assert len(checkins.scan()["Items"]) == 1

        clock["at"] = NEXT_HOUR
        second = eero_presence_handler.handler({"source": "scheduler"}, None)
        assert second["room"] == "Master bedroom"
        rows = [presence_store.from_dynamo(item) for item in checkins.scan()["Items"]]
        assert len(rows) == 2
        assert sorted(item["local_hour"] for item in rows) == [0, 23]
        assert {item["weekday"] for item in rows} == {0, 6}

        clock["at"] = DAY
        day = eero_presence_handler.handler({"source": "scheduler"}, None)
        assert day["room"] == "unknown"
        assert "room_source" not in presence.get_item(Key={"pk": "home_state"})["Item"]
        assert len(checkins.scan()["Items"]) == 2


def test_mcp_schedule_tools_invoke_the_calibration_function(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)
    seen = []

    def invoke(action, payload):
        seen.append((action, dict(payload)))
        return {"statusCode": 200, "body": json.dumps({"rule_id": "raja~23:00~06:00", "rules": []})}

    monkeypatch.setattr(room_calibration_client, "invoke", invoke)
    for name, arguments in (
        ("set_room_schedule_rule", {
            "person": "Raja", "room": "Master bedroom", "start": "23:00", "end": "06:00",
        }),
        ("list_room_schedule_rules", {"person": "Raja"}),
        ("delete_room_schedule_rule", {"rule_id": "raja~23:00~06:00"}),
    ):
        response = mcp_handler.handler({
            "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
            "headers": {"authorization": f"Bearer {TOKEN}"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }),
        }, None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["result"]["isError"] is False
    assert seen == [
        ("set_schedule", {
            "person": "Raja", "room": "Master bedroom", "start": "23:00", "end": "06:00",
        }),
        ("list_schedule", {"person": "Raja"}),
        ("delete_schedule", {"rule_id": "raja~23:00~06:00"}),
    ]
    assert MAC not in json.dumps(seen)
