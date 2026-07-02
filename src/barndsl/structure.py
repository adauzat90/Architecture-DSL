"""Automatic post-and-beam frame placement.

A barndominium is a post-and-beam metal building: a row of **bents** (frames)
spaced along the building's long axis, each a beam/truss spanning the width
between the two **eave** (long) walls, carried by a **post** at each end. A
**ridge** member runs the length over the frames. When the width exceeds a beam's
practical clear span, an **interior** post line splits it.

:func:`place_frame` derives that skeleton from a plan's footprint — deterministic
and pure-Python, the same brief-in/plan-out contract as the auto-layout engines.
It populates ``plan.posts`` and ``plan.beams``; the renderer draws them and the
validator nudges where an interior post lands awkwardly.

These are **layout aids, not an engineered design** — member sizing, connections,
foundations and lateral bracing are the structural engineer's job.
"""

from __future__ import annotations

import math

from .elements import Barndominium, Beam, FrameSpec, Post
from .geometry import SharedEdge, shared_edge

#: Coordinates snap to this grid (ft) so repeated placement is byte-stable and
#: posts that land on the same line dedupe cleanly.
_SNAP = 1e-4


def _snap(v: float) -> float:
    return round(v / _SNAP) * _SNAP


def _even_cuts(start: float, length: float, max_step: float) -> list[float]:
    """Positions from ``start`` to ``start+length`` in equal steps ≤ ``max_step``.

    Always includes both ends. ``n = ceil(length / max_step)`` segments, so the
    common case (a span already under the limit) yields just the two ends.
    """
    n = max(1, math.ceil(length / max_step - 1e-9))
    return [_snap(start + i * length / n) for i in range(n + 1)]


def _section_aligned(sec: tuple[float, float, float, float], edge: SharedEdge) -> bool:
    """Does ``edge`` run along ``sec``'s long axis (the direction interior
    post lines run)? Post lines split the bents' span, so they run along the
    long axis: east-west (``"h"``) when the block is wider than long, else
    north-south (``"v"``)."""
    _, _, w, l = sec
    return edge.orientation == ("h" if w >= l else "v")


def _edge_in_section(
    sec: tuple[float, float, float, float], edge: SharedEdge, tol: float = 1e-6
) -> bool:
    """Is ``edge``'s midpoint inside (or on the boundary of) ``sec``?"""
    x, y, w, l = sec
    mx, my = (edge.pos, edge.mid) if edge.orientation == "v" else (edge.mid, edge.pos)
    return x - tol <= mx <= x + w + tol and y - tol <= my <= y + l + tol


def bearing_wall_usage(plan: Barndominium) -> list[tuple[object, SharedEdge, bool]]:
    """Each **valid** declared bearing wall and whether the frame can use it.

    Returns ``(spec, edge, usable)`` per ground-level ``wall ... bearing``
    declaration whose rooms exist and share a wall. ``usable`` is True when the
    wall runs along the long axis of the footprint block it sits in — the
    direction an interior post line runs — so :func:`place_frame` drops interior
    posts onto it at each bent. A bearing wall parallel to the bents' span can't
    split that span; the validator turns it into a ``WALL_BEARING_AXIS`` info
    rather than letting the declaration silently do nothing.
    """
    out: list[tuple[object, SharedEdge, bool]] = []
    sections = plan.footprint_sections()
    for ws in getattr(plan, "wall_specs", None) or []:
        if "bearing" not in ws.attributes:
            continue
        a, b = plan.room(ws.room_a), plan.room(ws.room_b)
        if a is None or b is None:
            continue  # WALL_REF's problem, not the frame's
        if getattr(a, "level", 0) != 0 or getattr(b, "level", 0) != 0:
            continue  # the frame stands on the ground level
        edge = shared_edge(a, b)
        if edge is None:
            continue  # WALL_NOADJ's problem
        sec = next((s for s in sections if _edge_in_section(s, edge)), None)
        usable = sec is not None and _section_aligned(sec, edge)
        out.append((ws, edge, usable))
    return out


def _frame_section(
    plan: Barndominium,
    sec: tuple[float, float, float, float],
    spec: FrameSpec,
    bearing_edges: list[SharedEdge] | None = None,
) -> None:
    """Place posts and beams for one rectangular footprint block."""
    x, y, w, l = sec
    if w <= 0 or l <= 0:
        return

    # The ridge runs along the longer side; bents span the shorter side and are
    # spaced along the longer one. Ties (a square block) default to an E-W ridge.
    if w >= l:
        long0, long_len, short0, short_len, vertical_frames = x, w, y, l, True
    else:
        long0, long_len, short0, short_len, vertical_frames = y, l, x, w, False

    frame_lines = _even_cuts(long0, long_len, spec.bay)       # along the long axis
    interior_lines = _even_cuts(short0, short_len, spec.span)  # across the short axis
    interior = interior_lines[1:-1]  # drop the two eave walls; what's left is support
    short_lo, short_hi = short0, _snap(short0 + short_len)
    # Gable (end-wall) posts: columns up the two end frames at bay spacing.
    gable_posts = _even_cuts(short0, short_len, spec.bay)

    def add_post(lp: float, sp: float, role: str) -> None:
        px, py = (lp, sp) if vertical_frames else (sp, lp)
        plan.posts.append(Post(_snap(px), _snap(py), spec.post, role, level=0))

    def add_beam(lp: float, a: float, b: float, role: str) -> None:
        if vertical_frames:  # frame is a vertical line at x=lp spanning y
            plan.beams.append(Beam(_snap(lp), _snap(a), _snap(lp), _snap(b), role, 0))
        else:  # frame is a horizontal line at y=lp spanning x
            plan.beams.append(Beam(_snap(a), _snap(lp), _snap(b), _snap(lp), role, 0))

    for i, lp in enumerate(frame_lines):
        add_beam(lp, short_lo, short_hi, "frame")
        is_end = i in (0, len(frame_lines) - 1)
        if is_end:  # gable wall: a column at each bay across the end frame
            for sp in gable_posts:
                add_post(lp, sp, "post")
        else:  # interior frame: eave posts at the two ends only
            add_post(lp, short_lo, "post")
            add_post(lp, short_hi, "post")
        for sp in interior:  # interior support columns under the long beam
            add_post(lp, sp, "interior")
        # A declared interior bearing wall is an authored post line: drop an
        # interior post onto it at every bent that crosses its run, so the beam
        # bears on the wall instead of clear-spanning over it. (Coincident posts
        # dedupe with the auto interior lines.)
        for edge in bearing_edges or ():
            if edge.lo - 1e-6 <= lp <= edge.hi + 1e-6:
                add_post(lp, edge.pos, "interior")

    if spec.ridge:
        # The ridge runs *along* the long axis (parallel to it), centred on the
        # short axis — the opposite orientation to a frame.
        mid = _snap(short0 + short_len / 2.0)
        a, b = _snap(long0), _snap(long0 + long_len)
        if vertical_frames:  # long axis = x → ridge horizontal at y = mid
            plan.beams.append(Beam(a, mid, b, mid, "ridge", 0))
        else:  # long axis = y → ridge vertical at x = mid
            plan.beams.append(Beam(mid, a, mid, b, "ridge", 0))


def _dedupe_posts(plan: Barndominium) -> None:
    """Drop coincident posts (shared corners between frames/sections).

    A perimeter post takes precedence over an interior one at the same point.
    """
    rank = {"post": 0, "interior": 1}
    best: dict[tuple[float, float], Post] = {}
    for p in plan.posts:
        key = (round(p.x, 3), round(p.y, 3))
        cur = best.get(key)
        if cur is None or rank.get(p.role, 9) < rank.get(cur.role, 9):
            best[key] = p
    plan.posts = sorted(best.values(), key=lambda p: (p.x, p.y))


def place_frame(plan: Barndominium, spec: FrameSpec | None = None) -> None:
    """Populate ``plan.posts``/``plan.beams`` from ``spec`` (or ``plan.frame_spec``).

    Idempotent: clears any previously placed structure and rebuilds it, so calling
    it twice (e.g. builder then compile) yields the same result. Does nothing if no
    spec is set or the footprint has no positive area.

    A declared interior bearing wall (``wall a - b bearing``) that runs along its
    footprint block's long axis is honoured as an interior **post line**: an
    interior post lands on it at every bent crossing its run (see
    :func:`bearing_wall_usage`).
    """
    spec = spec or plan.frame_spec
    if spec is None:
        return
    plan.frame_spec = spec
    plan.posts = []
    plan.beams = []
    sections = plan.footprint_sections()
    aligned: dict[tuple[float, float, float, float], list[SharedEdge]] = {}
    for _ws, edge, ok in bearing_wall_usage(plan):
        if not ok:
            continue
        sec = next(s for s in sections if _edge_in_section(s, edge))
        aligned.setdefault(sec, []).append(edge)
    for sec in sections:
        _frame_section(plan, sec, spec, bearing_edges=aligned.get(sec))
    _dedupe_posts(plan)
