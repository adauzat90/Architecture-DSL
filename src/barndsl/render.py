"""Render a barndominium plan to an annotated 2D SVG floor plan.

The output is a top-down drawing with rooms (filled and labelled with their
square footage), doors (shown as gaps with swing arcs), windows (double lines on
exterior walls), overall dimension lines, a title, and a side panel summarising
areas and a rough material takeoff.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .elements import Barndominium, Direction, RoomType
from .geometry import opening_endpoints, shared_edge

# US architectural feet-and-inches glyphs: prime (feet) and double-prime (inches).
_FT = "′"  # ′
_IN = "″"  # ″


def fmt_ft_in(feet: float) -> str:
    """Format a decimal-feet length as US feet-and-inches (rounded to the inch).

    Whole feet drop the inch part (``18′``, never ``18′-0″``); a fractional value
    reads ``18′-6″``; a sub-foot value reads as inches alone (``9″``); zero is
    ``0′``. Negatives shouldn't occur in a plan, but are formatted from their
    magnitude with a leading ``-`` rather than crashing.
    """
    neg = feet < 0
    total_inches = round(abs(feet) * 12.0)
    ft, inch = divmod(int(total_inches), 12)
    if inch == 0:
        s = f"{ft}{_FT}"
    elif ft == 0:
        s = f"{inch}{_IN}"
    else:
        s = f"{ft}{_FT}-{inch}{_IN}"
    return f"-{s}" if neg else s

#: Standard US architectural plan scales, as (inches-of-paper per foot, label),
#: largest first — the set an architect steps through to fit a plan on a sheet.
ARCH_SCALES: tuple[tuple[float, str], ...] = (
    (1 / 4, '1/4"'),
    (3 / 16, '3/16"'),
    (1 / 8, '1/8"'),
    (3 / 32, '3/32"'),
    (1 / 16, '1/16"'),
)

#: Print sheets we size to, as (short, long) inches. Letter and Tabloid (a.k.a.
#: 11×17 / ARCH B) cover the common permit submittals.
SHEETS: dict[str, tuple[float, float]] = {
    "Letter": (8.5, 11.0),
    "Tabloid": (11.0, 17.0),
}

#: Printable margin (in) reserved on every edge when fitting a plan to a sheet.
SHEET_MARGIN = 0.5


def fit_scale(
    svg_w_ft: float, svg_h_ft: float, sheet: str = "Letter"
) -> tuple[float, str]:
    """Largest standard architectural scale at which a plan fits a sheet.

    ``svg_w_ft``/``svg_h_ft`` are the drawing's extents in **feet** (the SVG's
    px size divided by its px-per-foot). Returns ``(inches_per_foot, label)`` —
    the biggest of :data:`ARCH_SCALES` at which the drawing fits inside the
    sheet's printable area in *either* orientation (portrait or landscape,
    whichever admits the bigger scale). Falls back to the smallest scale if even
    that overruns (a very large plan on a small sheet).
    """
    sw, sl = SHEETS.get(sheet, SHEETS["Letter"])
    a = sw - 2 * SHEET_MARGIN
    b = sl - 2 * SHEET_MARGIN
    orientations = ((a, b), (b, a))  # portrait, landscape
    for ipf, label in ARCH_SCALES:
        for aw, ah in orientations:
            if svg_w_ft * ipf <= aw + 1e-9 and svg_h_ft * ipf <= ah + 1e-9:
                return ipf, label
    return ARCH_SCALES[-1]


def sheet_scale(
    plan: Barndominium, config: "RenderConfig | None" = None, sheet: str = "Letter"
) -> tuple[float, str, float]:
    """Pick the print scale for ``plan`` and return ``(ipf, label, css_width_in)``.

    ``ipf`` is inches-of-paper per foot and ``label`` its architectural name
    (e.g. ``1/4"``); ``css_width_in`` is the physical width to give the embedded
    SVG so its px-per-foot renders at exactly that scale — the number both print
    paths set as the SVG's CSS ``width``. The plan drawing is then at true scale;
    the fixed-pixel title/panel bands just ride along proportionally.
    """
    config = config or RenderConfig()
    r = _Renderer(plan, config)
    ppf = config.scale  # px per foot
    svg_w_ft = r.width / ppf
    svg_h_ft = r.height / ppf
    ipf, label = fit_scale(svg_w_ft, svg_h_ft, sheet)
    return ipf, label, svg_w_ft * ipf


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
# Positioned annotations (leader-line notes): a muted, print-friendly accent —
# distinct from the dimension grey and the room ink, in the drafting-note family.
NOTE_COLOR = "#7A6A55"
# Fixtures/furniture: thin dark outlines, drafting style — subtle so the plan
# stays readable (no fill, or a whisper of one).
FIXTURE_COLOR = "#5a5a5a"
FIXTURE_FILL = "#00000008"
# Electrical overlay (outlets / switches / lights) — a muted violet, distinct
# from the fixture grey and the structural rust so the layer reads as its own.
ELEC_COLOR = "#8A5FB0"
# Structural overlay (post-and-beam frame).
BEAM_COLOR = "#B5651D"  # frame/bent beams — a steel/timber rust tone
RIDGE_COLOR = "#8A4B12"  # ridge member, slightly darker
POST_COLOR = "#5A3210"  # solid columns


@dataclass
class RenderConfig:
    scale: float = 12.0  # pixels per foot
    margin_left: float = 70.0
    margin_top: float = 76.0
    margin_bottom: float = 60.0
    panel_width: float = 240.0
    gutter: float = 28.0
    font: str = "Helvetica, Arial, sans-serif"
    #: Print each room's W×L dimensions under its label (skipped for rooms too
    #: small to fit the extra line legibly).
    show_room_dims: bool = True
    #: Draw a graphic scale bar (bottom-left, under the plan) — set by the print
    #: and permit-packet paths so a printed sheet carries a bar that survives any
    #: reprographic resize. Off for the screen render so the view stays uncluttered.
    scale_bar: bool = False
    #: Optional scale statement drawn beside the bar (e.g. ``SCALE: 1/4" = 1'-0"
    #: (Letter)``). ``None`` draws the bar alone. Only used when ``scale_bar``.
    scale_note: str | None = None
    #: Draw the electrical layer (outlets, switches, ceiling lights) over the
    #: plan. Off by default so the screen/permit floor plan stays uncluttered;
    #: the playground's ⚡ toggle and the packet's Electrical Plan sheet turn it on.
    show_electrical: bool = False


def render_svg(plan: Barndominium, config: RenderConfig | None = None) -> str:
    """Return the plan rendered as an SVG document string."""
    return _Renderer(plan, config or RenderConfig()).render()


def save_svg(plan: Barndominium, path: str, config: RenderConfig | None = None) -> str:
    """Render the plan and write it to ``path``. Returns the path."""
    svg = render_svg(plan, config)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    return path


#: Raster/vector targets beyond SVG, each needing the optional ``cairosvg`` extra.
RASTER_FORMATS = ("png", "pdf")


def save_render(
    plan: Barndominium,
    path: str,
    fmt: str | None = None,
    config: RenderConfig | None = None,
) -> str:
    """Render ``plan`` to ``path`` as SVG, PNG, or PDF.

    ``fmt`` defaults to the file extension. PNG/PDF need the optional ``cairosvg``
    dependency (``pip install 'barndsl[raster]'``); a clear ImportError is raised
    if it's missing rather than failing deep in the converter.
    """
    fmt = (fmt or path.rsplit(".", 1)[-1]).lower()
    if fmt == "svg":
        return save_svg(plan, path, config)
    if fmt not in RASTER_FORMATS:
        raise ValueError(
            f"unsupported render format {fmt!r}; use one of: svg, "
            + ", ".join(RASTER_FORMATS)
        )
    try:
        import cairosvg
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise ImportError(
            f"{fmt.upper()} output needs cairosvg — install it with "
            "`pip install 'barndsl[raster]'` (or `pip install cairosvg`)."
        ) from exc

    svg = render_svg(plan, config).encode("utf-8")
    if fmt == "png":
        cairosvg.svg2png(bytestring=svg, write_to=path)
    else:  # pdf
        cairosvg.svg2pdf(bytestring=svg, write_to=path)
    return path


class _Renderer:
    def __init__(self, plan: Barndominium, config: RenderConfig):
        self.plan = plan
        self.c = config
        self.parts: list[str] = []

        # World bounding box (whole footprint — incl. wings — plus out-of-envelope
        # porches).
        fx0, fy0, fx1, fy1 = plan.bounds()
        xs = [fx0, fx1] + [p.x for p in plan.porches] + [
            p.x + p.width for p in plan.porches
        ]
        ys = [fy0, fy1] + [p.y for p in plan.porches] + [
            p.y + p.length for p in plan.porches
        ]
        self.min_x, self.max_x = min(xs), max(xs)
        self.min_y, self.max_y = min(ys), max(ys)

        self.content_w = (self.max_x - self.min_x) * config.scale
        self.content_h = (self.max_y - self.min_y) * config.scale
        self.width = (
            config.margin_left + self.content_w + config.gutter + config.panel_width + 20
        )

        # Multi-story: draw one floor-plan block per level, stacked vertically.
        self.levels = plan.levels()
        self.multi = len(self.levels) > 1
        # Effective top margin. A single-level plan whose north wall carries a
        # chain dimension needs that extra row to clear the title block, so grow
        # the top band minimally when one is present (multi-level plans reserve
        # the north row inside ``label_gap`` below).
        self.top = config.margin_top
        if not self.multi and self._level_has_north_chain(0):
            self.top += 12.0
        self.label_gap = 28.0  # space above each level's envelope for its label
        # A north-side chain dimension sits in the band above each level's
        # envelope, sharing it with the "LEVEL n" caption. When any level draws
        # one, deepen that band so the chain clears the caption below it.
        if self.multi and any(
            self._level_has_north_chain(lvl) for lvl in self.levels
        ):
            self.label_gap = 46.0
        self.block_stride = self.label_gap + self.content_h + 34.0
        self._block_top = self.top  # set per level when rendering
        if self.multi:
            n = len(self.levels)
            self.height = (
                self.top + self.label_gap + self.content_h
                + (n - 1) * self.block_stride + config.margin_bottom
            )
        else:
            self.height = self.top + self.content_h + config.margin_bottom

        # The PROJECT SUMMARY panel (summary rows + legend) can be taller than the
        # plan drawing — a plan with many room types overruns the canvas otherwise.
        # Grow the SVG to enclose the panel's real content height so nothing clips;
        # plans whose panel already fits keep their height unchanged (max()).
        self._panel_ph = self._panel_content_height()
        panel_py = self.top + (self.label_gap if self.multi else 0.0)
        self.height = max(self.height, panel_py + self._panel_ph + 16.0)

        # A print scale bar rides in an extra band at the very bottom of the sheet.
        self._scale_bar_y = 0.0
        if self.c.scale_bar:
            self._scale_bar_y = self.height + 8.0
            self.height += 44.0

    def _panel_content_height(self) -> float:
        """Pixel height the summary panel needs, from its top to below the last
        legend row — mirrors the y-cursor walk in :meth:`_draw_panel`."""
        n_rows = 3  # footprint, interior, habitable
        if self.multi:
            n_rows += len(self.plan.area_by_level())
        n_rows += 7  # bedrooms, bathrooms, ceiling, perimeter, wall, roof, foundation
        if self.plan.frame_spec is not None or self.plan.posts:
            n_rows += 3  # frames, posts, beam length
            if self.plan.frame_spec is not None:
                n_rows += 1  # bay spacing
        present: list[RoomType] = []
        for r in self.plan.rooms:
            if r.type not in present:
                present.append(r.type)
        # title (26) + gap (10) + 20/summary-row + legend header (30)
        # + 18/legend-row + a bottom pad clearing the last row off the border.
        return 26 + 10 + 20 * n_rows + 30 + 18 * len(present) + 14

    def _env_top(self, i: int) -> float:
        return self.top + self.label_gap + i * self.block_stride

    # -- coordinate transform ---------------------------------------------

    def sx(self, x: float) -> float:
        return self.c.margin_left + (x - self.min_x) * self.c.scale

    def sy(self, y: float) -> float:
        # Flip: world +y (north) maps to screen up. _block_top selects the level.
        return self._block_top + (self.max_y - y) * self.c.scale

    # -- primitives --------------------------------------------------------

    def _rect(self, x, y, w, h, fill, stroke, sw=1.0, dash=None, rx=0.0, extra=""):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        r = f' rx="{rx}"' if rx else ""
        e = f" {extra}" if extra else ""
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{r}{e} />'
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
        self._draw_compass()
        if self.multi:
            for i, lvl in enumerate(self.levels):
                self._block_top = self._env_top(i)
                self._draw_level_block(lvl)
            self._draw_panel(multi=True)
        else:
            self._draw_street()
            self._draw_porches()
            self._draw_envelope()
            self._draw_rooms()
            self._draw_fixtures()
            if self.c.show_electrical:
                self._draw_electrical()
            self._draw_structure()
            self._draw_windows()
            self._draw_doors()
            self._draw_notes()
            self._draw_chain_dims()
            self._draw_dimensions()
            self._draw_panel()

        if self.c.scale_bar:
            self._draw_scale_bar()
        self.parts.append("</svg>")
        return "\n".join(self.parts)

    def _draw_level_block(self, lvl: int) -> None:
        label = f"LEVEL {lvl}" + (" — GROUND" if lvl == 0 else "")
        label += f"   ({self._extent_label()})"
        # With a reserved north-chain row (deepened ``label_gap``) the caption
        # rides at the top of the band so the chain sits below it, clear.
        cap_dy = (self.label_gap - 18) if self.label_gap > 28 else 12
        self._text(
            self.c.margin_left, self._block_top - cap_dy, label,
            size=13, anchor="start", weight="bold", fill="#333333",
        )
        if lvl == 0:
            self._draw_street()
            self._draw_porches()
        self._draw_envelope()
        self._draw_rooms(level=lvl)
        self._draw_fixtures(level=lvl)
        if self.c.show_electrical:
            self._draw_electrical(level=lvl)
        self._draw_structure(level=lvl)
        self._draw_windows(level=lvl)
        self._draw_doors(level=lvl)
        self._draw_notes(level=lvl)
        self._draw_stairs(lvl)
        self._draw_chain_dims(level=lvl)

    def _draw_street(self):
        """Mark the street/approach edge (a thick grey line + label) on the side the
        `street` directive names — along the footprint edge. No-op unless the plan
        declares a ``street``."""
        st = self.plan.street
        if st is None:
            return
        x0, y0, x1, y1 = self.plan.bounds()
        col = "#8A8F98"
        if st in (Direction.SOUTH, Direction.NORTH):
            yw = y0 if st is Direction.SOUTH else y1
            ys = self.sy(yw)
            self._line(self.sx(x0), ys, self.sx(x1), ys, col, sw=3.0)
            ty = ys + 14 if st is Direction.SOUTH else ys - 6
            self._text((self.sx(x0) + self.sx(x1)) / 2, ty, "STREET", size=10, fill=col)
        else:
            xw = x0 if st is Direction.WEST else x1
            xs = self.sx(xw)
            self._line(xs, self.sy(y0), xs, self.sy(y1), col, sw=3.0)
            anchor = "start" if st is Direction.WEST else "end"
            tx = xs + 4 if st is Direction.WEST else xs - 4
            self._text(tx, self.sy(y1) + 12, "STREET", size=10, fill=col, anchor=anchor)

    def _draw_compass(self):
        """A north-arrow rosette showing *true* north given the plan orientation.

        Screen-up is plan-north (the drawing is in plan coordinates). Plan-north
        points to compass azimuth ``orientation``, so true north is that many
        degrees counter-clockwise of up: a direction ``(-sinθ, -cosθ)`` in screen
        space (x right, y down). At ``orientation 0`` the arrow points straight up.
        An unsited plan (no ``orientation``) still gets the arrow — walls are
        compass-named, so north-up is the drawing's convention either way — but
        its caption says ``plan north`` rather than claiming a true azimuth.
        """
        theta = math.radians(self.plan.orientation or 0.0)
        cx, cy, r = self.width - 46.0, 48.0, 22.0
        dx, dy = -math.sin(theta), -math.cos(theta)  # unit vector toward true north
        px, py = -dy, dx  # perpendicular (for the arrowhead base)
        self.parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="#ffffff" '
            f'stroke="#bbbbbb" stroke-width="1" />'
        )
        tipx, tipy = cx + dx * r, cy + dy * r
        tailx, taily = cx - dx * r * 0.7, cy - dy * r * 0.7
        self._line(tailx, taily, tipx, tipy, WALL, sw=1.6)
        # Arrowhead: a small filled triangle at the tip, along the arrow axis.
        hl, hw = 7.0, 4.0
        bx, by = tipx - dx * hl, tipy - dy * hl
        self.parts.append(
            f'<polygon points="{tipx:.1f},{tipy:.1f} '
            f'{bx + px * hw:.1f},{by + py * hw:.1f} '
            f'{bx - px * hw:.1f},{by - py * hw:.1f}" fill="{WALL}" />'
        )
        # "N" just beyond the tip, and the declared azimuth beneath the rosette
        # (or the plan-north disclaimer when the plan carries no orientation).
        self._text(cx + dx * (r + 9), cy + dy * (r + 9) + 3, "N", size=11, weight="bold")
        caption = (
            "plan north" if self.plan.orientation is None
            else f"true N · {self.plan.orientation:g}°"
        )
        self._text(cx, cy + r + 14, caption, size=9, fill="#888888")

    def _draw_title(self):
        self._text(self.c.margin_left, 34, self.plan.name, size=22, anchor="start", weight="bold")
        m = self.plan.metrics()
        sub = (
            f"{m['footprint_sqft']:.0f} sq ft footprint   ·   "
            f"{int(m['bedroom_count'])} bed / {m['bathroom_count']:.1f} bath   ·   "
            f"{self._extent_label()}"
        )
        self._text(self.c.margin_left, 54, sub, size=12, anchor="start", fill="#666666")

    def _extent_label(self) -> str:
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        label = f"{fmt_ft_in(fx1 - fx0)} × {fmt_ft_in(fy1 - fy0)}"
        return label + " (L/T/U)" if self.plan.wings else label

    def _draw_envelope(self):
        if not self.plan.wings:  # plain rectangle — one stroke
            x = self.sx(0)
            y = self.sy(self.plan.envelope_length)
            self._rect(
                x, y, self.plan.envelope_width * self.c.scale,
                self.plan.envelope_length * self.c.scale,
                fill="none", stroke=WALL, sw=3.0,
            )
            return
        # Rectilinear (L/T/U) footprint: stroke the outline of the section union.
        from .geometry import footprint_boundary

        for (x1, y1), (x2, y2) in footprint_boundary(self.plan.footprint_sections()):
            self._line(
                self.sx(x1), self.sy(y1), self.sx(x2), self.sy(y2), stroke=WALL, sw=3.0
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

    def _draw_rooms(self, level: int | None = None):
        for r in self.plan.rooms:
            if level is not None and r.level != level:
                continue
            x = self.sx(r.x)
            y = self.sy(r.y2)
            w = r.width * self.c.scale
            h = r.length * self.c.scale
            # data-room lets the playground link a click on the plan back to the
            # room's source line (an inert attribute — no effect on the drawing).
            self._rect(
                x, y, w, h, fill=ROOM_COLORS.get(r.type, "#f0f0f0"), stroke=WALL, sw=1.5,
                extra=f'data-room="{escape(r.id)}"',
            )
            cx = self.sx(r.center[0])
            cy = self.sy(r.center[1])
            # With dimensions, lift the label so name / size / area stack evenly.
            dims = self.c.show_room_dims and w >= 64 and h >= 52
            if dims:
                self._text(cx, cy - 11, r.display_name, size=12, weight="bold")
                self._text(
                    cx, cy + 3,
                    f"{fmt_ft_in(r.width)} × {fmt_ft_in(r.length)}",
                    size=9, fill="#777777",
                )
                self._text(cx, cy + 16, f"{r.area:.0f} sq ft", size=10, fill="#555555")
            else:
                self._text(cx, cy - 4, r.display_name, size=12, weight="bold")
                self._text(cx, cy + 11, f"{r.area:.0f} sq ft", size=10, fill="#555555")

    def _draw_fixtures(self, level: int | None = None):
        """Draw each room's fixtures/furniture as thin architectural glyphs — over
        the room fill, under the labels/openings, each in a ``data-fixture`` group.

        Placement comes from :func:`barndsl.fixtures.resolve_room_fixtures` (authored
        fixtures plus surviving auto-seeds), so the plan, the 3D model and the Revit
        exchange all agree on where each fixture sits."""
        from .fixtures import resolve_room_fixtures

        for r in self.plan.rooms:
            if level is not None and r.level != level:
                continue
            # Counters draw FIRST (z-under), the drafting convention: a sink or
            # range set into a run then draws cleanly over the countertop.
            fixtures = sorted(
                resolve_room_fixtures(self.plan, r),
                key=lambda f: 0 if f.kind == "counter" else 1,
            )
            for f in fixtures:
                self._fixture_glyph(f)

    def _fx_ellipse(self, cx, cy, rx, ry, sw=0.8, fill="none"):
        self.parts.append(
            f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
            f'fill="{fill}" stroke="{FIXTURE_COLOR}" stroke-width="{sw}" />'
        )

    def _fixture_glyph(self, f) -> None:
        """One fixture's glyph, in a ``data-fixture`` group, from its world box."""
        x = self.sx(f.x)
        y = self.sy(f.y + f.length)  # screen top-left (NW corner)
        w = f.width * self.c.scale
        h = f.length * self.c.scale
        if w < 2 or h < 2:
            return
        self.parts.append(f'<g data-fixture="{escape(f.id or f.kind)}">')
        # `back` names the screen edge the fixture backs onto (its wall), so tanks,
        # pillows and sofa-backs land on the correct side. World S is screen-bottom.
        back = {"S": "bottom", "N": "top", "W": "left", "E": "right"}.get(f.wall, "bottom")
        self._glyph_body(f.kind, x, y, w, h, back)
        self.parts.append("</g>")

    def _glyph_body(self, kind, x, y, w, h, back):
        cx, cy = x + w / 2, y + h / 2
        rr = min(w, h) * 0.12  # a small corner radius for furniture

        def band(depth):
            """The (x,y,w,h) of a strip of ``depth`` fraction on the ``back`` edge."""
            if back == "bottom":
                return x, y + h * (1 - depth), w, h * depth
            if back == "top":
                return x, y, w, h * depth
            if back == "left":
                return x, y, w * depth, h
            return x + w * (1 - depth), y, w * depth, h  # right

        if kind == "toilet":
            bx, by, bw, bh = band(0.32)  # tank
            self._rect(bx, by, bw, bh, "none", FIXTURE_COLOR, 0.8)
            # bowl ellipse centred in the remaining depth
            if back in ("bottom", "top"):
                ey = y + h * 0.34 if back == "bottom" else y + h * 0.66
                self._fx_ellipse(cx, ey, w * 0.32, h * 0.28)
            else:
                ex = x + w * 0.66 if back == "left" else x + w * 0.34
                self._fx_ellipse(ex, cy, w * 0.28, h * 0.32)
        elif kind == "tub":
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.9, rx=rr)
            self._fx_ellipse(cx, cy, w * 0.36, h * 0.36)
            self._fx_ellipse(cx, cy, 1.4, 1.4)  # drain
        elif kind == "shower":
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.9)
            self._line(x, y, x + w, y + h, FIXTURE_COLOR, 0.7)
            self._line(x + w, y, x, y + h, FIXTURE_COLOR, 0.7)
        elif kind in ("lavatory", "sink"):
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.8)
            self._fx_ellipse(cx, cy, w * 0.3, h * 0.3)
        elif kind == "range":
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.8)
            for gx in (0.3, 0.7):
                for gy in (0.3, 0.7):
                    self._fx_ellipse(x + w * gx, y + h * gy, min(w, h) * 0.13, min(w, h) * 0.13)
        elif kind == "refrigerator":
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.8)
            # door split line down the middle of the depth axis
            if back in ("bottom", "top"):
                self._line(cx, y, cx, y + h, FIXTURE_COLOR, 0.7)
            else:
                self._line(x, cy, x + w, cy, FIXTURE_COLOR, 0.7)
        elif kind in ("washer", "dryer"):
            self._rect(x, y, w, h, "none", FIXTURE_COLOR, 0.8)
            self._fx_ellipse(cx, cy, min(w, h) * 0.3, min(w, h) * 0.3)
        elif kind == "water_heater":
            self._fx_ellipse(cx, cy, w * 0.45, h * 0.45)
        elif kind in ("bed_queen", "bed_twin"):
            self._rect(x, y, w, h, FIXTURE_FILL, FIXTURE_COLOR, 0.9, rx=rr)
            bx, by, bw, bh = band(0.24)  # pillow band at the head
            self._rect(bx, by, bw, bh, "none", FIXTURE_COLOR, 0.7)
            # a turned-down fold line across the foot
            if back == "bottom":
                self._line(x, y + h * 0.24, x + w, y + h * 0.24, FIXTURE_COLOR, 0.6)
            elif back == "top":
                self._line(x, y + h * 0.76, x + w, y + h * 0.76, FIXTURE_COLOR, 0.6)
            elif back == "left":
                self._line(x + w * 0.24, y, x + w * 0.24, y + h, FIXTURE_COLOR, 0.6)
            else:
                self._line(x + w * 0.76, y, x + w * 0.76, y + h, FIXTURE_COLOR, 0.6)
        elif kind in ("sofa", "armchair"):
            self._rect(x, y, w, h, FIXTURE_FILL, FIXTURE_COLOR, 0.9, rx=rr)
            bx, by, bw, bh = band(0.24)  # back cushion band
            self._rect(bx, by, bw, bh, "none", FIXTURE_COLOR, 0.7)
        elif kind == "counter":
            # A countertop run: a plain casework rectangle plus a subtle second edge
            # line on the room-facing LONG edge — the bullnose — so a run reads as a
            # counter, not a box. The room-facing edge is opposite the wall (`back`).
            self._rect(x, y, w, h, FIXTURE_FILL, FIXTURE_COLOR, 0.9)
            inset = min(w, h) * 0.16
            if back == "bottom":  # backs S, faces N (screen top) → line near top
                self._line(x, y + inset, x + w, y + inset, FIXTURE_COLOR, 0.6)
            elif back == "top":  # faces screen bottom
                self._line(x, y + h - inset, x + w, y + h - inset, FIXTURE_COLOR, 0.6)
            elif back == "left":  # faces screen right
                self._line(x + w - inset, y, x + w - inset, y + h, FIXTURE_COLOR, 0.6)
            else:  # back == right, faces screen left
                self._line(x + inset, y, x + inset, y + h, FIXTURE_COLOR, 0.6)
        else:
            # tables / desk / dresser / wardrobe / counter / island / other:
            # a plain rounded rectangle reads as casework.
            self._rect(x, y, w, h, FIXTURE_FILL, FIXTURE_COLOR, 0.9, rx=rr)

    # -- electrical layer --------------------------------------------------

    def _wall_point(self, r, wall: Direction, offset: float) -> tuple[float, float, tuple[float, float]]:
        """World point of a device at ``offset`` along ``wall`` of room ``r``, plus
        the screen-space unit vector pointing *into* the room (for the ticks)."""
        off = min(max(offset, 0.0), r.width if wall in (Direction.SOUTH, Direction.NORTH) else r.length)
        if wall is Direction.SOUTH:
            return r.x + off, r.y, (0.0, -1.0)  # into room = screen up
        if wall is Direction.NORTH:
            return r.x + off, r.y + r.length, (0.0, 1.0)
        if wall is Direction.WEST:
            return r.x, r.y + off, (1.0, 0.0)
        return r.x + r.width, r.y + off, (-1.0, 0.0)  # east

    def _draw_electrical(self, level: int | None = None) -> None:
        """Draw the electrical layer — receptacles (duplex symbol, "GFCI" tag when
        ground-fault), wall switches ("S"), and ceiling lights (circled-X, with a
        kind variant) — over the plan in a muted ``data-layer="electrical"`` group.
        Devices carry no level of their own; they inherit their room's."""
        if not (
            self.plan.outlets or self.plan.switches or self.plan.lights
            or getattr(self.plan, "alarms", None)
        ):
            return
        by_id = {r.id: r for r in self.plan.rooms}
        self.parts.append('<g data-layer="electrical">')
        for o in self.plan.outlets:
            r = by_id.get(o.room)
            if r is None or (level is not None and r.level != level):
                continue
            wx, wy, into = self._wall_point(r, o.wall, o.offset)
            self._outlet_symbol(self.sx(wx), self.sy(wy), into, o.gfci)
        for s in self.plan.switches:
            r = by_id.get(s.room)
            if r is None or (level is not None and r.level != level):
                continue
            wx, wy, into = self._wall_point(r, s.wall, s.offset)
            self._switch_symbol(self.sx(wx), self.sy(wy), into)
        for lt in self.plan.lights:
            r = by_id.get(lt.room)
            if r is None or (level is not None and r.level != level):
                continue
            self._light_symbol(self.sx(r.x + lt.x), self.sy(r.y + lt.y), lt.kind)
        for al in getattr(self.plan, "alarms", None) or []:
            r = by_id.get(al.room)
            if r is None or (level is not None and r.level != level):
                continue
            ax = r.x + (al.x if al.x is not None else r.width / 2.0)
            ay = r.y + (al.y if al.y is not None else r.length / 2.0)
            self._alarm_symbol(self.sx(ax), self.sy(ay), al.kind)
        self.parts.append("</g>")

    def _elec_circle(self, cx, cy, rad, fill="#ffffff", sw=1.0):
        self.parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rad:.1f}" fill="{fill}" '
            f'stroke="{ELEC_COLOR}" stroke-width="{sw}" />'
        )

    def _outlet_symbol(self, px, py, into, gfci: bool) -> None:
        """A duplex receptacle: a small circle set just inside the wall with two
        ticks perpendicular to it (the classic NEC symbol). GFCI adds a tiny tag."""
        ix, iy = into  # inward unit (screen)
        rad = 3.4
        cx, cy = px + ix * (rad + 1.0), py + iy * (rad + 1.0)
        self._line(px, py, cx, cy, ELEC_COLOR, sw=0.9)  # stem to the wall
        self._elec_circle(cx, cy, rad)
        # Two ticks across the circle, perpendicular to the inward stem (the two
        # receptacle slots): the along-wall direction is (-iy, ix).
        ax, ay = -iy, ix
        for s in (-1.0, 1.0):
            ox, oy = ax * 1.5 * s, ay * 1.5 * s
            self._line(cx + ox - ix * 2.2, cy + oy - iy * 2.2,
                       cx + ox + ix * 2.2, cy + oy + iy * 2.2, ELEC_COLOR, sw=0.9)
        if gfci:
            self._text(cx + ix * 6.5, cy + iy * 6.5 + 3, "GFCI", size=6,
                       fill=ELEC_COLOR, weight="bold")

    def _switch_symbol(self, px, py, into) -> None:
        """A wall switch: an "S" set just inside the wall, with a short stem."""
        ix, iy = into
        cx, cy = px + ix * 6.0, py + iy * 6.0
        self._line(px, py, px + ix * 2.5, py + iy * 2.5, ELEC_COLOR, sw=0.9)
        self._text(cx, cy + 3.0, "S", size=9, fill=ELEC_COLOR, weight="bold")

    def _light_symbol(self, cx, cy, kind: str) -> None:
        """A ceiling luminaire: a circled-X. Kind variants keep it simple — a
        pendant gets a centre dot, a recessed can a second ring, a fan two blades."""
        rad = 4.2
        self._elec_circle(cx, cy, rad)
        d = rad * 0.7
        self._line(cx - d, cy - d, cx + d, cy + d, ELEC_COLOR, sw=0.9)
        self._line(cx + d, cy - d, cx - d, cy + d, ELEC_COLOR, sw=0.9)
        if kind == "recessed":
            self._elec_circle(cx, cy, rad + 1.8, fill="none", sw=0.7)
        elif kind == "pendant":
            self.parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.3" fill="{ELEC_COLOR}" />'
            )
        elif kind == "fan":
            self._line(cx - rad * 1.6, cy, cx + rad * 1.6, cy, ELEC_COLOR, sw=0.7)
            self._line(cx, cy - rad * 1.6, cx, cy + rad * 1.6, ELEC_COLOR, sw=0.7)

    #: The label a smoke/CO alarm draws in its circle, by kind.
    _ALARM_LABELS = {"smoke": "SD", "co": "CO", "smoke_co": "SD/CO"}

    def _alarm_symbol(self, cx, cy, kind: str) -> None:
        """A ceiling smoke/CO alarm: a small circle with "SD" (smoke), "CO" (carbon
        monoxide) or "SD/CO" (a combination unit) centred in it."""
        label = self._ALARM_LABELS.get(kind, "SD")
        rad = 6.2 if kind == "smoke_co" else 4.6
        self._elec_circle(cx, cy, rad)
        self._text(cx, cy + 2.2, label, size=6, fill=ELEC_COLOR, weight="bold")

    def _draw_structure(self, level: int = 0):
        """Overlay the post-and-beam frame: beam centrelines + solid posts.

        Structure lives on the ground level (level 0); on a multi-level drawing it
        appears only on that block.
        """
        if level != 0 or not (self.plan.beams or self.plan.posts):
            return
        for b in self.plan.beams:
            if getattr(b, "level", 0) != 0:
                continue
            color = RIDGE_COLOR if b.role == "ridge" else BEAM_COLOR
            dash = "7 4" if b.role == "ridge" else None
            self._line(
                self.sx(b.x1), self.sy(b.y1), self.sx(b.x2), self.sy(b.y2),
                color, 2.2, dash=dash,
            )
        for p in self.plan.posts:
            if getattr(p, "level", 0) != 0:
                continue
            # Draw at least a visible nib even for a small section.
            s = max(p.size * self.c.scale, 5.0)
            self._rect(
                self.sx(p.x) - s / 2, self.sy(p.y) - s / 2, s, s,
                fill=POST_COLOR, stroke=POST_COLOR, sw=0.8,
            )

    def _draw_windows(self, level: int | None = None):
        for win in self.plan.windows:
            room = self.plan.room(win.room)
            if not room:
                continue
            if level is not None and room.level != level:
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

    def _draw_doors(self, level: int | None = None):
        for door in self.plan.interior_doors:
            a, b = self.plan.room(door.room_a), self.plan.room(door.room_b)
            if not (a and b):
                continue
            if level is not None and not (a.level == b.level == level):
                continue  # cross-level doors are shown via the stair, not here
            edge = shared_edge(a, b)
            if edge is None:
                continue
            w = min(door.width, edge.length)
            offset = getattr(door, "offset", None)
            if offset is None:
                start = edge.mid - w / 2  # centre on the shared wall
            else:  # measured from the south/west end, clamped onto the wall
                start = edge.lo + max(0.0, min(offset, edge.length - w))
            kind = getattr(door, "kind", "swing" if getattr(door, "leaf", True) else "cased")
            ox, oy = (edge.pos, start) if edge.orientation == "v" else (start, edge.pos)
            if kind == "swing":
                sgn = self._swing_sgn(door, a, b, edge)
                hinge_far = getattr(door, "hinge", None) == "far"
                self._door_symbol(ox, oy, edge.orientation, w, sgn, hinge_far)
            elif kind in ("double", "french"):
                # Two half-width leaves hinged at opposite jambs, meeting at
                # the middle — the classic double-door plan symbol.
                sgn = self._swing_sgn(door, a, b, edge)
                half = w / 2.0
                self._door_symbol(ox, oy, edge.orientation, half, sgn, False)
                if edge.orientation == "v":
                    self._door_symbol(ox, oy + half, edge.orientation, half, sgn, True)
                else:
                    self._door_symbol(ox + half, oy, edge.orientation, half, sgn, True)
            elif kind in ("pocket", "sliding"):
                self._slide_symbol(ox, oy, edge.orientation, w)
            else:  # cased opening
                self._opening_symbol(ox, oy, edge.orientation, w)

        for xdoor in self.plan.exterior_doors:
            room = self.plan.room(xdoor.room)
            if not room:
                continue
            if level is not None and room.level != level:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, xdoor.wall, xdoor.offset, xdoor.width)
            xkind = getattr(xdoor, "kind", "entry")
            overhead = xkind == "overhead"
            double = xkind in ("double", "french")
            half = xdoor.width / 2.0
            if xdoor.wall in (Direction.NORTH, Direction.SOUTH):
                if overhead:
                    sgn = 1.0 if xdoor.wall is Direction.SOUTH else -1.0
                    self._overhead_symbol(min(x1, x2), y1, "h", xdoor.width, sgn)
                elif double:  # two half-width leaves hinged at opposite jambs
                    lo = min(x1, x2)
                    self._door_symbol(lo, y1, "h", half)
                    self._door_symbol(lo + half, y1, "h", half, hinge_far=True)
                else:
                    self._door_symbol(min(x1, x2), y1, "h", xdoor.width)
            else:
                if overhead:
                    sgn = 1.0 if xdoor.wall is Direction.WEST else -1.0
                    self._overhead_symbol(x1, min(y1, y2), "v", xdoor.width, sgn)
                elif double:
                    lo = min(y1, y2)
                    self._door_symbol(x1, lo, "v", half)
                    self._door_symbol(x1, lo + half, "v", half, hinge_far=True)
                else:
                    self._door_symbol(x1, min(y1, y2), "v", xdoor.width)

    @staticmethod
    def _swing_sgn(door, a, b, edge) -> float | None:
        """+1/-1 for the side the leaf swings into, or None to let the symbol
        fall back to its keep-inside-the-envelope heuristic."""
        into = getattr(door, "swing_into", None)
        room = a if (into and into == a.id) else (b if (into and into == b.id) else None)
        if room is None:
            return None
        cx, cy = room.center
        return (1.0 if cx > edge.pos else -1.0) if edge.orientation == "v" else (
            1.0 if cy > edge.pos else -1.0
        )

    def _door_symbol(
        self,
        ox: float,
        oy: float,
        orientation: str,
        w: float,
        sgn: float | None = None,
        hinge_far: bool = False,
    ):
        """Draw a door at plan-space origin (ox, oy) as gap + leaf + swing arc.

        ``sgn`` picks the swing side (None → keep the leaf inside the envelope);
        ``hinge_far`` hinges at the far (high-coordinate) end of the opening
        instead of the near end.
        """
        if orientation == "v":  # wall runs in +y; swing into +x or -x
            if sgn is None:
                sgn = 1.0 if (ox + w) <= self.max_x else -1.0
            hinge = (ox, oy + w) if hinge_far else (ox, oy)
            latch = (ox, oy) if hinge_far else (ox, oy + w)
            tip = (ox + sgn * w, hinge[1])
        else:  # wall runs in +x; swing into +y or -y
            if sgn is None:
                sgn = 1.0 if (oy + w) <= self.max_y else -1.0
            hinge = (ox + w, oy) if hinge_far else (ox, oy)
            latch = (ox, oy) if hinge_far else (ox + w, oy)
            tip = (hinge[0], oy + sgn * w)

        hx, hy = self.sx(hinge[0]), self.sy(hinge[1])
        lx, ly = self.sx(latch[0]), self.sy(latch[1])
        tx, ty = self.sx(tip[0]), self.sy(tip[1])

        # White out the wall under the opening, draw the leaf, then the arc.
        self._line(hx, hy, lx, ly, "#ffffff", 4.0)
        self._line(hx, hy, tx, ty, WALL, 1.2)
        r = w * self.c.scale
        # Pick the sweep flag that centres the arc on the hinge, so the swing
        # always bulges *away* from it (convex). A fixed flag is right for only
        # half the orientation/hinge/side combinations — the rest read concave.
        sweep = self._arc_sweep((hx, hy), (tx, ty), (lx, ly), r)
        self._path(
            f"M {tx:.1f} {ty:.1f} A {r:.1f} {r:.1f} 0 0 {sweep} {lx:.1f} {ly:.1f}",
            "#999999", 0.8,
        )

    @staticmethod
    def _arc_sweep(hinge, tip, latch, r) -> int:
        """SVG sweep flag whose minor arc (large-arc 0) from ``tip`` to ``latch``
        is centred on ``hinge`` — the door pivots about the hinge, so its swing
        arc must be the one centred there. All points are in screen space."""
        (x1, y1), (x2, y2) = tip, latch
        dx, dy = (x1 - x2) / 2.0, (y1 - y2) / 2.0
        denom = dx * dx + dy * dy
        if denom < 1e-9:
            return 1
        coef = math.sqrt(max(0.0, (r * r) / denom - 1.0))
        # Centre SVG uses with sweep-flag 1 (large-arc 0 ⇒ sign = +1):
        cx = coef * dy + (x1 + x2) / 2.0
        cy = -coef * dx + (y1 + y2) / 2.0
        return 1 if abs(cx - hinge[0]) + abs(cy - hinge[1]) < 1e-6 else 0

    def _opening_symbol(self, ox: float, oy: float, orientation: str, w: float):
        """Draw a cased opening (walk-through) as a plain gap with jamb ticks.

        Unlike :meth:`_door_symbol` there is no leaf or swing arc — just the
        wall stopping at two jambs, which reads as an open passage.
        """
        if orientation == "v":  # wall runs in +y
            ends = ((ox, oy), (ox, oy + w))
        else:  # wall runs in +x
            ends = ((ox, oy), (ox + w, oy))

        (ax, ay), (bx, by) = ends
        # White out the wall under the opening.
        self._line(self.sx(ax), self.sy(ay), self.sx(bx), self.sy(by), "#ffffff", 4.0)
        # A short jamb tick perpendicular to the wall at each end.
        t = 3.5
        for px, py in ends:
            sx0, sy0 = self.sx(px), self.sy(py)
            if orientation == "v":
                self._line(sx0 - t, sy0, sx0 + t, sy0, WALL, 1.2)
            else:
                self._line(sx0, sy0 - t, sx0, sy0 + t, WALL, 1.2)

    def _slide_symbol(self, ox: float, oy: float, orientation: str, w: float):
        """Draw a pocket/sliding door: the gap plus a slab line parallel to the
        wall, set just inside one room (no swing arc)."""
        d = 0.35  # how far the panel sits off the wall, ft
        if orientation == "v":  # wall runs in +y at x=ox
            s = d if (ox + d) <= self.max_x else -d
            self._line(self.sx(ox), self.sy(oy), self.sx(ox), self.sy(oy + w), "#ffffff", 4.0)
            self._line(self.sx(ox + s), self.sy(oy), self.sx(ox + s), self.sy(oy + w), WALL, 1.6)
        else:  # wall runs in +x at y=oy
            s = d if (oy + d) <= self.max_y else -d
            self._line(self.sx(ox), self.sy(oy), self.sx(ox + w), self.sy(oy), "#ffffff", 4.0)
            self._line(self.sx(ox), self.sy(oy + s), self.sx(ox + w), self.sy(oy + s), WALL, 1.6)

    def _overhead_symbol(self, ox: float, oy: float, orientation: str, w: float, sgn: float):
        """Draw an overhead/sectional garage door: the gap plus a dashed track
        line set just inside the room (the segmented panel riding its tracks —
        no leaf, no swing arc). ``sgn`` points into the room (+x/+y is +1)."""
        d = 0.5 * sgn  # how far the track line sits inside the room, ft
        if orientation == "v":  # wall runs in +y at x=ox
            self._line(self.sx(ox), self.sy(oy), self.sx(ox), self.sy(oy + w), "#ffffff", 4.0)
            self._line(
                self.sx(ox + d), self.sy(oy), self.sx(ox + d), self.sy(oy + w),
                WALL, 1.6, dash="5 3",
            )
        else:  # wall runs in +x at y=oy
            self._line(self.sx(ox), self.sy(oy), self.sx(ox + w), self.sy(oy), "#ffffff", 4.0)
            self._line(
                self.sx(ox), self.sy(oy + d), self.sx(ox + w), self.sy(oy + d),
                WALL, 1.6, dash="5 3",
            )

    def _draw_stairs(self, level: int):
        for s in self.plan.stairs:
            if level not in (s.from_level, s.to_level):
                continue
            is_run = level == s.from_level
            x = self.sx(s.x)
            y = self.sy(s.y2)
            w = s.width * self.c.scale
            h = s.length * self.c.scale
            self._rect(
                x, y, w, h,
                fill="#E9E2D0" if is_run else "#F4F0E6", stroke=WALL, sw=1.2,
                dash=None if is_run else "4 3",
            )
            cx = self.sx(s.x + s.width / 2)
            cy = self.sy(s.y + s.length / 2)
            if is_run:
                # Tread lines across the run, plus an "UP" marker.
                for k in range(1, 6):
                    ty = y + h * k / 6
                    self._line(x, ty, x + w, ty, "#b9ab86", 0.8)
                self._text(cx, cy, f"{s.display_name} ↑{s.to_level}", size=9, fill="#6b5d3a")
            else:
                self._text(cx, cy, f"{s.display_name} ↓{s.from_level}", size=9, fill="#8a7f63")

    def _draw_notes(self, level: int = 0):
        """Draw positioned notes on ``level`` as small italic leader callouts.

        Each note is a filled dot at its world anchor, a 45° leader up-and-right
        (NE) to the text, and the text set in a muted note colour. No collision
        avoidance this pass — the offset is fixed, professional and unobtrusive."""
        for nm in getattr(self.plan, "note_marks", None) or []:
            if getattr(nm, "level", 0) != level:
                continue
            ax, ay = self.sx(nm.x), self.sy(nm.y)
            # Leader: a short 45° run to the NE (screen +x, −y), then the text.
            lead = 16.0
            tx, ty = ax + lead, ay - lead
            self.parts.append(
                f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="2.4" fill="{NOTE_COLOR}" />'
            )
            self._line(ax, ay, tx, ty, NOTE_COLOR, 1.0)
            self.parts.append(
                f'<text x="{tx + 3:.1f}" y="{ty - 1:.1f}" font-family="{self.c.font}" '
                f'font-size="10" fill="{NOTE_COLOR}" text-anchor="start" '
                f'font-style="italic">{escape(nm.text)}</text>'
            )

    def _draw_scale_bar(self):
        """Draw the graphic scale bar (bottom-left) + optional scale statement.

        Alternating filled/empty 5 ft segments over a 10 ft run, labelled 0/5/10,
        drawn in plan px (``scale`` px/ft) so it scales exactly with the drawing —
        the mark that survives any reprographic resize of the printed sheet."""
        x0 = self.c.margin_left
        y0 = self._scale_bar_y
        ppf = self.c.scale
        seg_ft, n_seg = 5.0, 2
        bar_h = 5.0
        if self.c.scale_note:
            self._text(
                x0, y0 - 4, self.c.scale_note, size=10, anchor="start",
                weight="bold", fill=TEXT_COLOR,
            )
        for i in range(n_seg):
            sx = x0 + i * seg_ft * ppf
            fill = NOTE_COLOR if i % 2 == 0 else "#ffffff"
            self._rect(sx, y0, seg_ft * ppf, bar_h, fill=fill, stroke=WALL, sw=0.8)
        for i in range(n_seg + 1):
            sx = x0 + i * seg_ft * ppf
            self._text(sx, y0 + bar_h + 11, f"{int(i * seg_ft)}", size=8, fill=TEXT_COLOR)
        self._text(
            x0 + n_seg * seg_ft * ppf + 16, y0 + bar_h, "FEET",
            size=8, anchor="start", fill=TEXT_COLOR,
        )

    def _draw_dimensions(self):
        # Overall dimensions span the whole footprint (incl. wings), not just the
        # primary envelope block.
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        # Overall width dimension below the plan.
        y = self.top + self.content_h + 28
        self._dim_line(
            self.sx(fx0), y, self.sx(fx1), y, fmt_ft_in(fx1 - fx0), horizontal=True
        )
        # Overall length dimension left of the plan.
        x = self.c.margin_left - 34
        self._dim_line(
            x, self.sy(fy0), x, self.sy(fy1), fmt_ft_in(fy1 - fy0), horizontal=False
        )

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

    # -- chained per-side exterior dimension strings -----------------------

    #: Offset (px) of a chain dimension line from its exterior wall — inside the
    #: overall dimension line (28/34 px out), so the two rows read as one family.
    _CHAIN_OFFSET = 15.0
    _CHAIN_TICK = 4.0

    def _chain_breaks(
        self,
        side: str,
        rooms: list,
        fx0: float,
        fy0: float,
        fx1: float,
        fy1: float,
        tol: float = 1e-6,
    ) -> tuple[list[float], float, float]:
        """Break points partitioning one exterior ``side`` (N/S/E/W).

        Collect where the edges of rooms *touching* that exterior wall project
        onto it, add the envelope span ends, then sort, clamp and dedupe. Rooms
        inset from the wall contribute nothing (their edges aren't on it), so a
        side no room reaches back onto degrades to the bare span (one segment).
        Returns ``(points, span_lo, span_hi)``.
        """
        if side in ("S", "N"):
            lo, hi = fx0, fx1
            if side == "S":
                touch = [r for r in rooms if abs(r.y - fy0) <= tol]
            else:
                touch = [r for r in rooms if abs(r.y2 - fy1) <= tol]
            raw = [c for r in touch for c in (r.x, r.x2)]
        else:
            lo, hi = fy0, fy1
            if side == "W":
                touch = [r for r in rooms if abs(r.x - fx0) <= tol]
            else:
                touch = [r for r in rooms if abs(r.x2 - fx1) <= tol]
            raw = [c for r in touch for c in (r.y, r.y2)]
        pts: list[float] = []
        for c in sorted([lo, hi, *raw]):
            c = min(max(c, lo), hi)
            if not pts or c - pts[-1] > tol:
                pts.append(c)
        return pts, lo, hi

    def _level_has_north_chain(self, level: int) -> bool:
        rooms = [r for r in self.plan.rooms if r.level == level]
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        if not self.plan.wings:
            pts, _, _ = self._chain_breaks("N", rooms, fx0, fy0, fx1, fy1)
            return len(pts) > 2
        # Wing plans: only a chain on the top-most north run (offset == max_y)
        # rides in the title band — an inset wing run sits in the notch, clear.
        for offset, lo, hi in self._exterior_runs("N"):
            if abs(offset - fy1) <= 1e-6 and len(self._run_breaks(
                "N", rooms, offset, lo, hi
            )) > 2:
                return True
        return False

    def _exterior_runs(self, side: str) -> list[tuple[float, float, float]]:
        """Distinct colinear exterior wall runs facing ``side`` (S/N/W/E).

        Each run is ``(offset, lo, hi)``: its wall coordinate (y for S/N, x for
        W/E) and the span it covers on the perpendicular axis. A plain rectangle
        yields exactly one run per side (the bounds edge), so wing-free plans keep
        the old single-chain-per-side behaviour; an L/T/U footprint yields one run
        per notched face, each at its own wall offset.
        """
        from .geometry import footprint_boundary, point_in_footprint

        sections = self.plan.footprint_sections()
        eps = 1e-3
        intervals: dict[float, list[tuple[float, float]]] = {}
        for (x1, y1), (x2, y2) in footprint_boundary(sections):
            if side in ("S", "N"):
                if abs(y1 - y2) > 1e-9:
                    continue  # want a horizontal edge
                offset = y1
                mid = (x1 + x2) / 2.0
                inside_hi = point_in_footprint(sections, mid, offset + eps)
                inside_lo = point_in_footprint(sections, mid, offset - eps)
                faces = (
                    "S" if inside_hi and not inside_lo
                    else "N" if inside_lo and not inside_hi else None
                )
                a, b = sorted((x1, x2))
            else:
                if abs(x1 - x2) > 1e-9:
                    continue  # want a vertical edge
                offset = x1
                mid = (y1 + y2) / 2.0
                inside_hi = point_in_footprint(sections, offset + eps, mid)
                inside_lo = point_in_footprint(sections, offset - eps, mid)
                faces = (
                    "W" if inside_hi and not inside_lo
                    else "E" if inside_lo and not inside_hi else None
                )
                a, b = sorted((y1, y2))
            if faces != side:
                continue
            intervals.setdefault(offset, []).append((a, b))
        runs: list[tuple[float, float, float]] = []
        for offset, ivs in intervals.items():
            ivs.sort()
            cur_lo, cur_hi = ivs[0]
            for lo, hi in ivs[1:]:
                if lo <= cur_hi + 1e-9:
                    cur_hi = max(cur_hi, hi)
                else:
                    runs.append((offset, cur_lo, cur_hi))
                    cur_lo, cur_hi = lo, hi
            runs.append((offset, cur_lo, cur_hi))
        runs.sort()
        return runs

    def _run_breaks(
        self,
        side: str,
        rooms: list,
        offset: float,
        lo: float,
        hi: float,
        tol: float = 1e-6,
    ) -> list[float]:
        """Break points partitioning one exterior run (``offset``, span ``[lo,hi]``).

        Like :meth:`_chain_breaks` but keyed to a specific wall run: a room
        contributes only when its wall lies on this run's ``offset`` *and* its
        extent overlaps ``[lo, hi]`` — so a wing's north run collects only the
        rooms backing that wing, not rooms on the deeper main-block wall.
        """
        if side in ("S", "N"):
            edge = (lambda r: r.y) if side == "S" else (lambda r: r.y2)
            touch = [
                r for r in rooms
                if abs(edge(r) - offset) <= tol
                and min(r.x2, hi) - max(r.x, lo) > tol
            ]
            raw = [c for r in touch for c in (r.x, r.x2)]
        else:
            edge = (lambda r: r.x) if side == "W" else (lambda r: r.x2)
            touch = [
                r for r in rooms
                if abs(edge(r) - offset) <= tol
                and min(r.y2, hi) - max(r.y, lo) > tol
            ]
            raw = [c for r in touch for c in (r.y, r.y2)]
        pts: list[float] = []
        for c in sorted([lo, hi, *raw]):
            c = min(max(c, lo), hi)
            if not pts or c - pts[-1] > tol:
                pts.append(c)
        return pts

    @staticmethod
    def _label_min_px(label: str) -> float:
        """Rough pixel run a size-9 segment label needs (skip it below this)."""
        return len(label) * 5.5

    def _draw_chain_dims(self, level: int | None = None) -> None:
        """Draw a chained dimension string along each exterior side that has an
        interior break — a run of tick-to-tick segments between the wall and the
        overall dimension line. Sides with no interior break are left to the
        overall dimension (no duplicated single-segment string)."""
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        rooms = [r for r in self.plan.rooms if level is None or r.level == level]
        if self.plan.wings:
            # L/T/U footprint: chain along each notched exterior run at its own
            # wall offset, not the rectangular bounds.
            for side in ("S", "N", "W", "E"):
                for offset, lo, hi in self._exterior_runs(side):
                    pts = self._run_breaks(side, rooms, offset, lo, hi)
                    if len(pts) <= 2:
                        continue
                    self._chain_string(side, pts, offset)
            return
        for side in ("S", "N", "W", "E"):
            pts, _, _ = self._chain_breaks(side, rooms, fx0, fy0, fx1, fy1)
            if len(pts) <= 2:
                continue  # no interior break — the overall dim already covers it
            wall = {"S": fy0, "N": fy1, "W": fx0, "E": fx1}[side]
            self._chain_string(side, pts, wall)

    def _chain_string(self, side: str, pts: list[float], wall_coord: float) -> None:
        off, tick = self._CHAIN_OFFSET, self._CHAIN_TICK
        if side in ("S", "N"):
            wy = self.sy(wall_coord)
            ly = wy + off if side == "S" else wy - off
            xs = [self.sx(p) for p in pts]
            self._line(xs[0], ly, xs[-1], ly, DIM_COLOR, 1.0)
            for x in xs:
                self._line(x, ly - tick, x, ly + tick, DIM_COLOR, 1.0)
            for a, b, xa, xb in zip(pts, pts[1:], xs, xs[1:]):
                label = fmt_ft_in(b - a)
                if (xb - xa) < self._label_min_px(label):
                    continue
                ty = ly - 3 if side == "S" else ly + 10
                self._text((xa + xb) / 2, ty, label, size=9, fill=DIM_COLOR)
        else:
            wx = self.sx(wall_coord)
            lx = wx - off if side == "W" else wx + off
            ys = [self.sy(p) for p in pts]
            self._line(lx, ys[0], lx, ys[-1], DIM_COLOR, 1.0)
            for y in ys:
                self._line(lx - tick, y, lx + tick, y, DIM_COLOR, 1.0)
            for a, b, ya, yb in zip(pts, pts[1:], ys, ys[1:]):
                label = fmt_ft_in(b - a)
                if abs(yb - ya) < self._label_min_px(label):
                    continue
                cy = (ya + yb) / 2
                tx = lx + 4 if side == "W" else lx - 4
                self.parts.append(
                    f'<text x="{tx:.1f}" y="{cy:.1f}" font-family="{self.c.font}" '
                    f'font-size="9" fill="{DIM_COLOR}" text-anchor="middle" '
                    f'transform="rotate(-90 {tx:.1f} {cy:.1f})">{escape(label)}</text>'
                )

    def _draw_panel(self, multi: bool = False):
        px = self.c.margin_left + self.content_w + self.c.gutter
        if multi:
            py = self.top + self.label_gap
            ph = (len(self.levels) - 1) * self.block_stride + self.content_h
        else:
            py = self.top
            ph = self.content_h
        # Grow the panel rect to enclose its content when the legend runs long.
        ph = max(ph, self._panel_ph)
        pw = self.c.panel_width
        m = self.plan.metrics()

        self._rect(px, py, pw, ph, fill="#FAFAFA", stroke="#DDDDDD", sw=1.0, rx=6)
        cx = px + 14
        y = py + 26
        self._text(cx, y, "PROJECT SUMMARY", size=12, anchor="start", weight="bold")
        y += 10

        rows = [
            ("Footprint", f"{m['footprint_sqft']:.0f} sq ft"),
            ("Interior (cond.)", f"{m['interior_sqft']:.0f} sq ft"),
            ("Habitable", f"{m['habitable_sqft']:.0f} sq ft"),
        ]
        if multi:
            for lvl, area in self.plan.area_by_level().items():
                rows.append((f"  Level {lvl} area", f"{area:.0f} sq ft"))
        rows += [
            ("Bedrooms", f"{int(m['bedroom_count'])}"),
            ("Bathrooms", f"{m['bathroom_count']:.1f}"),
            ("Ceiling", fmt_ft_in(self.plan.ceiling_height)),
            ("Ext. perimeter", f"{m['exterior_perimeter_ft']:.0f} ft"),
            ("Ext. wall area", f"{m['exterior_wall_area_sqft']:.0f} sq ft"),
            ("Roof area (≈)", f"{m['roof_area_sqft']:.0f} sq ft"),
            ("Foundation (≈)", f"{m['foundation_concrete_yd3']:.1f} cu yd"),
        ]
        if self.plan.frame_spec is not None or self.plan.posts:
            fs = self.plan.frame_spec
            rows += [
                ("Frames (bents)", f"{int(m['frame_count'])}"),
                ("Posts", f"{int(m['post_count'])}"),
                ("Beam length", f"{m['beam_linear_ft']:.0f} ft"),
            ]
            if fs is not None:
                rows.append(("Bay spacing", f"≤ {fs.bay:g}′ o.c."))
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


def render_site_svg(plan: Barndominium) -> str:
    """Render a site plan: the lot boundary, the required setback lines, and the
    building footprint placed on the lot, with a north arrow, the street side, and
    lot/setback dimensions in feet-and-inches.

    Requires a declared ``site`` (lot dimensions); returns a small placeholder
    otherwise. The building is drawn at its declared ``building at <x>,<y>``
    position, or centred on the lot when none is given (the setback check makes
    the same choice explicit). This is a lightweight standalone drawing — it does
    not use the floor-plan renderer's panel/scale machinery."""
    ss = plan.site_spec
    if ss is None or not ss.has_dims:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="60" '
            'viewBox="0 0 240 60"><rect width="240" height="60" fill="#ffffff" />'
            f'<text x="120" y="34" font-family="{RenderConfig().font}" font-size="12" '
            'fill="#888888" text-anchor="middle">No site declared — add `site '
            '&lt;W&gt; x &lt;L&gt;`.</text></svg>'
        )
    assert ss.width is not None and ss.length is not None  # has_dims guaranteed both
    lot_w = float(ss.width)
    lot_l = float(ss.length)

    # Footprint bounding box in plan coordinates (building + wings + porches).
    minx, miny, maxx, maxy = plan.bounds()
    for p in plan.porches:
        minx = min(minx, p.x)
        miny = min(miny, p.y)
        maxx = max(maxx, p.x + p.width)
        maxy = max(maxy, p.y + p.length)
    fp_w = maxx - minx
    fp_l = maxy - miny
    if ss.has_building:
        bx = float(ss.building_x or 0.0)
        by = float(ss.building_y or 0.0)
    else:  # centre the footprint's bbox on the lot
        bx = (lot_w - fp_w) / 2.0 - minx
        by = (lot_l - fp_l) / 2.0 - miny

    font = RenderConfig().font
    margin = 84.0
    top = 66.0
    avail = 520.0
    scale = avail / max(lot_w, lot_l) if max(lot_w, lot_l) > 0 else 1.0
    draw_w = lot_w * scale
    draw_h = lot_l * scale
    width = margin * 2 + draw_w
    height = top + draw_h + 66.0
    parts: list[str] = []

    def sx(x: float) -> float:
        return margin + x * scale

    def sy(y: float) -> float:
        return top + (lot_l - y) * scale

    def line(x1, y1, x2, y2, stroke, sw=1.0, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d} />'
        )

    def text(x, y, s, size=11, anchor="middle", fill=TEXT_COLOR, weight="normal"):
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{font}" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{escape(s)}</text>'
        )

    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">'
    )
    parts.append(f'<rect width="{width:.0f}" height="{height:.0f}" fill="#ffffff" />')
    text(margin, 32, f"Site Plan — {plan.name}", size=20, anchor="start", weight="bold")
    text(margin, 52, f"Lot {fmt_ft_in(lot_w)} × {fmt_ft_in(lot_l)}", size=12,
         anchor="start", fill="#666666")

    # Lot boundary (heavy line).
    parts.append(
        f'<rect x="{sx(0):.1f}" y="{sy(lot_l):.1f}" width="{draw_w:.1f}" '
        f'height="{draw_h:.1f}" fill="#fafaf6" stroke="{WALL}" stroke-width="2.6" />'
    )

    # Setback lines (dashed, labelled), only for declared edges. front = south.
    def setback_edge(value, label, orient, at):
        if value is None:
            return
        if orient == "h":  # horizontal line at lot-y = at
            y = sy(at)
            line(sx(0), y, sx(lot_w), y, "#B23A48", sw=1.2, dash="6 4")
            ty = y - 4 if label.startswith("rear") else y + 13
            text(sx(lot_w) - 6, ty, f"{fmt_ft_in(value)} {label} setback", size=9,
                 anchor="end", fill="#B23A48")
        else:  # vertical line at lot-x = at
            x = sx(at)
            line(x, sy(lot_l), x, sy(0), "#B23A48", sw=1.2, dash="6 4")
            text(x + 3, sy(lot_l) + 26, f"{fmt_ft_in(value)} {label}", size=9,
                 anchor="start", fill="#B23A48")

    setback_edge(ss.front, "front", "h", ss.front or 0.0)
    setback_edge(ss.rear, "rear", "h", lot_l - (ss.rear or 0.0))
    setback_edge(ss.side, "W side", "v", ss.side or 0.0)
    setback_edge(ss.side, "E side", "v", lot_w - (ss.side or 0.0))

    # Building footprint on the lot (filled outline at the correct position).
    hatch = "site-hatch"
    parts.append(
        f'<defs><pattern id="{hatch}" width="7" height="7" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="7" '
        f'stroke="#9a8a63" stroke-width="0.8" /></pattern></defs>'
    )
    for (sxf, syf, sw_, sl_) in plan.footprint_sections():
        lx = bx + sxf
        ly = by + syf
        parts.append(
            f'<rect x="{sx(lx):.1f}" y="{sy(ly + sl_):.1f}" width="{sw_ * scale:.1f}" '
            f'height="{sl_ * scale:.1f}" fill="url(#{hatch})" stroke="{WALL}" '
            f'stroke-width="1.6" />'
        )
    # Porches (open outline, no hatch) so the projecting footprint reads.
    for p in plan.porches:
        parts.append(
            f'<rect x="{sx(bx + p.x):.1f}" y="{sy(by + p.y + p.length):.1f}" '
            f'width="{p.width * scale:.1f}" height="{p.length * scale:.1f}" '
            f'fill="none" stroke="{WALL}" stroke-width="1.0" stroke-dasharray="3 3" />'
        )
    # Building label at the footprint centre.
    text(sx(bx + (minx + maxx) / 2.0), sy(by + (miny + maxy) / 2.0) + 4, "BUILDING",
         size=11, weight="bold", fill="#5A3210")

    # Street side marker (reuse the `street` directive; front = south edge).
    st = plan.street
    if st is not None:
        col = "#8A8F98"
        if st in (Direction.SOUTH, Direction.NORTH):
            y = sy(0) if st is Direction.SOUTH else sy(lot_l)
            line(sx(0), y, sx(lot_w), y, col, sw=4.0)
            text(sx(lot_w / 2.0), y + (16 if st is Direction.SOUTH else -7), "STREET",
                 size=10, fill=col)
        else:
            x = sx(0) if st is Direction.WEST else sx(lot_w)
            line(x, sy(0), x, sy(lot_l), col, sw=4.0)
            text(x, sy(lot_l) - 6, "STREET", size=10, fill=col,
                 anchor="start" if st is Direction.WEST else "end")

    # North arrow (top-right of the drawing).
    theta = math.radians(plan.orientation or 0.0)
    cx, cy, r = width - 46.0, top + 4.0, 20.0
    dx, dy = -math.sin(theta), -math.cos(theta)
    px, py = -dy, dx
    parts.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="#ffffff" '
        f'stroke="#bbbbbb" stroke-width="1" />'
    )
    tipx, tipy = cx + dx * r, cy + dy * r
    line(cx - dx * r * 0.7, cy - dy * r * 0.7, tipx, tipy, WALL, sw=1.6)
    hbx, hby = tipx - dx * 7.0, tipy - dy * 7.0
    parts.append(
        f'<polygon points="{tipx:.1f},{tipy:.1f} {hbx + px * 4:.1f},{hby + py * 4:.1f} '
        f'{hbx - px * 4:.1f},{hby - py * 4:.1f}" fill="{WALL}" />'
    )
    text(cx + dx * (r + 9), cy + dy * (r + 9) + 3, "N", size=11, weight="bold")

    # Overall lot dimensions (bottom = width, left = length).
    dimcol = DIM_COLOR
    by_dim = sy(0) + 40
    line(sx(0), by_dim, sx(lot_w), by_dim, dimcol, sw=0.8)
    line(sx(0), by_dim - 4, sx(0), by_dim + 4, dimcol, sw=0.8)
    line(sx(lot_w), by_dim - 4, sx(lot_w), by_dim + 4, dimcol, sw=0.8)
    text(sx(lot_w / 2.0), by_dim - 5, fmt_ft_in(lot_w), size=10, fill=dimcol)
    lx_dim = sx(0) - 46
    line(lx_dim, sy(0), lx_dim, sy(lot_l), dimcol, sw=0.8)
    line(lx_dim - 4, sy(0), lx_dim + 4, sy(0), dimcol, sw=0.8)
    line(lx_dim - 4, sy(lot_l), lx_dim + 4, sy(lot_l), dimcol, sw=0.8)
    parts.append(
        f'<text x="{lx_dim - 6:.1f}" y="{(sy(0) + sy(lot_l)) / 2:.1f}" '
        f'font-family="{font}" font-size="10" fill="{dimcol}" text-anchor="middle" '
        f'transform="rotate(-90 {lx_dim - 6:.1f} {(sy(0) + sy(lot_l)) / 2:.1f})">'
        f'{escape(fmt_ft_in(lot_l))}</text>'
    )

    parts.append("</svg>")
    return "\n".join(parts)
