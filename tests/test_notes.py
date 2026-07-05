"""Positioned annotations — the ``note "text" at <x>,<y> [level <n>]`` feature.

Covers the grammar (parse, level default, un-positioned unchanged), the gentle
outside-footprint diagnostic, the plan-SVG leader render (correct level block),
and the four surgical note edit kinds (add/move/set/delete_note) round-tripping.
"""

from __future__ import annotations

from barndsl import compile_source, emit_dsl, render_svg
from barndsl.edits import Edit, apply_edit, edit_from_json
from barndsl.render import NOTE_COLOR

_BASE = """\
plan "Notes"
envelope 40 x 30
ceiling 10
room living: living at 0,0 size 20 x 20
entry living south width 3
window living north width 4
"""


def _compile(extra_notes: str):
    lines = _BASE.splitlines()
    lines[3:3] = extra_notes.splitlines()  # notes ride in the header, before rooms
    res = compile_source("\n".join(lines) + "\n")
    assert res.plan is not None, res.summary()
    return res


# --- grammar ----------------------------------------------------------------


def test_positioned_note_parses_with_coordinates():
    res = _compile('note "verify well location" at 12,20')
    assert len(res.plan.note_marks) == 1
    nm = res.plan.note_marks[0]
    assert (nm.text, nm.x, nm.y, nm.level) == ("verify well location", 12.0, 20.0, 0)


def test_positioned_note_level_default_is_zero_and_level_is_read():
    res = _compile('note "beam above" at 8,8 level 1\nnote "on grade" at 6,6')
    marks = {nm.text: nm.level for nm in res.plan.note_marks}
    assert marks == {"beam above": 1, "on grade": 0}


def test_unpositioned_note_keeps_current_behaviour():
    # A bare note flows into the free-text `notes` block and creates NO note_mark.
    res = _compile('note "a plain design note"')
    assert res.plan.notes == "a plain design note"
    assert res.plan.note_marks == []


def test_outside_footprint_note_is_a_gentle_info_not_an_error():
    res = _compile('note "future shed" at 80,5')  # well outside the 40x30 envelope
    outside = [d for d in res.diagnostics if d.code == "NOTE_OUTSIDE"]
    assert len(outside) == 1
    d = outside[0]
    assert d.severity.value == "info"           # a nudge, never blocking
    assert d.line == 4                           # points at the note statement
    assert res.ok                                # the plan still compiles clean


def test_note_inside_footprint_raises_no_diagnostic():
    res = _compile('note "on the slab" at 10,10')
    assert not [d for d in res.diagnostics if d.code == "NOTE_OUTSIDE"]


def test_positioned_note_round_trips_through_emit():
    res = _compile('note "beam above" at 20,15 level 1\nnote "well" at 5,5')
    round2 = compile_source(emit_dsl(res.plan))
    assert round2.plan is not None
    got = [(n.text, n.x, n.y, n.level) for n in round2.plan.note_marks]
    assert got == [("beam above", 20.0, 15.0, 1), ("well", 5.0, 5.0, 0)]


# --- render -----------------------------------------------------------------


def test_positioned_note_draws_text_dot_and_leader_in_svg():
    res = _compile('note "verify well location" at 12,15')
    svg = render_svg(res.plan)
    assert "verify well location" in svg          # the callout text
    assert NOTE_COLOR in svg                       # the annotation colour
    assert "<circle" in svg                        # the anchor dot
    assert "font-style=\"italic\"" in svg          # italic note styling


def test_note_renders_on_the_correct_level_block_of_a_multilevel_plan():
    src = """\
plan "Two Floors"
envelope 30 x 24
ceiling 9
note "loft callout" at 6,6 level 1
room living: living at 0,0 size 20 x 20
room loft: loft at 0,0 size 20 x 12 level 1
entry living south width 3
window living north width 4
stair flight at 20,0 size 4 x 10 from 0 to 1
"""
    res = compile_source(src)
    assert res.plan is not None and len(res.plan.levels()) > 1
    svg = render_svg(res.plan)
    # The note text appears once, in the level-1 block (it's the only note).
    assert svg.count("loft callout") == 1


# --- edits ------------------------------------------------------------------

_EDIT_SRC = """\
plan "Edit"
envelope 40 x 30
ceiling 10
note "existing" at 5,5
room living: living at 0,0 size 20 x 20
entry living south width 3
window living north width 4
"""


def _plan(source: str):
    res = compile_source(source)
    assert res.plan is not None, res.summary()
    return res.plan


def test_add_note_edit_appends_a_positioned_note():
    r = apply_edit(_EDIT_SRC, Edit("add_note", text="new callout", x=10, y=12))
    assert r.ok and r.changed
    plan = _plan(r.source)
    assert [(n.text, n.x, n.y) for n in plan.note_marks] == [
        ("existing", 5.0, 5.0),
        ("new callout", 10.0, 12.0),
    ]


def test_add_note_edit_carries_a_level():
    r = apply_edit(_EDIT_SRC, Edit("add_note", text="up top", x=3, y=3, level=1))
    assert r.ok and "level 1" in r.source


def test_move_note_edit_rewrites_only_the_coordinates():
    r = apply_edit(_EDIT_SRC, Edit("move_note", index=0, x=15, y=18))
    assert r.ok and r.changed
    plan = _plan(r.source)
    assert (plan.note_marks[0].x, plan.note_marks[0].y) == (15.0, 18.0)
    assert plan.note_marks[0].text == "existing"       # text untouched


def test_move_note_to_same_spot_is_a_no_op():
    r = apply_edit(_EDIT_SRC, Edit("move_note", index=0, x=5, y=5))
    assert r.ok and not r.changed and r.source == _EDIT_SRC


def test_set_note_edit_rewrites_text_and_position():
    r = apply_edit(_EDIT_SRC, Edit("set_note", index=0, text="renamed", x=7, y=8))
    assert r.ok and r.changed
    plan = _plan(r.source)
    nm = plan.note_marks[0]
    assert (nm.text, nm.x, nm.y) == ("renamed", 7.0, 8.0)


def test_delete_note_edit_removes_the_line():
    r = apply_edit(_EDIT_SRC, Edit("delete_note", index=0))
    assert r.ok and r.changed
    assert _plan(r.source).note_marks == []


def test_note_edits_reject_a_bad_index_with_a_typed_error():
    r = apply_edit(_EDIT_SRC, Edit("move_note", index=9, x=1, y=1))
    assert not r.ok and r.error.kind == "unknown_note"


def test_note_edit_kinds_round_trip_through_json():
    for obj, kind in (
        ({"kind": "add_note", "text": "t", "x": 1, "y": 2}, "add_note"),
        ({"kind": "move_note", "index": 0, "x": 3, "y": 4}, "move_note"),
        ({"kind": "set_note", "index": 0, "text": "z"}, "set_note"),
        ({"kind": "delete_note", "index": 0}, "delete_note"),
    ):
        e = edit_from_json(obj)
        assert isinstance(e, Edit) and e.kind == kind
