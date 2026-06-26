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


@dataclass
class Window:
    """A window on an exterior-facing wall of a room."""

    room: str
    wall: Direction
    width: float = feet(4)
    offset: float = 2.0
    sill_height: float = feet(3)
    head_height: float = feet(6.67)

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
        type: RoomType,
        *,
        x: float,
        y: float,
        width: float,
        length: float,
        label: str | None = None,
    ) -> "Barndominium":
        self.rooms.append(
            Room(room_id, type, float(x), float(y), float(width), float(length), label)
        )
        return self

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
        wall: Direction,
        *,
        width: float = feet(3),
        offset: float = 1.0,
        egress: bool = True,
    ) -> "Barndominium":
        """Add an exterior door on ``wall`` of ``room``."""
        self.exterior_doors.append(
            ExteriorDoor(room, wall, float(width), float(offset), egress)
        )
        return self

    def add_window(
        self,
        room: str,
        wall: Direction,
        *,
        width: float = feet(4),
        offset: float = 2.0,
        sill_height: float = feet(3),
        head_height: float = feet(6.67),
    ) -> "Barndominium":
        self.windows.append(
            Window(room, wall, float(width), float(offset), float(sill_height), float(head_height))
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
