"""The bifold door kind and the closet craft checks.

A closet under 4 ft deep is a REACH-IN — nobody steps inside, so the door must
open (nearly) the closet's full width, centred: the bifold idiom. These pin the
`bifold` kind end-to-end (parse, emit, render) and the CLOSET_ACCESS /
CLOSET_DEPTH / CLOSET_WINDOW checks the review added around it.
"""

from barndsl import RoomType as T
from barndsl import barndominium, compile_source, validate
from barndsl.emit import emit_dsl


def _codes(plan):
    return {i.code for i in validate(plan).issues}


def _issues(plan, code):
    return [i for i in validate(plan).issues if i.code == code]


def _reach_in(door_kwargs, closet_w=11.0, closet_l=3.0):
    """A bedroom with a reach-in closet across its north wall, fully tiled."""
    plan = (
        barndominium("x").envelope(30, 12 + closet_l).ceiling(9)
        .add_room("bed", T.BEDROOM, x=0, y=0, width=closet_w, length=12)
        .add_room("closet", T.CLOSET, x=0, y=12, width=closet_w, length=closet_l)
        .add_room("rest", T.LIVING, x=closet_w, y=0, width=30 - closet_w,
                  length=12 + closet_l)
    )
    return plan.connect("bed", "closet", **door_kwargs)


# --- the bifold kind ----------------------------------------------------------


def test_bifold_parses_with_stock_default_width():
    src = """\
plan "T"
envelope 30 x 15
ceiling 9
room bed: bedroom at 0,0 size 11 x 12
room closet: closet at 0,12 size 11 x 3
room rest: living at 11,0 size 19 x 15
door bed - closet bifold offset 3.5
door bed - rest width 3
entry rest south width 3 offset 4
window bed south width 4 offset 3
"""
    r = compile_source(src)
    assert r.plan is not None
    d = next(d for d in r.plan.interior_doors if "closet" in (d.room_a, d.room_b))
    assert d.kind == "bifold" and d.width == 4.0  # the stock 48 in pair


def test_bifold_emits_and_round_trips():
    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5})
    text = emit_dsl(plan)
    assert "bifold" in text
    r = compile_source(text)
    assert r.plan is not None
    d = next(d for d in r.plan.interior_doors if "closet" in (d.room_a, d.room_b))
    assert d.kind == "bifold" and d.width == 8.0 and d.offset == 1.5


def test_bifold_renders_without_a_swing_arc():
    from barndsl.render import render_svg

    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5})
    svg = render_svg(plan)
    assert "<svg" in svg  # the zigzag glyph draws; no crash on the new kind


def test_bifold_skips_the_passage_width_and_swing_checks():
    # A stock 24 in bifold fronts a broom closet — storage, not passage — so
    # DOOR_NARROW stays quiet; and with no leaf arc, CLOSET_DOOR_SWING too.
    plan = _reach_in({"kind": "bifold", "width": 2.0, "offset": 4.5})
    codes = _codes(plan)
    assert "DOOR_NARROW" not in codes
    assert "CLOSET_DOOR_SWING" not in codes


def test_bifold_stock_widths_pass_door_size_odd_widths_dont():
    ok = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5})  # 96 in: two units
    assert "DOOR_SIZE" not in _codes(ok)
    odd = _reach_in({"kind": "bifold", "width": 7.0, "offset": 2.0})  # 84 in: not stock
    assert "DOOR_SIZE" in _codes(odd)


# --- CLOSET_ACCESS: the centred-bifold rule -----------------------------------


def test_person_door_at_one_end_of_a_reach_in_warns():
    # The old scaffold shape: a 2.5 ft swing door parked at the end of an 11 ft
    # reach-in strands 8 ft of rod past the jamb.
    plan = _reach_in({"width": 2.5, "offset": 0.5})
    issues = _issues(plan, "CLOSET_ACCESS")
    assert issues and issues[0].severity.value == "warning"
    assert "reach-in" in issues[0].message
    assert "bifold width 8 offset 1.5" in issues[0].hint  # snapped to stock, centred


def test_centred_stock_bifold_on_a_reach_in_is_silent():
    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5})
    assert "CLOSET_ACCESS" not in _codes(plan)


def test_walk_in_closet_takes_an_ordinary_door():
    # 5 ft deep: you step inside, so a person-door at one end is fine.
    plan = _reach_in({"width": 2.5, "offset": 0.5}, closet_l=5.0)
    assert "CLOSET_ACCESS" not in _codes(plan)


def test_walk_through_closet_with_two_openings_is_exempt():
    plan = _reach_in({"width": 2.5, "offset": 0.5})
    plan.connect("closet", "rest", width=2.5)  # second opening covers the far end
    assert "CLOSET_ACCESS" not in _codes(plan)


def test_reach_in_wider_than_stock_bifolds_says_split_or_reshape():
    # 14 ft of reach-in: even the 96 in double unit leaves 3 ft blind per side.
    plan = _reach_in({"width": 2.5, "offset": 0.5}, closet_w=14.0)
    issues = _issues(plan, "CLOSET_ACCESS")
    assert issues and "two openings" in issues[0].hint


# --- CLOSET_DEPTH: a rod needs 2 ft -------------------------------------------


def test_shallow_bedroom_closet_warns():
    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5}, closet_l=1.5)
    issues = _issues(plan, "CLOSET_DEPTH")
    assert issues and issues[0].severity.value == "warning"
    assert "hang" in issues[0].message


def test_shallow_hall_linen_closet_is_fine():
    # The same shallow closet off a hallway is legitimate shelf-only storage.
    plan = (
        barndominium("x").envelope(30, 13.5).ceiling(9)
        .add_room("hall", T.HALLWAY, x=0, y=0, width=11, length=12)
        .add_room("closet", T.CLOSET, x=0, y=12, width=11, length=1.5)
        .add_room("rest", T.LIVING, x=11, y=0, width=19, length=13.5)
        .connect("hall", "closet", kind="bifold", width=8.0, offset=1.5)
    )
    assert "CLOSET_DEPTH" not in _codes(plan)


def test_two_foot_closet_hangs_clothes():
    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5}, closet_l=2.0)
    assert "CLOSET_DEPTH" not in _codes(plan)


# --- CLOSET_WINDOW -------------------------------------------------------------


def test_window_in_a_closet_is_an_info_nudge():
    plan = _reach_in({"kind": "bifold", "width": 8.0, "offset": 1.5})
    plan.add_window("closet", "north", width=3, offset=4)
    report = validate(plan)
    sev = {(i.code, i.severity.value) for i in report.issues}
    assert ("CLOSET_WINDOW", "info") in sev


def test_swing_hint_now_recommends_the_real_bifold_grammar():
    # CLOSET_DOOR_SWING's fix used to name a kind the grammar didn't have.
    plan = _reach_in({"width": 3.5, "offset": 1.0})  # 3.5 ft leaf > 3 ft depth
    issues = _issues(plan, "CLOSET_DOOR_SWING")
    assert issues and "bifold" in issues[0].hint
