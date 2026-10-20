"""DeviceWeave's Bedrock calls take their model from ModelWeave's role pin."""
from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

import modelweave_roles

ROOT = Path(__file__).resolve().parents[1]
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
PRO = "us.amazon.nova-pro-v1:0"


def test_vendored_module_is_byte_for_byte_the_pinned_modelweave_commit():
    manifest = json.loads((ROOT / "MODELWEAVE_VENDORED.json").read_text())
    data = (ROOT / manifest["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["sha256"]


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class Ssm:
    def __init__(self, pins):
        self.pins = pins

    def get_parameter(self, Name):
        if Name not in self.pins:
            raise ClientError("ParameterNotFound", Name)
        return {"Parameter": {"Value": json.dumps({"model_id": self.pins[Name]})}}


class Bedrock:
    def __init__(self, failing=None):
        self.failing, self.calls = failing or {}, []

    def converse(self, modelId, **kwargs):
        self.calls.append((modelId, kwargs))
        if modelId in self.failing:
            raise self.failing[modelId]
        return {"output": {"message": {"content": [{"text": " ok "}]}}, "stopReason": "end_turn"}


@pytest.fixture
def pins(monkeypatch):
    def install(values):
        monkeypatch.setattr(modelweave_roles, "_default",
                            modelweave_roles.RoleResolver(Ssm(values), enabled=True))
    yield install
    modelweave_roles.reset_default_resolver()


@pytest.fixture
def bedrock(monkeypatch):
    client = Bedrock()
    fake = types.SimpleNamespace(client=lambda *a, **k: client)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    return client


def test_llm_provider_uses_the_pin_through_converse(pins, bedrock):
    from llm_provider.bedrock import BedrockLLMProvider

    pins({"/modelweave/roles/reasoning": PRO})
    provider = BedrockLLMProvider(model_id=HAIKU)
    assert provider.model_id == PRO
    assert provider.invoke("be brief", "turn on the lamp", max_tokens=64) == "ok"
    model, kwargs = bedrock.calls[0]
    # Converse, not an Anthropic-format invoke_model body: a Nova pin works.
    assert model == PRO
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "turn on the lamp"}]}]
    assert kwargs["inferenceConfig"] == {"maxTokens": 64}


def test_llm_provider_falls_back_without_a_pin(pins, bedrock):
    from llm_provider.bedrock import BedrockLLMProvider

    pins({})
    BedrockLLMProvider(model_id=HAIKU).invoke("s", "u")
    assert bedrock.calls[0][0] == HAIKU


def test_agent_converse_uses_the_pin_and_recovers(pins):
    import bedrock_agent

    pins({"/modelweave/roles/reasoning": PRO})
    client = Bedrock({PRO: ClientError("AccessDeniedException",
                                       f"not authorized on inference-profile/{PRO}")})
    response = bedrock_agent._call_bedrock_converse(client, system=[], messages=[])
    assert response["stopReason"] == "end_turn"
    assert [model for model, _ in client.calls] == [PRO, HAIKU]
    assert all("modelId" not in kwargs for _, kwargs in client.calls)


def test_every_bedrock_grant_covers_the_reasoning_candidates_and_the_pin_read():
    template = (ROOT / "template.yaml").read_text()
    grants = template.count('foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"')
    assert grants == 5
    for model in ("amazon.nova-pro-v1:0", "meta.llama3-3-70b-instruct-v1:0"):
        assert template.count(f'foundation-model/{model}"') == grants
        assert template.count(f"inference-profile/us.{model}") == grants
    assert template.count("parameter/modelweave/roles/*") == grants
    assert "ssm:PutParameter" not in template
