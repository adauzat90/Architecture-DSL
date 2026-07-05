# Cross-file composition (`use`) — design

Status: **design approved for Phase 7a/7b** · not yet implemented
Prereqs shipped: full emit round-trip (every statement family), `barndsl fmt`
(comment-preserving), accept pragmas, ft-in literals.

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

- Geometry mirrors/rotates about the part's local bbox (then `at` places the
  transformed bbox's SW corner — the stamp always lands where you said).
- Wall directions remap: rotate 90° ccw ⇒ S→E, E→N, N→W, W→S; mirror y ⇒
  E↔W, N/S fixed (and vice versa for mirror x).
- Wall-attached offsets recompute from the new wall's start corner (S/W
  start convention): on a mirrored wall of length L with feature width w,
  `offset' = L − offset − w`. Applies to windows, doors, outlets, switches,
  and wall-backed fixtures.
- Fixture `rotate` composes additively mod 360; door swing `into` refs are
  id-prefixed and otherwise unchanged (side-ness is derived from geometry).
- Round-trip property tests: stamp∘mirror∘mirror ≡ stamp; rotate⁴ ≡ identity;
  every remapped offset re-derived from geometry equals the transform of the
  original point.

Translation-only ships first (7a) precisely because this table is where the
bugs live.

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
above the root, no network. Limits: `use` depth 1 in v1 (parts can't `use`;
`USE_NESTED` error), ≤ 64 instances, part file ≤ 256 KiB — each limit has a
teaching diagnostic, not a crash. The playground server already binds
localhost-only; the resolver must not widen what it will read.

## 9. New diagnostics

| Code | Sev | Meaning |
|---|---|---|
| `USE_UNRESOLVED` | E | path missing / escapes root / no base_dir |
| `USE_ALIAS_DUP` | E | alias reused |
| `USE_NESTED` | E | a part contains `use` (v1) |
| `USE_PART_INVALID` | E | part fails fragment compile (nested context attached) |
| `PART_HOST_STMT` | E | host-only statement inside a part |
| `PART_ORIGIN` | I | part's SW corner isn't 0,0 (loader normalized it) |
| `PART_EMPTY` | E | part declares no rooms |

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

**Future (explicitly deferred):** nested parts (depth > 1), multi-level parts
(stairs inside parts), parametric parts, scheme inheritance, remote libraries,
part editing inside the playground (multi-buffer).

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
