# AutoTap Study 1 vs the DeviceWeave Policy DSL — what people ask for, and what compiles

This note compares the household rules people wrote in AutoTap's first user
study with what DeviceWeave's Policy DSL can express. The examples are written
for this document. None is quoted or paraphrased from a participant.

**Source.** Lefan Zhang, Weijia He, Jesse Martinez, Noah Brackenbury, Shan Lu,
Blase Ur. *AutoTap: Synthesizing and Repairing Trigger-Action Programs Using
LTL Properties.* ICSE 2019. <https://ieeexplore.ieee.org/abstract/document/8811900>.
Artifact and data: <https://github.com/zlfben/autotap> (default branch `master`;
pinned here to commit `fe0fdd638a1b170b1ac71b0f2921ab54d547893b`).

**Not redistributed.** The AutoTap repository is GPL-3.0. Its `STATUS.md`
describes the tool as GPLv3 and says some study data is released, but no
separate licence for the files in `data/` was found. DeviceWeave is Apache-2.0,
so it does not copy the spreadsheets, any participant text, or any AutoTap code.
`scripts/compile_fidelity.py fetch` downloads Study 1 into a git-ignored cache
when you need it. See [README → Compile fidelity](../README.md#compile-fidelity-against-autotap-study-1).

## 1. What the paper and repository say

The IEEE page and the paper PDF were not reachable from the environment this
was written in, so this summary uses the abstract, the repository README,
`STATUS.md`, the property-builder UI in the repository, and the spreadsheet's
structure.

- **The paper.** Trigger-action programming (TAP) is how end users usually
  automate smart homes, and users often find it hard to state what they want
  as TAP rules. AutoTap lets novice users state *properties* instead. It
  translates them to linear temporal logic (LTL), then synthesises
  property-satisfying TAP rules or repairs existing ones. Its design came from
  Study 1, a survey about the properties people want. Study 2 showed that
  novices made significantly fewer mistakes stating properties in AutoTap than
  writing TAP rules.
- **Property templates** (from the property-builder UI in
  `ifttt-frontend/.../sp-creator*`). These are the shapes AutoTap chose after
  Study 1:
  - *This state* should always / never be active.
    - Optionally *for more than this long*.
    - Optionally *while that*.
    - Or both.
  - *This state and this state* should always / never occur together.
  - *This event* should always / never happen.
    - Optionally *while that*.
    - Or *within this long after that*.
- **Data.** The repository's `data/` directory holds:
  - `Data - User Study 1.xlsx`
  - `Data - User Study 2.xlsx`
  - `survey.pdf`, the survey and tutorial
  - `anonymous_optin.sql`

  The README says 73 of 75 Study 1 participants opted in to release. Rows for
  participants who did not opt in are left blank.

### Study 1 spreadsheet structure

| | |
|---|---|
| Sheets | `Result` (73 rows × 97 columns), `Discarded Data` (8 rows × 97 columns: off-topic answers, or participants with no smart devices) |
| Row 1 | Qualtrics column ids |
| Row 2 | Question text |
| Rows 3+ | One participant per row. In `Result`, 71 rows, 2 of them blank (not opted in) → **69 participants** |
| Per participant | `Duration`, `ReleaseData` (opt-in), devices used (`ux_used_*`), brands (`ux_brand_*`), frustrations with failures, built-in features and rule writing (`fail_*`, `built_in_*`, `rule_*`, `others_*`, `Q48`, `Q55`), **ten statements**, demographics (`Q20`–`Q23`), feedback (`Q17`), `saw examples` |
| Statement *k* (1–10) | `k_Q25` the statement · `k_Q42` category ("Things that should always happen" / "… never happen") · `k_Q35` "Are there any exceptions?" · `k_Q45` the exception · a second `k_Q35` "Do you expect that this is something most people would want?" |
| Rules | **690** in `Result` (69 × 10): 380 *always*, 310 *never*; 113 marked as having exceptions. `Discarded Data` adds 30 from 3 participants (not loaded by default) |
| Merged cells | 850 in `Result`. They span empty answer cells, never a statement |
| sha256 | `29c85b0081eb4e2c6c4590c6c87838b4bb1c114315b5c2f004142ab3d967728e` |

Study 1 statements are **properties** ("X should always / never happen"), not
trigger-action rules. Many are phrased as triggers ("when …, …"), but the
survey asked for invariants. That is the main reason for the mismatch below:
DeviceWeave's DSL is a **guard on commands**, not an automation language.

## 2. What the DSL can say

A policy has one `scope.device_type` from `fan | light | ac | plug | heater`.
It has an AND of conditions, each on one context field: `temperature`,
`humidity`, `time_hour` (integer 0–23), `cloud_cover_pct`, `is_home` or
`is_overcast`. Each policy has an action: `block`, `modify` (replace params) or
`allow` (informational).

The policy is evaluated when someone *asks* a device to do something.
`turn_off` and `get_status` always bypass it. The verdict is
BLOCK > MODIFY > ALLOW. A policy never causes anything to happen on its own.

## 3. Patterns and coverage

The *share* column is the harness's keyword tagger (`compile_fidelity.py list`)
run over the 690 statements. It is a heuristic for ranking gaps, not a coding
of the data. Multiple tags per statement are possible.

| # | Pattern | Example (our wording) | Share (heuristic) | DSL coverage | Suggested DSL change |
|---|---|---|---|---|---|
| 1 | **Never** a state, under context conditions | "Never run the space heater while the house is empty." | 310 *never* | **Covered**, when the device and the condition are in the vocabulary: `block` + conditions | — |
| 2 | **Always** a state / obligation | "The porch light should be on whenever it's dark out." | 380 *always* | **Not covered.** `allow` is informational, and nothing switches a device on. Making the opposite command fail does not work either, because `turn_off` bypasses policy by design | A separate *obligation* rule type run by a scheduler or automation runner, outside the guard. Keep the evaluator unchanged |
| 3 | Devices outside the five types | "The garage door must never stay open overnight." | 359 mention another device; 46 mention a supported one | **Not covered**: locks, doors, windows, cameras, thermostat, garage, blinds, sprinklers, appliances. Adapters exist for some (Ring, MyQ), but the policy enum does not include them | Extend `device_type` in step with providers. Each new type also needs a reviewed meaning for "on" |
| 4 | A specific device or room | "Don't run the bedroom fan after midnight." | — | **Partial.** `scope` is a device *type*, so the rule applies to every fan | Optional `scope.device_id` / `scope.room`. Loader and evaluator filter on it |
| 5 | Two device states together | "The AC should never be on while a window is open." | 68 multi-device | **Not covered.** The context has no device state | A `device_state` condition naming a device. It is still one boolean axis, so the rule-set checker's box argument holds. The context provider must read registry state |
| 6 | Time of day | "No loud devices before 7 am." / "Lights off from 11 pm to 6 am." / "At 6:30 …" | 60 | **Partial.** Whole hours only. A window across midnight needs two rules, because conditions are ANDed, and the compiler emits one. Minutes cannot be stated | Let the compiler emit a *list* of policies (OR), or add `minute_of_day`. The harness's labels already accept multi-policy expectations, so this gap shows up as a measured mismatch |
| 7 | Day / date / season | "On weekdays, turn the coffee maker off by 9." | 11 | **Not covered** | `day_of_week` (0–6) and `is_weekend`. Both fit the box model |
| 8 | Duration | "The fan should not run for more than two hours." | 34 | **Not covered.** No state-since timestamps, and enforcing it means turning something off, which is an action, not a gate | `on_for_minutes` context, plus an obligation (see 2) to switch off |
| 9 | Event trigger or sequence | "Within 5 minutes after I leave, the heater should be off." | 193 event words; 51 sequence words | **Not covered.** Policies see current context at command time. There is no event stream | Event-derived context (for example `minutes_since_departure`). Larger change |
| 10 | Presence | "Only when someone is home." / "while we're asleep" | 65 | **Partial.** Household `is_home` only. No per-person presence and no asleep or vacation state | `occupants_home` count, or per-person booleans |
| 11 | Weather / climate | "Don't open the blinds when it's raining." | 72 | **Partial.** Temperature, humidity and cloud cover are covered. Rain, snow and wind are not | `is_raining` / `precipitation_mm` from the existing Open-Meteo client |
| 12 | Exceptions / "unless" | "Keep the lamp off after 10 pm unless I'm home." | 113 flagged by participants; 19 say "unless/except" | **Partial.** "Unless X", with X on one field, is the negated condition, ANDed in. "Unless A and B" is a disjunction, so it needs several rules. The compiler prompt gives no guidance for "unless" | Add a semantic mapping ("unless X" → negated X) to the prompt, plus multi-policy output (see 6) |
| 13 | Disjunction ("or") | "No heater if it's over 75 or nobody is home." | 46 | **Partial.** Expressible as two rules; the compiler emits one | Multi-policy output (see 6) |
| 14 | Set-points / levels | "Cap the thermostat at 74." / "Lamps no brighter than half after 9 pm." | 49 | **Partial.** `modify` can replace params, but there is no condition on the *requested* value and no clamp | `requested.<param>` condition fields, or a `clamp` modify param |
| 15 | Periodic / scheduled | "Every night at 10, make sure the lights are off." | 14 | **Not covered.** There is no scheduler | Same runner as 2 |
| 16 | Notifications | "Alert me if the garage door is left open." | 24 | **Not covered.** There is no notify action | A `notify` action type. It changes runtime semantics, so it needs separate review |
| 17 | Conflicting rules | "Always keep the porch light on" plus "Never run lights when nobody is home" | — | **Covered at authoring.** `rule_set_checker` reports `conflict_block_allow`, `shadowed`, `unsatisfiable` and `redundant` with a witness context. BLOCK > MODIFY > ALLOW resolves deterministically | — (AutoTap resolves the same tension with user-set priorities) |
| 18 | Safety invariants | "The heater must be blocked whenever nobody is home." | — | **Covered for one device type** via `POLICY_INVARIANTS` (checked against the rule set). Not across devices | Follows from 5 |

### What this change implements, and what it does not

**No DSL or runtime change is made.** Every candidate above changes at least
one of these:

- the production compiler prompt (what live rules compile to);
- the context provider (what the evaluator sees);
- the evaluator or scope semantics.

None of them is a clearly low-risk addition. The BLOCK > MODIFY > ALLOW
precedence and the evaluator are untouched.

What this change adds is **measurement**, in `scripts/compile_fidelity.py`:

- The pattern tags above. A live run reports acceptance per pattern, so these
  gaps can be ranked with numbers rather than examples.
- Labels whose `policies` is a list. When a statement needs two DSL rules (an
  overnight window, an "or"), the label says so, and the harness reports
  `needs_multiple_policies` instead of hiding the gap.

The cheapest changes that would move the most statements are, in order:

1. multi-policy compiler output (6, 12, 13);
2. `day_of_week` (7);
3. per-device scope (4).

Each needs its own review, because each changes what live rules compile to.
