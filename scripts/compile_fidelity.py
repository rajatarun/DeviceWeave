#!/usr/bin/env python3
"""
Compile-fidelity runner for AutoTap Study 1 statements.

Reads a local Study 1 workbook (never a committed copy), compiles each
statement, validates the Policy DSL with the repo validator, and compares
labeled rules to benchmarks/compile_fidelity/labels.json. Unlabeled rules
are reported as unlabeled, not as failures.

Citation: Weijia He et al. (Lefan Zhang, Weijia He, Jesse Martinez, Noah
Brackenbury, Shan Lu, and Blase Ur), AutoTap, IEEE/ACM ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900.
Artifact: https://github.com/zlfben/autotap (branch master).

The workbook is not redistributed. The AutoTap repo is GPL-3.0 and no
separate data licence was found for data/. Fetch the file into the
git-ignored cache:

    python3 scripts/fetch_autotap_study1.py
    python3 scripts/compile_fidelity.py --responses saved.jsonl
    python3 scripts/compile_fidelity.py --live

``--live`` is opt-in. It calls policy_authoring.llm_compiler.compile_rule,
which uses the configured LLM provider. Tests and CI do not pass it.
Without ``--live`` or ``--responses`` the runner refuses to call a model.

Reports go to .cache/compile_fidelity/ (git-ignored) as report.json and
report.txt. They do not contain the source sentences.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policy_authoring.compile_fidelity import (  # noqa: E402
    live_compile,
    load_labels,
    load_responses,
    load_study1,
    report_dir,
    responses_compile,
    run,
    study1_workbook_path,
    write_report,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--xlsx",
        default=os.environ.get("AUTOTAP_STUDY1_XLSX") or None,
        help="Local Study 1 workbook. Defaults to AUTOTAP_STUDY1_XLSX, then "
             ".cache/autotap/Data - User Study 1.xlsx.",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="Expected-policy JSON. Default: benchmarks/compile_fidelity/labels.json.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Git-ignored report directory. Default: .cache/compile_fidelity/.",
    )
    parser.add_argument(
        "--include-discarded",
        action="store_true",
        help="Also load the Discarded Data sheet.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--responses",
        default=None,
        help="JSONL of {id, compiled} to score without calling a model.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Opt in to the real compiler (compile_rule). Requires a configured "
             "LLM provider. Omitted in tests.",
    )
    args = parser.parse_args(argv)

    if args.live and args.responses:
        parser.error("pass only one of --live and --responses")
    if not args.live and not args.responses:
        parser.error(
            "refusing to call a model. Pass --responses FILE to score saved "
            "compiler output offline, or --live to opt in to compile_rule "
            "(configured LLM provider). --live is not used by tests."
        )

    workbook = Path(args.xlsx) if args.xlsx else study1_workbook_path()
    labels_path = Path(args.labels) if args.labels else (
        Path(__file__).resolve().parents[1] / "benchmarks" / "compile_fidelity" / "labels.json"
    )
    out = Path(args.out) if args.out else report_dir()
    sheets = ("Result", "Discarded Data") if args.include_discarded else ("Result",)

    rules = load_study1(workbook, sheets=sheets)
    if args.limit is not None:
        rules = rules[: args.limit]
    labels = load_labels(labels_path)

    if args.live:
        print(
            "compile-fidelity: --live is opt-in. Each statement is sent to "
            "policy_authoring.llm_compiler.compile_rule, which calls the "
            "configured LLM provider (LLM_PROVIDER). Tests never set this flag.",
            file=sys.stderr,
        )
        compile_fn = live_compile
        compiler = "live"
    else:
        compile_fn = responses_compile(load_responses(Path(args.responses)))
        compiler = "responses"

    report = run(rules, labels, compile_fn, compiler=compiler)
    json_path, text_path = write_report(report, out)
    print(text_path.read_text(), end="")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
