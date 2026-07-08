// Headless probe harness for RENDERER_JS.
// Reads the renderer source from argv[2], a mode flag from argv[3]
// ("ok" = FBO stub reports COMPLETE, "fbfail" = FBO stub reports incomplete),
// evaluates mountScene, drives one draw, and prints a JSON report on stdout.
'use strict';
const fs = require('fs');
const rendererSrc = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3] || 'ok';

// ---- GL constant + stub -----------------------------------------------------
const GLC = {
  DEPTH_TEST: 1, BLEND: 2, CULL_FACE: 3, POLYGON_OFFSET_FILL: 4, STENCIL_TEST: 5,
  COLOR_BUFFER_BIT: 0x4000, DEPTH_BUFFER_BIT: 0x100, STENCIL_BUFFER_BIT: 0x400,
  TRIANGLES: 4, FLOAT: 0x1406, UNSIGNED_INT: 0x1405, UNSIGNED_BYTE: 0x1401,
  UNSIGNED_SHORT: 0x1403, ARRAY_BUFFER: 0x8892, ELEMENT_ARRAY_BUFFER: 0x8893,
  STATIC_DRAW: 0x88E4, TEXTURE_2D: 0x0DE1, TEXTURE0: 0x84C0, TEXTURE1: 0x84C1,
  RGBA: 0x1908, RGBA4: 0x8056, DEPTH_COMPONENT: 0x1902, DEPTH_COMPONENT16: 0x81A5,
  TEXTURE_WRAP_S: 1, TEXTURE_WRAP_T: 2, TEXTURE_MIN_FILTER: 3, TEXTURE_MAG_FILTER: 4,
  REPEAT: 5, CLAMP_TO_EDGE: 6, LINEAR: 7, NEAREST: 8, LINEAR_MIPMAP_LINEAR: 9,
  VERTEX_SHADER: 0x8B31, FRAGMENT_SHADER: 0x8B30, COMPILE_STATUS: 0x8B81,
  LINK_STATUS: 0x8B82, FRAMEBUFFER: 0x8D40, RENDERBUFFER: 0x8D41,
  COLOR_ATTACHMENT0: 0x8CE0, DEPTH_ATTACHMENT: 0x8D00,
  FRAMEBUFFER_COMPLETE: 0x8CD5, FRAMEBUFFER_UNSUPPORTED: 0x8CDD,
  KEEP: 1, INCR: 2, EQUAL: 3, SRC_ALPHA: 1, ONE_MINUS_SRC_ALPHA: 2,
};

let uid = 0;
function tag(kind) { return { __id: ++uid, __kind: kind }; }

const draws = [];       // {program, fb, blend, depthMask, viewport}
let curProgram = null;
let curFB = null;
let blend = false;
let depthMask = true;
let curViewport = [0, 0, 0, 0];
let uniforms = {};      // loc.__name -> value (last set)

const gl = new Proxy({}, {
  get(_t, prop) {
    if (prop in GLC) return GLC[prop];
    // Methods with meaningful behaviour:
    switch (prop) {
      case 'getContextAttributes': return () => ({ stencil: false });
      case 'getExtension': return (name) => {
        // Provide OES_element_index_uint; deny depth-texture so we exercise the
        // RGBA8-pack path (the widely-portable one).
        if (name === 'OES_element_index_uint') return {};
        return null;
      };
      case 'createShader': return () => tag('shader');
      case 'shaderSource': return () => {};
      case 'compileShader': return () => {};
      case 'getShaderParameter': return () => true;
      case 'getShaderInfoLog': return () => '';
      case 'createProgram': return () => tag('program');
      case 'attachShader': return () => {};
      case 'linkProgram': return () => {};
      case 'getProgramParameter': return () => true;
      case 'useProgram': return (p) => { curProgram = p; };
      case 'getAttribLocation': return () => ++uid;
      case 'getUniformLocation': return (p, name) => ({ __name: name });
      case 'createBuffer': return () => tag('buffer');
      case 'bindBuffer': return () => {};
      case 'bufferData': return () => {};
      case 'createTexture': return () => tag('texture');
      case 'bindTexture': return () => {};
      case 'texImage2D': return () => {};
      case 'texParameteri': return () => {};
      case 'generateMipmap': return () => {};
      case 'activeTexture': return () => {};
      case 'createFramebuffer': return () => tag('fbo');
      case 'bindFramebuffer': return (_t2, fb) => { curFB = fb; };
      case 'createRenderbuffer': return () => tag('rb');
      case 'bindRenderbuffer': return () => {};
      case 'renderbufferStorage': return () => {};
      case 'framebufferTexture2D': return () => {};
      case 'framebufferRenderbuffer': return () => {};
      case 'checkFramebufferStatus': return () =>
        (mode === 'fbfail') ? GLC.FRAMEBUFFER_UNSUPPORTED : GLC.FRAMEBUFFER_COMPLETE;
      case 'enable': return (c) => { if (c === GLC.BLEND) blend = true; };
      case 'disable': return (c) => { if (c === GLC.BLEND) blend = false; };
      case 'depthMask': return (v) => { depthMask = !!v; };
      case 'blendFunc': return () => {};
      case 'polygonOffset': return () => {};
      case 'stencilFunc': return () => {};
      case 'stencilOp': return () => {};
      case 'clearColor': return () => {};
      case 'clear': return () => {};
      case 'viewport': return (x, y, w, h) => { curViewport = [x, y, w, h]; };
      case 'enableVertexAttribArray': return () => {};
      case 'vertexAttribPointer': return () => {};
      case 'uniform1f': return (loc, v) => { if (loc) uniforms[loc.__name] = v; };
      case 'uniform1i': return (loc, v) => { if (loc) uniforms[loc.__name] = v; };
      case 'uniform3fv': return (loc, v) => { if (loc) uniforms[loc.__name] = Array.from(v); };
      case 'uniform4fv': return (loc, v) => { if (loc) uniforms[loc.__name] = Array.from(v); };
      case 'uniformMatrix4fv': return (loc, t, v) => { if (loc) uniforms[loc.__name] = Array.from(v); };
      case 'drawElements': return () => {
        draws.push({ program: curProgram, fb: curFB, blend, depthMask,
          viewport: curViewport.slice() });
      };
      case 'drawArrays': return () => {
        draws.push({ program: curProgram, fb: curFB, blend, depthMask,
          viewport: curViewport.slice(), arrays: true });
      };
      case 'deleteBuffer': return () => {};
      case 'deleteTexture': return () => {};
      case 'getParameter': return () => 0;
      default: return () => {};
    }
  },
});

// ---- DOM stubs --------------------------------------------------------------
function makeEl(tagName) {
  const style = {};
  style.cssText = '';
  const el = {
    tagName: (tagName || 'div').toUpperCase(),
    style, children: [], className: '', title: '',
    _text: '',
    width: 300, height: 150, clientWidth: 800, clientHeight: 600,
    offsetParent: {},
    getContext: (k) => (k === 'webgl' || k === 'experimental-webgl') ? gl : make2D(),
    appendChild(c) { this.children.push(c); return c; },
    replaceChild() {}, insertBefore() {},
    setAttribute() {}, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    setPointerCapture() {}, releasePointerCapture() {},
    requestPointerLock() {}, focus() {}, select() {},
    querySelector: () => null, remove() {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; },
    get innerHTML() { return this._text; },
    set innerHTML(v) { this._text = v; },
    get parentNode() { return this._parent || null; },
    set parentNode(v) { this._parent = v; },
    focus() {},
  };
  el._parent = null;
  return el;
}
function make2D() {
  return {
    canvas: { width: 128, height: 128 },
    fillStyle: '', strokeStyle: '', lineWidth: 1, lineCap: '', font: '', textAlign: '',
    textBaseline: '',
    fillRect() {}, strokeRect() {}, clearRect() {}, beginPath() {}, moveTo() {},
    lineTo() {}, arc() {}, arcTo() {}, closePath() {}, fill() {}, stroke() {},
    setTransform() {}, save() {}, restore() {}, fillText() {}, measureText: () => ({ width: 20 }),
    getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
    putImageData() {}, createLinearGradient: () => ({ addColorStop() {} }), translate() {},
    rotate() {}, scale() {},
  };
}

const canvas = makeEl('canvas');
const parent = makeEl('div');
parent.appendChild(canvas); canvas._parent = parent;

global.document = {
  createElement: (t) => makeEl(t),
  createTextNode: (t) => ({ nodeType: 3, textContent: t }),
  body: makeEl('body'),
  addEventListener() {}, removeEventListener() {},
  getElementById: () => null,
  activeElement: { tagName: 'BODY' },
  pointerLockElement: null, exitPointerLock() {},
};
global.window = {
  devicePixelRatio: 1,
  matchMedia: () => ({ matches: false }),
  addEventListener() {}, removeEventListener() {},
  performance: { now: () => 0 },
  getComputedStyle: () => ({ position: 'relative' }),
  requestAnimationFrame: () => 0, cancelAnimationFrame() {},
};
global.getComputedStyle = global.window.getComputedStyle;
global.requestAnimationFrame = global.window.requestAnimationFrame;
global.cancelAnimationFrame = global.window.cancelAnimationFrame;
try { Object.defineProperty(global, 'navigator', { value: { clipboard: null }, configurable: true }); }
catch (e) { /* navigator already present is fine */ }
global.location = { hash: '', href: 'file:///x' };
global.performance = global.window.performance;

// ---- Load the renderer ------------------------------------------------------
// The renderer is a top-level `function mountScene(...)` declaration; eval it and
// publish it onto global so the harness can call it.
(0, eval)(rendererSrc + '\n;this.mountScene = mountScene;');
const mountScene = global.mountScene;

const ctrl = mountScene(canvas, {}, makeEl('div'));
if (!ctrl) { console.log(JSON.stringify({ ok: false, error: 'mountScene returned null' })); process.exit(0); }

// ---- A tiny scene: two opaque nodes + one glass node ------------------------
// A unit box helper (positions/normals/indices) so drawElements has real buffers.
function box(cx) {
  const p = [], n = [], idx = [];
  const v = [[cx, 0, 0], [cx + 1, 0, 0], [cx + 1, 1, 0], [cx, 1, 0]];
  for (const q of v) { p.push(q[0], q[1], q[2]); n.push(0, 0, 1); }
  idx.push(0, 1, 2, 0, 2, 3);
  return { positions: p, normals: n, indices: idx };
}
const wallGeom = box(0), roofGeom = box(2), glassGeom = box(4);
const scene = {
  layers: ['walls', 'roof', 'openings'],
  nodes: [
    Object.assign({ name: 'wall:w0', layer: 'walls', color: [.5, .5, .5], mat: 'drywall' }, wallGeom),
    Object.assign({ name: 'roof', layer: 'roof', color: [.4, .4, .4], mat: 'metal' }, roofGeom),
    Object.assign({ name: 'window:o0:glass', layer: 'openings', color: [.6, .7, .8],
      mat: 'glass', isGlass: true }, glassGeom),
  ],
  walk: { eyeHeight: 5.5, segments: [], floors: [{ level: 0, elevation: 0, rects: [[0, 0, 5, 5]] }],
    stairs: [], doors: [], fixtures: [], rooms: [], spawn: { x: 1, y: 1, elevation: 0, face: [0, 1] } },
  sun: { orientation: 0, latitude: 35 },
};

ctrl.setScene(scene);
draws.length = 0;              // ignore setup draws; measure one clean frame
ctrl.draw();

const st = ctrl.sunState();

// ---- Classify the draws -----------------------------------------------------
// Programs are opaque tags; identify the shadow program as the one used while a
// non-null framebuffer is bound (the FBO pass), and the main program as the one
// used for the on-screen (fb null) node draws.
const shadowDraws = draws.filter(d => d.fb !== null && !d.arrays);
const screenDraws = draws.filter(d => d.fb === null && !d.arrays);
const mainProgram = screenDraws.length ? screenDraws[screenDraws.length - 1].program : null;
const shadowProgram = shadowDraws.length ? shadowDraws[0].program : null;

// Glass pass = on-screen node draws with blend on + depth write off.
const glassDraws = screenDraws.filter(d => d.blend === true && d.depthMask === false);
const opaqueDraws = screenDraws.filter(d => !(d.blend === true && d.depthMask === false));

// Index of the first shadow draw and first screen draw, to prove ordering.
const firstShadowIdx = draws.findIndex(d => d.fb !== null && !d.arrays);
const firstScreenIdx = draws.findIndex(d => d.fb === null && !d.arrays);

// ---- Light matrix maps a known interior point into the unit frustum ---------
// Multiply a column-major mat4 (16) by a vec4; return clip coords.
function mulv(m, v) {
  const o = [0, 0, 0, 0];
  for (let r = 0; r < 4; r++) {
    o[r] = m[0 * 4 + r] * v[0] + m[1 * 4 + r] * v[1] + m[2 * 4 + r] * v[2] + m[3 * 4 + r] * v[3];
  }
  return o;
}
// A point at the centre of the scene AABB (well inside the fitted frustum).
const centerPt = [2.5, 0.5, 0, 1];   // roughly the geometry centroid in world frame
const clip = st.lightVP ? mulv(st.lightVP, centerPt) : null;
let ndc = null, inFrustum = false;
if (clip && clip[3] !== 0) {
  ndc = [clip[0] / clip[3], clip[1] / clip[3], clip[2] / clip[3]];
  inFrustum = Math.abs(ndc[0]) <= 1.001 && Math.abs(ndc[1]) <= 1.001 && ndc[2] <= 1.001 && ndc[2] >= -1.001;
}

console.log(JSON.stringify({
  ok: true,
  mode,
  totalDraws: draws.length,
  shadowDrawCount: shadowDraws.length,
  screenDrawCount: screenDraws.length,
  glassDrawCount: glassDraws.length,
  opaqueDrawCount: opaqueDraws.length,
  shadowBeforeMain: (firstShadowIdx === -1) ? null : (firstScreenIdx === -1 ? true : firstShadowIdx < firstScreenIdx),
  shadowIsOwnProgram: shadowProgram && mainProgram && shadowProgram.__id !== mainProgram.__id,
  shadowViewport: shadowDraws.length ? shadowDraws[0].viewport : null,
  shadowOn: st.shadowOn,
  shadowSize: st.shadowSize,
  uShadowOn: uniforms.uShadowOn,
  ndc,
  inFrustum,
}, null, 2));
