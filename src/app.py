"""
DeviceWeave Lambda handler.

Routes
------
  POST /execute   Natural-language device or scene command.
  POST /learn     Manually bind a phrase to a device.
  POST /presence  Update home-occupancy state (is_home: true/false).
  GET  /health    Liveness probe.
  GET  /devices   List registered devices (no IPs).
  GET  /scenes    List registered scenes.

POST /execute dispatch order
-----------------------------
  1. Scene resolution  — nearest-neighbour cosine, conf ≥ threshold.
  2. Tier 1 device     — TF cosine + learned phrases, scored via decision engine.
  3. Tier 2 device     — Claude Haiku 4.5 via Bedrock (contextual / behavioral).
  4. Policy Engine     — check resolved (device_type, action) against DynamoDB rules.
                         BLOCK  → 403 Forbidden (no device I/O).
                         MODIFY → updated params forwarded to execution.
                         ALLOW  → pass-through.
  5. Auto-learn        — successful resolutions written to learning table.
  6. Behavior record   — every successful execution recorded in Memgraph.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import behavior_engine
import decision_engine
import graph_engine
from device_resolver import (
    _get_active_catalog,
    device_public_view,
    invalidate_device_registry_cache,
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
from policy_engine.middleware import enforce as policy_enforce, filter_steps as policy_filter_steps
from policy_engine.context_provider import get_context as get_policy_context

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
    path_params: Dict[str, str] = event.get("pathParameters") or {}

    logger.debug("Request: %s %s", method, path)

    # Parameterized routes — must be checked before collection routes
    if path_params.get("device_id"):
        device_id = path_params["device_id"]
        if method == "GET":
            return _route_get_device(device_id)
        if method == "PUT":
            return _route_update_device(device_id, event)
        if method == "DELETE":
            return _route_delete_device(device_id)

    if method == "GET" and path.endswith("/health"):
        return _route_health()
    if method == "GET" and path.endswith("/devices"):
        return _route_devices()
    if method == "POST" and path.endswith("/devices"):
        return _route_create_device(event)
    if method == "GET" and path.endswith("/scenes"):
        return _route_scenes()
    if method == "GET" and path.endswith("/learnings"):
        return _route_list_learnings()
    if method == "DELETE" and path.endswith("/learnings"):
        return _route_delete_learning(event)
    if method == "GET" and path.endswith("/presence"):
        return _route_get_presence()
    if method == "POST" and path.endswith("/learn"):
        return _route_learn(event)
    if method == "POST" and path.endswith("/presence"):
        return _route_presence(event)
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


def _route_presence(event: Dict[str, Any]) -> Dict[str, Any]:
    """POST /presence — update home-occupancy state used by the Policy Engine."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    if "is_home" not in body:
        return _error(400, "Missing 'is_home' field (boolean).")

    is_home = body["is_home"]
    if not isinstance(is_home, bool):
        return _error(400, "'is_home' must be a boolean (true or false).")

    table_name = os.environ.get("PRESENCE_TABLE_NAME", "")
    if not table_name:
        return _error(503, "Presence store not configured (PRESENCE_TABLE_NAME not set).")

    now = datetime.now(timezone.utc).isoformat()
    try:
        import boto3
        table = boto3.resource("dynamodb").Table(table_name)
        table.put_item(Item={"pk": "home_state", "is_home": is_home, "updated_at": now})
    except Exception as exc:
        logger.error("Presence update failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to update presence state: {exc}")

    logger.info("Presence updated: is_home=%s", is_home)
    return _ok({"is_home": is_home, "updated_at": now})


def _route_get_device(device_id: str) -> Dict[str, Any]:
    """GET /devices/{device_id} — single device detail."""
    catalog = _get_active_catalog()
    device = next((d for d in catalog if d["id"] == device_id), None)
    if device is None:
        return _error(404, f"Device '{device_id}' not found.")
    return _ok(device_public_view(device))


def _route_create_device(event: Dict[str, Any]) -> Dict[str, Any]:
    """POST /devices — manually register a device."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    device_id = (body.get("device_id") or "").strip()
    name = (body.get("name") or "").strip()
    device_type = (body.get("device_type") or "SmartPlug").strip()
    capabilities = body.get("capabilities") or ["turn_on", "turn_off", "get_status"]
    ip = (body.get("ip") or "").strip()
    model = (body.get("model") or "manual").strip()

    if not device_id:
        return _error(400, "Missing 'device_id' field.")
    if not name:
        return _error(400, "Missing 'name' field.")

    known_ids = {d["id"] for d in _get_active_catalog()}
    if device_id in known_ids:
        return _error(409, f"Device '{device_id}' already exists.")

    registry_table = os.environ.get("DEVICE_REGISTRY_TABLE", "")
    if not registry_table:
        return _error(503, "Device registry not configured.")

    now = datetime.now(timezone.utc).isoformat()
    try:
        import boto3
        table = boto3.resource("dynamodb").Table(registry_table)
        table.put_item(Item={
            "device_id": device_id,
            "provider": "manual",
            "name": name,
            "device_type": device_type,
            "capabilities": capabilities,
            "ip": ip,
            "model": model,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        })
        invalidate_device_registry_cache()
    except Exception as exc:
        logger.error("Create device failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to create device: {exc}")

    return {
        "statusCode": 201,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "device_id": device_id,
            "name": name,
            "device_type": device_type,
            "capabilities": capabilities,
            "ip": ip,
            "model": model,
            "status": "active",
            "created_at": now,
        }),
    }


def _route_update_device(device_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """PUT /devices/{device_id} — update mutable device fields."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    registry_table = os.environ.get("DEVICE_REGISTRY_TABLE", "")
    if not registry_table:
        return _error(503, "Device registry not configured.")

    import boto3
    from boto3.dynamodb.conditions import Key

    try:
        table = boto3.resource("dynamodb").Table(registry_table)
        resp = table.query(KeyConditionExpression=Key("device_id").eq(device_id))
        items = resp.get("Items", [])
    except Exception as exc:
        logger.error("Update device query failed: %s", exc, exc_info=True)
        return _error(500, f"Registry query failed: {exc}")

    if not items:
        return _error(404, f"Device '{device_id}' not found.")

    allowed = {"name", "capabilities", "ip", "device_type", "model"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return _error(400, f"No updatable fields. Allowed: {sorted(allowed)}.")

    now = datetime.now(timezone.utc).isoformat()
    expr_names: Dict[str, str] = {"#ua": "updated_at"}
    expr_values: Dict[str, Any] = {":ua": now}
    set_parts = ["#ua = :ua"]

    if "name" in updates:
        expr_names["#n"] = "name"
        expr_values[":n"] = updates["name"]
        set_parts.append("#n = :n")
    for field in ("capabilities", "ip", "device_type", "model"):
        if field in updates:
            expr_values[f":{field}"] = updates[field]
            set_parts.append(f"{field} = :{field}")

    update_expr = "SET " + ", ".join(set_parts)
    try:
        for item in items:
            table.update_item(
                Key={"device_id": device_id, "provider": item["provider"]},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
        invalidate_device_registry_cache()
    except Exception as exc:
        logger.error("Update device failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to update device: {exc}")

    return _ok({"device_id": device_id, "updated": list(updates.keys()), "updated_at": now})


def _route_delete_device(device_id: str) -> Dict[str, Any]:
    """DELETE /devices/{device_id} — deactivate a device (status → inactive)."""
    registry_table = os.environ.get("DEVICE_REGISTRY_TABLE", "")
    if not registry_table:
        return _error(503, "Device registry not configured.")

    import boto3
    from boto3.dynamodb.conditions import Key

    try:
        table = boto3.resource("dynamodb").Table(registry_table)
        resp = table.query(KeyConditionExpression=Key("device_id").eq(device_id))
        items = resp.get("Items", [])
    except Exception as exc:
        logger.error("Delete device query failed: %s", exc, exc_info=True)
        return _error(500, f"Registry query failed: {exc}")

    if not items:
        return _error(404, f"Device '{device_id}' not found.")

    now = datetime.now(timezone.utc).isoformat()
    try:
        for item in items:
            table.update_item(
                Key={"device_id": device_id, "provider": item["provider"]},
                UpdateExpression="SET #s = :s, updated_at = :ua",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "inactive", ":ua": now},
            )
        invalidate_device_registry_cache()
    except Exception as exc:
        logger.error("Delete device failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to deactivate device: {exc}")

    return _ok({"device_id": device_id, "status": "inactive", "updated_at": now})


def _route_list_learnings() -> Dict[str, Any]:
    """GET /learnings — list all phrase→device learnings with metadata."""
    table_name = os.environ.get("LEARNING_TABLE_NAME", "")
    if not table_name:
        return _error(503, "Learning store not configured.")

    try:
        import boto3
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.scan()
        items = sorted(
            resp.get("Items", []),
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )
        return _ok({
            "learnings": [
                {
                    "device_id": item["device_id"],
                    "phrase": item["phrase"],
                    "source": item.get("source", "learned"),
                    "confidence": float(item.get("confidence", 0)),
                    "use_count": int(item.get("use_count", 0)),
                    "created_at": item.get("created_at", ""),
                }
                for item in items
            ],
            "count": len(items),
        })
    except Exception as exc:
        logger.error("List learnings failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to list learnings: {exc}")


def _route_delete_learning(event: Dict[str, Any]) -> Dict[str, Any]:
    """DELETE /learnings — remove a specific phrase→device binding."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    device_id = (body.get("device_id") or "").strip()
    phrase = (body.get("phrase") or "").strip()
    if not device_id:
        return _error(400, "Missing 'device_id' field.")
    if not phrase:
        return _error(400, "Missing 'phrase' field.")

    table_name = os.environ.get("LEARNING_TABLE_NAME", "")
    if not table_name:
        return _error(503, "Learning store not configured.")

    try:
        import boto3
        table = boto3.resource("dynamodb").Table(table_name)
        table.delete_item(Key={"device_id": device_id, "phrase": phrase})
        invalidate_learned_phrases_cache()
    except Exception as exc:
        logger.error("Delete learning failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to delete learning: {exc}")

    return _ok({"device_id": device_id, "phrase": phrase, "deleted": True})


def _route_get_presence() -> Dict[str, Any]:
    """GET /presence — current home-occupancy state."""
    table_name = os.environ.get("PRESENCE_TABLE_NAME", "")
    if not table_name:
        return _error(503, "Presence store not configured.")

    try:
        import boto3
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.get_item(Key={"pk": "home_state"})
        item = resp.get("Item")
        if item is None:
            return _ok({"is_home": True, "updated_at": None})
        return _ok({"is_home": item.get("is_home", True), "updated_at": item.get("updated_at")})
    except Exception as exc:
        logger.error("Get presence failed: %s", exc, exc_info=True)
        return _error(500, f"Failed to get presence state: {exc}")


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

    # Policy Engine — filter out steps that are blocked by an active policy.
    policy_ctx = get_policy_context()
    allowed_steps, policy_blocks = policy_filter_steps(steps, context=policy_ctx)

    if policy_blocks:
        logger.info(
            "Scene '%s': %d step(s) blocked by policy: %s",
            scene["id"],
            len(policy_blocks),
            [(b["device_id"], b["reason"]) for b in policy_blocks],
        )

    if not allowed_steps:
        return _error(
            403,
            f"All steps in scene '{scene['id']}' were blocked by active policies.",
            extra={"policy_blocks": policy_blocks},
        )

    results: List[StepResult] = asyncio.run(execute_steps(allowed_steps))
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    for result in successes:
        if is_configured() and confidence >= LEARNING_THRESHOLD:
            save_learned_phrase(result.device_id, original_command, confidence)
        graph_engine.record_event(result.device_id, result.action, original_command)

    resp: Dict[str, Any] = {
        "type": "scene",
        "scene_id": scene["id"],
        "scene_name": scene["name"],
        "confidence": confidence,
        "results": [_step_result_dict(r) for r in results],
        "succeeded": len(successes),
        "failed": len(failures),
    }
    if policy_blocks:
        resp["policy_blocks"] = policy_blocks
    return _ok(resp)


def _execute_llm_devices(
    steps: List[ExecutionStep],
    original_command: str,
    confidence: float,
    reasoning: str,
    cosine_score: float,
    policy_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute multiple devices returned by the LLM resolver concurrently."""
    # Policy Engine — filter blocked steps before any I/O.
    allowed_steps, policy_blocks = policy_filter_steps(steps, context=policy_ctx)

    if policy_blocks:
        logger.info(
            "LLM multi-device: %d step(s) blocked by policy: %s",
            len(policy_blocks),
            [(b["device_id"], b["reason"]) for b in policy_blocks],
        )

    if not allowed_steps:
        return _error(
            403,
            "All resolved devices were blocked by active policies.",
            extra={"policy_blocks": policy_blocks},
        )

    try:
        results: List[StepResult] = asyncio.run(execute_steps(allowed_steps))
    except Exception as exc:
        logger.exception("LLM multi-device execution error")
        return _error(502, f"Device execution error: {exc}")

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    for result in successes:
        if is_configured() and confidence >= LEARNING_THRESHOLD:
            save_learned_phrase(result.device_id, original_command, confidence)
        graph_engine.record_event(result.device_id, result.action, original_command)

    resp: Dict[str, Any] = {
        "type": "multi_device",
        "resolution_tier": "llm",
        "confidence": confidence,
        "reasoning": reasoning,
        "scores": {"cosine": cosine_score, "final": confidence},
        "results": [_step_result_dict(r) for r in results],
        "succeeded": len(successes),
        "failed": len(failures),
    }
    if policy_blocks:
        resp["policy_blocks"] = policy_blocks
    return _ok(resp)


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

    # Fetch policy context once — shared across Tier 1, Tier 2, and any
    # multi-device execution so weather/presence I/O happens once per request.
    policy_ctx = get_policy_context()

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
            policy_ctx=policy_ctx,
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
                    policy_ctx=policy_ctx,
                )
            # Multiple devices — execute concurrently like a scene
            return _execute_llm_devices(
                steps, normalized_command, llm_conf,
                llm_result.get("reasoning", ""),
                cosine_score,
                policy_ctx=policy_ctx,
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
    policy_ctx: Optional[Dict[str, Any]] = None,
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

    # ------------------------------------------------------------------
    # Policy Engine — enforce before any device I/O
    # ------------------------------------------------------------------
    policy_decision = policy_enforce(
        device["device_type"], action, params, context=policy_ctx
    )
    if policy_decision.is_blocked:
        logger.info(
            "Policy BLOCK: device=%s action=%s rule_id=%s reason=%r",
            device["id"], action, policy_decision.rule_id, policy_decision.reason,
        )
        return _error(
            403,
            f"Policy blocked: {policy_decision.reason}",
            extra={
                "device_id": device["id"],
                "device_name": device["name"],
                "action": action,
                "rule_id": policy_decision.rule_id,
            },
        )
    if policy_decision.is_modified:
        logger.info(
            "Policy MODIFY: device=%s action=%s rule_id=%s new_params=%s",
            device["id"], action, policy_decision.rule_id, policy_decision.modified_params,
        )
        params = policy_decision.modified_params or {}

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
    if policy_decision.is_modified:
        response["policy"] = {
            "verdict": "modify",
            "rule_id": policy_decision.rule_id,
            "reason": policy_decision.reason,
        }
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
