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
