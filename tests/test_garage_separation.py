"""Garage / dwelling separation reminders (IRC R302.6 and R302.5.1).

The DSL can't model gypsum layers or door ratings, so these fire as reminders off
the geometry that triggers the requirement — a garage sharing a wall with (or
carrying habitable space above) the dwelling, and the fire-rated self-closing
door between them.
"""

from barndsl import RoomType as T
from barndsl import barndominium, validate


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _issue(plan, code):
    return next(i for i in validate(plan).issues if i.code == code)


def _garage_and_mudroom():
    return (
        barndominium("x").envelope(40, 20).ceiling(9)
        .add_room("garage", T.GARAGE, x=0, y=0, width=20, length=20)
        .add_room("mud", T.MUDROOM, x=20, y=0, width=8, length=20)
        .add_room("living", T.LIVING, x=28, y=0, width=12, length=20)
        .connect("garage", "mud", width=3)
        .connect("mud", "living", width=6)
    )


def test_shared_wall_triggers_separation_reminder():
    plan = _garage_and_mudroom()
    codes = _codes(plan)
    assert "GARAGE_SEPARATION" in codes
    assert "R302.6" in _issue(plan, "GARAGE_SEPARATION").message


def test_garage_dwelling_door_needs_self_closing_rating():
    plan = _garage_and_mudroom()
    assert "GARAGE_DOOR" in _codes(plan)
    assert "self-closing" in _issue(plan, "GARAGE_DOOR").message


def test_no_garage_no_reminders():
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=20)
        .add_room("kit", T.KITCHEN, x=20, y=0, width=10, length=20)
    )
    codes = _codes(plan)
    assert "GARAGE_SEPARATION" not in codes
    assert "GARAGE_DOOR" not in codes


def test_habitable_space_above_asks_for_type_x_ceiling():
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("garage", T.GARAGE, x=0, y=0, width=24, length=20)
        .add_room("bonus", T.LOFT, x=0, y=0, width=24, length=20, level=1)
    )
    sep = _issue(plan, "GARAGE_SEPARATION")
    assert "above" in sep.message and "Type X" in sep.message


def test_door_into_bedroom_is_the_bedroom_warning_not_the_door_reminder():
    plan = (
        barndominium("x").envelope(30, 20).ceiling(9)
        .add_room("garage", T.GARAGE, x=0, y=0, width=18, length=20)
        .add_room("bed", T.BEDROOM, x=18, y=0, width=12, length=20)
        .connect("garage", "bed", width=3)
    )
    codes = _codes(plan)
    assert "GARAGE_BEDROOM" in codes  # the hard prohibition
    assert "GARAGE_DOOR" not in codes  # not double-reported
