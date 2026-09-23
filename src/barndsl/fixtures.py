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
from dataclasses import dataclass, replace

from .elements import Barndominium, Direction, Room, RoomType
from .geometry import door_span, opening_endpoints
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

#: Pieces the wall auto-slot tries CENTRED on their wall before walking for the
#: first free run: a bed wants nightstand room on both sides and a sofa an end
#: table — cornered, they read as parked, not placed.
_CENTER_ON_WALL: frozenset[str] = frozenset({"bed_queen", "bed_twin", "sofa"})

#: The default depth of an ``along`` counter run (into the room). 25 in — 2 ft 1 in
#: — is the US-standard finished countertop depth (24 in cabinet + a 1 in overhang),
#: so an ``along`` run without a ``depth`` uses it. (The catalog ``counter`` keeps
#: its nominal 2 ft for an ``at``-placed piece; ``along`` is the run form.)
ALONG_DEFAULT_DEPTH: float = 25.0 / 12.0
#: The accepted range for a counter run's ``depth`` (ft) — a shallow bar ledge up to
#: a deep island-style run.
ALONG_DEPTH_MIN: float = 1.0
ALONG_DEPTH_MAX: float = 4.0
#: Appliances that read as *set into* a counter run (a top-mount sink, a slide-in
#: range) rather than colliding with it — the FIXTURE_OVERLAP inset exemption. A
#: refrigerator or dishwasher is not inset (it stands proud), so it isn't here.
_INSET_KINDS: frozenset[str] = frozenset({"sink", "range"})
#: How much of an inset appliance's footprint must lie within a counter to read as
#: set into it (its centre must also fall inside the run).
_INSET_MIN_FRACTION: float = 0.70

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

#: Room types where a counter run reads as odd (``COUNTER_ROOM`` info). Counters
#: are at home in a kitchen, pantry, bath (vanity), laundry, mudroom, office, shop
#: — this is the deliberately-small set where one is surprising enough to flag.
_COUNTER_ODD_ROOMS: frozenset[RoomType] = frozenset(
    {RoomType.BEDROOM, RoomType.CLOSET, RoomType.HALLWAY, RoomType.LOFT}
)

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
    # Kitchen: the range leads its wall (clear of a centred window, where a cooktop
    # doesn't belong), then a landing to the sink, then the tall refrigerator in a
    # bay of its own — never butted straight against the cooktop. See
    # _KITCHEN_SEED_GAPS for the spread.
    RoomType.KITCHEN: ["range", "sink", "refrigerator"],
    RoomType.LAUNDRY: ["washer", "dryer"],
}

#: Landing gaps (ft) the kitchen seed leaves *before* each appliance, so the three
#: spread with working counter between them rather than stacking in a butted row
#: (an "un-buildable" layout an architect flagged). The range→sink gap stays inside
#: a cook's landing reach so RANGE_LANDING still sees a landing beside the cooktop;
#: the refrigerator gets a wider bay of its own, well clear of the range. The range
#: leads (no gap) so it sits at the working end rather than under a centred window.
#: Deterministic, keyed by the fixture the gap precedes; non-kitchens keep the
#: tight butted seed.
_KITCHEN_SEED_GAPS: dict[str, float] = {"sink": 0.75, "refrigerator": 1.5}


def _seed_gaps(room_type: RoomType) -> dict[str, float] | None:
    """The per-appliance lead gaps the perimeter placer uses for ``room_type`` (a
    kitchen spreads its appliances; every other room butts its seeds)."""
    return _KITCHEN_SEED_GAPS if room_type is RoomType.KITCHEN else None


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


#: How far (ft) to nudge a fixture along its wall when a door/opening blocks it.
_KEEPOUT_STEP = 0.5
#: Depth of doorway/opening keepouts into a room. This keeps wall-backed
#: appliances from landing in a cased opening/doorway and preserves traffic flow.
_OPENING_KEEPOUT_DEPTH = 3.5
#: Depth of window keepouts for seed/auto-slotted wall fixtures. A sink under a
#: window can be legitimate, but the generic seed placer can't know that intent;
#: keeping default appliances off glazing avoids worse range/fridge-at-window
#: failures and authors can still override deliberately with `fixture ... at`.
_WINDOW_KEEPOUT_DEPTH = 2.5


def _rects_overlap(a, b, tol: float = 1e-6) -> bool:
    ax, ay, aw, al = a
    bx, by, bw, bl = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + al, by + bl) - max(ay, by)
    return dx > tol and dy > tol


def _rect_intersection(a, b):
    """The overlap rectangle ``(x, y, w, l)`` of two boxes, or ``None`` if they
    don't overlap."""
    ax, ay, aw, al = a
    bx, by, bw, bl = b
    ix = max(ax, bx)
    iy = max(ay, by)
    iw = min(ax + aw, bx + bw) - ix
    il = min(ay + al, by + bl) - iy
    if iw <= 1e-9 or il <= 1e-9:
        return None
    return (ix, iy, iw, il)


def _counter_depth(f: "Fixture") -> float:
    """A counter's depth into the room — its shorter footprint side (the run is the
    longer side)."""
    return min(f.width, f.length)


def _is_mitred_corner(a: "Fixture", b: "Fixture") -> bool:
    """True if counters ``a`` and ``b`` meet only in a corner square — an L/U join,
    not a collinear double-stack. The overlap must fit within ``depth × depth`` of
    the deeper run (a corner square), which two parallel runs overlapping along
    their length never do."""
    if a.kind != "counter" or b.kind != "counter":
        return False
    inter = _rect_intersection(
        (a.x, a.y, a.width, a.length), (b.x, b.y, b.width, b.length)
    )
    if inter is None:
        return False
    _ix, _iy, iw, il = inter
    dmax = max(_counter_depth(a), _counter_depth(b))
    return iw <= dmax + 1e-6 and il <= dmax + 1e-6


def miter_counters(room: Room, counters: list) -> tuple[list, list[tuple[float, float, float, float]]]:
    """Resolve mitred counter corners for drawing: ``(counters, miters)``.

    For each L/U join the later run is trimmed back to abut the earlier one
    along its run axis, and the 45° miter diagonal is returned across the corner
    square (from the square's outer corner — farthest from the room's centre —
    to its inner one). The model keeps the full overlapping rectangles (the
    takeoff counts the corner in both runs); only the drawing is trimmed, the
    same way in the SVG plan and the DXF export."""
    adjusted = list(counters)
    miters: list[tuple[float, float, float, float]] = []
    rcx, rcy = room.x + room.width / 2.0, room.y + room.length / 2.0
    for i in range(len(adjusted)):
        for j in range(i + 1, len(adjusted)):
            a, b = adjusted[i], adjusted[j]
            if not _is_mitred_corner(a, b):
                continue
            inter = _rect_intersection(
                (a.x, a.y, a.width, a.length), (b.x, b.y, b.width, b.length)
            )
            if inter is None:
                continue
            ix, iy, iw, il = inter
            # Trim b away from the joint along its run axis (its wall's axis;
            # a free-standing run falls back to its longer side).
            axis = (
                "x" if b.wall in ("S", "N")
                else "y" if b.wall in ("W", "E")
                else ("x" if b.width >= b.length else "y")
            )
            if axis == "x":
                if (ix - b.x) <= (b.x + b.width) - (ix + iw):  # joint at low-x end
                    nb = replace(b, x=ix + iw, width=b.width - iw)
                else:
                    nb = replace(b, width=b.width - iw)
            else:
                if (iy - b.y) <= (b.y + b.length) - (iy + il):  # joint at low-y end
                    nb = replace(b, y=iy + il, length=b.length - il)
                else:
                    nb = replace(b, length=b.length - il)
            adjusted[j] = nb
            # Miter diagonal: outer corner = the square's corner farthest from
            # the room centre (the walls' meeting corner), to its opposite.
            corners = [(ix, iy), (ix + iw, iy), (ix, iy + il), (ix + iw, iy + il)]
            outer = max(corners, key=lambda c: (c[0] - rcx) ** 2 + (c[1] - rcy) ** 2)
            inner = (ix + iw - (outer[0] - ix), iy + il - (outer[1] - iy))
            miters.append((outer[0], outer[1], inner[0], inner[1]))
    return adjusted, miters


def _inset_pair(a: "Fixture", b: "Fixture"):
    """If one of ``a``/``b`` is an inset appliance (sink/range) set *into* the other
    counter, return ``(appliance, counter)``; else ``None``. "Set into" means the
    appliance's centre lies inside the counter and ≥ :data:`_INSET_MIN_FRACTION` of
    its footprint overlaps."""
    for app, counter in ((a, b), (b, a)):
        if app.kind not in _INSET_KINDS or counter.kind != "counter":
            continue
        inter = _rect_intersection(
            (app.x, app.y, app.width, app.length),
            (counter.x, counter.y, counter.width, counter.length),
        )
        if inter is None:
            continue
        _ix, _iy, iw, il = inter
        area = app.width * app.length
        cx, cy = app.center
        centre_in = (
            counter.x - 1e-6 <= cx <= counter.x + counter.width + 1e-6
            and counter.y - 1e-6 <= cy <= counter.y + counter.length + 1e-6
        )
        if centre_in and area > 0 and (iw * il) / area >= _INSET_MIN_FRACTION - 1e-9:
            return app, counter
    return None


def _overlap_exempt(a: "Fixture", b: "Fixture") -> bool:
    """Whether an overlap between ``a`` and ``b`` is a legitimate kitchen join —
    a mitred counter corner (L/U) or an appliance set into a counter run — rather
    than a real collision. Collinear/parallel double-stacked counters and any
    non-inset piece over a counter are NOT exempt."""
    return _is_mitred_corner(a, b) or _inset_pair(a, b) is not None


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
    gaps: dict[str, float] | None = None,
) -> list[Fixture]:
    """Lay ``kinds`` along the clear-box perimeter (longer wall first), wrapping to
    the next wall when the current one runs out — the deterministic seed layout.

    ``keepouts`` are ``(x, y, w, l)`` door-swing / doorway rectangles a fixture
    must stay clear of; a blocked spot slides the fixture along the wall until it
    clears (or wraps to the next wall), so the placer never parks a fixture in a
    door's arc or across a cased opening.
    ``gaps`` maps a kind to a landing gap (ft) left *before* it on the same wall —
    how the kitchen seed spreads its appliances (a wrap to a new wall drops the
    gap, so a fixture still starts at the corner)."""
    walls = _walls(cw, cl)
    placed: list[Fixture] = []
    wi = 0
    cursor = 0.0
    for kind in kinds:
        spec = FIXTURES[kind]
        if gaps:
            cursor += gaps.get(kind, 0.0)  # a landing gap before this fixture
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
        if wall is None or fx is None or fy is None or fw is None or fl is None:
            break  # ran out of perimeter; the fit check reports the shortfall
        placed.append(Fixture(kind, fx, fy, fw, fl, wall))
        cursor += spec.width
    return placed


#: The longest alcove a seed tub stretches to span wall-to-wall (72 in tubs are
#: stock; past that the catalog tub anchors to a corner instead of stretching).
_TUB_ALCOVE_MAX = 6.0


def _alcove_tub(x0: float, y0: float, cw: float, cl: float, keepouts) -> Fixture | None:
    """A tub fitted like real construction: spanning the clear box's SHORT
    dimension wall-to-wall at one end of the room (an alcove), touching the side
    walls, rather than floating as a strip along the long wall. Stretches the
    catalog 5 ft tub up to :data:`_TUB_ALCOVE_MAX` to close the alcove. Returns
    ``None`` when the room is too narrow/too wide for an alcove or a door swing
    blocks both ends — the perimeter walk then places the tub as before."""
    spec = FIXTURES["tub"]
    span = min(cw, cl)
    if span + 1e-6 < spec.width or span > _TUB_ALCOVE_MAX + 1e-6:
        return None
    if cw <= cl:  # the alcove spans x: the tub sits across the N or S end
        ends = (
            ("N", (x0, y0 + cl - spec.depth, span, spec.depth)),
            ("S", (x0, y0, span, spec.depth)),
        )
    else:  # spans y: across the E or W end
        ends = (
            ("E", (x0 + cw - spec.depth, y0, spec.depth, span)),
            ("W", (x0, y0, spec.depth, span)),
        )
    for wall, (fx, fy, fw, fl) in ends:
        if not any(_rects_overlap((fx, fy, fw, fl), b) for b in keepouts):
            return Fixture("tub", fx, fy, fw, fl, wall)
    return None


def _place_seeds(
    x0: float, y0: float, cw: float, cl: float, kinds: list[str],
    keepouts: tuple = (), gaps: dict[str, float] | None = None,
) -> list[Fixture]:
    """The deterministic seed layout: an alcove tub first (when the room affords
    one), then the perimeter walk for everything else with the tub as a keepout."""
    placed: list[Fixture] = []
    if "tub" in kinds:
        tub = _alcove_tub(x0, y0, cw, cl, keepouts)
        if tub is not None:
            placed.append(tub)
            kinds = [k for k in kinds if k != "tub"]
            keepouts = tuple(keepouts) + ((tub.x, tub.y, tub.width, tub.length),)
    placed.extend(_place_perimeter(x0, y0, cw, cl, kinds, keepouts, gaps))
    return placed


def plan_room_fixtures(plan: Barndominium, room: Room, *, avoid_doors: bool = True) -> list[Fixture]:
    """Place ``room``'s **auto-seed** fixtures against its walls (deterministic).

    The historical seed placement (bath/kitchen/laundry), kept for callers that
    only want the auto-seeds. The combined authored-plus-seed layout the exchange,
    the plan drawing and the 3D model consume is :func:`resolve_room_fixtures`.

    ``avoid_doors`` (the default) slides fixtures clear of every hinged door's
    swing and every doorway/cased opening; pass ``False`` for the door-blind placement the swing-crowding check
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
    return _place_seeds(x0, y0, cw, cl, kinds, keepouts, _seed_gaps(room.type))


def _quarter_turns(rotation: float) -> int:
    """A rotation in degrees snapped to a plan quarter-turn count (0..3). The
    massing is axis-aligned, so a fixture turns in 90° steps; other angles snap to
    the nearest, keeping the plan glyph and the 3D box consistent."""
    return int(round((rotation or 0.0) / 90.0)) % 4


def _place_along(room: Room, pf) -> Fixture:
    """Resolve an ``along`` counter run to a world :class:`Fixture`.

    The run backs to ``pf.along``'s wall and spans it from ``run_from`` to ``run_to``
    (room-local feet from the wall's south/west start corner; the full wall when
    both are ``None``), projecting ``run_depth`` (or :data:`ALONG_DEFAULT_DEPTH`)
    into the room. The result is an ordinary wall-backed footprint, so every
    downstream consumer treats it exactly like an ``at``-placed counter."""
    wall = pf.along.name[0]  # S | N | E | W
    depth = pf.run_depth if pf.run_depth is not None else ALONG_DEFAULT_DEPTH
    run = room.width if wall in ("S", "N") else room.length
    a = 0.0 if pf.run_from is None else float(pf.run_from)
    b = run if pf.run_to is None else float(pf.run_to)
    length = max(0.0, b - a)  # the along-wall dimension
    if wall == "S":
        fx, fy, fw, fl = room.x + a, room.y, length, depth
    elif wall == "N":
        fx, fy, fw, fl = room.x + a, room.y2 - depth, length, depth
    elif wall == "W":
        fx, fy, fw, fl = room.x, room.y + a, depth, length
    else:  # E
        fx, fy, fw, fl = room.x2 - depth, room.y + a, depth, length
    return Fixture(pf.kind, fx, fy, fw, fl, wall)


def _place_explicit(
    room: Room, pf, x0: float, y0: float, cw: float, cl: float, occupied: list
) -> Fixture:
    """Resolve one authored :class:`~barndsl.elements.PlacedFixture` to a world
    :class:`Fixture`, honouring its ``at``/``wall``/``rotate`` (or auto-placing)."""
    if getattr(pf, "along", None) is not None:
        return _place_along(room, pf)
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

    if wall and getattr(pf, "offset", None) is not None:
        # `wall <W> offset <n>`: pinned n ft along the wall from its S/W start
        # corner (the door/window convention), judged against the room rectangle
        # like an `at` placement — so FIXTURE_OOB/FIXTURE_DOOR see it as authored.
        off = float(pf.offset)
        if wall == "S":
            fx, fy, fw, fl = room.x + off, room.y, width, min(depth, room.length)
        elif wall == "N":
            d_in = min(depth, room.length)
            fx, fy, fw, fl = room.x + off, room.y2 - d_in, width, d_in
        elif wall == "W":
            d_in = min(depth, room.width)
            fx, fy, fw, fl = room.x, room.y + off, d_in, width
        else:  # E
            d_in = min(depth, room.width)
            fx, fy, fw, fl = room.x2 - d_in, room.y + off, d_in, width
        return Fixture(pf.kind, fx, fy, fw, fl, wall, rotation=pf.rotation)

    if wall:
        # A bed or sofa reads best CENTRED on its wall (nightstand/end-table room
        # on both sides), so try the centred slot first; anything in the way —
        # another fixture or a door swing — falls back to the free-slot walk.
        if pf.kind in _CENTER_ON_WALL:
            run = cw if wall in ("S", "N") else cl
            if width <= run + 1e-9:
                rect = _wall_rect(
                    wall, x0, y0, cw, cl, (run - width) / 2.0, width, depth
                )
                if not any(_rects_overlap(rect, o) for o in occupied):
                    rx, ry, rw, rl = rect
                    return Fixture(pf.kind, rx, ry, rw, rl, wall, rotation=pf.rotation)
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
    that clears what's already down. Door swings count as occupied for both walks
    — a seed and an auto-slotted authored piece slide clear of every hinged
    door's arc and every doorway/cased opening; only an explicit ``at x,y`` can
    park a fixture in one (and the ``FIXTURE_DOOR``/``FIXTURE_OPENING`` checks
    flag it). Every fixture gets a stable
    ``<room>~<kind>~<i>`` id. Deterministic and side-effect-free.
    """
    explicit = [pf for pf in getattr(plan, "fixtures", []) if pf.room == room.id]
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []

    explicit_kinds = {pf.kind for pf in explicit}
    surviving = [k for k in fixtures_for(room.type) if k not in explicit_kinds]
    keepouts = (
        tuple(_door_swing_rects(plan, room))
        + tuple(_opening_keepout_rects(plan, room))
        + tuple(_window_keepout_rects(plan, room))
    )
    placed = _place_seeds(
        x0, y0, cw, cl, surviving, keepouts, _seed_gaps(room.type)
    )

    occupied = [(f.x, f.y, f.width, f.length) for f in placed] + list(keepouts)
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
    about the plan, not the placement: a fixture across a doorway/opening
    (``FIXTURE_OPENING``), an authored range under an operable window
    (``RANGE_WINDOW``) or with no landing beside it (``RANGE_LANDING``), an authored work
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
                if _rects_overlap(rects[i], rects[j]) and not _overlap_exempt(f, other):
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
                            Severity.WARNING,
                            "FIXTURE_DOOR",
                            f"Fixture '{f.kind}' in '{room.id}' sits in a door's "
                            "swing — the leaf hits it.",
                            room=room.id,
                            line=f.source_line,
                            hint="Keep the clear floor in front of the door open: "
                            "slide the fixture clear of the swing, or make the "
                            "door pocket/sliding.",
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

        # COUNTER_ROOM — a counter in a room type where it reads as odd.
        if f.kind == "counter" and room.type in _COUNTER_ODD_ROOMS:
            add(
                Issue(
                    Severity.INFO,
                    "COUNTER_ROOM",
                    f"A counter in the {room.type.value} '{room.id}' is unusual — "
                    "a run of casework reads as a kitchen/utility surface.",
                    room=room.id,
                    line=f.source_line,
                    hint="Counters live in a kitchen, pantry, bath, laundry or shop; "
                    "if this is a deliberate bar or work ledge, ignore this note.",
                )
            )

        # COUNTER_DOOR — a counter run crosses a doorway/opening/entry on its wall.
        if f.kind == "counter" and f.wall in ("S", "N", "E", "W"):
            if f.wall in ("S", "N"):
                clo, chi = f.x, f.x + f.width
            else:
                clo, chi = f.y, f.y + f.length
            for label, olo, ohi in _openings_on_wall(plan, room, f.wall):
                if min(chi, ohi) - max(clo, olo) > 1e-6:
                    add(
                        Issue(
                            Severity.WARNING,
                            "COUNTER_DOOR",
                            f"The counter run in '{room.id}' crosses {label} on its "
                            f"{f.wall} wall.",
                            room=room.id,
                            line=f.source_line,
                            hint="A run can't span a doorway — stop it short with "
                            "`from`/`to` so it clears the opening, or move the run to "
                            "another wall.",
                        )
                    )
                    break

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

        # FIXTURE_OPENING — a wall-backed piece installed across a doorway/cased
        # opening. Auto-seeds and wall-slotted fixtures avoid these bands; an
        # explicit `at` placement or an impossible room still gets called out.
        if f.kind in _WALL_BACKED and f.wall in ("S", "N", "E", "W"):
            for label, keepout in _fixture_opening_keepouts(plan, room, f.wall):
                if _rects_overlap(rect, keepout):
                    add(
                        Issue(
                            Severity.WARNING,
                            "FIXTURE_OPENING",
                            f"Fixture '{f.kind}' in '{room.id}' sits across {label} on its {f.wall} wall.",
                            room=room.id,
                            line=line,
                            hint="Keep appliances/casework out of doorways and cased openings: slide it along a solid wall with backing/services, or move the opening.",
                        )
                    )
                    break

        # RANGE_WINDOW — a cooktop directly under an operable window. Authored
        # ranges are judged; default seeds are schematic unless the author pins
        # them with a fixture line.
        if f.kind == "range" and not f.seed:
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
                if f.kind != "range" or f.seed:
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

        # SINK_NO_COUNTER — a kitchen sink not set into any counter run. Only fires
        # once the kitchen has counters (like RANGE_LANDING, we don't nag a bare
        # seed-only kitchen); a bath lavatory has its own vanity, so it's not judged.
        counters = [f for f in fixtures if f.kind == "counter"]
        if counters:
            for f in fixtures:
                if f.kind != "sink":
                    continue
                if not any(_inset_pair(f, c) is not None for c in counters):
                    add(
                        Issue(
                            Severity.INFO,
                            "SINK_NO_COUNTER",
                            f"The sink in '{room.id}' isn't set into any counter run.",
                            room=room.id,
                            line=None if f.seed else f.source_line,
                            hint="A kitchen sink wants counter to each side — extend a "
                            "`fixture counter ... along <wall>` run past the sink so "
                            "it drops into the countertop.",
                        )
                    )
                    break

        # KITCHEN_TRIANGLE — sink/range/refrigerator scattered too far apart.
        sink = next((f for f in fixtures if f.kind == "sink"), None)
        rng = next((f for f in fixtures if f.kind == "range"), None)
        fridge = next((f for f in fixtures if f.kind == "refrigerator"), None)
        if sink and rng and fridge and not (sink.seed or rng.seed or fridge.seed):
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
    width-deep band inside each hinged door on one of the room's walls, interior
    partitions and exterior entries alike (an overhead door rides its tracks, a
    pocket/sliding leaf stays in the wall and a bifold folds flat against its
    jambs, so none of those reserves an arc)."""
    from .geometry import shared_edge

    out = []
    for ext_door in plan.exterior_doors:
        if ext_door.room != room.id or getattr(ext_door, "overhead", False):
            continue
        if getattr(ext_door, "kind", "entry") in ("pocket", "sliding"):
            continue
        w = ext_door.width
        x1, y1, x2, y2 = opening_endpoints(room, ext_door.wall, ext_door.offset, ext_door.width)
        if ext_door.wall is Direction.SOUTH:
            out.append((min(x1, x2), room.y, w, min(w, room.length)))
        elif ext_door.wall is Direction.NORTH:
            d_in = min(w, room.length)
            out.append((min(x1, x2), room.y2 - d_in, w, d_in))
        elif ext_door.wall is Direction.WEST:
            out.append((room.x, min(y1, y2), min(w, room.width), w))
        else:  # EAST
            d_in = min(w, room.width)
            out.append((room.x2 - d_in, min(y1, y2), d_in, w))
    for int_door in plan.interior_doors:
        if getattr(int_door, "kind", "swing") not in ("swing", "double", "french"):
            continue
        if room.id not in (int_door.room_a, int_door.room_b):
            continue
        other_id = int_door.room_b if int_door.room_a == room.id else int_door.room_a
        other = plan.room(other_id)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        start, end = door_span(edge, int_door)
        w = end - start
        if edge.orientation == "v":  # wall runs north-south at x = edge.pos
            inward = edge.pos < room.center[0]
            bx = edge.pos if inward else edge.pos - w
            out.append((bx, start, w, w))
        else:  # wall runs east-west at y = edge.pos
            inward = edge.pos < room.center[1]
            by = edge.pos if inward else edge.pos - w
            out.append((start, by, w, w))
    return out


def _window_keepout_rects(plan: Barndominium, room: Room) -> list[tuple[float, float, float, float]]:
    """Window bands the default wall-fixture placer should avoid.

    This is a placement heuristic, not a diagnostic: explicit authored fixtures
    may still choose a window wall and the existing plan checks decide whether it
    is acceptable (e.g. ``RANGE_WINDOW`` for a cooktop under operable glass).
    """
    ext = set(exterior_walls(plan, room))
    return [
        _window_reach_rect(room, w, _WINDOW_KEEPOUT_DEPTH)
        for w in plan.windows
        if w.room == room.id and w.wall in ext
    ]


def _opening_keepout_rects(plan: Barndominium, room: Room) -> list[tuple[float, float, float, float]]:
    """Doorway/cased-opening bands a wall-backed fixture must not occupy.

    Door swings catch only hinged leaves. A cased opening has no leaf, but a
    range, sink, fridge, washer, dryer or cabinet still cannot be installed in
    that wall gap: it has no backing/services and blocks circulation. These
    shallow bands make the auto-placer slide past openings and let explicit
    placements be diagnosed by ``FIXTURE_OPENING``.
    """
    return [rect for _label, rect in _fixture_opening_keepouts(plan, room)]


def _fixture_opening_keepouts(
    plan: Barndominium, room: Room, wall_filter: str | None = None
) -> list[tuple[str, tuple[float, float, float, float]]]:
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    depth = _OPENING_KEEPOUT_DEPTH
    walls = (wall_filter,) if wall_filter is not None else ("S", "N", "E", "W")
    for wall in walls:
        for label, lo, hi in _openings_on_wall(plan, room, wall):
            span = max(0.0, hi - lo)
            if span <= 0:
                continue
            if wall == "S":
                rect = (lo, room.y, span, min(depth, room.length))
            elif wall == "N":
                d = min(depth, room.length)
                rect = (lo, room.y2 - d, span, d)
            elif wall == "W":
                rect = (room.x, lo, min(depth, room.width), span)
            else:  # E
                d = min(depth, room.width)
                rect = (room.x2 - d, lo, d, span)
            out.append((label, rect))
    return out


def _openings_on_wall(plan: Barndominium, room: Room, wall: str) -> list[tuple[str, float, float]]:
    """Every doorway/opening/entry on ``room``'s ``wall`` (S/N/E/W), as
    ``(label, lo, hi)`` world intervals along that wall — x for S/N walls, y for
    W/E. Used by ``COUNTER_DOOR`` to see when a counter run crosses an opening."""
    from .geometry import shared_edge

    dir_map = {
        "S": Direction.SOUTH, "N": Direction.NORTH,
        "W": Direction.WEST, "E": Direction.EAST,
    }
    wd = dir_map[wall]
    horiz = wall in ("S", "N")
    wall_pos = {"S": room.y, "N": room.y2, "W": room.x, "E": room.x2}[wall]
    out: list[tuple[str, float, float]] = []
    for d in plan.exterior_doors:
        if d.room != room.id or d.wall is not wd:
            continue
        base = room.x if horiz else room.y
        lo = base + d.offset
        label = (
            "the overhead door" if d.overhead
            else "the entry" if d.egress else "the exterior door"
        )
        out.append((label, lo, lo + d.width))
    for idoor in plan.interior_doors:
        if room.id not in (idoor.room_a, idoor.room_b):
            continue
        other = plan.room(idoor.room_b if idoor.room_a == room.id else idoor.room_a)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        want = "h" if horiz else "v"
        if edge.orientation != want or abs(edge.pos - wall_pos) > 1e-6:
            continue
        lo, hi = door_span(edge, idoor)
        label = (
            "the cased opening" if getattr(idoor, "kind", "swing") == "cased"
            else f"the door to '{other.id}'"
        )
        out.append((label, lo, hi))
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
