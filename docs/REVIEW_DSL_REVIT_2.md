# Review — DSL & Revit improvements, round 2 (2026-07)

## Status (update)

Implemented since this review: the **agent grounding batch** (§1.1 multimodal
critic — the critique call now sees the rendered plan; §1.2 geometry pack —
`barndsl inspect`, rooms/adjacency/unplaced-pockets/free-wall-spans appended
to the loop's feedback; §4.3 explained scores — per-component causes in
`ScoreReport.details`), **update-in-place Revit rebuilds** (§3.1 — identity +
fingerprint stamps in Extensible Storage, config-aware per-kind context,
unchanged elements keep their Revit ids and room numbers), the **`wall`
statement and window/door kinds** (§2.1/§2.2 — plumbing/bearing/rated shared
walls checked and honoured by the frame; casement/slider/fixed/double-hung
windows with honest per-kind egress math; double/french doors; all carried
through the exchange and mapped to real Revit types/families by the builder),
**`barndsl compare` and `barndsl revit-log`** (§4.1/§3.2 — scheme deltas, and
the build log translated into REVIT_FAIL/REVIT_SKIP/REVIT_NOTE diagnostics),
and the **solver objective alignment** (round-1 §1.4 — `_score` folds in the
design score).

Batch 5 added: **solver seeding** (§1.4 — opt-in `seed_with_solver`: the
layout engines' best compiling candidate becomes iteration 0, the floor the
LLM must beat, and its DSL seeds the first generation prompt), **the gradient
on failed compiles** (§1.3 — statement-level parser recovery keeps a scored
partial plan, the loop revises from the best valid source with coherent
feedback, and output commands refuse `recovered` partials), **`site` +
`setback`** (§2.4 — a verifiable `SETBACK` check, carried through the
exchange), and **`barndsl cost` + `barndsl packet`** (§4 head, §4.2 — an
assembly budget with honest line math, and the one-file HTML client packet).

Batch 6 added: **suites/zones** (§2.3 — `suite`/`zone` statements; declared
membership sharpens MASTER_ENSUITE / BED_SOUND / BED_PRIVACY / ENTRY_PRIVATE
only where the geometry backs it up; zones ride the exchange to the room's
Department parameter), **per-section and monitor roofs + exchange v2**
(round-1 §3.4/§3.5/§3.6 — L/T/U wings get their own roof planes, the monitor
form builds as two sheds rising to a raised centre gable, and program / frame
/ roof style-pitch / notes / accessible survive the round-trip so
PROGRAM_MISMATCH guards Revit-side edits), and **`barndsl revit-diff`**
(§3.4 — the model-vs-authored drift report with position-first matching for
positional-id kinds). Everything else below remains open; the notes are kept
as the original review.

A second full-app review with the same three questions as
`docs/REVIEW_DSL_REVIT.md`: what would let **AI agents design better plans**,
what would raise the **Revit integration's** fidelity and usefulness, and what
would give **architects** more value. Nothing here repeats what earlier rounds
already shipped (the round-1 status block and `docs/IDEAS.md` track those);
round-1 items that are *still open* are carried forward in a short list per
section so this document is the single up-to-date backlog.

**Where the app stands.** The compile-fix loop is now genuinely closed: ~100
registered diagnostics with IRC citations, a deterministic 0–100 score the
agent explicitly hill-climbs, best-iteration-wins with a hard `target_score`
gate, declared intent (`program`, `require`) checked mechanically, and an
idempotent Revit builder with a documentation pass, both harness-tested. The
three weakest seams today:

1. **The agent is spatially blind.** It optimizes a scalar and a list of
   diagnostic strings; it never sees the drawing, the resolved coordinates, or
   the adjacency graph the compiler already computes.
2. **The Revit build is write-once.** Purge-and-recreate is idempotent but
   destroys Revit element identity on every iteration, so a human can't
   annotate or refine a model the agent is still iterating on.
3. **The DSL's walls are two constants.** One implied exterior and one
   interior wall type caps both the validator (it can only *remind* about
   rated/plumbing walls) and the Revit exchange (every wall builds the same).

---

## 1. Make the agent spatially grounded (highest leverage)

The feedback channel is clean and token-lean (`render_feedback`,
`agent.py:95-118`) — but it is *only* the score header plus one line per
diagnostic. Everything below adds the missing spatial signal; none of it
requires a grammar change.

### 1.1 Give the critic eyes (multimodal critique)
The renderer exists and PNG output exists (`build --format png`), yet neither
`write_source` nor `critique` ever sees an image — the "senior architect"
critic judges flow, light, and wasted space from **text alone**
(`_CRITIQUE_SYSTEM`, `agent.py:72-80`). The generation and critique models are
multimodal; render each iteration to PNG and attach it as an image content
block to the critique call (and optionally to the revision prompt).

This is the cheapest possible upgrade to design quality: proportion,
circulation legibility, façade rhythm, and "this hall goes nowhere" are
things a vision pass catches instantly and text never will. Fall back to
text-only when `cairosvg` isn't installed. Require the critic's `rationale`
to cite what it *sees* ("the kitchen is an island unreachable from the
garage") so the visual signal demonstrably flows into `suggestions`.

### 1.2 A geometry pack in the feedback (and `barndsl inspect`)
The compiler already computes resolved rectangles, exterior-wall assignment,
the door/adjacency graph, unused-footprint area, and per-room clear dims — and
serializes **none of it to the agent**. `render_feedback` should append a
compact block:

```
Rooms (id type level x,y w x l | exterior walls):
  great_room living L0 0,0 28 x 26 | south west
Adjacency (door edges): great_room-kitchen, kitchen-pantry, ...
Unplaced footprint: 92 sqft in 1 pocket at 46,28 (12 x 8)
Free wall spans: kitchen north 4.0-11.5, master_bed east 0-11 ...
```

Two consumers: the agent (it currently re-derives geometry by mentally
re-executing its own source — the main source of offset/adjacency mistakes),
and tooling, via a new `barndsl inspect FILE --json` that dumps the same
structure. The **free-wall-spans** list is the sharpest single item: it turns
"guess an offset, get `DOOR_OOB`/`OPENING_CLASH`, retry" into a lookup — the
agent picks a legal span on the first try.

### 1.3 Keep the gradient when the compile fails
A hard error yields `plan is None`, which scores a flat 100 penalty **and**
drops all four continuous components (`score.py:160-168`). On a broken round
the agent loses exactly the space/proportion/daylight gradient it was
climbing. Two fixes, both worthwhile:

- **Statement-level error recovery in the parser**: skip the bad line, keep
  parsing, and validate the partial plan, marking the result failed but still
  scoring the survivors. One typo then costs its own diagnostic, not the whole
  signal.
- **Score the best prior iteration's continuous terms** in the feedback header
  ("your last valid plan scored 74; this one doesn't compile") so a regression
  is legible as a regression.

### 1.4 Seed the agent with the solver
The deterministic layout engines (`layout.py`/`layout2.py`, five engines, an
explicit adjacency model) and the LLM agent are parallel universes — the agent
never sees the solver. Wire them together:

- **Candidate 0 from the solver.** The generator is already forced to emit a
  `program` line; derive a `.brief` from the brief's program/requires, run the
  `fill` engine, and hand the emitted DSL to the model as a starting point to
  *refine* instead of a blank page. The solver's plans are dimensionally
  sound; the model's edits become design moves, not arithmetic.
- **Best-of-N at near-zero cost.** Solver candidates (all engines × 
  topologies) compile and score for free; keep the best as the floor the LLM
  must beat. `design()` already returns the best-scoring step, so this slots
  straight into the existing loop.

### 1.5 Carried over from round 1 (still open, still right)
- **Align the solver's objective with the design checks** (round-1 §1.4) —
  `_score` in `layout2.py` still ignores the ~25 design-quality codes, so
  auto-layout emits plans that immediately fire infos it never tried to avoid.
  Folding info counts (or the real `design_score`) into topology selection got
  *cheaper* now that `score.py` exists — the solver can literally optimize the
  same number the agent does.
- **Structured fix-its + `barndsl fix`** (round-1 §1.5) — attach an optional
  `{line, replacement}` machine edit to mechanical `Issue`s; deterministic
  errors get deterministic repairs, and the agent spends tokens on design.

---

## 2. DSL additions

Ordered by value-per-line-of-grammar.

### 2.1 A `wall` statement (the round-1 item that matters most now)
Still the biggest gap between what the validator *knows* and what it can
*verify*, and between the DSL and Revit fidelity. One statement closes three
loops at once:

```barn
wall master_bath - kitchen plumbing      # 2x6 wet wall
wall living - garage rated               # R302.6 separation, verified not reminded
wall great_room - shop bearing           # frame honours it as a post line
```

- **Validator**: `WET_GROUP` stops nudging and starts checking a declared wet
  wall; `GARAGE_SEPARATION` becomes verifiable; a `bearing` wall feeds
  `LOAD_PATH`.
- **Frame**: "honour an explicit interior bearing wall as a post line" has
  been an open follow-up in `docs/IDEAS.md` since the frame shipped — this is
  its missing input.
- **Revit**: the exchange today carries only a nominal thickness and an
  `exterior` bool (`revit.py:105-145`); a per-wall `kind` key lets
  `config.json` map plumbing/rated/bearing to real named wall types, the same
  precedence chain `_pick_wall_types` already implements.

### 2.2 Window and door types (round-1 §2.4, sharpened)
`window ... casement|slider|fixed|double-hung` and `door ... double|french`.
Two payoffs: honest egress (`BEDROOM_EGRESS`/`EGRESS_SIZE` can currently pass
on glazing that doesn't open — a `fixed` window is not an escape opening), and
direct family mapping in the builder, which already duplicates-and-sizes
symbols per opening (`_sized_symbol`) and just needs a type key to pick the
right family first.

### 2.3 Suites / zones
Rooms are a flat list; the plan's real structure is hierarchical:

```barn
suite primary: master_bed master_bath master_wic
zone private: primary bed_2 bed_3 hall_beds
```

- **Agents** think in zones ("private wing east, public core center") — today
  that intent lives only in the model's head and dies between iterations,
  exactly the failure `require` was built to fix for pairwise adjacency.
- **Checks** get sharper for free: `MASTER_ENSUITE`, `BED_PRIVACY`,
  `ENTRY_PRIVATE`, and `BED_SOUND` all currently *infer* suite/zone membership
  from types and adjacency; a declared suite makes them exact, and a
  `zone`-crossing check ("a public room inside the private band") becomes
  possible.
- **Revit** gets a `barndsl zone` room parameter → native room schedules
  grouped by zone, and (later) Areas.

### 2.4 A `site` statement
The only site datum today is the orientation azimuth. A minimal lot model:

```barn
site 120 x 200
setback front 25 side 10 rear 20
```

buys a deterministic `SETBACK` check (footprint + porches vs. the buildable
rectangle — pure rectangle math, same as everything else), gives `orientation`
something to anchor to, and carries through the exchange so the builder can
place the model correctly relative to a survey point instead of at the
internal origin. Barndominiums are rural/acreage builds where the *lot* is
rarely the constraint — but the setback check is exactly the kind of
"caught it before the county did" moment that builds trust, and it is cheap.

### 2.5 Carried over from round 1 (still open)
- **Solar awareness** (§2.5): `orientation` now rotates Project North in Revit
  but still feeds no check — south-glazing/west-bedroom infos plus a score
  term would let agents be *asked* for passive-solar layouts and be checked.
- A **plumbing-stack check** for multistory plans is the natural companion to
  the `wall plumbing` statement: flag an upper bath whose wet wall doesn't
  sit over (or one room away from) a lower one — real money in a slab build,
  pure rectangle math to detect.

---

## 3. Revit integration, round 2

The fidelity batch closed the dead-field gaps (swing, north, finish hints,
sizes, cased openings, fixture rotation). What remains is structural: the
build's *lifecycle* and the exchange's *reach*.

### 3.1 Update-in-place instead of purge-and-recreate
Re-running Build Plan deletes every managed element and rebuilds
(`_purge_managed`, `builder.py:224`) — idempotent, but every iteration gives
walls/doors/rooms **new Revit element ids**. Consequences: any user dimension,
tag, keynote, or view annotation attached to a managed element is orphaned;
schedules re-sort; nothing the architect adds survives the agent's next round.
This is the single biggest blocker to the workflow the project is really
about — *an agent and an architect iterating on the same model*.

The exchange already assigns stable ids (`wall_id`, room ids, opening ids) and
the builder already stamps managed elements via Extensible Storage. Persist
the **exchange id in the stamp**, then diff on rebuild:

- element with same id + same geometry → keep untouched (annotations live);
- same id, moved/resized → adjust in place where the API allows (location
  line, sized-symbol swap, `flip`), else recreate;
- id gone from the exchange → purge; new id → create.

Purge-and-recreate stays as the `replace` fallback. The diff itself is pure
(exchange vs. exchange) and unit-testable without Revit — only the "adjust in
place" arm needs the live-Revit checklist.

### 3.2 Feed the build log back into the loop
`report.py` already records every element's created/skipped/failed outcome
with reasons to `*.buildlog.json` — and nothing reads it. Two consumers:

- **`barndsl revit-log BUILDLOG`**: translate outcomes into standard
  diagnostics (`REVIT_SKIP door D3: no garage-door family loaded | hint: load
  one or map a named type in config.json`). Architects get a familiar report;
  CI can gate on "everything built".
- **The agent loop**: fold those diagnostics into the next iteration's
  feedback, so the agent designs around what the target Revit template can
  actually build (e.g. stop specifying 12-ft overhead doors when no family
  hosts them). This makes the *Revit environment itself* part of the
  compile-fix teacher — no other change does that.

Post-build, also harvest Revit's own document warnings (the API exposes them)
into the same report; today they vanish.

### 3.3 Build candidates as Design Options
`design()` keeps a full scored history but only the winner ships. Revit has a
native concept for exactly this: **Design Options**. A `--options N` build
that places the top-N scoring iterations into one option set (shared levels,
option-scoped walls/rooms/openings) turns "the agent picked for you" into
"the agent shortlisted, you choose in the model" — which is how architects
actually want to consume generated design. The managed-mark machinery already
scopes elements per build; scoping per option is the same pattern.

### 3.4 Strengthen `read_model` until round-trip editing is real
The Model-to-DSL path reads rooms and door/window instances but **no walls, no
structure**, guesses types from names, and collapses rooms to bounding boxes
(`builder.py:1705`, `revit/README.md`). Combined with 3.1, the target
workflow is: agent builds → architect nudges walls/doors in Revit → **`revit-
diff`** (new: read_model vs. the current `.barn`, report added/moved/removed
rooms and openings) → agent regenerates DSL incorporating the human edits.
The diff report alone — even before wall reading improves — makes drift
*visible*, which is the prerequisite for trusting two-way editing.

### 3.5 Carried over from round 1 (still open)
- **Monitor roof builds as a gable** (§3.4) and **L/T/U roofs cover the
  notch** (§3.5) — `roof_plan` is still the bounding rectangle
  (`revit.py:702-713`). The monitor form is the project's namesake barn
  silhouette; per-section roofs mirror what the slab pass already does.
- **Exchange v2 round-trip completeness** (§3.6) — `program`, `frame`, roof
  style/pitch, `notes`, `accessible` still don't survive
  `exchange_to_plan`; the declared program surviving Revit-and-back matters
  most (`PROGRAM_MISMATCH` should still guard edits made in Revit).
- **Live-Revit smoke fixture** (§3.7) — one `.barn` exercising every pass +
  an expected-buildlog diff script, so a human with Revit validates a release
  in minutes. The experimental list (gable profiles, slopes, stairs, section,
  north sign, swing hand) has only grown since round 1.
- **Units**: the exchange hardcodes feet (`exchange.py` rejects anything
  else). A `units` field with metric support is cheap now and breaking later.

---

## 4. Architect experience

Round-1 items still open, in priority order — the reasoning hasn't changed,
so they're listed, not re-argued: **`barndsl cost`** (top item two reviews
running; `metrics()` has every quantity), **jurisdiction profiles**,
**shareable HTML client report**, **furniture-fit + kitchen work-triangle**,
**electrical/life-safety seeds**, **glTF/isometric massing export**.

New this round:

### 4.1 `barndsl compare A.barn B.barn`
Everything needed exists: score with per-component deductions, metrics,
program, schedules. A side-by-side diff (score delta by component, sqft by
room type, program satisfaction, diagnostic delta) serves three audiences at
once — the agent's best-of-N selection gets explainable, the architect gets a
"scheme A vs scheme B" client artifact, and CI gets plan-regression review.
Pairs naturally with Design Options (§3.3): compare on paper, then build both.

### 4.2 A permit-sketch PDF packet
`schedule`, dimensioned SVG, PDF rendering, and `metrics()` all exist as
separate commands. One `barndsl packet FILE` that binds them — cover page
(name, program, metrics, score), dimensioned plan per level, schedules,
diagnostics appendix — is the "hand it to a builder/lender/county" deliverable
the product review kept circling. It is assembly, not new capability, and it
is the non-Revit user's equivalent of the Document pass.

### 4.3 Explain the score
`score --json` gives totals and component numbers; architects (and the
critic) get more from *named causes*. Attach the contributing rooms to each
continuous component ("proportion −4.2: bed_3 is 2.4:1, office 2.1:1") — the
data is computed and discarded inside each `_*_penalty`. This also makes the
agent's feedback header actionable without cross-referencing the info list.

---

## 5. Suggested sequencing

1. **Agent grounding batch** (§1.1 eyes, §1.2 geometry pack + `inspect`) — no
   grammar changes, immediate design-quality lift, and every later feature
   benefits from a smarter loop.
2. **`wall` statement + window/door types** (§2.1, §2.2) — the grammar batch
   with the best validator *and* Revit payoff per line.
3. **Revit lifecycle batch** (§3.1 update-in-place, §3.2 build-log feedback)
   — turns the plugin from a one-shot generator into an iteration partner.
4. **`compare` + score explanations + solver seeding** (§4.1, §4.3, §1.4) —
   cheap, compounding wins on the existing score infrastructure.
5. **`cost` + `site` + packet** (§4 head, §2.4, §4.2) — the architect-facing
   deliverables.
6. **Reach**: Design Options, `revit-diff`, monitor/L-T-U roofs, exchange v2,
   suites/zones, jurisdiction profiles, glTF massing.
