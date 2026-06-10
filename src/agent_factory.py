"""
Agent factory for selecting between Bedrock and Gemini conversational agents.

Environment variable: AGENT_PROVIDER (default: "auto")
  - auto:     Bedrock if internet reachable, else Gemini
  - bedrock:  Always use Bedrock Converse API
  - gemini:   Always use Gemini Live API

Both agents implement the same interface:
  async run_agent(user_message, history, system_prompt_extra="")
    → (reply_text, updated_history)

History format:
  Bedrock: [{"role": "user"|"assistant", "content": [{"text": ...}]}, ...]
  Gemini:  [{"role": "user"|"model", "parts": [{"text": ...}]}, ...]

The factory normalizes history format between providers.
"""

import logging
import os
import time
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


async def run_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Run the conversational agent via selected provider.

    Args:
        user_message:        The latest message from the user.
        history:             Prior messages (format depends on provider).
        system_prompt_extra: Optional suffix for system prompt.

    Returns:
        (reply_text, updated_history) — agent response and updated message history.
    """
    t0 = time.monotonic()
    try:
        provider = _select_provider()
    except Exception as exc:
        logger.exception("Provider selection failed")
        raise
    logger.info(
        "Provider selected: %s elapsed_ms=%.0f",
        provider, (time.monotonic() - t0) * 1000,
    )

    history = _normalize_history(history, provider)

    t1 = time.monotonic()
    try:
        if provider == "gemini":
            result = await _run_gemini_agent(user_message, history, system_prompt_extra)
        else:
            result = await _run_bedrock_agent(user_message, history, system_prompt_extra)
    except Exception as exc:
        logger.exception(
            "Agent run failed: provider=%s elapsed_ms=%.0f",
            provider, (time.monotonic() - t1) * 1000,
        )
        raise
    logger.info(
        "Agent run completed: provider=%s elapsed_ms=%.0f",
        provider, (time.monotonic() - t1) * 1000,
    )
    return result


async def _run_bedrock_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run Bedrock Converse API agent."""
    from bedrock_agent import run_agent as bedrock_run_agent

    return await bedrock_run_agent(user_message, history, system_prompt_extra)


async def _run_gemini_agent(
    user_message: str,
    history: List[Dict[str, Any]],
    system_prompt_extra: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Run Gemini Live API agent (async)."""
    from gemini_agent import run_agent as gemini_run_agent

    # Gemini agent is async
    return await gemini_run_agent(user_message, history, system_prompt_extra)


def _normalize_history(
    history: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    """
    Convert persisted session history to the selected provider's format.

    With AGENT_PROVIDER=auto the provider can flip between invocations of the
    same session (the internet probe is re-evaluated every 60 s), so history
    saved by one provider may be replayed into the other. Passing the wrong
    shape fails request validation and the agent 502s before any tool runs.

    Turns already in the target format are passed through untouched. Foreign
    turns are reduced to their text content with roles remapped
    (assistant ↔ model, content ↔ parts); tool-call artefacts are dropped and
    consecutive same-role turns merged so Bedrock's strict role alternation
    still holds.
    """
    target_is_gemini = provider == "gemini"
    if all(_matches_provider(turn, target_is_gemini) for turn in history):
        return history

    normalized: List[Dict[str, Any]] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if isinstance(turn.get("parts"), list):  # Gemini format
            texts = [p.get("text") for p in turn["parts"]
                     if isinstance(p, dict) and p.get("text")]
            role = "assistant" if role == "model" else role
        elif isinstance(turn.get("content"), list):  # Bedrock format
            texts = [b.get("text") for b in turn["content"]
                     if isinstance(b, dict) and b.get("text")]
        else:
            continue
        if not texts or role not in ("user", "assistant"):
            continue
        text = " ".join(texts)

        out_role = ("model" if role == "assistant" else "user") if target_is_gemini else role
        if normalized and normalized[-1]["role"] == out_role:
            # Merge consecutive same-role turns (tool turns dropped in between)
            key = "parts" if target_is_gemini else "content"
            normalized[-1][key].append({"text": text})
        elif target_is_gemini:
            normalized.append({"role": out_role, "parts": [{"text": text}]})
        else:
            normalized.append({"role": out_role, "content": [{"text": text}]})

    if len(normalized) != len(history):
        logger.info(
            "History normalized for provider=%s: %d turns in, %d turns out",
            provider, len(history), len(normalized),
        )
    return normalized


def _matches_provider(turn: Any, target_is_gemini: bool) -> bool:
    """Return True if a history turn is already in the target provider's format."""
    if not isinstance(turn, dict):
        return False
    if target_is_gemini:
        return turn.get("role") in ("user", "model") and isinstance(turn.get("parts"), list)
    return turn.get("role") in ("user", "assistant") and isinstance(turn.get("content"), list)


def _select_provider() -> str:
    """
    Select agent provider based on AGENT_PROVIDER env var.

    auto:    Bedrock if internet reachable, else Gemini
    bedrock: Always Bedrock
    gemini:  Always Gemini
    """
    provider = os.environ.get("AGENT_PROVIDER", "auto").lower()

    if provider == "auto":
        # Use network probe to decide
        from llm_provider import _is_nat_running
        return "bedrock" if _is_nat_running() else "gemini"

    if provider == "bedrock":
        return "bedrock"

    if provider == "gemini":
        return "gemini"

    raise ValueError(
        f"Unknown AGENT_PROVIDER={provider!r}. Supported: auto, bedrock, gemini"
    )
