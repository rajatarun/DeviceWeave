"""
MCP Observatory instrumentation wrapper for Bedrock API calls.

This module initializes and manages the observatory wrapper for telemetry
collection. The wrapper is created once per Lambda process and reused for
all invocations to minimize overhead.

DynamoDB Schema (from TeamWeave reference implementation):
  Partition Key (pk): OBSERVATORY#{operation}
    - operation: "invoke_agent" | "invoke_model"
  Sort Key (sk): {iso_timestamp}#{trace_id}
    - Example: 2024-01-15T10:30:45.123Z#abc-def-ghi
  TTL: 90 days (automatic expiration)

See: https://github.com/rajatarun/TeamWeave/blob/main/src/orchestrator/mcp_observatory.py
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_wrapper = None


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


def observe_bedrock_converse(wrapped_func):
    """
    Decorator to instrument Bedrock Converse API calls with observatory telemetry.

    Wraps the bedrock-runtime converse call and records metrics to DynamoDB.
    If instrumentation fails, the call still succeeds (observability is non-blocking).
    """
    def wrapper(*args, **kwargs):
        wrapper_instance = get_wrapper()

        if wrapper_instance is None:
            # Observatory not configured, fall through to unwrapped call
            return wrapped_func(*args, **kwargs)

        try:
            # Use observe_agent_request for agentic workloads (tool use)
            with wrapper_instance.observe_agent_request(
                model_id=kwargs.get("modelId", "unknown"),
                input_tokens=None,
                request_params={
                    "system": kwargs.get("system"),
                    "toolConfig": kwargs.get("toolConfig"),
                    "inferenceConfig": kwargs.get("inferenceConfig"),
                }
            ) as obs:
                result = wrapped_func(*args, **kwargs)
                # Record output tokens if available
                if "usage" in result:
                    obs.output_tokens = result["usage"].get("outputTokens")
                return result
        except Exception as exc:
            logger.warning("Observatory observation failed: %s", exc)
            # Observability failure doesn't block the actual API call
            return wrapped_func(*args, **kwargs)

    return wrapper
