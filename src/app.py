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
  1. Scene resolution  — nearest-neighbour cosine, conf ≥ threshold.
  2. Tier 1 device     — TF cosine + learned phrases, conf ≥ threshold.
  3. Tier 2 device     — Claude Haiku 4.5 via Bedrock (contextual inference).
  4. Auto-learn        — successful resolutions written to learning table.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from device_resolver import (
    _get_active_catalog,
    device_public_view,
    invalidate_learned_phrases_cache,
    resolve_device,
)
from llm_resolver import llm_resolve
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
logging.getLogger().setLevel(LOG_LEVEL)  # basicConfig is a no-op in Lambda
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.4"))


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
    catalog = _get_active_catalog()
    return _ok({
        "status": "healthy",
        "devices": len(catalog),
        "scenes": len(SCENE_CATALOG),
        "learning_enabled": is_configured(),
    })


def _route_devices() -> Dict[str, Any]:
    catalog = _get_active_catalog()
    return _ok({
        "devices": [device_public_view(d) for d in catalog],
        "count": len(catalog),
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
    known_ids = {d["id"] for d in _get_active_catalog()}
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

    logger.info("Intent: action=%s query=%r params=%s", intent.action, intent.device_query, intent.params)

    # ------------------------------------------------------------------
    # Tier 1 — TF cosine resolver
    # ------------------------------------------------------------------
    device, confidence = resolve_device(intent.device_query)

    if device is None:
        return _error(503, "Device catalog is empty.")

    logger.info("Tier 1: device=%s conf=%.4f", device["id"], confidence)

    if confidence >= CONFIDENCE_THRESHOLD:
        return _execute_device(
            device, intent.action, intent.params,
            normalized_command, confidence, tier="cosine",
        )

    # ------------------------------------------------------------------
    # Tier 2 — LLM contextual inference (Haiku 4.5 via Bedrock)
    # ------------------------------------------------------------------
    logger.info("Tier 1 miss (%.4f < %.2f) — invoking LLM resolver.", confidence, CONFIDENCE_THRESHOLD)

    llm_result = llm_resolve(normalized_command, intent.action, _get_active_catalog())

    if llm_result and llm_result.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
        catalog = _get_active_catalog()
        llm_device = next((d for d in catalog if d["id"] == llm_result.get("device_id")), None)
        if llm_device:
            return _execute_device(
                llm_device,
                llm_result["action"],
                llm_result.get("params") or {},
                normalized_command,
                llm_result["confidence"],
                tier="llm",
                reasoning=llm_result.get("reasoning", ""),
            )

    # ------------------------------------------------------------------
    # Both tiers failed
    # ------------------------------------------------------------------
    return _error(
        422,
        f"Could not resolve command with sufficient confidence "
        f"(cosine={confidence:.4f}, threshold={CONFIDENCE_THRESHOLD}). "
        f"Closest cosine match: '{device['name']}'.",
        extra={
            "best_match_id": device["id"],
            "cosine_confidence": confidence,
            "threshold": CONFIDENCE_THRESHOLD,
            "hint": "Use POST /learn to add new phrases for a device.",
        },
    )


def _execute_device(
    device: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
    original_command: str,
    confidence: float,
    tier: str,
    reasoning: str = "",
) -> Dict[str, Any]:
    # --- Capability check ---
    if action not in device["capabilities"]:
        return _error(
            422,
            f"'{device['name']}' does not support '{action}'.",
            extra={"supported": device["capabilities"]},
        )

    # --- Parameter check ---
    if action == "set_brightness" and "brightness" not in params:
        return _error(400, "set_brightness requires a brightness value, e.g. 'set brightness to 75%'.")

    # --- Execute ---
    steps = plan_device_execution(device, action, params)
    try:
        results: List[StepResult] = asyncio.run(execute_steps(steps))
    except Exception as exc:
        logger.exception("Execution error")
        return _error(502, f"Device execution error: {exc}")

    result = results[0]
    if not result.success:
        return _error(502, result.error, extra={"device_id": device["id"]})

    logger.info("Executed via %s tier — %s/%s → %s", tier, device["id"], action, result.result)

    # --- Auto-learn ---
    if is_configured() and confidence >= LEARNING_THRESHOLD:
        phrase = original_command if tier == "llm" else original_command
        save_learned_phrase(device["id"], phrase, confidence)
        if tier == "llm":
            invalidate_learned_phrases_cache()

    response = {
        "type": "device",
        "device_id": device["id"],
        "device_name": device["name"],
        "action": action,
        "confidence": confidence,
        "resolution_tier": tier,
        "result": result.result,
    }
    if reasoning:
        response["reasoning"] = reasoning
    return _ok(response)


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
