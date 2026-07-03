# Design: Site & solar — making `orientation` real

> Status: **Phases 1–3 shipped** (solar; lot/setback; approach). This is the
> implementation plan for review item #1 in
> [`../REVIEW_ARCHITECT.md`](../REVIEW_ARCHITECT.md): give the plan a *site* (a
> parcel with setbacks) and a *sun* (solar-aware glazing), and turn the
> currently-inert `orientation` directive into something the compiler reasons
> about. All three phases are in — remaining ideas (`latitude`/`hemisphere`, a
> solar term in `score.py`, overhang-credited shading) are noted in §7.
>
> **Phase 1 (shipped).** `orientation` is now `float | None` (undeclared vs. a
> declared `0`); `barndsl/solar.py` holds the pure `true_azimuth` / `solar_sector`
> / `compass_label` bridge; `_validate_solar` emits `SOLAR_WEST_GAIN` and
> `SOLAR_NORTH_ONLY` (gated on a declared orientation); the renderer draws a
> true-north compass rosette; and `metrics()` gained `true_north_azimuth` + a
> glazing-by-sector split. Covered by `tests/test_solar.py`. `SOLAR_SOUTH_UNUSED`
> was held (see §2).
>
> **Phase 2 (shipped).** A `Lot` model + `lot` / `setback` grammar (with
> `front/back/left/right` aliases); `lot_box()` (auto-centres when `at` is
> omitted) and `buildable_envelope()`; `_validate_setback` → the `SETBACK` warning
> (per short side, or "crosses the lot line"); the renderer draws the dashed lot +
> dotted buildable envelope and grows the drawing to fit the parcel; full
> emit round-trip. Covered by `tests/test_setback.py`. Deviations from the spec
> below: setbacks emit in canonical plan-relative cardinal order (aliases
> normalise); `Lot.x/y` are lazily-resolved `Optional` for order-independent
> auto-centring.
>
> **Phase 3 (shipped).** The `street <wall>` directive + `_validate_approach` →
> `APPROACH_ENTRY` (no people-door faces the street) and `APPROACH_GARAGE` (an
> overhead door faces the wall opposite the street); the deferred passive-solar
> `SOLAR_SOUTH_UNUSED` (a large south face left nearly unglazed); a `STREET` edge
> marker on the render; `Direction.opposite()`; emit round-trip. Covered by
> `tests/test_approach.py` (+ the south-unused cases in `tests/test_solar.py`).

## 0. The problem

`orientation <deg>` already parses and round-trips, but **nothing consumes it**
except the Revit exchange (it sets Project North). The attribute docstring in
`elements.py` even claims it's "used for solar/setback reasoning" — it isn't. And
there is no parcel at all: a plan floats in space with no lot lines, no setbacks,
no relationship to the street or the sun. For a barndominium — rural, big lot,
owner cares about the view and the afternoon heat — this is the single biggest
*design* gap (vs. the *code-compliance* gaps the validator already covers well).

Three capabilities, in priority order:

1. **Solar-aware glazing** (the headline — "make `orientation` real"). Given a
   declared orientation, compute the *true* compass direction each exterior wall
   faces and nudge on glazing: flag big **west** glass (afternoon overheating),
   flag a habitable room lit **only from the north** (dim, cold), and (softly)
   flag a large **south** wall left unglazed (a passive-solar opportunity missed).
2. **Lot & setbacks.** A `lot` rectangle and `setback` distances → a buildable
   envelope, and a `SETBACK` check when the footprint crosses it.
3. **Approach / street** (softest). Which lot edge fronts the street, enabling a
   nudge when the entry or the garage doors ignore it.

Everything here is deterministic — no API key, same plan in / same diagnostics
out — in the established house style.

## 1. Coordinate & orientation model (the foundation both features share)

barndsl plan coordinates are **plan-relative**: `x` east, `y` north (plan-north),
origin at the SW corner. `orientation θ` is the **true-north azimuth** (degrees
clockwise from true north) that plan-`+y` points; `θ = 0` means plan-north *is*
true north.

A wall's outward normal in true-compass azimuth is therefore:

| Plan wall | Faces (plan) | True azimuth |
|-----------|--------------|--------------|
| `north`   | `+y`         | `θ`          |
| `east`    | `+x`         | `θ + 90`     |
| `south`   | `-y`         | `θ + 180`    |
| `west`    | `-x`         | `θ + 270`    |

(all `mod 360`). This is the whole bridge between the plan frame and the sun. It
becomes one small pure helper:

```python
# geometry.py (or a new solar.py)
_WALL_BEARING = {Direction.NORTH: 0.0, Direction.EAST: 90.0,
                 Direction.SOUTH: 180.0, Direction.WEST: 270.0}

def true_azimuth(wall: Direction, orientation: float) -> float:
    """Compass azimuth (deg CW from true north) that `wall`'s outward face points."""
    return (orientation + _WALL_BEARING[wall]) % 360.0

def solar_sector(azimuth: float) -> str:
    """'south' | 'east' | 'west' | 'north' — the sun-exposure class of a bearing."""
    ...
```

**Solar sectors** (northern-hemisphere; see §5 assumptions), by principled arcs
rather than a naive nearest-cardinal so the "hot afternoon quadrant" is caught:

| Sector | Azimuth arc | Character |
|--------|-------------|-----------|
| `south` | 135°–225° | Controlled winter gain; easy to shade with an overhang. **Good.** |
| `west`  | 225°–300° | Low afternoon sun (SW→W); hard to shade. **Overheats.** |
| `east`  | 60°–135°  | Morning sun; generally benign. **Neutral.** |
| `north` | 300°–360°, 0°–60° | Little direct sun; heat loss. **Dim/cold.** |

Arc boundaries live in `constants.py` as tunables.

### 1.1 One model change: `orientation` becomes `Optional`

Today `orientation: float = 0.0`, so "declared north-up" is indistinguishable
from "no orientation given." The solar checks must **only run when the author
actually declared an orientation** (otherwise we'd impose a true-north assumption
on every abstract plan and nag the 0/0/0 gallery — the same opt-in discipline as
`accessible`/`electrical`). So:

- Change the field to `orientation: float | None = None`.
- `orient(deg)` stores a real value (including `0`, which now means "declared,
  north-up" and is distinct from `None`).
- **Migration (small, contained):** `revit.py` reads it as `orientation or 0.0`
  when setting Project North; `emit.py` emits `orientation N` when `is not None`
  (so an explicit `orientation 0` now round-trips, which is correct); any
  arithmetic reader coalesces `None → 0.0`. `grep` shows the only consumers are
  `revit.py`, `emit.py`, and the (soon) solar check — a ~5-line migration.

## 2. Solar-aware glazing (Phase 1 — the headline)

Gate: **runs only when `plan.orientation is not None`.** All `info` — guidance,
never blocking, exactly like the design-quality nudges.

New check `_validate_solar(plan, add)`:

For each habitable room (`HABITABLE_TYPES`), bucket its windows by the true sector
of the wall they sit on (`true_azimuth(win.wall, θ)` → `solar_sector`), summing
`win.glazed_area` per sector. Then:

| Code | Sev | Fires when | Hint |
|------|-----|-----------|------|
| `SOLAR_WEST_GAIN` | info | A room's **west-sector** glazed area exceeds `SOLAR_WEST_MAX_GLAZING` (default ~24 sq ft) | Shade it with a deep overhang/porch or an awning, add a west-side tree line, or move glass to the south face. |
| `SOLAR_NORTH_ONLY` | info | A room has glazing, **all** of it north-sector, **and** it has a non-north exterior wall it could use instead | This room is lit only from the north (dim, cold in winter); add a south/east window on `<wall>`. |
| `SOLAR_SOUTH_UNUSED` | info (optional / Phase 1.5) | The plan has a large **south-sector** exterior wall (> `SOLAR_SOUTH_MIN_WALL`) but its south glazing ratio is very low | A south wall is the best passive-solar face — add south glazing (with an overhang to block summer sun). |

Notes that keep it honest and quiet:

- `SOLAR_WEST_GAIN` keys off **absolute** west glazed area per room (a big west
  window), not a ratio, so a tiny west window in a bath never trips it.
- `SOLAR_NORTH_ONLY` requires an *alternative* exterior wall (`exterior_walls`)
  so we never tell a room with only a north wall to do the impossible.
- `SOLAR_SOUTH_UNUSED` is the one that risks over-nudging; it ships behind a
  conservative wall-size floor and can be held to Phase 1.5 if it's noisy on the
  corpus. The overhang it recommends is exactly the geometry review item #3 adds,
  so the two features reinforce.

The `overhang`/porch-roof geometry (review item #3) will later let these checks
*credit* a shaded west/south window instead of flagging it — designed for that
seam now, implemented when overhangs land.

## 3. Lot & setbacks (Phase 2)

### 3.1 Grammar

```
lot <W> x <L> [at <x>,<y>]     # the parcel rectangle, in plan coordinates
setback <side> <ft> [<side> <ft> ...]   # side ∈ south|north|east|west
                                        #   (aliases front=south, back=north,
                                        #    left=west, right=east)
```

- The lot is **another rectangle in the same plan coordinate frame** — so all
  existing geometry, checks and rendering are untouched; the lot just floats
  around the building. `at <x>,<y>` places the lot's SW corner (typically
  negative, so the building at origin sits inside): e.g. `lot 200 x 300 at
  -70,-40`. Default `at` centres the footprint in the lot.
- Setbacks are **plan-relative** cardinals (unambiguous, matching the rest of the
  DSL), with `front/back/left/right` accepted as readable aliases. Distances that
  matter for zoning are per-edge; the buildable envelope is the lot inset by each
  side's setback.

### 3.2 Check `_validate_setback(plan, add)` — gate: `plan.lot is not None`

Compute the footprint bounding box (`min/max x,y` over `footprint_sections()`;
conservative for an L/T/U — per-edge exactness is a later refinement). For each
side, the gap between the footprint and the lot edge must be ≥ that side's
setback:

```
south:  foot_min_y - lot_min_y  >=  setback_south
north:  lot_max_y - foot_max_y  >=  setback_north
west:   foot_min_x - lot_min_x  >=  setback_west
east:   lot_max_x - foot_max_x  >=  setback_east
```

| Code | Sev | Fires when |
|------|-----|-----------|
| `SETBACK` | **warning** | Any side's gap is less than its required setback (message names the side, the required distance, and the actual gap; the extreme case — footprint entirely outside the lot — is the same code with a "past the lot line" message). |

**Severity = warning, not error**, deliberately: zoning is real but this tool is
explicitly "not a substitute for the authority having jurisdiction," the lot data
is optional and approximate, and a warning still lets the plan compile and render.
This matches `EGRESS_DOOR`/`NAT_LIGHT` (a likely problem, flagged, not a gate).

### 3.3 A note on front/street

`front = south` by default is a convention, not a fact about the site. Phase 3
adds an explicit `street <wall>` so "front" binds to the real street edge and
unlocks the approach nudges (§4). Until then `front/back/left/right` are pure
plan-relative aliases and documented as such.

## 4. Approach / street (Phase 3 — optional)

```
street <wall>     # which plan wall of the lot faces the street/approach
```

Enables soft `info` nudges (all gated on `street` being declared):

- `APPROACH_ENTRY` — the main people-`entry` doesn't face or return to the street
  side (a front door around the back reads wrong).
- `APPROACH_GARAGE` — overhead/garage doors face *away* from the approach (you'd
  have to drive around the house).

These are genuinely softer/subjective, hence last and optional.

## 5. Assumptions & decisions (called out on purpose)

- **Northern hemisphere.** South is the good solar face. Stated loudly in the
  docstring and the `explain` text. A future `latitude <deg>` / `hemisphere`
  input flips the sectors and could scale the west/south thresholds by sun angle;
  out of scope here, but the `solar_sector` seam is where it plugs in.
- **Solar runs only on a declared `orientation`;** setbacks only on a declared
  `lot`; approach only on a declared `street`. No feature imposes a site
  assumption on a plan that didn't ask — clean plans (and the gallery) stay
  0/0/0.
- **Bounding-box setbacks in v1.** Exact per-edge setbacks for a re-entrant L/T/U
  corner are a refinement; the bbox is conservative (never *under*-reports a
  violation).
- **`SETBACK` is a warning, `SOLAR_*` are info.** Consistent with the tool's
  advisory posture.

## 6. Surfaces touched (implementation checklist)

| Layer | Change |
|-------|--------|
| `elements.py` | `orientation → float\|None`; add `Lot` dataclass + `lot`, `setbacks`, `street` fields; builder methods `.lot()`, `.setback()`, `.street()`; `buildable_envelope()` helper |
| `geometry.py` (or new `solar.py`) | `true_azimuth`, `solar_sector`, footprint bbox helper |
| `constants.py` | sector arcs, `SOLAR_WEST_MAX_GLAZING`, `SOLAR_SOUTH_MIN_WALL` |
| `compiler.py` | parse `lot` / `setback` / `street`; add to `_KEYWORDS` and `DSL_REFERENCE`; `orientation` now stores a real (possibly 0) value |
| `validation.py` | `_validate_solar`, `_validate_setback`, (`_validate_approach`); register in `validate()` |
| `diagnostics.py` | register `SETBACK`, `SOLAR_WEST_GAIN`, `SOLAR_NORTH_ONLY`, `SOLAR_SOUTH_UNUSED`, (`APPROACH_*`) |
| `emit.py` | round-trip `lot`, `setback`, `street`; emit `orientation` when `is not None` |
| `render.py` | **north arrow / compass** rosette (rotate by `orientation`; label true N/E/S/W); optional sun glyph on the south; **lot boundary** (dashed) + **setback lines** (dotted) + buildable envelope when a lot is present; extend world-bounds to include the lot |
| `revit.py` | read `orientation or 0.0` (migration); (lot → a Revit property line / site later) |
| `elements.metrics()` | add `true_north_azimuth` and a south/east/west/north glazing split (feeds a future energy pass and a possible score term) |

The **north arrow is the one must-have renderer piece** — the moment a plan
declares `orientation 30`, the drawing should *show* true north rotated, or the
number is still invisible. The lot/setback lines follow with Phase 2.

## 7. Phasing

1. **Phase 1 — solar (the "make it real" core).** `orientation → Optional`;
   `true_azimuth`/`solar_sector`; `_validate_solar` (`SOLAR_WEST_GAIN`,
   `SOLAR_NORTH_ONLY`); the **north-arrow** renderer; `metrics` glazing split.
   Smallest footprint, no placement questions, highest "aha." (`SOLAR_SOUTH_UNUSED`
   is Phase 1.5 pending corpus noise.)
2. **Phase 2 — lot & setbacks.** `lot`/`setback` grammar + `Lot` model +
   `_validate_setback` + the lot/setback render layer + round-trip.
3. **Phase 3 — approach.** `street` + `APPROACH_*` nudges; and the passive-solar
   `SOLAR_SOUTH_UNUSED` if deferred. Later: `latitude`/`hemisphere`, a solar term
   in `score.py`, overhang-credited shading (with review item #3).

## 8. Test plan

- **Azimuth math** (unit): each wall at `θ = 0, 45, 90, 270`; sector boundaries
  at 135/225/300/60.
- **Solar** fires: a big west window in a living room (`SOLAR_WEST_GAIN`); a
  bedroom windowed only to the north with a free south wall (`SOLAR_NORTH_ONLY`).
  Silent: same plans with `orientation` **undeclared**; a shaded/relocated glass.
  Orientation sensitivity: a plan that's clean at `θ=0` trips `SOLAR_WEST_GAIN`
  when rotated so the big window faces true west.
- **Setback** fits / violates each side; footprint fully outside; L/T/U bbox.
- **Round-trip**: `lot`/`setback`/`street`/`orientation 0` survive `emit_dsl` and
  recompile; builder methods set the fields.
- **Renderer** smoke: SVG contains the compass group and (with a lot) the lot
  rectangle.
- **Gallery** stays 0/0/0 (no plan declares orientation/lot, so all site/solar
  checks are dormant) — the regression guard that this feature can't nag.
- **Registry** coverage test picks up every new code (existing meta-test).

## 9. Worked example (target UX)

```barn
plan "Ridge House"
envelope 60 x 40
ceiling 12
orientation 30                 # plan-north points 30° E of true north
lot 200 x 300 at -70,-130      # the parcel; building sits inside
setback front 40 back 25 left 15 right 15
room great: living at 0,0 size 30 x 26
window great west width 8 offset 8   # a big window that, rotated 30°, faces WNW
entry great south width 3 offset 14
```

```text
$ barndsl compile ridge.barn
COMPILE OK — 0 error(s), 1 warning(s), 1 info(s)
ridge.barn: warning[SETBACK] (great): The building is 8 ft from the north lot
    line but the back setback is 25 ft — it encroaches 17 ft.
    hint: Move the footprint south, shrink it, or reduce the back setback.
ridge.barn:8: info[SOLAR_WEST_GAIN] (great): 'great' has 40 sq ft of glazing
    facing WNW (285° true) — low afternoon sun that's hard to shade and overheats
    the room.
    hint: Shade it with a deep overhang/porch, or move the glass to the south face.
```

…and the rendered SVG shows a north arrow rotated 30°, the dashed lot with dotted
setback lines, and the footprint sitting inside the buildable envelope.
