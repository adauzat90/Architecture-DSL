"""Plumbing fixtures and kitchen appliances — seeds + a clearance check.

A barndsl room is an empty rectangle; a *residential* room earns its keep by the
fixtures in it. This module derives the standard fixtures a wet room or kitchen
needs — deterministically, no Revit, no API key — as **seed footprints** the
Revit builder can drop loadable families onto, and it answers a plain question
the validator asks: *do the fixtures fit with code clearances?*

Sizes are nominal residential fixtures (feet); the clearances follow IRC R307
(a water closet wants 15 in from its centreline to any wall/fixture and 21 in of
clear floor in front) and common kitchen practice (a ~40 in working aisle). Like
the frame and the roof, this is a **layout aid**, not a fixture-approval — the
final layout is the designer's.
"""

from __future__ import annotations

from dataclasses import dataclass

from .elements import Barndominium, Room, RoomType
from .validation import clear_box


@dataclass(frozen=True)
class FixtureSpec:
    """A fixture's footprint: ``width`` runs along the wall it backs to, ``depth``
    projects into the room, and ``front`` is the clear floor it needs in front
    (measured from the fixture face into the room).

    ``free`` marks a **free-standing** piece — a dining table, a coffee table, an
    island — which sits in the open floor rather than backing to a wall, so it
    auto-places at the room centre instead of walking the perimeter. ``resizable``
    marks a run whose ``width`` is only nominal (a counter is any length), so an
    explicit ``width`` override is expected, not a warning.
    """

    kind: str
    width: float
    depth: float
    front: float
    free: bool = False
    resizable: bool = False


#: The standard plumbing/appliance fixtures, in the order they're laid along the
#: wall. Sizes are nominal residential (feet); ``front`` follows the IRC/practice
#: clearances documented on the module.
FIXTURES: dict[str, FixtureSpec] = {
    # --- plumbing & kitchen appliances (auto-seeded by room type) -------------
    "toilet": FixtureSpec("toilet", 2.5, 2.33, 1.75),  # 30 in bay (15+15), 28 in deep, 21 in front
    "lavatory": FixtureSpec("lavatory", 2.0, 1.83, 1.75),  # 24x22 vanity
    "tub": FixtureSpec("tub", 5.0, 2.5, 1.75),  # 60x30 tub
    "shower": FixtureSpec("shower", 2.67, 2.67, 1.75),  # 32x32 stall
    "sink": FixtureSpec("sink", 2.5, 2.0, 3.5),  # base cabinet + 42 in aisle
    "range": FixtureSpec("range", 2.5, 2.0, 3.5),
    "refrigerator": FixtureSpec("refrigerator", 3.0, 2.5, 3.5),
    # --- furniture & equipment (placed by the author, never auto-seeded except
    #     washer/dryer in a laundry) -------------------------------------------
    "bed_queen": FixtureSpec("bed_queen", 5.0, 6.67, 2.0),  # 60x80 mattress + walk-around
    "bed_twin": FixtureSpec("bed_twin", 3.25, 6.25, 2.0),  # 39x75 mattress
    "sofa": FixtureSpec("sofa", 7.0, 3.0, 2.5),
    "armchair": FixtureSpec("armchair", 3.0, 3.0, 1.5),
    "dining_table": FixtureSpec("dining_table", 6.0, 3.33, 3.0, free=True),  # 30 in chair pull all round
    "coffee_table": FixtureSpec("coffee_table", 4.0, 2.0, 1.5, free=True),
    "desk": FixtureSpec("desk", 4.0, 2.0, 3.0),
    "dresser": FixtureSpec("dresser", 5.0, 1.67, 3.0),
    "washer": FixtureSpec("washer", 2.25, 2.25, 3.0),
    "dryer": FixtureSpec("dryer", 2.25, 2.25, 3.0),
    "water_heater": FixtureSpec("water_heater", 2.0, 2.0, 1.5),
    "kitchen_island": FixtureSpec("kitchen_island", 6.0, 3.0, 3.5, free=True),
    "counter": FixtureSpec("counter", 6.0, 2.0, 3.5, resizable=True),  # 2 ft deep, any run
    "wardrobe": FixtureSpec("wardrobe", 4.0, 2.0, 3.0),
}

#: Every catalog kind, for the parser (a `fixture <kind>` must name one of these).
FIXTURE_KINDS: tuple[str, ...] = tuple(FIXTURES)

#: Which fixtures each room type auto-seeds. Furniture is a deliberate authoring
#: act, so bedrooms/living rooms stay unseeded; a laundry seeds its washer + dryer
#: because those are the room's whole reason to exist.
_ROOM_FIXTURES: dict[RoomType, list[str]] = {
    RoomType.BATHROOM: ["toilet", "lavatory", "tub"],
    RoomType.HALF_BATH: ["toilet", "lavatory"],
    RoomType.KITCHEN: ["refrigerator", "range", "sink"],
    RoomType.LAUNDRY: ["washer", "dryer"],
}


def fixtures_for(room_type: RoomType) -> list[str]:
    """The fixture kinds a room of this type should carry (empty if none)."""
    return list(_ROOM_FIXTURES.get(room_type, []))


@dataclass
class Fixture:
    """A placed fixture: its footprint rectangle in **world** feet (south-west
    corner ``(x, y)``), the wall it backs to, and its kind.

    ``rotation`` is the plan-clockwise quarter-turn applied (0/90/180/270); the
    footprint ``width``/``length`` already reflect it, so consumers read the box
    as-is. ``seed`` is True for an auto-placed fixture and False for one the author
    placed with a ``fixture`` statement; ``source_line`` is that statement's 1-based
    line (``None`` for a seed). ``id`` is a stable ``<room>~<kind>~<i>`` handle the
    plan renderer, the exchange and the edit engine all agree on.
    """

    kind: str
    x: float
    y: float
    width: float
    length: float
    wall: str  # S | N | E | W, or "" for a free-standing piece
    rotation: float = 0.0
    seed: bool = True
    source_line: int | None = None
    id: str | None = None

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.length / 2.0)


# Perimeter walk of the clear box, as (wall, run-length) starting on the longer
# wall so the widest fixtures get the most room; fixtures wrap around the corner.
def _walls(cw: float, cl: float):
    if cw >= cl:
        return [("S", cw), ("E", cl), ("N", cw), ("W", cl)]
    return [("W", cl), ("N", cw), ("E", cl), ("S", cw)]


def _wall_rect(wall: str, x0: float, y0: float, cw: float, cl: float, cursor: float,
               width: float, depth: float):
    """The world rectangle for a ``width`` × ``depth`` fixture at ``cursor`` feet
    along ``wall`` (``width`` runs along the wall, ``depth`` into the room)."""
    if wall == "S":
        return x0 + cursor, y0, width, min(depth, cl)
    if wall == "N":
        d = min(depth, cl)
        return x0 + cursor, y0 + cl - d, width, d
    if wall == "W":
        return x0, y0 + cursor, min(depth, cw), width
    d = min(depth, cw)  # E
    return x0 + cw - d, y0 + cursor, d, width


def _rects_overlap(a, b, tol: float = 1e-6) -> bool:
    ax, ay, aw, al = a
    bx, by, bw, bl = b
    dx = min(ax + aw, bx + bw) - max(ax, bx)
    dy = min(ay + al, by + bl) - max(ay, by)
    return dx > tol and dy > tol


def _place_perimeter(
    x0: float, y0: float, cw: float, cl: float, kinds: list[str]
) -> list[Fixture]:
    """Lay ``kinds`` along the clear-box perimeter (longer wall first), wrapping to
    the next wall when the current one runs out — the deterministic seed layout."""
    walls = _walls(cw, cl)
    placed: list[Fixture] = []
    wi = 0
    cursor = 0.0
    for kind in kinds:
        spec = FIXTURES[kind]
        while wi < len(walls):
            _, run = walls[wi]
            if cursor + spec.width <= run + 1e-9:
                break
            wi += 1
            cursor = 0.0
        if wi >= len(walls):
            break  # ran out of perimeter; the fit check reports the shortfall
        wall = walls[wi][0]
        fx, fy, fw, fl = _wall_rect(wall, x0, y0, cw, cl, cursor, spec.width, spec.depth)
        placed.append(Fixture(kind, fx, fy, fw, fl, wall))
        cursor += spec.width
    return placed


def plan_room_fixtures(plan: Barndominium, room: Room) -> list[Fixture]:
    """Place ``room``'s **auto-seed** fixtures against its walls (deterministic).

    The historical seed placement (bath/kitchen/laundry), kept for callers that
    only want the auto-seeds. The combined authored-plus-seed layout the exchange,
    the plan drawing and the 3D model consume is :func:`resolve_room_fixtures`.
    """
    kinds = fixtures_for(room.type)
    if not kinds:
        return []
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []
    return _place_perimeter(x0, y0, cw, cl, kinds)


def _quarter_turns(rotation: float) -> int:
    """A rotation in degrees snapped to a plan quarter-turn count (0..3). The
    massing is axis-aligned, so a fixture turns in 90° steps; other angles snap to
    the nearest, keeping the plan glyph and the 3D box consistent."""
    return int(round((rotation or 0.0) / 90.0)) % 4


def _place_explicit(
    room: Room, pf, x0: float, y0: float, cw: float, cl: float, occupied: list
) -> Fixture:
    """Resolve one authored :class:`~barndsl.elements.PlacedFixture` to a world
    :class:`Fixture`, honouring its ``at``/``wall``/``rotate`` (or auto-placing)."""
    spec = FIXTURES[pf.kind]
    width = float(pf.width) if getattr(pf, "width", None) else spec.width
    depth = spec.depth
    if _quarter_turns(getattr(pf, "rotation", 0.0)) % 2 == 1:
        width, depth = depth, width  # a quarter turn swaps the footprint axes
    wall = pf.wall.name[0] if getattr(pf, "wall", None) is not None else ""

    if pf.x is not None and pf.y is not None:
        # `at x,y` is room-local (offset from the room's SW corner).
        fx, fy = room.x + float(pf.x), room.y + float(pf.y)
        return Fixture(pf.kind, fx, fy, width, depth, wall or "S", rotation=pf.rotation)

    if wall:
        # Auto-place against the named wall: first free slot along its run.
        rx, ry, rw, rl = _first_free_on_wall(x0, y0, cw, cl, wall, width, depth, occupied)
        return Fixture(pf.kind, rx, ry, rw, rl, wall, rotation=pf.rotation)

    if spec.free:
        # Free-standing with no anchor: centre it in the clear box.
        fx = x0 + max(0.0, (cw - width) / 2.0)
        fy = y0 + max(0.0, (cl - depth) / 2.0)
        return Fixture(pf.kind, fx, fy, width, depth, "", rotation=pf.rotation)

    # No anchor: first free spot walking the perimeter (longer wall first).
    for w, _run in _walls(cw, cl):
        rect = _first_free_on_wall(x0, y0, cw, cl, w, width, depth, occupied, give_up=True)
        if rect is not None:
            rx, ry, rw, rl = rect
            return Fixture(pf.kind, rx, ry, rw, rl, w, rotation=pf.rotation)
    return Fixture(pf.kind, x0, y0, width, depth, "S", rotation=pf.rotation)


def _first_free_on_wall(
    x0, y0, cw, cl, wall, width, depth, occupied, give_up: bool = False
):
    """First 0.25-ft cursor on ``wall`` whose footprint clears ``occupied``; the
    run start if none is free (or ``None`` when ``give_up`` and it never fits)."""
    run = cw if wall in ("S", "N") else cl
    cursor = 0.0
    first = None
    while cursor + width <= run + 1e-9:
        rect = _wall_rect(wall, x0, y0, cw, cl, cursor, width, depth)
        if first is None:
            first = rect
        if not any(_rects_overlap(rect, o) for o in occupied):
            return rect
        cursor += 0.25
    if give_up:
        return None
    return first if first is not None else _wall_rect(wall, x0, y0, cw, cl, 0.0, width, depth)


def resolve_room_fixtures(plan: Barndominium, room: Room) -> list[Fixture]:
    """The final fixtures in ``room``: surviving auto-seeds plus authored fixtures.

    Add-vs-replace rule: authored fixtures **add** to the room's auto-seeds, except
    that an authored fixture of a *seeded* kind **replaces** that kind's seed (an
    explicit ``toilet`` moves the toilet; the lavatory/tub seeds stay). Seeds are
    laid first (perimeter walk), then authored fixtures fill the first free slot
    that clears what's already down. Every fixture gets a stable
    ``<room>~<kind>~<i>`` id. Deterministic and side-effect-free.
    """
    explicit = [pf for pf in getattr(plan, "fixtures", []) if pf.room == room.id]
    x0, y0, cw, cl = clear_box(plan, room)
    if cw <= 0 or cl <= 0:
        return []

    explicit_kinds = {pf.kind for pf in explicit}
    surviving = [k for k in fixtures_for(room.type) if k not in explicit_kinds]
    placed = _place_perimeter(x0, y0, cw, cl, surviving)

    occupied = [(f.x, f.y, f.width, f.length) for f in placed]
    for pf in explicit:
        if pf.kind not in FIXTURES:
            continue
        f = _place_explicit(room, pf, x0, y0, cw, cl, occupied)
        f.seed = False
        f.source_line = getattr(pf, "line", None)
        placed.append(f)
        occupied.append((f.x, f.y, f.width, f.length))

    counts: dict[str, int] = {}
    for f in placed:
        i = counts.get(f.kind, 0)
        counts[f.kind] = i + 1
        f.id = f"{room.id}~{f.kind}~{i}"
    return placed


def validate_fixtures(plan: Barndominium, add) -> None:
    """Compiler-as-teacher checks on authored fixtures (all non-blocking).

    Warns when an authored fixture names an unknown room (``FIXTURE_ROOM``), when
    its footprint leaves the room's clear box (``FIXTURE_OOB``), or when it overlaps
    another fixture (``FIXTURE_OVERLAP``); an info when it lands in a door's swing
    (``FIXTURE_DOOR``). Seeds are auto-fitted, so only authored fixtures are judged.
    """
    from .validation import Issue, Severity  # local: validation imports this module

    known = {r.id for r in plan.rooms}
    for pf in getattr(plan, "fixtures", []):
        if pf.room not in known:
            add(
                Issue(
                    Severity.ERROR,
                    "FIXTURE_ROOM",
                    f"Fixture '{pf.kind}' names unknown room '{pf.room}'.",
                    line=getattr(pf, "line", None),
                    col=getattr(pf, "col", None),
                    end_col=getattr(pf, "end_col", None),
                    hint="Place the fixture in a room that exists.",
                )
            )

    for room in plan.rooms:
        fixtures = resolve_room_fixtures(plan, room)
        # OOB is judged against the room rectangle (not the tighter clear box) so a
        # natural corner placement (`at 0,0`) doesn't warn on the wall inset alone.
        rects = [(f.x, f.y, f.width, f.length) for f in fixtures]
        for i, f in enumerate(fixtures):
            if f.seed:
                continue
            if (
                f.x + 1e-6 < room.x
                or f.y + 1e-6 < room.y
                or f.x + f.width - 1e-6 > room.x2
                or f.y + f.length - 1e-6 > room.y2
            ):
                add(
                    Issue(
                        Severity.WARNING,
                        "FIXTURE_OOB",
                        f"Fixture '{f.kind}' in '{room.id}' extends past the room "
                        f"({_fmt(room.width)} × {_fmt(room.length)} ft).",
                        room=room.id,
                        line=f.source_line,
                        hint="Move it inward (its `at` is measured from the room's "
                        "SW corner) or grow the room.",
                    )
                )
            for j, other in enumerate(fixtures):
                if j <= i:
                    continue
                if _rects_overlap(rects[i], rects[j]):
                    add(
                        Issue(
                            Severity.WARNING,
                            "FIXTURE_OVERLAP",
                            f"Fixture '{f.kind}' overlaps '{other.kind}' in "
                            f"'{room.id}'.",
                            room=room.id,
                            line=f.source_line,
                            hint="Nudge one along its wall, or place it against a "
                            "different wall.",
                        )
                    )
                    break
            for door in _door_swing_rects(plan, room):
                if _rects_overlap(rects[i], door):
                    add(
                        Issue(
                            Severity.INFO,
                            "FIXTURE_DOOR",
                            f"Fixture '{f.kind}' in '{room.id}' sits in a door's "
                            "swing.",
                            room=room.id,
                            line=f.source_line,
                            hint="Keep the clear floor in front of the door open; "
                            "slide the fixture clear of the swing.",
                        )
                    )
                    break


def _fmt(v: float) -> str:
    return f"{v:g}"


def _door_swing_rects(plan: Barndominium, room: Room) -> list:
    """Coarse swing-clearance rectangles for the leaves opening into ``room`` — a
    width-deep band inside each hinged interior door on one of the room's walls."""
    from .geometry import shared_edge

    out = []
    for d in plan.interior_doors:
        if getattr(d, "kind", "swing") not in ("swing", "double", "french"):
            continue
        if room.id not in (d.room_a, d.room_b):
            continue
        other_id = d.room_b if d.room_a == room.id else d.room_a
        other = plan.room(other_id)
        if other is None:
            continue
        edge = shared_edge(room, other)
        if edge is None:
            continue
        w = min(d.width, edge.length)
        offset = d.offset if d.offset is not None else max(0.0, (edge.length - w) / 2.0)
        start = edge.lo + max(0.0, min(offset, edge.length - w))
        if edge.orientation == "v":  # wall runs north-south at x = edge.pos
            inward = edge.pos < room.center[0]
            bx = edge.pos if inward else edge.pos - w
            out.append((bx, start, w, w))
        else:  # wall runs east-west at y = edge.pos
            inward = edge.pos < room.center[1]
            by = edge.pos if inward else edge.pos - w
            out.append((start, by, w, w))
    return out


def fixtures_fit(room_type: RoomType, clear_w: float, clear_l: float) -> tuple[bool, str]:
    """Whether ``room_type``'s fixtures fit in a ``clear_w`` × ``clear_l`` interior
    with code clearances. Returns ``(ok, reason)``; ``reason`` is empty when ok.

    Deliberately a set of **necessary** conditions (a clearly-too-small room
    fails), not a full packing — so it's a stable guardrail, not a finicky solver:

    * the short side must clear the deepest fixture plus its front clearance,
    * the long side must host the widest single fixture, and
    * the two longer walls together must hold the total fixture width.
    """
    kinds = fixtures_for(room_type)
    if not kinds:
        return True, ""
    specs = [FIXTURES[k] for k in kinds]
    short, long = min(clear_w, clear_l), max(clear_w, clear_l)

    deepest = max(s.depth for s in specs)
    front = max(s.front for s in specs)
    if short + 1e-6 < deepest + front:
        return False, (
            f"short side is {short:.1f} ft; a {room_type.value.replace('_', ' ')} "
            f"needs ~{deepest + front:.1f} ft for a fixture plus its clearance"
        )

    widest = max(s.width for s in specs)
    if long + 1e-6 < widest:
        return False, (
            f"longest wall is {long:.1f} ft; the widest fixture needs {widest:.1f} ft"
        )

    total_width = sum(s.width for s in specs)
    if (short + long) + 1e-6 < total_width:
        return False, (
            f"walls total ~{short + long:.1f} ft of run; the fixtures need "
            f"{total_width:.1f} ft"
        )
    return True, ""
