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
    DOORLEAF,
    MUNTIN,
    OPENING,
    OVERHEAD,
    PORCHPOST,
    PORCHROOF,
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


def test_door_reads_as_a_door_not_glazing():
    # The south wall has a hinged entry door and a casement window. The door must
    # be drawn in the wood leaf colour (not glazing blue), with a knob (circle) and
    # a two-panel inset — unmistakably a door.
    plan = compile_source(_PLAN).plan
    south = elevation_svg(plan, "south")
    assert DOORLEAF in south          # wood leaf, distinct from OPENING glazing
    assert "<circle" in south         # the door knob
    assert OPENING in south           # the window is still glazing


def test_windows_carry_muntins_for_divided_lites_but_not_fixed():
    # A default (casement) window gets a 2×2 divided-lite cross → muntin bars;
    # a fixed window is a single clean pane → no muntins.
    plan = compile_source(_PLAN).plan
    assert MUNTIN in elevation_svg(plan, "south")
    fixed = _PLAN.replace("window great south width 6 offset 2",
                          "window great south width 6 offset 2 kind fixed")
    fplan = compile_source(fixed).plan
    # South now has only the fixed window (no casement) → no muntin bars on it.
    assert MUNTIN not in elevation_svg(fplan, "south")


def test_overhead_door_gets_sectional_panel_lines():
    src = _PLAN + "\ndoor kitchen north overhead width 9 offset 4\n"
    plan = compile_source(src).plan
    north = elevation_svg(plan, "north")
    assert OVERHEAD in north  # sectional garage-door panel fill


def test_pitch_tag_prints_rise_over_twelve():
    # The default gable pitch is 4:12 → the tag carries "12" and the rise "4".
    plan = compile_source(_PLAN).plan
    east = elevation_svg(plan, "east")  # a gable end → a visible sloped roof
    assert ">12<" in east
    assert ">4<" in east


def test_grade_line_has_repeating_hatch_ticks():
    # The grade is a heavy line PLUS several diagonal earth-hatch ticks (more than
    # one grade-coloured stroke), so the building sits on something.
    from barndsl.views import GRADE

    south = elevation_svg(compile_source(_PLAN).plan, "south")
    assert south.count(f'stroke="{GRADE}"') > 3


def test_covered_porch_draws_roof_and_posts_on_its_face():
    src = _PLAN + "\nporch front at 0,-8 size 30 x 8 covered\n"
    plan = compile_source(src).plan
    south = elevation_svg(plan, "south")  # porch projects south of the building
    assert PORCHPOST in south
    assert PORCHROOF in south
    # A face the porch does NOT front carries neither.
    east = elevation_svg(plan, "east")
    assert PORCHPOST not in east


def test_section_draws_assemblies_not_a_bare_outline():
    plan = compile_source(_PLAN).plan
    svg = section_svg(plan)
    from barndsl.views import WALLFILL

    # Two exterior wall-thickness bands (the eave walls the cut passes through).
    assert svg.count(f'fill="{WALLFILL}"') >= 2
    # Slab band + rafter underside pair + a ceiling-height dimension.
    assert 'fill="#EDE9E2"' in svg          # slab
    assert "<polyline" in svg               # rafter/roof underside line
    assert "clg" in svg                     # ceiling-height dimension label
    assert ">12<" in svg                    # pitch tag on the section roof


def test_shed_roof_slopes():
    src = _PLAN.replace("ceiling 12", "ceiling 12\nroof shed pitch 0.25")
    plan = compile_source(src).plan
    g = _roof_geom(plan)
    assert g["style"] == "shed"
    # A shed rises across the full short span (40 ft) → a taller rise than a gable.
    assert g["rise"] == pytest.approx(40.0 * 0.25)
    _wellformed(elevation_svg(plan, "east"))


def test_mirrored_faces_draw_rects_inside_the_canvas():
    """North and West read from outside, so screen x is flipped. A rect's left edge
    must be the smaller *projected* x — taking the smaller world coordinate put the
    whole wall mass (and every opening) off the right edge of the canvas."""
    import re

    plan = compile_source(_PLAN).plan
    for side in ("north", "west", "south", "east"):
        svg = elevation_svg(plan, side)
        width = float(re.search(r'<svg[^>]*\bwidth="([\d.]+)"', svg).group(1))
        rects = re.findall(r'<rect x="([-\d.]+)" y="[-\d.]+" width="([\d.]+)"', svg)
        assert rects, side
        for x, w in rects:
            assert 0 <= float(x) and float(x) + float(w) <= width + 0.5, (side, x, w)
