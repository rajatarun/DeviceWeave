"""
Shared boto3 factories with VPC-safe network settings.

The Lambdas run inside a VPC whose IPv4 internet path exists only while the
NAT instance is up; when it is down, egress is IPv6-only. Plain
boto3.resource("dynamodb") targets the IPv4-only endpoint with botocore's
default 60-second connect timeout and multiple retries, so a single
DynamoDB call can silently outlive the Lambda timeout. This module hands
out a cached resource that:

  - uses the dual-stack endpoint so DynamoDB is reachable over IPv6 when
    the NAT instance is down (and still works over IPv4 when it is up);
  - fails fast (3 s connect / 10 s read, 2 attempts) so callers' existing
    error handling runs and logs instead of the Lambda dying mid-call.

AWS_ENDPOINT_URL_DYNAMODB (DynamoDB Local in docker-compose) takes
precedence over the dual-stack setting, which is skipped in that case.
"""

import os
from typing import Any

_ddb_resource: Any = None


def get_dynamodb_resource() -> Any:
    """Return the cached, VPC-safe DynamoDB service resource."""
    global _ddb_resource
    if _ddb_resource is None:
        import boto3
        from botocore.config import Config as BotocoreConfig

        local_endpoint = os.environ.get("AWS_ENDPOINT_URL_DYNAMODB") or os.environ.get(
            "AWS_ENDPOINT_URL"
        )
        config = BotocoreConfig(
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
            use_dualstack_endpoint=not local_endpoint,
        )
        _ddb_resource = boto3.resource("dynamodb", config=config)
    return _ddb_resource
