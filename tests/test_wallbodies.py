"""The wall bodies are one geometry, drawn two ways.

:mod:`barndsl.wallbodies` is the single source of truth for wall construction:
closed bands at nominal thickness, corners squared, openings cut at the jambs.
Both the DXF export and the SVG floor plan build their walls from it, so a wall
in the CAD file and the same wall on screen are the identical rectangle. These
tests pin that parity and the new poché wall-body look in the SVG.
"""

from __future__ import annotations

import re

from barndsl import compile_source, render_svg, to_dxf
from barndsl.render import POCHE_FILL, RenderConfig, _Renderer
from barndsl.wallbodies import (
    EXTERIOR,
    INTERIOR,
    PLUMBING,
    opening_gaps,
    partition_class,
    wall_bands,
)

# A fixture exercising every band flavour: exterior shell + windows + an entry,
# ordinary partitions, a declared plumbing wall, a swing door and a cased opening.
FIXTURE = """\
plan "Parity"
envelope 40 x 24
ceiling 9
room living: living   at 0,0   size 22 x 24
room bath:   bathroom at 22,0  size 8 x 12
room util:   utility  at 30,0  size 10 x 12
room bed:    bedroom  at 22,12 size 18 x 12
wall bath - living plumbing
door living - bath width 2.5
door bath - bed width 2.5
open bed - util width 6
entry living south width 3 offset 6
window living south width 6 offset 12
window bed east width 4 offset 4
"""


def _norm(x0, y0, x1, y1):
    return (round(min(x0, x1), 4), round(min(y0, y1), 4),
            round(max(x0, x1), 4), round(max(y0, y1), 4))


def _dxf_wall_rects(dxf: str) -> set[tuple[float, float, float, float]]:
    """Bounding rectangles of every A-WALL closed polyline in a DXF document."""
    lines = dxf.split("\n")
    rects: set[tuple[float, float, float, float]] = set()
    etype = layer = None
    xs: list[float] = []
    ys: list[float] = []
    pending_x: float | None = None

    def flush():
        if etype == "LWPOLYLINE" and layer == "A-WALL" and xs:
            rects.add(_norm(min(xs), min(ys), max(xs), max(ys)))

    i = 0
    while i + 1 < len(lines):
        code, value = lines[i].strip(), lines[i + 1]
        if code == "0":
            flush()
            etype = value
            layer = None
            xs, ys, pending_x = [], [], None
        elif code == "8":
            layer = value
        elif code == "10":
            pending_x = float(value)
        elif code == "20" and pending_x is not None:
            xs.append(pending_x)
            ys.append(float(value))
            pending_x = None
        i += 2
    flush()
    return rects


_WALLS_GROUP = re.compile(
    r'<g data-layer="walls"[^>]*>(.*?)</g>', re.DOTALL
)
_RECT = re.compile(
    r'<rect x="([\d.-]+)" y="([\d.-]+)" width="([\d.-]+)" height="([\d.-]+)"'
)


def _svg_wall_screen_rects(svg: str) -> set[tuple[float, float, float, float]]:
    m = _WALLS_GROUP.search(svg)
    assert m, "no wall-band layer in the SVG"
    body = m.group(1)
    assert POCHE_FILL in body, "wall bands are not drawn with the poché fill"
    return {
        (float(a), float(b), float(c), float(d))
        for a, b, c, d in _RECT.findall(body)
    }


def _band_screen_rects(plan) -> set[tuple[float, float, float, float]]:
    r = _Renderer(plan, RenderConfig())
    out = set()
    for b in wall_bands(plan, 0):
        x = round(r.sx(min(b.x0, b.x1)), 1)
        y = round(r.sy(max(b.y0, b.y1)), 1)
        w = round(abs(b.x1 - b.x0) * r.c.scale, 1)
        h = round(abs(b.y1 - b.y0) * r.c.scale, 1)
        out.add((x, y, w, h))
    return out


def test_dxf_walls_are_exactly_the_shared_bands():
    plan = compile_source(FIXTURE).plan
    want = {_norm(b.x0, b.y0, b.x1, b.y1) for b in wall_bands(plan, 0)}
    assert want, "fixture should have wall bands"
    assert _dxf_wall_rects(to_dxf(plan)) == want


def test_svg_walls_are_exactly_the_shared_bands():
    plan = compile_source(FIXTURE).plan
    assert _svg_wall_screen_rects(render_svg(plan)) == _band_screen_rects(plan)


def test_dxf_and_svg_walls_agree_for_a_wing_plan():
    # An L-footprint stresses the squared corners and notched exterior runs; the
    # DXF and the SVG must still derive from one band set.
    with open("examples/lshape.barn", encoding="utf-8") as fh:
        plan = compile_source(fh.read()).plan
    want = {_norm(b.x0, b.y0, b.x1, b.y1) for b in wall_bands(plan, 0)}
    assert _dxf_wall_rects(to_dxf(plan)) == want
    assert _svg_wall_screen_rects(render_svg(plan)) == _band_screen_rects(plan)


def test_partition_class_picks_the_plumbing_wall():
    plan = compile_source(FIXTURE).plan
    by_id = {r.id: r for r in plan.rooms}
    assert partition_class(plan, by_id["bath"], by_id["living"]) == PLUMBING
    assert partition_class(plan, by_id["bath"], by_id["bed"]) == INTERIOR


def test_bands_carry_thickness_class_and_provenance():
    plan = compile_source(FIXTURE).plan
    bands = wall_bands(plan, 0)
    ext = [b for b in bands if b.kind == "exterior"]
    inter = [b for b in bands if b.kind == "interior"]
    assert ext and inter
    assert all(b.thickness_class == EXTERIOR and b.wall in ("S", "N", "W", "E")
               and b.room_b is None for b in ext)
    assert all(b.room_a and b.room_b for b in inter)
    assert any(b.thickness_class == PLUMBING for b in inter)  # the wet wall


def test_opening_gaps_cover_every_opening():
    plan = compile_source(FIXTURE).plan
    gaps = opening_gaps(plan, 0)
    cats = [g.category for g in gaps]
    # two exterior windows + one exterior entry + one swing door + one cased open
    assert cats.count("window") == 2
    assert cats.count("door") == 3   # entry + living-bath + bath-bed
    assert cats.count("opening") == 1  # the leafless `open bed - util`


def test_room_fills_have_no_boundary_stroke():
    # The single-line-wall look is gone: a room rect is a fill only; the wall
    # bands (drawn over it) supply every wall line.
    svg = render_svg(compile_source(FIXTURE).plan)
    for m in re.finditer(r'<rect [^>]*data-room="[^"]*"[^>]*>', svg):
        assert 'stroke="none"' in m.group(0)


def test_wall_layer_is_a_passive_overlay():
    svg = render_svg(compile_source(FIXTURE).plan)
    m = re.search(r'<g data-layer="walls"[^>]*>', svg)
    assert m and 'pointer-events="none"' in m.group(0)


def test_window_symbol_spans_the_wall_band():
    # A window reads as sill + glazing + head across the band, closed by a jamb
    # line at each end — five window-colour strokes, not the old flat double line.
    src = """\
plan "One window"
envelope 20 x 16
ceiling 9
room a: living at 0,0 size 20 x 16
window a south width 6 offset 7
entry a north width 3 offset 8
"""
    svg = render_svg(compile_source(src).plan)
    from barndsl.render import WINDOW_COLOR

    assert svg.count(f'stroke="{WINDOW_COLOR}"') == 5


# -- walls around unassigned voids ---------------------------------------------
# A room edge is normally on the envelope (exterior shell) or shared with another
# room (interior partition). An edge facing a hole in the tiling got neither, so
# a plan with a void drew its neighbours wall-less and open into the pocket, and
# a void touching the envelope left a gap in the building outline. Both edges are
# real framed construction, so wall_bands now closes them.

# A 6x10 = 60 sq ft unassigned pocket at (12,10)-(18,20): bounded by room a's
# north edge (below), b's east edge (west), c's west edge (east), and the
# envelope's north wall (above, where no room reaches).
VOID_FIXTURE = """\
plan "Void"
envelope 30 x 20
ceiling 9
room a: living  at 0,0   size 30 x 10
room b: bedroom at 0,10  size 12 x 10
room c: kitchen at 18,10 size 12 x 10
door a - b width 3 offset 1
open a - c width 6 offset 1
entry a south width 3 offset 2
window a south width 8 offset 10
window b north width 4 offset 4
window c north width 4 offset 4
"""


def _void_plan():
    result = compile_source(VOID_FIXTURE)
    assert result.plan is not None, [d.code for d in result.errors]
    return result


def test_room_edges_facing_a_void_get_interior_bands():
    bands = wall_bands(_void_plan().plan, 0)
    void_walls = {
        _norm(b.x0, b.y0, b.x1, b.y1)
        for b in bands
        if b.kind == "interior" and b.room_b is None
    }
    from barndsl.wallbodies import THICKNESS

    ph = THICKNESS[INTERIOR] / 2.0
    assert _norm(12 - ph, 10, 12 + ph, 20) in void_walls  # b's east edge
    assert _norm(18 - ph, 10, 18 + ph, 20) in void_walls  # c's west edge
    assert _norm(12, 10 - ph, 18, 10 + ph) in void_walls  # a's north run


def test_envelope_run_over_a_void_gets_a_shell_band():
    bands = wall_bands(_void_plan().plan, 0)
    gap_shell = [
        b for b in bands
        if b.kind == "exterior" and b.room_a == "" and b.wall == "N"
    ]
    assert len(gap_shell) == 1
    b = gap_shell[0]
    # Spans the uncovered x 12..18 run (plus the half-thickness corner overrun).
    assert min(b.x0, b.x1) < 12 < 18 < max(b.x0, b.x1)


def test_void_free_plans_gain_no_extra_bands():
    # The parity FIXTURE tiles its envelope completely: every band is still an
    # exterior shell piece with a host room or a two-flank partition.
    result = compile_source(FIXTURE)
    for b in wall_bands(result.plan, 0):
        if b.kind == "exterior":
            assert b.room_a != ""
        else:
            assert b.room_b is not None


def test_a_room_sized_void_now_fires_area_void():
    # 60 sq ft slid under the original 70 sq ft bar (a live run shipped a 66 sq
    # ft dead pocket); the lowered threshold catches any usable-room's worth.
    result = _void_plan()
    assert "AREA_VOID" in [d.code for d in result.diagnostics]


def test_dxf_walls_match_the_bands_for_a_void_plan():
    # The void walls ride the same shared band geometry as every other wall, so
    # the DXF export carries them too — parity holds for a plan with a hole.
    plan = _void_plan().plan
    band_rects = {_norm(b.x0, b.y0, b.x1, b.y1) for b in wall_bands(plan, 0)}
    assert _dxf_wall_rects(to_dxf(plan)) == band_rects
