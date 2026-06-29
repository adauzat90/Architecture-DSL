"""Tests for the pure stair-run planner (`barndsl.plan_stair_runs`).

The planner is the testable half of the multi-flight stair feature: it decides
straight vs. switchback vs. overrun and lays out the flights deterministically,
so the (untestable) Revit builder just instantiates what it returns.
"""

from __future__ import annotations

import math

import pytest

from barndsl import compile_source, plan_stair_runs, to_revit_model
from barndsl.revit import MAX_RISER_HEIGHT, MIN_STAIR_WIDTH, MIN_TREAD_DEPTH


def test_straight_run_when_footprint_is_long_enough():
    # 9 ft rise → 14 risers → 13 treads × 10in ≈ 10.8 ft of run; a 4×14 fits it.
    p = plan_stair_runs(0, 0, 4, 14, rise=9.0)
    assert p["layout"] == "straight"
    assert p["fits"] is True
    assert len(p["runs"]) == 1
    assert p["runs"][0]["risers"] == p["risers"]


def test_riser_count_respects_max_riser():
    p = plan_stair_runs(0, 0, 4, 14, rise=9.0)
    assert p["risers"] == math.ceil(9.0 / MAX_RISER_HEIGHT)
    assert p["riser_height"] <= MAX_RISER_HEIGHT + 1e-9
    assert p["riser_height"] * p["risers"] == pytest.approx(9.0)


def test_switchback_when_long_axis_too_short_but_wide_enough():
    # Run needs ~10.8 ft, but the footprint is only 7 long — and 7 wide ≥ 2×3 ft,
    # so it folds into a switchback: two flights + a landing.
    p = plan_stair_runs(0, 0, 7, 7, rise=9.0)
    assert p["layout"] == "switchback"
    assert p["fits"] is True
    assert len(p["runs"]) == 2
    # The two flights together climb every riser.
    assert sum(r["risers"] for r in p["runs"]) == p["risers"]
    assert len(p["landings"]) == 1
    # Each flight is at least the code-minimum width.
    for r in p["runs"]:
        assert r["width"] >= MIN_STAIR_WIDTH - 1e-9


def test_overrun_when_neither_straight_nor_switchback_fits():
    # Narrow and short: can't fit a straight run, can't fold a switchback.
    p = plan_stair_runs(0, 0, 3.5, 4, rise=9.0)
    assert p["layout"] == "overrun"
    assert p["fits"] is False
    assert len(p["runs"]) == 1


def test_runs_start_within_the_footprint():
    p = plan_stair_runs(10, 20, 4, 14, rise=9.0)
    for r in p["runs"]:
        for px, py in (r["start"], r["end"]):
            assert 10 - 1e-6 <= px <= 10 + 4 + 1e-6
            assert 20 - 1e-6 <= py <= 20 + 14 + 1e-6


def test_run_length_matches_tread_geometry():
    p = plan_stair_runs(0, 0, 4, 30, rise=9.0)
    r = p["runs"][0]
    span = math.hypot(r["end"][0] - r["start"][0], r["end"][1] - r["start"][1])
    treads = max(1, p["risers"] - 1)
    assert span == pytest.approx(treads * MIN_TREAD_DEPTH)


def test_orientation_follows_long_axis():
    # Wide footprint (width > length): the run goes east-west.
    p = plan_stair_runs(0, 0, 14, 4, rise=9.0)
    r = p["runs"][0]
    assert abs(r["end"][0] - r["start"][0]) > abs(r["end"][1] - r["start"][1])


def test_stair_plan_rides_in_the_exchange():
    src = """\
plan "Two Story"
envelope 39 x 33
ceiling 9
room living: living at 0,0 size 24 x 18
room loft: loft at 0,0 size 24 x 18 level 1
stair flight at 0,3 size 4 x 12 from 0 to 1
entry living south width 3 offset 10
window living south width 6 offset 2
"""
    plan = compile_source(src).plan
    model = to_revit_model(plan)
    stair = [a for a in model.areas if a.kind == "stair"][0]
    assert "plan" in stair.meta
    sp = stair.meta["plan"]
    assert sp["layout"] in ("straight", "switchback", "overrun")
    assert sp["risers"] >= 1
    assert stair.meta["rise"] == pytest.approx(9.0)
