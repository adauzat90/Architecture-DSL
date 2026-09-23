"""Firing + satisfied cases for long-standing rules that had no test of their own.

ADR 0002: every diagnostic code is a contract with a firing case and a satisfied
case. These are the reference/geometry/stair basics the diagnostic matrix listed
as ``no_test_mention``. ``STAIR_LEVELS`` and ``STAIR_GEOMETRY`` can't be reached
from DSL text (the parser rejects the input first, as BAD_LEVEL / BAD_NUMBER), so
they are exercised through the builder API the validator also serves.
"""

from __future__ import annotations

import math

import pytest

from barndsl.compiler import compile_source
from barndsl.elements import Barndominium, Room, RoomType, Stair
from barndsl.validation import validate

BASE = (
    'plan "P"\nenvelope 30 x 20\nceiling 9\n'
    "room a: living at 0,0 size 15 x 20\n"
    "room b: bedroom at 15,0 size 15 x 20\n"
    "entry a south width 3\n"
)

#: code -> (a line that makes it fire, the corrected line that satisfies it)
DSL_CASES = {
    # Footprint coverage counts ground-floor rooms only; the same room upstairs fits.
    "AREA_OVERFLOW": ("room c: office at 0,0 size 15 x 20", "room c: loft at 0,0 size 15 x 20 level 1"),
    "DUP_ID": ("room a: office at 0,0 size 5 x 5", "room c: office at 0,0 size 5 x 5"),
    "WINDOW_REF": ("window zzz south width 3 offset 1", "window b south width 3 offset 1"),
    "STAIR_SIZE": ("stair s at 0,0 size 0 x 10 from 0 to 1", "stair s at 0,0 size 4 x 10 from 0 to 1"),
}


def _codes(src: str) -> set[str]:
    return {d.code for d in compile_source(src).diagnostics}


@pytest.mark.parametrize("code", sorted(DSL_CASES))
def test_rule_fires(code):
    firing, _ = DSL_CASES[code]
    assert code in _codes(BASE + firing + "\n")


@pytest.mark.parametrize("code", sorted(DSL_CASES))
def test_rule_satisfied(code):
    _, satisfied = DSL_CASES[code]
    assert code not in _codes(BASE + satisfied + "\n")


def _plan_with_stair(stair: Stair) -> Barndominium:
    plan = Barndominium(name="B").envelope(30, 20).ceiling(9)
    plan.add_room("a", RoomType.LIVING, x=0, y=0, width=15, length=20)
    plan.stairs.append(stair)
    return plan


def _validated(plan: Barndominium) -> set[str]:
    return {issue.code for issue in validate(plan).issues}


def test_stair_levels_fires_for_a_stair_to_its_own_level():
    assert "STAIR_LEVELS" in _validated(_plan_with_stair(Stair("s", 0, 0, 4, 10, 1, 1)))


def test_stair_levels_satisfied_for_distinct_levels():
    assert "STAIR_LEVELS" not in _validated(_plan_with_stair(Stair("s", 0, 0, 4, 10, 0, 1)))


def test_stair_geometry_fires_for_non_finite_coordinates():
    assert "STAIR_GEOMETRY" in _validated(_plan_with_stair(Stair("s", math.nan, 0, 4, 10, 0, 1)))


def test_stair_geometry_satisfied_for_finite_coordinates():
    assert "STAIR_GEOMETRY" not in _validated(_plan_with_stair(Stair("s", 0, 0, 4, 10, 0, 1)))


def test_dup_id_from_the_builder_names_the_duplicate_once():
    plan = Barndominium(name="B").envelope(30, 20).ceiling(9)
    plan.add_room("a", RoomType.LIVING, x=0, y=0, width=15, length=20)
    plan.rooms.append(Room("a", RoomType.OFFICE, 15, 0, 5, 5))
    dups = [i for i in validate(plan).issues if i.code == "DUP_ID"]
    assert len(dups) == 1 and dups[0].room == "a"
