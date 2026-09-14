"""Conformance of DeviceWeave's OBSERVATORY_METRICS writer against the
shared table contract vendored at ``contracts/observatory_metrics_item.json``.

DeviceWeave is one of several writers on a table it does not own; a sibling
repo was once found writing ``PK``/``SK`` (uppercase) into a table keyed on
lowercase ``pk``/``sk``, inside a bare ``except: pass``, so every write was
silently rejected by DynamoDB and reported as success. Nothing caught that
until a manual audit. These tests exercise the *real* writer — the actual
``_push_metric`` / ``observe_bedrock_converse`` code paths already covered in
``test_observatory_wrapper.py``, using the same ``fake_dynamodb`` /
``fake_mcp_observatory`` fixtures from ``conftest.py`` — and run the emitted
item through the vendored, dependency-free ``contracts/conformance.py`` so a
regression like that fails a local test instead of a cross-repo audit.

Assertions are driven from the vendored contract file wherever possible
(``check_item``, ``gsi``, ``discriminator_values``) rather than from
constants retyped here, so a contract version bump that DeviceWeave hasn't
caught up with breaks this test instead of silently drifting.
"""

import ast
import re
from pathlib import Path

import pytest

import observatory_wrapper as ow
from contracts.conformance import check_item, load_contract

CONTRACT = load_contract()  # finds contracts/observatory_metrics_item.json beside conformance.py

_WRAPPER_SRC = Path(ow.__file__)


@pytest.fixture(autouse=True)
def _reset_module_singletons(monkeypatch):
    """observatory_wrapper caches _wrapper/_ddb_table at module scope."""
    monkeypatch.setattr(ow, "_wrapper", None)
    monkeypatch.setattr(ow, "_ddb_table", None)


# ---------------------------------------------------------------------------
# I1/I2/I3/I4/I5 against a real item emitted by the telemetry-only path
# (_observe_only, used when the wrapper is present but exposes no invoke()).
# ---------------------------------------------------------------------------

def test_telemetry_only_item_conforms_to_the_shared_contract(monkeypatch, fake_dynamodb, fake_mcp_observatory):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="anthropic.claude-haiku-4.5", session_id="sess-1")
    def wrapped():
        return {
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "responseMetadata": {"RequestId": "req-contract-1"},
        }

    wrapped()

    table = fake_dynamodb.Table("obs-metrics-dev")
    assert len(table.put_items) == 1
    item = table.put_items[0]

    assert check_item(item, CONTRACT) == []


# ---------------------------------------------------------------------------
# Same check on the gated path (real observe_bedrock_converse decision
# handling), so both code paths that can write a row are covered, not just
# one of them.
# ---------------------------------------------------------------------------

class _FakeDecision:
    def __init__(self, action, reason="", metadata=None):
        self.action = action
        self.reason = reason
        self.metadata = metadata or {}


class _FakeSpan:
    from datetime import datetime as _dt

    trace_id = "trace-contract-2"
    start_time = _dt(2026, 4, 25, 1, 14, 0)
    end_time = _dt(2026, 4, 25, 1, 14, 2)
    prompt_tokens = 11
    completion_tokens = 3
    cost_usd = 0.00007
    composite_risk_score = 0.42


class _FakeResult:
    def __init__(self, output, decision):
        self.output = output
        self.span = _FakeSpan()
        self.decision = decision


class _FakeGateWrapper:
    """Stand-in for InvocationWrapperAPI with a fixed "allow" verdict."""

    async def invoke(self, *, source, model, prompt, input_payload, call, **kwargs):
        output = call()
        return _FakeResult(output, _FakeDecision("allow", "within_budget"))


def test_gated_path_item_conforms_to_the_shared_contract(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    monkeypatch.setattr(ow, "_wrapper", _FakeGateWrapper())

    @ow.observe_bedrock_converse(model_id="anthropic.claude-haiku-4.5", session_id="sess-9")
    def wrapped(client, **kwargs):
        return {"usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn"}

    wrapped(object(), modelId="test-model", messages=[{"role": "user", "content": [{"text": "hi"}]}])

    table = fake_dynamodb.Table("obs-metrics-dev")
    assert len(table.put_items) == 1
    item = table.put_items[0]

    assert check_item(item, CONTRACT) == []


# ---------------------------------------------------------------------------
# I1, spelled out explicitly: this is the invariant that stops the recurring
# failure mode (PK/SK written into a pk/sk-keyed table, swallowed by a bare
# except). check_item() == [] above already proves this, but the key spelling
# is important enough to assert on directly rather than only transitively.
# ---------------------------------------------------------------------------

def test_key_attributes_are_lowercase_pk_sk_never_uppercase(monkeypatch, fake_dynamodb, fake_mcp_observatory):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped():
        return {"usage": {"inputTokens": 1, "outputTokens": 1}, "responseMetadata": {"RequestId": "req-2"}}

    wrapped()

    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]

    assert CONTRACT["key_schema"]["partition_key"] == "pk"
    assert CONTRACT["key_schema"]["sort_key"] == "sk"
    assert "pk" in item and "sk" in item
    assert "PK" not in item
    assert "SK" not in item


# ---------------------------------------------------------------------------
# Contract v2.0.0: reads go through the SpanTimelineIndex GSI, not pk.
# I5 (pk reachability / readers_for) is superseded -- a GSI indexes only items
# carrying both of its key attributes, so that is now the reachability check.
# ---------------------------------------------------------------------------

def test_emitted_item_carries_the_span_timeline_index_keys(monkeypatch, fake_dynamodb, fake_mcp_observatory):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped():
        return {"usage": {"inputTokens": 1, "outputTokens": 1}, "responseMetadata": {"RequestId": "req-3"}}

    wrapped()

    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]
    gsi = CONTRACT["gsi"]

    assert gsi["partition_key"] in item, (
        f"item has no '{gsi['partition_key']}' — it would not be in the SpanTimelineIndex "
        "and no dashboard would ever show it"
    )
    assert gsi["sort_key"] in item, f"item has no '{gsi['sort_key']}'"
    assert item[gsi["partition_key"]] == item[gsi["sort_key"]][:10]
    assert check_item(item, CONTRACT) == []


# ---------------------------------------------------------------------------
# The operation values DeviceWeave's writer can actually emit must all be
# registered discriminator_values for the OBSERVATORY namespace. Found by
# parsing the real source (every literal first argument passed to the module-
# level _push_metric(...)) rather than retyping "invoke_agent" here, so a new
# call site with an unregistered operation is caught even if no test happens
# to exercise it.
# ---------------------------------------------------------------------------

def _operations_emitted_by_source() -> set[str]:
    tree = ast.parse(_WRAPPER_SRC.read_text(encoding="utf-8"), filename=str(_WRAPPER_SRC))
    ops: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_push_metric"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            ops.add(node.args[0].value)
    return ops


def test_operations_emitted_are_all_registered_discriminator_values():
    emitted = _operations_emitted_by_source()
    allowed = set(CONTRACT["namespace_registry"]["OBSERVATORY"]["discriminator_values"])

    # Sanity: the static scan must actually have found DeviceWeave's real
    # call sites, or this test would pass vacuously.
    assert emitted, "found no _push_metric(...) call sites in observatory_wrapper.py"

    unregistered = emitted - allowed
    assert not unregistered, (
        f"observatory_wrapper.py emits operation(s) {sorted(unregistered)} not in the "
        f"contract's OBSERVATORY.discriminator_values {sorted(allowed)}; those rows would "
        "land in a partition no reader enumerates. This is a real finding — either the "
        "operation is wrong here, or the contract's registry is out of date."
    )

    # DeviceWeave's docstring claims both "invoke_agent" and "invoke_model" are
    # emitted, but every real _push_metric call site in this module passes the
    # literal "invoke_agent" — confirmed by the static scan above. Pinned here
    # so a future call site that actually emits "invoke_model" (matching the
    # docstring) is a deliberate, visible change to this test, not a surprise.
    assert emitted == {"invoke_agent"}, (
        f"expected DeviceWeave to emit exactly {{'invoke_agent'}} today, found {sorted(emitted)}; "
        "update this pin (and check the module docstring) if that's an intentional change"
    )


def test_operations_emitted_are_registered_via_dynamic_capture(monkeypatch, fake_dynamodb, fake_mcp_observatory):
    """Belt-and-suspenders: drive the real writer and check the pk it actually
    put on the wire, not just what static analysis found."""
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped():
        return {"usage": {"inputTokens": 1, "outputTokens": 1}, "responseMetadata": {"RequestId": "req-4"}}

    wrapped()

    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]
    match = re.match(r"^([A-Z_]+)#(.+)$", item["pk"])
    assert match is not None

    namespace, operation = match.group(1), match.group(2)
    registry_entry = CONTRACT["namespace_registry"][namespace]
    assert operation in registry_entry["discriminator_values"]
