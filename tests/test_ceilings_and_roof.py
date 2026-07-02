"""Per-room ceiling heights, vaulted rooms, and roof-form selection (P3)."""

from barndsl import RoomType as T
from barndsl import barndominium, compile_source, emit_dsl, to_revit_model, validate
from barndsl.revit import roof_plan


def _codes(plan):
    return {i.code for i in validate(plan).issues}


# --- per-room ceiling + vaulted ---------------------------------------------


def test_room_ceiling_and_vaulted_parse_and_round_trip():
    src = (
        'plan "x"\nenvelope 40 x 24\nceiling 9\n'
        "room great: living at 0,0 size 24 x 24 ceiling 18 vaulted\n"
        "room bed: bedroom at 24,0 size 16 x 24\n"
        "entry great south width 3 offset 10\n"
        "window great south width 10 offset 6\n"
        "window bed east width 4 offset 4\n"
    )
    plan = compile_source(src).plan
    great = plan.room("great")
    assert great.ceiling_height == 18 and great.vaulted is True
    assert plan.room("bed").ceiling_height is None
    # Round-trips through emit.
    src2 = emit_dsl(plan)
    assert "ceiling 18 vaulted" in src2
    again = compile_source(src2, name=plan.name).plan
    assert emit_dsl(again) == src2


def test_low_room_ceiling_is_an_error():
    plan = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=16, ceiling_height=6)
        .entrance("living", "south", width=3, offset=8)
    )
    assert "CEILING" in _codes(plan)


def test_vaulted_room_skips_the_low_ceiling_check():
    plan = (
        barndominium("x").envelope(20, 16).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=16,
                  ceiling_height=6, vaulted=True)
        .entrance("living", "south", width=3, offset=8)
    )
    # A vaulted room's height is the roof; the flat-ceiling minimum doesn't apply.
    assert "CEILING" not in _codes(plan)


def test_room_ceiling_reaches_the_revit_exchange():
    plan = (
        barndominium("x").envelope(40, 24).ceiling(9)
        .add_room("great", T.LIVING, x=0, y=0, width=24, length=24,
                  ceiling_height=18, vaulted=True)
        .add_room("bed", T.BEDROOM, x=24, y=0, width=16, length=24)
        .entrance("great", "south", width=3, offset=10)
        .add_window("great", "south", width=10, offset=6)
        .add_window("bed", "east", width=4, offset=4)
    )
    data = to_revit_model(plan).to_dict()
    rooms = {r["id"]: r for r in data["rooms"]}
    assert rooms["great"]["ceiling_height"] == 18 and rooms["great"]["vaulted"] is True
    # A room with no override inherits the plan ceiling in the exchange.
    assert rooms["bed"]["ceiling_height"] == 9 and rooms["bed"]["vaulted"] is False


# --- roof forms --------------------------------------------------------------


def test_roof_directive_parses_and_round_trips():
    src = (
        'plan "x"\nenvelope 40 x 24\nceiling 9\nroof monitor pitch 0.5\n'
        "room living: living at 0,0 size 40 x 24\n"
        "entry living south width 3 offset 10\n"
    )
    plan = compile_source(src).plan
    assert plan.roof_style == "monitor" and plan.roof_pitch == 0.5
    assert "roof monitor pitch 0.5" in emit_dsl(plan)


def test_gable_is_the_default_and_not_emitted():
    plan = barndominium("x").envelope(20, 16).ceiling(9)
    assert plan.roof_style == "gable"
    assert "roof" not in emit_dsl(
        plan.add_room("a", T.LIVING, x=0, y=0, width=20, length=16)
    )


def test_shed_roof_rises_across_the_full_span_and_slopes_one_eave():
    plan = (
        barndominium("x").envelope(40, 24).ceiling(9).roof("shed")
        .add_room("living", T.LIVING, x=0, y=0, width=40, length=24)
    )
    gable = roof_plan(
        barndominium("g").envelope(40, 24).ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=40, length=24),
        0,
    )
    shed = roof_plan(plan, 0)
    assert shed["style"] == "shed"
    # A shed rises across the whole span → twice a gable's centre rise.
    assert abs(shed["rise"] - 2 * gable["rise"]) < 1e-6
    # Exactly one outline edge is slope-defining (the single plane).
    assert sum(1 for s in shed["outline_slopes"] if s) == 1


def test_bad_roof_style_is_a_diagnostic():
    r = compile_source(
        'plan "x"\nenvelope 20 x 16\nceiling 9\nroof spanish\n'
        "room a: living at 0,0 size 20 x 16\n"
    )
    assert any(d.code == "BAD_OPTION" for d in r.errors)
