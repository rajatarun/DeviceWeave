"""DynamoDB must use the regional endpoint the VPC's Gateway endpoint serves, never the dual-stack one."""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

import aws_clients


@pytest.fixture
def fresh_resource(monkeypatch):
    monkeypatch.setattr(aws_clients, "_ddb_resource", None)
    for var in ("AWS_ENDPOINT_URL_DYNAMODB", "AWS_ENDPOINT_URL", "AWS_USE_DUALSTACK_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    yield
    aws_clients._ddb_resource = None


def _host() -> str:
    return urlparse(aws_clients.get_dynamodb_resource().meta.client.meta.endpoint_url).hostname


def test_uses_the_regional_endpoint_the_gateway_endpoint_routes(fresh_resource):
    assert _host() == "dynamodb.us-east-1.amazonaws.com"


def test_dual_stack_env_cannot_move_dynamodb_off_the_gateway_endpoint(fresh_resource, monkeypatch):
    monkeypatch.setenv("AWS_USE_DUALSTACK_ENDPOINT", "true")
    assert not _host().endswith(".api.aws")


def test_local_endpoint_override_still_wins(fresh_resource, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "http://dynamodb-local:8000")
    assert _host() == "dynamodb-local"


def test_fails_fast(fresh_resource):
    cfg = aws_clients.get_dynamodb_resource().meta.client.meta.config
    assert cfg.connect_timeout <= 3 and cfg.read_timeout <= 10
