# Architecture Design Research

Source material for authoring barndsl validation rules, guidelines, and scoring
heuristics. Each document distills a canonical architecture reference into
paraphrased principles with **Rule candidates** — machine-checkable predicates
with proposed thresholds and severities (error / warning / info / score-only) —
aimed at barndominium design (residential wing + shop bays under one envelope).

All content is paraphrased; no verbatim book text or tables are reproduced.

## Documents

| Document | Primary sources | Focus |
| --- | --- | --- |
| [ching-form-space-order.md](ching-form-space-order.md) | Ching, *Architecture: Form, Space, and Order* | Spatial organization, circulation, proportion & scale, ordering principles (axis, symmetry, hierarchy, rhythm, datum) |
| [ching-architectural-graphics.md](ching-architectural-graphics.md) | Ching, *Architectural Graphics* | Plan-drawing conventions for the SVG renderer: line weights, poché, symbols, dimensioning, text sizes, sheet layout |
| [allen-iano-studio-companion.md](allen-iano-studio-companion.md) | Allen & Iano, *The Architect's Studio Companion* | Preliminary-design rules of thumb: structural spans, egress, accessibility, MEP space, daylight, parking, height/area |
| [residential-design-canon.md](residential-design-canon.md) | Alexander, *A Pattern Language*; Susanka; NKBA; IRC | Residential wing: ~33 patterns, room-size matrices, furniture clearances, kitchen/bath numbers, adjacency matrix |
| [commercial-shop-standards.md](commercial-shop-standards.md) | Neufert; Time-Saver Standards; AGS; NFBA | Shop side: vehicle/bay dimensions, overhead doors, workshop zoning, post-frame structure, mixed-use separation |

## Cross-cutting findings

Independent researchers converged on several points; these deserve attention
before individual rules are cherry-picked.

### Model gaps (prerequisites for whole rule families)

1. **Per-room or per-zone ceiling/eave height.** The plan-wide `ceiling`
   attribute blocks most clear-height rules — a barndominium wants ~9–10 ft
   living and 14–16 ft shop. Blocks the `OVERHEAD_DOOR_HEADROOM` rule
   (sectional door needs ~2 ft above the opening: a 14 ft door needs a 16 ft
   sidewall), flagged as the single best new rule on the shop side.
2. **Daylight distribution, not just quantity.** Three documents independently
   flag that glazing area is checked (IRC 8%) but distribution is not: light
   on two sides of a room (Pattern 159), single-aspect rooms, and daylight
   depth (~2–2.5× window head height) are all currently invisible to scoring.

### Scoring budget

Adding the proposed rule families naively will saturate score caps and flatten
the agent's optimization gradient:

- The ~45 delight-tier residential rules would exhaust the 20-point info cap —
  they belong in a separate continuous `livability` component.
- The three proposed shop score terms should share one bucket capped ~12 pts,
  or shop-heavy plans bottom out near 30 even when clean.
- New score terms are a contract change: `score.py`'s docstring formula must
  change in the same commit and existing scores will shift.

### Severity philosophy

- The docs use a reliability taxonomy — code minimums → dimensional standards
  → professional practice → taste — mapping onto error → warning → info →
  score-only. Keep that mapping when porting rules.
- **Do not import commercial code machinery wholesale.** IBC egress counts,
  44-inch corridors, 7/11 stairs, and the ~1.5:1 commercial bay rule would
  flag nearly every valid post-frame plan (a bent grid is ~5:1 by design).
  Default IBC/ADA-derived rules to info severity.
- Several aesthetic rules deliberately flag only the *ambiguous middle* (e.g.
  façade rhythm coefficient of variation 0.15–0.45, near-symmetry, minor datum
  jogs) rather than demanding regularity — this keeps critiques from being
  dogmatic and answers the "too subjective" deferral in `docs/IDEAS.md`.

### Renderer (from *Architectural Graphics*)

- The line-weight hierarchy is inverted: cut walls are the thinnest stroke
  (0.5 px) when they should be the heaviest; centerlines outweigh them.
  Mechanical, highest-value fix.
- Much label text sits below the 1/8-inch printed minimum (12 px at the
  renderer's effective 1:96 scale).
- Fixture catalog: dishwasher missing; toilet footprint conflates fixture with
  clearance; `OVERHEAD_DOOR_WIDTH = 9.0` is a residential door, undersized for
  shop bays.

### Constant calibration

Confirmed well-calibrated (do not change): `COMFORT_BAY = 12.0`,
`MIN_SHOP_DEPTH`/`SHOP_COMFORT_DEPTH` (12/20 ft), `DAYLIGHT_RATIO` and
`NATURAL_VENT_RATIO` (IRC R303.1). Worth revisiting: `GOOD_ASPECT = 1.6`
slightly penalizes Palladio's 5:3 (raise free band to ~1.70, bonus near
preferred ratios); `MIN_GREAT_ROOM_AREA = 200` is low for a combined
kitchen/dining/living volume (~350 sq ft functional floor);
`LOW_STORAGE_RATIO = 0.025` is a closet ratio, not bulk storage;
`MIN_MECH_AREA = 30` is a floor — ~60 sq ft is the realistic target when the
shop is conditioned.

### Named high-yield rules

If only a few rules get implemented, the researchers' top picks:
`DATUM_JOG_MINOR` (near-collinear partitions offset 0.25–2.0 ft — auto-layout
produces these constantly, humans never do), `OVERHEAD_DOOR_HEADROOM` (needs
the height model gap closed), and light-on-two-sides / `DAYLIGHT_SINGLE_ASPECT`.

## Caveats

Numbers the researchers could not pin to a citable primary source are flagged
inline in each document (e.g. commercial mechanical-space percentages,
skylight ratios, elevated-base-plane continuity thresholds). Treat inline
confidence notes as part of the data — do not hard-code a value marked
approximate.
