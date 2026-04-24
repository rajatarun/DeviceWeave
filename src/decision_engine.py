"""
Unified Decision Engine — merges all resolution signals into one final score.

Score composition
-----------------
The engine combines two independent signals:

  cosine_score    (0–1) — TF-vector similarity from device_resolver (Tier 1)
  behavior_score  (0–1) — historical context pattern from behavior_engine

Final score = α × cosine + (1 − α) × behavior

α is adaptive:
  • When Memgraph has no history (behavior neutral at 0.5), α is set to 0.9 so
    the cosine match dominates and the behavior engine has no negative effect.
  • Once the device accumulates ≥ MIN_HISTORY_EVENTS events, α drops to 0.5
    and behavior contributes equally.

This prevents the behavior engine from penalising devices that have just been
added to the registry (they haven't been used yet, so behavior=0.5 neutral).

Intent routing
--------------
classify_intent() routes the raw command to one of three paths:
    "direct"    — explicit device reference ("turn on office fan")
    "scene"     — known situational phrase ("movie time", "work mode")
    "behavior"  — implicit / contextual cue ("it's too hot", "going to kitchen")

The caller (app.py) may use this hint to skip Tier 1 and go straight to the
LLM resolver for behavior-type commands where cosine similarity is inherently low.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

import behavior_engine
import graph_engine

logger = logging.getLogger(__name__)

# Minimum event count before behavior signal is trusted with full weight
_MIN_HISTORY_EVENTS = 10

# Regex patterns that signal behavioral / contextual intent
_BEHAVIORAL_CUES = re.compile(
    r'\b(hot|warm|cold|tired|hungry|sleepy|working|relaxing|going|heading|'
    r'leaving|arriving|waking|sleeping|studying|reading|watching|cooking|'
    r'it is|it\'s|i am|i\'m|feel|feeling)\b',
    re.IGNORECASE,
)

# Explicit action verbs → direct device path
_DIRECT_ACTION_CUES = re.compile(
    r'\b(turn|switch|power|enable|disable|toggle|set|dim|brighten|activate|deactivate)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Intent routing
# ---------------------------------------------------------------------------

def classify_intent(command: str) -> str:
    """
    Classify a command as 'direct', 'scene', or 'behavior'.

    This is a lightweight, regex-only classifier — the LLM is NOT called here.
    The result is a routing hint to help app.py pick the fastest resolution path.

    direct   — command contains explicit action verbs ("turn on", "dim to 50%")
    scene    — scene catalog will be tried first regardless
    behavior — command implies a state/context without an explicit device action
    """
    if _DIRECT_ACTION_CUES.search(command):
        return "direct"
    if _BEHAVIORAL_CUES.search(command):
        return "behavior"
    return "scene"


# ---------------------------------------------------------------------------
# Unified score
# ---------------------------------------------------------------------------

def compute_score(
    cosine_score: float,
    device: Dict[str, Any],
    action: str,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float, float]:
    """
    Compute the unified decision score for (device, action).

    Returns:
        (final_score, cosine_score, behavior_score)

    The returned scores are logged at DEBUG level for observability.
    """
    b_score = behavior_engine.score(device, action, context)

    # Determine history depth to set adaptive α
    history = graph_engine.query_behavior_history(
        device_id=device["id"],
        action=action,
        hour=(context or behavior_engine.current_context()).get("hour", 0),
        hour_window=4,
    )
    total_events = history.get("total", 0)

    if total_events < _MIN_HISTORY_EVENTS:
        # Not enough history — let cosine dominate
        alpha = 0.9
    else:
        alpha = 0.5

    final = alpha * cosine_score + (1.0 - alpha) * b_score
    final = round(final, 4)

    logger.debug(
        "decision_engine device=%s action=%s cosine=%.4f behavior=%.4f "
        "alpha=%.1f events=%d → final=%.4f",
        device["id"], action, cosine_score, b_score,
        alpha, total_events, final,
    )

    return final, cosine_score, b_score


# ---------------------------------------------------------------------------
# Safety + identity check
# ---------------------------------------------------------------------------

def validate_execution(
    device: Dict[str, Any],
    action: str,
    final_score: float,
    threshold: float,
) -> Tuple[bool, str]:
    """
    Final gate before execution.

    Returns (allowed, reason).
    Currently enforces:
    - Score threshold
    - Action must be in device capabilities
    """
    if final_score < threshold:
        return False, (
            f"Score {final_score:.4f} below threshold {threshold} "
            f"for {device['id']}/{action}"
        )

    if action not in device.get("capabilities", []):
        return False, (
            f"'{device['name']}' does not support '{action}'. "
            f"Supported: {device.get('capabilities', [])}"
        )

    return True, ""
