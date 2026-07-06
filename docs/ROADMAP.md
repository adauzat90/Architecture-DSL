# Roadmap — remaining items from the persona review cycle

Status: **proposed** (nothing below is scheduled until picked). Phases 1–15 are
shipped; this document sequences everything that was found by the four-persona
test panel (novice / architect / contractor / developer) or deferred from an
earlier phase, but not yet built. Ordered by recommended sequence — value first,
then the items that depend on it.

Each phase lists why it matters (who asked), the scope, what it deliberately
does NOT include, and its acceptance bar. Effort is a coarse T-shirt size
relative to past phases (M ≈ one focused agent phase like countertops; L ≈
composition 7a).

---

## Phase 16 — Site plan v2: driveway, utilities, grade (L) — **SHIPPED**

**Status:** shipped. Grammar (`drive`/`walk`/`well`/`septic`/`service`/`grade`),
the site render (drive/well/septic/service symbols, a legend, and *actual*
building-to-lot-line clearance dims), four new rules (`WELL_SEPTIC_CLEAR`,
`DRIVE_DOOR`, `SEPTIC_SETBACK`, and — resolving the Phase 13 skip below —
`PORCH_GUARD` for IRC R312.1), site cost lines (drive by surface, walk, well &
septic allowances) with a reworded exclusions footer, and a packet clearance
table all landed. Showcase: `examples/gallery/homestead.barn` (0/0/0).

**Who asked:** the contractor persona ("site plan has no drive, well/septic, or
service entrance; building-to-line clearances aren't dimensioned") and,
indirectly, the architect persona — R312.1 exterior guards were skipped in
Phase 13 *because the model has no grade elevation*. This phase is the enabler.

**Scope**
- Grammar: `drive at <x>,<y> size <w> x <l> [gravel|concrete|asphalt]`,
  `walk from <door-room> to drive [width <ft>]`, `well at <x>,<y>`,
  `septic at <x>,<y> [field <w> x <l>]`, `service <electric|water|gas> from
  <N|S|E|W>`, and `grade <ft>` (finish-floor height above grade; single value,
  v1 is flat sites).
- Site render: draw the new features with standard symbols (drive hatch, well
  circle-W, septic tank + field lattice, service drop arrows), dimension the
  building's *actual* setback distances to each lot line (today only the
  required setback lines draw), and a site legend.
- Rules: `WELL_SEPTIC_CLEAR` (warning — separation distance below the common
  100 ft well-to-septic rule, profile-amendable), `DRIVE_DOOR` (info — no
  walk/drive reaches an entry), `SEPTIC_SETBACK` (info), and — now expressible —
  **R312.1 `PORCH_GUARD`** (warning: porch/landing with `grade` > 30 in needs a
  guard; the Phase 13 skip becomes a rule).
- Cost: drive area by surface type, walk area; septic/well as allowance line
  items with an "allowance" tag; the exclusions footer drops "site work" and
  names what is still excluded (permits, overhead & profit).
- Packet: the site sheet gains the new symbols + a clearance table.

**Not included:** contour/sloped grades, easements, multiple buildings,
survey-grade bearing/distance lot lines (the lot stays a rectangle).

**Acceptance:** a plan with drive/well/septic/grade renders a site sheet a
plans desk would recognize; PORCH_GUARD fires on a 36-in grade porch and stays
silent at 24 in; cost gains the site lines; all example plans unchanged unless
they opt in.

---

## Phase 17 — Compile performance: kill the O(n²) (M)

**Who asked:** the developer persona — 10 k lines compiles in ~115 s with a
measured quadratic curve (5× rooms ⇒ 25× time). Irrelevant at 30 rooms, but
`watch`, the LSP, and generated plans all pay it.

**Scope**
- Profile first, commit the numbers: the prime suspects are the all-pairs room
  loops (overlap, shared-edge discovery, adjacency, partition walls) and any
  per-diagnostic rescans of `plan.rooms`/openings.
- Introduce one shared spatial index built once per compile (a uniform grid or
  sorted-interval sweep — stdlib only) and route the pairwise checks through
  it. `shared_edge`/overlap results can be memoized on the plan object for the
  validation pass.
- Hard constraint: **zero behavior change** — diagnostics, emit, render, DXF
  byte-identical across the whole suite and gallery (the suite is the oracle;
  add a determinism test that compiles a large generated plan twice).

**Acceptance:** the developer persona's 10 k-line fixture compiles in < 5 s;
the 2 k-line fixture in < 1 s; full suite green with no pin changes; a
scaling test pins the new curve (e.g. 2× rooms ≤ ~2.5× time).

---

## Phase 18 — Dimension convention: face-of-stud mode (M)

**Who asked:** the architect persona — pros dimension to face-of-stud or
centerline, not to nominal room lines. Phases 10/12 kept dims nominal (the
model's coordinate truth) and documented it; this makes the pro convention
available without changing the default.

**Scope**
- `RenderConfig.dim_mode = "nominal" | "faces"` (default `"nominal"`, so
  nothing changes for existing users). In `"faces"` mode the chain and overall
  dims measure to wall faces from the shared `wallbodies` geometry: exterior
  overall = outside face to outside face; room segments = clear face to face;
  opening jambs unchanged (already face-of-opening).
- The packet and `barndsl build` grow a `--dims faces` flag; the playground a
  small toggle next to the print controls.
- Schedules already report clear dimensions; add the mode note to the packet
  title block ("dimensions to face of stud" vs "to nominal room lines").
- DXF inherits the same switch (its dim geometry comes from the same helpers).

**Not included:** mixed conventions on one sheet, dimension grips/associativity.

**Acceptance:** the two modes agree with hand math on a fixture plan (nominal
12′0″ room with 4.5 in partitions reads 11′7½″ clear in faces mode); default
output byte-identical to today; SVG/DXF stay in parity in both modes.

---

## Phase 19 — DXF v2: associative dimensions + glyph polish (M/L)

**Who asked:** the architect persona (DXF should carry real `DIMENSION`
entities); plus two small Phase 11/12 deferrals.

**Scope**
- Real associative `DIMENSION` entities (rotated linear) for the overall and
  chain strings, each with its anonymous `*D<n>` block rendering the exploded
  geometry we already know how to draw — readers that regenerate get live
  dims, readers that don't still see the block. Keep the exploded-geometry
  layer behind a flag for compatibility (`--dxf-dims geometry|associative`).
  This was deferred in Phase 11 as a tar pit — the ezdxf oracle from that
  phase is the safety net; budget the risk here, not in v1.
- Counter mitring in DXF (plain rectangles today; reuse the renderer's trim).
- Overhead/garage door: full dashed-panel glyph instead of a track line.
- Loft/stair guard lines on open edges (SVG + DXF, from `LOFT_GUARD` data).

**Acceptance:** ezdxf audit stays zero-error; a regenerating viewer shows true
associative dims; non-regenerating render matches today's geometry pixel-wise;
byte-determinism preserved.

---

## Phase 20 — Composition v2 (L)

**Who asked:** the cross-file composition design doc's Futures section
(docs/design/cross-file-composition.md) — requested capabilities that were
consciously cut from 7a/7b.

**Scope (pick subset when scheduled)**
- **Parametric parts:** `use "parts/bath.barn" as b at 0,0 with width=8` —
  parts declare `param <name> = <default>` and reference params in sizes;
  stamping substitutes then compiles the fragment.
- **Nested `use`** (depth 2 with the same sandbox + cycle detection), enabling
  part libraries composed of parts.
- **Multi-level parts** (a part carrying `level 1` rooms stamps with its
  levels intact, offset by the instance's `level n`).
- **Scheme inheritance:** `plan ... extends "base.barn"` — the host starts
  from the base's statements and overrides by id.

**Acceptance:** per-feature; each keeps the resolver sandbox guarantees
(relative-only, realpath containment, byte caps) and emit round-trip.

---

## Phase 21 — iPad / touch support for the playground (M) — **awaiting go-ahead**

Explicitly parked by the owner; do not start unprompted. Scope when unblocked:
pointer-events for drag/select/measure (unify mouse/touch), larger hit
targets on coarse pointers, pinch-zoom/pan on the plan pane that doesn't fight
the browser, and an on-screen keyboard-safe editor layout.

---

## Minor items bucket (S — batch several into any phase)

- Playground autocomplete for the `along`/`from`/`to` counter grammar.
- Document the Revit round-trip's intentional diagnostic drift (reimport
  writes explicit offsets, so `DOOR_CENTERED` infos vanish) in AUTHORING.md.
- `barndsl compare` on three or more files (A→B→C change-order history).
- Cost overrides discoverability: `barndsl cost --print-keys` emitting the
  full overridable key table with defaults.
- LSP: publish diagnostics for open part files too (deferred in Phase 8; only
  when the editor has the part open — no unsolicited URIs).

## Explicitly rejected (recorded so they aren't re-litigated)

- **Flat 12-riser stair-landing trigger** — would flag every normal
  single-story flight; the shipped rule uses the real R311.7.3 rise limit.
- **R312.1 porch guards without grade data** — fired always or never;
  ~~unblocked by~~ **resolved in** Phase 16: the `grade` statement now supplies
  the finish-floor-above-grade height, and `PORCH_GUARD` fires per porch only
  when that exceeds 30 in.
- **Solid-fill hatch DIMENSION-free DXF dims as the only mode** — viewers that
  regenerate would lose fidelity; Phase 19 keeps both.
