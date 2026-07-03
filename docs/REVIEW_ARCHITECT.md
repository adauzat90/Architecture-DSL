# Architect's review — the design gaps (non-Revit)

A **senior residential architect's** pass over barndsl (2026-07-02), reviewing
the application *as a design tool*, with the Revit integration deliberately set
aside (untested here — no Revit on this machine). The lens is not "is the
validator stricter than it needs to be" — it already covers IRC egress, light,
reachability, wet-room clearances, garage separation, stair geometry and
clear-dimension shortfalls more thoroughly than most early-design tools. The lens
is: **what does a practicing residential architect reach for that isn't here?**

The through-line of every finding below: barndsl is a superb *plan-compliance*
engine and a thin *design* engine. The parts of residential design that happen
**before the plan** (the site) and **above the plan cut** (the section), plus a
few mechanically-checkable code domains, are where the gaps are.

## Headline finding

The model lives at a single plan altitude, on no site. There is no section, no
elevation (outside Revit), no site/lot/setback, and no passive-comfort story
(sun, shading, thermal envelope) — even though `orientation` is already in the
grammar. These aren't "build a bigger validator" asks; several are deterministic,
IRC-cited checks in the exact house style the engine already excels at.

## Impact / effort summary

| # | Feature | Effort | Impact | In house style? |
|---|---------|--------|--------|-----------------|
| 1 | Site model: `lot`, `setback` + solar-aware glazing | M | High | Yes — deterministic checks from geometry + azimuth |
| 2 | Schematic **section + elevation** renderer (non-Revit) | M | High | Renderer-only; IR data already present |
| 3 | **Overhang/eave depth** + **porch roofs** in the model | M | High | New geometry; feeds shading + elevations |
| 4 | Electrical / life-safety check batch | S | High | Yes — pure deterministic `info`/`warning` |
| 5 | Thermal envelope: `climate` zone, WWR, thermal-bridge note | S | Med-High | Yes — takeoff + one ratio check |
| 6 | Furniture-fit for habitable rooms (bed/seating/dining) | M | Med | Yes — same clear-floor geometry as baths |
| 7 | Program staples: linen/coat closet, mechanical space | S | Med | Yes — extends `program`/`require` |
| 8 | `barndsl cost` (roadmap's open item) | S | High | Yes — `metrics()` already has the quantities |

(S = a few dozen lines, M = a focused day or two.)

---

## 1. The site doesn't exist — and it's where residential design starts — SHIPPED

**Update (2026-07-02).** All of [`design/SITE_SOLAR.md`](design/SITE_SOLAR.md)
shipped (Phases 1–3): `orientation` is now real (`SOLAR_WEST_GAIN` /
`SOLAR_NORTH_ONLY` / `SOLAR_SOUTH_UNUSED` glazing nudges, a true-north compass, a
glazing-by-sector metrics split); the plan has a **site** — `lot` + `setback` →
the `SETBACK` warning and the parcel drawn around the footprint; and an
**approach** — `street <wall>` → `APPROACH_ENTRY` / `APPROACH_GARAGE`, with a
STREET edge on the render. Remaining ideas (`latitude`/`hemisphere`, a solar
score term, overhang-credited shading) are parked in the design doc's §7.

`orientation` is in the grammar but **architecturally inert**: it only sets Revit
project north and round-trips through `emit`. The docstring at
`src/barndsl/elements.py:569` claims it's "used for solar/setback reasoning," but
nothing consumes it. For a barndominium — rural, large lot, few neighbours, owner
cares about the view and the sun — this is the biggest conceptual hole.

A site model unlocks, all deterministically:

* **Setbacks / buildable envelope** — `lot 200 x 300` + `setback front 40 side 15
  rear 25` → a `SETBACK_VIOLATION` check the moment the footprint crosses the
  line. Acreage rarely fails this; infill and deed-restricted lots do.
* **Solar-aware glazing** — with a real orientation, flag large **west** glazing
  (afternoon heat gain), reward **south** glazing, note **north** rooms that will
  feel cold and dim. The single most valuable comfort nudge available, and it is
  fully computable from geometry + azimuth. (Pairs with #3: a south overhang is
  the fix the nudge should suggest.)
* **Approach / entry side** — which face the car arrives on drives where the
  garage/overhead doors, the mudroom and the "front" door want to be.

## 2. Everything is a plan — there is no section — SECTION/ELEVATION SHIPPED

**Update (2026-07-03).** The schematic vertical views shipped (`barndsl/views.py`,
`barndsl elevation --side <n|s|e|w>` and `barndsl section`): exterior elevations
with the roof profile and doors/windows at their true sill/head heights, and a
transverse section showing each level's floor/ceiling, vaulted double-heights, and
the roof over them — drawn from the model's heights, roof form and pitch (gable
exact, shed reasonable, monitor approximated; L/T/U to its bounding box). Covered
by `tests/test_views.py`. **Overhangs, porch roofs, and eave depth (below /
item #3) are still what a *faithful* elevation needs — the schematic omits them.**

The whole model lives at one Z. There is `ceiling`, `floor` assembly depth and
roof pitch, but no **eave/plate height**, no section, and no elevation outside the
Revit *Document* pass. A builder or lender wants at least a **front elevation and
one section**; the non-Revit user gets neither. The roof/ceiling/loft/vault data
to draw a *schematic* section already exists in the IR — this is a renderer
feature, not a new model. It also makes `vaulted`, per-room `ceiling`, the loft
guard and the roof form finally *visible* to the author, who today can only read
them as text.

## 3. Overhang/eave depth and porch roofs — OVERHANGS + PORCH SHADING SHIPPED

**Update (2026-07-03).** The `overhang <ft>` directive shipped: a real eave/rake
depth that projects the roof past the walls in the elevations/section, grows the
roof-area takeoff, and — the item #1↔#3 seam the design doc promised — *shades*
south glazing (`SOLAR_SOUTH_NO_OVERHANG` flags substantial south glass with no
eave), while a **covered porch** now credits the glass behind it (suppresses
`SOLAR_WEST_GAIN`, and counts as south shade). Covered porches also carry a
roof-area figure (`covered_porch_roof_sqft`). Covered by `tests/test_overhang.py`.
**Still schematic:** drawing porch roofs *in the elevations* (only their shading
and materials are modelled so far).

* **Overhang / eave depth** is absent entirely. On a barndominium it is both the
  signature look *and* the primary passive-shading device; a 24" south overhang is
  a real move you can't currently express, advise on, or take off for materials.
* **The porch is a flat rectangle with no roof.** A wraparound or deep shed-roof
  porch is the defining barndo element; today it is only a slab outline — no
  covered-entry logic, no shading contribution, nothing to draw in elevation.

Both are prerequisites for the shading half of #1 and for a faithful elevation in
#2, which is why they're grouped as one geometry addition.

## 4. Mechanically-checkable code domains not yet touched — PARTLY SHIPPED

**Update (2026-07-02).** The plumbing-stack check and the electrical/life-safety
reminder shipped:

* **`PLUMBING_STACK`** (`info`) — a geometric, defect-only check: an upper-floor
  wet room (bath/kitchen/laundry/utility) that sits over no wet room on the level
  below, so its waste stack can't drop straight down. The cross-floor analogue of
  `WET_GROUP`; only a multi-storey plan with an upper wet room can trip it.
* **`ELECTRICAL_PLAN`** (`info`) — an **opt-in** reminder via a new `electrical`
  directive (modelled on `accessible`), gathering the code items the geometry
  genuinely can't place — receptacle spacing (E3901.2) with GFCI/AFCI (E3902),
  switched lighting (R303.7 / E3903), stair lighting, and exterior-door landings
  (R311.3) — into one checklist for the construction documents. The stair and
  landing clauses appear only when the plan has a stair / an exterior people-door.

The reminder is opt-in rather than always-on for a deliberate reason: without an
electrical model the DSL can't *verify* these (only remind), and an always-on
reminder would nag every plan and permanently break the curated 0/0/0 gallery.
Opt-in keeps clean plans clean and hands the checklist to the user who wants it.

The original table, for reference — the two shipped rows plus the three still
open (receptacle/landing/stair-lighting are the checklist items now carried by
`ELECTRICAL_PLAN`; a per-level egress roll-up is left out as redundant with the
existing per-bedroom `BEDROOM_EGRESS`):

| Check | Basis | Why it matters |
|-------|-------|----------------|
| **Receptacle spacing** (the "6-ft rule") | IRC E3901.2 | Every wall point within 6 ft of an outlet; every wall span is already computed — directly checkable, and no early-design tool does it |
| **Exterior door landing** | IRC R311.3 | A level landing outside each exterior door; today only no-step *accessible* entries are flagged — landings are required for every home |
| **Lighting/switch at stairs & entries** | IRC R303.7 / E3903 | A switched light at each stair and habitable-room entrance |
| **Plumbing stack alignment across floors** | good practice | `WET_GROUP` clusters wet rooms on *one* level; it never checks a level-1 bath lands over a level-0 wet wall so the stack drops straight. The cross-floor `LOAD_PATH` check already walks stacked levels for structure — this is the plumbing analogue |
| **Emergency escape per sleeping level** | IRC R310 | Per-bedroom egress is checked; a "each level carrying a bedroom needs egress" roll-up is the natural companion |

Smoke/CO alarms (`ALARM_CO`) are already handled well as a reminder — these slot
into the same channel.

## 5. Thermal / energy envelope — a total gap, acute for a metal frame — SHIPPED

**Update (2026-07-03).** The `climate <zone>` directive (IECC 1–8) shipped
(`barndsl/energy.py`): `ENERGY_ENVELOPE` reports the zone's prescriptive R-value
targets (ceiling/wall/floor/slab + window U) with the steel-frame
continuous-insulation note the review called for, and `WINDOW_HEAVY` adds the
window-to-wall-ratio *ceiling* (~28%) to complement the daylight *floor*. Both
gated on a declared `climate`; `metrics()` gained `climate_zone`. Covered by
`tests/test_energy.py`. (R-values are guidance, not the code of record — same
posture as the frame/foundation disclaimers.)

No climate zone, no insulation, no window-to-wall ratio ceiling, no
thermal-bridging note. There is a daylight **floor** (`NAT_LIGHT`, 8% min) but no
solar-gain **ceiling** — nothing stops an all-glass south wall. For a *steel*
building this is not academic: steel framing is a severe thermal bridge, and a
barndo built without continuous exterior insulation is a comfort/energy failure. A
minimal deterministic add:

* `climate 5` (IECC zone) → R-value guidance in the takeoff and a window-to-wall
  ratio check.
* A standing **thermal-bridge note** for metal-frame walls recommending
  continuous insulation — same posture as the "frame is a layout aid, not an
  engineered design" disclaimer.

## 6. Furniture-fit only exists for wet rooms

`BATH_CLEARANCE` and `KITCHEN_FIT` are excellent. But a bedroom that passes
`BEDROOM_AREA` can still fail to hold a queen bed with two nightstands and a 36"
walk path; a great room can be un-arrangeable. Extending the fixture-clearance
idea to a **bed block + circulation path** in bedrooms and a **seating/dining
clearance** in living/dining catches the "meets area, doesn't furnish" plan — the
same clear-floor geometry already computed for baths.

## 7. Program completeness — residential staples not modeled

`program`/`require` is a strong mechanism, but the standard non-bedroom
storage/service spaces have no first-class checks: **linen closet**, **coat/entry
closet**, a **mechanical/utility space sized for equipment** (water heater, panel,
air handler), and a **whole-house storage ratio**. `NO_CLOSET` covers bedrooms;
the rest of the storage/service program is invisible.

## 8. Cost — still the roadmap's open item

`metrics()` already carries `footprint_sqft`, `exterior_wall_area_sqft`,
`roof_area_sqft`, `beam_linear_ft`, `post_count` and `foundation_concrete_yd3`.
`barndsl cost` with an overridable rate table (JSON/TOML sidecar) and a loud
"rough order-of-magnitude, not a bid" disclaimer is the highest-value hand-off
feature still unbuilt — every client conversation starts with budget.

---

## Suggested sequencing

1. **Make `orientation` real** (#1) — site/lot/setback + solar-aware glazing.
   Starts the design where design actually starts, and gives the inert grammar
   word a job.
2. **Section + elevation renderer** (#2) with **overhang/eave depth and porch
   roofs** (#3) in the model. Turns a plan tool into a design tool and gives the
   non-Revit user a real deliverable; the overhangs also become the *fix* the
   solar nudge from #1 recommends.
3. **The electrical / life-safety check batch** (#4) and the **thermal envelope**
   (#5) — pure deterministic wins in the established style.
4. **Furniture-fit** (#6), **program staples** (#7), then **`cost`** (#8).

Items #4, #5, #7 build directly on data structures that already exist and touch
only the validator/metrics. Items #1–#3 are the genuine design-tool expansion —
they add a site plane and a vertical dimension the model doesn't have yet.
