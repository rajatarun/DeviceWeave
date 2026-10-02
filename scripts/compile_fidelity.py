#!/usr/bin/env python3
"""
Compile fidelity: how faithfully do rules written by real people compile into the Policy DSL?

``policy_compile_bench.py`` measures the compiler on rules generated from
DeviceWeave's own vocabulary, so every rule is phrased the way the compiler
prompt expects. This harness feeds it rules that people wrote for their own
homes, without seeing DeviceWeave's schema first, and records what happens to
each one.

Source data -- referenced, never redistributed
----------------------------------------------
The rules come from Study 1 of AutoTap:

  Lefan Zhang, Weijia He, Jesse Martinez, Noah Brackenbury, Shan Lu, Blase Ur.
  "AutoTap: Synthesizing and Repairing Trigger-Action Programs Using LTL
  Properties." ICSE 2019. https://ieeexplore.ieee.org/abstract/document/8811900
  Artifact: https://github.com/zlfben/autotap  (data/Data - User Study 1.xlsx)

In Study 1 each participant wrote ten statements of the form "things that
should always / never happen" in their smart home. The AutoTap repository is
GPL-3.0 and no separate licence for the files in its ``data/`` directory was
found, so DeviceWeave (Apache-2.0) does not copy the spreadsheet, any
participant text, or any AutoTap code. ``fetch`` downloads the file into a
git-ignored cache; every other command reads it from a local path at run time.
Nothing derived from participant text (labels, fixtures, docs) is committed:
labels are keyed by spreadsheet position, and the test fixtures are invented.

Commands
--------
  fetch   download Study 1 into benchmarks/autotap/_cache/ (git-ignored),
          pinned to an AutoTap commit and checked against its sha256
  list    load the rules and print counts -- no compiler, no network
  run     compile every rule and score it:
            --responses FILE  score saved compiler outputs (offline)
            --live            call the LLM (opt-in: costs money and sends
                              participant text to that provider). Paper run:
                              --provider bedrock --model-id ID --temperature 0
                              --reps 5 --concurrency K --out DIR --resume
            --dry-run-cost    print call count, estimated input tokens, and
                              cost from --input-usd-per-million and
                              --output-usd-per-million. No network call.
  enforceability
          classify compiled policies with the offline controllability check
          (policy_authoring.controllability.benchmark_enforceability).
          Reads --policies JSON or --responses JSONL. Does not open the
          Study 1 workbook and does not call a model. The shares are for
          the policies you pass in.

Per rule it records whether the compiler produced a policy or refused, whether
the repository's own validator accepts it, whether the rule-set checker finds it
unsatisfiable (the authoring API rejects those too), the refusal or validation
reason with a coarse category, and -- when ``benchmarks/autotap/labels.json``
has an expected policy for that rule id -- exact and meaning-level match. Rules
without a label are reported as ``unlabeled``, never as failures.

Usage
-----
  python scripts/compile_fidelity.py fetch
  python scripts/compile_fidelity.py list
  python scripts/compile_fidelity.py run --responses saved.jsonl
  python scripts/compile_fidelity.py run --dry-run-cost --reps 5 \\
      --input-usd-per-million PRICE --output-usd-per-million PRICE
  python scripts/compile_fidelity.py run --live --provider bedrock \\
      --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \\
      --temperature 0 --reps 5 --concurrency 4 --limit 20 \\
      --out benchmarks/autotap/_out
  python scripts/compile_fidelity.py run --live --provider bedrock \\
      --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 \\
      --temperature 0 --reps 5 --concurrency 4 --resume \\
      --out benchmarks/autotap/_out
  python scripts/compile_fidelity.py enforceability \\
      --responses benchmarks/autotap/_out/responses.jsonl --detail summary

The spreadsheet path is ``--xlsx PATH``, else ``$AUTOTAP_STUDY1_XLSX``, else
the cache file ``fetch`` writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from policy_authoring.rule_set_checker import check_new_rule, region_minus, rule_region  # noqa: E402
from policy_authoring.validator import ValidationError, validate_policy  # noqa: E402

BENCH_DIR = ROOT / "benchmarks" / "autotap"
CACHE_DIR = BENCH_DIR / "_cache"      # git-ignored: the downloaded spreadsheet
OUT_DIR = BENCH_DIR / "_out"          # git-ignored: reports and saved responses
LABELS_PATH = BENCH_DIR / "labels.json"
CACHE_FILE = CACHE_DIR / "autotap-study1.xlsx"
ENV_VAR = "AUTOTAP_STUDY1_XLSX"

AUTOTAP_REPO = "zlfben/autotap"
# Pinned so rule ids (sheet + row + statement slot) keep pointing at the same
# statements. Override with ``fetch --ref master`` at the cost of re-checking labels.
AUTOTAP_COMMIT = "fe0fdd638a1b170b1ac71b0f2921ab54d547893b"
STUDY1_REPO_PATH = "data/Data - User Study 1.xlsx"
STUDY1_SHA256 = "29c85b0081eb4e2c6c4590c6c87838b4bb1c114315b5c2f004142ab3d967728e"

LABELS_FORMAT = "deviceweave-compile-fidelity-labels/1"
DATASET = "study1"
RULE_ID_RE = re.compile(r"^study1:(?P<sheet>[^:]+):row(?P<row>[1-9]\d*):stmt(?P<slot>[1-9]\d*)$")

NOTICE = (
    "AutoTap Study 1 (Zhang, He, et al., ICSE 2019; github.com/zlfben/autotap) is not "
    "redistributed by DeviceWeave: the AutoTap repository is GPL-3.0 and no separate data "
    "licence was found. Keep the file in the git-ignored cache; do not commit it or any "
    "participant text."
)


class FidelityError(Exception):
    """A user-facing error: bad path, unexpected spreadsheet layout, bad labels file."""


# ─────────────────────────────────────────────────────────────────────────────
# Minimal .xlsx reader (stdlib only)
#
# An .xlsx file is a zip of XML parts. Reading cell text needs three of them:
# the workbook (sheet names -> part ids), its relationships (ids -> paths) and
# the shared-string table. Doing that directly keeps the harness free of a
# spreadsheet dependency that neither the Lambda nor the test suite needs.
# ─────────────────────────────────────────────────────────────────────────────

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")

Sheet = Dict[int, Dict[str, str]]   # row number -> column letter -> text


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el: Optional[ET.Element]) -> str:
    """All <t> text under an element, skipping phonetic runs (<rPh>)."""
    parts: List[str] = []

    def walk(e: ET.Element) -> None:
        if e.tag == _M + "rPh":     # phonetic guide text is not part of the visible value
            return
        if e.tag == _M + "t" and e.text:
            parts.append(e.text)
        for child in e:
            walk(child)

    if el is not None:
        walk(el)
    return "".join(parts)


def read_xlsx(path: Path) -> Dict[str, Sheet]:
    """Every sheet's non-empty cells as text, keyed by sheet name."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FidelityError(f"{path} is not a readable .xlsx file: {exc}") from exc
    with zf:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names:
            raise FidelityError(f"{path} has no xl/workbook.xml -- not an .xlsx workbook")
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in sst.findall(_M + "si")]
        targets: Dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels.findall(_REL + "Relationship"):
                target = rel.get("Target", "")
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                targets[rel.get("Id", "")] = target
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        out: Dict[str, Sheet] = {}
        for i, sheet in enumerate(workbook.iter(_M + "sheet"), start=1):
            part = targets.get(sheet.get(_R_ID, ""), f"xl/worksheets/sheet{i}.xml")
            if part not in names:
                raise FidelityError(f"sheet {sheet.get('name')!r} points at missing part {part}")
            out[sheet.get("name", f"Sheet{i}")] = _read_sheet(ET.fromstring(zf.read(part)), shared)
        return out


def _read_sheet(root: ET.Element, shared: Sequence[str]) -> Sheet:
    rows: Sheet = {}
    for row_pos, row in enumerate(root.iter(_M + "row"), start=1):
        rnum = int(row.get("r", row_pos))
        cells: Dict[str, str] = {}
        col = -1
        for c in row.findall(_M + "c"):
            m = _CELL_REF.match(c.get("r", ""))
            col = _column_index(m.group(1)) if m else col + 1
            kind = c.get("t", "n")
            if kind == "inlineStr":
                value = _text(c.find(_M + "is"))
            else:
                v = c.find(_M + "v")
                raw = v.text if v is not None and v.text is not None else ""
                if kind == "s" and raw:
                    value = shared[int(raw)]
                elif kind == "b":
                    value = "TRUE" if raw == "1" else "FALSE"
                else:
                    value = raw
            if value != "":
                cells[column_letter(col)] = value
        if cells:
            rows[rnum] = cells
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Loading Study 1 rules
#
# Layout (a Qualtrics export): row 1 holds column ids, row 2 the question
# text, and every later row one participant. Statement k (1..10) sits in
# column ``k_Q25``, followed by its category (``k_Q42``: "Things that should
# always/never happen"), whether it has exceptions (the first ``k_Q35``: "Yes,
# there are exceptions..." / "No, ..."), the exception text (``k_Q45``) and a
# second ``k_Q35``. Participants who did not opt in to release are blank rows.
# ─────────────────────────────────────────────────────────────────────────────

STATEMENT_HEADER = re.compile(r"^(\d+)_Q25$")
HEADER_ROW = 1
FIRST_DATA_ROW = 3
DEFAULT_SHEETS = ("Result",)


@dataclass(frozen=True)
class SourceRule:
    rule_id: str                    # study1:<sheet>:row<r>:stmt<k> -- stable, contains no text
    sheet: str
    row: int
    slot: int
    cell: str                       # e.g. "BB12", for finding the statement in the spreadsheet
    text: str                       # participant text: held in memory, never written by default
    category: Optional[str]         # "always" | "never" | None
    has_exception: Optional[bool]   # the participant said the statement has exceptions


def make_rule_id(sheet: str, row: int, slot: int) -> str:
    return f"{DATASET}:{sheet}:row{row}:stmt{slot}"


def _category(value: Optional[str]) -> Optional[str]:
    v = (value or "").lower()
    if "never" in v:
        return "never"
    if "always" in v:
        return "always"
    return None


def _exception_flag(value: Optional[str]) -> Optional[bool]:
    v = (value or "").strip().lower()
    if v.startswith("yes"):
        return True
    if v.startswith("no"):
        return False
    return None


def load_rules(path: Path, sheets: Sequence[str] = DEFAULT_SHEETS) -> List[SourceRule]:
    """Every non-empty statement in ``sheets``, in sheet/row/slot order."""
    book = read_xlsx(path)
    rules: List[SourceRule] = []
    for sheet_name in sheets:
        if sheet_name not in book:
            raise FidelityError(f"sheet {sheet_name!r} not in {path.name}; sheets: {sorted(book)}")
        sheet = book[sheet_name]
        header = sheet.get(HEADER_ROW, {})
        ordered = sorted(header.items(), key=lambda kv: _column_index(kv[0]))
        slots: List[Tuple[int, str, Optional[str], Optional[str]]] = []
        for pos, (col, name) in enumerate(ordered):
            m = STATEMENT_HEADER.match(name.strip())
            if not m:
                continue
            k = m.group(1)
            following = ordered[pos + 1: pos + 5]
            cat_col = next((c for c, n in following if n.strip() == f"{k}_Q42"), None)
            exc_col = next((c for c, n in following if n.strip() == f"{k}_Q35"), None)
            slots.append((int(k), col, cat_col, exc_col))
        if not slots:
            raise FidelityError(
                f"sheet {sheet_name!r} has no statement columns (headers like '1_Q25' in row "
                f"{HEADER_ROW}); is this AutoTap 'Data - User Study 1.xlsx'?")
        for rnum in sorted(r for r in sheet if r >= FIRST_DATA_ROW):
            row = sheet[rnum]
            for k, col, cat_col, exc_col in slots:
                text = row.get(col, "").strip()
                if not text:
                    continue
                rules.append(SourceRule(
                    rule_id=make_rule_id(sheet_name, rnum, k), sheet=sheet_name, row=rnum, slot=k,
                    cell=f"{col}{rnum}", text=text,
                    category=_category(row.get(cat_col)) if cat_col else None,
                    has_exception=_exception_flag(row.get(exc_col)) if exc_col else None))
    return rules


def resolve_xlsx_path(cli_path: Optional[str], env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    raw = cli_path or env.get(ENV_VAR) or ""
    path = Path(raw).expanduser() if raw else CACHE_FILE
    if not path.is_file():
        source = "--xlsx" if cli_path else (f"${ENV_VAR}" if env.get(ENV_VAR) else "the default cache path")
        raise FidelityError(
            f"Study 1 spreadsheet not found at {path} ({source}). Run "
            f"`python scripts/compile_fidelity.py fetch`, or pass --xlsx / set {ENV_VAR}.")
    return path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Fetch into the git-ignored cache
# ─────────────────────────────────────────────────────────────────────────────

def study1_url(ref: str = AUTOTAP_COMMIT) -> str:
    return (f"https://raw.githubusercontent.com/{AUTOTAP_REPO}/{ref}/"
            + urllib.parse.quote(STUDY1_REPO_PATH))


def fetch(ref: str = AUTOTAP_COMMIT, dest: Path = CACHE_FILE,
          urlopen: Callable[..., Any] = urllib.request.urlopen) -> Path:
    """Download Study 1 to ``dest``; verify the pinned checksum when ``ref`` is the pinned commit."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A second, local guard: even if the root .gitignore entry were removed,
    # git would still ignore everything in the cache directory.
    (dest.parent / ".gitignore").write_text("# AutoTap data is not redistributed -- see scripts/compile_fidelity.py\n*\n")
    with urlopen(study1_url(ref), timeout=60) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if ref == AUTOTAP_COMMIT and digest != STUDY1_SHA256:
        raise FidelityError(f"checksum mismatch for {study1_url(ref)}: got {digest}, expected {STUDY1_SHA256}")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# Labels
#
#   {
#     "format": "deviceweave-compile-fidelity-labels/1",
#     "dataset": {"name": "AutoTap Study 1", "sha256": "<of the xlsx>"},
#     "labels": {
#       "study1:Result:row12:stmt3": {
#         "expect": "policy",
#         "policies": [{"scope": {"device_type": "light"},
#                       "conditions": [{"field": "is_home", "operator": "==", "value": false}],
#                       "action": {"type": "block", "params": {}}}],
#         "note": "optional, your own words -- never the participant's"
#       },
#       "study1:Result:row12:stmt4": {"expect": "reject", "reason_category": "unsupported_device"}
#     }
#   }
#
# ``policies`` is a list because a statement can need more than one DSL rule
# (an overnight window, an "or"); the compiler emits one, so such a label
# measures the gap rather than hiding it.
# ─────────────────────────────────────────────────────────────────────────────

LABEL_KEYS = frozenset({"expect", "policies", "reason_category", "note"})
REASON_CATEGORIES = frozenset({
    "unsupported_device", "unsupported_condition", "ambiguous", "low_confidence",
    "unsatisfiable", "other",
})


def _as_validatable(policy: Dict[str, Any]) -> Dict[str, Any]:
    action = dict(policy.get("action") or {})
    action.setdefault("reason", "label")
    action.setdefault("params", {})
    return {"rule_id": "label", "scope": policy.get("scope"), "conditions": policy.get("conditions"),
            "action": action, "confidence": 1.0}


def load_labels(path: Path = LABELS_PATH) -> Dict[str, Dict[str, Any]]:
    """Parse and check the labels file; returns rule id -> label with validated policies."""
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise FidelityError(f"{path}: not valid JSON: {exc}") from exc
    if doc.get("format") != LABELS_FORMAT:
        raise FidelityError(f"{path}: 'format' must be {LABELS_FORMAT!r}")
    labels = doc.get("labels")
    if not isinstance(labels, dict):
        raise FidelityError(f"{path}: 'labels' must be an object keyed by rule id")
    out: Dict[str, Dict[str, Any]] = {}
    for rid, label in labels.items():
        where = f"{path.name}: {rid}"
        if not RULE_ID_RE.match(rid):
            raise FidelityError(f"{where}: rule id must look like 'study1:Result:row12:stmt3'")
        if not isinstance(label, dict):
            raise FidelityError(f"{where}: label must be an object")
        extra = set(label) - LABEL_KEYS
        if extra:
            raise FidelityError(f"{where}: unknown keys {sorted(extra)} -- labels hold expected "
                                f"policies only, never the rule text (allowed: {sorted(LABEL_KEYS)})")
        expect = label.get("expect")
        if expect == "policy":
            policies = label.get("policies")
            if not isinstance(policies, list) or not policies:
                raise FidelityError(f"{where}: expect=policy needs a non-empty 'policies' list")
            checked = []
            for i, p in enumerate(policies):
                try:
                    checked.append(validate_policy(_as_validatable(p)))
                except ValidationError as exc:
                    raise FidelityError(f"{where}: policies[{i}] is not a valid policy: {exc}") from exc
            out[rid] = {**label, "policies": checked}
        elif expect == "reject":
            rc = label.get("reason_category")
            if rc is not None and rc not in REASON_CATEGORIES:
                raise FidelityError(f"{where}: reason_category must be one of {sorted(REASON_CATEGORIES)}")
            out[rid] = dict(label)
        else:
            raise FidelityError(f"{where}: 'expect' must be 'policy' or 'reject'")
    return out


def _normalize_ws(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def labels_leaking_text(rules: Sequence[SourceRule], labels: Dict[str, Dict[str, Any]]) -> List[str]:
    """Rule ids whose label note contains that rule's own text (labels must not)."""
    by_id = {r.rule_id: r for r in rules}
    leaks = []
    for rid, label in labels.items():
        rule = by_id.get(rid)
        if rule is None or not label.get("note"):
            continue
        text = _normalize_ws(rule.text)
        if len(text) >= 12 and text in _normalize_ws(str(label["note"])):
            leaks.append(rid)
    return leaks


# ─────────────────────────────────────────────────────────────────────────────
# Rule patterns
#
# Coarse keyword tags so the report can say *which kinds* of household rule
# compile and which do not (see docs/autotap-gap-analysis.md). They are
# heuristics over English text, good enough to rank gaps, not to grade a rule.
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_DEVICE_WORDS = {
    "fan": r"\bfans?\b", "light": r"\b(lights?|lamps?|lighting|bulbs?)\b",
    "ac": r"\b(ac|a/c|air ?condition(er|ing)?)\b", "plug": r"\b(smart )?plugs?\b|\boutlets?\b",
    "heater": r"\b(space )?heaters?\b|\bheating\b",
}
OTHER_DEVICE_WORDS = (
    r"\b(doors?|locks?|windows?|thermostats?|cameras?|garage|ovens?|stoves?|tv|television|alarms?|"
    r"sensors?|blinds|curtains|shades|speakers?|vacuum|roomba|sprinklers?|washer|dryer|dishwasher|"
    r"fridge|refrigerator|coffee|kettle|doorbell|music|faucet|water heater|irrigation|security)\b"
)
PATTERNS: Dict[str, str] = {
    "time_of_day": r"\b\d{1,2}(:\d\d)? ?(am|pm|a\.m\.|p\.m\.|o'?clock)\b|\b(night|nighttime|morning|evening|"
                   r"afternoon|midnight|noon|bedtime|dark|sunset|sunrise|dusk|dawn)\b",
    "day_or_date": r"\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|weekends?|"
                   r"weekdays?|holidays?|vacation|summer|winter|spring|fall|autumn|season)\b",
    "duration": r"\bfor (more|longer) than\b|\b(more|longer) than \d+ ?(sec|min|hour|hr|day)|"
                r"\bfor \d+ ?(sec|min|hour|hr)|\b\d+ ?(seconds?|minutes?|mins?|hours?|hrs?) (after|later|of)\b|"
                r"\bleft on\b|\bstays? (on|open)\b",
    "periodic": r"\bevery\b|\bdaily\b|\bweekly\b|\beach (day|night|morning|evening|week)\b",
    "presence": r"\b(home|away|leave|leaves|leaving|left the house|arrive|arrives|arriving|nobody|no one|"
                r"empty house|asleep|sleeping|occupied)\b",
    "weather_or_climate": r"\b(rain|raining|snow|snowing|storm|cold|hot|warm|cool|temperature|degrees?|"
                          r"humid|humidity|cloudy|overcast|sunny|weather|freezing)\b",
    "event_trigger": r"\b(opens?|opened|closes?|closed|unlock(s|ed)?|rings?|detects?|detected|motion|"
                     r"arrives?|leaves|turns? (on|off)|is turned (on|off)|starts?|stops?|finishes)\b",
    "sequence_or_delay": r"\bwithin\b|\bafter\b|\bbefore\b|\buntil\b|\bthen\b|\bonce\b",
    "unless_in_text": r"\bunless\b|\bexcept\b|\bexcluding\b|\bother than\b",
    "disjunction": r"\bor\b|\beither\b",
    "notification": r"\b(notify|notification|alert|alerts|text me|send me|remind|warn|message me)\b",
    "setpoint_or_level": r"\b(set (to|at)|above|below|over|under|at least|at most|no (more|higher|lower) than|"
                         r"\d+ ?(degrees|%|percent))\b",
}
_COMPILED_PATTERNS = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
_SUPPORTED = {k: re.compile(v, re.I) for k, v in SUPPORTED_DEVICE_WORDS.items()}
_OTHER = re.compile(OTHER_DEVICE_WORDS, re.I)


def tag_patterns(text: str, category: Optional[str] = None, has_exception: Optional[bool] = None) -> List[str]:
    tags = sorted(k for k, rx in _COMPILED_PATTERNS.items() if rx.search(text))
    supported = sorted(k for k, rx in _SUPPORTED.items() if rx.search(text))
    other = {m.group(0).lower() for m in _OTHER.finditer(text)}
    if supported:
        tags.append("supported_device_mentioned")
    if other:
        tags.append("other_device_mentioned")
    if len(supported) + len(other) >= 2:
        tags.append("multi_device")
    if category:
        tags.append(f"category_{category}")
    if has_exception:
        tags.append("exception_flagged")
    return sorted(set(tags))


# ─────────────────────────────────────────────────────────────────────────────
# Scoring one rule
# ─────────────────────────────────────────────────────────────────────────────

# First match wins. Validator messages are fixed strings (validator.py); model
# refusal reasons are free text, so their categories are keyword guesses.
_VALIDATION_CATEGORIES: Sequence[Tuple[str, str]] = (
    ("low_confidence", r"below the required threshold|confidence"),
    ("invalid_device_type", r"scope\.device_type|'scope'"),
    ("invalid_condition_field", r"conditions\[\d+\]\.field"),
    ("invalid_operator", r"\.operator"),
    ("invalid_value_type", r"\.value for field"),
    ("no_conditions", r"'conditions' must be a non-empty list"),
    ("invalid_action", r"action\.type|'action"),
    ("schema_extra_fields", r"disallowed fields"),
    ("schema_missing_field", r"Missing required field|missing required field"),
)
_REFUSAL_CATEGORIES: Sequence[Tuple[str, str]] = (
    ("unsupported_device", r"device|appliance|not one of|dishwasher|tv|thermostat|lock|door|window|camera"),
    ("unsupported_condition", r"condition|field|sensor|trigger|state|event|duration|schedule|day"),
    ("ambiguous", r"ambigu|unclear|vague|multiple (valid )?interpretations|semantically empty"),
    ("low_confidence", r"confiden"),
    ("empty", r"empty"),
)


def categorize(reason: str, table: Sequence[Tuple[str, str]], default: str) -> str:
    for name, rx in table:
        if re.search(rx, reason, re.I):
            return name
    return default


def _canonical_value(v: Any) -> Any:
    return v if isinstance(v, bool) else float(v)


def canonical(policy: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a policy that carry meaning: no rule id, confidence or reason."""
    conds = sorted(((c["field"], c["operator"], _canonical_value(c["value"])) for c in policy["conditions"]),
                   key=lambda t: (t[0], t[1], str(t[2])))
    return {"device_type": policy["scope"]["device_type"], "action": policy["action"]["type"],
            "params": json.loads(json.dumps(policy["action"].get("params") or {}, sort_keys=True)),
            "conditions": [list(c) for c in conds]}


def compare(compiled: Dict[str, Any], expected: Sequence[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """('match_exact' | 'match_equivalent' | 'mismatch', mismatch kinds)."""
    got = canonical(compiled)
    want = [canonical(p) for p in expected]
    if len(want) == 1 and got == want[0]:
        return "match_exact", []
    kinds: List[str] = []
    groups = {(w["device_type"], w["action"], json.dumps(w["params"], sort_keys=True)) for w in want}
    if len(groups) > 1:
        kinds.append("needs_multiple_policies")
    if all(w["device_type"] != got["device_type"] for w in want):
        kinds.append("device")
    if all(w["action"] != got["action"] for w in want):
        kinds.append("action")
    if all(w["params"] != got["params"] for w in want):
        kinds.append("params")
    if kinds:
        return "mismatch", kinds
    # Same device, action and params: compare meaning. The expected policies
    # are ORed (a context matching any of them), the compiled one is a single
    # rule; equal when the two regions contain exactly the same contexts.
    region = rule_region(compiled)
    union = [box for p in expected for box in rule_region(p)]
    if not region_minus(region, union) and not region_minus(union, region):
        return "match_equivalent", []
    return "mismatch", ["conditions"] + (["needs_multiple_policies"] if len(expected) > 1 else [])


@dataclass
class RuleResult:
    id: str
    cell: str
    category: Optional[str]
    has_exception: Optional[bool]
    patterns: List[str]
    compile: str                        # compiled | refused | infra_error | no_response
    valid: bool = False                 # validator.validate_policy accepted it
    satisfiable: Optional[bool] = None  # rule-set checker: matches at least one context
    accepted: bool = False              # what POST /policies/author would store
    reason: Optional[str] = None        # refusal reason or validation/analysis message
    failure_category: Optional[str] = None
    policy: Optional[Dict[str, Any]] = None
    label: str = "unlabeled"
    mismatch: List[str] = field(default_factory=list)
    reason_category_match: Optional[bool] = None
    text: Optional[str] = None          # only with --include-text


class _ModelError:
    """In-memory marker for a model response that is not a policy. Not written to disk."""

    def __init__(self, kind: str) -> None:
        self.kind = kind


def score_rule(rule: SourceRule, compiled: Any, label: Optional[Dict[str, Any]] = None,
               no_response: bool = False) -> RuleResult:
    res = RuleResult(id=rule.rule_id, cell=rule.cell, category=rule.category,
                     has_exception=rule.has_exception,
                     patterns=tag_patterns(rule.text, rule.category, rule.has_exception), compile="compiled")
    if no_response:
        res.compile, res.failure_category = "no_response", "no_response"
        return res
    if isinstance(compiled, _ModelError):
        res.compile = "model_error"
        res.failure_category = compiled.kind
        if label is not None:
            _apply_label(res, label)
        return res
    if compiled is None:
        # Infrastructure: the model was not asked, or the call never returned.
        # Leave the row unlabeled so a throttle is not a compile failure.
        res.compile, res.failure_category = "infra_error", "infra_error"
        return res
    elif isinstance(compiled, dict) and compiled.get("rejected") is True:
        res.compile = "refused"
        res.reason = str(compiled.get("reason") or "")
        res.failure_category = "refused_" + categorize(res.reason, _REFUSAL_CATEGORIES, "other")
    else:
        try:
            # Round-trip through JSON so the validator sees what the API would.
            policy = validate_policy(json.loads(json.dumps(compiled)))
        except (ValidationError, TypeError, ValueError) as exc:
            res.reason = str(exc)
            res.failure_category = "invalid_" + categorize(res.reason, _VALIDATION_CATEGORIES, "other").replace("invalid_", "")
        else:
            res.valid = True
            res.policy = {k: policy[k] for k in ("scope", "conditions", "action")}
            errors = [f for f in check_new_rule(policy, []) if f.severity == "error"]
            res.satisfiable = not any(f.kind == "unsatisfiable" for f in errors)
            if errors:
                res.reason = errors[0].message
                res.failure_category = errors[0].kind
            else:
                res.accepted = True
    if label is not None:
        _apply_label(res, label)
    return res


def _apply_label(res: RuleResult, label: Dict[str, Any]) -> None:
    if label["expect"] == "reject":
        res.label = "reject_expected_but_compiled" if res.accepted else "reject_expected_ok"
        rc = label.get("reason_category")
        if rc and not res.accepted and res.failure_category:
            res.reason_category_match = res.failure_category.endswith(rc)
        return
    if not res.accepted or res.policy is None:
        res.label = "policy_expected_but_refused"
        return
    res.label, res.mismatch = compare({**res.policy, "rule_id": res.id}, label["policies"])


# ─────────────────────────────────────────────────────────────────────────────
# Running and reporting
# ─────────────────────────────────────────────────────────────────────────────

_NO_RESPONSE = object()


def evaluate(rules: Sequence[SourceRule], compile_fn: Callable[[str, str], Any],
             labels: Optional[Dict[str, Dict[str, Any]]] = None,
             include_text: bool = False) -> Dict[str, Any]:
    """``compile_fn(rule_id, text)`` returns the compiler's raw output (or ``_NO_RESPONSE``)."""
    labels = labels or {}
    results: List[RuleResult] = []
    for rule in rules:
        out = compile_fn(rule.rule_id, rule.text)
        res = score_rule(rule, None if out is _NO_RESPONSE else out, labels.get(rule.rule_id),
                         no_response=out is _NO_RESPONSE)
        if include_text:
            res.text = rule.text
        results.append(res)
    return {"summary": summarize(results), "rows": [asdict(r) for r in results]}


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 4) if d else None


def _group(results: Iterable[RuleResult]) -> Dict[str, Any]:
    # infra_error is a transport outcome, not a model decision.
    rs = [r for r in results if r.compile not in ("no_response", "infra_error")]
    return {"n": len(rs), "accepted": sum(r.accepted for r in rs),
            "acceptanceRate": _rate(sum(r.accepted for r in rs), len(rs))}


def summarize(results: Sequence[RuleResult]) -> Dict[str, Any]:
    scored = [r for r in results if r.compile != "no_response"]
    n = len(scored)
    infra = [r for r in scored if r.compile == "infra_error"]
    model = [r for r in scored if r.compile != "infra_error"]
    model_n = len(model)
    compiled = [r for r in model if r.compile == "compiled"]
    labels = Counter(r.label for r in results)
    labeled_policy = [r for r in model if r.label in ("match_exact", "match_equivalent", "mismatch",
                                                       "policy_expected_but_refused")]
    labeled_reject = [r for r in model if r.label in ("reject_expected_ok", "reject_expected_but_compiled")]
    by_pattern: Dict[str, List[RuleResult]] = {}
    for r in model:
        for p in r.patterns:
            by_pattern.setdefault(p, []).append(r)
    return {
        "n": n,
        "noResponse": len(results) - n,
        "infraError": len(infra),
        "modelN": model_n,
        "compile": dict(Counter(r.compile for r in scored)),
        "validatorAccepted": sum(r.valid for r in scored),
        "unsatisfiable": sum(r.satisfiable is False for r in scored),
        "accepted": sum(r.accepted for r in scored),
        "rates": {
            # Denominator is model responses only. An exhausted retry is not a
            # compile failure and does not move these rates.
            "compiled": _rate(len(compiled), model_n),
            "valid": _rate(sum(r.valid for r in model), model_n),
            "accepted": _rate(sum(r.accepted for r in model), model_n),
            "validGivenCompiled": _rate(sum(r.valid for r in compiled), len(compiled)),
        },
        "failureCategories": dict(Counter(
            r.failure_category for r in model if r.failure_category).most_common()),
        "labels": {
            "counts": dict(labels),
            "labeled": n - labels.get("unlabeled", 0) if n else 0,
            # Of rules labelled with an expected policy:
            "exactRate": _rate(labels.get("match_exact", 0), len(labeled_policy)),
            "equivalentRate": _rate(labels.get("match_exact", 0) + labels.get("match_equivalent", 0),
                                    len(labeled_policy)),
            # Of rules labelled as out of scope, how many were correctly refused:
            "rejectAgreement": _rate(labels.get("reject_expected_ok", 0), len(labeled_reject)),
            "mismatchKinds": dict(Counter(k for r in scored for k in r.mismatch)),
        },
        "byCategory": {c: _group(r for r in scored if r.category == c) for c in ("always", "never")},
        "byPattern": {p: _group(rs) for p, rs in sorted(by_pattern.items())},
        "compiledDeviceTypes": dict(Counter(r.policy["scope"]["device_type"] for r in scored if r.policy)),
        "compiledActionTypes": dict(Counter(r.policy["action"]["type"] for r in scored if r.policy)),
    }


def format_table(summary: Dict[str, Any]) -> str:
    def pct(x: Optional[float]) -> str:
        return "  n/a" if x is None else f"{x * 100:5.1f}%"

    extra = ""
    if summary["noResponse"]:
        extra += f" ({summary['noResponse']} without a saved response)"
    if summary.get("infraError"):
        extra += f" ({summary['infraError']} infra errors excluded from rates)"
    lines = [f"Compile fidelity -- {summary['n']} rules scored" + extra, ""]
    rates = summary["rates"]
    lines += [f"  {'compiled (not refused)':<34}{pct(rates['compiled'])}",
              f"  {'valid (validator)':<34}{pct(rates['valid'])}",
              f"  {'accepted (validator + rule-set)':<34}{pct(rates['accepted'])}",
              f"  {'unsatisfiable':<34}{summary['unsatisfiable']:>6}", ""]
    lab = summary["labels"]
    lines += [f"  {'labeled':<34}{lab['labeled']:>6}",
              f"  {'exact match (of policy labels)':<34}{pct(lab['exactRate'])}",
              f"  {'equivalent (of policy labels)':<34}{pct(lab['equivalentRate'])}",
              f"  {'refused as expected (rejects)':<34}{pct(lab['rejectAgreement'])}", ""]
    lines.append("  failure category                    count")
    for cat, count in summary["failureCategories"].items():
        lines.append(f"  {cat:<34}{count:>6}")
    lines += ["", "  pattern                         n   accepted"]
    for name, g in sorted(summary["byPattern"].items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"  {name:<28}{g['n']:>5}   {pct(g['acceptanceRate'])}")
    return "\n".join(lines) + "\n"


def assert_ignored(path: Path) -> None:
    """Refuse to write reports into a tracked part of the repository."""
    path = path.resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        return  # outside the repository
    for safe in (OUT_DIR.resolve(), CACHE_DIR.resolve()):
        if path == safe or safe in path.parents:
            return
    probe = path / "probe.json" if path.suffix == "" else path
    try:
        ok = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(probe)],
                            capture_output=True).returncode == 0
    except OSError:
        ok = False
    if not ok:
        raise FidelityError(f"{path} is inside the repository and not git-ignored; reports can carry "
                            f"compiler output derived from participant text. Use {OUT_DIR} or a path "
                            f"outside the repository.")


def write_report(result: Dict[str, Any], out_dir: Path, meta: Dict[str, Any]) -> Dict[str, Path]:
    assert_ignored(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"meta": meta, **result["summary"]}
    paths = {"summary": out_dir / "summary.json", "rows": out_dir / "rows.jsonl", "table": out_dir / "report.txt"}
    paths["summary"].write_text(json.dumps(summary, indent=2) + "\n")
    paths["rows"].write_text("".join(json.dumps(r) + "\n" for r in result["rows"]))
    paths["table"].write_text(format_table(result["summary"]))
    return paths


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

# Transient Bedrock failures. Matched on the exception class name and, for
# botocore ClientError, on response["Error"]["Code"], so tests can raise a
# same-named exception without importing botocore.
LIVE_MAX_ATTEMPTS = 6
LIVE_BACKOFF_BASE_S = 0.5
LIVE_BACKOFF_CAP_S = 20.0
_RETRYABLE_NAMES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ServiceUnavailable",
    "ModelTimeoutException",
    "ModelNotReadyException",
    "TimeoutError",
    "ReadTimeoutError",
    "ConnectTimeoutError",
    "EndpointConnectionError",
    "ConnectionClosedError",
    "RequestTimeout",
    "RequestTimeoutException",
})


class InfraExhausted(Exception):
    """A transient provider error that used every retry. Not a model failure."""

    def __init__(self, last: BaseException, attempts: int) -> None:
        super().__init__(f"{type(last).__name__} after {attempts} attempts")
        self.last = last
        self.attempts = attempts


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        return str(code or "")
    return ""


def is_transient(exc: BaseException) -> bool:
    """Throttling, service-unavailable, and timeouts. Other errors are not retried."""
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (TimeoutError, ConnectionError)):
            return True
        name = type(cur).__name__
        if name in _RETRYABLE_NAMES or "Timeout" in name:
            return True
        if _error_code(cur) in _RETRYABLE_NAMES:
            return True
        cur = cur.__cause__
    return False


def backoff_seconds(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """Full jitter in ``[0, min(cap, base * 2**attempt)]``. ``attempt`` is 0-based."""
    cap = min(LIVE_BACKOFF_CAP_S, LIVE_BACKOFF_BASE_S * (2 ** attempt))
    return rng() * cap


def call_with_retries(fn: Callable[[], Any], *, max_attempts: int = LIVE_MAX_ATTEMPTS,
                      sleep: Callable[[float], None] = time.sleep,
                      rng: Callable[[], float] = random.random) -> Any:
    """Call ``fn`` until it returns or a non-transient error escapes.

    Transient errors sleep and retry. When the attempt budget is spent, raise
    ``InfraExhausted`` so the caller can record an infra_error and move on.
    """
    last: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc):
                raise
            last = exc
            if attempt == max_attempts - 1:
                raise InfraExhausted(exc, max_attempts) from exc
            sleep(backoff_seconds(attempt, rng))
    raise InfraExhausted(last or RuntimeError("no attempt"), max_attempts)


def _is_envelope(compiled: Any) -> bool:
    return (isinstance(compiled, dict) and "error_class" in compiled
            and "parsed" in compiled and "raw" in compiled)


def record_succeeded(row: Dict[str, Any]) -> bool:
    """True when the model returned something. An infra_error is not done."""
    compiled = row.get("compiled")
    if _is_envelope(compiled):
        return compiled.get("error_class") != "infra_error"
    return compiled is not None


def _compiled_for_scoring(row: Dict[str, Any]) -> Any:
    compiled = row.get("compiled")
    if _is_envelope(compiled):
        if compiled.get("error_class") == "infra_error":
            return None
        if compiled.get("error_class"):
            return _ModelError(str(compiled["error_class"]))
        return compiled.get("parsed")
    return compiled


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if line.strip():
            yield json.loads(line)


def completed_pairs(path: Path) -> set:
    """(id, rep) pairs that already have a successful model response."""
    done = set()
    for row in _iter_jsonl(path):
        if "id" not in row:
            continue
        if record_succeeded(row):
            done.add((row["id"], int(row.get("rep", 1))))
    return done


def harness_git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def usage_totals(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    incoming = outgoing = 0
    for rec in records:
        usage = rec.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        if usage.get("input_tokens") is not None:
            incoming += int(usage["input_tokens"])
        if usage.get("output_tokens") is not None:
            outgoing += int(usage["output_tokens"])
    return {"input_tokens": incoming, "output_tokens": outgoing}


def latest_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Last line per (id, rep). A later success replaces an earlier infra_error."""
    order: List[Tuple[str, int]] = []
    by: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for rec in records:
        key = (rec["id"], int(rec.get("rep", 1)))
        if key not in by:
            order.append(key)
        by[key] = rec
    return [by[key] for key in order]


def estimate_live_cost(rules: Sequence[SourceRule], *, reps: int, max_tokens: int,
                       input_usd_per_million: float, output_usd_per_million: float,
                       assumed_output_tokens: Optional[int] = None) -> Dict[str, Any]:
    """Token and dollar estimate with no network call and no tokenizer.

    Input tokens are ``ceil(character_count / 4)`` of the system prompt plus
    the filled user message, summed over calls. Output tokens are
    ``assumed_output_tokens`` per call when that is passed, otherwise
    ``max_tokens`` per call as a ceiling, not a measured completion.
    """
    from policy_authoring.llm_compiler import compiler_system_prompt, render_user_message

    system = compiler_system_prompt()
    per_rule = [(len(system) + len(render_user_message(rule.text)) + 3) // 4 for rule in rules]
    calls = len(rules) * reps
    input_tokens = sum(per_rule) * reps
    if assumed_output_tokens is None:
        output_tokens = calls * max_tokens
        output_basis = "ceiling: max_tokens per call, not a measured completion"
    else:
        output_tokens = calls * assumed_output_tokens
        output_basis = "assumed_output_tokens per call"
    input_usd = input_tokens / 1_000_000 * input_usd_per_million
    output_usd = output_tokens / 1_000_000 * output_usd_per_million
    return {
        "calls": calls,
        "rules": len(rules),
        "reps": reps,
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "estimation_method": (
            "ceil(character_count/4) of the system prompt plus the filled user "
            "message, summed over calls. Character heuristic only: no tokenizer "
            "is loaded and no network call is made."
        ),
        "output_token_basis": output_basis,
        "input_usd_per_million": input_usd_per_million,
        "output_usd_per_million": output_usd_per_million,
        "input_usd": input_usd,
        "output_usd": output_usd,
        "total_usd": input_usd + output_usd,
        "max_tokens": max_tokens,
    }


def open_live_provider(provider: Optional[str], model_id: Optional[str]) -> Any:
    """Open the live-run provider.

    ``provider="bedrock"`` forces Bedrock. Construction failure is raised
    here and is never turned into a Gemini client.
    """
    from llm_provider import get_llm_provider

    try:
        if provider == "bedrock":
            return get_llm_provider("bedrock", model_id=model_id, reuse_client=True)
        if provider is None:
            llm = get_llm_provider()
            if getattr(llm, "provider_name", None) == "bedrock":
                return get_llm_provider(
                    "bedrock",
                    model_id=model_id or getattr(llm, "model_id", None),
                    reuse_client=True,
                )
            return llm
        raise FidelityError(
            f"--provider {provider} is not supported for the live benchmark; pass bedrock")
    except FidelityError:
        raise
    except Exception as exc:
        raise FidelityError(
            f"provider is unavailable ({type(exc).__name__}: {exc}). "
            f"Refusing to fall back to another provider."
        ) from exc


def _provider_call(provider: Any, system: str, user: str, max_tokens: int,
                   temperature: Optional[float]) -> Tuple[Any, int]:
    from llm_provider.base import LLMResult

    kwargs: Dict[str, Any] = {"max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    started = time.perf_counter()
    if hasattr(provider, "invoke_result"):
        result = provider.invoke_result(system, user, **kwargs)
    else:
        text = provider.invoke(system, user, **kwargs)
        usage = getattr(provider, "last_usage", None)
        result = LLMResult(text=text, usage=usage if isinstance(usage, dict) else None)
    latency_ms = int(round((time.perf_counter() - started) * 1000))
    return result, latency_ms


def execute_live(rules: Sequence[SourceRule], provider: Any, *, reps: int, concurrency: int,
                 out_path: Path, resume: bool, temperature: Optional[float], model_id: str,
                 provider_name: str, max_tokens: int,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random,
                 max_attempts: int = LIVE_MAX_ATTEMPTS) -> Dict[str, Any]:
    """Compile ``rules`` × ``reps`` and append one JSONL record per finished call.

    Records are keyed by id. The statement text is not written. Resume skips
    (id, rep) pairs that already succeeded and retries infra_error pairs.
    """
    from policy_authoring.llm_compiler import (
        compiler_system_prompt, parse_model_output, prompt_hash, render_user_message,
    )

    assert_ignored(out_path.parent if out_path.suffix else out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    system = compiler_system_prompt()
    phash = prompt_hash()
    done = completed_pairs(out_path) if resume else set()
    pending = [(rule, rep) for rule in rules for rep in range(1, reps + 1)
               if (rule.rule_id, rep) not in done]
    lock = threading.Lock()
    fh = open(out_path, "a")

    def write_record(record: Dict[str, Any]) -> None:
        line = json.dumps(record) + "\n"
        with lock:
            fh.write(line)
            fh.flush()

    def one(rule: SourceRule, rep: int) -> None:
        user = render_user_message(rule.text)
        latency_ms = 0

        def attempt() -> Any:
            nonlocal latency_ms
            result, latency_ms = _provider_call(provider, system, user, max_tokens, temperature)
            return result

        try:
            result = call_with_retries(attempt, max_attempts=max_attempts, sleep=sleep, rng=rng)
        except InfraExhausted:
            write_record({
                "id": rule.rule_id,
                "rep": rep,
                "model_id": model_id,
                "provider": provider_name,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "prompt_hash": phash,
                "compiled": {"raw": None, "parsed": None, "error_class": "infra_error"},
                "usage": None,
                "latency_ms": latency_ms,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return
        raw_text = getattr(result, "text", None)
        usage = getattr(result, "usage", None)
        try:
            parsed: Any = parse_model_output(raw_text or "")
            error_class = None
        except json.JSONDecodeError:
            parsed = None
            error_class = "json_decode"
        write_record({
            "id": rule.rule_id,
            "rep": rep,
            "model_id": model_id,
            "provider": provider_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_hash": phash,
            "compiled": {"raw": raw_text, "parsed": parsed, "error_class": error_class},
            "usage": usage if isinstance(usage, dict) else None,
            "latency_ms": latency_ms,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    try:
        if not pending:
            pass
        elif concurrency <= 1:
            for rule, rep in pending:
                one(rule, rep)
        else:
            workers = min(concurrency, len(pending))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(one, rule, rep) for rule, rep in pending]
                for fut in as_completed(futures):
                    fut.result()
    finally:
        fh.close()

    all_records = list(_iter_jsonl(out_path))
    wanted = {rule.rule_id for rule in rules}
    latest = [rec for rec in latest_records(all_records) if rec["id"] in wanted]
    return {
        "latest": latest,
        "all_records": all_records,
        "prompt_hash": phash,
        "usage": usage_totals(all_records),
        "calls_this_run": len(pending),
    }


def score_live_records(rules: Sequence[SourceRule], records: Sequence[Dict[str, Any]],
                       labels: Optional[Dict[str, Dict[str, Any]]] = None,
                       include_text: bool = False, reps: int = 1) -> Dict[str, Any]:
    """Score the latest record per (id, rep). infra_error is not a model failure.

    A pair with no record is ``no_response`` and is left out of the rates.
    """
    labels = labels or {}
    by_key = {(rec["id"], int(rec.get("rep", 1))): rec for rec in records}
    results: List[RuleResult] = []
    rows: List[Dict[str, Any]] = []
    for rule in rules:
        for rep in range(1, reps + 1):
            rec = by_key.get((rule.rule_id, rep))
            if rec is None:
                res = score_rule(rule, None, None, no_response=True)
            else:
                res = score_rule(rule, _compiled_for_scoring(rec), labels.get(rule.rule_id))
            if include_text:
                res.text = rule.text
            results.append(res)
            row = asdict(res)
            row["rep"] = rep
            rows.append(row)
    return {"summary": summarize(results), "rows": rows}


def _responses_fn(path: Path) -> Callable[[str, str], Any]:
    saved: Dict[str, Any] = {}
    success: Dict[str, Any] = {}
    for row in _iter_jsonl(path):
        rid = row["id"]
        value = _compiled_for_scoring(row)
        saved[rid] = value
        if record_succeeded(row):
            success[rid] = value
    saved.update(success)

    def run(rid: str, _text: str) -> Any:
        if rid not in saved:
            return _NO_RESPONSE
        return saved[rid]
    return run


def _policies_for_enforceability(policies_path: Optional[str], responses_path: Optional[str]) -> List[Any]:
    """Compiled policies from a JSON list and/or a JSONL of ``{id, compiled}``.

    A live-run envelope contributes its parsed policy. An ``infra_error``
    envelope is left out, so a throttle is not counted as "not compiled".
    A missing or null ``compiled`` value counts as not compiled. This does
    not read the Study 1 workbook.
    """
    if not policies_path and not responses_path:
        raise FidelityError("enforceability needs --policies FILE or --responses FILE (offline; no LLM)")
    items: List[Any] = []
    if policies_path:
        path = Path(policies_path)
        if not path.is_file():
            raise FidelityError(f"policies file not found: {path}")
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = data.get("policies", data.get("compiled"))
        if not isinstance(data, list):
            raise FidelityError("--policies must be a JSON list of compiled policies")
        items.extend(data)
    if responses_path:
        path = Path(responses_path)
        if not path.is_file():
            raise FidelityError(f"responses file not found: {path}")
        for row in _iter_jsonl(path):
            compiled = row.get("compiled")
            if _is_envelope(compiled):
                if compiled.get("error_class") == "infra_error":
                    continue
                parsed = compiled.get("parsed")
                items.append({"rejected": True} if parsed is None else parsed)
            else:
                items.append({"rejected": True} if compiled is None else compiled)
    return items


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download Study 1 into the git-ignored cache")
    f.add_argument("--ref", default=AUTOTAP_COMMIT, help="AutoTap git ref (default: pinned commit)")
    f.add_argument("--dest", default=str(CACHE_FILE))
    for name in ("list", "run"):
        p = sub.add_parser(name)
        p.add_argument("--xlsx", default=None, help=f"path to the Study 1 .xlsx (else ${ENV_VAR}, else the cache)")
        p.add_argument("--sheet", action="append", default=None,
                       help="sheet to read (repeatable; default: Result)")
        p.add_argument("--limit", type=int, default=None)
    r = sub.choices["run"]
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--responses", default=None, help="JSONL of {id, compiled} to score (offline)")
    mode.add_argument("--live", action="store_true",
                      help="call the LLM provider (costs money; sends rule text to it)")
    mode.add_argument("--dry-run-cost", action="store_true",
                      help="print call count, estimated tokens, and cost; no network call")
    r.add_argument("--provider", choices=("bedrock",), default=None,
                   help="force this provider. bedrock does not fall back to Gemini")
    r.add_argument("--model-id", default=None,
                   help="Bedrock model id (default: LLM_MODEL_ID or Claude Haiku 4.5)")
    r.add_argument("--temperature", type=float, default=0.0,
                   help="live-run temperature (default 0). The authoring compiler omits "
                        "temperature unless it is passed in")
    r.add_argument("--reps", type=int, default=1, help="repetitions per statement (paper run: 5)")
    r.add_argument("--concurrency", type=int, default=1,
                   help="worker threads. Bedrock uses one shared client")
    r.add_argument("--resume", action="store_true",
                   help="skip (id, rep) pairs that already succeeded; retry infra_error")
    r.add_argument("--input-usd-per-million", type=float, default=None,
                   help="with --dry-run-cost: input price in USD per million tokens")
    r.add_argument("--output-usd-per-million", type=float, default=None,
                   help="with --dry-run-cost: output price in USD per million tokens")
    r.add_argument("--assumed-output-tokens", type=int, default=None,
                   help="with --dry-run-cost: output tokens per call (default: max_tokens ceiling)")
    r.add_argument("--save-responses", default=None,
                   help=f"with --live: append raw outputs here (default {OUT_DIR / 'responses.jsonl'})")
    r.add_argument("--labels", default=str(LABELS_PATH))
    r.add_argument("--out-dir", "--out", dest="out_dir", default=str(OUT_DIR),
                   help="report directory (default: benchmarks/autotap/_out, git-ignored)")
    r.add_argument("--include-text", action="store_true",
                   help="copy each rule's text into rows.jsonl (stays in the git-ignored output dir)")
    e = sub.add_parser(
        "enforceability",
        help="classify compiled policies offline (no workbook, no model)",
    )
    e.add_argument("--policies", default=None,
                   help="JSON list of compiled policies (or {policies: [...]})")
    e.add_argument("--responses", default=None,
                   help="JSONL of {id, compiled}; null compiled counts as not compiled")
    e.add_argument("--manual-on", action="store_true",
                   help="assume an uncontrollable physical switch-on in every context")
    e.add_argument("--precursor", action="store_true",
                   help="assume an uncontrollable depart event between home and away")
    e.add_argument("--detail", choices=("summary", "full"), default="summary",
                   help="summary is class shares; full adds each policy's supervisor listing")
    e.add_argument("--max-states", type=int, default=None,
                   help="optional plant-size cap (default: none). Authoring uses its own smaller cap")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "fetch":
            print(NOTICE, file=sys.stderr)
            dest = fetch(args.ref, Path(args.dest))
            print(f"saved {dest} (sha256 {sha256_of(dest)})")
            return 0

        if args.cmd == "enforceability":
            from policy_authoring.controllability import benchmark_enforceability
            policies = _policies_for_enforceability(args.policies, args.responses)
            report = benchmark_enforceability(
                policies, manual_on=args.manual_on, precursor=args.precursor,
                max_states=args.max_states, detail=args.detail)
            print("caller-supplied policies; not an AutoTap Study 1 result", file=sys.stderr)
            print(json.dumps(report, indent=2))
            return 0

        path = resolve_xlsx_path(args.xlsx)
        rules = load_rules(path, args.sheet or DEFAULT_SHEETS)
        if args.limit:
            rules = rules[: args.limit]
        digest = sha256_of(path)
        if digest != STUDY1_SHA256:
            print(f"warning: {path.name} sha256 {digest} differs from the pinned AutoTap file; "
                  f"rule ids may point at different statements than the labels expect", file=sys.stderr)

        if args.cmd == "list":
            print(json.dumps({
                "file": path.name, "sha256": digest, "rules": len(rules),
                "participants": len({(r.sheet, r.row) for r in rules}),
                "byCategory": dict(Counter(r.category for r in rules)),
                "withException": sum(bool(r.has_exception) for r in rules),
                "patterns": dict(Counter(p for r in rules for p in tag_patterns(r.text, r.category, r.has_exception)).most_common()),
            }, indent=2))
            return 0

        if args.reps < 1 or args.concurrency < 1:
            raise FidelityError("--reps and --concurrency must be >= 1")

        if args.dry_run_cost:
            if args.input_usd_per_million is None or args.output_usd_per_million is None:
                raise FidelityError(
                    "pass --input-usd-per-million and --output-usd-per-million "
                    "(no prices are built in)")
            from policy_authoring.llm_compiler import DEFAULT_MAX_TOKENS
            print(json.dumps(estimate_live_cost(
                rules, reps=args.reps, max_tokens=DEFAULT_MAX_TOKENS,
                input_usd_per_million=args.input_usd_per_million,
                output_usd_per_million=args.output_usd_per_million,
                assumed_output_tokens=args.assumed_output_tokens,
            ), indent=2))
            return 0

        labels = load_labels(Path(args.labels))
        leaks = labels_leaking_text(rules, labels)
        if leaks:
            raise FidelityError(f"label notes contain the rule's own text: {leaks}; reword them")
        if args.responses:
            compile_fn, compiler = _responses_fn(Path(args.responses)), f"responses:{Path(args.responses).name}"
            result = evaluate(rules, compile_fn, labels, include_text=args.include_text)
            meta = {"dataset": "AutoTap Study 1", "file": path.name, "sha256": digest,
                    "sheets": list(args.sheet or DEFAULT_SHEETS), "compiler": compiler,
                    "labelsFile": Path(args.labels).name, "notice": NOTICE}
        elif args.live:
            from policy_authoring.llm_compiler import DEFAULT_MAX_TOKENS
            save = Path(args.save_responses) if args.save_responses else Path(args.out_dir) / "responses.jsonl"
            provider = open_live_provider(args.provider, args.model_id)
            if args.provider == "bedrock" and getattr(provider, "provider_name", None) not in (None, "bedrock"):
                raise FidelityError(
                    f"refusing to run: got {getattr(provider, 'provider_name', type(provider).__name__)}, "
                    f"not bedrock")
            model_id = args.model_id or getattr(provider, "model_id", None) or os.environ.get(
                "LLM_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
            provider_name = getattr(provider, "provider_name", None) or (args.provider or "unknown")
            print(f"LIVE: {len(rules)} rules x {args.reps} reps, provider={provider_name}, "
                  f"model={model_id}, temperature={args.temperature}, concurrency={args.concurrency}. "
                  f"This calls a paid model and sends each rule's text to that provider. "
                  f"Records are appended to {save}.", file=sys.stderr)
            if not args.resume and save.is_file() and save.stat().st_size:
                print(f"warning: {save} already has records and --resume was not set; "
                      f"those pairs will be called again", file=sys.stderr)
            try:
                live = execute_live(
                    rules, provider, reps=args.reps, concurrency=args.concurrency, out_path=save,
                    resume=args.resume, temperature=args.temperature, model_id=model_id,
                    provider_name=provider_name, max_tokens=DEFAULT_MAX_TOKENS,
                )
            except FidelityError:
                raise
            except Exception as exc:
                raise FidelityError(
                    f"live run stopped on {type(exc).__name__}. Not recorded as a compile failure. "
                    f"Re-run with --resume after the provider is available."
                ) from exc
            result = score_live_records(
                rules, live["latest"], labels, include_text=args.include_text, reps=args.reps)
            meta = {"dataset": "AutoTap Study 1", "file": path.name, "sha256": digest,
                    "sheets": list(args.sheet or DEFAULT_SHEETS),
                    "compiler": f"live:{provider_name}:{model_id}",
                    "model_id": model_id, "provider": provider_name,
                    "temperature": args.temperature, "reps": args.reps,
                    "prompt_hash": live["prompt_hash"],
                    "harness_git_commit": harness_git_commit(),
                    "max_tokens": DEFAULT_MAX_TOKENS,
                    "usage": live["usage"],
                    "labelsFile": Path(args.labels).name, "notice": NOTICE}
        else:
            raise FidelityError(
                "choose --responses FILE (offline), --live (calls the LLM provider), "
                "or --dry-run-cost")

        paths = write_report(result, Path(args.out_dir), meta)
        print(format_table(result["summary"]))
        print(f"wrote {', '.join(str(p) for p in paths.values())}")
        return 0
    except FidelityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
