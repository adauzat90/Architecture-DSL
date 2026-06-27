"""Tests for stairs and multi-story plans."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

import pytest

from barndsl import (
    RoomType as T,
)
from barndsl import (
    barndominium,
    compile_source,
    emit_dsl,
    render_svg,
)


def _two_story_src(with_stair: bool = True) -> str:
    src = (
        'plan "Two"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 40 x 30\n"
        "room loft: loft at 0,0 size 28 x 20 level 1\n"
        "entry living south width 3 offset 10\n"
        "window living west width 12 offset 8\n"
        "window loft west width 10 offset 4\n"
    )
    if with_stair:
        src += "stair s at 24,2 size 4 x 12 from 0 to 1\n"
    return src


def test_stair_makes_the_upper_floor_reachable():
    r = compile_source(_two_story_src(with_stair=True))
    assert not any(d.code == "NO_ACCESS" for d in r.diagnostics)
    assert len(r.plan.stairs) == 1
    assert r.plan.levels() == [0, 1]


def test_loft_without_a_stair_is_unreachable():
    r = compile_source(_two_story_src(with_stair=False))
    na = [d for d in r.diagnostics if d.code == "NO_ACCESS" and d.room == "loft"]
    assert na  # a loft with no stair/cross-level door can't be reached
    assert na[0].severity.value == "warning"  # loft downgraded from error


def test_stair_outside_envelope_is_flagged():
    src = _two_story_src() + "stair bad at 38,2 size 8 x 12 from 0 to 1\n"
    assert any(d.code == "STAIR_OOB" for d in compile_source(src).errors)


def test_stair_landing_in_no_room_warns():
    # A stair over empty floor on the upper level connects nothing.
    src = (
        'plan "x"\nenvelope 40 x 30\nceiling 9\n'
        "room living: living at 0,0 size 40 x 30\n"
        "room loft: loft at 0,0 size 10 x 10 level 1\n"  # only a small loft
        "entry living south width 3 offset 10\n"
        "window living west width 12 offset 8\n"
        "window loft west width 4 offset 2\n"
        "stair s at 30,2 size 4 x 12 from 0 to 1\n"  # x=30 misses the 10-wide loft
    )
    assert any(d.code == "STAIR_FLOAT" for d in compile_source(src).warnings)


def test_stair_same_level_is_rejected():
    assert any(
        d.code == "BAD_LEVEL"
        for d in compile_source(
            'plan "x"\nenvelope 20 x 20\nstair s at 0,0 size 4 x 8 from 1 to 1\n'
        ).errors
    )
    with pytest.raises(ValueError):
        barndominium("B").envelope(20, 20).add_stair(
            "s", x=0, y=0, width=4, length=8, from_level=1, to_level=1
        )


def test_stair_round_trips():
    plan = (
        barndominium("B")
        .envelope(40, 30)
        .ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=40, length=30)
        .add_room("loft", T.LOFT, x=0, y=0, width=28, length=20, level=1)
        .add_stair("s", x=24, y=2, width=4, length=12, from_level=0, to_level=1)
    )
    src = emit_dsl(plan)
    assert "stair s at 24,2 size 4 x 12 from 0 to 1" in src
    rr = compile_source(src)
    assert rr.plan is not None and len(rr.plan.stairs) == 1
    s = rr.plan.stairs[0]
    assert (s.x, s.y, s.width, s.length, s.from_level, s.to_level) == (24, 2, 4, 12, 0, 1)
    assert emit_dsl(rr.plan) == src  # idempotent


def test_multistory_renders_separate_level_blocks():
    plan = (
        barndominium("B")
        .envelope(40, 30)
        .ceiling(9)
        .add_room("living", T.LIVING, x=0, y=0, width=40, length=30)
        .add_room("loft", T.LOFT, x=0, y=0, width=28, length=20, level=1)
        .add_stair("s", x=24, y=2, width=4, length=12, from_level=0, to_level=1)
    )
    svg = render_svg(plan)
    assert "LEVEL 0" in svg and "LEVEL 1" in svg
    assert "Level 1 area" in svg  # per-level area in the summary panel


def test_single_level_plan_has_no_level_labels():
    # Regression: a plain plan still renders the original way (no level blocks).
    from simple_barndo import build_example

    svg = render_svg(build_example())
    assert "LEVEL 0" not in svg
    assert svg.startswith("<svg")
