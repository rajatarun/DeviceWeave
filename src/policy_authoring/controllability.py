"""
Which safety rules a BLOCK/MODIFY/ALLOW guard can enforce at all.

The policy evaluator (``policy_engine/evaluator.py``) is a command guard:
it may refuse a controllable command in the current context, and it never
fires an actuator by itself. ``turn_off`` and ``get_status`` are in
``_SAFE_ACTIONS``, so the guard cannot refuse them. Presence, weather and a
physical switch change the world whether or not a rule matches. Supervisory
control theory says when that guard is enough, when a forced action is
required, and when no supervisor of either kind exists.

Controllability
---------------
Ramadge and Wonham, SIAM J. Control Optim. 25(1):206-230, 1987,
doi:10.1137/0325013, reduce the existence of a supervisor to controllability
of the legal language. Wonham and Ramadge, SIAM J. Control Optim.
25(3):637-659, 1987, doi:10.1137/0325036, show the supremal controllable
sublanguage is the largest fixpoint of a monotone operator. The classical
condition, as stated by Reniers and Cai (arXiv:2404.08469, Definition 2,
citing Ramadge and Wonham 1987) for a specification ``F ⊆ L_m(P)``, is::

    (∀ s ∈ F̄, ∀ u ∈ Σ_u) [ u ∈ E_P(s) ⇒ s u ∈ F̄ ]

Equivalently (standard form; the 1987 SIAM line itself is marked TO VERIFY
in ``docs/controllability.md``)::

    F̄ Σ_u ∩ L(P) ⊆ F̄

A supervisor may delete controllable events only. After a legal prefix, every
uncontrollable event the plant can still do must stay legal.

Event forcing
-------------
Reniers and Cai, "Supervisory Control Theory with Event Forcing",
arXiv:2404.08469, 2024. A forcible event may preempt every other eligible
event. Their Definition 3 (forcible-controllability), verified from the
arXiv HTML, is::

    (∀ s ∈ F̄) [
        (∀ u ∈ Σ_u) [ u ∈ E_P(s) ⇒ s u ∈ F̄ ]
        ∨ ( (∃ f ∈ Σ_f) [ s f ∈ F̄ ] ∧ (∀ σ ∈ Σ \\ Σ_f) [ s σ ∉ F̄ ] )
    ]

Theorem 1 (ar5iv HTML): for a nonempty ``F ⊆ L_m(P)``, a supervisory control
``V`` with ``V/P`` nonblocking and ``L_m(V/P) = F`` exists if and only if
``F`` is forcibly-controllable. Proposition 1: that family is closed under
arbitrary unions, so a unique supremal sublanguage ``F_sup`` exists.
Theorem 2, as rendered on ar5iv, writes ``L_m(V_sup/P) = F``; its proof
applies Theorem 1 to ``F_sup``, and the next paragraph says the realized
language is ``F_sup``. The missing subscript is marked TO VERIFY in
``docs/controllability.md``. Algorithm 1 is ``O(|Q|² |Σ|)`` (Theorem 5,
citing the 1989 Proc. IEEE paper for the classical bound).

This module implements the prefix-closed safety case of that fixpoint.
Every surviving state is treated as marked, so the nonblocking iteration in
Algorithm 1 is vacuous: a marked state, even a deadlock, is nonblocking.
Forbidden states are removed first; a state is then deleted when an
uncontrollable event leaves the remaining set and no forcible event stays
inside it, and it is a forcing state when an uncontrollable event leaves
and some forcible event stays. That is Algorithm 1 lines 19-20 and 26 for a
safety specification. It is not a synthesis of marked-language nonblocking
beyond safety.

Mapping onto DeviceWeave
------------------------
* Controllable, not forcible: commands the guard can refuse (``turn_on``,
  ``set_*``, ``modify``). Not in ``_SAFE_ACTIONS``.
* Uncontrollable, forcible: ``turn_off``. The guard cannot refuse it, and an
  obligation may force it, preempting an uncontrollable departure.
* Uncontrollable, not forcible: ``get_status`` (self-loop), presence and
  weather changes, and ``manual_on`` when that physical event is assumed.
* Precursor: an optional uncontrollable ``depart`` (geofence or door)
  inserted on the path from home to away. It is not a DSL field.

The legal language of a block rule (or of an invariant, which is a block
the rules are supposed to cover) is: the device is not on in a context the
rule matches. ``enforceability`` classifies that language:

* ``GUARD_ENFORCEABLE`` — it is controllable. A BLOCK-only supervisor
  realizes it by disabling controllable events that leave it.
* ``NEEDS_OBLIGATION`` — it is not controllable, but it is forcibly
  controllable. Some legal state has an uncontrollable exit, and a forcible
  ``turn_off`` preempts that exit.
* ``UNENFORCEABLE`` — it is not forcibly controllable. Some legal state can
  be driven into the forbidden set by an event that cannot be refused and
  cannot be preempted.

The supremal controllable sublanguage may still be nonempty when the class
is ``NEEDS_OBLIGATION``: the guard can keep the initial state safe by also
refusing ``turn_on`` in legal contexts (the heater may never be on at home,
because ``leave`` is uncontrollable). That supervisor is reported; it is a
strict subset of the legal language, so the rule is not guard-enforceable.

Nothing here changes ``evaluator.py``, the DSL, or BLOCK > MODIFY > ALLOW.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from policy_authoring.rule_set_checker import (
    BOOLEAN_FIELDS,
    INTEGER_FIELDS,
    NUMERIC_DOMAINS,
    Interval,
    _TYPICAL,
)
from policy_engine.evaluator import _SAFE_ACTIONS, _all_conditions_match

# Classifications of a safety language. Strings are the finding vocabulary.
GUARD_ENFORCEABLE = "GUARD_ENFORCEABLE"
NEEDS_OBLIGATION = "NEEDS_OBLIGATION"
UNENFORCEABLE = "UNENFORCEABLE"

# Standard names classified even when a particular plant does not use them.
_CATALOGUE = (
    "turn_on", "turn_off", "get_status", "set_brightness", "modify",
    "leave", "arrive", "depart", "manual_on",
)

State = Tuple[Any, ...]


@dataclass(frozen=True)
class Event:
    """One plant event and the two independent control flags.

    Controllable: the guard may refuse it. Forcible: a supervisor may
    preempt every non-forcible event by taking it (Reniers and Cai,
    arXiv:2404.08469, Section II). ``turn_off`` is uncontrollable and forcible.
    """
    name: str
    controllable: bool
    forcible: bool
    kind: str  # command | safe_action | context | precursor | manual


@dataclass(frozen=True)
class Cell:
    """One uniform slice of a single context field."""
    field: str
    kind: str  # bool | presence | range | ints
    lo: float = 0.0
    hi: float = 0.0
    lo_closed: bool = True
    hi_closed: bool = True
    bool_value: Optional[bool] = None
    presence: Optional[str] = None

    def label(self) -> Any:
        if self.kind == "bool":
            return self.bool_value
        if self.kind == "presence":
            return self.presence
        if self.kind == "ints":
            lo, hi = int(self.lo), int(self.hi)
            return lo if lo == hi else f"{lo}-{hi}"
        if self.lo == self.hi and self.lo_closed and self.hi_closed:
            return self.lo
        a = "[" if self.lo_closed else "("
        b = "]" if self.hi_closed else ")"
        return f"{a}{self.lo:g},{self.hi:g}{b}"

    def sample(self) -> Any:
        if self.kind == "bool":
            return self.bool_value
        if self.kind == "presence":
            return {"home": True, "away": False}.get(self.presence or "")
        if self.kind == "ints":
            return int(Interval(self.lo, self.hi).sample(True, _TYPICAL.get(self.field, 0)))
        return Interval(self.lo, self.hi, self.lo_closed, self.hi_closed).sample(
            False, _TYPICAL.get(self.field, 0.0))


@dataclass
class Plant:
    """Deterministic finite plant. ``delta[(state, event)] = target``."""
    states: frozenset
    initial: State
    bad: frozenset
    events: Dict[str, Event]
    delta: Dict[Tuple[State, str], State]
    device_types: Tuple[str, ...] = ()
    fields: Tuple[str, ...] = ()
    # Parallel to fields: the Cell objects for each coordinate of a state.
    axes: Tuple[Tuple[Cell, ...], ...] = ()
    # rule ids that contributed a block (or invariant name) per device
    spec_ids: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def successors(self, state: State) -> List[Tuple[str, Event, State]]:
        out = []
        for name, ev in self.events.items():
            tgt = self.delta.get((state, name))
            if tgt is not None:
                out.append((name, ev, tgt))
        return out


@dataclass
class Synthesis:
    """Supremal safe sublanguage under one control mechanism.

    ``good`` is the set of legal states retained. ``forcing`` is empty for
    ``supcon``. ``enabled`` is the closed-loop event set of Equation (2) in
    Reniers and Cai: at a forcing state only forcible events that stay in
    ``good``; elsewhere every event that stays in ``good``, with controllable
    exits disabled.
    """
    good: frozenset
    forcing: frozenset
    enabled: frozenset          # (state, event)
    disabled: frozenset         # controllable (state, event) refused
    reachable: frozenset
    retains_legal: bool
    retains_initial: bool
    closed_loop_safe: bool
    # state -> (event, target) that witnesses why a legal state was removed
    removed_because: Dict[State, Tuple[str, State]] = field(default_factory=dict)
    # forcing state -> (uncontrollable event preempted, forcible event used)
    forced_because: Dict[State, Tuple[str, str]] = field(default_factory=dict)


@dataclass
class EnforceabilityResult:
    classification: str
    device_type: str
    rule_ids: List[str]
    witness_trace: List[Dict[str, Any]]
    minimal_supervisor: Dict[str, Any]
    message: str
    assumptions: Dict[str, Any]
    guard: Synthesis
    forcing: Synthesis
    # True when supcon keeps the initial state (a weaker BLOCK supervisor
    # exists, possibly by refusing commands the legal language would allow).
    blocking_supervisor_exists: bool

    def witness(self) -> Dict[str, Any]:
        bad_ctx = {}
        if self.witness_trace:
            bad_ctx = dict(self.witness_trace[-1].get("state") or {})
        return {
            "classification": self.classification,
            "trace": self.witness_trace,
            "counterexample_context": {
                k: v for k, v in bad_ctx.items() if k not in self._devices()
            },
            "minimal_supervisor": self.minimal_supervisor,
            "assumptions": self.assumptions,
            "blocking_supervisor_exists": self.blocking_supervisor_exists,
        }

    def _devices(self) -> set:
        return set(self.assumptions.get("device_types") or ())


# ─────────────────────────────────────────────────────────────────────────────
# Event classification
# ─────────────────────────────────────────────────────────────────────────────

def event_class(name: str) -> Event:
    """Classify one event name.

    The bare name after a ``device.`` prefix is what matters, so
    ``heater.turn_off`` and ``turn_off`` agree. Membership of ``_SAFE_ACTIONS``
    is what makes a command uncontrollable: the evaluator never applies the
    guard to those actions.
    """
    bare = name.split(".")[-1]
    if bare in _SAFE_ACTIONS:
        # turn_off bypasses the guard and is the obligation an external
        # agent can force. get_status bypasses the guard and changes nothing.
        return Event(name, controllable=False, forcible=(bare == "turn_off"), kind="safe_action")
    if bare == "manual_on":
        return Event(name, controllable=False, forcible=False, kind="manual")
    if bare == "depart":
        return Event(name, controllable=False, forcible=False, kind="precursor")
    if bare in ("turn_on", "modify") or bare.startswith("set_"):
        return Event(name, controllable=True, forcible=False, kind="command")
    return Event(name, controllable=False, forcible=False, kind="context")


def classify_events(event_names: Optional[Sequence[str]] = None,
                    plant: Optional[Plant] = None) -> Dict[str, Event]:
    """Map event names to controllable / forcible / kind.

    With a plant, classify the events that plant actually uses. With a list
    of names, classify those. With neither, classify the standard catalogue
    (commands, safe actions, presence, precursor, manual toggle).
    """
    if plant is not None:
        names: Sequence[str] = tuple(plant.events)
    elif event_names is not None:
        names = event_names
    else:
        names = _CATALOGUE
    return {n: event_class(n) for n in names}


# ─────────────────────────────────────────────────────────────────────────────
# Discretisation — one axis per field the spec actually constrains
# ─────────────────────────────────────────────────────────────────────────────

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _spec_items(rules: Sequence[Mapping[str, Any]],
                invariants: Sequence[Any],
                device: str) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """(id, conditions) for every block and invariant of ``device``."""
    items = []
    for rule in rules:
        scope = rule.get("scope") or {}
        dev = scope.get("device_type") or rule.get("device_type")
        if dev != device:
            continue
        if str((rule.get("action") or {}).get("type")) != "block":
            continue
        items.append((str(rule.get("rule_id", "?")), list(rule.get("conditions") or [])))
    for inv in invariants:
        dev = inv.device_type if hasattr(inv, "device_type") else inv.get("device_type")
        if dev != device:
            continue
        conds = inv.conditions if hasattr(inv, "conditions") else inv.get("conditions") or ()
        name = inv.name if hasattr(inv, "name") else inv.get("name", "invariant")
        items.append((str(name), [dict(c) for c in conds]))
    return items


def _fields_of(items: Sequence[Tuple[str, List[Dict[str, Any]]]]) -> List[str]:
    seen = []
    for _, conds in items:
        for cond in conds:
            f = cond.get("field")
            if f in NUMERIC_DOMAINS or f in BOOLEAN_FIELDS:
                if f not in seen:
                    seen.append(f)
    return seen


def _cuts(field: str, items: Sequence[Tuple[str, List[Dict[str, Any]]]]) -> List[float]:
    lo, hi = NUMERIC_DOMAINS[field]
    found = set()
    for _, conds in items:
        for cond in conds:
            if cond.get("field") != field:
                continue
            v = _num(cond.get("value"))
            if v is None:
                continue
            if field in INTEGER_FIELDS:
                for cand in (math.floor(v), math.ceil(v)):
                    if lo <= cand <= hi:
                        found.add(float(int(cand)))
            elif lo <= v <= hi:
                found.add(round(v, 9))
    return sorted(found)


def _atom_matches(field: str, sample: Any, items) -> Tuple[bool, ...]:
    """How each spec item treats this field at ``sample``.

    Items that do not mention the field contribute True (they do not split it).
    A missing boolean sample (the depart slice) makes an ``is_home`` condition
    fail, matching the evaluator, where an absent field does not match.
    """
    point = {} if sample is None else {field: sample}
    out = []
    for _, conds in items:
        relevant = [c for c in conds if c.get("field") == field]
        if not relevant:
            out.append(True)
            continue
        out.append(_all_conditions_match(relevant, point))
    return tuple(out)


def _real_atoms(field: str, cuts: Sequence[float]) -> List[Cell]:
    lo, hi = NUMERIC_DOMAINS[field]
    if not cuts:
        return [Cell(field, "range", lo, hi, True, True)]
    atoms: List[Cell] = []
    cursor, closed = lo, True
    for c in cuts:
        if c > cursor:
            atoms.append(Cell(field, "range", cursor, c, closed, False))
        atoms.append(Cell(field, "range", c, c, True, True))
        cursor, closed = c, False
    if cursor < hi or (cursor == hi and closed):
        atoms.append(Cell(field, "range", cursor, hi, closed, True))
    return [a for a in atoms if not Interval(a.lo, a.hi, a.lo_closed, a.hi_closed).empty()]


def _int_atoms(field: str, cuts: Sequence[float]) -> List[Cell]:
    lo, hi = (int(NUMERIC_DOMAINS[field][0]), int(NUMERIC_DOMAINS[field][1]))
    if not cuts:
        return [Cell(field, "ints", lo, hi, True, True)]
    atoms: List[Cell] = []
    cursor = lo
    for c in (int(x) for x in cuts):
        if cursor < c:
            atoms.append(Cell(field, "ints", cursor, c - 1, True, True))
        atoms.append(Cell(field, "ints", c, c, True, True))
        cursor = c + 1
    if cursor <= hi:
        atoms.append(Cell(field, "ints", cursor, hi, True, True))
    return atoms


def _merge_same(field: str, atoms: List[Cell], items) -> List[Cell]:
    """Merge adjacent atoms that every spec item treats the same way."""
    if not atoms:
        return atoms
    merged = [atoms[0]]
    sig = [_atom_matches(field, atoms[0].sample(), items)]
    for atom in atoms[1:]:
        s = _atom_matches(field, atom.sample(), items)
        prev = merged[-1]
        if s == sig[-1] and prev.kind == atom.kind and prev.kind in ("range", "ints"):
            # Adjacent by construction: prev ends where atom begins.
            merged[-1] = Cell(
                field, prev.kind, prev.lo, atom.hi, prev.lo_closed, atom.hi_closed)
        else:
            merged.append(atom)
            sig.append(s)
    return merged


def _axis(field: str, items, precursor: bool) -> List[Cell]:
    if field in BOOLEAN_FIELDS:
        if field == "is_home" and precursor:
            return [
                Cell(field, "presence", presence="home"),
                Cell(field, "presence", presence="depart"),
                Cell(field, "presence", presence="away"),
            ]
        return [Cell(field, "bool", bool_value=False), Cell(field, "bool", bool_value=True)]
    cuts = _cuts(field, items)
    atoms = _int_atoms(field, cuts) if field in INTEGER_FIELDS else _real_atoms(field, cuts)
    return _merge_same(field, atoms, items)


def _context_point(fields: Sequence[str], axes: Sequence[Sequence[Cell]], ctx: Tuple[int, ...]) -> Dict[str, Any]:
    point: Dict[str, Any] = {}
    for i, field in enumerate(fields):
        cell = axes[i][ctx[i]]
        if cell.kind == "presence":
            if cell.presence == "home":
                point["is_home"] = True
            elif cell.presence == "away":
                point["is_home"] = False
            # depart: leave is_home absent so an is_home condition does not match
            continue
        point[field] = cell.sample()
    return point


def _is_bad(device_modes: Tuple[str, ...], devices: Sequence[str],
            point: Mapping[str, Any], items_by_device) -> bool:
    for i, dev in enumerate(devices):
        if device_modes[i] != "on":
            continue
        for _, conds in items_by_device.get(dev, ()):
            if _all_conditions_match(conds, point):
                return True
    return False


def build_plant(device_types: Sequence[str],
                domains: Optional[Mapping[str, Any]] = None,
                discretisation: Optional[Mapping[str, Sequence[float]]] = None,
                rules: Sequence[Mapping[str, Any]] = (),
                invariants: Sequence[Any] = (),
                manual_on: bool = False,
                precursor: bool = False,
                initial_modes: Optional[Mapping[str, str]] = None) -> Plant:
    """Plant whose state is device mode × the spec's own context cells.

    ``domains`` defaults to ``rule_set_checker.NUMERIC_DOMAINS`` and
    ``BOOLEAN_FIELDS`` (the argument is accepted so callers can pass them
    explicitly; unknown fields are ignored because the evaluator never
    matches them). ``discretisation`` adds breakpoints on top of the
    thresholds that appear in ``rules`` and ``invariants``.

    Only fields a block or invariant of these devices actually constrains
    become axes. An unconstrained field cannot move the state into or out of
    the legal language, so the product over it is bisimilar to omitting it.
    """
    del domains  # the checker domains are the single source of truth
    devices = tuple(device_types)
    items_by_device = {d: _spec_items(rules, invariants, d) for d in devices}
    # Breakpoints supplied by the caller are treated as extra == cuts so the
    # axis splits there even when no rule mentions the value.
    extra_rules: List[Dict[str, Any]] = []
    if discretisation:
        for fld, cuts in discretisation.items():
            if fld not in NUMERIC_DOMAINS:
                continue
            for c in cuts:
                extra_rules.append({
                    "rule_id": f"cut:{fld}",
                    "scope": {"device_type": devices[0] if devices else ""},
                    "conditions": [{"field": fld, "operator": "==", "value": c}],
                    "action": {"type": "block"},
                })
    # Caller breakpoints split an axis without becoming forbidden states.
    cut_items = _spec_items(extra_rules, (), devices[0]) if extra_rules and devices else []
    all_items = [it for d in devices for it in items_by_device[d]] + cut_items
    fields = _fields_of(all_items)
    axes = tuple(_axis(f, all_items, precursor and f == "is_home") for f in fields)

    # Context index space.
    if axes:
        ranges = [range(len(ax)) for ax in axes]
    else:
        ranges = []

    def iter_ctx():
        if not ranges:
            yield ()
            return
        idx = [0] * len(ranges)
        while True:
            yield tuple(idx)
            for i in range(len(idx) - 1, -1, -1):
                idx[i] += 1
                if idx[i] < len(ranges[i]):
                    break
                idx[i] = 0
            else:
                return

    modes_space = []
    for _ in devices:
        modes_space.append(("off", "on"))

    def iter_modes():
        if not devices:
            yield ()
            return
        idx = [0] * len(devices)
        while True:
            yield tuple(modes_space[i][idx[i]] for i in range(len(devices)))
            for i in range(len(idx) - 1, -1, -1):
                idx[i] += 1
                if idx[i] < 2:
                    break
                idx[i] = 0
            else:
                return

    states = []
    bad = set()
    for modes in iter_modes():
        for ctx in iter_ctx():
            state = (modes, ctx)
            states.append(state)
            point = _context_point(fields, axes, ctx)
            if _is_bad(modes, devices, point, items_by_device):
                bad.add(state)

    events: Dict[str, Event] = {}
    delta: Dict[Tuple[State, str], State] = {}

    def add(name: str, src: State, tgt: State) -> None:
        if name not in events:
            events[name] = event_class(name)
        delta[(src, name)] = tgt

    by_key = {(m, c): (m, c) for m, c in states}
    for modes, ctx in states:
        # Device commands.
        for i, dev in enumerate(devices):
            flipped = list(modes)
            if modes[i] == "off":
                flipped[i] = "on"
                add(f"{dev}.turn_on", (modes, ctx), (tuple(flipped), ctx))
                if manual_on:
                    add(f"{dev}.manual_on", (modes, ctx), (tuple(flipped), ctx))
            else:
                flipped[i] = "off"
                add(f"{dev}.turn_off", (modes, ctx), (tuple(flipped), ctx))
            add(f"{dev}.get_status", (modes, ctx), (modes, ctx))
        # Context adjacency.
        for i, field in enumerate(fields):
            axis = axes[i]
            here = ctx[i]
            cell = axis[here]

            def jump(new_index: int, name: str, _ctx=ctx, _modes=modes) -> None:
                nxt = list(_ctx)
                nxt[i] = new_index
                tgt = (_modes, tuple(nxt))
                if tgt in by_key:
                    add(name, (_modes, _ctx), tgt)

            if cell.kind == "presence":
                index = {c.presence: n for n, c in enumerate(axis)}
                if cell.presence == "home" and "depart" in index:
                    jump(index["depart"], "depart")
                elif cell.presence == "depart" and "away" in index:
                    jump(index["away"], "leave")
                elif cell.presence == "away" and "home" in index:
                    jump(index["home"], "arrive")
            elif cell.kind == "bool":
                other = 1 - here
                if field == "is_home":
                    jump(other, "leave" if cell.bool_value else "arrive")
                else:
                    jump(other, f"{field}_{'on' if not cell.bool_value else 'off'}")
            else:
                if here + 1 < len(axis):
                    jump(here + 1, f"{field}_up")
                if here - 1 >= 0:
                    jump(here - 1, f"{field}_down")

    # Initial: every device off (unless overridden), context cell holding the
    # typical value. Home, not the depart slice.
    init_modes = []
    for dev in devices:
        mode = (initial_modes or {}).get(dev, "off")
        init_modes.append(mode if mode in ("off", "on") else "off")
    init_ctx = []
    for i, field in enumerate(fields):
        init_ctx.append(_initial_index(field, axes[i]))
    initial = (tuple(init_modes), tuple(init_ctx))
    if initial not in by_key:
        initial = states[0]

    return Plant(
        states=frozenset(states),
        initial=initial,
        bad=frozenset(bad),
        events=events,
        delta=delta,
        device_types=devices,
        fields=tuple(fields),
        axes=axes,
        spec_ids={d: tuple(i for i, _ in items_by_device[d]) for d in devices},
    )


def _initial_index(field: str, axis: Sequence[Cell]) -> int:
    if not axis:
        return 0
    if axis[0].kind == "presence":
        for i, c in enumerate(axis):
            if c.presence == "home":
                return i
        return 0
    if axis[0].kind == "bool":
        prefer = True if field == "is_home" else False
        for i, c in enumerate(axis):
            if c.bool_value is prefer:
                return i
        return 0
    typical = _TYPICAL.get(field, NUMERIC_DOMAINS.get(field, (0.0, 0.0))[0])
    for i, c in enumerate(axis):
        if c.kind == "ints":
            if int(c.lo) <= int(typical) <= int(c.hi):
                return i
        else:
            iv = Interval(c.lo, c.hi, c.lo_closed, c.hi_closed)
            # typical is inside the cell, allowing the closed endpoints
            if (c.lo < typical < c.hi or (typical == c.lo and c.lo_closed)
                    or (typical == c.hi and c.hi_closed)):
                return i
            if not iv.empty() and c.lo == c.hi == typical:
                return i
    return 0


def state_dict(plant: Plant, state: State) -> Dict[str, Any]:
    """JSON-ready view of a state. Abstract plants (int states) pass through."""
    if not plant.device_types and not plant.fields:
        return {"state": state if not isinstance(state, tuple) else list(state)}
    modes, ctx = state
    out: Dict[str, Any] = {}
    for dev, mode in zip(plant.device_types, modes):
        out[dev] = mode
    for i, field in enumerate(plant.fields):
        out[field] = plant.axes[i][ctx[i]].label()
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis — safety fixpoint (Algorithm 1, prefix-closed case)
# ─────────────────────────────────────────────────────────────────────────────

def _synthesize(plant: Plant, bad: Iterable[State], use_forcing: bool) -> Synthesis:
    """Largest subset of the legal states that is safe under the mechanism.

    Legal states start as ``plant.states - bad``. A state is removed when
    some uncontrollable event leaves the set and, if ``use_forcing``, no
    forcible event stays inside the set. Forcing states are those kept only
    by that preemption. The loop is the safety special case of Reniers and
    Cai, Algorithm 1 (lines 19-20, 26): forbidden states have already been
    removed, and every remaining state is marked, so the nonblocking
    iteration adds nothing.
    """
    bad_set = set(bad)
    Q = set(plant.states) - bad_set
    removed_because: Dict[State, Tuple[str, State]] = {}
    while True:
        forced: Dict[State, Tuple[str, str]] = {}
        remove = []
        for q in Q:
            escapes = []
            for name, ev, tgt in plant.successors(q):
                if ev.controllable:
                    continue
                if tgt not in Q:
                    escapes.append((name, tgt))
            if not escapes:
                continue
            safe_force = []
            if use_forcing:
                for name, ev, tgt in plant.successors(q):
                    if ev.forcible and tgt in Q:
                        safe_force.append(name)
            if safe_force:
                forced[q] = (escapes[0][0], safe_force[0])
            else:
                remove.append(q)
                removed_because[q] = escapes[0]
        if not remove:
            break
        for q in remove:
            Q.discard(q)
    enabled, disabled = _control(plant, Q, set(forced))
    reachable, safe = _reach(plant, plant.initial, enabled, bad_set)
    legal = set(plant.states) - set(plant.bad)
    # ``retains_legal`` is about the original specification, not an expanded bad set.
    return Synthesis(
        good=frozenset(Q),
        forcing=frozenset(forced),
        enabled=enabled,
        disabled=disabled,
        reachable=reachable,
        retains_legal=legal <= Q,
        retains_initial=plant.initial in Q,
        closed_loop_safe=safe and (plant.initial in Q),
        removed_because={s: removed_because[s] for s in removed_because if s not in Q},
        forced_because=forced,
    )


def _control(plant: Plant, good: set, forcing: set) -> Tuple[frozenset, frozenset]:
    """Enabled and disabled events of the supervisor in Equation (2).

    Forcing is used only where controllability fails (some uncontrollable
    event leaves ``good``) and a forcible event stays in ``good``. At those
    states the enabled set is exactly the forcible events that stay in
    ``good`` — a bad forcible event is not taken. Elsewhere every event that
    stays in ``good`` is enabled, which disables controllable exits and keeps
    uncontrollable events (they stay, or the state would have been removed).
    """
    enabled = set()
    disabled = set()
    for q in good:
        escapes = False
        for name, ev, tgt in plant.successors(q):
            if not ev.controllable and tgt not in good:
                escapes = True
                break
        if q in forcing and escapes:
            for name, ev, tgt in plant.successors(q):
                if ev.forcible and tgt in good:
                    enabled.add((q, name))
                elif ev.controllable:
                    disabled.add((q, name))
            continue
        for name, ev, tgt in plant.successors(q):
            if tgt in good:
                enabled.add((q, name))
            elif ev.controllable:
                disabled.add((q, name))
    return frozenset(enabled), frozenset(disabled)


def _reach(plant: Plant, initial: State, enabled: frozenset,
           bad: set) -> Tuple[frozenset, bool]:
    """States reachable under ``enabled``. Safe when none of them is bad."""
    if initial not in {s for s, _ in enabled} and initial not in plant.states:
        return frozenset(), False
    seen = set()
    if initial in plant.states:
        seen.add(initial)
    stack = [initial] if initial in seen else []
    safe = initial not in bad
    while stack:
        s = stack.pop()
        for name, _ev, tgt in plant.successors(s):
            if (s, name) not in enabled:
                continue
            if tgt in bad:
                safe = False
            if tgt not in seen:
                seen.add(tgt)
                stack.append(tgt)
    return frozenset(seen), safe


def supcon(plant: Plant, bad: Optional[Iterable[State]] = None) -> Synthesis:
    """Supremal controllable sublanguage for a prefix-closed safety spec.

    ``bad`` defaults to ``plant.bad``. Passing a larger set re-solves a
    tighter spec (used for monotonicity and idempotence). Forcing flags are
    ignored, which is Reniers and Cai, Remark 2: Algorithm 1 with an empty
    forcible set is ordinary supervisory control.
    """
    return _synthesize(plant, set(plant.bad if bad is None else bad), use_forcing=False)


def supcon_forcing(plant: Plant, bad: Optional[Iterable[State]] = None) -> Synthesis:
    """Supremal forcibly-controllable sublanguage, safety case of Algorithm 1."""
    return _synthesize(plant, set(plant.bad if bad is None else bad), use_forcing=True)


def reaches_bad(plant: Plant, synthesis: Synthesis) -> bool:
    """True when this supervisor does not keep the closed loop out of the forbidden states.

    A synthesis that drops the initial state does not enforce the spec from
    the state the plant actually starts in.
    """
    if plant.initial not in synthesis.good:
        return True
    return not synthesis.closed_loop_safe


def replay(plant: Plant, trace: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Walk a witness trace on the plant. Returns the final state label.

    The first step may omit the event (it is the initial state). Every later
    step's event must be enabled in the plant at the state reached so far.
    """
    if not trace:
        raise ValueError("empty trace")
    state = plant.initial
    for i, step in enumerate(trace):
        event = step.get("event")
        if i == 0 and event is None:
            continue
        if event is None:
            raise ValueError(f"step {i} has no event")
        nxt = plant.delta.get((state, event))
        if nxt is None:
            raise ValueError(f"event {event!r} is not enabled at {state_dict(plant, state)}")
        state = nxt
    return state_dict(plant, state)


# ─────────────────────────────────────────────────────────────────────────────
# Enforceability
# ─────────────────────────────────────────────────────────────────────────────

def _trace_from_path(plant: Plant, path: List[Tuple[Optional[str], State]]) -> List[Dict[str, Any]]:
    out = []
    for event, state in path:
        out.append({
            "event": event,
            "state": state_dict(plant, state),
            "bad": state in plant.bad,
        })
    return out


def _bfs(plant: Plant, start: State, goals: set,
         predicate=None) -> Optional[List[Tuple[Optional[str], State]]]:
    """Shortest path from ``start`` to ``goals`` as (event, resulting state).

    The first pair has event None and is ``start``. ``predicate(state, event, eventobj)``
    filters edges. ``start`` itself counts as a hit, with a one-element path.
    """
    if start in goals:
        return [(None, start)]
    prev: Dict[State, Tuple[State, str]] = {}
    seen = {start}
    q: deque = deque([start])
    hit: Optional[State] = None
    while q and hit is None:
        s = q.popleft()
        for name, ev, tgt in plant.successors(s):
            if tgt in seen:
                continue
            if predicate is not None and not predicate(s, name, ev):
                continue
            prev[tgt] = (s, name)
            seen.add(tgt)
            if tgt in goals:
                hit = tgt
                break
            q.append(tgt)
    if hit is None:
        return None
    steps: List[Tuple[Optional[str], State]] = []
    cur = hit
    while cur != start:
        src, name = prev[cur]
        steps.append((name, cur))
        cur = src
    steps.reverse()
    return [(None, start)] + steps


def _path_to_lost(plant: Plant, lost: set) -> Optional[List[Tuple[Optional[str], State]]]:
    return _bfs(plant, plant.initial, lost)


def _follow_removals(plant: Plant, start: State,
                     because: Mapping[State, Tuple[str, State]]) -> List[Tuple[Optional[str], State]]:
    """From ``start`` (already on the trace) follow recorded uncontrollable escapes."""
    path: List[Tuple[Optional[str], State]] = []
    seen = set()
    s = start
    while s in because and s not in seen and len(seen) <= len(plant.states):
        seen.add(s)
        event, tgt = because[s]
        path.append((event, tgt))
        s = tgt
        if s in plant.bad:
            break
    return path


def _witness(plant: Plant, guard: Synthesis, forced: Synthesis,
             classification: str) -> List[Dict[str, Any]]:
    legal = set(plant.states) - set(plant.bad)
    if classification == GUARD_ENFORCEABLE:
        # A controllable step the supervisor refuses because the target is bad,
        # reached from the initial state by events the supervisor allows.
        exits: Dict[State, Tuple[str, State]] = {}
        for src, name in guard.disabled:
            tgt = plant.delta.get((src, name))
            if tgt in plant.bad:
                exits.setdefault(src, (name, tgt))
        path = _bfs(plant, plant.initial, set(exits),
                    predicate=lambda s, name, _ev: (s, name) in guard.enabled) if exits else None
        if path:
            src = path[-1][1]
            name, tgt = exits[src]
            return _trace_from_path(plant, list(path) + [(name, tgt)])
        return _trace_from_path(plant, [(None, plant.initial)])

    if classification == NEEDS_OBLIGATION:
        lost = legal - set(guard.good)
        path = _path_to_lost(plant, lost) or [(None, plant.initial)]
        # Extend by the guard's removal chain so the trace ends in a bad state.
        tail_from = path[-1][1]
        tail = _follow_removals(plant, tail_from, guard.removed_because)
        return _trace_from_path(plant, list(path) + tail)

    # UNENFORCEABLE: a path the forcing supervisor also cannot cut.
    # Prefer an all-uncontrollable path from the initial state into the bad set.
    unc = _bfs(plant, plant.initial, set(plant.bad),
               predicate=lambda _s, _n, ev: not ev.controllable)
    if unc and unc[-1][1] in plant.bad:
        return _trace_from_path(plant, unc)
    lost = legal - set(forced.good)
    path = _path_to_lost(plant, lost) or _bfs(plant, plant.initial, set(plant.bad)) or [
        (None, plant.initial)]
    tail = _follow_removals(plant, path[-1][1], forced.removed_because)
    full = list(path) + tail
    if full[-1][1] not in plant.bad:
        extra = _bfs(plant, full[-1][1], set(plant.bad))
        if extra and len(extra) > 1:
            full = full + extra[1:]
    return _trace_from_path(plant, full)


def _supervisor_view(plant: Plant, syn: Synthesis) -> Dict[str, Any]:
    disabled = [
        {"state": state_dict(plant, s), "event": e}
        for (s, e) in sorted(syn.disabled, key=lambda p: (p[1], str(state_dict(plant, p[0]))))
        if s in syn.reachable
    ]
    forcing = []
    for s in sorted(syn.forcing, key=lambda st: str(state_dict(plant, st))):
        if s not in syn.reachable and s not in syn.good:
            continue
        events = sorted(e for (st, e) in syn.enabled if st == s)
        preempts, uses = syn.forced_because.get(s, (None, None))
        forcing.append({
            "state": state_dict(plant, s),
            "events": events,
            "preempts": preempts,
            "forces": uses,
        })
    return {"disabled": disabled, "forcing": forcing,
            "reachable": [state_dict(plant, s) for s in sorted(syn.reachable, key=lambda st: str(state_dict(plant, st)))]}


def _classify(guard: Synthesis, forced: Synthesis) -> str:
    if guard.retains_legal and guard.closed_loop_safe:
        return GUARD_ENFORCEABLE
    if forced.retains_legal and forced.closed_loop_safe:
        return NEEDS_OBLIGATION
    return UNENFORCEABLE


def _message(classification: str, device: str, plant: Plant,
             guard: Synthesis, forced: Synthesis) -> str:
    if classification == GUARD_ENFORCEABLE:
        return (f"{device}: GUARD_ENFORCEABLE. The legal language is controllable, so a "
                f"BLOCK-only supervisor realizes it by refusing controllable commands that "
                f"leave the legal states. The closed loop does not reach a forbidden state.")
    if classification == NEEDS_OBLIGATION:
        at_home = any(
            plant.device_types and state_dict(plant, s).get(device) == "on"
            and _presence_home(state_dict(plant, s))
            for s in forced.forcing
        )
        if "depart" in plant.events:
            where = "at depart, so the device can be on at home and is forced off on the precursor"
        elif at_home:
            where = ("as soon as the device is switched on at home (there is no precursor "
                     "event to force against, so the obligation fires immediately)")
        else:
            where = "at each legal state that has an uncontrollable exit into the forbidden set"
        return (f"{device}: NEEDS_OBLIGATION. The legal language is not controllable: an "
                f"uncontrollable event leaves a state the rule still allows, so the supremal "
                f"BLOCK-only supervisor refuses the command in that legal state as well. "
                f"It is forcibly controllable: forcing turn_off {where} keeps every legal "
                f"state, and the forbidden state is unreachable in that closed loop.")
    return (f"{device}: UNENFORCEABLE. The legal language is not forcibly controllable. "
            f"Some legal state can reach a forbidden state by an event the guard cannot "
            f"refuse and that no forcible turn_off preempts (a physical manual_on into the "
            f"forbidden context is the usual reason). The witness trace is that path.")


def _presence_home(state: Mapping[str, Any]) -> bool:
    home = state.get("is_home")
    return home is True or home == "home"


def enforceability(rule_set: Sequence[Mapping[str, Any]],
                   invariants: Sequence[Any] = (),
                   manual_on: bool = False,
                   precursor: bool = False,
                   device_types: Optional[Sequence[str]] = None,
                   discretisation: Optional[Mapping[str, Sequence[float]]] = None,
                   ) -> List[EnforceabilityResult]:
    """Classify each device's safety language.

    A device is included when it has a satisfiable block in ``rule_set`` or
    an invariant. Allow and modify rules do not define a forbidden state.
    Returns one result per such device. Assumptions (``manual_on``,
    ``precursor``) are part of the result: they are not DSL fields.
    """
    devices: List[str] = []
    if device_types:
        devices = list(device_types)
    else:
        for rule in rule_set:
            dev = (rule.get("scope") or {}).get("device_type") or rule.get("device_type")
            if dev and dev not in devices:
                devices.append(str(dev))
        for inv in invariants:
            dev = inv.device_type if hasattr(inv, "device_type") else inv.get("device_type")
            if dev and dev not in devices:
                devices.append(str(dev))
    results = []
    for dev in devices:
        items = _spec_items(rule_set, invariants, dev)
        if not items:
            continue
        plant = build_plant((dev,), rules=rule_set, invariants=invariants,
                            discretisation=discretisation, manual_on=manual_on,
                            precursor=precursor)
        guard = supcon(plant)
        forced = supcon_forcing(plant)
        classification = _classify(guard, forced)
        # The supervisor we would actually ship: the guard when it realizes
        # the legal language, otherwise the forcing supervisor when that does,
        # otherwise the forcing attempt (its removed states explain the witness).
        chosen = guard if classification == GUARD_ENFORCEABLE else forced
        trace = _witness(plant, guard, forced, classification)
        assumptions = {
            "manual_on": manual_on,
            "precursor": precursor,
            "device_types": [dev],
            "controllable": sorted(n for n, e in plant.events.items() if e.controllable),
            "uncontrollable": sorted(n for n, e in plant.events.items() if not e.controllable),
            "forcible": sorted(n for n, e in plant.events.items() if e.forcible),
        }
        results.append(EnforceabilityResult(
            classification=classification,
            device_type=dev,
            rule_ids=list(plant.spec_ids.get(dev, ())),
            witness_trace=trace,
            minimal_supervisor=_supervisor_view(plant, chosen),
            message=_message(classification, dev, plant, guard, forced),
            assumptions=assumptions,
            guard=guard,
            forcing=forced,
            blocking_supervisor_exists=bool(guard.retains_initial and guard.closed_loop_safe),
        ))
    return results


def benchmark_enforceability(policies: Sequence[Mapping[str, Any]],
                             *,
                             manual_on: bool = False,
                             precursor: bool = False) -> Dict[str, Any]:
    """Share of compiled policies in each enforceability class.

    Policies that are refusals, or that are allow/modify rules with no
    forbidden state, are counted apart from the three classes. Shares of the
    safety specs are the headline numbers. This function does not fetch or
    read the AutoTap workbook and does not call a model.
    """
    counts = {GUARD_ENFORCEABLE: 0, NEEDS_OBLIGATION: 0, UNENFORCEABLE: 0,
              "NO_SAFETY_SPEC": 0, "NOT_COMPILED": 0}
    blocking = 0
    safety = 0
    for policy in policies:
        if not isinstance(policy, Mapping) or policy.get("rejected") is True:
            counts["NOT_COMPILED"] += 1
            continue
        if "scope" not in policy or "action" not in policy:
            counts["NOT_COMPILED"] += 1
            continue
        results = enforceability([policy], manual_on=manual_on, precursor=precursor)
        if not results:
            counts["NO_SAFETY_SPEC"] += 1
            continue
        # One compiled policy has one device.
        cls = results[0].classification
        counts[cls] += 1
        safety += 1
        if results[0].blocking_supervisor_exists:
            blocking += 1

    def share(n: int, d: int) -> Optional[float]:
        return round(n / d, 4) if d else None

    return {
        "counts": counts,
        "safetySpecs": safety,
        "sharesOfSafetySpecs": {
            GUARD_ENFORCEABLE: share(counts[GUARD_ENFORCEABLE], safety),
            NEEDS_OBLIGATION: share(counts[NEEDS_OBLIGATION], safety),
            UNENFORCEABLE: share(counts[UNENFORCEABLE], safety),
        },
        "blockingSupervisorAmongSafetySpecs": share(blocking, safety),
        "assumptions": {"manual_on": manual_on, "precursor": precursor},
        "corpus": "caller-supplied policies",
        "note": ("Shares are computed from the policies passed in. They are not "
                 "AutoTap Study 1 results unless those compiled policies were supplied."),
    }
