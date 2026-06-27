"""Auto-layout 2.0 — a space-filling, perimeter-aware layout engine.

Where v1 (:mod:`barndsl.layout`) places rooms by greedy abutment and lets the
envelope fall out as the bounding box — leaving holes, burying habitable rooms,
and elongating the footprint — v2 **dissects the envelope**. It arranges rooms
into horizontal bands (a public core, an optional hall spine, a private row) and
dimensions every band to tile the rectangle with no gaps and no overlap, so
habitable rooms land on the perimeter *by construction*.

This is the Phase 1 engine from ``docs/design/AUTO_LAYOUT_2.md``: a deterministic,
pure-Python dimensioned slicing/band layout. It consumes a *size program* (target
area + minimum dimension, or a fixed ``w×l``) rather than fixed coordinates, which
is what lets it fill an arbitrary envelope. Doors, the entry and windows are added
by the same logic v1 already uses, and the result is the same
:class:`~barndsl.layout.LayoutResult`.

    from barndsl.layout2 import RoomSpec2, LayoutBrief2, solve_layout2

    out = solve_layout2(LayoutBrief2(
        name="Birch Run",
        rooms=[RoomSpec2("living", "living", area=360),
               RoomSpec2("hall", "hallway", area=160, min_dim=4),
               RoomSpec2("bed1", "bedroom", area=170)],
        adjacencies=[("living", "hall"), ("hall", "bed1")],
    ))
    print(out.summary())
    print(emit_dsl(out.plan))
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .elements import Barndominium, RoomType, feet, inches
from .geometry import shared_edge
from .layout import (
    LayoutBrief,
    LayoutResult,
    RoomSpec,
    _add_openings,
    _connect_adjacencies,
)

#: Rooms forming the open core; they tile the first band and share vertical walls.
_PUBLIC = (RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING)
#: Circulation; may be interior (no daylight needed) and sits between the bands.
_CIRCULATION = (RoomType.HALLWAY,)
#: A sane default footprint proportion (slightly long, like a barndo) when we
#: size the envelope ourselves.
_DEFAULT_ASPECT = 1.5


# --- the brief --------------------------------------------------------------


@dataclass
class RoomSpec2:
    """A room described by a *size program*, not coordinates.

    Give either a fixed ``width``/``length`` (rigid) or a target ``area`` (the
    engine picks the proportions to fill the band). ``min_dim`` is the smallest
    allowed side — defaults sensibly per type. The engine treats fixed sizes as
    a target area with the given min dimension, so it can still tile.
    """

    id: str
    type: RoomType | str
    area: float | None = None
    min_dim: float | None = None
    width: float | None = None
    length: float | None = None
    label: str | None = None
    level: int = 0

    def __post_init__(self) -> None:
        self.type = RoomType(self.type)
        if self.area is None:
            if self.width is None or self.length is None:
                raise ValueError(
                    f"Room '{self.id}': give an area=, or both width= and length=."
                )
            self.area = float(self.width) * float(self.length)
        self.area = float(self.area)
        if not math.isfinite(self.area) or self.area <= 0:
            raise ValueError(f"Room '{self.id}': area must be positive and finite.")
        if self.min_dim is None:
            if self.width is not None and self.length is not None:
                self.min_dim = min(float(self.width), float(self.length))
            else:
                self.min_dim = _default_min_dim(self.type)
        self.min_dim = float(self.min_dim)

    @property
    def target_w(self) -> float | None:
        return float(self.width) if self.width is not None else None


def _default_min_dim(t: RoomType) -> float:
    if t is RoomType.HALLWAY:
        return feet(3)
    if t in (RoomType.BEDROOM, RoomType.LIVING, RoomType.KITCHEN, RoomType.DINING):
        return feet(8)
    return feet(5)


@dataclass
class LayoutBrief2:
    """A program for the v2 engine: rooms (size programs) + adjacencies."""

    name: str
    rooms: list[RoomSpec2]
    adjacencies: list[tuple[str, str]] = field(default_factory=list)
    #: Fixed envelope ``(W, L)``, or ``None`` to size one to the packed bands.
    envelope: tuple[float, float] | None = None
    ceiling: float = feet(9)
    notes: str = ""
    entry_room: str | None = None
    add_openings: bool = True
    #: Footprint proportion target when sizing our own envelope (W/L).
    aspect: float = _DEFAULT_ASPECT


# --- bands ------------------------------------------------------------------


@dataclass
class _Band:
    """A horizontal strip of the envelope, tiled left→right by its rooms."""

    rooms: list[RoomSpec2]
    interior_ok: bool = False  # True for the hall — it needn't reach an outer wall

    @property
    def area(self) -> float:
        return sum(r.area for r in self.rooms)

    @property
    def min_width(self) -> float:
        """Narrowest this band can be: the sum of its rooms' minimum widths."""
        return sum(r.min_dim for r in self.rooms)

    @property
    def max_min_dim(self) -> float:
        return max((r.min_dim for r in self.rooms), default=0.0)


def solve_layout2(brief: LayoutBrief2) -> LayoutResult:
    """Dissect the envelope into bands and dimension every room to tile it."""
    specs = list(brief.rooms)
    if not specs:
        raise ValueError("Layout brief has no rooms.")
    ids = [s.id for s in specs]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"Duplicate room id(s) in brief: {', '.join(dupes)}.")
    by_id = {s.id: s for s in specs}
    adj: dict[str, set[str]] = {s.id: set() for s in specs}
    for a, b in brief.adjacencies:
        if a not in by_id or b not in by_id:
            missing = a if a not in by_id else b
            raise ValueError(f"Adjacency names unknown room '{missing}'.")
        if a != b:
            adj[a].add(b)
            adj[b].add(a)

    notes: list[str] = []
    bands = _build_bands(specs, adj, notes)
    env_w, env_l = _choose_envelope(bands, brief, notes)
    env_w, env_l = round(env_w, 2), round(env_l, 2)  # match the snapped grid
    placed = _dimension(bands, env_w, env_l)

    plan = Barndominium(brief.name, env_w, env_l, brief.ceiling)
    if brief.notes:
        plan.note(brief.notes)
    for spec in specs:  # keep brief order for a stable, readable plan
        plan.rooms.append(placed[spec.id])

    # Reuse v1's door + opening logic by lowering to a v1 brief (final sizes).
    v1 = _as_v1_brief(brief, placed)
    v1_by_id = {s.id: s for s in v1.rooms}
    satisfied, unsatisfied = _connect_adjacencies(plan, v1, v1_by_id)
    # The tiling is one physically-connected mass, but the *door* graph can split
    # when a requested adjacency couldn't be honoured (e.g. a room can't be a row
    # neighbour of all three of its requested neighbours). Add the fewest doors on
    # real shared walls to make every room reachable.
    added = _ensure_connected(plan)
    if added:
        notes.append(
            f"Added {added} circulation door(s) on shared walls to connect the plan."
        )
    if brief.add_openings:
        # Put the entry on a room that is actually connected to the rest.
        if v1.entry_room is None:
            v1.entry_room = _pick_connected_entry(plan)
        _add_openings(plan, v1, v1_by_id, notes)

    return LayoutResult(plan, satisfied, unsatisfied, notes)


def _build_bands(
    specs: list[RoomSpec2], adj: dict[str, set[str]], notes: list[str]
) -> list[_Band]:
    """Classify rooms and stack them south→north: public, hall, private.

    The hall sits between the public core and the private rooms so it abuts both;
    public and private bands reach the envelope's south/north edges, giving their
    rooms an exterior wall. Rooms within a band are ordered so requested
    adjacencies fall between neighbours (and thus share a vertical wall).
    """
    public_ids = {s.id for s in specs if s.type in _PUBLIC}
    hall_ids = {s.id for s in specs if s.type in _CIRCULATION}

    def is_public_band(s: RoomSpec2) -> bool:
        # Public rooms, plus service/office rooms that hang off the core (adjacent
        # to a public room and not to the hall) — keeps their adjacency and an
        # exterior wall. Bedrooms/baths always go to the private row off the hall.
        if s.type in _PUBLIC:
            return True
        if s.type in (RoomType.BEDROOM,) + _CIRCULATION:
            return False
        return bool(adj[s.id] & public_ids) and not (adj[s.id] & hall_ids)

    public = [s for s in specs if is_public_band(s)]
    halls = [s for s in specs if s.type in _CIRCULATION]
    private = [
        s for s in specs if s not in public and s.type not in _CIRCULATION
    ]

    bands: list[_Band] = []
    if public:
        bands.append(_Band(_order_in_band(public, adj)))
    for h in halls:  # usually one; multiple halls each get a thin strip
        bands.append(_Band([h], interior_ok=True))
    if private:
        bands.append(_Band(_order_in_band(private, adj)))

    if not bands:  # only halls, or some odd program — one band, everything
        bands = [_Band(_order_in_band(specs, adj))]
    return bands


def _order_in_band(rooms: list[RoomSpec2], adj: dict[str, set[str]]) -> list[RoomSpec2]:
    """Order a band's rooms so adjacent ones are neighbours (a greedy chain).

    Start from the lowest-degree room and walk the adjacency graph, preferring
    the next room that is adjacent to the one just placed. Deterministic; falls
    back to brief order when there's no adjacency to follow.
    """
    if len(rooms) <= 2:
        return rooms
    pool = {r.id: r for r in rooms}
    order_index = {r.id: i for i, r in enumerate(rooms)}
    within = {rid: (adj[rid] & pool.keys()) for rid in pool}
    # Seed: a room with the fewest in-band neighbours (an end of the chain).
    start = min(pool, key=lambda rid: (len(within[rid]), order_index[rid]))
    ordered = [start]
    seen = {start}
    while len(ordered) < len(rooms):
        nbrs = [n for n in within[ordered[-1]] if n not in seen]
        if nbrs:
            nxt = min(nbrs, key=lambda rid: (len(within[rid]), order_index[rid]))
        else:  # chain broke — take the next unplaced room in brief order
            nxt = min(
                (rid for rid in pool if rid not in seen), key=lambda rid: order_index[rid]
            )
        ordered.append(nxt)
        seen.add(nxt)
    return [pool[rid] for rid in ordered]


def _choose_envelope(
    bands: list[_Band], brief: LayoutBrief2, notes: list[str]
) -> tuple[float, float]:
    """Pick the envelope: honor a fixed one, else size it to fit the bands.

    Width must be at least the widest band's minimum width. Height is the sum of
    band heights, each height being the band's area / width (so the band tiles
    exactly) but never thinner than its tallest room's minimum dimension.
    """
    total_area = sum(b.area for b in bands)
    min_w = max((b.min_width for b in bands), default=feet(8))

    if brief.envelope is not None:
        env_w, env_l = float(brief.envelope[0]), float(brief.envelope[1])
        if env_w + 1e-6 < min_w:
            notes.append(
                f"Envelope width {env_w:g} ft is below the {min_w:g} ft needed to "
                "fit the widest band; rooms will be cramped or out of bounds."
            )
        return env_w, env_l

    # Size to fit: aim for the requested footprint proportion, then satisfy mins.
    env_w = max(math.sqrt(total_area * brief.aspect), min_w)
    env_l = sum(_band_height(b, env_w) for b in bands)
    return env_w, env_l


def _band_height(band: _Band, env_w: float) -> float:
    """Height that makes the band tile ``env_w`` at its area, respecting min dims."""
    return max(band.area / env_w, band.max_min_dim)


#: Grid resolution (ft). Coordinates snap here so the tiling's shared walls
#: survive the `%g` round-trip through emitted DSL exactly (see _grid_lines).
_GRID = 0.01


def _grid_lines(spans: list[float], start: float, end: float) -> list[float]:
    """Cumulative cut lines for ``spans``, snapped to the grid and to ``[start, end]``.

    Returns ``len(spans)+1`` coordinates. Each room is the gap between two
    consecutive lines, so abutting rooms share the *exact same* line value — which
    is what keeps their shared wall intact after the numbers are rounded for DSL
    output. The final line is pinned to ``end`` so the tiling meets the envelope.
    """
    lines = [round(start, 2)]
    pos = start
    for s in spans:
        pos += s
        lines.append(round(pos, 2))
    lines[-1] = round(end, 2)
    return lines


def _dimension(bands: list[_Band], env_w: float, env_l: float):
    """Assign absolute rectangles. Bands stack south→north and fill the width.

    Heights are proportional to band area (scaled to the envelope height); within
    a band, each room gets its minimum width plus a share of the slack
    proportional to its area, so widths sum to ``env_w`` and every room meets its
    minimum dimension. Coordinates come from shared grid lines (see _grid_lines)
    so the dissection round-trips losslessly.
    """
    from .elements import Room

    raw_heights = [_band_height(b, env_w) for b in bands]
    total_raw = sum(raw_heights) or 1.0
    heights = [env_l * h / total_raw for h in raw_heights]  # scale to fill height
    ylines = _grid_lines(heights, 0.0, env_l)

    placed: dict[str, Room] = {}
    for bi, band in enumerate(bands):
        y0, y1 = ylines[bi], ylines[bi + 1]
        xlines = _grid_lines(_share_widths(band, env_w), 0.0, env_w)
        for i, spec in enumerate(band.rooms):
            x0, x1 = xlines[i], xlines[i + 1]
            placed[spec.id] = Room(
                spec.id, spec.type, x0, y0, x1 - x0, y1 - y0, spec.label, spec.level
            )
    return placed


def _share_widths(band: _Band, env_w: float) -> list[float]:
    """Split ``env_w`` among a band's rooms: each gets its min + an area-share of slack."""
    mins = [r.min_dim for r in band.rooms]
    slack = env_w - sum(mins)
    if slack <= 0:  # envelope too narrow for the mins — distribute proportionally
        scale = env_w / (sum(mins) or 1.0)
        return [m * scale for m in mins]
    area_total = band.area or 1.0
    return [m + slack * (r.area / area_total) for m, r in zip(mins, band.rooms)]


def _ensure_connected(plan: Barndominium, min_wall: float = inches(32)) -> int:
    """Add doors on shared walls until the interior-door graph spans the plan.

    Returns the number of doors added. Uses union-find over rooms: while more than
    one component remains, connect the two components across their widest shared
    wall. The tiling is always one connected mass, so this terminates with a fully
    reachable plan; it adds the minimum number of doors (one per merge).
    """
    parent = {r.id: r.id for r in plan.rooms}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for d in plan.interior_doors:
        if d.room_a in parent and d.room_b in parent:
            union(d.room_a, d.room_b)

    added = 0
    while len({find(r.id) for r in plan.rooms}) > 1:
        best = None  # (wall_length, id_a, id_b)
        rooms = plan.rooms
        for i, a in enumerate(rooms):
            for b in rooms[i + 1 :]:
                if find(a.id) == find(b.id) or a.level != b.level:
                    continue
                se = shared_edge(a, b)
                if se is not None and se.length >= min_wall:
                    if best is None or se.length > best[0]:
                        best = (se.length, a.id, b.id)
        if best is None:
            break  # nothing wide enough to join (shouldn't happen on a tiling)
        plan.connect(best[1], best[2], width=min_wall)
        union(best[1], best[2])
        added += 1
    return added


def _pick_connected_entry(plan: Barndominium) -> str | None:
    """A room with an exterior wall in the largest door-connected component.

    Prefers public rooms (living/kitchen/dining) so the front door opens into the
    core, but only among rooms reachable through the interior-door graph — so the
    entry never strands the rest of the plan on the wrong side of a missing door.
    """
    from collections import deque

    from .validation import exterior_walls

    graph: dict[str, set[str]] = {r.id: set() for r in plan.rooms}
    for d in plan.interior_doors:
        if d.room_a in graph and d.room_b in graph:
            graph[d.room_a].add(d.room_b)
            graph[d.room_b].add(d.room_a)

    # Largest connected component by BFS.
    seen: set[str] = set()
    best_comp: set[str] = set()
    for start in graph:
        if start in seen:
            continue
        comp, q = set(), deque([start])
        while q:
            n = q.popleft()
            if n in comp:
                continue
            comp.add(n)
            seen.add(n)
            q.extend(graph[n] - comp)
        if len(comp) > len(best_comp):
            best_comp = comp

    pref = {RoomType.LIVING: 0, RoomType.KITCHEN: 1, RoomType.DINING: 2, RoomType.MUDROOM: 3}
    candidates = [
        r
        for r in plan.rooms
        if r.id in best_comp and r.level == 0 and exterior_walls(plan, r)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: (pref.get(r.type, 9), r.id))
    return candidates[0].id


def parse_brief2(text: str, name: str = "Layout") -> LayoutBrief2:
    """Parse a textual brief into a :class:`LayoutBrief2`.

    Same grammar as :func:`barndsl.layout.parse_brief`, but room lines carry a
    *size program* instead of fixed coordinates::

        room <id>: <type> <W> x <L>          # fixed size
        room <id>: <type> area <A> [min <m>] # target area, min dimension

    Plus ``plan``, ``envelope`` (optional — omit to size to fit), ``ceiling``,
    ``note``, ``entry <room>``, ``no-openings``, and ``adjacent <a> <b> [...]``
    (connect ``<a>`` to each of the rest).
    """
    plan_name = name
    rooms: list[RoomSpec2] = []
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
        head, rest = head.lower(), rest.strip()
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
            rooms.append(_parse_room2(rest, raw))
        elif head in ("adjacent", "adj"):
            members = rest.replace(",", " ").split()
            if len(members) < 2:
                raise ValueError(f"`adjacent` needs at least two rooms: {raw!r}")
            for other in members[1:]:
                adjacencies.append((members[0], other))
        else:
            raise ValueError(f"Unknown brief statement: {raw!r}")

    return LayoutBrief2(
        name=plan_name,
        rooms=rooms,
        adjacencies=adjacencies,
        envelope=envelope,
        ceiling=ceiling,
        notes="\n".join(notes),
        entry_room=entry_room,
        add_openings=add_openings,
    )


def _parse_room2(rest: str, raw: str) -> RoomSpec2:
    """Parse ``<id>: <type> (<W> x <L> | area <A> [min <m>])``."""
    rid, sep, after = rest.partition(":")
    if not sep:
        raise ValueError(f"Room line needs `id: type ...`: {raw!r}")
    rid = rid.strip()
    if not rid:
        raise ValueError(f"Room line has an empty id: {raw!r}")
    toks = after.split()
    if len(toks) < 2:
        raise ValueError(f"Room line needs a type and size: {raw!r}")
    rtype = toks[0].lower()
    rest_toks = [t.lower() for t in toks[1:]]
    if "area" in rest_toks:
        i = rest_toks.index("area")
        area = float(rest_toks[i + 1])
        min_dim = None
        if "min" in rest_toks:
            min_dim = float(rest_toks[rest_toks.index("min") + 1])
        return RoomSpec2(rid, rtype, area=area, min_dim=min_dim)
    # fixed: type W x L
    nums = [t for t in rest_toks if t not in ("x", "by")]
    if len(nums) < 2:
        raise ValueError(f"Room line needs `W x L` or `area A`: {raw!r}")
    return RoomSpec2(rid, rtype, width=float(nums[0]), length=float(nums[1]))


def _as_v1_brief(brief: LayoutBrief2, placed) -> LayoutBrief:
    """Lower to a v1 brief (with final fixed sizes) to reuse door/opening logic."""
    rooms = [
        RoomSpec(
            s.id,
            s.type,
            placed[s.id].width,
            placed[s.id].length,
            label=s.label,
            level=s.level,
        )
        for s in brief.rooms
    ]
    return LayoutBrief(
        name=brief.name,
        rooms=rooms,
        adjacencies=list(brief.adjacencies),
        envelope=(0.0, 0.0),  # unused by the helpers we call
        ceiling=brief.ceiling,
        notes=brief.notes,
        entry_room=brief.entry_room,
        add_openings=brief.add_openings,
    )
