"""
Conversational IoT agent using Google ADK (Agent Development Kit).

Refactored from direct google-genai generate_content calls to ADK:
- Tools are plain Python functions; ADK auto-generates Gemini function declarations
  from each function's signature and Google-style docstring.
- Agent created with google.adk.agents.Agent — no manual JSON tool defs needed.
- Agentic loop (tool calls, responses, retries) managed by google.adk.runners.InMemoryRunner.
- Session seeded from caller-provided history on each invocation and discarded after;
  DynamoDB persistence is handled by the caller (conversation_store.py).
- Async tools (execute_device_command, execute_scene) use `await` directly instead of
  asyncio.run() inside a thread-pool, since ADK awaits async tools in-place.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MODEL_ID: str = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
_RESPONSE_TIMEOUT_SECS: int = int(os.environ.get("GEMINI_TIMEOUT_SECS", "25"))

_APP_NAME = "deviceweave"
_USER_ID = "default"

# Module-level ADK runner — survives Lambda warm restarts.
_runner = None


def _load_api_key() -> str:
    """Load Gemini API key from AWS Secrets Manager."""
    import boto3
    from botocore.config import Config as BotocoreConfig

    secret_name = os.environ.get("GEMINI_SECRET_NAME", "gemini/api_key")
    logger.info("Loading Gemini API key from secret: %s", secret_name)
    try:
        sm_client = boto3.client(
            "secretsmanager",
            config=BotocoreConfig(
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 1},
                use_dualstack_endpoint=True,
            ),
        )
        secret_resp = sm_client.get_secret_value(SecretId=secret_name)
        api_key = json.loads(secret_resp["SecretString"])["key"]
        logger.info("Gemini API key loaded successfully")
        return api_key
    except Exception as exc:
        logger.exception("Failed to load Gemini API key from %s", secret_name)
        raise RuntimeError(f"Failed to load Gemini API key from {secret_name}: {exc}")


_SYSTEM_PROMPT = """\
You are DeviceWeave, a friendly and concise IoT home automation assistant.
You control smart home devices by calling the tools available to you.

Guidelines:
- Always call list_devices before executing a device command when you are not
  certain of the exact device_id.
- Prefer calling list_scenes when the user mentions a scene by name.
- Execute only what the user asked for — do not take additional actions.
- After execution, report the outcome in one or two sentences.
- If a command is blocked by a policy, explain clearly and do not retry.
- If you cannot find a matching device or scene, say so and suggest alternatives.
- Keep all responses short and conversational.

canonical_phrase requirement:
- ALWAYS populate canonical_phrase in execute_device_command and execute_scene.
- canonical_phrase must be a short, self-contained English phrase that fully
  describes the resolved intent using conversation context — no pronouns
  or relative references like "it", "that one", "the same", or "too".
- Examples of correct canonical_phrase values:
    user says "kitchen too" after turning on living room light
      → canonical_phrase: "turn on kitchen island light"
    user says "dim it a bit"
      → canonical_phrase: "dim living room ceiling light to 50 percent"
    user says "run movie mode"
      → canonical_phrase: "run movie mode scene"
- This phrase is recorded for future intent matching — accuracy matters.

behavior history requirement:
- Before executing a command on a device, call get_device_history for that
  device_id if the user's request involves any preference or ambiguity
  (e.g. "dim it", "the usual brightness", "like always", "movie mode lights").
- Use the top_actions list to confirm the intended action is typical for this device.
- Use action_context.typical_at_this_hour to flag unusual timing to the user if
  relevant (e.g. "you don't usually turn this on at this hour — shall I proceed?").
- If the device has no history yet, proceed normally without mentioning it.
"""


# ---------------------------------------------------------------------------
# Tools — plain Python functions with Google-style docstrings.
# ADK generates Gemini function declarations from signature + docstring automatically;
# no separate JSON tool definitions or dispatcher needed.
# ---------------------------------------------------------------------------


def list_devices() -> Dict[str, Any]:
    """Return all registered IoT devices with their id, name, device_type, and available capabilities.

    Call this first when you need to find the correct device_id for a user command.

    Returns:
        A dict with 'devices' (list of device objects) and 'count'.
    """
    from device_resolver import _get_active_catalog, DeviceRegistryError

    try:
        catalog = _get_active_catalog()
        devices = [
            {
                "id": d["id"],
                "name": d["name"],
                "device_type": d.get("device_type", ""),
                "capabilities": d.get("capabilities", []),
            }
            for d in catalog
        ]
        return {"devices": devices, "count": len(devices)}
    except DeviceRegistryError as exc:
        return {"error": str(exc)}


def list_scenes() -> Dict[str, Any]:
    """Return all active scenes with their id, name, and description.

    Use this when the user mentions a scene by name.

    Returns:
        A dict with 'scenes' (list of scene objects) and 'count'.
    """
    from scene_catalog import get_active_scenes

    scenes = get_active_scenes()
    return {
        "scenes": [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s.get("description", ""),
            }
            for s in scenes
        ],
        "count": len(scenes),
    }


async def execute_device_command(
    device_id: str,
    action: str,
    canonical_phrase: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute an action on a specific device.

    Policy enforcement is applied before any device I/O. A blocked command returns an error.
    Always resolve the device_id via list_devices first if uncertain.

    Args:
        device_id: Exact device ID from list_devices.
        action: Action to perform — must be in the device's capabilities list
            (e.g. 'on', 'off', 'set_brightness', 'toggle').
        canonical_phrase: REQUIRED. A short, self-contained phrase that fully describes
            the resolved intent using conversation context — no pronouns or relative
            references. Example: 'turn on kitchen island light'.
        params: Optional action parameters, e.g. {"brightness": 75} for set_brightness.
            Omit or pass {} for actions that take no params.

    Returns:
        A dict with success/error status and execution details.
    """
    from device_resolver import _get_active_catalog, DeviceRegistryError
    from execution_planner import plan_device_execution, execute_steps
    from policy_engine.middleware import enforce as policy_enforce
    from policy_engine.context_provider import get_context as get_policy_context
    from learning_store import LEARNING_THRESHOLD, is_configured, save_learned_phrase
    import graph_engine

    params = params or {}

    try:
        catalog = _get_active_catalog()
    except DeviceRegistryError as exc:
        return {"error": str(exc)}

    catalog_index = {d["id"]: d for d in catalog}
    device = catalog_index.get(device_id)
    if device is None:
        return {"error": f"Device '{device_id}' not found. Call list_devices to get valid IDs."}

    if action not in device.get("capabilities", []):
        return {
            "error": f"'{device['name']}' does not support '{action}'.",
            "supported_actions": device.get("capabilities", []),
        }

    if action == "set_brightness" and "brightness" not in params:
        return {"error": "set_brightness requires a 'brightness' param (0-100)."}

    try:
        policy_ctx = get_policy_context()
        decision = policy_enforce(device["device_type"], action, params, context=policy_ctx)
    except Exception as exc:
        logger.warning("Policy check failed: %s — proceeding without enforcement", exc)
        decision = None

    if decision is not None and decision.is_blocked:
        return {
            "blocked": True,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
        }
    if decision is not None and decision.is_modified:
        params = decision.modified_params or {}

    steps = plan_device_execution(device, action, params)
    try:
        results = await execute_steps(steps)
    except Exception as exc:
        logger.exception("Agent device execution error")
        return {"error": f"Execution error: {exc}"}

    result = results[0]
    if not result.success:
        return {"error": result.error, "device_id": device_id}

    if canonical_phrase:
        if is_configured():
            save_learned_phrase(device_id, canonical_phrase, LEARNING_THRESHOLD)
        graph_engine.record_event(device_id, action, canonical_phrase)
        logger.info("Learned phrase for %s: %r", device_id, canonical_phrase)

    return {
        "success": True,
        "device_id": device_id,
        "device_name": device["name"],
        "action": action,
        "params": params,
        "result": result.result,
    }


async def execute_scene(scene_id: str, canonical_phrase: str) -> Dict[str, Any]:
    """Execute a registered scene by its ID.

    Scenes trigger multiple device actions simultaneously. Policy enforcement applies to each step.

    Args:
        scene_id: Exact scene ID from list_scenes.
        canonical_phrase: REQUIRED. A short, self-contained phrase that fully describes
            the resolved intent using conversation context — no pronouns or relative
            references. Example: 'run movie mode scene'.

    Returns:
        A dict with success/error status and counts of succeeded/failed/blocked steps.
    """
    from scene_catalog import get_active_scenes
    from device_resolver import _get_active_catalog, DeviceRegistryError
    from execution_planner import plan_scene_execution, execute_steps
    from policy_engine.middleware import filter_steps as policy_filter_steps
    from policy_engine.context_provider import get_context as get_policy_context
    from learning_store import LEARNING_THRESHOLD, is_configured, save_learned_phrase
    import graph_engine

    scenes = {s["id"]: s for s in get_active_scenes()}
    scene = scenes.get(scene_id)
    if scene is None:
        return {"error": f"Scene '{scene_id}' not found. Call list_scenes to get valid IDs."}

    try:
        catalog = _get_active_catalog()
    except DeviceRegistryError as exc:
        return {"error": str(exc)}

    steps = plan_scene_execution(scene, catalog)
    if not steps:
        return {"error": f"Scene '{scene_id}' produced no executable steps."}

    try:
        policy_ctx = get_policy_context()
        allowed_steps, policy_blocks = policy_filter_steps(steps, context=policy_ctx)
    except Exception as exc:
        logger.warning("Policy filter failed: %s — executing all steps", exc)
        allowed_steps, policy_blocks = steps, []

    if not allowed_steps:
        return {
            "blocked": True,
            "reason": "All scene steps were blocked by active policies.",
            "policy_blocks": policy_blocks,
        }

    try:
        results = await execute_steps(allowed_steps)
    except Exception as exc:
        logger.exception("Agent scene execution error")
        return {"error": f"Scene execution error: {exc}"}

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    if canonical_phrase and successes:
        for r in successes:
            if is_configured():
                save_learned_phrase(r.device_id, canonical_phrase, LEARNING_THRESHOLD)
            graph_engine.record_event(r.device_id, r.action, canonical_phrase)
        logger.info(
            "Learned scene phrase for %s (%d devices): %r",
            scene_id, len(successes), canonical_phrase,
        )

    return {
        "success": True,
        "scene_id": scene_id,
        "scene_name": scene.get("name", scene_id),
        "succeeded": len(successes),
        "failed": len(failures),
        "policy_blocks": len(policy_blocks),
        "results": [
            {
                "device_id": r.device_id,
                "device_name": r.device_name,
                "action": r.action,
                "success": r.success,
                "error": r.error if not r.success else None,
            }
            for r in results
        ],
    }


def get_device_history(device_id: str, action: str = "") -> Dict[str, Any]:
    """Return historical behavior for a device.

    Returns most frequent actions and, when an action is specified, whether it is typical
    at the current hour. Call this before executing when the user's request involves a
    preference or ambiguity (e.g. 'dim it', 'the usual', 'like always').

    Args:
        device_id: Exact device ID from list_devices.
        action: Optional — the action you intend to execute. When provided, the response
            includes whether this action is typical at the current time of day.

    Returns:
        A dict with device_id, top_actions list, has_history flag, and optional action_context.
    """
    import graph_engine
    from datetime import datetime, timezone

    top_actions = graph_engine.query_top_actions(device_id, limit=5)
    result: Dict[str, Any] = {
        "device_id": device_id,
        "top_actions": top_actions,
        "has_history": len(top_actions) > 0,
    }

    if action:
        now = datetime.now(timezone.utc)
        counts = graph_engine.query_behavior_history(device_id, action, now.hour)
        result["action_context"] = {
            "action": action,
            "current_hour": now.hour,
            "typical_at_this_hour": (counts["matching"] > 0) if counts["total"] > 0 else None,
            "matching_events": counts["matching"],
            "total_events": counts["total"],
        }

    return result


# ---------------------------------------------------------------------------
# ADK Runner (module-level, cached for Lambda warm restarts)
# ---------------------------------------------------------------------------


def _get_runner():
    """Return the cached ADK InMemoryRunner, initializing on first call."""
    global _runner
    if _runner is not None:
        return _runner

    try:
        from google.adk.agents import Agent
        from google.adk.runners import InMemoryRunner
        from google.genai import types as genai_types
    except ImportError:
        raise RuntimeError(
            "google-adk not installed. Install with: pip install google-adk"
        )

    api_key = _load_api_key()
    # ADK picks up the API key from GOOGLE_API_KEY at agent-creation time.
    os.environ["GOOGLE_API_KEY"] = api_key

    agent = Agent(
        name="deviceweave",
        model=_MODEL_ID,
        instruction=_SYSTEM_PROMPT,
        tools=[
            list_devices,
            list_scenes,
            execute_device_command,
            execute_scene,
            get_device_history,
        ],
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=1024,
        ),
    )

    _runner = InMemoryRunner(agent=agent, app_name=_APP_NAME)
    logger.info("ADK Runner initialized: model=%s", _MODEL_ID)
    return _runner


# ---------------------------------------------------------------------------
# Agentic entrypoint
# ---------------------------------------------------------------------------


async def run_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the Gemini agentic loop using ADK.

    Args:
        user_message:        The latest message from the user.
        history:             Prior text turns for the session (may be []).
                             Format: [{"role": "user"|"model", "parts": [{"text": ...}]}, ...]
        system_prompt_extra: Ignored — instruction is fixed at Agent creation time in ADK.

    Returns:
        (reply_text, updated_history)
        reply_text      — the agent's final text response.
        updated_history — prior text turns plus the new user/model exchange.
    """
    from google.adk.events import Event
    from google.genai import types as genai_types

    if system_prompt_extra:
        logger.warning("system_prompt_extra is not supported in ADK mode; ignoring")

    runner = _get_runner()
    session_id = str(uuid.uuid4())
    session_service = runner.session_service

    logger.info("ADK agent invoked: model=%s history_turns=%d", _MODEL_ID, len(history))

    # Create an ephemeral session for this invocation.
    session = await session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=session_id,
    )

    # Seed the session with prior text turns from the persistent history.
    # Non-text parts (serialized tool call/response artifacts) are silently skipped.
    for i, turn in enumerate(history):
        role = turn.get("role", "user")
        parts = [
            genai_types.Part(text=p["text"])
            for p in turn.get("parts", [])
            if isinstance(p, dict) and p.get("text")
        ]
        if not parts:
            continue
        author = _USER_ID if role == "user" else "deviceweave"
        await session_service.append_event(
            session=session,
            event=Event(
                author=author,
                invocation_id=f"history-{i}",
                content=genai_types.Content(role=role, parts=parts),
            ),
        )

    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_message)],
    )

    final_response = ""
    try:
        async with asyncio.timeout(_RESPONSE_TIMEOUT_SECS):
            async for event in runner.run_async(
                user_id=_USER_ID,
                session_id=session_id,
                new_message=new_message,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    text = "".join(
                        p.text for p in event.content.parts
                        if hasattr(p, "text") and p.text
                    )
                    if text:
                        final_response = text
    except TimeoutError:
        logger.error("ADK agent timed out after %ds", _RESPONSE_TIMEOUT_SECS)
        raise RuntimeError(f"Gemini API timed out after {_RESPONSE_TIMEOUT_SECS}s")
    except Exception as exc:
        logger.exception("ADK agent error")
        raise RuntimeError(f"Gemini API failed: {exc}") from exc

    # Build updated history: carry forward only text turns, then append current exchange.
    text_history = [
        t for t in history
        if t.get("role") in ("user", "model")
        and any(isinstance(p, dict) and p.get("text") for p in t.get("parts", []))
    ]
    updated_history = text_history + [
        {"role": "user", "parts": [{"text": user_message}]},
        {"role": "model", "parts": [{"text": final_response or "(no response)"}]},
    ]

    logger.info(
        "ADK agent finished: session=%s history_turns=%d", session_id, len(updated_history)
    )
    return final_response or "(no response)", updated_history
