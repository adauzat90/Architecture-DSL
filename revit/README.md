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
    builder.py      # Revit API: create elements from the exchange
  barndsl.tab/
    Plan.panel/
      Build Plan.pushbutton/      # pick a .json or .barn → build it in the model
      Export Exchange.pushbutton/ # pick a .barn → write its .json (no model change)
```

## Requirements

- **Revit** 2022 or newer (any version pyRevit supports).
- **[pyRevit](https://github.com/pyrevitlabs/pyRevit)** installed.
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

**JSON-first (no Python in Revit needed):**
1. In a terminal: `barndsl revit examples/cedar_ridge.barn --out cedar_ridge.json`
2. In Revit: **barndsl → Plan → Build Plan**, pick `cedar_ridge.json`.

**Live (.barn) — needs barndsl on the CPython engine:**
- **barndsl → Plan → Build Plan**, pick a `.barn`; it compiles and builds in one
  step.
- **Export Exchange** picks a `.barn` and writes its `.json` next to it without
  changing the model — useful for inspecting what *Build Plan* will create.

## What gets built

| Exchange | Revit |
|---|---|
| `levels` | Reused if one exists at the same elevation, else a new `Level`. |
| `walls` | A `Wall` per segment, on its level, at its height. `exterior` picks an Exterior-function wall type; interior picks an Interior one (falls back to any basic type). |
| `openings` (doors/windows) | A hosted `FamilyInstance` on the matched wall, using the first loaded door/window family symbol. Window sill heights are applied. |
| `rooms` | A `Room` placed at each seed point once walls enclose it, then named. |
| `structure` | Structural columns at posts and framing along beams — **only if** structural-column / structural-framing families are loaded; skipped with a note otherwise. |

The whole build runs in **one transaction**, so Revit's undo rolls it back in a
single step. Every element is created defensively: if one fails (e.g. a missing
family), it's recorded as a note in the output and the rest still build.

### Known limitations (scaffold stage)

- Openings use their **family's default width/height** — the exchange carries the
  intended width, but door/window sizes are a *type* property in Revit, so swap
  to an appropriately sized family type after building if it matters.
- Wall centrelines sit on the barndsl room-rectangle edges; Revit applies each
  wall type's thickness about that centreline. For exact interior dimensions,
  set the wall **Location Line** to a finish face, or model with thin types.
- Rooms only place where walls actually enclose the seed point. A plan whose
  rooms don't fully tile the footprint may leave some seeds unplaced (reported).
- Porches and stairs are carried in the exchange as reference `areas` but are not
  yet instantiated by the builder.

## Testing

The Revit-free half (`exchange.py`) is covered by the repo's normal test suite
(`tests/test_revit_exchange.py`), which validates it against documents the core
actually emits. `builder.py` needs a running Revit and is exercised manually.
