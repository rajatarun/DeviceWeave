"""
Eero presence: client-list parsing, device-to-person mapping, debounce,
and the expired-session fail-safe.

Every eero HTTP call is a stand-in. Nothing in this file opens a socket.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import eero_client
import eero_presence
import eero_presence_handler
import presence_store

REPO = Path(__file__).resolve().parents[1]
NOW = "2026-10-18T16:05:00+00:00"
MAC = "aa:bb:cc:dd:ee:ff"


class _Response:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _envelope(data, code=200):
    return {"meta": {"code": code}, "data": data}


def _client(**overrides):
    device = {
        "mac": MAC,
        "nickname": "Raja phone",
        "hostname": "raja-phone",
        "display_name": "Raja phone",
        "connected": True,
        "source": {"location": "Office", "display_name": "Office eero"},
        "connectivity": {"signal": "strong", "score": 0.9, "score_bars": 4},
        "last_active": "2026-10-18T16:00:00.000Z",
    }
    device.update(overrides)
    return device


def _account():
    return _envelope({
        "networks": {"data": [{"url": "/2.2/networks/1001", "name": "Home"}]},
    })


def _install(monkeypatch, routes):
    """routes: path suffix → body dict or (status, body)."""
    seen = []

    def urlopen(req, timeout=0):
        seen.append(req)
        path = req.full_url.split(eero_client.API_BASE, 1)[-1]
        if path not in routes:
            raise AssertionError(f"unexpected eero URL {req.full_url}")
        route = routes[path]
        status, body = route if isinstance(route, tuple) else (200, route)
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "no", hdrs=None, fp=io.BytesIO(json.dumps(body).encode("utf-8"))
            )
        return _Response(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_login_and_verify_use_the_unofficial_auth_flow(monkeypatch):
    seen = _install(monkeypatch, {
        "/login": _envelope({"user_token": "verify-session"}),
        "/login/verify": _envelope({"user_token": "long-session"}),
    })

    user_token = eero_client.login("person@example.com")
    session = eero_client.login_verify("123456", user_token)

    assert user_token == "verify-session"
    assert session == "long-session"
    assert seen[0].full_url == f"{eero_client.API_BASE}/login"
    assert json.loads(seen[0].data.decode()) == {"login": "person@example.com"}
    assert seen[0].get_header("Cookie") is None
    assert seen[1].full_url == f"{eero_client.API_BASE}/login/verify"
    assert json.loads(seen[1].data.decode()) == {"code": "123456"}
    assert seen[1].get_header("Cookie") == "s=verify-session"


def _assert_amazon_workaround(message: str) -> None:
    assert "Amazon" in message
    assert "one-time code" in message
    assert "network admin" in message
    assert "accept the invite" in message
    assert "dedicated admin" in message


def test_login_401_error_login_unknown_explains_the_admin_workaround(monkeypatch):
    _install(monkeypatch, {
        "/login": (401, {"meta": {"code": 401, "error": "error.login.unknown"}}),
    })
    try:
        eero_client.login("owner@example.com")
    except eero_client.EeroAuthError as exc:
        assert exc.status == 401
        assert exc.error_code == "error.login.unknown"
        _assert_amazon_workaround(str(exc))
    else:
        raise AssertionError("Amazon-linked login should fail")


def test_login_401_without_error_code_still_explains_the_workaround(monkeypatch):
    _install(monkeypatch, {
        "/login": (401, {"meta": {"code": 401}}),
    })
    try:
        eero_client.login("owner@example.com")
    except eero_client.EeroAuthError as exc:
        assert exc.status == 401
        _assert_amazon_workaround(str(exc))
    else:
        raise AssertionError("HTTP 401 on login should fail")


def test_login_cli_prints_the_amazon_workaround(monkeypatch, capsys):
    _install(monkeypatch, {
        "/login": (401, {"meta": {"code": 401, "error": "error.login.unknown"}}),
    })
    script = _login_cli()
    code = script.main(["login", "--identifier", "owner@example.com"])
    err = capsys.readouterr().err
    assert code == 1
    _assert_amazon_workaround(err)


def test_session_401_is_not_reported_as_an_amazon_login(monkeypatch):
    _install(monkeypatch, {
        "/account": (401, {"meta": {"code": 401, "error": "error.session.invalid"}}),
    })
    try:
        eero_client.list_connected_clients("session-token")
    except eero_client.EeroAuthError as exc:
        assert exc.error_code == "error.session.invalid"
        assert "network admin" not in str(exc)
        assert "dedicated admin" not in str(exc)
    else:
        raise AssertionError("expired session should fail")


def _login_cli():
    path = REPO / "scripts" / "eero_login.py"
    spec = importlib.util.spec_from_file_location("eero_login_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_client_list_parses_into_presence(monkeypatch):
    seen = _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })

    clients = eero_client.list_connected_clients("session-token")
    presence = eero_presence.compute_presence(
        None,
        [{"person": "Raja", "match": "mac", "mac": "AA-BB-CC-DD-EE-FF"}],
        clients,
        miss_threshold=3,
        now=NOW,
    )

    assert seen[0].get_header("Cookie") == "s=session-token"
    assert seen[1].full_url.endswith("/networks/1001/devices")
    assert presence["is_home"] is True
    assert presence["freshness"] == "fresh"
    assert presence["source"] == "eero"
    assert presence["zone"] == "Office"
    assert presence["node"] == "Office eero"
    assert presence["signal"] == "strong"
    assert presence["last_seen"] == "2026-10-18T16:00:00.000Z"
    person = presence["people"][0]
    assert person["person"] == "Raja"
    assert person["is_home"] is True
    assert person["miss_count"] == 0
    assert person["zone"] == "Office"
    assert person["signal"] == "strong"


def test_device_to_person_mapping_by_mac_and_name():
    clients = [
        _client(),
        _client(
            mac="11:22:33:44:55:66",
            nickname="Sam phone",
            hostname="sam-phone",
            display_name="Sam phone",
            source={"location": "Kitchen", "display_name": "Kitchen eero"},
            connectivity={"signal": "fair"},
            last_active="2026-10-18T16:04:00.000Z",
        ),
        _client(
            mac="99:88:77:66:55:44",
            nickname="Guest laptop",
            hostname="guest",
            display_name="Guest laptop",
            source={"location": "Porch", "display_name": "Porch eero"},
        ),
    ]
    presence = eero_presence.compute_presence(
        None,
        [
            {"person": "Raja", "match": "mac", "mac": MAC},
            {"person": "Sam", "match": "name", "name": "Sam Phone"},
            {"person": "Raja", "match": "name", "name": "Raja tablet"},
        ],
        clients,
        miss_threshold=3,
        now=NOW,
    )

    by_person = {person["person"]: person for person in presence["people"]}
    assert set(by_person) == {"Raja", "Sam"}
    assert by_person["Raja"]["zone"] == "Office"
    assert by_person["Raja"]["node"] == "Office eero"
    assert by_person["Sam"]["zone"] == "Kitchen"
    assert by_person["Sam"]["node"] == "Kitchen eero"
    assert by_person["Sam"]["signal"] == "fair"
    assert presence["is_home"] is True
    # Two people home: the aggregate zone follows the latest last_seen.
    assert presence["zone"] == "Kitchen"
    assert presence["node"] == "Kitchen eero"


def test_any_mapped_device_keeps_the_person_home():
    clients = [
        _client(connected=False, last_active="2026-10-18T12:00:00.000Z"),
        _client(
            mac="11:22:33:44:55:66",
            nickname="Raja tablet",
            hostname="raja-tablet",
            display_name="Raja tablet",
            source={"location": "Den", "display_name": "Den eero"},
            connectivity={"score_bars": 3},
            last_active="2026-10-18T16:04:00.000Z",
        ),
    ]
    presence = eero_presence.compute_presence(
        None,
        [
            {"person": "Raja", "match": "mac", "mac": MAC},
            {"person": "Raja", "match": "name", "name": "Raja tablet"},
        ],
        clients,
        miss_threshold=3,
        now=NOW,
    )

    assert len(presence["people"]) == 1
    person = presence["people"][0]
    assert person["is_home"] is True
    assert person["zone"] == "Den"
    assert person["node"] == "Den eero"
    assert person["signal"] == "3/5"
    assert presence["is_home"] is True


def test_debounce_holds_home_until_consecutive_misses():
    tracked = [{"person": "Raja", "match": "mac", "mac": MAC}]
    state = eero_presence.compute_presence(
        None, tracked, [_client()], miss_threshold=3, now="2026-10-18T16:00:00+00:00",
    )
    assert state["is_home"] is True
    assert state["zone"] == "Office"

    for expected in (1, 2):
        state = eero_presence.compute_presence(
            state, tracked, [], miss_threshold=3, now=f"2026-10-18T16:0{expected}:00+00:00",
        )
        assert state["is_home"] is True, expected
        assert state["zone"] == "Office"
        assert state["node"] == "Office eero"
        assert state["signal"] == "strong"
        assert state["people"][0]["miss_count"] == expected
        assert state["people"][0]["is_home"] is True

    state = eero_presence.compute_presence(
        state, tracked, [_client(connected=False)], miss_threshold=3, now="2026-10-18T16:15:00+00:00",
    )
    assert state["is_home"] is False
    assert state.get("zone") is None
    assert state.get("node") is None
    assert state.get("signal") is None
    assert state["people"][0]["miss_count"] == 3
    assert state["people"][0]["last_seen"] == "2026-10-18T16:00:00.000Z"
    assert state["freshness"] == "fresh"

    state = eero_presence.compute_presence(
        state,
        tracked,
        [_client(source={"location": "Kitchen", "display_name": "Kitchen eero"})],
        miss_threshold=3,
        now="2026-10-18T18:00:00+00:00",
    )
    assert state["is_home"] is True
    assert state["zone"] == "Kitchen"
    assert state["people"][0]["miss_count"] == 0


def test_wrapped_device_list_and_node_name_without_location():
    clients = eero_client.unwrap_devices({
        "devices": [_client(source={"display_name": "Upstairs"})],
    })
    presence = eero_presence.compute_presence(
        None,
        [{"person": "Raja", "match": "name", "name": "raja phone"}],
        clients,
        miss_threshold=3,
        now=NOW,
    )
    assert presence["zone"] == "Upstairs"
    assert presence["node"] == "Upstairs"


def test_expired_session_keeps_last_presence_and_marks_it_stale(monkeypatch, fake_dynamodb, caplog):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setenv("EERO_AWAY_MISS_THRESHOLD", "3")
    monkeypatch.setattr(aws_clients(), "get_dynamodb_resource", lambda: fake_dynamodb)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req)
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"meta":{"code":401,"error":"error.session.invalid"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    previous = {
        "pk": "home_state",
        "is_home": True,
        "updated_at": "2026-10-18T15:00:00+00:00",
        "zone": "Office",
        "node": "Office eero",
        "signal": "strong",
        "last_seen": "2026-10-18T15:00:00+00:00",
        "freshness": "fresh",
        "source": "eero",
        "people": [{
            "person": "Raja",
            "is_home": True,
            "zone": "Office",
            "node": "Office eero",
            "signal": "strong",
            "last_seen": "2026-10-18T15:00:00+00:00",
            "freshness": "fresh",
            "miss_count": 1,
        }],
    }
    fake_dynamodb.Table("presence").put_item(Item=previous)

    with caplog.at_level("ERROR"):
        result = eero_presence_handler.handler({}, None)

    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert result["status"] == "stale"
    assert result["is_home"] is True
    assert stored["is_home"] is True
    assert stored["zone"] == "Office"
    assert stored["node"] == "Office eero"
    assert stored["signal"] == "strong"
    assert stored["freshness"] == "stale"
    assert "session rejected" in stored["stale_reason"]
    assert stored["people"][0]["is_home"] is True
    assert stored["people"][0]["miss_count"] == 1
    assert len(calls) == 1
    assert "eero session rejected" in caplog.text
    assert "Not marking anyone away" in caplog.text

    view = presence_store.public_view(stored)
    assert view["is_home"] is True
    assert view["freshness"] == "stale"
    assert view["zone"] == "Office"
    assert view["signal"] == "strong"


def test_poll_writes_presence_that_get_presence_returns(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setenv("EERO_AWAY_MISS_THRESHOLD", "3")
    monkeypatch.setattr(aws_clients(), "get_dynamodb_resource", lambda: fake_dynamodb)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })
    fake_dynamodb.Table("tracked").put_item(Item={
        "pk": f"mac:{eero_presence.normalize_mac(MAC)}",
        "person": "Raja",
        "match": "mac",
        "mac": MAC,
    })

    result = eero_presence_handler.handler({"source": "scheduler"}, None)
    assert result["status"] == "updated"
    assert result["zone"] == "Office"

    import app
    monkeypatch.setattr(app, "get_dynamodb_resource", lambda: fake_dynamodb)
    response = app._route_get_presence()
    body = json.loads(response["body"])
    assert body["is_home"] is True
    assert body["zone"] == "Office"
    assert body["node"] == "Office eero"
    assert body["signal"] == "strong"
    assert body["freshness"] == "fresh"
    assert body["last_seen"] == "2026-10-18T16:00:00.000Z"
    assert body["people"][0]["person"] == "Raja"


def test_blank_secret_name_does_not_call_eero_or_mark_away(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "   ")
    monkeypatch.setattr(aws_clients(), "get_dynamodb_resource", lambda: fake_dynamodb)
    fake_dynamodb.Table("presence").put_item(Item={
        "pk": "home_state", "is_home": True, "zone": "Office", "freshness": "fresh",
    })

    def urlopen(req, timeout=0):
        raise AssertionError("eero was called")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = eero_presence_handler.handler({}, None)
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert result["status"] == "stale"
    assert stored["is_home"] is True
    assert stored["zone"] == "Office"
    assert stored["freshness"] == "stale"


def test_no_tracked_devices_does_not_mark_everyone_away(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_TRACKED_TABLE", "tracked")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setattr(aws_clients(), "get_dynamodb_resource", lambda: fake_dynamodb)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")
    _install(monkeypatch, {
        "/account": _account(),
        "/networks/1001/devices": _envelope([_client()]),
    })
    fake_dynamodb.Table("presence").put_item(Item={
        "pk": "home_state", "is_home": True, "zone": "Office", "freshness": "fresh",
    })

    result = eero_presence_handler.handler({}, None)
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert result["status"] == "stale"
    assert stored["is_home"] is True
    assert "no tracked" in stored["stale_reason"]


def test_manual_set_preserves_zone_and_marks_the_write_fresh(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    import app
    monkeypatch.setattr(app, "get_dynamodb_resource", lambda: fake_dynamodb)
    fake_dynamodb.Table("presence").put_item(Item={
        "pk": "home_state",
        "is_home": True,
        "zone": "Office",
        "node": "Office eero",
        "signal": "strong",
        "freshness": "stale",
        "source": "eero",
        "stale_reason": "eero session rejected",
        "people": [{"person": "Raja", "is_home": True, "miss_count": 2}],
    })

    response = app._route_presence({"body": json.dumps({"is_home": False})})
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["is_home"] is False
    assert body["zone"] == "Office"
    assert body["node"] == "Office eero"
    assert body["signal"] == "strong"
    assert body["freshness"] == "fresh"
    assert body["source"] == "manual"
    assert "stale_reason" not in body
    assert body["people"][0]["person"] == "Raja"


def test_rejected_session_with_no_prior_presence_stays_home(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("PRESENCE_TABLE_NAME", "presence")
    monkeypatch.setenv("EERO_SESSION_SECRET_NAME", "deviceweave/eero-session")
    monkeypatch.setattr(aws_clients(), "get_dynamodb_resource", lambda: fake_dynamodb)
    monkeypatch.setattr(eero_presence_handler, "load_session_token", lambda name: "session-token")

    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"meta":{"code":401,"error":"error.session.refresh"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = eero_presence_handler.handler({}, None)
    stored = fake_dynamodb.Table("presence").get_item(Key={"pk": "home_state"})["Item"]
    assert result["is_home"] is True
    assert stored["is_home"] is True
    assert stored["freshness"] == "stale"


def test_template_requires_a_secret_name_and_keeps_the_poller_serverless():
    template = (REPO / "template.yaml").read_text(encoding="utf-8")
    workflow = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

    secret = _param_block(template, "EeroSessionSecretName")
    schedule = _param_block(template, "EeroPollSchedule")
    assert "MinLength: 1" in secret
    secret_pattern = _allowed_pattern(secret)
    schedule_pattern = _allowed_pattern(schedule)
    assert re.fullmatch(secret_pattern, "deviceweave/eero-session")
    assert re.fullmatch(secret_pattern, "") is None
    assert re.fullmatch(schedule_pattern, "rate(5 minutes)")
    assert re.fullmatch(schedule_pattern, "") is None
    assert re.fullmatch(schedule_pattern, "rate(0 minutes)") is None

    function = _resource_block(template, "EeroPresenceFunction")
    assert "VpcConfig" not in function
    assert "AWSSecretsManagerGetSecretValuePolicy" in function
    assert "EeroSessionSecretName" in function
    assert "secretsmanager:PutSecretValue" not in function
    assert "secretsmanager:UpdateSecret" not in function
    poll = _resource_block(template, "EeroPresencePollSchedule")
    assert "AWS::Scheduler::Schedule" in poll
    assert "RoleArn: !GetAtt EeroPresenceScheduleRole.Arn" in poll
    # A RoleName or ManagedPolicyName makes CreateChangeSet demand
    # CAPABILITY_NAMED_IAM. The schedule role is referenced only by ARN.
    assert "RoleName:" not in template
    assert "ManagedPolicyName:" not in template
    assert "deviceweave-eero-presence-schedule" not in template

    assert "EeroSessionSecretName=deviceweave/eero-session" in workflow
    assert "--capabilities CAPABILITY_IAM" in workflow
    assert "CAPABILITY_NAMED_IAM" not in workflow
    assert 'EeroPollSchedule="rate(5 minutes)"' in workflow
    assert '"EeroPollSchedule=rate(5 minutes)"' not in workflow
    assert 'ParameterValue=""' not in workflow.split("EeroSessionSecretName")[1][:80]


def aws_clients():
    import aws_clients as clients
    return clients


def _param_block(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [A-Za-z0-9]+:|\nResources:)", text, re.M | re.S)
    assert match, name
    return match.group(1)


def _resource_block(text: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [A-Za-z0-9]+:|\Z)", text, re.M | re.S)
    assert match, name
    return match.group(1)


def _allowed_pattern(block: str) -> str:
    match = re.search(r"AllowedPattern:\s*'([^']+)'|AllowedPattern:\s*\"([^\"]+)\"|AllowedPattern:\s*(\S+)", block)
    assert match, block
    return next(group for group in match.groups() if group)
