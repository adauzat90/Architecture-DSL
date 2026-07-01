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

from .elements import Barndominium
from .validation import clear_dimensions, exterior_walls


@dataclass
class Column:
    header: str
    get: Callable[[dict], str]


def _fmt_ft(v: float) -> str:
    return f"{v:g}′"


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
        rows.append(
            {
                "mark": f"D{n}",
                "kind": d.kind,
                "from": d.room_a,
                "to": d.room_b,
                "width": d.width,
            }
        )
    for xd in plan.exterior_doors:
        n += 1
        rows.append(
            {
                "mark": f"D{n}",
                "kind": "exterior" + ("" if xd.egress else " (no-egress)"),
                "from": xd.room,
                "to": f"exterior ({xd.wall.value})",
                "width": xd.width,
            }
        )
    return rows


def window_rows(plan: Barndominium) -> list[dict]:
    """One row per window — marked W1, W2… — with size, sill, and glazed area."""
    rows: list[dict] = []
    for i, w in enumerate(plan.windows, start=1):
        rows.append(
            {
                "mark": f"W{i}",
                "room": w.room,
                "wall": w.wall.value,
                "width": w.width,
                "height": max(0.0, w.head_height - w.sill_height),
                "sill": w.sill_height,
                "area": w.glazed_area,
            }
        )
    return rows


_ROOM_COLS = [
    Column("Mark", lambda r: r["mark"]),
    Column("Name", lambda r: r["name"]),
    Column("Type", lambda r: r["type"]),
    Column("Level", lambda r: str(r["level"])),
    Column("Size", lambda r: f"{r['width']:g}′ × {r['length']:g}′"),
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
]
_WINDOW_COLS = [
    Column("Mark", lambda r: r["mark"]),
    Column("Room", lambda r: r["room"]),
    Column("Wall", lambda r: r["wall"]),
    Column("Width", lambda r: _fmt_ft(r["width"])),
    Column("Height", lambda r: _fmt_ft(r["height"])),
    Column("Sill", lambda r: _fmt_ft(r["sill"])),
    Column("Glazed", lambda r: f"{r['area']:.0f} sq ft"),
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
