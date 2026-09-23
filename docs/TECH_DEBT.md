# Code-health backlog

This page lists the larger structural problems found by the 2026-09 review of
how rules and statements are added to barndsl. The review looked at four areas:
the statement pipeline, the rule pipeline, cross-module debt, and an
independent review of the fix. [PR #24](https://github.com/adauzat90/Architecture-DSL/pull/24)
fixed its bugs and the cheaper debt. The items below were left on purpose,
because each one needs its own work package or a design decision.

**How to use this page**
- Take one item per work package, as `AGENTS.md` requires.
- Re-check an item before starting it. Line numbers drift, so items name
  functions and modules instead.
- When an item ships, set its status to **Done**, link the PR, and keep the
  entry so the reasoning isn't lost.
- An item that needs a design decision stays **Blocked** until the decision
  is recorded (in an ADR, or in `MODEL_INVARIANTS.md` for model contracts).

Sizes use the roadmap scale: **S** is under a day, **M** is a few days, **L**
is a phase.

## Summary

| ID | Item | Size | Priority | Status |
|---|---|---|---|---|
| [TD-1](#td-1-one-interior-door-span-rule) | One interior-door span rule | M | High | Done |
| [TD-2](#td-2-dxf-depends-on-render-internals) | DXF depends on render internals | M | High | Done |
| [TD-3](#td-3-private-cross-module-imports) | Private cross-module imports | M | Medium | Open |
| [TD-4](#td-4-issue-and-severity-live-in-the-validator) | `Issue`/`Severity` live in the validator | S | Medium | Open |
| [TD-5](#td-5-validationpy-size-and-duplicated-thresholds) | `validation.py` size and duplicated thresholds | L | Medium | Open |
| [TD-6](#td-6-rule-wiring-and-profile-threading) | Rule wiring and profile threading | M | Medium | Open |
| [TD-7](#td-7-the-score-ignores-the-code-profile) | The score ignores the code profile | S | Medium | Open |
| [TD-8](#td-8-statement-vocabulary-still-partly-hand-kept) | Statement vocabulary still partly hand-kept | M | Medium | Open |
| [TD-9](#td-9-three-comment-scanners-one-wrong) | Three comment scanners, one wrong | S | Medium | Open |
| [TD-10](#td-10-embedded-web-assets-and-the-agent-module) | Embedded web assets and the agent module | M | Low | Open |
| [TD-11](#td-11-edit-payload-typing) | Edit payload typing | M | Low | Open |
| [TD-12](#td-12-package-surface) | Package surface (`__init__.py`) | S | Low | Open |
| [TD-13](#td-13-untyped-function-bodies) | Untyped function bodies | M | Low | Open |
| [TD-14](#td-14-pragmas-are-lost-on-emit) | Pragmas are lost on emit | M | Low | Blocked (design) |
| [TD-15](#td-15-tests-and-docs-hygiene) | Tests and docs hygiene | S | Low | Open |
| [TD-16](#td-16-default-swing-side-differs-between-drawing-and-validation) | Default swing side differs between drawing and validation | M | High | Open (needs a rule) |

## TD-1. One interior-door span rule

**Problem.** The span of an interior door along its shared wall is worked out
in about nine places, using three different rules:

| Rule | Where |
|---|---|
| Width and offset both clamped to the wall | `wallbodies`, `dxf`, `render`, `schedule`, two sites in `validation` |
| Nothing clamped | `validation._door_interval` (also used by `introspect`), the swing polygon in `validation`, `compose` |
| Width clamped, offset not | `revit` |

The rules only disagree when a door overruns its wall. When that happens,
the exports draw the door in different places.

**Fix.** Add one public helper in `geometry`, have every site call it, and add
a test that pins down the overrun case.

**Resolution.**
- **The rule.** `geometry.door_span(edge, door)` and its companion
  `door_offset` hold the clamped rule, which every drawing and export already
  used. They replace 14 copies, including five the review missed: two in
  `fixtures`, one in `schedule.door_rows`, and two in `validation`.
- **Behaviour change.** These now place an overrunning door where it is drawn:
  - the Revit export, and the glTF and IFC exports built from it. A Revit
    round trip now also pulls an overrun at either end back onto the wall;
    before, it only fixed a negative offset;
  - the introspection free spans;
  - the schedule's offset column;
  - the `STAIR_BLOCKS_DOOR` and `HALL_DEADEND` checks;
  - the interior swing regions.

  Only plans that already have `DOOR_FIT` or `DOOR_OOB` are affected.
- **Deliberate exceptions:**
  - `compose` still maps the authored offset, so `DOOR_OOB` survives stamping.
  - The editor's drag handle and inspector show the authored offset.
- **Tests.** `tests/test_door_span.py` covers:
  - the helper itself;
  - the public consumers (wall gaps, Revit, schedule, introspection) against
    an overrunning door; the `DOOR_OOB` case fails on the old code;
  - the swing checks against a too-wide door;
  - both exceptions.
- **Follow-up (pre-existing).** The playground draws its drag-handle line at
  the authored offset, so for an overrunning door the line runs past the wall
  while the plan's door sits clamped. The fix is to add a built-offset field to
  the overlay payload, used only for drawing, and keep `offset` authored.
- **Invariant.** Recorded in `MODEL_INVARIANTS.md`.

## TD-2. DXF depends on render internals

**Problem.** `dxf.py` builds a `render._Renderer` and calls seven of its private
methods: `_chain_breaks`, `_chain_ticks`, `_exterior_runs`, `_miter_counters`,
`_opening_jambs`, `_overall_span` and `_run_breaks`. It also re-draws the door,
bifold and overhead-door symbols by hand; its comments say "Mirrors …". The SVG
and DXF output can drift apart without any test noticing.

**Fix.** Move the world-coordinate dimension and symbol geometry into a public
module that both renderers use. Then check the two exports for the same
openings and dimension chains on the gallery plans.

**Resolution.**
- **The seam.** A new public module, `drawing.py`, computes in plan feet:
  - door symbols (`door_symbols`, built on `door_leaf`, `swing_side` and
    `swing_bounds`);
  - window symbols (`window_symbols`);
  - the exterior dimension chains (`exterior_chains` and `overall_span`, with
    the break, jamb and face-of-stud helpers public beside them).

  The counter mitres became the public `fixtures.miter_counters`.
- **Who uses it.**
  - `render` and `dxf` now only style this geometry. The DXF export no longer
    builds an SVG renderer, and it imports only `fmt_ft_in` from `render`.
  - The validator's swing regions use `door_leaf` and `swing_side`. They were a
    fourth copy of the leaf math and a third copy of the swing-side rule.
  - Three private imports are gone (see TD-3).
- **Bug fixed.** The DXF ignored `hinge far` on a single interior swing door
  and always hinged it at the near jamb, while the SVG honoured it.
- **Verification.** The corpus was 476 plans: every example, 400 generated
  plans, probes of each door kind on each wall, a porch plan and a wing plan.
  - The SVG is byte-identical in both dimension modes.
  - Every DXF flavour is byte-identical to the old code with only the one-line
    hinge fix applied.
  - Every diagnostic is unchanged.
  - An independent review repeated the check on 1,210 plans of its own,
    including multi-level plans and counter mitres, with the same result.
- **Tests.**
  - `tests/test_drawing.py` reads both outputs back for every example:
    - the DXF quarter arcs and the SVG leaf lines and arcs sit on the shared
      leaves;
    - the window lines match the shared window symbols;
    - both outputs tick every chain at the shared coordinates, in both
      dimension modes.

    It also pins the hinge regression, each door kind's geometry and the
    per-level split. Five deliberately introduced renderer bugs each failed
    it, the old hinge bug among them.
  - `test_chain_dims.py` and `test_dim_faces.py` now test the public functions.
- **Found, not fixed:** TD-16.

## TD-3. Private cross-module imports

**Problem.** 18 import sites in `src/` reach into another module's `_private`
names (measured after TD-2):

- **compiler:** `_PLACEMENT`, `_tokenize_line` and `_parse_ft_in` (used by
  edits and fmt), `_did_you_mean` (used by compose).
- **compose:** `_ComposeCtx`, used by compiler, so the two depend on each
  other's privates.
- **fixtures:** `_quarter_turns` (compose), `_door_swing_rects` (validation).
- **validation:** `_pt_rect_dist` (packet), `_largest_void` (score).
- **layout:** `_add_openings`, `_connect_adjacencies` and
  `_relieve_kitchen_passthrough`, used by layout2.
- **Others:**
  - `schedule._schedules` (packet, playground)
  - `viewer._LAYER_LABELS` (playground)
  - `gltf._to_gltf` (viewer)
  - `pragma._comment_start` (edits)

Tests and tools add roughly 100 more sites. Most are `agent` privates in
`test_agent_loop.py`.

**Fix.**
- Promote each helper that is really shared to a public name, with a test.
- Put the grammar helpers (tokenizer, length parser, placement anchors) in a
  small public module.

`AGENTS.md` says to add a public seam before relying on a private import.

## TD-4. `Issue` and `Severity` live in the validator

**Problem.** `diagnostics`, `pragma`, `compose`, `lsp` and `revitlog` import the
7,500-line `validation.py` just to get these two types. `fixtures` and
`validation` also import each other, which forces lazy imports inside
functions.

**Fix.** Move `Issue` and `Severity` to a new `issues.py`, and re-export them
from `validation` for compatibility. Then break the fixtures/validation cycle.

## TD-5. `validation.py` size and duplicated thresholds

**Problem.**
- **Size.** `validation.py` is about 7,500 lines, with about 390 top-level
  functions and about 90 module-level constants. Meanwhile `constants.py`
  claims to be the single source of truth.
- **Duplicated thresholds:**
  - The 0.85 coverage bar is written twice in `validation` and once in `score`.
  - The 1.001 overflow factor is written twice.
  - The window defaults (head 6.67 ft, sill 3 ft) are copied into
    `compiler`, `emit` and `layout` instead of read from `Window`.
  - `layout2` hard-codes `feet(3)` instead of `MIN_HALLWAY_WIDTH`.
  - A 0.75 ft porch-abutment tolerance appears eight times.
  - The 6.0 ft outlet spacing is written inline.
- **Dead or orphaned:**
  - `MIN_PORCH_DEPTH` has no rule.
  - `BRIEF_ACCEPTANCE` is only emitted by a script in `tools/`.

**Fix.**
1. Move the shared thresholds into `constants.py`, one small package at a time.
2. Then split `validation.py` by diagnostic category (geometry, openings,
   circulation, quality, electrical, site) behind the same `validate()` API.

## TD-6. Rule wiring and profile threading

**Problem.**
- **Two hand-kept dispatchers.** A new rule is wired by hand into either
  `_run_full_plan_validators` or the `_DESIGN_QUALITY_CHECKS` tuple, and the
  second also needs the hard-coded `len == 37` in
  `tests/test_design_quality_structure.py` bumped.
- **Rule metadata is scattered:**
  - `REGISTRY`
  - `_VARYING`, and the category sets in `diagnostics`
  - `PART_LOCAL_CODES` (compose)
  - `ACCEPT_DENIED_CODES` (pragma)

  `barndsl dev audit` now catches severity and category drift, but a new code
  still means editing four places.
- **Profile threading is fragile:**
  - Seven validators default `profile=DEFAULT`, so a dropped argument silently
    falls back to the IRC baseline.
  - `_validate_site_features` takes a profile and ignores it.
  - `_dq_hall_tight` gets the profile through a special case.
- **Smaller gaps:**
  - ADR 0002 says the registry holds a hint, but `CodeInfo` has no hint field.
  - Diagnostic order is stable only by convention; nothing sorts the final list.
  - The old regex registry guards (`test_review_improvements.py`,
    `test_fixture_rules.py`) overlap the audit's AST scan, and they read
    files without an explicit encoding.

**Fix.**
- Pass one check context (plan, profile, door graph, rooms by id) to every
  check, and register checks in a single ordered list.
- Let `CodeInfo` carry category, allowed severities, and the part-local and
  accept-denied flags.
- Fold the regex guards into the audit.

## TD-7. The score ignores the code profile

**Problem.**
- **Profile.** `design_score` takes no profile. Its daylight term uses the IRC
  8% (`NATURAL_LIGHT_RATIO`), but under the `strict` profile `NAT_LIGHT` flags
  rooms below 10%, so the score and the diagnostics disagree.
- **Proportion penalty.** The score and `layout2` use the same ratio bar but
  different formulas: a 0.6·worst + 0.4·mean weighting against a plain sum,
  and different constants.

**Fix.**
- Pass the profile through `design_score`, including its callers in the agent
  loop, compare and packet, and test a daylight case under `strict`.
- Share one proportion-penalty function.

## TD-8. Statement vocabulary still partly hand-kept

PR #24 made the parser's dispatch table the only list of statements. The
following items are still kept by hand:

- **Enum values in `DSL_REFERENCE`.** The door, window, light and alarm kinds,
  roofs, drive surfaces and services are typed out, as are some parser hints.
  Only room types, wall attributes and fixtures are generated from code, and no
  test links the rest to `DOOR_KINDS` and the other enums.
- **LSP completion contexts** (reported by the review; re-check before
  fixing):
  - `wall ` suggests compass directions instead of room ids.
  - `door a to ` suggests nothing.
- **Parser handler signatures.** They are inconsistent, so `roof`, `finish`,
  `climate` and `porch` keep no source line.

**Long-term fix.** A single `StatementSpec` table holding the keyword, handler,
host-only flag, room-reference slots, emitter and modifier words. Dispatch, the
host/part sets, highlighting, LSP contexts and the edit reference finders would
all be generated from it.

## TD-9. Three comment scanners, one wrong

**Problem.** `fmt._comment_start` and `pragma._comment_start` are identical
copies. `lsp._comment_start` is a third copy that doesn't handle `\"` escapes,
so on `note "say \" # hi" # real` it returns the `#` inside the string. The
compiler lexer has a fourth quote scanner of its own.

**Fix.** Keep one public scanner that matches the lexer, use it everywhere, and
add a test with an escaped quote.

## TD-10. Embedded web assets and the agent module

**Problem.**
- About 82% of `playground.py` is one HTML/JS string (`_APP_HTML`), and about
  88% of `viewer.py` is one JS string (`RENDERER_JS`). Neither can be linted
  or edited as a web file.
- In `agent.py`, about 26% is prompts and `BarndoAgent` is about 770 lines.
- The solver-seeding cluster is about 310 lines and could be separated.
- Tests import the private `_EXAMPLE_*` and `_CRITIQUE_*` prompt constants.

**Fix.**
- Move the assets into `barndsl/static/` and load them with
  `importlib.resources`, which is stdlib, plus a `package-data` entry.
- Split `agent.py` into prompts, seeding and the loop.

## TD-11. Edit payload typing

**Problem.** `edits.py` carries 59 `# type: ignore` comments. They all come from
the single `Edit` dataclass, which has about 35 optional fields shared by every
operation.

**Fix.** Use typed per-operation payloads, or a small `_req(edit, "field")`
helper that narrows the type.

## TD-12. Package surface

**Problem.**
- `barndsl/__init__.py` eagerly imports the playground (an HTTP server module)
  and the viewer, so `import barndsl` loads every exporter.
- It exports none of the newer model types (`WallSpec`, `Suite`, `Zone`,
  `Outlet`, `Switch`, `Light`, `Alarm`, `PlacedFixture`, `Note`, `UseSpec`,
  `SiteSpec`, `Requirement`).
- It doesn't export `format_source`, `apply_edit` or `STATEMENT_KEYWORDS`
  either.

**Fix.** Load the server and viewer lazily, and export the model types and
public helpers.

## TD-13. Untyped function bodies

**Problem.** `check_untyped_defs` is off in the mypy config, so mypy doesn't
check the bodies of 62 functions: 22 in `render`, 16 in `revit`, 5 in
`layout2`, and a few elsewhere.

**Fix.** Annotate those functions, then turn `check_untyped_defs` on.

## TD-14. Pragmas are lost on emit

**Problem.** `# barndsl: accept CODE "reason"` pragmas are comments, not model
state. `emit_dsl`, and every flow built on it (flatten, agent, layout, edits
that re-emit), therefore drops a plan's accepted deviations. The gap is already
recorded in `MODEL_INVARIANTS.md` under formatting and emission.

**Blocked on** a decision about storing pragmas on the model. That decision
should be an ADR, because it changes the model contract.

## TD-15. Tests and docs hygiene

- **Test layout.**
  - Some tests are named by phase: `test_phase5`, `test_phase6`,
    `test_phase7` and `test_phase13`.
  - `fmt` and `pragma` have no test module of their own; they are covered from
    `test_phase6`.
  - Splitting these by feature would make impact selection sharper.
- **Test-only code.** `agent._solver_seed_step` is used only by tests.
- **Small duplicates.**
  - Wall length is computed in three modules.
  - Rectangle overlap is computed in two, with different rectangle tuple
    formats.
  - `layout2._pick_connected_entry` builds the door graph again instead of
    calling `validation.door_graph`.
- **Stale docs.**
  - `docs/design/SITE_SOLAR.md` still calls its Phase 2 shipped and names
    functions and a test file that don't exist.
  - `docs/ARCHITECTURE.md` doesn't mention about 20 modules, including
    `layout`, `layout2`, `viewer` and `compose`.

## TD-16. Default swing side differs between drawing and validation

Found while doing TD-2. A door leaf with no `into` room gets a default swing
side, and three consumers pick it three ways:

| Consumer | Default side | Where |
|---|---|---|
| SVG plan, DXF | `+x`/`+y`, unless the leaf would pass the plan bounds widened by any porch | `drawing.door_leaf`, `drawing.swing_bounds` |
| Validator's swing checks | The same, but bounded by the primary envelope only | `validation._swing_region` |
| glTF/3D model | Toward its room: an exterior door's own room, or an interior door's first room | `gltf._swing_out` |

**Problem.** In 7 of the 17 example plans, at least one leaf is drawn swinging
one way and checked for clearance and clashes swinging the other way. The 3D
model can disagree with both. The first two rules are each wrong somewhere:
- **Validator.** In an L-shape (`gallery/lshape.barn`), every default-side
  interior door in the wing lies past the primary envelope, so the validator
  flips it away from the side it is drawn on.
- **Drawing.** An exterior door on a wall with a porch beyond it
  (`gallery/hall_spine.barn`, `gallery/homestead.barn`) is drawn swinging
  *outward* onto the porch, because the porch widens the bounds.

**Needs a decision:** the default swing rule. A likely answer is that an
exterior door swings into its room, and an interior door keeps `+x`/`+y`
unless the leaf doesn't fit in the room on that side. That is close to what
glTF does already. Once decided, put the rule in `drawing`, have the validator
and `gltf` use it, and accept the drawing, 3D and diagnostic changes on those
plans.

## Done

- **PR #24 (2026-09).**
  - Fixed:
    - The emit round trip (floor finish, and stamped stairs, suites and
      zones).
    - Rename and delete missing references.
    - The new `DEVICE_ROOM` error.
    - Host-only statements that parts silently accepted.
    - Playground anchor drift.
    - Diagnostic severity and category drift, and the stale diagnostic matrix.
  - Made `barndsl dev audit` checks able to fail.
  - Import-graph impact selection.
  - A model-level round-trip test.
  - One `PUBLIC_TYPES` and one `BUILD_MODULE`.
  - The score uses the public door graph.
  - Firing and satisfied tests for six untested codes.
- **TD-1** (PR #25): one interior-door span rule, `geometry.door_span`.
- **TD-2**: one drawing geometry, `drawing.py`, for the SVG plan and the DXF
  export; fixed the DXF's ignored `hinge far`.
