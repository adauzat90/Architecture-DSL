"""Tests for roof overhangs and covered-porch shading (review item #3).

The `overhang <ft>` directive gives the roof a real eave depth — the barndominium
signature and the primary passive-shading device. It widens the roof in the
elevations/section and the roof-area takeoff, and it (with a covered porch) lets
the solar checks *credit* shaded glass instead of flagging it:

- SOLAR_SOUTH_NO_OVERHANG  — lots of south glass with no eave to shade it.
- SOLAR_WEST_GAIN          — now suppressed when a covered porch shades the glass.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl
from barndsl import Direction as D, RoomType as T
from barndsl.views import elevation_svg


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- the model ---------------------------------------------------------------


def test_overhang_parses_and_round_trips():
    src = """\
plan "Eaves"
envelope 40 x 30
ceiling 9
overhang 2
room living: living at 0,0 size 40 x 30
entry living south width 3 offset 4
window living south width 6 offset 20
"""
    plan = compile_source(src).plan
    assert plan.overhang == 2.0
    assert "overhang 2" in emit_dsl(plan)
    assert compile_source(emit_dsl(plan)).plan.overhang == 2.0


def test_overhang_grows_the_roof_takeoff():
    base = """\
plan "Roof"
envelope 40 x 30
ceiling 9
{oh}room living: living at 0,0 size 40 x 30
entry living south width 3 offset 4
window living south width 6 offset 20
"""
    flush = compile_source(base.format(oh="")).plan.metrics()
    eaved = compile_source(base.format(oh="overhang 2\n")).plan.metrics()
    assert flush["overhang_ft"] == 0.0
    assert eaved["overhang_ft"] == 2.0
    assert eaved["roof_area_sqft"] > flush["roof_area_sqft"]


def test_negative_overhang_rejected():
    with pytest.raises(ValueError):
        barndominium("x").envelope(width=40, length=30).set_overhang(-1)


# --- SOLAR_SOUTH_NO_OVERHANG -------------------------------------------------

_SOUTH_GLASS = """\
plan "South glass"
envelope 40 x 30
ceiling 9
orientation 0
{extra}room living: living at 0,0 size 40 x 30
entry living north width 3 offset 10
window living south width 12 offset 4
window living south width 12 offset 22
"""


def test_lots_of_south_glass_without_an_eave_is_flagged():
    r = compile_source(_SOUTH_GLASS.format(extra=""))
    assert r.ok  # a nudge, not a blocker
    assert "SOLAR_SOUTH_NO_OVERHANG" in _codes(r, "info")


def test_an_adequate_overhang_clears_it():
    r = compile_source(_SOUTH_GLASS.format(extra="overhang 2\n"))
    assert "SOLAR_SOUTH_NO_OVERHANG" not in _codes(r, "info")


def test_a_covered_south_porch_shades_it():
    # A covered porch across the south wall counts as shade even with no eave.
    src = _SOUTH_GLASS.format(extra="") + "porch veranda at 0,-8 size 40 x 8 covered\n"
    assert "SOLAR_SOUTH_NO_OVERHANG" not in _codes(compile_source(src), "info")


def test_south_overhang_check_needs_orientation():
    r = compile_source(_SOUTH_GLASS.format(extra="").replace("orientation 0\n", ""))
    assert "SOLAR_SOUTH_NO_OVERHANG" not in _codes(r, "info")


# --- SOLAR_WEST_GAIN credit --------------------------------------------------

_WEST_GLASS = """\
plan "West glass"
envelope 40 x 30
ceiling 9
orientation 0
room living: living at 0,0 size 40 x 30
entry living north width 3 offset 10
window living west width 8 offset 11
{porch}"""


def test_big_west_window_still_flags_without_shade():
    r = compile_source(_WEST_GLASS.format(porch=""))
    assert "SOLAR_WEST_GAIN" in _codes(r, "info")


def test_covered_porch_credits_the_west_glass():
    # A porch abutting the west wall over the window gives the vertical shade a low
    # west sun needs — the gain nudge clears.
    src = _WEST_GLASS.format(porch="porch p at -8,0 size 8 x 30 covered\n")
    assert "SOLAR_WEST_GAIN" not in _codes(compile_source(src), "info")


def test_an_open_porch_does_not_shade():
    # An *open* (roofless) porch is no shade — the gain nudge still fires.
    src = _WEST_GLASS.format(porch="porch p at -8,0 size 8 x 30 open\n")
    assert "SOLAR_WEST_GAIN" in _codes(compile_source(src), "info")


# --- views -------------------------------------------------------------------


def test_overhang_widens_the_drawn_roof():
    src = """\
plan "Eaved gable"
envelope 32 x 24
ceiling 10
overhang 2
room a: living at 0,0 size 32 x 24
entry a south width 3 offset 4
window a east width 5 offset 8
"""
    plan = compile_source(src).plan
    flush = compile_source(src.replace("overhang 2\n", "")).plan
    # The eaved roof projects past the walls → a wider drawing than the flush one.
    import re

    def width(svg):
        return float(re.search(r'<svg[^>]*width="([\d.]+)"', svg).group(1))

    assert width(elevation_svg(plan, "east")) > width(elevation_svg(flush, "east"))
    assert "overhang 2" not in elevation_svg(plan, "east")  # sanity: not text
