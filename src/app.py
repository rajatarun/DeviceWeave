"""
DeviceWeave Lambda handler.

Flow per request:
  1. Parse + validate JSON body.
  2. Deterministic intent parsing  (intent_parser).
  3. Cosine-similarity device resolution  (device_resolver).
  4. Safety layer  — capability check + confidence threshold.
  5. Kasa LAN execution  (kasa_provider).
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict

from device_resolver import resolve_device
from intent_parser import Intent, parse_intent
from kasa_provider import execute_device_command

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# Minimum cosine similarity accepted as a device match.
CONFIDENCE_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler.

    Accepts:
        POST /execute  { "command": "<natural language string>" }
        GET  /health   (no body required)

    Returns HTTP-shaped responses: { statusCode, body }.
    """
    method = (event.get("requestContext", {})
                   .get("http", {})
                   .get("method", "POST"))

    if method == "GET":
        return _ok({"status": "healthy"})

    # --- Parse body ---
    raw_body = event.get("body") or ""
    try:
        body: Dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.")

    command: str = body.get("command", "").strip()
    if not command:
        return _error(400, "Missing or empty 'command' field in request body.")

    logger.info("Received command: %s", command)

    # --- Intent parsing ---
    try:
        intent: Intent = parse_intent(command)
    except ValueError as exc:
        return _error(400, str(exc))

    logger.info("Intent resolved — action=%s device_query=%s params=%s",
                intent.action, intent.device_query, intent.params)

    # --- Device resolution ---
    device, confidence = resolve_device(intent.device_query)

    if device is None:
        return _error(503, "Device catalog is empty.")

    logger.info("Device resolved — id=%s confidence=%.4f", device["id"], confidence)

    if confidence < CONFIDENCE_THRESHOLD:
        return _error(
            422,
            f"No device matched with sufficient confidence "
            f"(best={confidence:.4f}, threshold={CONFIDENCE_THRESHOLD}). "
            f"Closest candidate: '{device['name']}'.",
            extra={
                "best_match": device["id"],
                "confidence": confidence,
                "threshold": CONFIDENCE_THRESHOLD,
            },
        )

    # --- Capability check (safety layer) ---
    if intent.action not in device["capabilities"]:
        return _error(
            422,
            f"Device '{device['name']}' does not support action '{intent.action}'.",
            extra={"supported_capabilities": device["capabilities"]},
        )

    # --- Parameter validation for set_brightness ---
    if intent.action == "set_brightness" and "brightness" not in intent.params:
        return _error(
            400,
            "set_brightness requires a brightness value (e.g. 'set brightness to 75%').",
        )

    # --- Kasa execution ---
    try:
        result = asyncio.run(
            execute_device_command(device, intent.action, intent.params)
        )
    except ValueError as exc:
        return _error(422, str(exc))
    except Exception as exc:
        logger.exception("Kasa execution failed for device %s", device["id"])
        return _error(
            502,
            f"Device communication error: {exc}",
            extra={"device_id": device["id"], "device_ip": device["ip"]},
        )

    logger.info("Execution complete — device=%s action=%s result=%s",
                device["id"], intent.action, result)

    return _ok({
        "device_id": device["id"],
        "device_name": device["name"],
        "action": intent.action,
        "confidence": confidence,
        "result": result,
    })


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error(
    status: int,
    message: str,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"error": message}
    if extra:
        body.update(extra)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
