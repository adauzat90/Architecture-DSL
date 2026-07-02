# Cypress Retreat — Design Packet

**A 90 ′ × 60 ′ barndominium for Montgomery County, Texas**
3 bed / 2.5 bath residence + attached 3-bay activity gym · 5,400 sq ft under roof

> Generated with the **barndsl** architecture DSL. The plan compiles **0 errors**;
> every remaining diagnostic is an intentional design decision or an advisory code
> note (documented in §8). This packet is a schematic-design study — final
> geometry, structure, foundation, and materials must be confirmed by a licensed
> Texas engineer/architect and the local Authority Having Jurisdiction (AHJ).

---

## 1. The brief

> *"A 3 bedroom, 2½ bath, 60 × 90 barndominium featuring a large living room and
> kitchen with a central island. Tons of storage and a safe room. Attached gym with
> 3 sections — turf on one side, a rubber-matted tumble area on another, and a boat
> garage on the outer edge. These areas need safety precautions while maintaining
> views between spaces so parents can be in a conditioned space. Include climate data
> for Montgomery County, Texas, and, based on that data, determine the best materials."*

## 2. How the design answers it

| Requirement | Where it lives in the plan |
|---|---|
| 3 bedrooms | Primary suite (`Mbed`, 400 sf) + `Bed2` (144 sf) + `Bed3` (120 sf) |
| 2½ baths | `Mbath` (ensuite) + `Bath2` (hall) full baths, `Half` powder bath = **2 full + 1 half** |
| Large living room | `Living` great room, **560 sf**, vaulted, open to kitchen & lounge |
| Kitchen w/ central island | `Kitchen`, 16 × 16 (256 sf) — square footprint sized for a large central island + seating |
| Tons of storage | Walk-in `Mcloset` (160 sf), two 72 sf bedroom walk-ins, `Pantry`, `Mud`/drop-zone, 120 sf bulk `Store` |
| Safe room | `Safe`, 120 sf, dead-center, **windowless**, hardened (see §5) |
| Gym — turf | `Turf` bay, 18 × 30 (540 sf), artificial turf |
| Gym — tumble | `Tumble` bay, 18 × 30 (540 sf), rubber matting |
| Gym — boat garage (outer edge) | `Boat`, 18 × 60 (1,080 sf) on the far east edge, 12 ′ overhead door |
| Views + parents in conditioned space | Conditioned supervision `Lounge` + tempered vision panels chaining all 3 bays (see §5) |
| Safety precautions | Fire-rated garage separation, self-closing rated doors, CO/smoke alarms, tempered glazing, single controlled house↔gym threshold (see §5) |
| Montgomery County climate data | §6 |
| Climate-driven materials | §7 |

## 3. Program & area summary

| Metric | Value |
|---|---|
| Footprint (under roof) | **5,400 sq ft** (90 ′ × 60 ′) |
| Conditioned/interior area | 4,320 sq ft |
| Habitable area | 1,680 sq ft |
| Bedrooms / baths | 3 / 2.5 |
| Ceiling | 11 ′ typical · great room vaulted · gym bays 14 ′ |
| Exterior wall area | 3,300 sq ft |
| Roof area (approx.) | 5,692 sq ft |
| Foundation concrete (approx.) | 77.8 cu yd |
| Structural frame | 6 bents / 18 posts / 450 ft of beam (18 ′ bays, 60 ′ clear span) |

Full room, door, and window schedules: [`schedules.md`](schedules.md).

## 4. Plan organization

```
        WEST  ── conditioned residence (x 0–54) ──┤├── attached gym (x 54–90) ──  EAST
  N  ┌───────────────────────────────────────────────┬──────────────────────────┐
     │ Mcloset  Mbath │ Bed2  C2  C3  Bed3            │        TUMBLE            │
     │  (primary)     ├───────── Hall3 (gallery) ─────┤   (rubber mat, 540 sf)   │
     │  Mbed  │ Bath2      │      Laundry              │                          │
     │        │ Safe  Half │      Store               ├──────────────────────────┤
     │        ├──────────── Hall (spine) ─────────────┤                          │
     │ Living   │ Pantry Mud │           Lounge        │   TURF        │  BOAT    │
     │ (great)  │  Kitchen   │      (supervision)      │ (540 sf)      │ GARAGE   │
  S  └───────────────────────────────────────────────┴───────────────┴──────────┘
        front entry (S)          side/mud entry (W)      turf entry (S)   boat OHD (E)
```

- **Open conditioned core** (south): great room ⇄ kitchen ⇄ supervision lounge, one
  continuous space, with a walk-in pantry and mud drop-zone tucked behind the kitchen.
- **Private wing** (north/west): the primary suite occupies the full-depth west bay
  (bed + ensuite + walk-in); a hall spine plus a gallery serve two secondary bedrooms
  whose walk-in closets stack **between** them to buffer sound.
- **Service core** (center): the windowless safe room, powder bath, laundry, and bulk
  storage cluster on interior walls — no exterior glazing to protect.
- **Gym wing** (east): three bays in a line — turf and tumble against the house so they
  can be supervised, the boat garage on the outer (east) edge with its own overhead door.

Drawings: [`floorplan.svg`](floorplan.svg) (architectural) · [`floorplan.png`](floorplan.png)
· [`floorplan_structural.svg`](floorplan_structural.svg) (with the post-frame grid).

## 5. Supervision & safety systems

**Sightlines (the "maintain views" requirement).** Parents stay in the **conditioned
supervision Lounge** — part of the open living core, air-conditioned, with seating —
and watch the kids play:

- **Lounge → Turf**: a large tempered vision panel + a single glazed door.
- **Turf → Tumble** and **Turf/Tumble → Boat bay**: tempered vision panels create a
  continuous supervision sightline, so a parent in the conditioned lounge can see
  across all three activity bays without leaving the a/c.
- All four interior panels are **tempered safety glazing** (they intentionally trip the
  DSL's `WINDOW_INTERIOR` note — that is the brief being satisfied, not a defect).

**Controlled threshold.** There is exactly **one** house↔gym door (Lounge→Turf), a
**self-closing** unit, so the conditioned envelope isn't compromised and children pass
through a single supervised point. Each gym bay also has its own exterior door for
direct outdoor access/egress.

**Fire & life-safety (code-driven, flagged by the compiler):**
- Boat garage ↔ gym common walls detailed as a **gypsum fire separation** (IRC R302.6;
  ⅝ ″ Type X where under/adjacent to habitable space).
- Garage-to-room doors are **self-closing, 20-minute fire-rated / solid-core** (IRC R302.5.1).
- **Interconnected smoke + CO alarms** in every bedroom, outside each sleeping area, and
  on every level (IRC R314/R315) — required because of the attached garage.
- Powder and hall baths are interior → **mechanical exhaust** vented outside (IRC R303.3).

**Activity-area safety detailing (recommended):**
impact-absorbing wall padding in the tumble bay, poured/rolled shock-attenuating base
under the mats, slip-resistant sealed floors, no direct gym-to-bedroom openings, GFCI
protection, and separate HVAC/dehumidification zoning for the gym (see §7).

**Storm safe room.** The central `Safe` room (120 sf, no exterior walls) should be built
to **FEMA P-320 / ICC 500** as a residential storm shelter: reinforced cast-in-place
concrete or fully-grouted CMU walls, a concrete or steel-plate lid, and an impact-rated
door, designed for the hurricane/tornado wind event. It doubles as secure valuables and
storm-supply storage.

## 6. Climate data — Montgomery County, Texas

Montgomery County sits ~40–50 mi north of Galveston Bay (Conroe / The Woodlands / Lake
Conroe). It is **hot-humid** with mild winters, heavy Gulf rainfall, expansive clay
soils, and a very high termite load — the material palette in §7 is driven directly by
these. *(Values are typical design references; verify with a site-specific geotech report
and current ASCE 7 / IECC data for the parcel and its FEMA flood zone.)*

| Parameter | Design value (typical) | Design implication |
|---|---|---|
| IECC / ASHRAE climate zone | **2A — hot-humid** | Cooling- and humidity-dominated; air-tightness + latent control govern |
| Köppen | Cfa (humid subtropical) | Long cooling season, mild winter |
| Summer cooling design (1%) | ≈ **96–98 °F** DB / ~77 °F MCWB | High sensible **and** latent cooling loads |
| Winter heating design (99%) | ≈ **28–30 °F** | Light heating; occasional hard freezes (e.g., Feb 2021) |
| Degree days | ≈ 2,900 CDD65 · ≈ 1,400 HDD65 | Cooling ≫ heating |
| Mean annual rainfall | ≈ **50 in/yr**, high-intensity Gulf events | Aggressive roof/wall water management; positive site drainage |
| Relative humidity | High (frequently 70–90 % AM) | Condensation control on steel; dehumidification |
| Ultimate design wind (ASCE 7-16, RC II) | ≈ **115 mph** V_ult (≈ 89 mph nominal), Exposure B/C | Engineered uplift/lateral resistance; generally **outside** the wind-borne-debris region, but hurricane remnants occur (Ike '08, Harvey '17) |
| Seismic | **SDC A** (very low) | Not a governing load |
| Frost depth | Negligible (≈ 0–6 in) | No deep frost footings required |
| Predominant summer wind | South / southeast (Gulf breeze) | Orient glazing & porches for shade + cross-ventilation |
| Soils | **Expansive clay** ("gumbo"), high plasticity/shrink-swell | **Engineered foundation is the #1 local risk** |
| Termites | **Very high** (subterranean + Formosan) | Non-cellulose structure + termite barriers |
| Flood | Localized — creeks, San Jacinto, Lake Conroe | Confirm FEMA zone; set finished-floor elevation accordingly |

## 7. Climate-driven material & assembly selection

Each recommendation traces back to a climate driver above.

| System | Recommendation | Why (climate driver) |
|---|---|---|
| **Foundation** | Engineered **stiffened post-tensioned slab-on-grade** (PTI DC 10.5) *or* pier-and-beam with void forms, per geotech; 15-mil under-slab vapor retarder; uniform perimeter moisture (root barriers, drip-free gutters) | **Expansive clay** shrink-swell + heavy rain — the dominant local failure mode |
| **Primary structure** | **Galvanized steel post-frame ("red-iron")** shell — long clear spans for the gym, pairs with the 18 ′ bays; engineered for ~115 mph V_ult uplift/lateral | High wind, big spans, **termite-proof**, humidity → galvanized |
| **Roof** | **Standing-seam metal**, 26-ga Galvalume, light/high-SRI **cool-roof** color, concealed clips, ~4:12 pitch, continuous ridge vent + uplift straps, ice-&-water at penetrations | Cuts cooling load (hot sun), sheds 50 ″ rain, wind-uplift resistant, no exposed fasteners to leak |
| **Above-deck** | **Unvented conditioned attic**: closed-cell spray foam to the underside of the roof deck; radiant barrier | Keeps ducts in conditioned space; **stops condensation on steel** in a humid a/c climate |
| **Walls** | Metal siding **or** fiber-cement (James Hardie) over a **drainable rainscreen + WRB**; closed-cell spray foam in the cavity for air-seal + condensation control | Humidity, wind-driven rain, rot/termite resistance; ccSPF controls the steel dew point |
| **Insulation (Zone 2A targets)** | Roof/attic **R-38** (or R-30 ccSPF at deck); walls **R-13 + continuous**; slab perimeter insulation not required in 2A; **prioritize air-tightness (blower-door test)** | Cooling- and latent-load-dominated climate rewards air-sealing over R-value |
| **Windows / glazing** | Double-pane **Low-E, SHGC ≤ 0.25, U ≤ 0.30–0.40** (ENERGY STAR South-Central); minimize west glass; deep overhangs/porch shading; **tempered** at gym panels & near floors/doors | Blocks solar heat gain; shading beats glazing area in a hot climate; safety glazing |
| **HVAC** | Right-sized **variable-speed heat pump (SEER2 ≥ 15.2)** + **dedicated/whole-house dehumidification**; ducts in conditioned attic; **separate zone/mini-split for the gym** (turf/tumble semi-conditioned), boat garage vented-only | Latent load control is the hot-humid priority; independent gym zone keeps the residence comfortable |
| **Termite protection** | Steel framing is inherently termite-resistant; add slab-penetration termite barriers, borate-treat any wood, bait system, 6 ″ siding-to-grade clearance | Very high SE-Texas termite pressure |
| **Interior/wet areas** | Mold-resistant gypsum in baths/laundry, sealed/epoxy or polished concrete in gym & boat bay (rubber mats over), corrosion-resistant (stainless/hot-dip) fasteners | Humidity, spills, wet activity floors |
| **Storm hardening** | FEMA P-320 safe room (§5); impact-rated exterior doors; shutter provisions on windward openings | Hurricane remnants / tornado risk |

## 8. Compliance snapshot (barndsl diagnostics)

```
COMPILE OK — 0 error(s), 4 warning(s), 7 info(s)
Program: 3 bed / 3 bath · 4320 sq ft interior · 1680 sq ft habitable · footprint 5400 sq ft
```

All rooms are reachable, every bedroom has code egress + a closet, openings fit their
walls, and the program matches. The remaining items are **intentional** and documented:

| Code | Count | Status |
|---|---|---|
| `WINDOW_INTERIOR` | 4 | **By design** — the tempered parent-supervision vision panels (§5) |
| `GARAGE_SEPARATION`, `GARAGE_DOOR` | 3 | **By design** — fire separation + rated doors already specified (§5, §7) |
| `ALARM_CO` | 1 | **Reminder** — interconnected smoke/CO alarms on the electrical plan (§5) |
| `BATH_VENT` | 2 | **Reminder** — exhaust fans for the two interior baths (§5) |
| `BAY_WIDE` | 1 | **Accepted** — 18 ′ post-frame bays are normal for a metal barndominium; engineer sizes members |

## 9. Deliverables in this packet

| File | Contents |
|---|---|
| `montgomery_barndo.barn` | The editable DSL source of record |
| `floorplan.svg` / `floorplan.png` | Architectural floor plan |
| `floorplan_structural.svg` | Floor plan with the post-and-beam frame overlay |
| `schedules.md` | Room, door, and window schedules |
| `montgomery_barndo.dxf` | CAD interchange (23 rooms, 20 openings) |
| `montgomery_barndo.revit.json` | Revit exchange for the pyRevit add-in |
| `DESIGN_PACKET.md` | This document |

## 10. Suggested next steps

1. Site & **FEMA flood-zone** check; set finished-floor elevation.
2. **Geotechnical report** → final foundation design for the expansive clay.
3. Structural engineering of the steel frame for local wind (ASCE 7).
4. MEP design: HVAC load calc (Manual J/S/D) with latent sizing + gym zoning.
5. Confirm the safe room to **FEMA P-320 / ICC 500** with the AHJ.
6. Add porches/overhangs on the south & west elevations for shading (recommended).

*Schematic study produced with barndsl. Checks are approximate and loosely IRC-based —
not a substitute for a licensed designer, engineer, or the AHJ.*
