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
