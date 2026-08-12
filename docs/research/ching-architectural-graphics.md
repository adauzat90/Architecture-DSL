# Architectural Graphics (Francis D. K. Ching) — Research Notes for barndsl

**Purpose:** translate the drawing conventions of professional architectural practice into
concrete, implementable rules for `barndsl`'s SVG floor-plan renderer, its glTF 3D output, and
the validation/scoring layer that judges a generated plan.

**Audience:** a developer or AI agent writing renderer code, rule predicates, and scoring
heuristics. Every theme below ends with a **Rule candidates** block — the actionable part.

**Sourcing note:** everything here is paraphrased. Primary source is Ching's *Architectural
Graphics* (editions 4–7; the 6th, 2015, Wiley, is the one most often cited and is the edition
this document tracks). Where the book is silent — notably on dimensioning mechanics, lettering
metrics, and the fixture/appliance symbol library — the conventions come from general US
practice (AIA/NCS-style office standards, *Architectural Graphic Standards*, and Ching's
companion volume *Building Construction Illustrated*), and are flagged as such.

---

## 1. Book overview, and why graphic convention is a software problem

### 1.1 What the book is

*Architectural Graphics* is the standard first-year primer on how architects draw. It is not a
style guide and not a code book; it is a grammar. Its claim is that a drawing is a
**language**, and that a reader who knows the language extracts three-dimensional, material,
and spatial information from flat marks without being told. The 6th edition's chapter spine is
roughly:

1. Drawing tools and materials
2. Architectural drafting (line weights, line types, lettering, basic craft)
3. Architectural drawing systems (the taxonomy: multiview / paraline / perspective)
4. Multiview drawings (plans, sections, elevations)
5. Paraline drawings (isometric, dimetric, trimetric, oblique)
6. Perspective drawings (one-, two-, three-point)
7. Rendering tonal values (value, texture, shade and shadow)
8. Rendering context (site, entourage, people, vegetation)
9. Architectural presentations (sheet composition and hierarchy)
10. Freehand drawing

**Edition differences that matter to us.** The 4th edition is the classic hand-drafting text.
The 5th (2009) added material on computer-generated graphics and updated representation
conventions. The 6th (2015) is the one to cite: it explicitly reworked the explanations of
**line weights, scale, and dimensioning**, added coverage of orthographic projection *derived
from 3D models* (exactly our situation — we have a model and must project it), and shipped
web animations. The 7th (2023/24) adds beginner perspective/sketching guidance and more on the
analog→digital transition. For rule-writing purposes 5th/6th/7th are interchangeable; if a
number differs, prefer the 6th.

### 1.2 Why an auto-generating program must care

A generated plan can be *geometrically correct* and still read as amateur. The failure modes
are almost entirely graphic, not geometric:

- **Uniform line weight.** If every stroke is the same width, the eye gets no depth cue and
  cannot separate "wall I am cutting through" from "countertop below" from "roof above." This
  is the single loudest amateur tell.
- **No poché.** Unfilled wall outlines make the drawing read as a wireframe diagram, not a
  building. Filled cut walls create the figure–ground reading that makes rooms look like
  *rooms* — void carved out of solid.
- **Wrong symbols.** A door drawn as a gap, a window drawn as a gap, a stair drawn as a plain
  rectangle: each destroys information the reader expects for free.
- **Text that fights the drawing.** Labels overlapping walls, sub-legible font sizes, mixed
  fonts, dimension text on top of dimension lines.
- **Missing meta-graphics.** No north arrow, no scale, no title. A plan with no orientation and
  no scale is an illustration, not a drawing.

For barndsl specifically there is an added wrinkle: a **barndominium** is a hybrid — finished
living quarters plus large open shop/garage bays under one rectangular envelope. The two halves
want *different graphic treatment at the same scale*: the residential side is dense with small
rooms, doors, and plumbing fixtures; the shop side is a large void with a slab, overhead doors,
and maybe a mezzanine overhead. A renderer tuned only for one reads badly for the other. Most
of the rule candidates below are scale- and zone-aware for this reason.

Finally, the SVG output is not paper. It is resized arbitrarily by browsers and embedded in
review UIs. That pushes us toward conventions that survive rescaling (graphic scale bars,
non-scaling strokes) and away from ones that do not (a bare `1/4" = 1'-0"` note).

---

## 2. Drawing systems: the taxonomy

### 2.1 The three systems

Ching organizes all architectural drawing into three projection systems, distinguished by
where the projectors go and where the picture plane sits.

| System | Projectors | Picture plane | Key property |
|---|---|---|---|
| **Multiview (orthographic)** | Parallel, perpendicular to picture plane | Parallel to a principal face | True size/shape in the plane shown; no depth |
| **Paraline (axonometric/oblique)** | Parallel, oblique to picture plane | Arbitrary | Measurable in 3 dimensions; no convergence |
| **Perspective (linear)** | Converging on a station point | Between eye and object | Looks like vision; nothing measurable |

Multiview drawings are the *contract* drawings — plans, sections, elevations. They are what
barndsl's SVG is. Paraline drawings are the analytic/explanatory drawings — the natural fit for
a "cutaway axon" export from our 3D model. Perspective is the persuasion drawing — what a glTF
viewer produces when the user orbits the model.

Multiview's defining limitation is that no single view carries depth, so information is
*distributed across a set* and the reader reassembles it. That is why alignment between views
matters so much (§10), and why a plan alone under-communicates: it must carry annotation
(dashed overhead lines, labels) to compensate.

### 2.2 Paraline drawings — specifics

- **Isometric:** the three principal axes make equal angles with the picture plane; the two
  horizontal axes are drawn at **30° above horizontal**, giving 120° between all three axes.
  All axial lines are drawn to the **same scale, at true length** — this is what makes an
  isometric measurable. Faces are equally emphasized, which also makes it slightly bland.
- **Dimetric:** two axes equally foreshortened, third different. **Trimetric:** all three
  foreshortened differently. Rare in practice.
- **Plan oblique (also called "military"):** take the true floor plan, rotate it — commonly
  **45°/45°** (equal emphasis to both visible facades) or **30°/60°** (emphasizes the long
  facade) — then project heights straight up. The plan stays **true shape and true scale**,
  which is why this is the architect's default axon: you can build it directly from the plan
  file with a shear and a rotation, and measure the plan off it.
- **Elevation oblique:** one vertical plane stays true; receding lines project back. Good for
  facades with strong frontal composition.
- **Foreshortening of receding lines in obliques:** may be drawn full scale, or reduced to
  **3/4 or 1/2** true length to fight the apparent elongation that full-scale receding lines
  produce.
- **Vertical axis is always vertical on the sheet** in plan obliques and (conventionally) in
  architectural isometrics. Never tilt the building's vertical.
- **Viewpoint choice:** a "worm's-eye" plan oblique (looking up) is a legitimate and common
  variant used to show ceilings/roof structure.

### 2.3 Perspective — specifics

- **One-point:** picture plane parallel to one principal face. Good for interiors, symmetric
  approach views, and long corridors/bays.
- **Two-point:** picture plane vertical, parallel to the vertical axis only. The default
  exterior view.
- **Three-point:** picture plane oblique to all three axes; verticals converge. Used for
  extreme up/down views; rarely appropriate for a two-story barndominium.
- **Horizon line = eye level.** Standing eye height ~**5'-6"** (1.68 m) for exteriors; seated
  ~**3'-9"** for interiors. Putting the horizon at eye level is what makes a rendering feel
  inhabited rather than aerial.
- **Cone of vision:** keep the subject inside roughly a **60° cone** (some sources say 30°
  half-angle; a tighter **45–50°** total is safer) measured from the station point. Outside
  that, marginal distortion becomes obvious — circles go egg-shaped, corners splay.
- Practical corollary: station point distance ≈ **1.5–2× the width of the subject**.

### Rule candidates — drawing systems

- **DS-1.** The SVG deliverable is a multiview *plan*. Do not mix perspective or "3D-ish"
  effects into it (no faux-depth gradients on walls, no perspective furniture icons).
- **DS-2.** If barndsl ever emits a 3D-derived 2D graphic, prefer **plan oblique at 45°/45°**:
  it can be generated directly from the existing plan geometry (`x' = x + z·cos45`,
  `y' = y − z·sin45` after rotating the plan), keeps the plan measurable, and needs no new
  projection machinery.
- **DS-3.** For any isometric export, use **30°/30°** horizontal axes and draw all three axes
  at equal, unforeshortened scale.
- **DS-4.** For the glTF default camera, place a **two-point-like** exterior view: camera
  height 5'-6" (1.676 m) for a ground-level hero view, target the envelope centroid, and set
  camera distance ≥ 1.5× the envelope's long dimension so the whole building sits inside a ~50°
  horizontal FOV. Keep the camera's up-vector world-up so verticals stay vertical (i.e. avoid
  accidental three-point).
- **DS-5.** Never tilt the vertical axis in any generated axonometric.
- **DS-6.** Because a plan alone under-communicates, any element the plan cut cannot show
  (mezzanine over the shop, roof overhang, ceiling height changes, overhead door tracks) must
  be *annotated onto the plan* per §3, not silently dropped.

---

## 3. Floor plan conventions: what the plan cut actually shows

### 3.1 The cut

A floor plan is a **horizontal section**. Imagine slicing the building with a horizontal plane
and removing everything above it, then looking straight down.

- **Cut height: approximately 4 feet (about 1.2 m) above the finish floor.** Ching states this
  as an approximation that varies with the design. Four feet is chosen because it reliably
  passes above countertops (36") and below door heads (6'-8") and typical window heads (6'-8"
  to 7'-0"), and through most window bodies (sill 30"–36").
- The cut plane may be **stepped or shifted locally** where a strict 4'-0" plane would miss
  something important — a high clerestory, a window with a 5'-0" sill, a transom. The
  convention is to show the information rather than obey the plane pedantically.
- What the plane passes through is drawn as **cut**; everything below it and visible is drawn
  as **seen**; everything above it that matters is drawn **dashed** (see §4.3).

### 3.2 The three-tier reading

| Category | Examples | Graphic treatment |
|---|---|---|
| **Cut** | Walls, columns/posts, door and window openings' jambs, stair at the break | Heaviest continuous profile line + poché fill |
| **Seen below the cut** | Floor, counters (36"), vanities, tub rims, base cabinets, appliances, furniture, sills, treads, thresholds | Medium line, no fill (or very light fill) |
| **Above the cut (hidden)** | Mezzanine/loft edge, upper cabinets (12" deep), lowered ceilings, beams, skylights, roof overhang, overhead door track, wall openings above head height | Dashed line, light |
| **Surface, no form change** | Floor tile/board pattern, slab control joints, area rugs | Lightest line, sometimes gray rather than black |

Ching's key gradient rule: **the farther a horizontal surface lies below the plane of the cut,
the lighter its line weight.** A countertop at 36" is drawn heavier than the floor at 0". This
gives the plan a soft depth reading without any shading.

### 3.3 Poché

**Poché** is the darkening of cut solids. Its job is figure–ground: the solid mass goes dark,
the inhabitable void stays light, and the plan reads instantly as space carved from matter.

- At **small scales** (1/16", 1/8"): fill cut walls essentially **solid black or near-black**.
  There is no room to draw material and the solid mass is the whole point.
- At **larger scales** (1/4" and up): a solid black fill becomes visually overbearing and
  crowds out the detail you now have room for. Use a **middle gray**, or a material hatch, so
  cut elements stay dominant without swamping the sheet.
- The inverse scheme (light cut elements on a dark field) is legitimate for presentation
  drawings; it is not the default for working drawings.
- Digital tools make large flat tonal areas trivial, which removes the historical excuse for
  outlining walls and leaving them empty.

**Material hatch** in the cut (used mostly at 1/4" and larger): 45° diagonals for steel/metal;
brick coursing pattern for masonry; a speckle/aggregate pattern for concrete; diagonals plus
core dots for CMU; wood framing often left blank with a light outline, or shown with
member-by-member framing at very large scale.

### 3.4 Openings must break the wall

An opening in a wall is not the wall with a symbol drawn on top of it. The poché **stops at
each jamb**; the opening is a genuine gap in the filled mass, closed by the door/window symbol.
Getting this wrong — running the wall fill straight through the opening — is a hallmark of
naive generated plans.

### 3.5 Wall corners and junctions

Cut walls that meet must read as **one continuous mass**. The profile line around the outside
must be continuous and unbroken; there must be no leftover internal line where two wall
segments intersect. In geometry terms: **union the wall footprint polygons before rendering**,
then stroke the boundary of the union, not the boundary of each rectangle.

Ching's related rule for line hierarchy: a heavy cut profile line **should not terminate at or
be interrupted by a lighter line**. The heavy outline is a closed loop.

### 3.6 Barndominium-specific plan content

- **Post-frame / pole-barn posts:** if the envelope is post-frame, the posts are cut elements.
  Draw them poché'd at their real size (commonly 3-ply 2×6 ≈ 4.5"×5.5", or 6×6) at their real
  spacing (typically **8'-0" o.c.**, sometimes 10' or 12'). They should sit on a column
  centerline grid (§8.4).
- **Overhead (sectional) doors** on shop bays: the opening is a wall gap like any other, but
  there is **no swing arc**. Convention is a light line across the opening for the closed door
  panel, plus **dashed lines running perpendicular into the bay** for the horizontal track
  (typically ~ door height + 2' of track). Label with size, e.g. `14'-0" × 14'-0" OH DOOR`.
- **Slab:** shop bays are usually a bare slab; show control joints as very light lines and
  annotate slope-to-drain if modeled. A floor drain is a small circle with a light dashed
  slope indication.
- **Mezzanine / loft over the shop:** entirely above the 4' cut. Draw the mezzanine edge
  **dashed** with a label (`MEZZANINE ABOVE`), and its stair down in the plan below.
- **The living/shop demising wall** is usually a fire-separation and often thicker than
  interior partitions. Its thickness should be modeled and drawn true, not normalized to the
  generic partition thickness.
- **Ceiling height changes** between the residential side (8'–10') and the shop (14'–20') are
  invisible in plan. Annotate with a ceiling-height note per zone (`CLG 9'-0"`, `CLG 16'-0"`).

### Rule candidates — floor plan

- **FP-1.** Define a single constant `PLAN_CUT_HEIGHT = 4'-0"` (1219 mm) and derive
  cut/seen/hidden classification from it programmatically, rather than hard-coding which
  element types are dashed.
- **FP-2.** Classify every element into exactly one of four render tiers — `CUT`, `SEEN`,
  `HIDDEN_ABOVE`, `SURFACE` — and drive stroke width, dash pattern, and fill from the tier, not
  from the element type. This is the core of the line-weight system in §4.
- **FP-3.** Any element whose top is below the cut plane and whose top is visible from above is
  `SEEN`. Any element whose bottom is above the cut plane is `HIDDEN_ABOVE`. Any element
  spanning the cut plane is `CUT`.
- **FP-4.** Within `SEEN`, modulate stroke width by height above floor: a countertop at 36" gets
  the upper end of the SEEN band, floor-level items the lower end. A two-step ramp is enough;
  don't over-engineer a continuous function.
- **FP-5.** Union all wall/post footprint polygons into one (or a few) multi-polygons before
  emitting SVG. Stroke only the union boundary. No internal seams at wall intersections.
- **FP-6.** Openings subtract from the wall polygon *before* the union is stroked, so the poché
  genuinely stops at the jambs and the jamb faces are part of the profile loop.
- **FP-7.** Poché fill is scale-dependent: at effective scale ≤ 1/8"=1'-0", fill near-black
  (e.g. `#1a1a1a`); at ≥ 1/4"=1'-0", fill mid-gray (e.g. `#808080` or a 45° hatch) so the
  drawing does not go muddy.
- **FP-8.** Emit SVG in strict paint order so cut walls always sit on top:
  `floor fill → floor surface pattern → furniture/fixtures → opening symbols → wall poché →
  wall profile stroke → dimensions → annotation/text → title & meta-graphics`.
- **FP-9.** For post-frame envelopes, emit posts as discrete cut elements at true size and
  spacing, and place a column centerline grid through them.
- **FP-10.** Overhead doors render with no swing arc: opening gap, light panel line, dashed
  track lines extending into the bay a distance of (door height + 24"), plus a size label.
- **FP-11.** Emit a ceiling-height annotation per zone whenever adjacent zones differ in clear
  height by more than 12".
- **FP-12.** Anything above the cut that the design depends on (mezzanine, roof overhang,
  clerestory, beams over the bay) must be emitted as a dashed `HIDDEN_ABOVE` outline with a
  `... ABOVE` label. If a plan has a mezzanine in the model and no dashed edge in the SVG, that
  is a renderer bug, and a scoring penalty is justified.

---

## 4. Line weights and line types

### 4.1 The weight hierarchy

Ching organizes lines into four tiers, described in the book by pencil lead grade but mapping
cleanly onto stroke widths:

| Tier | Purpose | Ching's lead grades |
|---|---|---|
| **Heavy** | Profile of cut elements; spatial edges | H, F, HB, B (or repeated passes with 0.5 mm) |
| **Medium** | Edges of objects; intersections of planes | H, F, HB |
| **Light** | Material/texture change with no change of form | 2H, H, F |
| **Very light** | Construction lines, grids, guidelines, fine texture | 4H, 2H, H, F |

The **ISO 128 pen series** is the numeric backbone that survived into CAD:

`0.13 — 0.18 — 0.25 — 0.35 — 0.50 — 0.70 — 1.00 — 1.40 mm`

Each step is roughly **√2 (≈1.41×)** larger than the previous, so adjacent weights stay
distinguishable after photocopying or downscaling. A conventional architectural assignment:

| Width | Use |
|---|---|
| 0.13 mm | Hatch, centerlines, fine texture, construction geometry |
| 0.18 mm | Dimension lines, extension lines, text/leaders, furniture |
| 0.25 mm | Objects in elevation, secondary detail, fixtures |
| 0.35 mm | General object outlines, doors, seen edges |
| 0.50 mm | Walls / primary cut lines in plan |
| 0.70 mm+ | Section cut lines, building profile, title underlines |

The critical insight for code: **the ratio matters more than the absolute numbers.** Ching's
own framing is relative — three legible tiers (cut / seen / reference), each roughly double the
one below. If you get the ratios right the drawing works at any output size.

### 4.2 The available range shrinks with scale

At a small scale there is less room, so the usable range of line weights compresses. At
1/16"=1'-0" you have maybe three distinguishable weights; at 1/2"=1'-0" you have six. A
renderer that uses a fixed six-tier palette at every zoom level will produce a black smear at
small scale and an anemic drawing at large scale.

### 4.3 Line types

| Type | Meaning | Typical pattern |
|---|---|---|
| **Solid** | Object edges, plane intersections, cut profiles | continuous |
| **Dashed (hidden)** | Elements above the cut plane, or otherwise hidden/removed | ~1/8" dash, 1/16" gap on paper |
| **Short dash / dotted** | Elements hidden *below* (buried, under-slab, footings) | shorter dashes than "above" |
| **Centerline** | Axes of symmetry, column grids, centered dimensions | long — short — long (dash-dot) |
| **Property line** | Legal boundary | long dash with **two** dots/short dashes |
| **Break line** | Interrupts a drawing where the object continues | long segments joined by a zigzag |
| **Cutting-plane / section line** | Where a section is taken | heavy long dashes + short dashes, with direction arrows |
| **Match line** | Where a plan is split across sheets | heavy, labeled |
| **Utility line** | Buried service runs | long dashes interrupted by a letter (`W`, `G`, `E`, `S`) |

A useful discipline: **two dash patterns must not be confusable.** If "above" and "below" both
use dashes, one of them must be visibly different (dot vs. dash, or half the period).

### 4.4 SVG-specific translation

- Stroke widths defined in **model units** scale with the drawing when you set a `viewBox`,
  which means a plan zoomed to fit a small viewport gets hairlines and a zoomed-in one gets
  fat slabs. Two ways out:
  1. Set widths in model units and *recompute* them from the effective plot scale (correct for
     a fixed-scale plot; deterministic; preferred when you know the target scale).
  2. Use `vector-effect="non-scaling-stroke"` so widths are interpreted in the outer viewport's
     coordinate system and stay visually constant regardless of the `viewBox` transform
     (correct for an interactive/zoomable web viewer).
  Pick one deliberately per output mode; mixing them is where inconsistent drawings come from.
- `stroke-linejoin="miter"` with a sane `stroke-miterlimit` (4–10) for wall corners; a round
  join makes cut walls look soft and wrong.
- `stroke-linecap="butt"` for dimension/extension lines so tick marks land exactly; `"round"`
  only for freehand-style presentation output.
- `shape-rendering="geometricPrecision"` on the plan group; `crispEdges` causes visible
  snapping on thin diagonals.
- `stroke-dasharray` is in the *current* user coordinate system, so dash patterns need the same
  scale treatment as widths, or dashes at small scale collapse into a solid line.

### Rule candidates — line weights and types

- **LW-1.** Define one named, ordered weight palette and reference it symbolically everywhere.
  A workable default, expressed in paper millimetres:

  | Token | Paper mm | px @96dpi | Applies to |
  |---|---|---|---|
  | `W_PROFILE` | 0.70 | 2.65 | Outer building envelope profile |
  | `W_CUT` | 0.50 | 1.89 | Cut wall/post outlines, stair break |
  | `W_OBJECT` | 0.35 | 1.32 | Doors, seen edges at/near counter height |
  | `W_SEEN` | 0.25 | 0.94 | Fixtures, appliances, treads, sills |
  | `W_FINE` | 0.18 | 0.68 | Dimension & extension lines, leaders, furniture |
  | `W_HAIR` | 0.13 | 0.49 | Hatch, centerlines, floor pattern, grid |

  Ratio between adjacent tokens ≈ 1.4. Never emit a raw numeric width in renderer code.
- **LW-2.** Enforce a hard floor of **0.5 device px** on any emitted stroke; below that,
  antialiasing turns lines into pale gray fog. If the computed width falls under the floor,
  drop the tier entirely (see LW-3) rather than drawing an invisible line.
- **LW-3.** Make the palette **scale-adaptive**. Above 1/4"=1'-0" use all six tokens; at
  1/8"=1'-0" collapse to four (`W_PROFILE`, `W_CUT`, `W_SEEN`, `W_FINE`); at 1/16"=1'-0" or
  smaller collapse to three and suppress floor patterns, hatch, and furniture entirely.
- **LW-4.** The building envelope profile is one closed path at `W_PROFILE`, drawn last among
  geometry so nothing overlaps it. Interior cut walls are `W_CUT`.
- **LW-5.** Dash patterns, as paper millimetres, scaled identically to stroke widths:
  - hidden-above: `stroke-dasharray: 3.2 1.6`
  - hidden-below/buried: `stroke-dasharray: 1.6 1.6`
  - centerline: `stroke-dasharray: 9.5 1.6 1.6 1.6`
  - property line: `stroke-dasharray: 9.5 1.6 1.6 1.6 1.6 1.6`
  - section/cutting plane: `stroke-dasharray: 12.7 3.2 3.2 3.2` at `W_PROFILE`
- **LW-6.** Break lines are geometry, not a dash pattern: emit an explicit polyline with a
  zigzag (or a double-jog "Z") of amplitude ≈ 3× the local stroke width.
- **LW-7.** All plan strokes are the same color (near-black, e.g. `#111`) with *weight* — not
  color — carrying the hierarchy. Reserve color for optional overlay layers (zoning diagrams,
  diagnostics), and make those a toggleable SVG group so the base drawing stays monochrome.
- **LW-8.** Set `stroke-linejoin="miter"`, `stroke-linecap="butt"`,
  `shape-rendering="geometricPrecision"` once on the root plan group.
- **LW-9.** Choose the stroke-scaling strategy per output mode and record it: fixed-scale plot
  → widths recomputed from plot scale; interactive viewer → `vector-effect="non-scaling-stroke"`.

---

## 5. Architectural symbols

Symbols are a shared vocabulary. Deviating from them costs the reader more than any aesthetic
gain gets back. Ching covers doors, windows, and stairs in the plan chapter; the fixture,
appliance, and electrical libraries below are standard US office practice
(*Architectural Graphic Standards* lineage), not from this book.

### 5.1 Doors

- Show **location, width, and mode of operation** (swing, slide, fold, pocket, overhead).
- **Swinging door:** the leaf is drawn as a thin rectangle **perpendicular to the wall** (i.e.
  fully open at 90°), hinged at one jamb; a **quarter-circle arc** of radius equal to the leaf
  width sweeps from the leaf's free edge back to the closed position at the opposite jamb. The
  hinge is where the leaf meets the wall — a detail naive readers reverse.
- At **1/4"=1'-0" and larger**, show the **thickness of the door leaf and the jambs**; below
  that a single line for the leaf is fine.
- **Double doors:** two leaves, two arcs, mirrored about the opening center.
- **Sliding / bypass:** no arc. Two overlapping panel lines offset within/beside the opening,
  with the track shown.
- **Pocket:** panel line that runs into the wall cavity, drawn dashed inside the wall.
- **Bifold:** two (or four) short panel lines forming a shallow V or paired triangles in the
  opening.
- **Overhead sectional (shop/garage):** no arc; panel line across the opening plus dashed
  horizontal track running into the space.
- **Cased opening / no door:** jambs only, opening left clear.
- **Threshold** line at exterior doors, drawn light.
- **Door tags** (`101`, `A`) in a circle or hexagon reference the door schedule.

### 5.2 Windows

- Show **location and width**; indicate **jambs and mullions where scale permits**.
- The **sill is not cut** — it lies below the cut plane — so it is drawn with a **lighter weight
  than the cut wall**. This is the most commonly botched detail in generated plans, which tend
  to draw the whole window symbol at wall weight.
- Common plan symbol: the wall poché stops at each jamb; within the opening, **three parallel
  lines** — the two outer ones the interior and exterior wall faces / frame, the center one the
  glazing plane. Two-line and five-line variants exist; consistency matters more than count.
- **Operation (casement, double-hung, awning, fixed) is shown in elevation, not in plan.** Plan
  carries only width, location, and glazing plane. If barndsl needs to convey operation on the
  plan it must use a **tag or note**, not an invented plan glyph.
- Exterior sill and any interior stool/apron project past the wall face; draw them at
  `W_SEEN`.

### 5.3 Stairs

- Draw the **treads and landings** as a series of parallel lines perpendicular to travel. Riser
  *height* is not shown in plan.
- A **direction arrow** runs along the stair's centerline of travel. Modern convention: the
  arrow points in the direction of **ascent (UP)**, terminating the centerline. Label `UP` or
  `DN` in **uppercase** (the caps convention exists precisely because `up`/`dn` can be misread
  when the sheet is rotated). Include the riser count: `14R UP`.
- A **break line** — a zigzag or a pair of parallel diagonal lines — crosses the run where the
  4'-0" cut plane intersects the stair. Everything beyond the break is above the cut and is
  either omitted or drawn light/dashed. With a 7"-ish riser, the break lands around the
  **6th–7th riser**.
- The **diagonal break line must be visually distinct from the tread lines** so the reader does
  not mistake it for a tread.
- Handrails are drawn as a pair of light lines; they are *not* cut walls and must not get wall
  weight.
- At larger scales, show nosings, handrail returns, and the toe space under a landing.
- Typical residential geometry to draw against: riser **7"–7 3/4"**, tread **10"–11"** plus
  nosing, min clear width **36"**, headroom **6'-8"**. A 9'-0" floor-to-floor needs ~15 risers.

### 5.4 Plumbing fixtures (footprints, US residential)

Draw the fixture outline plus a minimal interior line or two (bowl, basin, drain) — enough to
be recognizable, not a product illustration. All `SEEN` tier.

| Fixture | Plan footprint (in) | Notes |
|---|---|---|
| Water closet | 20 W × 28–30 D | Round-front vs elongated; tank at wall |
| Lavatory (wall-hung) | 20 W × 18 D | |
| Vanity (single) | 30–36 W × 21–24 D | Counter at 30–36" AFF |
| Vanity (double) | 60–72 W × 21–24 D | |
| Bathtub | 60 L × 30–32 W | 66×32 and 72×36 soaking variants |
| Shower (alcove) | 36 × 36 std; 32 × 32 min; 36 × 48, 48 × 48, 60 × 36 | Draw drain + door swing/curtain |
| Kitchen sink (double) | 33 W × 22 D | Sits in a 24"-deep base cabinet |
| Kitchen sink (single) | 24 W × 21 D | |
| Laundry / utility sink | 24 × 22 | |
| Water heater | 20–24 dia. | Circle; annotate gallons |
| Floor drain | 4–6 dia. | Circle + light slope indication |

Clearance conventions worth *drawing* (as light dashed zones) when the renderer is in
diagnostic mode: 15" min from WC centerline to any side obstruction, 21" min clear in front of
a WC or lavatory, 30" min clear in front of a shower/tub entry.

### 5.5 Appliances and casework

| Item | Plan footprint (in) |
|---|---|
| Refrigerator | 33–36 W × 30–36 D (incl. door) |
| Range / cooktop | 30 W × 25 D (36" and 48" pro ranges exist) |
| Wall oven | 30 W × 24 D |
| Dishwasher | 24 W × 24 D |
| Microwave (OTR) | 30 W × 15–17 D — **above the cut, draw dashed** |
| Washer / dryer | 27–28 W × 30–32 D each |
| Base cabinet | 24 D (counter 25–25.5 D with overhang) |
| Upper cabinet | 12–13 D — **above the cut, draw dashed** |
| Tall pantry | 24 D |
| Kitchen island | Draw base at 24–48 D; min aisle 42" (36" absolute) |

Furniture, when shown, is `SEEN`/`W_FINE` and is deliberately simplified: bed queen 60×80,
king 76×80, twin 38×75; sofa 84×36; dining table 42×72; car 6'×16' (draw it in the shop bay to
prove clearance — a genuinely useful barndominium graphic).

### 5.6 Electrical

Electrical symbols in residential practice are drawn on the floor plan (small jobs) or on a
separate power/lighting plan (larger jobs). They are **schematic, not to scale**, and are
placed at the wall line.

- Duplex receptacle: a small circle on the wall line with two short parallel strokes.
- 220 V receptacle: same with three strokes, or a filled variant.
- GFCI: receptacle symbol annotated `GFCI`.
- Single-pole switch: `S`; three-way `S3`; four-way `S4`; dimmer `SD`.
- **Switch legs** are drawn as **curved dashed arcs** from the switch to the fixture(s) it
  controls; the curve distinguishes them from hidden-object dashes.
- Ceiling fixture: circle; recessed can: circle with `R` or a smaller solid circle; wall sconce:
  half-circle on the wall; fluorescent/linear: elongated rectangle.
- Ceiling fan: circle with two crossed blade lines.
- Smoke detector: `SD` in a circle; CO detector `CO`.
- Panel: a filled rectangle on the wall, labeled.

### Rule candidates — symbols

- **SY-1.** Every door renders its **operation type**. Swinging doors: leaf rectangle
  perpendicular to the wall at the hinge jamb + a 90° arc of radius = leaf width from the leaf
  tip to the strike jamb. Never render a door as a bare gap.
- **SY-2.** Draw leaf and jamb *thickness* only when effective scale ≥ 1/4"=1'-0"; below that,
  a single-line leaf.
- **SY-3.** Arc stroke = `W_FINE` or `W_SEEN`; the leaf itself = `W_OBJECT`. The arc must never
  be as heavy as the wall.
- **SY-4.** Sliding, pocket, bifold, and overhead doors get their own symbols and **no arc**.
  Overhead doors additionally get dashed track lines into the space.
- **SY-5.** Doors record a **hand** (left/right) and a **swing direction** (in/out) in the model;
  the renderer must honor both, and the validator should flag a swing that collides with a
  fixture, another door's arc, or a hallway less than the leaf width away.
- **SY-6.** Windows render as: poché stopped at jambs + three parallel lines across the opening
  (outer two at `W_SEEN` for frame/wall face, center glazing line at `W_FINE`). Sill/stool
  projections at `W_SEEN`. Never at wall weight.
- **SY-7.** Do not invent plan glyphs for window operation. If operation must appear, emit a
  window tag keyed to a schedule/legend.
- **SY-8.** Stairs render treads at true tread depth, a centerline **arrow pointing up**, an
  uppercase `UP`/`DN` label with riser count (`15R UP`), and a **zigzag break line** at the
  riser nearest 4'-0" of rise. Tread lines at `W_SEEN`; the break at `W_CUT`; handrails at
  `W_FINE`.
- **SY-9.** Maintain a single fixture/appliance symbol table with real footprints (§5.4–5.5).
  Symbol geometry should be *derived* from the footprint, not hand-drawn per instance, so a
  36"-wide range and a 30"-wide range render consistently.
- **SY-10.** Anything mounted above 4'-0" — upper cabinets, over-range microwave, mezzanine
  edge, soffits — renders **dashed** at `W_FINE`, never solid.
- **SY-11.** Electrical symbols, if emitted at all, live in a **separate toggleable SVG layer**
  so the architectural plan stays readable. Switch legs use curved dashed arcs distinct from the
  hidden-line dash pattern.
- **SY-12.** Any symbol set that appears in the drawing must be reproducible from a **legend**
  emitted alongside the plan. If barndsl invents a glyph (zone shading, diagnostic marker), the
  legend is mandatory.
- **SY-13.** Symbols must not overlap. Run a collision pass over fixture bounding boxes, door
  arcs, and text; report overlaps as a diagnostic. Overlapping symbols are the fastest way for
  a plan to read as machine-generated.

---

## 6. Dimensioning

Ching's book treats dimensioning relatively lightly (the 6th edition improved the coverage of
scale and dimensioning, but the mechanics below are office/CAD standard practice rather than a
close paraphrase of the text).

### 6.1 Anatomy

- **Dimension line:** a light continuous line spanning the measured distance, parallel to the
  measured feature.
- **Extension (witness) lines:** light lines projecting from the feature out past the dimension
  line. Convention: a small **gap (≈1/16" on paper)** between the object and the start of the
  extension line, and a small **overshoot (≈1/8")** past the dimension line.
- **Terminators:** architectural practice overwhelmingly uses a **45° tick/slash** rather than
  an arrowhead (arrows are the mechanical-engineering convention). Ticks are drawn consistently
  in one rotational direction. Dots are used in tight strings.
- **Text placement:** dimension text sits **above and centered on** the dimension line in US
  architectural practice (as opposed to breaking the line, which is a mechanical convention),
  reading left-to-right or bottom-to-top — **never upside down**.
- **Format:** feet and inches with the foot mark and inch mark, hyphenated:
  `12'-6"`, `3'-0"`, `0'-8"` or `8"`. Zero inches is written explicitly (`10'-0"`, not `10'`)
  so a reader can tell a complete dimension from a truncated one. Metric: millimetres with no
  unit suffix on the drawing, declared once in the notes.

### 6.2 Strings

Dimensions are organized into concentric **strings** (chains) running outside the plan, ordered
from the building outward:

1. **Innermost string:** individual openings — window and door centerlines, and jamb-to-jamb
   segments.
2. **Second string:** wall-to-wall — the locations of interior partitions and exterior wall
   offsets.
3. **Third / outermost string:** the **overall** building dimension.

Every string must **add up**. The sum of the segments in an inner string equals the overall.
A string whose segments don't total the overall is the classic drafting error, and is trivially
checkable in code.

Leave a clean offset between the building and the first string, and equal spacing between
strings, so they read as a hierarchy rather than a thicket.

### 6.3 What you dimension *to*

This is the part with real construction consequence, because a framer builds to the dimension.

- **Exterior frame walls:** dimension to the **face of stud** (the outside face of the framing),
  not to the face of finish/siding. The framer snaps lines on the deck to the stud face.
- **Interior frame partitions:** dimension to the **centerline** of the partition (common
  residential practice) or consistently to one face — but pick one and never mix within a
  drawing set.
- **Masonry / concrete:** dimension to the actual face of masonry.
- **Columns / posts:** dimension to the **centerline**, and carry a column grid.
- **Doors and windows in frame walls:** dimension to the **centerline of the rough opening**,
  not to the jambs. (Exception: doors placed a fixed distance off a corner are sometimes
  dimensioned as a jamb offset — e.g. `4" TO JAMB` — because that is how they get framed.)
- Do **not** dimension to the face of drywall or finish on a framing plan; that number is wrong
  by half an inch everywhere and generates RFIs.

### 6.4 Discipline rules

- **Dimension once.** A distance given twice invites contradiction. Prefer one governing string.
- **Do not over-dimension.** Leave one segment in a chain undimensioned if it is the remainder,
  or mark it `EQ`/`VIF`. (In practice most residential sets do dimension everything and rely on
  the string adding up.)
- Use **`EQ`** for intentionally equal spacings rather than computing a repeating decimal.
- Keep dimension text **outside the plan body** wherever possible; interior dimensions are for
  things that cannot be reached from outside (an interior island, a closet depth).
- Dimension lines and extension lines are the **lightest** structural elements on the sheet
  (`W_FINE`), so they never compete with the building.

### Rule candidates — dimensioning

- **DM-1.** Emit at minimum **three dimension strings per exterior face**: openings (centerlines),
  partitions/offsets, and overall. A plan with only an overall dimension is not buildable.
- **DM-2.** Anchor to construction faces per §6.3: exterior = face of stud; interior partitions =
  centerline; posts = centerline; openings = centerline of rough opening. Encode the anchor as
  an explicit enum on the dimension object so it is auditable, and print the convention in the
  general notes (`ALL DIMENSIONS TO FACE OF STUD U.N.O.`).
- **DM-3.** **Validate that every string sums to the overall** within a 1/16" tolerance and emit
  a diagnostic when it does not. This is cheap and catches real geometry bugs.
- **DM-4.** String offsets from the building face, in paper millimetres: first string at 10 mm,
  each subsequent string +8 mm. Constant spacing; never let strings collide with the plan or
  each other.
- **DM-5.** Terminators are **45° ticks** of length ≈ 2.5 mm paper, all rotated the same way, at
  `W_FINE`. Extension lines: 1.5 mm gap from the object, 2 mm overshoot past the dimension line.
- **DM-6.** Dimension text sits **above** and centered on its dimension line, offset ≈ 1 mm,
  never breaking the line, and is **rotated with the line** but constrained so it is never
  upside down (rotate 180° when the line's angle falls in the lower half-plane).
- **DM-7.** Format as `F'-I"` with explicit zero inches and fractional inches to the nearest
  1/16" (`12'-6 1/2"`). Never emit decimal feet in a plan dimension.
- **DM-8.** If a dimension's text is wider than the segment it labels, place it **outside** the
  extension lines with a short leader, or stagger alternating dimensions to two rows. Do not let
  text overprint a tick.
- **DM-9.** Dimensions live in their own SVG group at `W_FINE` so they can be toggled off for a
  presentation-mode render.
- **DM-10.** Suppress the openings string at effective scale ≤ 1/16"=1'-0"; there is no room for
  it and it becomes noise.

---

## 7. Scale

### 7.1 Common scales

| Imperial | Ratio | Nearest metric | Typical use |
|---|---|---|---|
| 1" = 40'-0" / 1" = 20'-0" | 1:480 / 1:240 | 1:500 / 1:200 | Site plans (engineer's scale) |
| 1/16" = 1'-0" | 1:192 | 1:200 | Large buildings, whole-complex plans |
| 1/8" = 1'-0" | 1:96 | 1:100 | The workhorse plan scale |
| 1/4" = 1'-0" | 1:48 | 1:50 | Residential plans; the standard presentation plan |
| 3/8" = 1'-0" | 1:32 | — | Enlarged rooms |
| 1/2" = 1'-0" | 1:24 | 1:20 | Kitchens, baths, stairs |
| 3/4" – 1 1/2" = 1'-0" | 1:16 – 1:8 | 1:10 | Details, millwork |
| 3" = 1'-0" | 1:4 | 1:5 | Large details, sections through assemblies |
| Full / half size | 1:1 / 1:2 | 1:1 / 1:2 | Profiles, joinery |

For a barndominium — typically 40'–80' wide by 60'–120' long — **1/8" = 1'-0"** fits a whole
plan on a 24×36 sheet, and **1/4" = 1'-0"** is right for the residential wing alone or for an
11×17 review print.

### 7.2 Detail belongs to scale

The obligation to show detail rises with scale. Ching is explicit that a large-scale plan
*requires* more information, and that drawing it well requires knowing how the building is
actually built.

| Scale | What must be shown | What should be suppressed |
|---|---|---|
| 1/16" | Envelope, major masses, room block names | Fixtures, furniture, door swings, floor pattern, most dimensions |
| 1/8" | True wall thickness, all openings with swings, fixtures, room names + areas, full dimension strings | Material hatch, tile patterns, trim, electrical |
| 1/4" | Door/jamb thickness, sills and stools, cabinet detail, appliance detail, tags, notes | Individual framing members |
| 1/2"+ | Material assemblies, trim, nosings, clearance circles, framing where relevant | — |

Key threshold from the book: **at 1/4"=1'-0" and larger, walls are drawn at their true thickness
with material assembly indicated**; at smaller scales a simplified/single-line representation is
acceptable. barndsl models real wall thicknesses, so it should always draw true thickness — but
the *material indication inside* the wall is what turns on at 1/4".

### 7.3 The web problem

A numeric scale note (`SCALE: 1/4" = 1'-0"`) is only true at one output size. An SVG rendered in
a browser at arbitrary width is almost never at that size. A **graphic (bar) scale** is
self-scaling: it distorts with the drawing and stays correct. Any plan that will be viewed or
printed at unknown size needs one.

### Rule candidates — scale

- **SC-1.** Carry an explicit `target_scale` through the render pipeline (e.g. `1:48`) and derive
  every paper-space quantity — stroke widths, text heights, dash periods, tick lengths, dimension
  offsets — as `paper_value × scale_denominator` in model units. One conversion function, used
  everywhere.
- **SC-2.** Pick the scale automatically from the envelope: choose the largest standard scale
  from {1:24, 1:32, 1:48, 1:96, 1:192} at which the plan plus its dimension strings fits the
  target sheet with margins. Snap to a *standard* scale; never emit `1:73.4`.
- **SC-3.** Gate detail on scale with an explicit **level-of-detail (LOD) enum** driven by the
  chosen scale (§7.2 table). One switch, applied consistently, rather than per-element ad-hoc
  checks.
- **SC-4.** Always emit a **graphic bar scale** (see §8.3) in addition to the numeric note.
- **SC-5.** Emit the numeric scale note as `SCALE: 1/4" = 1'-0"` (and/or `1:50`) near the title,
  and mark it as valid only for the intended sheet size.
- **SC-6.** Turn material hatch inside cut walls on only at LOD ≥ 1/4"=1'-0"; below that, flat
  poché.

---

## 8. Lettering, annotation, and meta-graphics

### 8.1 Lettering

- **Uppercase, single-stroke sans-serif** is the architectural default — it reads at small size,
  survives reproduction, and is unambiguous.
- **Minimum letter height on the printed sheet: 1/8" (≈3.0–3.2 mm).** Several jurisdictions
  codify this (e.g. NYC DOB drawing standards require 1/8" minimum for notes, lettering, and
  dimensions). **Maximum for single-stroke hand lettering: ~3/16"**, beyond which the stroke
  needs widening.
- **Title-block and view titles: 1/4" (≈6 mm)**; section/detail reference letters also 1/4".
- **Spacing:** letter spacing ≈ one letter width; word spacing ≈ two letter widths; **line
  spacing (leading) ≥ 1.5× letter height**.
- Use **one typeface family per drawing set**. Consistency of stroke weight throughout.
- Text is always **horizontal** where possible; where it must rotate (a vertical dimension
  string, a label along a wall), rotate so it reads from the bottom or the right of the sheet —
  never upside down.
- **Notes and leaders:** a leader is a straight or gently curved line from the note to the thing,
  terminating in an arrowhead or dot. Leaders should not cross each other, should not run
  parallel to nearby geometry, and should approach at a distinct angle.

### 8.2 North arrow

- A simple arrow with `N` (or `NORTH`), not an ornate compass rose on a working drawing.
- Placed consistently: near the plan title, or a fixed corner of the sheet.
- Plans are conventionally oriented with **north up** or as near to up as possible.
- If the building's major axis is within **45° of true north**, an **assumed north** may be used
  so the plan sits square on the sheet, and it should be labeled as assumed.
- **All plans in a set share the same orientation.** Rotating one floor to fit the sheet is a
  serious error.

### 8.3 Graphic scale bar

- A bar divided into labeled increments (e.g. `0 — 2 — 4 — 8 — 16 FT`), typically with the first
  increment subdivided finer.
- Placed adjacent to the numeric scale note, under the drawing title.
- Alternating filled/open segments, or ticks with labels below.

### 8.4 Reference symbols

- **Section / cutting-plane marker:** heavy long-dash line across the plan with **direction
  arrows** at the ends and a bubble carrying `section number / sheet number`. The line need not
  cross the whole plan but must extend past the exterior boundary at each end.
- **Detail marker:** circle or rounded rectangle around the detail area with a leader to a
  bubble.
- **Interior elevation marker:** typically a square/diamond with up to four arrows, one per
  wall, each with an elevation number.
- **Door/window tags:** circle (doors) and hexagon or oval (windows), keyed to schedules.
- **Column grid:** centerlines through columns with a **bubble at each end** — letters one way,
  numbers the other. Grid lines are `W_HAIR` centerline type. This is the natural spine for a
  post-frame barndominium.
- **Room tag:** name in uppercase, with area and/or clear dimensions beneath in smaller text.
- **Revision cloud + triangle** with a revision number.

### 8.5 Titles

- View title placed **below the drawing**, left-aligned with the drawing's left edge or centered
  under it, in the largest text on the sheet after the title block.
- Underlined with a heavy rule (`W_PROFILE`), with the **scale on the line below** in smaller
  text.
- Format: `FIRST FLOOR PLAN` / `SCALE: 1/4" = 1'-0"`, often with a view number bubble to the left.

### Rule candidates — annotation

- **AN-1.** All plan text is **uppercase sans-serif**, one family, one color. Establish a text
  size ladder in paper millimetres and convert per SC-1:

  | Token | Paper mm | Use |
  |---|---|---|
  | `T_TITLE` | 6.0 | View title |
  | `T_ROOM` | 3.5 | Room names |
  | `T_ANNO` | 3.0 | Dimension text, notes, tags, sub-labels |
  | `T_SMALL` | 2.4 | Areas, secondary sub-labels — **floor; never smaller** |

- **AN-2.** Hard minimum rendered text height of **~8 device px** in the SVG viewer default view.
  If the ladder computes below that, either raise the scale or suppress the label tier — do not
  emit unreadable text.
- **AN-3.** Line spacing ≥ **1.5×** font size for multi-line labels; set `dominant-baseline`
  and `text-anchor` explicitly (never rely on defaults) so labels center reliably.
- **AN-4.** Room labels are centered on the room's **pole of inaccessibility** (the largest
  inscribed circle center), not the bounding-box or centroid center — L-shaped rooms put a
  centroid label into a wall.
- **AN-5.** Room label block: `NAME` on line 1 at `T_ROOM`; `AREA SF` and/or clear dimensions on
  line 2 at `T_SMALL`. Suppress line 2 if the room's inscribed circle cannot contain the text.
  Suppress the whole label and use a leader to a note if even the name does not fit.
- **AN-6.** Run a **text collision pass**: no label may overlap a wall, fixture, door arc,
  dimension string, or another label. On collision, try (a) shrink one tier, (b) drop line 2,
  (c) move to a leader outside the room. Report unresolved collisions as a diagnostic.
- **AN-7.** Rotate text only when necessary and clamp so it never renders upside down: if the
  computed rotation ∈ (90°, 270°), add 180°.
- **AN-8.** Every plan emits, unconditionally: **north arrow**, **graphic bar scale**, **numeric
  scale note**, **view title with a heavy underline**, and a **general-notes block** stating the
  dimensioning convention and units.
- **AN-9.** North arrow at a fixed sheet position; if the plan is rotated for fit, rotate the
  arrow to match and label `ASSUMED NORTH` when the rotation exceeds 45° from true.
- **AN-10.** Bar scale increments in round numbers appropriate to the scale (0/2/4/8/16 ft at
  1/4"; 0/4/8/16/32 ft at 1/8"), with the first increment subdivided.
- **AN-11.** For post-frame envelopes, emit a **column grid** with bubbled letters (long axis)
  and numbers (short axis) at `W_HAIR` centerline dash. Dimension the grid.
- **AN-12.** Emit door and window **tags** at LOD ≥ 1/4" and a matching **schedule** (as a
  separate table output, not crammed onto the plan).
- **AN-13.** Leaders: straight, one bend maximum, arrival angle ≥ 15° off any nearby geometry,
  terminating in a small filled dot or arrowhead. No crossing leaders.

---

## 9. Rendering: values, textures, shade and shadow

### 9.1 Tonal value

- A tonal hierarchy is what defeats the flatness of the sheet. Ching's plan-specific application
  is **poché**: cut solids dark, void light, maximum figure–ground contrast.
- In elevations and perspectives, value carries **depth**: foreground darker/heavier, background
  lighter — atmospheric perspective. (Note that the general "closer = lighter/darker" rule flips
  depending on whether you're modeling atmosphere or emphasis; the operative rule is *consistent
  gradation*, not a fixed polarity.)
- Use a **limited number of discrete values** — typically 4–6 steps from white to black. Too many
  and the drawing muddies; too few and it goes graphic/flat.

### 9.2 Texture and hatching

- Tone is built from **hatching, cross-hatching, stippling, or scribble**; density and spacing
  control the value.
- **Keep the technique consistent** in direction and character across the drawing. Random,
  chaotic mark-making reads as noise.
- **Texture scale must track drawing scale.** A brick pattern drawn at true coursing at
  1/8"=1'-0" becomes a gray smear; at that scale you indicate the material with a *suggestion*
  in a corner of the surface, not an exhaustive fill. Fine grain belongs at large scales only.
- Texture indicates a **material change without a change of form**, so it is drawn at the
  **lightest** weight tier.

### 9.3 Shade and shadow

- **Shade** = a surface turned away from the light and therefore unlit. **Shadow** = the region
  another object prevents light from reaching. They get different (usually shade lighter than
  shadow) values.
- The **conventional light source is 45°** — a Beaux-Arts inheritance — usually taken as coming
  from the **upper left**, inclined at 45° to both the horizontal and the vertical planes.
- The great convenience of 45°: **shadow length equals object projection/height (1:1)**. A 2'-0"
  roof overhang casts a 2'-0" shadow on the wall below. This makes shadow construction a pure
  offset operation, no trigonometry.
- Shadows in **plan** are used sparingly — mostly to show a roof overhang or a change of level on
  a site plan — but a drop shadow on the building's envelope is a legitimate and readable device.
- Shadows in **elevation** are the primary tool for revealing depth: reveals, recesses, overhangs,
  and window setbacks are otherwise invisible in an orthographic elevation.

### 9.4 Context

Entourage (people, cars, trees, ground) is drawn to the **same scale and same level of
abstraction** as the building. A photorealistic tree next to a line-drawn building reads as a
collage. People at correct scale (5'-6"–6'-0") are the single most effective scale cue available.

### Rule candidates — rendering

- **RN-1.** Restrict the plan's value palette to a small named set — e.g. `#ffffff` (void/floor),
  `#f2f2f2` (secondary floor zones: shop slab vs. living), `#c8c8c8` (light fill / diagnostic),
  `#808080` (poché at large scale), `#1a1a1a` (poché at small scale, and all strokes). Five
  values, not a gradient.
- **RN-2.** Distinguish the residential zone from the shop zone with a **very light background
  fill difference** (e.g. white vs. `#f6f6f6`), not with color or heavy hatch. The barndominium's
  defining feature deserves one quiet graphic cue.
- **RN-3.** Floor surface patterns (tile, board, slab control joints) are `W_HAIR`, drawn in a
  gray (e.g. `#bbb`) rather than black so they recede, and are **suppressed below LOD 1/4"**.
- **RN-4.** Any hatch is generated at **paper-space frequency** (e.g. 45° lines at 1.5 mm paper
  spacing) via `<pattern patternUnits="userSpaceOnUse">` with the spacing computed through SC-1 —
  never at a fixed model-space frequency, or it collapses at small scale.
- **RN-5.** If the plan uses a drop shadow on the building envelope, use a **45° offset toward
  the lower right**, offset distance ≈ 1.5 mm paper, at ~15% black, with **no blur** (a blurred
  shadow reads as a UI card, not a drawing). Offset must be constant in paper space.
- **RN-6.** For glTF: place the default directional light at **45° altitude from the upper left**
  (azimuth such that shadows fall down-right in the default camera view) so the 3D view's shading
  agrees with the drawing convention. Add a low-intensity ambient/hemisphere fill so unlit faces
  are shaded but not black.
- **RN-7.** In glTF, use **flat, low-saturation materials** distinguished by value, not by
  saturated color: envelope walls one value, interior partitions lighter, slab a neutral gray,
  roof a darker value. Avoid procedural textures; they fight the diagrammatic reading and bloat
  the file.
- **RN-8.** If entourage is ever added to a 3D or perspective view, include a **6'-0" human
  figure** as the scale reference; keep abstraction level uniform.

---

## 10. Presentation and sheet layout

- Arrange views in a **logical, conventional sequence**: site → plans (bottom floor first) →
  sections → elevations → details. Within a sheet, plans above, sections/elevations below.
- **Align views.** Sections and elevations should align with the plan they derive from, so the
  reader can project between them without measuring. This is the whole point of multiview: the
  set carries the depth that no single view has.
- **Consistent orientation.** Every plan on every sheet points the same way; north is the same
  everywhere.
- **Hierarchy.** The primary drawing occupies the most area and often the largest scale;
  supporting views are smaller. Do not give a detail the same visual weight as the plan.
- **White space is structural.** Generous margins, consistent gutters between drawings, related
  drawings grouped. Cramming is the most common presentation failure.
- **Standard sheets:** 11×17 (B), 18×24 (C), 24×36 (D), 30×42/36×48 (E). Border/title block with
  at least a **1/2" margin** from the sheet edge; binding edge often wider (1.5").
- **Title block** conventionally at the **lower right** or along the full right edge, carrying
  project, sheet name, sheet number, scale, date, north (sometimes), and revisions.
- Sheet numbering by discipline (`A-1.1` architectural, `S-` structural, etc. under the National
  CAD Standard scheme).
- All text and dimensions sized for **reproduction**, not for the screen you drew them on.

### Rule candidates — presentation

- **PR-1.** The SVG root has an explicit `viewBox` in model units and `width`/`height` in real
  paper units (`mm` or `in`) so the file prints at true scale by default while still scaling in a
  browser. Add `preserveAspectRatio="xMidYMid meet"`.
- **PR-2.** Compute a sheet margin of ≥ **12.7 mm (0.5")** paper on all sides, and fit the plan
  *plus its dimension strings, titles, and meta-graphics* inside it. Sizing to the building
  bounding box alone will clip the dimensions.
- **PR-3.** Fixed layout zones: drawing body (center), view title + bar scale (below-left of the
  body), north arrow (upper right of the body), general notes (upper left or right margin),
  title block (lower right). Deterministic positions beat clever packing.
- **PR-4.** When barndsl emits more than one view (plan + elevations, or multiple floors),
  **align them on a shared axis** and keep the same scale, so the set reads as a set.
- **PR-5.** Multi-floor plans keep identical orientation and identical sheet position, so a
  reader flipping between them sees the building stay still.
- **PR-6.** Emit a machine-readable `<title>` and `<desc>` on the SVG root (project, view, scale,
  date) — the digital equivalent of a title block, and useful for the review UI.
- **PR-7.** Group the SVG with semantic `id`s (`#walls`, `#openings`, `#fixtures`, `#dimensions`,
  `#annotation`, `#grid`, `#diagnostics`) so downstream tooling and the playground can toggle
  layers without re-rendering.

---

## 11. Gap analysis against the current barndsl renderer

This section binds the conventions above to the code as it exists in
`D:\CodeProjects\Architecture-DSL\src\barndsl\render.py` (plus `gltf.py`, `fixtures.py`,
`elements.py`). It is the shortest path from "research" to "tickets."

### 11.1 The renderer's native scale is already 1/8" = 1'-0"

`RenderConfig.scale = 12.0` px/ft, fixed. At the CSS reference of 96 px/inch that is
`12 / 96 = 0.125 inch per foot` — i.e. **1/8" = 1'-0", exactly 1:96**. This is an important and
previously unstated fact: every paper-space number in this document can be converted to the
renderer's pixels with one constant.

> **px = paper_mm × 96 / 25.4 = paper_mm × 3.7795**
> (and `paper_inches × 96`)

So `1/8" minimum text height = 12 px`, and the ISO pen series lands at
0.13→0.49 px, 0.18→0.68, 0.25→0.94, 0.35→1.32, 0.50→1.89, 0.70→2.65 px.

The print path (`ARCH_SCALES`, `fit_scale`, `sheet_scale`, and the CSS width set by
`packet.py`) already selects a true architectural scale from {1/4", 3/16", 1/8", 3/32", 1/16"}
per sheet — good, and unusually rigorous. But the *drawing content* does not change with that
selection, so §7.2's level-of-detail obligation is unmet in both directions.

### 11.2 The line-weight hierarchy is inverted

This is the single highest-value fix. Measured from the current code:

| Element | Current stroke | ≈ pen | Should be |
|---|---|---|---|
| **Cut wall outline** (`POCHE_STROKE`) | **0.5 px** | 0.13 mm | **heaviest** — 1.89 px / 0.50 mm |
| Envelope profile | (same 0.5 px) | 0.13 mm | 2.65 px / 0.70 mm |
| Beam centerline (`BEAM_COLOR`) | **2.2 px** | 0.58 mm | ~0.7 px, and dashed/centerline type |
| Door leaf | 1.2 px | 0.32 mm | ≈1.3 px (`W_OBJECT`) — OK |
| Window symbol | 1.2 px | 0.32 mm | ≈0.9 px (`W_SEEN`), lighter than wall |
| Dimension lines | 1.0 px | 0.26 mm | 0.68 px (`W_FINE`) |
| Door swing arc | 0.8 px | 0.21 mm | 0.68 px (`W_FINE`) — OK |
| Fixtures | 0.6–0.9 px | 0.16–0.24 | 0.94 px (`W_SEEN`) |

The cut wall — conventionally the heaviest line on the sheet — is currently the **thinnest
stroke in the file**, with all of its visual weight carried by the poché fill. A structural
beam centerline, which should be a light dash-dot reference line, is the **heaviest line in the
drawing**. Windows, door leaves, and even dimension lines all outweigh the wall cut. Per §4 this
is precisely backwards, and it is why the plan reads flat despite having correct poché.

**Fix:** introduce the named pen table from **LW-1** in `render.py` (there is currently no
line-weight constant table at all — widths are literal floats at every call site), map every
call site onto a token, and invert the wall/beam relationship.

### 11.3 What is already right (do not regress)

The renderer is further along than a gap list implies. Already conventional:

- Double-line wall construction with **solid poché cut at every opening** — the wall genuinely
  breaks at jambs (**FP-6** satisfied), via shared `wallbodies.wall_bands()` geometry that the
  DXF export also consumes, so drawn and exported walls agree.
- **Door swing arcs correctly centered on the hinge**, with hand (`hinge` near/far) and swing
  side (`swing_into`) honored, and distinct symbols for pocket/sliding, bifold, cased opening,
  and overhead. Overhead doors already get a dashed track and no arc (**FP-10**, **SY-4**
  largely satisfied).
- **Chained exterior dimension strings broken at both room boundaries and opening jambs**, plus
  overall strings, with feet-inches formatting to the eighth (`18′-6″`, `11′-7½″`).
- A genuine **face-of-stud vs. nominal-centerline dimension mode** (`RenderConfig.dim_mode`)
  reading real wall faces off the same band geometry. This is **DM-2** already implemented and
  is better than most consumer plan tools manage.
- **North arrow** with a real true-north azimuth from `plan.orientation`, falling back to
  "plan north" when unsited.
- **Graphic bar scale** plus scale statement, tied to the true architectural scale selection.
- **Door/window mark bubbles** (`D1`, `W1`) keyed to the schedules and the DXF.
- Fixture glyphs that are thin drafting outlines rather than icons, with **mitred L/U counter
  runs** and counters drawn under inset appliances.

### 11.4 Prioritized gaps

Ordered by ratio of reading-quality gain to implementation cost.

**P0 — cheap, large effect**

1. **Pen table + inverted hierarchy** (§11.2). Touches every call site but is mechanical.
2. **Text below the printed minimum.** §8.1's floor is 1/8" = **12 px** at this scale. Current
   sizes: room area/notes 10, chain-dim labels/room W×L/stair labels 9, `POSTS o.c.`/scale-bar
   numerals 8, mark bubbles ~8.6, `GFCI`/alarm labels **6** (= 1/16", half the legal minimum in
   jurisdictions that codify it). Only the 12 px room name and larger clear the bar. Raise the
   ladder per **AN-1**, or accept the smaller sizes only for on-screen review and enforce the
   minimum in the print path.
3. **Turn the scale bar on by default.** `RenderConfig.scale_bar=False`; only `packet.py`
   enables it. Per **SC-4** and **AN-8** every emitted plan needs it — especially since the SVG
   is viewed at arbitrary browser widths where the numeric note is simply false.
4. **Stair break line, riser count, and a real arrow.** Currently a fixed **5 tread lines
   regardless of the actual riser count**, direction conveyed only by a `↑`/`↓` text glyph, and
   **no break line at all**. `constants.py` already carries `MAX_RISER_HEIGHT = 7.75/12` and
   `MIN_TREAD_DEPTH = 10/12` but the renderer never reads them. Per **SY-8**: derive tread lines
   from real geometry, draw a centerline arrow pointing up, label `15R UP`, and put a zigzag
   break at the riser nearest 4'-0" of rise.

**P1 — moderate cost, clearly conventional**

5. **Witness/extension lines and slash terminators.** Dimension lines currently float at fixed
   pixel offsets with plain cross ticks and **no extension lines** (**DM-5** unmet). Add
   extension lines with the 1.5 mm gap / 2 mm overshoot and rotate the ticks to 45°.
6. **Interior dimension strings.** Nothing dimensions room-to-room partitions; room size appears
   only as text under the room name. **DM-1** wants at least partition locations dimensioned.
7. **String-sum validation** (**DM-3**) — near-free given the chain machinery already exists,
   and it will catch real geometry bugs.
8. **Level-of-detail gating** (**SC-3**). No LOD switch exists; the same content is drawn at
   1/16" and 1/4". Suppress floor pattern, furniture, electrical, and the openings string at
   small scale; enable jamb thickness and material hatch at large.
9. **Windows do not vary.** All four `WINDOW_KINDS` render the identical 3-line + 2-jamb glyph,
   and `sill_height` (default 3.0 ft), `head_height` (6.67 ft), `kind`, and `tempered` never
   reach the drawing. Per **SY-6/SY-7**: keep the glyph, but drop it to `W_SEEN` (lighter than
   the wall), add the sill/stool projection, and express operation and tempering through the
   **tag and schedule**, not an invented glyph. Note that with sill 3.0 and head 6.67, a default
   window genuinely *is* cut by the 4'-0" plane — the model already has what **FP-3** needs.
10. **Text collision avoidance and halos** (**AN-6**). No masking, no collision pass; labels can
    overprint poché and dimensions, and note leaders are a hardcoded 45° NE with no adjustment.

**P2 — larger, but required for a "drawing" rather than a "diagram"**

11. **Title block** (**PR-3**). None exists — no sheet border, number, date, scale field, or
    revision block. The `PROJECT SUMMARY` panel is a metrics box, and the dimension-convention
    note (`"Dimensions to face of stud"`) currently lives in **HTML in `packet.py`**, not on the
    drawing. That note in particular belongs in general notes on the sheet (**DM-2**).
12. **Reference symbols** (**AN-12**, §8.4): no section cut marks, elevation reference bubbles,
    detail callouts, wall-type tags, room number tags, or ceiling-height tags — the last being
    specifically needed for the residential/shop ceiling split (**FP-11**).
13. **SVG layer discipline** (**PR-7**). Only three `data-layer` groups exist; fixtures, dims,
    doors, windows, stairs, structure, and text are emitted flat into one part list, so nothing
    downstream can toggle or re-weight by discipline.
14. **A monochrome plot mode.** The current output is a color presentation drawing (25
    `ROOM_COLORS` tints plus six accent colors). That is a legitimate deliverable, but there is
    no CD-style black-and-white output. Per **LW-7**/**RN-1**, the base drawing should be
    monochrome with color as a toggleable overlay — which also makes the line-weight hierarchy
    do the work color is currently doing.
15. **Material hatch in the cut** (**SC-6**, §3.3). Poché is a flat opaque `#3a3a3a` with no
    material indication and **no distinction between exterior, interior, and plumbing walls**
    (`WallBand.thickness_class` drives width only). `WALL_ATTRIBUTES` already carries `rated`
    with no graphic expression.
16. **Nothing dashed above the cut** (**FP-12**, **SY-10**). No roof-overhang outline, no upper
    cabinets, no mezzanine edge, no soffits. `plan.overhang` exists in the model and never
    reaches the plan.

### 11.5 Fixture catalog cross-check

`fixtures.py` footprints against §5.4–5.5 (all feet; `width` runs along the backing wall):

| kind | barndsl | §5.4/5.5 reference | Verdict |
|---|---|---|---|
| toilet | 2.5 × 2.33 (30″×28″) | fixture is ~20″ W; **30″ is the required clear bay** | Footprint conflates fixture with clearance — draw a ~20″ bowl inside a 30″ keep-out |
| lavatory | 2.0 × 1.83 (24″×22″) | 24″×21″ vanity | Good |
| tub | 5.0 × 2.5 (60″×30″) | 60″×30″ | Exact |
| shower | 2.67 × 2.67 (32″×32″) | 32″ is **minimum**; 36″×36″ is standard | Consider 36″ default |
| sink | 2.5 × 2.0 (30″×24″) | 33″×22″ double / 24″×21″ single | Reasonable middle |
| range | 2.5 × 2.0 (30″×24″) | 30″ W × 25″ D | Good |
| refrigerator | 3.0 × 2.5 (36″×30″) | 33–36″ × 30–36″ | Good |
| washer / dryer | 2.25 × 2.25 (27″×27″) | 27–28″ W × **30–32″ D** | Depth is ~4″ shallow |
| water_heater | 2.0 × 2.0 | 20–24″ dia. | Good |
| bed_queen | 5.0 × 6.67 (60″×80″) | 60″×80″ | Exact |
| bed_twin | 3.25 × 6.25 (39″×75″) | 38″×75″ | Good |
| **dishwasher** | *absent* | 24″×24″ | **Missing** — a kitchen without one reads as incomplete |
| **wall oven / OTR microwave** | *absent* | 30″×24″ / 30″×16″ (dashed) | Missing |

Also worth revisiting for the barndominium program specifically:
`OVERHEAD_DOOR_WIDTH = 9.0` / `OVERHEAD_DOOR_HEIGHT = 7.0` is a **residential single-car garage
door**. Shop bays in this building type commonly run 10×10, 12×12, or 14×14, and an RV bay 14×16.
A 9×7 default will under-size the defining feature of the plan and, graphically, will make the
shop end read like a garage.

### 11.6 glTF notes

`gltf.py` is well-grounded: 1 glTF unit = 1 foot, plan `(x, y, z)` → `(x, z, −y)` (a proper
rotation), layered parent nodes, real thicknesses (exterior 6.5″, interior 4.5″, slab 4″, door
head 6'-8", overhead 7'-0"), gable/shed/monitor roofs from `roof_pitch` (default 4:12), and room
floors tinted from `render.ROOM_COLORS` so 2D and 3D share one palette. That palette sharing is
exactly right (**RN-7**'s intent).

Two conventions from §9 to apply: set the default directional light at **45° altitude from the
upper left** so 3D shading agrees with the drawing convention (**RN-6**), and set the default
camera per **DS-4** (eye height 5'-6", distance ≥ 1.5× the long dimension, world-up preserved so
verticals never converge).

---

## 12. Checklist: professional vs. amateur

A generated plan reads as **professional** when:

1. **There are at least three visibly distinct line weights**, and the heaviest is the cut.
2. **Cut walls are filled (poché'd)**, and the fill is one continuous mass with no internal
   seams at wall intersections.
3. **The envelope profile is a single unbroken heavy loop.**
4. **Openings genuinely break the wall fill**, with jambs, and every door shows its swing or
   operation.
5. **Window symbols are lighter than wall cuts** and show a glazing line.
6. **Stairs have treads, a direction arrow, an uppercase `UP`/`DN` + riser count, and a break
   line.**
7. **Everything above 4'-0" is dashed** — upper cabinets, mezzanine, overhangs — and labeled
   `ABOVE`.
8. **Dimension strings are layered** (openings / partitions / overall), sit outside the plan at
   even offsets, use 45° ticks, add up, and state their anchor convention.
9. **Text is uppercase, one family, one size ladder, never below ~3 mm paper**, and never
   overlaps geometry.
10. **Room labels are centered in usable space** with names and areas.
11. **A north arrow, a graphic bar scale, a numeric scale, and a titled underline are present.**
12. **Fixtures are at real footprints** and do not collide with each other, with door swings, or
    with clearances.
13. **The level of detail matches the scale** — no tile patterns at 1/16", no bare rectangles at
    1/2".
14. **Nothing is decorative.** No gradients, no drop shadows with blur, no color used where
    weight would do, no rounded "app-style" corners on walls.

It reads as **amateur** when: every line is the same width; walls are hollow outlines; walls
show cross-hairs where they intersect; doors and windows are gaps; stairs are a rectangle with
some lines; nothing is dashed; there is one overall dimension or none; text overlaps walls;
labels are lowercase mixed-case sentence text; there is no north arrow and no scale; fixtures
are cartoon icons at invented sizes; the same drawing uses 1/16"-scale abstraction and
1/2"-scale detail at once; and the whole thing is rendered in pastel fills with a soft drop
shadow.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| **AFF** | Above finish floor. The datum for mounting heights. |
| **Axonometric** | Paraline projection with parallel projectors oblique to the picture plane; measurable in 3D. Includes isometric/dimetric/trimetric. |
| **Break line** | A line (zigzag or double-jog) indicating a drawing has been interrupted and the object continues. |
| **Cast shadow** | The region a surface is denied light because another object intervenes. |
| **Centerline** | Dash-dot line marking an axis of symmetry or a grid; also a dimensioning datum. |
| **Cone of vision** | The ~60° (safely 45–50°) cone from the station point inside which a perspective is undistorted. |
| **Cut / cut plane** | The imaginary plane slicing the building; in plan, horizontal at ≈4'-0" AFF. |
| **Datum** | A reference from which dimensions are measured (face of stud, centerline, finish floor). |
| **Dimension line** | The light line spanning a measured distance, terminated by ticks or arrows. |
| **Elevation** | An orthographic view of a vertical face, projected onto a vertical picture plane. |
| **Entourage** | The people, cars, vegetation, and ground that give a drawing scale and context. |
| **Extension (witness) line** | The light line projecting from a feature out to the dimension line. |
| **Face of stud (FOS)** | The framing face, the usual datum for exterior dimensions in wood-frame work. |
| **Figure–ground** | The perceptual separation of solid mass from void; in plan, produced by poché. |
| **Hidden line** | Dashed line for an element not directly visible in the view — in plan, usually above the cut. |
| **Isometric** | Paraline projection with all three axes equally inclined; horizontals at 30°, all axes true length. |
| **Leader** | A line from a note to the thing it describes, ending in an arrow or dot. |
| **Line weight** | The width/darkness of a line, encoding what kind of thing it represents. |
| **Match line** | A heavy labeled line where a large plan is split across sheets. |
| **Mezzanine** | An intermediate floor, partial in area — in a barndominium, typically a loft over the shop; drawn dashed in the plan below. |
| **Multiview / orthographic** | Projection with parallel projectors perpendicular to the picture plane; true size in the plane shown. |
| **Oblique (plan / elevation)** | Paraline projection in which one true plan or true elevation is kept and the third dimension is projected off it (45°/45° "military", or 30°/60°). |
| **Paraline** | Ching's umbrella term for all parallel-projection pictorial drawings (axonometric + oblique). |
| **Perspective** | Projection with converging projectors through a station point; pictorial but not measurable. |
| **Picture plane** | The surface onto which the projection is made. |
| **Poché** | Darkening/filling of elements cut by the drawing's cut plane, to establish figure–ground. |
| **Post-frame** | Construction using large posts at wide spacing (typically 8'-0" o.c.) rather than continuous stud walls; the classic barndominium structure. |
| **Profile line** | The heaviest line in a drawing: the continuous outline of the cut mass. |
| **Rough opening (RO)** | The framed opening a door or window is installed into; the usual dimensioning target. |
| **Sciography** | The construction of shades and shadows in a drawing. |
| **Section** | A view produced by a vertical cut through the building. |
| **Shade** | A surface turned away from the light; distinct from cast shadow. |
| **Station point** | The observer's eye position in a perspective construction. |
| **String (dimension string)** | A chain of contiguous dimensions along one line, which must sum to the overall. |
| **Tick / slash terminator** | The 45° stroke terminating an architectural dimension line, used in place of an arrowhead. |
| **U.N.O.** | "Unless noted otherwise" — the qualifier on a general note. |
| **VIF** | "Verify in field." |

---

## 14. Sources

- Francis D. K. Ching, *Architectural Graphics*, 6th ed. (Wiley, 2015) — primary source; 4th/5th
  and 7th editions consulted for edition differences.
  [Full text scan](https://archive.org/details/FrancisD.K.ChingArchitecturalGraphics6thEd2015) ·
  [Publisher page](https://www.wiley.com/en-mx/Architectural+Graphics,+6th+Edition-p-9781119073383)
- [First In Architecture — Architectural line weights and line types](https://www.firstinarchitecture.co.uk/architectural-line-weights-and-line-types/)
- [Life of an Architect — Architectural Graphics 101: Line Weight](https://www.lifeofanarchitect.com/architectural-graphics-101-line-weight/)
- [Studio Matrx — Lineweights: the grammar of hierarchy](https://www.studiomatrx.org/students/drawing-fundamentals/architectural-lineweights-explained)
- [Studio Matrx — Construction drawing symbols](https://www.studiomatrx.org/guides/construction-drawing-symbols-homeowners-should-know)
- [EVstudio — Dimensioning 101](https://evstudio.com/dimensioning-101/)
- [Chapter 17: Floor-Plan Dimensions and Notes (PDF)](https://designdraftingclasses.files.wordpress.com/2011/08/chapter-17.pdf)
- [NYC DOB — Drawing standards for plan/work applications (PDF)](https://www.nyc.gov/assets/buildings/pdf/drawing_standards_08132010.pdf)
- [CAD Setter Out — Technical drawing standards: lettering heights](https://cadsetterout.com/drawing-standards/lettering-heights/)
- [NKBA — Universal drawing standards (PDF)](https://elearning.nkba.org/wp-content/uploads/2023/10/Chapter-3-Universal-Drawing-Standards.pdf)
- [Blueprint Primer — Door and window symbols on floor plans](https://blueprintprimer.com/posts/door-and-window-symbols-on-floor-plans)
- [Engineer Fix — How to properly show stairs on a floor plan](https://engineerfix.com/how-to-properly-show-stairs-on-a-floor-plan/)
- [Wikipedia — Sciography](https://en.wikipedia.org/wiki/Sciography)
- [First In Architecture — Understanding scales and scale drawings](https://www.firstinarchitecture.co.uk/understanding-scales-and-scale-drawings/)
- [Learn Architecture Online — Architectural drawing scales explained](https://learnarchitecture.online/blogs/architecture-design/architectural-drawing-scales)
- [Architect Wisdom — What poché means and how it differs from a hatch](https://architectwisdom.com/poche/)
- [Cedreo — Electrical symbols on floor plans](https://cedreo.com/blog/electrical-symbols/)
- [House Plans Helper — Bathroom dimensions](https://www.houseplanshelper.com/bathroom-dimensions.html)
- [UBC — Axo demystified: visual glossary](https://blogs.ubc.ca/axonometric/visualglossary/)
