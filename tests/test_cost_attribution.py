"""Bedrock spend and every other resource here are billed to DeviceWeave.

The stack is deployed with the tag repo=DeviceWeave, which CloudFormation copies onto
each resource. Bedrock calls go through this stack's own application inference
profiles, tagged the same way, via the vendored bedrock_cost.py; that is the
only way a model call can carry a tag (ModelWeave scripts/weave_costs.py reads
it back).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "DeviceWeave"


class _Loader(yaml.SafeLoader):
    pass


def _intrinsic(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_Loader.add_multi_constructor("!", _intrinsic)
TEMPLATE = yaml.load((ROOT / "template.yaml").read_text(), Loader=_Loader)
TEXT = (ROOT / "template.yaml").read_text()


def _profiles():
    return {
        name: res for name, res in TEMPLATE["Resources"].items()
        if res["Type"] == "AWS::Bedrock::ApplicationInferenceProfile"
    }


def test_vendored_bedrock_cost_is_the_pinned_modelweave_file():
    manifest = json.loads((ROOT / "BEDROCK_COST_VENDORED.json").read_text())
    data = (ROOT / manifest["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["sha256"]


def test_every_profile_is_tagged_with_this_repo():
    profiles = _profiles()
    assert profiles
    for name, res in profiles.items():
        assert {"Key": "repo", "Value": REPO} in res["Properties"]["Tags"], name


def test_the_profile_map_names_exactly_the_profiles_the_stack_creates():
    mapped = re.findall(r'"([^"]+)": "\$\{(BedrockProfile\w+)\.InferenceProfileArn\}"', TEXT)
    assert {logical for _, logical in mapped} == set(_profiles())
    for model, logical in mapped:
        source = json.dumps(_profiles()[logical]["Properties"]["ModelSource"])
        assert model in source  # each model id maps to a copy of that same model


def test_iam_can_invoke_each_profile():
    for logical in _profiles():
        assert f"!GetAtt {logical}.InferenceProfileArn" in TEXT or f"{logical}.InferenceProfileArn" in TEXT
    assert TEXT.count(".InferenceProfileArn") >= 2 * len(_profiles())  # map + at least one grant


def test_the_deploy_tags_the_stack_with_this_repo():
    workflows = "".join(p.read_text() for p in (ROOT / ".github" / "workflows").glob("deploy*.y*ml"))
    assert re.search(rf"--tags[^\n]*\brepo={REPO}\b", workflows) or re.search(
        rf"--tags\s*\\\s*\n(\s+\S+=\S+\s*\\?\s*\n)*?\s+repo={REPO}\b", workflows)


def test_the_bedrock_provider_bills_through_the_profiles(monkeypatch):
    import sys
    import types

    profile = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/p1"
    sent = []

    class Runtime:
        def converse(self, modelId, **kwargs):
            sent.append(modelId)
            return {"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: Runtime()))
    monkeypatch.setenv("BEDROCK_PROFILE_ARNS", json.dumps({"us.amazon.nova-pro-v1:0": profile}))
    monkeypatch.setenv("MODELWEAVE_ROLES", "off")
    from llm_provider.bedrock import BedrockLLMProvider

    BedrockLLMProvider(model_id="us.amazon.nova-pro-v1:0").invoke("s", "u")
    assert sent == [profile]
