"""The compile benchmark's gold data is valid and its scorer judges meaning, not JSON."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from policy_authoring.rule_set_checker import rule_region
from policy_authoring.validator import validate_policy

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("policy_compile_bench", ROOT / "scripts" / "policy_compile_bench.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


def _as_compiled(gold):
    out = copy.deepcopy(gold)
    out["rule_id"] = "auto"
    out["confidence"] = 0.95
    return out


def test_committed_benchmark_is_the_generator_output():
    committed = [json.loads(l) for l in (ROOT / "benchmarks/policy_compile/rules.jsonl").read_text().splitlines()]
    assert committed == B.generate(), "regenerate with scripts/policy_compile_bench.py generate"
    assert len(committed) >= 300


def test_gold_policies_are_valid_and_contradictions_are_unsatisfiable():
    for item in B.generate():
        if item["gold"] is None:
            continue
        policy = validate_policy(_as_compiled(item["gold"]))
        if item["category"] == "contradiction":
            assert not rule_region(policy), item["rule"]
        else:
            assert rule_region(policy), item["rule"]


def test_a_perfect_compiler_scores_perfectly():
    items = B.generate()
    oracle = {it["rule"]: (_as_compiled(it["gold"]) if it["category"] not in ("reject", "contradiction")
                           else {"rejected": True, "reason": "x", "confidence": 0.0})
              for it in items}
    s = B.run(items, oracle.get)["summary"]
    assert s["accuracy"] == 1.0 and s["silentErrorRate"] == 0.0
    assert s["outcomes"].get("false_accept", 0) == 0


def test_equivalent_conditions_score_correct_and_a_flipped_operator_does_not():
    item = {"category": "single", "gold": B._gold("fan", [("time_hour", ">=", 22)], "block")}
    same = _as_compiled(B._gold("fan", [("time_hour", ">", 21)], "block"))
    flipped = _as_compiled(B._gold("fan", [("time_hour", "<", 22)], "block"))
    assert B.score_one(item, same) == "correct"
    assert B.score_one(item, flipped) == "wrong_meaning"
    assert B.score_one(item, _as_compiled(B._gold("heater", [("time_hour", ">=", 22)], "block"))) == "wrong_meaning"
    assert B.score_one(item, None) == "infra_error"


def test_a_compiled_contradiction_is_caught_only_by_the_checker():
    item = {"category": "contradiction", "gold": None}
    compiled = _as_compiled(B._gold("fan", [("temperature", ">", 80), ("temperature", "<", 60)], "block"))
    assert B.score_one(item, compiled) == "contradiction_caught"
    assert B.contradiction_passes_validator(compiled) is True
