"""
Compile-fidelity harness for natural-language household rules.

Measures how faithfully ``policy_authoring.llm_compiler.compile_rule`` turns a
sentence into the Policy DSL, then checks that object with
``policy_authoring.validator.validate_policy``. When a label exists, the
compiled policy is compared to the label both exactly and structurally
(``rule_set_checker.rule_region``: same contexts, so ``time_hour >= 22`` and
``time_hour > 21`` match structurally and differ exactly).

The intended corpus is AutoTap Study 1 (Weijia He et al.; author list Lefan
Zhang, Weijia He, Jesse Martinez, Noah Brackenbury, Shan Lu, and Blase Ur),
"AutoTap: Synthesizing and Repairing Trigger-Action Programs Using LTL
Properties", IEEE/ACM ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900.
Artifact: https://github.com/zlfben/autotap (default branch ``master``),
workbook ``data/Data - User Study 1.xlsx``.

That workbook is not redistributed here. The AutoTap repository is GPL-3.0
and no separate licence was found for the files under its ``data/`` directory,
so copying the workbook or any participant text into this Apache-2.0 tree is
not appropriate. Fetch it into the git-ignored cache with
``scripts/fetch_autotap_study1.py``. Labels are keyed by sheet, Excel row, and
statement index and must not contain the original rule text. Rules with no
label are reported as ``unlabeled``, not as failures.

This module does not change Policy DSL semantics. Runtime precedence remains
BLOCK over MODIFY over ALLOW.
"""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from policy_authoring.rule_set_checker import region_minus, rule_region
from policy_authoring.validator import ValidationError, validate_policy

# Qualtrics question-text row in the Study 1 export. Matching it skips that
# row. It is an instrument prompt, not a participant answer.
_QUESTION_ROW_MARK = "Which category does your statement belong to"

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_PKG_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

_HEADER_RE = re.compile(r"^(\d+)_(Q25|Q42|Q35|Q45)$")

_FORBIDDEN_LABEL_KEYS = frozenset({
    "rule",
    "rules",
    "rule_text",
    "text",
    "source_text",
    "natural_language",
    "utterance",
    "statement",
    "nl",
    "participant",
    "exception_text",
})

CACHE_DIR_PARTS = (".cache", "autotap")
REPORT_DIR_PARTS = (".cache", "compile_fidelity")
STUDY1_FILENAME = "Data - User Study 1.xlsx"

CITATION = (
    "Weijia He et al. (Lefan Zhang, Weijia He, Jesse Martinez, "
    "Noah Brackenbury, Shan Lu, and Blase Ur), "
    "AutoTap: Synthesizing and Repairing Trigger-Action Programs Using LTL "
    "Properties, IEEE/ACM ICSE 2019, "
    "https://ieeexplore.ieee.org/abstract/document/8811900. "
    "Artifact: https://github.com/zlfben/autotap (branch master)."
)


class DatasetGuardError(Exception):
    """Raised when a path would place the Study 1 workbook where git could track it."""


class LabelError(Exception):
    """Raised when a labels file is not a valid expected-policy document."""


class WorkbookError(Exception):
    """Raised when the local workbook is missing or not the expected export."""


@dataclass(frozen=True)
class StudyRule:
    """One natural-language statement addressed by a stable id.

    ``text`` is held only in memory so the compiler can read it. Reports built
    by :func:`run` do not copy ``text`` into their output.
    """

    rule_id: str
    sheet: str
    row: int
    statement: int
    text: str
    polarity: Optional[str]
    has_exception: Optional[bool]


def repo_root() -> Path:
    """Repository root (this file lives at ``src/policy_authoring/``)."""
    return Path(__file__).resolve().parents[2]


def cache_dir(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    return root.joinpath(*CACHE_DIR_PARTS)


def report_dir(root: Optional[Path] = None) -> Path:
    root = root or repo_root()
    return root.joinpath(*REPORT_DIR_PARTS)


def study1_workbook_path(root: Optional[Path] = None) -> Path:
    return cache_dir(root) / STUDY1_FILENAME


def _rel_posix(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise DatasetGuardError(
            f"{path} is outside the repository; the Study 1 workbook and "
            "fidelity reports stay under .cache/."
        ) from exc


def _git_ignored(root: Path, rel: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", "--", rel],
        capture_output=True,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = completed.stderr.decode("utf-8", errors="replace").strip()
    raise DatasetGuardError(detail or f"git check-ignore failed for {rel}")


def _under(rel: str, parts: Sequence[str]) -> bool:
    pieces = tuple(Path(rel).parts)
    return len(pieces) >= len(parts) and pieces[: len(parts)] == tuple(parts)


def assert_safe_dataset_destination(path: Path, root: Optional[Path] = None) -> None:
    """Refuse to write the workbook anywhere except the git-ignored cache."""
    root = root or repo_root()
    rel = _rel_posix(root, path)
    if not _under(rel, CACHE_DIR_PARTS):
        raise DatasetGuardError(
            f"Refusing to write {rel}. The Study 1 workbook belongs in "
            f"{'/'.join(CACHE_DIR_PARTS)}/, which is git-ignored."
        )
    name = path.name.lower()
    if not (name.endswith(".xlsx") or name.endswith(".xlsx.partial")):
        raise DatasetGuardError(f"Refusing to write {rel}: expected an .xlsx workbook.")
    if not _git_ignored(root, rel):
        raise DatasetGuardError(
            f"{rel} is not git-ignored. Add .cache/ to .gitignore before fetching."
        )


def assert_safe_report_destination(path: Path, root: Optional[Path] = None) -> None:
    """Refuse to write a fidelity report outside the git-ignored report directory."""
    root = root or repo_root()
    rel = _rel_posix(root, path)
    if not _under(rel, REPORT_DIR_PARTS):
        raise DatasetGuardError(
            f"Refusing to write {rel}. Fidelity reports belong in "
            f"{'/'.join(REPORT_DIR_PARTS)}/, which is git-ignored."
        )
    if not _git_ignored(root, rel):
        raise DatasetGuardError(
            f"{rel} is not git-ignored. Add .cache/ to .gitignore before running."
        )


def tracked_dataset_paths(root: Optional[Path] = None) -> List[str]:
    """Paths git already tracks that would republish the workbook or a report."""
    root = root or repo_root()
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    found: List[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        name = Path(rel).name.lower()
        if (
            name.endswith(".xlsx")
            or name.endswith(".xlsx.partial")
            or "user study" in name
            or _under(rel, CACHE_DIR_PARTS)
            or _under(rel, REPORT_DIR_PARTS)
        ):
            found.append(rel)
    return found


def assert_dataset_not_tracked(root: Optional[Path] = None) -> None:
    """Raise if the cache, a report, or any xlsx is already in the git index."""
    tracked = tracked_dataset_paths(root)
    if tracked:
        raise DatasetGuardError(
            "Refusing to continue: git is tracking dataset or report paths: "
            + ", ".join(tracked)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Workbook loader
# ─────────────────────────────────────────────────────────────────────────────

def _col_index(column: str) -> int:
    number = 0
    for char in column:
        number = number * 26 + (ord(char.upper()) - 64)
    return number


def _split_ref(cell_ref: str) -> Tuple[str, int]:
    column = "".join(ch for ch in cell_ref if ch.isalpha())
    row = int("".join(ch for ch in cell_ref if ch.isdigit()))
    return column, row


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(payload)
    values: List[str] = []
    for item in root.findall("m:si", _NS):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", _NS)))
    return values


def _cell_value(cell: ET.Element, strings: Sequence[str]) -> Optional[str]:
    kind = cell.attrib.get("t")
    node = cell.find("m:v", _NS)
    if kind == "s" and node is not None and node.text is not None:
        return strings[int(node.text)]
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", _NS))
    inline = cell.find("m:is", _NS)
    if inline is not None:
        return "".join(node.text or "" for node in inline.findall(".//m:t", _NS))
    if node is not None and node.text is not None:
        return node.text
    return None


def _sheet_rows(archive: zipfile.ZipFile, target: str, strings: Sequence[str]) -> Dict[int, Dict[str, str]]:
    if target.startswith("/"):
        target = target.lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    root = ET.fromstring(archive.read(target))
    rows: Dict[int, Dict[str, str]] = defaultdict(dict)
    for cell in root.findall(".//m:c", _NS):
        ref = cell.attrib.get("r")
        if not ref:
            continue
        column, row = _split_ref(ref)
        value = _cell_value(cell, strings)
        if value is not None and str(value).strip() != "":
            rows[row][column] = str(value)
    return rows


def _workbook_sheets(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("r:Relationship", _REL_NS)}
    sheets: List[Tuple[str, str]] = []
    for sheet in workbook.findall("m:sheets/m:sheet", _NS):
        name = sheet.attrib["name"]
        sheets.append((name, targets[sheet.attrib[_PKG_REL]]))
    return sheets


def _polarity(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip().lower()
    if "never" in text:
        return "never"
    if "always" in text:
        return "always"
    return None


def _has_exception(value: Optional[str]) -> Optional[bool]:
    if not value:
        return None
    text = value.strip().lower()
    if text.startswith("yes"):
        return True
    if text.startswith("no"):
        return False
    return None


def _statement_columns(header: Mapping[str, str]) -> Dict[int, List[Tuple[int, str, str]]]:
    grouped: Dict[int, List[Tuple[int, str, str]]] = defaultdict(list)
    for column, name in header.items():
        match = _HEADER_RE.match(name.strip())
        if not match:
            continue
        number = int(match.group(1))
        kind = match.group(2)
        grouped[number].append((_col_index(column), column, kind))
    for columns in grouped.values():
        columns.sort()
    return grouped


def _column_value(
    columns: Sequence[Tuple[int, str, str]],
    cells: Mapping[str, str],
    kind: str,
    occurrence: int = 0,
) -> Optional[str]:
    matches = [column for _index, column, name in columns if name == kind]
    if len(matches) <= occurrence:
        return None
    return cells.get(matches[occurrence])


def _is_question_row(cells: Mapping[str, str], grouped: Mapping[int, Sequence[Tuple[int, str, str]]]) -> bool:
    for columns in grouped.values():
        for _, column, kind in columns:
            if kind == "Q42" and _QUESTION_ROW_MARK in cells.get(column, ""):
                return True
    return False


def load_study1(
    path: Path,
    sheets: Sequence[str] = ("Result",),
) -> List[StudyRule]:
    """Load Study 1 statement cells from a local ``.xlsx``.

    The export's first row is variable names (``1_Q25`` … ``10_Q45``). The
    next row repeats the survey prompts and is skipped. Rows whose statement
    cells are all empty are skipped (the public file leaves non-opt-in rows
    blank). Only the statement cell (``Q25``) becomes ``StudyRule.text``.
    The closed polarity column (``Q42``) and the closed exception column
    (the first ``Q35``) are reduced to ``polarity`` and ``has_exception``.
    Free-text exception cells are not returned.

    ``sheets`` defaults to ``Result``, the sheet of responses the paper kept.
    Pass ``("Result", "Discarded Data")`` to include the discarded sheet too.
    """
    path = Path(path)
    if not path.is_file():
        raise WorkbookError(
            f"Study 1 workbook not found at {path}. Fetch it with "
            "scripts/fetch_autotap_study1.py or pass the local path."
        )
    if path.suffix.lower() != ".xlsx":
        raise WorkbookError(f"{path} is not an .xlsx workbook.")

    wanted = set(sheets)
    rules: List[StudyRule] = []
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        available = _workbook_sheets(archive)
        names = [name for name, _target in available]
        missing = [name for name in sheets if name not in names]
        if missing:
            raise WorkbookError(
                f"Workbook is missing sheet(s) {missing}. Sheets present: {names}."
            )
        for sheet_name, target in available:
            if sheet_name not in wanted:
                continue
            rows = _sheet_rows(archive, target, strings)
            if not rows:
                continue
            header_row = min(rows)
            grouped = _statement_columns(rows[header_row])
            if not grouped:
                raise WorkbookError(
                    f"Sheet {sheet_name!r} has no statement columns named like 1_Q25."
                )
            for excel_row in sorted(row for row in rows if row != header_row):
                cells = rows[excel_row]
                if _is_question_row(cells, grouped):
                    continue
                for number in sorted(grouped):
                    columns = grouped[number]
                    # The export names two different questions n_Q35. The
                    # exception question is the first of those columns; an
                    # empty first column must not fall through to the second.
                    text = (_column_value(columns, cells, "Q25") or "").strip()
                    if not text:
                        continue
                    rules.append(StudyRule(
                        rule_id=f"study1:{sheet_name}:{excel_row}:s{number}",
                        sheet=sheet_name,
                        row=excel_row,
                        statement=number,
                        text=text,
                        polarity=_polarity(_column_value(columns, cells, "Q42")),
                        has_exception=_has_exception(_column_value(columns, cells, "Q35", 0)),
                    ))
    return rules


# ─────────────────────────────────────────────────────────────────────────────
# Labels
# ─────────────────────────────────────────────────────────────────────────────

def _reject_forbidden_keys(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in _FORBIDDEN_LABEL_KEYS:
                raise LabelError(
                    f"{where} contains {key!r}. Labels are keyed by rule id and "
                    "must not store the original rule text."
                )
            _reject_forbidden_keys(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{where}[{index}]")


def _expect_policy(raw: Mapping[str, Any], rule_id: str) -> Dict[str, Any]:
    policy = raw.get("policy")
    if not isinstance(policy, dict):
        raise LabelError(f"Label {rule_id} expectation 'policy' needs a policy object.")
    action = policy.get("action")
    if not isinstance(action, dict) or "type" not in action:
        raise LabelError(f"Label {rule_id} policy.action.type is required.")
    candidate = {
        "rule_id": "auto",
        "scope": policy.get("scope"),
        "conditions": policy.get("conditions"),
        "action": {
            "type": action["type"],
            "reason": action.get("reason") or "expected label",
            "params": action.get("params") or {},
        },
        "confidence": 0.95,
    }
    try:
        validate_policy(candidate)
    except ValidationError as exc:
        raise LabelError(f"Label {rule_id} is not a valid Policy DSL object: {exc}") from exc
    return {
        "scope": policy["scope"],
        "conditions": policy["conditions"],
        "action": {
            "type": action["type"],
            "params": action.get("params") or {},
        },
    }


def load_labels(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load expected policies keyed by rule id.

    Shape::

        {"version": 1, "labels": {"study1:Result:3:s1": {
            "expectation": "policy",
            "policy": {"scope": {"device_type": "fan"},
                       "conditions": [{"field": "temperature", "operator": "<", "value": 65}],
                       "action": {"type": "block", "params": {}}}
        }}}

    ``expectation`` may also be ``"reject"``. Unknown ids are kept so the
    runner can report them as unused. The original rule text is rejected if
    it appears under a forbidden key.
    """
    path = Path(path)
    if not path.is_file():
        raise LabelError(f"Labels file not found: {path}")
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise LabelError(f"Labels file is not JSON: {exc}") from exc
    _reject_forbidden_keys(document, "labels")
    if not isinstance(document, dict):
        raise LabelError("Labels file must be a JSON object.")
    if document.get("version") != 1:
        raise LabelError("Labels file version must be 1.")
    raw_labels = document.get("labels", {})
    if not isinstance(raw_labels, dict):
        raise LabelError("'labels' must be an object keyed by rule id.")
    parsed: Dict[str, Dict[str, Any]] = {}
    for rule_id, raw in raw_labels.items():
        if not isinstance(raw, dict):
            raise LabelError(f"Label {rule_id} must be an object.")
        expectation = raw.get("expectation")
        if expectation == "reject":
            parsed[str(rule_id)] = {"expectation": "reject"}
        elif expectation == "policy":
            parsed[str(rule_id)] = {"expectation": "policy", "policy": _expect_policy(raw, str(rule_id))}
        else:
            raise LabelError(
                f"Label {rule_id} expectation must be 'policy' or 'reject'."
            )
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Comparison
# ─────────────────────────────────────────────────────────────────────────────

def _canon_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if number.is_integer():
            return int(number)
        return number
    return value


def _condition_key(condition: Mapping[str, Any]) -> Tuple[Any, Any, Any]:
    return (
        condition.get("field"),
        condition.get("operator"),
        _canon_scalar(condition.get("value")),
    )


def _params(policy: Mapping[str, Any]) -> Any:
    action = policy.get("action") or {}
    params = action.get("params") or {}
    return json.loads(json.dumps(params, sort_keys=True))


def exact_match(compiled: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Same device, action type, params, and condition multiset.

    ``rule_id``, ``confidence``, and ``action.reason`` are ignored. Numeric
    values ``65`` and ``65.0`` match. Condition order does not matter because
    a policy ANDs its conditions. Operators are compared literally, so
    ``>= 22`` and ``> 21`` are not an exact match.
    """
    if compiled.get("scope", {}).get("device_type") != expected.get("scope", {}).get("device_type"):
        return False
    if (compiled.get("action") or {}).get("type") != (expected.get("action") or {}).get("type"):
        return False
    if _params(compiled) != _params(expected):
        return False
    left = sorted(_condition_key(item) for item in compiled.get("conditions") or [])
    right = sorted(_condition_key(item) for item in expected.get("conditions") or [])
    return left == right


def structural_match(compiled: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Same device, action type, params, and condition region.

    Region equality is ``rule_set_checker``'s: two condition sets match when
    each region's remainder against the other is empty. Two unsatisfiable
    condition sets therefore compare equal here; exact match still requires
    the same operators and values.
    """
    if compiled.get("scope", {}).get("device_type") != expected.get("scope", {}).get("device_type"):
        return False
    if (compiled.get("action") or {}).get("type") != (expected.get("action") or {}).get("type"):
        return False
    if _params(compiled) != _params(expected):
        return False
    left = rule_region(dict(compiled))
    right = rule_region(dict(expected))
    return not region_minus(left, right) and not region_minus(right, left)


def assess_compiled(compiled: Any) -> Dict[str, Any]:
    """Classify one compiler return value using the repo validator."""
    if compiled is None:
        return {
            "compiler_returned": False,
            "explicit_rejection": False,
            "valid": False,
            "policy": None,
            "rejection_reason": "compiler returned no output",
            "rejection_category": "no_output",
        }
    if not isinstance(compiled, dict):
        return {
            "compiler_returned": True,
            "explicit_rejection": False,
            "valid": False,
            "policy": None,
            "rejection_reason": f"compiler returned {type(compiled).__name__}, not an object",
            "rejection_category": "invalid_dsl",
        }
    if compiled.get("rejected") is True:
        reason = compiled.get("reason") or "compiler rejected the rule"
        return {
            "compiler_returned": True,
            "explicit_rejection": True,
            "valid": False,
            "policy": None,
            "rejection_reason": str(reason),
            "rejection_category": "explicit_rejection",
        }
    try:
        policy = validate_policy(json.loads(json.dumps(compiled)))
    except ValidationError as exc:
        return {
            "compiler_returned": True,
            "explicit_rejection": False,
            "valid": False,
            "policy": None,
            "rejection_reason": str(exc),
            "rejection_category": "invalid_dsl",
        }
    return {
        "compiler_returned": True,
        "explicit_rejection": False,
        "valid": True,
        "policy": policy,
        "rejection_reason": None,
        "rejection_category": None,
    }


def _label_outcome(assessment: Mapping[str, Any], label: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if label is None:
        return {
            "label_status": "unlabeled",
            "exact_match": None,
            "structural_match": None,
            "fidelity_failure": False,
        }
    if label["expectation"] == "reject":
        if assessment["valid"]:
            return {
                "label_status": "unexpected_accept",
                "exact_match": False,
                "structural_match": False,
                "fidelity_failure": True,
            }
        if not assessment["compiler_returned"]:
            return {
                "label_status": "no_output",
                "exact_match": False,
                "structural_match": False,
                "fidelity_failure": True,
            }
        return {
            "label_status": "expected_reject",
            "exact_match": False,
            "structural_match": False,
            "fidelity_failure": False,
        }
    expected = label["policy"]
    if not assessment["valid"]:
        status = "no_output" if not assessment["compiler_returned"] else "unexpected_reject"
        return {
            "label_status": status,
            "exact_match": False,
            "structural_match": False,
            "fidelity_failure": True,
        }
    policy = assessment["policy"]
    exact = exact_match(policy, expected)
    structural = exact or structural_match(policy, expected)
    if exact:
        status = "exact_match"
    elif structural:
        status = "structural_match"
    else:
        status = "mismatch"
    return {
        "label_status": status,
        "exact_match": exact,
        "structural_match": structural,
        "fidelity_failure": status == "mismatch",
    }


def run(
    rules: Sequence[StudyRule],
    labels: Mapping[str, Mapping[str, Any]],
    compile_fn: Callable[[StudyRule], Any],
    compiler: str,
) -> Dict[str, Any]:
    """Compile each rule and build a report that omits the source sentence.

    ``compiler`` is ``live``, ``responses``, or ``injected`` and is stored on
    the summary so a report shows whether a model was called.
    """
    rows: List[Dict[str, Any]] = []
    for rule in rules:
        assessment = assess_compiled(compile_fn(rule))
        judged = _label_outcome(assessment, labels.get(rule.rule_id))
        rows.append({
            "rule_id": rule.rule_id,
            "sheet": rule.sheet,
            "row": rule.row,
            "statement": rule.statement,
            "polarity": rule.polarity,
            "has_exception": rule.has_exception,
            "compiler_returned": assessment["compiler_returned"],
            "explicit_rejection": assessment["explicit_rejection"],
            "valid": assessment["valid"],
            "rejection_reason": assessment["rejection_reason"],
            "rejection_category": assessment["rejection_category"],
            "label_status": judged["label_status"],
            "exact_match": judged["exact_match"],
            "structural_match": judged["structural_match"],
            "fidelity_failure": judged["fidelity_failure"],
        })
    return {
        "citation": CITATION,
        "redistributed": False,
        "summary": _summarize(rows, labels, rules, compiler),
        "rows": rows,
    }


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _summarize(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    rules: Sequence[StudyRule],
    compiler: str,
) -> Dict[str, Any]:
    outcomes = Counter(row["label_status"] for row in rows)
    rejection_categories = Counter(
        row["rejection_category"] for row in rows if row["rejection_category"]
    )
    labeled_rows = [row for row in rows if row["label_status"] != "unlabeled"]
    policy_rows = [
        row for row in labeled_rows
        if labels.get(row["rule_id"], {}).get("expectation") == "policy"
    ]
    reject_rows = [
        row for row in labeled_rows
        if labels.get(row["rule_id"], {}).get("expectation") == "reject"
    ]
    exact = sum(1 for row in policy_rows if row["exact_match"])
    structural = sum(1 for row in policy_rows if row["structural_match"])
    failures = sum(1 for row in rows if row["fidelity_failure"])
    seen = {rule.rule_id for rule in rules}
    unused = sorted(set(labels) - seen)
    returned = sum(1 for row in rows if row["compiler_returned"])
    valid = sum(1 for row in rows if row["valid"])
    return {
        "compiler": compiler,
        "n": len(rows),
        "compiler_returned": returned,
        "compiler_returned_rate": _rate(returned, len(rows)),
        "valid": valid,
        "valid_rate": _rate(valid, len(rows)),
        "explicit_rejections": sum(1 for row in rows if row["explicit_rejection"]),
        "unlabeled": outcomes.get("unlabeled", 0),
        "labeled": len(labeled_rows),
        "policy_labels": len(policy_rows),
        "reject_labels": len(reject_rows),
        "exact_matches": exact,
        "structural_matches": structural,
        "exact_match_rate": _rate(exact, len(policy_rows)),
        "structural_match_rate": _rate(structural, len(policy_rows)),
        "expected_rejects": sum(1 for row in reject_rows if row["label_status"] == "expected_reject"),
        "fidelity_failures": failures,
        "fidelity_failure_rate": _rate(failures, len(labeled_rows)),
        "outcomes": dict(outcomes),
        "rejection_categories": dict(rejection_categories),
        "unused_labels": unused,
    }


def format_report(report: Mapping[str, Any]) -> str:
    """Short text table. Per-rule detail stays in the JSON report."""
    summary = report["summary"]
    lines = [
        "DeviceWeave compile-fidelity",
        CITATION,
        "Dataset redistributed: no",
        f"compiler: {summary['compiler']}",
        "",
        f"{'rules':<28} {summary['n']:>8}",
        f"{'compiler returned':<28} {summary['compiler_returned']:>8}  {summary['compiler_returned_rate']}",
        f"{'valid DSL':<28} {summary['valid']:>8}  {summary['valid_rate']}",
        f"{'explicit rejections':<28} {summary['explicit_rejections']:>8}",
        f"{'unlabeled (not failures)':<28} {summary['unlabeled']:>8}",
        f"{'labeled':<28} {summary['labeled']:>8}",
        f"{'exact matches':<28} {summary['exact_matches']:>8}  {summary['exact_match_rate']}",
        f"{'structural (incl. exact)':<28} {summary['structural_matches']:>8}  {summary['structural_match_rate']}",
        f"{'expected rejects':<28} {summary['expected_rejects']:>8}",
        f"{'fidelity failures':<28} {summary['fidelity_failures']:>8}  {summary['fidelity_failure_rate']}",
        "",
        f"{'outcome':<28} {'count':>8}",
    ]
    outcomes = summary["outcomes"] or {"(none)": 0}
    for name, count in sorted(outcomes.items()):
        lines.append(f"{name:<28} {count:>8}")
    lines.append("")
    lines.append(f"{'rejection category':<28} {'count':>8}")
    categories = summary["rejection_categories"] or {"(none)": 0}
    for name, count in sorted(categories.items()):
        lines.append(f"{name:<28} {count:>8}")
    if summary["unused_labels"]:
        lines.append("")
        lines.append("unused labels: " + ", ".join(summary["unused_labels"]))
    if summary["labeled"] == 0:
        lines.append("")
        lines.append(
            "No expected-policy labels matched these rules. "
            "Add them to benchmarks/compile_fidelity/labels.json; "
            "unlabeled rules stay out of the failure count."
        )
    lines.append("")
    lines.append("Per-rule rows, including rejection reasons, are in report.json.")
    lines.append("That file is written under .cache/ and must not be committed.")
    return "\n".join(lines) + "\n"


def write_report(report: Mapping[str, Any], out_dir: Path, root: Optional[Path] = None) -> Tuple[Path, Path]:
    """Write ``report.json`` and ``report.txt`` under the git-ignored report dir."""
    root = root or repo_root()
    out_dir = Path(out_dir)
    assert_safe_report_destination(out_dir, root)
    assert_dataset_not_tracked(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    text_path = out_dir / "report.txt"
    assert_safe_report_destination(json_path, root)
    assert_safe_report_destination(text_path, root)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    text_path.write_text(format_report(report))
    return json_path, text_path


def live_compile(rule: StudyRule) -> Any:
    """Call the real compiler. Opt-in only: this reaches the configured LLM provider."""
    from policy_authoring.llm_compiler import compile_rule
    return compile_rule(rule.text)


def responses_compile(saved: Mapping[str, Any]) -> Callable[[StudyRule], Any]:
    """Score saved ``{id, compiled}`` records. Missing ids yield no output."""

    def compile_one(rule: StudyRule) -> Any:
        if rule.rule_id not in saved:
            return None
        return saved[rule.rule_id]

    return compile_one


def load_responses(path: Path) -> Dict[str, Any]:
    saved: Dict[str, Any] = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        saved[row["id"]] = row.get("compiled")
    return saved
