"""A deterministic 0–100 design score for a compiled plan.

The compiler tells an author *what* is wrong; this module says *how good* the
plan is as a single number an agent can hill-climb on — compare two candidate
plans, keep the better one, detect a regression. The score is a **contract**:
same plan in, same score out (pure arithmetic, no randomness, no LLM), and the
formula below is the whole definition.

Formula::

    total = clamp(100 - sum(components), 0, 100)

where each component is a penalty (points deducted):

    errors       100 flat if the compile has any error (or produced no plan) —
                 an unbuildable plan scores 0, full stop.
    warnings     8 per warning, capped at 40.
    infos        2 per design-quality info nudge, capped at 20.
    space        up to 10 — ground-floor room area / footprint, full marks at
                 the 85% coverage AREA_UNUSED expects, scaling to 10 at 0%; a
                 concentrated room-sized void also costs up to 6 even above 85%.
    circulation  up to 6 — hallway share of interior area; free up to 15%,
                 full penalty at 35% (a plan that is mostly corridor).
    proportion   up to 8 — habitable-room elongation past 1.7:1, bedrooms counted
                 double and worst-dominated (0.6·worst + 0.4·mean) so a lone tunnel
                 room can't be averaged away by squarer neighbours. The bar clears
                 the classical preferred ratios (4:3, √2, 3:2, φ, 5:3) so a room on
                 a proportioning system is never penalised for being on it.
    daylight     up to 8 — mean habitable-room glazing shortfall below the
                 IRC R303 8%-of-floor minimum (full penalty at zero glazing).
    topology     up to 15 — fraction of bedrooms whose only interior route to the
                 living core crosses a garage/shop (all severed → full 15). The
                 through-garage circulation defect as a continuous term, redundant
                 with the GARAGE_PASSTHROUGH warning so it can't be capped away.

Diagnostics dominate (their caps sum to 60 before the error gate); the five
continuous terms (47 max) refine, so two clean plans still rank — the one with
less waste, squarer rooms, more light, and a sound circulation topology wins. The
continuous terms overlap some diagnostics (AREA_UNUSED/AREA_VOID, HALL_TIGHT,
ROOM_PROPORTION, NATURAL_LIGHT, GARAGE_PASSTHROUGH) on purpose: the info/warning
gives the step, the margin gives the *gradient* an agent can descend even before
(or after) the threshold trips.

Each non-zero continuous component also carries a **cause** in
:attr:`ScoreReport.details` — a short string naming the worst offenders with
numbers ("bed_3 is 2.4:1, office is 2.1:1") — so a deduction is actionable
without cross-referencing the info list. Causes are explanation only: they
never change the arithmetic, and the totals are exactly what they were before
details existed.

    from barndsl import compile_file
    from barndsl.score import design_score
    print(design_score(compile_file("plan.barn")).total)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import GOOD_ASPECT, NATURAL_LIGHT_RATIO
from .elements import GARAGE_TYPES, HABITABLE_TYPES, PUBLIC_TYPES, Barndominium, RoomType
from .validation import (
    MIN_VOID_NOTE,
    Severity,
    largest_void,
    components_excluding,
    door_graph,
)

# --- weights (the contract; change these and the score changes meaning) ------

WARNING_PENALTY = 8.0  #: points per warning …
WARNING_CAP = 40.0  #: … up to this many
INFO_PENALTY = 2.0  #: points per design-quality info …
INFO_CAP = 20.0  #: … up to this many

SPACE_WEIGHT = 10.0  #: max penalty for unassigned footprint
SPACE_FULL_MARKS = 0.85  #: coverage at/above this loses nothing (AREA_UNUSED's bar)
VOID_SPACE_WEIGHT = 6.0  #: max penalty from one concentrated void (part of `space`)
VOID_FULL_FRAC = 0.10  #: a void this big a share of the footprint hits VOID_SPACE_WEIGHT
CIRCULATION_WEIGHT = 6.0  #: max penalty for hallway-heavy plans
CIRCULATION_FREE = 0.15  #: hallway share of interior area with no penalty
CIRCULATION_WORST = 0.35  #: share at/above which the full penalty applies
PROPORTION_WEIGHT = 8.0  #: max penalty for elongated habitable rooms
# GOOD_ASPECT (the bar elongation starts to cost past) is imported from
# .constants — the solver in layout2 optimises toward the same value, and it
# used to be declared separately in both places.
WORST_ASPECT_EXCESS = 1.5  #: excess (i.e. 3.2:1) at which the full penalty applies
BEDROOM_EXCESS_WEIGHT = 2.0  #: bedrooms count double in proportion (like layout2)
DAYLIGHT_WEIGHT = 8.0  #: max penalty for under-glazed habitable rooms
DAYLIGHT_RATIO = NATURAL_LIGHT_RATIO  #: IRC R303 glazing floor (the NAT_LIGHT base)
TOPOLOGY_WEIGHT = 15.0  #: max penalty for bedrooms reachable only through a garage/shop


@dataclass
class ScoreReport:
    """The scored view of one compile: a total and its explainable parts."""

    total: float
    components: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    #: Cause strings for the non-zero continuous components, keyed by component
    #: name in the components' own order — the worst offenders with numbers,
    #: e.g. ``{"proportion": "bed_3 is 2.4:1, office is 2.1:1 (…)"}``.
    #: Explanation only; the arithmetic lives entirely in ``components``.
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Stable JSON-able form (mirrors ``CompileResult.to_dict`` style)."""
        return {
            "total": self.total,
            "components": dict(self.components),
            "counts": dict(self.counts),
            "details": dict(self.details),
        }


# --- continuous components ----------------------------------------------------
# Each returns ``(points deducted, cause)`` — 0..weight, and a short offender
# string (or None when nothing deducts). The cause names the rooms/quantities
# the penalty was computed from; it never feeds back into the number.

#: How many offending rooms a cause string names before "+N more".
_DETAIL_LIMIT = 3


def _worst(offenders: list[tuple[float, str, str]]) -> str:
    """Join offender phrases worst-first (ties by room id), capped at
    :data:`_DETAIL_LIMIT` with a ``+N more`` tail."""
    offenders = sorted(offenders, key=lambda o: (-o[0], o[1]))
    parts = [o[2] for o in offenders[:_DETAIL_LIMIT]]
    more = len(offenders) - _DETAIL_LIMIT
    return ", ".join(parts) + (f" +{more} more" if more > 0 else "")


def _space_penalty(plan: Barndominium) -> tuple[float, str | None]:
    """Unassigned footprint. Coverage is a ground-floor concept (lofts sit above).

    Two ways a footprint wastes space, taking the harsher of the two:

    * **diffuse slack** — total ground-floor coverage below :data:`SPACE_FULL_MARKS`
      (85%), scaling to the full weight at 0% (the historical term).
    * **a concentrated void** — one connected room-sized gap that the coverage term
      forgives whenever overall coverage is high (a footprint 97% covered but with
      an 80 sqft dead pocket still reads as 97%). Charge for the largest single gap
      as a fraction of the footprint, so a room-sized hole costs *something* even
      above 85%. This is the score's continuous companion to the AREA_VOID info.
    """
    footprint = plan.footprint_area
    if footprint <= 0:
        return 0.0, None
    used = sum(r.area for r in plan.rooms if r.level == 0)
    frac = min(1.0, used / footprint)
    shortfall = max(0.0, SPACE_FULL_MARKS - frac) / SPACE_FULL_MARKS
    coverage_pen = SPACE_WEIGHT * min(1.0, shortfall)

    # Concentrated-void term: charged from the INFO bar (MIN_VOID_NOTE) so a
    # sub-error pocket still costs points and the loop has a gradient before the
    # cliff — a room-sized void (>= MIN_CONCENTRATED_VOID) is an ERROR now, which
    # zeroes the score before this term matters. A wall-thickness sliver of slack
    # between rooms still never pings. Scales the void's share of the footprint
    # into a penalty capped at VOID_SPACE_WEIGHT.
    void_area, bbox = largest_void(plan)
    void_pen = 0.0
    if void_area >= MIN_VOID_NOTE:
        void_pen = min(VOID_SPACE_WEIGHT, VOID_SPACE_WEIGHT * (void_area / footprint) / VOID_FULL_FRAC)

    penalty = max(coverage_pen, void_pen)
    if penalty <= 0.0:
        return penalty, None
    if void_pen > coverage_pen and bbox is not None:
        x1, y1, x2, y2 = bbox
        return penalty, (
            f"a concentrated {void_area:.0f} sqft void ({x1:.0f},{y1:.0f} to "
            f"{x2:.0f},{y2:.0f}) sits in an otherwise {frac * 100:.0f}%-covered "
            f"{footprint:.0f} sqft footprint"
        )
    return penalty, (
        f"ground-floor rooms cover {frac * 100:.0f}% of the {footprint:.0f} sqft "
        f"footprint ({footprint - used:.0f} sqft unassigned; free at "
        f"{SPACE_FULL_MARKS * 100:.0f}%+)"
    )


def _circulation_penalty(plan: Barndominium) -> tuple[float, str | None]:
    """Hallway share of interior area — corridors are overhead past a point."""
    interior = plan.interior_area
    if interior <= 0:
        return 0.0, None
    halls = [r for r in plan.rooms if r.type is RoomType.HALLWAY]
    frac = sum(r.area for r in halls) / interior
    band = CIRCULATION_WORST - CIRCULATION_FREE
    penalty = CIRCULATION_WEIGHT * min(1.0, max(0.0, (frac - CIRCULATION_FREE) / band))
    if penalty <= 0.0:
        return penalty, None
    worst = _worst([(r.area, r.id, f"{r.id} {r.area:.0f} sqft") for r in halls])
    return penalty, (
        f"hallways are {frac * 100:.0f}% of the interior ({worst}; free below "
        f"{CIRCULATION_FREE * 100:.0f}%)"
    )


def _proportion_penalty(plan: Barndominium) -> tuple[float, str | None]:
    """Habitable-room elongation past GOOD_ASPECT (halls/closets exempt).

    Two shape choices ported from the deterministic solver
    (:func:`barndsl.layout2._proportion_penalty`), because the old plain mean let a
    couple of bad rooms hide among many good ones — two 2.0:1 tunnel bedrooms among
    seven rooms diluted to ~0.6 pts:

    * **bedrooms count double** — a long thin bedroom is the most noticeable
      (you sleep in it), so its excess is weighted ``BEDROOM_EXCESS_WEIGHT``.
    * **worst-dominated aggregation** — ``0.6·worst + 0.4·mean`` instead of the
      mean alone, so the single worst room drives most of the penalty and can't be
      averaged away by squarer neighbours.
    """
    excesses: list[float] = []
    weights: list[float] = []
    offenders: list[tuple[float, str, str]] = []
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES:
            continue
        w = BEDROOM_EXCESS_WEIGHT if r.type is RoomType.BEDROOM else 1.0
        side = min(r.width, r.length)
        if side <= 0:
            excesses.append(WORST_ASPECT_EXCESS)  # degenerate: as bad as it gets
            weights.append(w)
            offenders.append((WORST_ASPECT_EXCESS * w, r.id, f"{r.id} has a zero side"))
            continue
        excess = max(0.0, max(r.width, r.length) / side - GOOD_ASPECT)
        excesses.append(excess)
        weights.append(w)
        if excess > 0.0:
            ratio = max(r.width, r.length) / side
            offenders.append((excess * w, r.id, f"{r.id} is {ratio:.1f}:1"))
    if not excesses:
        return 0.0, None
    # Weighted mean (bedrooms 2x) blended with the single worst weighted excess,
    # worst-dominated so a tunnel room can't be diluted by squarer neighbours.
    weighted = [e * wt for e, wt in zip(excesses, weights)]
    mean = sum(weighted) / sum(weights)
    worst = max(weighted)
    combined = 0.6 * worst + 0.4 * mean
    penalty = PROPORTION_WEIGHT * min(1.0, combined / WORST_ASPECT_EXCESS)
    if penalty <= 0.0 or not offenders:
        return penalty, None
    return penalty, f"{_worst(offenders)} (past the {GOOD_ASPECT:g}:1 target)"


def _daylight_penalty(plan: Barndominium) -> tuple[float, str | None]:
    """Mean glazing shortfall below the 8% floor across habitable rooms.

    At/above 8% a room costs nothing (the floor is the target, not a ceiling);
    below it the deficit is proportional, so 5% glazing scores better than 2% —
    a gradient where the pass/fail NATURAL_LIGHT check is a step.
    """
    deficits: list[float] = []
    offenders: list[tuple[float, str, str]] = []
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES or r.area <= 0:
            continue
        required = DAYLIGHT_RATIO * r.area
        glazed = sum(w.glazed_area for w in plan.windows_for(r.id))
        deficit = max(0.0, (required - glazed) / required)
        deficits.append(deficit)
        if deficit > 0.0:
            offenders.append((deficit, r.id, f"{r.id} at {glazed / r.area * 100:.1f}%"))
    if not deficits:
        return 0.0, None
    penalty = DAYLIGHT_WEIGHT * (sum(deficits) / len(deficits))
    if penalty <= 0.0 or not offenders:
        return penalty, None
    return penalty, (
        f"{_worst(offenders)} (below the {DAYLIGHT_RATIO * 100:.0f}% glazing floor)"
    )


def _topology_penalty(plan: Barndominium) -> tuple[float, str | None]:
    """Fraction of bedrooms whose only interior route to the public core crosses a
    garage/shop — the through-garage circulation defect, as a continuous term.

    Remove every garage/shop from the door graph; a bedroom no longer in the
    component holding the public rooms (living/kitchen/dining) is *severed* — you
    must cross the vehicle bay to reach it. Penalise proportionally: all bedrooms
    severed → the full weight, so the defect can't be capped away by the warning
    (GARAGE_PASSTHROUGH) alone. Redundant with that warning on purpose."""
    garages = {r.id for r in plan.rooms if r.type in GARAGE_TYPES}
    if not garages:
        return 0.0, None
    publics = {r.id for r in plan.rooms if r.type in PUBLIC_TYPES}
    beds = [r.id for r in plan.rooms if r.type is RoomType.BEDROOM]
    if not publics or not beds:
        return 0.0, None
    # The same topology question validation's GARAGE_PASSTHROUGH asks.
    comps = components_excluding(door_graph(plan), garages)
    public_comp = next((c for c in comps if c & publics), None)
    if public_comp is None:
        return 0.0, None  # the public rooms themselves sit behind the garage
    severed = sorted(b for b in beds if b not in public_comp)
    if not severed:
        return 0.0, None
    penalty = TOPOLOGY_WEIGHT * (len(severed) / len(beds))
    names = ", ".join(f"'{b}'" for b in severed)
    return penalty, (
        f"{len(severed)} of {len(beds)} bedroom(s) ({names}) reach the "
        "living core only through a garage/shop"
    )


# --- the score ----------------------------------------------------------------


def design_score(result) -> ScoreReport:
    """Score a :class:`~barndsl.compiler.CompileResult` on the 0–100 contract.

    Deterministic and pure: reads only ``result.diagnostics`` and
    ``result.plan``. An errored (or plan-less) compile scores 0; the continuous
    components are still reported when a plan exists, so an agent fixing errors
    can already see what else needs work.
    """
    # An *accepted* diagnostic (downgraded by an `# barndsl: accept CODE` pragma)
    # is a documented, deliberate deviation — it survives as an audited INFO but
    # must not deduct at any severity, so the score excludes it from every count.
    active = [d for d in result.diagnostics if not getattr(d, "accepted", False)]
    counts = {
        "error": sum(1 for d in active if d.severity is Severity.ERROR),
        "warning": sum(1 for d in active if d.severity is Severity.WARNING),
        "info": sum(1 for d in active if d.severity is Severity.INFO),
    }
    components: dict[str, float] = {
        "errors": 100.0 if (result.plan is None or result.errors) else 0.0,
        "warnings": min(WARNING_CAP, WARNING_PENALTY * counts["warning"]),
        "infos": min(INFO_CAP, INFO_PENALTY * counts["info"]),
    }
    plan = result.plan
    details: dict[str, str] = {}
    continuous = (
        ("space", _space_penalty),
        ("circulation", _circulation_penalty),
        ("proportion", _proportion_penalty),
        ("daylight", _daylight_penalty),
        ("topology", _topology_penalty),
    )
    for name, penalty in continuous:
        points, cause = penalty(plan) if plan is not None else (0.0, None)
        components[name] = points
        if cause is not None:
            details[name] = cause

    components = {k: round(v, 2) for k, v in components.items()}
    # A cause explains a *visible* deduction: drop any whose points rounded to 0.
    details = {k: v for k, v in details.items() if components[k]}
    total = max(0.0, min(100.0, 100.0 - sum(components.values())))
    return ScoreReport(
        total=round(total, 1), components=components, counts=counts, details=details
    )
