"""
Policy DSL condition evaluator — pure, deterministic, no I/O.

Accepts a list of active Policy DSL dicts (already loaded from DynamoDB)
and a runtime context dict, and returns a PolicyDecision for a specific
(device_type, action) pair.

Verdict priority (highest wins):
  1. BLOCK  — any matching block policy prevents execution entirely.
              Safety takes precedence: if the user said "never run fan
              when cold", that constraint must hold even if another policy
              would permit it.
  2. MODIFY — no block matched but at least one modify policy matched.
              The first matching modifier's params are returned.  Multiple
              matching modifiers are not merged — the most-recently-created
              policy wins (list is sorted newest-first by the GSI).
  3. ALLOW  — default.  No restrictive policy matched.  Explicit "allow"
              action-type policies are informational only in this version;
              they do not override a BLOCK.

Condition evaluation:
  All conditions in a single policy are evaluated with AND semantics.
  A policy matches only when every condition is true for the current context.
  An unknown context field (should not happen in practice) evaluates to False
  so the condition does not match — this is the safe/conservative choice.

Safe actions bypass:
  turn_off and get_status are never blocked regardless of policy.  These
  are deactivation/read-only operations; blocking them could prevent a user
  from switching off a device they want to stop.
"""

import logging
import operator as _op
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Device actions that are never blocked by any policy.
_SAFE_ACTIONS = frozenset({"turn_off", "get_status"})

_OPERATORS: Dict[str, Any] = {
    ">":  _op.gt,
    "<":  _op.lt,
    ">=": _op.ge,
    "<=": _op.le,
    "==": _op.eq,
    "!=": _op.ne,
}


@dataclass
class PolicyDecision:
    """Result of a policy evaluation for one (device_type, action) pair."""
    verdict: str                          # "allow" | "block" | "modify"
    reason: str                           # human-readable explanation
    rule_id: Optional[str]                # which rule triggered (None = default allow)
    modified_params: Optional[Dict[str, Any]]  # non-None only when verdict="modify"

    @property
    def is_blocked(self) -> bool:
        return self.verdict == "block"

    @property
    def is_modified(self) -> bool:
        return self.verdict == "modify"


# Sentinel for the default-allow case — created once, never mutated.
_ALLOW = PolicyDecision(
    verdict="allow",
    reason="",
    rule_id=None,
    modified_params=None,
)


def compute_verdict(
    policies: List[Dict[str, Any]],
    device_type: str,
    action: str,
    context: Dict[str, Any],
) -> PolicyDecision:
    """
    Evaluate all active policies for *device_type* against *context* and
    return the highest-priority matching verdict for *action*.

    Parameters
    ----------
    policies    All active Policy DSL dicts for this device_type (pre-filtered
                by the loader — but we defensively re-check device_type).
    device_type The resolved device's type (fan, light, ac, plug, heater).
    action      The requested device action (turn_on, set_brightness, …).
    context     Runtime context dict from context_provider.get_context().
    """
    # Safe actions (turn_off, get_status) are never restricted.
    if action in _SAFE_ACTIONS:
        logger.debug("Policy engine: safe action '%s' bypasses policy check", action)
        return _ALLOW

    first_block: Optional[Dict[str, Any]] = None
    first_modifier: Optional[Dict[str, Any]] = None

    for policy in policies:
        # Defensive scope check (loader pre-filters but be explicit)
        if policy.get("scope", {}).get("device_type") != device_type:
            continue

        conditions = policy.get("conditions", [])
        if not _all_conditions_match(conditions, context):
            continue

        action_type = policy["action"]["type"]

        if action_type == "block" and first_block is None:
            first_block = policy
        elif action_type == "modify" and first_modifier is None:
            first_modifier = policy
        # "allow" policies are noted but do not change the default outcome.

    # --- Apply priority ---
    if first_block is not None:
        p = first_block
        logger.info(
            "Policy BLOCK: rule_id=%s device_type=%s action=%s reason=%r context=%s",
            p["rule_id"], device_type, action, p["action"]["reason"],
            _context_summary(context),
        )
        return PolicyDecision(
            verdict="block",
            reason=p["action"]["reason"],
            rule_id=p["rule_id"],
            modified_params=None,
        )

    if first_modifier is not None:
        p = first_modifier
        modified = dict(p["action"].get("params") or {})
        logger.info(
            "Policy MODIFY: rule_id=%s device_type=%s action=%s params=%s reason=%r",
            p["rule_id"], device_type, action, modified, p["action"]["reason"],
        )
        return PolicyDecision(
            verdict="modify",
            reason=p["action"]["reason"],
            rule_id=p["rule_id"],
            modified_params=modified,
        )

    return _ALLOW


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_conditions_match(
    conditions: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> bool:
    """Return True only when every condition in the list evaluates to True."""
    for cond in conditions:
        if not _evaluate_condition(cond, context):
            return False
    return True


def _evaluate_condition(cond: Dict[str, Any], context: Dict[str, Any]) -> bool:
    """
    Evaluate one condition object against the runtime context.

    Returns False (no match) for any unknown field or operator rather than
    raising — missing context should never trigger a restrictive policy.
    """
    field = cond.get("field")
    operator_str = cond.get("operator")
    threshold = cond.get("value")

    ctx_value = context.get(field)
    if ctx_value is None:
        logger.debug(
            "Policy condition: field '%s' not in context — condition skipped (no match)",
            field,
        )
        return False

    op_fn = _OPERATORS.get(operator_str)
    if op_fn is None:
        logger.warning("Policy condition: unknown operator '%s' — skipped", operator_str)
        return False

    try:
        result = bool(op_fn(ctx_value, threshold))
        logger.debug(
            "Condition: %s(%s) %s %s → %s",
            field, ctx_value, operator_str, threshold, result,
        )
        return result
    except TypeError as exc:
        logger.warning(
            "Policy condition type mismatch: %s %s %s (%s) — skipped",
            field, operator_str, threshold, exc,
        )
        return False


def _context_summary(ctx: Dict[str, Any]) -> str:
    return (
        f"temp={ctx.get('temperature')}°F "
        f"hum={ctx.get('humidity')}% "
        f"hour={ctx.get('time_hour')} "
        f"home={ctx.get('is_home')}"
    )
