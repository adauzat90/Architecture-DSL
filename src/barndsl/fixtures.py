"""Plumbing fixtures and kitchen appliances — seeds + a clearance check.

A barndsl room is an empty rectangle; a *residential* room earns its keep by the
fixtures in it. This module derives the standard fixtures a wet room or kitchen
needs — deterministically, no Revit, no API key — as **seed footprints** the
Revit builder can drop loadable families onto, and it answers a plain question
the validator asks: *do the fixtures fit with code clearances?*

Sizes are nominal residential fixtures (feet); the clearances follow IRC R307
(a water closet wants 15 in from its centreline to any wall/fixture and 21 in of
clear floor in front) and common kitchen practice (a ~40 in working aisle). Like
the frame and the roof, this is a **layout aid**, not a fixture-approval — the
final layout is the designer's.
"""

from __future__ import annotations

from dataclasses import dataclass

from .elements import DOUBLE_LEAF_KINDS, Barndominium, Direction, Room, RoomType
from .geometry import opening_endpoints, shared_edge
from .validation import clear_box


@dataclass(frozen=True)
class FixtureSpec:
    """A fixture's footprint: ``width`` runs along the wall it backs to, ``depth``
    projects into the room, and ``front`` is the clear floor it needs in front
    (measured from the fixture face into the room)."""

    kind: str
    width: float
    depth: float
    front: float


#: The standard fixtures, in the order they're laid along the wall.
FIXTURES: dict[str, FixtureSpec] = {
    "toilet": FixtureSpec("toilet", 2.5, 2.33, 1.75),  # 30 in bay (15+15), 28 in deep, 21 in front
    "lavatory": FixtureSpec("lavatory", 2.0, 1.83, 1.75),  # 24x22 vanity
    "tub": FixtureSpec("tub", 5.0, 2.5, 1.75),  # 60x30 tub
    "shower": FixtureSpec("shower", 2.67, 2.67, 1.75),  # 32x32 stall
    "sink": FixtureSpec("sink", 2.5, 2.0, 3.5),  # base cabinet + 42 in aisle
    "range": FixtureSpec("range", 2.5, 2.0, 3.5),
    "refrigerator": FixtureSpec("refrigerator", 3.0, 2.5, 3.5),
}

#: Which fixtures each room type gets.
_ROOM_FIXTURES: dict[RoomType, list[str]] = {
    RoomType.BATHROOM: ["toilet", "lavatory", "tub"],
    RoomType.HALF_BATH: ["toilet", "lavatory"],
    RoomType.KITCHEN: ["refrigerator", "range", "sink"],
}


def fixtures_for(room_type: RoomType) -> list[str]:
    """The fixture kinds a room of this type should carry (empty if none)."""
    return list(_ROOM_FIXTURES.get(room_type, []))


@dataclass
class Fixture:
    """A placed fixture: its footprint rectangle in **world** feet (south-west
    corner ``(x, y)``), the wall it backs to, and its kind."""

    kind: str
    x: float
    y: float
    width: float
    length: float
    wall: str  # S | N | E | W

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.length / 2.0)


# Perimeter walk of the clear box, as (wall, run-length) starting on the longer
# wall so the widest fixtures get the most room; fixtures wrap around the corner.
def _walls(cw: float, cl: float):
    if cw >= cl:
        return [("S", cw), ("E", cl), ("N", cw), ("W", cl)]
    return [("W", cl), ("N", cw), ("E", cl), ("S", cw)]


def _footprint(wall: str, x0: float, y0: float, cw: float, cl: float, cursor: float, spec: FixtureSpec):
    """The world rectangle for a fixture at ``cursor`` feet along ``wall``."""
    d = min(spec.depth, (cl if wall in ("S", "N") else cw))
    if wall == "S":
        return x0 + cursor, y0, spec.width, d
    if wall == "N":
        return x0 + cursor, y0 + cl - d, spec.width, d
    if wall == "W":
        return x0, y0 + cursor, d, spec.width
    return x0 + cw - d, y0 + cursor, d, spec.width  # E


#: How far (ft) to nudge a fixture along its wall when a door swing blocks it.
_KEEPOUT_STEP = 0.5


def _rect_overlaps(fx, fy, fw, fl, box, tol: float = 1e-6) -> bool:
    """Do fixture footprint ``(fx, fy, fw, fl)`` and axis-aligned ``box`` overlap?"""
    bx0, by0, bx1, by1 = box
    return (
        fx < bx1 - tol and fx + fw > bx0 + tol
        and fy < by1 - tol and fy + fl > by0 + tol
    )


def _door_keepouts(plan: Barndominium, room: Room) -> list[tuple[float, float, float, float]]:
    """Rectangles ``(x0, y0, x1, y1)`` a fixture should stay clear of: each door on
    ``room`` reserves its opening span extended into the room by the door's own
    width — a conservative bound on the leaf's swept quarter-disc — so the placer
    never parks a fixture where a door swings (which would read as a false
    DOOR_HITS_FIXTURE and, in Revit, seed a toilet in a doorway)."""
    boxes: list[tuple[float, float, float, float]] = []

    def _into_box(wall, ax, ay, bx, by, w):
        lo_x, hi_x, lo_y, hi_y = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
        if wall is Direction.SOUTH:
            return (lo_x, hi_y, hi_x, hi_y + w)
        if wall is Direction.NORTH:
            return (lo_x, lo_y - w, hi_x, lo_y)
        if wall is Direction.WEST:
            return (lo_x, lo_y, lo_x + w, hi_y)
        return (hi_x - w, lo_y, hi_x, hi_y)  # EAST

    for xd in plan.exterior_doors:
        if xd.room != room.id or xd.overhead:
            continue
        x1, y1, x2, y2 = opening_endpoints(room, xd.wall, xd.offset, xd.width)
        boxes.append(_into_box(xd.wall, x1, y1, x2, y2, xd.width))

    for d in plan.interior_doors:
        if room.id not in (d.room_a, d.room_b):
            continue
        if d.kind not in ("swing", *DOUBLE_LEAF_KINDS):  # pocket/sliding/cased: no arc
            continue
        other = plan.room(d.room_b if d.room_a == room.id else d.room_a)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        w = min(d.width, edge.length)
        if getattr(d, "offset", None) is not None:
            start = edge.lo + max(0.0, min(d.offset, edge.length - w))
        else:
            start = edge.mid - w / 2.0
        if edge.orientation == "v":  # wall at x = edge.pos, opening spans y
            if room.x >= edge.pos - 1e-6:  # room lies east of the wall
                boxes.append((edge.pos, start, edge.pos + w, start + w))
            else:
                boxes.append((edge.pos - w, start, edge.pos, start + w))
        else:  # wall at y = edge.pos, opening spans x
            if room.y >= edge.pos - 1e-6:  # room lies north of the wall
                boxes.append((start, edge.pos, start + w, edge.pos + w))
            else:
                boxes.append((start, edge.pos - w, start + w, edge.pos))
    return boxes


def plan_room_fixtures(plan: Barndominium, room: Room, *, avoid_doors: bool = True) -> list[Fixture]:
    """Place ``room``'s fixtures against its walls (best-effort, deterministic).

    Lays each fixture along the clear-box perimeter starting on the longer wall,
    wrapping to the next wall when the current one runs out. Returns the placed
    :class:`Fixture` footprints in world coordinates — seeds for the Revit builder
    to host families on; the designer refines from there.

    ``avoid_doors`` (the default) slides fixtures clear of every door's swing;
    pass ``False`` for the door-blind placement, which the swing-crowding check
    diffs against to tell when a door — not just a small room — drops a fixture.
    """
    kinds = fixtures_for(room.type)
    if not kinds:
        return []
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []

    walls = _walls(cw, cl)
    keepouts = _door_keepouts(plan, room) if avoid_doors else []
    placed: list[Fixture] = []
    wi = 0
    cursor = 0.0
    for kind in kinds:
        spec = FIXTURES[kind]
        # Advance to a wall with room for this fixture's width (leave the corner),
        # nudging past any spot a door swings through.
        wall = fx = fy = fw = fl = None
        while wi < len(walls):
            _, run = walls[wi]
            if cursor + spec.width > run + 1e-9:
                wi += 1
                cursor = 0.0
                continue
            wname = walls[wi][0]
            tx, ty, tw, tl = _footprint(wname, x0, y0, cw, cl, cursor, spec)
            if any(_rect_overlaps(tx, ty, tw, tl, b) for b in keepouts):
                cursor += _KEEPOUT_STEP  # a door swings here; slide along the wall
                continue
            wall, fx, fy, fw, fl = wname, tx, ty, tw, tl
            break
        if wall is None:
            break  # ran out of perimeter; the fit check reports the shortfall
        placed.append(Fixture(kind, fx, fy, fw, fl, wall))
        cursor += spec.width
    return placed


def fixtures_fit(room_type: RoomType, clear_w: float, clear_l: float) -> tuple[bool, str]:
    """Whether ``room_type``'s fixtures fit in a ``clear_w`` × ``clear_l`` interior
    with code clearances. Returns ``(ok, reason)``; ``reason`` is empty when ok.

    Deliberately a set of **necessary** conditions (a clearly-too-small room
    fails), not a full packing — so it's a stable guardrail, not a finicky solver:

    * the short side must clear the deepest fixture plus its front clearance,
    * the long side must host the widest single fixture, and
    * the two longer walls together must hold the total fixture width.
    """
    kinds = fixtures_for(room_type)
    if not kinds:
        return True, ""
    specs = [FIXTURES[k] for k in kinds]
    short, long = min(clear_w, clear_l), max(clear_w, clear_l)

    deepest = max(s.depth for s in specs)
    front = max(s.front for s in specs)
    if short + 1e-6 < deepest + front:
        return False, (
            f"short side is {short:.1f} ft; a {room_type.value.replace('_', ' ')} "
            f"needs ~{deepest + front:.1f} ft for a fixture plus its clearance"
        )

    widest = max(s.width for s in specs)
    if long + 1e-6 < widest:
        return False, (
            f"longest wall is {long:.1f} ft; the widest fixture needs {widest:.1f} ft"
        )

    total_width = sum(s.width for s in specs)
    if (short + long) + 1e-6 < total_width:
        return False, (
            f"walls total ~{short + long:.1f} ft of run; the fixtures need "
            f"{total_width:.1f} ft"
        )
    return True, ""
