"""Schematic vertical views — exterior elevations and a building section.

The plan model carries a vertical dimension the 2D floor plan never draws: ceiling
heights (per-room overrides, vaulted rooms open to the roof), the inter-floor
assembly depth that stacks levels, the roof form and pitch, and every window's
sill/head. This module projects that into two orthographic views a builder or
lender expects but a plan alone can't give:

* an **elevation** — one exterior face straight on, with the roof profile and the
  doors/windows at their true heights;
* a **section** — a vertical cut across the building showing each level's floor and
  ceiling, vaulted double-heights, and the roof over them.

Schematic by design: heights, levels and openings are exact; the roof is drawn
from the ``roof`` form + pitch (**gable** exact; **shed** reasonable; **monitor**
approximated as a gable), and an L/T/U footprint (``wing``) is treated as its
bounding box — a note is drawn on the view. Pure Python — no Revit, no optional
deps; ``render_svg``'s companion for the vertical dimension.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from .elements import Barndominium, Direction, RoomType
from .revit import roof_plan

# Reuse the plan renderer's palette so the two views read as one drawing set.
from .render import ROOM_COLORS, TEXT_COLOR, WALL

SKY = "#F5F8FB"       # roof fill
WALLFILL = "#EFECE6"  # wall mass fill
GRADE = "#6B7B54"     # ground / grade line
OPENING = "#2F6FB0"   # window/door glazing
GUIDE = "#B9C0C7"     # eave / level guide lines
SCALE = 12.0          # pixels per foot
MARGIN_L = 64.0
MARGIN_T = 62.0
MARGIN_R = 150.0      # room on the right for height dimension labels
MARGIN_B = 52.0

#: Which world axis runs horizontally in a given elevation, and whether the
#: screen x is flipped so each face reads as seen from outside (N/E/S/W looking in).
_FACE_AXIS = {
    Direction.SOUTH: ("x", False),
    Direction.NORTH: ("x", True),
    Direction.EAST: ("y", False),
    Direction.WEST: ("y", True),
}


def _n(v: float) -> str:
    return f"{v:g}"


def _roof_geom(plan: Barndominium) -> dict:
    """Vertical roof facts: eave (top plate) and ridge heights, rise, ridge axis."""
    levels = plan.levels() or [0]
    top = levels[-1]
    eave = plan.level_elevation(top) + plan.ceiling_height
    rp = roof_plan(plan, top)
    return {
        "eave": eave,
        "rise": rp["rise"],
        "ridge": eave + rp["rise"],
        "gable_axis": rp["gable_axis"],  # the world axis the ridge runs along
        "style": rp["style"],
        "pitch": rp["pitch"],
    }


def _top_profile(
    plan: Barndominium, geom: dict, face: Direction
) -> tuple[str, float, float, float, float, list[tuple[float, float]]]:
    """The building's silhouette top (roof line) for ``face``, as
    ``(horiz_axis, wall0, wall1, roof0, roof1, [(p, z), ...])``. The walls span
    ``[wall0, wall1]``; the roof (and its top profile) spans the eave ``overhang``
    beyond them on each side — a triangle on a gable end, a flat ridge band on an
    eave side, a slope (or a low/high wall) for a shed."""
    minx, miny, maxx, maxy = plan.bounds()
    horiz = _FACE_AXIS[face][0]
    w0, w1 = (minx, maxx) if horiz == "x" else (miny, maxy)
    oh = plan.overhang
    p0, p1 = w0 - oh, w1 + oh  # roof/eave range, projected past the walls
    eave, ridge, rise = geom["eave"], geom["ridge"], geom["rise"]
    style, ga = geom["style"], geom["gable_axis"]
    if style in ("gable", "monitor"):
        if horiz != ga:  # face perpendicular to the ridge → gable end (triangle)
            prof = [(p0, eave), ((p0 + p1) / 2.0, ridge), (p1, eave)]
        else:  # eave side → ridge band
            prof = [(p0, ridge), (p1, ridge)]
    else:  # shed: one plane sloping across the short span from low to high eave.
        short = "x" if (maxx - minx) <= (maxy - miny) else "y"
        if horiz == short:
            prof = [(p0, eave), (p1, eave + rise)]
        else:  # a whole wall along the long axis: low at the short-axis min side.
            low_face = Direction.WEST if short == "x" else Direction.SOUTH
            h = eave if face is low_face else eave + rise
            prof = [(p0, h), (p1, h)]
    return horiz, w0, w1, p0, p1, prof


class _Canvas:
    """A tiny SVG builder with a world→screen transform (z is up)."""

    def __init__(self, h0: float, h1: float, zmax: float, flip: bool, title: str):
        self.h0, self.h1, self.flip = h0, h1, flip
        self.zmax = zmax
        self.title = title
        self.width = MARGIN_L + (h1 - h0) * SCALE + MARGIN_R
        self.height = MARGIN_T + zmax * SCALE + MARGIN_B
        self.parts: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:.0f}" '
            f'height="{self.height:.0f}" viewBox="0 0 {self.width:.0f} {self.height:.0f}">',
            f'<rect width="{self.width:.0f}" height="{self.height:.0f}" fill="#ffffff" />',
        ]
        self.text(MARGIN_L, 30, title, size=18, weight="bold", anchor="start")

    def sx(self, h: float) -> float:
        t = (self.h1 - h) if self.flip else (h - self.h0)
        return MARGIN_L + t * SCALE

    def sy(self, z: float) -> float:
        return MARGIN_T + (self.zmax - z) * SCALE

    def poly(self, pts, fill, stroke=WALL, sw=1.2):
        s = " ".join(f"{self.sx(h):.1f},{self.sy(z):.1f}" for h, z in pts)
        self.parts.append(
            f'<polygon points="{s}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" />'
        )

    def rect_wz(self, h0, z0, h1, z1, fill, stroke=WALL, sw=1.0, dash=None):
        x, y = self.sx(min(h0, h1)), self.sy(max(z0, z1))
        w = abs(self.sx(h1) - self.sx(h0))
        ht = abs(self.sy(z1) - self.sy(z0))
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{ht:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d} />'
        )

    def line_wz(self, h0, z0, h1, z1, stroke, sw=1.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{self.sx(h0):.1f}" y1="{self.sy(z0):.1f}" '
            f'x2="{self.sx(h1):.1f}" y2="{self.sy(z1):.1f}" stroke="{stroke}" '
            f'stroke-width="{sw}"{d} />'
        )

    def text(self, x, y, s, size=11, anchor="middle", fill=TEXT_COLOR, weight="normal"):
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="Helvetica, Arial, sans-serif" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(s)}</text>'
        )

    def dim(self, z: float, label: str):
        """A height dimension label at the right margin, on a guide line."""
        y = self.sy(z)
        self.line_wz(self.h0, z, self.h1, z, GUIDE, sw=0.8, dash="4,4")
        self.text(self.width - MARGIN_R + 8, y + 4, label, size=10, anchor="start", fill="#7A8896")

    def svg(self) -> str:
        return "\n".join([*self.parts, "</svg>"])


def _draw_grade(c: _Canvas, h0: float, h1: float) -> None:
    y = c.sy(0.0)
    c.parts.append(
        f'<line x1="{c.sx(h0) - 16:.1f}" y1="{y:.1f}" x2="{c.sx(h1) + 16:.1f}" '
        f'y2="{y:.1f}" stroke="{GRADE}" stroke-width="2.2" />'
    )


def elevation_svg(plan: Barndominium, side: Direction | str) -> str:
    """Return an SVG elevation of the plan's ``side`` (north|south|east|west) face."""
    side = Direction(side) if isinstance(side, str) else side
    geom = _roof_geom(plan)
    _, flip = _FACE_AXIS[side]
    horiz, w0, w1, r0, r1, profile = _top_profile(plan, geom, side)
    eave = geom["eave"]

    c = _Canvas(r0, r1, geom["ridge"], flip, f"{plan.name} — {side.value.upper()} elevation")
    _draw_grade(c, w0, w1)
    # Walls (0 → eave over [w0, w1]) and the roof above, projecting past the walls
    # by the eave overhang [r0, r1]; a light fill and the eave (plate) line.
    c.rect_wz(w0, 0.0, w1, eave, WALLFILL)
    c.poly([(r0, eave), *profile, (r1, eave)], SKY)
    c.line_wz(r0, eave, r1, eave, WALL, sw=1.0)

    # Openings on this face, at their true heights, stacked by level.
    for w in plan.windows:
        if w.wall is not side:
            continue
        room = plan.room(w.room)
        base = plan.level_elevation(room.level) if room else 0.0
        a = (room.x if horiz == "x" else room.y) + w.offset if room else w.offset
        c.rect_wz(a, base + w.sill_height, a + w.width, base + w.head_height, OPENING, sw=1.0)
    for d in plan.exterior_doors:
        if d.wall is not side:
            continue
        room = plan.room(d.room)
        base = plan.level_elevation(room.level) if room else 0.0
        a = (room.x if horiz == "x" else room.y) + d.offset if room else d.offset
        h = d.height if d.height is not None else (7.0 if d.overhead else 6.67)
        fill = "#DfE6EC" if d.overhead else OPENING
        c.rect_wz(a, base, a + d.width, base + h, fill, sw=1.0)

    c.dim(geom["eave"], f"eave {_n(geom['eave'])} ft")
    c.dim(geom["ridge"], f"ridge {_n(geom['ridge'])} ft")
    for lvl in plan.levels():
        if lvl > 0:
            c.dim(plan.level_elevation(lvl), f"L{lvl} floor {_n(plan.level_elevation(lvl))} ft")
    if plan.wings:
        c.text(MARGIN_L, c.height - 14, "schematic — L/T/U footprint shown to its bounding box",
               size=10, anchor="start", fill="#9AA6B2")
    return c.svg()


def section_svg(plan: Barndominium) -> str:
    """Return an SVG transverse section — a vertical cut perpendicular to the ridge
    at mid-building, showing each level's floor/ceiling, vaulted rooms and the roof."""
    geom = _roof_geom(plan)
    minx, miny, maxx, maxy = plan.bounds()
    ga = geom["gable_axis"]
    # Cut perpendicular to the ridge: constant on the ridge axis, show the other.
    if ga == "y":
        cut = (miny + maxy) / 2.0
        face = Direction.SOUTH
    else:
        cut = (minx + maxx) / 2.0
        face = Direction.EAST

    horiz, w0, w1, r0, r1, profile = _top_profile(plan, geom, face)
    eave = geom["eave"]
    c = _Canvas(r0, r1, geom["ridge"], False, f"{plan.name} — SECTION")
    _draw_grade(c, w0, w1)
    # The cut roof profile (a gable triangle across the short span, projecting past
    # the walls by the overhang) over an open interior — roof mass, then the plate
    # line, then the rooms the cut passes through at their true floor/ceiling.
    c.poly([(r0, eave), *profile, (r1, eave)], SKY)
    c.line_wz(r0, eave, r1, eave, GUIDE, sw=1.0, dash="4,4")

    def crossed(room) -> bool:
        lo, hi = (room.y, room.y2) if horiz == "x" else (room.x, room.x2)
        return lo - 1e-6 <= cut <= hi + 1e-6

    for room in plan.rooms:
        if room.type is RoomType.PORCH or not crossed(room):
            continue
        rp0 = room.x if horiz == "x" else room.y
        rp1 = room.x2 if horiz == "x" else room.y2
        floor = plan.level_elevation(room.level)
        fill = ROOM_COLORS.get(room.type, "#F0F0F0")
        ceil_h = room.ceiling_height if room.ceiling_height is not None else plan.ceiling_height
        top = geom["eave"] if room.vaulted else floor + ceil_h
        c.rect_wz(rp0, floor, rp1, top, fill, sw=0.8)
        mid = (rp0 + rp1) / 2.0
        tag = f"{room.id}" + (" · vaulted" if room.vaulted else f" · {_n(ceil_h)} ft")
        c.text(c.sx(mid), c.sy(floor) - 6, tag, size=9, fill="#555555")

    c.dim(geom["eave"], f"eave {_n(geom['eave'])} ft")
    c.dim(geom["ridge"], f"ridge {_n(geom['ridge'])} ft")
    for lvl in plan.levels():
        if lvl > 0:
            c.dim(plan.level_elevation(lvl), f"L{lvl} {_n(plan.level_elevation(lvl))} ft")
    c.text(c.sx((w0 + w1) / 2), c.height - 14,
           f"cut looking along the ridge ({ga}-axis)", size=10, fill="#9AA6B2")
    if plan.wings:
        c.text(MARGIN_L, c.height - 28, "schematic — L/T/U footprint to its bounding box",
               size=10, anchor="start", fill="#9AA6B2")
    return c.svg()


def save_elevation(plan: Barndominium, side: Direction | str, path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(elevation_svg(plan, side))
    return path


def save_section(plan: Barndominium, path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(section_svg(plan))
    return path
