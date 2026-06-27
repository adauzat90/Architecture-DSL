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

# --- Units ------------------------------------------------------------------

#: Internal unit is the foot. These helpers keep DSL authoring readable.
FOOT = 1.0


def feet(value: float) -> float:
    """Return a measurement expressed in feet (identity; for readability)."""
    return float(value)


def inches(value: float) -> float:
    """Convert inches to the internal unit (feet)."""
    return float(value) / 12.0


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


@dataclass
class InteriorDoor:
    """A doorway between two adjacent rooms."""

    room_a: str
    room_b: str
    width: float = inches(32)
    #: Source location of the statement that created this door (textual DSL
    #: front-end only); lets diagnostics point at the `door` line, not a room.
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


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
    #: Source location of the `entry` statement (textual front-end only).
    line: int | None = None
    col: int | None = None
    end_col: int | None = None


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
class Barndominium:
    """A complete barndominium floor plan.

    Construct directly, or fluently via the builder methods (each returns
    ``self`` so calls can be chained). See :mod:`barndsl.builder`.
    """

    name: str
    envelope_width: float = 0.0
    envelope_length: float = 0.0
    ceiling_height: float = feet(9)
    rooms: list[Room] = field(default_factory=list)
    interior_doors: list[InteriorDoor] = field(default_factory=list)
    exterior_doors: list[ExteriorDoor] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    porches: list[Porch] = field(default_factory=list)
    notes: str = ""

    # -- fluent builder API ------------------------------------------------
    # Each method mutates the plan and returns ``self`` so calls chain. This
    # is the embedded DSL surface: type-checked, IDE-completable, no parser.

    def envelope(self, width: float, length: float) -> "Barndominium":
        """Set the outer steel-frame footprint (feet)."""
        self.envelope_width = float(width)
        self.envelope_length = float(length)
        return self

    def ceiling(self, height: float) -> "Barndominium":
        self.ceiling_height = float(height)
        return self

    def note(self, text: str) -> "Barndominium":
        self.notes = (self.notes + "\n" + text).strip() if self.notes else text
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
        level: int = 0,
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
        self.rooms.append(
            Room(room_id, type, x, y, width, length, label, level, placement)
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
            nx = self._align_along(vref.x, vref.width, width, align) + offset
        if ny is None:
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

    def connect(
        self, room_a: str, room_b: str, *, width: float = inches(32)
    ) -> "Barndominium":
        """Add an interior doorway between two adjacent rooms."""
        self.interior_doors.append(InteriorDoor(room_a, room_b, float(width)))
        return self

    def entrance(
        self,
        room: str,
        wall: Direction | str,
        *,
        width: float = feet(3),
        offset: float = 1.0,
        egress: bool = True,
    ) -> "Barndominium":
        """Add an exterior door on ``wall`` of ``room``."""
        self.exterior_doors.append(
            ExteriorDoor(room, Direction(wall), float(width), float(offset), egress)
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
        return self.envelope_width * self.envelope_length

    @property
    def interior_area(self) -> float:
        return sum(r.area for r in self.rooms if r.type in INTERIOR_TYPES)

    @property
    def habitable_area(self) -> float:
        return sum(r.area for r in self.rooms if r.type in HABITABLE_TYPES)

    @property
    def assigned_area(self) -> float:
        return sum(r.area for r in self.rooms)

    def windows_for(self, room_id: str) -> list[Window]:
        return [w for w in self.windows if w.room == room_id]

    def exterior_doors_for(self, room_id: str) -> list[ExteriorDoor]:
        return [d for d in self.exterior_doors if d.room == room_id]

    def metrics(self) -> dict[str, float]:
        """Rough material / area takeoff for summaries and estimating."""
        perimeter = 2.0 * (self.envelope_width + self.envelope_length)
        exterior_wall_area = perimeter * self.ceiling_height
        # Gable roof over a rectangular footprint; ~1.15 factor for a modest pitch.
        roof_area = self.footprint_area * 1.15
        return {
            "footprint_sqft": self.footprint_area,
            "interior_sqft": self.interior_area,
            "habitable_sqft": self.habitable_area,
            "assigned_sqft": self.assigned_area,
            "unassigned_sqft": max(0.0, self.footprint_area - self.assigned_area),
            "exterior_perimeter_ft": perimeter,
            "exterior_wall_area_sqft": exterior_wall_area,
            "roof_area_sqft": roof_area,
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
        }
