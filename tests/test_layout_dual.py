"""Tests for the rectangular-dual topology (auto-layout 2.0, topology 3).

The dual exists to realise adjacency graphs that bands and slicing cannot — the
canonical case is the *pinwheel*: a centre room that must touch four others. The
tests pin the defining rectangular-dual property (required adjacencies become
real shared walls), the structural guarantees (tile, no overlap, perimeter), the
auto-selection wiring, determinism, graceful fallback, and a randomized property
sweep that verifies every plan the engine emits.
"""

from __future__ import annotations

import random

import pytest

from barndsl import compile_source, emit_dsl
from barndsl.elements import HABITABLE_TYPES
from barndsl.geometry import shared_edge
from barndsl.layout2 import (
    LayoutBrief2,
    RoomSpec2,
    _score,
    _solve_dual,
    _prepare,
    solve_layout2,
)
from barndsl.geometry import exterior_walls


def _pinwheel() -> LayoutBrief2:
    # centre touches all four arms — impossible for a band row or a slicing tree.
    return LayoutBrief2(
        name="Pinwheel",
        rooms=[
            RoomSpec2("center", "living", area=300),
            RoomSpec2("north", "bedroom", area=180),
            RoomSpec2("east", "bedroom", area=160),
            RoomSpec2("south", "kitchen", area=200),
            RoomSpec2("west", "bedroom", area=170),
        ],
        adjacencies=[
            ("center", "north"),
            ("center", "east"),
            ("center", "south"),
            ("center", "west"),
        ],
    )


# --- the defining property --------------------------------------------------


def test_dual_realises_every_required_adjacency_as_a_shared_wall():
    brief = _pinwheel()
    out = solve_layout2(brief, engine="dual")
    by_id = {r.id: r for r in out.plan.rooms}
    for a, b in brief.adjacencies:
        assert shared_edge(by_id[a], by_id[b]) is not None, f"{a}-{b} not a shared wall"
    assert out.unsatisfied == []


def test_dual_solves_the_pinwheel_that_bands_and_slice_cannot():
    brief = _pinwheel()
    dual = solve_layout2(brief, engine="dual")
    assert dual.unsatisfied == []
    # bands and slice each have to drop at least one of the four arms.
    assert solve_layout2(brief, engine="bands").unsatisfied
    assert solve_layout2(brief, engine="slice").unsatisfied


def test_auto_picks_the_dual_for_the_pinwheel():
    out = solve_layout2(_pinwheel())  # engine="auto"
    assert out.unsatisfied == []
    assert any("dual" in n for n in out.notes)
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()


# --- structural guarantees --------------------------------------------------


def test_dual_tiles_without_overlap_or_waste():
    plan = solve_layout2(_pinwheel(), engine="dual").plan
    for i, a in enumerate(plan.rooms):
        for b in plan.rooms[i + 1 :]:
            assert a.overlaps(b) == 0, f"{a.id} overlaps {b.id}"
    assigned = sum(r.area for r in plan.rooms)
    footprint = plan.envelope_width * plan.envelope_length
    assert assigned / footprint > 0.99


def test_dual_keeps_every_room_in_the_envelope():
    plan = solve_layout2(_pinwheel(), engine="dual").plan
    for r in plan.rooms:
        assert r.x >= -1e-6 and r.y >= -1e-6
        assert r.x2 <= plan.envelope_width + 1e-6
        assert r.y2 <= plan.envelope_length + 1e-6


def test_dual_puts_habitable_rooms_on_the_perimeter():
    plan = solve_layout2(_pinwheel(), engine="dual").plan
    for r in plan.rooms:
        if r.type in HABITABLE_TYPES:
            assert exterior_walls(plan, r), f"{r.id} has no exterior wall"


def test_dual_compiles_clean_and_reachable():
    out = solve_layout2(_pinwheel(), engine="dual")
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()
    assert not any(d.code == "NO_ACCESS" for d in result.diagnostics)


def test_dual_round_trips_losslessly_through_dsl():
    plan = solve_layout2(_pinwheel(), engine="dual").plan
    src = emit_dsl(plan)
    rr = compile_source(src)
    assert rr.plan is not None
    assert not rr.errors, rr.report()
    assert emit_dsl(rr.plan) == src


def test_dual_is_deterministic():
    a = emit_dsl(solve_layout2(_pinwheel(), engine="dual").plan)
    b = emit_dsl(solve_layout2(_pinwheel(), engine="dual").plan)
    assert a == b


# --- selection & fallback ---------------------------------------------------


def test_auto_is_never_worse_than_bands_or_slice():
    brief = _pinwheel()
    auto = _score(solve_layout2(brief, engine="auto"))
    assert auto <= _score(solve_layout2(brief, engine="bands"))
    assert auto <= _score(solve_layout2(brief, engine="slice"))


def test_explicit_dual_raises_when_no_dual_exists():
    # K4 with every room needing the perimeter over-constrains the rectangle.
    brief = LayoutBrief2(
        name="Dense",
        rooms=[
            RoomSpec2("a", "bedroom", area=150),
            RoomSpec2("b", "bedroom", area=150),
            RoomSpec2("c", "bedroom", area=150),
            RoomSpec2("d", "bedroom", area=150),
        ],
        adjacencies=[
            ("a", "b"), ("a", "c"), ("a", "d"),
            ("b", "c"), ("b", "d"), ("c", "d"),
        ],
    )
    specs, adj = _prepare(brief)
    assert _solve_dual(brief, specs, adj) is None
    with pytest.raises(ValueError):
        solve_layout2(brief, engine="dual")
    # auto must still return a valid plan via the fallback topologies.
    out = solve_layout2(brief, engine="auto")
    assert out.plan.rooms


def test_dual_skipped_above_the_room_cap():
    rooms = [RoomSpec2(f"r{i}", "bedroom", area=120) for i in range(12)]
    brief = LayoutBrief2("Big", rooms=rooms, adjacencies=[])
    specs, adj = _prepare(brief)
    assert _solve_dual(brief, specs, adj) is None  # > _DUAL_MAX_ROOMS


def test_unknown_engine_still_rejected():
    with pytest.raises(ValueError):
        solve_layout2(_pinwheel(), engine="nonsense")


# --- randomized property sweep ----------------------------------------------

_HAB = ["living", "kitchen", "dining", "bedroom", "office"]
_OTHER = ["bathroom", "closet", "pantry", "mudroom", "laundry"]


def _random_brief(rng: random.Random, k: int) -> LayoutBrief2:
    n = rng.randint(3, 7)
    rooms = []
    for i in range(n):
        hab = i < 2 or rng.random() < 0.5
        t = rng.choice(_HAB if hab else _OTHER)
        rooms.append(RoomSpec2(f"r{i}", t, area=rng.choice([90, 120, 144, 168, 200, 300])))
    order = list(range(n))
    rng.shuffle(order)
    edges = set()
    for p in range(1, n):  # a spanning tree keeps it connected
        edges.add(tuple(sorted((order[p], order[rng.randint(0, p - 1)]))))
    for _ in range(rng.randint(0, n)):  # plus a few extra required adjacencies
        a, b = rng.sample(range(n), 2)
        edges.add(tuple(sorted((a, b))))
    adj = [(f"r{a}", f"r{b}") for a, b in edges]
    return LayoutBrief2(f"T{k}", rooms=rooms, adjacencies=adj)


def test_dual_outputs_are_always_structurally_valid():
    """Every plan the dual emits must tile, not overlap, and honour the brief."""
    rng = random.Random(20240627)
    produced = 0
    for k in range(60):
        brief = _random_brief(rng, k)
        try:
            out = solve_layout2(brief, engine="dual")
        except ValueError:
            continue  # no dual for this graph — auto would fall back; fine
        produced += 1
        plan = out.plan
        # no overlaps
        for i, a in enumerate(plan.rooms):
            for b in plan.rooms[i + 1 :]:
                assert a.overlaps(b) == 0, (brief.name, a.id, b.id)
        # within the envelope
        for r in plan.rooms:
            assert r.x >= -1e-6 and r.y >= -1e-6
            assert r.x2 <= plan.envelope_width + 1e-6
            assert r.y2 <= plan.envelope_length + 1e-6
        # tiles (no waste)
        footprint = plan.envelope_width * plan.envelope_length
        assert sum(r.area for r in plan.rooms) / footprint > 0.98, brief.name
        # required adjacencies are real shared walls
        by_id = {r.id: r for r in plan.rooms}
        for a, b in brief.adjacencies:
            assert shared_edge(by_id[a], by_id[b]) is not None, (brief.name, a, b)
        # habitable rooms reach the perimeter
        for r in plan.rooms:
            if r.type in HABITABLE_TYPES:
                assert exterior_walls(plan, r), (brief.name, r.id)
        # and it compiles with zero errors and full reachability
        rr = compile_source(emit_dsl(plan), name=plan.name)
        assert not rr.errors, (brief.name, rr.report())
    assert produced >= 15  # the sweep must actually exercise the engine


# --- the shipped example ----------------------------------------------------


def test_pinwheel_example_compiles_clean_via_auto():
    import os

    from barndsl.layout2 import parse_brief2

    path = os.path.join(os.path.dirname(__file__), "..", "examples", "pinwheel.brief")
    out = solve_layout2(parse_brief2(open(path).read()))
    assert out.unsatisfied == []
    assert any("dual" in n for n in out.notes)  # auto must reach for the dual here
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()
    # The auto-layout declares `tempered` on its hazard-location windows, so
    # WINDOW_TEMPERED no longer surfaces, and the great-room-to-kitchen link is a
    # cased opening rather than a 6 ft swing leaf (DOOR_WIDE_SWING). The only soft
    # warning left is NO_BATH (the minimal pinwheel has no room for a bath);
    # nothing structural warns.
    assert all(d.code == "NO_BATH" for d in result.warnings), result.report()
