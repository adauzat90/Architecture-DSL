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
