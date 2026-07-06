"""Export a plan to DXF — the CAD interchange format architects and drafters use.

This writes a **DXF R2000 (AC1015)** ASCII document by hand, so the core stays
dependency-free (no ``ezdxf``), consistent with the rest of the project. R2000 is
the oldest format that carries declared drawing **units** (``$INSUNITS``) and the
``LWPOLYLINE`` entity, so the export can be a real drawing — closed, hatchable
wall polygons on named AIA layers — rather than a massing underlay of loose lines.

What it emits (see :data:`LAYERS` for the layer table):

* **Walls** as *closed* ``LWPOLYLINE`` bodies with real thickness — the exterior
  shell as per-side bands centred on the envelope, interior partitions centred on
  each shared room edge — with door and window openings **cut out** of the band.
* **Doors** as a leaf line + a 90° swing ``ARC`` (mirroring the SVG's hinge/swing
  convention); **windows** as the classic sill / head / centre-glazing symbol.
* **Dimensions** in one of two flavors (``dims=``): the default ``"geometry"`` is
  *drawing geometry* — overall dims per side plus the exterior chain strings with
  jamb breaks (the same breaks the SVG draws) as dim lines, extension lines, ticks
  and ``TEXT`` on ``A-ANNO-DIMS``; ``"associative"`` emits real rotated-linear
  ``DIMENSION`` entities (one per segment) each backed by an anonymous ``*D<n>``
  block of that same exploded geometry, so a regenerating reader gets live dims
  and everyone else sees today's picture (see ``docs/AUTHORING.md``).
* **Fixtures**, **frame posts**, **porches/stairs**, **room name/area** and
  **notes**, each on its own layer.

Coordinates pass straight through: barndsl is feet with ``x`` east / ``y`` north,
and DXF's world plane is also y-up, so a plan drops into model space at 1 unit =
1 foot — the same pass-through the IFC and Revit exchanges rely on. Units are
declared imperial (``$INSUNITS = 2``, ``$MEASUREMENT = 0``, ``$LUNITS = 4``).

The document is **byte-reproducible**: a deterministic handle counter and a fixed
build order mean re-exporting an unchanged plan yields an identical file.

    from barndsl import compile_source
    from barndsl.dxf import to_dxf
    plan = compile_source(src).plan
    open("plan.dxf", "w").write(to_dxf(plan))
"""

from __future__ import annotations

import math

from .constants import EXTERIOR_WALL_THICKNESS
from .elements import Barndominium, Direction, Room
from .geometry import opening_endpoints, shared_edge
from .render import RenderConfig, _Renderer, fmt_ft_in
from .wallbodies import wall_bands

#: Layer name → AutoCAD Color Index. AIA CAD Layer Guidelines discipline
#: prefixes: ``A-`` architectural, ``S-`` structural. A drafter opening the file
#: finds the categories on the names they expect.
LAYERS: dict[str, int] = {
    "A-WALL": 7,        # white/black — wall bodies (exterior shell + partitions)
    "A-DOOR": 30,       # orange — door leaves & swing arcs
    "A-GLAZ": 5,        # blue — windows (glazing)
    "A-FLOR-FIXT": 8,   # grey — plumbing/kitchen fixtures & furniture
    "A-FLOR-OTLN": 9,   # light grey — porch & stair outlines (dashed)
    "A-AREA-IDEN": 3,   # green — room name / area identification text
    "A-ANNO-DIMS": 2,   # yellow — dimension lines, ticks, text
    "A-ANNO-NOTE": 4,   # cyan — leader notes
    "S-COLS": 6,        # magenta — structural frame posts & beams
}

#: Layers drawn with the DASHED linetype (reference outlines, not built edges).
_DASHED_LAYERS: frozenset[str] = frozenset({"A-FLOR-OTLN"})

#: How far (ft) the overall and chained dimension strings sit outside the wall.
_OVERALL_GAP = 3.0
_CHAIN_GAP = 1.5
#: Half the length of an architectural dimension tick (the 45° slash), in feet.
_TICK = 0.18
#: Text heights (ft) for the various annotations.
_ROOM_NAME_H = 0.7
_ROOM_AREA_H = 0.5
_DIM_H = 0.45
_FIXT_H = 0.3
_NOTE_H = 0.4


#: Vulgar-fraction glyphs (face-of-stud labels) → the ASCII fraction a DXF
#: TEXT carries cleanly ("11'-7 1/2\"").
_ASCII_FRAC = {
    "⅛": " 1/8", "¼": " 1/4", "⅜": " 3/8", "½": " 1/2",
    "⅝": " 5/8", "¾": " 3/4", "⅞": " 7/8",
}


def _dim_label(feet: float) -> str:
    """A feet-and-inches dimension label in ASCII feet/inch marks (``18'-6"``).

    Same measurement text the SVG chain dims draw, with the unicode prime glyphs
    (``′``/``″``) folded to ASCII ``'``/``"`` and any vulgar fraction (the
    face-of-stud ``½``/``¼``) spelled out (``1/2``) so a DXF TEXT stays ASCII."""
    s = fmt_ft_in(feet).replace("′", "'").replace("″", '"')
    for glyph, ascii_frac in _ASCII_FRAC.items():
        s = s.replace(glyph, ascii_frac)
    return s


def _num(v: float) -> str:
    """Format a coordinate with jamb-exact precision and no signed zero."""
    s = f"{v:.6f}"
    if s.startswith("-") and float(s) == 0.0:
        s = s[1:]
    return s


class _DxfWriter:
    """Assembles a deterministic AC1015 document, tracking a drawing extent."""

    def __init__(
        self, plan: Barndominium, dim_mode: str = "nominal", dims: str = "geometry"
    ) -> None:
        self.plan = plan
        self._handle = 0x100
        self._ents: list[str] = []
        # Entity primitives append to `_sink` owned by `_owner`; both default to
        # model space, but while capturing an anonymous dimension block's exploded
        # geometry they retarget the block's body list and its BLOCK_RECORD handle.
        self._sink = self._ents
        self._owner = ""  # set to the model-space handle in build()
        # Associative (`dims="associative"`) emits real DIMENSION entities backed by
        # anonymous `*D<n>` geometry blocks; "geometry" (default) explodes the dims
        # to loose lines/ticks/TEXT — byte-identical to the historical output.
        self._dims = dims
        self._dim_blocks: list[tuple[str, str, str, str, str]] = []
        self._dimno = 0
        self.minx = self.miny = math.inf
        self.maxx = self.maxy = -math.inf
        # A throwaway renderer supplies the (world-coordinate) chain-dimension
        # break computation, so the DXF chain matches the SVG's exactly — in
        # whichever dimension mode (nominal room lines vs face-of-stud).
        self._r = _Renderer(plan, RenderConfig(dim_mode=dim_mode))

    # -- low-level ---------------------------------------------------------

    def _h(self) -> str:
        self._handle += 1
        return f"{self._handle:X}"

    @staticmethod
    def _g(code: int, value: object) -> str:
        return f"{code}\n{value}\n"

    def _bbox(self, x: float, y: float) -> None:
        self.minx = min(self.minx, x)
        self.miny = min(self.miny, y)
        self.maxx = max(self.maxx, x)
        self.maxy = max(self.maxy, y)

    # -- entity primitives (world feet; y-up, no flip) ---------------------

    def line(
        self, x1: float, y1: float, x2: float, y2: float, layer: str,
        *, linetype: str | None = None,
    ) -> None:
        self._bbox(x1, y1)
        self._bbox(x2, y2)
        g = self._g
        lt = g(6, linetype) if linetype else ""
        self._sink.append(
            g(0, "LINE") + g(5, self._h()) + g(330, self._owner) + g(100, "AcDbEntity")
            + g(8, layer) + lt + g(100, "AcDbLine")
            + g(10, _num(x1)) + g(20, _num(y1)) + g(30, "0.0")
            + g(11, _num(x2)) + g(21, _num(y2)) + g(31, "0.0")
        )

    def point(self, x: float, y: float, layer: str) -> None:
        """A POINT — used only for a DIMENSION block's definition points."""
        g = self._g
        self._sink.append(
            g(0, "POINT") + g(5, self._h()) + g(330, self._owner) + g(100, "AcDbEntity")
            + g(8, layer) + g(100, "AcDbPoint")
            + g(10, _num(x)) + g(20, _num(y)) + g(30, "0.0")
        )

    def polyline(self, pts: list[tuple[float, float]], layer: str) -> None:
        """A *closed* LWPOLYLINE (flag 1) — a hatchable wall/footprint polygon."""
        g = self._g
        body = (
            g(0, "LWPOLYLINE") + g(5, self._h()) + g(330, self._owner)
            + g(100, "AcDbEntity") + g(8, layer) + g(100, "AcDbPolyline")
            + g(90, len(pts)) + g(70, 1)
        )
        for x, y in pts:
            self._bbox(x, y)
            body += g(10, _num(x)) + g(20, _num(y))
        self._sink.append(body)

    def rect(self, x0: float, y0: float, x1: float, y1: float, layer: str) -> None:
        """A closed rectangle polyline from opposite corners."""
        self.polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer)

    def arc(
        self, cx: float, cy: float, r: float, start: float, end: float, layer: str
    ) -> None:
        """An ARC (degrees, CCW from ``start`` to ``end``) centred at (cx, cy)."""
        # Extent of the swept quarter-arc: cheap over-estimate via the box of the
        # centre ± r (the door swing never dominates the plan extent anyway).
        self._bbox(cx - r, cy - r)
        self._bbox(cx + r, cy + r)
        g = self._g
        self._sink.append(
            g(0, "ARC") + g(5, self._h()) + g(330, self._owner) + g(100, "AcDbEntity")
            + g(8, layer) + g(100, "AcDbCircle")
            + g(10, _num(cx)) + g(20, _num(cy)) + g(30, "0.0") + g(40, _num(r))
            + g(100, "AcDbArc") + g(50, _num(start)) + g(51, _num(end))
        )

    def circle(self, cx: float, cy: float, r: float, layer: str) -> None:
        self._bbox(cx - r, cy - r)
        self._bbox(cx + r, cy + r)
        g = self._g
        self._sink.append(
            g(0, "CIRCLE") + g(5, self._h()) + g(330, self._owner) + g(100, "AcDbEntity")
            + g(8, layer) + g(100, "AcDbCircle")
            + g(10, _num(cx)) + g(20, _num(cy)) + g(30, "0.0") + g(40, _num(r))
        )

    def text(
        self,
        x: float,
        y: float,
        s: str,
        height: float,
        layer: str,
        *,
        center: bool = False,
        rotation: float = 0.0,
    ) -> None:
        # Keep the payload ASCII — a DXF R2000 string is codepage-bound and the
        # plan's labels (feet/inch marks, room names) sit inside ASCII already.
        safe = "".join(ch if ord(ch) < 128 else "'" for ch in s)
        # Rough label extent so the drawing extent (EXTMIN/EXTMAX) encloses text.
        w = len(safe) * height * 0.65
        if rotation == 90.0:
            self._bbox(x, y)
            self._bbox(x + height, y + w)
        else:
            self._bbox(x, y)
            self._bbox(x + w, y + height)
        g = self._g
        body = (
            g(0, "TEXT") + g(5, self._h()) + g(330, self._owner) + g(100, "AcDbEntity")
            + g(8, layer) + g(100, "AcDbText")
            + g(10, _num(x)) + g(20, _num(y)) + g(30, "0.0") + g(40, _num(height))
            + g(1, safe)
        )
        if rotation:
            body += g(50, _num(rotation))
        if center:
            # Group 72=1 (centre) makes the alignment point (11/21) the anchor.
            body += g(72, 1) + g(100, "AcDbText") + g(11, _num(x)) + g(21, _num(y))
        self._sink.append(body)

    # -- document assembly -------------------------------------------------

    def build(self) -> str:
        g = self._g
        # Structural handles, assigned in document order for determinism.
        self._t_vport = self._h()
        self._t_ltype = self._h()
        self._t_layer = self._h()
        self._t_style = self._h()
        self._t_view = self._h()
        self._t_ucs = self._h()
        self._t_appid = self._h()
        self._t_dimstyle = self._h()
        self._t_blkrec = self._h()
        self._msp = self._h()
        self._psp = self._h()
        self._owner = self._msp

        layer_names = self._layer_names()
        if self._dims == "associative":
            # Draw first so the anonymous `*D<n>` dimension blocks discovered while
            # annotating are known before the table/block strings are assembled.
            # (Handles here follow model-space entities rather than preceding them;
            # this flavor is never held to the geometry flavor's byte layout.)
            self._draw()
            tables = self._tables(layer_names)
            blocks = self._blocks()
        else:
            # Geometry flavor: the historical order, byte-for-byte.
            tables = self._tables(layer_names)
            blocks = self._blocks()
            self._draw()
        objects = self._objects()
        seed = f"{self._handle + 1:X}"

        header = (
            g(0, "SECTION") + g(2, "HEADER")
            + g(9, "$ACADVER") + g(1, "AC1015")
            + g(9, "$HANDSEED") + g(5, seed)
            + g(9, "$INSUNITS") + g(70, 2)       # feet
            + g(9, "$MEASUREMENT") + g(70, 0)    # English
            + g(9, "$LUNITS") + g(70, 4)         # architectural
            + g(9, "$EXTMIN")
            + g(10, _num(self.minx)) + g(20, _num(self.miny)) + g(30, "0.0")
            + g(9, "$EXTMAX")
            + g(10, _num(self.maxx)) + g(20, _num(self.maxy)) + g(30, "0.0")
            + g(0, "ENDSEC")
        )
        entities = (
            g(0, "SECTION") + g(2, "ENTITIES") + "".join(self._ents) + g(0, "ENDSEC")
        )
        return header + tables + blocks + entities + objects + g(0, "EOF")

    def _layer_names(self) -> list[str]:
        """Layer 0 (mandatory) plus a base layer per category, with a ``-L{n}``
        suffixed copy of the drawable layers for each floor above the ground."""
        names = ["0"]
        names += list(LAYERS)
        for lvl in self.plan.levels():
            if lvl == 0:
                continue
            names += [f"{n}{self._suffix(lvl)}" for n in LAYERS]
        if self._dims == "associative":
            # The AutoCAD convention layer that carries a DIMENSION's definition
            # points — non-plotting, so the defpoints never print.
            names.append("Defpoints")
        return names

    @staticmethod
    def _suffix(level: int) -> str:
        return "" if level == 0 else f"-L{level}"

    def _tables(self, layer_names: list[str]) -> str:
        g = self._g

        def table(name: str, thandle: str, count: int, body: str, extra: str = "") -> str:
            return (
                g(0, "TABLE") + g(2, name) + g(5, thandle) + g(330, 0)
                + g(100, "AcDbSymbolTable") + g(70, count) + extra + body + g(0, "ENDTAB")
            )

        vport = (
            g(0, "VPORT") + g(5, self._h()) + g(330, self._t_vport)
            + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbViewportTableRecord")
            + g(2, "*Active") + g(70, 0)
            + g(10, "0.0") + g(20, "0.0") + g(11, "1.0") + g(21, "1.0")
            + g(12, "0.0") + g(22, "0.0") + g(13, "0.0") + g(23, "0.0")
            + g(14, "0.5") + g(24, "0.5") + g(15, "0.0") + g(25, "0.0")
            + g(16, "0.0") + g(26, "0.0") + g(36, "1.0")
            + g(17, "0.0") + g(27, "0.0") + g(37, "0.0")
            + g(40, "20.0") + g(41, "1.5") + g(42, "50.0") + g(43, "0.0") + g(44, "0.0")
            + g(50, "0.0") + g(51, "0.0") + g(71, 0) + g(72, 100) + g(73, 1)
            + g(74, 3) + g(75, 0) + g(76, 0) + g(77, 0) + g(78, 0)
        )

        ltbody = ""
        for lname, desc, dashes in (
            ("ByBlock", "", ()),
            ("ByLayer", "", ()),
            ("Continuous", "Solid line", ()),
            ("DASHED", "Dashed _ _ _ _ _", (0.5, -0.25)),
        ):
            ltbody += (
                g(0, "LTYPE") + g(5, self._h()) + g(330, self._t_ltype)
                + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbLinetypeTableRecord")
                + g(2, lname) + g(70, 0) + g(3, desc) + g(72, 65) + g(73, len(dashes))
                + g(40, _num(sum(abs(d) for d in dashes)))
            )
            for d in dashes:
                ltbody += g(49, _num(d)) + g(74, 0)

        lay_body = ""
        for lname in layer_names:
            base = lname.rsplit("-L", 1)[0] if lname.startswith(tuple(LAYERS)) else lname
            if lname == "0" or lname == "Defpoints":
                color = 7
            else:
                color = LAYERS[lname.split("-L")[0] if "-L" in lname else lname]
            lt = "DASHED" if base in _DASHED_LAYERS else "Continuous"
            lay_body += (
                g(0, "LAYER") + g(5, self._h()) + g(330, self._t_layer)
                + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbLayerTableRecord")
                + g(2, lname) + g(70, 0) + g(62, color) + g(6, lt)
                # Defpoints never plots (290=0) — the AutoCAD non-printing convention.
                + (g(290, 0) if lname == "Defpoints" else "")
                + g(370, -3) + g(390, "F")
            )

        style = (
            g(0, "STYLE") + g(5, self._h()) + g(330, self._t_style)
            + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbTextStyleTableRecord")
            + g(2, "Standard") + g(70, 0) + g(40, "0.0") + g(41, "1.0") + g(50, "0.0")
            + g(71, 0) + g(42, "0.2") + g(3, "txt") + g(4, "")
        )
        appid = (
            g(0, "APPID") + g(5, self._h()) + g(330, self._t_appid)
            + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbRegAppTableRecord")
            + g(2, "ACAD") + g(70, 0)
        )
        dimstyle = (
            g(0, "DIMSTYLE") + g(105, self._h()) + g(330, self._t_dimstyle)
            + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbDimStyleTableRecord")
            + g(2, "Standard") + g(70, 0)
        )
        blkrec = ""
        for bname, rec in (("*Model_Space", self._msp), ("*Paper_Space", self._psp)):
            blkrec += (
                g(0, "BLOCK_RECORD") + g(5, rec) + g(330, self._t_blkrec)
                + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbBlockTableRecord")
                + g(2, bname)
            )
        # Anonymous dimension geometry blocks (`*D<n>`), in draw order.
        for name, rec, _begin, _end, _body in self._dim_blocks:
            blkrec += (
                g(0, "BLOCK_RECORD") + g(5, rec) + g(330, self._t_blkrec)
                + g(100, "AcDbSymbolTableRecord") + g(100, "AcDbBlockTableRecord")
                + g(2, name)
            )

        return (
            g(0, "SECTION") + g(2, "TABLES")
            + table("VPORT", self._t_vport, 1, vport)
            + table("LTYPE", self._t_ltype, 4, ltbody)
            + table("LAYER", self._t_layer, len(layer_names), lay_body)
            + table("STYLE", self._t_style, 1, style)
            + table("VIEW", self._t_view, 0, "")
            + table("UCS", self._t_ucs, 0, "")
            + table("APPID", self._t_appid, 1, appid)
            + table(
                "DIMSTYLE", self._t_dimstyle, 1, dimstyle,
                extra=g(100, "AcDbDimStyleTable") + g(71, 0),
            )
            + table("BLOCK_RECORD", self._t_blkrec, 2 + len(self._dim_blocks), blkrec)
            + g(0, "ENDSEC")
        )

    def _blocks(self) -> str:
        g = self._g

        def block(name: str, rec: str) -> str:
            return (
                g(0, "BLOCK") + g(5, self._h()) + g(330, rec) + g(100, "AcDbEntity")
                + g(8, "0") + g(100, "AcDbBlockBegin") + g(2, name) + g(70, 0)
                + g(10, "0.0") + g(20, "0.0") + g(30, "0.0") + g(3, name) + g(1, "")
                + g(0, "ENDBLK") + g(5, self._h()) + g(330, rec) + g(100, "AcDbEntity")
                + g(8, "0") + g(100, "AcDbBlockEnd")
            )

        def dim_block(name: str, rec: str, begin: str, end: str, body: str) -> str:
            # An anonymous block (flag 70=1) holding a dimension's exploded
            # geometry — the same lines/ticks/TEXT the geometry flavor draws, so a
            # non-regenerating viewer shows exactly today's picture.
            return (
                g(0, "BLOCK") + g(5, begin) + g(330, rec) + g(100, "AcDbEntity")
                + g(8, "0") + g(100, "AcDbBlockBegin") + g(2, name) + g(70, 1)
                + g(10, "0.0") + g(20, "0.0") + g(30, "0.0") + g(3, name) + g(1, "")
                + body
                + g(0, "ENDBLK") + g(5, end) + g(330, rec) + g(100, "AcDbEntity")
                + g(8, "0") + g(100, "AcDbBlockEnd")
            )

        return (
            g(0, "SECTION") + g(2, "BLOCKS")
            + block("*Model_Space", self._msp)
            + block("*Paper_Space", self._psp)
            + "".join(dim_block(*b) for b in self._dim_blocks)
            + g(0, "ENDSEC")
        )

    def _objects(self) -> str:
        g = self._g
        return (
            g(0, "SECTION") + g(2, "OBJECTS")
            + g(0, "DICTIONARY") + g(5, self._h()) + g(330, 0)
            + g(100, "AcDbDictionary") + g(281, 1)
            + g(0, "ENDSEC")
        )

    # -- geometry ----------------------------------------------------------

    def _draw(self) -> None:
        for lvl in self.plan.levels():
            self._draw_level(lvl)
        self._draw_dimensions()

    def _draw_level(self, level: int) -> None:
        sfx = self._suffix(level)
        rooms = [r for r in self.plan.rooms if r.level == level]
        self._walls(level, sfx)
        self._windows(level, sfx)
        self._doors(level, rooms, sfx)
        self._fixtures(rooms, sfx)
        self._room_text(rooms, sfx)
        self._structure(level, sfx)
        self._notes(level, sfx)
        if level == 0:
            self._porches(sfx)
        self._stairs(level, sfx)
        self._guards(level, sfx)

    def _guards(self, level: int, sfx: str) -> None:
        """Draw a guard rail along each open loft edge on ``level`` — the
        double-height edges the ``LOFT_GUARD`` check flags (IRC R312), as a thin
        double line on the outline layer. Segments come from the same
        :func:`barndsl.validation.loft_guard_edges` the SVG and the check use."""
        from .validation import loft_guard_edges

        layer = "A-FLOR-OTLN" + sfx
        off = 0.12  # ft between the doubled rail lines
        for lvl, x1, y1, x2, y2 in loft_guard_edges(self.plan):
            if lvl != level:
                continue
            if abs(x1 - x2) < abs(y1 - y2):  # vertical edge → offset in x
                self.line(x1 - off, y1, x2 - off, y2, layer)
                self.line(x1 + off, y1, x2 + off, y2, layer)
            else:                             # horizontal edge → offset in y
                self.line(x1, y1 - off, x2, y2 - off, layer)
                self.line(x1, y1 + off, x2, y2 + off, layer)

    # -- wall bodies -------------------------------------------------------

    def _walls(self, level: int, sfx: str) -> None:
        # The wall bodies — exterior shell + interior partitions, corners squared
        # and openings cut at the jambs — come from the shared geometry module, so
        # this export and the SVG floor plan draw byte-for-byte the same walls. The
        # band corner order is what :meth:`rect` consumes, keeping the file
        # reproducible.
        layer = "A-WALL" + sfx
        for band in wall_bands(self.plan, level):
            self.rect(band.x0, band.y0, band.x1, band.y1, layer)

    # -- windows -----------------------------------------------------------

    def _windows(self, level: int, sfx: str) -> None:
        layer = "A-GLAZ" + sfx
        half = EXTERIOR_WALL_THICKNESS / 2.0
        for win in self.plan.windows:
            room = self.plan.room(win.room)
            if room is None or room.level != level:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, win.wall, win.offset, win.width)
            if win.wall in (Direction.SOUTH, Direction.NORTH):
                cy = y1
                lo, hi = min(x1, x2), max(x1, x2)
                self.line(lo, cy - half, hi, cy - half, layer)  # outer face
                self.line(lo, cy + half, hi, cy + half, layer)  # inner face
                self.line(lo, cy, hi, cy, layer)                # glazing centre
                self.line(lo, cy - half, lo, cy + half, layer)  # jambs
                self.line(hi, cy - half, hi, cy + half, layer)
            else:
                cx = x1
                lo, hi = min(y1, y2), max(y1, y2)
                self.line(cx - half, lo, cx - half, hi, layer)
                self.line(cx + half, lo, cx + half, hi, layer)
                self.line(cx, lo, cx, hi, layer)
                self.line(cx - half, lo, cx + half, lo, layer)
                self.line(cx - half, hi, cx + half, hi, layer)

    # -- doors -------------------------------------------------------------

    def _doors(self, level: int, rooms: list[Room], sfx: str) -> None:
        layer = "A-DOOR" + sfx
        mx, my = self._swing_bounds()
        for door in self.plan.interior_doors:
            a, b = self.plan.room(door.room_a), self.plan.room(door.room_b)
            if not (a and b) or not (a.level == b.level == level):
                continue
            edge = shared_edge(a, b)
            if edge is None:
                continue
            w = min(door.width, edge.length)
            if door.offset is None:
                start = edge.mid - w / 2.0
            else:
                start = edge.lo + max(0.0, min(door.offset, edge.length - w))
            kind = door.kind
            ox, oy = (edge.pos, start) if edge.orientation == "v" else (start, edge.pos)
            if kind in ("swing", "double", "french"):
                sgn = self._swing_sgn(door, a, b, edge)
                if kind == "swing":
                    self._door_leaf(ox, oy, edge.orientation, w, layer, sgn, False, mx, my)
                else:
                    half = w / 2.0
                    self._door_leaf(ox, oy, edge.orientation, half, layer, sgn, False, mx, my)
                    if edge.orientation == "v":
                        self._door_leaf(ox, oy + half, edge.orientation, half, layer, sgn, True, mx, my)
                    else:
                        self._door_leaf(ox + half, oy, edge.orientation, half, layer, sgn, True, mx, my)
            elif kind in ("pocket", "sliding"):
                self._slide_leaf(ox, oy, edge.orientation, w, layer, mx, my)
            # cased opening: the wall gap already reads as a passage — no leaf.

        for xd in self.plan.exterior_doors:
            room = self.plan.room(xd.room)
            if room is None or room.level != level:
                continue
            x1, y1, x2, y2 = opening_endpoints(room, xd.wall, xd.offset, xd.width)
            horizontal = xd.wall in (Direction.SOUTH, Direction.NORTH)
            orient = "h" if horizontal else "v"
            ox, oy = (min(x1, x2), y1) if horizontal else (x1, min(y1, y2))
            if xd.kind == "overhead":
                self._overhead_leaf(ox, oy, orient, xd.width, xd.wall, layer)
            elif xd.kind in ("double", "french"):
                half = xd.width / 2.0
                self._door_leaf(ox, oy, orient, half, layer, None, False, mx, my)
                if horizontal:
                    self._door_leaf(ox + half, oy, orient, half, layer, None, True, mx, my)
                else:
                    self._door_leaf(ox, oy + half, orient, half, layer, None, True, mx, my)
            else:
                self._door_leaf(ox, oy, orient, xd.width, layer, None, False, mx, my)

    def _swing_bounds(self) -> tuple[float, float]:
        """The (max_x, max_y) the SVG renderer uses to keep a leaf inside the
        envelope — plan bounds widened by any out-of-envelope porch."""
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        xs = [fx1] + [p.x + p.width for p in self.plan.porches]
        ys = [fy1] + [p.y + p.length for p in self.plan.porches]
        return max(xs), max(ys)

    @staticmethod
    def _swing_sgn(door, a: Room, b: Room, edge) -> float | None:
        into = door.swing_into
        room = a if (into and into == a.id) else (b if (into and into == b.id) else None)
        if room is None:
            return None
        cx, cy = room.center
        if edge.orientation == "v":
            return 1.0 if cx > edge.pos else -1.0
        return 1.0 if cy > edge.pos else -1.0

    def _door_leaf(
        self,
        ox: float,
        oy: float,
        orientation: str,
        w: float,
        layer: str,
        sgn: float | None,
        hinge_far: bool,
        mx: float,
        my: float,
    ) -> None:
        """A door as an open leaf line plus a 90° swing arc, in world coords.

        Mirrors :meth:`barndsl.render._Renderer._door_symbol`: the leaf runs from
        the hinge to the open tip, the arc pivots about the hinge from tip to
        latch. ``sgn`` picks the swing side (``None`` keeps the leaf inside the
        envelope); ``hinge_far`` hinges at the high-coordinate jamb."""
        if orientation == "v":
            if sgn is None:
                sgn = 1.0 if (ox + w) <= mx else -1.0
            hinge = (ox, oy + w) if hinge_far else (ox, oy)
            latch = (ox, oy) if hinge_far else (ox, oy + w)
            tip = (ox + sgn * w, hinge[1])
        else:
            if sgn is None:
                sgn = 1.0 if (oy + w) <= my else -1.0
            hinge = (ox + w, oy) if hinge_far else (ox, oy)
            latch = (ox, oy) if hinge_far else (ox + w, oy)
            tip = (hinge[0], oy + sgn * w)
        self.line(hinge[0], hinge[1], tip[0], tip[1], layer)
        al = math.degrees(math.atan2(latch[1] - hinge[1], latch[0] - hinge[0])) % 360.0
        at = math.degrees(math.atan2(tip[1] - hinge[1], tip[0] - hinge[0])) % 360.0
        # DXF arcs sweep CCW start→end; pick the order that spans the 90° quarter.
        start, end = (al, at) if (at - al) % 360.0 <= 180.0 else (at, al)
        self.arc(hinge[0], hinge[1], w, start, end, layer)

    def _slide_leaf(
        self, ox: float, oy: float, orientation: str, w: float, layer: str,
        mx: float, my: float,
    ) -> None:
        """A pocket/sliding door: a slab line just inside the room (no arc)."""
        d = 0.35
        if orientation == "v":
            s = d if (ox + d) <= mx else -d
            self.line(ox + s, oy, ox + s, oy + w, layer)
        else:
            s = d if (oy + d) <= my else -d
            self.line(ox, oy + s, ox + w, oy + s, layer)

    def _overhead_leaf(
        self, ox: float, oy: float, orientation: str, w: float, wall: Direction,
        layer: str,
    ) -> None:
        """An overhead/sectional garage door glyph — the plan convention: the door
        panel drawn as a dashed line pair across the opening (the closed sectional
        leaf), plus a dashed track line just inside the room. Mirrors the SVG's
        :meth:`barndsl.render._Renderer._overhead_symbol` (dashed, no swing) as
        closely as the DXF vocabulary allows."""
        d = 0.5      # track offset inside the room, ft
        panel = 0.1  # half-thickness of the drawn panel across the opening, ft
        dash = "DASHED"
        if orientation == "h":
            s = d if wall is Direction.SOUTH else -d
            self.line(ox, oy - panel, ox + w, oy - panel, layer, linetype=dash)  # panel
            self.line(ox, oy + panel, ox + w, oy + panel, layer, linetype=dash)
            self.line(ox, oy + s, ox + w, oy + s, layer, linetype=dash)          # track
        else:
            s = d if wall is Direction.WEST else -d
            self.line(ox - panel, oy, ox - panel, oy + w, layer, linetype=dash)
            self.line(ox + panel, oy, ox + panel, oy + w, layer, linetype=dash)
            self.line(ox + s, oy, ox + s, oy + w, layer, linetype=dash)

    # -- fixtures / structure / outlines / text ----------------------------

    def _fixtures(self, rooms: list[Room], sfx: str) -> None:
        from .fixtures import resolve_room_fixtures

        layer = "A-FLOR-FIXT" + sfx
        for room in rooms:
            fixtures = list(resolve_room_fixtures(self.plan, room))
            counters = [f for f in fixtures if f.kind == "counter"]
            # Reuse the SVG renderer's counter-mitre computation (do NOT re-derive
            # the trim math): counters meeting in an L/U corner are trimmed to abut
            # along their run axis, with a 45° miter joint across the corner square.
            # With no mitred corner, `trimmed` is the counters unchanged and
            # `miters` is empty — so a plan without corner counters is byte-identical
            # to the historical output.
            trimmed, miters = self._r._miter_counters(room, counters)
            tmap = {id(c): t for c, t in zip(counters, trimmed)}
            for f in fixtures:
                d = tmap.get(id(f), f)
                if d.width <= 0.05 or d.length <= 0.05:
                    continue
                self.rect(d.x, d.y, d.x + d.width, d.y + d.length, layer)
                self.text(
                    d.x + 0.1, d.y + d.length / 2.0, d.kind.upper().replace("_", " "),
                    _FIXT_H, layer,
                )
            for x1, y1, x2, y2 in miters:
                self.line(x1, y1, x2, y2, layer)

    def _structure(self, level: int, sfx: str) -> None:
        layer = "S-COLS" + sfx
        for b in self.plan.beams:
            if getattr(b, "level", 0) == level:
                self.line(b.x1, b.y1, b.x2, b.y2, layer)
        for p in self.plan.posts:
            if getattr(p, "level", 0) != level:
                continue
            h = p.size / 2.0
            self.rect(p.x - h, p.y - h, p.x + h, p.y + h, layer)

    def _porches(self, sfx: str) -> None:
        layer = "A-FLOR-OTLN" + sfx
        for p in self.plan.porches:
            self.rect(p.x, p.y, p.x + p.width, p.y + p.length, layer)
            self.text(
                p.x + 0.4, p.y + p.length / 2.0, p.display_name.upper(),
                _FIXT_H, layer,
            )

    def _stairs(self, level: int, sfx: str) -> None:
        layer = "A-FLOR-OTLN" + sfx
        for s in self.plan.stairs:
            if level not in (s.from_level, s.to_level):
                continue
            self.rect(s.x, s.y, s.x + s.width, s.y + s.length, layer)
            # Tread lines across the run (drawn on the run's originating level).
            if level == s.from_level:
                for k in range(1, 6):
                    yy = s.y + s.length * k / 6.0
                    self.line(s.x, yy, s.x + s.width, yy, layer)

    def _room_text(self, rooms: list[Room], sfx: str) -> None:
        layer = "A-AREA-IDEN" + sfx
        for r in rooms:
            cx, cy = r.center
            self.text(cx, cy + 0.2, r.display_name.upper(), _ROOM_NAME_H, layer, center=True)
            self.text(cx, cy - 0.8, f"{r.area:.0f} SF", _ROOM_AREA_H, layer, center=True)

    def _notes(self, level: int, sfx: str) -> None:
        layer = "A-ANNO-NOTE" + sfx
        for nm in getattr(self.plan, "note_marks", None) or []:
            if getattr(nm, "level", 0) != level:
                continue
            lead = 1.5
            tx, ty = nm.x + lead, nm.y + lead
            self.circle(nm.x, nm.y, 0.15, layer)
            self.line(nm.x, nm.y, tx, ty, layer)
            self.text(tx + 0.2, ty, nm.text, _NOTE_H, layer)

    # -- dimensions (drawing geometry, not associative DIMENSION entities) --

    def _draw_dimensions(self) -> None:
        layer = "A-ANNO-DIMS"
        fx0, fy0, fx1, fy1 = self.plan.bounds()
        # Overall dims for each principal span.
        self._overall("S", fx0, fx1, fy0, layer)
        self._overall("W", fy0, fy1, fx0, layer)
        # Chain strings: reuse the renderer's world-coordinate break computation.
        rooms = [r for r in self.plan.rooms if r.level == 0]
        if self.plan.wings:
            for side in ("S", "N", "W", "E"):
                for offset, lo, hi in self._r._exterior_runs(side):
                    room_pts = self._r._run_breaks(side, rooms, offset, lo, hi)
                    jambs = self._r._opening_jambs(side, rooms, offset, lo, hi)
                    pts = self._r._chain_ticks(side, room_pts, jambs, lo, hi, level=0)
                    if len(pts) > 2:
                        self._chain(side, pts, offset, layer)
            return
        for side in ("S", "N", "W", "E"):
            room_pts, lo, hi = self._r._chain_breaks(side, rooms, fx0, fy0, fx1, fy1)
            wall = {"S": fy0, "N": fy1, "W": fx0, "E": fx1}[side]
            span = (fx0, fx1) if side in ("S", "N") else (fy0, fy1)
            jambs = self._r._opening_jambs(side, rooms, wall, *span)
            pts = self._r._chain_ticks(side, room_pts, jambs, lo, hi, level=0)
            if len(pts) > 2:
                self._chain(side, pts, wall, layer)

    def _overall(self, side: str, lo: float, hi: float, wall: float, layer: str) -> None:
        """The overall dimension string for one principal span, ``_OVERALL_GAP``
        outside its wall: extension lines, a dim line, ticks and a centred label.
        In faces mode the span runs outside-face to outside-face (Phase 18)."""
        lo2, hi2 = self._r._overall_span(lo, hi)
        self._dim_run(side, [lo2, hi2], wall, _OVERALL_GAP, layer, _DIM_H, ext=True)

    def _chain(self, side: str, pts: list[float], wall: float, layer: str) -> None:
        self._dim_run(side, pts, wall, _CHAIN_GAP, layer, _DIM_H * 0.8, ext=False)

    def _dim_run(
        self, side: str, pts: list[float], wall: float, gap: float, layer: str,
        h: float, ext: bool,
    ) -> None:
        horizontal = side in ("S", "N")
        outward = -1.0 if side in ("S", "W") else 1.0
        dpos = wall + outward * gap
        if self._dims == "associative":
            self._assoc_run(pts, wall, dpos, outward, layer, h, ext, horizontal)
            return
        lo, hi = pts[0], pts[-1]
        if horizontal:
            self.line(lo, dpos, hi, dpos, layer)  # dimension line
            for p in pts:
                self._tick(p, dpos, layer)
                if ext:
                    self.line(p, wall, p, dpos + outward * _TICK, layer)  # extension
            for a, b in zip(pts, pts[1:]):
                self.text((a + b) / 2.0, dpos + 0.15, _dim_label(b - a), h, layer, center=True)
        else:
            self.line(dpos, lo, dpos, hi, layer)
            for p in pts:
                self._tick(dpos, p, layer)
                if ext:
                    self.line(wall, p, dpos + outward * _TICK, p, layer)
            for a, b in zip(pts, pts[1:]):
                self.text(dpos - 0.15, (a + b) / 2.0, _dim_label(b - a), h, layer, rotation=90.0, center=True)

    def _tick(self, x: float, y: float, layer: str) -> None:
        """A 45° architectural dimension tick centred on (x, y)."""
        self.line(x - _TICK, y - _TICK, x + _TICK, y + _TICK, layer)

    # -- associative dimensions (real DIMENSION entities) -------------------

    def _assoc_run(
        self, pts: list[float], wall: float, dpos: float, outward: float,
        layer: str, h: float, ext: bool, horizontal: bool,
    ) -> None:
        """Emit one rotated-linear ``DIMENSION`` per segment of the run, each backed
        by an anonymous ``*D<n>`` block holding that segment's exploded geometry —
        the same dim line, ticks, extension lines and TEXT the geometry flavor
        draws, so a non-regenerating viewer sees today's picture while a
        regenerating one (AutoCAD/BricsCAD) gets a live, editable dimension.

        The blocks (and their ``*D<n>`` names) are created in draw order, so both
        the names and the handles stay deterministic across runs."""
        for a, b in zip(pts, pts[1:]):
            self._dimno += 1
            name = f"*D{self._dimno}"
            rec = self._h()      # BLOCK_RECORD handle
            begin = self._h()    # BLOCK begin handle
            label = _dim_label(b - a)
            body: list[str] = []
            save_sink, save_owner = self._sink, self._owner
            self._sink, self._owner = body, rec
            if horizontal:
                self.line(a, dpos, b, dpos, layer)          # dim line segment
                self._tick(a, dpos, layer)
                self._tick(b, dpos, layer)
                if ext:
                    self.line(a, wall, a, dpos + outward * _TICK, layer)
                    self.line(b, wall, b, dpos + outward * _TICK, layer)
                self.text((a + b) / 2.0, dpos + 0.15, label, h, layer, center=True)
                self.point(a, dpos, "Defpoints")            # definition points
                self.point(a, wall, "Defpoints")
                self.point(b, wall, "Defpoints")
                base, p1, p2 = (a, dpos), (a, wall), (b, wall)
                tmid, angle = ((a + b) / 2.0, dpos), 0.0
            else:
                self.line(dpos, a, dpos, b, layer)
                self._tick(dpos, a, layer)
                self._tick(dpos, b, layer)
                if ext:
                    self.line(wall, a, dpos + outward * _TICK, a, layer)
                    self.line(wall, b, dpos + outward * _TICK, b, layer)
                self.text(
                    dpos - 0.15, (a + b) / 2.0, label, h, layer,
                    rotation=90.0, center=True,
                )
                self.point(dpos, a, "Defpoints")
                self.point(wall, a, "Defpoints")
                self.point(wall, b, "Defpoints")
                base, p1, p2 = (dpos, a), (wall, a), (wall, b)
                tmid, angle = (dpos, (a + b) / 2.0), 90.0
            self._sink, self._owner = save_sink, save_owner
            end = self._h()      # ENDBLK handle
            self._dim_blocks.append((name, rec, begin, end, "".join(body)))
            self._dimension(name, base, p1, p2, tmid, angle, label, layer)

    def _dimension(
        self, block: str, base: tuple[float, float], p1: tuple[float, float],
        p2: tuple[float, float], tmid: tuple[float, float], angle: float,
        text: str, layer: str,
    ) -> None:
        """A rotated-linear ``DIMENSION`` entity (dimtype 32 — the anonymous-block
        flavor). Group 2 names its ``*D<n>`` geometry block; 10/20 is a point on
        the dim line; 13/23 & 14/24 are the two measured points (extension-line
        origins); 50 is the run rotation; 1 is our explicit ft-in text, which
        overrides the value a regenerating reader would recompute so the displayed
        label always matches ours."""
        g = self._g
        safe = "".join(ch if ord(ch) < 128 else "'" for ch in text)
        for x, y in (base, p1, p2, tmid):
            self._bbox(x, y)
        self._ents.append(
            g(0, "DIMENSION") + g(5, self._h()) + g(330, self._msp)
            + g(100, "AcDbEntity") + g(8, layer) + g(100, "AcDbDimension")
            + g(2, block) + g(3, "Standard")
            + g(10, _num(base[0])) + g(20, _num(base[1])) + g(30, "0.0")
            + g(11, _num(tmid[0])) + g(21, _num(tmid[1])) + g(31, "0.0")
            + g(70, 32) + g(71, 5) + g(1, safe)
            + g(100, "AcDbAlignedDimension")
            + g(13, _num(p1[0])) + g(23, _num(p1[1])) + g(33, "0.0")
            + g(14, _num(p2[0])) + g(24, _num(p2[1])) + g(34, "0.0")
            + g(50, _num(angle))
            + g(100, "AcDbRotatedDimension")
        )


def to_dxf(
    plan: Barndominium, dim_mode: str = "nominal", dims: str = "geometry"
) -> str:
    """Return ``plan`` as a DXF R2000 (AC1015) document string.

    ``dim_mode`` picks the dimension convention (``"nominal"`` room lines or
    face-of-stud ``"faces"``); the dim geometry comes from the same renderer
    helpers the SVG uses, so the two exports stay in parity in both modes.

    ``dims`` picks the dimension *flavor*: ``"geometry"`` (default) explodes the
    dims to loose lines/ticks/TEXT on ``A-ANNO-DIMS`` — byte-identical to the
    historical output and rendered by every viewer; ``"associative"`` emits real
    rotated-linear ``DIMENSION`` entities, each backed by an anonymous ``*D<n>``
    block of that same exploded geometry, so a regenerating reader gets live dims
    while a non-regenerating one still sees today's picture."""
    return _DxfWriter(plan, dim_mode=dim_mode, dims=dims).build()


def save_dxf(
    plan: Barndominium, path: str, dim_mode: str = "nominal",
    dims: str = "geometry",
) -> str:
    """Write ``plan`` as DXF to ``path``. Returns the path."""
    with open(path, "w", encoding="ascii", errors="replace") as fh:
        fh.write(to_dxf(plan, dim_mode=dim_mode, dims=dims))
    return path
