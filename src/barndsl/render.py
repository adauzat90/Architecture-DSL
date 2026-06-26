"""Render a barndominium plan to an annotated 2D SVG floor plan.

The output is a top-down drawing with rooms (filled and labelled with their
square footage), doors (shown as gaps with swing arcs), windows (double lines on
exterior walls), overall dimension lines, a title, and a side panel summarising
areas and a rough material takeoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

from .elements import Barndominium, Direction, RoomType
from .geometry import opening_endpoints, shared_edge

# Fill colours per room type (soft, print-friendly).
ROOM_COLORS: dict[RoomType, str] = {
    RoomType.LIVING: "#FDE9D9",
    RoomType.KITCHEN: "#FFF3C4",
    RoomType.DINING: "#FCE4D6",
    RoomType.BEDROOM: "#DDEBF7",
    RoomType.BATHROOM: "#D9F2EC",
    RoomType.HALF_BATH: "#E3F4EF",
    RoomType.LAUNDRY: "#E8E2F5",
    RoomType.UTILITY: "#ECECEC",
    RoomType.HALLWAY: "#F4F4F4",
    RoomType.CLOSET: "#EFEFEF",
    RoomType.PANTRY: "#F2EEDF",
    RoomType.MUDROOM: "#EAF0E2",
    RoomType.OFFICE: "#E6EEF7",
    RoomType.LOFT: "#E9F0FA",
    RoomType.GARAGE: "#E4E7EB",
    RoomType.SHOP: "#E1E5E9",
    RoomType.PORCH: "#EFF6E8",
    RoomType.OTHER: "#F0F0F0",
}

WALL = "#2b2b2b"
WINDOW_COLOR = "#2F6FB0"
DIM_COLOR = "#888888"
TEXT_COLOR = "#222222"


@dataclass
class RenderConfig:
    scale: float = 12.0  # pixels per foot
    margin_left: float = 70.0
    margin_top: float = 76.0
    margin_bottom: float = 60.0
    panel_width: float = 240.0
    gutter: float = 28.0
    font: str = "Helvetica, Arial, sans-serif"


def render_svg(plan: Barndominium, config: RenderConfig | None = None) -> str:
    """Return the plan rendered as an SVG document string."""
    return _Renderer(plan, config or RenderConfig()).render()


def save_svg(plan: Barndominium, path: str, config: RenderConfig | None = None) -> str:
    """Render the plan and write it to ``path``. Returns the path."""
    svg = render_svg(plan, config)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    return path


class _Renderer:
    def __init__(self, plan: Barndominium, config: RenderConfig):
        self.plan = plan
        self.c = config
        self.parts: list[str] = []

        # World bounding box (envelope plus any out-of-envelope porches).
        xs = [0.0, plan.envelope_width] + [p.x for p in plan.porches] + [
            p.x + p.width for p in plan.porches
        ]
        ys = [0.0, plan.envelope_length] + [p.y for p in plan.porches] + [
            p.y + p.length for p in plan.porches
        ]
        self.min_x, self.max_x = min(xs), max(xs)
        self.min_y, self.max_y = min(ys), max(ys)

        self.content_w = (self.max_x - self.min_x) * config.scale
        self.content_h = (self.max_y - self.min_y) * config.scale
        self.width = (
            config.margin_left + self.content_w + config.gutter + config.panel_width + 20
        )
        self.height = config.margin_top + self.content_h + config.margin_bottom

    # -- coordinate transform ---------------------------------------------

    def sx(self, x: float) -> float:
        return self.c.margin_left + (x - self.min_x) * self.c.scale

    def sy(self, y: float) -> float:
        # Flip: world +y (north) maps to screen up.
        return self.c.margin_top + (self.max_y - y) * self.c.scale

    # -- primitives --------------------------------------------------------

    def _rect(self, x, y, w, h, fill, stroke, sw=1.0, dash=None, rx=0.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        r = f' rx="{rx}"' if rx else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{r} />'
        )

    def _line(self, x1, y1, x2, y2, stroke, sw=1.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d} />'
        )

    def _text(self, x, y, s, size=11, anchor="middle", fill=TEXT_COLOR, weight="normal"):
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{self.c.font}" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(s)}</text>'
        )

    def _path(self, d, stroke, sw=1.0, fill="none"):
        self.parts.append(
            f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" />'
        )

    # -- main --------------------------------------------------------------

    def render(self) -> str:
        self.parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:.0f}" '
            f'height="{self.height:.0f}" viewBox="0 0 {self.width:.0f} {self.height:.0f}">'
        )
        self.parts.append(f'<rect width="{self.width:.0f}" height="{self.height:.0f}" fill="#ffffff" />')

        self._draw_title()
        self._draw_porches()
        self._draw_envelope()
        self._draw_rooms()
        self._draw_windows()
        self._draw_doors()
        self._draw_dimensions()
        self._draw_panel()

        self.parts.append("</svg>")
        return "\n".join(self.parts)

    def _draw_title(self):
        self._text(self.c.margin_left, 34, self.plan.name, size=22, anchor="start", weight="bold")
        m = self.plan.metrics()
        sub = (
            f"{m['footprint_sqft']:.0f} sq ft footprint   ·   "
            f"{int(m['bedroom_count'])} bed / {m['bathroom_count']:.1f} bath   ·   "
            f'{self.plan.envelope_width:.0f}′ × {self.plan.envelope_length:.0f}′'
        )
        self._text(self.c.margin_left, 54, sub, size=12, anchor="start", fill="#666666")

    def _draw_envelope(self):
        x = self.sx(0)
        y = self.sy(self.plan.envelope_length)
        self._rect(
            x, y, self.plan.envelope_width * self.c.scale,
            self.plan.envelope_length * self.c.scale,
            fill="none", stroke=WALL, sw=3.0,
        )

    def _draw_porches(self):
        for p in self.plan.porches:
            x = self.sx(p.x)
            y = self.sy(p.y + p.length)
            self._rect(
                x, y, p.width * self.c.scale, p.length * self.c.scale,
                fill=ROOM_COLORS[RoomType.PORCH], stroke="#9bbf78", sw=1.0,
                dash="6 4",
            )
            cx, cy = self.sx(p.x + p.width / 2), self.sy(p.y + p.length / 2)
            label = p.display_name + (" (covered)" if p.covered else "")
            self._text(cx, cy, label, size=10, fill="#5a7a3c")

    def _draw_rooms(self):
        for r in self.plan.rooms:
            x = self.sx(r.x)
            y = self.sy(r.y2)
            w = r.width * self.c.scale
            h = r.length * self.c.scale
            self._rect(x, y, w, h, fill=ROOM_COLORS.get(r.type, "#f0f0f0"), stroke=WALL, sw=1.5)
            cx = self.sx(r.center[0])
            cy = self.sy(r.center[1])
            self._text(cx, cy - 4, r.display_name, size=12, weight="bold")
            self._text(cx, cy + 11, f"{r.area:.0f} sq ft", size=10, fill="#555555")

    def _draw_windows(self):
        for win in self.plan.windows:
            room = self.plan.room(win.room)
            if not room:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, win.wall, win.offset, win.width)
            sx1, sy1, sx2, sy2 = self.sx(x1), self.sy(y1), self.sx(x2), self.sy(y2)
            # Double line straddling the wall for a window symbol.
            if win.wall in (Direction.NORTH, Direction.SOUTH):
                self._line(sx1, sy1 - 2, sx2, sy2 - 2, WINDOW_COLOR, 1.4)
                self._line(sx1, sy1 + 2, sx2, sy2 + 2, WINDOW_COLOR, 1.4)
            else:
                self._line(sx1 - 2, sy1, sx2 - 2, sy2, WINDOW_COLOR, 1.4)
                self._line(sx1 + 2, sy1, sx2 + 2, sy2, WINDOW_COLOR, 1.4)

    def _draw_doors(self):
        for door in self.plan.interior_doors:
            a, b = self.plan.room(door.room_a), self.plan.room(door.room_b)
            if not (a and b):
                continue
            edge = shared_edge(a, b)
            if edge is None:
                continue
            w = min(door.width, edge.length)
            start = edge.mid - w / 2
            if edge.orientation == "v":
                self._door_symbol(edge.pos, start, "v", w)
            else:
                self._door_symbol(start, edge.pos, "h", w)

        for door in self.plan.exterior_doors:
            room = self.plan.room(door.room)
            if not room:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, door.wall, door.offset, door.width)
            if door.wall in (Direction.NORTH, Direction.SOUTH):
                self._door_symbol(min(x1, x2), y1, "h", door.width)
            else:
                self._door_symbol(x1, min(y1, y2), "v", door.width)

    def _door_symbol(self, ox: float, oy: float, orientation: str, w: float):
        """Draw a door at plan-space origin (ox, oy) as gap + leaf + swing arc."""
        if orientation == "v":  # wall runs in +y; swing into +x or -x
            sgn = 1.0 if (ox + w) <= self.max_x else -1.0
            hinge = (ox, oy)
            latch = (ox, oy + w)
            tip = (ox + sgn * w, oy)
        else:  # wall runs in +x; swing into +y or -y
            sgn = 1.0 if (oy + w) <= self.max_y else -1.0
            hinge = (ox, oy)
            latch = (ox + w, oy)
            tip = (ox, oy + sgn * w)

        hx, hy = self.sx(hinge[0]), self.sy(hinge[1])
        lx, ly = self.sx(latch[0]), self.sy(latch[1])
        tx, ty = self.sx(tip[0]), self.sy(tip[1])

        # White out the wall under the opening, draw the leaf, then the arc.
        self._line(hx, hy, lx, ly, "#ffffff", 4.0)
        self._line(hx, hy, tx, ty, WALL, 1.2)
        r = w * self.c.scale
        self._path(f"M {tx:.1f} {ty:.1f} A {r:.1f} {r:.1f} 0 0 1 {lx:.1f} {ly:.1f}", "#999999", 0.8)

    def _draw_dimensions(self):
        scale = self.c.scale
        # Overall width dimension below the plan.
        y = self.c.margin_top + self.content_h + 28
        x0 = self.sx(0)
        x1 = self.sx(self.plan.envelope_width)
        self._dim_line(x0, y, x1, y, f'{self.plan.envelope_width:.0f}′', horizontal=True)
        # Overall length dimension left of the plan.
        x = self.c.margin_left - 34
        y0 = self.sy(0)
        y1 = self.sy(self.plan.envelope_length)
        self._dim_line(x, y0, x, y1, f'{self.plan.envelope_length:.0f}′', horizontal=False)

    def _dim_line(self, x1, y1, x2, y2, label, horizontal):
        self._line(x1, y1, x2, y2, DIM_COLOR, 1.0)
        tick = 4
        if horizontal:
            self._line(x1, y1 - tick, x1, y1 + tick, DIM_COLOR, 1.0)
            self._line(x2, y2 - tick, x2, y2 + tick, DIM_COLOR, 1.0)
            self._text((x1 + x2) / 2, y1 - 6, label, size=11, fill=DIM_COLOR)
        else:
            self._line(x1 - tick, y1, x1 + tick, y1, DIM_COLOR, 1.0)
            self._line(x2 - tick, y2, x2 + tick, y2, DIM_COLOR, 1.0)
            self.parts.append(
                f'<text x="{x1 - 6:.1f}" y="{(y1 + y2) / 2:.1f}" font-family="{self.c.font}" '
                f'font-size="11" fill="{DIM_COLOR}" text-anchor="middle" '
                f'transform="rotate(-90 {x1 - 6:.1f} {(y1 + y2) / 2:.1f})">{escape(label)}</text>'
            )

    def _draw_panel(self):
        px = self.c.margin_left + self.content_w + self.c.gutter
        py = self.c.margin_top
        pw = self.c.panel_width
        m = self.plan.metrics()

        self._rect(px, py, pw, self.content_h, fill="#FAFAFA", stroke="#DDDDDD", sw=1.0, rx=6)
        cx = px + 14
        y = py + 26
        self._text(cx, y, "PROJECT SUMMARY", size=12, anchor="start", weight="bold")
        y += 10

        rows = [
            ("Footprint", f"{m['footprint_sqft']:.0f} sq ft"),
            ("Interior (cond.)", f"{m['interior_sqft']:.0f} sq ft"),
            ("Habitable", f"{m['habitable_sqft']:.0f} sq ft"),
            ("Bedrooms", f"{int(m['bedroom_count'])}"),
            ("Bathrooms", f"{m['bathroom_count']:.1f}"),
            ("Ceiling", f'{self.plan.ceiling_height:.1f}′'),
            ("Ext. perimeter", f"{m['exterior_perimeter_ft']:.0f} ft"),
            ("Ext. wall area", f"{m['exterior_wall_area_sqft']:.0f} sq ft"),
            ("Roof area (≈)", f"{m['roof_area_sqft']:.0f} sq ft"),
        ]
        for label, value in rows:
            y += 20
            self._text(cx, y, label, size=11, anchor="start", fill="#555555")
            self._text(px + pw - 14, y, value, size=11, anchor="end", weight="bold")

        # Legend of room types present.
        y += 30
        self._text(cx, y, "LEGEND", size=11, anchor="start", weight="bold")
        present: list[RoomType] = []
        for r in self.plan.rooms:
            if r.type not in present:
                present.append(r.type)
        for t in present:
            y += 18
            self._rect(cx, y - 9, 12, 12, fill=ROOM_COLORS.get(t, "#f0f0f0"), stroke=WALL, sw=0.8)
            self._text(cx + 18, y, t.value.replace("_", " ").title(), size=10, anchor="start", fill="#444444")
