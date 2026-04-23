"""
DeviceWeave Lambda handler.

Routes
------
  POST /execute   Natural-language device or scene command.
  POST /learn     Manually bind a phrase to a device.
  GET  /health    Liveness probe.
  GET  /devices   List registered devices (no IPs).
  GET  /scenes    List registered scenes.

POST /execute dispatch order
-----------------------------
  1. Try scene resolution  → if confidence ≥ 0.70, execute scene.
  2. Parse device intent   → resolve device → safety checks → execute.
  3. After any successful execution: auto-learn phrase if conf ≥ 0.85.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from device_resolver import (
    DEVICE_CATALOG,
    device_public_view,
    invalidate_learned_phrases_cache,
    resolve_device,
)
from execution_planner import (
    StepResult,
    execute_steps,
    plan_device_execution,
    plan_scene_execution,
)
from intent_parser import Intent, parse_intent
from learning_store import (
    LEARNING_THRESHOLD,
    is_configured,
    save_learned_phrase,
    save_manual_phrase,
)
from scene_catalog import SCENE_CATALOG, resolve_scene, scene_public_view

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AWS Lambda handler — dispatches to route handlers."""
    ctx = event.get("requestContext", {}).get("http", {})
    method: str = ctx.get("method", "POST").upper()
    path: str = ctx.get("path", "/execute")

    logger.debug("Request: %s %s", method, path)

    if method == "GET" and path.endswith("/health"):
        return _route_health()

    if method == "GET" and path.endswith("/devices"):
        return _route_devices()

    if method == "GET" and path.endswith("/scenes"):
        return _route_scenes()

    if method == "POST" and path.endswith("/learn"):
        return _route_learn(event)

    if method == "POST" and path.endswith("/execute"):
        return _route_execute(event)

    return _error(404, f"No route for {method} {path}.")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _route_health() -> Dict[str, Any]:
    return _ok({
        "status": "healthy",
        "devices": len(DEVICE_CATALOG),
        "scenes": len(SCENE_CATALOG),
        "learning_enabled": is_configured(),
    })


def _route_devices() -> Dict[str, Any]:
    return _ok({
        "devices": [device_public_view(d) for d in DEVICE_CATALOG],
        "count": len(DEVICE_CATALOG),
    })


def _route_scenes() -> Dict[str, Any]:
    return _ok({
        "scenes": [scene_public_view(s) for s in SCENE_CATALOG],
        "count": len(SCENE_CATALOG),
    })


def _route_learn(event: Dict[str, Any]) -> Dict[str, Any]:
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    device_id: str = body.get("device_id", "").strip()
    phrase: str = body.get("phrase", "").strip().lower()

    if not device_id:
        return _error(400, "Missing 'device_id' field.")
    if not phrase:
        return _error(400, "Missing 'phrase' field.")

    # Validate device_id against catalog
    known_ids = {d["id"] for d in DEVICE_CATALOG}
    if device_id not in known_ids:
        return _error(
            422,
            f"Unknown device_id '{device_id}'. Known: {sorted(known_ids)}.",
        )

    if not is_configured():
        return _error(503, "Learning store not configured (LEARNING_TABLE_NAME not set).")

    saved = save_manual_phrase(device_id, phrase)
    if saved:
        invalidate_learned_phrases_cache()

    return _ok({
        "status": "learned",
        "device_id": device_id,
        "phrase": phrase,
        "persisted": saved,
    })


def _route_execute(event: Dict[str, Any]) -> Dict[str, Any]:
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    command: str = body.get("command", "").strip()
    if not command:
        return _error(400, "Missing or empty 'command' field.")

    logger.info("Command received: %s", command)
    normalized = command.lower()

    # ------------------------------------------------------------------
    # 1. Try scene resolution first
    # ------------------------------------------------------------------
    scene, scene_conf = resolve_scene(normalized)

    if scene is not None and scene_conf >= CONFIDENCE_THRESHOLD:
        return _handle_scene(scene, scene_conf, normalized)

    # ------------------------------------------------------------------
    # 2. Fall back to single-device resolution
    # ------------------------------------------------------------------
    return _handle_device_command(normalized)


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def _handle_scene(
    scene: Dict[str, Any],
    confidence: float,
    original_command: str,
) -> Dict[str, Any]:
    logger.info("Scene matched: %s (conf=%.4f)", scene["id"], confidence)

    steps = plan_scene_execution(scene)
    if not steps:
        return _error(422, f"Scene '{scene['id']}' produced no executable steps.")

    results: List[StepResult] = asyncio.run(execute_steps(steps))

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    # Auto-learn: scene commands are high-signal; always above learning threshold
    # as scene resolution already requires 0.70+ cosine similarity.
    if is_configured() and confidence >= LEARNING_THRESHOLD:
        for result in successes:
            save_learned_phrase(result.device_id, original_command, confidence)

    return _ok({
        "type": "scene",
        "scene_id": scene["id"],
        "scene_name": scene["name"],
        "confidence": confidence,
        "results": [_step_result_dict(r) for r in results],
        "succeeded": len(successes),
        "failed": len(failures),
    })


def _handle_device_command(normalized_command: str) -> Dict[str, Any]:
    # --- Intent parsing ---
    try:
        intent: Intent = parse_intent(normalized_command)
    except ValueError as exc:
        return _error(400, str(exc))

    logger.info(
        "Intent: action=%s query=%s params=%s",
        intent.action, intent.device_query, intent.params,
    )

    # --- Device resolution ---
    device, confidence = resolve_device(intent.device_query)

    if device is None:
        return _error(503, "Device catalog is empty.")

    logger.info("Device: id=%s conf=%.4f", device["id"], confidence)

    if confidence < CONFIDENCE_THRESHOLD:
        return _error(
            422,
            f"No device matched with sufficient confidence "
            f"(best={confidence:.4f}, threshold={CONFIDENCE_THRESHOLD}). "
            f"Closest: '{device['name']}'.",
            extra={
                "best_match_id": device["id"],
                "confidence": confidence,
                "threshold": CONFIDENCE_THRESHOLD,
                "hint": "Use POST /learn to add new phrases for a device.",
            },
        )

    # --- Capability check ---
    if intent.action not in device["capabilities"]:
        return _error(
            422,
            f"'{device['name']}' does not support '{intent.action}'.",
            extra={"supported": device["capabilities"]},
        )

    # --- Parameter check ---
    if intent.action == "set_brightness" and "brightness" not in intent.params:
        return _error(
            400,
            "set_brightness requires a brightness value, e.g. 'set brightness to 75%'.",
        )

    # --- Execute ---
    steps = plan_device_execution(device, intent.action, intent.params)
    try:
        results: List[StepResult] = asyncio.run(execute_steps(steps))
    except Exception as exc:
        logger.exception("Execution error")
        return _error(502, f"Device execution error: {exc}")

    result = results[0]
    if not result.success:
        return _error(502, result.error, extra={"device_id": device["id"]})

    # --- Auto-learn ---
    if is_configured() and confidence >= LEARNING_THRESHOLD:
        save_learned_phrase(device["id"], intent.device_query, confidence)

    return _ok({
        "type": "device",
        "device_id": device["id"],
        "device_name": device["name"],
        "action": intent.action,
        "confidence": confidence,
        "result": result.result,
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _step_result_dict(r: StepResult) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "device_id": r.device_id,
        "device_name": r.device_name,
        "action": r.action,
        "success": r.success,
    }
    if r.success:
        d["result"] = r.result
    else:
        d["error"] = r.error
    return d


def _parse_body(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = event.get("body") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error(
    status: int,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"error": message}
    if extra:
        body.update(extra)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
