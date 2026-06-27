"""Regression tests for the bug-fix + hardening pass.

Each test maps to a finding from the DSL stress-test (numbered in comments).
They run with no API key and no `anthropic` dependency.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

import pytest

from barndsl import (
    Direction as D,
)
from barndsl import (
    RoomType as T,
)
from barndsl import (
    barndominium,
    compile_source,
    emit_dsl,
    validate,
)
from barndsl.validation import MIN_EGRESS_DOOR_WIDTH


# --- Bug 1 & 2: non-finite numbers ------------------------------------------


def test_nan_and_inf_numbers_are_rejected():
    for bad in ("nan", "inf", "-inf"):
        r = compile_source(f"envelope {bad} x 40\nroom a: living at 0,0 size 10 x 10\n")
        assert not r.ok
        assert any(d.code == "BAD_NUMBER" and d.line == 1 for d in r.errors)


def test_huge_dimensions_do_not_crash():
    # 1e200 * 1e200 overflows to inf internally; the validator must not throw.
    src = 'plan "x"\nenvelope 60 x 40\nceiling 9\nroom a: living at 0,0 size 1e200 x 1e200\nentry a south\n'
    r = compile_source(src)  # must return, not raise
    assert r.plan is not None
    assert any(d.code == "OUT_OF_BOUNDS" for d in r.diagnostics)


# --- Bug 3: openings must be on an exterior wall ----------------------------


def test_interior_window_does_not_satisfy_egress_or_light():
    src = (
        "plan \"x\"\nenvelope 40 x 20\nceiling 9\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room bed: bedroom at 20,0 size 20 x 20\n"  # west wall x=20 is interior
        "entry living south width 3 offset 2\n"
        "door living - bed width 3\n"
        "window bed west width 8 offset 4\n"  # interior wall -> no daylight/egress
    )
    r = compile_source(src)
    codes = {d.code for d in r.diagnostics}
    assert "WINDOW_INTERIOR" in codes
    assert any(d.code == "BEDROOM_EGRESS" and d.room == "bed" for d in r.errors)


def test_interior_exterior_door_is_an_error():
    src = (
        "plan \"x\"\nenvelope 20 x 40\nceiling 9\n"
        "room a: living at 0,0 size 20 x 20\n"
        "room b: living at 0,20 size 20 x 20\n"
        "entry a north width 3 offset 2\n"  # a north wall y=20 is interior
    )
    r = compile_source(src)
    assert any(d.code == "ENTRY_INTERIOR" and d.room == "a" for d in r.errors)


# --- Bug 4: door diagnostics point at the door statement --------------------


def test_door_diagnostic_points_at_the_door_line():
    src = (
        "plan \"x\"\nenvelope 40 x 20\nceiling 9\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room bath: bathroom at 0,21 size 8 x 8\n"  # not adjacent to living
        "entry living south width 3 offset 2\n"
        "door living - bath width 3\n"  # line 7 — the actual fault
    )
    r = compile_source(src)
    noadj = [d for d in r.diagnostics if d.code == "DOOR_NOADJ"][0]
    assert noadj.line == 7  # the door line, NOT living's declaration (line 4)
    assert ":7:" in r.report("p.barn")


# --- Bug 5: emit_dsl escapes quotes (lossless round-trip) -------------------


def test_emit_dsl_escapes_quotes_and_round_trips():
    p = (
        barndominium('A "B" C')
        .envelope(20, 20)
        .ceiling(9)
        .note('say "hi"')
        .add_room("a", T.LIVING, x=0, y=0, width=10, length=10)
    )
    src = emit_dsl(p)
    r = compile_source(src)
    assert r.plan is not None
    assert r.plan.name == 'A "B" C'
    assert r.plan.notes == 'say "hi"'


# --- Bug 6: add_room coerces/validates the room type ------------------------


def test_add_room_rejects_unknown_string_type():
    with pytest.raises(ValueError):
        barndominium("S").envelope(20, 20).add_room(
            "a", "lounge", x=0, y=0, width=5, length=5
        )


def test_add_room_accepts_valid_string_type():
    p = barndominium("S").envelope(20, 20).add_room(
        "a", "living", x=0, y=0, width=5, length=5
    )
    assert p.room("a").type is T.LIVING
    assert "living" in emit_dsl(p)  # no AttributeError on .value


def test_entrance_and_window_coerce_string_walls():
    # A string wall must round-trip through emit_dsl (no AttributeError on .value).
    p = (
        barndominium("W")
        .envelope(20, 20)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=20, length=20)
        .entrance("a", "south", width=3, offset=4)
        .add_window("a", "west", width=6, offset=4)
    )
    assert p.exterior_doors[0].wall is D.SOUTH
    assert p.windows[0].wall is D.WEST
    assert "entry a south" in emit_dsl(p)


# --- Bug 7: the egress door constant equals inches(32) ----------------------


def test_inches_32_door_passes_its_own_egress_check():
    from barndsl import inches

    assert MIN_EGRESS_DOOR_WIDTH == inches(32)
    p = (
        barndominium("E")
        .envelope(20, 20)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=10, length=10)
        .entrance("a", D.SOUTH, width=inches(32), egress=True)
    )
    report = validate(p)
    assert not any(i.code == "EGRESS_DOOR" for i in report.issues)


# --- Bug 8: empty id / name rejected ----------------------------------------


def test_empty_quoted_id_is_rejected():
    r = compile_source('envelope 20 x 20\nroom "": living at 0,0 size 5 x 5\n')
    assert any(d.code == "EMPTY_ID" and d.line == 2 for d in r.errors)


def test_empty_plan_name_is_rejected():
    r = compile_source('plan ""\nenvelope 20 x 20\n')
    assert any(d.code == "EMPTY_ID" and d.line == 1 for d in r.errors)


# --- Bug 9: an opening must fit on its wall ----------------------------------


def test_opening_running_off_the_wall_is_flagged():
    src = (
        "plan \"x\"\nenvelope 30 x 20\nceiling 9\n"
        "room a: living at 0,0 size 30 x 20\n"
        "entry a south width 5 offset 28\n"  # 28 + 5 = 33 > 30
    )
    r = compile_source(src)
    assert any(d.code == "OPENING_OOB" and d.room == "a" for d in r.errors)


# --- Bug 10: a door can't connect a room to itself --------------------------


def test_self_door_is_rejected():
    r = compile_source(
        "envelope 20 x 20\nroom a: living at 0,0 size 10 x 10\ndoor a - a\nentry a south\n"
    )
    assert any(d.code == "SELF_DOOR" for d in r.errors)


# --- Bug 11: caret spans the whole quoted token -----------------------------


def test_quoted_token_caret_spans_the_quotes():
    r = compile_source("envelope \"wide\" x 40\nroom a: living at 0,0 size 5 x 5\n")
    bad = [d for d in r.errors if d.code == "BAD_NUMBER"][0]
    assert bad.col == 10 and bad.end_col == 16  # underlines "wide" incl. quotes
    caret = next(
        ln for ln in r.report("p.barn").splitlines() if set(ln.strip()) <= {"^", "~"}
    )
    assert caret == "    " + " " * 9 + "^" + "~" * 5


# --- Bug 12: floor levels (lofts) -------------------------------------------


def test_loft_on_a_higher_level_does_not_overlap_the_room_below():
    src = (
        "plan \"Cabin\"\nenvelope 30 x 30\nceiling 12\n"
        "room living: living at 0,0 size 30 x 30\n"
        "room loft: loft at 0,0 size 30 x 12 level 1\n"  # sits above living
        "door living - loft width 3\n"  # cross-level = stair opening
        "entry living south width 3 offset 4\n"
        "window living south width 16 offset 4\n"
    )
    r = compile_source(src)
    codes = {d.code for d in r.diagnostics}
    assert "OVERLAP" not in codes  # different levels never overlap
    assert "DOOR_NOADJ" not in codes  # stacked rooms -> valid stair door
    assert r.plan.room("loft").level == 1


def test_cross_level_door_without_overlap_is_flagged():
    src = (
        "plan \"x\"\nenvelope 40 x 20\nceiling 12\n"
        "room living: living at 0,0 size 20 x 20\n"
        "room loft: loft at 20,0 size 20 x 20 level 1\n"  # NOT above living
        "door living - loft width 3\n"
        "entry living south width 3 offset 2\n"
    )
    r = compile_source(src)
    assert any(d.code == "DOOR_NOADJ" for d in r.errors)


def test_level_round_trips_through_emit():
    p = (
        barndominium("L")
        .envelope(30, 30)
        .ceiling(12)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=30)
        .add_room("loft", T.LOFT, x=0, y=0, width=30, length=12, level=1)
    )
    src = emit_dsl(p)
    assert "level 1" in src
    assert compile_source(src).plan.room("loft").level == 1


# --- Bug 14 (doc) + separator-only line -------------------------------------


def test_reference_axis_doc_is_correct():
    from barndsl import DSL_REFERENCE

    assert "east=x+W" in DSL_REFERENCE
    assert "east=x+L" not in DSL_REFERENCE


def test_separator_only_line_is_flagged_not_silently_dropped():
    r = compile_source("envelope 20 x 20\n:::\nroom a: living at 0,0 size 5 x 5\n")
    assert any(d.code == "SYNTAX" and d.line == 2 for d in r.errors)


# --- Ergonomics: relative / anchored placement ------------------------------


def test_relative_placement_in_dsl_abuts_rooms():
    src = (
        "plan \"Rel\"\nenvelope 46 x 26\nceiling 10\n"
        "room great: living at 0,0 size 28 x 26\n"
        "room kitchen: kitchen east-of great size 18 x 26\n"
        "door great - kitchen width 8\n"
        "entry great south width 3 offset 10\n"
        "window great south width 8 offset 8\n"
        "window great west width 6 offset 4\n"
        "window kitchen east width 6 offset 4\n"
    )
    r = compile_source(src)
    k = r.plan.room("kitchen")
    assert (k.x, k.y) == (28.0, 0.0)  # flush against great's east wall
    # They share a wall, so the interior door resolves (no DOOR_NOADJ).
    assert not any(d.code == "DOOR_NOADJ" for d in r.diagnostics)


def test_relative_placement_in_builder():
    p = (
        barndominium("B")
        .envelope(40, 20)
        .ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=20)
        .add_room("kitchen", T.KITCHEN, east_of="living", width=20, length=20)
        .add_room("loft", T.LOFT, north_of="living", width=20, length=10)
    )
    assert (p.room("kitchen").x, p.room("kitchen").y) == (20.0, 0.0)
    assert (p.room("loft").x, p.room("loft").y) == (0.0, 20.0)


def test_relative_placement_unknown_reference_is_flagged():
    r = compile_source(
        "envelope 40 x 20\nroom k: kitchen east-of ghost size 10 x 10\n"
    )
    assert any(d.code == "PLACE_REF" and d.line == 2 for d in r.errors)


def test_builder_relative_unknown_reference_raises():
    with pytest.raises(ValueError):
        barndominium("B").envelope(20, 20).add_room(
            "k", T.KITCHEN, east_of="nope", width=5, length=5
        )


# --- Round 2: new-feature hardening -----------------------------------------


def _err_codes(src: str) -> set[str]:
    return {d.code for d in compile_source(src).errors}


def test_non_integer_level_is_rejected():
    src = (
        'plan "x"\nenvelope 30 x 30\nceiling 10\n'
        "room a: living at 0,0 size 30 x 30\n"
        "room b: loft at 0,0 size 30 x 12 level 0.5\n"
    )
    assert "BAD_LEVEL" in _err_codes(src)


def test_negative_level_is_rejected_in_dsl_and_builder():
    src = (
        'plan "x"\nenvelope 30 x 30\nceiling 10\n'
        "room a: living at 0,0 size 30 x 30 level -1\n"
    )
    assert "BAD_LEVEL" in _err_codes(src)
    with pytest.raises(ValueError):
        barndominium("B").envelope(20, 20).add_room(
            "a", T.LOFT, x=0, y=0, width=5, length=5, level=-1
        )


def test_valid_level_still_compiles():
    src = (
        'plan "ok"\nenvelope 30 x 30\nceiling 12\n'
        "room living: living at 0,0 size 30 x 30\n"
        "room loft: loft at 0,0 size 30 x 12 level 1\n"
        "door living - loft width 3\n"
        "entry living south width 3 offset 4\n"
        "window living south width 16 offset 4\n"
    )
    assert "BAD_LEVEL" not in _err_codes(src)
    assert compile_source(src).plan.room("loft").level == 1


def test_unterminated_string_is_flagged():
    src = 'plan "Home\nenvelope 40 x 30\nceiling 10\nroom a: living at 0,0 size 40 x 30\n'
    assert any(
        d.code == "UNTERMINATED_STRING" and d.line == 1 for d in compile_source(src).errors
    )


def test_missing_placement_reference_is_anchored_on_the_direction():
    src = (
        "envelope 40 x 30\n"
        "room living: living at 0,0 size 24 x 30\n"
        "room kitchen: kitchen east-of size 24 x 18\n"  # ref omitted before `size`
    )
    bad = [d for d in compile_source(src).errors if d.code == "BAD_PLACEMENT"]
    assert bad and bad[0].line == 3
    assert "east-of" in bad[0].message


def test_quoted_value_is_not_accepted_as_a_number():
    src = 'envelope "30" x 30\nroom a: living at 0,0 size 10 x 10\n'
    assert "BAD_NUMBER" in _err_codes(src)


def test_builder_self_anchor_has_clear_message():
    with pytest.raises(ValueError, match="itself"):
        barndominium("B").envelope(20, 20).add_room(
            "a", T.LIVING, east_of="a", width=5, length=5
        )


def test_anchor_align_and_offset_slide_along_the_shared_wall():
    src = (
        'plan "Align"\nenvelope 40 x 30\nceiling 10\n'
        "room living: living at 0,0 size 20 x 30\n"
        "room far: bedroom east-of living align far size 12 x 10\n"
        "room mid: office east-of living align center size 8 x 6\n"
        "room shift: office east-of living offset 5 size 8 x 6\n"
        "room hall: hallway at 0,0 size 30 x 4\n"  # ignore; just need rooms
    )
    p = compile_source(src).plan
    assert (p.room("far").x, p.room("far").y) == (20.0, 20.0)  # 30-10 from south
    assert (p.room("mid").x, p.room("mid").y) == (20.0, 12.0)  # (30-6)/2
    assert (p.room("shift").x, p.room("shift").y) == (20.0, 5.0)


def test_north_anchor_offset_shifts_east():
    src = (
        'plan "N"\nenvelope 40 x 30\nceiling 10\n'
        "room hall: hallway at 0,0 size 30 x 4\n"
        "room bed: bedroom north-of hall offset 6 size 10 x 10\n"
    )
    p = compile_source(src).plan
    assert (p.room("bed").x, p.room("bed").y) == (6.0, 4.0)


def test_plain_anchor_is_unchanged_by_the_new_options():
    src = (
        "envelope 40 x 30\n"
        "room a: living at 0,0 size 20 x 30\n"
        "room b: kitchen east-of a size 20 x 30\n"
    )
    b = compile_source(src).plan.room("b")
    assert (b.x, b.y) == (20.0, 0.0)  # near/offset-0 == old behaviour


def test_builder_align_and_offset_match_dsl():
    p = (
        barndominium("B")
        .envelope(40, 30)
        .ceiling(10)
        .add_room("living", T.LIVING, x=0, y=0, width=20, length=30)
        .add_room("far", T.BEDROOM, east_of="living", align="far", width=12, length=10)
        .add_room("shift", T.OFFICE, east_of="living", offset=5, width=8, length=6)
    )
    assert (p.room("far").x, p.room("far").y) == (20.0, 20.0)
    assert (p.room("shift").x, p.room("shift").y) == (20.0, 5.0)


def test_bad_alignment_keyword_is_flagged():
    src = (
        "envelope 40 x 30\n"
        "room a: living at 0,0 size 10 x 10\n"
        "room b: office east-of a align sideways size 5 x 5\n"
    )
    assert any(d.code == "BAD_PLACEMENT" and d.line == 3 for d in compile_source(src).errors)


def test_align_or_offset_without_anchor_raises_in_builder():
    with pytest.raises(ValueError, match="anchor"):
        barndominium("B").envelope(20, 20).add_room(
            "a", T.LIVING, x=0, y=0, width=5, length=5, offset=3
        )


def test_summary_reports_an_info_count():
    # A plan with an interior kitchen yields a NAT_LIGHT warning; the summary
    # line now carries an info count too.
    from barndsl import compile_file

    s = compile_file(
        os.path.join(os.path.dirname(__file__), "..", "examples", "cedar_ridge.barn")
    ).summary()
    assert "info(s)" in s


# --- Round 3: align/offset hardening ----------------------------------------


def test_builder_rejects_non_finite_numbers():
    # The DSL rejects nan/inf at parse; the builder must too (a NaN coord
    # otherwise defeats every geometry comparison and corrupts the round-trip).
    for field in ("offset", "x", "width"):
        kwargs = dict(x=0, y=0, width=5, length=5)
        if field == "offset":
            kwargs = dict(east_of="ref", width=5, length=5, offset=float("nan"))
        else:
            kwargs[field] = float("nan")
        b = barndominium("B").envelope(50, 50).add_room(
            "ref", T.LIVING, x=0, y=0, width=10, length=10
        )
        with pytest.raises(ValueError, match="finite"):
            b.add_room("x", T.OFFICE, **kwargs)


def test_validation_catches_non_finite_geometry_defense_in_depth():
    # Even if a NaN is injected past the builder guard, validation flags it
    # rather than letting it pass (NaN comparisons are all False).
    from barndsl import Room, validate

    plan = barndominium("B").envelope(20, 20).ceiling(9)
    plan.rooms.append(Room("bad", T.LIVING, float("nan"), 0, 10, 10))
    assert any(i.code == "ROOM_GEOMETRY" for i in validate(plan).errors)


def test_builder_align_is_case_insensitive():
    p = (
        barndominium("B")
        .envelope(40, 30)
        .add_room("a", T.LIVING, x=0, y=0, width=20, length=30)
        .add_room("b", T.BEDROOM, east_of="a", align="FAR", width=12, length=10)
    )
    assert (p.room("b").x, p.room("b").y) == (20.0, 20.0)


def test_out_of_bounds_hint_is_placement_aware():
    # A relatively-placed room has no x,y token; the hint must not say "set its y".
    src = (
        "envelope 40 x 40\n"
        "room a: living at 0,0 size 20 x 20\n"
        "room b: bedroom east-of a align far size 8 x 30\n"  # far -> y = -10
        "entry a south width 3 offset 2\n"
    )
    oob = [d for d in compile_source(src).errors if d.code == "OUT_OF_BOUNDS"][0]
    assert "set its y" not in oob.hint
    assert "align/offset" in oob.hint and "east_of a" in oob.hint


def test_emit_dsl_resolves_align_offset_to_absolute():
    # align/offset are build-time sugar; the emitted source must be absolute and
    # carry no relative tokens (guards the round-trip).
    p = (
        barndominium("E")
        .envelope(40, 30)
        .ceiling(10)
        .add_room("a", T.LIVING, x=0, y=0, width=20, length=30)
        .add_room("b", T.OFFICE, east_of="a", offset=5, width=8, length=6)
    )
    src = emit_dsl(p)
    assert "at 20,5" in src
    for tok in ("east-of", "align", "offset"):
        assert tok not in src
