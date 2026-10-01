"""
Shared boto3 factories with VPC-safe network settings.

The Lambdas run inside the shared VPC (tarun-teamweave-shared). Its IPv4
internet path exists only while the NAT instance is up, but DynamoDB does not
need that path: the shared stack attaches a DynamoDB *Gateway* VPC endpoint to
the Lambda route tables, which carries traffic for the regional endpoint
``dynamodb.<region>.amazonaws.com`` privately whether the NAT instance is up or
down.

This module hands out a cached resource that:

  - uses that regional IPv4 endpoint, NOT the dual-stack one. The dual-stack
    hostname ``dynamodb.<region>.api.aws`` is outside what the Gateway endpoint
    serves, so it has to leave the VPC: over IPv4 that needs the NAT instance,
    and over IPv6 it needs ``Ipv6AllowedForDualStack`` on the function, which
    no DeviceWeave function sets. With the NAT instance down every registry,
    policy and session read then died with
    ``Connect timeout on endpoint URL: "https://dynamodb.us-east-1.api.aws/"``.
    The sibling TeamWeave stack pins DynamoDB (and S3) to the regional
    endpoint for the same reason;
  - fails fast (3 s connect / 10 s read, 2 attempts) so callers' existing
    error handling runs and logs instead of the Lambda dying mid-call.

AWS_ENDPOINT_URL_DYNAMODB (DynamoDB Local in docker-compose) still takes
precedence; botocore reads it natively.
"""

from typing import Any

_ddb_resource: Any = None


def get_dynamodb_resource() -> Any:
    """Return the cached, VPC-safe DynamoDB service resource."""
    global _ddb_resource
    if _ddb_resource is None:
        import boto3
        from botocore.config import Config as BotocoreConfig

        config = BotocoreConfig(
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
            # Explicit, so an AWS_USE_DUALSTACK_ENDPOINT set on the function
            # cannot move DynamoDB off the Gateway endpoint again.
            use_dualstack_endpoint=False,
        )
        _ddb_resource = boto3.resource("dynamodb", config=config)
    return _ddb_resource
