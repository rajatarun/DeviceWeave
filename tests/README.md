# DeviceWeave test suite

```
python3 -m pytest -q     # 62 passed, 1 xfailed in well under 1s
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
| `observatory_wrapper.observe_bedrock_converse` | falls through to the wrapped call when the wrapper is unavailable; pushes a metric on success |
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
  `fake_mcp_observatory` (injects a minimal fake module) so the
  "package available" tests don't depend on the real `mcp-observatory`
  package actually being pip-installed in whatever environment runs this
  suite.

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

2. **Execute path when the gate returns REVIEW** — **the code has no such
   path today**, so this is the `xfail`
   (`test_observe_bedrock_converse_surfaces_review_verdict` in
   `test_observatory_wrapper.py`). See "xfail" below for why.

## xfail

**`test_observatory_wrapper.py::test_observe_bedrock_converse_surfaces_review_verdict`**

Reason recorded on the test: `observatory_wrapper.observe_bedrock_converse()`
never calls any decision/gating method on the wrapper object `get_wrapper()`
returns — it only checks the wrapper for truthiness as an on/off switch, then
builds its own span dict from the raw Bedrock response and writes it straight
to DynamoDB via `_push_metric()`. No `WrapperDecision`, no `.decide()` call,
nothing from `mcp_observatory.policy`/`risk`/`fallback` is ever invoked. This
is true **today, at `mcp-observatory==0.2.0`**, independent of the 0.3.0
re-pin: even the *current* `WrapperPolicy.decide()` (which already returns
`action="review"` on a cost/latency budget overrun in 0.2.0) is dead code as
far as DeviceWeave is concerned. So when 0.3.0 adds the HIGH-criticality
insufficient-evidence → `REVIEW` gate, there is still nothing in
`observatory_wrapper.py` or `bedrock_agent.py` that would observe or act on
it — a `REVIEW` verdict is silently indistinguishable from `ALLOW`.

The test is marked `strict=True` specifically so that if someone later wires
the gate in, the test flips to an unexpected pass (XPASS) and **fails the
run**, forcing the assertion to be promoted from "documents the gap" to
"proves the fix."

No other correctness bugs were found and marked `xfail`; the rest of the
findings are README/code discrepancies documented in the task's final report.
