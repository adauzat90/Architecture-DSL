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
barndsl compile plan.barn -q               # Makefile mode: silent on success
```

**`-q` / `--quiet`** (on `compile`, `fmt`, `score`, `cost`) is the Unix-clean
mode for a Makefile or CI step: it prints **nothing on success**, sends the
diagnostic report to **stderr** on failure, and leaves the **exit code
unchanged** (so `barndsl compile plan.barn -q && …` gates cleanly). It suppresses
the human-readable stdout, not the exit signal.

**A clean compile does not mean you built the right plan.** It checks the code is
valid, not that you met the brief — you can compile `0/0/0` while having dropped a
bedroom. `compile` prints a one-line `Program: N bed / M bath · … sq ft` recap so
you can check the program against the brief; `--metrics` gives the full takeoff.
To make that check **mechanical**, declare the intended counts with a `program`
statement (e.g. `program 3 bed 2 bath`): the validator then warns
(`PROGRAM_MISMATCH`) if the rooms you placed don't match — so a dropped bedroom
can't slip through a clean compile. `program ... area <sqft>` remains a minimum
contract for compatibility, but a large overshoot emits `PROGRAM_AREA_OVERRUN` so
an area from the brief is not silently treated as just decorative text.

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

- **Units are feet.** The default is a plain decimal number (`10.5`, no `ft`
  suffix). Any length field also accepts a **feet-and-inches** literal written as
  one token: `10-6`, `10'6`, `10'`, `10′6″`, `10′` or `6″` all mean the same as
  `10.5` / `10` / `0.5`. The dash form only counts when there's no space around
  it, so `12 - 6` (three tokens), `door a - b` and a bare negative `-6` keep their
  meanings; `-12-6` (negative feet + inches) is rejected, and ASCII `10'6"` (with
  the inch `"`) isn't accepted because `"` starts a string. `emit` normalises
  everything back to decimal feet.
- **Origin `(0,0)` is the south-west (bottom-left) corner.** `x` increases east,
  `y` increases north.
- A room placed at `x,y` with `size W x L` occupies `[x, x+W]` east-west and
  `[y, y+L]` south-north. So its **south** wall is at `y`, **north** at `y+L`,
  **west** at `x`, **east** at `x+W`.
- An **exterior wall** is one that lies on the envelope edge (`x=0`, `y=0`,
  `x=envelope_width`, or `y=envelope_length`). Windows and entries only "count"
  on exterior walls — see the rules below.
- **Rooms tile on wall centrelines**, so those `x,y,W,L` coordinates are the
  *nominal* room lines, not built wall faces. The floor plan draws walls as real
  bodies (poché bands at nominal thickness, straddling each centreline) and the
  DXF export matches them exactly. A room's *clear* (built) interior is its
  nominal rectangle minus half of each bounding wall; that reduction drives the
  clear-dimension checks and the schedule's clear sizes.
- **Dimension convention (`dim_mode`, two options).** By default every drawn
  dimension — the overall strings and the per-side chains — measures to the
  **nominal room lines**: the centreline of an interior partition and the
  nominal envelope face. That is the model's coordinate truth (chosen in Phases
  10/12), and it is the default so existing drawings are byte-for-byte unchanged.
  Set `dim_mode = "faces"` (`--dims faces` on `build` / `dxf` / `packet`, or the
  playground's *Dims* toggle) for the professional **face-of-stud** convention:
  - the overall dims run **outside face to outside face** (nominal + one exterior
    wall thickness per axis — the ends move out by half an exterior wall to the
    drawn poché edge);
  - each interior room break becomes **two ticks**, the two faces of the wall
    crossing there, so the chain strings **clear width / wall thickness / clear
    width** — e.g. a nominal 12′0″ room between two 4½″ partitions reads
    **11′-7½″ clear** flanked by two **4½″** thickness segments, and the segments
    still sum to the overall. The face offsets come straight from the shared
    `wallbodies` band geometry, so a plumbing (2×6) wall reads its real 6½″ and a
    tick lands pixel-exact on the drawn band edge;
  - **opening jambs are unchanged** — already face-of-opening.

  The thin wall-thickness segments are **ticked but labelled only when the label
  fits** (the same tiny-segment rule the chain uses everywhere — the tick is
  never dropped, only its text when the segment is too narrow); at a typical
  plan scale a 4½″ segment shows its two ticks without a crowded number. Both
  the SVG and the DXF honour `dim_mode`, and the permit packet states the active
  convention on its floor-plan scale note ("Dimensions to face of stud" vs
  "Dimensions to nominal room lines (partition centrelines)"). Schedules always
  report clear dimensions regardless of mode.

## Statements

```barn
plan "Name"
envelope <W> x <L>                 # primary footprint block (at the origin)
wing <W> x <L> at <x>,<y>          # optional; L/T/U footprints (repeatable)
ceiling <H>                        # >= 7; 9–12 is typical
floor <D>                          # optional; inter-floor assembly depth (ft). floor-to-floor = ceiling + D (default 1)
accessible                         # optional; opt in to accessibility / aging-in-place nudges
electrical                         # optional; opt in to the electrical / life-safety checklist reminder
note "free text" [at <x>,<y> [level <n>]]  # optional, repeatable. bare = free text;
                                   #   `at <x>,<y>` = a leader callout drawn on the plan at
                                   #   that world point (level default 0). Outside the
                                   #   footprint → a gentle NOTE_OUTSIDE info, not an error.
program <n> bed [<m> bath] [<k> <type> ...] [area <sqft>] [storage <sqft>]  # optional intent, checked vs the rooms
require adjacent|separate <room_a> <room_b>   # optional spatial intent (repeatable); also:
require exterior <room> [<wall>]              #   `require area <room> >= <sqft>`
site <W> x <L>                     # optional; the lot's east-west × north-south dimensions (ft)
setback [front <n>] [side <n>] [rear <n>]     # optional; required yard clearances (needs a `site`)
building at <x>,<y>                # optional; place the building's SW corner on the lot (default centred)
drive at <x>,<y> size <W> x <L> [gravel|concrete|asphalt]  # optional; a driveway (lot ft; needs a `site`)
walk from <room> to drive [width <ft>]        # optional; a path from a room's exterior door to the drive
well at <x>,<y>                    # optional; a water well (lot ft; needs a `site`)
septic at <x>,<y> [field <W> x <L>]           # optional; a septic tank + optional drain field (needs a `site`)
service electric|water|gas from N|S|E|W       # optional; a utility service drop from a lot side
grade <ft>                         # optional; finish-floor height above finished grade (flat site)

room <id>: <type> <placement> size <W> x <L> [level <n>]
wall <id_a> - <id_b> plumbing|bearing|rated   # optional; attribute(s) of the shared wall (rooms must abut)
suite <id>: <room> ...             # optional; group rooms that read as one unit
zone <id>: <member> ...            # optional; group rooms/suites into a band (members: room OR suite ids)
door <id_a> - <id_b> [swing|cased|pocket|sliding|double|french] [width <w>] [offset <o>] [into <room>] [hinge near|far]
door <id> <wall> exterior [double|french] [width <w>] [offset <o>] [no-egress]   # exterior door
door <id> <wall> overhead [width <w>] [height <h>] [offset <o>]  # overhead/sectional garage door
open <id_a> - <id_b> [width <w>] [offset <o>]     # shorthand for `door <a> - <b> cased ...`
entry <id> <wall> [double|french] [width <w>] [offset <o>] [no-egress]   # shorthand for `door <id> <wall> exterior ...`
window <id> <wall> [casement|slider|fixed|double-hung] [width <w>] [offset <o>] [sill <s>] [head <h>] [fixed] [tempered]   # sill/head: ft above the floor
porch <id> at <x>,<y> size <W> x <L> [covered|open]
stair <id> at <x>,<y> size <W> x <L> [from <lo>] [to <hi>]   # vertical circulation
fixture <kind> in <room> [at <x>,<y>] [wall N|S|E|W] [rotate <deg>] [width <w>]   # furnishing
fixture counter in <room> along N|S|E|W [from <a> to <b>] [depth <d>]   # a countertop run
outlet in <room> wall N|S|E|W offset <ft> [gfci]   # optional; a receptacle on a room wall
switch in <room> wall N|S|E|W offset <ft>          # optional; a wall switch
light in <room> at <x>,<y> [kind ceiling|pendant|fan|recessed]   # optional; a ceiling luminaire (room-local x,y)
alarm smoke|co|smoke_co in <room> [at <x>,<y>]   # optional; a smoke/CO alarm (room-level, IRC R314/R315)
frame [bay <ft>] [span <ft>] [post <in>] [no-ridge]   # auto post-and-beam frame
```

- `<type>`: `living, great_room, kitchen, dining, bedroom, bathroom, half_bath,
  laundry, utility, mechanical, hallway, foyer, closet, pantry, storage, mudroom,
  office, flex, rec_room, loft, safe_room, garage, shop, porch, other`.
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
- `wall <a> - <b> plumbing|bearing|rated` declares what the **shared wall**
  between two abutting rooms *is* (one or more attributes; the rooms must really
  share a wall — `WALL_NOADJ` error otherwise, `WALL_REF` for an unknown id):
  - `plumbing` — the 2x6 wet wall the fixtures back onto. A wet room (bath /
    kitchen / laundry / utility) backing onto a declared plumbing wall satisfies
    the `WET_GROUP` grouping nudge; the flanking rooms' **clear dimensions**
    lose half a 2x6 on that side; and the Revit exchange hints the thicker wall
    type. Declared between two dry rooms it notes `WALL_UNUSED`.
  - `bearing` — an interior bearing wall. When it runs along the building's
    long axis, the auto `frame` honours it as an **interior post line** (a post
    at every bent crossing it). One running across the span can't split it —
    `WALL_BEARING_AXIS` info instead of a silent no-op.
  - `rated` — the garage/dwelling fire separation, detailed and declared. It
    silences the `GARAGE_SEPARATION` reminder for that pair (verified, not
    reminded); a garage ceiling under habitable space still reminds.
- `suite <id>: <room> ...` and `zone <id>: <member> ...` declare the plan's
  **structure** — which rooms read as one unit, and which band they sit in:

  ```barn
  suite primary: master_bed master_bath master_wic
  zone private: primary bed_2 bed_3 hall_beds
  ```

  A `suite`'s members are room ids; a `zone`'s members are room ids **or suite
  ids**, so a zone can group whole suites. Both are declared *intent* like
  `program`/`require` — not geometry — and the checks use them:
  - An unknown member is a `SUITE_REF` / `ZONE_REF` **error** (a typo would
    otherwise group nothing). A room in two suites is a `SUITE_OVERLAP`
    **warning**; a room in two zones (directly, or via a suite one zone lists)
    is a `ZONE_OVERLAP` warning — groups are meant to be mutually exclusive.
  - A **declared suite sharpens the design checks** that otherwise *infer*
    membership — but a declaration never overrides geometry, it only relaxes
    the inference where the plan backs it up: a bedroom satisfies
    `MASTER_ENSUITE` when a full bath in its suite is **reachable from it by
    doors that stay inside the suite** (bed → bath, or bed → wic → bath — a
    bath across the plan doesn't become an ensuite by declaration); two
    bedrooms whose suite contains **exactly that pair** (a bunk room) don't
    fire `BED_SOUND` (one giant all-bedroom "suite" doesn't mute the check);
    a public room inside a bedroom's own suite doesn't fire `BED_PRIVACY`;
    and a patio-door `entry` into a bedroom that is the **only bedroom of its
    suite** (the primary suite) doesn't fire `ENTRY_PRIVATE`. With **nothing
    declared the behaviour is unchanged** — the sharpening only ever
    suppresses a nudge the declaration *and the geometry* explain. Naming a
    suite like an existing room is a `SUITE_SHADOW` warning (a zone member
    with that name resolves to the room, not the suite).
  - A `zone` enables `ZONE_CROSS` (**info**): a clearly public room
    (living/kitchen/dining) whose only zone otherwise holds just private rooms
    (bed/bath) — or the reverse — is a public room stranded in the private band.
    It's deliberately conservative: it fires only when the room sits in exactly
    one zone that's unambiguously the opposite band, so a mixed open-concept
    zone (or a plan with no zones) never triggers it.
- A **window kind** (right after the wall, default `casement`) sets the honest
  escape-opening math: a casement clears ~its full glazed size (the historical
  default), a `slider` ~half its glazed width, a `double-hung` ~half its glazed
  height, and `fixed` glass **never** counts for bedroom egress
  (`BEDROOM_EGRESS` / `EGRESS_SIZE`) though it still daylights (`NAT_LIGHT`).
- A `double`/`french` door (interior or exterior) is a **pair of half-width
  leaves** (default 5 ft — the stock 60 in pair; stock pairs 48/60/64/72 in for
  `DOOR_SIZE`). Egress clear width counts **one leaf** (IRC R311.2): a 5 ft pair
  is two 30 in leaves and does *not* satisfy the 32 in egress-door minimum — use
  `width 6` where the pair is the required exit.
- `site <W> x <L>` declares the **lot** (feet, east-west × north-south) and
  `setback [front <n>] [side <n>] [rear <n>]` the required yard clearances (any
  subset). The **buildable rectangle** is the lot minus its setbacks: `front` and
  `rear` consume the plan's north-south depth (front along the plan's south/entry
  edge, rear along its north), and a single `side` clears **both** the east and
  west edges. If the building footprint — the envelope, any wings, **and any
  porch** — doesn't fit inside the buildable rectangle, that's a `SETBACK` error
  (a county/legal violation, so it's an error). barndsl has no lot-position
  statement, so the check is by **dimensions only**: the footprint's bounding box
  must fit the buildable width and length; *where* the building sits on the lot
  isn't modelled. A `site` on its own imposes no check; a `setback` with no
  `site` to measure against is a `SETBACK_NO_SITE` error. This is a sanity guard,
  not a substitute for a surveyed site plan.
- `building at <x>,<y>` pins the building's plan origin (its south-west envelope
  corner, world `0,0`) at that point in **lot feet** from the lot's south-west
  corner. Declare it and the `SETBACK` check becomes position-aware: it measures
  the real clear yard on each edge and, on a violation, **names the side and the
  encroachment in ft-in** (rather than the dimensions-only test above). Omit it
  and the site plan centres the footprint on the lot. It also drives the **Site
  Plan** sheet in the permit packet and the site drawing on the playground's
  Elevations tab.

### Site plan: driveway, utilities, and grade

With a `site` declared you can lay out the rest of the lot. Every coordinate here
is in **lot feet** (the same `building at` frame — from the lot's SW corner), so
the features sit in the same drawing as the lot and building:

- `drive at <x>,<y> size <W> x <L> [gravel|concrete|asphalt]` — a driveway
  (surface defaults to `gravel`; concrete/asphalt cost more). `walk from <room> to
  drive [width <ft>]` runs a walkway (default 4 ft) from that room's exterior door
  to the nearest drive edge. A drive with **no** walk or drive edge reaching a
  door draws a `DRIVE_DOOR` info ("guests arrive and have no path to a door"). Two
  declared drives that **overlap** draw a `SITE_OVERLAP` warning (the cost takeoff
  sums each drive's area, so an overlap double-counts the shared paving — merge or
  separate them). Only drive↔drive is checked; a `walk` is auto-routed to meet a
  drive, so a walk↔drive overlap is by design.
- `well at <x>,<y>` places a water well; `septic at <x>,<y> [field <W> x <L>]` a
  septic tank (drawn ~5×8 ft) with an optional drain field just north of it. A
  well closer than **100 ft** to the septic tank/field warns (`WELL_SEPTIC_CLEAR`
  — the common health-department separation; edit `WELL_SEPTIC_MIN_SEPARATION` in
  `constants.py` for a different local figure). A septic component inside a
  required setback band draws a `SEPTIC_SETBACK` info.
- `service electric|water|gas from N|S|E|W` draws a labelled service drop entering
  from a lot side.
- `grade <ft>` is the finish-floor height above finished grade (a single flat-site
  value — v1 models no slope; `grade 2-8` is fine). When it exceeds **30 in**,
  every `porch` is a walking surface that needs a **36 in guard** (IRC R312.1),
  drawn as a `PORCH_GUARD` warning per porch — note the guard on the drawings.
  `grade` is the exception to "site features need a `site`": it describes the
  building, not the lot, so it stands alone.

The site render adds recognisable symbols (drive hatch, circled-W well, septic
tank + drain-field lattice, service arrows), a **legend**, and dimensions of the
building's *actual* distance to each lot line; the permit packet's Site Plan sheet
adds a **yard-clearance table** (required setback vs actual, pass/fail), a
**feature-clearance table** (the actual well↔septic separation vs the 100 ft
rule, e.g. `Well ↔ septic: 116′-7⅜″ ≥ 100′ ✓`, plus each drive's distance to the
nearest lot line — printed whenever the features exist), and site notes. See
`examples/gallery/homestead.barn` for the full vocabulary, clean.
- The **electrical layer** is opt-in and drawn per statement:
  `outlet in <room> wall N|S|E|W offset <ft> [gfci]` places a receptacle on a
  wall (offset from its south/west start corner; `gfci` = ground-fault),
  `switch in <room> wall N|S|E|W offset <ft>` a wall switch, and
  `light in <room> at <x>,<y> [kind ...]` a ceiling luminaire at **room-local**
  x,y. Once a habitable room declares any `outlet`, the compiler checks receptacle
  spacing (no wall point more than 6 ft from one — `OUTLET_SPACING`, IRC E3901.2);
  a wet-room (kitchen/bath/laundry/utility) outlet without `gfci` warns
  (`OUTLET_GFCI`, E3902); and a habitable room with power but no `light` gets a
  `ROOM_NO_LIGHT` info (E3903). Rooms that draw nothing are never nagged — and a
  plan with the `electrical` directive but no drawn devices keeps the old
  one-shot `ELECTRICAL_PLAN` checklist. Toggle the layer with **⚡** in the
  playground's plan toolbar; the permit packet gains an **Electrical Plan** sheet.
- **Smoke/CO alarms** ride the same electrical layer: `alarm smoke in <room>`,
  `alarm co in <room>` or `alarm smoke_co in <room>` (the combination unit — the
  only combo spelling) place a room-level ceiling device (an "SD"/"CO"/"SD/CO"
  circle; optional `at <x>,<y>` moves the symbol, default room centre). A plan
  with bedrooms but no `alarm` gets a teaching reminder (`ALARM_CO`, info). Once
  any `alarm` is declared, the real placement checks run: a smoke alarm in each
  bedroom (`ALARM_BEDROOM`, R314.3), one in a room adjacent to each bedroom
  (`ALARM_HALL` — "outside the sleeping area", approximated as a room sharing a
  door), one on every level (`ALARM_LEVEL`), and — where bedrooms meet a
  garage/shop — a CO alarm outside the bedrooms (`ALARM_CO`, info; fuel appliances
  aren't modelled). The packet's Electrical Plan sheet counts them.
- **Safety glazing (IRC R308.4).** A window in a hazard location gets a
  `WINDOW_TEMPERED` warning and reads "tempered" in the window schedule's Glazing
  column: within 24 in of a hinged door on the same wall (a sidelite), a low
  window (sill < 60 in) within 60 in of a tub/shower in a wet room, or a low
  window (sill < 36 in) within 36 in of a stair. Raise the sill (a high privacy
  window is exempt) or accept that the glass must be tempered — there is no
  `tempered` override attribute yet, so the rule is derived from geometry.
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
- Every envelope, room, and wing side is a **plausible dimension** — finite and
  no larger than **1000 ft** (`DIM_IMPLAUSIBLE`). A bigger value is a typo (a
  stray digit, or an overflow) that would poison the area takeoff with `inf`; it
  is rejected and clamped so the rest of the report still reads.
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
- An opening's `width` is **positive** (`OPENING_SIZE`) — a window, interior door
  or exterior door (including an overhead door) with `width <= 0` isn't buildable
  and would misprice the estimate, so it's rejected (a positive `width` is required).
- A `wall` statement names two existing rooms (`WALL_REF`) that really share a
  wall (`WALL_NOADJ`) — it declares an attribute of a wall that must exist.
- A bedroom whose only exterior windows are `fixed` has **no escape opening**
  (`BEDROOM_EGRESS`) — fixed glass doesn't open.
- `SETBACK` — with a `site` + `setback` declared, the building footprint
  (envelope + wings + porches) must fit the **buildable rectangle** (the lot minus
  its setbacks). Checked by dimensions only, unless a `building at <x>,<y>` pins
  the building on the lot — then the check measures each edge's real yard and
  names the encroached side + overrun in ft-in. A `setback` with no `site` is a
  `SETBACK_NO_SITE` error.
- Electrical (opt-in, per room that draws devices): `OUTLET_SPACING` — a
  habitable room with outlets has a wall point more than 6 ft from a receptacle
  (IRC E3901.2). `OUTLET_GFCI` — a wet-room outlet isn't marked `gfci` (E3902).
  `ROOM_NO_LIGHT` — a habitable room has power (outlets/switches) but no `light`
  (E3903, info).
- Numbers are finite; ids/names are non-empty.

**Warnings (should address):**
- 8% natural-light glazing for habitable rooms (`living, great_room, kitchen,
  dining, bedroom, office, flex, rec_room, loft`) — windows must be on
  **exterior** walls to count.
- `VENT_AREA` — 4% **openable** window area (half the 8% light floor) for
  natural ventilation (IRC R303.1). A `fixed` window daylights but opens nothing,
  so it doesn't count here — make one operable or confirm mechanical ventilation.
- `DOOR_NO_LANDING` — an exterior `entry` with no landing on the outside (IRC
  R311.3). A `porch` covering the door's face (its full width, ≥ 3 ft deep) is the
  landing. With porches drawn anywhere, every uncovered entry warns; on a plan
  with *no* porches it's a single INFO nudge on the primary entry.
- A `window` on an interior wall (gives no daylight/egress).
- `ROOM_HABITABLE` — a habitable room (`living`, `dining`, `office`/den, `loft`)
  below the **IRC R304** minimum: 70 sq ft of floor area (R304.1) and 7 ft in
  every horizontal dimension (R304.2). **Bedrooms** carry the same rule as a hard
  *error* (`BEDROOM_AREA`/`BEDROOM_DIM`, above) and aren't repeated here; a
  **kitchen** is exempt from both (R304.2). Thresholds follow the active profile's
  habitable-room minimums.
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
- Placed fixtures (the `fixture` statement — judged over the resolved layout, seeds
  plus authored pieces): `FIXTURE_TOILET_CLEARANCE` — a toilet with under 15 in
  from its centreline to the nearest wall/fixture or under 21 in of clear floor in
  front (IRC R307.1). `RANGE_WINDOW` — a cooktop under an operable window (a draft
  blows out a burner; curtains hang over the flame). `FIXTURE_EGRESS` — a tall
  piece (fridge/wardrobe/water heater) parked over a bedroom's escape window
  (IRC R310). `FIXTURE_STAIR` — a fixture on a stair's run/landing footprint.
- `DOOR_SWING_INWARD` — an exterior door swings inward (the residential default)
  into a room too shallow for its leaf to fully open. Deepen the room, narrow the
  door, or use an out-swing/sliding door.
- `DOOR_HITS_FIXTURE` — a door's swing arc eats the wall a wet room or kitchen
  needs to place a fixture the room otherwise has floor area for (the gap
  `BATH_CLEARANCE`, which is door-blind, can't see). Move the door along the wall,
  swing it the other way (`into`/`hinge`), make it pocket/sliding, or enlarge the
  room.
- `STAIR_GEOMETRY` / `STAIR_OOB` / `STAIR_LEVELS` (errors), `STAIR_RUN` /
  `STAIR_FLOAT` — a stair with bad geometry, too short a run, or landing in no room.
- `WINDOW_TEMPERED` — a window in an IRC R308.4 hazard location (beside a door,
  near a tub/shower, near a stair, or a large glazing panel over 9 sq ft with its
  bottom edge below 18 in and top above 36 in — R308.4.3, *anywhere*) needs safety
  glazing. Declare `tempered` to silence it (the schedule then reads "tempered
  (declared)").
- `WINDOW_FALL` — an **operable** window with a sill below 24 in on an upper
  storey needs window fall protection (an opening-control device / fall guard,
  ASTM F2090 — IRC R312.2). The model has no grade elevation, so `level >= 1`
  stands in for "more than 72 in above grade". Fit a control device that limits
  the sash to a 4 in opening but still releases for escape — don't raise the sill
  (that fights the R310 egress-window rule). A `fixed` sash is exempt.
- `RECEPTACLE_COUNTER` — a kitchen counter run (a `fixture counter ... along` run
  ≥ 12 in wide) has a point more than 24 in from a small-appliance receptacle
  (IRC E3901.4). Add an `outlet` on the counter wall in the gap. Only checked once
  the plan draws its electrical layer, like `OUTLET_SPACING`.
- `PROGRAM_MISMATCH` — the rooms placed don't match a declared `program` (e.g.
  `program 3 bed` but only two bedrooms exist). The plan is still valid/buildable
  — it's a contract check, not a code error — so it's a warning.
- `PROGRAM_AREA_OVERRUN` — the drawn conditioned area is substantially larger
  than `program ... area`. The area clause is still a minimum, but this catches
  the common case where the brief's square footage was intended as a target.
- `KITCHEN_PASSTHROUGH` — the kitchen is the only route between dining and a
  living/great/rec room, so through-traffic crosses the work zone. Open the
  public rooms directly to each other or route a hall around the kitchen.
- `REQUIRE_UNMET` — the compiled geometry doesn't satisfy a declared `require`
  (a required adjacency/separation/exterior wall/minimum area). Same contract
  logic as `PROGRAM_MISMATCH`, so a warning; a `require` naming an unknown room
  id is a `REQUIRE_REF` **error**, like any dangling reference.

**Info (design quality — heed when you can):**
- `STAIR_HANDRAIL` — a stair flight of 4+ risers needs a handrail (IRC R311.7.8);
  the DSL can't draw one, so it's a one-per-plan checklist reminder on the first
  qualifying stair (carry it onto the construction documents).
- `STAIR_LANDING` — a single straight flight climbs more than 12 ft 7 in (151 in)
  of vertical rise; IRC R311.7.3 wants an intermediate landing (a switchback or
  L-turn) at that point. A normal one-storey flight stays well under, so this only
  speaks up on a tall or multi-level run. Note the mid-run landing on the CDs.
- `DOOR_THRESHOLD` — a once-per-plan reminder (before any `porch` is drawn) that
  the exterior landing at the required egress door may be no more than 1.5 in below
  the threshold — 7.75 in only where the door doesn't swing out over it (IRC
  R311.3.1). The model has no vertical threshold data; confirm the drop on the CDs.
- `GARAGE_DOOR` — a door between a `garage`/`shop` and the dwelling must be
  self-closing and 20-minute fire-rated (or solid-core / solid-wood ≥ 1-3/8 in
  thick — IRC R302.5.1). Anchored on the `door` statement itself.
- `GARAGE_VEHICLE_DOOR` — a `garage`/`shop` bay has no exterior overhead door,
  so it is trapped behind the dwelling and cannot function as a vehicle/equipment bay.
- `CLOSET_DOOR_SWING` — a swing door serving a `closet` shallower than the door is
  wide, so the leaf can't fully open; make it a bypass/sliding or bifold door.
- `WATER_HEATER_PLACEMENT` — a `water_heater` fixture in a garage/shop (ignition
  elevation, M1307.3) or on level 1+ over habitable space (drain pan, P2801.6).
- `KITCHEN_FLOW` — open the kitchen to dining/living.
- `BED_PRIVACY` — don't open a bedroom straight onto a public room; buffer with a
  hallway.
- `BATH_DISTANCE` — keep a bath within a door or two of the bedrooms.
- `WET_GROUP` — cluster wet rooms (bath/kitchen/laundry/utility) onto a shared
  plumbing wall; 3+ that share no walls means longer, costlier runs.
- `NO_CLOSET` — a bedroom with no closet reached *by a door* from it.
- `ROOM_PROPORTION` — a habitable room more elongated than ~3:1 is hard to
  furnish. `ROOM_SKINNY` escalates extreme non-circulation strips (~4:1+) to a
  warning because they read as leftover corridor space rather than usable rooms.
- `ROOM_TIGHT` — a room below the floor its use needs: kitchen ~70, full bath ~48
  (≥ 6 ft short side), half bath ~30 (≥ 5 ft) sq ft.
- New semantic room-type nudges: `SAFE_ROOM_WINDOW` / `SAFE_ROOM_EXTERIOR` /
  `SAFE_ROOM_SIZE` / `SAFE_ROOM_ACCESS`; `MECH_CLEARANCE` / `MECH_ACCESS` /
  `MECH_BEDROOM`; `FOYER_FLOW` / `FOYER_SHAPE`; `STORAGE_SHAPE` /
  `STORAGE_ACCESS`; `GREAT_ROOM_SCALE` / `GREAT_ROOM_FLOW`; `FLEX_FUTURE_BED`;
  and `REC_ROOM_SCALE` / `REC_ROOM_NOISE` keep the new labels honest.
- `HALL_TIGHT` — a hallway at the 3 ft code minimum; 4 ft is comfortable.
- `HALL_DEADEND` — a hall that serves ≤ 1 room, **or** runs well past its last
  **doorway** into a blank wall (a dead-end stub). Put the end room's door *at*
  the hall end (extend that room to cap the hall), or trim the hall back.
- `DOOR_SWING_CLASH` — two door leaves sweep into the same space and foul each
  other; move one along its wall, swing it the other way, or make it pocket/sliding.
- `DOOR_SWING_UNSET` — a bedroom/bath door with no `into` whose default swing
  opens *out* of the private room; pin it with `into <room>` so it opens into the
  space it serves. (With no `into`, a leaf swings east/north, or into the other
  room when it's too wide for that one. The plan, DXF, 3D model and every swing
  check use that same side, and an exterior door always swings inward.)
- `DOOR_SWING_PRIVACY` — a bedroom/bath door set to swing out into circulation;
  swing it `into` the private room so the leaf screens the view and folds to a wall.
- `ENVELOPE_MODULE` — an exterior (envelope/wing) dimension isn't a multiple of
  the 3 ft build module; rounding to it cuts sheet goods and framing with less waste.
- `NO_BACK_DOOR` — a home with a single exterior door; add a back/side door (off
  the kitchen, mudroom or laundry) for daily flow and a second way out.
- `DOOR_CENTERED` — a swing door floating mid-wall; back it to a corner (`offset`)
  so one side keeps an unbroken wall to furnish.
- `DOOR_SIZE` — a swing door off the stock leaf sizes; use `open` for a wide
  cased passage instead of a 96 in "door". `DOOR_WIDE_SWING` catches a wide
  single swing leaf; use `open` between public rooms or `double`/`french` for a
  real pair of leaves.
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
- Placed-fixture nudges (authored `fixture` pieces only — the auto-placer fits its
  own seeds): `FIXTURE_OOB` / `FIXTURE_OVERLAP` / `FIXTURE_DOOR` /
  `FIXTURE_OPENING` (past the room, overlapping, in a door swing, or across a
  doorway/opening); `FIXTURE_FRONT` — the clear-floor strip in front
  of a fixture (or a walkway beside a free-standing piece) is blocked;
  `FIXTURE_BACKING` — a wall-backed piece (vanity, range, dresser…) floating off
  every wall; `FIXTURE_ROOM_TYPE` — a fixture in a surprising room type (a tub in a
  living room). Plan-level infos judged over seeds *and* authored pieces:
  `RANGE_LANDING` — no counter/sink/fridge beside the cooktop (NKBA);
  `KITCHEN_TRIANGLE` — a sink–range–fridge triangle over ~26 ft (appliances too
  scattered); `DRYER_VENT` — a dryer more than ~10 ft from any exterior wall.
- Countertop-run nudges (the `along` form): `COUNTER_DOOR` (warning) — a run spans
  a doorway/opening/entry on its wall (stop it short with `from`/`to`);
  `COUNTER_ROOM` (info) — a counter in a room where a run reads as odd (bedroom,
  closet, hallway, loft); `SINK_NO_COUNTER` (info) — a kitchen sink not set into
  any counter run (fires only once the kitchen has counters to compare against).

### Countertop runs — `fixture counter ... along <wall>`

A counter is any length, so it has its own placement form: `along` lays a
countertop **run** down a wall.

```barn
fixture counter in kitchen along S                 # the whole south wall
fixture counter in kitchen along S from 2 to 12     # a partial run (room-local ft; ft-in ok)
fixture counter in kitchen along W depth 2-1         # depth override (default 25 in = 2-1)
```

- `from <a> to <b>` is room-local feet measured from the wall's **S/W start
  corner** (the same convention as every other wall offset); omit it for the full
  wall. `depth <d>` (1–4 ft) projects into the room; the default is the
  US-standard **25 in** (2′1″).
- An **L or U** kitchen is just two or three runs that meet at a corner — the
  compiler treats a mitred corner as a join (no `FIXTURE_OVERLAP`), and a sink or
  range whose footprint sits inside a run is *set into* it (also no overlap). A
  refrigerator over a run still warns — it stands proud, it isn't inset.
- `along` is a counter-only form and is exclusive with `at`/`wall`/`width`. It
  round-trips through `emit`/`fmt`, transforms under `use` mirror/rotate, and the
  takeoff reports `counter_linear_ft` / `counter_area_sqft` (and a countertop cost
  line). Dragging a run in the playground slides it along its wall.

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
| `street <wall>` | the wall facing the street/approach → APPROACH_ENTRY / APPROACH_GARAGE nudges |
| `overhang <ft>` | roof eave/rake projection past the walls (shades south glass; widens the roof + takeoff) |
| `climate <zone>` | IECC climate zone 1–8 → ENERGY_ENVELOPE R-value guidance + the WINDOW_HEAVY (WWR) ceiling |
| `note "…" [at <x>,<y> [level <n>]]` | free text, or a positioned leader callout drawn on the plan |
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

## Accepting a diagnostic (`# barndsl: accept`)

Real projects have justified deviations. A **suppression pragma** waives a
*specific* diagnostic without hiding it — a comment of the form:

```barn
window bath north width 3 offset 2   # barndsl: accept WINDOW_TEMPERED "glass block, inherently safety-rated"
# barndsl: accept HALL_DEADEND "gallery wall by design"
room hall: hallway at 18,0 size 4 x 24
```

- **Trailing** a statement (the primary form) accepts that `CODE` for the
  diagnostics anchored to that line. **On its own line** it accepts the code for
  the next statement line.
- "Accept" means *downgrade, not delete*: the diagnostic becomes an **INFO** with
  a `(accepted: "<reason>")` suffix, it stops deducting from the design score, and
  the audit trail survives (the permit packet lists it under **Accepted
  deviations**).
- **Errors can't be accepted** — an error must be fixed (`ACCEPT_DENIED`). An
  unknown code is `ACCEPT_UNKNOWN` (with did-you-mean); a pragma that matches
  nothing on its line is `ACCEPT_UNUSED` (a stale pragma to remove).

## Window flags: `fixed` and `tempered`

- `fixed` — sealed glass. It daylights (counts for `NAT_LIGHT`) but opens nothing,
  so it never counts as bedroom egress and doesn't help the 4% ventilation floor
  (`VENT_AREA`). `fixed` is both a *kind* (right after the wall) and a trailing
  flag; both mean the same non-opening glass.
- `tempered` — declares safety (tempered) glazing, the IRC R308.4 escape hatch. A
  window in a hazard location (beside a door, near a tub/shower, near a stair)
  normally warns `WINDOW_TEMPERED`; declaring it `tempered` silences that and the
  window schedule reads **"tempered (declared)"** instead of "tempered (required)".

## Formatting: `barndsl fmt`

`barndsl fmt FILE` prints canonical source; `-w` rewrites in place; `--check`
exits non-zero when a file isn't already formatted (CI mode). Unlike an
`emit_dsl` round-trip, `fmt` **preserves every comment and pragma** — it
re-renders each statement line from its own tokens (single spaces, `:g` numbers,
feet-and-inches → decimal feet, lower-case leading keyword) and leaves comments,
blank lines and statement order untouched. It refuses to touch a file with parse
errors, so it can't mask breakage. In the playground, **Format** (Shift+Alt+F)
does the same, as one undo step. `fmt(fmt(x)) == fmt(x)`.

**Reading from stdin.** `compile`, `fmt`, and `score` accept `-` as the file
argument to read the source from standard input (`… | barndsl compile -`); it
compiles as an untitled buffer, so a `use` relpath can't resolve (paste the part
inline instead). A leading UTF-8 BOM is stripped automatically. When a file
can't be read — missing, a directory, no permission, or not UTF-8 — the tool
prints a one-line `error: <path>: <reason>` and exits **2**, never a traceback.

## Reusable blocks: `use` (cross-file composition)

A **part** is any `.barn` file with **no `plan` header** — rooms, interior
doors/opens, windows, fixtures, electrical devices, alarms and positioned notes,
authored in its own local feet (SW corner at 0,0). It's a proven, reusable block:
a bath core that never trips `FIXTURE_TOILET_CLEARANCE`, a master suite, a kitchen
L. The starter library lives in `examples/composed/parts/`.

Stamp a part into a plan with

```
use "<relpath>" as <alias> at <x>,<y> [level <n>] [mirror x|y] [rotate 90|180|270] [with k=v[, k=v…]]
```

* `"<relpath>"` is quoted and **relative to the including file's directory**.
  Absolute paths, `..` escapes, and pasted/browser sources with no home directory
  are `USE_UNRESOLVED` errors — keep parts under the folder you compile or serve.
* `as <alias>` is required and unique. Every id inside the part is stamped
  `<alias>.<id>`, so a `bed` room becomes `m.bed`. **Host statements reference the
  namespaced ids exactly like locals** — `door great - m.bed`, `alarm smoke in m.bed`.
* `at <x>,<y>` places the stamped bounding box's SW corner; `level <n>` lands it
  on host level *n* (default 0).
* `mirror x|y` reflects the part and `rotate 90|180|270` turns it counter-clockwise
  (rooms are axis-aligned, so only 90° steps — `rotate 45` is a teaching error).
  **`mirror y` flips east↔west** (a vertical mirror line), **`mirror x` flips
  north↔south**. With **both**, the part is **rotated first, then mirrored** in its
  own local frame; the transformed bounding box's SW corner still lands at `at`.
  Wall directions, wall offsets, interior-door offsets and fixture rotations all
  remap so the stamped copy stays code-clean — a bath core that clears its
  clearances clears them mirrored or turned. Set them live in the playground's
  instance inspector (mirror / rotate selects), or via `set_use`.

The part is compiled **once** (in *fragment mode* — no envelope required, only
its own local checks run) and stamped per `use`. Everything downstream — render,
packet, exports, score — sees ordinary namespaced rooms, so composition needs no
new code anywhere else. Diagnostics split in two: a **part-internal** finding
(one that fires inside the part regardless of placement) is reported **once**,
anchored to the part file (`in part parts/master_suite.barn:12 — …`); a
**placement-dependent** finding (overlap, out of envelope, egress) fires **per
use**, anchored to the `use` line with the alias named (`instance m: …`). Accept
either with a `# barndsl: accept CODE` pragma — on the part's line for the former,
on the `use` line for the latter.

A worked composition (`examples/composed/cedar_ridge.barn`):

```
plan "Cedar Ridge (composed)"
envelope 48 x 30
ceiling 10

use "parts/master_suite.barn" as m  at 0,0
use "parts/kitchen_l.barn"    as k  at 26,18
use "parts/bath_core.barn"    as b1 at 40,0 mirror y   # the mirror-image core

room great: living at 0,13 size 22 x 17
door great - m.bed width 3 into m.bed hinge near   # host door into a stamped room
door dining - k.kitchen cased width 8
```

`emit_dsl(plan)` writes the `use` lines **verbatim** (transform included) and skips
the elements they stamped; `emit_dsl(plan, flatten=True)` drops the `use` lines and
writes the stamped members as literal statements in **world coordinates** (the
transform baked in, dotted ids kept), inlining every part. Recompiling the
flattened form reproduces the same composed plan.
In the playground the served folder is the resolution root; stamped members are
**read-only** (edit the part file, or use **Inline** to make one instance local),
while the instance itself is first-class — drag it, retarget its `at`/level/mirror/
rotate, Delete, Duplicate or Inline from the design panel's **Parts** group. The
same panel's **▣ Parts** button lists the plan-less `.barn` files beside the served
plan (and in `parts/`) — click **Insert** to stamp one at the plan centre.

### Parametric parts: `param` + `with`

A part can leave sizes open and let the host fill them in. Declare a parameter in
the part with a **mandatory default**:

```
# parts/flex_bath.barn
param width = 8
param depth = 6
room bath: bathroom at 0,0 size width x depth   # the bare NAME stands for a number
```

A bare param name stands **wherever a number stands** — sizes, positions, offsets,
widths. Params are **numbers only** (decimal feet or a ft-in literal like `7-6`); no
strings, no arithmetic. A name that matches no param is a `PARAM_UNKNOWN` error (with
a did-you-mean). The host passes values with a trailing `with` clause; every param
is *optional* (the default applies when it isn't passed):

```
use "parts/flex_bath.barn" as fb at 28,0 with width=9, depth=7-6
```

Write each pair with **no spaces around `=`** (`width=9`), comma-separated. A `with`
key the part doesn't declare is a `PARAM_UNDECLARED` error on the `use` line. The
part is compiled **once per distinct set of param values** (two instances with the
same values share the compile; different values recompile). Emit round-trips the
`with` clause exactly as written (ft-in canonicalized to decimal feet).

### Nested parts: a part that `use`s parts

A part may itself `use` other parts, up to **depth 2** (host → part → part). A
nested path resolves relative to the **using part's** folder (a part in `parts/lib`
reaches a sibling by bare name), but the sandbox root stays the **host's** folder at
every level — a nested `..` or symlink that leaves the host tree is still refused.
Ids namespace all the way down: a `bath` room two levels deep becomes
`outer.inner.bath`. A `use` that would reach depth 3 is `USE_NESTED`; a part that
`use`s itself or an ancestor is a clean `USE_CYCLE` (it names the cycle, never
hangs). The 64-instance cap counts every stamp at every depth against one budget.

### Multi-level parts: `level 1` rooms + a `stair`

A part can carry upper-level rooms and a connecting stair — a shop with a storage
loft above it, say:

```
# parts/shop_loft.barn
room shop: shop at 0,0 size 24 x 24
room loft: loft at 0,0 size 24 x 16 level 1
stair st at 19,0 size 4 x 15 from 0 to 1
```

`use ... level n` offsets **every** member's level by *n* (the loft above lands on
level `1 + n`, the stair's `from`/`to` shift too). Whole-building, cross-level checks
— stair rise/run, per-storey smoke alarms, loft guards, garage separation — run on
the **composed** plan, so they see the final levels (they can't run inside the part
alone). Place the part's exterior walls where they carry glass; a loft over open
floor draws a `LOFT_GUARD` you can accept.

See `examples/composed/cedar_ridge_v2.barn` for a plan that uses all three: a
parametric bath sized with `with`, a nested guest wing (its bath is a nested part),
and a two-level shop-with-loft.

## Touch devices (iPad, touch laptops)

The playground is usable with a finger — nothing to enable, it adapts to a coarse
pointer automatically.

- **Plan pane (view mode):** one finger drags to pan, two fingers pinch to zoom
  around the point between them, and a double-tap fits the plan to the pane (same as
  the **Fit** button). The zoom stepper and **Fit** still work too.
- **Edit layout:** turn on **Edit layout**, then tap a room to select it and drag it
  (or a corner/edge handle) to move or resize — each drag writes one surgical edit to
  the code, exactly like the mouse. One finger on empty space pans the overlay and two
  fingers pinch-zoom it; a room or handle under the finger drags instead (the usual
  tablet-CAD convention). If iOS takes over a gesture mid-drag, the drag aborts cleanly
  with nothing written.
- **Nudge chevrons:** with a room selected on a touch device, four arrows appear around
  it — each tap nudges the room 1 ft (the touch stand-in for the arrow keys).
- **Measure** works by dragging between two points, same as on the desktop; endpoints
  get fatter grab circles on touch.
- Hit targets (buttons, tabs, list rows, panel fields, drag handles) grow to a
  comfortable finger size on touch, and the on-screen keyboard shrinks the editor
  rather than hiding the toolbar. Page-wide pinch-to-zoom stays available (e.g. on the
  **Report** tab); only the plan pane consumes its own pinch gestures.

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

## Jurisdiction profiles: compiling under local code amendments

The code checks enforce one IRC-flavoured rule set by default. But the numeric
thresholds a check compares against — minimum ceiling height, habitable-room
area and width, hallway width, egress-window clear area/dimensions/sill, stair
riser/tread, daylight glazing — are exactly the numbers a county or state
*amends*. A **jurisdiction profile** bundles those ~14 amendable thresholds so
"compile under these local rules" is one flag instead of mental math on every
diagnostic.

```bash
barndsl profiles                             # list the built-ins and their numbers
barndsl compile plan.barn --profile strict   # a tighter, accessibility-leaning set
barndsl compile plan.barn --profile rural    # a looser example
barndsl compile plan.barn --profile travis.json   # your own JSON override file
barndsl score   plan.barn --profile strict   # score/build honour it too
```

Built-ins: `default` (alias `irc-2021`, the baseline — compiling with it is
identical to compiling with no profile), `strict`, and `rural`. A **JSON file**
overrides any subset of the thresholds and can extend a built-in:

```json
{ "name": "Travis County", "extends": "default",
  "min_ceiling_height": 7.5, "min_tread_depth": 0.9167 }
```

Unknown keys are rejected with the list of valid ones. Measurements are in feet
(so an 11 in tread is `0.9167`). From Python:

```python
from barndsl import compile_file, load_profile
result = compile_file("plan.barn", profile=load_profile("strict"))
```

A profiled diagnostic stays **honest about what number was enforced**: it prints
the enforced threshold, names the profile, and cites the IRC base value it
amended — e.g. *"Ceiling height 7 ft is below the 7.5 ft minimum for habitable
space (the 'strict' profile amends the IRC base of 7 ft)."* Only these ~14
thresholds are profile-driven; every other check (geometry, door/opening sizes,
fixture clearances, the design-quality nudges) is unchanged.

> **Not legal advice.** The non-default built-in profiles (`strict`, `rural`)
> are ILLUSTRATIVE examples of *how* thresholds vary between jurisdictions —
> plausible numbers chosen to demonstrate the mechanism. They are **not**
> transcribed from any adopted code. Always confirm the thresholds your
> jurisdiction actually enforces with the authority having jurisdiction before
> relying on a compile.

## Porches

`porch <id> at <x>,<y> size <W> x <L> [covered|open]` is an exterior platform. It
may sit **outside** the envelope (e.g. a front porch at a negative `y`) and is
exempt from the overlap / out-of-bounds / area checks that apply to rooms — so
place it wherever it physically goes (typically just outside an `entry`).

Every porch carries a **slab** line in `barndsl cost`; a `covered` porch adds a
**porch-roof** line too (at the same slab/roof rates as the house).

## Schedules and estimates

- **`barndsl schedule --doors --windows`** now includes a **Near jamb** column:
  the layout offset from the wall's canonical **start corner** to the opening's
  near jamb (the edge a framer measures to), stated as e.g. `4′ from W`. A
  horizontal (north/south) wall is measured from its **W**est end, a vertical
  (east/west) wall from its **S**outh end. Interior doors report it too; a door
  with no `offset` is centred, so its near jamb sits half the leftover to one
  side. The 2D plan's outermost chain dimension also **breaks at exterior opening
  jambs** (wall-segment / opening-width / wall-segment); interior doors get the
  schedule offset only, no plan leader.
- **The 2D plan (and the packet + DXF) tag every door and window** with the
  **same D1…/W1… marks the schedules assign** — a small bubble on the room side
  of each opening, numbered from one shared helper so a plan tag and its schedule
  row can never disagree (interior doors first, then exterior doors; windows in
  source order). The DXF writes them as TEXT on `A-ANNO-NOTE`.
- **`barndsl cost`** itemizes the shell (including **gable-end wall triangles**
  for a gable roof), foundation, partitions, openings, plumbing fixtures **and
  laundry washer/dryer**, systems (electrical + HVAC allowances) and finishes,
  then prints an **exclusions** footer (site work, well/septic, permits, HVAC
  unless itemized, GC overhead & profit). Every unit cost is overridable with
  `--costs FILE.json`. Run **`barndsl cost --print-keys`** (no plan needed) to
  dump the full overridable key table — every key with its default, unit and a
  one-line meaning — so you know exactly what an override touches. Add `--json`
  to get that same table as a machine shape (`[{key, default, unit, meaning}, …]`).
  - **Gable-end wall triangles** stand on the building's **short** dimension: the
    ridge always runs the **long** axis, so each gable triangle's base is
    `min(width, length)` and the pair is `min(width, length)² × pitch / 2` sq ft
    (priced at the exterior-wall rate). It's rotation-symmetric — `envelope 40 x 20`
    and `envelope 20 x 40` cost the same.
  - **Openings are priced by size, not a flat per-each** (so a picture window
    costs more than a bathroom awning and a 16 ft garage door costs more than a
    9 ft one):
    - **Windows** — a per-window base **`window_each`** (frame + flashing +
      install, default `$300`) **plus** **`window_glazed_sqft`** per sq ft of
      glazed (sill-to-head) area (default `$40`). A typical 3×4 window (12 sqft
      glazed) lands at `300 + 40×12 = $780`, the old flat casement price.
    - **Exterior (people) doors** — **`door_exterior`** (default `$1,500`) is a
      per-leaf rate **width-weighted over 3 ft**: each door counts as
      `max(1, width/3)` standard leaves, so a 6 ft double/french pair prices at
      2× a 3 ft single (~`$3,000`, near the old `$2,800` pair).
    - **Overhead (garage) doors** — **`garage_door_lf`** per linear ft of width
      (default `$178`): a 9 ft single ≈ `$1,602` (near the old `$1,600` flat), a
      16 ft double ≈ `$2,848` (~1.8×).

    All four keys are overridable via `--costs`.
- **`barndsl compare A B`** buckets diagnostics by how their count moved:
  `resolved` (gone entirely in B), `introduced` (new in B), `fewer` (dropped but
  still firing, `CODE (5 → 4)`) and `more` (rose). When both sides compile
  cleanly it also shows the **cost delta** (`Cost: $196,227 → $200,794
  (+$4,567)`).

## Exports: DXF for CAD

`barndsl dxf FILE.barn --out plan.dxf` writes a **DXF R2000 (AC1015)** drawing —
the oldest DXF that carries declared drawing units and the `LWPOLYLINE` entity,
so the export is a real drawing rather than a massing underlay. It is hand-written
in pure Python (no `ezdxf` dependency), and is **byte-reproducible**: the same
plan always yields an identical file.

- **Units are declared imperial** — `$INSUNITS = 2` (feet), `$MEASUREMENT = 0`
  (English), `$LUNITS = 4` (architectural). Coordinates pass straight through at
  1 unit = 1 foot, and `$EXTMIN`/`$EXTMAX` bound the whole drawing including the
  dimension strings.
- **Walls are closed, hatchable polygons** with real thickness (the same nominal
  `EXTERIOR_WALL_THICKNESS` / `INTERIOR_WALL_THICKNESS` the schedules net out):
  the exterior shell as per-side bands centred on the envelope, interior
  partitions centred on each shared room edge, with door and window openings
  **cut out** of the band.
- **Doors** draw a leaf line plus a 90° swing `ARC` (the SVG's hinge/swing side);
  **windows** draw the classic sill / head / centre-glazing symbol. An
  **overhead/sectional** door draws the plan glyph — a dashed panel line pair
  across the opening plus a dashed track line inside — mirroring the SVG.
- **Countertops** meeting in an L/U corner are drawn trimmed to abut, with a 45°
  miter joint across the corner square (the same computation the SVG uses), so a
  wrap-around kitchen reads as one continuous surface, not crossing boxes.
- **Loft/balcony guard lines** — where an upper room only partly covers the room
  below, its open edge over the double-height void (the same edge `LOFT_GUARD`
  flags) draws as a thin double line on `A-FLOR-OTLN`, on the loft's level.
- **Dimensions ship two flavors — pick with `--dxf-dims`** (or `to_dxf(plan,
  dims=…)`). The **default `geometry`** flavor emits the overall dims per side plus
  the exterior chain strings (with jamb breaks) as loose dim lines, extension
  lines, ticks and `TEXT` on `A-ANNO-DIMS` — every viewer renders them, a drafter
  can explode/re-associate, and the bytes are identical to the historical output.
  The **`associative`** flavor emits real rotated-linear `DIMENSION` entities —
  one per overall/chain segment — each backed by an anonymous `*D<n>` block that
  holds *exactly* that exploded geometry: a regenerating reader (AutoCAD,
  BricsCAD) gets live, editable dims, while a non-regenerating viewer still shows
  today's picture. Our ft-in label rides on the entity (group 1) so it survives a
  regen even though the reader recomputes the raw measurement; the definition
  points sit on the non-plotting `Defpoints` layer, the AutoCAD convention.
- **Multi-level plans** draw every floor at true model coordinates, with each
  floor above the ground on `-L{n}`-suffixed copies of the layers (`A-WALL-L1`,
  …) — toggle a level's layers to isolate it. Dimensions annotate the ground
  floor.

The geometry lands on AIA-style, discipline-prefixed layers:

| Layer | Colour | Contents |
| --- | --- | --- |
| `A-WALL` | 7 | Wall bodies — exterior shell bands + interior partitions |
| `A-DOOR` | 30 | Door leaves and swing arcs |
| `A-GLAZ` | 5 | Windows (sill / head / glazing lines) |
| `A-FLOR-FIXT` | 8 | Plumbing / kitchen fixtures and furniture, with kind labels |
| `A-FLOR-OTLN` | 9 | Porch and stair outlines (dashed) |
| `A-AREA-IDEN` | 3 | Room name and area text |
| `A-ANNO-DIMS` | 2 | Dimension lines, ticks and text |
| `A-ANNO-NOTE` | 4 | Leader notes |
| `S-COLS` | 6 | Structural frame posts and beams |
| `Defpoints` | 7 | Dimension definition points — non-plotting (`--dxf-dims associative` only) |

For a full BIM hand-off (walls with voided openings, spaces, roof, stairs) use
`barndsl ifc` (IFC4) instead — DXF is the 2D-drafting deliverable.

## Revit exchange: `revit` / `revit-import`

`barndsl revit FILE.barn` lowers a plan to the `barndsl.revit/1` exchange JSON
for the pyRevit add-in, and `barndsl revit-import FILE.json` reconstructs DSL
from that exchange (or from a model the add-in exported back).

The export→import round-trip is **geometry-faithful but not
diagnostic-identical**: the reconstructed plan places every room, wall and
opening at the same coordinates, but each opening comes back with an **explicit
`offset` re-derived from its position**, because the exchange carries geometry,
not authoring intent. So *advice-class* infos that only fire on an
**unspecified** placement — most visibly `DOOR_CENTERED`, which flags a swing
door left to float mid-wall — vanish after a reimport by design: the door now
carries a concrete offset, so there is nothing left to advise about. This is
expected drift, not data loss; the drawing is unchanged.
