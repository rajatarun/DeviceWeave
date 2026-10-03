"""Hand-coded properties: codebook checks, agreement, plant outcomes, analysis and rendering.

Every statement and code here is invented. None is taken or paraphrased from
AutoTap's Study 1 (Zhang, He, et al., ICSE 2019,
https://ieeexplore.ieee.org/abstract/document/8811900; data at
https://github.com/zlfben/autotap), which this repository does not redistribute.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("property_coding", ROOT / "scripts" / "property_coding.py")
PC = importlib.util.module_from_spec(_spec)
sys.modules["property_coding"] = PC
_spec.loader.exec_module(PC)

from tests.test_compile_fidelity import (  # noqa: E402
    ALWAYS, NEVER, NO_EXC, R_FAN, R_HEATER, R_PORCH, _header, _participant, write_xlsx,
)

FLAGS = dict(duration=False, within_after=False, multi_condition=False,
             exception_in_text=False, fits_autotap_template=True)


def code(**kw):
    d = dict(scope="property", **FLAGS)
    d.update(kw)
    return PC.validate_code(d, "test")


HEATER_AWAY = code(kind="state", modality="never", named_polarity="on", target_class="heater",
                   target_actor="system", condition="presence", condition_actor="world")
PORCH_DARK = code(kind="state", modality="always", named_polarity="on", target_class="light",
                  target_actor="system", condition="time_of_day", condition_actor="world")
AC_WINDOW = code(kind="state_pair", modality="never", named_polarity="on", target_class="ac",
                 target_actor="system", condition="device_state", condition_actor="human")
LOCKED_AWAY = code(kind="state", modality="always", named_polarity="off", target_class="lock",
                   target_actor="system", condition="presence", condition_actor="world")
FAN_DURATION = code(kind="state", modality="never", named_polarity="on", target_class="fan",
                    target_actor="system", condition="none", duration=True)
ALARM_BY_COMMAND = code(kind="event", modality="never", named_polarity="on",
                        target_class="camera_security_alarm", target_actor="system",
                        condition="presence", condition_actor="world")
ALARM_BY_WORLD = code(kind="event", modality="never", named_polarity="on",
                      target_class="camera_security_alarm", target_actor="world",
                      condition="presence", condition_actor="world")
LOCK_AFTER_LEAVING = code(kind="event", modality="always", named_polarity="off", target_class="lock",
                          target_actor="system", condition="presence", condition_actor="world",
                          within_after=True)
# A room drifts above the bound by itself; only something that can cool it repairs that.
THERMOSTAT_CAP = code(kind="state", modality="never", named_polarity="on", target_class="thermostat",
                      target_actor="mixed", condition="none")
FRIDGE_ALWAYS_ON = code(kind="state", modality="always", named_polarity="on",
                        target_class="kitchen_appliance", target_actor="system", condition="none")
NOT_A_PROPERTY = PC.validate_code({"scope": "not_a_property", "not_property_reason": "about_product"}, "t")

ARCHS = ("guard_dw", "guard_any", "guard_dw_obligation", "tap", "guard_tap")

# Derived by hand from the plant semantics (reaction on, no manual switch),
# then checked against the synthesis -- not copied from its output.
EXPECTED_DEFAULT = {
    # Leaving is uncontrollable, so a guard can only stay safe by refusing
    # turn-on at home too; a forced turn-off after leaving restores it exactly.
    "heater_away": (HEATER_AWAY, ("over_restrictive", "over_restrictive", "exact", "exact", "exact")),
    # Needs the light switched *on* when it gets dark: only an actuator can.
    "porch_dark": (PORCH_DARK, ("impossible", "impossible", "impossible", "exact", "exact")),
    # A person opens the window: same shape as leaving.
    "ac_window": (AC_WINDOW, ("over_restrictive", "over_restrictive", "exact", "exact", "exact")),
    # "Locked while away" is "never unlocked while away": the repair is the off-direction action.
    "locked_away": (LOCKED_AWAY, ("over_restrictive", "over_restrictive", "exact", "exact", "exact")),
    # A guard cannot end a run that has gone on too long; it can only refuse to start one.
    "fan_duration": (FAN_DURATION, ("over_restrictive", "over_restrictive", "exact", "exact", "exact")),
    # Refusing the command is exactly what a guard does; automation cannot refuse.
    "alarm_command": (ALARM_BY_COMMAND, ("exact", "exact", "exact", "over_restrictive", "exact")),
    # Nobody controls a world event.
    "alarm_world": (ALARM_BY_WORLD, ("impossible",) * 5),
    "lock_after_leaving": (LOCK_AFTER_LEAVING, ("over_restrictive", "over_restrictive", "exact", "exact", "exact")),
    # Only a guard that may refuse turn-off keeps it on; DeviceWeave never refuses turn-off.
    "thermostat_cap": (THERMOSTAT_CAP, ("impossible", "impossible", "exact", "exact", "exact")),
    "fridge_on": (FRIDGE_ALWAYS_ON, ("impossible", "exact", "impossible", "exact", "exact")),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_DEFAULT))
def test_outcomes_match_the_hand_derivation(name):
    prop, expected = EXPECTED_DEFAULT[name]
    got = tuple(PC.outcome_of(prop, a) for a in ARCHS)
    assert got == expected, f"{name}: {dict(zip(ARCHS, got))}"


def test_a_manual_switch_defeats_every_guard_but_not_a_forced_turn_off():
    assert PC.outcome_of(HEATER_AWAY, "guard_dw", manual=True) == "impossible"
    assert PC.outcome_of(HEATER_AWAY, "guard_dw_obligation", manual=True) == "exact"


def test_without_reaction_an_obligation_has_to_act_before_anything_happens():
    assert PC.outcome_of(HEATER_AWAY, "guard_dw_obligation", reaction=False) == "preemptive"
    assert PC.outcome_of(PORCH_DARK, "guard_tap", reaction=False) == "preemptive"
    assert PC.outcome_of(ALARM_BY_COMMAND, "guard_dw", reaction=False) == "exact"


def test_plants_contain_only_reachable_states_and_bad_is_absorbing():
    plant = PC.build_property_plant(HEATER_AWAY, PC.ARCHITECTURES["guard_dw"])
    seen, stack = {plant.initial}, [plant.initial]
    while stack:
        s = stack.pop()
        for _name, _ev, tgt in plant.successors(s):
            if tgt not in seen:
                seen.add(tgt)
                stack.append(tgt)
    assert seen == set(plant.states)
    assert PC.BAD in plant.bad and not plant.successors(PC.BAD)
    assert plant.initial not in plant.bad


def test_every_exact_outcome_has_a_safe_closed_loop():
    from policy_authoring.controllability import supcon_forcing

    for name, (prop, expected) in EXPECTED_DEFAULT.items():
        for arch, want in zip(ARCHS, expected):
            if want != "exact":
                continue
            plant = PC.build_property_plant(prop, PC.ARCHITECTURES[arch])
            syn = supcon_forcing(plant)
            assert syn.closed_loop_safe and not (syn.reachable & plant.bad), (name, arch)


def test_deviceweave_dsl_fit():
    assert PC.deviceweave_dsl_fit(HEATER_AWAY)
    assert not PC.deviceweave_dsl_fit(PORCH_DARK)       # an obligation
    assert not PC.deviceweave_dsl_fit(LOCKED_AWAY)      # a lock
    assert not PC.deviceweave_dsl_fit(FAN_DURATION)     # a duration


# ─────────────────────────────────────────────────────────────────────────────
# Codebook validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad,match", [
    ({"scope": "property", "kind": "state"}, "modality"),
    ({**HEATER_AWAY, "statement": "x"}, "unknown fields"),
    ({**HEATER_AWAY, "condition_actor": None}, "condition_actor"),
    ({**FAN_DURATION, "condition_actor": "world"}, "empty when condition is none"),
    ({**AC_WINDOW, "condition": "presence"}, "state_pair"),
    ({**HEATER_AWAY, "within_after": True}, "event statements only"),
    ({**HEATER_AWAY, "duration": "maybe"}, "yes/no"),
    ({"scope": "not_a_property", "not_property_reason": "vague", "kind": "state"}, "no property fields"),
    ({"scope": "not_a_property"}, "not_property_reason"),
    ({**HEATER_AWAY, "target_class": "toaster"}, "target_class"),
])
def test_codes_that_break_the_codebook_are_refused(bad, match):
    with pytest.raises(PC.CodingError, match=match):
        PC.validate_code(bad, "t")


def test_code_files_round_trip_and_reject_text_keys(tmp_path):
    path = tmp_path / "a.json"
    PC.write_codes(path, "A", {"study1:Result:row3:stmt1": HEATER_AWAY,
                               "study1:Result:row3:stmt2": NOT_A_PROPERTY})
    loaded = PC.load_codes(path)
    assert loaded["coder"] == "A" and loaded["codes"]["study1:Result:row3:stmt1"] == HEATER_AWAY
    doc = json.loads(path.read_text())
    doc["codes"]["Never run the heater"] = HEATER_AWAY
    path.write_text(json.dumps(doc))
    with pytest.raises(PC.CodingError, match="not a rule id"):
        PC.load_codes(path)


# ─────────────────────────────────────────────────────────────────────────────
# Worksheets
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def study_xlsx(tmp_path):
    header = _header(3)
    return write_xlsx(tmp_path / "study.xlsx", {"Result": {
        1: header, 2: ["q"] * len(header),
        3: _participant((R_HEATER, NEVER, NO_EXC), (R_PORCH, ALWAYS, NO_EXC), (R_FAN, NEVER, NO_EXC)),
        4: _participant((R_PORCH, ALWAYS, NO_EXC), ("", "", ""), ("", "", "")),
    }})


def test_pilot_subset_is_shared_and_order_differs(study_xlsx):
    rules = PC.cf.load_rules(study_xlsx)
    a = PC.select_rules(rules, 3, sample_seed=1, order_seed=10)
    b = PC.select_rules(rules, 3, sample_seed=1, order_seed=99)
    assert {r.rule_id for r in a} == {r.rule_id for r in b} and len(a) == 3


def test_worksheet_round_trip_never_puts_text_in_codes(study_xlsx, tmp_path, monkeypatch):
    monkeypatch.setattr(PC.cf, "assert_ignored", lambda p: None)
    rules = PC.cf.load_rules(study_xlsx)
    sheet = PC.write_worksheet(rules, tmp_path / "ws.csv")
    rows = list(csv.DictReader(open(sheet)))
    assert rows[0]["id"].startswith("#") and rows[1]["statement"] == R_HEATER
    for row in rows[1:]:
        if row["id"] == "study1:Result:row3:stmt1":
            row.update({k: ("yes" if v is True else "no" if v is False else v) for k, v in HEATER_AWAY.items()})
        elif row["id"] == "study1:Result:row3:stmt2":
            row.update(scope="not_a_property", not_property_reason="vague")
    with open(sheet, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PC.WORKSHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(PC.CodingError, match="not coded yet"):
        PC.import_worksheet(sheet)
    codes, uncoded = PC.import_worksheet(sheet, allow_incomplete=True)
    assert codes["study1:Result:row3:stmt1"] == HEATER_AWAY
    assert len(uncoded) == 2
    out = tmp_path / "codes.json"
    PC.write_codes(out, "A", codes)
    assert R_HEATER not in out.read_text() and R_PORCH not in out.read_text()


def test_worksheets_refuse_tracked_paths(study_xlsx):
    rules = PC.cf.load_rules(study_xlsx)
    with pytest.raises(PC.cf.FidelityError, match="not git-ignored"):
        PC.write_worksheet(rules, ROOT / "docs" / "paper" / "ws.csv")


def test_a_note_repeating_the_statement_is_caught(study_xlsx):
    rules = PC.cf.load_rules(study_xlsx)
    codes = {"study1:Result:row3:stmt1": {**HEATER_AWAY, "note": R_HEATER.lower()},
             "study1:Result:row3:stmt2": {**PORCH_DARK, "note": "obligation; needs actuation"}}
    assert PC.notes_leaking_text(rules, codes) == ["study1:Result:row3:stmt1"]


# ─────────────────────────────────────────────────────────────────────────────
# Agreement and adjudication
# ─────────────────────────────────────────────────────────────────────────────

def test_cohen_kappa_known_values():
    # 2x2 table [[20, 5], [10, 15]]: po = 0.70, pe = 0.50, kappa = 0.40.
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert PC.cohen_kappa(a, b) == pytest.approx(0.4)
    assert PC.cohen_kappa(a, a) == pytest.approx(1.0)
    assert PC.cohen_kappa(["x"] * 5, ["x"] * 5) is None     # chance agreement is 1
    with pytest.raises(ValueError):
        PC.cohen_kappa(["x"], ["x", "y"])


def test_wilson_interval_known_values():
    assert PC.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert PC.wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert PC.wilson(0, 0) is None


def _ids(n):
    return [f"study1:Result:row{3 + i}:stmt1" for i in range(n)]


def test_agreement_reports_fields_outcomes_and_disagreements():
    ids = _ids(4)
    a = {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK, ids[2]: NOT_A_PROPERTY, ids[3]: FAN_DURATION}
    b = {ids[0]: HEATER_AWAY, ids[1]: {**PORCH_DARK, "condition": "weather"},
         ids[2]: NOT_A_PROPERTY, ids[3]: {**FAN_DURATION, "duration": False}}
    rep = PC.agreement(a, b)
    assert rep["n"] == 4 and rep["fields"]["scope"]["kappa"] == pytest.approx(1.0)
    assert rep["fields"]["condition"]["percentAgreement"] == pytest.approx(2 / 3, abs=1e-4)
    assert rep["disagreements"] == [ids[1], ids[3]]
    assert set(rep["derivedOutcomes"]) == set(ARCHS)


def test_adjudication_needs_a_resolution_for_every_disagreement():
    ids = _ids(2)
    a = {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK}
    b = {ids[0]: HEATER_AWAY, ids[1]: LOCKED_AWAY}
    with pytest.raises(PC.CodingError, match="no resolution"):
        PC.adjudicate(a, b, {})
    gold = PC.adjudicate(a, b, {ids[1]: PORCH_DARK})
    assert gold == {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK}
    with pytest.raises(PC.CodingError, match="same statements"):
        PC.adjudicate(a, {ids[0]: HEATER_AWAY}, {})


# ─────────────────────────────────────────────────────────────────────────────
# Analysis and rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_analysis_shares_and_the_compile_cross_tab():
    ids = _ids(5)
    gold = {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK, ids[2]: ALARM_BY_COMMAND,
            ids[3]: NOT_A_PROPERTY, ids[4]: LOCKED_AWAY}
    rows = {ids[0]: {"id": ids[0], "accepted": True}, ids[2]: {"id": ids[2], "accepted": True},
            ids[1]: {"id": ids[1], "accepted": False}}
    res = PC.analyze(gold, rows, total_statements=690)
    assert res["properties"] == {"k": 4, "n": 5, "share": 0.8, "ci95": PC.wilson(4, 5)}
    default = res["outcomes"]["reaction=on,manual=off"]
    assert default["guard_dw"]["exact"]["k"] == 1          # only the command-refusal property
    assert default["guard_dw"]["notExact"]["k"] == 3
    assert default["tap"]["exact"]["k"] == 3
    assert res["fitsDeviceweaveDsl"]["k"] == 1
    ct = res["compileCrossTab"]
    assert ct["accepted"]["k"] == 2
    # The heater rule compiled and was accepted, yet a refusal-only guard does not enforce it.
    assert ct["acceptedNotEnforcedByGuard"]["k"] == 1
    assert set(res["outcomes"]) == {"reaction=on,manual=off", "reaction=on,manual=on",
                                    "reaction=off,manual=off", "reaction=off,manual=on"}


def test_render_fills_placeholders_and_reports_missing_ones():
    results = {"properties": {"k": 4, "n": 5, "share": 0.8, "ci95": (0.376, 0.964)}, "n": 690}
    text, missing = PC.render("{{properties|pct}} {{properties|ci}} of {{n}} ({{properties|int}}) {{nope|pct}}",
                              results)
    assert text == "80.0% [37.6%, 96.4%] of 690 (4) [[MISSING nope]]"
    assert missing == ["nope"]


def test_the_draft_only_uses_keys_analyze_produces():
    """Every placeholder in docs/paper/draft.md must be filled by a full analyze run."""
    ids = _ids(3)
    a = {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK, ids[2]: NOT_A_PROPERTY}
    b = {ids[0]: HEATER_AWAY, ids[1]: LOCKED_AWAY, ids[2]: NOT_A_PROPERTY}
    rows = {ids[0]: {"id": ids[0], "compile": "compiled", "valid": True, "accepted": True},
            ids[1]: {"id": ids[1], "compile": "refused", "valid": False, "accepted": False}}
    ver = PC.verification({rid: {"code": c, "blind": rid == ids[0]} for rid, c in a.items()}, b, [ids[0]])
    res = PC.analyze(a, rows, total_statements=690, agreement_report=PC.agreement(a, b),
                     verification_report=ver["verification"])
    _text, missing = PC.render((ROOT / "docs" / "paper" / "draft.md").read_text(), res)
    assert not missing, f"draft.md uses keys analyze does not produce: {sorted(set(missing))}"


def test_cli_analyze_and_render(tmp_path, capsys):
    ids = _ids(2)
    gold = tmp_path / "gold.json"
    PC.write_codes(gold, "gold", {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK})
    out = tmp_path / "results.json"
    assert PC.main(["analyze", str(gold), "--out", str(out)]) == 0
    tpl = tmp_path / "t.md"
    tpl.write_text("guard misses {{headline.guardDwNotExact|pct}}")
    rendered = tmp_path / "r.md"
    assert PC.main(["render", str(out), "--template", str(tpl), "--out", str(rendered)]) == 0
    assert rendered.read_text() == "guard misses 100.0%"


# ─────────────────────────────────────────────────────────────────────────────
# Human verification of LLM codes
# ─────────────────────────────────────────────────────────────────────────────

def _verification_fixture():
    ids = _ids(4)
    llm = {ids[0]: HEATER_AWAY, ids[1]: PORCH_DARK, ids[2]: LOCKED_AWAY, ids[3]: NOT_A_PROPERTY}
    answers = {
        ids[0]: {"code": HEATER_AWAY, "blind": True, "seconds": 20},                       # blind, agrees
        ids[1]: {"code": {**PORCH_DARK, "condition": "weather"}, "blind": True, "seconds": 30},  # blind, differs
        ids[2]: {"code": {**LOCKED_AWAY, "duration": True}, "blind": False, "seconds": 5},  # suggestion changed
        ids[3]: {"code": NOT_A_PROPERTY, "blind": False, "seconds": 3},                    # suggestion kept
    }
    return ids, llm, answers


def test_verification_gives_gold_blind_agreement_and_change_rates():
    ids, llm, answers = _verification_fixture()
    res = PC.verification(answers, llm, blind_ids=[ids[0], ids[1]])
    assert res["gold"][ids[1]]["condition"] == "weather"          # the human's answer wins
    ag = res["agreement"]
    assert ag["n"] == 2 and ag["fields"]["condition"]["percentAgreement"] == 0.5
    ver = res["verification"]
    assert ver["blindAnswered"] == 2 and ver["suggestionsAnswered"] == 2
    assert ver["suggestionsChanged"]["k"] == 1
    assert ver["changedByField"]["duration"]["k"] == 1 and ver["changedByField"]["kind"]["k"] == 0


def test_verification_refuses_a_blind_flag_that_disagrees_with_the_list():
    ids, llm, answers = _verification_fixture()
    with pytest.raises(PC.CodingError, match="blind"):
        PC.verification(answers, llm, blind_ids=[ids[0]])


def test_answers_load_from_an_export_directory_in_either_shape(tmp_path):
    ids, _llm, answers = _verification_fixture()
    d = tmp_path / "answers"
    d.mkdir()
    (d / f"{ids[0]}.json").write_text(json.dumps(answers[ids[0]]))                         # raw document
    (d / "x.json").write_text(json.dumps({"id": ids[1], "version": 3, "data": answers[ids[1]]}))  # wrapped
    loaded = PC.load_answers(d)
    assert set(loaded) == {ids[0], ids[1]} and loaded[ids[1]]["blind"] is True
    (d / "bad.json").write_text(json.dumps({"id": ids[2], "data": {"code": {"scope": "maybe"}}}))
    with pytest.raises(PC.CodingError):
        PC.load_answers(d)


def test_frozen_llm_codes_cover_the_corpus_and_hold_no_text():
    path = ROOT / "benchmarks" / "autotap" / "coding" / "llm.json"
    loaded = PC.load_codes(path)
    assert loaded["coder"] == "llm" and len(loaded["codes"]) == 690
    blind = json.loads((ROOT / "benchmarks" / "autotap" / "coding" / "blind_ids.json").read_text())
    assert len(blind["ids"]) == 138 and set(blind["ids"]) <= set(loaded["codes"])
    assert all(set(c) <= PC.CODE_KEYS and "note" not in c for c in loaded["codes"].values())
