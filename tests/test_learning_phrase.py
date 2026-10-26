"""Regression tests for auto-learn phrase selection (template poisoning)."""

import device_resolver as dr
from learning_phrase import auto_learn_phrase

DINING = {
    "id": "dining_light",
    "name": "Dining",
    "device_type": "light",
    "capabilities": ["turn_on", "turn_off", "get_status"],
}

HALLWAY = {
    "id": "hallway_light",
    "name": "Hallway",
    "device_type": "light",
    "capabilities": ["turn_on", "turn_off", "get_status"],
}

TEMPLATE_STATUS = (
    "what is the current status of the dining? do not change anything, only report status."
)


def test_llm_tier_template_utterance_synthesizes_short_phrase():
    phrase = auto_learn_phrase(
        DINING, "get_status", TEMPLATE_STATUS, resolution_tier="llm"
    )
    assert phrase == "dining status"
    assert "do not change" not in phrase
    assert "only report" not in phrase


def test_cosine_tier_short_natural_command_kept_verbatim():
    cmd = "turn on dining"
    phrase = auto_learn_phrase(DINING, "turn_on", cmd, resolution_tier="cosine")
    assert phrase == cmd


def test_cosine_tier_template_boilerplate_synthesizes():
    phrase = auto_learn_phrase(
        DINING, "get_status", TEMPLATE_STATUS, resolution_tier="cosine"
    )
    assert phrase == "dining status"


def test_learned_template_phrase_does_not_poison_other_devices(monkeypatch):
    """M1/M2 regression: full template must not steer tier-1 toward Dining."""
    catalog = [DINING, HALLWAY]
    learned = {DINING["id"]: ["dining status"]}

    monkeypatch.setattr(dr, "_get_active_catalog", lambda: catalog)
    monkeypatch.setattr(dr, "_get_learned_phrases", lambda: learned)

    hallway_query = (
        "what is the current status of the hallway? "
        "do not change anything, only report status."
    )
    device, score = dr.resolve_device(hallway_query)
    assert device is not None
    assert device["id"] == "hallway_light"
    assert score > 0

    poison_phrase = TEMPLATE_STATUS
    learned_poison = {DINING["id"]: [poison_phrase]}
    monkeypatch.setattr(dr, "_get_learned_phrases", lambda: learned_poison)

    device_poison, _ = dr.resolve_device(hallway_query)
    assert device_poison["id"] == "dining_light"
