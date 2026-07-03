# Product review — easy-to-implement features

A product-owner pass over barndsl (2026-06-29), looking specifically for
**high-value, low-effort** features. The engine is mature: a 2,300-line
IRC-based validator, a 3-topology auto-layout solver, a full pyRevit exchange
with round-trip, and a Claude design loop. The findings below are deliberately
*not* "build a bigger engine" — they are the cheap wins that close the gap
between that engine and what a user actually touches.

## Status (update)

Everything below has since been implemented: `schedule` (#2), `fmt` (#3), `watch`
(#4), `new` (#5), `--strict` (#6), `build --json` (#7), PNG/PDF (#8), DXF (#9), and
dimensioned SVG (#10). **`cost` (#1) shipped 2026-07-03** — `barndsl cost`
(`barndsl/cost.py`), an overridable rate table × the `metrics()` takeoff. The
notes below are kept as the original review.

## Headline finding

The **validation** and **Revit** layers are deep; the **authoring loop and the
non-Revit outputs are thin**. A user who isn't going to open Revit gets an SVG
and a metrics block — and nothing they can hand to a builder, a lender, or a
spreadsheet. Most of the gaps below are a few dozen lines because the data
already exists (`plan.metrics()`, `emit_dsl()`, the opening/room model); they
just aren't *exposed*.

## Effort / impact summary

| # | Feature | Effort | Impact | Why it's cheap |
|---|---------|--------|--------|----------------|
| 1 | `barndsl cost` — estimate from the takeoff | S | High | `metrics()` already has sqft, wall area, roof area, beam ft |
| 2 | Door/window/room **schedules** (CSV/MD), no Revit | S | High | The opening + room model is already built for the Revit doc pass |
| 3 | `barndsl fmt` — canonical formatter | XS | Med | `parse → emit_dsl` already round-trips; just write it back |
| 4 | `barndsl watch` — recompile/render on save | S | High | Tightens the core author loop; pure stdlib polling |
| 5 | `barndsl new` — scaffold a starter `.barn` | XS | Med | Onboarding; emit one gallery plan as a template |
| 6 | `--strict` (warnings → error exit) | XS | Med | CI gating; one branch in the exit-code logic |
| 7 | `build --json` — metrics/diagnostics as JSON | XS | Med | `to_dict()` + `metrics()` already exist |
| 8 | PNG/PDF render output | S | Med | One optional dep (`cairosvg`); SVG already generated |
| 9 | DXF/CAD export | M | High | Architects live in DXF; lines+text map cleanly from the IR |
| 10 | Bedroom/room **dimension labels & area** on SVG | S | Med | Renderer already places room labels |

(S = a few dozen lines, XS = trivial, M = a focused day.)

---

## 1. `barndsl cost` — material/cost estimate (top roadmap item, now cheap)

Cost estimation is already on the roadmap, and `Barndominium.metrics()` already
returns everything a first-cut estimate needs: `footprint_sqft`,
`interior_sqft`, `exterior_wall_area_sqft`, `roof_area_sqft`, `beam_linear_ft`,
`post_count`, plus bed/bath counts. A simple, **transparent** estimator —
unit costs × quantities, with a user-overridable rate table (a small JSON/TOML
sidecar) — turns the existing takeoff into a number a user can act on.

```
$ barndsl cost examples/cedar_ridge.barn
Item                    Qty        Rate        Cost
Slab (footprint)        2400 sqft  $7/sqft     $16,800
Roof                    2760 sqft  $9/sqft     $24,840
Exterior shell          ...        ...         ...
Frame (beam)            420 ft     $22/ft      $9,240
-----------------------------------------------------
Estimated subtotal                             $XXX,XXX
  ± regional factor, finishes not included
```

Ship it with a loud "rough order-of-magnitude, not a bid" disclaimer (same
posture as the frame "layout aid, not engineered design" note). Defensible,
deterministic, no API key — exactly the project's house style.

## 2. Schedules without Revit (door / window / room)

The Revit *Document* pass produces native door/window/room schedules — but
that value is **locked behind owning Revit**. The same data (`plan.doors`,
windows, rooms with type/area, wall hosting) can emit a plain
**CSV or Markdown schedule** straight from the compiler:

```
$ barndsl schedule examples/cedar_ridge.barn --rooms --openings --csv
```

This is the single biggest "give the non-Revit user something to hand off"
win, and it reuses the exact model the Revit doc pass already walks. A room
schedule (id, type, dimensions, area, exterior walls) and an opening schedule
(door/window, host wall, width, height, sill) are both already computable —
see `_print_coords` in `cli.py`, which is 80% of the room schedule already.

## 3. `barndsl fmt` — a canonical formatter

`emit_dsl(plan)` already serialises a parsed plan back to canonical DSL, and
the gallery test proves the round-trip is stable. A formatter is therefore
almost free: `compile → emit_dsl → write back` (with `--check` for CI). This
gives the DSL the table-stakes tooling every language is expected to have, and
it makes the agent/auto-layout output and hand-authored files converge on one
style. Genuinely ~15 lines on top of what exists.

## 4. `barndsl watch` — recompile/render on save

The whole pitch is a tight "edit → compile → see diagnostics → fix" loop, but
today the user re-runs the command by hand each time. A `watch` subcommand
(poll mtime, re-run compile + optional `--out` render, clear screen, print the
report) makes that loop feel like a real authoring tool. Pure stdlib; no new
dependency.

## 5. `barndsl new NAME` — scaffold a starter file

There is no "get started" command. `barndsl new cedar.barn` that writes a
minimal valid plan (a trimmed gallery example) drops the time-to-first-compile
to seconds and gives a newcomer a known-good thing to edit. The gallery plans
already exist as templates.

## 6. `--strict` / warnings-as-errors

`compile` exits non-zero only on errors. A `--strict` flag that also fails on
warnings (and `--strict-info` for the design nudges) lets a team gate a plan in
CI — "this plan must stay 0/0/0," which is exactly the bar the gallery test
already enforces internally. One conditional in the exit-code path.

## 7. `build --json`

`compile --json` exists but `build` has no machine-readable output. Adding
`--json` to `build` (diagnostics via `to_dict()` + the `metrics()` takeoff)
lets the SVG-rendering path also feed dashboards/tooling without re-running
compile separately.

## 8. PNG / PDF output

`save_svg` is the only render target. Many downstream consumers (chat, docs,
PRs, lenders) want a raster or PDF. With `cairosvg` as an optional extra,
`build --format png|pdf` is a thin wrapper over the SVG already produced. Keep
it optional so the core stays dependency-free.

## 9. DXF / CAD export

A notch up in effort, but high impact: architects and drafters work in DXF.
The IR is rectangles, lines, and labelled openings — all of which map cleanly
to DXF entities (LINE/LWPOLYLINE/TEXT on layers per category). `ezdxf` makes
this a focused day's work and meaningfully widens the addressable user beyond
"Revit owner" and "SVG viewer."

## 10. Dimensioned SVG

The renderer labels rooms; adding **room dimensions and area** inside each
rectangle (and overall envelope dimension strings) makes the SVG a usable
working drawing rather than a diagram. The geometry is all present; this is a
renderer-only change.

---

## Suggested sequencing

1. **Quick DX batch (a single afternoon):** `fmt` (#3), `new` (#5), `--strict`
   (#6), `build --json` (#7). Four small, independent, high-confidence wins
   that round out the CLI.
2. **Hand-off batch:** schedules (#2) then `cost` (#1) — the two features that
   give a non-Revit user something to *deliver*. `cost` is the roadmap's named
   next item and is now cheap.
3. **Loop & output polish:** `watch` (#4), dimensioned SVG (#10), PNG/PDF (#8).
4. **Reach:** DXF (#9) when widening beyond the current two output audiences.

Everything here builds on data structures that already exist; none requires
touching the validator or the Revit exchange.
