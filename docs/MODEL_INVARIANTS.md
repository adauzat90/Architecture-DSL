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
- Every statement is either building-wide (`compiler.HOST_ONLY_STATEMENTS`, a `PART_HOST_STMT` error in a part) or part-legal (`compiler.PART_STATEMENTS`) — never silently accepted in a part and then ignored by the host.
- Fragment compilation must avoid false whole-plan diagnostics like missing envelope or missing entry.
- `use` composition validates the final composed plan after transforms, parameter substitution, and ID stamping.

## Openings, walls, and adjacency

- Doors connect two rooms or a room to an exterior wall; windows/openings belong to a room wall.
- Exterior-wall eligibility is derived from final geometry, not just the statement text.
- Opening offsets are measured along the owning wall from that wall's canonical start.
- An interior door sits where `geometry.door_span` puts it: the leaf is cut to the shared wall and the offset slid back onto it. `DOOR_FIT`/`DOOR_OOB` report the authored overrun; drawing, exports, schedules, introspection and clearance checks all use the built span. Two places keep the authored offset instead. Composition transforms it, so a stamped overrun still reports `DOOR_OOB`. The editor's drag handle and inspector read and write it, because they edit the source.
- Validators should prefer deterministic anchors: room ID, opening statement line, or `use` line for instance-level findings.

## Diagnostics

- Diagnostic codes are stable API: CLI, LSP, docs, examples, pragmas, and agents rely on exact spelling.
- Every emitted code must have a registry entry in `src/barndsl/diagnostics.py`.
- Registry entries expose severity, category, owner, title, and explanation.
- The registry severity matches every literal emit site unless the code is declared context-dependent (`_VARYING`); every code has an explicit or prefix category rule (no silent default). `barndsl dev audit` enforces both.
- Validators should emit diagnostics in stable order.
- Accepted diagnostics remain visible as audited deviations; they are not deleted from compile output.
- Error diagnostics represent unbuildable or unrecoverable model problems and should not be accepted by pragma.

## Formatting and emission

- `fmt` is line-preserving and comment-preserving; it normalizes whitespace/numbers but should not reorder statements.
- `format_source(format_source(src)) == format_source(src)` must hold.
- `emit_dsl` serializes the compiled model and intentionally drops comments.
- `compile_source(emit_dsl(plan)).plan` rebuilds the same model as `plan` — every dataclass field except source positions (`line`/`col`/`*_line`) and the non-serialised `placement` hint. This is stronger than a text fixed point, which can't see a field the first emit already dropped; `tests/test_metamorphic.py` checks it for every example and for a fixture exercising every statement option.
- `emit_dsl(compile_source(emit_dsl(plan)).plan)` is a fixed point.
- Flattened emission of composed plans should preserve resolved/stamped geometry even though source comments and `use` statements are removed; `inline_use` must keep every stamped element type.
- Known gap: `# barndsl: accept` pragmas are comments, not model state, so emission drops them and a re-emitted plan loses its accepted deviations. Carrying them through emit would need pragmas stored on the model (an open design decision; tracked as TD-14 in `docs/TECH_DEBT.md`).

## Exports and introspection

- Render/SVG, glTF, IFC, Revit exchange, schedules, cost, and packet outputs should lower the same compiled model.
- Exporters should not invent semantic IDs when stable plan IDs exist.
- `barndsl inspect --json` and `src/barndsl/introspect.py` are the agent-facing source of truth for resolved geometry.

## Harness expectations

- Prefer repo-native JSON helpers in `src/barndsl/devtools.py` over ad-hoc scripts.
- Use `barndsl dev locate`, `barndsl dev diag-matrix`, and `barndsl dev fixtures` before changing unfamiliar features or rules.
- Run `barndsl dev doctor` after architecture-affecting changes and add `--export-plan examples/gallery/lshape.barn` for geometry/export changes.
