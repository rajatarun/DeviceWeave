# Compile fidelity: AutoTap Study 1

This directory holds the **labels** for `scripts/compile_fidelity.py`. It
never holds the data.

| Path | Committed | What |
|---|---|---|
| `labels.json` | yes | Expected compiled policies, keyed by spreadsheet position |
| `_cache/` | **no** (git-ignored, and has its own `*` `.gitignore`) | The Study 1 `.xlsx`, written by `fetch` |
| `_out/` | **no** (git-ignored) | `summary.json`, `rows.jsonl`, `report.txt`, `responses.jsonl` |

The dataset comes from AutoTap: Zhang, He, Martinez, Brackenbury, Lu, Ur,
ICSE 2019, <https://ieeexplore.ieee.org/abstract/document/8811900>, and
<https://github.com/zlfben/autotap>. DeviceWeave does not redistribute it. The
AutoTap repository is GPL-3.0, and no separate licence for its data files was
found. Do not commit the spreadsheet, the output directory, or any
participant's words. That includes labels, notes, fixtures and commit
messages.

## Labels

```json
{
  "format": "deviceweave-compile-fidelity-labels/1",
  "dataset": {"name": "AutoTap Study 1", "sha256": "29c85b00…"},
  "labels": {
    "study1:Result:row12:stmt3": {
      "expect": "policy",
      "policies": [
        {"scope": {"device_type": "light"},
         "conditions": [{"field": "time_hour", "operator": ">=", "value": 23}],
         "action": {"type": "block", "params": {}}},
        {"scope": {"device_type": "light"},
         "conditions": [{"field": "time_hour", "operator": "<", "value": 6}],
         "action": {"type": "block", "params": {}}}
      ],
      "note": "overnight window; one DSL rule cannot express it"
    },
    "study1:Result:row12:stmt4": {"expect": "reject", "reason_category": "unsupported_device"}
  }
}
```

- **Rule id**: `study1:<sheet>:row<spreadsheet row>:stmt<1-10>`. Get ids from
  `run --include-text` output in `_out/rows.jsonl`, or from the cell shown in
  every row (for example `BB12`). Ids are positions, so they stay valid only
  for the pinned file. The harness warns when the sha256 differs.
- **`expect: "policy"`**: `policies` is one or more Policy DSL objects.
  `rule_id`, `confidence` and `action.reason` may be omitted. Each one must
  pass `validator.validate_policy`. Several policies mean "any of these"
  (OR). The compiler emits one policy, so a multi-policy label scores
  `mismatch` with `needs_multiple_policies`. That records a DSL gap, not a
  labelling error.
- **`expect: "reject"`**: the statement is outside what the DSL can say.
  `reason_category` is optional: `unsupported_device`,
  `unsupported_condition`, `ambiguous`, `low_confidence`, `unsatisfiable` or
  `other`.
- **`note`**: optional, in your own words. The harness refuses a note that
  contains the statement's text, and refuses any key other than `expect`,
  `policies`, `reason_category` and `note`.

### Scoring

| Label outcome | Meaning |
|---|---|
| `unlabeled` | No label. It is reported, never counted as a failure |
| `match_exact` | Same device, action, params and condition set |
| `match_equivalent` | Same device, action and params, and the conditions match exactly the same contexts (`rule_set_checker` regions, so `time_hour > 22` ≡ `time_hour >= 23`) |
| `mismatch` | Accepted, but different. `mismatch` lists `device` / `action` / `params` / `conditions` / `needs_multiple_policies` |
| `policy_expected_but_refused` | A policy was expected, but the compiler, validator or rule-set check refused |
| `reject_expected_ok` / `reject_expected_but_compiled` | Out-of-scope statement refused, or wrongly accepted |

## Paper-grade live run

Claude Haiku 4.5 on Amazon Bedrock, temperature 0, five repetitions of each
statement. `--provider bedrock` does not fall back to Gemini. Outputs stay in
git-ignored `_out/`. Records are keyed by statement id; the statement text is
not written. An `infra_error` (throttling or a timeout that used every retry)
is left out of the model-failure rates and is retried by `--resume`.

Price the calls first. No model is contacted. Replace the two prices; none
are built into the harness. Input tokens are `ceil(character_count/4)` of the
system prompt plus the filled user message.

```bash
python scripts/compile_fidelity.py fetch

python scripts/compile_fidelity.py run --dry-run-cost --reps 5 \
  --input-usd-per-million 1 --output-usd-per-million 5
```

Twenty-statement dry run, then the full Result sheet (690 statements) at five
repetitions. `--resume` keeps the twenty that already succeeded.

```bash
python scripts/compile_fidelity.py run --live \
  --provider bedrock \
  --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --temperature 0 \
  --reps 5 \
  --concurrency 4 \
  --limit 20 \
  --out benchmarks/autotap/_out

python scripts/compile_fidelity.py run --live \
  --provider bedrock \
  --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --temperature 0 \
  --reps 5 \
  --concurrency 4 \
  --resume \
  --out benchmarks/autotap/_out
```

`summary.json` records the model id, provider, temperature, reps, `prompt_hash`
(sha256 of the system prompt, a newline, and the user template with `{rule}`
still unfilled), the harness git commit, and the summed input and output
tokens from every record in `responses.jsonl`.

Classify those responses offline. Infrastructure rows are omitted.

```bash
python scripts/compile_fidelity.py enforceability \
  --responses benchmarks/autotap/_out/responses.jsonl \
  --detail summary
```
