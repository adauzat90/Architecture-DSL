"""Lower a compiled plan into **IFC4** and serialise it as a STEP (SPF) file.

IFC is the open BIM interchange: an ``.ifc`` file opens in full Revit, ArchiCAD,
BIMcollab/Solibri and every IFC viewer, so this is the *professional hand-off* —
and the final demotion of the pyRevit path to "one exporter among several".

Like :mod:`barndsl.dxf` (hand-written DXF, no ``ezdxf``) and :mod:`barndsl.gltf`
(hand-written glTF, no 3D library), this module writes IFC **by hand in pure
Python, stdlib only** — a tiny ISO-10303-21 (STEP Physical File) backbone plus an
IFC4 entity graph. IfcOpenShell is *not* an install dependency; it is used only as
an optional test-time validation oracle (``tests/test_ifc.py``). The whole model
is extruded rectangles and a handful of prisms, so a hand-rolled writer is the
better fit than a heavyweight geometry kernel — and it keeps the engine zero-dep.

It does **not** invent a new geometry layer. It reads the Revit-shaped exchange
from :func:`barndsl.revit.to_revit_model` — deduplicated wall centreline runs with
hosted openings, per-level slabs, a ``roof_plan``, stair flights, frame members,
porches and room seed rectangles — and lowers each record into IFC products.

Coordinates and units
---------------------
The plan is **feet**, ``x`` east / ``y`` north / ``z`` up, and IFC's world frame
is also right-handed z-up, so coordinates **pass straight through** (the same
pass-through the Revit exchange and the DXF export rely on). Units are declared
**imperial**: an ``IfcConversionBasedUnit`` foot (= 0.3048 m), square foot and
cubic foot in the ``IfcUnitAssignment``, so every coordinate in the file stays in
feet with no boundary conversion. ``tests/test_ifc.py`` pins the unit block.

Determinism
-----------
The output is byte-reproducible: no timestamps (a fixed epoch in ``FILE_NAME`` and
``IfcOwnerHistory``), no random GlobalIds. Every ``IfcRoot`` GlobalId is an IFC
22-character compressed GUID derived deterministically with :func:`uuid.uuid5`
from the plan name + element kind + element id, so re-exporting an unchanged plan
produces an identical file.

Geometry choices (schematic by design, like the elevations and the glTF):

* Walls, slabs, columns, beams, spaces, stair treads and door/window panels are
  ``IfcExtrudedAreaSolid`` of an ``IfcRectangleProfileDef`` (a box) — a vertical
  extrusion of the plan rectangle. A wall run's vertical extent comes from
  :mod:`barndsl.wallheights` (shared with the glTF export): on a multi-level plan
  a lower run rises to the level above where an upper floor covers it and to the
  roof plate where it does not, so there is no open band between stacked levels or
  between a wall and the roof. A run keeps its lengthwise pieces as several solids
  in one IfcWall.
* Openings are modelled the *correct* BIM way: the wall keeps its **uncut** box,
  an ``IfcOpeningElement`` (a box spanning the wall) voids it via
  ``IfcRelVoidsElement``, and an ``IfcDoor``/``IfcWindow`` fills it via
  ``IfcRelFillsElement`` with real ``OverallWidth``/``OverallHeight``. Viewers
  that honour voids show the hole; the relationships carry the schedule data
  regardless.
* Roof planes are **triangular/wedge prisms**: a gable is one triangular prism
  (``IfcArbitraryClosedProfileDef`` swept horizontally along the ridge), a shed a
  wedge, a monitor its three per-section prisms. Prisms are real solids every
  common viewer renders — no faceted-brep fallback needed.

Public API::

    to_ifc(plan)            # -> the IFC4 SPF document as a str
    write_ifc(plan, path)   # -> path
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass

from .constants import SLAB_THICKNESS
from .elements import Barndominium
from .revit import RevitModel, RevitOpening, RevitWall, to_revit_model
from .wallheights import wall_top_intervals

# --- deterministic IFC GlobalId (22-char compressed GUID) --------------------

#: IFC's own base-64 alphabet for the 22-character compressed GUID (buildingSMART
#: "IfcGloballyUniqueId"). Note ``_`` and ``$`` as the last two digits.
_GUID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"

#: A fixed namespace so :func:`uuid.uuid5` GUIDs are stable across runs and hosts.
_NAMESPACE = uuid.UUID("6ba7b829-9dad-11d1-80b4-00c04fd430c8")


def compress_guid(value: uuid.UUID) -> str:
    """Compress a 128-bit UUID into IFC's 22-character GlobalId encoding.

    The 128 bits split as ``2 + 21*6`` (128), most-significant first: the first
    character carries 2 bits (so it is always ``0``–``3``) and the remaining 21
    carry 6 bits each, drawn from :data:`_GUID_ALPHABET`. This matches
    ``ifcopenshell.guid.compress`` exactly (asserted by the oracle test).
    """
    num = value.int
    chars: list[str] = []
    for i in range(22):
        if i < 21:
            chars.append(_GUID_ALPHABET[num & 0x3F])
            num >>= 6
        else:
            chars.append(_GUID_ALPHABET[num & 0x03])
            num >>= 2
    return "".join(reversed(chars))


def _guid(plan_name: str, kind: str, ident: str) -> str:
    """A deterministic IFC GlobalId from the plan name, element kind and id."""
    return compress_guid(uuid.uuid5(_NAMESPACE, f"{plan_name}|{kind}|{ident}"))


# --- STEP (ISO-10303-21) value serialisation ---------------------------------


@dataclass(frozen=True)
class Ref:
    """A reference to an entity instance (``#42``)."""

    id: int


@dataclass(frozen=True)
class Enum:
    """A STEP enumeration literal (``.AREA.``)."""

    name: str


@dataclass(frozen=True)
class Typed:
    """A typed value / defined-type wrapper (``IFCLENGTHMEASURE(0.3048)``)."""

    type: str
    value: object


class _Star:
    """The STEP ``*`` token — an inherited attribute derived in a subtype."""


STAR = _Star()


def _fmt_real(v: float) -> str:
    """Format a float as a STEP REAL — always carrying a decimal point, no ``e``."""
    x = float(v)
    if not math.isfinite(x):
        raise ValueError(f"non-finite coordinate: {v!r}")
    if x == 0.0:
        return "0."
    s = f"{x:.9f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return s


def _step_string(s: str) -> str:
    """Encode a Python string as a STEP string literal.

    Printable ASCII passes through with ``'`` doubled and ``\\`` escaped;
    non-ASCII runs are encoded with IFC's ``\\X2\\``…``\\X0\\`` control directive
    as big-endian UTF-16 hex (4 digits per BMP code unit, 8 for a surrogate pair),
    so an arbitrary plan title survives a round-trip through any conformant reader.
    """
    parts: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            parts.append("\\X2\\" + raw.hex().upper() + "\\X0\\")
            buf.clear()

    for ch in s:
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            flush()
            if ch == "'":
                parts.append("''")
            elif ch == "\\":
                parts.append("\\\\")
            else:
                parts.append(ch)
        else:
            buf.append(ch)
    flush()
    return "'" + "".join(parts) + "'"


def _fmt(v: object) -> str:
    """Serialise one attribute value to its STEP text."""
    if v is None:
        return "$"
    if isinstance(v, Ref):
        return f"#{v.id}"
    if isinstance(v, _Star):
        return "*"
    if isinstance(v, Enum):
        return f".{v.name}."
    if isinstance(v, Typed):
        return f"{v.type}({_fmt(v.value)})"
    if isinstance(v, bool):
        return ".T." if v else ".F."
    if isinstance(v, float):
        return _fmt_real(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _step_string(v)
    if isinstance(v, (list, tuple)):
        return "(" + ",".join(_fmt(x) for x in v) + ")"
    raise TypeError(f"cannot serialise {type(v).__name__} to STEP")


class _Spf:
    """A minimal STEP Physical File entity table.

    :meth:`add` appends an instance and returns its :class:`Ref`; geometry
    primitives (points, directions, 2D placements) are cached by value so a plan
    reuses shared origins/axes instead of exploding the file.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._n = 0
        self._cache: dict[tuple, Ref] = {}

    def add(self, type_name: str, *params: object) -> Ref:
        self._n += 1
        ref = Ref(self._n)
        self.lines.append(f"#{self._n}={type_name}({','.join(_fmt(p) for p in params)});")
        return ref

    def _cached(self, key: tuple, factory) -> Ref:
        ref = self._cache.get(key)
        if ref is None:
            ref = factory()
            self._cache[key] = ref
        return ref

    def point3(self, x: float, y: float, z: float) -> Ref:
        key = ("p3", round(x, 7), round(y, 7), round(z, 7))
        return self._cached(
            key, lambda: self.add("IFCCARTESIANPOINT", [float(x), float(y), float(z)])
        )

    def point2(self, x: float, y: float) -> Ref:
        key = ("p2", round(x, 7), round(y, 7))
        return self._cached(key, lambda: self.add("IFCCARTESIANPOINT", [float(x), float(y)]))

    def dir3(self, x: float, y: float, z: float) -> Ref:
        key = ("d3", x, y, z)
        return self._cached(
            key, lambda: self.add("IFCDIRECTION", [float(x), float(y), float(z)])
        )


# --- geometry helpers (all in world feet) ------------------------------------


def _box_solid(spf: _Spf, x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Ref:
    """An axis-aligned box as an ``IfcExtrudedAreaSolid`` of a rectangle profile."""
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    xd, yd, depth = abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)
    prof_pos = spf.add("IFCAXIS2PLACEMENT2D", spf.point2(cx, cy), None)
    prof = spf.add("IFCRECTANGLEPROFILEDEF", Enum("AREA"), None, prof_pos, float(xd), float(yd))
    pos = spf.add("IFCAXIS2PLACEMENT3D", spf.point3(0.0, 0.0, min(z0, z1)), None, None)
    return spf.add("IFCEXTRUDEDAREASOLID", prof, pos, spf.dir3(0.0, 0.0, 1.0), float(depth))


def _prism_solid(spf: _Spf, section: list[tuple[float, float]], axis: str, lo: float, hi: float) -> Ref:
    """A horizontal prism: ``section`` (``(horizontal, z)`` points in the vertical
    cross-section) swept along ``axis`` from ``lo`` to ``hi``.

    ``axis == "y"`` sweeps north (``horizontal`` is world ``x``); ``axis == "x"``
    sweeps east (``horizontal`` is world ``y``). This is the gable/shed roof
    lowering — an ``IfcArbitraryClosedProfileDef`` swept horizontally.
    """
    depth = hi - lo
    if axis == "y":
        loc = spf.point3(0.0, lo, 0.0)
        pos = spf.add("IFCAXIS2PLACEMENT3D", loc, spf.dir3(0, 1, 0), spf.dir3(1, 0, 0))
        pts = [(h, -z) for (h, z) in section]  # local Y points to world -Z here
    else:
        loc = spf.point3(lo, 0.0, 0.0)
        pos = spf.add("IFCAXIS2PLACEMENT3D", loc, spf.dir3(1, 0, 0), spf.dir3(0, 1, 0))
        pts = [(h, z) for (h, z) in section]  # local Y points to world +Z
    poly_pts = [spf.point2(u, v) for (u, v) in pts]
    poly_pts.append(poly_pts[0])  # IfcPolyline outer curve must close
    curve = spf.add("IFCPOLYLINE", poly_pts)
    prof = spf.add("IFCARBITRARYCLOSEDPROFILEDEF", Enum("AREA"), None, curve)
    return spf.add("IFCEXTRUDEDAREASOLID", prof, pos, spf.dir3(0.0, 0.0, 1.0), float(depth))


def _block_bounds(block: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for seg in block["outline"]:
        for px, py in seg:
            xs.append(px)
            ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def _roof_block_solids(spf: _Spf, block: dict, eave_z: float, oh: float) -> list[Ref]:
    """The prism solid(s) for one roof block (gable → one triangle, shed → wedge)."""
    minx, miny, maxx, maxy = _block_bounds(block)
    rise = float(block["rise"])
    ridge_z = eave_z + rise
    ga = block["gable_axis"]
    style = block["style"]

    if style == "shed":
        slopes = block["outline_slopes"]  # order: south(0), east(1), north(2), west(3)
        if ga == "x":  # ridge along x; slope varies along y (world horizontal = y)
            low_h, high_h = (miny, maxy) if slopes[0] else (maxy, miny)
            section = [(low_h - oh, eave_z), (high_h + oh, eave_z), (high_h + oh, ridge_z)]
            return [_prism_solid(spf, section, "x", minx - oh, maxx + oh)]
        low_h, high_h = (minx, maxx) if slopes[3] else (maxx, minx)  # ga == "y"
        section = [(low_h - oh, eave_z), (high_h + oh, eave_z), (high_h + oh, ridge_z)]
        return [_prism_solid(spf, section, "y", miny - oh, maxy + oh)]

    # gable (and a monitor section's central gable): one triangular prism.
    if ga == "x":  # ridge along x at y = mid; cross-section spans y
        mid = (miny + maxy) / 2.0
        section = [(miny - oh, eave_z), (maxy + oh, eave_z), (mid, ridge_z)]
        return [_prism_solid(spf, section, "x", minx - oh, maxx + oh)]
    mid = (minx + maxx) / 2.0  # ridge along y at x = mid; cross-section spans x
    section = [(minx - oh, eave_z), (maxx + oh, eave_z), (mid, ridge_z)]
    return [_prism_solid(spf, section, "y", miny - oh, maxy + oh)]


# --- IFC4 mappings for the exchange enums ------------------------------------

#: Roof form → IfcRoofTypeEnum.
_ROOF_TYPE = {"gable": "GABLE_ROOF", "shed": "SHED_ROOF", "monitor": "FREEFORM"}

#: Stair layout (from ``plan_stair_runs``) → IfcStairTypeEnum.
_STAIR_TYPE = {
    "straight": "STRAIGHT_RUN_STAIR",
    "switchback": "HALF_TURN_STAIR",
    "overrun": "STRAIGHT_RUN_STAIR",
}

#: Nominal fixture heights (ft) for the IfcFurnishingElement box massing.
_FIXTURE_HEIGHT = {
    "toilet": 2.5, "lavatory": 2.85, "sink": 3.0, "tub": 2.0, "shower": 6.5,
    "refrigerator": 5.8, "range": 3.05, "washer": 3.0, "dryer": 3.0,
    "water_heater": 4.6, "kitchen_island": 3.05, "counter": 3.05,
    "bed_queen": 2.15, "bed_twin": 2.15, "sofa": 2.7, "armchair": 2.7,
    "dining_table": 2.4, "coffee_table": 1.4, "desk": 2.4, "dresser": 3.0,
    "wardrobe": 6.0,
}


# --- the builder -------------------------------------------------------------


class _Builder:
    """Assembles the IFC4 entity graph for one plan into an :class:`_Spf`."""

    def __init__(self, plan: Barndominium, model: RevitModel) -> None:
        self.plan = plan
        self.model = model
        self.name = plan.name
        self.spf = _Spf()
        #: Products to place on each level, plus the storey placement for each.
        self.by_level: dict[int, list[Ref]] = {}
        self.storey: dict[int, Ref] = {}
        self.storey_placement: dict[int, Ref] = {}

    def guid(self, kind: str, ident: str) -> str:
        return _guid(self.name, kind, ident)

    # -- shared scaffolding (units, context, owner history, spatial tree) --

    def _owner_history(self) -> Ref:
        s = self.spf
        person = s.add("IFCPERSON", None, "barndsl", None, None, None, None, None, None)
        org = s.add("IFCORGANIZATION", None, "barndsl", None, None, None)
        p_and_o = s.add("IFCPERSONANDORGANIZATION", person, org, None)
        from . import __version__  # lazy: barndsl.__init__ imports this module

        app = s.add("IFCAPPLICATION", org, __version__, "barndsl", "barndsl")
        # A fixed creation date (epoch 0) keeps the file byte-reproducible.
        return s.add("IFCOWNERHISTORY", p_and_o, app, None, Enum("ADDED"), 0, p_and_o, app, 0)

    def _units(self) -> Ref:
        s = self.spf
        length = s.add(
            "IFCCONVERSIONBASEDUNIT",
            s.add("IFCDIMENSIONALEXPONENTS", 1, 0, 0, 0, 0, 0, 0),
            Enum("LENGTHUNIT"),
            "foot",
            s.add(
                "IFCMEASUREWITHUNIT",
                Typed("IFCLENGTHMEASURE", 0.3048),
                s.add("IFCSIUNIT", STAR, Enum("LENGTHUNIT"), None, Enum("METRE")),
            ),
        )
        area = s.add(
            "IFCCONVERSIONBASEDUNIT",
            s.add("IFCDIMENSIONALEXPONENTS", 2, 0, 0, 0, 0, 0, 0),
            Enum("AREAUNIT"),
            "square foot",
            s.add(
                "IFCMEASUREWITHUNIT",
                Typed("IFCAREAMEASURE", 0.09290304),
                s.add("IFCSIUNIT", STAR, Enum("AREAUNIT"), None, Enum("SQUARE_METRE")),
            ),
        )
        volume = s.add(
            "IFCCONVERSIONBASEDUNIT",
            s.add("IFCDIMENSIONALEXPONENTS", 3, 0, 0, 0, 0, 0, 0),
            Enum("VOLUMEUNIT"),
            "cubic foot",
            s.add(
                "IFCMEASUREWITHUNIT",
                Typed("IFCVOLUMEMEASURE", 0.028316846592),
                s.add("IFCSIUNIT", STAR, Enum("VOLUMEUNIT"), None, Enum("CUBIC_METRE")),
            ),
        )
        angle = s.add("IFCSIUNIT", STAR, Enum("PLANEANGLEUNIT"), None, Enum("RADIAN"))
        return s.add("IFCUNITASSIGNMENT", [length, area, volume, angle])

    def _spatial_tree(self, owner: Ref, context: Ref, units: Ref) -> None:
        """IfcProject → IfcSite → IfcBuilding → per-level IfcBuildingStorey."""
        s = self.spf
        origin = s.point3(0.0, 0.0, 0.0)
        self.wcs = s.add(
            "IFCAXIS2PLACEMENT3D", origin, s.dir3(0, 0, 1), s.dir3(1, 0, 0)
        )
        site_pl = s.add("IFCLOCALPLACEMENT", None, self.wcs)
        bldg_pl = s.add("IFCLOCALPLACEMENT", site_pl, self.wcs)

        project = s.add(
            "IFCPROJECT", self.guid("project", ""), owner, self.name, None, None,
            None, None, [context], units,
        )
        site = s.add(
            "IFCSITE", self.guid("site", ""), owner, "Site", None, None, site_pl, None,
            None, Enum("ELEMENT"), None, None, None, None, None,
        )
        building = s.add(
            "IFCBUILDING", self.guid("building", ""), owner, "Building", None, None,
            bldg_pl, None, None, Enum("ELEMENT"), None, None, None,
        )
        self.building = building
        s.add("IFCRELAGGREGATES", self.guid("aggr", "site"), owner, None, None, project, [site])
        s.add(
            "IFCRELAGGREGATES", self.guid("aggr", "building"), owner, None, None, site, [building]
        )

        storeys: list[Ref] = []
        for lvl in self.model.levels:
            placement = s.add("IFCLOCALPLACEMENT", bldg_pl, self.wcs)
            storey = s.add(
                "IFCBUILDINGSTOREY", self.guid("storey", str(lvl.index)), owner, lvl.name,
                None, None, placement, None, None, Enum("ELEMENT"), float(lvl.elevation),
            )
            self.storey[lvl.index] = storey
            self.storey_placement[lvl.index] = placement
            storeys.append(storey)
        s.add(
            "IFCRELAGGREGATES", self.guid("aggr", "storeys"), owner, None, None, building, storeys
        )

    # -- product emission helpers --

    def _shape(self, context: Ref, solids: list[Ref], rep_type: str = "SweptSolid") -> Ref:
        s = self.spf
        rep = s.add("IFCSHAPEREPRESENTATION", context, "Body", rep_type, solids)
        return s.add("IFCPRODUCTDEFINITIONSHAPE", None, None, [rep])

    def _place(self, level: int, product: Ref) -> None:
        self.by_level.setdefault(level, []).append(product)

    # -- walls, openings, and their filling doors/windows --

    def _walls(self, owner: Ref, context: Ref) -> None:
        s = self.spf
        elev = {lvl.index: lvl.elevation for lvl in self.model.levels}
        hosted: dict[str, list[RevitOpening]] = {}
        for o in self.model.openings:
            if o.host_wall is not None:
                hosted.setdefault(o.host_wall, []).append(o)

        for w in self.model.walls:
            base = elev.get(w.level, 0.0)
            placement = self.storey_placement.get(w.level, self.wcs)
            solids = self._wall_solids(w, base)
            wall = s.add(
                "IFCWALL", self.guid("wall", w.id), owner,
                "Exterior Wall" if w.exterior else "Interior Wall", None, None,
                s.add("IFCLOCALPLACEMENT", placement, self.wcs),
                self._shape(context, solids), w.id, Enum("STANDARD"),
            )
            self._place(w.level, wall)
            for o in hosted.get(w.id, []):
                self._opening(owner, context, w, o, base, wall)

    def _wall_solids(self, w: RevitWall, base: float) -> list[Ref]:
        """One box per corrected height interval of the run (see wallheights).

        A single IfcWall keeps its lengthwise pieces as several solids in one
        body representation, so a lower run reaches the level above where it is
        covered and the roof plate where it is not — closing the inter-floor gap
        band and the wall-to-roof void — while the wall/opening/door counts the
        tests pin stay one-per-run. The triangular gable above the plate is
        closed by the roof prism's end cap (an ``IfcExtrudedAreaSolid`` triangle),
        so no separate wall infill is needed here. A single-level plan yields one
        plate-high box per run, byte-identical to before.
        """
        t = w.thickness
        c = w.const_coord
        solids: list[Ref] = []
        for lo, hi, top in wall_top_intervals(w, self.model):
            if top - base <= 0:
                continue
            if w.orientation == "v":
                solids.append(_box_solid(self.spf, c - t / 2.0, lo, base, c + t / 2.0, hi, top))
            else:
                solids.append(_box_solid(self.spf, lo, c - t / 2.0, base, hi, c + t / 2.0, top))
        return solids

    def _opening(
        self, owner: Ref, context: Ref, w: RevitWall, o: RevitOpening, base: float, wall: Ref
    ) -> None:
        s = self.spf
        t = w.thickness
        c = w.const_coord
        vertical = w.orientation == "v"
        along = o.location[1] if vertical else o.location[0]
        a, b = along - o.width / 2.0, along + o.width / 2.0
        z0, z1 = base + o.sill, base + o.sill + o.height
        if vertical:
            void = _box_solid(self.spf, c - t / 2.0, a, z0, c + t / 2.0, b, z1)
        else:
            void = _box_solid(self.spf, a, c - t / 2.0, z0, b, c + t / 2.0, z1)
        placement = self.storey_placement.get(w.level, self.wcs)
        opening = s.add(
            "IFCOPENINGELEMENT", self.guid("opening", o.id), owner, "Opening", None, None,
            s.add("IFCLOCALPLACEMENT", placement, self.wcs),
            self._shape(context, [void]), o.id, Enum("OPENING"),
        )
        s.add(
            "IFCRELVOIDSELEMENT", self.guid("voids", o.id), owner, None, None, wall, opening
        )
        self._fill(owner, context, w, o, base, opening)

    def _fill(
        self, owner: Ref, context: Ref, w: RevitWall, o: RevitOpening, base: float, opening: Ref
    ) -> None:
        """The IfcDoor / IfcWindow that fills an opening (a thin panel solid)."""
        s = self.spf
        t = w.thickness
        c = w.const_coord
        vertical = w.orientation == "v"
        along = o.location[1] if vertical else o.location[0]
        a, b = along - o.width / 2.0, along + o.width / 2.0
        z0, z1 = base + o.sill, base + o.sill + o.height
        panel = t / 6.0  # a thin leaf/sash centred in the wall
        if vertical:
            solid = _box_solid(self.spf, c - panel / 2.0, a, z0, c + panel / 2.0, b, z1)
        else:
            solid = _box_solid(self.spf, a, c - panel / 2.0, z0, b, c + panel / 2.0, z1)
        placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(w.level, self.wcs), self.wcs)
        shape = self._shape(context, [solid])
        if o.category == "window":
            product = s.add(
                "IFCWINDOW", self.guid("window", o.id), owner, "Window", None, None,
                placement, shape, o.id, float(o.height), float(o.width),
                Enum("WINDOW"), Enum("NOTDEFINED"), None,
            )
        else:
            product = s.add(
                "IFCDOOR", self.guid("door", o.id), owner, "Door", None, None,
                placement, shape, o.id, float(o.height), float(o.width),
                Enum("DOOR"), Enum("NOTDEFINED"), None,
            )
        s.add(
            "IFCRELFILLSELEMENT", self.guid("fills", o.id), owner, None, None, opening, product
        )
        self._place(o.level, product)

    # -- slabs, roof, frame, stairs, spaces --

    def _slabs(self, owner: Ref, context: Ref) -> None:
        s = self.spf
        elev = {lvl.index: lvl.elevation for lvl in self.model.levels}
        for i, sl in enumerate(self.model.slabs):
            z = elev.get(sl.level, 0.0)
            solid = _box_solid(
                self.spf, sl.x, sl.y, z - SLAB_THICKNESS, sl.x + sl.width, sl.y + sl.length, z
            )
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(sl.level, self.wcs), self.wcs)
            slab = s.add(
                "IFCSLAB", self.guid("slab", f"{sl.level}.{i}"), owner, "Floor Slab", None,
                None, placement, self._shape(context, [solid]), f"slab{i}", Enum("FLOOR"),
            )
            self._place(sl.level, slab)
        # Porch slabs sit at grade, just below the ground level.
        for area in self.model.areas:
            if area.kind != "porch":
                continue
            solid = _box_solid(
                self.spf, area.x, area.y, -SLAB_THICKNESS, area.x + area.width,
                area.y + area.length, 0.0,
            )
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(0, self.wcs), self.wcs)
            slab = s.add(
                "IFCSLAB", self.guid("porch", area.id), owner, f"Porch {area.id}", None,
                None, placement, self._shape(context, [solid]), area.id, Enum("FLOOR"),
            )
            self._place(0, slab)

    def _roof(self, owner: Ref, context: Ref) -> None:
        roof = self.model.roof
        if not roof:
            return
        s = self.spf
        top = roof["top_level"]
        plate = self.plan.level_elevation(top) + self.plan.ceiling_height
        oh = float(getattr(self.plan, "overhang", 0.0) or 0.0)
        solids: list[Ref] = []
        sections = roof.get("sections")
        if sections:
            for sec in sections:
                solids += _roof_block_solids(self.spf, sec, plate + float(sec.get("base_height", 0.0)), oh)
        else:
            solids += _roof_block_solids(self.spf, roof, plate, oh)
        if not solids:
            return
        placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(top, self.wcs), self.wcs)
        roof_type = _ROOF_TYPE.get(str(roof.get("style", "gable")), "FREEFORM")
        product = s.add(
            "IFCROOF", self.guid("roof", "roof"), owner, "Roof", None, None,
            placement, self._shape(context, solids), "roof", Enum(roof_type),
        )
        self._place(top, product)

    def _frame(self, owner: Ref, context: Ref) -> None:
        s = self.spf
        for i, col in enumerate(self.model.columns):
            size = col.size or 0.5
            cx, cy = col.point
            solid = _box_solid(
                self.spf, cx - size / 2.0, cy - size / 2.0, col.base,
                cx + size / 2.0, cy + size / 2.0, col.top,
            )
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(col.level, self.wcs), self.wcs)
            product = s.add(
                "IFCCOLUMN", self.guid("column", str(i)), owner, f"Post ({col.role})", None,
                None, placement, self._shape(context, [solid]), f"post{i}", Enum("COLUMN"),
            )
            self._place(col.level, product)
        for i, bm in enumerate(self.model.framing):
            size = bm.size or 0.5
            (x0, y0), (x1, y1) = bm.start, bm.end
            if abs(x1 - x0) >= abs(y1 - y0):
                cy = (y0 + y1) / 2.0
                solid = _box_solid(
                    self.spf, min(x0, x1), cy - size / 2.0, bm.z - size / 2.0,
                    max(x0, x1), cy + size / 2.0, bm.z + size / 2.0,
                )
            else:
                cx = (x0 + x1) / 2.0
                solid = _box_solid(
                    self.spf, cx - size / 2.0, min(y0, y1), bm.z - size / 2.0,
                    cx + size / 2.0, max(y0, y1), bm.z + size / 2.0,
                )
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(bm.level, self.wcs), self.wcs)
            product = s.add(
                "IFCBEAM", self.guid("beam", str(i)), owner, f"Beam ({bm.role})", None,
                None, placement, self._shape(context, [solid]), f"beam{i}", Enum("BEAM"),
            )
            self._place(bm.level, product)

    def _stairs(self, owner: Ref, context: Ref) -> None:
        s = self.spf
        elev = {lvl.index: lvl.elevation for lvl in self.model.levels}
        for area in self.model.areas:
            if area.kind != "stair":
                continue
            sp = area.meta.get("plan") or {}
            base = elev.get(area.meta.get("from_level", area.level), 0.0)
            solids = self._stair_solids(sp, base)
            if not solids:
                continue
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(area.level, self.wcs), self.wcs)
            layout = str(sp.get("layout", "straight"))
            product = s.add(
                "IFCSTAIR", self.guid("stair", area.id), owner, f"Stair {area.id}", None,
                None, placement, self._shape(context, solids), area.id,
                Enum(_STAIR_TYPE.get(layout, "STRAIGHT_RUN_STAIR")),
            )
            self._place(area.level, product)

    def _stair_solids(self, sp: dict, base: float) -> list[Ref]:
        rh = float(sp.get("riser_height", 0.0))
        tread = float(sp.get("tread", 0.0))
        solids: list[Ref] = []
        for run in sp.get("runs", []):
            (sx, sy), (ex, ey) = run["start"], run["end"]
            width = float(run["width"])
            n = int(run["risers"])
            along_x = abs(ex - sx) >= abs(ey - sy)
            dirx = 1.0 if ex >= sx else -1.0
            diry = 1.0 if ey >= sy else -1.0
            for k in range(n):
                z1 = base + (k + 1) * rh
                if along_x:
                    a0, a1 = sx + dirx * k * tread, sx + dirx * (k + 1) * tread
                    solids.append(
                        _box_solid(self.spf, min(a0, a1), sy - width / 2.0, base,
                                   max(a0, a1), sy + width / 2.0, z1)
                    )
                else:
                    a0, a1 = sy + diry * k * tread, sy + diry * (k + 1) * tread
                    solids.append(
                        _box_solid(self.spf, sx - width / 2.0, min(a0, a1), base,
                                   sx + width / 2.0, max(a0, a1), z1)
                    )
        return solids

    def _fixtures(self, owner: Ref, context: Ref) -> None:
        """One IfcFurnishingElement per fixture — a simple box on its room's floor.

        The exchange already resolves each fixture's footprint (authored placements
        plus surviving auto-seeds); here each becomes a plain box solid at a nominal
        height, with a deterministic GUID from its id, contained on its storey."""
        s = self.spf
        elev = {lvl.index: lvl.elevation for lvl in self.model.levels}
        for fx in self.model.fixtures:
            z = elev.get(fx.level, 0.0)
            h = _FIXTURE_HEIGHT.get(fx.kind, 2.5)
            solid = _box_solid(
                self.spf, fx.x, fx.y, z, fx.x + fx.width, fx.y + fx.length, z + h
            )
            placement = s.add(
                "IFCLOCALPLACEMENT", self.storey_placement.get(fx.level, self.wcs), self.wcs
            )
            product = s.add(
                "IFCFURNISHINGELEMENT", self.guid("fixture", fx.id), owner,
                f"{fx.kind} ({fx.room})", None, None,
                placement, self._shape(context, [solid]), fx.id,
            )
            self._place(fx.level, product)

    def _spaces(self, owner: Ref, context: Ref) -> None:
        """One IfcSpace per room, aggregated under its storey (schedules/areas)."""
        s = self.spf
        elev = {lvl.index: lvl.elevation for lvl in self.model.levels}
        by_level: dict[int, list[Ref]] = {}
        for r in self.model.rooms:
            z = elev.get(r.level, 0.0)
            ceil = r.ceiling_height or self.plan.ceiling_height
            solid = _box_solid(self.spf, r.x, r.y, z, r.x + r.width, r.y + r.length, z + ceil)
            placement = s.add("IFCLOCALPLACEMENT", self.storey_placement.get(r.level, self.wcs), self.wcs)
            space = s.add(
                "IFCSPACE", self.guid("space", r.id), owner, r.name, None, None,
                placement, self._shape(context, [solid]), f"{r.id} ({r.type})",
                Enum("ELEMENT"), Enum("INTERNAL"), None,
            )
            by_level.setdefault(r.level, []).append(space)
        for lvl, spaces in by_level.items():
            storey = self.storey.get(lvl)
            if storey is not None and spaces:
                s.add(
                    "IFCRELAGGREGATES", self.guid("aggr", f"spaces{lvl}"), owner, None, None,
                    storey, spaces,
                )

    # -- property set carrying score / metrics / source hash --

    def _properties(self, owner: Ref) -> None:
        props = self._barndsl_props()
        if not props:
            return
        s = self.spf
        prop_refs: list[Ref] = []
        for pname, value in props:
            if isinstance(value, str):
                nominal: object = Typed("IFCTEXT", value)
            elif isinstance(value, bool):
                nominal = Typed("IFCBOOLEAN", value)
            elif isinstance(value, float):
                nominal = Typed("IFCREAL", value)
            elif isinstance(value, int):
                nominal = Typed("IFCINTEGER", value)
            else:  # pragma: no cover - all props are str/float/int
                continue
            prop_refs.append(s.add("IFCPROPERTYSINGLEVALUE", pname, None, nominal, None))
        pset = s.add(
            "IFCPROPERTYSET", self.guid("pset", "barndsl"), owner, "barndsl",
            "barndsl design metrics", prop_refs,
        )
        s.add(
            "IFCRELDEFINESBYPROPERTIES", self.guid("rel", "pset"), owner, None, None,
            [self.building], pset,
        )

    def _barndsl_props(self) -> list[tuple[str, object]]:
        """DesignScore, sq-ft metrics and the canonical source hash for the Pset.

        Derived deterministically from the plan via the canonical emitted DSL, so
        an unchanged plan re-exports identical property values.
        """
        m = self.plan.metrics()
        props: list[tuple[str, object]] = []
        try:
            from .compiler import compile_source
            from .emit import emit_dsl
            from .score import design_score

            src = emit_dsl(self.plan)
            props.append(("DesignScore", float(design_score(compile_source(src, name=self.name)).total)))
            props.append(("SourceHash", hashlib.sha256(src.encode("utf-8")).hexdigest()))
        except Exception:  # pragma: no cover - metrics stay useful without the score
            pass
        props.append(("FootprintSqFt", float(m["footprint_sqft"])))
        props.append(("InteriorSqFt", float(m["interior_sqft"])))
        props.append(("HabitableSqFt", float(m["habitable_sqft"])))
        props.append(("Bedrooms", int(m["bedroom_count"])))
        props.append(("Bathrooms", float(m["bathroom_count"])))
        return props

    # -- top-level assembly --

    def build(self) -> str:
        s = self.spf
        owner = self._owner_history()
        origin = s.point3(0.0, 0.0, 0.0)
        ctx_wcs = s.add("IFCAXIS2PLACEMENT3D", origin, s.dir3(0, 0, 1), s.dir3(1, 0, 0))
        context = s.add(
            "IFCGEOMETRICREPRESENTATIONCONTEXT", None, "Model", 3, 1e-05, ctx_wcs, None
        )
        body_ctx = s.add(
            "IFCGEOMETRICREPRESENTATIONSUBCONTEXT", "Body", "Model", STAR, STAR, STAR, STAR,
            context, None, Enum("MODEL_VIEW"), None,
        )
        units = self._units()
        self._spatial_tree(owner, context, units)

        self._walls(owner, body_ctx)
        self._slabs(owner, body_ctx)
        self._roof(owner, body_ctx)
        self._frame(owner, body_ctx)
        self._stairs(owner, body_ctx)
        self._fixtures(owner, body_ctx)
        self._spaces(owner, body_ctx)
        self._properties(owner)

        # Contain every physical product on its storey (spaces aggregate instead).
        for lvl, products in self.by_level.items():
            storey = self.storey.get(lvl)
            if storey is None or not products:
                continue
            s.add(
                "IFCRELCONTAINEDINSPATIALSTRUCTURE", self.guid("contains", str(lvl)), owner,
                None, None, products, storey,
            )
        return _document(self.name, s.lines)


def _document(plan_name: str, data_lines: list[str]) -> str:
    """Wrap the entity lines in the ISO-10303-21 header/DATA envelope."""
    header = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');",
        # A fixed timestamp keeps the whole document byte-reproducible.
        "FILE_NAME("
        + _step_string(f"{plan_name}.ifc")
        + ",'1970-01-01T00:00:00',(''),(''),'barndsl','barndsl','');",
        "FILE_SCHEMA(('IFC4'));",
        "ENDSEC;",
        "DATA;",
    ]
    footer = ["ENDSEC;", "END-ISO-10303-21;"]
    return "\n".join(header + data_lines + footer) + "\n"


# --- public API --------------------------------------------------------------


def to_ifc(plan: Barndominium) -> str:
    """Lower ``plan`` to an IFC4 STEP (SPF) document and return it as a string."""
    model = to_revit_model(plan)
    return _Builder(plan, model).build()


def write_ifc(plan: Barndominium, path: str) -> str:
    """Write ``plan`` as IFC4 to ``path``. Returns the path."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_ifc(plan))
    return path
