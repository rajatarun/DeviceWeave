"""MODIFY params are clamped to the device's capability ranges before actuation.

Brightness defaults to 0–100. Duration is clamped only when the device
declares a range. Out-of-range values must not reach the adapter.
"""

import asyncio
import json

import pytest

import app
import providers
from execution_planner import ExecutionStep, execute_steps
from policy_authoring.validator import validate_policy
from policy_engine import middleware, policy_loader
from policy_engine.param_clamp import clamp_numeric_params

CTX = {
    "temperature": 70.0, "humidity": 40.0, "time_hour": 12, "cloud_cover_pct": 0,
    "is_home": True, "is_overcast": False, "room": "unknown",
}


def _compiled(scope, kind="modify", params=None, rule_id="dim"):
    return validate_policy({
        "rule_id": rule_id,
        "scope": {"device_type": scope},
        "conditions": [{"field": "time_hour", "operator": ">=", "value": 0}],
        "action": {"type": kind, "reason": f"{kind} {scope}", "params": params or {}},
        "confidence": 0.95,
    })


def _store_item(policy):
    return {
        "pk": policy["rule_id"], "sk": "v1",
        "rule_id": policy["rule_id"], "version": 1,
        "device_type": policy["scope"]["device_type"],
        "scope": policy["scope"], "conditions": policy["conditions"],
        "action": policy["action"], "confidence": str(policy["confidence"]),
        "status": "active",
    }


def _device(dtype="SmartBulb", **extra):
    device = {
        "id": f"dev-{dtype}", "name": f"test {dtype}", "ip": "", "device_type": dtype,
        "capabilities": ["turn_on", "turn_off", "toggle", "get_status", "set_brightness"],
        "model": "M0",
    }
    device.update(extra)
    return device


@pytest.fixture
def policy_table(monkeypatch, fake_dynamodb):
    monkeypatch.setattr(policy_loader, "_TABLE_NAME", "policies")
    monkeypatch.setattr(policy_loader, "get_dynamodb_resource", lambda: fake_dynamodb)
    policy_loader.invalidate()
    table = fake_dynamodb.Table("policies")

    def put(*policies):
        for policy in policies:
            table.put_item(Item=_store_item(policy))
        policy_loader.invalidate()

    yield put
    policy_loader.invalidate()
    monkeypatch.setattr(policy_loader, "_cache", {})


@pytest.fixture
def adapter_spy(monkeypatch):
    seen = []

    async def spy(device, action, params):
        seen.append({"device_type": device["device_type"], "action": action, "params": dict(params)})
        return {"changed": True}

    for adapter in set(providers._REGISTRY.values()):
        monkeypatch.setattr(adapter, "execute", spy)
    return seen


def test_brightness_defaults_to_percent_and_duration_needs_a_declaration():
    device = _device()
    clamped = clamp_numeric_params(
        {"brightness": 500, "duration": 9999, "mode": "movie"}, device,
    )
    assert clamped == {"brightness": 100, "duration": 9999, "mode": "movie"}

    low = clamp_numeric_params({"brightness": -5}, device)
    assert low == {"brightness": 0}

    inside = clamp_numeric_params({"brightness": 30}, device)
    assert inside == {"brightness": 30}


def test_declared_ranges_override_the_brightness_default_and_bound_duration():
    device = _device(capability_ranges={
        "brightness": {"min": 10, "max": 40},
        "duration": [0, 120],
    })
    assert clamp_numeric_params({"brightness": 80, "duration": 500}, device) == {
        "brightness": 40, "duration": 120,
    }
    assert clamp_numeric_params({"brightness": 5, "duration": 15}, device) == {
        "brightness": 10, "duration": 15,
    }
    # A one-sided brightness declaration keeps the other side of the default.
    capped = _device(capability_ranges={"brightness": {"max": 40}})
    assert clamp_numeric_params({"brightness": -5}, capped) == {"brightness": 0}
    assert clamp_numeric_params({"brightness": 80}, capped) == {"brightness": 40}


def test_unreadable_declaration_keeps_the_brightness_default():
    device = _device(capability_ranges={"brightness": "percent", "duration": {"min": 9, "max": 1}})
    assert clamp_numeric_params({"brightness": 150, "duration": 3}, device) == {
        "brightness": 100, "duration": 3,
    }


def test_modify_out_of_range_does_not_reach_the_adapter(policy_table, adapter_spy):
    device = _device(capability_ranges={"duration": {"min": 0, "max": 120}})
    policy_table(_compiled("light", params={"brightness": 500, "duration": 9999}))
    response = app._execute_device(
        device, "set_brightness", {"brightness": 10},
        "set brightness", 0.9, tier="cosine", policy_ctx=dict(CTX),
    )
    assert response["statusCode"] == 200
    assert adapter_spy == [{
        "device_type": "SmartBulb",
        "action": "set_brightness",
        "params": {"brightness": 100, "duration": 120},
    }]
    body = json.loads(response["body"])
    assert body["policy"]["rule_id"] == "dim"


def test_modify_without_a_duration_range_leaves_duration_and_caps_brightness(
    policy_table, adapter_spy,
):
    policy_table(_compiled("light", params={"brightness": -8, "duration": 50}))
    response = app._execute_device(
        _device(), "set_brightness", {"brightness": 90},
        "set brightness", 0.9, tier="cosine", policy_ctx=dict(CTX),
    )
    assert response["statusCode"] == 200
    assert adapter_spy[0]["params"] == {"brightness": 0, "duration": 50}


def test_filter_steps_hands_the_adapter_clamped_modify_params(policy_table, adapter_spy):
    device = _device(capability_ranges={"brightness": {"max": 40}, "duration": {"max": 15}})
    policy_table(_compiled("light", params={"brightness": 250, "duration": 90, "mode": "movie"}))
    steps = [ExecutionStep(device=device, action="set_brightness", params={"brightness": 80})]
    allowed, blocked = middleware.filter_steps(steps, context=dict(CTX))
    assert blocked == []
    assert allowed[0].params == {"brightness": 40, "duration": 15, "mode": "movie"}
    asyncio.run(execute_steps(allowed))
    assert adapter_spy[0]["params"] == {"brightness": 40, "duration": 15, "mode": "movie"}


def test_direct_step_is_clamped_at_the_adapter_boundary(adapter_spy):
    """The actuation gate caps known params even when no policy ran."""
    device = _device(capability_ranges={
        "brightness": {"min": 5, "max": 40},
        "duration": [0, 30],
    })
    step = ExecutionStep(
        device=device, action="set_brightness",
        params={"brightness": 500, "duration": 90, "mode": "movie"},
    )
    [result] = asyncio.run(execute_steps([step]))
    assert result.success is True
    assert adapter_spy[0]["params"] == {"brightness": 40, "duration": 30, "mode": "movie"}
