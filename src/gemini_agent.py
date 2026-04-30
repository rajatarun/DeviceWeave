"""
Conversational IoT agent using Google Gemini Live API.

Design:
- The agent drives a live conversation: it calls tools (list_devices, list_scenes,
  execute_device_command, execute_scene) until it reaches end_turn.
- Uses Google's Gemini Live API for low-latency, streaming responses with
  native tool/function calling support.
- Policy enforcement lives inside execute_device_command / execute_scene.
- The caller is responsible for loading and saving session history
  (conversation_store.py). This module is purely functional.
- Maximum 10 tool-call rounds per invocation to prevent runaway loops.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MODEL_ID: str = os.environ.get("GEMINI_LIVE_MODEL", os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview"))
_MAX_TOOL_ROUNDS: int = 10
_RESPONSE_TIMEOUT_SECS: int = int(os.environ.get("GEMINI_TIMEOUT_SECS", "25"))

# Cached on first call; reused on warm Lambda starts to avoid repeating
# the Secrets Manager round-trip and SDK client construction.
_genai_client = None
_resolved_live_model_id: Optional[str] = None


def _get_genai_client():
    """Return a cached genai.Client, initialising it on the first call."""
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    try:
        from google import genai
        logger.info("Gemini SDK imported successfully")
    except ImportError:
        raise RuntimeError(
            "google-genai SDK not installed. Install with: pip install google-genai"
        )

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
    except Exception as exc:
        logger.exception("Failed to load Gemini API key from %s", secret_name)
        raise RuntimeError(f"Failed to load Gemini API key from {secret_name}: {exc}")

    _genai_client = genai.Client(api_key=api_key)
    logger.info("Gemini client initialised and cached")
    return _genai_client


def _resolve_live_model_id(client) -> str:
    """Return a Gemini model ID that supports the Live API bidi method.

    Preference order:
      1) Configured GEMINI_MODEL if it supports bidiGenerateContent.
      2) First available model that supports bidiGenerateContent.
    """
    global _resolved_live_model_id
    preferred = _MODEL_ID
    if _resolved_live_model_id:
        logger.info(
            "Gemini Live model selected from cache: %s (configured: %s)",
            _resolved_live_model_id,
            preferred,
        )
        return _resolved_live_model_id

    bidi_models: List[str] = []

    try:
        for model in client.models.list():
            methods = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", []
            )
            if "bidiGenerateContent" in methods:
                name = getattr(model, "name", "")
                # SDK can return names like "models/gemini-2.0-flash".
                model_id = name.split("/", 1)[1] if name.startswith("models/") else name
                if model_id:
                    bidi_models.append(model_id)
    except Exception:
        logger.exception("Failed to list Gemini models; using configured model: %s", preferred)
        _resolved_live_model_id = preferred
        return _resolved_live_model_id

    logger.info("Gemini Live-compatible models: %s", bidi_models)

    if not bidi_models:
        logger.warning(
            "No Gemini models reported bidiGenerateContent support; using configured model: %s",
            preferred,
        )
        _resolved_live_model_id = preferred
        return _resolved_live_model_id

    if preferred in bidi_models:
        _resolved_live_model_id = preferred
    else:
        _resolved_live_model_id = bidi_models[0]
        logger.warning(
            "Configured GEMINI_MODEL=%s does not support Live API bidiGenerateContent; "
            "falling back to %s",
            preferred,
            _resolved_live_model_id,
        )

    logger.info(
        "Gemini Live model selected: %s (configured: %s)",
        _resolved_live_model_id,
        preferred,
    )

    return _resolved_live_model_id

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
"""

# ---------------------------------------------------------------------------
# Tool definitions (Gemini Function Calling format)
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "list_devices",
        "description": (
            "Return all registered IoT devices with their id, name, device_type, "
            "and available capabilities (actions). Call this first when you need "
            "to find the correct device_id for a user command."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_scenes",
        "description": (
            "Return all active scenes with their id, name, and a description of "
            "what devices they control. Use this when the user mentions a scene."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "execute_device_command",
        "description": (
            "Execute an action on a specific device. Policy enforcement is applied "
            "before any device I/O — a blocked command will return an error. "
            "Always resolve the device_id via list_devices first if uncertain."
        ),
        "parameters": {
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
        },
    },
    {
        "name": "execute_scene",
        "description": (
            "Execute a registered scene by its ID. Scenes trigger multiple device "
            "actions simultaneously. Policy enforcement applies to each step."
        ),
        "parameters": {
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
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations (same as bedrock_agent.py)
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
# Live API Agentic Loop
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the Gemini Live API agentic loop.

    Args:
        user_message:        The latest message from the user.
        history:             Prior messages for the session (may be []).
                             Format: [{"role": "user"|"model", "parts": [{"text": ...}]}, ...]
        system_prompt_extra: Optional suffix appended to the system prompt.

    Returns:
        (reply_text, updated_history)
        reply_text      — the agent's final text response.
        updated_history — the full updated message list to persist.
    """
    from google import genai  # fast sys.modules lookup after first call
    client = _get_genai_client()
    model_id = _resolve_live_model_id(client)
    logger.info("Gemini agent invoked: model=%s history_turns=%d", model_id, len(history))
    system_text = _SYSTEM_PROMPT + system_prompt_extra if system_prompt_extra else _SYSTEM_PROMPT

    # Build messages for Gemini
    messages = list(history) + [
        {"role": "user", "parts": [{"text": user_message}]}
    ]

    # Configure Live API session with tool definitions
    config = genai.types.LiveConnectConfig(
        generation_config=genai.types.GenerationConfig(
            temperature=0.2,
            max_output_tokens=1024,
        ),
        system_instruction=system_text,
        tools=[genai.types.Tool(function_declarations=_TOOLS)],
    )

    logger.info(
        "Connecting to Gemini Live API: model=%s timeout=%ds", model_id, _RESPONSE_TIMEOUT_SECS
    )
    try:
        async with asyncio.timeout(_RESPONSE_TIMEOUT_SECS):
            async with client.aio.live.connect(model=model_id, config=config) as session:
                logger.info("Gemini Live API session established")

                # Replay prior history turns so the model has conversation context,
                # matching Bedrock which passes the full messages list on every call.
                for i, turn in enumerate(history):
                    role = turn.get("role", "user")
                    parts = turn.get("parts", [])
                    text = parts[0].get("text", "") if parts else ""
                    if text:
                        logger.debug("Replaying history turn %d role=%s", i, role)
                        await session.send(
                            genai.types.Content(
                                role=role,
                                parts=[genai.types.Part(text=text)],
                            )
                        )

                # Send the current user message
                logger.info("Sending user message to Gemini session")
                await session.send(user_message)

                final_response = ""
                tool_round = 0

                logger.info("Waiting for Gemini response stream")
                async for server_message in session.response_stream:
                    # Handle tool calls
                    if server_message.tool_calls:
                        for tool_call in server_message.tool_calls:
                            tool_name = tool_call.function_name
                            tool_input = tool_call.args

                            logger.info("Agent calling tool: %s(%s)", tool_name, json.dumps(tool_input))
                            result = _dispatch_tool(tool_name, tool_input)
                            logger.info("Tool %s result: %s", tool_name, json.dumps(result, default=str))

                            # Send tool result back to the model
                            await session.send(
                                genai.types.Content(
                                    role="user",
                                    parts=[
                                        genai.types.Part(
                                            function_response=genai.types.FunctionResponse(
                                                name=tool_name,
                                                response=result,
                                            )
                                        )
                                    ],
                                )
                            )

                            tool_round += 1
                            if tool_round >= _MAX_TOOL_ROUNDS:
                                logger.error(
                                    "Agent exceeded %d tool rounds — aborting", _MAX_TOOL_ROUNDS
                                )
                                return "I was unable to complete the request within the allowed steps.", messages

                    # Handle text responses
                    if hasattr(server_message, "text") and server_message.text:
                        logger.debug("Received text chunk: %d chars", len(server_message.text))
                        final_response = server_message.text

                logger.info(
                    "Gemini response stream ended: has_text=%s tool_rounds=%d",
                    bool(final_response),
                    tool_round,
                )

                # Update message history with final response
                messages.append(
                    {"role": "model", "parts": [{"text": final_response}]}
                )

                logger.info(
                    "Agent finished: rounds=%d session_messages=%d",
                    tool_round + 1,
                    len(messages),
                )

                return final_response or "(no response)", messages

    except TimeoutError:
        logger.error("Gemini Live API timed out after %ds", _RESPONSE_TIMEOUT_SECS)
        raise RuntimeError(f"Gemini Live API timed out after {_RESPONSE_TIMEOUT_SECS}s")
    except Exception as exc:
        logger.exception("Gemini Live API error")
        raise RuntimeError(f"Gemini Live API failed: {exc}") from exc
