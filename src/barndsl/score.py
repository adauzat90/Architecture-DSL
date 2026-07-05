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
                 the 85% coverage AREA_UNUSED expects, scaling to 10 at 0%.
    circulation  up to 6 — hallway share of interior area; free up to 15%,
                 full penalty at 35% (a plan that is mostly corridor).
    proportion   up to 8 — mean habitable-room elongation past 1.6:1
                 (full penalty at a mean 3.1:1 — long thin rooms don't furnish).
    daylight     up to 8 — mean habitable-room glazing shortfall below the
                 IRC R303 8%-of-floor minimum (full penalty at zero glazing).

Diagnostics dominate (their caps sum to 60 before the error gate); the four
continuous terms (32 max) refine, so two clean plans still rank — the one with
less waste, squarer rooms, and more light wins. The continuous terms overlap
some diagnostics (AREA_UNUSED, HALL_TIGHT, ROOM_PROPORTION, NATURAL_LIGHT) on
purpose: the info gives the step, the margin gives the *gradient* an agent can
descend even before (or after) the threshold trips.

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

from .elements import HABITABLE_TYPES, Barndominium, RoomType
from .validation import Severity

# --- weights (the contract; change these and the score changes meaning) ------

WARNING_PENALTY = 8.0  #: points per warning …
WARNING_CAP = 40.0  #: … up to this many
INFO_PENALTY = 2.0  #: points per design-quality info …
INFO_CAP = 20.0  #: … up to this many

SPACE_WEIGHT = 10.0  #: max penalty for unassigned footprint
SPACE_FULL_MARKS = 0.85  #: coverage at/above this loses nothing (AREA_UNUSED's bar)
CIRCULATION_WEIGHT = 6.0  #: max penalty for hallway-heavy plans
CIRCULATION_FREE = 0.15  #: hallway share of interior area with no penalty
CIRCULATION_WORST = 0.35  #: share at/above which the full penalty applies
PROPORTION_WEIGHT = 8.0  #: max penalty for elongated habitable rooms
GOOD_ASPECT = 1.6  #: elongation past this ratio starts to cost (ROOM_PROPORTION's bar)
WORST_ASPECT_EXCESS = 1.5  #: mean excess (i.e. 3.1:1) at which the full penalty applies
DAYLIGHT_WEIGHT = 8.0  #: max penalty for under-glazed habitable rooms
DAYLIGHT_RATIO = 0.08  #: IRC R303 glazing floor: 8% of habitable floor area


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
    """Unassigned footprint. Coverage is a ground-floor concept (lofts sit above)."""
    footprint = plan.footprint_area
    if footprint <= 0:
        return 0.0, None
    used = sum(r.area for r in plan.rooms if r.level == 0)
    frac = min(1.0, used / footprint)
    shortfall = max(0.0, SPACE_FULL_MARKS - frac) / SPACE_FULL_MARKS
    penalty = SPACE_WEIGHT * min(1.0, shortfall)
    if penalty <= 0.0:
        return penalty, None
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
    """Mean habitable-room elongation past GOOD_ASPECT (halls/closets exempt)."""
    excesses: list[float] = []
    offenders: list[tuple[float, str, str]] = []
    for r in plan.rooms:
        if r.type not in HABITABLE_TYPES:
            continue
        side = min(r.width, r.length)
        if side <= 0:
            excesses.append(WORST_ASPECT_EXCESS)  # degenerate: as bad as it gets
            offenders.append((WORST_ASPECT_EXCESS, r.id, f"{r.id} has a zero side"))
            continue
        excess = max(0.0, max(r.width, r.length) / side - GOOD_ASPECT)
        excesses.append(excess)
        if excess > 0.0:
            ratio = max(r.width, r.length) / side
            offenders.append((excess, r.id, f"{r.id} is {ratio:.1f}:1"))
    if not excesses:
        return 0.0, None
    mean = sum(excesses) / len(excesses)
    penalty = PROPORTION_WEIGHT * min(1.0, mean / WORST_ASPECT_EXCESS)
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
