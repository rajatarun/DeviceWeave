"""graph_engine recovers when the Memgraph Spot instance comes back.

Memgraph stops when its Spot instance is interrupted and can return on a new
IP. Before this, "not found" was cached for the life of a warm container, so a
container that started during an outage never used Memgraph again, and a
cached driver kept a dead IP. These tests drive the module's connection state
with a fake clock, a fake EC2 and a fake Bolt driver; the conftest stubs only
replace the public functions, so the originals are taken at import time.
"""
from __future__ import annotations

import sys
import types

import pytest
from neo4j.exceptions import ServiceUnavailable

import graph_engine as ge

QUERY_HISTORY = ge.query_behavior_history
QUERY_TOP = ge.query_top_actions
WRITE_EVENT = ge._write_event


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Ec2:
    """describe_instances answering with whatever ``ip`` is (None = no instance)."""

    def __init__(self):
        self.ip = None
        self.calls = 0

    def describe_instances(self, **kw):
        self.calls += 1
        if self.ip is None:
            return {"Reservations": []}
        return {"Reservations": [{"Instances": [{"PrivateIpAddress": self.ip}]}]}


class Bolt:
    """GraphDatabase stand-in. ``down`` hosts refuse; ``dies`` makes the next
    query on an open driver raise ServiceUnavailable."""

    def __init__(self):
        self.down: set[str] = set()
        self.connects: list[str] = []
        self.dies = False
        self.closed: list[str] = []

    def driver(self, uri, auth=None, connection_timeout=None):
        host = uri.split("//")[1].split(":")[0]
        self.connects.append(host)
        bolt = self

        class Session:
            def __enter__(self):
                if host in bolt.down:
                    raise ServiceUnavailable(f"Couldn't connect to {host}:7687")
                return self

            def __exit__(self, *a):
                return False

            def run(self, query, **kw):
                if bolt.dies and "CREATE INDEX" not in query:
                    raise ServiceUnavailable("connection reset")

                class R:
                    def single(self):
                        return {"cnt": 3}

                    def __iter__(self):
                        return iter([{"action": "on", "freq": 2}])
                return R()

        class Driver:
            def session(self):
                return Session()

            def close(self):
                bolt.closed.append(host)
        return Driver()


@pytest.fixture
def world(monkeypatch):
    clock, ec2, bolt = Clock(), Ec2(), Bolt()
    monkeypatch.setattr(ge, "_clock", clock)
    monkeypatch.setattr(ge, "_MEMGRAPH_HOST_OVERRIDE", "")
    monkeypatch.setattr(ge, "_RETRY_SECONDS", 60.0)
    monkeypatch.setattr(ge, "_driver", None)
    monkeypatch.setattr(ge, "_resolved_host", None)
    monkeypatch.setattr(ge, "_retry_after", 0.0)
    monkeypatch.setattr(ge, "_cred_cache", {"username": "", "password": ""})
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: ec2))
    real_neo4j = sys.modules.get("neo4j")
    fake = types.ModuleType("neo4j")
    fake.GraphDatabase = bolt
    fake.exceptions = real_neo4j.exceptions
    monkeypatch.setitem(sys.modules, "neo4j", fake)
    return clock, ec2, bolt


def test_an_instance_that_returns_is_used_after_the_retry_window(world):
    clock, ec2, bolt = world
    assert QUERY_HISTORY("d", "on", 8) == {"matching": 0, "total": 0}
    assert ec2.calls == 1

    ec2.ip = "10.0.0.5"  # the Spot instance comes back
    clock.now += 30
    assert QUERY_HISTORY("d", "on", 8) == {"matching": 0, "total": 0}
    assert ec2.calls == 1, "inside the window: no EC2 call"

    clock.now += 31
    assert QUERY_HISTORY("d", "on", 8) == {"matching": 3, "total": 3}
    assert bolt.connects == ["10.0.0.5"]


def test_a_refused_connect_waits_out_the_window_instead_of_paying_the_timeout(world):
    clock, ec2, bolt = world
    ec2.ip = "10.0.0.5"
    bolt.down.add("10.0.0.5")
    for _ in range(5):
        QUERY_HISTORY("d", "on", 8)
    assert bolt.connects == ["10.0.0.5"], "one connect attempt per window, not per request"

    bolt.down.clear()
    clock.now += 61
    assert QUERY_HISTORY("d", "on", 8)["total"] == 3


def test_a_lost_connection_rediscovers_the_new_ip(world):
    clock, ec2, bolt = world
    ec2.ip = "10.0.0.5"
    assert QUERY_HISTORY("d", "on", 8)["total"] == 3

    # Interrupted and back on a different IP; the cached driver now fails.
    bolt.dies = True
    ec2.ip = "10.0.0.9"
    assert QUERY_TOP("d") == []
    assert bolt.closed == ["10.0.0.5"]
    bolt.dies = False

    assert QUERY_HISTORY("d", "on", 8) == {"matching": 0, "total": 0}  # inside the window
    clock.now += 61
    assert QUERY_HISTORY("d", "on", 8)["total"] == 3
    assert bolt.connects == ["10.0.0.5", "10.0.0.9"]


def test_a_bad_query_does_not_drop_a_healthy_connection(world):
    clock, ec2, bolt = world
    ec2.ip = "10.0.0.5"
    assert QUERY_HISTORY("d", "on", 8)["total"] == 3
    assert ge._on_query_error("x", ValueError("bad cypher")) is None
    assert ge._driver is not None and bolt.closed == []


def test_the_background_writer_drops_a_lost_connection_too(world):
    clock, ec2, bolt = world
    ec2.ip = "10.0.0.5"
    assert QUERY_HISTORY("d", "on", 8)["total"] == 3
    bolt.dies = True
    WRITE_EVENT("d", "on", "turn on")
    assert ge._driver is None and bolt.closed == ["10.0.0.5"]


def test_errors_that_mean_the_server_is_gone():
    assert ge._connection_lost(ServiceUnavailable("x"))
    assert ge._connection_lost(ConnectionResetError())
    assert not ge._connection_lost(ValueError("syntax"))
