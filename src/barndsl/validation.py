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
MAX_SINGLE_SWING_DOOR_WIDTH = 3.0  # wider wants cased/open or double/french
# Manufactured door leaf widths (inches). A door off these isn't orderable
# off-the-shelf; the check nudges to the nearest. Doubles (60/72) included.
STD_INTERIOR_DOOR_WIDTHS_IN = (24, 28, 30, 32, 36, 60, 72)
STD_EXTERIOR_DOOR_WIDTHS_IN = (30, 32, 36, 60, 72)
#: Stock total widths for a declared double/french pair — two equal leaves
#: (2×24 .. 2×36). A declared double is checked against these instead of the
#: single-leaf sizes.
STD_DOUBLE_DOOR_WIDTHS_IN = (48, 60, 64, 72)
#: Stock bifold opening widths: 24-36 in singles, 48/60/72 in pairs, and 96 in
#: (two 48 in units sharing one opening — the wide reach-in closet standard).
STD_BIFOLD_DOOR_WIDTHS_IN = (24, 30, 32, 36, 48, 60, 72, 96)
DOOR_SIZE_TOL_IN = 0.5  # how far off a standard size before we nudge
# Overhead (sectional garage) door stock sizes, in **feet** — garage doors are
# ordered in feet, unlike leaf doors: singles 8/9/10 wide, doubles 12/16. Heights
# span the residential 7/8 ft panels AND the commercial sectional panels (10/12/14
# ft) a barndominium shop wants for lift/RV clearance — so a compliant tall shop
# door snaps to a real stock height, not down to 8 ft. An opening wider than
# OVERHEAD_HEADER_SPAN outruns a stock header and wants engineering with the frame.
STD_OVERHEAD_DOOR_WIDTHS_FT = (8, 9, 10, 12, 16)
STD_OVERHEAD_DOOR_HEIGHTS_FT = (7, 8, 10, 12, 14)
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
MAX_ROOM_ASPECT = 3.0  # default: a habitable room longer than this (long:short) is awkward
MAX_GENERAL_ROOM_ASPECT = 4.0  # severe long-skinny warning for most non-circulation rooms
#: Per-type elongation ceilings (long:short) above which ROOM_PROPORTION fires,
#: overriding :data:`MAX_ROOM_ASPECT`. A bedroom has to hold a bed *and* a
#: walk-around, so a 2:1 "tunnel" bedroom (an 8×16, say) is already awkward well
#: before the generic 3:1 bar — tighten it so the lint sees the tunnel bedroom the
#: score is penalising. An office is the same story with a desk: it holds a desk,
#: a chair-pull and a walk-around (often a guest chair), so a 2:1+ office is a
#: corridor with a desk — and the score already penalises any habitable room past
#: GOOD_ASPECT (1.7:1), so without this override an elongated office loses points
#: silently with no lint explaining why. Keeping these at 1.8 leaves only a 0.1
#: band of silent deduction. Other habitable rooms keep the 3:1 default.
MAX_ROOM_ASPECT_BY_TYPE: dict[RoomType, float] = {
    RoomType.BEDROOM: 1.8,
    RoomType.OFFICE: 1.8,
}
#: A mudroom's job needs floor, not length: a bench (~1.5 ft) plus a 3 ft
#: walkway means anything under ~5 ft wide can't hold the drop zone it exists
#: for, and past ~2.5:1 it reads as a corridor wearing a mudroom label.
#: Mudrooms aren't habitable, so ROOM_PROPORTION never sees them (MUDROOM_SHAPE).
MIN_MUDROOM_WIDTH = 5.0
MAX_MUDROOM_ASPECT = 2.5
#: A single *concentrated* patch of unassigned footprint this large (sq ft) is a
#: void — a real hole in the plan (an unfinished room, a mis-sized neighbour), not
#: the diffuse slack AREA_UNUSED measures. AREA_UNUSED only speaks below 85%
#: coverage and sums *all* slack, so a room-sized rectangle of dead space on an
#: otherwise well-covered footprint (95%+) is invisible to it; this fires on the
#: largest connected gap regardless of overall coverage. Severity is tiered:
#: at or above MIN_CONCENTRATED_VOID — a walk-in closet / powder room's worth
#: of floor — the void is an ERROR (an enclosed pocket you'd frame, roof and
#: pour foundation around, that no one can enter, is not a buildable intent);
#: between MIN_VOID_NOTE and that bar it is an INFO nudge. Below MIN_VOID_NOTE
#: — wall-thickness slack between rooms, a few sq ft — it never speaks. (The
#: error bar was 70 at first, "just below a small bedroom", until a live agent
#: run shipped a 6x11 = 66 sq ft dead pocket that slid under it.) Verified
#: against every shipped example compiled with its real base_dir: the only
#: void anywhere is the composed v2 showcase's, fixed alongside this change.
MIN_CONCENTRATED_VOID = 45.0
MIN_VOID_NOTE = 20.0
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
#: A closet at least this deep behind its door is a walk-in — a person steps
#: inside, so an ordinary person-door serves it. Shallower is a REACH-IN: you
#: stand at the opening and reach, so the door must open (nearly) the closet's
#: whole width — the centred-bifold idiom — or the rod past the jambs is dead.
CLOSET_WALKIN_DEPTH = 4.0
#: How far past a door jamb an arm usefully reaches into a reach-in closet.
CLOSET_REACH = 2.0
#: Clear depth a clothes rod needs — hanging clothes are 2 ft deep (24 in
#: hangers). A shallower closet is shelf-only (linen/broom) storage.
CLOSET_HANG_DEPTH = 2.0
#: A shop bay narrower than this (ft, shortest side) can't hold a vehicle or a
#: workbench wall plus a working aisle — it's storage mislabeled as a shop (the
#: shop analog of MIN_MUDROOM_WIDTH). Under the bar is a WARNING (it physically
#: can't do the job); between it and SHOP_COMFORT_DEPTH is an INFO comfort nudge.
#: A garage is exempt — garages are sized to cars, not equipment (GARAGE_TYPES).
#: The solver seed's shop floor is exactly 12 ft, so a strict `< 12` keeps it clean.
MIN_SHOP_DEPTH = 12.0
#: A shop narrower than this but at least MIN_SHOP_DEPTH is tight for a full-size
#: truck (8.5 ft wide + doors) plus a work zone along a wall — a comfort INFO.
SHOP_COMFORT_DEPTH = 20.0
#: The point of a barndo shop is clearance: an overhead door on a shop this short
#: (ft) or shorter is a residential-height panel that defeats a 12 ft+ bay — a
#: lift, RV or dually won't pass under it. INFO (an 8 ft door still opens; it's
#: the *height* of the bay that's wasted). Silent on garages (car-height doors).
SHOP_DOOR_MIN_HEIGHT = 8.0
#: IRC R305.1 habitable-room minimum ceiling height (ft). A loft is habitable and
#: often sleeps people, so a loft under this can't be lived in headroom-wise.
LOFT_MIN_CEILING = 7.0
#: An office must hold a desk (DESK_WIDTH x DESK_DEPTH) with a chair-pull behind
#: it, clear of door swings. Desk footprint + pull is the clear box the room needs
#: against a wall — mirrors the BED/DINING furniture-fit floors.
DESK_WIDTH = 4.0
DESK_DEPTH = 2.0
DESK_CHAIR_PULL = 3.0
#: New semantic room-type comfort floors. These are design-review heuristics,
#: not code minima; they keep the labels honest (safe_room, mechanical, etc.).
MIN_SAFE_ROOM_AREA = 24.0
MIN_SAFE_ROOM_DIM = 3.0
MAX_SAFE_ROOM_ASPECT = 2.5
MIN_MECH_AREA = 30.0
MIN_MECH_DIM = 5.0
MIN_FOYER_AREA = 25.0
MIN_FOYER_DIM = 4.0
MAX_FOYER_ASPECT = 2.5
MIN_GREAT_ROOM_AREA = 200.0
MIN_GREAT_ROOM_CEILING = 10.0
MIN_REC_ROOM_AREA = 120.0
MAX_STORAGE_ASPECT = 4.0
#: A covered porch shallower than this (ft) can't hold a chair (a rocker/seat is
#: ~2 ft deep) AND a walkway past it (~3 ft) — it's a stoop, not a sitting porch.
MIN_PORCH_DEPTH = 6.0
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
    _check_envelope_dimensions(plan, add)
    for room in plan.rooms:
        _check_room_dimensions(room, add)
    for index, wing in enumerate(plan.wings, start=1):
        _check_wing_dimensions(index, wing, add)


def _check_envelope_dimensions(plan: Barndominium, add) -> None:
    if not (_implausible_dim(plan.envelope_width) or _implausible_dim(plan.envelope_length)):
        return
    add(Issue(
        Severity.ERROR,
        "DIM_IMPLAUSIBLE",
        f"Envelope {_dim_desc(plan.envelope_width)} x {_dim_desc(plan.envelope_length)} ft "
        f"is implausible — each side must be finite and <= {MAX_PLAN_DIMENSION:.0f} ft.",
        hint=f"Use a realistic footprint in feet (each side <= {MAX_PLAN_DIMENSION:.0f}).",
    ))
    _clamp_implausible_attrs(plan, ("envelope_width", "envelope_length"))


def _check_room_dimensions(room: Room, add) -> None:
    if not (_implausible_dim(room.width) or _implausible_dim(room.length)):
        return
    add(Issue(
        Severity.ERROR,
        "DIM_IMPLAUSIBLE",
        f"Room size {_dim_desc(room.width)} x {_dim_desc(room.length)} ft is implausible — "
        f"each side must be finite and <= {MAX_PLAN_DIMENSION:.0f} ft.",
        room=room.id,
        hint=f"Use a realistic room size in feet (each side <= {MAX_PLAN_DIMENSION:.0f}).",
    ))
    _clamp_implausible_attrs(room, ("width", "length"))


def _check_wing_dimensions(index: int, wing, add) -> None:
    if not (_implausible_dim(wing.width) or _implausible_dim(wing.length)):
        return
    add(Issue(
        Severity.ERROR,
        "DIM_IMPLAUSIBLE",
        f"Wing #{index} size {_dim_desc(wing.width)} x {_dim_desc(wing.length)} ft "
        f"is implausible — each side must be finite and <= {MAX_PLAN_DIMENSION:.0f} ft.",
        hint=f"Use a realistic wing size in feet (each side <= {MAX_PLAN_DIMENSION:.0f}).",
    ))
    _clamp_implausible_attrs(wing, ("width", "length"))


def _clamp_implausible_attrs(obj, attrs: tuple[str, ...]) -> None:
    for attr in attrs:
        if _implausible_dim(getattr(obj, attr)):
            setattr(obj, attr, MAX_PLAN_DIMENSION)

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
PUBLIC_TYPES = {
    RoomType.LIVING,
    RoomType.GREAT_ROOM,
    RoomType.KITCHEN,
    RoomType.DINING,
    RoomType.REC_ROOM,
}
BATH_TYPES = {RoomType.BATHROOM, RoomType.HALF_BATH}
#: Dedicated-storage rooms, for the whole-house storage ratio (LOW_STORAGE) and
#: the `program storage <sqft>` minimum.
STORAGE_TYPES = {RoomType.CLOSET, RoomType.PANTRY, RoomType.STORAGE}
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
ZONE_PUBLIC_TYPES = {
    RoomType.LIVING,
    RoomType.GREAT_ROOM,
    RoomType.KITCHEN,
    RoomType.DINING,
    RoomType.REC_ROOM,
}
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


def door_graph(plan: Barndominium) -> dict[str, set[str]]:
    """Adjacency by interior doors (who can walk to whom).

    Public because the auto-layout asks the same circulation questions of a
    candidate plan that the design-quality checks ask of a compiled one.
    """
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


def building_corner(plan: Barndominium, room: Room, wall: Direction, at_end: bool) -> bool:
    """Is the given end of ``room``'s ``wall`` a *building* corner (vs an interior
    partition junction)? A corner is a building corner when the perpendicular wall
    meeting it there is also on the envelope.

    Public because the auto-layout places its windows against the SAME predicate
    WINDOW_PARTITION checks — a window may sit flush to a building corner but must
    keep a trim reveal off a partition junction.
    """
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

    Pocket/sliding/bifold/cased doors sweep no arc (a bifold folds flat against
    its jambs). A double/french pair sweeps two
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


def components_excluding(
    graph: dict[str, set[str]], excluded: set[str]
) -> list[set[str]]:
    """Connected components of the door graph with ``excluded`` rooms removed.

    Used to ask "if you couldn't walk through these rooms, what's still
    connected?" — the basis of the pass-through-a-private-room checks, and
    public so the auto-layout can apply the same test before compiling.
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
    _validate_wing_sizes(plan, add)
    if not _footprint_sections_connected(plan.footprint_sections(), tol):
        add(Issue(
            Severity.ERROR,
            "FOOTPRINT_SPLIT",
            "The footprint is disconnected — a wing doesn't share a wall with the rest "
            "of the building (a corner touch isn't enough).",
            hint="Reposition the wing so it abuts the envelope or another wing along a shared edge.",
        ))


def _validate_wing_sizes(plan: Barndominium, add) -> None:
    for index, wing in enumerate(plan.wings, 1):
        if wing.width > 0 and wing.length > 0:
            continue
        add(Issue(
            Severity.ERROR,
            "WING_SIZE",
            f"Wing #{index} has non-positive size ({_f(wing.width)}×{_f(wing.length)}).",
            hint="Use positive feet, e.g. `wing 20 x 24 at 40,0`.",
        ))


def _footprint_sections_connected(sections: list[tuple[float, float, float, float]], tol: float) -> bool:
    parent = list(range(len(sections)))
    for i in range(len(sections)):
        for j in range(i + 1, len(sections)):
            if _sections_abut(sections[i], sections[j], tol):
                parent[_find_parent(parent, i)] = _find_parent(parent, j)
    return len({_find_parent(parent, i) for i in range(len(sections))}) <= 1


def _sections_abut(s: tuple[float, float, float, float], t: tuple[float, float, float, float], tol: float) -> bool:
    ox = min(s[0] + s[2], t[0] + t[2]) - max(s[0], t[0])
    oy = min(s[1] + s[3], t[1] + t[3]) - max(s[1], t[1])
    return (ox > tol and oy > tol) or (abs(ox) <= tol and oy > tol) or (abs(oy) <= tol and ox > tol)


def _find_parent(parent: list[int], item: int) -> int:
    while parent[item] != item:
        parent[item] = parent[parent[item]]
        item = parent[item]
    return item

def validate(plan: Barndominium, profile: Profile | None = None) -> ValidationReport:
    """Run all checks and return a :class:`ValidationReport`.

    ``profile`` supplies the jurisdiction-variable code thresholds (see
    :mod:`barndsl.profiles`); ``None`` uses :data:`~barndsl.profiles.DEFAULT`,
    the IRC baseline, which is byte-identical to the pre-profile behaviour.
    """
    profile = profile or DEFAULT
    issues: list[Issue] = []
    add = issues.append

    _validate_plan_shell(plan, add, profile)
    _validate_site(plan, add)
    _validate_site_features(plan, add, profile)
    _validate_porch_guards(plan, add)

    if _validate_nonempty_plan(plan, add):
        return ValidationReport(issues)

    _validate_duplicate_room_ids(plan, add)
    _run_full_plan_validators(plan, add, profile)
    _validate_plan_has_bath(plan, add)
    return ValidationReport(issues)


def _validate_plan_shell(plan: Barndominium, add, profile: Profile) -> None:
    # Reject + clamp non-finite/absurd dimensions FIRST, so no later check or the
    # metrics takeoff ever sees an `inf`-poisoned value (see _check_dimensions).
    _check_dimensions(plan, add)
    if plan.envelope_width <= 0 or plan.envelope_length <= 0:
        add(Issue(
            Severity.ERROR,
            "ENVELOPE",
            "Envelope must have positive dimensions.",
            hint="Declare the footprint: `envelope 60 x 40`.",
        ))
    _check_wings(plan, add)
    _validate_ceiling_heights(plan, add, profile)


def _validate_ceiling_heights(plan: Barndominium, add, profile: Profile) -> None:
    min_ceiling = profile.min_ceiling_height
    ceil_tag = _profile_tag(profile, "min_ceiling_height", f"{MIN_CEILING:.0f} ft")
    if plan.ceiling_height < min_ceiling:
        add(Issue(
            Severity.ERROR,
            "CEILING",
            f"Ceiling height {_f(plan.ceiling_height)} ft is below the "
            f"{min_ceiling:g} ft minimum for habitable space{ceil_tag}.",
            hint=f"Set `ceiling {min_ceiling:g}` or greater (9–12 is typical).",
        ))
    for room in plan.rooms:
        _validate_room_ceiling_height(room, min_ceiling, ceil_tag, add)


def _validate_room_ceiling_height(room: Room, min_ceiling: float, ceil_tag: str, add) -> None:
    rc = getattr(room, "ceiling_height", None)
    if rc is None or getattr(room, "vaulted", False) or rc >= min_ceiling:
        return
    add(Issue(
        Severity.ERROR,
        "CEILING",
        f"Room '{room.id}' sets a {_f(rc)} ft ceiling, below the "
        f"{min_ceiling:g} ft minimum for habitable space{ceil_tag}.",
        room=room.id,
        hint=f"Raise its `ceiling` to >= {min_ceiling:g}, or drop the override to inherit the plan ceiling.",
    ))


def _validate_nonempty_plan(plan: Barndominium, add) -> bool:
    if plan.rooms:
        return False
    add(Issue(
        Severity.ERROR,
        "EMPTY",
        "Plan has no rooms.",
        hint="Add rooms: `room living: living at 0,0 size 20 x 16`.",
    ))
    return True


def _validate_duplicate_room_ids(plan: Barndominium, add) -> None:
    # Counter is O(n); the old ``ids.count(i)`` per id was O(n²) on big plans.
    id_counts = Counter(r.id for r in plan.rooms)
    for dup_id in sorted({i for i, c in id_counts.items() if c > 1}):
        add(Issue(
            Severity.ERROR,
            "DUP_ID",
            f"Duplicate room id '{dup_id}'.",
            room=dup_id,
            hint="Give each room a unique id.",
        ))


def _run_full_plan_validators(plan: Barndominium, add, profile: Profile) -> None:
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
    _validate_program_area_overrun(plan, add)
    _validate_requirements(plan, add)
    _validate_walls(plan, add)
    _validate_suites_zones(plan, add)
    _validate_structure(plan, add)
    _validate_finishes(plan, add)
    _validate_notes(plan, add)
    # Local import: fixtures.py imports clear_box from this module.
    from .fixtures import validate_fixtures

    validate_fixtures(plan, add)


def _validate_plan_has_bath(plan: Barndominium, add) -> None:
    if plan.metrics()["bathroom_count"]:
        return
    add(Issue(
        Severity.WARNING,
        "NO_BATH",
        "Plan has no bathroom.",
        hint="Add a bathroom, e.g. `room bath: bathroom at ... size 8 x 8`.",
    ))

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
    """Check a declared ``site`` / ``setback`` against the building footprint."""
    ss = getattr(plan, "site_spec", None)
    if ss is None:
        return
    if not ss.has_dims:
        _validate_site_without_dimensions(ss, add)
        return
    if _validate_site_dimensions(ss, add):
        return
    if ss.has_building:
        _validate_site_placed(plan, ss, add)
    elif ss.has_setback:
        _validate_site_setback_fit(plan, ss, add)


def _validate_site_without_dimensions(ss, add) -> None:
    if ss.has_setback:
        add(Issue(
            Severity.ERROR,
            "SETBACK_NO_SITE",
            "A `setback` was declared but there is no `site` to measure it against.",
            hint="Add the lot dimensions with `site <W> x <L>` (feet), or drop the `setback`.",
            line=ss.setback_line,
            col=ss.setback_col,
            end_col=ss.setback_end_col,
        ))
    if ss.has_features:
        _validate_orphan_site_features(ss, add)


def _validate_orphan_site_features(ss, add) -> None:
    # A drive/walk/well/septic/service places itself in lot feet, so it needs the
    # lot dimensions to sit on. One error per orphaned feature.
    for feature, keyword in _orphan_site_features(ss):
        add(Issue(
            Severity.ERROR,
            "SITE_REQUIRED",
            f"A `{keyword}` was declared but there is no `site` to place it on.",
            line=feature.line,
            col=feature.col,
            end_col=feature.end_col,
            hint="Add the lot dimensions with `site <W> x <L>` (feet) so the feature has a lot to sit on.",
        ))


def _validate_site_dimensions(ss, add) -> bool:
    # A degenerate lot is an authoring error whether or not setbacks follow —
    # mirror the ENVELOPE check's stance on non-positive/non-finite dims.
    bad_dims = not (math.isfinite(ss.width) and math.isfinite(ss.length) and ss.width > EPSILON and ss.length > EPSILON)
    bad_setbacks = any(v is not None and (not math.isfinite(v) or v < 0.0) for v in (ss.front, ss.side, ss.rear))
    if not (bad_dims or bad_setbacks):
        return False
    add(Issue(
        Severity.ERROR,
        "SITE",
        "Invalid site declaration: " + " and ".join(_site_dimension_problems(ss, bad_dims, bad_setbacks)) + ".",
        line=ss.line,
        col=ss.col,
        end_col=ss.end_col,
        hint="`site <W> x <L>` needs positive lot dimensions and `setback` values can't be negative.",
    ))
    return True


def _site_dimension_problems(ss, bad_dims: bool, bad_setbacks: bool) -> list[str]:
    problems = []
    if bad_dims:
        problems.append(f"lot dimensions {_f(ss.width)} x {_f(ss.length)} ft")
    if bad_setbacks:
        problems.append("negative setback value(s)")
    return problems


def _validate_site_setback_fit(plan: Barndominium, ss, add) -> None:
    problems = _site_setback_fit_problems(plan, ss)
    if not problems:
        return
    add(Issue(
        Severity.ERROR,
        "SETBACK",
        "The building footprint doesn't fit the buildable area: " + "; ".join(problems) + ".",
        hint="Shrink the footprint, enlarge the `site`, or reduce the `setback` — the "
        "footprint's bounding box (building + porches) must fit inside the lot minus its setbacks.",
        line=ss.setback_line or ss.line,
        col=ss.setback_col or ss.col,
        end_col=ss.setback_end_col or ss.end_col,
    ))


def _site_setback_fit_problems(plan: Barndominium, ss) -> list[str]:
    front, side, rear = ss.front or 0.0, ss.side or 0.0, ss.rear or 0.0
    buildable_w = ss.width - 2.0 * side
    buildable_l = ss.length - front - rear
    minx, miny, maxx, maxy = _site_footprint_bounds(plan)
    fp_w, fp_l = maxx - minx, maxy - miny
    problems: list[str] = []
    if buildable_w <= EPSILON or fp_w > buildable_w + EPSILON:
        problems.append(
            f"it is {_f(fp_w)} ft wide but only {_f(max(0.0, buildable_w))} ft is "
            f"buildable east-west ({_f(ss.width)} ft lot − 2 × {_f(side)} ft side)"
        )
    if buildable_l <= EPSILON or fp_l > buildable_l + EPSILON:
        problems.append(
            f"it is {_f(fp_l)} ft deep but only {_f(max(0.0, buildable_l))} ft is "
            f"buildable north-south ({_f(ss.length)} ft lot − {_f(front)} ft front − {_f(rear)} ft rear)"
        )
    return problems

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
    """The site-plan v2 checks: well/septic separation, drive access, and setbacks."""
    ss = getattr(plan, "site_spec", None)
    if ss is None or not ss.has_dims or not ss.has_features:
        return
    origin = plan.building_origin_on_lot()
    if origin is None:
        return
    bx, by = origin
    _validate_site_walk_refs(plan, ss, add)
    _validate_well_septic_clearance(ss, add)
    _validate_drive_door_access(plan, ss, bx, by, add)
    _validate_drive_overlaps(ss, add)
    _validate_septic_setback(ss, add)


def _validate_site_walk_refs(plan: Barndominium, ss, add) -> None:
    room_ids = {room.id for room in plan.rooms}
    for walk in ss.walks:
        if walk.room in room_ids:
            continue
        add(Issue(
            Severity.ERROR,
            "SITE_REF",
            f"`walk` names unknown room '{walk.room}'.",
            line=walk.line,
            col=walk.col,
            end_col=walk.end_col,
            hint="Name a room that exists and has an exterior door, e.g. `walk from mud to drive`.",
        ))


def _validate_well_septic_clearance(ss, add) -> None:
    sep = WELL_SEPTIC_MIN_SEPARATION
    for well in ss.wells:
        for septic in ss.septics:
            dist = min(_pt_rect_dist(well.x, well.y, rect) for rect in septic.rects())
            if dist < sep - EPSILON:
                _add_well_septic_issue(well, septic, dist, sep, add)


def _add_well_septic_issue(well, septic, dist: float, sep: float, add) -> None:
    add(Issue(
        Severity.WARNING,
        "WELL_SEPTIC_CLEAR",
        f"The well is {_f(dist)} ft from the septic {'field/tank' if septic.has_field else 'tank'} — "
        f"the common health-department rule wants at least {_f(sep)} ft between a private well and a septic system.",
        line=well.line,
        col=well.col,
        end_col=well.end_col,
        hint=f"Move the well or septic so they are >= {_f(sep)} ft apart (confirm the exact separation "
        "with your county health department — it varies).",
    ))


def _validate_drive_door_access(plan: Barndominium, ss, bx: float, by: float, add) -> None:
    if not ss.drives:
        return
    door_pts = _exterior_door_points(plan, bx, by)
    drive_rects = [(drive.x, drive.y, drive.x2, drive.y2) for drive in ss.drives]
    served = bool(ss.walks) or any(
        min(_pt_rect_dist(px, py, rect) for rect in drive_rects) <= DRIVE_DOOR_REACH + EPSILON
        for _, (px, py) in door_pts
    )
    if door_pts and not served:
        add(Issue(
            Severity.INFO,
            "DRIVE_DOOR",
            "The plan has a drive but no walk or drive edge reaches an exterior door — "
            "guests arrive at the drive and have no path to a door.",
            hint="Add `walk from <room> to drive` from an entry room, or extend the drive to within a few feet of a door.",
        ))


def _validate_drive_overlaps(ss, add) -> None:
    for index, d1 in enumerate(ss.drives):
        for d2 in ss.drives[index + 1:]:
            overlap = _drive_overlap_area(d1, d2)
            if overlap > EPSILON:
                add(Issue(
                    Severity.WARNING,
                    "SITE_OVERLAP",
                    f"Two driveways overlap by {_f(overlap)} sqft ({d1.surface} & {d2.surface}); "
                    "the cost estimate double-counts the shared paving.",
                    line=d2.line,
                    col=d2.col,
                    end_col=d2.end_col,
                    hint="Merge the drives into one rectangle, or move them apart so each patch of paving is declared once.",
                ))


def _drive_overlap_area(d1, d2) -> float:
    ox = min(d1.x2, d2.x2) - max(d1.x, d2.x)
    oy = min(d1.y2, d2.y2) - max(d1.y, d2.y)
    return ox * oy if ox > EPSILON and oy > EPSILON else 0.0


def _validate_septic_setback(ss, add) -> None:
    bands = _setback_bands(ss)
    for septic in ss.septics:
        hit = _septic_setback_hit(septic, bands)
        if hit:
            add(Issue(
                Severity.INFO,
                "SEPTIC_SETBACK",
                f"The septic system sits inside the {hit} setback band — septic tanks and "
                "drain fields are usually held out of the required yards too.",
                line=septic.line,
                col=septic.col,
                end_col=septic.end_col,
                hint="Move the septic clear of the setback, or confirm the allowed septic setback with your county health department.",
            ))


def _setback_bands(ss) -> list[tuple[str, float, float, float, float]]:
    lot_w, lot_l = ss.width, ss.length
    front, rear, side = ss.front or 0.0, ss.rear or 0.0, ss.side or 0.0
    bands: list[tuple[str, float, float, float, float]] = []
    if front > 0:
        bands.append(("front", 0.0, 0.0, lot_w, front))
    if rear > 0:
        bands.append(("rear", 0.0, lot_l - rear, lot_w, lot_l))
    if side > 0:
        bands.append(("west side", 0.0, 0.0, side, lot_l))
        bands.append(("east side", lot_w - side, 0.0, lot_w, lot_l))
    return bands


def _septic_setback_hit(septic, bands: list[tuple[str, float, float, float, float]]) -> str | None:
    for rx1, ry1, rx2, ry2 in septic.rects():
        for label, bx1, by1, bx2, by2 in bands:
            if min(rx2, bx2) - max(rx1, bx1) > EPSILON and min(ry2, by2) - max(ry1, by1) > EPSILON:
                return label
    return None

def _validate_porch_guards(plan: Barndominium, add) -> None:
    """IRC R312.1 — when the finish floor sits more than 30 in above finished
    grade, every porch is a walking surface that needs a guard. Fires once per
    porch; silent when no ``grade`` is declared or grade <= 30 in (so an at-grade
    or shallow-grade plan is never nagged). This resolves the Phase 13 skip:
    R312.1 exterior guards were unexpressible without a grade elevation.

    barndsl has two porch representations — the :class:`~barndsl.elements.Porch`
    platform (``porch <id> at ...``) and a ``RoomType.PORCH`` room. The guard
    trigger is the plan-wide ``grade`` (finish floor above grade), shared by both,
    so a raised room-typed porch is just as much an above-grade walking surface as
    a platform porch — both are checked. (A room-porch's ``line``/``col`` also give
    the diagnostic a caret the platform porch can't.)"""
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
    for room in plan.rooms:
        if room.type is not RoomType.PORCH:
            continue
        add(
            Issue(
                Severity.WARNING,
                "PORCH_GUARD",
                f"Porch '{room.id}' sits {_f(grade)} ft above grade "
                f"(> {GUARD_DROP_TRIGGER * 12:.0f} in), so it needs a "
                f"{GUARD_HEIGHT * 12:.0f} in guard (IRC R312.1).",
                room=room.id,
                hint=f"Add a {GUARD_HEIGHT * 12:.0f} in guard along the porch's open "
                "edges (balusters blocking a 4 in sphere) and note it on the drawings.",
            )
        )


def _largest_void(plan: Barndominium) -> tuple[float, tuple[float, float, float, float] | None]:
    """The largest *connected* patch of footprint assigned to no room."""
    sections = plan.footprint_sections()
    if not sections:
        return 0.0, None
    rooms = [room for room in plan.rooms if room.level == 0]
    xg, yg = _void_grid_axes(sections, rooms)
    nx, ny = len(xg) - 1, len(yg) - 1
    if nx <= 0 or ny <= 0:
        return 0.0, None
    open_cell = _open_void_cells(sections, rooms, xg, yg)
    return _largest_open_component(open_cell, xg, yg)


def _void_grid_axes(
    sections: list[tuple[float, float, float, float]], rooms: list[Room],
) -> tuple[list[float], list[float]]:
    xs: set[float] = set()
    ys: set[float] = set()
    for x, y, width, length in sections:
        xs.update((x, x + width))
        ys.update((y, y + length))
    for room in rooms:
        xs.update((room.x, room.x2))
        ys.update((room.y, room.y2))
    return sorted(xs), sorted(ys)


def _open_void_cells(
    sections: list[tuple[float, float, float, float]], rooms: list[Room], xg: list[float], yg: list[float],
) -> list[list[bool]]:
    nx, ny = len(xg) - 1, len(yg) - 1
    open_cell = [[False] * ny for _ in range(nx)]
    for i in range(nx):
        cx = (xg[i] + xg[i + 1]) / 2.0
        for j in range(ny):
            cy = (yg[j] + yg[j + 1]) / 2.0
            open_cell[i][j] = _cell_is_open_void(sections, rooms, cx, cy)
    return open_cell


def _cell_is_open_void(sections: list[tuple[float, float, float, float]], rooms: list[Room], cx: float, cy: float) -> bool:
    if not point_in_footprint(sections, cx, cy):
        return False
    return not any(room.x <= cx <= room.x2 and room.y <= cy <= room.y2 for room in rooms)


def _largest_open_component(open_cell: list[list[bool]], xg: list[float], yg: list[float]) -> tuple[float, tuple[float, float, float, float] | None]:
    nx, ny = len(xg) - 1, len(yg) - 1
    seen = [[False] * ny for _ in range(nx)]
    best_area = 0.0
    best_bbox: tuple[float, float, float, float] | None = None
    for i in range(nx):
        for j in range(ny):
            if seen[i][j] or not open_cell[i][j]:
                continue
            area, bbox = _flood_open_component(i, j, open_cell, seen, xg, yg)
            if area > best_area:
                best_area, best_bbox = area, bbox
    return best_area, best_bbox


def _flood_open_component(
    start_i: int,
    start_j: int,
    open_cell: list[list[bool]],
    seen: list[list[bool]],
    xg: list[float],
    yg: list[float],
) -> tuple[float, tuple[float, float, float, float]]:
    nx, ny = len(xg) - 1, len(yg) - 1
    stack = [(start_i, start_j)]
    area = 0.0
    minx = miny = math.inf
    maxx = maxy = -math.inf
    while stack:
        i, j = stack.pop()
        if seen[i][j] or not open_cell[i][j]:
            continue
        seen[i][j] = True
        area += (xg[i + 1] - xg[i]) * (yg[j + 1] - yg[j])
        minx, maxx = min(minx, xg[i]), max(maxx, xg[i + 1])
        miny, maxy = min(miny, yg[j]), max(maxy, yg[j + 1])
        stack.extend(_unseen_neighbors(i, j, nx, ny, seen))
    return area, (minx, miny, maxx, maxy)


def _unseen_neighbors(i: int, j: int, nx: int, ny: int, seen: list[list[bool]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        ni, nj = i + di, j + dj
        if 0 <= ni < nx and 0 <= nj < ny and not seen[ni][nj]:
            out.append((ni, nj))
    return out

def _validate_geometry(plan: Barndominium, add) -> None:
    _validate_room_geometries(plan, add)
    _validate_room_overlaps(plan, add)
    _validate_footprint_coverage(plan, add)


def _validate_room_geometries(plan: Barndominium, add) -> None:
    for room in plan.rooms:
        if _room_has_nonfinite_geometry(room, add):
            continue  # skip further geometry checks — NaN defeats every comparison
        _validate_positive_room_size(room, add)
        _validate_room_in_footprint(plan, room, add)


def _room_has_nonfinite_geometry(room: Room, add) -> bool:
    if all(math.isfinite(v) for v in (room.x, room.y, room.width, room.length)):
        return False
    add(Issue(
        Severity.ERROR,
        "ROOM_GEOMETRY",
        "Room has non-finite coordinates or size.",
        room=room.id,
        hint="Use finite measurements in feet (no nan/inf).",
    ))
    return True


def _validate_positive_room_size(room: Room, add) -> None:
    if room.width > 0 and room.length > 0:
        return
    add(Issue(
        Severity.ERROR,
        "ROOM_SIZE",
        "Room has non-positive size.",
        room=room.id,
        hint="Use positive feet, e.g. `size 12 x 10`.",
    ))


def _validate_room_in_footprint(plan: Barndominium, room: Room, add) -> None:
    if plan.wings:
        _validate_room_in_rectilinear_footprint(plan, room, add)
    else:
        _validate_room_in_envelope(plan, room, add)


def _validate_room_in_rectilinear_footprint(plan: Barndominium, room: Room, add) -> None:
    # Rectilinear footprint: the building may extend past the primary envelope
    # into wings, and have notches the bbox doesn't, so test the actual union.
    if room.width <= 0 or room.length <= 0:
        return
    if rect_in_footprint(plan.footprint_sections(), room.x, room.y, room.width, room.length):
        return
    add(Issue(
        Severity.ERROR,
        "OUT_OF_BOUNDS",
        f"Room falls outside the building footprint ({_f(room.x)},{_f(room.y)} → "
        f"{_f(room.x2)},{_f(room.y2)}); it isn't covered by the envelope or any wing.",
        room=room.id,
        hint="Move it inside a footprint block, or add a `wing` to cover that area.",
    ))


def _validate_room_in_envelope(plan: Barndominium, room: Room, add) -> None:
    over_x = max(0.0, room.x2 - plan.envelope_width)
    over_y = max(0.0, room.y2 - plan.envelope_length)
    if room.x >= -EPSILON and room.y >= -EPSILON and over_x <= EPSILON and over_y <= EPSILON:
        return
    add(Issue(
        Severity.ERROR,
        "OUT_OF_BOUNDS",
        f"Room extends outside the {_f(plan.envelope_width)}×{_f(plan.envelope_length)} ft envelope "
        f"({_f(room.x)},{_f(room.y)} → {_f(room.x2)},{_f(room.y2)}).",
        room=room.id,
        hint="; ".join(_room_oob_fixes(plan, room, over_x, over_y)) + ".",
    ))


def _room_oob_fixes(plan: Barndominium, room: Room, over_x: float, over_y: float) -> list[str]:
    # Phrase the fix differently for a relatively-placed room, which has no x,y
    # token to "set" — point at align/offset/size instead.
    relative = room.placement is not None
    fixes = []
    if not relative and room.x < 0:
        fixes.append("set its x to >= 0")
    if not relative and room.y < 0:
        fixes.append("set its y to >= 0")
    if over_x > EPSILON:
        fixes.append(_room_oob_axis_fix("width", "west", "x", plan.envelope_width - room.width, over_x, relative))
    if over_y > EPSILON:
        fixes.append(_room_oob_axis_fix("length", "south", "y", plan.envelope_length - room.length, over_y, relative))
    if relative:
        fixes.append(
            f"adjust its align/offset or size, or pin it with `at x,y` (it's placed `{room.placement}`)"
        )
    return fixes


def _room_oob_axis_fix(kind: str, direction: str, axis: str, move_to: float, over: float, relative: bool) -> str:
    move = "" if relative or move_to < 0 else f" or move it {direction} to {axis}={_f(move_to)}"
    return f"reduce its {kind} by {_f(over)} ft" + move


def _validate_room_overlaps(plan: Barndominium, add) -> None:
    # Only rooms whose bounding boxes intersect can overlap, so walk the index's
    # candidate pairs (in the same ascending (i, j) order the old nested loop
    # visited) rather than all n²/2 pairs.
    rooms = plan.rooms
    index = room_index(plan)
    for i, j in index.candidate_pairs():
        a, b = rooms[i], rooms[j]
        if a.level == b.level:
            _validate_room_pair_overlap(plan, a, b, add)


def _validate_room_pair_overlap(plan: Barndominium, a: Room, b: Room, add) -> None:
    ov = a.overlaps(b)
    if ov <= 0.5:  # ignore hairline floating-point overlaps
        return
    add(Issue(
        Severity.ERROR,
        "OVERLAP",
        f"Rooms '{a.id}' and '{b.id}' overlap by {_f(ov)} sq ft.",
        room=a.id,
        hint=f"Reposition so they don't intersect — e.g. {_overlap_suggestion(plan, a, b)}.{_overlap_note(a, b)}",
    ))


def _overlap_suggestion(plan: Barndominium, a: Room, b: Room) -> str:
    # Prefer a fix that keeps 'b' inside the envelope.
    east_fits = a.x2 + b.width <= plan.envelope_width + EPSILON
    north_fits = a.y2 + b.length <= plan.envelope_length + EPSILON
    ox = min(a.x2, b.x2) - max(a.x, b.x)
    oy = min(a.y2, b.y2) - max(a.y, b.y)
    prefer_east = (ox <= oy and east_fits) or (not north_fits and east_fits)
    if prefer_east:
        return f"move '{b.id}' to x={_f(a.x2)} (east of '{a.id}')"
    if north_fits:
        return f"move '{b.id}' to y={_f(a.y2)} (north of '{a.id}')"
    return "shrink one of them or enlarge the envelope"


def _overlap_note(a: Room, b: Room) -> str:
    # A collision is often a relative-anchor chain pushing a room onto one already
    # placed — surface that so the fix is re-anchoring, not guessing at a shrink.
    placed = next((r for r in (b, a) if r.placement is not None), None)
    if placed is None:
        return ""
    return (
        f" ('{placed.id}' is placed `{placed.placement}` — re-anchor it one room deep "
        "off a spine, or pin it with `at x,y`)"
    )


def _validate_footprint_coverage(plan: Barndominium, add) -> None:
    # Footprint coverage is a ground-floor (level 0) concept; lofts sit above.
    used = sum(r.area for r in plan.rooms if r.level == 0)
    if not (plan.footprint_area > 0 and math.isfinite(used) and math.isfinite(plan.footprint_area)):
        return
    frac = used / plan.footprint_area
    _validate_footprint_area_fraction(plan, used, frac, add)
    _validate_concentrated_void(plan, frac, add)


def _validate_footprint_area_fraction(plan: Barndominium, used: float, frac: float, add) -> None:
    if frac > 1.001:
        add(Issue(
            Severity.WARNING,
            "AREA_OVERFLOW",
            f"Assigned room area ({_f(used)} sq ft) exceeds the footprint ({_f(plan.footprint_area)} sq ft).",
            hint="Shrink rooms or enlarge the envelope; rooms likely overlap.",
        ))
    elif frac < 0.85:
        add(Issue(
            Severity.INFO,
            "AREA_UNUSED",
            f"Only {frac * 100:.0f}% of the footprint is assigned to rooms; "
            f"{_f(plan.footprint_area - used)} sq ft unallocated.",
            hint="Enlarge rooms or add spaces to fill the footprint.",
        ))


def _validate_concentrated_void(plan: Barndominium, frac: float, add) -> None:
    # A concentrated void — one connected room-sized patch of dead space — slips
    # past AREA_UNUSED whenever overall coverage is high. Flag the largest single
    # gap on its own so a room-sized rectangle of nothing is still visible.
    if frac > 1.001:
        return
    void_area, bbox = _largest_void(plan)
    if void_area < MIN_VOID_NOTE or bbox is None:
        return
    x1, y1, x2, y2 = bbox
    add(Issue(
        _void_severity(void_area, frac),
        "AREA_VOID",
        f"A single {_f(void_area)} sq ft patch of the footprint (around {_f(x1)},{_f(y1)} "
        f"to {_f(x2)},{_f(y2)}) is assigned to no room — a concentrated void, not diffuse slack.",
        hint="Every enclosed square foot must belong to a room: extend an adjacent room to cover "
        "the patch, or declare a room there (storage, closet, pantry, utility), or shrink the "
        "envelope so the footprint has no dead pocket.",
    ))


def _void_severity(void_area: float, frac: float) -> Severity:
    # Severity is tiered: in a substantially-tiled plan (>= 85% coverage — the
    # same bar that silences AREA_UNUSED) a room-sized void is an ERROR. A smaller
    # pocket, or any void in a sparse plan, stays an INFO nudge.
    if void_area >= MIN_CONCENTRATED_VOID and frac >= 0.85:
        return Severity.ERROR
    return Severity.INFO

def _validate_accessibility(plan: Barndominium, add) -> None:
    """Opt-in accessibility / aging-in-place nudges (ANSI A117.1-flavoured)."""
    if not plan.accessible:
        return
    by_id = {room.id: room for room in plan.rooms}
    _add_access_entry_reminder(add)
    _validate_accessible_interior_doors(plan, by_id, add)
    _validate_accessible_exterior_doors(plan, by_id, add)
    _validate_accessible_baths(plan, add)
    _validate_single_floor_living(plan, add)


def _add_access_entry_reminder(add) -> None:
    # Thresholds aren't in the geometry, so a no-step entrance is a reminder.
    add(Issue(
        Severity.INFO,
        "ACCESS_ENTRY",
        "Accessible target: provide at least one no-step entrance (threshold ≤ ½ in) "
        "with a level 5 ft × 5 ft landing (ANSI A117.1).",
        hint="Make the main entry no-step — a slab-on-grade helps; avoid a stoop step.",
    ))


def _validate_accessible_interior_doors(plan: Barndominium, by_id: dict[str, Room], add) -> None:
    # Accessible clear widths on the living route (not the garage/shop door).
    for door in plan.interior_doors:
        a, b = by_id.get(door.room_a), by_id.get(door.room_b)
        if a is None or b is None or a.type in GARAGE_TYPES or b.type in GARAGE_TYPES:
            continue
        _validate_accessible_interior_door(door, add)


def _validate_accessible_interior_door(door, add) -> None:
    # A double/french pair travels through ONE leaf, so it counts half.
    double = door.kind in DOUBLE_LEAF_KINDS
    effective = door.width / 2.0 if double else door.width
    need = ACCESSIBLE_LEAF_MIN if door.leaf else ACCESSIBLE_CLEAR_DOOR
    if effective + EPSILON >= need:
        return
    kind, need_in = ("door", "34 in leaf") if door.leaf else ("opening", "32 in")
    add(Issue(
        Severity.INFO,
        "ACCESS_DOOR",
        f"The {kind} between '{door.room_a}' and '{door.room_b}' is {_f(effective * 12)} "
        f"in wide{' per leaf' if double else ''}; an accessible route needs a {need_in} "
        "(32 in clear, ANSI A117.1 §404).",
        hint=f"Widen it to ≥ {need_in.split()[0]} in" + (" per leaf" if double else "") + ".",
    ))


def _validate_accessible_exterior_doors(plan: Barndominium, by_id: dict[str, Room], add) -> None:
    for door in plan.exterior_doors:
        room = by_id.get(door.room)
        if room is not None and room.type in GARAGE_TYPES:
            continue
        _validate_accessible_exterior_door(door, add)


def _validate_accessible_exterior_door(door, add) -> None:
    clear = _door_clear_width(door)
    if clear + EPSILON >= ACCESSIBLE_EXTERIOR_MIN:
        return
    double = getattr(door, "kind", "entry") in DOUBLE_LEAF_KINDS
    add(Issue(
        Severity.INFO,
        "ACCESS_DOOR",
        f"The exterior door on '{door.room}' is {_f(clear * 12)} in wide"
        f"{' per leaf' if double else ''}; an accessible entrance wants a 36 in door.",
        room=door.room,
        hint="Use a 36 in exterior door" + (" leaf" if double else "") + " on the accessible entrance.",
    ))


def _validate_accessible_baths(plan: Barndominium, add) -> None:
    for room in plan.rooms:
        if room.type is RoomType.BATHROOM and room.level == 0:
            _validate_accessible_bath(plan, room, add)


def _validate_accessible_bath(plan: Barndominium, room: Room, add) -> None:
    clear_w, clear_l = clear_dimensions(plan, room)
    short = min(clear_w, clear_l)
    if short + EPSILON >= ACCESSIBLE_TURN:
        return
    add(Issue(
        Severity.INFO,
        "ACCESS_BATH",
        f"Bathroom '{room.id}' has ~{_f(short)} ft clear on its short side; a wheelchair "
        f"turning space needs {_f(ACCESSIBLE_TURN)} ft (60 in) — plan a roll-in shower "
        "and grab-bar blocking too (ANSI A117.1).",
        room=room.id,
        hint=f"Widen the bath so the clear short side is ≥ {_f(ACCESSIBLE_TURN)} ft.",
    ))


def _validate_single_floor_living(plan: Barndominium, add) -> None:
    ground = [room for room in plan.rooms if room.level == 0]
    has_bed = any(room.type is RoomType.BEDROOM for room in ground)
    has_bath = any(room.type is RoomType.BATHROOM for room in ground)
    if has_bed and has_bath:
        return
    missing = " and ".join(label for label, ok in (("a bedroom", has_bed), ("a full bath", has_bath)) if not ok)
    add(Issue(
        Severity.INFO,
        "ACCESS_SINGLE_FLOOR",
        f"The entry level has no {missing}; accessible / aging-in-place living wants "
        "a bedroom and a full bath on one no-stair floor.",
        hint="Place a primary bedroom and a full bath on the ground level.",
    ))

def _validate_fixtures(plan: Barndominium, add) -> None:
    """Check that wet rooms and kitchens can actually hold their fixtures with
    code clearances (IRC R307 for the bath; a working aisle for the kitchen).

    Uses the clear (finish-face) interior, so the check reflects the built room,
    not the nominal rectangle. A bath that can't fit toilet/lav/tub with
    clearances is a ``BATH_CLEARANCE`` warning; a laundry that can't load its
    washer/dryer is a ``LAUNDRY_FIT`` warning; a cramped kitchen is a
    ``KITCHEN_FIT`` info.
    """
    from .fixtures import fixtures_fit  # lazy: fixtures imports back from here

    for room in plan.rooms:
        if room.type not in (
            RoomType.BATHROOM, RoomType.HALF_BATH, RoomType.KITCHEN,
            RoomType.LAUNDRY,
        ):
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
        elif room.type is RoomType.LAUNDRY:
            add(
                Issue(
                    Severity.WARNING,
                    "LAUNDRY_FIT",
                    f"Laundry '{room.id}' can't fit its washer/dryer with a "
                    f"working aisle: {reason}.",
                    room=room.id,
                    hint="A laundry wants ~5.5 ft of clear depth — a washer/dryer "
                    "(2.25 ft deep) plus a 3 ft aisle to load them; widen the "
                    "room or fold it into a bigger mudroom/utility.",
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
    """Flag a storage-poor plan — dedicated storage (closets, pantry, storage rooms)
    below a small fraction of the conditioned area. Deterministic and unconditional, but the floor
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
                f"Dedicated storage (closets + pantry + storage rooms) is {storage:.0f} sq ft — "
                f"{storage / interior * 100:.1f}% of the conditioned area, a "
                "storage-poor plan.",
                hint="Add closets, a pantry or a storage room — a linen closet by "
                "the baths, a coat closet at the entry, or bulk storage off a hall.",
            )
        )


def _validate_room_programs(plan: Barndominium, add, profile: Profile = DEFAULT) -> None:
    for room in plan.rooms:
        if room.type is RoomType.BEDROOM:
            _validate_bedroom_program(room, add, profile)
        elif room.type in R304_HABITABLE_TYPES:
            _validate_habitable_room_program(room, add, profile)
        _validate_hallway_width(room, add, profile)
        _validate_room_tightness(room, add)
        _check_clear_dimension(plan, room, add, profile)


def _validate_bedroom_program(room: Room, add, profile: Profile) -> None:
    bed_area = profile.min_bedroom_area
    bed_dim = profile.min_bedroom_dimension
    if room.area < bed_area:
        tag = _profile_tag(profile, "min_bedroom_area", f"{MIN_BEDROOM_AREA:.0f} sq ft")
        min_label = f"IRC minimum is {bed_area:g} sq ft" if not tag else f"the enforced minimum is {bed_area:g} sq ft"
        add(Issue(
            Severity.ERROR,
            "BEDROOM_AREA",
            f"Bedroom is {_f(room.area)} sq ft; {min_label}{tag}.",
            room=room.id,
            hint=_area_enlarge_hint(room, bed_area),
        ))
    if room.min_dimension < bed_dim:
        tag = _profile_tag(profile, "min_bedroom_dimension", f"{MIN_BEDROOM_DIMENSION:.0f} ft")
        add(Issue(
            Severity.ERROR,
            "BEDROOM_DIM",
            f"Bedroom's smallest dimension is {_f(room.min_dimension)} ft; minimum is {bed_dim:g} ft{tag}.",
            room=room.id,
            hint=f"Make both dimensions >= {bed_dim:g} ft.",
        ))


def _validate_habitable_room_program(room: Room, add, profile: Profile) -> None:
    # R304 habitable-room minimums (WARNING): 70 sq ft floor + 7 ft in every
    # horizontal dimension. Bedrooms are handled above as errors and kitchens are exempt.
    bed_area = profile.min_bedroom_area
    bed_dim = profile.min_bedroom_dimension
    if room.area < bed_area:
        tag = _profile_tag(profile, "min_bedroom_area", f"{MIN_BEDROOM_AREA:.0f} sq ft")
        add(Issue(
            Severity.WARNING,
            "ROOM_HABITABLE",
            f"{_room_kind(room)} '{room.id}' is {_f(room.area)} sq ft; the R304 minimum "
            f"habitable area is {bed_area:g} sq ft{tag}.",
            room=room.id,
            hint=f"Enlarge it to >= {bed_area:g} sq ft{_area_sizing_suffix(room, bed_area)}.",
        ))
    if room.min_dimension < bed_dim:
        tag = _profile_tag(profile, "min_bedroom_dimension", f"{MIN_BEDROOM_DIMENSION:.0f} ft")
        add(Issue(
            Severity.WARNING,
            "ROOM_HABITABLE",
            f"{_room_kind(room)} '{room.id}' is {_f(room.min_dimension)} ft on its smallest "
            f"dimension; the R304 minimum is {bed_dim:g} ft{tag}.",
            room=room.id,
            hint=f"Make both dimensions >= {bed_dim:g} ft.",
        ))


def _validate_hallway_width(room: Room, add, profile: Profile) -> None:
    hall_width = profile.min_hallway_width
    if room.type is not RoomType.HALLWAY or room.min_dimension >= hall_width:
        return
    tag = _profile_tag(profile, "min_hallway_width", f"{MIN_HALLWAY_WIDTH:.0f} ft")
    add(Issue(
        Severity.ERROR,
        "HALL_WIDTH",
        f"Hallway is {_f(room.min_dimension)} ft wide; minimum is {hall_width:g} ft{tag}.",
        room=room.id,
        hint=f"Widen it to >= {hall_width:g} ft.",
    ))


def _validate_room_tightness(room: Room, add) -> None:
    floor = MIN_USABLE_AREA.get(room.type)
    short_floor = MIN_ROOM_SHORT_SIDE.get(room.type)
    if floor is not None and 0 < room.area < floor:
        add(Issue(
            Severity.INFO,
            "ROOM_TIGHT",
            f"{_room_kind(room)} '{room.id}' is {_f(room.area)} sq ft; a workable "
            f"{room.type.value.replace('_', ' ')} wants about {floor:.0f} sq ft.",
            room=room.id,
            hint=f"Enlarge it to >= {floor:.0f} sq ft{_area_sizing_suffix(room, floor)}.",
        ))
    elif short_floor is not None and 0 < room.min_dimension < short_floor:
        _add_short_side_tightness(room, short_floor, add)


def _add_short_side_tightness(room: Room, short_floor: float, add) -> None:
    # Big enough by area but too narrow to fit the fixtures across it.
    add(Issue(
        Severity.INFO,
        "ROOM_TIGHT",
        f"{_room_kind(room)} '{room.id}' is only {_f(room.min_dimension)} ft on its "
        f"short side; a workable {room.type.value.replace('_', ' ')} wants >= {short_floor:.0f} ft.",
        room=room.id,
        hint=f"Widen the short side to >= {short_floor:.0f} ft.",
    ))


def _room_kind(room: Room) -> str:
    return room.type.value.replace("_", " ").capitalize()


def _area_sizing_suffix(room: Room, area: float) -> str:
    need_len = _suggest_int(area / max(room.width, EPSILON))
    return f" e.g. `size {_f(room.width)} x {need_len}`" if need_len is not None else ""


def _area_enlarge_hint(room: Room, area: float) -> str:
    need_len = _suggest_int(area / max(room.width, EPSILON))
    if need_len is not None:
        return f"Enlarge it, e.g. `size {_f(room.width)} x {need_len}`."
    return f"Enlarge it to at least {area:g} sq ft."

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
        _validate_interior_door(plan, door, room_ids, add)
    _validate_interior_door_clashes(plan, add)
    for xdoor in plan.exterior_doors:
        _validate_exterior_door(plan, xdoor, room_ids, add)


def _validate_interior_door(plan: Barndominium, door, room_ids: set[str], add) -> None:
    loc = _door_loc(door)
    if _validate_interior_door_basics(door, room_ids, loc, add):
        return
    a, b = plan.room(door.room_a), plan.room(door.room_b)
    if a and b:
        if a.level != b.level:
            _validate_cross_level_door(door, a, b, loc, add)
        else:
            _validate_same_level_door(plan, door, a, b, loc, add)
    _validate_interior_door_leaf_size(door, loc, add)
    if a and b:
        _validate_wide_single_swing_door(door, a, b, loc, add)
    if not door.leaf and a and b:
        _validate_open_bath_door(door, a, b, loc, add)


def _validate_interior_door_basics(door, room_ids: set[str], loc: dict, add) -> bool:
    """Return True when the door is invalid enough to skip remaining checks."""
    if door.width <= 0:
        add(Issue(
            Severity.ERROR,
            "OPENING_SIZE",
            f"Interior door between '{door.room_a}' and '{door.room_b}' has "
            f"non-positive width ({_f(door.width)} ft).",
            room=door.room_a,
            hint="Give it a positive width, e.g. `width 3`.",
            **loc,
        ))
        return True
    if door.room_a == door.room_b:
        add(Issue(
            Severity.ERROR,
            "SELF_DOOR",
            f"Interior door connects room '{door.room_a}' to itself.",
            room=door.room_a,
            hint="A door joins two different rooms.",
            **loc,
        ))
        return True
    for rid in (door.room_a, door.room_b):
        if rid not in room_ids:
            add(Issue(
                Severity.ERROR,
                "DOOR_REF",
                f"Interior door references unknown room '{rid}'.",
                room=rid,
                hint="Reference an existing room id, or declare the room.",
                **loc,
            ))
    return False


def _validate_cross_level_door(door, a: Room, b: Room, loc: dict, add) -> None:
    # A door across levels is a stair/opening; it's valid when the rooms stack
    # (overlap in plan), not when they share a wall.
    if a.overlaps(b) > 0.5:
        return
    add(Issue(
        Severity.ERROR,
        "DOOR_NOADJ",
        f"Door connects '{a.id}' (level {a.level}) and '{b.id}' "
        f"(level {b.level}) but their footprints don't overlap.",
        room=a.id,
        hint="A cross-level door is a stair; place the upper room directly "
        "above part of the lower one.",
        **loc,
    ))


def _validate_same_level_door(plan: Barndominium, door, a: Room, b: Room, loc: dict, add) -> None:
    edge = shared_edge(a, b)
    if edge is None:
        _add_door_noadj_issue(plan, a, b, loc, add)
    else:
        _validate_door_on_shared_edge(door, a, b, edge, loc, add)
    if door.swing_into is not None:
        _validate_door_swing(door, a, b, edge, loc, add)


def _add_door_noadj_issue(plan: Barndominium, a: Room, b: Room, loc: dict, add) -> None:
    nb = geometric_neighbors(plan, a.id)
    extra = f" '{b.id}' is adjacent to: {', '.join(nb)}." if (b.id not in nb and nb) else ""
    add(Issue(
        Severity.ERROR,
        "DOOR_NOADJ",
        f"Door between '{a.id}' and '{b.id}' but they don't share a wall "
        "(they may only touch at a corner).",
        room=a.id,
        hint="Doors only connect rooms with a common wall; reposition them "
        f"to abut along an edge, or route through a room between them.{extra}",
        **loc,
    ))


def _validate_door_on_shared_edge(door, a: Room, b: Room, edge, loc: dict, add) -> None:
    if edge.length + EPSILON < door.width:
        add(Issue(
            Severity.WARNING,
            "DOOR_FIT",
            f"Door ({_f(door.width)} ft) is wider than the shared wall "
            f"between '{a.id}' and '{b.id}' ({_f(edge.length)} ft).",
            room=a.id,
            hint=f"Set the door width to <= {_f(edge.length)}.",
            **loc,
        ))
    elif door.offset is not None and _runs_off_wall(door.offset, door.width, edge.length):
        add(Issue(
            Severity.ERROR,
            "DOOR_OOB",
            f"Door between '{a.id}' and '{b.id}' runs off their "
            f"{_f(edge.length)} ft shared wall (offset {_f(door.offset)} + "
            f"width {_f(door.width)}).",
            room=a.id,
            hint=f"Keep offset >= 0 and offset + width <= {_f(edge.length)}, "
            "or drop the offset to centre it.",
            **loc,
        ))


def _validate_door_swing(door, a: Room, b: Room, edge, loc: dict, add) -> None:
    if door.swing_into not in (a.id, b.id):
        add(Issue(
            Severity.ERROR,
            "DOOR_SWING",
            f"Door swings into '{door.swing_into}', which it doesn't connect "
            f"(it joins '{a.id}' and '{b.id}').",
            room=a.id,
            hint=f"Set `into {a.id}` or `into {b.id}`.",
            **loc,
        ))
        return
    if edge is not None and door.kind == "swing":
        _validate_door_swing_clearance(door, a, b, edge, loc, add)


def _validate_door_swing_clearance(door, a: Room, b: Room, edge, loc: dict, add) -> None:
    target = a if door.swing_into == a.id else b
    depth = target.width if edge.orientation == "v" else target.length
    if depth + EPSILON < door.width:
        other = b if target is a else a
        add(Issue(
            Severity.WARNING,
            "DOOR_SWING",
            f"A {door.width * 12:.0f} in door can't fully open into "
            f"'{target.id}' — only {_f(depth)} ft deep at the wall.",
            room=target.id,
            hint=f"Swing it into the other room (`into {other.id}`), narrow the door, or "
            "deepen the room.",
            **loc,
        ))
    elif _door_blocks_hall(door, target, depth):
        _add_door_blocks_hall_issue(door, a, b, target, depth, loc, add)


def _door_blocks_hall(door, target: Room, depth: float) -> bool:
    # Deliberately the raw constant, not the profile: this is a residual
    # swing-clearance heuristic, not the R311.6 hall-width rule.
    return target.type is RoomType.HALLWAY and depth - door.width + EPSILON < MIN_HALLWAY_WIDTH


def _add_door_blocks_hall_issue(door, a: Room, b: Room, target: Room, depth: float, loc: dict, add) -> None:
    other = b if target is a else a
    add(Issue(
        Severity.INFO,
        "DOOR_BLOCKS_HALL",
        f"This door swings into the hallway '{target.id}'; open, its leaf leaves only "
        f"{_f(max(0.0, depth - door.width))} ft of passage, blocking circulation.",
        room=target.id,
        hint=f"Swing it into '{other.id}' instead (`into {other.id}`) so the hall stays clear.",
        **loc,
    ))


def _validate_interior_door_leaf_size(door, loc: dict, add) -> None:
    if not door.leaf:
        return
    if door.kind != "bifold" and door.width < MIN_INTERIOR_DOOR_WIDTH:
        _add_narrow_door_issue(door, loc, add)
        return
    _validate_standard_interior_door_size(door, loc, add)


def _add_narrow_door_issue(door, loc: dict, add) -> None:
    # An open cased passage (leaf=False) is wide by design — the narrow check only
    # applies to swinging doors. A double/french pair passes its full width with
    # both leaves open, so the total-width check stays honest for it too. A bifold
    # fronts storage rather than passage, so it skips the passage minimum.
    add(Issue(
        Severity.WARNING,
        "DOOR_NARROW",
        f"Interior door between '{door.room_a}' and '{door.room_b}' is "
        f"{door.width * 12:.0f} in wide.",
        room=door.room_a,
        hint=f"Use width >= {MIN_INTERIOR_DOOR_WIDTH:g} "
        f"({MIN_INTERIOR_DOOR_WIDTH * 12:.0f} in).",
        **loc,
    ))


def _validate_wide_single_swing_door(door, a: Room, b: Room, loc: dict, add) -> None:
    if door.kind != "swing" or door.width <= MAX_SINGLE_SWING_DOOR_WIDTH + EPSILON:
        return
    public_pair = a.type in PUBLIC_TYPES and b.type in PUBLIC_TYPES
    if public_pair:
        hint = (
            f"For open-plan flow, make it a cased opening instead: "
            f"`open {door.room_a} - {door.room_b} width {_f(door.width)}`. "
            "If you need doors, use a declared pair such as "
            f"`door {door.room_a} - {door.room_b} double width {_f(door.width)}`."
        )
    else:
        hint = (
            f"A {_f(door.width)} ft single leaf is oversized. Use "
            f"`door {door.room_a} - {door.room_b} double width {_f(door.width)}` "
            "or `french`, or reduce the single swing door to a normal 2.5–3 ft leaf."
        )
    add(Issue(
        Severity.WARNING,
        "DOOR_WIDE_SWING",
        f"Interior door between '{door.room_a}' and '{door.room_b}' is "
        f"{_f(door.width)} ft wide but is a single swing leaf.",
        room=door.room_a,
        hint=hint,
        **loc,
    ))


def _validate_standard_interior_door_size(door, loc: dict, add) -> None:
    # A swing door that is wide enough should still be orderable. A declared
    # double/french pair checks against stock pair widths instead of single leaves.
    if door.kind in DOUBLE_LEAF_KINDS:
        sizes: tuple[int, ...] = STD_DOUBLE_DOOR_WIDTHS_IN
    elif door.kind == "bifold":
        sizes = STD_BIFOLD_DOOR_WIDTHS_IN
    else:
        sizes = STD_INTERIOR_DOOR_WIDTHS_IN
    nearest = _nearest_std(door.width * 12, sizes)
    if abs(nearest - door.width * 12) <= DOOR_SIZE_TOL_IN:
        return
    add(Issue(
        Severity.INFO,
        "DOOR_SIZE",
        f"Interior door between '{door.room_a}' and '{door.room_b}' is "
        f"{door.width * 12:.0f} in, not a standard leaf size.",
        room=door.room_a,
        hint=f"Use a stock width, e.g. `width {nearest / 12:g}` ({nearest} in).",
        **loc,
    ))


def _validate_open_bath_door(door, a: Room, b: Room, loc: dict, add) -> None:
    # A walk-through (`open`) gives no privacy; a bathroom needs a door.
    bath = next((r for r in (a, b) if r.type in BATH_TYPES), None)
    if bath is None:
        return
    other = b if bath.id == a.id else a
    add(Issue(
        Severity.WARNING,
        "OPEN_BATH",
        f"Bathroom '{bath.id}' opens to '{other.id}' through an open passage; "
        "a bathroom needs a door for privacy.",
        room=bath.id,
        hint=f"Use `door {door.room_a} - {door.room_b}` instead of `open` so "
        "the bathroom has a door.",
        **loc,
    ))


def _validate_interior_door_clashes(plan: Barndominium, add) -> None:
    # Two doors/openings between the same pair share one wall — they can't overlap
    # on it. (Positioned or centred; centred ones coincide, so a stray duplicate
    # connection is caught too.)
    by_pair: dict[frozenset, list] = {}
    for door in plan.interior_doors:
        span = _interior_door_wall_span(plan, door)
        if span is not None:
            by_pair.setdefault(frozenset((door.room_a, door.room_b)), []).append((*span, door))
    for spans in by_pair.values():
        _validate_door_pair_spans(spans, add)


def _interior_door_wall_span(plan: Barndominium, door) -> tuple[float, float] | None:
    a, b = plan.room(door.room_a), plan.room(door.room_b)
    if not (a and b) or a.level != b.level:
        return None
    edge = shared_edge(a, b)
    if edge is None:
        return None
    w = min(door.width, edge.length)
    if door.offset is None:
        start = edge.mid - w / 2
    else:
        start = edge.lo + max(0.0, min(door.offset, edge.length - w))
    return start, start + w


def _validate_door_pair_spans(spans: list, add) -> None:
    if len(spans) < 2:
        return
    spans.sort(key=lambda s: s[0])
    for (a_lo, a_hi, _), (b_lo, b_hi, d2) in zip(spans, spans[1:]):
        if min(a_hi, b_hi) - max(a_lo, b_lo) > EPSILON:
            add(Issue(
                Severity.ERROR,
                "OPENING_CLASH",
                f"Two doors between '{d2.room_a}' and '{d2.room_b}' overlap on their shared wall.",
                room=d2.room_a,
                hint="Offset them apart, or use a single door.",
                **_door_loc(d2),
            ))


def _validate_exterior_door(plan: Barndominium, xdoor, room_ids: set[str], add) -> None:
    if xdoor.room not in room_ids:
        add(Issue(
            Severity.ERROR,
            "DOOR_REF",
            f"Exterior door references unknown room '{xdoor.room}'.",
            room=xdoor.room,
            hint="Reference an existing room id.",
            **_door_loc(xdoor),
        ))
        return
    if xdoor.width <= 0:
        add(Issue(
            Severity.ERROR,
            "OPENING_SIZE",
            f"Exterior door on '{xdoor.room}' has non-positive width ({_f(xdoor.width)} ft).",
            room=xdoor.room,
            hint="Give it a positive width, e.g. `width 3` (or `width 9` for an overhead door).",
            **_door_loc(xdoor),
        ))
        return
    room = plan.room(xdoor.room)
    if xdoor.overhead:
        _check_overhead_door(xdoor, room, add)
        return
    if room is not None and room.type in (RoomType.GARAGE, RoomType.SHOP):
        return  # a garage/shop opening is an overhead door, not a leaf size
    _validate_exterior_door_leaf_size(xdoor, add)


def _validate_exterior_door_leaf_size(xdoor, add) -> None:
    sizes = (
        STD_DOUBLE_DOOR_WIDTHS_IN
        if getattr(xdoor, "kind", "entry") in DOUBLE_LEAF_KINDS
        else STD_EXTERIOR_DOOR_WIDTHS_IN
    )
    nearest = _nearest_std(xdoor.width * 12, sizes)
    if abs(nearest - xdoor.width * 12) <= DOOR_SIZE_TOL_IN:
        return
    add(Issue(
        Severity.INFO,
        "DOOR_SIZE",
        f"Exterior door on '{xdoor.room}' is {xdoor.width * 12:.0f} in, not a standard size.",
        room=xdoor.room,
        hint=f"Use a stock width, e.g. `width {nearest / 12:g}` ({nearest} in); 36 in is the usual entry.",
        **_door_loc(xdoor),
    ))

def _check_overhead_door(xdoor, room, add) -> None:
    """Overhead (sectional garage) door checks: an unusual host room, a shop bay's
    clearance height, a non-stock sectional size, and a double-width opening's
    header reality."""
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
    # SHOP_DOOR_HEIGHT — a shop is a barndominium's clearance play: a 12 ft+ bay
    # exists to swallow a lift, an RV, a dually with a topper. A 7-8 ft residential
    # panel over it throttles the opening to car height and wastes that headroom.
    # A garage is exempt (a car passes a 7 ft door fine); only a SHOP bay is nudged.
    if room is not None and room.type is RoomType.SHOP and h <= SHOP_DOOR_MIN_HEIGHT + EPSILON:
        add(
            Issue(
                Severity.INFO,
                "SHOP_DOOR_HEIGHT",
                f"The overhead door on shop '{xdoor.room}' is only {_f(h)} ft tall — "
                "a residential-height panel that defeats a barndo shop bay: a lift, "
                "RV or dually won't clear it.",
                room=xdoor.room,
                hint="Order a taller sectional — `height 10` (or 12/14) — so the "
                "opening matches the bay's clearance; 10 ft is the usual shop door.",
                **loc,
            )
        )
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
                "— widths 8/9/10/12/16 ft, heights 7/8/10/12/14 ft (16 x 7 is the "
                "usual double, 12 x 12 a tall shop bay).",
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
    for window in plan.windows:
        _validate_window_opening(plan, window, room_ids, add)
    for door in plan.exterior_doors:
        _validate_entry_opening(plan, door, room_ids, add)
    _validate_wall_opening_clashes(plan, room_ids, add)


def _validate_window_opening(plan: Barndominium, window, room_ids: set[str], add) -> None:
    if window.room not in room_ids:
        add(Issue(
            Severity.ERROR,
            "WINDOW_REF",
            f"Window references unknown room '{window.room}'.",
            room=window.room,
            hint="Reference an existing room id.",
            **_door_loc(window),
        ))
        return
    room = plan.room(window.room)
    assert room is not None  # guaranteed: window.room was checked against room_ids
    if _validate_window_size(window, add):
        return
    _validate_window_vertical_extent(window, add)
    _validate_opening_wall_fit(window, room, "Window", add)
    _validate_window_exterior_wall(plan, room, window, add)


def _validate_window_size(window, add) -> bool:
    if window.width > 0:
        return False
    add(Issue(
        Severity.ERROR,
        "OPENING_SIZE",
        f"Window on '{window.room}' has non-positive width ({_f(window.width)} ft).",
        room=window.room,
        hint="Give it a positive width, e.g. `width 3`.",
        **_door_loc(window),
    ))
    return True


def _validate_window_vertical_extent(window, add) -> None:
    if window.head_height > window.sill_height + EPSILON:
        return
    add(Issue(
        Severity.WARNING,
        "WINDOW_SILL",
        f"Window on '{window.room}' has its head ({_f(window.head_height)} ft) at "
        f"or below its sill ({_f(window.sill_height)} ft) — it has no glass.",
        room=window.room,
        hint="Set head above sill, e.g. `sill 3 head 6.5`.",
        **_door_loc(window),
    ))


def _validate_opening_wall_fit(opening: _WallOpening, room: Room, label: str, add) -> None:
    wall_len = _wall_length(room, opening.wall)
    if not _runs_off_wall(opening.offset, opening.width, wall_len):
        return
    add(Issue(
        Severity.ERROR,
        "OPENING_OOB",
        f"{label} runs off the {opening.wall.value} wall of '{opening.room}' "
        f"(offset {_f(opening.offset)} + width {_f(opening.width)} > wall {_f(wall_len)} ft).",
        room=opening.room,
        hint=f"Keep offset >= 0 and offset + width <= {_f(wall_len)}.",
        **_door_loc(opening),
    ))


def _validate_window_exterior_wall(plan: Barndominium, room: Room, window, add) -> None:
    if window.wall in exterior_walls(plan, room):
        return
    add(Issue(
        Severity.WARNING,
        "WINDOW_INTERIOR",
        f"Window on '{window.room}' is on its {window.wall.value} wall, which is "
        "interior — it gives no daylight or egress.",
        room=window.room,
        hint="Put the window on a wall that lies on the building envelope, or open "
        "the room to an exterior space.",
        **_door_loc(window),
    ))


def _validate_entry_opening(plan: Barndominium, door, room_ids: set[str], add) -> None:
    if door.room not in room_ids:
        return  # DOOR_REF already raised in _validate_doors
    room = plan.room(door.room)
    assert room is not None  # guaranteed: door.room was checked against room_ids
    _validate_opening_wall_fit(door, room, "Entry", add)
    _validate_entry_exterior_wall(plan, room, door, add)


def _validate_entry_exterior_wall(plan: Barndominium, room: Room, door, add) -> None:
    if door.wall in exterior_walls(plan, room):
        return
    add(Issue(
        Severity.ERROR,
        "ENTRY_INTERIOR",
        f"Exterior door on '{door.room}' is on its {door.wall.value} wall, which is "
        "interior — an entrance can't open onto another room.",
        room=door.room,
        hint="Place the entry on a wall that lies on the building envelope.",
        **_door_loc(door),
    ))


def _validate_wall_opening_clashes(plan: Barndominium, room_ids: set[str], add) -> None:
    # Two openings can't occupy the same run of wall. Group every wall-positioned
    # opening (window or entry) by (room, wall) and flag overlapping spans.
    spans: dict[tuple[str, Direction], list[tuple]] = {}
    openings: list[tuple[_WallOpening, str]] = [
        *((window, "window") for window in plan.windows),
        *((door, "entry") for door in plan.exterior_doors),
    ]
    for opening, kind in openings:
        if opening.room in room_ids:
            spans.setdefault((opening.room, opening.wall), []).append(
                (opening.offset, opening.offset + opening.width, kind, opening)
            )
    for (room_id, wall), items in spans.items():
        _validate_wall_opening_span_group(room_id, wall, items, add)


def _validate_wall_opening_span_group(room_id: str, wall: Direction, items: list[tuple], add) -> None:
    items.sort()  # by start offset
    for (a_lo, a_hi, a_kind, _), (b_lo, b_hi, b_kind, b_opening) in zip(items, items[1:]):
        ov = min(a_hi, b_hi) - max(a_lo, b_lo)
        if ov <= EPSILON:
            continue
        add(Issue(
            Severity.ERROR,
            "OPENING_CLASH",
            f"Two openings overlap on the {wall.value} wall of '{room_id}': the "
            f"{a_kind} at {_f(a_lo)}–{_f(a_hi)} ft and the {b_kind} at {_f(b_lo)}–"
            f"{_f(b_hi)} ft share {_f(ov)} ft.",
            room=room_id,
            hint="Move one along the wall or narrow it so their offsets don't overlap.",
            **_door_loc(b_opening),
        ))


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
    """Why ``window`` is a safety-glazing hazard location (IRC R308.4), or ``None``."""
    by_id = {room.id: room for room in plan.rooms}
    room = by_id.get(window.room)
    if room is None:
        return None
    segment = opening_endpoints(room, window.wall, window.offset, window.width)
    sill = getattr(window, "sill_height", 3.0)
    return (
        _tempered_door_reason(plan, room, segment)
        or _tempered_wet_reason(plan, room, segment, sill)
        or _tempered_stair_reason(plan, segment, sill)
        or _tempered_panel_reason(window, sill)
    )


def _tempered_door_reason(plan: Barndominium, room: Room, window_segment: tuple[float, float, float, float]) -> str | None:
    # Door sidelite — a hinged door of the SAME room, coplanar, gap < 24 in.
    wx1, wy1, wx2, wy2 = window_segment
    window_axis = _seg_axis(wx1, wy1, wx2, wy2)
    for door_segment in _room_door_segments(plan, room):
        if _coplanar_segments_close(window_axis, _seg_axis(*door_segment), _TEMPERED_DOOR):
            return "within 24 in of a door opening in the same wall — R308.4.1"
    return None


def _coplanar_segments_close(a: tuple[str, float, float, float], b: tuple[str, float, float, float], gap: float) -> bool:
    a_or, a_fixed, a_lo, a_hi = a
    b_or, b_fixed, b_lo, b_hi = b
    return a_or == b_or and abs(a_fixed - b_fixed) <= 0.1 and _interval_gap(a_lo, a_hi, b_lo, b_hi) < gap


def _tempered_wet_reason(
    plan: Barndominium, room: Room, window_segment: tuple[float, float, float, float], sill: float,
) -> str | None:
    if room.type not in WET_TYPES or sill >= _TEMPERED_WET_SILL:
        return None
    from .fixtures import resolve_room_fixtures

    wx1, wy1, wx2, wy2 = window_segment
    for fixture in resolve_room_fixtures(plan, room):
        if fixture.kind in ("tub", "shower") and _rect_seg_distance(
            fixture.x, fixture.y, fixture.width, fixture.length, wx1, wy1, wx2, wy2
        ) < _TEMPERED_WET:
            return "within 60 in of a tub/shower in a wet room — R308.4.5"
    return None


def _tempered_stair_reason(plan: Barndominium, window_segment: tuple[float, float, float, float], sill: float) -> str | None:
    if sill >= _TEMPERED_STAIR_SILL:
        return None
    wx1, wy1, wx2, wy2 = window_segment
    for stair in plan.stairs:
        if _rect_seg_distance(stair.x, stair.y, stair.width, stair.length, wx1, wy1, wx2, wy2) < _TEMPERED_STAIR:
            return "within 36 in of a stair flight — R308.4.6 (simplified)"
    return None


def _tempered_panel_reason(window, sill: float) -> str | None:
    # Large glazing panel near walking surface (R308.4.3), anywhere: pane over 9
    # sq ft, bottom below 18 in, top above 36 in. Same head/sill the schedule reads.
    head = getattr(window, "head_height", sill)
    pane = window.width * max(0.0, head - sill)
    if sill < _TEMPERED_PANEL_BOTTOM and head > _TEMPERED_PANEL_TOP and pane > _TEMPERED_PANEL_AREA:
        return "a glazed panel over 9 sq ft with its bottom edge below 18 in and top above 36 in above the floor — R308.4.3"
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
    for stair in plan.stairs:
        if _validate_stair_finite(stair, add):
            continue
        handrail_noted = _validate_stair_handrail(plan, stair, add, profile, handrail_noted)
        _validate_stair_size(stair, add)
        _validate_stair_levels(stair, add)
        _validate_stair_bounds(plan, stair, add)
        _validate_stair_run_geometry(plan, stair, add, profile)
        _validate_stair_landings_in_rooms(plan, stair, add)


def _validate_stair_finite(stair, add) -> bool:
    if all(math.isfinite(v) for v in (stair.x, stair.y, stair.width, stair.length)):
        return False
    add(Issue(
        Severity.ERROR,
        "STAIR_GEOMETRY",
        f"Stair '{stair.id}' has non-finite coordinates or size.",
        room=stair.id,
        hint="Use finite measurements in feet (no nan/inf).",
    ))
    return True


def _validate_stair_handrail(plan: Barndominium, stair, add, profile: Profile, noted: bool) -> bool:
    # Handrail (R311.7.8): four or more risers on a flight need a handrail. The
    # DSL can't place a rail, so this is a one-per-plan checklist INFO on the
    # first qualifying stair — computed from the same rise/riser math used below.
    if noted or not _stair_connects_valid_levels(stair) or plan.ceiling_height <= 0:
        return noted
    rise = _stair_rise(plan, stair)
    risers = _stair_risers(rise, profile.max_riser_height)
    if risers < 4:
        return noted
    add(Issue(
        Severity.INFO,
        "STAIR_HANDRAIL",
        f"Stair '{stair.id}' climbs {_f(rise)} ft in ~{risers} risers, so it needs "
        "at least one handrail (34–38 in above the nosings, graspable the full length, IRC R311.7.8).",
        room=stair.id,
        hint="The DSL can't draw a rail — carry the handrail (both sides if the flight "
        "is wider than 44 in) onto the construction documents.",
        line=stair.line,
        col=stair.col,
        end_col=stair.end_col,
    ))
    return True


def _validate_stair_size(stair, add) -> None:
    if stair.width > 0 and stair.length > 0:
        return
    add(Issue(
        Severity.ERROR,
        "STAIR_SIZE",
        f"Stair '{stair.id}' has non-positive size.",
        room=stair.id,
        hint="Use positive feet, e.g. `size 4 x 10`.",
    ))


def _validate_stair_levels(stair, add) -> None:
    if _stair_connects_valid_levels(stair):
        return
    add(Issue(
        Severity.ERROR,
        "STAIR_LEVELS",
        f"Stair '{stair.id}' must connect two different levels >= 0.",
        room=stair.id,
        hint="e.g. `from 0 to 1`.",
    ))


def _stair_connects_valid_levels(stair) -> bool:
    return stair.from_level != stair.to_level and stair.from_level >= 0 and stair.to_level >= 0


def _validate_stair_bounds(plan: Barndominium, stair, add) -> None:
    if plan.wings:
        _validate_stair_in_rectilinear_footprint(plan, stair, add)
    else:
        _validate_stair_in_envelope(plan, stair, add)


def _validate_stair_in_rectilinear_footprint(plan: Barndominium, stair, add) -> None:
    # Rectilinear footprint: a stair may legitimately sit in a wing (or straddle
    # a seam), so test the footprint union rather than the primary rectangle.
    if stair.width <= 0 or stair.length <= 0:
        return
    if rect_in_footprint(plan.footprint_sections(), stair.x, stair.y, stair.width, stair.length):
        return
    add(Issue(
        Severity.ERROR,
        "STAIR_OOB",
        f"Stair '{stair.id}' extends outside the building footprint ({_f(stair.x)},{_f(stair.y)} "
        f"→ {_f(stair.x2)},{_f(stair.y2)}); it isn't covered by the envelope or any wing.",
        room=stair.id,
        hint="Keep its footprint inside the envelope or a wing.",
    ))


def _validate_stair_in_envelope(plan: Barndominium, stair, add) -> None:
    over_x = max(0.0, stair.x2 - plan.envelope_width)
    over_y = max(0.0, stair.y2 - plan.envelope_length)
    if stair.x >= -EPSILON and stair.y >= -EPSILON and over_x <= EPSILON and over_y <= EPSILON:
        return
    add(Issue(
        Severity.ERROR,
        "STAIR_OOB",
        f"Stair '{stair.id}' extends outside the {_f(plan.envelope_width)}×"
        f"{_f(plan.envelope_length)} ft envelope.",
        room=stair.id,
        hint="Keep its footprint inside the envelope.",
    ))


def _validate_stair_run_geometry(plan: Barndominium, stair, add, profile: Profile) -> None:
    # Does the footprint hold the run one storey demands? A straight flight needs
    # (risers-1)·tread of horizontal run; a switchback halves that but needs a
    # footprint wide enough for two flights side by side. Only flag when even a
    # switchback wouldn't fit — keeps this conservative.
    if not _stair_has_run_inputs(plan, stair):
        return
    rise = _stair_rise(plan, stair)
    risers = _stair_risers(rise, profile.max_riser_height)
    run_needed = max(1, risers - 1) * profile.min_tread_depth
    _validate_stair_flight_landing(stair, rise, risers, add, profile)
    _validate_stair_run_fit(stair, rise, risers, run_needed, add, profile)


def _stair_has_run_inputs(plan: Barndominium, stair) -> bool:
    return stair.width > 0 and stair.length > 0 and _stair_connects_valid_levels(stair) and plan.ceiling_height > 0


def _stair_rise(plan: Barndominium, stair) -> float:
    return plan.ceiling_height * abs(stair.to_level - stair.from_level)


def _stair_risers(rise: float, max_riser: float) -> int:
    return max(1, math.ceil(rise / max_riser))


def _validate_stair_flight_landing(stair, rise: float, risers: int, add, profile: Profile) -> None:
    # STAIR_LANDING (R311.7.3): a single flight may rise at most 12 ft 7 in
    # (151 in) between floor levels or landings.
    if rise <= _STAIR_MAX_FLIGHT_RISE + EPSILON:
        return
    max_flight_risers = max(1, math.floor(_STAIR_MAX_FLIGHT_RISE / profile.max_riser_height))
    add(Issue(
        Severity.INFO,
        "STAIR_LANDING",
        f"Stair '{stair.id}' climbs {_f(rise)} ft (~{risers} risers) in one flight; "
        f"IRC R311.7.3 limits a flight to {_f(_STAIR_MAX_FLIGHT_RISE)} ft (12 ft 7 in, "
        f"~{max_flight_risers} risers) of vertical rise between landings, so it needs "
        "an intermediate landing.",
        room=stair.id,
        hint="Break the run with a landing (a switchback or an L-turn), or split it "
        "across levels — the DSL models one straight flight, so note the mid-run "
        "landing on the construction documents.",
        line=stair.line,
        col=stair.col,
        end_col=stair.end_col,
    ))


def _validate_stair_run_fit(stair, rise: float, risers: int, run_needed: float, add, profile: Profile) -> None:
    long_dim, short_dim = max(stair.width, stair.length), min(stair.width, stair.length)
    could_switchback = short_dim + EPSILON >= 2 * profile.min_stair_width
    needed = run_needed / 2 if could_switchback else run_needed
    if long_dim + EPSILON < needed:
        _add_stair_run_issue(stair, long_dim, rise, risers, run_needed, add, profile)
    else:
        _validate_stair_headroom(stair, long_dim, rise, risers, add, profile)


def _add_stair_run_issue(stair, long_dim: float, rise: float, risers: int, run_needed: float, add, profile: Profile) -> None:
    add(Issue(
        Severity.WARNING,
        "STAIR_RUN",
        f"Stair '{stair.id}' is {_f(long_dim)} ft long, too short to climb {_f(rise)} ft: "
        f"~{risers} risers need about {_f(run_needed)} ft of run (a {_f(profile.max_riser_height * 12)} "
        f"in riser / {_f(profile.min_tread_depth * 12)} in tread)"
        + _profile_tag(profile, "max_riser_height", f"{MAX_RISER_HEIGHT * 12:g} in / {MIN_TREAD_DEPTH * 12:g} in")
        + ".",
        room=stair.id,
        hint=f"Lengthen its footprint to >= {_f(run_needed)} ft, or make it >= "
        f"{_f(2 * profile.min_stair_width)} ft wide to fit a switchback.",
    ))


def _validate_stair_headroom(stair, long_dim: float, rise: float, risers: int, add, profile: Profile) -> None:
    # Headroom (R311.7.2): a person descending under the upper floor needs 6'-8"
    # clear until they pass the stairwell opening's edge.
    riser_h = rise / risers
    open_needed = STAIR_HEADROOM * (profile.min_tread_depth / max(riser_h, EPSILON))
    if long_dim + EPSILON >= open_needed:
        return
    add(Issue(
        Severity.WARNING,
        "STAIR_HEADROOM",
        f"Stair '{stair.id}' is {_f(long_dim)} ft long — too short for a floor opening "
        f"that keeps {_f(STAIR_HEADROOM)} ft (6'-8\") headroom under the upper floor "
        f"(IRC R311.7.2): the opening needs about {_f(open_needed)} ft of run to clear.",
        room=stair.id,
        hint=f"Lengthen the run/stairwell opening to >= {_f(open_needed)} ft, or lower "
        "the floor-to-floor so fewer risers are needed.",
    ))


def _validate_stair_landings_in_rooms(plan: Barndominium, stair, add) -> None:
    lower = _stair_rooms(plan, stair, stair.from_level)
    upper = _stair_rooms(plan, stair, stair.to_level)
    if lower and upper:
        return
    missing = []
    if not lower:
        missing.append(f"level {stair.from_level}")
    if not upper:
        missing.append(f"level {stair.to_level}")
    add(Issue(
        Severity.WARNING,
        "STAIR_FLOAT",
        f"Stair '{stair.id}' doesn't land in a room on {', '.join(missing)}.",
        room=stair.id,
        hint="Position it so its footprint overlaps a room on each level.",
    ))

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
    """Smoke/CO-alarm coverage (IRC R314/R315)."""
    by_id = {room.id: room for room in plan.rooms}
    bedrooms = [room for room in plan.rooms if room.type is RoomType.BEDROOM]
    alarms = plan.alarms
    if not alarms:
        _add_alarm_teaching_reminder(bedrooms, add)
        return
    smoke_rooms = {alarm.room for alarm in alarms if alarm.is_smoke}
    adjacency = door_graph(plan)
    _validate_bedroom_smoke_alarms(bedrooms, smoke_rooms, add)
    _validate_sleeping_area_smoke_alarms(bedrooms, smoke_rooms, adjacency, by_id, add)
    _validate_level_smoke_alarms(plan, alarms, by_id, add)
    _validate_co_alarms(plan, alarms, bedrooms, by_id, add)


def _add_alarm_teaching_reminder(bedrooms: list[Room], add) -> None:
    # Nothing declared. Teach the statement once, and only where sleeping rooms
    # make it matter — an alarm-free shop building isn't nagged.
    if not bedrooms:
        return
    add(Issue(
        Severity.INFO,
        "ALARM_CO",
        "This plan has bedrooms but declares no smoke/CO alarms. IRC R314 wants a "
        "smoke alarm in each bedroom, outside each sleeping area, and on every level; "
        "R315 wants a carbon-monoxide alarm outside sleeping areas where fuel appliances "
        "or an attached garage are present.",
        hint="Declare them so the placement checks can verify coverage — e.g. `alarm smoke "
        "in <bed>`, `alarm smoke in <hall>`, `alarm co in <hall>` (or `alarm smoke_co` "
        "for a combination unit).",
    ))


def _validate_bedroom_smoke_alarms(bedrooms: list[Room], smoke_rooms: set[str], add) -> None:
    # ALARM_BEDROOM — a smoke (or combo) alarm inside every bedroom (R314.3).
    for bedroom in bedrooms:
        if bedroom.id in smoke_rooms:
            continue
        add(Issue(
            Severity.WARNING,
            "ALARM_BEDROOM",
            f"Bedroom '{bedroom.id}' has no smoke alarm. IRC R314.3 requires a smoke alarm in each sleeping room.",
            room=bedroom.id,
            hint=f"Add `alarm smoke in {bedroom.id}` (or `alarm smoke_co in {bedroom.id}` for a combination unit).",
        ))


def _validate_sleeping_area_smoke_alarms(
    bedrooms: list[Room], smoke_rooms: set[str], adjacency: dict[str, set[str]], by_id: dict[str, Room], add,
) -> None:
    # ALARM_HALL — a smoke alarm just outside each sleeping area (R314.3(2)).
    for bedroom in bedrooms:
        neighbours = adjacency.get(bedroom.id, set())
        if any(neighbor in smoke_rooms for neighbor in neighbours):
            continue
        _add_alarm_hall_issue(bedroom, neighbours, by_id, add)


def _add_alarm_hall_issue(bedroom: Room, neighbours: set[str], by_id: dict[str, Room], add) -> None:
    # sorted() so the suggested room is stable across PYTHONHASHSEED.
    ordered = sorted(neighbours)
    hall = next((room_id for room_id in ordered if by_id.get(room_id) and by_id[room_id].type is RoomType.HALLWAY), None)
    target = hall or next(iter(ordered), None)
    where = f" (e.g. `alarm smoke in {target}`)" if target else ""
    add(Issue(
        Severity.WARNING,
        "ALARM_HALL",
        f"No smoke alarm is outside the sleeping area of bedroom '{bedroom.id}': no room "
        "adjacent to it carries one (IRC R314.3 wants an alarm outside each sleeping area).",
        room=bedroom.id,
        hint="Approximated as 'a room sharing a door with the bedroom' (a hallway if "
        "there is one, else any adjacent room)" + where + ".",
    ))


def _validate_level_smoke_alarms(plan: Barndominium, alarms, by_id: dict[str, Room], add) -> None:
    # ALARM_LEVEL — a smoke alarm on every level (R314.3(3)).
    smoke_levels = {by_id[alarm.room].level for alarm in alarms if alarm.is_smoke and alarm.room in by_id}
    for level in sorted({getattr(room, "level", 0) for room in plan.rooms}):
        if level in smoke_levels:
            continue
        add(Issue(
            Severity.WARNING,
            "ALARM_LEVEL",
            f"Level {level} has no smoke alarm. IRC R314.3(3) requires at least one on every storey of the dwelling.",
            hint=f"Place `alarm smoke in <room on level {level}>`.",
        ))


def _validate_co_alarms(plan: Barndominium, alarms, bedrooms: list[Room], by_id: dict[str, Room], add) -> None:
    # ALARM_CO — a CO alarm outside the sleeping areas where bedrooms coexist with
    # an attached garage/shop (R315). INFO; fuel-fired appliances aren't modelled.
    garage = next((room for room in plan.rooms if room.type in GARAGE_TYPES), None)
    if not bedrooms or garage is None:
        return
    co_outside = any(
        alarm.is_co and alarm.room in by_id and by_id[alarm.room].type is not RoomType.BEDROOM
        for alarm in alarms
    )
    if co_outside:
        return
    add(Issue(
        Severity.INFO,
        "ALARM_CO",
        f"The plan has bedrooms and an attached garage/shop ('{garage.id}'), so a "
        "carbon-monoxide alarm is required outside the sleeping areas (IRC R315), but none is declared.",
        hint="Add `alarm co in <hall>` (or `alarm smoke_co`) outside the bedrooms. INFO only — "
        "fuel-fired appliances aren't modelled, so this is triggered by the attached garage/shop alone.",
    ))

def _validate_access(plan: Barndominium, add) -> None:
    """Every interior room must be reachable from an exterior door."""
    interior_rooms = {room.id for room in plan.rooms if room.type is not RoomType.PORCH}
    if not interior_rooms:
        return
    adjacency = _access_adjacency(plan, interior_rooms)
    entries = {door.room for door in plan.exterior_doors if door.room in interior_rooms}
    _validate_people_entry(plan, add)
    if not entries:
        return
    reached = _reachable_rooms(adjacency, entries)
    _validate_unreachable_rooms(plan, interior_rooms - reached, reached, add)


def _access_adjacency(plan: Barndominium, interior_rooms: set[str]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {room_id: set() for room_id in interior_rooms}
    _add_door_access_edges(plan, adjacency)
    _add_stair_access_edges(plan, adjacency)
    return adjacency


def _add_door_access_edges(plan: Barndominium, adjacency: dict[str, set[str]]) -> None:
    for door in plan.interior_doors:
        if door.room_a in adjacency and door.room_b in adjacency:
            adjacency[door.room_a].add(door.room_b)
            adjacency[door.room_b].add(door.room_a)


def _add_stair_access_edges(plan: Barndominium, adjacency: dict[str, set[str]]) -> None:
    # Stairs link the rooms they land in across levels — that's how an upper floor
    # becomes reachable from the ground.
    for stair in plan.stairs:
        lower = [room.id for room in _stair_rooms(plan, stair, stair.from_level) if room.id in adjacency]
        upper = [room.id for room in _stair_rooms(plan, stair, stair.to_level) if room.id in adjacency]
        for lower_id in lower:
            for upper_id in upper:
                adjacency[lower_id].add(upper_id)
                adjacency[upper_id].add(lower_id)


def _validate_people_entry(plan: Barndominium, add) -> None:
    # An overhead garage door is vehicle access, not a building entrance: it still
    # makes its garage/shop reachable (the BFS below), but only a people-door
    # satisfies NO_ENTRY.
    if any(not door.overhead for door in plan.exterior_doors):
        return
    first = next(iter(plan.rooms)).id
    message = (
        "Plan has no entry door — an overhead door is vehicle access, not a way to enter on foot."
        if plan.exterior_doors
        else "Plan has no exterior door — no way to enter the building."
    )
    add(Issue(
        Severity.ERROR,
        "NO_ENTRY",
        message,
        hint=f"Add an entrance on an exterior wall, e.g. `entry {first} south width 3 offset 4`.",
    ))


def _reachable_rooms(adjacency: dict[str, set[str]], entries: set[str]) -> set[str]:
    reached: set[str] = set()
    queue: deque[str] = deque(entries)
    while queue:
        current = queue.popleft()
        if current in reached:
            continue
        reached.add(current)
        queue.extend(neighbor for neighbor in adjacency[current] if neighbor not in reached)
    return reached


def _validate_unreachable_rooms(plan: Barndominium, unreachable: set[str], reached: set[str], add) -> None:
    # O(1) id lookup instead of ``plan.room``'s linear scan — this runs once per
    # unreachable room, so the scan would be O(n²) on a large disconnected plan.
    index = room_index(plan)
    for room_id in sorted(unreachable):
        idx = index.first_index(room_id)
        room = plan.rooms[idx] if idx is not None else None
        add(Issue(
            _no_access_severity(room),
            "NO_ACCESS",
            f"Room '{room_id}' cannot be reached from any entrance.",
            room=room_id,
            hint=_no_access_hint(plan, room_id, reached),
        ))


def _no_access_severity(room: Room | None) -> Severity:
    # A loft reaches the floor by stairs, which aren't modelled yet, so an
    # unreachable loft is a warning, not a hard error (like closets/pantries).
    if room and room.type in (RoomType.CLOSET, RoomType.PANTRY, RoomType.LOFT):
        return Severity.WARNING
    return Severity.ERROR


def _no_access_hint(plan: Barndominium, room_id: str, reached: set[str]) -> str:
    neighbors = geometric_neighbors(plan, room_id)
    reachable_nb = [neighbor for neighbor in neighbors if neighbor in reached]
    if reachable_nb:
        return f"Add `door {room_id} - {reachable_nb[0]}` (they share a wall)."
    if neighbors:
        return (
            f"Connect it into the plan, e.g. `door {room_id} - {neighbors[0]}`, "
            f"or give it its own `entry {room_id} <wall>`."
        )
    return (
        f"'{room_id}' touches no other room — reposition it adjacent to one, or add "
        f"`entry {room_id} <wall>`."
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


def _dq_kitchen_passthrough(plan: Barndominium, graph, by_id, add) -> None:
    """Warn when the kitchen is the only route between public rooms.

    Open-plan kitchens should be adjacent to dining/living, but traffic should be
    able to bypass the work triangle. If removing the kitchen splits its dining
    neighbour from a living/great/rec neighbour, the kitchen is acting as a
    corridor rather than a work room.
    """
    living_like = {RoomType.LIVING, RoomType.GREAT_ROOM, RoomType.REC_ROOM}
    dining_like = {RoomType.DINING}
    for room in plan.rooms:
        if room.type is not RoomType.KITCHEN:
            continue
        public_neighbors = sorted(
            n
            for n in graph.get(room.id, ())
            if n in by_id and by_id[n].type in living_like | dining_like
        )
        if len(public_neighbors) < 2:
            continue
        comps = components_excluding(graph, {room.id})
        comp_by_room = {rid: comp for comp in comps for rid in comp}
        severed_pair: tuple[str, str] | None = None
        for a in public_neighbors:
            if by_id[a].type not in dining_like:
                continue
            for b in public_neighbors:
                if by_id[b].type not in living_like:
                    continue
                if comp_by_room.get(a) is not comp_by_room.get(b):
                    severed_pair = (b, a)
                    break
            if severed_pair is not None:
                break
        if severed_pair is None:
            continue
        living_id, dining_id = severed_pair
        add(
            Issue(
                Severity.WARNING,
                "KITCHEN_PASSTHROUGH",
                f"Kitchen '{room.id}' is the only route between '{living_id}' and "
                f"'{dining_id}', making the work zone a through-corridor.",
                room=room.id,
                hint=(
                    "Let traffic bypass the work triangle: open the living/great "
                    "room directly to dining, add a hall path around the kitchen, "
                    "or move the kitchen to the side of the public core."
                ),
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
        comps = components_excluding(graph, gate_ids)
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
                        "more square footprint (under ~3:1), at least 4 ft deep. (A "
                        "shallow reach-in is fine too, behind a near-full-width bifold.)",
                    )
                )


#: Per-room-type wording for the shared reach-in access check (CLOSET_ACCESS /
#: PANTRY_ACCESS). A reach-in closet and a reach-in pantry share the same
#: geometry — you stand at the opening and reach, so the single door must open
#: (nearly) the whole width — but a closet's dead run is unreachable ROD and a
#: pantry's is unreachable SHELF, so the prose differs. ``(code, noun, stored)``.
_REACH_IN_ACCESS: dict[RoomType, tuple[str, str, str]] = {
    RoomType.CLOSET: ("CLOSET_ACCESS", "closet", "rod"),
    RoomType.PANTRY: ("PANTRY_ACCESS", "pantry", "shelf"),
    RoomType.STORAGE: ("STORAGE_ACCESS", "storage room", "stored goods"),
}


def _dq_reach_in_access(plan: Barndominium, graph, by_id, add) -> None:
    # 8c2. Reach-in access, for closets AND pantries: a shallow store room can't
    # be walked into, so its single door must open nearly the whole width.
    doors_into = _reach_in_doors_by_room(plan, by_id)
    for room_id, doors in doors_into.items():
        if len(doors) == 1:  # a walk-through reaches stored goods from both openings
            _validate_reach_in_single_door(room_id, by_id, doors[0], add)


def _reach_in_doors_by_room(plan: Barndominium, by_id: dict[str, Room]) -> dict[str, list]:
    doors_into: dict[str, list] = {}
    for door in plan.interior_doors:
        for room_id in (door.room_a, door.room_b):
            if room_id in by_id and by_id[room_id].type in _REACH_IN_ACCESS:
                doors_into.setdefault(room_id, []).append(door)
    return doors_into


def _validate_reach_in_single_door(room_id: str, by_id: dict[str, Room], door, add) -> None:
    room = by_id[room_id]
    other = by_id.get(door.room_a if door.room_b == room_id else door.room_b)
    if other is None:
        return
    edge = shared_edge(room, other)
    if edge is None:
        return
    depth = room.width if edge.orientation == "v" else room.length
    if depth + EPSILON >= CLOSET_WALKIN_DEPTH:
        return  # a walk-in: you step inside, so an ordinary door serves it
    span = _reach_in_door_span(room, door, edge)
    if span is None:
        return
    start, width, lo, hi = span
    worst = max(start - lo, hi - (start + width))
    if worst <= CLOSET_REACH + EPSILON:
        return
    _add_reach_in_access_issue(room, door, edge, depth, width, worst, lo, hi, add)


def _reach_in_door_span(room: Room, door, edge) -> tuple[float, float, float, float] | None:
    width = min(door.width, edge.length)
    if width <= 0:
        return None
    start = edge.lo + (edge.length - width) / 2.0 if door.offset is None else edge.lo + max(0.0, min(door.offset, edge.length - width))
    lo = room.y if edge.orientation == "v" else room.x
    hi = room.y2 if edge.orientation == "v" else room.x2
    return start, width, lo, hi


def _add_reach_in_access_issue(room: Room, door, edge, depth: float, width: float, worst: float, lo: float, hi: float, add) -> None:
    code, noun, stored = _REACH_IN_ACCESS[room.type]
    add(Issue(
        Severity.WARNING,
        code,
        f"{noun.capitalize()} '{room.id}' is a reach-in ({_f(depth)} ft deep — under "
        f"{_f(CLOSET_WALKIN_DEPTH)} ft nobody can step inside) but its {_f(width)} ft "
        f"door leaves {_f(worst)} ft of {noun} past a jamb, beyond arm's reach of the back {stored}.",
        room=room.id,
        line=door.line,
        col=door.col,
        end_col=door.end_col,
        hint=_reach_in_access_fix(noun, stored, door, edge, lo, hi),
    ))


def _reach_in_access_fix(noun: str, stored: str, door, edge, lo: float, hi: float) -> str:
    breadth = hi - lo
    # The fix, computed: the widest STOCK bifold that fits the room with a jamb's
    # grace, centred on it (clamped to the shared run when a neighbour covers only
    # part of the room's wall).
    max_width = min(breadth - 1.0, edge.length)
    fix_width = max(
        (size / 12.0 for size in STD_BIFOLD_DOOR_WIDTHS_IN if size / 12.0 <= max_width),
        default=STD_BIFOLD_DOOR_WIDTHS_IN[0] / 12.0,
    )
    fix_offset = max(0.0, (lo - edge.lo) + (breadth - fix_width) / 2.0)
    if (breadth - fix_width) / 2.0 <= CLOSET_REACH + EPSILON:
        return (
            f"Centre a near-full-width bifold on the {noun} so every foot of {stored} is reachable: "
            f"`door {door.room_a} - {door.room_b} bifold width {_f(fix_width)} offset {_f(fix_offset)}` "
            f"(keep the blind run past each jamb under {_f(CLOSET_REACH)} ft)."
        )
    return (
        f"This {noun} is wider than one stock bifold covers — give it two openings "
        f"(a pair of bifold `door` statements side by side), or reshape it into a walk-in "
        f"({_f(CLOSET_WALKIN_DEPTH)} ft deep or more) behind an ordinary door."
    )

def _dq_closet_depth(plan: Barndominium, graph, by_id, add) -> None:
    # 8c3. Hanging depth: hanging clothes are 2 ft deep (24 in hangers), so a
    #      bedroom's clothes closet needs that much clear in its short dimension.
    #      Only a closet serving a bedroom (door-connected) is judged — a shallow
    #      hall linen/broom closet is legitimate shelf-only storage.
    for room in plan.rooms:
        if room.type is not RoomType.CLOSET:
            continue
        if room.min_dimension + EPSILON >= CLOSET_HANG_DEPTH:
            continue
        beds = [
            n
            for n in graph.get(room.id, ())
            if n in by_id and by_id[n].type is RoomType.BEDROOM
        ]
        if not beds:
            continue
        add(
            Issue(
                Severity.WARNING,
                "CLOSET_DEPTH",
                f"Closet '{room.id}' is only {_f(room.min_dimension)} ft deep — "
                f"hanging clothes need {_f(CLOSET_HANG_DEPTH)} ft, so bedroom "
                f"'{beds[0]}' can't hang anything in it.",
                room=room.id,
                hint="Deepen the closet to 2 ft or more (2 - 2.5 ft is the "
                "reach-in standard); shallower is shelf-only linen/broom storage, "
                "not a clothes closet.",
            )
        )


def _dq_closet_window(plan: Barndominium, graph, by_id, add) -> None:
    # 8c4. A window in a closet: sunlight fades clothes, the glass eats the wall
    #      the rod wants, and it spends exterior wall a habitable room could
    #      daylight with. Closets belong buried on interior walls. INFO — taste.
    for w in plan.windows:
        room = by_id.get(w.room)
        if room is not None and room.type is RoomType.CLOSET:
            add(
                Issue(
                    Severity.INFO,
                    "CLOSET_WINDOW",
                    f"Closet '{room.id}' has a window — sunlight fades clothes and "
                    "the glass eats the wall the rod wants.",
                    room=room.id,
                    line=w.line,
                    col=w.col,
                    end_col=w.end_col,
                    hint="Bury the closet on interior walls and give this stretch "
                    "of exterior wall (and its daylight) to a habitable room.",
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
    # 8l. Leaf direction / crowding: privacy swings, inward exterior clearance,
    # and door arcs that consume fixture wall space.
    _validate_interior_privacy_swings(plan, by_id, add)
    _validate_exterior_inward_swings(plan, by_id, add)
    _validate_door_fixture_conflicts(plan, add)


def _validate_interior_privacy_swings(plan: Barndominium, by_id: dict[str, Room], add) -> None:
    for door in plan.interior_doors:
        pair = _interior_privacy_swing_pair(plan, door, by_id)
        if pair is None:
            continue
        a, b, target, preferred = pair
        if target is not preferred:
            _add_privacy_swing_issue(door, a, b, target, preferred, add)


def _interior_privacy_swing_pair(plan: Barndominium, door, by_id: dict[str, Room]) -> tuple[Room, Room, Room, Room] | None:
    a, b = by_id.get(door.room_a), by_id.get(door.room_b)
    if a is None or b is None or a.level != b.level:
        return None
    edge = shared_edge(a, b)
    if edge is None:
        return None
    region = _interior_swing_region(plan, door, a, b, edge)
    if region is None:  # pocket/sliding/bifold/cased — no leaf to place
        return None
    target = _region_room(region, a, b, edge)
    preferred = _preferred_swing_room(a, b)
    if target is None or preferred is None:
        return None
    return a, b, target, preferred


def _add_privacy_swing_issue(door, a: Room, b: Room, target: Room, preferred: Room, add) -> None:
    word = preferred.type.value.replace("_", "-")
    if door.swing_into is None:
        add(Issue(
            Severity.INFO,
            "DOOR_SWING_UNSET",
            f"The {word} door '{a.id}'-'{b.id}' has no swing direction set and defaults "
            f"into '{target.id}'; a {word} door should open into the room.",
            room=preferred.id,
            hint=f"Pin it with `into {preferred.id}`.",
            **_door_loc(door),
        ))
    else:
        add(Issue(
            Severity.INFO,
            "DOOR_SWING_PRIVACY",
            f"The {word} door '{a.id}'-'{b.id}' swings into '{target.id}'; a {word} "
            "door should open into the room so the leaf screens the view and folds against a wall.",
            room=preferred.id,
            hint=f"Swing it into '{preferred.id}' (`into {preferred.id}`).",
            **_door_loc(door),
        ))


def _validate_exterior_inward_swings(plan: Barndominium, by_id: dict[str, Room], add) -> None:
    for door in plan.exterior_doors:
        if door.overhead:  # rides up its tracks — no swing
            continue
        room = by_id.get(door.room)
        if room is not None:
            _validate_exterior_inward_swing(door, room, add)


def _validate_exterior_inward_swing(door, room: Room, add) -> None:
    leaf = door.width / 2.0 if door.kind in DOUBLE_LEAF_KINDS else door.width
    depth = room.width if door.wall in (Direction.WEST, Direction.EAST) else room.length
    if depth + EPSILON >= leaf:
        return
    add(Issue(
        Severity.WARNING,
        "DOOR_SWING_INWARD",
        f"The exterior door on '{room.id}' swings inward, but the room is only {_f(depth)} "
        f"ft deep — a {leaf * 12:.0f} in leaf can't fully open.",
        room=room.id,
        hint="Deepen the room, narrow the door, or use an out-swing or sliding door.",
        **_door_loc(door),
    ))


def _validate_door_fixture_conflicts(plan: Barndominium, add) -> None:
    # A door swing that crowds a fixture out of a room the room could otherwise
    # hold: the door-aware placer drops a fixture the door-blind one keeps.
    from .fixtures import fixtures_for, plan_room_fixtures  # lazy: circular import

    for room in plan.rooms:
        if not fixtures_for(room.type):
            continue
        blind = plan_room_fixtures(plan, room, avoid_doors=False)
        clear = plan_room_fixtures(plan, room)
        if len(clear) < len(blind):
            _add_door_hits_fixture_issue(room, blind[len(clear):], add)


def _add_door_hits_fixture_issue(room: Room, dropped, add) -> None:
    names = " and ".join(fixture.kind for fixture in dropped)
    add(Issue(
        Severity.WARNING,
        "DOOR_HITS_FIXTURE",
        f"A door swing leaves '{room.id}' no clear wall for its {names} — the room has "
        "the space, but not once the door's arc is kept clear.",
        room=room.id,
        hint="Move the door along the wall, swing it the other way (`into <room>` / "
        "`hinge near|far`), make it a pocket/sliding door, or enlarge the room.",
    ))

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
        hit_start = near < WINDOW_WALL_CLEAR and not building_corner(plan, r, w.wall, False)
        hit_end = (wall_len - far) < WINDOW_WALL_CLEAR and not building_corner(
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
    #    furnish. A more severe, general pass catches non-habitable rooms too
    #    (laundry/utility/baths/etc.) when they are so skinny they read as leftover
    #    corridor space. Linear types with their own rules are exempt.
    for room in plan.rooms:
        short = room.min_dimension
        long = max(room.width, room.length)
        if short <= EPSILON:
            continue
        aspect = long / short
        if _general_skinny_room_type(room.type) and aspect > MAX_GENERAL_ROOM_ASPECT:
            add(
                Issue(
                    Severity.WARNING,
                    "ROOM_SKINNY",
                    f"{room.type.value.capitalize()} '{room.id}' is "
                    f"{_f(room.width)} x {_f(room.length)} ({aspect:.1f}:1) — "
                    "too long and skinny to function as a room.",
                    room=room.id,
                    hint=f"Keep rooms under about {MAX_GENERAL_ROOM_ASPECT:g}:1; "
                    "widen the short side, split it into smaller rooms, or relabel "
                    "it as hallway/storage if it is meant to be a linear strip.",
                )
            )
            if room.type not in HABITABLE_TYPES:
                continue
        if room.type in HABITABLE_TYPES:
            limit = MAX_ROOM_ASPECT_BY_TYPE.get(room.type, MAX_ROOM_ASPECT)
            if aspect > limit:
                add(
                    Issue(
                        Severity.INFO,
                        "ROOM_PROPORTION",
                        f"{room.type.value.capitalize()} '{room.id}' is "
                        f"{_f(room.width)} x {_f(room.length)} ({aspect:.1f}:1); "
                        "very elongated rooms are hard to furnish.",
                        room=room.id,
                        hint=f"Aim for a more rectangular footprint (under ~{limit:g}:1) "
                        "— widen the short side or split the space.",
                    )
                )

    # 8b. Mudroom shape: not habitable, so the check above never sees one — but a
    #     long, skinny mudroom is a corridor wearing a mudroom label. Its job is a
    #     drop zone: a bench and hooks (~1.5 ft) plus a 3 ft walkway wants >= 5 ft
    #     of width and a compact footprint (a 6 x 8 is the classic). Severity is
    #     tiered: under the width bar the room CANNOT do its job (a bench plus a
    #     walkway physically doesn't fit) — that's a WARNING, not a taste note;
    #     wide-enough-but-elongated stays an INFO nudge.
    for room in plan.rooms:
        if room.type is not RoomType.MUDROOM:
            continue
        short = room.min_dimension
        long_side = max(room.width, room.length)
        if short <= EPSILON:
            continue
        aspect = long_side / short
        tight = short + EPSILON < MIN_MUDROOM_WIDTH
        skinny = aspect > MAX_MUDROOM_ASPECT
        if not (tight or skinny):
            continue
        if tight and skinny:
            why = (
                f"only {_f(short)} ft wide and {aspect:.1f}:1 — a corridor, "
                "not a drop zone"
            )
        elif tight:
            why = (
                f"only {_f(short)} ft wide — a 1.5 ft bench plus a 3 ft walkway "
                "doesn't fit"
            )
        else:
            why = f"{aspect:.1f}:1 — a corridor, not a drop zone"
        add(
            Issue(
                Severity.WARNING if tight else Severity.INFO,
                "MUDROOM_SHAPE",
                f"Mudroom '{room.id}' is {_f(room.width)} x {_f(room.length)}, {why}.",
                room=room.id,
                hint=f"A working mudroom is >= {MIN_MUDROOM_WIDTH:g} ft wide and "
                f"compact (near 6 x 8, under ~{MAX_MUDROOM_ASPECT:g}:1); give the "
                "surplus length to the shop, laundry or pantry, or label the "
                "strip what it is (a hallway).",
            )
        )


def _general_skinny_room_type(room_type: RoomType) -> bool:
    # These either are intentionally linear (hall/garage/porch), have stronger
    # type-specific shape checks (safe_room, foyer, mudroom, shop, storage), or are
    # reach-in/cabinet storage where skinny can be legitimate.
    return room_type not in {
        RoomType.HALLWAY,
        RoomType.CLOSET,
        RoomType.PANTRY,
        RoomType.STORAGE,
        RoomType.SAFE_ROOM,
        RoomType.FOYER,
        RoomType.MUDROOM,
        RoomType.GARAGE,
        RoomType.SHOP,
        RoomType.PORCH,
    }


def _dq_shop_depth(plan: Barndominium, graph, by_id, add) -> None:
    # 8d. Shop depth: the shop analog of MUDROOM_SHAPE. A shop bay's job is to
    #     hold a vehicle or a workbench wall PLUS a working aisle, and that needs
    #     real width. Under MIN_SHOP_DEPTH (12 ft) the bay physically can't do it —
    #     it's storage mislabeled as a shop — a WARNING (the geometry proves it).
    #     Between 12 and SHOP_COMFORT_DEPTH (20 ft) it works but is tight for a
    #     full-size truck plus a work zone — an INFO comfort nudge. A GARAGE is
    #     exempt (garages are sized to cars, not equipment) — only a SHOP is judged.
    for room in plan.rooms:
        if room.type is not RoomType.SHOP:
            continue
        short = room.min_dimension
        if short <= EPSILON:
            continue
        tight = short + EPSILON < MIN_SHOP_DEPTH
        snug = short + EPSILON < SHOP_COMFORT_DEPTH
        if not (tight or snug):
            continue
        if tight:
            msg = (
                f"Shop '{room.id}' is only {_f(short)} ft across — under "
                f"{MIN_SHOP_DEPTH:g} ft it can't take a vehicle or a workbench wall "
                "plus a working aisle; it's storage, not a shop."
            )
            hint = (
                f"Widen the shop to >= {MIN_SHOP_DEPTH:g} ft (a single bay wants "
                "~14 ft; a two-bay shop ~24 ft), or relabel the strip as storage/"
                "utility."
            )
        else:
            msg = (
                f"Shop '{room.id}' is {_f(short)} ft across — usable, but tight for "
                "a full-size truck (8.5 ft wide plus door swing) with a work zone "
                f"along a wall (a comfortable bay is >= {SHOP_COMFORT_DEPTH:g} ft)."
            )
            hint = (
                f"Aim for >= {SHOP_COMFORT_DEPTH:g} ft across so a vehicle parks "
                "with a workbench and aisle beside it; deeper still for a two-bay."
            )
        add(
            Issue(
                Severity.WARNING if tight else Severity.INFO,
                "SHOP_DEPTH",
                msg,
                room=room.id,
                hint=hint,
            )
        )


def _dq_loft_ceiling(plan: Barndominium, graph, by_id, add) -> None:
    # 8e. Loft headroom: a loft is habitable (HABITABLE_TYPES) and often sleeps
    #     people, so it needs the IRC R305.1 7 ft habitable minimum. The room's
    #     EFFECTIVE ceiling is its per-room `ceiling_height` override, else the plan
    #     ceiling. This is a flat-ceiling check by design — barndsl carries no
    #     roof-slope geometry, so a sloped-ceiling loft's headroom-over-half-the-
    #     floor (R305.1.1) can't be measured here; the explanation says so. A
    #     `vaulted` loft is open to the ridge (its usable height rises well past
    #     7 ft), so it's exempt — the flat effective ceiling doesn't describe it.
    for room in plan.rooms:
        if room.type is not RoomType.LOFT or getattr(room, "vaulted", False):
            continue
        eff = room.ceiling_height if room.ceiling_height is not None else plan.ceiling_height
        if eff + EPSILON >= LOFT_MIN_CEILING:
            continue
        add(
            Issue(
                Severity.WARNING,
                "LOFT_CEILING",
                f"Loft '{room.id}' has a {_f(eff)} ft ceiling — under the "
                f"{LOFT_MIN_CEILING:g} ft habitable minimum (IRC R305.1), so it "
                "can't be lived or slept in.",
                room=room.id,
                hint=(
                    f"Raise the loft's ceiling to >= {LOFT_MIN_CEILING:g} ft "
                    "(set its `ceiling` override or the plan `ceiling`). Under a "
                    "sloped roof, keep >= 7 ft over at least half the floor and "
                    "note the clear-height line on the drawings."
                ),
            )
        )


def _dq_office_clearance(plan: Barndominium, graph, by_id, add) -> None:
    # 8f. Office furnish-fit: an office has to hold a desk (DESK_WIDTH x DESK_DEPTH)
    #     with a DESK_CHAIR_PULL behind it to push the chair back — a
    #     DESK_WIDTH x (DESK_DEPTH + DESK_CHAIR_PULL) clear box against a wall,
    #     clear of every door swing. Mirrors BED_CLEARANCE / DINING_CLEARANCE: uses
    #     the clear (finish-face) interior, subtracts the door-swing keepouts the
    #     auto-placer already computes, and asks whether the desk box still fits
    #     against any of the four walls. INFO — livability guidance, not a gate.
    from .fixtures import _door_swing_rects

    need_along = DESK_WIDTH  # the desk's width runs along the wall
    need_deep = DESK_DEPTH + DESK_CHAIR_PULL  # desk depth + chair-pull off the wall
    for room in plan.rooms:
        if room.type is not RoomType.OFFICE:
            continue
        x0, y0, cw, cl = clear_box(plan, room)
        if cw <= EPSILON or cl <= EPSILON:
            continue
        keepouts = _door_swing_rects(plan, room)
        if _desk_fits(x0, y0, cw, cl, need_along, need_deep, keepouts):
            continue
        add(
            Issue(
                Severity.INFO,
                "OFFICE_CLEARANCE",
                f"Office '{room.id}' is {_f(cw)}×{_f(cl)} ft clear — too tight to "
                f"place a desk ({DESK_WIDTH:g}×{DESK_DEPTH:g} ft) against a wall "
                f"with a {DESK_CHAIR_PULL:g} ft chair-pull behind it, clear of the "
                "door swing.",
                room=room.id,
                hint=(
                    "Enlarge or reshape the office so a desk backs to a wall with "
                    f"~{DESK_CHAIR_PULL:g} ft to roll the chair back, off the door's "
                    "approach; a ~8×10 ft office is the comfortable floor."
                ),
            )
        )


def _desk_fits(
    x0: float, y0: float, cw: float, cl: float,
    need_along: float, need_deep: float, keepouts,
) -> bool:
    """Can a desk box sit against a clear-rectangle wall without keepout overlap."""
    return _desk_fits_south_north(x0, y0, cw, cl, need_along, need_deep, keepouts) or _desk_fits_west_east(
        x0, y0, cw, cl, need_along, need_deep, keepouts
    )


def _box_clear_of_keepouts(bx: float, by: float, bw: float, bl: float, keepouts) -> bool:
    for kx, ky, kw, kl in keepouts:
        if min(bx + bw, kx + kw) - max(bx, kx) > EPSILON and min(by + bl, ky + kl) - max(by, ky) > EPSILON:
            return False
    return True


def _desk_fits_south_north(
    x0: float, y0: float, cw: float, cl: float, need_along: float, need_deep: float, keepouts,
) -> bool:
    # South & north walls: box is need_along wide, need_deep deep.
    if cw + EPSILON < need_along or cl + EPSILON < need_deep:
        return False
    span = cw - need_along
    for i in range(int(span / 0.5) + 2):
        bx = x0 + min(i * 0.5, span)
        if _box_clear_of_keepouts(bx, y0, need_along, need_deep, keepouts):
            return True
        if _box_clear_of_keepouts(bx, y0 + cl - need_deep, need_along, need_deep, keepouts):
            return True
    return False


def _desk_fits_west_east(
    x0: float, y0: float, cw: float, cl: float, need_along: float, need_deep: float, keepouts,
) -> bool:
    # West & east walls: box is need_along tall, need_deep deep (rotated 90°).
    if cl + EPSILON < need_along or cw + EPSILON < need_deep:
        return False
    span = cl - need_along
    for i in range(int(span / 0.5) + 2):
        by = y0 + min(i * 0.5, span)
        if _box_clear_of_keepouts(x0, by, need_deep, need_along, keepouts):
            return True
        if _box_clear_of_keepouts(x0 + cw - need_deep, by, need_deep, need_along, keepouts):
            return True
    return False


def _dq_new_room_semantics(plan: Barndominium, graph, by_id, add) -> None:
    """Design-quality checks for the semantic room types beyond the original core.

    These are intentionally heuristic: they make the labels mean something without
    turning taste into hard syntax. Existing generic checks still handle egress,
    daylight, reachability and room proportions.
    """
    for room in plan.rooms:
        if room.type is RoomType.SAFE_ROOM:
            _dq_safe_room(plan, graph, by_id, room, add)
        elif room.type is RoomType.MECHANICAL:
            _dq_mechanical_room(plan, graph, by_id, room, add)
        elif room.type is RoomType.FOYER:
            _dq_foyer(plan, graph, by_id, room, add)
        elif _looks_like_foyer(room):
            _dq_foyer_shape(room, add)
        elif room.type is RoomType.STORAGE:
            _dq_storage_room(plan, graph, by_id, room, add)
        elif room.type is RoomType.GREAT_ROOM:
            _dq_great_room(plan, graph, by_id, room, add)
        elif room.type is RoomType.FLEX:
            _dq_flex_room(plan, graph, by_id, room, add)
        elif room.type is RoomType.REC_ROOM:
            _dq_rec_room(plan, graph, by_id, room, add)


def _dq_safe_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    for w in plan.windows_for(room.id):
        add(Issue(
            Severity.WARNING,
            "SAFE_ROOM_WINDOW",
            f"Safe room '{room.id}' has a window; ordinary glazing defeats the protected-room intent.",
            room=room.id,
            line=w.line,
            col=w.col,
            end_col=w.end_col,
            hint="Move the safe room to an interior location with no window, or document a rated storm shutter/window assembly outside the DSL.",
        ))
    walls = exterior_walls(plan, room)
    if walls:
        add(Issue(
            Severity.WARNING,
            "SAFE_ROOM_EXTERIOR",
            f"Safe room '{room.id}' sits on exterior wall(s): {', '.join(w.value for w in walls)}.",
            room=room.id,
            hint="Prefer an interior room surrounded by other spaces; if it must touch the shell, harden that exterior wall assembly.",
        ))
    short = room.min_dimension
    long = max(room.width, room.length)
    too_small = room.area + EPSILON < MIN_SAFE_ROOM_AREA or short + EPSILON < MIN_SAFE_ROOM_DIM
    too_skinny = short > EPSILON and long / short > MAX_SAFE_ROOM_ASPECT
    if too_small or too_skinny:
        why = (
            f"{long / short:.1f}:1 — too long and skinny for a shelter"
            if too_skinny and not too_small
            else f"{_f(room.area)} sq ft with a {_f(short)} ft short side"
        )
        add(Issue(
            Severity.WARNING,
            "SAFE_ROOM_SIZE",
            f"Safe room '{room.id}' is {_f(room.width)} x {_f(room.length)} ({why}).",
            room=room.id,
            hint=f"Give it at least ~{MIN_SAFE_ROOM_AREA:g} sq ft, a {_f(MIN_SAFE_ROOM_DIM)} ft short side, and a compact shape under ~{MAX_SAFE_ROOM_ASPECT:g}:1.",
        ))
    if not _has_leaf_door(plan, room.id):
        add(Issue(
            Severity.WARNING,
            "SAFE_ROOM_ACCESS",
            f"Safe room '{room.id}' has no real door leaf; a cased/open passage cannot secure a shelter room.",
            room=room.id,
            hint=f"Use a swing/pocket/sliding door into the safe room, e.g. `door {room.id} - <hall> swing`.",
        ))
    if _is_pass_through_room(room.id, graph):
        add(Issue(
            Severity.WARNING,
            "SAFE_ROOM_ACCESS",
            f"Safe room '{room.id}' is part of the only route between other rooms.",
            room=room.id,
            hint="Do not use the safe room as circulation; put it off a hall/core with a single controlled doorway.",
        ))


def _dq_mechanical_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    if room.area + EPSILON < MIN_MECH_AREA or room.min_dimension + EPSILON < MIN_MECH_DIM:
        add(Issue(
            Severity.WARNING,
            "MECH_CLEARANCE",
            f"Mechanical room '{room.id}' is {_f(room.width)} x {_f(room.length)} ({_f(room.area)} sq ft).",
            room=room.id,
            hint=f"Give mechanical equipment service clearance — aim for at least {_f(MIN_MECH_DIM)} ft clear and ~{MIN_MECH_AREA:g} sq ft, or fold it into a larger utility room.",
        ))
    if not _has_leaf_door(plan, room.id):
        add(Issue(
            Severity.WARNING,
            "MECH_ACCESS",
            f"Mechanical room '{room.id}' has no real service door.",
            room=room.id,
            hint=f"Provide a door from a hall, utility, mudroom or garage: `door {room.id} - <service_space>`.",
        ))
    if _is_pass_through_room(room.id, graph):
        add(Issue(
            Severity.WARNING,
            "MECH_ACCESS",
            f"Mechanical room '{room.id}' is on the only circulation route between other rooms.",
            room=room.id,
            hint="Mechanical rooms should be service spaces, not hallways; route circulation around it.",
        ))
    for n in sorted(graph.get(room.id, ())):
        if n in by_id and by_id[n].type is RoomType.BEDROOM:
            add(Issue(
                Severity.WARNING,
                "MECH_BEDROOM",
                f"Mechanical room '{room.id}' opens directly into bedroom '{n}'.",
                room=room.id,
                hint="Put mechanical access off a hall, utility, mudroom or garage instead of a sleeping room.",
            ))


def _dq_foyer(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    _dq_foyer_shape(room, add)
    has_entry = any(d.room == room.id and not d.overhead for d in plan.exterior_doors)
    if not has_entry:
        add(Issue(
            Severity.INFO,
            "FOYER_FLOW",
            f"Foyer '{room.id}' has no exterior entry door; it may be mislabeled circulation.",
            room=room.id,
            hint=f"Land the front entry in the foyer: `entry {room.id} <wall> width 3`.",
        ))
    if room.area + EPSILON < MIN_FOYER_AREA or room.min_dimension + EPSILON < MIN_FOYER_DIM:
        add(Issue(
            Severity.INFO,
            "FOYER_FLOW",
            f"Foyer '{room.id}' is {_f(room.width)} x {_f(room.length)} — tight for an arrival space.",
            room=room.id,
            hint=f"A foyer wants at least about {_f(MIN_FOYER_DIM)} ft of width and ~{MIN_FOYER_AREA:g} sq ft for people, door swing and coats.",
        ))
    neigh = [by_id[n] for n in graph.get(room.id, ()) if n in by_id]
    if neigh and not any(r.type in PUBLIC_TYPES or r.type in {RoomType.HALLWAY, RoomType.MUDROOM} for r in neigh):
        add(Issue(
            Severity.WARNING,
            "FOYER_FLOW",
            f"Foyer '{room.id}' connects only to private/service rooms.",
            room=room.id,
            hint="Connect the foyer to the public core (great/living/kitchen/dining) or a hall, not only bedrooms/baths/service rooms.",
        ))


def _looks_like_foyer(room: Room) -> bool:
    return room.id.lower() in {"foyer", "entry", "entryway", "entry_hall"}


def _dq_foyer_shape(room: Room, add) -> None:
    short = room.min_dimension
    long = max(room.width, room.length)
    if short <= EPSILON or long / short <= MAX_FOYER_ASPECT:
        return
    add(Issue(
        Severity.INFO,
        "FOYER_SHAPE",
        f"Foyer '{room.id}' is {_f(room.width)} x {_f(room.length)} ({long / short:.1f}:1) — it reads as a hallway, not an arrival room.",
        room=room.id,
        hint=f"Make the entry compact (under ~{MAX_FOYER_ASPECT:g}:1), or type/name it as a hallway if it is meant to be a circulation spine.",
    ))


def _dq_storage_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    short = room.min_dimension
    long = max(room.width, room.length)
    if short > EPSILON and room.area >= MIN_WALKIN_AREA and long / short >= MAX_STORAGE_ASPECT:
        add(Issue(
            Severity.INFO,
            "STORAGE_SHAPE",
            f"Storage room '{room.id}' is {_f(room.width)} x {_f(room.length)} ({long / short:.1f}:1) — a long, skinny storage aisle.",
            room=room.id,
            hint="Make walk-in storage more compact (under ~3:1), or split it into closets/cabinets along circulation.",
        ))
    if not _has_any_door_or_opening(plan, room.id):
        add(Issue(
            Severity.WARNING,
            "STORAGE_ACCESS",
            f"Storage room '{room.id}' has no door or cased opening.",
            room=room.id,
            hint=f"Add a usable opening, e.g. `door {room.id} - <hall>` or relabel the dead pocket.",
        ))
    if _is_pass_through_room(room.id, graph):
        add(Issue(
            Severity.INFO,
            "STORAGE_ACCESS",
            f"Storage room '{room.id}' is being used as circulation between other rooms.",
            room=room.id,
            hint="Storage works best as a destination off a hall/core, not as the only route through the plan.",
        ))


def _dq_great_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    if room.area + EPSILON < MIN_GREAT_ROOM_AREA:
        add(Issue(
            Severity.INFO,
            "GREAT_ROOM_SCALE",
            f"Great room '{room.id}' is only {_f(room.area)} sq ft — closer to an ordinary living room.",
            room=room.id,
            hint=f"Use `living` for a modest room, or enlarge the great room to roughly {MIN_GREAT_ROOM_AREA:g}+ sq ft.",
        ))
    eff = room.ceiling_height if room.ceiling_height is not None else plan.ceiling_height
    if not getattr(room, "vaulted", False) and eff + EPSILON < MIN_GREAT_ROOM_CEILING:
        add(Issue(
            Severity.INFO,
            "GREAT_ROOM_SCALE",
            f"Great room '{room.id}' has a {_f(eff)} ft flat ceiling; great rooms usually want taller or vaulted volume.",
            room=room.id,
            hint=f"Mark it `vaulted` or give it a ceiling around {MIN_GREAT_ROOM_CEILING:g}+ ft if the great-room volume is intended.",
        ))
    neigh_types = {by_id[n].type for n in graph.get(room.id, ()) if n in by_id}
    if not neigh_types & {RoomType.KITCHEN, RoomType.DINING}:
        add(Issue(
            Severity.INFO,
            "GREAT_ROOM_FLOW",
            f"Great room '{room.id}' is not connected to kitchen or dining.",
            room=room.id,
            hint="Tie the great room to the public core with a wide cased opening to kitchen/dining.",
        ))


def _dq_flex_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    missing: list[str] = []
    if not _has_escape_like_opening(plan, room):
        missing.append("egress-capable exterior opening")
    if not any(by_id[n].type is RoomType.CLOSET for n in graph.get(room.id, ()) if n in by_id):
        missing.append("closet")
    if missing:
        add(Issue(
            Severity.INFO,
            "FLEX_FUTURE_BED",
            f"Flex room '{room.id}' is not bedroom-ready: missing {', '.join(missing)}.",
            room=room.id,
            hint="If this may become a guest room, put it on an exterior wall with an escape-capable window/door and add a closet.",
        ))


def _dq_rec_room(plan: Barndominium, graph, by_id, room: Room, add) -> None:
    if room.area + EPSILON < MIN_REC_ROOM_AREA:
        add(Issue(
            Severity.INFO,
            "REC_ROOM_SCALE",
            f"Rec room '{room.id}' is only {_f(room.area)} sq ft — tight for games or activity furniture.",
            room=room.id,
            hint=f"Use `flex`/`office` for a small multipurpose room, or grow the rec room toward {MIN_REC_ROOM_AREA:g}+ sq ft.",
        ))
    for other in plan.rooms:
        if other.type is not RoomType.BEDROOM:
            continue
        edge = shared_edge(room, other)
        if edge is not None and edge.length + EPSILON >= MIN_SOUND_BUFFER_WALL:
            add(Issue(
                Severity.INFO,
                "REC_ROOM_NOISE",
                f"Rec room '{room.id}' shares a {_f(edge.length)} ft wall with bedroom '{other.id}'.",
                room=room.id,
                hint="Buffer noisy rec rooms from bedrooms with a hall, closet, storage room or bath between them.",
            ))


def _has_leaf_door(plan: Barndominium, room_id: str) -> bool:
    return any(d.leaf and room_id in (d.room_a, d.room_b) for d in plan.interior_doors) or any(
        d.room == room_id and not d.overhead for d in plan.exterior_doors
    )


def _has_any_door_or_opening(plan: Barndominium, room_id: str) -> bool:
    return any(room_id in (d.room_a, d.room_b) for d in plan.interior_doors) or any(
        d.room == room_id for d in plan.exterior_doors
    )


def _is_pass_through_room(room_id: str, graph: dict[str, set[str]]) -> bool:
    neighbors = sorted(graph.get(room_id, ()))
    if len(neighbors) < 2:
        return False
    comps = components_excluding(graph, {room_id})
    if len(comps) < 2:
        return False
    touched = 0
    for comp in comps:
        if any(n in comp for n in neighbors):
            touched += 1
            if touched > 1:
                return True
    return False


def _has_escape_like_opening(plan: Barndominium, room: Room) -> bool:
    if any(d.room == room.id and d.egress and not d.overhead for d in plan.exterior_doors):
        return True
    walls = set(exterior_walls(plan, room))
    return any(
        w.room == room.id and w.wall in walls and getattr(w, "escape_capable", False)
        for w in plan.windows
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


def _dq_garage_passthrough(plan: Barndominium, graph, by_id, add) -> None:
    # 9b. Garage/shop as a *circulation spine*. GARAGE_BEDROOM catches a garage
    #     that opens straight into a bedroom (one hop); this catches the subtler,
    #     more dangerous case where the garage/shop is the ONLY interior route
    #     from the public core to the bedrooms — you must cross the vehicle bay
    #     (fumes, cold, no fire separation on the path) to get from the living
    #     room to bed, even though no single door is garage↔bedroom. Remove all
    #     garage/shop rooms from the door graph: if any bedroom is then cut off
    #     from the component holding the public rooms, the garage was a cut vertex
    #     on that route. A circulation-*shape* defect reachability (NO_ACCESS)
    #     can't see, so it warrants a WARNING.
    garages = {r.id for r in plan.rooms if r.type in GARAGE_TYPES}
    if not garages:
        return
    publics = {r.id for r in plan.rooms if r.type in PUBLIC_TYPES}
    beds = {r.id for r in plan.rooms if r.type is RoomType.BEDROOM}
    if not publics or not beds:
        return
    # Drop the garages and see what's still connected. A detached shop with no
    # interior door isn't a cut vertex — removing an isolated node leaves the
    # public/bedroom components exactly as they were, so it never fires here.
    comps = components_excluding(graph, garages)
    public_comp = next((c for c in comps if c & publics), None)
    if public_comp is None:
        return  # public rooms are only reachable through the garage themselves
    severed = sorted(b for b in beds if b not in public_comp)
    if not severed:
        return
    names = ", ".join(f"'{b}'" for b in severed)
    plural = "s" if len(severed) > 1 else ""
    add(
        Issue(
            Severity.WARNING,
            "GARAGE_PASSTHROUGH",
            f"The only interior route from the living core to bedroom{plural} "
            f"{names} passes through a garage/shop — you must cross the vehicle "
            "bay to reach the sleeping rooms.",
            room=severed[0],
            hint="Route the bedrooms off a hallway that reaches the public core "
            "without crossing the garage/shop — e.g. add a `door` from the "
            "bedroom hall directly to a living/kitchen/dining room, so the "
            "garage is a dead-end bay off the plan, not a corridor through it.",
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


def _dq_garage_vehicle_door(plan: Barndominium, graph, by_id, add) -> None:
    # A garage/shop bay should have an exterior overhead/sectional door. Without
    # one, the room may compile and even connect to the dwelling, but it cannot
    # function as a vehicle/equipment bay — exactly the trapped-shop failure mode
    # the visual review catches.
    for g in plan.rooms:
        if g.type not in GARAGE_TYPES:
            continue
        if any(d.room == g.id and d.overhead for d in plan.exterior_doors):
            continue
        walls = exterior_walls(plan, g)
        label = g.type.value
        if walls:
            wall = max(walls, key=lambda w: _wall_length(g, w))
            hint = (
                f"Add an overhead door on the {wall.value} wall, e.g. "
                f"`door {g.id} {wall.value} overhead width 10 height 8 offset 1`."
            )
        else:
            hint = (
                f"Move the {label} to the perimeter or give it an exterior wall, "
                "then add an overhead vehicle door."
            )
        add(Issue(
            Severity.WARNING,
            "GARAGE_VEHICLE_DOOR",
            f"{label.capitalize()} '{g.id}' has no exterior overhead/vehicle door; it is only reachable through the house.",
            room=g.id,
            hint=hint,
        ))


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
                    hint="Make it a bifold or bypass/sliding door so the leaf doesn't "
                    f"fill the closet, e.g. `door {d.room_a} - {d.room_b} bifold "
                    f"width {_f(d.width)}`.",
                )
            )


def _dq_hall_deadend(plan: Barndominium, graph, by_id, add) -> None:
    # 11. A hallway exists to *distribute* circulation. One that opens onto a
    # single room (or none) is just overhead. Exempt an entry foyer/vestibule.
    hall_entries = {door.room for door in plan.exterior_doors}
    for room in plan.rooms:
        if room.type is RoomType.HALLWAY:
            _validate_hall_deadend(plan, room, graph, by_id, hall_entries, add)


def _validate_hall_deadend(
    plan: Barndominium, room: Room, graph: dict[str, set[str]], by_id: dict[str, Room], hall_entries: set[str], add,
) -> None:
    served = len(graph.get(room.id, ()))
    if served <= 1:
        _validate_low_service_hall(room, served, hall_entries, add)
        return
    corridor = _hall_corridor_axis(room)
    if corridor is None:
        return
    axis_lo, axis_hi, long_x = corridor
    marks = _hall_door_marks(plan, room, by_id, long_x)
    if not marks:
        return
    stub = max(min(mark[0] for mark in marks) - axis_lo, axis_hi - max(mark[1] for mark in marks))
    if stub >= MIN_HALL_STUB:
        _add_hall_stub_issue(room, stub, add)


def _validate_low_service_hall(room: Room, served: int, hall_entries: set[str], add) -> None:
    if room.id in hall_entries:
        return
    add(Issue(
        Severity.INFO,
        "HALL_DEADEND",
        f"Hallway '{room.id}' opens onto {served} room(s); a hall that serves one room isn't earning its footprint.",
        room=room.id,
        hint="Open that room off a larger space and drop the hall, or extend the hall so it distributes to more rooms.",
    ))


def _hall_corridor_axis(room: Room) -> tuple[float, float, bool] | None:
    long_x = room.width >= room.length
    axis_lo, axis_hi = (room.x, room.x2) if long_x else (room.y, room.y2)
    if axis_hi - axis_lo <= room.min_dimension + EPSILON:
        return None  # roughly square (a foyer/landing), not a corridor
    return axis_lo, axis_hi, long_x


def _hall_door_marks(plan: Barndominium, room: Room, by_id: dict[str, Room], long_x: bool) -> list[tuple[float, float]]:
    # Measure from each doorway, not the whole abutting wall: a hall running past
    # its last door reads as a dead end even if a room's wall lines the rest of it.
    marks = _interior_hall_door_marks(plan, room, by_id, long_x)
    marks.extend(_exterior_hall_door_marks(plan, room, long_x))
    return marks


def _interior_hall_door_marks(plan: Barndominium, room: Room, by_id: dict[str, Room], long_x: bool) -> list[tuple[float, float]]:
    marks: list[tuple[float, float]] = []
    for door in plan.interior_doors:
        if room.id not in (door.room_a, door.room_b):
            continue
        other = door.room_b if door.room_a == room.id else door.room_a
        neighbor = by_id.get(other)
        edge = shared_edge(room, neighbor) if neighbor else None
        if edge is None:
            continue
        lo, hi = _door_interval(edge, door)
        along_axis = (edge.orientation == "h") if long_x else (edge.orientation == "v")
        marks.append((lo, hi) if along_axis else (edge.pos, edge.pos))
    return marks


def _exterior_hall_door_marks(plan: Barndominium, room: Room, long_x: bool) -> list[tuple[float, float]]:
    marks: list[tuple[float, float]] = []
    for door in plan.exterior_doors:
        if door.room != room.id:
            continue
        x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
        ns_wall = door.wall in (Direction.NORTH, Direction.SOUTH)
        if long_x:
            marks.append((min(x1, x2), max(x1, x2)) if ns_wall else (x1, x1))
        else:
            marks.append((min(y1, y2), max(y1, y2)) if not ns_wall else (y1, y1))
    return marks


def _add_hall_stub_issue(room: Room, stub: float, add) -> None:
    add(Issue(
        Severity.INFO,
        "HALL_DEADEND",
        f"Hallway '{room.id}' runs {_f(stub)} ft past its last doorway into a blank wall — "
        "a dead-end stub of circulation.",
        room=room.id,
        hint="Put the end room's door at the hall end (extend that room to cap the hall), "
        "or trim the hall back to its last doorway.",
    ))



#: The design-quality checks, run in order. Each is a standalone
#: ``(plan, graph, by_id, add)`` function so it can be unit-tested in
#: isolation; the driver below builds the shared derived state once.
_DESIGN_QUALITY_CHECKS = (
    _dq_kitchen_flow,
    _dq_kitchen_passthrough,
    _dq_bed_privacy,
    _dq_bath_distance,
    _dq_private_passthrough,
    _dq_entry_private,
    _dq_wet_group,
    _dq_no_closet,
    _dq_master_ensuite,
    _dq_bed_sound,
    _dq_closet_shape,
    _dq_reach_in_access,
    _dq_closet_depth,
    _dq_closet_window,
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
    _dq_shop_depth,
    _dq_loft_ceiling,
    _dq_office_clearance,
    _dq_new_room_semantics,
    _dq_garage_bedroom,
    _dq_garage_passthrough,
    _dq_garage_no_entry,
    _dq_garage_vehicle_door,
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
    graph = door_graph(plan)
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
    """Check the rooms placed against a declared ``program`` (if any)."""
    spec = plan.program_spec
    if spec is None:
        return
    mismatches = _program_mismatches(plan, spec)
    if not mismatches:
        return
    add(Issue(
        Severity.WARNING,
        "PROGRAM_MISMATCH",
        "Plan doesn't match its declared program: " + "; ".join(mismatches) + ".",
        hint="Add or remove rooms to match, or update the `program` line to the intent "
        "you mean (counts, required rooms, or `area`).",
        **_spec_loc(spec),
    ))


def _program_mismatches(plan: Barndominium, spec) -> list[str]:
    metrics = plan.metrics()
    mismatches: list[str] = []
    _append_program_count_mismatches(spec, metrics, mismatches)
    _append_program_required_room_mismatches(plan, spec, mismatches)
    _append_program_area_mismatch(spec, metrics, mismatches)
    _append_program_storage_mismatch(plan, spec, mismatches)
    return mismatches


def _append_program_count_mismatches(spec, metrics: dict, mismatches: list[str]) -> None:
    actual_beds = int(metrics["bedroom_count"])
    actual_baths = int(metrics["bathroom_count"])
    if spec.beds != actual_beds:
        mismatches.append(f"{spec.beds} bedroom(s) declared but {actual_beds} placed")
    if spec.baths is not None and spec.baths != actual_baths:
        mismatches.append(f"{spec.baths} bath(s) declared but {actual_baths} placed")


def _append_program_required_room_mismatches(plan: Barndominium, spec, mismatches: list[str]) -> None:
    # Required room types are an *at-least* check: missing/short is flagged, surplus never is.
    type_counts: dict[RoomType, int] = {}
    for room in plan.rooms:
        type_counts[room.type] = type_counts.get(room.type, 0) + 1
    for room_type, need in spec.required.items():
        have = type_counts.get(room_type, 0)
        if have >= need:
            continue
        if need == 1:
            mismatches.append(f"no {room_type.value} placed")
        else:
            mismatches.append(f"{need} {room_type.value}(s) declared but {have} placed")


def _append_program_area_mismatch(spec, metrics: dict, mismatches: list[str]) -> None:
    if spec.min_area is None:
        return
    interior = metrics["interior_sqft"]
    if math.isfinite(interior) and interior + EPSILON < spec.min_area:
        mismatches.append(f"{_f(spec.min_area)} sq ft declared but {interior:.0f} placed")


def _validate_program_area_overrun(plan: Barndominium, add) -> None:
    """Nudge when a declared program area reads like a target but geometry is much larger.

    ``program ... area`` remains a minimum contract for backwards compatibility;
    this INFO catches the common author/model mistake of declaring the requested
    area in the program line, then drawing a substantially larger house.
    """
    spec = plan.program_spec
    if spec is None or spec.min_area is None:
        return
    target = spec.min_area
    interior = plan.interior_area
    if not (math.isfinite(target) and math.isfinite(interior) and target > EPSILON):
        return
    over_by = interior - target
    if over_by <= max(100.0, target * 0.10) + EPSILON:
        return
    add(Issue(
        Severity.INFO,
        "PROGRAM_AREA_OVERRUN",
        f"The plan declares program area {_f(target)} sq ft but draws {interior:.0f} sq ft of conditioned interior.",
        hint=(
            "If the brief's area is a target, shrink the envelope/rooms toward it; "
            "if it is only a minimum, raise or omit `area` so the program line "
            "matches your intent."
        ),
        **_spec_loc(spec),
    ))


def _append_program_storage_mismatch(plan: Barndominium, spec, mismatches: list[str]) -> None:
    if spec.min_storage is None:
        return
    storage = sum(room.area for room in plan.rooms if room.type in STORAGE_TYPES)
    if storage + EPSILON < spec.min_storage:
        mismatches.append(f"{_f(spec.min_storage)} sq ft of storage declared but {storage:.0f} placed")

def _suggest_anchor(a: Room, b: Room) -> str:
    """The relative-placement direction that would abut ``b`` against ``a``,
    picked from where ``b`` already sits — so the hint moves it the short way."""
    ax, ay = a.center
    bx, by = b.center
    if abs(bx - ax) >= abs(by - ay):
        return "east-of" if bx >= ax else "west-of"
    return "north-of" if by >= ay else "south-of"


def _validate_requirements(plan: Barndominium, add) -> None:
    """Check declared ``require`` statements against the compiled plan."""
    room_ids = {room.id for room in plan.rooms}
    for req in plan.requirements:
        loc = _spec_loc(req)
        if _validate_requirement_refs(req, room_ids, loc, add):
            continue
        _validate_requirement(plan, req, loc, add)


def _validate_requirement_refs(req, room_ids: set[str], loc: dict, add) -> bool:
    missing = [room_id for room_id in (req.a, req.b) if room_id is not None and room_id not in room_ids]
    for room_id in missing:
        add(Issue(
            Severity.ERROR,
            "REQUIRE_REF",
            f"Requirement references unknown room '{room_id}'.",
            room=room_id,
            hint="Reference an existing room id, or declare the room.",
            **loc,
        ))
    return bool(missing)


def _validate_requirement(plan: Barndominium, req, loc: dict, add) -> None:
    a = plan.room(req.a)
    assert a is not None  # checked before dispatch
    if req.kind == "adjacent":
        _validate_adjacent_requirement(plan, req, a, loc, add)
    elif req.kind == "separate":
        _validate_separate_requirement(plan, req, a, loc, add)
    elif req.kind == "exterior":
        _validate_exterior_requirement(plan, req, a, loc, add)
    elif req.kind == "area":
        _validate_area_requirement(req, a, loc, add)


def _validate_adjacent_requirement(plan: Barndominium, req, a: Room, loc: dict, add) -> None:
    assert req.b is not None  # two-room kinds always carry b
    b = plan.room(req.b)
    assert b is not None
    if shared_edge(a, b) is not None:
        return
    detail = (
        f"they sit on different levels ({a.level} and {b.level})"
        if a.level != b.level
        else "they don't share a wall (a corner touch isn't enough)"
    )
    add(Issue(
        Severity.WARNING,
        "REQUIRE_UNMET",
        f"Required adjacency unmet: '{a.id}' and '{b.id}' — {detail}.",
        room=a.id,
        hint=f"Abut them along a wall — e.g. re-place '{b.id}' with a relative anchor: "
        f"`room {b.id}: {b.type.value} {_suggest_anchor(a, b)} {a.id} size "
        f"{_f(b.width)} x {_f(b.length)}`.",
        **loc,
    ))


def _validate_separate_requirement(plan: Barndominium, req, a: Room, loc: dict, add) -> None:
    assert req.b is not None  # two-room kinds always carry b
    b = plan.room(req.b)
    assert b is not None
    edge = shared_edge(a, b)  # None across levels: trivially separate
    if edge is None:
        return
    add(Issue(
        Severity.WARNING,
        "REQUIRE_UNMET",
        f"Required separation unmet: '{a.id}' and '{b.id}' share a {_f(edge.length)} ft wall.",
        room=a.id,
        hint=f"Reposition '{b.id}' so it doesn't touch '{a.id}', or put a buffer room "
        "(hall, closet) between them.",
        **loc,
    ))


def _validate_exterior_requirement(plan: Barndominium, req, room: Room, loc: dict, add) -> None:
    ext = exterior_walls(plan, room)
    interior = _interior_wall_list(ext)
    if req.wall is not None and req.wall not in ext:
        _add_exterior_wall_requirement_issue(req, room, ext, interior, loc, add)
    elif req.wall is None and not ext:
        add(Issue(
            Severity.WARNING,
            "REQUIRE_UNMET",
            f"Required exterior wall unmet: '{room.id}' has no exterior wall (interior walls: {interior}).",
            room=room.id,
            hint=f"Move '{room.id}' to the building perimeter so at least one wall lies on the footprint edge.",
            **loc,
        ))


def _interior_wall_list(exterior: list[Direction]) -> str:
    return ", ".join(
        wall.value
        for wall in (Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST)
        if wall not in exterior
    )


def _add_exterior_wall_requirement_issue(req, room: Room, ext: list[Direction], interior: str, loc: dict, add) -> None:
    assert req.wall is not None
    have = f"its exterior wall(s): {', '.join(wall.value for wall in ext)}" if ext else "it has no exterior wall at all"
    add(Issue(
        Severity.WARNING,
        "REQUIRE_UNMET",
        f"Required exterior wall unmet: '{room.id}'s {req.wall.value} wall is interior "
        f"(interior walls: {interior}).",
        room=room.id,
        hint=f"Move '{room.id}' so its {req.wall.value} wall lies on the footprint edge — {have}.",
        **loc,
    ))


def _validate_area_requirement(req, room: Room, loc: dict, add) -> None:
    assert req.min_area is not None  # the builder guarantees it
    if room.area + EPSILON >= req.min_area:
        return
    need_len = _suggest_int(req.min_area / max(room.width, EPSILON))
    sizing = f" — e.g. `size {_f(room.width)} x {need_len}`" if need_len is not None else ""
    add(Issue(
        Severity.WARNING,
        "REQUIRE_UNMET",
        f"Required area unmet: '{room.id}' is {_f(room.area)} sq ft; the requirement is "
        f">= {_f(req.min_area)} sq ft.",
        room=room.id,
        hint=f"Enlarge '{room.id}' to at least {_f(req.min_area)} sq ft{sizing}.",
        **loc,
    ))

def _validate_walls(plan: Barndominium, add) -> None:
    """Check declared ``wall`` statements against the compiled plan."""
    room_ids = {room.id for room in plan.rooms}
    for wall_spec in getattr(plan, "wall_specs", None) or []:
        _validate_wall_spec(plan, wall_spec, room_ids, add)
    _validate_bearing_wall_axis(plan, add)


def _validate_wall_spec(plan: Barndominium, wall_spec, room_ids: set[str], add) -> None:
    loc = _spec_loc(wall_spec)
    if _validate_wall_refs(wall_spec, room_ids, loc, add):
        return
    a, b = plan.room(wall_spec.room_a), plan.room(wall_spec.room_b)
    assert a is not None and b is not None  # checked above
    if _validate_wall_room_pair(wall_spec, a, b, loc, add):
        return
    _validate_plumbing_wall_usage(wall_spec, a, b, loc, add)


def _validate_wall_refs(wall_spec, room_ids: set[str], loc: dict, add) -> bool:
    missing = [room_id for room_id in (wall_spec.room_a, wall_spec.room_b) if room_id not in room_ids]
    for room_id in missing:
        add(Issue(
            Severity.ERROR,
            "WALL_REF",
            f"Wall statement references unknown room '{room_id}'.",
            room=room_id,
            hint="Reference an existing room id, or declare the room.",
            **loc,
        ))
    return bool(missing)


def _validate_wall_room_pair(wall_spec, a: Room, b: Room, loc: dict, add) -> bool:
    if a.id == b.id:
        add(Issue(
            Severity.ERROR,
            "WALL_NOADJ",
            f"Wall statement names '{a.id}' twice — a wall stands between two different rooms.",
            room=a.id,
            hint="Name the two rooms that flank the wall.",
            **loc,
        ))
        return True
    if shared_edge(a, b) is not None:
        return False
    _add_wall_noadj_issue(wall_spec, a, b, loc, add)
    return True


def _add_wall_noadj_issue(wall_spec, a: Room, b: Room, loc: dict, add) -> None:
    detail = (
        f"they sit on different levels ({a.level} and {b.level})"
        if a.level != b.level
        else "they don't share a wall (a corner touch isn't enough)"
    )
    add(Issue(
        Severity.ERROR,
        "WALL_NOADJ",
        f"`wall {a.id} - {b.id}` declares a wall that doesn't exist — {detail}.",
        room=a.id,
        hint="A wall statement describes the shared wall between two abutting rooms; "
        "reposition them to abut along an edge, or drop the declaration.",
        **loc,
    ))


def _validate_plumbing_wall_usage(wall_spec, a: Room, b: Room, loc: dict, add) -> None:
    if "plumbing" not in wall_spec.attributes or a.type in WET_TYPES or b.type in WET_TYPES:
        return
    add(Issue(
        Severity.INFO,
        "WALL_UNUSED",
        f"The declared plumbing wall between '{a.id}' and '{b.id}' serves no wet room — "
        "neither side is a bath, kitchen, laundry or utility.",
        room=a.id,
        hint="Put the wet wall where the fixtures back onto it, or drop the `plumbing` attribute.",
        **loc,
    ))


def _validate_bearing_wall_axis(plan: Barndominium, add) -> None:
    # A bearing wall the frame can't use: it runs parallel to the bents' span, so
    # it can't split that span into shorter beams. Only meaningful once a frame is
    # requested (without one, the declaration just rides the exchange).
    if plan.frame_spec is None:
        return
    from .structure import bearing_wall_usage

    for wall_spec, _edge, usable in bearing_wall_usage(plan):
        if not usable:
            _add_wall_bearing_axis_issue(wall_spec, add)


def _add_wall_bearing_axis_issue(wall_spec, add) -> None:
    add(Issue(
        Severity.INFO,
        "WALL_BEARING_AXIS",
        f"The declared bearing wall between '{wall_spec.room_a}' and '{wall_spec.room_b}' "
        "runs across the frame's span (parallel to the bents), so it can't carry a "
        "post line — the frame ignored it.",
        room=wall_spec.room_a,
        hint="A post line runs along the building's long axis; declare a wall running "
        "that way as bearing, or leave the span to the auto interior supports.",
        **_spec_loc(wall_spec),
    ))

def _validate_suites_zones(plan: Barndominium, add) -> None:
    """Check declared ``suite`` / ``zone`` statements against the plan."""
    suites = getattr(plan, "suites", None) or []
    zones = getattr(plan, "zones", None) or []
    if not suites and not zones:
        return
    by_id = {r.id: r for r in plan.rooms}
    room_ids = set(by_id)
    suite_ids = {suite.id for suite in suites}

    _validate_suite_shadow(suites, room_ids, add)
    _validate_suite_refs(suites, room_ids, add)
    _validate_suite_overlap(suites, room_ids, add)
    _validate_zone_refs(zones, room_ids, suite_ids, add)
    zrooms, room_zones, zone_loc = _build_zone_membership(plan, by_id, zones)
    _validate_zone_overlap(plan, room_zones, zone_loc, add)
    _validate_zone_cross(plan, by_id, zrooms, room_zones, zone_loc, add)


def _validate_suite_shadow(suites, room_ids: set[str], add) -> None:
    # SUITE_SHADOW: a suite named like a room is ambiguous as a zone member —
    # resolution picks the room, so the suite silently never expands.
    for suite in suites:
        if suite.id not in room_ids:
            continue
        add(Issue(
            Severity.WARNING,
            "SUITE_SHADOW",
            f"Suite '{suite.id}' has the same id as a room; a zone member named "
            f"'{suite.id}' resolves to the ROOM, not the suite.",
            room=suite.id,
            hint="Rename the suite so zone members can reference it unambiguously.",
            **_spec_loc(suite),
        ))


def _validate_suite_refs(suites, room_ids: set[str], add) -> None:
    # SUITE_REF: a suite member must be a real room.
    for suite in suites:
        loc = _spec_loc(suite)
        for member in suite.members:
            if member in room_ids:
                continue
            add(Issue(
                Severity.ERROR,
                "SUITE_REF",
                f"Suite '{suite.id}' references unknown room '{member}'.",
                room=member,
                hint="Reference an existing room id, or declare the room.",
                **loc,
            ))


def _validate_suite_overlap(suites, room_ids: set[str], add) -> None:
    # SUITE_OVERLAP: a room may live in only one suite.
    first_suite: dict[str, str] = {}
    warned_suite: set[str] = set()
    for suite in suites:
        for member in suite.members:
            if member not in room_ids:
                continue
            if member in first_suite and member not in warned_suite:
                _add_suite_overlap(member, first_suite[member], suite, add)
                warned_suite.add(member)
            else:
                first_suite.setdefault(member, suite.id)


def _add_suite_overlap(member: str, first: str, suite, add) -> None:
    add(Issue(
        Severity.WARNING,
        "SUITE_OVERLAP",
        f"Room '{member}' is a member of more than one suite ('{first}' and '{suite.id}').",
        room=member,
        hint="A room belongs to one suite; drop it from all but one.",
        **_spec_loc(suite),
    ))


def _validate_zone_refs(zones, room_ids: set[str], suite_ids: set[str], add) -> None:
    # ZONE_REF: a zone member must be a real room or a declared suite.
    for zone in zones:
        loc = _spec_loc(zone)
        for member in zone.members:
            if member in room_ids or member in suite_ids:
                continue
            add(Issue(
                Severity.ERROR,
                "ZONE_REF",
                f"Zone '{zone.id}' references unknown room or suite '{member}'.",
                room=member,
                hint="Reference an existing room id or a declared suite id.",
                **loc,
            ))


def _build_zone_membership(plan: Barndominium, by_id: dict[str, Room], zones) -> tuple[dict, dict, dict]:
    zrooms = _zone_room_sets(plan, by_id)
    room_zones: dict[str, list[str]] = {}
    for zone in zones:
        for room_id in zrooms.get(zone.id, ()):  # deterministic order via plan rooms below
            room_zones.setdefault(room_id, [])
            if zone.id not in room_zones[room_id]:
                room_zones[room_id].append(zone.id)
    zone_loc = {zone.id: _spec_loc(zone) for zone in zones}
    return zrooms, room_zones, zone_loc


def _validate_zone_overlap(plan: Barndominium, room_zones: dict[str, list[str]], zone_loc: dict, add) -> None:
    # ZONE_OVERLAP: a room may live in only one zone (counting suite expansion).
    for room in plan.rooms:  # plan order → deterministic diagnostics
        zones = room_zones.get(room.id)
        if not zones or len(zones) < 2:
            continue
        zone_list = ", ".join("'" + zone + "'" for zone in zones)
        add(Issue(
            Severity.WARNING,
            "ZONE_OVERLAP",
            f"Room '{room.id}' is in more than one zone ({zone_list}).",
            room=room.id,
            hint="A room belongs to one zone; drop it from all but one (a room inside "
            "a suite is already in that suite's zone).",
            **zone_loc[zones[-1]],
        ))


def _validate_zone_cross(
    plan: Barndominium,
    by_id: dict[str, Room],
    zrooms: dict[str, set[str]],
    room_zones: dict[str, list[str]],
    zone_loc: dict,
    add,
) -> None:
    # ZONE_CROSS: a public room stranded in the private band, or the reverse.
    for room in plan.rooms:
        zone_id = _single_zone_for_cross_check(room, room_zones)
        if zone_id is None:
            continue
        crossing = _zone_crossing(room, zone_id, zrooms, by_id)
        if crossing is None:
            continue
        band, kind = crossing
        add(Issue(
            Severity.INFO,
            "ZONE_CROSS",
            f"{kind.capitalize()} room '{room.id}' ({room.type.value}) sits in zone '{zone_id}', "
            f"which otherwise holds only {band} rooms — a {kind} room in the {band} band.",
            room=room.id,
            hint=f"Move '{room.id}' to a {kind} zone, or regroup the zones so the band is consistent.",
            **zone_loc[zone_id],
        ))


def _single_zone_for_cross_check(room: Room, room_zones: dict[str, list[str]]) -> str | None:
    if room.type not in ZONE_PUBLIC_TYPES and room.type not in ZONE_PRIVATE_TYPES:
        return None
    zones = room_zones.get(room.id)
    if not zones or len(zones) != 1:
        return None  # only reason about a room with a single, unambiguous zone
    return zones[0]


def _zone_crossing(room: Room, zone_id: str, zrooms: dict[str, set[str]], by_id: dict[str, Room]) -> tuple[str, str] | None:
    others = [by_id[other] for other in zrooms[zone_id] if other != room.id and other in by_id]
    other_public = any(other.type in ZONE_PUBLIC_TYPES for other in others)
    other_private = any(other.type in ZONE_PRIVATE_TYPES for other in others)
    is_public = room.type in ZONE_PUBLIC_TYPES
    if is_public and other_private and not other_public:
        return "private", "public"
    if not is_public and other_public and not other_private:
        return "public", "private"
    return None

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
    """Nudge on an auto-placed structural frame (``frame`` directive)."""
    spec = plan.frame_spec
    if spec is None:
        return
    loc = _spec_loc(spec)
    _validate_frame_bay_width(spec, loc, add)
    _validate_interior_post_obstructions(plan, loc, add)
    _validate_posts_in_openings(plan, loc, add)


def _validate_frame_bay_width(spec, loc: dict, add) -> None:
    if spec.bay <= COMFORT_BAY + EPSILON:
        return
    add(Issue(
        Severity.INFO,
        "BAY_WIDE",
        f"Frames are spaced up to {_f(spec.bay)} ft on centre — heavier than "
        f"the ~{COMFORT_BAY:.0f} ft typical of residential post-frame.",
        hint=f"Lower the spacing (e.g. `frame bay {COMFORT_BAY:.0f}`) or have the "
        "engineer size the beams/posts for the wider bay.",
        **loc,
    ))


def _validate_interior_post_obstructions(plan: Barndominium, loc: dict, add) -> None:
    for post in plan.posts:
        if post.role == "interior":
            _validate_post_obstruction(plan, post, loc, add)


def _validate_post_obstruction(plan: Barndominium, post, loc: dict, add) -> None:
    for room in plan.rooms:
        if getattr(room, "level", 0) != 0:
            continue
        inset_x = min(post.x - room.x, room.x2 - post.x)
        inset_y = min(post.y - room.y, room.y2 - post.y)
        if inset_x <= POST_CLEAR_MARGIN or inset_y <= POST_CLEAR_MARGIN:
            continue
        add(Issue(
            Severity.INFO,
            "POST_OBSTRUCT",
            f"An interior support post lands in the open floor of '{room.id}' "
            f"(about {post.x:g},{post.y:g}).",
            room=room.id,
            hint="Align a partition, closet, or island with the post line, or widen "
            "`span` so no interior support is needed.",
            **loc,
        ))
        break  # one note per post is enough


def _validate_posts_in_openings(plan: Barndominium, frame_loc: dict, add) -> None:
    # A post standing inside a window/door opening can't be framed — you can't run
    # a structural column through the glass. The post grid is the fixed discipline,
    # so flag the opening to be shifted into a clear bay (between posts).
    openings: list[tuple[_WallOpening, str]] = [(window, "window") for window in plan.windows]
    openings += [(door, "exterior door") for door in plan.exterior_doors]
    for opening, kind in openings:
        _validate_posts_in_opening(plan, opening, kind, frame_loc, add)


def _validate_posts_in_opening(plan: Barndominium, opening: _WallOpening, kind: str, frame_loc: dict, add) -> None:
    room = plan.room(opening.room)
    if room is None or getattr(room, "level", 0) != 0:
        return
    wall_line, lo, hi, horizontal = _opening_wall_axis(room, opening)
    for post in plan.posts:
        on_line = post.y if horizontal else post.x
        along = post.x if horizontal else post.y
        # Coincident with the wall and *inside* the clear opening (a post at the
        # jamb is how an opening is framed, so endpoints don't count).
        if abs(on_line - wall_line) <= EPSILON and lo + EPSILON < along < hi - EPSILON:
            _add_post_in_opening_issue(post, opening, kind, frame_loc, add)
            break  # one note per opening


def _opening_wall_axis(room: Room, opening: _WallOpening) -> tuple[float, float, float, bool]:
    x1, y1, x2, y2 = opening_endpoints(room, opening.wall, opening.offset, opening.width)
    horizontal = opening.wall in (Direction.NORTH, Direction.SOUTH)
    wall_line = y1 if horizontal else x1
    lo, hi = (min(x1, x2), max(x1, x2)) if horizontal else (min(y1, y2), max(y1, y2))
    return wall_line, lo, hi, horizontal


def _add_post_in_opening_issue(post, opening: _WallOpening, kind: str, frame_loc: dict, add) -> None:
    loc = dict(frame_loc)
    if getattr(opening, "line", None) is not None:
        loc = {"line": opening.line, "col": opening.col, "end_col": opening.end_col}
    add(Issue(
        Severity.WARNING,
        "POST_IN_OPENING",
        f"A structural post at {post.x:g},{post.y:g} stands inside the {kind} on "
        f"'{opening.room}'s {opening.wall.value} wall.",
        room=opening.room,
        hint="Shift the opening along its wall into a clear bay (between posts), or "
        "change `frame bay` so no post lands on it.",
        **loc,
    ))


def _partition_supported_below(plan: Barndominium, lower_level: int, edge) -> bool:
    """Is an upper partition on ``edge`` carried by a wall or beam on the level below?"""
    lo, hi = edge.lo, edge.lo + edge.length
    pos = edge.pos
    vertical = edge.orientation == "v"
    return _partition_supported_by_room_edge(plan, lower_level, lo, hi, pos, vertical) or _partition_supported_by_beam(
        plan, lower_level, lo, hi, pos, vertical
    )


def _spans_overlap(lo: float, hi: float, a_lo: float, a_hi: float) -> bool:
    return min(hi, a_hi) - max(lo, a_lo) > EPSILON


def _partition_supported_by_room_edge(
    plan: Barndominium, lower_level: int, lo: float, hi: float, pos: float, vertical: bool,
) -> bool:
    for room in plan.rooms:
        if getattr(room, "level", 0) != lower_level:
            continue
        if _room_edge_supports_partition(room, lo, hi, pos, vertical):
            return True
    return False


def _room_edge_supports_partition(room: Room, lo: float, hi: float, pos: float, vertical: bool) -> bool:
    if vertical:
        on_line = abs(room.x - pos) <= EPSILON or abs(room.x2 - pos) <= EPSILON
        return on_line and _spans_overlap(lo, hi, room.y, room.y2)
    on_line = abs(room.y - pos) <= EPSILON or abs(room.y2 - pos) <= EPSILON
    return on_line and _spans_overlap(lo, hi, room.x, room.x2)


def _partition_supported_by_beam(
    plan: Barndominium, lower_level: int, lo: float, hi: float, pos: float, vertical: bool,
) -> bool:
    # A beam (bent/ridge) running under the partition line supports it too.
    for beam in plan.beams:
        if getattr(beam, "level", 0) != lower_level:
            continue
        if _beam_supports_partition(beam, lo, hi, pos, vertical):
            return True
    return False


def _beam_supports_partition(beam, lo: float, hi: float, pos: float, vertical: bool) -> bool:
    if vertical and beam.orientation == "v" and abs(beam.x1 - pos) <= EPSILON:
        return _spans_overlap(lo, hi, min(beam.y1, beam.y2), max(beam.y1, beam.y2))
    if not vertical and beam.orientation == "h" and abs(beam.y1 - pos) <= EPSILON:
        return _spans_overlap(lo, hi, min(beam.x1, beam.x2), max(beam.x1, beam.x2))
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
    """Per-room electrical checks for plans that opt into the electrical layer."""
    if not (plan.outlets or plan.switches or plan.lights):
        return
    by_id = {r.id: r for r in plan.rooms}
    outlets_by = _outlets_by_room(plan)
    powered = _powered_rooms(plan, outlets_by)
    lit = {light.room for light in plan.lights}

    _validate_gfci_outlets(plan, by_id, add)
    _validate_receptacle_spacing(by_id, outlets_by, add)
    _validate_counter_receptacles(plan, outlets_by, add)
    _validate_lighting_outlets(by_id, powered, lit, add)


def _outlets_by_room(plan: Barndominium) -> dict[str, list]:
    outlets_by: dict[str, list] = {}
    for outlet in plan.outlets:
        outlets_by.setdefault(outlet.room, []).append(outlet)
    return outlets_by


def _powered_rooms(plan: Barndominium, outlets_by: dict[str, list]) -> set[str]:
    powered: set[str] = set(outlets_by)
    for switch in plan.switches:
        powered.add(switch.room)
    return powered


def _validate_gfci_outlets(plan: Barndominium, by_id: dict[str, Room], add) -> None:
    # GFCI — a receptacle in a wet/damp room that isn't ground-fault protected.
    for outlet in plan.outlets:
        room = by_id.get(outlet.room)
        if room is None or room.type not in WET_TYPES or outlet.gfci:
            continue
        add(Issue(
            Severity.WARNING,
            "OUTLET_GFCI",
            f"A receptacle in the {room.display_name.lower()} "
            f"({room.type.value.replace('_', ' ')}) isn't marked `gfci` — "
            "IRC E3902 requires ground-fault protection there.",
            room=outlet.room,
            line=outlet.line,
            col=outlet.col,
            end_col=outlet.end_col,
            hint="Add `gfci` to the outlet, or protect the circuit at the panel and "
            "note it on the electrical plan.",
        ))


def _validate_receptacle_spacing(by_id: dict[str, Room], outlets_by: dict[str, list], add) -> None:
    # Receptacle spacing — only habitable rooms that opted in by drawing an outlet.
    from .render import fmt_ft_in

    for room_id, outlets in outlets_by.items():
        room = by_id.get(room_id)
        if room is None or room.type not in HABITABLE_TYPES:
            continue
        reach = _receptacle_reach(room, outlets)
        if reach <= 6.0 + 1e-6:
            continue
        add(Issue(
            Severity.WARNING,
            "OUTLET_SPACING",
            f"In {room.display_name}, a point on the wall is up to {fmt_ft_in(reach)} "
            "from the nearest receptacle — IRC E3901.2 allows no more than 6 ft "
            "(a receptacle at least every 12 ft of wall run).",
            room=room_id,
            hint="Add an `outlet` in the widest gap so no wall point is more than 6 ft from one.",
        ))


def _validate_counter_receptacles(plan: Barndominium, outlets_by: dict[str, list], add) -> None:
    # RECEPTACLE_COUNTER — IRC E3901.4 kitchen small-appliance receptacles. Along
    # each kitchen counter run (>= 12 in wide), no point on the counter wall line
    # may be more than 24 in from a receptacle. Gated the same way as OUTLET_SPACING.
    from .fixtures import resolve_room_fixtures

    for room in plan.rooms:
        if room.type is not RoomType.KITCHEN:
            continue
        counters = [
            fixture
            for fixture in resolve_room_fixtures(plan, room)
            if fixture.kind == "counter" and fixture.wall in ("S", "N", "E", "W")
        ]
        for counter in counters:
            _validate_counter_run_receptacles(room, counter, outlets_by.get(room.id, []), add)


def _validate_counter_run_receptacles(room: Room, counter, room_outlets: list, add) -> None:
    from .render import fmt_ft_in

    lo, hi, wall_dir = _counter_run_span(room, counter)
    width = hi - lo
    if width + 1e-9 < _COUNTER_MIN_WIDTH:
        return  # a run under 12 in takes no receptacle (E3901.4.3(1))
    gap = _counter_receptacle_gap(room, wall_dir, lo, hi, room_outlets)
    if gap <= _COUNTER_MAX_REACH + 1e-6:
        return
    add(Issue(
        Severity.WARNING,
        "RECEPTACLE_COUNTER",
        f"The {fmt_ft_in(width)} kitchen counter run in '{room.id}' (on its {counter.wall} wall) "
        f"leaves a point {fmt_ft_in(gap)} from the nearest receptacle — IRC E3901.4 "
        "wants a small-appliance receptacle within 24 in of every point along a counter "
        "(and one on any counter >= 12 in wide).",
        room=room.id,
        hint="Add a receptacle on the counter wall in the gap, e.g. "
        f"`outlet in {room.id} wall {counter.wall} offset <ft>` — no point along the "
        "counter should be more than 24 in from one.",
    ))


def _counter_receptacle_gap(room: Room, wall_dir: Direction, lo: float, hi: float, room_outlets: list) -> float:
    base = room.x if wall_dir in (Direction.SOUTH, Direction.NORTH) else room.y
    points = sorted(
        base + outlet.offset
        for outlet in room_outlets
        if outlet.wall is wall_dir and lo - 1e-6 <= base + outlet.offset <= hi + 1e-6
    )
    if not points:
        return hi - lo  # the whole run is unserved
    gaps = [points[0] - lo, hi - points[-1]]
    gaps += [(points[i + 1] - points[i]) / 2.0 for i in range(len(points) - 1)]
    return max(gaps)


def _validate_lighting_outlets(by_id: dict[str, Room], powered: set[str], lit: set[str], add) -> None:
    # Lighting outlet — a habitable room with power but nothing to switch on.
    for room_id in sorted(powered):
        room = by_id.get(room_id)
        if room is None or room.type not in HABITABLE_TYPES or room_id in lit:
            continue
        add(Issue(
            Severity.INFO,
            "ROOM_NO_LIGHT",
            f"{room.display_name} draws receptacles/switches but no lighting outlet — "
            "IRC E3903 wants a wall-switch-controlled light in every habitable room.",
            room=room_id,
            hint="Add a `light in " + room_id + " at <x>,<y>` (or note a switched receptacle).",
        ))


#: The rooms where lack of winter sun (a north-only aspect) most hurts comfort.
#: An office is excluded on purpose: even, glare-free north light is a legitimate
#: choice for a studio/workspace, so "lit only from the north" isn't a defect there.
_SUN_WANTED_TYPES = {
    RoomType.LIVING,
    RoomType.GREAT_ROOM,
    RoomType.DINING,
    RoomType.BEDROOM,
    RoomType.KITCHEN,
    RoomType.REC_ROOM,
}
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
    """Solar-glazing nudges — only when the plan declares an ``orientation``."""
    theta = plan.orientation
    if theta is None:
        return
    for room in plan.rooms:
        if room.type in HABITABLE_TYPES:
            _validate_room_solar(plan, room, theta, add)
    _validate_south_wall_unused(plan, theta, add)
    _validate_south_overhang(plan, theta, add)


def _validate_room_solar(plan: Barndominium, room: Room, theta: float, add) -> None:
    windows = plan.windows_for(room.id)
    if not windows:
        return
    by_sector = _room_glazing_by_sector(windows, theta)
    _validate_west_solar_gain(plan, room, windows, theta, add)
    _validate_north_only_solar(plan, room, by_sector, theta, add)


def _room_glazing_by_sector(windows: list, theta: float) -> dict[str, float]:
    by_sector: dict[str, float] = {}
    for window in windows:
        sector = wall_sector(window.wall, theta)
        by_sector[sector] = by_sector.get(sector, 0.0) + window.glazed_area
    return by_sector


def _validate_west_solar_gain(plan: Barndominium, room: Room, windows: list, theta: float, add) -> None:
    # Too much *unshaded* west glass — overheats in the afternoon. A covered porch
    # gives the vertical shade a low west sun needs, so glass behind one doesn't count.
    west_windows = [
        window
        for window in windows
        if wall_sector(window.wall, theta) == "west" and not _shaded_by_covered_porch(plan, room, window)
    ]
    west_glass = sum(window.glazed_area for window in west_windows)
    if west_glass <= SOLAR_WEST_MAX_GLAZING:
        return
    widest = max(west_windows, key=lambda window: window.glazed_area)
    azimuth = true_azimuth(widest.wall, theta)
    add(Issue(
        Severity.INFO,
        "SOLAR_WEST_GAIN",
        f"'{room.id}' has {_f(west_glass)} sq ft of glazing facing {compass_label(azimuth)} "
        f"({azimuth:.0f}° true) — a low afternoon sun that overheats the room and is hard to shade.",
        room=room.id,
        hint="Shade it with a deep overhang/porch or an awning, cut the west glass back, or move it to the south face.",
    ))


def _validate_north_only_solar(plan: Barndominium, room: Room, by_sector: dict[str, float], theta: float, add) -> None:
    # Glazed only to the north — dim, cold — with a sunnier wall to spare.
    total = sum(by_sector.values())
    if room.type not in _SUN_WANTED_TYPES or total <= EPSILON or by_sector.get("north", 0.0) + EPSILON < total:
        return
    alternatives = _sunny_wall_alternatives(plan, room, theta)
    if not alternatives:
        return
    best = alternatives[0]
    bearing = compass_label(true_azimuth(best, theta))
    add(Issue(
        Severity.INFO,
        "SOLAR_NORTH_ONLY",
        f"'{room.id}' is glazed only to the north — little direct sun, so it will feel dim and cold in winter.",
        room=room.id,
        hint=f"Add a window on the {best.value} wall (faces {bearing}) for winter sun, "
        f"e.g. `window {room.id} {best.value} width 4 offset 2`.",
    ))


def _sunny_wall_alternatives(plan: Barndominium, room: Room, theta: float) -> list[Direction]:
    return sorted(
        (wall for wall in exterior_walls(plan, room) if wall_sector(wall, theta) != "north"),
        key=lambda wall: _SECTOR_PREF[wall_sector(wall, theta)],
    )


def _validate_south_wall_unused(plan: Barndominium, theta: float, add) -> None:
    south_wall = sum(
        _wall_length(room, wall)
        for room in plan.rooms
        for wall in exterior_walls(plan, room)
        if wall_sector(wall, theta) == "south"
    )
    south_glass = sum(window.glazed_area for window in plan.windows if wall_sector(window.wall, theta) == "south")
    if south_wall < SOLAR_SOUTH_MIN_WALL or south_glass >= SOLAR_SOUTH_MIN_GLAZING:
        return
    add(Issue(
        Severity.INFO,
        "SOLAR_SOUTH_UNUSED",
        f"The plan has ~{_f(south_wall)} ft of south-facing wall but only {_f(south_glass)} "
        "sq ft of south glazing — the best passive-solar face is nearly blank.",
        hint="Add south-facing windows for free winter sun (with an overhang or porch to block the high summer sun).",
    ))


def _validate_south_overhang(plan: Barndominium, theta: float, add) -> None:
    south_unshaded = sum(
        window.glazed_area
        for window in plan.windows
        if wall_sector(window.wall, theta) == "south" and not _window_shaded_by_porch(plan, window)
    )
    if plan.overhang >= MIN_SHADE_OVERHANG or south_unshaded <= SOLAR_SOUTH_SHADE_GLAZING:
        return
    add(Issue(
        Severity.INFO,
        "SOLAR_SOUTH_NO_OVERHANG",
        f"{_f(south_unshaded)} sq ft of south glazing has no roof overhang to shade it — "
        "the high summer sun will overheat those rooms.",
        hint="Add a ~2 ft eave (`overhang 2`) or a covered porch over the south glass — "
        "it blocks the high summer sun but still lets the low winter sun in.",
    ))


def _window_shaded_by_porch(plan: Barndominium, window) -> bool:
    room = plan.room(window.room)
    return room is not None and _shaded_by_covered_porch(plan, room, window)

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
    if plan.exterior_doors and not _has_code_egress_door(plan):
        _add_egress_door_issue(add)

    for room in plan.rooms:
        walls = exterior_walls(plan, room)
        if room.type is RoomType.BEDROOM:
            _validate_bedroom_egress(plan, room, walls, add, profile)
        if room.type in BATH_TYPES:
            _validate_bath_vent(plan, room, walls, add)
        if room.type in HABITABLE_TYPES:
            _validate_habitable_natural_light(plan, room, walls, add, profile)
            _validate_habitable_vent(plan, room, walls, add)


def _has_code_egress_door(plan: Barndominium) -> bool:
    # An overhead door never counts as egress (`entrance` forces egress=False
    # for it; the kind check guards a hand-built ExteriorDoor too). A double
    # door counts one leaf (see _door_clear_width).
    return any(
        d.egress
        and not d.overhead
        and _door_clear_width(d) + EPSILON >= MIN_EGRESS_DOOR_WIDTH
        for d in plan.exterior_doors
    )


def _add_egress_door_issue(add) -> None:
    add(Issue(
        Severity.WARNING,
        "EGRESS_DOOR",
        f"No exterior egress door is at least {MIN_EGRESS_DOOR_WIDTH * 12:.0f} in wide.",
        hint=f"Make at least one `entry` width >= {MIN_EGRESS_DOOR_WIDTH:g} "
        "(a double/french pair counts one leaf, so it needs twice that).",
    ))


def _validate_bedroom_egress(
    plan: Barndominium, room: Room, walls: list[Direction], add, profile: Profile,
) -> None:
    ext_windows = [w for w in plan.windows_for(room.id) if w.wall in walls]
    escape_windows = [w for w in ext_windows if getattr(w, "kind", "casement") != "fixed"]
    ext_doors = [d for d in plan.exterior_doors_for(room.id) if d.wall in walls]
    if not escape_windows and not ext_doors:
        _add_bedroom_egress_issue(room, walls, ext_windows, add)
        return
    _validate_bedroom_egress_size(room, walls, escape_windows, ext_doors, add, profile)


def _add_bedroom_egress_issue(room: Room, walls: list[Direction], ext_windows: list, add) -> None:
    only_fixed = bool(ext_windows)
    add(Issue(
        Severity.ERROR,
        "BEDROOM_EGRESS",
        "Bedroom has no emergency escape opening"
        + (" — its only exterior windows are fixed glass, which doesn't open." if only_fixed else "."),
        room=room.id,
        hint=_bedroom_egress_hint(room, walls, ext_windows, only_fixed),
    ))


def _bedroom_egress_hint(room: Room, walls: list[Direction], ext_windows: list, only_fixed: bool) -> str:
    if only_fixed:
        return (
            "Fixed glass doesn't open — make a window operable "
            f"(casement/slider/double-hung), e.g. `window {room.id} "
            f"{ext_windows[0].wall.value} width 4 offset 2`."
        )
    if walls:
        return (
            f"Add an egress window on an exterior wall, e.g. `window {room.id} "
            f"{walls[0].value} width 4 offset 2`."
        )
    return (
        f"'{room.id}' has no exterior wall — relocate it to the building perimeter "
        "so it can have an egress window."
    )


def _validate_bedroom_egress_size(
    room: Room, walls: list[Direction], escape_windows: list, ext_doors: list, add, profile: Profile,
) -> None:
    min_area, min_ow, min_oh, max_sill = _egress_thresholds(room, profile)
    door_ok = any(_door_clear_width(d) + EPSILON >= min_ow for d in ext_doors)
    window_ok = any(_escape_window_ok(w, min_ow, min_oh, min_area, max_sill) for w in escape_windows)
    if door_ok or window_ok:
        return
    detail = _egress_size_detail(escape_windows)
    add(Issue(
        Severity.WARNING,
        "EGRESS_SIZE",
        f"Bedroom '{room.id}' has an escape opening but it's below "
        f"{_egress_profile_rule(profile)} ({_f(min_area)} sq ft clear, "
        f"{min_ow * 12:.0f} in wide × {min_oh * 12:.0f} in tall, sill "
        f"<= {max_sill * 12:.0f} in); {detail}.",
        room=room.id,
        hint=f"Widen/enlarge the egress window so its clear opening is >= {_f(min_area)} "
        "sq ft (a casement clears ~its full glazed size; a slider ~half its width; "
        "a double-hung ~half its height), e.g. "
        f"`window {room.id} {(walls[0].value if walls else 'south')} width 4 offset 2`.",
    ))


def _egress_thresholds(room: Room, profile: Profile) -> tuple[float, float, float, float]:
    min_area = profile.min_egress_area_grade if room.level == 0 else profile.min_egress_area
    return (
        min_area,
        profile.min_egress_opening_width,
        profile.min_egress_opening_height,
        profile.max_egress_sill,
    )


def _escape_window_ok(w, min_ow: float, min_oh: float, min_area: float, max_sill: float) -> bool:
    cw, ch = w.clear_opening
    return (
        cw + EPSILON >= min_ow
        and ch + EPSILON >= min_oh
        and cw * ch + EPSILON >= min_area
        and w.sill_height <= max_sill + EPSILON
    )


def _egress_size_detail(escape_windows: list) -> str:
    if not escape_windows:
        return "its only exterior opening is a too-narrow door"
    best = max(escape_windows, key=lambda w: w.clear_opening[0] * w.clear_opening[1])
    cw, ch = best.clear_opening
    kind_note = "" if best.kind == "casement" else f" {best.kind}"
    return (
        f"its largest{kind_note} clears ~{_f(cw)} ft wide × {_f(ch)} ft "
        f"({_f(cw * ch)} sq ft, sill {best.sill_height * 12:.0f} in)"
    )


def _egress_profile_rule(profile: Profile) -> str:
    fields = (
        "min_egress_area", "min_egress_area_grade", "min_egress_opening_width",
        "min_egress_opening_height", "max_egress_sill",
    )
    if any(_amended(profile, field) for field in fields):
        return f"the '{profile.name}' profile's escape-opening minimum"
    return "the IRC R310 minimum"


def _validate_bath_vent(plan: Barndominium, room: Room, walls: list[Direction], add) -> None:
    # A bathroom needs light+ventilation: a window on an exterior wall, or else
    # mechanical exhaust. The DSL doesn't model fans, so a windowless bath gets
    # an info nudge to confirm one (IRC R303.3).
    has_window = any(w.wall in walls for w in plan.windows_for(room.id))
    if has_window:
        return
    add(Issue(
        Severity.INFO,
        "BATH_VENT",
        f"Bathroom '{room.id}' has no exterior window; it needs mechanical ventilation (IRC R303.3).",
        room=room.id,
        hint="Add an exterior window, or confirm an exhaust fan vented outside — "
        "the DSL can't see fans, so this is just a reminder.",
    ))


def _validate_habitable_natural_light(
    plan: Barndominium, room: Room, walls: list[Direction], add, profile: Profile,
) -> None:
    glazing = sum(w.glazed_area for w in plan.windows_for(room.id) if w.wall in walls)
    light_ratio = profile.natural_light_ratio
    required = room.area * light_ratio
    if glazing + EPSILON >= required:
        return
    add(Issue(
        Severity.WARNING,
        "NAT_LIGHT",
        f"Glazing {_f(glazing)} sq ft is below the natural-light minimum of {_f(required)} sq ft "
        f"({light_ratio * 100:.0f}% of floor area)"
        + _profile_tag(profile, "natural_light_ratio", f"{NATURAL_LIGHT_RATIO * 100:.0f}%")
        + ".",
        room=room.id,
        hint=_natural_light_hint(room, walls, required, glazing),
    ))


def _natural_light_hint(room: Room, walls: list[Direction], required: float, glazing: float) -> str:
    add_width = max(0.0, (required - glazing) / _WINDOW_TYP_HEIGHT)
    suggest = _suggest_int(add_width)
    if walls and suggest is not None:
        return (
            f"Add ~{suggest} ft of window width on an exterior wall, e.g. `window {room.id} "
            f"{walls[0].value} width {max(3, suggest)} offset 2`."
        )
    if walls:
        return f"Add more window area on an exterior wall (e.g. the {walls[0].value} wall)."
    return (
        f"'{room.id}' has no exterior wall — open it to an adjacent room (an open-concept "
        "layout) or move it to the perimeter."
    )


def _validate_habitable_vent(plan: Barndominium, room: Room, walls: list[Direction], add) -> None:
    # Natural ventilation (IRC R303.1): a habitable room needs OPENABLE window
    # area >= 4% of its floor. Fixed glass daylights but opens nothing, so it's
    # excluded here — the gap NAT_LIGHT can't see.
    openable = sum(
        w.glazed_area
        for w in plan.windows_for(room.id)
        if w.wall in walls and getattr(w, "openable", w.kind != "fixed")
    )
    vent_required = room.area * NATURAL_VENT_RATIO
    if openable + EPSILON >= vent_required:
        return
    fixed_here = any(
        w.wall in walls and not getattr(w, "openable", w.kind != "fixed")
        for w in plan.windows_for(room.id)
    )
    add(Issue(
        Severity.WARNING,
        "VENT_AREA",
        f"Openable window area {_f(openable)} sq ft is below the natural-ventilation minimum "
        f"of {_f(vent_required)} sq ft ({NATURAL_VENT_RATIO * 100:.0f}% of floor area, IRC R303.1).",
        room=room.id,
        hint=_vent_hint(room, walls, fixed_here),
    ))


def _vent_hint(room: Room, walls: list[Direction], fixed_here: bool) -> str:
    if not walls:
        return (
            f"'{room.id}' has no exterior wall — open it to an adjacent room or move it "
            "to the perimeter so it can be ventilated."
        )
    if fixed_here:
        return (
            "A `fixed` window opens nothing — make one operable (casement/slider/double-hung), "
            f"e.g. `window {room.id} {walls[0].value} width 4 offset 2`, or confirm mechanical ventilation."
        )
    return (
        f"Add openable window area on an exterior wall, e.g. `window {room.id} "
        f"{walls[0].value} width 4 offset 2`, or confirm mechanical ventilation."
    )
