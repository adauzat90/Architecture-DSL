# Ideas — future improvements

Parked ideas for improving barndsl, especially **agent plan-generation quality**.
Not yet scheduled; capture here so they aren't lost.

## Agent aids

### ~~Worked-example gallery (highest-leverage non-check aid)~~ — DONE
Shipped: four **verified 0/0/0** plans in `examples/gallery/` for the agent to
few-shot from — `cottage.barn` (1 bed/1 bath), `hall_spine.barn` (3 bed/2 bath
with a primary suite), `lshape.barn` (`wing`/L-footprint), and `two_story.barn`
(`stair` + `loft`). `tests/test_gallery.py` recompiles each and asserts it stays
0/0/0 (and round-trips through `emit`), so the gallery can't rot as the rules
evolve. Linked from the `barndsl-authoring` skill. Building these also surfaced
two rule gaps, now fixed: `NO_CLOSET` requiring a door-connected closet, and the
new `MASTER_ENSUITE` check.

### ~~`program N bed M bath` directive + `PROGRAM_MATCH` check~~ — DONE
Shipped: a `program <n> bed [<m> bath]` statement whose declared counts the
validator checks against the rooms placed (`PROGRAM_MISMATCH` warning). Closes the
"clean ≠ correct" gap mechanically. Since widened to a required-room list and a
minimum area — see "Widened `program` directive" below.

## Diagnostic machinery — DONE
Shipped alongside the checks below:

- **Central code registry** (`barndsl/diagnostics.py`): every code with its usual
  severity, a one-line title, and an explanation (citing the IRC clause where one
  applies). A test asserts the registry covers every code the source emits, so a
  new check can't ship without an explanation. Powers `barndsl explain <CODE>`.
- **Machine-readable diagnostics**: `CompileResult.to_dict()` / `barndsl compile
  --json` emit the diagnostics as stable JSON for the agent loop and tooling.
- `ValidationReport.summary()` now reports the **info** count too (it silently
  dropped it before).

## Checks shipped from this list
- ~~`EGRESS_SIZE`~~ — a bedroom escape opening that exists but is below the IRC
  R310 clear-opening minimums (area, width, height, sill). DONE (warning).
- ~~`STAIR_RUN`~~ — a stair footprint too short to physically climb its storey
  (R311.7 riser/tread geometry vs. ceiling height). DONE (warning; conservative —
  only fires when even a switchback wouldn't fit).
- ~~`OPENING_CLASH`~~ — two openings overlapping on the same wall span. DONE
  (error). Surfaced and fixed a latent bug in the auto-layout opening placer.

## Candidate checks shipped — DONE
Shipped as conservative `info` nudges (never block a compile):

- ~~`ROOM_TIGHT`~~ — type-aware usable minimums (kitchen ~70 sq ft, full bath
  ~35). `half_bath` is exempt; bedrooms stay covered by `BEDROOM_AREA`.
- ~~`BATH_VENT`~~ — a windowless bathroom needs mechanical ventilation (R303.3).
  The DSL can't model fans, so it's a reminder, not a hard check.
- ~~`HALL_DEADEND`~~ — a hallway opening onto ≤ 1 room isn't earning its
  footprint. Gated: a hall carrying an exterior entry (a foyer/vestibule) is
  exempt, and a hall connecting two rooms (a pass-through) doesn't fire.

## Widened `program` directive — DONE
`NO_LAUNDRY` / `NO_DINING` were too soft to emit unconditionally (an eat-in
kitchen has no dining room; a laundry is often a closet), so instead of guessing,
the `program` directive now lets the author declare the intent and the existing
`PROGRAM_MISMATCH` warning checks it:

    program 3 bed 2 bath 1 office 1 laundry area 1800

- `bed` / `bath` stay **exact** counts (catch a dropped bedroom).
- any other room type is an **at-least** requirement (a missing/short one warns;
  a surplus doesn't).
- `area <sqft>` is a minimum on the conditioned interior floor area.

The clauses round-trip through `emit_dsl` and the builder takes
`program(beds, baths, requires={...}, min_area=...)`.

## Candidate checks held for later
(none currently — the soft room-presence checks are now covered by `program`.)
