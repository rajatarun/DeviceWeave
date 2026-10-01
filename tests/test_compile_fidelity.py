"""Compile-fidelity harness: synthetic rules only, no Study 1 text, no network.

AutoTap (Weijia He et al., IEEE/ACM ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900;
https://github.com/zlfben/autotap) is the corpus the harness is built to score.
These tests never read that workbook. The upstream repo is GPL-3.0 and no
separate data licence was found, so the workbook must stay out of git.
"""
from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from policy_authoring.compile_fidelity import (
    DatasetGuardError,
    LabelError,
    assert_dataset_not_tracked,
    assert_safe_dataset_destination,
    assert_safe_report_destination,
    exact_match,
    load_labels,
    load_study1,
    repo_root,
    run,
    structural_match,
    study1_workbook_path,
    write_report,
)

ROOT = repo_root()


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FETCH = _load_script("fetch_autotap_study1.py")
RUNNER = _load_script("compile_fidelity.py")


def _letter(index: int) -> str:
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(64 + rem + 1) + letters
    return letters


def write_xlsx(path: Path, sheets: list, shared: bool = True) -> None:
    """Minimal workbook. ``sheets`` is [(name, rows of cell values)]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    strings: list = []
    index_of = {}

    def shared_index(value: str) -> int:
        if value not in index_of:
            index_of[value] = len(strings)
            strings.append(value)
        return index_of[value]

    sheet_xml = []
    for _name, rows in sheets:
        cells = []
        for r_i, row in enumerate(rows, start=1):
            for c_i, value in enumerate(row, start=1):
                if value is None or value == "":
                    continue
                ref = f"{_letter(c_i)}{r_i}"
                text = str(value)
                if shared:
                    cells.append(f'<c r="{ref}" t="s"><v>{shared_index(text)}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
                    )
        body = "".join(cells)
        sheet_xml.append(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData><row>{body}</row></sheetData></worksheet>"
        )

    sheet_entries = []
    rel_entries = []
    for number, (name, _rows) in enumerate(sheets, start=1):
        sheet_entries.append(
            f'<sheet name="{escape(name)}" sheetId="{number}" r:id="rId{number}"/>'
        )
        rel_entries.append(
            f'<Relationship Id="rId{number}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{number}.xml"/>'
        )
    if shared:
        rel_entries.append(
            '<Relationship Id="rIdShared" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
            'Target="sharedStrings.xml"/>'
        )

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
        + "</sst>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(sheet_entries)}</sheets></workbook>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{n}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for n in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rel_entries)
            + "</Relationships>",
        )
        for number, xml in enumerate(sheet_xml, start=1):
            archive.writestr(f"xl/worksheets/sheet{number}.xml", xml)
        if shared:
            archive.writestr("xl/sharedStrings.xml", shared_xml)


def _fixture_sheets():
    header = [
        "1_Q25", "1_Q42", "1_Q35", "1_Q45", "1_Q35",
        "2_Q25", "2_Q42", "2_Q35", "2_Q45", "2_Q35",
    ]
    question = [
        "1 - Please write down one of your statements.",
        "1 - Which category does your statement belong to?",
        "1 - Are there any exceptions to the statement you wrote above?",
        "1 - Can you please mention the exception?",
        "1 - Do you expect that this is something most people would want?",
        "2 - Please write down one of your statements.",
        "2 - Which category does your statement belong to?",
        "", "", "",
    ]
    participant = [
        "Don't turn on the fan when it is below 60 degrees",
        "never",
        "No",
        "When the window is open",
        "same",
        "Keep the lamp on when I am home",
        "always",
        "Yes",
        "",
        "",
    ]
    blank = [""] * 10
    only_second = [
        "", "", "", "", "",
        "Block the heater when humidity is high",
        "never",
        "No",
        "",
        "",
    ]
    # First Q35 (exceptions) is blank; the second Q35 is a different question.
    sparse_exception = [
        "Dim the porch light after 9 PM",
        "always",
        "",
        "",
        "Yes",
        "", "", "", "", "",
    ]
    result = [header, question, participant, blank, only_second, sparse_exception]
    discarded = [
        header,
        question,
        ["Turn the television off when I leave", "never", "No", "", "", "", "", "", "", ""],
    ]
    return result, discarded


RULE_FAN = "Don't turn on the fan when it is below 60 degrees"
RULE_LAMP = "Keep the lamp on when I am home"
RULE_HEATER = "Block the heater when humidity is high"
RULE_PORCH = "Dim the porch light after 9 PM"
EXCEPTION = "When the window is open"


def _compiled(device, conds, kind="block", params=None):
    return {
        "rule_id": "auto",
        "scope": {"device_type": device},
        "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
        "action": {"type": kind, "reason": "because the rule says so", "params": params or {}},
        "confidence": 0.95,
    }


def _policy_label(device, conds, kind="block", params=None):
    return {
        "expectation": "policy",
        "policy": {
            "scope": {"device_type": device},
            "conditions": [{"field": f, "operator": o, "value": v} for f, o, v in conds],
            "action": {"type": kind, "params": params or {}},
        },
    }


@pytest.fixture()
def workbook(tmp_path):
    path = tmp_path / "synthetic.xlsx"
    result, discarded = _fixture_sheets()
    write_xlsx(path, [("Result", result), ("Discarded Data", discarded), ("Notes", [["ignore me"]])])
    return path


def test_loader_reads_statements_and_skips_prompts_and_blanks(workbook):
    rules = load_study1(workbook)
    by_id = {rule.rule_id: rule for rule in rules}
    assert list(by_id) == [
        "study1:Result:3:s1",
        "study1:Result:3:s2",
        "study1:Result:5:s2",
        "study1:Result:6:s1",
    ]
    fan = by_id["study1:Result:3:s1"]
    assert fan.text == RULE_FAN
    assert fan.polarity == "never"
    assert fan.has_exception is False
    assert EXCEPTION not in fan.text
    lamp = by_id["study1:Result:3:s2"]
    assert lamp.text == RULE_LAMP
    assert lamp.polarity == "always"
    assert lamp.has_exception is True
    assert by_id["study1:Result:5:s2"].statement == 2
    assert by_id["study1:Result:5:s2"].has_exception is False
    porch = by_id["study1:Result:6:s1"]
    assert porch.text == RULE_PORCH
    assert porch.has_exception is None
    assert all("Which category" not in rule.text for rule in rules)
    assert all("Please write down" not in rule.text for rule in rules)


def test_loader_accepts_inline_strings_and_can_include_discarded(tmp_path):
    path = tmp_path / "inline.xlsx"
    result, discarded = _fixture_sheets()
    write_xlsx(path, [("Result", result), ("Discarded Data", discarded)], shared=False)
    default = load_study1(path)
    assert len(default) == 4
    both = load_study1(path, sheets=("Result", "Discarded Data"))
    assert any(rule.rule_id == "study1:Discarded Data:3:s1" for rule in both)
    assert len(both) == 5


def test_report_omits_source_text_and_scores_exact_and_structural(workbook):
    rules = load_study1(workbook)
    labels = {
        "study1:Result:3:s1": _policy_label("fan", [("temperature", "<", 60)]),
        "study1:Result:3:s2": _policy_label("light", [("is_home", "==", True)], kind="allow"),
        "study1:Result:9:s9": _policy_label("plug", [("time_hour", ">=", 22)]),
    }
    compiled = {
        "study1:Result:3:s1": _compiled("fan", [("temperature", "<", 60.0)]),
        "study1:Result:3:s2": _compiled("light", [("is_home", "==", True)], kind="allow"),
        "study1:Result:5:s2": _compiled("heater", [("humidity", ">", 60)]),
    }

    def compile_fn(rule):
        return compiled.get(rule.rule_id)

    report = run(rules, labels, compile_fn, compiler="injected")
    blob = json.dumps(report)
    for sentence in (RULE_FAN, RULE_LAMP, RULE_HEATER, RULE_PORCH, EXCEPTION, "Turn the television off when I leave"):
        assert sentence not in blob

    rows = {row["rule_id"]: row for row in report["rows"]}
    assert rows["study1:Result:3:s1"]["label_status"] == "exact_match"
    assert rows["study1:Result:3:s1"]["exact_match"] is True
    assert rows["study1:Result:3:s1"]["structural_match"] is True
    assert rows["study1:Result:3:s1"]["valid"] is True
    assert rows["study1:Result:3:s2"]["label_status"] == "exact_match"
    heater = rows["study1:Result:5:s2"]
    assert heater["label_status"] == "unlabeled"
    assert heater["fidelity_failure"] is False
    assert heater["valid"] is True
    summary = report["summary"]
    assert summary["unlabeled"] == 2
    assert summary["exact_matches"] == 2
    assert summary["fidelity_failures"] == 0
    assert summary["unused_labels"] == ["study1:Result:9:s9"]
    assert summary["compiler"] == "injected"


def test_structural_match_accepts_equivalent_hours_and_exact_does_not():
    expected = _policy_label("light", [("time_hour", ">=", 22)])["policy"]
    same_meaning = _compiled("light", [("time_hour", ">", 21)])
    flipped = _compiled("light", [("time_hour", "<", 22)])
    from policy_authoring.compile_fidelity import assess_compiled
    same = assess_compiled(same_meaning)["policy"]
    wrong = assess_compiled(flipped)["policy"]
    assert exact_match(same, expected) is False
    assert structural_match(same, expected) is True
    assert structural_match(wrong, expected) is False


def test_unlabeled_rejection_is_not_a_fidelity_failure():
    from policy_authoring.compile_fidelity import StudyRule
    rules = [StudyRule("study1:Result:3:s1", "Result", 3, 1, RULE_FAN, "never", False)]
    report = run(rules, {}, lambda _rule: {"rejected": True, "reason": "unsupported device", "confidence": 0.0}, "injected")
    row = report["rows"][0]
    assert row["label_status"] == "unlabeled"
    assert row["fidelity_failure"] is False
    assert row["valid"] is False
    assert row["explicit_rejection"] is True
    assert row["rejection_category"] == "explicit_rejection"
    assert report["summary"]["fidelity_failures"] == 0
    assert report["summary"]["valid_rate"] == 0.0


def test_labels_distinguish_reject_mismatch_and_invalid_dsl():
    from policy_authoring.compile_fidelity import StudyRule
    rules = [
        StudyRule("r-reject", "Result", 3, 1, "Turn the television off when I leave", "never", False),
        StudyRule("r-accept", "Result", 4, 1, "Keep the lamp on when I am home", "always", False),
        StudyRule("r-bad", "Result", 5, 1, "Don't turn on the fan when it is below 60 degrees", "never", False),
        StudyRule("r-none", "Result", 6, 1, "Block the heater when humidity is high", "never", False),
    ]
    labels = {
        "r-reject": {"expectation": "reject"},
        "r-accept": {"expectation": "reject"},
        "r-bad": _policy_label("fan", [("temperature", "<", 60)]),
        "r-none": _policy_label("heater", [("humidity", ">", 60)]),
    }
    outputs = {
        "r-reject": {"rejected": True, "reason": "unsupported device", "confidence": 0.0},
        "r-accept": _compiled("light", [("is_home", "==", True)], kind="allow"),
        "r-bad": _compiled("doorbell", [("temperature", "<", 60)]),
        "r-none": None,
    }
    report = run(rules, labels, lambda rule: outputs[rule.rule_id], "injected")
    status = {row["rule_id"]: row["label_status"] for row in report["rows"]}
    assert status == {
        "r-reject": "expected_reject",
        "r-accept": "unexpected_accept",
        "r-bad": "unexpected_reject",
        "r-none": "no_output",
    }
    failures = {row["rule_id"] for row in report["rows"] if row["fidelity_failure"]}
    assert failures == {"r-accept", "r-bad", "r-none"}
    bad = next(row for row in report["rows"] if row["rule_id"] == "r-bad")
    assert bad["rejection_category"] == "invalid_dsl"
    assert bad["valid"] is False


def test_low_confidence_is_invalid_dsl():
    from policy_authoring.compile_fidelity import StudyRule, assess_compiled
    compiled = _compiled("fan", [("temperature", "<", 60)])
    compiled["confidence"] = 0.5
    assessed = assess_compiled(compiled)
    assert assessed["valid"] is False
    assert assessed["rejection_category"] == "invalid_dsl"
    rule = StudyRule("r", "Result", 3, 1, RULE_FAN, "never", False)
    report = run([rule], {"r": _policy_label("fan", [("temperature", "<", 60)])}, lambda _rule: compiled, "injected")
    assert report["rows"][0]["label_status"] == "unexpected_reject"


def test_labels_file_rejects_source_text_and_the_committed_file_is_empty(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({
        "version": 1,
        "labels": {"study1:Result:3:s1": {"expectation": "reject", "source_text": RULE_FAN}},
    }))
    with pytest.raises(LabelError, match="source_text"):
        load_labels(path)
    committed = load_labels(ROOT / "benchmarks" / "compile_fidelity" / "labels.json")
    assert committed == {}


def test_label_policy_must_be_valid_dsl(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({
        "version": 1,
        "labels": {
            "study1:Result:3:s1": {
                "expectation": "policy",
                "policy": {
                    "scope": {"device_type": "doorbell"},
                    "conditions": [{"field": "temperature", "operator": "<", "value": 60}],
                    "action": {"type": "block"},
                },
            }
        },
    }))
    with pytest.raises(LabelError, match="doorbell"):
        load_labels(path)


def test_cache_and_xlsx_cannot_be_committed():
    assert_dataset_not_tracked(ROOT)
    assert_safe_dataset_destination(study1_workbook_path(), ROOT)
    assert_safe_report_destination(ROOT / ".cache" / "compile_fidelity" / "report.json", ROOT)
    with pytest.raises(DatasetGuardError):
        assert_safe_dataset_destination(ROOT / "Data - User Study 1.xlsx", ROOT)
    with pytest.raises(DatasetGuardError):
        assert_safe_report_destination(ROOT / "report.json", ROOT)
    with pytest.raises(DatasetGuardError):
        assert_safe_dataset_destination(ROOT / "benchmarks" / "leak.xlsx", ROOT)


def test_fetch_refuses_to_write_outside_the_cache(capsys):
    code = FETCH.main(["--dest", str(ROOT / "Data - User Study 1.xlsx")])
    assert code == 2
    assert "fetch refused" in capsys.readouterr().err
    assert "raw.githubusercontent.com/zlfben/autotap/master/" in FETCH.STUDY1_URL
    assert FETCH.STUDY1_URL.endswith("Data%20-%20User%20Study%201.xlsx")


def test_fetch_writes_only_the_ignored_cache(monkeypatch, tmp_path):
    dest = study1_workbook_path()
    payload = b"PK\x03\x04synthetic-cache-bytes"

    class _Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(FETCH.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    try:
        FETCH.fetch("http://example.invalid/study.xlsx", dest)
        assert dest.read_bytes() == payload
        assert_dataset_not_tracked(ROOT)
        assert_safe_dataset_destination(dest, ROOT)
    finally:
        dest.unlink(missing_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        partial.unlink(missing_ok=True)


def test_runner_refuses_a_live_model_unless_opted_in(capsys, workbook):
    with pytest.raises(SystemExit) as caught:
        RUNNER.main(["--xlsx", str(workbook)])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "--live" in err
    assert "opt in" in err


def test_runner_scores_responses_without_a_model(workbook):
    responses = ROOT / ".cache" / "compile_fidelity" / "pytest-responses.jsonl"
    out = ROOT / ".cache" / "compile_fidelity" / "pytest-out"
    responses.parent.mkdir(parents=True, exist_ok=True)
    fan = _compiled("fan", [("temperature", "<", 60)])
    responses.write_text(json.dumps({
        "id": "study1:Result:3:s1",
        "compiled": fan,
    }) + "\n")
    try:
        code = RUNNER.main([
            "--xlsx", str(workbook),
            "--responses", str(responses),
            "--out", str(out),
            "--labels", str(ROOT / "benchmarks" / "compile_fidelity" / "labels.json"),
            "--limit", "1",
        ])
        assert code == 0
        report = json.loads((out / "report.json").read_text())
        assert report["summary"]["compiler"] == "responses"
        assert report["summary"]["n"] == 1
        assert report["rows"][0]["label_status"] == "unlabeled"
        assert report["rows"][0]["valid"] is True
        assert RULE_FAN not in (out / "report.json").read_text()
        assert "DeviceWeave compile-fidelity" in (out / "report.txt").read_text()
        assert_dataset_not_tracked(ROOT)
    finally:
        responses.unlink(missing_ok=True)
        if out.exists():
            for child in out.iterdir():
                child.unlink()
            out.rmdir()


def test_write_report_round_trip_stays_untracked(workbook):
    rules = load_study1(workbook)
    report = run(rules, {}, lambda rule: _compiled("fan", [("temperature", "<", 60)]), "injected")
    out = ROOT / ".cache" / "compile_fidelity" / "pytest-roundtrip"
    try:
        json_path, text_path = write_report(report, out)
        assert json_path.is_file() and text_path.is_file()
        assert RULE_FAN not in json_path.read_text()
        assert_dataset_not_tracked(ROOT)
    finally:
        if out.exists():
            for child in out.iterdir():
                child.unlink()
            out.rmdir()
