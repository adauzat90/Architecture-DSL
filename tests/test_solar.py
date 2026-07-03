"""Tests for the solar-orientation feature (site & solar, Phase 1).

The `orientation` directive is now *real*: it drives two INFO glazing nudges
(northern hemisphere) and a north-arrow on the render. Both are gated on the plan
actually declaring an orientation, so an unsited plan (`orientation is None`) is
never nagged.

- true_azimuth / solar_sector  — the plan-frame → compass bridge (pure).
- SOLAR_WEST_GAIN              — a room with a lot of west-facing glass.
- SOLAR_NORTH_ONLY            — a sun-wanting room glazed only to the north.
- metrics / emit / render      — glazing split, round-trip, compass rosette.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl, render_svg
from barndsl import Direction as D, RoomType as T
from barndsl.solar import compass_label, solar_sector, true_azimuth, wall_sector


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- the compass bridge (pure) ----------------------------------------------


def test_true_azimuth_at_north_up():
    # orientation 0: plan walls face their true cardinal directly.
    assert true_azimuth(D.NORTH, 0) == 0
    assert true_azimuth(D.EAST, 0) == 90
    assert true_azimuth(D.SOUTH, 0) == 180
    assert true_azimuth(D.WEST, 0) == 270


def test_true_azimuth_rotates_with_orientation():
    # plan-north points true-east (θ=90) — the whole plan rotates 90° clockwise:
    assert true_azimuth(D.NORTH, 90) == 90   # plan-north → true east
    assert true_azimuth(D.EAST, 90) == 180   # plan-east  → true south
    assert true_azimuth(D.SOUTH, 90) == 270  # plan-south → true west
    assert true_azimuth(D.WEST, 90) == 0     # plan-west  → true north
    assert true_azimuth(D.NORTH, 270) == 270


def test_solar_sector_classification():
    assert solar_sector(180) == "south"
    assert solar_sector(270) == "west"
    assert solar_sector(90) == "east"
    assert solar_sector(0) == "north"
    assert solar_sector(350) == "north"
    # The hot afternoon quadrant (SW) lands in 'west', not split into 'south'.
    assert solar_sector(230) == "west"


def test_compass_label():
    assert compass_label(0) == "N"
    assert compass_label(270) == "W"
    assert compass_label(292) == "WNW"


# --- SOLAR_WEST_GAIN ---------------------------------------------------------

_WEST_GLASS = """\
plan "West glass"
envelope 40 x 30
ceiling 9
orientation 0
room living: living  at 0,0  size 22 x 30
room bed:    bedroom at 22,0 size 18 x 30
open living - bed width 6
entry living south width 3 offset 4
window living west width 8 offset 10
window bed east width 4 offset 5
"""


def test_big_west_window_overheats():
    r = compile_source(_WEST_GLASS)
    assert r.ok  # a nudge, not a blocker
    assert "SOLAR_WEST_GAIN" in _codes(r, "info")


def test_unsited_plan_is_silent():
    # Same plan without the `orientation` line — solar checks are dormant.
    r = compile_source(_WEST_GLASS.replace("orientation 0\n", ""))
    assert "SOLAR_WEST_GAIN" not in _codes(r, "info")
    assert "SOLAR_NORTH_ONLY" not in _codes(r, "info")


def test_orientation_moves_the_sun():
    # Rotate the plan so its WEST wall faces true *north* (θ=90): the big window is
    # now a north window, not a west one — west-gain clears, north-only may appear.
    r = compile_source(_WEST_GLASS.replace("orientation 0", "orientation 90"))
    assert "SOLAR_WEST_GAIN" not in _codes(r, "info")


# --- SOLAR_NORTH_ONLY --------------------------------------------------------


def test_room_glazed_only_north_is_flagged():
    src = """\
plan "North light"
envelope 40 x 30
ceiling 9
orientation 0
room living: living  at 0,0  size 22 x 30
room bed:    bedroom at 22,0 size 18 x 30
open living - bed width 6
entry living south width 3 offset 4
window living south width 6 offset 8
window bed north width 4 offset 5
"""
    r = compile_source(src)
    assert "SOLAR_NORTH_ONLY" in _codes(r, "info")
    # The hint recommends a sunnier wall the bedroom actually has (south or east).
    msg = next(i for i in r.infos if i.code == "SOLAR_NORTH_ONLY")
    assert "south" in msg.hint or "east" in msg.hint


def test_south_wall_left_unglazed_is_flagged():
    # A long south wall with no south glazing wastes the passive-solar face.
    src = """\
plan "Blank south"
envelope 40 x 30
ceiling 9
orientation 0
room living: living  at 0,0  size 24 x 30
room kitchen: kitchen at 24,0 size 16 x 30
open living - kitchen width 8
entry living north width 3 offset 10
window living north width 6 offset 8
window kitchen east width 4 offset 10
"""
    r = compile_source(src)
    assert "SOLAR_SOUTH_UNUSED" in _codes(r, "info")
    # Glazing the south face clears it.
    glazed = src.replace(
        "window living north width 6 offset 8", "window living south width 6 offset 8"
    )
    assert "SOLAR_SOUTH_UNUSED" not in _codes(compile_source(glazed), "info")


def test_south_unused_needs_orientation():
    src = """\
plan "Blank south unsited"
envelope 40 x 30
ceiling 9
room living: living  at 0,0  size 24 x 30
room kitchen: kitchen at 24,0 size 16 x 30
open living - kitchen width 8
entry living north width 3 offset 10
window living north width 6 offset 8
window kitchen east width 4 offset 10
"""
    assert "SOLAR_SOUTH_UNUSED" not in _codes(compile_source(src), "info")


def test_office_north_light_is_not_flagged():
    # North light is a legitimate studio/office choice — exempt from NORTH_ONLY.
    src = """\
plan "Studio"
envelope 30 x 24
ceiling 9
orientation 0
room office: office  at 0,0  size 18 x 24
room store:  closet  at 18,0 size 12 x 24
door office - store width 2.67
entry office south width 3 offset 4
window office north width 5 offset 6
"""
    r = compile_source(src)
    assert "SOLAR_NORTH_ONLY" not in _codes(r, "info")


# --- metrics / emit / render -------------------------------------------------


def test_metrics_glazing_split_and_azimuth():
    m = compile_source(_WEST_GLASS).plan.metrics()
    assert m["true_north_azimuth"] == 0.0
    # The living-room window (8 wide) faces west; the bedroom's faces east.
    assert m["glazing_west_sqft"] > m["glazing_north_sqft"]
    assert m["glazing_east_sqft"] > 0.0


def test_orientation_zero_round_trips():
    # A declared `orientation 0` is distinct from undeclared and must survive emit.
    plan = compile_source(_WEST_GLASS).plan
    assert plan.orientation == 0.0
    assert "orientation 0" in emit_dsl(plan)
    # An unsited plan emits no orientation line.
    unsited = compile_source(_WEST_GLASS.replace("orientation 0\n", "")).plan
    assert unsited.orientation is None
    assert "orientation" not in emit_dsl(unsited)


def test_compass_rendered_only_when_sited():
    sited = render_svg(compile_source(_WEST_GLASS).plan)
    assert "true N" in sited
    unsited = render_svg(compile_source(_WEST_GLASS.replace("orientation 0\n", "")).plan)
    assert "true N" not in unsited


def test_builder_orient_is_real():
    plan = (
        barndominium("Sited")
        .envelope(width=30, length=24)
        .ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .entrance("living", D.SOUTH, width=3, offset=4)
        .orient(45)
    )
    assert plan.orientation == 45.0
    assert wall_sector(D.NORTH, plan.orientation) in ("north", "east")
