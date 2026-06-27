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
heeding. The summary line counts all three: `COMPILE OK — 0 error(s), 1
warning(s), 0 info(s)`.

```bash
barndsl compile plan.barn                  # diagnostics + a one-line program recap
barndsl compile plan.barn --show-coords    # + each room's resolved rectangle
barndsl compile plan.barn --metrics        # + full area/material takeoff
barndsl build   plan.barn --out plan.svg   # compile + render if valid
```

**A clean compile does not mean you built the right plan.** It checks the code is
valid, not that you met the brief — you can compile `0/0/0` while having dropped a
bedroom. `compile` prints a one-line `Program: N bed / M bath · … sq ft` recap so
you can check the program against the brief; `--metrics` gives the full takeoff.

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

### The anchor rule (read this — it prevents most placement errors)

An anchor sets **both** coordinates of the new room:

- `east-of`/`west-of` butt against the reference's east/west wall **and copy its
  `y` (south edge)** — they align horizontally.
- `north-of`/`south-of` stack on the reference's north/south wall **and copy its
  `x` (west edge)** — they align vertically.

So **chaining in one direction is safe** (a stacked column, or a row):

```barn
room hall: hallway at 0,26 size 46 x 3
room bed1: bedroom north-of hall  size 12 x 11   # x = hall.x = 0
room bed2: bedroom east-of bed1   size 12 x 11   # y = bed1.y; sits beside bed1
room bed3: bedroom east-of bed2   size 12 x 11
```

But **branching re-anchors to the deeper room.** If you place `B north-of A`,
then `C east-of B`, then `D north-of C`, `D` inherits `C`'s (deeper) `x` and may
collide with rooms to its west. When a wing goes wrong this way, you'll see an
`OUT_OF_BOUNDS` or `OVERLAP` with a fix hint, or a `DOOR_NOADJ` whose hint lists
the room's *actual* neighbours. To lay a **hallway spine**, place **every** served
room directly off the spine (`north-of hall`, `east-of hall`, …), one room deep —
don't chain a second column off a room that's already off the spine, or it stops
touching the hall.

Relative placement fixes *adjacency*, not *bin-packing* — it won't tile the
footprint for you. It also can't offset a room partway along a shared wall (the
anchor always aligns to the reference's corner); for that, fall back to `at
<x>,<y>`. Use `barndsl compile FILE --show-coords` to print every room's resolved
rectangle and which walls ended up exterior — the fastest way to see what a chain
of anchors actually produced.

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

`<n>` must be a **whole number ≥ 0** (`0` = ground, `1` = the floor above);
non-integer or negative levels are rejected (`BAD_LEVEL`). A loft's exterior
walls are still computed from the envelope edges in plan view, so a loft **inset**
from the envelope (like the `30 x 12` example above, whose north wall is interior)
can't take a window that counts for daylight/egress — put the loft against an
envelope edge if it needs a real window.

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
core object; `emit_dsl(plan)` serialises it back to this language (round-trips
losslessly, including `level`).

The builder mirrors the DSL:

- `add_room(id, type, *, width, length, x=, y=, level=0, label=, east_of=,
  west_of=, north_of=, south_of=)` — relative anchors use **underscores**
  (`east_of=`), and the reference must be added *before* this call.
- `connect(a, b, width=)` is an interior `door`; `entrance(room, wall, …)` is an
  `entry`; `add_window(room, wall, …)`; `add_porch(id, …)`.
- `type` and `wall` accept the enum **or** a string (`"living"`, `"south"`) and
  are validated immediately (a bad value raises `ValueError`, not a late crash).
- `level=` must be a whole number ≥ 0, same as the DSL.

```python
from barndsl import barndominium, RoomType as T, validate, emit_dsl

plan = (
    barndominium("Maple Two-Bed")
    .envelope(48, 30).ceiling(10)
    .add_room("living", T.LIVING, x=0, y=0, width=24, length=30)
    .add_room("kitchen", T.KITCHEN, east_of="living", width=24, length=18)  # abut
    .add_room("loft", T.LOFT, north_of="kitchen", width=24, length=10, level=1)
    .connect("living", "kitchen", width=8)
    .entrance("living", "south", width=3, offset=10)   # string wall is fine
)
print(validate(plan))   # same diagnostics
print(emit_dsl(plan))   # → .barn source (loft emitted as `... level 1`)
```

## Porches

`porch <id> at <x>,<y> size <W> x <L> [covered|open]` is an exterior platform. It
may sit **outside** the envelope (e.g. a front porch at a negative `y`) and is
exempt from the overlap / out-of-bounds / area checks that apply to rooms — so
place it wherever it physically goes (typically just outside an `entry`).
