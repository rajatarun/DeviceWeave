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
  2. Tier 1 device     — TF cosine + learned phrases, scored via decision engine.
  3. Tier 2 device     — Claude Haiku 4.5 via Bedrock (contextual / behavioral).
  4. Auto-learn        — successful resolutions written to learning table.
  5. Behavior record   — every successful execution recorded in Memgraph.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

import behavior_engine
import decision_engine
import graph_engine
from device_resolver import (
    _get_active_catalog,
    device_public_view,
    invalidate_learned_phrases_cache,
    resolve_device,
)
from llm_resolver import llm_resolve
from execution_planner import (
    ExecutionStep,
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
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer", "neo4j"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.4"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
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
        "graph_enabled": graph_engine.is_available(),
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
    # 1. Scene resolution
    # ------------------------------------------------------------------
    scene, scene_conf = resolve_scene(normalized)
    if scene is not None and scene_conf >= CONFIDENCE_THRESHOLD:
        return _handle_scene(scene, scene_conf, normalized)

    # ------------------------------------------------------------------
    # 2. Single-device resolution
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

    for result in successes:
        if is_configured() and confidence >= LEARNING_THRESHOLD:
            save_learned_phrase(result.device_id, original_command, confidence)
        graph_engine.record_event(result.device_id, result.action, original_command)

    return _ok({
        "type": "scene",
        "scene_id": scene["id"],
        "scene_name": scene["name"],
        "confidence": confidence,
        "results": [_step_result_dict(r) for r in results],
        "succeeded": len(successes),
        "failed": len(failures),
    })


def _execute_llm_devices(
    steps: List[ExecutionStep],
    original_command: str,
    confidence: float,
    reasoning: str,
    cosine_score: float,
) -> Dict[str, Any]:
    """Execute multiple devices returned by the LLM resolver concurrently."""
    try:
        results: List[StepResult] = asyncio.run(execute_steps(steps))
    except Exception as exc:
        logger.exception("LLM multi-device execution error")
        return _error(502, f"Device execution error: {exc}")

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    for result in successes:
        if is_configured() and confidence >= LEARNING_THRESHOLD:
            save_learned_phrase(result.device_id, original_command, confidence)
        graph_engine.record_event(result.device_id, result.action, original_command)

    return _ok({
        "type": "multi_device",
        "resolution_tier": "llm",
        "confidence": confidence,
        "reasoning": reasoning,
        "scores": {"cosine": cosine_score, "final": confidence},
        "results": [_step_result_dict(r) for r in results],
        "succeeded": len(successes),
        "failed": len(failures),
    })


def _handle_device_command(normalized_command: str) -> Dict[str, Any]:
    try:
        intent: Intent = parse_intent(normalized_command)
    except ValueError as exc:
        return _error(400, str(exc))

    intent_type = decision_engine.classify_intent(normalized_command)
    logger.info(
        "Intent: action=%s query=%r type=%s params=%s",
        intent.action, intent.device_query, intent_type, intent.params,
    )

    ctx = behavior_engine.current_context()

    # ------------------------------------------------------------------
    # Tier 1 — TF cosine + behavior scoring via decision engine
    # ------------------------------------------------------------------
    device, cosine_score = resolve_device(intent.device_query)

    if device is None:
        return _error(503, "Device catalog is empty.")

    final_score, _, b_score = decision_engine.compute_score(
        cosine_score, device, intent.action, ctx
    )

    logger.info(
        "Tier 1: device=%s cosine=%.4f behavior=%.4f final=%.4f",
        device["id"], cosine_score, b_score, final_score,
    )

    if final_score >= CONFIDENCE_THRESHOLD:
        return _execute_device(
            device, intent.action, intent.params,
            normalized_command, final_score, tier="cosine",
            cosine_score=cosine_score, behavior_score=b_score,
        )

    # ------------------------------------------------------------------
    # Tier 2 — LLM contextual / behavioral inference
    # ------------------------------------------------------------------
    logger.info(
        "Tier 1 miss (%.4f < %.2f) — invoking LLM resolver (intent_type=%s).",
        final_score, CONFIDENCE_THRESHOLD, intent_type,
    )

    llm_result = llm_resolve(normalized_command, intent.action, _get_active_catalog())

    if llm_result and llm_result.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
        catalog_index = {d["id"]: d for d in _get_active_catalog()}
        llm_conf = llm_result["confidence"]

        steps: List[ExecutionStep] = []
        for spec in llm_result.get("devices", []):
            dev = catalog_index.get(spec.get("device_id"))
            if dev is None:
                continue
            action = spec.get("action", "")
            if action not in dev.get("capabilities", []):
                logger.warning(
                    "LLM suggested action %r not in capabilities for %s — skipping",
                    action, dev["id"],
                )
                continue
            steps.append(ExecutionStep(
                device=dev,
                action=action,
                params=spec.get("params") or {},
            ))

        if steps:
            # Single device — use the existing response format
            if len(steps) == 1:
                step = steps[0]
                _, _, llm_b_score = decision_engine.compute_score(
                    llm_conf, step.device, step.action, ctx
                )
                return _execute_device(
                    step.device, step.action, step.params,
                    normalized_command, llm_conf,
                    tier="llm",
                    reasoning=llm_result.get("reasoning", ""),
                    cosine_score=cosine_score,
                    behavior_score=llm_b_score,
                )
            # Multiple devices — execute concurrently like a scene
            return _execute_llm_devices(
                steps, normalized_command, llm_conf,
                llm_result.get("reasoning", ""),
                cosine_score,
            )

    # ------------------------------------------------------------------
    # Both tiers failed
    # ------------------------------------------------------------------
    return _error(
        422,
        f"Could not resolve command with sufficient confidence "
        f"(final={final_score:.4f}, threshold={CONFIDENCE_THRESHOLD}). "
        f"Closest cosine match: '{device['name']}'.",
        extra={
            "best_match_id": device["id"],
            "cosine_score": cosine_score,
            "behavior_score": b_score,
            "final_score": final_score,
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
    cosine_score: float = 0.0,
    behavior_score: float = 0.5,
) -> Dict[str, Any]:
    if action not in device["capabilities"]:
        return _error(
            422,
            f"'{device['name']}' does not support '{action}'.",
            extra={"supported": device["capabilities"]},
        )

    if action == "set_brightness" and "brightness" not in params:
        return _error(
            400,
            "set_brightness requires a brightness value, e.g. 'set brightness to 75%'.",
        )

    steps = plan_device_execution(device, action, params)
    try:
        results: List[StepResult] = asyncio.run(execute_steps(steps))
    except Exception as exc:
        logger.exception("Execution error")
        return _error(502, f"Device execution error: {exc}")

    result = results[0]
    if not result.success:
        return _error(502, result.error, extra={"device_id": device["id"]})

    logger.info(
        "Executed via %s tier — %s/%s → %s (final=%.4f cosine=%.4f behavior=%.4f)",
        tier, device["id"], action, result.result,
        confidence, cosine_score, behavior_score,
    )

    # Auto-learn
    if is_configured() and confidence >= LEARNING_THRESHOLD:
        save_learned_phrase(device["id"], original_command, confidence)
        if tier == "llm":
            invalidate_learned_phrases_cache()

    # Persist behavior event in Memgraph
    graph_engine.record_event(device["id"], action, original_command)

    response: Dict[str, Any] = {
        "type": "device",
        "device_id": device["id"],
        "device_name": device["name"],
        "action": action,
        "confidence": confidence,
        "resolution_tier": tier,
        "scores": {
            "cosine": cosine_score,
            "behavior": behavior_score,
            "final": confidence,
        },
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
