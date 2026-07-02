"""Tests for wing-aware metrics and stair checks (review §5 bug fixes)."""

from __future__ import annotations

import math

from barndsl import barndominium, compile_source
from barndsl.constants import DEFAULT_ROOF_PITCH


def _codes(result, severity: str) -> set[str]:
    return {d.code for d in result.diagnostics if d.severity.value == severity}


# --- metrics(): perimeter / exterior wall area --------------------------------


def test_rectangle_metrics_keep_the_closed_form_perimeter():
    # No wings: the perimeter stays the exact 2(W+L) closed form.
    plan = barndominium("Rect").envelope(40, 30).ceiling(10)
    m = plan.metrics()
    assert m["exterior_perimeter_ft"] == 2.0 * (40 + 30)
    assert m["exterior_wall_area_sqft"] == 2.0 * (40 + 30) * 10
    assert m["footprint_sqft"] == 40 * 30


def test_lshape_metrics_measure_the_union_perimeter():
    # 44×40 main block + 24×22 wing projecting east (examples/lshape.barn).
    # Walking the L outline: 68 (south) + 22 (wing east) + 24 (wing north) +
    # 18 (main east above the wing) + 44 (north) + 40 (west) = 216 ft — not
    # the primary envelope's 2*(44+40) = 168 ft.
    plan = barndominium("L").envelope(44, 40).ceiling(10).wing(24, 22, x=44, y=0)
    m = plan.metrics()
    assert math.isclose(m["exterior_perimeter_ft"], 216.0)
    assert math.isclose(m["exterior_wall_area_sqft"], 216.0 * 10)
    # Footprint area already used the union — still does.
    assert math.isclose(m["footprint_sqft"], 44 * 40 + 24 * 22)


# --- metrics(): roof area follows the authored pitch ---------------------------


def test_roof_area_uses_the_default_pitch_when_unset():
    m = barndominium("R").envelope(40, 30).metrics()
    assert math.isclose(
        m["roof_area_sqft"], 40 * 30 * math.hypot(1.0, DEFAULT_ROOF_PITCH)
    )


def test_roof_area_follows_a_pitch_override():
    # `roof gable pitch 0.5` (6:12) steepens the slope factor beyond the default.
    m = barndominium("S").envelope(40, 30).roof("gable", pitch=0.5).metrics()
    assert math.isclose(m["roof_area_sqft"], 40 * 30 * math.hypot(1.0, 0.5))
    assert m["roof_area_sqft"] > 40 * 30 * math.hypot(1.0, DEFAULT_ROOF_PITCH)


# --- stairs in wings: STAIR_OOB / STAIR_WALL -----------------------------------

_LWING = """\
plan "LWing"
envelope 44 x 40
wing 24 x 22 at 44,0
ceiling 9
room living: living at 0,0  size 44 x 40
room studio: office at 44,0 size 24 x 22
room loft:   loft   at 44,0 size 24 x 22 level 1
stair s at {sx},{sy} size 4 x 12 from 0 to 1
door living - studio width 3 offset 4
entry living south width 3 offset 10
window living west width 12 offset 8
window studio east width 8 offset 6
window loft east width 8 offset 6
"""


def test_stair_inside_a_wing_is_in_bounds():
    # 50..54 × 2..14 sits wholly in the 44..68 × 0..22 wing — past the primary
    # envelope's east edge (x=44) but inside the footprint union.
    r = compile_source(_LWING.format(sx=50, sy=2))
    assert "STAIR_OOB" not in _codes(r, "error")


def test_stair_outside_the_footprint_union_still_errors():
    # 50..54 × 30..42 falls in the L-notch north of the wing (and past y=40).
    r = compile_source(_LWING.format(sx=50, sy=30))
    assert "STAIR_OOB" in _codes(r, "error")


def test_stair_against_a_wing_exterior_edge_counts_as_walled():
    # x2 = 68 is the wing's east outline edge — an exterior wall, so no
    # free-floating info even though it touches none of the primary edges.
    r = compile_source(_LWING.format(sx=64, sy=2))
    assert "STAIR_WALL" not in _codes(r, "info")


def test_stair_floating_mid_wing_is_still_flagged():
    # 50..54 × 5..17 is marooned in the middle of the wing rooms.
    r = compile_source(_LWING.format(sx=50, sy=5))
    assert "STAIR_WALL" in _codes(r, "info")
