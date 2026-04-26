"""
Policy DSL validator — deterministic, post-LLM enforcement layer.

This module runs AFTER the LLM compiler and is entirely rule-based.
It does not perform any semantic interpretation — it only enforces structural
and type constraints defined by the Policy DSL schema.

Validation sequence (all checks must pass for a policy to be accepted):
  1.  Type check         — raw must be a dict
  2.  Rejection check    — LLM may have returned an explicit rejection object
  3.  Top-level whitelist — no fields outside the allowed set
  4.  Required fields    — rule_id, scope, conditions, action, confidence all present
  5.  scope.device_type  — must be in the allowed enum
  6.  conditions         — non-empty list
  7.  Per-condition      — field enum, operator enum, value type (numeric vs boolean)
  8.  action.type        — must be in the allowed enum
  9.  action.reason      — non-empty string
  10. action.params      — must be a dict if present
  11. confidence range   — float in [0.0, 1.0]
  12. confidence gate    — must meet CONFIDENCE_THRESHOLD (0.85)
  13. rule_id resolution — "auto" is replaced with a fresh UUID4
"""

import logging
import uuid
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Rules below this confidence threshold are rejected regardless of structure.
# Configurable via the module constant; the handler also exposes it in error
# responses so callers know exactly why a rule was rejected.
CONFIDENCE_THRESHOLD: float = 0.85

_ALLOWED_TOP_KEYS = frozenset({"rule_id", "scope", "conditions", "action", "confidence"})
_ALLOWED_DEVICE_TYPES = frozenset({"fan", "light", "ac", "plug", "heater"})
_ALLOWED_CONDITION_FIELDS = frozenset({
    "temperature", "humidity", "time_hour",
    "cloud_cover_pct", "is_home", "is_overcast",
})
_ALLOWED_OPERATORS = frozenset({">", "<", ">=", "<=", "==", "!="})
_ALLOWED_ACTION_TYPES = frozenset({"block", "allow", "modify"})

# Fields whose condition value must be numeric (int or float, not bool).
_NUMERIC_FIELDS = frozenset({"temperature", "humidity", "time_hour", "cloud_cover_pct"})

# Fields whose condition value must be a boolean.
_BOOLEAN_FIELDS = frozenset({"is_home", "is_overcast"})


class ValidationError(Exception):
    """Raised with a human-readable message when a policy fails any check."""


def validate_policy(raw: Any) -> Dict[str, Any]:
    """
    Validate and normalise a raw LLM-produced policy dict.

    Returns a clean, fully-validated policy dict with:
      - rule_id resolved (UUID4 string, never "auto")
      - confidence as a Python float
      - all other fields unchanged from the LLM output

    Raises ValidationError with a specific message on the first violation
    encountered.  The caller is responsible for converting this into an HTTP
    422 response and logging the rejection reason.
    """
    # --- 1. Type guard ---------------------------------------------------------
    if not isinstance(raw, dict):
        raise ValidationError("LLM output must be a JSON object.")

    # --- 2. Explicit LLM rejection ---------------------------------------------
    if raw.get("rejected") is True:
        reason = raw.get("reason") or "LLM could not compile the rule."
        raise ValidationError(f"Rule rejected by LLM compiler: {reason}")

    # --- 3. Top-level field whitelist ------------------------------------------
    extra_keys = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if extra_keys:
        raise ValidationError(
            f"Policy contains disallowed fields: {sorted(extra_keys)}. "
            f"Allowed top-level fields: {sorted(_ALLOWED_TOP_KEYS)}."
        )

    # --- 4. Required fields presence -------------------------------------------
    for field in ("rule_id", "scope", "conditions", "action", "confidence"):
        if field not in raw:
            raise ValidationError(f"Missing required field: '{field}'.")

    # --- 5. scope.device_type --------------------------------------------------
    scope = raw["scope"]
    if not isinstance(scope, dict):
        raise ValidationError("'scope' must be a JSON object.")
    if set(scope.keys()) != {"device_type"}:
        raise ValidationError(
            f"'scope' must contain exactly the field 'device_type', "
            f"got: {sorted(scope.keys())}."
        )
    device_type = scope.get("device_type")
    if device_type not in _ALLOWED_DEVICE_TYPES:
        raise ValidationError(
            f"Invalid scope.device_type '{device_type}'. "
            f"Allowed values: {sorted(_ALLOWED_DEVICE_TYPES)}."
        )

    # --- 6. conditions non-empty list ------------------------------------------
    conditions = raw["conditions"]
    if not isinstance(conditions, list) or len(conditions) == 0:
        raise ValidationError(
            "'conditions' must be a non-empty list. "
            "At least one condition is required."
        )

    # --- 7. Per-condition validation -------------------------------------------
    for idx, cond in enumerate(conditions):
        _validate_condition(cond, idx)

    # --- 8–10. action ----------------------------------------------------------
    action = raw["action"]
    if not isinstance(action, dict):
        raise ValidationError("'action' must be a JSON object.")

    action_type = action.get("type")
    if action_type not in _ALLOWED_ACTION_TYPES:
        raise ValidationError(
            f"Invalid action.type '{action_type}'. "
            f"Allowed values: {sorted(_ALLOWED_ACTION_TYPES)}."
        )

    action_reason = action.get("reason")
    if not isinstance(action_reason, str) or not action_reason.strip():
        raise ValidationError(
            "'action.reason' must be a non-empty string explaining the policy intent."
        )

    if "params" in action and not isinstance(action["params"], dict):
        raise ValidationError("'action.params' must be a JSON object when present.")

    # --- 11. confidence range --------------------------------------------------
    raw_confidence = raw["confidence"]
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise ValidationError(
            f"'confidence' must be a number, got {type(raw_confidence).__name__}."
        )
    confidence = float(raw_confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ValidationError(
            f"'confidence' must be in [0.0, 1.0], got {confidence:.4f}."
        )

    # --- 12. Confidence threshold gate -----------------------------------------
    if confidence < CONFIDENCE_THRESHOLD:
        raise ValidationError(
            f"Confidence {confidence:.4f} is below the required threshold "
            f"{CONFIDENCE_THRESHOLD}. Rule rejected: the LLM was not sufficiently "
            f"certain about the semantic mapping. Rephrase the rule more precisely."
        )

    # --- 13. rule_id resolution ------------------------------------------------
    rule_id = raw.get("rule_id", "auto")
    if not isinstance(rule_id, str) or not rule_id.strip() or rule_id.strip() == "auto":
        rule_id = str(uuid.uuid4())
    else:
        rule_id = rule_id.strip()

    logger.debug(
        "Policy validated: rule_id=%s device_type=%s conditions=%d confidence=%.4f",
        rule_id, device_type, len(conditions), confidence,
    )

    return {
        "rule_id": rule_id,
        "scope": scope,
        "conditions": conditions,
        "action": action,
        "confidence": confidence,
    }


def _validate_condition(cond: Any, index: int) -> None:
    """Validate one condition object from the conditions array."""
    if not isinstance(cond, dict):
        raise ValidationError(
            f"conditions[{index}] must be a JSON object, "
            f"got {type(cond).__name__}."
        )

    allowed_cond_keys = frozenset({"field", "operator", "value"})
    extra = set(cond.keys()) - allowed_cond_keys
    if extra:
        raise ValidationError(
            f"conditions[{index}] contains disallowed fields: {sorted(extra)}. "
            f"Allowed: {sorted(allowed_cond_keys)}."
        )

    for key in ("field", "operator", "value"):
        if key not in cond:
            raise ValidationError(
                f"conditions[{index}] is missing required field '{key}'."
            )

    field = cond["field"]
    if field not in _ALLOWED_CONDITION_FIELDS:
        raise ValidationError(
            f"conditions[{index}].field '{field}' is not allowed. "
            f"Allowed fields: {sorted(_ALLOWED_CONDITION_FIELDS)}."
        )

    operator = cond["operator"]
    if operator not in _ALLOWED_OPERATORS:
        raise ValidationError(
            f"conditions[{index}].operator '{operator}' is not allowed. "
            f"Allowed operators: {sorted(_ALLOWED_OPERATORS)}."
        )

    value = cond["value"]
    if field in _NUMERIC_FIELDS:
        # booleans are a subtype of int in Python — reject them explicitly
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"conditions[{index}].value for field '{field}' must be a "
                f"number (got {type(value).__name__}). "
                f"Example: temperature < 65 uses a numeric Fahrenheit value."
            )
    elif field in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise ValidationError(
                f"conditions[{index}].value for field '{field}' must be a "
                f"boolean (true or false), got {type(value).__name__}."
            )
