# Model invariants

These invariants are the assumptions that let the compiler, validator, LSP, direct edits, exports, and agents agree on one plan model.

## Coordinates and geometry

- Coordinates are feet in plan space.
- Room `x,y` is the southwest/lower-left corner of the nominal room rectangle.
- Room `width` runs east-west; `length` runs north-south.
- Directions use the DSL wall names `north`, `south`, `east`, `west` and map to the corresponding rectangle edges.
- Most authoring geometry is nominal; wall thickness/render/export layers may derive faces from that nominal model.
- Level `0` is ground. Higher levels use the same x/y coordinate space unless transformed by composition.

## Room identity

- Room IDs are stable user-authored identifiers inside a plan or part.
- Composed parts stamp IDs as `<alias>.<id>`; nested parts keep nesting, e.g. `<alias>.<inner>.<id>`.
- Editor/LSP features must preserve stamped IDs and still link definitions back to the source part file.
- Do not silently strip prefixes when resolving doors, windows, fixtures, alarms, notes, or diagnostics in composed plans.

## Whole plans vs fragments

- A whole plan has a `plan` header and should satisfy whole-building requirements such as envelope, entry, access, and program checks.
- A headerless part is a fragment and must compile in fragment mode when scanned independently.
- Fragment compilation must avoid false whole-plan diagnostics like missing envelope or missing entry.
- `use` composition validates the final composed plan after transforms, parameter substitution, and ID stamping.

## Openings, walls, and adjacency

- Doors connect two rooms or a room to an exterior wall; windows/openings belong to a room wall.
- Exterior-wall eligibility is derived from final geometry, not just the statement text.
- Opening offsets are measured along the owning wall from that wall's canonical start.
- Validators should prefer deterministic anchors: room ID, opening statement line, or `use` line for instance-level findings.

## Diagnostics

- Diagnostic codes are stable API: CLI, LSP, docs, examples, pragmas, and agents rely on exact spelling.
- Every emitted code must have a registry entry in `src/barndsl/diagnostics.py`.
- Registry entries expose severity, category, owner, title, and explanation.
- Validators should emit diagnostics in stable order.
- Accepted diagnostics remain visible as audited deviations; they are not deleted from compile output.
- Error diagnostics represent unbuildable or unrecoverable model problems and should not be accepted by pragma.

## Formatting and emission

- `fmt` is line-preserving and comment-preserving; it normalizes whitespace/numbers but should not reorder statements.
- `format_source(format_source(src)) == format_source(src)` must hold.
- `emit_dsl` serializes the compiled model and intentionally drops comments.
- For supported model features, `emit_dsl(compile_source(emit_dsl(plan)).plan)` should be a fixed point.
- Flattened emission of composed plans should preserve resolved/stamped geometry even though source comments and `use` statements are removed.

## Exports and introspection

- Render/SVG, glTF, IFC, Revit exchange, schedules, cost, and packet outputs should lower the same compiled model.
- Exporters should not invent semantic IDs when stable plan IDs exist.
- `barndsl inspect --json` and `src/barndsl/introspect.py` are the agent-facing source of truth for resolved geometry.

## Harness expectations

- Prefer repo-native JSON helpers in `src/barndsl/devtools.py` over ad-hoc scripts.
- Use `barndsl dev locate`, `barndsl dev diag-matrix`, and `barndsl dev fixtures` before changing unfamiliar features or rules.
- Run `barndsl dev doctor` after architecture-affecting changes and add `--export-plan examples/gallery/lshape.barn` for geometry/export changes.
