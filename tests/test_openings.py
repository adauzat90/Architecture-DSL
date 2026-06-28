"""Tests for `open` — cased openings / walk-throughs between rooms.

An `open` connection is a doorway with no leaf: the open-concept link between
e.g. a kitchen and a living area. It joins the two rooms in the circulation
graph exactly like a `door`, but renders as a plain gap (no swing arc) and is
exempt from the narrow-door warning (a walk-through is wide by design).
"""

from __future__ import annotations

from barndsl import (
    RoomType as T,
    barndominium,
    compile_source,
    emit_dsl,
    save_svg,
    validate,
)
from barndsl.elements import DEFAULT_OPENING_WIDTH


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


_OPEN_PLAN = """\
plan "Open concept"
envelope 40 x 24
ceiling 9
room kitchen: kitchen at 0,0  size 18 x 24
room living:  living  at 18,0 size 22 x 24
open kitchen - living width 10
entry living south width 3 offset 8
"""


# --- parsing + semantics -----------------------------------------------------


def test_open_parses_to_a_leafless_interior_door():
    r = compile_source(_OPEN_PLAN)
    assert r.ok, r.report()
    doors = r.plan.interior_doors
    assert len(doors) == 1
    assert doors[0].leaf is False
    assert doors[0].width == 10


def test_open_defaults_to_a_wide_opening():
    src = """\
plan "Default open"
envelope 40 x 24
ceiling 9
room kitchen: kitchen at 0,0  size 18 x 24
room living:  living  at 18,0 size 22 x 24
open kitchen - living
entry living south width 3 offset 8
"""
    r = compile_source(src)
    assert r.ok, r.report()
    assert r.plan.interior_doors[0].width == DEFAULT_OPENING_WIDTH


def test_open_connects_rooms_for_reachability():
    # `living` is the only room with an entry; `kitchen` is reachable only via
    # the open passage. If `open` didn't join them, kitchen would be NO_ACCESS.
    r = compile_source(_OPEN_PLAN)
    assert "NO_ACCESS" not in _codes(r, "error")


def test_open_must_share_a_wall_like_a_door():
    src = """\
plan "Disjoint"
envelope 40 x 24
ceiling 9
room kitchen: kitchen at 0,0   size 12 x 12
room living:  living  at 20,12 size 12 x 12
open kitchen - living
entry kitchen south width 3 offset 4
"""
    r = compile_source(src)
    assert "DOOR_NOADJ" in _codes(r, "error")


def test_open_too_wide_for_the_wall_still_warns():
    src = """\
plan "Too wide"
envelope 40 x 24
ceiling 9
room kitchen: kitchen at 0,0  size 18 x 10
room living:  living  at 18,0 size 22 x 24
open kitchen - living width 14
entry living south width 3 offset 8
"""
    r = compile_source(src)
    # The shared wall is only 10 ft, but the opening asks for 14 ft.
    assert "DOOR_FIT" in _codes(r, "warning")


# --- the narrow-door exemption ----------------------------------------------


def test_narrow_door_warns_but_narrow_opening_does_not():
    door_src = """\
plan "Narrow door"
envelope 40 x 24
ceiling 9
room kitchen: kitchen at 0,0  size 18 x 24
room living:  living  at 18,0 size 22 x 24
door kitchen - living width 2
entry living south width 3 offset 8
"""
    open_src = door_src.replace("door kitchen - living width 2", "open kitchen - living width 2")
    assert "DOOR_NARROW" in _codes(compile_source(door_src), "warning")
    assert "DOOR_NARROW" not in _codes(compile_source(open_src), "warning")


# --- bathroom privacy (OPEN_BATH) -------------------------------------------


def test_open_into_a_bathroom_warns():
    src = """\
plan "Doorless bath"
envelope 34 x 20
ceiling 9
room living: living   at 0,0  size 22 x 20
room bath:   bathroom at 22,0 size 12 x 20
open living - bath width 6
entry living south width 3 offset 8
window bath east width 4 offset 8
"""
    r = compile_source(src)
    assert "OPEN_BATH" in _codes(r, "warning")
    msg = next(d for d in r.warnings if d.code == "OPEN_BATH").message
    assert "bath" in msg


def test_open_into_a_half_bath_warns():
    src = """\
plan "Doorless powder"
envelope 34 x 20
ceiling 9
room living: living    at 0,0  size 22 x 20
room powder: half_bath at 22,0 size 12 x 20
open living - powder width 6
entry living south width 3 offset 8
window powder east width 4 offset 8
"""
    r = compile_source(src)
    assert "OPEN_BATH" in _codes(r, "warning")


def test_door_into_a_bathroom_does_not_warn():
    src = """\
plan "Bath with a door"
envelope 34 x 20
ceiling 9
room living: living   at 0,0  size 22 x 20
room bath:   bathroom at 22,0 size 12 x 20
door living - bath width 2.67
entry living south width 3 offset 8
window bath east width 4 offset 8
"""
    r = compile_source(src)
    assert "OPEN_BATH" not in _codes(r, "warning")


def test_open_between_non_bath_rooms_does_not_warn():
    r = compile_source(_OPEN_PLAN)
    assert "OPEN_BATH" not in _codes(r, "warning")


# --- emit round-trip ---------------------------------------------------------


def test_open_round_trips_through_emit():
    r = compile_source(_OPEN_PLAN)
    text = emit_dsl(r.plan)
    assert "open kitchen - living width 10" in text
    assert "door kitchen - living" not in text
    # Re-compiling the emitted source preserves the leafless connection.
    r2 = compile_source(text)
    assert r2.ok, r2.report()
    assert r2.plan.interior_doors[0].leaf is False


# --- builder API -------------------------------------------------------------


def test_builder_opening_method():
    plan = (
        barndominium("Builder open")
        .envelope(width=40, length=24)
        .ceiling(9)
        .add_room("kitchen", T.KITCHEN, x=0, y=0, width=18, length=24)
        .add_room("living", T.LIVING, x=18, y=0, width=22, length=24)
        .opening("kitchen", "living", width=10)
        .entrance("living", "south", width=3, offset=8)
    )
    assert plan.interior_doors[0].leaf is False
    result = validate(plan)
    assert result.is_valid, result.summary()
    assert "open kitchen - living width 10" in emit_dsl(plan)


def test_connect_default_still_has_a_leaf():
    plan = (
        barndominium("Builder door")
        .envelope(width=40, length=24)
        .add_room("kitchen", T.KITCHEN, x=0, y=0, width=18, length=24)
        .add_room("living", T.LIVING, x=18, y=0, width=22, length=24)
        .connect("kitchen", "living")
    )
    assert plan.interior_doors[0].leaf is True


# --- rendering ---------------------------------------------------------------


def test_open_renders_without_a_swing_arc(tmp_path):
    door_src = _OPEN_PLAN.replace("open kitchen - living width 10", "door kitchen - living width 6")
    door_svg = tmp_path / "door.svg"
    open_svg = tmp_path / "open.svg"
    save_svg(compile_source(door_src).plan, str(door_svg))
    save_svg(compile_source(_OPEN_PLAN).plan, str(open_svg))
    door_arcs = door_svg.read_text().count(" A ")
    open_arcs = open_svg.read_text().count(" A ")
    # The door draws a swing arc; the opening draws none.
    assert door_arcs > open_arcs
