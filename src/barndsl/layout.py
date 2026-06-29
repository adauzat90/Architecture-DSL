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
    Room,
    RoomType,
    feet,
    inches,
)
from .constants import NATURAL_LIGHT_RATIO as _NAT_LIGHT_RATIO
from .geometry import shared_edge

# Glazed area a 1-ft-wide window contributes (head 6.67 - sill 3.0 ft tall),
# used to size daylight windows; mirrors Window's defaults in elements.py.
_GLASS_PER_FT = feet(6.67) - feet(3.0)


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
    """Place every room in ``brief`` and return a plan plus a satisfaction report."""
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

    satisfied, unsatisfied = _connect_adjacencies(plan, brief, by_id)
    if brief.add_openings:
        _add_openings(plan, brief, by_id, notes)

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
        return Room(spec.id, spec.type, x, y, w, l, spec.label, spec.level)

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


def _connect_adjacencies(
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
                by_id[a].type in {RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING}
                and by_id[b].type in {RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING}
            )
            width = feet(6) if both_public else inches(32)
            plan.connect(a, b, width=width)
            satisfied.append((a, b))
        else:
            unsatisfied.append((a, b))
    return satisfied, unsatisfied


# --- openings ---------------------------------------------------------------


def _add_openings(
    plan: Barndominium,
    brief: LayoutBrief,
    by_id: dict[str, RoomSpec],
    notes: list[str],
) -> None:
    """Auto-add a front entry and egress/daylight windows on exterior walls."""
    from .validation import exterior_walls  # local: validation imports geometry only

    # 1. Front entry on a public/mudroom room with an exterior wall.
    entry_room = _pick_entry_room(plan, brief)
    if entry_room is not None:
        room = plan.room(entry_room)
        walls = exterior_walls(plan, room)
        wall = Direction.SOUTH if Direction.SOUTH in walls else walls[0]
        _add_opening(plan, room, wall, feet(3), is_entry=True)
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


def _pick_entry_room(plan: Barndominium, brief: LayoutBrief) -> str | None:
    from .validation import exterior_walls

    def has_ext(rid: str) -> bool:
        r = plan.room(rid)
        return r is not None and bool(exterior_walls(plan, r))

    if brief.entry_room and has_ext(brief.entry_room):
        return brief.entry_room
    priority = [
        RoomType.MUDROOM,
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
    from .validation import exterior_walls

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
    used_walls = {w.wall for w in plan.windows_for(room.id)}
    # Prefer walls without a window yet, longest first, then any wall.
    fresh = sorted(
        (w for w in walls if w not in used_walls),
        key=lambda d: _wall_len(room, d),
        reverse=True,
    )
    rest = sorted(
        (w for w in walls if w in used_walls),
        key=lambda d: _wall_len(room, d),
        reverse=True,
    )
    for wall in fresh + rest:
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
        need -= placed * _GLASS_PER_FT
    if need > 1e-6:
        notes.append(
            f"Room '{room.id}' may still fall short of 8% daylight; its exterior "
            "walls can't hold enough glazing — enlarge a window or add a wall."
        )


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


def _wall_len(room: Room, wall: Direction) -> float:
    return room.width if wall in (Direction.NORTH, Direction.SOUTH) else room.length


def _round_half_up(value: float) -> float:
    """Round a window width up to the next neat half-foot."""
    return math.ceil(value * 2.0 - 1e-9) / 2.0


# --- textual brief ----------------------------------------------------------


def parse_brief(text: str, name: str = "Layout") -> LayoutBrief:
    """Parse a small textual brief into a :class:`LayoutBrief`.

    Grammar (one statement per line, ``#`` comments)::

        plan "Name"
        envelope <W> x <L>        # optional; omit to size to the packed bbox
        ceiling <H>
        note "free text"
        entry <room>              # which room gets the front door
        no-openings               # don't auto-add entry/windows
        room <id>: <type> <W> x <L> [level <n>]
        adjacent <a> <b> [<c> ...]   # connect <a> to each of the rest (a hub)

    ``adjacent hall bed1 bed2 bath`` is shorthand for ``hall-bed1``,
    ``hall-bed2`` and ``hall-bath`` — exactly the "rooms off a spine" idiom.
    """
    plan_name = name
    rooms: list[RoomSpec] = []
    adjacencies: list[tuple[str, str]] = []
    envelope: tuple[float, float] | None = None
    ceiling = feet(9)
    notes: list[str] = []
    entry_room: str | None = None
    add_openings = True

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
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
            rooms.append(_parse_room_line(rest, raw))
        elif head in ("adjacent", "adj"):
            members = rest.replace(",", " ").split()
            if len(members) < 2:
                raise ValueError(f"`adjacent` needs at least two rooms: {raw!r}")
            hub = members[0]
            for other in members[1:]:
                adjacencies.append((hub, other))
        else:
            raise ValueError(f"Unknown brief statement: {raw!r}")

    return LayoutBrief(
        name=plan_name,
        rooms=rooms,
        adjacencies=adjacencies,
        envelope=envelope,
        ceiling=ceiling,
        notes="\n".join(notes),
        entry_room=entry_room,
        add_openings=add_openings,
    )


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
