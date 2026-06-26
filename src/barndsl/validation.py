"""Constraint and building-code validation for barndominium plans.

The checks are loosely modelled on the International Residential Code (IRC) plus
common-sense spatial constraints. They are intentionally approximate — enough to
catch the mistakes an LLM (or a human) most often makes when laying out a plan,
and to give the agent something concrete to iterate against. They are **not** a
substitute for a licensed designer or an authority-having-jurisdiction review.
"""

from __future__ import annotations

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
VENT_FALLBACK_TYPES = {RoomType.BATHROOM, RoomType.HALF_BATH}


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Issue:
    severity: Severity
    code: str
    message: str
    room: str | None = None

    def __str__(self) -> str:
        where = f" [{self.room}]" if self.room else ""
        return f"{self.severity.value.upper():7} {self.code}{where}: {self.message}"


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
        lines = [self.summary()]
        lines += [str(i) for i in self.issues]
        return "\n".join(lines)


def _room_within_envelope(plan: Barndominium, room: Room, tol: float = 1e-6) -> bool:
    return (
        room.x >= -tol
        and room.y >= -tol
        and room.x2 <= plan.envelope_width + tol
        and room.y2 <= plan.envelope_length + tol
    )


def _room_is_on_exterior(plan: Barndominium, room: Room, tol: float = 1e-6) -> bool:
    """Whether any of the room's walls lies on the building envelope."""
    return (
        abs(room.x) <= tol
        or abs(room.y) <= tol
        or abs(room.x2 - plan.envelope_width) <= tol
        or abs(room.y2 - plan.envelope_length) <= tol
    )


def validate(plan: Barndominium) -> ValidationReport:
    """Run all checks and return a :class:`ValidationReport`."""
    issues: list[Issue] = []
    add = issues.append

    # --- Envelope sanity --------------------------------------------------
    if plan.envelope_width <= 0 or plan.envelope_length <= 0:
        add(Issue(Severity.ERROR, "ENVELOPE", "Envelope must have positive dimensions."))
    if plan.ceiling_height < MIN_CEILING:
        add(
            Issue(
                Severity.ERROR,
                "CEILING",
                f"Ceiling height {plan.ceiling_height:.1f} ft is below the "
                f"{MIN_CEILING:.0f} ft minimum for habitable space.",
            )
        )
    if not plan.rooms:
        add(Issue(Severity.ERROR, "EMPTY", "Plan has no rooms."))
        return ValidationReport(issues)

    ids = [r.id for r in plan.rooms]
    dupes = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dupes):
        add(Issue(Severity.ERROR, "DUP_ID", f"Duplicate room id '{d}'.", d))

    # --- Containment & overlap -------------------------------------------
    for room in plan.rooms:
        if room.width <= 0 or room.length <= 0:
            add(Issue(Severity.ERROR, "ROOM_SIZE", "Room has non-positive size.", room.id))
        if not _room_within_envelope(plan, room):
            add(
                Issue(
                    Severity.ERROR,
                    "OUT_OF_BOUNDS",
                    f"Room extends outside the {plan.envelope_width:.0f}×"
                    f"{plan.envelope_length:.0f} ft envelope "
                    f"(occupies {room.x:.1f},{room.y:.1f} → {room.x2:.1f},{room.y2:.1f}).",
                    room.id,
                )
            )

    for i, a in enumerate(plan.rooms):
        for b in plan.rooms[i + 1 :]:
            ov = a.overlaps(b)
            if ov > 0.5:  # ignore hairline floating-point overlaps
                add(
                    Issue(
                        Severity.ERROR,
                        "OVERLAP",
                        f"Rooms '{a.id}' and '{b.id}' overlap by {ov:.1f} sq ft.",
                        a.id,
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
                    f"Assigned room area ({used:.0f} sq ft) exceeds the footprint "
                    f"({plan.footprint_area:.0f} sq ft).",
                )
            )
        elif frac < 0.85:
            add(
                Issue(
                    Severity.INFO,
                    "AREA_UNUSED",
                    f"Only {frac * 100:.0f}% of the footprint is assigned to rooms; "
                    f"{plan.footprint_area - used:.0f} sq ft unallocated.",
                )
            )

    _validate_room_programs(plan, add)
    _validate_doors(plan, add)
    _validate_access(plan, add)
    _validate_egress_and_light(plan, add)

    if not plan.metrics()["bathroom_count"]:
        add(Issue(Severity.WARNING, "NO_BATH", "Plan has no bathroom."))

    return ValidationReport(issues)


def _validate_room_programs(plan: Barndominium, add) -> None:
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            if room.area < MIN_BEDROOM_AREA:
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_AREA",
                        f"Bedroom is {room.area:.0f} sq ft; IRC minimum is "
                        f"{MIN_BEDROOM_AREA:.0f} sq ft.",
                        room.id,
                    )
                )
            if room.min_dimension < MIN_BEDROOM_DIMENSION:
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_DIM",
                        f"Bedroom's smallest dimension is {room.min_dimension:.1f} ft; "
                        f"minimum is {MIN_BEDROOM_DIMENSION:.0f} ft.",
                        room.id,
                    )
                )
        if room.type is RoomType.HALLWAY and room.min_dimension < MIN_HALLWAY_WIDTH:
            add(
                Issue(
                    Severity.ERROR,
                    "HALL_WIDTH",
                    f"Hallway is {room.min_dimension:.1f} ft wide; minimum is "
                    f"{MIN_HALLWAY_WIDTH:.0f} ft.",
                    room.id,
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
                        rid,
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
                        a.id,
                    )
                )
            elif edge.length + 1e-6 < door.width:
                add(
                    Issue(
                        Severity.WARNING,
                        "DOOR_FIT",
                        f"Door ({door.width:.1f} ft) is wider than the shared wall "
                        f"between '{a.id}' and '{b.id}' ({edge.length:.1f} ft).",
                        a.id,
                    )
                )
        if door.width < MIN_INTERIOR_DOOR_WIDTH:
            add(
                Issue(
                    Severity.WARNING,
                    "DOOR_NARROW",
                    f"Interior door between '{door.room_a}' and '{door.room_b}' is "
                    f"{door.width * 12:.0f} in wide; {MIN_INTERIOR_DOOR_WIDTH * 12:.0f} in is the practical minimum.",
                    door.room_a,
                )
            )

    for door in plan.exterior_doors:
        if door.room not in room_ids:
            add(
                Issue(
                    Severity.ERROR,
                    "DOOR_REF",
                    f"Exterior door references unknown room '{door.room}'.",
                    door.room,
                )
            )


def _validate_access(plan: Barndominium, add) -> None:
    """Every interior room must be reachable from an exterior door."""
    interior_rooms = {r.id for r in plan.rooms if r.type not in (RoomType.PORCH,)}
    if not interior_rooms:
        return

    adjacency: dict[str, set[str]] = {rid: set() for rid in interior_rooms}
    for d in plan.interior_doors:
        if d.room_a in adjacency and d.room_b in adjacency:
            adjacency[d.room_a].add(d.room_b)
            adjacency[d.room_b].add(d.room_a)

    entries = {d.room for d in plan.exterior_doors if d.room in interior_rooms}
    if not entries:
        add(
            Issue(
                Severity.ERROR,
                "NO_ENTRY",
                "Plan has no exterior door — no way to enter the building.",
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
        # Closets/pantries with no door are flagged but as warnings, not errors.
        sev = (
            Severity.WARNING
            if room and room.type in (RoomType.CLOSET, RoomType.PANTRY)
            else Severity.ERROR
        )
        add(
            Issue(
                sev,
                "NO_ACCESS",
                f"Room '{rid}' cannot be reached from any entrance "
                "(no connecting interior door path).",
                rid,
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
            )
        )

    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            has_window = bool(plan.windows_for(room.id))
            has_ext_door = bool(plan.exterior_doors_for(room.id))
            if not (has_window or has_ext_door):
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_EGRESS",
                        "Bedroom has no emergency escape opening "
                        "(needs an egress window or exterior door).",
                        room.id,
                    )
                )

        if room.type in HABITABLE_TYPES:
            glazing = sum(w.glazed_area for w in plan.windows_for(room.id))
            required = room.area * NATURAL_LIGHT_RATIO
            if glazing + 1e-6 < required:
                add(
                    Issue(
                        Severity.WARNING,
                        "NAT_LIGHT",
                        f"Glazing {glazing:.0f} sq ft is below the natural-light "
                        f"minimum of {required:.0f} sq ft "
                        f"({NATURAL_LIGHT_RATIO * 100:.0f}% of floor area).",
                        room.id,
                    )
                )
