# Review — DSL & Revit improvements (2026-07)

A full-app review focused on three questions: what would let **AI agents design
better plans**, what would make the **Revit integration** higher-fidelity and
more useful, and what would give **architects** more value. Findings cite
`file:line` as of this review; earlier review rounds live in
`docs/PRODUCT_REVIEW.md` and `docs/IDEAS.md` — nothing here duplicates what
those already shipped.

**Where the app stands.** The compile-fix half of the loop is genuinely strong:
~90 catalogued diagnostics with IRC citations and verbatim hints, a JSON
diagnostics view, a 3-topology auto-layout solver, a tested two-way Revit
exchange with an idempotent builder and a documentation pass. The weak half is
the *design-quality* loop: quality exists only as unranked `info` nudges and a
non-reproducible LLM critic boolean, the solver's objective ignores the very
design checks the compiler emits, and several DSL concepts die on the way into
(or back out of) Revit.

---

## 1. Close the agent quality loop (highest leverage)

The agent's only "is it good?" signal today is the critic's `satisfied` boolean
plus prose (`agent.py:76-85`). Everything below turns quality into a signal an
agent can optimize.

### 1.1 A design score, exposed
There is no numeric quality score anywhere in the CLI or `to_dict()`. The
solver already has one — `_score` in `layout2.py:273-296` (errors, unmet
adjacencies, waste fraction, proportion penalty, aspect) — but it's internal to
topology selection and never serialized.

Ship a `plan.design_score()` / `barndsl score FILE` that composes:
- hard failures (errors, then warnings) as dominating terms;
- the count/weight of design `info`s actually fired (they're already
  severity-tagged and coded — the data exists);
- continuous margins, not just pass/fail: waste fraction (`AREA_UNUSED`'s
  underlying number), daylight margin over the 8% floor, circulation
  efficiency (hall sqft / total), plumbing-cluster spread (`WET_GROUP`'s
  distance), proportion penalties.

Serialize it in `compile --json` and `build --json`. This single feature
unlocks hill-climbing, best-of-N selection, and regression detection — none of
which are possible against a boolean.

### 1.2 Fix the loop mechanics in `agent.py`
- **Keep the best iteration, not the last.** `design()` returns
  `history[-1]` (`agent.py:208-209`); a regression on the final round is
  silently returned. With a score, return `max(history, key=score)`.
- **Feed the agent JSON, not the formatted report.** `write_source` pastes
  `result.report()` — carets and all (`agent.py:139-143, 206`) — even though
  `to_dict()` (`compiler.py:972-1001`) exists. Structured
  `code/line/room/hint` fields are easier for a model to act on than scraping
  the human rendering, and cheaper in tokens.
- **Best-of-N generation.** Generate 2–4 candidates per round (temperature or
  prompt-angle variation), compile all, keep the best-scoring. The compiler is
  free; the model call is the only cost, and candidates parallelize.
- **Anchor the critic.** Give `_CRITIQUE_SYSTEM` the score and the info-level
  diagnostics as its rubric and require it to justify `satisfied` against
  them. Today two runs can disagree with no anchor (`agent.py:157-175`).
- **Mechanize the brief.** Extract a `program` line from the natural-language
  brief as step 0 (one cheap model call, or ask for it inside the first
  generation) and inject it into every candidate. `PROGRAM_MISMATCH` then
  guards "clean ≠ correct" mechanically — today nothing checks the generated
  plan actually has the beds/baths/sqft asked for unless the model remembers
  to write the `program` line itself.

### 1.3 Declare intent in the DSL: a `require` block
`program` proved the pattern: declared intent, mechanically checked. Extend it
beyond counts:

```barn
require adjacent kitchen dining
require exterior living south          # living wants south glazing
require area great_room >= 300
require separate master_bed garage     # no shared wall
```

Each unmet `require` is a warning with a hint. This is the single best DSL
change for agents: the brief's *spatial* intent (not just the program) becomes
part of the source and survives every revision — the compiler, not the model's
memory, holds the requirements. It also makes briefs diffable and lets the
layout solver consume the same constraints.

### 1.4 Align the solver's objective with the design checks
`solve_layout2`'s `_score` ignores every one of the ~25 design-quality codes
(`WET_GROUP`, `NO_CLOSET`, `MASTER_ENSUITE`, `BED_SOUND`, `NO_BACK_DOOR`,
`KITCHEN_FLOW`, …), so auto-layout reliably emits plans that immediately fire a
pile of infos it never tried to avoid. Two options, cheapest first:
- **Post-layout repair pass**: after placement, run the validator and apply
  mechanical fixes — carve a closet from a bedroom over 110 sqft, add a back
  door on the far exterior wall, swap two private-band rooms to cluster wet
  walls. Each fix is deterministic and re-validated.
- **Fold info counts into `_score`** as a trailing term so topology selection
  prefers the candidate with fewer design nudges.

### 1.5 Structured fix-its
Hints are free text (`validation.py:146`) — often verbatim-applicable, but the
agent still has to translate them into edits. Where the fix is mechanical
(widen a hall, move an offset, resize a window to clear egress), attach an
optional machine edit to the `Issue` — `{line, replacement}` — and add
`barndsl fix FILE` to apply all safe ones. The agent then spends its tokens on
*design* decisions, not door-width arithmetic; deterministic errors get
deterministic repairs.

---

## 2. DSL additions that raise design quality and Revit fidelity

### 2.1 Garage / overhead doors (biggest missing barndo feature)
A barndominium's defining feature is the shop bay with an overhead door — and
it's inexpressible. `door garage south exterior width 12` places a person-door
today; garage doors are only *exempted* from size checks (`validation.py:1354`).

```barn
door shop south overhead width 12 height 10
```

Gives you: correct rendering, `POST_IN_OPENING` interplay with the frame (a
12 ft opening in a 12 ft bay matters), a header-span note, the right Revit
family category (garage-door families exist and host like doors), and honest
egress logic (an overhead door isn't egress). Small grammar change, large
authenticity gain.

### 2.2 Carry door swing into the exchange
`swing_into` / `hinge` (`elements.py:219-225`) never reach `RevitOpening`
(`revit.py:147-169`), and the builder never sets hand/facing flips
(`builder.py:750`). The architect's (or validator's — `DOOR_SWING_CLASH`,
`DOOR_BLOCKS_HALL`) carefully-chosen swing is discarded and every Revit door
lands at the family default. Add `swing`/`hinge` fields to the opening and set
`FamilyInstance.FacingFlipped` / `HandFlipped` in `_build_openings`.

### 2.3 Wall attribute hints
One exterior and one interior thickness exist (`constants.py:65-66`); there is
no way to mark a **plumbing wall** (2x6 wet wall — `WET_GROUP` already finds
the shared wall, it just can't be declared), a **bearing** designation (the
frame docs say "honour an explicit interior bearing wall as a post line" —
still open), or the **fire-rated** garage separation the checks only *remind*
about. A modest `wall <a> - <b> plumbing|bearing|rated` statement closes the
loop: the validator can then verify (not nudge), the frame can use bearing
walls, and the exchange can carry a per-wall type hint the builder maps to a
real wall type.

### 2.4 Window and door types
Windows are bare rectangles; doors have kind/width only. `window ... casement|
slider|fixed` and `door ... double` map directly to Revit family choices and
sharpen egress checks (a fixed window is not an escape opening — today
`BEDROOM_EGRESS` can pass on glazing that couldn't open).

### 2.5 Solar awareness (orientation is already there)
`orientation` exists but nothing consumes it (`elements.py:504` claims Revit
uses it; see §3.1). Two cheap wins: an info check (south-facing living glazing,
west-facing bedroom heat, per the plan's azimuth) and a score term — then agents
can be asked for passive-solar layouts and be *checked* on them.

---

## 3. Revit integration: fidelity fixes

Ordered by user-visible impact; most are small.

### 3.1 Dead fields on the builder side
- **`orientation` never rotates Project North.** Carried in the exchange
  (`revit.py:319`) but `builder.py` never reads it. Set the project's true
  north (`ProjectLocation`/`ProjectPosition.Angle`) — without it, sun studies
  and shadows in the built model are wrong.
- **`siding`/`roofing` hints select nothing** (`revit.py:320-321` vs
  `_pick_wall_types`, `builder.py:265-279`). Map hint keywords → named types
  via the existing `config.json` mechanism (`"metal"` → the metal-panel wall
  type) with the current auto-pick as fallback.
- **Column/beam sizes are ignored.** `Post.size` rides the exchange
  (`revit.py:381`) but `_build_structure` (`builder.py:882-918`) never sets a
  section — duplicate-and-size the family type exactly as `_sized_symbol`
  already does for doors/windows (`builder.py:694-724`).
- **`egress` reaches no parameter.** Write it to a shared parameter or the
  door's Comments so a Revit schedule can filter egress doors.

### 3.2 Cased openings build as leaf doors
The exchange distinguishes `cased_opening` (`revit.py:930-931`) but
`_build_openings` collapses every non-window to a door family
(`builder.py:734`) — the open-concept walk-through gets a swinging leaf. Use an
opening family / wall opening, or fall back to a door family with a
`barndsl: cased` note. For very wide `open`s between public rooms, consider
emitting a **Room Separation Line** instead of a wall-with-hole — that's the
Revit-native way to bound two open-plan rooms.

### 3.3 Fixture placement ignores its wall
`fx.wall` (which wall the fixture backs onto) is in the exchange
(`revit.py:263-278`) but `_build_fixtures` places free-standing, unrotated
instances at the centre point (`builder.py:942-955`). Rotate to face away from
the backing wall and nudge flush to it — the seeds then read as a real bath
layout instead of a scatter that must be re-placed by hand.

### 3.4 Monitor roof is silently a gable
`roof_plan` treats anything non-shed as a gable (`revit.py:711`) — the raised
clerestory aisle, the classic barn form the DSL advertises, never reaches
Revit. Model it as three roof planes (two low side sheds + raised centre gable)
with clerestory stub walls, or at minimum flag the downgrade in the build
report instead of silently substituting.

### 3.5 L/T/U approximations
The roof outline is the *bounding box* (`revit.py:729`), so an L-plan gets a
roof over the notch; upper slabs are the bounding box of the level's rooms
(`revit.py:1096-1101`), flooring over open-to-below voids. Roof each footprint
section (the sections are already first-class — the slab pass at ground level
does exactly this) and union the upper rooms instead of boxing them.

### 3.6 Round-trip completeness (exchange v2)
`exchange_to_plan` drops `roof_style`/`roof_pitch` (never restored,
`revit.py:1227-1236`), `program`, `frame` spec, `notes`, and `accessible`;
`read_model` additionally flattens door kinds and per-room ceilings. For the
*forward* fields the fix is cheap — add optional fields (`program`, `frame`,
`roof.style`, `notes`, `accessible`, `swing`) and restore them on import. The
declared program surviving the round-trip matters most: it means a plan that
went through Revit and back still knows what it was *supposed* to be, and
`PROGRAM_MISMATCH` still guards edits made in Revit.

### 3.7 Live-Revit validation harness
Four of the most visible outputs (gable-end profiles, eave slopes, stairs, the
section) are experimental and degrade silently to flat/absent with only a
build-log note. Since a live Revit is required anyway, add a **smoke-test
plan + expected-report fixture**: one `.barn` exercising every pass, plus a
checklist script that diffs the `buildlog.json` against expected outcomes — so a
human with Revit can validate a release in minutes and regressions are caught
by comparing logs, not eyeballing models.

---

## 4. Architect experience / wider usefulness

1. **`barndsl cost` — still the top unbuilt roadmap item.** `metrics()`
   already carries every quantity needed (`docs/PRODUCT_REVIEW.md` #1). With a
   score (§1.1) and cost, agents can optimize *value* ("best 3-bed under
   $260k"), which is the question real clients ask.
2. **Jurisdiction profiles.** Every threshold is a compiled-in constant
   (`validation.py:63-127`, `constants.py`). A small profile file (IRC year +
   local amendments: egress sizes, hall widths, frost depth, climate-zone
   glazing) makes the checker honest outside the default assumptions and is
   mostly a constants-plumbing refactor.
3. **Furniture-fit checks.** Fixtures proved the pattern (`fixtures.py`).
   Bedrooms have standard contents — a queen bed + clearances is a
   deterministic rectangle test; a `BED_FIT` info ("fits a queen, not a king")
   is the kind of concrete feedback clients actually understand.
4. **Kitchen work-triangle.** Sink/range/refrigerator seeds already exist with
   coordinates; the triangle perimeter test (13–26 ft rule) is a few lines and
   upgrades `KITCHEN_FIT` from "big enough" to "works well."
5. **Electrical/life-safety seeds.** IRC receptacle spacing (E3901: no point
   on a wall >6 ft from an outlet), smoke/CO alarm placement (per bedroom +
   hall + per level) are deterministic derivations — same pattern as fixtures:
   derive seeds, carry in the exchange, place families in Revit, schedule
   them. Closes the `ALARM_CO` "reminder, can't model" gap and is a real
   deliverable (an electrical plan sketch) for very little geometry work.
6. **A shareable HTML report.** `watch` + SVG serve the author; nothing serves
   the *client*. One self-contained HTML (plan SVG per level, schedules,
   metrics, program recap, diagnostics) — `design_review.py` already renders
   most of this for the rule-review workflow; repointing it at a single plan
   gives architects a send-to-client artifact without Revit.
7. **Simple 3D massing export.** The exchange already contains everything a
   massing view needs (walls with heights, gable apexes, roof planes). A
   dependency-free glTF (or even isometric SVG) export would let clients see
   the *form* — the thing a floor plan least communicates — without opening
   Revit.

---

## 5. Bugs found during review

- **`metrics()` ignores wings for perimeter/wall area**
  (`elements.py:1069-1071`): perimeter and exterior-wall area use only the
  primary envelope, so L/T/U takeoffs (and any cost built on them) are wrong,
  while `footprint_area`/foundation correctly use the union.
- **Roof area ignores the authored pitch and style**
  (`elements.py:1074`): the slope factor is always `DEFAULT_ROOF_PITCH`, even
  when `roof ... pitch` sets `plan.roof_pitch` (`elements.py:518,616`) — and
  the comment above it claims the factor "follows the actual pitch," so the
  code contradicts its own comment.
- **Stair checks assume the primary rectangle** (`validation.py:353-358`,
  `1518-1524`): a stair legitimately placed in a wing can be flagged
  out-of-bounds / floating.
- **Interior-door egress is hardcoded `False` in the exchange**
  (`revit.py:942`) — harmless today but wrong if a room's egress ever routes
  through an interior door.

---

## Suggested sequencing

1. **Bug batch** (§5) — small, self-contained, protects the cost work.
2. **Score + agent loop** (§1.1, 1.2) — the highest-leverage change for agent
   design quality; no grammar changes needed.
3. **`require` block + overhead doors** (§1.3, §2.1) — the two grammar
   additions with the best value-per-line.
4. **Revit fidelity batch** (§3.1–3.3) — dead fields, cased openings,
   fixture orientation; each is a contained builder/exchange change.
5. **`cost` + jurisdiction profiles** (§4.1, 4.2) — turns the takeoff into
   decisions.
6. **Reach**: monitor roof, L/T/U roofs, exchange v2, MEP seeds, HTML report,
   massing export.
