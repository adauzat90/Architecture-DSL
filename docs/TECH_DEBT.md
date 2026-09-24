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
| [TD-3](#td-3-private-cross-module-imports) | Private cross-module imports | M | Medium | Done |
| [TD-4](#td-4-issue-and-severity-live-in-the-validator) | `Issue`/`Severity` live in the validator | S | Medium | Done |
| [TD-5](#td-5-validationpy-size-and-duplicated-thresholds) | `validation.py` size and duplicated thresholds | L | Medium | Open |
| [TD-6](#td-6-rule-wiring-and-profile-threading) | Rule wiring and profile threading | M | Medium | Done |
| [TD-7](#td-7-the-score-ignores-the-code-profile) | The score ignores the code profile | S | Medium | Open |
| [TD-8](#td-8-statement-vocabulary-still-partly-hand-kept) | Statement vocabulary still partly hand-kept | M | Medium | Open |
| [TD-9](#td-9-three-comment-scanners-one-wrong) | Three comment scanners, one wrong | S | Medium | Done |
| [TD-10](#td-10-embedded-web-assets-and-the-agent-module) | Embedded web assets and the agent module | M | Low | Open |
| [TD-11](#td-11-edit-payload-typing) | Edit payload typing | M | Low | Open |
| [TD-12](#td-12-package-surface) | Package surface (`__init__.py`) | S | Low | Open |
| [TD-13](#td-13-untyped-function-bodies) | Untyped function bodies | M | Low | Open |
| [TD-14](#td-14-pragmas-are-lost-on-emit) | Pragmas are lost on emit | M | Low | Blocked (design) |
| [TD-15](#td-15-tests-and-docs-hygiene) | Tests and docs hygiene | S | Low | Open |
| [TD-16](#td-16-default-swing-side-differs-between-drawing-and-validation) | Default swing side differs between drawing and validation | M | High | Done |
| [TD-17](#td-17-revit-keeps-the-door-familys-default-swing) | Revit keeps the door family's default swing | S | Low | Open (needs Revit) |

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
    `swing_bounds`, since reworked by TD-16);
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
- **Found:** TD-16, fixed separately.

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

**Resolution.**
- **Renamed in place.** Each shared helper now has a public name, where it
  already lived:

  | Module | Was | Now |
  |---|---|---|
  | compiler | `_tokenize_line`, `_Token` | `tokenize_line`, `Token` |
  | compiler | `_parse_ft_in` | `parse_ft_in` |
  | compiler | `_did_you_mean` | `did_you_mean` |
  | fixtures | `_quarter_turns`, `_door_swing_rects` | `quarter_turns`, `door_swing_rects` |
  | layout | `_add_openings` | `place_openings` (`add_openings` is a brief field) |
  | layout | `_connect_adjacencies`, `_relieve_kitchen_passthrough` | `connect_adjacencies`, `relieve_kitchen_passthrough` |
  | schedule | `_schedules` | `schedule_tables` |
  | validation | `_largest_void` | `largest_void` |
  | viewer | `_LAYER_LABELS` | `LAYER_LABELS` |
  | gltf | `_to_gltf` | `gltf_point` (`to_gltf` is the exporter) |
  | pragma | `_comment_start` | `comment_start` |

  The compiler already exposed `DSL_REFERENCE`, `STATEMENT_KEYWORDS` and
  `PLACEMENT_ANCHORS`, so its lexer helpers became public there instead of
  moving to a new module.
- **Moved.** `validation._pt_rect_dist` is now `geometry.point_rect_distance`,
  beside the other shared geometry.
- **Removed.** `edits` checks anchors against the public `PLACEMENT_ANCHORS`
  instead of the parser's `_PLACEMENT` table.
- **Seam, not a rename.**
  - `compose.compose_uses` now builds its own top-level recursion context, with
    a new `self_path` argument, and rejects a context it didn't make.
  - The compiler no longer touches `_ComposeCtx`, which stays private.
  - A test pins that a host file using itself is still one clean `USE_CYCLE`.
- **Kept that way.**
  - `barndsl dev audit` gained a `private_imports` check. Any `src/barndsl`
    module that reaches another module's `_private` name fails the audit, and
    therefore doctor, whether it imports the name or reaches it through a module
    (`from . import x; x._y`, `import barndsl.x as m; m._y`, `barndsl.x._y`).
  - The audit also reports `sources_scanned`, which is 0 outside a repo
    checkout.
  - `test_repo_audit_checks_can_fail` plants every one of those forms to prove
    each can trip.
- **Tests.** `tests/test_public_seams.py` pins the contract of each newly
  public helper that only had indirect coverage.
- **Out of scope.** Tests still import roughly 100 private names, mostly
  `agent` internals in `test_agent_loop.py`. The audit covers `src/` only.

## TD-4. `Issue` and `Severity` live in the validator

**Problem.** `diagnostics`, `pragma`, `compose`, `lsp` and `revitlog` import the
7,500-line `validation.py` just to get these two types. `fixtures` and
`validation` also import each other, which forces lazy imports inside
functions.

**Fix.** Move `Issue` and `Severity` to a new `issues.py`, and re-export them
from `validation` for compatibility. Then break the fixtures/validation cycle.

**Resolution.**
- **New module.** `issues.py` holds `Severity`, `Issue` and `ValidationReport`
  and imports nothing else from barndsl.
  - `validation` re-exports all three, so `from barndsl.validation import
    Issue` still works.
  - `compiler`, `diagnostics`, `pragma`, `compose`, `lsp`, `revitlog`,
    `agent`, `score` and `cli` import the types from `issues`. So do the tests
    and `tools/`, which lets `barndsl dev impact` map a change in `issues.py`
    or `geometry.py` to the tests that use it.
- **The cycle.** `fixtures` imported `clear_box` and `exterior_walls` from
  `validation` at module level, and `validation` imported `fixtures` back
  inside six functions.
  - The wall helpers moved to `geometry`, which `fixtures` already used:
    `exterior_walls`, `plumbing_wall_sides`, `clear_dimensions` and
    `clear_box`.
  - `fixtures` now takes nothing from `validation`, and `validation` imports
    `fixtures` once, at module level. Two `fixtures` helpers that took `Issue`
    and `Severity` as parameters, to work around the lazy import, now use the
    module's own import.
  - `validation` still exposes the moved helpers, and the wall-thickness
    constants they used, which `constants.py` promises stay reachable there.
    Only two names are no longer reachable through it, and nothing used them:
    the private `_wall_halves`, and `wall_faces_outside`, a `geometry`
    function it happened to import. `barndsl.clear_dimensions` is unchanged.
- **Other lazy imports removed.** `wallbodies` wrapped `exterior_walls` in a
  function only to import it late, and `layout` imported it inside three
  functions. Both, and `layout2`, now import it from `geometry` at module
  level. `schedule`, `introspect`, `revit` and `cli` also take the helpers from
  `geometry`.
- **Kept that way.** `barndsl dev audit` gained a `reexport_imports` check.
  - It fails when a `src/barndsl` module takes a name through a module that
    only re-exports it. It catches the same forms as the private-import check:
    `from .validation import Issue`, `from barndsl.validation import Issue`,
    `validation.Issue` after `from . import validation` or `import
    barndsl.validation as v`, and `from . import Issue` through the package.
    The message names where the name is defined.
  - The package `__init__` itself is exempt.
  - Nothing tripped it before this change.
- **Nothing else changed.** Old and new code give identical results on 456
  plans (the review corpus plus every example file):
  - all 7,122 diagnostics, every field;
  - the SVG, DXF and Revit exports;
  - door schedules, introspection and the score;
  - each room's exterior walls, clear box and fixtures.
- **Tests.** `tests/test_issues_module.py` checks that:
  - the types load from `issues.py` alone, and it has no barndsl imports;
  - the old import paths give the same objects;
  - `issues`, `geometry` and `fixtures` never reach `validation`, in any
    import form, inside a function, or through another module.

  `test_repo_audit_checks_can_fail` plants every re-export form, and
  `test_defined_names_counts_every_module_level_binding` covers what counts as
  defined. Each new check failed on a planted regression.
- **Independent review.** No bugs found. Its follow-ups are in: the tests and
  the impact map above, the names `validation` still exposes, the stale
  parameters, the wider layering test and audit check, and wording.
- **Left.** Other import cycles remain, all through imports inside functions;
  none is a module-level cycle:
  - `validation`, `render` and `schedule`, through `render.fmt_ft_in`,
    `validation.loft_guard_edges` and `validation.window_tempered_reason`;
  - `elements` with `fixtures`, `geometry`, `solar` and `structure` (model
    methods that call helpers);
  - `compiler`, `compose` and `pragma`.

## TD-5. `validation.py` size and duplicated thresholds

**Problem.**
- **Size.** `validation.py` is about 7,250 lines (after TD-4), with about 380
  top-level functions and about 90 module-level constants. Meanwhile `constants.py`
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

**Plan.** Three work packages:
- **TD-6a:** check wiring and profile threading. Done; see below.
- **TD-6b:** the rule metadata on `CodeInfo`. **Decided by the project owner
  (2026-09-24):** `CodeInfo` gets the `hint` field ADR 0002 promises; the ADR
  stays as it is. Done; see below.
- **TD-6c:** fold the regex guards into the audit, and sort the diagnostics.
  **Decided by the project owner (2026-09-24):** the final diagnostics list is
  sorted, even though that changes the output order. It is sorted once, in
  `compile_source` after pragmas apply, when every diagnostic has its final
  line. The order is:
  1. line, with diagnostics that have no line last;
  2. column;
  3. severity (errors, then warnings, then infos);
  4. code.

  The sort is stable, so ties keep the order they were emitted in. Until
  then nothing checks the registry order, which is the report order: the
  TD-6a review moved three checks and every test still passed. After the
  sort, the registry order only breaks ties. Done; see below.

**Resolution (TD-6a).**
- **One context.** `validation.CheckContext` carries the plan and the profile,
  plus the door graph and rooms-by-id. The derived state is computed on first
  use and shared, as the design-quality driver used to do. The context is
  frozen, so no check can swap the profile under the checks that run after it.
- **One signature.** All 78 checks take `(ctx, add)`: 41 shell, site and
  whole-plan checks, and 37 design-quality checks. Each reads what it needs
  from `ctx`; helpers still take plain arguments.
  - `fixtures.validate_fixtures` sits below `validation` (TD-4), so it keeps
    `(plan, add)`, and a one-line adapter registers it.
- **One list.** `_SHELL_CHECKS` (shell and site; they run even on an empty
  plan) and `_PLAN_CHECKS` (everything that needs rooms) list every check in
  run order.
  - `validate()` runs the first, stops at `EMPTY` on a plan with no rooms,
    then runs the second.
  - The design-quality checks sit inline where their driver ran.
  - `_run_full_plan_validators`, `_DESIGN_QUALITY_CHECKS` and
    `_validate_design_quality` are gone, and so are the special case for
    `_dq_hall_tight` and the `len == 37` test.
- **Profile threading.** No check or helper has a `profile` default any more.
  Only `validate()` turns `None` into `DEFAULT`, and `CheckContext` requires a
  profile. `_validate_site_features` no longer takes the profile it ignored;
  `Profile` has no site fields.
- **Nothing else changed.** Old and new code give identical diagnostics, in
  the same order and with every field, and the same score. That covers 456
  plans (the review corpus plus every example file) under all three built-in
  profiles: 7,122 diagnostics under `default`, 7,274 under `strict` and 7,118
  under `rural`.
- **Tests.** `tests/test_check_registry.py` (formerly
  `test_design_quality_structure.py`) checks that:
  - every function whose first parameter is `ctx` or a `CheckContext` is
    listed exactly once, so a check can't be written and never run;
  - every listed check takes `(ctx, add)`;
  - nothing defaults the profile, and only `validate()` and `_amended` name
    `DEFAULT`;
  - the context is frozen;
  - `validate()` runs the lists in order, hands every check the caller's
    profile (or `DEFAULT`), and stops an empty plan after the shell checks.

  It keeps the tests that run one check on its own. Each of these checks
  failed on a planted regression.
- **Independent review.** It found no bugs. It compared every rewritten check
  body by AST, rebuilt the old run order, and compared 541 plans (every
  `.barn` file plus the sources embedded in tests) under all three profiles.
  Its follow-ups are in:
  - the frozen context;
  - the wider registry and `DEFAULT` tests;
  - a comment in `_dq_private_passthrough` put back above the statement it
    describes;
  - the note on registry order above;
  - wording.
- **Left.** Seven checks still build their own rooms-by-id:
  `_validate_accessibility`, `_validate_window_fall`, `_validate_life_safety`,
  `_validate_electrical`, `_validate_water_heater`, `_validate_landings` and
  `_validate_suites_zones`. `_validate_life_safety` also builds its own door
  graph. They could read `ctx` instead; that is a behaviour-neutral clean-up
  for later.
- **Docs.** `CONTRIBUTING_FEATURES.md`, the `rule-scaffold` checklist and
  `MODEL_INVARIANTS.md` describe the new wiring. `DIAGNOSTIC_MATRIX.md` was
  regenerated; only its test and doc references had gone stale.

**Resolution (TD-6b).**
- **One entry per code.** `CodeInfo` gained four fields:
  - `hint`: the general answer to "how do I fix this?", which ADR 0002
    promised and the registry lacked;
  - `severities`: every severity the code can fire at, written `also=` on the
    entry;
  - `part_local`: a composed part reports the code once, for itself;
  - `accept_denied`: an `accept` pragma may never waive it.
- **Derived, not kept by hand.** `_VARYING` is gone; `varies` means "more than
  one allowed severity". `compose.PART_LOCAL_CODES` and
  `pragma.ACCEPT_DENIED_CODES` keep their names but are read off the entries.
  They hold exactly the codes they held before: 40 part-local, 1 denied, 9
  varying.
- **Category stays rule-based.** `CodeInfo.category` still comes from the
  explicit sets and prefix rules: 98 codes are listed and 138 match a prefix.
  Writing a category onto all 236 entries would add an edit for every new code
  whose prefix already classifies it, and the audit already fails on an
  unclassified code. A middle way remains open: an optional `category=` on an
  entry, falling back to the prefix rules, would retire the explicit sets
  without touching the 138.
- **Hints.** All 236 entries have one.
  - They were drafted from each code's explanation and its emit-site hints,
    then reviewed one by one.
  - Every example statement in them (62) compiles without a parse error.
  - Thresholds a profile can change say so, for example "the profile's
    daylight ratio (8% under the IRC)".
  - `barndsl explain` and LSP hover show the hint under the explanation, and
    list every severity a varying code can fire at.
- **A stricter audit.**
  - Every emit site must fire at a severity its entry allows. The old check
    skipped a varying code entirely.
  - It reads both branches of `Severity.X if … else Severity.Y`, and follows
    a same-module helper that only returns literal severities, such as
    `_no_access_severity`. No emit site with a literal code is left
    unreadable.
  - An entry with a missing or short hint fails, as a missing explanation
    already did.
  - `CodeInfo` refuses an entry whose usual severity isn't one it allows.
- **Tests.**
  - `tests/test_code_info.py` covers the hints, the allowed severities,
    `explain()`, the derived sets, and a plan firing `NO_ACCESS` at both of
    its severities.
  - `test_repo_audit_checks_can_fail` plants a disallowed severity, a
    disallowed conditional branch, one read through a helper, and a short
    hint.
  - Each check failed on a planted regression.
- **Independent review.** It found no bugs. It checked every entry's code,
  severity, title and explanation against the old code, and every allowed
  severity against the emit sites. Its follow-ups are in:
  - two inaccurate hints fixed: `BAD_WALL` (room walls take full names only)
    and `EGRESS_SIZE` (a high sill, and every R310 minimum);
  - fuller `NO_ACCESS`, `BEDROOM_EGRESS` and `STAIR_HANDRAIL` hints;
  - the stale `SETBACK` and `NO_ACCESS` explanations corrected: `building at`
    exists, and stairs do make an upper room reachable;
  - the audit reads severity helpers;
  - the post-init check.
- **Left.**
  - The `STAIR_HANDRAIL` diagnostic's own hint still says "both sides if the
    flight is wider than 44 in". IRC R311.7.8 asks for one side. Fixing it
    changes emitted output, so it is not in this package.
  - `DSL_REFERENCE` documents `street <wall>` as taking n|s|e|w, but the
    parser only accepts full wall names there.

**Resolution (TD-6c).**
- **The regex guards are gone.** `test_review_improvements.py` and
  `test_fixture_rules.py` each had a guard that scanned a few source files
  with a regex, without an explicit encoding, for codes missing from the
  registry. The audit now reads everything those regexes caught, in every
  `src/barndsl` module:
  - `_ParseError("CODE", …)`, reported as an ERROR (all 72 parser sites);
  - `Severity.X, "CODE"` side by side in a tuple, the table `revitlog`'s emit
    loop reads.

  It now sees 233 of the 236 registered codes, where it saw 218. The other
  three were invisible to the regexes too: `BRIEF_ACCEPTANCE` is emitted only
  by `tools/`, and `CLOSET_ACCESS` and `PANTRY_ACCESS` come from a lookup
  table (`_REACH_IN_ACCESS`).
- **One report order.** `issues.report_order` is the sort key. `compile_source`
  sorts the list once, in a shared last step (`_settle`) after the pragmas, on
  every return path, part compiles included. `report()`, `to_dict()` and the
  permit packet sort with the same key, so feedback the agent appends
  afterwards still lands in place. They used to sort by line then column,
  with line-less diagnostics first. `validate()` still returns check order,
  because most issues only get a line afterwards.
- **What changed.** The order, plus one reworded hint.
  - Over 456 plans under three profiles (1,368 compiles, 21,514
    diagnostics), every compile has exactly the same diagnostics as before,
    and the same score.
  - 1,336 of the compiles list them in a new order.
  - In the text report, the JSON and the permit packet, plan-level
    diagnostics with no line (`NO_BATH`, `NO_BACK_DOOR`, …) now come last
    instead of first.
  - So do the agent's own line-less notes in its revision prompt, which is
    built from `to_dict()`: the critic's `DESIGN` suggestions and `BLOCKING`
    lines, `TRUNCATED` and `NO_PROGRAM`. They used to lead the diagnostics
    block and now follow the line-anchored ones. The design loop was tuned
    against the old prompt, so if their place matters, `render_feedback`
    should list them first on purpose.
  - A part's own findings all take the host's `use` line and column, so in
    the host they sort by severity then code, not by the part's line. Each
    message still starts `in part <file>:<line>`.
    `USE_PART_INVALID`'s hint said "the errors reported above", which no
    longer holds; it now says "reported on this `use` line".
  - No existing test depended on the old order.
  - `tools/design_review.py` keeps its own sort, because its saved verdicts
    index into that order.
- **Tests.**
  - `tests/test_report_order.py` covers the key's rules and stable ties.
    Every example plan and part compiles to a sorted list. The report and
    the JSON keep the order after an append, and pragmas apply before the
    sort.
  - `test_repo_audit_checks_can_fail` plants a parse-error code and an emit
    table code that aren't registered, and a parse error naming a
    warning-only code.
  - Each check failed on a planted regression.
- **Independent review.** It found no bugs.
  - It confirmed that every return path is sorted and that nothing relied on
    the old order.
  - It re-ran the old regexes and found everything they caught in the
    audit's set.
  - Its own old-vs-new run covered every repository `.barn` file plus edge
    cases (garbage input, a recovered compile, pragmas, an empty part): 183
    compiles under three profiles, identical apart from order.
  - Its follow-ups are in:
    - the permit packet's order;
    - the `USE_PART_INVALID` hint;
    - three places that still called run order the report order;
    - the README example;
    - the notes above;
    - tie-break, column-only and parse-error severity tests.

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

**Problem.** `fmt._comment_start` and `pragma.comment_start` (public since
TD-3) are identical copies. `lsp._comment_start` is a third copy that doesn't
handle `\"` escapes, so on `note "say \" # hi" # real` it returns the `#`
inside the string. The compiler lexer has a fourth quote scanner of its own.

**Fix.** Keep one public scanner that matches the lexer, use it everywhere, and
add a test with an escaped quote.

**Resolution.**
- **One string reader.** `compiler.scan_string` holds the lexer's string rule:
  `\"` and `\\` are the only escapes, and any other backslash stays as written.
  `compiler.comment_start` is built on it.
- **Who uses it.**
  - `tokenize_line` reads strings with `scan_string`, so the lexer and the
    scanner can't disagree.
  - `fmt`, `pragma`, the LSP and `edits` all call `comment_start`.
  - The formatter's own string reader also became `scan_string`, and so did
    the LSP's `use`-path helpers (relpath, cursor-in-path, the parts and
    `with` completions).
  - The compiler's punctuation-only-line check stopped splitting on the first
    `#`, and so did the layout brief parser.
- **The bugs.**
  - **LSP.** Hover and completion misread any line with an escaped quote, in
    both directions:
    - On `note "see \" # accept NAT_LIGHT" at 1,1`, hovering `NAT_LIGHT`
      inside the string explained it as if it were a pragma.
    - On `note "a\"" at 1,1 # barndsl: accept NAT_LIGHT`, the real pragma got
      no hover and no code completion.

    A `use` path containing `\"` was also misread. All three now follow the
    lexer.
  - **Briefs.** The brief parser cut each line at its first `#`, so
    `plan "Unit #3"` came out named `Unit`.
- **Nothing else changed.** Old and new code give identical results for:
  - `fmt` on all 55 `.barn` files in the repo;
  - a 20,736-line corpus of quote/escape/`#` combinations (tokens, formatting,
    pragma parsing and compile diagnostics);
  - the 476-plan drawing and diagnostic corpus.
- **Independent review.** It compared old and new exhaustively on every line
  of up to 9 characters built from `"`, `\`, `#`, `a` and space (2.44 million
  lines). Lexer, formatter and pragma results were identical, and only the
  intended LSP fix differed.
- **Tests.** `tests/test_comment_scanner.py` covers:
  - the scanner's edge cases;
  - 28,561 generated lines, checking the lexer stops exactly where the comment
    starts;
  - every consumer sharing the one function;
  - the formatter keeping an escaped quote inside its string;
  - both LSP directions, the `use` path and the brief. Each of those tests
    fails on the old code.
- **Still separate.** The playground's JavaScript highlighter reads strings its
  own way. Its rule (skip any backslash pair) finds the same string ends, but
  it can't share Python code; that belongs with TD-10.

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

**Problem.** In 7 of the 10 whole-plan examples, at least one leaf is drawn
swinging one way and checked for clearance and clashes swinging the other way.
The 3D model disagreed with the drawing in all 10. The first two rules are each
wrong somewhere:
- **Validator.** In an L-shape (`gallery/lshape.barn`), every default-side
  interior door in the wing lies past the primary envelope, so the validator
  flips it away from the side it is drawn on.
- **Drawing.** An exterior door on a wall with a porch beyond it
  (`gallery/hall_spine.barn`, `gallery/homestead.barn`) is drawn swinging
  *outward* onto the porch, because the porch widens the bounds.

**Decision (2026-09-23).** Exterior doors swing into their room. An interior
door with no `into` swings toward `+x`/`+y`, unless the leaf doesn't fit in the
room on that side and does in the other.

**Resolution.**
- **One rule.** `drawing.swing_side` and `drawing.inward_side` hold the rule,
  and `drawing.door_leaf` no longer takes bounds.
- **Every consumer uses it:**
  - the SVG plan and the DXF export (through `door_symbols`);
  - the validator's swing regions (`DOOR_SWING_CLASH`, `DOOR_SWING_UNSET`);
  - `CLOSET_DOOR_SWING`, which now judges the side the leaf is drawn on
    instead of any side the renderer "might pick";
  - the glTF/3D door leaves (`gltf._leaf_out`).
- **The drawing's bounds** now only place a sliding panel or a folded bifold,
  which nothing else checks. They were renamed `drawing_bounds`.
- **Fixed alongside.** `DOOR_SWING_CLASH` compared swings on different levels,
  so an upstairs door could "clash" with an entry below it. Inward-swinging
  entries made that more frequent. It now compares one level at a time.
- **Gallery.** `gallery/lshape.barn` (and its copy embedded in the agent) now
  pins its master-closet door `into master`. That door had always been drawn
  swinging into the closet, the validator now says so, and gallery plans must
  compile clean. `hall_spine` and `homestead` already pin theirs.
- **Changes over the TD-2 corpus (476 plans), before the gallery edit:**
  - **Drawings: 9 plans.**
    - Six examples draw an entry beside a porch or wing swinging inward
      instead of out over it: `hall_spine`, `homestead`, `gallery/lshape`,
      `two_story`, and both `composed/cedar_ridge*`.
    - Two generated plans have leaves too wide for either room. They now keep
      `+y`, where the old bounds rule had turned them.
    - The porch probe also changes.
  - **Diagnostics: 4 plans.**
    - Both L-shape examples' `DOOR_SWING_UNSET` now describe the side each
      door is drawn on.
    - Three `DOOR_SWING_CLASH` findings that existed only on the undrawn side
      went away.
  - **3D model.** It now agrees with the plan on every leaf of every example.
- **Tests.** `tests/test_swing_rule.py` covers:
  - the rule on both wall orientations and at its fit boundaries;
  - 3D and plan agreement, with every 3D leaf matched to a drawn one, and
    every exterior leaf opening into its room, for every example;
  - the plan, the 3D model and the swing checks agreeing when:
    - a leaf is too wide for its closet;
    - a pair is fitted leaf by leaf;
    - a north entry swings inward;
  - no clash between levels.

  Nine deliberately introduced bugs, each making one consumer drift back to a
  rule of its own, all fail it.
- **Not covered: Revit.** See TD-17.

## TD-17. Revit keeps the door family's default swing

**Problem.** With no `into`, the Revit builder
(`revit/barndsl.extension/lib/barndsl_revit/builder.py`, `_flip_door_swing`)
leaves a door at its family's default facing. The Revit model can then swing a
default-side door differently from the plan, the DXF, the validator and the 3D
model (TD-16).

**Fix.** Add the resolved side to the `barndsl.revit/1` opening. Use a new
field, not `swing_into`, so exchange round trips don't invent `into` clauses.
The exporter half (`revit.to_revit_model`) can be built and tested here. The
builder half needs a machine with full Revit.

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
- **TD-2** (PR #26): one drawing geometry, `drawing.py`, for the SVG plan and
  the DXF export; fixed the DXF's ignored `hinge far`.
- **TD-16** (PR #27): one default door-swing rule for the plan, DXF,
  validator and 3D model.
- **TD-3** (PR #28): no module imports another's private names; `barndsl dev
  audit` enforces it.
- **TD-9** (PR #29): one comment scanner, the lexer's own
  (`compiler.comment_start` on `scan_string`); fixed the LSP treating text
  after `\"` as a comment.
- **TD-4** (PR #30): the diagnostic types live in `issues.py`; `fixtures` no
  longer imports `validation`; `barndsl dev audit` flags imports through a
  re-export.
- **TD-6a** (PR #31): every validator check takes one context and is listed
  once, in run order; no profile defaults.
- **TD-6b** (PR #32): everything about a diagnostic code lives on its registry
  entry, including a general fix hint for all 236 codes.
- **TD-6c**: the regex registry guards folded into the audit; a compile
  reports its diagnostics in one order (line, column, severity, code).
