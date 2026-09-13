"""
Unit tests for behavior_engine.score().

graph_engine.query_behavior_history is monkeypatched directly (rather than
relying on the global autouse stub's zero-history default) so each test can
control matching/total explicitly; weather is passed via an explicit
`context` dict so no network call is involved either.
"""

import behavior_engine

LIGHT = {"id": "office_light", "name": "Office Light", "capabilities": ["set_brightness"]}
FAN = {"id": "office_fan", "name": "Office Fan", "capabilities": ["turn_on", "turn_off"]}

_NEUTRAL_CONTEXT = {
    "hour": 8,  # neutral for light: not dark-hours (7-18), not midday (10-16)
    "is_hot": False,
    "is_cold": False,
    "is_humid": False,
    "is_overcast": False,
    "temperature_c": None,
}


def _stub_history(monkeypatch, matching: int, total: int):
    monkeypatch.setattr(
        behavior_engine.graph_engine,
        "query_behavior_history",
        lambda **kwargs: {"matching": matching, "total": total},
    )


def test_score_is_neutral_point5_with_no_history_and_neutral_weather(monkeypatch):
    _stub_history(monkeypatch, matching=0, total=0)

    result = behavior_engine.score(LIGHT, "set_brightness", context=_NEUTRAL_CONTEXT)

    assert result == 0.5


def test_score_neutral_with_no_history_holds_regardless_of_device_weather_weight(monkeypatch):
    # DEVICE_MODELS gives fan a much larger weather_weight (0.4) than light
    # (0.2); with truly neutral weather the blend must still land on 0.5
    # because _weather_score() itself returns 0.5 for a neutral context.
    _stub_history(monkeypatch, matching=0, total=0)

    result = behavior_engine.score(FAN, "turn_on", context=_NEUTRAL_CONTEXT)

    assert result == 0.5


def test_score_blends_weather_signal_when_no_history(monkeypatch):
    _stub_history(monkeypatch, matching=0, total=0)
    hot_context = dict(_NEUTRAL_CONTEXT, is_hot=True)

    result = behavior_engine.score(FAN, "turn_on", context=hot_context)

    # fan profile: weather_weight=0.4, weather_score(is_hot)=0.82
    # combined = 0.4*0.82 + 0.6*0.5 = 0.628
    assert result == 0.628


def test_score_combines_time_frequency_and_weather_with_history(monkeypatch):
    _stub_history(monkeypatch, matching=5, total=10)

    result = behavior_engine.score(FAN, "turn_on", context=_NEUTRAL_CONTEXT)

    # fan profile: time_weight=0.2, frequency_weight=0.4, weather_weight=0.4
    # time_score      = min(0.9, 0.5 + (5/10)*0.8) = 0.9
    # frequency_score = min(0.95, 0.5 + (5/10)*0.5) = 0.75
    # weather_score(neutral) = 0.5
    # combined = 0.2*0.9 + 0.4*0.75 + 0.4*0.5 = 0.68
    assert result == 0.68


def test_score_defaults_context_to_current_context_when_none(monkeypatch):
    _stub_history(monkeypatch, matching=0, total=0)
    monkeypatch.setattr(
        behavior_engine, "current_context",
        lambda: dict(_NEUTRAL_CONTEXT, hour=8),
    )

    # Should not raise and should not attempt any real weather/graph I/O.
    result = behavior_engine.score(LIGHT, "set_brightness", context=None)

    assert result == 0.5


def test_infer_device_class_prefers_capability_over_name():
    device = {"id": "d1", "name": "Kitchen Fan", "capabilities": ["set_brightness"]}
    # Name says "fan" but set_brightness capability makes it a light per
    # infer_device_class()'s documented capability-first priority.
    assert behavior_engine.infer_device_class(device) == "light"


def test_infer_device_class_falls_back_to_name_keyword():
    device = {"id": "d2", "name": "Bedroom Lamp", "capabilities": ["turn_on"]}
    assert behavior_engine.infer_device_class(device) == "light"


def test_infer_device_class_default_when_no_signal_matches():
    device = {"id": "d3", "name": "Mystery Box", "capabilities": ["turn_on"]}
    assert behavior_engine.infer_device_class(device) == "default"
