"""Conversational-agent tools must not actuate when the policy check raises.

The four call sites are:

* bedrock_agent._tool_execute_device_command
* bedrock_agent._tool_execute_scene
* gemini_agent.execute_device_command
* gemini_agent.execute_scene
"""

import asyncio

import pytest

import providers

DEVICE = {
    "id": "lamp",
    "name": "Lamp",
    "device_type": "SmartBulb",
    "capabilities": ["turn_on", "turn_off", "set_brightness"],
}
SCENE = {
    "id": "movie",
    "name": "Movie",
    "actions": [{"device_id": "lamp", "action": "turn_on", "params": {}}],
}

_DEVICE_CALLS = (
    ("bedrock_agent", "_tool_execute_device_command"),
    ("gemini_agent", "execute_device_command"),
)
_SCENE_CALLS = (
    ("bedrock_agent", "_tool_execute_scene"),
    ("gemini_agent", "execute_scene"),
)


@pytest.fixture
def adapter_calls(monkeypatch):
    calls = []

    async def spy(device, action, params):
        calls.append((device.get("id"), action, dict(params)))
        return {"changed": True}

    for adapter in set(providers._REGISTRY.values()):
        monkeypatch.setattr(adapter, "execute", spy)
    monkeypatch.setattr("device_resolver._get_active_catalog", lambda: [DEVICE])
    monkeypatch.setattr("scene_catalog.get_active_scenes", lambda: [SCENE])
    return calls


def _raise(*_args, **_kwargs):
    raise RuntimeError("policy store unavailable")


def _call(module_name, fn_name):
    import importlib
    fn = getattr(importlib.import_module(module_name), fn_name)
    if fn_name in ("_tool_execute_device_command", "execute_device_command"):
        return asyncio.run(fn("lamp", "turn_on", "turn on the lamp", {}))
    return asyncio.run(fn("movie", "run movie mode"))


@pytest.mark.parametrize("module_name,fn_name", _DEVICE_CALLS)
@pytest.mark.parametrize("target", ("enforce", "context"))
def test_device_command_does_not_actuate_when_the_policy_check_raises(
    monkeypatch, adapter_calls, module_name, fn_name, target,
):
    if target == "enforce":
        monkeypatch.setattr("policy_engine.middleware.enforce", _raise)
    else:
        monkeypatch.setattr("policy_engine.context_provider.get_context", _raise)
    result = _call(module_name, fn_name)
    assert adapter_calls == []
    assert result.get("success") is not True
    assert result.get("policy_error") is True
    assert "not executed" in result["error"]


@pytest.mark.parametrize("module_name,fn_name", _SCENE_CALLS)
@pytest.mark.parametrize("target", ("filter", "context"))
def test_scene_does_not_actuate_when_the_policy_check_raises(
    monkeypatch, adapter_calls, module_name, fn_name, target,
):
    if target == "filter":
        monkeypatch.setattr("policy_engine.middleware.filter_steps", _raise)
    else:
        monkeypatch.setattr("policy_engine.context_provider.get_context", _raise)
    result = _call(module_name, fn_name)
    assert adapter_calls == []
    assert result.get("success") is not True
    assert result.get("policy_error") is True
    assert "not executed" in result["error"]


@pytest.mark.parametrize("module_name,fn_name", _DEVICE_CALLS + _SCENE_CALLS)
def test_policy_check_success_still_reaches_the_adapter(adapter_calls, module_name, fn_name):
    """A passing check (no policies configured) still actuates. Fail-closed is not fail-always."""
    result = _call(module_name, fn_name)
    assert result.get("success") is True
    assert adapter_calls == [("lamp", "turn_on", {})]
