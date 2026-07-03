"""A single self-contained HTML 3D viewer for a compiled plan.

``write_viewer(plan, path)`` produces **one** HTML file — no network, no CDN, no
build step — that opens the plan's 3D model by double-clicking it. The scene is
the same box/quad lowering :mod:`barndsl.gltf` builds (via :func:`build_scene`),
serialised into a compact inline JSON blob, and rendered by a small inline WebGL
renderer: orbit / pan / zoom, one directional light with Lambert shading, and a
layer-toggle panel (walls, roof, frame, floors, porches, openings, stairs — the
roof toggle lets you look inside). No textures, no external dependencies.

The renderer is inline WebGL rather than a Three.js CDN import precisely because
the geometry is trivial — axis-aligned boxes and a handful of sloped quads — so a
~one-directional-light Lambert shader is all it takes, and the file stays offline
and self-contained (the property the design doc asks for). Styling is light and
clean, consistent with a design tool; the plan title and a few key metrics
(square footage, bed/bath) sit in a header.

The embedded geometry is in the same glTF y-up frame the exporter uses (plan
``(x, y, z)`` → ``(x, z, -y)``), so the viewer and any external glTF viewer agree.
"""

from __future__ import annotations

import json

from .elements import Barndominium
from .gltf import Scene, _to_gltf, build_scene, hex_to_linear

#: Human labels for the layer toggles, in display order.
_LAYER_LABELS = {
    "walls": "Walls",
    "roof": "Roof",
    "frame": "Frame",
    "floors": "Floors",
    "porches": "Porches",
    "openings": "Openings",
    "stairs": "Stairs",
}


def _scene_json(scene: Scene) -> dict:
    """The scene as inline-renderable JSON: one entry per non-empty node.

    Vertices/normals are pre-transformed into the glTF y-up frame and flattened;
    colours are linear-space RGB so the shader can light them directly.
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
        nodes.append(
            {
                "name": n.name,
                "layer": n.layer,
                "color": hex_to_linear(n.material)[:3],
                "positions": [round(x, 4) for x in verts],
                "normals": [round(x, 4) for x in norms],
                "indices": n.indices,
            }
        )
    layers = [ly for ly in Scene.LAYERS if any(nd["layer"] == ly for nd in nodes)]
    return {"nodes": nodes, "layers": layers}


def viewer_html(plan: Barndominium, scene: Scene | None = None) -> str:
    """Return the self-contained viewer HTML document for ``plan``."""
    scene = scene or build_scene(plan)
    data = _scene_json(scene)
    m = plan.metrics()
    stats = (
        f"{m['footprint_sqft']:.0f} sq ft &middot; "
        f"{int(m['bedroom_count'])} bed / {m['bathroom_count']:g} bath &middot; "
        f"{m['interior_sqft']:.0f} sq ft interior"
    )
    title = plan.name
    payload = json.dumps(data, separators=(",", ":"))
    labels = json.dumps(_LAYER_LABELS)
    return _TEMPLATE.format(
        title=_esc(title), stats=stats, payload=payload, labels=labels
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


# The whole viewer: HTML + CSS + an inline WebGL renderer. Kept as one template so
# the output is a single file that runs offline. `{...}` placeholders are filled
# by str.format; literal braces in the CSS/JS are doubled.
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
<script>
const LABELS = {labels};
const SCENE = JSON.parse(document.getElementById('scene').textContent);
const canvas = document.getElementById('canvas');
const gl = canvas.getContext('webgl', {{antialias: true}});
if (!gl) {{
  document.body.innerHTML = '<p style="padding:2em;font:16px sans-serif">'
    + 'This browser could not create a WebGL context, so the 3D view is unavailable.</p>';
  throw new Error('no webgl');
}}

// --- minimal mat4 ----------------------------------------------------------
function ident() {{ return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]; }}
function mul(a,b) {{ const o=new Array(16);
  for (let r=0;r<4;r++) for (let c=0;c<4;c++) {{ let s=0;
    for (let k=0;k<4;k++) s+=a[k*4+c]*b[r*4+k]; o[r*4+c]=s; }} return o; }}
function perspective(fovy, aspect, n, f) {{ const t=1/Math.tan(fovy/2);
  return [t/aspect,0,0,0, 0,t,0,0, 0,0,(f+n)/(n-f),-1, 0,0,(2*f*n)/(n-f),0]; }}
function lookAt(e, c, up) {{
  const z=norm(sub(e,c)), x=norm(cross(up,z)), y=cross(z,x);
  return [x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0,
    -dot(x,e),-dot(y,e),-dot(z,e),1]; }}
function sub(a,b){{return [a[0]-b[0],a[1]-b[1],a[2]-b[2]];}}
function cross(a,b){{return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];}}
function dot(a,b){{return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];}}
function norm(a){{const l=Math.hypot(a[0],a[1],a[2])||1;return [a[0]/l,a[1]/l,a[2]/l];}}

// --- shader ----------------------------------------------------------------
const vs = `attribute vec3 aPos; attribute vec3 aNorm;
  uniform mat4 uMVP; uniform mat4 uModel; varying vec3 vN; varying vec3 vP;
  void main(){{ vN=aNorm; vP=aPos; gl_Position=uMVP*vec4(aPos,1.0); }}`;
const fs = `precision mediump float; varying vec3 vN; varying vec3 vP;
  uniform vec3 uColor;
  void main(){{
    vec3 n=normalize(vN);
    vec3 L=normalize(vec3(0.4,0.9,0.5));
    float d=max(dot(n,L),0.0)*0.75+0.30;
    // gentle two-tone sky/ground ambient so unlit faces stay readable
    float amb=0.5+0.5*n.y; d+=amb*0.12;
    vec3 c=uColor*min(d,1.15);
    gl_FragColor=vec4(pow(c, vec3(1.0/2.2)), 1.0);
  }}`;
function compile(type, src){{ const s=gl.createShader(type); gl.shaderSource(s,src);
  gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw gl.getShaderInfoLog(s);
  return s; }}
const prog = gl.createProgram();
gl.attachShader(prog, compile(gl.VERTEX_SHADER, vs));
gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fs));
gl.linkProgram(prog); gl.useProgram(prog);
const aPos=gl.getAttribLocation(prog,'aPos'), aNorm=gl.getAttribLocation(prog,'aNorm');
const uMVP=gl.getUniformLocation(prog,'uMVP'), uColor=gl.getUniformLocation(prog,'uColor');

// --- upload geometry, compute bounds --------------------------------------
const bmin=[1e9,1e9,1e9], bmax=[-1e9,-1e9,-1e9];
const nodes = SCENE.nodes.map(n => {{
  const pos=new Float32Array(n.positions), nrm=new Float32Array(n.normals);
  for(let i=0;i<pos.length;i+=3) for(let k=0;k<3;k++){{
    bmin[k]=Math.min(bmin[k],pos[i+k]); bmax[k]=Math.max(bmax[k],pos[i+k]); }}
  const pb=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,pb);
  gl.bufferData(gl.ARRAY_BUFFER,pos,gl.STATIC_DRAW);
  const nb=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,nb);
  gl.bufferData(gl.ARRAY_BUFFER,nrm,gl.STATIC_DRAW);
  const ib=gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,ib);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,new Uint32Array(n.indices),gl.STATIC_DRAW);
  const ext=gl.getExtension('OES_element_index_uint');
  return {{layer:n.layer, color:n.color, pb, nb, ib, count:n.indices.length, uint:!!ext}};
}});
const center=[(bmin[0]+bmax[0])/2,(bmin[1]+bmax[1])/2,(bmin[2]+bmax[2])/2];
const radius=Math.max(1, Math.hypot(bmax[0]-bmin[0],bmax[1]-bmin[1],bmax[2]-bmin[2])/2);

// --- camera / interaction --------------------------------------------------
let yaw=-0.7, pitch=0.6, dist=radius*2.6;
const target=[center[0],center[1],center[2]];
const hidden={{}};
let dragging=false, panning=false, lx=0, ly=0;
canvas.addEventListener('pointerdown', e=>{{ dragging=true;
  panning = e.button===2 || e.shiftKey; lx=e.clientX; ly=e.clientY;
  canvas.setPointerCapture(e.pointerId); }});
canvas.addEventListener('pointerup', e=>{{ dragging=false; }});
canvas.addEventListener('contextmenu', e=>e.preventDefault());
canvas.addEventListener('pointermove', e=>{{ if(!dragging) return;
  const dx=e.clientX-lx, dy=e.clientY-ly; lx=e.clientX; ly=e.clientY;
  if(panning){{ const s=dist*0.0016;
    const cy=Math.cos(yaw), sy=Math.sin(yaw);
    target[0]-= (dx*cy)*s; target[2]-=(dx*sy)*s; target[1]+=dy*s;
  }} else {{ yaw-=dx*0.008; pitch=Math.max(-1.5,Math.min(1.5,pitch-dy*0.008)); }}
  draw(); }});
canvas.addEventListener('wheel', e=>{{ e.preventDefault();
  dist=Math.max(radius*0.3, Math.min(radius*12, dist*(1+Math.sign(e.deltaY)*0.1)));
  draw(); }}, {{passive:false}});

// --- layer toggles ---------------------------------------------------------
const toggles=document.getElementById('toggles');
SCENE.layers.forEach(ly => {{
  const lab=document.createElement('label'); const cb=document.createElement('input');
  cb.type='checkbox'; cb.checked=true;
  cb.onchange=()=>{{ hidden[ly]=!cb.checked; draw(); }};
  lab.appendChild(cb); lab.appendChild(document.createTextNode(LABELS[ly]||ly));
  toggles.appendChild(lab);
}});

// --- render loop -----------------------------------------------------------
function resize(){{ const dpr=Math.min(window.devicePixelRatio||1,2);
  canvas.width=canvas.clientWidth*dpr; canvas.height=canvas.clientHeight*dpr;
  gl.viewport(0,0,canvas.width,canvas.height); }}
window.addEventListener('resize', ()=>{{resize(); draw();}});
gl.enable(gl.DEPTH_TEST);
function draw(){{
  gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  const eye=[
    target[0]+dist*Math.cos(pitch)*Math.sin(yaw),
    target[1]+dist*Math.sin(pitch),
    target[2]+dist*Math.cos(pitch)*Math.cos(yaw)];
  const proj=perspective(0.9, canvas.width/canvas.height, radius*0.05, radius*40);
  const view=lookAt(eye,target,[0,1,0]);
  const vp=mul(proj,view);
  for(const nd of nodes){{
    if(hidden[nd.layer]) continue;
    gl.uniformMatrix4fv(uMVP,false,new Float32Array(vp));
    gl.uniform3fv(uColor,nd.color);
    gl.bindBuffer(gl.ARRAY_BUFFER,nd.pb);
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,nd.nb);
    gl.enableVertexAttribArray(aNorm); gl.vertexAttribPointer(aNorm,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,nd.ib);
    gl.drawElements(gl.TRIANGLES,nd.count,gl.UNSIGNED_INT,0);
  }}
}}
resize(); draw();
</script>
</body>
</html>
"""
