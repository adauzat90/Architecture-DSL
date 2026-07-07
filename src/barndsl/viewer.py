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

#: Human labels for the layer toggles, in display order.
_LAYER_LABELS = {
    "walls": "Walls",
    "roof": "Roof",
    "frame": "Frame",
    "floors": "Floors",
    "porches": "Porches",
    "openings": "Openings",
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
        nodes.append(entry)
    layers = [ly for ly in Scene.LAYERS if any(nd["layer"] == ly for nd in nodes)]
    return {"nodes": nodes, "layers": layers, "walk": _walk_block(scene)}


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
    + ' varying vec3 vN; varying vec3 vWorld;'
    + ' void main(){ vec4 wp=uModel*vec4(aPos,1.0); vWorld=wp.xyz;'
    + '   vN=mat3(uModel)*aNorm; gl_Position=uMVP*wp; }';
  const fs = 'precision mediump float;'
    + ' varying vec3 vN; varying vec3 vWorld;'
    + ' uniform vec3 uColor; uniform vec3 uEye;'
    + ' uniform float uRough; uniform float uMetal;'
    + ' uniform float uPatScale; uniform float uHasTex; uniform sampler2D uTex;'
    + ' uniform float uFill;'
    + ' void main(){'
    + '   vec3 n=normalize(vN); vec3 an=abs(n); vec2 uv;'
    // Project world coords onto the plane facing the dominant axis. Vertical
    // surfaces (walls, roof faces) keep world-up as V so ribs/courses read upright.
    + '   if(an.x>=an.y&&an.x>=an.z) uv=vec2(vWorld.z,vWorld.y);'
    + '   else if(an.z>=an.x&&an.z>=an.y) uv=vec2(vWorld.x,vWorld.y);'
    + '   else uv=vec2(vWorld.x,vWorld.z);'
    + '   float detail=1.0;'
    + '   if(uHasTex>0.5) detail=texture2D(uTex, uv/uPatScale).r;'
    + '   vec3 albedo=uColor*(0.4+0.6*detail);'
    + '   vec3 L=normalize(vec3(0.4,0.9,0.5));'
    + '   vec3 V=normalize(uEye-vWorld); vec3 H=normalize(L+V);'
    + '   float diff=max(dot(n,L),0.0);'
    + '   float amb=0.28+0.22*(0.5+0.5*n.y);'
    // Metals carry little diffuse; fade it out as metallic rises.
    + '   vec3 col=albedo*(amb+diff*0.72)*(1.0-0.65*uMetal);'
    // Eye-attached fill (walk mode only; uFill=0 in orbit is a true no-op): lights
    // surfaces facing the camera so first-person interiors don't read flat and
    // dim. A gentle distance falloff keeps far walls from glowing.
    + '   if(uFill>0.0){ float fd=length(uEye-vWorld);'
    + '     float fall=1.0/(1.0+0.04*fd);'
    + '     col+=albedo*uFill*max(dot(n,V),0.0)*fall; }'
    + '   float sh=mix(10.0,90.0,1.0-uRough);'
    + '   float spec=pow(max(dot(n,H),0.0),sh);'
    + '   vec3 specCol=mix(vec3(0.05),uColor,uMetal);'
    + '   col+=specCol*spec*(0.25+0.75*uMetal);'
    + '   gl_FragColor=vec4(pow(min(col,vec3(1.4)), vec3(1.0/2.2)), 1.0);'
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
  gl.getExtension('OES_element_index_uint');
  gl.enable(gl.DEPTH_TEST);

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

  // --- state (survives setScene so the view/toggles persist on recompile) ---
  let nodes = [];
  let center = [0, 0, 0], radius = 1;
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
  let wElev = 0;                   // current floor elevation (eased for smooth steps)
  let wYaw = 0, wPitch = 0;        // look angles (radians)
  const keys = {};                 // held movement keys {f,b,l,r,run}
  let walkRAF = null, walkLast = 0;
  let usePointerLock = false, walkDrag = false, wpx = 0, wpy = 0;
  let savedCam = null;             // orbit camera snapshot to restore on exit
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
      return { layer: n.layer, color: n.color,
        rough: n.roughness == null ? 0.8 : n.roughness,
        metal: n.metallic == null ? 0.0 : n.metallic,
        patScale: n.patternScale || 1.0, tex: patternTexture(pattern),
        door: n.door || null, open: 0, target: 0, pinned: false,
        pb, nb, ib, count: n.indices.length };
    });
    if (nodes.length) {
      center = [(bmin[0] + bmax[0]) / 2, (bmin[1] + bmax[1]) / 2, (bmin[2] + bmax[2]) / 2];
      radius = Math.max(1, Math.hypot(bmax[0] - bmin[0], bmax[1] - bmin[1],
        bmax[2] - bmin[2]) / 2);
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
    updateWalkUI();
    buildToggles(scene.layers || []);
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

  function draw() {
    if (!hasScene) return;
    resize();
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const aspect = canvas.width / Math.max(1, canvas.height);
    let eye, view, proj;
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
      gl.uniformMatrix4fv(uMVP, false, new Float32Array(mul(proj, view)));
      gl.uniform3fv(uEye, new Float32Array(eye));
      gl.uniform1f(uFill, WALK_FILL);   // eye-attached interior fill (walk only)
      drawNodes();
      return;
    }
    eye = [
      target[0] + dist * Math.cos(pitch) * Math.sin(yaw),
      target[1] + dist * Math.sin(pitch),
      target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
    proj = perspective(0.9, aspect, radius * 0.05, radius * 40);
    view = lookAt(eye, target, [0, 1, 0]);
    gl.uniformMatrix4fv(uMVP, false, new Float32Array(mul(proj, view)));
    gl.uniform3fv(uEye, new Float32Array(eye));
    gl.uniform1f(uFill, 0.0);           // orbit: fill is a true no-op (identical pixels)
    drawNodes();
  }

  // The layer-respecting node draw, shared by the orbit and walk cameras (the
  // MVP/eye uniforms are set by the caller). Hidden layers (e.g. the roof toggled
  // off to look inside) are skipped in walk mode too.
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

  function drawNodes() {
    for (const nd of nodes) {
      if (hidden[nd.layer]) continue;
      gl.uniformMatrix4fv(uModel, false, new Float32Array(
        (walking && nd.door) ? doorModelMatrix(nd) : IDENTITY));
      gl.uniform3fv(uColor, nd.color);
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
    + 'padding:7px 15px;border-radius:20px;border:1px solid rgba(0,0,0,.14);'
    + 'background:rgba(255,255,255,.88);color:#1d2530;cursor:pointer;'
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

  // --- walk-mode control pills (eye height + furniture), shown only in walk ---
  // Same pill look as walkBtn (see its cssText); they sit just left of the
  // Walk/Exit pill along the bottom-right so touch users can reach them, and stay
  // hidden until walk mode is entered so orbit view is uncluttered.
  const PILL = 'position:absolute;bottom:12px;z-index:6;'
    + 'font:600 12px -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;'
    + 'padding:7px 13px;border-radius:20px;border:1px solid rgba(0,0,0,.14);'
    + 'background:rgba(255,255,255,.88);color:#1d2530;cursor:pointer;'
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

  // One movement tick: WASD relative to the look yaw, slide off walls, then ease
  // the floor height under the (possibly stair-ramped) position.
  function updateMove(dt) {
    const fx = Math.sin(wYaw), fy = Math.cos(wYaw);  // forward (plan)
    const rx = fy, ry = -fx;                          // right (plan)
    let mx = 0, my = 0;
    if (keys.f) { mx += fx; my += fy; } if (keys.b) { mx -= fx; my -= fy; }
    if (keys.r) { mx += rx; my += ry; } if (keys.l) { mx -= rx; my -= ry; }
    const ml = Math.hypot(mx, my);
    if (ml > 1e-6) {
      const spd = walkSpeed * (keys.run ? 2.5 : 1);
      let nx = wpos[0] + (mx / ml) * spd * dt, ny = wpos[1] + (my / ml) * spd * dt;
      const slid = collide(nx, ny); nx = slid[0]; ny = slid[1];
      // Don't let a player walk off the slab into the void upstairs; grade
      // (elevation ~0) is open so you can step out an exterior door onto a porch.
      const ft = floorTarget(nx, ny);
      if (ft == null && wElev > 0.08) { /* blocked: keep current position */ }
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
      eyeHeight: walkEye, eyeTarget: walkEyeTarget, eyeLabel: EYE_PRESETS[eyeIdx].label,
      furniture, coarse, minimap: minimapOn,
      room: rm ? rm.name : null, roomLabel: rm ? rm.label : null };
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

  return { setScene, resize, draw, enterWalk, exitWalk, walkState, walkTeleport,
    walkStick, cycleEye, toggleFurniture, toggleMinimap };
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
