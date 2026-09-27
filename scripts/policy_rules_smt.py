#!/usr/bin/env python3
"""
Z3 encoding of the rule-set questions, as an independent check.

``src/policy_authoring/rule_set_checker.py`` answers satisfiability, shadowing,
overlap and invariant coverage exactly by interval arithmetic, because every
Policy DSL condition constrains one field. This script asks Z3 the same
questions from a separate encoding — context fields as SMT variables, each rule
as the conjunction of its conditions, the evaluator's priority order written
out as formulas — so the two implementations can be compared on random rule
sets (``tests/test_rule_set_checker.py``) and on a live policy table:

  python scripts/policy_rules_smt.py policies.json [--invariants inv.json]

where ``policies.json`` is the ``policies`` array from ``GET /policies``
(newest first) and ``inv.json`` a list of
``{"name", "device_type", "conditions"}``. Needs ``pip install z3-solver``;
it is not a Lambda dependency.

When the DSL gains a condition over two fields (``temperature > humidity``) the
box argument stops holding and this encoding is the one to keep.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import z3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_authoring.rule_set_checker import (  # noqa: E402
    BOOLEAN_FIELDS, INTEGER_FIELDS, NUMERIC_DOMAINS,
)


class Encoding:
    def __init__(self) -> None:
        self.vars: Dict[str, Any] = {}
        for f, (lo, hi) in NUMERIC_DOMAINS.items():
            self.vars[f] = z3.Int(f) if f in INTEGER_FIELDS else z3.Real(f)
        for f in BOOLEAN_FIELDS:
            self.vars[f] = z3.Bool(f)
        self.domain = z3.And(*[
            z3.And(self.vars[f] >= lo, self.vars[f] <= hi) for f, (lo, hi) in NUMERIC_DOMAINS.items()
        ])

    def condition(self, cond: Dict[str, Any]) -> Any:
        f, op, v = cond.get("field"), cond.get("operator"), cond.get("value")
        if f not in self.vars:
            return z3.BoolVal(False)
        x = self.vars[f]
        if f in BOOLEAN_FIELDS:
            if not isinstance(v, bool) or op not in ("==", "!="):
                return z3.BoolVal(False)
            return x == v if op == "==" else x != v
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return z3.BoolVal(False)
        val = z3.RealVal(str(float(v)))
        x = z3.ToReal(x) if f in INTEGER_FIELDS else x
        return {">": x > val, ">=": x >= val, "<": x < val, "<=": x <= val,
                "==": x == val, "!=": x != val}.get(op, z3.BoolVal(False))

    def rule(self, rule: Dict[str, Any]) -> Any:
        return z3.And(*[self.condition(c) for c in rule.get("conditions") or []] or [z3.BoolVal(True)])

    def sat(self, *formulas: Any) -> Optional[Dict[str, Any]]:
        s = z3.Solver()
        s.add(self.domain, *formulas)
        if s.check() != z3.sat:
            return None
        m = s.model()
        return {f: str(m.eval(x, model_completion=True)) for f, x in self.vars.items()}


def _atype(r: Dict[str, Any]) -> str:
    return (r.get("action") or {}).get("type")


def _device(r: Dict[str, Any]) -> str:
    return (r.get("scope") or {}).get("device_type") or r.get("device_type")


def analyze(rules: Sequence[Dict[str, Any]], invariants: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Same finding kinds as rule_set_checker.analyze, computed by Z3."""
    e = Encoding()
    out: List[Dict[str, Any]] = []
    live = []
    for r in rules:
        if e.sat(e.rule(r)) is None:
            out.append({"kind": "unsatisfiable", "rule_ids": [r.get("rule_id")]})
        else:
            live.append(r)
    devices = {_device(r) for r in live}
    for dev in devices:
        entries = [r for r in live if _device(r) == dev]
        blocked = z3.Or(*[e.rule(r) for r in entries if _atype(r) == "block"] or [z3.BoolVal(False)])
        for i, r in enumerate(entries):
            k = _atype(r)
            if k == "allow":
                for b in entries:
                    if _atype(b) == "block" and e.sat(e.rule(r), e.rule(b)) is not None:
                        out.append({"kind": "conflict_block_allow", "rule_ids": [r.get("rule_id"), b.get("rule_id")]})
            if k == "modify":
                newer = [m for m in entries[:i] if _atype(m) == "modify"]
                claimed = z3.Or(blocked, *[e.rule(m) for m in newer])
                if e.sat(e.rule(r), z3.Not(claimed)) is None:
                    out.append({"kind": "shadowed", "rule_ids": [r.get("rule_id")]})
                for m in newer:
                    if (m.get("action") or {}).get("params") != (r.get("action") or {}).get("params") and \
                            e.sat(e.rule(r), e.rule(m), z3.Not(blocked)) is not None:
                        out.append({"kind": "conflict_modify", "rule_ids": [m.get("rule_id"), r.get("rule_id")]})
            same = [o for o in entries if o is not r and _atype(o) == k and
                    (k != "modify" or (o.get("action") or {}).get("params") == (r.get("action") or {}).get("params"))]
            if same and e.sat(e.rule(r), z3.Not(z3.Or(*[e.rule(o) for o in same]))) is None:
                out.append({"kind": "redundant", "rule_ids": [r.get("rule_id")]})
    for inv in invariants:
        dev_blocked = z3.Or(*[e.rule(r) for r in live if _device(r) == inv["device_type"] and _atype(r) == "block"]
                            or [z3.BoolVal(False)])
        w = e.sat(e.rule({"conditions": inv.get("conditions") or []}), z3.Not(dev_blocked))
        if w is not None:
            out.append({"kind": "invariant_violated", "rule_ids": [], "name": inv["name"], "witness": w})
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("policies")
    ap.add_argument("--invariants", default=None)
    args = ap.parse_args(argv)
    rules = json.loads(Path(args.policies).read_text())
    if isinstance(rules, dict):
        rules = rules.get("policies", [])
    invs = json.loads(Path(args.invariants).read_text()) if args.invariants else []
    findings = analyze(rules, invs)
    print(json.dumps(findings, indent=2))
    return 1 if any(f["kind"] in ("unsatisfiable", "invariant_violated") for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
