"""LLM resolver output handling.

Production logged ``Unterminated string`` JSON tracebacks for "for every
device, report its current state" commands: the model's output was cut at
max_tokens and the resolver parsed it anyway.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

import llm_resolver
from llm_provider.base import STOP_MAX_TOKENS

_DEVICES = [
    {"id": f"{i:040x}", "name": f"Plug {i}", "device_type": "SmartPlug",
     "capabilities": ["turn_on", "turn_off", "get_status"]}
    for i in range(30)
]


class _FakeLLM:
    model_id = "fake"

    def __init__(self, text: str, stop: Optional[str] = "end_turn") -> None:
        self._text = text
        self._stop = stop
        self.last_stop_reason = None
        self.budgets: List[int] = []

    def invoke(self, system_prompt: str, user_message: str, max_tokens: int = 512) -> str:
        self.budgets.append(max_tokens)
        self.last_stop_reason = self._stop
        return self._text


def _use(monkeypatch, llm: Any) -> None:
    monkeypatch.setattr(llm_resolver, "get_llm_provider", lambda: llm)
    monkeypatch.setattr(llm_resolver.weather_client, "summary_line", lambda: "Weather: n/a")


def test_truncated_output_is_reported_as_truncation_not_a_json_error(monkeypatch, caplog):
    llm = _FakeLLM('{"devices": [\n  {"device_id": "0000000000', stop=STOP_MAX_TOKENS)
    _use(monkeypatch, llm)
    with caplog.at_level(logging.ERROR, logger="llm_resolver"):
        assert llm_resolver.llm_resolve("for every device, report its state", None, _DEVICES) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("truncated at max_tokens" in m for m in messages)
    assert not any("Unterminated string" in m for m in messages)
    assert all(r.exc_info is None for r in caplog.records)


def test_fenced_output_with_prose_is_parsed(monkeypatch):
    text = (
        "Here is the plan:\n```json\n"
        '{"devices": [{"device_id": "a", "action": "get_status"}], '
        '"confidence": 0.9, "reasoning": "all"}\n```\nDone.'
    )
    _use(monkeypatch, _FakeLLM(text))
    result = llm_resolver.llm_resolve("status of a", None, _DEVICES)
    assert result["devices"] == [{"device_id": "a", "action": "get_status"}]
    assert result["confidence"] == 0.9


def test_budget_covers_a_step_per_device_with_long_ids(monkeypatch):
    llm = _FakeLLM('{"devices": [], "confidence": 0, "reasoning": ""}')
    _use(monkeypatch, llm)
    llm_resolver.llm_resolve("every device", None, _DEVICES)
    # One step with a 40-hex id is ~35-40 tokens; the old 20/device was not.
    assert llm.budgets[0] >= 35 * len(_DEVICES)
    assert llm_resolver._output_budget(_DEVICES * 100) == llm_resolver._MAX_OUTPUT_TOKENS
