# Controllability of DeviceWeave safety rules

The policy evaluator is a command guard. It may refuse a controllable command
in the current context, and it never fires an actuator by itself. Supervisory
control theory says when that guard is enough for a safety rule, when a forced
action is required, and when neither exists.

The check lives in `src/policy_authoring/controllability.py`. Authoring reports
it from `rule_set_checker.check_new_rule` as an `enforceability` finding. It does not change
`evaluator.py`, the DSL, or BLOCK > MODIFY > ALLOW.

## What a rule is, as a language

One device is modelled at a time. A state is the device mode (`off` or `on`)
together with a context cell. The plant's context axes are the numeric and
boolean fields that the device's block rules or invariants actually constrain,
taken from `rule_set_checker.NUMERIC_DOMAINS` and cut at the rules' own
thresholds. Adjacent atoms on which every rule agrees are merged, so a
threshold that does not split the domain does not add states.

Events, classified by `classify_events` against `evaluator._SAFE_ACTIONS`:

| Event | Controllable | Forcible | Why |
|---|---|---|---|
| `turn_on`, `set_*`, `modify` | yes | no | The guard may refuse them. |
| `turn_off` | no | yes | In `_SAFE_ACTIONS`, so the guard cannot refuse it. An obligation may force it. |
| `get_status` | no | no | In `_SAFE_ACTIONS`. Modelled as a self-loop. |
| `leave`, `arrive`, weather steps | no | no | The world changes whether or not a rule matches. |
| `manual_on` | no | no | Optional. A physical switch that turns the device on in every context. Off by default. |
| `depart` | no | no | Optional precursor (geofence or door) inserted between home and away. Off by default. |

A state is forbidden when the device is on and some block or invariant matches
the cell's sample point. The legal language is every other state. The initial
state is devices off, at home, in the numeric cell that contains the checker's
typical point.

`depart` is not a context the DSL can name. Its sample point omits `is_home`,
which matches the evaluator: a missing field does not satisfy an `is_home`
condition.

## The three classes

`enforceability(rules, invariants)` returns one result per device that has a
block or an invariant.

| Class | Condition | What the witness is |
|---|---|---|
| `GUARD_ENFORCEABLE` | `supcon` keeps every legal state, and the closed loop is safe. | A path from the initial state to a controllable command the guard must refuse, then that command, which enters a forbidden state. |
| `NEEDS_OBLIGATION` | `supcon` drops some legal state, but `supcon_forcing` keeps every legal state and is safe. | A path to a legal state the guard-only supervisor loses, then the uncontrollable events that walk it into a forbidden state. |
| `UNENFORCEABLE` | `supcon_forcing` does not keep every legal state. | Prefer an all-uncontrollable path from the initial state into the forbidden set. |

`NEEDS_OBLIGATION` does not mean no blocking supervisor exists. The supremal
controllable sublanguage may still contain the initial state: the guard can
stay safe by also refusing `turn_on` in legal contexts. That weaker supervisor
is reported (`blocking_supervisor_exists`, and `minimal_supervisor` on the
witness). It is not the legal language, so the rule is not guard-enforceable.

Severity on the finding is `info` for `GUARD_ENFORCEABLE` and `not_computed`,
and `warning` for the other two. It is never `error`, so authoring still stores
the rule. The runtime does not install the synthesized supervisor.

`analyze` does not build a plant. `check_new_rule` does, and only when the
rule being authored is a satisfiable block, and only for that device. Allow
and modify rules, and invariants of other devices, do not pay for it. The
request path refuses a plant of more than `REQUEST_MAX_STATES` (1400) states
and returns classification `not_computed` with an empty trace. That cap is
what decides the class: the same rule set is classified or `not_computed` on
every host. `PolicyAuthoringFunction` sets no memory size, so it inherits the
global 256 MB, about 1/7 of a vCPU. A plant under 1400 states is around 1–2 s
at that share of a CPU, well under API Gateway's 30 s limit.
`REQUEST_TIME_BUDGET_S` (2.5 s) is only a backstop for a runaway fixpoint.
It sits above that 256 MB estimate, so it is not what chooses between a class
and `not_computed`.

With blocks that each constrain four numeric fields, the cap is reached at
about five rules on a device. Four such blocks are about 1250 states and
still classify; five are about 2600 and do not. For a realistic rule set,
authoring will usually report `not_computed`, and the classification comes
from the offline `compile_fidelity.py enforceability` command (no state cap
unless `--max-states` is passed) or from `enforceability()` with no
`max_states`.

The authoring witness has three fields: `classification`, `counts`, and
`trace` (one path). `witness(full=True)` and
`compile_fidelity.py enforceability --detail full` add the reachable states
and the refused events. That listing is not in the authoring response.

An unsatisfiable block (`rule_region` empty, for example `humidity > 100`) is
not a safety spec. It is omitted. A device left with no satisfiable block or
invariant is `NO_SAFETY_SPEC` in the benchmark, not `GUARD_ENFORCEABLE`.

## The heater, under the four assumptions the tests fix

Rule: block the heater when `is_home == false` ("never on while away").

1. **Physical `manual_on`, no precursor.** `UNENFORCEABLE`. From off-at-home the
   uncontrollable switch turns the heater on, and `leave` then reaches
   on-while-away. Off-while-away has no forcible `turn_off` that stays legal,
   because the device is already off. `supcon` and `supcon_forcing` both drop
   the initial state. Enumerating every BLOCK supervisor finds none that is safe.
2. **No physical switch, no precursor.** `NEEDS_OBLIGATION`. On-at-home is legal,
   but `leave` is uncontrollable, so the supremal controllable sublanguage keeps
   only the off states. The guard must disable `heater.turn_on` at home. With
   forcing, `turn_on` at home stays enabled and the only enabled event from
   on-at-home is `heater.turn_off`. The closed loop never reaches on-while-away.
3. **No physical switch, with `depart`.** Still `NEEDS_OBLIGATION`, but the
   forcing state moves. On-at-home is kept and is not forcing. On-at-depart is
   forcing, and the only enabled event there is `heater.turn_off`. The heater
   can stay on at home and is forced off when the precursor fires.
4. **Blocks for both `is_home` true and false.** `GUARD_ENFORCEABLE`. Every on
   state is forbidden, and refusing `turn_on` realizes that language.

## The conditions, as checked against the papers

Fetched and read for this note: the ar5iv HTML of arXiv:2404.08469 (Michel A.
Reniers, Eindhoven; Kai Cai, Osaka Metropolitan University), the Crossref
records for the two 1987 SIAM papers (abstracts only; the SIAM HTML is behind a
bot check), and the libFAUDES controllability page. The Proc. IEEE 1989 PDF was
not retrieved (IEEE Xplore bot check; Crossref has title, pages, and DOI, and
no abstract).

**Classical controllability.** Ramadge and Wonham, SIAM J. Control Optim.
25(1):206–230, 1987, doi:10.1137/0325013. The Crossref abstract says: "The
existence problem for a supervisor is reduced to finding the largest
controllable language contained in a given legal language." The formula itself
is not in that abstract.

Reniers and Cai, Definition 2, citing that paper as [16], state it for
`F ⊆ L_m(P)` as:

```
(∀ s ∈ F̄, ∀ u ∈ Σ_u) [ u ∈ E_P(s) ⇒ s u ∈ F̄ ]
```

libFAUDES (https://www.fgdes.tf.fau.de/faudes/reference/synthesis_controllability.html),
attributing the notion to Ramadge and Wonham, writes:

```
Closure(K) Σ_uc ∩ Closure(L) ⊆ Closure(K)
```

**TO VERIFY against the original:** the 1987 SIAM typesetting of the
controllability formula. The two forms above were checked; the SIAM line was not.

**Supremal controllable sublanguage.** Wonham and Ramadge, SIAM J. Control
Optim. 25(3):637–659, 1987, doi:10.1137/0325036. The Crossref abstract says the
supremal controllable sublanguage `S` of a given language `L` "is characterized
as the largest fixpoint of a monotone operator Ω", and for regular languages
"the fixpoint S can be computed as the limit of the (finite) sequence {K_j}
given by K_{j+1} = Ω(K_j), K_0 = L."

**Forcible-controllability.** Reniers and Cai, arXiv:2404.08469, Definition 3,
from the ar5iv HTML:

```
(∀ s ∈ F̄) [
    (∀ u ∈ Σ_u) [ u ∈ E_P(s) ⇒ s u ∈ F̄ ]
    ∨ ( (∃ f ∈ Σ_f) [ s f ∈ F̄ ] ∧ (∀ σ ∈ Σ \ Σ_f) [ s σ ∉ F̄ ] )
]
```

In their words: a sublanguage is forcibly-controllable if either it is
controllable or there exists a forcible event that keeps the prefix closure
invariant by preempting all non-forcible events.

Theorem 1, same HTML: for a plant `P` and a nonempty specification
`F ⊆ L_m(P)`, there exists a supervisory control `V` such that `V/P` is
nonblocking and `L_m(V/P) = F` if and only if `F` is forcibly-controllable.

Proposition 1: the family of forcibly-controllable sublanguages of `F` is
closed under arbitrary unions. The unique supremal element is their union.

Theorem 2, as rendered on ar5iv: if `F_sup` is nonempty then there exists
`V_sup` such that `V_sup/P` is nonblocking and `L_m(V_sup/P) = F`. The proof
says the conclusion follows from Theorem 1 applied to `F_sup`, and the next
paragraph says the realized language is `F_sup`. **TO VERIFY against the
original:** whether the subscript on `F` was dropped in that HTML rendering.
This module treats the realized language as `F_sup`.

Equation (2) is the control law in the proof of Theorem 1. Forcing is used
only when some uncontrollable event leaves the prefix closure and some forcible
event stays inside it. At those strings the enabled set is the forcible events
that stay inside. Elsewhere the enabled set is every uncontrollable event plus
the controllable events that stay inside. A forcible event whose target is
outside is not taken.

Remark 2: Algorithm 1 with an empty forcible set computes the ordinary
maximally permissive controllable nonblocking supervisor. `supcon` is that
call.

Theorem 5: Algorithm 1 is `O(|Q|^2 |Σ|)`. The proof says the classical part of
the algorithm has that bound in [21], which is Ramadge and Wonham, Proc. IEEE
77(1):81–98, 1989, doi:10.1109/5.21072. **TO VERIFY against the original:** the
1989 wording of that complexity claim. The citation and the Theorem 5 bound
were checked; the 1989 paper was not.

`supcon` and `supcon_forcing` implement the prefix-closed safety case of
Algorithm 1. Forbidden states are removed first. Every surviving state is
treated as marked, so the nonblocking iteration (lines 8–14) is vacuous: a
marked state, including a deadlock, is nonblocking. A state is removed when an
uncontrollable event leaves the remaining set and no forcible event stays
inside; it is forcing when an uncontrollable event leaves and some forcible
event stays (lines 19–20 and 26). The implementation repeats the removal until
nothing more is removed, which is the same safety fixpoint as the inner
bad-state iteration. It does not synthesize marked-language nonblocking beyond
safety.

## What is modelled, and what is not

Modelled: untimed prefix-closed safety; one device; on/off (a `modify` or
`set_*` is a controllable command and does not by itself change the on/off
safety state); context changes of one adjacent step, in both directions (time
does not only advance); full observation of the state.

Not modelled: partial observation; time bounds ("for more than an hour");
levels of a dimmer; joint multi-device specifications; a scheduler that
actually forces `turn_off`. `manual_on` and `depart` are assumptions passed to
`build_plant` / `enforceability`, not DSL fields. The synthesized supervisor
is a finding, not a runtime component.

## How this differs from the interval checker

`invariant_violated` means the written blocks do not cover a context, so
`compute_verdict` allows `turn_on` there. If the blocks do cover the region,
controllability can still be `NEEDS_OBLIGATION`, because `leave` is
uncontrollable. Static coverage is not controllability.

## Benchmark

`scripts/compile_fidelity.py enforceability` classifies compiled policies you
supply (`--policies` JSON, or `--responses` JSONL of `{id, compiled}`). It
does not open the Study 1 workbook and does not call a model. `--live` on
`run` is the opt-in path that calls the compiler; enforceability has no live
mode. `--detail full` adds each policy's supervisor listing. `--max-states`
is an optional cap; the default is no cap. CI (`.github/workflows/deploy.yml`)
does not run pytest.

Shares are of the safety specs in that input (block rules, or policies that
produce a forbidden state). Allow and modify rules with no forbidden state are
`NO_SAFETY_SPEC`. Refusals and objects without `scope` and `action` are
`NOT_COMPILED`. The report's `corpus` is `caller-supplied policies`.

AutoTap Study 1 (Lefan Zhang, Weijia He, Jesse Martinez, Noah Brackenbury,
Shan Lu, Blase Ur, ICSE 2019, doi via https://ieeexplore.ieee.org/abstract/document/8811900)
is the corpus the harness can score when compiled policies are supplied. The
workbook is fetched at run time by `compile_fidelity.py fetch` into a
git-ignored cache. It is not committed: the AutoTap repository is GPL-3.0 and
no separate licence for the spreadsheet was found. No Study 1 statement is
paraphrased here.

The manual baseline in `docs/autotap-gap-analysis.md` is 690 statements in the
`Result` sheet (380 *always*, 310 *never*). That is a count of statement
shapes, not an enforceability share, and it was not recomputed by this check.

No Study 1 enforceability shares were computed. Compiled Study 1 policies were
not available offline, and no model was called. The only shares computed are
on synthetic policies in `tests/test_controllability.py`: one away-block
(`NEEDS_OBLIGATION`), one block whose threshold covers the whole temperature
domain (`GUARD_ENFORCEABLE`), one modify (`NO_SAFETY_SPEC`), one rejection
(`NOT_COMPILED`). With the default assumptions that is 2 safety specs, half
`NEEDS_OBLIGATION` and half `GUARD_ENFORCEABLE`, and zero `UNENFORCEABLE`.
The same away-block with `--manual-on` is `UNENFORCEABLE`. Those figures are
synthetic.

`scripts/policy_rules_smt.py` exists and imports Z3 for the interval checker.
`z3` is not installed and is not in `requirements-dev.txt` or
`src/requirements.txt`, so the controllability fixpoint was not cross-checked
with Z3.

## Tests

`tests/test_controllability.py` (pytest, no network). Hypothesis property tests
draw plants of 2–6 states: the closed loop of either supervisor never reaches a
forbidden state when the initial state is kept; `supcon` is idempotent and
monotone, and so is `supcon_forcing`; on plants small enough to enumerate, the
events the synthesized closed loop takes equal the union of the events taken
by every safe supervisor. The brute-force union is the right comparison.
Cardinality can tie between supervisors that enable different events, so "the
largest supervisor" is not a single set.

## Where the throwaway sketches disagreed with the papers

The sketches that motivated this module were not treated as the algorithm.

1. Their forcing closed loop followed every forcible event, including one that
   leaves the good set. Equation (2) enables only forcible events whose target
   stays in the prefix closure. `test_two_forcible_events_only_the_safe_one_is_taken`
   locks that in.
2. Their forcing fixpoint did not repeat the inner bad-state step of Algorithm 1
   (lines 19–22), which reclassifies a forcing state as bad once every forcible
   successor is bad. The outer loop of this module repeats until no state is
   removed.
3. Their maximal-permissiveness check picked one maximum-cardinality supervisor.
   Incomparable supervisors can have the same cardinality. The tests compare the
   union of closed-loop events over every safe supervisor.
4. Their safety routine returned no supervisor when the initial state was
   removed, and discarded the remaining good states. Here the good set is still
   returned; "does not enforce from the start" is `retains_initial == False`.
