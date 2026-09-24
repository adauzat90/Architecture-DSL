"""Auto-layout: solve room placement from an adjacency brief.

You give a *brief* — a list of rooms (id, type, size) and which rooms should be
**adjacent** — and the solver computes concrete ``(x, y)`` positions, an
envelope, interior doors for the adjacencies that ended up sharing a wall, and
(optionally) an entry plus egress/daylight windows. The result is a
:class:`~barndsl.elements.Barndominium` that you can validate, render, or
``emit_dsl`` straight to ``.barn`` source.

It's the automation of the patterns the language already rewards: relative and
pocket placement produce shared walls, and a shared wall is exactly what a
``door`` needs. The solver is a deterministic greedy placer — same brief in,
same plan out — so its output is reproducible and explainable rather than the
black box of a constraint solver.

    from barndsl.layout import RoomSpec, LayoutBrief, solve_layout

    brief = LayoutBrief(
        name="Greenfield",
        rooms=[
            RoomSpec("living", "living", 24, 26),
            RoomSpec("kitchen", "kitchen", 16, 26),
            RoomSpec("bed1", "bedroom", 12, 12),
            RoomSpec("bath", "bathroom", 8, 10),
        ],
        adjacencies=[("living", "kitchen"), ("living", "bed1"), ("bed1", "bath")],
    )
    out = solve_layout(brief)
    print(out.plan and emit_dsl(out.plan))
    print(out.unsatisfied)   # adjacencies the packing couldn't honour
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .elements import (
    Barndominium,
    Direction,
    HABITABLE_TYPES,
    PUBLIC_TYPES,
    Room,
    RoomType,
    feet,
    inches,
)
from .compiler import comment_start
from .constants import NATURAL_LIGHT_RATIO as _NAT_LIGHT_RATIO
from .geometry import exterior_walls, shared_edge

# Glazed area a 1-ft-wide window contributes (head 6.67 - sill 3.0 ft tall),
# used to size daylight windows; mirrors Window's defaults in elements.py.
_GLASS_PER_FT = feet(6.67) - feet(3.0)

#: The rooms that form the open public core. Two of them sharing a wall get a
#: cased passage rather than a door; :mod:`barndsl.layout2` bands them together.
PUBLIC_CORE_TYPES = PUBLIC_TYPES
#: How wide the cased passage between two public-core rooms is.
PUBLIC_OPENING_WIDTH = feet(6)


# --- the brief --------------------------------------------------------------


@dataclass
class RoomSpec:
    """One room in a layout brief: what it is and how big, not yet where."""

    id: str
    type: RoomType | str
    width: float
    length: float
    label: str | None = None
    level: int = 0

    def __post_init__(self) -> None:
        self.type = RoomType(self.type)  # validate eagerly, like the builder
        self.width = float(self.width)
        self.length = float(self.length)
        self.level = int(self.level)


@dataclass
class LayoutBrief:
    """A program to lay out: rooms + desired adjacencies + a few preferences."""

    name: str
    rooms: list[RoomSpec]
    #: Pairs ``(a, b)`` of room ids that should share a wall (a door between them).
    adjacencies: list[tuple[str, str]] = field(default_factory=list)
    #: Fixed envelope, or ``None`` to size it to the packed bounding box.
    envelope: tuple[float, float] | None = None
    ceiling: float = feet(9)
    notes: str = ""
    #: Room to give the front door (default: the first public room).
    entry_room: str | None = None
    #: Auto-add an entry + egress/daylight windows so the plan compiles clean.
    add_openings: bool = True


@dataclass
class LayoutResult:
    """What the solver produced, and what it couldn't honour."""

    plan: Barndominium
    satisfied: list[tuple[str, str]] = field(default_factory=list)
    unsatisfied: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        sat, total = len(self.satisfied), len(self.satisfied) + len(self.unsatisfied)
        return f"Laid out {len(self.plan.rooms)} room(s); {sat}/{total} adjacencies satisfied"


# --- the solver -------------------------------------------------------------


def solve_layout(brief: LayoutBrief) -> LayoutResult:
    """Place every room in ``brief`` and return a plan plus a satisfaction report.

    .. deprecated::
        This is the v1 "greedy" abutment placer. New code should prefer
        :func:`barndsl.layout2.solve_layout2` (the CLI default, reached via the
        ``fill`` engine), which keeps habitable rooms on the perimeter and wastes
        no footprint. v1 is retained for the ``--engine greedy`` fallback.
    """
    specs = list(brief.rooms)
    if not specs:
        raise ValueError("Layout brief has no rooms.")
    ids = [s.id for s in specs]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"Duplicate room id(s) in brief: {', '.join(dupes)}.")
    by_id = {s.id: s for s in specs}
    orig_index = {s.id: i for i, s in enumerate(specs)}

    # Symmetric adjacency map, validated against the room set.
    adj: dict[str, set[str]] = {s.id: set() for s in specs}
    for a, b in brief.adjacencies:
        if a not in by_id or b not in by_id:
            missing = a if a not in by_id else b
            raise ValueError(f"Adjacency names unknown room '{missing}'.")
        if a != b:
            adj[a].add(b)
            adj[b].add(a)

    order = _placement_order(specs, adj, orig_index)
    placed: dict[str, Room] = {}
    notes: list[str] = []

    for rid in order:
        spec = by_id[rid]
        same_level = [r for r in placed.values() if r.level == spec.level]
        anchors = [n for n in adj[rid] if n in placed and placed[n].level == spec.level]
        room = _place_one(spec, anchors, placed, same_level, orig_index)
        placed[rid] = room

    # Normalise so the bounding box starts at the origin.
    min_x = min(r.x for r in placed.values())
    min_y = min(r.y for r in placed.values())
    for r in placed.values():
        r.x -= min_x
        r.y -= min_y

    bbox_w = max(r.x2 for r in placed.values())
    bbox_l = max(r.y2 for r in placed.values())
    if brief.envelope is not None:
        env_w, env_l = float(brief.envelope[0]), float(brief.envelope[1])
        if bbox_w - env_w > 1e-6 or bbox_l - env_l > 1e-6:
            notes.append(
                f"Packed footprint {bbox_w:g}×{bbox_l:g} exceeds the requested "
                f"envelope {env_w:g}×{env_l:g}; rooms will fall out of bounds."
            )
    else:
        env_w, env_l = bbox_w, bbox_l

    plan = Barndominium(brief.name, env_w, env_l, brief.ceiling)
    if brief.notes:
        plan.note(brief.notes)
    # Emit rooms in the brief's original order for a stable, readable plan.
    for spec in specs:
        plan.rooms.append(placed[spec.id])

    satisfied, unsatisfied = connect_adjacencies(plan, brief, by_id)
    bypassed = relieve_kitchen_passthrough(plan)
    if bypassed:
        notes.append(
            f"Added {bypassed} door(s) so traffic can bypass the kitchen work zone."
        )
    if brief.add_openings:
        place_openings(plan, brief, by_id, notes)

    return LayoutResult(plan, satisfied, unsatisfied, notes)


def _placement_order(
    specs: list[RoomSpec], adj: dict[str, set[str]], orig_index: dict[str, int]
) -> list[str]:
    """Greedy order: seed with the largest room, then always place the room
    with the most already-placed neighbours (so it can abut them), breaking ties
    by area then brief order. Keeps connected rooms together and is deterministic.
    """
    remaining = {s.id for s in specs}
    placed: set[str] = set()
    order: list[str] = []
    area = {s.id: s.width * s.length for s in specs}
    while remaining:
        best = max(
            remaining,
            key=lambda sid: (
                len(adj[sid] & placed),
                area[sid],
                -orig_index[sid],
            ),
        )
        order.append(best)
        placed.add(best)
        remaining.discard(best)
    return order


def _place_one(
    spec: RoomSpec,
    anchors: list[str],
    placed: dict[str, Room],
    same_level: list[Room],
    orig_index: dict[str, int],
) -> Room:
    """Choose the best non-overlapping position for ``spec``.

    Tries abutting each placed neighbour on each side (and pocketing into the
    corner between two), scoring by adjacencies actually touched, then
    compactness. Falls back to a free strip east of everything placed.
    """
    w, l = spec.width, spec.length

    def make(x: float, y: float) -> Room:
        return Room(spec.id, RoomType(spec.type), x, y, w, l, spec.label, spec.level)

    if not anchors:
        # No placed neighbour to abut. Seed at the origin, or start a new strip
        # to the east of everything so far (kept apart, no spurious adjacency).
        if not same_level:
            return make(0.0, 0.0)
        x = max(r.x2 for r in same_level) + 0.0
        return make(x, 0.0)

    anchor_rooms = [placed[a] for a in anchors]
    candidates: list[tuple[float, float]] = []
    for a in anchor_rooms:
        for x, y in _abut_candidates(a, w, l):
            candidates.append((x, y))
    # Pocket: x from one neighbour, y from another (corner between two rooms).
    for a in anchor_rooms:
        for b in anchor_rooms:
            if a is b:
                continue
            for x in (a.x2, a.x - w):
                for y in (b.y2, b.y - l):
                    candidates.append((x, y))

    best: tuple | None = None
    for x, y in candidates:
        cand = make(x, y)
        if any(cand.overlaps(r) > 0 for r in same_level):
            continue
        touch = sum(1 for a in anchor_rooms if shared_edge(cand, a) is not None)
        if not touch:
            continue
        # Compactness: bounding box of everything once this room lands.
        xs = [r.x for r in same_level] + [cand.x]
        ys = [r.y for r in same_level] + [cand.y]
        xe = [r.x2 for r in same_level] + [cand.x2]
        ye = [r.y2 for r in same_level] + [cand.y2]
        compact = (max(xe) - min(xs)) * (max(ye) - min(ys))
        key = (-touch, compact, y, x)
        if best is None or key < best[0]:
            best = (key, x, y)

    if best is not None:
        return make(best[1], best[2])
    # Every abutting spot overlapped something; drop it east, out of the way.
    x = max(r.x2 for r in same_level) + 0.0 if same_level else 0.0
    return make(x, 0.0)


def _abut_candidates(a: Room, w: float, l: float) -> list[tuple[float, float]]:
    """Positions for a ``w×l`` room flush against each wall of ``a`` (near/far/center)."""
    out: list[tuple[float, float]] = []
    # East / West: x fixed, slide in y (near=south, far=north, center).
    for x in (a.x2, a.x - w):
        for y in (a.y, a.y2 - l, a.y + (a.length - l) / 2.0):
            out.append((x, y))
    # North / South: y fixed, slide in x.
    for y in (a.y2, a.y - l):
        for x in (a.x, a.x2 - w, a.x + (a.width - w) / 2.0):
            out.append((x, y))
    return out


def connect_adjacencies(
    plan: Barndominium, brief: LayoutBrief, by_id: dict[str, RoomSpec]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Add a door for every requested adjacency whose rooms share a wall."""
    satisfied: list[tuple[str, str]] = []
    unsatisfied: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for a, b in brief.adjacencies:
        if a == b:
            continue
        key = frozenset((a, b))
        if key in seen:
            continue
        seen.add(key)
        ra, rb = plan.room(a), plan.room(b)
        if ra is None or rb is None:
            unsatisfied.append((a, b))
            continue
        if shared_edge(ra, rb) is not None:
            both_public = (
                by_id[a].type in PUBLIC_CORE_TYPES
                and by_id[b].type in PUBLIC_CORE_TYPES
            )
            if both_public:
                # The open core is a *cased* passage, not a 6 ft swing leaf — a
                # door that wide would need two leaves to be buildable
                # (DOOR_WIDE_SWING), and open-plan flow wants no door at all.
                plan.opening(a, b, width=PUBLIC_OPENING_WIDTH)
            else:
                plan.connect(a, b, width=inches(32))
            satisfied.append((a, b))
        else:
            unsatisfied.append((a, b))
    return satisfied, unsatisfied


def relieve_kitchen_passthrough(plan: Barndominium, min_wall: float = inches(32)) -> int:
    """Give traffic a way past the kitchen, and return how many doors that took.

    A kitchen that is the *only* route between the dining room and the living /
    great / rec room turns the work triangle into a corridor (KITCHEN_PASSTHROUGH).
    The packing often produces exactly that: rooms tile the public band
    ``living | kitchen | dining``, so dining's only requested adjacency is to the
    kitchen. The fix is the one the diagnostic asks for — route circulation
    *around* the kitchen — by adding one door on the widest shared wall that
    bridges the two sides without passing through it (in practice the hall behind
    the band). Each door merges the two components it bridges, so the loop ends
    once no kitchen severs a public pair — or once no wall is wide enough, which
    is the genuine "move the kitchen" case a door can't fix.
    """
    added = 0
    while True:
        bridge = _kitchen_bypass_wall(plan, min_wall)
        if bridge is None:
            return added
        plan.connect(bridge[0], bridge[1], width=min_wall)
        added += 1


#: The rooms KITCHEN_PASSTHROUGH treats as the living end of the traffic route.
_LIVING_LIKE = (RoomType.LIVING, RoomType.GREAT_ROOM, RoomType.REC_ROOM)


def _kitchen_severances(plan: Barndominium):
    """Yield ``(comps, dining_comp, living_comp)`` per kitchen-severed public pair.

    Mirrors KITCHEN_PASSTHROUGH exactly: a kitchen with both a dining and a
    living/great/rec door-neighbour, where dropping the kitchen from the door
    graph leaves those two in different components. ``comps`` is the component
    list the two indices point into.
    """
    from .validation import components_excluding, door_graph

    by_id = {r.id: r for r in plan.rooms}
    graph = door_graph(plan)
    for kitchen in plan.rooms:
        if kitchen.type is not RoomType.KITCHEN:
            continue
        comps = components_excluding(graph, {kitchen.id})
        comp_of = {rid: i for i, comp in enumerate(comps) for rid in comp}
        neighbours = [n for n in graph.get(kitchen.id, ()) if n in comp_of]
        dining = {n for n in neighbours if by_id[n].type is RoomType.DINING}
        living = {n for n in neighbours if by_id[n].type in _LIVING_LIKE}
        severed = sorted(
            {
                (comp_of[d], comp_of[lv])
                for d in dining
                for lv in living
                if comp_of[d] != comp_of[lv]
            }
        )
        for d_comp, l_comp in severed:
            yield comps, d_comp, l_comp


def kitchen_is_a_corridor(plan: Barndominium) -> bool:
    """True if some kitchen is the only route between dining and a living room.

    The predicate behind KITCHEN_PASSTHROUGH, without the compile step, so a
    solver can ask it of a candidate plan. Run it *after* the door passes: an
    arrangement only counts as a corridor once nothing has bypassed it.
    """
    return any(True for _ in _kitchen_severances(plan))


def _kitchen_bypass_wall(
    plan: Barndominium, min_wall: float
) -> tuple[str, str] | None:
    """The widest shared wall that would relieve a kitchen pass-through, if any."""
    by_id = {r.id: r for r in plan.rooms}
    for comps, d_comp, l_comp in _kitchen_severances(plan):
        best: tuple[float, str, str] | None = None
        for a in comps[d_comp]:
            for b in comps[l_comp]:
                ra, rb = by_id[a], by_id[b]
                if ra.level != rb.level:
                    continue
                edge = shared_edge(ra, rb)
                if edge is None or edge.length < min_wall:
                    continue
                key = (-edge.length, a, b)  # widest wall, ties by id
                if best is None or key < best:
                    best = key
        if best is not None:
            return best[1], best[2]
    return None


# --- openings ---------------------------------------------------------------


def place_openings(
    plan: Barndominium,
    brief: LayoutBrief,
    by_id: dict[str, RoomSpec],
    notes: list[str],
) -> None:
    """Auto-add a front entry and egress/daylight windows on exterior walls."""
    # 1. Front entry on a public/mudroom room with an exterior wall, plus the
    #    landing IRC R311.3 requires on the outside of it (DOOR_NO_LANDING).
    entry_room = _pick_entry_room(plan, brief)
    if entry_room is not None:
        eroom = plan.room(entry_room)
        assert eroom is not None  # _pick_entry_room only returns an existing room id
        walls = exterior_walls(plan, eroom)
        wall = Direction.SOUTH if Direction.SOUTH in walls else walls[0]
        if _add_opening(plan, eroom, wall, feet(3), is_entry=True):
            _add_entry_landing(plan, eroom, plan.exterior_doors_for(eroom.id)[-1])
    else:
        notes.append("No room sits on an exterior wall for a front entry.")

    # 2. Egress windows for bedrooms (one per bedroom, on an exterior wall).
    for room in plan.rooms:
        if room.type is not RoomType.BEDROOM:
            continue
        walls = exterior_walls(plan, room)
        if not walls:
            notes.append(
                f"Bedroom '{room.id}' has no exterior wall; it can't get an "
                "egress window — move it to the perimeter."
            )
            continue
        wall = max(walls, key=lambda d: _wall_len(room, d))
        _add_opening(plan, room, wall, min(feet(4), _wall_len(room, wall) * 0.8))

    # 2b. A small window on any bath that sits on an exterior wall — light plus
    #     ventilation, and it clears BATH_VENT. An interior bath stays windowless
    #     (the validator's BATH_VENT info then correctly asks for a fan).
    for room in plan.rooms:
        if room.type not in (RoomType.BATHROOM, RoomType.HALF_BATH):
            continue
        if plan.windows_for(room.id):
            continue
        walls = exterior_walls(plan, room)
        if not walls:
            continue
        wall = max(walls, key=lambda d: _wall_len(room, d))
        _add_opening(plan, room, wall, min(feet(2.5), _wall_len(room, wall) * 0.5))

    # 3. Daylight: top up habitable rooms to the 8% glazing ratio.
    for room in plan.rooms:
        if room.type not in HABITABLE_TYPES:
            continue
        _glaze_to_ratio(plan, room, exterior_walls(plan, room), notes)

    # 4. Declare safety glazing wherever geometry lands a window in an R308.4
    #    hazard location. The auto-layout can't avoid every hazard window — a
    #    daylight window beside the front door, a bath window near the tub — so it
    #    specifies those tempered up front (using the SAME predicate the
    #    WINDOW_TEMPERED check uses, the single source of truth). The emitted plan
    #    then compiles clean instead of carrying a safety-glazing warning it can't
    #    act on.
    from .validation import window_tempered_reason

    for w in plan.windows:
        if window_tempered_reason(plan, w) is not None:
            w.tempered = True


def _pick_entry_room(plan: Barndominium, brief: LayoutBrief) -> str | None:
    def has_ext(rid: str) -> bool:
        r = plan.room(rid)
        return r is not None and bool(exterior_walls(plan, r))

    if brief.entry_room and has_ext(brief.entry_room):
        return brief.entry_room
    priority = [
        RoomType.FOYER,
        RoomType.MUDROOM,
        RoomType.GREAT_ROOM,
        RoomType.LIVING,
        RoomType.KITCHEN,
        RoomType.DINING,
    ]
    for t in priority:
        for r in plan.rooms:
            if r.type is t and r.level == 0 and exterior_walls(plan, r):
                return r.id
    for r in plan.rooms:  # any ground room on the perimeter
        if r.level == 0 and exterior_walls(plan, r):
            return r.id
    return None


def _existing_glaze(plan: Barndominium, room: Room) -> float:
    """Glazed area on this room's exterior walls (what already counts for light)."""
    ext = set(exterior_walls(plan, room))
    return sum(
        w.glazed_area for w in plan.windows_for(room.id) if w.wall in ext
    )


#: Don't add windows narrower than this — tiny slivers read as noise.
_MIN_WINDOW_WIDTH = feet(2)


def _glaze_to_ratio(
    plan: Barndominium, room: Room, walls: list[Direction], notes: list[str]
) -> None:
    """Add windows on exterior walls until the room meets the 8% light ratio."""
    # A small margin keeps us clear of the threshold despite float rounding and
    # the validator's own ceil()-based reporting.
    need = _NAT_LIGHT_RATIO * room.area * 1.05 - _existing_glaze(plan, room)
    if need <= 1e-6:
        return
    if not walls:
        return  # interior room — validation will flag NAT_LIGHT; nothing we can do
    # Sweep the walls repeatedly: one pass can fall short when a wall's free run
    # is broken up (by the entry door, or by the trim reveal a window keeps off an
    # interior partition), and the leftover run on another wall still fits glass.
    # Each placement consumes wall length, so a pass that places nothing ends it.
    while need > 1e-6:
        progress = False
        for wall in _glaze_wall_order(plan, room, walls):
            if need <= 1e-6:
                break
            wall_len = _wall_len(room, wall)
            # Round up to a neat half-foot so widths read cleanly.
            raw = need / _GLASS_PER_FT
            width = min(_round_half_up(raw), wall_len * 0.8)
            if width < _MIN_WINDOW_WIDTH:
                if wall_len * 0.8 < _MIN_WINDOW_WIDTH:
                    continue  # wall too short for even a minimal window
                width = _MIN_WINDOW_WIDTH
            placed = _add_opening(plan, room, wall, width)
            if placed > 1e-6:
                progress = True
            need -= placed * _GLASS_PER_FT
        if not progress:
            break
    if need > 1e-6:
        notes.append(
            f"Room '{room.id}' may still fall short of 8% daylight; its exterior "
            "walls can't hold enough glazing — enlarge a window or add a wall."
        )


def _glaze_wall_order(
    plan: Barndominium, room: Room, walls: list[Direction]
) -> list[Direction]:
    """Walls to try next: those without a window yet first, longest run first.

    A stable sort, so walls of equal length keep the caller's order.
    """
    used = {w.wall for w in plan.windows_for(room.id)}
    return sorted(walls, key=lambda d: (d in used, -_wall_len(room, d)))


def _add_opening(
    plan: Barndominium, room: Room, wall: Direction, width: float, *, is_entry: bool = False
) -> float:
    """Place an opening on ``wall`` in the largest free gap, so it never overlaps
    an existing opening on the same wall (which would be an ``OPENING_CLASH``).

    The width is shrunk to fit the gap if necessary. Returns the width actually
    placed (0.0 if the wall is fully occupied), so a caller topping up glazing
    can account for what really went on the wall rather than what it asked for.
    """
    wall_len = _wall_len(room, wall)
    width = min(width, wall_len)
    # Intervals already taken by an opening on this wall, clamped to the wall.
    taken = sorted(
        (max(0.0, o.offset), min(wall_len, o.offset + o.width))
        for o in plan.exterior_doors_for(room.id) + plan.windows_for(room.id)
        if o.wall == wall
    )
    # The free gaps between them.
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for lo, hi in taken:
        if lo - cursor > 1e-6:
            gaps.append((cursor, lo))
        cursor = max(cursor, hi)
    if wall_len - cursor > 1e-6:
        gaps.append((cursor, wall_len))
    if not is_entry:
        gaps = [_window_reveal(plan, room, wall, wall_len, g) for g in gaps]
    gaps = [g for g in gaps if g[1] - g[0] > 1e-6]
    if not gaps:
        return 0.0  # wall is full — nothing we can place
    g_lo, g_hi = max(gaps, key=lambda g: g[1] - g[0])
    placed = min(width, g_hi - g_lo)
    if placed <= 1e-6:
        return 0.0
    offset = g_lo + (g_hi - g_lo - placed) / 2.0  # centre within the gap
    if is_entry:
        plan.entrance(room.id, wall, width=placed, offset=offset)
    else:
        plan.add_window(room.id, wall, width=placed, offset=offset)
    return placed


def _window_reveal(
    plan: Barndominium,
    room: Room,
    wall: Direction,
    wall_len: float,
    gap: tuple[float, float],
) -> tuple[float, float]:
    """Shrink ``gap`` so a window in it keeps clear of an interior partition.

    A window butting the partition where it lands on the exterior wall has no
    room for its framing and trim (WINDOW_PARTITION). A *building* corner is
    exempt — a window flush to one is fine — so the reveal is only taken at a wall
    end whose perpendicular neighbour is a partition. Gap ends formed by another
    opening are left alone: the door/window jambs already frame each other, and
    stealing width there would cost daylight the room needs.
    """
    from .validation import WINDOW_WALL_CLEAR, building_corner

    lo, hi = gap
    if lo <= 1e-6 and not building_corner(plan, room, wall, False):
        lo += WINDOW_WALL_CLEAR
    if hi >= wall_len - 1e-6 and not building_corner(plan, room, wall, True):
        hi -= WINDOW_WALL_CLEAR
    return lo, hi


def _add_entry_landing(plan: Barndominium, room: Room, door) -> None:
    """Draw the landing IRC R311.3 requires outside an exterior door.

    A stoop spanning the opening (a foot proud of each jamb) and reaching
    :data:`~barndsl.validation.LANDING_MIN_DEPTH` clear of the wall, so you don't
    step out into space. Without it every generated plan carries the same
    DOOR_NO_LANDING / DOOR_THRESHOLD nudge it has no way to act on.
    """
    from .geometry import opening_endpoints
    from .validation import LANDING_MIN_DEPTH

    depth = LANDING_MIN_DEPTH + 1.0
    flank = 1.0
    x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
    if door.wall in (Direction.SOUTH, Direction.NORTH):
        x = min(x1, x2) - flank
        y = y1 - depth if door.wall is Direction.SOUTH else y1
        w, length = abs(x2 - x1) + 2 * flank, depth
    else:
        y = min(y1, y2) - flank
        x = x1 - depth if door.wall is Direction.WEST else x1
        w, length = depth, abs(y2 - y1) + 2 * flank
    plan.add_porch(f"{room.id}_landing", x=x, y=y, width=w, length=length)


def _wall_len(room: Room, wall: Direction) -> float:
    return room.width if wall in (Direction.NORTH, Direction.SOUTH) else room.length


def _round_half_up(value: float) -> float:
    """Round a window width up to the next neat half-foot."""
    return math.ceil(value * 2.0 - 1e-9) / 2.0


# --- textual brief ----------------------------------------------------------


def parse_brief_fields(text: str, name: str, parse_room) -> dict:
    """Parse the brief grammar shared by the v1 and v2 engines.

    Both :func:`parse_brief` and :func:`barndsl.layout2.parse_brief2` accept the
    *same* statements — only the ``room`` line differs (fixed ``W x L`` vs a size
    program). This runs the common loop and delegates each room line to
    ``parse_room(rest, raw)``, returning the keyword arguments common to both
    :class:`LayoutBrief` and :class:`LayoutBrief2`.

    Grammar (one statement per line, ``#`` comments)::

        plan "Name"
        envelope <W> x <L>        # optional; omit to size to the packed bbox
        ceiling <H>
        note "free text"
        entry <room>              # which room gets the front door
        no-openings               # don't auto-add entry/windows
        room <id>: <type> ...     # engine-specific; parsed by ``parse_room``
        adjacent <a> <b> [<c> ...]   # connect <a> to each of the rest (a hub)

    ``adjacent hall bed1 bed2 bath`` is shorthand for ``hall-bed1``,
    ``hall-bed2`` and ``hall-bath`` — exactly the "rooms off a spine" idiom.
    """
    plan_name = name
    rooms: list = []
    adjacencies: list[tuple[str, str]] = []
    envelope: tuple[float, float] | None = None
    ceiling = feet(9)
    notes: list[str] = []
    entry_room: str | None = None
    add_openings = True

    for raw in text.splitlines():
        cut = comment_start(raw)  # a `#` inside `plan "Unit #3"` isn't a comment
        line = (raw if cut is None else raw[:cut]).strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        head = head.lower()
        rest = rest.strip()
        if head == "plan":
            plan_name = rest.strip().strip('"') or plan_name
        elif head == "envelope":
            parts = rest.lower().replace("x", " ").split()
            if len(parts) < 2:
                raise ValueError(f"Bad envelope line: {raw!r}")
            envelope = (float(parts[0]), float(parts[1]))
        elif head == "ceiling":
            ceiling = float(rest)
        elif head == "note":
            notes.append(rest.strip().strip('"'))
        elif head == "entry":
            entry_room = rest.strip()
        elif head in ("no-openings", "no_openings"):
            add_openings = False
        elif head == "room":
            rooms.append(parse_room(rest, raw))
        elif head in ("adjacent", "adj"):
            members = rest.replace(",", " ").split()
            if len(members) < 2:
                raise ValueError(f"`adjacent` needs at least two rooms: {raw!r}")
            hub = members[0]
            for other in members[1:]:
                adjacencies.append((hub, other))
        else:
            raise ValueError(f"Unknown brief statement: {raw!r}")

    return {
        "name": plan_name,
        "rooms": rooms,
        "adjacencies": adjacencies,
        "envelope": envelope,
        "ceiling": ceiling,
        "notes": "\n".join(notes),
        "entry_room": entry_room,
        "add_openings": add_openings,
    }


def parse_brief(text: str, name: str = "Layout") -> LayoutBrief:
    """Parse a small textual brief into a :class:`LayoutBrief`.

    Room lines carry fixed sizes (``room <id>: <type> <W> x <L> [level <n>]``);
    everything else is the shared grammar documented on :func:`parse_brief_fields`.

    .. deprecated::
        The v1 "greedy" engine is superseded by :func:`barndsl.layout2.solve_layout2`
        (the CLI default). ``parse_brief``/``solve_layout`` remain for the
        ``--engine greedy`` fallback and existing callers.
    """
    return LayoutBrief(**parse_brief_fields(text, name, _parse_room_line))


def _parse_room_line(rest: str, raw: str) -> RoomSpec:
    """Parse ``<id>: <type> <W> x <L> [level <n>]`` (the part after ``room``)."""
    rid, sep, after = rest.partition(":")
    if not sep:
        raise ValueError(f"Room line needs `id: type W x L`: {raw!r}")
    rid = rid.strip()
    if not rid:
        raise ValueError(f"Room line has an empty id: {raw!r}")
    toks = after.lower().replace("x", " ").split()
    # toks: type W L [level n]
    if len(toks) < 3:
        raise ValueError(f"Room line needs `type W x L`: {raw!r}")
    rtype = toks[0]
    width = float(toks[1])
    length = float(toks[2])
    level = 0
    if "level" in toks:
        i = toks.index("level")
        if i + 1 < len(toks):
            level = int(float(toks[i + 1]))
    return RoomSpec(rid, rtype, width, length, level=level)
