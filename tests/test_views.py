"""Tests for the schematic vertical views — elevations and section (review #2).

Heights, levels and openings are exact; the roof is drawn from the `roof` form +
pitch. These tests pin the vertical geometry (eave/ridge, gable-end vs eave-side
profile), that openings land on the drawn face, and that the SVG is well-formed.
"""

from __future__ import annotations

import xml.dom.minidom as minidom

import pytest

from barndsl import Direction as D, compile_source
from barndsl.views import (
    OPENING,
    _roof_geom,
    _top_profile,
    elevation_svg,
    section_svg,
)

# 60 (x) × 40 (y): the long axis is x, so the ridge runs E–W (gable_axis "x").
# East/West are the 40-ft gable ends; North/South are the 60-ft eave sides.
_PLAN = """\
plan "Vble"
envelope 60 x 40
ceiling 12
room great: living  at 0,0   size 30 x 40
room kitchen: kitchen at 30,0 size 30 x 40
open great - kitchen width 8
entry great south width 3 offset 10
window great south width 6 offset 2
window great east width 5 offset 10
window kitchen north width 5 offset 10
"""


def _wellformed(svg: str) -> str:
    minidom.parseString(svg)  # raises on malformed XML
    return svg


def test_roof_geometry_eave_and_ridge():
    plan = compile_source(_PLAN).plan
    g = _roof_geom(plan)
    assert g["eave"] == pytest.approx(12.0)
    assert g["gable_axis"] == "x"  # long axis is x
    # gable ridge rises over half the short (40 ft) span at the default 4:12.
    assert g["rise"] == pytest.approx(20.0 * (4.0 / 12.0))
    assert g["ridge"] == pytest.approx(12.0 + 20.0 * (4.0 / 12.0))


def test_gable_end_is_a_triangle_eave_side_is_a_band():
    plan = compile_source(_PLAN).plan
    g = _roof_geom(plan)
    # East face is perpendicular to the ridge → a 3-point gable triangle peaking at
    # the ridge; South face is parallel → a 2-point band topping out at the ridge.
    *_, east = _top_profile(plan, g, D.EAST)
    *_, south = _top_profile(plan, g, D.SOUTH)
    assert len(east) == 3 and east[1][1] == pytest.approx(g["ridge"])
    assert len(south) == 2 and all(z == pytest.approx(g["ridge"]) for _, z in south)


def test_elevation_is_wellformed_and_shows_face_openings():
    plan = compile_source(_PLAN).plan
    south = _wellformed(elevation_svg(plan, "south"))
    # The south wall carries a window + a door → glazing colour appears.
    assert OPENING in south
    assert "SOUTH elevation" in south
    # A face with no opening still draws (north here has one window on the kitchen).
    _wellformed(elevation_svg(plan, D.NORTH))
    _wellformed(elevation_svg(plan, "west"))


def test_section_is_wellformed_and_labels_rooms():
    plan = compile_source(_PLAN).plan
    svg = _wellformed(section_svg(plan))
    assert "SECTION" in svg
    # The cut runs across the short (y) span at mid-building; both rooms span it.
    assert "great" in svg and "kitchen" in svg


def test_vaulted_room_opens_to_the_roof_in_section():
    src = _PLAN.replace("room great: living  at 0,0   size 30 x 40",
                        "room great: living  at 0,0   size 30 x 40 vaulted")
    svg = section_svg(compile_source(src).plan)
    assert "vaulted" in svg


def test_multi_level_eave_reflects_upper_floor():
    src = """\
plan "Two"
envelope 30 x 24
ceiling 9
room living: living at 0,0 size 30 x 24
room loft:   loft   at 0,0 size 30 x 18 level 1
stair flight at 0,2 size 4 x 12 from 0 to 1
open living - loft width 3
entry living south width 3 offset 4
window living south width 6 offset 10
window loft north width 6 offset 10
"""
    plan = compile_source(src).plan
    g = _roof_geom(plan)
    # floor-to-floor = ceiling 9 + floor assembly 1 = 10 → level-1 floor at 10 ft,
    # so the eave (top plate) is 10 + 9 = 19 ft, not 9.
    assert g["eave"] == pytest.approx(19.0)
    _wellformed(elevation_svg(plan, "south"))
    _wellformed(section_svg(plan))


def test_shed_roof_slopes():
    src = _PLAN.replace("ceiling 12", "ceiling 12\nroof shed pitch 0.25")
    plan = compile_source(src).plan
    g = _roof_geom(plan)
    assert g["style"] == "shed"
    # A shed rises across the full short span (40 ft) → a taller rise than a gable.
    assert g["rise"] == pytest.approx(40.0 * 0.25)
    _wellformed(elevation_svg(plan, "east"))
