"""
Conversational IoT agent using the Bedrock Converse API.

Design:
- The agent drives an agentic loop: it calls tools (list_devices, list_scenes,
  execute_device_command, execute_scene) until it reaches end_turn.
- Policy enforcement lives inside execute_device_command / execute_scene, just
  as it does in the one-shot /execute path.
- The caller is responsible for loading and saving session history
  (conversation_store.py).  This module is purely functional.
- Maximum 10 tool-call rounds per invocation to prevent runaway loops.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MODEL_ID: str = os.environ.get(
    "LLM_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
_MAX_TOOL_ROUNDS: int = 10

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
  describes the resolved intent using only information from the conversation
  context — never use pronouns or relative references like "it", "that one",
  "the same", or "too".
- Examples of correct canonical_phrase values:
    user says "kitchen too" after turning on living room light
      → canonical_phrase: "turn on kitchen island light"
    user says "dim it a bit"
      → canonical_phrase: "dim living room ceiling light to 50 percent"
    user says "run movie mode"
      → canonical_phrase: "run movie mode scene"
- This phrase is recorded for future intent matching — accuracy matters.
"""

# ---------------------------------------------------------------------------
# Tool definitions (Bedrock Converse API toolSpec format)
# ---------------------------------------------------------------------------

_TOOLS: List[Dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "list_devices",
            "description": (
                "Return all registered IoT devices with their id, name, device_type, "
                "and available capabilities (actions). Call this first when you need "
                "to find the correct device_id for a user command."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "list_scenes",
            "description": (
                "Return all active scenes with their id, name, and a description of "
                "what devices they control. Use this when the user mentions a scene."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "execute_device_command",
            "description": (
                "Execute an action on a specific device. Policy enforcement is applied "
                "before any device I/O — a blocked command will return an error. "
                "Always resolve the device_id via list_devices first if uncertain."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "Exact device ID from list_devices.",
                        },
                        "action": {
                            "type": "string",
                            "description": (
                                "Action to perform — must be in the device's capabilities list "
                                "(e.g. 'on', 'off', 'set_brightness', 'toggle')."
                            ),
                        },
                        "params": {
                            "type": "object",
                            "description": (
                                "Optional action parameters, e.g. {\"brightness\": 75} for "
                                "set_brightness. Omit or pass {} for actions that take no params."
                            ),
                        },
                        "canonical_phrase": {
                            "type": "string",
                            "description": (
                                "REQUIRED. A short, self-contained phrase that fully describes "
                                "the resolved intent using conversation context — no pronouns "
                                "or relative references. Example: 'turn on kitchen island light'."
                            ),
                        },
                    },
                    "required": ["device_id", "action", "canonical_phrase"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "execute_scene",
            "description": (
                "Execute a registered scene by its ID. Scenes trigger multiple device "
                "actions simultaneously. Policy enforcement applies to each step."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "scene_id": {
                            "type": "string",
                            "description": "Exact scene ID from list_scenes.",
                        },
                        "canonical_phrase": {
                            "type": "string",
                            "description": (
                                "REQUIRED. A short, self-contained phrase that fully describes "
                                "the resolved intent using conversation context — no pronouns "
                                "or relative references. Example: 'run movie mode scene'."
                            ),
                        },
                    },
                    "required": ["scene_id", "canonical_phrase"],
                }
            },
        }
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_list_devices() -> Dict[str, Any]:
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


def _tool_list_scenes() -> Dict[str, Any]:
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


def _tool_execute_device_command(
    device_id: str,
    action: str,
    canonical_phrase: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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

    # Policy enforcement
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
        results = asyncio.run(execute_steps(steps))
    except Exception as exc:
        logger.exception("Agent device execution error")
        return {"error": f"Execution error: {exc}"}

    result = results[0]
    if not result.success:
        return {"error": result.error, "device_id": device_id}

    # Record behavior and learn the context-resolved canonical phrase.
    # canonical_phrase is the agent's full intent (e.g. "turn on kitchen island light")
    # derived from the conversation — safe to learn even for terse follow-ups like "kitchen too".
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


def _tool_execute_scene(scene_id: str, canonical_phrase: str) -> Dict[str, Any]:
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
        results = asyncio.run(execute_steps(allowed_steps))
    except Exception as exc:
        logger.exception("Agent scene execution error")
        return {"error": f"Scene execution error: {exc}"}

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    # Learn the canonical phrase for each successfully executed device in the scene.
    if canonical_phrase and successes:
        for r in successes:
            if is_configured():
                save_learned_phrase(r.device_id, canonical_phrase, LEARNING_THRESHOLD)
            graph_engine.record_event(r.device_id, r.action, canonical_phrase)
        logger.info("Learned scene phrase for %s (%d devices): %r",
                    scene_id, len(successes), canonical_phrase)

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


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Call the right tool implementation and return a JSON-serializable result."""
    if name == "list_devices":
        return _tool_list_devices()
    if name == "list_scenes":
        return _tool_list_scenes()
    if name == "execute_device_command":
        return _tool_execute_device_command(
            device_id=tool_input["device_id"],
            action=tool_input["action"],
            canonical_phrase=tool_input.get("canonical_phrase", ""),
            params=tool_input.get("params"),
        )
    if name == "execute_scene":
        return _tool_execute_scene(
            scene_id=tool_input["scene_id"],
            canonical_phrase=tool_input.get("canonical_phrase", ""),
        )
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def run_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the Bedrock Converse API agentic loop.

    Args:
        user_message:        The latest message from the user.
        history:             Prior Converse API messages for the session (may be []).
        system_prompt_extra: Optional suffix appended to the system prompt —
                             used by the SMS handler to enforce brevity.

    Returns:
        (reply_text, updated_history)
        reply_text      — the agent's final text response.
        updated_history — the full updated message list to persist.
    """
    import boto3

    client = boto3.client("bedrock-runtime", region_name=_REGION)
    system_text = _SYSTEM_PROMPT + system_prompt_extra if system_prompt_extra else _SYSTEM_PROMPT

    # Append the new user turn
    messages = list(history) + [
        {"role": "user", "content": [{"text": user_message}]}
    ]

    for round_idx in range(_MAX_TOOL_ROUNDS):
        resp = client.converse(
            modelId=_MODEL_ID,
            system=[{"text": system_text}],
            messages=messages,
            toolConfig={"tools": _TOOLS},
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )

        stop_reason: str = resp["stopReason"]
        assistant_message: Dict[str, Any] = resp["output"]["message"]

        # Always append the assistant turn to the history
        messages.append(assistant_message)

        if stop_reason == "end_turn":
            # Extract text from the final assistant message
            text_parts = [
                block["text"]
                for block in assistant_message.get("content", [])
                if "text" in block
            ]
            reply = " ".join(text_parts).strip() or "(no response)"
            logger.info(
                "Agent finished: rounds=%d session_messages=%d",
                round_idx + 1,
                len(messages),
            )
            return reply, messages

        if stop_reason == "tool_use":
            # Build tool result message (role=user with toolResult blocks)
            tool_results = []
            for block in assistant_message.get("content", []):
                if "toolUse" not in block:
                    continue
                tool_use = block["toolUse"]
                tool_id = tool_use["toolUseId"]
                tool_name = tool_use["name"]
                tool_input = tool_use.get("input", {})

                logger.info("Agent calling tool: %s(%s)", tool_name, json.dumps(tool_input))
                result = _dispatch_tool(tool_name, tool_input)
                logger.info("Tool %s result: %s", tool_name, json.dumps(result, default=str))

                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content": [{"json": result}],
                    }
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason (max_tokens, etc.)
        logger.warning("Agent unexpected stopReason=%s at round %d", stop_reason, round_idx)
        text_parts = [
            block["text"]
            for block in assistant_message.get("content", [])
            if "text" in block
        ]
        reply = " ".join(text_parts).strip() or "I reached my response limit. Please try again."
        return reply, messages

    # Exceeded max rounds
    logger.error("Agent exceeded %d tool rounds — aborting", _MAX_TOOL_ROUNDS)
    return "I was unable to complete the request within the allowed steps.", messages
