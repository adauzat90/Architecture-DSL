"""Geometry helpers shared by validation and rendering, including a room's
walls: which face the outside, and the clear floor left inside them."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import (
    EPSILON,
    EXTERIOR_WALL_THICKNESS,
    INTERIOR_WALL_THICKNESS,
    PLUMBING_WALL_THICKNESS,
)
from .elements import Barndominium, Direction, InteriorDoor, Room

TOL = 1e-6


@dataclass
class SharedEdge:
    """A segment two rooms share along a common wall."""

    orientation: str  # "v" (vertical, constant x) or "h" (horizontal, constant y)
    pos: float  # the constant coordinate (x for vertical, y for horizontal)
    lo: float  # interval start along the wall
    hi: float  # interval end along the wall

    @property
    def length(self) -> float:
        return self.hi - self.lo

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0


def shared_edge(a: Room, b: Room, tol: float = TOL) -> SharedEdge | None:
    """Return the wall segment ``a`` and ``b`` share, or ``None`` if not adjacent.

    Two rooms are adjacent when one's edge is collinear with the other's and the
    perpendicular extents overlap by more than ``tol``. Rooms on different floor
    levels never share a wall (no stairs are modelled yet).
    """
    if getattr(a, "level", 0) != getattr(b, "level", 0):
        return None
    # Vertical shared edge (a constant x), overlapping in y.
    for pos in (a.x2, a.x):
        if abs(pos - b.x) <= tol or abs(pos - b.x2) <= tol:
            lo = max(a.y, b.y)
            hi = min(a.y2, b.y2)
            if hi - lo > tol:
                return SharedEdge("v", pos, lo, hi)
    # Horizontal shared edge (a constant y), overlapping in x.
    for pos in (a.y2, a.y):
        if abs(pos - b.y) <= tol or abs(pos - b.y2) <= tol:
            lo = max(a.x, b.x)
            hi = min(a.x2, b.x2)
            if hi - lo > tol:
                return SharedEdge("h", pos, lo, hi)
    return None


def door_span(edge: SharedEdge, door: InteriorDoor) -> tuple[float, float]:
    """The world ``(lo, hi)`` an interior door occupies along its shared wall.

    ``offset`` is measured from the south/west end of the shared wall
    (``edge.lo``); ``None`` centres the door. The span is the door as built:
    a leaf wider than the wall is cut to the wall, and an offset that would run
    it off either end slides it back on. ``DOOR_FIT``/``DOOR_OOB`` report the
    authored overrun; drawing, exports, schedules, introspection and every
    clearance check use this span so they all put the door in one place.
    """
    lo = edge.lo + door_offset(edge, door)
    return lo, lo + min(door.width, edge.length)


def door_offset(edge: SharedEdge, door: InteriorDoor) -> float:
    """How far the near jamb of :func:`door_span` sits from ``edge.lo`` — the
    built offset, with a centred door resolved to half the leftover wall."""
    width = min(door.width, edge.length)
    if door.offset is None:
        return (edge.length - width) / 2.0
    return max(0.0, min(door.offset, edge.length - width))


# --- rectilinear footprint (a union of axis-aligned rectangular sections) ----
#
# A plain barndo footprint is one rectangle; an L/T/U footprint is a union of a
# few. A "section" is a ``(x, y, w, l)`` rectangle. These helpers answer the
# questions the validator and renderer ask of that union — is a point/room
# inside it, what's its area, where's its outline — via coordinate compression,
# which is exact and simple for the handful of sections a building has.


def point_in_footprint(
    sections: list[tuple[float, float, float, float]],
    px: float,
    py: float,
    tol: float = TOL,
) -> bool:
    """Is point ``(px, py)`` inside (or on the closed boundary of) the union?"""
    for x, y, w, l in sections:
        if x - tol <= px <= x + w + tol and y - tol <= py <= y + l + tol:
            return True
    return False


def _grid(sections, lo, hi, axis, clamp=True):
    pts = {lo, hi}
    for sec in sections:
        a, b = (sec[0], sec[0] + sec[2]) if axis == 0 else (sec[1], sec[1] + sec[3])
        for v in (a, b):
            if clamp:
                if lo - TOL <= v <= hi + TOL:
                    pts.add(min(max(v, lo), hi))
            else:
                pts.add(v)
    return sorted(pts)


def rect_in_footprint(
    sections: list[tuple[float, float, float, float]],
    x: float,
    y: float,
    w: float,
    l: float,
    tol: float = TOL,
) -> bool:
    """Is the whole rectangle ``[x,x+w]×[y,y+l]`` covered by the union?

    Splits the rectangle along every section edge and checks each resulting cell
    centre lies in some section — so a room poking into an L-notch (or beyond the
    footprint, or at a negative coordinate) is detected, while one tucked in a
    single section or straddling a seam between abutting sections passes.
    """
    xs = _grid(sections, x, x + w, 0)
    ys = _grid(sections, y, y + l, 1)
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx = (xs[i] + xs[i + 1]) / 2.0
            cy = (ys[j] + ys[j + 1]) / 2.0
            if not point_in_footprint(sections, cx, cy, tol):
                return False
    return True


def footprint_area(sections: list[tuple[float, float, float, float]]) -> float:
    """Area of the union (overlapping sections are not double-counted)."""
    xs = _grid(sections, 0.0, 0.0, 0, clamp=False)
    ys = _grid(sections, 0.0, 0.0, 1, clamp=False)
    area = 0.0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx = (xs[i] + xs[i + 1]) / 2.0
            cy = (ys[j] + ys[j + 1]) / 2.0
            if point_in_footprint(sections, cx, cy):
                area += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return area


def footprint_boundary(
    sections: list[tuple[float, float, float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """The union's outline as a list of ``((x1,y1),(x2,y2))`` edge segments.

    An edge of a covered grid cell is on the outline when the cell across it is
    not covered (or off-grid). Collinear pieces aren't merged — fine for drawing.
    """
    xs = _grid(sections, 0.0, 0.0, 0, clamp=False)
    ys = _grid(sections, 0.0, 0.0, 1, clamp=False)

    def covered(i, j):
        if i < 0 or j < 0 or i >= len(xs) - 1 or j >= len(ys) - 1:
            return False
        cx = (xs[i] + xs[i + 1]) / 2.0
        cy = (ys[j] + ys[j + 1]) / 2.0
        return point_in_footprint(sections, cx, cy)

    edges = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            if not covered(i, j):
                continue
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[j], ys[j + 1]
            if not covered(i, j - 1):
                edges.append(((x0, y0), (x1, y0)))
            if not covered(i, j + 1):
                edges.append(((x0, y1), (x1, y1)))
            if not covered(i - 1, j):
                edges.append(((x0, y0), (x0, y1)))
            if not covered(i + 1, j):
                edges.append(((x1, y0), (x1, y1)))
    return edges


def point_rect_distance(px: float, py: float, rect: tuple[float, float, float, float]) -> float:
    """Distance from point ``(px, py)`` to the axis-aligned rectangle
    ``(x1, y1, x2, y2)`` — 0 when the point is inside it."""
    x1, y1, x2, y2 = rect
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return math.hypot(dx, dy)


def wall_faces_outside(
    sections: list[tuple[float, float, float, float]],
    room: Room,
    wall: Direction,
    eps: float = 0.05,
) -> bool:
    """Does ``room``'s ``wall`` face outside the footprint (can take a window)?

    Checks the point just outside the wall's midpoint: if it isn't inside the
    union, the wall lies on the building's exterior boundary.
    """
    if wall is Direction.SOUTH:
        px, py = (room.x + room.x2) / 2.0, room.y - eps
    elif wall is Direction.NORTH:
        px, py = (room.x + room.x2) / 2.0, room.y2 + eps
    elif wall is Direction.WEST:
        px, py = room.x - eps, (room.y + room.y2) / 2.0
    else:  # EAST
        px, py = room.x2 + eps, (room.y + room.y2) / 2.0
    return not point_in_footprint(sections, px, py)


def wall_segment(room: Room, wall: Direction) -> tuple[float, float, float, float]:
    """Return the endpoints ``(x1, y1, x2, y2)`` of a room's named wall."""
    if wall is Direction.SOUTH:
        return room.x, room.y, room.x2, room.y
    if wall is Direction.NORTH:
        return room.x, room.y2, room.x2, room.y2
    if wall is Direction.WEST:
        return room.x, room.y, room.x, room.y2
    # EAST
    return room.x2, room.y, room.x2, room.y2


def opening_endpoints(
    room: Room, wall: Direction, offset: float, width: float
) -> tuple[float, float, float, float]:
    """Endpoints of an opening on ``wall``, ``offset`` ft from the wall start."""
    if wall in (Direction.SOUTH, Direction.NORTH):
        y = room.y if wall is Direction.SOUTH else room.y2
        x1 = room.x + offset
        return x1, y, x1 + width, y
    x = room.x if wall is Direction.WEST else room.x2
    y1 = room.y + offset
    return x, y1, x, y1 + width


def exterior_walls(plan: Barndominium, room: Room, tol: float = EPSILON) -> list[Direction]:
    """Walls of ``room`` that lie on the building envelope (can take windows).

    For a plain rectangular footprint this is the four envelope edges; for an
    L/T/U footprint (``wing`` blocks) a wall counts only when it faces *outside*
    the footprint union — a wall on the seam between two abutting blocks is
    interior even if it sits at the primary envelope's edge.
    """
    if not plan.wings:
        walls: list[Direction] = []
        if abs(room.y) <= tol:
            walls.append(Direction.SOUTH)
        if abs(room.y2 - plan.envelope_length) <= tol:
            walls.append(Direction.NORTH)
        if abs(room.x) <= tol:
            walls.append(Direction.WEST)
        if abs(room.x2 - plan.envelope_width) <= tol:
            walls.append(Direction.EAST)
        return walls

    sections = plan.footprint_sections()
    return [
        w
        for w in (Direction.SOUTH, Direction.NORTH, Direction.WEST, Direction.EAST)
        if wall_faces_outside(sections, room, w)
    ]


def plumbing_wall_sides(plan: Barndominium, room: Room) -> set[Direction]:
    """The sides of ``room`` that carry a declared **plumbing wall** (a ``wall
    a - b plumbing`` naming this room whose pair really shares a wall).

    A declared plumbing wall is built as a 2x6 (:data:`PLUMBING_WALL_THICKNESS`),
    so the flanking rooms lose half of that — not half an ordinary partition —
    from their clear dimensions. The whole side is treated as the thicker wall
    even when the shared run covers only part of it (conservative and simple).
    """
    sides: set[Direction] = set()
    for ws in getattr(plan, "wall_specs", None) or []:
        if "plumbing" not in ws.attributes or room.id not in (ws.room_a, ws.room_b):
            continue
        other_id = ws.room_b if room.id == ws.room_a else ws.room_a
        if other_id == room.id:
            continue
        other = plan.room(other_id)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        if edge.orientation == "v":
            sides.add(
                Direction.WEST if abs(edge.pos - room.x) <= EPSILON else Direction.EAST
            )
        else:
            sides.add(
                Direction.SOUTH if abs(edge.pos - room.y) <= EPSILON else Direction.NORTH
            )
    return sides


def _wall_halves(plan: Barndominium, room: Room) -> dict[Direction, float]:
    """Half the bounding wall's thickness per side of ``room``: an exterior
    shell edge, a declared plumbing (2x6) wall, or an ordinary partition."""
    ext = set(exterior_walls(plan, room))
    plumbing = (
        plumbing_wall_sides(plan, room)
        if getattr(plan, "wall_specs", None)
        else set()
    )

    def half(side: Direction) -> float:
        if side in ext:
            thk = EXTERIOR_WALL_THICKNESS
        elif side in plumbing:
            thk = PLUMBING_WALL_THICKNESS
        else:
            thk = INTERIOR_WALL_THICKNESS
        return thk / 2.0

    return {side: half(side) for side in Direction}


def clear_dimensions(plan: Barndominium, room: Room) -> tuple[float, float]:
    """The room's built **clear** (finish-face) ``(width, length)`` in feet.

    barndsl rooms tile on wall *centrelines*, so the interior you can actually
    use is the nominal rectangle minus half of each bounding wall's thickness —
    an exterior (shell) edge costs more than an interior partition, and a
    declared plumbing wall (``wall a - b plumbing``) is a 2x6. This is the
    dimension IRC habitability minimums are measured to (finished surfaces) and
    the one Revit computes for its room schedule, so reporting/checking it keeps
    barndsl and the built model telling the same story.
    """
    halves = _wall_halves(plan, room)
    clear_w = room.width - halves[Direction.WEST] - halves[Direction.EAST]
    clear_l = room.length - halves[Direction.SOUTH] - halves[Direction.NORTH]
    return max(0.0, clear_w), max(0.0, clear_l)


def clear_box(plan: Barndominium, room: Room) -> tuple[float, float, float, float]:
    """The room's clear interior as a world-coordinate rectangle
    ``(x0, y0, width, length)`` — the finish-face box inside the wall centrelines.
    Its south-west corner is inset from the room rectangle by half the west/south
    wall. Used to place fixtures inside the usable floor."""
    halves = _wall_halves(plan, room)
    clear_w, clear_l = clear_dimensions(plan, room)
    return (
        room.x + halves[Direction.WEST],
        room.y + halves[Direction.SOUTH],
        clear_w,
        clear_l,
    )
