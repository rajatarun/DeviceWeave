"""
Shared fixtures for the DeviceWeave test suite.

Design goals
------------
- Offline: no test may reach the network (Open-Meteo, Memgraph/Bolt,
  DynamoDB, Bedrock, provider clouds) or spin up a background thread that
  outlives the test.
- Fast: the whole suite must run in well under 10s.
- Safe by default: every test starts with all DynamoDB-table env vars
  UNSET, matching each module's own documented "not configured" fail-open
  behaviour (e.g. policy_engine.middleware.enforce() returns ALLOW when
  POLICY_TABLE_NAME is unset; device_resolver raises DeviceRegistryError
  when DEVICE_REGISTRY_TABLE is unset). Tests that need a table opt in
  explicitly via monkeypatch.setenv + the `fake_dynamodb` fixture.

`pythonpath = src` in pytest.ini makes every module under src/ importable
by its bare name (e.g. `import app`, `import decision_engine`) exactly as
the Lambda runtime imports them (each handler file assumes `src/` is on
sys.path, mirroring how SAM packages the function).
"""

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    # Belt-and-suspenders alongside pytest.ini's `pythonpath = src` — keeps
    # these tests importable even if a test runner ignores the ini file.
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Env var isolation
# ---------------------------------------------------------------------------

# Every table/host env var read at import time or call time by src modules.
# Left unset unless a specific test opts in.
_TABLE_ENV_VARS = [
    "DEVICE_REGISTRY_TABLE",
    "POLICY_TABLE_NAME",
    "LEARNING_TABLE_NAME",
    "SCENE_TABLE_NAME",
    "PRESENCE_TABLE_NAME",
    "OBSERVATORY_METRICS_TABLE",
    "CONVERSATION_TABLE_NAME",
    "MEMGRAPH_HOST",
    "MEMGRAPH_SECRET_ARN",
]


@pytest.fixture(autouse=True)
def _clean_table_env(monkeypatch):
    """Ensure no table/host env var leaks in from the host shell or a prior test."""
    for var in _TABLE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Harmless safety net: if any code path slips past our stubs and
    # constructs a real boto3 client, give it a region so it fails on
    # (mocked/absent) network rather than botocore.exceptions.NoRegionError.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# graph_engine (Memgraph) — always stubbed, never touches EC2/Bolt/network
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_graph_engine(monkeypatch):
    """
    Replace graph_engine's public surface with in-memory fakes.

    Real graph_engine fails open already (no Memgraph reachable → zeroed
    history, no-op writes), but it does so by attempting a boto3 EC2
    describe-instances call and, on record_event, submitting work to a
    background ThreadPoolExecutor. Both are avoided here so tests are fast,
    deterministic, and never touch the network or leave a background thread
    running past the test.
    """
    import graph_engine

    recorded = []

    def fake_query_behavior_history(device_id, action, hour, hour_window=2):
        return {"matching": 0, "total": 0}

    def fake_record_event(device_id, action, command):
        recorded.append((device_id, action, command))

    monkeypatch.setattr(graph_engine, "query_behavior_history", fake_query_behavior_history)
    monkeypatch.setattr(graph_engine, "record_event", fake_record_event)
    monkeypatch.setattr(graph_engine, "query_top_actions", lambda device_id, limit=5: [])
    monkeypatch.setattr(graph_engine, "is_available", lambda: False)
    return recorded


# ---------------------------------------------------------------------------
# weather_client (Open-Meteo) — always stubbed, never makes an HTTP call
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def stub_weather(monkeypatch):
    """
    Default weather_client.get_weather() to a neutral empty dict (network
    failure shape per its own docstring). Tests that care about a specific
    weather-driven branch override this with monkeypatch inside the test.
    """
    import weather_client

    monkeypatch.setattr(weather_client, "get_weather", lambda: {})
    monkeypatch.setattr(weather_client, "_cache", {}, raising=False)


# ---------------------------------------------------------------------------
# Fake DynamoDB resource — opt-in for tests that need a table
# ---------------------------------------------------------------------------

class FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, name: str):
        self.name = name
        self.put_items: list = []
        self._items: Dict[tuple, Dict[str, Any]] = {}

    def put_item(self, Item: Dict[str, Any]) -> Dict[str, Any]:
        self.put_items.append(Item)
        key = (Item.get("pk"), Item.get("sk"))
        self._items[key] = Item
        return {}

    def get_item(self, Key: Dict[str, Any]) -> Dict[str, Any]:
        key = (Key.get("pk"), Key.get("sk"))
        item = self._items.get(key)
        return {"Item": item} if item is not None else {}

    def scan(self, **kwargs) -> Dict[str, Any]:
        return {"Items": list(self._items.values())}


class FakeDynamoDBResource:
    """Minimal in-memory stand-in for boto3.resource("dynamodb")."""

    def __init__(self):
        self._tables: Dict[str, FakeTable] = {}

    def Table(self, name: str) -> FakeTable:
        return self._tables.setdefault(name, FakeTable(name))


@pytest.fixture
def fake_dynamodb(monkeypatch):
    """
    Patch aws_clients.get_dynamodb_resource() to return an in-memory fake,
    everywhere it's imported (aws_clients, app, learning_store, scene_catalog,
    policy_engine.*, observatory_wrapper all do `from aws_clients import
    get_dynamodb_resource`, binding their own module-local name — so the
    patch is applied on aws_clients itself AND re-bound on every module that
    already imported it before this fixture ran).
    """
    import aws_clients

    fake = FakeDynamoDBResource()
    monkeypatch.setattr(aws_clients, "get_dynamodb_resource", lambda: fake)

    for modname in (
        "app",
        "learning_store",
        "scene_catalog",
        "policy_engine.context_provider",
        "policy_engine.policy_loader",
        "observatory_wrapper",
    ):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "get_dynamodb_resource"):
            monkeypatch.setattr(mod, "get_dynamodb_resource", lambda: fake)

    return fake


# ---------------------------------------------------------------------------
# mcp_observatory — present/absent toggling for observatory_wrapper tests
# ---------------------------------------------------------------------------

@pytest.fixture
def missing_mcp_observatory(monkeypatch):
    """
    Simulate the `mcp-observatory` package being uninstalled/unimportable.

    Setting sys.modules['mcp_observatory'] = None makes `import
    mcp_observatory` (and `from mcp_observatory import ...`) raise
    ImportError, per Python's import system — without needing to actually
    uninstall the real package from the test environment.
    """
    monkeypatch.setitem(sys.modules, "mcp_observatory", None)


@pytest.fixture
def fake_mcp_observatory(monkeypatch):
    """
    Inject a fake, minimal `mcp_observatory` module so tests exercising
    observatory_wrapper's "package available" path do not depend on the
    real mcp-observatory package (currently pinned ==0.2.0, soon >=0.3.0)
    actually being pip-installed in whatever environment runs this suite.
    Returns the sentinel object that a successful get_wrapper() call must
    return unchanged.
    """
    import types

    fake_module = types.ModuleType("mcp_observatory")
    sentinel = object()
    fake_module.instrument_wrapper_api = lambda service_name, **kwargs: sentinel
    monkeypatch.setitem(sys.modules, "mcp_observatory", fake_module)
    return sentinel
