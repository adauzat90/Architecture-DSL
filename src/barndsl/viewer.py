"""A single self-contained HTML 3D viewer for a compiled plan.

``write_viewer(plan, path)`` produces **one** HTML file — no network, no CDN, no
build step — that opens the plan's 3D model by double-clicking it. The scene is
the same box/quad lowering :mod:`barndsl.gltf` builds (via :func:`build_scene`),
serialised into a compact inline JSON blob, and rendered by a small inline WebGL
renderer: orbit / pan / zoom, a directional light with a sky-fill term and a
roughness/metallic specular, procedural surface textures, and a layer-toggle panel
(walls, roof, frame, floors, porches, openings, stairs — the roof toggle lets you
look inside). No external dependencies.

A **first-person walk mode** is layered over the orbit camera: a "Walk" pill (or
the Enter key) drops you inside at a 5.5 ft eye height, WASD + mouse look, sliding
along the walls and ramping up the stairs. It reads a small ``walk`` block
(:func:`_walk_block`) computed from the Revit exchange — wall collision segments
with door gaps punched out, per-level slab footprints, stair ramps and a spawn
point — not from the triangles, so collision is exact plan feet.

Each surface carries a material from :mod:`barndsl.materials` — base colour,
roughness/metallic, and a procedural ``pattern`` (ribbed metal, board-and-batten,
shingle courses, plank flooring, tile, concrete speckle). The renderer draws one
small **detail texture per pattern** on an offscreen canvas at load time (no
network) and maps it **triplanarly** in the shader — the projection plane is
chosen by the dominant world-normal axis, so the mostly axis-aligned boxes and
sloped roof quads texture correctly without any UV arrays in the scene JSON.
Pattern scale is in feet, matching the world units, so ribs and planks read at
true size.

The renderer is inline WebGL rather than a Three.js CDN import precisely because
the geometry is trivial — axis-aligned boxes and a handful of sloped quads — so a
compact single-pass shader is all it takes, and the file stays offline and
self-contained (the property the design doc asks for). Styling is light and clean,
consistent with a design tool; the plan title and a few key metrics (square
footage, bed/bath) sit in a header.

The WebGL renderer itself lives in :data:`RENDERER_JS` — a single JavaScript
function, ``mountScene(canvas, labels, togglesEl)``, shared verbatim by this
single-file viewer and the web playground (:mod:`barndsl.playground`) so the two
never diverge. It returns a controller whose ``setScene(json)`` loads (or swaps)
the geometry the :func:`scene_json` helper produces.

The embedded geometry is in the same glTF y-up frame the exporter uses (plan
``(x, y, z)`` → ``(x, z, -y)``), so the viewer and any external glTF viewer agree.
"""

from __future__ import annotations

import json
import math

from .elements import Barndominium
from .gltf import Scene, _to_gltf, build_scene, effective_linear
from .materials import GLASS_MATERIAL

#: Human labels for the layer toggles, in display order.
_LAYER_LABELS = {
    "walls": "Walls",
    "roof": "Roof",
    "frame": "Frame",
    "floors": "Floors",
    "porches": "Porches",
    "openings": "Openings",
    "trim": "Trim",
    "ceilings": "Ceilings",
    "stairs": "Stairs",
    "fixtures": "Fixtures",
}


#: Player circle radius (ft) and eye height (ft) for the first-person walk mode —
#: the JS renderer reads ``eyeHeight`` from the walk block so this stays the one
#: authority. 5.5 ft puts the camera at a typical standing eye level.
WALK_EYE_HEIGHT = 5.5


def _subtract_intervals(
    lo: float, hi: float, gaps: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """``[lo, hi]`` with each ``gap`` span cut out — the surviving solid pieces.

    Used to punch **door-type openings** out of a wall run's collision span: a
    door (or a doorless cased passage) is a walkable gap, so removing it leaves the
    two solid piers either side. Windows are *not* passed as gaps — you can't walk
    through a window — so a run with only windows stays one segment.
    """
    segs = [(lo, hi)]
    for ga, gb in sorted(gaps):
        out: list[tuple[float, float]] = []
        for a, b in segs:
            if gb <= a + 1e-9 or ga >= b - 1e-9:
                out.append((a, b))  # gap misses this piece
                continue
            if ga > a:
                out.append((a, min(ga, b)))
            if gb < b:
                out.append((max(gb, a), b))
        segs = out
    return [(a, b) for a, b in segs if b - a > 1e-6]


def _walk_block(scene: Scene) -> dict:
    """Walk-support data derived from the **exchange**, not from triangles.

    A first-person walkthrough needs three things the mesh soup doesn't hand it
    cheaply: where the walls are (to slide along), where the floor is at each
    level (to set eye height), and where the stairs ramp between levels (to climb).
    We read them straight off the Revit-shaped :class:`~barndsl.revit.RevitModel`
    (deduplicated wall runs, per-level slabs, planned stair runs) so the numbers
    are exact plan feet and deterministic — the same order every call.

    The block is all in **plan** coordinates (``x`` east, ``y`` north, ``z`` up);
    the JS maps a plan point ``(x, y)`` to the viewer's glTF frame ``(x, z, -y)``
    at draw time, so movement and collision stay in the simple 2D plan space.

    Keys:

    * ``segments`` — 2D wall collision segments ``{x0, y0, x1, y1, level,
      elevation}``. Each wall run contributes its centreline minus every hosted
      **door / cased-opening** span (walkable gaps); windows stay solid. ``elevation``
      is the run's level floor elevation, so the JS collides only with walls near
      the player's current storey.
    * ``doors`` — one entry per **door-category** opening ``{id, x0, y0, x1, y1,
      elevation, level}``: the door's span segment on the wall centreline. The JS
      treats it as a live collision segment while the leaf is closed and drops it
      as the leaf swings open. Cased openings get no entry (always an open passage).
    * ``floors`` — per level ``{level, elevation, rects}`` (the slab footprint
      rectangles), so the JS knows where floor exists and how high the eye sits.
    * ``stairs`` — per stair run ``{x, y, w, l, fromLevel, toLevel, fromElevation,
      toElevation, dir}`` — the footprint, the two floor elevations and an uphill
      unit direction, enough for the JS to lerp eye height across the footprint so
      you can walk up.
    * ``fixtures`` — one ``{x, y, w, l, elevation, level, kind}`` per model
      fixture (its axis-aligned footprint rect, the level floor elevation, and the
      catalog kind). The JS collides the player circle against these rects when the
      furniture-collision toggle is on, using the same storey-elevation filter as
      the walls, so bumping into a sofa or kitchen island is felt.
    * ``rooms`` — one ``{id, name, x, y, w, l, elevation, level, area}`` per model
      room (its axis-aligned rectangle, the level floor elevation, the display
      name schedules show, and the plan area). The JS draws faint room outlines on
      the mini-map and point-in-rects the player against them for the room-name
      toast; ``elevation`` lets it filter to the player's current storey.
    * ``spawn`` — ``{x, y, elevation, face}`` a start point just inside the main
      entry door (facing into the house) if one exists, else the centroid of the
      largest ground-floor room.
    * ``eyeHeight`` — the standing eye height (ft) above the current floor.
    """
    model = scene.model
    elev = {lvl.index: lvl.elevation for lvl in model.levels}

    # --- collision segments: wall runs minus door/cased gaps ------------------
    hosted: dict[str, list] = {}
    for o in model.openings:
        if o.host_wall is not None:
            hosted.setdefault(o.host_wall, []).append(o)
    segments: list[dict] = []
    # A `doors` entry per door-category opening: the span segment on the wall
    # centreline, which the renderer turns into a *live* collision segment while
    # the leaf is closed (and drops while it swings open). A cased opening gets
    # no entry — it is always an open passage — so it never re-blocks the gap.
    doors: list[dict] = []
    for w in model.walls:
        vertical = w.orientation == "v"
        c = w.const_coord
        lo, hi = w.span
        z = elev.get(w.level, 0.0)
        gaps: list[tuple[float, float]] = []
        for o in hosted.get(w.id, []):
            # A door or a doorless cased opening is a walkable gap; a window is not.
            if o.category not in ("door", "cased_opening"):
                continue
            along = o.location[1] if vertical else o.location[0]
            a = max(lo, along - o.width / 2.0)
            b = min(hi, along + o.width / 2.0)
            if b <= a:
                continue
            gaps.append((a, b))
            if o.category == "door":  # a leafed door can re-close the gap
                if vertical:
                    x0, y0, x1, y1 = c, a, c, b
                else:
                    x0, y0, x1, y1 = a, c, b, c
                doors.append({
                    "id": o.id,
                    "x0": round(x0, 4), "y0": round(y0, 4),
                    "x1": round(x1, 4), "y1": round(y1, 4),
                    "elevation": round(z, 4), "level": w.level,
                })
        for a, b in _subtract_intervals(lo, hi, gaps):
            if vertical:
                seg = {"x0": c, "y0": a, "x1": c, "y1": b}
            else:
                seg = {"x0": a, "y0": c, "x1": b, "y1": c}
            seg.update({"level": w.level, "elevation": round(z, 4)})
            segments.append({k: (round(v, 4) if isinstance(v, float) else v)
                             for k, v in seg.items()})

    # --- floors: slab footprints per level ------------------------------------
    rects_by_level: dict[int, list[list[float]]] = {}
    for s in model.slabs:
        rects_by_level.setdefault(s.level, []).append(
            [round(s.x, 4), round(s.y, 4), round(s.width, 4), round(s.length, 4)]
        )
    floors = [
        {"level": lvl, "elevation": round(elev.get(lvl, 0.0), 4), "rects": rects_by_level[lvl]}
        for lvl in sorted(rects_by_level)
    ]

    # --- stairs: footprint, the two floor elevations, an uphill direction -----
    stairs: list[dict] = []
    for a in model.areas:
        if a.kind != "stair":
            continue
        from_level = int(a.meta.get("from_level", a.level))
        to_level = int(a.meta.get("to_level", from_level + 1))
        # Uphill direction = the footprint's long axis, signed by the first run's
        # travel (its start sits at the bottom). Straight runs get exact ramps;
        # a switchback is approximated as one ramp across the long axis (you can
        # still walk up), which is fine for a schematic walkthrough.
        long_axis = (0.0, 1.0) if a.length >= a.width else (1.0, 0.0)
        runs = (a.meta.get("plan") or {}).get("runs") or []
        sign = 1.0
        if runs:
            (sx, sy), (ex, ey) = runs[0]["start"], runs[0]["end"]
            if (ex - sx) * long_axis[0] + (ey - sy) * long_axis[1] < 0:
                sign = -1.0
        stairs.append(
            {
                "x": round(a.x, 4), "y": round(a.y, 4),
                "w": round(a.width, 4), "l": round(a.length, 4),
                "fromLevel": from_level, "toLevel": to_level,
                "fromElevation": round(elev.get(from_level, 0.0), 4),
                "toElevation": round(elev.get(to_level, 0.0), 4),
                "dir": [long_axis[0] * sign, long_axis[1] * sign],
            }
        )

    # --- fixtures: footprint rects for optional furniture collision -----------
    # One rect per model fixture, in model order (deterministic). The width/length
    # already bake in the fixture's quarter-turn rotation, so the rect is
    # axis-aligned and the JS collides its player circle against it directly. The
    # elevation is the fixture's level floor, so the same storey filter the walls
    # use keeps you from bumping into a sofa on the floor above.
    fixtures = [
        {
            "x": round(fx.x, 4), "y": round(fx.y, 4),
            "w": round(fx.width, 4), "l": round(fx.length, 4),
            "elevation": round(elev.get(fx.level, 0.0), 4),
            "level": fx.level, "kind": fx.kind,
        }
        for fx in model.fixtures
    ]

    # --- rooms: labelled rectangles for the mini-map + room-name toast --------
    # One rect per model room, in model order (deterministic). ``name`` is the
    # room's *display* name — the same string the schedules show — so a toast reads
    # "Primary Bedroom", not "primary_bedroom"; RevitRoom.name already resolves the
    # label-or-title-cased-id fallback (see revit.py's `to_revit_model`). The
    # elevation is the room's level floor, so the JS filters rooms to the player's
    # current storey exactly as it does walls and fixtures.
    rooms = [
        {
            "id": r.id, "name": r.name,
            "x": round(r.x, 4), "y": round(r.y, 4),
            "w": round(r.width, 4), "l": round(r.length, 4),
            "elevation": round(elev.get(r.level, 0.0), 4),
            "level": r.level, "area": round(r.area, 4),
        }
        for r in model.rooms
    ]

    return {
        "eyeHeight": WALK_EYE_HEIGHT,
        "spawn": _walk_spawn(model, elev),
        "segments": segments,
        "doors": doors,
        "floors": floors,
        "stairs": stairs,
        "fixtures": fixtures,
        "rooms": rooms,
    }


def _walk_spawn(model, elev: dict) -> dict:
    """A sensible first-person start point, facing into the house.

    Prefers a spot two feet inside the **main entry** — the first ground-floor
    exterior door (an egress door wins the tie), stepped in toward the room it
    serves so the camera opens on the interior, not the door leaf. With no exterior
    door it falls back to the centroid of the largest ground-floor room. ``face``
    is a plan unit vector the JS turns into the initial look yaw.
    """
    rooms0 = [r for r in model.rooms if r.level == 0]
    room_pt = {r.id: r.point for r in model.rooms}

    def norm2(dx: float, dy: float) -> list[float]:
        m = math.hypot(dx, dy) or 1.0
        return [round(dx / m, 4), round(dy / m, 4)]

    doors = [o for o in model.openings
             if o.category == "door" and o.exterior and o.level == 0]
    doors.sort(key=lambda o: (not o.egress, o.id))  # an egress entry first
    for o in doors:
        rid = o.rooms[0] if o.rooms else None
        centre = room_pt.get(rid)
        if centre is None:
            continue
        lx, ly = o.location
        inward = norm2(centre[0] - lx, centre[1] - ly)
        return {
            "x": round(lx + inward[0] * 2.0, 4),
            "y": round(ly + inward[1] * 2.0, 4),
            "elevation": round(elev.get(0, 0.0), 4),
            "face": inward,
        }

    if rooms0:
        r = max(rooms0, key=lambda r: r.area)
        # Face toward the ground-floor centroid so the opening view looks across
        # the plan rather than into the nearest wall.
        cx = sum(rr.point[0] for rr in rooms0) / len(rooms0)
        cy = sum(rr.point[1] for rr in rooms0) / len(rooms0)
        face = norm2(cx - r.point[0], cy - r.point[1])
        if face == [0.0, 0.0]:
            face = [0.0, 1.0]
        return {"x": round(r.point[0], 4), "y": round(r.point[1], 4),
                "elevation": round(elev.get(0, 0.0), 4), "face": face}

    return {"x": 0.0, "y": 0.0, "elevation": round(elev.get(0, 0.0), 4), "face": [0.0, 1.0]}


def scene_json(scene: Scene) -> dict:
    """The scene as inline-renderable JSON: one entry per non-empty node.

    Vertices/normals are pre-transformed into the glTF y-up frame and flattened;
    colours are linear-space RGB so the shader can light them directly. Each node
    also carries its material's ``roughness``/``metallic`` factors and a procedural
    ``pattern`` (with ``patternScale`` in feet) the shared renderer turns into a
    triplanar-mapped texture. A ``walk`` block (see :func:`_walk_block`) rides
    alongside the nodes so the shared renderer's first-person mode has wall
    collision, per-level floors and stairs without re-deriving them from triangles.
    This is the exact blob the viewer embeds and the playground returns, so the
    shared :data:`RENDERER_JS` renderer draws both from one code path.
    """
    nodes = []
    for n in scene.nodes:
        if n.empty:
            continue
        verts: list[float] = []
        norms: list[float] = []
        for p in n.positions:
            verts.extend(_to_gltf(p))
        for v in n.normals:
            norms.extend(_to_gltf(v))
        mat = n.material
        entry = {
            "name": n.name,
            "layer": n.layer,
            # A short display name for the material ("metal siding", "drywall") so
            # click-to-identify can name the surface's finish without the JS having a
            # material catalogue. Viewer-JSON only — the glTF/IFC exporters never see
            # it — and small (one ASCII string per node).
            "mat": mat.name,
            "color": effective_linear(mat, n.tint)[:3],
            "roughness": round(mat.roughness, 3),
            "metallic": round(mat.metallic, 3),
            "pattern": mat.pattern,
            "patternScale": mat.pattern_scale,
            "positions": [round(x, 4) for x in verts],
            "normals": [round(x, 4) for x in norms],
            "indices": n.indices,
        }
        # A movable door/panel leaf carries its animation record (hinge/dir/out in
        # plan coords); the renderer's walk mode swings/slides/lifts it about that
        # hinge. Static nodes (walls, glazing, casing) omit the key entirely, so
        # the orbit path and the exported glb — which bake closed geometry — agree.
        if n.door is not None:
            entry["door"] = n.door
        # A glazing pane ships ``isGlass`` so the renderer can draw it LAST in a
        # blended, depth-write-off pass (see-through windows) and keep it out of the
        # shadow-caster set (a window lets sun THROUGH, it doesn't cast a dark
        # patch). Identified by the shared glass material, so only true glazing
        # carries it; every opaque surface omits the key. Viewer-JSON only — the
        # glTF/IFC exporters never see it.
        if n.material is GLASS_MATERIAL:
            entry["isGlass"] = True
        nodes.append(entry)
    layers = [ly for ly in Scene.LAYERS if any(nd["layer"] == ly for nd in nodes)]
    return {"nodes": nodes, "layers": layers, "walk": _walk_block(scene),
            "sun": _sun_block(scene)}


#: Fixed default site latitude (deg N) for the sun-study model. The DSL carries no
#: site latitude, so a mid-latitude default gives a representative sun arc; it is a
#: viewer-only render input, never exported. ~35 deg N is the US "Sun Belt" band.
_SUN_DEFAULT_LATITUDE = 35.0


def _sun_block(scene: Scene) -> dict:
    """Sun-study inputs for the renderer: plan orientation + a default latitude.

    ``orientation`` is the plan's true-north azimuth (degrees clockwise from true
    north that plan-north points), ``0.0`` when the plan declares none, so the
    renderer's solar-position model honours the compass. ``latitude`` is a fixed
    default (:data:`_SUN_DEFAULT_LATITUDE`) — the DSL has no site latitude. This
    block is **viewer-JSON only**; it never reaches the glTF/IFC exporters.
    """
    orientation = scene.plan.orientation
    return {
        "orientation": round(float(orientation), 4) if orientation is not None else 0.0,
        "latitude": _SUN_DEFAULT_LATITUDE,
    }


#: Back-compat/internal alias — :func:`scene_json` was ``_scene_json``.
_scene_json = scene_json


def viewer_html(plan: Barndominium, scene: Scene | None = None) -> str:
    """Return the self-contained viewer HTML document for ``plan``."""
    scene = scene or build_scene(plan)
    data = scene_json(scene)
    m = plan.metrics()
    stats = (
        f"{m['footprint_sqft']:.0f} sq ft &middot; "
        f"{int(m['bedroom_count'])} bed / {m['bathroom_count']:g} bath &middot; "
        f"{m['interior_sqft']:.0f} sq ft interior"
    )
    title = plan.name
    payload = json.dumps(data, separators=(",", ":"))
    labels = json.dumps(_LAYER_LABELS)
    # The renderer is injected as a value (not through str.format), so its own
    # braces need no doubling; only the CSS braces in _TEMPLATE are doubled.
    return _TEMPLATE.format(
        title=_esc(title), stats=stats, payload=payload, labels=labels,
        renderer=RENDERER_JS,
    )


def write_viewer(plan: Barndominium, path: str, scene: Scene | None = None) -> str:
    """Write the single-file viewer HTML for ``plan`` to ``path``. Returns ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(viewer_html(plan, scene))
    return path


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# The inline WebGL renderer, shared by the single-file viewer and the playground.
# `mountScene(canvas, labels, togglesEl)` wires a <canvas> for orbit/pan/zoom and
# returns a controller; call `controller.setScene(json)` to load (or swap) the
# geometry `scene_json()` builds — the playground re-calls it on every recompile.
# Kept as a plain string (single braces): both embedders inject it as a value, so
# it is never run through str.format. Returns null when WebGL is unavailable.
RENDERER_JS = r"""
function mountScene(canvas, labels, togglesEl) {
  // Phase 7 replaces the planar ground-shadow pass with a real depth shadow map, so
  // no stencil buffer is needed; a plain antialiased context is enough.
  const gl = canvas.getContext('webgl', {antialias: true});
  if (!gl) {
    const msg = document.createElement('p');
    msg.style.cssText = 'padding:2em;font:16px sans-serif;color:#7a8494';
    msg.textContent = 'This browser could not create a WebGL context, '
      + 'so the 3D view is unavailable.';
    if (canvas.parentNode) canvas.parentNode.replaceChild(msg, canvas);
    return null;
  }
  labels = labels || {};

  // --- minimal mat4 ---------------------------------------------------------
  function mul(a, b) { const o = new Array(16);
    for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) { let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + c] * b[r * 4 + k]; o[r * 4 + c] = s; }
    return o; }
  function perspective(fovy, aspect, n, f) { const t = 1 / Math.tan(fovy / 2);
    return [t / aspect, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) / (n - f), -1,
      0, 0, (2 * f * n) / (n - f), 0]; }
  // A column-major orthographic projection (the sun\'s shadow camera is a parallel
  // projection). Maps [l,r]x[b,t]x[n,f] to the unit clip cube.
  function ortho(l, r, b, t, n, f) {
    return [2 / (r - l), 0, 0, 0, 0, 2 / (t - b), 0, 0, 0, 0, -2 / (f - n), 0,
      -(r + l) / (r - l), -(t + b) / (t - b), -(f + n) / (f - n), 1]; }
  function lookAt(e, c, up) {
    const z = norm(sub(e, c)), x = norm(cross(up, z)), y = cross(z, x);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
      -dot(x, e), -dot(y, e), -dot(z, e), 1]; }
  function sub(a, b) { return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function norm(a) { const l = Math.hypot(a[0], a[1], a[2]) || 1;
    return [a[0] / l, a[1] / l, a[2] / l]; }
  const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  // A rigid model matrix for a door leaf, built in the glTF frame (x, up=+y,
  // -y_plan). `rotY` rotates `ang` radians about the vertical axis through plan
  // pivot (px, py) — the hinge — mapped to (px, up, -py). `translate` shifts by a
  // glTF vector. Both are column-major so they feed uniformMatrix4fv directly and
  // compose as uMVP * uModel * pos in the shader.
  function rotY(px, py, ang) {
    const c = Math.cos(ang), s = Math.sin(ang), ax = px, az = -py;
    // Rotation about +y, then re-anchor so the pivot stays put.
    return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0,
      ax - (c * ax + s * az), 0, az - (-s * ax + c * az), 1];
  }
  function translate(dx, dy, dz) {
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, dx, dy, dz, 1];
  }
  // A dimension for a room-name toast: one decimal only when the value isn't a
  // whole number, so "14 x 16 ft" stays clean but "14.5" keeps its half. ASCII
  // only ("x", "sq ft") per the viewer's plain-text rule.
  function fmtFt(v) {
    const r = Math.round(v);
    return Math.abs(v - r) < 0.05 ? String(r) : v.toFixed(1);
  }

  // --- shader ---------------------------------------------------------------
  // World-space position and normal go to the fragment stage; the base colour is
  // modulated there by a triplanar-mapped procedural texture (chosen per dominant
  // world-normal axis) and lit with a diffuse + sky-fill term plus a
  // roughness/metallic-driven specular, so a metal roof reads as metal.
  // uModel is an optional per-node rigid transform (default identity); a door
  // leaf sets it each frame to swing/slide/lift, everything else draws at rest.
  // Position goes through it before the shared MVP; the normal is rotated by the
  // upper 3x3 (rigid rotations only, so no inverse-transpose is needed).
  const vs = 'attribute vec3 aPos; attribute vec3 aNorm;'
    + ' uniform mat4 uMVP; uniform mat4 uModel;'
    // uLightVP is the sun\'s ortho view-projection; vLightPos carries the world
    // point into the shadow map\'s clip space so the fragment can compare depths.
    + ' uniform mat4 uLightVP;'
    + ' varying vec3 vN; varying vec3 vWorld; varying vec4 vLightPos;'
    + ' void main(){ vec4 wp=uModel*vec4(aPos,1.0); vWorld=wp.xyz;'
    + '   vN=mat3(uModel)*aNorm; vLightPos=uLightVP*wp; gl_Position=uMVP*wp; }';
  const fs = 'precision mediump float;'
    + ' varying vec3 vN; varying vec3 vWorld;'
    + ' uniform vec3 uColor; uniform vec3 uEye;'
    + ' uniform float uRough; uniform float uMetal;'
    + ' uniform float uPatScale; uniform float uHasTex; uniform sampler2D uTex;'
    + ' uniform float uFill;'
    // uLightDir is the sun direction (world, surface->sun) the JS derives from the
    // time/season/latitude sun model; uSunWarmth (0..1) rises as the sun sinks, so
    // low sun tints the direct term warm and cools/dims the ambient. uClipY is a
    // section-cut plane in world-Y feet: fragments above it are discarded for a
    // dollhouse cutaway (default a huge value = no clip, so orbit's normal render
    // and walk mode both stay costless).
    + ' uniform vec3 uLightDir; uniform float uSunWarmth; uniform float uClipY;'
    // Distance fog: fragments blend toward uFogColor (the sky horizon colour) as
    // the eye-distance climbs from uFogNear to uFogFar, so the ground plane's far
    // edge dissolves into the sky instead of showing a hard rim. uFogNear >= uFogFar
    // (the JS default when fog is off) is a no-op.
    + ' uniform vec3 uFogColor; uniform float uFogNear; uniform float uFogFar;'
    // uGlassAlpha is 0 in the opaque pass (fragments stay fully opaque) and ~0.35 in
    // the glass pass, where a slight view-angle (fresnel-ish) boost makes grazing
    // panes read a touch more solid. A constant is acceptable for a schematic.
    + ' uniform float uGlassAlpha;'
    // Shadow map: uShadowTex is the sun\'s depth render; uShadowOn gates the whole
    // feature (0 = shadows disabled, e.g. FBO setup failed); uShadowTexel is 1/size
    // for the 3x3 PCF tap spacing; uShadowPack is 1 when depth is packed into RGBA8
    // (no WEBGL_depth_texture), 0 when it is a real depth texture sampled in .r.
    + ' uniform sampler2D uShadowTex; uniform float uShadowOn;'
    + ' uniform float uShadowTexel; uniform float uShadowPack;'
    // Unpack a depth in [0,1] from an RGBA8 texel (classic 4-byte pack); when using
    // a real depth texture the value already lives in .r, so uShadowPack picks.
    + ' float unpackDepth(vec4 c){'
    + '   return dot(c, vec4(1.0, 1.0/255.0, 1.0/65025.0, 1.0/16581375.0)); }'
    // 3x3 PCF: fraction of the neighbourhood in shadow. `ndc` is the fragment in the
    // light\'s clip space; `bias` is subtracted from the stored depth to kill acne.
    + ' float shadowFactor(vec4 lp, float bias){'
    + '   if(uShadowOn < 0.5) return 1.0;'
    + '   vec3 ndc = lp.xyz / lp.w;'
    + '   ndc = ndc * 0.5 + 0.5;'
    // Outside the light frustum: treat as lit (no shadow beyond the fitted bounds).
    + '   if(ndc.x < 0.0 || ndc.x > 1.0 || ndc.y < 0.0 || ndc.y > 1.0 || ndc.z > 1.0)'
    + '     return 1.0;'
    + '   float cur = ndc.z - bias; float lit = 0.0;'
    + '   for(int i=-1;i<=1;i++){ for(int j=-1;j<=1;j++){'
    + '     vec2 off = vec2(float(i), float(j)) * uShadowTexel;'
    + '     vec4 s = texture2D(uShadowTex, ndc.xy + off);'
    + '     float d = (uShadowPack > 0.5) ? unpackDepth(s) : s.r;'
    + '     lit += (cur <= d) ? 1.0 : 0.0; } }'
    + '   return lit / 9.0; }'
    + ' varying vec4 vLightPos;'
    + ' void main(){'
    + '   if(vWorld.y > uClipY) discard;'
    + '   vec3 n=normalize(vN); vec3 an=abs(n); vec2 uv;'
    // Project world coords onto the plane facing the dominant axis. Vertical
    // surfaces (walls, roof faces) keep world-up as V so ribs/courses read upright.
    + '   if(an.x>=an.y&&an.x>=an.z) uv=vec2(vWorld.z,vWorld.y);'
    + '   else if(an.z>=an.x&&an.z>=an.y) uv=vec2(vWorld.x,vWorld.y);'
    + '   else uv=vec2(vWorld.x,vWorld.z);'
    + '   float detail=1.0;'
    + '   if(uHasTex>0.5) detail=texture2D(uTex, uv/uPatScale).r;'
    + '   vec3 albedo=uColor*(0.4+0.6*detail);'
    + '   vec3 L=uLightDir;'
    + '   vec3 V=normalize(uEye-vWorld); vec3 H=normalize(L+V);'
    + '   float diff=max(dot(n,L),0.0);'
    // Warm the direct term toward a low-sun tint and dim+cool the sky ambient as
    // warmth rises (dusk/dawn). At uSunWarmth=0 (high sun) this is a no-op.
    + '   vec3 sunCol=mix(vec3(1.0),vec3(1.0,0.85,0.7),uSunWarmth);'
    + '   float amb=(0.28+0.22*(0.5+0.5*n.y))*(1.0-0.22*uSunWarmth);'
    + '   vec3 ambCol=mix(vec3(1.0),vec3(0.82,0.86,1.0),uSunWarmth*0.6);'
    // Shadow the DIRECT term only (this diffuse + the specular below): a
    // normal-offset bias that grows at grazing angles kills acne on faces
    // near-parallel to the sun. Ambient/sky/fill stay untouched, so a shadowed
    // interior floor still reads (the sun THROUGH a window brightens the patch it
    // reaches; the surrounding floor just loses the sun\'s direct contribution).
    + '   float sbias=mix(0.0009, 0.004, 1.0 - max(dot(n,L),0.0));'
    + '   float shadow=shadowFactor(vLightPos, sbias);'
    // Metals carry little diffuse; fade it out as metallic rises.
    + '   vec3 col=albedo*(amb*ambCol+diff*0.72*sunCol*shadow)*(1.0-0.65*uMetal);'
    // Eye-attached fill (walk mode only; uFill=0 in orbit is a true no-op): lights
    // surfaces facing the camera so first-person interiors don't read flat and
    // dim. A gentle distance falloff keeps far walls from glowing.
    + '   if(uFill>0.0){ float fd=length(uEye-vWorld);'
    + '     float fall=1.0/(1.0+0.04*fd);'
    + '     col+=albedo*uFill*max(dot(n,V),0.0)*fall; }'
    + '   float sh=mix(10.0,90.0,1.0-uRough);'
    + '   float spec=pow(max(dot(n,H),0.0),sh);'
    + '   vec3 specCol=mix(vec3(0.05),uColor,uMetal);'
    // The sun\'s specular highlight is a direct term too, so it darkens in shadow.
    + '   col+=specCol*spec*(0.25+0.75*uMetal)*shadow;'
    + '   vec3 outc=pow(min(col,vec3(1.4)), vec3(1.0/2.2));'
    // Fold in distance fog after tone-mapping so the mix meets the (already
    // gamma-space) sky gradient the sky pass drew. No-op when uFogFar<=uFogNear.
    + '   float fog=clamp((length(uEye-vWorld)-uFogNear)/max(uFogFar-uFogNear,1e-3),0.0,1.0);'
    + '   outc=mix(outc, uFogColor, fog);'
    // Opaque pass: uGlassAlpha=0 -> alpha 1.0. Glass pass: uGlassAlpha=0.35 with a
    // fresnel-ish boost toward grazing angles (up to ~+0.3), so windows are
    // see-through head-on and read a little more solid at the edges.
    + '   float alpha=1.0;'
    + '   if(uGlassAlpha>0.0){ float fres=pow(1.0-abs(dot(n,V)),3.0);'
    + '     alpha=clamp(uGlassAlpha+0.3*fres, 0.0, 1.0); }'
    + '   gl_FragColor=vec4(outc, alpha);'
    + ' }';
  function compileShader(type, src) { const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(s);
    return s; }
  const prog = gl.createProgram();
  gl.attachShader(prog, compileShader(gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, compileShader(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog); gl.useProgram(prog);
  const aPos = gl.getAttribLocation(prog, 'aPos');
  const aNorm = gl.getAttribLocation(prog, 'aNorm');
  const uMVP = gl.getUniformLocation(prog, 'uMVP');
  const uModel = gl.getUniformLocation(prog, 'uModel');
  const uColor = gl.getUniformLocation(prog, 'uColor');
  const uEye = gl.getUniformLocation(prog, 'uEye');
  const uRough = gl.getUniformLocation(prog, 'uRough');
  const uMetal = gl.getUniformLocation(prog, 'uMetal');
  const uPatScale = gl.getUniformLocation(prog, 'uPatScale');
  const uHasTex = gl.getUniformLocation(prog, 'uHasTex');
  const uTex = gl.getUniformLocation(prog, 'uTex');
  const uFill = gl.getUniformLocation(prog, 'uFill');
  const uLightDir = gl.getUniformLocation(prog, 'uLightDir');
  const uSunWarmth = gl.getUniformLocation(prog, 'uSunWarmth');
  const uClipY = gl.getUniformLocation(prog, 'uClipY');
  const uFogColor = gl.getUniformLocation(prog, 'uFogColor');
  const uFogNear = gl.getUniformLocation(prog, 'uFogNear');
  const uFogFar = gl.getUniformLocation(prog, 'uFogFar');
  const uGlassAlpha = gl.getUniformLocation(prog, 'uGlassAlpha');
  const uLightVP = gl.getUniformLocation(prog, 'uLightVP');
  const uShadowTex = gl.getUniformLocation(prog, 'uShadowTex');
  const uShadowOn = gl.getUniformLocation(prog, 'uShadowOn');
  const uShadowTexel = gl.getUniformLocation(prog, 'uShadowTexel');
  const uShadowPack = gl.getUniformLocation(prog, 'uShadowPack');
  gl.getExtension('OES_element_index_uint');
  gl.enable(gl.DEPTH_TEST);

  // --- sky + ground-shadow programs (renderer-side atmosphere) --------------
  // Two tiny extra programs keep the main single-pass shader clean: a fullscreen
  // sky gradient drawn behind everything with depth writes off, and a flat dark
  // pass that projects the shadow-casting layers onto the ground plane. Both are
  // renderer-only — no scene geometry, nothing in the glTF/IFC exports.
  //
  // SKY: a full-screen triangle in clip space; the fragment lerps between a zenith
  // and a horizon colour by screen height. Colours come from the sun model (day is
  // a soft desaturated blue; as the sun sinks uSunWarmth warms the horizon amber),
  // set each draw so sky, fog and lighting agree.
  const skyVs = 'attribute vec2 aPos; varying vec2 vUv;'
    + ' void main(){ vUv=aPos*0.5+0.5; gl_Position=vec4(aPos,0.999,1.0); }';
  const skyFs = 'precision mediump float; varying vec2 vUv;'
    + ' uniform vec3 uZenith; uniform vec3 uHorizon;'
    + ' void main(){ float t=clamp(vUv.y,0.0,1.0);'
    // ease the blend toward the horizon so the band sits low and natural.
    + '   float m=pow(t,0.8);'
    + '   gl_FragColor=vec4(mix(uHorizon,uZenith,m),1.0); }';
  const skyProg = gl.createProgram();
  gl.attachShader(skyProg, compileShader(gl.VERTEX_SHADER, skyVs));
  gl.attachShader(skyProg, compileShader(gl.FRAGMENT_SHADER, skyFs));
  gl.linkProgram(skyProg);
  const skyAPos = gl.getAttribLocation(skyProg, 'aPos');
  const uZenith = gl.getUniformLocation(skyProg, 'uZenith');
  const uHorizon = gl.getUniformLocation(skyProg, 'uHorizon');
  const skyBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, skyBuf);
  // A single oversized triangle covering the viewport (cheaper than a quad).
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]),
    gl.STATIC_DRAW);

  // SHADOW MAP (Phase 7, replaces the old planar ground-shadow pass): the scene is
  // rendered from the sun into an offscreen depth buffer each frame, and the main
  // shader compares each fragment\'s light-space depth to it, so sun falls THROUGH a
  // window opening onto the interior floor (the most persuasive interior effect).
  // The depth program writes only depth — packed into an RGBA8 colour when there is
  // no WEBGL_depth_texture (the classic 4-byte pack), or a real depth texture when
  // there is. Per-node uModel is honoured so an open door lets light through
  // (animated doors handled for free). Everything but glass + the ground casts.
  const depthExt = gl.getExtension('WEBGL_depth_texture')
    || gl.getExtension('WEBKIT_WEBGL_depth_texture')
    || gl.getExtension('MOZ_WEBGL_depth_texture');
  const shadowPack = depthExt ? 0 : 1;    // 1 = pack depth into RGBA8 colour
  const smVs = 'attribute vec3 aPos; uniform mat4 uLightVP; uniform mat4 uModel;'
    + ' varying vec4 vPos;'
    + ' void main(){ vPos=uLightVP*uModel*vec4(aPos,1.0); gl_Position=vPos; }';
  // When packing, encode gl_Position.z/w (0..1) into RGBA8; a real depth texture
  // fills its own depth attachment, so the colour output is unused (write white).
  const smFs = 'precision highp float; varying vec4 vPos; uniform float uPack;'
    + ' vec4 packDepth(float d){'
    + '   vec4 e = vec4(1.0, 255.0, 65025.0, 16581375.0) * d;'
    + '   e = fract(e);'
    + '   e -= e.yzww * vec4(1.0/255.0, 1.0/255.0, 1.0/255.0, 0.0);'
    + '   return e; }'
    + ' void main(){'
    + '   if(uPack > 0.5){ float d = vPos.z / vPos.w * 0.5 + 0.5;'
    + '     gl_FragColor = packDepth(clamp(d, 0.0, 1.0)); }'
    + '   else gl_FragColor = vec4(1.0); }';
  const smProg = gl.createProgram();
  gl.attachShader(smProg, compileShader(gl.VERTEX_SHADER, smVs));
  gl.attachShader(smProg, compileShader(gl.FRAGMENT_SHADER, smFs));
  gl.linkProgram(smProg);
  const smAPos = gl.getAttribLocation(smProg, 'aPos');
  const smLightVP = gl.getUniformLocation(smProg, 'uLightVP');
  const smModel = gl.getUniformLocation(smProg, 'uModel');
  const smPack = gl.getUniformLocation(smProg, 'uPack');

  // Which layers CAST a sun shadow: everything opaque that reads as building mass.
  // Openings cast (a door leaf / mullion / trim throws a real shadow; the glass
  // node inside them is filtered out by isGlass so a window casts light, not dark).
  // The ground plane is a receiver only, never a caster, so it is not a scene node
  // here and simply isn\'t drawn into the map.
  const SHADOW_LAYERS = { walls: 1, roof: 1, openings: 1, trim: 1, ceilings: 1,
    frame: 1, porches: 1, floors: 1, stairs: 1, fixtures: 1 };

  // --- shadow-map framebuffer (guarded; disables shadows on any failure) -----
  // A 2048^2 depth target (falls back to 1024 if the allocation fails), with either
  // a real depth texture (WEBGL_depth_texture) or a packed-RGBA8 colour target. Any
  // setup failure (no extensions, stubbed GL in the headless harness, incomplete
  // FBO) leaves `shadowOK` false: shadows are disabled, the main shader\'s uShadowOn
  // is 0, and rendering proceeds unshadowed — never broken.
  let shadowOK = false, shadowFBO = null, shadowTex = null, shadowDepthRB = null;
  let shadowSize = 2048;
  function buildShadowTarget(size) {
    const fb = gl.createFramebuffer();
    const tex = gl.createTexture();
    let depthRB = null;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    if (depthExt) {
      // Real depth texture: colour is unused, depth lives in the texture.
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.DEPTH_COMPONENT, size, size, 0,
        gl.DEPTH_COMPONENT, gl.UNSIGNED_INT, null);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, size, size, 0,
        gl.RGBA, gl.UNSIGNED_BYTE, null);
    }
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    if (depthExt) {
      // Colour attachment needs a (dummy) renderbuffer for a complete FBO.
      const colorRB = gl.createRenderbuffer();
      gl.bindRenderbuffer(gl.RENDERBUFFER, colorRB);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.RGBA4, size, size);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0,
        gl.RENDERBUFFER, colorRB);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT,
        gl.TEXTURE_2D, tex, 0);
    } else {
      depthRB = gl.createRenderbuffer();
      gl.bindRenderbuffer(gl.RENDERBUFFER, depthRB);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT16, size, size);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0,
        gl.TEXTURE_2D, tex, 0);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT,
        gl.RENDERBUFFER, depthRB);
    }
    const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.bindTexture(gl.TEXTURE_2D, null);
    return ok ? { fb, tex, depthRB } : null;
  }
  function initShadow() {
    // Some GL contexts / harness stubs throw on FBO calls; wrap the whole thing so a
    // failure just leaves shadows off. Try 2048, then 1024.
    try {
      if (!gl.createFramebuffer || !gl.checkFramebufferStatus) return;
      let t = buildShadowTarget(2048);
      if (!t) { t = buildShadowTarget(1024); shadowSize = t ? 1024 : shadowSize; }
      if (!t) return;
      shadowFBO = t.fb; shadowTex = t.tex; shadowDepthRB = t.depthRB;
      shadowOK = true;
    } catch (e) { shadowOK = false; }
  }
  initShadow();
  // The sun\'s ortho view-projection, rebuilt each draw from lightDir + scene bounds
  // by buildLightMatrix(); identity until the first scene sizes it.
  let lightVP = IDENTITY.slice();

  // --- procedural pattern textures ------------------------------------------
  // One detail map per pattern, drawn on an offscreen canvas (no network) and
  // uploaded as a repeating WebGL texture. Each map is a greyscale relief that
  // darkens grooves/seams; tiled at the material's world scale (feet) by the
  // shader. 128px is power-of-two, so REPEAT + mipmaps are valid.
  const texCache = {};
  function drawPattern(ctx, S, pattern) {
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    if (pattern === 'rib') {  // corrugated / standing-seam: one vertical rib period
      const img = ctx.getImageData(0, 0, S, S);
      for (let x = 0; x < S; x++) {
        const v = 0.6 + 0.4 * (0.5 - 0.5 * Math.cos(2 * Math.PI * x / S));
        const g = Math.round(255 * v);
        for (let y = 0; y < S; y++) { const i = (y * S + x) * 4;
          img.data[i] = img.data[i + 1] = img.data[i + 2] = g; img.data[i + 3] = 255; }
      }
      ctx.putImageData(img, 0, 0);
    } else if (pattern === 'batten') {  // wide board + a raised vertical batten
      ctx.fillStyle = '#efefef'; ctx.fillRect(0, 0, S, S);
      const bw = Math.max(3, Math.round(S * 0.13));
      ctx.fillStyle = '#c4c4c4'; ctx.fillRect(0, 0, bw, S);
      ctx.fillStyle = '#ffffff'; ctx.fillRect(2, 0, bw - 4, S);
      ctx.fillStyle = '#b0b0b0'; ctx.fillRect(bw, 0, 1, S);
    } else if (pattern === 'shingle') {  // horizontal courses, staggered joints
      const rows = 2, rh = S / rows;
      for (let r = 0; r < rows; r++) {
        const y0 = r * rh, off = (r % 2) * (S * 0.25);
        ctx.fillStyle = '#9a9a9a';
        ctx.fillRect(0, y0 + rh - Math.max(2, rh * 0.12), S, Math.max(2, rh * 0.12));
        ctx.fillStyle = '#bcbcbc';
        for (let k = 0; k < 4; k++) { const x = (off + k * S / 4) % S;
          ctx.fillRect(x, y0, 1, rh - 2); }
      }
    } else if (pattern === 'plank') {  // long planks: seam top/bottom + grain
      ctx.fillStyle = '#a8a8a8'; ctx.fillRect(0, 0, S, 1); ctx.fillRect(0, S - 1, S, 1);
      for (let i = 0; i < 36; i++) { const y = Math.random() * S,
        g = 205 + Math.round(Math.random() * 40);
        ctx.fillStyle = 'rgba(' + g + ',' + g + ',' + g + ',0.16)'; ctx.fillRect(0, y, S, 1); }
      ctx.fillStyle = '#b2b2b2'; ctx.fillRect(Math.round(S * 0.5), 0, 1, S);
    } else if (pattern === 'tile') {  // one tile with grout on two edges
      const g = Math.max(2, Math.round(S * 0.06));
      ctx.fillStyle = '#9a9a9a'; ctx.fillRect(0, 0, S, g); ctx.fillRect(0, 0, g, S);
    } else if (pattern === 'speckle') {  // concrete / carpet fleck
      const img = ctx.getImageData(0, 0, S, S);
      for (let p = 0; p < S * S; p++) { const v = Math.round(255 * (0.84 + 0.16 * Math.random()));
        const i = p * 4; img.data[i] = img.data[i + 1] = img.data[i + 2] = v; img.data[i + 3] = 255; }
      ctx.putImageData(img, 0, 0);
    }
  }
  function patternTexture(pattern) {
    if (!pattern || pattern === 'none') return null;
    if (texCache[pattern] !== undefined) return texCache[pattern];
    const S = 128, cv = document.createElement('canvas'); cv.width = cv.height = S;
    drawPattern(cv.getContext('2d'), S, pattern);
    const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, cv);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.generateMipmap(gl.TEXTURE_2D);
    texCache[pattern] = tex;
    return tex;
  }

  // Build (or rebuild) the ground plane: one big quad at y=0, extending ~6x the
  // scene radius around the scene centre so its rim is always past the fog's far
  // edge. Positions + an up normal in the glTF frame; textured with the shared
  // speckle map for a grass fleck. Renderer-only — never part of the scene nodes,
  // so the glTF/IFC exports stay clean architecture.
  function buildGround() {
    const R = Math.max(60, radius * 6);
    const cx = center[0], cz = center[2];       // glTF frame: y is up
    const x0 = cx - R, x1 = cx + R, z0 = cz - R, z1 = cz + R;
    // Two triangles, CCW from above (normal +y).
    const pos = new Float32Array([
      x0, 0, z0, x1, 0, z0, x1, 0, z1,
      x0, 0, z0, x1, 0, z1, x0, 0, z1]);
    const nrm = new Float32Array([
      0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0]);
    if (!groundBuf) groundBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf);
    gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
    if (!groundNorm) groundNorm = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, groundNorm);
    gl.bufferData(gl.ARRAY_BUFFER, nrm, gl.STATIC_DRAW);
    groundCount = 6;
    if (!groundTex) groundTex = patternTexture('speckle');
    // Fog band: begins just past the scene, full by the ground's rim, so the plane
    // edge dissolves into the horizon colour rather than cutting a hard line.
    fogNear = radius * 2.2;
    fogFar = R * 0.95;
  }

  // --- state (survives setScene so the view/toggles persist on recompile) ---
  let nodes = [];
  let center = [0, 0, 0], radius = 1;
  // World-frame (glTF: y up) scene AABB, kept so the shadow camera can fit an ortho
  // frustum around the whole model along the sun direction each frame.
  let sceneMin = [0, 0, 0], sceneMax = [1, 1, 1];
  let yaw = -0.7, pitch = 0.6, dist = 2.6;
  const target = [0, 0, 0];
  const hidden = {};
  let hasScene = false, framed = false;

  // --- first-person walk state ---------------------------------------------
  // An *additional* camera mode layered over the orbit camera: WASD + mouse look
  // at a 5.5 ft eye height, sliding along the wall segments and ramping up stairs.
  // All movement/collision math is in plan coords (x east, y north); the draw
  // step maps to the glTF frame (x, z, -y). Entering saves the orbit camera so
  // exiting restores it exactly. `walk` is the block scene_json() ships.
  let walk = null;                 // the walk-support data (segments/floors/stairs/spawn)
  let walkSegs = [];               // [x0,y0,x1,y1,elevation] collision segments (flat, fast)
  let walkDoors = [];              // per door: {id, seg:[x0,y0,x1,y1,elev], mid:[x,y], node}
  let walkFixtures = [];           // [x,y,w,l,elevation] fixture footprint rects (flat, fast)
  let walkRooms = [];              // per room: {x,y,w,l,elevation,level,name,area,area2} for map + toast
  let minimapOn = true;            // mini-map HUD shown (M toggles; session-only, no persistence)
  let curRoomIdx = -1;             // index into walkRooms of the room the player is in (-1 = none)
  let walkFF = 10;                 // floor-to-floor (ft) — filters walls to the current storey
  let walkEye = 5.5, walkSpeed = 4;
  let walkEyeTarget = 5.5;         // eased eye-height target (preset cycling)
  let walking = false;
  const wpos = [0, 0];             // player plan position (x, y)
  const wvel = [0, 0];             // player plan velocity (ft/s) — smoothed, Phase 6
  let wElev = 0;                   // current floor elevation (eased for smooth steps)
  let wYaw = 0, wPitch = 0;        // look angles (radians)
  const keys = {};                 // held movement keys {f,b,l,r,run}
  let walkRAF = null, walkLast = 0;
  let usePointerLock = false, walkDrag = false, wpx = 0, wpy = 0;
  let savedCam = null;             // orbit camera snapshot to restore on exit
  // Linked selection: the host names a room (its DSL id) and its floor draws in
  // the selection blue; a click on a room floor reports the id back to the host.
  let highlightId = null, onSelectCb = null;
  const HIGHLIGHT_MIX = [0.30, 0.52, 0.82];
  function highlightColor(c) {
    return new Float32Array([c[0] * 0.4 + HIGHLIGHT_MIX[0] * 0.6,
      c[1] * 0.4 + HIGHLIGHT_MIX[1] * 0.6, c[2] * 0.4 + HIGHLIGHT_MIX[2] * 0.6]);
  }
  let furniture = true;            // collide against fixtures (toggle; default ON)
  const WALK_R = 0.75;             // player collision radius (ft)
  const WALK_FILL = 0.35;          // eye-attached fill strength in walk mode
  // Eye-height presets cycled by the pill / the C key: standing (the shipped
  // walk.eyeHeight), a seated wheelchair/ADA sightline, and a child's eye level.
  // The standing default is set from walk.eyeHeight on setScene, so viewer.py
  // stays the single authority for the shipped height.
  const EYE_PRESETS = [
    { label: 'Standing', ft: 5.5 },
    { label: 'Seated', ft: 4.0 },
    { label: 'Child', ft: 3.5 },
  ];
  let eyeIdx = 0;                  // index into EYE_PRESETS (0 = standing default)
  // Coarse-pointer / touch: show the virtual thumbstick and skip pointer lock.
  // Latched true on the first touch too, so a hybrid laptop that gets a real
  // touch reveals the stick. matchMedia may be absent in a headless host.
  let coarse = false;
  try { coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches); }
  catch (e) { coarse = false; }

  // --- sun study + section cut ---------------------------------------------
  // The sun model turns (time-of-day, season) into a world-space light direction
  // + a warmth term, honoring the plan's compass orientation and a fixed default
  // latitude (scene.sun). Defaults are equinox 10:30 — the (season, hour) whose
  // world direction is closest to the OLD hard-coded L=normalize(0.4,0.9,0.5), so
  // an out-of-the-box render barely changes from before this control existed.
  const NO_CLIP = 1e9;                 // uClipY sentinel: no section cut
  const SUN_MIN_ALT = 5 * Math.PI / 180;   // never let the sun fully set (stay lit)
  const SEASONS = [                        // solar declination per season (deg)
    { key: 'winter', label: 'Winter', decl: -23.44 },
    { key: 'equinox', label: 'Equinox', decl: 0.0 },
    { key: 'summer', label: 'Summer', decl: 23.44 },
  ];
  let sunLat = 35.0;                    // site latitude (deg) from scene.sun
  let sunOrient = 0.0;                  // plan orientation (deg CW from true north)
  let sunHours = 10.5;                  // time of day (6..18) — default 10:30
  let sunSeasonIdx = 1;                 // index into SEASONS — default equinox
  let lightDir = [0.383, 0.757, 0.530];// world dir to the sun (seeded to the default)
  let sunWarmth = 0.0;                  // 0 high sun .. 1 low sun (warm tint)
  let sceneMaxY = 10;                   // top of the scene bounds (ft) for the section slider
  let sceneMinY = 0;                    // bottom of the scene bounds (ft)
  let clipY = NO_CLIP;                  // section-cut plane (world Y ft); NO_CLIP = off
  let levelIdx = null;                  // isolated level index, or null = all levels
  let floorElevs = [];                  // sorted per-level elevations (ft) from walk.floors

  // --- sky / ground / fog atmosphere (renderer-only) ------------------------
  // Colours are recomputed in computeSun so the sky gradient, the distance fog and
  // the ground all track the sun. Kept desaturated and architectural, not a game
  // sky: a soft blue that warms at the horizon as the sun sinks. The ground is a
  // muted grass green (its own speckle texture, built lazily) laid out ~6x the
  // scene radius so its edge is always lost in fog. Shadows are a translucent dark.
  let skyZenith = [0.42, 0.56, 0.72];   // upper sky colour
  let skyHorizon = [0.78, 0.84, 0.90];  // horizon band colour (also the fog colour)
  let groundTex = null;                  // lazily-built grass speckle texture
  let groundBuf = null, groundNorm = null, groundCount = 0;  // ground plane GL data
  const GROUND_COLOR = [0.40, 0.46, 0.34];  // muted grass green (linear-ish)
  let fogNear = 1e9, fogFar = 1e9;      // eye-distance fog band (ft); off until sized
  let showGround = true;                 // ground + sky + shadows on (session flag)

  // Derive the sky/horizon/fog colours from the current sun altitude. Called from
  // computeSun (which already has `alt`/`sunWarmth`). Desaturated on purpose.
  function computeAtmosphere() {
    const w = sunWarmth;                 // 0 high sun .. 1 low sun
    // Zenith: a soft blue that deepens and cools very slightly toward dusk.
    skyZenith = [0.40 - 0.06 * w, 0.55 - 0.10 * w, 0.74 - 0.10 * w];
    // Horizon: pale by day, warming to amber as the sun sinks (mix toward a warm
    // tone by warmth). This is also the fog colour so the two always agree.
    const day = [0.80, 0.85, 0.90], dusk = [0.92, 0.72, 0.52];
    skyHorizon = [day[0] + (dusk[0] - day[0]) * w,
      day[1] + (dusk[1] - day[1]) * w, day[2] + (dusk[2] - day[2]) * w];
  }

  // Compute lightDir + sunWarmth from the current (hours, season, latitude,
  // orientation). Standard simplified solar-position model: declination for the
  // season, hour angle 15 deg/hr from solar noon, then altitude/azimuth; the
  // azimuth is a compass bearing (0=N,90=E,180=S,270=W) which we rotate by the
  // plan orientation into the plan frame, then map plan (x east, y north, z up) to
  // the glTF frame (x, z_up, -y). Altitude is clamped to SUN_MIN_ALT so the model
  // never goes fully dark. Result: at solar noon on a 0-orientation plan the sun is
  // due plan-south -> world dir has a +Z component (world +Z = plan south).
  function computeSun() {
    const D2R = Math.PI / 180;
    const lat = sunLat * D2R, decl = SEASONS[sunSeasonIdx].decl * D2R;
    const H = (sunHours - 12) * 15 * D2R;            // hour angle from solar noon
    let sinAlt = Math.sin(lat) * Math.sin(decl)
      + Math.cos(lat) * Math.cos(decl) * Math.cos(H);
    sinAlt = Math.max(-1, Math.min(1, sinAlt));
    let alt = Math.asin(sinAlt);
    // Azimuth from north, clockwise. acos gives 0..pi (morning east side); the
    // afternoon (H>0) mirrors to the west side.
    const cosAz = (Math.sin(decl) - Math.sin(alt) * Math.sin(lat))
      / ((Math.cos(alt) * Math.cos(lat)) || 1e-6);
    let az = Math.acos(Math.max(-1, Math.min(1, cosAz)));
    if (H > 0) az = 2 * Math.PI - az;
    if (alt < SUN_MIN_ALT) alt = SUN_MIN_ALT;        // keep a little light at dusk
    // Sun bearing in the plan frame: compass azimuth minus the orientation that
    // plan-north points. Bearing 0 -> +y (north), 90 -> +x (east).
    const bearing = az - sunOrient * D2R;
    const ca = Math.cos(alt), sa = Math.sin(alt);
    const px = Math.sin(bearing) * ca;               // plan east
    const py = Math.cos(bearing) * ca;               // plan north
    lightDir = [px, sa, -py];                        // plan (x,y,z) -> gltf (x,z,-y)
    // Warmth rises as the sun sinks: 0 above ~40 deg, ramping to 1 near the horizon.
    const altDeg = alt / D2R;
    sunWarmth = Math.max(0, Math.min(1, (40 - altDeg) / 35));
    computeAtmosphere();   // sky/horizon/fog colours follow the sun
  }
  computeSun();
  // A "10:30" style clock label for the sun time-of-day slider (ASCII only).
  function clockLabel(h) {
    const hh = Math.floor(h), mm = Math.round((h - hh) * 60);
    return hh + ':' + (mm < 10 ? '0' + mm : String(mm));
  }

  // Set the sun from (hours 6..18, season key/index) and redraw — the test hook.
  function setSun(hours, season) {
    if (hours != null) sunHours = Math.max(6, Math.min(18, hours));
    if (season != null) {
      const i = (typeof season === 'number')
        ? season : SEASONS.findIndex(s => s.key === season || s.label === season);
      if (i >= 0) sunSeasonIdx = i;
    }
    computeSun();
    if (sunTime) sunTime.value = String(sunHours);
    if (sunTimeLbl) sunTimeLbl.textContent = clockLabel(sunHours);
    syncSeasonBtns();
    draw();
  }

  // Set the section-cut plane directly (world-Y ft); null / >= the scene top
  // disables clipping. Clears any active level isolation (a manual drag wins).
  // The test hook for the slider.
  function setSection(y) {
    levelIdx = null;
    if (y == null || y >= sceneMaxY + 1 - 1e-6) clipY = NO_CLIP;
    else clipY = y;
    syncSectionUI();
    draw();
  }

  // Isolate a level (index into the sorted floor elevations) or null for all.
  // Pure clip-plane isolation: clipY drops to just under the next level's floor
  // (top level: its floor + a storey height), nothing else is filtered. The test
  // hook for the level pills.
  function setLevel(idx) {
    if (idx == null || !floorElevs.length) { levelIdx = null; clipY = NO_CLIP; }
    else {
      idx = Math.max(0, Math.min(floorElevs.length - 1, idx));
      levelIdx = idx;
      const base = floorElevs[idx];
      const next = (idx + 1 < floorElevs.length) ? floorElevs[idx + 1] : null;
      clipY = (next != null) ? next - 0.1 : base + (walkFF || 10);
    }
    syncSectionUI();
    draw();
  }

  function setScene(scene) {
    for (const nd of nodes) {
      gl.deleteBuffer(nd.pb); gl.deleteBuffer(nd.nb); gl.deleteBuffer(nd.ib);
    }
    const bmin = [1e9, 1e9, 1e9], bmax = [-1e9, -1e9, -1e9];
    nodes = (scene.nodes || []).map(n => {
      const pos = new Float32Array(n.positions), nrm = new Float32Array(n.normals);
      for (let i = 0; i < pos.length; i += 3) for (let k = 0; k < 3; k++) {
        bmin[k] = Math.min(bmin[k], pos[i + k]); bmax[k] = Math.max(bmax[k], pos[i + k]); }
      const pb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, pb);
      gl.bufferData(gl.ARRAY_BUFFER, pos, gl.STATIC_DRAW);
      const nb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, nb);
      gl.bufferData(gl.ARRAY_BUFFER, nrm, gl.STATIC_DRAW);
      const ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(n.indices), gl.STATIC_DRAW);
      const pattern = n.pattern || 'none';
      // A leaf carries its `door` record + a runtime {open, target} the walk step
      // eases; static nodes have door=null and always draw at the identity model.
      // `pos` + `idx` are retained on the CPU (the same world-frame arrays the GL
      // buffers hold) so ray picking (measure + click-to-identify) can Moller-
      // Trumbore against the triangles without reading them back from WebGL; `name`
      // + `mat` name the surface for the identify toast.
      // `isGlass` (shipped by scene_json for glazing) routes the node into the LAST,
      // blended, depth-write-off draw pass so windows read see-through, and keeps it
      // out of the shadow-caster set so a window casts LIGHT, not a dark patch.
      return { layer: n.layer, color: n.color, name: n.name || '', mat: n.mat || '',
        rough: n.roughness == null ? 0.8 : n.roughness,
        metal: n.metallic == null ? 0.0 : n.metallic,
        patScale: n.patternScale || 1.0, tex: patternTexture(pattern),
        door: n.door || null, open: 0, target: 0, pinned: false,
        glass: !!n.isGlass,
        pos: pos, idx: n.indices,
        pb, nb, ib, count: n.indices.length };
    });
    if (nodes.length) {
      center = [(bmin[0] + bmax[0]) / 2, (bmin[1] + bmax[1]) / 2, (bmin[2] + bmax[2]) / 2];
      radius = Math.max(1, Math.hypot(bmax[0] - bmin[0], bmax[1] - bmin[1],
        bmax[2] - bmin[2]) / 2);
      // Vertical scene bounds drive the section-cut slider range (just above the
      // ground floor up to above the ridge). World Y is up in the glTF frame.
      sceneMinY = bmin[1]; sceneMaxY = bmax[1];
      // Full world AABB for the shadow camera to fit an ortho frustum around.
      sceneMin = bmin.slice(); sceneMax = bmax.slice();
      buildGround();   // (re)size the ground plane + fog band to the new scene
    }
    if (!framed && nodes.length) {  // frame once, then keep the user's view
      target[0] = center[0]; target[1] = center[1]; target[2] = center[2];
      dist = radius * 2.6; framed = true;
    }
    hasScene = true;
    // Prime the walk data (flatten segments, derive floor-to-floor) so entering
    // walk mode is instant. Exits any in-progress walk since geometry changed.
    if (walking) exitWalk();
    walk = scene.walk || null;
    if (walk) {
      walkSegs = (walk.segments || []).map(s => [s.x0, s.y0, s.x1, s.y1, s.elevation]);
      // Fixture footprints for optional furniture collision: [x, y, w, l, elev].
      walkFixtures = (walk.fixtures || []).map(f => [f.x, f.y, f.w, f.l, f.elevation]);
      // Room rects for the mini-map + name toast. Keep the label and area; `area2`
      // is the rect area used only to break point-in-rect ties (a closet inside a
      // bedroom, both containing the point, wins by smallest area) — kept local so
      // a zero authored `area` never mis-ranks. `label` is the toast text.
      walkRooms = (walk.rooms || []).map(r => ({
        x: r.x, y: r.y, w: r.w, l: r.l, elevation: r.elevation, level: r.level,
        name: r.name, area: r.area, area2: Math.abs(r.w * r.l),
        label: r.name + ' - ' + fmtFt(r.w) + ' x ' + fmtFt(r.l) + ' ft - '
          + fmtFt(r.area) + ' sq ft',
      }));
      curRoomIdx = -1;
      const evs = (walk.floors || []).map(f => f.elevation).sort((a, b) => a - b);
      walkFF = 10;
      for (let i = 1; i < evs.length; i++) { const g = evs[i] - evs[i - 1];
        if (g > 0.5) { walkFF = g; break; } }
      // The shipped standing eye height is the walk.eyeHeight authority; seed the
      // Standing preset from it so the pill and viewer.py never disagree.
      walkEye = walk.eyeHeight || 5.5;
      EYE_PRESETS[0].ft = walkEye;
      eyeIdx = 0; walkEyeTarget = walkEye;
      // Link each walk.doors span to the leaf node(s) that share its id — a
      // double/french door has two leaves, so a door drives every matching node.
      // The span segment collides while the door is < half open; its midpoint is
      // the proximity test point. Deterministic (walk.doors is in model order).
      walkDoors = (walk.doors || []).map(d => ({
        id: d.id, elevation: d.elevation,
        seg: [d.x0, d.y0, d.x1, d.y1, d.elevation],
        mid: [(d.x0 + d.x1) / 2, (d.y0 + d.y1) / 2],
        leaves: nodes.filter(n => n.door && n.door.id === d.id),
      }));
    } else {
      walkDoors = []; walkFixtures = []; walkRooms = []; curRoomIdx = -1;
    }
    // Sun model params: the plan's compass orientation (0 when unset) + a fixed
    // default latitude ship in scene.sun. Re-seed the light so a swap re-lights
    // with the new plan's orientation.
    const sun = scene.sun || {};
    sunOrient = (sun.orientation == null) ? 0.0 : sun.orientation;
    sunLat = (sun.latitude == null) ? 35.0 : sun.latitude;
    computeSun();
    // Floor elevations (sorted, deduped) for level-isolation clip planes; map each
    // plan floor elevation to world Y (plan z_up -> world y, so they are equal).
    const fe = (walk && walk.floors ? walk.floors.map(f => f.elevation) : [])
      .slice().sort((a, b) => a - b);
    floorElevs = fe.filter((v, i) => i === 0 || v - fe[i - 1] > 0.5);
    // Reset section state on a scene swap so a recompile isn't left mid-cut.
    clipY = NO_CLIP; levelIdx = null;
    updateWalkUI();
    buildToggles(scene.layers || []);
    updateSunUI(); updateSectionUI();
    resize(); draw();
  }

  function buildToggles(layers) {
    if (!togglesEl) return;
    togglesEl.textContent = '';
    layers.forEach(ly => {
      const lab = document.createElement('label'), cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = !hidden[ly];
      cb.onchange = () => { hidden[ly] = !cb.checked; draw(); };
      lab.appendChild(cb); lab.appendChild(document.createTextNode(labels[ly] || ly));
      togglesEl.appendChild(lab);
    });
  }

  // --- interaction ----------------------------------------------------------
  let dragging = false, panning = false, px = 0, py = 0;
  let lookId = null;   // pointerId of the canvas look-drag in walk mode (per-pointer)
  // A touch anywhere latches coarse-pointer mode (matchMedia can miss a hybrid
  // device); if we're mid-walk when the first touch lands, reveal the thumbstick.
  function markTouch() {
    if (coarse) return;
    coarse = true;
    if (walking) stickBase.style.display = '';
  }
  canvas.addEventListener('pointerdown', e => {
    if (e.pointerType === 'touch') markTouch();
    if (walking) {  // walk mode: a canvas drag looks around when no pointer lock
      // Per-pointer: the first canvas pointer owns look; a second touch (e.g. the
      // stick thumb, on its own element) is untouched here, so look + move run at
      // once. Pointer lock (fine pointers) uses a document mousemove instead.
      if (!usePointerLock && lookId === null) {
        lookId = e.pointerId; walkDrag = true; wpx = e.clientX; wpy = e.clientY;
        try { canvas.setPointerCapture(e.pointerId); } catch (er) {}
      }
      return;
    }
    dragging = true;
    panning = e.button === 2 || e.shiftKey; px = e.clientX; py = e.clientY;
    canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointerup', e => {
    if (walking) { if (e.pointerId === lookId) { lookId = null; walkDrag = false; } return; }
    dragging = false; });
  canvas.addEventListener('pointercancel', e => {
    if (walking && e.pointerId === lookId) { lookId = null; walkDrag = false; } });
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('pointermove', e => {
    if (walking) {  // drag-look fallback (pointer lock uses a document mousemove)
      if (walkDrag && !usePointerLock && e.pointerId === lookId) {
        applyLook(e.clientX - wpx, e.clientY - wpy); wpx = e.clientX; wpy = e.clientY; draw(); }
      return;
    }
    if (!dragging) return;
    const dx = e.clientX - px, dy = e.clientY - py; px = e.clientX; py = e.clientY;
    if (panning) { const s = dist * 0.0016;
      const cy = Math.cos(yaw), sy = Math.sin(yaw);
      target[0] -= (dx * cy) * s; target[2] -= (dx * sy) * s; target[1] += dy * s;
    } else { yaw -= dx * 0.008; pitch = Math.max(-1.5, Math.min(1.5, pitch - dy * 0.008)); }
    draw(); });
  canvas.addEventListener('wheel', e => { e.preventDefault();
    if (walking) return;  // no zoom in walk mode — you move with the keys
    dist = Math.max(radius * 0.3, Math.min(radius * 12, dist * (1 + Math.sign(e.deltaY) * 0.1)));
    draw(); }, { passive: false });
  window.addEventListener('resize', () => { resize(); draw(); });

  function resize() { const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, canvas.width, canvas.height); }

  // The sun\'s orthographic view-projection, fitted to the scene AABB along the
  // light direction, so the shadow map covers the whole model at full resolution.
  // Eye sits back along the sun from the scene centre by the bounding radius; the
  // ortho half-extent is the radius (a hair padded), near/far bracket the depth
  // along the light. Rebuilt each draw from lightDir (moves with the time/season
  // slider). Deterministic; the returned matrix is column-major for uniformMatrix4fv.
  function buildLightMatrix() {
    const c = [(sceneMin[0] + sceneMax[0]) / 2, (sceneMin[1] + sceneMax[1]) / 2,
      (sceneMin[2] + sceneMax[2]) / 2];
    // A world-space radius that comfortably encloses the scene AABB corners.
    const r = Math.max(1, 0.5 * Math.hypot(sceneMax[0] - sceneMin[0],
      sceneMax[1] - sceneMin[1], sceneMax[2] - sceneMin[2])) * 1.15;
    const L = lightDir;                       // surface->sun, so the eye is +L * r
    const eye = [c[0] + L[0] * r * 2, c[1] + L[1] * r * 2, c[2] + L[2] * r * 2];
    // Guard an up vector parallel to the light (sun near straight overhead).
    const up = (Math.abs(L[1]) > 0.99) ? [0, 0, 1] : [0, 1, 0];
    const v = lookAt(eye, c, up);
    const p = ortho(-r, r, -r, r, 0.01, r * 4);
    return mul(p, v);
  }

  // Render the shadow-caster scene from the sun into the depth FBO. Casters are all
  // opaque building layers EXCEPT glass (a window must pass light, not block it) and
  // the ground (a receiver only, not a scene node here). Per-node uModel is applied
  // so an OPEN door lets sun through the doorway — animated doors handled for free.
  // Hidden layers don\'t cast (a toggled-off roof throws no shadow). Casters ignore
  // the section cut (simplest and acceptable — a dollhouse cut still lights sanely).
  function drawShadowMap() {
    if (!shadowOK) return;
    lightVP = buildLightMatrix();
    gl.bindFramebuffer(gl.FRAMEBUFFER, shadowFBO);
    gl.viewport(0, 0, shadowSize, shadowSize);
    gl.clearColor(1, 1, 1, 1);   // far depth = white (packed) / unused (depth tex)
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(smProg);
    gl.uniformMatrix4fv(smLightVP, false, new Float32Array(lightVP));
    gl.uniform1f(smPack, shadowPack);
    for (const nd of nodes) {
      if (nd.glass || hidden[nd.layer] || !SHADOW_LAYERS[nd.layer]) continue;
      gl.uniformMatrix4fv(smModel, false, new Float32Array(
        (walking && nd.door) ? doorModelMatrix(nd) : IDENTITY));
      gl.bindBuffer(gl.ARRAY_BUFFER, nd.pb);
      gl.enableVertexAttribArray(smAPos);
      gl.vertexAttribPointer(smAPos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, nd.ib);
      gl.drawElements(gl.TRIANGLES, nd.count, gl.UNSIGNED_INT, 0);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    // The shadow pass set the viewport to the map's square; hand the canvas back
    // to the main pass, or everything renders scaled into the shadow map's frame
    // (the building lands in the top-right corner, most of the canvas empty).
    gl.viewport(0, 0, canvas.width, canvas.height);
  }

  function draw() {
    if (!hasScene) return;
    resize();
    // Shadow map first (its own FBO + viewport), before anything hits the screen, so
    // the main pass can sample it. Skipped entirely when shadows are unavailable.
    if (showGround && shadowOK) drawShadowMap();
    // Clear colour is a true no-op under the sky pass (which repaints every pixel);
    // it only shows for the split-second before the first sky draw.
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const aspect = canvas.width / Math.max(1, canvas.height);
    let eye, view, proj, fill, clip;
    if (walking) {
      // First-person: eye at plan (x, y) lifted to the eased floor + eye height;
      // glTF frame is (x, up, -y). Near plane 0.1 ft so interiors don't clip, a
      // ~60° FOV that reads naturally standing in a room.
      const fx = Math.sin(wYaw), fy = Math.cos(wYaw), cp = Math.cos(wPitch);
      eye = [wpos[0], wElev + walkEye, -wpos[1]];
      const dir = [cp * fx, Math.sin(wPitch), -cp * fy];
      const tgt = [eye[0] + dir[0], eye[1] + dir[1], eye[2] + dir[2]];
      proj = perspective(1.05, aspect, 0.1, Math.max(radius * 40, 300));
      view = lookAt(eye, tgt, [0, 1, 0]);
      fill = WALK_FILL;     // eye-attached interior fill (walk only)
      clip = NO_CLIP;       // no section cut inside the model (walk)
    } else {
      eye = [
        target[0] + dist * Math.cos(pitch) * Math.sin(yaw),
        target[1] + dist * Math.sin(pitch),
        target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
      proj = perspective(0.9, aspect, radius * 0.05, radius * 40);
      view = lookAt(eye, target, [0, 1, 0]);
      fill = 0.0;           // orbit: fill is a true no-op (identical pixels)
      clip = clipY;         // section cut applies in orbit only
    }
    const mvp = new Float32Array(mul(proj, view));
    // Atmosphere first: the sky gradient behind everything, then the ground plane,
    // then the model (opaque then glass). All renderer-only.
    if (showGround) drawSky();
    // The main program's per-frame camera + sun + fog + shadow uniforms (shared by
    // the ground draw and drawNodes).
    gl.useProgram(prog);
    gl.uniformMatrix4fv(uMVP, false, mvp);
    gl.uniform3fv(uEye, new Float32Array(eye));
    gl.uniform1f(uFill, fill);
    gl.uniform3fv(uLightDir, new Float32Array(lightDir));
    gl.uniform1f(uSunWarmth, sunWarmth);
    gl.uniform1f(uClipY, clip);
    // Fog agrees with the sky's horizon colour; off (near=far huge) when no ground.
    gl.uniform3fv(uFogColor, new Float32Array(skyHorizon));
    gl.uniform1f(uFogNear, showGround ? fogNear : 1e9);
    gl.uniform1f(uFogFar, showGround ? fogFar : 1e9);
    // Shadow uniforms: bind the depth map to unit 1, feed the light view-proj + PCF
    // texel size. uShadowOn gates the whole feature (off when unavailable, or when
    // the ground/atmosphere is toggled off so an unlit orbit stays flat as before).
    const shadowLive = showGround && shadowOK;
    gl.uniform1f(uShadowOn, shadowLive ? 1.0 : 0.0);
    if (shadowLive) {
      gl.uniformMatrix4fv(uLightVP, false, new Float32Array(lightVP));
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, shadowTex);
      gl.uniform1i(uShadowTex, 1);
      gl.uniform1f(uShadowTexel, 1.0 / shadowSize);
      gl.uniform1f(uShadowPack, shadowPack);
    } else {
      gl.uniformMatrix4fv(uLightVP, false, new Float32Array(IDENTITY));
    }
    if (showGround) { drawGround(); }
    drawNodes();
  }

  // Sky: a full-screen gradient behind the scene. Depth writes off + depth test
  // off so it paints every pixel and the model/ground draw over it normally. The
  // clip-space z (0.999) still parks it at the far plane conceptually.
  function drawSky() {
    gl.useProgram(skyProg);
    gl.disable(gl.DEPTH_TEST); gl.depthMask(false);
    gl.uniform3fv(uZenith, new Float32Array(skyZenith));
    gl.uniform3fv(uHorizon, new Float32Array(skyHorizon));
    gl.bindBuffer(gl.ARRAY_BUFFER, skyBuf);
    gl.enableVertexAttribArray(skyAPos);
    gl.vertexAttribPointer(skyAPos, 2, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.depthMask(true); gl.enable(gl.DEPTH_TEST);
  }

  // Ground: the muted-grass plane at y=0, drawn with the main program (so it gets
  // the same lighting + distance fog as the model). Its normal is up, so the sun's
  // diffuse reads on it; the speckle texture gives a grass fleck.
  function drawGround() {
    if (!groundCount) return;
    gl.uniformMatrix4fv(uModel, false, new Float32Array(IDENTITY));
    gl.uniform3fv(uColor, new Float32Array(GROUND_COLOR));
    gl.uniform1f(uRough, 0.95); gl.uniform1f(uMetal, 0.0);
    gl.uniform1f(uPatScale, 2.5);
    if (groundTex) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, groundTex);
      gl.uniform1i(uTex, 0); gl.uniform1f(uHasTex, 1.0); }
    else gl.uniform1f(uHasTex, 0.0);
    gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf);
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, groundNorm);
    gl.enableVertexAttribArray(aNorm); gl.vertexAttribPointer(aNorm, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, groundCount);
  }

  // The model matrix for a door leaf at its current open fraction (0 closed, 1
  // fully open). Swing rotates up to ~100 deg about the hinge toward `out`;
  // slide translates ~90% of the width along `dir`; overhead lifts ~90% of the
  // height. Only ever non-identity in walk mode — orbit forces `open`=0 below,
  // so the exported closed glb and the viewer agree.
  const SWING_MAX = 100 * Math.PI / 180;
  function doorModelMatrix(nd) {
    const d = nd.door, f = nd.open;
    if (!d || f <= 1e-4) return IDENTITY;
    if (d.mode === 'slide') {
      return translate(d.dir[0] * d.width * 0.9 * f, 0, -d.dir[1] * d.width * 0.9 * f);
    }
    if (d.mode === 'overhead') { return translate(0, d.height * 0.9 * f, 0); }
    // swing: sign chosen so the leaf rotates from the wall toward `out`. `dir`
    // and `out` are perpendicular plan units; their 2D cross fixes the handedness
    // (the y-axis rotation in the glTF frame flips sign vs. the plan cross).
    const cr = d.dir[0] * d.out[1] - d.dir[1] * d.out[0];
    const ang = SWING_MAX * f * (cr >= 0 ? 1 : -1);
    return rotY(d.hinge[0], d.hinge[1], ang);
  }

  // Draw one node with the main program (uModel/material/texture/geometry). Shared
  // by the opaque and glass passes; the caller sets the blend/depth state.
  function drawOne(nd) {
    gl.uniformMatrix4fv(uModel, false, new Float32Array(
      (walking && nd.door) ? doorModelMatrix(nd) : IDENTITY));
    gl.uniform3fv(uColor, (highlightId && nd.name === 'room:' + highlightId)
      ? highlightColor(nd.color) : nd.color);
    gl.uniform1f(uRough, nd.rough);
    gl.uniform1f(uMetal, nd.metal);
    gl.uniform1f(uPatScale, nd.patScale);
    if (nd.tex) { gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, nd.tex); gl.uniform1i(uTex, 0);
      gl.uniform1f(uHasTex, 1.0); }
    else { gl.uniform1f(uHasTex, 0.0); }
    gl.bindBuffer(gl.ARRAY_BUFFER, nd.pb);
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, nd.nb);
    gl.enableVertexAttribArray(aNorm); gl.vertexAttribPointer(aNorm, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, nd.ib);
    gl.drawElements(gl.TRIANGLES, nd.count, gl.UNSIGNED_INT, 0);
  }

  // The layer-respecting node draw, shared by the orbit and walk cameras (the
  // MVP/eye/sun/shadow uniforms are set by the caller). Two passes so glass reads
  // see-through: (1) OPAQUE — every non-glass visible node, depth write on; (2)
  // GLASS — the glazing panes LAST, with alpha blending on and depth WRITE off (so
  // a pane never occludes the pane behind it) but depth TEST on (so a wall in front
  // still hides it). Hidden layers (e.g. the roof toggled off to look inside) are
  // skipped in both passes and in walk mode too. A constant ~0.35 alpha with a
  // light view-facing fresnel boost is enough for a schematic — windows rarely
  // stack, so glass-over-glass artefacts are acceptable and no per-triangle sort is
  // done. `uGlassAlpha` 0 in the opaque pass keeps that pass fully opaque.
  function drawNodes() {
    // (1) Opaque pass.
    gl.uniform1f(uGlassAlpha, 0.0);
    for (const nd of nodes) {
      if (hidden[nd.layer] || nd.glass) continue;
      drawOne(nd);
    }
    // (2) Glass pass: blended, depth-test on, depth-write off.
    let anyGlass = false;
    for (const nd of nodes) { if (nd.glass && !hidden[nd.layer]) { anyGlass = true; break; } }
    if (!anyGlass) return;
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.depthMask(false);
    gl.uniform1f(uGlassAlpha, 0.35);
    for (const nd of nodes) {
      if (hidden[nd.layer] || !nd.glass) continue;
      drawOne(nd);
    }
    gl.depthMask(true);
    gl.disable(gl.BLEND);
    gl.uniform1f(uGlassAlpha, 0.0);
  }

  // === first-person walk mode ==============================================
  // A "Walk" pill overlays the canvas; Enter (when the view is visible) or a click
  // toggles it, Esc exits. Entering requests pointer lock; if that's denied we fall
  // back to click-drag look. Everything here is scoped to walk mode — the key/mouse
  // listeners are added on enter and removed on exit, so nothing leaks into the
  // host page's own shortcuts.
  const host = canvas.parentNode || document.body;
  const walkBtn = document.createElement('button');
  walkBtn.type = 'button';
  walkBtn.textContent = 'Walk';
  walkBtn.title = 'First-person walkthrough (Enter). Esc to exit.';
  walkBtn.style.cssText = 'position:absolute;bottom:12px;right:14px;z-index:6;'
    + 'font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;'
    + 'padding:7px 15px;border-radius:20px;border:1px solid var(--line, rgba(0,0,0,.14));'
    + 'background:var(--panel, rgba(255,255,255,.88));color:var(--ink, #1d2530);cursor:pointer;'
    + 'box-shadow:0 2px 10px rgba(20,30,50,.18);-webkit-backdrop-filter:blur(6px);'
    + 'backdrop-filter:blur(6px);display:none;';
  walkBtn.addEventListener('click', () => { walking ? exitWalk() : enterWalk(); });
  const walkHint = document.createElement('div');
  walkHint.style.cssText = 'position:absolute;bottom:54px;left:50%;'
    + 'transform:translateX(-50%);z-index:6;pointer-events:none;opacity:0;'
    + 'transition:opacity .3s;font:600 12px -apple-system,BlinkMacSystemFont,'
    + '"Segoe UI",Helvetica,Arial,sans-serif;padding:7px 15px;border-radius:20px;'
    + 'background:rgba(20,24,30,.84);color:#eef1f4;white-space:nowrap;';
  walkHint.textContent = 'WASD / joystick move · look · E doors · C eye height · '
    + 'M map · Shift run · Esc exit';
  if (host.style && getComputedStyle(host).position === 'static') host.style.position = 'relative';
  host.appendChild(walkBtn); host.appendChild(walkHint);
  let hintTimer = null;

  // A room-name toast: a *second* pill (the walk hint still fires on entry) that
  // reuses the hint's dark-pill look, shown when the player crosses into a room.
  // It sits just above the hint's baseline so the two never overlap. pointer-events
  // off so it never eats a canvas drag. `roomToast` fades in, holds, fades out.
  const roomToast = document.createElement('div');
  roomToast.style.cssText = 'position:absolute;bottom:90px;left:50%;'
    + 'transform:translateX(-50%);z-index:6;pointer-events:none;opacity:0;'
    + 'transition:opacity .3s;font:600 12px -apple-system,BlinkMacSystemFont,'
    + '"Segoe UI",Helvetica,Arial,sans-serif;padding:7px 15px;border-radius:20px;'
    + 'background:rgba(20,24,30,.84);color:#eef1f4;white-space:nowrap;';
  host.appendChild(roomToast);
  let toastTimer = null;

  // The mini-map HUD: a 2D-canvas inset in the TOP-RIGHT corner (clear of every
  // other overlay in both hosts — the Layers panel is top-left, the Walk/Eye/
  // Furniture pills bottom-right, the snapshot pill bottom-left, the hint
  // bottom-center). North-up, fixed orientation. Hidden outside walk mode and when
  // toggled off with M. The 2D context is grabbed once (not per frame); the canvas
  // is sized in setMinimapSize so it stays crisp on hi-dpi.
  const MAP_CSS = 172;               // css px (the inset is square)
  const minimap = document.createElement('canvas');
  minimap.title = 'Mini-map (M to toggle)';
  minimap.style.cssText = 'position:absolute;top:12px;right:12px;z-index:6;'
    + 'width:' + MAP_CSS + 'px;height:' + MAP_CSS + 'px;border-radius:10px;'
    + 'pointer-events:none;display:none;'
    + 'background:rgba(255,255,255,.12);border:1px solid rgba(140,150,165,.4);'
    + 'box-shadow:0 2px 10px rgba(20,30,50,.18);-webkit-backdrop-filter:blur(6px);'
    + 'backdrop-filter:blur(6px);';
  host.appendChild(minimap);
  const mapCtx = minimap.getContext('2d');
  let mapDpr = 0;                    // last device-pixel-ratio the canvas was sized for
  function setMinimapSize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (mapDpr === dpr && minimap.width) return;   // already correctly sized
    mapDpr = dpr;
    minimap.width = Math.round(MAP_CSS * dpr);
    minimap.height = Math.round(MAP_CSS * dpr);
  }

  // --- sun-study + section-cut controls (top-right, both modes) -------------
  // A compact "Sun" pill and a "Section" pill sit under the mini-map region in the
  // top-right. Each expands a small popover styled like the Layers panel. They show
  // in BOTH orbit and walk mode; the mini-map (walk-only) sits above them, so at
  // MAP_CSS + 12 px top the two pills clear it when both are visible. Section-cut
  // clipping only bites in orbit mode (walk forces no-clip), but the pill stays
  // reachable so you can set a cut before stepping inside.
  const CTRL_PILL = 'font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",'
    + 'Helvetica,Arial,sans-serif;padding:6px 13px;border-radius:20px;'
    + 'border:1px solid var(--line, rgba(0,0,0,.14));background:var(--panel, rgba(255,255,255,.88));'
    + 'color:var(--ink, #1d2530);cursor:pointer;box-shadow:0 2px 10px rgba(20,30,50,.18);'
    + '-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);';
  const POPOVER = 'margin-top:6px;padding:10px 12px;border-radius:10px;'
    + 'background:var(--panel, rgba(255,255,255,.92));border:1px solid var(--line, rgba(0,0,0,.10));'
    + 'box-shadow:0 4px 18px rgba(20,30,50,.14);-webkit-backdrop-filter:blur(6px);'
    + 'backdrop-filter:blur(6px);font:12px -apple-system,BlinkMacSystemFont,'
    + '"Segoe UI",Helvetica,Arial,sans-serif;color:var(--ink, #1d2530);display:none;min-width:172px;';
  const POP_HD = 'font-size:11px;text-transform:uppercase;letter-spacing:.6px;'
    + 'color:var(--faint, #8791a1);margin-bottom:6px;';
  // A 3-way / cycle mini-button inside a popover row.
  const SEG_BTN = 'font:600 11px -apple-system,BlinkMacSystemFont,"Segoe UI",'
    + 'Helvetica,Arial,sans-serif;padding:4px 9px;border-radius:14px;'
    + 'border:1px solid var(--line, rgba(0,0,0,.14));background:var(--panel, rgba(255,255,255,.6));'
    + 'color:var(--muted, #566072);cursor:pointer;margin-right:5px;';

  // A container stacked in the top-right, below the mini-map. Each control is a
  // wrapper holding its pill + its (hidden) popover so the popover tracks the pill.
  const sunWrap = document.createElement('div');
  sunWrap.style.cssText = 'position:absolute;top:' + (MAP_CSS + 24) + 'px;right:12px;'
    + 'z-index:6;display:flex;flex-direction:column;align-items:flex-end;';
  const sunBtn = document.createElement('button');
  sunBtn.type = 'button';
  sunBtn.title = 'Sun study: time of day + season';
  sunBtn.style.cssText = CTRL_PILL;
  const sunPop = document.createElement('div');
  sunPop.style.cssText = POPOVER;
  // Time-of-day row: a 6..18 slider (step 0.25 h) + a live "10:30" label.
  const sunTimeHd = document.createElement('div');
  sunTimeHd.style.cssText = POP_HD; sunTimeHd.textContent = 'Time of day';
  const sunTimeRow = document.createElement('div');
  sunTimeRow.style.cssText = 'display:flex;align-items:center;gap:8px;';
  const sunTime = document.createElement('input');
  sunTime.type = 'range'; sunTime.min = '6'; sunTime.max = '18'; sunTime.step = '0.25';
  sunTime.value = String(sunHours);
  sunTime.style.cssText = 'flex:1;accent-color:#d1873f;';
  const sunTimeLbl = document.createElement('span');
  sunTimeLbl.style.cssText = 'min-width:38px;text-align:right;font-variant-numeric:'
    + 'tabular-nums;color:var(--muted, #566072);';
  sunTimeLbl.textContent = clockLabel(sunHours);
  sunTime.addEventListener('input', () => {
    sunHours = parseFloat(sunTime.value);
    sunTimeLbl.textContent = clockLabel(sunHours);
    computeSun(); draw();
  });
  sunTimeRow.appendChild(sunTime); sunTimeRow.appendChild(sunTimeLbl);
  // Season row: a Winter / Equinox / Summer 3-way.
  const sunSeasonHd = document.createElement('div');
  sunSeasonHd.style.cssText = POP_HD + 'margin-top:9px;'; sunSeasonHd.textContent = 'Season';
  const sunSeasonRow = document.createElement('div');
  const seasonBtns = SEASONS.map((s, i) => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = s.label; b.style.cssText = SEG_BTN;
    b.addEventListener('click', e => {
      e.preventDefault(); sunSeasonIdx = i; computeSun();
      syncSeasonBtns(); draw();
    });
    sunSeasonRow.appendChild(b);
    return b;
  });
  sunPop.appendChild(sunTimeHd); sunPop.appendChild(sunTimeRow);
  sunPop.appendChild(sunSeasonHd); sunPop.appendChild(sunSeasonRow);
  sunWrap.appendChild(sunBtn); sunWrap.appendChild(sunPop);
  host.appendChild(sunWrap);
  let sunOpen = false;
  sunBtn.addEventListener('click', e => {
    e.preventDefault(); sunOpen = !sunOpen;
    sunPop.style.display = sunOpen ? '' : 'none';
  });
  // Highlight the active season chip so the 3-way reads as selected.
  function syncSeasonBtns() {
    seasonBtns.forEach((b, i) => {
      const on = i === sunSeasonIdx;
      b.style.background = on ? 'rgba(209,135,63,.9)' : 'rgba(255,255,255,.6)';
      b.style.color = on ? '#fff' : '#566072';
      b.style.borderColor = on ? 'rgba(209,135,63,.9)' : 'rgba(0,0,0,.14)';
    });
  }
  function updateSunUI() {
    sunBtn.textContent = 'Sun ' + clockLabel(sunHours);
    sunTime.value = String(sunHours);
    sunTimeLbl.textContent = clockLabel(sunHours);
    syncSeasonBtns();
  }

  // Section-cut control: a horizontal slider (compact) from ~3 ft above the ground
  // floor to above the ridge; at max it disables clipping. Plus level-isolation
  // pills (All / L1 / L2 ...) when the model has more than one storey.
  const secWrap = document.createElement('div');
  secWrap.style.cssText = 'position:absolute;top:' + (MAP_CSS + 62) + 'px;right:12px;'
    + 'z-index:6;display:flex;flex-direction:column;align-items:flex-end;';
  const secBtn = document.createElement('button');
  secBtn.type = 'button';
  secBtn.title = 'Section cut (dollhouse) + level isolation';
  secBtn.style.cssText = CTRL_PILL;
  const secPop = document.createElement('div');
  secPop.style.cssText = POPOVER;
  const secHd = document.createElement('div');
  secHd.style.cssText = POP_HD; secHd.textContent = 'Section cut';
  const secRow = document.createElement('div');
  secRow.style.cssText = 'display:flex;align-items:center;gap:8px;';
  const secSlide = document.createElement('input');
  secSlide.type = 'range'; secSlide.step = '0.5';
  secSlide.style.cssText = 'flex:1;accent-color:#d1873f;';
  const secLbl = document.createElement('span');
  secLbl.style.cssText = 'min-width:44px;text-align:right;font-variant-numeric:'
    + 'tabular-nums;color:var(--muted, #566072);';
  secSlide.addEventListener('input', () => {
    const v = parseFloat(secSlide.value);
    setSection(v);   // clears level isolation; NO_CLIP at the top of the range
  });
  secRow.appendChild(secSlide); secRow.appendChild(secLbl);
  // Level-isolation pills, populated in updateSectionUI when multi-storey.
  const lvlHd = document.createElement('div');
  lvlHd.style.cssText = POP_HD + 'margin-top:9px;'; lvlHd.textContent = 'Isolate level';
  const lvlRow = document.createElement('div');
  let lvlBtns = [];
  secPop.appendChild(secHd); secPop.appendChild(secRow);
  secPop.appendChild(lvlHd); secPop.appendChild(lvlRow);
  secWrap.appendChild(secBtn); secWrap.appendChild(secPop);
  host.appendChild(secWrap);
  let secOpen = false;
  secBtn.addEventListener('click', e => {
    e.preventDefault(); secOpen = !secOpen;
    secPop.style.display = secOpen ? '' : 'none';
  });
  // Push the current section state into the slider + label + the section pill text.
  function syncSectionUI() {
    const top = sceneMaxY + 1;
    const clipping = clipY < NO_CLIP - 1;
    secBtn.textContent = clipping ? ('Cut ' + fmtFt(clipY) + ' ft') : 'Section';
    secSlide.value = String(clipping ? clipY : top);
    secLbl.textContent = clipping ? (fmtFt(clipY) + ' ft') : 'Off';
    lvlBtns.forEach((b, i) => {
      const on = (i === 0) ? (levelIdx == null) : (levelIdx === i - 1);
      b.style.background = on ? 'rgba(209,135,63,.9)' : 'rgba(255,255,255,.6)';
      b.style.color = on ? '#fff' : '#566072';
      b.style.borderColor = on ? 'rgba(209,135,63,.9)' : 'rgba(0,0,0,.14)';
    });
  }
  // Rebuild the slider range + the level pills for the current scene. The slider
  // spans ~3 ft above the ground floor to the scene top + 1 (max = no clip). Level
  // pills appear only for a multi-storey model (All + one per floor elevation).
  function updateSectionUI() {
    const lo = Math.min(sceneMinY + 3, sceneMaxY);
    const hi = sceneMaxY + 1;
    secSlide.min = String(lo); secSlide.max = String(hi);
    lvlRow.textContent = '';
    lvlBtns = [];
    if (floorElevs.length > 1) {
      lvlHd.style.display = ''; lvlRow.style.display = '';
      const labels = ['All'].concat(floorElevs.map((e, i) => 'L' + (i + 1)));
      labels.forEach((lab, i) => {
        const b = document.createElement('button');
        b.type = 'button'; b.textContent = lab; b.style.cssText = SEG_BTN;
        b.addEventListener('click', e => {
          e.preventDefault();
          setLevel(i === 0 ? null : i - 1);
        });
        lvlRow.appendChild(b); lvlBtns.push(b);
      });
    } else {
      lvlHd.style.display = 'none'; lvlRow.style.display = 'none';
    }
    syncSectionUI();
  }

  // --- walk-mode control pills (eye height + furniture), shown only in walk ---
  // Same pill look as walkBtn (see its cssText); they sit just left of the
  // Walk/Exit pill along the bottom-right so touch users can reach them, and stay
  // hidden until walk mode is entered so orbit view is uncluttered.
  const PILL = 'position:absolute;bottom:12px;z-index:6;'
    + 'font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;'
    + 'padding:7px 13px;border-radius:20px;border:1px solid var(--line, rgba(0,0,0,.14));'
    + 'background:var(--panel, rgba(255,255,255,.88));color:var(--ink, #1d2530);cursor:pointer;'
    + 'box-shadow:0 2px 10px rgba(20,30,50,.18);-webkit-backdrop-filter:blur(6px);'
    + 'backdrop-filter:blur(6px);display:none;';
  const eyeBtn = document.createElement('button');
  eyeBtn.type = 'button';
  eyeBtn.title = 'Eye height: Standing / Seated / Child (C)';
  eyeBtn.style.cssText = PILL + 'right:150px;';
  eyeBtn.addEventListener('click', e => { e.preventDefault(); cycleEye(); });
  const furnBtn = document.createElement('button');
  furnBtn.type = 'button';
  furnBtn.title = 'Furniture collision: bump into fixtures, or ghost through';
  furnBtn.style.cssText = PILL + 'right:264px;';
  furnBtn.addEventListener('click', e => { e.preventDefault(); toggleFurniture(); });
  host.appendChild(eyeBtn); host.appendChild(furnBtn);

  // --- virtual thumbstick (touch/coarse-pointer only) -----------------------
  // A semi-transparent base circle anchored bottom-left with a draggable knob.
  // The stick vector feeds the same forward/strafe move logic as WASD; pushing
  // past ~85% of the radius engages run. Hidden until walk mode on a coarse
  // pointer. `stickId` tracks which pointer owns the stick so a second thumb can
  // look at the same time (per-pointer, via Pointer Events).
  const STICK_R = 52, KNOB_R = 26;   // base + knob radii (css px)
  const stickBase = document.createElement('div');
  stickBase.style.cssText = 'position:absolute;left:20px;bottom:20px;z-index:6;'
    + 'width:' + (STICK_R * 2) + 'px;height:' + (STICK_R * 2) + 'px;border-radius:50%;'
    + 'background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.35);'
    + 'box-shadow:0 2px 10px rgba(20,30,50,.18);touch-action:none;display:none;';
  const stickKnob = document.createElement('div');
  stickKnob.style.cssText = 'position:absolute;left:' + (STICK_R - KNOB_R) + 'px;'
    + 'top:' + (STICK_R - KNOB_R) + 'px;width:' + (KNOB_R * 2) + 'px;'
    + 'height:' + (KNOB_R * 2) + 'px;border-radius:50%;'
    + 'background:rgba(255,255,255,.62);border:1px solid rgba(0,0,0,.12);'
    + 'box-shadow:0 1px 6px rgba(20,30,50,.28);';
  stickBase.appendChild(stickKnob);
  host.appendChild(stickBase);
  let stickId = null;                // pointerId owning the stick (null = idle)
  const stickVec = [0, 0];           // [fwd, strafe] in -1..1, y up = forward
  let stickRun = false;              // pushed past the run threshold this frame

  function updateWalkUI() {
    walkBtn.style.display = (walk && walk.spawn) ? '' : 'none';
    eyeBtn.textContent = eyeLabel();
  }
  function eyeLabel() { return 'Eye ' + EYE_PRESETS[eyeIdx].ft.toFixed(1) + ' ft'; }
  function furnLabel() { return furniture ? 'Furniture on' : 'Furniture off'; }

  // Cycle the eye-height preset (click / tap / C). Eases toward the new height in
  // walkStep; standing (index 0) is seeded from walk.eyeHeight on setScene.
  function cycleEye() {
    eyeIdx = (eyeIdx + 1) % EYE_PRESETS.length;
    walkEyeTarget = EYE_PRESETS[eyeIdx].ft;
    eyeBtn.textContent = eyeLabel();
  }
  function toggleFurniture() {
    furniture = !furniture;
    furnBtn.textContent = furnLabel();
  }

  // --- thumbstick pointer handling ------------------------------------------
  // A pointer that lands on the stick base owns the stick (its id in `stickId`);
  // dragging it sets `stickVec` (forward/strafe, clamped to the base radius) and
  // engages run past ~85% of the radius. A stick pointer is fully independent of
  // the canvas look pointers, so one thumb steers while another looks. Captured on
  // the base so a drag that slides off it still tracks.
  function centreKnob() {
    stickKnob.style.left = (STICK_R - KNOB_R) + 'px';
    stickKnob.style.top = (STICK_R - KNOB_R) + 'px';
  }
  function stickMove(e) {
    const r = stickBase.getBoundingClientRect();
    let dx = e.clientX - (r.left + STICK_R), dy = e.clientY - (r.top + STICK_R);
    const len = Math.hypot(dx, dy) || 1;
    const cl = Math.min(len, STICK_R);            // clamp knob to the base
    const kx = dx / len * cl, ky = dy / len * cl;
    stickKnob.style.left = (STICK_R - KNOB_R + kx) + 'px';
    stickKnob.style.top = (STICK_R - KNOB_R + ky) + 'px';
    stickVec[0] = -ky / STICK_R;                  // up (screen -y) = forward
    stickVec[1] = kx / STICK_R;                   // right = strafe right
    stickRun = (cl / STICK_R) > 0.85;             // pushed to the rim = run
  }
  stickBase.addEventListener('pointerdown', e => {
    if (!walking || stickId !== null) return;
    e.preventDefault();
    stickId = e.pointerId;
    try { stickBase.setPointerCapture(e.pointerId); } catch (er) {}
    stickMove(e);
  });
  stickBase.addEventListener('pointermove', e => {
    if (stickId !== e.pointerId) return;
    e.preventDefault(); stickMove(e);
  });
  function stickEnd(e) {
    if (stickId !== e.pointerId) return;
    stickId = null; stickVec[0] = stickVec[1] = 0; stickRun = false; centreKnob();
  }
  stickBase.addEventListener('pointerup', stickEnd);
  stickBase.addEventListener('pointercancel', stickEnd);

  // Fold the live stick vector into the held-key move state each tick, so the
  // stick and WASD share one movement path (updateMove reads `keys`). run is the
  // Shift equivalent. Called from walkStep before updateMove.
  function updateStick(dt) {
    if (!coarse) return;
    const fwd = stickVec[0], str = stickVec[1];
    const dead = 0.12;                             // ignore tiny thumb wobble
    keys.f = fwd > dead; keys.b = fwd < -dead;
    keys.r = str > dead; keys.l = str < -dead;
    if (stickId !== null) keys.run = stickRun;     // stick drives run while held
  }

  function applyLook(dx, dy) {
    wYaw += dx * 0.0025;                                     // drag right → turn right
    const lim = 85 * Math.PI / 180;
    wPitch = Math.max(-lim, Math.min(lim, wPitch - dy * 0.0025));
  }

  function enterWalk() {
    if (!walk || !walk.spawn || !hasScene || walking) return;
    walking = true;
    savedCam = { yaw, pitch, dist, target: target.slice() };  // restore orbit on exit
    const sp = walk.spawn;
    wpos[0] = sp.x; wpos[1] = sp.y;
    wElev = sp.elevation || 0;
    const f = sp.face || [0, 1];
    wYaw = Math.atan2(f[0], f[1]);                            // face into the house
    wPitch = 0;
    wvel[0] = wvel[1] = 0;                                    // start from a standstill
    for (const k in keys) delete keys[k];
    for (const dr of walkDoors) for (const lf of dr.leaves) {
      lf.open = 0; lf.target = 0; lf.pinned = false;         // start every door shut
    }
    walkBtn.textContent = 'Exit';
    eyeBtn.textContent = eyeLabel(); furnBtn.textContent = furnLabel();
    eyeBtn.style.display = ''; furnBtn.style.display = '';
    stickId = null; stickVec[0] = stickVec[1] = 0; stickRun = false; centreKnob();
    if (coarse) stickBase.style.display = '';   // thumbstick on touch devices
    showWalkHint();
    curRoomIdx = -1; checkRoom();   // toast the spawn room if we start inside one
    drawMinimap();                  // paint the HUD immediately, before the first tick
    document.addEventListener('keydown', onWalkKey, true);
    document.addEventListener('keyup', onWalkKeyUp, true);
    document.addEventListener('mousemove', onLockMouse);
    document.addEventListener('pointerlockchange', onPLChange);
    // Touch/coarse pointers have no Esc and pointer lock hijacks the whole
    // screen, so don't request it there — drag-look + the joystick drive walk.
    if (!coarse && canvas.requestPointerLock) {
      try { canvas.requestPointerLock(); } catch (e) {}
    }
    walkLast = (window.performance || Date).now();
    walkRAF = requestAnimationFrame(walkStep);
  }

  function exitWalk() {
    if (!walking) return;
    walking = false;
    if (walkRAF) { cancelAnimationFrame(walkRAF); walkRAF = null; }
    document.removeEventListener('keydown', onWalkKey, true);
    document.removeEventListener('keyup', onWalkKeyUp, true);
    document.removeEventListener('mousemove', onLockMouse);
    document.removeEventListener('pointerlockchange', onPLChange);
    if (document.pointerLockElement === canvas && document.exitPointerLock)
      document.exitPointerLock();
    usePointerLock = false; walkDrag = false; lookId = null;
    for (const k in keys) delete keys[k];
    walkBtn.textContent = 'Walk';
    eyeBtn.style.display = 'none'; furnBtn.style.display = 'none';
    stickBase.style.display = 'none'; stickId = null;
    stickVec[0] = stickVec[1] = 0; stickRun = false; centreKnob();
    hideWalkHint();
    minimap.style.display = 'none'; hideRoomToast(); curRoomIdx = -1;
    if (savedCam) { yaw = savedCam.yaw; pitch = savedCam.pitch; dist = savedCam.dist;
      target[0] = savedCam.target[0]; target[1] = savedCam.target[1];
      target[2] = savedCam.target[2]; }
    draw();
  }

  function onPLChange() {
    if (document.pointerLockElement === canvas) { usePointerLock = true; }
    else if (walking && usePointerLock) { usePointerLock = false; exitWalk(); }
  }
  function onLockMouse(e) { if (walking && usePointerLock)
    applyLook(e.movementX || 0, e.movementY || 0); }

  const WALK_KEYS = { KeyW: 'f', ArrowUp: 'f', KeyS: 'b', ArrowDown: 'b',
    KeyA: 'l', ArrowLeft: 'l', KeyD: 'r', ArrowRight: 'r' };
  function onWalkKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); exitWalk(); return; }
    if (e.code === 'KeyE') { e.preventDefault(); toggleNearestDoor(); return; }
    if (e.code === 'KeyC') { e.preventDefault(); cycleEye(); return; }
    if (e.code === 'KeyM') { e.preventDefault(); toggleMinimap(); return; }
    const m = WALK_KEYS[e.code];
    if (m) { keys[m] = true; e.preventDefault(); }
    if (e.key === 'Shift') keys.run = true;
  }

  // E flips the nearest door within 4.5 ft and *pins* it (open or shut) so it
  // holds against the proximity rule until E again or the player walks out of
  // range. `pinned` rides on the door's leaves; updateDoors clears it out of range.
  function toggleNearestDoor() {
    let best = null, bd = 4.5 * 4.5;
    for (const dr of walkDoors) {
      if (Math.abs(dr.elevation - wElev) > walkFF * 0.75) continue;
      const dx = wpos[0] - dr.mid[0], dy = wpos[1] - dr.mid[1], q = dx * dx + dy * dy;
      if (q < bd) { bd = q; best = dr; }
    }
    if (!best) return;
    const opening = best.leaves.length && best.leaves[0].target < 0.5;
    for (const lf of best.leaves) { lf.target = opening ? 1 : 0; lf.pinned = true; }
  }
  function onWalkKeyUp(e) {
    const m = WALK_KEYS[e.code];
    if (m) keys[m] = false;
    if (e.key === 'Shift') keys.run = false;
  }

  function showWalkHint() { walkHint.style.opacity = '1';
    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { walkHint.style.opacity = '0'; }, 2600); }
  function hideWalkHint() { walkHint.style.opacity = '0';
    if (hintTimer) { clearTimeout(hintTimer); hintTimer = null; } }

  // === room detection + name toast =========================================
  // The room containing plan point (x, y) on the player's current storey, as an
  // index into walkRooms (-1 = none: porch / outside every rect). Ties — a closet
  // nested in a bedroom, both containing the point — go to the SMALLEST rect so
  // the tightest enclosing room wins. Only rooms near the current elevation count,
  // using the same storey filter the walls/fixtures use.
  function roomAt(x, y) {
    let best = -1, bestA = Infinity;
    for (let i = 0; i < walkRooms.length; i++) {
      const r = walkRooms[i];
      if (Math.abs(r.elevation - wElev) > walkFF * 0.75) continue;
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.l) {
        if (r.area2 < bestA) { bestA = r.area2; best = i; }
      }
    }
    return best;
  }
  // Fire a toast only when the player *crosses* into a different room; re-entering
  // the same room without leaving it first is silent, and stepping out into a
  // porch/outside (idx -1) clears the tracker but shows nothing.
  function checkRoom() {
    const idx = roomAt(wpos[0], wpos[1]);
    if (idx === curRoomIdx) return;
    curRoomIdx = idx;
    if (idx >= 0) showRoomToast(walkRooms[idx].label);
  }
  function showRoomToast(text) {
    roomToast.textContent = text;
    roomToast.style.opacity = '1';
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { roomToast.style.opacity = '0'; }, 2000);
  }
  function hideRoomToast() {
    roomToast.style.opacity = '0';
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  }

  // === mini-map HUD ========================================================
  // Draw the current storey (walls with door gaps + faint room outlines) into the
  // top-right inset, north-up, with the player as a dot + a ~60-degree view cone
  // rotated by wYaw. Auto-fits the storey's segment/room bounds into the inset with
  // a small padding. Cheap: at most a few hundred short strokes; nothing is
  // allocated per frame beyond a few numbers. Colors are rgba so they read on both
  // the light and the prefers-color-scheme dark viewer themes.
  function nearElev(e) { return Math.abs(e - wElev) <= walkFF * 0.75; }
  function drawMinimap() {
    if (!minimapOn || !walk) { minimap.style.display = 'none'; return; }
    minimap.style.display = '';
    setMinimapSize();
    const S = minimap.width, ctx = mapCtx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, S, S);
    // Fit the current storey's walls + rooms into the inset. Gather bounds in plan
    // feet; if the storey is empty (shouldn't happen mid-walk) bail after clearing.
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const s of walkSegs) { if (!nearElev(s[4])) continue;
      minx = Math.min(minx, s[0], s[2]); maxx = Math.max(maxx, s[0], s[2]);
      miny = Math.min(miny, s[1], s[3]); maxy = Math.max(maxy, s[1], s[3]); }
    for (const r of walkRooms) { if (!nearElev(r.elevation)) continue;
      minx = Math.min(minx, r.x); maxx = Math.max(maxx, r.x + r.w);
      miny = Math.min(miny, r.y); maxy = Math.max(maxy, r.y + r.l); }
    if (!(maxx > minx) || !(maxy > miny)) return;
    const pad = 12 * mapDpr;                        // inset padding (device px)
    const spanx = maxx - minx, spany = maxy - miny;
    const sc = Math.min((S - 2 * pad) / spanx, (S - 2 * pad) / spany);
    const ox = (S - sc * spanx) / 2, oy = (S - sc * spany) / 2;
    // Plan (x east, y north) -> canvas: x right, y UP (north-up), so flip Y. The
    // map never rotates with the player; only the view cone rotates.
    const px = x => ox + (x - minx) * sc;
    const py = y => S - (oy + (y - miny) * sc);
    // 1. Room outlines, faint (drawn under the walls).
    ctx.lineWidth = Math.max(1, mapDpr);
    ctx.strokeStyle = 'rgba(150,160,175,.5)';
    for (const r of walkRooms) { if (!nearElev(r.elevation)) continue;
      ctx.strokeRect(px(r.x), py(r.y + r.l), r.w * sc, r.l * sc); }
    // 2. Walls (already door-punched in walkSegs) plus every *closed* door span as
    // a thin mark; an open door leaves its gap. Neutral line that reads on both
    // themes. Batched into one path so it's a single stroke call.
    ctx.strokeStyle = 'rgba(120,132,150,.95)';
    ctx.lineWidth = Math.max(1.5, 1.5 * mapDpr);
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (const s of walkSegs) { if (!nearElev(s[4])) continue;
      ctx.moveTo(px(s[0]), py(s[1])); ctx.lineTo(px(s[2]), py(s[3])); }
    ctx.stroke();
    // Closed-door spans as a lighter thin mark (an open door shows a gap instead).
    ctx.strokeStyle = 'rgba(120,132,150,.5)';
    ctx.lineWidth = Math.max(1, mapDpr);
    ctx.beginPath();
    for (const dr of walkDoors) { if (!nearElev(dr.elevation)) continue;
      const shut = dr.leaves.length && dr.leaves.some(lf => lf.open < 0.5);
      if (!shut) continue;
      ctx.moveTo(px(dr.seg[0]), py(dr.seg[1])); ctx.lineTo(px(dr.seg[2]), py(dr.seg[3])); }
    ctx.stroke();
    // 3. The player: a ~60-degree view cone (matching the walk FOV) rotated by
    // wYaw, then a dot. Plan yaw 0 faces +y (north); a plan direction (sin, cos)
    // maps to canvas (right = +x, up = +y with the Y flip). Cone length is a small
    // fraction of the inset so it reads without swamping the map.
    const cx = px(wpos[0]), cy = py(wpos[1]);
    const cone = S * 0.16, half = 30 * Math.PI / 180;   // 60-degree total spread
    function dirPt(ang, len) {                          // plan yaw -> canvas point
      return [cx + Math.sin(ang) * len, cy - Math.cos(ang) * len]; }
    const pL = dirPt(wYaw - half, cone), pR = dirPt(wYaw + half, cone);
    ctx.fillStyle = 'rgba(209,135,63,.28)';             // the viewer's accent, faint
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(pL[0], pL[1]); ctx.lineTo(pR[0], pR[1]); ctx.closePath(); ctx.fill();
    ctx.fillStyle = 'rgba(209,135,63,1)';
    ctx.beginPath(); ctx.arc(cx, cy, Math.max(2.5, 3 * mapDpr), 0, 2 * Math.PI); ctx.fill();
  }
  function toggleMinimap() { minimapOn = !minimapOn; drawMinimap(); }

  function walkStep(now) {
    if (!walking) return;
    const dt = Math.min(0.05, (now - walkLast) / 1000) || 0; walkLast = now;
    updateDoors(dt);   // resolve door open fractions first so collision agrees
    updateStick(dt);   // fold the virtual thumbstick into the held-key state
    updateMove(dt);
    // Ease the eye height toward the active preset over ~0.25 s so cycling glides.
    walkEye += (walkEyeTarget - walkEye) * Math.min(1, dt * 4);
    if (Math.abs(walkEye - walkEyeTarget) < 1e-3) walkEye = walkEyeTarget;
    draw();
    checkRoom();       // toast on crossing into a new room (cheap point-in-rects)
    drawMinimap();     // redraw the HUD inset (also cheap; skips when off/hidden)
    walkRAF = requestAnimationFrame(walkStep);
  }

  // Proximity door logic + easing. A door within ~3.5 ft of the player wants to
  // be open; beyond ~6 ft it wants to be shut (a dead band between avoids it
  // flapping on the threshold). A pinned door (toggled with E) ignores this until
  // the player leaves the ~6 ft range, then un-pins. Each leaf eases its `open`
  // toward `target` over ~0.35 s. A door's leaves share the same door id, so a
  // double/french pair opens together.
  const DOOR_NEAR = 3.5, DOOR_FAR = 6.0, DOOR_EASE = 1 / 0.35;
  function updateDoors(dt) {
    const k = Math.min(1, dt * DOOR_EASE);
    for (const dr of walkDoors) {
      const near = Math.abs(dr.elevation - wElev) <= walkFF * 0.75;
      const dx = wpos[0] - dr.mid[0], dy = wpos[1] - dr.mid[1];
      const dist = Math.hypot(dx, dy);
      for (const lf of dr.leaves) {
        if (lf.pinned) { if (!near || dist > DOOR_FAR) lf.pinned = false; }
        if (!lf.pinned) {
          if (near && dist < DOOR_NEAR) lf.target = 1;
          else if (!near || dist > DOOR_FAR) lf.target = 0;
        }
        lf.open += (lf.target - lf.open) * k;
        if (Math.abs(lf.open - lf.target) < 1e-3) lf.open = lf.target;
      }
    }
  }

  // Velocity smoothing time constants (Phase 6): reach the input speed over ~0.15 s
  // of acceleration; coast to a stop over ~0.1 s when input drops. Smoothing is on
  // the *velocity* only — collision below is byte-for-byte the same slide, just fed
  // the eased displacement — so a person eases in and out of a stride instead of a
  // cart snapping on/off.
  const WALK_ACCEL_TAU = 0.15, WALK_DECEL_TAU = 0.10;
  // One movement tick: WASD relative to the look yaw, smooth toward the target
  // velocity, slide off walls, then ease the floor height under the (possibly
  // stair-ramped) position.
  function updateMove(dt) {
    const fx = Math.sin(wYaw), fy = Math.cos(wYaw);  // forward (plan)
    const rx = fy, ry = -fx;                          // right (plan)
    let mx = 0, my = 0;
    if (keys.f) { mx += fx; my += fy; } if (keys.b) { mx -= fx; my -= fy; }
    if (keys.r) { mx += rx; my += ry; } if (keys.l) { mx -= rx; my -= ry; }
    const ml = Math.hypot(mx, my);
    // Target velocity: the (normalised) input direction times the current speed, or
    // zero when no key/stick is held. Accelerate toward it (tau 0.15 s) or
    // decelerate to it (tau 0.10 s); an exponential approach framed per-dt.
    let tvx = 0, tvy = 0;
    if (ml > 1e-6) {
      const spd = walkSpeed * (keys.run ? 2.5 : 1);
      tvx = (mx / ml) * spd; tvy = (my / ml) * spd;
    }
    const tau = (ml > 1e-6) ? WALK_ACCEL_TAU : WALK_DECEL_TAU;
    const a = 1 - Math.exp(-dt / tau);               // eased blend factor for this dt
    wvel[0] += (tvx - wvel[0]) * a;
    wvel[1] += (tvy - wvel[1]) * a;
    if (Math.hypot(wvel[0], wvel[1]) < 1e-3) { wvel[0] = 0; wvel[1] = 0; }
    if (wvel[0] !== 0 || wvel[1] !== 0) {
      let nx = wpos[0] + wvel[0] * dt, ny = wpos[1] + wvel[1] * dt;
      const slid = collide(nx, ny); nx = slid[0]; ny = slid[1];
      // Don't let a player walk off the slab into the void upstairs; grade
      // (elevation ~0) is open so you can step out an exterior door onto a porch.
      const ft = floorTarget(nx, ny);
      if (ft == null && wElev > 0.08) { wvel[0] = 0; wvel[1] = 0; /* blocked */ }
      else { wpos[0] = nx; wpos[1] = ny; }
    }
    let te = floorTarget(wpos[0], wpos[1]);
    if (te == null) te = (wElev <= 0.08) ? 0 : wElev;
    wElev += (te - wElev) * Math.min(1, dt * 12);   // smooth the step/stair transition
  }

  // The wall segments plus every *closed* door span — a door whose leaves are
  // less than half open re-blocks its gap, so you can't walk through a shut door.
  // Rebuilt each move tick off the live `open` state; cased openings never appear
  // here (they have no walk.doors entry), so they stay open.
  function liveSegments() {
    const segs = walkSegs;
    if (!walkDoors.length) return segs;
    const live = segs.slice();
    for (const dr of walkDoors) {
      const shut = dr.leaves.length && dr.leaves.some(lf => lf.open < 0.5);
      if (shut) live.push(dr.seg);
    }
    return live;
  }

  // Slide a circle (radius WALK_R) out of every nearby wall/closed-door segment,
  // then (when furniture collision is on) out of every fixture footprint rect.
  // Two passes so an inside corner resolves cleanly; only segments/rects near the
  // current storey count.
  function collide(x, y) {
    const segs = liveSegments();
    for (let pass = 0; pass < 2; pass++) {
      for (const s of segs) {
        if (Math.abs(s[4] - wElev) > walkFF * 0.75) continue;
        const ax = s[0], ay = s[1], ex = s[2] - ax, ey = s[3] - ay;
        const el = ex * ex + ey * ey || 1;
        let t = ((x - ax) * ex + (y - ay) * ey) / el; t = Math.max(0, Math.min(1, t));
        const cx = ax + ex * t, cy = ay + ey * t;
        let dx = x - cx, dy = y - cy, d = Math.hypot(dx, dy);
        if (d < WALK_R) { if (d < 1e-6) { dx = 1; dy = 0; d = 1; }
          x = cx + dx / d * WALK_R; y = cy + dy / d * WALK_R; }
      }
      if (furniture) for (const r of walkFixtures) {
        if (Math.abs(r[4] - wElev) > walkFF * 0.75) continue;
        // Closest point on the axis-aligned rect [x,y,w,l], then push the circle
        // out along the vector to it. Deep inside the rect (the player is already
        // overlapping a fixture when collision toggles on) the push-out would jump
        // more than ~2 ft — skip it so toggling furniture on never traps you.
        const rx0 = r[0], ry0 = r[1], rx1 = r[0] + r[2], ry1 = r[1] + r[3];
        const qx = Math.max(rx0, Math.min(x, rx1)), qy = Math.max(ry0, Math.min(y, ry1));
        let dx = x - qx, dy = y - qy, d = Math.hypot(dx, dy);
        if (d >= WALK_R) continue;             // circle clears the rect
        if (d < 1e-6) {                        // centre inside: push to nearest edge
          const dl = x - rx0, dr = rx1 - x, db = y - ry0, dtp = ry1 - y;
          const m = Math.min(dl, dr, db, dtp);
          if (m > 2.0) continue;               // deep inside — don't trap the player
          if (m === dl) { dx = -1; dy = 0; } else if (m === dr) { dx = 1; dy = 0; }
          else if (m === db) { dx = 0; dy = -1; } else { dx = 0; dy = 1; }
          d = 1;
          x = (dx < 0 ? rx0 : dx > 0 ? rx1 : x) + dx * WALK_R;
          y = (dy < 0 ? ry0 : dy > 0 ? ry1 : y) + dy * WALK_R;
        } else if (WALK_R - d <= 2.0) {        // shallow overlap: slide out
          x = qx + dx / d * WALK_R; y = qy + dy / d * WALK_R;
        }
      }
    }
    return [x, y];
  }

  // The floor elevation under a plan point: a stair ramp if over one, else the
  // containing slab whose level is nearest the current eye (so you stay on your
  // storey where floors stack). null ⇒ off every slab (the void / grade).
  function floorTarget(x, y) {
    if (walk) {
      for (const s of walk.stairs) {
        if (x >= s.x - 0.5 && x <= s.x + s.w + 0.5 && y >= s.y - 0.5 && y <= s.y + s.l + 0.5) {
          const t = stairT(s, x, y);
          return s.fromElevation + (s.toElevation - s.fromElevation) * t;
        }
      }
      let best = null;
      for (const f of walk.floors) for (const r of f.rects) {
        if (x >= r[0] - 0.1 && x <= r[0] + r[2] + 0.1 && y >= r[1] - 0.1 && y <= r[1] + r[3] + 0.1) {
          if (best == null || Math.abs(f.elevation - wElev) < Math.abs(best - wElev))
            best = f.elevation;
        }
      }
      return best;
    }
    return null;
  }

  // How far up a stair footprint the point is (0 at the bottom level, 1 at the
  // top), measured along the stair's uphill direction.
  function stairT(s, x, y) {
    const dx = s.dir[0], dy = s.dir[1];
    let lo = 1e9, hi = -1e9;
    const cs = [[s.x, s.y], [s.x + s.w, s.y], [s.x, s.y + s.l], [s.x + s.w, s.y + s.l]];
    for (const c of cs) { const p = c[0] * dx + c[1] * dy; if (p < lo) lo = p; if (p > hi) hi = p; }
    const pp = x * dx + y * dy;
    return hi > lo ? Math.max(0, Math.min(1, (pp - lo) / (hi - lo))) : 0;
  }

  // Enter toggles walk mode when the 3D view is on screen and the user isn't
  // typing — a light, guarded global that never swallows the host's shortcuts.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.ctrlKey || e.metaKey || e.altKey || walking) return;
    if (!walk || !walk.spawn) return;
    const a = document.activeElement, tag = a && a.tagName;
    if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT') return;
    if (canvas.offsetParent === null) return;   // 3D pane not visible
    e.preventDefault(); enterWalk();
  });

  // `walkState` lets a test (or a host without pointer lock) read/verify the
  // camera; `enterWalk`/`exitWalk` drive it. `walkTeleport` jumps the player to a
  // plan point (and optional yaw) with the floor resolved instantly — handy for a
  // host that wants to drop you in a chosen room, and for headless driving.
  // `eyeHeight`/`furniture` report the live presets; the setters mirror the pills
  // so a host (or a headless test) can drive them without synthesising pointers.
  function walkState() {
    const rm = curRoomIdx >= 0 ? walkRooms[curRoomIdx] : null;
    return { walking, x: wpos[0], y: wpos[1], elevation: wElev,
      yaw: wYaw, pitch: wPitch, eye: wElev + walkEye,
      // `speed` is the smoothed velocity magnitude (ft/s); a headless test watches
      // it ramp over several ticks rather than jumping to full on the first frame.
      vx: wvel[0], vy: wvel[1], speed: Math.hypot(wvel[0], wvel[1]),
      eyeHeight: walkEye, eyeTarget: walkEyeTarget, eyeLabel: EYE_PRESETS[eyeIdx].label,
      furniture, coarse, minimap: minimapOn,
      room: rm ? rm.name : null, roomLabel: rm ? rm.label : null };
  }
  // `sunState` reports the live sun + section state so a headless test can assert
  // the direction moves with time/season, warmth rises toward dusk, and the clip
  // plane responds to the section slider / level isolation. `clipActive` is false
  // when nothing is cut (clipY at the no-clip sentinel).
  function sunState() {
    return {
      hours: sunHours, season: SEASONS[sunSeasonIdx].key,
      lightDir: lightDir.slice(), warmth: sunWarmth,
      orientation: sunOrient, latitude: sunLat,
      clipY: clipY, clipActive: clipY < NO_CLIP - 1, level: levelIdx,
      walking: walking,
      // Atmosphere (Phase 6): the derived sky/horizon/ground colours + the fog band,
      // so a headless test can check the horizon warms with the sun. `showGround`
      // reports the session toggle.
      skyZenith: skyZenith.slice(), skyHorizon: skyHorizon.slice(),
      groundColor: GROUND_COLOR.slice(), fogNear: fogNear, fogFar: fogFar,
      showGround: showGround,
      // Shadow map (Phase 7): whether shadows are live, and the sun\'s current
      // orthographic view-projection, so a headless test can map a known world
      // point through it and assert it lands inside the unit shadow frustum.
      shadowOn: shadowOK, shadowSize: shadowSize, lightVP: buildLightMatrix(),
    };
  }
  // Toggle the sky+ground+shadow atmosphere (renderer-only; session flag). The test
  // + a future host pill both drive it. Returns the new state.
  function setGround(on) {
    showGround = (on == null) ? !showGround : !!on;
    draw();
    return showGround;
  }
  function walkTeleport(x, y, yaw) {
    if (!walking) return;
    wpos[0] = x; wpos[1] = y;
    if (yaw != null) wYaw = yaw;
    let te = floorTarget(x, y); if (te == null) te = (wElev <= 0.08) ? 0 : wElev;
    wElev = te; draw();
    checkRoom();     // a teleport across a room boundary toasts the new room
    drawMinimap();   // and repaints the HUD at the new position
  }
  // Push a raw stick vector (forward, strafe in -1..1) for headless testing; the
  // move step consumes it exactly as a live thumbstick would. `run` mirrors the
  // rim threshold. A null/zero vector recentres.
  function walkStick(fwd, strafe, run) {
    coarse = true;                        // simulate a touch device
    stickId = (fwd || strafe) ? 1 : null;
    stickVec[0] = fwd || 0; stickVec[1] = strafe || 0;
    stickRun = !!run;
  }

  // === Phase 5: ray picking, measure, bookmarks, tour, identify =============
  // A single 2D overlay canvas stretched over the WebGL canvas carries the measure
  // line + its distance pill and (when a click lands) nothing else — the identify
  // and room toasts reuse the dark-pill DOM. It never eats input (pointer-events
  // off) so orbit drag / walk look pass straight through to the GL canvas.
  const overlay = document.createElement('canvas');
  overlay.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;'
    + 'z-index:5;pointer-events:none;display:none;';
  host.appendChild(overlay);
  const ovCtx = overlay.getContext('2d');
  let ovDpr = 0;
  function sizeOverlay() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (overlay.width !== w || overlay.height !== h || ovDpr !== dpr) {
      overlay.width = w; overlay.height = h; ovDpr = dpr;
    }
  }

  // --- camera ray from a canvas pixel --------------------------------------
  // The renderer builds its own proj/view each frame, so rather than invert them we
  // rebuild the same camera basis (eye, forward, right, up) the draw step uses and
  // fire a ray through the pixel's normalised device coords, scaled by the FOV's
  // half-tangent. Works for both cameras: orbit (yaw/pitch/dist/target, 0.9 fovy)
  // and walk (wpos/wElev + wYaw/wPitch, 1.05 fovy). Returns {o, d} in world feet.
  function cameraBasis() {
    const aspect = (canvas.clientWidth || 1) / (canvas.clientHeight || 1);
    if (walking) {
      const cp = Math.cos(wPitch);
      const eye = [wpos[0], wElev + walkEye, -wpos[1]];
      const fwd = norm([cp * Math.sin(wYaw), Math.sin(wPitch), -cp * Math.cos(wYaw)]);
      return { eye, fwd, fovy: 1.05, aspect };
    }
    const eye = [
      target[0] + dist * Math.cos(pitch) * Math.sin(yaw),
      target[1] + dist * Math.sin(pitch),
      target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
    const fwd = norm(sub(target, eye));
    return { eye, fwd, fovy: 0.9, aspect };
  }
  function rayFromPixel(pxCss, pyCss) {
    const b = cameraBasis();
    const wCss = canvas.clientWidth || 1, hCss = canvas.clientHeight || 1;
    // NDC in [-1, 1], y up. A pixel at the canvas centre fires straight along fwd.
    const nx = (pxCss / wCss) * 2 - 1, ny = 1 - (pyCss / hCss) * 2;
    const up0 = [0, 1, 0];
    const right = norm(cross(b.fwd, up0));
    const up = cross(right, b.fwd);
    const t = Math.tan(b.fovy / 2);
    const d = norm([
      b.fwd[0] + right[0] * nx * t * b.aspect + up[0] * ny * t,
      b.fwd[1] + right[1] * nx * t * b.aspect + up[1] * ny * t,
      b.fwd[2] + right[2] * nx * t * b.aspect + up[2] * ny * t]);
    return { o: b.eye.slice(), d };
  }

  // Moller-Trumbore ray/triangle: returns the ray parameter t (>0) of the hit, or
  // -1. Positions are already world-frame; a door leaf's runtime transform is
  // ignored (leaves closed at rest, and identify/measure target the built model).
  function rayTri(o, d, ax, ay, az, bx, by, bz, cx, cy, cz) {
    const e1x = bx - ax, e1y = by - ay, e1z = bz - az;
    const e2x = cx - ax, e2y = cy - ay, e2z = cz - az;
    const px = d[1] * e2z - d[2] * e2y, py = d[2] * e2x - d[0] * e2z,
      pz = d[0] * e2y - d[1] * e2x;
    const det = e1x * px + e1y * py + e1z * pz;
    if (det > -1e-9 && det < 1e-9) return -1;   // ray parallel to the triangle
    const inv = 1 / det;
    const tx = o[0] - ax, ty = o[1] - ay, tz = o[2] - az;
    const u = (tx * px + ty * py + tz * pz) * inv;
    if (u < -1e-6 || u > 1 + 1e-6) return -1;
    const qx = ty * e1z - tz * e1y, qy = tz * e1x - tx * e1z, qz = tx * e1y - ty * e1x;
    const v = (d[0] * qx + d[1] * qy + d[2] * qz) * inv;
    if (v < -1e-6 || u + v > 1 + 1e-6) return -1;
    return (e2x * qx + e2y * qy + e2z * qz) * inv;
  }

  // Pick the nearest triangle under a canvas pixel across every *visible* node
  // (hidden layers skipped), honouring the section clip (a hit above uClipY when
  // clipping is active is ignored, matching the fragment discard). Returns
  // {point:[x,y,z], node, name, mat, layer, dist} or null. This is the headless
  // test hook too: `ctrl.pick(px, py)` returns the same object.
  function pick(pxCss, pyCss) {
    if (!hasScene) return null;
    const { o, d } = rayFromPixel(pxCss, pyCss);
    const clip = (!walking && clipY < NO_CLIP - 1) ? clipY : NO_CLIP;
    let bestT = Infinity, best = null;
    for (const nd of nodes) {
      if (hidden[nd.layer]) continue;
      const p = nd.pos, ix = nd.idx;
      for (let i = 0; i + 2 < ix.length; i += 3) {
        const a = ix[i] * 3, b2 = ix[i + 1] * 3, c = ix[i + 2] * 3;
        const t = rayTri(o, d,
          p[a], p[a + 1], p[a + 2], p[b2], p[b2 + 1], p[b2 + 2],
          p[c], p[c + 1], p[c + 2]);
        if (t > 1e-4 && t < bestT) {
          const hy = o[1] + d[1] * t;
          if (hy > clip) continue;               // above the section cut: not visible
          bestT = t; best = nd;
        }
      }
    }
    if (!best) return null;
    return { point: [o[0] + d[0] * bestT, o[1] + d[1] * bestT, o[2] + d[2] * bestT],
      node: best, name: best.name, mat: best.mat, layer: best.layer, dist: bestT };
  }

  // A world (glTF-frame) point -> a canvas CSS pixel, or null when behind the eye.
  // Used to place the measure endpoints + label on the 2D overlay. Rebuilds the
  // same basis rayFromPixel uses so the projection round-trips.
  function worldToCss(wp) {
    const b = cameraBasis();
    const rel = sub(wp, b.eye);
    const up0 = [0, 1, 0];
    const right = norm(cross(b.fwd, up0));
    const up = cross(right, b.fwd);
    const z = dot(rel, b.fwd);
    if (z <= 1e-4) return null;                   // behind (or on) the camera plane
    const t = Math.tan(b.fovy / 2);
    const ndcx = dot(rel, right) / (z * t * b.aspect);
    const ndcy = dot(rel, up) / (z * t);
    const wCss = canvas.clientWidth || 1, hCss = canvas.clientHeight || 1;
    return [(ndcx * 0.5 + 0.5) * wCss, (1 - (ndcy * 0.5 + 0.5)) * hCss];
  }

  // --- identify + measure helpers ------------------------------------------
  // Human name for a picked node from its name prefix (walls, roof, doors, ...),
  // falling back to the room display name from walk.rooms for a room floor. ASCII.
  function roomNameById(id) {
    for (const r of walkRooms) if (r.id === id) return r.name;
    return null;
  }
  function titleCase(s) {
    return s.replace(/[_~]+/g, ' ').trim()
      .replace(/\b\w/g, c => c.toUpperCase());
  }
  function identityOf(hit) {
    const nm = hit.name || '', mat = hit.mat || '';
    let label;
    if (nm.startsWith('wall:')) label = 'Wall';
    else if (nm.startsWith('roof') || hit.layer === 'roof') label = 'Roof';
    else if (nm.startsWith('door:')) {
      const id = nm.split(':')[1] || '';
      label = id ? ('Door: ' + id) : 'Door';
    } else if (nm.startsWith('cased:')) label = 'Opening';
    else if (nm.startsWith('window:') || nm.startsWith('glazing:')) label = 'Window';
    else if (nm.startsWith('opening:')) label = 'Opening';
    else if (nm.startsWith('fixture:')) {
      // Names read "fixture:<room>~<kind>~<index>[:<part>]" — the kind is the middle
      // tilde-part; fall back to the whole tail if the shape differs.
      const tail = nm.slice('fixture:'.length).split(':')[0];
      const bits = tail.split('~');
      const kind = titleCase(bits.length >= 2 ? bits[1] : tail);
      label = kind ? ('Fixture: ' + kind) : 'Fixture';
    } else if (nm.startsWith('room:')) {
      const rn = roomNameById(nm.slice(5));
      label = rn ? ('Room floor: ' + rn) : 'Room floor';
    } else if (nm.startsWith('slab:') || hit.layer === 'floors') label = 'Floor';
    else if (nm.startsWith('stair:') || hit.layer === 'stairs') label = 'Stair';
    else if (hit.layer === 'frame') label = 'Frame';
    else if (hit.layer === 'porches') label = 'Porch';
    else label = titleCase(nm.split(':')[0]) || 'Surface';
    return mat ? (label + ' (' + mat + ')') : label;
  }

  // A transient identify toast reusing the dark-pill look. It lives bottom-center
  // like the room toast but a touch higher so a walk-mode room toast + an identify
  // never sit on the exact same line; in orbit only this one shows.
  const idToast = document.createElement('div');
  idToast.style.cssText = 'position:absolute;bottom:52px;left:50%;'
    + 'transform:translateX(-50%);z-index:6;pointer-events:none;opacity:0;'
    + 'transition:opacity .3s;font:600 12px -apple-system,BlinkMacSystemFont,'
    + '"Segoe UI",Helvetica,Arial,sans-serif;padding:7px 15px;border-radius:20px;'
    + 'background:rgba(20,24,30,.84);color:#eef1f4;white-space:nowrap;';
  host.appendChild(idToast);
  let idTimer = null;
  function showIdentify(text) {
    idToast.textContent = text; idToast.style.opacity = '1';
    if (idTimer) clearTimeout(idTimer);
    idTimer = setTimeout(() => { idToast.style.opacity = '0'; }, 2200);
  }

  // --- measure mode --------------------------------------------------------
  // Armed by the Measure pill (both cameras). Click/tap two points on the model to
  // lay a tape: a thin line on the overlay + a floating distance pill at the
  // midpoint reading feet to 0.1, with the horizontal (plan) component when the
  // ends differ in height. Esc or the pill clears + disarms. A third click starts a
  // fresh pair.
  let measuring = false;
  const measurePts = [];              // 0..2 world-frame points
  function setMeasure(on) {
    measuring = (on == null) ? !measuring : !!on;
    measurePts.length = 0;
    measBtn.textContent = measuring ? 'Measuring' : 'Measure';
    measBtn.style.background = measuring ? 'rgba(209,135,63,.9)' : 'var(--panel, rgba(255,255,255,.88))';
    measBtn.style.color = measuring ? '#fff' : 'var(--ink, #1d2530)';
    canvas.style.cursor = measuring ? 'crosshair' : '';
    drawOverlay();
    return measuring;
  }
  function addMeasurePoint(pxCss, pyCss) {
    const hit = pick(pxCss, pyCss);
    if (!hit) return false;
    if (measurePts.length >= 2) measurePts.length = 0;   // third click restarts
    measurePts.push(hit.point);
    drawOverlay();
    return true;
  }
  // 3D distance (glTF frame, so world y is height); plus the horizontal/plan
  // component (drop the y term). Both in feet.
  function measureDistances() {
    if (measurePts.length < 2) return null;
    const a = measurePts[0], b = measurePts[1];
    const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
    return { d3: Math.hypot(dx, dy, dz), plan: Math.hypot(dx, dz), dh: Math.abs(dy) };
  }
  // Redraw the overlay: the measure line + endpoints + the distance pill. Cheap and
  // only invoked on interaction / camera move while measuring (not per orbit frame).
  function drawOverlay() {
    const show = measuring && measurePts.length > 0;
    if (!show) { overlay.style.display = 'none'; return; }
    overlay.style.display = ''; sizeOverlay();
    const S = ovDpr, ctx = ovCtx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    const sp = measurePts.map(worldToCss);
    ctx.fillStyle = 'rgba(209,135,63,1)';
    for (const p of sp) if (p) {
      ctx.beginPath(); ctx.arc(p[0] * S, p[1] * S, 4 * S, 0, 2 * Math.PI); ctx.fill();
    }
    if (sp.length === 2 && sp[0] && sp[1]) {
      ctx.strokeStyle = 'rgba(209,135,63,.95)'; ctx.lineWidth = Math.max(1.5, 2 * S);
      ctx.beginPath(); ctx.moveTo(sp[0][0] * S, sp[0][1] * S);
      ctx.lineTo(sp[1][0] * S, sp[1][1] * S); ctx.stroke();
      const dd = measureDistances();
      let txt = dd.d3.toFixed(1) + ' ft';
      if (dd.dh > 0.1) txt += ' (' + dd.plan.toFixed(1) + ' ft plan)';
      const mx = (sp[0][0] + sp[1][0]) / 2 * S, my = (sp[0][1] + sp[1][1]) / 2 * S;
      ctx.font = (13 * S) + 'px -apple-system,BlinkMacSystemFont,"Segoe UI",'
        + 'Helvetica,Arial,sans-serif';
      const w = ctx.measureText(txt).width, padx = 9 * S, pady = 5 * S, h = 20 * S;
      ctx.fillStyle = 'rgba(20,24,30,.9)';
      roundRect(ctx, mx - w / 2 - padx, my - h / 2 - pady, w + 2 * padx, h + 2 * pady, 10 * S);
      ctx.fill();
      ctx.fillStyle = '#eef1f4'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(txt, mx, my);
    }
  }
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  // --- camera bookmarks (Views) --------------------------------------------
  // Session-only saved cameras (no persistence beyond the copyable hash links). Each
  // is {name, mode, ...} where an orbit view stores yaw/pitch/dist/target and a walk
  // view stores x/y/yaw/pitch. Restoring enters/exits walk as needed.
  const views = [];                   // saved cameras, in save order
  let viewSeq = 0;                    // running counter for default "View N" names
  function currentCamera() {
    if (walking) {
      return { mode: 'w', x: wpos[0], y: wpos[1], yaw: wYaw, pitch: wPitch };
    }
    return { mode: 'o', yaw, pitch, dist,
      target: [target[0], target[1], target[2]] };
  }
  // Save the current camera as a named view. Returns the new view (test hook).
  function saveView(name) {
    const cam = currentCamera();
    cam.name = name || ('View ' + (++viewSeq));
    views.push(cam);
    renderViews();
    return cam;
  }
  // Apply a saved (or ad-hoc) camera. Orbit restores immediately; a walk camera
  // enters walk mode (hash-restore / programmatic entry never grabs pointer lock —
  // it goes through enterWalk which skips lock on coarse pointers; here we force a
  // no-lock entry by teleporting after enter) and teleports + sets the look angles.
  function restoreView(cam) {
    if (!cam) return;
    if (cam.mode === 'w') {
      if (!walking) { enterWalk(); }
      if (walking) {
        walkTeleport(cam.x, cam.y, cam.yaw);
        if (cam.pitch != null) wPitch = cam.pitch;
        draw();
      }
    } else {
      if (walking) exitWalk();
      yaw = cam.yaw; pitch = cam.pitch; dist = cam.dist;
      if (cam.target) { target[0] = cam.target[0]; target[1] = cam.target[1];
        target[2] = cam.target[2]; }
      draw();
    }
  }

  // --- shareable hash state ------------------------------------------------
  // A compact camera string in location.hash: "#v=o,yaw,pitch,dist,tx,ty,tz" for an
  // orbit view or "#v=w,x,y,yaw,pitch" for a walk view. Round-trips through
  // parseHash / encodeHash so a copied link restores the same shot.
  function encodeHash(cam) {
    const r = n => Math.round(n * 1000) / 1000;
    if (cam.mode === 'w') return 'v=w,' + [cam.x, cam.y, cam.yaw, cam.pitch].map(r).join(',');
    const t = cam.target || [0, 0, 0];
    return 'v=o,' + [cam.yaw, cam.pitch, cam.dist, t[0], t[1], t[2]].map(r).join(',');
  }
  function parseHash(h) {
    if (!h) return null;
    h = h.replace(/^#/, '');
    const m = /(?:^|&)v=([^&]+)/.exec(h);
    if (!m) return null;
    const parts = m[1].split(',');
    const f = parts.map(Number);
    if (parts[0] === 'o' && parts.length >= 7 && f.slice(1, 7).every(v => !isNaN(v))) {
      return { mode: 'o', yaw: f[1], pitch: f[2], dist: f[3],
        target: [f[4], f[5], f[6]] };
    }
    if (parts[0] === 'w' && parts.length >= 5 && f.slice(1, 5).every(v => !isNaN(v))) {
      return { mode: 'w', x: f[1], y: f[2], yaw: f[3], pitch: f[4] };
    }
    return null;
  }
  // Write the camera into location.hash and copy the full URL. Clipboard may be
  // blocked (file:// viewers), so fall back to a readonly, pre-selected input the
  // user can copy by hand. Never throws.
  function copyLink(cam) {
    let url = '';
    try {
      if (typeof location !== 'undefined') {
        location.hash = encodeHash(cam);
        url = location.href;
      }
    } catch (e) { url = ''; }
    if (!url) return;
    let done = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url); done = true;
      }
    } catch (e) { done = false; }
    if (done) { flashViewMsg('Link copied'); return; }
    // Fallback: a selectable readonly field so the user can copy manually.
    linkField.value = url; linkField.style.display = '';
    try { linkField.focus(); linkField.select(); } catch (e) {}
    flashViewMsg('Copy the link below');
  }

  // --- guided tour ---------------------------------------------------------
  // Plays saved views in order. Orbit legs ease yaw/pitch/dist/target over ~2 s; a
  // walk leg teleports then dwells ~2.5 s with a slow ~30-degree look pan. With no
  // saved views, an "Auto tour" builds a default walk tour from walk.rooms (spawn,
  // then each room centre by area descending, max ~6). Esc or any user input stops
  // it. A small state machine driven from its own RAF loop.
  let tour = null;                    // {stops, i, phase, t0, from, dwellPan} or null
  const TOUR_DWELL = 2.5, TOUR_EASE = 2.0, TOUR_PAN = 30 * Math.PI / 180;
  function tourNow() { return (window.performance || Date).now(); }
  function stopTour() {
    if (!tour) return;
    tour = null;
    if (tourRAF) { cancelAnimationFrame(tourRAF); tourRAF = null; }
    tourBtn.textContent = views.length >= 2 ? 'Play tour' : 'Auto tour';
  }
  let tourRAF = null;
  // The default walk tour from rooms: spawn first, then room centres by area desc,
  // capped at ~6 stops. Each stop is a walk camera aimed toward the plan centroid.
  function autoTourStops() {
    if (!walk || !walkRooms.length) return [];
    const g0 = (walk.floors && walk.floors[0]) ? walk.floors[0].elevation : 0;
    const ground = walkRooms.filter(r => Math.abs(r.elevation - g0) < 0.5);
    const rooms = (ground.length ? ground : walkRooms).slice()
      .sort((a, b) => b.area - a.area).slice(0, 5);
    const cx = rooms.reduce((s, r) => s + (r.x + r.w / 2), 0) / (rooms.length || 1);
    const cy = rooms.reduce((s, r) => s + (r.y + r.l / 2), 0) / (rooms.length || 1);
    const stops = [];
    if (walk.spawn) stops.push({ mode: 'w', x: walk.spawn.x, y: walk.spawn.y,
      yaw: Math.atan2((walk.spawn.face || [0, 1])[0], (walk.spawn.face || [0, 1])[1]),
      pitch: 0 });
    for (const r of rooms) {
      const rx = r.x + r.w / 2, ry = r.y + r.l / 2;
      stops.push({ mode: 'w', x: rx, y: ry, yaw: Math.atan2(cx - rx, cy - ry), pitch: 0 });
    }
    return stops.slice(0, 6);
  }
  function playTour(stops) {
    stopTour();
    stops = stops || (views.length >= 2 ? views.slice() : autoTourStops());
    if (!stops || stops.length < 1) return false;
    if (stops.length < 2 && views.length >= 2) return false;
    tour = { stops, i: -1, phase: 'advance', t0: 0, from: null };
    tourBtn.textContent = 'Stop tour';
    if (viewsPop) { viewsPop.style.display = 'none'; viewsOpen = false; }
    tourRAF = requestAnimationFrame(tourStep);
    return true;
  }
  function tourStep() {
    if (!tour) return;
    const now = tourNow();
    if (tour.phase === 'advance') {
      tour.i += 1;
      if (tour.i >= tour.stops.length) { stopTour(); return; }
      const cam = tour.stops[tour.i];
      if (cam.mode === 'w') {
        if (!walking) enterWalk();
        if (walking) { walkTeleport(cam.x, cam.y, cam.yaw);
          if (cam.pitch != null) wPitch = cam.pitch; }
        tour.baseYaw = cam.yaw != null ? cam.yaw : wYaw;
        tour.phase = 'dwell'; tour.t0 = now;
      } else {
        if (walking) exitWalk();
        tour.from = { yaw, pitch, dist, target: target.slice() };
        tour.to = cam; tour.phase = 'ease'; tour.t0 = now;
      }
      tourRAF = requestAnimationFrame(tourStep); return;
    }
    if (tour.phase === 'ease') {
      const u = Math.min(1, (now - tour.t0) / (TOUR_EASE * 1000));
      const s = u * u * (3 - 2 * u);                 // smoothstep
      const a = tour.from, b = tour.to;
      yaw = a.yaw + shortAngle(a.yaw, b.yaw) * s;
      pitch = a.pitch + (b.pitch - a.pitch) * s;
      dist = a.dist + (b.dist - a.dist) * s;
      const bt = b.target || a.target;
      for (let k = 0; k < 3; k++) target[k] = a.target[k] + (bt[k] - a.target[k]) * s;
      draw();
      if (u >= 1) { tour.phase = 'dwell'; tour.t0 = now; }
      tourRAF = requestAnimationFrame(tourStep); return;
    }
    if (tour.phase === 'dwell') {
      const u = Math.min(1, (now - tour.t0) / (TOUR_DWELL * 1000));
      if (walking && tour.baseYaw != null) {         // slow look pan on a walk leg
        wYaw = tour.baseYaw - TOUR_PAN / 2 + TOUR_PAN * u; draw();
      }
      if (u >= 1) tour.phase = 'advance';
      tourRAF = requestAnimationFrame(tourStep); return;
    }
  }
  // Signed smallest angular delta a->b (radians), so an ease never spins the long
  // way round.
  function shortAngle(a, b) {
    let d = (b - a) % (2 * Math.PI);
    if (d > Math.PI) d -= 2 * Math.PI;
    if (d < -Math.PI) d += 2 * Math.PI;
    return d;
  }

  // --- Views pill + popover (top-right, under Section) ----------------------
  // The Views control: save/rename/delete saved cameras, copy a shareable link for
  // each (and the current camera), and play a guided tour. Sits below the Section
  // pill in the top-right stack. Same pill/popover styling as Sun/Section.
  const viewsWrap = document.createElement('div');
  viewsWrap.style.cssText = 'position:absolute;top:' + (MAP_CSS + 100) + 'px;right:12px;'
    + 'z-index:6;display:flex;flex-direction:column;align-items:flex-end;';
  const viewsBtn = document.createElement('button');
  viewsBtn.type = 'button'; viewsBtn.textContent = 'Views';
  viewsBtn.title = 'Saved camera views + shareable links + guided tour';
  viewsBtn.style.cssText = CTRL_PILL;
  const viewsPop = document.createElement('div');
  viewsPop.style.cssText = POPOVER + 'min-width:210px;max-width:260px;';
  const viewsHd = document.createElement('div');
  viewsHd.style.cssText = POP_HD; viewsHd.textContent = 'Saved views';
  const viewsList = document.createElement('div');   // one row per saved view
  const viewsMsg = document.createElement('div');     // transient status line
  viewsMsg.style.cssText = 'font-size:11px;color:var(--faint, #8791a1);min-height:14px;margin:4px 0;';
  // A readonly fallback field for when the clipboard is unavailable.
  const linkField = document.createElement('input');
  linkField.type = 'text'; linkField.readOnly = true;
  linkField.style.cssText = 'display:none;width:100%;margin:2px 0 6px;padding:4px 6px;'
    + 'font:11px monospace;border:1px solid var(--line, rgba(0,0,0,.14));border-radius:6px;'
    + 'background:rgba(255,255,255,.7);color:var(--ink, #1d2530);';
  const viewsBtnRow = document.createElement('div');
  viewsBtnRow.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-top:2px;';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button'; saveBtn.textContent = 'Save view'; saveBtn.style.cssText = SEG_BTN;
  saveBtn.addEventListener('click', e => { e.preventDefault(); saveView(); });
  const copyCurBtn = document.createElement('button');
  copyCurBtn.type = 'button'; copyCurBtn.textContent = 'Copy link'; copyCurBtn.style.cssText = SEG_BTN;
  copyCurBtn.title = 'Copy a link to the current camera';
  copyCurBtn.addEventListener('click', e => { e.preventDefault(); copyLink(currentCamera()); });
  const tourBtn = document.createElement('button');
  tourBtn.type = 'button'; tourBtn.textContent = 'Auto tour'; tourBtn.style.cssText = SEG_BTN;
  tourBtn.title = 'Play the saved views in order (or an auto room tour)';
  tourBtn.addEventListener('click', e => { e.preventDefault();
    if (tour) { stopTour(); } else { playTour(); } });
  viewsBtnRow.appendChild(saveBtn); viewsBtnRow.appendChild(copyCurBtn);
  viewsBtnRow.appendChild(tourBtn);
  viewsPop.appendChild(viewsHd); viewsPop.appendChild(viewsList);
  viewsPop.appendChild(linkField); viewsPop.appendChild(viewsMsg);
  viewsPop.appendChild(viewsBtnRow);
  viewsWrap.appendChild(viewsBtn); viewsWrap.appendChild(viewsPop);
  host.appendChild(viewsWrap);
  let viewsOpen = false;
  viewsBtn.addEventListener('click', e => {
    e.preventDefault(); viewsOpen = !viewsOpen;
    viewsPop.style.display = viewsOpen ? '' : 'none';
    if (viewsOpen) renderViews();
  });
  let viewsMsgTimer = null;
  function flashViewMsg(t) {
    viewsMsg.textContent = t;
    if (viewsMsgTimer) clearTimeout(viewsMsgTimer);
    viewsMsgTimer = setTimeout(() => { viewsMsg.textContent = ''; }, 2600);
  }
  // Rebuild the saved-views list: each row is [name -> restore][rename][link][x].
  function renderViews() {
    viewsList.textContent = '';
    if (!views.length) {
      const e = document.createElement('div');
      e.style.cssText = 'font-size:11px;color:var(--faint, #8791a1);padding:2px 0 4px;';
      e.textContent = 'No saved views yet.';
      viewsList.appendChild(e);
    }
    views.forEach((v, i) => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:4px;padding:2px 0;';
      const nameBtn = document.createElement('button');
      nameBtn.type = 'button'; nameBtn.textContent = v.name;
      nameBtn.title = 'Restore this view';
      nameBtn.style.cssText = 'flex:1;text-align:left;font:600 12px -apple-system,'
        + 'BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:3px 6px;'
        + 'border-radius:6px;border:1px solid var(--line, rgba(0,0,0,.10));cursor:pointer;'
        + 'background:var(--panel, rgba(255,255,255,.6));color:var(--ink, #1d2530);overflow:hidden;'
        + 'text-overflow:ellipsis;white-space:nowrap;'
        + (v.mode === 'w' ? '' : '');
      nameBtn.addEventListener('click', e => { e.preventDefault(); restoreView(v); });
      const renBtn = miniIcon('Rename', 'aa');
      renBtn.addEventListener('click', e => { e.preventDefault(); renameView(i, row, v); });
      const linkBtn = miniIcon('Copy link', 'link');
      linkBtn.addEventListener('click', e => { e.preventDefault(); copyLink(v); });
      const delBtn = miniIcon('Delete', 'x');
      delBtn.addEventListener('click', e => { e.preventDefault();
        views.splice(i, 1); renderViews(); });
      row.appendChild(nameBtn); row.appendChild(renBtn);
      row.appendChild(linkBtn); row.appendChild(delBtn);
      viewsList.appendChild(row);
    });
    tourBtn.textContent = tour ? 'Stop tour'
      : (views.length >= 2 ? 'Play tour' : 'Auto tour');
    tourBtn.disabled = false;
  }
  // A tiny square icon button (rename/link/delete). ASCII glyphs only.
  function miniIcon(title, kind) {
    const b = document.createElement('button');
    b.type = 'button'; b.title = title;
    b.textContent = kind === 'x' ? 'x' : (kind === 'link' ? '@' : 'Aa');
    b.style.cssText = 'flex:0 0 auto;font:600 11px -apple-system,BlinkMacSystemFont,'
      + '"Segoe UI",Helvetica,Arial,sans-serif;padding:3px 7px;border-radius:6px;'
      + 'border:1px solid rgba(0,0,0,.12);cursor:pointer;'
      + 'background:var(--panel, rgba(255,255,255,.6));color:var(--muted, #566072);';
    return b;
  }
  // Inline-rename: swap the row's name button for a text input; Enter/blur commits.
  function renameView(i, row, v) {
    const inp = document.createElement('input');
    inp.type = 'text'; inp.value = v.name;
    inp.style.cssText = 'flex:1;font:600 12px -apple-system,BlinkMacSystemFont,'
      + '"Segoe UI",Helvetica,Arial,sans-serif;padding:3px 6px;border-radius:6px;'
      + 'border:1px solid rgba(209,135,63,.7);background:rgba(255,255,255,.9);'
      + 'color:var(--ink, #1d2530);min-width:0;';
    const commit = () => { const t = inp.value.trim(); if (t) v.name = t; renderViews(); };
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      else if (e.key === 'Escape') { e.preventDefault(); renderViews(); }
      e.stopPropagation();
    });
    inp.addEventListener('blur', commit);
    row.replaceChild(inp, row.firstChild);
    try { inp.focus(); inp.select(); } catch (e) {}
  }

  // --- Measure pill (bottom-right, both modes) ------------------------------
  // Sits left of the Walk pill along the bottom-right so it's reachable in orbit and
  // walk. Toggling arms/disarms measure; the pill highlights while armed.
  const measBtn = document.createElement('button');
  measBtn.type = 'button'; measBtn.textContent = 'Measure';
  measBtn.title = 'Measure: click two points for a distance in feet (Esc clears)';
  measBtn.style.cssText = CTRL_PILL + 'position:absolute;bottom:52px;right:14px;z-index:6;';
  measBtn.addEventListener('click', e => { e.preventDefault(); setMeasure(); });
  host.appendChild(measBtn);

  // --- canvas click routing (measure point / identify) ----------------------
  // A plain click (not a drag) is distinguished by a small movement threshold so it
  // never fights orbit dragging or walk look. When measuring, a click adds a measure
  // point; otherwise (measure off, no tour, not walking) it identifies the surface.
  let clkStart = null;                // {x, y} at pointerdown, or null
  const CLICK_SLOP = 5;               // px of movement still counted as a click
  canvas.addEventListener('pointerdown', e => {
    clkStart = { x: e.clientX, y: e.clientY };
  }, true);
  canvas.addEventListener('pointerup', e => {
    const st = clkStart; clkStart = null;
    if (!st) return;
    if (Math.hypot(e.clientX - st.x, e.clientY - st.y) > CLICK_SLOP) return;  // a drag
    const rect = canvas.getBoundingClientRect();
    const px = e.clientX - rect.left, py = e.clientY - rect.top;
    if (measuring) { addMeasurePoint(px, py); return; }
    if (tour || walking) return;      // suppress identify mid-tour and while walking
    const hit = pick(px, py);
    if (hit) showIdentify(identityOf(hit));
    // A room floor is a selection, reported to the host (the playground links it
    // to the plan, the source line and the inspector). Other surfaces just identify.
    if (hit && onSelectCb && (hit.name || '').startsWith('room:')) onSelectCb(hit.name.slice(5));
  }, true);

  // Esc clears/disarms measure and stops a tour (in addition to walk's own Esc,
  // which exits walk). Registered once, capturing, so it works in both cameras.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (tour) { stopTour(); }
    if (measuring) { setMeasure(false); }
  }, true);

  // While measuring, keep the overlay projection in sync as the camera moves; hook
  // the existing draw by redrawing the overlay right after each draw() when active.
  const _drawBase = draw;
  draw = function () { _drawBase(); if (measuring && measurePts.length) drawOverlay(); };

  // Any user input stops a running tour (per the spec). A single capturing listener
  // on the host covers pointer + key; the tour's own key/pointer handlers still run.
  function stopTourOnInput() { if (tour) stopTour(); }
  document.addEventListener('pointerdown', stopTourOnInput, true);
  document.addEventListener('wheel', stopTourOnInput, true);
  document.addEventListener('keydown', e => {
    if (tour && e.key !== 'Escape') stopTour();     // Esc already handled above
  }, true);

  // On mount, apply a "#v=" camera from location.hash after the first setScene. A
  // walk state enters walk mode without pointer lock (like a touch entry). Guarded:
  // read once here, never written except on an explicit Copy-link action.
  function applyHashOnce() {
    let h = '';
    try { h = (typeof location !== 'undefined' && location.hash) || ''; } catch (e) { h = ''; }
    const cam = parseHash(h);
    if (!cam) return;
    // Hash-restored walk entry must not request pointer lock; temporarily latch
    // coarse so enterWalk skips it, matching a touch entry, then restore the flag.
    if (cam.mode === 'w') {
      const wasCoarse = coarse; coarse = true;
      restoreView(cam);
      coarse = wasCoarse;
    } else {
      restoreView(cam);
    }
  }
  // Defer to after the host's initial setScene: wrap setScene so the first call
  // frames the scene, then applies the hash camera on top.
  const _setSceneBase = setScene;
  let hashApplied = false;
  setScene = function (s) {
    _setSceneBase(s);
    if (!hashApplied) { hashApplied = true; applyHashOnce(); }
  };

  // Linked selection API: highlight a room's floor by its DSL id (null clears),
  // and register the callback a room-floor click reports the id to.
  function setHighlight(id) { highlightId = id || null; if (hasScene) draw(); }
  function onSelect(fn) { onSelectCb = typeof fn === 'function' ? fn : null; }

  return { setScene, resize, draw, enterWalk, exitWalk, walkState, walkTeleport,
    walkStick, cycleEye, toggleFurniture, toggleMinimap,
    setSun, setSection, setLevel, sunState, setGround,
    saveView, restoreView, playTour, stopTour, setMeasure, pick,
    parseHash, encodeHash, currentCamera, autoTourStops,
    setHighlight, onSelect };
}
"""


# The whole viewer: HTML + CSS + the shared inline renderer. Kept as one template
# so the output is a single file that runs offline. `{...}` placeholders are filled
# by str.format; literal braces in the CSS are doubled. The `{renderer}` value is
# RENDERER_JS, injected verbatim (its braces are not reprocessed by format).
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — barndsl 3D</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }}
  body {{ background: #eef1f4; color: #1d2530; }}
  #canvas {{ position: fixed; inset: 0; width: 100%; height: 100%; display: block;
    touch-action: none; cursor: grab; }}
  #canvas:active {{ cursor: grabbing; }}
  header {{ position: fixed; top: 0; left: 0; right: 0; padding: 12px 18px;
    background: linear-gradient(180deg, rgba(255,255,255,.92), rgba(255,255,255,.0));
    pointer-events: none; }}
  header h1 {{ margin: 0; font-size: 18px; font-weight: 650; letter-spacing: .2px; }}
  header .stats {{ margin-top: 3px; font-size: 12.5px; color: #566072; }}
  #panel {{ position: fixed; top: 64px; left: 18px; background: rgba(255,255,255,.9);
    border: 1px solid rgba(0,0,0,.08); border-radius: 10px; padding: 10px 12px;
    box-shadow: 0 4px 18px rgba(20,30,50,.10); font-size: 13px; min-width: 132px;
    backdrop-filter: blur(6px); }}
  #panel .hd {{ font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
    color: #8791a1; margin-bottom: 6px; }}
  #panel label {{ display: flex; align-items: center; gap: 8px; padding: 3px 0;
    cursor: pointer; user-select: none; }}
  #panel input {{ accent-color: #d1873f; }}
  #hint {{ position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%);
    font-size: 11.5px; color: #7a8494; background: rgba(255,255,255,.75);
    padding: 5px 12px; border-radius: 20px; pointer-events: none; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #171b21; color: #e6ebf2; }}
    header {{ background: linear-gradient(180deg, rgba(20,24,30,.92), rgba(20,24,30,0)); }}
    header .stats {{ color: #9aa4b4; }}
    #panel {{ background: rgba(30,35,43,.9); border-color: rgba(255,255,255,.08);
      box-shadow: 0 4px 18px rgba(0,0,0,.4); }}
    #hint {{ background: rgba(30,35,43,.7); color: #9aa4b4; }}
  }}
</style>
</head>
<body>
<canvas id="canvas"></canvas>
<header><h1>{title}</h1><div class="stats">{stats}</div></header>
<div id="panel"><div class="hd">Layers</div><div id="toggles"></div></div>
<div id="hint">drag to orbit &middot; right-drag / shift-drag to pan &middot; scroll to zoom &middot; Walk (or Enter) to step inside</div>
<script id="scene" type="application/json">{payload}</script>
<script>{renderer}</script>
<script>
const LABELS = {labels};
const SCENE = JSON.parse(document.getElementById('scene').textContent);
const ctrl = mountScene(document.getElementById('canvas'), LABELS,
  document.getElementById('toggles'));
if (ctrl) ctrl.setScene(SCENE);
</script>
</body>
</html>
"""
