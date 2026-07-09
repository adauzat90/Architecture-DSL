"""Door-swing direction checks and door-aware fixture placement.

Covers the swing-direction diagnostics (which way a leaf should open, whether an
inward exterior leaf clears, whether a swing crowds a fixture out) and the
placer change that keeps auto-placed fixtures out of a door's arc.
"""

from __future__ import annotations

from barndsl import compile_source
from barndsl.fixtures import (
    _door_swing_rects,
    _rects_overlap,
    plan_room_fixtures,
    resolve_room_fixtures,
)


def _codes(src: str) -> set[str]:
    res = compile_source(src)
    return {i.code for i in list(res.errors) + list(res.warnings) + list(res.infos)}


# --- overhead doors don't swing (the original false-clash fix) ----------------


def test_overhead_door_makes_no_phantom_swing_clash():
    # A garage door + a person-door near the same corner used to clash: the
    # overhead door was treated as swinging. It rides its tracks — no arc.
    src = """envelope 40 x 30
room shop: garage at 0,0 size 20 x 30
room mud: mudroom east-of shop size 20 x 30
door shop south overhead width 10 offset 0
door shop west exterior width 3 offset 0
door shop - mud
"""
    assert "DOOR_SWING_CLASH" not in _codes(src)


# --- exterior in-vs-out clearance ---------------------------------------------


def test_exterior_inward_leaf_cant_clear_shallow_room():
    src = """envelope 40 x 30
room mud: mudroom at 0,0 size 10 x 2.5
room shop: shop north-of mud size 10 x 20
door mud south exterior width 3
door mud - shop
"""
    assert "DOOR_SWING_INWARD" in _codes(src)


def test_exterior_inward_leaf_is_silent_when_it_clears():
    src = """envelope 40 x 30
room mud: mudroom at 0,0 size 10 x 8
room shop: shop north-of mud size 10 x 20
door mud south exterior width 3
door mud - shop
"""
    assert "DOOR_SWING_INWARD" not in _codes(src)


# --- privacy: a bed/bath door should open into the private room ---------------


def test_bedroom_door_swinging_out_into_hall_is_flagged():
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room bed: bedroom east-of hall size 12 x 12
door hall - bed into hall
"""
    assert "DOOR_SWING_PRIVACY" in _codes(src)


def test_bedroom_door_swinging_into_the_room_is_silent():
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room bed: bedroom east-of hall size 12 x 12
door hall - bed into bed
"""
    codes = _codes(src)
    assert "DOOR_SWING_PRIVACY" not in codes
    assert "DOOR_SWING_UNSET" not in codes


# --- undeclared direction that defaults the wrong way -------------------------


def test_undeclared_bath_door_defaulting_outward_is_flagged():
    src = """envelope 40 x 30
room hall: hallway at 20,0 size 6 x 30
room bath: bathroom west-of hall size 8 x 9
door hall - bath
"""
    assert "DOOR_SWING_UNSET" in _codes(src)


def test_pinning_the_direction_silences_the_unset_nudge():
    src = """envelope 40 x 30
room hall: hallway at 20,0 size 6 x 30
room bath: bathroom west-of hall size 8 x 9
door hall - bath into bath
"""
    assert "DOOR_SWING_UNSET" not in _codes(src)


# --- a swing that crowds a fixture out of a room with the capacity for it ------


def test_door_swing_crowding_out_a_fixture_warns():
    # A 5x9 bath has the floor area for toilet+lav+tub, but a door at offset 2
    # eats the wall the tub needs once its arc is kept clear.
    src = """envelope 13 x 20
room hall: hallway at 0,0 size 6 x 20
room bath: bathroom east-of hall size 5 x 9
door hall - bath into bath offset 2
"""
    assert "DOOR_HITS_FIXTURE" in _codes(src)


def test_a_pocket_door_reserves_no_arc_so_it_never_crowds():
    # Same tight bath, but a pocket door has no swing — nothing to crowd with.
    src = """envelope 13 x 20
room hall: hallway at 0,0 size 6 x 20
room bath: bathroom east-of hall size 5 x 9
door hall - bath pocket offset 2
"""
    assert "DOOR_HITS_FIXTURE" not in _codes(src)


# --- the placer itself keeps fixtures clear of a swing ------------------------


def test_placer_keeps_fixtures_out_of_the_door_arc():
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room bath: bathroom east-of hall size 9 x 11
door hall - bath into bath offset 2
"""
    plan = compile_source(src).plan
    bath = plan.room("bath")
    keepouts = _door_swing_rects(plan, bath)
    assert keepouts, "the swinging door should reserve an arc"
    for f in plan_room_fixtures(plan, bath):
        assert not any(
            _rects_overlap((f.x, f.y, f.width, f.length), b) for b in keepouts
        ), f"{f.kind} sits in the door swing"


# --- the DRAWN layout (resolve path) is door-aware too -------------------------
# resolve_room_fixtures is what the SVG/DXF/3D consume. It used to lay its seeds
# door-blind, so a drawing could show a door leaf sweeping through a toilet or a
# tub even while plan_room_fixtures (the check-side placer) avoided it.


def _no_swing_overlap(plan, room) -> None:
    keepouts = _door_swing_rects(plan, room)
    assert keepouts, "expected the hinged door to reserve an arc"
    for f in resolve_room_fixtures(plan, room):
        assert not any(
            _rects_overlap((f.x, f.y, f.width, f.length), b) for b in keepouts
        ), f"{f.kind} is drawn in the door swing"


def test_drawn_seed_fixtures_stay_out_of_an_interior_door_swing():
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room bath: bathroom east-of hall size 9 x 11
door hall - bath into bath offset 2
"""
    plan = compile_source(src).plan
    _no_swing_overlap(plan, plan.room("bath"))


def test_drawn_seed_fixtures_stay_out_of_an_entry_swing():
    # An exterior entry swings inward too: the laundry's washer/dryer seeds must
    # slide clear of its arc, not park inside it.
    src = """envelope 40 x 30
room laundry: laundry at 0,0 size 10 x 10
room living: living east-of laundry size 30 x 30
door laundry - living
entry laundry south width 3 offset 1
"""
    plan = compile_source(src).plan
    _no_swing_overlap(plan, plan.room("laundry"))


def test_overhead_door_reserves_no_keepout():
    src = """envelope 40 x 30
room shop: garage at 0,0 size 20 x 30
room mud: mudroom east-of shop size 20 x 30
door shop south overhead width 10 offset 0
door shop - mud
"""
    plan = compile_source(src).plan
    shop = plan.room("shop")
    # The only arc in the shop is the interior door's — the overhead adds none.
    assert len(_door_swing_rects(plan, shop)) == 1


def test_authored_wall_fixture_slides_clear_of_a_swing():
    # An authored piece placed by wall (no `at`) auto-slots like a seed: the
    # first free run on the wall PAST the door's arc.
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room office: office east-of hall size 12 x 12
door hall - office into office offset 2
fixture wardrobe in office wall W
"""
    plan = compile_source(src).plan
    office = plan.room("office")
    keepouts = _door_swing_rects(plan, office)
    ward = next(f for f in resolve_room_fixtures(plan, office) if f.kind == "wardrobe")
    assert not any(
        _rects_overlap((ward.x, ward.y, ward.width, ward.length), b) for b in keepouts
    )


def test_explicit_at_in_a_swing_is_a_fixture_door_warning():
    # Only an explicit `at x,y` can still park a fixture in a swing — and doing
    # so is now a WARNING (the leaf physically hits it), not a whisper.
    src = """envelope 40 x 30
room hall: hallway at 0,0 size 6 x 30
room office: office east-of hall size 12 x 12
door hall - office into office offset 2
fixture wardrobe in office at 0.5,2.5
"""
    res = compile_source(src)
    assert "FIXTURE_DOOR" in {d.code for d in res.warnings}
