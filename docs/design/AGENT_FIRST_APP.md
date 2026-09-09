# Agent-first architecture application

Status: **all five tiers shipped** — 1 (3D output), 2 (web playground), 3 (the
agent in the app), 4 (IFC export) and 5 (direct manipulation, round-tripped) are
**DONE**. The roadmap is complete: the design application described by the thesis
below — a DSL shared by human and agent, a compiler that validates every change,
and viewports that are pure functions of the source (now including *editable*
viewports whose gestures flow back into the text) — exists end to end.
Context: `docs/IDEAS.md` (Revit plug-in — foundation DONE), `docs/PRODUCT_REVIEW.md`,
`docs/REVIEW_DSL_REVIT*.md`.

## Motivation

The pyRevit extension (`revit/`) is complete and harness-tested, but it cannot
run on Revit LT — LT ships without the .NET API, so no add-ins, period. That
constraint exposed the deeper issue: Revit is a *human-first* tool. Its state is
a mutable binary document, its API assumes a UI thread and transactions, and an
agent driving it is puppeting a GUI designed for mouse-and-keyboard work. Every
friction in the integration was that impedance mismatch.

barndsl already inverts the model: the **source of truth is text**. The agent
writes the DSL, the compiler teaches with line-numbered diagnostics, and the
model — SVG plan, elevations, DXF, the Revit exchange — is a *build artifact*.
This is the same shift adjacent fields already made (schematic capture →
Verilog; console clicking → Terraform), and agents accelerate it because text
is the medium they are natively fluent in.

So the strategic move is to **demote Revit from "the host" to "one of several
exporters"**, and grow barndsl into the agent-first design application itself.

## Thesis

> A design application where the shared medium between human and agent is the
> DSL; the compiler validates and scores every change; and viewports (2D plan,
> elevations, 3D) are pure functions of the source.

Consequences:

* **Versionable design.** Plans diff, review, and regenerate like code.
* **The compiler is the product moat.** Every check added (egress, adjacency,
  climate, structure, cost) makes the agent's output better *without making the
  agent smarter*. Diagnostics are the feedback loop (`agent.py` already runs
  this compile–critique–revise loop, hill-climbing on `score.py`).
* **Interop, not hosting.** Export to open formats (DXF today; glTF and IFC
  next) instead of living inside a proprietary host. The professional workflow
  becomes: iterate here, hand off to the incumbent for construction documents.

## What exists (the foundation)

| Capability | Module | Notes |
| --- | --- | --- |
| Compile + diagnostics | `compiler.py`, `validation.py` | line/code/hint diagnostics; the teacher |
| Deterministic 0–100 score | `score.py` | hill-climb target for the agent |
| Agent loop (Claude) | `agent.py` | write → compile → critique → revise |
| 2D plan render | `render.py` | annotated SVG, takeoff panel |
| Elevations + section | `views.py` | schematic vertical views |
| CAD export | `dxf.py` | 2D interchange |
| BIM-shaped model | `revit.py` | deduplicated wall runs, hosted openings, floor slabs, `roof_plan`, stairs (`plan_stair_runs`), frame — **pure Python, no Revit** |
| Round-trip | `revit.py` `exchange_to_plan` | model → DSL (the inverse mapping) |
| Cost, energy, solar, schedule | `cost.py`, `energy.py`, `solar.py`, `schedule.py` | more compiler-as-teacher surface |

The key realization: `revit.py`'s exchange is not Revit-specific. It is a
general **3D-ready intermediate representation** — walls as centreline runs
with hosted openings, per-level slabs, a roof plan, stair flights, frame
members. Everything below builds on it, not on a new geometry layer.

## Product shape (target)

A web application with three panes:

```
┌────────────┬──────────────────┬──────────────────────┐
│ agent chat │  DSL editor      │  viewport            │
│ (brief in, │  (diagnostics    │  2D plan ⇄ 3D model  │
│  plan out) │   inline)        │  elevations, layers  │
└────────────┴──────────────────┴──────────────────────┘
```

The agent edits the DSL; the compiler validates; the viewport re-renders. The
human can talk, edit text directly, or (later) manipulate geometry with the
edit round-tripped into DSL — `exchange_to_plan` already proves the inverse
direction works.

**Shell re-layout (2026-09, canvas first).** The three equal panes above were the
right shape while the language was being built and the wrong shape for someone
designing a house: the code editor held the widest column, diagnostics were a
console, and the drawing was a tab in the third column. The shell now puts the
canvas first, keeping every module behind it unchanged:

```
┌────────────┬──────────────────────────────────┬────────────┐
│ rail       │  canvas                          │  3D dock   │
│ Design /   │  plan ⇄ elevations ⇄ report      │  (open /   │
│ Inspect    │  edit bar · zoom · status bar    │  split /   │
│            │            ┌─────────────────────┤  hidden)   │
│            │            │ source drawer       │            │
│            │            │ (editor + diags)    │            │
└────────────┴────────────┴─────────────────────┴────────────┘
```

* The left rail has two pages: **Design** (agent chat + offline form) and
  **Inspect** (the design panel — outline and properties — moved out of the
  viewport so the plan keeps its width).
* The **3D model is a dock**, not a tab: a fixed-width column beside the plan
  (drag to resize, Split for half the canvas) or a 36px tab when hidden, so plan
  and model are visible together. `2` toggles it; the state persists.
* The **editor + diagnostics are a drawer** that slides over the canvas from the
  right (`s`, the header Source button, or the status bar). It never pushes the
  drawing. Compile errors and any jump-to-line open it uninvited.
* A **status bar** under the canvas carries the compile counts, so the source can
  stay closed until it is needed.

Two follow-ons have landed on the same shell: **diagnostics on the drawing**
(a badge per room with open issues, a popover with the hint, the quick fix and
Ignore — which writes the compiler's own `accept` pragma onto the line) and
**linked selection** (one selected room ringed on the plan, highlighted in the
3D dock, marked in the gutter and picked in the inspector, started from any of
them, including the caret landing on a room's statement). Still to come, in
order: elevations and section as canvas views with a face picker rather than a
grid of thumbnails; then the visual pass (one type scale, one button recipe).

What we deliberately do **not** build: Revit's documentation engine (wall-layer
joins, detailing, sheet sets). That is where "Revit killers" die. The wedge is
design-iteration speed on a constrained domain (barndominiums), with open
formats as the hand-off.

## Roadmap

### Tier 1 — 3D output (highest leverage) — DONE

The single biggest change to how the product *feels*: a reviewable 3D model
from any `.barn` file, no CAD license, no plug-in, no API key. **Shipped:**
`src/barndsl/gltf.py` (`barndsl gltf`) and `src/barndsl/viewer.py`
(`barndsl view3d`), both pure Python / stdlib-only, tested in
`tests/test_gltf.py` and `tests/test_viewer.py`.

1. **`gltf.py` — glTF 2.0 exporter.** Pure Python, stdlib only (glTF is JSON +
   a base64/binary buffer; no library needed). Lower the `revit.py` exchange
   into meshes:
   * floor slabs per level; wall runs extruded to their level height with
     door/window openings cut (per-opening width × height at sill);
   * the roof from `roof_plan` (gable exact, shed/monitor as modeled);
   * porch slabs and posts; frame posts/beams as rectangular members;
   * stair flights from `plan_stair_runs`;
   * per-room-type colors reused from `render.py` so the drawing set reads as
     one system; nodes named and grouped (walls / roof / frame / …) so viewers
     can toggle layers.
   * CLI: `barndsl gltf FILE --out plan.glb` (also `.gltf` embedded-buffer).
   * Tests pin the invariants: valid glTF structure (accessor/bufferView
     bookkeeping, byte alignment), watertight counts (every wall run → a mesh,
     every opening cut), coordinates round-trip (feet, y-up glTF vs x-east/
     y-north plan), gallery plans export clean.

2. **`viewer.py` — single-file HTML viewer.** `barndsl view3d FILE --out
   plan.html`: one self-contained HTML file embedding the scene, with orbit
   controls, layer toggles, and the plan title/score. Works by double-clicking
   the file. (Three.js from CDN with a clear offline fallback message, or a
   minimal embedded renderer — implementation's choice; single-file output is
   the requirement.) **Shipped:** a first-person **walk mode** (Walk pill / Enter
   key) over the same renderer — WASD + mouse look at eye height, wall-sliding
   collision, walk-up stairs — driven by a `walk` block in `scene_json` and
   shared by the single-file viewer and the playground 3D tab.

### Tier 2 — web playground — DONE

DSL editor with inline diagnostics, live SVG + 3D. No agent yet; this is the
shell the agent plugs into. **Shipped:** `src/barndsl/playground.py`
(`barndsl serve`) — a stdlib `http.server` **local** app (127.0.0.1, zero new
dependencies, works offline; not Pyodide) that calls the installed compiler
directly. `POST /api/compile` returns the compile as JSON (diagnostics + `svg`,
`scene`, `score`, `metrics`, elevations/section — all pure functions of the
plan); bad DSL is a normal 200 with diagnostics, never a 500. The single-page app
(inlined, no CDN) is a textarea editor with a line-number + severity gutter,
a clickable diagnostics panel, and a viewport tabbed 2D plan / 3D / elevations.
The 3D tab reuses the single-file viewer's inline WebGL renderer verbatim — it
was factored into `viewer.RENDERER_JS` (a shared `mountScene()` asset both embed)
so the two never diverge. Tested in `tests/test_playground.py`. (A static Pyodide
build remains a possible later deploy target — see `docs/IDEAS.md`.)

### Tier 3 — the agent in the app — DONE

`agent.py`'s loop behind a conversation UI: brief in, validated plan out,
diagnostics-driven rewrites behind the scenes, every iteration scored and the
best returned. Streaming the intermediate renders makes the loop legible.
**Shipped:** a chat pane in `src/barndsl/playground.py` and a small, keyless
seam into `src/barndsl/agent.py`.

* `POST /api/design` (`{brief, source?, iterations?}`) is a **Server-Sent
  Events** stream of the compile-critique-revise loop: a `status` opener, one
  `iteration` per round carrying its score, diagnostic counts and the FULL
  `compile_payload` (the editor + viewport update live, so the user watches the
  design evolve), and a final `done` with the **best-scoring** iteration — a
  regressed final round is never returned. Failures are structured `error`
  frames (`kind` ∈ unavailable / missing_dependency / api_error / cancelled).
* `GET /api/agent` probes availability (`agent.agent_availability`: `anthropic`
  importable **and** `ANTHROPIC_API_KEY` set — only the key's *presence*, never
  its value) so the SPA enables or disables the pane with the install hint.
* **Refinement:** a `source` in the body seeds the loop, so follow-up briefs
  ("make the kitchen bigger") revise the current plan. In `agent.py`, `design()`
  gained keyword-only `seed_source` (primes round 1 as a revision, *not* scored
  as a competing iteration), `cancel` (a `() -> bool` polled between rounds) and
  `on_phase(phase, round)` — all default no-ops, so existing callers/tests are
  untouched.
* **Cancellation:** one job at a time (409); `POST /api/design/cancel {id}` sets
  a flag the loop polls between rounds (also tripped when the SSE connection
  drops); the **Stop** button drives it. The compile endpoint stays responsive
  throughout (`ThreadingHTTPServer` + one design lock).
* stdlib-only server; `anthropic` imported lazily only when a design job runs;
  the injectable `make_server(designer=…)` lets `tests/test_playground_agent.py`
  drive the whole SSE path with a scripted fake — no network, no key, no
  `anthropic` (agent-hook tests live in `tests/test_agent_loop.py`).

### Tier 4 — IFC export — DONE

The same exchange lowered to IfcWall/IfcDoor/IfcWindow/IfcSlab/IfcRoof, getting
plans into full Revit, ArchiCAD, and every IFC viewer — the professional
hand-off, and the final demotion of the pyRevit path to "one exporter among
several." **Shipped:** `src/barndsl/ifc.py` (`to_ifc`/`write_ifc`, `barndsl ifc`),
tested in `tests/test_ifc.py`.

**Deviation from this sketch (deliberate):** the sketch imagined `ifc.py` *via
IfcOpenShell* as an optional dependency (like `raster`). It ships instead as a
**hand-written, pure-Python, stdlib-only STEP (SPF) writer** — following the
`dxf.py` precedent (the repo hand-writes DXF rather than depend on `ezdxf`) and
the repo's zero-dep-engine ethos. The geometry is entirely extruded rectangles
plus a few prisms, so a tiny ISO-10303-21 backbone + IFC4 entity graph is the
better fit than a heavyweight geometry kernel. IfcOpenShell is **demoted to an
optional test-time validation oracle** (skipif-guarded tests, an optional
`barndsl[ifc-validate]` extra); it is never an install or runtime dependency.

Schema is **IFC4** (not 2x3). The spatial hierarchy is IfcProject → IfcSite →
IfcBuilding → one IfcBuildingStorey per level. Walls are `IfcWall` (uncut box)
with real `IfcOpeningElement` voids (`IfcRelVoidsElement`) filled by
`IfcDoor`/`IfcWindow` (`IfcRelFillsElement`, carrying `OverallWidth`/
`OverallHeight`); slabs and porch slabs are `IfcSlab` (FLOOR); the roof is one
`IfcRoof` (RoofType from gable/shed/monitor) whose planes are extruded-area-solid
triangular/wedge **prisms** (`IfcArbitraryClosedProfileDef` swept horizontally —
real solids every common viewer renders, so no faceted-brep fallback was needed);
frame members are `IfcColumn`/`IfcBeam`; stairs `IfcStair`; and every room is an
`IfcSpace` (footprint extruded to ceiling) so schedules and areas work downstream.
A small `barndsl` `IfcPropertySet` on the building carries the design score, sq-ft
metrics and a source hash. Units are declared **imperial** (a conversion-based
foot = 0.3048 m), so coordinates stay in feet and map straight onto IFC's z-up
world frame — the same pass-through the DXF and glTF exports rely on. GlobalIds
are IFC 22-char compressed GUIDs derived deterministically with `uuid5`, and no
wall-clock timestamp is written, so re-exporting an unchanged plan is
byte-identical.

### Tier 5 — direct manipulation, round-tripped — DONE

Viewport edits (drag a room, resize it, slide a door/window along its wall)
emitted as DSL edits, so the text stays the source of truth. **Shipped:**
`src/barndsl/edits.py` (`apply_edit`) and an **Edit layout** overlay in the 2D
plan tab of `src/barndsl/playground.py` (`POST /api/edit`), tested in
`tests/test_edits.py` and `tests/test_playground.py`.

**The key architectural decision:** the edit engine is a **pure function over
source *text***, not model→DSL regeneration. `revit.py`'s `exchange_to_plan` (and
`emit.py`) prove the inverse mapping works, but regenerating the whole source from
the model would flatten the author's comments, spacing and statement order — so it
is deliberately the *wrong* tool here. Instead each gesture compiles the source to
locate the one statement to touch, re-tokenizes only that line, rewrites the tokens
that actually changed, and returns the source with every other byte preserved. The
guarantees are test-pinned: only the target line changes (inline `# comments`
survive), a no-op is byte-identical (and a relative placement stays relative unless
the coordinates truly change — a real move converts it to absolute), and unknown /
malformed edits are typed errors, never exceptions or 500s. The element→line map is
a minimal `CompileResult.room_lines` addition (openings already carry their source
line); the overlay is drawn by the frontend from compact `rooms`/`openings` arrays
in the compile payload, with 0.5 ft grid snap, a 3 ft minimum-dimension guard, an
undo stack of source snapshots, and a floor switcher (segmented chips, or `[` /
`]`) that edits any level of a multi-level plan while the other floors — and any
stair footprint — render as a dimmed underlay to align against.

## Non-goals

* Construction-document production (sheets, detailing) beyond what `packet.py`
  and the Revit `document()` pass already do.
* General-purpose BIM. Scope discipline (rectangles, post-and-beam, one
  building type) is what makes the compiler's guarantees possible.
* Photorealism. The 3D model is for design review, not rendering.

## Landscape (why this position is defensible)

Hypar (buildings as code) is the closest philosophically; Arcol/Snaptrude are
browser-BIM; Forma/TestFit do generative layout. None are agent-first with a
textual, human-and-agent-shared source of truth — their chat features puppet a
GUI model, the retrofit problem again. The DSL-as-interface position, backed by
a diagnostic-rich compiler, is the differentiated part — and it already works.
