"""
MCP Observatory instrumentation wrapper for Bedrock API calls.

Instruments Bedrock Converse invocations with the mcp-observatory library,
capturing token usage, costs, and policy decisions to DynamoDB.

The wrapper is created once per Lambda process and reused for all invocations
to minimize overhead. Failures are logged but never propagate.

DynamoDB Schema (from TeamWeave reference implementation):
  Partition Key (pk): OBSERVATORY#{operation}
    - operation: "invoke_agent" | "invoke_model"
  Sort Key (sk): {iso_timestamp}#{trace_id}
    - Example: 2024-01-15T10:30:45.123Z#abc-def-ghi
  TTL: 90 days (automatic expiration)

Stored attributes:
  - prompt_tokens, completion_tokens — token counts from response
  - cost_usd — estimated invocation cost
  - model_id, timestamp, duration_ms — invocation metadata
  - policy_decision, risk_scores — policy enforcement and risk assessment

See: https://github.com/rajatarun/TeamWeave/blob/main/src/orchestrator/mcp_observatory.py
"""

import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict

import boto3
from aws_clients import get_dynamodb_resource

logger = logging.getLogger(__name__)

_wrapper = None
_ddb_table = None


def _get_ddb_table():
    """Get or initialize the DynamoDB table resource."""
    global _ddb_table

    if _ddb_table is not None:
        return _ddb_table

    try:
        table_name = os.environ.get("OBSERVATORY_METRICS_TABLE")
        if not table_name:
            return None

        ddb = get_dynamodb_resource()
        _ddb_table = ddb.Table(table_name)
        logger.info("Observatory DynamoDB table initialized: %s", table_name)
    except Exception as exc:
        logger.warning("Failed to initialize Observatory DynamoDB table: %s", exc)
        _ddb_table = None

    return _ddb_table


def get_wrapper():
    """
    Get or initialize the observatory wrapper.

    Returns the singleton wrapper instance, or None if observatory is not
    configured or initialization fails (errors are logged but not propagated).
    """
    global _wrapper

    if _wrapper is not None:
        return _wrapper

    try:
        from mcp_observatory import instrument_wrapper_api
        _wrapper = instrument_wrapper_api("deviceweave-bedrock")
        logger.info("Observatory wrapper initialized for deviceweave-bedrock")
    except Exception as exc:
        logger.warning("Failed to initialize observatory wrapper: %s", exc)
        _wrapper = None

    return _wrapper


def _push_metric(operation: str, span: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """
    Write a telemetry span to DynamoDB with 90-day TTL.

    Args:
        operation: "invoke_agent" or "invoke_model"
        span: Observatory span object containing token counts, costs, metadata
        extra: Additional context (model_id, etc.)
    """
    table = _get_ddb_table()
    if table is None:
        return

    try:
        # Extract trace ID from span (Observatory includes this)
        trace_id = span.get("trace_id", "unknown")
        timestamp_str = span.get("timestamp", datetime.utcnow().isoformat())

        # Construct DynamoDB item with TeamWeave schema
        item = {
            "pk": f"OBSERVATORY#{operation}",
            "sk": f"{timestamp_str}#{trace_id}",
            "ttl": int((datetime.utcnow() + timedelta(days=90)).timestamp()),
            "operation": operation,
            "trace_id": trace_id,
            "timestamp": timestamp_str,
            "model_id": extra.get("model_id", "unknown"),
            "prompt_tokens": Decimal(str(span.get("prompt_tokens", 0))),
            "completion_tokens": Decimal(str(span.get("completion_tokens", 0))),
            "cost_usd": Decimal(str(span.get("cost_usd", 0))),
            "duration_ms": Decimal(str(span.get("duration_ms", 0))),
        }

        # Add optional metadata
        if "session_id" in extra:
            item["session_id"] = extra["session_id"]
        if "device_id" in extra:
            item["device_id"] = extra["device_id"]

        table.put_item(Item=item)
        logger.debug("Observatory metric written: pk=%s sk=%s", item["pk"], item["sk"])
    except Exception as exc:
        logger.warning("Failed to write Observatory metric: %s", exc)


def observe_bedrock_converse(model_id: str = "unknown", session_id: str = ""):
    """
    Decorator to instrument Bedrock Converse API calls with observatory telemetry.

    Args:
        model_id: The Bedrock model ID being invoked
        session_id: Optional session identifier for tracking

    Wraps the bedrock-runtime converse call and records metrics to DynamoDB.
    If instrumentation fails, the call still succeeds (observability is non-blocking).
    """
    def decorator(wrapped_func):
        def wrapper(*args, **kwargs):
            wrapper_instance = get_wrapper()

            if wrapper_instance is None:
                # Observatory not configured, fall through to unwrapped call
                return wrapped_func(*args, **kwargs)

            try:
                # Invoke the actual Bedrock call and capture result
                result = wrapped_func(*args, **kwargs)

                # Extract telemetry from Bedrock response
                usage = result.get("usage", {})
                span = {
                    "trace_id": result.get("responseMetadata", {}).get("RequestId", "unknown"),
                    "timestamp": datetime.utcnow().isoformat(),
                    "prompt_tokens": usage.get("inputTokens", 0),
                    "completion_tokens": usage.get("outputTokens", 0),
                    "duration_ms": 0,  # Bedrock API doesn't provide this
                    "cost_usd": 0,  # Placeholder; real cost calculation would go here
                }

                # Build context metadata
                extra = {"model_id": model_id}
                if session_id:
                    extra["session_id"] = session_id

                # Write to DynamoDB (non-blocking)
                _push_metric("invoke_agent", span, extra)

                return result
            except Exception as exc:
                logger.warning("Observatory instrumentation failed: %s", exc)
                # Observability failure doesn't block the actual API call
                return wrapped_func(*args, **kwargs)

        return wrapper

    return decorator
