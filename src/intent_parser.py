"""
Deterministic, regex-based intent parser.

No external model calls. Every branch is explicit and testable.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class Intent:
    action: str
    device_query: str
    params: Dict[str, Any] = field(default_factory=dict)


# Ordered list of (compiled_pattern, action_name).
# Earlier patterns take priority over later ones.
_ACTION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(turn\s+on|switch\s+on|power\s+on|enable|activate)\b'), 'turn_on'),
    (re.compile(r'\b(turn\s+off|switch\s+off|power\s+off|disable|deactivate)\b'), 'turn_off'),
    (re.compile(r'\b(toggle|flip)\b'), 'toggle'),
    (re.compile(r'\bset\s+brightness\b|\bbrightness\s+to\b|\bdim\s+to\b|\bdim\b|\bdarken\b'), 'set_brightness'),
    (re.compile(r'\b(status|state|check|is\s+it\s+(on|off)|what\s+is)\b'), 'get_status'),
]

_BRIGHTNESS_PATTERN = re.compile(r'\b(\d{1,3})\s*%?')

# Minimum command length to avoid empty/noise inputs.
_MIN_COMMAND_LENGTH = 3


def parse_intent(command: str) -> Intent:
    """
    Parse a natural language command into a structured Intent.

    Raises ValueError for empty or too-short commands.
    """
    if not command or len(command.strip()) < _MIN_COMMAND_LENGTH:
        raise ValueError(f"Command too short to parse: '{command}'")

    normalized = command.strip().lower()

    action = _extract_action(normalized)
    params = _extract_params(normalized, action)

    # The full normalized command is used as the device query so the resolver
    # can apply TF-based cosine similarity across all tokens.
    return Intent(action=action, device_query=normalized, params=params)


def _extract_action(text: str) -> str:
    for pattern, action in _ACTION_RULES:
        if pattern.search(text):
            return action
    # No recognisable action keyword — default to status query.
    return 'get_status'


def _extract_params(text: str, action: str) -> Dict[str, Any]:
    if action != 'set_brightness':
        return {}

    match = _BRIGHTNESS_PATTERN.search(text)
    if not match:
        # Caller must handle missing brightness parameter.
        return {}

    raw = int(match.group(1))
    # Clamp to valid range.
    return {'brightness': max(0, min(100, raw))}
