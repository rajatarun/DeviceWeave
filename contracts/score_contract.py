"""Check scores against the shared score contract (``score_envelope.json``).

Dependency-free, like ``conformance.py``, so every weave repository can vendor
this file and its JSON byte-identical and run the same checks. Three things a
repository does with it:

1. **Declare** every [0, 1]-looking number it emits in a registry
   (``contracts/scores.json``): its kind, range, source, whether it is
   calibrated, what it means, and the code that produces it.
   ``check_registry`` holds the declaration to the contract.
2. **Emit** envelopes rather than bare floats where a score leaves the
   service: ``envelope(value, entry)`` builds one from a registry entry and
   ``check_envelope`` validates it.
3. **Combine** only what may be combined: ``can_combine(a, b, op)`` answers
   R1/R3 from the two envelopes and names the rule when the answer is no.

It also carries the calibration tools R2 asks for, so "calibrated: true" can
be earned with a number rather than asserted: ``calibration_report`` gives
Brier, ECE and a reliability table for (score, outcome) pairs, and
``isotonic_fit`` the monotone map from score to observed frequency (pool
adjacent violators), which is what a score that is only claimed to be
*ordered* correctly should be calibrated with.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

CONTRACT_FILENAME = "score_envelope.json"
REGISTRY_FILENAME = "scores.json"


def load_contract(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else Path(__file__).with_name(CONTRACT_FILENAME)
    with open(target, encoding="utf-8") as fh:
        return json.load(fh)


def load_registry(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else Path(__file__).with_name(REGISTRY_FILENAME)
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["scores"] if isinstance(data, dict) else data


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_registry_entry(entry: dict[str, Any], contract: dict[str, Any] | None = None) -> list[str]:
    contract = contract or load_contract()
    name = entry.get("name", "<unnamed>")
    problems = [f"{name}: missing '{k}'" for k in contract["registry_required"] if k not in entry]
    kind = entry.get("kind")
    spec = contract["kinds"].get(kind)
    if spec is None:
        problems.append(f"{name}: unknown kind {kind!r}; one of {sorted(contract['kinds'])}")
        return problems
    for k in spec["requires"]:
        if k == "calibrated":
            continue
        if entry.get(k) in (None, ""):
            problems.append(f"{name}: kind '{kind}' requires '{k}'")
    if spec["must_be_calibrated"] and entry.get("calibrated") is not True:
        problems.append(f"{name}: a probability must be calibrated (R2); declare it as 'score' until it is")
    if entry.get("calibrated") is True and not entry.get("calibration_ref"):
        problems.append(f"{name}: calibrated scores must say against what (calibration_ref)")
    if entry.get("source") not in contract["sources"]:
        problems.append(f"{name}: unknown source {entry.get('source')!r}")
    rng = entry.get("range")
    if not (isinstance(rng, list) and len(rng) == 2 and (rng[0] is None or _num(rng[0]))
            and (rng[1] is None or _num(rng[1]))):
        problems.append(f"{name}: range must be [lo, hi] with null for unbounded")
    elif rng[0] is not None and rng[1] is not None and rng[0] > rng[1]:
        problems.append(f"{name}: range lower bound above upper bound")
    return problems


def check_producer(entry: dict[str, Any]) -> list[str]:
    """``producer`` is ``module:attribute``; it must import. A registry naming code
    that no longer exists documents a score nobody emits."""
    ref = entry.get("producer", "")
    mod, _, attr = ref.partition(":")
    try:
        obj = importlib.import_module(mod)
        for part in filter(None, attr.split(".")):
            obj = getattr(obj, part)
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        return [f"{entry.get('name')}: producer {ref!r} does not resolve ({type(exc).__name__}: {exc})"]
    return []


def check_registry(entries: Sequence[dict[str, Any]], contract: dict[str, Any] | None = None,
                   *, resolve_producers: bool = True) -> list[str]:
    contract = contract or load_contract()
    problems: list[str] = []
    names = [e.get("name") for e in entries]
    problems += [f"duplicate score name {n!r}" for n in {n for n in names if names.count(n) > 1}]
    for e in entries:
        problems += check_registry_entry(e, contract)
        if resolve_producers:
            problems += check_producer(e)
    return problems


def envelope(value: float | None, entry: dict[str, Any], *, evidence: int | None = None,
             **extra: Any) -> dict[str, Any]:
    """An envelope for ``value`` as declared by ``entry``. ``None`` stays ``None`` (R5)."""
    env = {
        "value": None if value is None else float(value),
        "observed": value is not None,
        "name": entry["name"],
        "kind": entry["kind"],
        "source": entry["source"],
        "calibrated": bool(entry.get("calibrated")),
    }
    for k in ("event", "neutral", "calibration_ref", "producer_scope"):
        if entry.get(k) is not None:
            env[k] = entry[k]
    if evidence is not None:
        env["evidence"] = int(evidence)
    env.update(extra)
    return env


def check_envelope(env: dict[str, Any], contract: dict[str, Any] | None = None,
                   entry: dict[str, Any] | None = None) -> list[str]:
    contract = contract or load_contract()
    problems = [f"missing '{k}'" for k in contract["envelope_required"] if k not in env]
    spec = contract["kinds"].get(env.get("kind"))
    if spec is None:
        return problems + [f"unknown kind {env.get('kind')!r}"]
    value = env.get("value")
    if value is None:
        if env.get("observed") is not False:
            problems.append("a null value must carry observed: false (R5)")
    else:
        if not _num(value):
            problems.append(f"value must be a number or null, got {type(value).__name__}")
        elif entry is not None:
            lo, hi = entry["range"]
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                problems.append(f"value {value} outside declared range {entry['range']}")
    if spec["must_be_calibrated"] and env.get("calibrated") is not True:
        problems.append("a probability must be calibrated (R2)")
    for k in spec["requires"]:
        if k not in ("calibrated", "evidence") and env.get(k) in (None, ""):
            problems.append(f"kind '{env['kind']}' requires '{k}'")
    if "evidence" in spec["requires"] and env.get("observed", True) and "evidence" not in env:
        problems.append(f"kind '{env['kind']}' must carry its evidence count (R4)")
    return problems


def can_combine(a: dict[str, Any], b: dict[str, Any], op: str) -> tuple[bool, str]:
    """May two envelopes be combined by ``op``? (average | add | multiply | noisy_or | max)."""
    if not a.get("observed", True) or not b.get("observed", True):
        return False, "R5: one of the scores was not observed"
    if op in ("multiply", "noisy_or"):
        if a["kind"] != "probability" or b["kind"] != "probability":
            return False, "R1: only probabilities compose by probability rules"
        if not (a.get("calibrated") and b.get("calibrated")):
            return False, "R2: both probabilities must be calibrated"
        return True, "independent calibrated probabilities (independence is the caller's claim)"
    if op in ("average", "add"):
        if a["kind"] != b["kind"]:
            return False, f"R3: cannot {op} a {a['kind']} with a {b['kind']}"
        if a["kind"] == "probability":
            if a.get("event") != b.get("event"):
                return False, "R3: probabilities of different events"
            return True, "same event"
        if a.get("name") != b.get("name"):
            return False, f"R3: two {a['kind']}s from different producers have different scales"
        return True, "same producer"
    if op == "max":
        if a["kind"] == b["kind"] and a.get("name") == b.get("name"):
            return True, "same producer"
        return False, "R3: combine decisions (both gates pass), not numbers"
    return False, f"unknown operation {op!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Calibration (R2)
# ─────────────────────────────────────────────────────────────────────────────

def calibration_report(pairs: Iterable[tuple[float, float]], bins: int = 10) -> dict[str, Any]:
    """Brier, expected calibration error and a reliability table for (score, outcome in [0,1])."""
    pairs = [(float(s), float(o)) for s, o in pairs if s is not None and o is not None]
    if not pairs:
        return {"n": 0, "brier": None, "ece": None, "reliability": []}
    n = len(pairs)
    brier = sum((s - o) ** 2 for s, o in pairs) / n
    table: dict[int, list[tuple[float, float]]] = {}
    for s, o in pairs:
        table.setdefault(min(bins - 1, max(0, int(s * bins))), []).append((s, o))
    rel, ece = [], 0.0
    for b in sorted(table):
        rows = table[b]
        ms = sum(s for s, _ in rows) / len(rows)
        mo = sum(o for _, o in rows) / len(rows)
        ece += len(rows) / n * abs(ms - mo)
        rel.append({"bin": [b / bins, (b + 1) / bins], "n": len(rows),
                    "meanScore": round(ms, 4), "observed": round(mo, 4)})
    return {"n": n, "brier": round(brier, 4), "ece": round(ece, 4), "reliability": rel}


def isotonic_fit(pairs: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone non-decreasing map from score to outcome frequency (pool adjacent violators).

    Returns breakpoints ``[(score_upper, calibrated_value), ...]`` sorted by score;
    ``apply_isotonic`` evaluates it.
    """
    pts = sorted((float(s), float(o)) for s, o in pairs)
    blocks: list[list[float]] = []  # [sum_o, count, max_score]
    for s, o in pts:
        blocks.append([o, 1.0, s])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            so, c, ms = blocks.pop()
            blocks[-1][0] += so
            blocks[-1][1] += c
            blocks[-1][2] = ms
    return [(b[2], b[0] / b[1]) for b in blocks]


def apply_isotonic(fit: Sequence[tuple[float, float]], score: float) -> float:
    for upper, value in fit:
        if score <= upper:
            return value
    return fit[-1][1] if fit else score
