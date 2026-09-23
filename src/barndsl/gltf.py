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
from .drawing import swing_side
from .elements import Barndominium, RoomType
from .geometry import TOL, shared_edge
from .materials import (
    DOOR_MATERIAL,
    FIXTURE_FABRIC,
    FIXTURE_PORCELAIN,
    FIXTURE_STAINLESS,
    FIXTURE_WOOD,
    FRAME_MATERIAL,
    GLASS_MATERIAL,
    OPENING_MATERIAL,
    PALETTE,
    PORCH_MATERIAL,
    SLAB_MATERIAL,
    STAIR_MATERIAL,
    TRIM_MATERIAL,
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
        #: For an exterior wall run: the compass face it belongs to
        #: (``south``/``east``/``north``/``west``), so a viewer can light up the
        #: face an elevation shows. ``None`` for everything else.
        self.face: str | None = None
        self.positions: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.indices: list[int] = []
        #: Door-leaf animation record (hinge/dir/out/mode/width/height in plan
        #: coords), attached only to a swinging/sliding/overhead leaf node so the
        #: viewer can animate it independently. ``None`` on every static node —
        #: the exported glTF bakes the closed geometry and ignores it entirely.
        self.door: dict | None = None

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
    #: ``trim`` (baseboards) sits with the openings finish work; ``ceilings`` sits
    #: with the roof as the other overhead plane, so a client can strip the roof to
    #: look inside yet keep the flat ceilings, or drop both.
    LAYERS = (
        "floors", "walls", "openings", "trim", "ceilings", "roof",
        "frame", "porches", "stairs", "fixtures",
    )

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


# --- opening leaves, panels and glazing (the "reads as a door/window" layer) --
#
# `_opening_boxes` (above) only adds the lintel/sill drywall reveals; the void is
# an empty rectangle. This section fills that void with the recognisable part of
# the opening — a door leaf, a garage panel, or a glazed window — so the model
# reads as a house, not a wall with holes. Each *movable* leaf is its own node
# carrying a `door` record (hinge/dir/out in plan coords) so the viewer's walk
# mode can swing/slide/lift it independently; the exported glTF just bakes the
# closed geometry and never reads the record.
#
# Everything is built CLOSED, in the wall plane, in plan space (the Scene's
# transform sends it to the glTF frame). A thin leaf is ~0.15 ft, glazing ~0.1 ft.

_LEAF_THICKNESS = 0.15
_GLASS_THICKNESS = 0.1
_MULLION = 0.1  # square section of a window bar / a thin jamb casing
#: Perimeter frame/casing around a window and the head+jamb trim of an exterior
#: door: a slim band ~0.25 ft wide, proud of both wall faces by ~0.05 ft each side,
#: in painted-trim material. Deterministic and subtle — a reveal, not a moulding
#: profile — so the model reads as a finished house without over-modelling.
_FRAME_WIDTH = 0.25       # how far the band reaches into the opening (along + up)
_FRAME_PROUD = 0.05       # how far it stands proud of each wall face


def _opening_span(wall: RevitWall, o: RevitOpening) -> tuple[float, float] | None:
    """The opening's ``(lo, hi)`` along the wall's running axis, clamped, or None."""
    lo, hi = wall.span
    along = o.location[1] if wall.orientation == "v" else o.location[0]
    a = max(lo, along - o.width / 2.0)
    b = min(hi, along + o.width / 2.0)
    return (a, b) if b - a > 1e-6 else None


def _across_dir(wall: RevitWall) -> tuple[float, float]:
    """The plan unit vector across the wall toward ``+x``/``+y``.

    Vertical runs (const x) face east ``(1, 0)``; horizontal runs face north
    ``(0, 1)`` — the ``+1`` side of :func:`barndsl.drawing.swing_side`. The
    caller flips the sign toward the room a leaf swings into.
    """
    return (1.0, 0.0) if wall.orientation == "v" else (0.0, 1.0)


def _slab_at(along: float, face: float, wall: RevitWall) -> tuple[float, float]:
    """A plan ``(x, y)`` at running-axis position ``along`` and thickness offset ``face``."""
    c = wall.const_coord
    if wall.orientation == "v":
        return (c + face, along)
    return (along, c + face)


def _panel_box(wall: RevitWall, a: float, b: float, z0: float, z1: float,
               t: float) -> Box:
    """A thin panel filling ``[a, b]`` of the run, ``t`` thick, from ``z0`` to ``z1``."""
    c = wall.const_coord
    if wall.orientation == "v":
        return Box(c - t / 2.0, a, z0, c + t / 2.0, b, z1)
    return Box(a, c - t / 2.0, z0, b, c + t / 2.0, z1)


def _add_opening_frame(scene: Scene, wall: RevitWall, o: RevitOpening, name: str,
                       a: float, b: float, z0: float, z1: float,
                       sides: str = "all") -> None:
    """Add a perimeter casing/frame around the opening rect ``[a,b] x [z0,z1]``.

    Four slim boxes — head, sill and two jambs — each ``_FRAME_WIDTH`` ft wide and
    standing ``_FRAME_PROUD`` ft proud of both wall faces, in the painted-trim
    material. ``sides`` is ``"all"`` (a window's full casing) or ``"nosill"`` (an
    exterior door's head + two jambs, no threshold band). One node per opening,
    named ``<name>:frame``, on the ``openings`` layer, so it toggles with the rest
    of the opening and exports as real trim. Deterministic; skipped if the opening
    is too small to carry a frame.
    """
    fw = _FRAME_WIDTH
    if b - a <= 2 * fw or z1 - z0 <= 2 * fw:
        return  # opening too small to seat a casing without the bands overlapping
    t = wall.thickness + 2 * _FRAME_PROUD
    node = scene.node(f"{name}:frame", "openings", TRIM_MATERIAL)
    add = node.add_box
    # Jambs run the full opening height on each running-axis edge; the head (and,
    # for a window, the sill) span the width between them.
    add(_panel_box(wall, a, a + fw, z0, z1, t))          # near jamb
    add(_panel_box(wall, b - fw, b, z0, z1, t))          # far jamb
    add(_panel_box(wall, a + fw, b - fw, z1 - fw, z1, t))  # head casing
    if sides != "nosill":
        add(_panel_box(wall, a + fw, b - fw, z0, z0 + fw, t))  # sill / apron


def _door_record(wall: RevitWall, o: RevitOpening, a: float, b: float,
                 base: float, mode: str, hinge_far: bool,
                 out: tuple[float, float]) -> dict:
    """The per-leaf animation record: hinge/anchor point, latch direction, swing side.

    ``hinge`` is the plan point of the hinge (swing) or the closed anchor edge
    (slide/overhead); ``dir`` a plan unit vector from the hinge toward the latch
    along the wall; ``out`` a plan unit vector across the wall toward the side the
    leaf opens onto. The viewer rotates a swing leaf about ``hinge`` toward ``out``,
    slides a panel along ``dir``, or lifts an overhead panel; all deterministic.
    """
    # Anchor at the low-coordinate end unless hinged "far"; dir points to the latch.
    if hinge_far:
        hx, hy = _slab_at(b, 0.0, wall)
        dx, dy = _slab_at(a, 0.0, wall)
    else:
        hx, hy = _slab_at(a, 0.0, wall)
        dx, dy = _slab_at(b, 0.0, wall)
    length = math.hypot(dx - hx, dy - hy) or 1.0

    def r(v: float) -> float:
        # Round to 4 places and collapse a signed zero so the JSON is stable and
        # never emits the noisy "-0.0".
        return round(v, 4) + 0.0

    return {
        "id": o.id,
        "mode": mode,
        "hinge": [r(hx), r(hy)],
        "dir": [r((dx - hx) / length), r((dy - hy) / length)],
        "out": [r(out[0]), r(out[1])],
        "width": r(b - a),
        "height": r(o.height),
    }


def _leaf_out(plan: Barndominium, wall: RevitWall, o: RevitOpening, leaf: float,
              room_pt: dict) -> tuple[float, float]:
    """The across-wall unit vector toward the side a hinged leaf ``leaf`` ft wide
    swings onto: the swing rule the plan drawings and the validator use
    (:func:`barndsl.drawing.swing_side`). An exterior door swings into its room."""
    a = plan.room(o.rooms[0]) if len(o.rooms) == 2 else None
    b = plan.room(o.rooms[1]) if len(o.rooms) == 2 else None
    if not o.exterior and a is not None and b is not None:
        edge = shared_edge(a, b)
        if edge is not None:
            sgn = swing_side(o.swing_into, a, b, edge, leaf)
            bx, by = _across_dir(wall)
            return (bx * sgn, by * sgn)
    return _swing_out(wall, o, room_pt)


def _swing_out(wall: RevitWall, o: RevitOpening, room_pt: dict) -> tuple[float, float]:
    """The across-wall unit vector toward the room an opening serves.

    Toward its ``swing_into`` room's centre when it names one, else toward its
    first room — for an exterior door, the room it opens into. With no room to
    consult we fall back to the wall's arbitrary ``_across_dir``.
    """
    base = _across_dir(wall)
    c = wall.const_coord
    target = o.swing_into if o.swing_into else (o.rooms[0] if o.rooms else None)
    pt = room_pt.get(target)
    if pt is None:
        return base
    side = (pt[0] if wall.orientation == "v" else pt[1]) - c
    sgn = 1.0 if side >= 0 else -1.0
    return (base[0] * sgn, base[1] * sgn)


_SWING_KINDS = frozenset({"swing", "exterior", "double", "french"})
#: A closed bifold reads as a flat panel across the opening, same as a slider —
#: modelling the fold would add nothing at this LOD.
_SLIDE_KINDS = frozenset({"sliding", "pocket", "bifold"})


def _add_opening_geometry(
    scene: Scene, wall: RevitWall, openings: list[RevitOpening], base: float,
    room_pt: dict,
) -> None:
    """Fill each hosted opening's void: a window's glazing, or a door's leaf/panel.

    Called once per wall run from :func:`_add_walls`, after the lintel/sill boxes.
    Windows get a glazing pane plus a cross of mullions (so they don't read as
    mirrors); swing doors get one leaf node per leaf (double/french two), each
    with its own ``door`` animation record; sliding/pocket get one closed panel;
    overhead a ribbed metal panel; cased openings get only a thin jamb casing.
    Node order follows ``openings`` order — the caller passes them in model order —
    so the whole scene stays deterministic.
    """
    for o in openings:
        span = _opening_span(wall, o)
        if span is None:
            continue
        a, b = span
        z0 = base + o.sill
        z1 = z0 + o.height
        if o.category == "window":
            _add_window(scene, wall, o, a, b, z0, z1)
            continue
        if o.category == "cased_opening":
            _add_cased_casing(scene, wall, o, a, b, z0, z1)
            continue
        # An exterior door gets a painted head + jamb trim (no threshold band)
        # proud of the wall, so the entry reads as cased like the windows. Interior
        # doors keep only their leaf. Added before the leaf so the node order is
        # frame-then-leaf, deterministically.
        if o.exterior:
            _add_opening_frame(scene, wall, o, f"door:{o.id}", a, b, z0, z1,
                               sides="nosill")
        if o.kind == "overhead":
            _add_overhead(scene, wall, o, a, b, z0, z1)
        elif o.kind in _SLIDE_KINDS:
            _add_slider(scene, wall, o, a, b, z0, z1, room_pt)
        else:  # swing / exterior / double / french
            _add_swing(scene, wall, o, a, b, z0, z1, room_pt)


def _add_window(scene: Scene, wall: RevitWall, o: RevitOpening,
                a: float, b: float, z0: float, z1: float) -> None:
    """A thin glazing pane centred in the wall, a centre cross of mullions, and a
    perimeter casing frame proud of both wall faces so the window reads as framed."""
    _add_opening_frame(scene, wall, o, f"window:{o.id}", a, b, z0, z1, sides="all")
    glass = scene.node(f"window:{o.id}:glass", "openings", GLASS_MATERIAL)
    glass.add_box(_panel_box(wall, a, b, z0, z1, _GLASS_THICKNESS))
    bars = scene.node(f"window:{o.id}:mullions", "openings", OPENING_MATERIAL)
    m = _MULLION
    mid_along = (a + b) / 2.0
    mid_z = (z0 + z1) / 2.0
    # A vertical bar down the centre and a horizontal bar across it, each a thin
    # square-section box a hair proud of the glass so it reads on both faces.
    bars.add_box(_panel_box(wall, mid_along - m / 2.0, mid_along + m / 2.0, z0, z1,
                            _GLASS_THICKNESS + 0.02))
    bars.add_box(_panel_box(wall, a, b, mid_z - m / 2.0, mid_z + m / 2.0,
                            _GLASS_THICKNESS + 0.02))


def _add_cased_casing(scene: Scene, wall: RevitWall, o: RevitOpening,
                      a: float, b: float, z0: float, z1: float) -> None:
    """A thin jamb + head casing around a leafless cased opening (no leaf, no panel)."""
    node = scene.node(f"cased:{o.id}:casing", "openings", DOOR_MATERIAL)
    m = _MULLION
    t = wall.thickness + 0.05  # wrap slightly proud of both wall faces
    node.add_box(_panel_box(wall, a, a + m, z0, z1, t))          # near jamb
    node.add_box(_panel_box(wall, b - m, b, z0, z1, t))          # far jamb
    node.add_box(_panel_box(wall, a, b, z1 - m, z1, t))          # head casing


def _add_swing(scene: Scene, wall: RevitWall, o: RevitOpening,
               a: float, b: float, z0: float, z1: float, room_pt: dict) -> None:
    """One leaf node per leaf, closed in the wall plane, each with a ``door`` record.

    Single doors hinge at the ``hinge`` end (``"near"`` = low-coordinate end,
    the default); double/french split into two half-width leaves hinged at
    opposite jambs. French leaves carry the glass pane material so they read as
    a glazed pair. Each leaf's record lets the viewer swing it about its own hinge.
    """
    double = o.kind in ("double", "french")
    out = _leaf_out(scene.plan, wall, o, (b - a) / 2.0 if double else b - a, room_pt)
    glassy = o.kind == "french"
    leaf_mat = GLASS_MATERIAL if glassy else DOOR_MATERIAL
    if double:
        mid = (a + b) / 2.0
        # Leaf 0 hinges at the near jamb; leaf 1 at the far jamb (mirror hinge).
        _swing_leaf(scene, wall, o, a, mid, z0, z1, leaf_mat, out,
                    hinge_far=False, suffix=":leaf0")
        _swing_leaf(scene, wall, o, mid, b, z0, z1, leaf_mat, out,
                    hinge_far=True, suffix=":leaf1")
    else:
        hinge_far = o.hinge == "far"
        _swing_leaf(scene, wall, o, a, b, z0, z1, leaf_mat, out,
                    hinge_far=hinge_far, suffix=":leaf")


def _swing_leaf(scene: Scene, wall: RevitWall, o: RevitOpening,
                a: float, b: float, z0: float, z1: float, mat: Material,
                out: tuple[float, float], hinge_far: bool, suffix: str) -> None:
    node = scene.node(f"door:{o.id}{suffix}", "openings", mat)
    node.add_box(_panel_box(wall, a, b, z0, z1, _LEAF_THICKNESS))
    node.door = _door_record(wall, o, a, b, o.sill, "swing", hinge_far, out)


def _add_slider(scene: Scene, wall: RevitWall, o: RevitOpening,
                a: float, b: float, z0: float, z1: float, room_pt: dict) -> None:
    """A single sliding/pocket panel, closed across the opening (mode ``"slide"``)."""
    node = scene.node(f"door:{o.id}:panel", "openings", DOOR_MATERIAL)
    node.add_box(_panel_box(wall, a, b, z0, z1, _LEAF_THICKNESS))
    node.door = _door_record(wall, o, a, b, o.sill, "slide", False,
                             _swing_out(wall, o, room_pt))


def _add_overhead(scene: Scene, wall: RevitWall, o: RevitOpening,
                  a: float, b: float, z0: float, z1: float) -> None:
    """A single ribbed-metal garage panel filling the opening (mode ``"overhead"``)."""
    node = scene.node(f"door:{o.id}:panel", "openings", PALETTE["metal_siding"])
    node.add_box(_panel_box(wall, a, b, z0, z1, _LEAF_THICKNESS))
    node.door = _door_record(wall, o, a, b, o.sill, "overhead", False,
                             _across_dir(wall))


def _wall_bounds(model: RevitModel) -> tuple[float, float, float, float]:
    """``(min_x, max_x, min_y, max_y)`` over every wall centreline (the footprint)."""
    xs = [p[0] for w in model.walls for p in (w.start, w.end)]
    ys = [p[1] for w in model.walls for p in (w.start, w.end)]
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def _wall_face(w: RevitWall, bounds: tuple[float, float, float, float]) -> str | None:
    """The compass face an exterior wall run belongs to, or ``None``.

    A run along the footprint's south edge faces south, and so on. Re-entrant
    exterior walls of an L/T/U footprint (exterior, but not on the outer bounds)
    get no face — the elevation renderer draws them behind the outer face anyway.
    """
    if not w.exterior:
        return None
    min_x, max_x, min_y, max_y = bounds
    tol = max(0.5, w.thickness)
    (x0, y0), (x1, y1) = w.start, w.end
    if abs(y0 - y1) <= tol:  # a run along x
        y = (y0 + y1) / 2.0
        if abs(y - min_y) <= tol:
            return "south"
        if abs(y - max_y) <= tol:
            return "north"
    elif abs(x0 - x1) <= tol:  # a run along y
        x = (x0 + x1) / 2.0
        if abs(x - min_x) <= tol:
            return "west"
        if abs(x - max_x) <= tol:
            return "east"
    return None


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
    # Room centres, for deciding which way a leaf swings (toward its room).
    room_pt = {r.id: r.point for r in model.rooms}
    hosted: dict[str, list[RevitOpening]] = {}
    for o in model.openings:
        if o.host_wall is not None:
            hosted.setdefault(o.host_wall, []).append(o)
    plate = roof_plate(model)
    gl = gable_line(model)
    # The exterior shell wears the plan's siding hint (ribbed metal by default); an
    # interior partition is painted drywall, not corrugated steel. Split by the
    # run's `exterior` flag — single-material boxes, so we don't split the interior
    # face of an exterior wall, only whole partition runs (Phase 6 requirement A).
    siding_mat = wall_material(scene.plan)
    partition_mat = PALETTE["drywall"]
    bounds = _wall_bounds(model)
    for w in model.walls:
        base = elev.get(w.level, 0.0)
        ops = hosted.get(w.id, [])
        wall_mat = siding_mat if w.exterior else partition_mat
        node = scene.node(f"wall:{w.id}", "walls", wall_mat)
        node.face = _wall_face(w, bounds)
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
        # The recognisable part of each opening — glazing, a door leaf, a garage
        # panel — also rides the openings layer (movable leaves carry a `door`
        # record for the walk-mode animation).
        _add_opening_geometry(scene, w, ops, base, room_pt)


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


# --- fixture / furniture massing ---------------------------------------------
#
# Each fixture becomes a small stack of axis-aligned boxes on its room's floor —
# recognisable, not detailed. A wall-backed fixture reads front-to-back from the
# wall it backs to (its "front" faces into the room); a free-standing piece is
# symmetric. Materials come from the palette: porcelain plumbing, stainless
# appliances, fabric upholstery, wood casework.

#: base body material per fixture kind.
_FIXTURE_MATERIAL: dict[str, Material] = {
    "toilet": FIXTURE_PORCELAIN, "lavatory": FIXTURE_PORCELAIN, "tub": FIXTURE_PORCELAIN,
    "shower": FIXTURE_PORCELAIN, "sink": FIXTURE_PORCELAIN,
    "refrigerator": FIXTURE_STAINLESS, "range": FIXTURE_STAINLESS,
    "washer": FIXTURE_STAINLESS, "dryer": FIXTURE_STAINLESS, "water_heater": FIXTURE_STAINLESS,
    "sofa": FIXTURE_FABRIC, "armchair": FIXTURE_FABRIC,
    "bed_queen": FIXTURE_WOOD, "bed_twin": FIXTURE_WOOD,
    "dining_table": FIXTURE_WOOD, "coffee_table": FIXTURE_WOOD, "desk": FIXTURE_WOOD,
    "dresser": FIXTURE_WOOD, "kitchen_island": FIXTURE_WOOD, "counter": FIXTURE_WOOD,
    "wardrobe": FIXTURE_WOOD,
}

#: a light countertop / lid material (island & counter tops) and a dark cooktop.
_FIXTURE_TOP = PALETTE["drywall"]
_FIXTURE_COOKTOP = PALETTE["standing_seam"]


def _add_fixtures(scene: Scene) -> None:
    model = scene.model
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    for fx in model.fixtures:
        z = elev.get(fx.level, 0.0)
        _fixture_massing(scene, fx, z)


def _fixture_massing(scene: Scene, fx, z: float) -> None:
    x0, y0 = fx.x, fx.y
    x1, y1 = fx.x + fx.width, fx.y + fx.length
    mat = _FIXTURE_MATERIAL.get(fx.kind, FIXTURE_WOOD)
    body = scene.node(f"fixture:{fx.id or fx.kind}", "fixtures", mat)
    add = body.add_box
    wall = fx.wall

    def frac(a: float, b: float, lo: float, hi: float) -> tuple[float, float]:
        """A sub-interval of ``[a, b]`` at fractions ``[lo, hi]``."""
        return a + (b - a) * lo, a + (b - a) * hi

    # `back`/`front` split along the depth axis, measured from the backing wall.
    def depth_split(lo: float, hi: float) -> tuple[float, float, float, float]:
        """The footprint sub-box spanning ``[lo, hi]`` fraction from the wall."""
        if wall == "S":
            ya, yb = frac(y0, y1, lo, hi)
            return x0, ya, x1, yb
        if wall == "N":
            ya, yb = frac(y1, y0, lo, hi)
            return x0, yb, x1, ya
        if wall == "W":
            xa, xb = frac(x0, x1, lo, hi)
            return xa, y0, xb, y1
        if wall == "E":
            xa, xb = frac(x1, x0, lo, hi)
            return xb, y0, xa, y1
        # free-standing: split along the longer footprint axis, from the low end.
        if (x1 - x0) >= (y1 - y0):
            xa, xb = frac(x0, x1, lo, hi)
            return xa, y0, xb, y1
        ya, yb = frac(y0, y1, lo, hi)
        return x0, ya, x1, yb

    k = fx.kind
    if k == "toilet":
        bx0, by0, bx1, by1 = depth_split(0.0, 0.42)  # tank against the wall
        add(Box(bx0, by0, z, bx1, by1, z + 2.5))
        bx0, by0, bx1, by1 = depth_split(0.42, 1.0)  # bowl
        add(_inset(bx0, by0, bx1, by1, 0.15, z, z + 1.3))
    elif k == "lavatory" or k == "sink":
        add(Box(x0, y0, z, x1, y1, z + 2.8))
        basin = scene.node(f"fixture:{fx.id}:basin", "fixtures", FIXTURE_PORCELAIN)
        basin.add_box(_inset(x0, y0, x1, y1, 0.25, z + 2.6, z + 2.85))
    elif k == "tub":
        add(Box(x0, y0, z, x1, y1, z + 0.5))  # apron
        add(Box(x0, y0, z + 0.5, x1, y1, z + 2.0))  # a solid tub body (rim height)
        inner = scene.node(f"fixture:{fx.id}:basin", "fixtures", FIXTURE_PORCELAIN)
        inner.add_box(_inset(x0, y0, x1, y1, 0.35, z + 0.9, z + 1.95))
    elif k == "shower":
        add(Box(x0, y0, z, x1, y1, z + 0.4))  # pan / curb
        # a back panel up the backing wall (or the low-x side when free-standing).
        bx0, by0, bx1, by1 = depth_split(0.0, 0.12)
        add(Box(bx0, by0, z + 0.4, bx1, by1, z + 6.5))
    elif k == "refrigerator":
        add(Box(x0, y0, z, x1, y1, z + 5.8))
        door = scene.node(f"fixture:{fx.id}:door", "fixtures", FIXTURE_STAINLESS)
        dx0, dy0, dx1, dy1 = depth_split(0.9, 1.0)  # a shallow front face proud
        door.add_box(Box(dx0, dy0, z + 0.6, dx1, dy1, z + 5.6))
    elif k == "range":
        add(Box(x0, y0, z, x1, y1, z + 2.95))
        top = scene.node(f"fixture:{fx.id}:cooktop", "fixtures", _FIXTURE_COOKTOP)
        top.add_box(Box(x0, y0, z + 2.95, x1, y1, z + 3.05))  # a darker cooktop lid
    elif k in ("washer", "dryer"):
        add(Box(x0, y0, z, x1, y1, z + 3.0))
        door = scene.node(f"fixture:{fx.id}:door", "fixtures", _FIXTURE_COOKTOP)
        dx0, dy0, dx1, dy1 = depth_split(0.9, 1.0)
        door.add_box(_inset(dx0, dy0, dx1, dy1, 0.25, z + 1.2, z + 2.6))
    elif k == "water_heater":
        add(Box(x0, y0, z, x1, y1, z + 4.6))
    elif k in ("kitchen_island", "counter"):
        add(Box(x0, y0, z, x1, y1, z + 2.9))  # cabinet body
        top = scene.node(f"fixture:{fx.id}:top", "fixtures", _FIXTURE_TOP)
        top.add_box(_inset(x0, y0, x1, y1, -0.08, z + 2.9, z + 3.05))  # a proud lighter top
    elif k == "dresser":
        add(Box(x0, y0, z, x1, y1, z + 3.0))
    elif k == "wardrobe":
        add(Box(x0, y0, z, x1, y1, z + 6.0))
    elif k in ("bed_queen", "bed_twin"):
        add(Box(x0, y0, z, x1, y1, z + 1.0))  # platform
        mat_box = scene.node(f"fixture:{fx.id}:mattress", "fixtures", FIXTURE_FABRIC)
        mat_box.add_box(_inset(x0, y0, x1, y1, 0.08, z + 1.0, z + 1.8))
        px0, py0, px1, py1 = depth_split(0.0, 0.2)  # pillows at the head (wall side)
        mat_box.add_box(_inset(px0, py0, px1, py1, 0.15, z + 1.8, z + 2.15))
    elif k in ("sofa", "armchair"):
        add(Box(x0, y0, z, x1, y1, z + 1.4))  # seat
        bx0, by0, bx1, by1 = depth_split(0.0, 0.2)  # back against the wall
        add(Box(bx0, by0, z + 1.4, bx1, by1, z + 2.7))
        # arms down the two sides perpendicular to the wall.
        if wall in ("S", "N", ""):
            add(Box(x0, y0, z + 1.4, x0 + 0.5, y1, z + 2.2))
            add(Box(x1 - 0.5, y0, z + 1.4, x1, y1, z + 2.2))
        else:
            add(Box(x0, y0, z + 1.4, x1, y0 + 0.5, z + 2.2))
            add(Box(x0, y1 - 0.5, z + 1.4, x1, y1, z + 2.2))
    elif k in ("dining_table", "coffee_table", "desk"):
        top_z = 2.4 if k != "coffee_table" else 1.4
        add(Box(x0, y0, z + top_z - 0.2, x1, y1, z + top_z))  # top slab
        _legs(add, x0, y0, x1, y1, z, top_z - 0.2)
    else:  # any unmapped kind: a plain block, so it still reads as *something*.
        add(Box(x0, y0, z, x1, y1, z + 2.5))


def _inset(x0, y0, x1, y1, d, z0, z1) -> Box:
    """A box inset (or, negative ``d``, expanded) by ``d`` ft on all four sides."""
    return Box(x0 + d, y0 + d, z0, x1 - d, y1 - d, z1)


def _legs(add, x0, y0, x1, y1, z, top: float, s: float = 0.2) -> None:
    """Four corner legs from the floor to ``z + top`` under a table/desk slab."""
    for cx in (x0, x1 - s):
        for cy in (y0, y1 - s):
            add(Box(cx, cy, z, cx + s, cy + s, z + top))


# --- interior realism: baseboards + flat ceilings ----------------------------
#
# Phase 7 ships two finish layers as *real geometry* (they export like everything
# else, unlike the renderer-only sky/ground): a skirting run around each finished
# room's perimeter, and a flat ceiling slab over each private room. Both are thin,
# schematic and deterministic — a reveal, not a moulding profile — so the model
# reads as a finished interior without over-modelling. The walkthrough is the
# audience: a baseboard grounds the walls and a ceiling encloses a bedroom, while
# the great room stays open to the roof (the barndominium's vaulted signature).

#: Skirting: 0.35 ft tall, set 0.05 ft into the room off the finish-face, sitting
#: on the finished floor tile top (:data:`FLOOR_TILE_THICKNESS`). A door/cased gap
#: is punched where an opening touches the room edge.
_BASEBOARD_HEIGHT = 0.35
_BASEBOARD_DEPTH = 0.05
#: A flat ceiling slab thickness (ft) at the room's clear storey height.
_CEILING_THICKNESS = 0.1

#: Bare-slab spaces skip a baseboard (a garage/shop is unfinished; a porch is not
#: an interior room and never reaches this code): they get no skirting run.
_NO_BASEBOARD_TYPES = frozenset({"garage", "shop", "porch"})

#: Rooms that get a **flat ceiling** slab — the private, enclosed spaces where a
#: dropped ceiling reads right. Living / great / kitchen / dining / loft stay open
#: to the roof (the vaulted barndominium look), as do bare-slab garage/shop and
#: the porch. Names match :class:`~barndsl.elements.RoomType` values.
_CEILING_ROOM_TYPES = frozenset(
    {"bedroom", "bathroom", "half_bath", "closet", "pantry", "storage",
     "office", "flex", "rec_room", "laundry", "utility", "mechanical",
     "mudroom", "foyer", "hallway", "safe_room"}
)


def _edge_openings(model, walls_by_id, room, coord: float, vertical: bool) -> list[
    tuple[float, float]
]:
    """The door/cased-opening spans that touch ``room``'s edge, as running-axis gaps.

    A skirting run stops at a doorway. An opening touches the edge when its host
    wall lies on the edge line (constant coord ``coord``: the room's ``x``/``x2``
    for a vertical W/E edge, ``y``/``y2`` for a horizontal S/N edge) and its span
    overlaps the room's extent along that edge. Windows never punch a baseboard (a
    sill sits above it), so only door-category and cased openings are gaps.
    ``walls_by_id`` is the run lookup the caller builds once. Returned sorted, so the
    skirting decomposition is deterministic.
    """
    lo_room = room.y if vertical else room.x
    hi_room = (room.y + room.length) if vertical else (room.x + room.width)
    gaps: list[tuple[float, float]] = []
    for o in model.openings:
        if o.category not in ("door", "cased_opening"):
            continue
        w = walls_by_id.get(o.host_wall)
        if w is None:
            continue
        if (w.orientation == "v") != vertical:
            continue
        if abs(w.const_coord - coord) > 0.5:  # host wall not on this room edge
            continue
        along = o.location[1] if vertical else o.location[0]
        a = max(lo_room, along - o.width / 2.0)
        b = min(hi_room, along + o.width / 2.0)
        if b - a > 1e-6:
            gaps.append((a, b))
    gaps.sort()
    return gaps


def _runs_minus_gaps(lo: float, hi: float, gaps: list[tuple[float, float]]) -> list[
    tuple[float, float]
]:
    """``[lo, hi]`` with each ``gap`` cut out — the surviving solid skirting pieces."""
    runs = [(lo, hi)]
    for ga, gb in gaps:
        out: list[tuple[float, float]] = []
        for a, b in runs:
            if gb <= a + 1e-9 or ga >= b - 1e-9:
                out.append((a, b))
                continue
            if ga > a:
                out.append((a, min(ga, b)))
            if gb < b:
                out.append((max(gb, a), b))
        runs = out
    return [(a, b) for a, b in runs if b - a > 1e-6]


def _add_baseboards(scene: Scene) -> None:
    """A skirting run around each finished room's perimeter, with door gaps punched.

    One node per room (``baseboard:<id>``) on the ``trim`` layer in the painted-trim
    material. The run hugs the four room edges, set :data:`_BASEBOARD_DEPTH` ft into
    the room off the finish face and standing :data:`_BASEBOARD_HEIGHT` ft on the
    finished floor. Garage/shop bays (bare slab) get none. Door and cased-opening
    gaps are cut so the skirting reads as stopping at each casing. Deterministic:
    rooms in model order, each edge S, N, W, E, each edge's runs low→high.
    """
    model = scene.model
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    walls_by_id = {w.id: w for w in model.walls}
    d = _BASEBOARD_DEPTH
    for r in model.rooms:
        if r.type in _NO_BASEBOARD_TYPES:
            continue
        z0 = elev.get(r.level, 0.0) + FLOOR_TILE_THICKNESS
        z1 = z0 + _BASEBOARD_HEIGHT
        x0, y0 = r.x, r.y
        x1, y1 = r.x + r.width, r.y + r.length
        node = scene.node(f"baseboard:{r.id}", "trim", TRIM_MATERIAL)

        def edge(coord, vertical):
            return _edge_openings(model, walls_by_id, r, coord, vertical)

        # Horizontal edges (S at y0, N at y1) run along x; vertical edges (W at x0,
        # E at x1) run along y. Each edge's skirting box is a thin band along the
        # edge, `d` deep into the room, minus the door/cased gaps on that edge.
        # South edge (y = y0), running along x, band reaches north into the room.
        for lo, hi in _runs_minus_gaps(x0, x1, edge(y0, False)):
            node.add_box(Box(lo, y0, z0, hi, y0 + d, z1))
        # North edge (y = y1), band reaches south into the room.
        for lo, hi in _runs_minus_gaps(x0, x1, edge(y1, False)):
            node.add_box(Box(lo, y1 - d, z0, hi, y1, z1))
        # West edge (x = x0), running along y, band reaches east into the room;
        # trim the y-range to skip the corners the S/N bands already cover.
        for lo, hi in _runs_minus_gaps(y0, y1, edge(x0, True)):
            node.add_box(Box(x0, max(lo, y0 + d), z0, x0 + d, min(hi, y1 - d), z1))
        # East edge (x = x1), band reaches west into the room.
        for lo, hi in _runs_minus_gaps(y0, y1, edge(x1, True)):
            node.add_box(Box(x1 - d, max(lo, y0 + d), z0, x1, min(hi, y1 - d), z1))


def _add_ceilings(scene: Scene) -> None:
    """A flat ceiling slab over each private room at its clear storey height.

    One node per qualifying room (``ceiling:<id>``) on the ``ceilings`` layer in the
    drywall material. The slab sits at the room's finished ceiling — the level floor
    elevation plus the room's clear ceiling height (its override or the plan default)
    — exactly where the walls' plate is computed (see :mod:`barndsl.wallheights`,
    which extrudes every run to ``elevation + ceiling_height``), so the ceiling caps
    the walls with no gap. Only the private, enclosed room types get one (see
    :data:`_CEILING_ROOM_TYPES`); living/great/kitchen/dining/loft stay open to the
    roof (the vaulted barndominium signature), and a room the author marked
    ``vaulted`` is skipped too. Deterministic: rooms in model order.
    """
    model = scene.model
    plan = scene.plan
    elev = {lvl.index: lvl.elevation for lvl in model.levels}
    for r in model.rooms:
        if r.type not in _CEILING_ROOM_TYPES or r.vaulted:
            continue
        ch = r.ceiling_height if r.ceiling_height else plan.ceiling_height
        top = elev.get(r.level, 0.0) + ch
        node = scene.node(f"ceiling:{r.id}", "ceilings", PALETTE["drywall"])
        node.add_box(Box(r.x, r.y, top - _CEILING_THICKNESS, r.x + r.width,
                         r.y + r.length, top))


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
    _add_fixtures(scene)
    _add_baseboards(scene)
    _add_ceilings(scene)
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
