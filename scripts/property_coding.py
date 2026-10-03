#!/usr/bin/env python3
"""
Hand-coded formal properties for AutoTap Study 1, and what each enforcement architecture can guarantee.

``compile_fidelity.py`` measures how a five-device DSL and one LLM handle the
690 statements. That says something about DeviceWeave, not about what people
ask for. This tool supports the measurement that does generalise: two people
code every statement as a DSL-independent property (``docs/paper/codebook.md``,
built on AutoTap's own templates), and each coded property is classified, by
the same supervisory-control synthesis DeviceWeave uses, under several
enforcement architectures -- a refusal-only command guard among them.

Source data (referenced, never redistributed): Study 1 of AutoTap --
Lefan Zhang, Weijia He, Jesse Martinez, Noah Brackenbury, Shan Lu, Blase Ur,
"AutoTap: Synthesizing and Repairing Trigger-Action Programs Using LTL
Properties", ICSE 2019, https://ieeexplore.ieee.org/abstract/document/8811900;
https://github.com/zlfben/autotap. Code files are keyed by spreadsheet
position and hold only enumerated fields. Statement text appears only in the
coder worksheet, which is written to the git-ignored output directory.

Commands
--------
  worksheet   CSV for one coder (statement text + empty code columns), into
              benchmarks/autotap/_out/coding/. Seeded order; --sample N for
              the pilot (same subset for every coder).
  import      a filled-in worksheet CSV -> a code file (text dropped, every
              value checked against the codebook).
  agree       two code files -> Cohen's kappa per field and per derived
              outcome, and the ids that need adjudication.
  adjudicate  two code files + resolutions for the disagreements -> gold codes.
  analyze     gold codes (optionally with compile_fidelity rows.jsonl) ->
              results.json: shares with Wilson intervals per architecture and
              assumption, DSL and AutoTap-template coverage, and how many
              accepted compilations a refusal-only guard does not enforce.
  render      fill {{key}} placeholders in the paper draft from results.json.

Every number in docs/paper/draft.md is meant to come from ``render``; the
draft itself carries placeholders only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import compile_fidelity as cf  # noqa: E402
from policy_authoring.controllability import Event, Plant, supcon_forcing  # noqa: E402

CODES_FORMAT = "deviceweave-property-codes/1"
CODEBOOK_VERSION = "1.1"
CODING_OUT = cf.OUT_DIR / "coding"

# ─────────────────────────────────────────────────────────────────────────────
# The codebook as data (docs/paper/codebook.md is the prose)
# ─────────────────────────────────────────────────────────────────────────────

TARGET_CLASSES = (
    "light", "plug_outlet", "fan", "ac", "heater", "thermostat", "lock", "door", "window",
    "garage", "blinds", "camera_security_alarm", "kitchen_appliance", "laundry_appliance",
    "cleaning_robot", "media_speaker", "sensor", "water_irrigation", "multiple", "other",
)
ACTORS = ("system", "mixed", "human", "world")
CONDITIONS = ("none", "presence", "time_of_day", "day_date", "weather", "device_state",
              "sensor_event", "other")
ENUMS: Dict[str, Tuple[str, ...]] = {
    "scope": ("property", "not_a_property"),
    "not_property_reason": ("vague", "preference", "about_product", "not_home_automation", "other"),
    "kind": ("state", "state_pair", "event"),
    "modality": ("always", "never"),
    "named_polarity": ("on", "off"),
    "target_class": TARGET_CLASSES,
    "target_actor": ACTORS,
    "condition": CONDITIONS,
    "condition_actor": ACTORS,
}
BOOLS = ("duration", "within_after", "multi_condition", "exception_in_text", "fits_autotap_template")
PROPERTY_FIELDS = ("kind", "modality", "named_polarity", "target_class", "target_actor",
                   "condition") + BOOLS
CODE_KEYS = frozenset(("scope", "not_property_reason", "condition_actor", "note") + PROPERTY_FIELDS)
# Fields compared for agreement (condition_actor only where both coded a condition).
AGREEMENT_FIELDS = ("scope",) + PROPERTY_FIELDS + ("condition_actor",)

DEVICEWEAVE_DEVICES = frozenset({"light", "plug_outlet", "fan", "ac", "heater"})
DEVICEWEAVE_CONDITIONS = frozenset({"none", "presence", "time_of_day", "weather"})


class CodingError(cf.FidelityError):
    """A code file, worksheet or resolution breaks the codebook."""


def _parse_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("true", "yes", "y", "1"):
        return True
    if v in ("false", "no", "n", "0"):
        return False
    raise CodingError(f"{where}: expected yes/no, got {value!r}")


def validate_code(code: Mapping[str, Any], where: str) -> Dict[str, Any]:
    """Check one statement's code against the codebook; returns it normalised."""
    if not isinstance(code, Mapping):
        raise CodingError(f"{where}: a code must be an object")
    extra = set(code) - CODE_KEYS
    if extra:
        raise CodingError(f"{where}: unknown fields {sorted(extra)} -- codes hold codebook fields "
                          f"only, never statement text")
    out: Dict[str, Any] = {}
    scope = code.get("scope")
    if scope not in ENUMS["scope"]:
        raise CodingError(f"{where}: scope must be one of {ENUMS['scope']}")
    out["scope"] = scope
    if code.get("note") not in (None, ""):
        out["note"] = str(code["note"])
    if scope == "not_a_property":
        reason = code.get("not_property_reason")
        if reason not in ENUMS["not_property_reason"]:
            raise CodingError(f"{where}: not_property_reason must be one of {ENUMS['not_property_reason']}")
        stray = [k for k in PROPERTY_FIELDS + ("condition_actor",) if code.get(k) not in (None, "")]
        if stray:
            raise CodingError(f"{where}: a not_a_property code takes no property fields, got {stray}")
        out["not_property_reason"] = reason
        return out
    for name in ("kind", "modality", "named_polarity", "target_class", "target_actor", "condition"):
        value = code.get(name)
        if value not in ENUMS[name]:
            raise CodingError(f"{where}: {name} must be one of {ENUMS[name]}, got {value!r}")
        out[name] = value
    for name in BOOLS:
        if code.get(name) in (None, ""):
            raise CodingError(f"{where}: {name} is required (yes/no)")
        out[name] = _parse_bool(code[name], f"{where}: {name}")
    if out["condition"] == "none":
        if code.get("condition_actor") not in (None, ""):
            raise CodingError(f"{where}: condition_actor must be empty when condition is none")
    else:
        actor = code.get("condition_actor")
        if actor not in ACTORS:
            raise CodingError(f"{where}: condition_actor must be one of {ACTORS} when condition is set")
        out["condition_actor"] = actor
    if out["kind"] == "state_pair" and out["condition"] != "device_state":
        raise CodingError(f"{where}: a state_pair has condition device_state (the second state)")
    if out["within_after"] and out["kind"] != "event":
        raise CodingError(f"{where}: within_after applies to event statements only")
    if code.get("not_property_reason") not in (None, ""):
        raise CodingError(f"{where}: not_property_reason applies to not_a_property only")
    return out


def load_codes(path: Path) -> Dict[str, Any]:
    """A code file: {"format", "coder", "codebook_version", "codes": {rule_id: code}}."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CodingError(f"{path}: cannot read code file: {exc}") from exc
    if doc.get("format") != CODES_FORMAT:
        raise CodingError(f"{path}: format must be {CODES_FORMAT!r}")
    codes = doc.get("codes")
    if not isinstance(codes, dict):
        raise CodingError(f"{path}: 'codes' must be an object keyed by rule id")
    checked = {}
    for rid, code in codes.items():
        if not cf.RULE_ID_RE.match(rid):
            raise CodingError(f"{path}: {rid!r} is not a rule id (study1:<sheet>:row<r>:stmt<k>)")
        checked[rid] = validate_code(code, f"{Path(path).name}: {rid}")
    return {"coder": doc.get("coder"), "codebook_version": doc.get("codebook_version"), "codes": checked}


def write_codes(path: Path, coder: str, codes: Mapping[str, Mapping[str, Any]]) -> None:
    doc = {"format": CODES_FORMAT, "coder": coder, "codebook_version": CODEBOOK_VERSION,
           "codes": {rid: codes[rid] for rid in sorted(codes)}}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(doc, indent=2) + "\n")


def notes_leaking_text(rules: Sequence[cf.SourceRule], codes: Mapping[str, Mapping[str, Any]]) -> List[str]:
    labels = {rid: {"note": c.get("note")} for rid, c in codes.items() if c.get("note")}
    return cf.labels_leaking_text(rules, labels)


# ─────────────────────────────────────────────────────────────────────────────
# Worksheets
# ─────────────────────────────────────────────────────────────────────────────

WORKSHEET_FIELDS = ("scope", "not_property_reason", "kind", "modality", "named_polarity",
                    "target_class", "target_actor", "condition", "condition_actor") + BOOLS + ("note",)
WORKSHEET_COLUMNS = ("id", "cell", "survey_category", "survey_exception", "statement") + WORKSHEET_FIELDS


def select_rules(rules: Sequence[cf.SourceRule], sample: Optional[int], sample_seed: int,
                 order_seed: Optional[int]) -> List[cf.SourceRule]:
    """The pilot subset is the same for every coder; the order differs per coder."""
    chosen = list(rules)
    if sample:
        ids = sorted(r.rule_id for r in rules)
        keep = set(random.Random(sample_seed).sample(ids, min(sample, len(ids))))
        chosen = [r for r in rules if r.rule_id in keep]
    if order_seed is not None:
        random.Random(order_seed).shuffle(chosen)
    return chosen


def write_worksheet(rules: Sequence[cf.SourceRule], path: Path) -> Path:
    cf.assert_ignored(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(WORKSHEET_COLUMNS)
        w.writerow(["# allowed values"] + [""] * 4 + [
            "|".join(ENUMS[f]) if f in ENUMS else ("yes|no" if f in BOOLS else "free text, your own words")
            for f in WORKSHEET_FIELDS])
        for r in rules:
            w.writerow([r.rule_id, r.cell, r.category or "", "" if r.has_exception is None
                        else ("yes" if r.has_exception else "no"), r.text] + [""] * len(WORKSHEET_FIELDS))
    return path


def import_worksheet(path: Path, allow_incomplete: bool = False) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Codes from a filled-in worksheet; the statement column is never read into the result."""
    codes: Dict[str, Dict[str, Any]] = {}
    uncoded: List[str] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            rid = (row.get("id") or "").strip()
            if not rid or rid.startswith("#"):
                continue
            if not cf.RULE_ID_RE.match(rid):
                raise CodingError(f"{path.name}: {rid!r} is not a rule id")
            raw = {f: (row.get(f) or "").strip() for f in WORKSHEET_FIELDS}
            if not raw["scope"]:
                uncoded.append(rid)
                continue
            code = {k: v for k, v in raw.items() if v != ""}
            try:
                codes[rid] = validate_code(code, f"{path.name}: {rid}")
            except CodingError:
                if not allow_incomplete:
                    raise
                uncoded.append(rid)
    if uncoded and not allow_incomplete:
        raise CodingError(f"{path.name}: {len(uncoded)} rows are not coded yet (first: {uncoded[0]}); "
                          f"pass --allow-incomplete to import the rest")
    return codes, uncoded


# ─────────────────────────────────────────────────────────────────────────────
# Agreement and adjudication
# ─────────────────────────────────────────────────────────────────────────────

def cohen_kappa(a: Sequence[Any], b: Sequence[Any]) -> Optional[float]:
    """Cohen's kappa for two raters over the same items (None when chance agreement is 1)."""
    if len(a) != len(b):
        raise ValueError("raters must code the same items")
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def agreement(a: Mapping[str, Mapping[str, Any]], b: Mapping[str, Mapping[str, Any]],
              assumptions: Optional[Mapping[str, bool]] = None) -> Dict[str, Any]:
    common = sorted(set(a) & set(b))
    both_prop = [rid for rid in common if a[rid]["scope"] == b[rid]["scope"] == "property"]
    fields: Dict[str, Any] = {}
    for f in AGREEMENT_FIELDS:
        if f == "scope":
            items = common
        elif f == "condition_actor":
            items = [rid for rid in both_prop if a[rid]["condition"] != "none" and b[rid]["condition"] != "none"]
        else:
            items = both_prop
        xa = [a[rid].get(f) for rid in items]
        xb = [b[rid].get(f) for rid in items]
        fields[f] = {"n": len(items), "kappa": _round(cohen_kappa(xa, xb)),
                     "percentAgreement": _round(sum(x == y for x, y in zip(xa, xb)) / len(items)) if items else None}
    assumptions = dict(DEFAULT_ASSUMPTIONS, **(assumptions or {}))
    outcomes: Dict[str, Any] = {}
    for arch in ARCHITECTURES:
        oa = [outcome_of(a[rid], arch, **assumptions) for rid in both_prop]
        ob = [outcome_of(b[rid], arch, **assumptions) for rid in both_prop]
        outcomes[arch] = {"n": len(both_prop), "kappa": _round(cohen_kappa(oa, ob)),
                          "percentAgreement": _round(sum(x == y for x, y in zip(oa, ob)) / len(oa)) if oa else None}
    disagreements = sorted(rid for rid in common if a[rid] != b[rid])
    return {"n": len(common), "onlyA": sorted(set(a) - set(b)), "onlyB": sorted(set(b) - set(a)),
            "fields": fields, "derivedOutcomes": outcomes, "assumptions": assumptions,
            "disagreements": disagreements}


def adjudicate(a: Mapping[str, Mapping[str, Any]], b: Mapping[str, Mapping[str, Any]],
               resolved: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Gold codes: the shared code where the two agree, the resolution where they do not."""
    if set(a) != set(b):
        raise CodingError(f"coders must code the same statements; differ on "
                          f"{sorted(set(a) ^ set(b))[:5]}")
    gold: Dict[str, Dict[str, Any]] = {}
    missing = []
    for rid in sorted(a):
        if a[rid] == b[rid]:
            gold[rid] = dict(a[rid])
        elif rid in resolved:
            gold[rid] = dict(resolved[rid])
        else:
            missing.append(rid)
    if missing:
        raise CodingError(f"{len(missing)} disagreements have no resolution (first: {missing[0]})")
    unused = sorted(set(resolved) - set(a))
    if unused:
        raise CodingError(f"resolutions for statements nobody coded: {unused[:5]}")
    return gold


# ─────────────────────────────────────────────────────────────────────────────
# From a coded property to a plant
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Architecture:
    refuse_on: bool      # the guard may refuse an on-direction command
    refuse_off: bool     # ... an off-direction command
    force_on: bool       # the system may itself take an on-direction action
    force_off: bool      # ... an off-direction action
    description: str


ARCHITECTURES: Dict[str, Architecture] = {
    "guard_dw": Architecture(True, False, False, False,
                             "DeviceWeave today: refuses turn-on; never refuses turn-off; never acts"),
    "guard_any": Architecture(True, True, False, False, "a guard that may refuse any command; never acts"),
    "guard_dw_obligation": Architecture(True, False, False, True,
                                        "DeviceWeave plus a forced turn-off obligation"),
    "tap": Architecture(False, False, True, True, "trigger-action automation: acts, never refuses"),
    "guard_tap": Architecture(True, True, True, True, "a guard and an actuator together"),
}
DEFAULT_ASSUMPTIONS = {"reaction": True, "manual": False}
ASSUMPTION_GRID = [{"reaction": r, "manual": m} for r in (True, False) for m in (False, True)]
OUTCOMES = ("exact", "preemptive", "over_restrictive", "impossible", "unsatisfiable")
BAD = ("BAD",)


def _condition_actor(code: Mapping[str, Any]) -> Optional[str]:
    if code["kind"] == "state_pair":
        return code.get("condition_actor", "system")
    if code["condition"] == "none":
        return None
    # A multi_condition statement is one abstract condition, as controllable as
    # its least controllable part; the codebook has the coder record that actor.
    return code.get("condition_actor", "world")


def _variable_events(prefix: str, actor: str, arch: Architecture, manual: bool) -> List[Tuple[Event, int]]:
    """(event, value it sets) for one boolean the property mentions."""
    if actor == "system":
        evs = [(Event(f"{prefix}.req_on", arch.refuse_on, False, "command"), 1),
               (Event(f"{prefix}.req_off", arch.refuse_off, False, "command"), 0)]
        if arch.force_on:
            evs.append((Event(f"{prefix}.sys_on", True, True, "obligation"), 1))
        if arch.force_off:
            evs.append((Event(f"{prefix}.sys_off", True, True, "obligation"), 0))
        if manual:
            evs += [(Event(f"{prefix}.manual_on", False, False, "manual"), 1),
                    (Event(f"{prefix}.manual_off", False, False, "manual"), 0)]
        return evs
    if actor == "mixed":
        # The system can command it, and the world or a person also changes it
        # (a room's temperature, a faucet someone can turn by hand).
        return _variable_events(prefix, "system", arch, manual) + [
            (Event(f"{prefix}.world_on", False, False, "context"), 1),
            (Event(f"{prefix}.world_off", False, False, "context"), 0)]
    kind = "manual" if actor == "human" else "context"
    return [(Event(f"{prefix}.{actor}_on", False, False, kind), 1),
            (Event(f"{prefix}.{actor}_off", False, False, kind), 0)]


def _violating_base(code: Mapping[str, Any], t: int, x: int) -> bool:
    """Is the property's forbidden condition true in this valuation (before any duration)?"""
    named = t if code["named_polarity"] == "on" else 1 - t
    if code["kind"] == "state_pair":
        return bool(named and x) if code["modality"] == "never" else named != x
    cond = 1 if _condition_actor(code) is None else x
    if code["modality"] == "never":
        return bool(named and cond)
    return bool((not named) and cond)


def build_property_plant(code: Mapping[str, Any], arch: Architecture,
                         reaction: bool = True, manual: bool = False) -> Optional[Plant]:
    """One abstract plant for a coded property, or None when no state satisfies it.

    State: (t, x, k, p). t is the target in its on-direction state; x the
    condition (0 when there is none); k a 0-2 counter of steps the base
    violation has lasted (only with duration / within_after); p marks a
    violation the system still has one step to repair (only with reaction).
    BAD is an absorbing sink. Only states reachable from the initial state are
    included, so "every legal state is kept" means every one that can happen.
    """
    kind = code["kind"]
    timed = bool(code.get("duration") or code.get("within_after"))
    cond_actor = _condition_actor(code)
    t_events = _variable_events("target", code["target_actor"], arch, manual)
    x_events = _variable_events("condition", cond_actor, arch, manual) if cond_actor else []
    events: Dict[str, Event] = {ev.name: ev for ev, _ in t_events + x_events}
    if timed or reaction:
        # Time passing. It advances a duration counter, and it is the deadline
        # for a pending violation: the system's one step of grace ends at the next tick.
        events["tick"] = Event("tick", False, False, "context")
    forbidden_transition = kind == "event" and code["modality"] == "never"
    named_value = 1 if code["named_polarity"] == "on" else 0

    def violating(t: int, x: int, k: int) -> bool:
        if forbidden_transition:
            return False
        if timed:
            return k >= 2
        return _violating_base(code, t, x)

    def step(state: Tuple[int, int, int, int], name: str) -> Optional[Tuple[Any, ...]]:
        t, x, k, p = state
        ev = events[name]
        if name == "tick":
            if p == 1 and violating(t, x, k):
                return BAD    # the grace step passed without a repair
            if not (timed and _violating_base(code, t, x) and k < 2):
                return None
            nt, nx, nk = t, x, k + 1
        else:
            target_side = name.startswith("target.")
            value = next(v for e, v in (t_events if target_side else x_events) if e.name == name)
            nt, nx = (value, x) if target_side else (t, value)
            if (nt, nx) == (t, x):
                return None  # no self-loops: an event that changes nothing does not occur
            if forbidden_transition and target_side and nt == named_value and nt != t:
                cond_true = cond_actor is None or x == 1
                if cond_true:
                    return BAD
            nk = k if (timed and _violating_base(code, nt, nx)) else 0
        if not violating(nt, nx, nk):
            return (nt, nx, nk, 0)
        uncontrollable = not ev.controllable
        if p == 1 and uncontrollable:
            return BAD        # the system did not repair the violation in time
        if uncontrollable and reaction:
            return (nt, nx, nk, 1)
        return (nt, nx, nk, 0)  # a violation the system itself caused, or no grace: bad

    def is_bad(state: Tuple[Any, ...]) -> bool:
        if state == BAD:
            return True
        t, x, k, p = state
        return violating(t, x, k) and p == 0

    initial = None
    for t0 in (0, 1):
        candidate = (t0, 0, 0, 0)
        if not is_bad(candidate):
            initial = candidate
            break
    if initial is None:
        return None
    states = {initial}
    delta: Dict[Tuple[Any, str], Any] = {}
    queue = deque([initial])
    while queue:
        s = queue.popleft()
        if s == BAD:
            continue
        for name in events:
            tgt = step(s, name)
            if tgt is None:
                continue
            delta[(s, name)] = tgt
            if tgt not in states:
                states.add(tgt)
                queue.append(tgt)
    bad = frozenset(s for s in states if is_bad(s))
    return Plant(states=frozenset(states), initial=initial, bad=bad, events=events, delta=delta)


def outcome_of(code: Mapping[str, Any], arch_name: str, reaction: bool = True, manual: bool = False) -> str:
    """exact | preemptive | over_restrictive | impossible | unsatisfiable."""
    plant = build_property_plant(code, ARCHITECTURES[arch_name], reaction=reaction, manual=manual)
    if plant is None:
        return "unsatisfiable"
    syn = supcon_forcing(plant)
    if not syn.closed_loop_safe:
        return "impossible"
    if not syn.retains_legal:
        return "over_restrictive"
    pending = {s for s in plant.states if s != BAD and s[3] == 1}
    if any(s not in pending for s in syn.forcing & syn.reachable):
        return "preemptive"
    return "exact"


def deviceweave_dsl_fit(code: Mapping[str, Any]) -> bool:
    """Could DeviceWeave's policy DSL even state this property?"""
    return (code.get("scope") == "property" and code["kind"] == "state" and code["modality"] == "never"
            and code["named_polarity"] == "on" and code["target_class"] in DEVICEWEAVE_DEVICES
            and code["target_actor"] == "system" and code["condition"] in DEVICEWEAVE_CONDITIONS
            and not code["duration"])


# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

def _round(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(x, nd)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> Optional[Tuple[float, float]]:
    """Wilson score interval for a binomial proportion (95% by default)."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _share(k: int, n: int) -> Dict[str, Any]:
    return {"k": k, "n": n, "share": _round(k / n) if n else None, "ci95": wilson(k, n)}


def _rows_by_id(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if path is None:
        return {}
    out = {}
    for line in Path(path).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row
    return out


def _accepted(row: Mapping[str, Any]) -> Optional[bool]:
    for key in ("accepted", "valid"):
        if key in row:
            return bool(row[key])
    return None


def analyze(gold: Mapping[str, Mapping[str, Any]], rows: Optional[Mapping[str, Mapping[str, Any]]] = None,
            total_statements: Optional[int] = None,
            agreement_report: Optional[Mapping[str, Any]] = None,
            verification_report: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    rows = rows or {}
    ids = sorted(gold)
    props = [rid for rid in ids if gold[rid]["scope"] == "property"]
    n_props = len(props)
    res: Dict[str, Any] = {
        "codebookVersion": CODEBOOK_VERSION,
        "statementsCoded": len(ids),
        "statementsTotal": total_statements,
        "properties": _share(n_props, len(ids)),
        "notPropertyReasons": dict(Counter(gold[r].get("not_property_reason") for r in ids
                                           if gold[r]["scope"] != "property")),
        "kind": {k: _share(sum(gold[r]["kind"] == k for r in props), n_props) for k in ENUMS["kind"]},
        "modality": {m: _share(sum(gold[r]["modality"] == m for r in props), n_props) for m in ENUMS["modality"]},
        "targetActor": {a: _share(sum(gold[r]["target_actor"] == a for r in props), n_props) for a in ACTORS},
        "condition": {c: _share(sum(gold[r]["condition"] == c for r in props), n_props) for c in CONDITIONS},
        "targetClass": dict(Counter(gold[r]["target_class"] for r in props).most_common()),
        "duration": _share(sum(gold[r]["duration"] for r in props), n_props),
        "exceptionInText": _share(sum(gold[r]["exception_in_text"] for r in props), n_props),
        "fitsAutotapTemplate": _share(sum(gold[r]["fits_autotap_template"] for r in props), n_props),
        "fitsDeviceweaveDsl": _share(sum(deviceweave_dsl_fit(gold[r]) for r in props), n_props),
        "architectures": {name: a.description for name, a in ARCHITECTURES.items()},
        "outcomes": {},
    }
    for assume in ASSUMPTION_GRID:
        key = f"reaction={'on' if assume['reaction'] else 'off'},manual={'on' if assume['manual'] else 'off'}"
        per_arch = {}
        for arch in ARCHITECTURES:
            outs = Counter(outcome_of(gold[r], arch, **assume) for r in props)
            per_arch[arch] = {o: _share(outs.get(o, 0), n_props) for o in OUTCOMES}
            per_arch[arch]["notExact"] = _share(n_props - outs.get("exact", 0), n_props)
        res["outcomes"][key] = per_arch
    default_key = "reaction=on,manual=off"
    res["headline"] = {
        "assumptions": default_key,
        "guardDwNotExact": res["outcomes"][default_key]["guard_dw"]["notExact"],
        "guardDwImpossible": res["outcomes"][default_key]["guard_dw"]["impossible"],
        "tapExact": res["outcomes"][default_key]["tap"]["exact"],
        "guardDwObligationExact": res["outcomes"][default_key]["guard_dw_obligation"]["exact"],
    }
    if agreement_report is not None:
        res["agreement"] = {
            "n": agreement_report.get("n"),
            "coders": agreement_report.get("coders"),
            "fields": agreement_report.get("fields"),
            "derivedOutcomes": agreement_report.get("derivedOutcomes"),
            "disagreements": len(agreement_report.get("disagreements") or ()),
        }
    if verification_report is not None:
        res["verification"] = dict(verification_report)
    if rows:
        # Compile coverage over every statement with a compile result, coded or not.
        all_rows = list(rows.values())
        n_rows = len(all_rows)
        res["compile"] = {
            "statements": n_rows,
            "compiled": _share(sum(r.get("compile") == "compiled" for r in all_rows), n_rows),
            "valid": _share(sum(bool(r.get("valid")) for r in all_rows), n_rows),
            "accepted": _share(sum(bool(_accepted(r)) for r in all_rows), n_rows),
        }
        scored = [r for r in props if r in rows and _accepted(rows[r]) is not None]
        accepted = [r for r in scored if _accepted(rows[r])]
        under = [r for r in accepted if outcome_of(gold[r], "guard_dw", **DEFAULT_ASSUMPTIONS) != "exact"]
        res["compileCrossTab"] = {
            "propertiesWithCompileResult": len(scored),
            "accepted": _share(len(accepted), len(scored)),
            # The silent failure: the compiler produced a policy the validator
            # accepted, but a refusal-only guard does not enforce the property.
            "acceptedNotEnforcedByGuard": _share(len(under), len(accepted)),
            "acceptedOutcomes": dict(Counter(outcome_of(gold[r], "guard_dw", **DEFAULT_ASSUMPTIONS)
                                             for r in accepted)),
        }
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Human verification of LLM codes (the survey page)
#
# One person verifies every LLM code. On a seeded 20% blind subset they code
# from scratch without seeing the LLM's answer; that subset gives an unbiased
# human-vs-LLM kappa. On the rest they confirm or change a pre-selected
# answer; there we report how often each field was changed. The verified
# answers are the gold codes.
# ─────────────────────────────────────────────────────────────────────────────

def load_answers(path: Path) -> Dict[str, Dict[str, Any]]:
    """Answers exported from the survey store: a directory of <rule id>.json, or one JSON object."""
    path = Path(path)
    raw: Dict[str, Any] = {}
    if path.is_dir():
        for f in sorted(path.rglob("*.json")):
            doc = json.loads(f.read_text())
            raw[doc.get("id") or f.stem] = doc.get("data", doc) if isinstance(doc.get("data"), dict) else doc
    else:
        raw = json.loads(path.read_text())
    out = {}
    for rid, doc in raw.items():
        if not cf.RULE_ID_RE.match(rid):
            raise CodingError(f"{rid!r} is not a rule id")
        out[rid] = {"code": validate_code(doc["code"], f"answer {rid}"), "blind": bool(doc.get("blind")),
                    "seconds": doc.get("seconds")}
    return out


def verification(answers: Mapping[str, Mapping[str, Any]], llm: Mapping[str, Mapping[str, Any]],
                 blind_ids: Iterable[str]) -> Dict[str, Any]:
    blind = set(blind_ids)
    for rid, a in answers.items():
        if a["blind"] != (rid in blind):
            raise CodingError(f"{rid}: the page and the blind list disagree about whether it is blind")
    gold = {rid: a["code"] for rid, a in answers.items()}
    blind_human = {rid: gold[rid] for rid in gold if rid in blind and rid in llm}
    blind_llm = {rid: llm[rid] for rid in blind_human}
    report = agreement(blind_human, blind_llm)
    report["coders"] = ["human (blind)", "llm"]
    suggested = [rid for rid in gold if rid not in blind and rid in llm]
    changed_any = [rid for rid in suggested if gold[rid] != llm[rid]]
    per_field = {}
    for f in AGREEMENT_FIELDS:
        items = [rid for rid in suggested if f in gold[rid] or f in llm[rid]]
        per_field[f] = _share(sum(gold[rid].get(f) != llm[rid].get(f) for rid in items), len(items))
    secs = sorted(a["seconds"] for a in answers.values() if isinstance(a.get("seconds"), (int, float)))
    return {"gold": gold, "agreement": report, "verification": {
        "answered": len(answers), "llmCoded": len(llm), "blindAnswered": len(blind_human),
        "suggestionsAnswered": len(suggested),
        "suggestionsChanged": _share(len(changed_any), len(suggested)),
        "changedByField": per_field,
        "medianSeconds": secs[len(secs) // 2] if secs else None,
    }}


# ─────────────────────────────────────────────────────────────────────────────
# Rendering the paper draft
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.,=\-]+)(?:\|(pct|ci|int|f2|raw))?\s*\}\}")


def _lookup(results: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = results
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def render(template: str, results: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """Replace {{key|fmt}} with values from results; unknown keys are left marked and reported."""
    missing: List[str] = []

    def sub(m: "re.Match[str]") -> str:
        key, fmt = m.group(1), m.group(2) or "raw"
        try:
            value = _lookup(results, key)
        except KeyError:
            missing.append(key)
            return f"[[MISSING {key}]]"
        if fmt == "pct":
            v = value["share"] if isinstance(value, Mapping) else value
            return "n/a" if v is None else f"{100 * v:.1f}%"
        if fmt == "ci":
            ci = value.get("ci95") if isinstance(value, Mapping) else value
            return "n/a" if not ci else f"[{100 * ci[0]:.1f}%, {100 * ci[1]:.1f}%]"
        if fmt == "f2":
            return "n/a" if value is None else f"{float(value):.2f}"
        if fmt == "int":
            return str(value["k"] if isinstance(value, Mapping) else int(value))
        return str(value)

    return _PLACEHOLDER.sub(sub, template), missing


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worksheet")
    w.add_argument("--coder", required=True)
    w.add_argument("--xlsx", default=None)
    w.add_argument("--sample", type=int, default=None, help="pilot: code only N statements")
    w.add_argument("--sample-seed", type=int, default=2026)
    w.add_argument("--order-seed", type=int, default=None, help="default: derived from --coder")
    w.add_argument("--out", default=None)
    i = sub.add_parser("import")
    i.add_argument("worksheet")
    i.add_argument("--coder", required=True)
    i.add_argument("--out", required=True)
    i.add_argument("--allow-incomplete", action="store_true")
    i.add_argument("--xlsx", default=None, help="check notes against statement text (recommended)")
    g = sub.add_parser("agree")
    g.add_argument("a")
    g.add_argument("b")
    g.add_argument("--out", default=str(CODING_OUT / "agreement.json"))
    j = sub.add_parser("adjudicate")
    j.add_argument("a")
    j.add_argument("b")
    j.add_argument("resolved")
    j.add_argument("--out", required=True)
    v = sub.add_parser("verify", help="survey answers + LLM codes -> gold codes, blind agreement, change rates")
    v.add_argument("answers", help="ArtifactData export directory (out_dir) or a JSON object of answers")
    v.add_argument("--llm", default=str(ROOT / "benchmarks" / "autotap" / "coding" / "llm.json"))
    v.add_argument("--blind", default=str(ROOT / "benchmarks" / "autotap" / "coding" / "blind_ids.json"))
    v.add_argument("--out-dir", default=str(CODING_OUT))
    n = sub.add_parser("analyze")
    n.add_argument("gold")
    n.add_argument("--rows", default=None, help="compile_fidelity rows.jsonl for the cross-tab")
    n.add_argument("--total", type=int, default=None, help="statements in the corpus (default: coded count)")
    n.add_argument("--agreement", default=None, help="the agree report, to include kappas in the results")
    n.add_argument("--verification", default=None, help="the verify report, to include change rates")
    n.add_argument("--out", default=str(CODING_OUT / "results.json"))
    r = sub.add_parser("render")
    r.add_argument("results")
    r.add_argument("--template", default=str(ROOT / "docs" / "paper" / "draft.md"))
    r.add_argument("--out", default=str(CODING_OUT / "draft.rendered.md"))
    args = ap.parse_args(argv)

    try:
        if args.cmd == "worksheet":
            rules = cf.load_rules(cf.resolve_xlsx_path(args.xlsx))
            order_seed = args.order_seed if args.order_seed is not None else sum(map(ord, args.coder))
            chosen = select_rules(rules, args.sample, args.sample_seed, order_seed)
            out = Path(args.out) if args.out else CODING_OUT / f"worksheet-{args.coder}.csv"
            write_worksheet(chosen, out)
            print(f"wrote {out} ({len(chosen)} statements). It contains participant text: keep it "
                  f"in the git-ignored output directory.")
            return 0
        if args.cmd == "import":
            codes, uncoded = import_worksheet(Path(args.worksheet), args.allow_incomplete)
            if args.xlsx:
                leaks = notes_leaking_text(cf.load_rules(cf.resolve_xlsx_path(args.xlsx)), codes)
                if leaks:
                    raise CodingError(f"notes repeat the statement text: {leaks}; reword them")
            write_codes(Path(args.out), args.coder, codes)
            print(f"imported {len(codes)} codes ({len(uncoded)} uncoded) -> {args.out}")
            return 0
        if args.cmd == "agree":
            a, b = load_codes(Path(args.a)), load_codes(Path(args.b))
            report = agreement(a["codes"], b["codes"])
            report["coders"] = [a["coder"], b["coder"]]
            cf.assert_ignored(Path(args.out).parent)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
            for f, v in report["fields"].items():
                print(f"  {f:<24} n={v['n']:<4} kappa={v['kappa']}  agree={v['percentAgreement']}")
            print(f"  {len(report['disagreements'])} statements need adjudication -> {args.out}")
            return 0
        if args.cmd == "adjudicate":
            a, b = load_codes(Path(args.a)), load_codes(Path(args.b))
            resolved = load_codes(Path(args.resolved))["codes"]
            gold = adjudicate(a["codes"], b["codes"], resolved)
            write_codes(Path(args.out), "gold", gold)
            print(f"wrote {len(gold)} gold codes -> {args.out}")
            return 0
        if args.cmd == "verify":
            answers = load_answers(Path(args.answers))
            llm = load_codes(Path(args.llm))["codes"]
            blind = json.loads(Path(args.blind).read_text())["ids"]
            result = verification(answers, llm, blind)
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            write_codes(out / "gold.json", "human-verified", result["gold"])
            (out / "agreement.json").write_text(json.dumps(result["agreement"], indent=2) + "\n")
            (out / "verification.json").write_text(json.dumps(result["verification"], indent=2) + "\n")
            ver = result["verification"]
            print(f"{ver['answered']} answers; blind {ver['blindAnswered']}; suggestions changed "
                  f"{ver['suggestionsChanged']['share']}")
            for f, a in result["agreement"]["fields"].items():
                print(f"  blind kappa {f:<24} {a['kappa']}  (n={a['n']})")
            return 0
        if args.cmd == "analyze":
            gold = load_codes(Path(args.gold))["codes"]
            agreement_report = json.loads(Path(args.agreement).read_text()) if args.agreement else None
            verification_report = json.loads(Path(args.verification).read_text()) if args.verification else None
            res = analyze(gold, _rows_by_id(Path(args.rows) if args.rows else None), args.total,
                          agreement_report, verification_report)
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(res, indent=2) + "\n")
            print(json.dumps(res["headline"], indent=2))
            print(f"wrote {out}")
            return 0
        if args.cmd == "render":
            results = json.loads(Path(args.results).read_text())
            text, missing = render(Path(args.template).read_text(), results)
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
            print(f"wrote {out}")
            if missing:
                print(f"{len(set(missing))} placeholders have no value: {sorted(set(missing))}", file=sys.stderr)
                return 3
            return 0
    except cf.FidelityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
