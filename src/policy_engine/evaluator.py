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
              The winning modifier's params are returned. Multiple matching
              modifiers are not merged.
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

import json
import logging
import operator as _op
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from policy_engine.device_types import canonical_device_type

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


def rule_winner_key(policy: Dict[str, Any]) -> Tuple[int, str, str, str]:
    """Total order for ties inside one verdict type. The lowest key wins.

    The primary key is the rule id string, compared lexicographically. A
    missing or blank id sorts after every real id. The reason, then a
    canonical rendering of the params, break any remaining tie so two
    shuffles of the same rules cannot pick different ids, reasons, or
    MODIFY params.
    """
    rid = policy.get("rule_id")
    if rid is None:
        rid_s = ""
        missing = True
    else:
        rid_s = str(rid).strip()
        missing = rid_s == ""
    action = policy.get("action") if isinstance(policy.get("action"), dict) else {}
    reason = action.get("reason")
    reason_s = "" if reason is None else str(reason)
    raw_params = action.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    try:
        params_key = json.dumps(params, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        params_key = repr(sorted((str(k), repr(v)) for k, v in params.items()))
    return (1 if missing else 0, rid_s, reason_s, params_key)


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
    device_type The resolved device's type: an adapter routing key
                (SmartBulb, GoveePlug, ...) or a scope class (light, plug, ...).
                Both sides are compared via canonical_device_type().
    action      The requested device action (turn_on, set_brightness, …).
    context     Runtime context dict from context_provider.get_context().
    """
    # Safe actions (turn_off, get_status) are never restricted.
    if action in _SAFE_ACTIONS:
        logger.debug("Policy engine: safe action '%s' bypasses policy check", action)
        return _ALLOW

    blocks: List[Dict[str, Any]] = []
    modifiers: List[Dict[str, Any]] = []
    device_class = canonical_device_type(device_type)

    for policy in policies:
        # Defensive scope check (loader pre-filters but be explicit).
        # Compare in the canonical vocabulary so a "light" policy matches a
        # SmartBulb / GoveeBulb / ... device.
        if canonical_device_type(policy.get("scope", {}).get("device_type")) != device_class:
            continue

        conditions = policy.get("conditions", [])
        if not _all_conditions_match(conditions, context):
            continue

        action_type = policy["action"]["type"]

        if action_type == "block":
            blocks.append(policy)
        elif action_type == "modify":
            modifiers.append(policy)
        # "allow" policies are noted but do not change the default outcome.

    # --- Apply priority. Within one type the lowest rule id wins, so the
    # reported id, reason, and MODIFY params do not depend on list order.
    if blocks:
        p = min(blocks, key=rule_winner_key)
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

    if modifiers:
        p = min(modifiers, key=rule_winner_key)
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
