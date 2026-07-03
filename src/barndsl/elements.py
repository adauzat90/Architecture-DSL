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
#: are sliding leaves (no swing arc). All four join the two rooms in the
#: circulation graph; they differ in how they render and which checks apply.
DOOR_KINDS = ("swing", "cased", "pocket", "sliding")

#: The exterior-door kinds. ``entry`` is a hinged people-door (the default);
#: ``overhead`` is a sectional/overhead garage door — vehicle access on a
#: garage/shop bay. An overhead door has no swing, is never an egress door, and
#: doesn't count as a building entrance (the plan still needs an ``entry``).
EXTERIOR_DOOR_KINDS = ("entry", "overhead")

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


@dataclass
class Window:
    """A window on an exterior-facing wall of a room."""

    room: str
    wall: Direction
    width: float = feet(4)
    offset: float = 2.0
    sill_height: float = feet(3)
    head_height: float = feet(6.67)
    #: Source location of the `window` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def glazed_area(self) -> float:
        return self.width * max(0.0, self.head_height - self.sill_height)


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
class Lot:
    """The parcel the building sits on — a rectangle in plan coordinates.

    ``(x, y)`` is the south-west corner, in the *same* plan frame as the building
    (typically negative, so the building at the origin sits inside the lot).
    ``x``/``y`` may be ``None`` — "auto-centre the footprint in the lot" — resolved
    lazily against the final footprint by :meth:`Barndominium.lot_box` so it doesn't
    depend on whether wings/rooms were declared before or after the ``lot``.
    Setbacks are declared separately and inset this to the buildable envelope.
    """

    width: float
    length: float
    x: float | None = None
    y: float | None = None


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
    notes: str = ""
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
    #: True-north orientation: the compass azimuth (degrees, clockwise from north)
    #: that the plan's ``+y`` (plan-north) axis points. ``0`` means plan-north is
    #: true north. ``None`` means **undeclared** — distinct from a declared ``0`` —
    #: so the solar checks only run when the author actually sited the plan (the
    #: same opt-in discipline as ``accessible``/``electrical``). Drives the
    #: solar-glazing nudges and sets Project North when lowered to Revit.
    orientation: float | None = None
    #: Optional parcel the building sits on. When set, the footprint is checked
    #: against the buildable envelope (lot inset by :attr:`setbacks`) — SETBACK.
    lot: "Lot | None" = None
    #: Zoning setbacks by **plan-relative** side (keys ``south``/``north``/``east``/
    #: ``west``; ``front``/``back``/``left``/``right`` are input aliases). Feet from
    #: the matching lot edge. Only meaningful with a :attr:`lot`.
    setbacks: dict[str, float] = field(default_factory=dict)
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

    def set_lot(
        self,
        width: float,
        length: float,
        *,
        x: float | None = None,
        y: float | None = None,
    ) -> "Barndominium":
        """Declare the parcel (feet). ``x``/``y`` place its SW corner in the plan
        frame; omit them to auto-centre the footprint in the lot."""
        w = _finite("lot", "width", width)
        length_ = _finite("lot", "length", length)
        if w <= 0 or length_ <= 0:
            raise ValueError("lot width and length must be positive.")
        self.lot = Lot(
            w,
            length_,
            None if x is None else _finite("lot", "x", x),
            None if y is None else _finite("lot", "y", y),
        )
        return self

    def set_street(self, wall: "Direction | str") -> "Barndominium":
        """Declare which plan-relative wall faces the street / approach (the front)."""
        self.street = Direction(wall) if isinstance(wall, str) else wall
        return self

    #: Input aliases → the plan-relative cardinal a setback side maps to.
    _SETBACK_ALIASES = {
        "front": "south", "back": "north", "rear": "north",
        "left": "west", "right": "east",
    }

    def setback(
        self,
        *,
        south: float | None = None,
        north: float | None = None,
        east: float | None = None,
        west: float | None = None,
        front: float | None = None,
        back: float | None = None,
        rear: float | None = None,
        left: float | None = None,
        right: float | None = None,
    ) -> "Barndominium":
        """Set zoning setbacks (feet) by plan-relative side. ``front``/``back``/
        ``rear``/``left``/``right`` are accepted and normalised to the cardinal
        (``front``=south, ``back``/``rear``=north, ``left``=west, ``right``=east)."""
        given = {
            "south": south, "north": north, "east": east, "west": west,
            "front": front, "back": back, "rear": rear, "left": left, "right": right,
        }
        for side, dist in given.items():
            if dist is None:
                continue
            key = self._SETBACK_ALIASES.get(side, side)
            d = _finite("setback", side, dist)
            if d < 0:
                raise ValueError(f"setback {side} must be non-negative.")
            self.setbacks[key] = d
        return self

    def lot_box(self) -> tuple[float, float, float, float] | None:
        """The lot as concrete ``(x0, y0, x1, y1)`` in plan coordinates, resolving
        an auto-centred lot against the final footprint. ``None`` if no lot."""
        if self.lot is None:
            return None
        fx0, fy0, fx1, fy1 = self.bounds()
        w, length_ = self.lot.width, self.lot.length
        x0 = self.lot.x if self.lot.x is not None else (fx0 + fx1) / 2.0 - w / 2.0
        y0 = self.lot.y if self.lot.y is not None else (fy0 + fy1) / 2.0 - length_ / 2.0
        return (x0, y0, x0 + w, y0 + length_)

    def buildable_envelope(self) -> tuple[float, float, float, float] | None:
        """The lot inset by each side's setback — the box the footprint must fit
        inside. ``None`` if no lot."""
        box = self.lot_box()
        if box is None:
            return None
        x0, y0, x1, y1 = box
        s = self.setbacks
        return (
            x0 + s.get("west", 0.0),
            y0 + s.get("south", 0.0),
            x1 - s.get("east", 0.0),
            y1 - s.get("north", 0.0),
        )

    def finish(
        self, *, siding: str | None = None, roof: str | None = None
    ) -> "Barndominium":
        """Set the exterior wall (``siding``) and/or ``roof`` finish hints."""
        if siding is not None:
            self.siding = str(siding)
        if roof is not None:
            self.roofing = str(roof)
        return self

    def note(self, text: str) -> "Barndominium":
        self.notes = (self.notes + "\n" + text).strip() if self.notes else text
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
                ceiling_height=ch, vaulted=bool(vaulted), placement=placement,
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
        defaults to the stock 7 ft panel.
        """
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
    ) -> "Barndominium":
        self.windows.append(
            Window(
                room,
                Direction(wall),
                float(width),
                float(offset),
                float(sill_height),
                float(head_height),
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
        }
