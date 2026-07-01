"""Nominal vs clear (finish-face) room dimensions.

barndsl rooms tile on wall centrelines, so the built interior is smaller than the
nominal rectangle by half a wall on each side. These tests pin the clear-dimension
geometry, the ROOM_CLEAR nudge for the "passes on paper, short when built" band,
and that the schedule + exchange report the same clear number Revit does.
"""

import pytest

from barndsl import barndominium, to_revit_model, validate
from barndsl import RoomType as T
from barndsl.constants import EXTERIOR_WALL_THICKNESS as EXT
from barndsl.constants import INTERIOR_WALL_THICKNESS as INT
from barndsl.schedule import room_rows
from barndsl.validation import clear_dimensions


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def test_clear_subtracts_half_a_wall_per_edge():
    # A corner room: south+west on the envelope (exterior), north+east interior.
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("corner", T.LIVING, x=0, y=0, width=10, length=10)
        .add_room("filler", T.LIVING, x=10, y=0, width=20, length=20)
    )
    cw, cl = clear_dimensions(plan, plan.room("corner"))
    assert cw == pytest.approx(10 - EXT / 2 - INT / 2)
    assert cl == pytest.approx(10 - EXT / 2 - INT / 2)
    assert cw < 10  # always smaller than nominal


def test_interior_room_costs_two_partitions_per_axis():
    # A fully interior room loses only partition thickness, not the thicker shell.
    plan = (
        barndominium("x").envelope(40, 40).ceiling(9)
        .add_room("mid", T.OFFICE, x=10, y=10, width=10, length=10)
        .add_room("around", T.LIVING, x=0, y=0, width=40, length=40)
    )
    cw, cl = clear_dimensions(plan, plan.room("mid"))
    assert cw == pytest.approx(10 - INT)  # half a partition on each side


def test_room_clear_fires_when_nominal_passes_but_clear_fails():
    # 7 ft nominal bedroom width meets the 7 ft R304 minimum; clear does not.
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=7, length=12)
        .add_room("rest", T.LIVING, x=7, y=0, width=17, length=20)
    )
    assert plan.room("bed").min_dimension >= 7.0  # nominal passes
    assert min(clear_dimensions(plan, plan.room("bed"))) < 7.0  # clear does not
    assert "ROOM_CLEAR" in _codes(plan)


def test_room_clear_silent_when_clear_still_passes():
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=12, length=12)
        .add_room("rest", T.LIVING, x=12, y=0, width=18, length=20)
    )
    assert min(clear_dimensions(plan, plan.room("bed"))) >= 7.0
    assert "ROOM_CLEAR" not in _codes(plan)


def test_room_clear_does_not_double_report_a_nominal_failure():
    # Nominal already below the minimum → the ERROR check owns it, not ROOM_CLEAR.
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=6, length=12)
        .add_room("rest", T.LIVING, x=6, y=0, width=18, length=20)
    )
    codes = _codes(plan)
    assert "BEDROOM_DIM" in codes
    assert "ROOM_CLEAR" not in codes


def test_schedule_and_exchange_report_the_same_clear_area():
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("den", T.LIVING, x=0, y=0, width=12, length=10)
        .add_room("rest", T.LIVING, x=12, y=0, width=18, length=20)
    )
    cw, cl = clear_dimensions(plan, plan.room("den"))
    row = next(r for r in room_rows(plan) if r["mark"] == "den")
    assert row["clear_area"] == pytest.approx(cw * cl)
    assert row["clear_area"] < row["area"]  # clear < nominal

    rvt = next(r for r in to_revit_model(plan).to_dict()["rooms"] if r["id"] == "den")
    assert rvt["clear_area"] == pytest.approx(cw * cl)
