"""The compile-fidelity harness: loader, labels, scoring, reports and the dataset guards.

Every rule below is invented for these tests. None is taken or paraphrased from
AutoTap's Study 1 (Zhang, He, et al., ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900; data at
https://github.com/zlfben/autotap), which this repository does not redistribute.
The spreadsheet is built here with the stdlib in the same layout as that export,
and the compiler under test is the real ``llm_compiler.compile_rule`` with a fake
LLM provider, so nothing touches the network.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("compile_fidelity", ROOT / "scripts" / "compile_fidelity.py")
CF = importlib.util.module_from_spec(_spec)
sys.modules["compile_fidelity"] = CF   # dataclasses look their module up here
_spec.loader.exec_module(CF)

ALWAYS = "Things that should always happen"
NEVER = "Things that should never happen"
NO_EXC = "No, I believe it should be true no matter what."
YES_EXC = "Yes, there are exceptions in some cases."

# Invented statements, one per (row, slot).
R_HEATER = "Never switch the space heater on while the house is empty"
R_PORCH = "The porch light must stay off after 11 pm"
R_DOOR = "Lock the back door whenever everyone has gone out"
R_FAN = "Do not let the attic fan run for more than 3 hours"
R_LAMP = "Every weekday at 6:30 am turn the hallway lamp on"
R_AC = "Keep the AC off when it is cooler than 60 degrees"
R_WEIRD = "Make the kitchen feel nicer"


# ─────────────────────────────────────────────────────────────────────────────
# A synthetic workbook in the Study 1 layout
# ─────────────────────────────────────────────────────────────────────────────

def _header(slots: int):
    cols = ["Duration", "ReleaseData"]
    for k in range(1, slots + 1):
        cols += [f"{k}_Q25", f"{k}_Q42", f"{k}_Q35", f"{k}_Q45", f"{k}_Q35"]
    return cols + ["Q20"]


def _participant(*statements):
    """statements: (text, category, exception) triples -> one row's values."""
    row = ["300", "Yes"]
    for text, cat, exc in statements:
        row += [text, cat, exc, "", "I expect most people would want this exactly as written"]
    return row + ["prefer not to say"]


def write_xlsx(path: Path, sheets: dict) -> Path:
    """Minimal valid .xlsx: shared strings for row 1, inline strings elsewhere."""
    shared: list = []

    def cell(ref: str, value: str, row: int) -> str:
        if value == "":
            return ""
        if row == 1:
            if value not in shared:
                shared.append(value)
            return f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    parts = {}
    sheet_entries, rels = [], []
    for i, (name, rows) in enumerate(sheets.items(), start=1):
        xml_rows = []
        for r, values in rows.items():
            cells = "".join(cell(f"{CF.column_letter(c)}{r}", v, r) for c, v in enumerate(values))
            xml_rows.append(f'<row r="{r}">{cells}</row>')
        parts[f"xl/worksheets/sheet{i}.xml"] = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
        sheet_entries.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
                    f'2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
    parts["xl/workbook.xml"] = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheet_entries)}</sheets></workbook>')
    parts["xl/_rels/workbook.xml.rels"] = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(rels)}</Relationships>')
    parts["xl/sharedStrings.xml"] = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    parts["[Content_Types].xml"] = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return path


@pytest.fixture
def study_xlsx(tmp_path) -> Path:
    header = _header(3)
    return write_xlsx(tmp_path / "synthetic-study.xlsx", {
        "Result": {
            1: header,
            2: ["Duration", "May we release..."] + ["question text"] * (len(header) - 2),
            3: _participant((R_HEATER, NEVER, NO_EXC), (R_PORCH, ALWAYS, YES_EXC), (R_DOOR, ALWAYS, NO_EXC)),
            4: [],  # a participant who did not opt in: blank row
            5: _participant((R_FAN, NEVER, NO_EXC), ("", "", ""), (R_LAMP, ALWAYS, NO_EXC)),
            6: _participant((R_AC, NEVER, NO_EXC), (R_WEIRD, ALWAYS, NO_EXC), ("", "", "")),
        },
        "Discarded Data": {1: header, 2: [], 3: _participant((R_WEIRD, ALWAYS, NO_EXC), ("", "", ""), ("", "", ""))},
    })


# ─────────────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────────────

def test_loader_reads_statements_with_stable_position_ids(study_xlsx):
    rules = CF.load_rules(study_xlsx)
    assert [r.rule_id for r in rules] == [
        "study1:Result:row3:stmt1", "study1:Result:row3:stmt2", "study1:Result:row3:stmt3",
        "study1:Result:row5:stmt1", "study1:Result:row5:stmt3",
        "study1:Result:row6:stmt1", "study1:Result:row6:stmt2",
    ]
    by_id = {r.rule_id: r for r in rules}
    porch = by_id["study1:Result:row3:stmt2"]
    assert (porch.text, porch.category, porch.has_exception, porch.cell) == (R_PORCH, "always", True, "H3")
    assert by_id["study1:Result:row3:stmt1"].category == "never"
    assert all(CF.RULE_ID_RE.match(r.rule_id) for r in rules)


def test_loader_reads_other_sheets_on_request(study_xlsx):
    rules = CF.load_rules(study_xlsx, ["Discarded Data"])
    assert [r.rule_id for r in rules] == ["study1:Discarded Data:row3:stmt1"]


def test_loader_refuses_a_workbook_without_statement_columns(tmp_path):
    path = write_xlsx(tmp_path / "other.xlsx", {"Result": {1: ["a", "b"], 3: ["x", "y"]}})
    with pytest.raises(CF.FidelityError, match="no statement columns"):
        CF.load_rules(path)
    with pytest.raises(CF.FidelityError, match="not in"):
        CF.load_rules(path, ["Missing"])
    (tmp_path / "junk.xlsx").write_bytes(b"not a zip")
    with pytest.raises(CF.FidelityError, match="not a readable"):
        CF.read_xlsx(tmp_path / "junk.xlsx")


def test_loader_agrees_with_openpyxl(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Result"
    header = _header(2)
    ws.append(header)
    ws.append(["q"] * len(header))
    ws.append(_participant((R_HEATER, NEVER, NO_EXC), (R_LAMP, ALWAYS, YES_EXC)))
    path = tmp_path / "openpyxl.xlsx"
    wb.save(path)
    rules = CF.load_rules(path)
    assert [(r.text, r.cell) for r in rules] == [(R_HEATER, "C3"), (R_LAMP, "H3")]


def test_path_resolution_flag_then_env_then_cache(study_xlsx, tmp_path):
    assert CF.resolve_xlsx_path(str(study_xlsx), env={}) == study_xlsx
    assert CF.resolve_xlsx_path(None, env={CF.ENV_VAR: str(study_xlsx)}) == study_xlsx
    with pytest.raises(CF.FidelityError, match="compile_fidelity.py fetch"):
        CF.resolve_xlsx_path(str(tmp_path / "nope.xlsx"), env={})


# ─────────────────────────────────────────────────────────────────────────────
# The real compiler, with a fake LLM provider
# ─────────────────────────────────────────────────────────────────────────────

def _policy(device, conds, kind="block", confidence=0.95, params=None):
    return {"rule_id": "auto", "scope": {"device_type": device},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": kind, "reason": "test", "params": params or {}}, "confidence": confidence}


CANNED = {
    R_HEATER: json.dumps(_policy("heater", [("is_home", "==", False)])),
    # fenced output exercises compile_rule's own clean-up
    R_PORCH: "```json\n" + json.dumps(_policy("light", [("time_hour", ">=", 23)])) + "\n```",
    R_DOOR: json.dumps({"rejected": True, "reason": "Door locks are not a supported device type.", "confidence": 0.0}),
    R_FAN: json.dumps({"rejected": True, "reason": "Run duration cannot be mapped to any allowed condition field.",
                       "confidence": 0.0}),
    R_LAMP: "this is not json",
    R_AC: json.dumps(_policy("ac", [("temperature", "<", 60), ("temperature", ">", 80)])),   # contradiction
    R_WEIRD: json.dumps(_policy("light", [("time_hour", ">=", 22)], confidence=0.5)),
}


class FakeProvider:
    model_id = "fake"

    def __init__(self):
        self.calls = []

    def invoke(self, system_prompt, user_message, max_tokens=512):
        self.calls.append(user_message)
        for text, out in CANNED.items():
            if text in user_message:
                return out
        raise AssertionError(f"unexpected rule: {user_message}")


@pytest.fixture
def real_compiler(monkeypatch):
    from policy_authoring import llm_compiler

    fake = FakeProvider()
    monkeypatch.setattr(llm_compiler, "get_llm_provider", lambda: fake)
    return (lambda _rid, text: llm_compiler.compile_rule(text)), fake


def _rows(result):
    return {r["id"]: r for r in result["rows"]}


def test_every_rule_goes_through_the_real_compiler_and_validator(study_xlsx, real_compiler):
    compile_fn, fake = real_compiler
    rules = CF.load_rules(study_xlsx)
    result = CF.evaluate(rules, compile_fn)
    rows = _rows(result)
    assert len(fake.calls) == len(rules)

    heater = rows["study1:Result:row3:stmt1"]
    assert heater["compile"] == "compiled" and heater["valid"] and heater["accepted"]
    assert heater["policy"]["scope"] == {"device_type": "heater"}
    assert heater["label"] == "unlabeled"
    assert rows["study1:Result:row3:stmt2"]["accepted"]                          # fences stripped
    assert rows["study1:Result:row3:stmt3"]["failure_category"] == "refused_unsupported_device"
    assert rows["study1:Result:row5:stmt1"]["failure_category"] == "refused_unsupported_condition"
    assert rows["study1:Result:row5:stmt3"]["compile"] == "infra_error"
    ac = rows["study1:Result:row6:stmt1"]
    assert ac["valid"] and ac["satisfiable"] is False and not ac["accepted"]
    assert ac["failure_category"] == "unsatisfiable"
    assert rows["study1:Result:row6:stmt2"]["failure_category"] == "invalid_low_confidence"

    s = result["summary"]
    assert s["n"] == 7 and s["accepted"] == 2 and s["validatorAccepted"] == 3 and s["unsatisfiable"] == 1
    assert s["compile"] == {"compiled": 4, "refused": 2, "infra_error": 1}
    assert s["labels"]["counts"] == {"unlabeled": 7} and s["labels"]["exactRate"] is None
    assert s["byCategory"]["never"]["n"] == 3
    assert "duration" in s["byPattern"] and s["byPattern"]["duration"]["accepted"] == 0


def test_rows_never_carry_rule_text_unless_asked(study_xlsx, real_compiler):
    compile_fn, _ = real_compiler
    rules = CF.load_rules(study_xlsx)
    plain = json.dumps(CF.evaluate(rules, compile_fn)["rows"])
    assert R_HEATER not in plain and R_WEIRD not in plain
    with_text = CF.evaluate(rules, compile_fn, include_text=True)["rows"]
    assert with_text[0]["text"] == R_HEATER


# ─────────────────────────────────────────────────────────────────────────────
# Labels
# ─────────────────────────────────────────────────────────────────────────────

def _labels_file(tmp_path, labels):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"format": CF.LABELS_FORMAT, "labels": labels}))
    return path


def _expect(device, conds, kind="block"):
    return {"scope": {"device_type": device},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": kind, "params": {}}}


def test_labels_score_exact_equivalent_mismatch_and_rejects(study_xlsx, real_compiler, tmp_path):
    compile_fn, _ = real_compiler
    labels = CF.load_labels(_labels_file(tmp_path, {
        # identical
        "study1:Result:row3:stmt1": {"expect": "policy", "policies": [_expect("heater", [("is_home", "==", False)])]},
        # time_hour > 22 is time_hour >= 23 on an integer hour: same meaning, different JSON
        "study1:Result:row3:stmt2": {"expect": "policy", "policies": [_expect("light", [("time_hour", ">", 22)])]},
        "study1:Result:row3:stmt3": {"expect": "reject", "reason_category": "unsupported_device"},
        "study1:Result:row5:stmt1": {"expect": "policy", "policies": [_expect("fan", [("time_hour", ">=", 0)])]},
        "study1:Result:row6:stmt1": {"expect": "reject", "reason_category": "unsatisfiable"},
    }))
    rows = _rows(CF.evaluate(CF.load_rules(study_xlsx), compile_fn, labels))
    assert rows["study1:Result:row3:stmt1"]["label"] == "match_exact"
    assert rows["study1:Result:row3:stmt2"]["label"] == "match_equivalent"
    assert rows["study1:Result:row3:stmt3"]["label"] == "reject_expected_ok"
    assert rows["study1:Result:row3:stmt3"]["reason_category_match"] is True
    assert rows["study1:Result:row5:stmt1"]["label"] == "policy_expected_but_refused"
    assert rows["study1:Result:row6:stmt1"]["label"] == "reject_expected_ok"
    assert rows["study1:Result:row5:stmt3"]["label"] == "unlabeled"


def test_compare_reports_what_differs():
    got = CF.validate_policy(_policy("light", [("time_hour", ">=", 22)]))
    same = [_expect("light", [("time_hour", ">", 21)])]
    assert CF.compare(got, [CF._as_validatable(p) for p in same])[0] == "match_equivalent"
    flipped = [CF._as_validatable(_expect("light", [("time_hour", "<", 22)]))]
    assert CF.compare(got, flipped) == ("mismatch", ["conditions"])
    assert CF.compare(got, [CF._as_validatable(_expect("fan", [("time_hour", ">=", 22)]))]) == ("mismatch", ["device"])
    assert CF.compare(got, [CF._as_validatable(_expect("light", [("time_hour", ">=", 22)], "allow"))])[1] == ["action"]


def test_a_label_may_need_more_than_one_policy():
    # "Overnight, 11 pm to 6 am": two DSL rules, because one rule's conditions are ANDed.
    overnight = [CF._as_validatable(_expect("light", [("time_hour", ">=", 23)])),
                 CF._as_validatable(_expect("light", [("time_hour", "<", 6)]))]
    half = CF.validate_policy(_policy("light", [("time_hour", ">=", 23)]))
    assert CF.compare(half, overnight) == ("mismatch", ["conditions", "needs_multiple_policies"])
    # The same union written redundantly is still matched by one rule that covers it.
    redundant = [CF._as_validatable(_expect("heater", [("temperature", "<", 60)])),
                 CF._as_validatable(_expect("heater", [("temperature", "<", 50)]))]
    one = CF.validate_policy(_policy("heater", [("temperature", "<", 60)]))
    assert CF.compare(one, redundant)[0] == "match_equivalent"


@pytest.mark.parametrize("label,match", [
    ({"expect": "policy", "policies": [_expect("light", [("is_home", "==", False)])], "text": "x"}, "unknown keys"),
    ({"expect": "policy", "policies": [_expect("toaster", [("is_home", "==", False)])]}, "not a valid policy"),
    ({"expect": "policy", "policies": []}, "non-empty"),
    ({"expect": "maybe"}, "'expect'"),
    ({"expect": "reject", "reason_category": "because"}, "reason_category"),
])
def test_bad_labels_are_refused(tmp_path, label, match):
    with pytest.raises(CF.FidelityError, match=match):
        CF.load_labels(_labels_file(tmp_path, {"study1:Result:row3:stmt1": label}))


def test_label_ids_are_positions_not_text(tmp_path):
    with pytest.raises(CF.FidelityError, match="rule id"):
        CF.load_labels(_labels_file(tmp_path, {R_HEATER: {"expect": "reject"}}))


def test_a_note_that_repeats_the_rule_text_is_caught(study_xlsx, tmp_path):
    rules = CF.load_rules(study_xlsx)
    labels = CF.load_labels(_labels_file(tmp_path, {
        "study1:Result:row3:stmt1": {"expect": "reject", "note": f"participant wrote: {R_HEATER.upper()}!"},
        "study1:Result:row3:stmt2": {"expect": "reject", "note": "overnight window; needs two rules"},
    }))
    assert CF.labels_leaking_text(rules, labels) == ["study1:Result:row3:stmt1"]


def test_committed_labels_file_is_valid_and_holds_no_text():
    doc = json.loads(CF.LABELS_PATH.read_text())
    assert doc["format"] == CF.LABELS_FORMAT
    assert doc["dataset"]["sha256"] == CF.STUDY1_SHA256
    CF.load_labels(CF.LABELS_PATH)
    for rid, label in doc["labels"].items():
        assert CF.RULE_ID_RE.match(rid)
        assert set(label) <= CF.LABEL_KEYS


# ─────────────────────────────────────────────────────────────────────────────
# Report, CLI and fetch
# ─────────────────────────────────────────────────────────────────────────────

def test_cli_scores_saved_responses_and_writes_a_report(study_xlsx, tmp_path, capsys):
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "study1:Result:row3:stmt1", "compiled": _policy("heater", [("is_home", "==", False)])}) + "\n"
        + json.dumps({"id": "study1:Result:row3:stmt3",
                      "compiled": {"rejected": True, "reason": "unsupported device", "confidence": 0.0}}) + "\n")
    out = tmp_path / "out"
    labels = _labels_file(tmp_path, {})
    rc = CF.main(["run", "--xlsx", str(study_xlsx), "--responses", str(responses),
                  "--labels", str(labels), "--out-dir", str(out)])
    assert rc == 0
    summary = json.loads((out / "summary.json").read_text())
    assert summary["n"] == 2 and summary["noResponse"] == 5 and summary["accepted"] == 1
    assert summary["meta"]["compiler"] == "responses:responses.jsonl"
    assert len((out / "rows.jsonl").read_text().splitlines()) == 7
    table = (out / "report.txt").read_text()
    assert "accepted (validator + rule-set)" in table and "refused_unsupported_device" in table
    assert R_HEATER not in (out / "rows.jsonl").read_text() + table
    assert "Compile fidelity" in capsys.readouterr().out


def test_cli_requires_an_explicit_compiler_mode(study_xlsx, tmp_path, capsys):
    rc = CF.main(["run", "--xlsx", str(study_xlsx), "--out-dir", str(tmp_path / "o"),
                  "--labels", str(_labels_file(tmp_path, {}))])
    assert rc == 2 and "--live" in capsys.readouterr().err


def test_cli_list_needs_no_compiler(study_xlsx, capsys):
    assert CF.main(["list", "--xlsx", str(study_xlsx)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["rules"] == 7 and listed["participants"] == 3 and listed["withException"] == 1


def test_reports_are_not_written_into_tracked_parts_of_the_repo():
    with pytest.raises(CF.FidelityError, match="not git-ignored"):
        CF.assert_ignored(ROOT / "docs" / "fidelity-report")
    CF.assert_ignored(CF.OUT_DIR / "nested")      # the default output dir is fine


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_fetch_verifies_the_pinned_checksum_and_guards_the_cache(tmp_path, monkeypatch):
    payload = b"pretend spreadsheet bytes"
    urls = []

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        return _FakeResponse(payload)

    dest = tmp_path / "cache" / "study1.xlsx"
    with pytest.raises(CF.FidelityError, match="checksum mismatch"):
        CF.fetch(dest=dest, urlopen=fake_urlopen)
    assert not dest.exists()
    assert urls[0] == ("https://raw.githubusercontent.com/zlfben/autotap/" + CF.AUTOTAP_COMMIT
                       + "/data/Data%20-%20User%20Study%201.xlsx")

    import hashlib
    monkeypatch.setattr(CF, "STUDY1_SHA256", hashlib.sha256(payload).hexdigest())
    assert CF.fetch(dest=dest, urlopen=fake_urlopen).read_bytes() == payload
    assert (dest.parent / ".gitignore").read_text().splitlines()[-1] == "*"


def test_patterns_are_tagged_from_invented_examples():
    tags = CF.tag_patterns("Turn the porch light off at 11 pm every night unless guests are over", "always", True)
    assert {"time_of_day", "periodic", "unless_in_text", "supported_device_mentioned",
            "category_always", "exception_flagged"} <= set(tags)
    assert "multi_device" in CF.tag_patterns("If the window is open the AC should be off")
    assert "duration" in CF.tag_patterns("The attic fan may run for more than 90 minutes only on weekends")
    assert "notification" in CF.tag_patterns("Alert me when the garage door opens")


# ─────────────────────────────────────────────────────────────────────────────
# The dataset cannot be committed
# ─────────────────────────────────────────────────────────────────────────────

def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


needs_git = pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                               reason="needs a git checkout")


@needs_git
@pytest.mark.parametrize("path", [
    "benchmarks/autotap/_cache/autotap-study1.xlsx",
    "benchmarks/autotap/_cache/anything-else.json",
    "benchmarks/autotap/_out/rows.jsonl",
    "Data - User Study 1.xlsx",
    "benchmarks/autotap/Data - User Study 1.xlsx",
    "tests/fixtures/whatever.xlsx",
])
def test_cache_output_and_spreadsheets_are_git_ignored(path):
    assert _git("check-ignore", "-q", path).returncode == 0, f"{path} is not git-ignored"


@needs_git
def test_no_spreadsheet_is_tracked():
    tracked = _git("ls-files", "-z").stdout.split("\0")
    assert not [p for p in tracked if p.lower().endswith((".xlsx", ".xlsm", ".xls"))]
    assert not [p for p in tracked if p.startswith(("benchmarks/autotap/_cache/", "benchmarks/autotap/_out/"))]
    # A renamed workbook is still a zip with xl/workbook.xml inside.
    for p in tracked:
        f = ROOT / p
        if p and f.is_file() and zipfile.is_zipfile(f):
            with zipfile.ZipFile(f) as zf:
                assert "xl/workbook.xml" not in zf.namelist(), f"{p} is a spreadsheet"
