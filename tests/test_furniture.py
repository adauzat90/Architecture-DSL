"""Tests for furniture-fit in habitable rooms (review item #6).

The livability companion to the wet-room fixture checks: a room can clear its
area and nominal-dimension minimums yet be the wrong *shape* to arrange. Both INFO.

- BED_CLEARANCE    — a bedroom too narrow to hold a queen bed with a walk-around.
- DINING_CLEARANCE — a dining room too tight to seat a table and pull the chairs.
"""

from __future__ import annotations

from barndsl import compile_source


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A 7-ft-wide bedroom: 7×20 = 140 sq ft (clears BEDROOM_AREA) with both nominal
# sides ≥ 7 ft (clears BEDROOM_DIM), yet too narrow for a queen + walk-around.
_NARROW_BED = """\
plan "Narrow bed"
envelope 30 x 20
ceiling 9
room living: living  at 0,0  size 23 x 20
room bed:    bedroom at 23,0 size 7 x 20
open living - bed width 3
entry living south width 3 offset 4
window living south width 8 offset 8
window bed east width 4 offset 8
"""


def test_narrow_bedroom_is_flagged_beyond_area_and_dimension():
    r = compile_source(_NARROW_BED)
    assert r.ok  # a nudge, not a blocker
    assert "BED_CLEARANCE" in _codes(r, "info")
    # It genuinely adds value: the area/dimension checks do *not* fire here.
    assert "BEDROOM_AREA" not in _codes(r, "error")
    assert "BEDROOM_DIM" not in _codes(r, "error")


def test_normal_bedroom_is_silent():
    wide = _NARROW_BED.replace("size 7 x 20", "size 12 x 20").replace(
        "size 23 x 20", "size 18 x 20"
    )
    assert "BED_CLEARANCE" not in _codes(compile_source(wide), "info")


_TIGHT_DINING = """\
plan "Tight dining"
envelope 30 x 20
ceiling 9
room kitchen: kitchen at 0,0  size 22 x 20
room dining:  dining  at 22,0 size 8 x 20
open kitchen - dining width 6
entry kitchen south width 3 offset 4
window kitchen south width 8 offset 8
window dining east width 4 offset 8
"""


def test_tight_dining_room_is_flagged():
    assert "DINING_CLEARANCE" in _codes(compile_source(_TIGHT_DINING), "info")


def test_spacious_dining_room_is_silent():
    wide = _TIGHT_DINING.replace("size 8 x 20", "size 14 x 20").replace(
        "size 22 x 20", "size 16 x 20"
    )
    assert "DINING_CLEARANCE" not in _codes(compile_source(wide), "info")


def test_only_bedrooms_and_dining_are_furniture_checked():
    # A narrow office/other room isn't held to the bed/table envelopes.
    src = _NARROW_BED.replace("bedroom at 23,0 size 7 x 20", "office  at 23,0 size 7 x 20")
    codes = _codes(compile_source(src), "info")
    assert "BED_CLEARANCE" not in codes and "DINING_CLEARANCE" not in codes
