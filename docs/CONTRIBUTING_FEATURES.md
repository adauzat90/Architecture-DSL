# Contributing DSL features and validation rules

This checklist is optimized for both humans and coding agents.

## Before changing code

1. Run the default maintainability gate and skim the model contracts when changing core behavior:

   ```bash
   barndsl dev doctor
   ```

   See `docs/MODEL_INVARIANTS.md` for coordinate, identity, fragment, diagnostic and emit/format invariants.

2. Before editing an unfamiliar rule/feature, locate its breadcrumbs:

   ```bash
   barndsl dev locate BEDROOM_EGRESS
   barndsl dev locate room
   barndsl dev diag-matrix
   barndsl dev fixtures
   ```

3. For a new validation rule, scaffold guidance:

   ```bash
   barndsl dev rule-scaffold NEW_CODE
   ```

4. For a new DSL statement/model feature, scaffold guidance:

   ```bash
   barndsl dev feature-scaffold feature-name
   ```

## Adding a diagnostic rule

- Add or reuse a deterministic validator in `src/barndsl/validation.py` or the relevant domain module. Put its threshold in `constants.py` (or a `Profile` field when it varies by jurisdiction), not inline.
- Wire a new function into `validation._run_full_plan_validators` (whole-plan checks) or `validation._DESIGN_QUALITY_CHECKS` (design-quality checks); both run in a fixed order.
- Emit `Issue(Severity.X, "CODE", ...)` with a literal code, in stable order.
- Register the code in `src/barndsl/diagnostics.py` with title and explanation. The registry severity must match every literal emit site; a code that genuinely fires at more than one severity goes in `_VARYING`.
- Give it a category: list it in the matching explicit set in `diagnostics.py` unless a prefix rule already classifies it. Explicit listings win over prefixes, and `barndsl dev audit` fails on any code that only reaches the default bucket.
- A room-local code that a composed part should report once (not per `use`) also belongs in `compose.PART_LOCAL_CODES`.
- Add tests with a minimal failing source and a minimal satisfied source; prefer fixtures from `docs/FIXTURE_CATALOG.md` before inventing a large plan.
- Probe quickly while iterating:

  ```bash
  barndsl dev rule-probe --source "$(cat case.barn)" --expect '{"warning":["CODE"],"absent":["OTHER_CODE"]}'
  ```

- Run:

  ```bash
  barndsl dev audit
  barndsl dev diag-matrix --out docs/DIAGNOSTIC_MATRIX.md
  barndsl dev gallery-gate examples
  barndsl dev impact --run
  ```

  The audit fails until the matrix lists exactly the registered codes, so regenerate it whenever you add or remove a code.

## Adding a DSL statement or model feature

- Parser/compiler: add the keyword and its `_parse_<name>_statement` to `compiler._STATEMENT_PARSERS`. `compiler.STATEMENT_KEYWORDS` derives from that table, and the playground, LSP, `fmt` and agent import it, so there is no second list to update.
- Classify it as building-wide (`compiler.HOST_ONLY_STATEMENTS`, rejected in a part with `PART_HOST_STMT`) or part-legal (`compiler.PART_STATEMENTS`). Tests enforce that every statement is in exactly one.
- Document it as a 2-space-indented grammar line under `Statements:` in `DSL_REFERENCE` (LSP hover is built from that block; the audit checks every keyword has one).
- Model: add/extend dataclasses in `src/barndsl/elements.py` when persistent plan state is needed.
- Emit/format: `compile(emit(plan))` must rebuild the same *model*, not just the same text. Add the statement to `KITCHEN_SINK` in `tests/test_metamorphic.py` (and to `EVERYTHING_PART` if part-legal); if `compose` stamps the new element, add it to `emit._instance_emitters` so `inline_use` keeps it.
- Validation: add rule coverage or profile thresholds if the feature has code/design implications. A statement naming a room needs an unknown-room error (like `FIXTURE_ROOM`, `DEVICE_ROOM`).
- Direct edits: if the statement names a room id, add it to `edits._ROOM_REF_FINDERS` (rename) and `edits._room_dependent_lines` (delete); `tests/test_edits.py` makes you classify every statement.
- LSP/playground: statement and anchor sets are derived; add context-aware completions, hover or code actions if useful.
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
