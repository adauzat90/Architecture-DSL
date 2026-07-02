"""Lower a :class:`~barndsl.elements.Barndominium` into a **Revit-shaped exchange**.

barndsl models a plan as *rectangles* (rooms) in a feet-based 2D coordinate
system. Revit models a building as **levels + walls (line segments) + hosted
families (doors/windows) + rooms derived from wall enclosure + structural
framing**. This module is the bridge: it turns the rectangle IR into the data a
Revit front-end needs to *build* the model, and serialises it to a stable JSON
document (the ``barndsl.revit/1`` exchange).

It is **pure Python** — no Revit, no .NET, no API key — so it runs and is tested
anywhere. The companion pyRevit extension (a ribbon button inside Revit) reads
the JSON this produces and instantiates the elements via the Revit API.

Coordinate / unit mapping
-------------------------
barndsl's convention (``x`` east, ``y`` north, feet) maps **directly** onto
Revit's world XY plane, and Revit's internal unit is already the decimal foot,
so coordinates pass through unchanged. ``z`` (elevation) is derived from the
floor level. Walls are emitted as **centrelines on the room-rectangle edges**;
the consuming script chooses the wall types (and thus thicknesses), using the
``exterior`` flag and the nominal ``thickness`` hint carried here.

The wall set is **deduplicated**: a partition shared by two rooms becomes a
single wall, not two coincident ones. Walls are derived per level by decomposing
every room edge along its grid line into atomic segments, classifying each as
*interior* (a room on both sides) or *exterior* (open footprint on the far
side), then merging contiguous like-classified segments back into runs.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from .constants import (
    DEFAULT_ROOF_PITCH,
    EXTERIOR_WALL_THICKNESS,
    FOOTING_DEPTH,
    FOOTING_SIZE,
    INTERIOR_WALL_THICKNESS,
    PLUMBING_WALL_THICKNESS,
    MAX_RISER_HEIGHT,
    MIN_STAIR_WIDTH,
    MIN_TREAD_DEPTH,
    NICE_STAIR_WIDTH,
    SLAB_THICKNESS,
    TURNDOWN_DEPTH,
    TURNDOWN_WIDTH,
)
from .elements import (
    DOUBLE_LEAF_KINDS,
    WINDOW_KINDS,
    Barndominium,
    Direction,
    Room,
    feet,
)
from .fixtures import plan_room_fixtures
from .geometry import (
    TOL,
    footprint_area,
    footprint_boundary,
    opening_endpoints,
    point_in_footprint,
    shared_edge,
)
from .validation import clear_dimensions

# --- defaults the rectangle IR doesn't carry ---------------------------------
#: Head height of a standard door leaf (interior or exterior): 6'-8".
DEFAULT_DOOR_HEIGHT = feet(6.667)
#: Height of a doorless cased opening (walk-through); taller than a leaf door.
DEFAULT_CASED_HEIGHT = feet(7.0)
# Nominal wall thicknesses (EXTERIOR_WALL_THICKNESS / INTERIOR_WALL_THICKNESS)
# are defined once in constants.py and imported above — the exchange is
# centreline-based, so they don't move geometry; they hint the wall-type pick
# ("exterior" → a 2x6 shell, "interior" → a 2x4 partition) and derive the clear
# dimensions the validator and Revit's room schedule both report.

#: A short distance used to probe just past a wall to decide if its far side is
#: outside the footprint (and the wall therefore exterior).
_PROBE = 0.05

EXCHANGE_SCHEMA = "barndsl.revit/1"

# Stair geometry (IRC R311.7) is shared with the validator: defined once in
# constants.py (imported at the top of this module) and re-exported here, so
# ``barndsl.revit.MIN_STAIR_WIDTH`` etc. keep resolving for callers and tests.

# DEFAULT_ROOF_PITCH is defined once in constants.py and imported above, so the
# roof planner and the validator agree on the gable pitch.


# --- exchange dataclasses ----------------------------------------------------


@dataclass
class RevitLevel:
    """A Revit level: a named horizontal datum at an elevation."""

    index: int
    name: str
    elevation: float
    height: float


@dataclass
class RevitWall:
    """A wall centreline segment hosted on a level.

    ``start``/``end`` are ``(x, y)`` feet; the wall runs from one to the other at
    constant ``z`` (the level elevation), ``height`` feet tall. ``exterior``
    drives wall-type selection in the consumer; ``thickness`` is a nominal hint.
    """

    id: str
    level: int
    start: tuple[float, float]
    end: tuple[float, float]
    height: float
    exterior: bool
    thickness: float
    #: Top profile: ``"flat"`` (a plate-height rectangle, the default) or
    #: ``"gable"`` for a gable-end wall whose top rises to the roof ridge. A gable
    #: wall carries :attr:`apex` (the ridge point in plan) and :attr:`apex_height`
    #: (feet above the wall base), so the consumer can build the pentagon profile.
    profile: str = "flat"
    apex: tuple[float, float] | None = None
    apex_height: float = 0.0
    #: Declared wall kind from a matching `wall` statement: ``"plumbing"``,
    #: ``"bearing"`` or ``"rated"`` (one of :data:`_WALL_KIND_PRECEDENCE` when a
    #: statement declares several). ``None`` — and the JSON key absent — for an
    #: ordinary wall, so old documents are byte-identical. Lets the consumer's
    #: ``config.json`` map declared walls to real named wall types.
    kind: str | None = None

    @property
    def orientation(self) -> str:
        """``"v"`` if the wall runs north-south, ``"h"`` if east-west."""
        return "v" if abs(self.end[0] - self.start[0]) <= TOL else "h"

    @property
    def const_coord(self) -> float:
        """The coordinate held constant along the wall (x if vertical, y if h)."""
        return self.start[0] if self.orientation == "v" else self.start[1]

    @property
    def span(self) -> tuple[float, float]:
        """``(lo, hi)`` of the wall along its running axis."""
        if self.orientation == "v":
            return (min(self.start[1], self.end[1]), max(self.start[1], self.end[1]))
        return (min(self.start[0], self.end[0]), max(self.start[0], self.end[0]))


@dataclass
class RevitOpening:
    """A door or window to instantiate as a family hosted on a wall.

    ``location`` is the centre of the opening in plan. ``host_wall`` is the id of
    the :class:`RevitWall` it sits on (``None`` if no wall matched — the consumer
    can re-host by nearest wall). ``rooms`` lists the room(s) the opening serves
    (two for an interior door/cased opening, one for an exterior door/window).
    """

    id: str
    category: str  # "door" | "window" | "cased_opening"
    #: Doors: swing | pocket | sliding | double | french | cased | exterior |
    #: overhead. Windows: the window kind (casement | slider | fixed |
    #: double-hung; older documents wrote the generic "window", which imports
    #: as the casement default).
    kind: str
    level: int
    location: tuple[float, float]
    width: float
    height: float
    sill: float
    exterior: bool
    egress: bool
    rooms: list[str]
    host_wall: str | None
    #: The room id the leaf swings *into* (interior swing doors only; ``None``
    #: leaves the consumer at the family default). Optional/additive — an old
    #: ``barndsl.revit/1`` document without it still loads.
    swing_into: str | None = None
    #: Hinge end: ``"near"`` (the south/west end of the opening) or ``"far"``.
    hinge: str | None = None


@dataclass
class RevitRoom:
    """A seed for a Revit Room: a point inside the enclosed region + metadata.

    Revit creates a room by placing it at a point within a wall-bounded loop.
    ``point`` is the room centre; the consumer places the room there once the
    walls exist, then sets its name/number. The source rectangle
    ``(x, y, width, length)`` rides along so the exchange can be reconstructed
    back into a plan (see :func:`exchange_to_plan`).
    """

    id: str
    name: str
    type: str
    level: int
    point: tuple[float, float]
    area: float
    x: float
    y: float
    width: float
    length: float
    #: Built **clear** (finish-face) interior — the nominal rectangle minus half of
    #: each bounding wall. This is what Revit computes for a placed room's area, so
    #: carrying it lets the exchange and the Revit room schedule report one number.
    clear_width: float = 0.0
    clear_length: float = 0.0
    clear_area: float = 0.0
    #: Finished ceiling height (ft) for this room — its own override or the plan
    #: default — and whether it's vaulted (open to the roof, no flat ceiling).
    ceiling_height: float = 0.0
    vaulted: bool = False


@dataclass
class RevitColumn:
    """A structural post (column) at a point on a level.

    ``base``/``top`` are the world elevations (feet) of the post's bottom and top:
    the floor level and the **plate** (level + ceiling), so the consumer can give
    the column a real height that rises to the beam it carries rather than a
    default stub.
    """

    point: tuple[float, float]
    size: float
    role: str
    level: int
    base: float = 0.0
    top: float = 0.0


@dataclass
class RevitFraming:
    """A structural beam centreline (bent or ridge) on a level.

    ``z`` is the world elevation (feet) the member sits at — the **plate** for a
    bent, or the plate plus the roof rise for the ridge — so the beam is drawn up
    at the top of the posts, not down on the floor.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    role: str
    level: int
    z: float = 0.0
    #: Nominal square section (ft). In this MVP the ridge/bent beams share the
    #: frame's nominal post section (member sizing is the engineer's job); the
    #: consumer can duplicate-and-size a framing type from it like it does for
    #: columns. ``0`` means no size hint (old exchanges).
    size: float = 0.0


@dataclass
class RevitSlab:
    """A floor slab on a level, from a footprint rectangle."""

    level: int
    x: float
    y: float
    width: float
    length: float


@dataclass
class RevitArea:
    """A non-enclosed reference outline (porch slab / stair footprint)."""

    id: str
    kind: str
    x: float
    y: float
    width: float
    length: float
    level: int
    meta: dict = field(default_factory=dict)


@dataclass
class RevitFixture:
    """A fixture/appliance seed: a footprint rectangle + the room and wall it
    serves, for the consumer to host a loadable family at its centre."""

    id: str
    kind: str  # toilet | lavatory | tub | shower | sink | range | refrigerator
    room: str
    level: int
    x: float
    y: float
    width: float
    length: float
    wall: str
    point: tuple[float, float]


@dataclass
class RevitModel:
    """The full Revit-shaped exchange for one plan."""

    name: str
    ceiling_height: float
    floor_depth: float
    envelope_width: float
    envelope_length: float
    wings: list[tuple[float, float, float, float]]
    levels: list[RevitLevel]
    walls: list[RevitWall]
    openings: list[RevitOpening]
    rooms: list[RevitRoom]
    columns: list[RevitColumn]
    framing: list[RevitFraming]
    areas: list[RevitArea]
    slabs: list[RevitSlab]
    grids: list[dict]
    roof: dict | None
    fixtures: list[RevitFixture] = field(default_factory=list)
    foundation: dict | None = None
    orientation: float = 0.0
    siding: str | None = None
    roofing: str | None = None
    #: Optional lot block ``{"width", "length", "setbacks": {...}}`` carried from
    #: a declared ``site``/``setback``. ``None`` — and the JSON key absent — when
    #: no site is declared, so undeclared documents stay byte-identical and the
    #: builder can place the model relative to a survey point when it is present.
    site: dict | None = None

    def to_dict(self) -> dict:
        """A JSON-serialisable dict — the ``barndsl.revit/1`` exchange document."""
        return {
            "schema": EXCHANGE_SCHEMA,
            "units": "feet",
            "plan": {
                "name": self.name,
                "ceiling_height": self.ceiling_height,
                "floor_depth": self.floor_depth,
                "floor_to_floor": self.ceiling_height + self.floor_depth,
                "envelope_width": self.envelope_width,
                "envelope_length": self.envelope_length,
                "wings": [list(w) for w in self.wings],
                "orientation": self.orientation,
                "siding": self.siding,
                "roofing": self.roofing,
            },
            "levels": [asdict(l) for l in self.levels],
            "walls": [
                {
                    "id": w.id,
                    "level": w.level,
                    "start": list(w.start),
                    "end": list(w.end),
                    "height": w.height,
                    "exterior": w.exterior,
                    "thickness": w.thickness,
                    "profile": w.profile,
                    "apex": list(w.apex) if w.apex is not None else None,
                    "apex_height": w.apex_height,
                    # Only a declared wall carries a kind; the key is absent
                    # otherwise so undeclared documents stay byte-identical.
                    **({"kind": w.kind} if w.kind is not None else {}),
                }
                for w in self.walls
            ],
            "openings": [
                {
                    "id": o.id,
                    "category": o.category,
                    "kind": o.kind,
                    "level": o.level,
                    "location": list(o.location),
                    "width": o.width,
                    "height": o.height,
                    "sill": o.sill,
                    "exterior": o.exterior,
                    "egress": o.egress,
                    "rooms": list(o.rooms),
                    "host_wall": o.host_wall,
                    "swing_into": o.swing_into,
                    "hinge": o.hinge,
                }
                for o in self.openings
            ],
            "rooms": [
                {
                    "id": r.id,
                    "name": r.name,
                    "type": r.type,
                    "level": r.level,
                    "point": list(r.point),
                    "area": r.area,
                    "x": r.x,
                    "y": r.y,
                    "width": r.width,
                    "length": r.length,
                    "clear_width": r.clear_width,
                    "clear_length": r.clear_length,
                    "clear_area": r.clear_area,
                    "ceiling_height": r.ceiling_height,
                    "vaulted": r.vaulted,
                }
                for r in self.rooms
            ],
            "structure": {
                "columns": [
                    {
                        "point": list(c.point),
                        "size": c.size,
                        "role": c.role,
                        "level": c.level,
                        "base": c.base,
                        "top": c.top,
                    }
                    for c in self.columns
                ],
                "framing": [
                    {
                        "start": list(f.start),
                        "end": list(f.end),
                        "role": f.role,
                        "level": f.level,
                        "z": f.z,
                        "size": f.size,
                    }
                    for f in self.framing
                ],
            },
            "areas": [asdict(a) for a in self.areas],
            "slabs": [asdict(s) for s in self.slabs],
            "grids": list(self.grids),
            "roof": self.roof,
            "foundation": self.foundation,
            "fixtures": [
                {
                    "id": fx.id,
                    "kind": fx.kind,
                    "room": fx.room,
                    "level": fx.level,
                    "x": fx.x,
                    "y": fx.y,
                    "width": fx.width,
                    "length": fx.length,
                    "wall": fx.wall,
                    "point": list(fx.point),
                }
                for fx in self.fixtures
            ],
            # Only a declared site carries a block; the key is absent otherwise so
            # undeclared documents stay byte-identical.
            **({"site": self.site} if self.site is not None else {}),
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --- wall extraction ---------------------------------------------------------


def _walls_one_axis(rooms, sections, vertical, height, level, idgen, declared=()):
    """Derive deduplicated wall runs along one axis for a single level.

    ``vertical`` selects walls at constant ``x`` (running north-south); otherwise
    constant ``y`` (running east-west). Returns a list of :class:`RevitWall`.

    For each grid line, room edges contribute a room on the ``+`` side (its low
    edge lies on the line) and/or the ``-`` side (its high edge lies on the
    line). Each atomic interval between consecutive breakpoints is a wall when a
    room touches it; it is *interior* when rooms sit on both sides and *exterior*
    when the empty side falls outside the footprint. Contiguous like-classified
    intervals merge into one wall — "like-classified" includes the declared
    wall attributes (``declared``, from :func:`_declared_edges`), so a
    plumbing/rated/bearing segment starts its own run instead of tagging (or
    being swallowed by) a colinear neighbour.
    """
    # Accessors that swap meaning between the two axes. "line" is the constant
    # coordinate of an edge; "run" is the coordinate it varies along.
    if vertical:
        line_lo = lambda r: r.x  # noqa: E731 - room's low edge on the line axis
        line_hi = lambda r: r.x2
        run_lo = lambda r: r.y
        run_hi = lambda r: r.y2
    else:
        line_lo = lambda r: r.y
        line_hi = lambda r: r.y2
        run_lo = lambda r: r.x
        run_hi = lambda r: r.x2

    lines = sorted({round(line_lo(r), 6) for r in rooms} | {round(line_hi(r), 6) for r in rooms})
    walls: list[RevitWall] = []

    for c in lines:
        # Rooms whose interior is on the + side of the line (low edge == c), and
        # rooms whose interior is on the - side (high edge == c).
        pos = [r for r in rooms if abs(line_lo(r) - c) <= TOL]
        neg = [r for r in rooms if abs(line_hi(r) - c) <= TOL]
        if not (pos or neg):
            continue
        bps = sorted({round(run_lo(r), 6) for r in pos + neg} | {round(run_hi(r), 6) for r in pos + neg})

        run = None  # (lo, hi, exterior) accumulator for the current merged run
        for a, b in zip(bps, bps[1:]):
            mid = (a + b) / 2.0
            on_pos = any(run_lo(r) - TOL <= mid <= run_hi(r) + TOL for r in pos)
            on_neg = any(run_lo(r) - TOL <= mid <= run_hi(r) + TOL for r in neg)
            seg = None
            if on_pos or on_neg:
                attrs = frozenset()
                if on_pos and on_neg:
                    exterior = False  # partition between two rooms
                    attrs = _segment_attrs(declared, c, mid)
                else:
                    # Probe just past the empty side; outside the footprint ⇒ exterior.
                    if vertical:
                        px = c - _PROBE if on_pos else c + _PROBE
                        py = mid
                    else:
                        px = mid
                        py = c - _PROBE if on_pos else c + _PROBE
                    exterior = not point_in_footprint(sections, px, py)
                seg = (a, b, exterior, attrs)

            if seg is None:
                if run is not None:
                    walls.append(_make_wall(idgen, level, vertical, c, run, height))
                    run = None
                continue
            if (
                run is not None
                and run[2:] == seg[2:]  # same classification AND declared attrs
                and abs(run[1] - seg[0]) <= TOL
            ):
                run = (run[0], seg[1], seg[2], seg[3])  # extend the contiguous run
            else:
                if run is not None:
                    walls.append(_make_wall(idgen, level, vertical, c, run, height))
                run = seg
        if run is not None:
            walls.append(_make_wall(idgen, level, vertical, c, run, height))

    return walls


def _make_wall(idgen, level, vertical, c, run, height):
    lo, hi, exterior, attrs = run
    if vertical:
        start, end = (c, lo), (c, hi)
    else:
        start, end = (lo, c), (hi, c)
    thickness = EXTERIOR_WALL_THICKNESS if exterior else INTERIOR_WALL_THICKNESS
    kind = next((k for k in _WALL_KIND_PRECEDENCE if k in attrs), None)
    if "plumbing" in attrs:
        thickness = max(thickness, PLUMBING_WALL_THICKNESS)
    return RevitWall(
        id=next(idgen),
        level=level,
        start=(float(start[0]), float(start[1])),
        end=(float(end[0]), float(end[1])),
        height=float(height),
        exterior=exterior,
        thickness=thickness,
        kind=kind,
    )


def _extract_walls(plan: Barndominium, level: int, height: float, idgen) -> list[RevitWall]:
    rooms = [r for r in plan.rooms if getattr(r, "level", 0) == level]
    sections = plan.footprint_sections()
    walls = _walls_one_axis(
        rooms, sections, True, height, level, idgen, _declared_edges(plan, level, True)
    )
    walls += _walls_one_axis(
        rooms, sections, False, height, level, idgen, _declared_edges(plan, level, False)
    )
    return walls


#: When one `wall` statement declares several attributes the exchange carries a
#: single ``kind`` — picked in this order (life-safety first, then structure,
#: then plumbing), documented so the mapping is deterministic.
_WALL_KIND_PRECEDENCE = ("rated", "bearing", "plumbing")


def _declared_edges(plan: Barndominium, level: int, vertical: bool) -> list[tuple]:
    """The declared ``wall`` statements' shared edges on one level and axis.

    Returns ``(const_coord, lo, hi, attributes)`` tuples so the wall extraction
    can tag each *atomic* segment before contiguous runs merge — a declared
    kind therefore never bleeds past its own shared edge onto a colinear
    neighbour's wall, and two statements on distinct colinear edges each keep
    their own kind. Overlapping declarations on the same physical edge union
    their attributes (the exchange's single ``kind`` then follows
    :data:`_WALL_KIND_PRECEDENCE`, so ``rated`` beats ``plumbing`` regardless
    of declaration order).
    """
    want = "v" if vertical else "h"
    out = []
    for ws in getattr(plan, "wall_specs", None) or []:
        a, b = plan.room(ws.room_a), plan.room(ws.room_b)
        if a is None or b is None or a.id == b.id:
            continue  # WALL_REF's problem
        if getattr(a, "level", 0) != level or getattr(b, "level", 0) != level:
            continue
        edge = shared_edge(a, b)
        if edge is None or edge.orientation != want:
            continue  # WALL_NOADJ's problem (or the other axis)
        if ws.attributes:
            out.append((edge.pos, edge.lo, edge.hi, frozenset(ws.attributes)))
    return out


def _segment_attrs(declared: list[tuple], c: float, mid: float) -> frozenset:
    """The union of declared attributes covering one atomic segment."""
    attrs: set[str] = set()
    for pos, lo, hi, a in declared:
        if abs(pos - c) <= 1e-4 and lo - TOL <= mid <= hi + TOL:
            attrs |= a
    return frozenset(attrs)


# --- opening hosting ---------------------------------------------------------


def _find_host(walls: list[RevitWall], orientation: str, pos: float, lo: float, hi: float):
    """The wall id whose line carries the opening span ``[lo, hi]``.

    Prefers a wall that fully contains the span; falls back to the one on the
    same line with the greatest overlap. Returns ``None`` if nothing lines up.
    """
    best = None
    best_overlap = TOL
    for w in walls:
        if w.orientation != orientation or abs(w.const_coord - pos) > 1e-4:
            continue
        wlo, whi = w.span
        if wlo - TOL <= lo and hi <= whi + TOL:
            return w.id  # fully contained — the unambiguous host
        overlap = min(whi, hi) - max(wlo, lo)
        if overlap > best_overlap:
            best_overlap, best = overlap, w.id
    return best


def _opening_geometry(plan, opening, kind):
    """Return ``(orientation, pos, lo, hi, level)`` for an opening in plan."""
    if kind == "interior":
        a = plan.room(opening.room_a)
        b = plan.room(opening.room_b)
        if a is None or b is None:
            return None
        edge = shared_edge(a, b)
        if edge is None:
            return None
        width = min(opening.width, edge.length)
        if opening.offset is None:
            lo = edge.lo + (edge.length - width) / 2.0
        else:
            lo = edge.lo + opening.offset
        return edge.orientation, edge.pos, lo, lo + width, getattr(a, "level", 0)
    # exterior door or window: room + wall + offset + width
    room = plan.room(opening.room)
    if room is None:
        return None
    x1, y1, x2, y2 = opening_endpoints(room, opening.wall, opening.offset, opening.width)
    if opening.wall in (Direction.SOUTH, Direction.NORTH):
        return "h", y1, min(x1, x2), max(x1, x2), getattr(room, "level", 0)
    return "v", x1, min(y1, y2), max(y1, y2), getattr(room, "level", 0)


def _location(orientation, pos, lo, hi):
    mid = (lo + hi) / 2.0
    return (pos, mid) if orientation == "v" else (mid, pos)


# --- stair run planning ------------------------------------------------------


def plan_stair_runs(x: float, y: float, width: float, length: float, rise: float) -> dict:
    """Plan the flights of a stair that climbs ``rise`` feet within a footprint.

    Deterministic and **pure** (no Revit): given the footprint rectangle and the
    total rise, derive the riser count (≤ 7¾ in each), the run length needed
    (treads × 10 in), then choose a layout that fits:

    * **straight** — one flight, when the run fits the footprint's long axis;
    * **switchback** — two parallel flights with a landing at the turn, when the
      straight run is too long but the footprint is wide enough for two flights;
    * **overrun** — a single straight flight that exceeds the footprint, when
      neither fits (``fits=False``, so the consumer can flag it).

    Returns ``{risers, riser_height, tread, layout, fits, runs, landings}``.
    Each run is ``{start, end, width, risers}`` with ``start``/``end`` in feet and
    the path direction pointing *up* the flight; landings are ``{x,y,width,length}``
    rectangles. Coordinates are in the plan's world frame.
    """
    rise = abs(float(rise))
    risers = max(1, int(math.ceil(rise / MAX_RISER_HEIGHT))) if rise > 0 else 1
    riser_h = rise / risers if risers else 0.0
    treads = max(1, risers - 1)
    run_needed = treads * MIN_TREAD_DEPTH

    # Work in a local frame: `along` is the footprint's long axis, `across` the
    # short one. Map back to world (x, y) via the axis unit vectors.
    if length >= width:
        along_vec, across_vec = (0.0, 1.0), (1.0, 0.0)
        long_dim, short_dim = length, width
    else:
        along_vec, across_vec = (1.0, 0.0), (0.0, 1.0)
        long_dim, short_dim = width, length

    def pt(along, across):
        return [
            x + along_vec[0] * along + across_vec[0] * across,
            y + along_vec[1] * along + across_vec[1] * across,
        ]

    tol = 1e-6
    runs: list[dict] = []
    landings: list[dict] = []

    if run_needed <= long_dim + tol:
        layout, fits = "straight", True
        run_w = min(short_dim, NICE_STAIR_WIDTH)
        start = (long_dim - run_needed) / 2.0
        across_c = short_dim / 2.0
        runs.append(
            {"start": pt(start, across_c), "end": pt(start + run_needed, across_c),
             "width": run_w, "risers": risers}
        )
    elif short_dim + tol >= 2 * MIN_STAIR_WIDTH and run_needed / 2.0 <= long_dim + tol:
        layout, fits = "switchback", True
        half = run_needed / 2.0
        run_w = min(short_dim / 2.0, NICE_STAIR_WIDTH)
        start = (long_dim - half) / 2.0
        across1, across2 = short_dim / 4.0, 3.0 * short_dim / 4.0
        r1 = (risers + 1) // 2
        r2 = risers - r1
        # Flight 1 ascends to the far end; flight 2 returns, parallel, offset across.
        runs.append(
            {"start": pt(start, across1), "end": pt(start + half, across1),
             "width": run_w, "risers": r1}
        )
        if r2 > 0:
            runs.append(
                {"start": pt(start + half, across2), "end": pt(start, across2),
                 "width": run_w, "risers": r2}
            )
        # Landing at the turn: spans the short dimension at the far end of the run.
        land_lo = pt(start + half - run_w, 0.0)
        runlen_w, runlen_l = (short_dim, run_w) if length >= width else (run_w, short_dim)
        landings.append(
            {"x": min(land_lo[0], pt(start + half, short_dim)[0]),
             "y": min(land_lo[1], pt(start + half, short_dim)[1]),
             "width": runlen_w, "length": runlen_l}
        )
    else:
        layout, fits = "overrun", False
        run_w = min(short_dim, NICE_STAIR_WIDTH)
        start = max(0.0, (long_dim - run_needed) / 2.0)
        across_c = short_dim / 2.0
        runs.append(
            {"start": pt(start, across_c), "end": pt(start + run_needed, across_c),
             "width": run_w, "risers": risers}
        )

    return {
        "risers": risers,
        "riser_height": riser_h,
        "tread": MIN_TREAD_DEPTH,
        "layout": layout,
        "fits": fits,
        "runs": runs,
        "landings": landings,
    }


# --- roof & structural grids -------------------------------------------------


def roof_plan(plan: Barndominium, top_level: int, pitch: float = DEFAULT_ROOF_PITCH) -> dict:
    """A gable-roof plan over the building's bounding box (pure, no Revit).

    The ridge runs the **long** axis at the centre; the two eaves are the long
    edges. ``rise`` is the ridge height above the eaves for the given ``pitch``
    (rise:run) over half the short span. The outline is the bounding rectangle —
    a simplification for L/T/U footprints (a gable over the bounds), which the
    builder can refine. ``slope_angle`` is the eave-edge slope in **radians**, and
    ``outline_slopes`` is a bool per outline segment flagging the eave edges (the
    ones the builder makes slope-defining). Returns ``{top_level, pitch, rise,
    slope_angle, ridge, eaves, outline, outline_slopes, gable_axis}``; coordinates
    are ``[x, y]`` in feet.
    """
    style = getattr(plan, "roof_style", "gable")
    override = getattr(plan, "roof_pitch", None)
    if override:
        pitch = float(override)
    minx, miny, maxx, maxy = plan.bounds()
    w, l = maxx - minx, maxy - miny
    long_is_y = l >= w
    span = min(w, l)
    # A gable/monitor peaks at the centre over half the span; a shed rises across
    # the full span to one high eave.
    rise = (span * pitch) if style == "shed" else (span / 2.0) * pitch
    slope_angle = math.atan(pitch)
    if long_is_y:
        mid = (minx + maxx) / 2.0
        ridge = {"start": [mid, miny], "end": [mid, maxy]}
        eaves = [
            {"start": [minx, miny], "end": [minx, maxy]},
            {"start": [maxx, miny], "end": [maxx, maxy]},
        ]
        gable_axis = "y"
    else:
        mid = (miny + maxy) / 2.0
        ridge = {"start": [minx, mid], "end": [maxx, mid]}
        eaves = [
            {"start": [minx, miny], "end": [maxx, miny]},
            {"start": [minx, maxy], "end": [maxx, maxy]},
        ]
        gable_axis = "x"
    outline = [
        [[minx, miny], [maxx, miny]],
        [[maxx, miny], [maxx, maxy]],
        [[maxx, maxy], [minx, maxy]],
        [[minx, maxy], [minx, miny]],
    ]
    # An outline edge is an *eave* (slope-defining) when it runs parallel to the
    # ridge (the long axis); the gable ends run perpendicular and stay vertical.
    def _is_eave(seg):
        (sx, sy), (ex, ey) = seg
        horizontal = abs(ey - sy) <= TOL
        return horizontal if gable_axis == "x" else not horizontal

    outline_slopes = [_is_eave(seg) for seg in outline]
    if style == "shed":
        # A shed slopes as one plane from a low eave up to a high eave; only the
        # low eave is slope-defining (the other long edge is the high wall).
        seen_eave = False
        for i, is_eave in enumerate(outline_slopes):
            if is_eave and not seen_eave:
                seen_eave = True  # keep the first eave slope-defining
            elif is_eave:
                outline_slopes[i] = False
    return {
        "top_level": top_level,
        "style": style,
        "pitch": pitch,
        "rise": rise,
        "slope_angle": slope_angle,
        "ridge": ridge,
        "eaves": eaves,
        "outline": outline,
        "outline_slopes": outline_slopes,
        "gable_axis": gable_axis,
    }


def foundation_plan(plan: Barndominium) -> dict:
    """A monolithic slab-on-grade foundation for the footprint (pure, no Revit).

    Derives the slab outline (the footprint sections at grade), a **thickened
    perimeter edge** (turndown / grade beam) traced around the footprint boundary
    with a width and a depth below the slab, and a **pad footing** under each post
    of a placed frame. Also totals the rough concrete volume for a takeoff.
    Returns ``{top, slab_thickness, sections, edge, footings, concrete_yd3}`` with
    ``[x, y]`` / ``[x, y, w, l]`` coordinates in feet; ``top`` is the slab top at
    the ground finished floor (elevation 0).

    The depths are conservative defaults, not an engineered design — the frost
    line and soil report set the real ones.
    """
    sections = plan.footprint_sections()
    boundary = footprint_boundary(sections)
    edge = {
        "width": TURNDOWN_WIDTH,
        "depth": TURNDOWN_DEPTH,
        "segments": [[list(a), list(b)] for a, b in boundary],
    }
    footings = [
        {
            "point": [float(p.x), float(p.y)],
            "size": FOOTING_SIZE,
            "depth": FOOTING_DEPTH,
            "role": p.role,
        }
        for p in plan.posts
    ]

    slab_area = footprint_area(sections)
    perimeter = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in boundary)
    slab_vol = slab_area * SLAB_THICKNESS
    turndown_vol = perimeter * TURNDOWN_WIDTH * TURNDOWN_DEPTH
    footing_vol = sum(FOOTING_SIZE * FOOTING_SIZE * FOOTING_DEPTH for _ in footings)
    concrete_yd3 = (slab_vol + turndown_vol + footing_vol) / 27.0

    return {
        "top": 0.0,
        "slab_thickness": SLAB_THICKNESS,
        "sections": [[float(x), float(y), float(w), float(length)] for x, y, w, length in sections],
        "edge": edge,
        "footings": footings,
        "concrete_yd3": concrete_yd3,
    }


def structural_grids(plan: Barndominium) -> list[dict]:
    """Structural grid lines derived from a placed ``frame`` (pure, no Revit).

    Bents become **numbered** grids (``1, 2, …``) along the building's long axis,
    each coincident with its bent; the eave walls plus any interior support-post
    line become **lettered** grids (``A, B, …``) across. Empty when no frame is
    placed. Each grid is ``{label, start, end}`` with ``[x, y]`` endpoints.
    """
    bents = [b for b in plan.beams if b.role == "frame"]
    if not bents:
        return []
    minx, miny, maxx, maxy = plan.bounds()
    long_is_y = (maxy - miny) >= (maxx - minx)
    grids: list[dict] = []

    def station(b):
        return (b.y1 + b.y2) / 2.0 if long_is_y else (b.x1 + b.x2) / 2.0

    for i, b in enumerate(sorted(bents, key=station), start=1):
        grids.append({"label": str(i), "start": [b.x1, b.y1], "end": [b.x2, b.y2]})

    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    if long_is_y:
        coords = {round(minx, 4), round(maxx, 4)}
        coords.update(round(p.x, 4) for p in plan.posts if p.role == "interior" and minx < p.x < maxx)
        for i, cx in enumerate(sorted(coords)):
            grids.append({"label": letters[i % len(letters)], "start": [cx, miny], "end": [cx, maxy]})
    else:
        coords = {round(miny, 4), round(maxy, 4)}
        coords.update(round(p.y, 4) for p in plan.posts if p.role == "interior" and miny < p.y < maxy)
        for i, cy in enumerate(sorted(coords)):
            grids.append({"label": letters[i % len(letters)], "start": [minx, cy], "end": [maxx, cy]})
    return grids


def _mark_gable_walls(walls: list[RevitWall], roof: dict, plate_height: float) -> None:
    """Tag the top-level exterior walls on the gable ends with a gable profile.

    A gable-end wall runs perpendicular to the ridge at either end of it; its top
    rises from the eave plate to the ridge. We mark each such wall ``profile =
    "gable"`` and carry the apex (the ridge point in plan) and its height above
    the wall base, so the consumer can build the pentagon-profiled wall instead of
    a flat rectangle capped at the plate. Eave and interior walls stay flat.
    """
    gable_axis = roof["gable_axis"]
    apex_h = float(plate_height) + float(roof["rise"])
    ridge = roof["ridge"]
    if gable_axis == "y":
        # Ridge runs north-south; gable ends are the east-west ("h") walls.
        ridge_x = ridge["start"][0]
        ends = {round(ridge["start"][1], 6), round(ridge["end"][1], 6)}
        want, apex_from = "h", lambda w, a: (a, w.const_coord)
        ridge_c = ridge_x
    else:
        # Ridge runs east-west; gable ends are the north-south ("v") walls.
        ridge_y = ridge["start"][1]
        ends = {round(ridge["start"][0], 6), round(ridge["end"][0], 6)}
        want, apex_from = "v", lambda w, a: (w.const_coord, a)
        ridge_c = ridge_y
    for w in walls:
        if not w.exterior or w.orientation != want:
            continue
        if round(w.const_coord, 6) not in ends:
            continue
        lo, hi = w.span
        apex_along = min(max(ridge_c, lo), hi)  # clamp the apex into the wall run
        w.profile = "gable"
        w.apex = (float(apex_from(w, apex_along)[0]), float(apex_from(w, apex_along)[1]))
        w.apex_height = apex_h


# --- the public entry points -------------------------------------------------


def to_revit_model(plan: Barndominium) -> RevitModel:
    """Lower ``plan`` to a :class:`RevitModel` (the Revit-shaped exchange)."""
    height = plan.ceiling_height
    level_indexes = plan.levels()
    levels = [
        RevitLevel(
            index=i,
            name=f"Level {i + 1}",
            # Stack by floor-to-floor (ceiling + inter-floor assembly), so an
            # upper level sits on the structure below it, not on its ceiling plane.
            elevation=float(plan.level_elevation(i)),
            height=float(height),
        )
        for i in level_indexes
    ]

    # Stable id generators (w0, w1, … / o0, o1, …) for deterministic output.
    def _ids(prefix):
        n = 0
        while True:
            yield f"{prefix}{n}"
            n += 1

    wall_ids = _ids("w")
    walls: list[RevitWall] = []
    walls_by_level: dict[int, list[RevitWall]] = {}
    for i in level_indexes:
        lvl_walls = _extract_walls(plan, i, height, wall_ids)
        walls_by_level[i] = lvl_walls
        walls.extend(lvl_walls)
    # Declared wall attributes (`wall a - b plumbing|bearing|rated`) were
    # applied per atomic segment inside the extraction, so kinds never bleed
    # across colinear neighbours and each declaration keeps its own run.

    # Openings, hosted onto the walls just derived.
    op_ids = _ids("o")
    openings: list[RevitOpening] = []

    for d in plan.interior_doors:
        geom = _opening_geometry(plan, d, "interior")
        if geom is None:
            continue
        orientation, pos, lo, hi, lvl = geom
        host = _find_host(walls_by_level.get(lvl, []), orientation, pos, lo, hi)
        cased = d.kind == "cased"
        openings.append(
            RevitOpening(
                id=next(op_ids),
                category="cased_opening" if cased else "door",
                kind=d.kind,
                level=lvl,
                location=_location(orientation, pos, lo, hi),
                width=float(hi - lo),
                height=DEFAULT_CASED_HEIGHT if cased else DEFAULT_DOOR_HEIGHT,
                sill=0.0,
                exterior=False,
                # InteriorDoor has no egress concept (egress routes through
                # exterior doors/windows), so interior openings are never egress.
                egress=False,
                rooms=[d.room_a, d.room_b],
                host_wall=host,
                # Carry the authored swing side/hinge so the consumer can flip
                # the built instance instead of landing at the family default.
                swing_into=d.swing_into,
                hinge=d.hinge,
            )
        )

    for xd in plan.exterior_doors:
        geom = _opening_geometry(plan, xd, "exterior")
        if geom is None:
            continue
        orientation, pos, lo, hi, lvl = geom
        host = _find_host(walls_by_level.get(lvl, []), orientation, pos, lo, hi)
        xkind = getattr(xd, "kind", "entry")
        overhead = xkind == "overhead"
        if overhead:
            out_kind = "overhead"  # so the consumer picks a garage-door family
        elif xkind in DOUBLE_LEAF_KINDS:
            out_kind = xkind  # "double"/"french": a two-leaf exterior pair
        else:
            out_kind = "exterior"  # a single people-door, the historical kind
        openings.append(
            RevitOpening(
                id=next(op_ids),
                category="door",
                kind=out_kind,
                level=lvl,
                location=_location(orientation, pos, lo, hi),
                width=float(hi - lo),
                height=(
                    float(xd.height)
                    if overhead and xd.height is not None
                    else DEFAULT_DOOR_HEIGHT
                ),
                sill=0.0,
                exterior=True,
                # An overhead door is never an egress route (entrance() forces
                # the flag; the kind guard covers a hand-built door too).
                egress=bool(xd.egress) and not overhead,
                rooms=[xd.room],
                host_wall=host,
            )
        )

    for w in plan.windows:
        geom = _opening_geometry(plan, w, "exterior")
        if geom is None:
            continue
        orientation, pos, lo, hi, lvl = geom
        host = _find_host(walls_by_level.get(lvl, []), orientation, pos, lo, hi)
        openings.append(
            RevitOpening(
                id=next(op_ids),
                category="window",
                # The window kind (casement/slider/fixed/double-hung) folds into
                # `kind` so the consumer can map families; the category stays
                # "window", which is what the builder switches on.
                kind=getattr(w, "kind", "casement"),
                level=lvl,
                location=_location(orientation, pos, lo, hi),
                width=float(hi - lo),
                height=float(max(0.0, w.head_height - w.sill_height)),
                sill=float(w.sill_height),
                exterior=True,
                egress=False,
                rooms=[w.room],
                host_wall=host,
            )
        )

    rooms = []
    for r in plan.rooms:
        clear_w, clear_l = clear_dimensions(plan, r)
        rooms.append(
            RevitRoom(
                id=r.id,
                name=r.display_name,
                type=r.type.value,
                level=getattr(r, "level", 0),
                point=(float(r.center[0]), float(r.center[1])),
                area=float(r.area),
                x=float(r.x),
                y=float(r.y),
                width=float(r.width),
                length=float(r.length),
                clear_width=float(clear_w),
                clear_length=float(clear_l),
                clear_area=float(clear_w * clear_l),
                ceiling_height=float(
                    r.ceiling_height if getattr(r, "ceiling_height", None) is not None
                    else height
                ),
                vaulted=bool(getattr(r, "vaulted", False)),
            )
        )

    # Structure sits at the top of the storey, not on the floor: posts rise from
    # the level to the plate (level + ceiling), bents span at the plate, and the
    # ridge rides a roof-rise above it. Precompute the roof rise so the ridge lands
    # at the true apex.
    roof_rise = roof_plan(plan, max(level_indexes))["rise"] if plan.rooms else 0.0

    def _plate_z(lvl: int) -> float:
        return float(plan.level_elevation(lvl)) + float(height)

    columns = [
        RevitColumn(
            point=(float(p.x), float(p.y)),
            size=float(p.size),
            role=p.role,
            level=getattr(p, "level", 0),
            base=float(plan.level_elevation(getattr(p, "level", 0))),
            top=_plate_z(getattr(p, "level", 0)),
        )
        for p in plan.posts
    ]
    # The frame carries no per-beam section; in this MVP the ridge/bent beams
    # share the posts' nominal square section (a 6x6 frame gets 6x6 beams), so
    # the consumer can size a framing type the way it sizes columns.
    beam_size = max((float(p.size) for p in plan.posts), default=0.0)
    framing = [
        RevitFraming(
            start=(float(b.x1), float(b.y1)),
            end=(float(b.x2), float(b.y2)),
            role=b.role,
            level=getattr(b, "level", 0),
            z=_plate_z(getattr(b, "level", 0))
            + (float(roof_rise) if b.role == "ridge" else 0.0),
            size=beam_size,
        )
        for b in plan.beams
    ]

    areas: list[RevitArea] = []
    for p in plan.porches:
        areas.append(
            RevitArea(
                id=p.id,
                kind="porch",
                x=float(p.x),
                y=float(p.y),
                width=float(p.width),
                length=float(p.length),
                level=0,
                meta={"covered": bool(p.covered)},
            )
        )
    for s in plan.stairs:
        rise = float(height) * abs(s.to_level - s.from_level)
        stair_plan = plan_stair_runs(s.x, s.y, s.width, s.length, rise)
        areas.append(
            RevitArea(
                id=s.id,
                kind="stair",
                x=float(s.x),
                y=float(s.y),
                width=float(s.width),
                length=float(s.length),
                level=s.from_level,
                meta={
                    "from_level": s.from_level,
                    "to_level": s.to_level,
                    "rise": rise,
                    "plan": stair_plan,
                },
            )
        )

    # Floor slabs: the footprint at ground, each upper level's room extent above.
    slabs: list[RevitSlab] = []
    for sx, sy, sw, sl in plan.footprint_sections():
        slabs.append(RevitSlab(0, float(sx), float(sy), float(sw), float(sl)))
    for lvl in level_indexes:
        if lvl == 0:
            continue
        rs = [r for r in plan.rooms if getattr(r, "level", 0) == lvl]
        if not rs:
            continue
        x0 = min(r.x for r in rs)
        y0 = min(r.y for r in rs)
        slabs.append(
            RevitSlab(lvl, float(x0), float(y0),
                      float(max(r.x2 for r in rs) - x0), float(max(r.y2 for r in rs) - y0))
        )

    grids = structural_grids(plan)
    top_level = max(level_indexes)
    roof = roof_plan(plan, top_level) if plan.rooms else None
    if roof is not None and roof.get("style") != "shed":
        # A shed has no gable ends (both short walls stay at the plate/rake); a
        # gable and a monitor peak, so their end walls rise to the ridge.
        _mark_gable_walls(walls_by_level.get(top_level, []), roof, height)

    foundation = foundation_plan(plan) if plan.rooms else None

    fixtures: list[RevitFixture] = []
    for r in plan.rooms:
        for fx in plan_room_fixtures(plan, r):
            fixtures.append(
                RevitFixture(
                    id=f"{r.id}_{fx.kind}",
                    kind=fx.kind,
                    room=r.id,
                    level=getattr(r, "level", 0),
                    x=float(fx.x),
                    y=float(fx.y),
                    width=float(fx.width),
                    length=float(fx.length),
                    wall=fx.wall,
                    point=(float(fx.center[0]), float(fx.center[1])),
                )
            )

    return RevitModel(
        name=plan.name,
        ceiling_height=float(height),
        floor_depth=float(plan.floor_depth),
        envelope_width=float(plan.envelope_width),
        envelope_length=float(plan.envelope_length),
        wings=[(float(w.x), float(w.y), float(w.width), float(w.length)) for w in plan.wings],
        slabs=slabs,
        grids=grids,
        roof=roof,
        levels=levels,
        walls=walls,
        openings=openings,
        rooms=rooms,
        columns=columns,
        framing=framing,
        areas=areas,
        fixtures=fixtures,
        foundation=foundation,
        orientation=float(getattr(plan, "orientation", 0.0)),
        siding=getattr(plan, "siding", None),
        roofing=getattr(plan, "roofing", None),
        site=_site_block(plan),
    )


def _site_block(plan: Barndominium) -> dict | None:
    """The exchange's optional ``site`` block from a declared ``site``/``setback``.

    ``None`` when no lot dimensions are declared, so the key stays absent (old
    documents are byte-identical). Only declared setback edges appear, so the
    reverse path restores exactly what was authored.
    """
    ss = getattr(plan, "site_spec", None)
    if ss is None or not ss.has_dims:
        return None
    block: dict = {"width": float(ss.width), "length": float(ss.length)}
    setbacks: dict = {}
    if ss.front is not None:
        setbacks["front"] = float(ss.front)
    if ss.side is not None:
        setbacks["side"] = float(ss.side)
    if ss.rear is not None:
        setbacks["rear"] = float(ss.rear)
    if setbacks:
        block["setbacks"] = setbacks
    return block


def to_revit_json(plan: Barndominium, indent: int | None = 2) -> str:
    """Lower ``plan`` and serialise the exchange to a JSON string."""
    return to_revit_model(plan).to_json(indent=indent)


# --- the reverse direction: exchange → plan ----------------------------------
#
# The inverse of :func:`to_revit_model`. Reconstructs a :class:`Barndominium`
# from a ``barndsl.revit/1`` exchange dict, so a model that originated in (or was
# round-tripped through) Revit can come back to the DSL via :func:`emit_dsl`.
# Rooms carry their source rectangle, and each opening's host wall + location let
# its wall/offset be re-derived — so the reconstruction is geometric, not a
# stored copy of the DSL.


class RevitImportError(ValueError):
    """The exchange can't be reconstructed into a plan."""


def _infer_exterior_wall(room: Room, location, width: float, tol: float = 1e-3):
    """Return ``(Direction, offset)`` for an opening on ``room``'s exterior wall.

    Picks the room edge the opening's centre lies on, and measures the offset
    from that wall's south/west start corner to the opening's near edge — the
    inverse of :func:`barndsl.geometry.opening_endpoints`.
    """
    x, y = location
    if abs(x - room.x) <= tol:
        return Direction.WEST, (y - width / 2.0) - room.y
    if abs(x - room.x2) <= tol:
        return Direction.EAST, (y - width / 2.0) - room.y
    if abs(y - room.y) <= tol:
        return Direction.SOUTH, (x - width / 2.0) - room.x
    if abs(y - room.y2) <= tol:
        return Direction.NORTH, (x - width / 2.0) - room.x
    return None, None


def _infer_interior_offset(a: Room, b: Room, location, width: float):
    """Offset of an interior opening from the south/west end of the shared wall."""
    edge = shared_edge(a, b)
    if edge is None:
        return None
    along = location[1] if edge.orientation == "v" else location[0]
    return (along - width / 2.0) - edge.lo


def exchange_to_plan(data: dict) -> Barndominium:
    """Reconstruct a :class:`Barndominium` from a ``barndsl.revit/1`` exchange.

    The inverse of :func:`to_revit_model`. Rebuilds the envelope/wings, rooms,
    interior and exterior doors, windows, porches and stairs by re-deriving each
    opening's wall and offset from its geometry. Frame/program/notes aren't
    carried in the exchange, so they aren't restored. Raises
    :class:`RevitImportError` on a document that isn't this schema.
    """
    if not isinstance(data, dict) or data.get("schema") != EXCHANGE_SCHEMA:
        raise RevitImportError(
            "not a %s exchange (got schema %r)" % (EXCHANGE_SCHEMA, (data or {}).get("schema"))
        )
    if data.get("units", "feet") != "feet":
        raise RevitImportError("unsupported units %r" % data.get("units"))

    from .elements import Barndominium

    pinfo = data.get("plan", {})
    plan = Barndominium(name=pinfo.get("name", "Imported Plan"))
    plan.envelope(
        float(pinfo.get("envelope_width", 0.0)),
        float(pinfo.get("envelope_length", 0.0)),
    )
    plan.ceiling(float(pinfo.get("ceiling_height", feet(9))))
    if pinfo.get("floor_depth") is not None:
        plan.floors(float(pinfo["floor_depth"]))
    if pinfo.get("orientation"):
        plan.orient(float(pinfo["orientation"]))
    if pinfo.get("siding") or pinfo.get("roofing"):
        plan.finish(siding=pinfo.get("siding"), roof=pinfo.get("roofing"))
    site = data.get("site")
    if (
        isinstance(site, dict)
        and site.get("width") is not None
        and site.get("length") is not None
    ):
        plan.site(float(site["width"]), float(site["length"]))
        setbacks = site.get("setbacks") or {}
        if setbacks:
            plan.setback(
                front=setbacks.get("front"),
                side=setbacks.get("side"),
                rear=setbacks.get("rear"),
            )
    for wing in pinfo.get("wings", []) or []:
        wx, wy, ww, wl = wing
        plan.wing(float(ww), float(wl), x=float(wx), y=float(wy))

    # Rooms first — openings resolve against them.
    plan_ceiling = float(pinfo.get("ceiling_height", feet(9)))
    for r in data.get("rooms", []):
        # A per-room ceiling equal to the plan default isn't an override — only
        # carry one that actually differs, so the round-trip stays a fixed point.
        rc = r.get("ceiling_height")
        override = None if rc is None or abs(float(rc) - plan_ceiling) <= 1e-9 else float(rc)
        plan.add_room(
            r["id"],
            r["type"],
            x=float(r["x"]),
            y=float(r["y"]),
            width=float(r["width"]),
            length=float(r["length"]),
            level=int(r.get("level", 0)),
            ceiling_height=override,
            vaulted=bool(r.get("vaulted", False)),
        )

    # Declared wall kinds ride the wall segments; re-derive the room pair each
    # tagged segment separates so `wall a - b ...` statements survive the trip
    # (a plumbing-thickness hint on a rated segment restores both attributes).
    specs: dict[tuple[str, str], set[str]] = {}
    for w in data.get("walls", []):
        kind = w.get("kind")
        if not kind or w.get("exterior"):
            continue
        (sx, sy), (ex, ey) = w.get("start", (0.0, 0.0)), w.get("end", (0.0, 0.0))
        vertical = abs(sx - ex) <= 1e-9
        orientation = "v" if vertical else "h"
        const = sx if vertical else sy
        lo, hi = sorted((sy, ey) if vertical else (sx, ex))
        lvl_rooms = [r for r in plan.rooms if getattr(r, "level", 0) == w.get("level", 0)]
        for i, ra in enumerate(lvl_rooms):
            for rb in lvl_rooms[i + 1 :]:
                edge = shared_edge(ra, rb)
                if (
                    edge is None
                    or edge.orientation != orientation
                    or abs(edge.pos - const) > 1e-4
                ):
                    continue
                if min(hi, edge.hi) - max(lo, edge.lo) <= TOL:
                    continue
                attrs = specs.setdefault((ra.id, rb.id), set())
                attrs.add(kind)
                if float(w.get("thickness", 0.0)) >= PLUMBING_WALL_THICKNESS - 1e-9:
                    attrs.add("plumbing")
    for (ra_id, rb_id), attrs in specs.items():
        plan.wall(ra_id, rb_id, *sorted(attrs))

    for o in data.get("openings", []):
        rooms = o.get("rooms", [])
        width = float(o.get("width", 0.0))
        loc = o.get("location", [0.0, 0.0])
        if o.get("category") == "window":
            room = plan.room(rooms[0]) if rooms else None
            if room is None:
                continue
            wall, offset = _infer_exterior_wall(room, loc, width)
            if wall is None:
                continue
            head = float(o.get("sill", 0.0)) + float(o.get("height", 0.0))
            # Window kind round-trips; an old document's generic "window" (or a
            # missing kind) falls back to the casement default.
            wkind = o.get("kind")
            if wkind not in WINDOW_KINDS:
                wkind = "casement"
            plan.add_window(
                room.id, wall, width=width, offset=max(0.0, offset),
                sill_height=float(o.get("sill", 0.0)), head_height=head,
                kind=wkind,
            )
        elif o.get("exterior"):
            room = plan.room(rooms[0]) if rooms else None
            if room is None:
                continue
            wall, offset = _infer_exterior_wall(room, loc, width)
            if wall is None:
                continue
            raw_kind = o.get("kind")
            overhead = raw_kind == "overhead"
            if overhead:
                dkind = "overhead"
            elif raw_kind in DOUBLE_LEAF_KINDS:
                dkind = raw_kind  # a double/french pair round-trips its kind
            else:
                dkind = "entry"  # the historical "exterior" single door
            plan.entrance(
                room.id, wall, width=width, offset=max(0.0, offset),
                egress=bool(o.get("egress", True)),
                # An overhead door round-trips its kind and panel height;
                # entrance() re-forces egress=False for it.
                kind=dkind,
                height=(
                    float(o["height"])
                    if overhead and o.get("height") is not None
                    else None
                ),
            )
        else:  # interior door or cased opening
            if len(rooms) < 2:
                continue
            a, b = plan.room(rooms[0]), plan.room(rooms[1])
            if a is None or b is None:
                continue
            offset = _infer_interior_offset(a, b, loc, width)
            plan.connect(
                a.id, b.id, width=width, kind=o.get("kind", "swing"),
                offset=None if offset is None else max(0.0, offset),
                # Optional swing side/hinge (additive fields; absent on old docs).
                swing_into=o.get("swing_into"),
                hinge=o.get("hinge"),
            )

    for area in data.get("areas", []):
        meta = area.get("meta", {})
        if area.get("kind") == "porch":
            plan.add_porch(
                area["id"], x=float(area["x"]), y=float(area["y"]),
                width=float(area["width"]), length=float(area["length"]),
                covered=bool(meta.get("covered", True)),
            )
        elif area.get("kind") == "stair":
            plan.add_stair(
                area["id"], x=float(area["x"]), y=float(area["y"]),
                width=float(area["width"]), length=float(area["length"]),
                from_level=int(meta.get("from_level", area.get("level", 0))),
                to_level=int(meta.get("to_level", 1)),
            )

    return plan


def exchange_to_dsl(data: dict) -> str:
    """Reconstruct a plan from an exchange and serialise it to DSL source."""
    from .emit import emit_dsl

    return emit_dsl(exchange_to_plan(data))
