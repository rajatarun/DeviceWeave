"""
Unit tests for observatory_wrapper.get_wrapper() and _push_metric().

Also pins two behaviours relevant to the upcoming mcp-observatory 0.2.0 ->
>=0.3.0 re-pin (see tests/README.md):

  1. Import-failure tolerance: get_wrapper() must return None, never raise,
     when mcp_observatory is unimportable — this already holds today and
     must keep holding after the re-pin.
  2. The 0.3.0 REVIEW gate: observe_bedrock_converse() now runs the Bedrock
     call through InvocationWrapperAPI.invoke() and enforces the returned
     WrapperDecision. A "review" or "block" verdict — and an absent one —
     raises ObservatoryGateError so the agentic loop never reaches tool
     dispatch with an uncleared answer; only "allow" returns the response,
     annotated with gate_decision / gate_reason. The telemetry span is
     pushed to DynamoDB on every one of those paths.
"""

from datetime import datetime
from decimal import Decimal

import pytest

import observatory_wrapper as ow


@pytest.fixture(autouse=True)
def _reset_module_singletons(monkeypatch):
    """observatory_wrapper caches _wrapper/_ddb_table at module scope."""
    monkeypatch.setattr(ow, "_wrapper", None)
    monkeypatch.setattr(ow, "_ddb_table", None)


# ---------------------------------------------------------------------------
# get_wrapper()
# ---------------------------------------------------------------------------

def test_get_wrapper_returns_instance_when_package_available(fake_mcp_observatory):
    wrapper = ow.get_wrapper()
    assert wrapper is fake_mcp_observatory


def test_get_wrapper_returns_none_when_package_missing(missing_mcp_observatory):
    wrapper = ow.get_wrapper()
    assert wrapper is None


def test_get_wrapper_caches_singleton_across_calls(fake_mcp_observatory):
    first = ow.get_wrapper()
    second = ow.get_wrapper()
    assert first is second is fake_mcp_observatory


# ---------------------------------------------------------------------------
# _push_metric()
# ---------------------------------------------------------------------------

def test_push_metric_writes_expected_item_shape(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    span = {
        "trace_id": "trace-123",
        "timestamp": "2026-04-25T01:14:00.000000",
        "prompt_tokens": 42,
        "completion_tokens": 7,
        "duration_ms": 512.5,
        "cost_usd": 0.0031,
    }
    extra = {"model_id": "anthropic.claude-haiku-4.5", "session_id": "sess-1", "device_id": "office_light"}

    ow._push_metric("invoke_agent", span, extra)

    table = fake_dynamodb.Table("obs-metrics-dev")
    assert len(table.put_items) == 1
    item = table.put_items[0]

    assert item["pk"] == "OBSERVATORY#invoke_agent"
    assert item["sk"] == "2026-04-25T01:14:00.000000#trace-123"
    assert item["operation"] == "invoke_agent"
    assert item["trace_id"] == "trace-123"
    assert item["model_id"] == "anthropic.claude-haiku-4.5"
    assert item["prompt_tokens"] == Decimal("42")
    assert item["completion_tokens"] == Decimal("7")
    assert item["cost_usd"] == Decimal("0.0031")
    assert item["duration_ms"] == Decimal("512.5")
    assert item["session_id"] == "sess-1"
    assert item["device_id"] == "office_light"
    assert isinstance(item["ttl"], int)


def test_push_metric_is_a_noop_when_table_not_configured():
    # No OBSERVATORY_METRICS_TABLE env var -> _get_ddb_table() returns None
    # -> _push_metric must return quietly rather than raising.
    ow._push_metric("invoke_agent", {"trace_id": "t"}, {"model_id": "m"})


def test_push_metric_never_raises_on_dynamodb_failure(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    def boom(Item):
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(fake_dynamodb.Table("obs-metrics-dev"), "put_item", boom)

    # Must not raise -- observability failures are documented as non-blocking.
    ow._push_metric("invoke_agent", {"trace_id": "t"}, {"model_id": "m"})


# ---------------------------------------------------------------------------
# observe_bedrock_converse()
# ---------------------------------------------------------------------------

def test_observe_bedrock_converse_falls_through_when_wrapper_unavailable(missing_mcp_observatory):
    calls = []

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped(x):
        calls.append(x)
        return {"usage": {"inputTokens": 1, "outputTokens": 1}}

    result = wrapped("hello")

    assert result == {"usage": {"inputTokens": 1, "outputTokens": 1}}
    assert calls == ["hello"]


def test_observe_bedrock_converse_pushes_metric_on_success(monkeypatch, fake_dynamodb, fake_mcp_observatory):
    """Telemetry-only degradation: fake_mcp_observatory's wrapper exposes no
    invoke(), as an mcp-observatory older than the wrapper API would, so the
    call runs ungated and the metric is built from the Bedrock response —
    DeviceWeave's pre-gate behaviour, unchanged."""
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model", session_id="sess-9")
    def wrapped():
        return {"usage": {"inputTokens": 10, "outputTokens": 5}, "responseMetadata": {"RequestId": "req-1"}}

    result = wrapped()

    assert result["usage"]["inputTokens"] == 10
    table = fake_dynamodb.Table("obs-metrics-dev")
    assert len(table.put_items) == 1
    assert table.put_items[0]["prompt_tokens"] == Decimal("10")
    assert table.put_items[0]["session_id"] == "sess-9"


# ---------------------------------------------------------------------------
# observe_bedrock_converse() — gate decision handling
#
# The wrapper object returned by instrument_wrapper_api() is an
# InvocationWrapperAPI: `await wrapper.invoke(source=..., model=..., prompt=...,
# input_payload=..., call=...)` runs `call`, records a span, and returns a
# WrapperResult carrying `.output`, `.span` and `.decision` (a WrapperDecision
# with `.action` in {"allow", "review", "block"}, `.reason`, `.metadata`).
# The fakes below implement exactly that contract; the real library is
# exercised too, further down.
# ---------------------------------------------------------------------------

_BEDROCK_RESPONSE = {
    "usage": {"inputTokens": 10, "outputTokens": 5},
    "stopReason": "end_turn",
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
}

_CONVERSE_KWARGS = {
    "modelId": "test-model",
    "system": [{"text": "system prompt"}],
    "messages": [{"role": "user", "content": [{"text": "turn on the fan"}]}],
}


class _FakeDecision:
    def __init__(self, action, reason="", metadata=None):
        self.action = action
        self.reason = reason
        self.metadata = metadata or {}


class _FakeSpan:
    """Minimal stand-in for mcp_observatory's TraceContext."""

    def __init__(self):
        self.trace_id = "trace-gate"
        self.start_time = datetime(2026, 4, 25, 1, 14, 0)
        self.end_time = datetime(2026, 4, 25, 1, 14, 2)
        self.prompt_tokens = 11
        self.completion_tokens = 3
        self.cost_usd = 0.00007
        self.composite_risk_score = 0.42


class _FakeResult:
    def __init__(self, output, decision):
        self.output = output
        self.span = _FakeSpan()
        self.decision = decision


class FakeGateWrapper:
    """Stand-in for InvocationWrapperAPI with a fixed verdict."""

    def __init__(self, decision, call_raises=None, invoke_raises=None):
        self._decision = decision
        self._call_raises = call_raises
        self._invoke_raises = invoke_raises
        self.invocations = 0

    async def invoke(self, *, source, model, prompt, input_payload, call, **kwargs):
        self.invocations += 1
        self.prompt = prompt
        self.input_payload = input_payload
        output = call()
        if self._invoke_raises is not None:
            raise self._invoke_raises
        return _FakeResult(output, self._decision)


def _wrap(wrapper_obj, monkeypatch, response=None, boom=None):
    """Install `wrapper_obj` as the singleton and return a decorated callable."""
    monkeypatch.setattr(ow, "_wrapper", wrapper_obj)

    calls = []

    @ow.observe_bedrock_converse(model_id="test-model", session_id="sess-9")
    def wrapped(client, **kwargs):
        calls.append(kwargs)
        if boom is not None:
            raise boom
        return response if response is not None else dict(_BEDROCK_RESPONSE)

    wrapped.calls = calls
    return wrapped


def test_observe_bedrock_converse_surfaces_review_verdict(monkeypatch, fake_dynamodb):
    """A REVIEW verdict must not be silently treated as ALLOW.

    Was an xfail: the decorator used get_wrapper() only as a truthy on/off
    switch and never inspected a decision, so REVIEW was indistinguishable
    from ALLOW. The call is now routed through the wrapper's invoke() and the
    verdict is enforced.
    """
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    gate = FakeGateWrapper(_FakeDecision("review", "insufficient_evidence", {"criticality": "high"}))
    wrapped = _wrap(gate, monkeypatch)

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "review"
    assert excinfo.value.gate_reason == "insufficient_evidence"
    assert excinfo.value.gate_metadata == {"criticality": "high"}

    # Telemetry must still be written for a refused invocation.
    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]
    assert item["gate_decision"] == "review"
    assert item["gate_reason"] == "insufficient_evidence"
    assert item["prompt_tokens"] == Decimal("11")
    assert item["duration_ms"] == Decimal("2000.0")
    assert item["composite_risk_score"] == Decimal("0.42")


def test_observe_bedrock_converse_raises_on_block_verdict(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    gate = FakeGateWrapper(_FakeDecision("block", "empty_output"))
    wrapped = _wrap(gate, monkeypatch)

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "block"
    assert excinfo.value.gate_reason == "empty_output"
    assert fake_dynamodb.Table("obs-metrics-dev").put_items[0]["gate_decision"] == "block"


def test_observe_bedrock_converse_refuses_when_verdict_is_missing(monkeypatch, fake_dynamodb):
    """No verdict is not evidence of a safe answer — refuse."""
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    gate = FakeGateWrapper(_FakeDecision("", ""))
    wrapped = _wrap(gate, monkeypatch)

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "unavailable"


def test_observe_bedrock_converse_returns_annotated_result_on_allow(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    gate = FakeGateWrapper(_FakeDecision("allow", "within_budget"))
    wrapped = _wrap(gate, monkeypatch)

    result = wrapped(object(), **_CONVERSE_KWARGS)

    # The Converse response is returned untouched apart from the verdict.
    assert result["stopReason"] == "end_turn"
    assert result["gate_decision"] == "allow"
    assert result["gate_reason"] == "within_budget"
    assert gate.invocations == 1
    assert len(wrapped.calls) == 1

    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]
    assert item["gate_decision"] == "allow"
    assert item["session_id"] == "sess-9"

    # The span prompt is reconstructed from the Converse kwargs.
    assert "system prompt" in gate.prompt
    assert "turn on the fan" in gate.prompt


def test_observe_bedrock_converse_propagates_bedrock_failure_without_retrying(monkeypatch):
    """A failure of the Bedrock call itself surfaces unchanged, and once."""
    boom = RuntimeError("ThrottlingException")
    gate = FakeGateWrapper(_FakeDecision("allow"))
    wrapped = _wrap(gate, monkeypatch, boom=boom)

    with pytest.raises(RuntimeError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value is boom
    assert len(wrapped.calls) == 1


def test_observe_bedrock_converse_refuses_when_gate_machinery_fails(monkeypatch):
    """If the gate cannot produce a verdict, the answer is not cleared."""
    gate = FakeGateWrapper(_FakeDecision("allow"), invoke_raises=RuntimeError("scoring blew up"))
    wrapped = _wrap(gate, monkeypatch)

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "unavailable"
    # The model is not re-invoked behind the gate's back.
    assert len(wrapped.calls) == 1


# ---------------------------------------------------------------------------
# Against the real mcp-observatory (>=0.3.0) when it is installed
# ---------------------------------------------------------------------------

def test_real_wrapper_constructs_without_any_observatory_secrets(monkeypatch):
    """
    0.3.0 makes TokenIssuer/TokenVerifier/CommitTokenManager raise
    InsecureDefaultSecretError when MCP_OBSERVATORY_TOKEN_SECRET /
    MCP_OBSERVATORY_COMMIT_SECRET are unset. DeviceWeave only uses
    instrument_wrapper_api(), which constructs none of them — this pins that,
    so a future switch to instrument()/build_gate() fails here rather than in
    a Lambda.
    """
    pytest.importorskip("mcp_observatory")
    for var in (
        "MCP_OBSERVATORY_TOKEN_SECRET",
        "MCP_OBSERVATORY_COMMIT_SECRET",
        "MCP_OBSERVATORY_ALLOW_DEV_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    wrapper = ow.get_wrapper()

    assert wrapper is not None
    assert callable(getattr(wrapper, "invoke", None))


def test_real_wrapper_allows_a_normal_converse_response(monkeypatch, fake_dynamodb):
    pytest.importorskip("mcp_observatory")
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped(client, **kwargs):
        return dict(_BEDROCK_RESPONSE)

    result = wrapped(object(), **_CONVERSE_KWARGS)

    assert result["gate_decision"] == "allow"
    item = fake_dynamodb.Table("obs-metrics-dev").put_items[0]
    assert item["gate_decision"] == "allow"
    assert item["prompt_tokens"] > Decimal("0")


def test_real_wrapper_blocks_an_empty_converse_response(monkeypatch, fake_dynamodb):
    pytest.importorskip("mcp_observatory")
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped(client, **kwargs):
        return ""

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "block"
    assert excinfo.value.gate_reason == "empty_output"
    assert fake_dynamodb.Table("obs-metrics-dev").put_items[0]["gate_decision"] == "block"


def test_real_wrapper_reviews_when_latency_budget_is_exceeded(monkeypatch, fake_dynamodb):
    """A REVIEW from the real WrapperPolicy refuses, exactly like a BLOCK."""
    pytest.importorskip("mcp_observatory")
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")
    monkeypatch.setenv("OBSERVATORY_MAX_LATENCY_MS", "0")

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped(client, **kwargs):
        return dict(_BEDROCK_RESPONSE)

    with pytest.raises(ow.ObservatoryGateError) as excinfo:
        wrapped(object(), **_CONVERSE_KWARGS)

    assert excinfo.value.gate_decision == "review"
    assert excinfo.value.gate_reason == "latency_budget_exceeded"
