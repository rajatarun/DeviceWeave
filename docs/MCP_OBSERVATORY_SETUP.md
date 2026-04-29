# MCP Observatory Setup Guide

This document describes how to configure MCP Observatory for DeviceWeave telemetry collection.

## Overview

MCP Observatory provides instrumentation for AWS Bedrock API calls, recording metrics to DynamoDB for monitoring and analysis. The implementation in DeviceWeave follows the guidelines from [MCP Observatory Implementation](https://github.com/rajatarun/TeamWeave/blob/main/docs/MCP_OBSERVATORY_IMPLEMENTATION.md).

## Prerequisites

- AWS account with Bedrock and DynamoDB access
- Lambda execution role with appropriate IAM permissions (see IAM Policy below)
- Python 3.9+ environment

## Installation

Dependencies are listed in `src/requirements.txt`:
- `mcp-observatory==0.2.0` — telemetry instrumentation library
- `boto3>=1.26.0` — AWS SDK for DynamoDB access

Install with:
```bash
pip install -r src/requirements.txt
```

## Configuration

### 1. Create DynamoDB Table

Create a DynamoDB table named `ObservatoryMetrics` with:
- **Partition Key (pk)**: String — pattern `OBSERVATORY#{operation}` (e.g., `OBSERVATORY#invoke_agent`, `OBSERVATORY#invoke_model`)
- **Sort Key (sk)**: String — pattern `{iso_timestamp}#{trace_id}` (e.g., `2024-01-15T10:30:45.123Z#abc-def-ghi`)
- **TTL Attribute**: `ttl` (90-day automatic expiration)

**Schema rationale:**
- Partition key enables querying spans by operation type (agent invocations vs. model invocations)
- Sort key combines ISO 8601 timestamp with trace ID for chronological ordering and unique identity
- TTL automatically removes old metrics after 90 days to manage storage costs

AWS CLI example:
```bash
aws dynamodb create-table \
  --table-name ObservatoryMetrics \
  --attribute-definitions \
    AttributeName=pk,AttributeType=S \
    AttributeName=sk,AttributeType=S \
  --key-schema \
    AttributeName=pk,KeyType=HASH \
    AttributeName=sk,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST

aws dynamodb update-time-to-live \
  --table-name ObservatoryMetrics \
  --time-to-live-specification "AttributeName=ttl,Enabled=true"
```

### 2. Understand the Item Schema

Each invocation is recorded as a DynamoDB item with attributes including:

**Key Attributes:**
- `pk` (Partition Key): `OBSERVATORY#{operation}` — operation is "invoke_agent" or "invoke_model"
- `sk` (Sort Key): `{iso_timestamp}#{trace_id}` — ISO 8601 timestamp + UUID for uniqueness
- `ttl` (Number): Epoch timestamp set 90 days in future (auto-deletes via TTL)

**Telemetry Attributes:**
- `prompt_tokens`, `completion_tokens` — token usage from Bedrock response
- `cost_usd` — estimated cost of the invocation
- `model_id` — which model was invoked (e.g., `us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- `operation_type` — "invoke_agent" or "invoke_model"
- `trace_id` — unique identifier linking all spans for a request
- `timestamp` — ISO 8601 invocation time
- `duration_ms` — invocation latency in milliseconds

**Optional Risk/Policy Attributes (when enabled):**
- `hallucination_risk_score`, `composite_risk_score` — risk assessments
- `policy_decision`, `policy_reasoning` — policy enforcement details
- `shadow_comparison_result` — dual-invoke comparison data

For the complete schema definition, see [TeamWeave MCP Observatory Implementation](https://github.com/rajatarun/TeamWeave/blob/main/src/orchestrator/mcp_observatory.py).

### 3. Set Environment Variable

Configure the Lambda environment variable:
```
OBSERVATORY_METRICS_TABLE=ObservatoryMetrics
```

## IAM Permissions

Add these permissions to your Lambda execution role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/ObservatoryMetrics"
    },
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "*"
    }
  ]
}
```

## Implementation Details

### Wrapper Module (`src/observatory_wrapper.py`)

The `observatory_wrapper` module manages the lifecycle of the MCP Observatory wrapper:

- **Singleton pattern**: The wrapper is initialized once per Lambda process and reused for all invocations
- **Lazy initialization**: The wrapper is created on first use to minimize startup overhead
- **Non-blocking observability**: If telemetry collection fails, the actual API call still succeeds

### Instrumented Call (`src/bedrock_agent.py`)

The `run_agent()` function's Bedrock Converse API call is wrapped with the `@observe_bedrock_converse` decorator:

```python
@observe_bedrock_converse
def _call_bedrock_converse(client, **kwargs) -> Dict[str, Any]:
    """Wrap Bedrock converse call for observatory instrumentation."""
    return client.converse(**kwargs)
```

This decorator:
- Records request metadata (model ID, system prompt, tool config)
- Captures output tokens from the Bedrock response
- Logs telemetry to DynamoDB with automatic TTL-based cleanup

## Monitoring

### Verify Telemetry Collection

Query the DynamoDB table to verify telemetry is being recorded:

**List all agent invocations (most recent first):**
```bash
aws dynamodb query \
  --table-name ObservatoryMetrics \
  --key-condition-expression "pk = :pk" \
  --expression-attribute-values "{\":pk\":{\"S\":\"OBSERVATORY#invoke_agent\"}}" \
  --scan-index-forward false \
  --limit 10
```

**List all model invocations:**
```bash
aws dynamodb query \
  --table-name ObservatoryMetrics \
  --key-condition-expression "pk = :pk" \
  --expression-attribute-values "{\":pk\":{\"S\":\"OBSERVATORY#invoke_model\"}}" \
  --scan-index-forward false \
  --limit 10
```

**Scan entire table (for exploration):**
```bash
aws dynamodb scan --table-name ObservatoryMetrics --limit 10
```

### Optional: Amazon Managed Prometheus (AMP)

For real-time dashboard queries and monitoring, configure an Amazon Managed Prometheus workspace and export metrics from DynamoDB. Refer to the [main MCP Observatory documentation](https://github.com/rajatarun/TeamWeave/blob/main/docs/MCP_OBSERVATORY_IMPLEMENTATION.md) for details.

## Testing

Run the test suite to verify the instrumentation:

```bash
pytest src/tests/
```

The observatory wrapper is designed to be transparent — tests should pass with or without telemetry enabled.

## Troubleshooting

### Observatory wrapper not initializing

Check Lambda logs for:
- Missing `mcp-observatory` package (verify requirements.txt installation)
- Missing DynamoDB table or IAM permissions
- AWS region configuration (uses `AWS_REGION` environment variable)

Logs will show:
```
Failed to initialize observatory wrapper: <error_details>
```

This is a warning, not an error — the system continues to function without telemetry.

### DynamoDB write failures

If DynamoDB is unavailable:
- Check IAM permissions (see above)
- Verify table exists and is in ACTIVE state
- Check CloudWatch logs for detailed error messages

Failures are logged but do not propagate to callers, ensuring observability doesn't degrade application availability.

## References

- [TeamWeave MCP Observatory Implementation](https://github.com/rajatarun/TeamWeave/blob/main/src/orchestrator/mcp_observatory.py) — Reference implementation and schema definition
- [MCP Observatory Documentation](https://github.com/rajatarun/TeamWeave/blob/main/docs/MCP_OBSERVATORY_IMPLEMENTATION.md) — High-level adoption guide
- [DeviceWeave Observatory Instrumentation](src/../src/observatory_wrapper.py) — Local wrapper implementation
- [AWS Bedrock API Reference](https://docs.aws.amazon.com/bedrock/latest/userguide/)
- [DynamoDB Query API](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_Query.html)
