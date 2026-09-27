"""
Rule-set analysis — does the whole policy set mean what its rules say?

``validator.py`` checks one compiled rule at a time: right fields, right types,
confident enough. It cannot see a rule that contradicts itself
(``temperature > 80 AND temperature < 60``), a ``modify`` that a ``block``
always overrides so it never takes effect, two modifiers that disagree about
the same moment, or a safety property that no rule actually guarantees. Each of
those is a valid rule that goes live and silently does something other than
what the person asked for.

Why this is exact without a solver
----------------------------------
Every condition in the Policy DSL constrains exactly one context field, and a
rule's conditions are ANDed. So the set of contexts a rule matches is a union of
axis-aligned boxes (one box normally; ``!=`` splits a field into two pieces).
Satisfiability, overlap, containment and coverage of boxes are all decidable by
interval arithmetic, so the questions below are answered exactly, with a
concrete witness context for every finding, and with no dependency the Lambda
would have to package. ``scripts/policy_rules_smt.py`` encodes the same
questions for Z3 and the tests cross-check the two on random rule sets; the
solver becomes necessary only if the DSL grows cross-field conditions.

Semantics mirrored from ``policy_engine/evaluator.py``
-----------------------------------------------------
* BLOCK beats MODIFY beats the default ALLOW; ``allow`` rules are informational
  and never override a block.
* Among matching modifiers the newest wins (policies arrive newest-first).
* A condition on a field absent from the context does not match. The analysis
  assumes every field is present — the case the rules were written for — and
  says so, because a missing field can only make a block *not* fire.
* ``turn_off`` / ``get_status`` bypass policy entirely; invariants are about
  whether a device may be switched *on*.

Findings
--------
  unsatisfiable        error    the rule matches no possible context
  shadowed             warning  a modify never applies: blocks or newer modifiers
                                cover every context it matches
  conflict_block_allow warning  an allow and a block overlap; the block wins there
  conflict_modify      warning  two modifiers with different params overlap; the
                                newer wins there
  redundant            info     a rule adds nothing: others with the same effect
                                already cover it
  invariant_violated   error    a stated safety property has a counterexample
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# Physical domain of each context field. Numbers outside it are not "possible
# contexts", so ``humidity > 100`` is unsatisfiable rather than merely rare.
NUMERIC_DOMAINS: Dict[str, Tuple[float, float]] = {
    "temperature": (-80.0, 150.0),      # °F
    "humidity": (0.0, 100.0),
    "cloud_cover_pct": (0.0, 100.0),
    "time_hour": (0.0, 23.0),
}
INTEGER_FIELDS = frozenset({"time_hour"})
BOOLEAN_FIELDS = frozenset({"is_home", "is_overcast"})
FIELDS = tuple(NUMERIC_DOMAINS) + tuple(sorted(BOOLEAN_FIELDS))
# Where a witness context is placed when the region allows it, so a
# counterexample reads as a plausible afternoon rather than -80 °F at midnight.
_TYPICAL = {"temperature": 70.0, "humidity": 50.0, "cloud_cover_pct": 50.0, "time_hour": 12.0}


# ─────────────────────────────────────────────────────────────────────────────
# Intervals and boxes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float
    lo_closed: bool = True
    hi_closed: bool = True

    def empty(self, integer: bool = False) -> bool:
        if integer:
            return self.int_bounds() is None
        if self.lo > self.hi:
            return True
        if self.lo == self.hi:
            return not (self.lo_closed and self.hi_closed)
        return False

    def int_bounds(self) -> Optional[Tuple[int, int]]:
        lo = math.floor(self.lo) + 1 if (not self.lo_closed and float(self.lo).is_integer()) else math.ceil(self.lo)
        hi = math.ceil(self.hi) - 1 if (not self.hi_closed and float(self.hi).is_integer()) else math.floor(self.hi)
        return (lo, hi) if lo <= hi else None

    def intersect(self, other: "Interval") -> "Interval":
        if self.lo > other.lo:
            lo, loc = self.lo, self.lo_closed
        elif other.lo > self.lo:
            lo, loc = other.lo, other.lo_closed
        else:
            lo, loc = self.lo, self.lo_closed and other.lo_closed
        if self.hi < other.hi:
            hi, hic = self.hi, self.hi_closed
        elif other.hi < self.hi:
            hi, hic = other.hi, other.hi_closed
        else:
            hi, hic = self.hi, self.hi_closed and other.hi_closed
        return Interval(lo, hi, loc, hic)

    def minus(self, other: "Interval") -> List["Interval"]:
        """self \\ other as at most two intervals (possibly empty ones filtered by caller)."""
        left = Interval(self.lo, other.lo, self.lo_closed, not other.lo_closed).intersect(self)
        right = Interval(other.hi, self.hi, not other.hi_closed, self.hi_closed).intersect(self)
        return [left, right]

    def sample(self, integer: bool = False, prefer: float = 0.0) -> float:
        """A point in the interval, as close to ``prefer`` as the bounds allow."""
        if integer:
            lo, hi = self.int_bounds()  # type: ignore[misc]
            return int(min(max(round(prefer), lo), hi))
        if self.lo < prefer < self.hi or (prefer == self.lo and self.lo_closed) or (
                prefer == self.hi and self.hi_closed):
            return prefer
        if prefer <= self.lo:
            if self.lo_closed:
                return self.lo
            return min(self.lo + 0.5, (self.lo + self.hi) / 2)
        if self.hi_closed:
            return self.hi
        return max(self.hi - 0.5, (self.lo + self.hi) / 2)


Dim = Union[Interval, frozenset]
Box = Dict[str, Dim]


def _full(field: str) -> Dim:
    if field in BOOLEAN_FIELDS:
        return frozenset({True, False})
    lo, hi = NUMERIC_DOMAINS[field]
    return Interval(lo, hi)


def _dim_empty(field: str, d: Dim) -> bool:
    if isinstance(d, frozenset):
        return not d
    return d.empty(field in INTEGER_FIELDS)


def _dim_intersect(a: Dim, b: Dim) -> Dim:
    if isinstance(a, frozenset):
        return a & b  # type: ignore[operator]
    return a.intersect(b)  # type: ignore[arg-type]


def _dim_minus(field: str, a: Dim, b: Dim) -> List[Dim]:
    if isinstance(a, frozenset):
        rest = a - b  # type: ignore[operator]
        return [rest] if rest else []
    return [p for p in a.minus(b) if not _dim_empty(field, p)]  # type: ignore[arg-type]


def box_empty(box: Box) -> bool:
    return any(_dim_empty(f, d) for f, d in box.items())


def box_intersect(a: Box, b: Box) -> Optional[Box]:
    out = {f: _dim_intersect(a[f], b[f]) for f in FIELDS}
    return None if box_empty(out) else out


def box_minus(a: Box, b: Box) -> List[Box]:
    """a \\ b as disjoint boxes."""
    if box_intersect(a, b) is None:
        return [a]
    pieces: List[Box] = []
    rest = dict(a)
    for f in FIELDS:
        for part in _dim_minus(f, rest[f], b[f]):
            piece = dict(rest)
            piece[f] = part
            pieces.append(piece)
        rest[f] = _dim_intersect(rest[f], b[f])
    return pieces


def region_minus(region: List[Box], others: Iterable[Box]) -> List[Box]:
    rest = list(region)
    for b in others:
        rest = [p for r in rest for p in box_minus(r, b)]
        if not rest:
            break
    return rest


def witness(box: Box) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    for f in FIELDS:
        d = box[f]
        if isinstance(d, frozenset):
            ctx[f] = sorted(d)[-1]
        else:
            v = d.sample(f in INTEGER_FIELDS, _TYPICAL[f])
            ctx[f] = int(v) if f in INTEGER_FIELDS else float(v)
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Rules → regions
# ─────────────────────────────────────────────────────────────────────────────

def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else float("nan")


def _condition_dims(cond: Dict[str, Any]) -> Tuple[str, List[Dim]]:
    field, op, value = cond.get("field"), cond.get("operator"), cond.get("value")
    if field in BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            return field, []
        if op == "==":
            return field, [frozenset({value})]
        if op == "!=":
            return field, [frozenset({not value})]
        return field, []  # ordering on a boolean: the evaluator's result is not meaningful here
    if field not in NUMERIC_DOMAINS:
        return str(field), []
    v = _num(value)
    if math.isnan(v):
        return field, []
    inf = math.inf
    table = {
        ">": [Interval(v, inf, False, False)],
        ">=": [Interval(v, inf, True, False)],
        "<": [Interval(-inf, v, False, False)],
        "<=": [Interval(-inf, v, False, True)],
        "==": [Interval(v, v)],
        "!=": [Interval(-inf, v, False, False), Interval(v, inf, False, False)],
    }
    return field, table.get(op, [])


def rule_region(rule: Dict[str, Any]) -> List[Box]:
    """The contexts a rule matches, as disjoint boxes; [] if it matches none."""
    boxes: List[Box] = [{f: _full(f) for f in FIELDS}]
    for cond in rule.get("conditions") or []:
        field, dims = _condition_dims(cond)
        if field not in FIELDS:
            return []  # an unknown field never matches in the evaluator
        nxt: List[Box] = []
        for b in boxes:
            for d in dims:
                nb = dict(b)
                nb[field] = _dim_intersect(b[field], d)
                if not _dim_empty(field, nb[field]):
                    nxt.append(nb)
        boxes = nxt
        if not boxes:
            return []
    return boxes


def region_overlap(a: List[Box], b: List[Box], exclude: Sequence[Box] = ()) -> Optional[Box]:
    """A box in a ∩ b that lies outside every box of ``exclude``; None if there is none."""
    for x in a:
        for y in b:
            i = box_intersect(x, y)
            if i is None:
                continue
            rest = region_minus([i], exclude) if exclude else [i]
            if rest:
                return rest[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    kind: str
    severity: str        # error | warning | info
    rule_ids: List[str]
    device_type: str
    message: str
    witness: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {"kind": self.kind, "severity": self.severity, "rule_ids": self.rule_ids,
               "device_type": self.device_type, "message": self.message}
        if self.witness is not None:
            out["witness"] = self.witness
        return out


@dataclass(frozen=True)
class Invariant:
    """A safety property: in every context matching ``conditions``, ``device_type`` must be blocked."""
    name: str
    device_type: str
    conditions: Tuple[Dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Invariant":
        return cls(name=d["name"], device_type=d["device_type"], conditions=tuple(d.get("conditions") or ()))


def _rid(rule: Dict[str, Any]) -> str:
    return str(rule.get("rule_id", "?"))


def _atype(rule: Dict[str, Any]) -> str:
    return str((rule.get("action") or {}).get("type"))


def _params(rule: Dict[str, Any]) -> Dict[str, Any]:
    return dict((rule.get("action") or {}).get("params") or {})


def _device(rule: Dict[str, Any]) -> str:
    return str((rule.get("scope") or {}).get("device_type") or rule.get("device_type"))


def analyze(rules: Sequence[Dict[str, Any]], invariants: Sequence[Invariant] = ()) -> List[Finding]:
    """Analyse a rule set. ``rules`` must be newest-first, as the evaluator receives them."""
    findings: List[Finding] = []
    by_device: Dict[str, List[Tuple[Dict[str, Any], List[Box]]]] = {}
    for rule in rules:
        region = rule_region(rule)
        if not region:
            findings.append(Finding(
                "unsatisfiable", "error", [_rid(rule)], _device(rule),
                "No possible context satisfies every condition of this rule, so it can never fire. "
                "The conditions contradict each other or fall outside the field's range.",
            ))
            continue
        by_device.setdefault(_device(rule), []).append((rule, region))

    for device, entries in by_device.items():
        blocks = [(r, g) for r, g in entries if _atype(r) == "block"]
        for idx, (rule, region) in enumerate(entries):
            kind = _atype(rule)
            if kind == "allow":
                for b, bg in blocks:
                    w = region_overlap(region, bg)
                    if w is not None:
                        findings.append(Finding(
                            "conflict_block_allow", "warning", [_rid(rule), _rid(b)], device,
                            f"Allow rule {_rid(rule)} and block rule {_rid(b)} both match some contexts; "
                            "the block wins there, so the allow does not hold.", witness(w)))
            if kind == "modify":
                newer_mods = [(r, g) for r, g in entries[:idx] if _atype(r) == "modify"]
                cover = [box for _, g in blocks for box in g] + [box for _, g in newer_mods for box in g]
                if not region_minus(region, cover):
                    findings.append(Finding(
                        "shadowed", "warning", [_rid(rule)] + [_rid(r) for r, _ in blocks + newer_mods], device,
                        f"Modify rule {_rid(rule)} never applies: every context it matches is already "
                        "blocked or claimed by a newer modifier.", witness(region[0])))
                block_boxes = [box for _, g in blocks for box in g]
                for other, og in newer_mods:
                    if _params(other) != _params(rule):
                        # Where a block also matches, neither modifier applies.
                        w = region_overlap(region, og, exclude=block_boxes)
                        if w is not None:
                            findings.append(Finding(
                                "conflict_modify", "warning", [_rid(other), _rid(rule)], device,
                                f"Modifiers {_rid(other)} and {_rid(rule)} set different parameters for "
                                f"some contexts; the newer ({_rid(other)}) wins there.", witness(w)))
            same = [box for r, g in entries if r is not rule and _atype(r) == kind
                    and (kind != "modify" or _params(r) == _params(rule)) for box in g]
            if same and not region_minus(region, same):
                findings.append(Finding(
                    "redundant", "info", [_rid(rule)], device,
                    f"Rule {_rid(rule)} adds nothing: other {kind} rules already cover every context it matches."))

    for inv in invariants:
        region = rule_region({"conditions": list(inv.conditions)})
        blocks = [box for rule, g in by_device.get(inv.device_type, []) if _atype(rule) == "block" for box in g]
        gaps = region_minus(region, blocks)
        if gaps:
            findings.append(Finding(
                "invariant_violated", "error", [], inv.device_type,
                f"Invariant '{inv.name}' does not hold: in this context no rule blocks the "
                f"{inv.device_type} from being switched on.", witness(gaps[0])))
    return findings


def check_new_rule(new_rule: Dict[str, Any], existing: Sequence[Dict[str, Any]],
                   invariants: Sequence[Invariant] = ()) -> List[Finding]:
    """Findings that involve ``new_rule`` when it is added (as the newest) to ``existing``."""
    device = _device(new_rule)
    rules = [new_rule] + [r for r in existing if _device(r) == device and _rid(r) != _rid(new_rule)]
    rid = _rid(new_rule)
    return [f for f in analyze(rules, invariants) if rid in f.rule_ids or f.kind == "invariant_violated"]
