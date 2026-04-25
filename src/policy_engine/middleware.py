"""
Policy Engine public interface — the single call-site used by app.py.

Provides two functions:

  enforce(device_type, action, params, context=None) → PolicyDecision
    The main gate.  Loads active policies, assembles context, evaluates
    conditions, and returns a verdict.  Context is pre-fetched and passed
    in by callers that process multiple devices in one request (scenes /
    multi-device LLM responses) so the weather/presence I/O happens once
    per request, not once per device.

  get_context() → Dict
    Re-exported from context_provider so app.py has a single import point.

Usage in app.py
---------------
  from policy_engine.middleware import enforce, get_context as get_policy_context

  # Fetch context once at the start of the request:
  ctx = get_policy_context()

  # Single device:
  decision = enforce(device["device_type"], action, params, context=ctx)
  if decision.is_blocked:
      return _error(403, f"Policy blocked: {decision.reason}", ...)

  # Scene / multi-device — filter steps before execution:
  allowed, blocked = filter_steps(steps, context=ctx)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from policy_engine.context_provider import get_context
from policy_engine.evaluator import PolicyDecision, compute_verdict
from policy_engine.policy_loader import get_policies_for_device, is_configured

logger = logging.getLogger(__name__)


def enforce(
    device_type: str,
    action: str,
    params: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> PolicyDecision:
    """
    Evaluate active policies for (device_type, action) against runtime context.

    Parameters
    ----------
    device_type   From the resolved device dict (fan, light, ac, plug, heater).
    action        The requested device action (turn_on, set_brightness, …).
    params        Current action params; returned as-is for ALLOW, replaced
                  for MODIFY (caller must use decision.modified_params instead).
    context       Pre-fetched context dict.  If None, context is fetched here.
                  Pass a pre-fetched context when evaluating multiple devices
                  in the same request to avoid redundant weather/presence reads.

    Returns
    -------
    PolicyDecision with verdict "allow", "block", or "modify".
    Always returns ALLOW when the policy table is not configured so the
    engine is completely transparent when no policies exist.
    """
    if not is_configured():
        from policy_engine.evaluator import _ALLOW
        return _ALLOW

    if context is None:
        context = get_context()

    policies = get_policies_for_device(device_type)

    if not policies:
        from policy_engine.evaluator import _ALLOW
        return _ALLOW

    return compute_verdict(policies, device_type, action, context)


def filter_steps(
    steps: List[Any],           # List[ExecutionStep] — avoid circular import
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """
    Apply policy evaluation to a list of ExecutionStep objects.

    Returns
    -------
    (allowed_steps, blocked_records)

    allowed_steps    Steps that passed policy evaluation (params may be
                     updated if a MODIFY policy matched).
    blocked_records  List of dicts describing each blocked step:
                     {"device_id": …, "device_name": …, "action": …,
                      "reason": …, "rule_id": …}

    This function is the batch equivalent of enforce() — it fetches context
    once and evaluates all steps in a single pass.
    """
    if context is None:
        context = get_context()

    allowed = []
    blocked = []

    for step in steps:
        device_type = step.device.get("device_type", "")
        decision = enforce(device_type, step.action, step.params, context=context)

        if decision.is_blocked:
            blocked.append({
                "device_id": step.device["id"],
                "device_name": step.device.get("name", step.device["id"]),
                "action": step.action,
                "policy_verdict": "block",
                "reason": decision.reason,
                "rule_id": decision.rule_id,
            })
        elif decision.is_modified:
            # Return a new step with the modified params so the original
            # step object (owned by the caller) is never mutated.
            from execution_planner import ExecutionStep
            allowed.append(ExecutionStep(
                device=step.device,
                action=step.action,
                params=decision.modified_params or {},
            ))
        else:
            allowed.append(step)

    return allowed, blocked
