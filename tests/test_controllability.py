"""Supervisory control of DeviceWeave safety rules.

The heater stories are the ones the model has to get right: an uncontrollable
physical switch, a guard that must refuse turn_on at home, and forcing with
and without a depart precursor. Property tests compare the fixpoint with a
brute-force enumeration of supervisors on small plants. Every rule below is
synthetic.
"""
from __future__ import annotations

import itertools
import json
from typing import Dict, Optional, Set, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from policy_authoring import controllability as C
from policy_authoring import rule_set_checker as R
from policy_authoring.controllability import (
    GUARD_ENFORCEABLE, NEEDS_OBLIGATION, UNENFORCEABLE, Event, Plant,
)
from policy_engine.evaluator import _SAFE_ACTIONS, compute_verdict


def block(device, conds, rid="h"):
    return {"rule_id": rid, "scope": {"device_type": device},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": "block", "reason": "r", "params": {}}}


AWAY = [block("heater", [("is_home", "==", False)])]


def _plant(manual=False, precursor=False, rules=None):
    return C.build_plant(("heater",), rules=rules if rules is not None else AWAY,
                         manual_on=manual, precursor=precursor)


def _where(plant, synthesis, **want):
    return [s for s in synthesis.good
            if all(C.state_dict(plant, s).get(k) == v for k, v in want.items())]


def _enabled(plant, synthesis, event_suffix, **at):
    return [s for (s, e) in synthesis.enabled
            if e.endswith(event_suffix)
            and all(C.state_dict(plant, s).get(k) == v for k, v in at.items())]


# ── the four heater behaviours ───────────────────────────────────────────────

def test_manual_on_is_not_enforceable_and_brute_force_agrees():
    plant = _plant(manual=True)
    guard = C.supcon(plant)
    forced = C.supcon_forcing(plant)
    assert plant.initial not in guard.good
    assert plant.initial not in forced.good
    result = C.enforceability(AWAY, manual_on=True)[0]
    assert result.classification == UNENFORCEABLE
    assert result.witness_trace[-1]["bad"] is True
    events = [step["event"] for step in result.witness_trace if step["event"]]
    assert events and all(not plant.events[e].controllable for e in events)
    assert C.replay(plant, result.witness_trace)["heater"] == "on"
    assert C.replay(plant, result.witness_trace)["is_home"] is False
    assert _brute_guard(plant) is None          # no safe BLOCK supervisor at all


def test_without_manual_on_the_guard_must_block_turn_on_at_home():
    plant = _plant()
    guard = C.supcon(plant)
    assert plant.initial in guard.good
    assert guard.closed_loop_safe and not C.reaches_bad(plant, guard)
    # on-at-home is legal for the spec but not controllable: leave cannot be refused.
    assert not _where(plant, guard, heater="on", is_home=True)
    assert _enabled(plant, guard, "turn_on", heater="off", is_home=True) == []
    disabled_home = [e for (s, e) in guard.disabled
                     if e == "heater.turn_on" and C.state_dict(plant, s)["is_home"] is True]
    assert disabled_home
    brute = _brute_guard(plant)
    assert brute is not None
    assert brute == _taken(plant, guard)


def test_forcing_without_a_precursor_turns_the_heater_off_immediately():
    plant = _plant()
    forced = C.supcon_forcing(plant)
    result = C.enforceability(AWAY)[0]
    assert result.classification == NEEDS_OBLIGATION
    assert result.blocking_supervisor_exists          # the weaker guard exists
    assert forced.retains_legal and not C.reaches_bad(plant, forced)
    on_home = _where(plant, forced, heater="on", is_home=True)
    assert on_home and all(s in forced.forcing for s in on_home)
    # turn_on at home is allowed; the only way forward from on-at-home is turn_off.
    assert _enabled(plant, forced, "turn_on", heater="off", is_home=True)
    for s in on_home:
        events = {e for (st, e) in forced.enabled if st == s}
        assert events == {"heater.turn_off"}
    assert result.witness_trace[-1]["bad"]
    assert C.replay(plant, result.witness_trace)["is_home"] is False


def test_precursor_keeps_the_heater_on_at_home_and_forces_off_at_depart():
    plant = _plant(precursor=True)
    forced = C.supcon_forcing(plant)
    guard = C.supcon(plant)
    assert not C.reaches_bad(plant, forced)
    assert _enabled(plant, forced, "turn_on", heater="off", is_home="home")
    on_home = _where(plant, forced, heater="on", is_home="home")
    assert on_home and all(s not in forced.forcing for s in on_home)
    on_depart = _where(plant, forced, heater="on", is_home="depart")
    assert on_depart and all(s in forced.forcing for s in on_depart)
    for s in on_depart:
        assert {e for (st, e) in forced.enabled if st == s} == {"heater.turn_off"}
    # The guard-only supervisor still cannot leave the heater on at home.
    assert not _where(plant, guard, heater="on", is_home="home")
    result = C.enforceability(AWAY, precursor=True)[0]
    assert result.classification == NEEDS_OBLIGATION
    assert [step["event"] for step in result.witness_trace if step["event"]] == [
        "heater.turn_on", "depart", "leave"]
    assert not C.reaches_bad(plant, C.supcon_forcing(_plant()))  # no-precursor closed loop too


def test_a_block_that_covers_every_context_is_guard_enforceable():
    rules = [block("heater", [("is_home", "==", True)], "home"),
             block("heater", [("is_home", "==", False)], "away")]
    result = C.enforceability(rules)[0]
    assert result.classification == GUARD_ENFORCEABLE
    assert result.guard.retains_legal and result.guard.forcing == frozenset()
    plant = C.build_plant(("heater",), rules=rules)
    assert result.witness_trace[-1]["bad"] is True
    assert C.replay(plant, result.witness_trace)["heater"] == "on"


def test_two_forcible_events_only_the_safe_one_is_taken():
    """The forcing toy followed every forcible event, including one into the bad state."""
    events = {
        "u": Event("u", controllable=False, forcible=False, kind="context"),
        "f_bad": Event("f_bad", controllable=False, forcible=True, kind="safe_action"),
        "f_good": Event("f_good", controllable=False, forcible=True, kind="safe_action"),
    }
    plant = Plant(
        states=frozenset({0, 1, 2}), initial=0, bad=frozenset({2}), events=events,
        delta={(0, "u"): 2, (0, "f_bad"): 2, (0, "f_good"): 1, (1, "u"): 1},
    )
    forced = C.supcon_forcing(plant)
    assert 0 in forced.good and 0 in forced.forcing and 2 not in forced.reachable
    assert {e for (s, e) in forced.enabled if s == 0} == {"f_good"}
    assert not C.reaches_bad(plant, forced)


# ── classification of events against the evaluator's safe-action list ───────

def test_event_classes_follow_safe_actions():
    catalogue = C.classify_events()
    assert "turn_off" in _SAFE_ACTIONS and "get_status" in _SAFE_ACTIONS
    assert "turn_on" not in _SAFE_ACTIONS
    assert catalogue["turn_off"] == Event("turn_off", False, True, "safe_action")
    assert catalogue["get_status"] == Event("get_status", False, False, "safe_action")
    assert catalogue["turn_on"].controllable and not catalogue["turn_on"].forcible
    assert catalogue["set_brightness"].controllable and catalogue["set_brightness"].kind == "command"
    assert catalogue["modify"].controllable
    assert not catalogue["manual_on"].controllable and catalogue["manual_on"].kind == "manual"
    assert catalogue["depart"].kind == "precursor" and not catalogue["depart"].forcible
    assert not catalogue["leave"].controllable and catalogue["leave"].kind == "context"
    planted = C.classify_events(plant=_plant())
    assert planted["heater.turn_off"].forcible and not planted["heater.turn_off"].controllable
    assert planted["heater.turn_on"].controllable


def test_temperature_threshold_splits_the_domain_and_needs_an_obligation():
    rules = [block("heater", [("temperature", ">", 80)])]
    plant = C.build_plant(("heater",), rules=rules)
    assert plant.fields == ("temperature",)
    labels = [c.label() for c in plant.axes[0]]
    assert len(labels) == 2
    result = C.enforceability(rules)[0]
    assert result.classification == NEEDS_OBLIGATION
    assert not C.reaches_bad(plant, result.forcing)


# ── the finding is additive and the evaluator is untouched ──────────────────

def test_analyze_reports_enforceability_without_changing_verdicts():
    rules = [block("heater", [("is_home", "==", False)])]
    findings = R.analyze(rules)
    [enf] = [f for f in findings if f.kind == "enforceability"]
    assert enf.severity == "warning" and enf.rule_ids == ["h"]
    assert enf.witness["classification"] == NEEDS_OBLIGATION
    assert enf.witness["trace"][-1]["bad"] is True
    home = {"temperature": 70, "humidity": 40, "time_hour": 12, "cloud_cover_pct": 10,
            "is_home": True, "is_overcast": False}
    away = dict(home, is_home=False)
    assert compute_verdict(rules, "heater", "turn_on", home).verdict == "allow"
    assert compute_verdict(rules, "heater", "turn_on", away).verdict == "block"
    assert compute_verdict(rules, "heater", "turn_off", away).verdict == "allow"


def test_static_coverage_is_not_the_same_question_as_controllability():
    """The interval checker says the blocks cover 'off when away'. Controllability
    still requires an obligation, because leave is uncontrollable."""
    rules = [block("heater", [("is_home", "==", False)])]
    inv = R.Invariant("heater off when away", "heater",
                      ({"field": "is_home", "operator": "==", "value": False},))
    assert not [f for f in R.analyze(rules, [inv]) if f.kind == "invariant_violated"]
    assert C.enforceability(rules, [inv])[0].classification == NEEDS_OBLIGATION

    gappy = [block("heater", [("is_home", "==", False), ("time_hour", "!=", 3)])]
    [gap] = [f for f in R.analyze(gappy, [inv]) if f.kind == "invariant_violated"]
    assert compute_verdict(gappy, "heater", "turn_on", gap.witness).verdict == "allow"
    # The witness context is a forbidden plant state the written blocks do not cover.
    plant = C.build_plant(("heater",), rules=gappy, invariants=[inv])
    covered = False
    for state in plant.bad:
        view = C.state_dict(plant, state)
        if view["heater"] == "on" and view["time_hour"] in (3, "3") and view["is_home"] is False:
            covered = True
    assert covered


@pytest.fixture
def author(monkeypatch):
    """Same stand-in store the rule-set tests use. Fixtures are not shared across modules."""
    import json as _json
    from policy_authoring import handler as H
    saved = []
    state = {"existing": [], "compiled": None}
    monkeypatch.setattr(H, "compile_rule", lambda text: state["compiled"])
    monkeypatch.setattr(H, "list_policies", lambda device_type=None, limit=50: list(state["existing"]))
    monkeypatch.setattr(H, "save_policy", lambda rid, pol, text: saved.append(pol) or {
        **pol, "rule_id": rid, "version": 1, "device_type": pol["scope"]["device_type"],
        "confidence": str(pol["confidence"]), "source_text": text})
    monkeypatch.setattr(H, "is_configured", lambda: True, raising=False)

    def call(compiled, existing=()):
        state["compiled"], state["existing"] = compiled, list(existing)
        resp = H._route_author({"body": _json.dumps({"rule": "some rule"})})
        return resp["statusCode"], _json.loads(resp["body"]), saved

    return call


def test_authoring_stores_a_rule_that_needs_an_obligation(author):
    rule = block("light", [("is_home", "==", False)])
    rule["confidence"] = 0.95
    status, body, saved = author(rule)
    assert status == 201 and len(saved) == 1
    [enf] = [f for f in body["analysis"] if f["kind"] == "enforceability"]
    assert enf["severity"] == "warning"
    assert enf["witness"]["classification"] == NEEDS_OBLIGATION


# ── property tests ───────────────────────────────────────────────────────────

def _taken(plant: Plant, synthesis) -> Optional[Set[Tuple]]:
    """Events the closed loop actually fires. None when the initial state is unsafe."""
    if plant.initial in plant.bad or plant.initial not in synthesis.good:
        return None
    reach = {plant.initial}
    stack = [plant.initial]
    taken = set()
    while stack:
        s = stack.pop()
        for name, _ev, tgt in plant.successors(s):
            if (s, name) not in synthesis.enabled:
                continue
            if tgt in plant.bad:
                return None
            taken.add((s, name))
            if tgt not in reach:
                reach.add(tgt)
                stack.append(tgt)
    return taken


def _walk(plant: Plant, enabled: Set[Tuple]) -> Optional[Set[Tuple]]:
    if plant.initial in plant.bad:
        return None
    reach = {plant.initial}
    stack = [plant.initial]
    taken: Set[Tuple] = set()
    while stack:
        s = stack.pop()
        for name, _ev, tgt in plant.successors(s):
            if (s, name) not in enabled:
                continue
            if tgt in plant.bad:
                return None
            taken.add((s, name))
            if tgt not in reach:
                reach.add(tgt)
                stack.append(tgt)
    return taken


def _brute_guard(plant: Plant) -> Optional[Set[Tuple]]:
    ctl = [(s, e) for (s, e) in plant.delta if plant.events[e].controllable]
    if len(ctl) > 10:
        return None
    union: Set[Tuple] = set()
    safe = False
    for mask in itertools.product((0, 1), repeat=len(ctl)):
        disabled = {ctl[i] for i, bit in enumerate(mask) if bit}
        enabled = {(s, e) for (s, e) in plant.delta if not (plant.events[e].controllable and (s, e) in disabled)}
        taken = _walk(plant, enabled)
        if taken is not None:
            safe = True
            union |= taken
    if not safe:
        return None
    return union


def _brute_forcing(plant: Plant) -> Optional[Set[Tuple]]:
    """Every supervisor that may force a subset of the forcible events at each state."""
    ctl = [(s, e) for (s, e) in plant.delta if plant.events[e].controllable and not plant.events[e].forcible]
    by_state: Dict = {}
    for (s, e), _t in plant.delta.items():
        if plant.events[e].forcible:
            by_state.setdefault(s, []).append(e)
    if any(len(v) > 2 for v in by_state.values()):
        return None
    states = list(by_state)
    subset_space = []
    for s in states:
        names = by_state[s]
        subset_space.append(list(itertools.chain.from_iterable(
            itertools.combinations(names, k) for k in range(len(names) + 1))))
    choices = 1
    for sset in subset_space:
        choices *= len(sset)
    if len(ctl) > 6 or choices * (2 ** len(ctl)) > 4096:
        return None
    union: Set[Tuple] = set()
    safe = False
    for bits in itertools.product((0, 1), repeat=len(ctl)):
        disabled = {ctl[i] for i, bit in enumerate(bits) if bit}
        for picked in itertools.product(*subset_space) if subset_space else [()]:
            force_at = {states[i]: set(picked[i]) for i in range(len(states)) if picked[i]}
            enabled = set()
            for (s, e) in plant.delta:
                ev = plant.events[e]
                if s in force_at:
                    if e in force_at[s]:
                        enabled.add((s, e))
                    continue
                if ev.controllable and (s, e) in disabled:
                    continue
                enabled.add((s, e))
            taken = _walk(plant, enabled)
            if taken is not None:
                safe = True
                union |= taken
    if not safe:
        return None
    return union


@st.composite
def plants(draw):
    n = draw(st.integers(min_value=2, max_value=6))
    states = list(range(n))
    n_e = draw(st.integers(min_value=1, max_value=4))
    events = {}
    for i in range(n_e):
        controllable = draw(st.booleans())
        forcible = draw(st.booleans())
        events[f"e{i}"] = Event(f"e{i}", controllable, forcible, "command" if controllable else "context")
    delta = {}
    for s in states:
        for name in events:
            if draw(st.integers(0, 99)) < 40:
                delta[(s, name)] = draw(st.sampled_from(states))
    k = draw(st.integers(0, max(0, n // 2)))
    bad = set(draw(st.sampled_from(states)) for _ in range(k))
    return Plant(states=frozenset(states), initial=0, bad=frozenset(bad), events=events, delta=delta)


@settings(max_examples=40, deadline=None, derandomize=True)
@given(plants())
def test_synthesized_supervisors_are_safe_idempotent_and_monotone(plant):
    guard = C.supcon(plant)
    forced = C.supcon_forcing(plant)
    assert set(guard.good) <= set(forced.good)
    if plant.initial in guard.good:
        assert guard.closed_loop_safe and not C.reaches_bad(plant, guard)
        assert _taken(plant, guard) is not None
    if plant.initial in forced.good:
        assert forced.closed_loop_safe and not C.reaches_bad(plant, forced)
    again = C.supcon(plant, bad=set(plant.states) - set(guard.good))
    assert again.good == guard.good
    again_f = C.supcon_forcing(plant, bad=set(plant.states) - set(forced.good))
    assert again_f.good == forced.good
    assert again_f.forcing == forced.forcing
    extra = next(iter(guard.good)) if guard.good else None
    if extra is not None:
        tighter = C.supcon(plant, bad=set(plant.bad) | {extra})
        assert set(tighter.good) <= set(guard.good)
        assert extra not in tighter.good
        tighter_f = C.supcon_forcing(plant, bad=set(plant.bad) | {extra})
        assert set(tighter_f.good) <= set(forced.good)


@settings(max_examples=25, deadline=None, derandomize=True)
@given(plants())
def test_guard_matches_brute_force_on_small_plants(plant):
    ctl = [(s, e) for (s, e) in plant.delta if plant.events[e].controllable]
    if len(ctl) > 8:
        return
    brute = _brute_guard(plant)
    guard = C.supcon(plant)
    assert brute == _taken(plant, guard)


@settings(max_examples=15, deadline=None, derandomize=True)
@given(plants())
def test_forcing_matches_brute_force_on_small_plants(plant):
    brute = _brute_forcing(plant)
    if brute is None and _forcing_search_too_big(plant):
        return
    forced = C.supcon_forcing(plant)
    assert brute == _taken(plant, forced)


def _forcing_search_too_big(plant) -> bool:
    by_state: Dict = {}
    ctl = 0
    for (s, e) in plant.delta:
        ev = plant.events[e]
        if ev.forcible:
            by_state.setdefault(s, 0)
            by_state[s] += 1
        elif ev.controllable:
            ctl += 1
    if any(n > 2 for n in by_state.values()) or ctl > 6:
        return True
    choices = 1
    for n in by_state.values():
        choices *= 2 ** n
    return choices * (2 ** ctl) > 4096


def test_heater_forcing_supervisor_matches_brute_force():
    for manual, precursor in ((False, False), (False, True)):
        plant = _plant(manual=manual, precursor=precursor)
        assert _brute_forcing(plant) == _taken(plant, C.supcon_forcing(plant))


# ── benchmark on synthetic compiled policies (not Study 1) ───────────────────

def test_benchmark_shares_on_synthetic_policies_are_labeled_as_such(tmp_path):
    policies = [
        block("heater", [("is_home", "==", False)], "away"),
        block("heater", [("temperature", ">=", -80)], "always-off"),
        {"rule_id": "dim", "scope": {"device_type": "light"},
         "conditions": [{"field": "time_hour", "operator": ">=", "value": 20}],
         "action": {"type": "modify", "reason": "r", "params": {"brightness": 10}}},
        {"rejected": True, "reason": "no"},
    ]
    report = C.benchmark_enforceability(policies)
    assert report["corpus"] == "caller-supplied policies"
    assert "not AutoTap" in report["note"] or "not\nAutoTap" in report["note"] or "not " in report["note"]
    assert report["counts"][NEEDS_OBLIGATION] == 1
    assert report["counts"][GUARD_ENFORCEABLE] == 1
    assert report["counts"]["NO_SAFETY_SPEC"] == 1
    assert report["counts"]["NOT_COMPILED"] == 1
    assert report["counts"][UNENFORCEABLE] == 0
    assert report["sharesOfSafetySpecs"][NEEDS_OBLIGATION] == 0.5
    manual = C.benchmark_enforceability(policies[:1], manual_on=True)
    assert manual["counts"][UNENFORCEABLE] == 1
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(policies))
    from importlib.util import spec_from_file_location, module_from_spec
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location("compile_fidelity", root / "scripts" / "compile_fidelity.py")
    mod = module_from_spec(spec)
    sys.modules["compile_fidelity"] = mod
    spec.loader.exec_module(mod)
    assert mod.main(["enforceability", "--policies", str(path)]) == 0
