# barndsl → Revit (pyRevit extension)

This directory is the **Revit front-end** for barndsl. It turns a compiled plan
into a real Revit model — levels, walls, doors, windows, rooms, and (when the
right families are loaded) structural members — from a ribbon button inside
Revit.

It consumes the `barndsl.revit/1` **exchange** that the core produces
(`barndsl revit FILE --out plan.json`, or `to_revit_json(plan)` from Python). The
exchange is the contract between the two halves, so this extension never needs to
understand the DSL — only the JSON.

```
barndsl.extension/
  extension.json
  lib/barndsl_revit/
    exchange.py     # pure: load + validate a barndsl.revit/1 document (no Revit)
    report.py       # pure: build report + options (no Revit)
    naming.py       # pure: name → room-type / DSL-id heuristics (no Revit)
    builder.py      # Revit API: create elements; read a model back to an exchange
  barndsl.tab/
    Plan.panel/
      Build Plan.pushbutton/      # pick a .json or .barn → build or preview it
      Document.pushbutton/        # views + tags + schedules + a sheet per level
      Export Exchange.pushbutton/ # pick a .barn → write its .json (no model change)
      Diagnostics.pushbutton/     # report environment + available types/families
      Model to DSL.pushbutton/    # reverse: read the model → reconstruct .barn
```

The two halves are deliberately split: `exchange.py`, `report.py` and `naming.py`
are **Revit-free** (and covered by the repo's test suite), so all the
bookkeeping, validation, formatting and heuristics are verified without Revit;
only `builder.py` touches the API.

## Requirements

- **Revit 2025** — the primary, developed-against target (.NET 8). The builder
  uses APIs current in 2025 (`Floor.Create`, `ElementId.Value`, the Stairs
  component API) and avoids removed members, so recent prior versions should work
  too, but 2025 is what it's tuned for.
- **[pyRevit](https://github.com/pyrevitlabs/pyRevit) 5** or newer (the build
  that supports Revit 2025; ships the CPython 3.12 engine the buttons run on).
- For building straight from a `.barn` (and for *Export Exchange*), the
  **`barndsl` package importable from pyRevit's CPython engine** — the button
  scripts use the `#! python3` engine. If barndsl isn't on that engine's path,
  use the JSON-first workflow below instead (no Python install in Revit needed).

## Install the extension

Point pyRevit at this folder as a custom extension directory:

1. In Revit: **pyRevit → Settings → Custom Extension Directories → Add folder**,
   choose the `revit/` directory that contains `barndsl.extension`, **Save
   Settings**, then **pyRevit → Reload**.
2. Or from a terminal:
   ```bash
   pyrevit extensions paths add /path/to/Architecture-DSL/revit
   pyrevit reload
   ```

A **barndsl** tab with a **Plan** panel appears on the ribbon.

## Make `barndsl` importable in Revit (optional, for the .barn path)

pyRevit's CPython engine has its own site-packages. Install barndsl into it so
the buttons can compile `.barn` files live:

```bash
# from the repo root, using the Python that matches pyRevit's CPython engine
pip install -e .
```

If that engine can't see the package, the buttons fall back to telling you to use
the JSON-first workflow — nothing breaks.

## Workflows

**Check the project first.** Run **barndsl → Plan → Diagnostics** — it reports
the Revit / pyRevit / Python versions, whether `barndsl` is importable, and which
wall types, floor types, door/window families and structural families the
document has, with ✅/⚠️ readiness flags. Each build pass auto-picks from these,
so this tells you up front what will build and what will be skipped. It changes
nothing.

**JSON-first (no Python in Revit needed):**
1. In a terminal: `barndsl revit examples/cedar_ridge.barn --out cedar_ridge.json`
2. In Revit: **barndsl → Plan → Build Plan**, pick `cedar_ridge.json`.

**Live (.barn) — needs barndsl on the CPython engine:**
- **barndsl → Plan → Build Plan**, pick a `.barn`; it compiles and builds in one
  step.
- **Export Exchange** picks a `.barn` and writes its `.json` next to it without
  changing the model — useful for inspecting what *Build Plan* will create.

**Build or Preview.** *Build Plan* asks whether to **Build** or **Preview (dry
run)**. A preview does a *real* build and then rolls it back, so you see exactly
what would be created — and any per-element API errors — without changing the
model. Use it to shake a plan out against a project template before committing.

## What gets built

| Exchange | Revit |
|---|---|
| `levels` | Reused if one exists at the same elevation, else a new `Level`. |
| `walls` | A `Wall` per segment, on its level, at its height. `exterior` picks an Exterior-function wall type; interior picks an Interior one (falls back to any basic type). A **gable-end** wall (flagged `profile: gable`) builds from a vertical pentagon profile so its top rises to the ridge; a profile failure falls back to a flat wall with a note. |
| `openings` (doors/windows) | A hosted `FamilyInstance` on the matched wall. The base door/window family is **duplicated and sized** to the exchange's width/height (a `barndsl WxH` type, cached per size), so openings come out the right size — not the family default. Window sill heights are applied. |
| `rooms` | A `Room` placed at each seed point once walls enclose it, then named. |
| `structure` | Structural columns at posts and framing along beams — **only if** structural-column / structural-framing families are loaded; skipped with a note otherwise. |
| `slabs` | A floor slab (`Floor.Create`) per level — the footprint at ground (one per section for an L/T/U), each upper level's room extent above. |
| `grids` | Structural grid lines (`Grid.Create`) from a placed `frame`: numbered (`1, 2, …`) along the bents, lettered (`A, B, …`) across the eaves and any interior post line. None without a frame. |
| `roof` | A footprint roof (`NewFootPrintRoof`) over the building outline, with its **eave edges made slope-defining** at the plan's pitch so it comes out as a gable (the gable ends stay vertical). Falls back to a flat roof if the slope can't be applied (**experimental** — the slope call needs live-Revit validation). |
| `areas` (porches) | A floor slab (`Floor.Create`) from each porch outline at the ground level. |
| `areas` (stairs) | The flights the core planned for the footprint — a single **straight** run, or a **switchback** (two flights + an automatic landing) when the straight run won't fit — built via the Stairs component API. An overrun (neither fits) is built straight and flagged. Experimental: falls back to a note if the Stairs API rejects the geometry. |

Walls, openings, rooms, structure, slabs, porches, grids and the roof run in
**one transaction**, so Revit's undo rolls them back in a single step; stairs
build afterward in their own edit scopes (the Stairs API manages its own
transactions). Every element is created defensively: if one fails (e.g. a missing
family), it's recorded in the report and the rest still build.

**Re-building is idempotent.** Every element a build creates is stamped
barndsl-managed (in its Comments). By default a re-build first **removes the
previous barndsl build** and lays down the current one — so iterating on the
`.barn` and rebuilding *replaces* the model instead of stacking duplicates, and
never touches anything you drew by hand. It's all one undo step. Set
`"replace": false` in the config to append instead (levels are always reused, and
stairs aren't purged).

## Documentation (the Document button)

Build Plan makes the *model*; **Document** makes the *drawings* from it. Run it
after a build and it creates, in one transaction:

- a floor-plan **view** per level,
- room / door / window **tags** in those views (only barndsl-managed elements),
- native door / window / room **schedules**,
- a **sheet** per level with the plan placed in a viewport.

Views, sheets and schedules are named with a `barndsl - ` prefix and the pass is
**idempotent** — re-documenting replaces the ones a previous run made (set
`"replace": false` to append). It needs a floor-plan view type and, for sheets, a
loaded title block (Diagnostics doesn't list these yet; if a sheet is skipped,
load a title block family). Each sub-pass can be turned off with the `views`,
`tags`, `schedules`, `sheets` config flags.

## Debugging a run

Each build prints a **report** to the pyRevit output panel — a per-kind
created/skipped/failed table, the types/families it used, and a list of every
element that needs attention with the reason (e.g. `room bath — skipped: point
not in an enclosed region`). It also writes the full report as
`<source>.buildlog.json` next to the file you picked, so you can attach it when
reporting an issue. Each record carries the created Revit element id, so you can
select/zoom to it in Revit.

When something doesn't build:

1. Run **Diagnostics** — is the resource (wall type, door family, …) even in the
   project? ⚠️ flags tell you what's missing.
2. Run **Build Plan → Preview** — a no-commit dry run surfaces the exact
   per-element error without touching the model.
3. Read the **build log** — the `failed`/`skipped` records name the element and
   the reason.

## Mapping to a project template (config)

By default each pass auto-picks (exterior/interior wall type by Function, the
first loaded door/window/structural family, the first non-foundation floor type).
To pin a pass to a **named** type from your template, drop a `config.json` next
to the source (`<source>.config.json`, or `barndsl_revit.config.json` in the same
folder). Unknown keys are ignored, so you can leave comments.

```json
{
  "exterior_wall_type": "Exterior - Brick on Mtl. Stud",
  "interior_wall_type": "Interior - 4 7/8\" Partition (1-hr)",
  "door_family": "Single-Flush",
  "window_family": "Fixed",
  "floor_type": "Generic 12\"",
  "column_family": "HSS-Hollow Structural Section-Column",
  "beam_family": "W-Wide Flange",
  "size_families": true,
  "structure": true,
  "porches": true,
  "stairs": true,
  "slabs": true,
  "grids": true,
  "roof": true,
  "replace": true,
  "views": true,
  "tags": true,
  "schedules": true,
  "sheets": true,
  "verbose": false
}
```

Names come straight from **Diagnostics**. A named type that isn't found falls
back to the auto-pick with a note in the report.

### Known limitations

- **Opening sizing needs a flexible family.** Sizing duplicates the loaded
  door/window family and sets its Width/Height type parameters; a fixed-size
  family (no settable Width/Height) falls back to its default, with a note. Load
  parametric door/window families for best results.
- **Stairs are experimental.** The flight layout (straight vs. switchback, riser
  count, run length) is computed deterministically by the core's `plan_stair_runs`
  and *that* is unit-tested; the Revit instantiation of those flights is what a
  real Revit is still needed to confirm. If the Stairs API rejects the geometry,
  the stair is left for you to model (the rest of the build is fine).
- **The roof slopes its eaves (experimental).** The outline/ridge/pitch/slope are
  computed by the core's `roof_plan` (tested), and the builder now makes the eave
  edges slope-defining via the model-curve mapping `NewFootPrintRoof` returns — so
  the roof builds as a gable. The mapping and slope calls still need live-Revit
  confirmation; a failure falls back to a flat roof with a note. For an L/T/U
  footprint the outline is the bounding rectangle.
- **Gable-end walls build from a profile (experimental).** They're flagged in the
  exchange with a ridge apex and built via the vertical-profile overload of
  `Wall.Create`; if that overload rejects the geometry the wall falls back to a
  flat plate-height rectangle with a note. Needs live-Revit confirmation.
- Wall centrelines sit on the barndsl room-rectangle edges; Revit applies each
  wall type's thickness about that centreline, so a room's built **clear**
  interior is smaller than its nominal rectangle by half a wall on each side. The
  exchange now carries each room's `clear_width`/`clear_length`/`clear_area` (the
  figure Revit computes for a placed room), and the core's `ROOM_CLEAR` check
  flags any room that meets a code minimum nominally but not once built — so the
  DSL and the Revit room schedule tell the same story. For exact *nominal*
  interior dimensions instead, set the wall **Location Line** to a finish face, or
  model with thin types.
- Rooms only place where walls actually enclose the seed point. A plan whose
  rooms don't fully tile the footprint may leave some seeds unplaced (reported).

## Reading a model back (Model to DSL)

The exchange round-trips, so **Model to DSL** reconstructs `.barn` source from the
active document — the reverse of *Build Plan*. It reads each placed Revit Room
(its bounding box becomes the room rectangle; its name is mapped to a best-guess
room type) and each door/window instance (connected to rooms via FromRoom/ToRoom),
assembles a `barndsl.revit/1` exchange, and runs it through the barndsl core
(`exchange_to_plan` → `emit_dsl`). A `*.readlog.json` is written next to the
output for debugging the read.

This is **experimental** and needs the `barndsl` package on the CPython engine.
Caveats to review in the output before trusting it:

- **Room types are guessed from names** (`Master Bath` → bathroom, `Great Room` →
  living, …); an unrecognised name falls back to `other`. Rename rooms or fix the
  type in the `.barn`.
- **Rooms must be placed and bounded.** Unplaced rooms are skipped; an opening
  whose FromRoom/ToRoom isn't a known room is skipped (reported).
- **Walls and structure aren't read back** — the reconstruction is room- and
  opening-driven, and the DSL re-derives walls. Non-rectangular rooms collapse to
  their bounding box.

The pure reconstruction (`exchange_to_plan`) and the name heuristics are covered
by the repo suite (`tests/test_revit_roundtrip.py`, `tests/test_revit_naming.py`);
the Revit-reading step (`builder.read_model`) needs a running Revit.

## Testing

Everything that *can* be tested without Revit is. The Revit-free modules are
covered directly: `tests/test_revit_exchange.py` (validate against real core
output), `tests/test_revit_report.py` (counts, dry-run wording, markdown/JSON,
config round-trip), `tests/test_revit_naming.py` (the name→type/id heuristics),
`tests/test_revit_stairs.py` (the stair-flight planner), and
`tests/test_revit_model_extras.py` (floor slabs, structural grids, gable roof).

`builder.py` itself is exercised against a **fake Revit API** (`tests/revit_fakes.py`
supplies the DB/pyRevit/Stairs/views/`System` shapes). `tests/test_revit_builder.py`
covers the wall-type pick, opening hosting, family **sizing** (duplicate-per-size),
the **dry-run rollback**, **named overrides**, structure/slab/porch/grid/roof/stair
passes (including the switchback → two runs + landing), the **idempotent re-build**,
`diagnose`, and `read_model`'s round-trip; `tests/test_revit_document.py` covers
the **Document** pass (views, tags, schedules, sheets, and its idempotent
re-document). These verify the builder *drives the API correctly* —
only a running Revit confirms Revit does the right thing with the calls, so still
validate in-place with **Preview** + the **build log**, and attach the
`*.buildlog.json` and **Diagnostics** output to any issue.
