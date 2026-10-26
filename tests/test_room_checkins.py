"""Labeled room check-ins stored in DynamoDB.

eero HTTP is a stand-in. The tables are moto's DynamoDB through a real
boto3 Table, so a Python float cannot sneak past put_item.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import aws_clients
import eero_calibration_handler
import eero_presence
import eero_presence_handler
import mcp_handler
import presence_store
import room_calibration_client
import room_checkin
import room_fingerprint

_eero_spec = importlib.util.spec_from_file_location(
    "test_eero_presence_checkins",
    Path(__file__).with_name("test_eero_presence.py"),
)
_eero = importlib.util.module_from_spec(_eero_spec)
_eero_spec.loader.exec_module(_eero)

_cal_spec = importlib.util.spec_from_file_location(
    "test_room_calibration_checkins",
    Path(__file__).with_name("test_room_calibration.py"),
)
_cal = importlib.util.module_from_spec(_cal_spec)
_cal_spec.loader.exec_module(_cal)

MAC = _eero.MAC
TOKEN = _cal.TOKEN
HMAC_KEY = _cal.HMAC_KEY
_account = _eero._account
_client = _eero._client
_envelope = _eero._envelope
_http = _cal._http
_install = _eero._install

ASKED = "2026-10-18T18:30:00+00:00"
ANSWERED = "2026-10-18T18:34:00+00:00"
REPO = Path(__file__).resolve().parents[1]


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


def _ready(monkeypatch, resource, *, track=True):
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("ROOM_CHECKIN_TABLE", "checkins")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setenv("EERO_DEVICE_HMAC_SECRET_NAME", "deviceweave/eero-device-hmac")
    monkeypatch.setenv("MCP_AUTH_TOKEN_SECRET_NAME", "deviceweave/mcp-auth-token")
    monkeypatch.setattr(aws_clients, "get_dynamodb_resource", lambda: resource)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    monkeypatch.setattr(room_fingerprint, "load_hmac_key", lambda name, client=None: HMAC_KEY)
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)
    clock = {"at": datetime.fromisoformat(ASKED)}
    monkeypatch.setattr(room_checkin, "now", lambda: clock["at"])
    if track:
        resource.Table("tracked").put_item(Item={
            "pk": f"mac:{eero_presence.normalize_mac(MAC)}",
            "person": "Raja",
            "match": "mac",
            "mac": MAC,
        })
    return clock


def _routes(client=None):
    return {
        "/account": _account(),
        "/networks/1001/devices": _envelope([client if client is not None else _client()]),
    }


def _no_secrets(blob: str):
    assert MAC not in blob
    assert MAC.replace(":", "") not in blob
    assert "Raja phone" not in blob
    assert "session-token" not in blob
    assert "aa:bb:cc:dd:ee:ff" not in blob.casefold()


def _stored_rows(table):
    return table.scan()["Items"]


def test_record_stores_hmac_chicago_features_and_json_numbers(monkeypatch, caplog):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        checkins = _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)
        _install(monkeypatch, _routes())

        response = eero_calibration_handler.handler(
            _http("POST", "/presence/checkins", {"person": "Raja", "room": "Office", "asked_at": ASKED}),
            None,
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["person"] == "Raja"
        assert body["room"] == "Office"
        assert body["source"] == "record"
        assert body["asked_at"] == datetime.fromisoformat(ASKED).astimezone(timezone.utc).isoformat()
        assert body["answered_at"] == body["asked_at"]
        assert body["timezone"] == "America/Chicago"
        assert body["local_hour"] == 13
        assert body["weekday"] == 6
        assert body["node"] == "Office eero"
        assert body["signal"] == "strong"
        assert body["signal_bucket"] == 4
        assert isinstance(body["signal_bucket"], int)
        assert body["freshness"] == "fresh"
        assert body["is_home"] is True
        assert body["device_hmac"] == room_fingerprint.device_hmac(MAC, HMAC_KEY)
        assert body["checkin_id"].startswith("raja~")
        _no_secrets(response["body"])

        stored = _stored_rows(checkins)
        assert len(stored) == 1
        assert isinstance(stored[0]["signal_bucket"], Decimal)
        assert isinstance(stored[0]["local_hour"], Decimal)
        assert stored[0]["signal_bucket"] == Decimal("4")
        blob = json.dumps(presence_store.from_dynamo(stored[0]))
        _no_secrets(blob)
        assert "nickname" not in stored[0]
        assert "mac" not in stored[0]
        assert MAC not in caplog.text
        assert "Raja phone" not in caplog.text


def test_record_stamps_presence_prediction_at_capture_time(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        checkins = _table(resource, "checkins", sort=True)
        presence = _table(resource, "presence")
        _table(resource, "tracked")
        monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
        _ready(monkeypatch, resource)
        _install(monkeypatch, _routes())
        presence.put_item(
            Item={
                "pk": "home_state",
                "people": [
                    {
                        "person": "Raja",
                        "is_home": True,
                        "room": "Living room",
                        "room_confidence": Decimal("0.82"),
                        "room_source": "fingerprint",
                    }
                ],
            }
        )

        response = eero_calibration_handler.handler(
            _http("POST", "/presence/checkins", {"person": "Raja", "room": "Office", "asked_at": ASKED}),
            None,
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["predicted_room"] == "Living room"
        assert body["predicted_confidence"] == 0.82
        assert body["predicted_source"] == "fingerprint"

        stored = presence_store.from_dynamo(_stored_rows(checkins)[0])
        assert stored["predicted_room"] == "Living room"
        assert stored["predicted_confidence"] == 0.82
        assert stored["predicted_source"] == "fingerprint"


def test_capture_then_label_keeps_the_ask_time_reading(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        clock = _ready(monkeypatch, resource)
        _install(monkeypatch, _routes())

        captured = eero_calibration_handler.handler(
            _http("POST", "/presence/checkins/capture", {"person": "Raja"}),
            None,
        )
        assert captured["statusCode"] == 200
        snap = json.loads(captured["body"])
        assert "room" not in snap
        assert "answered_at" not in snap
        assert snap["node"] == "Office eero"
        assert snap["local_hour"] == 13
        assert snap["weekday"] == 6
        checkin_id = snap["checkin_id"]

        clock["at"] = datetime.fromisoformat(ANSWERED)
        _install(monkeypatch, {
            "/account": (401, {"meta": {"code": 401, "error": "error.session.invalid"}}),
        })
        labeled = eero_calibration_handler.handler(
            _http(
                "POST",
                f"/presence/checkins/{checkin_id}/label",
                {"room": "Not home"},
                path_params={"checkin_id": checkin_id},
            ),
            None,
        )
        assert labeled["statusCode"] == 200
        body = json.loads(labeled["body"])
        assert body["room"] == "Not home"
        assert body["asked_at"] == snap["asked_at"]
        assert body["local_hour"] == 13
        assert body["weekday"] == 6
        assert body["node"] == "Office eero"
        assert body["signal_bucket"] == 4
        assert body["is_home"] is True
        assert body["answered_at"] == datetime.fromisoformat(ANSWERED).astimezone(timezone.utc).isoformat()
        assert body["freshness"] == "fresh"


def test_list_since_limit_and_delete(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)
        _install(monkeypatch, _routes())

        early = "2026-10-18T18:00:00+00:00"
        late = "2026-10-18T19:00:00+00:00"
        for stamp, room in ((early, "Kitchen"), (late, "Office")):
            response = eero_calibration_handler.handler({
                "action": "record_checkin",
                "person": "Raja",
                "room": room,
                "asked_at": stamp,
            }, None)
            assert response["statusCode"] == 200

        listed = eero_calibration_handler.handler(
            _http("GET", "/presence/checkins", query={"person": "Raja", "since": late, "limit": "10"}),
            None,
        )
        rows = json.loads(listed["body"])["checkins"]
        assert [row["room"] for row in rows] == ["Office"]

        oldest = eero_calibration_handler.handler(
            _http("GET", "/presence/checkins", query={"limit": "1"}),
            None,
        )
        assert json.loads(oldest["body"])["checkins"][0]["room"] == "Kitchen"

        checkin_id = rows[0]["checkin_id"]
        removed = eero_calibration_handler.handler(
            _http("DELETE", f"/presence/checkins/{checkin_id}", path_params={"checkin_id": checkin_id}),
            None,
        )
        assert json.loads(removed["body"]) == {"checkin_id": checkin_id, "deleted": 1}
        again = eero_calibration_handler.handler(
            {"action": "delete_checkin", "checkin_id": checkin_id},
            None,
        )
        assert json.loads(again["body"])["deleted"] == 0
        assert again["statusCode"] == 200


def test_untracked_person_is_rejected_before_write(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        checkins = _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource, track=False)
        seen = _install(monkeypatch, _routes())

        response = eero_calibration_handler.handler(
            {"action": "record_checkin", "person": "Raja", "room": "Office"},
            None,
        )
        assert response["statusCode"] == 400
        assert json.loads(response["body"])["error"] == "person is not tracked"
        assert _stored_rows(checkins) == []
        assert seen == []


def test_eero_failure_still_stores_the_label_without_marking_away(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)

        def _down(_name):
            raise RuntimeError("secret unreadable")

        monkeypatch.setattr(eero_presence_handler, "load_session_token", _down)
        response = eero_calibration_handler.handler(
            {"action": "record_checkin", "person": "Raja", "room": "Not home", "asked_at": ASKED},
            None,
        )
        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["room"] == "Not home"
        assert body["freshness"] == "stale"
        assert "is_home" not in body
        assert "node" not in body
        assert "device_hmac" not in body

        monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
        _install(monkeypatch, {
            "/account": (401, {"meta": {"code": 401, "error": "error.session.invalid"}}),
        })
        denied = eero_calibration_handler.handler(
            {"action": "capture_reading", "person": "Raja"},
            None,
        )
        snap = json.loads(denied["body"])
        assert denied["statusCode"] == 200
        assert snap["freshness"] == "stale"
        assert "is_home" not in snap
        assert "room" not in snap


def test_connected_poll_without_the_device_is_fresh_and_away(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)
        other = _client(mac="11:22:33:44:55:66", nickname="other")
        _install(monkeypatch, _routes(other))

        response = eero_calibration_handler.handler(
            {"action": "record_checkin", "person": "Raja", "room": "Office"},
            None,
        )
        body = json.loads(response["body"])
        assert body["freshness"] == "fresh"
        assert body["is_home"] is False
        assert "node" not in body
        assert "11:22:33:44:55:66" not in response["body"]
        _no_secrets(response["body"])


def test_hmac_failure_keeps_the_node_and_omits_the_digest(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)

        def _no_key(_name, client=None):
            raise RuntimeError("hmac secret down")

        monkeypatch.setattr(room_fingerprint, "load_hmac_key", _no_key)
        _install(monkeypatch, _routes())
        body = json.loads(eero_calibration_handler.handler(
            {"action": "record_checkin", "person": "Raja", "room": "Office"},
            None,
        )["body"])
        assert body["node"] == "Office eero"
        assert body["signal_bucket"] == 4
        assert "device_hmac" not in body


def test_put_checkin_converts_floats_and_public_record_is_json():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        checkins = _table(resource, "checkins", sort=True)
        item = {
            "pk": "person#raja",
            "sk": "20261018T183000Z~abcd1234",
            "checkin_id": "raja~20261018T183000Z~abcd1234",
            "person": "Raja",
            "room": "Office",
            "source": "record",
            "asked_at": ASKED,
            "timezone": "America/Chicago",
            "local_hour": 13,
            "weekday": 6,
            "freshness": "fresh",
            "signal_score": 0.25,
            "signal_bucket": 4,
        }
        room_checkin.put_checkin(checkins, item)
        stored = checkins.get_item(Key={"pk": item["pk"], "sk": item["sk"]})["Item"]
        assert stored["signal_score"] == Decimal("0.25")
        assert isinstance(stored["signal_bucket"], Decimal)
        decoded = json.loads(json.dumps(presence_store.from_dynamo(stored)))
        assert decoded["signal_score"] == 0.25
        view = room_checkin.public_record(stored)
        assert json.loads(json.dumps(view))["signal_bucket"] == 4
        assert "pk" not in view and "sk" not in view
        assert "signal_score" not in view


def test_http_requires_the_bearer_and_does_not_call_eero(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _table(resource, "checkins", sort=True)
        _table(resource, "tracked")
        _ready(monkeypatch, resource)
        seen = _install(monkeypatch, _routes())

        denied = eero_calibration_handler.handler(
            _http("POST", "/presence/checkins", {"person": "Raja", "room": "Office"}, token=None),
            None,
        )
        assert denied["statusCode"] == 401
        assert seen == []

        missing = eero_calibration_handler.handler(
            _http(
                "POST",
                "/presence/checkins/missing/label",
                {"room": "Office"},
                path_params={"checkin_id": "raja~20261018T183000Z~abcd1234"},
            ),
            None,
        )
        assert missing["statusCode"] == 404


def test_mcp_tools_invoke_the_calibration_function_without_a_mac(monkeypatch, fresh_token):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)
    seen = []

    def invoke(action, payload):
        seen.append((action, dict(payload)))
        return {
            "statusCode": 200,
            "body": json.dumps({"checkin_id": "raja~20261018T183000Z~abcd1234", "room": payload.get("room")}),
        }

    monkeypatch.setattr(room_calibration_client, "invoke", invoke)
    calls = [
        ("record_room_checkin", {"person": "Raja", "room": "Office", "asked_at": ASKED}),
        ("capture_room_reading", {"person": "Raja"}),
        ("label_room_checkin", {"checkin_id": "raja~20261018T183000Z~abcd1234", "room": "Not home"}),
        ("list_room_checkins", {"person": "Raja", "since": ASKED, "limit": 10}),
        ("delete_room_checkin", {"checkin_id": "raja~20261018T183000Z~abcd1234"}),
    ]
    expected = [
        ("record_checkin", {"person": "Raja", "room": "Office", "asked_at": ASKED}),
        ("capture_reading", {"person": "Raja"}),
        ("label_checkin", {"checkin_id": "raja~20261018T183000Z~abcd1234", "room": "Not home"}),
        ("list_checkins", {"person": "Raja", "since": ASKED, "limit": 10}),
        ("delete_checkin", {"checkin_id": "raja~20261018T183000Z~abcd1234"}),
    ]
    for name, arguments in calls:
        response = mcp_handler.handler({
            "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
            "headers": {"authorization": f"Bearer {fresh_token()}"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }),
        }, None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["result"]["isError"] is False
    assert seen == expected
    for _action, payload in seen:
        assert "mac" not in payload
        assert MAC not in json.dumps(payload)


def test_template_gives_checkins_only_to_the_calibration_function():
    template = (REPO / "template.yaml").read_text(encoding="utf-8")
    poller = template.split("EeroPresenceFunction:", 1)[1].split("EeroPresenceFunctionLogGroup:", 1)[0]
    calibration = template.split("EeroCalibrationFunction:", 1)[1].split("EeroCalibrationFunctionLogGroup:", 1)[0]
    mcp = template.split("DeviceWeaveMcpFunction:", 1)[1].split("DeviceWeaveMcpFunctionLogGroup:", 1)[0]
    table = template.split("RoomCheckinTable:", 1)[1].split("PolicyTable:", 1)[0]

    assert "dynamodb:DeleteItem" not in poller
    assert "dynamodb:PutItem" in poller
    assert "!GetAtt RoomCheckinTable.Arn" in poller
    assert "RoomCheckinTable" not in mcp
    assert "RoomScheduleTable" not in mcp
    assert "lambda:InvokeFunction" in mcp
    assert "TableName: !Ref RoomCheckinTable" in calibration
    assert "DynamoDBCrudPolicy:" in calibration
    assert "PAY_PER_REQUEST" in table
    assert "SSESpecification" not in table
    assert "TimeToLiveSpecification" not in table
    assert "RoleName" not in table
    assert 'TableName: !Sub "deviceweave-room-checkins-${StageName}"' in table


def test_blank_room_and_bad_id_are_400():
    with pytest.raises(room_checkin.CheckinError) as blank:
        room_checkin.validate_room_label("   ")
    assert blank.value.status == 400
    assert room_checkin.validate_room_label(" Not home ") == "Not home"
    with pytest.raises(room_checkin.CheckinError) as bad_id:
        room_checkin._parse_id("not-an-id")
    assert bad_id.value.status == 400
