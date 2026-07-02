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
      Build Option.pushbutton/    # pick a candidate from candidates.json → build it into the active Design Option
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
| `plan.orientation` | **Project true north** rotated to the plan's azimuth (`ProjectPosition.Angle` on the active project location) so sun studies and shadows are right. Skipped when 0; recorded as a `project` entry in the report. The sign convention (counterclockwise-positive true-north rotation) needs live-Revit confirmation. |
| `plan.siding` / `plan.roofing` | **Finish hints select types**: between the named `config.json` override (highest) and the Function auto-pick (fallback), an exterior wall type / roof type whose *name* contains a hint token (case-insensitive; `siding "metal"` → "Exterior - Metal Panel") is preferred. The report notes which path picked the type. |
| `levels` | Reused if one exists at the same elevation, else a new `Level`. |
| `walls` | A `Wall` per segment, on its level, at its height. `exterior` picks an Exterior-function wall type; interior picks an Interior one (falls back to any basic type). A **gable-end** wall (flagged `profile: gable`) builds from a vertical pentagon profile so its top rises to the ridge; a profile failure falls back to a flat wall with a note. |
| `walls[].kind` | A **declared wall kind** (`wall a - b plumbing\|bearing\|rated` in the DSL) maps to its own wall type: the named `plumbing_wall_type`/`rated_wall_type`/`bearing_wall_type` config override, else an Interior-function type whose *name* reads like the kind (plumbing/wet, rated/fire, bearing), else the standard interior type. The kind resolution is folded into those walls' rebuild fingerprints, so changing the mapping recreates exactly the declared segments. |
| `openings[].kind` (window kinds, `double`/`french` doors) | An authored **window kind** (`casement`/`slider`/`fixed`/`double-hung`) or double-leaf door prefers a family whose name reads like the kind (e.g. a `fixed` window → the "Fixed" family, `double` door → "Double-Glass"); the standard sized family stands in with a note when nothing matches. The default `casement` kind never name-matches: it always uses the standard window pick (the `window_family` override / auto-pick), so old plans build exactly as before and a casement-named family can't hijack the override. Garage-door families (garage/overhead/sectional names) are excluded from `double`/`french` matching. |
| `openings` (doors/windows) | A hosted `FamilyInstance` on the matched wall. The base door/window family is **duplicated and sized** to the exchange's width/height (a `barndsl WxH` type, cached per size), so openings come out the right size — not the family default. Window sill heights are applied. A door's authored **swing** (`swing_into`/`hinge`) flips the instance's facing/hand so the leaf opens into the named room; **egress** doors are stamped `barndsl egress` in Comments so a schedule can filter them. |
| `openings` (cased) | A `cased_opening` (the doorless walk-through) cuts a **real wall opening** (`NewOpening`: floor to the opening height, the opening wide) instead of hanging a swinging leaf. If the cut fails it falls back to the sized door family with a note (the old behaviour). Reported as kind `opening`. |
| `rooms` | A `Room` placed at each seed point once walls enclose it, then named, **numbered** (101, 102, … per level) and given default **finishes** (floor/base/ceiling/wall) by room type — a residential room schedule filled in, ready to refine. |
| `ceilings` | A flat `Ceiling` per room at its ceiling height above the level — a reflected-ceiling plane to host lighting. A **vaulted** room is skipped (open to the roof). Needs a ceiling type; skipped with a note otherwise. |
| `structure` | Structural columns at posts and framing along beams — **only if** structural-column / structural-framing families are loaded; skipped with a note otherwise. Posts rise from the floor to the **plate** (a real top level/offset, not a default stub) and bents/ridge are drawn up **at the plate**, not down on the floor. Column/beam types are **duplicated and sized** (`barndsl WxD`) from the exchange's nominal section (`Post.size`; beams share the post section in this MVP), trying common section params (`b`/`h`, `Width`/`Depth`, `d`/`bf`); a family with none keeps its default size, with a note. |
| `fixtures` | A family instance at each fixture/appliance seed — a plumbing family for wet fixtures (toilet/lavatory/tub/shower/sink), a specialty-equipment family for appliances (refrigerator/range) — **rotated to back onto the wall** the seed was laid against (assumes the family default faces north with its back south). Seeds for the designer to swap/adjust; **only if** the family is loaded, skipped with a note otherwise. |
| `slabs` | A floor slab (`Floor.Create`) per level — the footprint at ground (one per section for an L/T/U), each upper level's room extent above. |
| `foundation` | A **pad footing** (structural-foundation family) under each post of a placed frame; **only if** such a family is loaded, skipped with a note otherwise. The thickened perimeter edge (turndown / grade beam) and the rough concrete takeoff are reported for detailing. |
| `grids` | Structural grid lines (`Grid.Create`) from a placed `frame`: numbered (`1, 2, …`) along the bents, lettered (`A, B, …`) across the eaves and any interior post line. None without a frame. |
| `roof` | A footprint roof (`NewFootPrintRoof`), with its **eave edges made slope-defining** at the plan's pitch (gable ends stay vertical); a flat-roof fallback if the slope can't be applied (**experimental** — the slope call needs live-Revit validation). A plain gable/shed plan builds **one** roof over the building outline (the historic shape). A richer plan carries `roof.sections` and builds **one plane per section**: for an **L/T/U** plan, one footprint roof per footprint rectangle (so the roof follows the wing instead of one box over the concave notch — mirrors the slab pass); for a **monitor** plan, three planes — two low side sheds plus a **raised centre gable** lifted by its `base_height` onto the clerestory (built as *center gable + two shed roofs*). The clerestory stub walls that close the gap under the raised centre are **not built** — a manual refinement. Each plane is stamped separately, so an unchanged plan keeps them all. |
| `areas` (porches) | A floor slab (`Floor.Create`) from each porch outline at the ground level. |
| `areas` (stairs) | The flights the core planned for the footprint — a single **straight** run, or a **switchback** (two flights + an automatic landing) when the straight run won't fit — built via the Stairs component API. An overrun (neither fits) is built straight and flagged. Experimental: falls back to a note if the Stairs API rejects the geometry. |

Walls, openings, rooms, structure, slabs, porches, grids and the roof run in
**one transaction**, so Revit's undo rolls them back in a single step; stairs
build afterward in their own edit scopes (the Stairs API manages its own
transactions). Every element is created defensively: if one fails (e.g. a missing
family), it's recorded in the report and the rest still build.

**Re-building updates in place.** Every element a build creates is stamped
barndsl-managed in a private **Extensible Storage** schema — not the user-facing
Comments field, so the mark never clobbers your annotations and you can't
accidentally match it. The stamp also carries the element's **exchange identity**
and a **fingerprint** of the exchange record that produced it, so a re-build can
*diff* instead of starting over: an element whose record is unchanged is **kept**
— same Revit element id, so your dimensions, tags and keynotes on it stay live
across the agent's iterations — while changed elements are deleted and recreated,
and removed ones purged. Dependencies cascade correctly: an opening's fingerprint
folds in its host wall's, so a recreated wall always recreates the doors/windows
hosted on it (Revit deletes hosted instances with their host); kept rooms keep
their room numbers (new rooms number around them, never colliding). The
fingerprints also fold in the **resolved types/families and placement options**
per element kind, so changing the config (a different `exterior_wall_type`,
`door_family`, `location_line`, `size_families`, …) rebuilds exactly the
elements that config shapes — a kept element is guaranteed built with the
resources the report names. (Deliberate exception: the auto-picked ceiling type
isn't folded in — it has no config override, and a project that *loses* its
ceiling type keeps its built ceilings rather than deleting what couldn't be
rebuilt.) The per-run outcome shows in the report as **kept** alongside
created/skipped/failed.

Set `"rebuild": "full"` in the config to restore the old purge-everything-and-
recreate behaviour (every re-build then hands out fresh element ids). Set
`"replace": false` to append instead of replacing at all. Levels are always
reused, and stairs aren't purged either way. A build from an **older extension
version** is still recognised: its elements carry only the managed marker (the
legacy Comments mark or the v1 marker-only storage schema, both still read) with
no identity, so they are purged and rebuilt once — after which diffing kicks in.
Everything runs in the one build transaction (one undo step).

Needs live-Revit confirmation: the two-schema Extensible Storage dance (schemas
are immutable once created, so the identity stamp lives under a new GUID while
the v1 schema is still read); that a kept room whose bounding walls are deleted
and recreated inside the same transaction stays placed and re-bounds; and the
exact reach of Revit's host-delete cascade (the fakes mirror the documented
behaviour: hosted instances and wall cuts die with the wall).

## Build candidates as Design Options (the Build Option button)

The agent's `design()` loop doesn't produce one plan — it shortlists several
scored iterations and, today, ships only the winner. **Build Option** turns that
into *"the agent shortlisted, you choose in the model"*: it builds one chosen
candidate into a native Revit **Design Option**, so an architect can flip between
schemes, compare them in real views, and keep the one they want — the way
generated design actually wants to be consumed.

### The API limitation (verified)

**The Revit API cannot create Design Options or option sets.** `DB.DesignOption`
is read-only — there is no supported call to make an option set, add options to
it, or activate one; that is a UI-only operation in every version this extension
targets (Revit 2025 included). What the API *does* give us is the one hook that
makes this workflow possible: **any element created while a Design Option is
active in the UI is automatically assigned to that option.** So the command never
touches the DesignOption API to *write* — it just builds into whatever option you
have active, and the identity namespacing (below) keeps each candidate's elements
independent. Build Option reads the active option's name only to show it back to
you (a read-only sanity check); if that read isn't available it prints a reminder
instead.

### The workflow

1. **Shortlist and export (terminal).** Run the agent, then export each candidate
   you want to offer to its own exchange with the core CLI — one file per
   candidate:

   ```bash
   barndsl revit iteration_3.barn --out plan_a.json
   barndsl revit iteration_7.barn --out plan_b.json
   ```

   (Producing the per-candidate `.barn`/exchange files from a `design()` run is a
   core/CLI step — see the note in the batch report; this extension only consumes
   the exchange JSON.)

2. **Write a manifest** — a `candidates.json` next to those exchanges the command
   reads to build its pick-list:

   ```json
   {
     "schema": "barndsl.options/1",
     "candidates": [
       {"label": "iteration-3, score 84", "exchange": "plan_a.json"},
       {"label": "iteration-7, score 79", "exchange": "plan_b.json"}
     ]
   }
   ```

   `label` is what you pick from the list (and what namespaces the build);
   `exchange` is the candidate's `barndsl.revit/1` JSON, resolved relative to the
   manifest.

3. **Create the option set in Revit, once (UI).** **Manage → Design Options → New**
   an option set (e.g. "Schemes"), **New** an option per candidate under it, then
   select an option and **Edit Selected** to *activate* it. (This is the UI-only
   step the API can't do for you.)

4. **Build Option.** With an option active, run **barndsl → Plan → Build Option**,
   pick the manifest, then pick the candidate — its model lands in the active
   option. Switch the active option and repeat for the next candidate. Choose
   **Build** or **Preview (dry run)** just like Build Plan.

### Identity namespacing (why candidates don't collide)

Every managed element a build stamps carries an **identity key** so a rebuild can
*diff* (keep unchanged elements, recreate changed ones, purge removed ones). That
diff historically considered **every** managed element in the document — which
would be a disaster here: building candidate B would see candidate A's elements as
"removed from the plan" and purge them.

Build Option fixes this by **namespacing every identity key with the candidate's
label**. A candidate's build only ever matches — and only ever deletes — elements
in its own namespace; another candidate's (and a plain Build Plan's) elements are
left completely untouched. So:

- rebuilding the **same** candidate keeps all of its elements (Revit ids, and any
  dimensions/tags on them, survive) — the normal diff guarantee, per candidate;
- building candidate **B** after **A** never disturbs A;
- a `"rebuild": "full"` of one candidate purges and recreates only *that*
  candidate's elements.

The namespace is applied **only** when a candidate label is set. A plain Build
Plan produces byte-identical identity keys and fingerprints to before, so existing
models never recreate — the two paths coexist in the same document. The build
report (and the `*.buildlog.json`) records the candidate label the build ran
under.

Config sidecars work exactly as for Build Plan (`<exchange>.config.json` or
`barndsl_revit.config.json` beside it) — the candidate label is orthogonal to the
config, so the same template mapping can drive every candidate.

**Needs live-Revit confirmation:** that elements created by the build genuinely
land in the active Design Option (the auto-assignment hook), and that
`DesignOption.GetActiveDesignOptionId` reads the active option as expected for the
sanity-check note. The identity namespacing and cross-candidate diff isolation
themselves are pure exchange bookkeeping and are covered by the fakes tests
(`tests/test_revit_builder.py`).

## Documentation (the Document button)

Build Plan makes the *model*; **Document** makes the *drawings* from it. Run it
after a build and it creates, in one transaction:

- a floor-plan **view** per level,
- room / door / window **tags** in those views (only barndsl-managed elements),
- overall **dimension** strings on the ground plan, referencing the structural
  grids (needs a `frame`; noted and skipped when there are no grids),
- four exterior **elevations** (N/E/S/W) from one marker, and one transverse
  building **section** (both need their view types loaded; the section's
  orientation is experimental and noted if it can't be placed),
- native door / window / room **schedules**,
- a **sheet** per level with the plan placed in a viewport, plus a dedicated
  **Schedules** sheet with the schedules placed on it.

Each sub-pass can be turned off with the `dimensions`, `elevations`, `sections`,
`views`, `tags`, `schedules`, `sheets` config flags.

Views, sheets and schedules are named with a `barndsl - ` prefix and the pass is
**idempotent** — re-documenting replaces the ones a previous run made (set
`"replace": false` to append). It needs a floor-plan view type and, for sheets, a
loaded title block (Diagnostics doesn't list these yet; if a sheet is skipped,
load a title block family). Each sub-pass can be turned off with the `views`,
`tags`, `schedules`, `sheets` config flags.

## Debugging a run

Each build prints a **report** to the pyRevit output panel — a per-kind
created/kept/skipped/failed table, the types/families it used, and a list of every
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
  "plumbing_wall_type": "Interior - 6 1/8\" Partition (Plumbing)",
  "rated_wall_type": "Interior - 5 1/2\" Partition (1-hr)",
  "bearing_wall_type": "Interior - Bearing 2x6",
  "door_family": "Single-Flush",
  "window_family": "Fixed",
  "floor_type": "Generic 12\"",
  "roof_type": "Standing Seam Metal",
  "column_family": "HSS-Hollow Structural Section-Column",
  "beam_family": "W-Wide Flange",
  "plumbing_family": "Toilet-Domestic-3D",
  "appliance_family": "Refrigerator",
  "foundation_family": "Footing-Rectangular",
  "location_line": "centerline",
  "size_families": true,
  "structure": true,
  "fixtures": true,
  "foundation": true,
  "porches": true,
  "stairs": true,
  "slabs": true,
  "grids": true,
  "roof": true,
  "replace": true,
  "rebuild": "diff",
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
  parametric door/window families for best results. The same applies to
  **column/beam sections**: common section parameter names are tried (`b`/`h`,
  `Width`/`Depth`, `d`/`bf`), and a family exposing none keeps its default size.
- **Door swing flips assume the family default.** `swing_into` compares the
  placed instance's `FacingOrientation` against the side of the host wall the
  named room lies on and flips when they disagree — sound for any family. The
  `hinge far` hand-flip assumes the family default hinges at the opening's
  near (south/west) end; a family hinged the other way comes out mirrored.
  Needs live-Revit confirmation.
- **Project north's sign convention needs live-Revit confirmation.** The builder
  sets `ProjectPosition.Angle = +radians(orientation)` (true north rotated
  counterclockwise from project north by the plan's azimuth); if a live model
  shows shadows mirrored, the sign is the first thing to check.
- **Cased-opening cuts (`NewOpening`) need live-Revit confirmation.** The
  rectangle (floor → opening height, centred, opening-wide) is computed and
  harness-tested; if Revit rejects the cut, the opening falls back to a sized
  door family with a note (the previous behaviour).
- **Fixture rotation assumes the family faces +Y.** Fixtures rotate about their
  seed point so their back lands on the wall the core laid them against,
  assuming the loaded family's default faces north (back at −Y) — the common
  convention, but a family authored otherwise comes out turned.
- **Stairs are experimental.** The flight layout (straight vs. switchback, riser
  count, run length) is computed deterministically by the core's `plan_stair_runs`
  and *that* is unit-tested; the Revit instantiation of those flights is what a
  real Revit is still needed to confirm. If the Stairs API rejects the geometry,
  the stair is left for you to model (the rest of the build is fine).
- **The roof slopes its eaves (experimental).** The outline/ridge/pitch/slope are
  computed by the core's `roof_plan` (tested), and the builder makes the eave
  edges slope-defining via the model-curve mapping `NewFootPrintRoof` returns — so
  the roof builds as a gable. The mapping and slope calls still need live-Revit
  confirmation; a failure falls back to a flat roof with a note.
- **L/T/U and monitor roofs build per section (experimental).** An L/T/U plan now
  roofs each footprint rectangle separately (the roof follows the footprint, no
  longer spanning the concave notch); a **monitor** plan builds three planes (two
  side sheds + a raised centre gable). The per-plane outlines/pitches/base heights
  are computed by `roof_plan`'s `sections` (tested); the raised centre is lifted by
  building its footprint at the clerestory elevation, which — like the eave slopes —
  needs live-Revit confirmation. The **clerestory stub walls** under the raised
  centre are not generated (model them by hand). A monitor's *gable-end* walls are
  still marked to the full bounding apex (the true low-high-low monitor end
  silhouette is a manual refinement).
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
  DSL and the Revit room schedule tell the same story. To make the building's
  **overall dimension** land exactly on the barndsl footprint instead, set
  `"location_line": "finish_face_exterior"` in the config — the exterior walls'
  outside finish then sits on the footprint line (Revit uses each wall type's real
  thickness). The default is `"centerline"` (unchanged), because moving the
  exterior finish out also shifts the interior face inward by a full wall.
- **Walls stay parametric.** On a multi-storey plan, each lower wall's **Top
  Constraint** is pinned to the level above (with a top offset for the
  floor-assembly depth), so editing a level moves the walls with it rather than
  leaving them at a baked-in height. The top storey keeps an explicit height.
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

### Declared intent round-trips (exchange v2)

Some of a plan's declared **intent** can't be reconstructed from geometry, so the
exchange carries it as optional keys in the `plan` block, and `exchange_to_plan`
restores each via the matching builder method. A plan that went through Revit and
back therefore still knows what it was meant to be — in particular the declared
**program** survives, so `PROGRAM_MISMATCH` keeps guarding a bedroom deleted after
a round-trip. Each key is present only when non-default, so a plan declaring none
of them (and any older `barndsl.revit/1` document) is byte-identical to before and
loads unchanged. None of these keys is fingerprinted, so they never affect the
diff rebuild.

| `plan` key | Restores |
|---|---|
| `roof_style` / `roof_pitch` | The roof form (`gable`/`shed`/`monitor`) and the authored pitch override. The *override* is carried (not the effective pitch, which stays in the geometric `roof` block), so a plan with no override round-trips back to the default — a fixed point under `emit_dsl`. |
| `notes` | Free-text plan notes (`plan.note`). |
| `accessible` | The accessibility / aging-in-place opt-in (`plan.mark_accessible`). |
| `program` | The declared program `{beds, baths?, required?, min_area?}` (`plan.program`) — `required` keyed by room-type name. |
| `frame` | The frame *request* `{bay, span, post, ridge}` (`plan.frame`); the posts/beams themselves ride `structure` and are re-placed from the request on import. |
| `suites` / `zones` | The declared `suite`/`zone` groupings, `[{id, members}, ...]` in declaration order (`plan.suite` / `plan.zone`). Each room record also carries its resolved `zone` (suite members inherit the zone that lists their suite; first-declared wins), which the builder writes to the room's **Department** parameter — so native room schedules group by zone with no shared-parameter setup. The per-room `zone` key IS part of the room's fingerprint, so changing a room's zone re-places that room (and re-applies its parameters). |

The pure reconstruction (`exchange_to_plan`) and the name heuristics are covered
by the repo suite (`tests/test_revit_roundtrip.py`, `tests/test_revit_naming.py`);
the Revit-reading step (`builder.read_model`) needs a running Revit.

## Units (`units`)

Every exchange carries a top-level `units` field. The exchange is canonically
**feet** — Revit's internal unit — and the producer *always emits* `"units":
"feet"` (this never changes, so element fingerprints are stable and a re-export
is byte-identical). What the field buys is **acceptance**: a document may declare
metric, and both consumers normalise it to feet before anything reads a
coordinate.

- **Accepted spellings.** `feet` (canonical), and the metric aliases `meters`,
  `metres`, `m`. A missing `units` is treated as feet (legacy documents). Any
  other value is rejected, naming the value and the supported set — a
  `RevitImportError` from `exchange_to_plan`, an `ExchangeError` (a hard `load`
  failure) in the extension.
- **Conversion factor.** `1 m = 1/0.3048 ft`, rounded to 6 decimals
  (`3.28084`). Areas convert by the **square** of the factor, the lone volume
  (`foundation.concrete_yd3`) by the cube. Angles and ratios — `orientation`,
  `roof_pitch`/`pitch`, `slope_angle` — are **not** converted.
- **Where it happens.** `exchange_to_plan` (core) and `exchange.load` (extension)
  each normalise up front; the feet path is a pass-through (no copy, no
  mutation), so the common case is untouched. The Revit builder therefore stays
  entirely unit-unaware — post-normalisation everything is feet.
- **Schema-driven + fails loud.** The conversion walks a table (`_UNIT_FIELDS`)
  classifying every field of every record as length / area / volume / point /
  pass-through. A **new numeric field added to the exchange without a table
  entry raises** during conversion rather than importing an unconverted (and
  silently corrupt) value — so extending the exchange forces a matching table
  entry. The table is duplicated in `barndsl.revit` and `barndsl_revit.exchange`
  (the extension can't import the core); `tests/test_revit_units.py` asserts the
  two copies never drift.

## Testing

Everything that *can* be tested without Revit is. The Revit-free modules are
covered directly: `tests/test_revit_exchange.py` (validate against real core
output), `tests/test_revit_report.py` (counts, dry-run wording, markdown/JSON,
config round-trip), `tests/test_revit_naming.py` (the name→type/id heuristics),
`tests/test_revit_stairs.py` (the stair-flight planner), and
`tests/test_revit_model_extras.py` (floor slabs, structural grids, gable roof).

`builder.py` itself is exercised against a **fake Revit API** (`tests/revit_fakes.py`
supplies the DB/pyRevit/Stairs/views/`System` shapes, including multi-schema
Extensible Storage and the wall→hosted-instance delete cascade). `tests/test_revit_builder.py`
covers the wall-type pick, opening hosting, family **sizing** (duplicate-per-size),
the **dry-run rollback**, **named overrides**, structure/slab/porch/grid/roof/stair
passes (including the switchback → two runs + landing), the **diff re-build**
(unchanged plans keep every element id; a moved room recreates only its own
elements; a changed wall recreates its hosted openings; removed/legacy elements
are purged; `"rebuild": "full"` still purges everything), the **Design Options
candidate isolation** (a candidate build namespaces its identities, rebuilding one
candidate keeps it, building candidate B never purges candidate A, and a plain
build stays byte-identical), `diagnose`, and
`read_model`'s round-trip; `tests/test_revit_document.py` covers
the **Document** pass (views, tags, schedules, sheets, and its idempotent
re-document). These verify the builder *drives the API correctly* —
only a running Revit confirms Revit does the right thing with the calls, so still
validate in-place with **Preview** + the **build log**, and attach the
`*.buildlog.json` and **Diagnostics** output to any issue.
