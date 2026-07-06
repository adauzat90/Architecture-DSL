# Changelog

All notable changes to barndsl are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-07-06

First stable release. barndsl 1.0 is a complete, dependency-free toolchain for
designing barndominium floor plans as text: a teaching compiler with ~190
diagnostic codes, a browser playground with touch support, professional drawing
output (SVG, DXF R2000, IFC4, glTF), schedules, an auditable cost estimator,
site plans, cross-file composition, a Language Server, and an optional
Claude-powered design agent.

The release was hardened by two adversarial persona-review cycles (a novice
homeowner, a licensed architect, a general contractor, and a developer
power-user), whose 31 confirmed findings are all either fixed or explicitly
rejected with recorded rationale (`docs/ROADMAP.md`). The suite stands at
2,174 tests.

### The language

- Plans, rooms (18 types), wings (L/T/U footprints), multi-level rooms and
  stairs, doors/openings/entries (swing/double/french/pocket/sliding/overhead/
  cased), windows (kinds, sill/head, `tempered`), porches, fixtures (21-kind
  catalog), countertop runs (`along` a wall with `from/to/depth`), post-and-beam
  frames, electrical (outlets/switches/lights/alarms), positioned notes, and
  site features (`site`, `setback`, `street`, `building`, `drive`, `walk`,
  `well`, `septic`, `service`, `grade`).
- Feet-and-inches literals everywhere (`12-6`, `12′6″`), with teaching errors
  for the ASCII `12'6"` trap and "12 feet 6 inches" phrasing.
- Cross-file composition: `use "parts/bath.barn" as b at x,y` with `level`,
  `mirror`, `rotate`, and `with` parameter passing; parts declare
  `param <name> = <default>`; nesting to depth 2 with cycle detection; a
  sandboxed resolver (relative-only, realpath-contained, size/instance caps).
- Suppression pragmas (`# barndsl: accept CODE "reason"`) that downgrade,
  never delete — with an audit trail in the permit packet.
- `emit` round-trips every statement to a fixpoint; `barndsl fmt` normalizes
  without touching comments or pragmas (idempotent, meaning-preserving,
  refuses on parse errors).

### The compiler as teacher

- ~190 registered diagnostics with severity, line/col carets, IRC citations
  where apt, and paste-ready hints; did-you-mean on misspelled statements and
  room types; `barndsl explain CODE`.
- Code checks spanning egress (R310), stairs (R311.7 runs/landings/handrails/
  headroom), safety glazing (R308.4 per sub-clause), window fall protection
  (R312.2), guards (R312.1, interior and — with `grade` — exterior),
  alarms (R314/R315), garage separation (R302.5/R302.6), habitable-room
  minimums (R304), natural light/ventilation (R303), receptacle spacing and
  kitchen-counter circuits (E3901.2/E3901.4, GFCI E3902), kitchen work
  triangle/landings, well↔septic separation, setbacks, and dozens of
  design-quality nudges. Jurisdiction profiles amend key thresholds.
- Near-linear compile scaling: a 10,000-line plan compiles in under a second
  (uniform-grid spatial index, byte-identical output to the naive loops).
- Deterministic output across runs and hash seeds.

### Drawings & deliverables

- Floor plans with double-line wall bodies (poché), door swings, window
  symbols spanning the wall band, mitred countertop corners, schedule mark
  tags (D1/W1 bubbles), north arrow, legend, and chained exterior dimensions
  that break at every opening jamb.
- Two dimension conventions: nominal room lines (default) or face-of-stud
  (`--dims faces`), with wall-thickness segments that sum to the overall.
- DXF R2000/AC1015 export, hand-written and byte-reproducible: AIA layers,
  closed hatchable wall polylines, swing arcs, declared imperial units, and
  optional associative `DIMENSION` entities (`--dxf-dims associative`).
- IFC4 (STEP/SPF) and glTF exports, both hand-written, both deterministic.
- Elevations (real doors, muntins, pitch tag, grade hatch, porches) and a
  section with slab/wall/rafter assemblies and a ceiling dimension.
- Site plan: lot, setbacks (required *and* actual clearances), drive, walk,
  well, septic with drain field, service drops, legend.
- The permit-sketch packet: cover metrics, true-scale floor plan (stated
  architectural scale, verified), electrical plan, site plan with clearance
  tables, elevations, schedules (with near-jamb offsets and glazing flags),
  cost estimate, accepted deviations, and a diagnostics appendix.
- Room/door/window schedules as Markdown or CSV.

### Numbers

- An auditable cost estimator: every line reads qty × unit with its source
  metric named; size-aware opening prices; gable ends, porches, laundry
  appliances, counters, and site work itemized; explicit exclusions footer;
  every unit cost overridable (`--costs`, discoverable via
  `cost --print-keys [--json]`); regional `--multiplier`.
- `barndsl compare` across 2..N plan files: score/metric deltas, honest
  diagnostic buckets (resolved / introduced / fewer / more), and cost deltas.
- A 0–100 design score with named components.

### Tooling

- `barndsl serve`: a stdlib playground — live diagnostics with quick-fix
  Apply, structural editing (drag/resize/nudge with surgical text edits and
  unified undo), a design panel, an offline rule-based layout generator, a
  3D walkthrough, scheme comparison, print-to-scale, and full iPad/touch
  support (pointer events, pinch zoom, coarse-pointer ergonomics).
- `barndsl lsp`: a stdlib Language Server — diagnostics (including part
  files), hover, completions, formatting, go-to-definition across `use`
  boundaries, rename, code actions. Editor wiring in `docs/EDITORS.md`.
- `barndsl layout`: a no-API-key adjacency-brief solver.
- The optional Claude agent (`pip install "barndsl[agent]"`): natural-language
  briefs to compiled plans via a compile-fix loop.
- Revit exchange (JSON) with a pyRevit add-in, export and re-import.
- CLI ergonomics: `--json` on the scripting commands, `-q/--quiet`, stdin via
  `-`, clean one-line errors with stable exit codes.

### Changed (from the 0.1 line)

- **The base install is now dependency-free.** `pydantic` and `python-dotenv`
  moved from install requirements into the `agent` extra; the engine imports
  neither. `pip install barndsl` brings in nothing else.
- Gable-end cost areas are computed on the short envelope side (the ridge
  runs the long axis); plans authored width-first previously overpriced
  gable walls up to 4×.
- Window costs split into per-unit frame/install plus per-square-foot
  glazing; garage doors price per linear foot; exterior doors weight by
  width. Flat per-kind window keys were removed.
- Openings with non-positive widths are now compile errors.

[1.0.0]: https://github.com/adauzat90/Architecture-DSL/releases/tag/v1.0.0
