# Ching, *Architecture: Form, Space, and Order* — Research Notes for barndsl

**Source:** Francis D. K. Ching, *Architecture: Form, Space, and Order* (Wiley). Editions 1–5;
5th ed. published 2023. Notes below are written against the 3rd/4th edition structure, which is
stable across editions for everything we care about.

**Audience:** a developer or agent translating architectural theory into machine-checkable rules
for the barndsl compiler (`src/barndsl/validation.py`, `src/barndsl/diagnostics.py`) and the
0–100 scorer (`src/barndsl/score.py`).

**Copyright note:** everything here is paraphrase and synthesis. Numeric facts, ratios, and
taxonomy terms are stated plainly; no passages are reproduced.

---

## Table of contents

1. [Book overview, and why it matters here](#1-book-overview-and-why-it-matters-here)
2. [How to read the "Rule candidates" blocks](#2-how-to-read-the-rule-candidates-blocks)
3. [Primary elements](#3-primary-elements-point-line-plane-volume)
4. [Form](#4-form-properties-transformations-articulation)
5. [Form and space](#5-form-and-space-defining-space-qualities-openings)
6. [Organization](#6-organization-spatial-relationships-and-spatial-organizations)
7. [Circulation](#7-circulation)
8. [Proportion and scale](#8-proportion-and-scale)
9. [Ordering principles](#9-ordering-principles)
10. [Applying all of it to barndominiums](#10-applying-all-of-it-to-barndominiums)
11. [Prioritized implementation backlog](#11-prioritized-implementation-backlog)
12. [Glossary](#12-glossary)
13. [Sources](#13-sources)

---

## 1. Book overview, and why it matters here

Ching's book is the standard first-year vocabulary text for architectural design. Its method is
to isolate the primitives of architectural composition and enumerate their variants exhaustively,
each illustrated with a hand-drawn diagram and a precedent building. It is deliberately
**taxonomic rather than prescriptive**: it tells you that there are exactly four ways two spaces
can relate, and five canonical ways to organize many spaces, without telling you which to pick.

That taxonomic quality is precisely what makes it useful for a rule engine. Most of the book
reduces to finite enumerations over a plan graph plus a rectangle set — which is exactly the data
barndsl already has. The book's seven chapters:

| Ch | Title | What it enumerates | barndsl leverage |
|----|-------|--------------------|------------------|
| 1 | Primary Elements | point, line, plane, volume | low (conceptual grounding) |
| 2 | Form | shape properties; additive/subtractive/dimensional transformation; articulation | medium — envelope + wing composition |
| 3 | Form & Space | how planes define space; degree of enclosure; openings | **high** — walls, doors, windows |
| 4 | Organization | 4 spatial relationships; 5 spatial organizations | **high** — zone/suite/adjacency |
| 5 | Circulation | approach, entrance, path config, path–space relation, path form | **very high** — door graph |
| 6 | Proportion & Scale | proportioning systems; anthropometrics; human scale | **very high** — numeric, directly checkable |
| 7 | Ordering Principles | axis, symmetry, hierarchy, datum, rhythm, transformation | high — the "why is this plan ugly" layer |

### Why it matters for computational floor-plan design specifically

barndsl's diagnostic surface (212 codes across 15 categories) is already **saturated on
code-derived rules** — IRC minimums, egress, fire separation, clearances. Those rules answer
"is this legal and usable?" They do not answer "is this *composed*?" A plan can pass every
existing check and still read as a bag of rectangles: no discernible entry hierarchy, a corridor
that wanders, rooms whose proportions have no relation to each other, windows scattered without
rhythm.

Ching supplies the missing layer, and does so in a form amenable to automation because:

- **The taxonomies are closed.** "Which of the five organizations is this plan?" is a
  classification problem with five labels, not an open judgment. A classifier that returns
  "clustered, weakly" is itself a design critique.
- **Many principles are geometric predicates.** Axis = collinearity of centroids. Symmetry =
  reflection invariance of a room set about a line. Datum = a shared edge coordinate recurring
  across many rooms. These are cheap to compute over an axis-aligned rectangle model.
- **The proportion chapter is literally numeric.** Golden section, Palladio's seven room ratios,
  the ken grid, the Modulor series — all are number sequences you can snap to or measure distance
  from.
- **It gives the agent a gradient.** barndsl's scorer already exists to convert pass/fail
  diagnostics into a continuous signal an LLM agent can climb. Ching-derived metrics
  (axis strength, rhythm regularity, proportion-system conformance) are naturally continuous and
  therefore excellent additional gradient terms.

### The honest limits

Three caveats to carry through the whole document:

1. **Ching is about three-dimensional and perceptual experience.** Much of chapter 3 concerns
   ceiling planes, eye level, and the sequential experience of moving through space. barndsl is
   primarily a plan-and-level model with ceiling heights. Anything requiring a section, a sight
   cone, or a rendered view has to be either approximated in plan or skipped.
2. **He describes; he does not score.** The book never says "aspect ratios above 2.4:1 are bad."
   Every threshold in this document is *my* inference, calibrated against the residential
   dimensions barndsl already uses. They should be treated as starting points to tune against
   the example gallery, not as authority.
3. **Compositional rules are taste, not code.** These belong at `info` severity, feeding
   score.py's `infos` component (2.0 pts each, 20 cap), or as new continuous score terms — never
   as `error`. An error should mean "this building cannot be built or occupied."

---

## 2. How to read the "Rule candidates" blocks

Each theme below ends with a **Rule candidates** block. Format:

- **`PROPOSED_CODE`** *(severity)* — one-line meaning.
  Detection: how to compute it from the existing model.
  Threshold: concrete numbers, in **decimal feet** (barndsl's unit throughout).
  Notes: calibration risks, interaction with existing codes.

Severity follows barndsl convention: `error` (unbuildable/illegal), `warning` (a real problem a
human would flag), `info` (design-quality nudge). Scoring suggestions reference score.py's eight
components: `errors`, `warnings`, `infos`, `space`, `circulation`, `proportion`, `daylight`,
`topology`.

Codes marked *(existing)* already exist — the note explains how Ching sharpens or re-calibrates
them rather than proposing something new. Codes marked *(proposed)* are new.

Existing model concepts these rules can lean on, for reference:
`Barndominium.envelope` + `wings` (footprint rectangles), `Room` (x, y, width, length, type,
level), `RoomType` (24 members incl. `SHOP`, `GARAGE`), `HABITABLE_TYPES`, `INTERIOR_TYPES`,
`GARAGE_TYPES`, `_PUBLIC_TYPES`, `Zone` (declared band), `Suite` (declared bedroom group),
`InteriorDoor` / `ExteriorDoor` / `Window`, `door_graph()` (permeability graph),
`SiteSpec` + `street` (approach direction), `orientation` (true-north azimuth), `FrameSpec`
(bays, posts, beams).

---

## 3. Primary Elements (point, line, plane, volume)

### The principle

Ching builds architecture from a four-step dimensional hierarchy. A **point** marks a position
and has no dimension; extended, it becomes a **line**, which has length and direction; a line
extended in a second direction becomes a **plane**, with surface and orientation; a plane
extended in a third becomes a **volume**, with mass and space.

The point is conceptually central: a point at the center of a field is stable and self-centering,
organizing everything around it. Move it off-center and the field becomes dynamic — the point and
the field now compete visually. Two points define a line and, implicitly, an **axis**
perpendicular to and passing through the midpoint of the segment joining them. That construction
is where chapter 7's axis principle originates.

Lines carry direction and can express structure. A row of columns — points repeated — reads as a
**colonnade**, which is a line that is permeable. Vertical linear elements (a column, an obelisk)
establish a point in three dimensions and mark a location.

Planes are the workhorses of enclosure. Ching distinguishes three architectural planes:
- **Overhead plane** — roof plane (shelters, reads from outside) and ceiling plane (defines the
  upper limit of a room, reads from inside).
- **Wall plane** — the vertical, the most active in shaping and enclosing space.
- **Base plane** — ground plane and floor plane.

Volume is what results: either the solid displaced by mass, or the void contained by planes. This
solid/void duality runs through the whole book and is worth internalizing — barndsl's rooms are
voids, its walls and posts are solids, and several quality problems are really solid/void
misreadings (a leftover void is `AREA_VOID`; an obstructing solid is `POST_OBSTRUCT`).

### Relevance to barndsl

barndsl is essentially a plane-and-volume model already: walls are planes with thickness
(`EXTERIOR_WALL_THICKNESS` 6.5 in, `INTERIOR_WALL_THICKNESS` 4.5 in), rooms are volumes,
`FrameSpec` posts are points. The primary-elements chapter mostly supplies vocabulary rather
than checks, but two ideas are directly actionable: the **centroid as a point** (needed for axis
and hierarchy tests), and **the column line as a line** (`FrameSpec` bay lines are a latent datum
the plan can be measured against — see §9, Datum).

### Rule candidates

- **`POINT_CENTROID_UTIL`** *(infrastructure, not a diagnostic)* — compute and cache room
  centroids, footprint centroid, and the envelope's two center-lines.
  Detection: trivial for axis-aligned rectangles; `cx = x + width/2`, `cy = y + length/2`.
  Notes: prerequisite for axis, symmetry, hierarchy-by-placement, and rhythm checks below. Worth
  landing as a shared helper in validation.py before any §9 rule.

- **`FRAME_GRID_IGNORED`** *(info, proposed)* — interior partitions ignore the structural bay
  grid entirely.
  Detection: for each interior partition line, compute distance to the nearest `FrameSpec` bay
  line (post/beam centerline). Report if fewer than ~40% of partition lines fall within 0.5 ft of
  a bay line.
  Threshold: tolerance 0.5 ft; coverage floor 40%.
  Notes: this is the primary-elements idea (line as organizing device) meeting a real
  post-frame-construction economy. In a genuine barndominium the posts are on 8–12 ft centers and
  partitions that land on them avoid header and blocking work. Interacts with existing
  `WALL_BEARING_AXIS` and `LOAD_PATH`. Keep at info — plenty of good plans ignore the grid.

- **`VOLUME_SOLID_VOID_RATIO`** *(scoring input, proposed)* — express the plan as a solid/void
  ratio and prefer plans where wall length per unit floor area is low.
  Detection: total interior partition length ÷ interior floor area.
  Threshold: for residential plans, roughly 0.05–0.10 lin ft of partition per sq ft is normal;
  above ~0.14 the plan is over-partitioned (many small cells and thick circulation).
  Notes: a cheap proxy for "chopped up," and it correlates with cost. Calibrate against
  `examples/gallery/` before shipping. Could fold into the `space` score component.

---

## 4. Form (properties, transformations, articulation)

### The principle

**Properties of form.** Ching lists the visual properties that let us identify and compare forms:
*shape* (the outline/silhouette), *size*, *color*, *texture*, plus the relational properties
*position*, *orientation*, and *visual inertia* (the degree to which a form appears stable —
maximal when it sits on a broad base and is symmetric about the vertical).

**Regular vs irregular.** Regular forms have parts related by a consistent, usually symmetric
geometry — the platonic solids and their derivatives. Irregular forms have dissimilar parts.
Crucially, a regular form stays regular under transformation: a cube with a corner bitten out is
still read as a cube.

**Transformations of form.** Three kinds:
- **Dimensional** — stretch or compress one dimension. A cube becomes a slab or a bar; a sphere
  becomes an ovoid. Identity persists until the stretch is extreme.
- **Subtractive** — remove volume. If little is removed, the original reads through and we call
  the result a cube with a notch; if a lot is removed, identity flips to a new form.
- **Additive** — attach volume. Ching enumerates five additive arrangements: *centralized*
  (secondary forms around a dominant core), *linear* (a row), *radial* (linear arms from a core),
  *clustered* (grouped by proximity), and *grid* (organized by a modular field). Note these five
  names recur exactly in chapter 4 as the five spatial organizations — the same taxonomy applied
  to solids and to voids.

Additive forms can join in four ways: **spatial tension** (near but not touching, related by
proximity or shared trait), **edge-to-edge** (sharing an edge, free to pivot about it),
**face-to-face** (sharing a coincident plane, usually aligned), and **interlocking volumes**
(overlapping, each retaining identity).

**Articulation of form** is how you make the parts legible. Ching's devices: differentiate
adjoining planes in material, color, or texture; develop corners as distinct elements; light
surfaces so their edges read; separate planes so the corner shows as a void; add trim lines at
edges and openings. Corners are treated at length — a corner can be sharply articulated (the two
planes clearly distinct), rounded (continuity emphasized), or opened (the corner is denied
altogether, which visually dissolves the enclosure).

### Relevance to barndsl

The barndominium form is a **regular rectangular volume**, usually a single gable bar. barndsl's
`wing` statement is literally additive transformation — extra footprint blocks producing L, T,
and U plans. `Section` blocks joined face-to-face give clean plans; joined at a corner with only
a shared edge, you get the "two barns kissing" problem that reads as unresolved. Meanwhile the
`porch` element is an additive subordinate volume, and a recessed porch (bitten out of the
envelope) is subtractive.

`ENVELOPE_MODULE` — the existing check that exterior dimensions land on the 3 ft build module —
is already a form-regularity rule in disguise. The rules below extend it.

### Rule candidates

- **`WING_JOIN_WEAK`** *(warning, proposed)* — a wing touches the primary envelope at a corner
  or along a very short shared edge.
  Detection: for each `wing` in `Barndominium.wings`, compute the length of shared boundary with
  the envelope (or a previously-joined section). Flag when the shared edge is shorter than the
  wing's own narrower dimension × 0.5, or shorter than 8 ft absolute.
  Threshold: shared-edge length ≥ max(8 ft, 0.5 × min(wing.width, wing.length)).
  Notes: face-to-face joins read as one building; edge-to-edge pinches read as an accident and
  usually create an unroofable valley. Pairs with existing `FOOTPRINT_SPLIT` (which only catches
  full disconnection).

- **`WING_IDENTITY_LOST`** *(info, proposed)* — added wings are so large relative to the primary
  envelope that the plan no longer reads as a barn with an addition.
  Detection: `sum(wing areas) / envelope area`.
  Threshold: info when the ratio exceeds 0.60; the barndominium type wants a dominant primary
  bar. Below 0.25 the wing is clearly subordinate (good); 0.25–0.60 is a legitimate two-part
  composition.
  Notes: this is hierarchy (§9) expressed on the footprint. Deliberately soft.

- **`ENVELOPE_ELONGATION`** *(info, proposed)* — dimensional transformation pushed to the point
  the envelope is a corridor.
  Detection: `max(envelope.width, envelope.length) / min(...)`.
  Threshold: the gallery examples run 33×24 (1.38), 69×33 (2.09), 36×30 (1.20), 39×33 (1.18).
  Post-frame barns legitimately run long, so set the info at 3.0 and a stronger nudge at 4.0.
  Notes: high elongation is not wrong (it is the classic shop-plus-living bar), but it forces a
  linear organization and a long spine hall, which drives up the `circulation` score penalty.
  Consider surfacing this as context in the design review rather than a standalone check.

- **`FORM_NOTCH_ORPHAN`** *(info, proposed)* — a subtractive notch in the footprint is too small
  to be useful.
  Detection: any concave region of the footprint boundary whose area is below a threshold and
  which contains no room or porch.
  Threshold: notch area < 48 sq ft (roughly 6×8) and depth < 6 ft.
  Notes: small notches cost two extra corners of framing and flashing for no spatial gain. Ching
  would say identity is unaffected by a subtraction that small — so why pay for it. Related to
  existing `AREA_VOID` but that check is about interior unassigned area, not footprint shape.

- **`CORNER_ROOM_OPEN`** *(info, proposed)* — a habitable corner room has glazing on both walls
  meeting at the corner, dissolving it.
  Detection: for a room occupying an envelope corner, check whether windows exist on both
  exterior walls within 4 ft of the shared corner.
  Threshold: both walls glazed within 4 ft of the corner.
  Notes: Ching treats the open corner as a deliberate, powerful move (it visually erodes the
  enclosure and gives a diagonal outlook). Emit as a *positive* note in the design review rather
  than a penalty — or better, use it as a bonus signal. Structurally it wants a moment frame or a
  hidden post, so pair with a `FrameSpec` cross-check before recommending it.

---

## 5. Form and Space (defining space, qualities, openings)

This is the chapter with the highest direct yield for a plan compiler.

### 5a. Horizontal elements defining space

Four cases, in increasing strength:

- **Base plane** — a horizontal plane sitting on a contrasting ground defines a field. Definition
  comes only from surface contrast (color, texture, material). Weakest.
- **Elevated base plane** — raising the plane creates vertical edges that reinforce separation.
  Ching gives a three-step gradient of how much continuity survives, roughly: a low step (on the
  order of 2–3 ft) keeps both visual and spatial continuity; a mid rise (around 5–6 ft, i.e. near
  eye level) breaks spatial continuity but keeps visual; a large rise (around 12 ft) breaks both
  and isolates the field. *(These specific figures come from a secondary summary of the book;
  treat the ~eye-level breakpoint as the reliable one — it is the perceptually meaningful
  threshold and matches Ching's repeated use of eye level as the criterion.)*
- **Depressed base plane** — sinking the plane uses the cut walls to define a volume. Depth
  determines whether the sunken area still reads as part of the larger room or becomes its own
  room.
- **Overhead plane** — a plane above defines the volume between itself and the ground. The
  ceiling plane is the interior reading; the roof plane the exterior one, and Ching notes the
  ceiling can detach from the roof and be shaped independently.

### 5b. Vertical elements defining space

Vertical elements are more active than horizontal ones because they meet our normal field of
vision head-on. Ching's sequence, again in increasing enclosure:

1. **Vertical linear elements** (columns) — define the edges of a volume without enclosing it. A
   single column marks a point; two define a plane-of-passage; four define a volume of space.
2. **Single vertical plane** — articulates the space in front of it and splits a field into two.
   It has a front and a back but does not enclose.
3. **L-shaped planes** — define a field along the diagonal outward from the corner. Stable,
   self-supporting, and the field is strongest at the corner and dissipates outward.
4. **Parallel planes** — define a directional, axial volume open at both ends. Strongly implies
   movement along the axis. This is the corridor condition.
5. **U-shaped planes** — an inward-focused field with one open end that gives it orientation. The
   open end is the natural entrance and the focus.
6. **Four planes / closure** — the fully enclosed, introverted room. The most common condition in
   architecture, and the one that most clearly defines a discrete space.

### 5c. Qualities of architectural space

Ching identifies the three qualities that most determine how a space feels:
- **Degree of enclosure** — governs the space's form and its relation to adjacent spaces.
- **Light** — the illumination of surfaces, which reveals form and shapes mood.
- **View / outlook** — what the openings frame, which is the space's focus.

Add to that the derived properties: shape, proportion, scale, texture, and the pattern of sound
and thermal behavior.

### 5d. Openings

The most directly checkable material in the book. Three positions:

- **Within planes** — an opening wholly surrounded by wall. A **centered** opening reads stable
  and static; the wall surface around it is even. An **off-center** opening creates visual
  tension between itself and the near edge — dynamic, and sometimes the point. As the opening
  grows, it stops reading as a hole in a wall and starts reading as a positive element in its own
  right, with the remaining wall reading as a frame.
- **At corners** — an opening at a corner orients the space diagonally, and visually erodes the
  corner. Strong move, weakens the sense of enclosure.
- **Between planes** — a vertical opening from floor to ceiling separating two wall planes, or a
  horizontal band at the ceiling. Window walls of this kind dissolve the vertical boundary and
  visually expand the room into the adjacent space or outdoors.

Openings in the overhead plane (skylights, clerestories) bring light down and, because the light
source is above eye level, dramatize the space without giving an outward view.

### Relevance to barndsl

The vertical-elements sequence (§5b) maps almost one-to-one onto how much wall barndsl has
actually built around a room, and the openings taxonomy (§5d) maps onto `Window` and
`InteriorDoor` offsets along wall segments. barndsl already has `DOOR_CENTERED` — which, notably,
prefers *off-center* doors backed to a corner, because that maximizes usable wall and furniture
placement. That is a case where practical residential planning inverts Ching's "centered reads
stable": stability is not what you want on a bedroom door. Keep it. But the same reasoning
inverts again for windows and for the main entry, where centering *is* the desired reading.

`WINDOW_HEAVY` (high window-to-wall ratio) and `WINDOW_PARTITION` (window butts an interior wall)
are already the beginnings of an openings-quality suite.

### Rule candidates

**Enclosure**

- **`ENCLOSURE_DEGREE`** *(info, proposed)* — classify each room's enclosure on Ching's ladder
  and flag rooms whose enclosure contradicts their type.
  Detection: for each room, compute the fraction of its perimeter that is solid wall vs. open
  (cased openings, missing partitions, shared open edges). Bucket: ≥0.90 = closed (four planes);
  0.65–0.90 = U-shaped; 0.40–0.65 = parallel/L; <0.40 = field only.
  Threshold: `bedroom`, `bathroom`, `office` should be ≥0.90 (a bath below that is already caught
  by `OPEN_BATH`). `kitchen`, `dining`, `living` in an open plan are expected at 0.40–0.75 —
  flag a `living` room above 0.90 in a plan that declares an open program as a possible
  contradiction of intent.
  Notes: this is the single most valuable new metric in this document, because it turns the
  vague word "open-plan" into a number. It also feeds `KITCHEN_FLOW`.

- **`HALL_PARALLEL_PLANES`** *(info, proposed)* — a corridor is defined by parallel planes and
  therefore reads purely as movement; if it is long and has no relief, it is a tunnel.
  Detection: hallway length ÷ width, plus count of openings (doors, cased openings, windows)
  along its two long walls.
  Threshold: flag when length/width > 8 and openings per 10 ft of run < 1.0. A 4 ft × 40 ft hall
  (ratio 10) with 3 doors is a tunnel; the same hall with 7 doors and an end window is a spine.
  Notes: directly complements existing `HALL_DEADEND` and `HALL_TIGHT`. See also §7 rules on
  path termination — a window or a room at the far end of a spine fixes the tunnel reading and
  should suppress this.

**Openings — position within a plane**

- **`WINDOW_OFFCENTER`** *(info, proposed)* — a lone window sits noticeably but not decisively
  off-center on an otherwise blank exterior wall.
  Detection: for each exterior wall segment of a habitable room carrying exactly one window,
  compute the normalized offset of the window center from the wall-segment center:
  `d = |wc - sc| / (segment_length / 2)`.
  Threshold: flag when `0.15 < d < 0.55`. Below 0.15 it reads as centered (fine); above 0.55 it
  reads as deliberately pushed to one end (also fine, and often driven by furniture). The middle
  band is the "looks like a mistake" zone.
  Notes: this is the most defensible reading of Ching's centered/off-center discussion for an
  automated check, and it directly addresses the gap recorded in `docs/IDEAS.md` where façade
  rhythm was judged "too subjective" and left out. Framing it as *avoid the ambiguous middle*
  rather than *always center* makes it objective.

- **`WINDOW_WALL_CROWDED`** *(info, proposed)* — window is too close to the end of its wall
  segment to leave a legible frame of wall.
  Detection: distance from window jamb to the nearest wall-segment end (interior corner).
  Threshold: flag below 1.0 ft; comfortable is ≥ 1.5 ft (allows trim, drapery return, and a stud
  bay).
  Notes: partially overlaps `WINDOW_PARTITION` (window butting an interior wall) — check that
  code first and only emit this for the exterior-corner case.

- **`OPENING_POSITIVE_ELEMENT`** *(info, positive signal, proposed)* — an opening large enough to
  read as a positive element rather than a hole.
  Detection: window (or window group) width ÷ wall-segment length.
  Threshold: ≥ 0.50 of the wall segment, or a glazed area ≥ 25% of that wall's area.
  Notes: use as a bonus, not a penalty — it usually indicates a deliberately composed room. Guard
  against double-counting with `WINDOW_HEAVY`, which penalizes the whole-plan ratio.

**Openings — at corners and between planes**

- **`WINDOW_CORNER_PAIR`** *(info, proposed)* — see `CORNER_ROOM_OPEN` in §4; same detection,
  reported from the room's point of view.
  Notes: pick one of the two, not both. Room-level reporting is probably more useful to the
  agent.

- **`CASED_OPENING_NOT_FULL`** *(info, proposed)* — a cased opening between two open-plan rooms
  is narrow enough that it re-imposes a door-like reading.
  Detection: for `InteriorDoor(kind=cased)` between two `_PUBLIC_TYPES` rooms, compare opening
  width to the shared-wall length.
  Threshold: flag when width / shared-wall-length < 0.35 and the plan's program declares an open
  layout. The gallery examples use cased widths of 4 ft (hall) to 10 ft (living–kitchen), so a
  10 ft opening in a 16 ft shared wall (0.63) is properly open; a 4 ft opening in the same wall
  (0.25) is a doorway pretending to be openness.
  Notes: this is Ching's "between planes" idea — a full-height, full-width separation dissolves
  the boundary; a modest hole does not. Strong candidate to feed `KITCHEN_FLOW`.

**Light and view**

- **`DAYLIGHT_SINGLE_ASPECT`** *(info, proposed)* — a habitable room is lit from one wall only.
  Detection: count distinct exterior walls (by orientation) carrying windows per habitable room.
  Threshold: info when a habitable room over ~180 sq ft is single-aspect; stronger when over
  ~300 sq ft. Bedrooms under 150 sq ft are fine single-aspect.
  Notes: existing `daylight` score component measures glazing *area* against the 8% IRC ratio; it
  says nothing about *distribution*. Two windows on one wall and one window each on two walls
  score identically today, but the second is far better lit and gives cross-ventilation. This is
  a genuine gap. Consider extending the `daylight` component to multiply the ratio term by an
  aspect factor (e.g. 1.0 for single-aspect, 1.15 for dual-aspect, capped).

- **`DAYLIGHT_DEPTH`** *(info, proposed)* — a room is too deep from its glazed wall to be
  daylit at the back.
  Detection: perpendicular distance from the glazed wall to the far wall.
  Threshold: rule of thumb is that useful daylight penetrates roughly 2.0–2.5× the window head
  height. With a 10 ft ceiling and a head at ~8 ft, that is 16–20 ft. Flag habitable rooms whose
  depth from the only glazed wall exceeds 2.5 × head height.
  Notes: barndsl's `Window` already carries `head_height`, so this is computable today. Highly
  relevant to barndominiums, where 33 ft-deep envelope bars mean a single-aspect room can easily
  be 20 ft deep.

- **`VIEW_BLOCKED_BY_SHOP`** *(info, proposed)* — a habitable room's only exterior wall faces the
  shop bay or a wing that blocks outlook.
  Detection: cast a ray outward from each glazed wall; if it immediately intersects another
  `Section` of the same building within a short distance, the outlook is nil.
  Threshold: obstruction within 12 ft.
  Notes: mostly matters for L and U plans where an inner corner faces itself.

---

## 6. Organization (spatial relationships and spatial organizations)

### 6a. The four spatial relationships (how *two* spaces relate)

1. **Space within a space** — a small space contained entirely inside a larger one. For the
   contained space to read as a distinct figure, there must be a clear size differential; if the
   inner space grows, the residual space around it stops reading as a field and starts reading as
   leftover. If the inner space is oriented differently from the outer, it gains emphasis and
   generates dynamic residual space.
2. **Interlocking spaces** — two fields overlap. The shared zone can be shared equally by both,
   or merge with one and become part of its volume, or become its own linking space.
3. **Adjacent spaces** — each space is clearly defined; the relationship depends entirely on the
   character of the separating plane. Ching runs the gradient: the plane can be solid (isolating,
   with movement only at a door), pierced by openings, a row of columns (implying separation
   while allowing continuity), or merely implied by a change of level or material.
4. **Spaces linked by a common space** — two spaces relate through an intermediary. The
   intermediary can differ in form and orientation from both, can be linear (making it a
   corridor), can be large enough to dominate and organize both, or can be residual and defined
   by the two it links.

### 6b. The five spatial organizations (how *many* spaces relate)

- **Centralized** — a dominant central space with secondary spaces grouped around it. Stable,
  concentrated, non-directional. The center must be regular and large enough to hold the
  secondaries around its perimeter. Approach and entry are flexible because there is no inherent
  direction.
- **Linear** — a sequence of spaces, either directly connected in a row or strung along a
  separate linear space. Inherently directional and expressive of movement, growth, and
  extension. Terminates well with a distinct space or an entrance. Adapts naturally to a site
  (curving, stepping, segmenting).
- **Radial** — a dominant center from which linear arms extend. Combines centralized and linear:
  introverted at the core, extroverted along the arms. The arms can differ from one another to
  respond to context.
- **Clustered** — spaces grouped by proximity or by a shared visual trait, with no rigid
  geometry. Flexible, accepts growth and change, accommodates cells of different size, form, and
  orientation. Needs a device — symmetry, an axis, a shared face — to give it order.
- **Grid** — spaces organized within a modular three-dimensional field. Order comes from the
  regularity and continuity of the pattern, which persists as a reference even where the grid is
  interrupted or the field is only partly filled. Grids tolerate subtraction, addition, and
  layering better than any other organization.

### Relevance to barndsl

This is the chapter that supplies barndsl's missing "what kind of plan is this?" classifier.
The existing model has all the inputs: room rectangles for geometry, `door_graph()` for
connectivity, and `Zone` / `Suite` for declared intent.

Barndominiums are overwhelmingly **linear** (bar of shop + bar of living, spine hall) or **linear
with a clustered residential end**. `examples/gallery/hall_spine.barn` at 69×33 is the canonical
linear case. Occasionally you get a **centralized** great-room plan where the living volume is
the dominant core and everything hangs off it — this is worth recognizing because the rules that
apply differ (a centralized plan should *not* be penalized for lacking a spine hall).

Note the vocabulary collision flagged in the codebase: `wing` in the DSL means a footprint block,
whereas "the private wing" is a `Zone`. Rules below say *zone* when they mean the programmatic
grouping.

### Rule candidates

**Classification (infrastructure)**

- **`ORG_CLASSIFY`** *(infrastructure, proposed)* — classify the plan as centralized / linear /
  radial / clustered / grid, with a confidence, and surface the label in `barndsl review` output.
  Detection sketch, over the ground-floor room set and door graph:
  - *Linear* — a hallway (or chain of `_PUBLIC_TYPES` rooms) whose bounding box spans ≥ 60% of
    the envelope's long dimension, with ≥ 60% of other rooms doored directly off it. Also: the
    door graph approximates a caterpillar (a path plus leaves).
  - *Centralized* — one room whose area is ≥ 2.5× the median room area and whose door-graph
    degree is ≥ 4, with ≥ 50% of rooms at graph distance 1 from it.
  - *Radial* — a centralized core plus ≥ 3 linear chains of length ≥ 2 emanating from it.
  - *Grid* — ≥ 70% of partition lines fall on a small set of regularly spaced coordinates (see
    `DATUM_GRID` in §9).
  - *Clustered* — the fallback; also positively indicated when rooms group into ≥ 2 spatially
    compact components joined by a single link.
  Notes: this is a *label*, not a diagnostic — but it should gate several other rules. Do not
  penalize a centralized plan for `HALL_*` absence, and do not penalize a linear plan for a long
  spine.

**Space within a space**

- **`NESTED_SPACE_TIGHT`** *(info, proposed)* — a room contained within a larger open volume (an
  island pantry, a closed office in a great room, a stair core) leaves residual space too thin to
  use.
  Detection: for a room fully surrounded by another room's field, measure the minimum residual
  gap on each side.
  Threshold: residual gap < 3.5 ft on any side reads as leftover; also flag when the contained
  room's area exceeds ~35% of the container's, because at that point neither reads as figure or
  ground.
  Notes: Ching's requirement is a clear size differential. 35% is a defensible reading of
  "clear."

**Interlocking / adjacent**

- **`ADJACENCY_PLANE_MISMATCH`** *(info, proposed)* — the separating plane between two rooms
  contradicts their programmatic relationship.
  Detection: for each pair of rooms sharing ≥ 6 ft of wall, classify the separating plane as
  solid / doored / cased-open / absent. Compare against an expectation table keyed on room types.
  Expectation table (starting point):
  | Pair | Expected | Flag if |
  |---|---|---|
  | kitchen ↔ dining | cased-open or absent | solid |
  | kitchen ↔ living | cased-open | solid, in a plan declaring open program |
  | bedroom ↔ living | solid | cased-open or absent |
  | bedroom ↔ bedroom | solid, ideally not shared at all | (already `BED_SOUND`) |
  | shop/garage ↔ bedroom | solid + rated | (already `GARAGE_BEDROOM`) |
  | shop/garage ↔ living | solid + rated, mudroom preferred | doored directly |
  | bath ↔ dining/kitchen | solid, no direct door | doored directly |
  Notes: sharpens existing `KITCHEN_FLOW` and `BED_PRIVACY` into one general mechanism. Best
  implemented as a data table so new pairs are cheap to add.

- **`INTERLOCK_AMBIGUOUS`** *(info, proposed)* — two open-plan rooms overlap so much in the
  declared layout that neither reads as a distinct space.
  Detection: two `_PUBLIC_TYPES` rooms sharing a wall with no partition at all over > 80% of the
  shared boundary, and neither having any other defining feature (level change, ceiling change,
  column line, differing width).
  Notes: this is the "great room that is just a big rectangle with labels" failure. Ching's
  interlocking case wants the shared zone to be legible. A partial-height wall, a column pair, or
  a 2 ft jog restores legibility — recommend one of those in the fix hint.

**Linked by a common space**

- **`LINK_SPACE_RESIDUAL`** *(info, proposed)* — the intermediary space linking two zones is
  merely leftover.
  Detection: identify articulation points in the door graph (rooms whose removal disconnects the
  graph). For each, check whether it has a declared type and a reasonable size.
  Threshold: flag an articulation-point room typed `flex`/untyped, or under 40 sq ft, or with an
  aspect ratio over 4:1 that is not typed `hallway`.
  Notes: barndsl already computes components via `validation.components_excluding(door_graph(plan), …)`, so
  articulation points are nearly free. The transition space between shop and living is exactly
  this condition and deserves to be a real mudroom — see §10.

**Organization coherence**

- **`ORG_ZONE_INCOHERENT`** *(info, proposed)* — declared `Zone` membership does not match
  spatial clustering.
  Detection: for each `Zone`, compute the convex hull (or bounding box) of its member rooms and
  the fraction of that hull's area occupied by non-members.
  Threshold: flag when > 25% of a zone's hull is occupied by rooms belonging to another zone.
  Notes: complements existing `ZONE_CROSS` (which checks a room against its zone's band). This
  checks the inverse — foreign rooms intruding into a zone's territory. Together they enforce
  Ching's clustered-organization requirement that grouping be legible.

- **`ORG_CLUSTER_ORPHAN`** *(info, proposed)* — a room sits spatially apart from every group it
  belongs to.
  Detection: for each room, distance from its centroid to its zone's centroid, normalized by the
  envelope diagonal.
  Threshold: flag beyond 0.5 of the envelope diagonal when the zone has ≥ 3 members.
  Notes: catches the stray bedroom parked at the shop end of the bar.

---

## 7. Circulation

The chapter that pays off most, because barndsl already maintains a door graph.

### 7a. Approach

The distant view of the building, before entry. Three types:

- **Frontal** — the path leads straight at the main façade along an axis. The entry is announced
  from far off; the reading is formal and the façade is seen whole.
- **Oblique** — the path meets the building at an angle, enhancing the perspective of the front
  and the sense of three-dimensionality. The path may be redirected once or several times to
  delay and dramatize arrival.
- **Spiral** — the path wraps the building, prolonging the sequence and emphasizing the volume as
  the observer moves around it. Entry may be at any point along the wrap.

### 7b. Entrance

Passing through the vertical plane. Ching's three treatments:

- **Flush** — the opening sits in the plane of the wall. Deliberately understated; the wall's
  continuity dominates.
- **Projected** — a porch or portico pushes forward to announce the entry and shelter the
  approach. Most emphatic.
- **Recessed** — the entry is carved into the volume, providing shelter and drawing the exterior
  space in.

He notes further that an entrance can be emphasized by making it taller, wider, deeper, or more
elaborate than the surrounding wall; that a small entrance into a large volume dramatizes the
arrival by contrast; and that entrances should be visible and legible from the approach.

### 7c. Configuration of the path

Six configurations: **linear** (the fundamental case — a straight, curved, segmented, looped, or
branching line), **radial** (paths extending from or converging on a center), **spiral** (a single
continuous path winding around a center), **grid** (two sets of parallel paths crossing at
intervals, creating a field of modular spaces), **network** (paths connecting points at arbitrary
positions), and **composite** (combinations, which is what real buildings are). Ching warns that
composite configurations without a hierarchical device become disorienting, and recommends
differentiating paths by scale, form, and length to establish hierarchy.

### 7d. Path–space relationships

Three cases:

- **Pass by spaces** — the path runs alongside; each space keeps its integrity; the connection is
  made by discrete openings. The most flexible.
- **Pass through spaces** — the path cuts through, creating in each space a pattern of rest and
  movement. The path can pass axially, obliquely, or along an edge. It fragments the space it
  crosses.
- **Terminate in a space** — the path ends at the space, which becomes its destination. Used to
  approach functionally or symbolically important rooms.

### 7e. Form of the circulation space

Circulation space is a real volume, not a leftover line. Ching classifies by enclosure:
**enclosed** (a private corridor with discrete openings), **open on one side** (a gallery or
balcony, giving visual continuity with the space it serves), and **open on both sides** (a
colonnade, which becomes an extension of the space it passes through).

He also makes the point that circulation width and height should be proportional to the movement
and use it accommodates — a corridor can widen at intervals to allow rest, gathering, and
furnishing, and a path's scale should signal its importance.

### Relevance to barndsl

barndsl has `street` (approach direction), `drive`, `walk`, `SiteSpec`, and the
`APPROACH_ENTRY` / `APPROACH_GARAGE` / `DRIVE_DOOR` checks — so approach is partly modeled
already. Entrance type is inferable from porch geometry: a `porch` projecting beyond the envelope
is *projected*; a porch carved into it is *recessed*; an exterior door with no porch is *flush*.

Path configuration is directly computable from the door graph plus hallway geometry. Path–space
relationships are computable from whether a route between two rooms transits a third — and
barndsl already penalizes the worst instance of "pass through" (`PRIVATE_PASSTHROUGH`,
`GARAGE_PASSTHROUGH`, plus the `topology` score component).

### Rule candidates

**Approach and entrance**

- **`ENTRY_NOT_EMPHASIZED`** *(info, proposed)* — the main entry has no device distinguishing it
  from any other exterior door.
  Detection: for the primary `ExteriorDoor(kind=entry)` — the one nearest the `street`/`walk` —
  check for at least one of: a covering `porch`, a wider-than-standard leaf (> 3.0 ft or
  `kind=double`/`french`), a recess in the footprint, flanking windows (sidelights approximated
  by windows within 3 ft on both sides), or a distinct roof form over it.
  Threshold: zero emphasis devices → info.
  Notes: this is entrance hierarchy, and it is the cheapest high-value composition rule available.
  Every real house has one of these; auto-generated plans routinely have none.

- **`ENTRY_APPROACH_ILLEGIBLE`** *(info, proposed)* — the entry is not visible from the approach.
  Detection: given `street` direction and `walk`, check that the primary entry lies on a façade
  facing the street (within 90°) and is not screened by a projecting wing or porch mass.
  Threshold: entry façade normal within 90° of the street direction.
  Notes: extends existing `APPROACH_ENTRY`. Note the barndominium-specific failure: the shop
  overhead door faces the drive and *is* the most prominent opening, so the human entry loses the
  contest. See §10.

- **`ENTRY_HIERARCHY_INVERTED`** *(warning, proposed)* — the garage/shop opening is more
  prominent from the approach than the human entry.
  Detection: compare the approach-facing area of overhead doors vs. the emphasis score of the
  primary entry (from `ENTRY_NOT_EMPHASIZED`). Flag when overhead door area on the approach
  façade > 4× the entry door area *and* the entry has zero emphasis devices.
  Threshold: 4× area ratio with zero emphasis.
  Notes: this is the single most characteristic composition failure of the building type. Warning
  rather than info because it materially affects how the building is used and read.

- **`ENTRY_NO_TRANSITION`** *(info, proposed)* — the primary entry opens directly into a
  habitable room with no foyer, vestibule, or defined arrival zone.
  Detection: the room the primary entry door opens into is a `_PUBLIC_TYPES` room with no
  entry-zone room (`foyer`/`mudroom`/`flex` under ~80 sq ft) between the door and the main volume.
  Threshold: no intermediate space and no ≥ 5 ft deep alcove.
  Notes: partially covered by existing `ENTRY_PRIVATE` (which catches opening into a *private*
  room). This catches opening straight into the great room, which is legal, common, and — in cold
  climates with a `climate` zone declared — genuinely bad. Consider gating severity on
  `climate`.

**Path configuration**

- **`PATH_CONFIG_CLASSIFY`** *(infrastructure, proposed)* — label the circulation configuration
  (linear / radial / grid / network / composite) from the hallway set and door graph.
  Detection: build the circulation subgraph (hallway rooms plus public rooms used for transit).
  Linear = a path graph; radial = a hub with ≥ 3 spokes; network = multiple cycles; grid = ≥ 2
  parallel runs in each of two orthogonal directions.
  Notes: gates the rules below and gives the design-review agent language to critique with.

- **`PATH_NO_HIERARCHY`** *(info, proposed)* — a composite circulation system in which all paths
  are the same width, so no path reads as primary.
  Detection: when ≥ 3 hallway segments exist, compute the coefficient of variation of their
  widths.
  Threshold: flag when all hallway widths are within 0.5 ft of each other *and* the total
  hallway run exceeds ~40 ft. Ching's remedy — differentiate by scale — means the spine should be
  wider (e.g. 4.5–5 ft) than the branches (3.5–4 ft).
  Notes: also a good fix hint: "widen the spine to 5 ft and let branches stay at 3.5 ft."

- **`PATH_REDUNDANT_LOOP`** *(info, proposed)* — the circulation graph contains a cycle whose
  extra path length buys nothing.
  Detection: find cycles in the circulation subgraph; for each, compute the shortest alternate
  route length vs. the direct route.
  Threshold: flag a cycle where the alternate route is > 2.5× the direct route and the cycle adds
  > 60 sq ft of hallway.
  Notes: loops are often *good* (they eliminate dead ends and improve flow — see `HALL_DEADEND`),
  so this must be tuned conservatively. A loop that costs little is a feature.

- **`PATH_DIRECTION_CHANGES`** *(info, proposed)* — the route from the entry to the master
  bedroom (or to any bedroom) turns excessively.
  Detection: BFS shortest route on the door graph from the primary entry to each bedroom; count
  direction changes along the geometric polyline through room centroids and door positions.
  Threshold: > 4 direction changes, or route length > 2.0× the straight-line distance.
  Notes: this is a **detour index**, borrowed from space-syntax practice, and it is one of the
  strongest continuous metrics available. It would make an excellent addition to the
  `circulation` score component, which currently measures only hallway *area* fraction.

**Path–space relationships**

- **`PATH_THROUGH_PUBLIC_ROOM`** *(info, proposed)* — the primary route to the private zone cuts
  through the middle of a public room, fragmenting it.
  Detection: for the entry→bedroom route, where it transits a `living`/`great_room`/`dining`
  room, compute how close the route polyline passes to the room's centroid, normalized by the
  room's half-width.
  Threshold: flag when the route passes within 0.35 of the normalized half-width — i.e. axially
  through the middle. A route hugging an edge (> 0.65) is Ching's acceptable "pass along the
  edge" case and should not be flagged.
  Notes: this is a genuinely new and useful distinction. Today barndsl treats any pass-through of
  a public room identically; Ching says passing along the edge preserves the room and passing
  through the middle destroys it. The fix hint writes itself: move the two doors toward the same
  end of the room.

- **`PATH_TERMINATES_WELL`** *(info, positive signal, proposed)* — a spine hall terminates in a
  space or a window rather than a blank wall.
  Detection: at the far end of the longest hallway run, check for (a) a door into a room, (b) a
  window in the end wall, or (c) the hall opening into a public room.
  Threshold: none of the three → this is essentially existing `HALL_DEADEND`; presence of a
  window at the terminus should *suppress* `HALL_DEADEND` or reduce its weight.
  Notes: a concrete recalibration of an existing check. Ching's point is that termination is
  about visual destination, not just about not wasting floor area — and a window is a perfectly
  good destination.

**Form of circulation space**

- **`CIRCULATION_ENCLOSED_ONLY`** *(info, proposed)* — every circulation space in the plan is
  fully enclosed corridor; none is open on one side.
  Detection: for each hallway, compute the fraction of its long-wall length that is open (cased
  openings, half-walls, or shared edges with public rooms).
  Threshold: flag when the plan's total hallway run exceeds 30 ft and *no* segment has ≥ 30% open
  frontage.
  Notes: opening one side of a corridor onto the great room converts corridor area into borrowed
  living space — which also improves the `space` and `circulation` score components. Good
  synergy: the fix helps three metrics at once.

- **`HALL_NO_RELIEF`** *(info, proposed)* — a long corridor never widens.
  Detection: hallway segments longer than 25 ft with constant width.
  Threshold: recommend a widening to ≥ 6 ft for a stretch of ≥ 6 ft at roughly the midpoint, or
  at a junction.
  Notes: Ching's "widen at intervals for rest and gathering." In residential practice this is
  where the linen closet, the bench, or the gallery wall goes.

---

## 8. Proportion and Scale

The most numerically concrete chapter, and the easiest to implement.

### 8a. Ching's framing

He distinguishes **proportion** (the relation of parts to each other and to the whole — a matter
of ratios, independent of absolute size) from **scale** (how big something is relative to a known
reference, usually the human body or the surrounding elements).

Before the historical systems, he covers three real-world constraints on proportion:
- **Material proportions** — every material has strength and stiffness limits that set rational
  dimensions (a masonry pier must be thick; a steel column can be thin).
- **Structural proportions** — beams deepen as they span farther; columns thicken as they carry
  more. Structure has a visible logic and reading it is part of reading a building.
- **Manufactured proportions** — standard product sizes (the 4×8 sheet, the modular brick, the
  standard door leaf) constrain dimensions in practice. Designing to them saves material and
  labor.

That third category is exactly what barndsl's `ENVELOPE_MODULE` (3 ft build module) encodes, and
it generalizes: sheet goods at 4 ft, post-frame bays at 8/10/12 ft, stud spacing at 16 in and
24 in.

### 8b. Proportioning systems

**Golden section.** φ = (1 + √5)/2 ≈ **1.618**; its reciprocal ≈ **0.618**. Defined by the
property that the ratio of the whole to the larger part equals the ratio of the larger part to
the smaller: (a+b)/a = a/b. The **Fibonacci** series (1, 1, 2, 3, 5, 8, 13, 21, 34, 55, …) has
successive ratios converging on φ: 3/2 = 1.5, 5/3 ≈ 1.667, 8/5 = 1.6, 13/8 = 1.625, 21/13 ≈
1.615. A **golden rectangle** can be subdivided into a square plus a smaller golden rectangle,
indefinitely. Called the *divine proportion* in the Renaissance.

**Regulating lines.** Diagonals of rectangles used as construction lines: two rectangles are
proportionally identical (similar) if their diagonals are parallel or perpendicular. This is the
graphic technique for propagating one proportion through a whole façade or plan, and it is the
practical face of the golden-section idea.

**Classical orders.** Vitruvius derived everything from the **column diameter at the base as the
module**. Column heights are stated in diameters — roughly 7 for Doric, 9 for Ionic, 10 for
Corinthian, depending on the source. Spacing between columns (**intercolumniation**) is likewise
in diameters, with named intervals: *pycnostyle* 1½ D, *systyle* 2 D, *eustyle* 2¼ D (with a
wider ~3 D center bay), *diastyle* 3 D, *araeostyle* 3½ D or more. The lesson worth extracting:
**one dimension is chosen as the module and every other dimension is expressed as a multiple or
fraction of it.**

**Renaissance theories.** Alberti and Palladio held that beauty came from whole-number ratios,
the same ratios that make musical intervals consonant. Palladio, in *I Quattro Libri*
(1570), gave seven room shapes he considered most beautiful:

| # | Shape | Ratio (length : width) | Decimal |
|---|-------|------------------------|---------|
| 1 | Circle | — | — |
| 2 | Square | 1 : 1 | 1.000 |
| 3 | Diagonal of the square | √2 : 1 | 1.414 |
| 4 | Square and a third | 4 : 3 | 1.333 |
| 5 | Square and a half | 3 : 2 | 1.500 |
| 6 | Square and two-thirds | 5 : 3 | 1.667 |
| 7 | Two squares | 2 : 1 | 2.000 |

Note that these cluster tightly: excluding the square and the double square, every preferred
ratio lies between **1.333 and 1.667**, and the mean is close to φ.

Palladio derived **ceiling height** from the room's plan dimensions using the three classical
means of width *w* and length *l*:
- **Arithmetic mean:** h = (w + l) / 2
- **Geometric mean:** h = √(w · l)
- **Harmonic mean:** h = 2wl / (w + l)

For a 12 × 18 ft room these give 15.0, 14.7, and 14.4 ft respectively — far higher than
residential practice, so the *formulas* are useful as relative guidance, not absolute heights.
The relative insight survives: **bigger rooms should be taller**, and the ratio between them
should be systematic.

**Modulor.** Le Corbusier's system, built on the human body, the golden section, and Fibonacci.
Early versions used a 1.75 m figure; the published version used a **6 ft (1829 mm)** man, with
the **navel at 1130 mm** and the **raised-arm height at 2260 mm** (7 ft 5 in) — exactly double
the navel height. Two Fibonacci-like series are generated: the **red series** from 1130 mm and
the **blue series** from 2260 mm, each term the sum of the two before it, with successive terms
in golden ratio (1130/698 ≈ 1.618; 698/432 ≈ 1.616). Le Corbusier's stated aim was to bridge
imperial and metric while keeping every dimension human-referenced.

**Ken.** The Japanese module, originally the variable spacing between columns, later standardized
at **6 shaku ≈ 1.818 m ≈ 5.97 ft** — call it 6 ft. The **tatami** mat is 1 ken × ½ ken, i.e.
6 × 3 shaku ≈ **1818 × 909 mm** ≈ 5.97 × 2.98 ft. Rooms are sized and named by mat count —
4½, 6, 8, 10, 12 mats — and area is measured in **tsubo**, one square ken ≈ 2 mats ≈ 3.31 m² ≈
**35.6 sq ft**. Two design methods:
- **Inaka-ma** ("country method") — the 6-shaku grid fixes **column centers**; mat sizes vary
  slightly to fit the residual. Structure governs.
- **Kyo-ma** ("capital method") — the **mat size is constant** (larger, ~6.3 × 3.15 shaku ≈
  1910 × 955 mm) and column spacing varies with room size. Finish governs.

The ken is the single best historical precedent for what barndsl should do, because it is a
**building-scale module tied to both structure and finish** — precisely the post-frame situation.

**Anthropometry.** Dimensioning from the body: forms and spaces are either containers of the body
or extensions of it, so their dimensions should derive from it. Ching's human-dimension plates
give the working figures. The commonly cited residential ones, consistent with the book's plates
and with standard practice:

| Dimension | Value |
|---|---|
| Standard door leaf height | 6 ft 8 in (2032 mm) |
| Standard interior door width | 2 ft 6 in – 3 ft 0 in |
| Eye level, standing adult | ~5 ft 0 in – 5 ft 6 in |
| Comfortable overhead reach | ~7 ft 0 in |
| Kitchen counter height | 3 ft 0 in |
| Table / desk height | 2 ft 6 in |
| Seat height | 1 ft 5 in – 1 ft 6 in |
| Passage, one person | 2 ft 0 in – 2 ft 6 in clear |
| Passage, two abreast | 4 ft 0 in – 5 ft 0 in clear |
| Minimum residential corridor | 3 ft 0 in |
| Reach across a counter | ~2 ft 0 in |

barndsl's `constants.py` already encodes several of these (`MIN_HALLWAY_WIDTH` 3.0,
`COMFORT_HALLWAY_WIDTH` 4.0) — the two-abreast figure is precisely the anthropometric
justification for the comfort value.

### 8c. Scale

- **Visual scale** — how big something *looks* relative to the other elements around it, which
  may differ from its actual size.
- **Human scale** — size relative to our own dimensions. Elements of known size (doors, stair
  risers, handrails, window sills, counters) are the yardsticks by which we read everything else,
  which is why they must not be distorted.
- **Mechanical scale** — measurement against a standard unit.
- **Scalar comparison** — an element reads large or small only in context; the same window is
  monumental in a cottage and trivial in a warehouse.

A space feels intimate when its ceiling is low relative to its plan dimensions and monumental
when it is tall. Ching notes that a room's horizontal dimensions relative to its height determine
whether it reads as tall, intimate, or well-proportioned — and that the wall a person can touch
and the door they pass through are what keep any space human-scaled.

### Relevance to barndsl

This chapter is where barndsl gains the most immediately. The existing `proportion` score
component uses a **single** bar — `GOOD_ASPECT = 1.6` — beyond which elongation is penalized,
with a bedroom weight of 2.0 and full penalty at 3.1:1. That 1.6 is, satisfyingly, almost exactly
φ and almost exactly the top of Palladio's preferred band. But it is a one-sided threshold: it
knows only "too long," not "on system." Palladio gives a *set* of preferred ratios, which is a
much richer signal: a 1.45:1 room is currently unpenalized but is not on any classical ratio,
whereas a 1.50:1 room is exactly 3:2.

The bigger opportunity is **module conformance**. Post-frame barndominiums have a natural ken:
the bay spacing (typically 8, 10, or 12 ft) and the 3 ft build module `ENVELOPE_MODULE` already
enforces on the envelope. Extending module conformance from the envelope to interior rooms is a
direct application of the ken system, and it has real construction payoff.

Ceiling height is the barndominium's signature variable — 9 to 10 ft is typical in the gallery,
but shop bays want 12–16 ft. Palladio's means give a principled way to relate height to plan
size, and scale gives a way to flag rooms whose height contradicts their use.

### Rule candidates

**Proportion — room ratios**

- **`ROOM_PROPORTION`** *(existing, info)* — recalibrate against Palladio's set rather than a
  single bar.
  Proposal: keep the current elongation penalty as the outer guard, but add a *proximity to a
  preferred ratio* term. Compute `r = long / short` and the distance to the nearest member of
  {1.000, 1.333, 1.414, 1.500, 1.618, 1.667, 2.000}. Award a small bonus (or reduce the
  `proportion` component) when `|r - nearest| ≤ 0.04`; leave the current penalty for `r > 1.6`
  in place but consider raising the free band to 1.70 so that 5:3 is not penalized.
  Threshold: snap tolerance 0.04 (a 12 ft room admits ±0.5 ft on the other dimension — realistic).
  Notes: the current `GOOD_ASPECT = 1.6` slightly penalizes 5:3 (1.667), one of Palladio's seven.
  Raising the free band to 1.70 is a small, defensible calibration change.

- **`ROOM_RATIO_OFF_SYSTEM`** *(info, proposed)* — a plan uses many different, arbitrary room
  ratios with no shared system.
  Detection: collect all habitable-room aspect ratios; count how many fall within 0.04 of a
  preferred ratio.
  Threshold: flag when fewer than 40% are on-system and there are ≥ 5 habitable rooms.
  Notes: Ching's actual argument for proportioning systems is not that any one ratio is magic —
  it is that a *consistent* system makes a building read as ordered. This rule captures that
  argument better than any single-room rule can. Excellent scoring-component candidate.

- **`ROOM_SQUARE_LARGE`** *(info, proposed)* — a large room is almost exactly square.
  Detection: `1.0 ≤ r ≤ 1.08` with area > 300 sq ft.
  Notes: Ching treats the square as static and non-directional — fine for a small room, awkward
  for a great room that needs to seat and orient furniture. Very soft; some plans want this.

**Proportion — module conformance (the ken idea)**

- **`ROOM_OFF_MODULE`** *(info, proposed)* — interior room dimensions ignore the build module.
  Detection: for each room, check `width` and `length` against the plan's module (default 2 ft,
  or the 3 ft used by `ENVELOPE_MODULE`, or a declared `param`).
  Threshold: flag a room when neither dimension is within 0.25 ft of a module multiple; report a
  plan-level info when < 50% of rooms are on-module.
  Notes: a direct transposition of the ken. Ties to sheet goods (4 ft), stud layout (16 in /
  24 in), and post-frame bays. Consider a `module` DSL statement so the author can declare it,
  mirroring how `climate` and `orientation` gate other checks.

- **`ROOM_BAY_ALIGNED`** *(info, proposed)* — inaka-ma vs kyo-ma, made explicit: does the plan
  let the structure govern or the finish govern?
  Detection: compare partition positions against `FrameSpec` bay lines (see `FRAME_GRID_IGNORED`
  in §3). Report which system the plan is closer to, and flag plans that are consistently neither.
  Notes: mostly a review-narrative feature rather than a hard check, but naming the choice is
  itself valuable design feedback and is exactly the kind of thing an LLM design reviewer can use.

**Ceiling height and scale**

- **`CEILING_SCALE_MISMATCH`** *(info, proposed)* — ceiling height is out of proportion to room
  plan size.
  Detection: compute `s = √(area) / ceiling_height` for each habitable room.
  Threshold: residential comfort band is roughly **s = 1.4 to 3.0**. Below 1.4 the room feels
  shaft-like (a 10×10 room with a 16 ft ceiling: s = 0.63); above ~3.5 the room feels
  low and cavernous (a 30×30 great room at 8 ft: s = 3.75). Flag outside 1.2–3.5.
  Notes: gives a principled reason for the barndominium's typical 10 ft ceiling — at 10 ft, a
  200 sq ft room gives s = 1.41 and a 600 sq ft great room gives s = 2.45, both comfortably in
  band. Do **not** apply to `SHOP`/`GARAGE`, which want tall ceilings for equipment.

- **`CEILING_UNIFORM_FLAT`** *(info, proposed)* — every room in the plan has an identical ceiling
  height, so ceiling height does nothing to differentiate important spaces.
  Detection: all rooms share one ceiling value and the plan has a `great_room` or `living` over
  ~350 sq ft.
  Notes: Ching's hierarchy-by-size in section. The remedy — a vaulted or raised great room — is
  cheap in a post-frame building because the trusses are already up there, so the fix hint is
  strong. Do not make this a penalty in plans that declare a `loft`, since the loft floor
  necessarily flattens the ceiling below.

- **`SCALE_REFERENCE_DISTORTED`** *(info, proposed)* — human-scale reference elements are
  non-standard in a way that distorts the reading of the space.
  Detection: doors whose height differs from 6 ft 8 in (or the declared standard) in habitable
  areas; windows whose `sill_height` is above ~3.5 ft in a living space (blocks a seated view
  out); counters off the 3 ft standard.
  Threshold: sill above 3.5 ft in `living`/`dining`/`great_room` — info. Existing
  `WINDOW_SILL` handles the code-required cases; this handles the perceptual one.
  Notes: bathrooms legitimately use high sills (the gallery uses `sill 5`), so exclude them.

**Modulor / anthropometry**

- **`ANTHRO_CLEARANCE_SUITE`** *(existing family, info)* — `BED_CLEARANCE`, `DINING_CLEARANCE`,
  `OFFICE_CLEARANCE`, `CLOSET_DEPTH` already exist and are the anthropometric layer. Ching's
  contribution is mainly to justify them and to suggest completing the set.
  Gaps worth adding: kitchen aisle clearance (recommend 3 ft 6 in – 4 ft between opposing
  counters; 4 ft when two cooks), bathroom fixture clearances (2 ft 6 in in front of a toilet,
  2 ft 6 in in front of a lavatory), and laundry door swing vs. appliance door swing.
  Notes: these are well covered by NKBA/IRC standards, which are more authoritative than Ching
  for the numbers; use Ching only for the framing.

- **`MODULOR_STAIR_HEADROOM`** *(existing, warning)* — `STAIR_HEADROOM` exists. Worth noting the
  Modulor's raised-arm figure of 7 ft 5 in as the perceptual comfort target for headroom, above
  the IRC's 6 ft 8 in code minimum. A plan hitting 6 ft 8 in exactly is legal but a tall occupant
  will feel it. Consider an info at < 7 ft 0 in.

---

## 9. Ordering Principles

Ching's closing chapter: the devices that make a composition read as ordered rather than
arbitrary. Order here means each part properly placed relative to the others and to its purpose —
not mere geometric regularity.

### 9a. Axis

A line established by two points in space, about which forms and spaces can be arranged
symmetrically or in balance. An axis is fundamentally linear and therefore has direction, length,
and the qualities of movement and extension. It must be reinforced by real elements — planes,
colonnades, a series of rooms — and it wants **termination at both ends**: a point, a vertical
element, a well-defined space, a gateway, or a view outward.

### 9b. Symmetry

The balanced distribution of equivalent forms about a common line or point. Two kinds:
- **Bilateral** — mirror equivalence about a single axis. The dominant kind in architecture.
- **Radial** — equivalent elements about two or more axes crossing at a center.

Symmetry can be *total* (governing the whole building) or *local* (a symmetric part inside an
otherwise asymmetric whole) — the local case is much more common and much more useful in
residential work.

### 9c. Hierarchy

The articulation of the relative significance of a form or space by making it exceptional. Three
means:
- **By size** — the important element is significantly larger.
- **By shape** — the important element differs in shape or geometry from the norm.
- **By placement** — the important element is strategically located: at the end of a sequence, on
  an axis, at the center, at the top, or offset from the group.

For hierarchy to read, the exception must be **clearly** exceptional; a marginally larger room
just looks like an inconsistency.

### 9d. Datum

A line, plane, or volume that, by its continuity and regularity, gathers, measures, and organizes
a pattern of forms and spaces. A datum is what lets an otherwise random collection read as
ordered. Kinds: **linear** (a road, a wall, a colonnade, a beam line), **planar** (a floor, a
ceiling plane, a facade), **volumetric** (a room or court that everything else is arranged
around). To function, a datum must have enough visual continuity to cut through or bound the
elements it organizes.

### 9e. Rhythm

Any unifying movement produced by patterned recurrence. Repetition is the simplest rhythm; more
complex rhythms come from varying interval, size, or emphasis while keeping the pattern legible.
We group repeating elements by proximity and by shared visual traits. A rhythm can be interrupted
and still read, provided the interruption is itself legible (and interruptions are how you make
hierarchy inside a rhythm).

### 9f. Transformation

An architectural concept or organization can be altered through a series of discrete
manipulations in response to context without losing its identity or its underlying idea. This is
the principle that lets a designer take a canonical type and adapt it — and it is arguably the
most important principle for a generative system, because it describes exactly what an editing
agent does.

### Relevance to barndsl

These are the "why does this plan feel arbitrary?" checks, and none of them exist today. All are
computable over axis-aligned rectangles. They should all be `info`, and the strongest candidates
(axis, datum, rhythm) are better expressed as **continuous score terms** than as binary flags,
because a plan is always *somewhat* ordered.

### Rule candidates

**Axis**

- **`AXIS_PRESENT`** *(scoring input / positive signal, proposed)* — does the plan have a legible
  primary axis?
  Detection: candidate axes are the envelope center-lines and any line through the primary entry
  perpendicular to its façade. For each candidate, score: (a) number of room centroids within
  2 ft of it, (b) number of doors whose centers lie on it, (c) whether both ends terminate in a
  window, a door, or a defined space.
  Threshold: an axis is "present" when ≥ 3 doors or centroids align within 2 ft and at least one
  end terminates.
  Notes: the classic residential expression is an entry axis running from the front door through
  the great room to a rear window or door — which barndsl could actively recommend.

- **`AXIS_UNTERMINATED`** *(info, proposed)* — a strong axis exists but dies at a blank wall.
  Detection: as above, but with zero terminating features at one end.
  Notes: strongly related to `HALL_DEADEND` and `PATH_TERMINATES_WELL` (§7). Consider merging.

- **`ENTRY_AXIS_ALIGNED`** *(info, positive signal, proposed)* — the entry door, the main
  circulation spine, and a terminal feature are collinear.
  Detection: entry door center, spine hall centerline, and terminus all within 2 ft of one line.
  Notes: a compact, high-signal "this plan was composed" indicator.

**Symmetry**

- **`SYMMETRY_LOCAL`** *(info, positive signal, proposed)* — detect local bilateral symmetry.
  Detection: for each candidate axis, test whether the room set within some neighborhood maps
  onto itself under reflection, matching on type and on dimensions within a tolerance.
  Threshold: dimension tolerance 1.0 ft; require ≥ 2 matched pairs to count.
  Notes: the common residential case is a pair of secondary bedrooms flanking a shared bath, or
  windows symmetric about the entry. Detecting and *rewarding* it is more useful than penalizing
  its absence.

- **`SYMMETRY_NEAR_MISS`** *(info, proposed)* — an arrangement is *almost* symmetric, which reads
  worse than either full symmetry or frank asymmetry.
  Detection: as above, but the match fails on a small margin.
  Threshold: paired rooms whose dimensions differ by 0.5–2.5 ft, or whose positions differ by
  0.5–2.5 ft from mirror position.
  Notes: this is the same "avoid the ambiguous middle" logic as `WINDOW_OFFCENTER`, and it is one
  of the most reliable ways to make an automated critique feel perceptive. Two bedrooms flanking
  a bath at 12×11 and 12.5×11 look like a mistake; at 12×11 and 14×11 they look intentional.

**Hierarchy**

- **`HIERARCHY_NO_PRIMARY_SPACE`** *(info, proposed)* — no room is clearly the dominant living
  space.
  Detection: sort habitable-room areas; compute `largest / second_largest` among
  `_PUBLIC_TYPES`.
  Threshold: flag when the ratio is < 1.35 — no room is decisively primary. Ching's requirement
  is that the exception be *clearly* exceptional.
  Notes: applies to `_PUBLIC_TYPES` only; bedrooms deliberately being similar is fine (and is
  itself a rhythm).

- **`HIERARCHY_MASTER_NOT_DISTINGUISHED`** *(info, proposed)* — the primary bedroom is not
  distinguished from the secondary bedrooms by size, shape, or placement.
  Detection: check at least one of: area ≥ 1.3× the next-largest bedroom; a declared `Suite` with
  ensuite and walk-in; placement at the end of the private wing / opposite end from the
  secondaries; a different aspect ratio.
  Threshold: zero distinguishing features → info.
  Notes: complements existing `MASTER_ENSUITE`. Together they cover hierarchy by size, by
  program, and by placement.

- **`HIERARCHY_BY_PLACEMENT`** *(scoring input, proposed)* — reward placing the dominant public
  space at the axial or terminal position.
  Detection: is the largest public room centered on the plan's primary axis, or at the terminus
  of the primary circulation path?
  Notes: purely a bonus signal.

**Datum**

- **`DATUM_SPINE`** *(info, positive signal, proposed)* — a continuous linear datum organizes the
  plan.
  Detection: find the longest continuous interior wall line (accumulating collinear partition
  segments within 0.25 ft of one another, allowing door gaps). Express as a fraction of the
  envelope's long dimension.
  Threshold: a datum is "strong" at ≥ 0.60 of the long dimension. Below 0.30, the plan has no
  organizing line.
  Notes: in a barndominium the shop/living demising wall is almost always the natural datum, and
  it usually *should* run the full width. Checking that it does is high-value. Also relates to
  `WALL_BEARING_AXIS`.

- **`DATUM_GRID`** *(info, proposed)* — measure how well partition coordinates collapse onto a
  small set of lines.
  Detection: collect all partition x-coordinates and y-coordinates; cluster within 0.5 ft; report
  the fraction of total partition length falling on the top *k* clusters (k ≈ 6 per axis).
  Threshold: ≥ 0.70 coverage = a well-gridded plan; < 0.45 = scattered.
  Notes: excellent continuous score term. Also inputs to `ORG_CLASSIFY` (§6). A plan where every
  partition line is unique is expensive to frame and reads as noise.

- **`DATUM_JOG_MINOR`** *(info, proposed)* — two nearly-collinear partition lines are offset by a
  small amount, breaking the datum for no reason.
  Detection: parallel partition segments whose offset is between 0.25 ft and 2.0 ft with
  overlapping extents.
  Threshold: 0.25–2.0 ft offset.
  Notes: the plan-geometry twin of `SYMMETRY_NEAR_MISS`. Small jogs cost framing, break the
  datum, and are almost always accidental in generated plans. Probably the **single
  highest-yield rule in this document** for auto-generated layouts, because the layout engine
  produces these constantly and a human never would.

**Rhythm**

- **`RHYTHM_WINDOW_SPACING`** *(info, proposed)* — windows on a façade are irregularly spaced
  with no legible rhythm.
  Detection: for each exterior façade with ≥ 3 windows, take the sorted window-center positions
  and compute the gaps between them. Compute the coefficient of variation (CV = σ/μ) of the gaps.
  Threshold: CV ≤ 0.15 reads as regular (good). CV ≥ 0.45 reads as deliberately varied (also
  acceptable in a picturesque composition). **0.15 < CV < 0.45 is the ambiguous zone** — flag it.
  Notes: this directly closes the gap recorded in `docs/IDEAS.md`, where even window spacing was
  judged "more subjective and left out." Framing it as a CV band rather than a spacing
  requirement makes it objective and avoids forcing a rigid façade. Also compare **head heights**
  — a shared head height across a façade is a planar datum and is worth checking separately (flag
  when heads differ by 0.25–1.0 ft, the ambiguous band again).

- **`RHYTHM_WINDOW_HEADS`** *(info, proposed)* — window head heights on one façade vary slightly.
  Detection: group windows by façade; compute the spread of `head_height`.
  Threshold: flag when heads differ by more than 0.25 ft but less than 1.0 ft. A deliberate
  clerestory band (differing by > 1.5 ft) is fine.
  Notes: this is the planar datum idea and it is very cheap to compute, since `head_height` is
  already on the `Window` model.

- **`RHYTHM_BAY_IRREGULAR`** *(info, proposed)* — structural bay spacing is irregular.
  Detection: gaps between `FrameSpec` posts along each wall; CV as above.
  Threshold: CV > 0.20 with no declared reason (e.g. an overhead door opening).
  Notes: post-frame construction wants regular bays; irregularity usually means the layout
  engine placed posts to dodge partitions rather than the reverse.

**Transformation**

- **`TRANSFORM_TYPE_DRIFT`** *(review narrative, proposed)* — describe how far the plan has moved
  from a canonical barndominium type.
  Detection: define 3–4 reference types (shop-end bar, shop-side bar, L-plan with courtyard,
  central-great-room). Score the current plan's similarity to each.
  Notes: not a check — a narrative aid for the design-review agent, giving it a vocabulary for
  "this started as a linear bar and has drifted into an unresolved cluster." Useful for the
  restructure step of the agent loop.

---

## 10. Applying all of it to barndominiums

The barndominium is architecturally interesting because it is **one envelope containing two
radically different scales of program**: a shop/garage volume wanting 12–16 ft ceilings, 12–16 ft
overhead doors, unobstructed spans, and hard floors; and a residential volume wanting 9–10 ft
ceilings, 3 ft doors, subdivided cells, and daylight. Nearly every quality problem in the type
comes from failing to manage that collision. Ching's principles map onto it unusually cleanly.

### 10.1 Hierarchy: the shop wins by default, and that is the problem

By size, the shop is the dominant volume — often 50–70% of the footprint. By placement on the
approach, the overhead door is usually the most prominent opening. So the building reads as a
shop that happens to have an apartment, even when the client's priority is the reverse.

Ching's hierarchy toolkit gives three independent levers, and a good plan uses at least one:
- **By shape** — give the residential end a different roof form (a shed dormer, a gable
  perpendicular to the main ridge, a monitor). barndsl's `roof_style` supports gable/shed/monitor.
- **By placement** — put the human entry on a different façade from the overhead doors, ideally
  the one the walk approaches, so the two do not compete.
- **By articulation** — a projected porch on the residential end, which is both entrance
  emphasis (§7b) and additive form (§4).

Rules: `ENTRY_HIERARCHY_INVERTED`, `ENTRY_NOT_EMPHASIZED`, `HIERARCHY_NO_PRIMARY_SPACE`.

### 10.2 The demising wall is the datum

The shop/living separation wall is the most important line in the building. It is
simultaneously:
- the fire separation (existing `GARAGE_SEPARATION`, IRC R302.6),
- the acoustic and thermal boundary,
- usually a bearing or shear line, and
- the plan's primary linear datum (§9d).

It should run the full width of the envelope, uninterrupted except by one controlled opening. A
demising wall that jogs, or that is punctured in two places, fails on all four counts at once.

Rules: `DATUM_SPINE`, `DATUM_JOG_MINOR`, plus a proposed `DEMISING_WALL_CONTINUOUS`
*(warning)* — flag when the shop/living boundary is not a single continuous line across the
envelope, or when it carries more than one door.

### 10.3 The transition space is not optional

Ching's "spaces linked by a common space" (§6a) is the correct frame for the shop→living
connection. Two spaces of utterly different character should not be adjacent-with-a-door; they
need an intermediary that mediates dirt, noise, temperature, and social register. In residential
terms that is a **mudroom**, and in a barndominium it is the most important room in the plan
relative to its size.

Minimums worth encoding: at least 6 × 8 ft (48 sq ft), with a bench wall of ≥ 4 ft, a path clear
width of ≥ 3 ft through it, and doors at *different* walls so it is a real transition rather than
a widened doorway. Existing `MUDROOM_SHAPE` already flags narrow/elongated mudrooms.

Rules: `LINK_SPACE_RESIDUAL`, plus a proposed `SHOP_NO_TRANSITION` *(warning)* — the shop connects
to living space through a single door with no intermediate room. This is stronger than the
existing `GARAGE_BEDROOM` / `GARAGE_PASSTHROUGH` checks, which catch only the egregious cases.

### 10.4 Linear organization is the default, and it has known failure modes

The canonical barndominium is a **linear organization**: shop bar, then living bar, with a spine
hall serving bedrooms. `examples/gallery/hall_spine.barn` at 69 × 33 is exactly this.

Linear organizations are directional and want termination at both ends (§9a). The failure modes:
- the spine runs long and dies at a blank wall (`HALL_DEADEND`, `AXIS_UNTERMINATED`);
- the spine is uniform width with no relief for its whole run (`HALL_NO_RELIEF`,
  `PATH_NO_HIERARCHY`);
- the spine is fully enclosed for its whole length, so its area is pure loss
  (`CIRCULATION_ENCLOSED_ONLY`, and it drives the `circulation` score penalty above the 0.15
  free threshold);
- the 33 ft envelope depth means single-aspect rooms can be 20 ft deep with a single window wall
  (`DAYLIGHT_DEPTH`, `DAYLIGHT_SINGLE_ASPECT`).

The last of these deserves emphasis. A 69 × 33 bar has a lot of interior that is far from any
exterior wall. Ching's answer is either to break the depth (a courtyard, an L, a light well) or
to bring light from above (clerestory, monitor roof — which barndsl's `roof_style monitor`
already supports and which is *the* traditional barn answer to exactly this problem). A rule
recommending a monitor or clerestory when interior depth exceeds daylight reach would be a
genuinely architectural suggestion rather than a code check.

### 10.5 When the plan is centralized, do not apply linear rules

Some barndominiums are organized around a dominant great room, with the kitchen, dining, and
bedroom access all hanging directly off it and no corridor at all. This is a **centralized**
organization and it is a legitimate, often excellent, solution — it eliminates corridor area
entirely, which is why the `circulation` score component rewards it.

But several existing and proposed checks assume a spine. `ORG_CLASSIFY` (§6) should gate them:
in a centralized plan, suppress `HALL_DEADEND`-family expectations, and instead check that the
central space is genuinely dominant (`HIERARCHY_NO_PRIMARY_SPACE`), that it is regular in shape
(`ROOM_SQUARE_LARGE` becomes a *positive* here — Ching notes centralized cores want regular,
non-directional form), and that bedroom doors opening onto it are handled for privacy (existing
`BED_PRIVACY`).

### 10.6 Scale collision at the demising wall

The shop wants a 14 ft ceiling; the living side wants 10 ft. Where they meet, the section is
discontinuous, and if the roof is a single gable the shop's tall volume is available "for free"
while the residential side has an attic. Ching's scale discussion (§8c) argues for exploiting
this: a great room vaulted into that available volume is the cheapest possible hierarchy move,
because the structure is already there.

Rules: `CEILING_UNIFORM_FLAT`, `CEILING_SCALE_MISMATCH` (excluding shop/garage).

### 10.7 Proportion at the building scale

The barndominium envelope is typically a **2:1 to 3:1 bar** (the gallery's 69 × 33 is 2.09:1 —
close to Palladio's double square). Post-frame bays give a natural ken of 8–12 ft. Together these
suggest a coherent module discipline: envelope on the 3 ft build module (already enforced), bays
on a fixed spacing, interior partitions landing on bay lines where possible, and room dimensions
snapping to the module.

Rules: `ROOM_OFF_MODULE`, `FRAME_GRID_IGNORED`, `RHYTHM_BAY_IRREGULAR`, `ENVELOPE_ELONGATION`.

### 10.8 Zoning: three zones, not two

The conventional residential split is public/private. A barndominium has three: **shop**,
**public living**, **private living** — and the sequence must be shop → transition → public →
private. Any route that violates that sequence is a defect, and the existing `topology` score
component (15 pts, bedrooms whose only route to the public core crosses a garage/shop) already
encodes the worst violation.

The natural extension is a **zone-sequence check**: for each bedroom, verify the entry→bedroom
route visits zones in non-decreasing privacy order and never re-enters a more public zone after
leaving it. This generalizes `GARAGE_PASSTHROUGH` and `PRIVATE_PASSTHROUGH` into one principle,
and it is directly Ching's linear-organization sequencing applied to program.

Proposed: **`ZONE_SEQUENCE_VIOLATION`** *(info)* — the route to a private room re-enters a more
public zone partway. Detection: label zones with a privacy rank (shop 0, transition 1, public 2,
private 3); walk the entry→room route; flag non-monotonic rank sequences.

---

## 11. Prioritized implementation backlog

Ordered by (value to plan quality) ÷ (implementation cost). All are `info` unless noted.

**Tier 1 — cheap, high signal, low false-positive risk**

1. `DATUM_JOG_MINOR` — near-collinear partitions offset 0.25–2.0 ft. Auto-layout produces these
   constantly; humans never do.
2. `ENTRY_NOT_EMPHASIZED` — main entry with zero distinguishing devices.
3. `ENTRY_HIERARCHY_INVERTED` *(warning)* — overhead door dominates the approach.
4. `RHYTHM_WINDOW_HEADS` — heads differing by 0.25–1.0 ft on one façade. `head_height` already
   exists on the model.
5. `SYMMETRY_NEAR_MISS` — paired rooms differing by 0.5–2.5 ft.
6. `DEMISING_WALL_CONTINUOUS` *(warning)* — the shop/living datum is broken.

**Tier 2 — moderate cost, strong architectural payoff**

7. `DATUM_GRID` — partition-coordinate clustering; also a good continuous score term.
8. `RHYTHM_WINDOW_SPACING` — gap CV outside 0.15/0.45 bands. Closes the recorded IDEAS.md gap.
9. `DAYLIGHT_DEPTH` and `DAYLIGHT_SINGLE_ASPECT` — extend the `daylight` score component from
   area to distribution.
10. `PATH_DIRECTION_CHANGES` (detour index) — extend the `circulation` score component from area
    to route quality.
11. `ENCLOSURE_DEGREE` — turns "open plan" into a number; feeds `KITCHEN_FLOW`.
12. `ROOM_OFF_MODULE` — the ken idea; needs a `module` declaration or a sensible default.

**Tier 3 — needs classification infrastructure first**

13. `ORG_CLASSIFY` and `PATH_CONFIG_CLASSIFY` — labels, not diagnostics, but they gate correct
    application of tiers 1–2 to centralized vs linear plans.
14. `PATH_THROUGH_PUBLIC_ROOM` — axial vs edge distinction; needs route polyline geometry.
15. `AXIS_PRESENT` / `ENTRY_AXIS_ALIGNED` — positive signals; needs centroid + door alignment
    machinery.
16. `ROOM_RATIO_OFF_SYSTEM` — proportion-system coherence across the whole plan.
17. `ZONE_SEQUENCE_VIOLATION` — generalizes the existing passthrough checks.

**Recalibrations of existing rules**

- `ROOM_PROPORTION` / `GOOD_ASPECT`: raise the free band from 1.6 to ~1.70 so Palladio's 5:3
  (1.667) is not penalized; add a bonus for landing within 0.04 of a preferred ratio.
- `HALL_DEADEND`: suppress or reduce when the terminus carries a window (Ching: a view is a valid
  destination).
- `DOOR_CENTERED`: keep as is — residential furniture logic correctly inverts Ching's
  "centered reads stable" for room doors. But do *not* extend the same preference to the primary
  entry or to windows.
- `daylight` component: multiply by an aspect factor (1.0 single-aspect, ~1.15 dual-aspect,
  capped) so distribution counts, not just area.
- `circulation` component: blend in the detour index alongside the hallway-area fraction.

**A note on false positives.** Compositional rules fire on plans that are *fine but unusual* far
more often than code rules do. Every rule above should ship behind the existing
`# barndsl: accept CODE` pragma mechanism, and the ambiguous-band framing used repeatedly here
(`WINDOW_OFFCENTER`, `SYMMETRY_NEAR_MISS`, `DATUM_JOG_MINOR`, `RHYTHM_WINDOW_SPACING`) is
deliberate: flagging "neither clearly one thing nor the other" is much safer than flagging
"not the thing I prefer," and it produces critiques that read as perceptive rather than
dogmatic.

---

## 12. Glossary

**Additive form** — a form produced by attaching volumes to a base volume. Five arrangements:
centralized, linear, radial, clustered, grid.

**Adjacent spaces** — two distinct spaces sharing a boundary; the character of the separating
plane governs the relationship.

**Anthropometry** — dimensioning derived from measurements of the human body.

**Approach** — the phase of the circulation sequence before entry. Frontal, oblique, or spiral.

**Araeostyle / diastyle / eustyle / systyle / pycnostyle** — Vitruvian names for column spacing,
measured in column diameters: 3½+, 3, 2¼, 2, 1½ respectively.

**Articulation** — making the parts of a form legible through material, corner treatment,
lighting, and edge definition.

**Axis** — a line established by two points, about which elements are arranged; wants
termination at both ends.

**Base plane** — the horizontal surface a field of space sits on; can be flush, elevated, or
depressed.

**Centralized organization** — a dominant central space with secondary spaces grouped around it.

**Clustered organization** — spaces grouped by proximity or shared trait, without rigid geometry.

**Datum** — a line, plane, or volume whose continuity and regularity organizes a pattern of other
elements.

**Degree of enclosure** — how completely a space is bounded; governs its form and its relation to
neighbors.

**Dimensional transformation** — altering a form by changing one or more of its dimensions while
retaining identity.

**Golden section** — the ratio φ ≈ 1.618 (reciprocal ≈ 0.618), defined by (a+b)/a = a/b; also
called the divine proportion.

**Grid organization** — spaces organized by a modular three-dimensional field.

**Hierarchy** — articulating relative importance by making an element exceptional in size, shape,
or placement.

**Human scale** — size relative to human dimensions, read through elements of known size (doors,
risers, counters).

**Inaka-ma** — the Japanese "country" method: the 6-shaku ken grid fixes column centers; tatami
sizes vary to fit.

**Interlocking spaces** — two spatial fields that overlap, each retaining identity.

**Intercolumniation** — the spacing between columns, expressed in column diameters.

**Ken** — the Japanese module, standardized at 6 shaku ≈ 1.818 m ≈ 5.97 ft.

**Kyo-ma** — the Japanese "capital" method: tatami size is fixed and column spacing varies.

**Linear organization** — a sequence of spaces in a row or strung along a linear space.

**Modulor** — Le Corbusier's proportioning system, based on a 6 ft (1829 mm) figure with navel at
1130 mm and raised arm at 2260 mm, generating golden-ratio red and blue series.

**Overhead plane** — a horizontal plane above, defining the volume beneath it; roof plane from
outside, ceiling plane from inside.

**Path–space relationship** — how a circulation path relates to the spaces it serves: passing by,
passing through, or terminating in.

**Proportion** — the relation of parts to each other and to the whole; a matter of ratio,
independent of absolute size.

**Radial organization** — a central core with linear arms extending outward.

**Regulating lines** — diagonals used graphically to propagate a single proportion through a
composition; parallel or perpendicular diagonals indicate similar rectangles.

**Rhythm** — unifying movement produced by patterned recurrence of elements.

**Scale** — size relative to a reference. Visual (relative to neighbors), human (relative to the
body), mechanical (relative to a standard unit).

**Space within a space** — a smaller volume wholly contained in a larger one; requires a clear
size differential to read.

**Spatial tension** — a relationship between forms that are near but not touching, established by
proximity or shared traits.

**Subtractive form** — a form produced by removing volume from a base volume.

**Symmetry** — balanced distribution of equivalent elements about an axis (bilateral) or a center
(radial); may be total or local.

**Tatami** — the Japanese floor mat, 1 ken × ½ ken (≈ 1818 × 909 mm); rooms are named by mat
count.

**Transformation** — altering a concept or organization through discrete manipulations without
losing its identity.

**Tsubo** — Japanese area unit, one square ken ≈ 2 tatami ≈ 3.31 m² ≈ 35.6 sq ft.

**Visual inertia** — the degree to which a form appears stable, maximized by a broad base and
symmetry about the vertical.

---

## 13. Sources

Primary source is my working knowledge of Ching, *Architecture: Form, Space, and Order*.
The following were used to verify terminology, taxonomy completeness, and numeric values.

- [Architecture: Form, Space, and Order, 5th Edition — Wiley](https://www.wiley.com/en-us/Architecture:+Form,+Space,+and+Order,+5th+Edition-p-9781119853381) — edition scope; the 5th (2023) adds contemporary precedents, digital-technology and sustainability material, and more diverse geographic examples, but does not change the seven-chapter structure or any taxonomy used here.
- [Full text, 3rd edition — Internet Archive](https://archive.org/stream/FrancisD.K.ChingArchitectureFormSpaceAndOrder3rdEdition/Francis+D.K.+Ching,+Architecture+-+Form,++Space+and+Order++3rd+Edition_djvu.txt) — chapter 6 section headings confirmed; the OCR text available online is truncated before the chapter 6 numeric content.
- [Full text, 4th edition (2014) — Internet Archive](https://archive.org/details/francis-d.-k.-ching-architecture-form-space-and-order-4-e-2014) — chapter 4 and 7 taxonomy lists confirmed.
- [Chapter 3 summary — Beckwith House Interiors](https://beckwithhouseinteriors.wordpress.com/2020/01/26/architecture-form-space-order-by-francis-d-k-ching-chapter-three/) — horizontal/vertical space-defining elements; openings within planes, at corners, between planes; elevated base plane continuity thresholds (secondary source — see caveat in §5a).
- [Chapter 4 summary — Beckwith House Interiors](https://beckwithhouseinteriors.wordpress.com/2020/01/30/architecture-form-space-order-by-francis-d-k-ching-chapter-four-organization/) — spatial relationships and organizations.
- [Chapter 5 summary — Beckwith House Interiors](https://beckwithhouseinteriors.wordpress.com/2020/02/10/architecture-form-space-order-by-francis-d-k-ching-chapter-five/) — circulation elements.
- [Chapter 6 summary — Beckwith House Interiors](https://beckwithhouseinteriors.wordpress.com/2020/02/17/architecture-form-space-order-by-francis-d-k-ching-chapter-six-proportion-and-scale/) — the seven proportioning systems.
- [Chapter 7 summary — Beckwith House Interiors](https://beckwithhouseinteriors.wordpress.com/2020/02/20/form-space-order-by-francis-d-k-ching-chapter-7-principles/) — ordering principles and sub-types.
- [Elements of Circulation in Architecture — Layak Architect](https://layakarchitect.com/circulation/) — approach, entrance, path configuration, and path–space sub-type names.
- [Modulor — Wikipedia](https://en.wikipedia.org/wiki/Modulor) and [Corbusier's Modulor — DT Online](https://wiki.dtonline.org/index.php/Corbusier's_Modulor) — 1829 mm / 1130 mm / 2260 mm figures, red and blue series.
- [Le Corbusier – the Modulor — ETH Library](https://library.ethz.ch/en/collections-and-archives/platforms/virtual-exhibitions/fibonacci-un-ponte-sul-mediterraneo/reception-of-fibonacci-numbers-and-the-golden-ratio/le-corbusier-the-modulor.html) — Fibonacci and golden-ratio basis.
- [The Ken System — Mysteries of the Carpenter](https://mysteriesofthecarpenter.ca/2023/09/11/the-ken-system/) and [Tatami — Kyō-machiya](https://kyomachiya.jimdofree.com/architecture/interior/tatami/) — ken standardization at 6 shaku / 1.818 m, inaka-ma vs kyo-ma, mat dimensions.
- [Palladio: The Proportions of Rooms — About Scotland](http://www.aboutscotland.com/harmony/prop3.html) and [Palladio's Room Proportions: The Harmonic Mean](http://www.aboutscotland.co.uk/harmony/prop6.html) — the seven room shapes and the three means for ceiling height.
- [Parametric Experiments on Palladio's 5 by 3 Villas — Nexus Network Journal](https://link.springer.com/article/10.1007/s00004-022-00592-1) — the six rectangle ratios stated numerically (1:1, √2:1, 4:3, 3:2, 5:3, 2:1).
- [Proportioning Systems — Olson & Baker](https://olsonbaker.com/blog-post-a-short-history-proportioning-systems/) — golden section, classical orders, Renaissance theory context.
- [Theory of Proportion — Archi-Monarch](https://archi-monarch.com/theory-of-proportion/) — Ching's seven-system list.

### Related barndsl files

- `D:\CodeProjects\Architecture-DSL\src\barndsl\score.py` — the eight score components these rules would feed.
- `D:\CodeProjects\Architecture-DSL\src\barndsl\diagnostics.py` — the diagnostic registry (code, severity, category, title, explanation).
- `D:\CodeProjects\Architecture-DSL\src\barndsl\validation.py` — `door_graph()`, `_dq_*` design-quality checks.
- `D:\CodeProjects\Architecture-DSL\src\barndsl\constants.py` and `profiles.py` — existing thresholds.
- `D:\CodeProjects\Architecture-DSL\docs\DIAGNOSTIC_MATRIX.md` — the current 212-code surface.
- `D:\CodeProjects\Architecture-DSL\docs\MODEL_INVARIANTS.md` — coordinate and unit conventions (decimal feet, SW-corner origin).
- `D:\CodeProjects\Architecture-DSL\docs\IDEAS.md` — records façade rhythm and even window spacing as explicitly deferred; §5 and §9 above propose objective formulations.
