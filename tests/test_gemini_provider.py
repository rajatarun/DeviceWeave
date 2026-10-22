"""Gemini's thinking tokens must not crowd out the answer.

Flash models count thinking against maxOutputTokens. The resolver asks for a
few hundred tokens, so a cap sized for the answer alone could be spent on
thinking. Production logged replies cut off ~35 characters in.
"""
from __future__ import annotations

import io
import json

import pytest

import llm_provider.gemini as gemini
from llm_provider.base import STOP_MAX_TOKENS


@pytest.fixture
def api(monkeypatch):
    sent = []
    reply = {}

    def urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        return io.BytesIO(json.dumps(reply["payload"]).encode())

    monkeypatch.setattr(gemini, "_load_api_key", lambda name: "k")
    monkeypatch.setattr(gemini.urllib.request, "urlopen", urlopen)
    return sent, reply


def test_the_cap_leaves_room_for_thinking(api):
    sent, reply = api
    reply["payload"] = {"candidates": [{"finishReason": "STOP",
                                        "content": {"parts": [{"text": "[]"}]}}]}
    provider = gemini.GeminiLLMProvider()
    assert provider.invoke("sys", "msg", max_tokens=300) == "[]"
    assert sent[0]["generationConfig"]["maxOutputTokens"] == 300 + gemini._THINKING_HEADROOM
    assert gemini._THINKING_HEADROOM >= 1024


def test_a_budget_spent_on_thinking_is_a_truncated_reply_not_a_crash(api, caplog):
    sent, reply = api
    reply["payload"] = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}],
                        "usageMetadata": {"thoughtsTokenCount": 2348}}
    provider = gemini.GeminiLLMProvider()
    with caplog.at_level("WARNING", logger=gemini.logger.name):
        assert provider.invoke("sys", "msg", max_tokens=300) == ""
    assert provider.last_stop_reason == STOP_MAX_TOKENS
    # The one line that says why the reply was empty.
    assert "spent its output budget thinking (2348 thought tokens)" in caplog.text


def test_thought_parts_are_not_returned_as_the_answer(api):
    sent, reply = api
    reply["payload"] = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "let me consider", "thought": True}, {"text": '{"steps": []}'}]}}]}
    assert gemini.GeminiLLMProvider().invoke("sys", "msg") == '{"steps": []}'


def test_a_response_with_no_candidates_is_still_an_error(api):
    sent, reply = api
    reply["payload"] = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(RuntimeError, match="Unexpected Gemini response shape"):
        gemini.GeminiLLMProvider().invoke("sys", "msg")
