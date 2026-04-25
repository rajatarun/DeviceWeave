"""
Policy Authoring Lambda handler.

Routes
------
  POST   /policies/author         Compile NL rule → DSL → validate → store
  GET    /policies                List active policies (filter: ?device_type=fan)
  GET    /policies/{rule_id}      Get policy by ID (optional: ?version=2)
  DELETE /policies/{rule_id}      Deactivate a policy

Request / response contract
----------------------------
  POST /policies/author
    Body:   {"rule": "<natural language automation rule>"}
    201:    Full policy document with rule_id, version, conditions, …
    422:    Rejected (ambiguous, low-confidence, schema violation)
    502:    LLM compiler infrastructure failure

  GET /policies
    Query:  device_type=fan|light|ac|plug|heater  (optional)
            limit=<1-100>  (optional, default 50)
    200:    {"policies": [...], "count": N}

  GET /policies/{rule_id}
    Query:  version=<int>  (optional, defaults to latest)
    200:    policy document
    404:    not found

  DELETE /policies/{rule_id}
    200:    {"rule_id": "…", "status": "inactive"}
    404:    not found / already inactive

Pipeline for POST /policies/author
-----------------------------------
  1. Parse + validate request body (no LLM involved)
  2. LLM compiler  → raw Policy DSL dict or None (502 if None)
  3. Validator     → clean policy dict or ValidationError (422 if error)
  4. Policy store  → DynamoDB write → stored item
  5. Return 201 with the stored policy
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from policy_authoring.llm_compiler import compile_rule
from policy_authoring.policy_store import (
    deactivate_policy,
    get_policy,
    is_configured,
    list_policies,
    save_policy,
)
from policy_authoring.validator import (
    CONFIDENCE_THRESHOLD,
    ValidationError,
    validate_policy,
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.getLogger().setLevel(LOG_LEVEL)
for _noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    ctx = event.get("requestContext", {}).get("http", {})
    method: str = ctx.get("method", "POST").upper()
    path: str = ctx.get("path", "/policies/author")

    logger.debug("Policy authoring request: %s %s", method, path)

    if not is_configured():
        return _error(503, "Policy store is not configured (POLICY_TABLE_NAME not set).")

    # POST /policies/author
    if method == "POST" and path.endswith("/policies/author"):
        return _route_author(event)

    # GET /policies  (exact match)
    if method == "GET" and (path.rstrip("/").endswith("/policies")):
        # Make sure it's not /policies/{rule_id}
        segments = [s for s in path.strip("/").split("/") if s]
        if segments and segments[-1] == "policies":
            return _route_list(event)

    # GET /policies/{rule_id}  or  DELETE /policies/{rule_id}
    if "/policies/" in path:
        segments = [s for s in path.strip("/").split("/") if s]
        # segments: ["policies", "<rule_id>"]  (stage prefix already stripped by APIGW)
        policy_idx = next(
            (i for i, s in enumerate(segments) if s == "policies"), None
        )
        if policy_idx is not None and policy_idx + 1 < len(segments):
            rule_id = segments[policy_idx + 1]
            if method == "GET":
                return _route_get(rule_id, event)
            if method == "DELETE":
                return _route_delete(rule_id)

    return _error(404, f"No policy route for {method} {path}.")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _route_author(event: Dict[str, Any]) -> Dict[str, Any]:
    """POST /policies/author — compile, validate, and store a NL rule."""
    body = _parse_body(event)
    if body is None:
        return _error(400, "Request body is not valid JSON.")

    rule_text: str = (body.get("rule") or "").strip()
    if not rule_text:
        return _error(400, "Missing or empty 'rule' field in request body.")

    logger.info("Policy authoring request: rule=%r", rule_text)

    # --- Step 1: LLM compilation -----------------------------------------------
    raw = compile_rule(rule_text)
    if raw is None:
        logger.error("LLM compiler returned None for rule=%r", rule_text)
        return _error(
            502,
            "LLM compiler failed to process the rule due to an infrastructure error. "
            "Please retry in a moment.",
        )

    # --- Step 2: Deterministic validation --------------------------------------
    try:
        validated = validate_policy(raw)
    except ValidationError as exc:
        logger.info("Policy rejected for rule=%r: %s", rule_text, exc)
        return _error(
            422,
            str(exc),
            extra={
                "rule": rule_text,
                "rejection_stage": "validation",
                "confidence_threshold": CONFIDENCE_THRESHOLD,
                "llm_raw_output": raw,
            },
        )

    # --- Step 3: DynamoDB persistence ------------------------------------------
    try:
        stored = save_policy(validated["rule_id"], validated, rule_text)
    except Exception as exc:
        logger.error("Policy store write failed for rule=%r: %s", rule_text, exc, exc_info=True)
        return _error(500, f"Failed to persist policy to storage: {exc}")

    logger.info(
        "Policy authored successfully: rule_id=%s version=%d device_type=%s confidence=%s",
        stored["rule_id"], stored["version"], stored["device_type"], stored["confidence"],
    )

    return {
        "statusCode": 201,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(_policy_view(stored)),
    }


def _route_list(event: Dict[str, Any]) -> Dict[str, Any]:
    """GET /policies — list active policies, optionally filtered by device_type."""
    params = event.get("queryStringParameters") or {}
    device_type = params.get("device_type") or None
    limit_str = params.get("limit", "50")
    limit = min(int(limit_str) if limit_str.isdigit() else 50, 100)

    items = list_policies(device_type=device_type, limit=limit)

    return _ok({
        "policies": [_policy_view(p) for p in items],
        "count": len(items),
        **({"filter": {"device_type": device_type}} if device_type else {}),
    })


def _route_get(rule_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """GET /policies/{rule_id} — retrieve a specific policy (latest or versioned)."""
    params = event.get("queryStringParameters") or {}
    version_str = params.get("version")
    version = int(version_str) if version_str and version_str.isdigit() else None

    item = get_policy(rule_id, version)
    if not item:
        suffix = f" (version {version})" if version is not None else ""
        return _error(404, f"Policy '{rule_id}'{suffix} not found.")

    return _ok(_policy_view(item))


def _route_delete(rule_id: str) -> Dict[str, Any]:
    """DELETE /policies/{rule_id} — deactivate a policy."""
    success = deactivate_policy(rule_id)
    if not success:
        return _error(
            404,
            f"Policy '{rule_id}' not found or is already inactive.",
        )
    logger.info("Policy deactivated: rule_id=%s", rule_id)
    return _ok({"rule_id": rule_id, "status": "inactive"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return a clean, client-facing representation of a DynamoDB policy item."""
    return {
        "rule_id": item["rule_id"],
        "version": int(item["version"]),
        "scope": item["scope"],
        "conditions": item["conditions"],
        "action": item["action"],
        "confidence": float(item["confidence"]),
        "source_text": item.get("source_text", ""),
        "status": item.get("status", "active"),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
    }


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
