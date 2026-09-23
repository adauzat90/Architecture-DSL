"""World-coordinate drawing geometry shared by the SVG plan and the DXF export.

The floor plan is drawn twice — :mod:`barndsl.render` as SVG and
:mod:`barndsl.dxf` as CAD entities — and both must put every door leaf, window
line and dimension tick in the same place. This module computes that geometry
once, in plan feet (``x`` east, ``y`` north). Each renderer only translates it
into its own vocabulary (SVG paths in screen pixels, DXF entities in model space)
and keeps its own styling and draw order. The validator's swing checks build on
the same leaf geometry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .constants import EXTERIOR_WALL_THICKNESS, INTERIOR_WALL_THICKNESS
from .elements import (
    DOUBLE_LEAF_KINDS,
    Barndominium,
    Direction,
    ExteriorDoor,
    InteriorDoor,
    Room,
)
from .geometry import (
    SharedEdge,
    door_span,
    footprint_boundary,
    opening_endpoints,
    point_in_footprint,
    shared_edge,
)
from .wallbodies import WallBand, wall_bands

Point = tuple[float, float]
Segment = tuple[Point, Point]

# --- door symbols ------------------------------------------------------------

#: How far (ft) a pocket/sliding panel sits off the wall line.
SLIDE_OFFSET = 0.35
#: How far (ft) an overhead door's track line sits inside the room.
OVERHEAD_TRACK = 0.5


@dataclass(frozen=True)
class Leaf:
    """One hinged door leaf, drawn open: the leaf runs from ``hinge`` to
    ``tip`` and its 90° swing arc pivots about ``hinge`` from ``tip`` to
    ``latch`` (radius ``width``)."""

    hinge: Point
    latch: Point
    tip: Point
    width: float


@dataclass(frozen=True)
class DoorSymbol:
    """The plan symbol of one door.

    ``kind`` is how it is drawn: ``"swing"`` (one or two hinged ``leaves``),
    ``"slide"`` (a pocket/sliding panel ``line`` just off the wall),
    ``"bifold"`` (the folded-panel ``zigzag``), ``"overhead"`` (a garage door's
    track ``line`` just inside the room) or ``"cased"`` (a bare opening).
    ``jambs`` are the opening's two ends on the wall line and ``orientation``
    the wall's axis (``"v"`` runs in ``+y``, ``"h"`` in ``+x``).
    """

    kind: str
    orientation: str
    jambs: Segment
    leaves: tuple[Leaf, ...] = ()
    line: Segment | None = None
    zigzag: tuple[Point, ...] = ()


def swing_bounds(plan: Barndominium) -> Point:
    """The ``(max_x, max_y)`` a leaf with no swing side stays within: the plan
    bounds widened by any porch outside the envelope (the drawing's extent)."""
    _, _, fx1, fy1 = plan.bounds()
    xs = [fx1] + [p.x + p.width for p in plan.porches]
    ys = [fy1] + [p.y + p.length for p in plan.porches]
    return max(xs), max(ys)


def swing_side(door: InteriorDoor, a: Room, b: Room, edge: SharedEdge) -> float | None:
    """``+1``/``-1`` for the side of the shared wall an interior door swings
    into (toward ``+x``/``+y`` is ``+1``), or ``None`` when it names no
    ``into`` room."""
    into = door.swing_into
    room = a if (into and into == a.id) else (b if (into and into == b.id) else None)
    if room is None:
        return None
    cx, cy = room.center
    return 1.0 if (cx if edge.orientation == "v" else cy) > edge.pos else -1.0


def door_leaf(
    ox: float, oy: float, orientation: str, width: float,
    sgn: float | None, hinge_far: bool, bounds: Point,
) -> Leaf:
    """The open leaf of a door whose opening starts at ``(ox, oy)``.

    ``sgn`` is the side it swings into. ``None`` swings toward ``+x``/``+y``
    unless the leaf would pass ``bounds`` (``max_x``, ``max_y``), then the other
    way. ``hinge_far`` hinges it at the high-coordinate jamb instead of the low.
    """
    max_x, max_y = bounds
    if orientation == "v":
        if sgn is None:
            sgn = 1.0 if (ox + width) <= max_x else -1.0
        hinge = (ox, oy + width) if hinge_far else (ox, oy)
        latch = (ox, oy) if hinge_far else (ox, oy + width)
        tip = (ox + sgn * width, hinge[1])
    else:
        if sgn is None:
            sgn = 1.0 if (oy + width) <= max_y else -1.0
        hinge = (ox + width, oy) if hinge_far else (ox, oy)
        latch = (ox, oy) if hinge_far else (ox + width, oy)
        tip = (hinge[0], oy + sgn * width)
    return Leaf(hinge, latch, tip, width)


def door_symbols(plan: Barndominium, level: int | None = None) -> list[DoorSymbol]:
    """Every door's plan symbol on ``level`` (all levels when ``None``):
    interior doors in plan order, then exterior doors."""
    bounds = swing_bounds(plan)
    out: list[DoorSymbol] = []
    for door in plan.interior_doors:
        a, b = plan.room(door.room_a), plan.room(door.room_b)
        if a is None or b is None:
            continue
        if level is not None and not (a.level == b.level == level):
            continue  # a cross-level door is shown by its stair
        edge = shared_edge(a, b)
        if edge is not None:
            out.append(_interior_symbol(door, a, b, edge, bounds))
    for xd in plan.exterior_doors:
        room = plan.room(xd.room)
        if room is None or (level is not None and room.level != level):
            continue
        out.append(_exterior_symbol(room, xd, bounds))
    return out


def _interior_symbol(
    door: InteriorDoor, a: Room, b: Room, edge: SharedEdge, bounds: Point,
) -> DoorSymbol:
    start, end = door_span(edge, door)
    w = end - start
    o = edge.orientation
    ox, oy = (edge.pos, start) if o == "v" else (start, edge.pos)
    jambs = _jambs(ox, oy, o, w)
    if door.kind == "swing":
        sgn = swing_side(door, a, b, edge)
        leaf = door_leaf(ox, oy, o, w, sgn, door.hinge == "far", bounds)
        return DoorSymbol("swing", o, jambs, leaves=(leaf,))
    if door.kind in DOUBLE_LEAF_KINDS:
        # Two half-width leaves hinged at opposite jambs, meeting in the middle.
        return DoorSymbol("swing", o, jambs, leaves=_pair(ox, oy, o, w, swing_side(door, a, b, edge), bounds))
    if door.kind in ("pocket", "sliding"):
        s = _keep_in(ox, oy, o, SLIDE_OFFSET, bounds)
        return DoorSymbol("slide", o, jambs, line=_along(ox, oy, o, w, s))
    if door.kind == "bifold":
        return DoorSymbol("bifold", o, jambs, zigzag=_bifold_zigzag(ox, oy, o, w, bounds))
    return DoorSymbol("cased", o, jambs)


def _exterior_symbol(room: Room, xd: ExteriorDoor, bounds: Point) -> DoorSymbol:
    x1, y1, x2, y2 = opening_endpoints(room, xd.wall, xd.offset, xd.width)
    o = "h" if xd.wall in (Direction.NORTH, Direction.SOUTH) else "v"
    ox, oy = (min(x1, x2), y1) if o == "h" else (x1, min(y1, y2))
    w = xd.width
    jambs = _jambs(ox, oy, o, w)
    if xd.kind == "overhead":
        # The sectional panel rides its tracks just inside the room.
        into = 1.0 if xd.wall in (Direction.SOUTH, Direction.WEST) else -1.0
        return DoorSymbol("overhead", o, jambs, line=_along(ox, oy, o, w, OVERHEAD_TRACK * into))
    if xd.kind in DOUBLE_LEAF_KINDS:
        return DoorSymbol("swing", o, jambs, leaves=_pair(ox, oy, o, w, None, bounds))
    return DoorSymbol("swing", o, jambs, leaves=(door_leaf(ox, oy, o, w, None, False, bounds),))


def _pair(ox: float, oy: float, o: str, w: float, sgn: float | None, bounds: Point) -> tuple[Leaf, Leaf]:
    half = w / 2.0
    nx, ny = (ox, oy + half) if o == "v" else (ox + half, oy)
    return (door_leaf(ox, oy, o, half, sgn, False, bounds),
            door_leaf(nx, ny, o, half, sgn, True, bounds))


def _jambs(ox: float, oy: float, o: str, w: float) -> Segment:
    """The opening's two ends on the wall line."""
    return ((ox, oy), (ox, oy + w)) if o == "v" else ((ox, oy), (ox + w, oy))


def _along(ox: float, oy: float, o: str, w: float, off: float) -> Segment:
    """The opening's run from ``(ox, oy)``, shifted ``off`` ft off the wall."""
    if o == "v":
        return (ox + off, oy), (ox + off, oy + w)
    return (ox, oy + off), (ox + w, oy + off)


def _keep_in(ox: float, oy: float, o: str, d: float, bounds: Point) -> float:
    """``d`` toward ``+x``/``+y``, or ``-d`` when that would pass ``bounds``."""
    if o == "v":
        return d if (ox + d) <= bounds[0] else -d
    return d if (oy + d) <= bounds[1] else -d


def _bifold_zigzag(ox: float, oy: float, o: str, w: float, bounds: Point) -> tuple[Point, ...]:
    """Two half-open panel pairs, each a shallow V with its apex just off the
    wall — the plan zigzag of a bifold folded against its jambs."""
    s = _keep_in(ox, oy, o, min(w / 4.0, 1.0), bounds)
    if o == "v":
        return ((ox, oy), (ox + s, oy + w / 4), (ox, oy + w / 2),
                (ox + s, oy + 3 * w / 4), (ox, oy + w))
    return ((ox, oy), (ox + w / 4, oy + s), (ox + w / 2, oy),
            (ox + 3 * w / 4, oy + s), (ox + w, oy))


# --- window symbols ----------------------------------------------------------


@dataclass(frozen=True)
class WindowSymbol:
    """A window spanning the exterior wall band: its two band ``faces`` (low
    coordinate first), the ``glazing`` line down the band centre and a ``jamb``
    across the band at each end."""

    faces: tuple[Segment, Segment]
    glazing: Segment
    jambs: tuple[Segment, Segment]


def window_symbols(plan: Barndominium, level: int | None = None) -> list[WindowSymbol]:
    """Every window's plan symbol on ``level`` (all levels when ``None``)."""
    half = EXTERIOR_WALL_THICKNESS / 2.0
    out: list[WindowSymbol] = []
    for win in plan.windows:
        room = plan.room(win.room)
        if room is None or (level is not None and room.level != level):
            continue
        x1, y1, x2, y2 = opening_endpoints(room, win.wall, win.offset, win.width)
        if win.wall in (Direction.NORTH, Direction.SOUTH):
            c, lo, hi = y1, min(x1, x2), max(x1, x2)
            sym = WindowSymbol(
                faces=(((lo, c - half), (hi, c - half)), ((lo, c + half), (hi, c + half))),
                glazing=((lo, c), (hi, c)),
                jambs=(((lo, c - half), (lo, c + half)), ((hi, c - half), (hi, c + half))),
            )
        else:
            c, lo, hi = x1, min(y1, y2), max(y1, y2)
            sym = WindowSymbol(
                faces=(((c - half, lo), (c - half, hi)), ((c + half, lo), (c + half, hi))),
                glazing=((c, lo), (c, hi)),
                jambs=(((c - half, lo), (c + half, lo)), ((c - half, hi), (c + half, hi))),
            )
        out.append(sym)
    return out


# --- exterior dimension chains -------------------------------------------------

#: Sides in the order every chain listing uses.
SIDES = ("S", "N", "W", "E")
#: A jamb break is only worth a tick when it leaves a segment at least this
#: wide (ft) on either side — a jamb hard against a room corner (or another
#: jamb) collapses into its neighbour rather than crowd the chain with a sliver
#: too narrow to label.
MIN_JAMB_SEG_FT = 1.0


@dataclass(frozen=True)
class DimChain:
    """One chained dimension string along an exterior wall run: its ``side``
    (``S``/``N``/``W``/``E``), the ``wall`` coordinate it runs along (``y`` for
    S/N, ``x`` for W/E) and its tick coordinates on the chain axis."""

    side: str
    wall: float
    ticks: tuple[float, ...]


def overall_span(lo: float, hi: float, dim_mode: str = "nominal") -> tuple[float, float]:
    """The overall-dimension endpoints for ``dim_mode``. Nominal keeps the
    bounds; ``"faces"`` pushes each end out by half an exterior wall, so the
    string reads outside face to outside face like the drawn poché."""
    if dim_mode == "faces":
        ext = EXTERIOR_WALL_THICKNESS / 2.0
        return lo - ext, hi + ext
    return lo, hi


def exterior_chains(
    plan: Barndominium, rooms: Sequence[Room], level: int = 0, dim_mode: str = "nominal",
) -> list[DimChain]:
    """The chained dimension strings along the exterior of ``rooms`` on ``level``.

    ``rooms`` are the rooms drawn on ``level`` (every room, for a single-level
    drawing); ``level`` picks the wall bodies a ``"faces"`` chain reads its wall
    thicknesses from. Each side breaks at the room edges that meet its wall and at the jambs of
    its exterior openings, so it reads wall segment / opening width / wall
    segment. A side with no break at all is left to the overall dimension. A
    wing (L/T/U) plan chains each notched run at its own wall offset.
    """
    bands = wall_bands(plan, level) if dim_mode == "faces" else []
    chains: list[DimChain] = []
    if plan.wings:
        for side in SIDES:
            for offset, lo, hi in exterior_runs(plan, side):
                room_pts = run_breaks(side, rooms, offset, lo, hi)
                jambs = opening_jambs(plan, side, rooms, offset, lo, hi)
                ticks = chain_ticks(side, room_pts, jambs, lo, hi, dim_mode, bands)
                if len(ticks) > 2:
                    chains.append(DimChain(side, offset, tuple(ticks)))
        return chains
    fx0, fy0, fx1, fy1 = plan.bounds()
    for side in SIDES:
        room_pts, lo, hi = chain_breaks(side, rooms, fx0, fy0, fx1, fy1)
        wall = {"S": fy0, "N": fy1, "W": fx0, "E": fx1}[side]
        jambs = opening_jambs(plan, side, rooms, wall, lo, hi)
        ticks = chain_ticks(side, room_pts, jambs, lo, hi, dim_mode, bands)
        if len(ticks) > 2:
            chains.append(DimChain(side, wall, tuple(ticks)))
    return chains


def opening_jambs(
    plan: Barndominium, side: str, rooms: Sequence[Room], offset: float, lo: float, hi: float,
    tol: float = 1e-6,
) -> list[float]:
    """Near/far jamb coordinates of the exterior openings on one run.

    A window or exterior door on ``side`` whose room's matching wall lies on
    the run ``offset`` contributes its two jambs, projected onto the chain axis
    and clamped to ``[lo, hi]``."""
    want = {
        "S": Direction.SOUTH, "N": Direction.NORTH,
        "W": Direction.WEST, "E": Direction.EAST,
    }[side]
    room_by_id = {r.id: r for r in rooms}
    coords: list[float] = []
    openings = [(w.room, w.wall, w.offset, w.width) for w in plan.windows]
    openings += [(d.room, d.wall, d.offset, d.width) for d in plan.exterior_doors]
    for rid, wall, off, width in openings:
        if wall != want:
            continue
        room = room_by_id.get(rid)
        if room is None:
            continue
        edge_coord = {"S": room.y, "N": room.y2, "W": room.x, "E": room.x2}[side]
        if abs(edge_coord - offset) > tol:
            continue
        x1, y1, x2, y2 = opening_endpoints(room, wall, off, width)
        near, far = (x1, x2) if side in ("S", "N") else (y1, y2)
        for c in (near, far):
            if lo - tol <= c <= hi + tol:
                coords.append(min(max(c, lo), hi))
    return coords


def with_jambs(pts: list[float], jambs: list[float]) -> list[float]:
    """Fold opening ``jambs`` into the room-edge breaks ``pts``, dropping any
    jamb that would leave a segment narrower than :data:`MIN_JAMB_SEG_FT`."""
    out = list(pts)
    for j in sorted(jambs):
        if all(abs(j - p) >= MIN_JAMB_SEG_FT for p in out):
            out.append(j)
    out.sort()
    return out


def chain_breaks(
    side: str, rooms: Sequence[Room], fx0: float, fy0: float, fx1: float, fy1: float,
    tol: float = 1e-6,
) -> tuple[list[float], float, float]:
    """Break points partitioning one exterior ``side`` of a plain rectangle.

    The edges of rooms *touching* that wall, projected onto it, plus the span
    ends — sorted, clamped and deduped. Returns ``(points, span_lo, span_hi)``.
    """
    if side in ("S", "N"):
        lo, hi = fx0, fx1
        if side == "S":
            touch = [r for r in rooms if abs(r.y - fy0) <= tol]
        else:
            touch = [r for r in rooms if abs(r.y2 - fy1) <= tol]
        raw = [c for r in touch for c in (r.x, r.x2)]
    else:
        lo, hi = fy0, fy1
        if side == "W":
            touch = [r for r in rooms if abs(r.x - fx0) <= tol]
        else:
            touch = [r for r in rooms if abs(r.x2 - fx1) <= tol]
        raw = [c for r in touch for c in (r.y, r.y2)]
    return _breaks(lo, hi, raw, tol), lo, hi


def run_breaks(
    side: str, rooms: Sequence[Room], offset: float, lo: float, hi: float,
    tol: float = 1e-6,
) -> list[float]:
    """Break points partitioning one exterior run (``offset``, span ``[lo, hi]``).

    Like :func:`chain_breaks` but keyed to one wall run: a room counts only when
    its wall lies on ``offset`` *and* its extent overlaps ``[lo, hi]``, so a
    wing's north run collects only the rooms backing that wing."""
    if side in ("S", "N"):
        edge = (lambda r: r.y) if side == "S" else (lambda r: r.y2)
        touch = [
            r for r in rooms
            if abs(edge(r) - offset) <= tol and min(r.x2, hi) - max(r.x, lo) > tol
        ]
        raw = [c for r in touch for c in (r.x, r.x2)]
    else:
        edge = (lambda r: r.x) if side == "W" else (lambda r: r.x2)
        touch = [
            r for r in rooms
            if abs(edge(r) - offset) <= tol and min(r.y2, hi) - max(r.y, lo) > tol
        ]
        raw = [c for r in touch for c in (r.y, r.y2)]
    return _breaks(lo, hi, raw, tol)


def _breaks(lo: float, hi: float, raw: list[float], tol: float) -> list[float]:
    pts: list[float] = []
    for c in sorted([lo, hi, *raw]):
        c = min(max(c, lo), hi)
        if not pts or c - pts[-1] > tol:
            pts.append(c)
    return pts


def exterior_runs(plan: Barndominium, side: str) -> list[tuple[float, float, float]]:
    """Distinct colinear exterior wall runs facing ``side`` (``S``/``N``/``W``/``E``).

    Each run is ``(offset, lo, hi)``: its wall coordinate (``y`` for S/N, ``x``
    for W/E) and the span it covers on the other axis. A plain rectangle has one
    run per side; an L/T/U footprint has one per notched face.
    """
    sections = plan.footprint_sections()
    eps = 1e-3
    intervals: dict[float, list[tuple[float, float]]] = {}
    for (x1, y1), (x2, y2) in footprint_boundary(sections):
        if side in ("S", "N"):
            if abs(y1 - y2) > 1e-9:
                continue  # want a horizontal edge
            offset = y1
            mid = (x1 + x2) / 2.0
            inside_hi = point_in_footprint(sections, mid, offset + eps)
            inside_lo = point_in_footprint(sections, mid, offset - eps)
            faces = (
                "S" if inside_hi and not inside_lo
                else "N" if inside_lo and not inside_hi else None
            )
            a, b = sorted((x1, x2))
        else:
            if abs(x1 - x2) > 1e-9:
                continue  # want a vertical edge
            offset = x1
            mid = (y1 + y2) / 2.0
            inside_hi = point_in_footprint(sections, offset + eps, mid)
            inside_lo = point_in_footprint(sections, offset - eps, mid)
            faces = (
                "W" if inside_hi and not inside_lo
                else "E" if inside_lo and not inside_hi else None
            )
            a, b = sorted((y1, y2))
        if faces != side:
            continue
        intervals.setdefault(offset, []).append((a, b))
    runs: list[tuple[float, float, float]] = []
    for offset, ivs in intervals.items():
        ivs.sort()
        cur_lo, cur_hi = ivs[0]
        for lo, hi in ivs[1:]:
            if lo <= cur_hi + 1e-9:
                cur_hi = max(cur_hi, hi)
            else:
                runs.append((offset, cur_lo, cur_hi))
                cur_lo, cur_hi = lo, hi
        runs.append((offset, cur_lo, cur_hi))
    runs.sort()
    return runs


def wall_faces(
    side: str, coord: float, bands: Sequence[WallBand], tol: float = 1e-6,
) -> tuple[float, float]:
    """The two faces (on the chain axis) of the wall crossing the chain at the
    nominal break ``coord``, read off the shared wall-body ``bands`` so a
    plumbing wall's extra thickness shows. An interior partition is preferred
    over an exterior return that happens to align; with no band on the line it
    falls back to ± half an ordinary partition."""
    want = "v" if side in ("S", "N") else "h"
    best: tuple[float, float] | None = None
    best_interior = False
    for b in bands:
        if b.orientation != want:
            continue
        f0, f1 = (b.x0, b.x1) if want == "v" else (b.y0, b.y1)
        if abs((f0 + f1) / 2.0 - coord) > tol:
            continue
        interior = b.kind == "interior"
        if best is None or (interior and not best_interior):
            best = (min(f0, f1), max(f0, f1))
            best_interior = interior
            if interior:
                break
    if best is not None:
        return best
    half = INTERIOR_WALL_THICKNESS / 2.0
    return (coord - half, coord + half)


def chain_ticks(
    side: str,
    room_pts: list[float],
    jamb_pts: list[float],
    lo: float,
    hi: float,
    dim_mode: str = "nominal",
    bands: Sequence[WallBand] = (),
    tol: float = 1e-6,
) -> list[float]:
    """Final tick coordinates of one chain for ``dim_mode``.

    ``room_pts`` are the nominal room-edge breaks including both span ends and
    ``jamb_pts`` the opening jambs. Nominal mode is :func:`with_jambs`.
    ``"faces"`` mode moves the span ends out to the outside envelope face,
    splits each interior break into the two faces of the wall crossing there
    (from ``bands``) and leaves the jambs where they are, so the segments still
    sum to the faces-mode overall.
    """
    if dim_mode != "faces":
        return with_jambs(room_pts, jamb_pts)
    ext = EXTERIOR_WALL_THICKNESS / 2.0
    ticks: list[float] = []
    for p in room_pts:
        if abs(p - lo) <= tol:
            ticks.append(lo - ext)  # outside face, low end
        elif abs(p - hi) <= tol:
            ticks.append(hi + ext)  # outside face, high end
        else:
            ticks.extend(wall_faces(side, p, bands))
    # Jambs stay put; drop only those that would collapse against a room break
    # (the nominal rule, so both modes agree on which slivers get a tick).
    for j in sorted(jamb_pts):
        if all(abs(j - p) >= MIN_JAMB_SEG_FT for p in room_pts):
            ticks.append(j)
    ticks.sort()
    out: list[float] = []
    for c in ticks:
        if not out or c - out[-1] > tol:
            out.append(c)
    return out
