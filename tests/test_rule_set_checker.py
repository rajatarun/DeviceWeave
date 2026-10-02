"""Rule-set analysis: findings a per-rule validator cannot see.

Two kinds of test. Hand-written cases pin each finding and its witness. A
randomised cross-check then asks Z3 (scripts/policy_rules_smt.py, a separate
encoding of the evaluator's semantics) the same questions on hundreds of random
rule sets and requires identical answers — so the interval arithmetic is held
to a solver rather than to the author's reading of it.
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

from policy_authoring import rule_set_checker as C
from policy_engine.evaluator import compute_verdict


def R(rid, dev, conds, kind, params=None):
    return {"rule_id": rid, "scope": {"device_type": dev},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": kind, "reason": "r", "params": params or {}}}


def kinds(findings):
    return sorted((f.kind, tuple(f.rule_ids[:2] if f.kind.startswith("conflict") else f.rule_ids[:1]))
                  for f in findings)


# ── single-rule satisfiability ───────────────────────────────────────────────

@pytest.mark.parametrize("conds", [
    [("temperature", ">", 80), ("temperature", "<", 60)],
    [("humidity", ">", 100)],
    [("time_hour", ">", 22), ("time_hour", "<", 23)],          # no integer hour between
    [("time_hour", ">=", 24)],
    [("is_home", "==", True), ("is_home", "==", False)],
    [("temperature", "==", 70), ("temperature", "!=", 70)],
])
def test_contradictory_rules_are_unsatisfiable(conds):
    assert kinds(C.analyze([R("x", "fan", conds, "block")])) == [("unsatisfiable", ("x",))]


@pytest.mark.parametrize("conds", [
    [("temperature", ">", 80), ("temperature", "<", 80.5)],
    [("time_hour", ">=", 23)],
    [("time_hour", "!=", 3), ("time_hour", ">=", 3), ("time_hour", "<=", 4)],
    [("humidity", "==", 100)],
])
def test_narrow_but_possible_rules_are_satisfiable(conds):
    assert not [f for f in C.analyze([R("x", "fan", conds, "block")]) if f.kind == "unsatisfiable"]


# ── set-level findings ───────────────────────────────────────────────────────

def test_block_silently_overrides_an_allow_and_the_witness_proves_it():
    rules = [R("away", "light", [("is_home", "==", False)], "block"),
             R("night", "light", [("time_hour", ">=", 20)], "allow")]
    [f] = [f for f in C.analyze(rules) if f.kind == "conflict_block_allow"]
    assert f.kind == "conflict_block_allow" and f.rule_ids == ["night", "away"]
    ctx = f.witness
    assert ctx["time_hour"] >= 20 and ctx["is_home"] is False
    assert compute_verdict(rules, "light", "turn_on", ctx).verdict == "block"


def test_modify_under_a_block_is_shadowed():
    rules = [R("away", "light", [("is_home", "==", False)], "block"),
             R("dim", "light", [("is_home", "==", False), ("time_hour", ">", 5)], "modify", {"brightness": 10})]
    assert ("shadowed", ("dim",)) in kinds(C.analyze(rules))


def test_newer_modifier_shadows_an_older_one_it_contains():
    rules = [R("new", "light", [("time_hour", ">=", 20)], "modify", {"brightness": 5}),
             R("old", "light", [("time_hour", ">=", 22)], "modify", {"brightness": 30})]
    got = kinds(C.analyze(rules))
    assert ("shadowed", ("old",)) in got and ("conflict_modify", ("new", "old")) in got


def test_modifier_overlap_inside_a_block_is_not_a_conflict():
    rules = [R("away", "light", [("is_home", "==", False)], "block"),
             R("a", "light", [("is_home", "==", False)], "modify", {"brightness": 5}),
             R("b", "light", [("is_home", "==", False)], "modify", {"brightness": 30})]
    assert not [f for f in C.analyze(rules) if f.kind == "conflict_modify"]


def test_duplicate_block_is_redundant_but_disjoint_blocks_are_not():
    dup = [R("a", "heater", [("temperature", ">", 80)], "block"),
           R("b", "heater", [("temperature", ">", 85)], "block")]
    assert ("redundant", ("b",)) in kinds(C.analyze(dup))
    apart = [R("a", "heater", [("temperature", ">", 80)], "block"),
             R("b", "heater", [("temperature", "<", 40)], "block")]
    assert not [f for f in C.analyze(apart) if f.kind != "enforceability"]


def test_rules_on_different_devices_never_interact():
    rules = [R("f", "fan", [("is_home", "==", False)], "block"),
             R("l", "light", [("is_home", "==", False)], "allow")]
    assert not [f for f in C.analyze(rules) if f.kind != "enforceability"]


# ── invariants ───────────────────────────────────────────────────────────────

AWAY = C.Invariant("heater off when away", "heater", ({"field": "is_home", "operator": "==", "value": False},))


def test_invariant_counterexample_is_a_real_gap():
    rules = [R("h", "heater", [("is_home", "==", False), ("time_hour", "!=", 3)], "block")]
    [f] = [f for f in C.analyze(rules, [AWAY]) if f.kind == "invariant_violated"]
    assert f.kind == "invariant_violated" and f.witness["time_hour"] == 3
    assert compute_verdict(rules, "heater", "turn_on", f.witness).verdict == "allow"


def test_invariant_holds_when_the_union_of_blocks_covers_it():
    rules = [R("h1", "heater", [("is_home", "==", False), ("temperature", ">", 60)], "block"),
             R("h2", "heater", [("is_home", "==", False), ("temperature", "<=", 60)], "block")]
    assert not [f for f in C.analyze(rules, [AWAY]) if f.kind == "invariant_violated"]


def test_check_new_rule_reports_only_what_the_new_rule_is_part_of():
    existing = [R("a", "light", [("is_home", "==", False)], "block"),
                R("b", "light", [("is_home", "==", False)], "block")]   # redundant pair, pre-existing
    new = R("n", "light", [("time_hour", ">=", 20)], "allow")
    got = C.check_new_rule(new, existing)
    assert got and all("n" in f.rule_ids for f in got)


def test_dynamodb_decimals_are_numbers():
    from decimal import Decimal
    rule = R("d", "fan", [("temperature", ">", Decimal("80")), ("temperature", "<", Decimal("60"))], "block")
    assert kinds(C.analyze([rule])) == [("unsatisfiable", ("d",))]


# ── witnesses agree with the evaluator on random sets ───────────────────────

FIELDS = ["temperature", "humidity", "cloud_cover_pct", "time_hour", "is_home", "is_overcast"]
VALUES = {"temperature": [40, 60, 65, 72, 85, 90], "humidity": [0, 30, 60, 100, 101],
          "cloud_cover_pct": [30, 70], "time_hour": [0, 9, 21, 22, 23, 24]}


def random_rule(rng, rid, device="light"):
    conds = []
    for _ in range(rng.randint(1, 3)):
        f = rng.choice(FIELDS)
        if f in ("is_home", "is_overcast"):
            conds.append((f, rng.choice(["==", "!="]), rng.choice([True, False])))
        else:
            conds.append((f, rng.choice([">", "<", ">=", "<=", "==", "!="]), rng.choice(VALUES[f])))
    kind = rng.choice(["block", "block", "modify", "modify", "allow"])
    return R(rid, device, conds, kind, {"brightness": rng.choice([10, 30])} if kind == "modify" else None)


def test_every_witness_reproduces_its_finding_in_the_real_evaluator():
    rng = random.Random(11)
    checked = 0
    for trial in range(300):
        rules = [random_rule(rng, f"r{trial}_{i}") for i in range(rng.randint(2, 5))]
        for f in C.analyze(rules):
            if f.kind == "conflict_block_allow":
                assert compute_verdict(rules, "light", "turn_on", f.witness).verdict == "block"
                checked += 1
            elif f.kind == "conflict_modify":
                d = compute_verdict(rules, "light", "turn_on", f.witness)
                assert d.verdict == "modify"
                assert d.rule_id not in f.rule_ids[1:], "the older modifier must not be the one applied"
                checked += 1
    assert checked > 20


# ── cross-check against Z3 ───────────────────────────────────────────────────

def _load_smt():
    pytest.importorskip("z3")
    path = Path(__file__).resolve().parents[1] / "scripts" / "policy_rules_smt.py"
    spec = importlib.util.spec_from_file_location("policy_rules_smt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key(kind, ids):
    return (kind, tuple(ids[:2] if kind.startswith("conflict") else ids[:1]))


def test_interval_analysis_agrees_with_z3_on_random_rule_sets():
    smt = _load_smt()
    rng = random.Random(5)
    for trial in range(200):
        rules = [random_rule(rng, f"t{trial}_{i}", rng.choice(["light", "heater"]))
                 for i in range(rng.randint(1, 5))]
        inv = [{"name": "inv", "device_type": "heater",
                "conditions": [{"field": "is_home", "operator": "==", "value": False}]}]
        boxes = sorted(_key(f.kind, f.rule_ids) for f in C.analyze(rules, [C.Invariant.from_dict(inv[0])])
                       if f.kind != "enforceability")
        solver = sorted(_key(f["kind"], f["rule_ids"]) for f in smt.analyze(rules, inv))
        assert boxes == solver, (rules, boxes, solver)


# ── POST /policies/author ────────────────────────────────────────────────────

import json as _json  # noqa: E402


@pytest.fixture
def author(monkeypatch):
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


def _compiled(conds, kind):
    rule = R("auto", "light", conds, kind)
    rule["confidence"] = 0.95
    return rule


def test_a_rule_that_can_never_fire_is_refused_and_not_stored(author):
    status, body, saved = author(_compiled([("temperature", ">", 80), ("temperature", "<", 60)], "block"))
    assert status == 422 and body["rejection_stage"] == "rule_set_analysis"
    assert body["analysis"][0]["kind"] == "unsatisfiable" and saved == []


def test_a_conflict_is_stored_with_the_warning_and_its_witness(author):
    existing = [R("away", "light", [("is_home", "==", False)], "block")]
    status, body, saved = author(_compiled([("time_hour", ">=", 20)], "allow"), existing)
    assert status == 201 and len(saved) == 1
    [f] = body["analysis"]
    assert f["kind"] == "conflict_block_allow" and "away" in f["rule_ids"] and f["witness"]["is_home"] is False


def test_an_invariant_counterexample_is_refused(author, monkeypatch):
    monkeypatch.setenv("POLICY_INVARIANTS", _json.dumps([{
        "name": "light off when away", "device_type": "light",
        "conditions": [{"field": "is_home", "operator": "==", "value": False}]}]))
    status, body, saved = author(_compiled([("time_hour", ">=", 20)], "allow"))
    assert status == 422 and body["analysis"][-1]["kind"] == "invariant_violated" and saved == []


def test_an_unreadable_invariant_setting_does_not_block_authoring(author, monkeypatch):
    monkeypatch.setenv("POLICY_INVARIANTS", "{not json")
    status, _, saved = author(_compiled([("time_hour", ">=", 20)], "block"))
    assert status == 201 and len(saved) == 1
