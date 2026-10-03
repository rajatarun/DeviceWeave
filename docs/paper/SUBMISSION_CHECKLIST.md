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

## Critical path (people, in order)

1. **Freeze the plan.** Commit the codebook and this analysis plan, and
   record the hash in the paper. Optionally pre-register it (OSF). From this
   point the codebook changes only by a new version.
2. **Pilot (about 2 hours per coder).** Each coder runs
   `worksheet --coder A --sample 60` (and B), codes the sheet, and imports it.
   Then run `agree`.
   - If κ < 0.7 on `kind`, `modality`, `target_actor` or `condition_actor`,
     revise the codebook (new version), recode the pilot, and repeat.
   - Report the pilot κ.
3. **Full coding (about 10–12 hours per coder, at roughly 1 minute per
   statement).** Run `worksheet` without `--sample`. Code independently and
   don't discuss cases.
4. **Adjudication (about 2–4 hours).** A third person, or both coders together,
   resolves every id that `agree` lists. Record who did it.
5. **Analysis (minutes).**
   `analyze gold.json --rows <live run rows.jsonl> --agreement agreement.json --total 690`,
   then `render`. Read Table 2 before writing a word of the conclusion. If
   the claim only holds under one assumption set, the paper says so.
6. **Baseline.** Table 1 already compares five architectures on the same
   properties. For an external baseline, also report the share that fits
   AutoTap's template language (`fitsAutotapTemplate`), and, if time allows,
   run AutoTap's own synthesiser on the properties that fit.

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
- [ ] **No human coding by models.** Do not substitute an LLM for either
      coder. An LLM may be reported as a third, clearly labelled rater against
      the adjudicated gold.

## What it would take to go beyond a workshop paper

- A second corpus (for example AutoTap Study 2's properties, or IFTTT
  recipes), so the share is not one study's artefact.
- A finer plant (multiple conditions kept separate) and a check that the
  headline share is stable under it.
- Two or more compilers and models in the case study, with repeated runs.
