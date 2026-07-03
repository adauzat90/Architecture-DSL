# Agent-first architecture application

Status: **design** — tier 1 (3D output) **shipped/DONE**; tiers 2–5 planned.
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
   the requirement.)

### Tier 2 — web playground

Compiler-in-browser (barndsl's engine is pure Python + pydantic — Pyodide can
run both) or a thin API server. DSL editor with inline diagnostics, live SVG +
3D. No agent yet; this is the shell the agent plugs into.

### Tier 3 — the agent in the app

`agent.py`'s loop behind a conversation UI: brief in, validated plan out,
diagnostics-driven rewrites behind the scenes, every iteration scored and the
best returned. Streaming the intermediate renders makes the loop legible.

### Tier 4 — IFC export

`ifc.py` via IfcOpenShell (optional dependency, like `raster`): the same
exchange lowered to IfcWall/IfcDoor/IfcWindow/IfcSlab/IfcRoof. Gets plans into
full Revit, ArchiCAD, and every IFC viewer — the professional hand-off, and the
final demotion of the pyRevit path to "one exporter among several."

### Tier 5 — direct manipulation, round-tripped

Viewport edits (drag a wall, resize a room) emitted as DSL edits, so the text
stays the source of truth. `exchange_to_plan` is the existing proof.

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
