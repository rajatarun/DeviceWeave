"""
Conversational IoT agent using Google Gemini API (generate_content with tool calling).

Design:
- Uses standard generate_content API (not Live/streaming) for text conversations.
- The agent drives an agentic loop: it calls tools until it produces a final text reply.
- Tool dispatch runs in a thread executor so that asyncio.run() inside sync tool
  handlers (execution_planner) does not conflict with the running event loop.
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

_MODEL_ID: str = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
_MAX_TOOL_ROUNDS: int = 10
_RESPONSE_TIMEOUT_SECS: int = int(os.environ.get("GEMINI_TIMEOUT_SECS", "25"))
_INIT_CACHE_TTL_SECS: int = 3600  # 1 hour
_INIT_CACHE_KEY: str = "__gemini_init__"

# L1 in-memory cache — survives warm Lambda restarts within the same container.
# _genai_client cannot be serialized; it is always rebuilt from Secrets Manager
# on a cold start. _resolved_model_id is also backed by DynamoDB (see below).
_genai_client = None
_resolved_model_id: Optional[str] = None


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


def _load_cached_model_id() -> Optional[str]:
    """Read the resolved model ID from DynamoDB (L2 cache). Returns None on miss or error."""
    table_name = os.environ.get("CONVERSATION_TABLE_NAME", "")
    if not table_name:
        return None
    try:
        import boto3
        resp = boto3.resource("dynamodb").Table(table_name).get_item(
            Key={"session_id": _INIT_CACHE_KEY}
        )
        item = resp.get("Item")
        return item.get("model_id") if item else None
    except Exception:
        logger.warning("DynamoDB model-ID cache read failed; will resolve from API")
        return None


def _write_cached_model_id(model_id: str) -> None:
    """Persist the resolved model ID to DynamoDB with a 1-hour TTL."""
    import time
    table_name = os.environ.get("CONVERSATION_TABLE_NAME", "")
    if not table_name:
        return
    try:
        import boto3
        boto3.resource("dynamodb").Table(table_name).put_item(Item={
            "session_id": _INIT_CACHE_KEY,
            "model_id": model_id,
            "ttl": int(time.time()) + _INIT_CACHE_TTL_SECS,
        })
        logger.info("Gemini model ID written to DynamoDB cache (1h TTL): %s", model_id)
    except Exception:
        logger.warning("DynamoDB model-ID cache write failed; continuing without caching")


def _resolve_model_id(client) -> str:
    """Return the Gemini model ID to use, with a two-tier cache.

    L1 — module-level global: survives warm Lambda restarts in the same container.
    L2 — DynamoDB item with 1h TTL: survives cold starts and is shared across
         all container instances so only one instance pays the models.list() cost.
    """
    global _resolved_model_id
    preferred = _MODEL_ID

    # L1: in-memory (warm start)
    if _resolved_model_id:
        logger.info("Gemini model from memory cache: %s", _resolved_model_id)
        return _resolved_model_id

    # L2: DynamoDB (cold start, cross-container)
    cached = _load_cached_model_id()
    if cached:
        logger.info("Gemini model from DynamoDB cache: %s", cached)
        _resolved_model_id = cached
        return _resolved_model_id

    # L3: Discover via Gemini models.list() then populate both cache tiers
    try:
        for model in client.models.list():
            methods = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", []
            )
            if "generateContent" in methods:
                name = getattr(model, "name", "")
                mid = name.split("/", 1)[1] if name.startswith("models/") else name
                if mid == preferred:
                    _resolved_model_id = preferred
                    _write_cached_model_id(_resolved_model_id)
                    logger.info("Gemini model verified and cached: %s", _resolved_model_id)
                    return _resolved_model_id
        logger.warning(
            "Model %s not found in generateContent-capable list; using as configured", preferred
        )
    except Exception:
        logger.exception("Failed to list Gemini models; using configured model: %s", preferred)

    _resolved_model_id = preferred
    _write_cached_model_id(_resolved_model_id)
    return _resolved_model_id


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
    {
        "name": "get_device_history",
        "description": (
            "Return the historical behavior for a device — most frequent actions and, "
            "when an action is specified, whether it is typical at the current hour. "
            "Call this before executing a command when the user's request involves a "
            "preference or ambiguity (e.g. 'dim it', 'the usual', 'like always'). "
            "Also useful to understand what a device is normally used for."
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
                        "Optional — the action you intend to execute. When provided, "
                        "the response includes whether this action is typical at the "
                        "current time of day."
                    ),
                },
            },
            "required": ["device_id"],
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


def _tool_get_device_history(device_id: str, action: str = "") -> Dict[str, Any]:
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
            "typical_at_this_hour": counts["matching"] > 0,
            "matching_events": counts["matching"],
            "total_events": counts["total"],
        }

    return result


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
    if name == "get_device_history":
        return _tool_get_device_history(
            device_id=tool_input["device_id"],
            action=tool_input.get("action", ""),
        )
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Agentic Loop
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the Gemini agentic loop using generate_content with function calling.

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
    model_id = _resolve_model_id(client)
    logger.info("Gemini agent invoked: model=%s history_turns=%d", model_id, len(history))
    system_text = _SYSTEM_PROMPT + system_prompt_extra if system_prompt_extra else _SYSTEM_PROMPT

    # Build message list: history + current user turn
    messages: List[Any] = list(history) + [
        {"role": "user", "parts": [{"text": user_message}]}
    ]

    config = genai.types.GenerateContentConfig(
        system_instruction=system_text,
        tools=[genai.types.Tool(function_declarations=_TOOLS)],
        temperature=0.2,
        max_output_tokens=1024,
    )

    tool_round = 0
    final_response = ""
    loop = asyncio.get_event_loop()

    try:
        async with asyncio.timeout(_RESPONSE_TIMEOUT_SECS):
            while True:
                logger.info(
                    "Calling Gemini generate_content: model=%s tool_round=%d", model_id, tool_round
                )
                response = await client.aio.models.generate_content(
                    model=model_id,
                    contents=messages,
                    config=config,
                )

                # Check for function calls in the response
                function_calls = getattr(response, "function_calls", None) or []

                if not function_calls:
                    # No tool calls — this is the final text response
                    final_response = response.text or ""
                    messages.append({"role": "model", "parts": [{"text": final_response}]})
                    logger.info(
                        "Gemini final response: %d chars tool_rounds=%d",
                        len(final_response),
                        tool_round,
                    )
                    break

                # Append the model's function-call turn to the message history so the
                # next generate_content call has the full context.
                messages.append(response.candidates[0].content)

                # Execute each tool call in a thread executor so that asyncio.run()
                # inside the sync tool handlers doesn't conflict with this event loop.
                tool_response_parts = []
                for fc in function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}
                    logger.info("Agent calling tool: %s(%s)", tool_name, json.dumps(tool_args))
                    result = await loop.run_in_executor(
                        None, _dispatch_tool, tool_name, tool_args
                    )
                    logger.info("Tool %s result: %s", tool_name, json.dumps(result, default=str))
                    tool_response_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                name=tool_name,
                                response=result,
                            )
                        )
                    )

                messages.append(genai.types.Content(role="user", parts=tool_response_parts))

                tool_round += 1
                if tool_round >= _MAX_TOOL_ROUNDS:
                    logger.error("Agent exceeded %d tool rounds — aborting", _MAX_TOOL_ROUNDS)
                    return "I was unable to complete the request within the allowed steps.", messages

    except TimeoutError:
        logger.error("Gemini API timed out after %ds", _RESPONSE_TIMEOUT_SECS)
        raise RuntimeError(f"Gemini API timed out after {_RESPONSE_TIMEOUT_SECS}s")
    except Exception as exc:
        logger.exception("Gemini API error")
        raise RuntimeError(f"Gemini API failed: {exc}") from exc

    logger.info(
        "Agent finished: tool_rounds=%d session_messages=%d", tool_round, len(messages)
    )
    return final_response or "(no response)", messages
