# DeviceWeave test suite

```
python3 -m pytest -q     # 79 passed in well under 1s
```

`pytest.ini` sets `pythonpath = src` so every module under `src/` imports by
its bare name (`import app`, `import decision_engine`, …), exactly as the
Lambda runtime sees it. No `pip install -r src/requirements.txt` is required
to run the suite — `tests/conftest.py` stubs every network/AWS-touching
dependency (see below), and `requirements-dev.txt` lists the only two
packages actually needed (`pytest`, `boto3`).

## What's covered

| Module | What's tested |
|---|---|
| `decision_engine.compute_score` | adaptive α (0.9 below `_MIN_HISTORY_EVENTS`=10, 0.5 at/above), the `final = α·cosine + (1−α)·behavior` formula including its floating-point rounding quirk, context fallback to `behavior_engine.current_context()` |
| `decision_engine.validate_execution` | threshold rejection/acceptance (`<` vs `>=` boundary), capability rejection/acceptance |
| `decision_engine.classify_intent` | direct / behavior / scene routing |
| `behavior_engine.score` | neutral 0.5 with no history + neutral weather, weather-only blend with no history, time+frequency+weather blend with history, `infer_device_class` priority (capability > name > default) |
| `intent_parser.parse_intent` | a handful of the README's example utterances (turn on/off, dim with/without a number, brightness clamping, status queries not being confused with "turn on", too-short input) |
| `device_resolver.resolve_device` | the TF-vector cosine path with a stubbed device/phrase corpus, blank query, empty catalog, and registry-not-configured propagation |
| `policy_engine.evaluator.compute_verdict` | BLOCK/MODIFY/ALLOW precedence using the README's three example rules verbatim, AND-semantics across multiple conditions, BLOCK > MODIFY precedence, the `turn_off`/`get_status` safe-action bypass, and defensive device_type re-scoping |
| `observatory_wrapper.get_wrapper` / `_push_metric` | singleton caching, graceful `None` on import failure, the exact DynamoDB item shape `_push_metric` writes (types included — token counts as `Decimal`), non-blocking behaviour on a DynamoDB failure |
| `observatory_wrapper.observe_bedrock_converse` | falls through to the wrapped call when the wrapper is unavailable; telemetry-only (pre-gate) behaviour when the wrapper exposes no `invoke()`; and, on the gated path, REVIEW/BLOCK/missing-verdict refusals (`ObservatoryGateError`, span still pushed with `gate_decision`/`gate_reason`), the `gate_decision`/`gate_reason` annotation on ALLOW, a Bedrock failure propagating unchanged and un-retried, and gate-machinery failure refusing rather than re-invoking the model. Four tests run against the real `mcp-observatory` (`importorskip`): ALLOW, empty-output BLOCK, latency-budget REVIEW, and construction with no `MCP_OBSERVATORY_*` secrets set |
| `app` confidence thresholds | per-gate override, fallback to `CONFIDENCE_THRESHOLD`, the shipped `0.4` default, non-numeric fallback, and one test per gate (scene / device / LLM) proving each is independent of the other two |
| `app.handler` route dispatch | `GET /health` (registry configured and not), `POST /execute` happy path with the resolver and executor stubbed, unsupported-capability 422, missing/invalid body 400s, both-tiers-failed 422, and unknown-route/unknown-method 404s |

## What's stubbed, and why

`tests/conftest.py` has four pieces, all **autouse unless noted**:

- **Table env vars cleared** (`DEVICE_REGISTRY_TABLE`, `POLICY_TABLE_NAME`,
  `LEARNING_TABLE_NAME`, `SCENE_TABLE_NAME`, `PRESENCE_TABLE_NAME`,
  `OBSERVATORY_METRICS_TABLE`, `CONVERSATION_TABLE_NAME`, `MEMGRAPH_HOST`,
  `MEMGRAPH_SECRET_ARN`) so every module's own documented "not configured"
  fail-open path is what actually runs (e.g. `policy_engine.middleware
  .enforce()` returns ALLOW; `graph_engine` reports no history) — no real
  boto3/Bolt/HTTP call is ever attempted from most tests without any mocking
  effort at all.
- **`graph_engine`** — `record_event`/`query_behavior_history`/
  `query_top_actions`/`is_available` are replaced with in-memory fakes.
  The real implementation already fails open (no Memgraph reachable → zeroed
  history), but it gets there via a `boto3.client("ec2").describe_instances`
  call and a background `ThreadPoolExecutor` write — both avoided here so
  tests stay fast, deterministic, and never leave a thread running past the
  test.
- **`weather_client.get_weather`** — replaced with `lambda: {}` (the same
  shape the real client returns on a network failure) so nothing ever calls
  `urlopen()`. Individual tests override this via `monkeypatch` when a
  test needs a specific weather-driven branch.
- **`aws_clients.get_dynamodb_resource`** — a `fake_dynamodb` fixture
  (opt-in, not autouse) swaps in an in-memory resource with a minimal
  `Table.put_item`/`get_item`/`scan`. Used by the `observatory_wrapper`
  metric-shape tests.
- **`mcp_observatory`** — two opt-in fixtures: `missing_mcp_observatory`
  (forces `ImportError` via `sys.modules["mcp_observatory"] = None`) and
  `fake_mcp_observatory` (injects a minimal fake module, whose wrapper
  deliberately has no `invoke()` and so exercises the telemetry-only
  degradation path) so the "package available" tests don't depend on the
  real `mcp-observatory` package actually being pip-installed in whatever
  environment runs this suite. The four tests that *do* want the real
  library `pytest.importorskip` it, so the suite still runs green without
  it — install `mcp-observatory>=0.3.0` to exercise them.

`app.py` route-dispatch tests additionally monkeypatch `app.resolve_scene`,
`app.resolve_device`, `app.decision_engine.compute_score`, `app.execute_steps`,
`app._get_active_catalog`, and `app.llm_resolve` directly on the `app` module
object — `app.py` does `from device_resolver import resolve_device` etc., which
binds its own name at import time, so patching the *source* module afterwards
would silently not affect what `app.py` calls.

## The two behaviours this suite pins for the `mcp-observatory` `>=0.3.0` re-pin

The task names two behaviours the 0.3.0 gate release (HIGH-criticality calls
with too little evidence → `REVIEW` instead of `ALLOW`; dev secrets fail
closed without `MCP_OBSERVATORY_ALLOW_DEV_SECRET=1`) will exercise:

1. **Observatory wrapper import-failure tolerance** — pinned by
   `test_get_wrapper_returns_none_when_package_missing` and
   `test_observe_bedrock_converse_falls_through_when_wrapper_unavailable`.
   This already holds today (`get_wrapper()`'s `try/except Exception` around
   the import) and must keep holding across the version bump — if `0.3.0`
   changes what exception type an incompatible import raises, or adds a
   required constructor argument to `instrument_wrapper_api()` that raises on
   call, these tests catch it immediately.

2. **Execute path when the gate returns REVIEW** — now wired and asserted.
   `observe_bedrock_converse()` runs the Bedrock call through
   `InvocationWrapperAPI.invoke()` and enforces the returned
   `WrapperDecision`: `review`, `block` and a missing verdict all raise
   `ObservatoryGateError`, so the agentic loop never reaches tool dispatch
   with an uncleared answer; only `allow` returns the response. See "the
   former xfail" below.

## The former xfail

**`test_observatory_wrapper.py::test_observe_bedrock_converse_surfaces_review_verdict`**

This was a `strict=True` xfail recording that
`observatory_wrapper.observe_bedrock_converse()` never called any
decision/gating method on the wrapper object `get_wrapper()` returned — it
used the wrapper only as a truthy on/off switch, built its own span dict from
the raw Bedrock response, and wrote that straight to DynamoDB. A `REVIEW`
verdict was therefore indistinguishable from `ALLOW`, at 0.2.0 as much as at
0.3.0.

The gate is now wired (`ObservatoryGateError`), so the test is a plain
assertion: a REVIEW verdict raises, carries `gate_decision` / `gate_reason` /
`gate_metadata`, and still writes its telemetry span. The suite has no xfails
left — a re-introduced gap would show up as a failure, not as an expected one.

No other correctness bugs were found and marked `xfail`; the rest of the
findings are README/code discrepancies documented in the task's final report.
