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
    #: Source location of the `window` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None

    @property
    def glazed_area(self) -> float:
        return self.width * max(0.0, self.head_height - self.sill_height)

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
    #: Declared shared-wall attributes (the ``wall`` statement): plumbing /
    #: bearing / rated walls between room pairs. See :class:`WallSpec`.
    wall_specs: list[WallSpec] = field(default_factory=list)
    #: True-north orientation: the compass azimuth (degrees, clockwise from north)
    #: that the plan's ``+y`` (plan-north) axis points. ``0`` means plan-north is
    #: true north. Used for solar/setback reasoning and to set Project North when
    #: the plan is lowered to Revit.
    orientation: float = 0.0
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

    def orient(self, degrees: float) -> "Barndominium":
        """Set the true-north azimuth (degrees) that plan-north (``+y``) points."""
        d = float(degrees)
        if not math.isfinite(d):
            raise ValueError("orientation must be a finite number of degrees.")
        self.orientation = d % 360.0
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

    def program(
        self,
        beds: int,
        baths: int | None = None,
        *,
        requires: dict[RoomType | str, int] | None = None,
        min_area: float | None = None,
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
        self.program_spec = ProgramSpec(b, ba, required=req, min_area=ma)
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
    ) -> "Barndominium":
        """Add a window. ``kind`` is one of :data:`WINDOW_KINDS` (default
        ``casement`` — full glazed size = clear opening; a ``fixed`` window
        never counts as an escape opening)."""
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
        roof_area = self.footprint_area * slope_factor
        # Monolithic slab-on-grade concrete: the slab, its thickened perimeter
        # edge (turndown), and a pad footing under each post. Rough takeoff (yd³).
        concrete_ft3 = (
            self.footprint_area * SLAB_THICKNESS
            + turndown_len * TURNDOWN_WIDTH * TURNDOWN_DEPTH
            + len(self.posts) * FOOTING_SIZE * FOOTING_SIZE * FOOTING_DEPTH
        )
        return {
            "footprint_sqft": self.footprint_area,
            "interior_sqft": self.interior_area,
            "habitable_sqft": self.habitable_area,
            "assigned_sqft": self.assigned_area,
            "unassigned_sqft": max(0.0, self.footprint_area - self.assigned_area),
            "exterior_perimeter_ft": perimeter,
            "exterior_wall_area_sqft": exterior_wall_area,
            "roof_area_sqft": roof_area,
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
        }
