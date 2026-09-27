"""DeviceWeave's scores against the shared score contract (contracts/score_envelope.json).

The contract and its checker are vendored byte-identical from ContextWeave
(the sha256 pins below are the same in every copy). This file holds
DeviceWeave's declaration of its scores (contracts/scores.json) to it and
resolves every producer.
"""
from __future__ import annotations

import os
# ── shared core: identical in every repository that vendors the contract ──────
import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from contracts import score_contract as SC  # noqa: E402

#: sha256 of the vendored files. The same constants are pinned in ContextWeave,
#: DeviceWeave, CipherWeave and mcp-observatory, so a copy edited in one
#: repository fails that repository's tests instead of drifting silently.
CONTRACT_SHA256 = "159d96c663ec824e9b685f011456a537774550660cf55d0808bd6110b907e27c"
MODULE_SHA256 = "b524d8b33946d6408cc178edc16597d2693a37ee2cf6997f7d0558e01567bf43"


def test_vendored_contract_is_the_shared_one():
    assert hashlib.sha256((REPO / "contracts" / "score_envelope.json").read_bytes()).hexdigest() == CONTRACT_SHA256
    assert hashlib.sha256((REPO / "contracts" / "score_contract.py").read_bytes()).hexdigest() == MODULE_SHA256


def _env(kind, name="x", calibrated=False, **kw):
    return {"value": 0.5, "observed": True, "kind": kind, "name": name, "source": "measurement",
            "calibrated": calibrated, **kw}


def test_combination_rules():
    p1 = _env("probability", "a", True, event="E1")
    p2 = _env("probability", "b", True, event="E2")
    assert SC.can_combine(p1, p2, "multiply")[0]
    assert not SC.can_combine(p1, p2, "average")[0], "probabilities of different events"
    s1, s2 = _env("score", "a"), _env("score", "b")
    ok, why = SC.can_combine(s1, s2, "multiply")
    assert not ok and why.startswith("R1")
    ok, why = SC.can_combine(_env("similarity", "cos"), _env("score", "beh"), "average")
    assert not ok and why.startswith("R3"), "the DeviceWeave final_score pattern"
    assert SC.can_combine(s1, dict(s1), "average")[0]
    assert not SC.can_combine({**s1, "observed": False, "value": None}, s1, "average")[0]


def test_unobserved_must_be_null_and_flagged():
    entry = {"name": "n", "kind": "score", "range": [0, 1], "source": "self", "calibrated": False,
             "meaning": "m", "producer": "json:dumps"}
    assert SC.check_envelope(SC.envelope(None, entry), entry=entry) == []
    assert SC.check_envelope({**SC.envelope(None, entry), "observed": True}, entry=entry)
    assert SC.check_envelope(SC.envelope(1.5, entry), entry=entry), "out of declared range"


def test_an_uncalibrated_probability_is_refused():
    bad = {"name": "p", "kind": "probability", "range": [0, 1], "source": "model", "calibrated": False,
           "meaning": "m", "producer": "json:dumps", "event": "e"}
    assert any("R2" in p for p in SC.check_registry([bad]))


def test_calibration_tools():
    perfect = [(0.1, 0), (0.1, 0), (0.9, 1), (0.9, 1)]
    assert SC.calibration_report(perfect)["brier"] == pytest.approx(0.01)
    overconfident = [(0.9, 1), (0.9, 0), (0.9, 0), (0.9, 0)]
    assert SC.calibration_report(overconfident)["ece"] == pytest.approx(0.65)
    fit = SC.isotonic_fit([(0.1, 1), (0.2, 0), (0.3, 1), (0.8, 1), (0.9, 1)])
    values = [v for _, v in fit]
    assert values == sorted(values), "isotonic fit must be monotone"
    assert SC.apply_isotonic(fit, 0.15) == pytest.approx(0.5)
    assert SC.apply_isotonic(fit, 0.95) == 1.0


# ── DeviceWeave ──────────────────────────────────────────────────────────────

def test_registry_conforms_and_every_producer_exists():
    assert SC.check_registry(SC.load_registry()) == []


def test_final_score_is_the_pattern_the_contract_forbids_across_producers():
    """compute_score averages a similarity with a heuristic score. Declared as an
    ordering-only score, it cannot be combined with any other system's number."""
    reg = {e["name"]: e for e in SC.load_registry()}
    cos = SC.envelope(0.6, reg["deviceweave.cosine_score"])
    beh = SC.envelope(0.5, reg["deviceweave.behavior_score"])
    ok, why = SC.can_combine(cos, beh, "average")
    assert not ok and why.startswith("R3")
    final = SC.envelope(0.55, reg["deviceweave.final_score"])
    assert SC.check_envelope(final, entry=reg["deviceweave.final_score"]) == []


def test_cosine_producer_stays_inside_its_declared_range():
    import random
    from device_resolver import _cosine_similarity
    reg = {e["name"]: e for e in SC.load_registry()}["deviceweave.cosine_score"]
    rng = random.Random(0)
    for _ in range(200):
        a = [rng.random() for _ in range(6)]
        b = [rng.random() for _ in range(6)]
        env = SC.envelope(_cosine_similarity(a, b), reg)
        assert SC.check_envelope(env, entry=reg) == []
