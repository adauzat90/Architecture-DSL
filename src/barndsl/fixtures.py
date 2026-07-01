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

from .elements import Barndominium, Room, RoomType
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


def plan_room_fixtures(plan: Barndominium, room: Room) -> list[Fixture]:
    """Place ``room``'s fixtures against its walls (best-effort, deterministic).

    Lays each fixture along the clear-box perimeter starting on the longer wall,
    wrapping to the next wall when the current one runs out. Returns the placed
    :class:`Fixture` footprints in world coordinates — seeds for the Revit builder
    to host families on; the designer refines from there.
    """
    kinds = fixtures_for(room.type)
    if not kinds:
        return []
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []

    walls = _walls(cw, cl)
    placed: list[Fixture] = []
    wi = 0
    cursor = 0.0
    for kind in kinds:
        spec = FIXTURES[kind]
        # Advance to a wall with room for this fixture's width (leave the corner).
        while wi < len(walls):
            _, run = walls[wi]
            if cursor + spec.width <= run + 1e-9:
                break
            wi += 1
            cursor = 0.0
        if wi >= len(walls):
            break  # ran out of perimeter; the fit check reports the shortfall
        wall = walls[wi][0]
        fx, fy, fw, fl = _footprint(wall, x0, y0, cw, cl, cursor, spec)
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
