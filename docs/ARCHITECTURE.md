# Architecture overview

This is the short maintainer/agent map for extending `barndsl` safely.

## Core pipeline

1. **Source read** — `src/barndsl/compiler.py` reads `.barn` text, expands `use` composition, and tracks file/line context for diagnostics.
2. **Parse/model build** — parser statements populate dataclasses from `src/barndsl/elements.py` and builder helpers.
3. **Validation** — `src/barndsl/validation.py` and domain modules emit `Issue` objects with stable diagnostic codes.
4. **Registry/explain** — every emitted code should be registered in `src/barndsl/diagnostics.py` so CLI, LSP and playground can teach the fix.
5. **Score/introspection** — `src/barndsl/score.py` provides deterministic design scoring; `src/barndsl/introspect.py` exposes resolved geometry for tools/agents.
6. **Outputs** — render/export modules lower the same compiled plan to SVG/PDF/PNG, glTF, IFC, Revit exchange JSON and packet HTML.

## Editor and app surfaces

- **CLI** — `src/barndsl/cli.py` is the human/CI entry point.
- **LSP** — `src/barndsl/lsp.py` is stdlib-only and mostly pure functions for diagnostics, completion, hover, definition, formatting and code actions.
- **Playground** — `src/barndsl/playground.py` serves the web editor and derives statement highlighting from compiler keywords.
- **Direct edits** — `src/barndsl/edits.py` applies structured source edits while preserving comments/formatting where possible.
- **Agent harness** — `src/barndsl/devtools.py`, `tools/pi_barndsl_*.py`, and `.pi/extensions/barndsl-harness.ts` expose deterministic helper tools.
- **Navigation** — `barndsl dev locate QUERY` returns line-numbered source/test/docs/example breadcrumbs for a diagnostic code, DSL statement, CLI command, module or free-text feature.

## Composition/stamped IDs

Composed plans use `use` parts. Rooms imported from a part are stamped with a prefix like `m.bed`. Any LSP or editor feature that deals with definitions, hover, symbols or room-id completions must preserve this stamped identity and still link back to the part file.

Use:

```bash
barndsl dev lsp-smoke --strict-composed
```

to catch regressions.

## Diagnostics contract

A diagnostic rule is maintainable when:

- emitted codes are deterministic and stable;
- each code has a `REGISTRY` entry with a useful explanation/hint;
- tests include one firing plan and one satisfied plan;
- examples/gallery impact is understood;
- accepted deviations remain explicit.

Use:

```bash
barndsl dev rule-scaffold CODE
barndsl dev rule-probe --source '...' --expect '{"warning":["CODE"]}'
barndsl dev audit
```

## Feature/statement contract

A DSL statement or model feature may need updates in parser/model, validation, emit/format, docs, LSP, playground, direct edits and exports. Start with:

```bash
barndsl dev feature-scaffold NAME
barndsl dev feature-check NAME
```

Then finish with:

```bash
barndsl dev doctor --run-impact
barndsl dev export-parity examples/gallery/lshape.barn
```

Use `--export-plan` on `doctor` when geometry/export behavior changed.
