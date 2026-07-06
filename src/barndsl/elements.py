"""Core geometric and semantic elements of a barndominium floor plan.

Coordinate convention
---------------------
All measurements are in **feet**. The plan lives in a single 2D coordinate
system whose origin ``(0, 0)`` is the **bottom-left (south-west) corner** of the
building envelope:

* ``x`` increases to the **east** (rightward).
* ``y`` increases to the **north** (upward / away from the viewer).

So a room's ``south`` wall is its low-``y`` edge, ``north`` its high-``y`` edge,
``west`` its low-``x`` edge and ``east`` its high-``x`` edge. The same convention
is used by the renderer (which flips ``y`` for screen space) and the agent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .constants import (
    DEFAULT_ROOF_PITCH,
    FLOOR_ASSEMBLY_DEPTH,
    FOOTING_DEPTH,
    FOOTING_SIZE,
    SLAB_THICKNESS,
    TURNDOWN_DEPTH,
    TURNDOWN_WIDTH,
)

# --- Units ------------------------------------------------------------------

#: Internal unit is the foot. These helpers keep DSL authoring readable.
FOOT = 1.0


def feet(value: float) -> float:
    """Return a measurement expressed in feet (identity; for readability)."""
    return float(value)


def inches(value: float) -> float:
    """Convert inches to the internal unit (feet)."""
    return float(value) / 12.0


#: The roof forms a plan can request (see :meth:`Barndominium.roof`).
ROOF_STYLES = ("gable", "shed", "monitor")

#: Default width for an open cased passage (walk-through) when none is given.
#: Walk-throughs are wide by design, so this is generous next to a 32-in door.
DEFAULT_OPENING_WIDTH = feet(6)


def _finite(room_id: str, field: str, value: float) -> float:
    """Coerce ``value`` to a finite float, or raise a clear ValueError.

    The textual front-end rejects non-finite numbers at parse time; this gives
    the Python builder the same guarantee so a NaN/inf can't slip into geometry
    (where it would defeat every comparison and corrupt the round-trip).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Room '{room_id}': {field} must be a number, got {value!r}.")
    if not math.isfinite(v):
        raise ValueError(f"Room '{room_id}': {field} must be a finite number, got {value!r}.")
    return v


# --- Enums ------------------------------------------------------------------


class RoomType(str, Enum):
    """The kinds of spaces a barndominium plan can contain."""

    LIVING = "living"
    KITCHEN = "kitchen"
    DINING = "dining"
    BEDROOM = "bedroom"
    BATHROOM = "bathroom"
    HALF_BATH = "half_bath"
    LAUNDRY = "laundry"
    UTILITY = "utility"
    HALLWAY = "hallway"
    CLOSET = "closet"
    PANTRY = "pantry"
    MUDROOM = "mudroom"
    OFFICE = "office"
    LOFT = "loft"
    GARAGE = "garage"
    SHOP = "shop"
    PORCH = "porch"
    OTHER = "other"


#: Rooms intended for living/sleeping. Drive code checks (egress, light, ceiling).
HABITABLE_TYPES: frozenset[RoomType] = frozenset(
    {
        RoomType.LIVING,
        RoomType.KITCHEN,
        RoomType.DINING,
        RoomType.BEDROOM,
        RoomType.OFFICE,
        RoomType.LOFT,
    }
)

#: Spaces that count as conditioned interior floor area for metrics.
INTERIOR_TYPES: frozenset[RoomType] = frozenset(
    set(RoomType) - {RoomType.PORCH, RoomType.GARAGE, RoomType.SHOP}
)

#: Garage-like spaces for the IRC R302.6/R302.5.1 separation checks. A
#: barndominium's shop bay is functionally a garage — an overhead door, vehicles,
#: equipment and fuel — so it carries the same dwelling-separation requirements.
GARAGE_TYPES: frozenset[RoomType] = frozenset({RoomType.GARAGE, RoomType.SHOP})


class Direction(str, Enum):
    """A cardinal wall of a rectangular room."""

    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"

    def opposite(self) -> "Direction":
        """The wall facing the other way (north↔south, east↔west)."""
        return _OPPOSITE_DIR[self]


_OPPOSITE_DIR: dict[Direction, Direction] = {
    Direction.NORTH: Direction.SOUTH,
    Direction.SOUTH: Direction.NORTH,
    Direction.EAST: Direction.WEST,
    Direction.WEST: Direction.EAST,
}


# --- Geometry ---------------------------------------------------------------


@dataclass
class Room:
    """An axis-aligned rectangular space.

    ``(x, y)`` is the bottom-left corner; ``width`` runs east (``+x``) and
    ``length`` runs north (``+y``).
    """

    id: str
    type: RoomType
    x: float
    y: float
    width: float
    length: float
    label: str | None = None
    #: Floor level (0 = ground). A loft sits on level 1 *above* a ground room,
    #: so rooms on different levels may share a footprint without overlapping.
    level: int = 0
    #: Optional per-room finished ceiling height (ft), overriding the plan default.
    #: A tray/dropped soffit or a taller great room lives here; ``None`` inherits
    #: the plan's ``ceiling``.
    ceiling_height: float | None = None
    #: A vaulted / cathedral room open to the roof — no flat ceiling plane. Its
    #: usable height rises to the ridge, so a flat ceiling isn't built for it.
    vaulted: bool = False
    #: Optional free-text floor-finish hint (e.g. "tile", "polished concrete",
    #: "wood plank"), fuzzy-matched to the 3D material palette (see
    #: :mod:`barndsl.materials`). ``None`` inherits a default finish by room type.
    floor: str | None = None
    #: How the room was placed, for diagnostics — e.g. "east_of kitchen" for a
    #: relative anchor, or None for an absolute position. Not serialised.
    placement: str | None = None

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.length

    @property
    def area(self) -> float:
        return self.width * self.length

    @property
    def min_dimension(self) -> float:
        return min(self.width, self.length)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.length / 2.0)

    @property
    def display_name(self) -> str:
        return self.label or self.id.replace("_", " ").title()

    def overlaps(self, other: "Room", tol: float = 1e-6) -> float:
        """Return the overlapping area with ``other`` (0 if they only touch)."""
        dx = min(self.x2, other.x2) - max(self.x, other.x)
        dy = min(self.y2, other.y2) - max(self.y, other.y)
        if dx > tol and dy > tol:
            return dx * dy
        return 0.0


#: The interior-door kinds. ``swing`` is a hinged leaf (the default); ``cased``
#: is an open walk-through (no leaf — the old ``open``); ``pocket``/``sliding``
#: are sliding leaves (no swing arc); ``double`` is a pair of hinged half-width
#: leaves and ``french`` its glazed variant (both swing). All join the two rooms
#: in the circulation graph; they differ in how they render and which checks apply.
DOOR_KINDS = ("swing", "cased", "pocket", "sliding", "double", "french")

#: Door kinds whose opening is a pair of half-width leaves. Egress and
#: accessibility clear widths count **one** leaf (IRC R311.2 — the required
#: egress door provides its 32 in clear through a single leaf), so the checks
#: divide a double's total width by two.
DOUBLE_LEAF_KINDS = frozenset({"double", "french"})

#: Default total width (feet) of a double/french door when none is given: the
#: stock 60 in pair (two 30 in leaves).
DEFAULT_DOUBLE_DOOR_WIDTH = 60 / 12.0

#: The exterior-door kinds. ``entry`` is a hinged people-door (the default);
#: ``double``/``french`` are a pair of hinged half-width leaves (a patio /
#: front-entry pair — egress counts one leaf, see :data:`DOUBLE_LEAF_KINDS`);
#: ``overhead`` is a sectional/overhead garage door — vehicle access on a
#: garage/shop bay. An overhead door has no swing, is never an egress door, and
#: doesn't count as a building entrance (the plan still needs an ``entry``).
EXTERIOR_DOOR_KINDS = ("entry", "overhead", "double", "french")

#: Overhead (sectional garage) door defaults: the residential 9 x 7 single.
#: A double is ``width 16``; stock heights are 7 or 8 ft.
OVERHEAD_DOOR_WIDTH = 9.0
OVERHEAD_DOOR_HEIGHT = 7.0


@dataclass
class InteriorDoor:
    """A connection between two adjacent rooms.

    ``kind`` is one of :data:`DOOR_KINDS`. ``leaf`` (a derived property) is True
    for every kind except ``cased`` — a cased opening is the doorless
    walk-through (open-concept link) between e.g. a kitchen and a living area.
    """

    room_a: str
    room_b: str
    width: float = inches(32)
    kind: str = "swing"
    #: Distance (ft) from the **south/west end** of the shared wall to the near
    #: edge of the door. ``None`` centres it on the shared wall (the default).
    offset: float | None = None
    #: The room the leaf swings *into* (must be ``room_a`` or ``room_b``).
    #: ``None`` lets the renderer pick a side; a value also enables the
    #: swing-clearance check.
    swing_into: str | None = None
    #: Which end of the opening the hinge is on: ``"near"`` (the south/west end,
    #: default) or ``"far"``. ``None`` means near.
    hinge: str | None = None
    #: Source location of the statement that created this door (textual DSL
    #: front-end only); lets diagnostics point at the `door` line, not a room.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def leaf(self) -> bool:
        """True for any door with a leaf (everything but a cased opening)."""
        return self.kind != "cased"


@dataclass
class ExteriorDoor:
    """A doorway from a room to the outside, on one of its walls."""

    room: str
    wall: Direction
    width: float = feet(3)
    #: Distance (ft) from the wall's start corner (south/west end) to the
    #: near edge of the opening.
    offset: float = 1.0
    egress: bool = True
    #: One of :data:`EXTERIOR_DOOR_KINDS`. ``overhead`` is a sectional garage
    #: door (a 9 x 7 single; 16 wide for a double) — it rides up its tracks, so
    #: it has no swing and never counts as an egress door or a people-entry.
    kind: str = "entry"
    #: Opening height (ft). ``None`` means the standard 6'-8" leaf; an overhead
    #: door defaults to 7 ft (set by :meth:`Barndominium.entrance`).
    height: float | None = None
    #: Source location of the `entry` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def overhead(self) -> bool:
        """True for a sectional/overhead (garage) door — no leaf, no egress."""
        return self.kind == "overhead"


#: The window kinds a plan can declare. ``casement`` is the **default** — it is
#: the only kind whose full glazed size is also its clear opening, which matches
#: the compiler's historical egress math, so plans written before window kinds
#: existed keep exactly the same diagnostics. A ``fixed`` window is glass that
#: doesn't open: it still daylights (NAT_LIGHT) but is **never** an escape
#: opening (BEDROOM_EGRESS / EGRESS_SIZE ignore it).
WINDOW_KINDS = ("casement", "slider", "fixed", "double-hung")

#: Per-kind ``(width, height)`` clear-opening fractions of the glazed size —
#: the honest escape-opening math behind EGRESS_SIZE. A casement swings its
#: whole sash out (~full opening; modelled as 1.0). A slider opens one of two
#: horizontal panels, so roughly half its glazed *width* is clear. A double-hung
#: raises one of two sashes, so roughly half its glazed *height* is clear.
#: Fixed glass opens nothing.
WINDOW_CLEAR_FACTORS: dict[str, tuple[float, float]] = {
    "casement": (1.0, 1.0),
    "slider": (0.5, 1.0),
    "double-hung": (1.0, 0.5),
    "fixed": (0.0, 0.0),
}


@dataclass
class Window:
    """A window on an exterior-facing wall of a room."""

    room: str
    wall: Direction
    width: float = feet(4)
    offset: float = 2.0
    sill_height: float = feet(3)
    head_height: float = feet(6.67)
    #: One of :data:`WINDOW_KINDS`. Defaults to ``casement`` (see there for why).
    kind: str = "casement"
    #: Author-declared safety (tempered) glazing — the R308.4 escape hatch. When
    #: ``True`` the window is already specified as safety glass, so the
    #: WINDOW_TEMPERED hazard-location warning is silenced for it and the window
    #: schedule reads "tempered (declared)" rather than "tempered (required)".
    tempered: bool = False
    #: Source location of the `window` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def glazed_area(self) -> float:
        return self.width * max(0.0, self.head_height - self.sill_height)

    @property
    def openable(self) -> bool:
        """Can this window open for ventilation? A ``fixed`` window is sealed
        glass — it daylights but provides no openable area (IRC R303.1's 4%
        ventilation floor, VENT_AREA). Every other kind opens."""
        return self.kind != "fixed"

    @property
    def escape_capable(self) -> bool:
        """Can this window be an emergency escape opening at all? Fixed glass
        doesn't open, so it can never be one (IRC R310)."""
        return self.kind != "fixed"

    @property
    def clear_opening(self) -> tuple[float, float]:
        """The net clear ``(width, height)`` this window can open to, per its
        kind (:data:`WINDOW_CLEAR_FACTORS`) — the figure EGRESS_SIZE checks."""
        fw, fh = WINDOW_CLEAR_FACTORS.get(self.kind, (1.0, 1.0))
        glass_h = max(0.0, self.head_height - self.sill_height)
        return self.width * fw, glass_h * fh


@dataclass
class PlacedFixture:
    """An author-placed fixture or furnishing (the ``fixture`` statement).

    ``kind`` is a catalog kind (see :data:`barndsl.fixtures.FIXTURE_KINDS`), placed
    in room ``room``. Position is either explicit — ``x``/``y`` are **room-local**
    feet measured from the room's south-west corner — or omitted, in which case it
    auto-places against ``wall`` (S/N/E/W) or, failing that, the first free spot.
    ``rotation`` turns it in plan (degrees, snapped to a quarter-turn by the
    massing); ``width`` overrides the nominal run of a resizable piece (a counter).

    Authored fixtures **add** to a room's auto-seeds, except that one of a seeded
    kind **replaces** that kind's seed — see
    :func:`barndsl.fixtures.resolve_room_fixtures`.
    """

    kind: str
    room: str
    x: float | None = None  # room-local (offset from the room's SW corner), ft
    y: float | None = None
    wall: Direction | None = None
    rotation: float = 0.0
    width: float | None = None  # override the nominal run (resizable fixtures)
    #: An **along-run** counter (the ``fixture counter in <room> along <wall>``
    #: sugar). When set, the fixture is a wall-backed countertop spanning ``wall``:
    #: its footprint is derived from the room's wall at resolve time (SW-corner
    #: room-local, like every other wall offset), so geometry/validation/render/
    #: compose all consume the desugared wall + span + depth exactly as for any
    #: wall-backed piece. The sugar is kept here only so ``emit_dsl`` round-trips the
    #: ``along`` form the author wrote. Legal for ``counter`` only.
    along: Direction | None = None
    #: The run's start/end along ``along``'s wall, room-local feet from the wall's
    #: south/west start corner. ``None``/``None`` means the full wall. Always given
    #: as a pair (both or neither); ``run_from <= run_to``.
    run_from: float | None = None
    run_to: float | None = None
    #: The run's depth into the room (ft). ``None`` uses the along-run default
    #: (:data:`barndsl.fixtures.ALONG_DEFAULT_DEPTH`, the US-standard 25 in).
    run_depth: float | None = None
    #: Source location of the `fixture` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


#: The luminaire kinds a ``light`` statement can name. ``ceiling`` is the default
#: surface-mounted fixture; ``pendant`` hangs, ``fan`` is a ceiling fan/light,
#: ``recessed`` is a can. They differ only in how they render — every one is a
#: lighting outlet for the IRC E3903 room-lighting check.
LIGHT_KINDS = ("ceiling", "pendant", "fan", "recessed")


@dataclass
class Outlet:
    """A receptacle (the ``outlet`` statement) on a room wall.

    ``wall`` is the room wall it sits on; ``offset`` is feet from the wall's
    **start corner** (its south or west end — the same convention as a door or
    window offset) to the receptacle. ``gfci`` marks a ground-fault receptacle
    (required at kitchens, baths, laundries and outdoors, IRC E3902). The level
    comes from the room, so an outlet carries none of its own. Receptacle spacing
    (IRC E3901.2) is checked per room once a room declares any outlet.
    """

    room: str
    wall: Direction
    offset: float = 1.0
    gfci: bool = False
    #: Source location of the `outlet` statement (textual DSL front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Switch:
    """A wall switch (the ``switch`` statement) on a room wall.

    Like :class:`Outlet` but it controls lighting rather than supplying power, so
    it never carries a GFCI flag and isn't counted for receptacle spacing. Its
    presence (with no ``light``) drives the ROOM_NO_LIGHT nudge. ``offset`` is
    feet from the wall's south/west start corner.
    """

    room: str
    wall: Direction
    offset: float = 1.0
    #: Source location of the `switch` statement (textual DSL front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Light:
    """A ceiling luminaire (the ``light`` statement) at a room-local point.

    ``x``/``y`` are **room-local** feet from the room's south-west corner (like a
    ``fixture`` position, unlike the wall-relative outlet/switch). ``kind`` is one
    of :data:`LIGHT_KINDS` (``ceiling`` default). A habitable room that has power
    (outlets/switches) but no light gets the ROOM_NO_LIGHT info (IRC E3903).
    """

    room: str
    x: float
    y: float
    kind: str = "ceiling"
    #: Source location of the `light` statement (textual DSL front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


#: The smoke/CO alarm kinds an ``alarm`` statement can name. ``smoke`` is a smoke
#: alarm (IRC R314), ``co`` a carbon-monoxide alarm (IRC R315), and ``smoke_co``
#: a combination unit that satisfies both. ``smoke_co`` is the sole spelling for
#: the combo unit — ``combo`` is not accepted.
ALARM_KINDS = ("smoke", "co", "smoke_co")


@dataclass
class Alarm:
    """A smoke/CO alarm (the ``alarm`` statement) — a room-level ceiling device.

    Alarms are placed at the room, not a wall point: ``alarm smoke in bed`` puts a
    smoke alarm in room ``bed``. ``kind`` is one of :data:`ALARM_KINDS`. Optional
    ``x``/``y`` are **room-local** feet from the room's SW corner (like a
    ``light``); omitted, the renderer centres the symbol in the room. The alarm
    drives the R314/R315 placement checks (ALARM_BEDROOM / ALARM_HALL / ALARM_LEVEL
    / ALARM_CO) once any alarm is declared.
    """

    room: str
    kind: str = "smoke"
    x: float | None = None
    y: float | None = None
    #: Source location of the `alarm` statement (textual DSL front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def is_smoke(self) -> bool:
        """True if this unit senses smoke (a plain smoke or a combo unit)."""
        return self.kind in ("smoke", "smoke_co")

    @property
    def is_co(self) -> bool:
        """True if this unit senses carbon monoxide (a CO or a combo unit)."""
        return self.kind in ("co", "smoke_co")


@dataclass
class Note:
    """A positioned annotation — the ``note "text" at <x>,<y> [level <n>]`` form.

    A leader-line callout drawn on the plan: ``text`` labels the point ``(x, y)``
    in **world/plan feet** (the same SW-origin frame as rooms — +x east, +y
    north), on floor ``level`` (0 = ground). Un-positioned ``note "text"``
    statements are *not* stored here — they keep flowing into
    :attr:`Barndominium.notes` (the free-text block); only a note carrying an
    ``at`` position becomes a :class:`Note`, drawn on the plan SVG with a leader.
    A note whose anchor lies outside the footprint is a gentle ``NOTE_OUTSIDE``
    info (architects annotate the site on purpose), never an error.
    """

    text: str
    x: float
    y: float
    level: int = 0
    #: Source location of the `note` statement (textual front-end only), so a
    #: surgical edit can find its line and a diagnostic can point at it.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Section:
    """One rectangular block of the building footprint.

    A plain barndo is a single section (the whole envelope); an L/T/U footprint
    is the union of two or more abutting sections. ``(x, y)`` is the south-west
    corner, like a room.
    """

    x: float
    y: float
    width: float
    length: float

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.length

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.width, self.length)


@dataclass
class Porch:
    """A covered or open exterior platform attached to the building.

    Porches may sit outside the envelope (e.g. a wrap-around front porch with a
    negative ``y``); they are excluded from interior-area code checks.
    """

    id: str
    x: float
    y: float
    width: float
    length: float
    covered: bool = True
    label: str | None = None

    @property
    def area(self) -> float:
        return self.width * self.length

    @property
    def display_name(self) -> str:
        return self.label or self.id.replace("_", " ").title()


@dataclass
class Stair:
    """Vertical circulation connecting two floor levels.

    Occupies a footprint on ``from_level`` (the run) with a matching opening on
    ``to_level`` (the landing). It links the rooms it overlaps on each level, so
    upper-level rooms become reachable from the floor below.
    """

    id: str
    x: float
    y: float
    width: float
    length: float
    from_level: int = 0
    to_level: int = 1
    label: str | None = None
    #: Source location of the `stair` statement (textual front-end only), so its
    #: diagnostics carry a column-accurate caret and can be `accept`-ed by line.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.length

    @property
    def area(self) -> float:
        return self.width * self.length

    @property
    def display_name(self) -> str:
        return self.label or self.id.replace("_", " ").title()

    def overlaps_rect(self, x: float, y: float, x2: float, y2: float, tol: float = 1e-6) -> bool:
        dx = min(self.x2, x2) - max(self.x, x)
        dy = min(self.y2, y2) - max(self.y, y)
        return dx > tol and dy > tol


#: The structural-member roles a placed frame produces. ``post`` is a perimeter
#: column on an exterior wall; ``interior`` is a column carrying a beam where the
#: clear span is too long for one piece; ``frame`` is a bent (the truss/beam
#: spanning the building between the eave walls); ``ridge`` is the ridge member
#: running the length of the building over the frames.
POST_ROLES = ("post", "interior")
BEAM_ROLES = ("frame", "ridge")


@dataclass
class Post:
    """A structural column (post) of the post-and-beam frame.

    ``(x, y)`` is the post centreline. ``size`` is the nominal square section in
    feet (a 6×6 is ``inches(6)``), used for drawing and the material takeoff.
    """

    x: float
    y: float
    size: float = inches(6)
    role: str = "post"
    level: int = 0


@dataclass
class Beam:
    """A horizontal structural member drawn as a centreline in plan.

    Runs from ``(x1, y1)`` to ``(x2, y2)`` (always axis-aligned). ``role`` is one
    of :data:`BEAM_ROLES`: a ``frame`` (bent) spans the building width between the
    eave walls; a ``ridge`` runs the length over the frames.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    role: str = "frame"
    level: int = 0

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def orientation(self) -> str:
        """``"v"`` if the beam runs north-south, ``"h"`` if east-west."""
        return "h" if abs(self.x2 - self.x1) >= abs(self.y2 - self.y1) else "v"


@dataclass
class FrameSpec:
    """Parameters for auto-placing the post-and-beam structural frame.

    Optional. When set (via the ``frame`` statement or :meth:`Barndominium.frame`)
    the engine in :mod:`barndsl.structure` derives a column-and-beam skeleton from
    the footprint: bents spaced no more than ``bay`` feet on centre along the long
    axis, each spanning the short axis, with interior support columns added when
    that span exceeds ``span`` feet, and a ``ridge`` member over the frames.

    All lengths are in feet. ``post`` is the nominal square post section. These are
    layout aids, **not** an engineered design — member sizing is the engineer's job.
    """

    bay: float = 12.0
    span: float = 40.0
    post: float = inches(6)
    ridge: bool = True
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class ProgramSpec:
    """A declared program (design *intent*): the expected room counts.

    Optional. When present, validation compares it against the rooms actually
    placed and warns on any mismatch (``PROGRAM_MISMATCH``) — the mechanical
    guard for "the code compiles clean but I dropped a bedroom". ``baths`` counts
    every bathroom *and* half-bath room, matching the compile recap; ``None``
    means that count wasn't declared and isn't checked.

    ``beds``/``baths`` are checked as **exact** counts. ``required`` maps a room
    type to a minimum count (an **at-least** check — a declared `1 laundry` warns
    only if none is placed; an extra never warns). ``min_area`` is the minimum
    conditioned interior floor area in square feet.
    """

    beds: int
    baths: int | None = None
    required: dict[RoomType, int] = field(default_factory=dict)
    min_area: float | None = None
    #: Minimum whole-house dedicated-storage area (closets + pantry), sq ft.
    min_storage: float | None = None
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


#: The requirement kinds a plan can declare (see :meth:`Barndominium.require`).
REQUIRE_KINDS = ("adjacent", "separate", "exterior", "area")

#: The attributes a `wall` statement can put on a shared wall (any combination):
#:
#: * ``plumbing`` — a 2x6 wet wall carrying supply/waste. Satisfies the
#:   WET_GROUP nudge for a wet room backing onto it, thickens the flanking
#:   rooms' clear-dimension math, and hints a thicker wall type in the exchange.
#: * ``bearing`` — an interior bearing wall. The auto frame honours it as an
#:   interior post line when it runs along the building (perpendicular to the
#:   bents' span); one parallel to the span can't split it and gets an info.
#: * ``rated`` — a fire-separation wall. Declared on a garage/shop–dwelling
#:   common wall it verifies (and silences) the GARAGE_SEPARATION reminder.
WALL_ATTRIBUTES = ("plumbing", "bearing", "rated")


@dataclass
class WallSpec:
    """Declared attributes of the shared wall between two rooms — one ``wall``
    statement (``wall <a> - <b> plumbing|bearing|rated``, one or more).

    Like :class:`Requirement`, this is declared *intent* checked mechanically:
    a spec naming an unknown room is a ``WALL_REF`` error, a pair that shares no
    wall is a ``WALL_NOADJ`` error (the wall doesn't exist), and a ``plumbing``
    wall no wet room backs onto is a ``WALL_UNUSED`` info. ``attributes`` is
    stored deduplicated in canonical :data:`WALL_ATTRIBUTES` order.
    """

    room_a: str
    room_b: str
    attributes: tuple[str, ...] = ()
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    def has(self, attribute: str) -> bool:
        return attribute in self.attributes


@dataclass
class Suite:
    """A named group of rooms that read as one unit — a bedroom with its
    ensuite bath and walk-in closet, say (the ``suite`` statement).

    Declared *intent*, like :class:`ProgramSpec` and :class:`WallSpec`: the
    membership isn't geometry, it's the author telling the checks which rooms
    belong together. A member naming an unknown room is a ``SUITE_REF`` error;
    a room declared in two suites is a ``SUITE_OVERLAP`` warning. When a suite
    covers the rooms a design-quality check reasons about, that check uses the
    declared membership instead of inferring it from types and adjacency.
    ``members`` are room ids, deduplicated in declaration order.
    """

    id: str
    members: tuple[str, ...] = ()
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Zone:
    """A named band of the plan — the private wing, the public core (the
    ``zone`` statement). Members are room ids **or** suite ids, so a zone can
    group whole suites.

    Declared intent like :class:`Suite`. An unknown member is a ``ZONE_REF``
    error; a room in two zones (directly or via a suite) is a ``ZONE_OVERLAP``
    warning; a clearly public room stranded in an otherwise-private zone (or the
    reverse) is a ``ZONE_CROSS`` info. ``members`` are stored deduplicated in
    declaration order.
    """

    id: str
    members: tuple[str, ...] = ()
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class SiteSpec:
    """Declared lot dimensions and yard setbacks — the ``site`` / ``setback``
    statements. Optional, like :class:`ProgramSpec`: barndominiums are usually
    acreage builds where the lot isn't the constraint, but when a ``site`` is
    declared the validator checks the building footprint fits inside the
    **buildable rectangle** (the lot minus its setbacks).

    ``width``/``length`` are the lot's east-west / north-south dimensions (feet;
    ``None`` until a ``site`` statement sets them). Setbacks are the required
    clear yard on each edge: ``front`` and ``rear`` consume the plan's
    north-south depth (front along the plan's south/entry side, rear along its
    north), and ``side`` applies to **both** the east and west edges (a single
    value, the two side yards being equal — there is no lot-position statement to
    tell them apart). Any subset may be declared; ``None`` means that edge has no
    setback (treated as 0 in the fit math). A ``setback`` with no ``site`` is a
    ``SETBACK_NO_SITE`` error; a footprint that overruns the buildable rectangle
    is a ``SETBACK`` error (a county/legal violation, checked by dimensions only).
    """

    width: float | None = None
    length: float | None = None
    front: float | None = None
    side: float | None = None
    rear: float | None = None
    #: Optional placement of the building's plan origin (world ``0,0``, the
    #: south-west envelope corner) on the lot — the ``building at <x>,<y>``
    #: statement, in lot feet from the lot's south-west corner. ``None`` leaves
    #: the building position unmodelled (the dimension-only setback fit still
    #: runs); when set, the setback check measures each side's real clearance and
    #: can name which side is encroached and by how much.
    building_x: float | None = None
    building_y: float | None = None
    #: Source location of the `site` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None
    #: Source location of the `setback` statement (textual front-end only).
    setback_line: int | None = None
    setback_col: int | None = None
    setback_end_col: int | None = None
    #: Source location of the `building` statement (textual front-end only).
    building_line: int | None = None
    building_col: int | None = None
    building_end_col: int | None = None

    @property
    def has_dims(self) -> bool:
        """True once a ``site <W> x <L>`` has set the lot dimensions."""
        return self.width is not None and self.length is not None

    @property
    def has_setback(self) -> bool:
        """True if any of front/side/rear was declared."""
        return any(v is not None for v in (self.front, self.side, self.rear))

    @property
    def has_building(self) -> bool:
        """True once a ``building at <x>,<y>`` has pinned the building on the lot."""
        return self.building_x is not None and self.building_y is not None


@dataclass
class Requirement:
    """A declared spatial requirement (design *intent*), one `require` statement.

    Optional, like :class:`ProgramSpec` — the same pattern extended from counts
    to space: when present, validation checks the compiled plan against it and
    warns (``REQUIRE_UNMET``) on any requirement the geometry doesn't satisfy.
    A requirement naming an unknown room id is an error (``REQUIRE_REF``), like
    any dangling reference. Requirements never block a compile.

    ``kind`` is one of :data:`REQUIRE_KINDS`; the other fields apply per kind:

    * ``adjacent`` — rooms ``a`` and ``b`` must share a wall (a positive-length
      shared edge on the same level). Purely geometric: a door or cased opening
      between non-abutting rooms doesn't satisfy it — adjacency is what a
      ``door`` *needs*, so the two checks agree.
    * ``separate`` — ``a`` and ``b`` must NOT share a wall. Rooms on different
      levels never share a wall, so a cross-level pair is trivially separate.
    * ``exterior`` — room ``a`` needs an exterior wall; ``wall`` optionally
      pins which side (north/south/east/west) must face outside.
    * ``area`` — room ``a``'s nominal area must be at least ``min_area`` sq ft
      (the same nominal figure ``program area`` uses).

    Multiple requirements are allowed; duplicates are harmless.
    """

    kind: str
    a: str
    b: str | None = None
    wall: Direction | None = None
    min_area: float | None = None
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class UseSpec:
    """A ``use "<relpath>" as <alias> at <x>,<y> [level <n>] [mirror x|y] [rotate 90|180|270]``
    statement (host side).

    Records the author's intent to stamp a part into the host plan: the quoted
    **relative** path to the part file, the required ``alias`` (every id inside the
    part is stamped ``<alias>.<id>``), the ``at`` corner (the stamped bounding
    box's SW corner in host feet), the target ``level`` (default 0) and the
    optional transform. Parsed at compile time and kept on
    :attr:`Barndominium.uses` so :func:`barndsl.emit.emit_dsl` can round-trip the
    ``use`` line **verbatim** (independently of whether it resolved). The loader
    turns each resolved ``use`` into an :class:`Instance`.

    :attr:`mirror` is ``"x"`` / ``"y"`` / ``None`` and :attr:`rotate` is one of
    ``0`` / ``90`` / ``180`` / ``270`` (a counter-clockwise turn). When both are
    given the part is **rotated first, then mirrored** in its own local frame —
    the composition order the stamper (:mod:`barndsl.compose`), emit and the edit
    engine all reproduce identically (Phase 7b).
    """

    relpath: str
    alias: str
    x: float
    y: float
    level: int = 0
    #: The instance transform (Phase 7b). ``mirror`` reflects the part about a
    #: local axis — ``"y"`` swaps east↔west (a vertical mirror line), ``"x"``
    #: swaps north↔south. ``rotate`` is a counter-clockwise turn in 90° steps.
    #: Composition order is rotate-then-mirror in local coords.
    mirror: str | None = None
    rotate: int = 0
    #: Source location of the `use` statement (textual front-end only), so a
    #: placement/instance diagnostic anchors to the `use` line and a surgical edit
    #: can find it.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Instance:
    """A stamped part instance in a composed host plan.

    Produced by the loader (:mod:`barndsl.compose`) from a resolved :class:`UseSpec`:
    the part is fragment-compiled once, then a transformed copy of every element is
    appended to the host plan with each id prefixed ``<alias>.``. This carries the
    bookkeeping the playground panel, the whole-instance drag and the ``inline_use``
    edit need — the alias, the part path, the placement, the stamped bounding box
    (host coords), the stamped room ids and every stamped element object.
    """

    alias: str
    relpath: str
    part_path: str  # resolved absolute (realpath) path
    x: float
    y: float
    level: int
    #: The instance transform baked into the stamped elements (Phase 7b) — carried
    #: so emit/edits can round-trip the `use` line and the panel can show it.
    mirror: str | None = None
    rotate: int = 0
    #: Bounding box ``(min_x, min_y, max_x, max_y)`` of the stamped rooms, host feet.
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    #: The stamped room ids (``<alias>.<id>``), in the part's declaration order.
    room_ids: list[str] = field(default_factory=list)
    #: Every stamped element object appended to the host (rooms, doors, windows,
    #: fixtures, devices, alarms, notes) — used by emit to skip them and by
    #: ``inline_use`` to flatten just this instance.
    objects: list = field(default_factory=list)
    #: Source location of the originating `use` statement.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


@dataclass
class Barndominium:
    """A complete barndominium floor plan.

    Construct directly, or fluently via the builder methods (each returns
    ``self`` so calls can be chained). See :mod:`barndsl.builder`.
    """

    name: str
    envelope_width: float = 0.0
    envelope_length: float = 0.0
    ceiling_height: float = feet(9)
    #: Depth (feet) of the inter-floor assembly between two stacked levels — the
    #: joists/subfloor/ceiling that a clear ceiling height ignores. Floor-to-floor
    #: is ``ceiling_height + floor_depth`` (see :attr:`floor_to_floor`), so an
    #: upper level stacks on the level below's *structure*, not its ceiling plane.
    floor_depth: float = FLOOR_ASSEMBLY_DEPTH
    #: Opt-in accessibility / aging-in-place target. When set, validation runs an
    #: extra set of ANSI A117.1-flavoured nudges (accessible door widths, a
    #: wheelchair turning space in the bath, single-floor living, a no-step entry).
    #: Off by default so ordinary plans aren't held to an accessible standard.
    accessible: bool = False
    #: Opt-in electrical / life-safety checklist. When set, validation emits a
    #: one-shot ``ELECTRICAL_PLAN`` reminder for the requirements the geometry
    #: can't place (receptacle spacing, switched lighting, stair light, exterior
    #: door landings). Off by default so ordinary plans aren't nagged.
    electrical: bool = False
    rooms: list[Room] = field(default_factory=list)
    interior_doors: list[InteriorDoor] = field(default_factory=list)
    exterior_doors: list[ExteriorDoor] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    porches: list[Porch] = field(default_factory=list)
    stairs: list[Stair] = field(default_factory=list)
    #: Author-placed fixtures & furnishings (the ``fixture`` statement). They add
    #: to a room's auto-seeds (an explicit fixture of a seeded kind replaces that
    #: seed). See :class:`PlacedFixture` and :func:`barndsl.fixtures.resolve_room_fixtures`.
    fixtures: list[PlacedFixture] = field(default_factory=list)
    #: Author-placed electrical devices (the ``outlet`` / ``switch`` / ``light``
    #: statements). Opt-in — a room that declares any outlet is checked for
    #: receptacle spacing (IRC E3901.2); a wet-room outlet is checked for GFCI
    #: (E3902); a habitable room with power but no light gets a lighting nudge
    #: (E3903). See :class:`Outlet`, :class:`Switch`, :class:`Light`.
    outlets: list[Outlet] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    lights: list[Light] = field(default_factory=list)
    #: Author-placed smoke/CO alarms (the ``alarm`` statement). Room-level ceiling
    #: devices. Declaring any alarm turns on the real R314/R315 placement checks
    #: (ALARM_BEDROOM / ALARM_HALL / ALARM_LEVEL / ALARM_CO). See :class:`Alarm`.
    alarms: list[Alarm] = field(default_factory=list)
    notes: str = ""
    #: Positioned annotations (``note "text" at <x>,<y> [level <n>]``): leader-line
    #: callouts drawn on the plan SVG. Un-positioned notes stay in :attr:`notes`
    #: (a plain string); only ``at``-positioned notes land here. See :class:`Note`.
    note_marks: list[Note] = field(default_factory=list)
    #: Extra footprint blocks beyond the primary ``envelope`` rectangle. Empty for
    #: a plain rectangular building; one entry per ``wing`` for an L/T/U footprint.
    #: The primary block (the envelope at the origin) is implicit — see
    #: :meth:`footprint_sections`.
    wings: list[Section] = field(default_factory=list)
    #: Optional declared program (intent). When set, validation checks the actual
    #: room counts against it. See :class:`ProgramSpec`.
    program_spec: ProgramSpec | None = None
    #: Optional declared spatial requirements (intent). Each one is checked
    #: against the compiled geometry. See :class:`Requirement`.
    requirements: list[Requirement] = field(default_factory=list)
    #: Declared shared-wall attributes (the ``wall`` statement): plumbing /
    #: bearing / rated walls between room pairs. See :class:`WallSpec`.
    wall_specs: list[WallSpec] = field(default_factory=list)
    #: Declared room groupings (the ``suite`` statement) — a bedroom + ensuite
    #: + closet read as one unit. Declaration order preserved. See :class:`Suite`.
    suites: list[Suite] = field(default_factory=list)
    #: Declared bands (the ``zone`` statement) — private wing, public core.
    #: Members are room or suite ids. Declaration order preserved. See :class:`Zone`.
    zones: list[Zone] = field(default_factory=list)
    #: Optional declared lot dimensions + yard setbacks (the ``site`` /
    #: ``setback`` statements). When set with dimensions and any setback, the
    #: validator checks the footprint fits the buildable rectangle. See
    #: :class:`SiteSpec`.
    site_spec: SiteSpec | None = None
    #: True-north orientation: the compass azimuth (degrees, clockwise from north)
    #: that the plan's ``+y`` (plan-north) axis points. ``0`` means plan-north is
    #: true north. ``None`` means **undeclared** — distinct from a declared ``0`` —
    #: so the solar checks only run when the author actually sited the plan (the
    #: same opt-in discipline as ``accessible``/``electrical``). Drives the
    #: solar-glazing nudges and sets Project North when lowered to Revit.
    orientation: float | None = None
    #: The plan-relative wall that faces the street / approach (the "front"). When
    #: set, the approach nudges check the entry and garage doors relate to it.
    street: "Direction | None" = None
    #: Exterior wall finish hint (e.g. metal siding). ``None`` leaves the consumer
    #: to choose; carried into the Revit exchange so a metal-shell wall type can be
    #: matched. A barndominium is typically metal or board-and-batten.
    siding: str | None = None
    #: Roof finish hint (e.g. standing-seam metal). ``None`` leaves it open.
    roofing: str | None = None
    #: Roof form over the building: ``"gable"`` (default, ridge down the long
    #: axis), ``"shed"`` (a single slope), or ``"monitor"`` (a raised centre aisle
    #: — the classic barn/​barndominium clerestory form). Set via the ``roof``
    #: directive or :meth:`roof`.
    roof_style: str = "gable"
    #: Optional roof pitch (rise:run) override; ``None`` uses the default pitch.
    roof_pitch: float | None = None
    #: Roof eave/rake overhang depth (ft) — the roof's projection past the walls.
    #: A barndominium's signature deep eave, and the primary passive-shading device
    #: for south glass. ``0`` (default) is a flush roof; a typical eave is 1–2 ft.
    #: Widens the roof in the elevations/section and the roof-area takeoff, and
    #: shades south glazing in the solar checks.
    overhang: float = 0.0
    #: Optional IECC climate zone (1 warmest … 8 coldest). When set, the compiler
    #: reports the prescriptive envelope R-value targets, the metal-frame
    #: thermal-bridge note, and a window-to-wall-ratio ceiling. ``None`` = undeclared.
    climate: int | None = None
    #: Optional structural-frame request. When set, :func:`barndsl.structure.place_frame`
    #: populates :attr:`posts` and :attr:`beams` from the footprint. See :class:`FrameSpec`.
    frame_spec: FrameSpec | None = None
    #: The placed structural skeleton (derived from ``frame_spec``). Regenerated by
    #: the placer; not authored directly (there is no `post`/`beam` statement).
    posts: list[Post] = field(default_factory=list)
    beams: list[Beam] = field(default_factory=list)
    #: Cross-file composition (the ``use`` statement). :attr:`uses` are the parsed
    #: ``use`` statements, kept verbatim so emit round-trips them (independent of
    #: resolution). The loader (:mod:`barndsl.compose`) stamps each resolved use
    #: into the plan and records an :class:`Instance`; :attr:`stamped_rooms` is the
    #: set of stamped room ids (read-only members — an edit on one is refused, see
    #: :mod:`barndsl.edits`). Empty for a plan with no ``use`` statements.
    uses: list[UseSpec] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    stamped_rooms: set[str] = field(default_factory=set)

    # -- fluent builder API ------------------------------------------------
    # Each method mutates the plan and returns ``self`` so calls chain. This
    # is the embedded DSL surface: type-checked, IDE-completable, no parser.

    def envelope(self, width: float, length: float) -> "Barndominium":
        """Set the primary steel-frame footprint block (feet), at the origin."""
        self.envelope_width = float(width)
        self.envelope_length = float(length)
        return self

    def wing(
        self, width: float, length: float, *, x: float, y: float
    ) -> "Barndominium":
        """Add a rectangular footprint block at ``(x, y)`` — an L/T/U extension.

        The building footprint becomes the union of the primary ``envelope`` block
        (at the origin) and every wing. ``envelope_width``/``envelope_length`` stay
        the *primary* block as declared; use :meth:`bounds` for the overall extent.
        """
        self.wings.append(Section(float(x), float(y), float(width), float(length)))
        return self

    def footprint_sections(self) -> list[tuple[float, float, float, float]]:
        """The footprint as ``(x, y, w, l)`` rectangles whose union is the building.

        The primary ``envelope`` block at the origin plus any ``wing`` blocks. For
        a plain rectangular plan this is a single rectangle, so every footprint
        query degrades to the ordinary envelope check.
        """
        secs = [(0.0, 0.0, self.envelope_width, self.envelope_length)]
        secs.extend(w.as_tuple() for w in self.wings)
        return secs

    def bounds(self) -> tuple[float, float, float, float]:
        """Bounding box ``(min_x, min_y, max_x, max_y)`` of the whole footprint."""
        secs = self.footprint_sections()
        return (
            min(s[0] for s in secs),
            min(s[1] for s in secs),
            max(s[0] + s[2] for s in secs),
            max(s[1] + s[3] for s in secs),
        )

    def ceiling(self, height: float) -> "Barndominium":
        self.ceiling_height = float(height)
        return self

    def floors(self, depth: float) -> "Barndominium":
        """Set the inter-floor assembly depth (feet) between stacked levels.

        This is the joist/subfloor/ceiling thickness a clear ceiling height
        leaves out; it makes :attr:`floor_to_floor` (and every upper-level
        elevation) reflect a real floor system instead of stacking levels
        directly on the ceiling plane below. Defaults to
        :data:`~barndsl.constants.FLOOR_ASSEMBLY_DEPTH`.
        """
        depth = float(depth)
        if depth < 0:
            raise ValueError("floor assembly depth must be non-negative.")
        self.floor_depth = depth
        return self

    @property
    def floor_to_floor(self) -> float:
        """Vertical distance between one finished floor and the next: the clear
        ceiling height plus the inter-floor assembly depth."""
        return self.ceiling_height + self.floor_depth

    def level_elevation(self, level: int) -> float:
        """Finished-floor elevation (feet) of ``level`` above grade — the sum of
        the floor-to-floor heights of every level beneath it."""
        return float(level) * self.floor_to_floor

    def roof(self, style: str = "gable", *, pitch: float | None = None) -> "Barndominium":
        """Set the roof form (``gable`` | ``shed`` | ``monitor``) and optional pitch.

        A ``gable`` runs the ridge down the long axis (the default); a ``shed`` is a
        single slope; a ``monitor`` raises a central clerestory aisle over the span
        — the classic barn/​barndominium roof. ``pitch`` is rise:run (e.g. ``0.333``
        for 4:12); omit it to use the default.
        """
        style = str(style).lower()
        if style not in ROOF_STYLES:
            raise ValueError(f"roof style must be one of {ROOF_STYLES}, got {style!r}.")
        self.roof_style = style
        if pitch is not None:
            pitch = float(pitch)
            if pitch <= 0:
                raise ValueError("roof pitch must be positive.")
            self.roof_pitch = pitch
        return self

    def set_overhang(self, depth: float) -> "Barndominium":
        """Set the roof eave/rake overhang depth (ft) — the roof's projection past
        the walls. ``0`` is flush; a typical barndominium eave is 1–2 ft."""
        d = _finite("plan", "overhang", depth)
        if d < 0:
            raise ValueError("overhang must be non-negative.")
        self.overhang = d
        return self

    def set_climate(self, zone: int) -> "Barndominium":
        """Declare the IECC climate zone (1–8) for the thermal-envelope guidance."""
        z = int(zone)
        if not 1 <= z <= 8:
            raise ValueError("climate zone must be an IECC zone 1–8.")
        self.climate = z
        return self

    def orient(self, degrees: float) -> "Barndominium":
        """Set the true-north azimuth (degrees) that plan-north (``+y``) points."""
        d = float(degrees)
        if not math.isfinite(d):
            raise ValueError("orientation must be a finite number of degrees.")
        self.orientation = d % 360.0
        return self

    def set_street(self, wall: "Direction | str") -> "Barndominium":
        """Declare which plan-relative wall faces the street / approach (the front)."""
        self.street = Direction(wall) if isinstance(wall, str) else wall
        return self

    def finish(
        self, *, siding: str | None = None, roof: str | None = None
    ) -> "Barndominium":
        """Set the exterior wall (``siding``) and/or ``roof`` finish hints."""
        if siding is not None:
            self.siding = str(siding)
        if roof is not None:
            self.roofing = str(roof)
        return self

    def note(
        self,
        text: str,
        *,
        x: float | None = None,
        y: float | None = None,
        level: int = 0,
    ) -> "Barndominium":
        """Add a design note.

        Plain ``note("verify well location")`` appends to the free-text
        :attr:`notes` block (the historical behaviour, unchanged). Passing a
        position — ``note("beam above", x=20, y=15, level=1)`` — instead records a
        :class:`Note`: a leader-line callout anchored at ``(x, y)`` (world/plan
        feet, SW origin) on floor ``level``, drawn on the plan SVG. Give **both**
        ``x`` and ``y`` to position a note (a lone one is an error).
        """
        if x is None and y is None:
            self.notes = (self.notes + "\n" + text).strip() if self.notes else text
            return self
        if x is None or y is None:
            raise ValueError("a positioned note needs both x and y.")
        lvl = int(level)
        if lvl < 0:
            raise ValueError("a note's level must be >= 0 (0 = ground).")
        self.note_marks.append(
            Note(str(text), _finite("note", "x", x), _finite("note", "y", y), lvl)
        )
        return self

    def mark_accessible(self, value: bool = True) -> "Barndominium":
        """Declare an accessibility / aging-in-place target for the plan.

        Turns on an extra set of advisory checks (accessible door widths, a
        wheelchair turning space in the bath, single-floor living, a no-step
        entry). Off by default, so a plan is only held to this standard when it
        opts in — via this method or the `accessible` DSL directive.
        """
        self.accessible = bool(value)
        return self

    def mark_electrical(self, value: bool = True) -> "Barndominium":
        """Opt in to the electrical / life-safety checklist reminder.

        The DSL models rooms and openings, not receptacles, luminaires, switches
        or exterior grade, so those code requirements can't be verified from the
        geometry. This flag turns on a one-shot ``ELECTRICAL_PLAN`` reminder that
        carries them onto the electrical/site plans. Off by default so an ordinary
        plan isn't nagged — opt in via this method or the `electrical` directive.
        """
        self.electrical = bool(value)
        return self

    def program(
        self,
        beds: int,
        baths: int | None = None,
        *,
        requires: dict[RoomType | str, int] | None = None,
        min_area: float | None = None,
        min_storage: float | None = None,
    ) -> "Barndominium":
        """Declare the intended program (bedroom / bathroom counts and more).

        Optional. When set, :func:`~barndsl.validation.validate` warns
        (``PROGRAM_MISMATCH``) if the rooms actually placed don't match — a
        mechanical check that you built what you set out to. ``baths`` counts
        every bathroom and half-bath; omit it to check only bedrooms.

        ``requires`` adds an **at-least** check per room type (e.g.
        ``requires={RoomType.LAUNDRY: 1}`` warns only if no laundry is placed),
        and ``min_area`` sets a minimum conditioned interior floor area (sq ft).
        """
        b = int(beds)
        ba = None if baths is None else int(baths)
        if b < 0 or (ba is not None and ba < 0):
            raise ValueError("program counts must be non-negative whole numbers.")
        req: dict[RoomType, int] = {}
        for t, n in (requires or {}).items():
            n = int(n)
            if n < 0:
                raise ValueError("program counts must be non-negative whole numbers.")
            if n > 0:
                req[RoomType(t)] = n
        ma = None if min_area is None else float(min_area)
        if ma is not None and ma < 0:
            raise ValueError("program area must be non-negative.")
        ms = None if min_storage is None else float(min_storage)
        if ms is not None and ms < 0:
            raise ValueError("program storage must be non-negative.")
        self.program_spec = ProgramSpec(b, ba, required=req, min_area=ma, min_storage=ms)
        return self

    def require(
        self,
        kind: str,
        a: str,
        b: str | None = None,
        *,
        wall: Direction | str | None = None,
        min_area: float | None = None,
    ) -> "Barndominium":
        """Declare a spatial requirement (the ``require`` statement).

        Like :meth:`program`, this records *intent* the validator checks
        mechanically against the compiled plan: an unmet requirement is a
        ``REQUIRE_UNMET`` warning (never blocking — the plan stays buildable),
        and a requirement naming an unknown room id is a ``REQUIRE_REF`` error.

        Forms — ``require("adjacent", a, b)`` (the rooms must share a wall);
        ``require("separate", a, b)`` (they must NOT share a wall; rooms on
        different levels are trivially separate); ``require("exterior", room,
        wall=...)`` (the room needs an exterior wall, optionally that specific
        side); ``require("area", room, min_area=sqft)`` (nominal area at least
        ``sqft`` — the same figure ``program area`` uses). See
        :class:`Requirement` for the exact semantics. Duplicates are harmless.
        """
        kind = str(kind).lower()
        if kind not in REQUIRE_KINDS:
            raise ValueError(
                f"require kind must be one of {REQUIRE_KINDS}, got {kind!r}."
            )
        if kind in ("adjacent", "separate"):
            if not b:
                raise ValueError(f"require {kind} names two rooms.")
            self.requirements.append(Requirement(kind, str(a), str(b)))
            return self
        if b is not None:
            raise ValueError(f"require {kind} names a single room.")
        if kind == "exterior":
            w = None if wall is None else Direction(wall)
            self.requirements.append(Requirement(kind, str(a), wall=w))
            return self
        # area
        if min_area is None:
            raise ValueError("require area needs min_area (sq ft).")
        ma = float(min_area)
        if ma < 0:
            raise ValueError("require area must be non-negative.")
        self.requirements.append(Requirement(kind, str(a), min_area=ma))
        return self

    def wall(self, room_a: str, room_b: str, *attributes: str) -> "Barndominium":
        """Declare attributes of the shared wall between two rooms (the ``wall``
        statement): ``plumbing`` (a 2x6 wet wall), ``bearing`` (an interior
        bearing wall the auto frame honours as a post line), and/or ``rated``
        (a fire-separation wall, verifying the garage-separation reminder).

        One or more attributes; duplicates are deduplicated and the set is
        stored in canonical :data:`WALL_ATTRIBUTES` order. Validation errors on
        an unknown room id (``WALL_REF``) or a pair that shares no wall
        (``WALL_NOADJ``) — see :class:`WallSpec`.
        """
        if room_a == room_b:
            raise ValueError("a wall statement names two different rooms.")
        if not attributes:
            raise ValueError(
                f"a wall needs at least one attribute: {WALL_ATTRIBUTES}."
            )
        attrs = []
        for a in attributes:
            a = str(a).lower()
            if a not in WALL_ATTRIBUTES:
                raise ValueError(
                    f"wall attribute must be one of {WALL_ATTRIBUTES}, got {a!r}."
                )
            attrs.append(a)
        canonical = tuple(a for a in WALL_ATTRIBUTES if a in attrs)
        self.wall_specs.append(WallSpec(str(room_a), str(room_b), canonical))
        return self

    def suite(self, suite_id: str, *rooms: str) -> "Barndominium":
        """Declare a suite (the ``suite`` statement): a named group of rooms
        that read as one unit — e.g. ``suite("primary", "master_bed",
        "master_bath", "master_wic")``.

        Like :meth:`program` / :meth:`wall`, this records intent the validator
        checks: a member naming an unknown room is a ``SUITE_REF`` error, and a
        room declared in two suites a ``SUITE_OVERLAP`` warning. Declaration
        order is preserved; duplicate members are dropped (keeping the first).
        Needs at least one member.
        """
        if not rooms:
            raise ValueError("a suite needs at least one member room.")
        members = tuple(dict.fromkeys(str(r) for r in rooms))
        self.suites.append(Suite(str(suite_id), members))
        return self

    def zone(self, zone_id: str, *members: str) -> "Barndominium":
        """Declare a zone (the ``zone`` statement): a named band of the plan —
        ``zone("private", "primary", "bed_2", "hall_beds")``. Members are room
        ids **or** suite ids, so a zone can group whole suites.

        Records intent like :meth:`suite`: an unknown member is a ``ZONE_REF``
        error, a room in two zones a ``ZONE_OVERLAP`` warning. Declaration order
        preserved; duplicate members dropped. Needs at least one member.
        """
        if not members:
            raise ValueError("a zone needs at least one member.")
        mems = tuple(dict.fromkeys(str(m) for m in members))
        self.zones.append(Zone(str(zone_id), mems))
        return self

    def site(self, width: float, length: float) -> "Barndominium":
        """Declare the lot (``site``) dimensions in feet — ``site <W> x <L>``.

        Optional. Gives the ``orientation`` azimuth something to anchor to and,
        with a ``setback``, lets the validator check the building footprint fits
        the buildable rectangle (see :meth:`setback` and :class:`SiteSpec`).
        """
        if self.site_spec is None:
            self.site_spec = SiteSpec()
        self.site_spec.width = float(width)
        self.site_spec.length = float(length)
        return self

    def setback(
        self,
        *,
        front: float | None = None,
        side: float | None = None,
        rear: float | None = None,
    ) -> "Barndominium":
        """Declare yard setbacks (feet) — ``setback front <n> side <n> rear <n>``.

        Any subset may be given. ``front``/``rear`` clear the plan's south/north
        (depth) edges; ``side`` clears **both** the east and west edges. A
        setback with no :meth:`site` is a ``SETBACK_NO_SITE`` error, since there
        are no lot dimensions to measure it against. See :class:`SiteSpec`.
        """
        if self.site_spec is None:
            self.site_spec = SiteSpec()
        if front is not None:
            self.site_spec.front = float(front)
        if side is not None:
            self.site_spec.side = float(side)
        if rear is not None:
            self.site_spec.rear = float(rear)
        return self

    def frame(
        self,
        *,
        bay: float = 12.0,
        span: float = 40.0,
        post: float = inches(6),
        ridge: bool = True,
    ) -> "Barndominium":
        """Auto-place the post-and-beam structural frame over the footprint.

        Bents are spaced no more than ``bay`` feet on centre along the building's
        long axis, each spanning the short axis; interior support columns are added
        wherever that span exceeds ``span`` feet, and (unless ``ridge=False``) a
        ridge member runs the length over the frames. Populates :attr:`posts` and
        :attr:`beams` immediately, so :func:`~barndsl.validation.validate` and the
        renderer see the structure. Lengths in feet; ``post`` is the square section.
        """
        bay = float(bay)
        span = float(span)
        post = float(post)
        if bay <= 0 or span <= 0 or post <= 0:
            raise ValueError("frame bay/span/post must be positive.")
        self.frame_spec = FrameSpec(bay, span, post, bool(ridge))
        from .structure import place_frame

        place_frame(self)
        return self

    def add_room(
        self,
        room_id: str,
        type: RoomType | str,
        *,
        x: float | None = None,
        y: float | None = None,
        width: float,
        length: float,
        label: str | None = None,
        level: int | float = 0,
        ceiling_height: float | None = None,
        vaulted: bool = False,
        floor: str | None = None,
        east_of: str | None = None,
        west_of: str | None = None,
        north_of: str | None = None,
        south_of: str | None = None,
        align: str = "near",
        offset: float = 0.0,
    ) -> "Barndominium":
        """Add a room.

        Position it either absolutely (``x=``, ``y=``) or **relatively** by
        abutting an already-defined room: ``east_of="kitchen"`` places this room
        flush against the kitchen's east wall (and so on). Relative placement
        shares a wall, so an interior ``connect`` between the two will resolve.

        With a relative anchor you can also slide along the shared wall:
        ``align`` picks where it sits — ``"near"`` (default, the reference's
        start corner), ``"far"`` (flush to the far corner), or ``"center"`` —
        and ``offset`` adds a further shift in feet (positive = north for
        east/west anchors, east for north/south anchors).
        """
        type = RoomType(type)  # coerce/validate strings -> raises on unknown
        width = _finite(room_id, "width", width)
        length = _finite(room_id, "length", length)
        offset = _finite(room_id, "offset", offset)
        if x is not None:
            x = _finite(room_id, "x", x)
        if y is not None:
            y = _finite(room_id, "y", y)
        align = align.lower() if isinstance(align, str) else align
        if isinstance(level, float) and not level.is_integer():
            raise ValueError(f"Room '{room_id}': level must be a whole number, got {level}.")
        level = int(level)
        if level < 0:
            raise ValueError(
                f"Room '{room_id}': level must be >= 0 (0 = ground), got {level}."
            )
        x, y = self._resolve_position(
            room_id, x, y, width, length, east_of, west_of, north_of, south_of,
            align, offset,
        )
        used = [
            f"{k} {v}"
            for k, v in (
                ("east_of", east_of),
                ("west_of", west_of),
                ("north_of", north_of),
                ("south_of", south_of),
            )
            if v is not None
        ]
        placement = " + ".join(used) if used else None
        ch = None if ceiling_height is None else _finite(room_id, "ceiling_height", ceiling_height)
        if ch is not None and ch <= 0:
            raise ValueError(f"Room '{room_id}': ceiling height must be positive.")
        self.rooms.append(
            Room(
                room_id, type, x, y, width, length, label, level,
                ceiling_height=ch, vaulted=bool(vaulted),
                floor=(str(floor) if floor is not None else None),
                placement=placement,
            )
        )
        return self

    @staticmethod
    def _align_along(ref_lo: float, ref_span: float, new_span: float, align: str) -> float:
        """Position the new room's near edge along a shared wall."""
        if align == "far":
            return ref_lo + ref_span - new_span
        if align == "center":
            return ref_lo + (ref_span - new_span) / 2.0
        return ref_lo  # near

    def _resolve_position(
        self,
        rid: str,
        x: float | None,
        y: float | None,
        width: float,
        length: float,
        east_of: str | None,
        west_of: str | None,
        north_of: str | None,
        south_of: str | None,
        align: str = "near",
        offset: float = 0.0,
    ) -> tuple[float, float]:
        h_used = [(k, v) for k, v in (("east_of", east_of), ("west_of", west_of)) if v is not None]
        v_used = [(k, v) for k, v in (("north_of", north_of), ("south_of", south_of)) if v is not None]
        if not (h_used or v_used):
            if align != "near" or offset:
                raise ValueError(
                    f"Room '{rid}': align/offset only apply with a relative anchor."
                )
            if x is None or y is None:
                raise ValueError(
                    f"Room '{rid}' needs a position: pass x= and y=, or a relative "
                    "anchor like east_of=."
                )
            return float(x), float(y)
        if len(h_used) > 1:
            raise ValueError(f"Room '{rid}': use at most one east-of/west-of anchor.")
        if len(v_used) > 1:
            raise ValueError(f"Room '{rid}': use at most one north-of/south-of anchor.")
        if x is not None or y is not None:
            raise ValueError(
                f"Room '{rid}': give either an absolute position or relative "
                "anchors, not both."
            )
        # With one anchor per axis the position is fully pinned, so the
        # slide-along-the-wall options have nowhere to apply.
        if h_used and v_used and (align != "near" or offset):
            raise ValueError(
                f"Room '{rid}': align/offset apply to a single anchor; with both a "
                "horizontal and a vertical anchor the position is already fixed."
            )
        if align not in ("near", "far", "center"):
            raise ValueError(
                f"Room '{rid}': align must be near, far, or center; got {align!r}."
            )

        def _ref(kind: str, ref_id: str) -> "Room":
            if ref_id == rid:
                raise ValueError(f"Room '{rid}' can't be placed relative to itself.")
            r = self.room(ref_id)
            if r is None:
                raise ValueError(
                    f"Cannot place '{rid}' {kind} unknown room '{ref_id}' — "
                    "define the reference room first."
                )
            return r

        nx = ny = None
        href = vref = None
        if h_used:
            kind, ref_id = h_used[0]
            href = _ref(kind, ref_id)
            nx = href.x2 if kind == "east_of" else href.x - width
        if v_used:
            kind, ref_id = v_used[0]
            vref = _ref(kind, ref_id)
            ny = vref.y2 if kind == "north_of" else vref.y - length
        # Fill the unconstrained axis (single-anchor case) via align/offset
        # along the anchor's shared wall — preserves prior single-anchor behaviour.
        if nx is None:
            # nx unset means no horizontal anchor, so a vertical one must exist.
            assert vref is not None
            nx = self._align_along(vref.x, vref.width, width, align) + offset
        if ny is None:
            # ny unset means no vertical anchor, so a horizontal one must exist.
            assert href is not None
            ny = self._align_along(href.y, href.length, length, align) + offset
        return nx, ny

    def add_porch(
        self,
        porch_id: str,
        *,
        x: float,
        y: float,
        width: float,
        length: float,
        covered: bool = True,
        label: str | None = None,
    ) -> "Barndominium":
        self.porches.append(
            Porch(porch_id, float(x), float(y), float(width), float(length), covered, label)
        )
        return self

    def add_stair(
        self,
        stair_id: str,
        *,
        x: float,
        y: float,
        width: float,
        length: float,
        from_level: int = 0,
        to_level: int = 1,
        label: str | None = None,
    ) -> "Barndominium":
        """Add a staircase connecting ``from_level`` to ``to_level``."""
        x = _finite(stair_id, "x", x)
        y = _finite(stair_id, "y", y)
        width = _finite(stair_id, "width", width)
        length = _finite(stair_id, "length", length)
        lo, hi = int(from_level), int(to_level)
        if lo < 0 or hi < 0:
            raise ValueError(f"Stair '{stair_id}': levels must be >= 0.")
        if lo == hi:
            raise ValueError(
                f"Stair '{stair_id}': from_level and to_level must differ "
                f"(got {lo})."
            )
        self.stairs.append(Stair(stair_id, x, y, width, length, lo, hi, label))
        return self

    def add_fixture(
        self,
        kind: str,
        room: str,
        *,
        x: float | None = None,
        y: float | None = None,
        wall: Direction | str | None = None,
        rotation: float = 0.0,
        width: float | None = None,
        along: Direction | str | None = None,
        run_from: float | None = None,
        run_to: float | None = None,
        depth: float | None = None,
    ) -> "Barndominium":
        """Place a fixture/furnishing (the ``fixture`` statement).

        ``kind`` must be a catalog kind (see :data:`barndsl.fixtures.FIXTURE_KINDS`).
        ``x``/``y`` are **room-local** feet from the room's SW corner; omit them to
        auto-place against ``wall`` (or the first free spot). ``rotation`` turns it
        in plan (degrees). Authored fixtures add to a room's auto-seeds; one of a
        seeded kind replaces that seed. See :class:`PlacedFixture`.

        ``along`` (``counter`` only) makes a wall-backed countertop run spanning a
        wall: give ``along`` a direction and, optionally, ``run_from``/``run_to``
        (room-local feet along the wall from its S/W corner — omit for the full
        wall) and ``depth`` (into the room; 1–4 ft, default the US-standard 25 in).
        It is mutually exclusive with ``at``/``wall``/``width``.
        """
        from .fixtures import ALONG_DEPTH_MAX, ALONG_DEPTH_MIN, FIXTURES

        kind = str(kind)
        if kind not in FIXTURES:
            raise ValueError(
                f"Unknown fixture kind '{kind}'. Known: {', '.join(FIXTURES)}."
            )
        wd = Direction(wall) if isinstance(wall, str) else wall
        ad = Direction(along) if isinstance(along, str) else along
        if x is not None:
            x = _finite(room, "fixture x", x)
        if y is not None:
            y = _finite(room, "fixture y", y)
        if (x is None) != (y is None):
            raise ValueError("A fixture `at` needs both an x and a y offset.")
        rot = _finite(room, "fixture rotation", rotation)
        w = None if width is None else _finite(room, "fixture width", width)
        if w is not None and w <= 0:
            raise ValueError("A fixture width must be positive.")
        rf = None if run_from is None else _finite(room, "counter run start", run_from)
        rt = None if run_to is None else _finite(room, "counter run end", run_to)
        rd = None if depth is None else _finite(room, "counter depth", depth)
        if ad is not None:
            if kind != "counter":
                raise ValueError(
                    f"`along` is only for a counter run, not a '{kind}' — every "
                    "other fixture has a fixed footprint. Use `at`/`wall` instead."
                )
            if x is not None or wd is not None or w is not None:
                raise ValueError(
                    "An `along` counter run can't also take `at`, `wall` or `width` "
                    "— the run's wall and length come from `along` and `from`/`to`."
                )
            if (rf is None) != (rt is None):
                raise ValueError("A counter run needs both `from` and `to`, or neither.")
            if rf is not None and rt is not None and rt <= rf:
                raise ValueError(
                    f"A counter run's `to` ({rt:g}) must be past its `from` ({rf:g})."
                )
            if rd is not None and not (ALONG_DEPTH_MIN - 1e-9 <= rd <= ALONG_DEPTH_MAX + 1e-9):
                raise ValueError(
                    f"A counter depth must be {ALONG_DEPTH_MIN:g}–{ALONG_DEPTH_MAX:g} ft, "
                    f"got {rd:g}."
                )
        elif rf is not None or rt is not None or rd is not None:
            raise ValueError("`from`/`to`/`depth` need an `along <wall>` counter run.")
        self.fixtures.append(
            PlacedFixture(kind, str(room), x, y, wd, rot, w, ad, rf, rt, rd)
        )
        return self

    def add_outlet(
        self,
        room: str,
        wall: Direction | str,
        *,
        offset: float = 1.0,
        gfci: bool = False,
    ) -> "Barndominium":
        """Place a receptacle (the ``outlet`` statement) on ``wall`` of ``room``.

        ``offset`` is feet from the wall's south/west start corner. ``gfci`` marks
        a ground-fault receptacle (kitchens, baths, laundries, outdoors — IRC
        E3902). Declaring any outlet opts the room into the receptacle-spacing
        check (IRC E3901.2). See :class:`Outlet`.
        """
        o = _finite(room, "outlet offset", offset)
        self.outlets.append(Outlet(str(room), Direction(wall), o, bool(gfci)))
        return self

    def add_switch(
        self, room: str, wall: Direction | str, *, offset: float = 1.0
    ) -> "Barndominium":
        """Place a wall switch (the ``switch`` statement) on ``wall`` of ``room``.

        ``offset`` is feet from the wall's south/west start corner. See
        :class:`Switch`.
        """
        o = _finite(room, "switch offset", offset)
        self.switches.append(Switch(str(room), Direction(wall), o))
        return self

    def add_light(
        self,
        room: str,
        *,
        x: float,
        y: float,
        kind: str = "ceiling",
    ) -> "Barndominium":
        """Place a ceiling luminaire (the ``light`` statement) at room-local
        ``x``,``y`` (feet from the room's SW corner). ``kind`` is one of
        :data:`LIGHT_KINDS` (``ceiling`` default). See :class:`Light`."""
        kind = str(kind).lower()
        if kind not in LIGHT_KINDS:
            raise ValueError(
                f"light kind must be one of {LIGHT_KINDS}, got {kind!r}."
            )
        lx = _finite(room, "light x", x)
        ly = _finite(room, "light y", y)
        self.lights.append(Light(str(room), lx, ly, kind))
        return self

    def add_alarm(
        self,
        room: str,
        kind: str = "smoke",
        *,
        x: float | None = None,
        y: float | None = None,
    ) -> "Barndominium":
        """Place a smoke/CO alarm (the ``alarm`` statement) in ``room``.

        ``kind`` is one of :data:`ALARM_KINDS` (``smoke``, ``co``, or the
        combination ``smoke_co``). ``x``/``y`` are optional **room-local** feet
        from the room's SW corner; omit them to centre the symbol in the room.
        See :class:`Alarm`."""
        kind = str(kind).lower()
        if kind not in ALARM_KINDS:
            raise ValueError(
                f"alarm kind must be one of {ALARM_KINDS}, got {kind!r}."
            )
        ax = None if x is None else _finite(room, "alarm x", x)
        ay = None if y is None else _finite(room, "alarm y", y)
        if (ax is None) != (ay is None):
            raise ValueError("An alarm `at` needs both an x and a y offset.")
        self.alarms.append(Alarm(str(room), kind, ax, ay))
        return self

    def building(self, x: float, y: float) -> "Barndominium":
        """Pin the building's plan origin on the lot — ``building at <x>,<y>``.

        ``x``/``y`` place the plan's south-west corner (world ``0,0``) in lot feet
        from the lot's south-west corner. Optional; needs a :meth:`site`. When set
        with ``setback``s the fit check measures each side's real clearance (and
        names an encroached side + its overrun) instead of the dimension-only
        bounding-box test. See :class:`SiteSpec`.
        """
        if self.site_spec is None:
            self.site_spec = SiteSpec()
        self.site_spec.building_x = _finite("building", "x", x)
        self.site_spec.building_y = _finite("building", "y", y)
        return self

    def connect(
        self,
        room_a: str,
        room_b: str,
        *,
        width: float = inches(32),
        leaf: bool = True,
        kind: str | None = None,
        offset: float | None = None,
        swing_into: str | None = None,
        hinge: str | None = None,
    ) -> "Barndominium":
        """Add an interior doorway between two adjacent rooms.

        ``kind`` is one of :data:`DOOR_KINDS` (``swing`` default, ``cased`` =
        walk-through, ``pocket``/``sliding``); the legacy ``leaf=False`` is a
        shorthand for ``kind="cased"``. ``offset`` (ft from the south/west end of
        the shared wall) positions the door along that wall; omit it to centre.
        ``swing_into`` names the room the leaf opens into (``room_a``/``room_b``)
        and ``hinge`` is ``"near"`` or ``"far"``.
        """
        resolved = kind if kind is not None else ("swing" if leaf else "cased")
        if resolved not in DOOR_KINDS:
            raise ValueError(f"door kind must be one of {DOOR_KINDS}, got {resolved!r}.")
        if hinge is not None and hinge not in ("near", "far"):
            raise ValueError(f"hinge must be 'near' or 'far', got {hinge!r}.")
        self.interior_doors.append(
            InteriorDoor(
                room_a,
                room_b,
                float(width),
                kind=resolved,
                offset=None if offset is None else float(offset),
                swing_into=swing_into,
                hinge=hinge,
            )
        )
        return self

    def opening(
        self,
        room_a: str,
        room_b: str,
        *,
        width: float = DEFAULT_OPENING_WIDTH,
        offset: float | None = None,
    ) -> "Barndominium":
        """Add an open cased passage (walk-through) between two adjacent rooms.

        Like :meth:`connect`, but with no door leaf — the open-concept link
        between e.g. a kitchen and a living area. Defaults to a wide opening.
        """
        return self.connect(room_a, room_b, width=width, leaf=False, offset=offset)

    def entrance(
        self,
        room: str,
        wall: Direction | str,
        *,
        width: float = feet(3),
        offset: float = 1.0,
        egress: bool = True,
        kind: str = "entry",
        height: float | None = None,
    ) -> "Barndominium":
        """Add an exterior door on ``wall`` of ``room``.

        ``kind="overhead"`` makes it a sectional garage door: ``egress`` is
        forced False (vehicle access, never an escape route) and ``height``
        defaults to the stock 7 ft panel. ``kind="double"``/``"french"`` is a
        pair of half-width leaves (egress clear width counts one leaf).
        """
        if kind not in EXTERIOR_DOOR_KINDS:
            raise ValueError(
                f"exterior door kind must be one of {EXTERIOR_DOOR_KINDS}, "
                f"got {kind!r}."
            )
        if kind == "overhead":
            egress = False
            if height is None:
                height = OVERHEAD_DOOR_HEIGHT
        self.exterior_doors.append(
            ExteriorDoor(
                room, Direction(wall), float(width), float(offset), egress,
                kind=kind, height=None if height is None else float(height),
            )
        )
        return self

    def add_window(
        self,
        room: str,
        wall: Direction | str,
        *,
        width: float = feet(4),
        offset: float = 2.0,
        sill_height: float = feet(3),
        head_height: float = feet(6.67),
        kind: str = "casement",
        tempered: bool = False,
    ) -> "Barndominium":
        """Add a window. ``kind`` is one of :data:`WINDOW_KINDS` (default
        ``casement`` — full glazed size = clear opening; a ``fixed`` window
        never counts as an escape opening). ``tempered`` declares safety
        glazing, the R308.4 escape hatch (silences WINDOW_TEMPERED)."""
        kind = str(kind).lower()
        if kind not in WINDOW_KINDS:
            raise ValueError(
                f"window kind must be one of {WINDOW_KINDS}, got {kind!r}."
            )
        self.windows.append(
            Window(
                room,
                Direction(wall),
                float(width),
                float(offset),
                float(sill_height),
                float(head_height),
                kind=kind,
                tempered=bool(tempered),
            )
        )
        return self

    # -- lookups -----------------------------------------------------------

    def room(self, room_id: str) -> Room | None:
        for r in self.rooms:
            if r.id == room_id:
                return r
        return None

    # -- metrics -----------------------------------------------------------

    @property
    def footprint_area(self) -> float:
        if not self.wings:
            return self.envelope_width * self.envelope_length
        from .geometry import footprint_area

        return footprint_area(self.footprint_sections())

    @property
    def interior_area(self) -> float:
        return sum(r.area for r in self.rooms if r.type in INTERIOR_TYPES)

    @property
    def habitable_area(self) -> float:
        return sum(r.area for r in self.rooms if r.type in HABITABLE_TYPES)

    @property
    def assigned_area(self) -> float:
        return sum(r.area for r in self.rooms)

    def levels(self) -> list[int]:
        """All floor levels present, ascending (always includes ground = 0)."""
        seen = {0}
        seen.update(r.level for r in self.rooms)
        for s in self.stairs:
            seen.update((s.from_level, s.to_level))
        return sorted(seen)

    def area_by_level(self) -> dict[int, float]:
        """Assigned room floor area per level."""
        areas: dict[int, float] = {lvl: 0.0 for lvl in self.levels()}
        for r in self.rooms:
            areas[r.level] = areas.get(r.level, 0.0) + r.area
        return areas

    def windows_for(self, room_id: str) -> list[Window]:
        return [w for w in self.windows if w.room == room_id]

    def exterior_doors_for(self, room_id: str) -> list[ExteriorDoor]:
        return [d for d in self.exterior_doors if d.room == room_id]

    def metrics(self) -> dict[str, float]:
        """Rough material / area takeoff for summaries and estimating."""
        from .geometry import footprint_boundary

        # The footprint outline doubles as the slab turndown edge, and for an
        # L/T/U plan (wings) its total length *is* the exterior perimeter — the
        # primary rectangle alone would understate an L/T/U takeoff. A plain
        # rectangle keeps the closed form 2(W+L).
        boundary = footprint_boundary(self.footprint_sections())
        turndown_len = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in boundary)
        if self.wings:
            perimeter = turndown_len
        else:
            perimeter = 2.0 * (self.envelope_width + self.envelope_length)
        exterior_wall_area = perimeter * self.ceiling_height
        # Gable roof over the footprint: the sloped area is the plan area divided
        # by the cosine of the roof slope (both planes share the pitch), so the
        # factor follows the actual pitch instead of a fixed guess.
        pitch = self.roof_pitch if self.roof_pitch is not None else DEFAULT_ROOF_PITCH
        slope_factor = math.hypot(1.0, pitch)  # sec(atan(pitch))
        # The roof covers the footprint plus a band of the eave/rake `overhang`
        # around its perimeter (exact for a rectangle: perimeter·oh + 4·oh²; a
        # close approximation for an L/T/U). Zero overhang leaves the plan area.
        oh = self.overhang
        roof_plan_area = self.footprint_area + perimeter * oh + 4.0 * oh * oh
        roof_area = roof_plan_area * slope_factor
        # Covered porches carry their own (shed) roof — a rough materials figure.
        covered_porch_roof = sum(p.area for p in self.porches if p.covered) * slope_factor
        # Monolithic slab-on-grade concrete: the slab, its thickened perimeter
        # edge (turndown), and a pad footing under each post. Rough takeoff (yd³).
        concrete_ft3 = (
            self.footprint_area * SLAB_THICKNESS
            + turndown_len * TURNDOWN_WIDTH * TURNDOWN_DEPTH
            + len(self.posts) * FOOTING_SIZE * FOOTING_SIZE * FOOTING_DEPTH
        )
        # Glazing split by true compass sector (local import avoids the
        # elements↔solar cycle). Uses orientation, or plan-north when unsited.
        from .solar import wall_sector

        theta = self.orientation or 0.0
        glaze = {"south": 0.0, "east": 0.0, "west": 0.0, "north": 0.0}
        for w in self.windows:
            glaze[wall_sector(w.wall, theta)] += w.glazed_area
        # Countertop takeoff: the run length (the wall-parallel dimension) and the
        # footprint area, summed over every resolved counter (authored + seeds). A
        # counter's run is its longer footprint side; L/U corner squares are counted
        # in BOTH meeting runs (not subtracted) — a small, deliberate over-count that
        # keeps the takeoff a simple sum and errs generous for estimating.
        from .fixtures import resolve_room_fixtures

        counter_lf = 0.0
        counter_area = 0.0
        for r in self.rooms:
            for f in resolve_room_fixtures(self, r):
                if f.kind == "counter":
                    counter_lf += max(f.width, f.length)
                    counter_area += f.width * f.length
        return {
            "footprint_sqft": self.footprint_area,
            "interior_sqft": self.interior_area,
            "habitable_sqft": self.habitable_area,
            "assigned_sqft": self.assigned_area,
            "unassigned_sqft": max(0.0, self.footprint_area - self.assigned_area),
            "exterior_perimeter_ft": perimeter,
            "exterior_wall_area_sqft": exterior_wall_area,
            "roof_area_sqft": roof_area,
            "overhang_ft": float(oh),
            "covered_porch_roof_sqft": covered_porch_roof,
            # Flat platform area of every porch (covered or open) — each carries a
            # slab whether or not it is roofed.
            "porch_sqft": sum(p.area for p in self.porches),
            "climate_zone": float(self.climate) if self.climate is not None else 0.0,
            "foundation_concrete_yd3": concrete_ft3 / 27.0,
            "bedroom_count": float(
                sum(1 for r in self.rooms if r.type is RoomType.BEDROOM)
            ),
            "bathroom_count": float(
                sum(
                    1
                    for r in self.rooms
                    if r.type in (RoomType.BATHROOM, RoomType.HALF_BATH)
                )
            ),
            # Structural takeoff (zero unless a `frame` has been placed).
            "post_count": float(len(self.posts)),
            "beam_count": float(len(self.beams)),
            "frame_count": float(sum(1 for b in self.beams if b.role == "frame")),
            "beam_linear_ft": sum(b.length for b in self.beams),
            # Solar takeoff: true-north azimuth and glazing by compass sector
            # (plan-relative when the plan declares no orientation).
            "true_north_azimuth": float(theta),
            "glazing_south_sqft": glaze["south"],
            "glazing_east_sqft": glaze["east"],
            "glazing_west_sqft": glaze["west"],
            "glazing_north_sqft": glaze["north"],
            # Countertop takeoff (linear feet of run and footprint area).
            "counter_linear_ft": counter_lf,
            "counter_area_sqft": counter_area,
        }
