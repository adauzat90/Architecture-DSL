"""Auto-layout 2.0 — a space-filling, perimeter-aware layout engine.

Where v1 (:mod:`barndsl.layout`) places rooms by greedy abutment and lets the
envelope fall out as the bounding box — leaving holes, burying habitable rooms,
and elongating the footprint — v2 **dissects the envelope**: it dimensions rooms
to tile the rectangle with no gaps and no overlap, so habitable rooms land on the
perimeter *by construction*.

It generates three topologies and keeps the best-scoring one (generate-and-select,
following the floor-planning literature):

* **bands** — horizontal strips (a public core, an optional hall spine, a private
  row); the residential idiom.
* **slice** — a recursive adjacency-ordered slicing tree, for clusters or a room
  needing three neighbours that a flat band can't seat.
* **dual** — a rectangular dual, which tiles so that *every* requested adjacency
  is a shared wall, including non-sliceable graphs like a pinwheel (a centre room
  touching four others).

It is deterministic and pure-Python, and consumes a *size program* (target area +
minimum dimension, or a fixed ``w×l``) rather than fixed coordinates, which is
what lets it fill an arbitrary envelope. Doors, the entry and windows are added by
the same logic v1 already uses, and the result is the same
:class:`~barndsl.layout.LayoutResult`. See ``docs/design/AUTO_LAYOUT_2.md``.

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

from .constants import GOOD_ASPECT
from .elements import HABITABLE_TYPES, Barndominium, RoomType, feet, inches
from .geometry import shared_edge
from .layout import (
    LayoutBrief,
    LayoutResult,
    RoomSpec,
    _add_openings,
    _connect_adjacencies,
    parse_brief_fields,
)

#: Rooms forming the open core; they tile the first band and share vertical walls.
_PUBLIC = (
    RoomType.LIVING,
    RoomType.GREAT_ROOM,
    RoomType.KITCHEN,
    RoomType.DINING,
    RoomType.REC_ROOM,
)
#: Circulation; may be interior (no daylight needed) and sits between the bands.
_CIRCULATION = (RoomType.HALLWAY,)
#: Large non-habitable spaces that get their own band, so their bulk doesn't set
#: the depth of the bedroom row.
_LARGE_UTILITY = (RoomType.GARAGE, RoomType.SHOP)
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
    #: Target floor area; ``0`` (the default) means "derive it from
    #: ``width``/``length``". Always a positive float once constructed.
    area: float = 0.0
    #: Smallest allowed side; ``0`` means "pick a sensible default per type".
    #: Always a positive float once constructed.
    min_dim: float = 0.0
    width: float | None = None
    length: float | None = None
    label: str | None = None
    level: int = 0

    def __post_init__(self) -> None:
        self.type = RoomType(self.type)
        if not self.area:
            if self.width is None or self.length is None:
                raise ValueError(
                    f"Room '{self.id}': give an area=, or both width= and length=."
                )
            self.area = float(self.width) * float(self.length)
        self.area = float(self.area)
        if not math.isfinite(self.area) or self.area <= 0:
            raise ValueError(f"Room '{self.id}': area must be positive and finite.")
        if not self.min_dim:
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
    if t in (
        RoomType.BEDROOM,
        RoomType.LIVING,
        RoomType.GREAT_ROOM,
        RoomType.KITCHEN,
        RoomType.DINING,
        RoomType.FLEX,
        RoomType.REC_ROOM,
    ):
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


def solve_layout2(brief: LayoutBrief2, engine: str = "auto") -> LayoutResult:
    """Lay out the brief by dissecting the envelope; return the placed plan.

    ``engine`` selects the topology:

    * ``"bands"`` — stack the rooms in horizontal bands (public core · hall ·
      private row). Habitable rooms reach an outer wall by construction; ideal
      for the residential idiom.
    * ``"slice"`` — a recursive adjacency-ordered slicing tree (nested H/V cuts).
      Handles a room that needs three neighbours, or nested clusters, that a flat
      band can't — but can bury a habitable room, so it isn't always valid.
    * ``"dual"`` — a rectangular dual: dissect the rectangle so *every* required
      adjacency is a shared wall, including non-sliceable graphs like the pinwheel
      (a centre room touching four others). Returns the best tiling it finds; if
      none exists it falls back (raises for an explicit ``engine="dual"``).
    * ``"auto"`` (default) — run every applicable engine and return the
      best-scoring valid layout (fewest errors, then fewest unmet adjacencies,
      then least wasted space). Never worse than ``bands``; sometimes better. The
      (more expensive) dual is tried only when bands and slice still leave an
      adjacency unmet or a room misplaced — exactly when it can help.

    This generate-and-select strategy follows the floor-planning literature
    (GPLAN enumerates topologies precisely because no single one fits every
    adjacency program); see ``docs/design/AUTO_LAYOUT_2.md``.
    """
    specs, adj = _prepare(brief)
    builders = {"bands": _solve_bands, "slice": _solve_slice}
    if engine in builders:
        return builders[engine](brief, specs, adj)
    if engine == "dual":
        result = _solve_dual(brief, specs, adj)
        if result is None:
            raise ValueError("No rectangular dual realises this adjacency program.")
        return result
    if engine != "auto":
        raise ValueError(f"Unknown engine {engine!r}; use auto, bands, slice or dual.")

    # Score the cheap topologies first. Ties favour the earlier (bands) one since
    # the sort is stable, so "auto" is never worse than bands.
    scored = [
        (_score(result), name, result)
        for name, result in ((n, fn(brief, specs, adj)) for n, fn in builders.items())
    ]
    scored.sort(key=lambda s: s[0])

    # The dual is the only topology that can realise an arbitrary adjacency graph
    # (e.g. a pinwheel), but it costs more, so only reach for it when the best
    # cheap topology still has errors or unmet adjacencies — its whole reason to
    # exist. Appended last, so it never steals a tie from bands/slice.
    best_score = scored[0][0]
    if best_score[0] or best_score[1]:
        dual = _solve_dual(brief, specs, adj)
        if dual is not None:
            scored.append((_score(dual), "dual", dual))
            scored.sort(key=lambda s: s[0])

    _, best_name, best = scored[0]
    if best_name != "bands":
        best.notes.append(f"Chose the '{best_name}' topology (best fit for this program).")
    return best


def _prepare(brief: LayoutBrief2):
    """Validate the brief and return ``(specs, symmetric adjacency map)``."""
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
    return specs, adj


def _proportion_penalty(plan: Barndominium) -> float:
    """How badly habitable rooms are elongated past
    :data:`~barndsl.constants.GOOD_ASPECT`.

    The bar is shared with :mod:`barndsl.score` rather than restated here, so the
    solver optimises toward exactly the ratio the plan is later judged against.

    Sums the aspect excess over every habitable room (bedrooms weighted double —
    a long, thin bedroom is the most noticeable). Zero when every such room is
    reasonably square; grows with each elongated room. Lower is better.
    """
    pen = 0.0
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES:
            continue
        side = min(r.width, r.length)
        if side <= 0:
            pen += 10.0
            continue
        excess = max(0.0, max(r.width, r.length) / side - GOOD_ASPECT)
        pen += (2.0 if r.type is RoomType.BEDROOM else 1.0) * excess
    return pen


def _score(result: LayoutResult) -> tuple:
    """Rank a candidate layout. Lower is better.

    In order of priority: fewer compiler **errors** (a buried bedroom or
    unreachable room is disqualifying), fewer **unmet adjacencies** (how much of
    the requested program it honored), then the plan's **design score** — the
    same deterministic 0–100 the agent loop maximises (warnings, design-quality
    infos, space/circulation/proportion/daylight margins), so topology selection
    stops emitting plans that immediately fire a pile of nudges it never tried
    to avoid. The old waste/proportion/aspect terms stay as tiebreaks (the
    design score coarsens them through its rounding).
    """
    from .compiler import compile_source
    from .emit import emit_dsl
    from .score import design_score

    plan = result.plan
    report = compile_source(emit_dsl(plan), name=plan.name)
    footprint = plan.envelope_width * plan.envelope_length or 1.0
    unused = 1.0 - sum(r.area for r in plan.rooms) / footprint
    w, h = plan.envelope_width, plan.envelope_length
    aspect = max(w, h) / min(w, h) if min(w, h) > 0 else 99.0
    return (
        len(report.errors),
        len(result.unsatisfied),
        round(100.0 - design_score(report).total, 1),
        round(unused, 3),
        round(_proportion_penalty(plan), 2),
        round(aspect, 2),
    )


def _solve_bands(
    brief: LayoutBrief2, specs: list[RoomSpec2], adj: dict[str, set[str]]
) -> LayoutResult:
    """Topology 1: horizontal bands (public core · hall · private row).

    A single-story program with a large utility room (garage/shop) routes through
    :func:`_solve_bands_with_column`, which seats the utility as a full-depth
    **gable-end column** rather than a full-width band — the barndominium idiom —
    so every habitable room keeps an exterior wall and the shop gets its own gable
    wall for the overhead door. All other programs use the plain band stack.
    """
    notes: list[str] = []
    utility = [s for s in specs if s.type in _LARGE_UTILITY]
    single_story = all(s.level == 0 for s in specs)
    if utility and single_story:
        column = _solve_bands_with_column(brief, specs, adj, utility, notes)
        if column is not None:
            return column
    bands = _build_bands(specs, adj, notes)
    env_w, env_l = _choose_envelope(bands, brief, notes)
    env_w, env_l = round(env_w, 2), round(env_l, 2)  # match the snapped grid
    placed = _dimension(bands, env_w, env_l)
    return _finalize(brief, specs, placed, env_w, env_l, notes)


def _solve_bands_with_column(
    brief: LayoutBrief2,
    specs: list[RoomSpec2],
    adj: dict[str, set[str]],
    utility: list[RoomSpec2],
    notes: list[str],
) -> LayoutResult | None:
    """Seat garage/shop rooms as a full-depth gable-end column; band the rest.

    The vehicle bay takes one short end of the envelope at FULL DEPTH, giving it
    an exterior gable wall for the overhead door and three exterior walls total.
    The remaining rectangle gets the classic residential band stack WITHOUT the
    utility: public on the south edge, hall(s), private on the north edge — so
    every habitable room reaches the perimeter and the route to the bedrooms never
    crosses the shop (no GARAGE_PASSTHROUGH). Returns ``None`` if no non-utility
    rooms remain to band (a utility-only program falls back to the plain stack).
    """
    from .elements import Room

    house = [s for s in specs if s.type not in _LARGE_UTILITY]
    if not house:
        return None

    bands = _build_bands(house, adj, notes)

    # The room that buffers the column from the house — the brief-declared neighbour
    # of a utility room that lives in the public band (a mudroom, ideally). It, and
    # the column, go on the SAME end so the shop doors straight into its buffer.
    util_ids = {s.id for s in utility}
    buffer_id = _column_buffer_room(bands, adj, util_ids)

    # Pick the column end deterministically. Prefer WEST (matches the barndo idiom
    # and the reference GOOD plan); the public band is then ordered so the buffer
    # room sits at that same (west) seam.
    on_west = True

    # Size the envelope. The house bands set the residential footprint (via the
    # same sizing the plain stack uses); the column adds its width beside them,
    # spanning the full depth. Keep total area consistent so rooms don't shrink.
    min_house_w = max((b.min_width for b in bands), default=feet(8))
    house_w, env_l = _choose_envelope(bands, brief, notes)
    if brief.envelope is not None:
        col_w = _column_width(utility, env_l)
        house_w = brief.envelope[0] - col_w
        if house_w < min_house_w:
            # A fixed envelope too narrow to seat the column beside the house
            # at its rooms' minimum widths: squeezing the bands would shrink
            # bedrooms/baths below code minimums (BEDROOM_DIM/BATH_CLEARANCE).
            # Fall back to the plain band stack, which keeps the full envelope
            # width for every band.
            return None
        env_w = float(brief.envelope[0])
    else:
        # Snap toward the 3-ft build module so the seed lands on buildable numbers
        # (clears ENVELOPE_MODULE and gives the LLM clean door/window arithmetic):
        # the envelope depth and the column width each snap to a 3-ft multiple, and
        # the house-block width absorbs the remainder to hit a 3-ft total width. The
        # bands/column still tile exactly, so area is conserved as room sizes flex.
        col_w = _snap_module(_column_width(utility, env_l), min_val=max(
            (u.min_dim for u in utility), default=feet(12)
        ))
        env_l = _snap_module(env_l, min_val=_min_stack_depth(bands))
        env_w = _snap_module(house_w + col_w, min_val=min_house_w + col_w)
        house_w = env_w - col_w
    env_w, env_l, house_w, col_w = (
        round(env_w, 2), round(env_l, 2), round(house_w, 2), round(col_w, 2)
    )

    # Place the house bands in the sub-rectangle beside the column.
    house_x0 = col_w if on_west else 0.0
    house_x1 = env_w if on_west else house_w
    _order_public_band_for_seam(bands, buffer_id, on_west)
    placed = _dimension_in(bands, house_x0, 0.0, house_x1, env_l)

    # Stack the utility rooms down the full-depth column (usually one room).
    ylines = _grid_lines(
        [max(u.area / max(col_w, 1e-6), u.min_dim) for u in utility], 0.0, env_l
    )
    cx0 = 0.0 if on_west else round(env_w - col_w, 2)
    cx1 = round(cx0 + col_w, 2)
    for i, u in enumerate(utility):
        y0, y1 = ylines[i], ylines[i + 1]
        placed[u.id] = Room(
            u.id, RoomType(u.type), cx0, y0, cx1 - cx0, y1 - y0, u.label, u.level
        )

    notes.append(
        f"Placed {'/'.join(u.id for u in utility)} as a full-depth "
        f"{'west' if on_west else 'east'} gable-end column (its own exterior "
        "gable wall for the overhead door)."
    )
    return _finalize(brief, specs, placed, env_w, env_l, notes)


def _column_buffer_room(
    bands: list[_Band], adj: dict[str, set[str]], util_ids: set[str]
) -> str | None:
    """The public-band room the brief wires to a utility room (the shop's buffer).

    Prefers a mudroom; otherwise any public-band room adjacent to a utility room.
    Returns its id, or ``None`` when nothing buffers the column (the shop will then
    fall back to a door onto the hall, never a bedroom — see ``_finalize``).
    """
    public_rooms = [r for b in bands for r in b.rooms if not b.interior_ok]
    buffered = [r for r in public_rooms if adj[r.id] & util_ids]
    if not buffered:
        return None
    buffered.sort(key=lambda r: (r.type is not RoomType.MUDROOM, r.id))
    return buffered[0].id


def _order_public_band_for_seam(
    bands: list[_Band], buffer_id: str | None, on_west: bool
) -> None:
    """Rotate the public band so the buffer room sits at the column seam.

    The column abuts the public (day-zone) band. Placing the buffer room (mudroom)
    at the seam end lets the shop door into it directly — a short, private route
    from the drive into the house. Reverses in place when the buffer isn't already
    at the seam end; a no-op when there's no buffer.
    """
    if buffer_id is None:
        return
    public = next((b for b in bands if not b.interior_ok), None)
    if public is None or len(public.rooms) < 2:
        return
    ids = [r.id for r in public.rooms]
    if buffer_id not in ids:
        return
    # West column -> buffer at the west (index 0) end; east column -> east end.
    at_seam = ids[0] if on_west else ids[-1]
    if at_seam != buffer_id:
        public.rooms.reverse()


def _column_width(utility: list[RoomSpec2], env_l: float) -> float:
    """Column width that seats the utility rooms at the full envelope depth.

    Area / depth, but never below the widest utility room's minimum dimension (a
    12-ft shop min keeps a vehicle bay usable). The column spans the full depth, so
    its area is width x depth; solving for the target total gives area / depth.
    """
    total_area = sum(u.area for u in utility)
    max_min = max((u.min_dim for u in utility), default=feet(12))
    return max(total_area / max(env_l, 1e-6), max_min)


#: The build module (ft) exterior dimensions snap to — matches
#: :data:`barndsl.validation.BUILD_MODULE`, so a snapped envelope clears
#: ENVELOPE_MODULE. Kept here (not imported) so layout2 stays free of validation.
_BUILD_MODULE = 3.0


def _snap_module(value: float, min_val: float = 0.0) -> float:
    """Round ``value`` to the nearest :data:`_BUILD_MODULE` multiple, >= ``min_val``.

    Snaps a raw dimension onto the 3-ft build module for buildable numbers, but
    never below ``min_val`` (a room minimum or the widest band) — it rounds *up*
    to the next module when the nearest one would violate the floor.
    """
    if value <= 0:
        return value
    snapped = round(value / _BUILD_MODULE) * _BUILD_MODULE
    while snapped + 1e-9 < min_val:
        snapped += _BUILD_MODULE
    return snapped


def _min_stack_depth(bands: list[_Band]) -> float:
    """The shallowest the house block can be: the sum of each band's tallest room.

    Every band must be at least as deep as its deepest room's minimum dimension,
    so the stack can't be shorter than their sum — the floor for snapping the
    envelope depth down onto the module.
    """
    return sum(b.max_min_dim for b in bands)


def _dimension_in(
    bands: list[_Band], x0: float, y0: float, x1: float, y1: float
) -> dict:
    """Tile ``bands`` south->north into the sub-rectangle ``[x0,x1] x [y0,y1]``.

    The band-stack analogue of :func:`_dimension`, but confined to a sub-rectangle
    (the house block beside a gable-end column) instead of the whole envelope, so
    band widths run ``x0..x1`` and heights fill ``y0..y1``. Coordinates come from
    shared grid lines so the dissection round-trips losslessly.
    """
    from .elements import Room

    span_w = x1 - x0
    raw_heights = [_band_height(b, span_w) for b in bands]
    total_raw = sum(raw_heights) or 1.0
    heights = [(y1 - y0) * h / total_raw for h in raw_heights]
    ylines = _grid_lines(heights, y0, y1)

    placed: dict[str, Room] = {}
    for bi, band in enumerate(bands):
        by0, by1 = ylines[bi], ylines[bi + 1]
        xlines = _grid_lines(_share_widths(band, span_w), x0, x1)
        for i, spec in enumerate(band.rooms):
            bx0, bx1 = xlines[i], xlines[i + 1]
            placed[spec.id] = Room(
                spec.id, RoomType(spec.type), bx0, by0, bx1 - bx0, by1 - by0,
                spec.label, spec.level,
            )
    return placed


def _finalize(
    brief: LayoutBrief2,
    specs: list[RoomSpec2],
    placed: dict,
    env_w: float,
    env_l: float,
    notes: list[str],
) -> LayoutResult:
    """Build the plan from placed rooms and add doors, connectivity and openings.

    Shared by every topology: the geometry differs, but turning it into a valid,
    reachable, glazed plan is identical.
    """
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
        if v1.entry_room is None:  # entry on a room connected to the rest
            v1.entry_room = _pick_connected_entry(plan)
        _add_openings(plan, v1, v1_by_id, notes)

    return LayoutResult(plan, satisfied, unsatisfied, notes)


def _build_bands(
    specs: list[RoomSpec2], adj: dict[str, set[str]], notes: list[str]
) -> list[_Band]:
    """Classify rooms and stack them south→north: utility, public, hall, private.

    The hall sits between the public core and the private rooms so it buffers
    day↔night and abuts both. The garage/shop band goes **outermost** on the south
    end (an exterior gable wall for its overhead door), never *between* the public
    and private bands — routing it between them would make the vehicle bay the only
    corridor from the living core to the bedrooms (the GARAGE_PASSTHROUGH defect the
    score now punishes). Large utility spaces still get their own band so their bulk
    doesn't dictate the depth of the bedroom row (a 1,200 sq ft shop sharing a band
    with bedrooms would stretch them long and thin). Rooms within a band are ordered
    so requested adjacencies fall between neighbours.
    """
    public_ids = {s.id for s in specs if s.type in _PUBLIC}
    hall_ids = {s.id for s in specs if s.type in _CIRCULATION}

    def is_public_band(s: RoomSpec2) -> bool:
        # Public rooms, plus service/office rooms that hang off the core (adjacent
        # to a public room and not to the hall) — keeps their adjacency and an
        # exterior wall. Bedrooms/baths always go to the private row off the hall.
        if s.type in _PUBLIC:
            return True
        if s.type in (RoomType.BEDROOM,) + _CIRCULATION + _LARGE_UTILITY:
            return False
        return bool(adj[s.id] & public_ids) and not (adj[s.id] & hall_ids)

    public = [s for s in specs if is_public_band(s)]
    halls = [s for s in specs if s.type in _CIRCULATION]
    utility = [s for s in specs if s.type in _LARGE_UTILITY]
    private = [
        s
        for s in specs
        if s not in public and s.type not in _CIRCULATION and s.type not in _LARGE_UTILITY
    ]

    # Band order (south→north): utility, public, hall(s), private.
    #
    # The utility (garage/shop) band goes OUTERMOST on the south end, not between
    # the public and private bands. Two reasons: (1) circulation — a utility band
    # wedged between day and night makes the vehicle bay the sole corridor from the
    # living core to the bedrooms (GARAGE_PASSTHROUGH); putting it on an end keeps
    # the hall as the day↔night buffer. (2) the shop's overhead door wants an
    # exterior gable wall, which an end band gives it. It abuts the *public* band
    # (never the private one), so the bedrooms stay a hall away from it. The private
    # band still takes the far (north) edge for daylight/egress; the public band,
    # now interior, keeps daylight through its east/west gable-end rooms (windows go
    # on any exterior wall) and through the openings added between its rooms.
    bands: list[_Band] = []
    if utility:  # garage/shop: outermost, exterior gable wall, off the public core
        bands.append(_Band(_order_in_band(utility, adj), interior_ok=True))
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

    Decompose the in-band adjacency graph into connected components and lay each
    out as a contiguous chain, walked from a proper **endpoint** (a degree-1 leaf)
    so a degree-2 room lands in the *middle* with both its neighbours abutting —
    e.g. a master flanked by its ensuite and walk-in comes out ``mbath·master·
    mcloset``, keeping both shared walls, rather than one neighbour stranded when
    the chain seeds at the wrong node. Components are concatenated in brief order;
    within a chain, ties break by (fewest remaining neighbours, brief order), so
    the walk stays deterministic and finishes each spur before moving on.
    """
    if len(rooms) <= 2:
        return rooms
    pool = {r.id: r for r in rooms}
    order_index = {r.id: i for i, r in enumerate(rooms)}
    within = {rid: (adj[rid] & pool.keys()) for rid in pool}

    ordered: list[str] = []
    seen: set[str] = set()
    # Lay out each connected component contiguously, seeding the walk at a true
    # endpoint (a component leaf) so an interior degree-2 room ends up flanked by
    # both its neighbours. Components are emitted in brief order; a branch (a hub
    # of degree >= 3, which a flat band can't fully seat) has its first-walked spur
    # continued and the leftover spur appended right after, keeping every room in
    # one contiguous run so the connectivity pass only ever bridges a single seam.
    for seed_id in sorted(pool, key=lambda r: order_index[r]):
        if seed_id in seen:
            continue
        comp = _component(seed_id, within)
        start = min(comp, key=lambda c: (len(within[c]), order_index[c]))
        # Greedy walks within this component until every member is placed. Each
        # walk stops at a dead end; if the component still has unplaced members
        # (a branch), the next walk resumes from the unplaced room nearest the
        # chain (fewest unseen neighbours), and its run is appended contiguously.
        pending: str | None = start
        while pending is not None:
            cur: str | None = pending
            while cur is not None:
                ordered.append(cur)
                seen.add(cur)
                nbrs = [n for n in within[cur] if n not in seen]
                cur = (
                    min(nbrs, key=lambda rid: (len(within[rid]), order_index[rid]))
                    if nbrs
                    else None
                )
            leftover = [c for c in comp if c not in seen]
            pending = (
                min(leftover, key=lambda c: (len(within[c]), order_index[c]))
                if leftover
                else None
            )
    return [pool[rid] for rid in ordered]


def _component(start: str, within: dict[str, set[str]]) -> set[str]:
    """The connected component of ``start`` in the in-band adjacency graph."""
    comp: set[str] = set()
    stack = [start]
    while stack:
        v = stack.pop()
        if v in comp:
            continue
        comp.add(v)
        stack.extend(within[v] - comp)
    return comp


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

    # Size to fit. The width sets every band's *depth* (area / width), and a
    # band's rooms are as deep as the band — so choose the width that gives the
    # bedroom/quality band a depth keeping its rooms reasonably square, rather
    # than a blind area×aspect guess that can leave bedrooms long and thin. Fall
    # back to the footprint-proportion heuristic when there's no such band.
    quality = _quality_band(bands)
    if quality is not None:
        env_w = quality.area / _target_depth(quality)
        # don't stray too far from a sane overall footprint
        base = math.sqrt(total_area * brief.aspect)
        env_w = min(max(env_w, 0.7 * base), 1.5 * base)
    else:
        env_w = math.sqrt(total_area * brief.aspect)
    env_w = max(env_w, min_w)
    env_l = sum(_band_height(b, env_w) for b in bands)
    return env_w, env_l


def _quality_band(bands: list[_Band]) -> _Band | None:
    """The exterior band whose room proportions matter most — the bedroom row.

    Picks the non-hall band with the most bedrooms (then the most habitable
    rooms); its depth drives the envelope width so those rooms come out square.
    """
    cands = [
        b
        for b in bands
        if not b.interior_ok and any(r.type in HABITABLE_TYPES for r in b.rooms)
    ]
    if not cands:
        return None
    return max(
        cands,
        key=lambda b: (
            sum(1 for r in b.rooms if r.type is RoomType.BEDROOM),
            sum(1 for r in b.rooms if r.type in HABITABLE_TYPES),
        ),
    )


def _target_depth(band: _Band) -> float:
    """A band depth that keeps its rooms square: the geometric mean of the extreme
    room areas (which minimises the worst room's aspect), clamped to a realistic
    residential range."""
    areas = [r.area for r in band.rooms if r.type in HABITABLE_TYPES] or [
        r.area for r in band.rooms
    ]
    depth = (min(areas) * max(areas)) ** 0.25
    return min(max(depth, feet(9.5)), feet(15.0))


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
                spec.id, RoomType(spec.type), x0, y0, x1 - x0, y1 - y0, spec.label, spec.level
            )
    return placed


def _share_widths(band: _Band, env_w: float) -> list[float]:
    """Split ``env_w`` among a band's rooms, **proportional to area**, respecting mins.

    A room's width should track its area (so, at the band's depth, it hits its
    target size and a sensible aspect). We start from the pure area-proportional
    width, floor each at its minimum dimension, then settle the small surplus or
    deficit back onto the rooms that have slack — so widths sum to ``env_w`` and
    no room dips below its minimum. (The old "min + area-share of slack" base made
    bedrooms narrower than their area warranted, stretching them thin.)
    """
    rooms = band.rooms
    mins = [r.min_dim for r in rooms]
    if sum(mins) >= env_w:  # envelope too narrow for the mins — scale them down
        scale = env_w / (sum(mins) or 1.0)
        return [m * scale for m in mins]
    area_total = band.area or 1.0
    w = [max(env_w * r.area / area_total, m) for r, m in zip(rooms, mins)]
    for _ in range(12):
        diff = sum(w) - env_w
        if abs(diff) < 1e-7:
            break
        if diff > 0:  # over budget — shrink rooms that sit above their minimum
            slack = [wi - m for wi, m in zip(w, mins)]
            total_slack = sum(slack)
            if total_slack <= 1e-9:
                break
            w = [
                max(wi - diff * (s / total_slack), m)
                for wi, s, m in zip(w, slack, mins)
            ]
        else:  # under budget — grow rooms proportional to area
            w = [wi + (-diff) * (r.area / area_total) for wi, r in zip(w, rooms)]
    return w


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


# --- topology 2: recursive slicing ------------------------------------------


@dataclass
class _Slice:
    """A slicing-tree node: a leaf room, or a cut with two children."""

    spec: RoomSpec2 | None = None
    a: "_Slice | None" = None
    b: "_Slice | None" = None
    area: float = 0.0
    min_dim: float = 0.0


def _solve_slice(
    brief: LayoutBrief2, specs: list[RoomSpec2], adj: dict[str, set[str]]
) -> LayoutResult:
    """Topology 2: a recursive adjacency-ordered slicing tree.

    Order the rooms so adjacent ones are close (Cuthill–McKee), recursively split
    that order into balanced halves to form a slicing tree, then carve the
    envelope top-down — cutting each rectangle along its longer side so rooms stay
    reasonably square. Unlike bands this can give a room three neighbours, but it
    offers no perimeter guarantee, so ``auto`` only keeps it when it scores well.
    """
    total = sum(s.area for s in specs)
    if brief.envelope is not None:
        env_w, env_l = float(brief.envelope[0]), float(brief.envelope[1])
    else:
        env_w = math.sqrt(total * brief.aspect)
        env_l = total / env_w if env_w else total
    env_w, env_l = round(env_w, 2), round(env_l, 2)

    tree = _build_slice_tree(_cuthill_mckee(specs, adj))
    placed: dict = {}
    _carve(tree, 0.0, 0.0, env_w, env_l, placed)
    return _finalize(brief, specs, placed, env_w, env_l, [])


def _cuthill_mckee(specs: list[RoomSpec2], adj: dict[str, set[str]]) -> list[RoomSpec2]:
    """Order rooms so adjacent ones get nearby indices (small graph bandwidth).

    A breadth-first sweep from a lowest-degree room, visiting neighbours in
    increasing degree. Keeps adjacent rooms close, so the contiguous splits below
    tend to realise their adjacencies as shared walls. Deterministic.
    """
    ids = [s.id for s in specs]
    by_id = {s.id: s for s in specs}
    pos = {i: k for k, i in enumerate(ids)}
    nbr = {i: adj[i] & set(ids) for i in ids}
    deg = {i: len(nbr[i]) for i in ids}
    order: list[str] = []
    seen: set[str] = set()
    while len(order) < len(ids):
        start = min((i for i in ids if i not in seen), key=lambda i: (deg[i], pos[i]))
        queue = [start]
        seen.add(start)
        while queue:
            v = queue.pop(0)
            order.append(v)
            for n in sorted(nbr[v], key=lambda j: (deg[j], pos[j])):
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
    return [by_id[i] for i in order]


def _build_slice_tree(seq: list[RoomSpec2]) -> _Slice:
    """Recursively split an ordered room list into a balanced slicing tree."""
    if len(seq) == 1:
        s = seq[0]
        return _Slice(spec=s, area=s.area, min_dim=s.min_dim)
    total = sum(s.area for s in seq)
    cum, best = 0.0, (1e18, 1)
    for k in range(1, len(seq)):
        cum += seq[k - 1].area
        gap = abs(cum - total / 2.0)
        if gap < best[0]:
            best = (gap, k)
    k = best[1]
    a = _build_slice_tree(seq[:k])
    b = _build_slice_tree(seq[k:])
    return _Slice(a=a, b=b, area=total, min_dim=max(a.min_dim, b.min_dim))


def _carve(node: _Slice, x0: float, y0: float, x1: float, y1: float, placed: dict) -> None:
    """Carve ``node`` into the rectangle, cutting along the rectangle's long side."""
    from .elements import Room

    if node.spec is not None:
        rx0, ry0 = round(x0, 2), round(y0, 2)
        placed[node.spec.id] = Room(
            node.spec.id, RoomType(node.spec.type), rx0, ry0,
            round(x1, 2) - rx0, round(y1, 2) - ry0, node.spec.label, node.spec.level,
        )
        return
    # A non-leaf slice always has both children (set together in the tree build).
    assert node.a is not None and node.b is not None
    frac = node.a.area / node.area if node.area else 0.5
    if (x1 - x0) >= (y1 - y0):  # vertical cut, side by side
        cut = _split_at(x0, x1, frac, node.a.min_dim, node.b.min_dim)
        _carve(node.a, x0, y0, cut, y1, placed)
        _carve(node.b, cut, y0, x1, y1, placed)
    else:  # horizontal cut, stacked
        cut = _split_at(y0, y1, frac, node.a.min_dim, node.b.min_dim)
        _carve(node.a, x0, y0, x1, cut, placed)
        _carve(node.b, x0, cut, x1, y1, placed)


def _split_at(lo: float, hi: float, frac: float, min_a: float, min_b: float) -> float:
    """A cut between ``lo`` and ``hi`` at ``frac``, keeping both sides non-trivial.

    Honors each side's minimum extent where the span allows; if it's too short for
    both minimums, falls back to a proportional split (and the thin room shows up
    as a min-dimension issue, which the scorer penalises).
    """
    span = hi - lo
    cut = lo + span * frac
    floor, ceil = lo + min_a, hi - min_b
    if floor <= ceil:
        cut = min(max(cut, floor), ceil)
    else:  # too short for both minimums — keep a sliver each so nothing degenerates
        cut = min(max(cut, lo + 0.5), hi - 0.5) if span > 1.0 else lo + span / 2.0
    return round(cut, 2)


# --- topology 3: rectangular dual (general, non-sliceable adjacency graphs) --
#
# The bands and slice topologies cannot realise an arbitrary required-adjacency
# graph as shared walls — the canonical counter-example is the *pinwheel*: a
# centre room that must touch four others, which no single band row and no
# slicing tree can seat. The rectangular-dual topology can: it dissects the
# rectangle so that every required adjacency is a real shared wall.
#
# We build it by directly searching for an integer *rectangulation* — a tiling of
# a small structural grid by one rectangle per room — with the classic "fill the
# lowest-leftmost empty cell" exact-tiling backtracker, keeping only tilings where
# every required adjacency is a shared wall and every daylight/egress room sits on
# the boundary. This is the floor-plan use of a rectangular dual; because a floor
# plan tolerates *extra* shared walls (we simply don't cut a door there), we need
# the required edges to be a **subset** of the realised contacts, which is weaker
# and easier than a strict dual yet still covers the non-sliceable cases.
#
# The search is correct by construction — its acceptance test *is* the spec (tile,
# no overlap, required ⊆ shared walls, perimeter) — so it needs none of the
# planar-embedding / regular-edge-labeling machinery a strict dual constructor
# would, which is far harder to get right in pure Python. The chosen topology is
# then dimensioned to the size program and, like every topology, only kept by
# ``auto`` when it scores best; when no tiling is found it returns ``None`` and
# ``auto`` falls back. See ``docs/design/AUTO_LAYOUT_2.md`` §7.4.

#: Above this room count the rectangulation search is skipped (its grids grow as
#: n²); bands/slice still handle larger programs. Barndos sit well under this.
_DUAL_MAX_ROOMS = 9
#: Backtracking-node budget across all grids before the search gives up (→ None).
_DUAL_NODE_CAP = 200_000
#: Stop collecting once this many distinct valid topologies are found.
_DUAL_WANT = 240
#: Dimension and fully score only this many best-by-proxy topologies.
_DUAL_SCORE_TOP = 16


def _rectangulations(
    n: int,
    grid_w: int,
    grid_h: int,
    perimeter: set[int],
    node_cap: int,
    want: int,
) -> tuple[list, int]:
    """Enumerate integer rectangulations of a ``grid_w × grid_h`` grid into ``n`` rooms.

    Each solution is a list of ``(cxl, cyb, cxr, cyt)`` cell rectangles, one per
    room index. We repeatedly fill the lowest-leftmost empty cell (which must be
    some room's bottom-left corner) with an unplaced room of some extent, so the
    tiling is gap-free by construction; perimeter-needing rooms are pruned the
    moment they'd land fully interior. Deterministic order.
    """
    cover = [[-1] * grid_w for _ in range(grid_h)]
    rect: list = [None] * n
    placed = [False] * n
    nodes = [0]
    out: list = []

    def lowest_leftmost():
        for r in range(grid_h):
            for c in range(grid_w):
                if cover[r][c] == -1:
                    return r, c
        return None

    def max_width(r, c):
        w = 0
        while c + w < grid_w and cover[r][c + w] == -1:
            w += 1
        return w

    def block_free(r, c, w, h):
        if r + h > grid_h or c + w > grid_w:
            return False
        for rr in range(r, r + h):
            for cc in range(c, c + w):
                if cover[rr][cc] != -1:
                    return False
        return True

    def recurse():
        if len(out) >= want or nodes[0] > node_cap:
            return
        nodes[0] += 1
        cell = lowest_leftmost()
        if cell is None:
            if all(placed):
                out.append([rect[i] for i in range(n)])
            return
        r, c = cell
        mw = max_width(r, c)
        for i in range(n):
            if placed[i]:
                continue
            for w in range(1, mw + 1):
                for h in range(1, grid_h - r + 1):
                    if not block_free(r, c, w, h):
                        break  # taller blocks at this width can only stay blocked
                    on_perim = c == 0 or r == 0 or c + w == grid_w or r + h == grid_h
                    if i in perimeter and not on_perim:
                        continue
                    for rr in range(r, r + h):
                        for cc in range(c, c + w):
                            cover[rr][cc] = i
                    rect[i] = (c, r, c + w, r + h)
                    placed[i] = True
                    recurse()
                    placed[i] = False
                    rect[i] = None
                    for rr in range(r, r + h):
                        for cc in range(c, c + w):
                            cover[rr][cc] = -1
                    if len(out) >= want or nodes[0] > node_cap:
                        return

    recurse()
    return out, nodes[0]


def _struct_shared(rects: list) -> set:
    """Pairs ``(i, j)`` (``i < j``) whose structural rectangles share a wall segment."""
    adj: set = set()
    n = len(rects)
    for i in range(n):
        xl, yb, xr, yt = rects[i]
        for j in range(i + 1, n):
            xl2, yb2, xr2, yt2 = rects[j]
            if (xr == xl2 or xr2 == xl) and min(yt, yt2) > max(yb, yb2):
                adj.add((i, j))
            elif (yt == yb2 or yt2 == yb) and min(xr, xr2) > max(xl, xl2):
                adj.add((i, j))
    return adj


def _dual_topologies(
    n: int, perimeter: set[int], required: set[tuple[int, int]]
) -> list:
    """Valid structural rectangulations: ``required ⊆ shared walls`` and on-perimeter.

    Searches near-square grids first (good aspect, and pinwheels live there),
    deduplicating across grid sizes, up to the node/solution budget.
    """
    cell_cap = 3 * n + 2
    grids = [
        (gw, gh)
        for gw in range(1, n + 1)
        for gh in range(1, n + 1)
        if n <= gw * gh <= cell_cap
    ]
    grids.sort(key=lambda g: (abs(g[0] - g[1]), g[0] * g[1], g))

    sols: list = []
    seen: set = set()
    nodes = 0
    for gw, gh in grids:
        raw, used = _rectangulations(
            n, gw, gh, perimeter, _DUAL_NODE_CAP - nodes, _DUAL_WANT
        )
        nodes += used
        for rects in raw:
            if not required.issubset(_struct_shared(rects)):
                continue
            key = tuple(rects)
            if key not in seen:
                seen.add(key)
                sols.append(rects)
        if nodes > _DUAL_NODE_CAP or len(sols) >= _DUAL_WANT:
            break
    return sols


def _dimension_dual(
    rects: list, specs: list[RoomSpec2], env_w: float, env_l: float, iters: int = 60
) -> dict:
    """Size a structural topology to the area program via iterative proportional fitting.

    The structural grid fixes which rooms share each cut line; we assign real
    positions to those lines so each room's width×height approaches its target
    area while meeting its minimum dimension. Column widths and row heights are
    nudged alternately toward the per-room targets, then renormalised to fill the
    envelope — a few sweeps converge. Cut lines snap to the grid so shared walls
    survive the DSL round-trip (see ``_grid_lines``).
    """
    from .elements import Room

    n = len(rects)
    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    ys = sorted({r[1] for r in rects} | {r[3] for r in rects})
    xi = {v: k for k, v in enumerate(xs)}
    yi = {v: k for k, v in enumerate(ys)}
    ncol, nrow = len(xs) - 1, len(ys) - 1
    cols = [list(range(xi[r[0]], xi[r[2]])) for r in rects]
    rows = [list(range(yi[r[1]], yi[r[3]])) for r in rects]
    area = [s.area for s in specs]
    mind = [s.min_dim for s in specs]

    cw = [env_w / ncol] * ncol
    rh = [env_l / nrow] * nrow

    def renorm(vals, total):
        s = sum(vals) or 1.0
        return [v * total / s for v in vals]

    for _ in range(iters):
        rw = [sum(cw[c] for c in cols[i]) for i in range(n)]
        rha = [sum(rh[c] for c in rows[i]) for i in range(n)]
        scale = [1.0] * ncol
        wsum = [0.0] * ncol
        for i in range(n):
            want = max(area[i] / rha[i] if rha[i] else rw[i], mind[i])
            ratio = want / rw[i] if rw[i] else 1.0
            for c in cols[i]:
                scale[c] += ratio
                wsum[c] += 1
        cw = renorm([cw[c] * (scale[c] / (wsum[c] + 1)) for c in range(ncol)], env_w)
        rw = [sum(cw[c] for c in cols[i]) for i in range(n)]
        scale = [1.0] * nrow
        hsum = [0.0] * nrow
        for i in range(n):
            want = max(area[i] / rw[i] if rw[i] else rha[i], mind[i])
            ratio = want / rha[i] if rha[i] else 1.0
            for c in rows[i]:
                scale[c] += ratio
                hsum[c] += 1
        rh = renorm([rh[c] * (scale[c] / (hsum[c] + 1)) for c in range(nrow)], env_l)

    X = _grid_lines(cw, 0.0, env_w)
    Y = _grid_lines(rh, 0.0, env_l)
    placed: dict = {}
    for i, s in enumerate(specs):
        x0, x1 = X[xi[rects[i][0]]], X[xi[rects[i][2]]]
        y0, y1 = Y[yi[rects[i][1]]], Y[yi[rects[i][3]]]
        placed[s.id] = Room(
            s.id, RoomType(s.type), x0, y0, round(x1 - x0, 2), round(y1 - y0, 2), s.label, s.level
        )
    return placed


def _solve_dual(
    brief: LayoutBrief2, specs: list[RoomSpec2], adj: dict[str, set[str]]
) -> LayoutResult | None:
    """Topology 3: rectangular dual. Returns ``None`` when no tiling realises the brief.

    Finds structural rectangulations honouring the required adjacencies and
    perimeter needs, then dimensions and fully scores the most promising ones
    (ranked by how well their cell shares match the area program) and keeps the
    best. The result is verified downstream by ``_score`` like any candidate.
    """
    n = len(specs)
    if n > _DUAL_MAX_ROOMS:
        return None
    idx = {s.id: i for i, s in enumerate(specs)}
    required = {
        (min(idx[a], idx[b]), max(idx[a], idx[b])) for a in adj for b in adj[a]
    }
    perimeter = {i for i, s in enumerate(specs) if s.type in HABITABLE_TYPES}
    sols = _dual_topologies(n, perimeter, required)
    if not sols:
        return None

    total_area = sum(s.area for s in specs)

    def proxy(rects: list) -> float:
        cells = [(r[2] - r[0]) * (r[3] - r[1]) for r in rects]
        tcells = sum(cells) or 1.0
        return sum(
            abs(cells[i] / tcells - specs[i].area / total_area) for i in range(n)
        )

    sols.sort(key=lambda r: (proxy(r), tuple(r)))

    if brief.envelope is not None:
        env_w, env_l = round(float(brief.envelope[0]), 2), round(float(brief.envelope[1]), 2)
    else:
        env_w = round(math.sqrt(total_area * brief.aspect), 2)
        env_l = round(total_area / env_w, 2) if env_w else round(total_area, 2)

    best: tuple | None = None
    for rects in sols[:_DUAL_SCORE_TOP]:
        placed = _dimension_dual(rects, specs, env_w, env_l)
        result = _finalize(brief, specs, placed, env_w, env_l, [])
        sc = _score(result)
        if best is None or sc < best[0]:
            best = (sc, result)
    return best[1] if best else None


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

    pref = {
        RoomType.FOYER: 0,
        RoomType.MUDROOM: 1,
        RoomType.GREAT_ROOM: 2,
        RoomType.LIVING: 3,
        RoomType.KITCHEN: 4,
        RoomType.DINING: 5,
    }
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
    (connect ``<a>`` to each of the rest). The non-room statements are parsed by
    the shared :func:`barndsl.layout.parse_brief_fields`; only the room line
    (a size *program*) is v2-specific.
    """
    return LayoutBrief2(**parse_brief_fields(text, name, _parse_room2))


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
        min_dim = 0.0
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
