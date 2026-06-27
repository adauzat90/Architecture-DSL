"""Tests for auto-layout: solving room placement from an adjacency brief."""

from __future__ import annotations

import os

import pytest

from barndsl import compile_source, emit_dsl
from barndsl.layout import (
    LayoutBrief,
    RoomSpec,
    parse_brief,
    solve_layout,
)


def _brief() -> LayoutBrief:
    return LayoutBrief(
        name="Test",
        rooms=[
            RoomSpec("living", "living", 22, 20),
            RoomSpec("kitchen", "kitchen", 18, 20),
            RoomSpec("hall", "hallway", 40, 4),
            RoomSpec("bed1", "bedroom", 16, 14),
            RoomSpec("bed2", "bedroom", 16, 14),
            RoomSpec("bath", "bathroom", 8, 14),
        ],
        adjacencies=[
            ("living", "kitchen"),
            ("living", "hall"),
            ("hall", "bed1"),
            ("hall", "bed2"),
            ("hall", "bath"),
        ],
    )


def test_solves_every_adjacency_with_a_door():
    out = solve_layout(_brief())
    assert out.unsatisfied == []
    assert len(out.satisfied) == 5
    # Every satisfied adjacency became an interior door.
    pairs = {frozenset((d.room_a, d.room_b)) for d in out.plan.interior_doors}
    for a, b in out.satisfied:
        assert frozenset((a, b)) in pairs


def test_no_rooms_overlap():
    rooms = solve_layout(_brief()).plan.rooms
    for i, a in enumerate(rooms):
        for b in rooms[i + 1 :]:
            assert a.overlaps(b) == 0, f"{a.id} overlaps {b.id}"


def test_rooms_stay_within_the_auto_envelope():
    plan = solve_layout(_brief()).plan
    for r in plan.rooms:
        assert 0 <= r.x and 0 <= r.y
        assert r.x2 <= plan.envelope_width + 1e-6
        assert r.y2 <= plan.envelope_length + 1e-6


def test_auto_envelope_hugs_the_bounding_box():
    plan = solve_layout(_brief()).plan
    assert plan.envelope_width == max(r.x2 for r in plan.rooms)
    assert plan.envelope_length == max(r.y2 for r in plan.rooms)


def test_layout_is_deterministic():
    a = emit_dsl(solve_layout(_brief()).plan)
    b = emit_dsl(solve_layout(_brief()).plan)
    assert a == b


def test_laid_out_plan_compiles_clean():
    out = solve_layout(_brief())
    result = compile_source(emit_dsl(out.plan))
    assert result.ok, result.report()
    # Doors connect everything: no room is unreachable.
    assert not any(d.code == "NO_ACCESS" for d in result.diagnostics)


def test_add_openings_false_leaves_a_bare_shell():
    brief = _brief()
    brief.add_openings = False
    plan = solve_layout(brief).plan
    assert plan.windows == []
    assert plan.exterior_doors == []
    # ... but the geometry and doors are still there.
    assert plan.interior_doors


def test_bedrooms_get_an_egress_window():
    plan = solve_layout(_brief()).plan
    for bed in ("bed1", "bed2"):
        assert plan.windows_for(bed), f"{bed} has no window"


def test_fixed_envelope_is_respected_and_overflow_noted():
    brief = _brief()
    brief.envelope = (20, 20)  # far too small for six rooms
    out = solve_layout(brief)
    assert out.plan.envelope_width == 20 and out.plan.envelope_length == 20
    assert any("exceeds" in n for n in out.notes)


def test_mutual_triangle_adjacency_via_pocket():
    # Three rooms each adjacent to the other two: the third must pocket into the
    # corner where the first two meet, sharing a wall with each.
    brief = LayoutBrief(
        name="Triangle",
        rooms=[
            RoomSpec("living", "living", 24, 16),
            RoomSpec("kitchen", "kitchen", 16, 24),
            RoomSpec("dining", "dining", 16, 16),
        ],
        adjacencies=[("living", "kitchen"), ("kitchen", "dining"), ("living", "dining")],
        add_openings=False,
    )
    out = solve_layout(brief)
    assert out.unsatisfied == []  # all three pairs share a wall
    assert len(out.plan.interior_doors) == 3


# --- brief parsing ----------------------------------------------------------


def test_parse_brief_round_trips_the_program():
    text = (
        'plan "Willow"\n'
        "envelope 60 x 40\n"
        "ceiling 10\n"
        "note \"hi\"\n"
        "room living: living 24 x 30\n"
        "room loft: loft 24 x 10 level 1\n"
        "adjacent living loft\n"
        "entry living\n"
    )
    brief = parse_brief(text)
    assert brief.name == "Willow"
    assert brief.envelope == (60.0, 40.0)
    assert brief.ceiling == 10
    assert brief.entry_room == "living"
    assert [r.id for r in brief.rooms] == ["living", "loft"]
    assert brief.rooms[1].level == 1
    assert ("living", "loft") in brief.adjacencies


def test_adjacent_is_a_hub_not_a_chain():
    brief = parse_brief(
        "room hall: hallway 30 x 4\n"
        "room a: bedroom 12 x 12\n"
        "room b: bedroom 12 x 12\n"
        "room c: bedroom 12 x 12\n"
        "adjacent hall a b c\n"
    )
    assert ("hall", "a") in brief.adjacencies
    assert ("hall", "b") in brief.adjacencies
    assert ("hall", "c") in brief.adjacencies
    assert ("a", "b") not in brief.adjacencies  # not a chain


def test_no_openings_directive_parsed():
    brief = parse_brief("room a: living 20 x 20\nno-openings\n")
    assert brief.add_openings is False


def test_parse_brief_rejects_garbage():
    with pytest.raises(ValueError):
        parse_brief("frobnicate the gizmo\n")
    with pytest.raises(ValueError):
        parse_brief("room oops 20 x 20\n")  # missing colon


# --- input validation -------------------------------------------------------


def test_bad_room_type_raises_eagerly():
    with pytest.raises(ValueError):
        RoomSpec("x", "lounge", 10, 10)


def test_duplicate_room_id_rejected():
    brief = LayoutBrief(
        name="D",
        rooms=[RoomSpec("a", "living", 10, 10), RoomSpec("a", "bedroom", 10, 10)],
    )
    with pytest.raises(ValueError):
        solve_layout(brief)


def test_adjacency_to_unknown_room_rejected():
    brief = LayoutBrief(
        name="U",
        rooms=[RoomSpec("a", "living", 10, 10)],
        adjacencies=[("a", "ghost")],
    )
    with pytest.raises(ValueError):
        solve_layout(brief)


def test_empty_brief_rejected():
    with pytest.raises(ValueError):
        solve_layout(LayoutBrief(name="E", rooms=[]))


# --- the shipped example ----------------------------------------------------


def test_birch_run_example_compiles_clean():
    path = os.path.join(
        os.path.dirname(__file__), "..", "examples", "birch_run.brief"
    )
    out = solve_layout(parse_brief(open(path).read()))
    result = compile_source(emit_dsl(out.plan), name=out.plan.name)
    assert not result.errors, result.report()
    assert not result.warnings, result.report()
