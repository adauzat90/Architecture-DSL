# Contributing DSL features and validation rules

This checklist is optimized for both humans and coding agents.

## Before changing code

1. Run the default maintainability gate:

   ```bash
   barndsl dev doctor
   ```

2. For a new validation rule, scaffold guidance:

   ```bash
   barndsl dev rule-scaffold NEW_CODE
   ```

3. For a new DSL statement/model feature, scaffold guidance:

   ```bash
   barndsl dev feature-scaffold feature-name
   ```

## Adding a diagnostic rule

- Add or reuse a deterministic validator in `src/barndsl/validation.py` or the relevant domain module.
- Emit `Issue(severity, "CODE", ...)` in stable order.
- Register the code in `src/barndsl/diagnostics.py` with title, explanation and actionable hint.
- Add tests with a minimal failing source and a minimal satisfied source.
- Probe quickly while iterating:

  ```bash
  barndsl dev rule-probe --source "$(cat case.barn)" --expect '{"warning":["CODE"],"absent":["OTHER_CODE"]}'
  ```

- Run:

  ```bash
  barndsl dev audit
  barndsl dev gallery-gate examples
  barndsl dev impact --run
  ```

## Adding a DSL statement or model feature

- Parser/compiler: update `_KEYWORDS`, parsing, recovery diagnostics and `DSL_REFERENCE`.
- Model: add/extend dataclasses in `src/barndsl/elements.py` when persistent plan state is needed.
- Emit/format: keep round-trips stable via `emit.py` and `fmt.py`.
- Validation: add rule coverage or profile thresholds if the feature has code/design implications.
- LSP/playground: statement sets are audited; add context-aware completions, hover or code actions if useful.
- Direct edits: update `edits.py` if agents/users should manipulate the feature structurally.
- Exports: update SVG/views/glTF/IFC/Revit/cost/schedule when geometry, quantities or BIM data changes.
- Tests: include parser, validation, emit round-trip, LSP/playground and export parity coverage as applicable.

Check wiring with:

```bash
barndsl dev feature-check feature-name
barndsl dev audit
```

## Final confidence gates

Use the fast default gate for most changes:

```bash
barndsl dev doctor
```

Use the stronger gate after larger changes:

```bash
barndsl dev doctor --run-impact --export-plan examples/gallery/lshape.barn
```

If examples or diagnostic behavior intentionally changed, inspect the JSON from:

```bash
barndsl dev diag-diff examples --after examples
```

and update tests/docs with the reason.
