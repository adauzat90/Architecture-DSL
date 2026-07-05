"""Chained per-side exterior dimension strings on the 2D plan SVG.

A chain is the run of tick-to-tick segments along an exterior wall where interior
room boundaries meet it, each labelled with its length. A side with no interior
break is left to the overall dimension line (no duplicated single-segment string).
"""

from __future__ import annotations

from barndsl import compile_source, render_svg
from barndsl.elements import Barndominium, Room, RoomType
from barndsl.render import RenderConfig, _Renderer

# Two rooms side-by-side along the full depth: they partition the south (and,
# mirrored, the north) wall into a 12 ft + 8 ft chain; the two vertical walls are
# each met by a single full-height room, so they carry no chain.
_TWO_ROOM = (
    'plan "Chain Demo"\n'
    "envelope 20 x 9\n"
    "ceiling 9\n"
    "room living: living at 0,0 size 12 x 9\n"
    "room kitchen: kitchen at 12,0 size 8 x 9\n"
    "entry living south width 3 offset 4\n"
)

# A single room filling the envelope: every side is one segment, so no chain is
# drawn anywhere (the overall dims stand alone).
_SOLO = (
    'plan "Solo"\n'
    "envelope 23 x 9\n"
    "ceiling 9\n"
    "room big: living at 0,0 size 23 x 9\n"
    "entry big south width 3 offset 4\n"
)


def test_south_wall_gets_a_chain_with_both_segment_labels():
    # Room dims off so the 12′/8′ come only from the exterior chain string.
    plan = compile_source(_TWO_ROOM).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    assert "12′" in svg and "8′" in svg  # the two south-wall chain segments
    # The overall dimension still shows the full 20 ft span — the chain reports
    # the sub-lengths, never the full span, so nothing is stacked twice.
    assert "20′" in svg


def test_a_side_with_no_interior_break_draws_no_chain():
    plan = compile_source(_SOLO).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    # Only the overall *length* dimension is a rotated label; neither vertical
    # wall (each one segment) adds a chain, so there is exactly one.
    assert svg.count("rotate(-90") == 1
    # The 23 ft width appears only in the subtitle extent and the overall width
    # dimension — a wrongly-drawn single-segment chain would duplicate it.
    assert svg.count("23′") == 2


def test_compass_title_and_overall_dims_still_present():
    plan = compile_source(_TWO_ROOM).plan
    svg = render_svg(plan)
    assert "Chain Demo" in svg  # title block
    assert "plan north" in svg or "true N" in svg  # north-arrow compass caption
    assert "20′" in svg and "9′" in svg  # overall width + length dimensions


def test_chain_breaks_skip_full_and_inset_rooms():
    # A full-width room and an interior (inset) room touching no exterior wall
    # contribute no interior break to the south side — only the span remains.
    plan = Barndominium("Inset").envelope(20, 12)
    plan.rooms = [
        Room("big", RoomType.LIVING, 0, 0, 20, 12),
        Room("mid", RoomType.OFFICE, 5, 4, 6, 4),
    ]
    r = _Renderer(plan, RenderConfig())
    pts, lo, hi = r._chain_breaks("S", plan.rooms, 0.0, 0.0, 20.0, 12.0)
    assert pts == [0.0, 20.0] and (lo, hi) == (0.0, 20.0)

    # Two abutting rooms do partition the south wall at their shared edge.
    plan2 = Barndominium("Split").envelope(20, 12)
    plan2.rooms = [
        Room("a", RoomType.LIVING, 0, 0, 12, 12),
        Room("b", RoomType.KITCHEN, 12, 0, 8, 12),
    ]
    r2 = _Renderer(plan2, RenderConfig())
    pts2, _, _ = r2._chain_breaks("S", plan2.rooms, 0.0, 0.0, 20.0, 12.0)
    assert pts2 == [0.0, 12.0, 20.0]


def test_multistory_chain_uses_each_levels_own_rooms():
    # The two-story gallery plan: level 0 fills the footprint (breaks on all four
    # sides); the level-1 loft only reaches the S/W walls, so its N/E sides get no
    # chain. Rendering must succeed and carry the ground-level segment labels.
    with open("examples/gallery/two_story.barn", encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    assert "LEVEL 0" in svg and "LEVEL 1" in svg
    # South wall of level 0 splits 39 ft into 24 + 15 at the living/kitchen edge.
    assert "24′" in svg and "15′" in svg
