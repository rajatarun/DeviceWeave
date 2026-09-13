"""
Unit tests for app.py route dispatch: GET /health, POST /execute (with the
resolver and executor stubbed so no provider I/O happens), and a 404 for an
unknown route.

Resolver/executor stubs are applied on the `app` module's own namespace
(e.g. `app.resolve_device`, not `device_resolver.resolve_device`) because
app.py does `from device_resolver import resolve_device` — that binds a
name in app's own module dict at import time, so patching the *source*
module after the fact would not affect what app.py actually calls.
"""

import json

import pytest

import app
from execution_planner import StepResult


def _event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "body": json.dumps(body) if body is not None else None,
    }


def _body(response: dict) -> dict:
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_reports_registry_not_configured_by_default():
    # No DEVICE_REGISTRY_TABLE set (autouse fixture clears it) -> _get_active_catalog()
    # raises DeviceRegistryError, which _route_health() must catch, not propagate.
    response = app.handler(_event("GET", "/health"), None)

    assert response["statusCode"] == 200
    payload = _body(response)
    assert payload["status"] == "healthy"
    assert payload["registry_ok"] is False
    assert payload["devices"] == 0
    assert "registry_error" in payload
    assert payload["graph_enabled"] is False  # stubbed by conftest


def test_health_reports_device_count_when_registry_configured(monkeypatch):
    monkeypatch.setattr(app, "_get_active_catalog", lambda: [{"id": "d1"}, {"id": "d2"}])

    response = app.handler(_event("GET", "/health"), None)

    payload = _body(response)
    assert response["statusCode"] == 200
    assert payload["registry_ok"] is True
    assert payload["devices"] == 2
    assert "registry_error" not in payload


# ---------------------------------------------------------------------------
# POST /execute — one-shot device command, resolver + executor stubbed
# ---------------------------------------------------------------------------

DEVICE = {
    "id": "office_light",
    "name": "Office Light",
    "device_type": "light",
    "capabilities": ["turn_on", "turn_off", "set_brightness"],
}


def _stub_execute_pipeline(monkeypatch, final_score=0.9, cosine_score=0.9, behavior_score=0.5):
    # 1. No scene matches -> falls through to single-device resolution.
    monkeypatch.setattr(app, "resolve_scene", lambda command: (None, 0.0))
    # 2. Tier 1 device resolver returns our fixed device deterministically.
    monkeypatch.setattr(app, "resolve_device", lambda query: (DEVICE, cosine_score))
    # 3. decision_engine.compute_score is module-qualified in app.py
    #    (`decision_engine.compute_score(...)`) -> patch on the module object.
    monkeypatch.setattr(
        app.decision_engine, "compute_score",
        lambda cosine, device, action, ctx: (final_score, cosine, behavior_score),
    )

    captured_steps = []

    async def fake_execute_steps(steps):
        captured_steps.append(steps)
        step = steps[0]
        return [StepResult(
            device_id=step.device["id"],
            device_name=step.device["name"],
            action=step.action,
            success=True,
            result={"state": "on", "changed": True},
        )]

    monkeypatch.setattr(app, "execute_steps", fake_execute_steps)
    return captured_steps


def test_execute_device_command_happy_path(monkeypatch):
    _stub_execute_pipeline(monkeypatch)

    response = app.handler(_event("POST", "/execute", {"command": "turn on the office light"}), None)

    assert response["statusCode"] == 200
    payload = _body(response)
    assert payload["type"] == "device"
    assert payload["device_id"] == "office_light"
    assert payload["action"] == "turn_on"
    assert payload["confidence"] == 0.9
    assert payload["resolution_tier"] == "cosine"
    assert payload["result"] == {"state": "on", "changed": True}
    assert payload["scores"] == {"cosine": 0.9, "behavior": 0.5, "final": 0.9}


def test_execute_rejects_unsupported_capability(monkeypatch):
    _stub_execute_pipeline(monkeypatch)
    # "toggle" is not in DEVICE's capabilities.
    monkeypatch.setattr(app, "resolve_device", lambda query: (DEVICE, 0.9))

    response = app.handler(_event("POST", "/execute", {"command": "toggle the office light"}), None)

    assert response["statusCode"] == 422
    payload = _body(response)
    assert "does not support" in payload["error"]


def test_execute_missing_body_is_400():
    # No body at all -> _parse_body returns {} (not None), so this actually
    # exercises the "no command extracted" 400, not the JSON-parse-error 400
    # (see test_execute_invalid_json_body_is_400 for that one).
    response = app.handler(_event("POST", "/execute", None), None)
    assert response["statusCode"] == 400


def test_execute_invalid_json_body_is_400():
    event = {
        "requestContext": {"http": {"method": "POST", "path": "/execute"}},
        "body": "{not valid json",
    }
    response = app.handler(event, None)
    assert response["statusCode"] == 400


def test_execute_below_confidence_threshold_is_422(monkeypatch):
    # Tier 1 misses; stub the LLM resolver (Tier 2) to also miss so the
    # request cleanly falls through to the "both tiers failed" 422 branch
    # without making a real Bedrock call.
    monkeypatch.setattr(app, "resolve_scene", lambda command: (None, 0.0))
    monkeypatch.setattr(app, "resolve_device", lambda query: (DEVICE, 0.1))
    monkeypatch.setattr(
        app.decision_engine, "compute_score",
        lambda cosine, device, action, ctx: (0.1, cosine, 0.5),
    )
    monkeypatch.setattr(app, "_get_active_catalog", lambda: [DEVICE])
    monkeypatch.setattr(app, "llm_resolve", lambda command, action, catalog: None)

    response = app.handler(_event("POST", "/execute", {"command": "turn on the office light"}), None)

    assert response["statusCode"] == 422
    payload = _body(response)
    assert payload["best_match_id"] == "office_light"
    assert payload["final_score"] == 0.1


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

def test_unknown_route_is_404():
    response = app.handler(_event("GET", "/nope"), None)
    assert response["statusCode"] == 404
    payload = _body(response)
    assert "No route for GET /nope" in payload["error"]


def test_unsupported_method_on_known_path_is_404():
    # PATCH isn't wired up for /execute at all.
    response = app.handler(_event("PATCH", "/execute"), None)
    assert response["statusCode"] == 404
