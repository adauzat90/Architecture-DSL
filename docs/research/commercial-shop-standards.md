# Commercial, Industrial & Workshop Planning Standards for Barndominium Shops

Research reference for authoring `barndsl` validation rules, design guidelines, and
scoring heuristics that govern the **shop / garage half** of a barndominium.

Everything here is paraphrased in the author's own words from the sources listed in
§1 plus the trade practice noted inline. Numeric dimensional standards are stated
plainly; no source text or table is reproduced. Where a number is *convention*
rather than *code*, it is labelled as such — this distinction matters, because
`barndsl` maps code to **errors**, consensus practice to **warnings**, and taste to
**infos / score deductions**.

Units: feet unless noted. `barndsl` works in feet with decimal fractions, so inch
values are given as decimals too (e.g. 30 in = 2.5 ft).

**Status of this document:** research input, not a spec. Section 8 maps the findings
onto the diagnostics that already exist so new rules extend rather than duplicate.

---

## 1. Overview of sources

### 1.1 Dimensional planning references

| Source | What it contributes here | How to treat it |
| --- | --- | --- |
| Neufert, *Architects' Data* (Wiley, 4th–6th eds.) | Body-and-machine ergonomics: workbench heights and reach, aisle and passage widths for people carrying loads, vehicle envelopes and turning circles, industrial planning grids, workshop and small-industry room programmes. Metric-native — figures below are converted and rounded to buildable US dimensions. | Best source for *human* clearances (bench depth, aisle width, working zones). Its vehicle data is European, so US vehicle sizes below come from AGS/manufacturer data instead. |
| De Chiara & Callender, *Time-Saver Standards for Building Types* | Building-type programmes: private and multi-car garages, service/repair garages, light industrial and loft buildings, agricultural buildings, small office suites. Gives programme ratios (support space as a fraction of shop floor) and adjacency diagrams. | Best source for *programme* rules — what support rooms a shop of a given size needs. |
| AIA / Ramsey & Sleeper, *Architectural Graphic Standards* | US vehicle design envelopes, turning radii and swept paths, parking stall and aisle geometry, driveway and apron layout, overhead door types and rough-opening details, slab-on-grade and apron detailing. | Best source for *vehicle geometry and site maneuvering*. |
| NFBA *Post-Frame Building Design Manual* and accepted-practice bulletins | Post-frame structural vocabulary: column spacing, truss spans, eave heights, framing tolerances, diaphragm vs. non-diaphragm design. NFBA's own prescriptive scope covers 40/50/60 ft truss spans against 12/16/20 ft eave heights. | Best source for *which envelope numbers are "normal"* — i.e. what should not trip a warning. |
| ICC codes: IRC (R302, R309, R311, R315, M1307), IBC (Ch. 5 §508, Ch. 7), IMC (§403), NEC (Art. 110, 210, 220) | The hard separation, floor, ventilation and electrical-clearance requirements between a shop bay and living quarters. | The only tier that justifies an **error**. Version-sensitive: cite the section, let a jurisdiction profile carry the number. |
| Industry practice: overhead-door manufacturers, RV/boat storage guidance, post-frame builders, machine-tool setup guides | Stock door sizes, headroom above openings, clear-height rules for vehicle classes, machine infeed/outfeed envelopes, dust and compressor placement. | Consensus practice → **warning** or **info** depending on how bad the failure mode is. |

### 1.2 Reliability tiers used throughout

Every rule candidate below carries one of these tags. It determines severity.

- **[CODE]** — a published code requirement. Justifies an **error** when violated
  unambiguously by declared geometry, or a **warning** when the model can only
  infer the condition.
- **[STANDARD]** — near-universal industry dimension (stock door sizes, vehicle
  envelopes, minimum bay depths). **Warning** when a plan is physically unusable,
  **info** when it is merely non-standard.
- **[PRACTICE]** — good planning consensus with real variance (zoning of dirty/clean
  work, support-room programme). **Info** plus a score deduction.
- **[TASTE]** — preference. **Score deduction only**, never a diagnostic.

---

## 2. Vehicle and bay planning

### 2.1 Vehicle and equipment envelopes

Overall dimensions of the *thing being parked*, including anything permanently
attached (mirrors, ladder racks, outdrives, tongues). Rounded up to the nearest
half-foot: a planning rule wants the conservative number, not the brochure number.

| Class | Length | Width (body) | Width (mirrors) | Height |
| --- | --- | --- | --- | --- |
| Motorcycle | 7.5 | 2.5 | — | 4.5 |
| ATV | 8.0 | 4.0 | — | 4.0 |
| UTV / side-by-side | 12.0 | 5.5 | — | 6.5 |
| Zero-turn mower (ROPS up) | 7.0 | 6.0 | — | 6.0 |
| Compact car | 15.0 | 6.0 | 6.8 | 5.0 |
| Mid-size sedan | 16.5 | 6.2 | 7.0 | 5.0 |
| Full-size sedan | 17.5 | 6.3 | 7.2 | 5.0 |
| Compact SUV / crossover | 15.5 | 6.2 | 7.2 | 5.5 |
| Mid-size SUV | 17.0 | 6.5 | 7.5 | 6.0 |
| Full-size SUV (extended) | 19.0 | 6.8 | 7.8 | 6.5 |
| Minivan | 17.5 | 6.5 | 7.5 | 6.0 |
| Half-ton crew cab, short bed | 20.0 | 6.8 | 8.0 | 6.5 |
| Half-ton crew cab, 6.5 ft bed | 21.0 | 6.8 | 8.0 | 6.5 |
| 3/4–1 ton crew cab, 8 ft bed | 22.5 | 6.8 | 8.8 | 6.8 |
| Dually crew cab, 8 ft bed | 22.5 | 8.0 | 9.0 | 6.8 |
| Cargo van, standard roof | 20.0 | 6.8 | 8.0 | 7.0 |
| Cargo van, high roof extended | 24.0 | 6.8 | 8.0 | 9.5 |
| Box truck, 16 ft body | 24.0 | 8.0 | 8.8 | 11.0 |
| Class B camper van | 21.0 | 7.0 | 8.0 | 10.0 |
| Class C motorhome | 30.0 | 8.5 | 9.5 | 11.5 |
| Class A motorhome | 40.0 | 8.5 | 10.0 | 13.0 |
| Travel trailer (mid) | 30.0 | 8.5 | — | 11.0 |
| Fifth-wheel trailer | 38.0 | 8.5 | — | 13.5 |
| Bass / bowrider boat + trailer | 24.0 | 8.5 | — | 8.5 |
| Boat with wakeboard tower | 26.0 | 8.5 | — | 11.0 |
| Cuddy / cabin boat + trailer | 32.0 | 9.0 | — | 11.5 |
| Enclosed cargo trailer, 16 ft box | 21.0 | 8.5 | — | 10.0 |
| Flatbed equipment trailer, 20 ft deck | 26.0 | 8.5 | — | 3.0 |
| Gooseneck trailer, 30 ft deck | 35.0 | 8.5 | — | 3.0 |
| Compact utility tractor + loader (ROPS up) | 13.0 | 5.5 | — | 8.5 |
| Utility tractor with cab | 15.5 | 7.5 | — | 10.0 |
| Row-crop tractor with cab | 21.0 | 9.5 | — | 11.5 |
| Skid steer with bucket | 12.5 | 6.0 | — | 7.0 |
| Compact track loader | 13.5 | 6.5 | — | 7.2 |
| Semi day-cab tractor | 22.0 | 8.5 | 10.0 | 13.0 |

Notes that matter for rule authoring:

- **Trailered things are longer than they look.** A boat's advertised length is the
  hull. Add roughly 3–5 ft for the trailer tongue and 1–2 ft for an outdrive or
  outboard hanging past the transom. A "24 ft boat" wants a 30 ft bay minimum.
- **Mirror width, not body width, sets door width.** Towing mirrors on a dually add
  about 2 ft over the body. Folded mirrors are not a plan assumption — mirrors get
  left extended.
- **ROPS-up height governs.** Tractor and mower heights are quoted with the rollbar
  folded; folding it every time is not a design assumption. Use the ROPS-up figure.
- **Height is the sneaky failure.** Length shortfalls are visible in plan and get
  caught. Height shortfalls (a boat tower, a high-roof van, a Class A) are invisible
  in a 2D plan and are the single most common expensive mistake in this building
  type. A validator that models `ceiling` and overhead-door `height` can catch what
  a human eye scanning a floor plan cannot.

### 2.2 Bay footprints

A **bay** here means the clear rectangle a vehicle occupies plus its working
clearances — not a structural bay. `barndsl` has no `bay` noun; a bay is a `shop` or
`garage` room, or a share of one.

| Bay purpose | Min clear W × D | Recommended W × D | Generous W × D | Clear height |
| --- | --- | --- | --- | --- |
| Compact car, park only | 10 × 20 | 12 × 22 | 14 × 24 | 8 |
| One full-size car / SUV | 12 × 22 | 14 × 24 | 16 × 26 | 9 |
| One pickup (crew cab, long bed) | 12 × 26 | 14 × 28 | 16 × 30 | 9 |
| Two cars, shared bay | 20 × 22 | 24 × 24 | 26 × 28 | 9 |
| Two trucks, shared bay | 24 × 26 | 26 × 30 | 30 × 32 | 10 |
| Three cars, shared bay | 30 × 22 | 34 × 24 | 36 × 28 | 9 |
| Vehicle bay + workbench wall | 14 × 30 | 16 × 32 | 18 × 36 | 10 |
| Working shop, one vehicle + tools | 20 × 30 | 24 × 36 | 30 × 40 | 12 |
| Shop with a 2-post lift | 14 × 26 per lift | 16 × 28 | 18 × 30 | 12 (14 for trucks) |
| Boat + trailer | 12 × 30 | 14 × 34 | 16 × 40 | 12 |
| Class C motorhome | 14 × 34 | 14 × 38 | 16 × 42 | 13 |
| Class A motorhome | 14 × 44 | 16 × 48 | 16 × 55 | 14–16 |
| Fifth wheel + tow truck (in-line) | 14 × 55 | 16 × 60 | 16 × 65 | 14 |
| Tractor / implement | 14 × 30 | 16 × 36 | 18 × 40 | 14 |
| Drive-through bay (doors both ends) | 14 × 40 | 16 × 48 | 18 × 60 | 12–14 |

**Rules of thumb behind those numbers**

- Bay width = vehicle body width + 6 ft is comfortable (3 ft each side). Vehicle
  width + 5 ft is the practical floor. Below vehicle + 4 ft a person cannot open a
  door and get out without contortion.
- Bay depth = vehicle length + 4 ft is the working floor (2 ft nose, 2 ft tail).
  Vehicle length + 6 ft is comfortable. Add another 4–8 ft if a workbench, shelving
  or a freezer lives on the end wall — that band is *not* parking depth.
- In a shared multi-vehicle bay, the per-vehicle width allowance is smaller than a
  private bay because door swings interleave: budget 11–12 ft per car and 13–14 ft
  per truck across a shared bay, plus 1.5–2 ft at each outside wall.
- A bay whose depth exceeds ~2.2× its width reads as a tunnel: hard to maneuver,
  hard to light, and the far end becomes dead storage. A bay whose width exceeds
  ~2.5× its depth cannot fit a vehicle plus its door-swing at all in the short
  direction. Both are worth flagging.

### 2.3 Clearances around a parked vehicle

Neufert-style body-and-machine clearances, applied to garages:

| Condition | Clearance |
| --- | --- |
| Side of vehicle to wall — squeeze past only | 1.8 |
| Side of vehicle to wall — open door, exit normally | 2.5 |
| Side of vehicle to wall — exit plus carry something | 3.0 |
| Side of vehicle to wall — load a child seat, work on the vehicle | 4.0 |
| Between two vehicles side by side | 2.5 min, 3.0 preferred |
| Front of vehicle to wall — walk past | 2.0 |
| Front of vehicle to wall — shelving above a bumper | 2.5 |
| Front of vehicle to workbench actually in use | 4.0 |
| Front of vehicle to wall — open the hood and work | 4.0 |
| Rear of vehicle to wall — walk past | 2.0 |
| Rear of vehicle to wall — open a hatch or tailgate | 3.5 |
| Rear of truck to wall — tailgate down and load | 5.0 |
| Around a vehicle on a 2-post lift, all sides | 4.0 |
| Car door swing, normal open | 2.5 |
| Car door swing, full open | 3.5 |
| Truck tailgate down, projection | 3.0 |
| Hood open, forward projection past bumper | 1.0 |

### 2.4 Overhead door selection

**Stock sectional door sizes.** Widths in feet: 8, 9, 10, 12, 14, 16, 18, 20.
Heights in feet: 7, 8, 9, 10, 12, 14, 16. Residential single doors are 8×7 or 9×7;
9×8 is the standard upgrade for taller trucks; 16×7 and 16×8 are the standard
doubles; 10×10 and 12×12 are the standard shop doors; 14×14 and 16×16 are the RV
and equipment doors. Anything off this grid is a special order and costs
disproportionately. [STANDARD]

**Sizing rules** [STANDARD]

- Door **width** ≥ vehicle mirror width + 3.0 ft. A 6.8 ft body with 8 ft mirrors
  wants an 11 ft opening → specify 12 ft.
- Door **width** ≥ vehicle body width + 4.0 ft when a trailer is backed through it,
  because a trailer tracks inside the tow vehicle's path and the driver is aiming
  by mirror. An 8.5 ft trailer wants a 12 ft door minimum, 14 ft to be pleasant.
- Door **height** ≥ vehicle height + 1.0 ft. Cars: 7 ft is enough only for sedans;
  8 ft covers SUVs and most pickups; 9–10 ft covers high-roof vans, boats with
  towers and small tractors; 12 ft covers Class C motorhomes and cab tractors;
  14 ft covers Class A motorhomes and fifth wheels; 16 ft is for semi and
  agricultural equipment.
- One 16 ft double vs. two 9 ft singles: the double is cheaper per opening and
  easier to aim at; two singles are structurally cheaper (no long header), lose less
  heat, and give a post between them. Both are correct — this is a **[TASTE]** call,
  never a diagnostic.

**Headroom and structure above the opening** [STANDARD]

- A standard-lift torsion door needs roughly 1.25–1.5 ft of clear wall above the
  opening for the spring shaft and curved track; low-headroom hardware gets it to
  ~1.0 ft; high-lift and vertical-lift need much more (proportional to the lift).
- Practical envelope rule: **eave/wall height ≥ door height + 2.0 ft**. A 14 ft door
  needs a 16 ft eave. A 12 ft door needs 14 ft. This is the single most quoted
  number in the post-frame trade and is a clean machine check against `ceiling`.
- A roll-up (coiling) door needs about 1 additional foot above the opening for the
  barrel and hood, so the same rule holds with margin.
- **Horizontal track intrusion**: a standard-lift door parks its leaf horizontally at
  ceiling level, extending roughly `door height + 1.5 ft` back into the room. A 12 ft
  door parked open occupies ~13.5 ft of ceiling. That strip cannot hold ductwork,
  lighting, a hoist, or storage racks, and the room must be at least that deep for
  the door to open at all.

**Wall geometry around the opening** [STANDARD]

- Leave ≥ 2.0 ft of wall between an overhead door jamb and the building corner;
  3.0–4.0 ft is better, because the corner post and any wall bracing live there.
- Leave ≥ 2.0 ft of pier between two overhead doors on the same wall; 3.0–4.0 ft is
  better so a structural column lands in the pier.
- The door opening should land inside one structural bay wherever possible: align
  jambs to column lines. An opening that spans a column requires that column to be
  removed and a carrier beam / header truss added — always possible, never free.
- Header span: past roughly 10 ft an opening outruns a conventionally sized stock
  header in light-frame construction and needs an engineered header or a truss
  carrier. In post-frame this is normal and expected, but it is still a cost note.

### 2.5 Ceiling, eave and clear-height rules

| Use | Minimum clear height | Comfortable | Why |
| --- | --- | --- | --- |
| Cars only, no storage | 8 | 9 | Door leaf + opener rail |
| Cars + overhead storage racks | 9 | 10 | 2 ft of rack above a 6.5 ft vehicle |
| General shop, no lift | 10 | 12 | Standing work, long stock, ventilation |
| 4-post storage lift (stack two cars) | 11.5 | 12 | Two vehicle heights + lift structure |
| 2-post lift, cars | 12 | 12 | Full rise with a person underneath |
| 2-post lift, trucks | 13 | 14 | Taller vehicle, same rise |
| Overhead crane / gantry | hook height + 4 | — | Hoist, trolley, beam depth |
| Shop with a mezzanine/loft above | 16 | 18 | 8 ft below + floor depth + 7 ft above |
| RV bay | 14 | 16 | 13 ft vehicle + door headroom |

`barndsl` already models a single plan-wide `ceiling`. A shop and a residence wing
almost never want the same ceiling: 9–10 ft is right for living, 12–16 ft for the
shop. That is a **modelling gap** worth noting (see §8.3).

### 2.6 Apron, drive and maneuvering

Getting a vehicle *to* the door is half the problem. [STANDARD]

| Item | Dimension |
| --- | --- |
| Drive width, single lane | 10–12 |
| Drive width to a shop (trailers) | 14–16 |
| Drive width at the door face, two-abreast | 20–24 |
| Apron depth in front of a car bay (back and turn) | 24 min, 30 comfortable |
| Apron depth in front of a truck/trailer bay | 40–45 |
| Apron depth for an RV or gooseneck | 50–60 |
| Turnaround pad (car) | 20 × 20 |
| Turnaround pad (truck + trailer) | 40 × 40 |
| 90° parking stall | 9 × 18 |
| Two-way drive aisle beside 90° stalls | 24 |
| Outside turning radius, passenger car | 20 |
| Outside turning radius, pickup / dually | 26 |
| Outside turning radius, Class A motorhome | 42–45 |
| Apron slab slope away from the door | 1–2 % (0.125–0.25 in/ft) |
| Apron slab length, minimum durable | 10–12 |

Practical corollary: the depth of clear ground in front of the door should be at
least the depth of the bay behind it. A 40 ft RV bay with a 20 ft apron is a
building you cannot use.

### 2.7 Rule candidates — vehicle and bay planning

| Code | Sev | Tier | Threshold / condition | Notes |
| --- | --- | --- | --- | --- |
| `BAY_DEPTH_SHORT` | warning | STANDARD | A `garage` or `shop` room whose depth measured perpendicular to its overhead door is < 20 ft. | Below 20 ft nothing bigger than a compact parks. Existing `SHOP_DEPTH` checks the *short* dimension generically; this one is door-relative and so catches a wide-but-shallow bay. |
| `BAY_DEPTH_TIGHT` | info | STANDARD | Door-relative depth 20–24 ft on a `shop`; 20–22 ft on a `garage`. | "A crew-cab pickup is 22 ft; add 4 ft of working room." Score: −1 per offending bay, cap −3. |
| `BAY_WIDTH_TIGHT` | warning | STANDARD | Bay clear width < (widest declared overhead door width on that room + 4 ft), or < 12 ft absolute. | A door wider than its room means the vehicle cannot be centred and no door can be opened. |
| `BAY_DOOR_WIDTH` | info | STANDARD | Overhead door width < 9 ft on a `shop`; < 8 ft anywhere. | An 8 ft door is a sedan door. Shops take trucks. |
| `BAY_DOOR_HEIGHT` | info | STANDARD | Overhead door height ≤ 8 ft on a `shop`. | Already implemented as `SHOP_DOOR_HEIGHT` — extend to also fire when the plan's `ceiling` ≥ 12 (a tall bay with a short door wastes the height it paid for). |
| `BAY_DOOR_NONSTANDARD` | info | STANDARD | Door width ∉ {8,9,10,12,14,16,18,20} or height ∉ {7,8,9,10,12,14,16}. | Existing `DOOR_SIZE` / `OVERHEAD_SIZE` — recommend extending the constant tuples: widths add 14/18/20, heights add 9/16. |
| `BAY_DOOR_HEADROOM` | warning | STANDARD | `ceiling` < overhead door height + 2.0 ft. | The eave-vs-door rule. A 14 ft door under a 14 ft ceiling cannot be built. Error-adjacent; warning because `ceiling` is a plan-wide proxy. |
| `BAY_DOOR_TRACK` | info | STANDARD | Bay depth perpendicular to the door < door height + 4 ft. | The parked leaf needs `height + 1.5` of ceiling; anything less and the door cannot fully open. |
| `BAY_DOOR_CORNER` | info | STANDARD | Overhead door `offset` < 2.0 ft from a wall end, or (wall length − offset − width) < 2.0 ft. | Corner post and bracing need the wall. |
| `BAY_DOOR_PIER` | warning | STANDARD | Two overhead doors on the same wall separated by < 2.0 ft of wall. | Below 2 ft there is no column and no jamb. |
| `BAY_DOOR_HEADER` | info | STANDARD | Overhead door width > 10 ft. | Already implemented as `OVERHEAD_HEADER`. Keep; downgrade its urgency for `shop` rooms since wide doors are normal there — consider suppressing below 16 ft on a post-frame `frame` declaration. |
| `BAY_DOOR_ORIENT` | info | PRACTICE | Overhead door on a wall that faces the street when a quieter wall is available, or that faces the living wing's principal glazing. | Complements the existing `APPROACH_GARAGE`. Curb-appeal + noise. Score −1. |
| `BAY_DOOR_MISSING` | warning | STANDARD | A `garage` or `shop` room ≥ 200 sq ft with no overhead door and no exterior door on any exterior wall. | A sealed shop is a modelling mistake almost every time. |
| `BAY_APRON` | info | PRACTICE | A `site` + `drive` are declared and the clear ground in front of an overhead door (measured along the door's outward normal to the site edge or the nearest obstruction) is < the bay's depth. | Needs the site layer, so only fires on plans that opted in. |
| `BAY_APRON_NONE` | info | PRACTICE | Overhead door declared, `site` declared, but no `drive` polygon within 10 ft of the door's outward face. | Mirrors the existing `walk`-to-door reachability idea for vehicles. |
| `BAY_ASPECT` | info | TASTE | Bay long:short ratio > 2.2:1 for a room ≥ 300 sq ft. | Tunnel bays. Score: fold into the existing proportion component but exempt genuine RV/drive-through bays (depth > 36 ft with doors on both ends). |
| `BAY_HEIGHT_LIFT` | info | PRACTICE | A `shop` ≥ 600 sq ft with `ceiling` < 12. | "12 ft eave is the price of admission for a lift." Purely advisory. |
| `BAY_HEIGHT_RV` | warning | STANDARD | Any bay depth ≥ 36 ft (an RV-scale bay) with `ceiling` < 14 or with all overhead doors < 12 ft tall. | A 40 ft bay you can only get a car into is a planning contradiction. |

**Scoring heuristic — `vehicle_fit` (proposed, max 8 points):**
For each `garage`/`shop` room, compute the largest standard vehicle class from §2.1
that fits given (a) door width vs. mirror width + 3, (b) door height vs. height + 1,
(c) bay depth vs. length + 4, (d) bay width vs. body width + 5. Award the bay a
`fit_class` score: 0 = nothing fits, 1 = compact only, 2 = full-size car, 3 = pickup,
4 = truck + trailer, 5 = RV/equipment. Penalty = `8 × (1 − mean(fit_class)/3)`
clamped at 0 — i.e. full marks once the average bay takes a pickup, partial credit
below. Rationale: it is a *continuous gradient* an agent can hill-climb, which is
what the existing score module says it wants from continuous terms.

---

## 3. Workshop layout principles

### 3.1 Work zones

A shop is not one room; it is four overlapping zones that want different adjacencies,
different floor treatment and different ventilation. Time-Saver Standards' light
industrial programmes and Neufert's small-workshop layouts agree on the taxonomy
even where they use different names.

| Zone | Activities | Wants | Avoids |
| --- | --- | --- | --- |
| **Vehicle / drive** | Parking, in-out, loading | Door, slope to drain, hardest floor, 12+ ft clear | Being crossed by a work aisle |
| **Dirty / hot** | Welding, grinding, cutting, plasma | Noncombustible floor and wall surfaces, local exhaust, isolation from finishes and dust | Proximity to sawdust, solvents, upholstery |
| **Machine / loud** | Table saw, planer, lathe, mill, compressor, dust collector | Long clear runs, dust collection, 240 V, vibration-tolerant slab | Sharing a wall with bedrooms |
| **Clean / assembly** | Bench work, electronics, finishing, layout | Best light, conditioned or semi-conditioned, level clean floor | Airborne dust and weld spatter |
| **Support** | Bath, mechanical, compressor, storage, office | Plumbing wall, service entrance, quiet | Consuming prime bay depth |

**Adjacency logic to encode**

- Dirty/hot at the **far end** from the living wing. Clean/assembly nearest the
  living wing — it is the zone that most wants heat and light, and it is the least
  offensive neighbour.
- Loud (compressor, dust collector) should not share a partition with a `bedroom`.
  It may share with `storage`, `utility`, `mechanical`, `laundry`, `garage`.
- Wet (parts washer, sink, shop bath) should back onto the same plumbing wall as the
  house wet group where geometry allows — this is the single biggest cost saving
  available in a barndominium and `barndsl` already has `wall a - b plumbing` and a
  `WET_GROUP` nudge to express it.
- Material flow should be linear: in → rough cut → machine → assemble → finish →
  out. A drive-through bay (doors on opposite walls) makes this trivially true and
  is the reason drive-through shops are so popular.

### 3.2 Bench, machine and aisle dimensions

**Workbenches** [STANDARD / Neufert-derived]

| Item | Dimension |
| --- | --- |
| Bench depth, general purpose | 2.0–2.5 |
| Bench depth, heavy / assembly | 3.0 |
| Bench depth, absolute minimum useful | 1.5 |
| Bench height, general work (standing) | 2.8–3.0 (34–36 in) |
| Bench height, hand-tool woodworking | 2.5–2.8 (30–34 in) |
| Bench height, precision / seated | 2.5 (30 in) |
| Bench length, minimum useful run | 4.0 |
| Bench length, comfortable | 6.0–8.0 |
| Clear standing zone in front of a bench | 3.0 min, 4.0 preferred |
| Total wall band consumed by bench + user | 5.5–6.5 |
| Wall band for shelving/cabinets only | 1.5–2.0 |
| Wall band for a tool wall (pegboard/french cleat) | 1.0 |

**Aisles and circulation** [STANDARD / Neufert + OSHA-flavoured]

| Aisle purpose | Width |
| --- | --- |
| One person, sideways squeeze | 1.8 |
| One person walking | 2.5 |
| One person carrying material | 3.0 |
| Egress aisle, keep-clear minimum | 3.0 |
| Two people passing | 4.0 |
| Person with a hand truck or shop cart | 4.0 |
| Two-way traffic with material | 6.0 |
| Pallet jack / mower / mobile-base tool transit | 8.0 |
| Vehicle transit through the shop interior | 12.0 |
| Sheet-goods transit (4×8 panel carried flat) | 5.0 |
| Long-stock transit (16 ft lumber, turning a corner) | 8.0 clear at the corner |

Neufert's principle underneath the numbers: an aisle's width is the body width of
whatever moves through it plus a shoulder allowance each side, and mechanical
handling equipment adds its own width plus roughly 3 ft of clearance total.

**Machine working envelopes** [STANDARD / trade practice]

The footprint of a machine is never its working footprint. Budget the *envelope*.

| Machine | Machine footprint | Working envelope (total clear rectangle) |
| --- | --- | --- |
| Table saw, sheet goods | 3 × 3 | 20 × 12 (10 ft infeed + 10 ft outfeed in the rip direction, 4 ft each side) |
| Table saw, mobile base, small shop | 3 × 3 | 12 × 10 (accept 6 ft infeed/outfeed, roll it out for sheets) |
| Miter saw station | 2 × 3 | 12–16 ft of wall × 4 ft deep (6–8 ft of support each side of the blade) |
| Jointer, 6–8 in | 1.5 × 6 | 6 ft clear at each end (18 ft total run) × 3 ft |
| Planer, 13–20 in | 2.5 × 3 | 8 ft clear at each end (19 ft total run) × 3 ft |
| Bandsaw | 2.5 × 2.5 | 8 × 6 |
| Drill press | 2 × 2 | 6 × 5 |
| Wood lathe | 2 × 6 | 10 × 6 (4 ft at the headstock, 3 ft at the tailstock, 3 ft along the bed) |
| Metal lathe, 12–14 in swing | 2.5 × 7 | 12 × 7 (bar stock passes through the spindle) |
| Vertical mill (knee) | 3 × 4 | 8 × 8 |
| Welding table | 3 × 5 | 11 × 13 (4 ft clear all round for a person with a torch and a part) |
| Media blast cabinet | 3 × 4 | 6 × 6 |
| Parts washer | 2 × 3 | 5 × 5 |
| Tire machine + balancer | 4 × 4 each | 12 × 10 combined |
| 2-post vehicle lift | 12 × 14 (columns) | 22 × 20 (approach + door swing + walk-around) |
| Dust collector, 2 HP cyclone | 3 × 3, 8 ft tall | 5 × 5 plus duct run |
| Air compressor, 60–80 gal vertical | 2.5 × 2.5, 6 ft tall | 4 × 5 with service access |

**Fire and process separation** [CODE-adjacent / PRACTICE]

- Hot work (welding, cutting, grinding) wants combustibles relocated well away —
  OSHA's general-industry rule is roughly 35 ft of clear radius, or shielding with
  noncombustible curtains/screens where that is impossible. In a home shop the
  practical translation is: a dedicated welding corner with a bare slab, metal or
  gypsum-faced walls, no wood storage, and no solvent storage within the bay.
- Flammable liquids (solvents, fuels, finishes) belong in a listed flammable-storage
  cabinet, sited away from hot work and away from any ignition source; quantities
  above a modest threshold trigger real fire-code provisions that are outside the
  scope of a residential accessory shop but worth an advisory note.
- Finishing / spray work needs its own exhausted room with metallic ductwork, bonded
  and grounded metal parts, and no ignition sources — never the same air volume as
  welding, and never sharing return air with the dwelling.
- Dust collection is best placed **outside** the conditioned/occupied volume (an
  external lean-to or a dedicated closet with makeup air), both for noise and
  because a collector recirculating fines back into a shop defeats its purpose.

### 3.3 Support spaces in a shop

Time-Saver Standards-style programme ratios, converted to barndominium scale:

| Support space | Trigger | Minimum size | Comfortable |
| --- | --- | --- | --- |
| Shop half-bath | Shop ≥ 600 sq ft, or shop that is not directly adjacent to a house bath | 3 × 6 | 5 × 7 |
| Shop full bath / shower | Shop ≥ 1200 sq ft, or dirty work (welding, ag) | 5 × 8 | 6 × 9 |
| Mechanical (shop heat, water heater, softener) | Any shop with plumbing or a dedicated heater | 5 × 6 | 6 × 8 |
| Compressor closet | Any fixed compressor | 4 × 5 | 5 × 6 |
| Dust-collector closet | Any fixed collector | 5 × 5 | 6 × 6 |
| Shop storage / parts room | Shop ≥ 800 sq ft | 6 × 8 | 8 × 12 |
| Shop office / desk | Shop ≥ 1000 sq ft or any commercial use | 8 × 8 | 10 × 12 |
| Electrical panel wall | Always | 3 ft wide × 3 ft deep clear | 4 × 4 |
| Loft / mezzanine storage | Optional | 8 ft deep | 12–16 ft deep |

Programme rule of thumb: support space runs roughly **8–15 % of shop floor area** in
a working shop, and closer to 3–5 % in a pure vehicle-storage bay. Below 5 % in a
1000+ sq ft working shop, the shop will end up storing its own support functions in
the middle of the floor.

**Electrical working space** [CODE — NEC 110.26]
A panelboard needs clear working space of at least **2.5 ft wide** (or the width of
the equipment, whichever is greater), **3.0 ft deep** in front, and **6.5 ft high**,
kept clear and not used for storage. This is a genuinely machine-checkable rule when
a plan declares a panel location, and it is violated constantly in real shops
(panels behind shelving, panels behind a parked truck). `barndsl` has an `electrical`
opt-in and `outlet`/`switch`/`light` nouns; a `panel` noun would make this checkable.

**Shop electrical service planning** [PRACTICE]

| Load | Typical circuit |
| --- | --- |
| Lighting | 15–20 A, 120 V |
| General receptacles | multiple 20 A, 120 V; one per 8–10 ft of bench wall |
| Dust collector, 2 HP | 20–30 A, 240 V |
| Air compressor, 5 HP | 30 A, 240 V |
| MIG/TIG welder, 250 A class | 50 A, 240 V |
| Plasma cutter | 40–50 A, 240 V |
| Vehicle lift | 20–30 A, 240 V |
| EV charger, level 2 | 40–60 A, 240 V |
| Shop heater, electric | 30–60 A, 240 V |

A working home shop with a welder, compressor, dust collector and general circuits
lands around 100–150 A of *calculated* demand once NEC Article 220 demand factors and
the largest-motor 125 % rule are applied. **100 A is the practical minimum shop
subpanel; 150–200 A is the right answer for a shop that welds.** Undersizing the
feeder is the most expensive shop mistake after undersizing the door.

### 3.4 Rule candidates — workshop layout

| Code | Sev | Tier | Threshold / condition | Notes |
| --- | --- | --- | --- | --- |
| `SHOP_BENCH_BAND` | info | PRACTICE | A `shop` whose clear width < (largest vehicle width that fits + 6 ft) — i.e. no wall band left over for a bench once a vehicle is parked. | Concrete restatement of "a bay is not a shop." Complements existing `SHOP_DEPTH`. |
| `SHOP_AISLE` | warning | STANDARD | Any clear circulation path through a `shop` narrower than 3.0 ft, or narrower than 4.0 ft between a declared `fixture` and a wall. | Requires fixture-aware pathing, which `barndsl` already does for baths. |
| `SHOP_EGRESS_AISLE` | warning | CODE | The route from the deepest point of a `shop` to a people-door passes through a clear width < 3.0 ft. | Egress aisles must stay clear. |
| `SHOP_NO_PEOPLE_DOOR` | warning | PRACTICE | A `shop`/`garage` ≥ 400 sq ft whose only openings are overhead doors. | Existing logic already refuses to count an overhead door as an entrance; make it explicit per-bay. A shop should have a walk door so the big door stays shut. |
| `SHOP_SUPPORT_NONE` | info | PRACTICE | A `shop` ≥ 800 sq ft with no adjacent `storage`, `utility`, `mechanical`, `half_bath`, `bathroom` or `office`. | Score −2. |
| `SHOP_BATH` | info | PRACTICE | A `shop` ≥ 600 sq ft whose shortest interior route to any `bathroom`/`half_bath` crosses ≥ 2 rooms or > 40 ft. | Shop users track dirt through the house. Score −1. |
| `SHOP_SUPPORT_RATIO` | info | PRACTICE | Support area (storage + mechanical + utility + office + shop bath, adjacent to the shop) < 5 % of shop area, when shop ≥ 1000 sq ft. | Programme ratio check. |
| `SHOP_LOUD_BEDROOM` | warning | PRACTICE | A `mechanical` or `utility` room adjacent to a `shop` shares a partition with a `bedroom`. | Compressors run at 80–90 dBA. Complements the existing `BED_SOUND` party-wall nudge. |
| `SHOP_PANEL_CLEAR` | warning | CODE | If a `panel` noun is added: any `fixture`, `counter` or room boundary within 3.0 ft in front of, or 2.5 ft across, the panel. | NEC 110.26. High-value, currently unmodellable. |
| `SHOP_PANEL_MISSING` | info | PRACTICE | `electrical` opted in, a `shop` ≥ 600 sq ft exists, no panel declared in the shop or an adjacent utility/mechanical room. | Reminder-class, like the existing `ALARM_CO`. |
| `SHOP_ZONE_MIX` | info | PRACTICE | If a `use` / `activity` annotation is added to shop rooms: a `welding` or `hot` zone adjacent to a `finish`/`paint` or `wood` zone with no intervening partition. | Needs new vocabulary; documented here for when it exists. |
| `SHOP_DUST_INSIDE` | info | PRACTICE | A declared dust collector fixture placed in the main shop volume rather than a closet or an exterior lean-to. | Advisory. |
| `SHOP_FLOW_LINEAR` | info | TASTE | A drive-through shop (overhead doors on opposite walls) scores positively; a shop with all doors on one wall and depth > 36 ft scores negatively. | Score only, ±2. |
| `SHOP_LIGHT` | info | PRACTICE | A `shop` ≥ 400 sq ft with zero windows and zero skylights. | Not a code issue (a shop is not habitable), but a windowless shop is a bad shop. Score −1. Mirror of the existing `BATH_VENT` reasoning. |
| `SHOP_LOFT_HEADROOM` | warning | CODE | A `loft` over a `shop` where `ceiling` (shop) + `floor` depth + loft clear height exceeds the declared eave, or loft clear height < 7.0 ft. | The multi-level path already exists (`shop_loft.barn`); this closes the vertical budget. |

**Scoring heuristic — `shop_program` (proposed, max 6 points):**
Only active when the plan contains ≥ 200 sq ft of `shop`. Components:
support-ratio shortfall (0–2), bench-band shortfall (0–2), bath-distance (0–1),
windowless (0–1). Zero for a plan with no shop, so a pure-residential plan is not
penalised by a term that does not apply to it — important, because the existing
score contract is that a clean plan scores 100.

---

## 4. Structural and envelope conventions: post-frame and steel shops

### 4.1 Post-frame vocabulary

| Element | Convention |
| --- | --- |
| Column (post) spacing | 8 ft o.c. is the default; 9, 10 and 12 ft o.c. are common with larger columns and heavier purlins. 12 ft is about the practical maximum for standard prescriptive designs. |
| Column size | 6×6 nominal, or 3-ply 2×6 laminated, for typical 12–16 ft eaves; 6×8 / 3-ply 2×8 for tall or heavily loaded walls. |
| Column embedment | 4 ft is the common depth; must reach below frost. Concrete footing pad under every column. |
| Truss span (clear) | Standard set: 24, 30, 36, 40, 48, 50, 60, 70, 80 ft. NFBA's own prescriptive envelope centres on 40 / 50 / 60 ft. 40–60 ft covers almost every barndominium. |
| Eave (wall) height | 8, 10, 12, 14, 16, 18, 20 ft. NFBA's prescriptive set uses 12 / 16 / 20. Residential wings 10–12; shops 12–16; RV bays 16–18. |
| Roof pitch | 4:12 is the workhorse. 3:12 is about the minimum for exposed-fastener steel. 5:12 to 8:12 for a residential look or attic trusses. |
| Overhang / eave projection | 1.0–2.0 ft. 1.5 ft is the sweet spot and also satisfies the "real summer shade" threshold `barndsl` already uses for south glazing. |
| Girts / purlins | Wall girts at 2 ft o.c. typical; roof purlins at 2 ft o.c. |
| Skirt board / splash plank | Pressure-treated 2×8 or 2×10 at the base of the wall, sitting on grade, retaining the slab edge. |
| Wainscot | 3 ft is standard (a 39 in steel panel run horizontally); 4 ft is the common upgrade. Purely aesthetic + impact protection. |
| Bracing | Diaphragm design relies on the roof and wall sheathing; large openings interrupt shear walls and must be compensated. |

**Modularity.** The building length should be a whole multiple of the column
spacing. A 62 ft building on 8 ft centres means one odd 6 ft bay; a 64 ft building
is seven 8 ft bays plus nothing. Same for openings: door and window locations that
land on column lines cost nothing; ones that land mid-bay cost a header.

**Steel (pre-engineered metal building) equivalents.** Frame spacing 20–25 ft o.c.
is typical, clear spans 40–100+ ft, eave heights the same set. Girts and purlins at
5–7 ft o.c. The planning consequences are similar; only the numbers move.

### 4.2 Slab

| Item | Convention | Tier |
| --- | --- | --- |
| Thickness, cars only | 4 in nominal | STANDARD |
| Thickness, trucks / trailers / equipment | 5–6 in | STANDARD |
| Thickness, under a 2-post lift or heavy machine | 6–8 in, often a thickened pad | STANDARD |
| Reinforcement, light | fibre mesh or 6×6 W1.4 WWF | PRACTICE |
| Reinforcement, equipment slab | #4 bar at 12–18 in o.c. each way | PRACTICE |
| Control joints | spacing in feet ≈ 2–3 × slab thickness in inches (4 in → 8–12 ft; 6 in → 12–18 ft); keep panels near-square | STANDARD |
| Slope to the vehicle door or a drain | 1/8 in/ft minimum; 1/8–1/4 in/ft is the practical band | CODE (IRC R309.1) |
| Vapour retarder under conditioned slab | required under living; recommended under any slab that will be coated | CODE / PRACTICE |
| Thickened edge at door openings | 12–16 in deep haunch | PRACTICE |
| Apron slope, away from the building | 1–2 % | STANDARD |
| Trench drain across a wash bay | 4–8 in wide, sloped to a sand/oil interceptor | PRACTICE |
| Floor drain in a shop | needs an oil/sand interceptor and a legal discharge — often the reason a shop slopes to the door instead | CODE, jurisdictional |

**The slope decision is a plan decision, not a detail.** A slab that drains to the
overhead door requires the door to be at the low point — which conflicts with a
drive-through bay (two doors, one high one low), with a bay backing onto the living
wing, and with any bay where the apron slopes toward the building. A `barndsl` plan
that knows where the overhead doors are and knows the site grade can catch this.

### 4.3 Rule candidates — structure and envelope

| Code | Sev | Tier | Threshold / condition | Notes |
| --- | --- | --- | --- | --- |
| `FRAME_BAY_WIDE` | info | STANDARD | `frame bay` > 12 ft. | Already implemented as `BAY_WIDE`. Keep; consider raising to a warning above 16 ft for post-frame. |
| `FRAME_BAY_NARROW` | info | TASTE | `frame bay` < 8 ft. | Buildable but wasteful — you are paying for posts you do not need. Score −0.5. |
| `FRAME_MODULE` | info | PRACTICE | Envelope length (the dimension along the frame lines) is not a whole multiple of `frame bay`, remainder > 0.5 ft. | "A 62 ft building on 8 ft centres leaves a 6 ft orphan bay; 64 ft is clean." Cheap, deterministic, genuinely useful. |
| `FRAME_SPAN_LARGE` | info | STANDARD | `frame span` > 60 ft. | Buildable; leaves the prescriptive envelope and needs engineering. |
| `FRAME_SPAN_HUGE` | warning | STANDARD | `frame span` > 80 ft. | Past the normal wood-truss range. |
| `FRAME_SPAN_ENVELOPE` | warning | STANDARD | `frame span` ≠ envelope width (within tolerance) with no interior `bearing` wall declared. | Catches a declared span that does not match the building. |
| `FRAME_OPENING_BAY` | info | PRACTICE | An overhead door's opening (offset .. offset+width) straddles a computed frame line. | Needs the frame line positions, which `structure.py` already computes. Very high value: "shift the door 1.5 ft east to land in bay 3." |
| `EAVE_DOOR_CLEAR` | warning | STANDARD | `ceiling` < max overhead door height + 2.0. | Same rule as `BAY_DOOR_HEADROOM`; register once, in whichever category reads better (structure). |
| `EAVE_SHOP_LOW` | info | PRACTICE | A `shop` ≥ 600 sq ft with `ceiling` < 12. | See `BAY_HEIGHT_LIFT`; pick one. |
| `SLAB_SLOPE_TARGET` | info | CODE | If a `slab` noun is added: garage/shop floor with no declared slope target. | IRC R309.1 requires a slope to the door or a drain. Reminder-class. |
| `SLAB_DRAIN_CONFLICT` | info | PRACTICE | A drive-through bay (overhead doors on opposite walls) with no declared floor drain. | You cannot slope to two doors. |
| `SLAB_THICKNESS` | info | PRACTICE | If a `slab` noun is added: thickness < 5 in on a bay whose door is ≥ 12 ft wide or ≥ 10 ft tall (i.e. a truck/equipment bay). | Advisory. |
| `WAINSCOT_HEIGHT` | info | TASTE | If a `wainscot` noun is added: height ∉ {3, 4}. | Purely cosmetic; low priority. |
| `ROOF_PITCH_LOW` | info | STANDARD | If a `roof pitch` noun is added: pitch < 3:12 with steel roofing. | Exposed-fastener steel wants 3:12+. |
| `OVERHANG_SHALLOW` | info | PRACTICE | Roof overhang < 1.0 ft on a wall carrying glazing. | The existing `MIN_SHADE_OVERHANG` of 1.5 ft already encodes the solar half of this. |

---

## 5. Mixed-use separation: shop vs. living quarters

This is the section with the most genuine **code** content, and therefore the most
justified errors. It is also where a barndominium differs most from both a house and
a shop building: the two occupancies share a slab, a roof, a frame and often a
service.

### 5.1 Fire separation (IRC path — most barndominiums)

An attached private garage under the IRC is not required to be a *fire-rated
assembly* in the technical sense; it is required to be **separated** by specified
materials. The distinction matters when writing hint text.

| Condition | Requirement | Section |
| --- | --- | --- |
| Wall between garage and residence | Not less than 1/2 in gypsum board (or equivalent), applied to the **garage side** | IRC R302.6 |
| Garage ceiling with **habitable space above** | Not less than 5/8 in Type X gypsum board on the garage side | IRC R302.6 |
| Structure supporting the separation | Same treatment as the separation it carries | IRC R302.6 |
| Garage attached to a dwelling by a common wall/attic | Separation continues to the roof sheathing at the wall line | IRC R302.6 |
| Opening from garage **directly into a sleeping room** | **Not permitted** | IRC R302.5.1 |
| Any other garage→dwelling door | Solid wood ≥ 1-3/8 in, or solid/honeycomb-core steel ≥ 1-3/8 in, or a 20-minute rated door — and **self-closing / self-latching** in current editions | IRC R302.5.1 |
| Ducts penetrating the separation | Steel ≥ 26 gauge (or other approved material), no openings into the garage | IRC R302.5.2 |
| Return air from a garage | Prohibited — a dwelling HVAC system may not draw return air from the garage | IRC M1602.2 |
| Other penetrations | Sealed per the fireblocking provisions | IRC R302.5.3 / R302.11 |
| Garage floor surface | Approved noncombustible material, sloped toward the main vehicle door or a drain | IRC R309.1 |
| Sprinklers | Where the dwelling is sprinklered, the garage is included | IRC R309.5 |
| Carbon monoxide alarm | Required outside sleeping areas in dwellings with an attached garage | IRC R315 |
| Ignition source elevation in a garage | Ignition source ≥ 18 in above the floor, unless the appliance is listed flammable-vapour-ignition-resistant | IRC M1307.3 |
| Appliance protection from vehicle impact | Required where subject to vehicle damage (bollard or equivalent) | IRC M1307.3.1 |

**A shop bay is a garage for this purpose.** `barndsl` already encodes exactly this
judgement in `GARAGE_TYPES = {GARAGE, SHOP}`, and it is the right call: an overhead
door, vehicles, fuel, solvents and hot work make a shop *more* hazardous than a
two-car garage, not less.

### 5.2 When the IBC path applies instead

A residential shop crosses into IBC territory when it stops being an accessory to the
dwelling — a business is run from it, it is leased, employees work in it, or the
jurisdiction simply classifies it separately. Then:

- The dwelling is **R-3**; a storage bay is **S-2** (or **S-1** for repair/hazardous
  storage); a fabrication shop is **F-1**.
- Section 508 gives three strategies: **accessory occupancy** (the minor occupancy
  stays below roughly 10 % of the floor area of the story and is not separated),
  **non-separated occupancies** (no separation, but the most restrictive
  height/area/construction requirements govern the whole building), and
  **separated occupancies** (a fire barrier per Table 508.4, with area ratios).
- Table 508.4's required ratings run 1–4 hours depending on the pair and are
  generally **reduced where the building is sprinklered throughout**. For an
  R-3-plus-storage barndominium the practical answer is usually 1 hour, but the
  table and the local amendment govern.
- Practical consequence for a design tool: the *strategy* is a declaration, not
  something to be inferred. A `barndsl` plan that declares a commercial use for a
  shop should get an informational note that IBC mixed-occupancy provisions apply
  and the IRC garage-separation rules alone are not sufficient — not a fabricated
  hour rating.

### 5.3 Sound isolation

Not code in a single-family dwelling, but the most common regret. [PRACTICE]

| Source in shop | Typical level | Wants |
| --- | --- | --- |
| Air compressor (piston) | 80–90 dBA | Isolated closet, ideally an exterior lean-to |
| Dust collector | 80–85 dBA | Closet or outside |
| Planer / thickness planer | 95–105 dBA | Not adjacent to a bedroom |
| Angle grinder | 90–100 dBA | Not adjacent to a bedroom |
| Overhead door opening | 70–75 dBA + structure-borne | Not directly under a bedroom |
| Vehicle start / exhaust | 80–95 dBA | Not sharing a wall with a bedroom |

Assembly targets: a plain 1/2 in gypsum / 2×6 / steel-liner shop wall is roughly
STC 35–40 — audible speech-level transfer. Adding cavity insulation, a second layer
of gypsum on the living side, and resilient channel or a staggered/double stud gets
to STC 50–55, which is the practical target for a shop-to-living partition. A shop
partition that also happens to be a `bedroom` wall should be treated as the
worst-case case and detailed to STC 55+, or better, avoided entirely by planning.

**Structure-borne noise** is the one that drywall does not fix: a compressor bolted
to a slab that continues under a bedroom transmits through the slab. Isolation pads
and, better, planning distance are the fixes.

### 5.4 Thermal and air separation

- The shop is typically **unconditioned or intermittently conditioned**; the living
  wing is conditioned. The common wall is therefore a **thermal boundary** and needs
  insulation, an air barrier, and continuity into the roof/ceiling plane — exactly
  as if it were an exterior wall.
- No shared return air (IRC M1602.2). A shop that needs heat wants its own unit
  heater, mini-split, or radiant slab loop — never a branch off the house system.
- Vehicle exhaust, solvent vapour and welding fume must not have a path into the
  dwelling. Practical rules: the separation door self-closes; penetrations are
  sealed; the shop is kept at neutral-to-negative pressure relative to the house
  when exhaust runs; and no dwelling supply or return grille opens into the shop.
- **Ventilation rates for reference** [CODE, IMC §403]: enclosed parking garages and
  motor-vehicle repair garages are ventilated at **0.75 cfm per sq ft** of floor
  area. Demand-controlled systems may idle at **0.05 cfm/sq ft** and step up to
  0.75 on CO/NO₂ detection or occupancy. These are commercial numbers and do not
  apply to a private residential garage, but they are the right order of magnitude
  for sizing an exhaust fan in a shop where an engine actually gets run: a
  1,000 sq ft shop wants roughly 750 cfm of exhaust while a vehicle runs, plus
  makeup air. A welding bay wants local source capture on top of that.

### 5.5 Transition spaces

The classic and correct answer to "how do I get from the shop to the house" is
**never a direct door into a living room, and never a direct door into a bedroom
(prohibited)**. Three good patterns:

1. **Mudroom / boot room airlock** — 6×8 minimum, 8×10 comfortable. Holds the rated
   self-closing door, a bench, boot storage, coat hooks, ideally a utility sink and
   a floor drain. Adjacent to the laundry is the classic pairing. This is the
   single highest-value 60 sq ft in a barndominium.
2. **Utility / laundry buffer** — the same idea with the machines in it. Cheaper
   (one room does two jobs), slightly worse (dirty traffic crosses clean laundry).
3. **Breezeway** — an open or semi-open covered space physically separating the two
   volumes. If genuinely open, the IRC dwelling-garage separation provisions may not
   apply at all because the garage is no longer attached; the trade-off is stepping
   outside in every weather and losing the shared-slab economy that makes a
   barndominium cheap.

Anti-patterns worth flagging: a shop door opening directly into the kitchen (food +
solvent + noise); a shop door into a hallway that serves bedrooms with no
intervening door; a bedroom sharing a party wall with a bay; habitable space
directly above an overhead door.

### 5.6 Utility sharing

| Utility | Good practice |
| --- | --- |
| Electrical | One service, one main; a dedicated **shop subpanel** of 100–200 A fed from the main. Keeps shop circuits out of the house load calc and gives one shutoff. |
| Water | One supply; branch to the shop with its own shutoff and, in cold climates, a drain-down or a heated wall. |
| Drain | Shop bath and utility sink tie into the house DWV where geometry allows — same reason the plumbing wall matters. Floor drains are a separate problem (interceptor, discharge permit). |
| Gas | If a shop heater is gas, it is a separate appliance with its own combustion air and venting; do not tee off a dwelling appliance. |
| HVAC | Fully separate. See §5.4. |
| Water heater | A shared water heater serving both is fine and economical *if* it is not in the shop at floor level (ignition-source and impact rules), and if the run length to the shop bath is reasonable. |

The single geometric decision that unlocks most of this: **put the shop's wet
functions on the same wall as the house's wet functions.** `barndsl` can already
express and check this with `wall <a> - <b> plumbing` and the `WET_GROUP` nudge.

### 5.7 Rule candidates — mixed-use separation

| Code | Sev | Tier | Threshold / condition | Notes |
| --- | --- | --- | --- | --- |
| `SEP_BEDROOM_DOOR` | **error** | CODE | A `door` connecting a `garage`/`shop` room directly to a `bedroom`. | IRC R302.5.1 flatly prohibits it. Unambiguous from declared geometry → error is correct. |
| `SEP_WALL_UNDECLARED` | info | CODE | A `garage`/`shop` room shares a partition with any dwelling room and that pair has no `wall a - b rated` declaration. | This is the existing `GARAGE_SEPARATION` reminder. Keep as a reminder — the tool cannot verify a wall assembly, only that the author thought about it. |
| `SEP_CEILING_HABITABLE` | warning | CODE | A habitable room on level *n+1* overlaps a `garage`/`shop` on level *n*. | Triggers the 5/8 in Type X ceiling requirement. Already partially present ("a garage ceiling under habitable space still reminds"); make it a warning when the overlap is a `bedroom`. |
| `SEP_DOOR_SELFCLOSE` | info | CODE | Any people-door between a `garage`/`shop` and the dwelling. | Reminder that the door must be 1-3/8 in solid / 20-minute and self-closing. Non-verifiable from geometry → info, always. |
| `SEP_KITCHEN_DOOR` | info | PRACTICE | A door from a `garage`/`shop` directly into a `kitchen`, `dining`, `living` or `great_room`. | Not prohibited, but the mudroom buffer is the right answer. Score −2. |
| `SEP_NO_BUFFER` | info | PRACTICE | No `mudroom`, `utility`, `laundry`, `foyer` or `hallway` on the shortest interior route between the shop and the living core. | The airlock rule. Score −2. |
| `SEP_BED_PARTY_WALL` | warning | PRACTICE | A `bedroom` shares a partition ≥ 6 ft long with a `garage`/`shop`. | Noise, vibration, CO risk, and the worst separation-detailing burden. Existing `BED_SOUND` covers bedroom-to-bedroom; this is the harsher case. |
| `SEP_BED_OVER_DOOR` | info | PRACTICE | A `bedroom` on level *n+1* overlaps the swept ceiling strip of an overhead door on level *n* (offset..offset+width × door height + 1.5 ft deep). | Door-opener noise directly under a bed. |
| `SEP_CO_ALARM` | warning | CODE | Bedrooms exist, an attached `garage`/`shop` exists, and no `co`/`smoke_co` alarm is declared outside the sleeping area. | IRC R315. Currently an info; the code requirement is absolute, so a warning is defensible. |
| `SEP_RETURN_AIR` | info | CODE | Reminder when a `mechanical` room is inside a `garage`/`shop` or opens only to it. | IRC M1602.2 — no return air from a garage. |
| `SEP_IGNITION_ELEV` | info | CODE | A `mechanical` room or a water-heater/furnace fixture located in a `garage`/`shop`. | IRC M1307.3: ignition source ≥ 18 in above the floor, plus impact protection. |
| `SEP_APPLIANCE_IMPACT` | info | CODE | Same trigger as above where the appliance is within a bay's parking footprint. | IRC M1307.3.1 bollard. |
| `SEP_VENT_SHOP` | info | PRACTICE | A `shop` ≥ 600 sq ft with no declared exhaust and at least one overhead door. | Advisory sizing note at 0.75 cfm/sq ft. |
| `SEP_COMMERCIAL_USE` | info | CODE | If a `use commercial` annotation is added to a shop: note that IBC §508 mixed-occupancy provisions apply and the IRC garage separation alone is insufficient. | Never fabricate an hour rating; point at the strategy choice. |
| `SEP_THERMAL_BOUNDARY` | info | PRACTICE | The shop/dwelling common wall is not declared `rated` **and** the plan declares an energy/insulation intent. | Weak; include only if the energy layer grows. |

**Scoring heuristic — `separation` (proposed, max 10 points):**
- 10 (i.e. the whole term) if any `SEP_BEDROOM_DOOR` exists — but this is already an
  error, and errors zero the score, so in practice this branch is unreachable and
  should be omitted.
- 4 for a bedroom sharing a party wall with a bay (`SEP_BED_PARTY_WALL`), scaled by
  shared-wall length / bedroom perimeter.
- 3 for no buffer room on the shop→living route.
- 2 for a shop door landing directly in a public room.
- 1 for an undeclared separation wall.

This complements — and does **not** duplicate — the existing `topology` term, which
measures whether bedrooms are *reachable only through* a garage. Topology is about
circulation; separation is about adjacency and detailing. A plan can pass one and
fail the other.

---

## 6. Cross-cutting rule-authoring guidance

### 6.1 Severity discipline

The barndominium shop domain tempts a rule author into over-erroring, because the
failure modes are expensive. Resist it. Suggested discipline:

- **Error** only when (a) a code section prohibits the exact declared thing
  (`SEP_BEDROOM_DOOR`), or (b) the geometry is self-contradictory (a door wider than
  its wall). There are very few of these in this domain.
- **Warning** when the plan is buildable but the stated purpose cannot be achieved —
  a 14 ft door under a 14 ft ceiling, a 40 ft RV bay with a 9 ft door, a bedroom
  sharing a bay wall.
- **Info** for everything that is a judgement call, a reminder about a detail the
  tool cannot verify, or a programme ratio.
- **Score only** for taste, and for continuous gradients that give an agent
  something to descend.

### 6.2 Thresholds should be constants, not literals

Everything in §7 should land in `constants.py` alongside `MIN_SHOP_DEPTH` and
`SHOP_COMFORT_DEPTH`, so a jurisdiction or a user profile can override it. Proposed
names, following the existing style:

```
MIN_BAY_DEPTH_CAR = 20.0
MIN_BAY_DEPTH_TRUCK = 26.0
COMFORT_BAY_DEPTH_TRUCK = 28.0
MIN_BAY_WIDTH_SINGLE = 12.0
COMFORT_BAY_WIDTH_SINGLE = 14.0
MIN_BAY_WIDTH_DOUBLE = 20.0
COMFORT_BAY_WIDTH_DOUBLE = 24.0
BAY_SIDE_CLEARANCE = 2.5          # vehicle side to wall, exit normally
BAY_SIDE_CLEARANCE_WORK = 4.0     # room to work on the vehicle
BAY_END_CLEARANCE = 2.0           # nose or tail to wall, walk past
BAY_TAILGATE_CLEARANCE = 3.5      # hatch or tailgate open
DOOR_WIDTH_OVER_VEHICLE = 3.0     # opening width over mirror width
DOOR_WIDTH_OVER_TRAILER = 4.0
DOOR_HEIGHT_OVER_VEHICLE = 1.0
EAVE_OVER_DOOR = 2.0              # wall height above an overhead door
OVERHEAD_TRACK_DEPTH = 1.5        # extra ceiling depth the parked leaf needs
OVERHEAD_CORNER_PIER = 2.0        # wall between a door jamb and a corner
OVERHEAD_BETWEEN_PIER = 2.0       # wall between two doors
SHOP_CEILING_LIFT = 12.0
SHOP_CEILING_RV = 14.0
SHOP_AISLE_MIN = 3.0
SHOP_AISLE_CART = 4.0
SHOP_AISLE_TWO_WAY = 6.0
BENCH_DEPTH = 2.5
BENCH_CLEAR_FRONT = 3.0
BENCH_BAND = 6.0                  # bench + working person
SHOP_SUPPORT_RATIO = 0.05         # support area / shop area
SHOP_SUPPORT_TRIGGER = 800.0      # sq ft above which support is expected
SHOP_BATH_TRIGGER = 600.0
PANEL_CLEAR_DEPTH = 3.0           # NEC 110.26
PANEL_CLEAR_WIDTH = 2.5
GARAGE_SLAB_SLOPE_MIN = 1.0 / 8.0 / 12.0   # ft per ft
GARAGE_EXHAUST_CFM_PER_SF = 0.75           # IMC 403 reference rate
STD_OVERHEAD_DOOR_WIDTHS_FT = (8, 9, 10, 12, 14, 16, 18, 20)   # extend existing
STD_OVERHEAD_DOOR_HEIGHTS_FT = (7, 8, 9, 10, 12, 14, 16)       # extend existing
FRAME_BAY_STD = (8, 9, 10, 12)
FRAME_SPAN_STD = (24, 30, 36, 40, 48, 50, 60, 70, 80)
EAVE_HEIGHT_STD = (8, 10, 12, 14, 16, 18, 20)
```

### 6.3 What the DSL cannot currently express

Rules above that need new vocabulary, ordered by value-per-unit-of-work:

1. **Per-room or per-wing ceiling height.** Today `ceiling` is plan-wide. A
   barndominium fundamentally has two ceiling heights. Nearly every rule in §2.5 and
   §4 is blocked or approximated by this. Highest-value change on the list.
2. **`panel` noun** (electrical panel with a wall + offset). Unlocks the NEC 110.26
   working-space check, which is a real, frequently violated, exactly-checkable rule.
3. **`slab` noun** (thickness, slope direction, drain). Unlocks R309.1, the
   drive-through drainage conflict, and the equipment-slab check.
4. **`use` / activity annotation on a shop room** (`storage`, `vehicle`, `wood`,
   `metal`, `welding`, `finish`, `commercial`). Unlocks zoning rules and the IBC
   note. Could be a room-level tag rather than a new statement.
5. **`vehicle` noun** (a declared thing to be parked, by class). Turns the whole of
   §2 from heuristics into direct fit checks: "this bay is declared for a Class A
   motorhome and its door is 10 ft tall." Highest-value for *user intent capture*.
6. **`lift`** as a fixture kind. Unlocks the clear-height and footprint checks.
7. **`eave`, `pitch`, `wainscot`** on `frame`. Unlocks §4.3's envelope rules.

Items 1 and 5 together would let the tool answer the question a barndominium owner
actually asks — "will my truck and my boat fit?" — which is a different and more
useful question than "is this plan code-compliant?"

### 6.4 Interaction with the existing score contract

The score module's stated contract is that penalties sum and clamp, and that
continuous terms give gradients. Adding shop terms must not silently penalise
pure-residential plans. Two safeguards:

- Every proposed shop term is **gated on the presence of shop/garage area** and
  returns 0 otherwise.
- The new terms' caps should be carved out of headroom, not stacked on top: the
  current continuous terms total 47 and the diagnostic caps total 60. Adding
  `vehicle_fit` (8), `shop_program` (6) and `separation` (10) pushes continuous
  terms to 71, which would make it hard for a shop-heavy plan to score above 30 even
  when clean. Recommend either scaling the shop terms down (4 / 3 / 5) or making
  them *share* a single `shop` bucket capped at ~12 points total.

---

## 7. Quick-reference tables

### 7.1 The twenty numbers that matter most

| # | Rule | Value |
| --- | --- | --- |
| 1 | Minimum usable single-vehicle bay | 12 × 22 |
| 2 | Recommended single-vehicle bay | 14 × 24 |
| 3 | Truck bay depth (crew cab, long bed) | 28 |
| 4 | Recommended two-car bay | 24 × 24 |
| 5 | Working shop minimum | 24 × 36 |
| 6 | RV bay | 16 × 48, 14 ft clear |
| 7 | Vehicle side clearance to wall | 3.0 |
| 8 | Vehicle end clearance to wall | 2.0 (3.5 with a tailgate) |
| 9 | Overhead door width over mirror width | +3.0 |
| 10 | Overhead door height over vehicle height | +1.0 |
| 11 | Wall/eave height over door height | +2.0 |
| 12 | Wall between door jamb and corner | ≥ 2.0, prefer 3.0 |
| 13 | Post-frame column spacing | 8 ft o.c. default |
| 14 | Common clear spans | 40 / 50 / 60 |
| 15 | Shop eave height | 12 default, 14 for a lift, 16 for RV |
| 16 | Garage slab slope | 1/8 in/ft to door or drain |
| 17 | Shop aisle minimum | 3.0 (4.0 with a cart) |
| 18 | Workbench band (bench + user) | 6.0 |
| 19 | Shop subpanel | 100 A minimum, 150–200 A if welding |
| 20 | Garage exhaust reference rate | 0.75 cfm/sq ft |

### 7.2 Overhead door selection matrix

| What goes through it | Door W × H | Min eave |
| --- | --- | --- |
| Sedan only | 8 × 7 | 9 |
| Car or SUV | 9 × 8 | 10 |
| Two cars, one opening | 16 × 8 | 10 |
| Pickup, crew cab | 10 × 9 | 11 |
| Pickup + utility trailer | 12 × 10 | 12 |
| High-roof van, small tractor | 10 × 10 | 12 |
| Boat with tower, cab tractor | 12 × 12 | 14 |
| Class C motorhome | 12 × 12 | 14 |
| Class A motorhome, fifth wheel | 14 × 14 | 16 |
| Semi, large ag equipment | 16 × 16 | 18 |

### 7.3 Bay depth by intent

| Intent | Depth |
| --- | --- |
| Park a car | 22 |
| Park a truck | 26 |
| Park a truck and keep a bench | 32 |
| Park a boat + trailer | 34 |
| Work on one vehicle with a lift | 30 |
| Class C motorhome | 38 |
| Class A motorhome | 48 |
| Fifth wheel + tow vehicle in line | 60 |
| Drive-through with a work zone between | 48–60 |

### 7.4 Support-space programme by shop size

| Shop area (sq ft) | Expect |
| --- | --- |
| < 400 | Nothing; it is a garage |
| 400–800 | A people-door, a bench wall, a broom closet |
| 800–1,200 | + half-bath, storage room, panel wall |
| 1,200–2,000 | + full bath with shower, mechanical room, compressor closet, loft |
| > 2,000 | + office, parts room, second overhead door, likely a separate service |

### 7.5 Clearance cheat sheet

| Between | Clear |
| --- | --- |
| Vehicle and wall (exit) | 2.5 |
| Vehicle and wall (work) | 4.0 |
| Two vehicles | 3.0 |
| Vehicle and bench in use | 4.0 |
| Person walking | 2.5 |
| Person with material | 3.0 |
| Cart / hand truck | 4.0 |
| Two-way with material | 6.0 |
| Panel front (NEC) | 3.0 deep × 2.5 wide × 6.5 high |
| Welding table, all sides | 4.0 |
| Table saw, rip direction | 10 in + 10 out |
| Miter saw, along the wall | 6–8 each side |
| Planer / jointer, each end | 6–8 |

### 7.6 Severity assignment summary

| Domain | Errors | Warnings | Infos |
| --- | --- | --- | --- |
| Vehicle & bay | — | bay depth < 20, bay narrower than its door, door headroom, door piers, RV bay with a car door | tight bays, non-standard door sizes, track depth, corners, orientation, apron |
| Workshop | — | aisle < 3 ft, blocked egress aisle, panel clearance, loud room on a bedroom wall, loft headroom | support programme, bath distance, zoning, dust, windowless, flow |
| Structure | — | span > 80, span ≠ envelope | bay > 12, bay < 8, module remainder, opening straddling a frame line, low eave, slab notes, wainscot, pitch |
| Separation | garage/shop door into a bedroom | habitable over a bay, bedroom party wall with a bay, missing CO alarm | undeclared rated wall, self-closing door reminder, door into a public room, no buffer, return air, ignition elevation, impact protection, ventilation, commercial use |

---

## 8. Relationship to the existing `barndsl` rule set

### 8.1 Already implemented — extend, do not duplicate

| Existing | What it does | Suggested extension |
| --- | --- | --- |
| `SHOP_DEPTH` | Shop's short dimension < 12 ft (warning) or < 20 ft (info), garages exempt | Add the door-relative depth check (§2.7 `BAY_DEPTH_SHORT`); keep the short-dimension check for the bench-band case |
| `SHOP_DOOR_HEIGHT` | Shop overhead door ≤ 8 ft (info) | Also fire when `ceiling` ≥ 12 and the door is short — the wasted-height case |
| `OVERHEAD_ROOM` | Overhead door on a non-garage/shop room | No change |
| `OVERHEAD_HEADER` | Overhead door wider than 10 ft | Consider suppressing at 12–16 ft when `frame` is declared (post-frame handles it) |
| `DOOR_SIZE` / `OVERHEAD_SIZE` | Nearest stock size nudge | Extend the width/height tuples (§6.2) |
| `GARAGE_SEPARATION` | Reminder to declare a `rated` wall | Add the habitable-above and bedroom-party-wall variants |
| `GARAGE_PASSTHROUGH` + `topology` score term | Bedrooms routed through a garage | No change; add the orthogonal `separation` term |
| `ALARM_CO` | CO alarm reminder with an attached garage | Consider raising to warning (IRC R315 is absolute) |
| `BAY_WIDE` | `frame bay` > 12 ft | Add `FRAME_BAY_NARROW`, `FRAME_MODULE`, `FRAME_OPENING_BAY` |
| `APPROACH_GARAGE` | Garage faces away from the street | Add the "faces the living wing's glazing" variant |
| `WET_GROUP` / `wall … plumbing` | Wet rooms sharing a plumbing wall | Extend to shop baths and utility sinks |
| `DRIVE_DOOR` / `walk` | Door reachable from the drive | Add the vehicle-side equivalent: apron in front of an overhead door |

### 8.2 Genuinely new territory

Nothing in the current rule set addresses: overhead-door headroom and track depth,
piers between doors, door alignment to frame lines, apron and maneuvering depth,
vehicle-class fit, workshop aisles and bench bands, machine envelopes, support-space
programme ratios, electrical working space, slab slope and drainage, or shop-specific
ventilation. Those are the gaps this document is meant to fill.

### 8.3 Blocking modelling gap

The single plan-wide `ceiling` is the constraint that most limits shop rules. A
barndominium with a 10 ft living ceiling and a 14 ft shop eave cannot be expressed,
which means every clear-height rule either fires falsely on the living wing or has to
be written so loosely it never fires. Per-room or per-zone ceiling (or an `eave`
declaration on `frame` plus a per-room override) should be treated as a prerequisite
for the §2.5 and §4 rule families.

---

## 9. Sources

- Neufert, E., *Architects' Data* (Wiley) — workshop, industrial and vehicle
  dimensional planning data.
- De Chiara, J. & Callender, J., *Time-Saver Standards for Building Types* — garage,
  light-industrial and agricultural building programmes.
- AIA / Ramsey & Sleeper, *Architectural Graphic Standards* — vehicle envelopes,
  turning geometry, parking and driveway layout, door and slab detailing.
- National Frame Building Association, *Post-Frame Building Design Manual* and
  accepted-practice bulletins: <https://nfba.org/aws/NFBA/pt/sp/tech-resources>
- USDA NRCS, *Prescriptive Design Specifications for Non-Diaphragm Post-Frame
  Buildings* (span and eave-height design envelope):
  <https://www.nrcs.usda.gov/sites/default/files/2022-11/PA-12%20Design%20Guide%2012-%20Prescriptive%20Design%20Specifications%20for%20Non-Diaphragm%20Post-Frame%20Buildings.pdf>
- ICC, *International Residential Code* — R302.5/R302.6 dwelling-garage separation,
  R309 garage floor and separation, R315 CO alarms, M1307.3 ignition-source
  elevation, M1602.2 return air. Section text via
  <https://up.codes/s/dwelling-garage-fire-separation> and
  <https://codes.iccsafe.org/s/IRC2021P2/part-v-mechanical/IRC2021P2-Pt05-Ch13-SecM1307.3>
- ICC, *International Building Code* §508 mixed occupancies:
  <https://codes.iccsafe.org/s/IBC2024P1/chapter-5-general-building-heights-and-areas/IBC2024P1-Ch05-Sec508.4>
- ICC, *International Mechanical Code* §403 — garage exhaust rates:
  <https://www.iccsafe.org/wp-content/uploads/2018SC-PMG404.1.pdf>
- OSHA 29 CFR 1910.252 — welding, cutting and brazing general requirements:
  <https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.252>
- Overhead door and RV-bay clear-height practice:
  <https://www.probuiltsteel.com/blog/garage-door-sizes-for-class-a-b-c-motorhomes/>,
  <https://gensteel.com/resources/infographics/what-size-rv-garage-do-you-need/>
- Garage sizing practice: <https://alansfactoryoutlet.com/blog/standard-garage-size/>,
  <https://nationalgarageauthority.com/garage-size-standards>
- Vehicle dimensions: <https://www.dimensions.com/element/ford-f250-crew-cab-long-bed-p708-5th-gen>
- Garage slab slope practice:
  <https://engineerfix.com/what-is-the-code-for-residential-garage-floor-slope/>
- Shop electrical planning: <https://workshopcalc.com/guides/workshop-electrical-guide>
- Machine working envelopes: <https://www.finewoodworking.com/2000/01/01/shop-layout>,
  <https://workshopcalc.com/guides/metalworking-shop-setup-guide>
- Woodworking/refinishing shop loss-control practice:
  <https://www.grinnellmutual.com/upload/assets/documents/losscontrolbulletins/LCB30-Woodworking-refinishing-shops.pdf>
