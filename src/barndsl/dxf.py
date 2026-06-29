"""Export a plan to DXF — the CAD interchange format architects and drafters use.

A barndsl plan is rectangles, lines, and labelled openings, all of which map
cleanly onto DXF entities (LINE / TEXT on per-category layers). This writes a
minimal but valid **DXF R12 (AC1009)** ASCII file by hand, so the core stays
dependency-free (no `ezdxf`), consistent with the rest of the project.

Coordinates pass straight through: barndsl is feet with ``x`` east / ``y`` north,
and DXF's world plane is also y-up, so a plan drops into model space 1 unit = 1
foot — the same pass-through the Revit exchange relies on.

    from barndsl import compile_source
    from barndsl.dxf import to_dxf
    plan = compile_source(src).plan
    open("plan.dxf", "w").write(to_dxf(plan))
"""

from __future__ import annotations

from .elements import Barndominium
from .geometry import footprint_boundary, opening_endpoints

# Layer name → AutoCAD Color Index (ACI). Mirrors the SVG's category split.
LAYERS: dict[str, int] = {
    "BARNDSL-ENVELOPE": 7,   # white/black — exterior shell
    "BARNDSL-ROOMS": 8,      # grey — interior partitions
    "BARNDSL-OPENINGS": 5,   # blue — doors & windows
    "BARNDSL-TEXT": 3,       # green — annotation
    "BARNDSL-PORCH": 2,      # yellow
    "BARNDSL-STAIRS": 4,     # cyan
}


def _g(code: int, value) -> str:
    """One DXF group: a code line then its value line."""
    return f"{code}\n{value}\n"


def _line(x1: float, y1: float, x2: float, y2: float, layer: str) -> str:
    return (
        _g(0, "LINE") + _g(8, layer)
        + _g(10, f"{x1:.4f}") + _g(20, f"{y1:.4f}") + _g(30, "0.0")
        + _g(11, f"{x2:.4f}") + _g(21, f"{y2:.4f}") + _g(31, "0.0")
    )


def _rect(x: float, y: float, w: float, h: float, layer: str) -> str:
    return (
        _line(x, y, x + w, y, layer)
        + _line(x + w, y, x + w, y + h, layer)
        + _line(x + w, y + h, x, y + h, layer)
        + _line(x, y + h, x, y, layer)
    )


def _text(x: float, y: float, s: str, height: float, layer: str) -> str:
    # DXF TEXT has no UTF niceties in R12; keep the string to plain ASCII.
    safe = "".join(ch if ord(ch) < 128 else "'" for ch in s)
    return (
        _g(0, "TEXT") + _g(8, layer)
        + _g(10, f"{x:.4f}") + _g(20, f"{y:.4f}") + _g(30, "0.0")
        + _g(40, f"{height:.4f}") + _g(1, safe)
    )


def _header() -> str:
    return _g(0, "SECTION") + _g(2, "HEADER") + _g(0, "ENDSEC")


def _tables() -> str:
    body = _g(0, "SECTION") + _g(2, "TABLES")
    body += _g(0, "TABLE") + _g(2, "LAYER") + _g(70, len(LAYERS))
    for name, color in LAYERS.items():
        body += (
            _g(0, "LAYER") + _g(2, name) + _g(70, 0)
            + _g(62, color) + _g(6, "CONTINUOUS")
        )
    body += _g(0, "ENDTAB") + _g(0, "ENDSEC")
    return body


def _entities(plan: Barndominium) -> str:
    body = _g(0, "SECTION") + _g(2, "ENTITIES")

    # Footprint outline (envelope, or the union boundary for an L/T/U plan).
    if plan.wings:
        for (x1, y1), (x2, y2) in footprint_boundary(plan.footprint_sections()):
            body += _line(x1, y1, x2, y2, "BARNDSL-ENVELOPE")
    else:
        body += _rect(0, 0, plan.envelope_width, plan.envelope_length, "BARNDSL-ENVELOPE")

    # Rooms (partitions) + labels.
    for r in plan.rooms:
        body += _rect(r.x, r.y, r.width, r.length, "BARNDSL-ROOMS")
        cx, cy = r.center
        body += _text(r.x + 0.4, cy + 0.3, r.display_name, 0.8, "BARNDSL-TEXT")
        body += _text(
            r.x + 0.4, cy - 0.9, f"{r.area:.0f} SF", 0.6, "BARNDSL-TEXT"
        )

    # Openings: windows and exterior doors drawn as a line across the wall.
    for win in plan.windows:
        room = plan.room(win.room)
        if room is None:
            continue
        x1, y1, x2, y2 = opening_endpoints(room, win.wall, win.offset, win.width)
        body += _line(x1, y1, x2, y2, "BARNDSL-OPENINGS")
    for door in plan.exterior_doors:
        room = plan.room(door.room)
        if room is None:
            continue
        x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
        body += _line(x1, y1, x2, y2, "BARNDSL-OPENINGS")

    # Porches and stair footprints as reference outlines.
    for p in plan.porches:
        body += _rect(p.x, p.y, p.width, p.length, "BARNDSL-PORCH")
    for s in plan.stairs:
        body += _rect(s.x, s.y, s.width, s.length, "BARNDSL-STAIRS")

    body += _g(0, "ENDSEC")
    return body


def to_dxf(plan: Barndominium) -> str:
    """Return ``plan`` as a DXF R12 document string."""
    return _header() + _tables() + _entities(plan) + _g(0, "EOF")


def save_dxf(plan: Barndominium, path: str) -> str:
    """Write ``plan`` as DXF to ``path``. Returns the path."""
    with open(path, "w", encoding="ascii", errors="replace") as fh:
        fh.write(to_dxf(plan))
    return path
