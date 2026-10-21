"""MCP 2026-07-28 on /mcp, served beside the handshake revisions.

The revision is stateless: no initialize, every request carries its version
and client capabilities in params._meta, and the version, method and tool
name are mirrored in headers that must agree with the body. Authorization is
the same code path whichever era a request belongs to.
"""

from __future__ import annotations

import json

import pytest

import mcp_handler

LEGACY = "legacy-static-token-value"
MODERN = "2026-07-28"


@pytest.fixture(autouse=True)
def _legacy(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: LEGACY)
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)


def _post(body, headers, token=LEGACY):
    hdrs = {"authorization": f"Bearer {token}"} if token else {}
    hdrs.update({k.lower(): v for k, v in headers.items() if v is not None})
    return mcp_handler.handler(
        {"requestContext": {"http": {"method": "POST", "path": "/mcp"}}, "headers": hdrs,
         "body": json.dumps(body)},
        None,
    )


def _modern(method, params=None, *, headers=None, meta=None, token=LEGACY):
    params = dict(params or {})
    params["_meta"] = meta if meta is not None else {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    sent = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        sent["Mcp-Name"] = params.get("name", "")
    sent.update(headers or {})
    return _post({"jsonrpc": "2.0", "id": 9, "method": method, "params": params}, sent, token)


def _body(response):
    return json.loads(response["body"])


def test_discover_lists_both_eras_and_is_private():
    response = _modern("server/discover")
    assert response["statusCode"] == 200
    result = _body(response)["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == MODERN and "2025-06-18" in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "deviceweave"
    assert result["cacheScope"] == "private"
    assert response["headers"]["MCP-Protocol-Version"] == MODERN


def test_tools_list_is_cacheable_per_token_and_deterministic():
    first = _body(_modern("tools/list"))["result"]
    second = _body(_modern("tools/list"))["result"]
    assert first["resultType"] == "complete" and first["cacheScope"] == "private" and first["ttlMs"] > 0
    assert [t["name"] for t in first["tools"]] == [t["name"] for t in second["tools"]]


def test_tools_call_runs_the_same_authorization(cw_issuer):
    read_only = cw_issuer.mint(audience="deviceweave", scopes=("read",))
    ok = _modern("tools/call", {"name": "health", "arguments": {}}, token=read_only)
    assert ok["statusCode"] == 200 and _body(ok)["result"]["resultType"] == "complete"
    refused = _modern("tools/call", {"name": "delete_device", "arguments": {"device_id": "d"}},
                      token=read_only)
    assert refused["statusCode"] == 403


@pytest.mark.parametrize("headers", [
    {"MCP-Protocol-Version": None},
    {"MCP-Protocol-Version": "2025-06-18"},
    {"Mcp-Method": None},
    {"Mcp-Method": "tools/list"},
    {"Mcp-Name": None},
    {"Mcp-Name": "delete_device"},
])
def test_a_header_that_disagrees_with_the_body_is_refused(headers, cw_issuer):
    token = cw_issuer.mint(audience="deviceweave", scopes=("read",))
    response = _modern("tools/call", {"name": "health", "arguments": {}}, headers=headers, token=token)
    assert response["statusCode"] == 400
    assert _body(response)["error"]["code"] == -32020


def test_unsupported_version_and_missing_capabilities():
    meta = {"io.modelcontextprotocol/protocolVersion": "1900-01-01",
            "io.modelcontextprotocol/clientCapabilities": {}}
    response = _modern("tools/list", meta=meta, headers={"MCP-Protocol-Version": "1900-01-01"})
    assert response["statusCode"] == 400 and _body(response)["error"]["code"] == -32022
    response = _modern("tools/list", meta={"io.modelcontextprotocol/protocolVersion": MODERN})
    assert response["statusCode"] == 400 and _body(response)["error"]["code"] == -32602


@pytest.mark.parametrize("method", ["ping", "logging/setLevel", "prompts/list"])
def test_removed_and_unserved_methods_are_404(method):
    response = _modern(method)
    assert response["statusCode"] == 404 and _body(response)["error"]["code"] == -32601


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26"])
def test_initialize_answers_each_handshake_version(version):
    response = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": version, "capabilities": {}}}, {})
    assert _body(response)["result"]["protocolVersion"] == version


def test_initialize_never_answers_the_stateless_version():
    response = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": MODERN}}, {})
    assert _body(response)["result"]["protocolVersion"] == "2025-11-25"


def test_a_modern_header_without_meta_is_not_served_as_legacy():
    response = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                     {"MCP-Protocol-Version": MODERN})
    assert response["statusCode"] == 400 and _body(response)["error"]["code"] == -32602


def test_unauthenticated_modern_request_is_401():
    assert _modern("server/discover", token="wrong")["statusCode"] == 401


def test_invalid_arguments_are_a_tool_error_the_model_can_read(cw_issuer):
    token = cw_issuer.mint(audience="deviceweave", scopes=("read",))
    response = _modern("tools/call", {"name": "get_device", "arguments": {}}, token=token)
    assert response["statusCode"] == 200
    result = _body(response)["result"]
    assert result["isError"] is True and "device_id" in result["content"][0]["text"]


def test_vendored_protocol_module_matches_the_other_weaves():
    import hashlib
    from pathlib import Path

    data = (Path(__file__).resolve().parents[1] / "src" / "mcp_era.py").read_bytes()
    assert hashlib.sha256(data).hexdigest() == MCP_ERA_SHA256


# The same bytes as ContextWeave's src/mcp_server/mcp_era.py and ModelWeave's.
MCP_ERA_SHA256 = "ff733427e2de38963a26f952f2291254b7c834357548e60a88ef383950a87e3e"
