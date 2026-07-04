"""A single self-contained HTML 3D viewer for a compiled plan.

``write_viewer(plan, path)`` produces **one** HTML file — no network, no CDN, no
build step — that opens the plan's 3D model by double-clicking it. The scene is
the same box/quad lowering :mod:`barndsl.gltf` builds (via :func:`build_scene`),
serialised into a compact inline JSON blob, and rendered by a small inline WebGL
renderer: orbit / pan / zoom, a directional light with a sky-fill term and a
roughness/metallic specular, procedural surface textures, and a layer-toggle panel
(walls, roof, frame, floors, porches, openings, stairs — the roof toggle lets you
look inside). No external dependencies.

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


def scene_json(scene: Scene) -> dict:
    """The scene as inline-renderable JSON: one entry per non-empty node.

    Vertices/normals are pre-transformed into the glTF y-up frame and flattened;
    colours are linear-space RGB so the shader can light them directly. Each node
    also carries its material's ``roughness``/``metallic`` factors and a procedural
    ``pattern`` (with ``patternScale`` in feet) the shared renderer turns into a
    triplanar-mapped texture. This is the exact blob the viewer embeds and the
    playground returns, so the shared :data:`RENDERER_JS` renderer draws both from
    one code path.
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
        nodes.append(
            {
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
        )
    layers = [ly for ly in Scene.LAYERS if any(nd["layer"] == ly for nd in nodes)]
    return {"nodes": nodes, "layers": layers}


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

  // --- shader ---------------------------------------------------------------
  // World-space position and normal go to the fragment stage; the base colour is
  // modulated there by a triplanar-mapped procedural texture (chosen per dominant
  // world-normal axis) and lit with a diffuse + sky-fill term plus a
  // roughness/metallic-driven specular, so a metal roof reads as metal.
  const vs = 'attribute vec3 aPos; attribute vec3 aNorm;'
    + ' uniform mat4 uMVP; varying vec3 vN; varying vec3 vWorld;'
    + ' void main(){ vN=aNorm; vWorld=aPos; gl_Position=uMVP*vec4(aPos,1.0); }';
  const fs = 'precision mediump float;'
    + ' varying vec3 vN; varying vec3 vWorld;'
    + ' uniform vec3 uColor; uniform vec3 uEye;'
    + ' uniform float uRough; uniform float uMetal;'
    + ' uniform float uPatScale; uniform float uHasTex; uniform sampler2D uTex;'
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
  const uColor = gl.getUniformLocation(prog, 'uColor');
  const uEye = gl.getUniformLocation(prog, 'uEye');
  const uRough = gl.getUniformLocation(prog, 'uRough');
  const uMetal = gl.getUniformLocation(prog, 'uMetal');
  const uPatScale = gl.getUniformLocation(prog, 'uPatScale');
  const uHasTex = gl.getUniformLocation(prog, 'uHasTex');
  const uTex = gl.getUniformLocation(prog, 'uTex');
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
      return { layer: n.layer, color: n.color,
        rough: n.roughness == null ? 0.8 : n.roughness,
        metal: n.metallic == null ? 0.0 : n.metallic,
        patScale: n.patternScale || 1.0, tex: patternTexture(pattern),
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
  canvas.addEventListener('pointerdown', e => { dragging = true;
    panning = e.button === 2 || e.shiftKey; px = e.clientX; py = e.clientY;
    canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointerup', () => { dragging = false; });
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('pointermove', e => { if (!dragging) return;
    const dx = e.clientX - px, dy = e.clientY - py; px = e.clientX; py = e.clientY;
    if (panning) { const s = dist * 0.0016;
      const cy = Math.cos(yaw), sy = Math.sin(yaw);
      target[0] -= (dx * cy) * s; target[2] -= (dx * sy) * s; target[1] += dy * s;
    } else { yaw -= dx * 0.008; pitch = Math.max(-1.5, Math.min(1.5, pitch - dy * 0.008)); }
    draw(); });
  canvas.addEventListener('wheel', e => { e.preventDefault();
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
    const eye = [
      target[0] + dist * Math.cos(pitch) * Math.sin(yaw),
      target[1] + dist * Math.sin(pitch),
      target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
    const proj = perspective(0.9, canvas.width / Math.max(1, canvas.height),
      radius * 0.05, radius * 40);
    const view = lookAt(eye, target, [0, 1, 0]);
    const vp = mul(proj, view);
    gl.uniformMatrix4fv(uMVP, false, new Float32Array(vp));
    gl.uniform3fv(uEye, new Float32Array(eye));
    for (const nd of nodes) {
      if (hidden[nd.layer]) continue;
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

  return { setScene, resize, draw };
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
<div id="hint">drag to orbit &middot; right-drag / shift-drag to pan &middot; scroll to zoom</div>
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
