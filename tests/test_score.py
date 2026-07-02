"""The design score: deterministic 0-100, errors gate to 0, warnings cost.

The score is a contract (see the `barndsl/score.py` module docstring): curated
gallery plans must stay high, introducing a diagnostic must cost points, an
unbuildable plan is worth nothing, and the same plan always scores the same —
otherwise an agent can't hill-climb on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from barndsl import compile_file, compile_source, design_score
from barndsl.cli import main

GALLERY = Path(__file__).resolve().parent.parent / "examples" / "gallery"
PLANS = sorted(GALLERY.glob("*.barn"))
CEDAR_RIDGE = GALLERY.parent / "cedar_ridge.barn"

# A small clean-compiling two-room plan; WARNED narrows its interior door below
# the code minimum, which adds exactly one warning (the geometry is unchanged,
# so every continuous component stays put — only the warning penalty moves).
FIXED = """\
plan "Score Fixture"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 18 x 24
room bed: bedroom at 18,0 size 12 x 24
door living - bed width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""
WARNED = FIXED.replace("door living - bed width 2.67", "door living - bed width 1.5")

# Doesn't compile to a plan-with-no-errors: the bedroom overflows the envelope.
BROKEN = """\
plan "Broken"
envelope 20 x 20
room bed: bedroom at 0,0 size 30 x 30
"""


# -- the contract -----------------------------------------------------------


@pytest.mark.parametrize("path", PLANS, ids=lambda p: p.stem)
def test_gallery_plans_score_high(path: Path):
    """Curated 0/0/0 plans keep at least 85 — the score can't punish clean work."""
    report = design_score(compile_file(str(path)))
    assert report.total >= 85.0, report.to_dict()
    assert report.total <= 100.0


def test_a_warning_strictly_lowers_the_score():
    fixed = design_score(compile_source(FIXED))
    warned = design_score(compile_source(WARNED))
    # The edit introduces one more warning and changes nothing else.
    assert warned.counts["warning"] == fixed.counts["warning"] + 1
    assert warned.total < fixed.total


def test_errors_score_zero():
    result = compile_source(BROKEN)
    assert result.errors
    report = design_score(result)
    assert report.total == 0.0
    assert report.components["errors"] == 100.0


def test_no_plan_scores_zero():
    result = compile_source("not a plan at all")
    assert result.plan is None
    assert design_score(result).total == 0.0


def test_score_is_deterministic():
    a = design_score(compile_source(FIXED))
    b = design_score(compile_source(FIXED))
    assert a.total == b.total
    assert a.components == b.components
    assert a.counts == b.counts


def test_to_dict_round_trips_via_json():
    report = design_score(compile_file(str(CEDAR_RIDGE)))
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload == report.to_dict()
    assert set(payload) == {"total", "components", "counts"}


# -- CLI ---------------------------------------------------------------------


def test_cli_score_json(capsys):
    rc = main(["score", str(CEDAR_RIDGE), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert 0.0 <= payload["total"] <= 100.0
    assert "components" in payload and "counts" in payload


def test_cli_score_human_table(tmp_path, capsys):
    p = tmp_path / "fixed.barn"
    p.write_text(FIXED)
    assert main(["score", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Design score:" in out and "/ 100" in out


def test_cli_score_fails_on_no_plan(tmp_path, capsys):
    p = tmp_path / "bad.barn"
    p.write_text("nonsense\n")
    assert main(["score", str(p)]) == 1


def test_compile_json_includes_score(tmp_path, capsys):
    p = tmp_path / "fixed.barn"
    p.write_text(FIXED)
    main(["compile", str(p), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert 0.0 <= payload["score"]["total"] <= 100.0
    # Additive: the pre-score keys are untouched.
    assert "diagnostics" in payload and "counts" in payload


def test_build_json_includes_score(tmp_path, capsys):
    p = tmp_path / "fixed.barn"
    p.write_text(FIXED)
    out = tmp_path / "plan.svg"
    main(["build", str(p), "--json", "--out", str(out)])
    payload = json.loads(capsys.readouterr().out)
    assert 0.0 <= payload["score"]["total"] <= 100.0
    assert "metrics" in payload
