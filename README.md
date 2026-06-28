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
note "free text"
program <n> bed [<m> bath]          # optional; intended counts, checked vs the rooms
room <id>: <type> <placement> size <W> x <L> [level <n>]
door <id_a> - <id_b> [width <w>]
open <id_a> - <id_b> [width <w>]    # cased opening / walk-through (no door leaf)
entry <id> <wall> [width <w>] [offset <o>] [no-egress]
window <id> <wall> [width <w>] [offset <o>]
porch <id> at <x>,<y> size <W> x <L> [covered|open]
stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]
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
The footprint is one rectangle by default. For an **L/T/U-shaped building**, add
`wing <W> x <L> at <x>,<y>` blocks: the footprint becomes the union of the
`envelope` (the primary block at the origin) and every wing. Containment,
exterior walls (a wall on the seam between two blocks is *interior*; one facing a
notch is *exterior*), daylight/egress, area and the drawn outline all follow the
rectilinear shape — see [`examples/lshape.barn`](examples/lshape.barn). (The
auto-layout solver still targets a single rectangle; wings are for authored or
builder plans.)

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
wall), bedroom closets, room proportion, workable room sizes, bathroom
ventilation, and dead-end hallways — so "is it good?" travels the same diagnostic
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

Each round: **write DSL → compile → critique (design quality) → revise**, until
it compiles clean and the critic is satisfied (or the cap is hit). Uses Claude
(`claude-opus-4-8`) — the compiler's diagnostics are the steering signal.

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
barndsl compile examples/cedar_ridge.barn          # diagnostics only
barndsl compile examples/cedar_ridge.barn --json   # diagnostics as JSON
barndsl build   examples/cedar_ridge.barn --out plan.svg
barndsl layout  examples/birch_run.brief --emit    # adjacency brief → placed plan
barndsl demo --out cedar_ridge.svg                 # compile + render the example
barndsl design "2 bed barndo with a 30x40 shop, ~1500 sq ft" --out plan.svg
barndsl explain BEDROOM_EGRESS                     # what a diagnostic code means
```

`design` needs `ANTHROPIC_API_KEY` (see `.env.example`).

## Install

```bash
pip install -e .            # compiler + renderer (no API key)
pip install -e '.[agent]'   # + the Claude agent
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
  render.py      # annotated 2D SVG renderer
  agent.py       # Claude write → compile → critique → revise loop
  cli.py         # `barndsl` command
examples/
  cedar_ridge.barn   # the worked plan in DSL (used by `barndsl demo`)
  simple_barndo.py   # the same plan via the Python builder
  birch_run.brief    # an adjacency brief for `barndsl layout`
  pinwheel.brief     # a non-sliceable brief that exercises the rectangular dual
  lshape.barn        # an L-shaped (rectilinear) footprint via `wing`
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
- Cost estimation from the material takeoff
- More residential building types beyond barndominiums

## License

MIT
