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

## Candidate checks held for later
Lower-confidence than the ones already shipped (fuzzier thresholds / higher
false-positive risk); revisit if they prove worth it:

- `ROOM_TIGHT` — type-aware usable minimums (e.g. full bath ≥ ~35 sq ft, kitchen
  ≥ ~70). Needs conservative thresholds; exempt `half_bath`.
- `HALL_DEADEND` — a hallway serving ≤ 1 room isn't earning its footprint. A short
  hall to a single suite is legitimate, so this needs a careful gate.
- `NO_LAUNDRY` / `NO_DINING` — too soft / high false-positive as written; would
  need a sharper definition to be worth the noise.
