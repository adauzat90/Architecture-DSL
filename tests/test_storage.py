"""Tests for whole-house storage — the program-completeness item (review #7).

Two additions that make dedicated storage (closets + pantry) visible, without
guessing at required rooms (the codebase's established `program`-declares-intent
posture — see the NO_LAUNDRY note in docs/IDEAS.md):

- LOW_STORAGE — an unconditional INFO for a storage-poor plan (conservative floor,
  below the worked gallery, so curated plans are untouched).
- `program ... storage <sqft>` — a declared whole-house storage minimum, checked
  via the existing PROGRAM_MISMATCH warning.
"""

from __future__ import annotations

import pytest

from barndsl import barndominium, compile_source, emit_dsl
from barndsl import Direction as D, RoomType as T


def _codes(result, severity: str) -> set[str]:
    bucket = {"error": result.errors, "warning": result.warnings, "info": result.infos}[
        severity
    ]
    return {d.code for d in bucket}


# A plan with no closets or pantry at all — storage-poor.
_NO_STORAGE = """\
plan "Storage poor"
envelope 40 x 30
ceiling 9
{prog}room living: living  at 0,0  size 24 x 30
room bed:    bedroom at 24,0 size 16 x 30
open living - bed width 6
entry living south width 3 offset 4
window living south width 8 offset 8
window bed east width 4 offset 8
"""

# The same footprint but with a walk-in closet carved out of the bedroom.
_WITH_STORAGE = """\
plan "Storage ok"
envelope 40 x 30
ceiling 9
room living: living  at 0,0  size 24 x 30
room bed:    bedroom at 24,0 size 16 x 22
room cl:     closet  at 24,22 size 16 x 8
open living - bed width 6
door bed - cl width 2.5 offset 1
entry living south width 3 offset 4
window living south width 8 offset 8
window bed east width 4 offset 8
"""


# --- LOW_STORAGE (unconditional) ---------------------------------------------


def test_storage_poor_plan_is_flagged():
    r = compile_source(_NO_STORAGE.format(prog=""))
    assert r.ok  # a nudge, not a blocker
    assert "LOW_STORAGE" in _codes(r, "info")


def test_adequate_storage_is_silent():
    assert "LOW_STORAGE" not in _codes(compile_source(_WITH_STORAGE), "info")


# --- program storage minimum -------------------------------------------------


def test_program_storage_minimum_flags_a_shortfall():
    r = compile_source(_NO_STORAGE.format(prog="program 1 bed storage 60\n"))
    mismatch = [i for i in r.warnings if i.code == "PROGRAM_MISMATCH"]
    assert mismatch and "storage" in mismatch[0].message


def test_program_storage_met_is_silent():
    r = compile_source(_WITH_STORAGE.replace("ceiling 9", "ceiling 9\nprogram 1 bed storage 60"))
    assert "PROGRAM_MISMATCH" not in _codes(r, "warning")


def test_program_storage_round_trips():
    plan = compile_source(_NO_STORAGE.format(prog="program 1 bed storage 60\n")).plan
    assert plan.program_spec.min_storage == 60.0
    assert "storage 60" in emit_dsl(plan)
    assert compile_source(emit_dsl(plan)).plan.program_spec.min_storage == 60.0


def test_builder_rejects_negative_storage():
    with pytest.raises(ValueError):
        barndominium("x").envelope(width=40, length=30).program(2, min_storage=-1)


def test_builder_program_storage():
    plan = (
        barndominium("B")
        .envelope(width=40, length=30)
        .ceiling(9)
        .add_room("a", T.LIVING, x=0, y=0, width=40, length=30)
        .entrance("a", D.SOUTH, width=3, offset=4)
        .program(0, min_storage=50)
    )
    assert plan.program_spec.min_storage == 50.0
