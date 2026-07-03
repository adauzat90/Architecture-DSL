"""Tests for the electrical / life-safety check batch.

Two additions, both INFO (they never block a compile):

- PLUMBING_STACK  — a geometric, defect-only check: an upper-floor wet room that
  sits over no wet room below, so its waste stack can't drop straight down. The
  cross-floor analogue of WET_GROUP. Fires only on a real multi-storey defect, so
  single-storey and dry-upper-floor plans are untouched.
- ELECTRICAL_PLAN — an opt-in reminder (the `electrical` directive, mirroring
  `accessible`) for the code items the geometry can't verify: receptacle spacing,
  switched lighting, stair lighting, and exterior-door landings. Off by default so
  ordinary plans aren't nagged; the stair / landing clauses appear only when the
  plan actually has a stair / an exterior people-door.
"""

from __future__ import annotations

from barndsl import barndominium, compile_source, emit_dsl
from barndsl import Direction as D, RoomType as T


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# --- PLUMBING_STACK ----------------------------------------------------------

# A two-storey plan whose upper bath sits over the LIVING room (dry below), so its
# waste stack can't drop straight into a wet room.
_UNSTACKED = """\
plan "Unstacked bath"
envelope 39 x 33
ceiling 9
room living:  living   at 0,0   size 24 x 18
room kitchen: kitchen  at 24,0  size 15 x 18
room hall:    hallway  at 0,18  size 39 x 4
room bed:     bedroom  at 0,22  size 14 x 11
room bath:    bathroom at 14,22 size 10 x 11
room laundry: laundry  at 24,22 size 15 x 11
room loft:    loft     at 12,0  size 27 x 18 level 1
room bath2:   bathroom at 0,0   size 12 x 12 level 1
stair flight at 33,0 size 4 x 12 from 0 to 1
open living - kitchen width 10
open living - hall width 4
door hall - bed width 3 offset 0.5
door hall - bath width 2.67 offset 2
door hall - laundry width 2.5 offset 9
door loft - bath2 width 2.67 offset 2
entry living south width 3 offset 14
entry laundry east width 3 offset 4
window living south width 10 offset 2
window kitchen south width 7 offset 4
window bed north width 4 offset 5
window bath2 north width 3 offset 2
window loft south width 10 offset 8
"""

# The same plan with the upper bath moved directly over the ground-floor kitchen
# (a wet room), so the stack drops straight down.
_STACKED = _UNSTACKED.replace(
    "room loft:    loft     at 12,0  size 27 x 18 level 1\n"
    "room bath2:   bathroom at 0,0   size 12 x 12 level 1",
    "room loft:    loft     at 0,0   size 24 x 18 level 1\n"
    "room bath2:   bathroom at 24,0  size 12 x 12 level 1",
).replace("stair flight at 33,0 size 4 x 12", "stair flight at 0,3 size 4 x 12")


def test_unstacked_upper_wet_room_nudges():
    r = compile_source(_UNSTACKED)
    assert r.ok  # INFO — a nudge, not a blocker
    assert "PLUMBING_STACK" in _codes(r, "info")


def test_wet_room_stacked_over_wet_room_is_silent():
    r = compile_source(_STACKED)
    assert r.ok
    assert "PLUMBING_STACK" not in _codes(r, "info")


def test_single_storey_never_stacks():
    # No upper floor at all — the check must not fire (and must not error).
    src = """\
plan "One storey"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room bath:   bathroom at 18,0 size 12 x 12
room kitchen: kitchen at 18,12 size 12 x 12
door living - bath width 2.67
door living - kitchen width 6
entry living south width 3 offset 4
window living south width 8 offset 5
window kitchen east width 4 offset 4
"""
    r = compile_source(src)
    assert "PLUMBING_STACK" not in _codes(r, "info")


def test_dry_upper_floor_is_silent():
    # An upper loft (dry) over the living room never trips the wet-stack check.
    src = """\
plan "Dry loft"
envelope 30 x 24
ceiling 9
room living: living  at 0,0  size 18 x 24
room kitchen: kitchen at 18,0 size 12 x 24
room loft:   loft    at 0,0  size 18 x 18 level 1
stair flight at 0,2 size 4 x 12 from 0 to 1
open living - kitchen width 8
entry living south width 3 offset 4
window living south width 8 offset 5
window kitchen south width 4 offset 4
window loft north width 6 offset 6
"""
    r = compile_source(src)
    assert "PLUMBING_STACK" not in _codes(r, "info")


# --- ELECTRICAL_PLAN (opt-in) ------------------------------------------------

_ELEC_BASE = """\
plan "Electrical"
envelope 30 x 24
ceiling 9
{opt}room living:  living   at 0,0   size 18 x 24
room kitchen: kitchen  at 18,0  size 12 x 12
room bath:    bathroom at 18,12 size 12 x 12
door living - kitchen width 6
door living - bath width 2.67
entry living south width 3 offset 4
entry living north width 3 offset 4
window living south width 8 offset 8
window kitchen south width 4 offset 4
"""


def test_electrical_off_by_default():
    r = compile_source(_ELEC_BASE.format(opt=""))
    assert "ELECTRICAL_PLAN" not in _codes(r, "info")


def test_electrical_directive_emits_reminder():
    r = compile_source(_ELEC_BASE.format(opt="electrical\n"))
    assert r.ok  # a reminder, never a blocker
    assert "ELECTRICAL_PLAN" in _codes(r, "info")
    msg = next(i.message for i in r.infos if i.code == "ELECTRICAL_PLAN")
    # Receptacle + lighting clauses are always present …
    assert "E3901.2" in msg and "R303.7" in msg
    # … the landing clause appears (there is an exterior people-door) …
    assert "R311.3" in msg
    # … but there is no stair, so no stair-lighting clause.
    assert "each stair" not in msg


def test_electrical_stair_clause_only_with_a_stair():
    src = """\
plan "Two storey electrical"
envelope 30 x 24
ceiling 9
electrical
room living: living  at 0,0  size 18 x 24
room kitchen: kitchen at 18,0 size 12 x 24
room loft:   loft    at 0,0  size 18 x 18 level 1
stair flight at 0,2 size 4 x 12 from 0 to 1
open living - kitchen width 8
entry living south width 3 offset 4
window living south width 8 offset 5
window kitchen south width 4 offset 4
window loft north width 6 offset 6
"""
    r = compile_source(src)
    msg = next(i.message for i in r.infos if i.code == "ELECTRICAL_PLAN")
    assert "each stair" in msg


def test_electrical_round_trips_through_emit():
    plan = compile_source(_ELEC_BASE.format(opt="electrical\n")).plan
    dsl = emit_dsl(plan)
    assert "electrical" in dsl.splitlines()
    # And the emitted source still parses and still carries the flag.
    again = compile_source(dsl).plan
    assert again.electrical is True


def test_mark_electrical_from_builder():
    plan = (
        barndominium("Builder")
        .envelope(width=30, length=24)
        .ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=30, length=24)
        .entrance("living", D.SOUTH, width=3, offset=4)
        .add_window("living", D.NORTH, width=8, offset=5)
    )
    assert plan.electrical is False
    plan.mark_electrical()
    assert plan.electrical is True
    assert "electrical" in emit_dsl(plan).splitlines()
