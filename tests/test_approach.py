"""Tests for the approach feature (site & solar, Phase 3).

The `street <wall>` directive names the side that faces the street/approach and
turns on two INFO nudges, both gated on it being declared:

- APPROACH_ENTRY   — no people-door faces the street (front door round the back).
- APPROACH_GARAGE  — an overhead door faces the wall opposite the street.

Also covers the deferred passive-solar SOLAR_SOUTH_UNUSED lives in test_solar.py.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl, render_svg
from barndsl import Direction as D, RoomType as T


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


def test_direction_opposite():
    assert D.NORTH.opposite() is D.SOUTH
    assert D.SOUTH.opposite() is D.NORTH
    assert D.EAST.opposite() is D.WEST
    assert D.WEST.opposite() is D.EAST


# --- APPROACH_ENTRY ----------------------------------------------------------

_FRONT_ON_BACK = """\
plan "Front on back"
envelope 40 x 30
ceiling 9
street south
room living: living  at 0,0  size 24 x 30
room kitchen: kitchen at 24,0 size 16 x 30
open living - kitchen width 8
entry living north width 3 offset 10
window living south width 6 offset 8
window kitchen east width 4 offset 10
"""


def test_no_entry_faces_street_is_flagged():
    r = compile_source(_FRONT_ON_BACK)
    assert r.ok  # a nudge, never a blocker
    assert "APPROACH_ENTRY" in _codes(r, "info")


def test_entry_on_street_wall_is_silent():
    # Move the front door to the street (south) wall.
    r = compile_source(_FRONT_ON_BACK.replace("entry living north", "entry living south"))
    assert "APPROACH_ENTRY" not in _codes(r, "info")


def test_no_street_means_no_approach_check():
    r = compile_source(_FRONT_ON_BACK.replace("street south\n", ""))
    assert "APPROACH_ENTRY" not in _codes(r, "info")
    assert r.plan.street is None


# --- APPROACH_GARAGE ---------------------------------------------------------

_GARAGE = """\
plan "Garage approach"
envelope 60 x 40
ceiling 10
street south
room living: living  at 0,0  size 30 x 40
room garage: garage  at 30,0 size 30 x 40
door living - garage width 3 offset 4
entry living south width 3 offset 10
door garage {wall} overhead width 16 offset 6
window living east width 6 offset 20
"""


def test_overhead_opposite_the_street_is_flagged():
    # street south → the north wall is "away"; an overhead door there gets flagged.
    r = compile_source(_GARAGE.format(wall="north"))
    assert "APPROACH_GARAGE" in _codes(r, "info")


def test_overhead_on_a_side_wall_is_silent():
    # An overhead on the east (side) wall isn't facing directly away — no nudge.
    r = compile_source(_GARAGE.format(wall="east"))
    assert "APPROACH_GARAGE" not in _codes(r, "info")


# --- emit / builder / render -------------------------------------------------


def test_street_round_trips():
    plan = compile_source(_FRONT_ON_BACK).plan
    assert plan.street is D.SOUTH
    assert "street south" in emit_dsl(plan)
    again = compile_source(emit_dsl(plan)).plan
    assert again.street is D.SOUTH


def test_builder_set_street_accepts_str_and_enum():
    plan = (
        barndominium("B")
        .envelope(width=40, length=30)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=40, length=30)
        .entrance("a", D.SOUTH, width=3, offset=4)
        .set_street("south")
    )
    assert plan.street is D.SOUTH
    plan.set_street(D.EAST)
    assert plan.street is D.EAST


def test_bad_street_wall_is_a_parse_error():
    r = compile_source(_FRONT_ON_BACK.replace("street south", "street sideways"))
    assert not r.ok
    assert "BAD_WALL" in _codes(r, "error")


def test_street_rendered_when_declared():
    assert "STREET" in render_svg(compile_source(_FRONT_ON_BACK).plan)
    assert "STREET" not in render_svg(
        compile_source(_FRONT_ON_BACK.replace("street south\n", "")).plan
    )
