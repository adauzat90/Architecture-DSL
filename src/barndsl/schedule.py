"""Room, door, and window schedules from a compiled plan — without Revit.

The Revit *Document* pass produces native door/window/room schedules, but that
value is locked behind owning Revit. The same data lives in the plan IR, so we
can emit the three schedules straight from a :class:`~barndsl.elements.Barndominium`
as a Markdown table (for a doc/PR) or CSV (for a spreadsheet) — something a
non-Revit user can hand to a builder.

    from barndsl import compile_source
    from barndsl.schedule import schedules_markdown
    plan = compile_source(src).plan
    print(schedules_markdown(plan))
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Callable

from .elements import Barndominium, Direction, ExteriorDoor, InteriorDoor, Window
from .geometry import door_offset, door_span, opening_endpoints, shared_edge
from .render import fmt_ft_in
from .validation import clear_dimensions, exterior_walls

#: How far (world ft) a mark bubble sits inside the room from the opening
#: centerline, so it lands on the room side clear of the exterior dim chains.
TAG_INSET_FT = 2.0


@dataclass
class Column:
    header: str
    get: Callable[[dict], str]


def _fmt_ft(v: float) -> str:
    return fmt_ft_in(v)


def _wall_start_corner(wall: Direction) -> str:
    """The corner an opening's offset is measured *from* — the wall's canonical
    south/west start. A horizontal wall (north/south) starts at its **W**est end;
    a vertical wall (east/west) starts at its **S**outh end. Framers lay out to
    this end, so the schedule states it beside every offset."""
    return "W" if wall in (Direction.NORTH, Direction.SOUTH) else "S"


def _offset_cell(offset: float | None, corner: str) -> str:
    """Render a near-jamb offset as ``<ft-in> from <corner>`` (or ``—``)."""
    if offset is None:
        return "—"
    return f"{fmt_ft_in(offset)} from {corner}"


# -- row builders ---------------------------------------------------------


def room_rows(plan: Barndominium) -> list[dict]:
    """One row per room: id, name, type, level, size, area, exterior walls."""
    rows: list[dict] = []
    for r in plan.rooms:
        walls = ", ".join(w.value for w in exterior_walls(plan, r)) or "—"
        clear_w, clear_l = clear_dimensions(plan, r)
        rows.append(
            {
                "mark": r.id,
                "name": r.display_name,
                "type": r.type.value,
                "level": r.level,
                "width": r.width,
                "length": r.length,
                "area": r.area,
                "clear_width": clear_w,
                "clear_length": clear_l,
                "clear_area": clear_w * clear_l,
                "exterior": walls,
            }
        )
    return rows


def door_marks(
    plan: Barndominium,
) -> list[tuple[str, InteriorDoor | ExteriorDoor]]:
    """``(mark, door)`` for every door in schedule order — interior doors first
    (``plan.interior_doors`` order), then exterior doors, numbered D1…Dn.

    The **single source of truth** for door marks: both the door schedule
    (:func:`door_rows`) and the floor-plan tag bubbles number from this list, so a
    plan tag and its schedule row can never disagree (pinned by a parity test)."""
    marks: list[tuple[str, InteriorDoor | ExteriorDoor]] = []
    n = 0
    for d in plan.interior_doors:
        n += 1
        marks.append((f"D{n}", d))
    for xd in plan.exterior_doors:
        n += 1
        marks.append((f"D{n}", xd))
    return marks


def window_marks(plan: Barndominium) -> list[tuple[str, Window]]:
    """``(mark, window)`` for every window in ``plan.windows`` order, W1…Wn — the
    single source of truth shared by :func:`window_rows` and the plan tags."""
    return [(f"W{i}", w) for i, w in enumerate(plan.windows, start=1)]


def _ext_tag_point(room, wall, x1, y1, x2, y2) -> tuple[float, float]:
    """Tag point for an opening on ``room``'s ``wall``: the opening midpoint pushed
    ``TAG_INSET_FT`` ft into the room (toward the room center)."""
    if wall in (Direction.NORTH, Direction.SOUTH):
        inward = 1.0 if room.center[1] > y1 else -1.0
        return ((x1 + x2) / 2.0, y1 + inward * TAG_INSET_FT)
    inward = 1.0 if room.center[0] > x1 else -1.0
    return (x1 + inward * TAG_INSET_FT, (y1 + y2) / 2.0)


def _door_tag_point(plan, door, level) -> tuple[float, float] | None:
    """World ``(x, y)`` for a door's tag bubble, or ``None`` when the door isn't on
    ``level``. Interior doors tag on room_a's side; exterior doors on the room."""
    if hasattr(door, "room_a"):  # interior (room-to-room)
        a, b = plan.room(door.room_a), plan.room(door.room_b)
        if not (a and b):
            return None
        if level is not None and not (a.level == b.level == level):
            return None
        edge = shared_edge(a, b)
        if edge is None:
            return None
        start, end = door_span(edge, door)
        mid = (start + end) / 2.0
        if edge.orientation == "v":  # vertical wall at x = edge.pos
            inward = 1.0 if a.center[0] > edge.pos else -1.0
            return (edge.pos + inward * TAG_INSET_FT, mid)
        inward = 1.0 if a.center[1] > edge.pos else -1.0
        return (mid, edge.pos + inward * TAG_INSET_FT)
    room = plan.room(door.room)  # exterior door
    if not room:
        return None
    if level is not None and room.level != level:
        return None
    x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
    return _ext_tag_point(room, door.wall, x1, y1, x2, y2)


def _window_tag_point(plan, win, level) -> tuple[float, float] | None:
    room = plan.room(win.room)
    if not room:
        return None
    if level is not None and room.level != level:
        return None
    x1, y1, x2, y2 = opening_endpoints(room, win.wall, win.offset, win.width)
    return _ext_tag_point(room, win.wall, x1, y1, x2, y2)


def opening_tag_points(
    plan: Barndominium, level: int | None = None
) -> list[tuple[str, float, float]]:
    """``(mark, world_x, world_y)`` for every door then window tag on ``level``
    (all levels when ``None``), numbered from :func:`door_marks`/:func:`window_marks`.

    The single geometry source shared by the SVG floor plan (mark bubbles) and the
    DXF export (mark TEXT), so a tag's position and number match across both."""
    pts: list[tuple[str, float, float]] = []
    for mark, door in door_marks(plan):
        pt = _door_tag_point(plan, door, level)
        if pt is not None:
            pts.append((mark, pt[0], pt[1]))
    for mark, win in window_marks(plan):
        pt = _window_tag_point(plan, win, level)
        if pt is not None:
            pts.append((mark, pt[0], pt[1]))
    return pts


def door_rows(plan: Barndominium) -> list[dict]:
    """One row per door — interior (room-to-room) and exterior — marked D1, D2…"""
    rows: list[dict] = []
    marks = {id(obj): mark for mark, obj in door_marks(plan)}
    for d in plan.interior_doors:
        # Near-jamb offset from the shared wall's south/west start, where the
        # door is drawn (a centred door sits half the leftover to one side).
        a, b = plan.room(d.room_a), plan.room(d.room_b)
        edge = shared_edge(a, b) if a is not None and b is not None else None
        if edge is None:
            offset, corner = None, ""
        else:
            offset = door_offset(edge, d)
            corner = "W" if edge.orientation == "h" else "S"
        rows.append(
            {
                "mark": marks[id(d)],
                "kind": d.kind,
                "from": d.room_a,
                "to": d.room_b,
                "width": d.width,
                "offset": offset,
                "corner": corner,
            }
        )
    for xd in plan.exterior_doors:
        kind = "exterior"
        if getattr(xd, "kind", "entry") in ("double", "french", "overhead"):
            kind += f" {xd.kind}"  # a two-leaf pair, or the sectional garage door
        rows.append(
            {
                "mark": marks[id(xd)],
                "kind": kind + ("" if xd.egress else " (no-egress)"),
                "from": xd.room,
                "to": f"exterior ({xd.wall.value})",
                "width": xd.width,
                "offset": xd.offset,
                "corner": _wall_start_corner(xd.wall),
            }
        )
    return rows


def window_rows(plan: Barndominium) -> list[dict]:
    """One row per window — marked W1, W2… — with kind, size, sill, glazed area and
    the glazing type ("tempered" for a safety-glazing hazard location, else "—").

    The Glazing value is computed from the SAME predicate the WINDOW_TEMPERED check
    uses (:func:`barndsl.validation.window_tempered_reason`), so the schedule and
    the diagnostic can never disagree about which windows need tempered glass."""
    from .validation import window_tempered_reason

    rows: list[dict] = []
    for mark, w in window_marks(plan):
        # Two distinguishable ways a window ends up tempered: the author DECLARED
        # it (`tempered`), or the geometry REQUIRES it (an R308.4 hazard location).
        # A declared window that also sits in a hazard reads "tempered (declared)"
        # — the declaration is what matters for the schedule.
        if getattr(w, "tempered", False):
            glazing = "tempered (declared)"
        elif window_tempered_reason(plan, w) is not None:
            glazing = "tempered (required)"
        else:
            glazing = "—"
        rows.append(
            {
                "mark": mark,
                "room": w.room,
                "wall": w.wall.value,
                "kind": getattr(w, "kind", "casement"),
                "width": w.width,
                "height": max(0.0, w.head_height - w.sill_height),
                "sill": w.sill_height,
                "area": w.glazed_area,
                "glazing": glazing,
                "offset": w.offset,  # already the distance to the near jamb
                "corner": _wall_start_corner(w.wall),
            }
        )
    return rows


_ROOM_COLS = [
    Column("Mark", lambda r: r["mark"]),
    Column("Name", lambda r: r["name"]),
    Column("Type", lambda r: r["type"]),
    Column("Level", lambda r: str(r["level"])),
    Column("Size", lambda r: f"{fmt_ft_in(r['width'])} × {fmt_ft_in(r['length'])}"),
    Column("Area", lambda r: f"{r['area']:.0f} sq ft"),
    # Clear (finish-face) area — what Revit's room schedule reports and what IRC
    # habitability minimums are measured to; smaller than nominal by the walls.
    Column("Clear area", lambda r: f"{r['clear_area']:.0f} sq ft"),
    Column("Exterior walls", lambda r: r["exterior"]),
]
_DOOR_COLS = [
    Column("Mark", lambda r: r["mark"]),
    Column("Type", lambda r: r["kind"]),
    Column("From", lambda r: r["from"]),
    Column("To", lambda r: r["to"]),
    Column("Width", lambda r: _fmt_ft(r["width"])),
    # Layout offset to the near jamb, from the wall's canonical start corner —
    # what a framer measures to. Interior doors report it too (no plan leader).
    Column("Near jamb", lambda r: _offset_cell(r["offset"], r["corner"])),
]
_WINDOW_COLS = [
    Column("Mark", lambda r: r["mark"]),
    Column("Room", lambda r: r["room"]),
    Column("Wall", lambda r: r["wall"]),
    Column("Type", lambda r: r["kind"]),
    Column("Width", lambda r: _fmt_ft(r["width"])),
    Column("Height", lambda r: _fmt_ft(r["height"])),
    Column("Sill", lambda r: _fmt_ft(r["sill"])),
    Column("Near jamb", lambda r: _offset_cell(r["offset"], r["corner"])),
    Column("Glazed", lambda r: f"{r['area']:.0f} sq ft"),
    Column("Glazing", lambda r: r["glazing"]),
]


def _schedules(plan: Barndominium, rooms: bool, doors: bool, windows: bool):
    """Yield ``(title, columns, rows)`` for each requested schedule."""
    if rooms:
        yield "Room Schedule", _ROOM_COLS, room_rows(plan)
    if doors:
        yield "Door Schedule", _DOOR_COLS, door_rows(plan)
    if windows:
        yield "Window Schedule", _WINDOW_COLS, window_rows(plan)


# -- formatters -----------------------------------------------------------


def _markdown_table(columns: list[Column], rows: list[dict]) -> str:
    head = "| " + " | ".join(c.header for c in columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    if not rows:
        return head + "\n" + rule + "\n| " + " | ".join("—" for _ in columns) + " |"
    body = [
        "| " + " | ".join(c.get(row) for c in columns) + " |"
        for row in rows
    ]
    return "\n".join([head, rule, *body])


def schedules_markdown(
    plan: Barndominium, *, rooms: bool = True, doors: bool = True, windows: bool = True
) -> str:
    """Render the requested schedules as Markdown (one table each)."""
    blocks = [f"# {plan.name} — Schedules"]
    for title, columns, rows in _schedules(plan, rooms, doors, windows):
        blocks.append(f"## {title} ({len(rows)})")
        blocks.append(_markdown_table(columns, rows))
    return "\n\n".join(blocks) + "\n"


def schedules_csv(
    plan: Barndominium, *, rooms: bool = True, doors: bool = True, windows: bool = True
) -> str:
    """Render the requested schedules as CSV, one block per schedule.

    CSV has no notion of multiple tables, so each schedule is preceded by a
    ``# <Title>`` comment line and separated by a blank line — a spreadsheet
    imports each block cleanly, and the comment marks where each starts.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    first = True
    for title, columns, rows in _schedules(plan, rooms, doors, windows):
        if not first:
            buf.write("\n")
        first = False
        buf.write(f"# {title}\n")
        writer.writerow([c.header for c in columns])
        for row in rows:
            writer.writerow([c.get(row) for c in columns])
    return buf.getvalue()
