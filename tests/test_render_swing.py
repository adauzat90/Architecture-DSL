"""The door-swing arc must pivot about the hinge (always convex), not flip to a
concave bulge for half the orientation/hinge/side combinations."""

from __future__ import annotations

import math
import re

from barndsl import compile_source, render_svg
from barndsl.render import _Renderer


def _svg_center(p0, p1, r, fs):
    """SVG endpoint->centre for rx=ry=r, large-arc 0 (independent reference)."""
    (x1, y1), (x2, y2) = p0, p1
    dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    denom = dx * dx + dy * dy
    coef = math.sqrt(max(0.0, (r * r) / denom - 1.0))
    sign = 1.0 if fs != 0 else -1.0
    return (sign * coef * dy + (x1 + x2) / 2.0, sign * coef * (-dx) + (y1 + y2) / 2.0)


def test_arc_sweep_always_centres_on_the_hinge():
    r = 10.0
    # A door leaf: hinge at H, latch r away along one axis, tip r away along the
    # perpendicular axis. Enumerate every quadrant pairing (8 combos).
    H = (0.0, 0.0)
    axes = [(r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r)]
    for lx, ly in axes:
        latch = (lx, ly)
        for tx, ty in axes:
            if abs(tx) == abs(lx) and abs(ty) == abs(ly):
                continue  # tip must be perpendicular to latch
            tip = (tx, ty)
            fs = _Renderer._arc_sweep(H, tip, latch, r)
            cx, cy = _svg_center(tip, latch, r, fs)
            assert abs(cx - H[0]) < 1e-6 and abs(cy - H[1]) < 1e-6, (latch, tip, fs)


def _arc_flags(svg: str) -> list[str]:
    # Match the swing-arc paths: "A r r 0 0 <sweep> lx ly".
    return re.findall(r"A [\d.]+ [\d.]+ 0 0 ([01]) ", svg)


def test_gallery_swings_use_both_sweep_flags():
    # The bug hardcoded sweep-flag 1; a correct renderer needs both across the
    # gallery's mix of door orientations and hinge sides.
    flags: set[str] = set()
    for name in ("cottage", "hall_spine", "lshape", "two_story"):
        with open(f"examples/gallery/{name}.barn", encoding="utf-8") as fh:
            flags.update(_arc_flags(render_svg(compile_source(fh.read()).plan)))
    assert flags == {"0", "1"}
