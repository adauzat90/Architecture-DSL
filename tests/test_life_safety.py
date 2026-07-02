"""Life-safety checks: loft guards (R312), stair headroom (R311.7.2), and the
smoke/CO-alarm reminder for an attached garage/shop (R314/R315).

These are conditional so they don't nag ordinary single-storey plans — a plan
with no upper floor, no stair, and no attached garage sees none of them.
"""

from barndsl import RoomType as T
from barndsl import barndominium, validate


def _codes(plan):
    return {i.code for i in validate(plan).issues}


# --- loft guard (R312) -------------------------------------------------------


def test_loft_over_double_height_space_needs_a_guard():
    # The loft (24x10) covers only the north half of the 24x20 living room, so the
    # south half is open to the loft floor — a double-height void needing a guard.
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=20)
        .add_room("loft", T.LOFT, x=0, y=10, width=24, length=10, level=1)
        .entrance("living", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=4, length=12, from_level=0, to_level=1)
    )
    assert "LOFT_GUARD" in _codes(plan)


def test_full_loft_over_room_needs_no_guard():
    # A loft sized exactly to the room below has a solid floor to its edge — no
    # void, so no guard nag (this is the gallery two_story configuration).
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=20)
        .add_room("loft", T.LOFT, x=0, y=0, width=24, length=20, level=1)
        .entrance("living", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=4, length=12, from_level=0, to_level=1)
    )
    assert "LOFT_GUARD" not in _codes(plan)


def test_single_storey_plan_has_no_guard_check():
    plan = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=16)
        .entrance("living", "south", width=3, offset=8)
    )
    assert "LOFT_GUARD" not in _codes(plan)


# --- stair headroom (R311.7.2) ----------------------------------------------


def test_short_stair_flags_headroom():
    # A wide-but-short stair footprint passes the switchback run check yet is far
    # too short to open a stairwell with 6'-8" headroom.
    plan = (
        barndominium("x").envelope(30, 24).ceiling(10)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .add_room("loft", T.LOFT, x=0, y=0, width=30, length=24, level=1)
        .entrance("living", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=8, length=6, from_level=0, to_level=1)
    )
    assert "STAIR_HEADROOM" in _codes(plan)


def test_ample_stair_has_no_headroom_flag():
    # The gallery two_story stair (4x12, ceiling 9) is long enough — no flag.
    plan = (
        barndominium("x").envelope(39, 33).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=18)
        .add_room("loft", T.LOFT, x=0, y=0, width=24, length=18, level=1)
        .entrance("living", "south", width=3, offset=14)
        .add_stair("s", x=0, y=3, width=4, length=12, from_level=0, to_level=1)
    )
    assert "STAIR_HEADROOM" not in _codes(plan)


# --- smoke/CO alarms (R314/R315) --------------------------------------------


def test_attached_garage_triggers_alarm_reminder():
    plan = (
        barndominium("x").envelope(60, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .add_room("garage", T.GARAGE, x=30, y=0, width=30, length=24)
        .connect("living", "garage", width=3)
        .entrance("living", "south", width=3, offset=10)
    )
    assert "ALARM_CO" in _codes(plan)


def test_shop_bay_triggers_alarm_reminder():
    plan = (
        barndominium("x").envelope(60, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .add_room("shop", T.SHOP, x=30, y=0, width=30, length=24)
        .connect("living", "shop", width=3)
        .entrance("living", "south", width=3, offset=10)
    )
    assert "ALARM_CO" in _codes(plan)


def test_no_garage_no_alarm_reminder():
    plan = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=16)
        .entrance("living", "south", width=3, offset=8)
    )
    assert "ALARM_CO" not in _codes(plan)
