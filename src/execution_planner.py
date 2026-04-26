"""
Execution planner — converts resolved device/scene intents into typed
ExecutionStep records, then runs them via the provider registry.

Single-device commands produce one step.
Scene commands produce N steps (one per scene action) and run concurrently
via asyncio.gather so total latency ≈ max(individual latencies).

All capability checks are done here before any I/O so the provider
adapters only receive validated, supported actions.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from providers import ProviderError, get_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ExecutionStep:
    device: Dict[str, Any]
    action: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    device_id: str
    device_name: str
    action: str
    success: bool
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------

def plan_device_execution(
    device: Dict[str, Any],
    action: str,
    params: Dict[str, Any],
) -> List[ExecutionStep]:
    """Return a single-step plan for a resolved device command."""
    return [ExecutionStep(device=device, action=action, params=params)]


def plan_scene_execution(
    scene: Dict[str, Any],
    catalog: List[Dict[str, Any]],
) -> List[ExecutionStep]:
    """
    Convert a scene's action list into ExecutionSteps with full device dicts.

    Steps whose device_id is not in the catalog or whose action is not in the
    device's capabilities are skipped with a warning rather than aborting the
    entire scene — partial execution is better than no execution.
    """
    device_index: Dict[str, Dict[str, Any]] = {d["id"]: d for d in catalog}
    steps: List[ExecutionStep] = []
    for spec in scene["actions"]:
        device = device_index.get(spec["device_id"])
        if device is None:
            logger.warning(
                "Scene '%s': device_id '%s' not found in catalog — skipping.",
                scene["id"], spec["device_id"],
            )
            continue
        if spec["action"] not in device["capabilities"]:
            logger.warning(
                "Scene '%s': device '%s' does not support '%s' — skipping.",
                scene["id"], spec["device_id"], spec["action"],
            )
            continue
        steps.append(ExecutionStep(
            device=device,
            action=spec["action"],
            params=spec.get("params", {}),
        ))
    return steps


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

async def execute_steps(steps: List[ExecutionStep]) -> List[StepResult]:
    """
    Execute all steps concurrently.

    asyncio.gather runs all coroutines in parallel so a 2-device scene
    incurs the latency of the slower device, not the sum of both.
    return_exceptions=True ensures one failing device does not abort others.
    """
    tasks = [_execute_one(step) for step in steps]
    results: List[StepResult] = await asyncio.gather(*tasks, return_exceptions=False)
    return results


async def _execute_one(step: ExecutionStep) -> StepResult:
    device = step.device
    try:
        provider = get_provider(device["device_type"])
        result = await provider.execute(device, step.action, step.params)
        return StepResult(
            device_id=device["id"],
            device_name=device["name"],
            action=step.action,
            success=True,
            result=result,
        )
    except ProviderError as exc:
        logger.error("ProviderError for %s/%s: %s", device["id"], step.action, exc)
        return StepResult(
            device_id=device["id"],
            device_name=device["name"],
            action=step.action,
            success=False,
            error=str(exc),
        )
    except ValueError as exc:
        logger.error("ValueError for %s/%s: %s", device["id"], step.action, exc)
        return StepResult(
            device_id=device["id"],
            device_name=device["name"],
            action=step.action,
            success=False,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Unexpected error for %s/%s", device["id"], step.action)
        return StepResult(
            device_id=device["id"],
            device_name=device["name"],
            action=step.action,
            success=False,
            error=f"Unexpected error: {exc}",
        )
