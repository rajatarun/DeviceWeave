"""Regenerate every number in paper 2 (policy verification). No AWS, no model.

  python paper/p2_policy_verification/experiments.py > paper/p2_policy_verification/results.json
"""
import importlib.util
import json
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from policy_authoring import rule_set_checker as C  # noqa: E402
from policy_engine.evaluator import compute_verdict  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smt = load("smt", ROOT / "scripts/policy_rules_smt.py")
bench = load("bench", ROOT / "scripts/policy_compile_bench.py")
T = load("tests_rsc", ROOT / "tests/test_rule_set_checker.py")

out = {}

# 1. Agreement with Z3 and witness replay, over many random sets.
rng = random.Random(2026)
agree = total = 0
kinds = Counter()
replays = replay_ok = 0
by_kind_replay = {}
for trial in range(1000):
    rules = [T.random_rule(rng, f"t{trial}_{i}", rng.choice(["light", "heater"])) for i in range(rng.randint(1, 6))]
    inv = [{"name": "inv", "device_type": "heater",
            "conditions": [{"field": "is_home", "operator": "==", "value": False}]}]
    fs = C.analyze(rules, [C.Invariant.from_dict(inv[0])])
    a = sorted(T._key(f.kind, f.rule_ids) for f in fs)
    b = sorted(T._key(f["kind"], f["rule_ids"]) for f in smt.analyze(rules, inv))
    total += 1
    agree += a == b
    for f in fs:
        kinds[f.kind] += 1
        if f.kind in ("conflict_block_allow", "invariant_violated", "conflict_modify"):
            dev = f.device_type
            dev_rules = [r for r in rules if r["scope"]["device_type"] == dev]
            d = compute_verdict(dev_rules, dev, "turn_on", f.witness)
            replays += 1
            by_kind_replay[f.kind] = by_kind_replay.get(f.kind, 0) + 1
            if f.kind == "conflict_block_allow":
                ok = d.verdict == "block"
            elif f.kind == "invariant_violated":
                ok = d.verdict != "block"
            else:  # the newer modifier (or an even newer one) applies, never the older
                ok = d.verdict == "modify" and d.rule_id != f.rule_ids[1]
            replay_ok += ok
out["agreement"] = {"sets": total, "identical": agree, "finding_counts": dict(kinds),
                    "witness_replays": replays, "witness_replays_confirmed": replay_ok,
                    "witness_replays_by_kind": by_kind_replay}

# 2. Time per analysis, interval checker vs Z3, by rule-set size.
timing = {}
for n in (5, 10, 20, 40, 80):
    ti, tz = [], []
    for trial in range(20 if n <= 20 else 5):
        rules = [T.random_rule(rng, f"s{n}_{trial}_{i}", "light") for i in range(n)]
        t0 = time.perf_counter(); C.analyze(rules); ti.append(time.perf_counter() - t0)
        t0 = time.perf_counter(); smt.analyze(rules); tz.append(time.perf_counter() - t0)
    timing[n] = {"interval_ms": round(1000 * statistics.median(ti), 2), "z3_ms": round(1000 * statistics.median(tz), 2)}
out["timing"] = timing

# 3. Benchmark composition and the validator's blind spot.
items = bench.generate()
out["benchmark"] = {"n": len(items), "by_category": dict(Counter(i["category"] for i in items))}
contra = [i for i in items if i["category"] == "contradiction"]
passes = sum(bench.contradiction_passes_validator({**i["gold"], "rule_id": "auto", "confidence": 0.95}) for i in contra)
out["benchmark"]["contradictions_passing_validator_if_compiled_faithfully"] = f"{passes}/{len(contra)}"
print(json.dumps(out, indent=1))
