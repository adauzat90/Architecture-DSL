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
from .geometry import shared_edge

# Approximate IRC-derived thresholds (feet unless noted).
MIN_CEILING = 7.0
MIN_BEDROOM_AREA = 70.0
MIN_BEDROOM_DIMENSION = 7.0
MIN_HALLWAY_WIDTH = 3.0  # 36 in
MIN_EGRESS_DOOR_WIDTH = 2.67  # ~32 in clear (3'0" leaf)
MIN_INTERIOR_DOOR_WIDTH = 2.5  # 30 in
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
    hint: str | None = None

    def __str__(self) -> str:
        loc = f"line {self.line}: " if self.line else ""
        where = f" ({self.room})" if self.room else ""
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
    """Walls of ``room`` that lie on the building envelope (can take windows)."""
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


def geometric_neighbors(plan: Barndominium, room_id: str) -> list[str]:
    """Ids of rooms that share a wall segment with ``room_id``."""
    r = plan.room(room_id)
    if r is None:
        return []
    return [o.id for o in plan.rooms if o.id != room_id and shared_edge(r, o)]


def _f(value: float) -> str:
    """Format a measurement: drop a trailing .0 (so 28.0 -> '28')."""
    return f"{value:g}"


# --- entry point ------------------------------------------------------------


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
    _validate_access(plan, add)
    _validate_egress_and_light(plan, add)

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
        if room.x < -1e-6 or room.y < -1e-6 or over_x > 1e-6 or over_y > 1e-6:
            fixes = []
            if room.x < 0:
                fixes.append(f"set its x to >= 0")
            if room.y < 0:
                fixes.append(f"set its y to >= 0")
            if over_x > 1e-6:
                fixes.append(
                    f"reduce its width by {_f(over_x)} ft or move it west to "
                    f"x={_f(plan.envelope_width - room.width)}"
                )
            if over_y > 1e-6:
                fixes.append(
                    f"reduce its length by {_f(over_y)} ft or move it south to "
                    f"y={_f(plan.envelope_length - room.length)}"
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
            ov = a.overlaps(b)
            if ov > 0.5:  # ignore hairline floating-point overlaps
                ox = min(a.x2, b.x2) - max(a.x, b.x)
                oy = min(a.y2, b.y2) - max(a.y, b.y)
                if ox <= oy:
                    sug = f"move '{b.id}' to x={_f(a.x2)} (east of '{a.id}')"
                else:
                    sug = f"move '{b.id}' to y={_f(a.y2)} (north of '{a.id}')"
                add(
                    Issue(
                        Severity.ERROR,
                        "OVERLAP",
                        f"Rooms '{a.id}' and '{b.id}' overlap by {_f(ov)} sq ft.",
                        room=a.id,
                        hint=f"Reposition so they don't intersect — e.g. {sug}.",
                    )
                )

    used = plan.assigned_area
    if plan.footprint_area > 0:
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
                need_len = math.ceil(MIN_BEDROOM_AREA / max(room.width, 1e-6))
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_AREA",
                        f"Bedroom is {_f(room.area)} sq ft; IRC minimum is "
                        f"{MIN_BEDROOM_AREA:.0f} sq ft.",
                        room=room.id,
                        hint=f"Enlarge it, e.g. `size {_f(room.width)} x {need_len}`.",
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


def _validate_doors(plan: Barndominium, add) -> None:
    room_ids = {r.id for r in plan.rooms}
    for door in plan.interior_doors:
        for rid in (door.room_a, door.room_b):
            if rid not in room_ids:
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_REF",
                        f"Interior door references unknown room '{rid}'.",
                        room=rid,
                        hint="Reference an existing room id, or declare the room.",
                    )
                )
        a, b = plan.room(door.room_a), plan.room(door.room_b)
        if a and b:
            edge = shared_edge(a, b)
            if edge is None:
                add(
                    Issue(
                        Severity.ERROR,
                        "DOOR_NOADJ",
                        f"Door between '{a.id}' and '{b.id}' but they don't share a wall.",
                        room=a.id,
                        hint="Doors only connect rooms with a common wall; reposition "
                        "them to be adjacent, or route through a room between them.",
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
                )
            )


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

    entries = {d.room for d in plan.exterior_doors if d.room in interior_rooms}
    if not entries:
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
        sev = (
            Severity.WARNING
            if room and room.type in (RoomType.CLOSET, RoomType.PANTRY)
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
            has_window = bool(plan.windows_for(room.id))
            has_ext_door = bool(plan.exterior_doors_for(room.id))
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
            glazing = sum(w.glazed_area for w in plan.windows_for(room.id))
            required = room.area * NATURAL_LIGHT_RATIO
            if glazing + 1e-6 < required:
                add_width = max(0.0, (required - glazing) / _WINDOW_TYP_HEIGHT)
                if walls:
                    hint = (
                        f"Add ~{add_width:.0f} ft of window width on an exterior wall, "
                        f"e.g. `window {room.id} {walls[0].value} "
                        f"width {max(3, math.ceil(add_width))} offset 2`."
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
