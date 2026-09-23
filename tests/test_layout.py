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
    assert plan.porches == []  # the entry landing rides with the entry
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


# --- the generator satisfies the rules it used to trip -----------------------
#
# These pin *why* the shipped examples compile clean: each one used to fire on
# every auto-generated plan, and the fix was to make the generator do the right
# thing rather than to soften the rule.


def test_public_core_link_is_a_cased_opening_not_a_wide_swing():
    plan = solve_layout(_brief()).plan
    link = next(
        d for d in plan.interior_doors
        if {d.room_a, d.room_b} == {"living", "kitchen"}
    )
    assert link.kind == "cased"  # a 6 ft single swing leaf is unbuildable
    assert not link.leaf
    result = compile_source(emit_dsl(plan))
    assert not any(d.code == "DOOR_WIDE_SWING" for d in result.diagnostics)


def test_the_front_entry_gets_the_landing_r311_3_requires():
    from barndsl.geometry import opening_endpoints
    from barndsl.validation import LANDING_MIN_DEPTH, _porch_lands_door

    plan = solve_layout(_brief()).plan
    (entry,) = plan.exterior_doors
    assert len(plan.porches) == 1
    room = plan.room(entry.room)
    x1, y1, x2, y2 = opening_endpoints(room, entry.wall, entry.offset, entry.width)
    lo, hi = (min(x1, x2), max(x1, x2)) if x1 != x2 else (min(y1, y2), max(y1, y2))
    assert _porch_lands_door(plan, room, entry.wall, lo, hi)
    assert plan.porches[0].length >= LANDING_MIN_DEPTH
    result = compile_source(emit_dsl(plan))
    assert not any(
        d.code in ("DOOR_NO_LANDING", "DOOR_THRESHOLD") for d in result.diagnostics
    )


def test_windows_keep_a_trim_reveal_off_interior_partitions():
    plan = solve_layout(_brief()).plan
    result = compile_source(emit_dsl(plan))
    assert not any(d.code == "WINDOW_PARTITION" for d in result.diagnostics)
    # ... and pulling them off the partitions did not starve the daylight.
    assert not any(d.code == "NAT_LIGHT" for d in result.diagnostics)


def test_the_trim_reveal_is_taken_at_a_partition_but_not_a_building_corner():
    # WINDOW_PARTITION exempts a true building corner, so the placer must too:
    # the reveal comes off the partition end of the wall and nowhere else.
    from barndsl.elements import Direction
    from barndsl.layout import _window_reveal
    from barndsl.validation import WINDOW_WALL_CLEAR

    brief = LayoutBrief(
        name="Corner",
        rooms=[
            RoomSpec("living", "living", 24, 16),
            RoomSpec("bed1", "bedroom", 14, 16),
        ],
        adjacencies=[("living", "bed1")],
        add_openings=False,
    )
    plan = solve_layout(brief).plan
    living, bed1 = plan.room("living"), plan.room("bed1")
    # bed1 | living: the seam between them is a partition, the far end of each
    # south wall run is the envelope corner.
    assert _window_reveal(plan, living, Direction.SOUTH, 24.0, (0.0, 24.0)) == (
        WINDOW_WALL_CLEAR, 24.0,
    )
    assert _window_reveal(plan, bed1, Direction.SOUTH, 14.0, (0.0, 14.0)) == (
        0.0, 14.0 - WINDOW_WALL_CLEAR,
    )


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
    # The auto-layout DECLARES `tempered` on the windows it lands in R308.4
    # hazard locations (a daylight window beside the entry, a bath window near the
    # tub) via the shared predicate, so no WINDOW_TEMPERED warning survives; the
    # public core is a cased opening rather than a 6 ft swing leaf, so no
    # DOOR_WIDE_SWING either — the plan is strictly clean of warnings.
    assert not result.warnings, result.report()
