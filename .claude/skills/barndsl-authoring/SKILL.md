---
name: barndsl-authoring
description: >-
  Write, fix, or review a barndominium floor plan in the barndsl DSL — `.barn`
  source, the Python builder, or an auto-layout brief. Use whenever a task
  involves authoring or debugging a barndsl plan: it teaches the compile-fix
  loop, the coordinate model, placement anchors, the rules the compiler
  enforces (errors/warnings/infos), and the mistakes that cause the most
  compile failures. Pairs with `docs/AUTHORING.md` (the long-form guide) and
  the package's `DSL_REFERENCE` (the canonical grammar).
---

# Authoring barndsl plans

barndsl turns a floor-plan description into a validated plan and an SVG. Your job
is to write a plan that **compiles clean** (0 errors) *and* matches the brief.
Those are two different things — see "Clean ≠ correct" below.

## Ground truth — don't author from memory

The grammar can change; these two sources are authoritative, so consult them
rather than trusting this skill's snippets verbatim:

```bash
python -c "from barndsl import DSL_REFERENCE; print(DSL_REFERENCE)"   # the grammar
```
`docs/AUTHORING.md` — the full guide (anchors, wings, levels, auto-layout). This
skill is the fast path; that file is the reference.

## The loop

```
write .barn ─▶ barndsl compile FILE ─▶ read diagnostics ─▶ fix ─▶ repeat ─▶ barndsl build FILE --out plan.svg
```

```bash
barndsl compile plan.barn                 # diagnostics + a one-line program recap
barndsl compile plan.barn --show-coords   # + each room's resolved rectangle (use this when placement surprises you)
barndsl compile plan.barn --metrics       # + full area / material takeoff
barndsl build   plan.barn --out plan.svg  # compile, then render if valid
```

Every diagnostic carries `file:line:col`, a `[CODE]`, a caret under the exact
token, and a `hint` you can usually apply verbatim. **Fix every `error`. Address
`warning`s where reasonable. `info`s are design-quality nudges.** Apply the hint,
recompile, repeat until `COMPILE OK`.

**Clean ≠ correct.** A clean compile means the *code* is valid, not that you met
the brief — you can score `0/0/0` having dropped a bedroom. The `compile` recap
(`Program: N bed / M bath · … sq ft`) and `--metrics` are how you check the
program against what was asked. Always reconcile the recap with the brief — or
make it mechanical: add a `program <n> bed [<m> bath]` line and the validator
warns (`PROGRAM_MISMATCH`) when the rooms placed don't match the declared counts.

## Mental model (the source of most early bugs)

- **Units are feet.** Plain numbers — `10.5`, never `10'6"` or `ft`.
- **Origin `(0,0)` is the south-west corner.** `x` → east, `y` → north.
- A room at `x,y` `size W x L` spans `[x, x+W]` × `[y, y+L]`: **south** wall `y`,
  **north** `y+L`, **west** `x`, **east** `x+W`.
- An **exterior wall** lies on an envelope edge (`x=0`, `y=0`, `x=W`, `y=L`).
  Windows and entries only count on exterior walls — this drives egress and
  daylight.

## Placement: prefer relative anchors over absolute coordinates

Abutting rooms automatically **share a wall**, which is exactly what an interior
`door` needs. Hand-placed `at x,y` is where adjacency bugs come from (a room one
foot off, or touching only at a corner, shares no wall and the door fails).

```barn
room living:  living  at 0,0          size 24 x 30
room kitchen: kitchen east-of living  size 24 x 18   # shares living's east wall
```

**The anchor rule** — an anchor sets *both* coordinates:
- `east-of`/`west-of` → butt the east/west wall **and copy the ref's `y`**.
- `north-of`/`south-of` → stack on the north/south wall **and copy the ref's `x`**.

So **chaining in one direction is safe** (a row or a column). But **branching
re-anchors to the deeper room** and tends to collide. For a hallway spine, place
**every** served room directly off the spine (`north-of hall`, `east-of hall`,
…), one room deep — don't chain a second column off a room already off the spine.

`align near|far|center` and `offset <n>` slide a room along the shared wall
without dropping to absolute coords. Two anchors (one horizontal + one vertical)
pin a room into a corner ("pocket placement"). When a chain surprises you, run
`--show-coords` to see the resolved rectangles and which walls came out exterior.

> Relative placement fixes *adjacency*, not *bin-packing* — it won't tile the
> footprint for you. If you want the solver to size and pack rooms from an
> adjacency brief, use `barndsl layout` (see AUTHORING.md → Auto-layout).

## The rules the compiler enforces (write to these up front)

**Errors — must fix:**
- Rooms stay inside the envelope and don't overlap (same level).
- A `door`/`open` joins two *different* rooms that **share a wall** (corner-only
  contact is not shared). `open` is a door without a leaf — a cased opening /
  walk-through: same shared-wall + reachability rules, renders as a plain gap,
  defaults wide, exempt from the narrow-door warning.
- Bedrooms: area ≥ 70 sq ft, smallest side ≥ 7 ft, **and an egress** window (or
  its own entry) **on an exterior wall**.
- Every room is **reachable** from an `entry` through interior doors.
- An `entry` is on an **exterior** wall; openings fit (`offset + width ≤ wall`).

**Warnings — should address:** 8% glazing for habitable rooms; window on an
interior wall; hallway ≥ 3 ft; interior door ≥ 30 in; ≥ 1 egress door ≥ 32 in; a
bathroom exists; `OPEN_BATH` (a bath joined by `open` instead of a `door` — baths
need a door for privacy); `PRIVATE_PASSTHROUGH` (a room reachable only *through*
a bath/bedroom); `ENTRY_PRIVATE` (front door opening into a bath); `GARAGE_BEDROOM`
(a garage opening into a sleeping room — IRC R302.5.1); `PROGRAM_MISMATCH` (rooms
placed don't match a declared `program` line).

**Infos — design nudges:** `KITCHEN_FLOW`, `BED_PRIVACY`, `BATH_DISTANCE`,
`WET_GROUP` (cluster bath/kitchen/laundry on a shared plumbing wall), `NO_CLOSET`
(a bedroom with no closet reached *by a door* — an abutting one with no door
doesn't count), `MASTER_ENSUITE` (2+ full baths but no bedroom has a
private ensuite), `BATH_OVERSIZE` (an ensuite bigger than its bedroom),
`BED_SOUND` (two bedrooms share a wall — stack their closets
back-to-back on it to buffer sound), `CLOSET_SHAPE` (a closet big enough for a
walk-in but shaped as a long skinny strip), `ROOM_TIGHT` (kitchen < ~70, full
bath < ~48 / under 6 ft, half bath < ~30 / under 5 ft), `BATH_VENT`
(a windowless bath — confirm a fan), `HALL_TIGHT` (a hall under 4 ft — legal but
tight), `HALL_DEADEND` (a hall serving ≤ 1 room, **or** running past its last
door into a blank wall), `NO_BACK_DOOR` (only one exterior door — add a back/side
door), `DOOR_CENTERED` (a swing door floating mid-wall — back it to a corner with
`offset`), `WINDOW_PARTITION` (a window butting an interior partition — pull it off
the corner and space windows evenly), `ROOM_PROPORTION` (a habitable room more
elongated than ~3:1), `DOOR_SIZE` (a swing door that isn't a stock leaf width —
interior 30/32/36 in, exterior 36), `DOOR_SWING_CLASH` (two leaves sweep into the
same space — move/narrow one or make it pocket/sliding), `STAIR_WALL` (a stair
marooned mid-room), `ENVELOPE_MODULE` (an exterior dim off the 3 ft build module),
`GARAGE_NO_ENTRY`, `AREA_UNUSED`. **Warnings** also include `STAIR_BLOCKS_DOOR` (a
stair footprint on a doorway's clear floor). Heed when you can; they don't block.
Use `open` (not a wide `door`) for cased openings. For a hall, put the end rooms'
doors *at* the hall ends so the corridor terminates at doorways, not blank walls
(`HALL_DEADEND` is measured from the last doorway). `barndsl explain <CODE>`
prints the rationale for any code.

**Structure (optional).** Add a `frame [bay <ft>] [span <ft>] [post <in>]
[no-ridge]` line and the compiler auto-places the post-and-beam skeleton (bents
≤ `bay` ft o.c. along the long axis, each spanning the short axis, a ridge over
them, interior support posts where the span exceeds `span`). It draws over the
plan and adds a structural takeoff. Checks: `BAY_WIDE` (info — bay > ~12 ft o.c.),
`POST_OBSTRUCT` (info — an interior post stranded in open floor; align a partition
to it), `POST_IN_OPENING` (warning — a post inside a window/door; put openings in
the bays between posts). It's a layout aid, not an engineered design. `barndsl
build FILE --frame` frames a plan that has no `frame` line.

> Checks are approximate, loosely IRC-based — not a substitute for a licensed
> designer or the AHJ.

## Idioms that keep you out of trouble

- **Open core, private wing.** Open `living`/`kitchen`/`dining` joined directly;
  bedrooms and baths off a `hallway` spine.
- **Egress first.** Give every bedroom an exterior-wall window *early* — it's the
  most common hard error.
- **Habitable rooms on the perimeter** so they can take exterior windows; an
  interior habitable room trips `NAT_LIGHT` — open it to a neighbor instead.
- **Tile, then connect.** Place rooms so neighbors abut, then add a `door` per
  adjacency you actually walk through.
- **Bath gets a `door`, not an `open`.** Use `open` for kitchen↔living flow.

## A complete plan that compiles 0 / 0 / 0

Note the closets (clears `NO_CLOSET`), the bath on the kitchen's wet wall (clears
`WET_GROUP`), the `open` core (a cased opening, not a 96 in "door"), hall doors
backed to a corner with `offset` (clears `DOOR_CENTERED`), and a second exterior
door (clears `NO_BACK_DOOR`):

```barn
plan "Maple Two-Bed"
envelope 51 x 30
ceiling 10

room living:  living   at 0,0            size 20 x 30
room kitchen: kitchen  east-of living    size 22 x 14
room bath:    bathroom east-of kitchen   size 9 x 14
room hall:    hallway  north-of kitchen  size 31 x 4
room bed1:    bedroom  north-of hall     size 11 x 12
room c1:      closet   east-of bed1      size 4 x 12
room bed2:    bedroom  east-of c1        size 11 x 12
room c2:      closet   east-of bed2      size 5 x 12

open living - kitchen width 8
door living - hall width 3
door hall - bath width 2.67 offset 5.83
door hall - bed1 width 2.67 offset 0.5
door hall - bed2 width 2.67 offset 0.5
door bed1 - c1 width 2.5 offset 0.5
door bed2 - c2 width 2.5 offset 0.5
entry living south width 3 offset 8
entry living west width 3 offset 24

window living west width 14 offset 8
window kitchen south width 8 offset 6
window bath east width 4 offset 5
window bed1 north width 4 offset 3
window bed2 north width 4 offset 3
```

## Worked-example gallery — copy and adapt

`examples/gallery/` holds four **verified 0 / 0 / 0** plans (pinned by
`tests/test_gallery.py` so they can't rot). Few-shot from the closest one rather
than writing from scratch:

| File | Shape | Shows |
|------|-------|-------|
| `cottage.barn` | 1 bed / 1 bath | relative anchors, `open` core, hall-buffered bedroom |
| `hall_spine.barn` | 3 bed / 2 bath | hall spine capped by doorways, ensuite + wet-wall baths, back-to-back walk-in closets buffering the beds |
| `lshape.barn` | 2 bed / 2 bath, `wing` | L-footprint, interior seam walls, a primary suite with its own ensuite + walk-in |
| `two_story.barn` | 1 bed + loft | `level`, `loft`, a `stair` along the wall whose run fits the storey |

```bash
barndsl compile examples/gallery/hall_spine.barn   # read it, then adapt
```

## Python builder (same core, same rules)

```python
from barndsl import barndominium, RoomType as T, validate, emit_dsl

plan = (
    barndominium("Maple Two-Bed").envelope(48, 30).ceiling(10)
    .add_room("living", T.LIVING, x=0, y=0, width=24, length=30)
    .add_room("kitchen", T.KITCHEN, east_of="living", width=24, length=18)  # underscores: east_of=
    .connect("living", "kitchen", width=8)        # door; opening(...) for a walk-through (leaf=False)
    .entrance("living", "south", width=3, offset=10)
)
print(validate(plan))     # same diagnostics
print(emit_dsl(plan))     # → .barn source, round-trips losslessly
```

The reference room must be added *before* any anchor that points at it. `type`
and `wall` accept the enum or a string and are validated immediately.

## When you're stuck

- Placement not what you expected → `--show-coords`.
- "Shares no wall" / `DOOR_NOADJ` → the hint lists the room's *actual* neighbors;
  re-anchor off the right room, one room deep off a spine.
- Dropped part of the brief → check the `compile` program recap and `--metrics`.
- Anything grammar-level → re-print `DSL_REFERENCE`; deeper → `docs/AUTHORING.md`.
