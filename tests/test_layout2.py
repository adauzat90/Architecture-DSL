"""Tests for auto-layout 2.0 — the space-filling band/slicing engine."""

from __future__ import annotations

import pytest

from barndsl import compile_source, emit_dsl
from barndsl.elements import HABITABLE_TYPES, RoomType
from barndsl.layout import LayoutBrief, RoomSpec, solve_layout
from barndsl.layout2 import (
    LayoutBrief2,
    RoomSpec2,
    _proportion_penalty,
    _score,
    parse_brief2,
    solve_layout2,
)
from barndsl.validation import exterior_walls


def _brief(**kw) -> LayoutBrief2:
    return LayoutBrief2(
        name="Test",
        rooms=[
            RoomSpec2("living", "living", area=360),
            RoomSpec2("kitchen", "kitchen", area=240),
            RoomSpec2("hall", "hallway", area=140, min_dim=4),
            RoomSpec2("bed1", "bedroom", area=168),
            RoomSpec2("bed2", "bedroom", area=144),
            RoomSpec2("bath", "bathroom", area=90),
        ],
        adjacencies=[
            ("living", "kitchen"),
            ("living", "hall"),
            ("hall", "bed1"),
            ("hall", "bed2"),
            ("hall", "bath"),
        ],
        **kw,
    )


# --- the core guarantees ----------------------------------------------------


def test_tiles_the_envelope_with_almost_no_waste():
    plan = solve_layout2(_brief()).plan
    assigned = sum(r.area for r in plan.rooms)
    footprint = plan.envelope_width * plan.envelope_length
    assert footprint > 0
    assert assigned / footprint > 0.99  # the dissection fills the rectangle


def test_no_rooms_overlap():
    rooms = solve_layout2(_brief()).plan.rooms
    for i, a in enumerate(rooms):
        for b in rooms[i + 1 :]:
            assert a.overlaps(b) == 0, f"{a.id} overlaps {b.id}"


def test_every_room_is_within_the_envelope():
    plan = solve_layout2(_brief()).plan
    for r in plan.rooms:
        assert r.x >= -1e-6 and r.y >= -1e-6
        assert r.x2 <= plan.envelope_width + 1e-6
        assert r.y2 <= plan.envelope_length + 1e-6


def test_habitable_rooms_land_on_the_perimeter():
    plan = solve_layout2(_brief()).plan
    for r in plan.rooms:
        if r.type in HABITABLE_TYPES:
            assert exterior_walls(plan, r), f"{r.id} has no exterior wall"


def test_compiles_clean():
    out = solve_layout2(_brief())
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()
    assert not any(d.code == "NO_ACCESS" for d in result.diagnostics)


def test_is_deterministic():
    a = emit_dsl(solve_layout2(_brief()).plan)
    b = emit_dsl(solve_layout2(_brief()).plan)
    assert a == b


def test_round_trips_losslessly_through_dsl():
    # The dissection's shared walls must survive emit's rounding (the grid-snap).
    plan = solve_layout2(_brief()).plan
    src = emit_dsl(plan)
    rr = compile_source(src)
    assert rr.plan is not None
    assert not rr.errors, rr.report()
    assert emit_dsl(rr.plan) == src  # idempotent


def test_fixed_envelope_is_honored():
    out = solve_layout2(_brief(envelope=(48, 32)))
    assert out.plan.envelope_width == 48 and out.plan.envelope_length == 32
    result = compile_source(emit_dsl(out.plan))
    assert not result.errors, result.report()


# --- connectivity & doors ---------------------------------------------------


def test_connectivity_pass_keeps_every_room_reachable():
    # A program where a room can't be a row-neighbour of all its requested
    # neighbours: the engine must still connect everything.
    brief = LayoutBrief2(
        name="Hub",
        rooms=[
            RoomSpec2("living", "living", area=400),
            RoomSpec2("kitchen", "kitchen", area=300),
            RoomSpec2("dining", "dining", area=200),
            RoomSpec2("mud", "mudroom", area=120),
        ],
        # kitchen wants three row-neighbours (living, dining, mud) — impossible.
        adjacencies=[("kitchen", "living"), ("kitchen", "dining"), ("kitchen", "mud")],
    )
    out = solve_layout2(brief)
    result = compile_source(emit_dsl(out.plan))
    assert not any(d.code == "NO_ACCESS" for d in result.diagnostics), result.report()


def test_bedrooms_get_egress_windows():
    plan = solve_layout2(_brief()).plan
    for bed in ("bed1", "bed2"):
        assert plan.windows_for(bed)


# --- room proportions -------------------------------------------------------


def _aspect(r):
    return max(r.width, r.length) / min(r.width, r.length)


def test_bedrooms_are_reasonably_square():
    # Bedrooms should not come out long and thin (the band depth is sized to keep
    # them square). 1.8 is generous; in practice they land near 1.1–1.4.
    plan = solve_layout2(_brief()).plan
    for r in plan.rooms:
        if r.type is RoomType.BEDROOM:
            assert _aspect(r) <= 1.8, f"{r.id} is {_aspect(r):.2f}:1 — too elongated"


def test_a_big_shop_does_not_stretch_the_bedrooms():
    # A large garage/shop must not share the bedroom band (its bulk would force a
    # deep band and stretch the bedrooms long and thin); it gets its own band, and
    # no habitable room is buried.
    brief = LayoutBrief2(
        name="ShopHouse",
        rooms=[
            RoomSpec2("great", "living", area=380),
            RoomSpec2("kitchen", "kitchen", area=240),
            RoomSpec2("hall", "hallway", area=130, min_dim=4),
            RoomSpec2("bed1", "bedroom", area=200),
            RoomSpec2("bed2", "bedroom", area=160),
            RoomSpec2("bed3", "bedroom", area=150),
            RoomSpec2("bath", "bathroom", area=90),
            RoomSpec2("mud", "mudroom", area=100),
            RoomSpec2("shop", "shop", width=30, length=40),
        ],
        adjacencies=[
            ("great", "kitchen"), ("great", "hall"),
            ("hall", "bed1"), ("hall", "bed2"), ("hall", "bed3"), ("hall", "bath"),
            ("great", "mud"), ("mud", "shop"),
        ],
    )
    out = solve_layout2(brief)
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()  # no buried bedrooms
    for r in out.plan.rooms:
        if r.type is RoomType.BEDROOM:
            assert _aspect(r) <= 1.8, f"{r.id} is {_aspect(r):.2f}:1"


def test_proportion_penalty_prefers_square_rooms():
    from barndsl.elements import Barndominium, Room, RoomType as T

    def plan_with(w, l):
        p = Barndominium("p", 40, 40, 9)
        p.rooms.append(Room("bed", T.BEDROOM, 0, 0, w, l))
        return p

    square = _proportion_penalty(plan_with(12, 12))
    thin = _proportion_penalty(plan_with(6, 24))
    assert square == 0.0  # 1:1 is below the threshold — no penalty
    assert thin > square  # a 4:1 bedroom is penalised


# --- beats v1 ---------------------------------------------------------------


def test_fill_wastes_far_less_than_greedy():
    rooms = [
        ("living", "living", 24, 26), ("kitchen", "kitchen", 16, 26),
        ("dining", "dining", 14, 14), ("hall", "hallway", 24, 4),
        ("bed1", "bedroom", 12, 12), ("bed2", "bedroom", 12, 12),
        ("bath", "bathroom", 8, 10), ("master", "bedroom", 14, 14),
    ]
    adj = [
        ("living", "kitchen"), ("kitchen", "dining"), ("living", "hall"),
        ("hall", "bed1"), ("hall", "bed2"), ("hall", "bath"), ("hall", "master"),
    ]

    def unused(plan):
        return 1 - sum(r.area for r in plan.rooms) / (
            plan.envelope_width * plan.envelope_length
        )

    g = solve_layout(
        LayoutBrief("g", rooms=[RoomSpec(i, t, w, l) for i, t, w, l in rooms], adjacencies=adj)
    ).plan
    f = solve_layout2(
        LayoutBrief2(
            "f",
            rooms=[
                RoomSpec2(i, t, area=w * l, min_dim=(4 if t == "hallway" else min(w, l)))
                for i, t, w, l in rooms
            ],
            adjacencies=adj,
        )
    ).plan
    assert unused(f) < 0.02
    assert unused(f) < unused(g) - 0.2  # dramatically tighter


# --- topology selection (auto) ----------------------------------------------


def _fan_brief() -> LayoutBrief2:
    # kitchen wants three neighbours — impossible for a flat band row.
    return LayoutBrief2(
        name="Fan",
        rooms=[
            RoomSpec2("living", "living", area=400),
            RoomSpec2("kitchen", "kitchen", area=300),
            RoomSpec2("dining", "dining", area=200),
            RoomSpec2("mud", "mudroom", area=120),
        ],
        adjacencies=[("kitchen", "living"), ("kitchen", "dining"), ("kitchen", "mud")],
    )


def test_auto_picks_slice_when_it_beats_bands():
    out = solve_layout2(_fan_brief())  # engine="auto" default
    assert out.unsatisfied == []  # all three of kitchen's neighbours satisfied
    assert any("slice" in n for n in out.notes)
    result = compile_source(emit_dsl(out.plan))
    assert not result.errors, result.report()


def test_auto_is_never_worse_than_bands():
    # On the residential corpus, auto must score <= bands (bands is a candidate).
    out_auto = solve_layout2(_brief())
    out_bands = solve_layout2(_brief(), engine="bands")
    assert _score(out_auto) <= _score(out_bands)
    # and for the band-friendly program, auto should match bands (no buried rooms)
    for r in out_auto.plan.rooms:
        if r.type in HABITABLE_TYPES:
            assert exterior_walls(out_auto.plan, r)


def test_auto_is_deterministic():
    assert emit_dsl(solve_layout2(_fan_brief()).plan) == emit_dsl(
        solve_layout2(_fan_brief()).plan
    )


def test_slice_engine_produces_valid_geometry():
    plan = solve_layout2(_fan_brief(), engine="slice").plan
    for i, a in enumerate(plan.rooms):
        for b in plan.rooms[i + 1 :]:
            assert a.overlaps(b) == 0
    for r in plan.rooms:
        assert r.x2 <= plan.envelope_width + 1e-6
        assert r.y2 <= plan.envelope_length + 1e-6


def test_unknown_engine_rejected():
    with pytest.raises(ValueError):
        solve_layout2(_brief(), engine="nonsense")


# --- brief parsing ----------------------------------------------------------


def test_parse_brief2_area_and_fixed_forms():
    brief = parse_brief2(
        'plan "P"\n'
        "ceiling 10\n"
        "room living: living area 360\n"
        "room hall: hallway area 120 min 4\n"
        "room bed: bedroom 12 x 12\n"
        "adjacent living hall bed\n"
        "entry living\n"
    )
    assert brief.name == "P"
    assert brief.ceiling == 10
    by_id = {r.id: r for r in brief.rooms}
    assert by_id["living"].area == 360
    assert by_id["hall"].min_dim == 4
    assert by_id["bed"].area == 144  # 12 x 12
    assert ("living", "hall") in brief.adjacencies
    assert ("living", "bed") in brief.adjacencies  # hub, not chain


def test_parse_brief2_rejects_garbage():
    with pytest.raises(ValueError):
        parse_brief2("room oops bedroom 10 x 10\n")  # missing colon
    with pytest.raises(ValueError):
        parse_brief2("wat is this\n")


# --- input validation -------------------------------------------------------


def test_roomspec2_needs_a_size():
    with pytest.raises(ValueError):
        RoomSpec2("x", "bedroom")  # neither area nor w&l


def test_roomspec2_bad_type_raises():
    with pytest.raises(ValueError):
        RoomSpec2("x", "lounge", area=100)


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        solve_layout2(
            LayoutBrief2("d", rooms=[RoomSpec2("a", "living", area=100),
                                     RoomSpec2("a", "bedroom", area=100)])
        )


def test_empty_brief_rejected():
    with pytest.raises(ValueError):
        solve_layout2(LayoutBrief2("e", rooms=[]))


# --- the shipped example ----------------------------------------------------


def test_oakline_example_compiles_clean():
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "examples", "oakline.brief")
    out = solve_layout2(parse_brief2(open(path).read()))
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()
    assert not result.warnings, result.report()
    assert out.unsatisfied == []


def test_score_folds_in_the_design_score():
    """Topology selection optimizes the same 0-100 the agent loop maximises.

    Two candidates identical on errors/unmet adjacencies: the one whose plan
    carries a design defect (a narrow door -> DOOR_NARROW warning) must rank
    strictly worse, because the design score is the tuple's next term.
    """
    from barndsl.layout2 import LayoutResult

    src = (
        'plan "s"\nenvelope 30 x 24\nceiling 9\n'
        "room living: living at 0,0 size 18 x 24\n"
        "room bed: bedroom at 18,0 size 12 x 24\n"
        "door living - bed width {w}\n"
        "entry living south width 3 offset 4\n"
        "window bed south width 4 offset 3\n"
        "window living south width 6 offset 8\n"
    )
    clean = compile_source(src.format(w=3)).plan
    narrow = compile_source(src.format(w=2)).plan
    s_clean = _score(LayoutResult(clean, [], [], []))
    s_narrow = _score(LayoutResult(narrow, [], [], []))
    assert s_clean[:2] == s_narrow[:2]  # same errors / unmet adjacencies
    assert s_clean < s_narrow  # the design score decides
    assert s_narrow[2] - s_clean[2] >= 8  # a warning costs its 8 points
