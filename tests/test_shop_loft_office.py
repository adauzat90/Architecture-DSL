"""Shop, loft and office room-quality checks.

Four checks that judge whether a room can do its job:

* SHOP_DEPTH (warning under 12 ft, info under 20) — a shop bay narrower than a
  vehicle plus a working aisle is storage mislabeled as a shop (the shop analog
  of MUDROOM_SHAPE). A GARAGE is exempt (sized to cars, not equipment).
* SHOP_DOOR_HEIGHT (info) — a 7-8 ft overhead door on a shop defeats a 12 ft+
  bay's clearance; the tall commercial panels (10/12/14 ft) are now stock sizes.
* LOFT_CEILING (warning) — a loft is habitable and often sleeps people, so its
  effective ceiling (its override, else the plan ceiling) must clear IRC R305.1's
  7 ft. A `vaulted` loft (open to the ridge) is exempt.
* OFFICE_CLEARANCE (info) — an office too small to hold a desk (4×2) with a 3 ft
  chair-pull behind it, clear of door swings (the office analog of BED_CLEARANCE).

Each test builds a small, fully-tiled plan and asserts code membership on
`validate`, one behavior per test — the same pattern as tests/test_closets.py.
"""

from barndsl import RoomType as T
from barndsl import barndominium, compile_source, validate


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _issues(plan, code):
    return [i for i in validate(plan).issues if i.code == code]


def _sev(plan, code):
    return {i.severity.value for i in _issues(plan, code)}


# --- SHOP_DEPTH ---------------------------------------------------------------
# A shop's shortest side is what limits it — a 30 ft deep, 10 ft wide bay is
# still only 10 ft "across" and can't take a truck. Under 12 ft the bay CANNOT
# do the job (warning); 12-20 ft works but is tight (info); >= 20 ft is clean.


def _shop(width, rtype=T.SHOP, env_extra=0):
    """A `width` x 30 ft shop bay beside a living block, fully tiling a 40+ x 30."""
    return (
        barndominium("x").envelope(40 + env_extra, 30).ceiling(10)
        .add_room("shop", rtype, x=0, y=0, width=width, length=30)
        .add_room("living", T.LIVING, x=width, y=0, width=40 + env_extra - width,
                  length=30)
    )


def test_shop_under_twelve_feet_is_a_warning():
    plan = _shop(10)
    issues = _issues(plan, "SHOP_DEPTH")
    assert issues and issues[0].severity.value == "warning"
    assert "10 ft across" in issues[0].message and "storage, not a shop" in issues[0].message


def test_shop_at_exactly_twelve_feet_is_only_an_info():
    # 12 is the warning floor: `< 12` is strict, so a 12 ft shop clears the
    # warning tier (this is why the solver seed's 12 ft shop stays warning-free).
    plan = _shop(12, env_extra=4)
    assert _sev(plan, "SHOP_DEPTH") == {"info"}


def test_shop_between_twelve_and_twenty_is_a_comfort_info():
    plan = _shop(14, env_extra=4)
    issues = _issues(plan, "SHOP_DEPTH")
    assert issues and issues[0].severity.value == "info"
    assert "tight for a full-size truck" in issues[0].message


def test_shop_at_twenty_feet_is_clean():
    plan = _shop(20, env_extra=10)
    assert "SHOP_DEPTH" not in _codes(plan)


def test_garage_is_exempt_from_shop_depth():
    # Garages are sized to cars, not equipment — a 10 ft garage bay is normal.
    plan = _shop(10, rtype=T.GARAGE)
    assert "SHOP_DEPTH" not in _codes(plan)


# --- SHOP_DOOR_HEIGHT ---------------------------------------------------------
# A shop bay's overhead door should clear the bay (10 ft+). A 7-8 ft residential
# panel throttles the opening to car height. Only a SHOP door is judged; a garage
# door is fine at 7 ft.

_SHOP_DOOR = """\
plan "Shop Door"
envelope 50 x 30
ceiling 10
room living: living at 0,0 size 30 x 30
room {shop}: {rtype} at 30,0 size 20 x 30
door living - {shop} width 3
entry living south width 3 offset 4
door {shop} south overhead width 10 height {h} offset 4
"""


def _shop_door_infos(h="7", rtype="shop", shop="shop"):
    src = _SHOP_DOOR.format(h=h, rtype=rtype, shop=shop)
    return {d.code for d in compile_source(src).infos}


def test_short_overhead_door_on_a_shop_nudges_shop_door_height():
    r = compile_source(_SHOP_DOOR.format(h="7", rtype="shop", shop="shop"))
    hits = [d for d in r.infos if d.code == "SHOP_DOOR_HEIGHT"]
    assert hits and hits[0].severity.value == "info"
    assert "7 ft tall" in hits[0].message
    assert "height 10" in (hits[0].hint or "")


def test_eight_foot_door_still_nudges_but_ten_is_clean():
    assert "SHOP_DOOR_HEIGHT" in _shop_door_infos(h="8")
    assert "SHOP_DOOR_HEIGHT" not in _shop_door_infos(h="10")


def test_tall_commercial_height_is_now_a_stock_sectional_size():
    # 10/12/14 ft were added to the stock heights, so a compliant tall shop door
    # doesn't also trip the non-standard-size nudge (DOOR_SIZE).
    assert "DOOR_SIZE" not in _shop_door_infos(h="10")
    assert "DOOR_SIZE" not in _shop_door_infos(h="12")


def test_garage_overhead_door_is_exempt_from_shop_door_height():
    # A 7 ft door on a GARAGE bay is fine — cars clear it.
    assert "SHOP_DOOR_HEIGHT" not in _shop_door_infos(h="7", rtype="garage")


# --- LOFT_CEILING -------------------------------------------------------------
# A loft is habitable (sleeps people), so its effective ceiling must clear 7 ft.


def _loft(ceiling=9, override=None, vaulted=False):
    """A ground living room with a loft above it on level 1, fully stacked."""
    return (
        barndominium("x").envelope(24, 18).ceiling(ceiling)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=18)
        .add_room("loft", T.LOFT, x=0, y=0, width=24, length=18, level=1,
                  ceiling_height=override, vaulted=vaulted)
    )


def test_loft_with_a_low_override_ceiling_warns():
    plan = _loft(ceiling=9, override=6.5)
    issues = _issues(plan, "LOFT_CEILING")
    assert issues and issues[0].severity.value == "warning"
    assert "6.5 ft ceiling" in issues[0].message and "R305" in issues[0].message


def test_loft_inheriting_a_tall_plan_ceiling_is_clean():
    plan = _loft(ceiling=9)  # inherits 9 ft
    assert "LOFT_CEILING" not in _codes(plan)


def test_loft_at_exactly_seven_feet_is_clean():
    plan = _loft(ceiling=7)
    assert "LOFT_CEILING" not in _codes(plan)


def test_vaulted_loft_is_exempt():
    # Open to the ridge — usable height rises well past 7 ft, so the flat
    # effective-ceiling number doesn't describe it.
    plan = _loft(ceiling=9, override=6.5, vaulted=True)
    assert "LOFT_CEILING" not in _codes(plan)


# --- OFFICE_CLEARANCE ---------------------------------------------------------
# An office must hold a desk (4×2) with a 3 ft chair-pull behind it, clear of the
# door swing — a 4×5 clear box against some wall.


def _office(width, length, door=False):
    plan = (
        barndominium("x").envelope(width + 30, max(length, 20)).ceiling(9)
        .add_room("office", T.OFFICE, x=0, y=0, width=width, length=length)
        .add_room("living", T.LIVING, x=width, y=0, width=30, length=max(length, 20))
    )
    if door:
        plan.connect("office", "living", width=3, swing_into="office")
    return plan


def test_tiny_office_cannot_hold_a_desk():
    plan = _office(5, 5)
    issues = _issues(plan, "OFFICE_CLEARANCE")
    assert issues and issues[0].severity.value == "info"
    assert "desk" in issues[0].message


def test_roomy_office_is_clean():
    plan = _office(10, 10)
    assert "OFFICE_CLEARANCE" not in _codes(plan)


def test_door_swing_can_crowd_the_desk_out_of_a_marginal_office():
    # A 7×5 office fits the desk on its long wall with no door; a door swinging
    # into that wall eats the only viable spot — the keepout machinery is engaged,
    # the office analog of DOOR_HITS_FIXTURE.
    assert "OFFICE_CLEARANCE" not in _codes(_office(7, 5))
    assert "OFFICE_CLEARANCE" in _codes(_office(7, 5, door=True))


def test_elongated_office_trips_the_tightened_proportion_cap():
    # An office holds a desk, a chair-pull and a walk-around, so it gets the
    # bedroom's 1.8:1 cap — the score already penalises past 1.7:1, and without
    # the override an 8×17 corridor-with-a-desk lost points with no lint saying
    # why. 8×12 (1.5:1) stays clean; 8×17 (2.1:1) flags.
    assert "ROOM_PROPORTION" not in _codes(_office(8, 12))
    issues = _issues(_office(8, 17), "ROOM_PROPORTION")
    assert issues and "office" in issues[0].message.lower()


# --- registry -----------------------------------------------------------------


def test_new_codes_are_registered_with_the_right_severity():
    from barndsl.diagnostics import REGISTRY

    for code, sev in (
        ("SHOP_DEPTH", "warning"),  # strongest tier
        ("SHOP_DOOR_HEIGHT", "info"),
        ("LOFT_CEILING", "warning"),
        ("OFFICE_CLEARANCE", "info"),
    ):
        assert code in REGISTRY, code
        assert REGISTRY[code].severity.value == sev, code
    # SHOP_DEPTH is tiered, so it's in the _VARYING set.
    assert REGISTRY["SHOP_DEPTH"].varies
