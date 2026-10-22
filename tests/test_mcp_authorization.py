"""Per-tool authorization on /mcp and the physical-access gate in execution.

read / execute / destructive scopes, 60-second step-up, single-use jti for
step-up calls, the per-call audit line, and fail-closed behaviour when the
issuer's keys cannot be reached. Tokens come from conftest's ``cw_issuer``
(cipherweave_verify.testing.LocalIssuer); nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from cipherweave_verify import Principal, reset_default_verifier
from cipherweave_verify import jwks as cw_jwks

import access_control
import execution_planner
import mcp_handler

LEGACY = "legacy-static-token-value"


@pytest.fixture(autouse=True)
def _legacy(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: LEGACY)
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)


@pytest.fixture
def deleted(monkeypatch):
    """Stub the delete route so a delete_device call has no side effect."""
    calls = []

    def _route_delete_device(device_id):
        calls.append(device_id)
        return {"statusCode": 200, "body": json.dumps({"deleted": device_id})}

    import app

    monkeypatch.setattr(app, "_route_delete_device", _route_delete_device)
    return calls


def _rpc(token, method, params=None):
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        body["params"] = params
    return mcp_handler.handler(
        {
            "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
            "headers": {"authorization": f"Bearer {token}"},
            "body": json.dumps(body),
        },
        None,
    )


def _call(token, name, arguments=None):
    return _rpc(token, "tools/call", {"name": name, "arguments": arguments or {}})


def _listed(token):
    return [t["name"] for t in json.loads(_rpc(token, "tools/list", {})["body"])["result"]["tools"]]


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

def test_every_tool_has_exactly_the_scope_its_annotations_say():
    for tool in mcp_handler.TOOLS:
        policy = mcp_handler.TOOL_POLICIES[tool["name"]]
        if tool["name"] == "execute":
            assert (policy.scope, policy.step_up) == ("execute", False)
        elif tool["name"] == "ingest" or tool["annotations"]["destructiveHint"]:
            assert (policy.scope, policy.step_up) == ("destructive", True), tool["name"]
        elif tool["annotations"]["readOnlyHint"]:
            assert (policy.scope, policy.step_up) == ("read", False), tool["name"]
        else:
            assert (policy.scope, policy.step_up) == ("execute", False), tool["name"]
    assert mcp_handler.TOOL_POLICIES["execute"].scope == "execute"
    assert mcp_handler.TOOL_POLICIES["delete_device"].step_up


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

def test_read_token_sees_and_calls_only_read_tools(fresh_token):
    token = fresh_token(("read",))
    listed = _listed(token)
    assert "health" in listed and "list_devices" in listed
    assert not {"execute", "delete_device", "ingest", "create_device"} & set(listed)

    response = _call(token, "execute", {"command": "unlock the front door"})
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error"]["code"] == mcp_handler.FORBIDDEN_CODE
    assert 'error="insufficient_scope"' in response["headers"]["WWW-Authenticate"]
    assert 'scope="execute"' in response["headers"]["WWW-Authenticate"]


def test_token_without_scopes_can_call_nothing(fresh_token):
    token = fresh_token(())
    assert _listed(token) == []
    assert _call(token, "health")["statusCode"] == 403


def test_audit_mode_lists_and_runs_scope_failures_but_keeps_step_up(fresh_token, monkeypatch, deleted):
    monkeypatch.setenv("CIPHERWEAVE_SCOPE_MODE", "audit")
    token = fresh_token(())
    assert "delete_device" in _listed(token)
    assert _call(token, "health")["statusCode"] == 200
    stale = fresh_token(("destructive",), now=int(time.time()) - 120)
    assert _call(stale, "delete_device", {"device_id": "d1"})["statusCode"] == 403
    assert deleted == []


# ---------------------------------------------------------------------------
# Step-up and replay
# ---------------------------------------------------------------------------

def test_fresh_destructive_token_deletes_once(fresh_token, deleted):
    token = fresh_token(("destructive",))
    assert _call(token, "delete_device", {"device_id": "d1"})["statusCode"] == 200
    replay = _call(token, "delete_device", {"device_id": "d2"})
    assert replay["statusCode"] == 403
    assert json.loads(replay["body"])["error"]["message"] == "Forbidden: token_replayed"
    assert deleted == ["d1"]


def test_destructive_token_older_than_60s_needs_step_up(fresh_token, deleted):
    stale = fresh_token(("destructive",), now=int(time.time()) - 90)
    response = _call(stale, "delete_device", {"device_id": "d1"})
    assert response["statusCode"] == 403
    challenge = response["headers"]["WWW-Authenticate"]
    assert 'error="insufficient_user_authentication"' in challenge and 'max_age="60"' in challenge
    assert deleted == []


def test_legacy_token_reads_but_never_steps_up(cw_issuer, deleted):
    assert _call(LEGACY, "health")["statusCode"] == 200
    response = _call(LEGACY, "delete_device", {"device_id": "d1"})
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error"]["message"] == "Forbidden: step_up_required"
    assert deleted == []


def test_legacy_scopes_can_be_narrowed(cw_issuer, monkeypatch):
    monkeypatch.setenv("LEGACY_TOKEN_SCOPES", "read")
    assert _call(LEGACY, "health")["statusCode"] == 200
    assert _call(LEGACY, "execute", {"command": "turn on the lamp"})["statusCode"] == 403


def test_step_up_without_a_ledger_fails_closed(fresh_token, monkeypatch, deleted):
    monkeypatch.setattr(mcp_handler, "_jti_ledger", lambda: None)
    response = _call(fresh_token(("destructive",)), "delete_device", {"device_id": "d1"})
    assert response["statusCode"] == 403
    assert "replay_ledger_unavailable" in response["body"]
    assert deleted == []


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_every_call_is_logged_with_sub_and_jti_never_the_token(cw_issuer, fresh_token, caplog, deleted):
    token = fresh_token(("read",), subject="teamweave-agent", jti="jti-123")
    with caplog.at_level(logging.INFO, logger="mcp_handler"):
        _call(token, "health")
        _call(token, "delete_device", {"device_id": "d1"})
    events = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "mcp_handler" and r.getMessage().startswith("{")
    ]
    assert [(e["tool"], e["outcome"], e["reason"]) for e in events] == [
        ("health", "ok", None),
        ("delete_device", "denied", "insufficient_scope"),
    ]
    assert all(e["sub"] == "teamweave-agent" and e["jti"] == "jti-123" for e in events)
    for part in token.split("."):
        assert part not in caplog.text


# ---------------------------------------------------------------------------
# Fail closed when the issuer's keys are unreachable
# ---------------------------------------------------------------------------

def test_unreachable_jwks_is_401_not_500_and_not_the_legacy_path(cw_issuer, fresh_token, monkeypatch):
    token = fresh_token(("read",))

    def _down(_url):
        raise OSError("issuer unreachable")

    monkeypatch.setattr(cw_jwks, "http_fetcher", _down)
    reset_default_verifier()

    def _no_legacy():
        raise AssertionError("a JWS that failed verification must not try the legacy secret")

    monkeypatch.setattr(mcp_handler, "_load_expected_token", _no_legacy)
    response = _call(token, "health")
    assert response["statusCode"] == 401


# ---------------------------------------------------------------------------
# Physical access inside execute
# ---------------------------------------------------------------------------

LOCK = {"id": "front-door", "name": "Front door", "device_type": "wyze_lock",
        "capabilities": ["lock", "unlock", "get_status"]}
GARAGE = {"id": "garage", "name": "Garage", "device_type": "myq_garage",
          "capabilities": ["open", "close", "toggle", "get_status"]}
LAMP = {"id": "lamp", "name": "Lamp", "device_type": "plug",
        "capabilities": ["turn_on", "turn_off", "toggle"]}


@pytest.fixture
def provider(monkeypatch):
    executed = []

    class _Provider:
        async def execute(self, device, action, params):
            executed.append((device["id"], action))
            return {"ok": True}

    monkeypatch.setattr(execution_planner, "get_provider", lambda _t: _Provider())
    return executed


def _run(*steps):
    return asyncio.run(execution_planner.execute_steps(
        [execution_planner.ExecutionStep(device=d, action=a) for d, a in steps]
    ))


def _bound(scopes, *, age=0, jti="j1", method="jwt"):
    now = int(time.time()) - age
    principal = Principal(method, "agent", frozenset(scopes), now, now + 900, jti)
    return access_control.bind(access_control.RequestAuth(principal, mcp_handler._AUTHORIZER))


@pytest.mark.parametrize("device,action", [(LOCK, "unlock"), (GARAGE, "open"), (GARAGE, "toggle")])
def test_physical_access_without_a_token_is_refused(provider, device, action):
    [result] = _run((device, action))
    assert not result.success and "physical access" in result.error
    assert provider == []


def test_safe_direction_and_ordinary_devices_need_no_step_up(provider):
    results = _run((LOCK, "lock"), (GARAGE, "close"), (LAMP, "toggle"))
    assert all(r.success for r in results)


def test_unlock_needs_destructive_scope_even_inside_execute(cw_issuer, provider):
    with _bound(("execute",)):
        [result] = _run((LOCK, "unlock"))
    assert not result.success and "insufficient_scope" in result.error
    assert provider == []


def test_unlock_needs_a_fresh_token(cw_issuer, provider):
    with _bound(("execute", "destructive"), age=61):
        [result] = _run((LOCK, "unlock"))
    assert not result.success and "step_up_required" in result.error


def test_fresh_destructive_token_unlocks_two_doors_on_one_jti(cw_issuer, provider):
    with _bound(("execute", "destructive")):
        results = _run((LOCK, "unlock"), (GARAGE, "open"))
    assert all(r.success for r in results)
    assert sorted(provider) == [("front-door", "unlock"), ("garage", "open")]
    # The jti is now spent: a second request with the same token is a replay.
    with _bound(("execute", "destructive")):
        [again] = _run((LOCK, "unlock"))
    assert not again.success and "token_replayed" in again.error


def test_legacy_token_cannot_unlock(cw_issuer, provider):
    with _bound(("read", "execute", "destructive"), method="legacy", jti=None):
        [result] = _run((LOCK, "unlock"))
    assert not result.success and "step_up_required" in result.error


def test_template_wires_scopes_and_a_put_only_jti_ledger():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "template.yaml").read_text(encoding="utf-8")
    for needle in (
        "CIPHERWEAVE_SCOPE_MODE: !Ref ToolScopeMode",
        "LEGACY_TOKEN_SCOPES: !Ref LegacyTokenScopes",
        "TOKEN_JTI_TABLE: !Ref TokenJtiTable",
        "AttributeName: expires_at",
    ):
        assert needle in template
    ledger_grant = template.split("Step-up replay ledger", 1)[1].split("- !If", 1)[0]
    assert "dynamodb:PutItem" in ledger_grant and "GetItem" not in ledger_grant
    assert "DeleteItem" not in ledger_grant and "UpdateItem" not in ledger_grant
    workflow = (root / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "ToolScopeMode=" in workflow


# ---------------------------------------------------------------------------
# Audit reason for failed execute calls
# ---------------------------------------------------------------------------

def _execute_events(token, device, action, caplog, monkeypatch):
    import app

    def _route_execute(_event):
        return app._execute_device(device, action, {}, f"{action} {device['name']}", 0.9, "cosine")

    monkeypatch.setattr(app, "_route_execute", _route_execute)
    with caplog.at_level(logging.INFO, logger="mcp_handler"):
        response = _call(token, "execute", {"command": f"{action} {device['name']}"})
    events = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "mcp_handler" and '"mcp_tool_call"' in r.getMessage()
    ]
    return response, events


def test_execute_refused_by_physical_access_is_audited_with_a_reason(
    cw_issuer, fresh_token, provider, caplog, monkeypatch
):
    token = fresh_token(("execute",))
    response, events = _execute_events(token, LOCK, "unlock", caplog, monkeypatch)
    assert json.loads(response["body"])["result"]["isError"] is True
    assert [(e["tool"], e["outcome"], e["reason"]) for e in events] == [
        ("execute", "error", "access_denied"),
    ]
    assert provider == []
    for part in token.split("."):
        assert part not in caplog.text


def test_execute_provider_failure_is_audited_with_the_exception_class(
    cw_issuer, fresh_token, caplog, monkeypatch
):
    from providers import ProviderError

    class _Failing:
        async def execute(self, device, action, params):
            raise ProviderError(device["id"], "Ring action 'get_status' failed: 404, url='https://x'")

    monkeypatch.setattr(execution_planner, "get_provider", lambda _t: _Failing())
    token = fresh_token(("execute",))
    _, events = _execute_events(token, LAMP, "turn_on", caplog, monkeypatch)
    assert [(e["tool"], e["outcome"], e["reason"]) for e in events] == [
        ("execute", "error", "provider_error:ProviderError"),
    ]
    assert "https://x" not in events[0]["reason"]
