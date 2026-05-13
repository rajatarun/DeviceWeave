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
    provider = _select_provider()
    logger.info("Using agent provider: %s", provider)

    if provider == "gemini":
        return await _run_gemini_agent(user_message, history, system_prompt_extra)
    else:
        return await _run_bedrock_agent(user_message, history, system_prompt_extra)


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
