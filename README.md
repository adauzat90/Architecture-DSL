# barndsl

A small **architecture description language** with a **compiler**, plus an
**agentic workflow** that designs floor plans by writing that language and
iterating against the compiler's feedback. The MVP is focused on
**barndominiums** (metal-frame post-and-beam homes).

The core idea: give an AI a *language to describe architecture in*, and a
*compilation step* that tells it — precisely, with line numbers and fix hints —
what's wrong and how to make it right. The compiler is the teacher; the
diagnostics steer the design toward something valid and well laid out.

```
DSL source ─▶ compile ─▶ ┌── ok? ──▶ render SVG
                          └── diagnostics (line, code, hint) ─┐
                                                              │
   author / agent rewrites the DSL  ◀──────── feedback ───────┘
```

## The language

You describe a plan as source code. One statement per line; `#` for comments;
measurements in feet; origin `(0,0)` at the south-west corner (`x`→east,
`y`→north).

```barn
plan "Cedar Ridge"
envelope 60 x 40
ceiling 12

room great_room: living   at 0,0          size 28 x 26
room kitchen:    kitchen   east-of great_room size 18 x 26   # abut, no hand-math
room master_bed: bedroom   north-of great_room size 16 x 11

door great_room - kitchen width 8         # interior door (rooms must share a wall)
entry great_room south width 3 offset 20  # exterior door, on an exterior wall
window master_bed north width 5 offset 5  # egress window, on an exterior wall
porch front_porch at 0,-8 size 28 x 8 covered
```

Full grammar:

```
plan "Name"
envelope <W> x <L>
wing <W> x <L> at <x>,<y>          # optional; L/T/U footprints (repeatable)
ceiling <H>
floor <D>                          # optional; inter-floor assembly depth (ft). floor-to-floor = ceiling + D
accessible                         # optional; opt in to accessibility / aging-in-place nudges
note "free text"
program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]  # optional; intent, checked vs the rooms
require adjacent|separate <room_a> <room_b>   # optional; spatial intent, checked vs the plan
require exterior <room> [<wall>]              #   (also: require area <room> >= <sqft>)
room <id>: <type> <placement> size <W> x <L> [level <n>] [ceiling <h>] [vaulted]
wall <id_a> - <id_b> plumbing|bearing|rated   # optional; attribute(s) of the shared wall between two rooms
roof gable|shed|monitor [pitch <rise:run>]                           # optional; roof form (default gable)
orientation <degrees>              # optional; compass azimuth plan-north (+y) points (0 = true north)
finish [siding "<name>"] [roof "<name>"]  # optional; exterior material hints (metal siding, standing-seam)
door <id_a> - <id_b> [swing|cased|pocket|sliding|double|french] [width <w>] [offset <o>] [into <room>] [hinge near|far]
door <id> <wall> exterior [double|french] [width <w>] [offset <o>] [no-egress]   # exterior door
door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]  # overhead/sectional garage door (9 x 7 default; width 16 = double)
open <id_a> - <id_b> [width <w>] [offset <o>]   # shorthand for `door <a> - <b> cased ...`
entry <id> <wall> [double|french] [width <w>] [offset <o>] [no-egress]   # shorthand for `door <id> <wall> exterior ...`
window <id> <wall> [casement|slider|fixed|double-hung] [width <w>] [offset <o>] [sill <s>] [head <h>]
porch <id> at <x>,<y> size <W> x <L> [covered|open]
stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]   # auto post-and-beam frame
```

`<placement>` is absolute — `at <x>,<y>` — or **relative**: `east-of`,
`west-of`, `north-of`, `south-of <room>` (aliases `right-of`, `left-of`,
`above`, `below`) abuts an already-defined room flush to its corner, so the two
share a wall and a `door` between them resolves — no coordinate bookkeeping. Add
`align near|far|center` and/or `offset <n>` to slide the room along the shared
wall.
A `door` is a swinging door; `open <a> - <b> [width <w>]` is a **cased opening /
walk-through** — an open passage with no door leaf (the open-concept link
between e.g. a kitchen and a living area). It joins the two rooms in the
circulation graph exactly like a door, but renders as a plain gap (no swing
arc), defaults to a wide opening, and is exempt from the narrow-door warning.
An `open` into a **bathroom** is a privacy defect (a bath needs a door), so it
warns (`OPEN_BATH`).
`door <id> <wall> overhead` is an **overhead/sectional garage door** on a
garage/shop bay's exterior wall — 9 × 7 by default (`width 16` for a double); it
renders as a gap with a dashed track (no swing arc) and is never an egress door
or a building entrance, so the plan still needs a people-door `entry`.
`program` declares the intended counts and `require` the brief's **spatial**
intent (a required adjacency, separation, exterior wall, or minimum room area);
both are re-checked mechanically on every compile (`PROGRAM_MISMATCH` /
`REQUIRE_UNMET` warnings), so the brief lives in the source and survives every
revision.
`wall <a> - <b> plumbing|bearing|rated` declares what the **shared wall**
between two abutting rooms *is* (the pair must really share one — `WALL_NOADJ`
otherwise): a `plumbing` wall is the 2x6 wet wall the fixtures back onto (it
satisfies the wet-room grouping nudge, thickens the flanking rooms'
clear-dimension math, and hints a thicker wall type in the Revit exchange —
`WALL_UNUSED` if no wet room backs onto it); a `bearing` wall is an interior
bearing wall the auto `frame` honours as an **interior post line** when it runs
along the building's long axis (one parallel to the bents' span can't split it —
`WALL_BEARING_AXIS`); a `rated` wall records the garage/dwelling fire separation
as built, turning the `GARAGE_SEPARATION` reminder into a verified fact (it
silences for that pair; a garage ceiling under habitable space still reminds).
**Window kinds** make egress honest: the default `casement` clears ~its full
glazed size (exactly the historical math, so old plans are unchanged), a
`slider` clears ~half its glazed width, a `double-hung` ~half its glazed height,
and `fixed` glass still daylights (`NAT_LIGHT`) but is **never** an escape
opening (`BEDROOM_EGRESS`/`EGRESS_SIZE`). A `double`/`french` door (interior or
exterior) is a pair of half-width leaves — its egress clear width counts **one
leaf** (IRC R311.2), it checks against stock pair widths (48/60/64/72 in), and
it renders as two leaves.
The footprint is one rectangle by default. For an **L/T/U-shaped building**, add
`wing <W> x <L> at <x>,<y>` blocks: the footprint becomes the union of the
`envelope` (the primary block at the origin) and every wing. Containment,
exterior walls (a wall on the seam between two blocks is *interior*; one facing a
notch is *exterior*), daylight/egress, area and the drawn outline all follow the
rectilinear shape — see [`examples/lshape.barn`](examples/lshape.barn). (The
auto-layout solver still targets a single rectangle; wings are for authored or
builder plans.)

A room can override the plan ceiling with `ceiling <h>` (a tray, a dropped
soffit, or a taller great room) and be marked `vaulted` — open to the roof, so no
flat ceiling plane is built for it and the low-ceiling minimum doesn't apply. The
`roof` directive picks the building's roof form: `gable` (default, ridge down the
long axis), `shed` (a single slope), or `monitor` (a raised centre clerestory
aisle — the classic barn form), with an optional `pitch`.

`level <n>` (default 0) puts a room on an upper floor; a `loft` on `level 1` sits
above a ground room without overlapping it. A `stair` connects floors (the upper
level becomes reachable from a ground `entry`), and `barndsl build` renders each
level as its own labelled floor plan.

`<type>`: living, kitchen, dining, bedroom, bathroom, hallway, closet, pantry,
mudroom, office, loft, garage, shop, … · `<wall>`: north|south|east|west.
Windows and entries must be on an **exterior** wall (one on the envelope edge)
to count for daylight, bedroom egress, or building access.

## The compiler

`compile_source(src)` lexes, parses, lowers to a plan, runs the building-code
checks, and returns diagnostics with **line:column locations, error codes,
column-accurate carets, and actionable fix hints** — like compiler output:

```text
$ barndsl compile broken.barn
COMPILE FAILED — 3 error(s), 1 warning(s), 0 info(s)
broken.barn:6:14: error[BAD_TYPE]: Unknown room type 'lounge'.
    room office: lounge at 24,0 size 20 x 14
                 ^~~~~~
    hint: Use one of: living, kitchen, dining, bedroom, bathroom, ...
broken.barn:5:6: error[NO_ACCESS] (kitchen): Room 'kitchen' cannot be reached from any entrance.
    room kitchen: kitchen at 24,0 size 16 x 12
         ^~~~~~~
    hint: Add `door kitchen - living` (they share a wall).
broken.barn:6:6: info[BED_PRIVACY] (bed): Bedroom 'bed' opens directly onto the living area.
    room bed: bedroom at 24,12 size 16 x 12
         ^~~
    hint: Buffer bedrooms with a hallway for privacy.
...
```

The caret underlines the exact token at fault — for syntax errors it points at
the offending word, and for semantic checks it points at the room's id, so every
diagnostic ties back to a precise span of source.

What it checks (loosely IRC-based + spatial sanity): rooms stay in the envelope
and don't overlap, and no two openings collide on the same wall; bedrooms meet min
area/dimension and have **egress** — an escape opening that also clears the R310
size minimums (~5.7 sq ft, 20 in × 24 in, sill ≤ 44 in); every interior room is
**reachable** from an entrance via interior doors — and that the *route* doesn't
force you **through a bathroom** (or a stranger's bedroom) to get there; habitable
rooms meet the **8% natural-light** ratio; hallway/door widths; a stair footprint
long enough to climb its storey; ceiling height; at least one egress door. Every
diagnostic includes a concrete fix in DSL terms.

Every diagnostic code is catalogued in `barndsl/diagnostics.py`; run
`barndsl explain BEDROOM_EGRESS` (or `barndsl explain` to list them all) for the
rationale and the IRC clause behind a check. `barndsl compile FILE --json` emits
the diagnostics as machine-readable JSON for the agent loop or other tooling.

**Three severities, one channel.** `error`s must be fixed; `warning`s flag likely
problems; `info`s carry **design-quality** guidance — open-concept kitchen flow,
bedroom privacy, bath proximity, plumbing economy (cluster wet rooms on a shared
wall), bedroom closets, room proportion, workable room sizes, **fixture
clearances** (a bath that can't hold a toilet/lav/tub with IRC R307 clearances, a
kitchen too tight for its appliances), bathroom ventilation, dead-end hallways,
**garage/dwelling fire separation** (a garage *or shop* common wall or the ceiling
under habitable space above it, and the self-closing rated door between them — IRC
R302.6 / R302.5.1), **clear-dimension** shortfalls (a room that meets a code
minimum on its centreline rectangle but not once the walls are built), and — when
a plan opts in with `accessible` — **accessibility / aging-in-place** nudges
(accessible door clear widths, a wheelchair turning space in the bath,
single-floor living, a no-step entry, per ANSI A117.1) — so "is it good?" travels
the same diagnostic
stream as "is it valid?" and never blocks a compile. The agent's architectural
critique is folded into this same `info` channel.

> These checks are approximate and **not** a substitute for a licensed designer
> or a review by the authority having jurisdiction.

```python
from barndsl import compile_source, save_svg

src = open("cedar_ridge.barn").read()
result = compile_source(src)
print(result.report())          # diagnostics
if result.ok:
    save_svg(result.plan, "plan.svg")
```

## Auto-layout: a brief, not coordinates

Don't want to place rooms at all? Give the solver an **adjacency brief** — what
rooms, how big, and which ones should touch — and it computes the coordinates,
abutting rooms so each requested adjacency shares a wall, then adds a `door` for
every one, a front `entry`, and egress/daylight `window`s. No API key; it's a
deterministic greedy placer (same brief in, same plan out).

```text
$ barndsl layout examples/birch_run.brief --emit
Laid out 6 room(s); 5/5 adjacencies satisfied

COMPILE OK — 0 error(s), 0 warning(s), 1 info(s)
...
room living: living at 38,18 size 20 x 18
room kitchen: kitchen east-of … (resolved to absolute coordinates)
door living - kitchen width 6
entry living north width 3 offset 8.5
window bed1 south width 4 offset 7
...
```

The brief is a tiny line-based format (`room <id>: <type> <W> x <L>`,
`adjacent <a> <b> …` to hang rooms off a hub like a hall).

**Two engines.** The default **`fill`** engine *dissects the envelope*: it
dimensions each room to **tile the rectangle with no wasted space**, so habitable
rooms land on the perimeter for daylight/egress by construction. Give it **target
areas** (`room living: living area 360`) and it sizes everything to fit.
Internally it generates three topologies — *bands* (public core · hall · private
row), a recursive *slice* (which can give a room three neighbours), and a
rectangular *dual* (which tiles so that *every* requested adjacency is a shared
wall, even non-sliceable graphs like a **pinwheel** — a centre room touching four
others) — and keeps whichever scores best, following the floor-planning
literature's generate-and-select approach. `--engine greedy` is the original
abutment placer, which honors fixed sizes exactly but leaves gaps and the odd
buried room. The design and the floor-planning research behind `fill` are in
[`docs/design/AUTO_LAYOUT_2.md`](docs/design/AUTO_LAYOUT_2.md).

```bash
barndsl layout examples/birch_run.brief             # fill (default): 0/0/0, tiled
barndsl layout examples/pinwheel.brief              # fill picks the dual topology
barndsl layout examples/birch_run.brief --engine greedy
```

From Python:

```python
from barndsl import RoomSpec, LayoutBrief, solve_layout, emit_dsl

out = solve_layout(LayoutBrief(
    name="Birch Run",
    rooms=[RoomSpec("living", "living", 20, 18), RoomSpec("hall", "hallway", 40, 4), ...],
    adjacencies=[("living", "hall"), ("hall", "bed1"), ...],
))
print(out.summary())          # "Laid out 6 room(s); 5/5 adjacencies satisfied"
print(out.unsatisfied)        # adjacencies the packing couldn't honour
print(emit_dsl(out.plan))     # → DSL source, ready to compile/render
```

## Structure: automatic beam placement

A barndominium is a **post-and-beam** metal building, and the app can place that
skeleton for you. Add a `frame` directive and the compiler derives the structural
grid from the footprint — deterministically, no API key:

```barn
frame bay 12 span 40 post 6
```

* **Bents (frames)** are spaced no more than `bay` feet on centre along the
  building's long axis; each is a beam/truss spanning the short axis between the
  two eave walls.
* A **ridge** member runs the length over the bents (`no-ridge` omits it).
* **Posts** land at every bent on the eave walls, up the gable end walls at the
  same bay spacing, and at the corners.
* When the span exceeds `span` feet, an **interior support post** line is added
  to split the beam; the validator flags one that strands in a room's open floor
  (`POST_OBSTRUCT`) so you can align a partition to it.
* A declared interior bearing wall (`wall a - b bearing`) running along the long
  axis is honoured as an **authored post line** — an interior post lands on it at
  every bent crossing its run, so the beams bear on the wall you named instead of
  (or in addition to) the auto-derived support line.
* The post grid is the fixed discipline, so windows and doors belong in the
  **bays between posts**. A window or exterior door that a post lands inside is
  flagged (`POST_IN_OPENING`) — shift the opening into a clear bay (a post at the
  opening's jamb is fine; only one *inside* it warns).

`barndsl build plan.barn --out plan.svg` draws the bents, ridge, and posts over
the floor plan, and the summary panel reports the bent count, post count, and
beam linear feet for the takeoff. No `frame` directive in the source? `barndsl
build plan.barn --frame` places a default frame for you. From Python it's
`plan.frame(bay=12, span=40, post=inches(6))`, or `place_frame(plan, spec)`.

```bash
barndsl build examples/frame_demo.barn --out frame_demo.svg   # frame over the plan
barndsl build examples/cedar_ridge.barn --frame --out cr.svg  # auto-frame any plan
```

> The frame is a **layout aid, not an engineered design** — member sizing,
> connections, foundations and lateral bracing are the structural engineer's job.

## Revit: lowering a plan to a buildable model

A barndsl plan is **rectangles**; Revit is **levels + walls + hosted families
(doors/windows) + rooms enclosed by walls + structural framing**. `barndsl revit`
bridges the two — it *lowers* the plan into a Revit-shaped exchange and writes it
as JSON for a **pyRevit extension** (a ribbon button inside Revit) to build:

```bash
barndsl revit examples/cedar_ridge.barn --out plan.json
# Revit exchange: 1 level(s), 12 wall(s) (4 exterior), 17 opening(s), 9 room(s), 0 column(s)
```

The lowering is **pure Python** — no Revit, no .NET, no API key — so it runs and
is tested anywhere; only the final element creation needs Revit. What it does:

* **Coordinates pass through unchanged.** barndsl's convention (`x` east, `y`
  north, feet) is exactly Revit's world XY plane, and Revit's internal unit is
  the decimal foot. `z` comes from the floor level.
* **Levels stack by floor-to-floor, not ceiling height.** An upper level sits at
  `ceiling + floor` (the inter-floor assembly depth, `floor` directive; default
  12") above the one below, so a second storey rests on the first floor's
  structure rather than dropping onto its ceiling plane.
* **The roof is a gable, not a flat cap.** The exchange carries the ridge, pitch
  and per-edge slope, and the pyRevit builder makes the eave edges slope-defining
  so a footprint roof comes out as a gable. The **gable-end walls** are flagged
  with their ridge apex and build from a vertical pentagon profile, so their tops
  rise to the roof instead of stopping flat at the plate.
* **Walls are deduplicated.** Every room edge is decomposed along its grid line
  into atomic segments, each classified *interior* (a room on both sides) or
  *exterior* (open footprint beyond), then contiguous like segments merge back
  into runs. A partition shared by two rooms becomes **one** wall centreline, not
  two coincident ones. Walls carry an `exterior` flag and a nominal `thickness`
  hint so the consumer can pick a 2x6 shell vs. a 2x4 partition wall type — and
  a segment matching a declared `wall a - b plumbing|bearing|rated` statement
  carries a `kind` key (absent otherwise) so `config.json` can map it to a real
  named wall type (a plumbing wall also raises the thickness hint to a 2x6).
* **Openings host onto walls.** Each interior door, cased opening, exterior door
  and window is matched to the wall id whose line carries it, with a centre
  point, width, height, and (for windows) sill — ready to place as a family.
* **Rooms become seed points.** A point inside each rectangle, with name/type/
  area — plus the **clear** (finish-face) width/length/area, the figure Revit
  computes for a placed room — for Revit to place a Room once the walls enclose it.
* **Wet rooms and kitchens get fixtures.** Each bathroom, half-bath and kitchen
  carries deterministic **fixture seeds** (toilet/lavatory/tub·shower;
  refrigerator/range/sink) — footprints placed against the walls with IRC R307
  clearances in mind — and the pyRevit builder drops a plumbing/appliance family
  at each. The compiler also checks the room can actually *hold* them
  (`BATH_CLEARANCE`, `KITCHEN_FIT`), so an empty box that's too small to be a real
  bath is flagged before you build.
* **Structure carries through.** A placed `frame` lowers to columns (posts) and
  framing centrelines (bents/ridge); porches and stairs come across as reference
  outlines.
* **A slab-on-grade foundation.** The footprint lowers to a monolithic
  slab-on-grade: the slab outline, a **thickened perimeter edge** (turndown /
  grade beam) with a width and depth, and a **pad footing** under each post of a
  placed frame — plus a rough concrete takeoff (cu yd) for estimating. The builder
  places the pad footings and reports the turndown run for detailing.

The JSON is the stable `barndsl.revit/1` schema. From Python it's
`to_revit_model(plan)` (a typed `RevitModel`) or `to_revit_json(plan)`:

```python
from barndsl import compile_source, to_revit_json
result = compile_source(open("cedar_ridge.barn").read())
open("plan.json", "w").write(to_revit_json(result.plan))
```

### The reverse direction (round-trip)

The exchange goes **both ways**. `exchange_to_plan` reconstructs a plan from a
`barndsl.revit/1` document — rooms carry their rectangle, and each opening's wall
and offset are re-derived from its geometry — so a model that came from (or was
edited in) Revit can return to the DSL:

```bash
barndsl revit-import plan.json --out recovered.barn   # exchange JSON → DSL
```

```python
from barndsl import exchange_to_plan, exchange_to_dsl
import json
data = json.load(open("plan.json"))
plan = exchange_to_plan(data)          # → a Barndominium
dsl  = exchange_to_dsl(data)           # → DSL source (via emit_dsl)
```

The round-trip is verified in the test suite: every gallery plan lowered to the
exchange and reconstructed recovers the same rooms, door/window connections and
envelope, and re-emits DSL that still compiles clean.

### Seeing the drift: `revit-diff`

Once a plan is in Revit the architect *nudges* it — slides a wall, widens a door,
deletes a window. `revit-diff` makes that drift visible: export the edited model
back out (the extension's **Model to DSL** / an exchange `.json`) and diff it
against the authored source.

```bash
barndsl revit-diff model.json plan.barn                 # what changed in Revit
barndsl revit-diff model.json plan.barn --json          # machine-readable
barndsl revit-diff model.json plan.barn --tolerance 0.25  # tighter move threshold
```

```
Drift: model.json vs plan.barn  (tolerance 0.5 ft)
1 moved, 1 added, 0 removed, 1 changed across 4 rooms / 5 doors / 2 windows / 7 walls
Score: authored 52 → model 58  (Δ +6)
Rooms:
  changed  D: kind office→bedroom
Doors:
  added    o4 at (40,2.5)
```

Either argument may be a `.barn` source **or** a `barndsl.revit/1` `.json`
exchange (sniffed by extension, then content). Both sides are lowered the same
way — `to_revit_model → to_dict` — so the model side (which came back from Revit,
where rooms collapse to bounding boxes and types are guessed) compares
apples-to-apples with the authored side. Elements are matched by **stable id
first**, then by **nearest position** when ids differ (Revit-drawn elements
rarely carry barndsl ids); each side reports `added` / `removed` / `moved` /
`resized` / `changed` per kind, with before→after numbers, plus the design-score
delta. Exit code: `0` no drift, `1` drift found, `2` unreadable input (a `.barn`
that doesn't compile cleanly is refused — a diff against a half-parsed plan is
meaningless). The API is `diff_plans(model, authored) -> dict` and
`diff_text(d) -> str`.

### The pyRevit extension

The Revit front-end that consumes the exchange lives in
[`revit/`](revit/README.md): a **pyRevit extension** with a **barndsl** ribbon
tab. Point pyRevit at the `revit/` folder as a custom extension directory and you
get two buttons:

* **Build Plan** — pick a compiled `.json` (or a `.barn`, compiled on the spot)
  and it creates the levels, walls, correctly-**sized** doors and windows, rooms,
  floor slabs, a footprint roof, structural members and grids, porch slabs and
  multi-flight stairs in the active document, in **one transaction** (one undo
  step). **Re-building is idempotent** — it replaces the previous barndsl build
  instead of stacking duplicates (and never touches what you drew). Offers a
  **Preview** (a real build that's rolled back) and writes a `*.buildlog.json`
  report. Primary target: **Revit 2025**.
* **Document** — make drawings from the built model: a floor-plan view per level
  with room/door/window tags, native door/window/room schedules, and a sheet per
  level. Idempotent (replaces a previous run's views/sheets/schedules).
* **Export Exchange** — pick a `.barn` and write its exchange `.json` next to it
  without touching the model.
* **Diagnostics** — report the environment and which wall/floor/family types the
  project offers (with readiness flags); the first thing to run when a build
  doesn't produce what you expect.
* **Model to DSL** *(experimental)* — the reverse: read the active model's rooms
  and door/window instances and reconstruct `.barn` source from them.

The extension is cleanly split: `lib/barndsl_revit/exchange.py` (loader/validator)
and `lib/barndsl_revit/report.py` (build report + options) are **Revit-free** and
covered by the repo test suite, while `lib/barndsl_revit/builder.py` does the
Revit API element creation. A `config.json` sidecar maps each pass to named types
in your template. See [`revit/README.md`](revit/README.md) for install,
workflows, and debugging.

## The agent: a compile-fix loop

The agent *writes architecture in the DSL*, compiles it, and feeds the compiler's
diagnostics straight back into the next prompt — the same loop a developer runs
against a compiler:

```python
from barndsl.agent import design

result = design(
    "3 bed / 2 bath barndominium ~1800 sq ft, open-concept living, "
    "a mudroom off the carport, and a covered back porch.",
    max_iterations=3,
)
print(result.source)        # the DSL the model wrote
print(result.result.report())
```

Each round: **write DSL → compile → score → critique (design quality) →
revise**, until it compiles clean, the critic is satisfied AND the
deterministic 0-100 design score clears `target_score` (default 90; `None`
disables the gate) — or the cap is hit. Every iteration is scored and the
**best-scoring one wins** (`result.best_iteration` says which), so a
regression on the last round is never returned. Uses Claude
(`claude-opus-4-8`) — the compiler's structured diagnostics plus the score's
per-component deductions are the steering signal.

## Two front-ends, one core

You can also build a plan with the **embedded Python builder** — handy for tests
and programmatic generation; it lowers to the same plan object the compiler
produces, and `emit_dsl(plan)` serialises it back to DSL source.

```python
from barndsl import barndominium, RoomType as T, Direction as D, validate, emit_dsl

plan = (
    barndominium("Cedar Ridge")
    .envelope(width=60, length=40).ceiling(12)
    .add_room("great_room", T.LIVING, x=0, y=0, width=28, length=26)
    .entrance("great_room", D.SOUTH, width=3, offset=20)
)
print(validate(plan))
print(emit_dsl(plan))   # → DSL source
```

## CLI

```bash
barndsl new "Cedar Ridge" --out cedar.barn         # scaffold a clean starter plan
barndsl compile examples/cedar_ridge.barn          # diagnostics only
barndsl compile examples/cedar_ridge.barn --json   # diagnostics as JSON
barndsl compile examples/cedar_ridge.barn --strict # warnings also fail (CI gate)
barndsl compile examples/cedar_ridge.barn --profile strict   # amend code thresholds to a jurisdiction
barndsl compile examples/cedar_ridge.barn --profile travis.json  # or a JSON override file
barndsl profiles                                   # list the built-in jurisdiction profiles
barndsl score   examples/cedar_ridge.barn          # deterministic 0-100 design score
barndsl inspect examples/cedar_ridge.barn          # geometry pack: rooms, adjacency, free wall spans
barndsl compare a.barn b.barn                      # scheme A vs B: score/takeoff/diagnostic deltas
barndsl cost    examples/cedar_ridge.barn          # assembly construction cost estimate (budget)
barndsl cost    examples/cedar_ridge.barn --costs local.json --multiplier 1.15
barndsl packet  examples/cedar_ridge.barn -o plan.html  # one print-ready HTML permit packet
barndsl fmt -w examples/cedar_ridge.barn           # canonically reformat in place
barndsl build   examples/cedar_ridge.barn --out plan.svg
barndsl build   examples/cedar_ridge.barn --format png  # PNG/PDF (needs [raster])
barndsl build   examples/cedar_ridge.barn --json   # diagnostics + metrics as JSON
barndsl watch   examples/cedar_ridge.barn --out plan.svg  # recompile/render on save
barndsl schedule examples/cedar_ridge.barn         # room/door/window schedules (MD)
barndsl schedule examples/cedar_ridge.barn --format csv --out sched.csv
barndsl dxf     examples/cedar_ridge.barn --out plan.dxf  # → DXF for CAD
barndsl layout  examples/birch_run.brief --emit    # adjacency brief → placed plan
barndsl revit   examples/cedar_ridge.barn --out plan.json  # → Revit exchange JSON
barndsl revit-import plan.json --out recovered.barn        # Revit exchange JSON → DSL
barndsl revit-diff model.json plan.barn            # drift: what changed in Revit vs the authored plan
barndsl revit-log plan.buildlog.json               # what the Revit build couldn't do, as diagnostics
barndsl demo --out cedar_ridge.svg                 # compile + render the example
barndsl design "2 bed barndo with a 30x40 shop, ~1500 sq ft" --out plan.svg
barndsl explain BEDROOM_EGRESS                     # what a diagnostic code means
```

**Outputs without Revit.** `barndsl schedule` emits room/door/window schedules
(Markdown or CSV) straight from the compiler — the same data the Revit *Document*
pass schedules, but for users who don't open Revit. `barndsl dxf` exports the
plan to DXF (a minimal, dependency-free DXF R12 writer) for any CAD tool;
coordinates pass straight through (feet, x-east/y-north). `barndsl build
--format png|pdf` rasterises the SVG (optional `cairosvg`).

**Cost estimate.** `barndsl cost plan.barn` turns the takeoff into a transparent,
assembly-based budget: every line is `quantity × unit cost` with the quantity's
source named (slab, exterior/interior walls, roof, windows/doors/garage doors,
plumbing fixtures, electrical/HVAC/finish allowances). The default unit costs are
rough 2026 US national averages *for budgeting only, not a bid*; override any
subset with `--costs FILE.json` and apply a regional factor with `--multiplier`.
The total carries a ±15% low/expected/high band. `--json` emits the full sheet.

**Permit-sketch packet.** `barndsl packet plan.barn -o plan.html` binds the cover
metrics, design score, dimensioned floor plan, room/door/window schedules, cost
estimate, and diagnostics appendix into one self-contained, print-ready HTML file
(SVG inlined, no external requests, works offline). It adds no new dependency — to
get a PDF, open it in a browser and *Print → Save as PDF*; CSS page-breaks
paginate it into sections.

**Jurisdiction profiles.** The code checks enforce one IRC-flavoured rule set by
default, but a real project answers to a county or state that *amends* the
numbers. `--profile NAME_OR_JSON` (on `compile`, `score`, `build`) swaps in a
named set of thresholds so "compile under these local rules" is one flag instead
of mental math. Built-ins: `default` (alias `irc-2021`), `strict` (tighter,
accessibility-leaning), `rural` (looser). A JSON file overrides any subset of the
thresholds (`{"extends": "strict", "min_ceiling_height": 8}`); unknown keys are
rejected. `barndsl profiles` prints every built-in and the numbers it sets.
Diagnostics stay honest — a profiled message prints the number actually enforced
and names the profile and IRC base. **The non-default profiles are ILLUSTRATIVE
examples of how thresholds vary — not legal advice, and not transcribed from any
adopted code. Confirm the numbers your jurisdiction enforces with the authority
having jurisdiction.**

`design` needs `ANTHROPIC_API_KEY` (see `.env.example`).

## The design score

`barndsl score plan.barn` compiles a plan and prints a **deterministic 0–100
design score** — the "how good is it?" number to go with the compiler's "what's
wrong?". A plan with errors scores 0 (unbuildable); warnings and info nudges
deduct fixed points (8 and 2 each, capped); four continuous terms then refine —
unassigned footprint, hallway share of interior area, habitable-room elongation
past 1.6:1, and glazing shortfall below the 8% daylight floor. Same plan in,
same score out (no randomness, no LLM), and `--json` breaks the total into its
per-component deductions, so an agent can hill-climb it: compare candidates,
keep the best, catch a regression. The full formula is the module docstring in
`barndsl/score.py` — the score is a contract, not a vibe. `compile --json` and
`build --json` include the same report under a `"score"` key.

## Install

```bash
pip install -e .            # compiler + renderer (no API key)
pip install -e '.[agent]'   # + the Claude agent
pip install -e '.[raster]'  # + PNG/PDF render output (cairosvg)
```

## Project layout

```
src/barndsl/
  elements.py    # plan IR + the fluent Python builder
  geometry.py    # shared-edge / wall / opening helpers
  compiler.py    # lexer + parser + compile_source → CompileResult (diagnostics)
  emit.py        # plan → DSL source
  validation.py  # building-code checks → diagnostics with fix hints
  diagnostics.py # registry of every diagnostic code (powers `explain`)
  layout.py      # auto-layout v1: greedy abutment from an adjacency brief
  layout2.py     # auto-layout 2.0: space-filling `fill` engine (bands + slice + rectangular dual)
  structure.py   # auto post-and-beam frame placement (the `frame` directive)
  revit.py       # lower the plan IR → Revit-shaped exchange JSON (barndsl.revit/1)
  schedule.py    # room/door/window schedules → Markdown or CSV (no Revit needed)
  dxf.py         # export the plan → DXF R12 (CAD interchange), dependency-free
  scaffold.py    # the starter plan `barndsl new` writes
  render.py      # annotated 2D SVG renderer (+ PNG/PDF via optional cairosvg)
  agent.py       # Claude write → compile → critique → revise loop
  cli.py         # `barndsl` command
examples/
  cedar_ridge.barn   # the worked plan in DSL (used by `barndsl demo`)
  simple_barndo.py   # the same plan via the Python builder
  birch_run.brief    # an adjacency brief for `barndsl layout`
  pinwheel.brief     # a non-sliceable brief that exercises the rectangular dual
  lshape.barn        # an L-shaped (rectilinear) footprint via `wing`
  frame_demo.barn    # a plan with the auto-placed post-and-beam `frame`
  gallery/           # four verified-clean (0/0/0) worked plans to few-shot from
tests/             # no API key required
```

## Roadmap

- Auto-layout: the `fill` engine generates three topologies — bands, recursive
  slice, and a rectangular **dual** that handles non-sliceable adjacency graphs
  (e.g. a 5-room pinwheel) — and keeps the best-scoring one. The dual is built by
  a direct structural-grid rectangulation search (verified against the adjacency
  spec, with graceful fallback) rather than the heavier REL / planar-embedding
  pipeline, which remains the scaling path for very large room counts. See
  [`docs/design/AUTO_LAYOUT_2.md`](docs/design/AUTO_LAYOUT_2.md)
- **Revit plug-in.** `barndsl revit` lowers a plan to the `barndsl.revit/1`
  exchange (levels, deduplicated walls, hosted openings, room seeds, structural
  members), and the **pyRevit extension** in [`revit/`](revit/README.md) reads it
  and instantiates the walls, doors, windows, rooms and framing live in the
  active Revit document — a `.barn` plan becomes an editable Revit model from a
  ribbon button (targeting Revit 2025). Doors/windows are sized to the exchange
  widths, porches build as floor slabs, and stairs as multi-flight runs
  (straight or a switchback when the footprint is short). The exchange
  round-trips: `exchange_to_plan` / `barndsl revit-import` reconstruct DSL from a
  `barndsl.revit/1` document, and a *Model to DSL* button reads a live Revit model
  back, and re-building is idempotent (replaces the prior build). A *Document*
  button adds floor-plan views, room/door/window tags, schedules and a sheet per
  level. The builder is unit-tested against a fake Revit API. Remaining work
  genuinely needs a live Revit: validating the calls against Revit 2025 and
  refining the experimental pieces (the gable-roof slope, the model reader).
- Cost estimation from the material takeoff
- More residential building types beyond barndominiums

## License

MIT
