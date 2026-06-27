# Design: Auto-layout 2.0 — a space-filling, adjacency-true layout engine

> Status: **proposed** (design + research). No code yet. This document is the
> plan; implementation is phased and gated on the decisions in §11.

## 1. Why

`barndsl layout` (v1, `src/barndsl/layout.py`) turns an adjacency brief into a
plan by **greedy abutment**: seed with the largest room, then repeatedly place
the room with the most already-placed neighbours flush against one of them. It
reliably produces a *valid, connected* plan and satisfies the requested
adjacencies, but it has three structural weaknesses, all measured on real
briefs during v1 development:

| Weakness | Cause | Symptom |
|---|---|---|
| **Wasted footprint** | abutment packs a blob with interior holes; the envelope is the bounding box | `AREA_UNUSED` info at 50–80% unused on denser briefs |
| **Buried habitable rooms** | a room placed early gets surrounded as the cluster grows | bedrooms with no exterior wall → `BEDROOM_EGRESS` *error*; living rooms with no daylight → `NAT_LIGHT` warning |
| **Elongated aspect** | the seed sits central and growth is one-directional | 38×64, 34×78 footprints |

These are not tuning bugs — we tried (seed-by-area vs seed-by-degree,
exposure-vs-compactness scoring, an NE-growth bias, an egress repair pass; see
the v1 commit history). They are inherent to greedy abutment: it solves
*adjacency*, not *space-filling dissection*. Getting a hole-free dissection of a
fixed rectangle with rooms on the perimeter **by construction** requires a
different class of algorithm.

## 2. Goals / non-goals

**Goals**

1. **Tile the envelope** — partition a rectangle into rooms with no gaps and no
   overlaps (kills `AREA_UNUSED`, fixes aspect).
2. **Adjacency as shared walls** — every requested adjacency becomes a real
   shared wall (so the auto-added `door` always resolves), not mere proximity.
3. **Perimeter by construction** — rooms that need daylight/egress (bedrooms,
   habitable rooms) land on the building boundary, not the interior.
4. **Dimension to a program** — honor each room's target area / min dimension /
   aspect-ratio, fitting a given envelope (or sizing one).
5. **Deterministic & pure-Python** — same brief → same plan; no heavy runtime
   deps; integrates with the existing IR, validator, emitter, renderer.

**Non-goals**

- Multi-storey auto-layout (v2 targets one level; stairs/levels stay manual).
- Optimal layouts. We want *good and valid*, refined in the compile-fix loop —
  the project's thesis. "Best" floor-planning is NP-hard (§5).
- Replacing v1. v2 is a new engine; v1 greedy remains the universal fallback.

## 3. The objective is the compiler

The layout engine should target *exactly* what `validate()` checks, so its
output compiles clean. Distilled from `src/barndsl/validation.py`:

**Hard (errors) — must hold by construction:**
- Rooms within `[0,W]×[0,L]`; no overlap (same level).
- Bedrooms: area ≥ 70 sq ft, min side ≥ 7 ft, **egress window on an exterior wall**.
- Every room reachable from an `entry` via interior doors.
- Openings fit their wall; finite, positive geometry.

**Soft (warnings) — minimise:**
- 8 % glazing for habitable rooms (needs an exterior wall).
- Hallway ≥ 3 ft; door widths; a bathroom exists.

**Quality (info):** kitchen-flow, bed-privacy, bath-distance, `AREA_UNUSED`.

The three goals in §2 (tile, shared-wall adjacency, perimeter) map directly onto
the hard constraints that v1 violates. That correspondence is the whole point:
**make the geometry correct by construction instead of repairing it afterward.**

## 4. The shape of the problem

Inputs (extend today's `LayoutBrief`):
- Rooms with a **type** and a **size program**: fixed `w×l`, or a target area +
  min-dimension + aspect range (flexible).
- A **required-adjacency graph** (rooms that must share a wall).
- An **envelope** (fixed `W×L`) or "size to fit".
- Per-room flags derivable from type: *needs-exterior-wall* (bedrooms,
  habitable), *is-circulation* (hall — may be interior).

Output: absolute room rectangles (`Room.x/y/width/length`) that tile the
envelope, honor the adjacencies as shared walls, and put exterior-needing rooms
on the boundary — emitted straight into the existing IR, then doors/openings are
added exactly as v1 already does (`_connect_adjacencies`, `_add_openings`).

This is, precisely, the **rectangular floor-plan / rectangular-dual problem**
from VLSI-CAD and computational geometry.

## 5. Research: the algorithm families

Deep-research survey (25 claims, each verified 3-0 adversarially; full report in
the session transcript). Summary of the families and their fit:

| Family | Adjacency = shared wall? | No-overlap + envelope fill? | Deterministic? | Pure-Python / deps | Verdict |
|---|---|---|---|---|---|
| **Rectangular dual graphs** | **Yes, natively** | **Yes, by construction** | **Yes** (polynomial) | Yes (longest-path sizing; no LP solver needed for feasible dimensioning) | **Core engine** |
| Slicing trees / Polish expr. | Partial (siblings only; not every adjacency graph is sliceable) | Yes | Yes | Yes | **Pragmatic MVP** core / fallback |
| Non-slicing VLSI (sequence-pair, B*-tree, O-tree) + simulated annealing | Via cost term, not guaranteed | Yes | **No** (stochastic SA) | Yes but heuristic | Rejected (non-deterministic) |
| Constraint / ILP / SMT (Z3, PuLP/CBC) | Declarative (separation + adjacency constraints) | Yes | Yes (given solver) | **Solver dependency**; NP-hard, scales poorly past ~20 rooms, slow UNSAT | Optional fallback only |
| Metaheuristic / physics / GA | Cost term | Often grid-cell, not clean rects | **No** | Heavy (Grasshopper/Wallacei) | Rejected |
| ML generative (House-GAN, Graph2Plan, HouseDiffusion) | Conditioned on graph | **Several don't even guarantee non-overlap** | **No** | PyTorch + 80–117 K-plan corpora | Rejected (overkill, non-deterministic) |
| Generic packers (`rectpack`) | **No** (containment only) | Yes | Yes | Pure-Python | Only for free-space infill |

### Why rectangular duals

A **rectangular dual** of a planar graph *G* partitions a rectangle into
sub-rectangles, one per vertex, such that two rectangles **share a wall segment
iff their vertices are adjacent in *G***. That is exactly our requirement:
vertices = rooms, required-adjacency edges = shared walls, the partition tiles
the envelope with no overlap. It is the only surveyed family that delivers all
three of §2's structural goals *by construction*, deterministically, in
polynomial time. [GPLAN, arXiv:2008.01803]

**Existence gate (the catch).** A rectangular dual exists **iff** *G* is a
*Properly Triangulated Planar Graph* (PTPG — planar, internally triangulated, no
separating triangles) with **at most four corner-implying-paths** (the
Kozmiński–Kinnen condition). The four CIPs correspond to the four corners/sides
of the bounding rectangle. A graph needing more than four corners can't fit a
rectangular boundary. [arXiv:2008.01803; Kozmiński & Kinnen 1985; arXiv:2102.05304]

**Construction** is classical and deterministic: Bhasker & Sahni give a **linear-time**
rectangular-dual construction; Kozmiński–Kinnen an O(n²). The modern recipe
(GPLAN): build a **Regular Edge Labeling (REL)** that splits the inner edges
into a horizontal set and a vertical set, yielding two directed *st*-graphs
(`Gh`, `Gv`); the rooms' coordinates come from numbering those graphs.

**Dimensioning** is a *separate, tractable* step over the *st*-graphs and is
where fixed/flexible sizes, min-area and aspect-ratio live — see §7.2. GPLAN
does it with iterative linear optimisation; crucially, a **feasible minimum-size
dimensioning needs no LP solver at all** (longest-path on a difference-constraint
DAG — pure Python, §7.2). [arXiv:2008.01803; Yeap & Sarrafzadeh, SIAM J. Discrete
Math. 8(2) 1995]

**Perimeter / daylight.** Add four virtual **exterior vertices** (N/E/S/W). A
room that must be on an outer wall gets an edge to the relevant exterior vertex;
in the dual it is then forced against that boundary. This reuses the same
machinery that turns the four CIPs into the four sides — so "bedroom on the
perimeter" becomes a graph edge, directly fixing v1's buried-bedroom error.
*(Medium-confidence design inference from the dual's corner/boundary structure;
to be validated in a Phase-2 spike.)*

**Graceful fallback.** If *G* has > 4 CIPs (no rectangular dual), the theory
extends deterministically to **L-shaped / rectilinear** envelopes in O(n²)
[arXiv:2205.14434]; GPLAN itself falls back to an orthogonal layout. For a
barndominium (almost always a plain rectangle) we instead fall back to **v1
greedy**, which always returns *something* valid-ish, and report why.

### Why not the others (briefly)

- **Constraint/ILP/SMT**: clean declarative no-overlap + adjacency, but NP-hard,
  "remains extremely challenging" even with mature MIP formulations
  [arXiv:1602.07760], Z3 layout "gets increasingly difficult … as rooms increase"
  with very slow infeasibility proofs [ACM 10.1145/3402942.3409603], and it drags
  in a solver. Good as an *optional* exact fallback, not the core.
- **Metaheuristic / ML**: non-deterministic and/or heavyweight; some ML methods
  don't even guarantee non-overlap (Graph2Plan avg overlap 112.4 vs 95.3 GT,
  needs Matlab to align to the boundary) [arXiv:2004.13204]. Disqualified by the
  determinism + dependency-light requirements.

## 6. Recommended approach: a phased hybrid

A full general rectangular-dual engine (REL construction + planar embedding +
CIP analysis + graph augmentation + retry-on-infeasible-sizing) is research-grade
— weeks of work with real "doesn't finish cleanly" risk. Barndominiums rarely
*need* arbitrary adjacency topologies (they're long rectangles: open core + a
hall spine + a row of rooms). So we phase by ROI, sharing one dimensioner:

- **Phase 1 — Dimensioned slicing core (the high-ROI win).** Build a **slicing
  tree** for the program and dimension it to fill the envelope with rooms on the
  perimeter. Pure-Python, deterministic, tractable. Fixes all three v1
  weaknesses for the common case. Honors adjacency where the slicing structure
  allows; reports the rest. *This is what most briefs actually need.*

- **Phase 2 — Rectangular-dual topology (the general engine).** Replace the
  Phase-1 tree-builder with a true REL-based rectangular-dual constructor so
  *arbitrary* PTPG adjacency graphs become shared-wall layouts, with the
  exterior-vertex perimeter encoding and the CIP existence check. **Reuses the
  Phase-1 *st*-graph dimensioner unchanged.**

- **Always — v1 greedy fallback.** When the graph admits neither a sliceable nor
  a rectangular dual (or dimensioning is infeasible under the size program), fall
  back to v1 and surface a diagnostic. The engine never hard-fails.

This sequencing de-risks: Phase 1 ships visible value fast; the expensive,
uncertain topology work (Phase 2) layers on without throwing away the
dimensioner, the API, or the tests.

## 7. Architecture

### 7.1 Module layout & data flow

New package `src/barndsl/layout2/` (keep v1 `layout.py` intact and selectable):

```
layout2/
  brief.py        # extended brief: SizeProgram (fixed|flex), required adjacency,
                  #   perimeter requirements (derived from RoomType)
  topology.py     # adjacency graph + envelope → a "floorplan topology":
                  #   Phase 1: slicing-tree builder
                  #   Phase 2: rectangular-dual (REL) builder
                  #   both emit the SAME intermediate: two st-graphs (Gh, Gv)
  stgraph.py      # the st-graph intermediate + longest-path dimensioner
  dimension.py    # SizeProgram + st-graphs → absolute wall coordinates
  solve.py        # orchestration: try slicing → (Phase 2) dual → v1 fallback;
                  #   build Barndominium, reuse v1 _connect_adjacencies/_add_openings
  __init__.py     # solve_layout2(brief) -> LayoutResult (same result type as v1)
```

Data flow:

```
LayoutBrief2 ──topology.py──▶ (Gh, Gv) st-graphs ──dimension.py──▶ wall coords
     │                              ▲                                   │
 (rooms, sizes,         one intermediate, two                    absolute Room
  adjacency,            front-ends (slice/dual)                   rectangles
  envelope)                                                            │
                                                          reuse v1: doors, entry,
                                                          windows, LayoutResult
```

Reuse, don't reinvent: `geometry.shared_edge` (verify adjacencies), `validation`
(the objective + post-check), `emit_dsl`/`render` (output), and v1's
`_connect_adjacencies` / `_add_openings` (doors + openings are already solved).
The result type stays `LayoutResult` so the CLI and callers don't change.

### 7.2 The shared core: *st*-graph dimensioning (no LP solver)

Both front-ends reduce to two **constraint DAGs**:

- A *vertical* cut line has an x-coordinate; `Gv` orders them left→right. For each
  room between left wall `a` and right wall `b`: `x[b] − x[a] ≥ wmin(room)`.
- Symmetrically `Gh` orders horizontal cut lines bottom→top with
  `y[d] − y[c] ≥ hmin(room)`.

A system of difference constraints `x[b] − x[a] ≥ c` is solved by **longest path**
from the source wall in the DAG (topological order, O(V+E)) — textbook
(CLRS difference constraints), **pure Python, no solver**. That yields the
*minimum-feasible* envelope and coordinates. To hit a *target* envelope or
optimise proportions/aspect, distribute the slack proportionally to each room's
target area (a closed-form pass), or — only if we later want provably optimal
proportions — hand the same constraints to `scipy.optimize.linprog`/PuLP behind a
flag. **Default path needs neither.**

Min-width/height per room come from the SizeProgram: fixed → `w,l`; flexible →
from target area, min dimension and aspect bounds (`wmin = max(min_dim,
sqrt(area/aspect_max))`, etc.).

### 7.3 Phase 1 topology: slicing tree from the program

1. **Cluster** rooms into a shallow band structure using the adjacency graph and
   types: a *public* cluster (living/kitchen/dining, mutually adjacent), a
   *circulation* spine (hall), a *private* cluster (bedrooms/baths off the hall).
   This is barndsl's documented idiom, now computed.
2. Emit a **slicing tree**: e.g. `V(public_core, H(hall, private_row))` — cuts
   chosen so that (a) each required adjacency that the tree *can* honor becomes a
   sibling relationship sharing the cut, and (b) perimeter-needing leaves land on
   an outer edge of the tree (outer leaves touch the envelope boundary).
3. Convert the tree to `(Gh, Gv)` and dimension (§7.2).
4. **Verify** every requested adjacency with `shared_edge`; record `unsatisfied`.

Phase 1 honors adjacencies expressible as slicing siblings (the common case) and
*guarantees* fill + no-overlap + outer leaves on the perimeter.

### 7.4 Phase 2 topology: rectangular dual

1. **Build & validate the graph**: required adjacencies + 4 exterior vertices
   (perimeter edges for daylight/egress rooms). Check planarity, proper
   triangulation, ≤ 4 CIPs. If it fails, **augment** (add the fewest extra
   adjacencies to triangulate) or fall back.
2. **REL → st-graphs**: construct a Regular Edge Labeling (Bhasker–Sahni linear,
   or Kozmiński–Kinnen O(n²)); split into `Gh`, `Gv`.
3. **Dimension** with the *same* §7.2 code. On infeasible sizing, try an
   alternate REL/topology (GPLAN generates several for this reason); after K
   tries, fall back.
4. Verify + report, as Phase 1.

## 8. Dependency policy

- **Phase 1 & default Phase 2: pure-Python, zero new runtime deps.** Longest-path
  dimensioning and slicing/REL construction are all elementary graph code.
- **Optional extra** (behind a flag / extras group, never required): SciPy
  `linprog` or PuLP for provably-optimal proportional dimensioning; a tiny CSP/SMT
  fallback for exotic graphs. Documented as opt-in; the library works without them.

This preserves barndsl's "compiler + renderer, no extra deps" baseline.

## 9. Public API & integration

```python
from barndsl import solve_layout2   # new; v1 solve_layout stays

out = solve_layout2(LayoutBrief2(
    name="...",
    rooms=[RoomSpec2("living", "living", area=360, min_dim=12),
           RoomSpec2("bed1", "bedroom", width=12, length=12)],  # flex or fixed
    adjacencies=[("living", "hall"), ("hall", "bed1")],
    envelope=(60, 40),               # or None → size to fit
))                                    # -> LayoutResult (unchanged type)
```

- `LayoutBrief2`/`RoomSpec2` extend v1 with a **SizeProgram** (fixed `w×l` *or*
  `area`+`min_dim`+`aspect`). v1's `LayoutBrief` stays for back-compat; a shim can
  lift a v1 brief into a v2 one (fixed sizes).
- CLI: `barndsl layout FILE --engine dual|slice|greedy` (default `slice`,
  i.e. Phase 1; `greedy` = today's v1). Same flags otherwise.
- The brief text format gains optional `area`/`aspect` forms for rooms.
- Output path identical to v1 (`LayoutResult`, doors/openings, emit, render).

## 10. Testing & validation strategy

- **Property tests** (the heart): for a corpus of briefs, assert the solved plan
  has **no overlaps**, **tiles within the envelope**, every requested adjacency
  shares a wall (or is reported `unsatisfied`), and every perimeter-needing room
  has an exterior wall — then assert `compile_source(emit_dsl(plan))` has **zero
  errors**. This directly encodes §3.
- **Determinism**: same brief → byte-identical `emit_dsl`.
- **Dimensioner units**: difference-constraint solver tested against hand-computed
  longest paths; area/aspect math checked.
- **Phase 2 existence**: graphs with > 4 CIPs detected and routed to fallback;
  known PTPGs produce the expected topology.
- **Regression**: a measured comparison vs v1 on the §1 brief corpus
  (`AREA_UNUSED %`, buried-room count, error count) — v2 must dominate.
- **No new deps in the default path**: a test importing the engine with optional
  extras absent.

## 11. Risks & decisions for the user

**Risks**
- *Phase 2 is research-grade.* REL construction, planar embedding, CIP analysis
  and graph augmentation are intricate; a from-scratch pure-Python implementation
  is the biggest uncertainty. **Mitigation:** Phase 1 delivers the user-visible
  win without it; Phase 2 is isolated behind the `(Gh,Gv)` interface and the
  greedy fallback, so partial completion still ships.
- *Sizing feasibility.* A topology may admit no dimensioning meeting all fixed
  sizes; flexible sizes + retry + fallback mitigate, but a brief over-constrained
  on exact sizes can still fail — reported, not crashed.
- *Perimeter encoding* is a design inference (§5); a Phase-2 spike validates it
  before committing.

**Decisions (gate before coding):**
1. **Scope / sequencing** — Phase 1 first (recommended), full rectangular-dual
   up front, or just harden v1 greedy?
2. **Dependency policy** — pure-Python only (recommended), or allow an optional
   SciPy/PuLP path for optimal dimensioning?

## 12. References

Verified sources (deep-research, 3-0 each):

- GPLAN — graph-driven dimensioned rectangular floorplans via rectangular
  dualization + st-graph linear optimisation. arXiv:2008.01803.
- Existence of rectangular duals / PTPG + four-CIP (Kozmiński–Kinnen);
  "A Theory of Rectangularly Dualizable Graphs", arXiv:2102.05304.
- Yeap & Sarrafzadeh — sliceable floorplan from an adjacency graph in
  O(n log n + hn). SIAM J. Discrete Math. 8(2), 1995.
- L-shaped extension of rectangular-dual theory, O(n²). arXiv:2205.14434.
- Strong MIP formulations for the Floor Layout Problem (NP-hardness/scaling).
  arXiv:1602.07760.
- SMT/Z3 procedural layout (scaling/UNSAT cost). ACM 10.1145/3402942.3409603.
- House-GAN (arXiv:2003.06988), Graph2Plan (arXiv:2004.13204), HouseDiffusion
  (arXiv:2211.13287) — graph-conditioned ML; non-deterministic / heavyweight /
  no overlap guarantee.
- `rectpack` (github.com/secnot/rectpack) — pure-Python packing, no adjacency.
- PuLP (github.com/coin-or/pulp) — optional LP/MIP, if an exact path is ever
  wanted.
