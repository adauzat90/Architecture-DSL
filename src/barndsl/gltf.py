"""Lower a compiled plan into a 3D scene and serialise it as **glTF 2.0**.

The 3D model is a *build artifact* of the DSL, the same way the SVG plan, the
elevations and the DXF are: a reviewable, license-free, plug-in-free model any
glTF viewer (or :mod:`barndsl.viewer`) can open. Like the elevations it is
**schematic by design** — the point is design review, not construction detailing
or photorealism.

Crucially this module does **not** invent a new geometry layer. It reads the
Revit-shaped exchange from :func:`barndsl.revit.to_revit_model` — deduplicated
wall centreline runs with hosted openings, per-level floor slabs, a ``roof_plan``,
stair flights (``plan_stair_runs``), frame posts/beams, porches and room seed
rectangles — and *lowers* each of those records into axis-aligned boxes and a few
sloped quads. True boolean CSG is not required: a wall with a door is a lengthwise
box decomposition (solid segments either side of the opening, a lintel box above,
a sill box below a window), which matches the rectangle IR exactly.

Coordinates and units
---------------------
The plan is in **feet**, ``x`` east / ``y`` north, ``z`` up. glTF is **y-up**,
right-handed. We map plan ``(x, y, z)`` → glTF ``(x, z, -y)``: plan-up becomes
glTF ``+y``, and plan-north (``+y``) becomes glTF ``-z``. That map is a proper
rotation (determinant ``+1``), so triangle winding and normals transform by the
same rule and stay consistent — we build all geometry in plan space and transform
points and normals identically at serialisation time. **1 glTF unit = 1 foot**
(recorded in ``asset.extras``).

Wall heights on multi-level plans
---------------------------------
The exchange carries each wall at its storey's clear ceiling (its plate). Extruded
naively that leaves a gap band between stacked levels and a void where a lower
level is not covered by an upper floor. :mod:`barndsl.wallheights` corrects the
vertical extent of every run (shared with :mod:`barndsl.ifc`): a lower run rises
to the **base of the level above** where an upper floor covers it, and an exterior
lower run rises to the **top plate** where it does not, so walls meet the floor
above or the roof with no open band. A gable end that reaches the plate this way
is closed to the ridge by :func:`_gable_infill_segment`. Single-level plans have
only top-level runs and are byte-identical to before.

Roof approximations mirror what :mod:`barndsl.views` documents: **gable** exact
(two sloped planes eave→ridge at the plan's pitch, projected past the walls by the
eave overhang), **shed** one sloped plane, **monitor** built from its per-section
planes (two side sheds rising to a raised central gable).

Scene organisation
------------------
Every mesh is a named node (``wall:<id>``, ``room:<id>``, ``roof``, ``post:*`` …)
grouped under a parent node per layer — ``floors``, ``walls``, ``openings``,
``roof``, ``frame``, ``porches``, ``stairs`` — so a viewer can toggle layers (the
roof especially, to look inside). Room floors are tinted with the per-room-type
palette from :mod:`barndsl.render` so the drawing set reads as one system.

Public API::

    to_gltf(plan)            # -> glTF JSON dict (embedded base64 buffer, .gltf)
    to_glb(plan)             # -> bytes (the binary .glb container)
    write_gltf(plan, path)   # -> path; .glb vs .gltf chosen by the extension
    build_scene(plan)        # -> Scene (the intermediate box/quad node graph)
"""

from __future__ import annotations

import base64
import json
import math
import struct
from dataclasses import dataclass, field

from .constants import SLAB_THICKNESS
from .elements import Barndominium, RoomType
from .geometry import TOL
from .materials import (
    FRAME_MATERIAL,
    OPENING_MATERIAL,
    PALETTE,
    PORCH_MATERIAL,
    SLAB_MATERIAL,
    STAIR_MATERIAL,
    Material,
    floor_material,
    roof_material,
    wall_material,
)
from .render import ROOM_COLORS
from .revit import RevitModel, RevitOpening, RevitWall, to_revit_model
from .wallheights import gable_line, is_gable_end, roof_plate, wall_top_intervals

# --- materials ---------------------------------------------------------------
#
# Every surface carries a real :class:`~barndsl.materials.Material` (base colour +
# roughness/metallic + procedural pattern) rather than a bare hex string. Room
# floors additionally carry a *tint* — the per-room-type colour from
# :data:`barndsl.render.ROOM_COLORS` — which modulates the floor material's base
# colour so the drawing set still reads as one system (a tiled bath floor is a
# little cooler than a tiled kitchen). See :func:`_effective_linear`.

#: A thin visible thickness (ft) for floor tiles that only need to read as a
#: coloured surface, not a structural slab.
FLOOR_TILE_THICKNESS = 0.08


# --- geometry primitives -----------------------------------------------------


@dataclass
class Box:
    """An axis-aligned box in **plan space** (feet), by its min/max corners."""

    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float

    @property
    def volume(self) -> float:
        return abs(self.x1 - self.x0) * abs(self.y1 - self.y0) * abs(self.z1 - self.z0)


def _normal(a, b, c) -> tuple[float, float, float]:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    m = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / m, ny / m, nz / m)


class MeshNode:
    """A named node accumulating triangles (plan space) under one material.

    Geometry is built with :meth:`add_box`/:meth:`add_quad`; the :class:`Scene`
    transforms every vertex and normal into glTF space when it serialises.
    ``tint`` is an optional ``#rrggbb`` that modulates the material's base colour
    (room floors carry their per-room-type tint here); ``None`` uses the material
    colour verbatim.
    """

    def __init__(self, name: str, layer: str, material: Material, tint: str | None = None):
        self.name = name
        self.layer = layer
        self.material = material
        self.tint = tint
        self.positions: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.indices: list[int] = []

    @property
    def empty(self) -> bool:
        return not self.indices

    def _tri(self, a, b, c, n) -> None:
        i = len(self.positions)
        self.positions.extend((a, b, c))
        self.normals.extend((n, n, n))
        self.indices.extend((i, i + 1, i + 2))

    def add_quad(self, a, b, c, d, n=None) -> None:
        """A planar quad wound CCW around its outward normal (a→b→c→d)."""
        if n is None:
            n = _normal(a, b, c)
        self._tri(a, b, c, n)
        self._tri(a, c, d, n)

    def add_quad_up(self, a, b, c, d) -> None:
        """A quad forced to face upward (used for roof planes, winding-agnostic)."""
        n = _normal(a, b, c)
        if n[2] < 0:
            a, b, c, d = a, d, c, b
            n = _normal(a, b, c)
        self.add_quad(a, b, c, d, n)

    def add_box(self, box: Box) -> None:
        if box.volume <= 0:
            return
        x0, y0, z0 = box.x0, box.y0, box.z0
        x1, y1, z1 = box.x1, box.y1, box.z1
        # Six faces, each wound CCW as seen from outside (outward normal by the
        # right-hand rule) — see the module tests for the winding invariant.
        self.add_quad((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))  # +z
        self.add_quad((x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0))  # -z
        self.add_quad((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))  # +x
        self.add_quad((x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (x0, y0, z0))  # -x
        self.add_quad((x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1))  # +y
        self.add_quad((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))  # -y


@dataclass
class Scene:
    """The intermediate scene: named mesh nodes grouped by layer, plus the plan.

    This is the natural glTF lowering — a box/quad node graph — not a new upstream
    geometry IR. Exposed so the viewer and the tests can introspect it (node names,
    per-node geometry) without reparsing the serialised glTF.
    """

    plan: Barndominium
    model: RevitModel
    nodes: list[MeshNode] = field(default_factory=list)

    #: Layer order (parents in the glTF scene, and the viewer's toggle order).
    LAYERS = ("floors", "walls", "openings", "roof", "frame", "porches", "stairs")

    def node(
        self, name: str, layer: str, material: Material, tint: str | None = None
    ) -> MeshNode:
        n = MeshNode(name, layer, material, tint)
        self.nodes.append(n)
        return n


# --- wall solids (the door/window box decomposition) -------------------------


def wall_solids(
    wall: RevitWall,
    openings: list[RevitOpening],
    base: float,
    intervals: list[tuple[float, float, float]] | None = None,
) -> list[Box]:
    """The solid boxes of one wall run, with its hosted ``openings`` cut out.

    Pure and deterministic: the run is split lengthwise into full-height segments
    around each opening; below a window sill and above every opening head a box is
    re-added (sill / lintel), so the void is exactly the opening rectangle. With no
    openings this is a single box — the uncut wall — so opening cuts strictly
    reduce the summed volume (the invariant the tests pin).

    ``intervals`` is the run's corrected vertical extent as ``(lo, hi, top)``
    running pieces (see :mod:`barndsl.wallheights`): each piece extrudes from
    ``base`` to its own ``top``, so a lower level's run reaches the level above
    where it is covered and the roof plate where it is not. It defaults to a
    single plate-high box over the whole span, which is the historical behaviour
    (and keeps single-level plans byte-identical).
    """
    if intervals is None:
        lo, hi = wall.span
        intervals = [(lo, hi, base + wall.height)]
    boxes: list[Box] = []
    for lo, hi, top in intervals:
        boxes.extend(_segment_solids(wall, openings, base, lo, hi, top))
    return boxes


def _segment_solids(
    wall: RevitWall, openings: list[RevitOpening], base: float, lo: float, hi: float, top: float
) -> list[Box]:
    """One vertical segment ``[lo, hi]`` of a run, base→``top``, with openings cut."""
    t = wall.thickness
    vertical = wall.orientation == "v"
    c = wall.const_coord

    def seg_box(a: float, b: float, z0: float, z1: float) -> Box:
        if vertical:
            return Box(c - t / 2.0, a, z0, c + t / 2.0, b, z1)
        return Box(a, c - t / 2.0, z0, b, c + t / 2.0, z1)

    # Opening spans along the running axis, clamped to this segment and sorted.
    spans: list[tuple[float, float, float, float]] = []  # (a, b, sill, head)
    for o in openings:
        along = o.location[1] if vertical else o.location[0]
        a = max(lo, along - o.width / 2.0)
        b = min(hi, along + o.width / 2.0)
        if b - a <= 1e-6:
            continue
        sill = max(0.0, min(o.sill, wall.height))
        head = max(sill, min(o.sill + o.height, wall.height))
        spans.append((a, b, sill, head))
    spans.sort()

    boxes: list[Box] = []
    cursor = lo
    for a, b, sill, head in spans:
        if a - cursor > 1e-6:
            boxes.append(seg_box(cursor, a, base, top))  # solid pier before opening
        if sill > 1e-6:
            boxes.append(seg_box(a, b, base, base + sill))  # sill box (windows)
        if top - (base + head) > 1e-6:
            boxes.append(seg_box(a, b, base + head, top))  # lintel/header box
        cursor = max(cursor, b)
    if hi - cursor > 1e-6:
        boxes.append(seg_box(cursor, hi, base, top))
    return boxes


def _opening_boxes(wall: RevitWall, openings: list[RevitOpening], base: float) -> list[Box]:
    """Just the lintel (and window sill) boxes — the ``openings`` layer geometry."""
    t = wall.thickness
    lo, hi = wall.span
    top = base + wall.height
    vertical = wall.orientation == "v"
    c = wall.const_coord
    out: list[Box] = []
    for o in openings:
        along = o.location[1] if vertical else o.location[0]
        a = max(lo, along - o.width / 2.0)
        b = min(hi, along + o.width / 2.0)
        if b - a <= 1e-6:
            continue
        sill = max(0.0, min(o.sill, wall.height))
        head = max(sill, min(o.sill + o.height, wall.height))

        def box(a, b, z0, z1):
            if vertical:
                return Box(c - t / 2.0, a, z0, c + t / 2.0, b, z1)
            return Box(a, c - t / 2.0, z0, b, c + t / 2.0, z1)

        if sill > 1e-6:
            out.append(box(a, b, base, base + sill))
        if top - (base + head) > 1e-6:
            out.append(box(a, b, base + head, top))
    return out


def _gable_infill(node: MeshNode, wall: RevitWall, base: float) -> None:
    """Add the triangular gable-end infill above the plate up to the ridge apex."""
    if wall.profile != "gable" or wall.apex is None:
        return
    t = wall.thickness
    lo, hi = wall.span
    plate = base + wall.height
    apex_top = base + wall.apex_height
    vertical = wall.orientation == "v"
    c = wall.const_coord
    ax = wall.apex[1] if vertical else wall.apex[0]  # apex position along the run

    def pt(along: float, face: float, z: float):
        # face is the offset across the wall thickness (± t/2).
        if vertical:
            return (c + face, along, z)
        return (along, c + face, z)

    for face in (t / 2.0, -t / 2.0):
        # Triangular cap on each face.
        node.add_quad(
            pt(lo, face, plate), pt(hi, face, plate), pt(ax, face, apex_top),
            pt(ax, face, apex_top),
        )
    # Two sloped rectangles closing the top of the gable across the thickness.
    node.add_quad_up(
        pt(lo, -t / 2.0, plate), pt(lo, t / 2.0, plate),
        pt(ax, t / 2.0, apex_top), pt(ax, -t / 2.0, apex_top),
    )
    node.add_quad_up(
        pt(ax, -t / 2.0, apex_top), pt(ax, t / 2.0, apex_top),
        pt(hi, t / 2.0, plate), pt(hi, -t / 2.0, plate),
    )


def _gable_infill_segment(
    node: MeshNode, wall: RevitWall, lo: float, hi: float, plate: float, gl: dict
) -> None:
    """Close the gable above ``[lo, hi]`` for a run that reaches the plate.

    Generalises :func:`_gable_infill` (which fills a whole marked top-level gable
    wall) to an arbitrary run interval, following the roof underside from
    :func:`barndsl.wallheights.gable_line`. Used for a *lower* exterior run that
    the uncovered-extension rule lifts to the plate at a gable end — e.g. the
    single-storey end of a plan with a partial upper floor, which the exchange's
    top-level-only gable marking never reaches.
    """
    t = wall.thickness
    vertical = wall.orientation == "v"
    c = wall.const_coord
    mid, half, rise = gl["mid"], gl["half"], gl["rise"]

    def zf(s: float) -> float:
        return plate + rise * max(0.0, 1.0 - abs(s - mid) / half)

    def pt(along: float, face: float, z: float):
        if vertical:
            return (c + face, along, z)
        return (along, c + face, z)

    # Split at the ridge so each piece has a straight (monotonic) roofline.
    breaks = [lo, mid, hi] if lo < mid < hi else [lo, hi]
    for p, q in zip(breaks, breaks[1:]):
        zp, zq = zf(p), zf(q)
        for face in (t / 2.0, -t / 2.0):  # the trapezoid plate→roofline on each face
            node.add_quad(
                pt(p, face, plate), pt(q, face, plate), pt(q, face, zq), pt(p, face, zp),
            )
        # The roof-underside strip closing the top across the wall thickness.
        node.add_quad_up(
            pt(p, -t / 2.0, zp), pt(p, t / 2.0, zp),
            pt(q, t / 2.0, zq), pt(q, -t / 2.0, zq),
        )


# --- roof --------------------------------------------------------------------


def _block_bounds(block: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for seg in block["outline"]:
        for px, py in seg:
            xs.append(px)
            ys.append(py)
    return min(xs), min(ys), max(xs), max(ys)


def _roof_block(node: MeshNode, block: dict, eave_z: float, oh: float) -> None:
    """Add one roof block's planes (gable/shed) into ``node`` at eave ``eave_z``."""
    minx, miny, maxx, maxy = _block_bounds(block)
    rise = float(block["rise"])
    ridge_z = eave_z + rise
    ga = block["gable_axis"]
    style = block["style"]

    if style == "shed":
        slopes = block["outline_slopes"]  # order: south(0), east(1), north(2), west(3)
        if ga == "x":  # eaves are south/north; slope varies along y
            low_lo = slopes[0]  # south is the low eave?
            y_lo, y_hi = (miny, maxy) if low_lo else (maxy, miny)
            span = abs(maxy - miny) or 1.0

            def zf(y):
                return eave_z + rise * abs(y - y_lo) / span

            node.add_quad_up(
                (minx - oh, miny - oh, zf(miny)), (maxx + oh, miny - oh, zf(miny)),
                (maxx + oh, maxy + oh, zf(maxy)), (minx - oh, maxy + oh, zf(maxy)),
            )
        else:  # eaves are west(3)/east(1); slope varies along x
            low_lo = slopes[3]  # west is the low eave?
            x_lo, x_hi = (minx, maxx) if low_lo else (maxx, minx)
            span = abs(maxx - minx) or 1.0

            def zf(x):
                return eave_z + rise * abs(x - x_lo) / span

            node.add_quad_up(
                (minx - oh, miny - oh, zf(minx)), (maxx + oh, miny - oh, zf(maxx)),
                (maxx + oh, maxy + oh, zf(maxx)), (minx - oh, maxy + oh, zf(minx)),
            )
        return

    # gable (and monitor sections' central gable): two planes meeting at the ridge.
    if ga == "x":  # ridge runs east-west at y = mid
        mid = (miny + maxy) / 2.0
        node.add_quad_up(  # south plane
            (minx - oh, miny - oh, eave_z), (maxx + oh, miny - oh, eave_z),
            (maxx + oh, mid, ridge_z), (minx - oh, mid, ridge_z),
        )
        node.add_quad_up(  # north plane
            (minx - oh, mid, ridge_z), (maxx + oh, mid, ridge_z),
            (maxx + oh, maxy + oh, eave_z), (minx - oh, maxy + oh, eave_z),
        )
    else:  # ridge runs north-south at x = mid
        mid = (minx + maxx) / 2.0
        node.add_quad_up(  # west plane
            (minx - oh, miny - oh, eave_z), (mid, miny - oh, ridge_z),
            (mid, maxy + oh, ridge_z), (minx - oh, maxy + oh, eave_z),
        )
        node.add_quad_up(  # east plane
            (mid, miny - oh, ridge_z), (maxx + oh, miny - oh, eave_z),
            (maxx + oh, maxy + oh, eave_z), (mid, maxy + oh, ridge_z),
        )


def _add_roof(scene: Scene) -> None:
    roof = scene.model.roof
    if not roof:
        return
    plan = scene.plan
    top = roof["top_level"]
    plate = plan.level_elevation(top) + plan.ceiling_height
    oh = float(getattr(plan, "overhang", 0.0) or 0.0)
    node = scene.node("roof", "roof", roof_material(plan))
    sections = roof.get("sections")
    if sections:
        for sec in sections:
            _roof_block(node, sec, plate + float(sec.get("base_height", 0.0)), oh)
    else:
        _roof_block(node, roof, plate, oh)


# --- assembly of every layer -------------------------------------------------


def _room_tint(rtype) -> str:
    """The per-room-type tint (a `#rrggbb`) that modulates the floor material."""
    return ROOM_COLORS.get(rtype, ROOM_COLORS[RoomType.OTHER])


def _add_floors(scene: Scene) -> None:
    model = scene.model
    rooms_by_id = {r.id: r for r in scene.plan.rooms}
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    for i, s in enumerate(model.slabs):
        z = elev.get(s.level, 0.0)
        node = scene.node(f"slab:{s.level}.{i}", "floors", SLAB_MATERIAL)
        node.add_box(Box(s.x, s.y, z - SLAB_THICKNESS, s.x + s.width, s.y + s.length, z))
    for r in model.rooms:
        z = elev.get(r.level, 0.0)
        # Resolve the floor finish from the authored room (its `floor` hint / type),
        # then wash it with the room-type tint so the floors read as one palette.
        room = rooms_by_id.get(r.id)
        if room is not None:
            mat, tint = floor_material(room), _room_tint(room.type)
        else:  # defensive: a slab with no authored room keeps the wood default
            mat, tint = PALETTE["wood_plank"], None
        node = scene.node(f"room:{r.id}", "floors", mat, tint)
        node.add_box(Box(r.x, r.y, z, r.x + r.width, r.y + r.length, z + FLOOR_TILE_THICKNESS))


def _add_walls(scene: Scene) -> None:
    model = scene.model
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    hosted: dict[str, list[RevitOpening]] = {}
    for o in model.openings:
        if o.host_wall is not None:
            hosted.setdefault(o.host_wall, []).append(o)
    plate = roof_plate(model)
    gl = gable_line(model)
    wall_mat = wall_material(scene.plan)
    for w in model.walls:
        base = elev.get(w.level, 0.0)
        ops = hosted.get(w.id, [])
        node = scene.node(f"wall:{w.id}", "walls", wall_mat)
        intervals = wall_top_intervals(w, model)
        for box in wall_solids(w, ops, base, intervals):
            node.add_box(box)
        # Top-level gable ends carry the exchange's own marking; a lower exterior
        # run lifted to the plate at a gable end (uncovered extension) is closed
        # here from the corrected intervals so its end meets the ridge too.
        _gable_infill(node, w, base)
        if gl is not None and plate is not None and w.profile != "gable" and w.exterior \
                and is_gable_end(w, gl):
            for lo, hi, top in intervals:
                if abs(top - plate) <= TOL:
                    _gable_infill_segment(node, w, lo, hi, plate, gl)
        # Lintel / sill boxes ride the openings layer so they can be toggled apart.
        lintels = _opening_boxes(w, ops, base)
        if lintels:
            onode = scene.node(f"opening:{w.id}", "openings", OPENING_MATERIAL)
            for box in lintels:
                onode.add_box(box)


def _add_frame(scene: Scene) -> None:
    model = scene.model
    for i, c in enumerate(model.columns):
        s = c.size or 0.5
        node = scene.node(f"post:{i}", "frame", FRAME_MATERIAL)
        node.add_box(Box(c.point[0] - s / 2, c.point[1] - s / 2, c.base,
                         c.point[0] + s / 2, c.point[1] + s / 2, c.top))
    for i, b in enumerate(model.framing):
        s = b.size or 0.5
        (x0, y0), (x1, y1) = b.start, b.end
        node = scene.node(f"beam:{i}", "frame", FRAME_MATERIAL)
        if abs(x1 - x0) >= abs(y1 - y0):  # runs east-west
            node.add_box(Box(min(x0, x1), (y0 + y1) / 2 - s / 2, b.z - s / 2,
                             max(x0, x1), (y0 + y1) / 2 + s / 2, b.z + s / 2))
        else:  # runs north-south
            node.add_box(Box((x0 + x1) / 2 - s / 2, min(y0, y1), b.z - s / 2,
                             (x0 + x1) / 2 + s / 2, max(y0, y1), b.z + s / 2))


def _add_porches(scene: Scene) -> None:
    plan = scene.plan
    plate = plan.ceiling_height
    for a in scene.model.areas:
        if a.kind != "porch":
            continue
        node = scene.node(f"porch:{a.id}", "porches", PORCH_MATERIAL)
        node.add_box(Box(a.x, a.y, -SLAB_THICKNESS, a.x + a.width, a.y + a.length, 0.0))
        if a.meta.get("covered"):
            s = 0.5
            corners = [
                (a.x, a.y), (a.x + a.width, a.y),
                (a.x, a.y + a.length), (a.x + a.width, a.y + a.length),
            ]
            posts = scene.node(f"porch:{a.id}:posts", "porches", FRAME_MATERIAL)
            for cx, cy in corners:
                px = min(max(cx - s / 2, a.x), a.x + a.width - s)
                py = min(max(cy - s / 2, a.y), a.y + a.length - s)
                posts.add_box(Box(px, py, 0.0, px + s, py + s, plate))
            node.add_box(Box(a.x, a.y, plate, a.x + a.width, a.y + a.length, plate + 0.25))


def _add_stairs(scene: Scene) -> None:
    model = scene.model
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    for a in model.areas:
        if a.kind != "stair":
            continue
        sp = a.meta.get("plan") or {}
        base = elev.get(a.meta.get("from_level", a.level), 0.0)
        rh = float(sp.get("riser_height", 0.0))
        tread = float(sp.get("tread", 0.0))
        node = scene.node(f"stair:{a.id}", "stairs", STAIR_MATERIAL)
        for run in sp.get("runs", []):
            (sx, sy), (ex, ey) = run["start"], run["end"]
            width = float(run["width"])
            n = int(run["risers"])
            along_x = abs(ex - sx) >= abs(ey - sy)
            dirx = 1.0 if ex >= sx else -1.0
            diry = 1.0 if ey >= sy else -1.0
            for k in range(n):
                z0 = base
                z1 = base + (k + 1) * rh
                if along_x:
                    a0 = sx + dirx * k * tread
                    a1 = sx + dirx * (k + 1) * tread
                    node.add_box(Box(min(a0, a1), sy - width / 2, z0,
                                     max(a0, a1), sy + width / 2, z1))
                else:
                    a0 = sy + diry * k * tread
                    a1 = sy + diry * (k + 1) * tread
                    node.add_box(Box(sx - width / 2, min(a0, a1), z0,
                                     sx + width / 2, max(a0, a1), z1))
        for land in sp.get("landings", []):
            top = base + rh * int(sp.get("risers", 1))
            node.add_box(Box(land["x"], land["y"], top - 0.1,
                             land["x"] + land["width"], land["y"] + land["length"], top))


def build_scene(plan: Barndominium) -> Scene:
    """Lower ``plan`` into the intermediate box/quad :class:`Scene` (pure)."""
    model = to_revit_model(plan)
    scene = Scene(plan=plan, model=model)
    _add_floors(scene)
    _add_walls(scene)
    _add_roof(scene)
    _add_frame(scene)
    _add_porches(scene)
    _add_stairs(scene)
    return scene


# --- colour conversion (sRGB hex → linear baseColorFactor) -------------------


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear(hex_color: str) -> list[float]:
    """A ``#rrggbb`` colour as a linear-space ``[r, g, b, 1]`` baseColorFactor."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return [_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b), 1.0]


def effective_linear(material: Material, tint: str | None = None) -> list[float]:
    """The material's linear ``[r, g, b, 1]`` base colour, modulated by ``tint``.

    With no tint this is just the material colour; a tint (a room-type ``#rrggbb``)
    multiplies it channel-wise in linear space — the room palette washes over the
    floor finish rather than replacing it, so a tiled bath and a tiled kitchen
    share a material yet still read as different rooms.
    """
    base = hex_to_linear(material.color)
    if not tint:
        return base
    t = hex_to_linear(tint)
    return [base[0] * t[0], base[1] * t[1], base[2] * t[2], 1.0]


# --- glTF space transform ----------------------------------------------------


def _to_gltf(p) -> tuple[float, float, float]:
    """Plan ``(x, y, z)`` → glTF ``(x, z, -y)`` (y-up, right-handed)."""
    return (p[0], p[2], -p[1])


# --- serialisation -----------------------------------------------------------


def _pad4(data: bytearray, fill: int = 0x00) -> None:
    while len(data) % 4:
        data.append(fill)


def _build_gltf(plan: Barndominium) -> tuple[dict, bytes]:
    """Return the glTF JSON dict (no buffer uri) and the raw binary buffer."""
    scene = build_scene(plan)
    live = [n for n in scene.nodes if not n.empty]

    # Deduplicate materials by (palette id, tint), in first-seen order. The tint
    # only distinguishes room floors sharing a finish; every other surface has no
    # tint, so untinted materials collapse to one entry each as before.
    def mat_key(n: MeshNode) -> tuple[str, str]:
        return (n.material.name, n.tint or "")

    mat_index: dict[tuple[str, str], int] = {}
    materials: list[dict] = []
    for n in live:
        key = mat_key(n)
        if key not in mat_index:
            mat_index[key] = len(materials)
            mat = n.material
            name = mat.name if not n.tint else f"{mat.name} ({n.tint})"
            materials.append(
                {
                    "name": name,
                    "pbrMetallicRoughness": {
                        "baseColorFactor": effective_linear(mat, n.tint),
                        "metallicFactor": mat.metallic,
                        "roughnessFactor": mat.roughness,
                    },
                    "doubleSided": True,
                }
            )

    buf = bytearray()
    buffer_views: list[dict] = []
    accessors: list[dict] = []
    meshes: list[dict] = []
    child_nodes: list[dict] = []

    def add_view(raw: bytes, target: int) -> int:
        _pad4(buf)
        offset = len(buf)
        buf.extend(raw)
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(raw), "target": target}
        )
        return len(buffer_views) - 1

    layer_children: dict[str, list[int]] = {ly: [] for ly in Scene.LAYERS}
    for n in live:
        verts = [_to_gltf(p) for p in n.positions]
        norms = [_to_gltf(v) for v in n.normals]
        pos_bytes = b"".join(struct.pack("<3f", *v) for v in verts)
        nrm_bytes = b"".join(struct.pack("<3f", *v) for v in norms)
        idx_bytes = b"".join(struct.pack("<I", i) for i in n.indices)

        pos_view = add_view(pos_bytes, 34962)
        nrm_view = add_view(nrm_bytes, 34962)
        idx_view = add_view(idx_bytes, 34963)

        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        pos_acc = len(accessors)
        accessors.append(
            {
                "bufferView": pos_view,
                "componentType": 5126,
                "count": len(verts),
                "type": "VEC3",
                "min": [min(xs), min(ys), min(zs)],
                "max": [max(xs), max(ys), max(zs)],
            }
        )
        nrm_acc = len(accessors)
        accessors.append(
            {"bufferView": nrm_view, "componentType": 5126, "count": len(norms), "type": "VEC3"}
        )
        idx_acc = len(accessors)
        accessors.append(
            {
                "bufferView": idx_view,
                "componentType": 5125,
                "count": len(n.indices),
                "type": "SCALAR",
            }
        )
        mesh_index = len(meshes)
        meshes.append(
            {
                "name": n.name,
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                        "indices": idx_acc,
                        "material": mat_index[mat_key(n)],
                    }
                ],
            }
        )
        node_index = len(child_nodes)  # provisional; offset by len(parents) below
        child_nodes.append({"name": n.name, "mesh": mesh_index})
        layer_children[n.layer].append(node_index)

    # Parent (layer) nodes come first in the node array; children follow, so their
    # provisional indices shift by the number of parents actually emitted.
    parents = [ly for ly in Scene.LAYERS if layer_children[ly]]
    shift = len(parents)
    nodes: list[dict] = []
    for ly in parents:
        nodes.append(
            {"name": ly, "children": [c + shift for c in layer_children[ly]]}
        )
    nodes.extend(child_nodes)

    gltf: dict = {
        "asset": {
            "version": "2.0",
            "generator": "barndsl.gltf",
            "extras": {"units": "feet", "unit_scale": 1.0, "plan": plan.name},
        },
        "scene": 0,
        "scenes": [{"name": plan.name, "nodes": list(range(len(parents)))}],
        "nodes": nodes,
        "meshes": meshes,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "materials": materials,
        "buffers": [{"byteLength": len(buf)}],
    }
    return gltf, bytes(buf)


def to_gltf(plan: Barndominium) -> dict:
    """Return ``plan`` as a self-contained glTF 2.0 JSON dict.

    The single buffer is embedded as a base64 ``data:`` URI, so the returned dict
    is a complete ``.gltf`` document with no external ``.bin`` sidecar.
    """
    gltf, buf = _build_gltf(plan)
    uri = "data:application/octet-stream;base64," + base64.b64encode(buf).decode("ascii")
    gltf["buffers"][0]["uri"] = uri
    return gltf


def to_glb(plan: Barndominium) -> bytes:
    """Return ``plan`` as a binary **.glb** container (JSON + BIN chunks)."""
    gltf, buf = _build_gltf(plan)
    json_bytes = bytearray(json.dumps(gltf, separators=(",", ":")).encode("utf-8"))
    _pad4(json_bytes, 0x20)  # JSON chunk padded with spaces
    bin_bytes = bytearray(buf)
    _pad4(bin_bytes, 0x00)  # BIN chunk padded with zeros

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    out = bytearray()
    out.extend(struct.pack("<I", 0x46546C67))  # "glTF"
    out.extend(struct.pack("<I", 2))           # version 2
    out.extend(struct.pack("<I", total))       # total length
    out.extend(struct.pack("<I", len(json_bytes)))
    out.extend(struct.pack("<I", 0x4E4F534A))  # "JSON"
    out.extend(json_bytes)
    out.extend(struct.pack("<I", len(bin_bytes)))
    out.extend(struct.pack("<I", 0x004E4942))  # "BIN\0"
    out.extend(bin_bytes)
    return bytes(out)


def write_gltf(plan: Barndominium, path: str) -> str:
    """Write ``plan`` to ``path``; ``.glb`` → binary, otherwise embedded ``.gltf``."""
    if path.lower().endswith(".glb"):
        with open(path, "wb") as fh:
            fh.write(to_glb(plan))
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(to_gltf(plan), fh)
    return path
