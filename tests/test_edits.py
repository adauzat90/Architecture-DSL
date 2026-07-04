"""Tests for the surgical DSL edit engine (`barndsl.edits`), Tier 5.

The engine's contract: a viewport gesture becomes the *smallest* change to the
DSL text. Only the target statement's line moves; comments, blank lines, odd
spacing and inline ``# comments`` survive; a true no-op returns the source
byte-identical; unknown/malformed edits are typed errors, never exceptions.
"""

from __future__ import annotations

import os

import pytest

from barndsl.compiler import compile_source
from barndsl.edits import (
    Edit,
    EditError,
    apply_edit,
    edit_from_json,
    iter_openings,
    opening_overlays,
)

# A source with comments, blank lines, ragged spacing, a relative placement and
# an inline comment on a room line — every preservation guarantee in one plan.
SRC = """\
plan "Preserve Me"
envelope 60 x 40
ceiling 12

# --- rooms ---
room great_room: living at 0,0 size 28 x 26   # the hearth
room kitchen: kitchen east-of great_room size 18 x 26
room master_bed: bedroom north-of great_room align far size 16 x 11

door great_room - kitchen width 8
entry great_room south width 3 offset 20
window master_bed north width 5 offset 5
"""


def _line(source: str, n: int) -> str:
    return source.split("\n")[n - 1]


def _only_line_changed(before: str, after: str) -> int | None:
    """Return the single 1-based line index that differs, or fail if not exactly one."""
    a, b = before.split("\n"), after.split("\n")
    assert len(a) == len(b), "line count changed"
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    assert len(diff) == 1, f"expected exactly one changed line, got {diff}"
    return diff[0] + 1


def _recompiles(source: str):
    """Every successful edit must leave source the compiler can still build into a
    plan. Returns the compiled plan so a test can also assert model facts on it."""
    res = compile_source(source)
    assert res.plan is not None, f"edit produced unbuildable source:\n{source}"
    return res.plan


# --- move_room ---------------------------------------------------------------


def test_move_room_absolute_rewrites_only_placement_and_keeps_comment():
    r = apply_edit(SRC, Edit("move_room", room="great_room", x=2, y=3))
    assert r.ok and r.changed
    ln = _only_line_changed(SRC, r.source)
    assert ln == 6
    assert _line(r.source, 6) == "room great_room: living at 2,3 size 28 x 26   # the hearth"


def test_move_room_noop_returns_byte_identical_source():
    # great_room is at 0,0 already → idempotent no-op, source unchanged exactly.
    r = apply_edit(SRC, Edit("move_room", room="great_room", x=0, y=0))
    assert r.ok and not r.changed
    assert r.source == SRC


def test_move_room_relative_converts_to_absolute_on_real_move():
    kitchen = compile_source(SRC).plan.room("kitchen")
    assert kitchen.x == 28.0 and kitchen.y == 0.0  # east_of great_room
    r = apply_edit(SRC, Edit("move_room", room="kitchen", x=30, y=1))
    assert r.ok and r.changed
    assert _line(r.source, 7) == "room kitchen: kitchen at 30,1 size 18 x 26"


def test_move_room_relative_noop_stays_relative():
    # Moving a relative room to exactly where it already resolves must NOT convert
    # it to absolute — a true no-op returns the source unchanged.
    kitchen = compile_source(SRC).plan.room("kitchen")
    r = apply_edit(SRC, Edit("move_room", room="kitchen", x=kitchen.x, y=kitchen.y))
    assert r.ok and not r.changed
    assert r.source == SRC
    assert "east-of great_room" in _line(r.source, 7)


def test_move_room_rounds_to_two_decimals():
    r = apply_edit(SRC, Edit("move_room", room="great_room", x=2.123456, y=3.005))
    assert _line(r.source, 6).split("size")[0].strip().endswith("at 2.12,3")


# --- resize_room -------------------------------------------------------------


def test_resize_room_rewrites_only_size_and_keeps_comment():
    r = apply_edit(SRC, Edit("resize_room", room="great_room", w=30, l=24))
    assert r.ok and r.changed
    ln = _only_line_changed(SRC, r.source)
    assert ln == 6
    assert _line(r.source, 6) == "room great_room: living at 0,0 size 30 x 24   # the hearth"


def test_resize_room_noop_is_byte_identical():
    r = apply_edit(SRC, Edit("resize_room", room="great_room", w=28, l=26))
    assert r.ok and not r.changed and r.source == SRC


def test_resize_room_rejects_nonpositive():
    r = apply_edit(SRC, Edit("resize_room", room="great_room", w=0, l=26))
    assert not r.ok and r.error.kind == "malformed"


# --- move_opening ------------------------------------------------------------


def test_move_opening_rewrites_existing_offset():
    r = apply_edit(SRC, Edit("move_opening", opening="exterior", key="great_room~south~0", offset=10))
    assert r.ok and r.changed
    _only_line_changed(SRC, r.source)
    assert _line(r.source, 11) == "entry great_room south width 3 offset 10"


def test_move_opening_inserts_offset_when_absent():
    # The interior door has no `offset` token — the engine inserts one.
    r = apply_edit(SRC, Edit("move_opening", opening="interior", key="great_room~kitchen~0", offset=4))
    assert r.ok and r.changed
    assert _line(r.source, 10) == "door great_room - kitchen width 8 offset 4"


def test_move_opening_insert_preserves_trailing_comment():
    src = SRC.replace(
        "door great_room - kitchen width 8",
        "door great_room - kitchen width 8    # pass-through",
    )
    r = apply_edit(src, Edit("move_opening", opening="interior", key="great_room~kitchen~0", offset=4))
    assert r.ok
    assert _line(r.source, 10) == "door great_room - kitchen width 8 offset 4    # pass-through"


def test_move_window_offset():
    r = apply_edit(SRC, Edit("move_opening", opening="window", key="master_bed~north~0", offset=8))
    assert r.ok and r.changed
    assert _line(r.source, 12) == "window master_bed north width 5 offset 8"


def test_move_opening_noop_on_existing_offset_is_identical():
    r = apply_edit(SRC, Edit("move_opening", opening="window", key="master_bed~north~0", offset=5))
    assert r.ok and not r.changed and r.source == SRC


# --- typed errors ------------------------------------------------------------


def test_unknown_room_is_typed_error_not_exception():
    r = apply_edit(SRC, Edit("move_room", room="nope", x=1, y=1))
    assert not r.ok and r.error.kind == "unknown_room"
    assert r.source == SRC


def test_unknown_opening_is_typed_error():
    r = apply_edit(SRC, Edit("move_opening", opening="window", key="nope~north~0", offset=1))
    assert not r.ok and r.error.kind == "unknown_opening"


def test_uncompilable_source_is_not_editable():
    r = apply_edit("this is not dsl at all", Edit("move_room", room="x", x=1, y=1))
    assert not r.ok and r.error.kind == "not_editable"


def test_malformed_edit_kinds():
    for edit in (
        Edit("move_room", room="", x=1, y=1),
        Edit("move_room", room="a", x=float("inf"), y=1),
        Edit("bogus_kind"),
        Edit("move_opening", opening="wall", key="a", offset=1),
        Edit("move_opening", opening="window", key="a", offset=-1),
    ):
        r = apply_edit(SRC, edit)
        assert not r.ok and r.error.kind == "malformed", edit


def test_edit_from_json_rejects_bad_shapes():
    assert isinstance(edit_from_json("nope"), EditError)
    assert isinstance(edit_from_json({"kind": "nope"}), EditError)
    good = edit_from_json({"kind": "move_room", "room": "a", "x": 1, "y": 2})
    assert isinstance(good, Edit) and good.room == "a" and good.x == 1.0
    # booleans must not pass as numbers
    bad = edit_from_json({"kind": "move_room", "room": "a", "x": True, "y": 2})
    assert bad.x is None


# --- opening key scheme ------------------------------------------------------


def test_opening_keys_are_unique_and_indexed():
    plan = compile_source(SRC).plan
    keys = [key for _, key, _ in iter_openings(plan)]
    assert len(keys) == len(set(keys))
    assert "great_room~kitchen~0" in keys
    assert "master_bed~north~0" in keys


def test_same_pair_openings_get_distinct_indices():
    src = """\
plan "Twins"
envelope 40 x 20
ceiling 9
room a: living at 0,0 size 20 x 20
room b: office east-of a size 20 x 20
door a - b width 3 offset 2
door a - b width 3 offset 12
"""
    plan = compile_source(src).plan
    keys = [key for k, key, _ in iter_openings(plan) if k == "interior"]
    assert keys == ["a~b~0", "a~b~1"]
    r = apply_edit(src, Edit("move_opening", opening="interior", key="a~b~1", offset=14))
    assert r.ok and _line(r.source, 7) == "door a - b width 3 offset 14"
    assert _line(r.source, 6) == "door a - b width 3 offset 2"  # sibling untouched


def test_opening_overlays_carry_geometry_and_line():
    plan = compile_source(SRC).plan
    ovs = {o["key"]: o for o in opening_overlays(plan)}
    win = ovs["master_bed~north~0"]
    assert win["kind"] == "window" and win["line"] == 12
    assert win["offset"] == 5.0 and win["max"] >= 0
    # geometry endpoints are finite plan coordinates
    for k in ("ax", "ay", "bx", "by", "width"):
        assert isinstance(win[k], (int, float))


# --- multi-level -------------------------------------------------------------

MULTI = """\
plan "Two Story"
envelope 40 x 30
ceiling 9
room living: living at 0,0 size 40 x 30
room loft: loft at 0,0 size 20 x 30 level 1
stair st at 0,0 size 4 x 12 from 0 to 1
"""


def test_multi_level_move_targets_the_named_room():
    r = apply_edit(MULTI, Edit("move_room", room="loft", x=2, y=0))
    assert r.ok and r.changed
    assert _line(r.source, 5) == "room loft: loft at 2,0 size 20 x 30 level 1"


# --- gallery sweep -----------------------------------------------------------

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _all_examples() -> list[tuple[str, str]]:
    out = []
    for base in (EXAMPLES, os.path.join(EXAMPLES, "gallery")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if name.endswith(".barn"):
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    out.append((name, fh.read()))
    return out


@pytest.mark.parametrize("name,source", _all_examples())
def test_gallery_noop_move_resize_keeps_an_equivalent_plan(name, source):
    """Apply a no-op-shaped move+resize to the first room of every example and
    assert the result still compiles to an equivalent plan (same rooms/areas)."""
    before = compile_source(source)
    if not before.plan or not before.plan.rooms:
        pytest.skip(f"{name}: no rooms")
    room = before.plan.rooms[0]
    src = source
    for edit in (
        Edit("move_room", room=room.id, x=room.x, y=room.y),
        Edit("resize_room", room=room.id, w=room.width, l=room.length),
    ):
        res = apply_edit(src, edit)
        assert res.ok, f"{name}: {res.error}"
        src = res.source
    after = compile_source(src)
    assert after.plan is not None
    assert len(after.plan.rooms) == len(before.plan.rooms), name
    a0 = sorted(r.area for r in before.plan.rooms)
    a1 = sorted(r.area for r in after.plan.rooms)
    assert a0 == pytest.approx(a1), name


# =============================================================================
# Tier 5 graphical design panel — the form-control edit vocabulary
# =============================================================================

# A plan with anchors, fixtures (authored + seeded), and every opening kind, so
# the panel edits have real references to rewrite / cascade / recompile.
PANEL = """\
plan "Panel Plan"
envelope 60 x 40
ceiling 10

room great_room: living at 0,0 size 28 x 26   # hearth
room kitchen: kitchen east-of great_room size 18 x 26
room bath: bathroom north-of kitchen size 10 x 8
room master_bed: bedroom north-of great_room align far size 16 x 11

door great_room - kitchen width 8
open kitchen - bath width 5
entry great_room south width 3 offset 20
window master_bed north width 5 offset 5

fixture bed_queen in master_bed at 2,2   # the bed
"""


# --- set_room_type -----------------------------------------------------------


def test_set_room_type_rewrites_only_the_type_token():
    r = apply_edit(PANEL, Edit("set_room_type", room="great_room", rtype="dining"))
    assert r.ok and r.changed
    ln = _only_line_changed(PANEL, r.source)
    assert ln == 5
    assert _line(r.source, 5) == "room great_room: dining at 0,0 size 28 x 26   # hearth"
    assert _recompiles(r.source).room("great_room").type.value == "dining"


def test_set_room_type_noop_is_byte_identical():
    r = apply_edit(PANEL, Edit("set_room_type", room="great_room", rtype="living"))
    assert r.ok and not r.changed and r.source == PANEL


def test_set_room_type_rejects_unknown_type():
    r = apply_edit(PANEL, Edit("set_room_type", room="great_room", rtype="ballroom"))
    assert not r.ok and r.error.kind == "bad_value"


def test_set_room_type_unknown_room():
    r = apply_edit(PANEL, Edit("set_room_type", room="nope", rtype="office"))
    assert not r.ok and r.error.kind == "unknown_room"


# --- rename_room -------------------------------------------------------------


def test_rename_room_rewrites_definition_and_all_references():
    r = apply_edit(PANEL, Edit("rename_room", room="great_room", to="hearth"))
    assert r.ok and r.changed
    src = r.source
    # definition (comment preserved), interior door, exterior entry, anchor refs.
    assert _line(src, 5) == "room hearth: living at 0,0 size 28 x 26   # hearth"
    assert _line(src, 6) == "room kitchen: kitchen east-of hearth size 18 x 26"
    assert _line(src, 8) == "room master_bed: bedroom north-of hearth align far size 16 x 11"
    assert _line(src, 10) == "door hearth - kitchen width 8"
    assert _line(src, 12) == "entry hearth south width 3 offset 20"
    assert "great_room" not in src
    plan = _recompiles(src)
    assert plan.room("hearth") is not None and plan.room("great_room") is None


def test_rename_room_updates_open_window_and_fixture_references():
    r = apply_edit(PANEL, Edit("rename_room", room="master_bed", to="primary"))
    assert r.ok and r.changed
    src = r.source
    assert _line(src, 8).startswith("room primary: bedroom north-of great_room")
    assert _line(src, 13) == "window primary north width 5 offset 5"
    assert _line(src, 15) == "fixture bed_queen in primary at 2,2   # the bed"
    _recompiles(src)


def test_rename_room_updates_cased_open_both_sides():
    r = apply_edit(PANEL, Edit("rename_room", room="kitchen", to="galley"))
    assert r.ok
    assert _line(r.source, 11) == "open galley - bath width 5"
    assert _line(r.source, 10) == "door great_room - galley width 8"
    _recompiles(r.source)


def test_rename_room_prefix_collision_trap():
    # `bed` must not match the `bedroom` type token nor a room named `bed2`.
    src = """\
plan "Collide"
envelope 40 x 30
ceiling 9
room bed: bedroom at 0,0 size 12 x 12
room bed2: bedroom east-of bed size 12 x 12
door bed - bed2 width 3
fixture bed_queen in bed at 1,1
"""
    r = apply_edit(src, Edit("rename_room", room="bed", to="nook"))
    assert r.ok and r.changed
    out = r.source
    assert _line(out, 4) == "room nook: bedroom at 0,0 size 12 x 12"      # type token intact
    assert _line(out, 5) == "room bed2: bedroom east-of nook size 12 x 12"  # bed2 untouched
    assert _line(out, 6) == "door nook - bed2 width 3"
    assert _line(out, 7) == "fixture bed_queen in nook at 1,1"           # bed_queen intact
    _recompiles(out)


def test_rename_room_noop_when_target_equals_source():
    r = apply_edit(PANEL, Edit("rename_room", room="kitchen", to="kitchen"))
    assert r.ok and not r.changed and r.source == PANEL


def test_rename_room_rejects_taken_id():
    r = apply_edit(PANEL, Edit("rename_room", room="kitchen", to="bath"))
    assert not r.ok and r.error.kind == "bad_value"


def test_rename_room_rejects_invalid_identifier():
    r = apply_edit(PANEL, Edit("rename_room", room="kitchen", to="2kitchen"))
    assert not r.ok and r.error.kind == "bad_value"


def test_rename_room_leaves_quoted_plan_name_alone():
    src = 'plan "kitchen"\nenvelope 20 x 20\nroom kitchen: kitchen at 0,0 size 10 x 10\n'
    r = apply_edit(src, Edit("rename_room", room="kitchen", to="cook"))
    assert r.ok
    assert _line(r.source, 1) == 'plan "kitchen"'  # the quoted string is not a ref
    assert _line(r.source, 3) == "room cook: kitchen at 0,0 size 10 x 10"


# --- add_room ----------------------------------------------------------------


def test_add_room_absolute_appended_after_last_room():
    r = apply_edit(PANEL, Edit("add_room", room="office", rtype="office", w=10, l=10,
                               x=0, y=26))
    assert r.ok and r.changed
    assert _line(r.source, r.line) == "room office: office at 0,26 size 10 x 10"
    assert r.line == 9  # right after master_bed (line 8), before the blank
    plan = _recompiles(r.source)
    assert plan.room("office") is not None


def test_add_room_relative_anchor():
    r = apply_edit(PANEL, Edit("add_room", room="pantry", rtype="pantry", w=6, l=8,
                               anchor="east-of", of="kitchen"))
    assert r.ok
    assert _line(r.source, r.line) == "room pantry: pantry east-of kitchen size 6 x 8"
    assert _recompiles(r.source).room("pantry") is not None


def test_add_room_with_level_lands_after_that_levels_rooms():
    r = apply_edit(MULTI, Edit("add_room", room="studio", rtype="office", w=8, l=8,
                               x=20, y=0, level=1))
    assert r.ok
    assert _line(r.source, r.line) == "room studio: office at 20,0 size 8 x 8 level 1"
    assert r.line == 6  # after the level-1 loft (line 5), not after the ground living
    assert _recompiles(r.source).room("studio").level == 1


def test_add_room_rejects_taken_id():
    r = apply_edit(PANEL, Edit("add_room", room="kitchen", rtype="office", w=8, l=8,
                               x=0, y=0))
    assert not r.ok and r.error.kind == "bad_value"


def test_add_room_rejects_missing_placement():
    r = apply_edit(PANEL, Edit("add_room", room="den", rtype="office", w=8, l=8))
    assert not r.ok and r.error.kind == "malformed"


def test_add_room_unknown_anchor_target():
    r = apply_edit(PANEL, Edit("add_room", room="den", rtype="office", w=8, l=8,
                               anchor="east-of", of="ghost"))
    assert not r.ok and r.error.kind == "unknown_room"


def test_add_room_rejects_nonpositive_size():
    r = apply_edit(PANEL, Edit("add_room", room="den", rtype="office", w=0, l=8,
                               x=0, y=0))
    assert not r.ok and r.error.kind == "bad_value"


# --- delete_room -------------------------------------------------------------


def test_delete_room_cascades_and_pins_dependents_absolute():
    r = apply_edit(PANEL, Edit("delete_room", room="great_room"))
    assert r.ok and r.changed
    src = r.source
    assert "great_room" not in src
    # its interior door and exterior entry are gone…
    assert "door great_room" not in src and "entry great_room" not in src
    # …and the rooms anchored to it are converted to their resolved absolute spot.
    assert _line(src, 5) == "room kitchen: kitchen at 28,0 size 18 x 26"
    assert _line(src, 7) == "room master_bed: bedroom at 12,26 size 16 x 11"
    plan = _recompiles(src)
    assert plan.room("kitchen").x == 28.0 and plan.room("master_bed").y == 26.0


def test_delete_room_reports_count_and_removes_fixtures():
    r = apply_edit(PANEL, Edit("delete_room", room="master_bed"))
    assert r.ok
    # master_bed line + its window + its authored fixture = 3 lines removed.
    assert "master_bed" not in r.source
    assert "window master_bed" not in r.source
    assert "fixture bed_queen" not in r.source
    assert "2 dependent" in r.summary
    _recompiles(r.source)


def test_delete_room_no_doubled_blank_lines():
    src = """\
plan "Gap"
envelope 40 x 20
ceiling 9

room a: living at 0,0 size 20 x 20
room b: office east-of a size 20 x 20

door a - b width 3

entry a south width 3
"""
    r = apply_edit(src, Edit("delete_room", room="b"))
    assert r.ok
    assert "\n\n\n" not in r.source  # the removed `door a - b` left no doubled gap
    _recompiles(r.source)


def test_delete_room_unknown():
    r = apply_edit(PANEL, Edit("delete_room", room="nope"))
    assert not r.ok and r.error.kind == "unknown_room"


# --- add_opening -------------------------------------------------------------


def test_add_opening_door_appended_after_last_opening():
    r = apply_edit(PANEL, Edit("add_opening", opening="door", a="great_room",
                               b="master_bed", width=3))
    assert r.ok and r.changed
    assert _line(r.source, r.line) == "door great_room - master_bed width 3"
    assert _recompiles(r.source) is not None


def test_add_opening_window_with_offset():
    r = apply_edit(PANEL, Edit("add_opening", opening="window", room="kitchen",
                               side="east", width=4, offset=3))
    assert r.ok
    assert _line(r.source, r.line) == "window kitchen east width 4 offset 3"
    _recompiles(r.source)


def test_add_opening_entry():
    r = apply_edit(PANEL, Edit("add_opening", opening="entry", room="great_room",
                               side="west", width=3))
    assert r.ok
    assert _line(r.source, r.line) == "entry great_room west width 3"
    _recompiles(r.source)


def test_add_opening_unknown_room():
    r = apply_edit(PANEL, Edit("add_opening", opening="door", a="great_room",
                               b="ghost", width=3))
    assert not r.ok and r.error.kind == "unknown_room"


def test_add_opening_rejects_same_room_door():
    r = apply_edit(PANEL, Edit("add_opening", opening="door", a="kitchen",
                               b="kitchen", width=3))
    assert not r.ok and r.error.kind == "bad_value"


def test_add_opening_window_rejects_bad_side():
    r = apply_edit(PANEL, Edit("add_opening", opening="window", room="kitchen",
                               side="up", width=4))
    assert not r.ok and r.error.kind == "bad_value"


# --- delete_opening ----------------------------------------------------------


def test_delete_opening_removes_the_line():
    r = apply_edit(PANEL, Edit("delete_opening", opening="window",
                               key="master_bed~north~0"))
    assert r.ok and r.changed
    assert "window master_bed" not in r.source
    _recompiles(r.source)


def test_delete_opening_interior_door():
    r = apply_edit(PANEL, Edit("delete_opening", opening="interior",
                               key="great_room~kitchen~0"))
    assert r.ok
    assert "door great_room - kitchen" not in r.source
    _recompiles(r.source)


def test_delete_opening_unknown():
    r = apply_edit(PANEL, Edit("delete_opening", opening="window", key="nope~north~0"))
    assert not r.ok and r.error.kind == "unknown_opening"


# --- set_opening -------------------------------------------------------------


def test_set_opening_width_rewrites_in_place():
    r = apply_edit(PANEL, Edit("set_opening", opening="exterior",
                               key="great_room~south~0", width=4))
    assert r.ok and r.changed
    assert _line(r.source, 12) == "entry great_room south width 4 offset 20"
    _recompiles(r.source)


def test_set_opening_adds_offset_clause_when_absent():
    # The interior door has no offset token — set_opening inserts one.
    r = apply_edit(PANEL, Edit("set_opening", opening="interior",
                               key="great_room~kitchen~0", offset=2))
    assert r.ok and r.changed
    assert _line(r.source, 10) == "door great_room - kitchen width 8 offset 2"
    _recompiles(r.source)


def test_set_opening_adds_and_then_removes_door_swing_clause():
    added = apply_edit(PANEL, Edit("set_opening", opening="interior",
                                   key="great_room~kitchen~0", into="kitchen", hinge="far"))
    assert added.ok
    assert _line(added.source, 10) == "door great_room - kitchen width 8 into kitchen hinge far"
    _recompiles(added.source)
    removed = apply_edit(added.source, Edit("set_opening", opening="interior",
                                            key="great_room~kitchen~0", into=None, into_set=True))
    assert removed.ok
    assert _line(removed.source, 10) == "door great_room - kitchen width 8"
    _recompiles(removed.source)


def test_set_opening_window_sill():
    r = apply_edit(PANEL, Edit("set_opening", opening="window",
                               key="master_bed~north~0", sill=2))
    assert r.ok
    assert _line(r.source, 13) == "window master_bed north width 5 offset 5 sill 2"
    _recompiles(r.source)


def test_set_opening_noop_is_byte_identical():
    r = apply_edit(PANEL, Edit("set_opening", opening="window",
                               key="master_bed~north~0", width=5, offset=5))
    assert r.ok and not r.changed and r.source == PANEL


def test_set_opening_sill_on_non_window_rejected():
    r = apply_edit(PANEL, Edit("set_opening", opening="interior",
                               key="great_room~kitchen~0", sill=2))
    assert not r.ok and r.error.kind == "bad_value"


def test_set_opening_into_on_non_interior_rejected():
    r = apply_edit(PANEL, Edit("set_opening", opening="window",
                               key="master_bed~north~0", into="master_bed", into_set=True))
    assert not r.ok and r.error.kind == "bad_value"


def test_set_opening_needs_a_property():
    r = apply_edit(PANEL, Edit("set_opening", opening="window", key="master_bed~north~0"))
    assert not r.ok and r.error.kind == "malformed"


# --- delete_fixture ----------------------------------------------------------


def test_delete_fixture_removes_authored_line():
    r = apply_edit(PANEL, Edit("delete_fixture", key="master_bed~bed_queen~0"))
    assert r.ok and r.changed
    assert "fixture bed_queen" not in r.source
    _recompiles(r.source)


def test_delete_fixture_seed_is_not_editable():
    # bath auto-seeds a toilet — it has no source line, so it can't be deleted.
    r = apply_edit(PANEL, Edit("delete_fixture", key="bath~toilet~0"))
    assert not r.ok and r.error.kind == "not_editable"
    assert "seed" in r.error.message


def test_delete_fixture_unknown():
    r = apply_edit(PANEL, Edit("delete_fixture", key="master_bed~ghost~0"))
    assert not r.ok and r.error.kind == "unknown_opening"


# --- set_fixture -------------------------------------------------------------


def test_set_fixture_authored_adds_clauses():
    r = apply_edit(PANEL, Edit("set_fixture", key="master_bed~bed_queen~0",
                               rotate=90, wall="N"))
    assert r.ok and r.changed
    assert _line(r.source, 15) == "fixture bed_queen in master_bed at 2,2 wall N rotate 90   # the bed"
    _recompiles(r.source)


def test_set_fixture_authored_noop_when_clause_present_and_equal():
    src = PANEL.replace("fixture bed_queen in master_bed at 2,2   # the bed",
                        "fixture bed_queen in master_bed at 2,2 rotate 90")
    r = apply_edit(src, Edit("set_fixture", key="master_bed~bed_queen~0", rotate=90))
    assert r.ok and not r.changed and r.source == src


def test_set_fixture_materialises_a_seed():
    # A seeded toilet has no line; set_fixture materialises a `fixture` line for it.
    r = apply_edit(PANEL, Edit("set_fixture", key="bath~toilet~0", rotate=180))
    assert r.ok and r.changed
    added = _line(r.source, r.line)
    assert added.startswith("fixture toilet in bath at ")
    assert "rotate 180" in added
    plan = _recompiles(r.source)
    # the materialised toilet is now an authored fixture with a source line.
    from barndsl.fixtures import resolve_room_fixtures
    toilets = [f for f in resolve_room_fixtures(plan, plan.room("bath"))
               if f.kind == "toilet"]
    assert toilets and not toilets[0].seed


def test_set_fixture_unknown():
    r = apply_edit(PANEL, Edit("set_fixture", key="master_bed~ghost~0", rotate=90))
    assert not r.ok and r.error.kind == "unknown_opening"


def test_set_fixture_rejects_bad_wall():
    r = apply_edit(PANEL, Edit("set_fixture", key="master_bed~bed_queen~0", wall="up"))
    assert not r.ok and r.error.kind == "bad_value"


# --- set_plan ----------------------------------------------------------------


def test_set_plan_rewrites_name_envelope_and_ceiling():
    r = apply_edit(PANEL, Edit("set_plan", pname="Cedar Ridge", env_w=70, env_l=44,
                               ceiling=12))
    assert r.ok and r.changed
    assert _line(r.source, 1) == 'plan "Cedar Ridge"'
    assert _line(r.source, 2) == "envelope 70 x 44"
    assert _line(r.source, 3) == "ceiling 12"
    plan = _recompiles(r.source)
    assert plan.name == "Cedar Ridge" and plan.envelope_width == 70.0


def test_set_plan_only_ceiling_touches_one_line():
    r = apply_edit(PANEL, Edit("set_plan", ceiling=11))
    assert r.ok and r.changed
    assert _only_line_changed(PANEL, r.source) == 3
    assert _line(r.source, 3) == "ceiling 11"


def test_set_plan_adds_ceiling_line_when_absent():
    src = 'plan "No Ceiling"\nenvelope 40 x 20\nroom a: living at 0,0 size 10 x 10\n'
    r = apply_edit(src, Edit("set_plan", ceiling=9))
    assert r.ok and r.changed
    assert _line(r.source, 3) == "ceiling 9"  # inserted right after envelope
    assert _recompiles(r.source).ceiling_height == 9.0


def test_set_plan_noop_is_byte_identical():
    r = apply_edit(PANEL, Edit("set_plan", pname="Panel Plan", env_w=60, env_l=40,
                               ceiling=10))
    assert r.ok and not r.changed and r.source == PANEL


def test_set_plan_rejects_empty_name():
    r = apply_edit(PANEL, Edit("set_plan", pname="   "))
    assert not r.ok and r.error.kind == "bad_value"


def test_set_plan_rejects_nonpositive_envelope():
    r = apply_edit(PANEL, Edit("set_plan", env_w=0, env_l=40))
    assert not r.ok and r.error.kind == "bad_value"


def test_set_plan_needs_a_field():
    r = apply_edit(PANEL, Edit("set_plan"))
    assert not r.ok and r.error.kind == "malformed"


# --- edit_from_json for the new kinds ----------------------------------------


def test_edit_from_json_panel_kinds():
    e = edit_from_json({"kind": "set_room_type", "room": "a", "type": "office"})
    assert isinstance(e, Edit) and e.rtype == "office"
    e = edit_from_json({"kind": "add_room", "id": "a", "type": "office",
                        "w": 10, "l": 8, "at": [1, 2]})
    assert isinstance(e, Edit) and e.x == 1.0 and e.y == 2.0
    e = edit_from_json({"kind": "add_opening", "opening": "door", "a": "x", "b": "y",
                        "width": 3})
    assert isinstance(e, Edit) and e.width == 3.0
    # `into: null` is distinguished from an absent `into`.
    e = edit_from_json({"kind": "set_opening", "opening": "interior", "key": "k",
                        "into": None})
    assert isinstance(e, Edit) and e.into is None and e.into_set is True
    e2 = edit_from_json({"kind": "set_opening", "opening": "interior", "key": "k",
                         "width": 3})
    assert isinstance(e2, Edit) and e2.into_set is False
    e = edit_from_json({"kind": "set_plan", "envelope": [50, 30]})
    assert isinstance(e, Edit) and e.env_w == 50.0 and e.env_l == 30.0


def test_edit_from_json_unknown_panel_field_ignored():
    e = edit_from_json({"kind": "set_room_type", "room": "a", "type": "office",
                        "bogus": 1})
    assert isinstance(e, Edit) and e.rtype == "office"


# --- every successful panel edit recompiles (a sweep over the gallery) --------


@pytest.mark.parametrize("name,source", _all_examples())
def test_gallery_first_room_retype_recompiles(name, source):
    before = compile_source(source)
    if not before.plan or not before.plan.rooms:
        pytest.skip(f"{name}: no rooms")
    room = before.plan.rooms[0]
    r = apply_edit(source, Edit("set_room_type", room=room.id, rtype=room.type.value))
    assert r.ok, f"{name}: {r.error}"
    # setting a room to its own type is a no-op; the source must be untouched.
    assert not r.changed and r.source == source, name
