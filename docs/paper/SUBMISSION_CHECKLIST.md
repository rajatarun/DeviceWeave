# From tool to paper: what is done, and what only people can do

The claim to test: **a refusal-only command guard structurally cannot
enforce a measurable share of what users ask their homes to do.** The draft
(`draft.md`) states it with placeholders. Every number comes from
`scripts/property_coding.py render`. Nothing in this directory is a result
yet.

## Done (tooling, tested)

| Piece | Where | Evidence |
|---|---|---|
| Pinned, checksummed corpus loader (not redistributed) | `scripts/compile_fidelity.py` | matches openpyxl on all 690 cells |
| DSL-independent codebook on AutoTap's templates | `docs/paper/codebook.md` v1.0 | — |
| Coder worksheets (seeded pilot subset, per-coder order), import with codebook checks, no text in code files | `property_coding.py worksheet / import` | tests; refuses tracked paths and notes that repeat a statement |
| Cohen's κ per field and on the derived outcome; adjudication | `agree / adjudicate` | κ checked against a textbook 2×2 value |
| Property → plant → outcome under 5 architectures × 4 assumption sets | `outcome_of` | 9 canonical properties match a hand derivation; 6 mutations of the semantics each fail a test; synthesis itself cross-checked against brute force in `tests/test_controllability.py` |
| Shares with Wilson 95% intervals; DSL and template coverage; compile cross-tab | `analyze` | tests |
| Placeholder-only draft that cannot reference a missing key | `draft.md`, `render` | test fails on any unfilled placeholder |

## Critical path (in order)

1. **Freeze the LLM codes.** Done: `benchmarks/autotap/coding/llm.json` (all
   690 statements, codebook v1.1) and the seeded 20% blind subset
   (`blind_ids.json`) are committed *before* any human verification, so the
   LLM rater cannot be tuned to the verifier. Record that commit hash in the
   paper and name the model that produced the codes.
2. **Verify (one author, about 2–3 hours).** Use the private survey page.
   - On the 138 blind statements, code from scratch; no suggestion is shown.
   - On the other 552, confirm Claude's pre-selected code or change any field.
   - Answers save as you go.
3. **Import.** Export the page's `answers` collection with `ArtifactData`
   (`out_dir`), then run `property_coding.py verify <dir>`. That writes
   `gold.json`, `agreement.json` (blind human vs LLM) and `verification.json`
   (change rates).
4. **Analyse.** Run `analyze gold.json --rows <live run rows.jsonl>
   --agreement agreement.json --verification verification.json --total 690`,
   then `render`. Read Table 2 before writing the conclusion. If the claim
   holds only under one assumption set, the paper says so.
5. **Optional, stronger.** Have a second person code a random 100 statements
   blind with `worksheet --sample 100`, then compare with `agree`. That gives
   human–human κ beside human–LLM κ.

## Citations and reading (before submission)

- [ ] Ramadge & Wonham 1987 (SIAM 25(1)): check the controllability formula
      against the original typesetting.
- [ ] Reniers & Cai, arXiv:2404.08469: check whether Theorem 2's
      `L_m(V_sup/P) = F` dropped a subscript on `F_sup`.
- [ ] Ramadge & Wonham, Proc. IEEE 1989: the wording of the
      `O(|Q|^2 |Σ|)` bound, if it is cited.
- [ ] Read in full and position against: Knox (arXiv:2607.29198); iConPAL;
      *Say What You Mean* (arXiv:2505.23835); HomeBench; the Purdue
      dissertation on safety in pervasive computing; HAL hal-01862608
      (discrete control for IoT and smart buildings). Fill in authors and
      venues; every `[TO VERIFY]` in `draft.md` goes.
- [ ] AutoTap's first author is Lefan Zhang; Weijia He is second.

## Integrity and ethics

- [ ] **Data:** the participants opted in to public research release.
      Confirm secondary use with your institution (IRB exemption), and
      consider telling the AutoTap authors. Release only position-keyed codes,
      never statement text.
- [ ] **AI assistance:** the tooling was written with AI coding agents
      (Claude Code; Cursor for the controllability engine). State this
      according to the venue's policy. The authors must re-derive the canonical
      outcomes in `tests/test_property_coding.py` themselves, and understand
      every modelling choice in `codebook.md` § "How codes become plants".
- [ ] **Say exactly who coded what.** An LLM coded every statement; one
      author verified every code; the blind subset is the only unanchored
      human–LLM comparison. Never describe this as two independent human
      coders.

## What it would take to go beyond a workshop paper

- A second corpus (for example AutoTap Study 2's properties, or IFTTT
  recipes), so the share is not one study's artefact.
- A finer plant (multiple conditions kept separate) and a check that the
  headline share is stable under it.
- Two or more compilers and models in the case study, with repeated runs.
