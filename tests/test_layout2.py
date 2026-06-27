"""Tests for auto-layout 2.0 — the space-filling band/slicing engine."""

from __future__ import annotations

import pytest

from barndsl import compile_source, emit_dsl
from barndsl.elements import HABITABLE_TYPES, RoomType
from barndsl.layout import LayoutBrief, RoomSpec, solve_layout
from barndsl.layout2 import (
    LayoutBrief2,
    RoomSpec2,
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
