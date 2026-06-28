# Ideas — future improvements

Parked ideas for improving barndsl, especially **agent plan-generation quality**.
Not yet scheduled; capture here so they aren't lost.

## Agent aids

### Worked-example gallery (highest-leverage non-check aid)
Agents produce better plans by few-shotting from known-good examples than from
prose alone. Curate a small, labeled set of **verified 0/0/0** plans the agent
can copy-and-adapt:

- a 1-bed / 1-bath cottage,
- a 3-bed / 2-bath with a hallway spine,
- an L-shaped plan exercising `wing`,
- a two-story plan with a `stair` and a loft.

Each should be pinned by a test that re-compiles it and asserts it stays clean,
so the gallery can't silently rot as the rules evolve. Wire it into the
`barndsl-authoring` skill (link or inline the most representative one). This was
scoped alongside the design-quality `info` checks and deferred deliberately.

### ~~`program N bed M bath` directive + `PROGRAM_MATCH` check~~ — DONE
Shipped: a `program <n> bed [<m> bath]` statement whose declared counts the
validator checks against the rooms placed (`PROGRAM_MISMATCH` warning). Closes the
"clean ≠ correct" gap mechanically. A natural extension is to widen the directive
to more of the brief (e.g. a required room list, or min total area).

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

## Candidate checks held for later
Lower-confidence than the ones already shipped (fuzzier thresholds / higher
false-positive risk); revisit if they prove worth it:

- `NO_LAUNDRY` / `NO_DINING` — too soft / high false-positive as written; better
  folded into a widened `program` directive (a required-room list) than emitted
  unconditionally.
