# AutoTap Study 1 and the compile-fidelity harness

DeviceWeave compiles a natural-language household rule into one Policy DSL
object and, at runtime, evaluates active policies with precedence **BLOCK over
MODIFY over ALLOW**. This note records what AutoTap Study 1 actually contains,
how to point the compile-fidelity harness at a local copy, and why that copy
is not in the tree.

## Citation

Weijia He et al., *AutoTap: Synthesizing and Repairing Trigger-Action Programs
Using LTL Properties*, IEEE/ACM 41st International Conference on Software
Engineering (ICSE), 2019.
<https://ieeexplore.ieee.org/abstract/document/8811900>
(DOI 10.1109/ICSE.2019.00043).

Author list: Lefan Zhang, Weijia He, Jesse Martinez, Noah Brackenbury, Shan Lu,
and Blase Ur.

Artifact repository (default branch `master`):
<https://github.com/zlfben/autotap>.

## What the paper is about

End users program smart homes with trigger-action rules ("if this, then that").
Expressing a property that must hold — "the window is never open while it is
raining" — as one such rule often misses corner cases. AutoTap lets a user
state the property instead. It translates the property to linear temporal
logic and synthesizes or repairs trigger-action rules so the property holds,
without disabling behaviors that were already safe.

Study 1 (paper Section III) asked people who already had a household IoT
device to write, in free text, statements about devices that should hold at
all times, with only occasional exceptions. Participants were asked for ten
statements, preferably five that should always be true and five that should
never be true, imagining a house that contained a fixed list of smart devices.
Half of them saw example properties; half did not. The authors coded the
statements into seven templates (one-state unconditional, one-event
unconditional, one-state duration, multi-state unconditional, state-state
conditional, event-state conditional, event-event conditional). Those templates
are the pattern list in `docs/autotap-gap-analysis.md`.

The paper reports 75 responses, four discarded as off-topic or from people
with no smart device, and 71 participants remaining. A second study (Section
VI) compared a property interface with a conventional trigger-action interface;
that workbook is a different file and this harness does not load it.

## Licence

The AutoTap repository's `LICENSE` is the GNU General Public License v3.0
(GitHub identifies it as `GPL-3.0`). The `data/` directory contains the study
workbooks, `survey.pdf`, and `anonymous_optin.sql`, and the repository has no
separate data-licence file for those artifacts. DeviceWeave is Apache-2.0 and
public. The Study 1 workbook and participant text are therefore not copied
into this repository. Reference the paper and the artifact, and fetch the
workbook into a git-ignored cache when you want to run the harness against it.

## Workbook layout

File: `data/Data - User Study 1.xlsx` on branch `master`
(raw URL used by `scripts/fetch_autotap_study1.py`).

Two sheets, both Qualtrics-style exports:

| Sheet | What it is |
| --- | --- |
| `Result` | Responses kept for the study. This is what the harness loads by default. |
| `Discarded Data` | Responses the authors set aside (off-topic, or no smart device). Loaded only with `--include-discarded`. |

On each sheet:

- Row 1 is variable names.
- Row 2 is the survey prompt text (the cell for each category question contains "Which category does your statement belong to?"). The loader skips this row.
- Later rows are participants. Rows whose ten statement cells are all empty are skipped. The AutoTap README describes non-opt-in participants as empty cells; both sheets contain such rows.

`Result` (observed): 94 named columns (A through CP). Row 1 is names, row 2 is prompts, and Excel rows 3 through 73 hold 69 participants with statements plus 2 empty rows. Ids use those Excel row numbers, so they are not a dense 1..69. The first id is `study1:Result:3:s1` and the last populated row is 73. Every one of those 69 rows has `ReleaseData` = Yes and ten statement cells, so the default corpus is **690 rules**. Closed category counts on those 690: 380 "always" and 310 "never". Closed exception counts: 577 with no exception and 113 with an exception (the exception write-in column is filled on those same 113). `saw examples` on the 69 rows splits 38 yes / 31 no. The paper's 71 analyzed participants and this public sheet's 69 complete rows are not the same count; the harness reports the rows it actually reads.

`Discarded Data` (observed): the same columns. 3 rows have ten statements each (30 statements), plus empty rows and the prompt row. The paper says four responses were discarded, so this sheet is not a one-to-one copy of that count.

Each statement `n` from 1 to 10 is five columns. Two of them share the variable name `n_Q35`; the loader tells them apart by column order.

| Column | Role | Used by the harness |
| --- | --- | --- |
| `n_Q25` | Free-text statement. This is the rule. | Compiled. Not written to reports or labels. |
| `n_Q42` | Closed choice: should always happen, or should never happen. | Reduced to `polarity`: `always`, `never`, or null. |
| `n_Q35` (first) | Closed choice: any exceptions? | Reduced to `has_exception`: true if the cell starts with "yes", false if it starts with "no". |
| `n_Q45` | Free-text exception. | Not loaded and not compiled. |
| `n_Q35` (second) | Closed choice plus occasional write-in: would most people want this? | Not loaded. |

Other columns are survey metadata, not rules: duration, the opt-in question (`ReleaseData`), device-ownership checkboxes (`ux_used_*`) and brand fields (`ux_brand_*`), experience with failures and with writing rules, demographics (`Q20` gender, `Q21` age, `Q22` education, `Q23` technical background, `Q17` comments), and `saw examples`. The paper treats the section on buggy device behavior as outside its scope. The harness ignores all of those columns.

Stable id, used as the label key:

```text
study1:<sheet>:<excel-row>:s<statement>
```

Example shape: `study1:Result:3:s1` is statement 1 on the first participant row of `Result`. The sheet name is the workbook's name, including the space in `Discarded Data`.

## How to run

Fetch (network, once) into `.cache/autotap/`, which `.gitignore` excludes:

```bash
python3 scripts/fetch_autotap_study1.py
```

Equivalent manual download, same cache path:

```bash
mkdir -p .cache/autotap
curl -fsSL -o ".cache/autotap/Data - User Study 1.xlsx" \
  "https://raw.githubusercontent.com/zlfben/autotap/master/data/Data%20-%20User%20Study%201.xlsx"
```

The fetch script refuses a destination outside `.cache/autotap/` and refuses to run if git is already tracking an `.xlsx`, that cache, or a fidelity report.

Score without a model, using saved compiler JSONL (`{"id", "compiled"}` per line, `id` = the rule id above):

```bash
python3 scripts/compile_fidelity.py --responses path/to/saved.jsonl
```

Or set the workbook path explicitly:

```bash
AUTOTAP_STUDY1_XLSX=".cache/autotap/Data - User Study 1.xlsx" \
  python3 scripts/compile_fidelity.py --responses path/to/saved.jsonl
```

Call the real compiler (opt-in; uses `LLM_PROVIDER` via `compile_rule`):

```bash
python3 scripts/compile_fidelity.py --live
```

`--live` is never the default. The runner exits if you pass neither `--live` nor `--responses`. CI tests use synthetic spreadsheets and an in-process compiler; they do not download the workbook and do not call a model.

Reports:

- `.cache/compile_fidelity/report.json` — one row per rule (ids, validity, rejection category and reason, label status, exact and structural match). Source sentences are omitted.
- `.cache/compile_fidelity/report.txt` — counts, rates, and outcome categories.

Both paths are git-ignored. Rejection reasons are compiler output and can paraphrase a statement, so leave the report in `.cache/`.

Useful flags: `--labels`, `--out` (still required to sit under `.cache/compile_fidelity/`), `--limit`, `--include-discarded`.

## Expected-policy labels

`benchmarks/compile_fidelity/labels.json` is committed and starts empty. Add labels as you decide what the compiler should emit. The key is the rule id. The file must not contain the original sentence (keys such as `text`, `source_text`, `rule`, and `statement` are rejected).

```json
{
  "version": 1,
  "labels": {
    "study1:Result:3:s1": {
      "expectation": "policy",
      "policy": {
        "scope": {"device_type": "fan"},
        "conditions": [{"field": "temperature", "operator": "<", "value": 65}],
        "action": {"type": "block", "params": {}}
      }
    },
    "study1:Result:4:s2": {"expectation": "reject"}
  }
}
```

A `policy` label is checked with `validate_policy` (confidence and `rule_id` are filled in for that check). Comparison:

- **Exact** — same `device_type`, `action.type`, and `action.params`, and the same multiset of `(field, operator, value)`. `65` and `65.0` match. Condition order does not. `rule_id`, `confidence`, and `action.reason` do not. `>=` and `>` do not match exactly.
- **Structural** — same device, action type, and params, and the same condition region as `rule_set_checker` (the same notion of equivalence as `scripts/policy_compile_bench.py`). `time_hour >= 22` and `time_hour > 21` match structurally.

Rates use labeled rules only. A rule with no label is `unlabeled` and is not a fidelity failure, whether or not it compiled. `expectation: "reject"` passes when the compiler or the validator refuses the rule.

DeviceWeave's synthetic compile benchmark (`benchmarks/policy_compile/rules.jsonl`, built by `scripts/policy_compile_bench.py`) is a separate measurement. It does not contain Study 1 text.
