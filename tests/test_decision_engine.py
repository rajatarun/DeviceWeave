"""
Unit tests for decision_engine.compute_score and validate_execution.

compute_score() pins the adaptive-alpha behaviour documented in the module
docstring:
    alpha = 0.9  when total_events <  _MIN_HISTORY_EVENTS (10)
    alpha = 0.5  when total_events >= _MIN_HISTORY_EVENTS
    final = alpha * cosine + (1 - alpha) * behavior, rounded to 4 places.

behavior_engine.score() and graph_engine.query_behavior_history() are
monkeypatched so the test only exercises decision_engine's own arithmetic.
"""

import decision_engine


DEVICE = {
    "id": "office_light",
    "name": "Office Light",
    "capabilities": ["turn_on", "turn_off", "set_brightness"],
}


def _stub_history(monkeypatch, total_events: int):
    monkeypatch.setattr(
        decision_engine.graph_engine,
        "query_behavior_history",
        lambda **kwargs: {"matching": 0, "total": total_events},
    )


def _stub_behavior_score(monkeypatch, value: float):
    monkeypatch.setattr(decision_engine.behavior_engine, "score", lambda *a, **k: value)


def test_compute_score_alpha_is_point9_below_min_history_events(monkeypatch):
    _stub_history(monkeypatch, total_events=decision_engine._MIN_HISTORY_EVENTS - 1)
    _stub_behavior_score(monkeypatch, 0.6)

    final, cosine, behavior = decision_engine.compute_score(
        cosine_score=0.8, device=DEVICE, action="turn_on", context={"hour": 10}
    )

    assert cosine == 0.8
    assert behavior == 0.6
    # alpha=0.9: 0.9*0.8 + 0.1*0.6 = 0.78
    assert final == 0.78


def test_compute_score_alpha_is_point5_at_min_history_events(monkeypatch):
    _stub_history(monkeypatch, total_events=decision_engine._MIN_HISTORY_EVENTS)
    _stub_behavior_score(monkeypatch, 0.6)

    final, cosine, behavior = decision_engine.compute_score(
        cosine_score=0.8, device=DEVICE, action="turn_on", context={"hour": 10}
    )

    # alpha=0.5 at exactly the threshold (>= 10, not just > 10)
    assert final == 0.7


def test_compute_score_alpha_is_point5_above_min_history_events(monkeypatch):
    _stub_history(monkeypatch, total_events=decision_engine._MIN_HISTORY_EVENTS + 5)
    _stub_behavior_score(monkeypatch, 0.6)

    final, _, _ = decision_engine.compute_score(
        cosine_score=0.8, device=DEVICE, action="turn_on", context={"hour": 10}
    )

    assert final == 0.7


def test_compute_score_rounds_to_four_places(monkeypatch):
    _stub_history(monkeypatch, total_events=0)
    _stub_behavior_score(monkeypatch, 0.5)

    # final = alpha*cosine + (1.0-alpha)*behavior computed in floating point:
    # 0.9*0.8135 + (1.0-0.9)*0.5 == 0.7821499999999999 (NOT the mathematically
    # exact 0.78215, because 1.0-0.9 != 0.1 in binary floating point) ->
    # round(..., 4) == 0.7821. This pins the engine's actual rounding
    # behaviour, including this float-representation quirk, rather than the
    # mathematically "obvious" 0.7822 a naive reader would expect.
    final, cosine, behavior = decision_engine.compute_score(
        cosine_score=0.8135, device=DEVICE, action="turn_on", context={"hour": 10}
    )

    assert final == 0.7821
    assert isinstance(final, float)


def test_compute_score_uses_current_context_hour_when_none_passed(monkeypatch):
    # context=None → compute_score must fall back to behavior_engine.current_context()
    _stub_behavior_score(monkeypatch, 0.5)
    calls = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        return {"matching": 0, "total": 0}

    monkeypatch.setattr(decision_engine.graph_engine, "query_behavior_history", fake_history)
    monkeypatch.setattr(
        decision_engine.behavior_engine, "current_context", lambda: {"hour": 5}
    )

    decision_engine.compute_score(0.9, DEVICE, "turn_on", context=None)

    assert calls[0]["hour"] == 5


# ---------------------------------------------------------------------------
# validate_execution
# ---------------------------------------------------------------------------

def test_validate_execution_rejects_below_threshold():
    allowed, reason = decision_engine.validate_execution(
        DEVICE, "turn_on", final_score=0.3, threshold=0.7
    )
    assert allowed is False
    assert "below threshold" in reason
    assert "office_light" in reason


def test_validate_execution_accepts_at_threshold():
    # Threshold check is `<`, so exactly-at-threshold must pass.
    allowed, reason = decision_engine.validate_execution(
        DEVICE, "turn_on", final_score=0.7, threshold=0.7
    )
    assert allowed is True
    assert reason == ""


def test_validate_execution_rejects_unsupported_capability():
    allowed, reason = decision_engine.validate_execution(
        DEVICE, "set_color", final_score=0.95, threshold=0.7
    )
    assert allowed is False
    assert "does not support" in reason
    assert "set_color" in reason


def test_validate_execution_accepts_supported_capability_above_threshold():
    allowed, reason = decision_engine.validate_execution(
        DEVICE, "set_brightness", final_score=0.95, threshold=0.7
    )
    assert allowed is True
    assert reason == ""


def test_classify_intent_direct_action_verb():
    assert decision_engine.classify_intent("turn on the office light") == "direct"


def test_classify_intent_behavioral_cue():
    assert decision_engine.classify_intent("it's too hot in here") == "behavior"


def test_classify_intent_falls_back_to_scene():
    # No action verb, no behavioral cue keyword -> scene routing hint.
    assert decision_engine.classify_intent("starting work") == "scene"
