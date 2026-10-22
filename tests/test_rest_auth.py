"""The REST API takes the /mcp credential and a scope per route (rest_auth).

Before this, every route below answered anyone holding the API URL: switch a
device, learn who is home, delete a device, rewrite the automation policies.
"""

from __future__ import annotations

import json

import pytest

import app
import ingestion_handler
import mcp_handler
import rest_auth
from policy_authoring import handler as policy_handler

LEGACY = "legacy-static-token-value"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mcp_handler, "_load_expected_token", lambda: LEGACY)
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)
    monkeypatch.delenv("REST_AUTH_MODE", raising=False)
    rest_auth.reset()
    yield
    rest_auth.reset()


def _event(method, path, token=None, body=None, scheme="Bearer"):
    headers = {"authorization": f"{scheme} {token}"} if token else {}
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/execute"),
        ("GET", "/presence"),
        ("GET", "/devices"),
        ("DELETE", "/devices/d1"),
        ("PUT", "/devices/d1"),
        ("POST", "/learn"),
        ("DELETE", "/learnings"),
    ],
)
def test_app_routes_refuse_an_anonymous_caller(method, path):
    event = _event(method, path)
    if "/devices/" in path:
        event["pathParameters"] = {"device_id": "d1"}
    response = app.handler(event, None)
    assert response["statusCode"] == 401
    assert response["headers"]["WWW-Authenticate"].startswith("Bearer ")


def test_health_stays_public():
    assert app.handler(_event("GET", "/health"), None)["statusCode"] == 200


def test_policy_and_ingest_routes_refuse_an_anonymous_caller():
    assert policy_handler.handler(_event("POST", "/policies/author"), None)["statusCode"] == 401
    assert policy_handler.handler(_event("DELETE", "/policies/r1"), None)["statusCode"] == 401
    assert ingestion_handler.handler(_event("POST", "/ingest"), None)["statusCode"] == 401


def test_scheduled_ingest_is_not_an_http_request(monkeypatch):
    seen = []
    monkeypatch.setattr(ingestion_handler, "_extract_payload", lambda e: seen.append(e) or {"mode": "nope"})
    ingestion_handler.handler({"source": "aws.events"}, None)
    assert seen  # reached the ingest body without an Authorization header


def test_route_scopes(cw_issuer):
    read = cw_issuer.mint(audience="deviceweave", scopes=("read",))
    execute = cw_issuer.mint(audience="deviceweave", scopes=("read", "execute"))
    assert app.handler(_event("GET", "/presence", read), None)["statusCode"] != 401
    refused = app.handler(_event("POST", "/execute", read, {"command": "x"}), None)
    assert refused["statusCode"] == 403
    assert 'scope="execute"' in refused["headers"]["WWW-Authenticate"]
    delete = _event("DELETE", "/devices/d1", execute)
    delete["pathParameters"] = {"device_id": "d1"}
    assert app.handler(delete, None)["statusCode"] == 403


def test_delegated_token_cannot_use_a_destructive_route(cw_issuer):
    token = cw_issuer.mint(
        audience="deviceweave", scopes=("destructive",), act={"sub": "contextweave-svc"}
    )
    event = _event("DELETE", "/devices/d1", token)
    event["pathParameters"] = {"device_id": "d1"}
    response = app.handler(event, None)
    assert response["statusCode"] == 403
    assert "delegation_not_allowed" in response["body"]


def test_token_for_another_weave_is_refused(cw_issuer):
    token = cw_issuer.mint(audience="toolweave", scopes=("read",))
    assert app.handler(_event("GET", "/devices", token), None)["statusCode"] == 401


def test_audit_mode_lets_reads_through_but_never_destructive(monkeypatch):
    monkeypatch.setenv("REST_AUTH_MODE", "audit")
    assert app.handler(_event("GET", "/providers"), None)["statusCode"] == 200
    event = _event("DELETE", "/devices/d1")
    event["pathParameters"] = {"device_id": "d1"}
    assert app.handler(event, None)["statusCode"] == 401
    assert ingestion_handler.handler(_event("POST", "/ingest"), None)["statusCode"] == 401


def test_unlisted_write_route_needs_execute():
    assert rest_auth.route_policy("POST", "/something/new").scope == "execute"
    assert rest_auth.route_policy("PATCH", "/devices").scope == "execute"
    assert rest_auth.route_policy("GET", "/health") is None


def test_execute_binds_the_principal_for_the_door_gate(cw_issuer, monkeypatch):
    import access_control

    seen = {}

    def fake_handle(event, context):
        seen["auth"] = access_control.current()
        return {"statusCode": 200, "body": "{}"}

    monkeypatch.setattr(app, "_handle", fake_handle)
    token = cw_issuer.mint(audience="deviceweave", scopes=("execute",))
    app.handler(_event("POST", "/execute", token, {"command": "unlock"}), None)
    assert seen["auth"] is not None and seen["auth"].principal.subject == "test-client"


def test_template_and_workflow_wire_rest_auth():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "template.yaml").read_text()
    assert "REST_AUTH_MODE: !Ref RestAuthMode" in template
    assert "RestAuthMode=" in (root / ".github/workflows/deploy.yml").read_text()
