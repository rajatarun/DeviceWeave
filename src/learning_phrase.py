"""
Derive a short, device-specific phrase for auto-learning after execution.

Auto-learning must not persist long templated wrapper text (evaluation prompts,
status-report boilerplate, etc.) as if it were a natural device command.
Those strings add generic tokens to one device's TF corpus and poison tier-1
cosine resolution for other devices that share the same template.

Conversational agents supply an explicit canonical_phrase (see DECISIONS.md #30).
REST /execute and tier-2 resolution use this module instead of the raw utterance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from device_resolver import _tokenize

# Shared evaluation / status-report wrappers (lowercased before match).
_TEMPLATE_BOILERPLATE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdo not change anything\b"),
    re.compile(r"\bonly report status\b"),
    re.compile(r"\bfor every device\b"),
    re.compile(r"\breport its state\b"),
    re.compile(r"\bcurrent status of\b"),
)

_MAX_VERBATIM_CHARS = 80
_MAX_VERBATIM_CONTENT_TOKENS = 10

_ACTION_PHRASES: Dict[str, str] = {
    "turn_on": "turn on {name}",
    "turn_off": "turn off {name}",
    "toggle": "toggle {name}",
    "set_brightness": "{name} brightness",
    "get_status": "{name} status",
}


def _has_template_boilerplate(normalized_command: str) -> bool:
    return any(p.search(normalized_command) for p in _TEMPLATE_BOILERPLATE)


def _device_name_tokens(device: Dict[str, Any]) -> set[str]:
    tokens = set(_tokenize(device.get("name") or ""))
    for part in (device.get("id") or "").replace("-", "_").split("_"):
        part = part.lower()
        if len(part) > 2:
            tokens.add(part)
    return tokens


def _mentions_device(normalized_command: str, device: Dict[str, Any]) -> bool:
    command_tokens = set(_tokenize(normalized_command))
    name_tokens = _device_name_tokens(device)
    if not name_tokens:
        return True
    return bool(command_tokens & name_tokens)


def _synthesize_phrase(device: Dict[str, Any], action: str) -> str:
    name = (device.get("name") or device.get("id") or "device").strip().lower()
    template = _ACTION_PHRASES.get(action)
    if template:
        return template.format(name=name)
    return name


def auto_learn_phrase(
    device: Dict[str, Any],
    action: str,
    command: str,
    *,
    resolution_tier: str,
) -> Optional[str]:
    """
    Return a phrase safe to persist for auto-learning, or None to skip learning.

    resolution_tier is \"cosine\" (tier 1) or \"llm\" (tier 2).
    """
    normalized = (command or "").strip().lower()
    if not normalized:
        return None

    if _has_template_boilerplate(normalized):
        return _synthesize_phrase(device, action)

    if resolution_tier == "llm":
        return _synthesize_phrase(device, action)

    content_tokens = _tokenize(normalized)
    if (
        len(normalized) > _MAX_VERBATIM_CHARS
        or len(content_tokens) > _MAX_VERBATIM_CONTENT_TOKENS
    ):
        return _synthesize_phrase(device, action)

    if not _mentions_device(normalized, device):
        return _synthesize_phrase(device, action)

    return normalized
