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

Possible follow-ups: Tier 4 IFC export (the same exchange lowered to
IfcWall/IfcSlab/IfcRoof via an optional `ifcopenshell` dep); billboarded room
labels in the viewer; true swept gable-wall pentagons instead of the box + infill
approximation.

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
