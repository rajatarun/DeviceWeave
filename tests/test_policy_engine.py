"""
Unit tests for policy_engine.evaluator.compute_verdict() — BLOCK/MODIFY/ALLOW
precedence, using the three example rules from the README's "Rules you can
author" section:

    "Don't turn on fan when it's cold"        -> block, fan, temperature < 65
    "Don't turn on lights between 11am-3pm"   -> block, light, 11 <= time_hour <= 15
    "Dim lights after 10 PM"                  -> modify, light, time_hour >= 22, brightness: 20

Rule shape matches what policy_authoring/validator.py actually returns
(validate_policy()'s return dict — rule_id/scope/conditions/action/confidence)
and what evaluator.compute_verdict() actually reads (policy["rule_id"],
policy["scope"]["device_type"], policy["conditions"], policy["action"]["type"|"reason"|"params"]).
compute_verdict() is pure and I/O-free, so no fixtures/mocks are needed.
"""

from policy_engine.evaluator import compute_verdict

FAN_COLD_BLOCK = {
    "rule_id": "rule-fan-cold",
    "scope": {"device_type": "fan"},
    "conditions": [{"field": "temperature", "operator": "<", "value": 65}],
    "action": {"type": "block", "reason": "Too cold to run fan", "params": {}},
    "confidence": 0.92,
}

LIGHT_MIDDAY_BLOCK = {
    "rule_id": "rule-light-midday",
    "scope": {"device_type": "light"},
    "conditions": [
        {"field": "time_hour", "operator": ">=", "value": 11},
        {"field": "time_hour", "operator": "<=", "value": 15},
    ],
    "action": {"type": "block", "reason": "No lights needed midday", "params": {}},
    "confidence": 0.9,
}

LIGHT_NIGHT_DIM_MODIFY = {
    "rule_id": "rule-light-night-dim",
    "scope": {"device_type": "light"},
    "conditions": [{"field": "time_hour", "operator": ">=", "value": 22}],
    "action": {"type": "modify", "reason": "Dim lights after 10 PM", "params": {"brightness": 20}},
    "confidence": 0.88,
}


def _ctx(**overrides):
    base = {"temperature": 70.0, "humidity": 50.0, "cloud_cover_pct": 0,
            "is_overcast": False, "time_hour": 12, "is_home": True}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# BLOCK
# ---------------------------------------------------------------------------

def test_block_verdict_when_fan_and_cold():
    decision = compute_verdict([FAN_COLD_BLOCK], "fan", "turn_on", _ctx(temperature=60))

    assert decision.verdict == "block"
    assert decision.is_blocked is True
    assert decision.rule_id == "rule-fan-cold"
    assert decision.reason == "Too cold to run fan"
    assert decision.modified_params is None


def test_block_verdict_requires_all_and_semantics_conditions_to_match():
    # Both time_hour conditions must hold — 10 is outside the 11-15 window.
    decision = compute_verdict([LIGHT_MIDDAY_BLOCK], "light", "turn_on", _ctx(time_hour=10))
    assert decision.verdict == "allow"

    decision = compute_verdict([LIGHT_MIDDAY_BLOCK], "light", "turn_on", _ctx(time_hour=13))
    assert decision.verdict == "block"
    assert decision.rule_id == "rule-light-midday"


# ---------------------------------------------------------------------------
# MODIFY
# ---------------------------------------------------------------------------

def test_modify_verdict_when_light_at_night():
    decision = compute_verdict(
        [LIGHT_NIGHT_DIM_MODIFY], "light", "set_brightness", _ctx(time_hour=23)
    )

    assert decision.verdict == "modify"
    assert decision.is_modified is True
    assert decision.rule_id == "rule-light-night-dim"
    assert decision.modified_params == {"brightness": 20}


# ---------------------------------------------------------------------------
# ALLOW (default)
# ---------------------------------------------------------------------------

def test_allow_verdict_is_default_when_nothing_matches():
    decision = compute_verdict(
        [FAN_COLD_BLOCK, LIGHT_MIDDAY_BLOCK, LIGHT_NIGHT_DIM_MODIFY],
        "light", "turn_on", _ctx(time_hour=9),
    )

    assert decision.verdict == "allow"
    assert decision.rule_id is None
    assert decision.modified_params is None


# ---------------------------------------------------------------------------
# Precedence: BLOCK > MODIFY > ALLOW
# ---------------------------------------------------------------------------

def test_block_takes_precedence_over_modify_for_same_device_type():
    # Construct a context where a block-type and a modify-type policy for
    # the same device_type both match simultaneously, and confirm BLOCK wins
    # regardless of list order (evaluator.py's documented priority).
    always_modify = {
        "rule_id": "rule-light-always-modify",
        "scope": {"device_type": "light"},
        "conditions": [{"field": "is_home", "operator": "==", "value": True}],
        "action": {"type": "modify", "reason": "Always dim a bit", "params": {"brightness": 50}},
        "confidence": 0.9,
    }
    ctx = _ctx(time_hour=13, is_home=True)  # matches both LIGHT_MIDDAY_BLOCK and always_modify

    decision_modify_first = compute_verdict(
        [always_modify, LIGHT_MIDDAY_BLOCK], "light", "turn_on", ctx
    )
    decision_block_first = compute_verdict(
        [LIGHT_MIDDAY_BLOCK, always_modify], "light", "turn_on", ctx
    )

    assert decision_modify_first.verdict == "block"
    assert decision_block_first.verdict == "block"


# ---------------------------------------------------------------------------
# Safe-action bypass
# ---------------------------------------------------------------------------

def test_turn_off_bypasses_even_a_matching_block_policy():
    decision = compute_verdict([FAN_COLD_BLOCK], "fan", "turn_off", _ctx(temperature=60))
    assert decision.verdict == "allow"
    assert decision.rule_id is None


def test_get_status_bypasses_even_a_matching_block_policy():
    decision = compute_verdict([LIGHT_MIDDAY_BLOCK], "light", "get_status", _ctx(time_hour=13))
    assert decision.verdict == "allow"


# ---------------------------------------------------------------------------
# Defensive device_type re-check (loader is documented to pre-filter, but
# evaluator re-checks scope defensively)
# ---------------------------------------------------------------------------

def test_policy_scoped_to_other_device_type_is_ignored():
    decision = compute_verdict([FAN_COLD_BLOCK], "light", "turn_on", _ctx(temperature=60))
    assert decision.verdict == "allow"
