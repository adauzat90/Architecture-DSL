# barndsl

An embedded **Python DSL** and **agentic workflow** for designing residential
floor plans — focused, for the MVP, on **barndominiums** (metal-frame
post-and-beam homes).

It has two layers:

1. **A pure, dependency-light engine** — a fluent, type-hinted DSL to describe a
   plan, a building-code validator, and a 2D SVG renderer with dimensions and a
   project summary. No API key required.
2. **A Claude-powered agent** — turns a natural-language brief into a *validated*
   plan through a **generate → validate → critique → refine** loop.

```
brief ──▶ ┌─────────┐   ┌──────────┐   ┌──────────┐   refine
          │generate │──▶│ validate │──▶│ critique │──────┐
          └─────────┘   └──────────┘   └──────────┘      │
               ▲                                          │
               └──────────  feedback (errors + critique) ─┘
```

## Install

```bash
pip install -e .            # engine only
pip install -e '.[agent]'   # + the Claude agent (needs ANTHROPIC_API_KEY)
```

## The DSL

The DSL is *embedded*: plans are ordinary Python objects built with a fluent,
chainable API. You get IDE autocomplete and type-checking, and no custom parser
to maintain.

```python
from barndsl import barndominium, RoomType as T, Direction as D, validate, save_svg

plan = (
    barndominium("Cedar Ridge")
    .envelope(width=60, length=40)      # footprint in feet
    .ceiling(12)
    .add_room("great_room", T.LIVING, x=0,  y=0,  width=28, length=26)
    .add_room("kitchen",    T.KITCHEN, x=28, y=14, width=18, length=12)
    .add_room("master_bed", T.BEDROOM, x=0,  y=29, width=16, length=11)
    .connect("great_room", "kitchen", width=8)        # interior door
    .entrance("great_room", D.SOUTH, width=3, offset=20)  # exterior door
    .add_window("master_bed", D.NORTH, width=5, offset=5)  # egress window
)

print(validate(plan))         # building-code report
save_svg(plan, "plan.svg")    # annotated 2D floor plan
```

### Coordinate system

All measurements are in **feet**. The origin `(0, 0)` is the **south-west
corner** of the envelope; `x` increases **east**, `y` increases **north**. A room
at `(x, y)` with `width` (east-west) and `length` (north-south) occupies
`[x, x+width] × [y, y+length]`.

## Validation

`validate(plan)` returns a report of `ERROR` / `WARNING` / `INFO` issues, modelled
loosely on the IRC plus spatial sanity. Among the checks:

- rooms stay inside the envelope and don't overlap;
- bedrooms meet minimum area/dimension and have an **egress** window or door;
- every interior room is **reachable** from an entrance via interior doors;
- habitable rooms meet the **8% natural-light** glazing ratio;
- hallway widths, door widths, ceiling height, at least one egress door.

> These checks are approximate and **not** a substitute for a licensed designer
> or a code review by the authority having jurisdiction.

## The agent

```python
from barndsl.agent import design

result = design(
    "A 3 bed / 2 bath barndominium around 1,800 sq ft, open-concept living, "
    "a mudroom off the carport, and a covered back porch.",
    max_iterations=3,
)
print(result.report)                 # final validation
print(result.iterations, "rounds")
```

The agent uses Claude (`claude-opus-4-8` by default) with **structured outputs**
to emit a `PlanSpec`, builds it with the same DSL, validates it, asks the model
to **critique** the design, then feeds the errors and critique back and
regenerates — until the plan is code-valid and the critic is satisfied (or the
iteration cap is reached).

## CLI

```bash
barndsl demo --out cedar_ridge.svg
barndsl design "2 bed barndo with a 30x40 shop, ~1500 sq ft" --out plan.svg
barndsl design "..." --iterations 4 --model claude-opus-4-8
```

`design` needs `ANTHROPIC_API_KEY` (see `.env.example`).

## Project layout

```
src/barndsl/
  elements.py    # dataclasses + the fluent builder API
  geometry.py    # shared-edge / wall / opening helpers
  validation.py  # building-code checks → ValidationReport
  render.py      # annotated 2D SVG renderer
  agent.py       # Claude generate → validate → critique → refine loop
  cli.py         # `barndsl` command
examples/
  simple_barndo.py   # the worked Cedar Ridge plan (used by `barndsl demo`)
tests/
  test_engine.py     # engine tests (no API key needed)
```

## Roadmap

- Multi-story / loft levels and stairs
- Auto-layout (constraint-solve room placement from an adjacency brief)
- Cost estimation from the material takeoff
- Additional residential building types beyond barndominiums

## License

MIT
