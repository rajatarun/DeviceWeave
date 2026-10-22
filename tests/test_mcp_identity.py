"""Weave Identity on /mcp: DPoP-bound tokens, delegated tokens, discovery metadata.

A device that unlocks a door has no undo, so this file is about the two ways a
token can reach the handler in the wrong hands: copied out of a transcript
(DPoP), and carried by a deputy acting for someone else (RFC 8693 ``act``).
"""

from __future__ import annotations

import json

import pytest
from cipherweave_verify import DPoPKey

import mcp_handler

URL = "https://dw.test/prod/mcp"


@pytest.fixture(autouse=True)
def _legacy(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: "legacy-static-token-value")
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)


@pytest.fixture
def deleted(monkeypatch):
    calls = []

    def _route_delete_device(device_id):
        calls.append(device_id)
        return {"statusCode": 200, "body": json.dumps({"deleted": device_id})}

    import app

    monkeypatch.setattr(app, "_route_delete_device", _route_delete_device)
    return calls


def _event(authorization, *, tool="health", arguments=None, dpop=None, path="/prod/mcp"):
    headers = {"authorization": authorization}
    if dpop is not None:
        headers["dpop"] = dpop
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": "POST", "path": path},
            "domainName": "dw.test",
            "stage": "prod",
        },
        "headers": headers,
        "body": json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        ),
    }


def test_dpop_bound_token_works_with_its_proof(fresh_token):
    key = DPoPKey()
    token = fresh_token(("read",), cnf={"jkt": key.jkt})
    response = mcp_handler.handler(
        _event(f"DPoP {token}", dpop=key.proof("POST", URL, access_token=token)), None
    )
    assert response["statusCode"] == 200


def test_copied_dpop_token_is_useless_without_the_key(fresh_token):
    key = DPoPKey()
    token = fresh_token(("read",), cnf={"jkt": key.jkt})
    for event in (
        _event(f"Bearer {token}"),
        _event(f"DPoP {token}"),
        _event(f"DPoP {token}", dpop=DPoPKey().proof("POST", URL, access_token=token)),
    ):
        response = mcp_handler.handler(event, None)
        assert response["statusCode"] == 401
        assert response["headers"]["WWW-Authenticate"].startswith("DPoP ")


def test_delegated_token_cannot_delete_a_device(fresh_token, deleted):
    token = fresh_token(("destructive",), act={"sub": "contextweave-svc"})
    response = mcp_handler.handler(
        _event(f"Bearer {token}", tool="delete_device", arguments={"device_id": "d1"}), None
    )
    assert response["statusCode"] == 403
    assert "delegation_not_allowed" in response["body"]
    assert deleted == []


def test_delegated_token_can_still_read(fresh_token):
    token = fresh_token(("read",), act={"sub": "contextweave-svc"})
    assert mcp_handler.handler(_event(f"Bearer {token}"), None)["statusCode"] == 200


def test_resource_metadata_and_the_401_pointer(cw_issuer):
    response = mcp_handler.handler(
        {
            "rawPath": "/prod/.well-known/oauth-protected-resource",
            "requestContext": {"http": {"method": "GET"}, "domainName": "dw.test", "stage": "prod"},
            "headers": {},
        },
        None,
    )
    assert response["statusCode"] == 200
    doc = json.loads(response["body"])
    assert doc["resource"] == URL
    assert doc["authorization_servers"] == ["https://cipherweave.test/prod"]
    assert doc["audience"] == "deviceweave"

    unauthorized = mcp_handler.handler(_event("Bearer nope"), None)
    assert unauthorized["statusCode"] == 401
    assert (
        'resource_metadata="https://dw.test/prod/.well-known/oauth-protected-resource"'
        in unauthorized["headers"]["WWW-Authenticate"]
    )
