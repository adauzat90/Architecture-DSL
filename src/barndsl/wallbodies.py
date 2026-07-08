"""Wall-body geometry — the single source of truth for drawn *and* exported walls.

A barndsl room tiles on wall **centrelines**, so a wall has no thickness in the
model. Both deliverables that show construction — the DXF export (:mod:`dxf`) and
the SVG floor plan (:mod:`render`) — must turn those centrelines into real wall
*bodies*: closed bands at nominal thickness (exterior shell, interior partition,
plumbing wall), squared at the corners, with each door/window opening **cut out**
at its jambs so the opening reads as a clean gap in the mass.

Computing that in one place means a wall drawn on screen and the same wall in the
CAD file are byte-for-byte the same rectangle. :func:`wall_bands` yields the solid
pieces (already split at the jambs); :func:`opening_gaps` yields the cut intervals.

The band corner order ``(x0, y0, x1, y1)`` is exactly what :meth:`dxf._DxfWriter.rect`
consumes, so the DXF export stays reproducible when it iterates these bands.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    EXTERIOR_WALL_THICKNESS,
    INTERIOR_WALL_THICKNESS,
    PLUMBING_WALL_THICKNESS,
)
from .elements import Barndominium, Direction, Room
from .geometry import (
    SharedEdge,
    opening_endpoints,
    point_in_footprint,
    shared_edge,
    wall_segment,
)
from .spatial import RoomIndex

_TOL = 1e-6

#: Thickness class tokens (also the DXF/SVG provenance tag on each band).
EXTERIOR = "ext"
INTERIOR = "int"
PLUMBING = "plumbing"

#: Nominal built thickness (feet) per class — the one source both drawings size to.
THICKNESS: dict[str, float] = {
    EXTERIOR: EXTERIOR_WALL_THICKNESS,
    INTERIOR: INTERIOR_WALL_THICKNESS,
    PLUMBING: PLUMBING_WALL_THICKNESS,
}

#: Wall direction → the one-letter side tag the drawings use.
_SIDE = {
    Direction.SOUTH: "S",
    Direction.NORTH: "N",
    Direction.WEST: "W",
    Direction.EAST: "E",
}


@dataclass(frozen=True)
class WallBand:
    """One solid wall-mass rectangle, already split at any opening jambs.

    ``(x0, y0, x1, y1)`` are opposite corners in world feet, in the order the DXF
    ``rect`` primitive consumes (so re-exporting is byte-identical). ``orientation``
    is the wall's run axis: ``"h"`` (runs in x, a S/N wall) or ``"v"`` (runs in y,
    a W/E wall or a vertical partition).
    """

    x0: float
    y0: float
    x1: float
    y1: float
    kind: str              # "exterior" | "interior"
    thickness_class: str   # EXTERIOR | INTERIOR | PLUMBING
    orientation: str       # "h" | "v"
    room_a: str            # host room (exterior) / one flank (interior)
    room_b: str | None     # the other flank (interior) / None (exterior)
    wall: str | None       # "S"/"N"/"W"/"E" (exterior) / None (interior)


@dataclass(frozen=True)
class OpeningGap:
    """An opening's cut-out interval in a wall band.

    ``lo``/``hi`` bound the gap along the wall's run axis; ``pos`` is the wall
    centreline on the perpendicular axis; ``orientation`` matches its band's.
    """

    lo: float
    hi: float
    pos: float
    orientation: str       # "h" | "v"
    thickness_class: str
    category: str          # "window" | "door" | "opening"


def solid_runs(
    lo: float, hi: float, openings: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Sub-intervals of ``[lo, hi]`` left solid after cutting ``openings`` out.

    Splits a wall band at the jambs of the doors/windows on it, so each surviving
    piece is a closed wall polygon that stops cleanly at the opening.
    """
    cuts = sorted(
        (max(lo, a), min(hi, b)) for a, b in openings if b > lo + _TOL and a < hi - _TOL
    )
    runs: list[tuple[float, float]] = []
    cur = lo
    for a, b in cuts:
        if a > cur + _TOL:
            runs.append((cur, a))
        cur = max(cur, b)
    if hi > cur + _TOL:
        runs.append((cur, hi))
    return runs


def _exterior_wall_dirs(plan: Barndominium, room: Room) -> list[Direction]:
    from .validation import exterior_walls

    return exterior_walls(plan, room)


def _axis_interval(
    room: Room, wall: Direction, offset: float, width: float
) -> tuple[float, float]:
    x1, y1, x2, y2 = opening_endpoints(room, wall, offset, width)
    if wall in (Direction.SOUTH, Direction.NORTH):
        return min(x1, x2), max(x1, x2)
    return min(y1, y2), max(y1, y2)


def _exterior_openings(
    plan: Barndominium, room: Room, wall: Direction
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for w in plan.windows:
        if w.room == room.id and w.wall == wall:
            out.append(_axis_interval(room, wall, w.offset, w.width))
    for d in plan.exterior_doors:
        if d.room == room.id and d.wall == wall:
            out.append(_axis_interval(room, wall, d.offset, d.width))
    return out


def partition_class(plan: Barndominium, ra: Room, rb: Room) -> str:
    """Thickness class of the partition between ``ra`` and ``rb`` — a declared
    plumbing (wet) wall is the thicker 2x6, otherwise an ordinary 2x4."""
    for ws in getattr(plan, "wall_specs", None) or []:
        if "plumbing" in ws.attributes and {ws.room_a, ws.room_b} == {ra.id, rb.id}:
            return PLUMBING
    return INTERIOR


def _interior_opening_interval(door, edge: SharedEdge) -> tuple[float, float]:
    w = min(door.width, edge.length)
    if door.offset is None:
        start = edge.mid - w / 2.0
    else:
        start = edge.lo + max(0.0, min(door.offset, edge.length - w))
    return start, start + w


def _interior_openings(
    plan: Barndominium, ra: Room, rb: Room, edge: SharedEdge
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for d in plan.interior_doors:
        if {d.room_a, d.room_b} != {ra.id, rb.id}:
            continue
        out.append(_interior_opening_interval(d, edge))
    return out


def wall_bands(plan: Barndominium, level: int) -> list[WallBand]:
    """Solid wall-mass rectangles for ``level``, split at every opening jamb.

    Order (fixed, for the byte-reproducible DXF): the exterior shell first — one
    band per room exterior wall, corners squared by a half-thickness overrun, with
    windows and exterior doors cut out — then the interior partitions, one band
    centred on each shared room edge, cut by the doors between that pair.
    """
    rooms = [r for r in plan.rooms if r.level == level]
    bands: list[WallBand] = []

    half = EXTERIOR_WALL_THICKNESS / 2.0
    for room in rooms:
        for wall in _exterior_wall_dirs(plan, room):
            x1, y1, x2, y2 = wall_segment(room, wall)
            opens = _exterior_openings(plan, room, wall)
            side = _SIDE[wall]
            if wall in (Direction.SOUTH, Direction.NORTH):
                cy = y1
                lo, hi = min(x1, x2) - half, max(x1, x2) + half
                for a, b in solid_runs(lo, hi, opens):
                    bands.append(
                        WallBand(a, cy - half, b, cy + half, "exterior",
                                 EXTERIOR, "h", room.id, None, side)
                    )
            else:
                cx = x1
                lo, hi = min(y1, y2) - half, max(y1, y2) + half
                for a, b in solid_runs(lo, hi, opens):
                    bands.append(
                        WallBand(cx - half, a, cx + half, b, "exterior",
                                 EXTERIOR, "v", room.id, None, side)
                    )

    index = RoomIndex(rooms)
    for i, j in index.candidate_pairs():
        ra, rb = rooms[i], rooms[j]
        edge = shared_edge(ra, rb)
        if edge is None:
            continue
        cls = partition_class(plan, ra, rb)
        ph = THICKNESS[cls] / 2.0
        opens = _interior_openings(plan, ra, rb, edge)
        if edge.orientation == "v":
            for a, b in solid_runs(edge.lo, edge.hi, opens):
                bands.append(
                    WallBand(edge.pos - ph, a, edge.pos + ph, b, "interior",
                             cls, "v", ra.id, rb.id, None)
                )
        else:
            for a, b in solid_runs(edge.lo, edge.hi, opens):
                bands.append(
                    WallBand(a, edge.pos - ph, b, edge.pos + ph, "interior",
                             cls, "h", ra.id, rb.id, None)
                )

    # Last (so void-free plans keep a byte-identical band sequence): walls on
    # room edges that face an unassigned interior pocket — a hole in the tiling —
    # and the exterior shell along envelope runs no room reaches (a void touching
    # the envelope would otherwise leave a gap in the building outline).
    if level == 0:
        bands.extend(_void_wall_bands(plan, rooms))
        bands.extend(_void_shell_bands(plan, rooms))
    return bands


def _void_wall_bands(plan: Barndominium, rooms: list[Room]) -> list[WallBand]:
    """Wall bands on room edges that face *unassigned* interior area (a void).

    A room edge is normally either on the envelope (an exterior shell band) or
    shared with another room (an interior partition band). An edge facing a
    hole in the tiling — footprint area no room claims — got neither, so a
    void's neighbours (a hall, a bedroom) drew with no wall at all and the
    plan read as open into the pocket. Real construction frames that wall, so
    emit an ordinary interior partition along the room's side of the line.

    Level 0 only: on an upper storey the non-room area is open-to-below (a
    railing line, not a framed wall).
    """
    sections = plan.footprint_sections()
    ph = THICKNESS[INTERIOR] / 2.0
    probe = 0.05  # just past the wall line, mirroring wall_faces_outside()
    bands: list[WallBand] = []
    for room in rooms:
        for wall in (Direction.SOUTH, Direction.NORTH, Direction.WEST, Direction.EAST):
            horizontal = wall in (Direction.SOUTH, Direction.NORTH)
            x1, y1, x2, y2 = wall_segment(room, wall)
            if horizontal:
                lo, hi, pos = min(x1, x2), max(x1, x2), y1
            else:
                lo, hi, pos = min(y1, y2), max(y1, y2), x1
            # Intervals of this edge covered by a facing room: its opposite
            # edge collinear with ours, extents overlapping along the run.
            covered: list[tuple[float, float]] = []
            for rb in rooms:
                if rb is room:
                    continue
                if horizontal:
                    facing = rb.y2 if wall is Direction.SOUTH else rb.y
                    if abs(facing - pos) > _TOL:
                        continue
                    a, b = max(lo, rb.x), min(hi, rb.x2)
                else:
                    facing = rb.x2 if wall is Direction.WEST else rb.x
                    if abs(facing - pos) > _TOL:
                        continue
                    a, b = max(lo, rb.y), min(hi, rb.y2)
                if b > a + _TOL:
                    covered.append((a, b))
            for a, b in solid_runs(lo, hi, covered):
                if b - a <= _TOL:
                    continue
                mid = (a + b) / 2.0
                if wall is Direction.SOUTH:
                    px, py = mid, pos - probe
                elif wall is Direction.NORTH:
                    px, py = mid, pos + probe
                elif wall is Direction.WEST:
                    px, py = pos - probe, mid
                else:
                    px, py = pos + probe, mid
                if not point_in_footprint(sections, px, py):
                    continue  # faces outside: the exterior shell owns this run
                if horizontal:
                    bands.append(
                        WallBand(a, pos - ph, b, pos + ph, "interior",
                                 INTERIOR, "h", room.id, None, None)
                    )
                else:
                    bands.append(
                        WallBand(pos - ph, a, pos + ph, b, "interior",
                                 INTERIOR, "v", room.id, None, None)
                    )
    return bands


def _void_shell_bands(plan: Barndominium, rooms: list[Room]) -> list[WallBand]:
    """Exterior shell bands along envelope runs no room reaches.

    The shell pass in :func:`wall_bands` is per room, so an unassigned pocket
    that touches the envelope leaves its stretch of the building outline
    undrawn. Walk each footprint section's edges, subtract the intervals rooms
    cover, and band what remains — but only where the far side really is
    outside the union (a seam between two abutting sections is interior).
    Solid runs only: openings belong to rooms, and no room is here.
    """
    sections = plan.footprint_sections()
    half = EXTERIOR_WALL_THICKNESS / 2.0
    probe = 0.05
    bands: list[WallBand] = []
    for sx, sy, sw, sl in sections:
        edges = (
            (Direction.SOUTH, sy, sx, sx + sw),
            (Direction.NORTH, sy + sl, sx, sx + sw),
            (Direction.WEST, sx, sy, sy + sl),
            (Direction.EAST, sx + sw, sy, sy + sl),
        )
        for wall, pos, lo, hi in edges:
            horizontal = wall in (Direction.SOUTH, Direction.NORTH)
            covered: list[tuple[float, float]] = []
            for r in rooms:
                if horizontal:
                    redge = r.y if wall is Direction.SOUTH else r.y2
                    if abs(redge - pos) > _TOL:
                        continue
                    a, b = max(lo, r.x), min(hi, r.x2)
                else:
                    redge = r.x if wall is Direction.WEST else r.x2
                    if abs(redge - pos) > _TOL:
                        continue
                    a, b = max(lo, r.y), min(hi, r.y2)
                if b > a + _TOL:
                    covered.append((a, b))
            for a, b in solid_runs(lo, hi, covered):
                if b - a <= _TOL:
                    continue
                mid = (a + b) / 2.0
                if wall is Direction.SOUTH:
                    px, py = mid, pos - probe
                elif wall is Direction.NORTH:
                    px, py = mid, pos + probe
                elif wall is Direction.WEST:
                    px, py = pos - probe, mid
                else:
                    px, py = pos + probe, mid
                if point_in_footprint(sections, px, py):
                    continue  # a seam into an abutting section, not the boundary
                side = _SIDE[wall]
                if horizontal:
                    bands.append(
                        WallBand(a - half, pos - half, b + half, pos + half,
                                 "exterior", EXTERIOR, "h", "", None, side)
                    )
                else:
                    bands.append(
                        WallBand(pos - half, a - half, pos + half, b + half,
                                 "exterior", EXTERIOR, "v", "", None, side)
                    )
    return bands


def opening_gaps(plan: Barndominium, level: int) -> list[OpeningGap]:
    """The cut-out interval of every opening on ``level`` (the gaps the bands leave).

    Emitted in the same wall order as :func:`wall_bands`: exterior windows/doors
    per room wall, then interior doors per shared edge.
    """
    rooms = [r for r in plan.rooms if r.level == level]
    gaps: list[OpeningGap] = []
    room_by_id = {r.id: r for r in rooms}

    for room in rooms:
        for wall in _exterior_wall_dirs(plan, room):
            horizontal = wall in (Direction.SOUTH, Direction.NORTH)
            orient = "h" if horizontal else "v"
            pos = wall_segment(room, wall)[1 if horizontal else 0]
            for win in plan.windows:
                if win.room == room.id and win.wall == wall:
                    lo, hi = _axis_interval(room, wall, win.offset, win.width)
                    gaps.append(OpeningGap(lo, hi, pos, orient, EXTERIOR, "window"))
            for xd in plan.exterior_doors:
                if xd.room == room.id and xd.wall == wall:
                    lo, hi = _axis_interval(room, wall, xd.offset, xd.width)
                    gaps.append(OpeningGap(lo, hi, pos, orient, EXTERIOR, "door"))

    index = RoomIndex(rooms)
    for i, j in index.candidate_pairs():
        ra, rb = rooms[i], rooms[j]
        edge = shared_edge(ra, rb)
        if edge is None:
            continue
        cls = partition_class(plan, ra, rb)
        for door in plan.interior_doors:
            if {door.room_a, door.room_b} != {ra.id, rb.id}:
                continue
            if room_by_id.get(door.room_a) is None or room_by_id.get(door.room_b) is None:
                continue
            lo, hi = _interior_opening_interval(door, edge)
            cat = "opening" if getattr(door, "leaf", True) is False else "door"
            gaps.append(OpeningGap(lo, hi, edge.pos, edge.orientation, cls, cat))
    return gaps
