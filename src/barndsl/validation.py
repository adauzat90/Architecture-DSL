"""Semantic / building-code checks — the "type checker" of the compiler.

These run after a plan parses. They are loosely modelled on the International
Residential Code (IRC) plus spatial sanity, and — crucially — every diagnostic
carries an actionable ``hint`` telling the author *how* to fix it, in DSL terms.
That is what turns validation from a pass/fail gate into something that guides
the design.

The checks are approximate and **not** a substitute for a licensed designer or a
review by the authority having jurisdiction.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

from .elements import (
    HABITABLE_TYPES,
    Barndominium,
    Direction,
    Room,
    RoomType,
)
from .geometry import (
    opening_endpoints,
    rect_in_footprint,
    shared_edge,
    wall_faces_outside,
)

# Approximate IRC-derived thresholds (feet unless noted).
MIN_CEILING = 7.0
MIN_BEDROOM_AREA = 70.0
MIN_BEDROOM_DIMENSION = 7.0
MIN_HALLWAY_WIDTH = 3.0  # 36 in — hard minimum (HALL_WIDTH error)
COMFORT_HALLWAY_WIDTH = 4.0  # a hall under this passes code but feels tight (HALL_TIGHT)
#: A hallway that runs this far past its last served door is a circulation stub
#: (a dead end) — wasted footprint you walk into and back out of.
MIN_HALL_STUB = 2.5
#: Type-aware usable-area floors (sq ft) plus a shortest-side floor (ft). Only the
#: rooms whose use needs a clear minimum of fixtures/clearances. A full bath wants
#: a 5 ft tub run plus a vanity and a clear floor (~6x8); a half bath (powder
#: room) just a toilet + sink (~5x6). Bedrooms are covered by BEDROOM_AREA.
MIN_USABLE_AREA: dict[RoomType, float] = {
    RoomType.KITCHEN: 70.0,  # counters + appliances + a working aisle
    RoomType.BATHROOM: 48.0,  # full bath: tub/shower + toilet + vanity (~6x8)
    RoomType.HALF_BATH: 30.0,  # powder room: toilet + sink (~5x6)
}
#: Shortest-side floor (ft) for rooms where a narrow strip can't hold the fixtures.
MIN_ROOM_SHORT_SIDE: dict[RoomType, float] = {
    RoomType.BATHROOM: 6.0,
    RoomType.HALF_BATH: 5.0,
}
MIN_EGRESS_DOOR_WIDTH = 32 / 12  # 32 in clear; matches inches(32) exactly
MIN_INTERIOR_DOOR_WIDTH = 30 / 12  # 30 in
# Manufactured door leaf widths (inches). A door off these isn't orderable
# off-the-shelf; the check nudges to the nearest. Doubles (60/72) included.
STD_INTERIOR_DOOR_WIDTHS_IN = (24, 28, 30, 32, 36, 60, 72)
STD_EXTERIOR_DOOR_WIDTHS_IN = (30, 32, 36, 60, 72)
DOOR_SIZE_TOL_IN = 0.5  # how far off a standard size before we nudge
NATURAL_LIGHT_RATIO = 0.08  # glazing >= 8% of floor area
_WINDOW_TYP_HEIGHT = 3.67  # head - sill for a typical window, ft
MAX_ROOM_ASPECT = 3.0  # a habitable room longer than this (long:short) is awkward
MIN_SOUND_BUFFER_WALL = 4.0  # a bedroom-bedroom shared wall this long wants a buffer
CLOSET_WALKIN_ASPECT = 4.0  # a closet skinnier than this (long:short) is "long skinny"
MIN_WALKIN_AREA = 24.0  # a closet this big is worth shaping as a walk-in, not a strip
#: A swing door centred on a wall with at least this much clear wall on *both*
#: flanks is floating mid-wall; backing it to a corner frees a usable wall run.
DOOR_CORNER_MARGIN = 2.0
#: Clear floor a doorway needs in front of it (a landing/approach), ft. A stair
#: intruding into this blocks the door.
DOOR_CLEARANCE_DEPTH = 3.0
#: A window whose edge lands this close to an interior-partition corner collides
#: with that wall's framing/trim — pull it toward the centre or the building corner.
WINDOW_WALL_CLEAR = 0.5

# Emergency-escape opening minimums (IRC R310). The area is the net *clear*
# opening; we approximate it from the modelled width × (head − sill), which is
# generous for a single-hung sash but matches the project's "loosely IRC" stance.
MIN_EGRESS_AREA = 5.7  # sq ft, upper floors
MIN_EGRESS_AREA_GRADE = 5.0  # sq ft, at-grade floor (level 0)
MIN_EGRESS_OPENING_WIDTH = 20 / 12  # 20 in clear
MIN_EGRESS_OPENING_HEIGHT = 24 / 12  # 24 in clear
MAX_EGRESS_SILL = 44 / 12  # sill <= 44 in above the finished floor

# Stair geometry (IRC R311.7): a flight needs enough run to climb one storey.
MAX_RISER_HEIGHT = 7.75 / 12  # 7-3/4 in max riser
MIN_TREAD_DEPTH = 10 / 12  # 10 in min tread
MIN_STAIR_WIDTH = 3.0  # 36 in; two side-by-side flights (a switchback) need ~6 ft


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    """A single diagnostic. Used for both syntax (compiler) and semantic checks."""

    severity: Severity
    code: str
    message: str
    room: str | None = None
    line: int | None = None
    col: int | None = None
    end_col: int | None = None  # 1-based, exclusive — for column-accurate carets
    hint: str | None = None

    def __str__(self) -> str:
        loc = f"line {self.line}: " if self.line else ""
        where = f" ({self.room})" if self.room is not None else ""
        head = f"{loc}{self.severity.value}[{self.code}]{where}: {self.message}"
        if self.hint:
            head += f"\n    hint: {self.hint}"
        return head


@dataclass
class ValidationReport:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.INFO]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        e, w, n = len(self.errors), len(self.warnings), len(self.infos)
        status = "VALID" if self.is_valid else "INVALID"
        return f"{status} — {e} error(s), {w} warning(s), {n} info(s)"

    def __str__(self) -> str:
        return "\n".join([self.summary(), *(str(i) for i in self.issues)])


# --- helpers ----------------------------------------------------------------


def exterior_walls(plan: Barndominium, room: Room, tol: float = 1e-6) -> list[Direction]:
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


def geometric_neighbors(plan: Barndominium, room_id: str) -> list[str]:
    """Ids of rooms that share a wall segment with ``room_id``."""
    r = plan.room(room_id)
    if r is None:
        return []
    return [o.id for o in plan.rooms if o.id != room_id and shared_edge(r, o)]


def _f(value: float) -> str:
    """Format a measurement: drop a trailing .0 (so 28.0 -> '28')."""
    return f"{value:g}"


#: Public, shared living spaces — bedrooms ideally don't open straight onto these.
PUBLIC_TYPES = {RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING}
BATH_TYPES = {RoomType.BATHROOM, RoomType.HALF_BATH}
#: Rooms with plumbing fixtures — cheaper to build when clustered on a wet wall.
WET_TYPES = {
    RoomType.BATHROOM,
    RoomType.HALF_BATH,
    RoomType.KITCHEN,
    RoomType.LAUNDRY,
    RoomType.UTILITY,
}


def _door_graph(plan: Barndominium) -> dict[str, set[str]]:
    """Adjacency by interior doors (who can walk to whom)."""
    graph: dict[str, set[str]] = {r.id: set() for r in plan.rooms}
    for d in plan.interior_doors:
        if d.room_a in graph and d.room_b in graph:
            graph[d.room_a].add(d.room_b)
            graph[d.room_b].add(d.room_a)
    return graph


def _wall_length(room: Room, wall: Direction) -> float:
    """Length of ``room``'s named wall (north/south run east-west = width)."""
    return room.width if wall in (Direction.NORTH, Direction.SOUTH) else room.length


def _nearest_std(width_in: float, sizes: tuple[int, ...]) -> int:
    """The manufactured door width (inches) closest to ``width_in``."""
    return min(sizes, key=lambda s: abs(s - width_in))


def _door_interval(edge, door) -> tuple[float, float]:
    """The door's span along its shared wall, in world coordinates.

    ``offset`` is measured from the south/west end of the shared wall (``edge.lo``);
    ``None`` centres the door on the wall.
    """
    if door.offset is None:
        lo = edge.mid - door.width / 2.0
    else:
        lo = edge.lo + door.offset
    return lo, lo + door.width


def _zone_from_edge(edge, lo: float, hi: float, depth: float) -> tuple[float, float, float, float]:
    """The clear-floor rectangle straddling a wall edge over span ``[lo, hi]``.

    Extends ``depth`` ft to either side of the wall (into both rooms it divides).
    """
    if edge.orientation == "v":  # constant x; span runs in y
        return edge.pos - depth, lo, edge.pos + depth, hi
    return lo, edge.pos - depth, hi, edge.pos + depth


def _ext_door_zone(room: Room, door, depth: float) -> tuple[float, float, float, float]:
    """The clear-floor rectangle just inside an exterior door."""
    x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
    if door.wall in (Direction.NORTH, Direction.SOUTH):
        return min(x1, x2), y1 - depth, max(x1, x2), y1 + depth
    return x1 - depth, min(y1, y2), x1 + depth, max(y1, y2)


def _building_corner(plan: Barndominium, room: Room, wall: Direction, at_end: bool) -> bool:
    """Is the given end of ``room``'s ``wall`` a *building* corner (vs an interior
    partition junction)? A corner is a building corner when the perpendicular wall
    meeting it there is also on the envelope."""
    ext = set(exterior_walls(plan, room))
    if wall in (Direction.SOUTH, Direction.NORTH):
        perp = Direction.EAST if at_end else Direction.WEST
    else:  # vertical wall: ends run south->north
        perp = Direction.NORTH if at_end else Direction.SOUTH
    return perp in ext


def _stair_against_wall(plan: Barndominium, s) -> bool:
    """Does a stair sit along a wall (envelope edge or a room partition) rather
    than floating free in the middle of a room?"""
    tol = 1e-6
    if (
        abs(s.x) <= tol
        or abs(s.y) <= tol
        or abs(s.x2 - plan.envelope_width) <= tol
        or abs(s.y2 - plan.envelope_length) <= tol
    ):
        return True
    # A room (on either level the stair links) sharing a full edge counts.
    for r in plan.rooms:
        if r.level not in (s.from_level, s.to_level):
            continue
        # Vertical contact (shared constant x), overlapping in y.
        for sx, rx in ((s.x, r.x2), (s.x2, r.x)):
            if abs(sx - rx) <= tol and min(s.y2, r.y2) - max(s.y, r.y) > tol:
                return True
        # Horizontal contact (shared constant y), overlapping in x.
        for sy, ry in ((s.y, r.y2), (s.y2, r.y)):
            if abs(sy - ry) <= tol and min(s.x2, r.x2) - max(s.x, r.x) > tol:
                return True
    return False


def _suggest_int(value: float, cap: float = 1e4) -> int | None:
    """Round ``value`` up to a sane suggestion, or None if non-finite/absurd."""
    if not math.isfinite(value) or value > cap:
        return None
    return max(0, math.ceil(value))


def _nearest_distance(
    graph: dict[str, set[str]], start: str, targets: set[str]
) -> int | None:
    """Fewest doors from ``start`` to any id in ``targets`` (None if unreachable)."""
    if start in targets:
        return 0
    seen = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        cur, dist = queue.popleft()
        for nb in graph.get(cur, ()):  # neighbours
            if nb in seen:
                continue
            if nb in targets:
                return dist + 1
            seen.add(nb)
            queue.append((nb, dist + 1))
    return None


def _components_excluding(
    graph: dict[str, set[str]], excluded: set[str]
) -> list[set[str]]:
    """Connected components of the door graph with ``excluded`` rooms removed.

    Used to ask "if you couldn't walk through these rooms, what's still
    connected?" — the basis of the pass-through-a-private-room checks.
    """
    comps: list[set[str]] = []
    seen: set[str] = set()
    for start in graph:
        if start in excluded or start in seen:
            continue
        comp: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in comp:
                continue
            comp.add(node)
            seen.add(node)
            for nb in graph[node]:
                if nb not in excluded and nb not in comp:
                    stack.append(nb)
        comps.append(comp)
    return comps


# --- entry point ------------------------------------------------------------


def _check_wings(plan: Barndominium, add, tol: float = 1e-6) -> None:
    """Validate ``wing`` blocks: positive size, and a single connected footprint."""
    if not plan.wings:
        return
    for i, w in enumerate(plan.wings, 1):
        if w.width <= 0 or w.length <= 0:
            add(
                Issue(
                    Severity.ERROR,
                    "WING_SIZE",
                    f"Wing #{i} has non-positive size ({_f(w.width)}×{_f(w.length)}).",
                    hint="Use positive feet, e.g. `wing 20 x 24 at 40,0`.",
                )
            )
    secs = plan.footprint_sections()  # [primary, *wings]

    def abuts(s, t) -> bool:
        ox = min(s[0] + s[2], t[0] + t[2]) - max(s[0], t[0])
        oy = min(s[1] + s[3], t[1] + t[3]) - max(s[1], t[1])
        if ox > tol and oy > tol:
            return True  # overlap
        if abs(ox) <= tol and oy > tol:
            return True  # shared vertical edge
        if abs(oy) <= tol and ox > tol:
            return True  # shared horizontal edge
        return False

    parent = list(range(len(secs)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(secs)):
        for j in range(i + 1, len(secs)):
            if abuts(secs[i], secs[j]):
                parent[find(i)] = find(j)

    if len({find(i) for i in range(len(secs))}) > 1:
        add(
            Issue(
                Severity.ERROR,
                "FOOTPRINT_SPLIT",
                "The footprint is disconnected — a wing doesn't share a wall with "
                "the rest of the building (a corner touch isn't enough).",
                hint="Reposition the wing so it abuts the envelope or another wing "
                "along a shared edge.",
            )
        )


def validate(plan: Barndominium) -> ValidationReport:
    """Run all checks and return a :class:`ValidationReport`."""
    issues: list[Issue] = []
    add = issues.append

    if plan.envelope_width <= 0 or plan.envelope_length <= 0:
        add(
            Issue(
                Severity.ERROR,
                "ENVELOPE",
                "Envelope must have positive dimensions.",
                hint="Declare the footprint, e.g. `envelope 60 x 40`.",
            )
        )
    _check_wings(plan, add)
    if plan.ceiling_height < MIN_CEILING:
        add(
            Issue(
                Severity.ERROR,
                "CEILING",
                f"Ceiling height {_f(plan.ceiling_height)} ft is below the "
                f"{MIN_CEILING:.0f} ft minimum for habitable space.",
                hint=f"Set `ceiling {MIN_CEILING:.0f}` or greater (9–12 is typical).",
            )
        )
    if not plan.rooms:
        add(
            Issue(
                Severity.ERROR,
                "EMPTY",
                "Plan has no rooms.",
                hint="Add rooms, e.g. `room living: living at 0,0 size 20 x 16`.",
            )
        )
        return ValidationReport(issues)

    ids = [r.id for r in plan.rooms]
    for d in sorted({i for i in ids if ids.count(i) > 1}):
        add(
            Issue(
                Severity.ERROR,
                "DUP_ID",
                f"Duplicate room id '{d}'.",
                room=d,
                hint="Give each room a unique id.",
            )
        )

    _validate_geometry(plan, add)
    _validate_room_programs(plan, add)
    _validate_doors(plan, add)
    _validate_openings(plan, add)
    _validate_stairs(plan, add)
    _validate_access(plan, add)
    _validate_egress_and_light(plan, add)
    _validate_design_quality(plan, add)
    _validate_program(plan, add)

    if not plan.metrics()["bathroom_count"]:
        add(
            Issue(
                Severity.WARNING,
                "NO_BATH",
                "Plan has no bathroom.",
                hint="Add a bathroom, e.g. `room bath: bathroom at ... size 8 x 8`.",
            )
        )

    return ValidationReport(issues)


def _validate_geometry(plan: Barndominium, add) -> None:
    for room in plan.rooms:
        if not all(math.isfinite(v) for v in (room.x, room.y, room.width, room.length)):
            add(
                Issue(
                    Severity.ERROR,
                    "ROOM_GEOMETRY",
                    "Room has non-finite coordinates or size.",
                    room=room.id,
                    hint="Use finite measurements in feet (no nan/inf).",
                )
            )
            continue  # skip further geometry checks — NaN defeats every comparison
        if room.width <= 0 or room.length <= 0:
            add(
                Issue(
                    Severity.ERROR,
                    "ROOM_SIZE",
                    "Room has non-positive size.",
                    room=room.id,
                    hint="Use positive feet, e.g. `size 12 x 10`.",
                )
            )
        over_x = max(0.0, room.x2 - plan.envelope_width)
        over_y = max(0.0, room.y2 - plan.envelope_length)
        if plan.wings:
            # Rectilinear footprint: the building may extend past the primary
            # envelope into wings, and have notches the bbox doesn't, so test the
            # actual footprint union rather than the primary rectangle.
            if (
                room.width > 0
                and room.length > 0
                and not rect_in_footprint(
                    plan.footprint_sections(),
                    room.x,
                    room.y,
                    room.width,
                    room.length,
                )
            ):
                add(
                    Issue(
                        Severity.ERROR,
                        "OUT_OF_BOUNDS",
                        f"Room falls outside the building footprint "
                        f"({_f(room.x)},{_f(room.y)} → {_f(room.x2)},{_f(room.y2)}); "
                        "it isn't covered by the envelope or any wing.",
                        room=room.id,
                        hint="Move it inside a footprint block, or add a `wing` "
                        "to cover that area.",
                    )
                )
        elif room.x < -1e-6 or room.y < -1e-6 or over_x > 1e-6 or over_y > 1e-6:
            # Phrase the fix differently for a relatively-placed room, which has
            # no x,y token to "set" — point at align/offset/size instead.
            relative = room.placement is not None
            fixes = []
            if not relative and room.x < 0:
                fixes.append("set its x to >= 0")
            if not relative and room.y < 0:
                fixes.append("set its y to >= 0")
            if over_x > 1e-6:
                move_x = plan.envelope_width - room.width
                fixes.append(
                    f"reduce its width by {_f(over_x)} ft"
                    + ("" if relative or move_x < 0 else f" or move it west to x={_f(move_x)}")
                )
            if over_y > 1e-6:
                move_y = plan.envelope_length - room.length
                fixes.append(
                    f"reduce its length by {_f(over_y)} ft"
                    + ("" if relative or move_y < 0 else f" or move it south to y={_f(move_y)}")
                )
            if relative:
                fixes.append(
                    f"adjust its align/offset or size, or pin it with `at x,y` "
                    f"(it's placed `{room.placement}`)"
                )
            add(
                Issue(
                    Severity.ERROR,
                    "OUT_OF_BOUNDS",
                    f"Room extends outside the {_f(plan.envelope_width)}×"
                    f"{_f(plan.envelope_length)} ft envelope "
                    f"({_f(room.x)},{_f(room.y)} → {_f(room.x2)},{_f(room.y2)}).",
                    room=room.id,
                    hint="; ".join(fixes) + ".",
                )
            )

    for i, a in enumerate(plan.rooms):
        for b in plan.rooms[i + 1 :]:
            if a.level != b.level:
                continue  # different floors may share a footprint (e.g. a loft)
            ov = a.overlaps(b)
            if ov > 0.5:  # ignore hairline floating-point overlaps
                # Prefer a fix that keeps 'b' inside the envelope.
                east_fits = a.x2 + b.width <= plan.envelope_width + 1e-6
                north_fits = a.y2 + b.length <= plan.envelope_length + 1e-6
                ox = min(a.x2, b.x2) - max(a.x, b.x)
                oy = min(a.y2, b.y2) - max(a.y, b.y)
                prefer_east = (ox <= oy and east_fits) or (not north_fits and east_fits)
                if prefer_east:
                    sug = f"move '{b.id}' to x={_f(a.x2)} (east of '{a.id}')"
                elif north_fits:
                    sug = f"move '{b.id}' to y={_f(a.y2)} (north of '{a.id}')"
                else:
                    sug = f"shrink one of them or enlarge the envelope"
                # A collision is often a relative-anchor chain pushing a room onto
                # one already placed — surface that so the fix is re-anchoring, not
                # guessing at a shrink.
                placed = next((r for r in (b, a) if r.placement is not None), None)
                note = (
                    f" ('{placed.id}' is placed `{placed.placement}` — re-anchor it "
                    "one room deep off a spine, or pin it with `at x,y`)"
                    if placed is not None
                    else ""
                )
                add(
                    Issue(
                        Severity.ERROR,
                        "OVERLAP",
                        f"Rooms '{a.id}' and '{b.id}' overlap by {_f(ov)} sq ft.",
                        room=a.id,
                        hint=f"Reposition so they don't intersect — e.g. {sug}.{note}",
                    )
                )

    # Footprint coverage is a ground-floor (level 0) concept; lofts sit above.
    used = sum(r.area for r in plan.rooms if r.level == 0)
    if plan.footprint_area > 0 and math.isfinite(used) and math.isfinite(plan.footprint_area):
        frac = used / plan.footprint_area
        if frac > 1.001:
            add(
                Issue(
                    Severity.WARNING,
                    "AREA_OVERFLOW",
                    f"Assigned room area ({_f(used)} sq ft) exceeds the footprint "
                    f"({_f(plan.footprint_area)} sq ft).",
                    hint="Shrink rooms or enlarge the envelope; rooms likely overlap.",
                )
            )
        elif frac < 0.85:
            add(
                Issue(
                    Severity.INFO,
                    "AREA_UNUSED",
                    f"Only {frac * 100:.0f}% of the footprint is assigned to rooms; "
                    f"{_f(plan.footprint_area - used)} sq ft unallocated.",
                    hint="Enlarge rooms or add spaces to fill the footprint.",
                )
            )


def _validate_room_programs(plan: Barndominium, add) -> None:
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            if room.area < MIN_BEDROOM_AREA:
                need_len = _suggest_int(MIN_BEDROOM_AREA / max(room.width, 1e-6))
                hint = (
                    f"Enlarge it, e.g. `size {_f(room.width)} x {need_len}`."
                    if need_len is not None
                    else f"Enlarge it to at least {MIN_BEDROOM_AREA:.0f} sq ft."
                )
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_AREA",
                        f"Bedroom is {_f(room.area)} sq ft; IRC minimum is "
                        f"{MIN_BEDROOM_AREA:.0f} sq ft.",
                        room=room.id,
                        hint=hint,
                    )
                )
            if room.min_dimension < MIN_BEDROOM_DIMENSION:
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_DIM",
                        f"Bedroom's smallest dimension is {_f(room.min_dimension)} ft; "
                        f"minimum is {MIN_BEDROOM_DIMENSION:.0f} ft.",
                        room=room.id,
                        hint=f"Make both dimensions >= {MIN_BEDROOM_DIMENSION:.0f} ft.",
                    )
                )
        if room.type is RoomType.HALLWAY and room.min_dimension < MIN_HALLWAY_WIDTH:
            add(
                Issue(
                    Severity.ERROR,
                    "HALL_WIDTH",
                    f"Hallway is {_f(room.min_dimension)} ft wide; minimum is "
                    f"{MIN_HALLWAY_WIDTH:.0f} ft.",
                    room=room.id,
                    hint=f"Widen it to >= {MIN_HALLWAY_WIDTH:.0f} ft.",
                )
            )
        floor = MIN_USABLE_AREA.get(room.type)
        short_floor = MIN_ROOM_SHORT_SIDE.get(room.type)
        if floor is not None and 0 < room.area < floor:
            need_len = _suggest_int(floor / max(room.width, 1e-6))
            sizing = (
                f" e.g. `size {_f(room.width)} x {need_len}`"
                if need_len is not None
                else ""
            )
            add(
                Issue(
                    Severity.INFO,
                    "ROOM_TIGHT",
                    f"{room.type.value.replace('_', ' ').capitalize()} '{room.id}' is "
                    f"{_f(room.area)} sq ft; a workable {room.type.value.replace('_', ' ')} "
                    f"wants about {floor:.0f} sq ft.",
                    room=room.id,
                    hint=f"Enlarge it to >= {floor:.0f} sq ft{sizing}.",
                )
            )
        elif short_floor is not None and 0 < room.min_dimension < short_floor:
            # Big enough by area but too narrow to fit the fixtures across it
            # (e.g. a 4 ft-wide full bath can't take a tub on the short wall).
            add(
                Issue(
                    Severity.INFO,
                    "ROOM_TIGHT",
                    f"{room.type.value.replace('_', ' ').capitalize()} '{room.id}' is only "
                    f"{_f(room.min_dimension)} ft on its short side; a workable "
                    f"{room.type.value.replace('_', ' ')} wants >= {short_floor:.0f} ft.",
                    room=room.id,
                    hint=f"Widen the short side to >= {short_floor:.0f} ft.",
                )
            )


def _door_loc(door) -> dict:
    """Source-location kwargs for a door/window's own statement (if known)."""
    return {"line": door.line, "col": door.col, "end_col": door.end_col}


def _validate_doors(plan: Barndominium, add) -> None:
    room_ids = {r.id for r in plan.rooms}
    for door in plan.interior_doors:
        loc = _door_loc(door)
        if door.room_a == door.room_b:
            add(
                Issue(
                    Severity.ERROR,
                    "SELF_DOOR",
                    f"Interior door connects room '{door.room_a}' to itself.",
                    room=door.room_a,
                    hint="A door joins two different rooms.",
                    **loc,
                )
            )
            continue
        for rid in (door.room_a, door.room_b):
            if rid not in room_ids:
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_REF",
                        f"Interior door references unknown room '{rid}'.",
                        room=rid,
                        hint="Reference an existing room id, or declare the room.",
                        **loc,
                    )
                )
        a, b = plan.room(door.room_a), plan.room(door.room_b)
        if a and b and a.level != b.level:
            # A door across levels is a stair/opening; it's valid when the rooms
            # stack (overlap in plan), not when they share a wall.
            if a.overlaps(b) <= 0.5:
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_NOADJ",
                        f"Door connects '{a.id}' (level {a.level}) and '{b.id}' "
                        f"(level {b.level}) but their footprints don't overlap.",
                        room=a.id,
                        hint="A cross-level door is a stair; place the upper room "
                        "directly above part of the lower one.",
                        **loc,
                    )
                )
        elif a and b:
            edge = shared_edge(a, b)
            if edge is None:
                nb = geometric_neighbors(plan, a.id)
                extra = f" '{b.id}' is adjacent to: {', '.join(nb)}." if (b.id not in nb and nb) else ""
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_NOADJ",
                        f"Door between '{a.id}' and '{b.id}' but they don't share a "
                        f"wall (they may only touch at a corner).",
                        room=a.id,
                        hint="Doors only connect rooms with a common wall; reposition "
                        f"them to abut along an edge, or route through a room between them.{extra}",
                        **loc,
                    )
                )
            elif edge.length + 1e-6 < door.width:
                add(
                    Issue(
                        Severity.WARNING,
                        "DOOR_FIT",
                        f"Door ({_f(door.width)} ft) is wider than the shared wall "
                        f"between '{a.id}' and '{b.id}' ({_f(edge.length)} ft).",
                        room=a.id,
                        hint=f"Set the door width to <= {_f(edge.length)}.",
                        **loc,
                    )
                )
            elif door.offset is not None and (
                door.offset < -1e-6
                or door.offset + door.width > edge.length + 1e-6
            ):
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_OOB",
                        f"Door between '{a.id}' and '{b.id}' runs off their "
                        f"{_f(edge.length)} ft shared wall (offset {_f(door.offset)} + "
                        f"width {_f(door.width)}).",
                        room=a.id,
                        hint=f"Keep offset >= 0 and offset + width <= {_f(edge.length)}, "
                        "or drop the offset to centre it.",
                        **loc,
                    )
                )
            if door.swing_into is not None:
                if door.swing_into not in (a.id, b.id):
                    add(
                        Issue(
                            Severity.ERROR,
                            "DOOR_SWING",
                            f"Door swings into '{door.swing_into}', which it doesn't "
                            f"connect (it joins '{a.id}' and '{b.id}').",
                            room=a.id,
                            hint=f"Set `into {a.id}` or `into {b.id}`.",
                            **loc,
                        )
                    )
                elif edge is not None and door.kind == "swing":
                    target = a if door.swing_into == a.id else b
                    depth = target.width if edge.orientation == "v" else target.length
                    if depth + 1e-6 < door.width:
                        add(
                            Issue(
                                Severity.WARNING,
                                "DOOR_SWING",
                                f"A {door.width * 12:.0f} in door can't fully open into "
                                f"'{target.id}' — only {_f(depth)} ft deep at the wall.",
                                room=target.id,
                                hint="Swing it into the other room (`into "
                                f"{(b if target is a else a).id}`), narrow the door, or "
                                "deepen the room.",
                                **loc,
                            )
                        )
                    elif (
                        target.type is RoomType.HALLWAY
                        and depth - door.width + 1e-6 < MIN_HALLWAY_WIDTH
                    ):
                        # It opens, but a leaf swung into a narrow hall leaves less
                        # than a 3 ft passage beside it — it blocks circulation.
                        other = b if target is a else a
                        add(
                            Issue(
                                Severity.INFO,
                                "DOOR_BLOCKS_HALL",
                                f"This door swings into the hallway '{target.id}'; open, "
                                f"its leaf leaves only {_f(max(0.0, depth - door.width))} "
                                "ft of passage, blocking circulation.",
                                room=target.id,
                                hint=f"Swing it into '{other.id}' instead (`into "
                                f"{other.id}`) so the hall stays clear.",
                                **loc,
                            )
                        )
        if door.leaf and door.width < MIN_INTERIOR_DOOR_WIDTH:
            # An open cased passage (leaf=False) is wide by design — the narrow
            # check only applies to swinging doors.
            add(
                Issue(
                    Severity.WARNING,
                    "DOOR_NARROW",
                    f"Interior door between '{door.room_a}' and '{door.room_b}' is "
                    f"{door.width * 12:.0f} in wide.",
                    room=door.room_a,
                    hint=f"Use width >= {MIN_INTERIOR_DOOR_WIDTH:g} "
                    f"({MIN_INTERIOR_DOOR_WIDTH * 12:.0f} in).",
                    **loc,
                )
            )
        elif door.leaf:
            # A swing door that *is* wide enough should still be an orderable size.
            nearest = _nearest_std(door.width * 12, STD_INTERIOR_DOOR_WIDTHS_IN)
            if abs(nearest - door.width * 12) > DOOR_SIZE_TOL_IN:
                add(
                    Issue(
                        Severity.INFO,
                        "DOOR_SIZE",
                        f"Interior door between '{door.room_a}' and '{door.room_b}' is "
                        f"{door.width * 12:.0f} in, not a standard leaf size.",
                        room=door.room_a,
                        hint=f"Use a stock width, e.g. `width {nearest / 12:g}` "
                        f"({nearest} in).",
                        **loc,
                    )
                )
        if not door.leaf and a and b:
            # A walk-through (`open`) gives no privacy; a bathroom needs a door.
            bath = next((r for r in (a, b) if r.type in BATH_TYPES), None)
            if bath is not None:
                other = b if bath.id == a.id else a
                add(
                    Issue(
                        Severity.WARNING,
                        "OPEN_BATH",
                        f"Bathroom '{bath.id}' opens to '{other.id}' through an open "
                        f"passage; a bathroom needs a door for privacy.",
                        room=bath.id,
                        hint=f"Use `door {door.room_a} - {door.room_b}` instead of "
                        f"`open` so the bathroom has a door.",
                        **loc,
                    )
                )

    # Two doors/openings between the same pair share one wall — they can't overlap
    # on it. (Positioned or centred; centred ones coincide, so a stray duplicate
    # connection is caught too.)
    by_pair: dict[frozenset, list] = {}
    for door in plan.interior_doors:
        a, b = plan.room(door.room_a), plan.room(door.room_b)
        if not (a and b) or a.level != b.level:
            continue
        edge = shared_edge(a, b)
        if edge is None:
            continue
        w = min(door.width, edge.length)
        if door.offset is None:
            start = edge.mid - w / 2
        else:
            start = edge.lo + max(0.0, min(door.offset, edge.length - w))
        by_pair.setdefault(frozenset((door.room_a, door.room_b)), []).append(
            (start, start + w, door)
        )
    for spans in by_pair.values():
        if len(spans) < 2:
            continue
        spans.sort(key=lambda s: s[0])
        for (a_lo, a_hi, _), (b_lo, b_hi, d2) in zip(spans, spans[1:]):
            if min(a_hi, b_hi) - max(a_lo, b_lo) > 1e-6:
                add(
                    Issue(
                        Severity.ERROR,
                        "OPENING_CLASH",
                        f"Two doors between '{d2.room_a}' and '{d2.room_b}' overlap "
                        "on their shared wall.",
                        room=d2.room_a,
                        hint="Offset them apart, or use a single door.",
                        **_door_loc(d2),
                    )
                )

    for door in plan.exterior_doors:
        if door.room not in room_ids:
            add(
                Issue(
                    Severity.ERROR,
                    "DOOR_REF",
                    f"Exterior door references unknown room '{door.room}'.",
                    room=door.room,
                    hint="Reference an existing room id.",
                    **_door_loc(door),
                )
            )
            continue
        room = plan.room(door.room)
        if room is not None and room.type in (RoomType.GARAGE, RoomType.SHOP):
            continue  # a garage/shop opening is an overhead door, not a leaf size
        nearest = _nearest_std(door.width * 12, STD_EXTERIOR_DOOR_WIDTHS_IN)
        if abs(nearest - door.width * 12) > DOOR_SIZE_TOL_IN:
            add(
                Issue(
                    Severity.INFO,
                    "DOOR_SIZE",
                    f"Exterior door on '{door.room}' is {door.width * 12:.0f} in, "
                    "not a standard size.",
                    room=door.room,
                    hint=f"Use a stock width, e.g. `width {nearest / 12:g}` "
                    f"({nearest} in); 36 in is the usual entry.",
                    **_door_loc(door),
                )
            )


def _validate_openings(plan: Barndominium, add) -> None:
    """Windows and exterior doors must sit on an exterior wall and fit on it."""
    room_ids = {r.id for r in plan.rooms}

    for w in plan.windows:
        if w.room not in room_ids:
            add(
                Issue(
                    Severity.ERROR,
                    "WINDOW_REF",
                    f"Window references unknown room '{w.room}'.",
                    room=w.room,
                    hint="Reference an existing room id.",
                    **_door_loc(w),
                )
            )
            continue
        room = plan.room(w.room)
        if w.head_height <= w.sill_height + 1e-6:
            add(
                Issue(
                    Severity.WARNING,
                    "WINDOW_SILL",
                    f"Window on '{w.room}' has its head ({_f(w.head_height)} ft) at "
                    f"or below its sill ({_f(w.sill_height)} ft) — it has no glass.",
                    room=w.room,
                    hint="Set head above sill, e.g. `sill 3 head 6.5`.",
                    **_door_loc(w),
                )
            )
        wlen = _wall_length(room, w.wall)
        if w.offset < -1e-6 or w.offset + w.width > wlen + 1e-6:
            add(
                Issue(
                    Severity.ERROR,
                    "OPENING_OOB",
                    f"Window runs off the {w.wall.value} wall of '{w.room}' "
                    f"(offset {_f(w.offset)} + width {_f(w.width)} > wall {_f(wlen)} ft).",
                    room=w.room,
                    hint=f"Keep offset >= 0 and offset + width <= {_f(wlen)}.",
                    **_door_loc(w),
                )
            )
        if w.wall not in exterior_walls(plan, room):
            add(
                Issue(
                    Severity.WARNING,
                    "WINDOW_INTERIOR",
                    f"Window on '{w.room}' is on its {w.wall.value} wall, which is "
                    f"interior — it gives no daylight or egress.",
                    room=w.room,
                    hint="Put the window on a wall that lies on the building "
                    "envelope, or open the room to an exterior space.",
                    **_door_loc(w),
                )
            )

    for d in plan.exterior_doors:
        if d.room not in room_ids:
            continue  # DOOR_REF already raised in _validate_doors
        room = plan.room(d.room)
        wlen = _wall_length(room, d.wall)
        if d.offset < -1e-6 or d.offset + d.width > wlen + 1e-6:
            add(
                Issue(
                    Severity.ERROR,
                    "OPENING_OOB",
                    f"Entry runs off the {d.wall.value} wall of '{d.room}' "
                    f"(offset {_f(d.offset)} + width {_f(d.width)} > wall {_f(wlen)} ft).",
                    room=d.room,
                    hint=f"Keep offset >= 0 and offset + width <= {_f(wlen)}.",
                    **_door_loc(d),
                )
            )
        if d.wall not in exterior_walls(plan, room):
            add(
                Issue(
                    Severity.ERROR,
                    "ENTRY_INTERIOR",
                    f"Exterior door on '{d.room}' is on its {d.wall.value} wall, "
                    f"which is interior — an entrance can't open onto another room.",
                    room=d.room,
                    hint="Place the entry on a wall that lies on the building envelope.",
                    **_door_loc(d),
                )
            )

    # Two openings can't occupy the same run of wall. Group every wall-positioned
    # opening (window or entry) by (room, wall) and flag overlapping spans.
    spans: dict[tuple[str, Direction], list[tuple]] = {}
    for o, kind in (
        *((w, "window") for w in plan.windows),
        *((d, "entry") for d in plan.exterior_doors),
    ):
        if o.room not in room_ids:
            continue  # *_REF already raised
        spans.setdefault((o.room, o.wall), []).append(
            (o.offset, o.offset + o.width, kind, o)
        )
    for (rid, wall), items in spans.items():
        items.sort()  # by start offset
        for (a_lo, a_hi, a_kind, a_o), (b_lo, b_hi, b_kind, b_o) in zip(items, items[1:]):
            ov = min(a_hi, b_hi) - max(a_lo, b_lo)
            if ov > 1e-6:
                add(
                    Issue(
                        Severity.ERROR,
                        "OPENING_CLASH",
                        f"Two openings overlap on the {wall.value} wall of '{rid}': "
                        f"the {a_kind} at {_f(a_lo)}–{_f(a_hi)} ft and the {b_kind} at "
                        f"{_f(b_lo)}–{_f(b_hi)} ft share {_f(ov)} ft.",
                        room=rid,
                        hint="Move one along the wall or narrow it so their offsets "
                        "don't overlap.",
                        **_door_loc(b_o),
                    )
                )


def _stair_rooms(plan: Barndominium, stair, level: int) -> list[Room]:
    """Rooms on ``level`` whose footprint the stair lands in."""
    return [
        r
        for r in plan.rooms
        if r.level == level and stair.overlaps_rect(r.x, r.y, r.x2, r.y2)
    ]


def _validate_stairs(plan: Barndominium, add) -> None:
    for s in plan.stairs:
        if not all(math.isfinite(v) for v in (s.x, s.y, s.width, s.length)):
            add(Issue(Severity.ERROR, "STAIR_GEOMETRY",
                      f"Stair '{s.id}' has non-finite coordinates or size.", room=s.id,
                      hint="Use finite measurements in feet (no nan/inf)."))
            continue
        if s.width <= 0 or s.length <= 0:
            add(Issue(Severity.ERROR, "STAIR_SIZE",
                      f"Stair '{s.id}' has non-positive size.", room=s.id,
                      hint="Use positive feet, e.g. `size 4 x 10`."))
        if s.from_level == s.to_level or s.from_level < 0 or s.to_level < 0:
            add(Issue(Severity.ERROR, "STAIR_LEVELS",
                      f"Stair '{s.id}' must connect two different levels >= 0.",
                      room=s.id, hint="e.g. `from 0 to 1`."))
        over_x = max(0.0, s.x2 - plan.envelope_width)
        over_y = max(0.0, s.y2 - plan.envelope_length)
        if s.x < -1e-6 or s.y < -1e-6 or over_x > 1e-6 or over_y > 1e-6:
            add(Issue(Severity.ERROR, "STAIR_OOB",
                      f"Stair '{s.id}' extends outside the "
                      f"{_f(plan.envelope_width)}×{_f(plan.envelope_length)} ft envelope.",
                      room=s.id, hint="Keep its footprint inside the envelope."))
        # Does the footprint hold the run one storey demands? A straight flight
        # needs (risers-1)·tread of horizontal run; a switchback halves that but
        # needs a footprint wide enough for two flights side by side. Only flag
        # when even a switchback wouldn't fit — keeps this conservative.
        if (
            s.width > 0
            and s.length > 0
            and s.from_level != s.to_level
            and s.from_level >= 0
            and s.to_level >= 0
            and plan.ceiling_height > 0
        ):
            rise = plan.ceiling_height * abs(s.to_level - s.from_level)
            risers = max(1, math.ceil(rise / MAX_RISER_HEIGHT))
            run_needed = max(1, risers - 1) * MIN_TREAD_DEPTH
            long_dim, short_dim = max(s.width, s.length), min(s.width, s.length)
            could_switchback = short_dim + 1e-6 >= 2 * MIN_STAIR_WIDTH
            needed = run_needed / 2 if could_switchback else run_needed
            if long_dim + 1e-6 < needed:
                add(Issue(
                    Severity.WARNING, "STAIR_RUN",
                    f"Stair '{s.id}' is {_f(long_dim)} ft long, too short to climb "
                    f"{_f(rise)} ft: ~{risers} risers need about {_f(run_needed)} ft "
                    "of run (a 7.75 in riser / 10 in tread).",
                    room=s.id,
                    hint=f"Lengthen its footprint to >= {_f(run_needed)} ft, or make it "
                    f">= {_f(2 * MIN_STAIR_WIDTH)} ft wide to fit a switchback."))
        lower = _stair_rooms(plan, s, s.from_level)
        upper = _stair_rooms(plan, s, s.to_level)
        if not lower or not upper:
            missing = []
            if not lower:
                missing.append(f"level {s.from_level}")
            if not upper:
                missing.append(f"level {s.to_level}")
            add(Issue(Severity.WARNING, "STAIR_FLOAT",
                      f"Stair '{s.id}' doesn't land in a room on {', '.join(missing)}.",
                      room=s.id,
                      hint="Position it so its footprint overlaps a room on each level."))


def _validate_access(plan: Barndominium, add) -> None:
    """Every interior room must be reachable from an exterior door."""
    interior_rooms = {r.id for r in plan.rooms if r.type is not RoomType.PORCH}
    if not interior_rooms:
        return

    adjacency: dict[str, set[str]] = {rid: set() for rid in interior_rooms}
    for d in plan.interior_doors:
        if d.room_a in adjacency and d.room_b in adjacency:
            adjacency[d.room_a].add(d.room_b)
            adjacency[d.room_b].add(d.room_a)
    # Stairs link the rooms they land in across levels — that's how an upper
    # floor becomes reachable from the ground.
    for s in plan.stairs:
        lower = [r.id for r in _stair_rooms(plan, s, s.from_level) if r.id in adjacency]
        upper = [r.id for r in _stair_rooms(plan, s, s.to_level) if r.id in adjacency]
        for a in lower:
            for b in upper:
                adjacency[a].add(b)
                adjacency[b].add(a)

    entries = {d.room for d in plan.exterior_doors if d.room in interior_rooms}
    if not entries:
        # Don't also cry NO_ENTRY when entries exist but reference unknown rooms
        # (DOOR_REF already explains that); only when there are no entries at all.
        if not plan.exterior_doors:
            first = next(iter(plan.rooms)).id
            add(
                Issue(
                    Severity.ERROR,
                    "NO_ENTRY",
                    "Plan has no exterior door — no way to enter the building.",
                    hint=f"Add an entrance on an exterior wall, e.g. "
                    f"`entry {first} south width 3 offset 4`.",
                )
            )
        return

    reached: set[str] = set()
    queue: deque[str] = deque(entries)
    while queue:
        cur = queue.popleft()
        if cur in reached:
            continue
        reached.add(cur)
        queue.extend(n for n in adjacency[cur] if n not in reached)

    for rid in sorted(interior_rooms - reached):
        room = plan.room(rid)
        # A loft reaches the floor by stairs, which aren't modelled yet, so an
        # unreachable loft is a warning, not a hard error (like closets/pantries).
        sev = (
            Severity.WARNING
            if room and room.type in (RoomType.CLOSET, RoomType.PANTRY, RoomType.LOFT)
            else Severity.ERROR
        )
        neighbors = geometric_neighbors(plan, rid)
        reachable_nb = [n for n in neighbors if n in reached]
        if reachable_nb:
            hint = f"Add `door {rid} - {reachable_nb[0]}` (they share a wall)."
        elif neighbors:
            hint = (
                f"Connect it into the plan, e.g. `door {rid} - {neighbors[0]}`, "
                f"or give it its own `entry {rid} <wall>`."
            )
        else:
            hint = (
                f"'{rid}' touches no other room — reposition it adjacent to one, "
                f"or add `entry {rid} <wall>`."
            )
        add(
            Issue(
                sev,
                "NO_ACCESS",
                f"Room '{rid}' cannot be reached from any entrance.",
                room=rid,
                hint=hint,
            )
        )


def _validate_design_quality(plan: Barndominium, add) -> None:
    """Soft, advisory checks (INFO) that nudge toward a livable layout.

    These never block compilation — they flow through the *same* diagnostic
    channel as code errors so the author (or the agent) gets quality guidance,
    not just code-compliance. They mirror what a reviewing architect notices:
    open-concept flow, bedroom privacy, and bath proximity.
    """
    graph = _door_graph(plan)
    by_id = {r.id: r for r in plan.rooms}

    # 1. Open-concept flow: a kitchen should connect to dining or living.
    for room in plan.rooms:
        if room.type is RoomType.KITCHEN:
            neigh_types = {by_id[n].type for n in graph.get(room.id, ()) if n in by_id}
            if not neigh_types & {RoomType.DINING, RoomType.LIVING}:
                add(
                    Issue(
                        Severity.INFO,
                        "KITCHEN_FLOW",
                        f"Kitchen '{room.id}' isn't connected to a dining or living area.",
                        room=room.id,
                        hint="Open it to the main living space for an idiomatic "
                        f"barndo, e.g. `door {room.id} - <living_or_dining>`.",
                    )
                )

    # 2. Bedroom privacy: a bedroom shouldn't open straight onto a public room.
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            public_nb = [
                n
                for n in graph.get(room.id, ())
                if n in by_id and by_id[n].type in PUBLIC_TYPES
            ]
            if public_nb:
                onto = by_id[public_nb[0]].type.value
                add(
                    Issue(
                        Severity.INFO,
                        "BED_PRIVACY",
                        f"Bedroom '{room.id}' opens directly onto the "
                        f"{onto} area ('{public_nb[0]}').",
                        room=room.id,
                        hint="Buffer bedrooms with a hallway for privacy — route the "
                        "door off a `hallway` rather than a shared living space.",
                    )
                )

    # 3. Bath proximity: each bedroom should be near a bathroom.
    baths = {r.id for r in plan.rooms if r.type in BATH_TYPES}
    if baths:
        for room in plan.rooms:
            if room.type is RoomType.BEDROOM:
                dist = _nearest_distance(graph, room.id, baths)
                if dist is None or dist > 2:
                    detail = "no connected bathroom" if dist is None else f"{dist} doors away"
                    add(
                        Issue(
                            Severity.INFO,
                            "BATH_DISTANCE",
                            f"Bedroom '{room.id}' is far from any bathroom ({detail}).",
                            room=room.id,
                            hint="Site a bath adjacent to the bedrooms — ideally off "
                            "the same hallway — so it's within a door or two.",
                        )
                    )

    # 4. Pass-through privacy: you shouldn't have to walk through a bathroom (or,
    #    apart from its own ensuite/closet, a bedroom) to get between rooms. This
    #    is a circulation-*shape* defect — reachability alone (NO_ACCESS) misses
    #    it — so it warrants a WARNING, not just an INFO. Remove the gateway rooms
    #    and see what falls off the main body of the house.
    #    (key set, label, types whose isolated clusters are legitimate suites)
    gateways = [
        (BATH_TYPES, "bathroom", frozenset()),
        ({RoomType.BEDROOM}, "bedroom", {RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.CLOSET}),
    ]
    for gate_types, label, suite_exempt in gateways:
        gate_ids = {r.id for r in plan.rooms if r.type in gate_types}
        if not gate_ids:
            continue
        comps = _components_excluding(graph, gate_ids)
        if len(comps) <= 1:
            continue
        main = max(comps, key=len)
        for comp in comps:
            if comp is main:
                continue
            # A bedroom's own ensuite/closet is *meant* to sit behind it.
            if suite_exempt and all(by_id[r].type in suite_exempt for r in comp):
                continue
            for rid in sorted(comp):
                gate = next(
                    (n for n in graph.get(rid, ()) if n in gate_ids), None
                )
                if gate is None:
                    continue  # separated, but not directly off this gateway kind
                add(
                    Issue(
                        Severity.WARNING,
                        "PRIVATE_PASSTHROUGH",
                        f"'{rid}' connects to the rest of the home only through the "
                        f"{label} '{gate}'.",
                        room=rid,
                        hint=f"Route '{rid}' off circulation (a hallway) or a living "
                        f"space instead of through the {label} — e.g. "
                        f"`door {rid} - hall`.",
                    )
                )

    # 5. An exterior entry shouldn't open straight into a bathroom (a real
    #    defect → WARNING) or a bedroom (sometimes a patio door → INFO).
    for d in plan.exterior_doors:
        rt = by_id[d.room].type if d.room in by_id else None
        if rt in BATH_TYPES:
            add(
                Issue(
                    Severity.WARNING,
                    "ENTRY_PRIVATE",
                    f"An exterior entry opens directly into the bathroom '{d.room}'.",
                    room=d.room,
                    hint="Land the entry in a mudroom, hall or living space, not a bath.",
                )
            )
        elif rt is RoomType.BEDROOM:
            add(
                Issue(
                    Severity.INFO,
                    "ENTRY_PRIVATE",
                    f"An exterior entry opens into the bedroom '{d.room}'.",
                    room=d.room,
                    hint="If this isn't a private patio door, route the entrance "
                    "through a public space instead.",
                )
            )

    # 6. Plumbing economy: wet rooms (bath/kitchen/laundry/utility) are cheaper to
    #    run when they share a wall. If there are 3+ but none abut another wet
    #    room, the supply/waste runs are needlessly spread out.
    wet = [r for r in plan.rooms if r.type in WET_TYPES]
    if len(wet) >= 3:
        grouped = any(
            by_id[n].type in WET_TYPES
            for r in wet
            for n in geometric_neighbors(plan, r.id)
            if n in by_id
        )
        if not grouped:
            add(
                Issue(
                    Severity.INFO,
                    "WET_GROUP",
                    f"The {len(wet)} wet rooms (bath/kitchen/laundry) don't share any "
                    "walls; scattered plumbing means longer supply and waste runs.",
                    hint="Group two or more wet rooms back-to-back on a shared wall "
                    "(a 'wet wall') to cut plumbing cost — e.g. site a bath against "
                    "the kitchen or laundry.",
                )
            )

    # 7. Storage: a bedroom needs a closet it can actually use — one reached by a
    #    door/opening *from that bedroom*, not merely a closet that happens to abut
    #    it (which might be a neighbour's, with no way in). Not code, so INFO.
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            own_closet = any(
                by_id[n].type is RoomType.CLOSET
                for n in graph.get(room.id, ())
                if n in by_id
            )
            if not own_closet:
                # Distinguish "no closet at all" from "a closet abuts but with no
                # door" — the second is the subtler omission, so name it.
                abuts_closet = any(
                    by_id[n].type is RoomType.CLOSET
                    for n in geometric_neighbors(plan, room.id)
                    if n in by_id
                )
                detail = (
                    "has a closet beside it but no door into it"
                    if abuts_closet
                    else "has no closet"
                )
                add(
                    Issue(
                        Severity.INFO,
                        "NO_CLOSET",
                        f"Bedroom '{room.id}' {detail}.",
                        room=room.id,
                        hint=f"Give it its own closet with a door, e.g. "
                        f"`room {room.id}_closet: closet east-of {room.id} size 6 x 3` "
                        f"and `door {room.id} - {room.id}_closet`.",
                    )
                )

    # 8. Primary suite: on a floor with two or more full bathrooms, *some* bedroom
    #    should have a private (ensuite) bath rather than every bath only being a
    #    shared hall bath — that's the point of a second bath. INFO (a preference).
    beds_by_level: dict[int, list[Room]] = {}
    full_baths_by_level: dict[int, int] = {}
    for r in plan.rooms:
        if r.type is RoomType.BEDROOM:
            beds_by_level.setdefault(r.level, []).append(r)
        elif r.type is RoomType.BATHROOM:
            full_baths_by_level[r.level] = full_baths_by_level.get(r.level, 0) + 1

    def _bedroom_ensuite(bed_id: str) -> bool:
        # A full bath reached only through this bedroom (a closet off it is fine);
        # a bath also opening to a hall or another room is a shared bath.
        return any(
            by_id[n].type is RoomType.BATHROOM
            and all(
                m == bed_id or (m in by_id and by_id[m].type is RoomType.CLOSET)
                for m in graph.get(n, ())
            )
            for n in graph.get(bed_id, ())
            if n in by_id
        )

    for level, beds in beds_by_level.items():
        nbaths = full_baths_by_level.get(level, 0)
        if nbaths < 2 or not beds:
            continue
        # Satisfied as long as *any* bedroom has a private ensuite — don't depend
        # on an arbitrary "largest bedroom" tiebreak (two equal-area bedrooms used
        # to flip a false positive). The largest just names where to add one.
        if any(_bedroom_ensuite(b.id) for b in beds):
            continue
        master = max(beds, key=lambda r: r.area)
        add(
            Issue(
                Severity.INFO,
                "MASTER_ENSUITE",
                f"This floor has {nbaths} full baths but none is a private ensuite — "
                f"the primary bedroom (e.g. '{master.id}') should have its own.",
                room=master.id,
                hint=f"Make one bath open only off a bedroom, e.g. "
                f"`door {master.id} - <bath>` with that bath connected to nothing else.",
            )
        )

    # 8b. Acoustic buffer: two bedrooms that share a wall pass sound straight
    #     between them. The idiom is to stack each bedroom's closet on that wall
    #     (back-to-back), so the closets buffer the sleeping rooms — once buffered
    #     the bedrooms no longer share a wall and this clears.
    beds = [r for r in plan.rooms if r.type is RoomType.BEDROOM]
    for i, ba in enumerate(beds):
        for bb in beds[i + 1 :]:
            edge = shared_edge(ba, bb)
            if edge is not None and edge.length + 1e-6 >= MIN_SOUND_BUFFER_WALL:
                add(
                    Issue(
                        Severity.INFO,
                        "BED_SOUND",
                        f"Bedrooms '{ba.id}' and '{bb.id}' share a {_f(edge.length)} ft "
                        "wall — sound carries straight between the sleeping rooms.",
                        room=ba.id,
                        hint="Buffer them: stack a closet on each side of the shared "
                        "wall (back-to-back), or put a hall/closet between the bedrooms.",
                    )
                )

    # 8c. Walk-in vs long, skinny closet: a closet with the floor area for a
    #     walk-in but shaped as a narrow strip wastes that floor. Small reach-ins
    #     (under the walk-in area) and wide/shallow closets (under the aspect
    #     bar) are fine and exempt.
    for room in plan.rooms:
        if room.type is RoomType.CLOSET:
            short = room.min_dimension
            long = max(room.width, room.length)
            if (
                short > 1e-6
                and room.area >= MIN_WALKIN_AREA
                and long / short >= CLOSET_WALKIN_ASPECT
            ):
                add(
                    Issue(
                        Severity.INFO,
                        "CLOSET_SHAPE",
                        f"Closet '{room.id}' is {_f(room.width)} x {_f(room.length)} "
                        f"({long / short:.1f}:1) — a long, skinny closet.",
                        room=room.id,
                        hint="With this much floor a walk-in is more usable — aim for a "
                        "more square footprint (under ~3:1), at least 4 ft deep.",
                    )
                )

    # 8d. Comfort width: a hall at the 3 ft code minimum passes but feels tight
    #     for two people or moving furniture; 4 ft is the comfortable target.
    for room in plan.rooms:
        if (
            room.type is RoomType.HALLWAY
            and MIN_HALLWAY_WIDTH <= room.min_dimension < COMFORT_HALLWAY_WIDTH - 1e-9
        ):
            add(
                Issue(
                    Severity.INFO,
                    "HALL_TIGHT",
                    f"Hallway '{room.id}' is {_f(room.min_dimension)} ft wide — legal "
                    f"(>= {MIN_HALLWAY_WIDTH:.0f} ft) but tight; {COMFORT_HALLWAY_WIDTH:.0f} "
                    "ft is comfortable for two people and moving furniture.",
                    room=room.id,
                    hint=f"Widen it to >= {COMFORT_HALLWAY_WIDTH:.0f} ft.",
                )
            )

    # 8e. Front *and* back door: a home wants a second exterior door (a back/side
    #     door off the kitchen, mudroom or laundry) — for daily flow and a second
    #     way out. Garage/porch doors don't count as the house's back door.
    people_doors = [
        d
        for d in plan.exterior_doors
        if d.room in by_id and by_id[d.room].type not in (RoomType.GARAGE, RoomType.PORCH)
    ]
    if plan.rooms and plan.exterior_doors and len(people_doors) < 2:
        where = (
            f" (only one, on '{people_doors[0].room}')"
            if people_doors
            else " (the only exterior door is a garage/utility door)"
        )
        add(
            Issue(
                Severity.INFO,
                "NO_BACK_DOOR",
                f"Plan has a single exterior entrance{where}; a home wants a front "
                "and a back door.",
                hint="Add a second exterior door on another wall — e.g. off the "
                "kitchen, a mudroom or the laundry — `entry <room> <wall>`.",
            )
        )

    # 8f. Ensuite proportion: a private bath shouldn't be larger than the bedroom
    #     it serves — that's a sign the suite is mis-proportioned. Only judged for
    #     a true ensuite (a bath reached only through this bedroom, closets aside).
    for bed in (r for r in plan.rooms if r.type is RoomType.BEDROOM):
        for n in graph.get(bed.id, ()):
            bath = by_id.get(n)
            if bath is None or bath.type is not RoomType.BATHROOM:
                continue
            private = all(
                m == bed.id or (m in by_id and by_id[m].type is RoomType.CLOSET)
                for m in graph.get(bath.id, ())
            )
            if private and bath.area > bed.area + 1e-6:
                add(
                    Issue(
                        Severity.INFO,
                        "BATH_OVERSIZE",
                        f"Ensuite '{bath.id}' ({_f(bath.area)} sq ft) is larger than the "
                        f"bedroom '{bed.id}' it serves ({_f(bed.area)} sq ft).",
                        room=bath.id,
                        hint="A bath should be the same size or smaller than its "
                        "bedroom — shrink the bath or enlarge the bedroom.",
                    )
                )

    # 8g. A door needs clear floor in front of it; a stair landing intruding on a
    #     doorway blocks it (you step off the stair straight into a swinging door).
    for s in plan.stairs:
        levels = (s.from_level, s.to_level)
        blocked: set[str] = set()
        for d in plan.interior_doors:
            a, b = by_id.get(d.room_a), by_id.get(d.room_b)
            if a is None or b is None or a.level != b.level or a.level not in levels:
                continue
            edge = shared_edge(a, b)
            if edge is None:
                continue
            lo, hi = _door_interval(edge, d)
            zx1, zy1, zx2, zy2 = _zone_from_edge(edge, lo, hi, DOOR_CLEARANCE_DEPTH)
            key = f"{a.id}-{b.id}"
            if key not in blocked and s.overlaps_rect(zx1, zy1, zx2, zy2):
                blocked.add(key)
                add(
                    Issue(
                        Severity.WARNING,
                        "STAIR_BLOCKS_DOOR",
                        f"Stair '{s.id}' intrudes on the clear floor in front of the "
                        f"'{a.id}'-'{b.id}' doorway, blocking it.",
                        room=s.id,
                        hint="Shift the stair off the doorway (place it along a wall), "
                        "or move the door so its approach is clear.",
                    )
                )
        for d in plan.exterior_doors:
            r = by_id.get(d.room)
            if r is None or r.level not in levels:
                continue
            zx1, zy1, zx2, zy2 = _ext_door_zone(r, d, DOOR_CLEARANCE_DEPTH)
            key = f"ext:{d.room}:{d.wall.value}"
            if key not in blocked and s.overlaps_rect(zx1, zy1, zx2, zy2):
                blocked.add(key)
                add(
                    Issue(
                        Severity.WARNING,
                        "STAIR_BLOCKS_DOOR",
                        f"Stair '{s.id}' intrudes on the clear floor in front of the "
                        f"exterior door into '{d.room}', blocking it.",
                        room=s.id,
                        hint="Shift the stair off the doorway (place it along a wall), "
                        "or move the entry so its approach is clear.",
                    )
                )

    # 8h. Stairs belong along a wall, not marooned in the middle of a room (where
    #     they'd need railings all round and chop up the floor). We can't model
    #     mid-flight landings/turns, but we can flag a free-floating flight.
    for s in plan.stairs:
        if not _stair_against_wall(plan, s):
            add(
                Issue(
                    Severity.INFO,
                    "STAIR_WALL",
                    f"Stair '{s.id}' floats free of any wall — place it along an "
                    "exterior or partition wall.",
                    room=s.id,
                    hint="Move it so a long side runs against a wall; for a tall climb "
                    "an L-shaped run with a mid landing keeps the footprint compact.",
                )
            )

    # 8i. Door position: a swing door centred on a wall with usable wall on *both*
    #     flanks wastes the room — backing it to a corner leaves an unbroken run to
    #     line with furniture. Only swing leaves the author left to default (no
    #     explicit offset); cased/sliding and deliberately-placed doors are exempt.
    for d in plan.interior_doors:
        if d.kind != "swing" or d.offset is not None:
            continue
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None:
            continue
        if not (a.type in HABITABLE_TYPES or b.type in HABITABLE_TYPES):
            continue
        edge = shared_edge(a, b)
        if edge is None:
            continue
        margin = (edge.length - d.width) / 2.0
        if margin > DOOR_CORNER_MARGIN:
            add(
                Issue(
                    Severity.INFO,
                    "DOOR_CENTERED",
                    f"Door '{a.id}'-'{b.id}' is centred on a {_f(edge.length)} ft wall, "
                    f"floating {_f(margin)} ft from each corner.",
                    room=a.id,
                    hint=f"Back it toward a corner — e.g. `door {a.id} - {b.id} ... "
                    "offset <n>` — so one side keeps a full wall run for furniture.",
                    **_door_loc(d),
                )
            )

    # 8j. Windows shouldn't butt an interior partition where it meets the exterior
    #     wall — there's no room for framing/trim and it reads as off-balance. (A
    #     window flush to a true *building* corner is fine.)
    for w in plan.windows:
        r = by_id.get(w.room)
        if r is None or w.wall not in exterior_walls(plan, r):
            continue
        wall_len = _wall_length(r, w.wall)
        near, far = w.offset, w.offset + w.width
        hit_start = near < WINDOW_WALL_CLEAR and not _building_corner(plan, r, w.wall, False)
        hit_end = (wall_len - far) < WINDOW_WALL_CLEAR and not _building_corner(
            plan, r, w.wall, True
        )
        if hit_start or hit_end:
            add(
                Issue(
                    Severity.INFO,
                    "WINDOW_PARTITION",
                    f"Window on the {w.wall.value} wall of '{w.room}' sits against an "
                    "interior partition — it'll collide with the wall framing/trim.",
                    room=w.room,
                    hint="Pull it toward the wall's centre (or a building corner) so it "
                    "has clear wall on both sides; space multiple windows evenly.",
                    **_door_loc(w),
                )
            )

    # 8. Proportion: a habitable room shaped like a bowling alley is hard to
    #    furnish. Hallways/closets are *meant* to be skinny — they're not habitable,
    #    so the HABITABLE_TYPES gate already excludes them.
    for room in plan.rooms:
        if room.type in HABITABLE_TYPES:
            short = room.min_dimension
            long = max(room.width, room.length)
            if short > 1e-6 and long / short > MAX_ROOM_ASPECT:
                add(
                    Issue(
                        Severity.INFO,
                        "ROOM_PROPORTION",
                        f"{room.type.value.capitalize()} '{room.id}' is "
                        f"{_f(room.width)} x {_f(room.length)} ({long / short:.1f}:1); "
                        "very elongated rooms are hard to furnish.",
                        room=room.id,
                        hint="Aim for a more rectangular footprint (under ~3:1) — "
                        "widen the short side or split the space.",
                    )
                )

    # 9. Garage → sleeping room. IRC R302.5.1: a garage opening shall not open
    #    into a room used for sleeping. This is code-grounded, so it's a WARNING.
    garages = [r for r in plan.rooms if r.type is RoomType.GARAGE]
    for g in garages:
        for n in graph.get(g.id, ()):
            if n in by_id and by_id[n].type is RoomType.BEDROOM:
                add(
                    Issue(
                        Severity.WARNING,
                        "GARAGE_BEDROOM",
                        f"Garage '{g.id}' opens directly into the bedroom '{n}'; a "
                        "garage must not open into a sleeping room (IRC R302.5.1).",
                        room=n,
                        hint=f"Buffer it with a mudroom or hall — connect the garage "
                        f"there instead, e.g. `door {g.id} - <mudroom_or_hall>`.",
                    )
                )

    # 10. Garage with no interior people-door into the house. A vehicle `entry`
    #     satisfies reachability (NO_ACCESS), so this gap slips through: you'd have
    #     to go outside to get in. Only nudge when it actually abuts the house.
    house_types = {
        t for t in RoomType if t not in (RoomType.GARAGE, RoomType.PORCH)
    }
    for g in garages:
        connected_inside = any(
            n in by_id and by_id[n].type in house_types for n in graph.get(g.id, ())
        )
        if connected_inside:
            continue
        abuts_house = any(
            n in by_id and by_id[n].type in house_types
            for n in geometric_neighbors(plan, g.id)
        )
        if abuts_house:
            add(
                Issue(
                    Severity.INFO,
                    "GARAGE_NO_ENTRY",
                    f"Garage '{g.id}' has no interior door into the house — you'd "
                    "have to go outside to get in.",
                    room=g.id,
                    hint="Add a people-door from the garage into a mudroom, hall or "
                    f"living space, e.g. `door {g.id} - <adjacent_room>`.",
                )
            )

    # 11. A hallway exists to *distribute* circulation. One that opens onto a
    #     single room (or none) is just overhead. Exempt a hall that carries an
    #     exterior entry — a foyer/vestibule is legitimately a one-room hall.
    hall_entries = {d.room for d in plan.exterior_doors}
    for room in plan.rooms:
        if room.type is not RoomType.HALLWAY:
            continue
        served = len(graph.get(room.id, ()))
        if served <= 1 and room.id not in hall_entries:
            add(
                Issue(
                    Severity.INFO,
                    "HALL_DEADEND",
                    f"Hallway '{room.id}' opens onto {served} room(s); a hall that "
                    "serves one room isn't earning its footprint.",
                    room=room.id,
                    hint="Open that room off a larger space and drop the hall, or "
                    "extend the hall so it distributes to more rooms.",
                )
            )
            continue
        # A hall that *does* distribute can still waste a stub: it runs on past its
        # last doorway into a blank wall, so you walk into a dead end and back out.
        long_x = room.width >= room.length
        axis_lo, axis_hi = (room.x, room.x2) if long_x else (room.y, room.y2)
        if axis_hi - axis_lo <= room.min_dimension + 1e-6:
            continue  # roughly square (a foyer/landing), not a corridor
        marks: list[tuple[float, float]] = []
        for n in graph.get(room.id, ()):
            nb = by_id.get(n)
            edge = shared_edge(room, nb) if nb else None
            if edge is None:
                continue
            along_axis = (edge.orientation == "h") if long_x else (edge.orientation == "v")
            marks.append((edge.lo, edge.hi) if along_axis else (edge.pos, edge.pos))
        for d in plan.exterior_doors:
            if d.room != room.id:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, d.wall, d.offset, d.width)
            ns = d.wall in (Direction.NORTH, Direction.SOUTH)
            if long_x:
                marks.append((min(x1, x2), max(x1, x2)) if ns else (x1, x1))
            else:
                marks.append((min(y1, y2), max(y1, y2)) if not ns else (y1, y1))
        if not marks:
            continue
        served_lo = min(m[0] for m in marks)
        served_hi = max(m[1] for m in marks)
        stub = max(served_lo - axis_lo, axis_hi - served_hi)
        if stub >= MIN_HALL_STUB:
            add(
                Issue(
                    Severity.INFO,
                    "HALL_DEADEND",
                    f"Hallway '{room.id}' runs {_f(stub)} ft past its last doorway "
                    "into a blank wall — a dead-end stub of circulation.",
                    room=room.id,
                    hint="Trim the hall back to its last door, or put a room/closet at "
                    "the dead end so the run is earning its footprint.",
                )
            )


def _validate_program(plan: Barndominium, add) -> None:
    """Check the rooms placed against a declared ``program`` (if any).

    This is the mechanical guard for "compiles clean but isn't what I asked for":
    a clean compile means the *code* is valid, not that you met the brief. When
    the author declares the intended counts, a dropped (or surplus) bedroom/bath
    surfaces as a ``PROGRAM_MISMATCH`` warning instead of slipping through.
    """
    spec = plan.program_spec
    if spec is None:
        return
    m = plan.metrics()
    actual_beds = int(m["bedroom_count"])
    actual_baths = int(m["bathroom_count"])
    mismatches = []
    if spec.beds != actual_beds:
        mismatches.append(f"{spec.beds} bedroom(s) declared but {actual_beds} placed")
    if spec.baths is not None and spec.baths != actual_baths:
        mismatches.append(f"{spec.baths} bath(s) declared but {actual_baths} placed")
    # Required room types are an *at-least* check: a declared room that's missing
    # (or short) is flagged, but a surplus never is.
    type_counts: dict[RoomType, int] = {}
    for r in plan.rooms:
        type_counts[r.type] = type_counts.get(r.type, 0) + 1
    for rtype, need in spec.required.items():
        have = type_counts.get(rtype, 0)
        if have < need:
            if need == 1:
                mismatches.append(f"no {rtype.value} placed")
            else:
                mismatches.append(
                    f"{need} {rtype.value}(s) declared but {have} placed"
                )
    if spec.min_area is not None:
        interior = m["interior_sqft"]
        if math.isfinite(interior) and interior + 1e-6 < spec.min_area:
            mismatches.append(
                f"{_f(spec.min_area)} sq ft declared but {interior:.0f} placed"
            )
    if not mismatches:
        return
    loc = {}
    if spec.line is not None:
        loc = {"line": spec.line, "col": spec.col, "end_col": spec.end_col}
    add(
        Issue(
            Severity.WARNING,
            "PROGRAM_MISMATCH",
            "Plan doesn't match its declared program: " + "; ".join(mismatches) + ".",
            hint="Add or remove rooms to match, or update the `program` line to the "
            "intent you mean (counts, required rooms, or `area`).",
            **loc,
        )
    )


def _validate_egress_and_light(plan: Barndominium, add) -> None:
    has_egress_door = any(
        d.egress and d.width + 1e-6 >= MIN_EGRESS_DOOR_WIDTH for d in plan.exterior_doors
    )
    if plan.exterior_doors and not has_egress_door:
        add(
            Issue(
                Severity.WARNING,
                "EGRESS_DOOR",
                f"No exterior egress door is at least {MIN_EGRESS_DOOR_WIDTH * 12:.0f} in wide.",
                hint=f"Make at least one `entry` width >= {MIN_EGRESS_DOOR_WIDTH:g}.",
            )
        )

    for room in plan.rooms:
        walls = exterior_walls(plan, room)
        if room.type is RoomType.BEDROOM:
            # Only an opening on an exterior wall counts as an escape route.
            ext_windows = [w for w in plan.windows_for(room.id) if w.wall in walls]
            ext_doors = [d for d in plan.exterior_doors_for(room.id) if d.wall in walls]
            if not ext_windows and not ext_doors:
                if walls:
                    hint = (
                        f"Add an egress window on an exterior wall, e.g. "
                        f"`window {room.id} {walls[0].value} width 4 offset 2`."
                    )
                else:
                    hint = (
                        f"'{room.id}' has no exterior wall — relocate it to the "
                        "building perimeter so it can have an egress window."
                    )
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_EGRESS",
                        "Bedroom has no emergency escape opening.",
                        room=room.id,
                        hint=hint,
                    )
                )
            else:
                # An escape opening exists — does it meet the R310 clear-opening
                # minimums? A full-height door qualifies on width alone; a window
                # must clear the area and both dimensions and sit low enough.
                min_area = MIN_EGRESS_AREA_GRADE if room.level == 0 else MIN_EGRESS_AREA

                def _win_ok(w) -> bool:
                    h = max(0.0, w.head_height - w.sill_height)
                    return (
                        w.width + 1e-6 >= MIN_EGRESS_OPENING_WIDTH
                        and h + 1e-6 >= MIN_EGRESS_OPENING_HEIGHT
                        and w.width * h + 1e-6 >= min_area
                        and w.sill_height <= MAX_EGRESS_SILL + 1e-6
                    )

                door_ok = any(
                    d.width + 1e-6 >= MIN_EGRESS_OPENING_WIDTH for d in ext_doors
                )
                if not (door_ok or any(_win_ok(w) for w in ext_windows)):
                    if ext_windows:
                        best = max(ext_windows, key=lambda w: w.glazed_area)
                        h = max(0.0, best.head_height - best.sill_height)
                        detail = (
                            f"its largest is {_f(best.width)} ft wide × {_f(h)} ft "
                            f"({_f(best.width * h)} sq ft, sill {best.sill_height * 12:.0f} in)"
                        )
                    else:
                        detail = "its only exterior opening is a too-narrow door"
                    add(
                        Issue(
                            Severity.WARNING,
                            "EGRESS_SIZE",
                            f"Bedroom '{room.id}' has an escape opening but it's below "
                            f"the IRC R310 minimum ({_f(min_area)} sq ft clear, "
                            f"{MIN_EGRESS_OPENING_WIDTH * 12:.0f} in wide × "
                            f"{MIN_EGRESS_OPENING_HEIGHT * 12:.0f} in tall, sill "
                            f"<= {MAX_EGRESS_SILL * 12:.0f} in); {detail}.",
                            room=room.id,
                            hint=f"Widen/enlarge the egress window so its clear opening "
                            f"is >= {_f(min_area)} sq ft, e.g. "
                            f"`window {room.id} {(walls[0].value if walls else 'south')} "
                            f"width 4 offset 2`.",
                        )
                    )

        if room.type in BATH_TYPES:
            # A bathroom needs light+ventilation: a window on an exterior wall, or
            # else mechanical exhaust. The DSL doesn't model fans, so a windowless
            # bath gets an info nudge to confirm one (IRC R303.3).
            has_window = any(w.wall in walls for w in plan.windows_for(room.id))
            if not has_window:
                add(
                    Issue(
                        Severity.INFO,
                        "BATH_VENT",
                        f"Bathroom '{room.id}' has no exterior window; it needs "
                        "mechanical ventilation (IRC R303.3).",
                        room=room.id,
                        hint="Add an exterior window, or confirm an exhaust fan vented "
                        "outside — the DSL can't see fans, so this is just a reminder.",
                    )
                )

        if room.type in HABITABLE_TYPES:
            glazing = sum(
                w.glazed_area for w in plan.windows_for(room.id) if w.wall in walls
            )
            required = room.area * NATURAL_LIGHT_RATIO
            if glazing + 1e-6 < required:
                add_width = max(0.0, (required - glazing) / _WINDOW_TYP_HEIGHT)
                suggest = _suggest_int(add_width)
                if walls and suggest is not None:
                    hint = (
                        f"Add ~{suggest} ft of window width on an exterior wall, "
                        f"e.g. `window {room.id} {walls[0].value} "
                        f"width {max(3, suggest)} offset 2`."
                    )
                elif walls:
                    hint = (
                        f"Add more window area on an exterior wall "
                        f"(e.g. the {walls[0].value} wall)."
                    )
                else:
                    hint = (
                        f"'{room.id}' has no exterior wall — open it to an adjacent "
                        "room (an open-concept layout) or move it to the perimeter."
                    )
                add(
                    Issue(
                        Severity.WARNING,
                        "NAT_LIGHT",
                        f"Glazing {_f(glazing)} sq ft is below the natural-light "
                        f"minimum of {_f(required)} sq ft "
                        f"({NATURAL_LIGHT_RATIO * 100:.0f}% of floor area).",
                        room=room.id,
                        hint=hint,
                    )
                )
