"""Unit tests for the remote MCP endpoint (Streamable HTTP, protocol 2025-06-18)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import mcp_handler

TOKEN = "expected-token-value"
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "template.yaml"

_REAL_LOAD = mcp_handler._load_expected_token


def _event(method, body=None, token=TOKEN, headers=None):
    hdrs = {}
    if token is not None:
        hdrs["authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update({str(k).lower(): v for k, v in headers.items()})
    event = {
        "requestContext": {"http": {"method": method, "path": "/mcp"}},
        "headers": hdrs,
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _rpc(method, params=None, req_id=1, token=TOKEN, headers=None):
    message = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return _event("POST", message, token=token, headers=headers)


def _body(response):
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: TOKEN)


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------

def test_initialize_returns_protocol_capabilities_and_server_info():
    response = mcp_handler.handler(_rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
        req_id=1,
        headers={"MCP-Protocol-Version": "2025-06-18"},
    ), None)

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    result = payload["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "deviceweave"
    assert result["serverInfo"]["version"]
    assert "execute" in result["instructions"]


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

def test_tools_list_exposes_schemas_and_destructive_hints():
    response = mcp_handler.handler(_rpc("tools/list", {}, req_id="list-1"), None)

    assert response["statusCode"] == 200
    payload = _body(response)
    assert payload["id"] == "list-1"
    tools = payload["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "list_devices",
        "get_device",
        "create_device",
        "update_device",
        "delete_device",
        "execute",
        "get_presence",
        "calibrate_room",
        "list_room_fingerprints",
        "delete_room_fingerprint",
        "record_room_checkin",
        "capture_room_reading",
        "label_room_checkin",
        "list_room_checkins",
        "delete_room_checkin",
        "set_presence",
        "list_scenes",
        "delete_scene",
        "list_policies",
        "get_policy",
        "author_policy",
        "delete_policy",
        "get_learnings",
        "learn_phrase",
        "delete_learning",
        "list_providers",
        "ingest",
        "health",
    ]
    read_only = {
        "list_devices",
        "get_device",
        "get_presence",
        "list_room_fingerprints",
        "list_room_checkins",
        "list_scenes",
        "list_policies",
        "get_policy",
        "get_learnings",
        "list_providers",
        "health",
    }
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for key in schema.get("required", []):
            assert key in schema["properties"]
        annotations = tool["annotations"]
        destructive = tool["name"].startswith("delete") or tool["name"] == "execute"
        assert annotations["destructiveHint"] is destructive
        assert annotations["readOnlyHint"] is (tool["name"] in read_only)
        assert "call" not in tool


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

def test_tools_call_health_happy_path():
    response = mcp_handler.handler(_rpc(
        "tools/call",
        {"name": "health", "arguments": {}},
        req_id=7,
    ), None)

    assert response["statusCode"] == 200
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 7
    result = payload["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    health = json.loads(result["content"][0]["text"])
    assert health["status"] == "healthy"
    assert health["registry_ok"] is False
    assert health["devices"] == 0
    assert "registry_error" in health


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token", [None, "submitted-token-value", ""])
def test_auth_rejection(token):
    """Missing and wrong bearer tokens are the same 401 JSON-RPC error."""
    if token == "":
        event = _event("POST", {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        }, token=None)
        event["headers"]["authorization"] = "Token not-a-bearer"
    else:
        event = _rpc(
            "tools/call",
            {"name": "health", "arguments": {}},
            req_id=4,
            token=token,
        )

    response = mcp_handler.handler(event, None)

    assert response["statusCode"] == 401
    assert response["headers"]["Content-Type"] == "application/json"
    assert "Bearer" in response["headers"]["WWW-Authenticate"]
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 4
    assert payload["error"] == {"code": -32001, "message": "Unauthorized"}
    assert TOKEN not in response["body"]
    assert "submitted-token-value" not in response["body"]
    assert "result" not in payload


def test_auth_rejection_does_not_call_the_tool():
    def _boom(*_args, **_kwargs):
        raise AssertionError("tool must not run when auth fails")

    tool = mcp_handler._TOOLS_BY_NAME["health"]
    original = tool["call"]
    tool["call"] = _boom
    try:
        response = mcp_handler.handler(_rpc(
            "tools/call",
            {"name": "health"},
            token="submitted-token-value",
        ), None)
    finally:
        tool["call"] = original

    assert response["statusCode"] == 401


def test_get_without_token_is_unauthorized():
    response = mcp_handler.handler(_event("GET", token=None), None)
    assert response["statusCode"] == 401
    assert _body(response)["error"]["code"] == -32001


def test_token_compare_uses_fixed_length_digests(monkeypatch):
    seen = {}

    def _fake_compare(left, right):
        seen["left"] = left
        seen["right"] = right
        return True

    monkeypatch.setattr(mcp_handler.hmac, "compare_digest", _fake_compare)
    assert mcp_handler.tokens_equal("short", "much-longer-value") is True
    assert isinstance(seen["left"], (bytes, bytearray))
    assert len(seen["left"]) == len(seen["right"]) == 32


def test_token_compare_rejects_mismatches():
    assert mcp_handler.tokens_equal("abc", "abc") is True
    assert mcp_handler.tokens_equal("abc", "abd") is False
    assert mcp_handler.tokens_equal("abc", "abcd") is False
    assert mcp_handler.tokens_equal("", "abc") is False


def test_load_expected_token_reads_named_secret(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", _REAL_LOAD)
    monkeypatch.setenv("MCP_AUTH_TOKEN_SECRET_NAME", "deviceweave/mcp-auth-token")
    mcp_handler._clear_token_cache()
    captured = {"n": 0}

    class _FakeSecrets:
        def get_secret_value(self, SecretId):
            captured["n"] += 1
            captured["id"] = SecretId
            return {"SecretString": "  from-secret \n"}

    monkeypatch.setattr(mcp_handler, "_secrets_client", lambda: _FakeSecrets())
    try:
        assert mcp_handler._load_expected_token() == "from-secret"
        assert mcp_handler._load_expected_token() == "from-secret"
    finally:
        mcp_handler._clear_token_cache()
    assert captured == {"n": 1, "id": "deviceweave/mcp-auth-token"}


def test_auth_unavailable_is_500_without_leaking_the_cause(monkeypatch):
    def _boom():
        raise mcp_handler.McpAuthUnavailable("secret name leaked-internal-detail")

    monkeypatch.setattr(mcp_handler, "_load_expected_token", _boom)
    response = mcp_handler.handler(_rpc("initialize", {}), None)
    assert response["statusCode"] == 500
    payload = _body(response)
    assert payload["error"]["code"] == -32603
    assert payload["error"]["message"] == "MCP authentication is unavailable"
    assert "leaked-internal-detail" not in response["body"]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def test_get_with_valid_token_is_405_and_not_sse():
    response = mcp_handler.handler(_event("GET"), None)
    assert response["statusCode"] == 405
    assert response["headers"]["Allow"] == "POST"
    assert "text/event-stream" not in response["headers"]["Content-Type"]
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["error"]["code"] == -32601


def test_initialized_notification_is_accepted():
    response = mcp_handler.handler(_event("POST", {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }), None)
    assert response["statusCode"] == 202
    assert response["body"] == ""


def test_jsonrpc_batch_is_rejected():
    response = mcp_handler.handler(_event("POST", [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]), None)
    assert response["statusCode"] == 400
    assert _body(response)["error"]["code"] == -32600


def test_unsupported_protocol_header_is_400():
    response = mcp_handler.handler(_rpc(
        "ping",
        headers={"MCP-Protocol-Version": "1999-01-01"},
    ), None)
    assert response["statusCode"] == 400
    assert "1999-01-01" in _body(response)["error"]["message"]


def test_template_adds_mcp_routes_and_does_not_embed_a_token():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert re.search(r"Path:\s*/mcp\s*\n\s*Method:\s*POST", text)
    assert re.search(r"Path:\s*/mcp\s*\n\s*Method:\s*GET", text)
    assert "McpAuthTokenSecretName" in text
    assert "MCP_AUTH_TOKEN_SECRET_NAME" in text
    assert "deviceweave/mcp-auth-token" in text
    assert "Bearer " not in text
    assert "SecretString:" not in text
