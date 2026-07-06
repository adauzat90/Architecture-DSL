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
from collections import Counter, deque
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .constants import (
    COMFORT_HALLWAY_WIDTH,
    EPSILON,
    EXTERIOR_WALL_THICKNESS,
    GUARD_DROP_TRIGGER,
    GUARD_HEIGHT,
    INTERIOR_WALL_THICKNESS,
    MAX_RISER_HEIGHT,
    MIN_BEDROOM_AREA,
    MIN_BEDROOM_DIMENSION,
    MIN_CEILING,
    MIN_HALLWAY_WIDTH,
    MIN_SHADE_OVERHANG,
    MIN_TREAD_DEPTH,
    NATURAL_LIGHT_RATIO,
    PLUMBING_WALL_THICKNESS,
    SOLAR_SOUTH_MIN_GLAZING,
    SOLAR_SOUTH_MIN_WALL,
    SOLAR_SOUTH_SHADE_GLAZING,
    SOLAR_WEST_MAX_GLAZING,
    STAIR_HEADROOM,
    DRIVE_DOOR_REACH,
    WELL_SEPTIC_MIN_SEPARATION,
)
from .profiles import DEFAULT, Profile
from .elements import (
    DOUBLE_LEAF_KINDS,
    GARAGE_TYPES,
    HABITABLE_TYPES,
    INTERIOR_TYPES,
    Barndominium,
    Direction,
    Room,
    RoomType,
)
from .geometry import (
    opening_endpoints,
    point_in_footprint,
    rect_in_footprint,
    shared_edge,
    wall_faces_outside,
)
from .solar import compass_label, true_azimuth, wall_sector
from .energy import WWR_CEILING, describe_targets
from .spatial import room_index


class _WallOpening(Protocol):
    """Structural view shared by a :class:`Window` and an :class:`ExteriorDoor`:
    an opening positioned on a single wall. Lets a check iterate both kinds
    together without losing their common ``room``/``wall``/``offset``/``width``."""

    room: str
    wall: Direction
    offset: float
    width: float
    line: int | None
    col: int | None
    end_col: int | None

# The IRC-derived habitability / circulation / egress / stair / daylight
# thresholds these checks compare against now live in ``constants.py`` (the
# single source of truth) and are imported above. A jurisdiction ``Profile``
# (profiles.py) supplies the *enforced* value for each of them at check time —
# ``DEFAULT`` reproduces the constants exactly, so an unprofiled compile is
# byte-identical. Only these ~14 numbers are profile-driven; everything else
# below keeps its bare constant.
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
#: Stock total widths for a declared double/french pair — two equal leaves
#: (2×24 .. 2×36). A declared double is checked against these instead of the
#: single-leaf sizes.
STD_DOUBLE_DOOR_WIDTHS_IN = (48, 60, 64, 72)
DOOR_SIZE_TOL_IN = 0.5  # how far off a standard size before we nudge
# Overhead (sectional garage) door stock sizes, in **feet** — garage doors are
# ordered in feet, unlike leaf doors: singles 8/9/10 wide, doubles 12/16; panels
# 7 or 8 ft tall. An opening wider than OVERHEAD_HEADER_SPAN outruns a stock
# header and wants engineering with the frame.
STD_OVERHEAD_DOOR_WIDTHS_FT = (8, 9, 10, 12, 16)
STD_OVERHEAD_DOOR_HEIGHTS_FT = (7, 8)
OVERHEAD_HEADER_SPAN = 10.0
# --- accessibility / aging-in-place (opt-in; ANSI A117.1) --------------------
ACCESSIBLE_CLEAR_DOOR = 32 / 12  # 32 in clear opening (A117.1 §404)
ACCESSIBLE_LEAF_MIN = 34 / 12  # a ~34 in leaf yields the 32 in clear
ACCESSIBLE_EXTERIOR_MIN = 36 / 12  # 36 in door on the accessible entrance
ACCESSIBLE_TURN = 5.0  # 60 in wheelchair turning circle (A117.1 §304)
# NATURAL_LIGHT_RATIO and the stair constants below live in constants.py (the
# single source of truth) and are imported above; re-stated here in prose only.
_WINDOW_TYP_HEIGHT = 3.67  # head - sill for a typical window, ft
#: Habitable rooms subject to the R304 area/dimension minimums as a WARNING
#: (ROOM_HABITABLE). Bedrooms are excluded — they carry the same rule as a hard
#: ERROR (BEDROOM_AREA/BEDROOM_DIM) and must not double-fire. Kitchens are
#: excluded — R304.2 exempts them from both the area floor and the 7 ft dimension.
R304_HABITABLE_TYPES: frozenset[RoomType] = HABITABLE_TYPES - {
    RoomType.BEDROOM,
    RoomType.KITCHEN,
}
MAX_ROOM_ASPECT = 3.0  # a habitable room longer than this (long:short) is awkward
MIN_SOUND_BUFFER_WALL = 4.0  # a bedroom-bedroom shared wall this long wants a buffer
#: Minimum plan overlap (sq ft) between an upper-floor wet room and a wet room
#: below for their plumbing to share one straight vertical waste stack. A mere
#: corner touch (0) can't route a stack + wet wall; require a small real overlap.
MIN_STACK_OVERLAP = 4.0
#: Furniture-fit floors on the *clear* interior (ft) — the livability analogue of
#: the wet-room fixture checks, and one step beyond BEDROOM_DIM (which only sets a
#: 7 ft nominal side). A bedroom must hold a **queen** bed (5×6.67) against a wall
#: with a 24 in walk-around on one long side → a 7 (bed width + access) × 6.67 (bed
#: length) clear envelope, so a 7×10 room that clears the area/dimension checks but
#: is too narrow for a queen still gets flagged. A dining room must seat a 4-person
#: table (~3 ft) with 30 in of chair-pull/circulation all round → ~8 ft clear.
BED_FURNISH_SHORT = 6.67
BED_FURNISH_LONG = 7.0
DINING_FURNISH_CLEAR = 8.0
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
#: Exterior dimensions should land on this module (ft) for efficient material use.
BUILD_MODULE = 3.0
#: Two door swings overlapping by less than this (ft) are treated as just grazing.
SWING_CLASH_EPS = 0.02
#: Natural-ventilation floor (IRC R303.1): openable window area >= 4% of a
#: habitable room's floor — half the 8% daylight floor (NATURAL_LIGHT_RATIO).
NATURAL_VENT_RATIO = 0.04
#: A landing (R311.3) must reach at least this far (ft) out from an exterior
#: door's face — a porch shallower than this doesn't count as a landing.
LANDING_MIN_DEPTH = 3.0

# The emergency-escape opening minimums (IRC R310) are imported from constants;
# the modelled clear opening is width × (head − sill), generous for a single-hung
# sash but consistent with the project's "loosely IRC" stance. A jurisdiction
# Profile can amend the enforced values.


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
    #: Set by an ``# barndsl: accept <CODE>`` pragma (see :mod:`barndsl.pragma`).
    #: An accepted diagnostic has been DOWNGRADED to an INFO — its ``severity`` is
    #: already ``INFO`` — but the flag records that it was a deliberate,
    #: documented deviation so the score stops deducting for it and the audit
    #: trail survives. ``accept_reason`` carries the quoted justification (if any).
    accepted: bool = False
    accept_reason: str | None = None
    #: Cross-file composition (the ``use`` statement). A *part-internal* diagnostic
    #: — one that fires inside a used part file regardless of where it's placed —
    #: carries ``file`` (the resolved part path) and ``part`` (the relative path as
    #: written in the ``use`` line). Its ``line`` anchors to the ``use`` statement in
    #: the host buffer (the nearest thing there), while the message names the part's
    #: own ``file:line``. ``None`` on an ordinary host diagnostic. See
    #: :mod:`barndsl.compose`.
    file: str | None = None
    part: str | None = None

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


def exterior_walls(plan: Barndominium, room: Room, tol: float = EPSILON) -> list[Direction]:
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


def plumbing_wall_sides(plan: Barndominium, room: Room) -> set[Direction]:
    """The sides of ``room`` that carry a declared **plumbing wall** (a ``wall
    a - b plumbing`` naming this room whose pair really shares a wall).

    A declared plumbing wall is built as a 2x6 (:data:`PLUMBING_WALL_THICKNESS`),
    so the flanking rooms lose half of that — not half an ordinary partition —
    from their clear dimensions. The whole side is treated as the thicker wall
    even when the shared run covers only part of it (conservative and simple).
    """
    sides: set[Direction] = set()
    for ws in getattr(plan, "wall_specs", None) or []:
        if "plumbing" not in ws.attributes or room.id not in (ws.room_a, ws.room_b):
            continue
        other_id = ws.room_b if room.id == ws.room_a else ws.room_a
        if other_id == room.id:
            continue
        other = plan.room(other_id)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        if edge.orientation == "v":
            sides.add(
                Direction.WEST if abs(edge.pos - room.x) <= EPSILON else Direction.EAST
            )
        else:
            sides.add(
                Direction.SOUTH if abs(edge.pos - room.y) <= EPSILON else Direction.NORTH
            )
    return sides


def _wall_halves(plan: Barndominium, room: Room) -> dict[Direction, float]:
    """Half the bounding wall's thickness per side of ``room``: an exterior
    shell edge, a declared plumbing (2x6) wall, or an ordinary partition."""
    ext = set(exterior_walls(plan, room))
    plumbing = (
        plumbing_wall_sides(plan, room)
        if getattr(plan, "wall_specs", None)
        else set()
    )

    def half(side: Direction) -> float:
        if side in ext:
            thk = EXTERIOR_WALL_THICKNESS
        elif side in plumbing:
            thk = PLUMBING_WALL_THICKNESS
        else:
            thk = INTERIOR_WALL_THICKNESS
        return thk / 2.0

    return {side: half(side) for side in Direction}


def clear_dimensions(plan: Barndominium, room: Room) -> tuple[float, float]:
    """The room's built **clear** (finish-face) ``(width, length)`` in feet.

    barndsl rooms tile on wall *centrelines*, so the interior you can actually
    use is the nominal rectangle minus half of each bounding wall's thickness —
    an exterior (shell) edge costs more than an interior partition, and a
    declared plumbing wall (``wall a - b plumbing``) is a 2x6. This is the
    dimension IRC habitability minimums are measured to (finished surfaces) and
    the one Revit computes for its room schedule, so reporting/checking it keeps
    barndsl and the built model telling the same story.
    """
    halves = _wall_halves(plan, room)
    clear_w = room.width - halves[Direction.WEST] - halves[Direction.EAST]
    clear_l = room.length - halves[Direction.SOUTH] - halves[Direction.NORTH]
    return max(0.0, clear_w), max(0.0, clear_l)


def clear_box(plan: Barndominium, room: Room) -> tuple[float, float, float, float]:
    """The room's clear interior as a world-coordinate rectangle
    ``(x0, y0, width, length)`` — the finish-face box inside the wall centrelines.
    Its south-west corner is inset from the room rectangle by half the west/south
    wall. Used to place fixtures inside the usable floor."""
    halves = _wall_halves(plan, room)
    clear_w, clear_l = clear_dimensions(plan, room)
    return (
        room.x + halves[Direction.WEST],
        room.y + halves[Direction.SOUTH],
        clear_w,
        clear_l,
    )


def geometric_neighbors(plan: Barndominium, room_id: str) -> list[str]:
    """Ids of rooms that share a wall segment with ``room_id``."""
    rooms = plan.rooms
    index = room_index(plan)
    i = index.first_index(room_id)
    if i is None:
        return []
    r = rooms[i]
    # A room can only share a wall with one whose bounding box touches it, so ask
    # the spatial index for those few candidates (in room-list order) instead of
    # rescanning every room — same result, no O(n²) full sweep.
    return [
        rooms[j].id
        for j in index.candidates_near(r)
        if rooms[j].id != room_id and shared_edge(r, rooms[j])
    ]


def _f(value: float) -> str:
    """Format a measurement: drop a trailing .0 (so 28.0 -> '28')."""
    return f"{value:g}"


#: The largest plausible plan dimension, in feet. No barndominium envelope side,
#: room, or wing runs anywhere near 1000 ft (three football fields end to end); a
#: value beyond it is a typo or an overflow that would poison every area product
#: with ``inf``. Sides past this are rejected (DIM_IMPLAUSIBLE) and clamped so the
#: rest of the takeoff/summary stays finite.
MAX_PLAN_DIMENSION = 1000.0


def _dim_desc(value: float) -> str:
    """A dimension for a diagnostic message — never the literal ``inf``/``nan``
    (which would itself leak an ``inf`` into the output)."""
    return _f(value) if math.isfinite(value) else "non-finite"


def _implausible_dim(value: float) -> bool:
    return not math.isfinite(value) or value > MAX_PLAN_DIMENSION


def _check_dimensions(plan: Barndominium, add) -> None:
    """Reject non-finite or absurdly large envelope/room/wing sides.

    A finite-but-enormous side (``envelope 1e308 x 1e308``) sails past the
    positive-dimension checks yet overflows every area product to ``inf``,
    leaking ``inf sq ft`` into the takeoff. Flag it (DIM_IMPLAUSIBLE, error) and
    clamp the offending value to :data:`MAX_PLAN_DIMENSION` so the rest of the
    report stays finite and readable — the plan is already unbuildable, the clamp
    is cosmetic. Runs first in :func:`validate`, so every later check and the
    metrics takeoff see finite numbers."""
    clamp = MAX_PLAN_DIMENSION

    if _implausible_dim(plan.envelope_width) or _implausible_dim(plan.envelope_length):
        add(
            Issue(
                Severity.ERROR,
                "DIM_IMPLAUSIBLE",
                f"Envelope {_dim_desc(plan.envelope_width)} x "
                f"{_dim_desc(plan.envelope_length)} ft is implausible — each side "
                f"must be finite and <= {MAX_PLAN_DIMENSION:.0f} ft.",
                hint=f"Use a realistic footprint in feet (each side <= "
                f"{MAX_PLAN_DIMENSION:.0f}).",
            )
        )
        if _implausible_dim(plan.envelope_width):
            plan.envelope_width = clamp
        if _implausible_dim(plan.envelope_length):
            plan.envelope_length = clamp
    for room in plan.rooms:
        if _implausible_dim(room.width) or _implausible_dim(room.length):
            add(
                Issue(
                    Severity.ERROR,
                    "DIM_IMPLAUSIBLE",
                    f"Room size {_dim_desc(room.width)} x {_dim_desc(room.length)} ft "
                    f"is implausible — each side must be finite and <= "
                    f"{MAX_PLAN_DIMENSION:.0f} ft.",
                    room=room.id,
                    hint=f"Use a realistic room size in feet (each side <= "
                    f"{MAX_PLAN_DIMENSION:.0f}).",
                )
            )
            if _implausible_dim(room.width):
                room.width = clamp
            if _implausible_dim(room.length):
                room.length = clamp
    for i, w in enumerate(plan.wings, start=1):
        if _implausible_dim(w.width) or _implausible_dim(w.length):
            add(
                Issue(
                    Severity.ERROR,
                    "DIM_IMPLAUSIBLE",
                    f"Wing #{i} size {_dim_desc(w.width)} x {_dim_desc(w.length)} ft "
                    f"is implausible — each side must be finite and <= "
                    f"{MAX_PLAN_DIMENSION:.0f} ft.",
                    hint=f"Use a realistic wing size in feet (each side <= "
                    f"{MAX_PLAN_DIMENSION:.0f}).",
                )
            )
            if _implausible_dim(w.width):
                w.width = clamp
            if _implausible_dim(w.length):
                w.length = clamp


def _amended(profile: Profile, field: str) -> bool:
    """True when ``profile`` enforces a value other than the IRC baseline for
    ``field`` — used to keep a diagnostic's wording honest (and byte-identical
    under the default profile) about *what number was actually enforced*."""
    return getattr(profile, field) != getattr(DEFAULT, field)


def _profile_tag(profile: Profile, field: str, base_str: str) -> str:
    """A parenthetical naming the profile behind an amended threshold, e.g.
    " (the 'strict' profile amends the IRC base of 70)". Empty for the default
    profile, so default-profile messages are unchanged."""
    if not _amended(profile, field):
        return ""
    return f" (the '{profile.name}' profile amends the IRC base of {base_str})"


#: Public, shared living spaces — bedrooms ideally don't open straight onto these.
PUBLIC_TYPES = {RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING}
BATH_TYPES = {RoomType.BATHROOM, RoomType.HALF_BATH}
#: Dedicated-storage rooms, for the whole-house storage ratio (LOW_STORAGE) and
#: the `program storage <sqft>` minimum.
STORAGE_TYPES = {RoomType.CLOSET, RoomType.PANTRY}
#: Below this fraction of conditioned interior area in dedicated storage, a plan is
#: storage-poor. Conservative — set below the worked gallery's floor so a curated,
#: reasonably-storaged plan never trips it; it catches a home with almost no closets.
LOW_STORAGE_RATIO = 0.025
#: Rooms with plumbing fixtures — cheaper to build when clustered on a wet wall.
WET_TYPES = {
    RoomType.BATHROOM,
    RoomType.HALF_BATH,
    RoomType.KITCHEN,
    RoomType.LAUNDRY,
    RoomType.UTILITY,
}


#: Room types that read as clearly "public" / "private" for the zone-band checks
#: (ZONE_CROSS). Public = the shared living core; private = sleeping/bathing.
#: Every other type (hall, closet, office, laundry, mudroom, garage, …) is
#: neutral — it appears in both wings, so it neither triggers nor blocks a band.
ZONE_PUBLIC_TYPES = {RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING}
ZONE_PRIVATE_TYPES = {RoomType.BEDROOM, RoomType.BATHROOM, RoomType.HALF_BATH}


def _suite_members(plan: Barndominium) -> dict[str, tuple[str, ...]]:
    """``suite id -> member room ids`` (declaration order preserved)."""
    return {s.id: s.members for s in (getattr(plan, "suites", None) or [])}


def _same_suite(plan: Barndominium, a_id: str, b_id: str) -> bool:
    """True if two rooms are declared members of one common suite."""
    for s in getattr(plan, "suites", None) or []:
        if a_id in s.members and b_id in s.members:
            return True
    return False


def _suites_of(plan: Barndominium, room_id: str) -> list[str]:
    """The ids of every declared suite that lists ``room_id`` as a member."""
    return [s.id for s in (getattr(plan, "suites", None) or []) if room_id in s.members]


def _sole_bedroom_suite(
    plan: Barndominium, room_id: str, by_id: dict[str, Room]
) -> bool:
    """True if ``room_id`` is in a declared suite where it is the ONLY bedroom.

    That is the primary-suite shape (bed + bath + closet). A suite holding
    several bedrooms is a shared grouping, not a primary suite — a declaration
    must not silence checks whose concern is exactly the shared case.
    """
    for s in getattr(plan, "suites", None) or []:
        if room_id not in s.members:
            continue
        if not any(
            m != room_id
            and (r := by_id.get(m)) is not None
            and r.type is RoomType.BEDROOM
            for m in s.members
        ):
            return True
    return False


def _suite_pair_intentional(
    plan: Barndominium, a_id: str, b_id: str, by_id: dict[str, Room]
) -> bool:
    """True when two bedrooms share a suite whose bedrooms are EXACTLY this pair.

    A bunk pairing or a nursery off the master is a deliberate two-bed suite;
    dumping every bedroom into one giant "suite" is not — the size guard stops
    a single declaration from silencing a check plan-wide.
    """
    for s in getattr(plan, "suites", None) or []:
        if a_id not in s.members or b_id not in s.members:
            continue
        n_beds = sum(
            1
            for m in s.members
            if (r := by_id.get(m)) is not None and r.type is RoomType.BEDROOM
        )
        if n_beds <= 2:
            return True
    return False


def _suite_ensuite(
    plan: Barndominium,
    bed_id: str,
    by_id: dict[str, Room],
    graph: dict[str, set[str]],
) -> bool:
    """True if a full bath in ``bed_id``'s suite is reachable from it by doors
    that stay inside the suite (bed → bath, or bed → wic → bath).

    Declared membership names the grouping, but an ensuite is a spatial fact —
    a bath at the other end of the plan doesn't become private by declaration.
    """
    for s in getattr(plan, "suites", None) or []:
        if bed_id not in s.members:
            continue
        members = set(s.members)
        seen = {bed_id}
        stack = [bed_id]
        while stack:
            cur = stack.pop()
            for n in sorted(graph.get(cur, ())):
                if n not in members or n in seen:
                    continue
                r = by_id.get(n)
                if r is not None and r.type is RoomType.BATHROOM:
                    return True
                seen.add(n)
                stack.append(n)
    return False


def _zone_room_sets(plan: Barndominium, by_id: dict[str, Room]) -> dict[str, set[str]]:
    """``zone id -> the set of room ids it covers``, expanding suite members.

    A zone member is a room id (kept if it names a real room) or a suite id
    (expanded to that suite's rooms). Unknown members are dropped here — they're
    reported separately as ``ZONE_REF``.
    """
    suites = _suite_members(plan)
    out: dict[str, set[str]] = {}
    for z in getattr(plan, "zones", None) or []:
        rooms: set[str] = set()
        for m in z.members:
            if m in by_id:
                rooms.add(m)
            elif m in suites:
                rooms.update(r for r in suites[m] if r in by_id)
        out[z.id] = rooms
    return out


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


def _runs_off_wall(offset: float, width: float, wall_length: float) -> bool:
    """True if an opening at ``offset`` of ``width`` spills past either end of a
    ``wall_length`` run (within :data:`EPSILON`). Shared by the door, window and
    exterior-door bounds checks so the tolerance handling lives in one place."""
    return offset < -EPSILON or offset + width > wall_length + EPSILON


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
    """Does a stair sit along a wall (footprint edge or a room partition) rather
    than floating free in the middle of a room?"""
    tol = EPSILON
    if plan.wings:
        # L/T/U footprint: every outline edge is an exterior wall, not just the
        # four primary-envelope edges. Probe just outside each stair edge's
        # midpoint — leaving the footprint union means that edge lies on the
        # outline (mirrors ``wall_faces_outside`` for rooms).
        secs = plan.footprint_sections()
        eps = 0.05
        mx, my = (s.x + s.x2) / 2.0, (s.y + s.y2) / 2.0
        probes = (
            (mx, s.y - eps),
            (mx, s.y2 + eps),
            (s.x - eps, my),
            (s.x2 + eps, my),
        )
        if any(not point_in_footprint(secs, px, py) for px, py in probes):
            return True
    elif (
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


def _off_module(value: float, module: float = BUILD_MODULE, tol: float = EPSILON) -> bool:
    """Is ``value`` not a whole multiple of ``module``?"""
    return abs(value - round(value / module) * module) > tol


def _side_of(room: Room, edge) -> float:
    """+1/-1 for the side of ``edge`` the room's center lies on (its own side)."""
    cx, cy = room.center
    return 1.0 if (cx if edge.orientation == "v" else cy) > edge.pos else -1.0


def _swing_sgn(door, a: Room, b: Room, edge) -> float | None:
    """Mirror the renderer: +1/-1 for the side the leaf swings into, or None to
    fall back to the keep-inside-the-envelope heuristic."""
    into = door.swing_into
    room = a if (into and into == a.id) else (b if (into and into == b.id) else None)
    return None if room is None else _side_of(room, edge)


def _swing_region(
    plan: Barndominium, orientation: str, ox: float, oy: float, w: float,
    hinge_far: bool, sgn: float | None, samples: int = 4,
) -> list[tuple[float, float]]:
    """The quarter-disc the leaf sweeps, as a small convex polygon (pie slice),
    in plan coordinates — matching :meth:`SVGRenderer._door_symbol`."""
    if orientation == "v":
        if sgn is None:
            sgn = 1.0 if (ox + w) <= plan.envelope_width else -1.0
        hinge = (ox, oy + w) if hinge_far else (ox, oy)
        latch = (ox, oy) if hinge_far else (ox, oy + w)
        tip = (ox + sgn * w, hinge[1])
    else:
        if sgn is None:
            sgn = 1.0 if (oy + w) <= plan.envelope_length else -1.0
        hinge = (ox + w, oy) if hinge_far else (ox, oy)
        latch = (ox, oy) if hinge_far else (ox + w, oy)
        tip = (hinge[0], oy + sgn * w)
    a0 = math.atan2(latch[1] - hinge[1], latch[0] - hinge[0])
    a1 = math.atan2(tip[1] - hinge[1], tip[0] - hinge[0])
    d = a1 - a0
    while d <= -math.pi:
        d += 2 * math.pi
    while d > math.pi:
        d -= 2 * math.pi
    pts = [hinge]
    for i in range(samples + 1):
        ang = a0 + d * i / samples
        pts.append((hinge[0] + w * math.cos(ang), hinge[1] + w * math.sin(ang)))
    return pts


def _interior_swing_region(plan: Barndominium, door, a: Room, b: Room, edge):
    """The swept region of an interior door's leaf, or None if nothing swings.

    Pocket/sliding/cased doors have no leaf. A double/french pair sweeps two
    half-width leaves; like the exterior check, approximate it with the hinge
    end's quarter-disc (half the total width) so a wide pair can't silently
    clash-exempt itself while the renderer draws two swinging leaves.
    """
    if door.kind not in ("swing", *DOUBLE_LEAF_KINDS):
        return None
    leaf = door.width / 2.0 if door.kind in DOUBLE_LEAF_KINDS else door.width
    start = edge.lo + door.offset if door.offset is not None else edge.mid - door.width / 2.0
    hinge_far = door.hinge == "far"
    if hinge_far and door.kind in DOUBLE_LEAF_KINDS:
        # The far leaf hinges at the opening's far end; its sweep starts at
        # the pair's midpoint, so shift the region to the outer half.
        start += door.width - leaf
    sgn = _swing_sgn(door, a, b, edge)
    if edge.orientation == "v":
        return _swing_region(plan, "v", edge.pos, start, leaf, hinge_far, sgn)
    return _swing_region(plan, "h", start, edge.pos, leaf, hinge_far, sgn)


def _exterior_swing_region(plan: Barndominium, room: Room, door):
    """The swept region of an exterior door (renderer hinges near, keeps inside).

    A double/french pair sweeps two half-width leaves; approximate it with the
    near leaf's quarter-disc (half the total width, hinged near)."""
    w = (
        door.width / 2.0
        if getattr(door, "kind", "entry") in DOUBLE_LEAF_KINDS
        else door.width
    )
    x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
    if door.wall in (Direction.NORTH, Direction.SOUTH):
        return _swing_region(plan, "h", min(x1, x2), y1, w, False, None)
    return _swing_region(plan, "v", x1, min(y1, y2), w, False, None)


def _convex_overlap(poly_a, poly_b, eps: float = SWING_CLASH_EPS) -> bool:
    """Do two convex polygons overlap by more than ``eps`` (separating-axis test)?

    Axes are the unit edge normals of both polygons; if any axis separates the
    projections (with an ``eps`` gap, in feet), they don't overlap."""
    for poly in (poly_a, poly_b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)
            length = math.hypot(nx, ny)
            if length < 1e-12:
                continue
            nx, ny = nx / length, ny / length
            a_proj = [nx * px + ny * py for px, py in poly_a]
            b_proj = [nx * px + ny * py for px, py in poly_b]
            if max(a_proj) < min(b_proj) + eps or max(b_proj) < min(a_proj) + eps:
                return False
    return True


def _region_room(region, a: Room, b: Room, edge) -> Room | None:
    """Which of ``a``/``b`` a swept ``region`` lands in, by the side its centroid
    falls on — robust for both a declared swing and the geometric default."""
    axis = 0 if edge.orientation == "v" else 1
    centroid = sum(p[axis] for p in region) / len(region)
    a_positive = a.center[axis] > edge.pos
    return a if (centroid > edge.pos) == a_positive else b


def _preferred_swing_room(a: Room, b: Room) -> Room | None:
    """The room a privacy-sensitive door should open into: a lone wet room over
    anything, else a lone bedroom — or None when it's ambiguous (two rooms of the
    same private kind) or neither room is private."""
    wet = [r for r in (a, b) if r.type in (RoomType.BATHROOM, RoomType.HALF_BATH)]
    if len(wet) == 1:
        return wet[0]
    beds = [r for r in (a, b) if r.type is RoomType.BEDROOM]
    return beds[0] if len(beds) == 1 else None


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


def _check_wings(plan: Barndominium, add, tol: float = EPSILON) -> None:
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


def validate(plan: Barndominium, profile: Profile | None = None) -> ValidationReport:
    """Run all checks and return a :class:`ValidationReport`.

    ``profile`` supplies the jurisdiction-variable code thresholds (see
    :mod:`barndsl.profiles`); ``None`` uses :data:`~barndsl.profiles.DEFAULT`,
    the IRC baseline, which is byte-identical to the pre-profile behaviour.
    """
    if profile is None:
        profile = DEFAULT
    issues: list[Issue] = []
    add = issues.append

    # Reject + clamp non-finite/absurd dimensions FIRST, so no later check or the
    # metrics takeoff ever sees an `inf`-poisoned value (see _check_dimensions).
    _check_dimensions(plan, add)

    if plan.envelope_width <= 0 or plan.envelope_length <= 0:
        add(
            Issue(
                Severity.ERROR,
                "ENVELOPE",
                "Envelope must have positive dimensions.",
                hint="Declare the footprint: `envelope 60 x 40`.",
            )
        )
    _check_wings(plan, add)
    min_ceiling = profile.min_ceiling_height
    ceil_tag = _profile_tag(profile, "min_ceiling_height", f"{MIN_CEILING:.0f} ft")
    if plan.ceiling_height < min_ceiling:
        add(
            Issue(
                Severity.ERROR,
                "CEILING",
                f"Ceiling height {_f(plan.ceiling_height)} ft is below the "
                f"{min_ceiling:g} ft minimum for habitable space{ceil_tag}.",
                hint=f"Set `ceiling {min_ceiling:g}` or greater (9–12 is typical).",
            )
        )
    for room in plan.rooms:
        rc = getattr(room, "ceiling_height", None)
        if rc is not None and not getattr(room, "vaulted", False) and rc < min_ceiling:
            add(
                Issue(
                    Severity.ERROR,
                    "CEILING",
                    f"Room '{room.id}' sets a {_f(rc)} ft ceiling, below the "
                    f"{min_ceiling:g} ft minimum for habitable space{ceil_tag}.",
                    room=room.id,
                    hint=f"Raise its `ceiling` to >= {min_ceiling:g}, or drop the "
                    "override to inherit the plan ceiling.",
                )
            )

    _validate_site(plan, add)
    _validate_site_features(plan, add, profile)
    _validate_porch_guards(plan, add)

    if not plan.rooms:
        add(
            Issue(
                Severity.ERROR,
                "EMPTY",
                "Plan has no rooms.",
                hint="Add rooms: `room living: living at 0,0 size 20 x 16`.",
            )
        )
        return ValidationReport(issues)

    # Counter is O(n); the old ``ids.count(i)`` per id was O(n²) on big plans.
    id_counts = Counter(r.id for r in plan.rooms)
    for d in sorted({i for i, c in id_counts.items() if c > 1}):
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
    _validate_room_programs(plan, add, profile)
    _validate_fixtures(plan, add)
    _validate_furniture(plan, add)
    _validate_storage(plan, add)
    _validate_doors(plan, add)
    _validate_openings(plan, add)
    _validate_safety_glazing(plan, add)
    _validate_window_fall(plan, add)
    _validate_stairs(plan, add, profile)
    _validate_landings(plan, add)
    _validate_door_threshold(plan, add)
    _validate_water_heater(plan, add)
    _validate_guards(plan, add)
    _validate_life_safety(plan, add)
    _validate_load_path(plan, add)
    _validate_plumbing_stack(plan, add)
    _validate_electrical_plan(plan, add)
    _validate_electrical(plan, add)
    _validate_solar(plan, add)
    _validate_approach(plan, add)
    _validate_energy(plan, add)
    _validate_access(plan, add)
    _validate_egress_and_light(plan, add, profile)
    _validate_design_quality(plan, add, profile)
    _validate_accessibility(plan, add)
    _validate_program(plan, add)
    _validate_requirements(plan, add)
    _validate_walls(plan, add)
    _validate_suites_zones(plan, add)
    _validate_structure(plan, add)
    _validate_finishes(plan, add)
    _validate_notes(plan, add)
    # Local import: fixtures.py imports clear_box from this module.
    from .fixtures import validate_fixtures

    validate_fixtures(plan, add)

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


def _validate_notes(plan: Barndominium, add) -> None:
    """Gently flag a positioned ``note`` anchored outside the footprint (INFO).

    Architects legitimately annotate the site, a setback, or a future addition
    outside the walls, so this never blocks — it just points out a callout that
    may have meant to land on the plan (see NOTE_OUTSIDE)."""
    marks = getattr(plan, "note_marks", None)
    if not marks:
        return
    secs = plan.footprint_sections()
    for nm in marks:
        if point_in_footprint(secs, nm.x, nm.y):
            continue
        snippet = nm.text if len(nm.text) <= 40 else nm.text[:37] + "…"
        add(
            Issue(
                Severity.INFO,
                "NOTE_OUTSIDE",
                f"Note “{snippet}” is anchored outside the footprint at "
                f"{_f(nm.x)},{_f(nm.y)}.",
                line=nm.line,
                col=nm.col,
                end_col=nm.end_col,
                hint="If the callout belongs on the plan, move its `at` point inside "
                "the walls; annotating the site on purpose is fine.",
            )
        )


def _validate_finishes(plan: Barndominium, add) -> None:
    """Teach on an unrecognised per-room ``floor`` finish (never blocking).

    A floor hint is free text fuzzy-matched to the material palette; a name that
    matches nothing still builds (it silently inherits the room's default finish),
    so a friendly WARNING points the author at the vocabulary rather than letting
    the typo pass unseen."""
    from .materials import known_floor_names, match_floor

    for room in plan.rooms:
        hint = getattr(room, "floor", None)
        if hint and match_floor(hint) is None:
            add(
                Issue(
                    Severity.WARNING,
                    "FLOOR_FINISH",
                    f"Room '{room.id}' floor finish {hint!r} matched no known "
                    "material; it will use the default for its room type.",
                    room=room.id,
                    hint="Try one of: " + ", ".join(known_floor_names()) + ".",
                )
            )


def _site_footprint_bounds(plan: Barndominium) -> tuple[float, float, float, float]:
    """Bounding box ``(min_x, min_y, max_x, max_y)`` of everything with a physical
    footprint on the lot — the building (envelope + wings) plus every porch. This
    is what the setback check must clear, so a projecting porch counts against the
    yard the same way the county measures it."""
    minx, miny, maxx, maxy = plan.bounds()
    for p in plan.porches:
        minx = min(minx, p.x)
        miny = min(miny, p.y)
        maxx = max(maxx, p.x + p.width)
        maxy = max(maxy, p.y + p.length)
    return minx, miny, maxx, maxy


def _validate_site(plan: Barndominium, add) -> None:
    """Check a declared ``site`` / ``setback`` against the building footprint.

    The buildable rectangle is the lot minus its setbacks: ``front``/``rear``
    consume the plan's north-south depth and ``side`` clears both the east and
    west edges. barndsl has no lot-position statement, so the check is by
    **dimensions only** — the footprint's bounding box (building + porches) must
    fit inside the buildable width and length; where it sits on the lot isn't
    modelled (front is measured along the plan's south/entry edge). A footprint
    that overruns is a ``SETBACK`` error (a legal/county violation, like the other
    code checks); a ``setback`` with no ``site`` to measure against is a
    ``SETBACK_NO_SITE`` error.
    """
    ss = getattr(plan, "site_spec", None)
    if ss is None:
        return
    if not ss.has_dims:
        if ss.has_setback:
            loc = {
                "line": ss.setback_line, "col": ss.setback_col,
                "end_col": ss.setback_end_col,
            }
            add(
                Issue(
                    Severity.ERROR,
                    "SETBACK_NO_SITE",
                    "A `setback` was declared but there is no `site` to measure it "
                    "against.",
                    hint="Add the lot dimensions with `site <W> x <L>` (feet), or "
                    "drop the `setback`.",
                    **loc,
                )
            )
        if ss.has_features:
            # A drive/walk/well/septic/service places itself in lot feet, so it
            # needs the lot dimensions to sit on. One error per orphaned feature.
            for feat, what in _orphan_site_features(ss):
                add(
                    Issue(
                        Severity.ERROR,
                        "SITE_REQUIRED",
                        f"A `{what}` was declared but there is no `site` to place it on.",
                        line=feat.line, col=feat.col, end_col=feat.end_col,
                        hint="Add the lot dimensions with `site <W> x <L>` (feet) so "
                        "the feature has a lot to sit on.",
                    )
                )
        return
    # A degenerate lot is an authoring error whether or not setbacks follow —
    # mirror the ENVELOPE check's stance on non-positive/non-finite dims.
    bad_dims = not (
        math.isfinite(ss.width) and math.isfinite(ss.length)
        and ss.width > EPSILON and ss.length > EPSILON
    )
    bad_setbacks = any(
        v is not None and (not math.isfinite(v) or v < 0.0)
        for v in (ss.front, ss.side, ss.rear)
    )
    if bad_dims or bad_setbacks:
        what = []
        if bad_dims:
            what.append(f"lot dimensions {_f(ss.width)} x {_f(ss.length)} ft")
        if bad_setbacks:
            what.append("negative setback value(s)")
        add(
            Issue(
                Severity.ERROR,
                "SITE",
                "Invalid site declaration: " + " and ".join(what) + ".",
                line=ss.line, col=ss.col, end_col=ss.end_col,
                hint="`site <W> x <L>` needs positive lot dimensions and "
                "`setback` values can't be negative.",
            )
        )
        return
    if ss.has_building:
        # The building is pinned on the lot, so measure each side's real yard and
        # name the violated side + encroachment (ft-in) — stricter and clearer than
        # the dimension-only fit below.
        _validate_site_placed(plan, ss, add)
        return

    if not ss.has_setback:
        return  # a `site` on its own imposes no check

    front = ss.front or 0.0
    side = ss.side or 0.0
    rear = ss.rear or 0.0
    buildable_w = ss.width - 2.0 * side
    buildable_l = ss.length - front - rear
    minx, miny, maxx, maxy = _site_footprint_bounds(plan)
    fp_w = maxx - minx
    fp_l = maxy - miny

    problems: list[str] = []
    if buildable_w <= EPSILON or fp_w > buildable_w + EPSILON:
        problems.append(
            f"it is {_f(fp_w)} ft wide but only {_f(max(0.0, buildable_w))} ft is "
            f"buildable east-west ({_f(ss.width)} ft lot − 2 × {_f(side)} ft side)"
        )
    if buildable_l <= EPSILON or fp_l > buildable_l + EPSILON:
        problems.append(
            f"it is {_f(fp_l)} ft deep but only {_f(max(0.0, buildable_l))} ft is "
            f"buildable north-south ({_f(ss.length)} ft lot − {_f(front)} ft front "
            f"− {_f(rear)} ft rear)"
        )
    if problems:
        loc = {
            "line": ss.setback_line or ss.line,
            "col": ss.setback_col or ss.col,
            "end_col": ss.setback_end_col or ss.end_col,
        }
        add(
            Issue(
                Severity.ERROR,
                "SETBACK",
                "The building footprint doesn't fit the buildable area: "
                + "; ".join(problems)
                + ".",
                hint="Shrink the footprint, enlarge the `site`, or reduce the "
                "`setback` — the footprint's bounding box (building + porches) "
                "must fit inside the lot minus its setbacks.",
                **loc,
            )
        )


def _validate_site_placed(plan: Barndominium, ss, add) -> None:
    """Setback check for a building pinned on the lot (``building at <x>,<y>``).

    Measures the real clear yard on each lot edge — building (envelope + wings +
    porches) to lot line — and reports each edge whose yard is short of its
    required ``setback`` (or where the building crosses the lot line entirely),
    naming the side and the encroachment in ft-in. A ``SETBACK`` error, like the
    dimension-only check it supersedes."""
    from .render import fmt_ft_in

    minx, miny, maxx, maxy = _site_footprint_bounds(plan)
    bx = ss.building_x or 0.0
    by = ss.building_y or 0.0
    lot_w = ss.width
    lot_l = ss.length
    front = ss.front or 0.0
    rear = ss.rear or 0.0
    side = ss.side or 0.0
    # (label, clear yard on that edge, required setback there). front = south by
    # convention; `side` applies to both the east and west yards.
    checks = [
        ("front (south)", by + miny, front),
        ("rear (north)", lot_l - (by + maxy), rear),
        ("west side", bx + minx, side),
        ("east side", lot_w - (bx + maxx), side),
    ]
    problems: list[str] = []
    for label, clear, req in checks:
        if clear < req - EPSILON:
            over = req - clear
            if req > EPSILON:
                problems.append(
                    f"the {label} yard is {fmt_ft_in(max(0.0, clear))} but the "
                    f"setback needs {_f(req)} ft — {fmt_ft_in(over)} short"
                )
            else:
                problems.append(
                    f"the building crosses the {label} lot line by {fmt_ft_in(over)}"
                )
    if problems:
        add(
            Issue(
                Severity.ERROR,
                "SETBACK",
                "The building footprint violates the setbacks: "
                + "; ".join(problems) + ".",
                line=ss.setback_line or ss.building_line or ss.line,
                col=ss.setback_col or ss.building_col or ss.col,
                end_col=ss.setback_end_col or ss.building_end_col or ss.end_col,
                hint="Move the building (`building at <x>,<y>`), shrink the "
                "footprint, enlarge the `site`, or reduce the `setback`.",
            )
        )


def _orphan_site_features(ss):
    """``(feature, keyword)`` pairs for every site feature on a ``SiteSpec`` with
    no lot dimensions — the SITE_REQUIRED offenders, in source order."""
    out = []
    for d in ss.drives:
        out.append((d, "drive"))
    for w in ss.walks:
        out.append((w, "walk"))
    for w in ss.wells:
        out.append((w, "well"))
    for s in ss.septics:
        out.append((s, "septic"))
    for s in ss.services:
        out.append((s, "service"))
    out.sort(key=lambda t: (t[0].line or 0, t[0].col or 0))
    return out


def _pt_rect_dist(px: float, py: float, rect: tuple[float, float, float, float]) -> float:
    """Distance from point ``(px, py)`` to axis-aligned rect ``(x1, y1, x2, y2)``
    (0 if the point is inside)."""
    x1, y1, x2, y2 = rect
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return math.hypot(dx, dy)


def _door_lot_point(room, door, bx: float, by: float) -> tuple[float, float]:
    """The midpoint of an exterior door, in lot feet (building origin at bx,by)."""
    mid = door.offset + door.width / 2.0
    if door.wall is Direction.SOUTH:
        mx, my = room.x + mid, room.y
    elif door.wall is Direction.NORTH:
        mx, my = room.x + mid, room.y2
    elif door.wall is Direction.WEST:
        mx, my = room.x, room.y + mid
    else:  # EAST
        mx, my = room.x2, room.y + mid
    return (bx + mx, by + my)


def _exterior_door_points(plan: Barndominium, bx: float, by: float):
    """``(room_id, (lot_x, lot_y))`` for every people (non-overhead) exterior door."""
    rooms = {r.id: r for r in plan.rooms}
    out = []
    for d in plan.exterior_doors:
        if getattr(d, "overhead", False):
            continue
        room = rooms.get(d.room)
        if room is not None:
            out.append((d.room, _door_lot_point(room, d, bx, by)))
    return out


def _validate_site_features(plan: Barndominium, add, profile) -> None:
    """The site-plan v2 checks: well/septic separation, drive-to-door access, and
    septic-in-setback. All silent unless the plan declares the relevant features,
    so a plan with no site features gets zero of these diagnostics."""
    ss = getattr(plan, "site_spec", None)
    if ss is None or not ss.has_dims or not ss.has_features:
        return
    origin = plan.building_origin_on_lot()
    if origin is None:
        return
    bx, by = origin
    room_ids = {r.id for r in plan.rooms}

    # A walk must name a real room (a dangling reference is an authoring error).
    for wk in ss.walks:
        if wk.room not in room_ids:
            add(
                Issue(
                    Severity.ERROR,
                    "SITE_REF",
                    f"`walk` names unknown room '{wk.room}'.",
                    line=wk.line, col=wk.col, end_col=wk.end_col,
                    hint="Name a room that exists and has an exterior door, e.g. "
                    "`walk from mud to drive`.",
                )
            )

    # WELL_SEPTIC_CLEAR — the common 100 ft well-to-septic health rule.
    sep = WELL_SEPTIC_MIN_SEPARATION
    for wl in ss.wells:
        for sp in ss.septics:
            dist = min(_pt_rect_dist(wl.x, wl.y, rect) for rect in sp.rects())
            if dist < sep - EPSILON:
                add(
                    Issue(
                        Severity.WARNING,
                        "WELL_SEPTIC_CLEAR",
                        f"The well is {_f(dist)} ft from the septic "
                        f"{'field/tank' if sp.has_field else 'tank'} — the common "
                        f"health-department rule wants at least {_f(sep)} ft between a "
                        "private well and a septic system.",
                        line=wl.line, col=wl.col, end_col=wl.end_col,
                        hint=f"Move the well or septic so they are >= {_f(sep)} ft "
                        "apart (confirm the exact separation with your county health "
                        "department — it varies).",
                    )
                )

    # DRIVE_DOOR — a drive but no path (walk, or a near-enough drive edge) to a door.
    if ss.drives:
        door_pts = _exterior_door_points(plan, bx, by)
        drive_rects = [(d.x, d.y, d.x2, d.y2) for d in ss.drives]
        served = bool(ss.walks) or any(
            min(_pt_rect_dist(px, py, r) for r in drive_rects) <= DRIVE_DOOR_REACH + EPSILON
            for _, (px, py) in door_pts
        )
        if door_pts and not served:
            add(
                Issue(
                    Severity.INFO,
                    "DRIVE_DOOR",
                    "The plan has a drive but no walk or drive edge reaches an "
                    "exterior door — guests arrive at the drive and have no path to a "
                    "door.",
                    hint="Add `walk from <room> to drive` from an entry room, or "
                    "extend the drive to within a few feet of a door.",
                )
            )

    # SITE_OVERLAP — two declared drives that overlap on the lot. The cost takeoff
    # sums each drive's area independently, so an overlap double-counts the shared
    # paving; the warning is the honest fix (we don't silently subtract it). Only
    # drive-drive is checked: a walk is auto-routed to meet a drive (overlap by
    # design) and two thin auto-routed walks aren't a meaningful double-count.
    for i, d1 in enumerate(ss.drives):
        for d2 in ss.drives[i + 1:]:
            ox = min(d1.x2, d2.x2) - max(d1.x, d2.x)
            oy = min(d1.y2, d2.y2) - max(d1.y, d2.y)
            if ox > EPSILON and oy > EPSILON:
                add(
                    Issue(
                        Severity.WARNING,
                        "SITE_OVERLAP",
                        f"Two driveways overlap by {_f(ox * oy)} sqft "
                        f"({d1.surface} & {d2.surface}); the cost estimate "
                        "double-counts the shared paving.",
                        line=d2.line, col=d2.col, end_col=d2.end_col,
                        hint="Merge the drives into one rectangle, or move them "
                        "apart so each patch of paving is declared once.",
                    )
                )

    # SEPTIC_SETBACK — a septic tank/field inside a required setback band.
    lot_w, lot_l = ss.width, ss.length
    front, rear, side = ss.front or 0.0, ss.rear or 0.0, ss.side or 0.0
    bands = []  # (label, x1, y1, x2, y2)
    if front > 0:
        bands.append(("front", 0.0, 0.0, lot_w, front))
    if rear > 0:
        bands.append(("rear", 0.0, lot_l - rear, lot_w, lot_l))
    if side > 0:
        bands.append(("west side", 0.0, 0.0, side, lot_l))
        bands.append(("east side", lot_w - side, 0.0, lot_w, lot_l))
    for sp in ss.septics:
        hit = None
        for rx1, ry1, rx2, ry2 in sp.rects():
            for label, bx1, by1, bx2, by2 in bands:
                if (min(rx2, bx2) - max(rx1, bx1) > EPSILON
                        and min(ry2, by2) - max(ry1, by1) > EPSILON):
                    hit = label
                    break
            if hit:
                break
        if hit:
            add(
                Issue(
                    Severity.INFO,
                    "SEPTIC_SETBACK",
                    f"The septic system sits inside the {hit} setback band — septic "
                    "tanks and drain fields are usually held out of the required "
                    "yards too.",
                    line=sp.line, col=sp.col, end_col=sp.end_col,
                    hint="Move the septic clear of the setback, or confirm the "
                    "allowed septic setback with your county health department.",
                )
            )


def _validate_porch_guards(plan: Barndominium, add) -> None:
    """IRC R312.1 — when the finish floor sits more than 30 in above finished
    grade, every porch is a walking surface that needs a guard. Fires once per
    porch; silent when no ``grade`` is declared or grade <= 30 in (so an at-grade
    or shallow-grade plan is never nagged). This resolves the Phase 13 skip:
    R312.1 exterior guards were unexpressible without a grade elevation."""
    grade = getattr(plan, "grade", None)
    if grade is None or grade <= GUARD_DROP_TRIGGER + EPSILON:
        return
    for p in plan.porches:
        add(
            Issue(
                Severity.WARNING,
                "PORCH_GUARD",
                f"Porch '{p.display_name}' sits {_f(grade)} ft above grade "
                f"(> {GUARD_DROP_TRIGGER * 12:.0f} in), so it needs a "
                f"{GUARD_HEIGHT * 12:.0f} in guard (IRC R312.1).",
                hint=f"Add a {GUARD_HEIGHT * 12:.0f} in guard along the porch's open "
                "edges (balusters blocking a 4 in sphere) and note it on the drawings.",
            )
        )


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
        elif room.x < -EPSILON or room.y < -EPSILON or over_x > EPSILON or over_y > EPSILON:
            # Phrase the fix differently for a relatively-placed room, which has
            # no x,y token to "set" — point at align/offset/size instead.
            relative = room.placement is not None
            fixes = []
            if not relative and room.x < 0:
                fixes.append("set its x to >= 0")
            if not relative and room.y < 0:
                fixes.append("set its y to >= 0")
            if over_x > EPSILON:
                move_x = plan.envelope_width - room.width
                fixes.append(
                    f"reduce its width by {_f(over_x)} ft"
                    + ("" if relative or move_x < 0 else f" or move it west to x={_f(move_x)}")
                )
            if over_y > EPSILON:
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

    # Only rooms whose bounding boxes intersect can overlap, so walk the index's
    # candidate pairs (in the same ascending (i, j) order the old nested loop
    # visited) rather than all n²/2 pairs.
    rooms = plan.rooms
    index = room_index(plan)
    for i, j in index.candidate_pairs():
        a, b = rooms[i], rooms[j]
        if a.level != b.level:
            continue  # different floors may share a footprint (e.g. a loft)
        ov = a.overlaps(b)
        if ov > 0.5:  # ignore hairline floating-point overlaps
            # Prefer a fix that keeps 'b' inside the envelope.
            east_fits = a.x2 + b.width <= plan.envelope_width + EPSILON
            north_fits = a.y2 + b.length <= plan.envelope_length + EPSILON
            ox = min(a.x2, b.x2) - max(a.x, b.x)
            oy = min(a.y2, b.y2) - max(a.y, b.y)
            prefer_east = (ox <= oy and east_fits) or (not north_fits and east_fits)
            if prefer_east:
                sug = f"move '{b.id}' to x={_f(a.x2)} (east of '{a.id}')"
            elif north_fits:
                sug = f"move '{b.id}' to y={_f(a.y2)} (north of '{a.id}')"
            else:
                sug = "shrink one of them or enlarge the envelope"
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


def _validate_accessibility(plan: Barndominium, add) -> None:
    """Opt-in accessibility / aging-in-place nudges (ANSI A117.1-flavoured).

    Only runs when the plan declares an ``accessible`` target (the ``accessible``
    directive / :meth:`Barndominium.mark_accessible`), so ordinary plans aren't
    held to an accessible standard. All INFO — guidance, never blocking. Covers a
    no-step entry, accessible door clear widths, a wheelchair turning space in a
    ground-floor bath, and single-floor living.
    """
    if not plan.accessible:
        return
    by_id = {r.id: r for r in plan.rooms}

    # 1. A no-step entrance — thresholds aren't in the geometry, so a reminder.
    add(
        Issue(
            Severity.INFO,
            "ACCESS_ENTRY",
            "Accessible target: provide at least one no-step entrance (threshold "
            "≤ ½ in) with a level 5 ft × 5 ft landing (ANSI A117.1).",
            hint="Make the main entry no-step — a slab-on-grade helps; avoid a "
            "stoop step.",
        )
    )

    # 2. Accessible clear widths on the living route (not the garage/shop door).
    for d in plan.interior_doors:
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None or a.type in GARAGE_TYPES or b.type in GARAGE_TYPES:
            continue
        # A double/french pair travels through ONE leaf, so it counts half.
        double = d.kind in DOUBLE_LEAF_KINDS
        eff = d.width / 2.0 if double else d.width
        need = ACCESSIBLE_LEAF_MIN if d.leaf else ACCESSIBLE_CLEAR_DOOR
        if eff + EPSILON < need:
            kind, need_in = ("door", "34 in leaf") if d.leaf else ("opening", "32 in")
            add(
                Issue(
                    Severity.INFO,
                    "ACCESS_DOOR",
                    f"The {kind} between '{d.room_a}' and '{d.room_b}' is "
                    f"{_f(eff * 12)} in wide{' per leaf' if double else ''}; an "
                    f"accessible route needs a {need_in} "
                    "(32 in clear, ANSI A117.1 §404).",
                    hint=f"Widen it to ≥ {need_in.split()[0]} in"
                    + (" per leaf" if double else "")
                    + ".",
                )
            )
    for xd in plan.exterior_doors:
        room = by_id.get(xd.room)
        if room is not None and room.type in GARAGE_TYPES:
            continue
        if _door_clear_width(xd) + EPSILON < ACCESSIBLE_EXTERIOR_MIN:
            xdouble = getattr(xd, "kind", "entry") in DOUBLE_LEAF_KINDS
            add(
                Issue(
                    Severity.INFO,
                    "ACCESS_DOOR",
                    f"The exterior door on '{xd.room}' is "
                    f"{_f(_door_clear_width(xd) * 12)} in wide"
                    f"{' per leaf' if xdouble else ''}; "
                    "an accessible entrance wants a 36 in door.",
                    room=xd.room,
                    hint="Use a 36 in exterior door"
                    + (" leaf" if xdouble else "")
                    + " on the accessible entrance.",
                )
            )

    # 3. A wheelchair turning space in a ground-level full bath.
    for room in plan.rooms:
        if room.type is not RoomType.BATHROOM or room.level != 0:
            continue
        cw, cl = clear_dimensions(plan, room)
        if min(cw, cl) + EPSILON < ACCESSIBLE_TURN:
            add(
                Issue(
                    Severity.INFO,
                    "ACCESS_BATH",
                    f"Bathroom '{room.id}' has ~{_f(min(cw, cl))} ft clear on its "
                    f"short side; a wheelchair turning space needs {_f(ACCESSIBLE_TURN)} "
                    "ft (60 in) — plan a roll-in shower and grab-bar blocking too "
                    "(ANSI A117.1).",
                    room=room.id,
                    hint=f"Widen the bath so the clear short side is ≥ "
                    f"{_f(ACCESSIBLE_TURN)} ft.",
                )
            )

    # 4. Single-floor living: a bedroom AND a full bath on the entry level.
    ground = [r for r in plan.rooms if r.level == 0]
    has_bed = any(r.type is RoomType.BEDROOM for r in ground)
    has_bath = any(r.type is RoomType.BATHROOM for r in ground)
    if not (has_bed and has_bath):
        missing = " and ".join(
            w for w, ok in (("a bedroom", has_bed), ("a full bath", has_bath)) if not ok
        )
        add(
            Issue(
                Severity.INFO,
                "ACCESS_SINGLE_FLOOR",
                f"The entry level has no {missing}; accessible / aging-in-place living "
                "wants a bedroom and a full bath on one no-stair floor.",
                hint="Place a primary bedroom and a full bath on the ground level.",
            )
        )


def _validate_fixtures(plan: Barndominium, add) -> None:
    """Check that wet rooms and kitchens can actually hold their fixtures with
    code clearances (IRC R307 for the bath; a working aisle for the kitchen).

    Uses the clear (finish-face) interior, so the check reflects the built room,
    not the nominal rectangle. A bath that can't fit toilet/lav/tub with
    clearances is a ``BATH_CLEARANCE`` warning; a cramped kitchen is a
    ``KITCHEN_FIT`` info.
    """
    from .fixtures import fixtures_fit  # lazy: fixtures imports back from here

    for room in plan.rooms:
        if room.type not in (RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.KITCHEN):
            continue
        clear_w, clear_l = clear_dimensions(plan, room)
        ok, reason = fixtures_fit(room.type, clear_w, clear_l)
        if ok:
            continue
        if room.type is RoomType.KITCHEN:
            add(
                Issue(
                    Severity.INFO,
                    "KITCHEN_FIT",
                    f"Kitchen '{room.id}' is tight for its appliances: {reason}.",
                    room=room.id,
                    hint="Enlarge it so a sink, range and refrigerator fit with a "
                    "working aisle.",
                )
            )
        else:
            label = room.type.value.replace("_", " ")
            add(
                Issue(
                    Severity.WARNING,
                    "BATH_CLEARANCE",
                    f"{label.capitalize()} '{room.id}' can't fit its fixtures with "
                    f"clearances: {reason} (IRC R307).",
                    room=room.id,
                    hint="Enlarge the room so the toilet/lavatory"
                    + ("/tub" if room.type is RoomType.BATHROOM else "")
                    + " fit with clear floor in front.",
                )
            )


def _validate_furniture(plan: Barndominium, add) -> None:
    """Furniture-fit nudges for habitable rooms — the livability companion to the
    wet-room fixture checks. A room can clear ``BEDROOM_AREA`` yet be the wrong
    *shape* to arrange: a long thin bedroom that won't hold a bed with a
    walk-around, a dining room too tight to pull a chair. Uses the clear
    (finish-face) interior, like the fixture checks. INFO — guidance, not a gate.
    """
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            cw, cl = clear_dimensions(plan, room)
            short, long = min(cw, cl), max(cw, cl)
            if short + EPSILON < BED_FURNISH_SHORT or long + EPSILON < BED_FURNISH_LONG:
                add(
                    Issue(
                        Severity.INFO,
                        "BED_CLEARANCE",
                        f"Bedroom '{room.id}' is {_f(cw)}×{_f(cl)} ft clear — too tight "
                        "to place a queen bed with a walk-around (needs about "
                        f"{_f(BED_FURNISH_LONG)}×{_f(BED_FURNISH_SHORT)} ft clear).",
                        room=room.id,
                        hint="Widen or reshape the room so a queen bed backs to a wall "
                        "with a ~24 in path on one long side.",
                    )
                )
        elif room.type is RoomType.DINING:
            cw, cl = clear_dimensions(plan, room)
            if min(cw, cl) + EPSILON < DINING_FURNISH_CLEAR:
                add(
                    Issue(
                        Severity.INFO,
                        "DINING_CLEARANCE",
                        f"Dining room '{room.id}' is {_f(cw)}×{_f(cl)} ft clear — too "
                        "tight to seat a table with room to pull the chairs (needs "
                        f"about {_f(DINING_FURNISH_CLEAR)} ft clear each way).",
                        room=room.id,
                        hint="Enlarge it so a 4-seat table (~3 ft) has ~30 in of "
                        "chair-pull and circulation all round.",
                    )
                )


def _validate_storage(plan: Barndominium, add) -> None:
    """Flag a storage-poor plan — dedicated storage (closets + pantry) below a small
    fraction of the conditioned area. Deterministic and unconditional, but the floor
    is conservative (below the worked gallery), so it only catches a home with almost
    no closets. INFO. For a specific target, declare `program ... storage <sqft>`.
    """
    interior = plan.interior_area
    if interior <= EPSILON:
        return
    storage = sum(r.area for r in plan.rooms if r.type in STORAGE_TYPES)
    if storage / interior < LOW_STORAGE_RATIO:
        add(
            Issue(
                Severity.INFO,
                "LOW_STORAGE",
                f"Dedicated storage (closets + pantry) is {storage:.0f} sq ft — "
                f"{storage / interior * 100:.1f}% of the conditioned area, a "
                "storage-poor plan.",
                hint="Add closets or a pantry — a linen closet by the baths, a coat "
                "closet at the entry, a walk-in pantry off the kitchen.",
            )
        )


def _validate_room_programs(plan: Barndominium, add, profile: Profile = DEFAULT) -> None:
    bed_area = profile.min_bedroom_area
    bed_dim = profile.min_bedroom_dimension
    hall_w = profile.min_hallway_width
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            if room.area < bed_area:
                need_len = _suggest_int(bed_area / max(room.width, EPSILON))
                hint = (
                    f"Enlarge it, e.g. `size {_f(room.width)} x {need_len}`."
                    if need_len is not None
                    else f"Enlarge it to at least {bed_area:g} sq ft."
                )
                tag = _profile_tag(profile, "min_bedroom_area", f"{MIN_BEDROOM_AREA:.0f} sq ft")
                minlbl = f"IRC minimum is {bed_area:g} sq ft" if not tag else (
                    f"the enforced minimum is {bed_area:g} sq ft"
                )
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_AREA",
                        f"Bedroom is {_f(room.area)} sq ft; {minlbl}{tag}.",
                        room=room.id,
                        hint=hint,
                    )
                )
            if room.min_dimension < bed_dim:
                tag = _profile_tag(
                    profile, "min_bedroom_dimension", f"{MIN_BEDROOM_DIMENSION:.0f} ft"
                )
                add(
                    Issue(
                        Severity.ERROR,
                        "BEDROOM_DIM",
                        f"Bedroom's smallest dimension is {_f(room.min_dimension)} ft; "
                        f"minimum is {bed_dim:g} ft{tag}.",
                        room=room.id,
                        hint=f"Make both dimensions >= {bed_dim:g} ft.",
                    )
                )
        elif room.type in R304_HABITABLE_TYPES:
            # R304 habitable-room minimums (WARNING): 70 sq ft floor + 7 ft in
            # every horizontal dimension. Bedrooms are handled above as errors and
            # kitchens are exempt (R304.2), so neither reaches here.
            kind = room.type.value.replace("_", " ").capitalize()
            if room.area < bed_area:
                need_len = _suggest_int(bed_area / max(room.width, EPSILON))
                sizing = (
                    f" e.g. `size {_f(room.width)} x {need_len}`"
                    if need_len is not None else ""
                )
                tag = _profile_tag(profile, "min_bedroom_area", f"{MIN_BEDROOM_AREA:.0f} sq ft")
                add(
                    Issue(
                        Severity.WARNING,
                        "ROOM_HABITABLE",
                        f"{kind} '{room.id}' is {_f(room.area)} sq ft; the R304 "
                        f"minimum habitable area is {bed_area:g} sq ft{tag}.",
                        room=room.id,
                        hint=f"Enlarge it to >= {bed_area:g} sq ft{sizing}.",
                    )
                )
            if room.min_dimension < bed_dim:
                tag = _profile_tag(
                    profile, "min_bedroom_dimension", f"{MIN_BEDROOM_DIMENSION:.0f} ft"
                )
                add(
                    Issue(
                        Severity.WARNING,
                        "ROOM_HABITABLE",
                        f"{kind} '{room.id}' is {_f(room.min_dimension)} ft on its "
                        f"smallest dimension; the R304 minimum is {bed_dim:g} ft{tag}.",
                        room=room.id,
                        hint=f"Make both dimensions >= {bed_dim:g} ft.",
                    )
                )
        if room.type is RoomType.HALLWAY and room.min_dimension < hall_w:
            tag = _profile_tag(profile, "min_hallway_width", f"{MIN_HALLWAY_WIDTH:.0f} ft")
            add(
                Issue(
                    Severity.ERROR,
                    "HALL_WIDTH",
                    f"Hallway is {_f(room.min_dimension)} ft wide; minimum is "
                    f"{hall_w:g} ft{tag}.",
                    room=room.id,
                    hint=f"Widen it to >= {hall_w:g} ft.",
                )
            )
        floor = MIN_USABLE_AREA.get(room.type)
        short_floor = MIN_ROOM_SHORT_SIDE.get(room.type)
        if floor is not None and 0 < room.area < floor:
            need_len = _suggest_int(floor / max(room.width, EPSILON))
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
        _check_clear_dimension(plan, room, add, profile)


def _clear_targets(room: Room, profile: Profile = DEFAULT):
    """The *hard-code* minimums the IRC measures between finished surfaces, keyed
    by room type → ``(kind, minimum)`` where ``kind`` is ``"area"`` or ``"short"``.

    Deliberately only the legal minimums — bedroom habitable area/width (R304) and
    hallway width (R311.6) — not the softer ``ROOM_TIGHT`` comfort floors: a plan
    that passes one of *these* nominally but fails it once wall thickness is
    applied is genuinely non-compliant when built, which is exactly what
    ``ROOM_CLEAR`` is for. (A conventionally-fine 6 ft bath shouldn't be nagged
    for losing a wall thickness.)
    """
    if room.type is RoomType.BEDROOM:
        return [
            ("area", profile.min_bedroom_area),
            ("short", profile.min_bedroom_dimension),
        ]
    if room.type is RoomType.HALLWAY:
        return [("short", profile.min_hallway_width)]
    return []


def _check_clear_dimension(
    plan: Barndominium, room: Room, add, profile: Profile = DEFAULT
) -> None:
    """Flag a room that meets a clear-measured minimum on its nominal rectangle
    but falls below it once the bounding walls' thickness is subtracted.

    Only fires when the room is *nominally compliant* on every one of its
    hard-code targets — if it already fails one on paper, that error owns the
    problem and a clear nudge would just be noise.
    """
    targets = _clear_targets(room, profile)
    if not targets:
        return

    def nominal_of(kind: str) -> float:
        return room.area if kind == "area" else room.min_dimension

    if any(nominal_of(kind) + EPSILON < minimum for kind, minimum in targets):
        return  # a nominal failure is already reported at higher severity

    clear_w, clear_l = clear_dimensions(plan, room)
    clear_area = clear_w * clear_l
    clear_short = min(clear_w, clear_l)
    for kind, minimum in targets:
        clear = clear_area if kind == "area" else clear_short
        if minimum > clear + 1e-3:
            nominal = nominal_of(kind)
            unit = "sq ft" if kind == "area" else "ft"
            where = "usable area" if kind == "area" else "short side"
            if room.type is RoomType.BEDROOM:
                field = "min_bedroom_area" if kind == "area" else "min_bedroom_dimension"
                base = MIN_BEDROOM_AREA if kind == "area" else MIN_BEDROOM_DIMENSION
            else:
                field, base = "min_hallway_width", MIN_HALLWAY_WIDTH
            tag = _profile_tag(profile, field, f"{base:g} {unit}")
            add(
                Issue(
                    Severity.INFO,
                    "ROOM_CLEAR",
                    f"{room.type.value.replace('_', ' ').capitalize()} '{room.id}' "
                    f"measures {_f(nominal)} {unit} nominal but only ~{_f(clear)} {unit} "
                    f"clear (finish-face); the {minimum:g} {unit} minimum is measured "
                    "between finished surfaces, so the built room falls short"
                    + tag + ".",
                    room=room.id,
                    hint=f"Add wall thickness to the {where}: grow it ~"
                    f"{_f(minimum - clear)} {unit} so the clear dimension still meets "
                    f"{minimum:g} {unit}.",
                )
            )
            return  # one nudge per room is enough


def _door_loc(door) -> dict:
    """Source-location kwargs for a door/window's own statement (if known)."""
    return {"line": door.line, "col": door.col, "end_col": door.end_col}


def _validate_doors(plan: Barndominium, add) -> None:
    room_ids = {r.id for r in plan.rooms}
    for door in plan.interior_doors:
        loc = _door_loc(door)
        if door.width <= 0:
            add(
                Issue(
                    Severity.ERROR,
                    "OPENING_SIZE",
                    f"Interior door between '{door.room_a}' and '{door.room_b}' has "
                    f"non-positive width ({_f(door.width)} ft).",
                    room=door.room_a,
                    hint="Give it a positive width, e.g. `width 3`.",
                    **loc,
                )
            )
            continue
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
            elif edge.length + EPSILON < door.width:
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
            elif door.offset is not None and _runs_off_wall(
                door.offset, door.width, edge.length
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
                    if depth + EPSILON < door.width:
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
                        # Deliberately the raw constant, not the profile: this
                        # is a residual swing-clearance heuristic, not the
                        # R311.6 hall-width rule (HALL_WIDTH tracks the profile;
                        # this asks "can you get past the open leaf").
                        and depth - door.width + EPSILON < MIN_HALLWAY_WIDTH
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
            # check only applies to swinging doors. A double/french pair passes
            # its full width with both leaves open, so the total-width check
            # stays honest for it too (egress is where one leaf counts).
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
            # A swing door that *is* wide enough should still be an orderable
            # size. A declared double/french pair checks against the stock pair
            # widths (two equal leaves) instead of the single-leaf sizes.
            sizes: tuple[int, ...] = (
                STD_DOUBLE_DOOR_WIDTHS_IN
                if door.kind in DOUBLE_LEAF_KINDS
                else STD_INTERIOR_DOOR_WIDTHS_IN
            )
            nearest = _nearest_std(door.width * 12, sizes)
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
            if min(a_hi, b_hi) - max(a_lo, b_lo) > EPSILON:
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

    for xdoor in plan.exterior_doors:
        if xdoor.room not in room_ids:
            add(
                Issue(
                    Severity.ERROR,
                    "DOOR_REF",
                    f"Exterior door references unknown room '{xdoor.room}'.",
                    room=xdoor.room,
                    hint="Reference an existing room id.",
                    **_door_loc(xdoor),
                )
            )
            continue
        if xdoor.width <= 0:
            add(
                Issue(
                    Severity.ERROR,
                    "OPENING_SIZE",
                    f"Exterior door on '{xdoor.room}' has non-positive width "
                    f"({_f(xdoor.width)} ft).",
                    room=xdoor.room,
                    hint="Give it a positive width, e.g. `width 3` (or `width 9` "
                    "for an overhead door).",
                    **_door_loc(xdoor),
                )
            )
            continue
        room = plan.room(xdoor.room)
        if xdoor.overhead:
            _check_overhead_door(xdoor, room, add)
            continue
        if room is not None and room.type in (RoomType.GARAGE, RoomType.SHOP):
            continue  # a garage/shop opening is an overhead door, not a leaf size
        sizes = (
            STD_DOUBLE_DOOR_WIDTHS_IN
            if getattr(xdoor, "kind", "entry") in DOUBLE_LEAF_KINDS
            else STD_EXTERIOR_DOOR_WIDTHS_IN
        )
        nearest = _nearest_std(xdoor.width * 12, sizes)
        if abs(nearest - xdoor.width * 12) > DOOR_SIZE_TOL_IN:
            add(
                Issue(
                    Severity.INFO,
                    "DOOR_SIZE",
                    f"Exterior door on '{xdoor.room}' is {xdoor.width * 12:.0f} in, "
                    "not a standard size.",
                    room=xdoor.room,
                    hint=f"Use a stock width, e.g. `width {nearest / 12:g}` "
                    f"({nearest} in); 36 in is the usual entry.",
                    **_door_loc(xdoor),
                )
            )


def _check_overhead_door(xdoor, room, add) -> None:
    """Overhead (sectional garage) door checks: an unusual host room, a
    non-stock sectional size, and a double-width opening's header reality."""
    loc = _door_loc(xdoor)
    if room is not None and room.type not in GARAGE_TYPES:
        add(
            Issue(
                Severity.INFO,
                "OVERHEAD_ROOM",
                f"Overhead door on '{xdoor.room}' ({room.type.value}) — an "
                "overhead door in a living space is unusual; is this a "
                "garage/shop?",
                room=xdoor.room,
                hint="Put it on a garage/shop bay, or use `entry` for a "
                "people door.",
                **loc,
            )
        )
    h = xdoor.height if xdoor.height is not None else float(STD_OVERHEAD_DOOR_HEIGHTS_FT[0])
    near_w = _nearest_std(xdoor.width, STD_OVERHEAD_DOOR_WIDTHS_FT)
    near_h = _nearest_std(h, STD_OVERHEAD_DOOR_HEIGHTS_FT)
    tol = DOOR_SIZE_TOL_IN / 12
    if abs(near_w - xdoor.width) > tol or abs(near_h - h) > tol:
        add(
            Issue(
                Severity.INFO,
                "DOOR_SIZE",
                f"Overhead door on '{xdoor.room}' is {_f(xdoor.width)} x {_f(h)} "
                "ft, not a standard sectional size.",
                room=xdoor.room,
                hint=f"Use a stock size, e.g. `width {near_w:g} height {near_h:g}` "
                "— widths 8/9/10/12/16 ft, heights 7/8 ft (16 x 7 is the usual "
                "double).",
                **loc,
            )
        )
    if xdoor.width > OVERHEAD_HEADER_SPAN + EPSILON:
        add(
            Issue(
                Severity.INFO,
                "OVERHEAD_HEADER",
                f"A {_f(xdoor.width)} ft opening needs an engineered header — "
                "coordinate with the frame.",
                room=xdoor.room,
                hint="Have the header and jamb posts over this opening engineered "
                f"(a stock header tops out around {OVERHEAD_HEADER_SPAN:g} ft), or "
                "split it into two singles.",
                **loc,
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
        assert room is not None  # guaranteed: w.room was checked against room_ids
        if w.width <= 0:
            add(
                Issue(
                    Severity.ERROR,
                    "OPENING_SIZE",
                    f"Window on '{w.room}' has non-positive width ({_f(w.width)} ft).",
                    room=w.room,
                    hint="Give it a positive width, e.g. `width 3`.",
                    **_door_loc(w),
                )
            )
            continue
        if w.head_height <= w.sill_height + EPSILON:
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
        if _runs_off_wall(w.offset, w.width, wlen):
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
        assert room is not None  # guaranteed: d.room was checked against room_ids
        wlen = _wall_length(room, d.wall)
        if _runs_off_wall(d.offset, d.width, wlen):
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
    openings: list[tuple[_WallOpening, str]] = [
        *((w, "window") for w in plan.windows),
        *((d, "entry") for d in plan.exterior_doors),
    ]
    for o, kind in openings:
        if o.room not in room_ids:
            continue  # *_REF already raised
        spans.setdefault((o.room, o.wall), []).append(
            (o.offset, o.offset + o.width, kind, o)
        )
    for (rid, wall), items in spans.items():
        items.sort()  # by start offset
        for (a_lo, a_hi, a_kind, a_o), (b_lo, b_hi, b_kind, b_o) in zip(items, items[1:]):
            ov = min(a_hi, b_hi) - max(a_lo, b_lo)
            if ov > EPSILON:
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


# --- safety glazing (IRC R308.4) --------------------------------------------
#
# A single predicate, shared by the WINDOW_TEMPERED check below AND the window
# schedule's "Glazing" column (schedule.py imports it), so the schedule can never
# disagree with the diagnostic about which windows are hazard locations.

#: Hazard-location proximities requiring tempered/safety glazing (IRC R308.4), ft.
_TEMPERED_DOOR = 2.0    # 24 in of a door edge in the same wall plane (R308.4.1)
_TEMPERED_WET = 5.0     # 60 in of a tub/shower in a wet room (R308.4.5)
_TEMPERED_STAIR = 3.0   # 36 in of a stair flight (R308.4.6/.7, simplified)


def _seg_axis(x1: float, y1: float, x2: float, y2: float) -> tuple[str, float, float, float]:
    """An axis-aligned opening segment as ``(orientation, fixed, lo, hi)`` — ``'h'``
    (runs in x at a fixed y) or ``'v'`` (runs in y at a fixed x)."""
    if abs(y1 - y2) <= abs(x1 - x2):
        return "h", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2)
    return "v", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2)


def _interval_gap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """The gap between two 1-D intervals (``0`` when they overlap/touch)."""
    if a_hi < b_lo:
        return b_lo - a_hi
    if b_hi < a_lo:
        return a_lo - b_hi
    return 0.0


def _rect_seg_distance(
    rx: float, ry: float, rw: float, rl: float,
    x1: float, y1: float, x2: float, y2: float,
) -> float:
    """Minimum plan distance from the axis-aligned rect ``(rx,ry,rw,rl)`` to the
    axis-aligned segment ``(x1,y1)-(x2,y2)``."""
    dx = _interval_gap(rx, rx + rw, min(x1, x2), max(x1, x2))
    dy = _interval_gap(ry, ry + rl, min(y1, y2), max(y1, y2))
    return math.hypot(dx, dy)


#: R308.4 exempts glazing whose bottom (sill) edge is high above the walking/
#: standing surface — a person can't fall into it. R308.4.5 (wet) uses 60 in;
#: R308.4.6/.7 (stairs) uses 36 in. These sill floors keep an ordinary high
#: privacy window out of the hazard set (and are the built-in "escape hatch"
#: until a `tempered` attribute exists).
_TEMPERED_WET_SILL = 5.0    # 60 in bottom-edge exemption (R308.4.5)
_TEMPERED_STAIR_SILL = 3.0  # 36 in bottom-edge exemption (R308.4.6/.7)

#: R308.4.3 "large glazing panel" hazard — a pane over 9 sq ft whose bottom edge
#: sits below 18 in and whose top edge rises above 36 in above the floor is a
#: walk-into hazard *anywhere* (not just beside a door/tub/stair), so it needs
#: safety glazing. Fixed IRC figures (not jurisdiction-variable).
_TEMPERED_PANEL_AREA = 9.0        # sq ft — the "exposed area of an individual pane"
_TEMPERED_PANEL_BOTTOM = 18.0 / 12.0  # 18 in — bottom (sill) edge below this
_TEMPERED_PANEL_TOP = 36.0 / 12.0     # 36 in — top (head) edge above this


def _room_door_segments(
    plan: Barndominium, room: Room
) -> list[tuple[float, float, float, float]]:
    """World-space opening segments of every HINGED door that belongs to ``room``
    — an exterior entry/double/french on one of its walls, or an interior swing/
    double/french on a wall it shares. Overhead, cased, pocket and sliding doors
    have no hinged leaf, so R308.4.1 doesn't count them. Scoping to the window's
    OWN room keeps a door between two *other* rooms that merely lines up with the
    wall from counting."""
    by_id = {r.id: r for r in plan.rooms}
    segs: list[tuple[float, float, float, float]] = []
    for xd in plan.exterior_doors:
        if xd.overhead or xd.room != room.id:
            continue
        segs.append(opening_endpoints(room, xd.wall, xd.offset, xd.width))
    for d in plan.interior_doors:
        if getattr(d, "kind", "swing") not in ("swing", "double", "french"):
            continue
        if room.id not in (d.room_a, d.room_b):
            continue
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None:
            continue
        edge = shared_edge(a, b)
        if edge is None:
            continue
        w = min(d.width, edge.length)
        offset = getattr(d, "offset", None)
        if offset is None:
            start = edge.mid - w / 2.0
        else:
            start = edge.lo + max(0.0, min(offset, edge.length - w))
        if edge.orientation == "v":
            segs.append((edge.pos, start, edge.pos, start + w))
        else:
            segs.append((start, edge.pos, start + w, edge.pos))
    return segs


def window_tempered_reason(plan: Barndominium, window) -> str | None:
    """Why ``window`` is a safety-glazing hazard location (IRC R308.4), or ``None``.

    The single rule behind both the WINDOW_TEMPERED warning and the window
    schedule's Glazing column. Three simplified hazard locations, checked in order:

    (a) within 24 in horizontally of either edge of a hinged door in the **same
        wall plane** of the SAME room (a door sidelite) — R308.4.1;
    (b) a low window (sill < 60 in) in a wet room whose wall segment lies within
        60 in of a tub/shower footprint — R308.4.5;
    (c) a low window (sill < 36 in) within 36 in of a stair flight footprint —
        R308.4.6/.7 (simplified to a plan-distance-to-footprint test).

    The sill floors are R308.4's own bottom-edge exemptions, and they double as the
    built-in escape hatch (a high privacy window is not a hazard) until a
    ``tempered`` override attribute exists.
    """
    by_id = {r.id: r for r in plan.rooms}
    room = by_id.get(window.room)
    if room is None:
        return None
    wx1, wy1, wx2, wy2 = opening_endpoints(room, window.wall, window.offset, window.width)
    w_or, w_fixed, w_lo, w_hi = _seg_axis(wx1, wy1, wx2, wy2)
    sill = getattr(window, "sill_height", 3.0)
    # (a) door sidelite — a hinged door of the SAME room, coplanar, gap < 24 in.
    for dx1, dy1, dx2, dy2 in _room_door_segments(plan, room):
        d_or, d_fixed, d_lo, d_hi = _seg_axis(dx1, dy1, dx2, dy2)
        if d_or != w_or or abs(d_fixed - w_fixed) > 0.1:
            continue
        if _interval_gap(w_lo, w_hi, d_lo, d_hi) < _TEMPERED_DOOR:
            return "within 24 in of a door opening in the same wall — R308.4.1"
    # (b) wet-zone tub/shower proximity (uses the resolved fixtures — seeds too).
    if room.type in WET_TYPES and sill < _TEMPERED_WET_SILL:
        from .fixtures import resolve_room_fixtures

        for f in resolve_room_fixtures(plan, room):
            if f.kind not in ("tub", "shower"):
                continue
            if _rect_seg_distance(f.x, f.y, f.width, f.length, wx1, wy1, wx2, wy2) < _TEMPERED_WET:
                return "within 60 in of a tub/shower in a wet room — R308.4.5"
    # (c) stair proximity (simplified footprint distance).
    if sill < _TEMPERED_STAIR_SILL:
        for s in plan.stairs:
            if _rect_seg_distance(s.x, s.y, s.width, s.length, wx1, wy1, wx2, wy2) < _TEMPERED_STAIR:
                return "within 36 in of a stair flight — R308.4.6 (simplified)"
    # (d) large glazing panel near the walking surface (R308.4.3), anywhere: an
    #     individual pane over 9 sq ft whose bottom edge is below 18 in and top
    #     edge above 36 in above the floor — a full-height sash a person can walk
    #     into. Uses the same head/sill the schedule reads, so no new geometry.
    head = getattr(window, "head_height", sill)
    pane = window.width * max(0.0, head - sill)
    if (
        sill < _TEMPERED_PANEL_BOTTOM
        and head > _TEMPERED_PANEL_TOP
        and pane > _TEMPERED_PANEL_AREA
    ):
        return (
            "a glazed panel over 9 sq ft with its bottom edge below 18 in and top "
            "above 36 in above the floor — R308.4.3"
        )
    return None


def _validate_safety_glazing(plan: Barndominium, add) -> None:
    """Flag each window in an IRC R308.4 hazard location as needing tempered glass.

    One WINDOW_TEMPERED warning per offending window, naming the trigger. A window
    that already declares ``tempered`` (the R308.4 escape hatch) is skipped — it is
    specified as safety glass, so there is nothing to warn about (the schedule
    still records it as "tempered (declared)")."""
    for w in plan.windows:
        if getattr(w, "tempered", False):
            continue
        reason = window_tempered_reason(plan, w)
        if reason is None:
            continue
        add(
            Issue(
                Severity.WARNING,
                "WINDOW_TEMPERED",
                f"The window in '{w.room}' is a hazard location ({reason}), so it "
                "needs safety (tempered) glazing.",
                room=w.room,
                hint="Specify tempered/safety glazing for this window on the window "
                "schedule (human glazing in this location must resist impact, IRC "
                "R308.4).",
                **_door_loc(w),
            )
        )


#: IRC R312.2 window fall protection: an operable window whose sill is below this
#: height needs an opening-control device or fall guard where it sits more than
#: 72 in above the exterior grade below. The model carries no grade elevation, so
#: "on an upper level" (level >= 1) is the stand-in for "far enough above grade".
#: A fixed IRC figure (not jurisdiction-variable).
_WINDOW_FALL_SILL = 24.0 / 12.0  # 24 in


def _validate_window_fall(plan: Barndominium, add) -> None:
    """Flag an operable low-silled window on an upper level for fall protection.

    IRC R312.2 requires an opening-control device (or a fall-prevention guard) at
    an operable window whose sill is below 24 in *and* more than 72 in above the
    grade below. The DSL has no grade elevation, so this uses an upper storey
    (``level >= 1``) as the proxy for "well above grade" — stated plainly in the
    message. A ``fixed`` sash can't open, so it is exempt (R312.2 covers operable
    windows only). The hint points at an opening-control device rather than
    raising the sill, because a bedroom's escape window *wants* a low sill (IRC
    R310) — the two rules are reconciled by an ASTM F2090 device, not by geometry.
    """
    by_id = {r.id: r for r in plan.rooms}
    for w in plan.windows:
        if not getattr(w, "openable", True):
            continue  # a fixed sash doesn't open — no fall path
        room = by_id.get(w.room)
        if room is None or getattr(room, "level", 0) < 1:
            continue
        sill = getattr(w, "sill_height", 3.0)
        if sill + EPSILON >= _WINDOW_FALL_SILL:
            continue
        add(
            Issue(
                Severity.WARNING,
                "WINDOW_FALL",
                f"The operable window in '{w.room}' has a {sill * 12:.0f} in sill on "
                f"level {room.level}; IRC R312.2 wants window fall protection where an "
                "operable sash sits below 24 in and more than 72 in above the grade "
                "below. The model has no grade elevation, so an upper storey stands in "
                "for 'well above grade'.",
                room=w.room,
                hint="Fit a window opening-control device or fall guard (ASTM F2090) "
                "that limits the sash to a 4 in clear opening yet still releases for "
                "escape — don't raise the sill, which would fight the egress-window "
                "rule (IRC R310).",
                **_door_loc(w),
            )
        )


def _stair_rooms(plan: Barndominium, stair, level: int) -> list[Room]:
    """Rooms on ``level`` whose footprint the stair lands in."""
    return [
        r
        for r in plan.rooms
        if r.level == level and stair.overlaps_rect(r.x, r.y, r.x2, r.y2)
    ]


#: IRC R311.7.3 — the maximum vertical rise of a single flight between floor
#: levels or landings (12 ft 7 in). The task brief cites ~12 ft 3 in; the adopted
#: IRC figure is 151 in, used here. A fixed code value, not jurisdiction-variable.
_STAIR_MAX_FLIGHT_RISE = 151.0 / 12.0


def _validate_stairs(plan: Barndominium, add, profile: Profile = DEFAULT) -> None:
    handrail_noted = False  # STAIR_HANDRAIL is one per plan (first qualifying flight)
    for s in plan.stairs:
        if not all(math.isfinite(v) for v in (s.x, s.y, s.width, s.length)):
            add(Issue(Severity.ERROR, "STAIR_GEOMETRY",
                      f"Stair '{s.id}' has non-finite coordinates or size.", room=s.id,
                      hint="Use finite measurements in feet (no nan/inf)."))
            continue
        # Handrail (R311.7.8): four or more risers on a flight need a handrail.
        # The DSL can't place a rail, so this is a one-per-plan checklist INFO on
        # the first qualifying stair — computed from the same rise/riser math the
        # geometry-fit checks below use.
        if (
            not handrail_noted
            and plan.ceiling_height > 0
            and s.from_level != s.to_level
            and s.from_level >= 0
            and s.to_level >= 0
        ):
            rise0 = plan.ceiling_height * abs(s.to_level - s.from_level)
            risers0 = max(1, math.ceil(rise0 / profile.max_riser_height))
            if risers0 >= 4:
                handrail_noted = True
                add(Issue(
                    Severity.INFO, "STAIR_HANDRAIL",
                    f"Stair '{s.id}' climbs {_f(rise0)} ft in ~{risers0} risers, so "
                    "it needs at least one handrail (34–38 in above the nosings, "
                    "graspable the full length, IRC R311.7.8).",
                    room=s.id,
                    hint="The DSL can't draw a rail — carry the handrail (both sides "
                    "if the flight is wider than 44 in) onto the construction "
                    "documents.",
                    line=s.line, col=s.col, end_col=s.end_col))
        if s.width <= 0 or s.length <= 0:
            add(Issue(Severity.ERROR, "STAIR_SIZE",
                      f"Stair '{s.id}' has non-positive size.", room=s.id,
                      hint="Use positive feet, e.g. `size 4 x 10`."))
        if s.from_level == s.to_level or s.from_level < 0 or s.to_level < 0:
            add(Issue(Severity.ERROR, "STAIR_LEVELS",
                      f"Stair '{s.id}' must connect two different levels >= 0.",
                      room=s.id, hint="e.g. `from 0 to 1`."))
        if plan.wings:
            # Rectilinear footprint: a stair may legitimately sit in a wing (or
            # straddle a seam), so test the footprint union rather than the
            # primary rectangle — same treatment as room OUT_OF_BOUNDS.
            if (
                s.width > 0
                and s.length > 0
                and not rect_in_footprint(
                    plan.footprint_sections(), s.x, s.y, s.width, s.length
                )
            ):
                add(Issue(Severity.ERROR, "STAIR_OOB",
                          f"Stair '{s.id}' extends outside the building footprint "
                          f"({_f(s.x)},{_f(s.y)} → {_f(s.x2)},{_f(s.y2)}); it isn't "
                          "covered by the envelope or any wing.",
                          room=s.id,
                          hint="Keep its footprint inside the envelope or a wing."))
        else:
            over_x = max(0.0, s.x2 - plan.envelope_width)
            over_y = max(0.0, s.y2 - plan.envelope_length)
            if s.x < -EPSILON or s.y < -EPSILON or over_x > EPSILON or over_y > EPSILON:
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
            max_riser = profile.max_riser_height
            min_tread = profile.min_tread_depth
            min_width = profile.min_stair_width
            rise = plan.ceiling_height * abs(s.to_level - s.from_level)
            risers = max(1, math.ceil(rise / max_riser))
            run_needed = max(1, risers - 1) * min_tread
            # STAIR_LANDING (R311.7.3): a single flight may rise at most 12 ft 7 in
            # (151 in) between floor levels or landings. Past that a switchback or
            # mid-run landing is required. Risers use the same rise/max_riser math as
            # the run-fit check, so a gentler profile riser (more risers per foot) is
            # reflected in the "~N risers" the message quotes. A normal one-storey
            # flight (~9 ft) stays well under, so this only speaks up on a tall or
            # multi-level run.
            if rise > _STAIR_MAX_FLIGHT_RISE + EPSILON:
                max_flight_risers = max(1, math.floor(_STAIR_MAX_FLIGHT_RISE / max_riser))
                add(Issue(
                    Severity.INFO, "STAIR_LANDING",
                    f"Stair '{s.id}' climbs {_f(rise)} ft (~{risers} risers) in one "
                    f"flight; IRC R311.7.3 limits a flight to {_f(_STAIR_MAX_FLIGHT_RISE)} "
                    f"ft (12 ft 7 in, ~{max_flight_risers} risers) of vertical rise "
                    "between landings, so it needs an intermediate landing.",
                    room=s.id,
                    hint="Break the run with a landing (a switchback or an L-turn), or "
                    "split it across levels — the DSL models one straight flight, so "
                    "note the mid-run landing on the construction documents.",
                    line=s.line, col=s.col, end_col=s.end_col))
            long_dim, short_dim = max(s.width, s.length), min(s.width, s.length)
            could_switchback = short_dim + EPSILON >= 2 * min_width
            needed = run_needed / 2 if could_switchback else run_needed
            if long_dim + EPSILON < needed:
                add(Issue(
                    Severity.WARNING, "STAIR_RUN",
                    f"Stair '{s.id}' is {_f(long_dim)} ft long, too short to climb "
                    f"{_f(rise)} ft: ~{risers} risers need about {_f(run_needed)} ft "
                    f"of run (a {_f(max_riser * 12)} in riser / {_f(min_tread * 12)} in tread)"
                    + _profile_tag(
                        profile, "max_riser_height",
                        f"{MAX_RISER_HEIGHT * 12:g} in / {MIN_TREAD_DEPTH * 12:g} in"
                    )
                    + ".",
                    room=s.id,
                    hint=f"Lengthen its footprint to >= {_f(run_needed)} ft, or make it "
                    f">= {_f(2 * min_width)} ft wide to fit a switchback."))
            else:
                # Headroom (R311.7.2): a person descending under the upper floor
                # needs 6'-8" clear until they pass the stairwell opening's edge.
                # The opening must run at least the horizontal distance over which
                # the treads drop that 6'-8" below the floor above. Approximate the
                # opening by the run's long footprint dimension — if even the whole
                # footprint is shorter than that, no cut can develop headroom.
                riser_h = rise / risers
                open_needed = STAIR_HEADROOM * (min_tread / max(riser_h, EPSILON))
                if long_dim + EPSILON < open_needed:
                    add(Issue(
                        Severity.WARNING, "STAIR_HEADROOM",
                        f"Stair '{s.id}' is {_f(long_dim)} ft long — too short for a "
                        f"floor opening that keeps {_f(STAIR_HEADROOM)} ft (6'-8\") "
                        f"headroom under the upper floor (IRC R311.7.2): the opening "
                        f"needs about {_f(open_needed)} ft of run to clear.",
                        room=s.id,
                        hint=f"Lengthen the run/stairwell opening to >= {_f(open_needed)} "
                        "ft, or lower the floor-to-floor so fewer risers are needed."))
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


def loft_guard_pairs(plan: Barndominium):
    """Yield ``(upper, lower)`` room pairs where ``upper``'s floor only *partially*
    covers ``lower`` a storey below, leaving the uncovered remainder of ``lower``
    open to ``upper``'s floor — a double-height void whose loft edge needs a guard
    (IRC R312).

    This is the single source of truth for "which loft edge is open": the
    ``LOFT_GUARD`` check and the drawing exports (SVG/DXF guard lines) both consume
    it, so a plan is never flagged without a guard line drawn, or vice versa. One
    pair per ``upper`` (the first lower it overlooks), mirroring the check.
    """
    if len(plan.levels()) < 2:
        return
    by_level: dict[int, list[Room]] = {}
    for r in plan.rooms:
        by_level.setdefault(r.level, []).append(r)

    flagged: set[str] = set()
    for upper in plan.rooms:
        if upper.level < 1 or upper.id in flagged:
            continue
        drop = plan.level_elevation(upper.level) - plan.level_elevation(upper.level - 1)
        if drop <= GUARD_DROP_TRIGGER + EPSILON:
            continue
        for lower in by_level.get(upper.level - 1, []):
            cov = upper.overlaps(lower)
            if cov <= 0.5:
                continue  # not above this room at all
            # Partially above it → the rest of `lower` is open to `upper`'s floor.
            if cov + 0.5 < lower.area:
                flagged.add(upper.id)
                yield upper, lower
                break


def loft_guard_edges(plan: Barndominium) -> list[tuple[int, float, float, float, float]]:
    """Guard-line segments ``(level, x1, y1, x2, y2)`` along every open loft edge
    :func:`loft_guard_pairs` flags — drawn on the loft's own level.

    The open edge is the boundary of the loft's floor (its overlap with the room
    below) that lies strictly *inside* that lower room: where a segment of the
    overlap rectangle's perimeter is interior to ``lower``, the lower room keeps
    going past it as open void, so the loft floor ends there over a drop. Edges
    that coincide with ``lower``'s own wall have no void beyond them and are
    skipped."""
    tol = 1e-6
    segs: list[tuple[int, float, float, float, float]] = []
    for upper, lower in loft_guard_pairs(plan):
        ix0, iy0 = max(upper.x, lower.x), max(upper.y, lower.y)
        ix1, iy1 = min(upper.x2, lower.x2), min(upper.y2, lower.y2)
        lvl = upper.level
        if ix0 > lower.x + tol:              # void to the west
            segs.append((lvl, ix0, iy0, ix0, iy1))
        if ix1 < lower.x2 - tol:             # void to the east
            segs.append((lvl, ix1, iy0, ix1, iy1))
        if iy0 > lower.y + tol:              # void to the south
            segs.append((lvl, ix0, iy0, ix1, iy0))
        if iy1 < lower.y2 - tol:             # void to the north
            segs.append((lvl, ix0, iy1, ix1, iy1))
    return segs


def _validate_guards(plan: Barndominium, add) -> None:
    """Flag an open loft/balcony edge that overlooks a double-height space and
    needs a guard (IRC R312).

    An upper-level room that only *partially* covers a room below leaves the
    uncovered part of that lower room open to the floor above — a double-height
    void. The upper room's edge along that void is a walking surface more than a
    storey up, so it needs a 36 in guard. (An upper room that fully covers the one
    below has a solid floor to its edge — no void — so it isn't flagged; that's
    why a loft sized to its great room below doesn't nag.)
    """
    for upper, lower in loft_guard_pairs(plan):
        drop = plan.level_elevation(upper.level) - plan.level_elevation(upper.level - 1)
        add(
            Issue(
                Severity.INFO,
                "LOFT_GUARD",
                f"'{upper.id}' (level {upper.level}) overlooks the "
                f"double-height space of '{lower.id}' below; its open edge is "
                f"~{_f(drop)} ft up and needs a {GUARD_HEIGHT * 12:.0f} in guard "
                "(IRC R312).",
                room=upper.id,
                hint=f"Add a {GUARD_HEIGHT * 12:.0f} in guard/railing along the "
                "open edge (with balusters spaced so a 4 in sphere can't pass).",
            )
        )


def _validate_life_safety(plan: Barndominium, add) -> None:
    """Smoke/CO-alarm coverage (IRC R314/R315).

    Two modes, chosen by whether the plan **declares** any ``alarm``:

    * No alarms declared — a single teaching reminder (info) on a plan that has
      bedrooms, pointing at the ``alarm`` statement so the sharper checks below
      can take over. A plan with no sleeping rooms isn't nagged.
    * Alarms declared — the real placement checks: a smoke alarm in each bedroom
      (ALARM_BEDROOM), a smoke alarm in a room adjacent to each bedroom
      (ALARM_HALL, an approximation of "outside each sleeping area"), a smoke
      alarm on every level (ALARM_LEVEL), and — where bedrooms coexist with an
      attached garage/shop — a CO alarm outside the sleeping areas (ALARM_CO).
    """
    by_id = {r.id: r for r in plan.rooms}
    bedrooms = [r for r in plan.rooms if r.type is RoomType.BEDROOM]
    alarms = plan.alarms

    if not alarms:
        # Nothing declared. Teach the statement once, and only where sleeping
        # rooms make it matter — an alarm-free shop building isn't nagged.
        if bedrooms:
            add(
                Issue(
                    Severity.INFO,
                    "ALARM_CO",
                    "This plan has bedrooms but declares no smoke/CO alarms. IRC "
                    "R314 wants a smoke alarm in each bedroom, outside each "
                    "sleeping area, and on every level; R315 wants a "
                    "carbon-monoxide alarm outside sleeping areas where fuel "
                    "appliances or an attached garage are present.",
                    hint="Declare them so the placement checks can verify coverage — "
                    "e.g. `alarm smoke in <bed>`, `alarm smoke in <hall>`, "
                    "`alarm co in <hall>` (or `alarm smoke_co` for a combination "
                    "unit).",
                )
            )
        return

    # Alarms are declared -> verify placement.
    smoke_rooms = {a.room for a in alarms if a.is_smoke}
    # Rooms adjacent through an interior door (the "outside the door" graph).
    adj: dict[str, set[str]] = {r.id: set() for r in plan.rooms}
    for d in plan.interior_doors:
        if d.room_a in adj and d.room_b in adj:
            adj[d.room_a].add(d.room_b)
            adj[d.room_b].add(d.room_a)

    # ALARM_BEDROOM — a smoke (or combo) alarm inside every bedroom (R314.3).
    for b in bedrooms:
        if b.id not in smoke_rooms:
            add(
                Issue(
                    Severity.WARNING,
                    "ALARM_BEDROOM",
                    f"Bedroom '{b.id}' has no smoke alarm. IRC R314.3 requires a "
                    "smoke alarm in each sleeping room.",
                    room=b.id,
                    hint=f"Add `alarm smoke in {b.id}` (or `alarm smoke_co in "
                    f"{b.id}` for a combination unit).",
                )
            )

    # ALARM_HALL — a smoke alarm just outside each sleeping area (R314.3(2)).
    # Approximation: a room ADJACENT to the bedroom (a hall preferred, else any
    # room reachable through a door) must carry a smoke alarm.
    for b in bedrooms:
        neighbours = adj.get(b.id, set())
        if not any(n in smoke_rooms for n in neighbours):
            # sorted() so the suggested room is stable across PYTHONHASHSEED —
            # a set's iteration order must never leak into the hint text.
            ordered_nb = sorted(neighbours)
            hall = next(
                (n for n in ordered_nb if by_id.get(n) and by_id[n].type is RoomType.HALLWAY),
                None,
            )
            target = hall or (next(iter(ordered_nb), None))
            where = f" (e.g. `alarm smoke in {target}`)" if target else ""
            add(
                Issue(
                    Severity.WARNING,
                    "ALARM_HALL",
                    f"No smoke alarm is outside the sleeping area of bedroom "
                    f"'{b.id}': no room adjacent to it carries one (IRC R314.3 "
                    "wants an alarm outside each sleeping area).",
                    room=b.id,
                    hint="Approximated as 'a room sharing a door with the bedroom' "
                    "(a hallway if there is one, else any adjacent room)" + where + ".",
                )
            )

    # ALARM_LEVEL — a smoke alarm on every level (R314.3(3)).
    smoke_levels = {
        by_id[a.room].level for a in alarms if a.is_smoke and a.room in by_id
    }
    for lvl in sorted({getattr(r, "level", 0) for r in plan.rooms}):
        if lvl not in smoke_levels:
            add(
                Issue(
                    Severity.WARNING,
                    "ALARM_LEVEL",
                    f"Level {lvl} has no smoke alarm. IRC R314.3(3) requires at "
                    "least one on every storey of the dwelling.",
                    hint=f"Place `alarm smoke in <room on level {lvl}>`.",
                )
            )

    # ALARM_CO — a CO alarm outside the sleeping areas where bedrooms coexist with
    # an attached garage/shop (R315). INFO, not a warning: fuel-fired appliances
    # aren't modelled, so the trigger is only the attached garage/shop we can see.
    garage = next((r for r in plan.rooms if r.type in GARAGE_TYPES), None)
    if bedrooms and garage is not None:
        co_outside = any(
            a.is_co and a.room in by_id and by_id[a.room].type is not RoomType.BEDROOM
            for a in alarms
        )
        if not co_outside:
            add(
                Issue(
                    Severity.INFO,
                    "ALARM_CO",
                    f"The plan has bedrooms and an attached garage/shop "
                    f"('{garage.id}'), so a carbon-monoxide alarm is required "
                    "outside the sleeping areas (IRC R315), but none is declared.",
                    hint="Add `alarm co in <hall>` (or `alarm smoke_co`) outside the "
                    "bedrooms. INFO only — fuel-fired appliances aren't modelled, so "
                    "this is triggered by the attached garage/shop alone.",
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
    # An overhead garage door is vehicle access, not a building entrance: it
    # still makes its garage/shop reachable (the BFS below), but only a
    # people-door satisfies NO_ENTRY.
    people_doors = [d for d in plan.exterior_doors if not d.overhead]
    if not people_doors:
        # Don't also cry NO_ENTRY when entries exist but reference unknown rooms
        # (DOOR_REF already explains that); only when there are none at all.
        first = next(iter(plan.rooms)).id
        message = (
            "Plan has no entry door — an overhead door is vehicle access, "
            "not a way to enter on foot."
            if plan.exterior_doors
            else "Plan has no exterior door — no way to enter the building."
        )
        add(
            Issue(
                Severity.ERROR,
                "NO_ENTRY",
                message,
                hint=f"Add an entrance on an exterior wall, e.g. "
                f"`entry {first} south width 3 offset 4`.",
            )
        )
    if not entries:
        return

    reached: set[str] = set()
    queue: deque[str] = deque(entries)
    while queue:
        cur = queue.popleft()
        if cur in reached:
            continue
        reached.add(cur)
        queue.extend(n for n in adjacency[cur] if n not in reached)

    # O(1) id lookup instead of ``plan.room``'s linear scan — this runs once per
    # unreachable room, so the scan would be O(n²) on a large disconnected plan.
    index = room_index(plan)
    for rid in sorted(interior_rooms - reached):
        idx = index.first_index(rid)
        room = plan.rooms[idx] if idx is not None else None
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


def _dq_kitchen_flow(plan: Barndominium, graph, by_id, add) -> None:
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


def _dq_bed_privacy(plan: Barndominium, graph, by_id, add) -> None:
    # 2. Bedroom privacy: a bedroom shouldn't open straight onto a public room.
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            # A public room in the bedroom's OWN declared suite (a sitting area,
            # say) isn't a privacy leak — the door is inside the suite. Only
            # flag public neighbours outside it; with no suite declared the
            # `_same_suite` filter is a no-op, so behaviour is unchanged.
            public_nb = [
                n
                # sorted() so the named neighbour is stable — a set's iteration
                # order varies with PYTHONHASHSEED and must never leak into text.
                for n in sorted(graph.get(room.id, ()))
                if n in by_id and by_id[n].type in PUBLIC_TYPES
                and not _same_suite(plan, room.id, n)
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


def _dq_bath_distance(plan: Barndominium, graph, by_id, add) -> None:
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


def _dq_private_passthrough(plan: Barndominium, graph, by_id, add) -> None:
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
                # sorted() so the named gateway room is stable across
                # PYTHONHASHSEED (a set's iteration order otherwise leaks in).
                gate = next(
                    (n for n in sorted(graph.get(rid, ())) if n in gate_ids), None
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


def _dq_entry_private(plan: Barndominium, graph, by_id, add) -> None:
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
        elif rt is RoomType.BEDROOM and not _sole_bedroom_suite(plan, d.room, by_id):
            # An entry into a bedroom is normally the "maybe a patio door" info.
            # A bedroom that is the ONLY bedroom of a declared suite is the
            # primary suite, where a private patio/deck door is expected — that
            # declaration confirms the benign reading and the info is
            # suppressed. A shared multi-bed suite doesn't (an entry straight
            # into a kids' room is exactly the concern); undeclared → fires.
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


def _dq_wet_group(plan: Barndominium, graph, by_id, add) -> None:
    # 6. Plumbing economy: wet rooms (bath/kitchen/laundry/utility) are cheaper to
    #    run when they share a wall. If there are 3+ but none abut another wet
    #    room, the supply/waste runs are needlessly spread out. A declared
    #    plumbing wall (`wall a - b plumbing`) that a wet room really backs onto
    #    also satisfies this — the wet wall exists, it's just shared with a dry
    #    room (the supply/waste stack lives in that declared 2x6).
    wet = [r for r in plan.rooms if r.type in WET_TYPES]
    if len(wet) >= 3:
        grouped = any(
            by_id[n].type in WET_TYPES
            for r in wet
            for n in geometric_neighbors(plan, r.id)
            if n in by_id
        )
        if not grouped:
            # A declared plumbing wall counts only when it matches reality: the
            # pair really shares a wall AND a wet room backs onto it. (A bogus
            # declaration is WALL_NOADJ / WALL_UNUSED's job.)
            for ws in getattr(plan, "wall_specs", None) or []:
                if "plumbing" not in ws.attributes:
                    continue
                a, b = by_id.get(ws.room_a), by_id.get(ws.room_b)
                if a is None or b is None or a.id == b.id:
                    continue
                if shared_edge(a, b) is None:
                    continue
                if a.type in WET_TYPES or b.type in WET_TYPES:
                    grouped = True
                    break
        if not grouped:
            add(
                Issue(
                    Severity.INFO,
                    "WET_GROUP",
                    f"The {len(wet)} wet rooms (bath/kitchen/laundry) don't share any "
                    "walls; scattered plumbing means longer supply and waste runs.",
                    hint="Group two or more wet rooms back-to-back on a shared wall "
                    "(a 'wet wall') to cut plumbing cost — e.g. site a bath against "
                    "the kitchen or laundry — or declare the wall the fixtures back "
                    "onto: `wall <bath> - <neighbour> plumbing`.",
                )
            )


def _dq_no_closet(plan: Barndominium, graph, by_id, add) -> None:
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


def _dq_master_ensuite(plan: Barndominium, graph, by_id, add) -> None:
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
        # to flip a false positive). The largest just names where to add one. A
        # declared suite relaxes the strictly-private rule to "reachable through
        # the suite" (bed → wic → bath), but the bath must still be reachable —
        # a declaration alone doesn't conjure an ensuite across the plan.
        if any(
            _bedroom_ensuite(b.id)
            or _suite_ensuite(plan, b.id, by_id, graph)
            for b in beds
        ):
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


def _dq_bed_sound(plan: Barndominium, graph, by_id, add) -> None:
    # 8b. Acoustic buffer: two bedrooms that share a wall pass sound straight
    #     between them. The idiom is to stack each bedroom's closet on that wall
    #     (back-to-back), so the closets buffer the sleeping rooms — once buffered
    #     the bedrooms no longer share a wall and this clears.
    beds = [r for r in plan.rooms if r.type is RoomType.BEDROOM]
    for i, ba in enumerate(beds):
        for bb in beds[i + 1 :]:
            # Two bedrooms declared in one `suite` (a bunk room, a nursery off
            # the master) are *meant* to adjoin — the acoustic separation is
            # intentional, so a declared shared suite silences the buffer nudge
            # ONLY when that suite's bedrooms are exactly this pair: one giant
            # all-bedroom "suite" must not mute the check plan-wide. No suite
            # declared → unchanged.
            if _suite_pair_intentional(plan, ba.id, bb.id, by_id):
                continue
            edge = shared_edge(ba, bb)
            if edge is not None and edge.length + EPSILON >= MIN_SOUND_BUFFER_WALL:
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


def _dq_closet_shape(plan: Barndominium, graph, by_id, add) -> None:
    # 8c. Walk-in vs long, skinny closet: a closet with the floor area for a
    #     walk-in but shaped as a narrow strip wastes that floor. Small reach-ins
    #     (under the walk-in area) and wide/shallow closets (under the aspect
    #     bar) are fine and exempt.
    for room in plan.rooms:
        if room.type is RoomType.CLOSET:
            short = room.min_dimension
            long = max(room.width, room.length)
            if (
                short > EPSILON
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


def _dq_hall_tight(plan: Barndominium, graph, by_id, add, profile: Profile = DEFAULT) -> None:
    # 8d. Comfort width: a hall at the code minimum passes but feels tight for two
    #     people or moving furniture; the profile's comfort width is the target.
    #     A profile whose comfort width equals its hard minimum disables the nudge.
    hard = profile.min_hallway_width
    comfort = profile.comfort_hallway_width
    for room in plan.rooms:
        if (
            room.type is RoomType.HALLWAY
            and hard <= room.min_dimension < comfort - 1e-9
        ):
            add(
                Issue(
                    Severity.INFO,
                    "HALL_TIGHT",
                    f"Hallway '{room.id}' is {_f(room.min_dimension)} ft wide — legal "
                    f"(>= {hard:g} ft) but tight; {comfort:g} "
                    "ft is comfortable for two people and moving furniture"
                    + _profile_tag(
                        profile, "comfort_hallway_width",
                        f"{COMFORT_HALLWAY_WIDTH:g} ft"
                    )
                    + ".",
                    room=room.id,
                    hint=f"Widen it to >= {comfort:g} ft.",
                )
            )


def _dq_no_back_door(plan: Barndominium, graph, by_id, add) -> None:
    # 8e. Front *and* back door: a home wants a second exterior door (a back/side
    #     door off the kitchen, mudroom or laundry) — for daily flow and a second
    #     way out. Garage/porch doors don't count as the house's back door, and
    #     neither does an overhead garage door (vehicle access, wherever it is).
    people_doors = [
        d
        for d in plan.exterior_doors
        if not d.overhead
        and d.room in by_id
        and by_id[d.room].type not in (RoomType.GARAGE, RoomType.PORCH)
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


def _dq_bath_oversize(plan: Barndominium, graph, by_id, add) -> None:
    # 8f. Ensuite proportion: a private bath shouldn't be larger than the bedroom
    #     it serves — that's a sign the suite is mis-proportioned. Only judged for
    #     a true ensuite (a bath reached only through this bedroom, closets aside).
    for bed in (r for r in plan.rooms if r.type is RoomType.BEDROOM):
        # sorted() so multiple ensuites off one bedroom emit in a stable order
        # (set iteration otherwise varies the issue order with PYTHONHASHSEED).
        for n in sorted(graph.get(bed.id, ())):
            bath = by_id.get(n)
            if bath is None or bath.type is not RoomType.BATHROOM:
                continue
            private = all(
                m == bed.id or (m in by_id and by_id[m].type is RoomType.CLOSET)
                for m in graph.get(bath.id, ())
            )
            if private and bath.area > bed.area + EPSILON:
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


def _dq_stair_blocks_door(plan: Barndominium, graph, by_id, add) -> None:
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
        for xd in plan.exterior_doors:
            r = by_id.get(xd.room)
            if r is None or r.level not in levels:
                continue
            zx1, zy1, zx2, zy2 = _ext_door_zone(r, xd, DOOR_CLEARANCE_DEPTH)
            key = f"ext:{xd.room}:{xd.wall.value}"
            if key not in blocked and s.overlaps_rect(zx1, zy1, zx2, zy2):
                blocked.add(key)
                add(
                    Issue(
                        Severity.WARNING,
                        "STAIR_BLOCKS_DOOR",
                        f"Stair '{s.id}' intrudes on the clear floor in front of the "
                        f"exterior door into '{xd.room}', blocking it.",
                        room=s.id,
                        hint="Shift the stair off the doorway (place it along a wall), "
                        "or move the entry so its approach is clear.",
                    )
                )


def _dq_stair_wall(plan: Barndominium, graph, by_id, add) -> None:
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


def _dq_door_centered(plan: Barndominium, graph, by_id, add) -> None:
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
                    "offset 0.5` (~6 in off the wall for trim) — so one side keeps a "
                    "full wall run for furniture.",
                    **_door_loc(d),
                )
            )


def _dq_door_swing_clash(plan: Barndominium, graph, by_id, add) -> None:
    # 8k. Door swings shouldn't overlap: two leaves sweeping into the same space
    #     foul each other. Build each swing's swept quarter-disc (matching the
    #     renderer) and test for overlap.
    swings = []
    for d in plan.interior_doors:
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None or a.level != b.level:
            continue
        edge = shared_edge(a, b)
        if edge is None:
            continue
        region = _interior_swing_region(plan, d, a, b, edge)
        if region is not None:
            swings.append((f"'{d.room_a}'-'{d.room_b}'", region, d.line, d.col, d.end_col))
    for xd in plan.exterior_doors:
        r = by_id.get(xd.room)
        if r is None or xd.overhead:  # an overhead door rides up its tracks — no swing
            continue
        swings.append((f"the {xd.wall.value} entry to '{xd.room}'",
                       _exterior_swing_region(plan, r, xd),
                       xd.line, xd.col, xd.end_col))
    for i in range(len(swings)):
        for j in range(i + 1, len(swings)):
            if _convex_overlap(swings[i][1], swings[j][1]):
                lbl_i, _, line, col, end_col = swings[i]
                lbl_j = swings[j][0]
                add(
                    Issue(
                        Severity.INFO,
                        "DOOR_SWING_CLASH",
                        f"The swings of {lbl_i} and {lbl_j} overlap — the leaves "
                        "would foul each other.",
                        line=line, col=col, end_col=end_col,
                        hint="Move one door along its wall, narrow it, swing it the "
                        "other way (`into <room>` / `hinge near|far`), or make one a "
                        "pocket/sliding door so the leaves don't collide.",
                    )
                )


def _dq_door_swing_direction(plan: Barndominium, graph, by_id, add) -> None:
    # 8l. Which way a leaf swings, and whether it crowds the room:
    #       * a bedroom/bath door should open *into* the private room it serves,
    #         not out into circulation (measured against the rendered swing);
    #       * an inward exterior leaf can't clear a too-shallow room — the case
    #         for an out-swing door;
    #       * a door swing eats the wall a wet room / kitchen needs for a fixture
    #         the room otherwise has the capacity to hold (DOOR_HITS_FIXTURE).
    for d in plan.interior_doors:
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None or a.level != b.level:
            continue
        edge = shared_edge(a, b)
        if edge is None:
            continue
        region = _interior_swing_region(plan, d, a, b, edge)
        if region is None:  # pocket/sliding/cased — no leaf to place
            continue
        target = _region_room(region, a, b, edge)
        if target is None:
            continue

        # A bedroom/bath door should open *into* the private room it serves.
        pref = _preferred_swing_room(a, b)
        if pref is None or target is pref:
            continue
        word = pref.type.value.replace("_", "-")
        if d.swing_into is None:
            add(
                Issue(
                    Severity.INFO,
                    "DOOR_SWING_UNSET",
                    f"The {word} door '{a.id}'-'{b.id}' has no swing direction set and "
                    f"defaults into '{target.id}'; a {word} door should open into the room.",
                    room=pref.id,
                    hint=f"Pin it with `into {pref.id}`.",
                    **_door_loc(d),
                )
            )
        else:
            add(
                Issue(
                    Severity.INFO,
                    "DOOR_SWING_PRIVACY",
                    f"The {word} door '{a.id}'-'{b.id}' swings into '{target.id}'; a "
                    f"{word} door should open into the room so the leaf screens the "
                    "view and folds against a wall.",
                    room=pref.id,
                    hint=f"Swing it into '{pref.id}' (`into {pref.id}`).",
                    **_door_loc(d),
                )
            )

    for xd in plan.exterior_doors:
        if xd.overhead:  # rides up its tracks — no swing
            continue
        r = by_id.get(xd.room)
        if r is None:
            continue
        leaf = xd.width / 2.0 if xd.kind in DOUBLE_LEAF_KINDS else xd.width
        depth = r.width if xd.wall in (Direction.WEST, Direction.EAST) else r.length
        if depth + EPSILON < leaf:
            add(
                Issue(
                    Severity.WARNING,
                    "DOOR_SWING_INWARD",
                    f"The exterior door on '{r.id}' swings inward, but the room is only "
                    f"{_f(depth)} ft deep — a {leaf * 12:.0f} in leaf can't fully open.",
                    room=r.id,
                    hint="Deepen the room, narrow the door, or use an out-swing or "
                    "sliding door.",
                    **_door_loc(xd),
                )
            )

    # A door swing that crowds a fixture out of a room the room could otherwise
    # hold: the door-aware placer drops a fixture the door-blind one keeps. This
    # is the gap BATH_CLEARANCE (capacity only, door-blind) can't see.
    from .fixtures import fixtures_for, plan_room_fixtures  # lazy: circular import

    for room in plan.rooms:
        if not fixtures_for(room.type):
            continue
        blind = plan_room_fixtures(plan, room, avoid_doors=False)
        clear = plan_room_fixtures(plan, room)
        if len(clear) >= len(blind):
            continue
        dropped = [f.kind for f in blind[len(clear):]]
        names = " and ".join(dropped)
        add(
            Issue(
                Severity.WARNING,
                "DOOR_HITS_FIXTURE",
                f"A door swing leaves '{room.id}' no clear wall for its {names} — "
                "the room has the space, but not once the door's arc is kept clear.",
                room=room.id,
                hint="Move the door along the wall, swing it the other way "
                "(`into <room>` / `hinge near|far`), make it a pocket/sliding door, "
                "or enlarge the room.",
            )
        )


def _dq_envelope_module(plan: Barndominium, graph, by_id, add) -> None:
    # 8m. Material efficiency: exterior dimensions that land on a build module cut
    #     less sheet/board waste. Flag envelope and wing measurements off the module.
    off = []
    for label, value in (
        ("envelope width", plan.envelope_width),
        ("envelope length", plan.envelope_length),
    ):
        if _off_module(value):
            off.append(f"{label} {_f(value)}")
    for i, wing in enumerate(plan.wings):
        for label, value in (("width", wing.width), ("length", wing.length)):
            if _off_module(value):
                off.append(f"wing {i + 1} {label} {_f(value)}")
    if off:
        add(
            Issue(
                Severity.INFO,
                "ENVELOPE_MODULE",
                f"Exterior dimensions off the {_f(BUILD_MODULE)} ft build module: "
                f"{', '.join(off)}.",
                hint=f"Round exterior measurements to a multiple of {_f(BUILD_MODULE)} ft "
                "so sheet goods and framing cut with less waste.",
            )
        )


def _dq_window_partition(plan: Barndominium, graph, by_id, add) -> None:
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


def _dq_room_proportion(plan: Barndominium, graph, by_id, add) -> None:
    # 8. Proportion: a habitable room shaped like a bowling alley is hard to
    #    furnish. Hallways/closets are *meant* to be skinny — they're not habitable,
    #    so the HABITABLE_TYPES gate already excludes them.
    for room in plan.rooms:
        if room.type in HABITABLE_TYPES:
            short = room.min_dimension
            long = max(room.width, room.length)
            if short > EPSILON and long / short > MAX_ROOM_ASPECT:
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


def _dq_garage_bedroom(plan: Barndominium, graph, by_id, add) -> None:
    # 9. Garage/shop → sleeping room. IRC R302.5.1: the opening shall not open
    #    into a room used for sleeping. This is code-grounded, so it's a WARNING.
    garages = [r for r in plan.rooms if r.type in GARAGE_TYPES]
    for g in garages:
        label = g.type.value
        # sorted() so multiple bedrooms off one garage emit in a stable order
        # (set iteration otherwise varies the issue order with PYTHONHASHSEED).
        for n in sorted(graph.get(g.id, ())):
            if n in by_id and by_id[n].type is RoomType.BEDROOM:
                add(
                    Issue(
                        Severity.WARNING,
                        "GARAGE_BEDROOM",
                        f"{label.capitalize()} '{g.id}' opens directly into the "
                        f"bedroom '{n}'; a {label} must not open into a sleeping "
                        "room (IRC R302.5.1).",
                        room=n,
                        hint=f"Buffer it with a mudroom or hall — connect the {label} "
                        f"there instead, e.g. `door {g.id} - <mudroom_or_hall>`.",
                    )
                )


def _dq_garage_no_entry(plan: Barndominium, graph, by_id, add) -> None:
    # 10. Garage with no interior people-door into the house. A vehicle `entry`
    #     satisfies reachability (NO_ACCESS), so this gap slips through: you'd have
    #     to go outside to get in. Only nudge when it actually abuts the house.
    house_types = {t for t in RoomType if t not in GARAGE_TYPES and t is not RoomType.PORCH}
    garages = [r for r in plan.rooms if r.type in GARAGE_TYPES]
    for g in garages:
        label = g.type.value
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
                    f"{label.capitalize()} '{g.id}' has no interior door into the "
                    "house — you'd have to go outside to get in.",
                    room=g.id,
                    hint=f"Add a people-door from the {label} into a mudroom, hall or "
                    f"living space, e.g. `door {g.id} - <adjacent_room>`.",
                )
            )


def _dq_garage_separation(plan: Barndominium, graph, by_id, add) -> None:
    # 10b. IRC R302.6: the wall between a private garage and the dwelling — and any
    #      ceiling under habitable space above the garage — must be a fire
    #      separation (≥ ½ in gypsum; ⅝ in Type X where habitable space is above).
    #      The DSL can't model gypsum layers, so this is a reminder (INFO) fired by
    #      the geometry that triggers the requirement, like BATH_VENT. A declared
    #      rated wall (`wall garage - x rated`) records that the common wall IS
    #      detailed as a separation, so that neighbour stops firing — the reminder
    #      becomes verifiable. A ceiling can't be declared, so habitable space
    #      above the garage keeps reminding regardless.
    rated_pairs = {
        frozenset((ws.room_a, ws.room_b))
        for ws in getattr(plan, "wall_specs", None) or []
        if "rated" in ws.attributes
    }
    for g in plan.rooms:
        if g.type not in GARAGE_TYPES:
            continue
        label = g.type.value
        shares = sorted(
            n
            for n in geometric_neighbors(plan, g.id)
            if n in by_id
            and by_id[n].type in INTERIOR_TYPES
            and frozenset((g.id, n)) not in rated_pairs
        )
        above = sorted(
            r.id
            for r in plan.rooms
            if r.level > g.level and r.type in HABITABLE_TYPES and g.overlaps(r) > 0
        )
        if not (shares or above):
            continue
        if above:
            msg = (
                f"{label.capitalize()} '{g.id}' has habitable space above it "
                f"({', '.join(above)}); the {label} ceiling needs ⅝ in Type X gypsum "
                "and the common wall a fire separation (IRC R302.6)."
            )
        else:
            msg = (
                f"{label.capitalize()} '{g.id}' shares a wall with conditioned space "
                f"({', '.join(shares)}); that common wall needs a gypsum fire "
                "separation (IRC R302.6)."
            )
        hint = (
            "Detail the common wall/ceiling as a fire separation "
            "(≥ ½ in gypsum; ⅝ in Type X under habitable space)."
        )
        if shares:
            hint += (
                f" Once detailed, declare it — `wall {g.id} - {shares[0]} rated` — "
                "so the compiler can verify it instead of reminding."
            )
        add(
            Issue(
                Severity.INFO,
                "GARAGE_SEPARATION",
                msg,
                room=g.id,
                hint=hint,
            )
        )


def _dq_garage_door(plan: Barndominium, graph, by_id, add) -> None:
    # 10c. IRC R302.5.1: a door between a private garage and the dwelling must be
    #      self-closing and 20-minute fire-rated (or a solid-core/solid-wood door
    #      at least 1-3/8 in thick). A door into a sleeping room is barred outright
    #      (GARAGE_BEDROOM), so this reminder covers the other garage-to-dwelling
    #      doors. It anchors on the actual `door` statement — the opening that has
    #      to carry the rated leaf — rather than on the garage room, so the caret
    #      lands on the line the author edits.
    seen: set[tuple[str, str]] = set()
    for d in plan.interior_doors:
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None or (a.type in GARAGE_TYPES) == (b.type in GARAGE_TYPES):
            continue  # need exactly one side to be a garage/shop
        gar, other = (a, b) if a.type in GARAGE_TYPES else (b, a)
        if other.type in GARAGE_TYPES or other.type in (RoomType.PORCH, RoomType.BEDROOM):
            continue  # bedroom is the worse GARAGE_BEDROOM warning's job
        if (gar.id, other.id) in seen:
            continue
        seen.add((gar.id, other.id))
        add(
            Issue(
                Severity.INFO,
                "GARAGE_DOOR",
                f"The door from {gar.type.value} '{gar.id}' into '{other.id}' must be "
                "a self-closing, 20-minute fire-rated (or solid-core / solid-wood, at "
                "least 1-3/8 in thick) door (IRC R302.5.1).",
                room=gar.id,
                hint="Spec a self-closing 20-min / >= 1-3/8 in solid-core door on the "
                "garage-to-dwelling opening.",
                line=getattr(d, "line", None),
                col=getattr(d, "col", None),
                end_col=getattr(d, "end_col", None),
            )
        )


def _dq_closet_door_swing(plan: Barndominium, graph, by_id, add) -> None:
    # 10d. A swing door into a shallow closet: the leaf (as wide as the door) can't
    #      fully open because the closet isn't as deep as the door is wide, so the
    #      swing fills the closet. A bypass/sliding or bifold door clears the space.
    #      Doors here are author-declared (kinds aren't seeded), so this is an INFO
    #      nudge — not a re-seed. Only a leaf that actually swings *into* the closet
    #      (or an unspecified side the renderer might pick) is judged; one explicitly
    #      swinging into the other room doesn't fill the closet.
    from .geometry import shared_edge

    for d in plan.interior_doors:
        if getattr(d, "kind", "swing") != "swing":
            continue  # a sliding/pocket/bifold/cased leaf already clears the closet
        a, b = by_id.get(d.room_a), by_id.get(d.room_b)
        if a is None or b is None:
            continue
        if (a.type is RoomType.CLOSET) == (b.type is RoomType.CLOSET):
            continue  # need exactly one closet side
        closet, other = (a, b) if a.type is RoomType.CLOSET else (b, a)
        if d.swing_into is not None and d.swing_into != closet.id:
            continue  # swings into the room, not the closet — the closet depth is moot
        edge = shared_edge(closet, other)
        if edge is None:
            continue
        leaf = min(d.width, edge.length)
        # Closet depth = the closet's extent perpendicular to the shared wall.
        depth = closet.width if edge.orientation == "v" else closet.length
        if depth + EPSILON < leaf:
            add(
                Issue(
                    Severity.INFO,
                    "CLOSET_DOOR_SWING",
                    f"The swing door into closet '{closet.id}' is {_f(leaf)} ft wide "
                    f"but the closet is only {_f(depth)} ft deep, so the leaf can't "
                    "fully open inside it.",
                    room=closet.id,
                    line=d.line,
                    col=d.col,
                    end_col=d.end_col,
                    hint="Make it a bypass/sliding or bifold door so the leaf doesn't "
                    f"fill the closet, e.g. `door {d.room_a} - {d.room_b} sliding "
                    f"width {_f(d.width)}`.",
                )
            )


def _dq_hall_deadend(plan: Barndominium, graph, by_id, add) -> None:
    # 11. A hallway exists to *distribute* circulation. One that opens onto a
    #     single room (or none) is just overhead. Exempt a hall that carries an
    #     exterior entry — a foyer/vestibule is legitimately a one-room hall.
    hall_entries = {d.room for d in plan.exterior_doors}
    for room in plan.rooms:
        if room.type is not RoomType.HALLWAY:
            continue
        served = len(graph.get(room.id, ()))
        if served <= 1:
            # A 1-room hall isn't a distributing spine: flag it as overhead (unless
            # it's a foyer carrying the entry), and never stub-check it.
            if room.id not in hall_entries:
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
        if axis_hi - axis_lo <= room.min_dimension + EPSILON:
            continue  # roughly square (a foyer/landing), not a corridor
        # Measure from each *doorway*, not the room's whole abutting wall: a hall
        # running past its last door reads as a dead end even if a room's wall
        # lines the rest of it. So the served span is between the first and last
        # doorway along the hall.
        marks: list[tuple[float, float]] = []
        for d in plan.interior_doors:
            if room.id not in (d.room_a, d.room_b):
                continue
            other = d.room_b if d.room_a == room.id else d.room_a
            nb = by_id.get(other)
            edge = shared_edge(room, nb) if nb else None
            if edge is None:
                continue
            lo, hi = _door_interval(edge, d)
            along_axis = (edge.orientation == "h") if long_x else (edge.orientation == "v")
            marks.append((lo, hi) if along_axis else (edge.pos, edge.pos))
        for xd in plan.exterior_doors:
            if xd.room != room.id:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, xd.wall, xd.offset, xd.width)
            ns = xd.wall in (Direction.NORTH, Direction.SOUTH)
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
                    hint="Put the end room's door at the hall end (extend that room to "
                    "cap the hall), or trim the hall back to its last doorway.",
                )
            )


#: The design-quality checks, run in order. Each is a standalone
#: ``(plan, graph, by_id, add)`` function so it can be unit-tested in
#: isolation; the driver below builds the shared derived state once.
_DESIGN_QUALITY_CHECKS = (
    _dq_kitchen_flow,
    _dq_bed_privacy,
    _dq_bath_distance,
    _dq_private_passthrough,
    _dq_entry_private,
    _dq_wet_group,
    _dq_no_closet,
    _dq_master_ensuite,
    _dq_bed_sound,
    _dq_closet_shape,
    _dq_hall_tight,
    _dq_no_back_door,
    _dq_bath_oversize,
    _dq_stair_blocks_door,
    _dq_stair_wall,
    _dq_door_centered,
    _dq_door_swing_clash,
    _dq_door_swing_direction,
    _dq_envelope_module,
    _dq_window_partition,
    _dq_room_proportion,
    _dq_garage_bedroom,
    _dq_garage_no_entry,
    _dq_garage_separation,
    _dq_garage_door,
    _dq_closet_door_swing,
    _dq_hall_deadend,
)


def _validate_design_quality(plan: Barndominium, add, profile: Profile = DEFAULT) -> None:
    """Soft, advisory checks (mostly INFO) that nudge toward a livable layout.

    These never block compilation -- they flow through the *same* diagnostic
    channel as code errors so the author (or the agent) gets quality guidance,
    not just code-compliance. They mirror what a reviewing architect notices:
    open-concept flow, bedroom privacy, and bath proximity. Each individual
    check lives in its own ``_dq_*`` function (see ``_DESIGN_QUALITY_CHECKS``).
    """
    graph = _door_graph(plan)
    by_id = {r.id: r for r in plan.rooms}
    for check in _DESIGN_QUALITY_CHECKS:
        # Only the hall-comfort nudge reads jurisdiction thresholds; every other
        # check keeps the plain ``(plan, graph, by_id, add)`` shape (and stays
        # directly unit-testable with those four args).
        if check is _dq_hall_tight:
            _dq_hall_tight(plan, graph, by_id, add, profile)
        else:
            check(plan, graph, by_id, add)


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
        if math.isfinite(interior) and interior + EPSILON < spec.min_area:
            mismatches.append(
                f"{_f(spec.min_area)} sq ft declared but {interior:.0f} placed"
            )
    if spec.min_storage is not None:
        storage = sum(r.area for r in plan.rooms if r.type in STORAGE_TYPES)
        if storage + EPSILON < spec.min_storage:
            mismatches.append(
                f"{_f(spec.min_storage)} sq ft of storage declared but "
                f"{storage:.0f} placed"
            )
    if not mismatches:
        return
    loc: dict = {}
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


def _suggest_anchor(a: Room, b: Room) -> str:
    """The relative-placement direction that would abut ``b`` against ``a``,
    picked from where ``b`` already sits — so the hint moves it the short way."""
    ax, ay = a.center
    bx, by = b.center
    if abs(bx - ax) >= abs(by - ay):
        return "east-of" if bx >= ax else "west-of"
    return "north-of" if by >= ay else "south-of"


def _validate_requirements(plan: Barndominium, add) -> None:
    """Check declared ``require`` statements against the compiled plan.

    The ``program`` pattern extended to space: declared intent, mechanically
    checked. Requirements never block a compile — each unmet one is a
    ``REQUIRE_UNMET`` warning with a concrete fix — except a requirement naming
    an unknown room id, which is a ``REQUIRE_REF`` error like every dangling
    reference (a mistyped id would otherwise silently check nothing).

    Semantics (see :class:`~barndsl.elements.Requirement`):

    * ``adjacent`` is purely geometric — a positive-length shared wall on the
      same level (:func:`shared_edge`). A door or cased opening alone does NOT
      satisfy it: adjacency is the precondition a ``door`` needs, so the two
      checks agree rather than one excusing the other.
    * ``separate`` fails only on a shared wall; rooms on different levels never
      share a wall, so a cross-level pair is trivially separate.
    * ``exterior`` uses the same footprint-aware :func:`exterior_walls` the
      window/entry checks use, so seam walls between wings count as interior.
    * ``area`` checks the room's **nominal** area (the same figure
      ``program area`` uses), not the clear finish-face area.
    """
    room_ids = {r.id for r in plan.rooms}
    for req in plan.requirements:
        loc: dict = {}
        if req.line is not None:
            loc = {"line": req.line, "col": req.col, "end_col": req.end_col}
        missing = [
            rid for rid in (req.a, req.b) if rid is not None and rid not in room_ids
        ]
        if missing:
            for rid in missing:
                add(
                    Issue(
                        Severity.ERROR,
                        "REQUIRE_REF",
                        f"Requirement references unknown room '{rid}'.",
                        room=rid,
                        hint="Reference an existing room id, or declare the room.",
                        **loc,
                    )
                )
            continue
        a = plan.room(req.a)
        assert a is not None  # checked above
        if req.kind == "adjacent":
            assert req.b is not None  # two-room kinds always carry b
            b = plan.room(req.b)
            assert b is not None
            if shared_edge(a, b) is None:
                detail = (
                    f"they sit on different levels ({a.level} and {b.level})"
                    if a.level != b.level
                    else "they don't share a wall (a corner touch isn't enough)"
                )
                add(
                    Issue(
                        Severity.WARNING,
                        "REQUIRE_UNMET",
                        f"Required adjacency unmet: '{a.id}' and '{b.id}' — {detail}.",
                        room=a.id,
                        hint=f"Abut them along a wall — e.g. re-place '{b.id}' with a "
                        f"relative anchor: `room {b.id}: {b.type.value} "
                        f"{_suggest_anchor(a, b)} {a.id} size {_f(b.width)} x "
                        f"{_f(b.length)}`.",
                        **loc,
                    )
                )
        elif req.kind == "separate":
            assert req.b is not None  # two-room kinds always carry b
            b = plan.room(req.b)
            assert b is not None
            edge = shared_edge(a, b)  # None across levels: trivially separate
            if edge is not None:
                add(
                    Issue(
                        Severity.WARNING,
                        "REQUIRE_UNMET",
                        f"Required separation unmet: '{a.id}' and '{b.id}' share a "
                        f"{_f(edge.length)} ft wall.",
                        room=a.id,
                        hint=f"Reposition '{b.id}' so it doesn't touch '{a.id}', or "
                        "put a buffer room (hall, closet) between them.",
                        **loc,
                    )
                )
        elif req.kind == "exterior":
            ext = exterior_walls(plan, a)
            interior = ", ".join(
                w.value
                for w in (
                    Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST
                )
                if w not in ext
            )
            if req.wall is not None and req.wall not in ext:
                have = (
                    f"its exterior wall(s): {', '.join(w.value for w in ext)}"
                    if ext
                    else "it has no exterior wall at all"
                )
                add(
                    Issue(
                        Severity.WARNING,
                        "REQUIRE_UNMET",
                        f"Required exterior wall unmet: '{a.id}'s {req.wall.value} "
                        f"wall is interior (interior walls: {interior}).",
                        room=a.id,
                        hint=f"Move '{a.id}' so its {req.wall.value} wall lies on the "
                        f"footprint edge — {have}.",
                        **loc,
                    )
                )
            elif req.wall is None and not ext:
                add(
                    Issue(
                        Severity.WARNING,
                        "REQUIRE_UNMET",
                        f"Required exterior wall unmet: '{a.id}' has no exterior wall "
                        f"(interior walls: {interior}).",
                        room=a.id,
                        hint=f"Move '{a.id}' to the building perimeter so at least "
                        "one wall lies on the footprint edge.",
                        **loc,
                    )
                )
        elif req.kind == "area":
            assert req.min_area is not None  # the builder guarantees it
            if a.area + EPSILON < req.min_area:
                need_len = _suggest_int(req.min_area / max(a.width, EPSILON))
                sizing = (
                    f" — e.g. `size {_f(a.width)} x {need_len}`"
                    if need_len is not None
                    else ""
                )
                add(
                    Issue(
                        Severity.WARNING,
                        "REQUIRE_UNMET",
                        f"Required area unmet: '{a.id}' is {_f(a.area)} sq ft; the "
                        f"requirement is >= {_f(req.min_area)} sq ft.",
                        room=a.id,
                        hint=f"Enlarge '{a.id}' to at least {_f(req.min_area)} sq ft"
                        f"{sizing}.",
                        **loc,
                    )
                )


def _validate_walls(plan: Barndominium, add) -> None:
    """Check declared ``wall`` statements against the compiled plan.

    A wall spec is a claim about a *real* shared wall, so a spec naming an
    unknown room is a ``WALL_REF`` error and a pair that shares no wall is a
    ``WALL_NOADJ`` error (the declared wall doesn't exist — same geometry rule
    an interior ``door`` needs). Two advisory follow-ups: a ``plumbing`` wall
    that no wet room backs onto is a ``WALL_UNUSED`` info (the declaration
    matches nothing), and a ``bearing`` wall that runs parallel to the frame's
    bents can't split their span, so the frame can't use it as a post line —
    a ``WALL_BEARING_AXIS`` info rather than a silent no-op.
    """
    room_ids = {r.id for r in plan.rooms}
    for ws in getattr(plan, "wall_specs", None) or []:
        loc: dict = {}
        if ws.line is not None:
            loc = {"line": ws.line, "col": ws.col, "end_col": ws.end_col}
        missing = [rid for rid in (ws.room_a, ws.room_b) if rid not in room_ids]
        if missing:
            for rid in missing:
                add(
                    Issue(
                        Severity.ERROR,
                        "WALL_REF",
                        f"Wall statement references unknown room '{rid}'.",
                        room=rid,
                        hint="Reference an existing room id, or declare the room.",
                        **loc,
                    )
                )
            continue
        a, b = plan.room(ws.room_a), plan.room(ws.room_b)
        assert a is not None and b is not None  # checked above
        if a.id == b.id:
            add(
                Issue(
                    Severity.ERROR,
                    "WALL_NOADJ",
                    f"Wall statement names '{a.id}' twice — a wall stands between "
                    "two different rooms.",
                    room=a.id,
                    hint="Name the two rooms that flank the wall.",
                    **loc,
                )
            )
            continue
        edge = shared_edge(a, b)
        if edge is None:
            detail = (
                f"they sit on different levels ({a.level} and {b.level})"
                if a.level != b.level
                else "they don't share a wall (a corner touch isn't enough)"
            )
            add(
                Issue(
                    Severity.ERROR,
                    "WALL_NOADJ",
                    f"`wall {a.id} - {b.id}` declares a wall that doesn't exist — "
                    f"{detail}.",
                    room=a.id,
                    hint="A wall statement describes the shared wall between two "
                    "abutting rooms; reposition them to abut along an edge, or "
                    "drop the declaration.",
                    **loc,
                )
            )
            continue
        if "plumbing" in ws.attributes and not (
            a.type in WET_TYPES or b.type in WET_TYPES
        ):
            add(
                Issue(
                    Severity.INFO,
                    "WALL_UNUSED",
                    f"The declared plumbing wall between '{a.id}' and '{b.id}' "
                    "serves no wet room — neither side is a bath, kitchen, "
                    "laundry or utility.",
                    room=a.id,
                    hint="Put the wet wall where the fixtures back onto it, or "
                    "drop the `plumbing` attribute.",
                    **loc,
                )
            )
    # A bearing wall the frame can't use: it runs parallel to the bents' span,
    # so it can't split that span into shorter beams. Only meaningful once a
    # frame is requested (without one, the declaration just rides the exchange).
    if plan.frame_spec is not None:
        from .structure import bearing_wall_usage

        for ws, _edge, usable in bearing_wall_usage(plan):
            if usable:
                continue
            loc = {}
            if ws.line is not None:
                loc = {"line": ws.line, "col": ws.col, "end_col": ws.end_col}
            add(
                Issue(
                    Severity.INFO,
                    "WALL_BEARING_AXIS",
                    f"The declared bearing wall between '{ws.room_a}' and "
                    f"'{ws.room_b}' runs across the frame's span (parallel to the "
                    "bents), so it can't carry a post line — the frame ignored it.",
                    room=ws.room_a,
                    hint="A post line runs along the building's long axis; declare "
                    "a wall running that way as bearing, or leave the span to the "
                    "auto interior supports.",
                    **loc,
                )
            )


def _validate_suites_zones(plan: Barndominium, add) -> None:
    """Check declared ``suite`` / ``zone`` statements against the plan.

    Declared intent, like ``program`` / ``require`` / ``wall``: the grouping
    isn't geometry, it's the author naming which rooms belong together, so the
    checks here are all reference/consistency guards plus one design nudge.

    * ``SUITE_REF`` / ``ZONE_REF`` (error) — a member names a room id (suite) or
      room/suite id (zone) that doesn't exist; a typo would otherwise group
      nothing.
    * ``SUITE_OVERLAP`` / ``ZONE_OVERLAP`` (warning) — a room declared in two
      suites, or a room in two zones (directly or via a suite). Overlapping
      groups are almost always an authoring slip; a warning (not an error)
      because it never makes the plan unbuildable, matching the intent-check
      family (PROGRAM_MISMATCH / REQUIRE_UNMET).
    * ``ZONE_CROSS`` (info) — a clearly public room (living/kitchen/dining)
      whose only zone otherwise holds just private rooms (bed/bath), or the
      reverse. A design nudge (a public room stranded in the private band), kept
      conservative: it needs the room in exactly one zone, that zone to contain
      at least one opposite-band room, and none of the same band — so a mixed
      (open-concept) zone or a plan without zones never fires it.
    """
    suites = getattr(plan, "suites", None) or []
    zones = getattr(plan, "zones", None) or []
    if not suites and not zones:
        return
    by_id = {r.id: r for r in plan.rooms}
    room_ids = set(by_id)
    suite_ids = {s.id for s in suites}

    # SUITE_SHADOW: a suite named like a room is ambiguous as a zone member —
    # resolution picks the room, so the suite silently never expands.
    for s in suites:
        if s.id in room_ids:
            add(
                Issue(
                    Severity.WARNING,
                    "SUITE_SHADOW",
                    f"Suite '{s.id}' has the same id as a room; a zone member "
                    f"named '{s.id}' resolves to the ROOM, not the suite.",
                    room=s.id,
                    hint="Rename the suite so zone members can reference it "
                    "unambiguously.",
                    **_spec_loc(s),
                )
            )

    # SUITE_REF: a suite member must be a real room.
    for s in suites:
        loc = _spec_loc(s)
        for m in s.members:
            if m not in room_ids:
                add(
                    Issue(
                        Severity.ERROR,
                        "SUITE_REF",
                        f"Suite '{s.id}' references unknown room '{m}'.",
                        room=m,
                        hint="Reference an existing room id, or declare the room.",
                        **loc,
                    )
                )

    # SUITE_OVERLAP: a room may live in only one suite.
    first_suite: dict[str, str] = {}
    warned_suite: set[str] = set()
    for s in suites:
        for m in s.members:
            if m not in room_ids:
                continue
            if m in first_suite and m not in warned_suite:
                add(
                    Issue(
                        Severity.WARNING,
                        "SUITE_OVERLAP",
                        f"Room '{m}' is a member of more than one suite "
                        f"('{first_suite[m]}' and '{s.id}').",
                        room=m,
                        hint="A room belongs to one suite; drop it from all but one.",
                        **_spec_loc(s),
                    )
                )
                warned_suite.add(m)
            else:
                first_suite.setdefault(m, s.id)

    # ZONE_REF: a zone member must be a real room or a declared suite.
    for z in zones:
        loc = _spec_loc(z)
        for m in z.members:
            if m not in room_ids and m not in suite_ids:
                add(
                    Issue(
                        Severity.ERROR,
                        "ZONE_REF",
                        f"Zone '{z.id}' references unknown room or suite '{m}'.",
                        room=m,
                        hint="Reference an existing room id or a declared suite id.",
                        **loc,
                    )
                )

    # ZONE_OVERLAP: a room may live in only one zone (counting suite expansion).
    zrooms = _zone_room_sets(plan, by_id)
    room_zones: dict[str, list[str]] = {}
    for z in zones:
        for rid in zrooms.get(z.id, ()):  # deterministic order via plan rooms below
            room_zones.setdefault(rid, [])
            if z.id not in room_zones[rid]:
                room_zones[rid].append(z.id)
    zone_loc = {z.id: _spec_loc(z) for z in zones}
    for r in plan.rooms:  # plan order → deterministic diagnostics
        zs = room_zones.get(r.id)
        if zs and len(zs) >= 2:
            zlist = ", ".join("'" + z + "'" for z in zs)
            add(
                Issue(
                    Severity.WARNING,
                    "ZONE_OVERLAP",
                    f"Room '{r.id}' is in more than one zone ({zlist}).",
                    room=r.id,
                    hint="A room belongs to one zone; drop it from all but one "
                    "(a room inside a suite is already in that suite's zone).",
                    **zone_loc[zs[-1]],
                )
            )

    # ZONE_CROSS: a public room stranded in the private band, or the reverse.
    for r in plan.rooms:
        if r.type not in ZONE_PUBLIC_TYPES and r.type not in ZONE_PRIVATE_TYPES:
            continue
        zs = room_zones.get(r.id)
        if not zs or len(zs) != 1:
            continue  # only reason about a room with a single, unambiguous zone
        others = [by_id[o] for o in zrooms[zs[0]] if o != r.id and o in by_id]
        other_public = any(o.type in ZONE_PUBLIC_TYPES for o in others)
        other_private = any(o.type in ZONE_PRIVATE_TYPES for o in others)
        is_public = r.type in ZONE_PUBLIC_TYPES
        # Public room whose zone is otherwise all-private (and vice versa).
        if is_public and other_private and not other_public:
            band, kind = "private", "public"
        elif not is_public and other_public and not other_private:
            band, kind = "public", "private"
        else:
            continue
        add(
            Issue(
                Severity.INFO,
                "ZONE_CROSS",
                f"{kind.capitalize()} room '{r.id}' ({r.type.value}) sits in zone "
                f"'{zs[0]}', which otherwise holds only {band} rooms — a {kind} "
                f"room in the {band} band.",
                room=r.id,
                hint=f"Move '{r.id}' to a {kind} zone, or regroup the zones so the "
                "band is consistent.",
                **zone_loc[zs[0]],
            )
        )


def _spec_loc(spec) -> dict:
    """Source-location kwargs for a suite/zone (or any spec) statement."""
    if getattr(spec, "line", None) is None:
        return {}
    return {"line": spec.line, "col": spec.col, "end_col": spec.end_col}


#: An interior support post sitting at least this far (ft) from every wall of the
#: room it lands in is out in the open floor — awkward to live around.
POST_CLEAR_MARGIN = 1.5
#: A bay spacing wider than this (ft on centre) is heavy for ordinary residential
#: post-frame members — an info nudge, not a hard limit.
COMFORT_BAY = 12.0


def _validate_structure(plan: Barndominium, add) -> None:
    """Nudge on an auto-placed structural frame (``frame`` directive).

    Two info-level checks, never blocking: an interior support post stranded in a
    room's open floor (``POST_OBSTRUCT``), and a bay spacing heavier than typical
    residential post-frame (``BAY_WIDE``). The frame is a layout aid, so these
    point at livability/economy, not engineered adequacy.
    """
    spec = plan.frame_spec
    if spec is None:
        return

    loc: dict = {}
    if spec.line is not None:
        loc = {"line": spec.line, "col": spec.col, "end_col": spec.end_col}

    if spec.bay > COMFORT_BAY + EPSILON:
        add(
            Issue(
                Severity.INFO,
                "BAY_WIDE",
                f"Frames are spaced up to {_f(spec.bay)} ft on centre — heavier than "
                f"the ~{COMFORT_BAY:.0f} ft typical of residential post-frame.",
                hint=f"Lower the spacing (e.g. `frame bay {COMFORT_BAY:.0f}`) or have "
                "the engineer size the beams/posts for the wider bay.",
                **loc,
            )
        )

    for p in plan.posts:
        if p.role != "interior":
            continue
        for room in plan.rooms:
            if getattr(room, "level", 0) != 0:
                continue
            inset_x = min(p.x - room.x, room.x2 - p.x)
            inset_y = min(p.y - room.y, room.y2 - p.y)
            if inset_x > POST_CLEAR_MARGIN and inset_y > POST_CLEAR_MARGIN:
                add(
                    Issue(
                        Severity.INFO,
                        "POST_OBSTRUCT",
                        f"An interior support post lands in the open floor of "
                        f"'{room.id}' (about {p.x:g},{p.y:g}).",
                        room=room.id,
                        hint="Align a partition, closet, or island with the post line, "
                        "or widen `span` so no interior support is needed.",
                        **loc,
                    )
                )
                break  # one note per post is enough

    # A post standing inside a window/door opening can't be framed — you can't run
    # a structural column through the glass. The post grid is the fixed discipline,
    # so flag the opening to be shifted into a clear bay (between posts).
    openings: list[tuple[_WallOpening, str]] = [(w, "window") for w in plan.windows]
    openings += [(d, "exterior door") for d in plan.exterior_doors]
    for obj, kind in openings:
        oroom = plan.room(obj.room)
        if oroom is None or getattr(oroom, "level", 0) != 0:
            continue
        x1, y1, x2, y2 = opening_endpoints(oroom, obj.wall, obj.offset, obj.width)
        horizontal = obj.wall in (Direction.NORTH, Direction.SOUTH)
        wall_line = y1 if horizontal else x1
        lo, hi = (
            (min(x1, x2), max(x1, x2)) if horizontal else (min(y1, y2), max(y1, y2))
        )
        for p in plan.posts:
            on_line = p.y if horizontal else p.x
            along = p.x if horizontal else p.y
            # Coincident with the wall and *inside* the clear opening (a post at the
            # jamb is how an opening is framed, so endpoints don't count).
            if abs(on_line - wall_line) <= EPSILON and lo + EPSILON < along < hi - EPSILON:
                oloc = dict(loc)
                if getattr(obj, "line", None) is not None:
                    oloc = {"line": obj.line, "col": obj.col, "end_col": obj.end_col}
                add(
                    Issue(
                        Severity.WARNING,
                        "POST_IN_OPENING",
                        f"A structural post at {p.x:g},{p.y:g} stands inside the "
                        f"{kind} on '{obj.room}'s {obj.wall.value} wall.",
                        room=obj.room,
                        hint="Shift the opening along its wall into a clear bay "
                        "(between posts), or change `frame bay` so no post lands on it.",
                        **oloc,
                    )
                )
                break  # one note per opening


def _partition_supported_below(plan: Barndominium, lower_level: int, edge) -> bool:
    """Is an upper partition on ``edge`` carried by a wall or beam on the level
    below? A lower-level room edge on the same grid line (overlapping the span) is
    a wall; a beam on that line counts too. The footprint boundary is a wall."""
    lo, hi = edge.lo, edge.lo + edge.length
    pos, vertical = edge.pos, (edge.orientation == "v")

    def overlaps(a_lo, a_hi) -> bool:
        return min(hi, a_hi) - max(lo, a_lo) > EPSILON

    for r in plan.rooms:
        if getattr(r, "level", 0) != lower_level:
            continue
        if vertical:
            if (abs(r.x - pos) <= EPSILON or abs(r.x2 - pos) <= EPSILON) and overlaps(r.y, r.y2):
                return True
        else:
            if (abs(r.y - pos) <= EPSILON or abs(r.y2 - pos) <= EPSILON) and overlaps(r.x, r.x2):
                return True
    # A beam (bent/ridge) running under the partition line supports it too.
    for b in plan.beams:
        if getattr(b, "level", 0) != lower_level:
            continue
        if vertical and b.orientation == "v" and abs(b.x1 - pos) <= EPSILON:
            if overlaps(min(b.y1, b.y2), max(b.y1, b.y2)):
                return True
        if not vertical and b.orientation == "h" and abs(b.y1 - pos) <= EPSILON:
            if overlaps(min(b.x1, b.x2), max(b.x1, b.x2)):
                return True
    return False


def _validate_load_path(plan: Barndominium, add) -> None:
    """Flag an upper-floor partition with no wall or beam beneath it (IRC R502).

    An interior wall on an upper level that lands over the open middle of a room
    below has no direct load path — the floor framing must carry it. That's fine
    for a light partition on adequately sized joists, but a bearing wall wants a
    wall, beam, or post below. INFO, so it nudges rather than blocks; only runs on
    multi-storey plans.
    """
    if len(plan.levels()) < 2:
        return
    seen: set[frozenset] = set()
    for i, a in enumerate(plan.rooms):
        lvl = getattr(a, "level", 0)
        if lvl < 1:
            continue
        for b in plan.rooms[i + 1:]:
            if getattr(b, "level", 0) != lvl:
                continue
            edge = shared_edge(a, b)
            if edge is None or edge.length < MIN_SOUND_BUFFER_WALL:
                continue  # ignore very short partitions
            key = frozenset((a.id, b.id))
            if key in seen:
                continue
            seen.add(key)
            if not _partition_supported_below(plan, lvl - 1, edge):
                add(
                    Issue(
                        Severity.INFO,
                        "LOAD_PATH",
                        f"The partition between '{a.id}' and '{b.id}' (level {lvl}) has "
                        "no wall or beam directly beneath it — the floor framing must "
                        "carry it (IRC R502).",
                        room=a.id,
                        hint="Align a wall, beam, or post on the level below with this "
                        "partition, or size the floor framing to carry a bearing wall.",
                    )
                )


def _validate_plumbing_stack(plan: Barndominium, add) -> None:
    """Flag an upper-floor wet room with no wet room stacked beneath it.

    A bath/kitchen/laundry drains through a vertical waste stack, and the plumbing
    is far cheaper and simpler when that stack drops straight into a wet room (or a
    wet wall) on the level below rather than jogging horizontally through the floor
    assembly and down through a dry room's ceiling. If an upper-level wet room's
    footprint doesn't overlap any wet room on the level directly below, its stack
    can't run straight down. INFO — a cost/quality nudge like ``WET_GROUP`` (the
    same-floor analogue); only a multi-storey plan with an upper wet room can trip
    it, so single-storey and dry upper floors are untouched.
    """
    if len(plan.levels()) < 2:
        return
    by_level: dict[int, list[Room]] = {}
    for r in plan.rooms:
        by_level.setdefault(r.level, []).append(r)
    for upper in plan.rooms:
        if upper.level < 1 or upper.type not in WET_TYPES:
            continue
        below = [r for r in by_level.get(upper.level - 1, []) if r.type in WET_TYPES]
        if any(upper.overlaps(r) >= MIN_STACK_OVERLAP for r in below):
            continue
        kind = upper.type.value.replace("_", " ")
        add(
            Issue(
                Severity.INFO,
                "PLUMBING_STACK",
                f"The {kind} '{upper.id}' (level {upper.level}) sits over no wet room "
                "below, so its waste stack can't drop straight down — it has to jog "
                "through the floor assembly and down a dry room.",
                room=upper.id,
                hint="Stack it over a bath/kitchen/laundry on the level below (align "
                "their wet walls) so the plumbing drops straight through the floor.",
            )
        )


def _validate_electrical_plan(plan: Barndominium, add) -> None:
    """Opt-in electrical / life-safety reminders the geometry can't verify.

    The DSL models rooms and openings, not receptacles, luminaires, switches or
    exterior grade, so these code requirements can't be checked from the plan —
    yet a plan handed to a builder still has to meet them. Gated behind the
    ``electrical`` directive (exactly like ``accessible``) so an ordinary plan
    isn't nagged; when a plan opts in it gets a single checklist ``info`` to carry
    onto the construction documents. The stair-lighting and door-landing clauses
    only appear when the plan actually has a stair / an exterior people-door.
    """
    if not getattr(plan, "electrical", False):
        return
    # Once the plan actually draws its electrical layer, the sharper per-room
    # checks (OUTLET_SPACING / OUTLET_GFCI / ROOM_NO_LIGHT) take over — the
    # generic checklist would just be noise next to them.
    if plan.outlets or plan.switches or plan.lights:
        return
    parts = [
        "space receptacles so no point along any wall is more than 6 ft from one "
        "(IRC E3901.2), with GFCI protection at kitchens, baths, laundry and "
        "outdoors and AFCI protection on habitable-room circuits (E3902)",
        "provide a wall-switch-controlled lighting outlet at every habitable room, "
        "hallway and exterior entrance (IRC R303.7 / E3903)",
    ]
    if plan.stairs:
        parts.append(
            "light each stair, switched at every floor level it serves (IRC R303.7)"
        )
    if any(not d.overhead for d in plan.exterior_doors):
        parts.append(
            "provide a level landing on each side of every exterior door, no more "
            "than 1.5 in below the threshold (IRC R311.3)"
        )
    add(
        Issue(
            Severity.INFO,
            "ELECTRICAL_PLAN",
            "Electrical / life-safety items the DSL can't place — carry them onto "
            "the construction documents: " + "; ".join(parts) + ".",
            hint="These aren't in the geometry; confirm them on the electrical and "
            "site plans.",
        )
    )


#: IRC E3901.4 kitchen counter receptacles: any counter run at least 12 in wide
#: needs a small-appliance receptacle, spaced so no point along the counter wall
#: line is more than 24 in from one. Fixed IRC figures (not jurisdiction-variable).
_COUNTER_MIN_WIDTH = 12.0 / 12.0   # 12 in — the narrowest run that needs a receptacle
_COUNTER_MAX_REACH = 24.0 / 12.0   # 24 in — max horizontal reach to a receptacle


def _counter_run_span(room: Room, f) -> tuple[float, float, "Direction"]:
    """A kitchen counter fixture's along-wall interval ``(lo, hi)`` in world feet
    plus the :class:`Direction` of the wall it backs to."""
    if f.wall == "S":
        return f.x, f.x + f.width, Direction.SOUTH
    if f.wall == "N":
        return f.x, f.x + f.width, Direction.NORTH
    if f.wall == "E":
        return f.y, f.y + f.length, Direction.EAST
    return f.y, f.y + f.length, Direction.WEST


def _receptacle_reach(room: Room, outlets: list) -> float:
    """The worst-case distance (ft) from any point on ``room``'s wall line to the
    nearest receptacle, walking the perimeter as a closed loop (so an outlet near
    a corner covers the adjacent wall too). ``max_gap / 2`` — the midpoint of the
    widest run between two receptacles — is the figure IRC E3901.2 caps at 6 ft.
    Doorways (which reset wall space) aren't modelled, so this is a slightly
    conservative wall-line measure."""
    w, length = room.width, room.length
    perim = 2.0 * (w + length)
    if perim <= 0 or not outlets:
        return perim  # nothing to reach
    arcs: list[float] = []
    for o in outlets:
        if o.wall is Direction.SOUTH:
            a = min(max(o.offset, 0.0), w)
        elif o.wall is Direction.EAST:
            a = w + min(max(o.offset, 0.0), length)
        elif o.wall is Direction.NORTH:
            a = w + length + (w - min(max(o.offset, 0.0), w))
        else:  # WEST
            a = 2.0 * w + length + (length - min(max(o.offset, 0.0), length))
        arcs.append(a % perim)
    arcs.sort()
    gaps = [arcs[i + 1] - arcs[i] for i in range(len(arcs) - 1)]
    gaps.append(perim - arcs[-1] + arcs[0])  # wrap-around gap (whole loop if n==1)
    return max(gaps) / 2.0


def _validate_electrical(plan: Barndominium, add) -> None:
    """Per-room electrical checks — active for any room that draws its electrical
    layer (declares an `outlet`/`switch`/`light`), independent of the `electrical`
    directive. Rooms that draw nothing are never nagged (the layer is opt-in).

    Checks: receptacle spacing (OUTLET_SPACING, IRC E3901.2 — no wall point > 6 ft
    from a receptacle, in habitable rooms with outlets); GFCI protection
    (OUTLET_GFCI, IRC E3902 — a wet-room receptacle not marked `gfci`); and a
    lighting outlet (ROOM_NO_LIGHT, IRC E3903 — a habitable room with power but no
    `light`)."""
    if not (plan.outlets or plan.switches or plan.lights):
        return
    from .render import fmt_ft_in

    by_id = {r.id: r for r in plan.rooms}
    outlets_by: dict[str, list] = {}
    for o in plan.outlets:
        outlets_by.setdefault(o.room, []).append(o)
    powered: set[str] = set(outlets_by)
    for sw in plan.switches:
        powered.add(sw.room)
    lit = {lt.room for lt in plan.lights}

    # GFCI — a receptacle in a wet/damp room that isn't ground-fault protected.
    for o in plan.outlets:
        room = by_id.get(o.room)
        if room is not None and room.type in WET_TYPES and not o.gfci:
            add(
                Issue(
                    Severity.WARNING,
                    "OUTLET_GFCI",
                    f"A receptacle in the {room.display_name.lower()} "
                    f"({room.type.value.replace('_', ' ')}) isn't marked `gfci` — "
                    "IRC E3902 requires ground-fault protection there.",
                    room=o.room,
                    line=o.line,
                    col=o.col,
                    end_col=o.end_col,
                    hint="Add `gfci` to the outlet, or protect the circuit at the "
                    "panel and note it on the electrical plan.",
                )
            )

    # Receptacle spacing — only habitable rooms that opted in by drawing an outlet.
    for rid, outs in outlets_by.items():
        room = by_id.get(rid)
        if room is None or room.type not in HABITABLE_TYPES:
            continue
        reach = _receptacle_reach(room, outs)
        if reach > 6.0 + 1e-6:
            add(
                Issue(
                    Severity.WARNING,
                    "OUTLET_SPACING",
                    f"In {room.display_name}, a point on the wall is up to "
                    f"{fmt_ft_in(reach)} from the nearest receptacle — IRC E3901.2 "
                    "allows no more than 6 ft (a receptacle at least every 12 ft of "
                    "wall run).",
                    room=rid,
                    hint="Add an `outlet` in the widest gap so no wall point is "
                    "more than 6 ft from one.",
                )
            )

    # RECEPTACLE_COUNTER — IRC E3901.4 kitchen small-appliance receptacles. Along
    # each kitchen counter run (>= 12 in wide), no point on the counter wall line
    # may be more than 24 in from a receptacle. Gated the same way as OUTLET_SPACING
    # (only a plan that draws its electrical layer is judged), so a seed-only kitchen
    # is never nagged. Uses the resolved counter runs, so a `fixture counter ... along`
    # run is what's measured.
    from .fixtures import resolve_room_fixtures

    for room in plan.rooms:
        if room.type is not RoomType.KITCHEN:
            continue
        counters = [
            f
            for f in resolve_room_fixtures(plan, room)
            if f.kind == "counter" and f.wall in ("S", "N", "E", "W")
        ]
        if not counters:
            continue
        room_outlets = outlets_by.get(room.id, [])
        for c in counters:
            lo, hi, wall_dir = _counter_run_span(room, c)
            width = hi - lo
            if width + 1e-9 < _COUNTER_MIN_WIDTH:
                continue  # a run under 12 in takes no receptacle (E3901.4.3(1))
            base = room.x if wall_dir in (Direction.SOUTH, Direction.NORTH) else room.y
            pts = sorted(
                base + o.offset
                for o in room_outlets
                if o.wall is wall_dir and lo - 1e-6 <= base + o.offset <= hi + 1e-6
            )
            if not pts:
                gap = width  # the whole run is unserved
            else:
                gaps = [pts[0] - lo, hi - pts[-1]]
                gaps += [(pts[i + 1] - pts[i]) / 2.0 for i in range(len(pts) - 1)]
                gap = max(gaps)
            if gap > _COUNTER_MAX_REACH + 1e-6:
                add(
                    Issue(
                        Severity.WARNING,
                        "RECEPTACLE_COUNTER",
                        f"The {fmt_ft_in(width)} kitchen counter run in "
                        f"'{room.id}' (on its {c.wall} wall) leaves a point "
                        f"{fmt_ft_in(gap)} from the nearest receptacle — IRC E3901.4 "
                        "wants a small-appliance receptacle within 24 in of every "
                        "point along a counter (and one on any counter >= 12 in wide).",
                        room=room.id,
                        hint="Add a receptacle on the counter wall in the gap, e.g. "
                        f"`outlet in {room.id} wall {c.wall} offset <ft>` — no point "
                        "along the counter should be more than 24 in from one.",
                    )
                )

    # Lighting outlet — a habitable room with power but nothing to switch on.
    for rid in sorted(powered):
        room = by_id.get(rid)
        if room is None or room.type not in HABITABLE_TYPES:
            continue
        if rid not in lit:
            add(
                Issue(
                    Severity.INFO,
                    "ROOM_NO_LIGHT",
                    f"{room.display_name} draws receptacles/switches but no "
                    "lighting outlet — IRC E3903 wants a wall-switch-controlled "
                    "light in every habitable room.",
                    room=rid,
                    hint="Add a `light in " + rid + " at <x>,<y>` (or note a "
                    "switched receptacle).",
                )
            )


#: The rooms where lack of winter sun (a north-only aspect) most hurts comfort.
#: An office is excluded on purpose: even, glare-free north light is a legitimate
#: choice for a studio/workspace, so "lit only from the north" isn't a defect there.
_SUN_WANTED_TYPES = {RoomType.LIVING, RoomType.DINING, RoomType.BEDROOM, RoomType.KITCHEN}
#: Preference order for the sunnier wall SOLAR_NORTH_ONLY recommends.
_SECTOR_PREF = {"south": 0, "east": 1, "west": 2}


def _shaded_by_covered_porch(plan: Barndominium, room: Room, win) -> bool:
    """True if a covered porch abuts ``win``'s wall on the outside and spans it —
    real vertical shade (the kind a low west sun actually needs)."""
    if win.wall in (Direction.SOUTH, Direction.NORTH):
        lo = room.x + win.offset
    else:
        lo = room.y + win.offset
    mid = lo + win.width / 2.0
    for p in plan.porches:
        if not p.covered:
            continue
        px2, py2 = p.x + p.width, p.y + p.length
        spans_x = p.x - EPSILON <= mid <= px2 + EPSILON
        spans_y = p.y - EPSILON <= mid <= py2 + EPSILON
        if win.wall is Direction.SOUTH and abs(py2 - room.y) < 0.75 and spans_x:
            return True
        if win.wall is Direction.NORTH and abs(p.y - room.y2) < 0.75 and spans_x:
            return True
        if win.wall is Direction.WEST and abs(px2 - room.x) < 0.75 and spans_y:
            return True
        if win.wall is Direction.EAST and abs(p.x - room.x2) < 0.75 and spans_y:
            return True
    return False


def _validate_solar(plan: Barndominium, add) -> None:
    """Solar-glazing nudges — only when the plan declares an ``orientation``.

    With a true-north azimuth in hand, each exterior wall's outward face maps to a
    compass bearing, so a room's windows can be bucketed by sun exposure. Two
    nudges (INFO; northern-hemisphere): too much unshaded **west** glass overheats
    a room through a low afternoon sun that's hard to shade, and a room lit **only
    from the north** is dim and cold in winter when it has a sunnier wall free.
    Dormant on an unsited plan (``orientation is None``), so it never imposes a
    north on an abstract sketch — the same opt-in discipline as ``accessible``.
    """
    theta = plan.orientation
    if theta is None:
        return
    for room in plan.rooms:
        if room.type not in HABITABLE_TYPES:
            continue
        wins = plan.windows_for(room.id)
        if not wins:
            continue
        by_sector: dict[str, float] = {}
        for w in wins:
            sec = wall_sector(w.wall, theta)
            by_sector[sec] = by_sector.get(sec, 0.0) + w.glazed_area

        # (1) Too much *unshaded* west glass — overheats in the afternoon. A
        #     covered porch gives the vertical shade a low west sun needs, so glass
        #     behind one doesn't count against this.
        west_wins = [
            w
            for w in wins
            if wall_sector(w.wall, theta) == "west"
            and not _shaded_by_covered_porch(plan, room, w)
        ]
        west = sum(w.glazed_area for w in west_wins)
        if west > SOLAR_WEST_MAX_GLAZING:
            widest = max(west_wins, key=lambda w: w.glazed_area)
            az = true_azimuth(widest.wall, theta)
            add(
                Issue(
                    Severity.INFO,
                    "SOLAR_WEST_GAIN",
                    f"'{room.id}' has {_f(west)} sq ft of glazing facing "
                    f"{compass_label(az)} ({az:.0f}° true) — a low afternoon sun "
                    "that overheats the room and is hard to shade.",
                    room=room.id,
                    hint="Shade it with a deep overhang/porch or an awning, cut the "
                    "west glass back, or move it to the south face.",
                )
            )

        # (2) Glazed only to the north — dim, cold — with a sunnier wall to spare.
        total = sum(by_sector.values())
        if (
            room.type in _SUN_WANTED_TYPES
            and total > EPSILON
            and by_sector.get("north", 0.0) + EPSILON >= total
        ):
            alt = sorted(
                (
                    wall
                    for wall in exterior_walls(plan, room)
                    if wall_sector(wall, theta) != "north"
                ),
                key=lambda wall: _SECTOR_PREF[wall_sector(wall, theta)],
            )
            if alt:
                best = alt[0]
                bearing = compass_label(true_azimuth(best, theta))
                add(
                    Issue(
                        Severity.INFO,
                        "SOLAR_NORTH_ONLY",
                        f"'{room.id}' is glazed only to the north — little direct sun, "
                        "so it will feel dim and cold in winter.",
                        room=room.id,
                        hint=f"Add a window on the {best.value} wall (faces {bearing}) "
                        f"for winter sun, e.g. `window {room.id} {best.value} width 4 "
                        "offset 2`.",
                    )
                )

    # (3) Plan-level: a large south face left almost unglazed wastes the best
    #     passive-solar wall (free winter heat you can shade in summer).
    south_wall = sum(
        _wall_length(room, wall)
        for room in plan.rooms
        for wall in exterior_walls(plan, room)
        if wall_sector(wall, theta) == "south"
    )
    south_glass = sum(
        w.glazed_area for w in plan.windows if wall_sector(w.wall, theta) == "south"
    )
    if south_wall >= SOLAR_SOUTH_MIN_WALL and south_glass < SOLAR_SOUTH_MIN_GLAZING:
        add(
            Issue(
                Severity.INFO,
                "SOLAR_SOUTH_UNUSED",
                f"The plan has ~{_f(south_wall)} ft of south-facing wall but only "
                f"{_f(south_glass)} sq ft of south glazing — the best passive-solar "
                "face is nearly blank.",
                hint="Add south-facing windows for free winter sun (with an overhang "
                "or porch to block the high summer sun).",
            )
        )

    # (4) Plan-level: substantial south glass with no eave to shade it overheats
    #     in summer. A covered porch over the glass counts as shade instead.
    def _win_shaded(w) -> bool:
        r = plan.room(w.room)
        return r is not None and _shaded_by_covered_porch(plan, r, w)

    south_unshaded = sum(
        w.glazed_area
        for w in plan.windows
        if wall_sector(w.wall, theta) == "south" and not _win_shaded(w)
    )
    if plan.overhang < MIN_SHADE_OVERHANG and south_unshaded > SOLAR_SOUTH_SHADE_GLAZING:
        add(
            Issue(
                Severity.INFO,
                "SOLAR_SOUTH_NO_OVERHANG",
                f"{_f(south_unshaded)} sq ft of south glazing has no roof overhang to "
                "shade it — the high summer sun will overheat those rooms.",
                hint="Add a ~2 ft eave (`overhang 2`) or a covered porch over the south "
                "glass — it blocks the high summer sun but still lets the low winter "
                "sun in.",
            )
        )


def _validate_approach(plan: Barndominium, add) -> None:
    """Approach nudges — only when the plan declares a ``street`` side.

    A home should meet its street: the front door faces the approach, and the
    garage doors don't turn their back on it (forcing a drive around the house).
    Both INFO; dormant unless ``street`` is set, so an unsited plan is untouched.
    """
    street = plan.street
    if street is None:
        return
    people = [d for d in plan.exterior_doors if not d.overhead]
    if people and not any(d.wall == street for d in people):
        add(
            Issue(
                Severity.INFO,
                "APPROACH_ENTRY",
                f"No entrance faces the street ({street.value} side); the front door "
                "is around the side or back.",
                hint=f"Put a people-door on the {street.value} wall, e.g. "
                f"`entry <room> {street.value} width 3`.",
            )
        )
    back = street.opposite()
    for d in plan.exterior_doors:
        if d.overhead and d.wall == back:
            add(
                Issue(
                    Severity.INFO,
                    "APPROACH_GARAGE",
                    f"The overhead door on '{d.room}' faces away from the street (the "
                    f"{back.value} side); a vehicle would have to drive around the "
                    "house to reach it.",
                    room=d.room,
                    hint=f"Face the overhead door toward the street/approach or a side "
                    f"wall, not the {back.value} wall.",
                )
            )


def _validate_energy(plan: Barndominium, add) -> None:
    """Thermal-envelope guidance — only when the plan declares a ``climate`` zone.

    The compiler can't run an energy model, so this is guidance, not a pass/fail:
    the prescriptive R-value targets for the zone, the steel-frame thermal-bridge
    note (the barndominium's characteristic failure), and a window-to-wall-ratio
    *ceiling* to go with the existing daylight *floor* (NAT_LIGHT). Dormant unless
    ``climate`` is set, so ordinary plans are untouched.
    """
    zone = plan.climate
    if zone is None:
        return
    add(
        Issue(
            Severity.INFO,
            "ENERGY_ENVELOPE",
            f"IECC climate zone {zone} — prescriptive envelope targets (approx.): "
            f"{describe_targets(zone)}. On a steel frame, put the wall insulation as "
            "continuous exterior insulation — steel studs are a severe thermal bridge "
            "that guts the cavity R-value.",
            hint="Confirm the R-values against the adopted energy code (ideally with a "
            "rater); the DSL can't model the assembly.",
        )
    )
    wall_area = plan.metrics()["exterior_wall_area_sqft"]
    glazing = sum(w.glazed_area for w in plan.windows)
    if wall_area > EPSILON and glazing / wall_area > WWR_CEILING:
        wwr = glazing / wall_area
        add(
            Issue(
                Severity.INFO,
                "WINDOW_HEAVY",
                f"Glazing is {wwr * 100:.0f}% of the exterior wall area (target "
                f"<= {WWR_CEILING * 100:.0f}%) — a high window-to-wall ratio drives "
                "the heating and cooling load.",
                hint="Trim glazing toward the target, or concentrate it on the south "
                "for winter gain and shade it with an overhang.",
            )
        )


def _porch_lands_door(plan: Barndominium, room: Room, wall: Direction,
                      seg_lo: float, seg_hi: float, tol: float = 0.05) -> bool:
    """True if a porch platforms an exterior door: its footprint abuts ``wall``
    from outside, spans the door opening (``[seg_lo, seg_hi]`` along the wall),
    and reaches at least :data:`LANDING_MIN_DEPTH` outward from the wall."""
    for p in plan.porches:
        px2, py2 = p.x + p.width, p.y + p.length
        if wall is Direction.SOUTH:
            abut, depth = py2 >= room.y - 0.75, room.y - p.y
            span = p.x <= seg_lo + tol and px2 >= seg_hi - tol
        elif wall is Direction.NORTH:
            abut, depth = p.y <= room.y2 + 0.75, py2 - room.y2
            span = p.x <= seg_lo + tol and px2 >= seg_hi - tol
        elif wall is Direction.WEST:
            abut, depth = px2 >= room.x - 0.75, room.x - p.x
            span = p.y <= seg_lo + tol and py2 >= seg_hi - tol
        else:  # EAST
            abut, depth = p.x <= room.x2 + 0.75, px2 - room.x2
            span = p.y <= seg_lo + tol and py2 >= seg_hi - tol
        if abut and span and depth + tol >= LANDING_MIN_DEPTH:
            return True
    return False


def _primary_entry(plan: Barndominium):
    """The plan's primary people-entrance: prefer an egress door on an exterior
    wall, front (street side if declared, else south) first, else the first
    entry. Returns ``None`` if the plan has no people-door."""
    entries = [d for d in plan.exterior_doors if not d.overhead]
    if not entries:
        return None
    by_id = {r.id: r for r in plan.rooms}

    def on_exterior(d) -> bool:
        r = by_id.get(d.room)
        return r is not None and d.wall in exterior_walls(plan, r)

    front = plan.street if plan.street is not None else Direction.SOUTH
    ext = [d for d in entries if on_exterior(d)]
    pool = ext or entries

    def rank(d) -> tuple:
        return (0 if d.egress else 1, 0 if d.wall is front else 1)

    return min(pool, key=rank)


def _validate_landings(plan: Barndominium, add) -> None:
    """Flag an exterior people-door with no landing (IRC R311.3).

    A porch whose footprint covers the door's exterior face (its full width, at
    least a door-depth out) is the landing. Only entries are checked — an overhead
    garage door needs none. Severity is context-aware: with porches modelled
    anywhere, every uncovered entry warns; on a plan with NO porches at all it's a
    single INFO nudge on the primary entry (the plan just hasn't drawn porches
    yet — don't spam every door)."""
    entries = [d for d in plan.exterior_doors if not d.overhead]
    if not entries:
        return
    if not plan.porches:
        primary = _primary_entry(plan)
        if primary is not None:
            add(Issue(
                Severity.INFO, "DOOR_NO_LANDING",
                f"Exterior door in '{primary.room}' needs a landing on the outside "
                "(IRC R311.3) — the plan draws no porch yet.",
                room=primary.room,
                hint="Add a `porch` at the door (covering its width, >= "
                f"{LANDING_MIN_DEPTH:g} ft deep), or note the landing on the "
                "construction documents. Nudged once, on the primary entry.",
                **_door_loc(primary)))
        return
    by_id = {r.id: r for r in plan.rooms}
    for d in entries:
        room = by_id.get(d.room)
        if room is None:
            continue  # DOOR_REF handles a bad ref
        if d.wall not in exterior_walls(plan, room):
            continue  # an entry on an interior wall is ENTRY_INTERIOR's problem
        x1, y1, x2, y2 = opening_endpoints(room, d.wall, d.offset, d.width)
        if d.wall in (Direction.SOUTH, Direction.NORTH):
            seg_lo, seg_hi = min(x1, x2), max(x1, x2)
        else:
            seg_lo, seg_hi = min(y1, y2), max(y1, y2)
        if _porch_lands_door(plan, room, d.wall, seg_lo, seg_hi):
            continue
        add(Issue(
            Severity.WARNING, "DOOR_NO_LANDING",
            f"Exterior door in '{d.room}' (on the {d.wall.value} wall) opens onto "
            "no landing — IRC R311.3 wants a porch/landing spanning the door, at "
            f"least {LANDING_MIN_DEPTH:g} ft deep.",
            room=d.room,
            hint=f"Add a `porch` at the door covering its {_f(d.width)} ft width "
            f"(>= {LANDING_MIN_DEPTH:g} ft deep), swing the door where a porch "
            "already reaches, or note the landing on the construction documents.",
            **_door_loc(d)))


def _validate_door_threshold(plan: Barndominium, add) -> None:
    """Remind, once, about the threshold-to-landing drop at the required egress door.

    IRC R311.3.1: at the required egress door the exterior landing may be no more
    than 1.5 in below the top of the threshold (7.75 in is allowed only where the
    door does not swing out over the landing). The model carries no vertical
    threshold data, so this is a reminder-class INFO like BATH_VENT — it teaches,
    it doesn't measure. It nudges once, on the primary entry, and only *before* a
    porch/landing is modelled (mirroring DOOR_NO_LANDING's single info): once the
    plan draws its landings, DOOR_NO_LANDING's per-door pass and the CD set carry
    the detail, so a second always-on reminder would just be noise.
    """
    if plan.porches:
        return
    primary = _primary_entry(plan)
    if primary is None:
        return
    add(Issue(
        Severity.INFO, "DOOR_THRESHOLD",
        f"At the required egress door in '{primary.room}', keep the exterior landing "
        "no more than 1.5 in below the threshold (7.75 in only where the door doesn't "
        "swing out over it) — IRC R311.3.1.",
        room=primary.room,
        hint="The model has no vertical threshold data — confirm the landing-to-"
        "threshold drop on the construction documents. Nudged once, on the primary "
        "entry.",
        **_door_loc(primary)))


def _validate_water_heater(plan: Barndominium, add) -> None:
    """Flag a water_heater fixture whose placement needs extra protection.

    Two cases (either, or both): in a garage/shop its ignition source must be
    elevated 18 in / be a listed FVIR unit (IRC M1307.3); on an upper floor over
    habitable space it needs a drain pan piped to a drain (IRC P2801.6). One INFO
    per heater, naming which case(s) apply."""
    by_id = {r.id: r for r in plan.rooms}
    for f in plan.fixtures:
        if f.kind != "water_heater":
            continue
        room = by_id.get(f.room)
        if room is None:
            continue  # FIXTURE_ROOM handles a bad ref
        cases: list[str] = []
        if room.type in GARAGE_TYPES:
            cases.append(
                "in a garage/shop, so its ignition source must be elevated 18 in "
                "above the floor or be a listed flammable-vapour-ignition-resistant "
                "unit (IRC M1307.3)"
            )
        if room.level >= 1:
            below = [
                r for r in plan.rooms
                if r.level == room.level - 1
                and r.type in HABITABLE_TYPES
                and min(room.x2, r.x2) - max(room.x, r.x) > EPSILON
                and min(room.y2, r.y2) - max(room.y, r.y) > EPSILON
            ]
            if below:
                cases.append(
                    f"on level {room.level} over habitable space, so it needs a "
                    "drain pan piped to an approved drain (IRC P2801.6)"
                )
        if not cases:
            continue
        add(Issue(
            Severity.INFO, "WATER_HEATER_PLACEMENT",
            f"The water heater in '{f.room}' is " + " and ".join(cases) + ".",
            room=f.room,
            hint="The DSL can't draw the pan/elevation — carry the detail onto the "
            "plumbing/mechanical documents."))


def _door_clear_width(door) -> float:
    """An exterior door's egress **clear** width: a double/french pair provides
    its required clear opening through ONE leaf (IRC R311.2), so it counts half
    the total width; a single leaf counts its full width."""
    if getattr(door, "kind", "entry") in DOUBLE_LEAF_KINDS:
        # Round the derived leaf to 1/100 ft so a 64-in stock pair authored as
        # `width 5.333` yields a 32.0-in leaf instead of 31.998 (a 1/16-in
        # grace, far below construction tolerance); a single leaf is the
        # authored number and needs no rounding.
        return round(door.width / 2.0, 2)
    return door.width


def _validate_egress_and_light(plan: Barndominium, add, profile: Profile = DEFAULT) -> None:
    # An overhead door never counts as egress (`entrance` forces egress=False
    # for it; the kind check guards a hand-built ExteriorDoor too). A double
    # door counts one leaf (see _door_clear_width).
    has_egress_door = any(
        d.egress
        and not d.overhead
        and _door_clear_width(d) + EPSILON >= MIN_EGRESS_DOOR_WIDTH
        for d in plan.exterior_doors
    )
    if plan.exterior_doors and not has_egress_door:
        add(
            Issue(
                Severity.WARNING,
                "EGRESS_DOOR",
                f"No exterior egress door is at least {MIN_EGRESS_DOOR_WIDTH * 12:.0f} in wide.",
                hint=f"Make at least one `entry` width >= {MIN_EGRESS_DOOR_WIDTH:g} "
                "(a double/french pair counts one leaf, so it needs twice that).",
            )
        )

    for room in plan.rooms:
        walls = exterior_walls(plan, room)
        if room.type is RoomType.BEDROOM:
            # Only an opening on an exterior wall counts as an escape route —
            # and only one that OPENS: fixed glass daylights but is never an
            # emergency escape opening (IRC R310).
            ext_windows = [w for w in plan.windows_for(room.id) if w.wall in walls]
            escape_windows = [
                w for w in ext_windows if getattr(w, "kind", "casement") != "fixed"
            ]
            ext_doors = [d for d in plan.exterior_doors_for(room.id) if d.wall in walls]
            if not escape_windows and not ext_doors:
                only_fixed = bool(ext_windows)
                if only_fixed:
                    hint = (
                        f"Fixed glass doesn't open — make a window operable "
                        f"(casement/slider/double-hung), e.g. `window {room.id} "
                        f"{ext_windows[0].wall.value} width 4 offset 2`."
                    )
                elif walls:
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
                        "Bedroom has no emergency escape opening"
                        + (
                            " — its only exterior windows are fixed glass, which "
                            "doesn't open."
                            if only_fixed
                            else "."
                        ),
                        room=room.id,
                        hint=hint,
                    )
                )
            else:
                # An escape opening exists — does it meet the R310 clear-opening
                # minimums? A full-height door qualifies on width alone (one leaf
                # of a double); a window must clear the area and both dimensions
                # — per its kind's honest clear opening (a slider opens ~half its
                # width, a double-hung ~half its height) — and sit low enough.
                min_area = (
                    profile.min_egress_area_grade if room.level == 0
                    else profile.min_egress_area
                )
                min_ow = profile.min_egress_opening_width
                min_oh = profile.min_egress_opening_height
                max_sill = profile.max_egress_sill

                def _win_ok(w, min_ow=min_ow, min_oh=min_oh, min_area=min_area,
                            max_sill=max_sill) -> bool:
                    cw, ch = w.clear_opening
                    return (
                        cw + EPSILON >= min_ow
                        and ch + EPSILON >= min_oh
                        and cw * ch + EPSILON >= min_area
                        and w.sill_height <= max_sill + EPSILON
                    )

                door_ok = any(
                    _door_clear_width(d) + EPSILON >= min_ow
                    for d in ext_doors
                )
                if not (door_ok or any(_win_ok(w) for w in escape_windows)):
                    if escape_windows:
                        best = max(
                            escape_windows,
                            key=lambda w: w.clear_opening[0] * w.clear_opening[1],
                        )
                        cw, ch = best.clear_opening
                        kind_note = (
                            "" if best.kind == "casement" else f" {best.kind}"
                        )
                        detail = (
                            f"its largest{kind_note} clears ~{_f(cw)} ft wide × "
                            f"{_f(ch)} ft ({_f(cw * ch)} sq ft, sill "
                            f"{best.sill_height * 12:.0f} in)"
                        )
                    else:
                        detail = "its only exterior opening is a too-narrow door"
                    egress_amended = any(
                        _amended(profile, f) for f in (
                            "min_egress_area", "min_egress_area_grade",
                            "min_egress_opening_width", "min_egress_opening_height",
                            "max_egress_sill",
                        )
                    )
                    rule = (
                        f"the '{profile.name}' profile's escape-opening minimum"
                        if egress_amended
                        else "the IRC R310 minimum"
                    )
                    add(
                        Issue(
                            Severity.WARNING,
                            "EGRESS_SIZE",
                            f"Bedroom '{room.id}' has an escape opening but it's below "
                            f"{rule} ({_f(min_area)} sq ft clear, "
                            f"{min_ow * 12:.0f} in wide × "
                            f"{min_oh * 12:.0f} in tall, sill "
                            f"<= {max_sill * 12:.0f} in); {detail}.",
                            room=room.id,
                            hint=f"Widen/enlarge the egress window so its clear opening "
                            f"is >= {_f(min_area)} sq ft (a casement clears ~its full "
                            "glazed size; a slider ~half its width; a double-hung "
                            "~half its height), e.g. "
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
            light_ratio = profile.natural_light_ratio
            required = room.area * light_ratio
            if glazing + EPSILON < required:
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
                        f"({light_ratio * 100:.0f}% of floor area)"
                        + _profile_tag(
                            profile, "natural_light_ratio",
                            f"{NATURAL_LIGHT_RATIO * 100:.0f}%"
                        )
                        + ".",
                        room=room.id,
                        hint=hint,
                    )
                )

            # Natural ventilation (IRC R303.1): a habitable room needs OPENABLE
            # window area >= 4% of its floor. Fixed glass daylights (counted above)
            # but opens nothing, so it's excluded here — the gap NAT_LIGHT can't
            # see. Existing plans (default casement windows) keep passing: any room
            # that clears the 8% daylight floor with openable glass clears this 4%
            # floor too.
            openable = sum(
                w.glazed_area
                for w in plan.windows_for(room.id)
                if w.wall in walls and getattr(w, "openable", w.kind != "fixed")
            )
            vent_required = room.area * NATURAL_VENT_RATIO
            if openable + EPSILON < vent_required:
                fixed_here = any(
                    w.wall in walls and not getattr(w, "openable", w.kind != "fixed")
                    for w in plan.windows_for(room.id)
                )
                if not walls:
                    vhint = (
                        f"'{room.id}' has no exterior wall — open it to an adjacent "
                        "room or move it to the perimeter so it can be ventilated."
                    )
                elif fixed_here:
                    vhint = (
                        "A `fixed` window opens nothing — make one operable "
                        f"(casement/slider/double-hung), e.g. `window {room.id} "
                        f"{walls[0].value} width 4 offset 2`, or confirm mechanical "
                        "ventilation."
                    )
                else:
                    vhint = (
                        f"Add openable window area on an exterior wall, e.g. "
                        f"`window {room.id} {walls[0].value} width 4 offset 2`, or "
                        "confirm mechanical ventilation."
                    )
                add(
                    Issue(
                        Severity.WARNING,
                        "VENT_AREA",
                        f"Openable window area {_f(openable)} sq ft is below the "
                        f"natural-ventilation minimum of {_f(vent_required)} sq ft "
                        f"({NATURAL_VENT_RATIO * 100:.0f}% of floor area, IRC R303.1).",
                        room=room.id,
                        hint=vhint,
                    )
                )
