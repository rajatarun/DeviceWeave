#!/usr/bin/env python3
"""
How often does the LLM compile a plain-English rule into the wrong policy?

The per-rule validator only proves a compiled rule is *well-formed*. A rule
that says "don't run the heater when it's above 80" compiled to
``temperature < 80`` is well-formed, confident, and backwards -- and nothing
downstream can tell. This benchmark measures that silent error rate.

  generate   write the benchmark: 380 rules with gold policies, built from
             device, condition and action phrasings (including explicit
             numbers, the semantic shorthands the compiler prompt defines, and
             two-condition conjunctions), plus rules that must be rejected
             (unsupported devices or conditions, vague requests) and rules
             that contradict themselves.
  run        compile each rule with the configured LLM provider
             (llm_compiler.compile_rule, so the real prompt and model) and
             score it; or --responses FILE to score saved compiler outputs.

Scoring compares *meaning*, not JSON: two rules are equivalent when they
match exactly the same contexts, which rule_set_checker decides exactly
(region A \\ B and B \\ A both empty). So ``time_hour >= 22`` and
``time_hour > 21`` are the same rule, and ``temperature > 85`` vs ``>= 85`` is
not. Outcomes:

  correct         right device, right action type, equivalent conditions
  wrong_meaning   accepted by the validator, but different conditions,
                  device or action -- the silent failure this exists to count
  false_reject    a compilable rule was refused (by the model or validator)
  false_accept    a rule that should be refused was compiled and accepted
  rejected_ok     a rule that should be refused was refused
  contradiction_caught / contradiction_missed
                  a self-contradictory rule: caught if refused by the model,
                  the validator, or the rule-set checker; ``missed`` counts
                  how many the validator alone would have let through
  infra_error     the compiler returned nothing (network, unparseable output)

Usage
-----
  python scripts/policy_compile_bench.py generate --out benchmarks/policy_compile/rules.jsonl
  python scripts/policy_compile_bench.py run benchmarks/policy_compile/rules.jsonl --out results.jsonl
  python scripts/policy_compile_bench.py run benchmarks/policy_compile/rules.jsonl --responses saved.jsonl
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_authoring.rule_set_checker import region_minus, rule_region  # noqa: E402
from policy_authoring.validator import ValidationError, validate_policy  # noqa: E402

DEVICES = {
    "fan": ["the fan", "the ceiling fan", "the bedroom fan"],
    "light": ["the lights", "the living room lamp", "the porch light"],
    "ac": ["the AC", "the air conditioner"],
    "plug": ["the smart plug", "the coffee maker plug"],
    "heater": ["the heater", "the space heater"],
}

# phrase -> list of (field, operator, value)
CONDITIONS: List[tuple] = [
    ("when it's colder than 65 degrees", [("temperature", "<", 65)]),
    ("when it's cold", [("temperature", "<", 65)]),
    ("when it's hot", [("temperature", ">", 85)]),
    ("when the temperature is above 90", [("temperature", ">", 90)]),
    ("when the temperature is 78 or higher", [("temperature", ">=", 78)]),
    ("when it's below 50 degrees", [("temperature", "<", 50)]),
    ("when it's humid", [("humidity", ">", 60)]),
    ("when humidity is over 70%", [("humidity", ">", 70)]),
    ("when the air is dry", [("humidity", "<", 30)]),
    ("when it's overcast", [("is_overcast", "==", True)]),
    ("when it's sunny", [("is_overcast", "==", False)]),
    ("when it's mostly cloudy", [("cloud_cover_pct", ">", 70)]),
    ("after 10 PM", [("time_hour", ">=", 22)]),
    ("after 11 PM", [("time_hour", ">=", 23)]),
    ("before 7 AM", [("time_hour", "<", 7)]),
    ("in the early morning", [("time_hour", "<=", 9)]),
    ("at night", [("time_hour", ">=", 21)]),
    ("when nobody is home", [("is_home", "==", False)]),
    ("when I'm home", [("is_home", "==", True)]),
    ("when no one is home", [("is_home", "==", False)]),
]

ACTIONS = [
    ("Don't turn on {d} {c}", "block"),
    ("Never run {d} {c}", "block"),
    ("Block {d} {c}", "block"),
    ("Keep {d} on {c}", "allow"),
    ("Always allow {d} {c}", "allow"),
]
MODIFY = ("Dim {d} {c}", "modify")

REJECT = [
    "Don't run the dishwasher after 10 PM",
    "Turn off the TV when nobody is home",
    "Lock the front door at night",
    "Open the garage door when I'm home",
    "Set the thermostat to 68 when it's cold",
    "Don't turn on the fan when it rains",
    "Turn off the lights when the door is open",
    "Keep the heater off while I'm sleeping",
    "Do something with the fan",
    "Make the lights nicer",
    "Handle the AC sensibly",
    "Don't run the sprinklers when it's windy",
    "Turn on the lights when my phone battery is low",
    "Block the heater during meetings",
    "Keep the fan on when the baby cries",
    "Don't run the plug when electricity is expensive",
    "Turn off the AC when the window is open",
    "Dim the lights when a movie is playing",
    "Never run the vacuum when the dog is asleep",
    "Keep the porch light on when a package arrives",
]

CONTRADICTIONS = [
    ("Don't turn on {d} when it's above 80 degrees and below 60 degrees",
     [("temperature", ">", 80), ("temperature", "<", 60)]),
    ("Never run {d} when humidity is over 100%", [("humidity", ">", 100)]),
    ("Block {d} when nobody is home and when I'm home", [("is_home", "==", False), ("is_home", "==", True)]),
    ("Don't turn on {d} after 11 PM and before 6 AM on the same night",
     [("time_hour", ">=", 23), ("time_hour", "<", 6)]),
]


def _gold(device: str, conds: Iterable[tuple], kind: str) -> Dict[str, Any]:
    return {"rule_id": "gold", "scope": {"device_type": device},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": kind, "reason": "gold", "params": {}}}


def generate(seed: int = 7) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    items: List[Dict[str, Any]] = []
    n = 0

    def add(rule: str, category: str, gold: Optional[Dict[str, Any]]):
        nonlocal n
        n += 1
        items.append({"id": f"r{n:04d}", "rule": rule, "category": category, "gold": gold})

    # Single-condition rules: every condition with every action, on two different devices.
    for (cphrase, conds), (template, kind) in itertools.product(CONDITIONS, ACTIONS):
        for device in rng.sample(list(DEVICES), 2):
            add(template.format(d=rng.choice(DEVICES[device]), c=cphrase), "single", _gold(device, conds, kind))
    # Modify rules make sense for lights.
    for cphrase, conds in CONDITIONS:
        add(MODIFY[0].format(d=rng.choice(DEVICES["light"]), c=cphrase), "modify", _gold("light", conds, "modify"))
    # Two-condition conjunctions over different fields.
    pairs = [(a, b) for a, b in itertools.combinations(CONDITIONS, 2) if a[1][0][0] != b[1][0][0]]
    for (ca, conds_a), (cb, conds_b) in rng.sample(pairs, 120):
        device = rng.choice(list(DEVICES))
        template, kind = rng.choice(ACTIONS)
        add(template.format(d=rng.choice(DEVICES[device]), c=f"{ca} and {cb.replace('when ', '', 1)}"),
            "conjunction", _gold(device, conds_a + conds_b, kind))
    for rule in REJECT:
        add(rule, "reject", None)
    for template, conds in CONTRADICTIONS:
        for device in DEVICES:
            add(template.format(d=DEVICES[device][0]), "contradiction", _gold(device, conds, "block"))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def equivalent(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ra, rb = rule_region(a), rule_region(b)
    return not region_minus(ra, rb) and not region_minus(rb, ra)


def score_one(item: Dict[str, Any], compiled: Optional[Dict[str, Any]]) -> str:
    if compiled is None:
        return "infra_error"
    try:
        policy = validate_policy(json.loads(json.dumps(compiled)))
    except ValidationError:
        policy = None
    cat = item["category"]
    if cat == "reject":
        return "rejected_ok" if policy is None else "false_accept"
    if cat == "contradiction":
        if policy is None or not rule_region(policy):
            return "contradiction_caught"
        return "contradiction_missed"
    if policy is None:
        return "false_reject"
    gold = item["gold"]
    if policy["scope"]["device_type"] != gold["scope"]["device_type"]:
        return "wrong_meaning"
    if policy["action"]["type"] != gold["action"]["type"]:
        return "wrong_meaning"
    return "correct" if equivalent(policy, gold) else "wrong_meaning"


def contradiction_passes_validator(compiled: Optional[Dict[str, Any]]) -> bool:
    """True when the validator alone accepts a compiled contradiction (only the checker catches it)."""
    if compiled is None:
        return False
    try:
        policy = validate_policy(json.loads(json.dumps(compiled)))
    except ValidationError:
        return False
    return not rule_region(policy)


def summarize(items: List[Dict[str, Any]], outcomes: List[str], compiled: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    by_cat: Dict[str, Counter] = {}
    for it, o in zip(items, outcomes):
        by_cat.setdefault(it["category"], Counter())[o] += 1
    total = Counter(outcomes)
    compilable = [o for it, o in zip(items, outcomes) if it["category"] not in ("reject", "contradiction")]
    accepted = [o for o in compilable if o in ("correct", "wrong_meaning")]
    return {
        "n": len(items),
        "outcomes": dict(total),
        "byCategory": {k: dict(v) for k, v in by_cat.items()},
        "accuracy": round(total["correct"] / len(compilable), 4) if compilable else None,
        # Of the rules that passed the validator, how many mean something else:
        # the error rate no per-rule check can see.
        "silentErrorRate": round(accepted.count("wrong_meaning") / len(accepted), 4) if accepted else None,
        "contradictionsOnlyTheCheckerCaught": sum(
            1 for it, c in zip(items, compiled)
            if it["category"] == "contradiction" and contradiction_passes_validator(c)),
    }


def run(items: List[Dict[str, Any]], compile_fn: Callable[[str], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    compiled = [compile_fn(it["rule"]) for it in items]
    outcomes = [score_one(it, c) for it, c in zip(items, compiled)]
    return {"summary": summarize(items, outcomes, compiled),
            "rows": [{"id": it["id"], "rule": it["rule"], "category": it["category"], "outcome": o, "compiled": c}
                     for it, o, c in zip(items, outcomes, compiled)]}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("benchmark")
    r.add_argument("--responses", default=None, help="JSONL of {id, compiled} to score instead of calling the LLM")
    r.add_argument("--out", default=None)
    r.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    if args.cmd == "generate":
        items = generate(args.seed)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("".join(json.dumps(i) + "\n" for i in items))
        print(f"wrote {len(items)} rules: {dict(Counter(i['category'] for i in items))}")
        return 0

    items = [json.loads(l) for l in Path(args.benchmark).read_text().splitlines() if l.strip()]
    if args.limit:
        items = items[: args.limit]
    if args.responses:
        saved = {}
        for line in Path(args.responses).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                saved[row["id"]] = row.get("compiled")
        by_rule = {it["rule"]: saved.get(it["id"]) for it in items}
        compile_fn = by_rule.get
    else:
        from policy_authoring.llm_compiler import compile_rule
        compile_fn = compile_rule
    result = run(items, compile_fn)
    print(json.dumps(result["summary"], indent=2))
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in result["rows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
