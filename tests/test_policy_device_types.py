"""
Regression tests: policy scope classes must resolve to adapter device types.

Before the fix the validator only accepted scope.device_type in
{fan, light, ac, plug, heater} while devices carry adapter routing keys
(SmartBulb, GoveePlug, WyzeLock, ...).  The evaluator compared the two by
string equality, so a compiled BLOCK policy never matched a real device.

These tests drive the real enforcement path: validate_policy() ->
policy item in the (fake) policy table -> policy_loader -> middleware.enforce
/ filter_steps -> app._execute_device -> execution_planner -> the adapter
registered in providers._REGISTRY.  The adapter's execute() is replaced by a
spy so no network I/O happens; a BLOCK must leave the spy uncalled.
"""

import asyncio
import json

import pytest

import app
import providers
from execution_planner import ExecutionStep, execute_steps
from policy_authoring.validator import ValidationError, validate_policy, _ALLOWED_DEVICE_TYPES
from policy_engine import middleware, policy_loader
from policy_engine.device_types import (
    ADAPTER_TYPE_TO_SCOPE,
    POLICY_SCOPE_TYPES,
    canonical_device_type,
)
from policy_engine.evaluator import compute_verdict

ALL_ADAPTER_TYPES = sorted(providers._REGISTRY)

# Context in which the "always" condition below holds.
CTX = {"temperature": 70.0, "humidity": 40.0, "time_hour": 12, "cloud_cover_pct": 0,
       "is_home": True, "is_overcast": False, "room": "unknown"}


def _compiled(scope, kind="block", params=None, rule_id="r1"):
    """A policy as the compiler would emit it, run through the real validator."""
    return validate_policy({
        "rule_id": rule_id,
        "scope": {"device_type": scope},
        "conditions": [{"field": "time_hour", "operator": ">=", "value": 0}],
        "action": {"type": kind, "reason": f"{kind} {scope}", "params": params or {}},
        "confidence": 0.95,
    })


def _store_item(policy):
    """Same item shape policy_store.save_policy() writes."""
    return {
        "pk": policy["rule_id"], "sk": "v1",
        "rule_id": policy["rule_id"], "version": 1,
        "device_type": policy["scope"]["device_type"],
        "scope": policy["scope"], "conditions": policy["conditions"],
        "action": policy["action"], "confidence": str(policy["confidence"]),
        "status": "active",
    }


@pytest.fixture
def policy_table(monkeypatch, fake_dynamodb):
    """Configure the real policy loader against an in-memory table."""
    monkeypatch.setattr(policy_loader, "_TABLE_NAME", "policies")
    monkeypatch.setattr(policy_loader, "get_dynamodb_resource", lambda: fake_dynamodb)
    policy_loader.invalidate()
    table = fake_dynamodb.Table("policies")

    def put(*policies):
        for p in policies:
            table.put_item(Item=_store_item(p))
        policy_loader.invalidate()

    yield put
    policy_loader.invalidate()
    monkeypatch.setattr(policy_loader, "_cache", {})


@pytest.fixture
def adapter_spy(monkeypatch):
    """Replace every registered adapter's execute() with a recording spy."""
    calls = []
    for adapter in set(providers._REGISTRY.values()):
        async def spy(device, action, params, _a=adapter):
            calls.append((type(_a).__name__, device["device_type"], action))
            return {"changed": True}
        monkeypatch.setattr(adapter, "execute", spy)
    return calls


def _device(dtype):
    return {"id": f"dev-{dtype}", "name": f"test {dtype}", "ip": "", "device_type": dtype,
            "capabilities": ["turn_on", "turn_off", "toggle", "get_status", "set_brightness"],
            "model": "M0"}


# ---------------------------------------------------------------------------
# Type resolution
# ---------------------------------------------------------------------------

def test_every_adapter_type_has_a_scope_class():
    assert set(ADAPTER_TYPE_TO_SCOPE) == set(providers._REGISTRY)
    assert set(ADAPTER_TYPE_TO_SCOPE.values()) <= POLICY_SCOPE_TYPES


def test_validator_and_evaluator_share_one_vocabulary():
    assert _ALLOWED_DEVICE_TYPES is POLICY_SCOPE_TYPES


@pytest.mark.parametrize("dtype", ALL_ADAPTER_TYPES)
def test_adapter_type_resolves_to_its_class(dtype):
    cls = canonical_device_type(dtype)
    assert cls == ADAPTER_TYPE_TO_SCOPE[dtype]
    assert canonical_device_type(cls) == cls  # idempotent
    assert canonical_device_type(dtype.lower()) == cls
    assert canonical_device_type(dtype.upper()) == cls


@pytest.mark.parametrize("dtype,cls", [
    ("SmartBulb", "light"), ("SmartPlug", "plug"), ("GoveeBulb", "light"),
    ("GoveePlug", "plug"), ("SwitchBotFan", "fan"), ("RingLight", "light"),
    ("MyQLamp", "light"), ("WyzeLock", "lock"), ("RingDoorbell", "doorbell"),
    ("WyzeThermostat", "thermostat"), ("MyQGarageDoor", "garage_door"),
])
def test_known_mappings(dtype, cls):
    assert canonical_device_type(dtype) == cls


def test_scope_classes_resolve_to_themselves_and_unknowns_pass_through():
    for cls in POLICY_SCOPE_TYPES:
        assert canonical_device_type(cls) == cls
        assert canonical_device_type(cls.upper()) == cls
    assert canonical_device_type("Dishwasher") == "Dishwasher"
    assert canonical_device_type(None) == ""
    assert canonical_device_type("") == ""


def test_validator_canonicalises_adapter_type_and_rejects_unknown():
    assert _compiled("SmartBulb")["scope"] == {"device_type": "light"}
    assert _compiled("Light")["scope"] == {"device_type": "light"}
    assert _compiled("lock")["scope"] == {"device_type": "lock"}
    with pytest.raises(ValidationError):
        _compiled("dishwasher")


def test_compiler_prompt_lists_every_scope_class():
    from policy_authoring.llm_compiler import _SYSTEM_PROMPT
    for cls in POLICY_SCOPE_TYPES:
        assert cls in _SYSTEM_PROMPT


def test_evaluator_matches_class_policy_against_adapter_type():
    pol = _compiled("light")
    assert compute_verdict([pol], "SmartBulb", "turn_on", CTX).is_blocked
    assert compute_verdict([pol], "GoveeBulb", "turn_on", CTX).is_blocked
    assert not compute_verdict([pol], "SmartPlug", "turn_on", CTX).is_blocked
    # Pre-existing behaviour for scope-class device types is unchanged.
    assert compute_verdict([pol], "light", "turn_on", CTX).is_blocked
    assert not compute_verdict([pol], "fan", "turn_on", CTX).is_blocked


# ---------------------------------------------------------------------------
# Real enforcement path, one case per adapter device type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dtype", ALL_ADAPTER_TYPES)
def test_block_policy_stops_adapter_call_single_device(dtype, policy_table, adapter_spy):
    policy_table(_compiled(ADAPTER_TYPE_TO_SCOPE[dtype], rule_id=f"block-{dtype}"))
    r = app._execute_device(_device(dtype), "turn_on", {}, "turn on", 0.9,
                            tier="cosine", policy_ctx=dict(CTX))
    assert r["statusCode"] == 403
    assert json.loads(r["body"])["rule_id"] == f"block-{dtype}"
    assert adapter_spy == []


@pytest.mark.parametrize("dtype", ALL_ADAPTER_TYPES)
def test_block_policy_stops_adapter_call_multi_step(dtype, policy_table, adapter_spy):
    policy_table(_compiled(ADAPTER_TYPE_TO_SCOPE[dtype], rule_id=f"block-{dtype}"))
    steps = [ExecutionStep(device=_device(dtype), action="turn_on", params={})]
    allowed, blocked = middleware.filter_steps(steps, context=dict(CTX))
    assert allowed == []
    assert [b["rule_id"] for b in blocked] == [f"block-{dtype}"]
    asyncio.run(execute_steps(allowed))
    assert adapter_spy == []


@pytest.mark.parametrize("dtype", ALL_ADAPTER_TYPES)
def test_positive_control_adapter_is_called_without_matching_policy(dtype, policy_table, adapter_spy):
    # A BLOCK on a different class must not affect this device; proves the spy
    # observes real adapter calls on this path.
    other = "camera" if ADAPTER_TYPE_TO_SCOPE[dtype] != "camera" else "fan"
    policy_table(_compiled(other))
    r = app._execute_device(_device(dtype), "turn_on", {}, "turn on", 0.9,
                            tier="cosine", policy_ctx=dict(CTX))
    assert r["statusCode"] == 200
    assert len(adapter_spy) == 1
    assert adapter_spy[0][1:] == (dtype, "turn_on")


def test_modify_policy_scoped_by_class_reaches_adapter_with_modified_params(policy_table, monkeypatch):
    seen = []
    adapter = providers._REGISTRY["SmartBulb"]

    async def spy(device, action, params):
        seen.append(dict(params))
        return {"changed": True}
    monkeypatch.setattr(adapter, "execute", spy)
    policy_table(_compiled("light", kind="modify", params={"brightness": 30}))
    r = app._execute_device(_device("SmartBulb"), "set_brightness", {"brightness": 90},
                            "set brightness", 0.9, tier="cosine", policy_ctx=dict(CTX))
    assert r["statusCode"] == 200
    assert seen == [{"brightness": 30}]


def test_loader_groups_legacy_adapter_type_items_by_class(policy_table):
    pol = _compiled("light")
    item_policy = dict(pol, scope={"device_type": "SmartBulb"})  # legacy/hand-written item
    policy_table(item_policy)
    assert [p["rule_id"] for p in policy_loader.get_policies_for_device("GoveeBulb")] == [pol["rule_id"]]
    assert [p["rule_id"] for p in policy_loader.get_policies_for_device("light")] == [pol["rule_id"]]
