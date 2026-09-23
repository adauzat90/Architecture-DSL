"""One drawing geometry for the SVG plan and the DXF export.

:mod:`barndsl.drawing` computes the door and window symbols and the exterior
dimension chains once, in plan feet; the SVG renderer and the DXF export only
style them. These tests read both outputs back and check each one draws that
shared geometry where it says. The DXF used to re-derive the symbols by hand
and, doing so, hinged every single swing door at its near jamb even when the
plan said ``hinge far``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from barndsl import compile_source, render_svg, to_dxf
from barndsl.drawing import door_symbols, exterior_chains, window_symbols
from barndsl.render import RenderConfig, _Renderer

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(p for p in (REPO / "examples").glob("**/*.barn") if "parts" not in p.parts)


def _dxf_entities(dxf: str, kind: str) -> list[dict[str, str]]:
    """The group codes of every ``kind`` entity in a DXF document."""
    lines = dxf.split("\n")
    out: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for code, value in zip(lines[::2], lines[1::2]):
        if code == "0":
            current = {} if value == kind else None
            if current is not None:
                out.append(current)
        elif current is not None:
            current.setdefault(code, value)
    return out


def _pt(x: float, y: float) -> tuple[float, float]:
    return round(x, 3) + 0.0, round(y, 3) + 0.0


def _dxf_arc_ends(dxf: str, layer: str) -> list[tuple]:
    """Each arc's centre, radius and its two end points, from its angles."""
    out = []
    for e in _dxf_entities(dxf, "ARC"):
        if not e["8"].startswith(layer):
            continue
        # A door swing is the quarter arc, never its 270° complement (which has
        # the same two end points).
        assert abs((float(e["51"]) - float(e["50"])) % 360.0 - 90.0) < 1e-6
        cx, cy, r = float(e["10"]), float(e["20"]), float(e["40"])
        ends = sorted(
            _pt(cx + r * math.cos(math.radians(float(e[c]))), cy + r * math.sin(math.radians(float(e[c]))))
            for c in ("50", "51")
        )
        out.append((_pt(cx, cy), round(r, 3), tuple(ends)))
    return sorted(out)


def _dxf_lines(dxf: str, layer: str) -> list[tuple]:
    return sorted(
        tuple(sorted((_pt(float(e["10"]), float(e["20"])), _pt(float(e["11"]), float(e["21"])))))
        for e in _dxf_entities(dxf, "LINE") if e["8"].startswith(layer)
    )


def _svg_levels(plan):
    """``(level, renderer)`` for each plan block the SVG draws, the renderer set
    to that block's screen transform (a single-level plan draws every level)."""
    r = _Renderer(plan, RenderConfig())
    if not r.multi:
        r._block_top = r.top
        yield None, r
        return
    for i, lvl in enumerate(r.levels):
        r._block_top = r._env_top(i)
        yield lvl, r


def _plan(path: Path):
    return compile_source(path.read_text(encoding="utf-8"), base_dir=str(path.parent)).plan


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_both_drawings_put_every_door_leaf_and_window_where_the_shared_geometry_says(path):
    plan = _plan(path)
    dxf = to_dxf(plan)
    svg = render_svg(plan)

    # DXF: each hinged leaf is an arc about its hinge, from its tip to its latch.
    leaves = [leaf for sym in door_symbols(plan) for leaf in sym.leaves]
    assert _dxf_arc_ends(dxf, "A-DOOR") == sorted(
        (_pt(*lf.hinge), round(lf.width, 3), tuple(sorted((_pt(*lf.tip), _pt(*lf.latch)))))
        for lf in leaves
    )

    # SVG: each leaf is a line from hinge to tip plus an arc from tip to latch,
    # at the right screen position on its level's plan block.
    arcs = re.findall(r'd="M ([^"]*) A ([\d.]+) [\d.]+ 0 0 [01] ([^"]*)" fill="none" stroke="#999999"', svg)
    expected_arcs = []
    for level, r in _svg_levels(plan):
        for sym in door_symbols(plan, level):
            for lf in sym.leaves:
                hx, hy = r.sx(lf.hinge[0]), r.sy(lf.hinge[1])
                tx, ty = r.sx(lf.tip[0]), r.sy(lf.tip[1])
                lx, ly = r.sx(lf.latch[0]), r.sy(lf.latch[1])
                assert f'x1="{hx:.1f}" y1="{hy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}"' in svg
                expected_arcs.append((f"{tx:.1f} {ty:.1f}", f"{lf.width * r.c.scale:.1f}", f"{lx:.1f} {ly:.1f}"))
    assert sorted(arcs) == sorted(expected_arcs)

    # Each window is its two band faces, glazing line and two jambs.
    expected = sorted(
        tuple(sorted((_pt(*a), _pt(*b))))
        for sym in window_symbols(plan)
        for a, b in (*sym.faces, sym.glazing, *sym.jambs)
    )
    assert _dxf_lines(dxf, "A-GLAZ") == expected


LEVELS = """\
plan "Levels"
envelope 20 x 10
ceiling 9
room a: living  at 0,0  size 10 x 10
room b: kitchen at 10,0 size 10 x 10
room c: office  at 0,0  size 10 x 10 level 1
room d: bedroom at 10,0 size 10 x 10 level 1
door a - b width 3
door c - d width 3 offset 1
door a - c width 3
entry a south width 3 offset 2
window a west width 3 offset 2
window d north width 3 offset 2
"""


def test_symbols_split_by_level_and_a_cross_level_door_draws_no_leaf():
    plan = compile_source(LEVELS).plan
    ground, upper = door_symbols(plan, 0), door_symbols(plan, 1)
    # The a–b door and the entry on the ground floor, the c–d door upstairs; the
    # a–c "door" joins two levels (a stair's job) and is drawn on neither.
    assert len(ground) == 2 and len(upper) == 1
    assert upper[0].jambs == ((10.0, 1.0), (10.0, 4.0))
    assert len(door_symbols(plan)) == 3
    assert [len(window_symbols(plan, lvl)) for lvl in (0, 1)] == [1, 1]


HINGE = """\
plan "Hinge"
envelope 22 x 10
ceiling 9
room a: living  at 0,0  size 12 x 10
room b: bedroom at 12,0 size 10 x 10
door a - b width 3 offset 2 into b hinge far
entry a south width 3 offset 2
"""


def test_the_dxf_hinges_a_single_swing_door_at_the_far_jamb():
    plan = compile_source(HINGE).plan
    (sym,) = [s for s in door_symbols(plan) if s.orientation == "v"]
    (leaf,) = sym.leaves
    assert leaf.hinge == (12.0, 5.0)  # the far (north) jamb of the 2–5 span
    assert leaf.tip == (15.0, 5.0)  # open into b, east of the wall
    arcs = _dxf_arc_ends(to_dxf(plan), "A-DOOR")
    assert ((12.0, 5.0), 3.0, ((12.0, 2.0), (15.0, 5.0))) in arcs


SYMBOLS = """\
plan "Symbols"
envelope 30 x 24
ceiling 9
room a: living  at 0,0   size 16 x 12
room b: garage  at 16,0  size 14 x 12
room c: bedroom at 0,12  size 16 x 12
room d: office  at 16,12 size 14 x 12
door a - c pocket width 3 offset 2
door c - d bifold width 4 offset 1
door a - b cased width 5
door b south overhead width 9 offset 2
door d north overhead width 8 offset 3
entry c west double width 5 offset 3
"""


def test_each_door_kind_gets_its_own_symbol_geometry():
    syms = door_symbols(compile_source(SYMBOLS).plan)
    by_kind: dict[str, list] = {}
    for s in syms:
        by_kind.setdefault(s.kind, []).append(s)

    # A pocket panel runs along the opening just off the wall (a–c wall at y=12).
    (slide,) = by_kind["slide"]
    assert slide.line == ((2.0, 12.35), (5.0, 12.35))

    # A bifold zigzags across its opening (c–d wall at x=16, y 13→17).
    (bifold,) = by_kind["bifold"]
    assert bifold.zigzag[0] == (16.0, 13.0) and bifold.zigzag[-1] == (16.0, 17.0)
    assert len(bifold.zigzag) == 5

    # A cased opening is only its jambs.
    (cased,) = by_kind["cased"]
    assert not cased.leaves and cased.line is None

    # An overhead door's track sits just inside its own room on either wall.
    south, north = sorted(by_kind["overhead"], key=lambda s: s.jambs[0][1])
    assert south.line == ((18.0, 0.5), (27.0, 0.5))  # garage is north of y=0
    assert north.line == ((19.0, 23.5), (27.0, 23.5))  # office is south of y=24

    # A double entry is two half leaves hinged at opposite jambs.
    (double,) = [s for s in by_kind["swing"] if len(s.leaves) == 2]
    near, far = double.leaves
    assert (near.hinge, far.hinge) == ((0.0, 15.0), (0.0, 20.0))
    assert near.width == far.width == 2.5


@pytest.mark.parametrize("dim_mode", ["nominal", "faces"])
def test_both_drawings_tick_every_chain_at_the_shared_coordinates(dim_mode):
    # The lshape wing plan chains several notched runs at their own offsets.
    plan = _plan(REPO / "examples" / "gallery" / "lshape.barn")
    chains = exterior_chains(plan, plan.rooms, 0, dim_mode)
    assert {c.side for c in chains} == {"S", "N", "W", "E"}

    # DXF: a 45° tick centred on each tick coordinate, on the chain row 1.5 ft
    # outside its wall.
    ticks = {
        _pt((float(e["10"]) + float(e["11"])) / 2, (float(e["20"]) + float(e["21"])) / 2)
        for e in _dxf_entities(to_dxf(plan, dim_mode=dim_mode), "LINE")
        if e["8"] == "A-ANNO-DIMS" and abs(abs(float(e["11"]) - float(e["10"])) - 0.36) < 1e-6
    }
    # SVG: a short tick across the chain row, 15 px outside its wall.
    svg = render_svg(plan, RenderConfig(dim_mode=dim_mode))
    (_, r), = _svg_levels(plan)
    off, tk = r._CHAIN_OFFSET, r._CHAIN_TICK
    for chain in chains:
        out = -1.0 if chain.side in ("S", "W") else 1.0
        row = chain.wall + out * 1.5
        for p in chain.ticks:
            if chain.side in ("S", "N"):
                assert _pt(p, row) in ticks, (chain.side, p)
                x, y = r.sx(p), r.sy(chain.wall) - out * off
                assert f'x1="{x:.1f}" y1="{y - tk:.1f}" x2="{x:.1f}" y2="{y + tk:.1f}"' in svg
            else:
                assert _pt(row, p) in ticks, (chain.side, p)
                x, y = r.sx(chain.wall) + out * off, r.sy(p)
                assert f'x1="{x - tk:.1f}" y1="{y:.1f}" x2="{x + tk:.1f}" y2="{y:.1f}"' in svg
