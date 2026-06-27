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
ceiling <H>
note "free text"
room <id>: <type> <placement> size <W> x <L> [level <n>]
door <id_a> - <id_b> [width <w>]
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
and don't overlap; bedrooms meet min area/dimension and have **egress**; every
interior room is **reachable** from an entrance via interior doors; habitable
rooms meet the **8% natural-light** ratio; hallway/door widths; ceiling height;
at least one egress door. Every diagnostic includes a concrete fix in DSL terms.

**Three severities, one channel.** `error`s must be fixed; `warning`s flag likely
problems; `info`s carry **design-quality** guidance — open-concept kitchen flow,
bedroom privacy, bath proximity — so "is it good?" travels the same diagnostic
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
barndsl build   examples/cedar_ridge.barn --out plan.svg
barndsl demo --out cedar_ridge.svg                 # compile + render the example
barndsl design "2 bed barndo with a 30x40 shop, ~1500 sq ft" --out plan.svg
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
  render.py      # annotated 2D SVG renderer
  agent.py       # Claude write → compile → critique → revise loop
  cli.py         # `barndsl` command
examples/
  cedar_ridge.barn   # the worked plan in DSL (used by `barndsl demo`)
  simple_barndo.py   # the same plan via the Python builder
tests/             # 17 tests, no API key required
```

## Roadmap

- Auto-layout: solve room placement from an adjacency brief (relative and pocket
  placement are the first steps)
- Cost estimation from the material takeoff
- More residential building types beyond barndominiums

## License

MIT
