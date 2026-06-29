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
from dataclasses import asdict, dataclass, field

from .elements import (
    Barndominium,
    Direction,
    feet,
    inches,
)
from .geometry import TOL, opening_endpoints, point_in_footprint, shared_edge

# --- defaults the rectangle IR doesn't carry ---------------------------------
#: Head height of a standard door leaf (interior or exterior): 6'-8".
DEFAULT_DOOR_HEIGHT = feet(6.667)
#: Height of a doorless cased opening (walk-through); taller than a leaf door.
DEFAULT_CASED_HEIGHT = feet(7.0)
#: Nominal wall thicknesses, as a hint for picking a Revit wall type. The
#: exchange is centreline-based, so these don't move geometry — they let the
#: consumer map "exterior" → a 2x6 shell and "interior" → a 2x4 partition.
EXTERIOR_WALL_THICKNESS = inches(6.5)
INTERIOR_WALL_THICKNESS = inches(4.5)

#: A short distance used to probe just past a wall to decide if its far side is
#: outside the footprint (and the wall therefore exterior).
_PROBE = 0.05

EXCHANGE_SCHEMA = "barndsl.revit/1"


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
    kind: str  # swing | pocket | sliding | cased | exterior | window
    level: int
    location: tuple[float, float]
    width: float
    height: float
    sill: float
    exterior: bool
    egress: bool
    rooms: list[str]
    host_wall: str | None


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


@dataclass
class RevitColumn:
    """A structural post (column) at a point on a level."""

    point: tuple[float, float]
    size: float
    role: str
    level: int


@dataclass
class RevitFraming:
    """A structural beam centreline (bent or ridge) on a level."""

    start: tuple[float, float]
    end: tuple[float, float]
    role: str
    level: int


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
class RevitModel:
    """The full Revit-shaped exchange for one plan."""

    name: str
    ceiling_height: float
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

    def to_dict(self) -> dict:
        """A JSON-serialisable dict — the ``barndsl.revit/1`` exchange document."""
        return {
            "schema": EXCHANGE_SCHEMA,
            "units": "feet",
            "plan": {
                "name": self.name,
                "ceiling_height": self.ceiling_height,
                "envelope_width": self.envelope_width,
                "envelope_length": self.envelope_length,
                "wings": [list(w) for w in self.wings],
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
                    }
                    for c in self.columns
                ],
                "framing": [
                    {
                        "start": list(f.start),
                        "end": list(f.end),
                        "role": f.role,
                        "level": f.level,
                    }
                    for f in self.framing
                ],
            },
            "areas": [asdict(a) for a in self.areas],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --- wall extraction ---------------------------------------------------------


def _walls_one_axis(rooms, sections, vertical, height, level, idgen):
    """Derive deduplicated wall runs along one axis for a single level.

    ``vertical`` selects walls at constant ``x`` (running north-south); otherwise
    constant ``y`` (running east-west). Returns a list of :class:`RevitWall`.

    For each grid line, room edges contribute a room on the ``+`` side (its low
    edge lies on the line) and/or the ``-`` side (its high edge lies on the
    line). Each atomic interval between consecutive breakpoints is a wall when a
    room touches it; it is *interior* when rooms sit on both sides and *exterior*
    when the empty side falls outside the footprint. Contiguous like-classified
    intervals merge into one wall.
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
                if on_pos and on_neg:
                    exterior = False  # partition between two rooms
                else:
                    # Probe just past the empty side; outside the footprint ⇒ exterior.
                    if vertical:
                        px = c - _PROBE if on_pos else c + _PROBE
                        py = mid
                    else:
                        px = mid
                        py = c - _PROBE if on_pos else c + _PROBE
                    exterior = not point_in_footprint(sections, px, py)
                seg = (a, b, exterior)

            if seg is None:
                if run is not None:
                    walls.append(_make_wall(idgen, level, vertical, c, run, height))
                    run = None
                continue
            if run is not None and run[2] == seg[2] and abs(run[1] - seg[0]) <= TOL:
                run = (run[0], seg[1], seg[2])  # extend the contiguous run
            else:
                if run is not None:
                    walls.append(_make_wall(idgen, level, vertical, c, run, height))
                run = seg
        if run is not None:
            walls.append(_make_wall(idgen, level, vertical, c, run, height))

    return walls


def _make_wall(idgen, level, vertical, c, run, height):
    lo, hi, exterior = run
    if vertical:
        start, end = (c, lo), (c, hi)
    else:
        start, end = (lo, c), (hi, c)
    thickness = EXTERIOR_WALL_THICKNESS if exterior else INTERIOR_WALL_THICKNESS
    return RevitWall(
        id=next(idgen),
        level=level,
        start=(float(start[0]), float(start[1])),
        end=(float(end[0]), float(end[1])),
        height=float(height),
        exterior=exterior,
        thickness=thickness,
    )


def _extract_walls(plan: Barndominium, level: int, height: float, idgen) -> list[RevitWall]:
    rooms = [r for r in plan.rooms if getattr(r, "level", 0) == level]
    sections = plan.footprint_sections()
    walls = _walls_one_axis(rooms, sections, True, height, level, idgen)
    walls += _walls_one_axis(rooms, sections, False, height, level, idgen)
    return walls


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


# --- the public entry points -------------------------------------------------


def to_revit_model(plan: Barndominium) -> RevitModel:
    """Lower ``plan`` to a :class:`RevitModel` (the Revit-shaped exchange)."""
    height = plan.ceiling_height
    level_indexes = plan.levels()
    levels = [
        RevitLevel(
            index=i,
            name=f"Level {i + 1}",
            elevation=float(i * height),
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
                egress=False,
                rooms=[d.room_a, d.room_b],
                host_wall=host,
            )
        )

    for d in plan.exterior_doors:
        geom = _opening_geometry(plan, d, "exterior")
        if geom is None:
            continue
        orientation, pos, lo, hi, lvl = geom
        host = _find_host(walls_by_level.get(lvl, []), orientation, pos, lo, hi)
        openings.append(
            RevitOpening(
                id=next(op_ids),
                category="door",
                kind="exterior",
                level=lvl,
                location=_location(orientation, pos, lo, hi),
                width=float(hi - lo),
                height=DEFAULT_DOOR_HEIGHT,
                sill=0.0,
                exterior=True,
                egress=bool(d.egress),
                rooms=[d.room],
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
                kind="window",
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

    rooms = [
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
        )
        for r in plan.rooms
    ]

    columns = [
        RevitColumn(
            point=(float(p.x), float(p.y)),
            size=float(p.size),
            role=p.role,
            level=getattr(p, "level", 0),
        )
        for p in plan.posts
    ]
    framing = [
        RevitFraming(
            start=(float(b.x1), float(b.y1)),
            end=(float(b.x2), float(b.y2)),
            role=b.role,
            level=getattr(b, "level", 0),
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
        areas.append(
            RevitArea(
                id=s.id,
                kind="stair",
                x=float(s.x),
                y=float(s.y),
                width=float(s.width),
                length=float(s.length),
                level=s.from_level,
                meta={"from_level": s.from_level, "to_level": s.to_level},
            )
        )

    return RevitModel(
        name=plan.name,
        ceiling_height=float(height),
        envelope_width=float(plan.envelope_width),
        envelope_length=float(plan.envelope_length),
        wings=[(float(w.x), float(w.y), float(w.width), float(w.length)) for w in plan.wings],
        levels=levels,
        walls=walls,
        openings=openings,
        rooms=rooms,
        columns=columns,
        framing=framing,
        areas=areas,
    )


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
    for wing in pinfo.get("wings", []) or []:
        wx, wy, ww, wl = wing
        plan.wing(float(ww), float(wl), x=float(wx), y=float(wy))

    # Rooms first — openings resolve against them.
    for r in data.get("rooms", []):
        plan.add_room(
            r["id"],
            r["type"],
            x=float(r["x"]),
            y=float(r["y"]),
            width=float(r["width"]),
            length=float(r["length"]),
            level=int(r.get("level", 0)),
        )

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
            plan.add_window(
                room.id, wall, width=width, offset=max(0.0, offset),
                sill_height=float(o.get("sill", 0.0)), head_height=head,
            )
        elif o.get("exterior"):
            room = plan.room(rooms[0]) if rooms else None
            if room is None:
                continue
            wall, offset = _infer_exterior_wall(room, loc, width)
            if wall is None:
                continue
            plan.entrance(
                room.id, wall, width=width, offset=max(0.0, offset),
                egress=bool(o.get("egress", True)),
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
