# Authoring barndsl — a guide for designers (human and AI)

This is how to **describe a barndominium floor plan in the barndsl language** and
drive it to a clean compile. It's written for an agent in a compile-fix loop, but
it's just as useful at a keyboard.

> The single source of truth for the grammar is `DSL_REFERENCE`, exported from
> the package. Print it any time:
> ```bash
> python -c "from barndsl import DSL_REFERENCE; print(DSL_REFERENCE)"
> ```
> This document adds the *how* and *why* — patterns, the rules the compiler
> enforces, and the mistakes that bite first.

## The loop

```
write .barn  ─▶  barndsl compile FILE  ─▶  read diagnostics ─▶  fix ─▶  repeat
                                          └▶ COMPILE OK ─▶  barndsl build FILE --out plan.svg
```

Every diagnostic has a `line:col`, a code, a caret under the exact token, and a
`hint` with a concrete DSL fix. **Fix every `error`. Address `warning`s where
reasonable. `info`s are design-quality nudges** — not blockers, but worth
heeding.

```bash
barndsl compile plan.barn          # diagnostics only
barndsl build   plan.barn --out plan.svg   # compile + render if valid
```

## The mental model

- **Units are feet.** Everything is a plain number; no `ft`, no fractions like
  `10'6"` — write `10.5`.
- **Origin `(0,0)` is the south-west (bottom-left) corner.** `x` increases east,
  `y` increases north.
- A room placed at `x,y` with `size W x L` occupies `[x, x+W]` east-west and
  `[y, y+L]` south-north. So its **south** wall is at `y`, **north** at `y+L`,
  **west** at `x`, **east** at `x+W`.
- An **exterior wall** is one that lies on the envelope edge (`x=0`, `y=0`,
  `x=envelope_width`, or `y=envelope_length`). Windows and entries only "count"
  on exterior walls — see the rules below.

## Statements

```barn
plan "Name"
envelope <W> x <L>                 # the steel-frame footprint
ceiling <H>                        # >= 7; 9–12 is typical
note "free text"                   # optional; repeatable

room <id>: <type> <placement> size <W> x <L> [level <n>]
door <id_a> - <id_b> [width <w>]                  # interior; rooms MUST share a wall
entry <id> <wall> [width <w>] [offset <o>] [no-egress]   # exterior door
window <id> <wall> [width <w>] [offset <o>]
porch <id> at <x>,<y> size <W> x <L> [covered|open]
```

- `<type>`: `living, kitchen, dining, bedroom, bathroom, half_bath, laundry,
  utility, hallway, closet, pantry, mudroom, office, loft, garage, shop, porch,
  other`.
- `<wall>`: `north | south | east | west`.
- `<offset>` is feet from the wall's **start corner** (its south or west end) to
  the near edge of the opening. The opening must fit: `offset + width <= wall
  length` (and `offset >= 0`).
- `#` starts a comment. One statement per line. Braces `{ }` are ignored if you
  use them.

## Placement: prefer relative

You can place a room **absolutely** (`at <x>,<y>`) or **relatively** by abutting
an already-defined room:

| keyword (+ aliases)          | puts the new room …                    |
|------------------------------|----------------------------------------|
| `east-of`  (`right-of`)      | flush against the ref's **east** wall  |
| `west-of`  (`left-of`)       | flush against the ref's **west** wall  |
| `north-of` (`above`)         | flush against the ref's **north** wall |
| `south-of` (`below`)         | flush against the ref's **south** wall |

```barn
room living:  living  at 0,0          size 24 x 30
room kitchen: kitchen east-of living  size 24 x 18   # shares living's east wall
```

**Why relative is the default:** abutting rooms automatically **share a wall**,
which is exactly what an interior `door` requires. Placing by hand is where
adjacency bugs come from — a room one foot off, or two rooms that only touch at a
corner, won't share a wall and the door fails. The reference room must be defined
*above* the line that points at it.

Relative placement still leaves gaps if your sizes don't tile the footprint —
it fixes *adjacency*, not *bin-packing*. Watch the `AREA_UNUSED` info and the
metrics to see how much footprint is unallocated.

## Levels and lofts

`level <n>` (default `0` = ground) puts a room on an upper floor.

```barn
room living: living at 0,0 size 30 x 30
room loft:   loft   at 0,0 size 30 x 12 level 1   # sits ABOVE part of living
door living - loft width 3                          # cross-level door = a stair
```

Rooms on **different levels don't overlap** (the loft is above, not beside, the
room below) and don't share walls. A `door` between levels is read as a stair and
is valid when the two rooms **stack** (their footprints overlap). Vertical
circulation isn't modelled further yet, so an unreachable loft is a `warning`,
not an error.

## The rules the compiler enforces

**Errors (must fix):**
- Rooms stay inside the envelope and don't overlap (same level).
- A `door` connects two *different* rooms that **share a wall** (or, across
  levels, stack). Corner-only contact is **not** a shared wall.
- Bedrooms: area ≥ 70 sq ft, smallest side ≥ 7 ft, and an **egress** opening — a
  `window` (or its own `entry`) **on an exterior wall**.
- Every interior room is **reachable** from an `entry` through interior doors.
- An `entry` must be on an **exterior** wall (it can't open onto another room).
- Openings fit on their wall (`offset + width <= wall length`).
- Numbers are finite; ids/names are non-empty.

**Warnings (should address):**
- 8% natural-light glazing for habitable rooms (`living, kitchen, dining,
  bedroom, office, loft`) — windows must be on **exterior** walls to count.
- A `window` on an interior wall (gives no daylight/egress).
- Hallway ≥ 3 ft; interior door ≥ 30 in; at least one egress door ≥ 32 in; a
  bathroom exists.

**Info (design quality — heed when you can):**
- `KITCHEN_FLOW` — open the kitchen to dining/living.
- `BED_PRIVACY` — don't open a bedroom straight onto a public room; buffer with a
  hallway.
- `BATH_DISTANCE` — keep a bath within a door or two of the bedrooms.
- `AREA_UNUSED` — a lot of footprint is unallocated.

> These checks are approximate (loosely IRC-based) and are **not** a substitute
> for a licensed designer or the authority having jurisdiction.

## Idioms that keep you out of trouble

- **Open core, private wing.** Open-concept `living`/`kitchen`/`dining`
  connected directly; bedrooms and baths off a `hallway` spine.
- **Put habitable rooms on the perimeter** so they can have exterior windows.
  An interior room (no envelope wall) will trip `NAT_LIGHT` — open it to a
  neighbor instead.
- **Tile, then connect.** Lay rooms with relative placement so neighbors abut,
  then add a `door` for each adjacency you want to walk through.
- **Egress first.** Give every bedroom a window on an exterior wall early; it's
  the most common hard error.

## A complete, clean plan

This compiles with **0 errors, 0 warnings, 0 info**:

```barn
plan "Maple Two-Bed"
envelope 48 x 30
ceiling 10
note "2 bed / 1 bath, open living-kitchen, bedrooms off a hall."

room living:  living   at 0,0            size 24 x 30
room kitchen: kitchen  east-of living    size 24 x 18
room hall:    hallway  north-of kitchen  size 24 x 4
room bed1:    bedroom  north-of hall     size 10 x 8
room bed2:    bedroom  east-of bed1      size 9 x 8
room bath:    bathroom east-of bed2      size 5 x 8

door living - kitchen width 8
door living - hall width 3
door hall - bed1 width 2.7
door hall - bed2 width 2.7
door hall - bath width 2.7

entry living south width 3 offset 10

window living west width 10 offset 8
window living south width 8 offset 4
window kitchen south width 10 offset 6
window kitchen east width 6 offset 4
window bed1 north width 4 offset 3
window bed2 north width 4 offset 2
window bath north width 3 offset 1
```

## Reading a diagnostic

```
maple.barn:10:6: error[BEDROOM_AREA] (bed2): Bedroom is 64 sq ft; IRC minimum is 70 sq ft.
    room bed2:    bedroom  east-of bed1      size 8 x 8
         ^~~~
    hint: Enlarge it, e.g. `size 8 x 9`.
```

`file:line:col`, severity + `[CODE]` + the room, the message, the **source line
with a caret** under the offending token, and a `hint` you can usually apply
verbatim. Apply the hint, recompile, repeat until `COMPILE OK`.

## Two front-ends, one core

You can also build a plan with the embedded **Python builder** — same rules, same
core object; `emit_dsl(plan)` serialises it back to this language.

```python
from barndsl import barndominium, RoomType as T, validate, emit_dsl

plan = (
    barndominium("Maple Two-Bed")
    .envelope(48, 30).ceiling(10)
    .add_room("living", T.LIVING, x=0, y=0, width=24, length=30)
    .add_room("kitchen", T.KITCHEN, east_of="living", width=24, length=18)
    .entrance("living", "south", width=3, offset=10)
)
print(validate(plan))   # same diagnostics
print(emit_dsl(plan))   # → .barn source
```
