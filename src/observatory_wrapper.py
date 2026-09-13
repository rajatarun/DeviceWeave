"""
MCP Observatory instrumentation and gating wrapper for Bedrock API calls.

Instruments Bedrock Converse invocations with the mcp-observatory library,
capturing token usage, costs, and policy decisions to DynamoDB — and enforcing
the library's verdict on the model output before the caller can use it.

The wrapper is created once per Lambda process and reused for all invocations
to minimize overhead.  Telemetry failures are logged but never propagate.

Gating
------
The Bedrock call is executed *through* ``InvocationWrapperAPI.invoke()``, which
runs the call, records a span, and applies ``WrapperPolicy.decide()`` to the
output.  The resulting verdict is inspected, not discarded:

  - ``allow``  — the output is returned, annotated with ``gate_decision`` and
                 ``gate_reason`` so callers can see what cleared it.
  - ``review`` — a budget was exceeded / evidence was insufficient.  DeviceWeave
                 has no human-approval queue and the model output drives tool
                 calls that actuate physical devices, so an answer flagged for
                 review is refused: ``ObservatoryGateError`` is raised.
  - ``block``  — the output is unusable (e.g. empty).  Refused the same way.
  - no verdict / gate failure — treated as "not cleared" and refused too.  A
                 missing verdict is not evidence of a safe answer.

``ObservatoryGateError`` propagates out of ``bedrock_agent.run_agent()``, where
both callers already fail safe: ``app._route_execute_conversational()`` returns
502 without saving the session, and ``sms_handler`` replies with an error and
discards the turn.  Either way the agentic loop never reaches the tool-dispatch
step, so an ungated answer cannot actuate a device.

If mcp-observatory is not installed, fails to construct, or exposes no
``invoke()`` (an older release), behaviour is unchanged: the call runs
unwrapped or telemetry-only, with no gating and no crash.

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
  - gate_decision, gate_reason, composite_risk_score — the gate verdict and the
    risk evidence behind it (present only when the gate ran)

See: https://github.com/rajatarun/TeamWeave/blob/main/src/orchestrator/mcp_observatory.py
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3
from aws_clients import get_dynamodb_resource

logger = logging.getLogger(__name__)

_wrapper = None
_ddb_table = None

# Verdicts that mean "this output was not cleared for use".
_REFUSED_ACTIONS = ("block", "review")


class ObservatoryGateError(RuntimeError):
    """
    Raised when the observatory gate does not clear a Bedrock response.

    Attributes:
        gate_decision: The verdict — "block", "review", or "unavailable" when
                       the gate could not produce one.
        gate_reason:   Machine-readable cause (e.g. "empty_output",
                       "cost_budget_exceeded", "insufficient_evidence").
        gate_metadata: Supplementary data from the decision, if any.
    """

    def __init__(
        self,
        gate_decision: str,
        gate_reason: str = "",
        gate_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.gate_decision = gate_decision
        self.gate_reason = gate_reason or "unspecified"
        self.gate_metadata = gate_metadata or {}
        super().__init__(
            f"Bedrock response not cleared by the observatory gate "
            f"(decision={self.gate_decision}, reason={self.gate_reason})"
        )


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


def _build_policy():
    """
    Build the WrapperPolicy for gating, or None if the library predates it.

    The library's own defaults (0.25 USD / 8000 ms per invocation) are used
    unless OBSERVATORY_MAX_COST_USD / OBSERVATORY_MAX_LATENCY_MS are set.  They
    are configurable because exceeding either budget now *refuses* the answer:
    a deployment whose Converse rounds legitimately run past 8 s needs to raise
    the budget rather than lose the conversational path.
    """
    try:
        from mcp_observatory import WrapperPolicy
    except Exception:
        return None

    kwargs: Dict[str, Any] = {}
    for env_var, field in (
        ("OBSERVATORY_MAX_COST_USD", "max_cost_usd"),
        ("OBSERVATORY_MAX_LATENCY_MS", "max_latency_ms"),
    ):
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            continue
        try:
            kwargs[field] = float(raw)
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r", env_var, raw)

    return WrapperPolicy(**kwargs)


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

        kwargs: Dict[str, Any] = {}
        policy = _build_policy()
        if policy is not None:
            kwargs["policy"] = policy

        _wrapper = instrument_wrapper_api("deviceweave-bedrock", **kwargs)
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
            # SpanTimelineIndex partition key (contract v2.0.0, I6/I7). Sliced
            # from timestamp_str (already UTC — see datetime.utcnow() default
            # above) so the two attributes can never disagree.
            "span_date": timestamp_str[:10],
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

        # Gate verdict and the risk evidence behind it — present only when the
        # call went through the gating path.
        if span.get("gate_decision"):
            item["gate_decision"] = span["gate_decision"]
        if span.get("gate_reason"):
            item["gate_reason"] = span["gate_reason"]
        if span.get("composite_risk_score") is not None:
            item["composite_risk_score"] = Decimal(str(span["composite_risk_score"]))

        table.put_item(Item=item)
        logger.debug("Observatory metric written: pk=%s sk=%s", item["pk"], item["sk"])
    except Exception as exc:
        logger.warning("Failed to write Observatory metric: %s", exc)


def _span_to_metric(span: Any, action: str, reason: str) -> Dict[str, Any]:
    """Flatten an observatory TraceContext into the _push_metric span shape."""
    start = getattr(span, "start_time", None)
    end = getattr(span, "end_time", None)
    duration_ms = 0
    if start is not None and end is not None:
        duration_ms = round((end - start).total_seconds() * 1000, 3)

    return {
        "trace_id": getattr(span, "trace_id", "unknown") or "unknown",
        "timestamp": start.isoformat() if start is not None else datetime.utcnow().isoformat(),
        "prompt_tokens": getattr(span, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(span, "completion_tokens", 0) or 0,
        "duration_ms": duration_ms,
        "cost_usd": getattr(span, "cost_usd", 0) or 0,
        "gate_decision": action,
        "gate_reason": reason,
        "composite_risk_score": getattr(span, "composite_risk_score", None),
    }


def _prompt_text(call_kwargs: Dict[str, Any]) -> str:
    """Reconstruct the prompt text from Converse API kwargs for span hashing."""
    parts = []
    for block in call_kwargs.get("system") or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(block["text"])
    for message in call_kwargs.get("messages") or []:
        for block in (message.get("content") or []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(block["text"])
    return "\n".join(parts)


def _run_sync(make_coro):
    """
    Run a coroutine to completion from synchronous code.

    ``_call_bedrock_converse`` is normally reached through ``asyncio.to_thread``
    (bedrock_agent.run_agent), i.e. a worker thread with no event loop of its
    own, so ``asyncio.run`` applies directly.  If a loop *is* already running on
    this thread, the coroutine is driven on a short-lived thread instead of
    deadlocking it.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    box: Dict[str, Any] = {}

    def _runner():
        try:
            box["value"] = asyncio.run(make_coro())
        except BaseException as exc:  # re-raised unchanged on the caller thread
            box["error"] = exc

    thread = threading.Thread(target=_runner, name="observatory-gate")
    thread.start()
    thread.join()

    if "error" in box:
        raise box["error"]
    return box["value"]


def _observe_only(wrapped_func, args, kwargs, model_id: str, session_id: str) -> Any:
    """
    Telemetry-only path used when the wrapper exposes no decision API.

    This is DeviceWeave's pre-gate behaviour, kept verbatim so an older or
    incompatible mcp-observatory release degrades to what it always did:
    metrics, no gating, and never a failure of the underlying call.
    """
    try:
        result = wrapped_func(*args, **kwargs)

        usage = result.get("usage", {})
        span = {
            "trace_id": result.get("responseMetadata", {}).get("RequestId", "unknown"),
            "timestamp": datetime.utcnow().isoformat(),
            "prompt_tokens": usage.get("inputTokens", 0),
            "completion_tokens": usage.get("outputTokens", 0),
            "duration_ms": 0,  # Bedrock API doesn't provide this
            "cost_usd": 0,  # Placeholder; real cost calculation would go here
        }

        extra = {"model_id": model_id}
        if session_id:
            extra["session_id"] = session_id

        _push_metric("invoke_agent", span, extra)

        return result
    except Exception as exc:
        logger.warning("Observatory instrumentation failed: %s", exc)
        # Observability failure doesn't block the actual API call
        return wrapped_func(*args, **kwargs)


def observe_bedrock_converse(model_id: str = "unknown", session_id: str = ""):
    """
    Decorator to instrument *and gate* Bedrock Converse API calls.

    Args:
        model_id: The Bedrock model ID being invoked
        session_id: Optional session identifier for tracking

    The call is executed through the observatory wrapper, which records a span
    and returns a verdict.  On "allow" the response is returned with
    ``gate_decision`` / ``gate_reason`` attached; on "block", "review", or a
    missing verdict an ObservatoryGateError is raised so the caller cannot use
    an uncleared model answer to drive device actuation.  The telemetry span is
    written to DynamoDB in every case, refusals included.
    """
    def decorator(wrapped_func):
        def wrapper(*args, **kwargs):
            wrapper_instance = get_wrapper()

            if wrapper_instance is None:
                # Observatory not configured, fall through to unwrapped call
                return wrapped_func(*args, **kwargs)

            invoke = getattr(wrapper_instance, "invoke", None)
            if not callable(invoke):
                logger.warning(
                    "Observatory wrapper exposes no invoke() — telemetry only, no gating."
                )
                return _observe_only(wrapped_func, args, kwargs, model_id, session_id)

            extra = {"model_id": model_id}
            if session_id:
                extra["session_id"] = session_id

            # Distinguishes "the Bedrock call itself failed" (propagate the
            # original exception unchanged) from "the gate machinery failed".
            call_error: Dict[str, BaseException] = {}

            def _call():
                try:
                    return wrapped_func(*args, **kwargs)
                except BaseException as exc:
                    call_error["exc"] = exc
                    raise

            try:
                wrapper_result = _run_sync(lambda: invoke(
                    source="agent",
                    model=model_id,
                    prompt=_prompt_text(kwargs),
                    input_payload=kwargs,
                    call=_call,
                ))
            except Exception as exc:
                if call_error.get("exc") is exc:
                    raise
                # No verdict was produced, so nothing says this output is safe.
                logger.exception("Observatory gate failed to produce a verdict: %s", exc)
                raise ObservatoryGateError("unavailable", f"gate_error: {exc}") from exc

            decision = getattr(wrapper_result, "decision", None)
            action = str(getattr(decision, "action", "") or "").lower()
            reason = str(getattr(decision, "reason", "") or "")
            metadata = getattr(decision, "metadata", None) or {}
            result = getattr(wrapper_result, "output", None)

            # Telemetry first: a refused invocation is exactly the one worth
            # having a span for.
            _push_metric(
                "invoke_agent",
                _span_to_metric(getattr(wrapper_result, "span", None), action, reason),
                extra,
            )

            if action in _REFUSED_ACTIONS or not action:
                logger.error(
                    "Observatory gate refused Bedrock output: decision=%s reason=%s metadata=%s",
                    action or "unavailable", reason or "unspecified", json.dumps(metadata, default=str),
                )
                raise ObservatoryGateError(action or "unavailable", reason, metadata)

            if isinstance(result, dict):
                result["gate_decision"] = action
                result["gate_reason"] = reason

            return result

        return wrapper

    return decorator
