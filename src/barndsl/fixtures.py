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

import math
from dataclasses import dataclass

from .elements import Barndominium, Direction, Room, RoomType
from .geometry import opening_endpoints
from .validation import clear_box, exterior_walls


@dataclass(frozen=True)
class FixtureSpec:
    """A fixture's footprint: ``width`` runs along the wall it backs to, ``depth``
    projects into the room, and ``front`` is the clear floor it needs in front
    (measured from the fixture face into the room).

    ``free`` marks a **free-standing** piece — a dining table, a coffee table, an
    island — which sits in the open floor rather than backing to a wall, so it
    auto-places at the room centre instead of walking the perimeter. ``resizable``
    marks a run whose ``width`` is only nominal (a counter is any length), so an
    explicit ``width`` override is expected, not a warning.
    """

    kind: str
    width: float
    depth: float
    front: float
    free: bool = False
    resizable: bool = False


#: The standard plumbing/appliance fixtures, in the order they're laid along the
#: wall. Sizes are nominal residential (feet); ``front`` follows the IRC/practice
#: clearances documented on the module.
FIXTURES: dict[str, FixtureSpec] = {
    # --- plumbing & kitchen appliances (auto-seeded by room type) -------------
    "toilet": FixtureSpec("toilet", 2.5, 2.33, 1.75),  # 30 in bay (15+15), 28 in deep, 21 in front
    "lavatory": FixtureSpec("lavatory", 2.0, 1.83, 1.75),  # 24x22 vanity
    "tub": FixtureSpec("tub", 5.0, 2.5, 1.75),  # 60x30 tub
    "shower": FixtureSpec("shower", 2.67, 2.67, 1.75),  # 32x32 stall
    "sink": FixtureSpec("sink", 2.5, 2.0, 3.5),  # base cabinet + 42 in aisle
    "range": FixtureSpec("range", 2.5, 2.0, 3.5),
    "refrigerator": FixtureSpec("refrigerator", 3.0, 2.5, 3.5),
    # --- furniture & equipment (placed by the author, never auto-seeded except
    #     washer/dryer in a laundry) -------------------------------------------
    "bed_queen": FixtureSpec("bed_queen", 5.0, 6.67, 2.0),  # 60x80 mattress + walk-around
    "bed_twin": FixtureSpec("bed_twin", 3.25, 6.25, 2.0),  # 39x75 mattress
    "sofa": FixtureSpec("sofa", 7.0, 3.0, 2.5),
    "armchair": FixtureSpec("armchair", 3.0, 3.0, 1.5),
    "dining_table": FixtureSpec("dining_table", 6.0, 3.33, 3.0, free=True),  # 30 in chair pull all round
    "coffee_table": FixtureSpec("coffee_table", 4.0, 2.0, 1.5, free=True),
    "desk": FixtureSpec("desk", 4.0, 2.0, 3.0),
    "dresser": FixtureSpec("dresser", 5.0, 1.67, 3.0),
    "washer": FixtureSpec("washer", 2.25, 2.25, 3.0),
    "dryer": FixtureSpec("dryer", 2.25, 2.25, 3.0),
    "water_heater": FixtureSpec("water_heater", 2.0, 2.0, 1.5),
    "kitchen_island": FixtureSpec("kitchen_island", 6.0, 3.0, 3.5, free=True),
    "counter": FixtureSpec("counter", 6.0, 2.0, 3.5, resizable=True),  # 2 ft deep, any run
    "wardrobe": FixtureSpec("wardrobe", 4.0, 2.0, 3.0),
}

#: Every catalog kind, for the parser (a `fixture <kind>` must name one of these).
FIXTURE_KINDS: tuple[str, ...] = tuple(FIXTURES)

#: Kinds that read as *wall-backed* casework — parked floating in open floor they
#: look misplaced (``FIXTURE_BACKING``). Free-standing pieces (a table, an island)
#: and the furniture that sits proud of a wall (bed, sofa, armchair, desk) are not
#: here: they legitimately float.
_WALL_BACKED: frozenset[str] = frozenset(
    {
        "toilet", "lavatory", "tub", "shower", "sink", "range", "refrigerator",
        "counter", "wardrobe", "dresser", "water_heater", "washer", "dryer",
    }
)

#: Tall, view-blocking pieces that must never park over a bedroom's escape window
#: (``FIXTURE_EGRESS``) — a small, deliberately conservative set.
_TALL_KINDS: frozenset[str] = frozenset({"refrigerator", "wardrobe", "water_heater"})

#: Landing surfaces a cooktop wants beside it (``RANGE_LANDING``; NKBA practice).
_LANDING_KINDS: frozenset[str] = frozenset(
    {"counter", "sink", "kitchen_island", "refrigerator"}
)

#: ``FIXTURE_ROOM_TYPE`` unusual-map: a kind -> the room types where it reads as
#: at home. A kind absent from this map is *never* judged, so a desk in a bedroom,
#: a water heater in a utility/closet/garage, a washer in a mudroom — all pass. The
#: map is deliberately small and conservative so the note doesn't cry wolf.
_EXPECTED_ROOMS: dict[str, frozenset[RoomType]] = {
    "toilet": frozenset({RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.LAUNDRY}),
    "tub": frozenset({RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.LAUNDRY}),
    "shower": frozenset({RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.LAUNDRY}),
    "lavatory": frozenset({RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.LAUNDRY}),
    "range": frozenset({RoomType.KITCHEN}),
    "refrigerator": frozenset({RoomType.KITCHEN}),
    "kitchen_island": frozenset({RoomType.KITCHEN}),
    "bed_queen": frozenset({RoomType.BEDROOM, RoomType.LOFT}),
    "bed_twin": frozenset({RoomType.BEDROOM, RoomType.LOFT}),
}

#: IRC R307.1 water-closet clearances (feet): 15 in from the centreline to any
#: wall/fixture on each side, 21 in of clear floor in front.
_WC_SIDE_CLEAR = 15.0 / 12.0
_WC_FRONT_CLEAR = 21.0 / 12.0
#: NKBA work-triangle: legs summing past ~26 ft mean a kitchen whose appliances
#: are scattered too far apart. (The compact lower bound is left to ``KITCHEN_FIT``
#: — a tight galley is efficient, not a defect, and the auto-seed lays a compact
#: row on purpose.)
_TRIANGLE_MAX = 26.0
#: A dryer wants a short duct run to an exterior wall (``DRYER_VENT``).
_DRYER_VENT_REACH = 10.0
#: How far off every wall a wall-backed piece must float to look adrift.
_BACKING_FLOAT = 0.5

#: Which fixtures each room type auto-seeds. Furniture is a deliberate authoring
#: act, so bedrooms/living rooms stay unseeded; a laundry seeds its washer + dryer
#: because those are the room's whole reason to exist.
_ROOM_FIXTURES: dict[RoomType, list[str]] = {
    RoomType.BATHROOM: ["toilet", "lavatory", "tub"],
    RoomType.HALF_BATH: ["toilet", "lavatory"],
    RoomType.KITCHEN: ["refrigerator", "range", "sink"],
    RoomType.LAUNDRY: ["washer", "dryer"],
}


def fixtures_for(room_type: RoomType) -> list[str]:
    """The fixture kinds a room of this type should carry (empty if none)."""
    return list(_ROOM_FIXTURES.get(room_type, []))


@dataclass
class Fixture:
    """A placed fixture: its footprint rectangle in **world** feet (south-west
    corner ``(x, y)``), the wall it backs to, and its kind.

    ``rotation`` is the plan-clockwise quarter-turn applied (0/90/180/270); the
    footprint ``width``/``length`` already reflect it, so consumers read the box
    as-is. ``seed`` is True for an auto-placed fixture and False for one the author
    placed with a ``fixture`` statement; ``source_line`` is that statement's 1-based
    line (``None`` for a seed). ``id`` is a stable ``<room>~<kind>~<i>`` handle the
    plan renderer, the exchange and the edit engine all agree on.
    """

    kind: str
    x: float
    y: float
    width: float
    length: float
    wall: str  # S | N | E | W, or "" for a free-standing piece
    rotation: float = 0.0
    seed: bool = True
    source_line: int | None = None
    id: str | None = None

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.length / 2.0)


# Perimeter walk of the clear box, as (wall, run-length) starting on the longer
# wall so the widest fixtures get the most room; fixtures wrap around the corner.
def _walls(cw: float, cl: float):
    if cw >= cl:
        return [("S", cw), ("E", cl), ("N", cw), ("W", cl)]
    return [("W", cl), ("N", cw), ("E", cl), ("S", cw)]


def _wall_rect(wall: str, x0: float, y0: float, cw: float, cl: float, cursor: float,
               width: float, depth: float):
    """The world rectangle for a ``width`` × ``depth`` fixture at ``cursor`` feet
    along ``wall`` (``width`` runs along the wall, ``depth`` into the room)."""
    if wall == "S":
        return x0 + cursor, y0, width, min(depth, cl)
    if wall == "N":
        d = min(depth, cl)
        return x0 + cursor, y0 + cl - d, width, d
    if wall == "W":
        return x0, y0 + cursor, min(depth, cw), width
    d = min(depth, cw)  # E
    return x0 + cw - d, y0 + cursor, d, width


#: How far (ft) to nudge a fixture along its wall when a door swing blocks it.
_KEEPOUT_STEP = 0.5


def _rects_overlap(a, b, tol: float = 1e-6) -> bool:
    ax, ay, aw, al = a
    bx, by, bw, bl = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + al, by + bl) - max(ay, by)
    return dx > tol and dy > tol


def _rect_gap(a, b) -> float:
    """The clear distance between two axis-aligned rectangles (0 if they touch or
    overlap) — the larger of the x-gap and y-gap, so two boxes side by side on a
    wall report their along-wall separation."""
    ax, ay, aw, al = a
    bx, by, bw, bl = b
    gx = max(0.0, bx - (ax + aw), ax - (bx + bw))
    gy = max(0.0, by - (ay + al), ay - (by + bl))
    return max(gx, gy)


def _side_clearances(f: "Fixture", room: Room, others: list) -> dict[str, float]:
    """Clear distance from ``f``'s footprint to the nearest wall (or ``others``
    fixture) on each of its four sides (keys S/N/W/E)."""
    fx, fy, fw, fl = f.x, f.y, f.width, f.length
    out = {
        "S": fy - room.y,
        "N": room.y2 - (fy + fl),
        "W": fx - room.x,
        "E": room.x2 - (fx + fw),
    }
    for ox, oy, ow, ol in others:
        x_ov = min(fx + fw, ox + ow) - max(fx, ox) > 1e-6
        y_ov = min(fy + fl, oy + ol) - max(fy, oy) > 1e-6
        if y_ov:
            if ox + ow <= fx + 1e-6:
                out["W"] = min(out["W"], fx - (ox + ow))
            elif ox >= fx + fw - 1e-6:
                out["E"] = min(out["E"], ox - (fx + fw))
        if x_ov:
            if oy + ol <= fy + 1e-6:
                out["S"] = min(out["S"], fy - (oy + ol))
            elif oy >= fy + fl - 1e-6:
                out["N"] = min(out["N"], oy - (fy + fl))
    return out


def _front_strip(f: "Fixture", front: float):
    """The clear-floor rectangle a fixture needs in front of its face, projected
    ``front`` ft from the face into the room (the fixture backs to ``f.wall`` and
    faces the opposite way). ``None`` for a free-standing piece with no wall."""
    if f.wall == "S":  # backs south, faces north
        return (f.x, f.y + f.length, f.width, front)
    if f.wall == "N":  # faces south
        return (f.x, f.y - front, f.width, front)
    if f.wall == "W":  # faces east
        return (f.x + f.width, f.y, front, f.length)
    if f.wall == "E":  # faces west
        return (f.x - front, f.y, front, f.length)
    return None


def _window_reach_rect(room: Room, w, depth: float):
    """A thin rectangle over a window's span, ``depth`` ft into the room from the
    wall — the floor a fixture would have to occupy to sit under that window."""
    x1, y1, x2, y2 = opening_endpoints(room, w.wall, w.offset, w.width)
    if w.wall is Direction.SOUTH:
        return (min(x1, x2), room.y, abs(x2 - x1), depth)
    if w.wall is Direction.NORTH:
        return (min(x1, x2), room.y2 - depth, abs(x2 - x1), depth)
    if w.wall is Direction.WEST:
        return (room.x, min(y1, y2), depth, abs(y2 - y1))
    return (room.x2 - depth, min(y1, y2), depth, abs(y2 - y1))  # EAST


def _wc_clearances(f: "Fixture", room: Room, others: list) -> tuple[float, float]:
    """A water closet's ``(side, front)`` clearances in feet (IRC R307.1).

    ``side`` is the smaller of the two centreline-to-obstruction distances along
    the back wall (obstruction = a side wall or an adjacent fixture whose depth
    band overlaps the toilet's); ``front`` is the clear floor from the toilet face
    to the nearest wall or fixture directly ahead.
    """
    fx, fy, fw, fl = f.x, f.y, f.width, f.length
    horiz = f.wall in ("S", "N")
    if horiz:
        c = fx + fw / 2.0
        depth_lo, depth_hi = fy, fy + fl
        along_lo, along_hi = fx, fx + fw
        left, right = c - room.x, room.x2 - c
        if f.wall == "S":
            face, fwall, fsign = fy + fl, room.y2, 1.0
        else:
            face, fwall, fsign = fy, room.y, -1.0
    else:
        c = fy + fl / 2.0
        depth_lo, depth_hi = fx, fx + fw
        along_lo, along_hi = fy, fy + fl
        left, right = c - room.y, room.y2 - c
        if f.wall == "W":
            face, fwall, fsign = fx + fw, room.x2, 1.0
        else:
            face, fwall, fsign = fx, room.x, -1.0
    front = (fwall - face) * fsign
    for ox, oy, ow, ol in others:
        if horiz:
            o_alo, o_ahi, o_plo, o_phi = ox, ox + ow, oy, oy + ol
        else:
            o_alo, o_ahi, o_plo, o_phi = oy, oy + ol, ox, ox + ow
        if min(o_phi, depth_hi) - max(o_plo, depth_lo) > 1e-6:  # beside the toilet
            if o_ahi <= c + 1e-6:
                left = min(left, c - o_ahi)
            elif o_alo >= c - 1e-6:
                right = min(right, o_alo - c)
        if min(o_ahi, along_hi) - max(o_alo, along_lo) > 1e-6:  # ahead of the face
            o_near = o_plo if fsign > 0 else o_phi
            d = (o_near - face) * fsign
            if d >= -1e-6:
                front = min(front, max(0.0, d))
    return min(left, right), front


def _place_perimeter(
    x0: float, y0: float, cw: float, cl: float, kinds: list[str],
    keepouts: tuple = (),
) -> list[Fixture]:
    """Lay ``kinds`` along the clear-box perimeter (longer wall first), wrapping to
    the next wall when the current one runs out — the deterministic seed layout.

    ``keepouts`` are ``(x, y, w, l)`` door-swing rectangles a fixture must stay
    clear of; a blocked spot slides the fixture along the wall until it clears (or
    wraps to the next wall), so the placer never parks a fixture in a door's arc."""
    walls = _walls(cw, cl)
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
            tx, ty, tw, tl = _wall_rect(wname, x0, y0, cw, cl, cursor, spec.width, spec.depth)
            if any(_rects_overlap((tx, ty, tw, tl), b) for b in keepouts):
                cursor += _KEEPOUT_STEP  # a door swings here; slide along the wall
                continue
            wall, fx, fy, fw, fl = wname, tx, ty, tw, tl
            break
        if wall is None:
            break  # ran out of perimeter; the fit check reports the shortfall
        placed.append(Fixture(kind, fx, fy, fw, fl, wall))
        cursor += spec.width
    return placed


def plan_room_fixtures(plan: Barndominium, room: Room, *, avoid_doors: bool = True) -> list[Fixture]:
    """Place ``room``'s **auto-seed** fixtures against its walls (deterministic).

    The historical seed placement (bath/kitchen/laundry), kept for callers that
    only want the auto-seeds. The combined authored-plus-seed layout the exchange,
    the plan drawing and the 3D model consume is :func:`resolve_room_fixtures`.

    ``avoid_doors`` (the default) slides fixtures clear of every hinged door's
    swing; pass ``False`` for the door-blind placement the swing-crowding check
    (DOOR_HITS_FIXTURE) diffs against to tell when a door — not just a small room —
    drops a fixture.
    """
    kinds = fixtures_for(room.type)
    if not kinds:
        return []
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []
    keepouts = tuple(_door_swing_rects(plan, room)) if avoid_doors else ()
    return _place_perimeter(x0, y0, cw, cl, kinds, keepouts)


def _quarter_turns(rotation: float) -> int:
    """A rotation in degrees snapped to a plan quarter-turn count (0..3). The
    massing is axis-aligned, so a fixture turns in 90° steps; other angles snap to
    the nearest, keeping the plan glyph and the 3D box consistent."""
    return int(round((rotation or 0.0) / 90.0)) % 4


def _place_explicit(
    room: Room, pf, x0: float, y0: float, cw: float, cl: float, occupied: list
) -> Fixture:
    """Resolve one authored :class:`~barndsl.elements.PlacedFixture` to a world
    :class:`Fixture`, honouring its ``at``/``wall``/``rotate`` (or auto-placing)."""
    spec = FIXTURES[pf.kind]
    width = float(pf.width) if getattr(pf, "width", None) else spec.width
    depth = spec.depth
    if _quarter_turns(getattr(pf, "rotation", 0.0)) % 2 == 1:
        width, depth = depth, width  # a quarter turn swaps the footprint axes
    wall = pf.wall.name[0] if getattr(pf, "wall", None) is not None else ""

    if pf.x is not None and pf.y is not None:
        # `at x,y` is room-local (offset from the room's SW corner).
        fx, fy = room.x + float(pf.x), room.y + float(pf.y)
        return Fixture(pf.kind, fx, fy, width, depth, wall or "S", rotation=pf.rotation)

    if wall:
        # Auto-place against the named wall: first free slot along its run.
        rx, ry, rw, rl = _first_free_on_wall(x0, y0, cw, cl, wall, width, depth, occupied)
        return Fixture(pf.kind, rx, ry, rw, rl, wall, rotation=pf.rotation)

    if spec.free:
        # Free-standing with no anchor: centre it in the clear box.
        fx = x0 + max(0.0, (cw - width) / 2.0)
        fy = y0 + max(0.0, (cl - depth) / 2.0)
        return Fixture(pf.kind, fx, fy, width, depth, "", rotation=pf.rotation)

    # No anchor: first free spot walking the perimeter (longer wall first).
    for w, _run in _walls(cw, cl):
        rect = _first_free_on_wall(x0, y0, cw, cl, w, width, depth, occupied, give_up=True)
        if rect is not None:
            rx, ry, rw, rl = rect
            return Fixture(pf.kind, rx, ry, rw, rl, w, rotation=pf.rotation)
    return Fixture(pf.kind, x0, y0, width, depth, "S", rotation=pf.rotation)


def _first_free_on_wall(
    x0, y0, cw, cl, wall, width, depth, occupied, give_up: bool = False
):
    """First 0.25-ft cursor on ``wall`` whose footprint clears ``occupied``; the
    run start if none is free (or ``None`` when ``give_up`` and it never fits)."""
    run = cw if wall in ("S", "N") else cl
    cursor = 0.0
    first = None
    while cursor + width <= run + 1e-9:
        rect = _wall_rect(wall, x0, y0, cw, cl, cursor, width, depth)
        if first is None:
            first = rect
        if not any(_rects_overlap(rect, o) for o in occupied):
            return rect
        cursor += 0.25
    if give_up:
        return None
    return first if first is not None else _wall_rect(wall, x0, y0, cw, cl, 0.0, width, depth)


def resolve_room_fixtures(plan: Barndominium, room: Room) -> list[Fixture]:
    """The final fixtures in ``room``: surviving auto-seeds plus authored fixtures.

    Add-vs-replace rule: authored fixtures **add** to the room's auto-seeds, except
    that an authored fixture of a *seeded* kind **replaces** that kind's seed (an
    explicit ``toilet`` moves the toilet; the lavatory/tub seeds stay). Seeds are
    laid first (perimeter walk), then authored fixtures fill the first free slot
    that clears what's already down. Every fixture gets a stable
    ``<room>~<kind>~<i>`` id. Deterministic and side-effect-free.
    """
    explicit = [pf for pf in getattr(plan, "fixtures", []) if pf.room == room.id]
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []

    explicit_kinds = {pf.kind for pf in explicit}
    surviving = [k for k in fixtures_for(room.type) if k not in explicit_kinds]
    placed = _place_perimeter(x0, y0, cw, cl, surviving)

    occupied = [(f.x, f.y, f.width, f.length) for f in placed]
    for pf in explicit:
        if pf.kind not in FIXTURES:
            continue
        f = _place_explicit(room, pf, x0, y0, cw, cl, occupied)
        f.seed = False
        f.source_line = getattr(pf, "line", None)
        placed.append(f)
        occupied.append((f.x, f.y, f.width, f.length))

    counts: dict[str, int] = {}
    for f in placed:
        i = counts.get(f.kind, 0)
        counts[f.kind] = i + 1
        f.id = f"{room.id}~{f.kind}~{i}"
    return placed


def validate_fixtures(plan: Barndominium, add) -> None:
    """Compiler-as-teacher checks on placed fixtures (all non-blocking).

    Two families of check run over the resolved layout (:func:`resolve_room_fixtures`
    — seeds plus authored pieces):

    *Placement* checks judge only **authored** fixtures — the auto-placer fits its
    own seeds, so it shouldn't nag about them: an unknown room (``FIXTURE_ROOM``),
    a footprint past the room (``FIXTURE_OOB``), an overlap (``FIXTURE_OVERLAP``), a
    door swing (``FIXTURE_DOOR``), a water-closet clearance short of IRC R307.1
    (``FIXTURE_TOILET_CLEARANCE``), a blocked clear-floor strip in front
    (``FIXTURE_FRONT``), a fixture in an unusual room type (``FIXTURE_ROOM_TYPE``),
    a wall-backed piece floating mid-floor (``FIXTURE_BACKING``), and a tall piece
    parked over a bedroom's escape window (``FIXTURE_EGRESS``).

    *Plan* checks judge seeds **and** authored pieces, because what they teach is
    about the plan, not the placement: a range under an operable window
    (``RANGE_WINDOW``) or with no landing beside it (``RANGE_LANDING``), a work
    triangle scattered too wide (``KITCHEN_TRIANGLE``), a dryer far from any
    exterior wall to vent through (``DRYER_VENT``), and a fixture on a stair
    footprint (``FIXTURE_STAIR``).
    """
    from .validation import Issue, Severity  # local: validation imports this module

    known = {r.id for r in plan.rooms}
    for pf in getattr(plan, "fixtures", []):
        if pf.room not in known:
            add(
                Issue(
                    Severity.ERROR,
                    "FIXTURE_ROOM",
                    f"Fixture '{pf.kind}' names unknown room '{pf.room}'.",
                    line=getattr(pf, "line", None),
                    col=getattr(pf, "col", None),
                    end_col=getattr(pf, "end_col", None),
                    hint="Place the fixture in a room that exists.",
                )
            )

    for room in plan.rooms:
        fixtures = resolve_room_fixtures(plan, room)
        # OOB is judged against the room rectangle (not the tighter clear box) so a
        # natural corner placement (`at 0,0`) doesn't warn on the wall inset alone.
        rects = [(f.x, f.y, f.width, f.length) for f in fixtures]
        for i, f in enumerate(fixtures):
            if f.seed:
                continue
            if (
                f.x + 1e-6 < room.x
                or f.y + 1e-6 < room.y
                or f.x + f.width - 1e-6 > room.x2
                or f.y + f.length - 1e-6 > room.y2
            ):
                add(
                    Issue(
                        Severity.WARNING,
                        "FIXTURE_OOB",
                        f"Fixture '{f.kind}' in '{room.id}' extends past the room "
                        f"({_fmt(room.width)} × {_fmt(room.length)} ft).",
                        room=room.id,
                        line=f.source_line,
                        hint="Move it inward (its `at` is measured from the room's "
                        "SW corner) or grow the room.",
                    )
                )
            for j, other in enumerate(fixtures):
                if j <= i:
                    continue
                if _rects_overlap(rects[i], rects[j]):
                    add(
                        Issue(
                            Severity.WARNING,
                            "FIXTURE_OVERLAP",
                            f"Fixture '{f.kind}' overlaps '{other.kind}' in "
                            f"'{room.id}'.",
                            room=room.id,
                            line=f.source_line,
                            hint="Nudge one along its wall, or place it against a "
                            "different wall.",
                        )
                    )
                    break
            for door in _door_swing_rects(plan, room):
                if _rects_overlap(rects[i], door):
                    add(
                        Issue(
                            Severity.INFO,
                            "FIXTURE_DOOR",
                            f"Fixture '{f.kind}' in '{room.id}' sits in a door's "
                            "swing.",
                            room=room.id,
                            line=f.source_line,
                            hint="Keep the clear floor in front of the door open; "
                            "slide the fixture clear of the swing.",
                        )
                    )
                    break

        _check_placement(plan, room, fixtures, rects, add, Issue, Severity)
        _check_plan_rules(plan, room, fixtures, add, Issue, Severity)


def _check_placement(plan, room, fixtures, rects, add, Issue, Severity) -> None:
    """The authored-only placement checks (clearance, front, room type, backing,
    egress). Seeds are auto-fitted, so they're skipped here."""
    ext = set(exterior_walls(plan, room))
    windows = [w for w in plan.windows if w.room == room.id]
    for i, f in enumerate(fixtures):
        if f.seed:
            continue
        spec = FIXTURES.get(f.kind)
        if spec is None:
            continue
        others = [rects[j] for j in range(len(fixtures)) if j != i]

        # FIXTURE_TOILET_CLEARANCE — IRC R307.1 (15 in side, 21 in front).
        if f.kind == "toilet" and f.wall in ("S", "N", "E", "W"):
            side, front = _wc_clearances(f, room, others)
            if side + 1e-6 < _WC_SIDE_CLEAR or front + 1e-6 < _WC_FRONT_CLEAR:
                if side + 1e-6 < _WC_SIDE_CLEAR:
                    what = (
                        f"only {side * 12:.0f} in from its centreline to the nearest "
                        "wall or fixture (IRC R307.1 wants 15 in each side)"
                    )
                else:
                    what = (
                        f"only {front * 12:.0f} in of clear floor in front (IRC R307.1 "
                        "wants 21 in)"
                    )
                add(
                    Issue(
                        Severity.WARNING,
                        "FIXTURE_TOILET_CLEARANCE",
                        f"Toilet in '{room.id}' has {what}.",
                        room=room.id,
                        line=f.source_line,
                        hint="Give the toilet a 30 in bay (15 in each side of centre) "
                        "and 21 in of clear floor in front — slide it along the wall "
                        "or widen the room.",
                    )
                )

        # FIXTURE_FRONT — the clear-floor strip a fixture needs in front is blocked.
        # A toilet's front clearance is the R307.1 job of FIXTURE_TOILET_CLEARANCE.
        if f.kind == "toilet":
            pass
        elif spec.free:
            gaps = _side_clearances(f, room, others)
            long_sides = (
                (gaps["N"], gaps["S"]) if f.width >= f.length else (gaps["E"], gaps["W"])
            )
            if max(long_sides) + 1e-6 < 2.0:
                add(
                    Issue(
                        Severity.INFO,
                        "FIXTURE_FRONT",
                        f"Free-standing '{f.kind}' in '{room.id}' has under 2 ft of "
                        "walkway on either long side.",
                        room=room.id,
                        line=f.source_line,
                        hint="Leave ~2 ft to pass on at least one long side, or move "
                        "it toward the room centre.",
                    )
                )
        elif spec.front > 0:
            strip = _front_strip(f, spec.front)
            if strip is not None:
                sx, sy, sw, sl = strip
                cut = (
                    sx + 1e-6 < room.x
                    or sy + 1e-6 < room.y
                    or sx + sw - 1e-6 > room.x2
                    or sy + sl - 1e-6 > room.y2
                )
                blocked = any(_rects_overlap(strip, o) for o in others)
                if cut or blocked:
                    by = "a wall" if cut and not blocked else "another fixture"
                    add(
                        Issue(
                            Severity.INFO,
                            "FIXTURE_FRONT",
                            f"Fixture '{f.kind}' in '{room.id}' has its "
                            f"{_fmt(spec.front)} ft clear-floor strip cut off by {by}.",
                            room=room.id,
                            line=f.source_line,
                            hint="Keep clear floor in front of it — slide it along the "
                            "wall, or place it where nothing crowds its approach.",
                        )
                    )

        # FIXTURE_ROOM_TYPE — a fixture in a surprising room type.
        expected = _EXPECTED_ROOMS.get(f.kind)
        if expected is not None and room.type not in expected:
            names = " / ".join(
                sorted(t.value.replace("_", " ") for t in expected)
            )
            add(
                Issue(
                    Severity.INFO,
                    "FIXTURE_ROOM_TYPE",
                    f"A {f.kind.replace('_', ' ')} in the {room.type.value} "
                    f"'{room.id}' is unusual — intentional?",
                    room=room.id,
                    line=f.source_line,
                    hint=f"A {f.kind.replace('_', ' ')} normally lives in a {names}; "
                    "if this is deliberate, ignore this note.",
                )
            )

        # FIXTURE_BACKING — a wall-backed piece floating off every wall.
        if f.kind in _WALL_BACKED:
            wall_gap = min(
                f.y - room.y,
                room.y2 - (f.y + f.length),
                f.x - room.x,
                room.x2 - (f.x + f.width),
            )
            if wall_gap > _BACKING_FLOAT + 1e-6:
                add(
                    Issue(
                        Severity.INFO,
                        "FIXTURE_BACKING",
                        f"Fixture '{f.kind}' in '{room.id}' floats "
                        f"{_fmt(wall_gap)} ft off every wall.",
                        room=room.id,
                        line=f.source_line,
                        hint=f"A {f.kind.replace('_', ' ')} backs to a wall — back it "
                        "to a wall or use `wall N|S|E|W`.",
                    )
                )

        # FIXTURE_EGRESS — a tall piece parked over a bedroom's escape window.
        if room.type is RoomType.BEDROOM and f.kind in _TALL_KINDS:
            for w in windows:
                if not getattr(w, "escape_capable", True) or w.wall not in ext:
                    continue
                if _rects_overlap(rects[i], _window_reach_rect(room, w, 1.5)):
                    add(
                        Issue(
                            Severity.WARNING,
                            "FIXTURE_EGRESS",
                            f"Fixture '{f.kind}' in bedroom '{room.id}' blocks the "
                            "escape window on its "
                            f"{w.wall.value} wall (IRC R310 emergency egress).",
                            room=room.id,
                            line=f.source_line,
                            hint="Keep the egress window clear — a bedroom must be able "
                            "to escape through it. Move the fixture to another wall.",
                        )
                    )
                    break


def _check_plan_rules(plan, room, fixtures, add, Issue, Severity) -> None:
    """The plan checks that judge seeds and authored pieces alike (range window /
    landing, kitchen triangle, dryer vent, stair)."""
    ext = set(exterior_walls(plan, room))
    windows = [w for w in plan.windows if w.room == room.id]
    stairs = getattr(plan, "stairs", []) or []

    for f in fixtures:
        rect = (f.x, f.y, f.width, f.length)
        line = None if f.seed else f.source_line

        # RANGE_WINDOW — a cooktop directly under an operable window.
        if f.kind == "range":
            for w in windows:
                if not getattr(w, "escape_capable", True) or w.wall not in ext:
                    continue  # a fixed sash doesn't open; interior walls give no code
                if _rects_overlap(rect, _window_reach_rect(room, w, 2.0)):
                    add(
                        Issue(
                            Severity.WARNING,
                            "RANGE_WINDOW",
                            f"The range in '{room.id}' sits under the operable window "
                            f"on its {w.wall.value} wall.",
                            room=room.id,
                            line=line,
                            hint="Don't put a cooktop under an openable window — a "
                            "breeze can blow out a burner and curtains hang over the "
                            "flame. Slide the range along the wall, clear of the sash.",
                        )
                    )
                    break

        # FIXTURE_STAIR — a fixture parked on a stair's footprint.
        for s in stairs:
            if room.level not in (s.from_level, s.to_level):
                continue
            if _rects_overlap(rect, (s.x, s.y, s.width, s.length)):
                add(
                    Issue(
                        Severity.WARNING,
                        "FIXTURE_STAIR",
                        f"Fixture '{f.kind}' in '{room.id}' overlaps the "
                        f"'{s.id}' stair footprint.",
                        room=room.id,
                        line=line,
                        hint="Keep the run and its landing clear — slide the fixture "
                        "off the stair, or move the stair along the wall.",
                    )
                )
                break

        # DRYER_VENT — a dryer with a long duct run to any exterior wall.
        if f.kind == "dryer":
            cx, cy = f.center
            reach = math.inf
            for d in ext:
                if d is Direction.SOUTH:
                    reach = min(reach, cy - room.y)
                elif d is Direction.NORTH:
                    reach = min(reach, room.y2 - cy)
                elif d is Direction.WEST:
                    reach = min(reach, cx - room.x)
                elif d is Direction.EAST:
                    reach = min(reach, room.x2 - cx)
            if reach > _DRYER_VENT_REACH + 1e-6:
                where = (
                    f"~{reach:.0f} ft from the nearest exterior wall"
                    if reach != math.inf
                    else "in a room with no exterior wall"
                )
                add(
                    Issue(
                        Severity.INFO,
                        "DRYER_VENT",
                        f"The dryer in '{room.id}' is {where} — a long vent duct.",
                        room=room.id,
                        line=line,
                        hint="Keep the dryer within ~10 ft of an exterior wall; long, "
                        "bendy ducts trap lint and cut airflow.",
                    )
                )

    # RANGE_LANDING — a cooktop with no landing surface within 1 ft to either side.
    if room.type is RoomType.KITCHEN:
        landings = [
            (f.x, f.y, f.width, f.length)
            for f in fixtures
            if f.kind in _LANDING_KINDS
        ]
        if landings:  # only judge when there's other casework to compare against
            for f in fixtures:
                if f.kind != "range":
                    continue
                rect = (f.x, f.y, f.width, f.length)
                if not any(_rect_gap(rect, land) <= 1.0 + 1e-6 for land in landings):
                    add(
                        Issue(
                            Severity.INFO,
                            "RANGE_LANDING",
                            f"The range in '{room.id}' has no counter, sink or "
                            "refrigerator landing within 1 ft to either side.",
                            room=room.id,
                            line=None if f.seed else f.source_line,
                            hint="NKBA wants a landing surface beside the cooktop — "
                            "put a `fixture counter` next to the range to set hot pans "
                            "down.",
                        )
                    )

        # KITCHEN_TRIANGLE — sink/range/refrigerator scattered too far apart.
        sink = next((f for f in fixtures if f.kind == "sink"), None)
        rng = next((f for f in fixtures if f.kind == "range"), None)
        fridge = next((f for f in fixtures if f.kind == "refrigerator"), None)
        if sink and rng and fridge:
            def _d(a, b):
                (ax, ay), (bx, by) = a.center, b.center
                return math.hypot(ax - bx, ay - by)

            perim = _d(sink, rng) + _d(rng, fridge) + _d(fridge, sink)
            if perim > _TRIANGLE_MAX + 1e-6:
                add(
                    Issue(
                        Severity.INFO,
                        "KITCHEN_TRIANGLE",
                        f"Kitchen '{room.id}' has a {perim:.0f} ft work triangle "
                        f"(sink–range–fridge) — over the ~{_TRIANGLE_MAX:.0f} ft NKBA "
                        "keeps within.",
                        room=room.id,
                        hint="Draw the three closer together (each leg ~4–9 ft) so the "
                        "cook isn't walking marathons between sink, range and fridge.",
                    )
                )


def _fmt(v: float) -> str:
    return f"{v:g}"


def _door_swing_rects(plan: Barndominium, room: Room) -> list:
    """Coarse swing-clearance rectangles for the leaves opening into ``room`` — a
    width-deep band inside each hinged interior door on one of the room's walls."""
    from .geometry import shared_edge

    out = []
    for d in plan.interior_doors:
        if getattr(d, "kind", "swing") not in ("swing", "double", "french"):
            continue
        if room.id not in (d.room_a, d.room_b):
            continue
        other_id = d.room_b if d.room_a == room.id else d.room_a
        other = plan.room(other_id)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        w = min(d.width, edge.length)
        offset = d.offset if d.offset is not None else max(0.0, (edge.length - w) / 2.0)
        start = edge.lo + max(0.0, min(offset, edge.length - w))
        if edge.orientation == "v":  # wall runs north-south at x = edge.pos
            inward = edge.pos < room.center[0]
            bx = edge.pos if inward else edge.pos - w
            out.append((bx, start, w, w))
        else:  # wall runs east-west at y = edge.pos
            inward = edge.pos < room.center[1]
            by = edge.pos if inward else edge.pos - w
            out.append((start, by, w, w))
    return out


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
