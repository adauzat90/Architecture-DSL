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
from .geometry import rect_in_footprint, shared_edge, wall_faces_outside

# Approximate IRC-derived thresholds (feet unless noted).
MIN_CEILING = 7.0
MIN_BEDROOM_AREA = 70.0
MIN_BEDROOM_DIMENSION = 7.0
MIN_HALLWAY_WIDTH = 3.0  # 36 in
MIN_EGRESS_DOOR_WIDTH = 32 / 12  # 32 in clear; matches inches(32) exactly
MIN_INTERIOR_DOOR_WIDTH = 30 / 12  # 30 in
NATURAL_LIGHT_RATIO = 0.08  # glazing >= 8% of floor area
_WINDOW_TYP_HEIGHT = 3.67  # head - sill for a typical window, ft


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
    def is_valid(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        e, w = len(self.errors), len(self.warnings)
        status = "VALID" if self.is_valid else "INVALID"
        return f"{status} — {e} error(s), {w} warning(s)"

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
    return walls


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
                add(
                    Issue(
                        Severity.ERROR,
                        "OVERLAP",
                        f"Rooms '{a.id}' and '{b.id}' overlap by {_f(ov)} sq ft.",
                        room=a.id,
                        hint=f"Reposition so they don't intersect — e.g. {sug}.",
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
        if door.width < MIN_INTERIOR_DOOR_WIDTH:
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
                      f"Stair '{s.id}' has non-finite coordinates or size.", room=s.id))
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
            has_window = any(w.wall in walls for w in plan.windows_for(room.id))
            has_ext_door = any(
                d.wall in walls for d in plan.exterior_doors_for(room.id)
            )
            if not (has_window or has_ext_door):
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
