"""Fixture/appliance seeds and the wet-room / kitchen clearance checks.

A residential room earns its keep by the fixtures in it. These pin the
deterministic fixture placer (seeds for the Revit builder), the necessary-
condition fit check, and the BATH_CLEARANCE / KITCHEN_FIT diagnostics.
"""

from barndsl import (
    RoomType as T,
)
from barndsl import (
    barndominium,
    fixtures_fit,
    fixtures_for,
    plan_room_fixtures,
    to_revit_model,
    validate,
)
from barndsl.validation import clear_box


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _big_bath(w, l):
    return (
        barndominium("x").envelope(w + 16, max(l, 20)).ceiling(9)
        .add_room("bath", T.BATHROOM, x=0, y=0, width=w, length=l)
        .add_room("rest", T.LIVING, x=w, y=0, width=16, length=max(l, 20))
    )


def test_fixtures_for_by_type():
    assert fixtures_for(T.BATHROOM) == ["toilet", "lavatory", "tub"]
    assert fixtures_for(T.HALF_BATH) == ["toilet", "lavatory"]
    assert "refrigerator" in fixtures_for(T.KITCHEN)
    assert fixtures_for(T.BEDROOM) == []  # bedrooms carry none


def test_placed_fixtures_sit_inside_the_clear_box():
    plan = _big_bath(8, 11)
    room = plan.room("bath")
    x0, y0, cw, cl = clear_box(plan, room)
    placed = plan_room_fixtures(plan, room)
    assert [f.kind for f in placed] == ["toilet", "lavatory", "tub"]
    for f in placed:
        assert x0 - 1e-6 <= f.x and f.x + f.width <= x0 + cw + 1e-6
        assert y0 - 1e-6 <= f.y and f.y + f.length <= y0 + cl + 1e-6
        assert f.wall in ("S", "N", "E", "W")


# --- the alcove tub -------------------------------------------------------------
# A tub is built into an alcove — spanning the room's short dimension wall-to-wall
# at one end — not floated as a 2.5 ft strip along the long wall (a live plan's
# 6 x 12 bath drew exactly that strip). The seed stretches the catalog 5 ft tub
# up to 6 ft to close the alcove; wider rooms keep the plain perimeter walk.


def test_seed_tub_spans_the_alcove_in_a_standard_bath():
    plan = _big_bath(6, 12)  # the live "Wheatland" mbath shape
    room = plan.room("bath")
    x0, y0, cw, cl = clear_box(plan, room)
    placed = plan_room_fixtures(plan, room)
    tub = next(f for f in placed if f.kind == "tub")
    assert tub.x == x0 and tub.width == cw  # wall-to-wall across the short side
    assert tub.wall in ("N", "S") and tub.length == 2.5
    assert tub.y in (y0, y0 + cl - 2.5)  # parked at one end of the long axis
    # The toilet and lavatory still land, clear of the tub.
    assert {f.kind for f in placed} == {"toilet", "lavatory", "tub"}


def test_seed_tub_keeps_the_perimeter_walk_when_no_alcove_fits():
    plan = _big_bath(8, 11)  # ~7.5 ft clear span: too wide to stretch a tub across
    room = plan.room("bath")
    tub = next(f for f in plan_room_fixtures(plan, room) if f.kind == "tub")
    assert 5.0 in (tub.width, tub.length)  # catalog width, unstretched


def test_seed_tub_alcove_dodges_the_door_end():
    from barndsl import compile_source
    from barndsl.fixtures import resolve_room_fixtures

    # The bath's door is on its north wall, so the north alcove end is a swing
    # keepout — the tub takes the south end instead.
    src = """\
plan "T"
envelope 22 x 20
ceiling 9
room bath: bathroom at 0,0 size 6 x 12
room living: living at 6,0 size 16 x 20
room hall: hallway at 0,12 size 6 x 8
door hall - bath width 2.67 offset 1 into bath
door hall - living width 3 offset 2
entry living south width 3 offset 6
window living south width 10 offset 4
"""
    r = compile_source(src)
    assert r.plan is not None
    bath = r.plan.room("bath")
    x0, y0, cw, _cl = clear_box(r.plan, bath)
    tub = next(f for f in resolve_room_fixtures(r.plan, bath) if f.kind == "tub")
    assert tub.wall == "S" and tub.y == y0
    assert tub.x == x0 and tub.width == cw


def test_fit_passes_a_reasonable_bath_fails_a_tiny_one():
    ok, _ = fixtures_fit(T.BATHROOM, 7.6, 10.5)  # a normal full bath
    assert ok
    bad, reason = fixtures_fit(T.BATHROOM, 3.5, 4.5)  # too small for tub + clearances
    assert not bad and reason


def test_bath_clearance_warns_on_a_cramped_bath():
    plan = _big_bath(4, 5)  # clear ~3.5 x 4.5 — no room for a tub + clearances
    assert "BATH_CLEARANCE" in _codes(plan)


def test_bath_clearance_silent_on_a_workable_bath():
    plan = _big_bath(8, 11)
    assert "BATH_CLEARANCE" not in _codes(plan)


def test_laundry_fit_warns_on_a_3ft_strip():
    # A live agent plan shipped a 3 x 13 "laundry" — a washer is 2.25 ft deep
    # and wants a 3 ft aisle to load, so a 3 ft strip can't work at all.
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("laundry", T.LAUNDRY, x=0, y=0, width=3, length=13)
        .add_room("rest", T.LIVING, x=3, y=0, width=21, length=20)
    )
    assert "LAUNDRY_FIT" in _codes(plan)


def test_laundry_fit_silent_on_a_workable_laundry():
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("laundry", T.LAUNDRY, x=0, y=0, width=6, length=8)
        .add_room("rest", T.LIVING, x=6, y=0, width=18, length=20)
    )
    assert "LAUNDRY_FIT" not in _codes(plan)


def test_kitchen_fit_is_an_info_not_a_warning():
    plan = (
        barndominium("x").envelope(24, 20).ceiling(9)
        .add_room("kit", T.KITCHEN, x=0, y=0, width=5, length=6)  # tiny galley
        .add_room("rest", T.LIVING, x=5, y=0, width=19, length=20)
    )
    report = validate(plan)
    codes_by_sev = {(i.code, i.severity.value) for i in report.issues}
    assert ("KITCHEN_FIT", "info") in codes_by_sev


def test_exchange_carries_fixture_seeds():
    plan = _big_bath(8, 11)
    data = to_revit_model(plan).to_dict()
    fixtures = [f for f in data["fixtures"] if f["room"] == "bath"]
    assert {f["kind"] for f in fixtures} == {"toilet", "lavatory", "tub"}
    for f in fixtures:
        assert f["id"].startswith("bath~")  # <room>~<kind>~<i>
        assert len(f["point"]) == 2
        assert f["level"] == 0
        assert f["seed"] is True  # bath fixtures are auto-seeded
