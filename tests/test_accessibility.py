"""Opt-in accessibility / aging-in-place nudges (ANSI A117.1-flavoured).

These only fire when a plan declares an `accessible` target, so ordinary plans
are never held to an accessible standard. All are INFO — guidance, not blocking.
"""

from barndsl import RoomType as T
from barndsl import barndominium, compile_source, emit_dsl, validate


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _accessible_base(**kw):
    """A one-floor plan with a bedroom + full bath on the ground, wide doors."""
    p = (
        barndominium("x").envelope(44, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=24)
        .add_room("bed", T.BEDROOM, x=24, y=0, width=20, length=14)
        .add_room("bath", T.BATHROOM, x=24, y=14, width=20, length=10)
        .connect("living", "bed", width=3.0)
        .connect("living", "bath", width=3.0)
        .entrance("living", "south", width=3.0, offset=10)
    )
    if kw.get("accessible", True):
        p.mark_accessible()
    return p


# --- opt-in gating -----------------------------------------------------------


def test_no_accessibility_codes_by_default():
    plan = _accessible_base(accessible=False)
    assert not any(c.startswith("ACCESS_") for c in _codes(plan))


def test_accessible_directive_parses_and_round_trips():
    src = 'plan "x"\nenvelope 30 x 20\nceiling 9\naccessible\n'
    src += "room a: living at 0,0 size 30 x 20\nentry a south width 3 offset 6\n"
    plan = compile_source(src).plan
    assert plan.accessible is True
    assert "accessible" in emit_dsl(plan)
    assert compile_source(emit_dsl(plan), name=plan.name).plan.accessible is True


def test_a_well_formed_accessible_plan_is_quiet_on_the_geometry_checks():
    # Wide doors, a turnable bath, single-floor living: only the no-step-entry
    # reminder (which can't be verified from geometry) remains.
    codes = _codes(_accessible_base())
    assert "ACCESS_DOOR" not in codes
    assert "ACCESS_BATH" not in codes
    assert "ACCESS_SINGLE_FLOOR" not in codes
    assert "ACCESS_ENTRY" in codes  # always-on reminder in accessible mode


# --- the individual nudges ---------------------------------------------------


def test_narrow_doors_flag_access_door():
    plan = _accessible_base()
    # Shrink the interior doors below the 34 in accessible leaf.
    for d in plan.interior_doors:
        d.width = 2.5  # 30 in
    assert "ACCESS_DOOR" in _codes(plan)


def test_bath_without_turning_space_flags_access_bath():
    plan = _accessible_base()
    plan.room("bath").width = 5  # clear short ~4.6 ft, under the 60 in circle
    assert "ACCESS_BATH" in _codes(plan)


def test_bedroom_only_upstairs_flags_single_floor():
    plan = (
        barndominium("x").envelope(30, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=16, length=14, level=1)
        .add_room("bath", T.BATHROOM, x=16, y=0, width=12, length=14, level=1)
        .entrance("living", "south", width=3.0, offset=10)
        .mark_accessible()
    )
    codes = _codes(plan)
    assert "ACCESS_SINGLE_FLOOR" in codes


def test_garage_door_is_not_held_to_the_accessible_route():
    # A narrow door into a garage/shop isn't on the accessible living route.
    plan = (
        barndominium("x").envelope(56, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=24, length=24)
        .add_room("bed", T.BEDROOM, x=24, y=0, width=20, length=14)
        .add_room("bath", T.BATHROOM, x=24, y=14, width=20, length=10)
        .add_room("shop", T.SHOP, x=44, y=0, width=12, length=24)
        .connect("living", "bed", width=3.0)
        .connect("living", "bath", width=3.0)
        .connect("shop", "living", width=2.5)  # narrow, but into a shop
        .entrance("living", "south", width=3.0, offset=10)
        .mark_accessible()
    )
    door_issues = [i for i in validate(plan).issues if i.code == "ACCESS_DOOR"]
    assert not any("shop" in i.message for i in door_issues)
