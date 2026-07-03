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
To make that check **mechanical**, declare the intended counts with a `program`
statement (e.g. `program 3 bed 2 bath`): the validator then warns
(`PROGRAM_MISMATCH`) if the rooms you placed don't match — so a dropped bedroom
can't slip through a clean compile.

`require` extends the same pattern from counts to **spatial** intent — declare
the brief's constraints in the source and every compile re-checks them:

```barn
require adjacent kitchen dining        # the two rooms must share a wall
require separate master_bed garage     # the two rooms must NOT share a wall
require exterior living south          # living needs an exterior (south) wall
require area great_room >= 300         # nominal area at least 300 sq ft
```

Each unmet requirement is a `REQUIRE_UNMET` warning with a concrete fix (a
relative anchor to abut the rooms, which walls are interior, actual vs required
area); a requirement naming an unknown room id is a `REQUIRE_REF` error, like
any dangling reference. `adjacent` is purely geometric — a shared wall, exactly
what a `door` between the rooms needs; a door alone doesn't satisfy it.
`separate` is trivially satisfied across levels, and `area` uses the same
nominal figure `program area` does. Requirements never block a compile.

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
envelope <W> x <L>                 # primary footprint block (at the origin)
wing <W> x <L> at <x>,<y>          # optional; L/T/U footprints (repeatable)
ceiling <H>                        # >= 7; 9–12 is typical
floor <D>                          # optional; inter-floor assembly depth (ft). floor-to-floor = ceiling + D (default 1)
accessible                         # optional; opt in to accessibility / aging-in-place nudges
electrical                         # optional; opt in to the electrical / life-safety checklist reminder
note "free text"                   # optional; repeatable
program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>]  # optional intent, checked vs the rooms
require adjacent|separate <room_a> <room_b>   # optional spatial intent (repeatable); also:
require exterior <room> [<wall>]              #   `require area <room> >= <sqft>`

room <id>: <type> <placement> size <W> x <L> [level <n>]
door <id_a> - <id_b> [swing|cased|pocket|sliding] [width <w>] [offset <o>] [into <room>] [hinge near|far]
door <id> <wall> exterior [width <w>] [offset <o>] [no-egress]   # exterior door
door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]  # overhead/sectional garage door
open <id_a> - <id_b> [width <w>] [offset <o>]     # shorthand for `door <a> - <b> cased ...`
entry <id> <wall> [width <w>] [offset <o>] [no-egress]   # shorthand for `door <id> <wall> exterior ...`
window <id> <wall> [width <w>] [offset <o>] [sill <s>] [head <h>]   # sill/head: ft above the floor
porch <id> at <x>,<y> size <W> x <L> [covered|open]
stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]   # vertical circulation
frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]   # auto post-and-beam frame
```

- `<type>`: `living, kitchen, dining, bedroom, bathroom, half_bath, laundry,
  utility, hallway, closet, pantry, mudroom, office, loft, garage, shop, porch,
  other`.
- `<wall>`: `north | south | east | west`.
- `<offset>` is feet from the wall's **start corner** (its south or west end) to
  the near edge of the opening. The opening must fit: `offset + width <= wall
  length` (and `offset >= 0`).
- `door <id> <wall> overhead` is a **sectional garage door** on a garage/shop
  bay's exterior wall. Defaults to the residential 9 × 7 single; `width 16` is a
  double (stock widths 8/9/10/12/16 ft, heights 7/8 ft — off-standard nudges
  `DOOR_SIZE`, and wider than 10 ft notes `OVERHEAD_HEADER`). It is never an
  egress door (no-egress is implied) and doesn't count as a building entrance —
  the plan still needs a people-door `entry`. On a room that isn't a garage/shop
  it notes `OVERHEAD_ROOM`.
- `#` starts a comment. One statement per line. Braces `{ }` are ignored if you
  use them.

## L/T/U footprints with `wing`

The footprint is one rectangle unless you add `wing` blocks. Each `wing <W> x <L>
at <x>,<y>` adds a rectangle; the building is the **union** of the `envelope`
(the primary block at the origin) and every wing. An L is one wing, a U is two:

```barn
envelope 44 x 40            # main block, 0,0 → 44,40
wing 24 x 22 at 44,0        # a primary-suite wing projecting east
```

Everything tracks the real shape, not the bounding box:

- **Containment** — a room poking into the notch (covered by neither the envelope
  nor a wing) is an `OUT_OF_BOUNDS` error; a room sitting wholly in a wing, or
  straddling the seam between two abutting blocks, is fine.
- **Exterior walls** — a wall on the **seam** between two blocks is *interior*
  (no window/egress there); a wall facing a **notch** or the outside is
  *exterior*. Daylight and egress checks use this.
- **Area & outline** — `footprint_area`, the drawn building outline, and the
  dimension labels all follow the union.
- Wings must form **one connected footprint** (a shared wall, not just a corner
  touch) — otherwise `FOOTPRINT_SPLIT`.

See [`examples/lshape.barn`](../examples/lshape.barn). Note the **auto-layout
solver still fills a single rectangle** — `wing` is for plans you place yourself
(textual DSL or the Python builder's `.wing(w, l, x=…, y=…)`).

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

**Sliding along the shared wall.** By default a room aligns to the reference's
*near* corner (south for east/west anchors, west for north/south). Two optional
clauses move it along that wall without dropping to absolute coordinates:

- `align near|far|center` — `far` makes the two rooms' far edges flush;
  `center` centres the new room on the reference's wall.
- `offset <n>` — shift `n` feet further along the wall (positive = north for
  `east-of`/`west-of`, east for `north-of`/`south-of`; negatives are fine).

```barn
room living: living  at 0,0                       size 24 x 30
room bath:   bathroom east-of living align far    size 8 x 10   # flush to the north end
room office: office   east-of living offset 12    size 8 x 8    # starts 12 ft up the wall
room garage: garage  north-of living align far    size 12 x 22  # far = the east end here
```

Note this placement `offset` is **not** range-checked against the wall (unlike an
opening's `offset`, which must fit): it just shifts the room, and an `align`/
`offset`/size that pushes the room off the envelope (or past the shared wall,
breaking the adjacency a `door` needs) surfaces later as `OUT_OF_BOUNDS` or
`DOOR_NOADJ`. align/offset are resolved when the room is created, so `emit_dsl`
writes plain `at x,y` — they don't survive a round-trip as relations.

**Pocket placement (two anchors).** Combine **one** horizontal anchor
(`east-of`/`west-of`) and **one** vertical anchor (`north-of`/`south-of`) to pin
a room into a corner between two rooms — the horizontal anchor sets `x`, the
vertical sets `y`:

```barn
room living: living  at 0,0          size 20 x 20
room hall:   hallway at 0,20          size 40 x 4
room closet: closet  east-of living north-of hall size 8 x 16   # x=20, y=24
```

With two anchors the position is fully determined, so `align`/`offset` don't
apply (and two anchors on the *same* axis are an error). Relative placement fixes
*adjacency*, not *bin-packing* — it still won't tile the footprint for you. Use
`barndsl compile FILE --show-coords` to print every room's resolved rectangle and
which walls ended up exterior — the fastest way to see what a chain of anchors
actually produced.

## Levels and lofts

`level <n>` (default `0` = ground) puts a room on an upper floor.

```barn
room living: living at 0,0 size 30 x 30
room loft:   loft   at 0,0 size 30 x 12 level 1   # sits ABOVE part of living
door living - loft width 3                          # cross-level door = a stair
```

Rooms on **different levels don't overlap** (the loft is above, not beside, the
room below) and don't share walls.

Connect floors with a **`stair`**:

```barn
room living: living at 0,0 size 40 x 30
room loft:   loft   at 0,0 size 28 x 20 level 1     # sits above the living room
stair s at 24,2 size 4 x 12 from 0 to 1             # run on level 0, landing on level 1
```

A stair links the rooms its footprint overlaps on each level, so the upper floor
becomes reachable from an `entry` below. Place it over a room on **both** levels
(otherwise `STAIR_FLOAT` warns it connects nothing); a stair whose `from`/`to`
levels are equal or whose footprint leaves the envelope is an error. An upper room
with no stair (or cross-level `door`) reaching it is flagged `NO_ACCESS` (a
`warning` for a loft, since it might be an open mezzanine). `barndsl build` draws
each level as its own labelled floor plan, with the stair marked ↑/↓.

`<n>` must be a **whole number ≥ 0** (`0` = ground, `1` = the floor above);
non-integer or negative levels are rejected (`BAD_LEVEL`). A loft's exterior
walls are still computed from the envelope edges in plan view, so a loft **inset**
from the envelope (like the `30 x 12` example above, whose north wall is interior)
can't take a window that counts for daylight/egress — put the loft against an
envelope edge if it needs a real window.

## Structure: auto-placing the post-and-beam frame

A barndominium is a post-and-beam metal building. Add one `frame` line and the
compiler places the structural skeleton over the footprint — you don't draw a
single post:

```barn
frame bay 12 span 40 post 6      # all options optional; these are the defaults
```

What it derives, per footprint block (so an L/T/U `wing` plan frames each block):

- The **ridge** runs along the block's **long axis**; **bents** (frames) span the
  **short axis** and are spaced ≤ `bay` ft on centre along the long axis. So a
  60×40 plan with `bay 12` gets 6 bents (every 12 ft) each spanning 40 ft.
- **Posts** land at every bent on the two eave walls, up the gable end walls at
  the same `bay` spacing, and at the four corners (coincident posts dedupe).
- If the short-axis span exceeds `span` ft, an **interior support post** line is
  added to split the beam. Keep `span` at or above your building width to stay a
  clear span (the barndo norm); lower it to force interior posts.
- `no-ridge` omits the ridge member.

`barndsl build` draws the bents (solid), ridge (dashed), and posts (solid
squares) over the plan, and the summary panel lists the bent/post counts and beam
linear feet. Checks: `BAY_WIDE` (info — a bay wider than ~12 ft o.c.),
`POST_OBSTRUCT` (info — an interior post stranded in a room's open floor; align a
partition, closet, or island to it), and `POST_IN_OPENING` (**warning** — a post
lands inside a window or exterior door; the post grid is fixed, so put openings in
the bays *between* posts — a post at the opening's jamb is fine, only one inside it
warns). To frame a plan that has no `frame` line, `barndsl build plan.barn
--frame`.

> The frame is a **layout aid, not an engineered design.** It schedules a sane
> post-and-beam grid for drawings and a rough takeoff; member sizing, connections,
> footings and lateral bracing belong to a licensed structural engineer.

## The rules the compiler enforces

**Errors (must fix):**
- Rooms stay inside the envelope and don't overlap (same level).
- A `door` connects two *different* rooms that **share a wall** (or, across
  levels, stack). Corner-only contact is **not** a shared wall. `open` is the
  same connection without a door leaf — a cased opening / walk-through — and is
  subject to the same shared-wall rule and reachability, but renders as a plain
  gap, defaults wide, and is exempt from the narrow-door warning.
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
- `OPEN_BATH` — a bathroom connected by an `open` walk-through instead of a
  `door`. A bathroom needs a door for privacy; use `door` for it.
- `PRIVATE_PASSTHROUGH` — a room reachable only by walking **through a bathroom**
  (or, apart from its own ensuite/closet, through a bedroom). Reachability alone
  (`NO_ACCESS`) is satisfied, but the *route* goes through a private room — a real
  circulation defect, so it's a warning, not just a note. Route the room off a
  hallway or living space instead.
- `ENTRY_PRIVATE` — an exterior `entry` opening directly into a **bathroom**
  (warning; into a **bedroom** it's an info — it might be a patio door).
- `GARAGE_BEDROOM` — a `garage` opening directly into a **bedroom**. A garage
  must not open into a sleeping room (IRC R302.5.1) — buffer it with a mudroom or
  hall.
- `STAIR_BLOCKS_DOOR` — a `stair` footprint intrudes on the clear floor in front
  of a door, so you'd step off the flight straight into a swinging door. Place the
  stair along a wall, clear of door approaches.
- `STAIR_GEOMETRY` / `STAIR_OOB` / `STAIR_LEVELS` (errors), `STAIR_RUN` /
  `STAIR_FLOAT` — a stair with bad geometry, too short a run, or landing in no room.
- `PROGRAM_MISMATCH` — the rooms placed don't match a declared `program` (e.g.
  `program 3 bed` but only two bedrooms exist). The plan is still valid/buildable
  — it's a contract check, not a code error — so it's a warning.
- `REQUIRE_UNMET` — the compiled geometry doesn't satisfy a declared `require`
  (a required adjacency/separation/exterior wall/minimum area). Same contract
  logic as `PROGRAM_MISMATCH`, so a warning; a `require` naming an unknown room
  id is a `REQUIRE_REF` **error**, like any dangling reference.

**Info (design quality — heed when you can):**
- `KITCHEN_FLOW` — open the kitchen to dining/living.
- `BED_PRIVACY` — don't open a bedroom straight onto a public room; buffer with a
  hallway.
- `BATH_DISTANCE` — keep a bath within a door or two of the bedrooms.
- `WET_GROUP` — cluster wet rooms (bath/kitchen/laundry/utility) onto a shared
  plumbing wall; 3+ that share no walls means longer, costlier runs.
- `NO_CLOSET` — a bedroom with no closet reached *by a door* from it.
- `ROOM_PROPORTION` — a habitable room more elongated than ~3:1 is hard to
  furnish.
- `ROOM_TIGHT` — a room below the floor its use needs: kitchen ~70, full bath ~48
  (≥ 6 ft short side), half bath ~30 (≥ 5 ft) sq ft.
- `HALL_TIGHT` — a hallway at the 3 ft code minimum; 4 ft is comfortable.
- `HALL_DEADEND` — a hall that serves ≤ 1 room, **or** runs well past its last
  **doorway** into a blank wall (a dead-end stub). Put the end room's door *at*
  the hall end (extend that room to cap the hall), or trim the hall back.
- `DOOR_SWING_CLASH` — two door leaves sweep into the same space and foul each
  other; move one along its wall, swing it the other way, or make it pocket/sliding.
- `ENVELOPE_MODULE` — an exterior (envelope/wing) dimension isn't a multiple of
  the 3 ft build module; rounding to it cuts sheet goods and framing with less waste.
- `NO_BACK_DOOR` — a home with a single exterior door; add a back/side door (off
  the kitchen, mudroom or laundry) for daily flow and a second way out.
- `DOOR_CENTERED` — a swing door floating mid-wall; back it to a corner (`offset`)
  so one side keeps an unbroken wall to furnish.
- `DOOR_SIZE` — a swing door off the stock leaf sizes; use `open` for a wide
  cased passage instead of a 96 in "door".
- `WINDOW_PARTITION` — a window butting an interior partition where it meets the
  exterior wall; pull it toward the centre or a building corner, and space windows
  evenly.
- `BED_SOUND` — two bedrooms share a wall; stack their closets on it to buffer
  sound. `CLOSET_SHAPE` — a walk-in-sized closet shaped as a skinny strip.
- `MASTER_ENSUITE` — 2+ full baths but none is a private ensuite.
- `BATH_OVERSIZE` — an ensuite larger than the bedroom it serves.
- `STAIR_WALL` — a stair marooned mid-room rather than run along a wall.
- `GARAGE_NO_ENTRY` — a `garage` that abuts the house but has no interior
  people-door into it (you'd have to go outside to get in).
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

This compiles with **0 errors, 0 warnings, 0 info** — note every bedroom gets a
closet and the bath sits on the kitchen's wet wall, which is what clears the
`NO_CLOSET` and `WET_GROUP` nudges:

```barn
plan "Maple Two-Bed"
envelope 51 x 30                              # exterior dims on the 3 ft module
ceiling 10
note "2 bed / 1 bath, open living-kitchen, bedrooms + closets off a hall."

room living:  living   at 0,0            size 20 x 30
room kitchen: kitchen  east-of living    size 22 x 14
room bath:    bathroom east-of kitchen   size 9 x 14
room hall:    hallway  north-of kitchen  size 31 x 4
room bed1:    bedroom  north-of hall     size 11 x 12
room c1:      closet   east-of bed1      size 4 x 12
room bed2:    bedroom  east-of c1        size 11 x 12
room c2:      closet   east-of bed2      size 5 x 12

open living - kitchen width 8                # cased opening, not a 96 in door
door living - hall width 3
door hall - bath width 2.67 offset 5.83      # at the hall's far end (caps the run)
door hall - bed1 width 2.67 offset 0.5       # backed to a corner, not centred
door hall - bed2 width 2.67 offset 0.5
door bed1 - c1 width 2.5 offset 0.5
door bed2 - c2 width 2.5 offset 0.5

entry living south width 3 offset 8
entry living west width 3 offset 24          # a back/side door — front + back

window living west width 14 offset 8
window kitchen south width 8 offset 6
window bath east width 4 offset 5
window bed1 north width 4 offset 3
window bed2 north width 4 offset 3
```

## Auto-layout: hand the solver a brief instead of coordinates

If you'd rather describe the *program* than place rooms, write an **adjacency
brief** and let `barndsl layout` compute the geometry. You list rooms and sizes
and say which rooms should touch; the solver packs them, abutting each requested
pair so they share a wall, then adds a `door` per adjacency, a front `entry`,
and egress/daylight `window`s.

```text
# birch_run.brief
plan "Birch Run"
ceiling 9
room living:  living   20 x 18
room kitchen: kitchen  16 x 18
room hall:    hallway  40 x 4
room bed1:    bedroom  18 x 14
room bed2:    bedroom  14 x 14
room bath:    bathroom 8 x 14
adjacent living kitchen        # open core
adjacent living hall           # core opens to the hall spine
adjacent hall bed1 bed2 bath   # private rooms off the hall
entry living
```

```bash
barndsl layout birch_run.brief --emit --out plan.svg
```

Brief grammar (one statement per line, `#` comments):

| statement | meaning |
|-----------|---------|
| `plan "Name"` | plan name |
| `envelope <W> x <L>` | optional; omit to size the envelope to the packed bounding box |
| `ceiling <H>` | ceiling height |
| `floor <D>` | inter-floor assembly depth (ft); floor-to-floor = ceiling + D |
| `accessible` | opt in to accessibility / aging-in-place nudges (ANSI A117.1) |
| `electrical` | opt in to the electrical / life-safety checklist reminder (receptacles, lighting, stair light, door landings) |
| `orientation <deg>` | true-north azimuth plan-north points; drives the solar-glazing nudges + compass |
| `lot <W> x <L> [at <x>,<y>]` | the parcel (plan coords); omit `at` to auto-centre the footprint |
| `setback <side> <ft> …` | zoning setbacks (south/north/east/west; front/back/left/right aliases) → SETBACK |
| `street <wall>` | the wall facing the street/approach → APPROACH_ENTRY / APPROACH_GARAGE nudges |
| `overhang <ft>` | roof eave/rake projection past the walls (shades south glass; widens the roof + takeoff) |
| `climate <zone>` | IECC climate zone 1–8 → ENERGY_ENVELOPE R-value guidance + the WINDOW_HEAVY (WWR) ceiling |
| `note "…"` | free text |
| `room <id>: <type> <W> x <L> [level <n>]` | a room to place (no coordinates) |
| `adjacent <a> <b> [<c> …]` | connect `<a>` to **each** of the rest — a hub. `adjacent hall bed1 bed2 bath` is the "rooms off a spine" idiom |
| `entry <room>` | which room gets the front door (default: the first public room on an exterior wall) |
| `no-openings` | don't auto-add the entry/windows (geometry + doors only) |

### Two engines: `fill` (default) and `greedy`

`barndsl layout` has two solvers, selected with `--engine`:

- **`fill` (default)** — a *space-filling* engine. It **dimensions every room to
  tile the rectangle with no gaps**, so habitable rooms land on the perimeter (for
  daylight/egress) and `AREA_UNUSED` ≈ 0. Because it sizes rooms to fit, briefs
  give a **target area** rather than fixed dimensions — `room living: living area
  360`. Fixed `W x L` still works (treated as that area). Internally it tries three
  topologies — *bands* (public core · hall · private row), a recursive *slice*
  (which can give a room three neighbours), and a rectangular *dual* (which tiles
  so *every* requested adjacency is a shared wall, handling non-sliceable graphs
  like a **pinwheel** — a centre room touching four others) — and **keeps whichever
  scores best** (fewest errors, then unmet adjacencies, then waste); it tells you
  in a note when it picks slice or dual. The dual is tried only when bands and
  slice leave an adjacency unmet (it is the most expensive), and is built by a
  verified structural-grid search that falls back gracefully when no tiling fits.
  Force one with `--engine bands|slice|dual` for debugging. See
  `docs/design/AUTO_LAYOUT_2.md` for how and why.
- **`greedy`** — the original abutment placer (below). Honors fixed sizes exactly
  but packs a blob with holes; kept for when you want rooms at their exact given
  dimensions.

```bash
barndsl layout brief.txt                    # fill (default)
barndsl layout brief.txt --engine greedy    # v1 abutment
```

A `fill` brief uses `area` (or fixed `W x L`); everything else — `adjacent`,
`entry`, `envelope`, `no-openings` — is identical:

```text
room living: living area 360
room hall:   hallway area 140 min 4     # `min` sets the smallest side
room bed1:   bedroom area 168
```

**What `greedy` is and isn't.** It's a deterministic greedy placer built on
the same relative/pocket placement you'd write by hand — so it fixes *adjacency*
(shared walls, working doors) but it does **not** bin-pack a perfect rectangle.
Expect an `AREA_UNUSED` info and the odd elongated footprint; treat its output
as a **valid, connected starting layout** and refine it in the compile-fix loop.
It reports what it couldn't honour:

- `LayoutResult.unsatisfied` — adjacency pairs that didn't end up sharing a wall.
- `LayoutResult.notes` — e.g. a bedroom with no exterior wall (it can't get an
  egress window there) or a room short on daylight. These map straight onto the
  `error`/`warning`s you'd then fix.

From Python the brief is plain dataclasses:

```python
from barndsl import RoomSpec, LayoutBrief, solve_layout, emit_dsl

out = solve_layout(LayoutBrief(
    name="Birch Run",
    rooms=[RoomSpec("living", "living", 20, 18), RoomSpec("hall", "hallway", 40, 4)],
    adjacencies=[("living", "hall")],
))
print(out.summary())
print(emit_dsl(out.plan))   # absolute DSL, ready to compile/render/refine
```

`add_openings=False` (or the `no-openings` directive) gives you the bare placed
shell with doors but no entry/windows — handy when you want to add those by hand.

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
  west_of=, north_of=, south_of=, align="near", offset=0)` — relative anchors
  use **underscores** (`east_of=`), and the reference must be added *before* this
  call. `align=` (`"near"`/`"far"`/`"center"`, case-insensitive) and `offset=`
  slide the room along the shared wall, exactly like the DSL clauses.
- `connect(a, b, width=)` is an interior `door` (`leaf=False` for a walk-through);
  `opening(a, b, width=)` is the shorthand for that cased opening; `entrance(room,
  wall, …)` is an `entry`; `add_window(room, wall, …)`; `add_porch(id, …)`.
- `program(beds, baths=None)` declares the intended counts (the `program`
  statement); omit `baths` to check only bedrooms.
- `require(kind, a, b=None, *, wall=None, min_area=None)` declares a spatial
  requirement (the `require` statement): `require("adjacent", "kitchen",
  "dining")`, `require("separate", "master", "garage")`, `require("exterior",
  "living", wall="south")`, `require("area", "great_room", min_area=300)`.
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
