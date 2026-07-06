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


def test_rectangular_plan_has_one_run_per_side_at_the_bounds():
    # The wing-aware run finder must degrade to the bounds edge on a plain
    # rectangle — one run per side — so rectangular plans are unchanged.
    plan = compile_source(_TWO_ROOM).plan  # 20 x 9, no wing
    r = _Renderer(plan, RenderConfig())
    assert r._exterior_runs("S") == [(0.0, 0.0, 20.0)]
    assert r._exterior_runs("N") == [(9.0, 0.0, 20.0)]
    assert r._exterior_runs("W") == [(0.0, 0.0, 9.0)]
    assert r._exterior_runs("E") == [(20.0, 0.0, 9.0)]


def _lshape_plan():
    with open("examples/gallery/lshape.barn", encoding="utf-8") as fh:
        return compile_source(fh.read()).plan


def test_wing_footprint_splits_notched_sides_into_per_offset_runs():
    # Maple Bend: a 36×30 main block with an 18×18 east wing at (36,0). The
    # north and east faces are each notched into two colinear runs at *different*
    # wall offsets; the south and west faces stay single continuous runs.
    plan = _lshape_plan()
    r = _Renderer(plan, RenderConfig())
    assert r._exterior_runs("S") == [(0.0, 0.0, 54.0)]
    assert r._exterior_runs("W") == [(0.0, 0.0, 30.0)]
    # North: the wing top (y=18, x 36→54) and the main-block top (y=30, x 0→36).
    assert r._exterior_runs("N") == [(18.0, 36.0, 54.0), (30.0, 0.0, 36.0)]
    # East: the wing east wall (x=54, y 0→18) and the main east wall above the
    # wing (x=36, y 18→30).
    assert r._exterior_runs("E") == [(36.0, 18.0, 30.0), (54.0, 0.0, 18.0)]


def test_wing_run_breaks_only_collect_rooms_backing_that_run():
    plan = _lshape_plan()
    r = _Renderer(plan, RenderConfig())
    rooms = plan.rooms
    # Main-block north run: bed1|closet1|bath|laundry partition x into 16+4+8+8.
    assert r._run_breaks("N", rooms, 30.0, 0.0, 36.0) == [0.0, 16.0, 20.0, 28.0, 36.0]
    # Wing north run: master (12) + mcloset (6) — the hall's y2=18 wall does NOT
    # contribute because its x-extent [0,36] doesn't overlap the wing run [36,54].
    assert r._run_breaks("N", rooms, 18.0, 36.0, 54.0) == [36.0, 48.0, 54.0]
    # Wing east run: mbath (9) + mcloset (9) stacked up the x=54 wall.
    assert r._run_breaks("E", rooms, 54.0, 0.0, 18.0) == [0.0, 9.0, 18.0]


def test_wing_plan_draws_a_chain_per_broken_run_at_its_own_offset():
    plan = _lshape_plan()
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    # The wing-suite ensuite/closet split (6 ft segments) only appears when the
    # east chain follows the x=54 wall — the old bounds-only chain would miss it.
    assert "6′" in svg
    # Two distinct north chain lines sit at the two wall offsets (y=18 and y=30
    # in world space) — proving each run is drawn at its own wall, not the bounds.
    r = _Renderer(plan, RenderConfig(show_room_dims=False))
    off = r._CHAIN_OFFSET
    y18 = r.sy(18.0) - off  # wing north run chain line (north side → wall minus offset)
    y30 = r.sy(30.0) - off  # main-block north run chain line
    assert f'y1="{y18:.1f}"' in svg and f'y1="{y30:.1f}"' in svg
    assert abs(y18 - y30) > 1.0  # genuinely two different rows


def test_multistory_chain_uses_each_levels_own_rooms():
    # The two-story gallery plan: level 0 fills the footprint (breaks on all four
    # sides); the level-1 loft only reaches the S/W walls, so its N/E sides get no
    # chain. Rendering must succeed and carry the ground-level segment labels.
    with open("examples/gallery/two_story.barn", encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    assert "LEVEL 0" in svg and "LEVEL 1" in svg
    # South wall of level 0: the living/kitchen room edge at 24 ft plus the living
    # window (10 ft) and entry jambs now break the chain, so the kitchen keeps its
    # 15 ft segment and the 10 ft window segment reads on the outermost tier.
    assert "15′" in svg and "10′" in svg


# --- exterior opening jamb breaks (§5b) --------------------------------------

# One room filling a 40 ft wall, with a door and two windows on its south side.
# The outermost chain should read wall / opening / wall at each jamb:
#   0—6 wall, 6—9 door(3), 9—14 wall(5), 14—18 window(4), 18—24 wall(6),
#   24—29 window(5), 29—40 wall(11).
_OPENINGS = (
    'plan "Openings"\n'
    "envelope 40 x 24\n"
    "ceiling 9\n"
    "room living: living at 0,0 size 40 x 24\n"
    "entry living south width 3 offset 6\n"
    "window living south width 4 offset 14\n"
    "window living south width 5 offset 24\n"
)


def test_openings_break_the_chain_wall_opening_wall():
    plan = compile_source(_OPENINGS).plan
    r = _Renderer(plan, RenderConfig(show_room_dims=False))
    # The south run has NO interior room boundary, only opening jambs — yet it
    # now breaks at each jamb (hand-computed break coordinates).
    pts, _, _ = r._chain_breaks("S", plan.rooms, 0.0, 0.0, 40.0, 24.0)
    pts = r._with_jambs(pts, r._opening_jambs("S", plan.rooms, 0.0, 0.0, 40.0))
    assert pts == [0.0, 6.0, 9.0, 14.0, 18.0, 24.0, 29.0, 40.0]


def test_opening_jamb_labels_render_on_the_south_chain():
    plan = compile_source(_OPENINGS).plan
    svg = render_svg(plan, RenderConfig(show_room_dims=False))
    # The opening widths (3/4/5) and wall segments (6/5/6/11) all appear as chain
    # labels — a wall that would otherwise be one bare 40 ft span.
    for seg in ("3′", "4′", "5′", "6′", "11′"):
        assert seg in svg, seg


def test_tiny_jamb_segment_collapses_into_its_neighbour():
    # A window whose near jamb sits 0.3 ft from the room-edge break collapses
    # (< 1 ft of label space); the far jamb, well clear, still breaks.
    src = (
        'plan "Collapse"\n'
        "envelope 40 x 24\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 20 x 24\n"
        "room b: kitchen at 20,0 size 20 x 24\n"
        "window b south width 4 offset 0.3\n"
    )
    plan = compile_source(src).plan
    r = _Renderer(plan, RenderConfig(show_room_dims=False))
    jambs = r._opening_jambs("S", plan.rooms, 0.0, 0.0, 40.0)
    assert sorted(jambs) == [20.3, 24.3]  # near + far jamb
    merged = r._with_jambs([0.0, 20.0, 40.0], jambs)
    # 20.3 is within 1 ft of the room edge at 20 → dropped; 24.3 kept.
    assert merged == [0.0, 20.0, 24.3, 40.0]


def test_interior_doors_add_no_plan_leader():
    # An interior door between two rooms must NOT introduce a jamb break on any
    # exterior chain (this phase adds interior-door offsets to the SCHEDULE only).
    src = (
        'plan "Interior"\n'
        "envelope 20 x 12\n"
        "ceiling 9\n"
        "room a: living at 0,0 size 12 x 12\n"
        "room b: kitchen at 12,0 size 8 x 12\n"
        "door a - b width 3 offset 4\n"
    )
    plan = compile_source(src).plan
    r = _Renderer(plan, RenderConfig())
    # No exterior openings → the south chain has only the room-boundary break.
    pts, _, _ = r._chain_breaks("S", plan.rooms, 0.0, 0.0, 20.0, 12.0)
    merged = r._with_jambs(pts, r._opening_jambs("S", plan.rooms, 0.0, 0.0, 20.0))
    assert merged == [0.0, 12.0, 20.0]
