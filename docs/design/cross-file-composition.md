# Cross-file composition (`use`) — design

Status: **Phase 7a + 7b + 20 implemented** (translation core + mirror/rotate
transform + composition v2). Prereqs shipped: full emit round-trip (every statement
family), `barndsl fmt` (comment-preserving), accept pragmas, ft-in literals.

Phase 20 (composition v2) shipped **parametric parts**, **nested `use` (depth 2)**
with cycle detection, and **multi-level parts** — all documented in their final
semantics in §12 below (moved out of the old Futures list). **Scheme inheritance
(`extends`)** was evaluated and **deferred** — the design sketch and the go/no-go
rationale are in §13.

7a shipped: `use "<relpath>" as <alias> at <x>,<y> [level <n>]`;
fragment-mode compile (`compile_source(..., fragment=True)`); the sandboxed
loader/resolver (`barndsl.compose`); stamping; the two diagnostic classes with
dedupe + pragma anchoring; read-only stamped members + the `add/move/set/delete/
inline_use` edits with whole-instance drag; `emit_dsl(plan, flatten=…)`; fmt
support; the `examples/composed/parts/` starter library (bath_core, master_suite,
kitchen_l, laundry_core) + the composed `examples/composed/cedar_ridge.barn`.

7b shipped: the `[mirror x|y] [rotate 90|180|270]` transform on `use` (rotate-then-
mirror in local coords; `rotate 45` / `mirror z` are teaching errors); the §5 remap
table in the stamper (geometry about the part bbox, wall directions per the table,
wall/interior-door offsets **re-derived from the transformed geometry**, fixture
rotation composed/reflected); the property tests (mirror∘mirror ≡ id, rotate⁴ ≡ id,
rotate 270 ≡ rotate 90³, the offset oracle, mirror-invariant part diagnostics);
transformed inline + flatten equivalence; `set_use` gaining `mirror`/`rotate` (with
the instance inspector's mirror/rotate selects) + transform-carrying `add_use`
(faithful Duplicate); and the playground **Parts browser** (a `parts_available`
payload the design panel lists with per-part **Insert**). cedar_ridge now stamps a
`mirror y` bath core.

## 1. Goal

Let a plan **instantiate proven, reusable blocks** — a master suite, a bath
core, a kitchen L — authored once in their own `.barn` files:

```barn
plan "Cedar Ridge"
envelope 48 x 30
ceiling 9

use "parts/master_suite.barn" as m  at 30,0
use "parts/bath_core.barn"    as b1 at 12,18
use "parts/bath_core.barn"    as b2 at 12,0  mirror y     # phase 7b

room living: living at 0,0 size 30 x 18
door living - m.bed                # host statements may reference stamped ids
```

The wins, in order: a **stdlib of parts** that encode the compiler's own rules
(a bath core that never trips `FIXTURE_TOILET_CLEARANCE`), repetition without
copy-paste drift (dual suites, motel-style bed wings), and team libraries.

**Non-goals (v1):** scheme inheritance / overrides ("base plan + variant"),
multi-level parts, parametric parts (`use ... width 14`), remote/URL parts.
Each is listed under Future in §10.

## 2. Core decision: component instantiation, not textual include

A `use` is **not** an `#include`. The part file is compiled once, in *fragment
mode*, into a typed component; each `use` **stamps** a transformed copy of that
component into the host plan at compile time. Consequences that fall out:

- Everything downstream of the compiler (render, packet, DXF/IFC/GLB, revit,
  score, compare, cost, 3D, schedules) sees ordinary rooms/fixtures/devices
  with namespaced ids — **zero changes** in those subsystems.
- Line/column diagnostics stay meaningful (§6).
- The DSL text remains the single source of truth — the host text plus the
  part texts, composed by reference, with a **flatten** escape hatch (§7).

Textual include is rejected: it breaks id uniqueness, line anchoring, and undo,
and it composes nothing (see §11).

## 3. Grammar

### 3.1 A part is a file (no block syntax)

The grammar stays flat — no `part … end` blocks. **A part file is any `.barn`
file with no `plan` header.** It contains rooms, interior doors/opens between
its own rooms, windows, fixtures, electrical devices, alarms, and positioned
notes, in its own local coordinates (SW-most corner at 0,0; the loader
normalizes and emits a `PART_ORIGIN` info if it isn't).

Host-only statements are **illegal in a part** → `PART_HOST_STMT` error, in
the teaching voice: *"`envelope` describes a whole building — a part borrows
the host's. Remove it; size the part by its rooms."* Host-only: `plan`,
`envelope`, `wing`, `ceiling`, `program`, `require`, `site`, `setback`,
`building`, `street`, `orientation`, `roof`, `overhang`, `finish`, `frame`,
`electrical`, `use` *(no nesting in v1 — see §10)*, `stair` *(multi-level —
deferred with multi-level parts)*.

A part must declare ≥1 room. Comments and accept pragmas are legal and apply
to the part's own diagnostics (§6).

### 3.2 The `use` statement (host side)

```
use "<relpath>" as <alias> at <x>,<y> [level <n>] [mirror x|y] [rotate 90|180|270]
```

- `"<relpath>"` — quoted, **relative** path, resolved against the *including
  file's* directory (§8). Absolute paths and paths escaping the root are
  `USE_UNRESOLVED` errors.
- `as <alias>` — required, a plain identifier, unique among `use` statements
  (`USE_ALIAS_DUP`). Every id inside the part is stamped as `<alias>.<id>`
  (`m.bed`, `m.bath`). **Verified: dotted ids already lex, compile, and
  resolve in references today** — no lexer change needed.
- `at x,y` — required; the stamped bounding box's SW corner in host feet
  (ft-in literals fine).
- `level n` — the host level the (single-level) part lands on; default 0.
- `mirror` / `rotate` — Phase 7b, §5.

Host statements may reference stamped ids exactly like local ids:
`door living - m.bed`, `window m.bed north width 4 offset 5` (a host-anchored
statement attached to a stamped room), `alarm smoke in m.bed`.

## 4. Compilation semantics

**Loader.** `compile_file` gains the resolution root (its own directory);
`compile_source` gains an optional `base_dir` (None → any `use` is
`USE_UNRESOLVED`, teaching: *"sources without a home directory can't resolve
parts — compile the file, or serve its folder"*). Each part file is compiled
once per top-level compile (memo keyed by resolved path), regardless of how
many times it's used.

**Fragment mode.** `compile_source(fragment=True)`: skips the `plan`/
`envelope` requirement and every whole-building check (entry/egress-per-plan,
program, site, chains…), runs all *local* checks (room sizes, fixture
clearances, opening clashes, device rules). The result caches a
`PartComponent`: rooms, openings, fixtures, devices, notes, its local bbox,
and its own diagnostics.

**Stamping.** For each `use`: transform every element (translate; 7b:
mirror/rotate), prefix every id and every internal reference with
`<alias>.`, then append to the host plan **before validation** — so overlap,
envelope bounds, egress, adjacency, chains, electrical spacing etc. all run on
the composed plan with no new code.

**Two diagnostic classes.**
- *Part-internal* (fire inside the part regardless of placement): reported
  **once per part file** (not per use), anchored to the part's own file:line —
  carried on new `Diagnostic.file` / `part` fields, message prefixed
  `in part parts/master_suite.barn:12 — …`. In the playground they anchor the
  caret to the `use` line (the nearest thing in the host buffer).
- *Instance* (placement-dependent: overlap with host rooms, out of envelope,
  adjacency…): fire per use, anchored to the **`use` statement's line** with
  the alias in the message. Accept pragmas on the `use` line accept these;
  pragmas inside the part file accept part-internal ones. `ACCEPT_UNUSED`
  semantics unchanged.

## 5. Transforms (Phase 7b)

Rooms are axis-aligned, so rotation is restricted to 90° multiples and mirror
to the two axes — both are closed over the model. The fiddly, test-heavy part
is **attribute remapping**, specified here so implementation is mechanical:

- **Composition order (decided):** with both a `rotate` and a `mirror`, the part
  is **rotated first, then mirrored** in its own local frame. The stamper builds
  the single 2×2 matrix `M = mirror · rotate`; emit, `inline`/`flatten` and the
  edit line-rebuilds all reproduce the `at …[ mirror …][ rotate …]` clause in that
  order, so the pipeline is self-consistent (`barndsl.compose._Xform`).
- Geometry mirrors/rotates about the part's local bbox (then `at` places the
  transformed bbox's SW corner — the stamp always lands where you said; a 90/270
  turn swaps the bbox width and length).
- Wall directions remap: rotate 90° ccw ⇒ S→E, E→N, N→W, W→S; mirror y ⇒
  E↔W, N/S fixed (and vice versa for mirror x). (`_ROT90_WALL` / `_MIRROR_*_WALL`.)
- **Wall-attached offsets are re-derived from the transformed geometry** (decided:
  the robust route, not composed formulas): the feature's span endpoints are
  transformed and the new offset is measured from the new wall's start corner
  (S/W convention). This reproduces `offset' = L − offset − w` for a mirror and
  handles rotations without formula-chaining, and it doubles as the property-test
  oracle. Applies to windows, exterior doors, outlets, switches. Interior doors
  between part rooms store only an offset from the shared wall's low end (their
  geometry is derived from the two rooms at validate/render time), so that offset
  is re-derived from the **transformed shared edge** (a `None`/centred offset is
  transform-invariant and stays `None`); no other remap is needed.
- Fixtures: the room-local anchor transforms as the SW corner of the footprint
  rect; `rotate` composes additively mod 360 under a rotation and **reflects under
  a mirror** — `rotation' = (−rotation) mod 360` for `mirror y` (a vertical mirror
  line negates the plan turn) and `(180 − rotation) mod 360` for `mirror x`; the
  wall backing remaps like any wall direction. Door swing `into` refs are
  id-prefixed and otherwise unchanged (side-ness is derived from geometry).
- Round-trip property tests (all implemented): stamp∘mirror∘mirror ≡ stamp;
  rotate⁴ ≡ identity; rotate 270 ≡ rotate 90³; every remapped offset re-derived
  from geometry equals the stamped offset; and a mirrored part keeps the same
  part-internal diagnostic set (clearances are mirror-invariant).

## 6. Single source of truth — the crux

The playground has one buffer, one undo history, one autosave, and an edit
engine that rewrites lines of *that* buffer. Composition must not break that.

**Decision: stamped members are read-only; instances are first-class.**

- *Instance-level operations stay fully live*, because they are host-line
  edits: dragging any member of a stamped instance drags the **whole
  instance** (rewrites `at x,y` on the `use` line — the existing `movegroup`
  ghost machinery fits exactly); the design panel shows the instance as a
  collapsible group (`▣ m — master_suite.barn`) with move/level/mirror/rotate
  fields, Delete (removes the `use` line), and Duplicate (adds a `use` with a
  fresh alias). New wire kinds: `move_use`, `set_use`, `delete_use`,
  `add_use`, `inline_use` — same shapes as the existing families.
- *Member-level edits are refused with a typed teaching error*:
  `EditError("not_editable", "m.bed is stamped from parts/master_suite.barn —
  edit that file, or Inline the instance to make it local.")` The panel greys
  member rows and shows an **Inline** button instead of the usual fields.
- **`inline_use`** is the escape hatch that keeps text sovereign: it replaces
  the `use` line with the stamped members as literal host statements
  (canonical emit style, transform applied, `alias.` prefixes kept as plain
  dotted ids so references keep working). One undo step. Part-file comments
  don't survive inlining (documented; same rule as emit).
- Editing the part *file* from the playground is **out of scope for v1** (no
  multi-buffer editor). The CLI/an external editor owns part files; the
  served page recompiles on request as always. A "parts browser" (list
  `parts/*.barn` next to the served file, click → insert a `use` at plan
  center) is a cheap 7b add.

`emit_dsl` emits `use` lines verbatim (path/alias/transform) — it does **not**
flatten; `emit_dsl(plan, flatten=True)` (and the `inline_use` edit) produce
the flattened form. `fmt` treats `use` as an ordinary statement line and never
touches other files.

## 7. Subsystem impact (complete sweep)

| Subsystem | Impact |
|---|---|
| render / views / site / 3D / DXF / IFC / GLB / revit | none — stamped elements are ordinary elements |
| score / compare / cost / metrics / schedules | none (schedules may later gain a "Part" column — optional) |
| packet | none required; optional "Parts" line on the cover (n parts, m instances) |
| edits.py | refuse member edits (typed); add `add_use`/`move_use`/`set_use`/`delete_use`/`inline_use` |
| playground server | `/api/compile`, `/api/edit`, `/api/fmt` pass the served file's `base_dir`; **Open-from-browser** files have no dir → `USE_UNRESOLVED` teaches "serve the folder" |
| playground client | instance group in panel; whole-instance drag; Inline button; parts browser (7b) |
| pragmas | line-anchoring per §4; no code change beyond the `file` field |
| fmt / emit | §6; fmt idempotence tests extended to `use` lines |
| agent / auto-layout | unchanged in v1; agent prompt may later emit `use` of stdlib parts |
| CLI | `compile/serve/packet/fmt/compare` all just work via `base_dir`; `barndsl parts DIR` lister is optional 7b |

## 8. Security & limits

The resolver is rooted at the top-level file's directory: resolve, `realpath`,
then require the result to stay under the root (symlink escapes rejected) —
`USE_UNRESOLVED` otherwise, never an OS error. No absolute paths, no `..`
above the root, no network. **The root stays the top-level host's directory at
every nesting depth** (Phase 20): a nested `use` resolves relative to the using
part's directory but the containment check is always against the host root, so a
nested `..`, a nested absolute path, a symlinked nested part dir, and a sibling dir
reached through a nested relative path are all `escape`s — never a read outside the
sandbox. Limits: `use` depth ≤ 2 (Phase 20; depth 3 is `USE_NESTED`, a cycle is
`USE_CYCLE`), ≤ 64 instances **across all depths** (one shared budget), part file ≤
256 KiB — each limit has a teaching diagnostic, not a crash. The playground server
already binds localhost-only; the resolver must not widen what it will read.

## 9. New diagnostics

| Code | Sev | Meaning |
|---|---|---|
| `USE_UNRESOLVED` | E | path missing / escapes root / no base_dir / instance cap |
| `USE_ALIAS_DUP` | E | alias reused |
| `USE_NESTED` | E | a `use` nested deeper than 2 (Phase 20) |
| `USE_CYCLE` | E | a part `use`s itself or an ancestor (Phase 20) |
| `USE_PART_INVALID` | E | part fails fragment compile (nested context attached) |
| `PART_HOST_STMT` | E | host-only statement inside a part |
| `PART_ORIGIN` | I | part's SW corner isn't 0,0 (loader normalized it) |
| `PART_EMPTY` | E | part declares no rooms |
| `PARAM_UNKNOWN` | E | a bare name is no declared param (Phase 20) |
| `PARAM_UNDECLARED` | E | a `with` key the part doesn't declare (Phase 20) |
| `PARAM_DUP` | E | a param declared/passed twice (Phase 20) |
| `PARAM_IN_PLAN` | E | `param` in a whole plan (Phase 20) |

Placement errors reuse `OVERLAP` / `OUT_OF_BOUNDS` etc., anchored per §4.

## 10. Phasing & test plan

**Phase 7a (translation-only core):** loader + fragment compile + `use … as …
at … [level]`, stamping, diagnostics/pragma anchoring, read-only members +
instance edits (`add/move/set/delete/inline_use`), whole-instance drag, emit +
fmt, security tests, docs, and a starter `parts/` library (bath core, master
suite, kitchen L, laundry core — each compiling clean *and* stamped into a
gallery example). Tests: everything in §4–§9 positive+negative, resolver
escape attempts, memoization (one part compile for N uses), inline round-trip
(inline → compile ⇒ identical plan modulo `use` bookkeeping), fmt/emit
idempotence, playground markup + offline guarantee.

**Phase 7b:** mirror/rotate with the §5 remap table + property tests,
duplicate-instance, parts browser, optional packet/schedule "Part" columns.

**Phase 20 (composition v2):** parametric parts, nested `use` (depth 2) with
cycle detection, multi-level parts — see §12. `scheme inheritance` evaluated and
deferred (§13).

**Future (still deferred):** scheme inheritance (`extends`, §13), remote/URL
libraries, part editing inside the playground (multi-buffer), string/expression
params.

## 11. Alternatives considered

- **Textual `#include`** — rejected: id collisions, meaningless line anchors,
  no reuse semantics, undo/autosave ambiguity. Composition needs a component
  boundary.
- **`part … end` blocks (several parts per file)** — rejected for v1: the
  grammar is deliberately flat, one-statement-per-line; block scoping is a
  bigger language change than the feature needs. A "kit" is a directory.
- **Multi-buffer playground editor** — rejected for v1: it dissolves the
  single-buffer undo/compare/autosave model that everything since the unified
  undo work relies on. Read-only stamps + inline keeps text sovereign.
- **Stamping at parse time (macro expansion into the host token stream)** —
  rejected: destroys the once-per-part diagnostic dedupe and re-anchors every
  part diagnostic to synthetic host lines.

## 12. Composition v2 (Phase 20) — shipped semantics

Three capabilities the design consciously cut from 7a/7b landed here, each keeping
every 7a/7b guarantee (the resolver sandbox, deterministic stamping with `alias.id`
prefixing, part-internal diagnostic attribution, read-only stamped members, emit
round-trip fixpoints, mirror/rotate).

### 12.1 Parametric parts

**Grammar.** A part file may declare parameters:

```
param <name> = <number>          # part files only; the default is mandatory
```

and a host passes values on the `use` line with a trailing `with` clause:

```
use "<relpath>" as <alias> at <x>,<y> [level <n>] [mirror x|y] [rotate 90|180|270] [with k=v[, k=v…]]
```

`<number>` is a decimal-feet number or a ft-in literal (`8`, `7-6`). **Numbers
only in v1** — no strings, no arithmetic. Every param is *optional* at the use
site (the declared default applies when it isn't passed).

**Resolution mechanism (where the env lives, how `number()` consumes it).** Params
resolve at the **token level in fragment mode** — the source text is never
rewritten. `compile_source(..., fragment=True, params=<overrides>)` builds a
`param_env: dict[str, float]` = `{**declared-defaults, **use-site-overrides}` from a
cheap pre-scan (`_scan_param_defaults`, so a bare name may be referenced above its
own `param` line), and threads it into the parser's `_Cursor`. In `_Cursor.number`,
when a token is neither a float nor a ft-in literal, an identifier that names a
param returns its value; an identifier that names no param is `PARAM_UNKNOWN` (with
a did-you-mean over the declared names). A bare param name therefore stands wherever
a number stands — sizes, positions, offsets, widths — with no textual substitution.

**Diagnostics.** `PARAM_UNKNOWN` (bare name is no param, in the part),
`PARAM_UNDECLARED` (a `with` key the part doesn't declare, anchored to the *use*
line, with a did-you-mean), `PARAM_DUP` (declared/passed twice), `PARAM_IN_PLAN`
(`param` in a whole plan). A default that makes the part invalid is an ordinary
part-internal diagnostic.

**Memoization.** The loader memo is keyed `(resolved-path, sorted param items)`, so
two instances with the same params share one compile and different params recompile
— the 64-instance cap stays meaningful either way.

**Emit.** Part files emit their `param` lines (values baked into the geometry — the
symbolic reference is resolved to a literal at compile time, an accepted, documented
lossiness); hosts emit the `with` clause **exactly as passed, in source order**
(`with k=v, …`, ft-in canonicalized to decimal feet). Emit → recompile → emit is a
fixpoint.

### 12.2 Nested `use` (depth 2)

A part may itself `use` nested parts. **Containment root stays the HOST's root** at
every level; a nested relative path resolves relative to the **using part's**
directory (`_resolve` joins `base_dir`, then requires the realpath to stay under the
host `root`). Depth is host = 0, part = 1, part-used-by-part = 2; a `use` that would
reach depth 3 is `USE_NESTED` ("deeper than 2").

**Cycle detection.** The compose context carries a `stack` of every ancestor's
realpath (including the file being composed). A resolved candidate already on the
stack is `USE_CYCLE` (self-use or mutual use), naming the cycle path (`a.barn →
b.barn → a.barn`) — checked **before** the depth limit so a ring reads as a cycle,
not a depth overflow, and the loader stops cleanly instead of recursing.

**Composition.** Id prefixing composes: the inner stamp produces `inner.room` in the
part's frame, the outer stamp re-prefixes to `outer.inner.room`. Transforms compose
by re-application — the inner `use`'s `_Xform` transforms geometry into the middle
part's frame, then the outer `use`'s `_Xform` transforms the already-transformed
geometry into the host (matrix composition via the same `_Xform`). The instance cap
is a **single shared budget** decremented on every stamp at every depth, and the
part memo is shared across the whole compile tree.

**Diagnostic attribution through two levels.** A part folds its parts' findings with
the alias re-prefixed onto the finding's room (so the parent's one-level dedup lines
up: `outer.inner.room` → strip one alias → `inner.room`, matched against the folded
`(code, inner.room)` key). The message accumulates a chain — `in part
outer.barn:5 — in part inner.barn:3 — <original>` — while the `file`/`part` fields
keep the **innermost** attribution (the first level to set them wins), and the caret
anchors to the host `use` line the author can act on.

### 12.3 Multi-level parts

A part may carry `level 1` (etc.) rooms and a connecting `stair` (both were
host-only before). Stamping offsets every member's level by the instance's `level n`
(rooms, notes, and the stair's `from_level`/`to_level`), so the composed plan
presents the **final** levels. Whole-building, cross-level validation — stair
connectivity, per-storey smoke alarms (`ALARM_LEVEL`), loft guards (`LOFT_GUARD`),
garage separation — is skipped in the part's fragment compile (it can't run across a
part boundary) and runs on the composed host plan, so it sees the final levels. The
stair footprint transforms like a room under mirror/rotate.

## 13. Scheme inheritance (`extends`) — deferred (Phase 20 go/no-go: NO-GO)

**Proposed:** `plan "X" extends "base.barn"` — the host starts from the base's
statements; a host statement with the same id/kind overrides the base's; new
statements append. Same sandbox as `use`.

**Decision: deferred.** The three required v2 features (parametric / nested /
multi-level) were prioritized, shipped, and are green; `extends` did not fall out
cleanly in the remaining budget, and it carries semantic ambiguities that deserve
their own design pass rather than a rushed one. Specifically:

- **The override key is only clean for id'd entities and singletons.** Rooms
  override by `id`; `envelope`/`ceiling`/`program`/`site`/`grade` are singletons
  that replace. But openings (`door`/`open`/`window`/`entry`), fixtures, and devices
  have **no stable identity** — there is no principled key to decide whether a host
  `window great north` *replaces* a base one or *adds* a second. `use` sidesteps
  this entirely (it stamps a namespaced copy, never merges); `extends` cannot.
- **Emit round-trip needs provenance tracking.** Emitting an `extends` plan must
  write the `extends` line plus **only the host's own** statements (not the
  inherited ones), which means threading a base-vs-host provenance tag through the
  whole compiled model — new machinery every downstream subsystem would have to
  ignore correctly.
- **It composes with `use` in the base**, multiplying the edge cases (a base that
  itself `use`s parts, then is `extend`ed and partially overridden).

**Design sketch (for a future phase).** Parse `extends "<relpath>"` on the `plan`
line; resolve it through the *same* sandboxed resolver as `use` (relative-only,
realpath containment, size cap), compiling the base **as a full plan** (not a
fragment). Merge at the **model** level, not the text level: start from the base
plan, then for each host statement family apply a family-specific override rule —
rooms/suites/zones by `id`; the singletons replace; openings/fixtures/devices
**append** in v1 (a conservative, predictable rule — overriding a non-id'd opening
would need an explicit `remove`/`replace` verb, a separate feature). Tag every
element with its origin file so (a) emit writes only host-origin statements after
the `extends` line and (b) a base-origin diagnostic attributes to the base file with
the same `in part …` chain machinery Phase 20 already built for nested parts. Cycle
detection and the instance budget reuse the Phase 20 `_ComposeCtx`. The riskiest
part is the openings/fixtures merge — ship append-only first, add `remove`/`replace`
later once real plans show which override verbs are actually wanted.
