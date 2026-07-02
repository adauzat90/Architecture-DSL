"""Resolved-geometry introspection of a compiled plan (the "geometry pack").

The compiler resolves every room to a rectangle, assigns exterior walls, builds
the door/adjacency graph and knows exactly which runs of wall are still clear —
then historically threw all of it away after validating. This module serializes
it: :func:`plan_summary` computes a JSON-able dict from a compiled plan and
:func:`summary_text` renders it as the compact deterministic block that
``barndsl inspect`` prints and the agent's feedback appends — so a consumer
reads coordinates off a table instead of re-deriving them by mentally
re-executing the source.

The summary has four parts:

    rooms       each room's resolved rectangle, level, and which of its walls
                lie on the footprint boundary (the same footprint-aware
                :func:`~barndsl.validation.exterior_walls` every check uses);
    adjacency   the compiler's door graph — one edge per interior door/opening
                (room pair, kind, width);
    unplaced    footprint area no room covers, per level, as total square feet
                plus the rectangular pockets themselves — where AREA_UNUSED
                says "92 sqft unallocated", this says *where*;
    free_spans  for each room and wall, the intervals not blocked by existing
                openings, tagged with what an opening there reaches
                (``exterior`` or the neighbouring room sharing that span).

Everything here is a pure function of the plan: same plan in, same dict out.
The ``free_spans`` offsets follow the validator's conventions exactly (the ones
``DOOR_OOB``/``OPENING_CLASH`` enforce): a span to a neighbouring room is
measured from the south/west end of the **shared** wall — the value a
``door ... offset`` takes — and a span to the exterior is measured from the
start corner of the room's own wall — the value a ``window``/``entry`` offset
takes. An opening placed inside a reported span is legal by construction.
"""

from __future__ import annotations

from .constants import EPSILON
from .elements import Barndominium, Direction, Room
from .geometry import SharedEdge, point_in_footprint, shared_edge

# The door-offset convention must be the validator's, not a re-derivation —
# _door_interval is exactly what DOOR_OOB/OPENING_CLASH measure against.
from .validation import _door_interval, exterior_walls

#: The narrowest opening worth reporting a span for (a stock 24-in leaf).
#: Free runs thinner than this can't host a door or a useful window.
MIN_SPAN = 2.0

#: Walls in the order every per-room listing uses (matches ``exterior_walls``).
_WALL_ORDER = (Direction.SOUTH, Direction.NORTH, Direction.WEST, Direction.EAST)


# --- free wall spans ----------------------------------------------------------


def _wall_axis_interval(room: Room, wall: Direction) -> tuple[float, float]:
    """The wall's run as ``(lo, hi)`` along its axis (x for north/south walls,
    y for east/west walls) in world coordinates."""
    if wall in (Direction.SOUTH, Direction.NORTH):
        return room.x, room.x2
    return room.y, room.y2


def _edge_on_wall(room: Room, wall: Direction, edge: SharedEdge, tol: float = EPSILON) -> bool:
    """Does a shared edge lie on ``room``'s named ``wall``?"""
    if wall is Direction.SOUTH:
        return edge.orientation == "h" and abs(edge.pos - room.y) <= tol
    if wall is Direction.NORTH:
        return edge.orientation == "h" and abs(edge.pos - room.y2) <= tol
    if wall is Direction.WEST:
        return edge.orientation == "v" and abs(edge.pos - room.x) <= tol
    return edge.orientation == "v" and abs(edge.pos - room.x2) <= tol


def _blocked_intervals(plan: Barndominium, room: Room, wall: Direction) -> list[tuple[float, float]]:
    """World-coordinate spans on ``room``'s ``wall`` already taken by openings.

    Windows and exterior doors are offset from the wall's start corner; an
    interior door's span comes from :func:`~barndsl.validation._door_interval`
    on its shared edge (offset from the shared wall's south/west end, ``None``
    centred) — the identical arithmetic the clash checks use.
    """
    lo, _ = _wall_axis_interval(room, wall)
    blocked: list[tuple[float, float]] = []
    for w in plan.windows:
        if w.room == room.id and w.wall is wall:
            blocked.append((lo + w.offset, lo + w.offset + w.width))
    for xd in plan.exterior_doors:
        if xd.room == room.id and xd.wall is wall:
            blocked.append((lo + xd.offset, lo + xd.offset + xd.width))
    for d in plan.interior_doors:
        if room.id not in (d.room_a, d.room_b):
            continue
        other = plan.room(d.room_b if d.room_a == room.id else d.room_a)
        if other is None or other.id == room.id:
            continue
        edge = shared_edge(room, other)
        if edge is not None and _edge_on_wall(room, wall, edge):
            blocked.append(_door_interval(edge, d))
    return blocked


def _subtract(lo: float, hi: float, blocked: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The sub-intervals of ``[lo, hi]`` left after removing ``blocked`` spans."""
    spans = [(lo, hi)]
    for b_lo, b_hi in sorted(blocked):
        nxt: list[tuple[float, float]] = []
        for s_lo, s_hi in spans:
            if b_hi <= s_lo + EPSILON or b_lo >= s_hi - EPSILON:
                nxt.append((s_lo, s_hi))
                continue
            if b_lo > s_lo + EPSILON:
                nxt.append((s_lo, b_lo))
            if b_hi < s_hi - EPSILON:
                nxt.append((b_hi, s_hi))
        spans = nxt
    return spans


def _free_spans(plan: Barndominium) -> list[dict]:
    """Every legal-opening span, one dict per (room, wall, target, interval).

    ``lo``/``hi`` are already in the convention the matching statement takes
    (see the module docstring); spans shorter than :data:`MIN_SPAN` are dropped.
    Order is deterministic: rooms in plan order, walls south/north/west/east,
    the exterior target before neighbours (neighbours in plan order).
    """
    out: list[dict] = []
    for room in plan.rooms:
        ext = set(exterior_walls(plan, room))
        for wall in _WALL_ORDER:
            w_lo, w_hi = _wall_axis_interval(room, wall)
            free = _subtract(w_lo, w_hi, _blocked_intervals(plan, room, wall))
            # Each target owns a stretch of the wall and its own offset origin.
            targets: list[tuple[str, float, float, float]] = []
            if wall in ext:
                targets.append(("exterior", w_lo, w_hi, w_lo))
            for other in plan.rooms:
                if other.id == room.id or other.level != room.level:
                    continue
                edge = shared_edge(room, other)
                if edge is not None and _edge_on_wall(room, wall, edge):
                    targets.append((other.id, edge.lo, edge.hi, edge.lo))
            for to, t_lo, t_hi, origin in targets:
                for f_lo, f_hi in free:
                    s_lo, s_hi = max(f_lo, t_lo), min(f_hi, t_hi)
                    if s_hi - s_lo + EPSILON < MIN_SPAN:
                        continue
                    out.append(
                        {
                            "room": room.id,
                            "wall": wall.value,
                            "to": to,
                            "lo": round(s_lo - origin, 4),
                            "hi": round(s_hi - origin, 4),
                        }
                    )
    return out


# --- unplaced footprint pockets -----------------------------------------------


def _uncovered_pockets(
    sections: list[tuple[float, float, float, float]], rooms: list[Room]
) -> tuple[float, list[dict]]:
    """Footprint area no room covers: ``(total_sqft, pocket rectangles)``.

    Coordinate compression over every section and room edge (the same exact
    technique the footprint helpers in :mod:`barndsl.geometry` use), then a
    greedy south-west-first merge of uncovered cells into rectangles — not
    guaranteed minimal in count, but exact in area and deterministic.
    """
    xs = sorted({v for s in sections for v in (s[0], s[0] + s[2])} | {v for r in rooms for v in (r.x, r.x2)})
    ys = sorted({v for s in sections for v in (s[1], s[1] + s[3])} | {v for r in rooms for v in (r.y, r.y2)})

    def uncovered(i: int, j: int) -> bool:
        if xs[i + 1] - xs[i] <= EPSILON or ys[j + 1] - ys[j] <= EPSILON:
            return False  # a hairline cell from nearly-coincident edges
        cx = (xs[i] + xs[i + 1]) / 2.0
        cy = (ys[j] + ys[j + 1]) / 2.0
        if not point_in_footprint(sections, cx, cy):
            return False
        return not any(
            r.x - EPSILON <= cx <= r.x2 + EPSILON and r.y - EPSILON <= cy <= r.y2 + EPSILON
            for r in rooms
        )

    grid = [[uncovered(i, j) for j in range(len(ys) - 1)] for i in range(len(xs) - 1)]
    total = sum(
        (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
        for i in range(len(xs) - 1)
        for j in range(len(ys) - 1)
        if grid[i][j]
    )
    pockets: list[dict] = []
    claimed: set[tuple[int, int]] = set()
    for j in range(len(ys) - 1):  # south to north, so pockets sort by (y, x)
        for i in range(len(xs) - 1):
            if not grid[i][j] or (i, j) in claimed:
                continue
            i2 = i  # grow east …
            while i2 + 1 < len(xs) - 1 and grid[i2 + 1][j] and (i2 + 1, j) not in claimed:
                i2 += 1
            j2 = j  # … then north, while the whole strip above is uncovered
            while j2 + 1 < len(ys) - 1 and all(
                grid[k][j2 + 1] and (k, j2 + 1) not in claimed for k in range(i, i2 + 1)
            ):
                j2 += 1
            claimed.update((k, m) for k in range(i, i2 + 1) for m in range(j, j2 + 1))
            pockets.append(
                {
                    "x": xs[i],
                    "y": ys[j],
                    "width": xs[i2 + 1] - xs[i],
                    "length": ys[j2 + 1] - ys[j],
                }
            )
    return total, pockets


def _unplaced(plan: Barndominium) -> list[dict]:
    """Per-level unplaced footprint (ground always included; other levels when
    they hold rooms). The footprint union bounds every level — walls go up."""
    sections = plan.footprint_sections()
    levels = sorted({0} | {r.level for r in plan.rooms})
    out: list[dict] = []
    for level in levels:
        rooms = [r for r in plan.rooms if r.level == level]
        sqft, pockets = _uncovered_pockets(sections, rooms)
        out.append({"level": level, "sqft": round(sqft, 1), "pockets": pockets})
    return out


# --- the summary ----------------------------------------------------------------


def plan_summary(plan: Barndominium) -> dict:
    """The plan's resolved geometry as a stable JSON-able dict.

    Keys: ``name``, ``rooms``, ``adjacency``, ``unplaced``, ``free_spans`` (see
    the module docstring for each). Pure and deterministic — the machine face of
    ``barndsl inspect``, and what :func:`summary_text` renders for humans/agents.
    """
    rooms = [
        {
            "id": r.id,
            "type": r.type.value,
            "level": r.level,
            "x": r.x,
            "y": r.y,
            "width": r.width,
            "length": r.length,
            "exterior": [w.value for w in exterior_walls(plan, r)],
        }
        for r in plan.rooms
    ]
    adjacency = [
        {"a": d.room_a, "b": d.room_b, "kind": d.kind, "width": d.width}
        for d in plan.interior_doors
    ]
    return {
        "name": plan.name,
        "rooms": rooms,
        "adjacency": adjacency,
        "unplaced": _unplaced(plan),
        "free_spans": _free_spans(plan),
    }


def _g(value: float) -> str:
    """Format a measurement: drop a trailing .0 and float noise (28.0 -> '28')."""
    return f"{value:g}"


def summary_text(summary: dict) -> str:
    """Render :func:`plan_summary`'s dict as a compact deterministic text block.

    One line per room / unplaced level / free-span group, a single adjacency
    line — token-lean by design, since this rides the agent's revision prompt
    as well as the ``barndsl inspect`` output.
    """
    lines = ["Rooms (id type level x,y w x l | exterior walls):"]
    for r in summary["rooms"]:
        ext = " ".join(r["exterior"]) or "-"
        lines.append(
            f"  {r['id']} {r['type']} L{r['level']} {_g(r['x'])},{_g(r['y'])} "
            f"{_g(r['width'])} x {_g(r['length'])} | {ext}"
        )
    edges = ", ".join(
        f"{e['a']}-{e['b']} ({e['kind']} {_g(e['width'])})" for e in summary["adjacency"]
    )
    lines.append(f"Adjacency (door edges): {edges or 'none'}")
    for u in summary["unplaced"]:
        if not u["pockets"]:
            lines.append(f"Unplaced footprint (L{u['level']}): none")
            continue
        pockets = "; ".join(
            f"{_g(p['width'])} x {_g(p['length'])} at {_g(p['x'])},{_g(p['y'])}"
            for p in u["pockets"]
        )
        n = len(u["pockets"])
        lines.append(
            f"Unplaced footprint (L{u['level']}): {_g(u['sqft'])} sqft in "
            f"{n} pocket{'s' if n != 1 else ''}: {pockets}"
        )
    lines.append(
        "Free wall spans (legal opening offsets; door offsets count from the "
        "shared wall's S/W end):"
    )
    groups: list[tuple[tuple[str, str, str], list[str]]] = []
    for s in summary["free_spans"]:
        key = (s["room"], s["wall"], s["to"])
        if groups and groups[-1][0] == key:
            groups[-1][1].append(f"{_g(s['lo'])}-{_g(s['hi'])}")
        else:
            groups.append((key, [f"{_g(s['lo'])}-{_g(s['hi'])}"]))
    if not groups:
        lines.append("  none")
    for (room, wall, to), intervals in groups:
        lines.append(f"  {room} {wall} -> {to}: {', '.join(intervals)}")
    return "\n".join(lines)
