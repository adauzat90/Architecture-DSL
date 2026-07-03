# Ideas — future improvements

Parked ideas for improving barndsl, especially **agent plan-generation quality**.
Not yet scheduled; capture here so they aren't lost.

## Structural frame placement — DONE
Shipped: a `frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]` directive that
auto-places the post-and-beam skeleton over the footprint (`src/barndsl/structure.py`,
`place_frame`). Bents are spaced ≤ `bay` ft o.c. along each footprint block's long
axis, span the short axis, get eave/gable/corner posts and (unless `no-ridge`) a
ridge member; interior support posts split a span over `span` ft. Deterministic and
round-trips through `emit`. The renderer overlays bents/ridge/posts and the panel
reports a structural takeoff (bent/post counts, beam linear feet); `metrics()` gains
`post_count`/`beam_count`/`frame_count`/`beam_linear_ft`. Checks: `BAY_WIDE` and
`POST_OBSTRUCT` (info), and `POST_IN_OPENING` (warning — a post landing inside a
window/door, since the post grid is fixed and openings go in the bays between
posts). `barndsl build FILE --frame` frames a plan with no directive. Explicitly a
layout aid, not an engineered design.

Possible follow-ups: feed `beam_linear_ft` into the roadmap's cost estimator;
engineered member-sizing tables (span vs. section); lateral-bracing / shear-wall
hints. ~~Honour an explicit interior bearing wall as a post line~~ — DONE: the
`wall a - b bearing` statement declares it, and `place_frame` drops an interior
post onto the wall at every bent crossing its run (a wall across the span gets a
`WALL_BEARING_AXIS` info instead).

## 3D export + viewer — DONE
Shipped (Tier 1 of `docs/design/AGENT_FIRST_APP.md`): `src/barndsl/gltf.py`
(`to_gltf`/`to_glb`/`write_gltf`) lowers the plan into a 3D model as **glTF 2.0**
— pure Python, stdlib only (`base64`, `struct`, `json`, `math`), no new
dependency. It builds on the existing Revit-shaped exchange, not a new geometry
layer: wall runs extruded to their level height with door/window openings cut
(solid piers + lintel/sill boxes — axis-aligned box decomposition, no CSG), floor
slabs, gable-end infill, the roof from `roof_plan` (gable exact, shed one plane,
monitor from its sections), frame posts/beams, porches (+ covered-porch posts),
stepped stair flights from `plan_stair_runs`, and room floors tinted with
`render.ROOM_COLORS` (hex → linear baseColorFactor). Plan feet x-east/y-north/z-up
maps to glTF y-up `(x, z, -y)`; nodes are named and grouped per layer so viewers
can toggle them. `src/barndsl/viewer.py` (`write_viewer`) writes **one**
self-contained, offline HTML file with an inline WebGL renderer (orbit/pan/zoom,
one directional light + Lambert, layer toggles). CLI: `barndsl gltf` and `barndsl
view3d`. Tests pin glTF structural validity (accessor/bufferView bookkeeping,
POSITION min/max, index ranges, .glb chunk padding), the geometry contract (every
wall run → a mesh, opening cuts reduce wall volume), and a gallery export sweep.

Bugfix — multi-level wall heights (`src/barndsl/wallheights.py`, shared by
`gltf.py` and `ifc.py`): the exchange carries each run at its storey's clear
ceiling (the centreline fact the pyRevit consumer needs), so extruded naively a
multi-level plan showed an open **gap band** the floor-assembly depth between
stacked levels and a wall-to-roof **void** wherever a lower level wasn't under an
upper floor (e.g. `two_story`'s single-storey east end). The exporters now correct
each run's vertical extent per segment — covered runs rise to the base of the level
above, uncovered exterior runs rise to the roof plate — and close any gable end a
run newly reaches. The Revit exchange is unchanged; single-level plans are
byte-identical.

Possible follow-ups: billboarded room labels in the viewer; true swept gable-wall
pentagons instead of the box + infill approximation.

## IFC export — DONE
Shipped (Tier 4 of `docs/design/AGENT_FIRST_APP.md`): `src/barndsl/ifc.py`
(`to_ifc`/`write_ifc`, CLI `barndsl ifc`) lowers the same Revit-shaped exchange
into **IFC4**, the open BIM interchange, so a plan opens in full Revit, ArchiCAD,
BIMcollab/Solibri and every IFC viewer — the professional hand-off. **Deviation
from the original sketch:** rather than the optional `ifcopenshell` dependency the
design doc imagined, it is a **hand-written, pure-Python, stdlib-only STEP/SPF
writer** (like `dxf.py` hand-writes DXF) — the geometry is all extruded rectangles
and a few prisms, so a tiny ISO-10303-21 backbone beats a heavyweight kernel and
keeps the engine zero-dep. `ifcopenshell` is demoted to a test-time validation
oracle (skipif in `tests/test_ifc.py`; optional `barndsl[ifc-validate]` extra),
never a runtime dep. IfcProject → IfcSite → IfcBuilding → per-level
IfcBuildingStorey; walls are `IfcWall` (uncut box) with real `IfcOpeningElement`
voids (`IfcRelVoidsElement`) filled by `IfcDoor`/`IfcWindow` (`IfcRelFillsElement`,
real `OverallWidth`/`OverallHeight`); slabs and porch slabs are `IfcSlab` (FLOOR);
the roof is one `IfcRoof` (RoofType from gable/shed/monitor) whose planes are
`IfcExtrudedAreaSolid` triangular/wedge **prisms** (an `IfcArbitraryClosedProfileDef`
swept horizontally along the ridge — real solids every viewer renders, no
faceted-brep fallback needed); frame → `IfcColumn`/`IfcBeam`; stairs → `IfcStair`;
and one `IfcSpace` per room (name + LongName room-id/type, footprint extruded to
ceiling) so downstream schedules/areas work. A small `barndsl` `IfcPropertySet` on
the building carries the design score, sq-ft metrics and a source hash. Units are
declared **imperial** (a conversion-based foot = 0.3048 m, plus square/cubic foot),
so coordinates stay in feet with no boundary conversion; plan x-east/y-north/z-up
maps straight onto IFC's z-up world frame. GlobalIds are IFC's 22-char compressed
GUIDs derived deterministically with `uuid5` (plan name + kind + id), and the file
carries no wall-clock timestamp, so re-exporting an unchanged plan is
byte-identical. Tests parse the SPF textually (header, reference integrity, entity
counts vs the exchange, GUID validity/determinism, unit block, tricky-name
escaping, gallery sweep) with an `ifcopenshell` oracle layer that opens the file,
walks the spatial tree and tessellates the geometry.

Possible follow-ups: room-type colours as `IfcStyledItem`/`IfcSurfaceStyle` on the
spaces/floor tiles; aggregate the roof as per-plane `IfcSlab(ROOF)` under the
`IfcRoof` instead of direct geometry (stricter model-checker conformance); IFC2x3
output for older Revit-import paths that still prefer it. ~~A download/export menu
in the playground offering `.barn`/`.svg`/`.glb`/`.ifc` of the current plan.~~ —
DONE: the viewport's **Export** menu offers `.barn`, `SVG`, `DXF`, `GLB`, `IFC` and
the self-contained 3D viewer `.html` via `POST /api/export`, reusing these
exporters unchanged (see the Web playground entry).

## Web playground — DONE
Shipped (Tier 2 of `docs/design/AGENT_FIRST_APP.md`): `src/barndsl/playground.py`
(`barndsl serve [FILE] [--port 8787] [--open]`) — a **local** web app, not a
hosted service. It's a stdlib `http.server` (`ThreadingHTTPServer` +
`BaseHTTPRequestHandler`) bound to `127.0.0.1` that calls the installed compiler
directly: **zero new dependencies, no CDN, works offline** (deliberately not
Pyodide). Four routes and nothing else — no static-file serving, no directory
listing, no state, no files written. `POST /api/compile` (1 MB cap) returns the
compile as JSON — the same diagnostics `CompileResult.to_dict` exposes plus,
on a built plan, `svg` (render), `scene` (the exact blob the single-file viewer
embeds), `score`, `metrics`, and the four elevations + section (pure functions,
included). Bad DSL is a normal 200 with diagnostics, never a 500; only
malformed/oversize JSON is 400. `GET /` serves a self-contained single-page app
(inlined HTML/CSS/JS, no external references): a textarea editor with a
synchronised line-number + severity gutter, a click-to-jump diagnostics panel,
compile-on-type (400 ms debounce, Ctrl/Cmd+Enter to force), a header with plan
title / score / metrics, a `/api/examples` load menu, and a viewport tabbed 2D
plan (CSS-transform pan/zoom) / 3D / elevations. The last good render stays
visible (dimmed) while the source is broken. `GET /api/reference` serves
`DSL_REFERENCE` for a help panel.

The 3D tab reuses the single-file viewer's inline WebGL renderer **verbatim**:
it was factored out of `viewer.py`'s template into `viewer.RENDERER_JS` — one
`mountScene(canvas, labels, togglesEl)` function returning a controller whose
`setScene(json)` loads/swaps geometry — which both the viewer and the playground
embed, so the two renderers can't drift. `tests/test_playground.py` pins the
API contract (fields present, every example compiles through it, bad DSL → 200,
malformed → 400, oversize rejected, unknown path → 404 serving no files), the
no-external-references invariant, and the shared-renderer refactor (the viewer
stays self-contained; the app embeds the same asset).

Possible follow-ups: a **static Pyodide build** as an alternative zero-backend
deploy target (the engine is pure Python + pydantic, so Pyodide can run it
entirely client-side — trades the local server for a heavier first load); a
shareable-plan permalink (source in the URL hash); side-by-side scheme compare
(`compare_plans`) in the UI.

## Agent in the playground — DONE
Shipped (Tier 3 of `docs/design/AGENT_FIRST_APP.md`): `agent.py`'s
compile-critique-revise loop behind a conversation pane in the playground, its
intermediate renders streamed live. `POST /api/design` (`{brief, source?,
iterations?}`) is a **Server-Sent Events** stream — a `status` opener, one
`iteration` per round carrying the round's score, diagnostic counts and the FULL
`compile_payload` (so the editor + viewport update as each round lands and the
user watches the design evolve), and a final `done` carrying the **best-scoring**
iteration (not the last — a regressed final round is never handed back). Errors
are structured `error` frames with a `kind` (unavailable / missing_dependency /
api_error / cancelled). `GET /api/agent` probes availability
(`agent.agent_availability`: `anthropic` importable **and** `ANTHROPIC_API_KEY`
set — only the key's *presence*, never its value) so the SPA lights up or disables
the pane with the one-line install hint. A `source` in the body seeds the loop, so
follow-up briefs refine the current plan instead of starting fresh. One job at a
time (409); `POST /api/design/cancel {id}` sets a cancel flag the loop polls
between rounds (also tripped when the SSE connection drops), and the **Stop**
button uses it. The compile endpoint stays responsive throughout
(`ThreadingHTTPServer`, one shared design lock).

The seam into `agent.py` stays minimal and keyless-testable: `design()` gained
three keyword-only hooks — `seed_source` (refine the caller's plan; primes round 1
as a revision, *not* recorded as a competing iteration so an edit that trades a
point for the user's request isn't vetoed by best-iteration-wins), `cancel` (a
`() -> bool` polled per round), and `on_phase(phase, round)` (narrates
writing/compiling/critiquing) — all defaulting to no-ops, so existing callers and
tests are untouched. `stdlib`-only server; `anthropic` imported lazily only when a
design job runs; the injectable `make_server(designer=…)` lets the tests exercise
the whole SSE path with a scripted fake — no network, no key, no `anthropic`
(`tests/test_playground_agent.py`, plus the hook tests in
`tests/test_agent_loop.py`).

Possible follow-ups: a **diff view** of what a refinement changed (source diff +
score delta, reusing `compare_plans`); **per-phase token/latency** surfaced in the
status line; letting the user pick `iterations` / `target_score` / model from the
pane; **resumable** streams (an `EventSource` reconnect with a job cursor) so a
dropped tab can rejoin a running job; a **thread transcript** export.

## Direct manipulation — DONE
Shipped (Tier 5 of `docs/design/AGENT_FIRST_APP.md`): `src/barndsl/edits.py`
(`apply_edit`) plus an **Edit layout** overlay in the 2D plan tab of the
playground. Viewport gestures — drag a room, resize it by its edge/corner
handles, slide a door/window/entry along its wall — become **surgical DSL text
edits**, so the source stays the source of truth. The engine is a pure function
over source *text* (not `emit.py`/`exchange_to_plan` regeneration, which would
flatten comments, spacing and statement order): it compiles the source to find the
one statement to touch, re-tokenizes only that line, and rewrites the changed
tokens with every other byte preserved (inline `# comments` on the touched line
survive; a no-op is byte-identical; a relative placement stays relative unless the
coordinates truly change, then converts to absolute; unknown/malformed edits are
typed errors, never exceptions). `POST /api/edit` (`{source, edit}`) applies one
edit and returns the recompiled `compile_payload`; a refused edit is a normal 200
with `{error:{kind,message}}`. The element→line map is a minimal
`CompileResult.room_lines` addition (openings already carry `.line`); the overlay
is drawn client-side from compact `rooms`/`openings` payload arrays (room-palette
colours, id labels) with 0.5 ft grid snap, a 3 ft minimum-dimension guard, an undo
stack of source snapshots (button / Ctrl-Cmd-Z), a click-to-jump-to-line select,
and level-0 editing on multi-level plans. Tests: `tests/test_edits.py` (the
preservation/idempotence/typed-error guarantees + a gallery sweep) and the
`/api/edit` + SPA-markup cases in `tests/test_playground.py`.

Genuine follow-ups: **adjacency-preserving drags** — when a moved room stays flush
against its former anchor, emit an updated *relative* placement (`east-of foo align
… offset …`) instead of converting to absolute, so the author's intent survives;
**wall-attribute editing** from the overlay (toggle a shared wall plumbing /
bearing / rated, add/remove a `wall` statement); **multi-select** + group move /
align / distribute (one batched edit set, one undo entry); dragging to **create**
(rubber-band a new `room`, drop a new window/door onto a wall) and **delete**;
editing **porches / stairs / wings** (not just rooms and openings) and the upper
levels of a multi-level plan (a level switcher); a **live coordinate/size readout**
and dimension witnesses while dragging; snapping to **sibling edges** (align to an
adjacent room's wall, not just the 0.5 ft grid).

## Revit plug-in — foundation DONE
Shipped: `src/barndsl/revit.py` (`to_revit_model` / `to_revit_json`, the
`barndsl.revit/1` exchange) and a `barndsl revit FILE --out plan.json` command.
Lowers the rectangle IR into a Revit-shaped model — per-level `RevitLevel`s,
**deduplicated** wall centrelines (room edges decomposed to atomic segments,
classified interior/exterior against the footprint, then merged into runs),
openings hosted onto wall ids (interior doors / cased openings / exterior doors /
windows, with width/height/sill), room seed points, and structural columns/framing
from a placed `frame`; porches/stairs come across as reference areas. Pure Python,
no Revit/.NET/API key; `tests/test_revit.py` pins the invariants (exterior walls
trace the envelope perimeter, every opening lands on a real wall at its line,
interior↔interior / exterior↔exterior hosting, room seeds inside their rectangles,
multi-level walls, JSON round-trip). Coordinates pass straight through (barndsl
feet/x-east/y-north == Revit's foot-based world XY).

The **pyRevit extension** (chosen integration path) is in `revit/` — a barndsl
ribbon tab with *Build Plan* and *Export Exchange* buttons, targeting **Revit
2025** (.NET 8 / pyRevit 5 / CPython 3.12). Split into a Revit-free
`exchange.py` (load/validate; tested in `tests/test_revit_exchange.py`) and a
`builder.py` that creates levels, walls, **size-matched** doors/windows
(duplicates the family and sets Width/Height type params, cached per size), rooms,
structural columns/framing, porch floor slabs (`Floor.Create`), and best-effort
straight stairs (StairsEditScope, after the main transaction). One transaction for
everything but stairs; defensive per-element error handling.

Debuggability (for in-Revit testing): a Revit-free `report.py` (BuildReport /
BuildOptions, tested) records every element's outcome (created/skipped/failed +
Revit id + reason) and renders markdown + a `*.buildlog.json`. *Build Plan* offers
**Preview** (a real build, rolled back). A **Diagnostics** button reports the
environment and the project's available wall/floor/door/window/structural types
with readiness flags. A `config.json` sidecar maps each pass to **named** template
types (auto-pick fallback).

Round-trip: the exchange now goes both ways. `exchange_to_plan` / `barndsl
revit-import` reconstruct a plan/DSL from a `barndsl.revit/1` document (rooms carry
their rectangle; each opening's wall/offset is re-derived from geometry), verified
by `tests/test_revit_roundtrip.py` (every gallery plan recovers its rooms/doors/
windows/envelope and re-emits clean DSL). The exchange gained room rectangles +
envelope/wings. A `Model to DSL` button reads a live Revit model (rooms +
door/window instances) back into an exchange via `builder.read_model` (experimental;
room types guessed from names — `naming.py`, tested).

Multi-flight stairs: `plan_stair_runs` (pure, in `revit.py`, tested by
`tests/test_revit_stairs.py`) lays out the flights for a stair footprint —
straight, or a switchback (two flights + landing) when the straight run won't
fit, or a flagged overrun — and rides in the exchange; the builder instantiates
each flight via the Stairs component API with automatic landings.

Builder testability: `tests/revit_fakes.py` is a minimal fake of the Revit/pyRevit
API so `builder.py` runs in plain CPython; `tests/test_revit_builder.py` (22
tests) covers wall-type selection, opening hosting, family sizing, dry-run
rollback, named overrides, structure/porch/stair passes, `diagnose`, and
`read_model`. Everything that can be tested without a live Revit now is.

Building completion: the exchange + builder now also produce **floor slabs**
(per level), **structural grids** (`structural_grids` — numbered bents + lettered
eaves/interior-post lines from a placed frame), and a **footprint roof** (`roof_plan`
— ridge along the long axis, pitch/rise; the builder lays a flat footprint roof,
gable slope a manual refinement). All three have pure tested cores
(`tests/test_revit_model_extras.py`) and harness-tested builder passes.

Idempotent re-build — DONE: every created element is stamped barndsl-managed (in
Comments); a re-build purges the prior managed set first (default; `replace`
flag), so iterating replaces instead of duplicating and never touches hand-drawn
elements. Levels are reused, stairs aren't purged. Harness-tested
(`test_revit_builder.py`: marking, idempotence, no-replace duplicates, leaves
unmanaged alone).

Deliverables (Tier 2) — DONE: `builder.document(doc, options)` makes a floor-plan
view per level, room/door/window tags in those views, native door/window/room
schedules, and a sheet per level with the plan placed — all in one transaction,
idempotent (barndsl-named views/sheets/schedules are replaced on re-document), and
behind a *Document* ribbon button. Harness-tested in `tests/test_revit_document.py`
(counts, two-level views/sheets, idempotence, skip-without-view-type/title-block,
disable flags, managed-only tagging).

Fidelity fixes (review §3.1–3.3) — DONE: the exchange now carries door
`swing_into`/`hinge` (and the builder flips facing/hand to match), a nominal
`size` on framing (beams share the post section; columns/beams get
duplicate-per-size `barndsl WxD` types); `orientation` rotates Project true
north (`ProjectPosition.Angle`); `siding`/`roofing` hints pick wall/roof types
by name between the named config override and the auto-pick; cased openings cut
a real wall opening (`NewOpening`) instead of a leaf-door stand-in (door-family
fallback with a note); fixtures rotate to back onto their wall; exterior egress
doors are stamped `barndsl egress` in Comments for schedule filtering. All
harness-tested (`test_revit_builder.py`, fakes extended with
facing/hand flips, `NewOpening`, `RotateElement`, `ActiveProjectLocation`);
interior-door `egress` stays hardcoded False in the exchange because
`InteriorDoor` genuinely has no egress concept.

Still needs a live Revit (not unit-testable here): validate every builder/document
call against Revit 2025; harden the experimental reader (wall-type/level inference,
non-rectangular rooms); the gable-roof slope; turned/multi-flight stair landings;
the true-north sign convention, swing default-hand assumption, cased-opening
cuts and fixture default-facing assumption from the fidelity batch;
and (a smaller follow-on) dimension strings, which aren't placed yet.

## Agent aids

### ~~Worked-example gallery (highest-leverage non-check aid)~~ — DONE
Shipped: four **verified 0/0/0** plans in `examples/gallery/` for the agent to
few-shot from — `cottage.barn` (1 bed/1 bath), `hall_spine.barn` (3 bed/2 bath
with a primary suite), `lshape.barn` (`wing`/L-footprint), and `two_story.barn`
(`stair` + `loft`). `tests/test_gallery.py` recompiles each and asserts it stays
0/0/0 (and round-trips through `emit`), so the gallery can't rot as the rules
evolve. Linked from the `barndsl-authoring` skill. Building these also surfaced
two rule gaps, now fixed: `NO_CLOSET` requiring a door-connected closet, and the
new `MASTER_ENSUITE` check.

### ~~`program N bed M bath` directive + `PROGRAM_MATCH` check~~ — DONE
Shipped: a `program <n> bed [<m> bath]` statement whose declared counts the
validator checks against the rooms placed (`PROGRAM_MISMATCH` warning). Closes the
"clean ≠ correct" gap mechanically. Since widened to a required-room list and a
minimum area — see "Widened `program` directive" below.

## Diagnostic machinery — DONE
Shipped alongside the checks below:

- **Central code registry** (`barndsl/diagnostics.py`): every code with its usual
  severity, a one-line title, and an explanation (citing the IRC clause where one
  applies). A test asserts the registry covers every code the source emits, so a
  new check can't ship without an explanation. Powers `barndsl explain <CODE>`.
- **Machine-readable diagnostics**: `CompileResult.to_dict()` / `barndsl compile
  --json` emit the diagnostics as stable JSON for the agent loop and tooling.
- `ValidationReport.summary()` now reports the **info** count too (it silently
  dropped it before).

## Checks shipped from this list
- ~~`EGRESS_SIZE`~~ — a bedroom escape opening that exists but is below the IRC
  R310 clear-opening minimums (area, width, height, sill). DONE (warning).
- ~~`STAIR_RUN`~~ — a stair footprint too short to physically climb its storey
  (R311.7 riser/tread geometry vs. ceiling height). DONE (warning; conservative —
  only fires when even a switchback wouldn't fit).
- ~~`OPENING_CLASH`~~ — two openings overlapping on the same wall span. DONE
  (error). Surfaced and fixed a latent bug in the auto-layout opening placer.

## Candidate checks shipped — DONE
Shipped as conservative `info` nudges (never block a compile):

- ~~`ROOM_TIGHT`~~ — type-aware usable minimums by area *and* shortest side
  (kitchen ~70; full bath ~48 / ≥ 6 ft; half bath ~30 / ≥ 5 ft). Bedrooms stay
  covered by `BEDROOM_AREA`. (Half-bath minimums added in the second review round.)
- ~~`BATH_VENT`~~ — a windowless bathroom needs mechanical ventilation (R303.3).
  The DSL can't model fans, so it's a reminder, not a hard check.
- ~~`HALL_DEADEND`~~ — a hallway opening onto ≤ 1 room isn't earning its
  footprint. Gated: a hall carrying an exterior entry (a foyer/vestibule) is
  exempt, and a hall connecting two rooms (a pass-through) doesn't fire. Extended
  in the second review round to also flag a hall that runs past its last doorway
  into a blank wall (a dead-end stub).

## Widened `program` directive — DONE
`NO_LAUNDRY` / `NO_DINING` were too soft to emit unconditionally (an eat-in
kitchen has no dining room; a laundry is often a closet), so instead of guessing,
the `program` directive now lets the author declare the intent and the existing
`PROGRAM_MISMATCH` warning checks it:

    program 3 bed 2 bath 1 office 1 laundry area 1800

- `bed` / `bath` stay **exact** counts (catch a dropped bedroom).
- any other room type is an **at-least** requirement (a missing/short one warns;
  a surplus doesn't).
- `area <sqft>` is a minimum on the conditioned interior floor area.

The clauses round-trip through `emit_dsl` and the builder takes
`program(beds, baths, requires={...}, min_area=...)`.

## Second review round — from the HTML design-review feedback — DONE
Rules distilled from a pass through `tools/design_review` (every diagnostic that
fired was confirmed correct; the value was in the notes). All ship as `info`
nudges except `STAIR_BLOCKS_DOOR` (a real circulation defect → warning):

- ~~`HALL_TIGHT`~~ — a hall at the 3 ft code minimum; 4 ft is comfortable.
- ~~`HALL_DEADEND` stub~~ — a hall running past its last door (see above).
- ~~`NO_BACK_DOOR`~~ — a home with a single people-door wants a front *and* a
  back/side door (garage/porch doors don't count).
- ~~`BATH_OVERSIZE`~~ — a private ensuite larger than the bedroom it serves.
- ~~`STAIR_BLOCKS_DOOR`~~ (warning) — a stair footprint intruding on a doorway's
  clear floor. ~~`STAIR_WALL`~~ — a stair marooned mid-room instead of along a wall.
- ~~`DOOR_CENTERED`~~ — a swing door floating mid-wall; back it to a corner.
- ~~`WINDOW_PARTITION`~~ — a window butting an interior partition at the exterior
  wall (no room for framing/trim); pull it off the corner, space windows evenly.
- ~~half-bath sizing~~ — full bath 6×8 / half bath 5×6 minimums (folded into
  `ROOM_TIGHT` as area + shortest-side floors).

The four gallery plans were re-tuned to model these (4 ft halls, corner-backed
doors via `offset`, a back door each, no hall stub, the two-story stair run along
a wall) and still pin 0/0/0.

## Third review round — from a second design-review pass — DONE
Again every diagnostic that fired was confirmed correct; the value was in the
notes. Shipped:

- ~~`DOOR_SWING_CLASH`~~ — two door leaves whose swept quarter-discs overlap.
  Builds each swing the way the renderer draws it (hinge/`into` or the
  keep-inside fallback) and tests overlap with a separating-axis check, so the
  warning matches the picture. Pocket/sliding/cased doors have no arc and are exempt.
- ~~`ENVELOPE_MODULE`~~ — an exterior (envelope or wing) dimension that isn't a
  whole multiple of the 3 ft build module, for efficient sheet-goods/framing cuts.
- ~~`HALL_DEADEND` measured from the doorway~~ — the stub is now measured from the
  last *door's* position, not the room's whole abutting wall. A hall that runs
  past its last doorway flags even if a room's wall lines the rest; the cure is to
  put the end room's door at the hall end (and extend the room to cap it).

The gallery was reworked again to model all of this: 3 ft-module envelopes
(33/69/36/39), end-room doors at the hall ends, `hall_spine`'s two beds split by a
stacked pair of square walk-in closets (which also buffers sound), and an ensuite
added to `lshape`'s primary suite. All four still pin 0/0/0.

## Held for later — not modelable today
- **Stair landings / turns.** The feedback asked stairs to "land halfway and
  turn." We model a stair as a footprint + level span, not individual risers, so
  a mid-flight landing/turn can't be verified — `STAIR_WALL` + `STAIR_RUN` are the
  closest proxies. Would need a stepped stair model.
- **Even window spacing.** `WINDOW_PARTITION` catches the concrete failure (a
  window jammed against a partition); true even-spacing scoring across a façade is
  more subjective and is left out for now.
