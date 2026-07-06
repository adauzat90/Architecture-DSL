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

from .elements import Barndominium, Direction
from .geometry import shared_edge
from .render import fmt_ft_in
from .validation import clear_dimensions, exterior_walls


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


def door_rows(plan: Barndominium) -> list[dict]:
    """One row per door — interior (room-to-room) and exterior — marked D1, D2…"""
    rows: list[dict] = []
    n = 0
    for d in plan.interior_doors:
        n += 1
        # Near-jamb offset from the shared wall's south/west start. An explicit
        # `offset` is that distance directly; `None` centres the leaf, so the near
        # jamb sits half the leftover to one side. `hi`/`lo` come from the shared
        # edge (the wall the door actually sits on).
        a, b = plan.room(d.room_a), plan.room(d.room_b)
        edge = shared_edge(a, b) if a is not None and b is not None else None
        if edge is None:
            offset, corner = None, ""
        else:
            w = min(d.width, edge.length)
            offset = d.offset if d.offset is not None else max(0.0, (edge.length - w) / 2.0)
            corner = "W" if edge.orientation == "h" else "S"
        rows.append(
            {
                "mark": f"D{n}",
                "kind": d.kind,
                "from": d.room_a,
                "to": d.room_b,
                "width": d.width,
                "offset": offset,
                "corner": corner,
            }
        )
    for xd in plan.exterior_doors:
        n += 1
        kind = "exterior"
        if getattr(xd, "kind", "entry") in ("double", "french", "overhead"):
            kind += f" {xd.kind}"  # a two-leaf pair, or the sectional garage door
        rows.append(
            {
                "mark": f"D{n}",
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
    for i, w in enumerate(plan.windows, start=1):
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
                "mark": f"W{i}",
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
