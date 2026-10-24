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

import itertools
import random

from hypothesis import given, settings
from hypothesis import strategies as st

from policy_engine.evaluator import compute_verdict, rule_winner_key

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


# ---------------------------------------------------------------------------
# Stable winner: lowest rule id, then reason, then params. Order-invariant.
# ---------------------------------------------------------------------------

def _policy(rule_id, kind, *, matches=True, device="light", reason="because", params=None):
    return {
        "rule_id": rule_id,
        "scope": {"device_type": device},
        "conditions": [{
            "field": "time_hour",
            "operator": ">=" if matches else "<",
            "value": 0,
        }],
        "action": {"type": kind, "reason": reason, "params": {} if params is None else params},
    }


def _signature(decision):
    params = decision.modified_params
    frozen = None if params is None else tuple(sorted(params.items()))
    return (decision.verdict, decision.rule_id, decision.reason, frozen)


def _expected_signature(policies, device_type, action, ctx):
    """The winner ``compute_verdict`` must report, derived one rule at a time."""
    if action in ("turn_off", "get_status"):
        return ("allow", None, "", None)
    blocks, modifiers = [], []
    for policy in policies:
        one = compute_verdict([policy], device_type, action, ctx)
        if one.verdict == "block":
            blocks.append(policy)
        elif one.verdict == "modify":
            modifiers.append(policy)
    chosen = blocks or modifiers
    if not chosen:
        return ("allow", None, "", None)
    winner = min(chosen, key=rule_winner_key)
    if blocks:
        return ("block", winner["rule_id"], winner["action"]["reason"], None)
    params = dict(winner["action"].get("params") or {})
    return ("modify", winner["rule_id"], winner["action"]["reason"], tuple(sorted(params.items())))


def test_lowest_matching_block_id_wins_and_beats_a_lower_modify():
    # "!" sorts before "a", but a matching BLOCK still beats any MODIFY.
    # "0" sorts before "a" and does not match, so it must not win.
    rules = [
        _policy("b", "block", reason="later block"),
        _policy("a", "block", reason="earlier block"),
        _policy("0", "block", matches=False, reason="does not match"),
        _policy("!", "modify", reason="dim", params={"brightness": 5}),
    ]
    ctx = _ctx()
    for order in itertools.permutations(rules):
        decision = compute_verdict(list(order), "light", "turn_on", ctx)
        assert _signature(decision) == ("block", "a", "earlier block", None)
        assert _signature(decision) == _expected_signature(list(order), "light", "turn_on", ctx)


def test_lowest_modify_id_keeps_its_own_params_in_either_order():
    low = _policy("m1", "modify", reason="cap low", params={"brightness": 10, "color": "warm"})
    high = _policy("m2", "modify", reason="cap high", params={"brightness": 90})
    ctx = _ctx()
    for order in ([low, high], [high, low]):
        decision = compute_verdict(order, "SmartBulb", "set_brightness", ctx)
        assert decision.verdict == "modify"
        assert decision.rule_id == "m1"
        assert decision.reason == "cap low"
        assert decision.modified_params == {"brightness": 10, "color": "warm"}


def test_blank_rule_id_loses_to_any_real_id():
    blank = _policy("  ", "block", reason="unnamed")
    named = _policy("z", "block", reason="named")
    ctx = _ctx()
    for order in ([blank, named], [named, blank]):
        decision = compute_verdict(order, "light", "turn_on", ctx)
        assert decision.rule_id == "z"
        assert decision.reason == "named"


def test_equal_rule_ids_tie_break_on_reason_then_stay_put_under_shuffle():
    beta = _policy("same", "modify", reason="beta", params={"brightness": 1})
    alpha = _policy("same", "modify", reason="alpha", params={"brightness": 2})
    ctx = _ctx()
    for order in ([beta, alpha], [alpha, beta]):
        decision = compute_verdict(order, "light", "turn_on", ctx)
        assert decision.rule_id == "same"
        assert decision.reason == "alpha"
        assert decision.modified_params == {"brightness": 2}


_RULE_IDS = st.text(alphabet="abcde012", min_size=1, max_size=4)
_REASONS = st.text(alphabet="wxyz ", min_size=1, max_size=8)


@st.composite
def _policy_lists(draw):
    count = draw(st.integers(min_value=0, max_value=5))
    policies = []
    for _ in range(count):
        kind = draw(st.sampled_from(["block", "modify", "allow"]))
        params = None
        if kind == "modify":
            params = {"brightness": draw(st.integers(min_value=-20, max_value=180))}
        policies.append(_policy(
            draw(_RULE_IDS),
            kind,
            matches=draw(st.booleans()),
            device=draw(st.sampled_from(["light", "fan", "SmartBulb"])),
            reason=draw(_REASONS),
            params=params,
        ))
    return policies


@given(
    policies=_policy_lists(),
    action=st.sampled_from(["turn_on", "set_brightness", "turn_off", "get_status"]),
    device_type=st.sampled_from(["light", "SmartBulb", "fan"]),
)
@settings(max_examples=200, deadline=None)
def test_order_invariance_covers_verdict_id_reason_and_params(policies, action, device_type):
    """M5: one action and context, one winner, under rule shuffle."""
    ctx = _ctx()
    expected = _expected_signature(policies, device_type, action, ctx)
    shuffled = list(policies)
    random.Random(20261024).shuffle(shuffled)
    for order in (policies, list(reversed(policies)), shuffled):
        assert _signature(compute_verdict(order, device_type, action, ctx)) == expected
