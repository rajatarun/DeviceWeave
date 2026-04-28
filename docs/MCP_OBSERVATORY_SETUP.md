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
- **Partition Key (pk)**: String
- **Sort Key (sk)**: String
- **TTL Attribute**: `ttl` (enables automatic cleanup of old metrics)

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

### 2. Set Environment Variable

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

- [MCP Observatory Documentation](https://github.com/rajatarun/TeamWeave/blob/main/docs/MCP_OBSERVATORY_IMPLEMENTATION.md)
- [AWS Bedrock API Reference](https://docs.aws.amazon.com/bedrock/latest/userguide/)
- [DynamoDB Developer Guide](https://docs.aws.amazon.com/dynamodb/)
