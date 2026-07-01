"""Orientation, exterior-finish hints, and the cross-floor load-path check (P6)."""

from barndsl import RoomType as T
from barndsl import (
    barndominium,
    compile_source,
    emit_dsl,
    exchange_to_plan,
    to_revit_model,
    validate,
)


def _codes(plan):
    return {i.code for i in validate(plan).issues}


# --- orientation + finish ----------------------------------------------------


def test_orientation_and_finish_parse_round_trip_and_reach_exchange():
    src = (
        'plan "x"\nenvelope 40 x 24\nceiling 9\n'
        'orientation 30\nfinish siding "Metal - Corrugated" roof "Standing Seam Metal"\n'
        "room living: living at 0,0 size 40 x 24\n"
        "entry living south width 3 offset 10\n"
    )
    plan = compile_source(src).plan
    assert plan.orientation == 30
    assert plan.siding == "Metal - Corrugated" and plan.roofing == "Standing Seam Metal"
    # Emit round-trips as a fixed point.
    src2 = emit_dsl(plan)
    assert "orientation 30" in src2 and 'finish siding "Metal - Corrugated"' in src2
    again = compile_source(src2, name=plan.name).plan
    assert emit_dsl(again) == src2
    # And the hints reach the Revit exchange.
    data = to_revit_model(plan).to_dict()
    assert data["plan"]["orientation"] == 30
    assert data["plan"]["siding"] == "Metal - Corrugated"
    assert data["plan"]["roofing"] == "Standing Seam Metal"
    # The exchange round-trips back to a plan carrying the same values.
    rt = exchange_to_plan(data)
    assert rt.orientation == 30 and rt.siding == "Metal - Corrugated"


def test_orientation_normalises_and_defaults_are_not_emitted():
    plan = barndominium("x").envelope(20, 16).ceiling(9).orient(390)
    assert plan.orientation == 30  # 390 % 360
    base = barndominium("y").envelope(20, 16).ceiling(9)
    assert "orientation" not in emit_dsl(
        base.add_room("a", T.LIVING, x=0, y=0, width=20, length=16)
    )


# --- cross-floor load path ---------------------------------------------------


def test_unsupported_upper_partition_is_flagged():
    # Two upper rooms split by a partition mid-span over a single open room below.
    plan = (
        barndominium("x").envelope(40, 24).ceiling(9)
        .add_room("great", T.LIVING, x=0, y=0, width=40, length=24)
        .add_room("loftA", T.LOFT, x=0, y=0, width=20, length=24, level=1)
        .add_room("loftB", T.OFFICE, x=20, y=0, width=20, length=24, level=1)
        .entrance("great", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=4, length=12, from_level=0, to_level=1)
    )
    # The loftA|loftB partition sits at x=20, over the open middle of `great`.
    assert "LOAD_PATH" in _codes(plan)


def test_supported_upper_partition_is_not_flagged():
    # The upper partition at x=20 aligns with a wall between two lower rooms.
    plan = (
        barndominium("x").envelope(40, 24).ceiling(9)
        .add_room("livW", T.LIVING, x=0, y=0, width=20, length=24)
        .add_room("livE", T.KITCHEN, x=20, y=0, width=20, length=24)
        .add_room("loftA", T.LOFT, x=0, y=0, width=20, length=24, level=1)
        .add_room("loftB", T.OFFICE, x=20, y=0, width=20, length=24, level=1)
        .entrance("livW", "south", width=3, offset=10)
        .add_stair("s", x=0, y=0, width=4, length=12, from_level=0, to_level=1)
    )
    assert "LOAD_PATH" not in _codes(plan)


def test_single_storey_plan_has_no_load_path_check():
    plan = (
        barndominium("x").envelope(40, 24).ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=20, length=24)
        .add_room("b", T.KITCHEN, x=20, y=0, width=20, length=24)
        .entrance("a", "south", width=3, offset=10)
    )
    assert "LOAD_PATH" not in _codes(plan)
