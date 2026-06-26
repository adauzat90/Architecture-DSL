"""Geometry helpers shared by validation and rendering."""

from __future__ import annotations

from dataclasses import dataclass

from .elements import Direction, Room

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
    perpendicular extents overlap by more than ``tol``.
    """
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
