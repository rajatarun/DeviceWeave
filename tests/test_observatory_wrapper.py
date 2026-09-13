"""
Unit tests for observatory_wrapper.get_wrapper() and _push_metric().

Also pins two behaviours relevant to the upcoming mcp-observatory 0.2.0 ->
>=0.3.0 re-pin (see tests/README.md):

  1. Import-failure tolerance: get_wrapper() must return None, never raise,
     when mcp_observatory is unimportable — this already holds today and
     must keep holding after the re-pin.
  2. The 0.3.0 "HIGH-criticality + too little evidence -> REVIEW instead of
     ALLOW" gate: DeviceWeave's observe_bedrock_converse() decorator does
     not currently call any decision/gating method on the wrapper it gets
     from get_wrapper() at all (it only uses the wrapper's truthiness as an
     on/off switch, then does its own ad hoc metric push) — so there is no
     code path today that could distinguish a REVIEW verdict from ALLOW.
     This is documented as an xfail below rather than skipped so it starts
     failing loudly (turns into an unexpected pass, i.e. XPASS) the moment
     someone wires the gate in and the expectation should be promoted to a
     real assertion.
"""

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


@pytest.mark.xfail(
    reason=(
        "mcp-observatory 0.3.0 introduces a HIGH-criticality-with-insufficient-"
        "evidence gate that returns a REVIEW verdict instead of ALLOW. "
        "observatory_wrapper.observe_bedrock_converse() never calls any "
        "decision/gating API on the wrapper object it holds (get_wrapper() "
        "result is only used as a truthy on/off switch) — there is no code "
        "path today that inspects a WrapperDecision at all, so a REVIEW "
        "verdict cannot currently change execution (block, hold for approval, "
        "annotate the response, etc.). This test documents the expected "
        "behaviour once that gate is wired in: a REVIEW decision should be "
        "surfaced on the result rather than silently treated as allowed."
    ),
    strict=True,
)
def test_observe_bedrock_converse_surfaces_review_verdict(monkeypatch, fake_dynamodb):
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", "obs-metrics-dev")

    class FakeReviewWrapper:
        """Stand-in for a future wrapper whose policy gates on REVIEW."""

        def decide(self, *args, **kwargs):
            return type("WrapperDecision", (), {"action": "review", "reason": "insufficient_evidence"})()

    monkeypatch.setattr(ow, "_wrapper", FakeReviewWrapper())
    monkeypatch.setattr(ow, "get_wrapper", lambda: FakeReviewWrapper())

    @ow.observe_bedrock_converse(model_id="test-model")
    def wrapped():
        return {"usage": {"inputTokens": 1, "outputTokens": 1}}

    result = wrapped()

    # Desired future behaviour: a REVIEW verdict must be visible on the
    # result so callers (bedrock_agent.py) can act on it instead of treating
    # the call as an ordinary ALLOW.
    assert result.get("observatory_verdict") == "review"
