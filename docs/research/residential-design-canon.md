# The Residential Design Canon — Source Research for barndsl Rules

**Purpose.** This is a research brief for whoever (human or agent) writes the next batch of
barndsl validation rules, design-quality nudges, and scoring heuristics. It distills the
residential design literature that sits *outside* the Ching/Allen line — Christopher
Alexander's *A Pattern Language*, Sarah Susanka's *Not So Big* books, the standard
dimensional references (Architectural Graphic Standards, Time-Saver Standards for Housing,
NKBA planning guidelines), and the IRC-derived habitability floor — and translates each idea
into a **rule candidate**: something a compiler can actually measure.

**How to read this.** Every principle gets three things: (a) the idea restated in plain
language, (b) *why it matters* — the failure mode it prevents, and (c) a **Rule candidate**
with a proposed diagnostic code, severity, threshold, and the geometry the check needs. Codes
are written in barndsl's existing `SCREAMING_SNAKE` convention. Where an existing barndsl
diagnostic already covers part of the ground, it is named so the implementer extends rather
than duplicates (barndsl currently registers ~212 diagnostics; see `docs/DIAGNOSTIC_MATRIX.md`).

**Copyright note.** Everything below is paraphrase. Pattern numbers, pattern names, and
numeric standards are facts and are stated directly; no source text is reproduced.

**Units.** barndsl works in feet. Inches are given where the source states inches; the
feet-equivalent is in parentheses when it matters for a threshold.

---

## 1. The sources and what each is good for

### 1.1 *A Pattern Language* — Alexander, Ishikawa, Silverstein (1977)

253 numbered patterns, each stated as a recurring problem plus a spatial resolution, ordered
from region scale down to construction detail. Patterns 95–204 are the building- and
room-scale ones relevant here. The book's core claim is that livability is *structural*: it
comes from a small number of geometric relationships (where light falls, how privacy is
sequenced, where people naturally sit), not from finish or square footage.

**Why it's the best source for a rule engine.** Alexander's patterns are already stated as
conditional geometry — "if X, arrange Y" — and many carry explicit numbers (10 ft between
kitchen work centers, 12 ft of counter, 6 ft balcony depth, 10–12 ft public ceilings). They
map onto adjacency graphs, sightline traces, and wall-exposure counts almost directly.

**Its limits.** Patterns are *preferences*, not code. Almost none should be an ERROR. Most
belong at INFO with a scoring gradient, because they trade off against each other and against
budget. Alexander himself ranks patterns by confidence (zero, one, or two asterisks); a
sensible mapping is: two-asterisk patterns → WARNING-eligible, one/zero-asterisk → INFO only.

### 1.2 *The Not So Big House* (1998) / *Creating the Not So Big House* (2000) / *Home by Design* (2004) — Sarah Susanka

Susanka's thesis: American houses are built too big and detailed too cheaply, and the fix is
to spend the same money on less square footage with more spatial articulation. *Home by
Design* organizes roughly 30 design concepts under three headings — **Space**, **Light**, and
**Order** — including the process of entering, shelter around activity, sequences of places,
ceiling height variety, interior views, inside/outside, changes in level, public to private,
openability, enclosure, differentiation of parts, depth and thickness, light to walk toward,
varied light intensity, reflecting surfaces, window positioning, visual weight, view and
nonview, pattern and geometry, alignments, rhythm, theme and variations, composition,
expressed structure, point of focus, and organizing strategy.

**Why it matters for a barndominium.** Susanka is the canonical critic of exactly the failure
mode a big open shell invites: one undifferentiated volume with a high flat ceiling, nowhere
cozy, no acoustic refuge, and rooms nobody uses. Her "away room," "shelter around activity,"
and "ceiling height variety" ideas are the antidote, and all three are measurable.

**Its limits.** Her language is qualitative and perceptual. Turning "shelter around activity"
into a number requires committing to proxies (ceiling delta, enclosure ratio, alcove depth)
that are defensible but not canonical. Keep these at INFO and score-gradient only.

### 1.3 Dimensional standards — Architectural Graphic Standards, Time-Saver Standards for Housing and Residential Development, NKBA Kitchen & Bath Planning Guidelines

These are the "will the furniture fit and can a person walk past it" references. AGS and TSS
give typical room sizes, furniture footprints, and clearance envelopes; NKBA publishes 31
kitchen guidelines and 27 bath guidelines, each split into a *code requirement* and a
(larger) *recommended* value — a structure that maps beautifully onto WARNING vs INFO.

**Why they matter.** These are the only source in the set that gives *hard, checkable*
livability numbers that are not code: 42 in work aisle, 158 in of counter frontage, 30 in
recommended clear floor at a lavatory. barndsl already encodes a handful (`BED_FURNISH_SHORT`,
`DINING_FURNISH_CLEAR`, `DESK_WIDTH`); most of the catalog is unexploited.

### 1.4 IRC-derived habitability minimums

The International Residential Code is the floor, not the goal. The relevant chapter is
Chapter 3 (Building Planning): R303 light/ventilation, R304 minimum room area, R305 ceiling
height, R306 sanitation, R307 fixture clearances, R310 emergency escape, R311 means of egress.

**Why it matters.** These are the only rules that justify ERROR severity. A plan that violates
R304 or R310 is not a design opinion, it is unpermittable. barndsl already implements most of
this core (`BEDROOM_AREA`, `BEDROOM_DIM`, `BEDROOM_EGRESS`, `CEILING`, `NAT_LIGHT`,
`VENT_AREA`, `HALL_WIDTH`, `EGRESS_DOOR`, `STAIR_*`, `GARAGE_SEPARATION`).

**Its limits.** Code minimums are catastrophically low as design targets. A 7 ft × 10 ft
bedroom passes R304 and is uninhabitable in practice. Never let an IRC number double as a
quality target; that is what the WARNING/INFO tiers are for.

### 1.5 How the four tiers should stack in barndsl

| Tier | Source | Severity | Score effect |
|---|---|---|---|
| Legality | IRC | `error` | plan scores 0 |
| Function (fixtures/furniture fit) | NKBA / AGS "code" column | `warning` | 8 pts each, cap 40 |
| Comfort (NKBA "recommended", AGS typical) | NKBA / AGS | `info` | 2 pts each, cap 20 |
| Delight (pattern language / Susanka) | Alexander / Susanka | `info` + continuous term | gradient term, 2–6 pts |

The fourth tier is the one barndsl is thinnest on and the one this document is mostly about.
Because `score.py` caps info penalties at 20 points, adding 25 new INFO-only rules would
saturate the cap and flatten the gradient. **Recommendation:** add a new continuous score
component, `livability` (suggested weight 10–12), fed by the pattern/Susanka metrics as
*margins* rather than as counted infos, exactly as `proportion` and `daylight` already work.

---

## 2. Pattern Language principles and rule candidates

Ordered roughly as Alexander orders them: siting and entry, then the public/private
structure, then rooms, then room interiors.

### 2.1 Building form and siting

#### P107 — Wings of Light

**Idea.** Buildings should be composed of narrow, daylight-penetrable wings rather than deep
blocks. Alexander's working figure is that a wing much deeper than about 25 ft cannot get
useful daylight from both sides, so the middle becomes permanently artificial.

**Why it matters.** This is *the* structural tension in a barndominium. A metal building is a
wide clear-span box — 40, 50, 60 ft deep — and the residential wing carved out of it inherits
that depth. Rooms land in the middle with no exterior wall at all. Every daylight, ventilation,
and egress problem in a barndo plan traces back to this.

**Rule candidate.**
- `DEPTH_NO_DAYLIGHT` — **info**: any habitable room whose centroid is more than 25 ft from the
  nearest exterior wall of the residential wing. Score gradient: penalize `max(0, d − 25)/15`.
- `WING_DEPTH` — **info**: residential-wing short dimension exceeds 32 ft *and* more than 20% of
  habitable floor area has zero exterior wall exposure. Suggest a light court, clerestory, or
  an interior-window strategy (see P194).
- Related existing: `NAT_LIGHT`, `ROOM_NO_LIGHT`, `WING_SIZE`.

#### P109 — Long Thin House

**Idea.** When a household is small or wants every room to touch the outside, a long narrow
plan beats a compact square one, despite the worse surface-to-volume ratio.

**Why it matters.** It licenses the barndominium's natural shape. A 30 ft × 60 ft residential
bar with rooms in a single or one-and-a-half file gives every room two exterior walls. The rule
engine should not penalize *plan* elongation the way it penalizes *room* elongation.

**Rule candidate.** Explicitly exempt the building envelope from `ROOM_PROPORTION`-style
aspect logic (barndsl already scopes aspect checks per-room; document the exemption). Consider
a positive score credit: `exterior_exposure_ratio` = habitable rooms with ≥2 exterior walls /
all habitable rooms — reward ≥0.5.

#### P110 / P112 — Main Entrance, Entrance Transition

**Idea.** The front door must be visible and obvious on approach (P110), and there must be a
*transition* — a change of light, level, surface, or enclosure — between the outside world and
the interior (P112). Walking straight from a driveway into a living room is the failure the
pattern names.

**Why it matters.** The transition is what makes a house feel like a refuge rather than a
lobby. In a barndo it is also purely practical: mud, weather, and a 100 ft driveway.

**Rule candidate.**
- `ENTRY_TRANSITION` — **info**: the primary exterior door opens directly into a room of type
  `living`/`great_room`/`kitchen`/`dining` with no `foyer`, `mudroom`, or covered `porch`
  ≥ 6 ft deep in the sequence. Satisfied by any one of: a foyer ≥ 25 sf, a porch ≥ 6 ft deep,
  or a defined entry zone of ≥ 30 sf inside the great room bounded on ≥2 sides.
- `ENTRY_AXIS` — **info**: the primary entry door's inward axis terminates on a wall less than
  8 ft away (a blank-wall walk-in) *or* has a clear sightline > 40 ft (no arrival, just
  exposure). A good entry has 10–25 ft of forward view.
- Related existing: `ENTRY_INTERIOR`, `ENTRY_PRIVATE`, `NO_ENTRY`, `FOYER_FLOW`, `FOYER_SHAPE`,
  `APPROACH_ENTRY`, `DOOR_NO_LANDING`.

#### P113 — Car Connection

**Idea.** The car is the real front door of a rural house, so the path from the vehicle into
the building deserves the same care as the ceremonial entrance — covered, direct, and landing
somewhere that can absorb mess.

**Why it matters.** Barndominium residents enter from the shop or the drive 95% of the time.
If the shop-side entry dumps into the great room, the great room becomes a mudroom.

**Rule candidate.**
- `CAR_ENTRY_DROP` — **warning**: a door from `garage`/`shop` into the residential zone lands
  directly in `living`/`great_room`/`dining`/`bedroom` without an intervening `mudroom`,
  `foyer`, `laundry`, or `hallway`. (Extend `GARAGE_PASSTHROUGH` semantics.)
- `CAR_ENTRY_DISTANCE` — **info**: walking path from the garage/shop door to the kitchen
  exceeds 25 ft, or crosses the primary seating zone of a public room. This is the
  groceries rule.

#### P128 / P161 / P162 — Indoor Sunlight, Sunny Place, North Face

**Idea.** Put the rooms where people spend daytime hours on the sunny side; put service and
storage on the cold/dark side; and give the building a sheltered sunny outdoor spot adjoining
a well-used indoor room.

**Why it matters.** Free orientation decisions with real comfort and energy consequences, and
barndsl already has site/solar infrastructure (`docs/design/SITE_SOLAR.md`, `solar.py`).

**Rule candidate.**
- `SUN_DAY_ROOMS` — **info**: kitchen, dining, living/great room, and any room typed `office`
  have zero south-facing glazing while a south wall of the residential wing is available.
  Extends `SOLAR_SOUTH_UNUSED`.
- `SUN_SERVICE_NORTH` — **info**: `garage`, `shop`, `mechanical`, `utility`, `storage`,
  `laundry`, `pantry` occupy > 50% of available south wall length. Service uses displacing the
  solar frontage is the barndo-specific version of this pattern.
- `SUN_OUTDOOR_ROOM` — **info**: no `porch` (or site-defined outdoor room) that is (a) ≥ 6 ft
  deep, (b) adjacent to a public habitable room, and (c) on the south or east elevation.
- Related existing: `SOLAR_NORTH_ONLY`, `SOLAR_SOUTH_UNUSED`, `SOLAR_SOUTH_NO_OVERHANG`,
  `SOLAR_WEST_GAIN`.

#### P163 / P167 — Outdoor Room, Six-Foot Balcony

**Idea.** Outdoor space is only used if it is partly enclosed — walls, roof, or a change of
level on at least two sides. And any outdoor sitting space shallower than about 6 ft is
useless: chairs plus a passing person need that depth.

**Why it matters.** barndsl already has `MIN_PORCH_DEPTH = 6.0`, which is exactly P167's
number. The unexploited half is enclosure: a 6 ft × 40 ft porch strip along a wall with no
end walls is still barely used.

**Rule candidate.**
- `PORCH_ENCLOSURE` — **info**: a `porch` with only one bounding building face and no end
  return. Reward porches tucked into an L or bounded on ≥2 sides.
- `PORCH_ACCESS` — **info**: no porch is reachable directly from a public habitable room (a
  porch you must go through a bedroom or utility room to reach is not used).

### 2.2 The public/private structure of the house

#### P127 — Intimacy Gradient ★★ (the single most implementable pattern)

**Idea.** Arrange rooms so that moving inward from the entrance passes through steadily more
private territory: entry → common/social → semi-private → private. When rooms of different
privacy levels are interleaved, there is nowhere to hold a given kind of encounter, and
visitors are constantly in the wrong place.

**Why it matters.** This is the master ordering principle and it *is* a graph property.
barndsl already has zone machinery (`ZONE_PUBLIC_TYPES`, `ZONE_PRIVATE_TYPES`, `ZONE_CROSS`,
`ZONE_OVERLAP`) and a privacy nudge (`BED_PRIVACY`), but no measure of *monotonic sequence*.

**Rule candidate — the "privacy depth" metric.** Assign each room type a privacy rank:

| Rank | Types |
|---|---|
| 0 (outside) | `porch` |
| 1 (threshold) | `foyer`, `mudroom` |
| 2 (public) | `living`, `great_room`, `dining`, `kitchen`, `rec_room` |
| 3 (semi-private) | `hallway`, `half_bath`, `office`, `flex`, `laundry`, `pantry`, `loft` |
| 4 (private) | `bedroom`, `bathroom`, `closet` |
| — (service, excluded) | `garage`, `shop`, `mechanical`, `utility`, `storage`, `safe_room` |

Compute, for each room, `depth` = number of doors on the shortest path from the primary entry.

- `GRADIENT_INVERSION` — **info**: a room of privacy rank *r* is reachable at a strictly
  smaller door-depth than a room of rank *r−1* on the same branch. In plain terms: you reach a
  bedroom before you reach the living room.
- `GRADIENT_MONOTONIC` — score gradient (no diagnostic): Spearman correlation between privacy
  rank and door-depth across all rooms. Full marks at ρ ≥ 0.7; linear penalty to 0 at ρ ≤ 0.2.
  Suggested weight 3 pts within a `livability` component.
- `PRIVATE_FROM_ENTRY` — **warning**: a `bedroom` or `bathroom` door is directly visible from
  the primary entry door on a straight, unobstructed sightline shorter than 15 ft.
- Related existing: `BED_PRIVACY`, `ZONE_CROSS`, `PRIVATE_PASSTHROUGH`, `ENTRY_PRIVATE`.

#### P129 — Common Areas at the Heart ★★

**Idea.** The shared rooms must sit at the *center of gravity* of movement — on the path
between the entrance and the private rooms — not off to one side. If you can get from the door
to your bedroom without passing the common area, the family stops meeting.

**Why it matters.** In a barndo, the shop entrance often bypasses the great room entirely via
a back hallway. Technically efficient; socially fatal. This is measurable and is one of the
few patterns worth a WARNING.

**Rule candidate.**
- `COMMON_BYPASSED` — **info** (escalate to **warning** if it holds for *every* bedroom): the
  shortest interior path from any entry door to a given bedroom does not pass through, or
  directly adjoin, a room in the public set. "Directly adjoin" = the path traverses a hallway
  that has a wide opening (≥ 6 ft or `cased`/`open`) onto a public room.
- `COMMON_CENTRALITY` — score gradient: fraction of {entry doors × bedrooms} pairs whose
  shortest path touches the public set. Full marks at 1.0, full penalty at ≤ 0.34. Suggested
  weight 3 pts.
- `COMMON_SHARE` — **info**: combined public-room area is less than 25% of conditioned
  residential area (a house that is all bedrooms and corridors) or more than 60% with fewer
  than two distinct seating zones (see Susanka §3).

#### P130 — Entrance Room

**Idea.** The entrance deserves to be a *room* — a place to stand, greet, remove coats, set
things down — not a hallway widening.

**Rule candidate.** Already close to barndsl's `MIN_FOYER_AREA = 25.0`, `MIN_FOYER_DIM = 4.0`,
`MAX_FOYER_ASPECT = 2.5`. Add:
- `FOYER_STORAGE` — **info**: `foyer` (or the entry zone) has no adjacent `closet` within one
  door. A coat closet at the entry is the functional core of the pattern.

#### P131 — The Flow Through Rooms

**Idea.** Public rooms should be connected to each other by wide openings so movement flows
through the social space rather than being channeled into corridors alongside it.

**Why it matters.** The dual of P132 below: a plan can satisfy "short passages" by having no
passages at all — but only if the public rooms actually interconnect.

**Rule candidate.**
- `PUBLIC_FLOW` — **info**: a public room has exactly one opening (a dead-end social room), or
  the public rooms form a tree rather than containing at least one loop. Circulation loops are
  the geometric signature of flow.
- `PUBLIC_LOOP` — score credit: +1 to 2 pts if at least one circulation cycle exists among
  public rooms + hallway.
- Related existing: `GREAT_ROOM_FLOW`, `KITCHEN_FLOW`.

#### P132 — Short Passages ★

**Idea.** Corridors longer than roughly 50 ft are miserable; even a short passage must have
light, width, and something to look at. Alexander's remedy is to keep passages short and give
them daylight at the end.

**Why it matters.** A long single-loaded bedroom corridor is the default barndo failure — it's
the cheap way to serve four bedrooms along a 60 ft wall. barndsl has `HALL_WIDTH`,
`HALL_TIGHT`, and `HALL_DEADEND` but no length or daylight rule.

**Rule candidate.**
- `HALL_LONG` — **info** at > 20 ft of continuous hallway run; **warning** at > 35 ft. (Alexander's
  50 ft figure is the misery threshold; residential practice starts complaining much earlier.)
- `HALL_DARK` — **info**: a hallway run longer than 12 ft with no window, no glazed door, and
  no wide opening (≥ 5 ft) to a daylit room at either terminus. This is also Susanka's "light
  to walk toward."
- `HALL_SHARE` — already partly in `score.py`'s `circulation` term (free to 15% of interior
  area, full penalty at 35%). Keep; consider tightening the free band to 12% for single-story
  plans, where circulation cannot be amortized over floors.

#### P134 — Zen View

**Idea.** A treasured view is destroyed by constant exposure. Reveal it at a threshold — a
passage, a turn, a doorway — where it is encountered afresh, rather than glazing an entire
wall toward it.

**Why it matters.** This is the counterweight to "more glass is better," which barndsl's
`daylight` score term currently implies without limit. (There is already a `WINDOW_HEAVY`
diagnostic — this pattern is its design rationale.)

**Rule candidate.**
- `VIEW_SATURATION` — **info**: glazing on a single wall exceeds 40% of that wall's area *and*
  the room already meets the 8% daylight floor from other walls. Diminishing returns, rising
  cost, thermal penalty, and — per the pattern — a devalued view.
- `VIEW_AT_THRESHOLD` — score credit (+1 pt): a window is placed at the terminus of a hallway
  or at a stair landing. Cheap, high-perceptual-value.

#### P135 — Tapestry of Light and Dark

**Idea.** Uniformly lit interiors are lifeless. People move toward light; a house should have
bright destinations and dimmer connecting territory.

**Rule candidate.**
- `LIGHT_UNIFORM` — score gradient only: compute per-room glazing-to-floor ratio across
  habitable rooms; if the coefficient of variation is < 0.15 *and* all rooms sit near the same
  ratio, apply a small penalty (1 pt). The intent is to reward deliberate variation — a bright
  bay, a darker den — not to punish adequacy. **Implement last**; this one is the easiest to
  get wrong and annoy authors.

#### P136 / P137 / P141 — Couple's Realm, Children's Realm, A Room of One's Own

**Idea.** The adults need a private territory that is acoustically and visually separate from
the rest of the house; children need a zone of their own that connects to common space; and
every member of the household needs some space that is theirs alone.

**Why it matters.** These three justify the "split bedroom plan" that dominates modern
single-story residential design — primary suite on one end, secondary bedrooms on the other,
public rooms in between.

**Rule candidate.**
- `SUITE_SEPARATION` — **info**: the primary bedroom (largest bedroom, or one flagged with an
  ensuite) shares a wall of length > 4 ft with a secondary bedroom, or its door is within 10 ft
  of a secondary bedroom door along the same corridor. This is the split-plan check.
- `SUITE_BUFFER` — score credit: primary suite separated from secondary bedrooms by at least
  one public room or by ≥ 20 ft of path. +2 pts.
- `ROOM_OF_ONES_OWN` — **info**: a plan with ≥ 3 bedrooms and no `office`, `flex`, or `loft`
  has no space for solitary use beyond a bedroom. Low-confidence nudge; INFO only.
- Related existing: `BED_SOUND` (`MIN_SOUND_BUFFER_WALL = 4.0`), `MASTER_ENSUITE`,
  `SUITE_OVERLAP`, `SUITE_SHADOW`.

#### P138 — Sleeping to the East

**Idea.** Bedrooms benefit from morning light; put them where the sun comes up.

**Rule candidate.** `BED_EAST` — **info**, opt-in only (this is a genuine preference, and
shift-workers exist): no bedroom has east glazing while east wall is available. Suggest
gating behind a profile flag rather than firing by default. barndsl's profile system
(`profiles.py`) is the right home for it.

#### P139 — Farmhouse Kitchen ★★

**Idea.** The kitchen should be big enough to be *lived* in, not a workroom you retreat from —
the eating table belongs in or immediately at the cooking space, and the cook should not be
excluded from company.

**Why it matters.** This is the single most barndominium-appropriate pattern in the book. The
"great room" the barndo culture builds *is* the farmhouse kitchen, whether or not the builder
knows the reference.

**Rule candidate.**
- `KITCHEN_EATING` — **info**: no eating surface (a `dining` room sharing an opening ≥ 6 ft with
  the kitchen, or a declared island/peninsula with seating clearance, or a `great_room`
  containing the kitchen) is reachable from the kitchen without passing through a door.
- `KITCHEN_ISOLATED` — **warning**: the kitchen's only openings are conventional doors (no cased
  opening, no shared great-room boundary) *and* the kitchen is < 120 sf. A small closed kitchen
  in a barndominium is a program mismatch.
- `KITCHEN_SIZE_SOCIAL` — **info**: kitchen floor area below 120 sf in a plan with ≥ 3 bedrooms.
  (Separate from `MIN_USABLE_AREA[KITCHEN] = 70.0`, which is the "fixtures fit" floor. 70 sf is
  a galley; 120 sf is a kitchen people can be in together.)

#### P142 — Sequence of Sitting Spaces / P185 — Sitting Circle

**Idea.** A house needs several distinct places to sit, each with a different character —
formal, informal, solitary, outdoor — and each sitting group needs to be shaped by the room
(a bounded zone with a focus), not scattered furniture in an open field.

**Why it matters.** Directly addresses the barndo great-room hazard: 700 sf of open floor with
one sofa group floating in the middle and 300 sf of nothing.

**Rule candidate.**
- `SITTING_COUNT` — **info**: fewer than 3 distinct sitting places in the plan, counting: each
  public room with a bounded seating zone, an office/away room, a window seat/alcove, a porch
  ≥ 6 ft deep. Aim for 3–5.
- `SEATING_ZONE_UNBOUND` — **info**: a public room larger than 400 sf with no sub-zone defined
  by any of: a ceiling-height change, a wall return ≥ 3 ft, a level change, a declared
  island/peninsula, or a fireplace/hearth element. See also `GREAT_ROOM_SCALE`.

#### P144 — Bathing Room / P189 — Dressing Room

**Idea.** The bath is worth treating as a room, not a plumbing closet; and clothes storage
belongs *between* the bedroom and the bath, so the dressing sequence works.

**Rule candidate.**
- `SUITE_SEQUENCE` — **info**: in a primary suite, the walk-in closet is not on the path
  between bedroom and ensuite (or is entered only from the bedroom while the bath is entered
  separately). The canonical good order is bed → closet → bath, or bed → bath with the closet
  off the bath.
- Related existing: `MASTER_ENSUITE`, `BATH_OVERSIZE`, `CLOSET_DEPTH`, `CLOSET_SHAPE`.

#### P145 — Bulk Storage ★

**Idea.** A household needs a substantial volume of storage for things not in daily use —
Alexander suggests on the order of 15–20% of the floor area — and it must be somewhere people
will actually walk to.

**Why it matters.** barndsl already has `LOW_STORAGE` with `LOW_STORAGE_RATIO = 0.025` (2.5%),
which is a *closet* ratio, not a bulk-storage ratio. In a barndominium the shop absorbs bulk
storage, which is either an elegant answer or a trap (unconditioned, dusty, and you have to
cross the fire separation).

**Rule candidate.**
- `BULK_STORAGE` — **info**: conditioned storage (`storage` + `pantry` + non-bedroom `closet`)
  below 5% of conditioned residential area *and* no adjacent shop/garage storage zone declared.
  Keep the 2.5% `LOW_STORAGE` bar as the warning-level floor and add 5% as the comfort nudge.
- Do **not** count shop area as storage automatically — flag it as a deliberate accept.

#### P147 — Communal Eating / P182 — Eating Atmosphere

**Idea.** Shared meals need a table that is the acknowledged center of a bounded space with a
light over it — not an afterthought at the end of a room.

**Rule candidate.** `DINING_DEFINED` — **info**: no `dining` room and no dining zone declared
within a great room. barndsl already checks the furnishing envelope
(`DINING_FURNISH_CLEAR = 8.0`, `DINING_CLEARANCE`); the missing check is *existence and
boundedness* in open-plan layouts.

### 2.3 Room interiors

#### P159 — Light on Two Sides of Every Room ★★ (the highest-value new rule)

**Idea.** Rooms lit from two directions are the ones people choose; single-sided rooms feel
harsh (high contrast against the window wall) and are abandoned. Alexander's fallback for a
single-sided room is to make it shallow relative to the window wall.

**Why it matters.** This is the most cited, most testable, most consequential pattern in the
book, and barndsl currently measures only *quantity* of glazing (`NAT_LIGHT`, 8% of floor per
IRC R303), never its *distribution*. Two rooms with identical glazing area score identically
today even if one has all of it on one wall.

**Rule candidate.**
- `LIGHT_ONE_SIDE` — **info**: a habitable room has glazing on exactly one wall. Exempt
  `bathroom`, `half_bath`, `closet`, `pantry`, `laundry`, `utility`, `mechanical`, `storage`.
- `LIGHT_ONE_SIDE_DEEP` — **warning**: single-sided *and* the room's depth perpendicular to the
  window wall exceeds 2.0 × the window wall's length. (Alexander's shallow-room escape hatch,
  quantified. A 12 ft window wall permits a 24 ft depth; beyond that the far end is dark.)
- Score gradient: `two_sided_fraction` = habitable rooms with glazing on ≥ 2 walls / all
  habitable rooms. Full marks ≥ 0.7, full penalty ≤ 0.2. Suggested weight 3 pts within
  `livability`. Reflected-corner glazing (two windows on adjacent walls near a corner) should
  count as partial credit only — Alexander's effect wants separation.
- **Barndo caveat.** Interior bedrooms in a deep bar physically cannot get two exterior walls.
  Accept interior windows/transoms (P194) and clerestories as satisfying a *reduced* version:
  `LIGHT_BORROWED` — the room has one exterior wall plus an interior glazed opening onto a
  daylit space. Grant partial credit (0.5) in the gradient.

#### P179 — Alcoves ★★ / P188 — Bed Alcove / P202 — Built-In Seats / P203 — Child Caves

**Idea.** A large room needs small recessed sub-spaces at its edges — Alexander's figure is
about 6 ft wide and 3–6 ft deep, with a ceiling noticeably lower than the main room — so that
one or two people can be semi-apart while remaining in the group.

**Why it matters.** This is the mechanism by which a big open barndo volume becomes habitable.
It is also the direct ancestor of Susanka's "shelter around activity."

**Rule candidate.**
- `ALCOVE_ABSENT` — **info**: a public room over 400 sf with no alcove-like feature. Define an
  alcove geometrically as a bounded recess 4–8 ft wide × 3–7 ft deep off a larger room, or a
  declared window seat, or a zone with ceiling ≥ 1.5 ft lower than the parent room.
- `ALCOVE_SHAPE` — **info**: a declared alcove deeper than it is wide, or deeper than 7 ft
  (past that it stops reading as part of the parent room and should be a room).

#### P180 — Window Place ★★

**Idea.** In any room where people spend time, at least one window should be a *place* — a
bay, a seat, a widened sill, a chair-sized zone in the light — not just a hole in the wall.
When a room's only comfortable seat and its only window pull in different directions, the room
is uncomfortable in a way people can't name.

**Rule candidate.**
- `WINDOW_PLACE` — **info**: no public habitable room contains a window-adjacent zone at least
  3 ft deep × 5 ft wide clear of circulation, or a declared window seat / bay. Fire once per
  plan, not per room.
- `WINDOW_SEAT_SILL` — **info**: a declared window seat whose window sill exceeds 30 in
  (2.5 ft) above the seat's floor — you cannot see out while seated.

#### P181 — The Fire

**Idea.** A hearth is a natural focus for a gathering space; something must anchor the seating
group's attention.

**Rule candidate.** `PUBLIC_FOCUS` — **info**: a public room over 300 sf with no anchoring
element on any wall (fireplace/hearth, media wall, a window group ≥ 8 ft wide, or a declared
focal element). Susanka calls the same idea "point of focus." Low-confidence; INFO only, and
skip entirely if barndsl has no element vocabulary for hearths (check `elements.py`).

#### P183 — Workspace Enclosure ★★

**Idea.** A workspace needs to be enclosed on about half to three-quarters of its perimeter,
with the worker's back to a wall and a view out — neither exposed in the open nor sealed in a
box. Alexander adds that the workspace wants a view of at least ~20 ft.

**Why it matters.** Directly applicable to the `office` type, and to the home-office demand
that drives a lot of barndominium programs.

**Rule candidate.**
- `OFFICE_ENCLOSURE` — **info**: an `office` whose enclosed perimeter fraction (solid wall
  length / total perimeter, counting openings and glazing as unenclosed) falls outside
  0.5–0.85.
- `OFFICE_VIEW` — **info**: an `office` with no window, or with a longest interior sightline
  under 10 ft (nowhere for the eye to rest).
- Related existing: `OFFICE_CLEARANCE` (`DESK_WIDTH`/`DESK_DEPTH`/`DESK_CHAIR_PULL`).

#### P184 — Cooking Layout ★★ (hard numbers, directly checkable)

**Idea.** The four kitchen elements — stove, sink, refrigerator, counter — must be close enough
to work as one station. Alexander's three explicit rules:

1. No two of the four are more than **10 ft** apart.
2. Total counter length, *excluding* the sink, stove, and refrigerator, is at least **12 ft**.
3. No single counter section is shorter than **4 ft**.

**Why it matters.** These predate and roughly agree with the NKBA work-triangle guidelines
(§4.4) and are simpler to check. barndsl already has counter and fixture elements
(`fixtures.py`, `COUNTER_*` diagnostics), so all three are computable today.

**Rule candidate.**
- `COOK_SPREAD` — **warning**: any pair among {sink, range/cooktop, refrigerator} more than
  10 ft apart, measured center-to-center.
- `COUNTER_TOTAL` — **info**: usable counter run (excluding fixture footprints) under 12 ft.
  Escalate to **warning** under 8 ft.
- `COUNTER_SEGMENT` — **info**: any counter segment shorter than 4 ft that is not a designated
  landing zone beside an appliance.
- Related existing: `KITCHEN_FIT`, `KITCHEN_FLOW`, `COUNTER_DOOR`, `COUNTER_ROOM`,
  `RECEPTACLE_COUNTER`.

#### P190 — Ceiling Height Variety ★★

**Idea.** A building where every ceiling is the same height cannot feel comfortable. Ceiling
height should track the social size of the space: roughly **10–12 ft** for large gathering
rooms, **7–9 ft** for small-group rooms, **6–7 ft** for one- or two-person spaces (alcoves,
nooks — Alexander's low figures are lower than modern code allows for habitable rooms, so
treat 7 ft as the practical floor and reserve sub-7 ft for non-habitable alcoves).

**Why it matters.** This and Susanka's version of it are the highest-leverage moves in a
barndominium, where the shell offers 14–20 ft of clear height for free. Flat-lidding the whole
thing at 9 ft wastes the building's one gift; leaving it all at 16 ft produces a hangar.

**Rule candidate.**
- `CEILING_UNIFORM` — **info**: all rooms on a level share the same ceiling height (within
  0.5 ft) and the plan's conditioned area exceeds 1,200 sf. Suggest raising the great room or
  dropping the entry/hall/alcove.
- `CEILING_SCALE` — **info**: a public room over 400 sf with ceiling below 9 ft (under-scaled),
  or a room under 120 sf with ceiling above 12 ft (a shaft).
- `CEILING_RATIO` — **info**: ceiling height exceeds 0.6 × the room's short dimension (a
  12 ft × 12 ft room with a 16 ft ceiling reads as a well, not a room). Rough proportional
  guide, not a hard rule.
- Score gradient: `ceiling_variety` = number of distinct ceiling heights (bucketed to 0.5 ft)
  among habitable rooms. Full marks at ≥ 3 distinct heights, zero at 1. Suggested weight 2 pts.
- Related existing: `CEILING`, `LOFT_CEILING`, `MIN_GREAT_ROOM_CEILING = 10.0`.

#### P191 — The Shape of Indoor Space

**Idea.** Rooms should be roughly convex, with walls that are mostly straight and corners that
are mostly square. Leftover, wedge-shaped, and deeply re-entrant rooms are unusable at the
edges.

**Rule candidate.** Largely covered by barndsl's rectangle-only room model plus
`ROOM_PROPORTION` / `MAX_ROOM_ASPECT_BY_TYPE`. Worth adding for composed/L-shaped plans:
- `ROOM_CONVEXITY` — **info**: a non-rectangular room's area is less than 0.75 × its convex
  hull area.

#### P193 / P194 — Half-Open Wall, Interior Windows

**Idea.** Two spaces can be separated without being severed — by a partial wall, a
counter-height divider, a wide cased opening, or a glazed interior opening. Interior windows
also carry daylight into rooms with no exterior wall.

**Why it matters.** *The* answer to the deep barndominium bar. An interior room with a transom
or a glazed wall onto the great room gets borrowed light and connection without losing
enclosure.

**Rule candidate.**
- `INTERIOR_LIGHT` — **info**: a habitable room with no exterior wall and no interior glazed
  opening. (Today such a room only trips `ROOM_NO_LIGHT`; the *remedy* deserves naming.)
- `OPENING_BINARY` — **info**: all openings between public rooms are either standard doors or
  fully-open boundaries, with no intermediate (cased opening, half wall, pass-through). Very
  low confidence — a nudge, not a finding.
- Related existing: `WINDOW_INTERIOR`, `ROOM_NO_LIGHT`, `KITCHEN_PASSTHROUGH`.

#### P196 — Corner Doors

**Idea.** Doors belong near the corners of rooms, not centered on walls. A centered door splits
the wall into two unusable halves and drives circulation through the middle of the room.

**Rule candidate.** Already implemented as `DOOR_CENTERED` with `DOOR_CORNER_MARGIN = 2.0`.
Worth adding the consequence:
- `ROOM_CROSS_TRAFFIC` — **info**: two doors in a room positioned such that the straight line
  between them passes through the room's central third, where the room is a public/seating room
  larger than 180 sf. The through-route destroys the seating zone.

#### P197 — Thick Walls / P198 — Closets Between Rooms ★★ / P200 — Open Shelves

**Idea.** Walls should have depth — storage, niches, built-ins — and closets are the ideal
material for it, because a closet wall between two rooms is both storage *and* an acoustic
buffer.

**Why it matters.** This gives barndsl a *constructive* answer to `BED_SOUND` instead of just a
complaint. Today `BED_SOUND` fires when two bedrooms share more than 4 ft of wall; P198 says
the fix is to slide the closets into the shared wall.

**Rule candidate.**
- `SOUND_BUFFER_MISSING` — **info**: two bedrooms (or a bedroom and a `rec_room`/`great_room`/
  `shop`) share more than 4 ft of wall with no closet, bathroom, or storage volume interposed.
  Should carry the P198 remedy in its message text.
- `BUFFER_CREDIT` — score credit: for each bedroom whose noisy-neighbor wall is buffered by a
  closet/bath, add credit. Turns `BED_SOUND` from a pure penalty into a gradient.
- Related existing: `BED_SOUND`, `MIN_SOUND_BUFFER_WALL = 4.0`, `REC_ROOM_NOISE`,
  `GARAGE_SEPARATION`.

#### P199 — Sunny Counter

**Idea.** Kitchen work surfaces should be in daylight, ideally with a view — the sink under a
window is the folk version of this pattern.

**Rule candidate.** `COUNTER_DAYLIGHT` — **info**: no counter run in the kitchen is within 3 ft
of a window, and no window is placed above a sink. Cheap to check with barndsl's existing
counter geometry.

#### P201 — Waist-High Shelf / P202 — Built-In Seats

**Idea.** Circulation spaces need a place to set things down; the house needs built-in seating
at its edges. Mostly below the resolution of a floor-plan DSL.

**Rule candidate.** Skip unless barndsl gains a built-ins vocabulary. Noted for completeness.

### 2.4 Pattern coverage summary

| Pattern | New code proposed | Sev | Already covered by |
|---|---|---|---|
| 107 Wings of Light | `DEPTH_NO_DAYLIGHT`, `WING_DEPTH` | info | `NAT_LIGHT`, `ROOM_NO_LIGHT` |
| 109 Long Thin House | (exemption + credit) | — | — |
| 110/112 Entrance | `ENTRY_TRANSITION`, `ENTRY_AXIS` | info | `ENTRY_INTERIOR`, `FOYER_*` |
| 113 Car Connection | `CAR_ENTRY_DROP`, `CAR_ENTRY_DISTANCE` | warn/info | `GARAGE_PASSTHROUGH` |
| 127 Intimacy Gradient | `GRADIENT_INVERSION`, `PRIVATE_FROM_ENTRY` | info/warn | `BED_PRIVACY`, `ZONE_CROSS` |
| 128/161/162 Sun | `SUN_DAY_ROOMS`, `SUN_SERVICE_NORTH`, `SUN_OUTDOOR_ROOM` | info | `SOLAR_*` |
| 129 Common Areas at Heart | `COMMON_BYPASSED`, `COMMON_SHARE` | info/warn | — |
| 130 Entrance Room | `FOYER_STORAGE` | info | `MIN_FOYER_AREA` |
| 131 Flow Through Rooms | `PUBLIC_FLOW` | info | `GREAT_ROOM_FLOW` |
| 132 Short Passages | `HALL_LONG`, `HALL_DARK` | info/warn | `HALL_DEADEND`, `HALL_TIGHT` |
| 134 Zen View | `VIEW_SATURATION` | info | `WINDOW_HEAVY` |
| 135 Light and Dark | `LIGHT_UNIFORM` (gradient) | — | — |
| 136/137/141 Realms | `SUITE_SEPARATION`, `ROOM_OF_ONES_OWN` | info | `BED_SOUND`, `MASTER_ENSUITE` |
| 138 Sleeping to East | `BED_EAST` (opt-in) | info | — |
| 139 Farmhouse Kitchen | `KITCHEN_EATING`, `KITCHEN_ISOLATED`, `KITCHEN_SIZE_SOCIAL` | info/warn | `KITCHEN_FLOW` |
| 142/185 Sitting | `SITTING_COUNT`, `SEATING_ZONE_UNBOUND` | info | `GREAT_ROOM_SCALE` |
| 144/189 Bath/Dressing | `SUITE_SEQUENCE` | info | `MASTER_ENSUITE` |
| 145 Bulk Storage | `BULK_STORAGE` | info | `LOW_STORAGE` |
| 147/182 Eating | `DINING_DEFINED` | info | `DINING_CLEARANCE` |
| 159 Light Two Sides | `LIGHT_ONE_SIDE`, `LIGHT_ONE_SIDE_DEEP`, `LIGHT_BORROWED` | info/warn | `NAT_LIGHT` (quantity only) |
| 163/167 Outdoor Room | `PORCH_ENCLOSURE`, `PORCH_ACCESS` | info | `MIN_PORCH_DEPTH` |
| 179/188 Alcoves | `ALCOVE_ABSENT`, `ALCOVE_SHAPE` | info | — |
| 180 Window Place | `WINDOW_PLACE`, `WINDOW_SEAT_SILL` | info | — |
| 181 The Fire | `PUBLIC_FOCUS` | info | — |
| 183 Workspace Enclosure | `OFFICE_ENCLOSURE`, `OFFICE_VIEW` | info | `OFFICE_CLEARANCE` |
| 184 Cooking Layout | `COOK_SPREAD`, `COUNTER_TOTAL`, `COUNTER_SEGMENT` | warn/info | `KITCHEN_FIT` |
| 190 Ceiling Variety | `CEILING_UNIFORM`, `CEILING_SCALE`, `CEILING_RATIO` | info | `CEILING`, `LOFT_CEILING` |
| 191 Shape of Indoor Space | `ROOM_CONVEXITY` | info | `ROOM_PROPORTION` |
| 193/194 Half-Open Wall | `INTERIOR_LIGHT` | info | `WINDOW_INTERIOR` |
| 196 Corner Doors | `ROOM_CROSS_TRAFFIC` | info | `DOOR_CENTERED` |
| 197/198 Thick Walls/Closets | `SOUND_BUFFER_MISSING` | info | `BED_SOUND` |
| 199 Sunny Counter | `COUNTER_DAYLIGHT` | info | — |

---

## 3. Susanka principles and rule candidates

Susanka's ideas overlap Alexander's deliberately — she cites him — but she reframes them for
the contemporary American house and adds several that are specifically about *open plans*,
which is exactly the barndominium condition.

### 3.1 Quality over quantity ("build not so big")

**Idea.** Reduce the footprint by roughly a third from what the program seems to demand, and
put the savings into articulation: better light, better proportions, built-ins, a real
ceiling. Every square foot should be used every day.

**Rule candidate.**
- `AREA_PER_OCCUPANT` — **info**: conditioned residential area divided by bedroom count exceeds
  ~700 sf/bedroom (a sprawl signal) or falls below ~350 sf/bedroom (cramped). These are soft
  bands; the point is to *notice* when a program is out of proportion to itself.
- `ROOM_UNUSED_PROGRAM` — **info**: the plan contains both a `living` and a `great_room`, or
  both a `dining` and a `rec_room`, while total public area exceeds 45% of conditioned area.
  Susanka's specific complaint: the formal living room and formal dining room nobody enters.
  In barndominium culture this is nearly always the right call — one great room, no parlor.
- Related existing: `PROGRAM_AREA_OVERRUN`, `PROGRAM_MISMATCH`, `AREA_UNUSED`.

### 3.2 Shelter around activity

**Idea.** Every activity zone wants a sense of enclosure over and around it — a lowered
ceiling, a soffit, a beam, a partial wall, a change of floor — while remaining visually open to
the larger space. This is Alexander's alcove idea generalized to the open plan.

**Rule candidate.**
- `ZONE_SHELTER` — **info**: an activity zone within a large open room (kitchen, dining, and
  seating zones of a `great_room` over 400 sf) with no sheltering device: no ceiling change,
  no soffit/beam, no wall return ≥ 3 ft, no island/peninsula, no level change.
- Score gradient: `sheltered_zone_fraction` across public zones. Full marks ≥ 0.6.
- **Implementation note.** This needs barndsl to have a *zone-within-room* concept for open
  plans. If it does not yet (check `spatial.py` / `views.py`), the prerequisite is a
  `zone` element inside `great_room` with its own ceiling height and boundary hints. That is a
  DSL feature request, not just a rule.

### 3.3 Ceiling height variety and the "height ratio" heuristic

**Idea.** Susanka's operational version of P190: vary ceiling heights deliberately, and use the
*contrast* to define rooms without walls. A low ceiling over a nook next to a high ceiling over
a gathering space makes both feel better than either alone.

**Rule candidate.** (See `CEILING_UNIFORM` / `CEILING_SCALE` / `CEILING_RATIO` above.) Add:
- `CEILING_CONTRAST` — score credit: any two directly adjacent open zones whose ceiling heights
  differ by ≥ 1.5 ft. +1 pt each, cap 2 pts.
- Practical barndominium targets: entry/alcove/nook 7.5–8 ft, bedrooms and secondary spaces
  8–9 ft, kitchen 8–9 ft (a low kitchen ceiling next to a high great room is the classic
  Susanka move), great room 11–16 ft, loft-adjacent volumes to the underside of the frame.

### 3.4 Diagonal views and interior views

**Idea.** The longest sightline in a room should be on the diagonal, and the eye should be able
to travel from one space into another. A room feels much larger than its footprint if it
borrows depth from an adjacent space.

**Why it matters.** Highly measurable and rarely checked by any tool. It's also the reason a
20 ft × 20 ft room with a corner opening feels bigger than a 22 ft × 22 ft room with a
mid-wall door.

**Rule candidate.**
- `DIAGONAL_VIEW` — **info**: the longest unobstructed interior sightline from the geometric
  center of each public room is shorter than 1.3 × the room's own diagonal. Meeting the bar
  means the room borrows depth from a neighbor.
- `PLAN_LONGEST_VIEW` — score gradient: the longest sightline anywhere in the plan, normalized
  by the plan's own diagonal. Full marks at ≥ 0.5 of the plan diagonal; zero at ≤ 0.2. Cap the
  reward — an unbroken 60 ft view is a warehouse, not a home (this is P134's counterweight).
  Suggested weight 2 pts.
- **Implementation note.** Needs a ray/visibility trace over the wall set. barndsl has enough
  geometry (`geometry.py`, `spatial.py`) to do a 2D segment-intersection visibility test; the
  cost is O(rooms × walls) per sample point, which is fine at residential scale.

### 3.5 Light on two sides / light to walk toward / reflecting surfaces

**Idea.** Susanka restates P159 and adds two corollaries: put a light source at the end of
every corridor so you walk toward brightness, and place light-colored surfaces where daylight
can bounce off them (a splayed reveal, a return wall next to a window).

**Rule candidate.** `LIGHT_ONE_SIDE` and `HALL_DARK` above cover the first two. The third is a
finish concern and out of scope for a floor-plan DSL. Add one corridor-specific check:
- `HALL_TERMINUS_DARK` — **info**: a hallway ≥ 12 ft long whose far terminus is a blank wall or
  a closed door rather than a window, glazed door, or open room. Distinct from
  `HALL_DEADEND` (which is about wasted length past the last served door); this is about what
  you *see* while walking.

### 3.6 The away room

**Idea.** An open plan needs one closable room adjoining the common space — a den, a study, a
TV room — that gives acoustic privacy without exile. Susanka's core critique of great rooms is
that when everything is open, two people cannot do two different things.

**Why it matters.** This is the highest-value single rule to add for barndominiums, because the
entire building type is one big open volume. The away room is the pressure valve.

**Rule candidate.**
- `AWAY_ROOM` — **info** (escalate to **warning** in a plan with ≥ 3 bedrooms and a great room
  over 500 sf): no room that satisfies all of — (a) 100–250 sf, (b) type in
  {`office`, `flex`, `rec_room`, `loft`, `living` when a `great_room` also exists},
  (c) closable with a conventional door, (d) directly adjacent to or within one room of the
  public core, (e) *not* a bedroom.
- `AWAY_ROOM_ISOLATED` — **info**: a candidate away room exists but is more than 2 doors from
  the public core, or is only reachable through the private bedroom zone. Then it is not an
  away room, it is an office in the bedroom wing.
- Related existing: `FLEX_FUTURE_BED`, `REC_ROOM_SCALE`, `REC_ROOM_NOISE`.

### 3.7 Spatial layering, framed openings, and "differentiation of parts"

**Idea.** Instead of one boundary between inside and outside (or between room and room), build
several in series: porch, then entry, then a framed opening, then the room. Each layer makes
the destination feel more arrived-at. "Framed openings" — a cased opening with a real header
and jamb depth — do the same job at room scale.

**Rule candidate.**
- `ENTRY_LAYERS` — score credit: count the layers on the primary entry sequence (covered porch
  ≥ 6 ft deep = 1, foyer/entry room = 1, framed/cased opening or a partial wall into the public
  room = 1). Full marks at ≥ 2, zero at 0. +2 pts.
- `OPENING_UNDIFFERENTIATED` — **info**: an opening wider than 10 ft between two named rooms
  with no declared header/cased treatment. At that width the "rooms" are one room; either
  narrow the opening or merge the rooms in the model so the area accounting is honest.
- Related existing: `MAX_SINGLE_SWING_DOOR_WIDTH = 3.0`, `DOOR_WIDE_SWING`.

### 3.8 Double duty and openability

**Idea.** Spaces and furniture should serve more than one purpose (guest room = office; island
= prep + eating + storage), and openings should be operable so the house can be reconfigured
between open and closed.

**Rule candidate.**
- `DOUBLE_DUTY_GUEST` — **info**: a plan with a bedroom used only for occasional guests (a
  bedroom with no closet, or one flagged `guest`) that has no secondary function declared.
  Suggest `flex` typing plus a closet — barndsl has `FLEX_FUTURE_BED` for the inverse case.
- `POCKET_OPPORTUNITY` — **info**: a swing door whose swing arc conflicts with a fixture or
  another swing (`DOOR_SWING_CLASH`, `DOOR_HITS_FIXTURE`) in a room under 60 sf — suggest a
  pocket or barn door rather than relocating. A remedy hint attached to existing checks, not a
  new failure.

### 3.9 Point of entry / process of entering

**Idea.** Entering should be a sequence with a pause, not a single step. Susanka is emphatic
that the transition needs somewhere to *stop*: set down keys, take off boots, greet.

**Rule candidate.** Covered by `ENTRY_TRANSITION` and `FOYER_STORAGE`. Add the barndo-specific
version in §5 (`MUDROOM_DROP_ZONE`).

### 3.10 Susanka coverage summary

| Principle | New code | Sev |
|---|---|---|
| Not so big / quality over quantity | `AREA_PER_OCCUPANT`, `ROOM_UNUSED_PROGRAM` | info |
| Shelter around activity | `ZONE_SHELTER` | info |
| Ceiling height variety | `CEILING_UNIFORM`, `CEILING_SCALE`, `CEILING_CONTRAST` | info / credit |
| Interior & diagonal views | `DIAGONAL_VIEW`, `PLAN_LONGEST_VIEW` | info / gradient |
| Light to walk toward | `HALL_TERMINUS_DARK` | info |
| Away room | `AWAY_ROOM`, `AWAY_ROOM_ISOLATED` | info / warn |
| Layering & framed openings | `ENTRY_LAYERS`, `OPENING_UNDIFFERENTIATED` | credit / info |
| Double duty | `DOUBLE_DUTY_GUEST` | info |
| Point of focus | `PUBLIC_FOCUS` | info |

---

## 4. Hard numbers

Everything below is either code (IRC), a published planning guideline (NKBA), or the typical
range reported by the standard dimensional references. **Code numbers are the ERROR line;
guideline "code column" values are the WARNING line; guideline "recommended" values and
typical ranges are the INFO line.**

### 4.1 IRC habitability floor (2018/2021 IRC, Chapter 3)

| Item | Requirement | Section |
|---|---|---|
| Habitable room area | ≥ 70 sf (kitchens exempt) | R304.1 / R304.2 |
| Habitable room horizontal dimension | ≥ 7 ft (kitchens exempt) | R304.2 |
| Ceiling height, habitable rooms/halls/baths | ≥ 7 ft 0 in | R305.1 |
| Ceiling height, bath/toilet/laundry at fixtures | ≥ 6 ft 8 in permitted | R305.1 exc. |
| Ceiling height under beams ≤ 4 ft o.c. | ≥ 6 ft 6 in (2021) | R305.1.1 |
| Sloped ceiling | ≥ 50% of required floor area at ≥ 7 ft; area under 5 ft not counted | R305.1 |
| Natural light (glazed area) | ≥ 8% of floor area of habitable room | R303.1 |
| Natural ventilation (openable) | ≥ 4% of floor area | R303.1 |
| Bathroom ventilation | window ≥ 3 sf (half openable) or mechanical 50 cfm intermittent / 20 cfm continuous | R303.3 |
| Emergency escape opening, net clear area | ≥ 5.7 sf (≥ 5.0 sf at grade-floor / below-grade with direct grade access) | R310.2.1 |
| Escape opening, net clear height | ≥ 24 in | R310.2.1 |
| Escape opening, net clear width | ≥ 20 in | R310.2.1 |
| Escape opening, sill height | ≤ 44 in above floor | R310.2.2 |
| Egress door (at least one) | ≥ 32 in clear width, ≥ 78 in clear height, side-hinged | R311.2 |
| Hallway width | ≥ 36 in | R311.6 |
| Stair width | ≥ 36 in above handrail height | R311.7.1 |
| Stair riser / tread | riser ≤ 7¾ in; tread ≥ 10 in | R311.7.5 |
| Stair headroom | ≥ 6 ft 8 in from tread nosings | R311.7.2 |
| Stair landing | ≥ 36 in in direction of travel | R311.7.6 |
| Handrail height | 34–38 in; may project ≤ 4½ in into required width | R311.7.8 |
| Guard height | ≥ 36 in where drop > 30 in; 4 in sphere rule | R312.1 |
| Water-closet clear width | ≥ 30 in (≥ 15 in from centerline to any wall/obstruction each side) | R307.1 |
| Water-closet clear space in front | ≥ 21 in | R307.1 |
| Lavatory clear space in front | ≥ 21 in | R307.1 |
| Shower minimum interior | ≥ 30 in × 30 in, ≥ 900 sq in, must contain a 30 in circle | R307.2 |
| Shower compartment ceiling / headroom | ≥ 6 ft 8 in above a 30×30 area at the showerhead | R307.2 |
| Window fall protection | operable window sill < 24 in above floor and > 72 in above exterior grade needs a device | R312.2 |
| Dwelling/garage separation | ½ in gypsum on the garage side; ⅝ in Type X under habitable rooms | R302.6 |
| Garage → dwelling opening | no door directly into a sleeping room; solid wood/steel ≥ 1⅜ in or 20-min rated, self-closing (2021) | R302.5.1 |

**barndsl status.** Most of the above is implemented. The named gaps worth checking:
sloped-ceiling area accounting, the 6 ft 8 in bath/laundry ceiling exception, and the guard
sphere rule.

### 4.2 Room sizes — minimum / functional / typical / generous

"Functional" = will hold the furniture with code-level clearances. "Typical" = what the
dimensional references report for contemporary US single-family construction. Areas are net
clear (inside face of finish).

| Room | Code min | Functional min | Typical | Generous | Notes |
|---|---|---|---|---|---|
| Secondary bedroom | 70 sf, 7 ft dim | 100 sf, 9 ft dim (10×10) | 120–150 sf (10×12, 11×13) | 170–200 sf | must hold a full/queen + dresser + walk-around |
| Primary bedroom | 70 sf | 150 sf, 11 ft dim | 200–260 sf (14×16) | 300+ sf | king (6.33×6.67) needs 12 ft clear wall |
| Guest bedroom | 70 sf | 100 sf | 110–130 sf | 150 sf | pairs with `flex` typing |
| Walk-in closet (primary) | — | 25 sf (5×5, single-sided) | 48–80 sf (6×8, 8×10) | 100–150 sf | 24 in hang depth + 36 in aisle → 5 ft min single-sided, 7 ft double-sided |
| Reach-in closet | — | 24 in deep × 4 ft wide | 24 in × 5–6 ft | 24 in × 8 ft | ~6 linear ft of hanging per adult |
| Full bathroom | ~35 sf (5×7) | 40–48 sf | 50–70 sf | 80–100 sf | 5 ft tub run + lav + WC |
| Primary bathroom | ~35 sf | 60 sf | 80–120 sf | 150–200 sf | double vanity needs 60–72 in run |
| Half bath / powder | ~18 sf (3×6) | 20 sf, 3 ft dim | 20–30 sf (5×5, 5×6) | 40 sf | 30 in WC clear width is the hard driver |
| Kitchen | exempt from R304 | 70 sf (galley) | 150–200 sf | 250–350 sf | see §4.4 |
| Pantry (reach-in) | — | 2 ft × 4 ft | 2 ft × 5–6 ft | — | 16–24 in shelf depth |
| Pantry (walk-in) | — | 25 sf (5×5) | 35–50 sf | 60–80 sf | 36 in aisle + 16–24 in shelves each side → 5.5–7 ft width |
| Dining room | 70 sf | 100 sf, 9 ft dim | 150–200 sf (12×14) | 240–300 sf | see §4.5 |
| Living room | 70 sf | 180 sf, 11 ft dim | 250–350 sf (15×20) | 400+ sf | |
| Great room (combined) | — | 350 sf | 450–700 sf | 900+ sf | barndsl `MIN_GREAT_ROOM_AREA = 200` is low; 350 is a better functional floor for kitchen+dining+living |
| Home office | 70 sf, 7 ft | 80 sf (8×10) | 100–140 sf | 180 sf | desk 4×2 + 3 ft chair pull |
| Laundry (closet) | — | 3 ft × 5 ft (side-by-side) or 3×3 (stacked) | — | — | needs 36 in clear in front |
| Laundry (room) | — | 35 sf (5×7) | 42–60 sf (6×7, 6×10) | 80–120 sf | + folding counter, sink, hanging rod |
| Mudroom | — | 35 sf, 5 ft width | 49–70 sf (7×7, 7×10) | 100–150 sf | bench 18–20 in deep, lockers 15–24 in, 36–42 in aisle |
| Foyer / entry | — | 25 sf, 4 ft dim | 40–60 sf (6×8) | 80–100 sf | |
| Mechanical / utility | — | 30 sf, 5 ft dim | 40–60 sf | 80 sf | 30 in service clearance at equipment faces |
| Safe room | — | 24 sf, 3 ft dim | 40–60 sf | — | ~7 sf/person is the common storm-shelter rule of thumb |
| Hallway | 36 in wide | 36 in | 42 in | 48 in | 48 in enables passing and accessible use |
| Covered porch | — | 6 ft deep | 8 ft deep | 10–12 ft deep | 6 ft is the seated-plus-passing minimum |
| Garage, 1 car | — | 12 × 20 | 14 × 22 | 16 × 24 | |
| Garage, 2 car | — | 20 × 20 | 22–24 × 22–24 | 26 × 28 | |
| Garage, 3 car | — | 32 × 22 | 36 × 24 | 36 × 28 | |
| Shop bay (barndo) | — | 12 ft short side | 24–30 × 30–40 | 40 × 60 | see §5 |
| RV bay | — | 14 × 35 | 16 × 40–45 | 16 × 50 | 14 ft door height minimum |

**Cross-check against current barndsl constants.** `MIN_USABLE_AREA` has kitchen 70 /
bathroom 48 / half_bath 30, `MIN_ROOM_SHORT_SIDE` bathroom 6 / half_bath 5,
`MIN_GREAT_ROOM_AREA = 200`, `MIN_REC_ROOM_AREA = 120`, `MIN_FOYER_AREA = 25`,
`MIN_MECH_AREA = 30`, `MIN_SAFE_ROOM_AREA = 24`, `MIN_MUDROOM_WIDTH = 5`. These are all
defensible *warning-level* floors and agree with the "functional min" column. The gaps are the
**comfort tier**: there is currently no INFO that fires when a room clears the functional floor
but sits well below the typical range. Proposal: `ROOM_BELOW_TYPICAL` — **info** when a room's
area is below 70% of the typical value for its type, message naming the typical range.

### 4.3 Furniture and clearance envelopes

| Item | Dimension | Clearance needed |
|---|---|---|
| Twin bed | 38 × 75 in | 24 in one side + foot |
| Full bed | 54 × 75 in | 24 in both sides preferred |
| Queen bed | 60 × 80 in (5 × 6.67 ft) | 24 in min / 30 in preferred each side + 36 in at foot |
| King bed | 76 × 80 in (6.33 × 6.67 ft) | same; needs ≥ 12 ft clear wall run |
| Nightstand | 20–24 in square | included in the bed-side clearance |
| Dresser | 18–20 × 54–72 in | 36 in in front to open drawers |
| Bedroom minimum clear envelope (queen) | — | 7 ft (bed + one 24 in walk-around) × 6.67 ft — matches barndsl `BED_FURNISH_LONG/SHORT` |
| Dining table, 4 seats | 36 × 48 in | 36 in min / 42–48 in preferred all around |
| Dining table, 6 seats | 40 × 72 in | as above → ~10 × 13 ft clear zone |
| Dining table, 8 seats | 42 × 96 in | as above → ~10 × 15 ft clear zone |
| Chair pull-back only | — | 36 in from table edge |
| Chair pull + walk-behind | — | 44–48 in from table edge |
| Sofa (3-seat) | 84–96 × 36 in | 30–36 in circulation behind/around |
| Sofa to coffee table | — | 14–18 in |
| Coffee table | 48 × 24 in | — |
| TV viewing distance | — | 1.5–2.5 × screen diagonal |
| Seating group diameter | — | conversation works to ~10 ft face-to-face; beyond that it splits |
| Desk | 48–60 × 24–30 in | 36 in chair pull behind |
| Washer / dryer (each) | 27–29 in wide × 32–34 in deep | 36–42 in clear in front |
| Stacked laundry | 27–29 in wide | 36 in clear in front |
| Mudroom bench | 18–20 in deep, 17–18 in seat height | 36–42 in clear aisle |
| Mudroom locker/cubby | 15–24 in deep, 12–18 in wide per person | as above |
| Refrigerator | 36 in wide × 30–36 in deep | 36–42 in clear in front (door swing + pull) |
| Range | 30 in wide (36/48 pro) | 42 in min work aisle |
| Dishwasher | 24 in wide | 21 in standing space beside; ≤ 36 in to sink |
| General walkway (residential) | — | 36 in; below 30 in reads as pinched |
| Two-person passing | — | 42–48 in |

### 4.4 Kitchen planning (NKBA guidelines + P184)

| Item | Code column | Recommended |
|---|---|---|
| Entry door width | 32 in clear | — |
| Work aisle, one cook | 42 in | — |
| Work aisle, two cooks | — | 48 in |
| Walkway (no work) | 36 in | — |
| Work triangle, total of 3 legs | — | ≤ 26 ft |
| Work triangle, each leg | — | 4–9 ft |
| Triangle leg intersecting an island | — | ≤ 12 in of intrusion |
| Distance between any two of sink/range/fridge (Alexander P184) | — | ≤ 10 ft |
| Total counter frontage | — | ≥ 158 in (13.2 ft) of usable frontage |
| Usable counter excluding fixtures (P184) | — | ≥ 12 ft |
| Minimum single counter section (P184) | — | ≥ 4 ft |
| Landing beside refrigerator | 15 in on handle side | — |
| Landing beside cooking surface | 12 in one side / 15 in other | 9 in behind, for an island cooktop |
| Landing at sink | 24 in one side / 18 in other | — |
| Landing at oven | 15 in | — |
| Landing at microwave | 15 in | microwave bottom ≤ 54 in AFF |
| Clearance above unprotected cooking surface | 30 in | 24 in if protected |
| Dishwasher to nearest sink edge | — | ≤ 36 in |
| Standing space beside dishwasher | — | ≥ 21 in |
| Seating counter, 30 in high | 30 in wide × 19 in deep per seat | — |
| Seating counter, 36 in high | 24 in wide × 15 in deep per seat | — |
| Seating counter, 42 in high | 24 in wide × 12 in deep per seat | — |
| Clear floor behind seated diner, no traffic | 32 in | — |
| … with traffic edging past | — | 36 in |
| … with traffic walking behind | — | 44 in |
| Range hood exhaust | — | ≥ 150 cfm |
| No cooking surface below an operable window | (safety rule) | — |

**Rule candidates.** `KITCHEN_TRIANGLE` — **info** when total > 26 ft or any leg outside
4–9 ft (skip entirely if the kitchen has more than 3 work centers or is a single-wall galley,
where the triangle degenerates). `KITCHEN_AISLE` — **warning** below 42 in, **info** below 48 in
when two openings serve the kitchen (two-cook signal). `KITCHEN_LANDING` — **warning** when an
appliance has no landing meeting the table above. `ISLAND_CLEARANCE` — **warning** when an
island's clearance to any parallel counter is < 42 in, **info** below 48 in.

### 4.5 Bath planning (NKBA guidelines + IRC R307)

| Item | Code column | Recommended |
|---|---|---|
| Door clear opening | 32 in | — |
| Clear floor at lavatory | 21 in | 30 in |
| Clear floor at water closet | 21 in | 30 in |
| Clear floor at tub | 21 in | 30 in |
| Clear floor at shower | 24 in | 30 in |
| WC centerline to sidewall/obstruction | 15 in | 18 in |
| Lavatory centerline to sidewall | 15 in | 20 in |
| Between two lavatory centerlines | 30 in | 36 in |
| Ceiling height | 80 in over fixture and its clear floor area | — |
| Shower interior | 30 × 30 in (900 sq in, 30 in circle) | 36 × 36 in |
| Shower door swing | must not hit a fixture; opens outward | — |
| Standard alcove tub | 60 × 30–32 in | 60 × 36 in |
| Vanity height | 32–34 in (32 in traditional) | 36 in for adults |
| Grab bar reinforcement | — | blocking at all tub/shower walls |
| Towel bar within reach of tub/shower | — | reachable from inside, ≤ 30 in |

**Rule candidates.** barndsl's `BATH_CLEARANCE` covers fixture fit; add the *recommended* tier
as INFO: `BATH_COMFORT` — **info** when any fixture's clear floor is between the 21 in code
value and the 30 in recommendation. Add `LAV_SPACING` — **info** for double vanities under
30 in centerline spacing.

### 4.6 Adjacency matrix

Legend: **R** = required (error/warning if absent), **P** = strongly preferred (info if absent),
**p** = mildly preferred, **·** = neutral, **a** = avoid (info if present), **A** = strongly
avoid (warning), **X** = prohibited (error / code).

"Adjacent" here means *direct connection*: a shared door or a shared open boundary. Distances
are shortest walking path.

| | Kitchen | Dining | Living/Great | Foyer | Mudroom | Garage/Shop | Laundry | Pantry | Primary bed | Sec. bed | Full bath | Half bath | Office/Away | Mech/Util | Porch |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Kitchen** | — | **R** | **P** | · | p | a | p | **R** | a | a | · | a | · | · | p |
| **Dining** | **R** | — | **P** | · | · | A | · | p | · | · | · | p | · | · | **P** |
| **Living/Great** | **P** | **P** | — | **P** | · | A | · | · | a | a | · | p | **P** | a | **P** |
| **Foyer** | · | · | **P** | — | p | · | · | · | a | a | · | **P** | p | · | **P** |
| **Mudroom** | p | · | · | p | — | **R** | **P** | p | · | · | · | p | · | p | · |
| **Garage/Shop** | a | A | A | · | **R** | — | p | · | **A** | **A** | · | · | · | p | · |
| **Laundry** | p | · | · | · | **P** | p | — | · | p | **P** | · | · | · | p | · |
| **Pantry** | **R** | p | · | · | p | · | · | — | · | · | · | · | · | · | · |
| **Primary bed** | a | · | a | a | · | **A** | p | · | — | a | **R** (ensuite) | · | p | A | p |
| **Sec. bed** | a | · | a | a | · | **A** | **P** | · | a | — | **R** (≤ 1 room) | · | · | A | · |
| **Full bath** | · | · | · | · | · | · | · | · | **R** | **R** | — | · | · | · | · |
| **Half bath** | a | p | p | **P** | p | · | · | · | · | · | p | — | · | · | · |
| **Office/Away** | · | · | **P** | p | · | · | · | · | p | · | · | · | — | · | p |
| **Mech/Util** | · | · | a | · | p | p | p | · | A | A | · | · | · | — | · |
| **Porch** | p | **P** | **P** | **P** | · | · | · | · | p | · | · | · | p | · | — |

**Distance rules (path length, not straight line).**

| Pair | Threshold | Severity |
|---|---|---|
| Any bedroom → nearest full bath | ≤ 25 ft ideal; **info** > 35 ft; **warning** > 50 ft | existing `BATH_DISTANCE` |
| Any bedroom → full bath without crossing a public gathering zone | required | **info** if violated (`BED_PRIVACY` neighbor) |
| Garage/shop entry door → kitchen | ≤ 25 ft | **info** if exceeded |
| Primary entry → kitchen | ≤ 40 ft | **info** if exceeded |
| Laundry → farthest bedroom | ≤ 40 ft | **info** if exceeded |
| Half bath → public core | ≤ 25 ft and not visible from dining seating | **info** |
| Primary bed → secondary beds | ≥ 20 ft or separated by a public room | **info** if closer (split plan) |
| Any habitable room → an exterior wall | ≤ 25 ft | **info** (P107) |

**Hard prohibitions (error tier).**
- A door opening directly from a `garage`/`shop` into a `bedroom` (IRC R302.5.1) — barndsl
  has `GARAGE_BEDROOM`.
- A bedroom whose only route to an exterior exit crosses a `garage`/`shop` — `GARAGE_PASSTHROUGH`
  plus the `topology` score term.
- A bathroom or bedroom serving as the only passage to another habitable room —
  `PRIVATE_PASSTHROUGH`.

---

## 5. Mapping onto barndominium living quarters

The barndominium is a specific building type with its own failure modes. Everything above has
to be filtered through five facts about it.

### 5.1 Fact 1 — One rectangular shell, so the residential wing is deep

A pole barn or steel building comes in standard widths (30, 40, 50, 60 ft) with clear span and
no interior bearing walls. The residential portion inherits that depth. Consequences:

- **P107 bites hardest here.** A 40 ft deep house bar can just barely give every room an
  exterior wall in a single-loaded arrangement (rooms ~14 ft deep + a 4 ft hall + rooms
  ~14 ft deep = 32 ft, leaving slack). At 50–60 ft, interior rooms are unavoidable.
- **Rule.** `WING_DEPTH` (§2.1) plus a positive credit for double-loaded arrangements where
  the hall runs the long axis. Interior rooms should be *chosen* — pantry, laundry, closets,
  mechanical, safe room, media/away room — never bedrooms.
- **Rule.** `INTERIOR_BEDROOM` — **warning**: a `bedroom` with no exterior wall. This is
  already effectively an error via `BEDROOM_EGRESS` (no escape opening is possible), but the
  *diagnosis* deserves its own message so the fix ("swap it with the pantry") is obvious.

### 5.2 Fact 2 — The shop is the real front door, and it is a hazard

The shop/garage bay is loud, dusty, cold, and full of combustion sources. IRC treats it as a
garage (barndsl's `GARAGE_TYPES` already folds `shop` in with `garage`, which is correct).

- **Transition sequence.** The canonical good barndo sequence is:
  `shop → [rated door] → mudroom → laundry (optional) → hallway or kitchen`.
  A shop that opens straight into the great room is the type's signature mistake.
- **Rule.** `SHOP_TRANSITION` — **warning**: no `mudroom` (or `foyer`/`laundry` acting as one)
  between the shop/garage and any public habitable room.
- **Rule.** `MUDROOM_DROP_ZONE` — **info**: a `mudroom` under 35 sf, or narrower than 5 ft
  (existing `MIN_MUDROOM_WIDTH`), or without an adjacent `closet`/storage or a declared bench.
  The mudroom is the highest-traffic room in a working barndominium and is routinely
  under-sized.
- **Rule.** `SHOP_BEDROOM_WALL` — **warning**: a `bedroom` sharing more than 4 ft of wall with
  `shop`/`garage` with no buffered volume (closet, bath, storage, hallway) between. Stricter
  than `BED_SOUND`'s bedroom-to-bedroom case, because a compressor at 6 a.m. is not a
  conversation through a wall. Extends `GARAGE_SEPARATION` (which is about fire, not sound).
- **Rule.** `SHOP_ROOM_ABOVE` — **info**: a habitable room on an upper level directly over a
  shop bay without a declared sound/thermal assembly. Common in loft plans.
- **Rule.** `SHOP_ODOR_PATH` — **info**: the shop's connecting door is within 10 ft of a
  kitchen counter or a dining zone. Exhaust and solvent smell travel.

### 5.3 Fact 3 — Great-room culture is the default, and it needs discipline

Nearly every barndominium plan is one open kitchen/dining/living volume, often 500–900 sf,
frequently with a vaulted ceiling to the underside of the frame. Alexander (P139 Farmhouse
Kitchen) and Susanka (shelter around activity, ceiling variety, away room) are in violent
agreement about what saves it and what kills it.

**What kills a barndo great room:**
1. Uniform high ceiling over the whole volume — no shelter anywhere.
2. No away room, so two activities cannot coexist.
3. Furniture zones with no bounding device, so the middle is dead floor.
4. Kitchen with a single opening, cut off from the room it is supposed to anchor.
5. Acoustics — hard floors, metal shell, 16 ft ceiling, no soft boundaries.

**Rule bundle for `great_room` (fire as a set, one INFO each):**

| Code | Condition | Sev |
|---|---|---|
| `GREAT_ROOM_FLAT_LID` | uniform ceiling height across a `great_room` > 500 sf with no lowered zone anywhere | info |
| `GREAT_ROOM_NO_ZONES` | > 400 sf with fewer than 2 bounded sub-zones (island/peninsula, wall return ≥ 3 ft, ceiling change, level change) | info |
| `GREAT_ROOM_NO_AWAY` | > 500 sf and no away room in the plan (§3.6) | warning |
| `GREAT_ROOM_ACOUSTIC` | volume (area × mean ceiling) > 6,000 cf with no soft-boundary or ceiling-break declaration | info |
| `GREAT_ROOM_TOO_LONG` | aspect ratio > 2.5:1 — the "bowling alley" great room, common when it fills the shell width | info |
| existing `GREAT_ROOM_SCALE` / `GREAT_ROOM_FLOW` | — | — |

**Ceiling strategy that works (use as the suggestion text):** vault the seating/dining zone to
the frame (12–16 ft), drop the kitchen to 8–9 ft with a soffit, drop entry and hall to 8 ft,
keep bedrooms at 9 ft. That is P190 and Susanka's ceiling variety in one move and it costs
almost nothing in a building that already has the height.

### 5.4 Fact 4 — Single-story is the norm, which changes the circulation math

Most barndominiums are one story (sometimes with a shop loft). Consequences:

- **Circulation cannot be amortized.** In a two-story house the stair absorbs the vertical
  circulation and hall length is short on both floors. In a single-story bar, every bedroom
  added extends the corridor. Recommend tightening `score.py`'s circulation free band from 15%
  to 12% for single-level plans and keeping the 35% full-penalty point.
- **Corridor length is the failure mode.** `HALL_LONG` (§2.2) is more important here than in
  any other house type. The remedies to suggest: a double-loaded corridor, a bedroom cluster
  off a short spur rather than a long single-loaded run, or splitting bedrooms to two ends of
  the public core (which also serves P136/P137).
- **Aging in place is nearly free.** Single-story plus a big footprint means the accessible
  route is achievable at almost no cost. barndsl's `ACCESS_*` family is opt-in; consider making
  `ACCESS_SINGLE_FLOOR` a default INFO for one-story plans and raising the default interior
  door target to 32 in clear in the barndominium profile.

### 5.5 Fact 5 — Guest quarters, multigenerational wings, and shop offices are common

Barndominium programs frequently include: a guest suite over the shop, a mother-in-law wing, a
bunk room, or an office attached to the shop for a working business.

- **Rule.** `SUITE_INDEPENDENCE` — **info**: a declared guest/secondary suite (bedroom + bath,
  possibly + kitchenette) with no direct exterior door and no route to the public core that
  avoids the primary bedroom zone.
- **Rule.** `LOFT_ISOLATION` — **info**: a habitable `loft` over the shop whose only access is
  through the shop bay. Same defect as `GARAGE_PASSTHROUGH`, one level up; check the topology
  term covers it.
- **Rule.** `SHOP_OFFICE_SEPARATION` — **info**: an `office` typed inside the shop envelope with
  no door and no glazed partition — a desk in a shop is not an office (P183 Workspace
  Enclosure applied literally).
- **Bunk rooms.** A bunk room is a bedroom for code purposes: escape opening, smoke alarm,
  ceiling height, area per occupant. If barndsl ever adds an occupancy count, the planning
  rule of thumb is 50–60 sf per bed plus circulation.

### 5.6 Barndominium-specific numeric targets

| Item | Target | Note |
|---|---|---|
| Shell widths available | 30 / 40 / 50 / 60 ft | drives residential wing depth |
| Residential wing depth for single-loading | ≤ 32 ft | every room touches an exterior wall |
| Residential wing depth needing interior rooms | > 40 ft | plan interior rooms deliberately |
| Shop bay minimum short side | 12 ft | barndsl `MIN_SHOP_DEPTH` |
| Shop bay comfortable short side | ≥ 20 ft | barndsl `SHOP_COMFORT_DEPTH` |
| Shop bay typical | 24–40 ft × 30–60 ft | |
| Shop clear height (lift / RV) | 12–16 ft | 14 ft minimum for RV |
| Overhead door, shop | 12–16 ft wide × 12–14 ft tall | barndsl `STD_OVERHEAD_DOOR_*` |
| Overhead door, garage | 8–10 ft (single) / 16–18 ft (double) × 7–8 ft | |
| Great room (K+D+L combined) | 450–700 sf | |
| Great room vault | 12–16 ft to frame underside | |
| Kitchen ceiling under a vaulted great room | 8–9 ft | the Susanka contrast move |
| Mudroom (working barndo) | 60–100 sf | larger than a suburban mudroom |
| Front porch depth | 8 ft | 6 ft is the absolute minimum |
| Bedroom wing corridor length | ≤ 20 ft info / ≤ 35 ft warning | single-story penalty |
| Fire separation, shop→house | ½ in gyp min; ⅝ in Type X if habitable above | IRC R302.6 |

---

## 6. Consolidated proposed thresholds

Severity column: **E** = error, **W** = warning, **I** = info, **G** = score gradient only.
"New?" marks whether this is a new diagnostic or an extension of an existing one.

### 6.1 Legality tier (already largely implemented — listed for completeness)

| Code | Sev | Threshold | New? |
|---|---|---|---|
| `BEDROOM_AREA` | E | < 70 sf | existing |
| `BEDROOM_DIM` | E | any horizontal dim < 7 ft | existing |
| `BEDROOM_EGRESS` | E | no opening ≥ 5.7 sf net clear, ≥ 24 in h, ≥ 20 in w, sill ≤ 44 in | existing |
| `ROOM_HABITABLE` | W | non-bedroom habitable < 70 sf or < 7 ft (kitchen exempt) | existing |
| `CEILING` | E | < 7 ft habitable; < 6 ft 8 in at bath/laundry fixtures | existing (verify exception) |
| `NAT_LIGHT` | W | glazing < 8% of floor | existing |
| `VENT_AREA` | W | openable < 4% of floor | existing |
| `HALL_WIDTH` | E | < 36 in | existing |
| `EGRESS_DOOR` | E | no door ≥ 32 in clear / 78 in high | existing |
| `GARAGE_BEDROOM` | E | door from garage/shop into a bedroom | existing |
| `GARAGE_SEPARATION` | W | missing rated separation | existing |
| `BATH_CLEARANCE` | W | WC clear < 30 in wide or < 21 in front; lav < 21 in front; shower < 30×30 | existing |
| `CEILING_SLOPE_AREA` | W | < 50% of required floor area at ≥ 7 ft under a sloped ceiling | **new** |

### 6.2 Function tier (fixtures and furniture fit)

| Code | Sev | Threshold | New? |
|---|---|---|---|
| `COOK_SPREAD` | W | any two of sink/range/fridge > 10 ft apart | **new** (P184) |
| `KITCHEN_AISLE` | W / I | work aisle < 42 in / < 48 in with two cooks | **new** (NKBA) |
| `ISLAND_CLEARANCE` | W / I | island to parallel counter < 42 in / < 48 in | **new** (NKBA) |
| `KITCHEN_LANDING` | W | fridge < 15 in, sink < 24/18 in, cooktop < 12/15 in, oven/mw < 15 in | **new** (NKBA) |
| `COUNTER_TOTAL` | W / I | usable counter < 8 ft / < 12 ft | **new** (P184) |
| `COUNTER_SEGMENT` | I | any counter run < 4 ft that isn't a landing | **new** (P184) |
| `KITCHEN_ISOLATED` | W | kitchen < 120 sf with only conventional doors | **new** (P139) |
| `LAUNDRY_FIT` | W | < 36 in clear in front of machines | existing |
| `BED_CLEARANCE` | I → W | queen envelope 7 × 6.67 ft not clear | existing (consider W) |
| `DINING_CLEARANCE` | I | < 8 ft clear (4-seat) / < 10 × 13 ft (6-seat) | existing, extend by seat count |
| `MUDROOM_DROP_ZONE` | I | mudroom < 35 sf, or no adjacent storage | **new** |
| `SHOP_TRANSITION` | W | no mudroom/foyer between shop and a public room | **new** |
| `CAR_ENTRY_DROP` | W | garage door lands in living/dining/bedroom | extends `GARAGE_PASSTHROUGH` |
| `INTERIOR_BEDROOM` | W | bedroom with no exterior wall | **new** |
| `SHOP_BEDROOM_WALL` | W | > 4 ft bedroom–shop shared wall, unbuffered | **new** |

### 6.3 Comfort tier (typical-range and recommended values)

| Code | Sev | Threshold | New? |
|---|---|---|---|
| `ROOM_BELOW_TYPICAL` | I | room area < 70% of the §4.2 typical for its type | **new** |
| `BATH_COMFORT` | I | fixture clear floor between 21 in and 30 in | **new** (NKBA rec.) |
| `LAV_SPACING` | I | double-vanity centerlines < 30 in apart | **new** |
| `KITCHEN_TRIANGLE` | I | total > 26 ft, or a leg outside 4–9 ft | **new** (NKBA) |
| `KITCHEN_SIZE_SOCIAL` | I | kitchen < 120 sf with ≥ 3 bedrooms | **new** |
| `HALL_LONG` | I / W | continuous hall run > 20 ft / > 35 ft | **new** |
| `HALL_DARK` | I | hall run > 12 ft with no daylit terminus | **new** |
| `HALL_TERMINUS_DARK` | I | hall ≥ 12 ft ending on a blank wall or closed door | **new** |
| `BULK_STORAGE` | I | conditioned storage < 5% of residential area | extends `LOW_STORAGE` (2.5%) |
| `FOYER_STORAGE` | I | no closet within one door of the entry | **new** |
| `BATH_DISTANCE` | I / W | bedroom → full bath > 35 ft / > 50 ft | existing, add W tier |
| `CAR_ENTRY_DISTANCE` | I | garage door → kitchen > 25 ft | **new** |
| `LAUNDRY_DISTANCE` | I | laundry → farthest bedroom > 40 ft | **new** |
| `AREA_PER_OCCUPANT` | I | > 700 or < 350 sf per bedroom | **new** |

### 6.4 Delight tier (pattern language and Susanka)

| Code | Sev | Threshold | New? |
|---|---|---|---|
| `LIGHT_ONE_SIDE` | I | habitable room glazed on exactly one wall | **new** (P159) |
| `LIGHT_ONE_SIDE_DEEP` | W | single-sided and depth > 2.0 × window-wall length | **new** (P159) |
| `INTERIOR_LIGHT` | I | habitable room with no exterior wall and no interior glazing | **new** (P194) |
| `DEPTH_NO_DAYLIGHT` | I | habitable room centroid > 25 ft from an exterior wall | **new** (P107) |
| `GRADIENT_INVERSION` | I | a private room reached at lower door-depth than a public one | **new** (P127) |
| `PRIVATE_FROM_ENTRY` | W | bedroom/bath door on a clear sightline < 15 ft from the entry | **new** (P127) |
| `COMMON_BYPASSED` | I / W | bedroom route bypasses the public core / all bedrooms do | **new** (P129) |
| `COMMON_SHARE` | I | public area < 25% of conditioned area | **new** (P129) |
| `ENTRY_TRANSITION` | I | no porch ≥ 6 ft, foyer ≥ 25 sf, or bounded entry zone ≥ 30 sf | **new** (P112) |
| `ENTRY_AXIS` | I | entry forward view < 8 ft or > 40 ft | **new** (P112) |
| `CEILING_UNIFORM` | I | all ceilings within 0.5 ft on a level, plan > 1,200 sf | **new** (P190) |
| `CEILING_SCALE` | I | public room > 400 sf with ceiling < 9 ft; room < 120 sf with ceiling > 12 ft | **new** (P190) |
| `CEILING_RATIO` | I | ceiling > 0.6 × room short dimension | **new** (P190) |
| `AWAY_ROOM` | I / W | no closable 100–250 sf non-bedroom near the public core / plus great room > 500 sf | **new** (Susanka) |
| `AWAY_ROOM_ISOLATED` | I | candidate away room > 2 doors from public core | **new** (Susanka) |
| `ALCOVE_ABSENT` | I | public room > 400 sf with no alcove/nook/window seat | **new** (P179) |
| `WINDOW_PLACE` | I | no 3 ft × 5 ft window-adjacent clear zone in any public room | **new** (P180) |
| `SITTING_COUNT` | I | fewer than 3 distinct sitting places in the plan | **new** (P142) |
| `SEATING_ZONE_UNBOUND` | I | public room > 400 sf with no bounding device | **new** (P185) |
| `ZONE_SHELTER` | I | open-plan activity zone with no sheltering device | **new** (Susanka) |
| `DIAGONAL_VIEW` | I | public room's longest sightline < 1.3 × its own diagonal | **new** (Susanka) |
| `VIEW_SATURATION` | I | one wall > 40% glazed while daylight floor already met elsewhere | **new** (P134) |
| `KITCHEN_EATING` | I | no eating surface reachable from the kitchen without a door | **new** (P139) |
| `DINING_DEFINED` | I | no dining room and no bounded dining zone | **new** (P147) |
| `COUNTER_DAYLIGHT` | I | no counter run within 3 ft of a window | **new** (P199) |
| `SUITE_SEPARATION` | I | primary bedroom shares > 4 ft wall or < 10 ft door spacing with a secondary bedroom | **new** (P136) |
| `SUITE_SEQUENCE` | I | primary closet not on the bed→bath path | **new** (P189) |
| `SOUND_BUFFER_MISSING` | I | > 4 ft bedroom-to-noise shared wall with no interposed volume | extends `BED_SOUND` (P198) |
| `OFFICE_ENCLOSURE` | I | office enclosed perimeter fraction outside 0.5–0.85 | **new** (P183) |
| `OFFICE_VIEW` | I | office with no window or longest sightline < 10 ft | **new** (P183) |
| `PUBLIC_FLOW` | I | a public room with exactly one opening | **new** (P131) |
| `ROOM_CROSS_TRAFFIC` | I | door-to-door line crosses the central third of a > 180 sf seating room | **new** (P196) |
| `PORCH_ENCLOSURE` | I | porch bounded on only one side | **new** (P163) |
| `PORCH_ACCESS` | I | no porch reachable directly from a public room | **new** (P163) |
| `SUN_DAY_ROOMS` | I | no south glazing on day-use rooms with a south wall available | extends `SOLAR_SOUTH_UNUSED` |
| `SUN_SERVICE_NORTH` | I | service rooms occupy > 50% of the south wall | **new** (P162) |
| `GREAT_ROOM_FLAT_LID` | I | uniform ceiling over a > 500 sf great room | **new** |
| `GREAT_ROOM_NO_ZONES` | I | > 400 sf with < 2 bounded sub-zones | **new** |
| `GREAT_ROOM_NO_AWAY` | W | > 500 sf and no away room | **new** |
| `GREAT_ROOM_TOO_LONG` | I | great-room aspect > 2.5:1 | **new** |
| `ROOM_UNUSED_PROGRAM` | I | both formal and informal versions of the same room, public > 45% | **new** (Susanka) |
| `ROOM_OF_ONES_OWN` | I | ≥ 3 bedrooms and no office/flex/loft | **new** (P141) |
| `SUITE_INDEPENDENCE` | I | guest suite with no exterior door and no non-private route | **new** |
| `SHOP_OFFICE_SEPARATION` | I | office in the shop envelope with no enclosure | **new** (P183) |
| `SHOP_ODOR_PATH` | I | shop door within 10 ft of a kitchen counter or dining zone | **new** |
| `SHOP_ROOM_ABOVE` | I | habitable room over a shop bay with no declared assembly | **new** |
| `LOFT_ISOLATION` | I | habitable loft reachable only through the shop | **new** |
| `BED_EAST` | I | no bedroom with east glazing (opt-in profile only) | **new** (P138) |

### 6.5 Proposed score-component changes

`score.py` currently deducts from 100 via: errors (100 flat), warnings (8 each, cap 40), infos
(2 each, cap 20), space (10), circulation (6), proportion (8), daylight (8), topology (15).

**Problem.** Adding ~45 new INFO rules saturates the 20-point info cap almost immediately,
which destroys the gradient the agent hill-climbs on. A plan with 10 info nudges and one with
30 would score identically.

**Proposal.** Add a `livability` continuous component (suggested max 12 points) fed by margins,
not counts, and *exclude* the delight-tier infos from the counted-info cap (register them in a
separate `advisory` bucket that feeds only the gradient). Suggested internal weights:

| Sub-term | Metric | Max pts |
|---|---|---|
| `two_sided_light` | fraction of habitable rooms with glazing on ≥ 2 walls (borrowed = 0.5) — full marks ≥ 0.7 | 3 |
| `privacy_gradient` | Spearman ρ of privacy rank vs. door-depth — full marks ≥ 0.7 | 2 |
| `common_centrality` | fraction of entry×bedroom paths touching the public core — full marks 1.0 | 3 |
| `ceiling_variety` | distinct ceiling heights among habitable rooms — full marks ≥ 3 | 2 |
| `spatial_depth` | longest plan sightline / plan diagonal, rewarded in 0.2–0.5, flat above | 2 |

**Also propose:**
- Tighten `circulation`'s free band from 15% to 12% for single-level plans (§5.4).
- Raise `MIN_GREAT_ROOM_AREA` from 200 to 350 for the *comfort* tier only, keeping 200 as the
  warning floor, so combined kitchen/dining/living volumes are judged as such.
- Add a small positive credit set (capped, so the total still cannot exceed 100): entry
  layering ≥ 2, a buffered bedroom wall, a public circulation loop, an adjacent ceiling
  contrast ≥ 1.5 ft. Credits should offset penalties, never create score above 100.

### 6.6 Suggested implementation order

Ranked by (value to a barndominium plan) ÷ (implementation cost against barndsl's current model):

1. `LIGHT_ONE_SIDE` + `two_sided_light` gradient — pure wall/window bookkeeping, highest payoff.
2. `AWAY_ROOM` / `GREAT_ROOM_NO_AWAY` — room-type + area + adjacency; no new geometry needed.
3. `HALL_LONG` / `HALL_DARK` / `HALL_TERMINUS_DARK` — hallway runs are already traced for
   `HALL_DEADEND`.
4. `SHOP_TRANSITION` / `CAR_ENTRY_DROP` / `CAR_ENTRY_DISTANCE` — adjacency-graph work on top of
   the existing `GARAGE_PASSTHROUGH` machinery.
5. `GRADIENT_INVERSION` + `privacy_gradient` — needs a door-depth BFS from the entry, which the
   topology term already computes something close to.
6. `CEILING_UNIFORM` / `CEILING_SCALE` / `ceiling_variety` — ceiling heights are already
   modeled (`wallheights.py`, `CEILING`, `LOFT_CEILING`).
7. `COOK_SPREAD` / `COUNTER_TOTAL` / `KITCHEN_TRIANGLE` — needs fixture positions, which
   `fixtures.py` has.
8. `ROOM_BELOW_TYPICAL` — a lookup table; trivial, but tune carefully to avoid noise.
9. `DIAGONAL_VIEW` / `PLAN_LONGEST_VIEW` — needs a visibility trace; highest cost, real payoff.
10. `ZONE_SHELTER` / `SEATING_ZONE_UNBOUND` / `ALCOVE_ABSENT` — **blocked** on a DSL feature: a
    zone-within-room concept for open plans. Treat as a design proposal, not a rule to write
    today.

### 6.7 Calibration cautions

- **Do not ship all of §6.4 at once.** With ~45 new INFOs a clean plan would suddenly emit
  15–20 nudges and the agent would chase noise. Ship in the order above, in batches of 3–5, and
  re-run the example gallery each time (`examples/`, `docs/FIXTURE_CATALOG.md`).
- **Every delight-tier rule must be acceptable.** These are preferences; the `accept` pragma is
  the pressure valve. A rule that cannot be accepted without argument does not belong at INFO.
- **Watch the interaction with the agent loop.** `docs/AUTHORING.md` and the design-loop notes
  record that refine rounds can already break compiles. New rules that suggest *structural*
  moves (relocate a bedroom, add an away room) are far riskier for an automated fixer than ones
  that suggest *local* moves (add a window, widen an opening). Prefer local-remedy rules first
  and make the suggestion text name the specific local edit.
- **Thresholds in this document are proposals, not measurements.** Before committing any
  number, run it against the example gallery and check the false-positive rate. Alexander's and
  NKBA's numbers are well-established; the derived ones (25 ft daylight depth, 2.0× single-side
  depth ratio, 0.6 ceiling-to-short-dimension, ρ ≥ 0.7) are the author's syntheses and should be
  treated as starting points.

---

## Sources

- Alexander, C., Ishikawa, S., Silverstein, M. — *A Pattern Language: Towns, Buildings,
  Construction* (Oxford University Press, 1977). Pattern summaries cross-checked against
  [patternlanguage.cc](https://patternlanguage.cc/) —
  [Cooking Layout (184)](https://patternlanguage.cc/Patterns/Cooking-Layout-(184)),
  [Light on Two Sides (159)](https://patternlanguage.cc/Patterns/Light-on-Two-Sides-of-Every-Room-(159)),
  [Ceiling Height Variety (190)](https://patternlanguage.cc/Patterns/Ceiling-Height-Variety-(190)),
  [Alcoves (179)](https://patternlanguage.cc/Patterns/Alcoves-(179)),
  [Workspace Enclosure (183)](https://patternlanguage.cc/Patterns/Workspace-Enclosure-(183)).
  Full pattern list: [contents PDF](https://experiencingartsculture2015.wordpress.com/wp-content/uploads/2015/05/pattern-language-contents.pdf).
- Susanka, S. — *The Not So Big House* (1998), *Creating the Not So Big House* (2000),
  *Home by Design* (2004). Principle lists cross-checked at
  [susanka.com](https://susanka.com/susanka_books/home-by-design-transforming-your-house-into-home/)
  and [thetinyhouse.net interview](https://thetinyhouse.net/sarah-susanka/).
- NKBA — *Kitchen Planning Guidelines with Access Standards* and *Bath Planning Guidelines with
  Access Standards*: [nkba.org/planning-guidelines](https://nkba.org/planning-guidelines/);
  numeric values cross-checked at
  [thewcsupply.com](https://www.thewcsupply.com/pages/kitchen-design-guidelines-standard-clearances)
  and [ashtonrenovations.com](https://ashtonrenovations.com/nkba-bathroom-planning-guidlines/).
- 2021 International Residential Code, Chapter 3:
  [R303.1](https://codes.iccsafe.org/s/IRC2021P2/part-iii-building-planning-and-construction/IRC2021P2-Pt03-Ch03-SecR303.1),
  [R305.1](https://codes.iccsafe.org/s/IRC2021P2/chapter-3-building-planning/IRC2021P2-Pt03-Ch03-SecR305.1),
  [R310.1](https://codes.iccsafe.org/s/IRC2021P2/chapter-3-building-planning/IRC2021P2-Pt03-Ch03-SecR310.1);
  summary of minimum dimensions:
  [Fine Homebuilding](https://www.finehomebuilding.com/2024/01/10/minimum-dimensions-in-the-irc).
- Ramsey/Sleeper, *Architectural Graphic Standards*, residential sections; *Time-Saver
  Standards for Housing and Residential Development* — typical room sizes and furniture
  clearances, cross-checked against contemporary reported ranges
  ([DesignFiles](https://blog.designfiles.co/average-room-size/),
  [RoomSketch3D clearances](https://roomsketch3d.com/help/dimensions/clearance-around-furniture)).
- Barndominium layout conventions:
  [barndominiumlife.com](https://www.barndominiumlife.com/barndominium-floor-plans-with-shop/),
  [shermanpolebuildings.com](https://shermanpolebuildings.com/barndominium-floor-plans/);
  garage/RV/shop dimensions:
  [alansfactoryoutlet.com](https://alansfactoryoutlet.com/blog/standard-garage-size/),
  [americansteelinc.com](https://americansteelinc.com/blog/rv-garage-dimensions-complete-guide/).
- barndsl internals referenced: `src/barndsl/validation.py` (constants and checks),
  `src/barndsl/score.py` (score contract), `src/barndsl/elements.py` (`RoomType`,
  `HABITABLE_TYPES`, `GARAGE_TYPES`), `docs/DIAGNOSTIC_MATRIX.md` (212 registered diagnostics).
