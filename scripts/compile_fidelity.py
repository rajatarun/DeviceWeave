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
            --live            call the configured LLM provider through
                              llm_compiler.compile_rule (opt-in: costs money
                              and sends participant text to that provider)

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
  LLM_PROVIDER=bedrock python scripts/compile_fidelity.py run --live --limit 20

The spreadsheet path is ``--xlsx PATH``, else ``$AUTOTAP_STUDY1_XLSX``, else
the cache file ``fetch`` writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
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


def score_rule(rule: SourceRule, compiled: Any, label: Optional[Dict[str, Any]] = None,
               no_response: bool = False) -> RuleResult:
    res = RuleResult(id=rule.rule_id, cell=rule.cell, category=rule.category,
                     has_exception=rule.has_exception,
                     patterns=tag_patterns(rule.text, rule.category, rule.has_exception), compile="compiled")
    if no_response:
        res.compile, res.failure_category = "no_response", "no_response"
        return res
    if compiled is None:
        res.compile, res.failure_category = "infra_error", "infra_error"
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
    rs = [r for r in results if r.compile != "no_response"]
    return {"n": len(rs), "accepted": sum(r.accepted for r in rs),
            "acceptanceRate": _rate(sum(r.accepted for r in rs), len(rs))}


def summarize(results: Sequence[RuleResult]) -> Dict[str, Any]:
    scored = [r for r in results if r.compile != "no_response"]
    n = len(scored)
    compiled = [r for r in scored if r.compile == "compiled"]
    labels = Counter(r.label for r in results)
    labeled_policy = [r for r in scored if r.label in ("match_exact", "match_equivalent", "mismatch",
                                                         "policy_expected_but_refused")]
    labeled_reject = [r for r in scored if r.label in ("reject_expected_ok", "reject_expected_but_compiled")]
    by_pattern: Dict[str, List[RuleResult]] = {}
    for r in scored:
        for p in r.patterns:
            by_pattern.setdefault(p, []).append(r)
    return {
        "n": n,
        "noResponse": len(results) - n,
        "compile": dict(Counter(r.compile for r in scored)),
        "validatorAccepted": sum(r.valid for r in scored),
        "unsatisfiable": sum(r.satisfiable is False for r in scored),
        "accepted": sum(r.accepted for r in scored),
        "rates": {
            "compiled": _rate(len(compiled), n),
            "valid": _rate(sum(r.valid for r in scored), n),
            "accepted": _rate(sum(r.accepted for r in scored), n),
            "validGivenCompiled": _rate(sum(r.valid for r in compiled), len(compiled)),
        },
        "failureCategories": dict(Counter(r.failure_category for r in scored if r.failure_category).most_common()),
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

    lines = [f"Compile fidelity -- {summary['n']} rules scored"
             + (f" ({summary['noResponse']} without a saved response)" if summary["noResponse"] else ""), ""]
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

def _responses_fn(path: Path) -> Callable[[str, str], Any]:
    saved: Dict[str, Any] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            saved[row["id"]] = row.get("compiled")
    return lambda rid, _text: saved.get(rid, _NO_RESPONSE) if rid in saved else _NO_RESPONSE


def _live_fn(save_to: Optional[Path]) -> Callable[[str, str], Any]:
    from policy_authoring.llm_compiler import compile_rule

    fh = None
    if save_to is not None:
        assert_ignored(save_to.parent)
        save_to.parent.mkdir(parents=True, exist_ok=True)
        fh = open(save_to, "a")

    def run(rid: str, text: str) -> Any:
        out = compile_rule(text)
        if fh is not None:
            fh.write(json.dumps({"id": rid, "compiled": out}) + "\n")
            fh.flush()
        return out
    return run


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
                      help="call the configured LLM provider (costs money; sends rule text to it)")
    r.add_argument("--save-responses", default=None,
                   help=f"with --live: append raw outputs here (default {OUT_DIR / 'responses.jsonl'})")
    r.add_argument("--labels", default=str(LABELS_PATH))
    r.add_argument("--out-dir", default=str(OUT_DIR))
    r.add_argument("--include-text", action="store_true",
                   help="copy each rule's text into rows.jsonl (stays in the git-ignored output dir)")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "fetch":
            print(NOTICE, file=sys.stderr)
            dest = fetch(args.ref, Path(args.dest))
            print(f"saved {dest} (sha256 {sha256_of(dest)})")
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

        labels = load_labels(Path(args.labels))
        leaks = labels_leaking_text(rules, labels)
        if leaks:
            raise FidelityError(f"label notes contain the rule's own text: {leaks}; reword them")
        if args.responses:
            compile_fn, compiler = _responses_fn(Path(args.responses)), f"responses:{Path(args.responses).name}"
        elif args.live:
            save = Path(args.save_responses) if args.save_responses else OUT_DIR / "responses.jsonl"
            print(f"LIVE: compiling {len(rules)} rules with LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'auto')}. "
                  f"This calls a paid model and sends each rule's text to that provider. "
                  f"Raw outputs are appended to {save}.", file=sys.stderr)
            compile_fn, compiler = _live_fn(save), f"live:{os.environ.get('LLM_PROVIDER', 'auto')}"
        else:
            raise FidelityError("choose --responses FILE (offline) or --live (calls the LLM provider)")

        result = evaluate(rules, compile_fn, labels, include_text=args.include_text)
        meta = {"dataset": "AutoTap Study 1", "file": path.name, "sha256": digest,
                "sheets": list(args.sheet or DEFAULT_SHEETS), "compiler": compiler,
                "labelsFile": Path(args.labels).name, "notice": NOTICE}
        paths = write_report(result, Path(args.out_dir), meta)
        print(format_table(result["summary"]))
        print(f"wrote {', '.join(str(p) for p in paths.values())}")
        return 0
    except FidelityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
