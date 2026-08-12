# Allen & Iano, *The Architect's Studio Companion* — research notes for barndsl rule authoring

**Source book:** Edward Allen and Joseph Iano, *The Architect's Studio Companion: Rules of Thumb for
Preliminary Design* (Wiley). Notes below are keyed to the **7th edition** (2022, ISBN 9781119826798),
with edition differences called out where they matter. Earlier editions in circulation: 6th (2017,
ISBN 9781119092414), 5th (2011), 4th (2006).

**Audience:** a developer or agent translating preliminary-design rules of thumb into machine-checkable
`barndsl` diagnostics (`ERROR` / `WARNING` / `INFO`) and `score.py` deductions.

**Copyright note:** everything here is paraphrase and independently-sourced numeric fact. No tables,
charts, or prose from the book are reproduced. Span ranges and depth ratios are *engineering commonplaces*
that appear in many references (AISC, SJI, APA, AWC, NCMA, PCI design guides, Ching's *Building
Construction Illustrated*, *Architectural Graphic Standards*); they are stated here from those general
sources, not transcribed from Allen & Iano's charts. Code-derived numbers (IRC/IBC/ADA/NEC) are public
regulatory facts.

**Status of the numbers:** treat every figure as a *preliminary-design* value — good enough to size a
plan, never a substitute for an engineer's calculation or the authority having jurisdiction. That
caveat matters for barndsl too: the repo's own `frame` directive already carries a "layout aid, not an
engineered design" disclaimer (`docs/AUTHORING.md`), and every rule proposed below inherits it.

---

## Table of contents

1. [Why this book is good source material for a validation engine](#1-why-this-book-is-good-source-material-for-a-validation-engine)
2. [How the book is organized (and how that maps to barndsl)](#2-how-the-book-is-organized-and-how-that-maps-to-barndsl)
3. [Designing with building codes](#3-designing-with-building-codes)
4. [Designing the structure — system selection](#4-designing-the-structure--system-selection)
5. [Designing the structure — preliminary member sizing](#5-designing-the-structure--preliminary-member-sizing)
6. [Designing for egress](#6-designing-for-egress)
7. [Designing for accessibility](#7-designing-for-accessibility)
8. [Designing spaces for mechanical and electrical services](#8-designing-spaces-for-mechanical-and-electrical-services)
9. [Vertical transportation and stairs as circulation](#9-vertical-transportation-and-stairs-as-circulation)
10. [Designing with daylight](#10-designing-with-daylight)
11. [Acoustics](#11-acoustics-adjacent-material-not-a-studio-companion-section)
12. [Designing for parking and site layout](#12-designing-for-parking-and-site-layout)
13. [Height and area limitations](#13-height-and-area-limitations)
14. [What matters most for barndominiums](#14-what-matters-most-for-barndominiums)
15. [Quick-reference tables](#15-quick-reference-tables)
16. [Proposed diagnostic codes, consolidated](#16-proposed-diagnostic-codes-consolidated)
17. [Open questions and cautions](#17-open-questions-and-cautions)

---

## 1. Why this book is good source material for a validation engine

The *Studio Companion* exists to answer a specific class of question: *before* anything is engineered,
how big does this thing need to be, and will the scheme survive contact with the code? That is
structurally identical to what barndsl's compiler does. Four properties make it unusually easy to
mechanize:

**It is written as bounded numeric ranges, not narrative.** The book's recurring move is "system X is
economical from *a* to *b* feet, and its depth is roughly span ÷ *n*." A range with two endpoints and a
ratio is directly expressible as a validation predicate. Compare this to most architectural writing,
which offers principles that cannot be evaluated against a rectangle with a room type.

**It separates *feasible* from *economical*.** Almost every span chart distinguishes the outer limit of
what a system can do from the band where it is cost-effective. That distinction is exactly the
`ERROR` / `WARNING` split barndsl already uses: outside the feasible range is unbuildable (error);
inside feasible but outside economical is a nudge (warning or info). This mapping is the single most
useful idea to lift from the book.

**Its rules are dimensional, and barndsl's model is dimensional.** The plan model is rectangles in feet
with a room type, a level, doors, windows, and an optional post-and-beam frame (`src/barndsl/elements.py`).
Nearly every rule of thumb in the book takes the form *dimension → dimension* or *area → area*, so it can
be evaluated on that model without inventing new data. Rules that need something barndsl does not model
(soil bearing, thermal loads, structural member sizes) are flagged below as "needs a new field."

**It is preliminary by design, which matches the tolerance of a scoring engine.** barndsl is not a
permit set. It is a design-loop instrument: the agent hill-climbs a 0–100 score and the compiler tells it
what is wrong. Rules of thumb are the correct precision for that job — precise enough to rank two schemes,
loose enough that a false positive is a nudge rather than a wrong answer. The book's own framing
("preliminary") is the license to be approximately right.

**Where the book is a poor fit — read this before mining it.** The *Studio Companion* is aimed at
commercial and institutional buildings under the IBC. A barndominium is usually a one- or two-family
dwelling under the **IRC**, with a large accessory garage/shop. Three consequences:

- The egress chapter's machinery (occupant load factors, common-path limits, exit counts, corridor
  ratings) mostly *does not apply* to an IRC dwelling, which needs exactly one compliant exterior door
  plus emergency escape openings from sleeping rooms. Importing IBC egress wholesale would generate a
  storm of false positives. Use it for the **shop side** and for the odd barndominium that exceeds IRC
  scope, not for the living wing.
- The structural chapters are the *most* transferable material, because a barndominium's defining
  feature — a 40–60+ ft clear span over the shop — is a long-span problem the residential codes'
  prescriptive tables do not cover at all.
- The accessibility chapter is written to ADA/IBC Chapter 11, which does not legally bind a private
  single-family home. barndsl already treats accessibility as `INFO`-level coaching (`ACCESS_*` codes are
  all info). Keep it that way.

---

## 2. How the book is organized (and how that maps to barndsl)

### 7th edition section list (verified)

| # | Section | barndsl diagnostic category |
| --- | --- | --- |
| 1 | Designing with Building Codes | `program`, `structure`, new: `code` |
| 2 | Designing the Structure | `structure` |
| 3 | Designing with Daylight | `access_egress` (light), `quality` |
| 4 | Designing Spaces for Mechanical and Electrical Services | `fixtures`, `electrical`, new: `services` |
| 5 | Designing for Egress and Accessibility | `access_egress` |
| 6 | Designing for Parking | `site` |
| 7 | Designing with Height and Area Limitations | new: `code` |
| A | Appendix A: Example Use of This Book | — (a worked example; useful as a model for docs) |
| B | Appendix B: Units of Conversion | — |

### Edition differences worth knowing

- The **7th edition** consolidated what earlier editions split. Selecting a structural system and sizing
  it are now one section ("Designing the Structure"); egress and accessibility are merged; and height/area
  limitations were pulled out to the back as their own section.
- Earlier editions (**5th/6th**) presented structural *selection* (which system, what span) and structural
  *sizing* (how deep, how big a column) as separate front-of-book material, with egress and accessibility
  as distinct chapters. If you are working from a 6th edition copy, expect the same content under
  different headings rather than different numbers.
- The 7th edition adds **tall mass timber** (Types IV-A/B/C, introduced in the 2021 IBC) and refreshes
  heating/cooling system coverage. Mass timber is irrelevant to barndominiums; the code updates are not.
- **Practical consequence for barndsl:** cite the *content* ("preliminary-design span guidance"), not a
  page or table number, in diagnostic hints. Page references go stale across editions and the repo's
  diagnostic strings are a stable API (`docs/MODEL_INVARIANTS.md`).

### Two things the book does *not* cover

The task brief asks for acoustics and vertical transportation. Be aware:

- **Acoustics is not a *Studio Companion* section.** The book covers structure, codes, daylight, MEP
  space, egress/accessibility, parking, and height/area. Sound isolation is treated in Allen & Iano's
  *other* book, *The Architecture of Building Systems* / *Fundamentals of Building Construction*, and
  properly in Egan's *Architectural Acoustics*. Section 11 below is drawn from the IBC and general
  practice and is labelled as adjacent material.
- **Vertical transportation is thin.** Elevator and escalator space planning appears, if at all, inside
  the mechanical/electrical services material rather than as its own section. For a one- or two-story
  barndominium it is nearly moot. Section 9 covers stairs (which *are* well covered, under egress) and
  treats residential elevators as an optional accessibility feature.

---

## 3. Designing with building codes

### What the book does

Section 1 walks the reader through the front-end code determination that governs everything downstream:
classify the **occupancy**, choose a **construction type**, then read the allowable **height and area**
that combination permits — and iterate, because the three are coupled. The book's contribution is
turning IBC Chapters 3, 5, and 6 into a decision sequence a designer can run in an afternoon.

### Occupancy classification, applied to a barndominium

| Space | IBC group | Notes |
| --- | --- | --- |
| Living quarters | **R-3** | One- and two-family dwellings |
| Attached private garage | **U** | Private garages and carports; IBC 406.3 limits a *private* garage to 1,000 sq ft before it becomes S-2 |
| Shop / workshop, non-commercial | **U** (or **S-1**) | If it stores or services vehicles it drifts toward S-1 (moderate hazard: repair garages, woodworking) |
| Large storage / equipment bay | **S-1** or **S-2** | S-2 is low hazard (parking only); S-1 covers repair work |
| Commercial shop operation | **F-1**, **S-1**, or **B** | Any business use changes the analysis entirely |

**The threshold that decides which code book applies.** The IRC governs detached one- and two-family
dwellings and townhouses **not more than three stories** above grade plane, with their accessory
structures. A barndominium normally sits inside IRC scope, and then:

- Living/garage separation is **IRC R302.6**, not IBC 508.
- Egress is **IRC R310/R311**, not IBC Chapter 10.
- No occupant-load calculation, no exit-count analysis, no corridor rating.

A barndominium leaves IRC scope when it exceeds three stories, contains more than two dwelling units,
or the shop becomes a commercial use. Then IBC Chapters 5/6/10 apply in full and the numbers in this
document's egress and height/area sections become live.

**The 1,000 sq ft trap.** A shop bay larger than 1,000 sq ft is not a "private garage" under IBC 406.3.
For a 40 × 60 barndominium where the shop is 40 × 40 = 1,600 sq ft, this is routine — and it is exactly
the kind of thing a plan reviewer catches late. Under the IRC the private-garage size limit does not
apply the same way, but the classification question is worth surfacing to the author.

### Construction types (IBC Chapter 6, Table 601)

Five types, each split A (protected) / B (unprotected):

| Type | Materials | Table 601 ratings |
| --- | --- | --- |
| I-A / I-B | Noncombustible | 3 hr / 2 hr primary structural frame |
| II-A / II-B | Noncombustible | 1 hr / **0 hr** |
| III-A / III-B | Noncombustible exterior walls, any interior | 1 hr / 0 hr (2 hr exterior bearing walls) |
| IV-A/B/C/HT | Mass timber / heavy timber | 2–3 hr (IV-A/B/C, new in 2021 IBC); HT by member size |
| V-A / V-B | Any code-permitted material | 1 hr / **0 hr** |

A typical barndominium is **Type V-B** (wood post-frame, unprotected) or **Type II-B** (steel frame,
unprotected). Both carry zero required fire-resistance ratings on structural elements — which is precisely
why the dwelling/garage separation in R302.6 is doing all the life-safety work in these buildings.

### Fire separation, the number that matters most

**IRC R302.6 — dwelling/garage separation:**

| Condition | Minimum protection |
| --- | --- |
| Garage → residence (walls and ceilings) | ≥ 1/2 in gypsum board on the **garage side** |
| Garage → habitable rooms **above** | ≥ 5/8 in **Type X** gypsum board on the garage ceiling |
| Structure supporting a required floor/ceiling separation | ≥ 1/2 in gypsum board on those supporting walls/columns |
| Garage → attic | ≥ 1/2 in gypsum board |

**IRC R302.5.1 — the door between garage and dwelling:**

- Solid wood, **≥ 1-3/8 in** thick; **or** solid/honeycomb steel ≥ 1-3/8 in; **or** a 20-minute fire door.
- Equipped with a **self-closing** (and, in recent editions, self-latching) device.
- **No door directly into a sleeping room.** This is an absolute prohibition and the single most
  important adjacency rule in a barndominium.

**IRC R302.5.2 / R302.5.3** — ducts penetrating the separation must be ≥ 26 gauge steel with no openings
into the garage; other penetrations must be sealed.

`barndsl` already models the first and third of these (`GARAGE_SEPARATION`, `GARAGE_BEDROOM`,
`DRIVE_DOOR`). The gaps are the door *specification* (self-closing, thickness/rating), habitable-above-garage
ceiling protection, and duct penetration.

### Rule candidates — building codes

Codes below follow the repo's existing naming (short, uppercase, underscore-separated) and would need
registry entries in `src/barndsl/diagnostics.py`. Proposed category: **`code`** (new) or fold into
`structure` / `access_egress`.

| Code | Sev | Predicate | Message intent |
| --- | --- | --- | --- |
| `GARAGE_DOOR_SPEC` | **WARNING** | An interior door with a `GARAGE_TYPES` room on one side lacks a `fire_rated` / `self_closing` attribute | R302.5.1 wants a solid-core or 20-min door with a self-closer |
| `GARAGE_BEDROOM_DOOR` | **ERROR** | Interior door directly connects a `GARAGE`/`SHOP` to a `BEDROOM` | R302.5.1 flatly prohibits it (barndsl's `GARAGE_BEDROOM` may already cover adjacency; this is the *door* case) |
| `GARAGE_CEILING_SEP` | **WARNING** | Any level-1 room whose footprint overlaps a level-0 `GARAGE`/`SHOP` | 5/8 in Type X on the garage ceiling; also implies a rated supporting structure |
| `GARAGE_DUCT_PENETRATION` | **INFO** | A `MECHANICAL` room shares a wall with a garage/shop and serves the dwelling | R302.5.2: 26-ga steel duct, no garage openings |
| `IRC_SCOPE_EXCEEDED` | **INFO** | `max(level) >= 3`, or the plan declares more than two dwelling units | Plan is outside IRC scope; IBC Chapters 5/6/10 now govern and most of barndsl's residential checks are the wrong rule set |
| `PRIVATE_GARAGE_AREA` | **INFO** | Total `GARAGE` + `SHOP` area > 1,000 sq ft | Exceeds IBC 406.3's private-garage limit; if the AHJ applies the IBC this reclassifies to S-1/S-2 |
| `SHOP_COMMERCIAL_HINT` | **INFO** | Shop area > 2,000 sq ft, **or** shop area > 50% of the total footprint | At this scale a reviewer will ask whether the use is commercial; classification changes egress, separation, and sprinkler requirements |

**Scoring:** the fire-separation family should be diagnostic-driven only. `GARAGE_BEDROOM_DOOR` as an
`ERROR` already zeroes the score under the existing contract (`score.py` deducts a flat 100 for any
error), which is the right severity for a life-safety prohibition. Do **not** add a continuous term
here — fire separation is binary, and the existing `topology` term already penalizes garage-entangled
circulation on a gradient.

**Profile candidates.** Per `docs/AUTHORING.md`, roughly 14 thresholds are jurisdiction-amendable.
`PRIVATE_GARAGE_AREA`'s 1,000 sq ft and `SHOP_COMMERCIAL_HINT`'s 2,000 sq ft are good profile keys
(`max_private_garage_area`, `commercial_shop_area`) since local amendments vary widely on accessory-use size.

---

## 4. Designing the structure — system selection

### What the book does

The book's central structural device is a set of span-range charts: for each horizontal spanning system
(and each vertical system), a bar showing the range of spans over which the system is *usable*, with the
*economical* band highlighted. You pick your required span, read down the chart, and get a short list of
candidate systems. A second layer gives typical bay proportions and the depth each system needs.

This is the most directly mechanizable content in the book, and the most relevant to barndominiums,
because the shop bay is a genuine long-span problem.

### Horizontal spanning systems — span ranges and depth ratios

Numbers below were cross-checked against the 5th edition's own text, Ruddy & Ioannides' "Rules of Thumb
for Steel Design" (ASCE Structures Congress 2004 — the canonical source for steel L/d ratios), the SJI
Standard Specification for K/LH/DLH-series joists, and standard concrete sizing guidance. Where the book
publishes a chart rather than a ratio, that is noted.

**Wood:**

| System | Span range (ft) | Depth ÷ span | Notes for barndsl |
| --- | --- | --- | --- |
| Wood joists, solid sawn | ~6–24 | ≈ L/19–L/24 *(back-calculated from the chart, not a stated rule)* | 12/16/24 in o.c. Add 2–3 in to joist depth for the finished floor |
| Wood I-joists | ~12–33 | not published as a ratio | Depths 9-1/4, 11-7/8, 14, 16 in. Deepest 16 in proprietary joists reach ~33 ft at 16 in o.c. |
| Parallel-chord wood floor trusses | 0–40 | ~L/16 at the deep end | Depths 12–30 in in 2 in steps; **spacing 16–48 in o.c.** Open webs pass ducts — a real win for barndominium lofts |
| Pitched light wood roof trusses | **15–60** | pitch-governed | Whole-number pitches **2:12 to 7:12** commonly stocked. **This is the post-frame default** |
| Heavy wood trusses | 0–240 | ~L/8 | Spacing 4–8 ft needs no purlins; ~20 ft max with purlins |
| **Glulam beams** | **20–80** simple; 25–65 continuous | **L/20** | Depths in 1-1/2 in multiples; widths 3-1/8, 5-1/8, 6-3/4, 8-3/4 in; **width ≈ depth ÷ 3 to depth ÷ 7**. Girder ≥ 1-1/2 in deeper than the beams it carries |
| Glulam arches | 20–80 | — | Low/medium pitch 3:12–8:12; high pitch 10:12–16:12 |
| Solid wood beams | 0–32 | — | Width = 1/4 of depth up to = depth; girder ≥ 2 in deeper than its beams |
| Wood decking (plank-and-beam) | — | — | Beams limited to **20 ft with solid decking, 24 ft with laminated**. Heavy Timber minimums: 3 in nominal floor deck, 2 in nominal roof deck |
| Steel beams under wood joists | W8 8–13; W10 10–16; W12 12–18 | — | Useful for a garage-door header |

**Steel** (L/d and span ranges from Ruddy & Ioannides; depths and increments from the book):

| System | **L/d** | Span range (ft) | Notes |
| --- | --- | --- | --- |
| Steel beam (wide flange) | **20–28** | 0–75 | **Most economical 25–40 ft**; above ~40 ft switch to open-web joists |
| Steel joist, **floor** member | **20** | 8–144 | |
| Steel joist, **roof** member | **24** | 8–144 | The two ratios differ by use — this is the answer to "L/20 or L/24?" |
| Plate girder | 15 | 40–100 | |
| **Joist girder** | **12** | 20–100 | Depths 20–96 in in 4 in steps |
| **Steel truss** | **12** | 40–300 | Parallel-chord economical to 120–140 ft; ~12 ft deep is the shipping limit |
| Space frame | 12–20 | 80–300 | Not residential |
| Steel floor/roof decking | — | 3–12 | Spans between joists |

SJI series limits (the hard boundaries):

| Series | Depths | Maximum span |
| --- | --- | --- |
| **K-series** | 10–30 in | **60 ft** |
| **LH-series** | 18–48 in | **96 ft** |
| **DLH-series** | 52–120 in | 240 ft |
| Joist girders | 20–120 in | 120 ft |

Industry practice also caps **span ≤ 24 × joist depth** for non-composite joists (30 × for composite).

**Concrete** (rarely relevant to a barndominium; included for completeness):

| System | Typical span (ft) | Depth ÷ span |
| --- | --- | --- |
| One-way solid slab | 10–20 | **L/22** |
| One-way joist (pan joist) | 18–36 | **L/18** (wide-module economical to ~40) |
| Two-way flat plate | 10–36 | **L/30** |
| Two-way flat slab (drop panels) | 10–40 | ~L/33–L/36 *(unverified)* |
| Waffle slab | 25–55 | **L/24** |
| Concrete beams | — | **L/16** |
| Concrete girders | — | **L/12** |
| Precast hollow-core plank | 20–40 | **L/40** (L/35–L/40 working band); depths 6–12 in, add 2 in topping |
| Precast double tees | 30–90 | **L/30**; less economical past 60–80 ft (transport) |
| Precast single tees | 40–120 | **L/30** |
| Precast beams | 25–65 | **L/16** |

### Post-frame specifics (what barndominiums actually are)

Post-frame (pole barn) construction is not in the *Studio Companion*'s chart set — it is an agricultural
/ light-commercial system, and the book targets institutional work. These numbers come from post-frame
industry practice (NFBA design guidance, truss manufacturer catalogues):

| Post-frame parameter | Value |
| --- | --- |
| Building widths commonly offered | **24–80 ft** (30, 40, 50, 60 the volume sizes) |
| **Single-ply** truss practical maximum | **~60 ft** |
| **Two-ply / multi-ply** truss | **72–80 ft** |
| Post (column) spacing | **8–12 ft o.c.** (8 ft traditional; 10 and 12 ft with heavier headers) |
| Truss spacing | 2–10 ft o.c.; **4 ft and 8 ft** most common |
| Roof pitches commonly stocked | **3:12, 4:12, 6:12, 8:12** |
| Eave heights commonly offered | **8, 10, 12, 14, 16 ft** |
| Truss members | 2×6 or larger chords, 2×4 or larger webs |
| Column embedment | 4–5 ft, or bracket-mounted on piers |

- **Where the truss types break** is the number that matters for rule thresholds: single-ply stops near
  **60 ft**, multi-ply reaches **72–80 ft**, and past 80 ft you are out of post-frame entirely. This is
  the direct justification for `SPAN_LONG` at 60–80 ft and `SPAN_INFEASIBLE` above 80 ft.
- barndsl's `COMFORT_BAY = 12.0` info threshold is **well calibrated**: 12 ft is exactly the top of
  ordinary post-frame column spacing, and past it members need engineering attention.
- **Steel rigid-frame alternative:** metal-building rigid frames are charted at **25–100 ft**, with clear
  spans beyond 300 ft achievable in special cases. Roof pitches 1/2:12 to 12:12, eave heights 8–30 ft.
  Note the very different **frame spacing: 20–25 ft typical, up to 40 ft** — an order of magnitude wider
  than post-frame bents, because the frames are much heavier and fewer.
- **Cost:** industry commentary puts the structural premium for the 40–60 ft clear-span band at roughly
  **10–30%** of the structural package versus a column-supported scheme.
- **Wide clear spans and floor framing:** the clear span that matters for a barndominium is usually the
  *roof*. Once you add a loft, the *floor* span becomes the binding constraint and it is much shorter —
  parallel-chord floor trusses run to about 40 ft, versus a 60 ft roof truss. A loft spanning the full
  shop width is a much bigger structural ask than the roof above it, and authors routinely miss this.

### Bay sizing and proportion

- A structural bay works best when it is **roughly square to about 1.5:1** — for steel framing the
  guidance is a bay of about **1,000 sq ft** with the long side **1.25–1.5×** the short side. Push past
  ~2:1 and the short-direction members are underused while the long-direction members are overworked.
- Published bay optimums worth knowing: **30 × 40 ft** for cantilever/continuous framing, **40 × 40 ft**
  for truss-joist framing, and **30 × 34 ft** as a typical office-building example.
- Deck spans the **short** direction; beams/joists span the **long**; girders pick up the beams. Getting
  this backwards is the classic student error, and it is exactly what barndsl's `WALL_BEARING_AXIS`
  info catches (a declared bearing wall running parallel to the bents can't carry a post line).
- **For barndominiums the commercial bay rule inverts.** The "bay" is the bent spacing along the long
  axis (8–12 ft) crossed with the clear span across the short axis (40–60 ft) — roughly **5:1**, wildly
  non-square by commercial standards. That is correct for the building type, because the bents are cheap
  and repetitive while the span is the expensive move. **Do not import the 1.5:1 bay rule into barndsl**;
  it would flag every valid post-frame plan.

### Rule candidates — structural system selection

barndsl's frame model (`frame bay <ft> span <ft> post <in>`) already exposes the two numbers these rules
need. Existing codes: `BAY_WIDE` (info, bay > 12 ft), `POST_OBSTRUCT`, `POST_IN_OPENING`,
`WALL_BEARING_AXIS`, `LOAD_PATH`, `OVERHEAD_HEADER` (warning, overhead door > 10 ft).

| Code | Sev | Predicate | Rationale |
| --- | --- | --- | --- |
| `SPAN_INFEASIBLE` | **ERROR** | Frame clear span > 80 ft with no engineered-system declaration | Past stock post-frame truss capacity entirely; the plan is asserting something no catalogue product does |
| `SPAN_LONG` | **WARNING** | Frame clear span in 60–80 ft | Beyond stock trusses (≤ ~60 ft); needs custom trusses or a steel rigid frame, and a cost premium |
| `SPAN_UNECONOMICAL` | **INFO** | Frame clear span in 44–60 ft **and** no room in the spanned block is wider than 30 ft | Paying for a clear span nothing uses; an interior bearing line would be cheaper |
| `SPAN_NONSTANDARD` | **INFO** | Clear span not within 1 ft of {24, 30, 32, 36, 40, 44, 48, 50, 54, 60} | Stock truss sizes; a 42 ft span costs custom money for no benefit |
| `BAY_TIGHT` | **INFO** | Frame bay < 6 ft o.c. | Below post-frame economy; you are buying columns you don't need |
| `BAY_ASPECT` | **INFO** | (clear span ÷ bay) > 8 | Extremely elongated structural bay; expected for post-frame, so keep this quiet or omit — documented here to explain *why* the commercial ~1.5:1 bay rule must **not** be imported |
| `LOFT_SPAN_LONG` | **WARNING** | A level ≥ 1 room's short dimension > 35 ft with no interior support below | Floor framing is far past parallel-chord truss economy; the roof span does not license the floor span |
| `LOFT_SPAN_INFEASIBLE` | **ERROR** | Level ≥ 1 room short dimension > 60 ft unsupported | No residential floor system does this |
| `HEADER_SPAN_LONG` | **WARNING** | Any opening wider than 16 ft | Existing `OVERHEAD_HEADER` fires at 10 ft; a second tier at 16 ft flags where a glulam/steel header and engineered posts are unavoidable |
| `BEARING_LINE_MISSING` | **INFO** | A level-0 block's short dimension > `frame span` **and** no interior bearing wall declared along the long axis | The frame will auto-insert an interior post line; better to align a partition with it deliberately |

**Scoring:** structural rules should feed the existing `warnings`/`infos` buckets, not a new continuous
term. Rationale: span feasibility is a step function (a truss either exists at that span or doesn't),
so there is no useful gradient to descend. The one exception worth considering is a **`structure`
continuous term** penalizing *unused clear span* — `max(0, frame_span − widest_room_in_block) / frame_span`
scaled to ~4 points — which gives the agent a smooth signal to either narrow the building or widen the
shop rather than paying for span it wastes.

---

## 5. Designing the structure — preliminary member sizing

### What the book does

Having chosen a system, the book gives approximate depths and column sizes so the designer can draw a
section and a floor-to-floor height that will survive engineering. The core heuristics:

### The depth heuristics

Three mnemonics do most of the work, and they are exact restatements of L/d ratios rather than
approximations:

| Mnemonic | Equivalent ratio | Applies to |
| --- | --- | --- |
| **1/2 inch of depth per foot of span** — depth(in) = 0.5 × span(ft) | **L/24** | Steel joists, **roof** |
| **0.6 inch per foot** — depth(in) = 0.6 × span(ft) | **L/20** | Steel joists, **floor** |
| **1 inch per foot** — depth(in) = span(ft) | **L/12** | **Joist girders and steel trusses** |

- **The master rule: structural depth ≈ span ÷ 20.** Right for glulam (L/20 exactly), steel beams
  (L/20–L/28), and steel floor joists (L/20). Start here, then adjust by system using the L/d column
  in §4.
- **Steel joists differ by use, and this is the answer to the common "L/20 or L/24?" confusion:**
  **floor members are L/20, roof members are L/24.** A 40 ft roof joist is ~20 in deep; the same span
  carrying a floor wants ~24 in.
- **Joist girders and trusses are far deeper** — L/12, i.e. depth in inches equals span in feet. A 40 ft
  joist girder is ~40 in deep. Budget this before drawing a section; it is the number most often missed.
- **Residential wood joists: depth in inches ≈ (span in feet ÷ 2) + 2.** A 2×8 reaches ~12 ft, a 2×10
  ~15 ft, a 2×12 ~18 ft at 16 in o.c. under ordinary residential floor load. This is the rule builders
  actually carry in their heads and it agrees with the IRC span tables closely enough for schematic work.
  Add **2–3 in to joist depth for the finished floor** assembly.
- **Pitched roof trusses have no L/d ratio** — depth is set by the pitch. At 4:12 over a 40 ft span the
  heel-to-peak depth is about 6.7 ft. This is why a post-frame building's roof volume is large enough to
  hide ductwork and, sometimes, a loft.
- **Glulam proportions:** depth per L/20, and **width ≈ depth ÷ 3 to depth ÷ 7**. Depths come in
  1-1/2 in multiples; widths are 3-1/8, 5-1/8, 6-3/4, 8-3/4 in. A girder should be **at least 1-1/2 in
  deeper** than the glulam beams it carries (2 in for solid sawn).
- **Deflection is the usual governing limit,** not strength: L/360 for floors carrying plaster/gypsum
  ceilings, L/240 for roofs, L/180 for some roof/live cases. When a rule of thumb feels conservative,
  deflection is why.
- **Practical joist cap:** span ≤ **24 × joist depth** for non-composite steel joists (30 × composite).

### Columns and vertical elements

- **Steel columns** are sized from tributary area × load × number of stories above. A W8 or W10 handles
  most low-rise bays; the practical preliminary move is to allow a column footprint of about 8–12 in
  square for light steel, growing with tributary area.
- **Post-frame columns** are typically nominal 6×6 solid-sawn or 3-ply 2×6 laminated for ordinary
  residential barndominiums (barndsl's `frame ... post 6` default matches this), stepping up to 6×8 or
  glulam columns for tall walls, wide bays, or high wind/snow.
- **Column embedment/footing:** post-frame posts are typically embedded 4–5 ft or set on engineered
  brackets over piers. Not modelled by barndsl and probably shouldn't be.
- **Bearing walls** in light framing: a 2×6 stud wall at 16 in o.c. covers ordinary residential loads;
  the useful preliminary rule is that bearing walls need a continuous path to the foundation — which is
  precisely barndsl's `LOAD_PATH` info (upper-level partition with nothing beneath it).

### Floor-to-floor height budgeting

The book's method for setting section heights, restated as an additive budget:

```
floor-to-floor = ceiling height
               + ceiling/plenum allowance (lights, ducts, sprinkler)
               + structural depth (span ÷ 20, or per system)
               + floor construction (topping, finish)
```

For a barndominium loft over a living wing:
- 8 ft ceiling below
- 12–18 in for ducts and lights if ducts run in the joist space (0 in if the truss webs absorb them)
- 12–20 in of floor truss/joist depth
- 1–2 in floor build-up

→ roughly **10 to 11 ft floor-to-floor** for an 8 ft ceiling. This is a good sanity check for any plan
that declares both a level-0 ceiling height and a level-1 room.

### Rule candidates — member sizing

These need model fields barndsl does not have today. Flagged accordingly.

| Code | Sev | Predicate | Needs |
| --- | --- | --- | --- |
| `FLOOR_DEPTH_UNSTATED` | **INFO** | A level ≥ 1 room exists and no floor-system depth is declared | New optional `frame floor-depth <in>` or a per-level attribute |
| `FLOOR_TO_FLOOR_TIGHT` | **WARNING** | (level-1 floor elevation − level-0 ceiling height) < span ÷ 20 for the widest unsupported floor span | Needs level elevations, which barndsl models only implicitly |
| `POST_UNDERSIZED` | **INFO** | `frame post` < 6 in **and** (clear span > 40 ft **or** bay > 10 ft) | Available today — `frame` already carries `post` |
| `POST_OVERSIZED` | **INFO** | `frame post` > 8 in with span ≤ 40 ft and bay ≤ 10 ft | Available today |
| `CEILING_LOFT_BUDGET` | **INFO** | level-0 ceiling + 2.0 ft > declared eave/wall height, with a level-1 room present | Approximates the floor-to-floor budget with the fields that exist |

**Recommendation:** implement `POST_UNDERSIZED`/`POST_OVERSIZED` now (zero new model surface, real value)
and defer the rest until the model carries member depths. Adding a `frame floor-depth` option would
unlock the whole family cheaply and is a small, well-scoped feature.

---

## 6. Designing for egress

### What the book does

The egress material walks the IBC path: compute **occupant load** → determine the **number of exits**
required → check **exit access travel distance**, **common path of egress travel**, and **dead-end**
limits → size the **egress width** (doors, corridors, stairs) → verify **exit separation**.

**Applicability warning, restated because it is the biggest false-positive risk in this document:** an
IRC dwelling does not do any of this. It needs one compliant egress door, emergency escape and rescue
openings from sleeping rooms and basements, compliant stairs, and compliant hallways. Everything below
in the IBC subsection applies to the **shop side treated as a separate occupancy**, or to a barndominium
that has left IRC scope.

### IRC egress (the living wing) — the rules that actually bind

| Requirement | Value | IRC section |
| --- | --- | --- |
| Required egress doors | **at least one** side-hinged exterior door, not through a garage | R311.2 |
| Egress door clear width | **≥ 32 in** clear | R311.2 |
| Egress door clear height | **≥ 78 in** clear | R311.2 |
| Hallway width | **≥ 36 in** | R311.6 |
| Landing at each exterior door | ≥ 36 in in the direction of travel, ≥ the door's width | R311.3 |
| Landing drop at the required egress door | ≤ **1-1/2 in** below the threshold | R311.3.1 |
| Habitable room ceiling height | **≥ 7 ft** | R305.1 |
| Habitable room minimum area | ≥ 70 sq ft (one room), ≥ 7 ft in any horizontal dimension | R304 |
| Emergency escape opening (sleeping rooms, basements, habitable attics) | **5.7 sq ft** net clear (**5.0** at grade floor and below-grade) | R310.2.1 |
| — minimum clear height | **24 in** | R310.2.1 |
| — minimum clear width | **20 in** | R310.2.1 |
| — maximum sill height above floor | **44 in** | R310.2.2 |
| Window well serving an escape opening | ≥ **9 sq ft**, ≥ **36 in** in any horizontal dimension | R310.2.3 |
| Window well ladder/steps required | where the well is deeper than **44 in** | R310.2.3.1 |
| Natural light | glazing ≥ **8%** of floor area | R303.1 |
| Natural ventilation | openable ≥ **4%** of floor area | R303.1 |

Note the scope of R310: the escape opening is required from **every sleeping room**, from basements,
and from habitable attics — not merely from bedrooms on upper floors. barndsl's `BEDROOM_EGRESS` error
already reflects this.

`barndsl` implements essentially all of these already (`EGRESS_DOOR`, `EGRESS_SIZE`, `BEDROOM_EGRESS`,
`HALL_WIDTH`, `CEILING`, `NAT_LIGHT`, `VENT_AREA`, `DOOR_NO_LANDING`, `WINDOW_SILL`), and several are
already jurisdiction-profile keys. This section is therefore mostly *confirmation* that the existing
thresholds are right, plus the gaps below.

### IRC stairs (R311.7)

| Requirement | IRC value (dwellings) | IBC value (for contrast) |
| --- | --- | --- |
| Maximum riser | **7-3/4 in** | **7 in** (4 in minimum) |
| Minimum tread depth | **10 in** | **11 in** |
| Minimum clear width above handrail | **36 in** | **44 in** (36 in where occupant load served < 50) |
| Minimum width at/below handrail height | 31-1/2 in (one handrail), 27 in (two handrails) | — |
| Minimum headroom | **6 ft 8 in** (80 in) | 80 in |
| Maximum vertical rise per flight | **12 ft 7 in** (151 in) | 12 ft |
| Riser/tread uniformity tolerance | 3/8 in max variation | 3/8 in |
| Handrail height above nosings | 34–38 in | 34–38 in |
| Handrails required | one side, 4+ risers | both sides |
| Landing depth | ≥ stair width, min **36 in** | **48 in** minimum |
| Guard height (open sides) | ≥ 36 in (dwellings) | 42 in |
| Guard opening limit | 4 in sphere | 4 in sphere |

barndsl already encodes `_STAIR_MAX_FLIGHT_RISE = 151/12` and has `STAIR_*` codes covering geometry,
headroom, landing, handrail, run, and width — a good match. The **7 in / 11 in** figure the brief
mentions is the *IBC* rule; using it for a dwelling would be over-strict. It belongs in a `strict`
jurisdiction profile, not the default — and the existing `max_riser_height` / `min_tread_depth` profile
keys already make that a configuration change rather than a code change.

The IRC's **width-below-handrail** allowance (31-1/2 in / 27 in) is a subtlety worth encoding if
barndsl ever models handrail placement: the 36 in requirement is measured *above* handrail height, so
a 36 in stair with two handrails is compliant even though the usable width at hand level is 27 in.

### IBC egress (the shop side, or an out-of-scope building)

**Occupant load factors (IBC Table 1004.5), gross floor area per occupant:**

| Use | sq ft per occupant |
| --- | --- |
| Residential | 200 gross |
| Business | 150 gross (100 in editions before 2018) |
| Storage, mechanical equipment room | 300 gross |
| Accessory storage, mechanical | 300 gross |
| Warehouse | 500 gross |
| Industrial | 100 gross |
| Assembly, unconcentrated (tables/chairs) | 15 net |
| Assembly, standing | 5 net |
| Parking garages | 200 gross |

A 1,600 sq ft shop at 300 sq ft/occupant = 6 occupants. At 100 sq ft/occupant (industrial) = 16. Either
way it stays under the thresholds that force a second exit — which is the point worth encoding.

**When two exits are required (IBC Table 1006.2.1 / 1006.3.3):** a space with one exit is allowed up to
an occupant load of **49** for most occupancies (**29** for higher-hazard groups, **10** for H groups),
subject to the common-path limit. Above that, two exits; above 500 occupants, three; above 1,000, four.

**Travel distance limits (IBC Table 1017.2), exit access travel distance:**

| Occupancy | Unsprinklered (ft) | Sprinklered (ft) |
| --- | --- | --- |
| B (business) | 200 | 300 |
| R (residential) | 200 | 250 |
| S-1 (storage, moderate) | 200 | 250 |
| S-2 / U (low hazard, utility) | 300 | 400 |
| F-1 (factory, moderate) | 200 | 250 |
| F-2 / S-2 (low hazard) | 300 | 400 |

**Common path of egress travel (IBC Table 1006.2.1):**

| Occupancy | Unsprinklered (ft) | Sprinklered (ft) |
| --- | --- | --- |
| B | 75 | 100 |
| R-2 | 75 | 125 |
| S-1, S-2, U | 75 | 100 |
| F | 75 | 100 |

**Dead-end corridors (IBC 1020.4):** **20 ft** maximum generally; **50 ft** where the building is
sprinklered (groups B, E, F, I-1, M, R-1, R-2, R-4, S, U).

**Egress width:**
- Corridors: **44 in** minimum; **36 in** permitted where the occupant load served is fewer than 50.
- Doors: **32 in clear** minimum (a 36 in leaf yields it); max single-leaf width 48 in.
- Egress capacity factors: 0.2 in/occupant for level components, 0.3 in/occupant for stairways
  (0.15 / 0.2 where sprinklered with an alarm system).
- Exit separation: exits at least **1/2 the diagonal** of the area served apart (**1/3** if sprinklered).

### Rule candidates — egress

Existing barndsl codes cover the IRC baseline well. These fill the gaps, with a strong bias toward
`INFO` on anything IBC-derived so the living wing never gets falsely flagged.

| Code | Sev | Predicate | Notes |
| --- | --- | --- | --- |
| `SHOP_SECOND_EXIT` | **INFO** | A `SHOP`/`GARAGE` > 1,500 sq ft has fewer than 2 exits to outside (an overhead door counts) | Shop occupant load stays low, so this is prudence, not code |
| `SHOP_TRAVEL_DISTANCE` | **INFO** | Any point in a shop/garage is > 100 ft from an exterior door | Massively inside the 300–400 ft IBC limit; the useful signal is *practical* egress, not the code number. Consider 150 ft to reduce noise |
| `EGRESS_DEAD_END` | **INFO** | A hallway run > 20 ft with rooms on only one dead end | Complements the existing `HALL_DEADEND`; the 20 ft IBC number is a defensible bar |
| `EGRESS_TRAVEL_BEDROOM` | **INFO** | A bedroom door is > 75 ft of path from the nearest exterior egress door | Adapts the common-path idea to a dwelling; catches sprawling single-story barndominiums where the far bedroom is a long way out |
| `EGRESS_SINGLE_PATH` | **WARNING** | Every bedroom's only interior route to an exterior door passes through one room (an articulation point in the door graph) | Genuinely useful for dwellings: a single choke point means one blocked room strands the sleeping wing |
| `EGRESS_THROUGH_GARAGE` | **WARNING** | The *only* exterior egress door reachable from the living core is in a garage/shop | R311.2 explicitly disallows the required egress door being through a garage. May overlap `GARAGE_PASSTHROUGH` |
| `STAIR_IBC_PROFILE` | — | — | Not a code; a note that 7 in/11 in belongs in a `strict` profile via existing `max_riser_height` / `min_tread_depth` keys |

**Scoring:** propose a new continuous **`egress`** term worth up to **6 points**, computed as the
normalized worst-bedroom egress path length:

```
egress_penalty = 6 * clamp((worst_bedroom_path_ft - 50) / (120 - 50), 0, 1)
```

Free below 50 ft of travel, full penalty at 120 ft. Rationale mirrors the existing `circulation` and
`daylight` terms: the diagnostic gives the step, the continuous term gives the agent a gradient to
descend while laying out a long, thin barndominium. It is also a genuine quality signal — the distance
from the far bedroom to the front door is something people feel every day.

---

## 7. Designing for accessibility

### What the book does

The accessibility material distils the ADA Standards for Accessible Design and ANSI/ICC A117.1 into the
dimensions a designer needs at schematic stage: how wide a route is, how much floor a wheelchair needs
to turn, how much room a door needs beside its latch, and what an accessible toilet room and kitchen
require.

**Applicability:** the ADA does not reach private single-family dwellings, and the IRC does not require
accessibility in one- and two-family homes. This entire section is **voluntary design quality** for a
barndominium — which is exactly how barndsl already treats it (every `ACCESS_*` code is `INFO`). It is,
however, one of the highest-value voluntary standards for this building type, because barndominiums skew
rural, owner-built, single-story, and long-tenure — aging in place is the norm, not the exception.

### Core dimensions (ADA 2010 Standards / ICC A117.1)

| Element | Requirement |
| --- | --- |
| Accessible route clear width | **36 in** minimum; may narrow to **32 in** for a length of **≤ 24 in** |
| Two wheelchairs passing | 60 in |
| Turning space, circular | **60 in** diameter |
| Turning space, T-shaped | 60 × 60 in envelope, 36 in wide arms |
| Clear floor space (single wheelchair) | **30 × 48 in** |
| Door clear opening | **32 in** minimum (needs a ~34–36 in leaf) |
| Door maneuvering, front approach, **pull** side | **18 in** beyond the latch + **60 in** deep |
| Door maneuvering, front approach, **push** side | **12 in** beyond latch (if closer+latch) + **48 in** deep |
| Door maneuvering, hinge approach, pull | 36 in latch side + 60 in deep (or 42 + 54) |
| Door maneuvering, latch approach, pull | 24 in beyond latch + 60 in (54 in if no closer) |
| Threshold | ≤ 1/2 in (≤ 3/4 in at exterior sliding doors) |
| Door opening force, interior | ≤ 5 lbf |
| Ramp slope | **1:12** maximum (1:20 and flatter is not a ramp) |
| Ramp maximum rise per run | **30 in**, then a landing |
| Ramp landings | 60 in long minimum; 60 × 60 at direction changes |
| Ramp clear width | 36 in between handrails |
| 180° turn around an obstruction < 48 in wide | 42 in approach corridors + 48 in turn |
| Water closet clearance | **60 in** wide × **56 in** deep (wall-hung) / 59 in (floor-mounted) |
| Water closet centerline from side wall | 16–18 in |
| Grab bars | 33–36 in above floor; 42 in side bar, 24 in rear bar minimum |
| Lavatory | 34 in max rim height; 27 in knee clearance; 30 × 48 forward approach |
| Roll-in shower | 30 × 60 in minimum |
| Transfer shower | 36 × 36 in with a 36 × 48 clear space |
| Kitchen clearance, pass-through | **40 in** between opposing counters/appliances/walls |
| Kitchen clearance, U-shaped | **60 in** between opposing elements |
| Kitchen work surface | 34 in max height, 30 in wide clear floor space |
| Reach range, forward or side, unobstructed | 15–48 in above floor |
| Protruding objects | ≤ 4 in projection between 27 and 80 in above floor |
| Headroom on a circulation path | ≥ 80 in |
| Accessible parking stall | 8 ft stall + **5 ft** access aisle |
| Van-accessible stall | 11 ft stall + 5 ft aisle (or 8 ft stall + 8 ft aisle) |

### Rule candidates — accessibility

barndsl already has `ACCESS_DOOR` (32 in), `ACCESS_TURN` machinery (`ACCESSIBLE_TURN = 5.0`),
`ACCESS_BATH`, `ACCESS_ENTRY`, `ACCESS_SINGLE_FLOOR`. Gaps worth adding, all `INFO`:

| Code | Sev | Predicate |
| --- | --- | --- |
| `ACCESS_ROUTE` | **INFO** | A hallway on the path from the accessible entry to a bedroom/bath is < 36 in (already `HALL_WIDTH` at 36 in — consider whether a 42 in "comfortable" tier is worth an info) |
| `ACCESS_DOOR_APPROACH` | **INFO** | A door's latch side has < 18 in of clear wall on the pull side | Needs wall-segment geometry barndsl already derives for `DOOR_CORNER_MARGIN = 2.0` (24 in) — tightening this to a directional 18 in latch-side check is a natural extension |
| `ACCESS_KITCHEN_CLEAR` | **INFO** | Opposing counter runs closer than 40 in (or 60 in where the layout is U-shaped) | barndsl models counter runs (`fixture counter ... along <wall>`) so this is computable |
| `ACCESS_RAMP_SLOPE` | **INFO** | A declared grade change at an entry exceeds 1:12 over the available run | Needs site grade, which `docs/design/SITE_SOLAR.md` territory partly covers |
| `ACCESS_SHOWER` | **INFO** | A primary bath has no 30 × 60 clear area suitable for a roll-in shower | Aging-in-place nudge, very apt for this building type |
| `ACCESS_BEDROOM_TURN` | **INFO** | A ground-floor bedroom has no 60 in turning circle clear of the furniture envelope | Extends the existing `BED_CLEARANCE` reasoning |
| `ACCESS_THRESHOLD` | **INFO** | An exterior door into the shop/garage from the dwelling has a declared step | Existing `DOOR_THRESHOLD` may already cover |

**Scoring:** keep accessibility out of the continuous terms. It is voluntary, and barndsl's `infos`
bucket (2 points each, capped at 20) is the right weight — enough to break a tie between two otherwise
equal plans, not enough to dominate. If a future "aging in place" *profile* is added, the right move is
to let that profile **promote** selected `ACCESS_*` codes from `INFO` to `WARNING` rather than to change
the score formula.

---

## 8. Designing spaces for mechanical and electrical services

### What the book does

The MEP section answers "how much of the building do the systems eat?" — floor area for equipment rooms,
vertical shaft area, horizontal plenum depth, and the space around gear that keeps it serviceable. The
book's approach is percentage-of-floor-area budgets, which are easy to check.

### Equipment space as a fraction of gross floor area

Consolidated commercial rules of thumb (ASHRAE, MEEB):

| System type | Mechanical space as % of gross floor area |
| --- | --- |
| Simple packaged / rooftop, small building | **1–3%** |
| Split systems, light commercial | 3–5% |
| Central all-air (VAV) with AHU rooms | **4–7%** |
| Central plant with chillers/boilers | **6–9%** |
| Total MEP including electrical, telecom, plumbing | up to **9–12%** |

> **Confidence: moderate.** These commercial percentage bands are widely repeated in space-planning
> practice but I could not pin them to a citable published table during this research pass. Treat them
> as orientation, not as thresholds to hard-code. The residential numbers below are the ones that
> actually matter for barndsl, and they are better grounded.

For a **residential** barndominium the applicable number is far smaller — a furnace/air-handler, a water
heater, a panel, and often a softener:

| Scope | Footprint |
| --- | --- |
| Bare furnace/air-handler closet | ~10–15 sq ft (equipment + front service access) |
| Utility room: furnace + water heater + softener | **40–80 sq ft** |
| Published minimum for a small-facility mechanical closet | ~70 sq ft |
| Larger residential buildings (multifamily, complex systems) | 150–200 sq ft |

barndsl's `MIN_MECH_AREA = 30.0` and `MIN_MECH_DIM = 5.0` sit sensibly at the low end — they are a
*floor* below which service access is impossible, not a target. For a barndominium that conditions the
shop as well as the house, **60–100 sq ft** is the realistic ask, because the shop side adds its own
equipment (compressor, dust collection, welding exhaust, radiant-floor manifolds). That gap between the
30 sq ft floor and the ~60 sq ft realistic target is what the proposed `MECH_AREA_RATIO` info covers.

### Horizontal distribution — plenum and ceiling depth

| Condition | Depth allowance |
| --- | --- |
| Residential ducts in a joist/truss space | 12–18 in (often absorbed by an open-web floor truss) |
| Small commercial ceiling plenum | 18–24 in |
| Commercial VAV with main trunks and structure | 24–48 in |
| Main duct trunk sizing (residential) | ~1 sq in of duct per ~2 CFM at typical velocities |
| Residential design airflow | roughly 400 CFM per ton; ~1 ton per 400–600 sq ft conditioned |

**Vertical shafts:** a common budget is shaft area of about **1–2%** of the floor area served per floor
for a fully ducted commercial system. Not meaningful for a one- or two-story barndominium, where a chase
between levels is a single dimensioned box.

### Service clearances that a plan can actually violate

These are the ones worth checking, because they are dimensional and authors forget them:

| Equipment | Clearance |
| --- | --- |
| **Electrical panel** (NEC 110.26) | **30 in wide × 36 in deep** working space in front, **6 ft 6 in** headroom; width may be centered on the equipment; the space must be clear floor-to-ceiling |
| Panel prohibited locations | Bathrooms, clothes closets, over steps |
| Furnace / air handler, front service access | ~30 in in front of the access panel (verify against listing) |
| Water heater, tank type | ~30 in front access; 18 in above floor for ignition source in a garage (IRC M1307.3 / G2408) |
| Water heater, tankless | Wall-mounted; vent clearances per listing; frees ~10 sq ft over a tank |
| Furnace in an attic/loft | 30 in × 30 in working platform, 24 in wide passageway, ≤ 20 ft to the unit |
| Appliance in a garage | Ignition source **≥ 18 in above the floor**, and protected from vehicle impact |
| Water heater / furnace combustion air | Sized per IRC Chapter 24; a sealed closet needs louvered openings or direct-vent equipment |

The **18 in ignition-source elevation** rule for garages is a barndominium-specific gotcha: putting the
water heater or furnace in the shop is a common cost-saving move, and it comes with real requirements.

### Rule candidates — mechanical and electrical space

barndsl has `MECH_CLEARANCE`, `MECH_ACCESS`, `MECH_BEDROOM`, `WATER_HEATER_PLACEMENT`, `ELECTRICAL_PLAN`,
`OUTLET_SPACING`, `OUTLET_GFCI`, `RECEPTACLE_COUNTER`, `PLUMBING_STACK`.

| Code | Sev | Predicate | Notes |
| --- | --- | --- | --- |
| `MECH_AREA_RATIO` | **INFO** | Total `MECHANICAL` + `UTILITY` area < 1.5% of conditioned floor area | Catches the plan with a token 30 sq ft closet serving a 3,000 sq ft house plus a shop |
| `MECH_SHOP_UNSERVED` | **INFO** | Shop area > 800 sq ft and no `MECHANICAL`/`UTILITY` room adjoins or opens to it | A conditioned shop needs its own equipment space; sharing the house system across the fire separation is awkward |
| `PANEL_CLEARANCE` | **WARNING** | A declared electrical panel has < 36 in of clear depth or < 30 in of clear width in front | NEC 110.26 is a hard requirement and a genuine inspection failure. Needs a panel fixture in the model |
| `PANEL_LOCATION` | **ERROR** | A panel is located in a `BATHROOM` or a `CLOSET` | NEC 110.26(A)(3) / 240.24(D)-(E); flatly prohibited |
| `IGNITION_HEIGHT` | **INFO** | A water heater or furnace fixture is placed in a `GARAGE`/`SHOP` | Reminder of the 18 in elevation and impact-protection requirements |
| `MECH_DUCT_RUN` | **INFO** | The straight-line distance from the mechanical room to the farthest conditioned room exceeds 60 ft | A long-thin barndominium with the mechanical room at one end has a real duct-run problem; central placement is cheaper and quieter |
| `PLENUM_DEPTH` | **INFO** | A level-1 room exists and the level-0 ceiling + 1.0 ft exceeds the declared wall height | Approximates "no room left for ducts" |
| `WATER_HEATER_DISTANCE` | **INFO** | A water heater is > 30 ft of pipe run from the farthest fixture group | Hot-water wait time; a real livability issue in sprawling plans |

**Scoring:** `MECH_DUCT_RUN` and `WATER_HEATER_DISTANCE` are both good candidates for the `infos` bucket
rather than continuous terms — they are secondary to the layout quality the existing terms already
measure. If a continuous **`services`** term is ever wanted, the cleanest formulation is a normalized
distance from the mechanical room's centroid to the area-weighted centroid of the conditioned rooms,
worth ~3 points, which rewards centrally placing the equipment.

---

## 9. Vertical transportation and stairs as circulation

### Status in the book

Elevator and escalator space planning is minor material in the *Studio Companion*, sitting inside the
mechanical/electrical services discussion rather than commanding its own section. Stairs get thorough
treatment, but under egress. For barndominiums this ordering is correct: a one- or two-story building
with a loft needs a good stair and, occasionally, a residential elevator for aging in place.

### Numbers

**Residential elevators:**
- Typical cab: 36 × 48 in to 40 × 54 in; hoistway roughly 5 ft × 5 ft (about **25 sq ft** of footprint
  per floor plus a machine space).
- Pit depth 8–12 in for common residential hydraulic/winding-drum units; overhead clearance ~8 ft above
  the top landing.
- ADA-compliant passenger elevator (not required in dwellings): 51 × 68 in minimum interior, 36 in door.

**Commercial rules of thumb (context, not applicable):**
- Roughly one passenger elevator per 30,000–45,000 sq ft of office area, or per 250–300 occupants.
- Escalator capacity ~2,000–5,000 persons/hour depending on width and speed.

**Stairs as a space budget:**
- A straight residential run rising 10 ft at 7-1/2 in risers needs 16 risers, 15 treads at 10 in =
  12.5 ft of run — plus landings. Budget roughly **35–45 sq ft** per floor for a straight run and
  **50–65 sq ft** for a U-shaped run with a landing.
- A switchback saves length at the cost of width: 6–7 ft wide by 9–11 ft long.

### Rule candidates — vertical circulation

barndsl has `STAIR_GEOMETRY`, `STAIR_RUN`, `STAIR_SIZE`, `STAIR_LANDING`, `STAIR_HEADROOM`,
`STAIR_HANDRAIL`, `STAIR_LEVELS`, `STAIR_WALL`, `STAIR_FLOAT`, `STAIR_BLOCKS_DOOR`, `STAIR_OOB`,
`ACCESS_SINGLE_FLOOR`. Coverage is strong. Additions:

| Code | Sev | Predicate |
| --- | --- | --- |
| `STAIR_LANDS_IN_PUBLIC` | **INFO** | A stair's lower landing sits inside a `LIVING`/`GREAT_ROOM`/`KITCHEN` with no adjoining hall or foyer | Traffic through the living core; a foyer or hall landing is better |
| `STAIR_FAR_FROM_ENTRY` | **INFO** | The stair's lower landing is > 40 ft of path from the primary entry | Loft access should be near the front of the plan |
| `ELEVATOR_FUTURE` | **INFO** | A two-level plan with an `ACCESS_SINGLE_FLOOR` finding and no ~25 sq ft stacked closet pair reserved | "Stack two closets for a future elevator" is the classic aging-in-place move and is cheap to check: a `CLOSET` on level 0 and level 1 with ≥ 20 sq ft overlapping footprint |

`ELEVATOR_FUTURE` is a nice, specific, checkable rule with real value for this building type, and it
composes with the existing `ACCESS_SINGLE_FLOOR` info rather than duplicating it.

---

## 10. Designing with daylight

### What the book does

The daylight section gives geometric rules for how deep daylight reaches, how much glass a room needs,
and how to combine side lighting with top lighting. The headline heuristics:

### The core rules of thumb

- **Daylight penetration depth ≈ 2 to 2.5 × the window head height** above the floor. A window whose head
  is at 8 ft lights about **16–20 ft** into the room adequately; past that you are in electric-light
  territory. With a light shelf or a high clerestory the multiplier stretches to about 2.5×.
- **Room depth-to-height ratio ≈ 2:1 to 2.5:1** for good single-sided daylighting. Deeper than that and the
  back of the room goes dim regardless of glass area.
- **Glazing area as a fraction of floor area:**
  - **IRC R303.1 minimum for habitable rooms: 8%** of floor area in glazing, **4%** openable for
    ventilation. This is a *health* minimum, not a daylighting target. (barndsl's `DAYLIGHT_RATIO = 0.08`
    and `NATURAL_VENT_RATIO = 0.04` match exactly.)
  - **Daylighting design target: roughly 15–20%** of floor area in glazing for a genuinely daylit room,
    concentrated high on the wall rather than spread low.
  - Beyond ~25–30% window-to-wall ratio the thermal penalty usually outruns the daylight benefit —
    which is the reasoning behind barndsl's existing `WWR_CEILING` / `WINDOW_HEAVY` checks.
- **Toplighting:** published skylight-to-floor-area ratios range widely — **3–5%** is the band usually
  cited for uniform daylighting with properly spaced units, optimization studies span **3–9%**, and some
  residential guidance goes as high as 5–15% for "adequate" light. Use **3–6%** as the working target
  and treat anything outside 1–9% as suspect. Toplighting is far more efficient per square foot of glass
  than sidelighting but only works on the top floor. *(Confidence: moderate — the spread between sources
  is real, driven by climate, glazing type, and whether the goal is daylight harvesting or ambience.)*
- **Window head height is the lever, not window area.** Raising the head 1 ft buys 2–2.5 ft of daylight
  depth; widening the window buys uniformity, not depth. This is the most under-used rule in residential
  design and the most valuable one to encode as a nudge.
- **Orientation:** south glazing is controllable with a fixed overhang; west glazing produces the worst
  low-angle summer gain; north is diffuse and stable. barndsl already models this
  (`SOLAR_SOUTH_UNUSED`, `SOLAR_WEST_GAIN`, `SOLAR_NORTH_ONLY`, `SOLAR_SOUTH_NO_OVERHANG`).
- **Overhang sizing rule of thumb:** for south glazing at mid-US latitudes, an overhang projection of
  roughly **0.4–0.5 × the window height** below the overhang shades the summer sun while admitting winter
  sun.

### Barndominium-specific daylight problems

A barndominium is a **deep rectangle**, often 40–60 ft across, with a big opaque shop at one end. Three
consequences that a rule set should catch:

1. **The middle of the building is dark.** With a 40 ft depth and 8 ft window heads, side lighting from
   both long walls reaches ~16–20 ft from each side — which just barely meets in the middle at 40 ft, and
   fails entirely at 50–60 ft. Clerestories, a monitor roof, or a light court are the standard fixes.
2. **Interior rooms against the shop wall get nothing.** A bedroom sharing a wall with the shop has one
   exterior wall at best and no daylight from the shop side.
3. **The shop itself is usually unlit.** Shops want daylight (working light, and it is free), and the
   easy move is a row of high windows or translucent wall panels above the overhead doors.

### Rule candidates — daylight

barndsl has `NAT_LIGHT`, `ROOM_NO_LIGHT`, `VENT_AREA`, `WINDOW_HEAVY`, `WINDOW_SILL`, `WINDOW_INTERIOR`,
`WINDOW_PARTITION`, plus the `daylight` continuous score term. Additions:

| Code | Sev | Predicate | Notes |
| --- | --- | --- | --- |
| `DAYLIGHT_DEPTH` | **INFO** | A habitable room's depth measured perpendicular from its glazed exterior wall exceeds **2.5 × window head height** | The book's signature rule. Needs a window head height; barndsl has `_WINDOW_TYP_HEIGHT = 3.67` and sill data, so head is derivable |
| `DAYLIGHT_SINGLE_SIDED` | **INFO** | A habitable room > 300 sq ft has glazing on only one wall | Two-sided daylighting is dramatically better; large great rooms in barndominiums frequently have one glazed gable end |
| `DAYLIGHT_CORE_DARK` | **INFO** | The plan's footprint is deeper than **2 × (2.5 × head height)** and no clerestory/skylight is declared | The barndominium-specific version: a 50 ft deep building cannot be side-lit through the middle |
| `SHOP_NO_LIGHT` | **INFO** | A `SHOP` > 600 sq ft with zero windows | Working light; also a safety and comfort issue |
| `DAYLIGHT_TARGET` | **INFO** | A habitable room's glazing is ≥ 8% (passes code) but < 12% of floor area | A second tier above the code floor. **Risk:** this will fire on most plans; gate it behind a `quality`-focused profile or omit |
| `WINDOW_HEAD_LOW` | **INFO** | A window's head height is below 6.5 ft in a room deeper than 14 ft | Head height is the daylight lever |

**Scoring:** the existing `daylight` term (8 points, shortfall below the 8% floor) measures *compliance*
distance. Consider extending it — or adding a small **`daylight_depth`** term worth ~3 points — that
penalizes the fraction of habitable floor area lying beyond 2.5 × head height from a glazed wall:

```
dark_fraction = (habitable area beyond the daylight-depth line) / (total habitable area)
penalty = 3 * clamp(dark_fraction / 0.5, 0, 1)
```

This is a genuine gradient (the agent can shrink room depth, add a second window wall, or raise heads),
it is orthogonal to the existing glazing-ratio term, and it directly targets the deep-rectangle failure
mode that defines this building type.

---

## 11. Acoustics (adjacent material, not a *Studio Companion* section)

**Caveat repeated:** the *Studio Companion* does not have an acoustics section. The numbers below come
from the IBC, ASTM classifications, and general practice. Included because the brief asks and because
garage/shop-to-bedroom sound isolation is a real barndominium problem.

### The numbers

**Sound Transmission Class (STC)** rates airborne isolation of a partition; **Impact Insulation Class
(IIC)** rates footfall through a floor/ceiling.

| Assembly / condition | Target |
| --- | --- |
| IBC 1206 requirement between dwelling units | **STC 50** (STC 45 if field-tested), **IIC 50** |
| Typical interior partition, 2×4 + 1/2 in gypsum both sides | STC ~33–35 |
| Same, with insulation in the cavity | STC ~38–39 |
| Staggered or double stud, insulated | STC ~50–56 |
| Resilient channel + insulation, single stud | STC ~45–52 |
| Solid-core door | STC ~28–30 |
| Hollow-core door | STC ~20 |

**Rules of thumb:**
- **A door defeats a wall.** An STC 55 wall with a hollow-core door performs like STC ~25. Sound isolation
  is governed by the weakest path, which in a house is always a door or a duct.
- **Buffer, don't build.** Two closets, a hall, or a bathroom between a noisy space and a bedroom is worth
  more than any partition upgrade — and it is free. This is the design move a plan-level checker can
  actually verify.
- **Garage/shop to bedroom** is the worst adjacency in this building type: compressors, impact wrenches,
  and dust collectors run 85–100 dBA. Aim for **STC 55+** if the wall is unavoidable, but prefer a buffer.
- **Mechanical rooms** want isolation from bedrooms and from the great room; the equipment is the noise
  source and the duct is the flanking path.
- **Loft over living** transmits footfall; IIC matters more than STC there.

### Rule candidates — acoustics

barndsl has `BED_SOUND` (bedrooms sharing a party wall), `REC_ROOM_NOISE`, `MECH_BEDROOM`.

| Code | Sev | Predicate |
| --- | --- | --- |
| `SOUND_SHOP_BEDROOM` | **WARNING** | A `BEDROOM` shares a wall ≥ 4 ft with a `SHOP` or `GARAGE` | The worst adjacency; the existing `MIN_SOUND_BUFFER_WALL = 4.0` threshold transfers directly |
| `SOUND_SHOP_BUFFER` | **INFO** | A shop/garage wall abuts the sleeping wing with no buffer room (closet, bath, hall, storage, mechanical) between | The positive form: reward the buffer |
| `SOUND_LOFT_OVER_BED` | **INFO** | A level-1 `REC_ROOM`/`LOFT`/`GREAT_ROOM` sits over a level-0 `BEDROOM` | Footfall; IIC problem |
| `SOUND_BATH_ADJACENT` | **INFO** | A `BATHROOM` shares a wall with a `LIVING`/`GREAT_ROOM`/`DINING` with no closet buffer | Plumbing noise into the public core |
| `SOUND_MECH_BEDROOM` | — | Covered by existing `MECH_BEDROOM` | |

**Scoring:** route these through the `warnings`/`infos` buckets. `SOUND_SHOP_BEDROOM` at `WARNING` (8
points under the current contract) is a strong but defensible weight for what is genuinely the most
common livability failure in owner-designed barndominiums.

---

## 12. Designing for parking and site layout

### What the book does

The parking section gives stall and aisle dimensions, layout geometry for various parking angles, area
budgets per car, and accessible-parking requirements. For a barndominium the relevant content is the
*vehicle envelope* material — how much room a truck, a trailer, or an RV actually needs — rather than lot
design.

### Numbers

**Stall and aisle:**

| Element | Dimension |
| --- | --- |
| Standard stall, 90° | **9 × 18 ft** (9 × 20 ft generous; 8.5 × 18 ft tight) |
| Compact stall | 8 × 16 ft |
| Two-way drive aisle, 90° parking | **24 ft** |
| One-way aisle, 60° parking | 18 ft |
| One-way aisle, 45° parking | 13–14 ft |
| One-way aisle, parallel parking | 12 ft |
| Total area per car including circulation | **300–400 sq ft** (≈ 350 typical) |
| Accessible stall (car) | 8 ft + **5 ft** access aisle |
| Accessible stall (van) | 11 ft + 5 ft aisle, **or** 8 ft + 8 ft aisle |
| Accessible stall vertical clearance (van route) | **98 in** |
| Residential driveway width, single | 10–12 ft |
| Residential driveway width, double | 18–24 ft |
| Passenger car turning radius (outer) | ~19–24 ft |
| Single-unit truck / RV turning radius | ~40–45 ft |
| Backup space in front of a garage door | ≥ 24 ft, 30+ ft comfortable |

**Garage and shop bay envelopes — the numbers that matter for barndominiums:**

| Space | Minimum | Comfortable | Notes |
| --- | --- | --- | --- |
| 1-car garage | 12 × 20 ft | 14 × 24 ft | |
| 2-car garage | **20 × 20 ft** | **24 × 24 ft** | 20 ft is genuinely tight with doors open |
| 3-car garage | 30 × 20 ft | 36 × 24 ft | |
| Workshop bay (with bench wall) | 12 ft deep | **20 ft** deep | barndsl: `MIN_SHOP_DEPTH = 12.0`, `SHOP_COMFORT_DEPTH = 20.0` — well calibrated |
| Pickup + trailer bay | 14 × 40 ft | 16–20 × 45 ft | |
| **RV bay** | 14 × 40 ft | **18 × 40 ft × 14 ft tall** (the commonly quoted standard) | Class A motorhomes run 30–45 ft long, ~8.5 ft wide, 12.5–13.5 ft tall. A 14 ft bay width is workable; 16–20 ft adds cabinets or a side workspace |
| Shop with a vehicle lift | — | 14 ft clear height minimum | Two-post lift needs ~12 ft plus vehicle |

**Vehicle clearance rule of thumb:** add at least **12 in of height** and **18 in of width** beyond the
vehicle's actual dimensions for safe manoeuvring.

**Overhead door sizes:**

| Use | Width × height |
| --- | --- |
| Single car | 8 or 9 ft × 7 ft |
| Wide single / truck | 10 × 8 ft |
| Double car | 16 × 7 ft or 16 × 8 ft |
| Shop / equipment | 12 × 12 ft, 14 × 14 ft |
| **RV** | **12–14 ft tall** (14 ft for Class A with roof units) |

**The door-to-eave rule — verified, and the basis for `OVERHEAD_DOOR_HEADROOM`:** a sectional overhead
door needs about **2 ft between the top of the door and the eave** for track, springs, and hardware.
So a **16 ft sidewall carries at most a 14 ft door**; a 14 ft sidewall tops out at a 12 ft door. This is
the single cleanest geometric check in this document — it is unambiguous, it is commonly violated in
owner-drawn plans, and it is expensive to discover after the frame is up.

*Escape hatch worth mentioning in the diagnostic hint:* a **jackshaft** (wall-mounted) operator instead
of a centre-mounted trolley recovers usable headroom, and low-headroom track kits reduce the 2 ft
requirement somewhat. So the check is a `WARNING`, not an `ERROR`.

barndsl's `STD_OVERHEAD_DOOR_WIDTHS_FT = (8, 9, 10, 12, 16)` and
`STD_OVERHEAD_DOOR_HEIGHTS_FT = (7, 8, 10, 12, 14)` match standard product offerings well. The gap is
that a 14 ft door needs a taller wall than a typical barndominium eave — see the rule candidates.

**Site rules of thumb:**
- Setbacks are jurisdictional; barndsl models them (`SETBACK`, `SETBACK_NO_SITE`).
- Well-to-septic separation: commonly **50–100 ft** (barndsl: `WELL_SEPTIC_CLEAR`, `SEPTIC_SETBACK`).
- Driveway grade: ≤ 10% preferred, ≤ 15% maximum; the first 20 ft at the garage should be ≤ 5%.
- Turning apron in front of a shop: allow a **50 × 50 ft** paved area for a truck-and-trailer to swing.

### Rule candidates — parking and site

barndsl has `DRIVE_DOOR`, `APPROACH_GARAGE`, `APPROACH_ENTRY`, `SETBACK`, `SITE_OVERLAP`,
`WELL_SEPTIC_CLEAR`, `SEPTIC_SETBACK`, `GARAGE_VEHICLE_DOOR`, `SHOP_DEPTH`, `SHOP_DOOR_HEIGHT`.

| Code | Sev | Predicate | Notes |
| --- | --- | --- | --- |
| `GARAGE_BAY_TIGHT` | **WARNING** | A `GARAGE` intended for 2 cars is < 20 ft in either dimension | Extends `SHOP_DEPTH`'s reasoning to the garage type |
| `GARAGE_DEPTH_SHALLOW` | **INFO** | A `GARAGE` with an overhead door is < 22 ft deep | 20 ft fits a car; 22–24 ft fits a car *and* a person walking past it |
| `RV_BAY_LENGTH` | **INFO** | A bay with a ≥ 14 ft tall door is < 40 ft deep | If you built the clearance you probably meant an RV, and RVs are 30–45 ft long |
| `OVERHEAD_DOOR_HEADROOM` | **WARNING** | `door_height + 2.0 > block_eave_height` | **Verified rule:** a sectional door needs ~2 ft above it for track, springs, and hardware — a 14 ft door needs a **16 ft** sidewall, a 12 ft door needs 14 ft. A 14 ft door under a 14 ft eave does not fit. Common, geometric, and expensive to discover after the frame is up. `WARNING` not `ERROR` because a jackshaft operator plus low-headroom track can recover some of the 2 ft |
| `DRIVE_BACKUP_SPACE` | **INFO** | The site model shows < 24 ft of clear depth in front of an overhead door | Needs site geometry, which barndsl partially has |
| `TURNING_APRON` | **INFO** | A shop with a ≥ 12 ft door has no declared apron/paved area ≥ 40 × 40 ft | Rural barndominiums with trailers need swing room |
| `PARKING_COUNT` | **INFO** | Bedroom count ≥ 4 and total garage + shop area < 600 sq ft | Weak; probably not worth implementing |

**Scoring:** parking/site belongs in the `infos` bucket. `OVERHEAD_DOOR_HEADROOM` deserves `WARNING`
because it is a geometric impossibility rather than a preference, and it is exactly the class of error
this compiler exists to catch.

---

## 13. Height and area limitations

### What the book does

Section 7 of the 7th edition turns IBC Chapter 5 into a workable procedure: given occupancy and
construction type, look up allowable **height in feet**, allowable **stories**, and allowable **area per
story**, then apply the increases (frontage, sprinklers) and the mixed-occupancy rules.

### The mechanics

- **Table 504.3** — allowable height in feet by occupancy and construction type.
- **Table 504.4** — allowable number of stories.
- **Table 506.2** — allowable area factor (sq ft per story) by occupancy, construction type, and
  sprinkler status (columns: NS unsprinklered, S1 single-story sprinklered, SM multi-story sprinklered).
- **Frontage increase (506.3):** additional area for open perimeter, up to 75% in the common case.
- **Sprinkler increase:** built into the S1/SM columns of Table 506.2; a fully sprinklered single-story
  building gets a large multiple.

**Mixed occupancy (IBC 508) — three routes:**

1. **Accessory occupancy (508.2):** an accessory use ≤ **10%** of the story's floor area and within the
   allowable area for its own group needs no separation and does not change the classification. *This is
   almost never available to a barndominium* — the shop is far more than 10%.
2. **Nonseparated (508.3):** no fire barrier between groups, but the **whole building** is held to the
   most restrictive height/area/construction requirements of any group present.
3. **Separated (508.4):** each portion analyzed under its own group's limits, with fire barriers per
   Table 508.4, and the **sum of area ratios ≤ 1.0**.

**Table 508.4 separations relevant here:** R to S-1 is 1 hour where sprinklered (2 hours where not);
R to U is 1 hour sprinklered / 2 hours not; S-1 to U is generally 1 hour. Under the **IRC**, none of this
applies and R302.6's gypsum-board prescription substitutes.

### Applicability to barndominiums

For the overwhelming majority of barndominiums, **this entire section is moot** — the building is under
the IRC, which has no allowable-area table. It becomes live only when the plan leaves IRC scope.

### Rule candidates — height and area

| Code | Sev | Predicate | Notes |
| --- | --- | --- | --- |
| `HEIGHT_AREA_NA` | — | — | Do **not** implement allowable-area checks by default. They are wrong for an IRC dwelling and would confuse every author |
| `IBC_AREA_REVIEW` | **INFO** | Fires only when `IRC_SCOPE_EXCEEDED` also fires | "This plan is outside IRC scope; allowable height and area under IBC Tables 504/506 now govern and barndsl does not check them." An honest disclaimer beats a wrong number |
| `MIXED_OCCUPANCY_RATIO` | **INFO** | Garage + shop area > 10% of total floor area | Documents that the accessory-occupancy exemption is unavailable and the plan is a genuine mixed-occupancy building if the IBC applies. Will fire on nearly every barndominium — so make the message *informative* rather than corrective, or gate it behind an `ibc` profile |

**Recommendation:** this is the one section of the book to deliberately *not* mechanize. The honest
implementation is a single informational diagnostic that names what barndsl does not check. Encoding
IBC allowable areas would add a large table and a large false-positive surface for a case that almost
never arises.

---

## 14. What matters most for barndominiums

Ranking the material by value-per-implementation-effort for this specific building type.

### Tier 1 — implement first

**1. Dwelling/garage fire separation (IRC R302.5/R302.6).** This is the defining code issue of the
building type and the one an owner-builder is most likely to get wrong. barndsl covers the adjacency
(`GARAGE_SEPARATION`, `GARAGE_BEDROOM`) but not the door specification, the ceiling assembly under a
loft, or duct penetrations. All three are cheap to add and all three are real inspection failures.

**2. Clear-span feasibility for the shop bay.** The barndominium's whole reason for existing is the
column-free shop. A plan that assumes a 70 ft clear span without knowing it has left stock-truss
territory is making a five-figure mistake silently. `SPAN_LONG` / `SPAN_INFEASIBLE` / `SPAN_NONSTANDARD`
are high-value and use fields the `frame` directive already has.

**3. Loft floor span ≠ roof span.** The single most common structural misconception in this building
type. A 60 ft clear-span roof does not license a 60 ft clear-span floor; residential floor systems top
out around 35 ft economically. `LOFT_SPAN_LONG` catches it.

**4. Shop-to-bedroom sound and buffering.** Owner-designed plans routinely put a bedroom against the
shop wall. `SOUND_SHOP_BEDROOM` at `WARNING` with a buffer-based positive form is high-value coaching.

**5. Overhead door headroom.** A 14 ft RV door under a 14 ft eave does not fit — you need the door
height plus roughly 2 ft for track, springs, and operator. Purely geometric, purely checkable,
expensive to discover on site.

### Tier 2 — high value, more work

**6. Egress path length from the far bedroom.** Barndominiums are long thin rectangles and the far
bedroom can be 100+ ft from the front door. Not a code violation under the IRC, but a real quality
signal and a natural continuous score term (§6).

**7. Daylight depth in a deep rectangle.** A 50–60 ft deep building cannot be side-lit through its
middle. The 2–2.5 × head-height rule is the book's signature heuristic and it directly targets this
building type's characteristic failure. Proposed as a small continuous term (§10).

**8. Mechanical space for a conditioned shop.** Barndominium owners increasingly condition the shop, and
the plan rarely reserves equipment space for it. `MECH_SHOP_UNSERVED` is a simple check.

**9. Duct-run and hot-water-run distance in long plans.** Both are direct consequences of the linear
footprint and both are checkable from geometry barndsl already has.

### Tier 3 — worth documenting, low priority to implement

**10. Aging-in-place / accessibility.** Rural, owner-occupied, long-tenure, and usually single-story
already — so the marginal cost of accessibility is low and the payoff is high. Keep at `INFO`; consider
a future `aging-in-place` profile that promotes selected codes to `WARNING`.

**11. IBC occupancy and area analysis.** Almost always moot. Implement as a single honest disclaimer
(§13), not as a table.

**12. Parking geometry beyond the bay envelope.** Stall and aisle dimensions matter for commercial lots,
not for a rural homestead with a gravel apron.

### Anti-patterns — rules from the book to deliberately *not* import

| Book material | Why not |
| --- | --- |
| IBC occupant-load-driven exit counts | An IRC dwelling needs one exit door. Importing this generates pure noise |
| IBC 44 in corridor width | IRC hallways are 36 in. A 44 in check would flag nearly every compliant plan |
| IBC 7 in / 11 in stair geometry | IRC allows 7-3/4 in / 10 in. Belongs in a `strict` profile only |
| Commercial ~1.5:1 bay proportion | A post-frame bent grid is ~5:1 by design. This rule is inverted for the building type |
| Commercial mechanical space percentages (4–9%) | A residential barndominium needs 30–60 sq ft, not 4% of 3,000 |
| IBC allowable area tables | Not applicable under the IRC; large table, large false-positive surface |
| ADA compliance as a requirement | Legally inapplicable to private dwellings; correct as `INFO` coaching only |
| Elevator sizing rules of thumb | One or two stories; the residential-elevator closet-stack check is the useful residue |

---

## 15. Quick-reference tables

### 15.1 Span selection — pick a system by required span

| Required clear span | Candidate systems |
| --- | --- |
| ≤ 20 ft | Wood joists, wood rafters, wood decking, one-way concrete slab |
| 20–35 ft | Wood I-joists, parallel-chord floor trusses, glulam, steel beams, hollow-core plank |
| 30–50 ft | Pitched wood roof trusses, open-web steel joists, glulam, steel beams, double tees |
| **40–60 ft** | **Post-frame roof trusses (stock), long-span steel joists, steel rigid frames** |
| 60–80 ft | Custom trusses, steel rigid frames, long-span steel joists, steel trusses |
| > 80 ft | Steel trusses, rigid frames, space frames — engineered, not catalogue |

### 15.2 Depth-to-span ratios

| System | **L/d** | Depth for a 40 ft span | Mnemonic |
| --- | --- | --- | --- |
| Master rule (any flexural member) | **20** | 24 in | — |
| Steel joist, **roof** | **24** | 20 in | 1/2 in per ft of span |
| Steel joist, **floor** | **20** | 24 in | 0.6 in per ft of span |
| Steel wide-flange beam | 20–28 | 17–24 in | — |
| **Steel joist girder** | **12** | **40 in** | 1 in per ft of span |
| **Steel truss** | **12** | **40 in** | 1 in per ft of span |
| Plate girder | 15 | 32 in | — |
| Space frame | 12–20 | 24–40 in | — |
| Glulam | **20** | 24 in | width = depth ÷ 3 to ÷ 7 |
| Heavy wood truss | ~8 | 60 in | — |
| Parallel-chord wood floor truss | ~16 | 30 in | — |
| Wood joists | ~19–24 | (past economical range) | depth(in) ≈ span(ft) ÷ 2 + 2 |
| Hollow-core plank | **40** | 12 in | + 2 in topping |
| Precast double tee | **30** | 16 in | — |
| Precast beam | 16 | 30 in | — |
| One-way concrete slab | 22 | (past economical range) | — |
| One-way concrete joist (pan) | 18 | 27 in | — |
| Two-way flat plate | 30 | (past economical range) | — |
| Waffle slab | 24 | 20 in | — |
| Concrete beam / girder | 16 / 12 | 30 / 40 in | — |

Residential shortcut: **joist depth (in) ≈ span (ft) ÷ 2 + 2** — 2×8 → 12 ft, 2×10 → 15 ft, 2×12 → 18 ft.

### 15.3 IRC dimensional minimums (the barndsl default rule set)

| Item | Value | IRC | barndsl constant |
| --- | --- | --- | --- |
| Habitable ceiling height | 7 ft | R305.1 | `MIN_CEILING` (profile key) |
| Habitable room area | 70 sq ft | R304.1 | `MIN_USABLE_AREA` |
| Habitable room dimension | 7 ft | R304.2 | `MIN_ROOM_SHORT_SIDE` |
| Hallway width | 36 in | R311.6 | `HALL_WIDTH` (profile key) |
| Egress door clear width | 32 in | R311.2 | `MIN_EGRESS_DOOR_WIDTH` |
| Egress door clear height | 78 in | R311.2 | — |
| Interior door width | 30 in (practical) | — | `MIN_INTERIOR_DOOR_WIDTH` |
| Escape opening net clear area | 5.7 sq ft (5.0 at grade) | R310.2.1 | profile key |
| Escape opening height / width | 24 in / 20 in | R310.2.1 | profile key |
| Escape opening max sill | 44 in | R310.2.2 | profile key |
| Natural light | 8% of floor area | R303.1 | `DAYLIGHT_RATIO` |
| Natural ventilation | 4% of floor area | R303.1 | `NATURAL_VENT_RATIO` |
| Max riser | 7-3/4 in | R311.7.5.1 | profile key |
| Min tread | 10 in | R311.7.5.2 | profile key |
| Stair width | 36 in | R311.7.1 | — |
| Stair headroom | 6 ft 8 in | R311.7.2 | `STAIR_HEADROOM` |
| Max flight rise | 12 ft 7 in | R311.7.3 | `_STAIR_MAX_FLIGHT_RISE` |
| Landing depth | 36 in | R311.3 | `LANDING_MIN_DEPTH` |
| Guard height (dwelling) | 36 in | R312.1.2 | `LOFT_GUARD`, `PORCH_GUARD` |
| Garage separation, walls | 1/2 in gypsum, garage side | R302.6 | `GARAGE_SEPARATION` |
| Garage separation, ceiling under habitable | 5/8 in Type X | R302.6 | proposed |
| Garage door to dwelling | 1-3/8 in solid / 20-min, self-closing | R302.5.1 | proposed |
| Garage door to sleeping room | prohibited | R302.5.1 | `GARAGE_BEDROOM` |

### 15.4 IBC egress numbers (shop side / out-of-IRC-scope only)

| Item | Value |
| --- | --- |
| Occupant load, residential | 200 sq ft gross/occupant |
| Occupant load, business | 150 sq ft gross/occupant |
| Occupant load, storage / mechanical | 300 sq ft gross/occupant |
| Occupant load, industrial | 100 sq ft gross/occupant |
| One exit permitted up to | 49 occupants (most groups) |
| Travel distance, S-1 / R | 200 ft (250 ft sprinklered) |
| Travel distance, S-2 / U | 300 ft (400 ft sprinklered) |
| Travel distance, B | 200 ft (300 ft sprinklered) |
| Common path, B / S / U | 75 ft (100 ft sprinklered) |
| Common path, R-2 | 75 ft (125 ft sprinklered) |
| Dead-end corridor | 20 ft (50 ft sprinklered) |
| Corridor width | 44 in (36 in if occupant load < 50) |
| Door clear width | 32 in |
| Egress capacity, level | 0.2 in/occupant (0.15 sprinklered + alarm) |
| Egress capacity, stairs | 0.3 in/occupant (0.2 sprinklered + alarm) |
| Exit separation | 1/2 diagonal (1/3 sprinklered) |
| Stair riser / tread | 7 in / 11 in |
| Stair width | 44 in (36 in if occupant load < 50) |

### 15.5 Accessibility quick numbers

| Item | Value |
| --- | --- |
| Accessible route width | 36 in (32 in for ≤ 24 in length) |
| Turning circle | 60 in diameter |
| T-turn | 60 × 60 in |
| Clear floor space | 30 × 48 in |
| Door clear opening | 32 in |
| Latch-side clearance, pull, front approach | 18 in + 60 in deep |
| Latch-side clearance, push, front approach | 12 in + 48 in deep |
| Ramp slope / max rise / landing | 1:12 / 30 in / 60 in |
| Water closet clearance | 60 in wide × 56 in deep |
| Roll-in shower | 30 × 60 in |
| Kitchen, pass-through | 40 in between opposing faces |
| Kitchen, U-shaped | 60 in between opposing faces |
| Reach range | 15–48 in |
| Circulation headroom | 80 in |
| Accessible parking (car / van) | 8 + 5 ft / 11 + 5 ft |

### 15.6 Mechanical, electrical, daylight

| Item | Value |
| --- | --- |
| Furnace/air-handler closet alone | ~10–15 sq ft |
| Residential utility room (furnace + WH + softener) | **40–80 sq ft** |
| barndsl floor for a mechanical room | 30 sq ft, 5 ft min dimension (`MIN_MECH_AREA` / `MIN_MECH_DIM`) |
| Barndominium with a conditioned shop | 60–100 sq ft realistic |
| Commercial mechanical space | 4–9% of gross floor area *(moderate confidence)* |
| Total MEP space | up to 9–12% of gross floor area *(moderate confidence)* |
| Residential duct depth allowance | 12–18 in |
| Commercial plenum depth | 24–48 in |
| Cooling load rule of thumb | 1 ton per 400–600 sq ft; 400 CFM/ton |
| **Electrical panel working space (NEC 110.26)** | **30 in wide × 36 in deep × 6 ft 6 in high** — verified |
| Appliance ignition source in a garage | ≥ 18 in above the floor |
| **Daylight penetration depth** | **2–2.5 × window head height** — verified (conservative sources say 1.5×) |
| Room depth for single-sided daylight | ≤ 2–2.5 × ceiling height |
| Glazing, code minimum (IRC R303.1) | 8% of floor area |
| Glazing, daylighting target | 15–20% of floor area |
| Window-to-wall ratio ceiling | ~25–30% |
| Skylight area | **3–6%** of floor area below (sources span 1–9%) |
| South overhang projection | 0.4–0.5 × window height below the overhang |

### 15.7 Vehicle and bay envelopes

| Space | Minimum | Comfortable |
| --- | --- | --- |
| 1-car garage | 12 × 20 ft | 14 × 24 ft |
| 2-car garage | 20 × 20 ft | 24 × 24 ft |
| 3-car garage | 30 × 20 ft | 36 × 24 ft |
| Workshop bay | 12 ft deep | 20 ft deep |
| RV / trailer bay | 14 ft wide × 40 ft deep × 14 ft tall | **18 × 40 × 14 ft** |
| Shop with vehicle lift | 14 ft clear height | 16 ft |
| Vehicle manoeuvring allowance | +12 in height, +18 in width over the vehicle | — |
| Parking stall | 9 × 18 ft | 9 × 20 ft |
| Two-way drive aisle | 24 ft | — |
| Area per car with circulation | 300 sq ft | 400 sq ft |
| Overhead door, double car | 16 × 7 ft | 16 × 8 ft |
| Overhead door, RV | 12 ft tall | 14 ft tall |
| **Door top to eave** | **+2 ft** for track/springs/hardware — *verified* | +3 ft |
| **Sidewall needed for a 14 ft door** | **16 ft** | — |
| Sidewall needed for a 12 ft door | 14 ft | — |

---

## 16. Proposed diagnostic codes, consolidated

Every code proposed above, sorted by severity. New codes need a registry entry in
`src/barndsl/diagnostics.py` per `docs/MODEL_INVARIANTS.md` ("every emitted code must have a registry
entry"), a stable category, and at least one test.

### Errors (unbuildable / prohibited)

| Code | Category | Predicate summary |
| --- | --- | --- |
| `GARAGE_BEDROOM_DOOR` | access_egress | Door directly connects a garage/shop to a bedroom (IRC R302.5.1) |
| `PANEL_LOCATION` | electrical | Electrical panel in a bathroom or clothes closet (NEC 110.26) |
| `SPAN_INFEASIBLE` | structure | Frame clear span > 80 ft with no engineered declaration |
| `LOFT_SPAN_INFEASIBLE` | structure | Unsupported upper-level floor span > 60 ft |

### Warnings

| Code | Category | Predicate summary |
| --- | --- | --- |
| `GARAGE_DOOR_SPEC` | access_egress | Garage↔dwelling door lacks a rated/self-closing spec |
| `GARAGE_CEILING_SEP` | structure | Habitable room stacked over a garage/shop (5/8 in Type X ceiling) |
| `SPAN_LONG` | structure | Frame clear span 60–80 ft (past stock trusses) |
| `LOFT_SPAN_LONG` | structure | Upper-level floor span > 35 ft unsupported |
| `HEADER_SPAN_LONG` | opening | Opening wider than 16 ft |
| `OVERHEAD_DOOR_HEADROOM` | opening | Door height + 2 ft exceeds the wall/eave height |
| `PANEL_CLEARANCE` | electrical | < 36 in deep / 30 in wide clear at the panel |
| `EGRESS_SINGLE_PATH` | access_egress | All bedrooms depend on one articulation-point room |
| `EGRESS_THROUGH_GARAGE` | access_egress | The only reachable egress door is in a garage/shop |
| `SOUND_SHOP_BEDROOM` | circulation | Bedroom shares ≥ 4 ft of wall with a shop/garage |
| `GARAGE_BAY_TIGHT` | quality | Two-car garage under 20 ft in a dimension |

### Infos

| Code | Category | Predicate summary |
| --- | --- | --- |
| `GARAGE_DUCT_PENETRATION` | fixtures | Mechanical room shares a wall with the garage/shop |
| `IRC_SCOPE_EXCEEDED` | program | ≥ 3 stories or > 2 dwelling units |
| `PRIVATE_GARAGE_AREA` | program | Garage + shop > 1,000 sq ft |
| `SHOP_COMMERCIAL_HINT` | program | Shop > 2,000 sq ft or > 50% of footprint |
| `IBC_AREA_REVIEW` | program | Paired with `IRC_SCOPE_EXCEEDED`; honest non-coverage notice |
| `MIXED_OCCUPANCY_RATIO` | program | Garage + shop > 10% of floor area |
| `SPAN_UNECONOMICAL` | structure | Clear span 44–60 ft with no room needing it |
| `SPAN_NONSTANDARD` | structure | Span not near a stock truss size |
| `BAY_TIGHT` | structure | Frame bay < 6 ft o.c. |
| `BEARING_LINE_MISSING` | structure | Block exceeds `frame span` with no declared bearing wall |
| `POST_UNDERSIZED` / `POST_OVERSIZED` | structure | `frame post` vs span/bay |
| `FLOOR_DEPTH_UNSTATED` | structure | Upper level with no declared floor depth |
| `CEILING_LOFT_BUDGET` | structure | Level-0 ceiling + 2 ft > wall height with a level-1 room |
| `EGRESS_DEAD_END` | circulation | Dead-end hall run > 20 ft |
| `EGRESS_TRAVEL_BEDROOM` | access_egress | Bedroom > 75 ft of path from an egress door |
| `SHOP_SECOND_EXIT` | access_egress | Shop > 1,500 sq ft with < 2 exits |
| `SHOP_TRAVEL_DISTANCE` | access_egress | > 100–150 ft to an exit inside a shop |
| `ACCESS_DOOR_APPROACH` | access_egress | < 18 in latch-side clear on the pull side |
| `ACCESS_KITCHEN_CLEAR` | fixtures | Opposing counters closer than 40 in (60 in U-shaped) |
| `ACCESS_SHOWER` | fixtures | No 30 × 60 roll-in shower area in the primary bath |
| `ACCESS_BEDROOM_TURN` | quality | Ground-floor bedroom without a 60 in turning circle |
| `ACCESS_RAMP_SLOPE` | site | Entry grade change steeper than 1:12 |
| `MECH_AREA_RATIO` | fixtures | Mechanical + utility < 1.5% of conditioned area |
| `MECH_SHOP_UNSERVED` | fixtures | Shop > 800 sq ft with no adjoining mechanical space |
| `MECH_DUCT_RUN` | fixtures | > 60 ft from mechanical room to the farthest conditioned room |
| `WATER_HEATER_DISTANCE` | fixtures | > 30 ft to the farthest fixture group |
| `IGNITION_HEIGHT` | fixtures | Fuel-fired appliance placed in a garage/shop |
| `PLENUM_DEPTH` | structure | No depth left between ceiling and the level above |
| `DAYLIGHT_DEPTH` | access_egress | Room depth > 2.5 × window head height |
| `DAYLIGHT_SINGLE_SIDED` | quality | Room > 300 sq ft glazed on one wall only |
| `DAYLIGHT_CORE_DARK` | quality | Footprint deeper than twice the daylight reach, no toplighting |
| `SHOP_NO_LIGHT` | quality | Shop > 600 sq ft with no windows |
| `WINDOW_HEAD_LOW` | opening | Head below 6.5 ft in a room deeper than 14 ft |
| `SOUND_SHOP_BUFFER` | circulation | No buffer room between shop and sleeping wing |
| `SOUND_LOFT_OVER_BED` | circulation | Noisy upper room stacked over a bedroom |
| `SOUND_BATH_ADJACENT` | circulation | Bath against the public core with no buffer |
| `STAIR_LANDS_IN_PUBLIC` | circulation | Stair lands inside the living core |
| `STAIR_FAR_FROM_ENTRY` | circulation | Stair > 40 ft of path from the primary entry |
| `ELEVATOR_FUTURE` | access_egress | Two-level plan with no stacked-closet elevator reservation |
| `GARAGE_DEPTH_SHALLOW` | quality | Garage under 22 ft deep |
| `RV_BAY_LENGTH` | quality | 14 ft door on a bay under 40 ft deep |
| `DRIVE_BACKUP_SPACE` | site | < 24 ft of clear apron at an overhead door |
| `TURNING_APRON` | site | No 40 × 40 ft apron at a large shop door |

### Proposed score-formula changes

The existing contract is `total = clamp(100 - sum(components), 0, 100)` with diagnostic buckets capped
at 60 and five continuous terms totalling 47. Two additions are proposed, both small, both filling a
genuine gap rather than re-measuring something already counted:

| Term | Weight | Formula sketch | Justification |
| --- | --- | --- | --- |
| `egress` | 6 | `6 * clamp((worst_bedroom_path_ft - 50) / 70, 0, 1)` | Long thin footprints strand the far bedroom; nothing existing measures path length |
| `daylight_depth` | 3 | `3 * clamp(dark_habitable_fraction / 0.5, 0, 1)` where "dark" = beyond 2.5 × head height from glazed wall | The existing `daylight` term measures glazing *ratio*, not reach; deep rectangles pass the ratio and are still dark |

Optional third, lower confidence:

| Term | Weight | Formula sketch | Justification |
| --- | --- | --- | --- |
| `structure` | 4 | `4 * clamp((frame_span - widest_room_in_block) / frame_span, 0, 1)` | Rewards not paying for clear span nothing uses. Risk: interacts oddly with wing/block decomposition — prototype before committing |

Both proposed terms follow the established pattern in `score.py`: a diagnostic gives the step, the
continuous term gives the gradient, and the overlap is deliberate. Each must also return a **cause
string** naming the worst offenders with numbers, per the module's existing convention.

Adding 9 points of continuous weight moves the theoretical maximum deduction from 107 to 116. Because
the total clamps at 0 and diagnostics already dominate, this does not meaningfully change ranking
behaviour — but it does mean **existing scores will shift**, so the change is a contract change and the
docstring formula in `score.py` must be updated in the same commit.

---

## 17. Open questions and cautions

**On the numbers.**
- Span ranges above are consolidated from general engineering references, not transcribed from the book.
  Before hard-coding an endpoint as an `ERROR`, verify it against a manufacturer's load table for the
  actual product family (SJI for steel joists, the truss supplier's catalogue for post-frame).
- Every code-derived number is stated for the 2018/2021 IRC and IBC. Jurisdictions amend freely. Anything
  that is likely to be amended belongs in the jurisdiction-profile mechanism rather than as a constant —
  `docs/AUTHORING.md` describes the ~14 existing profile keys and the JSON override format.
- The IBC editions differ on the business occupant load factor (100 sq ft gross in older editions,
  150 in 2018+). If that number is ever used, cite the edition.

**On severity discipline.** The strongest recommendation in this document: **default to `INFO` for
anything derived from IBC or ADA**, because neither binds an IRC dwelling. Reserve `WARNING` for things
that are real under the IRC or are geometrically impossible, and `ERROR` for prohibitions and
unbuildable geometry. barndsl's existing `ACCESS_*` codes are all `INFO`, which is the right precedent.

**On false-positive load.** Several proposed rules (`MIXED_OCCUPANCY_RATIO`, `DAYLIGHT_TARGET`,
`PRIVATE_GARAGE_AREA`) will fire on the *majority* of valid barndominiums. Under the current score
contract that is 2 points each from the `infos` bucket, and three of them is 6 points off every plan —
a uniform shift that helps nobody. Either gate them behind an opt-in profile, make them
non-score-affecting notices, or omit them. The `infos` cap of 20 points means info inflation directly
degrades the score's discriminating power.

**On model gaps.** Several high-value rules need fields barndsl does not have:
- **Wall/eave height per footprint block** — needed for `OVERHEAD_DOOR_HEADROOM`, the single
  highest-value proposed warning. Probably the best next model addition.
- **Floor-system depth** — a `frame floor-depth <in>` option unlocks the whole floor-to-floor family.
- **Window head height** — derivable today from sill + height, but making it explicit would make
  `DAYLIGHT_DEPTH` and `WINDOW_HEAD_LOW` cleaner.
- **Door fire rating / self-closing attribute** — needed for `GARAGE_DOOR_SPEC`.
- **Electrical panel as a placed fixture** — needed for `PANEL_CLEARANCE` and `PANEL_LOCATION`.

**On what this document is not.** Not legal advice, not an engineered design, and not a substitute for
the authority having jurisdiction — the same disclaimer `docs/AUTHORING.md` attaches to the `frame`
directive and the jurisdiction profiles applies to every rule proposed here.

---

## Sources

**Primary:**
- Edward Allen and Joseph Iano, *The Architect's Studio Companion: Rules of Thumb for Preliminary Design*,
  7th ed. (Wiley, 2022), ISBN 9781119826798. Section structure verified via
  [Perlego](https://www.perlego.com/book/3526323/the-architects-studio-companion-rules-of-thumb-for-preliminary-design-pdf)
  and [Wiley](https://www.wiley.com/en-us/The+Architect's+Studio+Companion%3A+Rules+of+Thumb+for+Preliminary+Design%2C+7th+Edition-p-9781119826804).
- 6th ed. (2017), ISBN 9781119092414 — [Wiley](https://www.wiley.com/en-ie/The+Architect's+Studio+Companion:+Rules+of+Thumb+for+Preliminary+Design,+6th+Edition-p-9781119092650).

**Code:**
- [IRC R302.6 dwelling-garage fire separation](https://codes.iccsafe.org/s/IRC2018P7/chapter-3-building-planning/IRC2018P7-Pt03-Ch03-SecR302.6) and [UpCodes summary](https://up.codes/s/dwelling-garage-fire-separation)
- [ICC guidance on R302.6 and Table R302.6](https://media.iccsafe.org/news/eNews/2010v7n10/IRC302.6.pdf)
- [IBC Chapter 3, occupancy classification](https://codes.iccsafe.org/content/IBC2021P1/chapter-3-occupancy-classification-and-use)
- [IBC 506.2 allowable area](https://codes.iccsafe.org/s/IBC2021P2/chapter-5-general-building-heights-and-areas/IBC2021P2-Ch05-Sec506.2)
- [IBC Table 601 construction types](https://bahlfireproofing.com/ib-table-601-fire-resistance-ratings/)
- [IBC 508 mixed occupancy](https://www.woodworks.org/resources/mixed-use-code-strategies-part-2-nonseparated-occupancies-and-fire-limits/) and [accessory occupancies](https://www.woodworks.org/resources/mixed-use-code-strategies-part-1-incidental-uses-accessory-occupancies-and-small-spaces/)
- [IBC 406 private garages](https://up.codes/s/motor-vehicle-related-occupancies)
- [IRC R309 garages and carports](https://www.in.gov/dhs/files/R309-Garages-and-carports.pdf)

**Structure:**
- Ruddy, J. L. and Ioannides, S. A., "Rules of Thumb for Steel Design" (ASCE Structures Congress) — the
  canonical source for the steel L/d ratios used in §4, §5, and §15.2.
- Steel Joist Institute (SJI) Standard Specification — K/LH/DLH series depth and span limits.
- [Post-frame truss spans and spacing, Hansen Buildings](https://www.hansenpolebuildings.com/tag/post-frame-barndominium/)
- [Clear-span barndominium engineering and cost premiums](https://alldraft.com/clear-span-barndominium-engineering-the-real-cost-and-benefit-of-eliminating-interior-support-columns/)
- [Post-frame vs. steel-frame barndominium spans](https://alldraft.com/post%E2%80%91frame-vs-steel-frame-barndominiums-which-one-really-delivers-greater-strength/)
- [Floor trusses for barndominiums](https://www.hansenpolebuildings.com/2020/01/floor-trusses-for-barndominiums/)

**Services, daylight, and vehicles:**
- [NEC 110.26 working space](https://www.askthejourneyman.com/guides/working-space-clearances-110-26) and [depth/width/height breakdown](https://expertce.com/learn-articles/nec-working-clearance-requirements-110-26/)
- [Daylight rule of thumb — window head height to daylit zone depth](https://www.researchgate.net/publication/320100400_A_Review_of_the_Daylight_Rule_of_Thumb_Assessing_window_head_height_to_daylit_zone_depth_for_shading_devices_in_commercial_buildings)
- [Daylighting guidelines](https://www.bdcnetwork.com/13-daylighting-guidelines) and [WBDG daylighting](https://stg.wbdg.org/resources/daylighting)
- [Toplighting / skylight-to-floor ratios](http://lightingcontrolsassociation.org/2011/11/21/daylight-zones-toplighted-spaces/)
- [RV garage dimensions and the door-to-eave rule](https://www.vikingmetalgarages.com/blog/a-detailed-guide-on-rv-garage-dimensions) and [RV garage door sizing](https://megagaragehomes.com/rv-garage-dimensions-cheat-sheet/)
- [Mechanical room sizing guidance](https://engineerfix.com/how-big-should-a-mechanical-room-be/)

**Research provenance note.** Structural (§4, §5) and egress/accessibility (§6, §7) numbers were verified
against dedicated source sweeps including the 5th edition text. The MEP, daylight, and parking sections
(§8, §10, §12) were verified in a second pass after the original research thread was lost; figures that
could not be pinned to a citable source in that pass carry an inline confidence note. The commercial
mechanical-space percentages are the weakest numbers in this document — do not hard-code them.

**Repo cross-references:** `src/barndsl/validation.py` (thresholds), `src/barndsl/score.py` (score
contract), `src/barndsl/diagnostics.py` (code registry), `src/barndsl/elements.py` (model),
`docs/AUTHORING.md` (frame directive, jurisdiction profiles), `docs/MODEL_INVARIANTS.md` (diagnostic
codes are a stable API), `docs/DIAGNOSTIC_MATRIX.md` (existing 212 codes by category).
