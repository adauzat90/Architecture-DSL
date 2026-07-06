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

from .constants import SLAB_THICKNESS
from .elements import Barndominium, Direction, RoomType
from .revit import roof_plan

# Reuse the plan renderer's palette + shared ft-in formatter so the drawing set reads as one.
from .render import ROOM_COLORS, TEXT_COLOR, WALL, fmt_ft_in

# Wall thickness is shared with the DXF/plan wall bodies so the section reads at the
# same nominal thickness the rest of the drawing set builds to.
from .wallbodies import EXTERIOR as _WB_EXTERIOR
from .wallbodies import THICKNESS as _WB_THICKNESS

SKY = "#F5F8FB"       # roof fill
WALLFILL = "#EFECE6"  # wall mass fill
GRADE = "#6B7B54"     # ground / grade line
OPENING = "#2F6FB0"   # window glazing
MUNTIN = "#DCE7F1"    # window muntin / divided-lite bars (light, over the glass)
DOORLEAF = "#B98A4E"  # a hinged door leaf — a warm wood tone, unmistakably not glass
OVERHEAD = "#DfE6EC"  # an overhead / sectional (garage) door panel
DOORKNOB = "#5A4326"  # door hardware
PORCHPOST = "#8A7A63"  # porch column
PORCHROOF = "#E7E0D4"  # porch roof slab
DIMLINE = "#5B6B7A"   # section dimension colour
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

    def vdim(self, h: float, z0: float, z1: float, label: str, color: str = DIMLINE):
        """A vertical dimension line at horizontal ``h`` spanning ``[z0, z1]``, with
        tick caps and a rotated label — the section's ceiling-height measure."""
        x = self.sx(h)
        ya, yb = self.sy(z0), self.sy(z1)
        self.parts.append(
            f'<line x1="{x:.1f}" y1="{ya:.1f}" x2="{x:.1f}" y2="{yb:.1f}" '
            f'stroke="{color}" stroke-width="1.0" />'
        )
        for yy in (ya, yb):
            self.parts.append(
                f'<line x1="{x - 4:.1f}" y1="{yy:.1f}" x2="{x + 4:.1f}" y2="{yy:.1f}" '
                f'stroke="{color}" stroke-width="1.0" />'
            )
        cy = (ya + yb) / 2.0
        self.parts.append(
            f'<text x="{x - 6:.1f}" y="{cy:.1f}" font-family="Helvetica, Arial, sans-serif" '
            f'font-size="10" fill="{color}" text-anchor="middle" '
            f'transform="rotate(-90 {x - 6:.1f} {cy:.1f})">{escape(label)}</text>'
        )

    def svg(self) -> str:
        return "\n".join([*self.parts, "</svg>"])


def _draw_grade(c: _Canvas, h0: float, h1: float) -> None:
    """A heavier grade line with the standard repeating diagonal earth-hatch ticks
    below it, so the building visibly sits *on* the ground."""
    y = c.sy(0.0)
    xa, xb = c.sx(h0) - 16, c.sx(h1) + 16
    lo, hi = min(xa, xb), max(xa, xb)
    c.parts.append(
        f'<line x1="{lo:.1f}" y1="{y:.1f}" x2="{hi:.1f}" y2="{y:.1f}" '
        f'stroke="{GRADE}" stroke-width="2.6" />'
    )
    # Repeating 45° hatch ticks under the line — the drafting "earth" symbol.
    step, tick = 9.0, 7.0
    x = lo + step
    while x < hi:
        c.parts.append(
            f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x - tick:.1f}" y2="{y + tick:.1f}" '
            f'stroke="{GRADE}" stroke-width="1.0" />'
        )
        x += step


def _draw_window_elev(c: _Canvas, x0: float, z0: float, x1: float, z1: float, kind: str) -> None:
    """A window: glazing rect with sill + head lines and simple muntins — a 2×2
    divided-lite hint for casement/double-hung, one vertical bar for a slider, and
    a single clean pane for ``fixed`` glass."""
    c.rect_wz(x0, z0, x1, z1, OPENING, stroke=WALL, sw=1.0)
    ov = 0.22  # sill/head lines run a touch past the jambs, like a real trim board
    c.line_wz(x0 - ov, z0, x1 + ov, z0, WALL, sw=2.0)   # sill
    c.line_wz(x0 - ov, z1, x1 + ov, z1, WALL, sw=1.4)   # head
    xm, zm = (x0 + x1) / 2.0, (z0 + z1) / 2.0
    if kind == "fixed":
        return  # single fixed pane — no muntins
    if kind == "slider":
        c.line_wz(xm, z0, xm, z1, MUNTIN, sw=1.1)       # two side-by-side lites
        return
    # casement / double-hung → a 2×2 divided-lite cross
    c.line_wz(xm, z0, xm, z1, MUNTIN, sw=1.1)
    c.line_wz(x0, zm, x1, zm, MUNTIN, sw=1.1)


def _draw_door_elev(
    c: _Canvas, x0: float, z0: float, x1: float, z1: float, overhead: bool, front: bool
) -> None:
    """A door drawn to read as a door, never as glazing. Overhead/garage doors get
    horizontal sectional panel lines; a hinged door gets a wood leaf, a threshold, a
    two-panel inset and a knob. The plan's front door additionally gets a cased
    surround so it is unmistakable at a glance."""
    if overhead:
        c.rect_wz(x0, z0, x1, z1, OVERHEAD, stroke=WALL, sw=1.1)
        n = 4  # sectional panels
        for i in range(1, n):
            z = z0 + (z1 - z0) * i / n
            c.line_wz(x0, z, x1, z, WALL, sw=0.7)
        return
    c.rect_wz(x0, z0, x1, z1, DOORLEAF, stroke=WALL, sw=1.2)
    # Threshold: a heavy line at the sill, run slightly past the jambs.
    c.line_wz(x0 - 0.18, z0, x1 + 0.18, z0, WALL, sw=2.4)
    # Two stacked recessed panels.
    inset = min(0.4, (x1 - x0) * 0.2)
    mid = z0 + (z1 - z0) * 0.5
    gap = 0.28
    c.rect_wz(x0 + inset, mid + gap, x1 - inset, z1 - 0.35, "none", stroke=WALL, sw=0.8)
    c.rect_wz(x0 + inset, z0 + 0.5, x1 - inset, mid - gap, "none", stroke=WALL, sw=0.8)
    # Knob on the latch (right) side, at handle height.
    kx, kz = c.sx(x1 - inset - 0.18), c.sy(z0 + (z1 - z0) * 0.45)
    c.parts.append(f'<circle cx="{kx:.1f}" cy="{kz:.1f}" r="2.0" fill="{DOORKNOB}" />')
    if front:
        # A cased surround (jamb + head casing) marks the primary entrance.
        cas = 0.3
        c.rect_wz(x0 - cas, z0, x1 + cas, z1 + cas, "none", stroke=WALL, sw=1.6)


def _slope_segments(profile: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for (h0, z0), (h1, z1) in zip(profile, profile[1:]):
        if abs(z1 - z0) > 1e-6 and abs(h1 - h0) > 1e-6:
            out.append(((h0, z0), (h1, z1)))
    return out


def _draw_pitch_tag(c: _Canvas, profile: list[tuple[float, float]], pitch: float) -> None:
    """The standard rise-over-12 pitch triangle on one roof slope: a horizontal run
    leg labelled ``12`` and a vertical rise leg labelled with the actual rise, its
    hypotenuse drawn parallel to the roof plane."""
    segs = _slope_segments(profile)
    if not segs or pitch <= 0:
        return
    (h0, z0), (h1, z1) = segs[-1]  # the right-hand slope of a gable end
    descends_right = (h1 > h0) == (z1 < z0)
    run = 2.4
    rise = run * pitch
    t = 0.32
    ax = h0 + (h1 - h0) * t
    az = z0 + (z1 - z0) * t + 0.7  # floated just above the roof line
    dirn = 1.0 if descends_right else -1.0
    bx = ax + run * dirn
    c.line_wz(ax, az, bx, az, WALL, sw=1.0)          # run (horizontal)
    c.line_wz(bx, az, bx, az - rise, WALL, sw=1.0)   # rise (vertical)
    c.line_wz(ax, az, bx, az - rise, WALL, sw=1.2)   # hypotenuse ∥ roof
    c.text(c.sx(ax + run * dirn / 2.0), c.sy(az) - 3, "12", size=9)
    c.text(c.sx(bx) + 5 * dirn, c.sy(az - rise / 2.0) + 3, f"{pitch * 12.0:g}",
           size=9, anchor="start" if dirn > 0 else "end")


def _porch_side(plan: Barndominium, p, tol: float = 1e-6) -> Direction | None:
    """Which exterior face a covered porch shows on — the wall it projects past."""
    minx, miny, maxx, maxy = plan.bounds()
    if p.y < miny - tol:
        return Direction.SOUTH
    if p.y + p.length > maxy + tol:
        return Direction.NORTH
    if p.x < minx - tol:
        return Direction.WEST
    if p.x + p.width > maxx + tol:
        return Direction.EAST
    return None


def _draw_porches(c: _Canvas, plan: Barndominium, side: Direction, horiz: str, eave: float) -> None:
    """Covered porches on the viewed face: their roof slab and frame posts, drawn in
    front of the wall (they stand between the viewer and the facade)."""
    porch_h = min(eave - 1.0, 8.5)
    if porch_h <= 1.5:
        porch_h = eave * 0.7
    for p in plan.porches:
        if not p.covered or _porch_side(plan, p) is not side:
            continue
        a0, a1 = (p.x, p.x + p.width) if horiz == "x" else (p.y, p.y + p.length)
        # Posts: the two ends plus intermediate columns for a wide porch (≤ ~12 ft bays).
        n_bays = max(1, round((a1 - a0) / 12.0))
        post_w = 0.5
        for i in range(n_bays + 1):
            xi = a0 + (a1 - a0) * i / n_bays
            c.rect_wz(xi - post_w / 2.0, 0.0, xi + post_w / 2.0, porch_h,
                      PORCHPOST, stroke=WALL, sw=0.8)
        # Roof slab (fascia band) across the porch, projecting a small eave overhang.
        oh = 0.8
        c.rect_wz(a0 - oh, porch_h, a1 + oh, porch_h + 0.5, PORCHROOF, stroke=WALL, sw=1.0)


def _draw_slab(c: _Canvas, w0: float, w1: float, thick: float) -> None:
    """A slab-on-grade drawn as a double line (top at grade, bottom a slab-thickness
    below) with a light earth hatch between — the section's ground assembly."""
    lo, hi = min(w0, w1), max(w0, w1)
    c.rect_wz(lo, -thick, hi, 0.0, "#EDE9E2", stroke=WALL, sw=1.2)
    c.line_wz(lo, 0.0, hi, 0.0, WALL, sw=1.8)  # slab top, heavier
    xa, xb = c.sx(lo), c.sx(hi)
    y0, y1 = c.sy(0.0), c.sy(-thick)
    x = min(xa, xb) + 8.0
    while x < max(xa, xb):
        c.parts.append(
            f'<line x1="{x:.1f}" y1="{y0:.1f}" x2="{x - (y1 - y0):.1f}" y2="{y1:.1f}" '
            f'stroke="{GRADE}" stroke-width="0.6" />'
        )
        x += 12.0


def _draw_rafters(c: _Canvas, profile: list[tuple[float, float]], r0: float, r1: float, eave: float) -> None:
    """The roof drawn as a rafter/sheathing line pair: an underside line parallel to
    the roof top, offset down by a nominal rafter depth (the roof assembly thickness)."""
    depth = 0.85
    full = [(r0, eave), *profile, (r1, eave)]
    under = " ".join(f"{c.sx(h):.1f},{c.sy(z - depth):.1f}" for h, z in full)
    c.parts.append(
        f'<polyline points="{under}" fill="none" stroke="{WALL}" stroke-width="1.0" />'
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
        _draw_window_elev(c, a, base + w.sill_height, a + w.width, base + w.head_height, w.kind)
    front = next((d for d in plan.exterior_doors if not d.overhead), None)
    for d in plan.exterior_doors:
        if d.wall is not side:
            continue
        room = plan.room(d.room)
        base = plan.level_elevation(room.level) if room else 0.0
        a = (room.x if horiz == "x" else room.y) + d.offset if room else d.offset
        h = d.height if d.height is not None else (7.0 if d.overhead else 6.67)
        _draw_door_elev(c, a, base, a + d.width, base + h, d.overhead, d is front)

    # The pitch tag on the visible roof slope, and covered porches in front.
    _draw_pitch_tag(c, profile, geom["pitch"])
    _draw_porches(c, plan, side, horiz, eave)

    c.dim(geom["eave"], f"eave {fmt_ft_in(geom['eave'])}")
    c.dim(geom["ridge"], f"ridge {fmt_ft_in(geom['ridge'])}")
    for lvl in plan.levels():
        if lvl > 0:
            c.dim(plan.level_elevation(lvl), f"L{lvl} floor {fmt_ft_in(plan.level_elevation(lvl))}")
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
        tag = f"{room.id}" + (" · vaulted" if room.vaulted else f" · {fmt_ft_in(ceil_h)}")
        c.text(c.sx(mid), c.sy(floor) - 6, tag, size=9, fill="#555555")

    # --- A simple assembly vocabulary (a design section, not a construction detail) ---
    t_wall = _WB_THICKNESS[_WB_EXTERIOR]
    _draw_slab(c, w0, w1, SLAB_THICKNESS)
    # Exterior wall thickness at each eave wall the cut passes through.
    c.rect_wz(w0, 0.0, w0 + t_wall, eave, WALLFILL, stroke=WALL, sw=1.2)
    c.rect_wz(w1 - t_wall, 0.0, w1, eave, WALLFILL, stroke=WALL, sw=1.2)
    _draw_rafters(c, profile, r0, r1, eave)
    _draw_pitch_tag(c, profile, geom["pitch"])
    # Ceiling-height dimension just inside the left wall.
    c.vdim(w0 + t_wall + 1.2, 0.0, plan.ceiling_height, f"clg {fmt_ft_in(plan.ceiling_height)}")

    c.dim(geom["eave"], f"eave {fmt_ft_in(geom['eave'])}")
    c.dim(geom["ridge"], f"ridge {fmt_ft_in(geom['ridge'])}")
    for lvl in plan.levels():
        if lvl > 0:
            c.dim(plan.level_elevation(lvl), f"L{lvl} {fmt_ft_in(plan.level_elevation(lvl))}")
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
