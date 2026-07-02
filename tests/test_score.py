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
    assert set(payload) == {"total", "components", "counts", "details"}


# -- explained scores (details) -----------------------------------------------

# 60% ground-floor coverage: the same rooms as FIXED inside a bigger envelope.
SPARSE = FIXED.replace("envelope 30 x 24", "envelope 40 x 30")

# A third of the interior is corridor — the circulation penalty has a cause.
HALLY = """\
plan "Hally"
envelope 40 x 24
ceiling 9
room living: living at 0,0 size 15 x 24
room bed: bedroom at 15,0 size 12 x 24
room hall: hallway at 27,0 size 13 x 24
door living - bed width 2.67
door bed - hall width 2.67
entry living south width 3 offset 4
window bed south width 4 offset 3
window living south width 6 offset 8
"""

# Four elongated offices: the cause caps at three offenders plus a "+1 more".
RATIOS = """\
plan "Ratios"
envelope 32 x 24
ceiling 9
room a: office at 0,0 size 8 x 20
room b: office at 8,0 size 8 x 20
room c: office at 16,0 size 8 x 20
room d: office at 24,0 size 8 x 20
"""


def test_details_cover_exactly_the_nonzero_continuous_components():
    """A cause exists iff its component deducts — no phantom or missing causes."""
    for src in (FIXED, WARNED, SPARSE, HALLY, RATIOS):
        report = design_score(compile_source(src))
        expected = {
            k
            for k in ("space", "circulation", "proportion", "daylight")
            if report.components[k]
        }
        assert set(report.details) == expected, src


def test_details_never_touch_the_arithmetic():
    """WARNED differs from FIXED by one diagnostic; every continuous component
    (and so every detail-backed number) stays byte-identical."""
    fixed = design_score(compile_source(FIXED))
    warned = design_score(compile_source(WARNED))
    continuous = ("space", "circulation", "proportion", "daylight")
    assert [fixed.components[k] for k in continuous] == [
        warned.components[k] for k in continuous
    ]


def test_proportion_detail_names_the_elongated_room_with_its_ratio():
    report = design_score(compile_source(FIXED))
    assert report.components["proportion"] > 0
    assert "bed is 2.0:1" in report.details["proportion"]
    assert "1.6:1" in report.details["proportion"]  # the target it overshoots


def test_proportion_detail_orders_worst_first_and_caps_the_list():
    report = design_score(compile_source(RATIOS))
    detail = report.details["proportion"]
    assert detail.startswith("a is 2.5:1, b is 2.5:1, c is 2.5:1")
    assert "+1 more" in detail and "d is" not in detail


def test_daylight_detail_names_underglazed_rooms_with_percentages():
    report = design_score(compile_source(FIXED))
    detail = report.details["daylight"]
    assert "living at 5.1%" in detail and "bed at 5.1%" in detail
    assert "8%" in detail  # the glazing floor being missed


def test_space_detail_reports_coverage_and_unassigned_area():
    report = design_score(compile_source(SPARSE))
    assert report.components["space"] > 0
    detail = report.details["space"]
    assert "60%" in detail and "480 sqft unassigned" in detail


def test_circulation_detail_names_the_hallways_with_areas():
    report = design_score(compile_source(HALLY))
    assert report.components["circulation"] > 0
    detail = report.details["circulation"]
    assert "hall 312 sqft" in detail and "%" in detail


def test_details_are_deterministic_and_ordered():
    a = design_score(compile_source(SPARSE))
    b = design_score(compile_source(SPARSE))
    assert a.details == b.details
    assert list(a.details) == list(b.details)  # insertion order is the contract
    # Components order: the continuous causes follow the components' own order.
    order = [k for k in a.components if k in a.details]
    assert list(a.details) == order


def test_no_plan_has_no_details():
    report = design_score(compile_source("not a plan at all"))
    assert report.details == {}


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


def test_cli_score_human_table_shows_causes(tmp_path, capsys):
    p = tmp_path / "sparse.barn"
    p.write_text(SPARSE)
    assert main(["score", str(p)]) == 0
    out = capsys.readouterr().out
    assert "— ground-floor rooms cover 60%" in out
    assert "bed is 2.0:1" in out


def test_cli_score_json_includes_details(tmp_path, capsys):
    p = tmp_path / "sparse.barn"
    p.write_text(SPARSE)
    assert main(["score", str(p), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "sqft unassigned" in payload["details"]["space"]


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
