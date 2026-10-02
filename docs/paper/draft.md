<!--
Workshop-length draft. Every number is a double-brace placeholder filled by

    python scripts/property_coding.py analyze GOLD.json --rows ROWS.jsonl \
        --agreement AGREEMENT.json --total 690
    python scripts/property_coding.py render benchmarks/autotap/_out/coding/results.json

Do not type a result into this file by hand. tests/test_property_coding.py fails
if a placeholder names a key the analysis does not produce. Items marked
[TO VERIFY] have not been checked against the original source; see
docs/paper/SUBMISSION_CHECKLIST.md.
-->

# Refusal Is Not Enforcement: How Much of What Smart-Home Users Ask For Can a Command Guard Guarantee?

## Abstract

LLM-based home assistants increasingly compile natural-language rules into
policies that a runtime *guard* checks before each command: it may refuse a
command, but it never acts by itself. We ask how much of what people actually
want from their homes such a guard can enforce. We formalise the
{{statementsTotal}} statements written by participants in AutoTap's first user
study (Zhang et al., ICSE 2019) into a DSL-independent property language based
on AutoTap's own templates, with two independent coders (Cohen's κ for the
property kind {{agreement.fields.kind.kappa|f2}}). For each property we
compute, by supervisory control with event forcing, whether five enforcement
architectures can guarantee it. Under our default assumptions, a refusal-only
guard of the kind deployed in DeviceWeave exactly enforces only
{{outcomes.reaction=on,manual=off.guard_dw.exact|pct}}
{{outcomes.reaction=on,manual=off.guard_dw.exact|ci}} of the
{{properties|int}} coded properties; it can stay safe for many others only by
refusing commands the user allowed, and cannot enforce
{{headline.guardDwImpossible|pct}} at all. Adding a forced turn-off raises exact
enforcement to {{headline.guardDwObligationExact|pct}}; trigger-action
automation reaches {{headline.tapExact|pct}}. In a case study, an LLM compiler
targeting a five-device guard DSL produced a validator-accepted policy for
{{compile.accepted|pct}} of statements, and for
{{compileCrossTab.acceptedNotEnforcedByGuard|pct}} of the accepted ones the
guard does not in fact enforce the property the user stated.

## 1 Introduction

<!-- Motivation: LLM agents and assistants gate tool calls with refusal-only
policies; TAP systems act; users state properties (AutoTap's finding). The gap
between "the compiler produced a policy" and "the property holds" is silent:
nothing fails, nothing logs. -->

Contributions:

1. A DSL-independent coding of {{statementsCoded}} user-written smart-home
   statements into AutoTap-style properties, with inter-coder reliability, released
   as position-keyed codes (the statements themselves are not redistributed).
2. An enforceability classification of every coded property under five
   enforcement architectures, computed with forcible supervisory control
   (Reniers and Cai) rather than argued case by case, with sensitivity to two
   modelling assumptions.
3. A measurement of the share of user-stated properties that a refusal-only
   command guard cannot exactly enforce, and a case study of the silent
   under-enforcement that follows when an LLM compiles them into such a guard.

## 2 Background

**Trigger-action programming and AutoTap.** AutoTap lets users state
properties of the form *this state should always/never be active (for more
than this long) (while that)*, *these states should always/never occur
together*, and *this event should always/never happen (while that / within
this long after that)*, translates them to LTL, and synthesises or repairs
trigger-action rules [Zhang et al. 2019].

**Supervisory control.** Ramadge and Wonham's supervisor may only *disable*
controllable events; the largest controllable sublanguage of a legal language
is the fixpoint of a monotone operator [Ramadge & Wonham 1987a; Wonham &
Ramadge 1987b]. Reniers and Cai add *forcible* events that a supervisor may
take to preempt uncontrollable ones, define forcible-controllability, and give
an algorithm for the maximally permissive supervisor [Reniers & Cai 2024,
arXiv:2404.08469]. A refusal-only guard is a supervisor with no forcible
events; an automation that acts is one with forcible actions.

## 3 Method

**Corpus.** AutoTap Study 1: {{statementsTotal}} statements, ten per
participant, each labelled by its author as something that should *always* or
*never* happen, with a flag for exceptions. The file is fetched at a pinned
commit and checked by SHA-256; it is not redistributed.

**Coding.** Two coders independently code every statement with a codebook
(`docs/paper/codebook.md`, v1.0, frozen at commit [COMMIT]) after a pilot of
[N] statements. Fields: scope (property or not), kind (state, state pair,
event), modality, the polarity of the named state, the target device class and
who can change it (system, human, world), the condition and who can change it,
and modifiers (duration, within-after, multiple conditions, an exception in the
text, whether the statement fits one AutoTap template exactly). Disagreements
are adjudicated by [ADJUDICATOR]. Agreement is reported per field and on the
derived enforceability outcome.

**From a coded property to a plant.** Each property becomes a small plant: a
boolean for the target being in its on-direction state, a boolean for the
condition, a 0–2 counter for durations, and a flag for a violation the system
may still repair. Who can change each boolean decides which events exist and
whether they are controllable (refusable) or forcible (the system may take
them). We compute the supremal forcibly-controllable sublanguage with the
safety case of Reniers and Cai's Algorithm 1 and classify each property as
*exact*, *preemptive* (exact only by acting in advance), *over-restrictive*
(safe only by refusing what the property allows), *impossible*, or
*unsatisfiable*.

**Architectures.** `guard_dw` refuses on-direction commands and never refuses
turn-off (as DeviceWeave does); `guard_any` may refuse any command;
`guard_dw_obligation` adds a forced turn-off; `tap` acts but never refuses;
`guard_tap` does both.

**Assumptions.** *Reaction*: after an uncontrollable change makes a property
false, the system has one step to repair it. *Manual*: a system device also has
a switch the system cannot refuse. We report all four combinations.

**Case study.** One LLM compiler (pinned model, temperature 0, one pass)
compiles every statement into DeviceWeave's five-device policy DSL; the
repository's own validator and rule-set checker decide acceptance.

## 4 Results

**What people ask for.** {{properties|pct}} {{properties|ci}} of statements
are properties. Of those, {{modality.always|pct}} are *always* properties,
{{kind.state|pct}} constrain a state, {{kind.state_pair|pct}} a pair of states
and {{kind.event|pct}} an event; {{duration|pct}} bound a duration.
{{fitsAutotapTemplate|pct}} fit one AutoTap template exactly, and
{{fitsDeviceweaveDsl|pct}} {{fitsDeviceweaveDsl|ci}} could be stated in
DeviceWeave's DSL at all.

**Reliability.** Over {{agreement.n}} statements, κ = {{agreement.fields.scope.kappa|f2}}
(scope), {{agreement.fields.kind.kappa|f2}} (kind),
{{agreement.fields.modality.kappa|f2}} (modality),
{{agreement.fields.target_actor.kappa|f2}} (target actor) and
{{agreement.fields.condition_actor.kappa|f2}} (condition actor). On the derived
`guard_dw` outcome, κ = {{agreement.derivedOutcomes.guard_dw.kappa|f2}};
{{agreement.disagreements}} statements were adjudicated.

**Table 1.** Outcomes per architecture (reaction on, no manual switch), share of
{{properties|int}} properties.

| Architecture | exact | preemptive | over-restrictive | impossible |
|---|---|---|---|---|
| guard_dw | {{outcomes.reaction=on,manual=off.guard_dw.exact|pct}} | {{outcomes.reaction=on,manual=off.guard_dw.preemptive|pct}} | {{outcomes.reaction=on,manual=off.guard_dw.over_restrictive|pct}} | {{outcomes.reaction=on,manual=off.guard_dw.impossible|pct}} |
| guard_any | {{outcomes.reaction=on,manual=off.guard_any.exact|pct}} | {{outcomes.reaction=on,manual=off.guard_any.preemptive|pct}} | {{outcomes.reaction=on,manual=off.guard_any.over_restrictive|pct}} | {{outcomes.reaction=on,manual=off.guard_any.impossible|pct}} |
| guard_dw_obligation | {{outcomes.reaction=on,manual=off.guard_dw_obligation.exact|pct}} | {{outcomes.reaction=on,manual=off.guard_dw_obligation.preemptive|pct}} | {{outcomes.reaction=on,manual=off.guard_dw_obligation.over_restrictive|pct}} | {{outcomes.reaction=on,manual=off.guard_dw_obligation.impossible|pct}} |
| tap | {{outcomes.reaction=on,manual=off.tap.exact|pct}} | {{outcomes.reaction=on,manual=off.tap.preemptive|pct}} | {{outcomes.reaction=on,manual=off.tap.over_restrictive|pct}} | {{outcomes.reaction=on,manual=off.tap.impossible|pct}} |
| guard_tap | {{outcomes.reaction=on,manual=off.guard_tap.exact|pct}} | {{outcomes.reaction=on,manual=off.guard_tap.preemptive|pct}} | {{outcomes.reaction=on,manual=off.guard_tap.over_restrictive|pct}} | {{outcomes.reaction=on,manual=off.guard_tap.impossible|pct}} |

**Table 2.** Sensitivity: share *not* exactly enforced.

| Assumptions | guard_dw | guard_dw_obligation | tap |
|---|---|---|---|
| reaction on, no manual | {{outcomes.reaction=on,manual=off.guard_dw.notExact|pct}} | {{outcomes.reaction=on,manual=off.guard_dw_obligation.notExact|pct}} | {{outcomes.reaction=on,manual=off.tap.notExact|pct}} |
| reaction on, manual | {{outcomes.reaction=on,manual=on.guard_dw.notExact|pct}} | {{outcomes.reaction=on,manual=on.guard_dw_obligation.notExact|pct}} | {{outcomes.reaction=on,manual=on.tap.notExact|pct}} |
| reaction off, no manual | {{outcomes.reaction=off,manual=off.guard_dw.notExact|pct}} | {{outcomes.reaction=off,manual=off.guard_dw_obligation.notExact|pct}} | {{outcomes.reaction=off,manual=off.tap.notExact|pct}} |
| reaction off, manual | {{outcomes.reaction=off,manual=on.guard_dw.notExact|pct}} | {{outcomes.reaction=off,manual=on.guard_dw_obligation.notExact|pct}} | {{outcomes.reaction=off,manual=on.tap.notExact|pct}} |

**Case study: compiling into a guard.** The compiler produced a policy for
{{compile.compiled|pct}} {{compile.compiled|ci}} of statements, the validator
accepted {{compile.valid|pct}}, and {{compile.accepted|pct}} passed the
rule-set checker. Of the {{compileCrossTab.accepted|int}} accepted policies
whose statement was coded as a property, the guard does not exactly enforce
{{compileCrossTab.acceptedNotEnforcedByGuard|pct}}
{{compileCrossTab.acceptedNotEnforcedByGuard|ci}}: the compilation succeeded
and the property still does not hold.

## 5 Related work

<!-- Each entry must be read in full before submission; see the checklist. -->

- Smart-home trigger-action programming and its bugs [Ur et al., CHI 2014
  — TO VERIFY]; AutoTap [Zhang et al., ICSE 2019].
- Supervisory control in pervasive and IoT settings [Purdue dissertation,
  *Enforcing safety in pervasive computing environments* — TO VERIFY authors,
  year]; modular and hierarchical discrete controller synthesis for IoT and
  smart buildings [HAL hal-01862608, 2018 — TO VERIFY authors]; Knox
  [arXiv:2607.29198 — NOT YET READ].
- Natural-language policy authoring with LLMs: iConPAL [TO VERIFY authors,
  venue]; *Say What You Mean* [arXiv:2505.23835 — TO VERIFY]; HomeBench [TO
  VERIFY].

What we add: prior IoT supervisory-control work synthesises controllers for
given specifications; we measure, on user-written statements, which
*enforcement architecture* a property needs, and show that the architecture
common to LLM policy gates cannot enforce most of them.

## 6 Threats to validity

- **Abstraction.** One boolean per side, multiple conditions collapsed to the
  least controllable one, *event/always* read as its post-state, a 0–2 duration
  counter. A finer plant could move individual properties between classes.
- **Degenerate strategies.** Forcing semantics admit strategies no product
  would ship (for example keeping an alarm sounding so that it cannot start
  sounding later). These appear as *over-restrictive* or *preemptive*, never
  as *exact*.
- **Coding.** Coders infer actors (is this door smart?) from text that rarely
  says; the codebook fixes defaults, and agreement on the actor fields is
  reported separately.
- **Corpus.** One study, {{statementsTotal}} statements from participants who
  opted in to release; a convenience sample, not a population estimate.
- **Case study.** One model, one pass, one DSL; it illustrates the silent
  failure and does not estimate compiler quality in general.

## 7 Conclusion

<!-- One paragraph, written after the numbers exist. -->

## Data and artifact availability

Code, codebook, position-keyed codes and analysis scripts: [REPOSITORY URL,
commit]. The AutoTap Study 1 spreadsheet is not redistributed; it is fetched
from the AutoTap repository at a pinned commit and verified by SHA-256.

## Use of AI tools

[Disclose per the venue's policy: which parts of the tooling and text were
drafted with AI assistance, and that the authors verified every claim.]

## References

- Zhang, He, Martinez, Brackenbury, Lu, Ur. AutoTap: Synthesizing and Repairing
  Trigger-Action Programs Using LTL Properties. ICSE 2019.
  https://ieeexplore.ieee.org/abstract/document/8811900
- Ramadge, Wonham. Supervisory control of a class of discrete event processes.
  SIAM J. Control Optim. 25(1):206–230, 1987. doi:10.1137/0325013
  [controllability formula TO VERIFY against the original]
- Wonham, Ramadge. On the supremal controllable sublanguage of a given language.
  SIAM J. Control Optim. 25(3):637–659, 1987. doi:10.1137/0325036
- Reniers, Cai. Supervisory Control Theory with Event Forcing. arXiv:2404.08469,
  2024. [Theorem 2 subscript TO VERIFY]
